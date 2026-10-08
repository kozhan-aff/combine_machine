"""W2f: проверка индексации без SearXNG (GSC URL Inspection) + IndexNow-пинг после публикации.

Сеть — только httpx.MockTransport. Реальный RSA-ключ генерируется в тесте (cryptography), в Google
ничего не уходит. Каждый тест падает без своего фикса: порядок источников, честный unknown, квота,
фолбэк на SearXNG, ключ IndexNow от сида домена, пинг только при записанном файле-ключе.
"""
import base64
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives import hashes
from sqlalchemy import select

import app.db as db
from app.config import settings
from app.integrations import gsc as gsc_mod
from app.integrations.gsc import GscClient
from app.integrations.indexnow import IndexNowClient, IndexNowError
from app.models.domain import Domain
from app.models.monitoring import IndexHistory
from app.models.offer import Offer
from app.models.site import Page, Site
from app.services import publish, site_builder as sb

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_PEM = _KEY.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                          serialization.NoEncryption()).decode()
SA = json.dumps({"type": "service_account", "client_email": "bot@proj.iam.gserviceaccount.com",
                 "private_key": _PEM, "token_uri": "https://evil.example/steal"})


class Google:
    """Мок oauth2 + searchconsole. inspect: callable(siteUrl, inspectionUrl) -> (status, json)."""

    def __init__(self, inspect):
        self.inspect, self.calls = inspect, []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(req)
        if req.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3600})
        body = json.loads(req.content)
        st, js = self.inspect(body["siteUrl"], body["inspectionUrl"])
        return httpx.Response(st, json=js)

    def inspects(self):
        return [c for c in self.calls if c.url.host != "oauth2.googleapis.com"]


def res(verdict, cov, crawl="2026-10-01T10:00:00Z"):
    return 200, {"inspectionResult": {"indexStatusResult": {
        "verdict": verdict, "coverageState": cov, "lastCrawlTime": crawl}}}


@pytest.fixture
def gsc_on(monkeypatch):
    monkeypatch.setattr(settings, "GSC_SERVICE_ACCOUNT_JSON", SA)

    def install(inspect):
        g = Google(inspect)
        orig = GscClient.__init__

        def init(self):
            orig(self)
            self._client = httpx.Client(transport=httpx.MockTransport(g))
        monkeypatch.setattr(GscClient, "__init__", init)
        return g
    return install


def _site(domain="idx.com", paths=("/",)):
    with db.SessionLocal() as s:
        d = Domain(domain=domain, source="dropcatch", status="live")
        s.add(d)
        s.commit()
        site = Site(domain_id=d.id, status="published", doc_root=f"/www/wwwroot/{domain}")
        s.add(site)
        s.commit()
        for p in paths:
            s.add(Page(site_id=site.id, url_path=p, title="t", status="published", body="<p>x</p>"))
        s.commit()
        return site.id


def _searx(monkeypatch, results=(), dead=()):
    calls = []
    payload = {"results": list(results), "unresponsive_engines": [list(e) for e in dead]}
    monkeypatch.setattr("app.integrations.searxng.SearxngClient.search_full",
                        lambda self, q, **kw: calls.append(q) or payload)
    return calls


def _pages(sid):
    with db.SessionLocal() as s:
        return {p.url_path: p for p in s.execute(select(Page).where(Page.site_id == sid)).scalars()}


# ── GSC: клиент ───────────────────────────────────────────────────────────────────────────────

