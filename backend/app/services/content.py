"""M4 — Content pipeline. LiteLLM draft -> HUMAN edit gate (draft->edited) -> offers + disclosure.

Generation NEVER publishes: it only creates pages in status='draft'. A human moves
draft -> edited (`mark_edited`); publish (M5) reads ONLY 'edited'. This is the hard
editorial gate from PLAN §2. Content must be topically coherent with the offer.
"""
import html
import re

import hashlib

import nh3

from app.services import page_doc
from app.services.locales import LANG_NAMES, norm_lang, resolve_lang, t

# Русская редакция раскрытия — для обратной совместимости; страницы берут текст по своему языку
# из services/locales (t(lang, "disclosure")).
from app.services.locales import TEXTS as _LOCALES
DISCLOSURE = _LOCALES["ru"]["disclosure"]

# rel любой ссылки внутри тела страницы (S6-06/S5-08): nh3 сам ставит этот rel на каждую <a> и
# вырезает чужой (rel в allowlist атрибутов НЕТ, иначе nh3 паникует) — LLM/редактор не могут
# выпустить dofollow-партнёрку. Внутренних ссылок у сайта нет, поэтому отдельной ветки для них нет.
LINK_REL = "sponsored nofollow noopener"

# Sanitize-on-write allowlist: published pages are public sites, so hostile HTML
# (<script>/<iframe>/on*/style) must never reach the DB. Tags match what M4 emits.
_ALLOWED_TAGS = {"h2", "h3", "h4", "p", "ul", "ol", "li", "a", "strong", "em", "b", "i",
                 "br", "blockquote", "table", "thead", "tbody", "tr", "th", "td",
                 "code", "pre", "figure", "figcaption", "img"}
_ALLOWED_ATTRS = {"a": {"href", "title"}, "img": {"src", "alt", "width", "height"}}

# <img> (S6-04/F8-10): ТОЛЬКО локальные картинки сайта assets/<имя>.svg. Внешний src, data:,
# javascript:, протокол-относительный //host — вырезаются. Относительный путь нормализуется к
# корневому (/assets/x.svg): на странице /vs/ относительный assets/x.svg указал бы в /vs/assets/.
_IMG_SRC = re.compile(r"^/?assets/([A-Za-z0-9][A-Za-z0-9._-]*)\.svg$")


def _attr_filter(tag: str, attr: str, value: str):
    if tag == "a" and attr == "href":
        # протокол-относительная «//host/…» — внешний хост без схемы, мимо allowlist http(s)
        # браузер выкидывает \t\r\n и трактует «\» как «/»: «/\evil.com», «\\evil.com»,
        # «/&#9;/evil.com» (после декода «/<tab>/evil.com») — тоже внешний хост
        norm = re.sub(r"[\t\r\n]", "", (value or "").strip()).replace("\\", "/")
        return None if norm.startswith("//") else value
    if tag != "img":
        return value
    if attr == "src":
        m = _IMG_SRC.match((value or "").strip())
        return f"/assets/{m.group(1)}.svg" if m else None
    if attr in ("width", "height"):
        return value if re.fullmatch(r"\d{1,4}", value or "") else None
    return value


def _sanitize(body: str | None) -> str:
    """Strip everything outside the allowlist (script/iframe/event-handlers/style)."""
    out = nh3.clean(body or "", tags=_ALLOWED_TAGS, attributes=_ALLOWED_ATTRS, link_rel=LINK_REL,
                    url_schemes={"http", "https"},        # без mailto:/tel:/data:/javascript:
                    attribute_filter=_attr_filter,
                    set_tag_attribute_values={"img": {"loading": "lazy"}})
    # <img> без (прошедшего фильтр) src — мусор: убираем целиком
    return re.sub(r"<img(?![^>]*\ssrc=)[^>]*>", "", out)


# F28 (аудит 2026-07-14): affiliate_link идёт прямо в href через html.escape(), а html.escape
# экранирует ТОЛЬКО HTML-спецсимволы (< > & " ') — схему НЕ проверяет. "javascript:alert(1)"
# проходит насквозь и выполняется по клику на опубликованной странице (реальный XSS, не
# гипотетический: rel="sponsored nofollow" от исполнения ссылки не защищает). allowlist схем
# проверяется В ДВУХ МЕСТАХ (defense in depth) — на создании оффера (panel.py/pipeline.py: форма
# может быть обойдена прямым API-вызовом) И здесь, в render_html (egress — офферы, заведённые до
# этой проверки, тоже не должны попасть в HTML с опасной схемой).
_ALLOWED_URL_SCHEMES = {"http", "https"}


def is_safe_url(url: str | None) -> bool:
    """True только для http(s)-ссылок. javascript:/data:/vbscript:/file:/пустая схема -> False."""
    from urllib.parse import urlparse
    try:
        u = urlparse((url or "").strip())
        # netloc обязателен: «https:» / «https:///x» — схема есть, адресата нет (S6-07)
        return u.scheme.lower() in _ALLOWED_URL_SCHEMES and bool(u.netloc)
    except ValueError:
        return False


