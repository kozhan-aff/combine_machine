"""NameSilo (W2b): клиент регистратора по docs/v2/research/namesilo-api-spec.md.

Сеть — только httpx.MockTransport (рубильник conftest). Реальных заказов нет. Каждый тест падает без
своего фикса: таблица кодов спеки §3/§4, write-once register, маскирование ключа, сверка исхода.
"""
import logging
from datetime import datetime, timedelta, timezone

import json
import httpx
from pathlib import Path
import pytest

import app.db as db
from app.config import settings
from app.integrations import namesilo as ns
from app.integrations import registrar
from app.integrations.namesilo import (NameSiloAmbiguous, NameSiloAuthError, NameSiloClient, NameSiloError,
                                       NameSiloReadError, as_list)
from app.models.domain import AcquisitionOrder, Domain
from app.services import acquisition

KEY = "NSKEY-abc123SECRET"


def ok(**kw):
    return {"reply": {"code": 300, "detail": "success", **kw}}


def err(code, detail="x", **kw):
    return {"reply": {"code": code, "detail": detail, **kw}}


class Srv:
    """Мок-сервер NameSilo: handlers[op] — dict (200 + JSON), httpx.Response, исключение или callable(request)."""

    def __init__(self, **handlers):
        self.handlers, self.calls = handlers, []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        op = request.url.path.rsplit("/", 1)[-1]
        self.calls.append((op, dict(request.url.params), request))
        h = self.handlers.get(op)
        if callable(h):
            h = h(request)
        if isinstance(h, list):                      # последовательность ответов; последний повторяется
            h = h.pop(0) if len(h) > 1 else h[0]
        if isinstance(h, Exception):
            raise h
        if isinstance(h, httpx.Response):
            return h
        if h is None:
            return httpx.Response(200, json=err(107, "Invalid API operation"))
        return httpx.Response(200, json=h)

    def n(self, op):
        return [c for c in self.calls if c[0] == op]


@pytest.fixture
def sleeps(monkeypatch):
    out = []
    monkeypatch.setattr(ns, "_sleep", out.append)
    clock = [1000.0]

    def tick():                      # часы бегут вперёд сами: пейсинг не ждёт и не пачкает список пауз
        clock[0] += 10
        return clock[0]
    monkeypatch.setattr(ns, "_monotonic", tick)
    monkeypatch.setattr(ns, "_last_call", [0.0])
    return out


@pytest.fixture
def make(monkeypatch, sleeps):
    monkeypatch.setattr(settings, "NAMESILO_API_KEY", KEY)
    monkeypatch.setattr(settings, "NAMESILO_SANDBOX", False)
    monkeypatch.setattr(settings, "NAMESILO_BASE_URL", "https://www.namesilo.com/apibatch")
    monkeypatch.setattr(settings, "NAMESILO_ALLOW_PREMIUM", False)
    monkeypatch.setattr(settings, "NAMESILO_CONTACT_ID", "")

    def _make(**handlers):
        srv = Srv(**handlers)
        c = NameSiloClient()
        c._client = httpx.Client(transport=httpx.MockTransport(srv))
        return c, srv
    return _make


AVAIL = ok(available={"domain": "good.com", "price": 17.29, "premium": 0, "duration": 1})


# --- транспорт ----------------------------------------------------------------------------------

def test_calls_go_to_apibatch_with_key_only_in_query(make):
    c, srv = make(getAccountBalance=ok(balance=12.5))
    c.balance()
    op, params, req = srv.calls[0]
    assert req.url.path == "/apibatch/getAccountBalance"
    assert params["key"] == KEY and params["version"] == "1" and params["type"] == "json"
    assert KEY not in str(dict(req.headers)) and req.method == "GET" and not req.content


def test_sandbox_flag_switches_base_url(make, monkeypatch):
    monkeypatch.setattr(settings, "NAMESILO_SANDBOX", True)
    c, srv = make(getAccountBalance=ok(balance=1))
    c.balance()
    assert str(srv.calls[0][2].url).startswith("https://sandbox.namesilo.com/api/getAccountBalance")


def test_plain_http_base_url_is_refused_before_sending_the_key(make, monkeypatch):
    monkeypatch.setattr(settings, "NAMESILO_BASE_URL", "http://www.namesilo.com/apibatch")
    c, srv = make(getAccountBalance=ok(balance=1))
    with pytest.raises(NameSiloError, match="https"):
        c.balance()
    assert srv.calls == []


def test_missing_key_never_hits_the_network(make, monkeypatch):
    monkeypatch.setattr(settings, "NAMESILO_API_KEY", "")
    c, srv = make()
    with pytest.raises(NameSiloError):
        c.balance()
    assert srv.calls == [] and c.configured is False


def test_body_is_read_on_http_401_and_string_code_is_normalized(make):
    # живая находка: getPrices/getAccountBalance отдают HTTP 401 с JSON и числовым кодом, check* — 200 и "110"
    c, _ = make(getAccountBalance=httpx.Response(401, json=err(110, "Invalid API key")))
    with pytest.raises(NameSiloAuthError) as ei:
        c.balance()
    assert ei.value.code == 110
    c2, _ = make(getAccountBalance=err("110", "Invalid API Key (Permission denied)"))
    with pytest.raises(NameSiloAuthError):
        c2.balance()


def test_as_list_normalizes_single_object_and_list():
    assert as_list(None) == [] and as_list("") == []
    assert as_list({"a": 1}) == [{"a": 1}] and as_list([{"a": 1}, {"a": 2}]) == [{"a": 1}, {"a": 2}]


def test_auth_error_is_not_retried(make, sleeps):
    c, srv = make(getAccountBalance=err(113, "IP not allowed"))
    with pytest.raises(NameSiloAuthError, match="IP"):
        c.balance()
    assert len(srv.calls) == 1 and sleeps == []


def test_throttle_keeps_one_second_between_calls(make, monkeypatch, sleeps):
    t = [100.0]
    monkeypatch.setattr(ns, "_monotonic", lambda: t[0])
    monkeypatch.setattr(ns, "_last_call", [t[0]])
    c, _ = make(getAccountBalance=ok(balance=1))
    c.balance()                       # прошло 0 с с прошлого вызова -> ждём весь интервал
    assert sleeps == [pytest.approx(ns.MIN_INTERVAL)]
    t[0] += 5
    c.balance()                       # прошло 5 с -> не ждём
    assert len(sleeps) == 1


