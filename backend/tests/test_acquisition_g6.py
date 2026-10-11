"""M2: безопасность денежного пути без нового провайдера (G6, аудит 2026-10-07: S3-03 … S3-10, шов F8-13).

Каждый тест падает без своего фикса. Сеть — только мок-транспорт/monkeypatch (рубильник conftest).
"""
from datetime import datetime, timedelta, timezone

import httpx
import pytest

import app.db as db
from app.config import settings
from app.integrations import backorder, registrar
from app.integrations.optimizator import (OptimizatorAmbiguous, OptimizatorClient, OptimizatorError,
                                          ZoneNotSold)
from app.models.domain import AcquisitionOrder, Domain
from app.services import acquisition
from app.services.transitions import TransitionDenied
from tests.test_order_recovery import _age_the_claim, _orders, _send_and_die_optimizator

KEY = "SECRETKEY123"


def _approved(name="free-clean.com", lane="free") -> int:
    with db.SessionLocal() as s:
        d = Domain(domain=name, source="dropcatch", status="approved", lane=lane)
        s.add(d)
        s.commit()
        s.refresh(d)
        return d.id


def _order(oid) -> AcquisitionOrder:
    with db.SessionLocal() as s:
        return s.get(AcquisitionOrder, oid)


def _prices(monkeypatch, price=179, seen=None):
    def _p(self, zone="ru"):
        if seen is not None:
            seen.append(zone)
        return {"price_registration": price, "currency": "RUB"}
    monkeypatch.setattr(OptimizatorClient, "prices", _p)


def _opt_order(monkeypatch, name="free-clean.com", price=179):
    _prices(monkeypatch, price)
    oid = acquisition.create_order(_approved(name), "optimizator")
    acquisition.confirm_order(oid)
    return oid


# --- S3-05: api_key не утекает ------------------------------------------------------------------

def test_register_error_text_has_no_api_key(monkeypatch):
    monkeypatch.setattr(settings, "OPTIMIZATOR_API_KEY", KEY)

    def fake_get(self, url, **kw):
        req = httpx.Request("GET", f"http://optimizator.ru/?a=api&sa=reg_domains&api_key={KEY}&nicd=&domains=a.com")
        return httpx.Response(502, request=req)
    monkeypatch.setattr("httpx.Client.get", fake_get)
    with pytest.raises(OptimizatorAmbiguous) as ei:
        OptimizatorClient().register(["a.com"])
    assert KEY not in str(ei.value) and "api_key=***" in str(ei.value)


def test_get_error_text_has_no_api_key(monkeypatch):
    monkeypatch.setattr(settings, "OPTIMIZATOR_API_KEY", KEY)
    c = OptimizatorClient(quick=True)
    c._client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(502)))
    with pytest.raises(OptimizatorAmbiguous) as ei:
        c.balance()
    assert KEY not in str(ei.value)


def test_queue_scrubs_key_in_old_rows_and_new_errors(monkeypatch):
    """Старая строка с утёкшим ключом (уже в БД) не показывается как есть: скраб при показе."""
    monkeypatch.setattr(settings, "OPTIMIZATOR_API_KEY", KEY)
    oid = _opt_order(monkeypatch)
    with db.SessionLocal() as s:
        o = s.get(AcquisitionOrder, oid)
        o.status = "failed"
        o.result = {"error": f"HTTPStatusError: for url 'http://optimizator.ru/?api_key={KEY}&x=1'"}
        s.commit()
    shown = acquisition.list_orders()[0]["result"]["error"]
    assert KEY not in shown and "***" in shown


def test_execute_failure_text_is_scrubbed(monkeypatch):
    monkeypatch.setattr(settings, "OPTIMIZATOR_API_KEY", KEY)
    oid = _opt_order(monkeypatch)
    monkeypatch.setattr(OptimizatorClient, "check_domain",
                        lambda self, d: (_ for _ in ()).throw(OptimizatorError("нет", 404)))
    monkeypatch.setattr(OptimizatorClient, "register",
                        lambda self, d: (_ for _ in ()).throw(RuntimeError(f"boom api_key={KEY}")))
    acquisition.execute_confirmed_order(oid)
    assert KEY not in str(_order(oid).result)


