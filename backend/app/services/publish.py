"""M5 — Publish & Monitor. Deploy ONLY 'edited' pages -> docroot (aaPanel file API).

Hard gate: a page publishes ONLY from status 'edited' (never 'draft'). Then index
monitoring via SearXNG `site:` (GSC excluded from v1 — manual/free check). See PLAN §2.
"""
from datetime import datetime, timezone


def _target_path(doc_root: str, url_path: str) -> str:
    """docroot + url_path -> index.html file path. '/' -> docroot/index.html."""
    sub = url_path.strip("/")
    return f"{doc_root.rstrip('/')}/{sub + '/' if sub else ''}index.html"


def _pick_offer(db, site_id: int):
    from sqlalchemy import select
    from app.models.offer import Offer, SiteOffer
    off = db.execute(
        select(Offer).join(SiteOffer, SiteOffer.offer_id == Offer.id)
        .where(SiteOffer.site_id == site_id, Offer.active.is_(True)).limit(1)
    ).scalar_one_or_none()
    if off is None:  # fall back to any active offer
        off = db.execute(select(Offer).where(Offer.active.is_(True)).limit(1)).scalar_one_or_none()
    return off


def publish_site(site_id: int) -> dict:
    """Deploy every 'edited' page of a site. Refuses if there are none (the edit gate)."""
    from sqlalchemy import select
    from app.db import SessionLocal
    from app.models.site import Site, Page
    from app.models.domain import Domain
    from app.integrations.aapanel import AaPanelClient
    from app.services.content import render_html

    with SessionLocal() as db:
        site = db.get(Site, site_id)
        if site is None:
            raise ValueError(f"site {site_id} not found")
        domain = db.get(Domain, site.domain_id).domain
        pages = db.execute(select(Page).where(
            Page.site_id == site_id, Page.status == "edited")).scalars().all()
        if not pages:
            return {"status": "no_edited_pages",
                    "hint": "гейт: публикуются только страницы в статусе 'edited'"}

        offer = _pick_offer(db, site_id)
        ap = AaPanelClient()
        now = datetime.now(timezone.utc)
        published = []
        for p in pages:
            ap.write_file(_target_path(site.doc_root, p.url_path), render_html(p, offer))
            p.status = "published"
            p.published_at = now
            published.append(p.url_path)

        site.status = "published"
        site.published_at = now
        db.commit()
        return {"status": "published", "domain": domain, "pages": published}


def check_index(site_id: int) -> dict:
    """SearXNG `site:` check for each published page -> pages.index_status + index_history."""
    from sqlalchemy import select
    from app.db import SessionLocal
    from app.models.site import Site, Page
    from app.models.domain import Domain
    from app.models.monitoring import IndexHistory
    from app.integrations.searxng import SearxngClient

    with SessionLocal() as db:
        site = db.get(Site, site_id)
        if site is None:
            raise ValueError(f"site {site_id} not found")
        domain = db.get(Domain, site.domain_id).domain
        pages = db.execute(select(Page).where(
            Page.site_id == site_id, Page.status == "published")).scalars().all()

        sx = SearxngClient()
        now = datetime.now(timezone.utc)
        out = {}
        for p in pages:
            q = f"site:{domain}{p.url_path if p.url_path != '/' else ''}"
            hit = any(domain in (r.get("url") or "") for r in sx.search(q))
            p.index_status = "indexed" if hit else "not_indexed"
            p.index_checked_at = now
            db.add(IndexHistory(page_id=p.id, checked_at=now, index_status=p.index_status))
            out[p.url_path] = p.index_status
        db.commit()
        return {"domain": domain, "pages": out}


if __name__ == "__main__":  # pure path helper self-check
    assert _target_path("/www/wwwroot/ex.ru", "/") == "/www/wwwroot/ex.ru/index.html"
    assert _target_path("/www/wwwroot/ex.ru/", "/vs") == "/www/wwwroot/ex.ru/vs/index.html"
    assert _target_path("/www/wwwroot/ex.ru", "/setup/") == "/www/wwwroot/ex.ru/setup/index.html"
    print("publish _target_path ok")
