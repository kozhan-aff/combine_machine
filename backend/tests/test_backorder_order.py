"""Денежный путь backorder: форма заказа, отсутствие ретрая, гейт, поллинг статусов.

Живой заказ здесь НЕ уходит — транспорт замокан. Проверяем логику, а не сеть.
"""
import httpx
import pytest

from app.integrations import backorder
from app.services import acquisition


class _Resp:
    def __init__(self, payload):
        self._p = payload

    def json(self):
        return self._p

    def raise_for_status(self):
        return None


def _client(payload, spy: list | None = None):
    """BackorderClient с замоканным транспортом; spy собирает params каждого вызова."""
    c = backorder.BackorderClient()
    c.login, c.password = "LOGIN", "SECRET"
    c.account_id, c.contact_id = "9841111", "301648"

    def fake(method, url, **kw):
        if spy is not None:
            spy.append(kw.get("params", {}))
        return _Resp(payload)
    c._client.request = fake          # type: ignore[method-assign]
    return c


# --- форма заказа (docs/api/backorder.md §4, офиц. дока 1:1) --------------------

def test_order_sends_documented_params():
    spy: list = []
    c = _client({"elem": [{"id": "5140302"}]}, spy)
    r = c.order("example.ru", price_id="4770", period_id="3443")

    assert r["order_id"] == "5140302"          # elid для последующего поллинга
    p = spy[0]
    assert p["func"] == "uniservice.order" and p["out"] == "json"
    assert p["price"] == "4770" and p["period"] == "3443"
    assert p["domainname"] == "example.ru"
    assert p["itype"] == "63"                  # тип услуги = освобождающийся домен
    assert p["payfrom"] == "account9841111"    # литерал 'account' + id счёта СЛИТНО
    assert p["contact"] == "301648"
    assert p["sok"] == "ok" and p["paynow"] == "on" and p["clientbackorder"] == "yes"
    assert p["authinfo"] == "LOGIN:SECRET"


def test_order_never_retries():
    """BaseClient.request ретраит транспорт 3 раза — на uniservice.order это второй
    платный заказ. Денежный вызов обязан идти мимо ретрая: ровно одна попытка."""
    calls: list = []
    c = _client({"elem": []})

    def boom(method, url, **kw):
        calls.append(1)
        raise httpx.ConnectTimeout("таймаут")
    c._client.request = boom          # type: ignore[method-assign]

    with pytest.raises(httpx.ConnectTimeout):
        c.order("example.ru", price_id="4770", period_id="3443")
    assert len(calls) == 1, f"заказ ушёл {len(calls)} раз — риск двойного списания"


def test_order_refuses_without_tariff():
    c = _client({"elem": []})
    with pytest.raises(RuntimeError, match="не задан тариф"):
        c.order("example.ru", price_id="", period_id="")


def test_order_refuses_without_account_or_contact():
    c = _client({"elem": []})
    c.contact_id = ""
    with pytest.raises(RuntimeError, match="BACKORDER_ACCOUNT_ID/BACKORDER_CONTACT_ID"):
        c.order("example.ru", price_id="4770", period_id="3443")


def test_provider_error_raises_and_scrubs_password():
    """Пароль уходит в query — он не должен всплыть ни в одной строке наружу."""
    c = _client({"error": {"code": "100", "msg": "Auth failed for SECRET"}})
    with pytest.raises(RuntimeError) as e:
        c.client_orders()
    assert "SECRET" not in str(e.value) and "***" in str(e.value)


# --- статусы провайдера ---------------------------------------------------------

def test_state_map_covers_all_documented_statuses():
    """8 «в процессе передачи» НЕ caught: caught терминален (домен -> purchased -> M3),
    а передача ещё может сорваться. Ждём 11 — ждать ничего не стоит."""
    failed, pending = [3, 6, 7, 9, 12, 14], [2, 4, 5, 8, 10, 13]
    assert backorder.state_of(11) == "caught"        # единственное терминальное «наш»
    assert all(backorder.state_of(s) == "failed" for s in failed)
    assert all(backorder.state_of(s) == "pending" for s in pending)
    assert backorder.state_of("11") == "caught"      # API отдаёт строкой
    assert backorder.state_of(None) == "pending"     # мусор -> не трогаем заказ


def test_zone_of():
    assert backorder.zone_of("site.ru") == ".RU"
    assert backorder.zone_of("сайт.рф") == ".РФ"
    assert backorder.zone_of("xn--80aswg.xn--p1ai") == ".РФ"   # punycode тоже .РФ


# --- гейт + очередь -------------------------------------------------------------