def test_gsc_inspect_maps_verdicts_and_signs_jwt_with_fixed_token_uri(gsc_on):
    g = gsc_on(lambda site, url: res("PASS", "Submitted and indexed"))
    r = GscClient().inspect("idx.com", "https://idx.com/")
    assert r == {"indexed": True, "verdict": "PASS", "coverage_state": "Submitted and indexed",
                 "last_crawl": "2026-10-01T10:00:00Z", "property": "sc-domain:idx.com"}
    tok = g.calls[0]
    assert tok.url.host == "oauth2.googleapis.com"          # token_uri из JSON ("evil.example") игнорируется
    form = dict(x.split("=", 1) for x in tok.content.decode().split("&"))
    h, c, sig = form["assertion"].split(".")
    pad = lambda x: x + "=" * (-len(x) % 4)               # noqa: E731
    claims = json.loads(base64.urlsafe_b64decode(pad(c)))
    assert claims["iss"] == "bot@proj.iam.gserviceaccount.com" and claims["aud"] == gsc_mod.TOKEN_URL
    assert claims["scope"] == gsc_mod.SCOPE
    _KEY.public_key().verify(base64.urlsafe_b64decode(pad(sig)), f"{h}.{c}".encode(),
                             padding.PKCS1v15(), hashes.SHA256())    # подпись настоящая
    api = g.inspects()[0]
    assert api.headers["Authorization"] == "Bearer tok-1"
    assert json.loads(api.content) == {"inspectionUrl": "https://idx.com/", "siteUrl": "sc-domain:idx.com"}


@pytest.mark.parametrize("verdict,expected", [("PASS", True), ("NEUTRAL", False), ("FAIL", False),
                                              ("PARTIAL", None), ("VERDICT_UNSPECIFIED", None)])
def test_gsc_verdict_table(gsc_on, verdict, expected):
    gsc_on(lambda s, u: res(verdict, "x"))
    assert GscClient().inspect("a.com", "https://a.com/")["indexed"] is expected


def test_gsc_missing_status_block_is_unknown_not_not_indexed(gsc_on):
    gsc_on(lambda s, u: (200, {"inspectionResult": {}}))
    assert GscClient().inspect("a.com", "https://a.com/")["indexed"] is None


def test_gsc_falls_back_to_url_prefix_property_and_remembers_it(gsc_on):
    g = gsc_on(lambda site, url: (403, {}) if site.startswith("sc-domain:") else res("PASS", "ok"))
    c = GscClient()
    assert c.inspect("a.com", "https://a.com/")["property"] == "https://a.com/"
    c.inspect("a.com", "https://a.com/x/")
    assert [json.loads(x.content)["siteUrl"] for x in g.inspects()] == [
        "sc-domain:a.com", "https://a.com/", "https://a.com/"]       # во 2-й раз доменное не пробуем


def test_gsc_no_access_when_both_properties_forbidden(gsc_on):
    gsc_on(lambda s, u: (403, {}))
    with pytest.raises(gsc_mod.GscNoAccess):
        GscClient().inspect("a.com", "https://a.com/")


def test_gsc_429_is_quota_and_not_retried(gsc_on):
    g = gsc_on(lambda s, u: (429, {}))
    with pytest.raises(gsc_mod.GscQuota):
        GscClient().inspect("a.com", "https://a.com/")
    assert len(g.inspects()) == 1


def test_gsc_configured_requires_valid_service_account(monkeypatch):
    monkeypatch.setattr(settings, "GSC_SERVICE_ACCOUNT_JSON", "")
    assert not gsc_mod.configured()
    for bad in ("not json", "[1]", '{"client_email": "a"}'):
        monkeypatch.setattr(settings, "GSC_SERVICE_ACCOUNT_JSON", bad)
        assert not gsc_mod.configured()
    monkeypatch.setattr(settings, "GSC_SERVICE_ACCOUNT_JSON", SA)
    assert gsc_mod.configured()


# ── check_index: порядок источников ──────────────────────────────────────────────────────────

def test_check_index_uses_gsc_first_and_never_asks_searxng(gsc_on, monkeypatch):
    gsc_on(lambda s, u: res("PASS", "Submitted and indexed") if u.endswith("/vs/")
           else res("NEUTRAL", "Crawled - currently not indexed"))
    sx = _searx(monkeypatch)
    sid = _site(paths=("/", "/vs"))
    out = publish.check_index(sid)
    assert out["pages"] == {"/": "not_indexed", "/vs": "indexed"}
    assert out["sources"] == {"/": "gsc", "/vs": "gsc"} and sx == []
    assert out["details"]["/vs"]["last_crawl"] == "2026-10-01T10:00:00Z"
    with db.SessionLocal() as s:
        h = s.execute(select(IndexHistory).order_by(IndexHistory.id)).scalars().all()
        assert sorted(x.coverage_state for x in h) == ["Crawled - currently not indexed", "Submitted and indexed"]