# --- S3-06: зона и канал до денег ---------------------------------------------------------------

def test_dotcom_through_backorder_refused_at_create():
    with pytest.raises(acquisition.ChannelUnavailable, match="только .RU/.РФ"):
        acquisition.create_order(_approved("x.com"), "backorder")
    with db.SessionLocal() as s:                              # заявки-призрака нет, домен не сдвинут
        assert s.query(AcquisitionOrder).count() == 0


def test_zone_outside_v2_whitelist_refused_at_create(monkeypatch):
    monkeypatch.setattr(acquisition, "_zone_allowlist", lambda: ["com"])   # реальный v2-список без .ru
    with pytest.raises(TransitionDenied):
        acquisition.create_order(_approved("old.ru"), "backorder")


def test_confirmed_order_for_now_closed_zone_does_not_reach_the_till(monkeypatch):
    """Заказ подтверждён, пока .ru был в списке; потом «РФ исключена» — execute на кассе его не пускает."""
    oid = _opt_order(monkeypatch, "legacy.ru")
    sent = []
    monkeypatch.setattr(OptimizatorClient, "register", lambda self, d: sent.append(d) or {"order_id": 1})
    monkeypatch.setattr(acquisition, "_zone_allowlist", lambda: ["com"])
    with pytest.raises(TransitionDenied):
        acquisition.execute_confirmed_order(oid)
    assert sent == [] and _order(oid).status == "pending_confirm"


def test_confirm_checks_route_too(monkeypatch):
    oid = _opt_order(monkeypatch, "legacy.ru")
    with db.SessionLocal() as s:                              # откатываем подтверждение, закрываем зону
        o = s.get(AcquisitionOrder, oid)
        o.confirmed_by_human = False
        s.commit()
    monkeypatch.setattr(acquisition, "_zone_allowlist", lambda: ["com"])
    with pytest.raises(TransitionDenied):
        acquisition.confirm_order(oid)


def test_default_channel_comes_from_zone_table(monkeypatch):
    from app.services.settings import update_settings
    did = _approved("viaauto.com")
    with pytest.raises(acquisition.ChannelUnavailable, match="не настроен"):   # нет записи -> registrar-заглушка
        acquisition.create_order(did)
    update_settings(zone_channels={"com": "optimizator"})
    oid = acquisition.create_order(did)
    assert _order(oid).provider == "optimizator"


def test_zone_channels_rejects_unknown_channel():
    from app.services.settings import update_settings
    with pytest.raises(ValueError, match="неизвестный канал"):
        update_settings(zone_channels={"com": "nosuchbank"})


# --- S3-07: подтверждение протухает -------------------------------------------------------------

def _age_confirm(oid, hours):
    with db.SessionLocal() as s:
        o = s.get(AcquisitionOrder, oid)
        o.confirmed_at = datetime.now(timezone.utc) - timedelta(hours=hours)
        s.commit()


def test_expired_confirmation_cannot_be_executed(monkeypatch):
    oid = _opt_order(monkeypatch)
    sent = []
    monkeypatch.setattr(OptimizatorClient, "check_domain",
                        lambda self, d: (_ for _ in ()).throw(OptimizatorError("нет", 404)))
    monkeypatch.setattr(OptimizatorClient, "register", lambda self, d: sent.append(d) or {"order_id": 1})
    _age_confirm(oid, settings.ACQ_CONFIRM_TTL_HOURS + 1)
    r = acquisition.execute_confirmed_order(oid)
    assert "просрочено" in r["error"] and sent == []
    o = _order(oid)
    assert o.confirmed_by_human is False and o.status == "pending_confirm"
    assert acquisition.list_orders()[0]["confirmed"] is False           # на экране снова «подтвердить»


def test_legacy_confirmation_without_timestamp_counts_as_expired(monkeypatch):
    oid = _opt_order(monkeypatch)
    with db.SessionLocal() as s:
        s.get(AcquisitionOrder, oid).confirmed_at = None
        s.commit()
    assert "просрочено" in acquisition.execute_confirmed_order(oid)["error"]