def _queued(status="approved"):
    """approved-домен в очереди выкупа -> order_id."""
    import app.db as db
    from app.models.domain import Domain
    with db.SessionLocal() as s:
        d = Domain(domain="drop.ru", source="backorder", status=status)
        s.add(d); s.commit(); s.refresh(d)
        did = d.id
    return acquisition.create_order(did, "backorder"), did


def test_confirm_requires_bid(sqlite_db):
    """Ставка = решение о деньгах. Без неё гейт не поднимается."""
    oid, _ = _queued()
    with pytest.raises(ValueError, match="не выбрана ставка"):
        acquisition.confirm_order(oid)
    with pytest.raises(ValueError, match="больше нуля"):
        acquisition.confirm_order(oid, -5)


def test_confirm_stores_bid(sqlite_db):
    oid, _ = _queued()
    r = acquisition.confirm_order(oid, 400)
    assert r["confirmed_by_human"] is True and r["bid_rub"] == 400.0


def test_execute_blocked_without_human_confirm(sqlite_db, monkeypatch):
    """ХАРД-ГЕЙТ: без confirmed_by_human заказ провайдеру не уходит, что бы ни было в cost."""
    sent: list = []
    monkeypatch.setattr(backorder.BackorderClient, "order",
                        lambda self, d, price_id, period_id: sent.append(d))
    oid, _ = _queued()
    r = acquisition.execute_confirmed_order(oid)
    assert "gate" in r["error"] and sent == [], "деньги ушли без подтверждения человеком!"


def test_execute_picks_tier_by_bid_and_sends(sqlite_db, monkeypatch):
    """Подтверждённый заказ: ставка 300 ₽ -> тир 400 ₽; в cost ложится фактический тир."""
    import app.db as db
    from app.models.domain import AcquisitionOrder
    sent: list = []
    monkeypatch.setattr(backorder.BackorderClient, "tariffs",
                        lambda self, zone=".RU", refresh=False: [
                            {"price_id": "4769", "period_id": "3442", "price": 190.0},
                            {"price_id": "4770", "period_id": "3443", "price": 400.0}])
    monkeypatch.setattr(backorder.BackorderClient, "order",
                        lambda self, d, price_id, period_id: sent.append((d, price_id, period_id))
                        or {"order_id": "5140302"})
    oid, _ = _queued()
    acquisition.confirm_order(oid, 300)                 # человек поставил 300 ₽
    r = acquisition.execute_confirmed_order(oid)

    assert r["status"] == "ordered"
    assert sent == [("drop.ru", "4770", "3443")], "ставка не округлилась вверх до тира"
    with db.SessionLocal() as s:
        o = s.get(AcquisitionOrder, oid)
        assert float(o.cost) == 400.0                   # фактический тир, не желаемые 300
        assert o.provider_order_id == "5140302"


def test_execute_does_not_double_order_if_provider_already_has_one(sqlite_db, monkeypatch):
    """ИДЕМПОТЕНТНОСТЬ ДЕНЕГ: у провайдера уже есть заказ на домен -> второй НЕ шлём.

    Закрывает ambiguous-таймаут (заказ ушёл, ответ не дошёл -> failed -> «повторить»)
    и ручной заказ из ЛК. Провайдерский domain работает как idempotency key.
    """
    sent: list = []
    monkeypatch.setattr(backorder.BackorderClient, "tariffs",
                        lambda self, zone=".RU", refresh=False: [
                            {"price_id": "4769", "period_id": "3442", "price": 190.0}])
    monkeypatch.setattr(backorder.BackorderClient, "find_order", lambda self, domain: {
        "elid": "5140302", "domain": domain, "id_status": "2",
        "clear_status": "Не оплачен", "state": "pending", "tariff": "190"})
    monkeypatch.setattr(backorder.BackorderClient, "order",
                        lambda self, d, price_id, period_id: sent.append(d))
    oid, _ = _queued()
    acquisition.confirm_order(oid, 190)
    r = acquisition.execute_confirmed_order(oid)

    assert sent == [], "второй платный заказ на тот же домен!"
    assert r["status"] == "ordered" and r["result"]["clear_status"] == "Не оплачен"
    import app.db as db
    from app.models.domain import AcquisitionOrder
    with db.SessionLocal() as s:
        assert s.get(AcquisitionOrder, oid).provider_order_id == "5140302"  # усыновили