def test_check_index_without_gsc_key_uses_searxng(monkeypatch):
    sx = _searx(monkeypatch, results=[{"url": "https://idx.com/"}])
    sid = _site()
    out = publish.check_index(sid)
    assert out["pages"] == {"/": "indexed"} and out["sources"] == {"/": "searxng"} and sx == ["site:idx.com"]
    with db.SessionLocal() as s:
        assert s.execute(select(IndexHistory)).scalar_one().coverage_state is None


def test_gsc_partial_is_honest_unknown_without_searxng_override(gsc_on, monkeypatch):
    gsc_on(lambda s, u: res("PARTIAL", "x"))
    sx = _searx(monkeypatch, results=[{"url": "https://idx.com/"}])
    out = publish.check_index(_site())
    assert out["pages"] == {"/": "unknown"} and out["all_unknown"] and sx == []


def test_no_access_to_property_falls_back_to_searxng_with_note(gsc_on, monkeypatch):
    g = gsc_on(lambda s, u: (403, {}))
    _searx(monkeypatch, results=[{"url": "https://idx.com/"}, {"url": "https://idx.com/vs/"}])
    out = publish.check_index(_site(paths=("/", "/vs")))
    assert out["pages"] == {"/": "indexed", "/vs": "indexed"}
    assert set(out["sources"].values()) == {"searxng"} and "GSC" in out["gsc_note"]
    assert len(g.inspects()) == 2          # две попытки свойства на ПЕРВОЙ странице, дальше GSC не долбим


def test_gsc_quota_429_switches_to_searxng_and_stops_asking_google(gsc_on, monkeypatch):
    g = gsc_on(lambda s, u: (429, {}))
    _searx(monkeypatch, dead=[("brave", "CAPTCHA")])
    out = publish.check_index(_site(paths=("/", "/vs", "/setup")))
    assert len(g.inspects()) == 1
    assert set(out["pages"].values()) == {"unknown"} and out["all_unknown"]       # SearXNG слеп -> честно unknown


def test_daily_cap_counted_from_history_rows(gsc_on, monkeypatch):
    g = gsc_on(lambda s, u: res("PASS", "ok"))
    sx = _searx(monkeypatch, results=[{"url": "https://idx.com/"}])
    monkeypatch.setattr(publish, "GSC_SITE_DAILY_CAP", 2)
    sid = _site(paths=("/", "/vs", "/setup"))
    pg = _pages(sid)
    with db.SessionLocal() as s:                       # два GSC-вопроса уже заданы сегодня
        for p in ("/", "/vs"):
            s.add(IndexHistory(page_id=pg[p].id, index_status="indexed", coverage_state="ok",
                               checked_at=datetime.now(timezone.utc)))
        s.commit()
    out = publish.check_index(sid)
    assert g.inspects() == [] and set(out["sources"].values()) == {"searxng"} and len(sx) == 3


def test_cap_ignores_yesterday_and_searxng_rows(gsc_on, monkeypatch):
    g = gsc_on(lambda s, u: res("PASS", "ok"))
    monkeypatch.setattr(publish, "GSC_SITE_DAILY_CAP", 1)
    sid = _site()
    pid = _pages(sid)["/"].id
    with db.SessionLocal() as s:
        s.add(IndexHistory(page_id=pid, index_status="indexed", coverage_state="ok",
                           checked_at=datetime.now(timezone.utc) - timedelta(days=2)))
        s.add(IndexHistory(page_id=pid, index_status="indexed", coverage_state=None,
                           checked_at=datetime.now(timezone.utc)))
        s.commit()
    assert publish.check_index(sid)["sources"] == {"/": "gsc"} and len(g.inspects()) == 1


def test_only_indexed_moves_site_to_monitoring_not_not_indexed(gsc_on):
    gsc_on(lambda s, u: res("NEUTRAL", "URL is unknown to Google"))
    sid = _site()
    publish.check_index(sid)
    with db.SessionLocal() as s:
        assert s.get(Site, sid).status == "published"
    gsc_on(lambda s, u: res("PASS", "ok"))
    publish.check_index(sid)
    with db.SessionLocal() as s:
        assert s.get(Site, sid).status == "monitoring"


