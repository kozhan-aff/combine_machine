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
  "verdict": {"score": null (число от 1 до 10 — ТОЛЬКО если в правилах письма задана методика оценки),
              "summary": "вывод в одном-двух предложениях",
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


class WriterDown(RuntimeError):
    """Модель недоступна: таймаут, обрыв связи, 5xx/408/429 шлюза, LlmEmptyContent. Отдельно от «ответ
    мимо схемы»: тот лечится повтором, а этот — нет (POST к модели ждёт до 10 минут), и следующие
    страницы пачки упрутся в тот же шлюз. Текст исключения — причина словами для оператора.
    Отказ 4xx сюда НЕ относится: это ответ шлюза про конкретный запрос — см. write_doc."""


def write_doc(llm, *, system: str, prompt: str,
              issues: list[str] | None = None) -> tuple[page_doc.PageDoc | None, str | None]:
    """Страница от модели: `(doc, None)` или `(None, причина словами)`. Не больше двух вызовов.

    Ответ, не прошедший схему (ограду и текст вокруг JSON снимает page_doc.parse), и пустой ответ
    повторяются ОДИН раз; после ошибки схемы — с её текстом. Исключение клиента не повторяется:
    страница провалена сразу. Отказ 4xx (кроме 408/429) — провал ЭТОЙ страницы: `(None, причина)`,
    слова шлюза — в причине (промпт длинен, 422…); остальное — WriterDown, модель недоступна.
    `issues` — замечания критика при переписывании. И они, и просьба о повторе стоят ВЫШЕ брифа:
    бриф кончается данными конкурентов и закрывающим напоминанием о них — после него нашего текста нет."""
    import httpx
    from app.config import settings
    from app.integrations.llm import _err_text
    model = settings.LLM_WRITER_MODEL or settings.LLM_MODEL
    head = ("## Замечания редактора, которые нужно устранить\n"
            + "\n".join(f"- {x}" for x in issues) + "\n\n") if issues else ""
    retry, reason = "", None
    for _ in range(2):
        try:
            raw = llm.complete(system, head + retry + prompt, model=model)
        except Exception as e:  # noqa: BLE001 — любая осечка клиента = причина словами, не трейс в сводке
            why = f"{type(e).__name__}: {e}"
            if isinstance(e, httpx.HTTPStatusError):
                # текст исключения httpx — URL и ссылка на MDN; что не так, шлюз пишет в теле ответа
                code = e.response.status_code
                why = f"HTTP {code}{_err_text(e.response)}"
                if 400 <= code < 500 and code not in (408, 429):
                    return None, f"модель отклонила запрос: {why}"
            raise WriterDown(f"писатель не ответил: {why}"[:300]) from e
        if not (raw or "").strip():
            reason = "писатель вернул пустой ответ"
            continue
        try:
            return page_doc.parse(raw), None
        except ValueError as e:
            reason = f"ответ писателя не прошёл схему: {e}"
            retry = (f"## Повтор\nПредыдущий ответ не прошёл проверку схемы: {e}. Верни ТОЛЬКО исправленный "
                     "JSON-объект — без текста до и после него.\n\n")
    return None, reason


NO_RULES = "папка правил письма не видна этому процессу — переписывать нечем руководствоваться"


def _prompts(rows: list, spec: dict, *, brand: str, lang: str, country: str | None, promo: tuple,
             vertical: str | None) -> tuple[str, str, bool, int]:
    """Промпты одной страницы нового пути: (системный с правилами оператора, бриф из досье, «правила
    письма не влезли в лимит и обрезаны», сколько файлов правил не учтено — у них нет выжимки).

    Папка правил этому процессу не видна (не смонтирована) или правила не прочитались — ValueError
    (`NO_RULES`): писать страницу без единого правила оператора молча нельзя. Пустая папка — не отказ:
    правил у оператора может и не быть."""
    from app.services import brief, guides
    kind = spec["kind"]
    try:
        rules = guides.load_guides(lang, kind, role="writer")
    except Exception as e:  # noqa: BLE001 — причина словами в карточке задачи, а не трейс
        raise ValueError(NO_RULES) from e
    if rules.get("missing"):
        raise ValueError(NO_RULES)
    prompt = brief.brief_text(brief.build_brief(rows, kind), brand=brand, kind=kind, title=spec["title"],
                              lang_name=LANG_NAMES[norm_lang(lang)], country=country, promo=promo,
                              vertical=vertical)
    return writer_system(lang, country, rules["text"]), prompt, bool(rules["truncated"]), len(rules["pending"])


def _apply_doc(page, doc: page_doc.PageDoc, kind: str, lang: str) -> None:
    """Записать страницу писателя в строку Page (новую или переписываемую). Статус — всегда draft:
    переписанный текст никто не читал, прежние «вычитано»/«опубликовано» и оценка критика к нему не
    относятся. url_path, lang, published_at и поля индексации не трогаем — это история строки; оффер, под
    который текст написан, в переписанную строку ставит зовущий — в той же записи."""
    from sqlalchemy.orm.attributes import flag_modified
    page.title = doc.meta.title
    page.body = _sanitize(page_doc.render_blocks(doc, kind, lang))
    page.blocks = doc.model_dump()
    page.blocks_stale = False
    page.status = "draft"
    # Статус пишется в строку ВСЕГДА, а не «если изменился»: загруженное значение могло быть draft, а в
    # строке к мигу записи — уже edited (страницу одобрили, пока мы шли сюда). ORM перемены не увидел бы,
    # не включил бы статус в UPDATE — и новый, никем не читанный текст остался бы «вычитанным».
    flag_modified(page, "status")
    page.critic_score = page.critic_notes = page.critic_checked_at = None


def _same_text(page, doc: page_doc.PageDoc, kind: str, lang: str) -> bool:
    """Писатель вернул то, что уже лежит в строке (заголовок и тело знак в знак). Это не переписывание:
    строку не трогаем вовсе — иначе запись «нового» текста стёрла бы статус и заметки критика (в том
    числе его окончательный отказ), ничего не изменив на странице."""
    return (page.title or "") == doc.meta.title and \
        (page.body or "") == _sanitize(page_doc.render_blocks(doc, kind, lang))


def _hand_edited(page, seen_body: str | None, overwrite_manual: bool = False) -> bool:
    """Страницу правили руками: флаг blocks_stale либо тело не то, что мы видели до вызова модели
    (модель пишет минуты, редактор панели всё это время открыт). `overwrite_manual` снимает только
    флаг: оператор разрешил затереть правки, о которых знал, — не ту, что сохранена посреди прогона."""
    return (bool(page.blocks_stale) and not overwrite_manual) or (page.body or "") != (seen_body or "")


# Секунд на одну страницу у писателя. Живой замер 2026-10-11: длинный ответ через шлюз (headless Claude)
# идёт ~50 знаков/с при большом задании — страница в 15 тыс. знаков JSON не укладывается в прежние 600.
WRITER_TIMEOUT = 1200

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
                  use_competitor: bool = False, rewrite: bool = False, overwrite_manual: bool = False) -> int:
    """Generate draft pages for a site via LiteLLM. Returns count created (+ rewritten). status stays 'draft'.

    Два пути. У сайта есть досье конкурентов (site_research) — пишет ПИСАТЕЛЬ: бриф из досье + правила
    оператора -> JSON по схеме PageDoc -> Page.blocks и его рендер в Page.body. Досье нет — старый
    путь: один промпт -> HTML-фрагмент (blocks пуст).

    rewrite: только путь с досье. Кроме недостающих страниц переписывает на месте существующие
    (draft|edited|published -> draft, та же строка), кроме правленых руками (blocks_stale). Пишет под
    оффер САЙТА и записывает его в переписанную строку; без rewrite недостающие страницы дописываются
    под оффер уже написанных. Оффер сайта выключен или удалён — ошибка. Текст, вернувшийся знак в знак
    прежним, строку не меняет. Папка правил письма процессу не видна — ошибка до первого вызова модели.
    overwrite_manual: переписать и правленые руками — только по явному решению оператора (галочка
    в панели); автопилот его не передаёт.

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
        return _generate_site(site_id, lang, vertical_data, use_competitor, run, rewrite, overwrite_manual)


def _generate_site(site_id, lang, vertical_data, use_competitor, run, rewrite=False, overwrite_manual=False) -> int:
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

        # Переписывание идёт под оффер САЙТА: сайт могли перепривязать (досье собрано уже под новый
        # бренд), и текст «про оффер первой страницы» вышел бы про прежний бренд — с его ссылкой при
        # публикации. Оффер уже написанных страниц берётся, только если у сайта своего нет.
        offer = site_offer(db, site) if rewrite else None
        if rewrite and offer is None and site.offer_id is not None:
            # оффер у сайта есть, но выключен или удалён: молча переписать «под оффер первой страницы» —
            # значит снова написать про прежний бренд
            raise ValueError("оффер сайта выключен или удалён — включи его или привяжи другой")
        if existing_page is not None:
            # Дозаполнение (S4/S5, аудит 2026-07-18): сайт уже частично сгенерирован —
            # наследуем lang/offer от уже существующих страниц, а не резолвим заново.
            # Иначе повторный вызов (второй клик / автопилотная стадия generate после
            # LLM-осечки на части спек) может дописать недостающие страницы на другом
            # языке или под другим брендом, чем уже созданные, — нарушая "один домен =
            # одно гео/язык" и рассинхронизируя publish.py с телом контента.
            lang = existing_page.lang or lang
            if offer is None:
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
        return _write_site(site_id, run, rows, existing_pages, rewrite, overwrite_manual, brand=brand,
                           niche=niche, lang=lang, country=country, promo=promo, vertical=vertical_data,
                           offer_id=offer_id)
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


MESSAGE_MAX = 400      # длина JobRun.message: jobs.report режет по ней вслепую, с хвоста


def _batch_message(written: int, total: int, *, down: bool, not_started: int, hand_edited: int,
                   truncated: bool, failed: list, no_digest: int = 0) -> str | None:
    """Итог прогона писателя одной строкой — или None, если сказать нечего. Укладывается в MESSAGE_MAX
    сам: одинаковые причины схлопнуты в одну со списком путей, а под нож идут только тексты причин
    (хвост заменяет «…») — счётчики и пути остаются целыми. Место делится от коротких причин к
    длинным: чего не взяла короткая, достаётся длинной."""
    notes = [f"написано {written} из {total}"]
    if down:
        notes.append(f"модель недоступна — прогон остановлен, не начато страниц: {not_started}")
    if hand_edited:
        notes.append(f"не тронуты, правлены вручную: {hand_edited}")
    if truncated:
        notes.append("правила письма обрезаны по лимиту")
    if no_digest:
        from app.services.guides import no_digest_ru
        notes.append("правила письма: " + no_digest_ru(no_digest, "не учтён", "не учтены"))
    by_reason: dict[str, list[str]] = {}
    for path, err in failed:
        by_reason.setdefault(err, []).append(path)
    if len(notes) == 1 and not by_reason:
        return None
    if not by_reason:
        return "; ".join(notes)
    heads = {err: ", ".join(paths) + " — " for err, paths in by_reason.items()}
    lead = "; ".join(notes) + "; не написаны: "
    room = MESSAGE_MAX - len(lead) - sum(map(len, heads.values())) - 2 * (len(heads) - 1)
    shown = {}
    for left, err in zip(range(len(heads), 0, -1), sorted(heads, key=len)):
        take = min(len(err), max(room, 0) // left)
        shown[err] = err if take == len(err) else err[:max(take - 1, 0)] + "…"
        room -= take
    return lead + "; ".join(heads[err] + shown[err] for err in heads)


def _write_site(site_id: int, run, rows: list, existing_pages: list, rewrite: bool, overwrite_manual: bool,
                *, brand: str, niche: str | None, lang: str, country: str | None, promo: tuple,
                vertical: str | None, offer_id: int) -> int:
    """Путь с досье: страницы пишет писатель (PageDoc). Возвращает создано + переписано.

    Проваленная страница (два ответа мимо схемы, пустой ответ) НЕ создаётся и НЕ меняется: причина
    уходит в сообщение задачи, прогон закрывается «с замечаниями», остальные страницы пишутся.
    Сбой клиента модели (WriterDown) останавливает пачку: шлюз лежит, каждая следующая страница
    сожгла бы тот же таймаут. Уже записанные страницы остаются."""
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
        elif rewrite and old.blocks_stale and not overwrite_manual:
            hand_edited += 1                 # ручная правка дороже свежего текста модели — не затираем
        elif rewrite and old.status in REWRITE_STATUSES:
            todo.append((spec, old))

    llm = LlmClient(timeout=WRITER_TIMEOUT)  # страница на 2000 слов через шлюз идёт минуты
    written, failed, down, truncated, no_digest, i = 0, [], False, False, 0, 0
    jobs.report(run, done=0, total=len(todo))
    # try/finally: накопленные причины и счётчики обязаны дожить до карточки задачи и при отмене, и при
    # гонке вставки (ValueError ниже) — иначе оператор видит «отменено»/ошибку без того, что уже выяснено
    try:
        for i, (spec, old) in enumerate(todo):
            if jobs.cancelled(run):
                raise jobs.Cancelled()       # уже записанные страницы остаются (коммит по странице)
            jobs.report(run, done=i, total=len(todo), current=spec["title"])
            system, prompt, cut_rules, waiting = _prompts(rows, spec, brand=brand, lang=lang, country=country,
                                                          promo=promo, vertical=vertical)
            truncated, no_digest = truncated or cut_rules, max(no_digest, waiting)
            try:
                doc, err = write_doc(llm, system=system, prompt=prompt)
            except WriterDown as e:
                failed.append((spec["url_path"], str(e)))
                down = True
                break
            if err:
                failed.append((spec["url_path"], err))
                continue
            # коммит КАЖДОЙ страницы сразу, как в старом пути: осечка на 3-й не выбрасывает токены 1-й и 2-й
            with SessionLocal() as db:
                if old is None:
                    page = Page(site_id=site_id, url_path=spec["url_path"], lang=lang, offer_id=offer_id)
                    db.add(page)
                else:
                    # строка — под блокировкой до коммита: чтение, проверки и запись неразрывны
                    page = db.get(Page, old.id, with_for_update=True)
                    if page is None:
                        failed.append((spec["url_path"], "страница исчезла, пока модель писала"))
                        continue
                    if _hand_edited(page, old.body, overwrite_manual):
                        hand_edited += 1
                        continue
                    if _same_text(page, doc, spec["kind"], lang):
                        failed.append((spec["url_path"], "писатель вернул прежний текст"))
                        continue
                    page.offer_id = offer_id     # под какой оффер текст написан — в той же записи, что и текст
                    if not page.lang:
                        page.lang = lang         # и на каком языке: строка без языка иначе осталась бы без него
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
        if not down:
            jobs.report(run, done=len(todo), total=len(todo), current="")
    finally:
        message = _batch_message(written, len(todo), down=down, not_started=len(todo) - i - 1,
                                 hand_edited=hand_edited, truncated=truncated, failed=failed,
                                 no_digest=no_digest)
        if message:
            jobs.report(run, message=message)
    if failed:
        jobs.finish(run, "done_warn")
    return written


def rewrite_page(page_id: int, issues: list[str], overwrite_manual: bool = False,
                 only_status: str | None = None) -> dict:
    """Переписать ОДНУ страницу по замечаниям критика, на месте: -> {"page_id", "ok", "error"}; если
    отказ вызван недоступной моделью (WriterDown) — ещё и "down": True.
    Правленую руками (blocks_stale) не трогает, пока оператор явно не разрешил (`overwrite_manual`).

    `only_status` — переписать, только если страница в этом статусе и в начале, и в момент записи.
    Писатель работает минуты: без этого критик затёр бы новым текстом и разжаловал в draft страницу,
    которую оператор за это время одобрил (или которая ушла на сайт). Запись всегда идёт под блокировкой
    строки, и статус draft в ней пишется всегда (`_apply_doc`).

    Оффер и язык — те, под которые страница написана (Page.offer_id/lang, F26), тип — по её пути в
    scaffold(). Отказ (нет досье, ручная правка, провал писателя, прежний текст вместо нового) страницу
    не меняет и возвращается
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
        if page.blocks_stale and not overwrite_manual:
            return out("страницу правили вручную — переписывание затёрло бы правку")
        if page.status not in REWRITE_STATUSES:
            return out(f"страница в статусе «{page.status}» — переписывать нельзя")
        if only_status is not None and page.status != only_status:
            return out(f"страница уже в статусе «{page.status}» — переписывать её не нам")
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
        lang, seen_body, offer_id = page.lang, page.body, offer.id
        brand, country, promo = offer.brand, offer.country, (offer.promo_code, offer.promo_terms)
        db.expunge_all()                     # строки досье нужны после закрытия сессии

    try:
        system, prompt, *_ = _prompts(rows, spec, brand=brand, lang=lang, country=country, promo=promo,
                                      vertical=vertical_block(brand))
    except ValueError as e:                  # папка правил не видна — отказ словами, круг не засчитан
        return out(str(e))
    try:
        doc, err = write_doc(LlmClient(timeout=WRITER_TIMEOUT), system=system, prompt=prompt, issues=issues)
    except WriterDown as e:
        # модель недоступна: зовущему (круги критика) незачем идти к следующей странице — там тот же таймаут
        return {**out(str(e)), "down": True}
    if err:
        return out(err)
    with SessionLocal() as db:
        # строка — под блокировкой до коммита: проверки (статус, ручная правка) и запись неразрывны
        page = db.get(Page, page_id, with_for_update=True)
        if page is None:
            return out(f"страница #{page_id} исчезла, пока модель писала")
        if only_status is not None and page.status != only_status:
            return out("страница изменилась, пока писатель работал")
        if _hand_edited(page, seen_body, overwrite_manual):
            return out("страницу правили вручную, пока модель писала, — правка сохранена, текст модели отброшен")
        if _same_text(page, doc, spec["kind"], lang):
            return out("писатель вернул прежний текст")
        _apply_doc(page, doc, spec["kind"], lang)
        page.offer_id = offer_id             # под какой оффер текст написан — в той же записи, что и текст
        db.commit()
    return out()