def test_transport_error_marks_maybe_sent_not_plain_failed(sqlite_db, monkeypatch):
    """Обрыв связи неотличим от «заказ ушёл» -> maybe_sent, чтобы панель не звала повторить вслепую."""
    monkeypatch.setattr(backorder.BackorderClient, "tariffs",
                        lambda self, zone=".RU", refresh=False: [
                            {"price_id": "4769", "period_id": "3442", "price": 190.0}])
    monkeypatch.setattr(backorder.BackorderClient, "find_order", lambda self, domain: None)

    def _timeout(self, d, price_id, period_id):
        raise httpx.ReadTimeout("оборвалось")
    monkeypatch.setattr(backorder.BackorderClient, "order", _timeout)
    oid, _ = _queued()
    acquisition.confirm_order(oid, 190)
    r = acquisition.execute_confirmed_order(oid)
    assert r["status"] == "failed" and r["maybe_sent"] is True


def test_confirm_freezes_tier_and_execute_does_not_repick(sqlite_db, monkeypatch):
    """Тир выбирает ЧЕЛОВЕК на confirm, и он заморожен: execute не «доплачивает» за него сам.

    Свойство проверяем сменой сетки ПОД НОГАМИ между confirm и execute (так выглядит
    протухший процессный кеш или пополнение сетки у провайдера): заказ обязан уйти по
    тарифу, который человек подтвердил, а не по пересчитанному.
    """
    import app.db as db
    from app.models.domain import AcquisitionOrder
    grid = [{"price_id": "4769", "period_id": "3442", "price": 190.0},
            {"price_id": "4770", "period_id": "3443", "price": 400.0}]
    monkeypatch.setattr(backorder.BackorderClient, "tariffs",
                        lambda self, zone=".RU", refresh=False: grid)
    monkeypatch.setattr(backorder.BackorderClient, "find_order", lambda self, domain: None)
    sent: list = []
    monkeypatch.setattr(backorder.BackorderClient, "order",
                        lambda self, d, price_id, period_id: sent.append((price_id, period_id))
                        or {"order_id": "OK"})

    oid, _ = _queued()
    acquisition.confirm_order(oid, 300)                  # человек: 300 ₽ -> тир 400 ₽, заморожен
    with db.SessionLocal() as s:
        o = s.get(AcquisitionOrder, oid)
        assert float(o.cost) == 400.0                    # видит фактический тир, не свои 300
        assert o.result["price_id"] == "4770" and o.result["period_id"] == "3443"

    grid.insert(1, {"price_id": "9999", "period_id": "8888", "price": 300.0})   # сетка «уехала»
    assert acquisition.execute_confirmed_order(oid)["status"] == "ordered"
    assert sent == [("4770", "3443")], "execute пересчитал тариф вместо подтверждённого человеком"


def test_pick_tariff_refuses_unknown_zone(monkeypatch):
    """Незнакомая зона (.su/gTLD) -> отказ, а не покупка по .RU-сетке."""
    c = _client({"elem": []})
    monkeypatch.setattr(backorder.BackorderClient, "tariffs",
                        lambda self, zone=".RU", refresh=False: [
                            {"price_id": "4769", "period_id": "3442", "price": 190.0}])
    assert backorder.zone_of("site.su") is None
    with pytest.raises(RuntimeError, match="нет тарифной сетки"):
        c.pick_tariff("site.su", 190)


def test_poll_moves_ordered_to_caught_and_failed(sqlite_db, monkeypatch):
    """Поллинг тянет правду провайдера: 11 -> пойман (домен purchased), 7 -> не вышло."""
    import app.db as db
    from app.models.domain import Domain, AcquisitionOrder
    with db.SessionLocal() as s:
        won = Domain(domain="won.ru", source="backorder", status="purchasing")
        lost = Domain(domain="lost.ru", source="backorder", status="purchasing")
        s.add_all([won, lost]); s.commit()
        s.add_all([AcquisitionOrder(domain_id=won.id, provider="backorder", status="ordered",
                                    confirmed_by_human=True),
                   AcquisitionOrder(domain_id=lost.id, provider="backorder", status="ordered",
                                    confirmed_by_human=True)])
        s.commit()

    monkeypatch.setattr(backorder.BackorderClient, "client_orders", lambda self: [
        {"elid": "1", "domain": "won.ru", "id_status": "11", "clear_status": "Завершён",
         "state": "caught", "tariff": "400"},
        {"elid": "2", "domain": "lost.ru", "id_status": "7", "clear_status": "Аннулирован",
         "state": "failed", "tariff": "190"},
    ])
    r = acquisition.poll_orders()
    assert r["caught"] == 1 and r["failed"] == 1

    with db.SessionLocal() as s:
        from sqlalchemy import select
        doms = {d.domain: d.status for d in s.execute(select(Domain)).scalars()}
        sts = sorted(o.status for o in s.execute(select(AcquisitionOrder)).scalars())
    assert doms["won.ru"] == "purchased" and doms["lost.ru"] == "purchasing"
    assert sts == ["caught", "failed"]