def scaffold(brand: str, niche: str | None = None, lang: str = "en") -> list[dict]:
    """Minimal site structure (page specs). Заголовки — на языке сайта (services/locales).
    Tune per niche/SERP later."""
    return [
        {"url_path": "/", "title": t(lang, "title_review", brand=brand), "kind": "review"},
        {"url_path": "/vs", "title": t(lang, "title_comparison", brand=brand), "kind": "comparison"},
        {"url_path": "/setup", "title": t(lang, "title_howto", brand=brand), "kind": "howto"},
    ]


def _system_prompt(lang: str, country: str | None = None) -> str:
    # F27 (аудит 2026-07-14): раньше здесь стояло "замеры скорости" — а vertical_data.py не
    # содержит ни одного реального измерения скорости ни по одному бренду. Промпт прямо
    # ПРОВОЦИРОВАЛ модель выдумывать конкретные цифры (Mbps/пинги), а гейт редактуры вместо
    # проверки реальных данных превращался в "поймай галлюцинацию". Формулировка ниже просит
    # то, что реально ЕСТЬ в vertical_data (обход гео-блоков, юзкейсы, устройства/протоколы),
    # и явно требует не придумывать числа, которых не давали.
    # G8 (S6-03/S7-06): промпт английский, язык вывода и рынок — параметры, а не зашитый русский.
    name = LANG_NAMES[norm_lang(lang)]
    market = f" for readers in {country}" if country else ""
    return (f"You are an experienced VPN review editor. Write in {name}{market}: natural, native-level "
            f"{name}, to the point, genuinely useful (bypassing geo-blocks, use cases, supported devices "
            "and protocols), no filler or marketing fluff. Do not invent specific numbers (speed, "
            "ping, percentages) that are not in the supplied data — if no measurements are given, "
            "write without them. Return an HTML FRAGMENT only (<h2>/<h3>/<p>/<ul>, no <html>/<body>, "
            "no <h1>, no images). This is a DRAFT for later human editing.")


def _page_prompt(spec: dict, brand: str, vertical_data: str | None,
                 competitor: list[str] | None = None, lang: str = "en",
                 promo: tuple[str, str] | None = None) -> str:
    # блок фактов приходит на русском (vertical_data) — модель переводит его в язык вывода
    data = ("\n\nReal vertical data (use it as facts; it is written in Russian — translate it into "
            f"the output language, keep numbers and dates exact):\n{vertical_data}") if vertical_data else ""
    comp = ""
    if competitor:
        topics = "\n".join(f"- {h}" for h in competitor)
        comp = ("\n\nTopics covered by the top competitor (for completeness; do NOT copy the wording, "
                f"pick what is relevant to this page):\n{topics}")
    # условия промокода — единственный бонус, о котором можно писать: без них модель сочиняет скидки
    deal = (f"\n\nPromo code for readers: {promo[0]}. What it gives (the ONLY bonus you may mention; "
            f"translate into the output language, do not invent other discounts or terms): {promo[1]}"
            ) if promo and promo[0] and promo[1] else ""
    return (f"Topic: {spec['title']} (type: {spec['kind']}). Brand: {brand}. Output language: "
            f"{LANG_NAMES[norm_lang(lang)]}. Make a coherent draft with a heading structure.{data}{comp}{deal}")


def _clean(body: str) -> str:
    """Strip a leading/trailing ```lang fence the model sometimes wraps output in."""
    b = body.strip()
    b = re.sub(r"^```[a-zA-Z]*\n?", "", b)
    b = re.sub(r"\n?```$", "", b)
    return b.strip()


# Схема ответа писателя — текстом для модели. Имена полей обязаны совпадать с page_doc.PageDoc:
# лишнее поле pydantic молча отбросит, недостающее обязательное сожжёт повтор (их сверяет test_writer).
_DOC_SCHEMA = """{
  "meta": {"title": "заголовок страницы, 10–110 символов", "description": "описание для поиска, 40–200 символов"},
  "verdict": {"score": оценка редакции числом от 1 до 10, "summary": "вывод в одном-двух предложениях",
              "for_whom": "кому подойдёт", "not_for_whom": "кому не подойдёт"},
  "pros": ["плюс", ...],
  "cons": ["минус", ...],
  "sections": [{"h2": "заголовок раздела", "paragraphs": ["абзац", ...], "bullets": ["пункт списка", ...],
                "h3s": [{"h3": "подзаголовок", "paragraphs": ["абзац", ...]}]}, ...],
  "table": {"columns": ["название колонки", ...], "rows": [["ячейка", ...], ...]},
  "steps": [{"title": "название шага", "text": "что сделать", "platform": "платформа или null"}, ...],
  "faq": [{"q": "вопрос", "a": "ответ"}, ...],
  "sources": ["URL источника из брифа", ...]
}"""