# --- чтение: ретраи -----------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [
    httpx.Response(500), httpx.Response(429), httpx.Response(200, text="<html>cloudflare</html>"),
    err(400, "still processing"), err(115, "registry down"), err(201, "internal")])
def test_read_retries_temporary_failures_then_succeeds(make, bad, sleeps):
    c, srv = make(getAccountBalance=[bad, bad, ok(balance=7)])
    assert c.balance().amount == 7.0
    assert len(srv.n("getAccountBalance")) == 3 and len(sleeps) >= 2     # паузы между попытками


def test_read_gives_up_after_three_attempts_as_read_error(make):
    c, srv = make(getAccountBalance=httpx.Response(503))
    with pytest.raises(NameSiloReadError):
        c.balance()
    assert len(srv.calls) == 3


def test_balance_missing_field_is_none_not_zero(make):
    c, _ = make(getAccountBalance=ok())
    assert c.balance() is None and c.ping() is False


def test_balance_money_and_ping(make):
    c, _ = make(getAccountBalance=ok(balance="355.75"))
    assert c.balance() == registrar.Money(355.75, "USD") and c.ping() is True


# --- доступность и цена -------------------------------------------------------------------------

def test_check_many_batches_by_200_with_commas(make):
    names = [f"d{i}.com" for i in range(450)]
    c, srv = make(checkRegisterAvailability=ok())
    c.check_many(names)
    sizes = [len(p["domains"].split(",")) for _, p, _ in srv.n("checkRegisterAvailability")]
    assert sizes == [200, 200, 50]


def test_check_many_parses_object_and_list_forms(make):
    c, _ = make(checkRegisterAvailability=ok(
        available=[{"domain": "A.com", "price": "17.29", "premium": "0", "duration": "1"}],
        unavailable={"domain": "b.com"}, invalid=[{"domain": "c"}, "d"]))
    r = c.check_many(["a.com", "b.com"])
    assert r["a.com"] == {"status": "available", "price": 17.29, "premium": 0, "duration": 1}
    assert r["b.com"]["status"] == "unavailable" and r["c"]["status"] == "invalid" and r["d"]["status"] == "invalid"


def test_price_is_usd_per_year(make):
    c, _ = make(checkRegisterAvailability=AVAIL)
    assert c.price("good.com") == registrar.Money(17.29, "USD")
    assert c.check_available("good.com") is True


def test_price_refuses_premium_without_operator_flag(make, monkeypatch):
    prem = ok(available={"domain": "good.com", "price": 2500, "premium": 1, "duration": 1})
    c, _ = make(checkRegisterAvailability=prem)
    with pytest.raises(NameSiloError, match="премиум"):
        c.price("good.com")
    monkeypatch.setattr(settings, "NAMESILO_ALLOW_PREMIUM", True)
    c2, _ = make(checkRegisterAvailability=prem)
    assert c2.price("good.com").amount == 2500


def test_price_refuses_unavailable_and_ignores_duration(make):
    # duration=10 — у всех зон в живом ответе, это не срок цены (см. LIVE_AVAIL): цена принимается
    c, _ = make(checkRegisterAvailability=ok(available={"domain": "good.com", "price": 90, "premium": 0, "duration": 10}))
    assert c.price("good.com").amount == 90
    c2, _ = make(checkRegisterAvailability=ok(unavailable={"domain": "good.com"}))
    with pytest.raises(NameSiloError, match="недоступен"):
        c2.price("good.com")


LIVE_AVAIL = json.loads((Path(__file__).parent / "fixtures" / "namesilo_check_availability_live.json").read_text())


def _live(name):
    return {"reply": LIVE_AVAIL[name]}


def test_check_many_live_single_element_shapes(make):
    """2026-10-10 на боксе: tunnelnotes.xyz был доступен, а гейт говорил «нет в ответе» — один домен
    приходит объектом {"domain": {...}}, разбор брал строку целиком. Образцы — живые."""
    c, _ = make(checkRegisterAvailability=_live("one_available"))
    assert c.check_many(["tunnelnotes.xyz"]) == {
        "tunnelnotes.xyz": {"status": "available", "price": 2.79, "premium": 0, "duration": 10}}
    assert c.check_available("tunnelnotes.xyz") is True
    assert c.price("tunnelnotes.xyz") == registrar.Money(2.79, "USD")
    c, _ = make(checkRegisterAvailability=_live("mix"))
    r = c.check_many(["tunnelnotes.xyz", "google.com", "tunnelnotes.zzzzzz"])
    assert r["tunnelnotes.xyz"]["status"] == "available"
    assert r["google.com"] == {"status": "unavailable"} and r["tunnelnotes.zzzzzz"] == {"status": "invalid"}
    c, _ = make(checkRegisterAvailability=_live("many_unavailable"))
    assert c.check_many(["google.com", "facebook.com"]) == {"google.com": {"status": "unavailable"},
                                                            "facebook.com": {"status": "unavailable"}}
    c, _ = make(checkRegisterAvailability=_live("many_available"))
    r = c.check_many(["tunnelnotes-q1.com", "tunnelnotes-q1.co.uk"])
    assert r["tunnelnotes-q1.co.uk"]["price"] == 6.49 and c.check_available("tunnelnotes-q1.com")


# --- регистрация (ДЕНЬГИ) -----------------------------------------------------------------------

NOT_OURS = err(200, "Domain is not active, or does not belong to this account")
REG_OK = ok(domain="good.com", order_amount=17.29, message="Your domain registration was successfully processed.")


def test_register_sends_one_request_with_explicit_safe_flags(make):
    c, srv = make(getDomainInfo=NOT_OURS, registerDomain=REG_OK)
    res = c.register("Good.com", 1)
    (op, p, req), = srv.n("registerDomain")
    assert req.url.path == "/apibatch/registerDomain"
    assert p["years"] == "1" and p["private"] == "1" and p["auto_renew"] == "0" and p["domain"] == "good.com"
    assert "contact_id" not in p
    assert res["order_amount"] == 17.29 and res["currency"] == "USD" and res["warnings"] == []
    assert [o for o, *_ in srv.calls] == ["getDomainInfo", "registerDomain"]      # adopt-проверка ДО отправки


