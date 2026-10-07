"""Группа G1 аудита 2026-10-07: aaPanel-коннект и честная /diag.

S5-01/S7-02/F8-01 — предохранитель авторизации aaPanel (пауза после отказа, чтобы фон не копил
неудачи до бана IP на час); S5-02 — ping() называет причину; S5-10 — таймауты и записи без слепого
ретрая; S5-17 — HTML вместо JSON; S5-11 — без лишнего клиента; S7-13/F8-08/S1-12 — /diag из кэша
+ single-flight; S2-01/S6-01/S7-01 — LLM-проба настоящим completion; S6-09/S7-04/F8-03 — SearXNG
судит по выдаче; F8-18 — Cloudflare показывает права.

Сеть везде подменена: `_client.request` на экземпляре (как в test_aapanel_errors.py)."""
import threading
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest

import app.db as db
from app.config import settings
from app.integrations import aapanel
from app.integrations.aapanel import AaPanelBlocked, AaPanelClient
from app.models.domain import Domain
from app.models.site import Page, Site
from app.services import diag_cache, diagnostics

_real_sleep = time.sleep   # фикстура ниже глушит time.sleep ради tenacity; тестам с потоками он нужен настоящий

IP_FAIL = {"status": False, "msg": "IP validation failed, your access IP is[203.0.113.7]"}
BAN = {"status": False, "msg": "20 consecutive verification failures, prohibited for 1 hour"}
KEY_FAIL = {"status": False, "msg": "Secret key verification failed"}
LIST_EMPTY = {"data": [], "where": "type_id=0", "page": ""}


@pytest.fixture(autouse=True)
def _loopback_panel(monkeypatch):
    monkeypatch.setattr(settings, "AAPANEL_URL", "https://127.0.0.1:8888")
    monkeypatch.setattr(settings, "AAPANEL_CA_BUNDLE", "")
    monkeypatch.setattr(settings, "AAPANEL_API_KEY", "testsk")
    monkeypatch.setattr("time.sleep", lambda s: None)   # tenacity-backoff не тормозит тест


class _Panel:
    """HTTP-уровень панели: body|исключение по фрагменту action; считает запросы."""

    def __init__(self, **routes):
        self.routes = routes
        self.calls: list[str] = []

    def request(self, method, url, **kw):
        self.calls.append(url)
        for frag, v in self.routes.items():
            if frag in url:
                if isinstance(v, BaseException):
                    raise v
                if callable(v):
                    v = v()
                if isinstance(v, httpx.Response):
                    return v
                return httpx.Response(200, json=v, request=httpx.Request(method, url))
        raise AssertionError(f"не ждали: {url}")   # pragma: no cover

    def close(self):
        pass

    def n(self, frag):
        return sum(1 for u in self.calls if frag in u)


def _client(panel):
    c = AaPanelClient()
    c._client = panel
    return c


# ============================ 1. предохранитель aaPanel ============================

def test_ping_names_the_reason_instead_of_bare_fail():
    """S5-02: раньше ping() возвращал False и /diag показывал fail без текста."""
    with pytest.raises(RuntimeError, match=r"IP validation failed.*203\.0\.113\.7"):
        _client(_Panel(GetTaskCount=IP_FAIL)).ping()


def test_ping_reports_unreachable_panel_with_exception_type():
    with pytest.raises(RuntimeError, match="aaPanel недоступна: ConnectError"):
        _client(_Panel(GetTaskCount=httpx.ConnectError("refused"))).ping()


def test_ping_still_true_on_bare_int_and_ok_dict():
    assert _client(_Panel(GetTaskCount=0)).ping() is True
    assert _client(_Panel(GetTaskCount={"status": True})).ping() is True


def test_auth_refusal_pauses_panel_no_network_until_pause_ends():
    """S5-01/S7-02/F8-01: после «IP validation failed» следующий вызов в сеть НЕ уходит —
    иначе фон копил бы неудачи до бана на час."""
    p = _Panel(GetTaskCount=IP_FAIL, getData=LIST_EMPTY)
    c = _client(p)
    with pytest.raises(RuntimeError):
        c.ping()
    assert p.n("GetTaskCount") == 1
    with pytest.raises(AaPanelBlocked, match=r"IP validation failed.*whitelist.*пауза до \d\d:\d\d UTC"):
        c.ping()
    with pytest.raises(AaPanelBlocked):
        c.list_sites()                      # любой метод клиента, не только ping
    assert len(p.calls) == 1, p.calls        # ни одного нового запроса в панель