def writer_system(lang: str, country: str | None, guides_text: str | None) -> str:
    """Системный промпт писателя: роль, язык и рынок, правила письма оператора, запреты, схема ответа.
    Данные страницы (бренд, источники, факты, объём) несёт бриф — `brief.brief_text`."""
    name = LANG_NAMES[norm_lang(lang)]
    market = f" Читатели — из страны: {country}." if country else ""
    rules = guides_text.strip() if guides_text else ""
    parts = [
        "Ты — опытный редактор сайта с обзорами VPN-сервисов. По брифу пользователя ты пишешь ОДНУ страницу "
        "сайта: полезную читателю, конкретную, без воды и рекламных штампов.",
        # бриф и правила написаны по-русски, а страница — на языке рынка: без явной оговорки русские
        # формулировки брифа протекают в текст на другом языке
        f"Язык текста страницы: {name}. Каждое строковое значение ответа пиши на этом языке, как носитель."
        f"{market} Бриф пользователя и правила оператора написаны по-русски — это рабочие материалы: передавай "
        "их смысл на языке страницы, русские формулировки в ответ не переноси (если язык страницы не русский).",
    ]
    if rules:
        parts.append("Правила письма оператора (обязательны для этой страницы):\n" + rules)
    parts += [
        "Запреты:\n"
        "- Не выдумывай числа и характеристики (скорость, цены, число серверов и стран, проценты, сроки), "
        "которых нет в брифе. Нет данных — пиши без цифр.\n"
        '- Каждую использованную цифру подтверждай: URL её источника из брифа положи в "sources".\n'
        "- Не копируй формулировки источников: бери у них факты и структуру, текст пиши своими словами.\n"
        "- Слово «неизвестно» в фактах бренда значит «данных нет»: не превращай его в цифры и не додумывай.",
        "Формат ответа — ТОЛЬКО один JSON-объект по схеме ниже: без Markdown-ограды (```), без текста до и "
        "после него. Строки — простой текст без HTML и без Markdown-разметки.\n" + _DOC_SCHEMA,
        'Всегда обязательны "meta" и "sections" (не меньше двух разделов, в каждом непустой "paragraphs"). '
        "Обязательные блоки по типу страницы:\n"
        '- обзор (review): "verdict", "pros", "cons";\n'
        '- сравнение (comparison): "table", "pros", "cons";\n'
        '- пошаговая инструкция (howto): "steps".\n'
        'Остальные блоки ("bullets", "h3s", "faq" и блоки другого типа страницы) добавляй, только если в брифе '
        'для них есть материал; иначе не указывай их. В "table" — от 2 до 6 колонок и от 1 до 20 строк, в '
        "каждой строке ровно столько ячеек, сколько колонок.",
    ]
    return "\n\n".join(parts)


def write_doc(llm, *, system: str, prompt: str,
              issues: list[str] | None = None) -> tuple[page_doc.PageDoc | None, str | None]:
    """Страница от модели: `(doc, None)` или `(None, причина словами)`. Не больше двух вызовов.

    Ответ, не прошедший схему (ограду и текст вокруг JSON снимает page_doc.parse), повторяется ОДИН
    раз с текстом ошибки. Пустой ответ и исключение клиента (таймаут, 5xx шлюза, LlmEmptyContent) —
    тоже проваленная попытка, а не падение прогона: POST к модели BaseClient сам не повторяет.
    `issues` — замечания критика при переписывании: уходят в промпт отдельным блоком."""
    from app.config import settings
    model = settings.LLM_WRITER_MODEL or settings.LLM_MODEL
    if issues:
        prompt += ("\n\n## Замечания редактора, которые нужно устранить\n"
                   + "\n".join(f"- {x}" for x in issues))
    ask, reason = prompt, None
    for _ in range(2):
        try:
            raw = llm.complete(system, ask, model=model)
        except Exception as e:  # noqa: BLE001 — любая осечка клиента = причина отказа, не трейс в сводке
            reason = f"писатель не ответил: {type(e).__name__}: {e}"[:300]
            continue
        if not (raw or "").strip():
            reason = "писатель вернул пустой ответ"
            continue
        try:
            return page_doc.parse(raw), None
        except ValueError as e:
            reason = f"ответ писателя не прошёл схему: {e}"
            # добавка — только после ошибки схемы: пустой ответ и осечку клиента повторяем тем же промптом
            ask = (f"{prompt}\n\nПредыдущий ответ не прошёл проверку схемы: {e}. Верни ТОЛЬКО исправленный "
                   "JSON-объект — без текста до и после него.")
    return None, reason


def _write_page(llm, rows: list, spec: dict, *, brand: str, lang: str, country: str | None, promo: tuple,
                vertical: str | None, issues: list[str] | None = None) -> tuple[page_doc.PageDoc | None, str | None]:
    """Одна страница нового пути: правила оператора + бриф из досье -> write_doc."""
    from app.services import brief, guides
    kind = spec["kind"]
    system = writer_system(lang, country, guides.load_guides(lang, kind)["text"])
    prompt = brief.brief_text(brief.build_brief(rows, kind), brand=brand, kind=kind, title=spec["title"],
                              lang_name=LANG_NAMES[norm_lang(lang)], country=country, promo=promo,
                              vertical=vertical)
    return write_doc(llm, system=system, prompt=prompt, issues=issues)