def test_register_passes_contact_id_when_configured(make, monkeypatch):
    monkeypatch.setattr(settings, "NAMESILO_CONTACT_ID", "98765")
    c, srv = make(getDomainInfo=NOT_OURS, registerDomain=REG_OK)
    c.register("good.com")
    assert srv.n("registerDomain")[0][1]["contact_id"] == "98765"


def test_register_omits_private_for_zones_without_privacy(make):
    c, srv = make(getDomainInfo=NOT_OURS, registerDomain=ok(domain="shop.co.uk", order_amount=6.49))
    c.register("shop.co.uk")
    p = srv.n("registerDomain")[0][1]
    assert "private" not in p and p["auto_renew"] == "0"


def test_register_only_one_year(make):
    c, srv = make(getDomainInfo=NOT_OURS, registerDomain=REG_OK)
    with pytest.raises(NameSiloError):
        c.register("good.com", 2)
    assert srv.calls == []


def test_register_adopts_our_active_domain_without_second_order(make):
    info = ok(created="2026-10-01", status="Active",
              nameservers=[{"nameserver": "NS1.NAMESILO.COM", "position": 1}])
    c, srv = make(getDomainInfo=info, registerDomain=REG_OK)
    res = c.register("good.com")
    assert res["adopted"] is True and srv.n("registerDomain") == []


def test_register_refuses_foreign_status_without_sending(make):
    c, srv = make(getDomainInfo=ok(status="Expired"), registerDomain=REG_OK)
    with pytest.raises(NameSiloError, match="Expired"):
        c.register("good.com")
    assert srv.n("registerDomain") == []


def test_register_read_failure_before_send_is_clean_refusal(make):
    c, srv = make(getDomainInfo=httpx.Response(500), registerDomain=REG_OK)
    with pytest.raises(NameSiloReadError):
        c.register("good.com")
    assert srv.n("registerDomain") == []


@pytest.mark.parametrize("code,warn", [(301, "NS"), (302, "контакт")])
def test_register_codes_301_302_are_success_with_warning(make, code, warn):
    c, _ = make(getDomainInfo=NOT_OURS, registerDomain=ok(domain="good.com", order_amount=9.0) | {
        "reply": {"code": code, "domain": "good.com", "order_amount": 9.0}})
    res = c.register("good.com")
    assert res["code"] == code and warn in res["warnings"][0]


AMBIGUOUS_RESPONSES = {
    "http500": httpx.Response(500), "http502": httpx.Response(502), "http408": httpx.Response(408),
    "http429": httpx.Response(429), "html": httpx.Response(200, text="<html>Just a moment...</html>"),
    "empty_json": httpx.Response(200, json={}), "no_code": httpx.Response(200, json={"reply": {"detail": "?"}}),
    "timeout": httpx.ReadTimeout("read timed out"), "conn_drop": httpx.RemoteProtocolError("peer closed"),
    "115": err(115), "201": err(201), "400": err(400, "previous request still processing"),
    "261": err(261), "262": err(262, "Domain is already active in the system"), "210": err(210, "generic error"),
    "unknown_code": err(999), "300_no_amount": ok(domain="good.com"),
    "300_no_domain": ok(order_amount=9.0), "300_other_domain": ok(domain="other.com", order_amount=9.0),
}


@pytest.mark.parametrize("key", sorted(AMBIGUOUS_RESPONSES))
def test_register_unknown_outcome_is_ambiguous_and_never_retried(make, key, sleeps):
    c, srv = make(getDomainInfo=NOT_OURS, registerDomain=AMBIGUOUS_RESPONSES[key])
    with pytest.raises(NameSiloAmbiguous):
        c.register("good.com")
    assert len(srv.n("registerDomain")) == 1          # ни одного повтора платного вызова
    assert not any(s >= 2 for s in sleeps)            # и пауз ретрая нет


@pytest.mark.parametrize("code", [101, 102, 103, 104, 105, 106, 107, 108, 109, 114, 116, 117, 118, 119, 200, 263, 267])
def test_register_clean_rejection_codes_are_rejected_not_ambiguous(make, code):
    c, srv = make(getDomainInfo=NOT_OURS, registerDomain=err(code, "no"))
    with pytest.raises(NameSiloError) as ei:
        c.register("good.com")
    assert not isinstance(ei.value, NameSiloAmbiguous) and ei.value.code == code
    assert len(srv.n("registerDomain")) == 1


def test_register_insufficient_funds_has_human_text(make):
    c, _ = make(getDomainInfo=NOT_OURS, registerDomain=err(119, "x"))
    with pytest.raises(NameSiloError, match="недостаточно средств"):
        c.register("good.com")


@pytest.mark.parametrize("code", [110, 111, 112, 113])
def test_register_auth_codes_are_auth_errors(make, code):
    c, _ = make(getDomainInfo=NOT_OURS, registerDomain=httpx.Response(401, json=err(code)))
    with pytest.raises(NameSiloAuthError):
        c.register("good.com")


def test_register_210_with_claims_is_clean_refusal_needing_a_human(make):
    c, _ = make(getDomainInfo=NOT_OURS, registerDomain=err(210, "claims", claims=[{"x": 1}]))
    with pytest.raises(NameSiloError, match="claims") as ei:
        c.register("good.com")
    assert not isinstance(ei.value, NameSiloAmbiguous)


# --- NS -----------------------------------------------------------------------------------------

def test_set_nameservers_verifies_case_insensitively(make):
    info = ok(nameservers=[{"nameserver": "ANN.NS.CLOUDFLARE.COM", "position": 1},
                           {"nameserver": "bob.ns.cloudflare.com.", "position": 2}])
    c, srv = make(changeNameServers=ok(), getDomainInfo=info)
    res = c.set_nameservers("good.com", ["ann.ns.cloudflare.com", "BOB.ns.cloudflare.com"])
    p = srv.n("changeNameServers")[0][1]
    assert p["ns1"] == "ann.ns.cloudflare.com" and p["ns2"] == "BOB.ns.cloudflare.com"
    assert res["verified"] is True and res["ok"] is True


def test_set_nameservers_reports_unverified_and_rejects_bad_input(make):
    c, _ = make(changeNameServers=ok(), getDomainInfo=ok(nameservers=[{"nameserver": "NS1.NAMESILO.COM"}]))
    assert c.set_nameservers("good.com", ["a.ns.cloudflare.com", "b.ns.cloudflare.com"])["verified"] is False
    with pytest.raises(NameSiloError):
        c.set_nameservers("good.com", ["only.one.ns"])


