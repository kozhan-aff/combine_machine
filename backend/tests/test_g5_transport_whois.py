"""G5: whois:43 для зон без RDAP (S1-03, S2-02, F8-05), транспорт BaseClient (F8-06, F8-14, S4-07/08,
S5-11), A-Parser whois (S2-12), кэш отказа Spamhaus (F8-17), троттлинг jobs (F8-15)."""
import threading
import time
from types import SimpleNamespace

import httpx
import pytest

from app.integrations import base, blacklist, whois43
from app.integrations.aparser import AParserClient, _parse_whois_available
from app.services import jobs, whois


# ---------- whois:43: разбор и маршрутизация ----------

MX_FREE = "Whois Server Version 2.0\r\nNo_Se_Encontro_El_Objeto\r\n"
MX_TAKEN = "Domain Name:      example.mx\r\nCreated On:       2005-10-24\r\nRegistrar: X\r\n"


def test_whois43_parse_mx_free_taken_and_unclear():
    assert whois43.parse("mx", MX_FREE) == {"available": True, "created": None}
    r = whois43.parse("mx", MX_TAKEN)
    assert r["available"] is False and r["created"].year == 2005
    # шапка/лимит запросов без даты и без маркера — НЕ «занят» (S2-02): не определилось
    assert whois43.parse("mx", "Rate limit exceeded, try later") == {"available": None, "created": None}


class _Direct:
    def __init__(self, out):
        self.out, self.calls, self.whois43_failures = out, [], 0

    def whois_probe(self, d):
        self.calls.append(d)
        if self.out == "boom":
            raise OSError("timed out")
        return self.out


class _Ap:
    def __init__(self, out):
        self.out, self.calls = out, []

    def whois_probe(self, d):
        self.calls.append(d)
        return self.out


def test_probe_mx_uses_direct_whois_first_and_skips_aparser():
    ap, w = _Ap({"available": False, "created": None}), _Direct({"available": True, "created": None})
    r = whois.probe("x.mx", {"rdap": None, "aparser": ap, "whois43": w})
    assert r["available"] is True and r["whois_source"] == "whois43" and ap.calls == []


def test_probe_mx_falls_back_to_aparser_on_direct_failure_or_unclear():
    for direct in ("boom", {"available": None, "created": None}):
        ap = _Ap({"available": True, "created": None})
        r = whois.probe("x.mx", {"rdap": None, "aparser": ap, "whois43": _Direct(direct)})
        assert r["whois_source"] == "aparser" and r["available"] is True and ap.calls == ["x.mx"]


def test_probe_direct_breaker_opens_then_aparser_only():
    w, ap = _Direct("boom"), _Ap({"available": True, "created": None})
    c = {"rdap": None, "aparser": ap, "whois43": w}
    for i in range(4):
        whois.probe(f"d{i}.mx", c)
    assert len(w.calls) == 3 and len(ap.calls) == 4     # 4-й домен прямой канал уже не дёргал


def test_probe_zone_not_in_table_goes_to_aparser():
    w, ap = _Direct({"available": True, "created": None}), _Ap({"available": True, "created": None})
    whois.probe("x.fr", {"rdap": None, "aparser": ap, "whois43": w})
    assert w.calls == [] and ap.calls == ["x.fr"]


def test_whois43_client_speaks_port_43(monkeypatch):
    sent = []

    class Sock:
        def __init__(self): self.chunks = [MX_FREE.encode(), b""]
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def settimeout(self, t): pass
        def sendall(self, b): sent.append(b)
        def recv(self, n): return self.chunks.pop(0)

    seen = {}

    def conn(addr, timeout):
        seen["addr"], seen["timeout"] = addr, timeout
        return Sock()
    monkeypatch.setattr(whois43.socket, "create_connection", conn)
    r = whois43.Whois43Client(timeout=4).whois_probe("free.mx")
    assert r["available"] is True and sent == [b"free.mx\r\n"]
    assert seen == {"addr": ("whois.mx", 43), "timeout": 4}


# ---------- A-Parser: registered:1 без даты — не «занят» (S2-02, F8-05) ----------

def test_aparser_registered_without_creation_is_undetermined():
    assert _parse_whois_available("x.com - registered: 1, expire: none, creation: none") is None
    assert _parse_whois_available("x.com - registered: 1, expire: none, creation: 13.01.2001") is False
    assert _parse_whois_available("x.com - registered: 0, expire: none, creation: none") is True


def test_aparser_whois_single_attempt_long_timeout(monkeypatch):
    """S2-12: whois — одна попытка (не ретрай BaseClient) с 60-секундным read-таймаутом."""
    c = AParserClient()
    seen = {}

    def once(method, url, **kw):
        seen.update(kw)
        return SimpleNamespace(json=lambda: {"success": 1, "data": {"resultString": ""}})
    c._request_once = once
    c.request = lambda *a, **k: pytest.fail("whois ушёл в ретрайный request")
    c.whois_probe("x.mx")
    assert seen["timeout"] == 60.0


