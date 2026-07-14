"""Задача 5 (F8): ставка обязана быть конечным числом внутри тарифной сетки.

Репро на a6ab6ab: `confirm_order` держит два гарда — `if not bid_rub` и `if bid_rub <= 0`.
`float('nan')` проходит ОБА: `not nan` -> False (nan truthy), `nan <= 0` -> False (любое
сравнение с nan ложно). `float('inf')` — тоже (inf truthy, `inf <= 0` -> False). Дальше
`pick_tariff` не находит тир с ценой >= bid_rub и молча возвращает `grid[-1]` — верхний тир
сетки (в проде 5 000 000 ₽).

`-inf` — единственный из трёх, кто ловился и ДО фикса: гард `bid_rub <= 0` для него истинен.
Здесь он закреплён под новым, правильным поводом (неконечность), а не по знаку — чтобы гард
нельзя было ослабить до «только про знак», не уронив тест.

Ставка выше сетки — то же самое молчание: `pick_tariff` возвращал верхний тир вместо явной
ошибки, поэтому опечатка (лишний ноль) списывала счёт по максимальному тарифу без единого слова.

Форма панели (`POST /queue/{id}/confirm`, `bid_rub: float = Form(0)`) не спасает: Pydantic v2
разбирает строки "nan"/"inf"/"1e400" в float молча (проверено — `TypeAdapter(float)` даёт
nan/inf без ошибки), значит guard обязан жить в `confirm_order`, а не только в UI.
"""
import math

import pytest

from app.services import acquisition
from app.integrations import backorder


def _queued(status="approved"):
    """approved-домен в очереди выкупа -> order_id (тот же паттерн, что test_backorder_order.py)."""
    import app.db as db
    from app.models.domain import Domain
    with db.SessionLocal() as s:
        d = Domain(domain="drop.ru", source="backorder", status=status)
        s.add(d); s.commit(); s.refresh(d)
        did = d.id
    return acquisition.create_order(did, "backorder")


def _offline_grid(monkeypatch, grid=None):
    """Сетка .RU + «у провайдера заказов нет» — офлайн, без живой сети (рубильник в conftest)."""
    monkeypatch.setattr(backorder.BackorderClient, "tariffs",
                        lambda self, zone=".RU", refresh=False: grid or [
                            {"price_id": "4769", "period_id": "3442", "price": 190.0},
                            {"price_id": "4770", "period_id": "3443", "price": 400.0}])
    monkeypatch.setattr(backorder.BackorderClient, "find_order", lambda self, domain: None)


@pytest.mark.parametrize("bad_bid", [float("nan"), float("inf"), float("-inf")])
def test_confirm_rejects_non_finite_bid(sqlite_db, monkeypatch, bad_bid):
    """nan/inf/-inf обязаны падать ValueError — не доезжать до pick_tariff (и уж тем более
    не молчаливым тиром 5 000 000 ₽)."""
    _offline_grid(monkeypatch)
    oid = _queued()
    with pytest.raises(ValueError, match="конечным числом"):
        acquisition.confirm_order(oid, bad_bid)

    from app.models.domain import AcquisitionOrder
    import app.db as db
    with db.SessionLocal() as s:
        o = s.get(AcquisitionOrder, oid)
        assert o.confirmed_by_human is False, "гейт не должен подняться на мусорной ставке"
        assert o.cost is None, "мусорная ставка не должна долететь до cost"


@pytest.mark.parametrize("bad_bid, why", [(0, "не выбрана ставка"),
                                          (-5.0, "больше нуля")])
def test_confirm_rejects_zero_or_negative_bid(sqlite_db, monkeypatch, bad_bid, why):
    """Ноль и отрицательное ловились и ДО фикса — но каждое своим гардом, и сторожить надо
    именно повод. Без `match=` тест остался бы зелёным, даже если снести isfinite-проверку:
    ноль всё равно упал бы на «не выбрана ставка», и дыра для nan/inf проехала бы незамеченной."""
    _offline_grid(monkeypatch)
    oid = _queued()
    with pytest.raises(ValueError, match=why):
        acquisition.confirm_order(oid, bad_bid)


def test_confirm_rejects_bid_above_top_tier(sqlite_db, monkeypatch):
    """Ставка выше самого дорогого тира сетки — явная ошибка (MAX_BID_RUB), а не молчаливый
    выбор верхнего тира. Раньше `pick_tariff` тихо возвращал `grid[-1]` на любой ставке, для
    которой не нашлось тира с ценой >= bid_rub — то есть и на 1e9, и на nan."""
    _offline_grid(monkeypatch)   # верхний тир сетки — 400 ₽
    oid = _queued()
    with pytest.raises(ValueError, match="выше максимума"):
        acquisition.confirm_order(oid, 1_000_000.0)

    from app.models.domain import AcquisitionOrder
    import app.db as db
    with db.SessionLocal() as s:
        o = s.get(AcquisitionOrder, oid)
        assert o.confirmed_by_human is False
        assert o.cost is None, "ставка выше сетки не должна списаться по верхнему тиру"


def test_confirm_accepts_bid_exactly_at_top_tier(sqlite_db, monkeypatch):
    """Ставка РОВНО в максимум сетки — легитимная, не должна упасть в MAX_BID_RUB-гард."""
    _offline_grid(monkeypatch)   # верхний тир — 400 ₽
    oid = _queued()
    r = acquisition.confirm_order(oid, 400.0)
    assert r["confirmed_by_human"] is True and r["bid_rub"] == 400.0


def test_pick_tariff_rejects_non_finite_and_above_max(monkeypatch):
    """Тот же гард — на уровне транспортного клиента (единственный вызывающий — confirm_order,
    но метод публичный, и молчаливый клэмп был именно здесь)."""
    monkeypatch.setattr(backorder.BackorderClient, "tariffs",
                        lambda self, zone=".RU", refresh=False: [
                            {"price_id": "4769", "period_id": "3442", "price": 190.0}])
    c = backorder.BackorderClient()
    for bad in (float("nan"), float("inf"), float("-inf"), 0.0, -5.0):
        with pytest.raises(ValueError):
            c.pick_tariff("drop.ru", bad)
    with pytest.raises(ValueError, match="выше максимума"):
        c.pick_tariff("drop.ru", 1_000_000.0)
    assert math.isfinite(c.pick_tariff("drop.ru", 190.0)["price"])   # легитимная ставка проходит