def _apply_doc(page, doc: page_doc.PageDoc, kind: str, lang: str) -> None:
    """Записать страницу писателя в строку Page (новую или переписываемую). Статус — всегда draft:
    переписанный текст никто не читал, прежние «вычитано»/«опубликовано» и оценка критика к нему не
    относятся. url_path, lang, offer_id, published_at и поля индексации не трогаем — это история строки."""
    page.title = doc.meta.title
    page.body = _sanitize(page_doc.render_blocks(doc, kind, lang))
    page.blocks = doc.model_dump()
    page.blocks_stale = False
    page.status = "draft"
    page.critic_score = page.critic_notes = page.critic_checked_at = None


def _hand_edited(page, seen_body: str | None) -> bool:
    """Страницу правили руками: флаг blocks_stale либо тело не то, что мы видели до вызова модели
    (модель пишет минуты, редактор панели всё это время открыт; у страниц старого пути blocks нет,
    и флаг им не ставится — их ловит сравнение тела)."""
    return bool(page.blocks_stale) or (page.body or "") != (seen_body or "")


# Статусы сайта, в которых можно генерировать контент (инфраструктура уже поднята provision()).
GENERATE_STATUSES = frozenset({"content", "published", "monitoring"})


def site_offer(db, site):
    """Оффер, ЯВНО привязанный к сайту, или None. Глобального «первого активного оффера
    портфеля» больше нет (S6-13/S7-12): сайт без оффера не генерируется и не получает чужую
    ссылку при публикации. Приоритет — Site.offer_id; legacy-сайт без него берёт самый ранний
    из явно привязанных SiteOffer. Выключенный оффер не подходит (None)."""
    from sqlalchemy import select
    from app.models.offer import Offer, SiteOffer
    if site.offer_id is not None:
        off = db.get(Offer, site.offer_id)
        return off if off is not None and off.active else None
    return db.execute(
        select(Offer).join(SiteOffer, SiteOffer.offer_id == Offer.id)
        .where(SiteOffer.site_id == site.id, Offer.active.is_(True))
        .order_by(Offer.id).limit(1)).scalar_one_or_none()


def status_refusal(site) -> str | None:
    """Почему сайт нельзя генерировать по статусу (None — можно). S6-12/S7-05: раньше статус
    безусловно ставился в 'content' — сайт в `provisioning` перескакивал провижн (автопилот брал
    только provisioning и больше его не доводил, а publish потом выкладывал файлы без зоны и
    vhost'а), а published откатывался назад."""
    if site.status not in GENERATE_STATUSES:
        return (f"сайт #{site.id} в статусе «{site.status}»: сначала provision — "
                "контент пишется для готовой инфраструктуры")
    return None


def generate_site(site_id: int, lang: str | None = None, vertical_data: str | None = None,
                  use_competitor: bool = False, rewrite: bool = False) -> int:
    """Generate draft pages for a site via LiteLLM. Returns count created (+ rewritten). status stays 'draft'.

    Два пути. У сайта есть досье конкурентов (site_research) — пишет ПИСАТЕЛЬ: бриф из досье + правила
    оператора -> JSON по схеме PageDoc -> Page.blocks и его рендер в Page.body. Досье нет — старый
    путь: один промпт -> HTML-фрагмент (blocks пуст).

    rewrite: только путь с досье. Кроме недостающих страниц переписывает на месте существующие
    (draft|edited|published -> draft, та же строка), кроме правленых руками (blocks_stale).

    lang: язык сайта. None -> берётся сам (S6-02/S7-06): язык уже написанных страниц сайта,
    иначе рынок домена (Domain.market_lang), иначе язык оффера, иначе en — НЕ 'ru' по умолчанию.
    Язык без словаря шаблона (pl, ja…) -> ValueError, а не молча английский.

    use_competitor: подмешать структуру тем от топ-конкурента (A-Parser, best-effort).
    По умолчанию off — сеть не дёргается в тестах/скриптах; панель включает явно.
    Только старый путь: с досье структуру конкурентов уже несёт бриф.

    Идёт задачей реестра `generate` (S6-11/S7-14/F8-07): прогресс по страницам, «стоп», один
    прогон за раз (LLM — единственный ресурс). Кто бы ни позвал — панель через jobs.spawn,
    автопилот, API — карточка на Пульте появляется сама.
    """
    from app.services import jobs
    with jobs.track("generate") as run:
        return _generate_site(site_id, lang, vertical_data, use_competitor, run, rewrite)