def test_set_nameservers_254_is_error_and_250_is_noop(make):
    c, _ = make(changeNameServers=err(254, "bad ns"))
    with pytest.raises(NameSiloError, match="254"):
        c.set_nameservers("good.com", ["a.x", "b.x"])
    c2, _ = make(changeNameServers=err(250, "unchanged"), getDomainInfo=ok(nameservers=[{"nameserver": "A.X"}, {"nameserver": "B.X"}]))
    assert c2.set_nameservers("good.com", ["a.x", "b.x"])["verified"] is True


# --- сверка неизвестного исхода -----------------------------------------------------------------

LONG_AGO = datetime.now(timezone.utc) - timedelta(hours=1)


def test_reconcile_registered_when_domain_active(make):
    c, srv = make(getDomainInfo=ok(status="Active"))
    assert c.reconcile("good.com", datetime.now(timezone.utc), 50.0)[0] == "registered"
    assert [o for o, *_ in srv.calls] == ["getDomainInfo"]


def test_reconcile_too_early_never_concludes_not_registered(make):
    c, _ = make(getDomainInfo=NOT_OURS)
    assert c.reconcile("good.com", datetime.now(timezone.utc) - timedelta(minutes=2), 50.0)[0] == "unknown"


def test_reconcile_not_registered_only_when_no_domain_no_order_and_balance_unchanged(make):
    c, srv = make(getDomainInfo=NOT_OURS, listOrders=ok(), getAccountBalance=ok(balance=50.0))
    v, note = c.reconcile("good.com", LONG_AGO, 50.0)
    assert v == "not_registered" and "новым подтверждением" in note
    assert [o for o, *_ in srv.calls] == ["getDomainInfo", "listOrders", "getAccountBalance"]


def test_reconcile_balance_drop_without_domain_stays_unknown(make):
    c, _ = make(getDomainInfo=NOT_OURS, listOrders=ok(), getAccountBalance=ok(balance=32.71))
    v, note = c.reconcile("good.com", LONG_AGO, 50.0)
    assert v == "unknown" and "баланс изменился" in note


def test_reconcile_paid_order_found_but_domain_not_active_stays_unknown(make):
    c, _ = make(getDomainInfo=NOT_OURS, listOrders=ok(order={"order_number": "77", "total": 17.29}),
                orderDetails=ok(description="good.com - registration", price=17.29, status="Processed"),
                getAccountBalance=ok(balance=50.0))
    v, note = c.reconcile("good.com", LONG_AGO, 50.0)
    assert v == "unknown" and "77" in note


def test_reconcile_without_balance_before_or_on_read_failure_is_unknown(make):
    c, _ = make(getDomainInfo=NOT_OURS, listOrders=ok(), getAccountBalance=ok(balance=50.0))
    assert c.reconcile("good.com", LONG_AGO, None)[0] == "unknown"
    c2, _ = make(getDomainInfo=httpx.Response(500))
    assert c2.reconcile("good.com", LONG_AGO, 50.0)[0] == "unknown"


# --- аукционы ----------------------------------------------------------------------------------

def _lot(name, bid=12.0, aid=None):
    return {"domain": name, "auctionId": aid or hash(name) % 1000, "currentBid": bid,
            "endDate": "2026-10-20 12:00:00", "created": "2015-03-01"}


def test_list_dropping_gives_discovery_rows_with_deadline(make):
    c, srv = make(listAuctions=ok(auctions=[_lot("a.com"), _lot("B.net")]))
    rows = c.list_dropping()
    assert [r["domain"] for r in rows] == ["a.com", "b.net"]
    assert rows[0]["source"] == "namesilo_auction" and rows[0]["lane"] == "bid"
    assert rows[0]["acquire_deadline"] == datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
    p = srv.n("listAuctions")[0][1]
    assert p["typeId"] == "3" and p["statusId"] == "2"


def test_list_dropping_paginates_until_short_page(make):
    full = ok(auctions=[_lot(f"d{i}.com") for i in range(ns.AUCTION_PAGE_SIZE)])
    c, srv = make(listAuctions=[full, ok(auctions=[_lot("tail.com")])])
    assert len(c.list_dropping()) == ns.AUCTION_PAGE_SIZE + 1
    assert len(srv.n("listAuctions")) == 2


def test_auction_rows_without_domain_are_dropped(make):
    c, _ = make(listAuctions=ok(auctions=[{"auctionId": 1}, _lot("ok.com")]))
    assert [r["domain"] for r in c.list_dropping()] == ["ok.com"]


def test_auction_price_is_current_bid(make):
    c, _ = make(listAuctions=ok(auctions=[_lot("a.com", bid=33.5, aid=7)]))
    assert c.price("a.com", auction=True) == registrar.Money(33.5, "USD")


def test_bid_sends_once_with_confirmed_ceiling(make):
    c, srv = make(listAuctions=ok(auctions=[_lot("a.com", bid=20.0, aid=7)]), bidAuction=ok())
    res = c.bid("a.com", 45.0)
    p = srv.n("bidAuction")[0][1]
    # потолок уходит как proxyBid; сама ставка — шаг над текущей (лот без hasBids -> max(opening, current, 1))
    assert p["auctionId"] == "7" and p["proxyBid"] == "45.00" and p["bid"] == "20.00"
    assert res["bid"] == 45.0 and res["bid_now"] == 20.0 and len(srv.n("bidAuction")) == 1


LIVE_AUCTIONS = json.loads((Path(__file__).parent / "fixtures" / "namesilo_list_auctions_live.json").read_text())


def test_live_fixture_parses_body_fields_and_goes_to_public_api(make):
    c, srv = make(listAuctions=LIVE_AUCTIONS)
    lots = c.list_auctions()
    assert [l["domain"] for l in lots] == ["xd9.net", "swaydboots.com", "surronelectricride.com"]
    x = lots[0]
    assert x["auction_id"] == 16970943 and x["bid"] == 1.0 and x["max_bid"] == 1995.0 and x["has_bids"] is True
    assert x["end"] == datetime(2025, 7, 22, 15, 0, tzinfo=timezone.utc)          # auctionEndsOnUtc, не локальное
    assert x["created"] == datetime(2024, 6, 21, 0, 0, tzinfo=timezone.utc)
    req = srv.n("listAuctions")[0][2]
    assert req.url.host == "www.namesilo.com" and req.url.path == "/public/api/listAuctions"
    assert srv.n("listAuctions")[0][1]["pageSize"] == str(ns.AUCTION_PAGE_SIZE)