# Минимум ВИДИМОГО текста страницы для одобрения (S6-16/S7-11): пустая <article> с одним футером
# не должна проходить гейт редактуры только потому, что кто-то нажал кнопку.
MIN_BODY_TEXT = 40


def _visible_len(body: str | None) -> int:
    return len(html.unescape(nh3.clean(body or "", tags=set())).strip())


def _set_body(page, new_body: str) -> None:
    """Тело из редактора. Изменилось — помечаем blocks_stale («тело правили руками»): переписывание
    такую страницу не тронет. Флаг ставится и странице без blocks (старый путь): правка оператора
    стоит того же, чем бы ни был написан исходный текст. Одобрение «как лежит» флаг не ставит."""
    if new_body != (page.body or ""):
        page.blocks_stale = True
    page.body = new_body


_UNSET = object()
# кто изменил страницу, не говорим: тот же отказ получит и оператор, сохранивший её в соседней вкладке
STALE_FORM = ("страница изменилась, пока ты её редактировал — открой её заново; свой текст верни кнопкой «назад» "
              "в браузере и скопируй")


def _is(column, value):
    """Сравнение колонки со значением, верное и для NULL."""
    return column.is_(None) if value is None else column == value


def _form_is_stale(page, seen_fp: str | None) -> bool:
    """Редактор открыли с одним текстом, а в строке уже другой. `seen_fp` — отпечаток заголовка и тела,
    с которыми страница была показана (скрытое поле формы); None — зовущий его не прислал (JSON-API,
    старый клиент), сверять не с чем."""
    from app.services.content_critic import fingerprint
    return seen_fp is not None and seen_fp != fingerprint(page.title, page.body)