def _generate_site(site_id, lang, vertical_data, use_competitor, run, rewrite=False) -> int:
    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError
    from app.db import SessionLocal
    from app.models.site import Site, Page
    from app.models.offer import Offer
    from app.models.domain import Domain
    from app.integrations.llm import LlmClient
    from app.services import jobs

    # ФАЗА 1 — короткая сессия: всё, что нужно знать до LLM. Дальше БД не держим минутами (F8-07).
    with SessionLocal() as db:
        site = db.get(Site, site_id)
        if site is None:
            raise ValueError(f"site {site_id} not found")
        refusal = status_refusal(site)
        if refusal:
            raise ValueError(refusal)

        existing_pages = db.execute(
            select(Page).where(Page.site_id == site_id).order_by(Page.id)).scalars().all()
        existing_paths = {p.url_path for p in existing_pages}
        existing_page = existing_pages[0] if existing_pages else None

        offer = None
        if existing_page is not None:
            # Дозаполнение (S4/S5, аудит 2026-07-18): сайт уже частично сгенерирован —
            # наследуем lang/offer от уже существующих страниц, а не резолвим заново.
            # Иначе повторный вызов (второй клик / автопилотная стадия generate после
            # LLM-осечки на части спек) может дописать недостающие страницы на другом
            # языке или под другим брендом, чем уже созданные, — нарушая "один домен =
            # одно гео/язык" и рассинхронизируя publish.py с телом контента.
            lang = existing_page.lang or lang
            offer = db.get(Offer, existing_page.offer_id) if existing_page.offer_id else None
        if offer is None:
            # тематическая связность: бренд — из оффера, ЯВНО привязанного к сайту (Site.offer_id),
            # а не «самого раннего активного» (S6-13/S7-12): иначе несколько сайтов портфеля
            # пишутся про один бренд, а ссылка при публикации уходит на другой.
            offer = site_offer(db, site)
        if offer is None:
            raise ValueError(
                f"сайт #{site_id}: оффер не привязан (или выключен) — сначала привяжи активный "
                "оффер на карточке сайта: без него страницы получились бы про чужой бренд")
        brand, offer_id, niche = offer.brand, offer.id, site.niche
        promo = (offer.promo_code, offer.promo_terms)
        dom = db.get(Domain, site.domain_id)
        lang = resolve_lang(lang, dom.market_lang if dom else None, offer.language)
        country = offer.country
        # досье решает путь генерации. Строки (и страницы сайта — их читает переписывание) отсоединяем:
        # сессия закрывается до LLM, а данные нужны после
        from app.services import research
        rows = research.dossier(db, site_id)
        db.expunge_all()

    # information gain (PLAN §2): подмешиваем реальные факты вертикали, если бренд знаком.
    # Явно переданный vertical_data приоритетнее (напр. свежий фид). Неизвестный бренд -> None.
    if vertical_data is None:
        from app.services.vertical_data import vertical_block
        vertical_data = vertical_block(brand)

    if rows:
        return _write_site(site_id, run, rows, existing_pages, rewrite, brand=brand, niche=niche, lang=lang,
                           country=country, promo=promo, vertical=vertical_data, offer_id=offer_id)
    # Досье пусто -> старый путь, без изменений. Панель и автопилот без досье сюда не приходят (сначала
    # собирают его); ветка жива для API/скриптов и тестов старого пути. Переписывать по старому промпту
    # нечем — существующие страницы остаются как есть, и это сказано словами, а не молчаливым нулём.
    if rewrite:
        jobs.report(run, message="досье конкурентов нет — существующие страницы не переписаны (сначала собери досье)")

    # опц. карта тем от топ-конкурента (best-effort: осечка -> None, генерация идёт без неё)
    competitor = None
    if use_competitor:
        from app.services.competitor import outline_for
        got = outline_for(brand, lang=lang)
        competitor = got["headings"] if got else None

    llm = LlmClient()
    todo = [sp for sp in scaffold(brand, niche, lang) if sp["url_path"] not in existing_paths]
    created = 0
    jobs.report(run, done=0, total=len(todo))
    for i, spec in enumerate(todo):
        if jobs.cancelled(run):
            raise jobs.Cancelled()           # уже записанные страницы остаются (коммит по странице)
        jobs.report(run, done=i, total=len(todo), current=spec["title"])
        body = _sanitize(_clean(llm.complete(_system_prompt(lang, country),
                                             _page_prompt(spec, brand, vertical_data, competitor, lang, promo))))
        if not body.strip():
            continue  # empty LLM output (null/blocked): skip page, don't crash the batch
        # ФАЗА 2 — коммит КАЖДОЙ страницы сразу: осечка на 3-й не выбрасывает оплаченные токены
        # 1-й и 2-й (F8-07). F26: фиксируем, под какой оффер и язык страница реально написана —
        # publish.py читает это отсюда, а не пересчитывает (см. миграцию 0018).
        with SessionLocal() as db:
            db.add(Page(site_id=site_id, url_path=spec["url_path"], title=spec["title"],
                        status="draft", body=body, lang=lang, offer_id=offer_id))
            try:
                db.commit()
            except IntegrityError:
                # uq_page_per_path (site_id, url_path) — миграция 0014. Сюда попадаем НЕ от кривых
                # данных, а от гонки двух ПРОЦЕССОВ: кнопка «сгенерировать» в панели и стадия
                # generate автопилотного свипа в воркере вошли одновременно, оба честно увидели
                # «страницы нет» и оба вставили один путь. Инвариант отбил вторую вставку — и
                # оператор обязан прочитать это словами, а не SQL-трейсом в сводке свипа.
                # Остальное допишет победивший прогон; наши уже закоммиченные страницы валидны.
                db.rollback()
                raise ValueError(
                    f"страницы сайта #{site_id} прямо сейчас создаёт другой прогон — "
                    f"генерация пропущена, дубли не заводим") from None
        created += 1
    jobs.report(run, done=len(todo), total=len(todo), current="")
    return created