def test_non_auction_ops_stay_on_base_url(make):
    c, srv = make(getAccountBalance=ok(balance=5.0))
    c.balance()
    assert srv.n("getAccountBalance")[0][2].url.path == "/apibatch/getAccountBalance"


def test_list_dropping_skips_lots_that_already_ended(make, monkeypatch):
    c, _ = make(listAuctions=LIVE_AUCTIONS)
    monkeypatch.setattr(ns, "_utcnow", lambda: datetime(2026, 4, 1, tzinfo=timezone.utc))
    rows = c.list_dropping()
    assert [r["domain"] for r in rows] == ["surronelectricride.com"]
    assert rows[0]["acquire_deadline"] == datetime(2026, 4, 16, 15, 0, tzinfo=timezone.utc)


def test_find_auction_filters_by_domain_name_first(make):
    c, srv = make(listAuctions=LIVE_AUCTIONS)
    a = c.find_auction("Swaydboots.com")
    assert a["auction_id"] == 19016942
    assert srv.n("listAuctions")[0][1]["domainName"] == "swaydboots.com" and len(srv.n("listAuctions")) == 1


def test_bid_refuses_ended_lot_before_sending(make, monkeypatch):
    c, srv = make(listAuctions=LIVE_AUCTIONS, bidAuction=ok())
    monkeypatch.setattr(ns, "_utcnow", lambda: datetime(2026, 10, 10, tzinfo=timezone.utc))   # все лоты фикстуры в прошлом
    with pytest.raises(NameSiloError, match="завершён"):
        c.bid("swaydboots.com", 30.0)
    assert srv.n("bidAuction") == []


def test_bid_steps_over_current_bid_when_lot_has_bids(make, monkeypatch):
    c, srv = make(listAuctions=LIVE_AUCTIONS, bidAuction=ok(body={"auctionId": 19016942, "bid": 2, "proxyBid": 30}))
    monkeypatch.setattr(ns, "_utcnow", lambda: datetime(2026, 3, 1, tzinfo=timezone.utc))      # лот ещё идёт
    res = c.bid("swaydboots.com", 30.0)
    p = [x for x in srv.n("bidAuction")][0][1]
    assert p["bid"] == "2.00" and p["proxyBid"] == "30.00" and res["bid_now"] == 2.0


def test_bid_refuses_when_current_bid_above_confirmed_ceiling(make):
    c, srv = make(listAuctions=ok(auctions=[_lot("a.com", bid=60.0, aid=7)]), bidAuction=ok())
    with pytest.raises(NameSiloError, match="выросла"):
        c.bid("a.com", 45.0)
    assert srv.n("bidAuction") == []


@pytest.mark.parametrize("resp", [httpx.ReadTimeout("t"), httpx.Response(502), err(400), err(999)])
def test_bid_unknown_outcome_is_ambiguous_without_retry(make, resp):
    c, srv = make(listAuctions=ok(auctions=[_lot("a.com", bid=20.0, aid=7)]), bidAuction=resp)
    with pytest.raises(NameSiloAmbiguous):
        c.bid("a.com", 45.0)
    assert len(srv.n("bidAuction")) == 1


def test_bid_on_missing_lot_is_clean_refusal(make):
    c, srv = make(listAuctions=ok(auctions=[_lot("other.com")]), bidAuction=ok())
    with pytest.raises(NameSiloError, match="не найден"):
        c.bid("a.com", 45.0)
    assert srv.n("bidAuction") == []


# --- ключ нигде не светится ---------------------------------------------------------------------

def test_key_is_masked_in_transport_error_text(make):
    def boom(req):
        return httpx.ConnectError(f"cannot connect to {req.url}")      # httpx-ошибки бывают с полным URL
    c, _ = make(getAccountBalance=boom, registerDomain=boom, getDomainInfo=NOT_OURS)
    with pytest.raises(NameSiloReadError) as e1:
        c.balance()
    with pytest.raises(NameSiloAmbiguous) as e2:
        c.register("good.com")
    for e in (e1, e2):
        assert KEY not in str(e.value) and "key=***" in str(e.value)


def test_key_is_masked_in_error_detail(make):
    c, _ = make(getAccountBalance=err(110, f"bad key {KEY}"))
    with pytest.raises(NameSiloAuthError) as ei:
        c.balance()
    assert KEY not in str(ei.value)


def test_httpx_log_filter_masks_key_even_at_info(caplog):
    from app.log_scrub import install
    install()
    lg = logging.getLogger("httpx")
    old = lg.level
    lg.setLevel(logging.INFO)
    try:
        with caplog.at_level(logging.INFO, logger="httpx"):
            lg.info('HTTP Request: %s %s "%s"', "GET",
                    f"https://www.namesilo.com/apibatch/registerDomain?version=1&key={KEY}&domain=a.com", "HTTP/1.1 200 OK")
    finally:
        lg.setLevel(old)
    assert caplog.records and all(KEY not in r.getMessage() for r in caplog.records)
    assert "key=***" in caplog.records[0].getMessage()


def test_diagnostics_scrub_masks_namesilo_key_by_value_and_by_pattern(monkeypatch):
    from app.services.diagnostics import _scrub
    monkeypatch.setattr(settings, "NAMESILO_API_KEY", KEY)
    assert KEY not in _scrub(f"boom {KEY}")
    assert "OLDKEY999" not in _scrub("GET https://x/apibatch/y?version=1&key=OLDKEY999&domain=a.com")


def test_namesilo_key_is_secret_on_keys_screen():
    from app.services.api_keys import GROUPS, SECRET_KEYS
    keys = {f.key: f for _g, _t, _d, fs in GROUPS for f in fs}
    assert {"NAMESILO_API_KEY", "NAMESILO_BASE_URL", "NAMESILO_SANDBOX", "NAMESILO_CONTACT_ID",
            "NAMESILO_ALLOW_PREMIUM"} <= set(keys)
    assert "NAMESILO_API_KEY" in SECRET_KEYS and keys["NAMESILO_API_KEY"].secret


# --- подключение: get_registrar, /diag, discovery -----------------------------------------------

