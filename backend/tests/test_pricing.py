"""Тарифы backorder = СЕТКА СТАВОК, не одна цена. Форма фикстур снята с живого API 2026-07-11."""
import pytest


# Живая форма записи (см. docs/api/backorder.md §1): цена в period[0].price_num,
# верхнеуровневый price — строка "190.0000 RUB / 190", float() на ней падает.
def _tier(tid, pid, price, zone=".RU", type_id="63"):
    return {"id": tid, "type_id": type_id,
            "grp": f"Заказы на освобождающиеся доменные имена в {zone}",
            "price": f"{price}.0000 RUB / {price}",
            "period": [{"id": pid, "price_num": f"{price}.0000"}]}


_LIVE = [
    _tier("4769", "3442", 190), _tier("4770", "3443", 400), _tier("4771", "3444", 550),
    _tier("4790", "3463", 190, zone=".РФ"), _tier("4791", "3464", 400, zone=".РФ"),
    # шум, который сетка обязана отфильтровать: обычная регистрация (type_id=3), не backorder
    _tier("100", "200", 999, type_id="3"),
]


@pytest.fixture(autouse=True)
def _clear_grid():
    from app.integrations import backorder
    backorder._GRID_CACHE.clear()          # кеш на процесс — иначе тесты текут друг в друга
    yield
    backorder._GRID_CACHE.clear()


class _R:
    """Ответ price-JSON. tariffs() ходит мимо BaseClient.request (короткий таймаут, без
    ретрая — денежный экран не должен висеть), поэтому подменяем именно _client.request."""
    def __init__(self, payload): self._p = payload
    def json(self): return self._p
    def raise_for_status(self): return None


def _client(monkeypatch, payload=None):
    from app.integrations import backorder
    c = backorder.BackorderClient()
    monkeypatch.setattr(c._client, "request",
                        lambda *a, **k: _R(_LIVE if payload is None else payload))
    return c


def test_tariffs_grid_filters_type_and_zone(monkeypatch):
    """Сетка = только type_id 63 и только своя зона, по возрастанию цены."""
    c = _client(monkeypatch)
    ru = c.tariffs(".RU")
    assert [t["price"] for t in ru] == [190.0, 400.0, 550.0]      # 999 (type_id=3) отфильтрован
    assert ru[0] == {"price_id": "4769", "period_id": "3442", "price": 190.0}
    assert [t["price"] for t in c.tariffs(".РФ")] == [190.0, 400.0]


def test_get_tariffs_parses_price_from_period(monkeypatch):
    """Базовый тариф — самый дешёвый тир. Цена берётся из period[0].price_num:
    на верхнеуровневом "190.0000 RUB / 190" float() падал и цена приходила None."""
    t = _client(monkeypatch).get_tariffs()
    assert t["price"] == 190.0 and t["price_id"] == "4769" and t["period_id"] == "3442"


def test_pick_tariff_rounds_bid_up_to_tier(monkeypatch):
    """Ставка округляется ВВЕРХ до ближайшего тира: платить между тирами нельзя."""
    c = _client(monkeypatch)
    assert c.pick_tariff("x.ru", 190)["price"] == 190.0           # точное попадание
    assert c.pick_tariff("x.ru", 300)["price"] == 400.0           # между тирами -> вверх
    assert c.pick_tariff("x.ru", 999999)["price"] == 550.0        # выше сетки -> верхний тир


def test_pick_tariff_uses_domain_zone(monkeypatch):
    """Зона домена решает, из какой сетки брать тариф — .РФ не должен купиться по .RU-тарифу."""
    c = _client(monkeypatch)
    assert c.pick_tariff("сайт.рф", 190)["price_id"] == "4790"    # .РФ-тир, не 4769
    assert c.pick_tariff("site.ru", 190)["price_id"] == "4769"


def test_pick_tariff_refuses_empty_grid(monkeypatch):
    """Пустая сетка -> RuntimeError, а не молчаливый заказ по неизвестному тарифу."""
    c = _client(monkeypatch, payload=[])
    with pytest.raises(RuntimeError, match="сетка тарифов"):
        c.pick_tariff("x.ru", 190)


def test_empty_grid_is_not_cached(monkeypatch):
    """Пустой ответ НЕ кешируется: иначе один сбой формата навсегда ломает подтверждение
    (refresh=True ниоткуда не зовётся — сетка обновляется только рестартом контейнера)."""
    from app.integrations import backorder
    c = _client(monkeypatch, payload=[])
    assert c.tariffs(".RU") == []
    assert ".RU" not in backorder._GRID_CACHE          # пустоту не запомнили
    c2 = _client(monkeypatch)                          # провайдер «починился»
    assert len(c2.tariffs(".RU")) == 3                 # сетка поднялась без рестарта


def test_tariff_row_without_period_id_is_skipped(monkeypatch):
    """Битая запись (period без id) выбрасывается из сетки, а не роняет её KeyError'ом."""
    broken = {"id": "7", "type_id": "63", "grp": "... в .RU", "period": [{"price_num": "490"}]}
    c = _client(monkeypatch, payload=[broken])
    assert c.tariffs(".RU") == []
    assert c.get_tariffs()["period_id"] is None


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


def _dom(name):
    from sqlalchemy import select
    from app.models.domain import Domain
    return select(Domain).where(Domain.domain == name)
