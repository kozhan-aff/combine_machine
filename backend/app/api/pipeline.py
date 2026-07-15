"""Pipeline driver endpoints — operate the MVP loop by hand.

Manual buy -> M3 provision -> M4 generate -> HUMAN edit -> M5 publish -> index check.
Heavy calls run synchronously (fine for one-domain MVP; move to the worker for scale).
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.db import get_session
from app.models.domain import Domain
from app.models.site import Site, Page
from app.models.offer import Offer, SiteOffer
from app.services import provisioning, content, publish
from app.services.content import is_safe_url

router = APIRouter(tags=["pipeline"])


class OfferIn(BaseModel):
    brand: str
    affiliate_link: str
    promo_code: str | None = None
    network: str | None = None
    country: str | None = None
    language: str | None = None
    payout_type: str | None = None
    payout_value: str | None = None


class EditIn(BaseModel):
    body: str | None = None


class SiteOfferIn(BaseModel):
    offer_id: int
    country: str | None = None
    placement: str | None = None


# --- offers -----------------------------------------------------------------
@router.post("/offers")
def create_offer(o: OfferIn, db: Session = Depends(get_session)):
    # F28: тот же allowlist http/https, что и в panel.py::offer_create_action — этот JSON-роут
    # ровно та "форма обойдена прямым API-вызовом", от которой defense-in-depth в render_html
    # предостерегает; закрываем и вход, а не только выход.
    if not is_safe_url(o.affiliate_link):
        raise HTTPException(400, "affiliate_link: разрешены только http/https")
    offer = Offer(**o.model_dump())
    db.add(offer)
    db.commit()
    db.refresh(offer)
    return {"id": offer.id, "brand": offer.brand}


@router.get("/offers")
def list_offers(db: Session = Depends(get_session)):
    return [{"id": o.id, "brand": o.brand, "country": o.country, "active": o.active}
            for o in db.execute(select(Offer)).scalars().all()]


# --- M2 (manual buy) --------------------------------------------------------
@router.post("/domains/{domain_id}/purchase")
def mark_purchased(domain_id: int, db: Session = Depends(get_session)):
    """Человек пометил домен купленным мимо очереди — MVP покупает руками, и этот клик И ЕСТЬ
    денежный гейт. Оркестратор (services/orchestrator) этот роут НЕ зовёт.

    «Мимо очереди» — не значит «мимо воронки»: роут ставил 'purchased' из ЛЮБОГО статуса, не
    спросив ни исходного, ни reject_reason (аудит F13). Так покупался и РКН-домен, и вовсе не
    оценённое сырьё. Теперь переход судит политика (services/transitions).
    """
    from app.services import transitions
    d = db.get(Domain, domain_id)
    if not d:
        raise HTTPException(404, "domain not found")
    try:
        transitions.set_status(d, "purchased")
    except transitions.TransitionDenied as e:
        db.rollback()
        raise HTTPException(409, str(e))
    db.commit()
    return {"id": d.id, "domain": d.domain, "status": d.status}


# --- M3 provisioning --------------------------------------------------------
@router.post("/domains/{domain_id}/site")
def make_site(domain_id: int):
    return {"site_id": provisioning.create_site_for(domain_id)}


@router.post("/sites/{site_id}/provision")
def do_provision(site_id: int):
    return provisioning.provision(site_id)


# --- M4 content -------------------------------------------------------------
@router.post("/sites/{site_id}/generate")
def do_generate(site_id: int, lang: str = "ru"):
    return {"created": content.generate_site(site_id, lang=lang)}


@router.post("/pages/{page_id}/edit")
def do_edit(page_id: int, body: EditIn):
    return content.mark_edited(page_id, body.body)   # HARD GATE: draft -> edited (human)


@router.post("/sites/{site_id}/offer")
def attach_offer(site_id: int, so: SiteOfferIn, db: Session = Depends(get_session)):
    exists = db.execute(select(SiteOffer).where(          # зеркало panel: без дублей SiteOffer
        SiteOffer.site_id == site_id, SiteOffer.offer_id == so.offer_id)).scalar_one_or_none()
    if not exists:
        db.add(SiteOffer(site_id=site_id, **so.model_dump()))
        db.commit()
    return {"site_id": site_id, "offer_id": so.offer_id}


# --- M5 publish + monitor ---------------------------------------------------
@router.post("/sites/{site_id}/publish")
def do_publish(site_id: int):
    return publish.publish_site(site_id)   # refuses non-'edited' pages


@router.post("/sites/{site_id}/check-index")
def do_check_index(site_id: int):
    return publish.check_index(site_id)


# --- views ------------------------------------------------------------------
@router.get("/sites")
def list_sites(db: Session = Depends(get_session)):
    rows = db.execute(select(Site)).scalars().all()
    return [{"id": s.id, "domain_id": s.domain_id, "status": s.status,
             "cf_zone_id": s.cf_zone_id, "doc_root": s.doc_root} for s in rows]


@router.get("/sites/{site_id}/pages")
def list_pages(site_id: int, db: Session = Depends(get_session)):
    rows = db.execute(select(Page).where(Page.site_id == site_id)).scalars().all()
    return [{"id": p.id, "url_path": p.url_path, "title": p.title, "status": p.status,
             "index_status": p.index_status} for p in rows]
