"""M4 — Content pipeline. LiteLLM draft -> HUMAN edit gate (draft->edited) -> offers + disclosure.

Generation NEVER publishes: it only creates pages in status='draft'. A human moves
draft -> edited (`mark_edited`); publish (M5) reads ONLY 'edited'. This is the hard
editorial gate from PLAN §2. Content must be topically coherent with the offer.
"""
import html
import re

import nh3

DISCLOSURE = ("Раскрытие: страница содержит партнёрские ссылки. Мы можем получить "
              "комиссию за покупки по ним — без доплаты для вас.")

# Sanitize-on-write allowlist: published pages are public sites, so hostile HTML
# (<script>/<iframe>/on*/style) must never reach the DB. Tags match what M4 emits.
_ALLOWED_TAGS = {"h2", "h3", "h4", "p", "ul", "ol", "li", "a", "strong", "em", "b", "i",
                 "br", "blockquote", "table", "thead", "tbody", "tr", "th", "td",
                 "code", "pre", "figure", "figcaption"}
_ALLOWED_ATTRS = {"a": {"href", "title"}}


def _sanitize(body: str | None) -> str:
    """Strip everything outside the allowlist (script/iframe/event-handlers/style)."""
    return nh3.clean(body or "", tags=_ALLOWED_TAGS, attributes=_ALLOWED_ATTRS)


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
        return urlparse((url or "").strip()).scheme.lower() in _ALLOWED_URL_SCHEMES
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


def generate_site(site_id: int, lang: str = "ru", vertical_data: str | None = None,
                  use_competitor: bool = False) -> int:
    """Generate draft pages for a site via LiteLLM. Returns count created. status stays 'draft'.

    use_competitor: подмешать структуру тем от топ-конкурента (A-Parser, best-effort).
    По умолчанию off — сеть не дёргается в тестах/скриптах; панель включает явно.
    """
    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError
    from app.db import SessionLocal
    from app.models.site import Site, Page
    from app.models.offer import Offer, SiteOffer
    from app.integrations.llm import LlmClient

    with SessionLocal() as db:
        site = db.get(Site, site_id)
        if site is None:
            raise ValueError(f"site {site_id} not found")
        # тематическая связность: бренд берём из оффера, ПРИВЯЗАННОГО к сайту (как в publish),
        # иначе контент напишется про один бренд, а ссылка при публикации уйдёт на другой
        # deterministic order_by(Offer.id): publish (_pick_offer) MUST pick the same offer,
        # else content is written about one brand and the sponsored link points at another.
        offer = db.execute(
            select(Offer).join(SiteOffer, SiteOffer.offer_id == Offer.id)
            .where(SiteOffer.site_id == site_id, Offer.active.is_(True))
            .order_by(Offer.id).limit(1)
        ).scalar_one_or_none()
        if offer is None:  # fall back to any active offer
            offer = db.execute(
                select(Offer).where(Offer.active.is_(True)).order_by(Offer.id).limit(1)
            ).scalar_one_or_none()
        brand = offer.brand if offer else (site.niche or "VPN")

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
        created = 0
        for spec in scaffold(brand, site.niche):
            exists = db.execute(select(Page).where(
                Page.site_id == site_id, Page.url_path == spec["url_path"])).scalar_one_or_none()
            if exists:
                continue  # idempotent — don't regenerate existing pages
            body = _sanitize(_clean(llm.complete(_system_prompt(lang),
                                                 _page_prompt(spec, brand, vertical_data, competitor))))
            if not body.strip():
                continue  # empty LLM output (null/blocked): skip page, don't crash the batch
            # F26: фиксируем на КАЖДОЙ странице, под какой оффер и язык она реально написана —
            # publish.py читает это отсюда, а не пересчитывает "текущий активный оффер сайта"
            # заново (см. миграцию 0018).
            db.add(Page(site_id=site_id, url_path=spec["url_path"], title=spec["title"],
                        status="draft", body=body, lang=lang,
                        offer_id=(offer.id if offer else None)))
            created += 1
        site.status = "content"
        try:
            db.commit()
        except IntegrityError:
            # uq_page_per_path (site_id, url_path) — миграция 0014. Сюда попадаем НЕ от кривых
            # данных, а от гонки двух ПРОЦЕССОВ: кнопка «сгенерировать» в панели и стадия
            # generate автопилотного свипа в воркере вошли одновременно, SELECT «страница уже
            # есть» выше у обоих честно ответил «нет» (чужая незакоммиченная строка под READ
            # COMMITTED невидима), и оба вставили один и тот же путь. Инвариант отбил вторую
            # вставку — и оператор обязан прочитать это словами, а не SQL-трейсом в сводке свипа.
            #
            # Откатываем ВЕСЬ батч, а не досыпаем остаток: страницы этого сайта прямо сейчас
            # пишет другой прогон, и он допишет их целиком. Потерянные токены LLM — цена
            # честная: альтернатива (вторая копия страницы) стоит дороже, см. модель Page.
            db.rollback()
            raise ValueError(
                f"страницы сайта #{site_id} прямо сейчас создаёт другой прогон — "
                f"генерация пропущена, дубли не заводим") from None
        return created