def test_get_registrar_returns_namesilo_only_with_key(monkeypatch):
    assert registrar.get_registrar().configured is False
    monkeypatch.setattr(settings, "NAMESILO_API_KEY", KEY)
    r = registrar.get_registrar()
    assert isinstance(r, NameSiloClient) and r.configured is True and isinstance(r, registrar.Registrar)


def test_diag_namesilo_skips_without_key_and_pings_balance_with_key(monkeypatch):
    from app.services import diagnostics
    row = next(s for s in diagnostics._spec() if s[0] == "namesilo")
    assert row[3] == "" and row[4] == "M2" and row[5] is False
    assert diagnostics._run_one(*row)["status"] == "skip"
    monkeypatch.setattr(settings, "NAMESILO_API_KEY", KEY)
    seen = []
    monkeypatch.setattr(NameSiloClient, "_call", lambda self, op, params=None, **kw: seen.append(op) or {"balance": 3})
    row = next(s for s in diagnostics._spec() if s[0] == "namesilo")
    assert diagnostics._run_one(*row)["status"] == "ok" and seen == ["getAccountBalance"]


def test_discovery_knows_namesilo_auction_source_off_by_default():
    import importlib.util
    from app.services import discovery, scoring_config
    # conftest подменяет SOURCES_ENABLED на офлайн-вариант: настоящий дефолт читаем из файла
    spec = importlib.util.spec_from_file_location("_cfg_real", scoring_config.__file__)
    real = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(real)
    assert "namesilo_auction" in discovery.AUTO_SOURCES
    assert real.SOURCES_ENABLED["namesilo_auction"] is False
    assert discovery._clients()["namesilo_auction"] is NameSiloClient


def test_namesilo_auction_toggle_survives_settings_save():
    from app.services.settings import get_settings, update_settings
    update_settings(sources_enabled={"namesilo_auction": True})
    assert get_settings()["sources_enabled"]["namesilo_auction"] is True
    update_settings(sources_enabled={"dropcatch": False})            # форма без поля = выключено, как у прочих
    assert get_settings()["sources_enabled"]["namesilo_auction"] is False


# --- гейт: канал registrar на настоящем клиенте с мок-транспортом -------------------------------

def _approved(name, source="dropcatch"):
    with db.SessionLocal() as s:
        d = Domain(domain=name, source=source, status="approved", lane="bid")
        s.add(d)
        s.commit()
        s.refresh(d)
        return d.id


def _wire(monkeypatch, make, **handlers):
    from app.services.settings import update_settings
    c, srv = make(**handlers)
    monkeypatch.setattr(registrar, "get_registrar", lambda: c)
    update_settings(zone_channels={"com": "registrar"})
    return c, srv


def _flow(name, source="dropcatch"):
    oid = acquisition.create_order(_approved(name, source))
    acquisition.confirm_order(oid)
    return oid


def _order(oid) -> AcquisitionOrder:
    with db.SessionLocal() as s:
        return s.get(AcquisitionOrder, oid)


GOOD = dict(checkRegisterAvailability=AVAIL, getAccountBalance=ok(balance=100.0))


def test_gate_unconfirmed_order_sends_nothing_to_namesilo(monkeypatch, make):
    c, srv = _wire(monkeypatch, make, getDomainInfo=NOT_OURS, registerDomain=REG_OK, **GOOD)
    oid = acquisition.create_order(_approved("good.com"))
    r = acquisition.execute_confirmed_order(oid)
    assert "gate" in r["error"] and srv.n("registerDomain") == [] and srv.n("getDomainInfo") == []


def test_confirmed_order_registers_once_and_freezes_usd_cost(monkeypatch, make):
    c, srv = _wire(monkeypatch, make, getDomainInfo=NOT_OURS, registerDomain=REG_OK, **GOOD)
    oid = _flow("good.com")
    o = _order(oid)
    assert o.provider == "registrar" and o.cost_currency == "USD" and float(o.cost) == 17.29
    assert acquisition.execute_confirmed_order(oid)["status"] == "caught"     # 300 от registerDomain = куплен
    assert len(srv.n("registerDomain")) == 1 and _order(oid).result["order_amount"] == 17.29


def test_price_rise_after_confirm_blocks_send(monkeypatch, make):
    c, srv = _wire(monkeypatch, make, getDomainInfo=NOT_OURS, registerDomain=REG_OK, **GOOD)
    oid = _flow("good.com")
    srv.handlers["checkRegisterAvailability"] = ok(available={"domain": "good.com", "price": 25.0, "premium": 0, "duration": 1})
    r = acquisition.execute_confirmed_order(oid)
    assert r["status"] == "failed" and "выросла" in r["error"] and srv.n("registerDomain") == []


def test_low_balance_blocks_send(monkeypatch, make):
    c, srv = _wire(monkeypatch, make, getDomainInfo=NOT_OURS, registerDomain=REG_OK,
                   checkRegisterAvailability=AVAIL, getAccountBalance=ok(balance=3.0))
    oid = _flow("good.com")
    r = acquisition.execute_confirmed_order(oid)
    assert r["status"] == "failed" and srv.n("registerDomain") == []


def test_timeout_after_send_sets_maybe_sent_with_reconcile_context(monkeypatch, make):
    c, srv = _wire(monkeypatch, make, getDomainInfo=NOT_OURS, registerDomain=httpx.ReadTimeout("t"), **GOOD)
    oid = _flow("good.com")
    r = acquisition.execute_confirmed_order(oid)
    assert r["status"] == "failed" and r["maybe_sent"] is True
    ctx = _order(oid).result["registrar_ctx"]
    assert ctx["balance_before"] == 100.0 and ctx["sent_at"]
    assert acquisition.cancel_order(oid).get("status") != "cancelled"      # отмена заблокирована


def test_clean_rejection_119_does_not_set_maybe_sent(monkeypatch, make):
    c, srv = _wire(monkeypatch, make, getDomainInfo=NOT_OURS, registerDomain=err(119), **GOOD)
    oid = _flow("good.com")
    r = acquisition.execute_confirmed_order(oid)
    assert r["status"] == "failed" and "maybe_sent" not in _order(oid).result


def _stuck_order(monkeypatch, make, **handlers):
    c, srv = _wire(monkeypatch, make, registerDomain=httpx.ReadTimeout("t"), **GOOD, **{"getDomainInfo": NOT_OURS})
    oid = _flow("good.com")
    acquisition.execute_confirmed_order(oid)
    srv.handlers.update(handlers)
    return oid, c, srv