def test_ban_message_gets_hour_pause_and_plain_auth_fail_gets_short_one():
    with pytest.raises(RuntimeError):
        _client(_Panel(GetTaskCount=BAN)).ping()
    assert 3000 < aapanel._block["until"] - time.monotonic() <= aapanel.BAN_PAUSE_SEC
    aapanel.reset_block()
    with pytest.raises(RuntimeError):
        _client(_Panel(GetTaskCount=KEY_FAIL)).ping()
    assert 600 < aapanel._block["until"] - time.monotonic() <= aapanel.AUTH_PAUSE_SEC


def test_pause_expires_and_panel_is_asked_again():
    p = _Panel(GetTaskCount=0)
    aapanel._block.update(until=time.monotonic() - 1, reason="IP validation failed",
                          fp=aapanel._fingerprint())
    assert _client(p).ping() is True        # пауза истекла -> запрос ушёл
    assert aapanel.blocked_reason() is None and p.n("GetTaskCount") == 1


def test_changing_the_key_lifts_the_pause(monkeypatch):
    """Оператор сменил api_sk на «Ключи и сервисы» — ждать 15 минут по старому ключу бессмысленно."""
    with pytest.raises(RuntimeError):
        _client(_Panel(GetTaskCount=KEY_FAIL)).ping()
    assert aapanel.blocked_reason()
    monkeypatch.setattr(settings, "AAPANEL_API_KEY", "newsk")
    assert aapanel.blocked_reason() is None


def test_non_auth_refusals_do_not_pause():
    """«Requested file exists!» и «сайт уже есть» — не отказ авторизации, счётчик бана не копят."""
    p = _Panel(CreateFile={"status": False, "msg": "Requested file exists!"},
               SaveFileBody={"status": True}, AddSite={"status": False, "msg": "网站已存在"})
    c = _client(p)
    c.write_file("/www/x/index.html", "x")
    with pytest.raises(RuntimeError):
        c.add_site("ex.ru", "/www/x")
    assert aapanel.blocked_reason() is None


def test_write_stops_after_first_auth_refusal():
    """CreateFile получил отказ авторизации -> SaveFileBody не шлём (второе очко в счётчик бана)."""
    p = _Panel(CreateFile=KEY_FAIL, SaveFileBody={"status": True})
    with pytest.raises(AaPanelBlocked):
        _client(p).write_file("/www/x/index.html", "x")
    assert p.n("SaveFileBody") == 0


def _seed_site(page_statuses=()) -> int:
    with db.SessionLocal() as s:
        d = Domain(domain="ex.com", source="dropcatch", status="purchased")
        s.add(d)
        s.commit()
        site = Site(domain_id=d.id, status="provisioning", doc_root="/www/wwwroot/ex.com")
        s.add(site)
        s.commit()
        for i, st in enumerate(page_statuses):
            s.add(Page(site_id=site.id, url_path="/" if i == 0 else f"/p{i}", title="t",
                       status=st, body="<p>x</p>"))
        s.commit()
        return site.id


class _FakeCF:
    """Cloudflare без сети: зона pending/active по флагу класса; считает шаги."""
    status = "pending"
    steps: list = []

    def ensure_zone(self, domain):
        self.steps.append("ensure_zone")
        return {"id": "z1", "status": self.status, "name_servers": ["a.ns.cf", "b.ns.cf"]}

    def get_zone(self, zone_id):
        return {"id": zone_id, "status": self.status, "name_servers": ["a.ns.cf", "b.ns.cf"]}

    def ensure_a_record(self, *a, **kw):
        self.steps.append("a_record")

    def get_zone_setting(self, *a, **kw):
        return {"value": "full"}


def _pause_panel(monkeypatch, status):
    _FakeCF.status, _FakeCF.steps = status, []
    monkeypatch.setattr("app.integrations.cloudflare.CloudflareClient", _FakeCF)
    monkeypatch.setattr(settings, "VPS_ORIGIN_IP", "203.0.113.9")
    aapanel._block.update(until=time.monotonic() + 600, reason="IP validation failed",
                          fp=aapanel._fingerprint())