def test_retry_of_failed_order_needs_a_fresh_decision(monkeypatch):
    """«↻ повторить» старого подтверждённого failed без нового решения человека невозможен."""
    oid = _opt_order(monkeypatch)
    monkeypatch.setattr(OptimizatorClient, "check_domain",
                        lambda self, d: (_ for _ in ()).throw(OptimizatorError("нет", 404)))
    monkeypatch.setattr(OptimizatorClient, "register",
                        lambda self, d: (_ for _ in ()).throw(OptimizatorError("недостаточно средств", 42)))
    assert acquisition.execute_confirmed_order(oid)["status"] == "failed"
    _age_confirm(oid, 24 * 7)
    sent = []
    monkeypatch.setattr(OptimizatorClient, "register", lambda self, d: sent.append(d) or {"order_id": 9})
    assert "просрочено" in acquisition.execute_confirmed_order(oid)["error"] and sent == []
    # новое решение человека (confirm на failed) — и только тогда повтор идёт
    assert acquisition.confirm_order(oid)["confirmed_by_human"] is True
    assert acquisition.execute_confirmed_order(oid)["status"] == "ordered" and sent == [["free-clean.com"]]


def test_sql_claim_also_guards_ttl(monkeypatch):
    """Ремень: даже если Python-проверка пропущена (гонка), условный UPDATE не заклеймит просрочку."""
    oid = _opt_order(monkeypatch)
    monkeypatch.setattr(acquisition, "_confirm_expired", lambda o: False)
    monkeypatch.setattr(OptimizatorClient, "register", lambda self, d: pytest.fail("ушло провайдеру"))
    _age_confirm(oid, settings.ACQ_CONFIRM_TTL_HOURS + 1)
    r = acquisition.execute_confirmed_order(oid)
    assert r["status"] == "pending_confirm" and _order(oid).status == "pending_confirm"


# --- S3-04 / S3-03: опрос независим, виден сбой ---------------------------------------------------

def test_poll_survives_dead_backorder_and_still_recovers_optimizator(monkeypatch):
    did, oid = _send_and_die_optimizator(monkeypatch, "recover.com")
    _age_the_claim(oid)
    with db.SessionLocal() as s:   # у backorder есть что сверять -> он пойдёт в сеть и упадёт
        s.add(AcquisitionOrder(domain_id=_approved("bo-row.ru"), provider="backorder", status="ordered",
                               provider_order_id="7", confirmed_by_human=True))
        s.commit()
    monkeypatch.setattr(backorder.BackorderClient, "client_orders",
                        lambda self: (_ for _ in ()).throw(RuntimeError("backorder clientbackorder: капча")))
    monkeypatch.setattr(OptimizatorClient, "check_domain",
                        lambda self, d: {"data_end": "02.12.2027", "domain": d.upper()})
    r = acquisition.poll_orders()
    assert _orders(did)[0].status == "ordered"                        # optimizator восстановлен
    assert "капча" in r["errors"]["backorder"] and "optimizator" not in r["errors"]


def test_poll_does_not_touch_backorder_without_its_rows(monkeypatch):
    monkeypatch.setattr(backorder.BackorderClient, "client_orders",
                        lambda self: pytest.fail("сеть backorder без единой его строки"))
    assert acquisition.poll_orders()["errors"] == {}


def test_poll_total_deadline_is_reported_not_hung(monkeypatch):
    did, oid = _send_and_die_optimizator(monkeypatch, "slow.com")
    _age_the_claim(oid)
    monkeypatch.setattr(acquisition, "POLL_DEADLINE_SEC", -1.0)
    r = acquisition.poll_orders()
    assert "дедлайн" in r["errors"]["optimizator"] and _orders(did)[0].status == "ordering"


def test_optimizator_poll_uses_quick_client(monkeypatch):
    did, oid = _send_and_die_optimizator(monkeypatch, "quick.com")
    _age_the_claim(oid)
    seen = []
    monkeypatch.setattr(OptimizatorClient, "check_domain",
                        lambda self, d: seen.append(self.quick) or {"data_end": "x"})
    acquisition.poll_orders()
    assert seen == [True]