def test_cooldown_respected_with_gsc(gsc_on):
    g = gsc_on(lambda s, u: res("PASS", "ok"))
    sid = _site()
    publish.check_index(sid, only_due=True)
    publish.check_index(sid, only_due=True)            # cooldown 3 суток у indexed
    assert len(g.inspects()) == 1


# ── IndexNow ─────────────────────────────────────────────────────────────────────────────────

def test_indexnow_key_deterministic_per_domain_and_distinct():
    k = sb.indexnow_key("Alpha.com")
    assert k == sb.indexnow_key("www.alpha.com") == sb.indexnow_key("alpha.com")
    assert len(k) == 32 and int(k, 16) >= 0
    assert k != sb.indexnow_key("beta.com")


def test_site_files_carry_key_file_with_key_as_body():
    f = sb.build_site_files("alpha.com", [])
    k = sb.indexnow_key("alpha.com")
    assert f[f"{k}.txt"] == k


def test_indexnow_submit_payload(monkeypatch):
    seen = []
    c = IndexNowClient()
    c._client = httpx.Client(transport=httpx.MockTransport(
        lambda r: seen.append(r) or httpx.Response(202)))
    assert c.submit("a.com", "k" * 32, ["https://a.com/", "https://a.com/", "https://a.com/vs/"]) == 2
    r = seen[0]
    assert r.method == "POST" and str(r.url) == settings.INDEXNOW_URL
    assert json.loads(r.content) == {"host": "a.com", "key": "k" * 32, "keyLocation": f"https://a.com/{'k' * 32}.txt",
                                     "urlList": ["https://a.com/", "https://a.com/vs/"]}
    assert c.submit("a.com", "k", []) == 0 and len(seen) == 1


@pytest.mark.parametrize("code", [400, 403, 422, 429, 500])
def test_indexnow_error_statuses_raise(code):
    c = IndexNowClient()
    c._client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(code)))
    with pytest.raises(IndexNowError):
        c.submit("a.com", "k", ["https://a.com/"])


@pytest.fixture
def deploy(monkeypatch):
    log = []
    monkeypatch.setattr("app.integrations.aapanel.AaPanelClient.__init__", lambda self: None)
    monkeypatch.setattr("app.integrations.aapanel.AaPanelClient.write_file",
                        lambda self, path, body: log.append(path))
    return log


def _edited_site(domain="pub.com"):
    with db.SessionLocal() as s:
        d = Domain(domain=domain, source="dropcatch", status="purchased")
        s.add(d)
        s.commit()
        off = Offer(brand="NordVPN", affiliate_link="https://ex.com/aff", active=True, language="en")
        s.add(off)
        s.commit()
        site = Site(domain_id=d.id, status="content", aapanel_site_name=domain,
                    doc_root=f"/www/wwwroot/{domain}", offer_id=off.id)
        s.add(site)
        s.commit()
        for path in ("/", "/vs"):
            s.add(Page(site_id=site.id, url_path=path, title=f"T {path}", status="edited", lang="en",
                       offer_id=off.id, body="<p>Body of the page that is long enough.</p>"))
        s.commit()
        return site.id


def _indexnow_on(monkeypatch, status=200):
    monkeypatch.setattr(settings, "INDEXNOW_ENABLED", True)
    seen = []
    orig = IndexNowClient.__init__

    def init(self):
        orig(self)
        self._client = httpx.Client(transport=httpx.MockTransport(
            lambda r: seen.append(json.loads(r.content)) or httpx.Response(status)))
    monkeypatch.setattr(IndexNowClient, "__init__", init)
    return seen


def test_publish_pings_indexnow_with_published_urls_and_key_file(deploy, monkeypatch):
    seen = _indexnow_on(monkeypatch)
    out = publish.publish_site(_edited_site())
    assert out["status"] == "published"
    k = sb.indexnow_key("pub.com")
    assert f"/www/wwwroot/pub.com/{k}.txt" in deploy
    assert len(seen) == 1 and seen[0]["key"] == k and seen[0]["host"] == "pub.com"
    assert sorted(seen[0]["urlList"]) == ["https://pub.com/", "https://pub.com/vs/"]