def save_draft(page_id: int, body: str, seen_fp: str | None = None) -> dict:
    """Сохранить правку БЕЗ одобрения (S6-16): статус — draft. Одобряет только mark_edited.
    Правка уже одобренной (edited) страницы возвращает её в draft: иначе непросмотренный текст
    уехал бы на сайт под старой отметкой «вычитано». `seen_fp` — см. `_form_is_stale`: форма, открытая
    до того, как страницу переписал писатель, его текст не затирает."""
    from app.db import SessionLocal
    from app.models.site import Page

    from sqlalchemy.orm.attributes import flag_modified

    with SessionLocal() as db:
        p = db.get(Page, page_id, with_for_update=True)      # чтение и запись неразрывны
        if p is None:
            raise ValueError(f"page {page_id} not found")
        if p.status not in ("draft", "edited"):
            raise ValueError(f"страница #{page_id} в статусе «{p.status}» — править можно "
                             "только черновик или вычитанную, ещё не опубликованную страницу")
        if _form_is_stale(p, seen_fp):
            raise ValueError(STALE_FORM)
        _set_body(p, _sanitize(body))
        p.status = "draft"
        # и текст формы, и статус пишутся в строку всегда, а не «если изменились» (см. _apply_doc): иначе
        # правка, сохранённая поверх только что одобренной страницы, осталась бы «вычитанной»
        flag_modified(p, "body")
        flag_modified(p, "status")
        db.commit()
    return {"page_id": page_id, "status": "draft"}