def test_whois_sem_limits_aparser_concurrency():
    peak, cur, lock = [0], [0], threading.Lock()

    class Slow:
        whois_failures = 0

        def whois_probe(self, d):
            with lock:
                cur[0] += 1
                peak[0] = max(peak[0], cur[0])
            time.sleep(0.05)
            with lock:
                cur[0] -= 1
            return {"available": True, "created": None}
    c = {"rdap": None, "aparser": Slow(), "_whois_sem": threading.BoundedSemaphore(2)}
    ts = [threading.Thread(target=whois.probe, args=(f"d{i}.mx", c)) for i in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert peak[0] == 2


def test_make_clients_whois_concurrency_from_settings(monkeypatch):
    from app.config import settings
    from app.services import scoring
    monkeypatch.setattr(settings, "WHOIS_APARSER_CONCURRENCY", 5)
    sem = scoring._make_clients()["_whois_sem"]
    assert [sem.acquire(blocking=False) for _ in range(6)].count(True) == 5


# ---------- BaseClient: таймауты, retry по методу, Retry-After, UA, пул ----------

def _bc(handler, cls=base.BaseClient):
    c = cls("http://x")
    c._client = httpx.Client(transport=httpx.MockTransport(handler))
    return c


@pytest.fixture
def no_sleep(monkeypatch):
    slept = []
    monkeypatch.setattr("tenacity.nap.time.sleep", lambda s: slept.append(s))
    return slept


def test_connect_timeout_is_short():
    t = base.BaseClient("http://x", timeout=30.0)._client.timeout
    assert t.connect == base.CONNECT_TIMEOUT and t.read == 30.0
    assert base.BaseClient("http://x", timeout=3.0)._client.timeout.connect == 3.0


def test_post_is_not_retried_by_default_but_get_is(no_sleep):
    n = {"n": 0}

    def h(req):
        n["n"] += 1
        return httpx.Response(503)
    c = _bc(h)
    with pytest.raises(httpx.HTTPStatusError):
        c.request("POST", "http://x/pay")
    assert n["n"] == 1
    n["n"] = 0
    with pytest.raises(httpx.HTTPStatusError):
        c.request("GET", "http://x/read")
    assert n["n"] == 3
    n["n"] = 0
    with pytest.raises(httpx.HTTPStatusError):
        c.request("POST", "http://x/read-only", retry=True)
    assert n["n"] == 3


def test_503_retry_after_respected_and_long_fails_fast(no_sleep):
    n = {"n": 0}

    def h(req):
        n["n"] += 1
        if n["n"] == 1:
            return httpx.Response(503, headers={"Retry-After": "9"})
        return httpx.Response(200)
    assert _bc(h).request("GET", "http://x/").status_code == 200
    assert no_sleep and no_sleep[0] >= 9
    n["n"], no_sleep[:] = 0, []

    def h2(req):
        n["n"] += 1
        return httpx.Response(503, headers={"Retry-After": "600"})
    with pytest.raises(httpx.HTTPStatusError):
        _bc(h2).request("GET", "http://x/")
    assert n["n"] == 1 and not no_sleep


def test_contact_user_agent(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "CONTACT_EMAIL", "ops@example.org")
    seen = {}

    def h(req):
        seen["ua"] = req.headers["user-agent"]
        return httpx.Response(200)
    c = base.BaseClient("http://x")
    c._client = httpx.Client(transport=httpx.MockTransport(h), headers={"User-Agent": base._user_agent()})
    c.request("GET", "http://x/")
    assert seen["ua"] == "combine-machine/2.0 (+mailto:ops@example.org)"


def test_paid_llm_post_not_retried(no_sleep):
    from app.integrations.llm import LlmClient
    n = {"n": 0}

    def h(req):
        n["n"] += 1
        return httpx.Response(502)
    c = LlmClient()
    c._client = httpx.Client(transport=httpx.MockTransport(h))
    with pytest.raises(httpx.HTTPStatusError):
        c.complete("s", "p")
    assert n["n"] == 1


def test_cf_create_zone_not_retried_but_patch_is(no_sleep):
    from app.integrations.cloudflare import CloudflareClient, CloudflareError
    calls = []

    def h(req):
        calls.append(req.method)
        return httpx.Response(500, json={"success": False, "errors": []})
    c = CloudflareClient.with_token("t", "a")
    c._client = httpx.Client(transport=httpx.MockTransport(h))
    with pytest.raises(CloudflareError):
        c.create_zone("a.com")
    assert calls == ["POST"]
    calls.clear()
    with pytest.raises(CloudflareError):
        c.set_ssl("z1")
    assert calls == ["PATCH"] * 3
    calls.clear()
    with pytest.raises(CloudflareError):
        c.add_a_record("z1", "a.com", "1.2.3.4")      # POST создаёт запись — повтор после обрыва не идемпотентен
    assert calls == ["POST"]
    calls.clear()
    with pytest.raises(CloudflareError):
        c.update_a_record("z1", "r1", "a.com", "1.2.3.4")
    assert calls == ["PATCH"] * 3


def test_whois43_de_not_claimed():
    """DENIC не отдаёт дату регистрации (`Changed:` — правка записи) — зона не должна давать created."""
    assert not whois43.has_whois43("example.de")


def test_pooled_client_reused_and_reopened_on_url_change():
    class P(base.BaseClient):
        POOLED = True
    base.close_pool()
    a, b = P("http://one"), P("http://one")
    assert a._client is b._client
    c = P("http://two")                       # api_keys-override сменил URL на лету
    assert c._client is not a._client
    assert not a._client.is_closed            # соседние потоки со старым клиентом не ломаем
    base.close_pool()
    assert a._client.is_closed
    assert P("http://one")._client is not a._client     # закрытый пул даёт свежий клиент


def test_pool_is_bounded():
    class P(base.BaseClient):
        POOLED = True
    base.close_pool()
    for i in range(base._POOL_MAX + 5):
        P(f"http://h{i}")
    assert len(base._POOL) == base._POOL_MAX
    base.close_pool()


# ---------- Spamhaus: кэш отказа (F8-17) ----------

def test_blacklist_negative_cache_stops_resolver_pileup(monkeypatch):
    blacklist.BlacklistClient._control_ok = None
    blacklist.BlacklistClient._control_failed = None
    c = blacklist.BlacklistClient()
    calls = {"n": 0}

    def resolve(host):
        calls["n"] += 1
        return None
    monkeypatch.setattr(c, "_resolve", resolve)
    for _ in range(5):
        with pytest.raises(RuntimeError):
            c._ensure_control()
    assert calls["n"] == 1                       # остальные 4 потока не ждут таймаута резолвера
    # по истечении окна — снова реальная попытка (транзиент не вечен, S14)
    blacklist.BlacklistClient._control_failed = (time.monotonic() - blacklist.NEGATIVE_TTL - 1,
                                                 blacklist.BlacklistClient._control_failed[1])
    with pytest.raises(RuntimeError):
        c._ensure_control()
    assert calls["n"] == 2
    blacklist.BlacklistClient._control_failed = None


# ---------- jobs: троттлинг и лок (F8-15) ----------

def _run_id():
    return jobs._open("g5job", "manual", None)


def test_report_ticks_are_throttled_but_stage_message_and_final_pass(monkeypatch):
    from app.db import SessionLocal
    from app.models.job import JobRun
    rid = _run_id()
    t = [100.0]
    monkeypatch.setattr(jobs, "_clock", lambda: t[0])

    def done():
        with SessionLocal() as db:
            return db.get(JobRun, rid).done
    jobs.report(rid, done=1, total=10)
    jobs.report(rid, done=2, total=10)            # в том же окне — отброшен
    assert done() == 1
    jobs.report(rid, done=3, total=10, message="волна")      # message не троттлится
    assert done() == 3
    jobs.report(rid, done=4, total=10)
    assert done() == 3
    jobs.report(rid, done=10, total=10)           # финальный тик проходит всегда
    assert done() == 10
    t[0] += 1.0
    jobs.report(rid, done=5, total=10)            # окно истекло
    assert done() == 5


def test_cancelled_cached_flag_but_request_cancel_is_immediate():
    rid = _run_id()
    assert jobs.cancelled(rid, cached=True) is False
    jobs.request_cancel("g5job")
    assert jobs.cancelled(rid, cached=True) is True       # сброс кэша стопом


def test_db_lock_is_noop_off_sqlite(monkeypatch):
    lock = jobs._DbLock()

    def other_thread_can_take() -> bool:
        got = []

        def probe():
            ok = lock._rl.acquire(timeout=0.1)
            got.append(ok)
            if ok:
                lock._rl.release()
        t = threading.Thread(target=probe)
        t.start(); t.join()
        return got[0]
    monkeypatch.setattr(lock, "_sqlite", staticmethod(lambda: False))
    with lock:
        assert other_thread_can_take()                    # на PostgreSQL лок не берётся
    monkeypatch.setattr(lock, "_sqlite", staticmethod(lambda: True))
    with lock:
        assert not other_thread_can_take()                # на sqlite (харнесс) — взят
    assert other_thread_can_take()                        # и отпущен на выходе