def test_client_orders_has_short_timeout_and_no_retry(monkeypatch):
    got = {}
    monkeypatch.setattr(backorder.BackorderClient, "_billmgr",
                        lambda self, func, **kw: got.update(kw) or [])
    backorder.BackorderClient().client_orders()
    assert got == {"retry": False, "timeout": 8.0}


def test_backorder_ping_names_the_captcha(monkeypatch):
    req = httpx.Request("GET", "https://backorder.ru/tmgrdfrend/showcaptcha")
    resp = httpx.Response(200, request=req, text="<html>captcha</html>")
    monkeypatch.setattr(backorder.BackorderClient, "request", lambda self, m, u, **kw: resp)
    with pytest.raises(RuntimeError, match="SmartCaptcha"):
        backorder.BackorderClient().ping()


def test_billmgr_captcha_is_not_a_json_error(monkeypatch):
    c = backorder.BackorderClient()
    c._client = httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, headers={"x-yandex-captcha": "captcha"}, text="<html>")))
    with pytest.raises(RuntimeError, match="SmartCaptcha"):
        c._billmgr("clientbackorder", retry=False)


# --- S3-08: зона, ZoneNotSold, валюта ------------------------------------------------------------

def test_prices_empty_list_is_zone_not_sold_not_ambiguous(monkeypatch):
    c = OptimizatorClient()
    monkeypatch.setattr(OptimizatorClient, "_fetch", lambda self, action, **p: [])
    with pytest.raises(ZoneNotSold):
        c.prices("co.uk")
    assert not issubclass(ZoneNotSold, OptimizatorAmbiguous)


def test_confirm_says_zone_not_supported(monkeypatch):
    monkeypatch.setattr(OptimizatorClient, "prices",
                        lambda self, zone: (_ for _ in ()).throw(ZoneNotSold(f"не продаёт .{zone}")))
    oid = acquisition.create_order(_approved("x.co.uk"), "optimizator")
    with pytest.raises(ValueError, match="зона не поддерживается"):
        acquisition.confirm_order(oid)
    assert _order(oid).confirmed_by_human is False


def test_zone_is_public_suffix_not_last_label(monkeypatch):
    seen = []
    _prices(monkeypatch, 179, seen)
    oid = acquisition.create_order(_approved("shop.co.uk"), "optimizator")
    acquisition.confirm_order(oid)
    assert seen == ["co.uk"]


def test_cost_has_explicit_currency(monkeypatch):
    oid = _opt_order(monkeypatch)
    assert _order(oid).cost_currency == "RUB"
    assert acquisition.list_orders()[0]["currency"] == "RUB"


# --- S3-09: баланс -------------------------------------------------------------------------------

def test_optimizator_refuses_before_send_when_balance_short(monkeypatch):
    oid = _opt_order(monkeypatch, price=179)
    monkeypatch.setattr(acquisition, "_balance_of", lambda c: 50.0)
    monkeypatch.setattr(OptimizatorClient, "check_domain",
                        lambda self, d: (_ for _ in ()).throw(OptimizatorError("нет", 404)))
    monkeypatch.setattr(OptimizatorClient, "register", lambda self, d: pytest.fail("ушло при нехватке денег"))
    r = acquisition.execute_confirmed_order(oid)
    assert r["status"] == "failed" and "пополни" in r["error"] and "maybe_sent" not in r


def test_optimizator_unknown_balance_does_not_block(monkeypatch):
    oid = _opt_order(monkeypatch)
    monkeypatch.setattr(acquisition, "_balance_of", lambda c: None)
    monkeypatch.setattr(OptimizatorClient, "check_domain",
                        lambda self, d: (_ for _ in ()).throw(OptimizatorError("нет", 404)))
    monkeypatch.setattr(OptimizatorClient, "register", lambda self, d: {"order_id": 5})
    assert acquisition.execute_confirmed_order(oid)["status"] == "ordered"