def mark_edited(page_id: int, body: str | None = None, *, expected_body: str | None = None,
                expected_title=_UNSET, seen_fp: str | None = None) -> dict:
    """HUMAN gate: draft -> edited (the ONLY path to 'edited'). Optionally save edited body.

    Принимает только draft/edited-страницу (published не разжалуется молча, S7-11) и тело с
    видимым текстом не короче MIN_BODY_TEXT (после sanitize). body=None — одобрить как лежит.

    Одобряется и записывается ровно то, что человек видел, — одним UPDATE с условием на статус и на
    заголовок (он тоже уходит на сайт — в <h1> и <title>). Текст формы пишется в строку явно; «как лежит»
    одобряет только тело, прочитанное в этой же транзакции. `seen_fp` — отпечаток заголовка и тела, с
    которыми редактор был открыт (см. `_form_is_stale`): если страницу с тех пор переписал писатель —
    отказ, а не одобрение старого текста под новым заголовком; при нём UPDATE держит и условие на тело.
    Без `seen_fp` (JSON-API, старый клиент) текст формы пишется, что бы ни лежало в строке.

    `expected_body` (+ `expected_title`) — одобрение критиком: «одобрить, только если страница — черновик
    и её тело (и заголовок) ровно эти». Один условный UPDATE, без чтения перед записью: между вычиткой и
    одобрением страницу мог изменить редактор панели или другой прогон писателя, и окна на это здесь нет.
    Ни одной строки не обновлено -> ValueError, страница не тронута. Тело при этом не пишется."""
    from sqlalchemy import case, update
    from app.db import SessionLocal
    from app.models.site import Page

    if expected_body is not None or expected_title is not _UNSET:
        if body is not None or seen_fp is not None:
            raise ValueError("mark_edited: body/seen_fp и expected_body вместе не передаются")
        if not isinstance(expected_body, str):
            # критик без прочитанного тела не должен провалиться в ветку человека «одобрить как лежит»
            raise ValueError("mark_edited: одобрение критиком без прочитанного тела")
        n = _visible_len(expected_body)
        if n < MIN_BODY_TEXT:
            raise ValueError(f"в тексте страницы {n} симв. — нужно хотя бы {MIN_BODY_TEXT}: "
                             "пустую страницу одобрить нельзя")
        where = [Page.id == page_id, Page.status == "draft", Page.body == expected_body]
        if expected_title is not _UNSET:
            where.append(_is(Page.title, expected_title))
        with SessionLocal() as db:
            done = db.execute(update(Page).where(*where).values(status="edited")).rowcount
            db.commit()
        if done != 1:
            raise ValueError("страница изменилась во время вычитки — не одобрена")
        return {"page_id": page_id, "status": "edited"}

    with SessionLocal() as db:
        p = db.get(Page, page_id)
        if p is None:
            raise ValueError(f"page {page_id} not found")
        if p.status not in ("draft", "edited"):
            raise ValueError(f"страница #{page_id} в статусе «{p.status}»: одобрять можно только "
                             "черновик или вычитанную страницу, опубликованную молча не разжалуем")
        if _form_is_stale(p, seen_fp):
            raise ValueError(STALE_FORM)
        new_body = _sanitize(body) if body is not None else (p.body or "")  # defense-in-depth
        n = _visible_len(new_body)
        if n < MIN_BODY_TEXT:
            raise ValueError(f"в тексте страницы {n} симв. — нужно хотя бы {MIN_BODY_TEXT}: "
                             "пустую страницу одобрить нельзя (сохрани как черновик и допиши)")
        # заголовок — тот, что мы сейчас прочли: одобрить заголовок, которого никто не видел, нельзя
        where = [Page.id == page_id, Page.status.in_(("draft", "edited")), _is(Page.title, p.title)]
        if body is None or seen_fp is not None:
            # «как лежит» — только то тело, что мы сейчас прочли; с отпечатком формы — тоже: человек видел
            # именно его, и правка пишется поверх него, а не поверх чужой, прилетевшей в этот миг
            where.append(_is(Page.body, p.body))
        # blocks_stale — как в _set_body («тело правили руками»), но по телу, которое лежит в строке в миг
        # записи: совпало с новым — флаг не трогаем, иначе ставим
        done = db.execute(
            update(Page).where(*where)
            .values(status="edited", body=new_body,
                    blocks_stale=case((Page.body == new_body, Page.blocks_stale), else_=True))
            .execution_options(synchronize_session=False)).rowcount
        db.commit()
    if done != 1:
        raise ValueError(f"страница #{page_id} изменилась, пока шло одобрение: одобрять можно только черновик "
                         "или вычитанную страницу — открой её заново и проверь текст")
    return {"page_id": page_id, "status": "edited"}


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