# Статусы страницы, которую переписывание берёт в работу (других у Page сейчас нет; список явный,
# чтобы новый статус не попал под перезапись молча).
REWRITE_STATUSES = frozenset({"draft", "edited", "published"})


def _write_site(site_id: int, run, rows: list, existing_pages: list, rewrite: bool, *, brand: str,
                niche: str | None, lang: str, country: str | None, promo: tuple, vertical: str | None,
                offer_id: int) -> int:
    """Путь с досье: страницы пишет писатель (PageDoc). Возвращает создано + переписано.

    Проваленная страница (два ответа мимо схемы, молчащая модель) НЕ создаётся и НЕ меняется: причина
    уходит в сообщение задачи, прогон закрывается «с замечаниями», остальные страницы пишутся."""
    from sqlalchemy.exc import IntegrityError
    from app.db import SessionLocal
    from app.models.site import Page
    from app.integrations.llm import LlmClient
    from app.services import jobs

    by_path = {p.url_path: p for p in existing_pages}
    todo, hand_edited = [], 0
    for spec in scaffold(brand, niche, lang):
        old = by_path.get(spec["url_path"])
        if old is None:
            todo.append((spec, None))
        elif rewrite and old.blocks_stale:
            hand_edited += 1                 # ручная правка дороже свежего текста модели — не затираем
        elif rewrite and old.status in REWRITE_STATUSES:
            todo.append((spec, old))

    llm = LlmClient(timeout=600)             # страница на 2000 слов через шлюз идёт минуты
    written, failed = 0, []
    jobs.report(run, done=0, total=len(todo))
    for i, (spec, old) in enumerate(todo):
        if jobs.cancelled(run):
            raise jobs.Cancelled()           # уже записанные страницы остаются (коммит по странице)
        jobs.report(run, done=i, total=len(todo), current=spec["title"])
        doc, err = _write_page(llm, rows, spec, brand=brand, lang=lang, country=country, promo=promo,
                               vertical=vertical)
        if err:
            failed.append((spec["url_path"], err))
            continue
        # коммит КАЖДОЙ страницы сразу, как в старом пути: осечка на 3-й не выбрасывает токены 1-й и 2-й
        with SessionLocal() as db:
            if old is None:
                page = Page(site_id=site_id, url_path=spec["url_path"], lang=lang, offer_id=offer_id)
                db.add(page)
            else:
                page = db.get(Page, old.id)
                if page is None:
                    failed.append((spec["url_path"], "страница исчезла, пока модель писала"))
                    continue
                if _hand_edited(page, old.body):
                    hand_edited += 1
                    continue
            _apply_doc(page, doc, spec["kind"], lang)
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                if old is not None:
                    raise
                # uq_page_per_path: гонка двух процессов на вставке одного пути — см. старый путь выше
                raise ValueError(
                    f"страницы сайта #{site_id} прямо сейчас создаёт другой прогон — "
                    f"генерация пропущена, дубли не заводим") from None
        written += 1
    jobs.report(run, done=len(todo), total=len(todo), current="")
    notes = []
    if hand_edited:
        notes.append(f"не тронуты, правлены вручную: {hand_edited}")
    if failed:
        cut = 280 // len(failed)             # message — 400 симв. на всё: причины делят место поровну
        notes.append("не написаны: " + "; ".join(f"{path} — {err[:cut]}" for path, err in failed))
    if notes:
        jobs.report(run, message=f"написано {written} из {len(todo)}; " + "; ".join(notes))
    if failed:
        jobs.finish(run, "done_warn")
    return written


def rewrite_page(page_id: int, issues: list[str]) -> dict:
    """Переписать ОДНУ страницу по замечаниям критика, на месте: -> {"page_id", "ok", "error"}.

    Оффер и язык — те, под которые страница написана (Page.offer_id/lang, F26), тип — по её пути в
    scaffold(). Отказ (нет досье, ручная правка, провал писателя) страницу не меняет и возвращается
    словами в `error`, а не исключением: зовущий обходит страницы пачкой. Реестр задач не трогает —
    задачу ведёт тот, кто зовёт."""
    from app.db import SessionLocal
    from app.models.site import Site, Page
    from app.models.offer import Offer
    from app.integrations.llm import LlmClient
    from app.services import research
    from app.services.vertical_data import vertical_block

    def out(error: str | None = None) -> dict:
        return {"page_id": page_id, "ok": error is None, "error": error}

    with SessionLocal() as db:
        page = db.get(Page, page_id)
        if page is None:
            return out(f"страница #{page_id} не найдена")
        if page.blocks_stale:
            return out("страницу правили вручную — переписывание затёрло бы правку")
        if page.status not in REWRITE_STATUSES:
            return out(f"страница в статусе «{page.status}» — переписывать нельзя")
        offer = db.get(Offer, page.offer_id) if page.offer_id else None
        if offer is None or not page.lang:
            return out("у страницы не записан оффер или язык — писать не под что")
        site = db.get(Site, page.site_id)
        spec = next((sp for sp in scaffold(offer.brand, site.niche if site else None, page.lang)
                     if sp["url_path"] == page.url_path), None)
        if spec is None:
            return out(f"путь «{page.url_path}» не из структуры сайта — тип страницы неизвестен")
        rows = research.dossier(db, page.site_id)
        if not rows:
            return out("у сайта нет досье конкурентов — сначала собери его")
        lang, seen_body = page.lang, page.body
        brand, country, promo = offer.brand, offer.country, (offer.promo_code, offer.promo_terms)
        db.expunge_all()                     # строки досье нужны после закрытия сессии

    doc, err = _write_page(LlmClient(timeout=600), rows, spec, brand=brand, lang=lang, country=country,
                           promo=promo, vertical=vertical_block(brand), issues=issues)
    if err:
        return out(err)
    with SessionLocal() as db:
        page = db.get(Page, page_id)
        if page is None:
            return out(f"страница #{page_id} исчезла, пока модель писала")
        if _hand_edited(page, seen_body):
            return out("страницу правили вручную, пока модель писала, — правка сохранена, текст модели отброшен")
        _apply_doc(page, doc, spec["kind"], lang)
        db.commit()
    return out()