def test_backorder_refuses_before_order_when_balance_short(monkeypatch):
    monkeypatch.setattr(backorder.BackorderClient, "tariffs",
                        lambda self, zone=".RU", refresh=False: [{"price_id": "1", "period_id": "2", "price": 190.0}])
    oid = acquisition.create_order(_approved("bo.ru", "bid"), "backorder")
    acquisition.confirm_order(oid, 190)
    monkeypatch.setattr(acquisition, "_balance_of", lambda c: 0.0)
    monkeypatch.setattr(backorder.BackorderClient, "find_order", lambda self, d: None)
    monkeypatch.setattr(backorder.BackorderClient, "order",
                        lambda self, *a, **k: pytest.fail("заказ ушёл при нулевом балансе"))
    r = acquisition.execute_confirmed_order(oid)
    assert r["status"] == "failed" and "пополни" in r["error"]


def test_optimizator_price_rise_after_confirm_blocks_send(monkeypatch):
    oid = _opt_order(monkeypatch, price=179)
    _prices(monkeypatch, 250)
    monkeypatch.setattr(OptimizatorClient, "check_domain",
                        lambda self, d: (_ for _ in ()).throw(OptimizatorError("нет", 404)))
    monkeypatch.setattr(OptimizatorClient, "register", lambda self, d: pytest.fail("заплатили больше потолка"))
    r = acquisition.execute_confirmed_order(oid)
    assert r["status"] == "failed" and "выросла" in r["error"]


def test_channel_status_shows_balance_and_failure(monkeypatch):
    monkeypatch.setattr(settings, "OPTIMIZATOR_API_KEY", KEY)
    monkeypatch.setattr(OptimizatorClient, "balance", lambda self: 123.0)
    st = acquisition.channel_status()
    assert st["optimizator"]["balance"] == 123.0 and st["registrar"]["configured"] is False
    monkeypatch.setattr(OptimizatorClient, "balance",
                        lambda self: (_ for _ in ()).throw(OptimizatorAmbiguous(f"timeout api_key={KEY}")))
    err = acquisition.channel_status()["optimizator"]["error"]
    assert "timeout" in err and KEY not in err


def test_queue_page_shows_optimizator_balance(client, monkeypatch):
    monkeypatch.setattr(settings, "OPTIMIZATOR_API_KEY", KEY)
    monkeypatch.setattr(OptimizatorClient, "balance", lambda self: 0.0)
    html = client.get("/queue").text
    assert "баланс optimizator" in html and "регистратор для .com/.net/…: не настроен" in html


# --- S3-10: форма успеха reg_domains --------------------------------------------------------------

def test_register_without_order_id_is_ambiguous_not_ordered(monkeypatch):
    class _R:
        def raise_for_status(self): pass
        def json(self): return [{"status": "ok"}]
    monkeypatch.setattr("httpx.Client.get", lambda self, url, **kw: _R())
    with pytest.raises(OptimizatorAmbiguous, match="order_id"):
        OptimizatorClient().register(["a.com"])


def test_execute_unrecognized_success_shape_sets_maybe_sent(monkeypatch):
    oid = _opt_order(monkeypatch)
    monkeypatch.setattr(OptimizatorClient, "check_domain",
                        lambda self, d: (_ for _ in ()).throw(OptimizatorError("нет", 404)))

    class _R:
        def raise_for_status(self): pass
        def json(self): return [{"result": "queued"}]
    monkeypatch.setattr("httpx.Client.get", lambda self, url, **kw: _R())
    r = acquisition.execute_confirmed_order(oid)
    assert r["status"] == "failed" and r["maybe_sent"] is True


# --- S3-01 шов: Registrar + таблица зона->канал ----------------------------------------------------

class _FakeRegistrar:
    name, configured = "fake", True

    def __init__(self, price=10.0, balance=100.0, ambiguous=False):
        self._p, self._b, self._amb, self.registered = price, balance, ambiguous, []

    def check_available(self, domain): return True
    def price(self, domain): return registrar.Money(self._p, "USD")
    def balance(self): return registrar.Money(self._b, "USD")
    def set_nameservers(self, domain, ns): return {}

    def register(self, domain, period=1):
        if self._amb:
            raise registrar.RegistrarAmbiguous("timeout")
        self.registered.append((domain, period))
        return {"order_id": "R1"}


