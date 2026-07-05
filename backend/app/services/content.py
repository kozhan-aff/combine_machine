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


def scaffold(brand: str, niche: str | None = None) -> list[dict]:
    """Minimal site structure (page specs). Tune per niche/SERP later."""
    return [
        {"url_path": "/", "title": f"{brand}: обзор и честный тест", "kind": "review"},
        {"url_path": "/vs", "title": f"{brand} против конкурентов", "kind": "comparison"},
        {"url_path": "/setup", "title": f"Как настроить {brand}", "kind": "howto"},
    ]


def _system_prompt(lang: str) -> str:
    return (f"Ты опытный редактор VPN-обзоров. Пиши на языке '{lang}', по делу, с реальной "
            "пользой (замеры скорости, обход гео-блоков, юзкейсы), без воды и маркетингового "
            "мусора. Верни HTML-ФРАГМЕНT (только <h2>/<h3>/<p>/<ul>, без <html>/<body>). "
            "Это ЧЕРНОВИК для последующей человеческой редактуры.")


def _page_prompt(spec: dict, brand: str, vertical_data: str | None) -> str:
    data = f"\n\nРеальные данные вертикали (использовать):\n{vertical_data}" if vertical_data else ""
    return (f"Тема: {spec['title']} (тип: {spec['kind']}). Бренд: {brand}. "
            f"Сделай связный черновик со структурой заголовков.{data}")


def _clean(body: str) -> str:
    """Strip a leading/trailing ```lang fence the model sometimes wraps output in."""
    b = body.strip()
    b = re.sub(r"^```[a-zA-Z]*\n?", "", b)
    b = re.sub(r"\n?```$", "", b)
    return b.strip()


def generate_site(site_id: int, lang: str = "ru", vertical_data: str | None = None) -> int:
    """Generate draft pages for a site via LiteLLM. Returns count created. status stays 'draft'."""
    from sqlalchemy import select
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
        offer = db.execute(
            select(Offer).join(SiteOffer, SiteOffer.offer_id == Offer.id)
            .where(SiteOffer.site_id == site_id, Offer.active.is_(True)).limit(1)
        ).scalar_one_or_none()
        if offer is None:  # fall back to any active offer
            offer = db.execute(select(Offer).where(Offer.active.is_(True)).limit(1)).scalar_one_or_none()
        brand = offer.brand if offer else (site.niche or "VPN")

        llm = LlmClient()
        created = 0
        for spec in scaffold(brand, site.niche):
            exists = db.execute(select(Page).where(
                Page.site_id == site_id, Page.url_path == spec["url_path"])).scalar_one_or_none()
            if exists:
                continue  # idempotent — don't regenerate existing pages
            body = _sanitize(_clean(llm.complete(_system_prompt(lang),
                                                 _page_prompt(spec, brand, vertical_data))))
            db.add(Page(site_id=site_id, url_path=spec["url_path"], title=spec["title"],
                        status="draft", body=body))
            created += 1
        site.status = "content"
        db.commit()
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


def render_html(page, offer=None) -> str:
    """Wrap an edited page into a full HTML doc with offer link (sponsored) + disclosure. For M5."""
    offer_block = ""
    if offer is not None:
        promo = f" Промокод: <b>{html.escape(offer.promo_code)}</b>." if offer.promo_code else ""
        offer_block = (f'<p class="offer"><a href="{html.escape(offer.affiliate_link)}" '
                       f'rel="sponsored nofollow">Перейти к {html.escape(offer.brand)}</a>.{promo}</p>')
    return (
        "<!doctype html><html lang='ru'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{html.escape(page.title or '')}</title></head><body>"
        f"<article>{page.body or ''}</article>{offer_block}"
        f"<footer><small>{html.escape(DISCLOSURE)}</small></footer></body></html>"
    )


if __name__ == "__main__":  # pure checks (no network/DB): disclosure + sponsored rel always present
    from types import SimpleNamespace as N
    pg = N(title="Обзор", body="<h2>Тест</h2><p>...</p>")
    off = N(brand="NordVPN", affiliate_link="https://ex.com/aff?x=1", promo_code="SAVE10")
    out = render_html(pg, off)
    assert DISCLOSURE in out and 'rel="sponsored nofollow"' in out and "SAVE10" in out
    assert "<article><h2>Тест</h2>" in out
    assert render_html(pg, None).count("offer") == 0  # no offer -> no offer block
    assert _clean("```html\n<h2>x</h2>\n```") == "<h2>x</h2>"  # fence stripped
    assert _clean("<p>plain</p>") == "<p>plain</p>"
    dirty = _sanitize('<h2>ok</h2><script>alert(1)</script><a href="x" onclick="bad()">l</a>')
    assert "script" not in dirty.lower() and "onclick" not in dirty.lower(), dirty
    assert "<h2>ok</h2>" in dirty and "alert" not in dirty, dirty   # tag+content of <script> gone
    print("content render_html + _clean + _sanitize ok")