# Минимум ВИДИМОГО текста страницы для одобрения (S6-16/S7-11): пустая <article> с одним футером
# не должна проходить гейт редактуры только потому, что кто-то нажал кнопку.
MIN_BODY_TEXT = 40


def _visible_len(body: str | None) -> int:
    return len(html.unescape(nh3.clean(body or "", tags=set())).strip())


def _set_body(page, new_body: str) -> None:
    """Тело из редактора. Если оно разошлось с рендером блоков писателя — помечаем blocks_stale:
    переписывание такую страницу не тронет. Одобрение «как лежит» и страницы без blocks флаг не ставят."""
    if page.blocks and new_body != (page.body or ""):
        page.blocks_stale = True
    page.body = new_body


def save_draft(page_id: int, body: str) -> dict:
    """Сохранить правку БЕЗ одобрения (S6-16): статус — draft. Одобряет только mark_edited.
    Правка уже одобренной (edited) страницы возвращает её в draft: иначе непросмотренный текст
    уехал бы на сайт под старой отметкой «вычитано»."""
    from app.db import SessionLocal
    from app.models.site import Page

    with SessionLocal() as db:
        p = db.get(Page, page_id)
        if p is None:
            raise ValueError(f"page {page_id} not found")
        if p.status not in ("draft", "edited"):
            raise ValueError(f"страница #{page_id} в статусе «{p.status}» — править можно "
                             "только черновик или вычитанную, ещё не опубликованную страницу")
        _set_body(p, _sanitize(body))
        p.status = "draft"
        db.commit()
        return {"page_id": page_id, "status": p.status}


def mark_edited(page_id: int, body: str | None = None) -> dict:
    """HUMAN gate: draft -> edited (the ONLY path to 'edited'). Optionally save edited body.

    Принимает только draft/edited-страницу (published не разжалуется молча, S7-11) и тело с
    видимым текстом не короче MIN_BODY_TEXT (после sanitize). body=None — одобрить как лежит."""
    from app.db import SessionLocal
    from app.models.site import Page

    with SessionLocal() as db:
        p = db.get(Page, page_id)
        if p is None:
            raise ValueError(f"page {page_id} not found")
        if p.status not in ("draft", "edited"):
            raise ValueError(f"страница #{page_id} в статусе «{p.status}»: одобрять можно только "
                             "черновик или вычитанную страницу, опубликованную молча не разжалуем")
        new_body = _sanitize(body) if body is not None else (p.body or "")  # defense-in-depth
        n = _visible_len(new_body)
        if n < MIN_BODY_TEXT:
            raise ValueError(f"в тексте страницы {n} симв. — нужно хотя бы {MIN_BODY_TEXT}: "
                             "пустую страницу одобрить нельзя (сохрани как черновик и допиши)")
        _set_body(p, new_body)
        p.status = "edited"
        db.commit()
        return {"page_id": page_id, "status": p.status}


def cta_link(offer, reserve_url: str | None = None) -> str | None:
    """Ссылка CTA-блока оффера или None, если выводить нечего/нельзя (оффера нет, ссылка с
    опасной схемой или без адресата). Одна функция для рендера и для проверки в publish:
    «оффер привязан, а CTA молча не вышел» (S6-07) обязан быть виден до публикации."""
    if offer is None:
        return None
    link = offer.affiliate_link
    if not offer.active and reserve_url:
        link = reserve_url
    # F28: не рендерим ссылку с опасной схемой (javascript:/data:/...) — последний рубеж.
    return link if is_safe_url(link) else None