def test_provision_paused_panel_still_returns_ns_for_pending_zone(monkeypatch):
    """S5-01: пауза панели не отнимает у оператора NS — зона создаётся, возвращается awaiting_ns,
    в панель не уходит ни одного запроса (шаг NS самый долгий и от панели не зависит)."""
    from app.services import provisioning
    _pause_panel(monkeypatch, "pending")
    panel = _Panel()
    monkeypatch.setattr(AaPanelClient, "_request_once", lambda self, *a, **kw: panel.request(*a, **kw))
    out = provisioning.provision(_seed_site())
    assert out["status"] == "awaiting_ns" and out["name_servers"] == ["a.ns.cf", "b.ns.cf"]
    assert _FakeCF.steps == ["ensure_zone"] and panel.calls == []


def test_provision_paused_panel_stops_before_any_panel_request_on_active_zone(monkeypatch):
    """S5-01: зона active -> CF-шаги прошли, а на панели провижн падает AaPanelBlocked ДО сети."""
    from app.services import provisioning
    _pause_panel(monkeypatch, "active")
    panel = _Panel()
    monkeypatch.setattr(AaPanelClient, "_request_once", lambda self, *a, **kw: panel.request(*a, **kw))
    sid = _seed_site()
    with pytest.raises(AaPanelBlocked):
        provisioning.provision(sid)
    assert _FakeCF.steps == ["ensure_zone", "a_record"] and panel.calls == []
    with db.SessionLocal() as s:
        assert s.get(Site, sid).status == "provisioning"


def test_publish_stops_when_panel_paused_and_keeps_pages_edited():
    from app.services import publish
    sid = _seed_site(page_statuses=("edited",))
    aapanel._block.update(until=time.monotonic() + 600, reason="IP validation failed",
                          fp=aapanel._fingerprint())
    with pytest.raises(AaPanelBlocked):
        publish.publish_site(sid)
    with db.SessionLocal() as s:
        assert s.query(Page).filter_by(site_id=sid).one().status == "edited"


def test_publish_gate_message_survives_paused_panel():
    """Гейт редактуры отвечает раньше паузы: нет edited-страниц -> no_edited_pages, не AaPanelBlocked."""
    from app.services import publish
    aapanel._block.update(until=time.monotonic() + 600, reason="x", fp=aapanel._fingerprint())
    assert publish.publish_site(_seed_site(page_statuses=("draft",)))["status"] == "no_edited_pages"


# ============================ 2. таймауты, ретраи, не-JSON ============================

def test_connect_timeout_is_separate_and_short():
    t = AaPanelClient()._client.timeout
    assert t.connect == 10.0 and t.read == 30.0


def test_write_is_not_retried_on_read_timeout():
    """S5-10: запрос AddSite мог дойти и исполниться — повтор создал бы дубль/гонку."""
    p = _Panel(AddSite=httpx.ReadTimeout("slow"))
    with pytest.raises(httpx.ReadTimeout):
        _client(p).add_site("ex.com", "/www/x")
    assert p.n("AddSite") == 1


def test_write_is_retried_on_connect_error_but_read_timeout_of_a_read_is_retried_too():
    """Сбой СОЕДИНЕНИЯ — запрос не уходил, повтор безопасен; чтение (getData) ретраится как раньше."""
    p = _Panel(AddSite=httpx.ConnectTimeout("no route"))
    with pytest.raises(httpx.ConnectTimeout):
        _client(p).add_site("ex.com", "/www/x")
    assert p.n("AddSite") == 3
    p = _Panel(getData=httpx.ReadTimeout("slow"))
    with pytest.raises(httpx.ReadTimeout):
        _client(p).list_sites()
    assert p.n("getData") == 3


def test_ping_is_single_attempt():
    p = _Panel(GetTaskCount=httpx.ConnectTimeout("no route"))
    with pytest.raises(RuntimeError):
        _client(p).ping()
    assert p.n("GetTaskCount") == 1


