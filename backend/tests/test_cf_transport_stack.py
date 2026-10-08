"""Транспорт CF через НАСТОЯЩИЙ стек BaseClient (MockTransport на httpx-клиенте): 4xx с телом,
Retry-After, таймауты, статус токена. Старый test_cloudflare_transport подменял `request` на
инстансе и не проходил через raise_for_status/ретрай — S4-06/S4-07 не были прикрыты (S4-13)."""
import httpx
import pytest

from app.integrations.cloudflare import CloudflareClient, CloudflareError


def _client(handler) -> CloudflareClient:
    c = CloudflareClient.with_token("tok", "accHEX")
    c._client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    return c


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    slept = []
    monkeypatch.setattr("tenacity.nap.time.sleep", lambda s: slept.append(s))
    return slept


def test_4xx_keeps_cf_error_body():
    """S4-06: 400 {errors:[{code:1061}]} раньше превращался в «Client error '400 Bad Request»."""
    def h(req):
        return httpx.Response(400, json={"success": False, "errors": [
            {"code": 1061, "message": "The zone already exists"}]})
    with pytest.raises(CloudflareError) as e:
        _client(h).create_zone("a.com")
    assert 1061 in e.value.codes
    assert "1061:The zone already exists" in str(e.value)


def test_ensure_zone_race_1061_refinds_zone():
    """Панель и воркер создали зону одновременно: 1061 = желаемое состояние уже достигнуто."""
    calls = {"find": 0}

    def h(req):
        if req.method == "GET":
            calls["find"] += 1
            res = [] if calls["find"] == 1 else [{"id": "z1", "status": "pending", "name_servers": ["n"]}]
            return httpx.Response(200, json={"success": True, "result": res})
        return httpx.Response(400, json={"success": False, "errors": [{"code": 1061, "message": "exists"}]})
    z = _client(h).ensure_zone("a.com")
    assert z["id"] == "z1" and calls["find"] == 2


def test_find_zone_scoped_to_account():
    seen = {}

    def h(req):
        seen.update(dict(req.url.params))
        return httpx.Response(200, json={"success": True, "result": []})
    _client(h).find_zone("a.com")
    assert seen["account.id"] == "accHEX"


def test_429_short_retry_after_is_respected(_no_sleep):
    n = {"n": 0}

    def h(req):
        n["n"] += 1
        if n["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "7"}, json={"success": False, "errors": []})
        return httpx.Response(200, json={"success": True, "result": {"status": "active"}})
    assert _client(h).ping() is True
    assert n["n"] == 2 and _no_sleep and _no_sleep[0] >= 7      # паузу диктует сервер, а не 1 с


def test_429_long_retry_after_fails_fast(_no_sleep):
    """CF блокирует на окно 5 мин: ретраи 1 с + 2 с только жгут запросы."""
    n = {"n": 0}

    def h(req):
        n["n"] += 1
        return httpx.Response(429, headers={"Retry-After": "300"}, json={"success": False, "errors": []})
    with pytest.raises(CloudflareError) as e:
        _client(h).ping()
    assert n["n"] == 1 and e.value.response.status_code == 429 and not _no_sleep


def test_connect_timeout_shorter_than_read():
    t = CloudflareClient()._client.timeout
    assert t.connect == 5.0 and t.read == 15.0


def test_verify_token_rejects_non_active_status():
    """S4-10: expired/disabled токен отвечает 200 — раньше sync красил connection зелёным."""
    def h(req):
        return httpx.Response(200, json={"success": True, "result": {"status": "expired"}})
    with pytest.raises(RuntimeError, match="expired"):
        _client(h).verify_token("user")


def test_verify_token_active_ok():
    def h(req):
        return httpx.Response(200, json={"success": True, "result": {"status": "active", "id": "t"}})
    assert _client(h).verify_token("user")["status"] == "active"
