"""M4 — Content pipeline. LiteLLM draft -> HUMAN edit gate (draft->edited) -> offers + disclosure.

Generation NEVER publishes: it only creates pages in status='draft'. A human moves
draft -> edited (`mark_edited`); publish (M5) reads ONLY 'edited'. This is the hard
editorial gate from PLAN §2. Content must be topically coherent with the offer.
"""
import html
import re

import hashlib

import nh3

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
                  use_competitor: bool = False) -> int:
    """Generate draft pages for a site via LiteLLM. Returns count created. status stays 'draft'.

    lang: язык сайта. None -> берётся сам (S6-02/S7-06): язык уже написанных страниц сайта,
    иначе рынок домена (Domain.market_lang), иначе язык оффера, иначе en — НЕ 'ru' по умолчанию.
    Язык без словаря шаблона (pl, ja…) -> ValueError, а не молча английский.

    use_competitor: подмешать структуру тем от топ-конкурента (A-Parser, best-effort).
    По умолчанию off — сеть не дёргается в тестах/скриптах; панель включает явно.

    Идёт задачей реестра `generate` (S6-11/S7-14/F8-07): прогресс по страницам, «стоп», один
    прогон за раз (LLM — единственный ресурс). Кто бы ни позвал — панель через jobs.spawn,
    автопилот, API — карточка на Пульте появляется сама.
    """
    from app.services import jobs
    with jobs.track("generate") as run:
        return _generate_site(site_id, lang, vertical_data, use_competitor, run)


def _generate_site(site_id, lang, vertical_data, use_competitor, run) -> int:
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

    # information gain (PLAN §2): подмешиваем реальные факты вертикали, если бренд знаком.
    # Явно переданный vertical_data приоритетнее (напр. свежий фид). Неизвестный бренд -> None.
    if vertical_data is None:
        from app.services.vertical_data import vertical_block
        vertical_data = vertical_block(brand)

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


# Минимум ВИДИМОГО текста страницы для одобрения (S6-16/S7-11): пустая <article> с одним футером
# не должна проходить гейт редактуры только потому, что кто-то нажал кнопку.
MIN_BODY_TEXT = 40


def _visible_len(body: str | None) -> int:
    return len(html.unescape(nh3.clean(body or "", tags=set())).strip())


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
        p.body = _sanitize(body)
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
        p.body = new_body
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