def test_ensure_site_after_read_timeout_checks_panel_and_accepts_existing():
    """AddSite ушёл в ReadTimeout, но панель его исполнила: ensure_site спрашивает список и считает
    желаемое состояние достигнутым, а не падает и не создаёт второй раз."""
    state = {"added": False}

    def _list():
        return {"data": [{"id": 1, "name": "ex.com"}]} if state["added"] else LIST_EMPTY

    def _add():
        state["added"] = True
        raise httpx.ReadTimeout("slow")

    p = _Panel(getData=_list, AddSite=_add)
    assert _client(p).ensure_site("ex.com", "/www/x") == {"exists": True, "name": "ex.com"}
    assert p.n("AddSite") == 1


def test_ensure_site_after_read_timeout_reraises_when_site_not_created():
    p = _Panel(getData=LIST_EMPTY, AddSite=httpx.ReadTimeout("slow"))
    with pytest.raises(httpx.ReadTimeout):
        _client(p).ensure_site("ex.com", "/www/x")


def test_delete_site_after_timeout_verifies_via_list():
    gone = _Panel(DeleteSite=httpx.ReadTimeout("slow"), getData=LIST_EMPTY)
    assert _client(gone).delete_site("ex.com", 7)["status"] is True
    assert gone.n("DeleteSite") == 1
    still = _Panel(DeleteSite=httpx.ReadTimeout("slow"),
                   getData={"data": [{"id": 7, "name": "ex.com"}]})
    with pytest.raises(httpx.ReadTimeout):
        _client(still).delete_site("ex.com", 7)


def test_html_instead_of_json_gives_readable_error():
    """S5-17: HTML-страница (security entrance/API выключен) раньше давала 'Expecting value: line 1'."""
    html = httpx.Response(200, text="<html><body>  Please   login </body></html>",
                          request=httpx.Request("POST", "https://x/"))
    with pytest.raises(RuntimeError, match=r"ответ не JSON \(HTTP 200\).*Please login"):
        _client(_Panel(GetTaskCount=html)).ping()


def test_empty_url_has_its_own_message(monkeypatch):
    monkeypatch.setattr(settings, "AAPANEL_URL", "")
    with pytest.raises(RuntimeError, match="AAPANEL_URL не задан"):
        AaPanelClient()


# ============================ 3. /diag: кэш, single-flight ============================

def _row(key, status="ok"):
    return {"key": key, "label": key, "status": status, "role": "", "module": "M1",
            "critical": False, "ms": 1, "error": None}


def test_get_is_cache_after_first_run_and_does_not_run_diagnostics(monkeypatch):
    n = {"c": 0}

    def fake():
        n["c"] += 1
        return [_row("a")]
    monkeypatch.setattr(diag_cache, "run_diagnostics", fake)
    c1, at1 = diag_cache.get()          # холодный старт: один живой прогон
    c2, at2 = diag_cache.get()
    assert n["c"] == 1 and c1 == c2 and at1 == at2


def test_stale_cache_is_served_instantly_and_refreshed_in_background(monkeypatch):
    started = []

    class _T:
        def __init__(self, target=None, **kw):
            started.append(target)

        def start(self):
            pass
    monkeypatch.setattr(diag_cache.threading, "Thread", _T)
    diag_cache._checks = [_row("old")]
    diag_cache._checked_at = datetime.now(timezone.utc) - timedelta(seconds=3 * diag_cache.REFRESH_SEC)
    monkeypatch.setattr(diag_cache, "run_diagnostics",
                        lambda: pytest.fail("страница не должна ждать живого прогона"))
    checks, _ = diag_cache.get()
    assert checks == [_row("old")] and started == [diag_cache.refresh]


def test_refresh_is_single_flight(monkeypatch):
    """F8-08: GET, кнопка и фоновый цикл раньше запускали каждый свой прогон на 16 потоков."""
    release, calls = threading.Event(), []

    def slow():
        calls.append(1)
        release.wait(5)
        return [_row("a")]
    monkeypatch.setattr(diag_cache, "run_diagnostics", slow)
    out = []
    ts = [threading.Thread(target=lambda: out.append(diag_cache.refresh())) for _ in range(3)]
    ts[0].start()
    while not calls:
        _real_sleep(0.001)
    for t in ts[1:]:
        t.start()
    _real_sleep(0.1)
    release.set()
    for t in ts:
        t.join(5)
    assert len(calls) == 1 and len(out) == 3 and all(o == [_row("a")] for o in out)


