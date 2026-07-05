"""Web-layer hardening (review findings). Uses the SQLite `client` fixture from conftest.

Covers: same-origin CSRF guard on state-changing POST, server-side `limit` clamp on
both the panel and JSON domain lists, and JSON attach-offer dedup (mirror of panel).
TestClient sends no Origin/Referer (like curl/API clients) so existing tests keep working.
"""
from sqlalchemy import select, func

import app.db as db
from app.models.domain import Domain
from app.models.offer import SiteOffer


def _seed_domains(n: int, status: str = "scored") -> None:
    with db.SessionLocal() as s:
        for i in range(n):
            s.add(Domain(domain=f"seed-{i}.ru", source="backorder", status=status,
                         referring_domains=i, score=1.0 - i * 0.01))
        s.commit()


# --- CSRF: same-origin guard on state-changing methods ----------------------
def test_post_foreign_origin_rejected(client):
    """Враждебный Origin на POST -> 403 (до хендлера), даже без валидного тела."""
    r = client.post("/api/offers", json={"brand": "X", "affiliate_link": "https://e/x"},
                    headers={"origin": "http://evil.example"}, follow_redirects=False)
    assert r.status_code == 403


def test_post_foreign_referer_rejected(client):
    """Referer (когда Origin нет) с чужим хостом на POST -> 403."""
    r = client.post("/api/offers", json={"brand": "X", "affiliate_link": "https://e/x"},
                    headers={"referer": "http://evil.example/page"}, follow_redirects=False)
    assert r.status_code == 403


def test_post_money_gate_forgery_rejected(client):
    """Ключевой сценарий: подделка «человеческого» подтверждения из чужой вкладки -> 403."""
    r = client.post("/queue/1/confirm", headers={"origin": "http://evil.example"},
                    follow_redirects=False)
    assert r.status_code == 403


def test_post_same_origin_allowed(client):
    """Свой Origin (совпадает с Host) на POST -> проходит как обычно."""
    r = client.post("/api/offers", json={"brand": "OK", "affiliate_link": "https://e/ok"},
                    headers={"origin": "http://testserver"})
    assert r.status_code == 200 and r.json()["brand"] == "OK"


def test_post_without_origin_still_works(client):
    """Без Origin/Referer (curl/скрипты/TestClient) — не блокируем (обратная совместимость)."""
    r = client.post("/api/offers", json={"brand": "NoOrigin", "affiliate_link": "https://e/n"})
    assert r.status_code == 200 and r.json()["brand"] == "NoOrigin"


def test_get_foreign_origin_not_blocked(client):
    """GET — safe-метод: чужой Origin не блокируется."""
    r = client.get("/domains", headers={"origin": "http://evil.example"})
    assert r.status_code == 200


# --- limit clamp: server-side cap -------------------------------------------
def test_api_limit_clamped_low(client):
    """limit<=0 клампится к 1 (без клампа SQL LIMIT 0 вернул бы 0 строк)."""
    _seed_domains(3)
    rows = client.get("/api/domains/?limit=0").json()
    assert len(rows) == 1


def test_api_limit_huge_does_not_error(client):
    """Огромный limit не тянет всю таблицу и не падает (кап 1000, строк меньше)."""
    _seed_domains(3)
    rows = client.get("/api/domains/?limit=100000000").json()
    assert len(rows) == 3


def test_panel_limit_clamped_high(client):
    """Панель клампит верх до 1000 — в поле фильтра отражается 1000, не миллионы."""
    r = client.get("/domains?limit=100000000")
    assert r.status_code == 200 and 'value="1000"' in r.text


# --- JSON attach-offer dedup (mirror of panel.attach_offer_action) -----------
def test_api_attach_offer_no_duplicate(client):
    """Повторный attach через /api не создаёт второй SiteOffer."""
    offer_id = client.post("/api/offers", json={
        "brand": "DupVPN", "affiliate_link": "https://e/d"}).json()["id"]
    with db.SessionLocal() as s:
        d = Domain(domain="dup.ru", source="backorder", status="purchased")
        s.add(d)
        s.commit()
        did = d.id
    site_id = client.post(f"/api/domains/{did}/site").json()["site_id"]

    r1 = client.post(f"/api/sites/{site_id}/offer", json={"offer_id": offer_id})
    r2 = client.post(f"/api/sites/{site_id}/offer", json={"offer_id": offer_id})
    assert r1.status_code == 200 and r2.status_code == 200
    with db.SessionLocal() as s:
        n = s.scalar(select(func.count()).select_from(SiteOffer).where(
            SiteOffer.site_id == site_id, SiteOffer.offer_id == offer_id))
    assert n == 1