def render_html(page, offer=None, lang: str = "en", reserve_url: str | None = None,
                build_id: str | None = None, ctx=None) -> str:
    """Wrap an edited page into a full HTML doc with offer link (sponsored) + disclosure. For M5.

    lang: <html lang=...> for the generation language (publish passes it down). Body is
    re-sanitized here (egress) so any writer that skipped _sanitize can't leak hostile HTML.
    Служебные строки (CTA, промокод, раскрытие, меню) — на этом же языке (services/locales).

    ctx: site_builder.SiteContext — контекст сайта (домен, тема по сиду домена, навигация, файлы
    assets). Без него — одиночный документ с inline-CSS, без шапки/canonical/картинок (тесты,
    предпросмотр). Вёрстка целиком в services/site_builder.

    reserve_url: F3 (аудит 2026-07-15) — если offer.active=False, ссылка подменяется на этот
    общий резервный URL (если задан). offer_id зафиксирован при генерации и остаётся фактом
    истории (F26) — меняется ТОЛЬКО href, бренд/промокод в тексте не трогаются.

    build_id: метка сборки в <meta name="build-id"> — по ней publish сверяет, что по домену
    отдаётся именно записанная версия, а не заглушка панели.
    """
    from app.services import site_builder as sb
    ctx = ctx or sb.SiteContext()
    k = ctx.theme.k
    link = cta_link(offer, reserve_url)
    disc = html.escape(t(lang, "disclosure"))
    offer_block = ""
    if link:
        # условия рядом с кодом и кнопкой: читатель видит, ЧТО даёт промокод (SimpleNamespace в тестах и
        # офферы до 0037 — без поля, отсюда getattr)
        terms = getattr(offer, "promo_terms", None)
        promo = (f" {html.escape(t(lang, 'promo'))}: <b>{html.escape(offer.promo_code)}</b>"
                 f"{' — ' + html.escape(terms.rstrip('.')) if terms else ''}."
                 if offer.promo_code else "")
        cta = html.escape(t(lang, "cta", brand=offer.brand))
        # раскрытие — В блоке оффера, рядом со ссылкой, не только в подвале (S6-07)
        offer_block = (f'<aside class="{k("off")}"><p class="{k("disc")}">{disc}</p>'
                       f'<p><a href="{html.escape(link)}" rel="sponsored nofollow noopener">'
                       f'{cta}</a>.{promo}</p></aside>')
    # шапку <h1> рисует site_builder из page.title, а LLM кладёт заголовок первым и в тело — на живом
    # сайте (tunnelnotes.xyz, 2026-10-10) он стоял дважды. Снимаем ведущий h1 (любой) или h2, если тот
    # начинается с текста заголовка; делаем ДО санитайзера — он h1 не пропускает и оставил бы голый текст.
    raw = page.body or ""
    m = re.match(r"\s*<(h[12])\b[^>]*>(.*?)</\1>\s*", raw, flags=re.S | re.I)
    if m:
        norm = lambda x: re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", x)).strip().lower()  # noqa: E731
        ttl = norm(page.title or "")
        if m.group(1).lower() == "h1" or (ttl and norm(m.group(2)).startswith(ttl)):
            raw = raw[m.end():]
    body = _sanitize(raw)
    # и над текстом: ссылки (CTA или партнёрские в теле) видны раньше, чем читатель дойдёт до низа
    top = f'<p class="{k("disc")}">{disc}</p>' if (link or "<a " in body) else ""
    return sb.render_document(
        ctx, lang, title=page.title or "", description=sb.description_of(body, page.title),
        url_path=getattr(page, "url_path", None) or "/", body=body, top_note=top,
        offer_block=offer_block, footer_note=disc, build_id=build_id)


def build_id_of(doc: str) -> str:
    """Короткий отпечаток отрисованного документа (до вставки метки)."""
    return hashlib.sha256(doc.encode("utf-8")).hexdigest()[:16]


if __name__ == "__main__":  # pure checks (no network/DB): disclosure + sponsored rel always present
    from types import SimpleNamespace as N
    pg = N(title="Обзор", body="<h2>Тест</h2><p>...</p>")
    off = N(brand="NordVPN", affiliate_link="https://ex.com/aff?x=1", promo_code="SAVE10",
            active=True)
    out = render_html(pg, off, lang="ru")
    assert DISCLOSURE in out and 'rel="sponsored nofollow noopener"' in out and "SAVE10" in out
    assert "<article><h2>Тест</h2>" in out
    assert render_html(pg, None).count("<aside") == 0  # no offer -> no offer block
    assert _clean("```html\n<h2>x</h2>\n```") == "<h2>x</h2>"  # fence stripped
    assert _clean("<p>plain</p>") == "<p>plain</p>"
    dirty = _sanitize('<h2>ok</h2><script>alert(1)</script><a href="x" onclick="bad()">l</a>')
    assert "script" not in dirty.lower() and "onclick" not in dirty.lower(), dirty
    assert "<h2>ok</h2>" in dirty and "alert" not in dirty, dirty   # tag+content of <script> gone
    assert is_safe_url("https://ex.com/x") and is_safe_url("http://ex.com")
    assert not is_safe_url("javascript:alert(1)") and not is_safe_url("data:text/html,x")
    evil = N(brand="Evil", affiliate_link="javascript:alert(1)", promo_code=None, active=True)
    assert "javascript:" not in render_html(pg, evil)  # F28: dangerous scheme never rendered
    print("content render_html + _clean + _sanitize ok")