def test_diag_page_serves_warm_cache_without_probing(client, monkeypatch):
    diag_cache._checks = [_row("llm")]
    diag_cache._checked_at = datetime.now(timezone.utc)
    monkeypatch.setattr(diag_cache, "run_diagnostics",
                        lambda: pytest.fail("GET /diag не должен гнать живой прогон"))
    r = client.get("/diag")
    assert r.status_code == 200 and "снимок от" in r.text
    assert 'action="/diag/refresh"' in r.text     # «проверить снова» — явная кнопка (POST)


def test_explicit_refresh_resets_probe_cache(client, monkeypatch):
    diagnostics._probe_cache["k"] = (time.monotonic(), True, True)
    monkeypatch.setattr(diag_cache, "run_diagnostics", lambda: [_row("a")])
    assert client.post("/diag/refresh", follow_redirects=False).status_code == 303
    assert diagnostics._probe_cache == {}


# ============================ 4. LLM-проба ============================

def _llm(handler):
    from app.integrations.llm import LlmClient
    c = LlmClient()
    seen = []

    class _F:
        def request(self, method, url, **kw):
            seen.append(kw)
            return handler(method, url, kw)
    c._client = _F()
    c.seen = seen
    return c


def _resp(code, body, method="POST", url="http://l/v1/chat/completions"):
    return httpx.Response(code, json=body, request=httpx.Request(method, url))


def test_llm_probe_is_a_real_tiny_completion_of_the_configured_model(monkeypatch):
    monkeypatch.setattr(settings, "LLM_MODEL", "mistral")
    c = _llm(lambda m, u, kw: _resp(200, {"choices": [{"message": {"content": "p"}}]}))
    assert c.probe() is True
    assert c.seen[0]["json"]["max_tokens"] == 4 and c.seen[0]["json"]["model"] == "mistral"
    assert c.seen[0]["timeout"] == 15.0


@pytest.mark.parametrize("code,body,expect", [
    (403, {"error": {"message": "tier_not_allowed: not in your tier"}}, r"HTTP 403.*тариф.*tier_not_allowed"),
    (429, {"error": {"message": "rate_limited"}}, r"HTTP 429.*лимит запросов"),
    (500, {"error": {"message": "Cannot connect to host.docker.internal:11434"}}, r"HTTP 500.*бэкенд.*Cannot connect"),
])
def test_llm_probe_distinguishes_tier_ratelimit_and_dead_backend(code, body, expect):
    """S2-01/S6-01/S7-01: /v1/models был 200 при 403/429/500 на самой генерации."""
    c = _llm(lambda m, u, kw: _resp(code, body))
    with pytest.raises(RuntimeError, match=expect):
        c.probe()
    assert len(c.seen) == 1          # без ретраев BaseClient


def test_llm_probe_unreachable_and_empty_envelope():
    def _down(m, u, kw):
        raise httpx.ConnectError("refused")
    with pytest.raises(RuntimeError, match="LiteLLM недоступен: ConnectError"):
        _llm(_down).probe()
    with pytest.raises(RuntimeError, match="без choices"):
        _llm(lambda m, u, kw: _resp(200, {"data": []})).probe()


def test_llm_diag_row_goes_red_with_reason_and_probe_is_cached(monkeypatch):
    from app.integrations.llm import LlmClient
    calls = []

    def probe(self, timeout=15.0):
        calls.append(1)
        raise RuntimeError("модель mistral: HTTP 403 — модель недоступна (тариф/права)")
    monkeypatch.setattr(LlmClient, "probe", probe)
    spec = next(s for s in diagnostics._spec() if s[0] == "llm")
    for _ in range(3):                  # три прогона диагностики подряд = одна проба
        out = diagnostics.run_diagnostics(specs=[spec])[0]
    assert out["status"] == "fail" and "HTTP 403" in out["error"] and len(calls) == 1
    diagnostics.reset_probe_cache()     # явная «проверить снова»
    diagnostics.run_diagnostics(specs=[spec])
    assert len(calls) == 2


