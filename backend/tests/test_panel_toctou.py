"""S9/S10 (аудит 2026-07-18): check-then-insert роуты панели переживают конфликт
уникального индекса дружелюбным редиректом, а не голым 500.

TOCTOU-гонку (два конкурентных писателя под READ COMMITTED оба видят «нет» и оба
вставляют) детерминированно воспроизводим, заставив ПЕРВЫЙ commit поднять IntegrityError —
это ровно то, что случилось бы у проигравшего гонку писателя на уникальном индексе."""
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import app.db as db
from app.models.domain import Domain
from app.models.offer import Offer
from app.models.site import Site


def _seed_site_and_offer() -> tuple[int, int]:
    with db.SessionLocal() as s:
        d = Domain(domain="toctou.ru", source="backorder", status="purchased")
        s.add(d); s.commit(); s.refresh(d)
        site = Site(domain_id=d.id, status="content")
        offer = Offer(brand="NordVPN", affiliate_link="https://ref/nord")
        s.add(site); s.add(offer); s.commit()
        s.refresh(site); s.refresh(offer)
        return site.id, offer.id


def _commit_raises_once(monkeypatch):
    """Первый Session.commit -> IntegrityError (проигравший гонку), остальные — как есть."""
    orig = Session.commit
    state = {"n": 0}

    def flaky(self):
        state["n"] += 1
        if state["n"] == 1:
            raise IntegrityError("INSERT ...", {}, Exception("duplicate key"))
        return orig(self)
    monkeypatch.setattr(Session, "commit", flaky)


def test_attach_offer_toctou_is_friendly_not_500(client, monkeypatch):
    site_id, offer_id = _seed_site_and_offer()
    _commit_raises_once(monkeypatch)
    r = client.post(f"/sites/{site_id}/attach-offer", data={"offer_id": offer_id})
    # RedirectResponse -> follow -> 200 (страница сайта), НЕ 500
    assert r.status_code == 200


def test_reserve_url_toctou_is_friendly_not_500(client, monkeypatch):
    _commit_raises_once(monkeypatch)
    r = client.post("/offers/reserve-url", data={"reserve_offer_url": "https://ref/reserve"})
    assert r.status_code == 200