def test_indexnow_failure_is_warning_not_publish_failure(deploy, monkeypatch):
    _indexnow_on(monkeypatch, status=429)
    out = publish.publish_site(_edited_site())
    assert out["status"] == "published" and any("IndexNow" in w for w in out["warnings"])
    with db.SessionLocal() as s:
        assert {p.status for p in s.execute(select(Page)).scalars()} == {"published"}


def test_indexnow_off_means_no_call(deploy, monkeypatch):
    seen = _indexnow_on(monkeypatch)
    monkeypatch.setattr(settings, "INDEXNOW_ENABLED", False)
    publish.publish_site(_edited_site())
    assert seen == []


def test_indexnow_skipped_when_key_file_not_written(deploy, monkeypatch):
    seen = _indexnow_on(monkeypatch)
    k = sb.indexnow_key("pub.com")

    def write(self, path, body):
        if path.endswith(f"{k}.txt"):
            raise RuntimeError("disk full")
        deploy.append(path)
    monkeypatch.setattr("app.integrations.aapanel.AaPanelClient.write_file", write)
    out = publish.publish_site(_edited_site())
    assert seen == [] and any("файл-ключ" in w for w in out["warnings"])


def test_gsc_and_indexnow_fields_are_on_keys_screen():
    from app.services.api_keys import EDITABLE
    assert EDITABLE["GSC_SERVICE_ACCOUNT_JSON"].secret
    for k in ("GSC_API_URL", "INDEXNOW_ENABLED", "INDEXNOW_URL"):
        assert k in EDITABLE and hasattr(settings, k)


# ── фикс-раунд 1: секрет IndexNow, видимость отката GSC ──────────────────────────────────────

def test_indexnow_key_is_hmac_not_plain_hash_and_depends_on_secret(monkeypatch):
    import hashlib
    k = sb.indexnow_key("alpha.com")
    # не голый sha256 от сида, ни с «солью», ни без — иначе схему угадают и сцепят сайты портфеля
    for seed in ("alpha.com", "indexnow:alpha.com"):
        assert k != hashlib.sha256(seed.encode()).hexdigest()[:32]
    monkeypatch.setattr(settings, "INDEXNOW_SECRET", "другой-секрет")
    assert sb.indexnow_key("alpha.com") != k


def test_no_secret_means_no_key_file_and_no_ping(deploy, monkeypatch):
    seen = _indexnow_on(monkeypatch)
    monkeypatch.setattr(settings, "INDEXNOW_SECRET", "")
    assert sb.indexnow_key("pub.com") is None
    assert set(sb.build_site_files("pub.com", ["/"])) == {"robots.txt", "sitemap.xml"}
    out = publish.publish_site(_edited_site())
    assert out["status"] == "published" and seen == []
    assert any("INDEXNOW_SECRET" in w for w in out["warnings"])


def test_stage_surfaces_gsc_fallback(gsc_on, monkeypatch):
    from app.services import orchestrator as orch
    gsc_on(lambda s, u: (403, {}))
    _searx(monkeypatch, results=[{"url": "https://idx.com/"}])
    sid = _site()
    done, errs, counts = orch._stage_check_index(5)
    assert done == 1 and counts.get("gsc_fallback") == 1
    assert any(f"site#{sid}" in e and "GSC" in e for e in errs)
    assert "gsc_fallback" in orch.COUNT_RU


def test_panel_check_index_flash_shows_source_and_gsc_reason(gsc_on, monkeypatch, client):
    from urllib.parse import unquote
    gsc_on(lambda s, u: (403, {}))
    _searx(monkeypatch, results=[{"url": "https://idx.com/"}])
    sid = _site()
    r = client.post(f"/sites/{sid}/check-index", follow_redirects=False)
    loc = unquote(r.headers["location"])
    assert "searxng" in loc and "GSC недоступен" in loc and "свойств" in loc


def test_indexnow_secret_is_on_keys_screen():
    from app.services.api_keys import EDITABLE
    assert EDITABLE["INDEXNOW_SECRET"].secret and hasattr(settings, "INDEXNOW_SECRET")
