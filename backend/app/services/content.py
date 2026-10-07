"""M4 — Content pipeline. LiteLLM draft -> HUMAN edit gate (draft->edited) -> offers + disclosure.

Generation NEVER publishes: it only creates pages in status='draft'. A human moves
draft -> edited (`mark_edited`); publish (M5) reads ONLY 'edited'. This is the hard
editorial gate from PLAN §2. Content must be topically coherent with the offer.
"""
import html
import re

import hashlib

import nh3

from app.services.locales import t

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
                 "code", "pre", "figure", "figcaption"}
_ALLOWED_ATTRS = {"a": {"href", "title"}}


def _sanitize(body: str | None) -> str:
    """Strip everything outside the allowlist (script/iframe/event-handlers/style)."""
    return nh3.clean(body or "", tags=_ALLOWED_TAGS, attributes=_ALLOWED_ATTRS, link_rel=LINK_REL)


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


def scaffold(brand: str, niche: str | None = None) -> list[dict]:
    """Minimal site structure (page specs). Tune per niche/SERP later."""
    return [
        {"url_path": "/", "title": f"{brand}: обзор и честный тест", "kind": "review"},
        {"url_path": "/vs", "title": f"{brand} против конкурентов", "kind": "comparison"},
        {"url_path": "/setup", "title": f"Как настроить {brand}", "kind": "howto"},
    ]


def _system_prompt(lang: str) -> str:
    # F27 (аудит 2026-07-14): раньше здесь стояло "замеры скорости" — а vertical_data.py не
    # содержит ни одного реального измерения скорости ни по одному бренду. Промпт прямо
    # ПРОВОЦИРОВАЛ модель выдумывать конкретные цифры (Mbps/пинги), а гейт редактуры вместо
    # проверки реальных данных превращался в "поймай галлюцинацию". Формулировка ниже просит
    # то, что реально ЕСТЬ в vertical_data (обход гео-блоков, юзкейсы, устройства/протоколы),
    # и явно требует не придумывать числа, которых не давали.
    return (f"Ты опытный редактор VPN-обзоров. Пиши на языке '{lang}', по делу, с реальной "
            "пользой (обход гео-блоков, юзкейсы, поддерживаемые устройства и протоколы), без "
            "воды и маркетингового мусора. Не выдумывай конкретные цифры (скорость, пинг, "
            "проценты), которых нет в переданных данных — если измерений не дали, пиши без них. "
            "Верни HTML-ФРАГМЕНT (только <h2>/<h3>/<p>/<ul>, без <html>/<body>). "
            "Это ЧЕРНОВИК для последующей человеческой редактуры.")


def _page_prompt(spec: dict, brand: str, vertical_data: str | None,
                 competitor: list[str] | None = None) -> str:
    data = f"\n\nРеальные данные вертикали (использовать):\n{vertical_data}" if vertical_data else ""
    comp = ""
    if competitor:
        topics = "\n".join(f"- {h}" for h in competitor)
        comp = ("\n\nТемы, которые покрывает топ-конкурент (для полноты охвата; НЕ копировать "
                f"формулировки дословно, отбирай релевантное теме страницы):\n{topics}")
    return (f"Тема: {spec['title']} (тип: {spec['kind']}). Бренд: {brand}. "
            f"Сделай связный черновик со структурой заголовков.{data}{comp}")


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


def generate_site(site_id: int, lang: str = "ru", vertical_data: str | None = None,
                  use_competitor: bool = False) -> int:
    """Generate draft pages for a site via LiteLLM. Returns count created. status stays 'draft'.

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
    todo = [sp for sp in scaffold(brand, niche) if sp["url_path"] not in existing_paths]
    created = 0
    jobs.report(run, done=0, total=len(todo))
    for i, spec in enumerate(todo):
        if jobs.cancelled(run):
            raise jobs.Cancelled()           # уже записанные страницы остаются (коммит по странице)
        jobs.report(run, done=i, total=len(todo), current=spec["title"])
        body = _sanitize(_clean(llm.complete(_system_prompt(lang),
                                             _page_prompt(spec, brand, vertical_data, competitor))))
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


def render_html(page, offer=None, lang: str = "ru", reserve_url: str | None = None,
                build_id: str | None = None) -> str:
    """Wrap an edited page into a full HTML doc with offer link (sponsored) + disclosure. For M5.

    lang: <html lang=...> for the generation language (publish passes it down). Body is
    re-sanitized here (egress) so any writer that skipped _sanitize can't leak hostile HTML.
    Служебные строки (CTA, промокод, раскрытие) — на этом же языке (services/locales).

    reserve_url: F3 (аудит 2026-07-15) — если offer.active=False, ссылка подменяется на этот
    общий резервный URL (если задан). offer_id зафиксирован при генерации и остаётся фактом
    истории (F26) — меняется ТОЛЬКО href, бренд/промокод в тексте не трогаются.

    build_id: метка сборки в <meta name="build-id"> — по ней publish сверяет, что по домену
    отдаётся именно записанная версия, а не заглушка панели.
    """
    link = cta_link(offer, reserve_url)
    disc = html.escape(t(lang, "disclosure"))
    note = "font-size:.95em;border-left:3px solid #999;padding-left:.75em"
    offer_block = ""
    if link:
        promo = (f" {html.escape(t(lang, 'promo'))}: <b>{html.escape(offer.promo_code)}</b>."
                 if offer.promo_code else "")
        cta = html.escape(t(lang, "cta", brand=offer.brand))
        # раскрытие — В блоке оффера, рядом со ссылкой, не только в подвале (S6-07)
        offer_block = (f'<aside class="offer"><p class="disclosure" style="{note}">{disc}</p>'
                       f'<p><a href="{html.escape(link)}" rel="sponsored nofollow noopener">'
                       f'{cta}</a>.{promo}</p></aside>')
    body = _sanitize(page.body)
    # и над текстом: ссылки (CTA или партнёрские в теле) видны раньше, чем читатель дойдёт до низа
    top = f'<p class="disclosure" style="{note}">{disc}</p>' if (link or "<a " in body) else ""
    meta = f"<meta name='build-id' content='{html.escape(build_id)}'>" if build_id else ""
    return (
        f"<!doctype html><html lang='{html.escape(lang or 'ru')}'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"{meta}<title>{html.escape(page.title or '')}</title></head><body>"
        f"{top}<article>{body}</article>{offer_block}"
        f"<footer><small>{disc}</small></footer></body></html>"
    )


def build_id_of(doc: str) -> str:
    """Короткий отпечаток отрисованного документа (до вставки метки)."""
    return hashlib.sha256(doc.encode("utf-8")).hexdigest()[:16]


if __name__ == "__main__":  # pure checks (no network/DB): disclosure + sponsored rel always present
    from types import SimpleNamespace as N
    pg = N(title="Обзор", body="<h2>Тест</h2><p>...</p>")
    off = N(brand="NordVPN", affiliate_link="https://ex.com/aff?x=1", promo_code="SAVE10",
            active=True)
    out = render_html(pg, off)
    assert DISCLOSURE in out and 'rel="sponsored nofollow noopener"' in out and "SAVE10" in out
    assert "<article><h2>Тест</h2>" in out
    assert render_html(pg, None).count("offer") == 0  # no offer -> no offer block
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