def mark_edited(page_id: int, body: str | None = None) -> dict:
    """HUMAN gate: draft -> edited (the ONLY path to 'edited'). Optionally save edited body."""
    from app.db import SessionLocal
    from app.models.site import Page

    with SessionLocal() as db:
        p = db.get(Page, page_id)
        if p is None:
            raise ValueError(f"page {page_id} not found")
        if body is not None:
            p.body = _sanitize(body)   # human-approved, but still allowlist it (defense-in-depth)
        p.status = "edited"
        db.commit()
        return {"page_id": page_id, "status": p.status}


def render_html(page, offer=None, lang: str = "ru", reserve_url: str | None = None) -> str:
    """Wrap an edited page into a full HTML doc with offer link (sponsored) + disclosure. For M5.

    lang: <html lang=...> for the generation language (publish passes it down). Body is
    re-sanitized here (egress) so any writer that skipped _sanitize can't leak hostile HTML.

    reserve_url: F3 (аудит 2026-07-15) — если offer.active=False, ссылка подменяется на этот
    общий резервный URL (если задан). offer_id зафиксирован при генерации и остаётся фактом
    истории (F26) — меняется ТОЛЬКО href, бренд/промокод в тексте не трогаются.
    """
    offer_block = ""
    if offer is not None:
        link = offer.affiliate_link
        if not offer.active and reserve_url:
            link = reserve_url
        # F28: не рендерим ссылку с опасной схемой (javascript:/data:/...), даже если она как-то
        # обошла проверку на создании оффера/сохранении резерва — это последний рубеж перед
        # публикацией. Без безопасной ссылки лучше показать блок без ссылки (только бренд/промокод
        # потеряны — не XSS), чем опубликовать страницу с исполняемым href.
        if is_safe_url(link):
            promo = f" Промокод: <b>{html.escape(offer.promo_code)}</b>." if offer.promo_code else ""
            offer_block = (f'<p class="offer"><a href="{html.escape(link)}" '
                           f'rel="sponsored nofollow">Перейти к {html.escape(offer.brand)}</a>.{promo}</p>')
    return (
        f"<!doctype html><html lang='{html.escape(lang or 'ru')}'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{html.escape(page.title or '')}</title></head><body>"
        f"<article>{_sanitize(page.body)}</article>{offer_block}"
        f"<footer><small>{html.escape(DISCLOSURE)}</small></footer></body></html>"
    )


if __name__ == "__main__":  # pure checks (no network/DB): disclosure + sponsored rel always present
    from types import SimpleNamespace as N
    pg = N(title="Обзор", body="<h2>Тест</h2><p>...</p>")
    off = N(brand="NordVPN", affiliate_link="https://ex.com/aff?x=1", promo_code="SAVE10",
            active=True)
    out = render_html(pg, off)
    assert DISCLOSURE in out and 'rel="sponsored nofollow"' in out and "SAVE10" in out
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