def _age_ctx(oid, minutes):
    with db.SessionLocal() as s:
        o = s.get(AcquisitionOrder, oid)
        res = dict(o.result)
        res["registrar_ctx"] = {**res["registrar_ctx"],
                                "sent_at": (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()}
        o.result = res
        s.commit()


def test_poll_adopts_order_when_domain_turns_out_registered(monkeypatch, make):
    oid, c, srv = _stuck_order(monkeypatch, make, getDomainInfo=ok(status="Active"))
    sent = len(srv.n("registerDomain"))
    out = acquisition.poll_orders()
    o = _order(oid)
    assert o.status == "caught" and "maybe_sent" not in o.result and out["errors"] == {}
    assert len(srv.n("registerDomain")) == sent      # поллинг ничего не отправляет
    from app.models.site import Site
    with db.SessionLocal() as s:                     # сверка «зарегистрирован» = куплен + карточка сайта
        assert s.get(Domain, o.domain_id).status == "purchased"
        assert s.query(Site).filter_by(domain_id=o.domain_id).count() == 1


def test_poll_verified_not_registered_drops_confirmation_and_unlocks_cancel(monkeypatch, make):
    oid, c, srv = _stuck_order(monkeypatch, make, listOrders=ok())
    _age_ctx(oid, 30)
    n_reg = len(srv.n("registerDomain"))
    acquisition.poll_orders()
    o = _order(oid)
    assert o.status == "failed" and o.confirmed_by_human is False and "maybe_sent" not in o.result
    assert "новым подтверждением" in o.result["error"] and len(srv.n("registerDomain")) == n_reg
    # повтор без нового confirm невозможен
    assert "gate" in acquisition.execute_confirmed_order(oid)["error"]


def test_poll_too_early_keeps_maybe_sent_and_cancel_locked(monkeypatch, make):
    oid, c, srv = _stuck_order(monkeypatch, make, listOrders=ok())
    acquisition.poll_orders()
    o = _order(oid)
    assert o.result["maybe_sent"] is True and o.confirmed_by_human is True


def test_poll_balance_mismatch_keeps_maybe_sent(monkeypatch, make):
    oid, c, srv = _stuck_order(monkeypatch, make, listOrders=ok(), getAccountBalance=ok(balance=82.71))
    _age_ctx(oid, 30)
    acquisition.poll_orders()
    assert _order(oid).result["maybe_sent"] is True


def test_retry_after_ambiguity_adopts_instead_of_double_ordering(monkeypatch, make):
    """«↻ повторить» после обрыва: registerDomain не шлётся второй раз, если домен уже наш."""
    oid, c, srv = _stuck_order(monkeypatch, make, getDomainInfo=ok(status="Active"))
    sent_before = len(srv.n("registerDomain"))
    assert acquisition.execute_confirmed_order(oid)["status"] == "caught"
    assert len(srv.n("registerDomain")) == sent_before


PRICES = ok(com={"registration": "9.0", "transfer": "9.0", "renew": "11.50"})


def test_auction_lot_goes_through_gate_and_bids_with_confirmed_ceiling(monkeypatch, make):
    lots = ok(auctions=[_lot("lot.com", bid=20.0, aid=5)])
    c, srv = _wire(monkeypatch, make, listAuctions=lots, bidAuction=ok(), getAccountBalance=ok(balance=100.0),
                   getPrices=PRICES)
    oid = _flow("lot.com", source="namesilo_auction")
    o = _order(oid)
    # замораживается ПОЛНОЕ списание: потолок (= текущая ставка) + год продления
    assert float(o.cost) == 31.5 and o.cost_currency == "USD"
    assert o.result["auction_ceiling"] == 20.0 and o.result["renew"] == 11.5
    assert srv.n("bidAuction") == [] and srv.n("registerDomain") == []        # до исполнения ничего не ушло
    assert acquisition.execute_confirmed_order(oid)["status"] == "ordered"
    assert len(srv.n("bidAuction")) == 1 and srv.n("registerDomain") == []
    assert srv.n("bidAuction")[0][1]["bid"] == "20.00"


def test_auction_unconfirmed_never_bids(monkeypatch, make):
    lots = ok(auctions=[_lot("lot.com", bid=20.0, aid=5)])
    c, srv = _wire(monkeypatch, make, listAuctions=lots, bidAuction=ok(), getAccountBalance=ok(balance=100.0))
    oid = acquisition.create_order(_approved("lot.com", "namesilo_auction"))
    assert "gate" in acquisition.execute_confirmed_order(oid)["error"] and srv.n("bidAuction") == []


def test_auction_ambiguous_bid_sets_maybe_sent_and_poll_does_not_guess(monkeypatch, make):
    lots = ok(auctions=[_lot("lot.com", bid=20.0, aid=5)])
    c, srv = _wire(monkeypatch, make, listAuctions=lots, bidAuction=httpx.ReadTimeout("t"),
                   getAccountBalance=ok(balance=100.0), getPrices=PRICES)
    oid = _flow("lot.com", source="namesilo_auction")
    assert acquisition.execute_confirmed_order(oid)["maybe_sent"] is True
    _age_ctx(oid, 60)
    acquisition.poll_orders()
    o = _order(oid)
    assert o.result["maybe_sent"] is True and o.status == "failed"


def test_reconcile_context_survives_a_failed_retry(monkeypatch, make):
    """Повтор после обрыва упал чистым отказом (чтение до отправки): maybe_sent и контекст сверки
    (баланс ДО, момент отправки) обязаны выжить — иначе поллинг нечем сверять, а отмена откроется."""
    oid, c, srv = _stuck_order(monkeypatch, make, getDomainInfo=httpx.Response(500))
    r = acquisition.execute_confirmed_order(oid)
    o = _order(oid)
    assert r["status"] == "failed" and o.result["maybe_sent"] is True
    assert o.result["registrar_ctx"]["balance_before"] == 100.0


def test_auction_operator_ceiling_is_frozen_with_renewal(monkeypatch, make):
    """Потолок задаёт человек (bid_rub): гейт замораживает потолок + продление, bid уходит с потолком."""
    lots = ok(auctions=[_lot("lot.com", bid=20.0, aid=5)])
    c, srv = _wire(monkeypatch, make, listAuctions=lots, bidAuction=ok(), getAccountBalance=ok(balance=100.0),
                   getPrices=PRICES)
    oid = acquisition.create_order(_approved("lot.com", "namesilo_auction"))
    r = acquisition.confirm_order(oid, 40.0)
    assert r["bid_rub"] == 51.5 and r["currency"] == "USD"
    assert acquisition.execute_confirmed_order(oid)["status"] == "ordered"
    p = srv.n("bidAuction")[0][1]
    assert p["proxyBid"] == "40.00" and p["bid"] == "20.00"     # потолок = proxyBid, ставка = текущая (нет hasBids)


def test_auction_ceiling_below_current_bid_is_refused_at_gate(monkeypatch, make):
    lots = ok(auctions=[_lot("lot.com", bid=20.0, aid=5)])
    c, srv = _wire(monkeypatch, make, listAuctions=lots, bidAuction=ok(), getPrices=PRICES)
    oid = acquisition.create_order(_approved("lot.com", "namesilo_auction"))
    with pytest.raises(ValueError, match="ниже текущей"):
        acquisition.confirm_order(oid, 10.0)
    assert _order(oid).confirmed_by_human is False


def test_auction_balance_must_cover_ceiling_plus_renewal(monkeypatch, make):
    """Баланса хватает на ставку (20), но не на ставку + продление (31.5): ставка не уходит."""
    lots = ok(auctions=[_lot("lot.com", bid=20.0, aid=5)])
    c, srv = _wire(monkeypatch, make, listAuctions=lots, bidAuction=ok(), getAccountBalance=ok(balance=25.0),
                   getPrices=PRICES)
    oid = _flow("lot.com", source="namesilo_auction")
    r = acquisition.execute_confirmed_order(oid)
    assert r["status"] == "failed" and srv.n("bidAuction") == []


def test_auction_current_bid_above_frozen_ceiling_refuses_before_sending(monkeypatch, make):
    lots_now = ok(auctions=[_lot("lot.com", bid=50.0, aid=5)])
    lots_then = ok(auctions=[_lot("lot.com", bid=20.0, aid=5)])
    c, srv = _wire(monkeypatch, make, listAuctions=[lots_then, lots_now], bidAuction=ok(),
                   getAccountBalance=ok(balance=500.0), getPrices=PRICES)
    oid = _flow("lot.com", source="namesilo_auction")
    r = acquisition.execute_confirmed_order(oid)
    assert r["status"] == "failed" and "выросла" in r["error"] and srv.n("bidAuction") == []


def test_renew_price_missing_is_clean_refusal(make):
    c, _ = make(getPrices=ok(net={"renew": "10"}))
    with pytest.raises(NameSiloError, match="продления"):
        c.renew_price("a.com")


def test_list_dropping_carries_created_and_bid(make):
    c, _ = make(listAuctions=ok(auctions=[_lot("a.com", bid=33.5)]))
    row = c.list_dropping()[0]
    assert row["bid"] == 33.5 and row["created"] == datetime(2015, 3, 1, tzinfo=timezone.utc)


def test_discovery_writes_auction_created_and_bid_to_domain():
    from app.services.discovery import _new_domain
    created = datetime(2015, 3, 1, tzinfo=timezone.utc)
    d = _new_domain("a.com", {"source": "namesilo_auction", "lane": "bid", "created": created, "bid": 33.5}, None)
    assert d.whois_created == created and d.acquire_price == 33.5


def test_auction_current_bid_between_ceiling_and_ceiling_plus_renew_is_refused(monkeypatch, make):
    """Ставка 25 > потолка 20, но < потолок+продление 31.5: сравнивать надо с ПОТОЛКОМ, не с o.cost."""
    lots_then = ok(auctions=[_lot("lot.com", bid=20.0, aid=5)])
    lots_now = ok(auctions=[_lot("lot.com", bid=25.0, aid=5)])
    c, srv = _wire(monkeypatch, make, listAuctions=[lots_then, lots_now], bidAuction=ok(),
                   getAccountBalance=ok(balance=500.0), getPrices=PRICES)
    oid = _flow("lot.com", source="namesilo_auction")
    r = acquisition.execute_confirmed_order(oid)
    assert r["status"] == "failed" and "выросла" in r["error"] and srv.n("bidAuction") == []
    # отказ принял сам execute ДО клиента: клиентский гард bid() (запасной) не добрался бы до третьего
    # чтения лотов — подмена потолка на потолок+продление пропустила бы заказ до него
    assert len(srv.n("listAuctions")) == 2 and "подтверждённых 20.00" in r["error"]


def test_auction_renew_price_drift_at_execute_is_refused(monkeypatch, make):
    """Продление подорожало между подтверждением и отправкой: замороженная сумма больше не верна."""
    pricier = ok(com={"registration": "9.0", "transfer": "9.0", "renew": "14.00"})
    lots = ok(auctions=[_lot("lot.com", bid=20.0, aid=5)])
    c, srv = _wire(monkeypatch, make, listAuctions=lots, bidAuction=ok(), getAccountBalance=ok(balance=500.0),
                   getPrices=[PRICES, pricier])
    oid = _flow("lot.com", source="namesilo_auction")
    r = acquisition.execute_confirmed_order(oid)
    assert r["status"] == "failed" and "продления" in r["error"] and srv.n("bidAuction") == []


def test_queue_auction_row_has_ceiling_field_and_honest_confirm(client, monkeypatch, make):
    """Панель: у лота аукциона — поле потолка (min = текущая ставка, USD), не «цена фиксированная»;
    POST с потолком замораживает потолок + продление."""
    lots = ok(auctions=[_lot("lot.com", bid=20.0, aid=5)])
    c, srv = _wire(monkeypatch, make, listAuctions=lots, bidAuction=ok(), getAccountBalance=ok(balance=100.0),
                   getPrices=PRICES)
    with db.SessionLocal() as s:
        d = Domain(domain="lot.com", source="namesilo_auction", status="approved", lane="bid", acquire_price=20.0)
        s.add(d)
        s.commit()
        did = d.id
    oid = acquisition.create_order(did)
    html = client.get("/queue").text
    assert 'name="bid_rub"' in html and 'min="20.0"' in html and "потолок" in html
    assert "Цена фиксированная" not in html
    r = client.post(f"/queue/{oid}/confirm", data={"bid_rub": "40"}, follow_redirects=False)
    assert r.status_code == 303
    o = _order(oid)
    assert float(o.cost) == 51.5 and o.result["auction_ceiling"] == 40.0
