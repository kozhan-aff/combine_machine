def test_get_tariffs_parses_price(monkeypatch):
    from app.integrations import backorder
    c = backorder.BackorderClient()
    class _R:
        @staticmethod
        def json(): return {"id": "42", "period": [{"id": "7"}], "cost": "199.00"}
    monkeypatch.setattr(c, "request", lambda *a, **k: _R())
    t = c.get_tariffs()
    assert t["price"] == 199.0 and t["price_id"] == "42" and t["period_id"] == "7"


def test_refresh_prices_only_backorder(monkeypatch, sqlite_db):
    from app.services import pricing
    import app.db as db
    from app.models.domain import Domain
    monkeypatch.setattr("app.integrations.backorder.BackorderClient.get_tariffs",
                        lambda self: {"price": 199.0, "price_id": "42", "period_id": "7"})
    with db.SessionLocal() as s:
        s.add_all([Domain(domain="bo.ru", source="backorder", status="discovered"),
                   Domain(domain="fr.ru", source="cctld", status="discovered")])
        s.commit()
    assert pricing.refresh_backorder_prices() == 1              # только backorder-домен
    with db.SessionLocal() as s:
        bo = s.execute(_dom("bo.ru")).scalar_one(); fr = s.execute(_dom("fr.ru")).scalar_one()
    assert float(bo.acquire_price) == 199.0 and bo.price_checked_at is not None
    assert fr.acquire_price is None                              # сырой не трогаем


def test_get_tariffs_survives_period_without_id(monkeypatch):
    from app.integrations import backorder

    class _Resp:
        def json(self):
            return {"id": 7, "price": 490, "period": [{"cost": 490}]}   # period без "id"

    c = backorder.BackorderClient()
    monkeypatch.setattr(c, "request", lambda *a, **k: _Resp())
    out = c.get_tariffs()                 # не должно падать KeyError
    assert out["period_id"] is None       # мягкий None вместо KeyError


def _dom(name):
    from sqlalchemy import select
    from app.models.domain import Domain
    return select(Domain).where(Domain.domain == name)
