"""G4 (S1-06): предохранитель RDAP — по зоне; 429 уважается (Retry-After/интервал зоны) и не
считается падением канала."""
import httpx
import pytest

from app.integrations import rdap as rdap_mod
from app.integrations.rdap import RdapClient, RdapThrottled
from app.services import whois


def _status(code, headers=None):
    req = httpx.Request("GET", "https://rdap/x")
    return httpx.HTTPStatusError("e", request=req, response=httpx.Response(code, headers=headers, request=req))


class _Clock:
    def __init__(self):
        self.t, self.slept = 100.0, []

    def now(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


@pytest.fixture
def clk(monkeypatch, real_rdap_bootstrap):
    c = _Clock()
    monkeypatch.setattr(rdap_mod, "_clock", c.now)
    monkeypatch.setattr(rdap_mod, "_sleep", c.sleep)
    return c


def _client(outcomes):
    c = RdapClient()
    c._servers = {"nl": "https://rdap.sidn.nl/", "com": "https://rdap.verisign.com/com/v1/"}
    it, calls = iter(outcomes), []

    def req(method, url, **kw):
        calls.append(url)
        o = next(it)
        if isinstance(o, Exception):
            raise o
        return o
    c.request = req
    return c, calls


_NONE = httpx.Response(200, json={"status": [], "events": []})


def test_breaker_is_per_zone():
    """Три сбоя .nl подряд открывают предохранитель .nl, но не .com."""
    class R:
        def __init__(self):
            self.n = 0

        def has_rdap(self, d):
            return True

        def lookup(self, d):
            if d.endswith(".nl"):
                raise OSError("boom")
            self.n += 1
            return {"exists": False, "status": [], "registered_at": None}
    r = R()
    for i in range(3):
        with pytest.raises(OSError):
            whois.probe(f"x{i}.nl", {"rdap": r, "aparser": None})
    with pytest.raises(whois.CircuitOpen):
        whois.probe("y.nl", {"rdap": r, "aparser": None})
    assert whois.probe("ok.com", {"rdap": r, "aparser": None})["available"] is True      # .com жив


def test_throttled_does_not_open_breaker():
    class R:
        def has_rdap(self, d):
            return True

        def lookup(self, d):
            raise RdapThrottled("429")
    r = R()
    for i in range(10):
        with pytest.raises(RdapThrottled):
            whois.probe(f"x{i}.nl", {"rdap": r, "aparser": None})
    assert getattr(r, "lookup_failures_nl", 0) == 0


def test_429_waits_retry_after_and_retries_once(clk):
    c, calls = _client([_status(429, {"Retry-After": "4"}), _NONE])
    assert c.lookup("a.nl")["exists"] is True
    assert len(calls) == 2 and sum(clk.slept) >= 4


def test_429_without_retry_after_uses_default_and_raises_on_second(clk):
    c, calls = _client([_status(429), _status(429)])
    with pytest.raises(RdapThrottled):
        c.lookup("a.nl")
    assert len(calls) == 2 and sum(clk.slept) >= rdap_mod._COOLDOWN_DEFAULT


def test_long_retry_after_is_refused_not_slept(clk):
    c, calls = _client([_status(429, {"Retry-After": "300"})])
    with pytest.raises(RdapThrottled):
        c.lookup("a.nl")
    assert len(calls) == 1 and sum(clk.slept) == 0
    with pytest.raises(RdapThrottled):          # сосед по зоне видит cooldown и не бьёт сервер
        c.lookup("b.nl")
    assert len(calls) == 1


def test_zone_interval_spaces_nl_but_not_com(clk):
    c, _ = _client([_NONE] * 6)
    for i in range(3):
        c.lookup(f"x{i}.nl")
    assert sum(clk.slept) >= 2 * rdap_mod._ZONE_INTERVAL["nl"] - 1e-9
    clk.slept.clear()
    for i in range(3):
        c.lookup(f"x{i}.com")
    assert clk.slept == []


def test_404_is_free_and_5xx_propagates(clk):
    c, _ = _client([_status(404)])
    assert c.lookup("free.nl")["exists"] is False
    c, _ = _client([_status(503)])
    with pytest.raises(httpx.HTTPStatusError):
        c.lookup("x.com")


def test_request_does_not_retry_429_at_transport_level():
    c = RdapClient()
    calls = []

    def once(method, url, **kw):
        calls.append(1)
        raise _status(429)
    c._request_once = once
    with pytest.raises(httpx.HTTPStatusError):
        c.request("GET", "https://rdap/x")
    assert len(calls) == 1