def _registrar_flow(monkeypatch, fake):
    from app.services.settings import update_settings
    monkeypatch.setattr(registrar, "get_registrar", lambda: fake)
    update_settings(zone_channels={"com": "registrar"})
    oid = acquisition.create_order(_approved("intl.com", "bid"))
    acquisition.confirm_order(oid)
    return oid


def test_not_configured_registrar_stub():
    r = registrar.get_registrar()
    assert isinstance(r, registrar.Registrar) and r.configured is False
    with pytest.raises(registrar.RegistrarNotConfigured):
        r.price("a.com")


def test_registrar_channel_runs_through_the_same_gate(monkeypatch):
    fake = _FakeRegistrar()
    oid = _registrar_flow(monkeypatch, fake)
    o = _order(oid)
    assert o.provider == "registrar" and o.cost_currency == "USD" and float(o.cost) == 10.0
    assert acquisition.execute_confirmed_order(oid)["status"] == "caught"     # регистрация синхронна = куплен
    assert fake.registered == [("intl.com", 1)]
    with db.SessionLocal() as s:
        assert s.get(Domain, _order(oid).domain_id).status == "purchased"


def test_registrar_gate_unconfirmed_never_registers(monkeypatch):
    fake = _FakeRegistrar()
    oid = _registrar_flow(monkeypatch, fake)
    with db.SessionLocal() as s:
        s.get(AcquisitionOrder, oid).confirmed_by_human = False
        s.commit()
    assert "gate" in acquisition.execute_confirmed_order(oid)["error"] and fake.registered == []


def test_registrar_ambiguous_sets_maybe_sent_and_balance_guard(monkeypatch):
    oid = _registrar_flow(monkeypatch, _FakeRegistrar(ambiguous=True))
    assert acquisition.execute_confirmed_order(oid)["maybe_sent"] is True
    fake = _FakeRegistrar(balance=1.0)
    monkeypatch.setattr(registrar, "get_registrar", lambda: fake)
    from app.services.settings import update_settings
    update_settings(zone_channels={"com": "registrar"})
    oid2 = acquisition.create_order(_approved("intl2.com", "bid"))
    acquisition.confirm_order(oid2)
    r = acquisition.execute_confirmed_order(oid2)
    assert r["status"] == "failed" and "пополни" in r["error"] and fake.registered == []


class _CurRegistrar(_FakeRegistrar):
    def __init__(self, price_cur="USD", bal_cur="USD", **kw):
        super().__init__(**kw)
        self._pc, self._bc = price_cur, bal_cur

    def price(self, domain): return registrar.Money(self._p, self._pc)
    def balance(self): return registrar.Money(self._b, self._bc)


@pytest.mark.parametrize("cur", ["EUR", ""])
def test_registrar_price_currency_mismatch_refuses_before_send(monkeypatch, cur):
    fake = _CurRegistrar()                       # подтверждаем в USD/USD
    oid = _registrar_flow(monkeypatch, fake)
    fake._pc = cur                               # валюта котировки уплыла после confirm; баланс в USD
    r = acquisition.execute_confirmed_order(oid)
    assert r["status"] == "failed" and "котировки" in r["error"] and fake.registered == []


def test_registrar_confirm_refuses_empty_quote_currency(monkeypatch):
    from app.services.settings import update_settings
    monkeypatch.setattr(registrar, "get_registrar", lambda: _CurRegistrar(price_cur=""))
    update_settings(zone_channels={"com": "registrar"})
    oid = acquisition.create_order(_approved("intl.com", "bid"))
    with pytest.raises(ValueError, match="валюта"):
        acquisition.confirm_order(oid)
    assert not _order(oid).confirmed_by_human


@pytest.mark.parametrize("cur", ["EUR", ""])
def test_registrar_balance_currency_mismatch_refuses_before_send(monkeypatch, cur):
    fake = _CurRegistrar(bal_cur=cur)
    oid = _registrar_flow(monkeypatch, fake)
    r = acquisition.execute_confirmed_order(oid)
    assert r["status"] == "failed" and "валюта" in r["error"] and fake.registered == []