def test_probe_cache_expires(monkeypatch):
    n = []
    diagnostics._cached_probe("k", lambda: n.append(1) or True)
    diagnostics._cached_probe("k", lambda: n.append(1) or True)
    assert len(n) == 1
    diagnostics._probe_cache["k"] = (time.monotonic() - diagnostics.PROBE_TTL - 1, True, True)
    diagnostics._cached_probe("k", lambda: n.append(1) or True)
    assert len(n) == 2


# ============================ 5. SearXNG ============================

def _sx(body):
    from app.integrations.searxng import SearxngClient
    c = SearxngClient()

    class _F:
        def request(self, method, url, **kw):
            c.q = kw["params"]["q"]
            return httpx.Response(200, json=body, request=httpx.Request(method, url))
    c._client = _F()
    return c


def test_searxng_health_ok_on_results_and_uses_site_query():
    c = _sx({"results": [{"url": "https://en.wikipedia.org/"}], "unresponsive_engines": []})
    assert c.health() is True and c.q.startswith("site:")


def test_searxng_health_red_on_zero_results_names_dead_engines():
    """S6-09/S7-04/F8-03: ping() был True при results=0 и всех движках в CAPTCHA."""
    c = _sx({"results": [], "unresponsive_engines": [["brave", "Suspended: too many requests"],
                                                      ["duckduckgo", "CAPTCHA"]]})
    with pytest.raises(RuntimeError, match=r"0 результатов.*brave \(Suspended.*duckduckgo \(CAPTCHA\)"):
        c.health()
    with pytest.raises(RuntimeError, match="молчат без ошибок"):
        _sx({"results": []}).health()


def test_searxng_diag_row_red_when_engines_dead(monkeypatch):
    from app.integrations.searxng import SearxngClient
    monkeypatch.setattr(SearxngClient, "search_full",
                        lambda self, q, **kw: {"results": [], "unresponsive_engines": [["brave", "CAPTCHA"]]})
    spec = next(s for s in diagnostics._spec() if s[0] == "searxng")
    out = diagnostics.run_diagnostics(specs=[spec])[0]
    assert out["status"] == "fail" and "brave (CAPTCHA)" in out["error"]


# ============================ 6. Cloudflare ============================

def _cf(verify, policy):
    from app.integrations.cloudflare import CloudflareClient
    c = CloudflareClient()

    class _F:
        def request(self, method, url, **kw):
            body = verify if url.endswith("/verify") else policy
            if isinstance(body, int):
                return httpx.Response(body, json={"success": False, "errors": []},
                                      request=httpx.Request(method, url))
            return httpx.Response(200, json={"success": True, "result": body},
                                  request=httpx.Request(method, url))
    c._client = _F()
    return c


def _pol(*names):
    return {"policies": [{"permission_groups": [{"id": str(i), "name": n} for i, n in enumerate(names)]}]}


def test_cloudflare_note_lists_write_permissions():
    c = _cf({"id": "t1", "status": "active"}, _pol("Zone Write", "DNS Write", "Zone Read"))
    note = c.ping_detail()
    assert "токен активен" in note and "Zone Write" in note and "DNS Write" in note and "⚠" not in note


def test_cloudflare_note_warns_about_missing_dns_and_fails_on_read_only():
    c = _cf({"id": "t1", "status": "active"}, _pol("Zone Write", "Zone Read"))
    assert "⚠ не найдено: DNS" in c.ping_detail()
    with pytest.raises(RuntimeError, match="только на чтение"):
        _cf({"id": "t1", "status": "active"}, _pol("Zone Read", "DNS Read")).ping_detail()


def test_cloudflare_note_honest_when_token_cannot_read_own_policies():
    note = _cf({"id": "t1", "status": "active"}, 403).ping_detail()
    assert "токен активен" in note and "права записи не проверены" in note


def test_cloudflare_inactive_token_fails():
    with pytest.raises(RuntimeError, match="не активен"):
        _cf({"id": "t1", "status": "disabled"}, _pol()).ping_detail()


def test_run_one_carries_string_note_on_ok():
    row = diagnostics._run_one("cf", "CF", "r", "1", "M3", False, lambda: "токен активен; права записи не проверены")
    assert row["status"] == "ok" and row["note"].startswith("токен активен") and row["error"] is None
