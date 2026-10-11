"""M3 provision: режим SSL CF согласован с origin (S4-02/S5-04/S7-08/F8-09), www (S4-09/S5-12), NS-ожидание
(S4-04), порядок шагов (S5-13), финальная проверка по IP origin. Всё на фейках: CF — объект, aaPanel —
подменённый `_post`, origin — MockTransport (фикстура `origin_probe`)."""
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

import app.db as db
from app.config import settings
from app.integrations.aapanel import AaPanelClient
from app.models.domain import Domain
from app.models.site import Site
from app.services import provisioning

IP = "203.0.113.9"


class _CF:
    account_id = "accHEX"

    def __init__(self, status="active", ssl="full", ca_boom=None, ssl_boom=None):
        self.zone_status, self.mode, self.ca_boom, self.ssl_boom = status, ssl, ca_boom, ssl_boom
        self.steps, self.settings, self.records, self.csr = [], {}, [], None
        self.checks = 0

    def _zone(self):
        return {"id": "zone1", "status": self.zone_status, "name_servers": ["a.ns.cf", "b.ns.cf"]}

    def ensure_zone(self, domain):
        self.steps.append("zone")
        return self._zone()

    def get_zone(self, zid):
        return self._zone()

    def activation_check(self, zid):
        self.checks += 1
        return True

    def ensure_a_record(self, zid, name, ip, proxied=True):
        self.steps.append(f"a:{name}")
        self.records.append((name, ip, proxied))
        return {"id": "r"}

    def get_zone_setting(self, zid, sid):
        return {"value": self.mode if sid == "ssl" else self.settings.get(sid, "on" if sid == "rum" else "off")}

    def set_ssl(self, zid, mode="full"):
        if self.ssl_boom:
            raise self.ssl_boom
        self.steps.append(f"ssl:{mode}")
        self.mode = mode
        return True

    def set_zone_setting(self, zid, sid, value):
        self.settings[sid] = value
        return True

    def create_origin_certificate(self, csr_pem, hostnames, validity_days=5475):
        if self.ca_boom:
            raise self.ca_boom
        self.csr = (csr_pem, hostnames)
        self.steps.append("origin_ca")
        return {"certificate": "-----BEGIN CERTIFICATE-----\nAAA\n-----END CERTIFICATE-----\n"}


class _Panel:
    def __init__(self, ssl_ok=True, add_ok=True, sites=None, on_add_domain=None):
        self.calls, self.ssl_ok, self.add_ok = [], ssl_ok, add_ok
        self.sites, self.on_add_domain = sites or [], on_add_domain

    def __call__(self, path, data=None):
        self.calls.append((path, data))
        if "getData" in path:
            return {"data": self.sites}
        if "CreateFile" in path or "SaveFileBody" in path:
            return {"status": True}
        if "DeleteFile" in path:
            return {"status": True}
        if "AddDomain" in path:
            if self.on_add_domain:
                self.on_add_domain()
            return {"domains": [{"name": "www.ex.com", "status": True}]}
        if "AddSite" in path:
            return {"siteStatus": True} if self.add_ok else {"status": False, "msg": "boom"}
        if "SetSSL" in path:
            return {"status": True} if self.ssl_ok else {"status": False, "msg": "ssl boom"}
        raise AssertionError(f"неожиданный вызов панели: {path}")


def _seed(status="provisioning", **kw) -> int:
    with db.SessionLocal() as s:
        d = Domain(domain="ex.com", source="dropcatch", status="purchased")
        s.add(d)
        s.commit()
        site = Site(domain_id=d.id, status=status, doc_root="/www/wwwroot/ex.com", **kw)
        s.add(site)
        s.commit()
        return site.id


def _env(monkeypatch, cf=None, panel=None):
    cf = cf or _CF()
    panel = panel or _Panel()
    monkeypatch.setattr(settings, "VPS_ORIGIN_IP", IP)
    monkeypatch.setattr(settings, "AAPANEL_URL", "https://127.0.0.1:8888")
    monkeypatch.setattr("app.integrations.cloudflare.CloudflareClient", lambda: cf)
    monkeypatch.setattr(AaPanelClient, "_post", panel)
    return cf, panel


def _site(sid) -> Site:
    with db.SessionLocal() as s:
        site = s.get(Site, sid)
        s.expunge(site)
        return site


# --- SSL согласован с origin ------------------------------------------------------------------

def test_no_origin_https_means_flexible_never_full(monkeypatch, origin_probe):
    """КРИТИЧНО (S5-04/F8-09): на origin нет HTTPS -> CF НЕ в full/strict (525). Зона пришла в 'full' —
    провижн понижает её до flexible и пишет origin_https='none'."""
    origin_probe.https = httpx.ConnectError("closed")
    cf, _ = _env(monkeypatch, _CF(ssl="full"))
    out = provisioning.provision(_seed())
    assert out["status"] == "provisioned" and out["ssl_mode"] == "flexible"
    assert cf.mode == "flexible" and "ssl:full" not in cf.steps and "ssl:strict" not in cf.steps
    assert _site(1).origin_https == "none"


def test_https_ok_but_foreign_cert_gives_full_not_strict(monkeypatch):
    """HTTPS отдаёт НАШ маркер, но сертификат не Origin CA -> full, не strict."""
    cf, _ = _env(monkeypatch, _CF(ssl="off"))
    out = provisioning.provision(_seed())
    assert out["ssl_mode"] == "full" and cf.mode == "full" and _site(1).origin_https == "ok"


def test_foreign_ssl_vhost_answering_200_is_not_confirmed_https(monkeypatch, origin_probe):
    """КРИТИЧНО (ревью G2): на :443 уже есть чужой ssl-vhost, nginx отдаёт ему неизвестный SNI — код
    200, а тела с нашим nonce нет. Раньше это считалось «HTTPS ок» -> CF full -> посетители нового
    домена видели чужой сайт портфеля. Теперь режим остаётся flexible, причина названа."""
    origin_probe.marker_https = False
    cf, _ = _env(monkeypatch, _CF(ssl="full"))
    out = provisioning.provision(_seed())
    assert _site(1).origin_https == "none" and out["ssl_mode"] == "flexible" and cf.mode == "flexible"
    assert "сервер отдаёт чужой сайт или заглушку" in out["ssl_error"]
    assert "ssl:full" not in cf.steps and "ssl:strict" not in cf.steps


def test_marker_nonce_is_written_to_docroot_and_probed(monkeypatch, origin_probe):
    """Маркер кладётся в docroot САЙТА через панель, а проба просит именно этот файл по IP+Host(+SNI)."""
    _, panel = _env(monkeypatch)
    provisioning.provision(_seed())
    save = next(d for p, d in panel.calls if "SaveFileBody" in p)
    name = provisioning.marker_name("ex.com")
    assert save["path"] == f"/www/wwwroot/ex.com/{name}" and save["data"] == origin_probe.nonce
    https = next(r for r in origin_probe.requests if r.url.scheme == "https")
    assert https.url.path == f"/{name}" and https.extensions["sni_hostname"] == "ex.com"
    assert provisioning.marker_name("a.com") != provisioning.marker_name("b.com")   # не общий отпечаток


def test_marker_removed_after_verify_and_salt_is_not_the_api_key(monkeypatch, origin_probe):
    """Minor: после успешной пробы маркер удаляется; имя не зависит от AAPANEL_API_KEY."""
    _, panel = _env(monkeypatch)
    out = provisioning.provision(_seed())
    assert out["status"] == "provisioned"
    name = provisioning.marker_name("ex.com")
    dele = [d for p, d in panel.calls if "DeleteFile" in p]
    assert dele and dele[0]["path"] == f"/www/wwwroot/ex.com/{name}"
    monkeypatch.setattr(settings, "AAPANEL_API_KEY", "другой-ключ")
    assert provisioning.marker_name("ex.com") == name


def test_default_vhost_answering_200_does_not_pass_verify(monkeypatch, origin_probe):
    """Ревью G2 (Important): дефолтный vhost aaPanel на :80 отвечает 200 на любой Host. Без нашего
    маркера в теле финальная проверка обязана упасть, а не объявить сайт готовым."""
    origin_probe.marker_http = False
    _env(monkeypatch)
    out = provisioning.provision(_seed())
    assert out["status"] == "error" and out["step"] == "verify" and "нашего проверочного файла нет" in out["error"]
    assert _site(1).status == "provisioning"


def test_operator_strict_kept_when_origin_https_answers(monkeypatch):
    cf, _ = _env(monkeypatch, _CF(ssl="strict"))
    provisioning.provision(_seed())
    assert cf.mode == "strict" and not [s for s in cf.steps if s.startswith("ssl:")]


def test_operator_strict_downgraded_when_origin_https_dead(monkeypatch, origin_probe):
    """Согласованность важнее старого «не откатывать strict»: strict над мёртвым origin-HTTPS = 525."""
    origin_probe.https = httpx.ConnectError("closed")
    cf, _ = _env(monkeypatch, _CF(ssl="strict"))
    provisioning.provision(_seed())
    assert cf.mode == "flexible"


def test_origin_ca_flow_installs_cert_then_strict(monkeypatch):
    monkeypatch.setattr(settings, "ORIGIN_CA_AUTO", True)
    cf, panel = _env(monkeypatch, _CF(ssl="off"))
    out = provisioning.provision(_seed())
    assert out["origin_https"] == "origin_ca" and out["ssl_mode"] == "strict" and cf.mode == "strict"
    # порядок: сертификат -> режим strict (strict только после установки)
    assert cf.steps.index("origin_ca") < cf.steps.index("ssl:strict")
    csr_pem, hosts = cf.csr
    assert hosts == ["ex.com", "www.ex.com"]                          # один сертификат на ДОМЕН
    from cryptography import x509
    csr = x509.load_pem_x509_csr(csr_pem.encode())
    assert csr.subject.rfc4514_string() == "CN=ex.com"
    ssl_call = next(d for p, d in panel.calls if "SetSSL" in p)
    assert ssl_call["key"].startswith("-----BEGIN PRIVATE KEY-----") and "BEGIN CERTIFICATE" in ssl_call["csr"]
    assert ssl_call["siteName"] == "ex.com"


def test_origin_ca_failure_keeps_flexible_and_reports(monkeypatch, origin_probe):
    monkeypatch.setattr(settings, "ORIGIN_CA_AUTO", True)
    origin_probe.https = httpx.ConnectError("closed")
    cf, _ = _env(monkeypatch, _CF(ssl="full", ca_boom=RuntimeError("403 no Origin CA right")))
    out = provisioning.provision(_seed())
    assert out["status"] == "provisioned" and out["ssl_mode"] == "flexible"
    assert "Origin CA" in out["ssl_error"] and "403" in out["ssl_error"]


def test_origin_ca_installed_but_https_silent_is_demoted(monkeypatch, origin_probe):
    """SetSSL «успешен», а HTTPS не отвечает -> не врём 'origin_ca', режим flexible, причина названа."""
    monkeypatch.setattr(settings, "ORIGIN_CA_AUTO", True)
    origin_probe.https = httpx.ReadTimeout("silent")
    cf, _ = _env(monkeypatch, _CF(ssl="off"))
    # на момент пробы флаг уже 'origin_ca' (установили) — но проба не прошла
    out = provisioning.provision(_seed())
    assert _site(1).origin_https == "none" and out["ssl_mode"] == "flexible"
    assert "сервер по HTTPS не отвечает" in out["ssl_error"]


def test_ssl_mode_failure_is_reported(monkeypatch):
    cf, _ = _env(monkeypatch, _CF(ssl="off", ssl_boom=RuntimeError("Cloudflare 403")))
    out = provisioning.provision(_seed())
    assert out["status"] == "provisioned" and "Cloudflare 403" in out["ssl_error"]


# --- финальная проверка -----------------------------------------------------------------------

def test_not_ready_until_origin_answers_http(monkeypatch, origin_probe):
    """provision не объявляет content без ответа vhost'а на origin с Host=домен."""
    origin_probe.http = 502
    _env(monkeypatch)
    out = provisioning.provision(_seed())
    site = _site(1)
    assert out["status"] == "error" and out["step"] == "verify" and "HTTP 502" in out["error"]
    assert site.status == "provisioning" and site.provision_step == "verify"


def test_probe_goes_to_origin_ip_with_host_and_sni(monkeypatch, origin_probe):
    _env(monkeypatch)
    provisioning.provision(_seed())
    http = next(r for r in origin_probe.requests if r.url.scheme == "http" and r.headers["host"] == "ex.com"
                and r.url.path != "/")
    https = next(r for r in origin_probe.requests if r.url.scheme == "https")
    assert http.url.host == IP and http.headers["host"] == "ex.com"
    assert https.url.host == IP and https.headers["host"] == "ex.com"
    assert https.extensions["sni_hostname"] == "ex.com"


def test_success_sets_content_and_done(monkeypatch):
    _env(monkeypatch)
    out = provisioning.provision(_seed())
    site = _site(1)
    assert out["status"] == "provisioned" and site.status == "content" and site.provision_step == "done"
    assert site.cloudflare_account_id == "accHEX"


def test_reprovision_does_not_regress_published_site(monkeypatch):
    _env(monkeypatch)
    provisioning.provision(_seed(status="published"))
    assert _site(1).status == "published"


# --- www, настройки зоны, порядок -----------------------------------------------------------

def test_www_alias_dns_and_zone_hardening(monkeypatch):
    cf, panel = _env(monkeypatch)
    provisioning.provision(_seed())
    assert ("ex.com", IP, True) in cf.records and ("www.ex.com", IP, True) in cf.records
    webname = json.loads(next(d for p, d in panel.calls if "AddSite" in p)["webname"])
    assert webname["domainlist"] == ["www.ex.com"]
    assert cf.settings == {"always_use_https": "on", "min_tls_version": "1.2", "rum": "off"}


def test_existing_vhost_without_www_alias_gets_adddomain_then_dns(monkeypatch, origin_probe):
    """Ревью G2 (Important): vhost создан до появления www-алиаса. Проба по Host=www не проходит ->
    AddDomain -> проба проходит -> только тогда A-запись www."""
    origin_probe.www = False
    sites = [{"id": 5, "name": "ex.com", "path": "/www/wwwroot/ex.com"}]
    cf, panel = _env(monkeypatch, panel=_Panel(sites=sites, on_add_domain=lambda: setattr(origin_probe, "www", True)))
    out = provisioning.provision(_seed())
    add = next(d for p, d in panel.calls if "AddDomain" in p)
    assert add == {"id": 5, "webname": "ex.com", "domain": "www.ex.com"}
    assert not [p for p, _ in panel.calls if "AddSite" in p]
    assert ("www.ex.com", IP, True) in cf.records and out["www"] is True
    assert not any("www" in w for w in out.get("warnings", []))


def test_www_dns_skipped_while_alias_unconfirmed(monkeypatch, origin_probe):
    """AddDomain не помог (или ручка отказала) -> A-записи www НЕТ, провижн не падает, оператор предупреждён."""
    origin_probe.www = False
    sites = [{"id": 5, "name": "ex.com", "path": "/www/wwwroot/ex.com"}]
    cf, _ = _env(monkeypatch, panel=_Panel(sites=sites))
    out = provisioning.provision(_seed())
    assert out["status"] == "provisioned" and out["www"] is False
    assert ("ex.com", IP, True) in cf.records and "a:www.ex.com" not in cf.steps
    assert any("запись www НЕ создана" in w for w in out["warnings"])


def test_dns_goes_after_vhost_and_never_without_it(monkeypatch):
    """S5-13: A-записи на origin появляются ТОЛЬКО когда vhost создан; отказ AddSite — CF-зона есть,
    DNS нет, повтор идемпотентен."""
    cf, panel = _env(monkeypatch, panel=_Panel(add_ok=False))
    sid = _seed()
    with pytest.raises(RuntimeError, match="boom"):
        provisioning.provision(sid)
    assert cf.steps == ["zone"] and _site(sid).cf_zone_id == "zone1"
    _env(monkeypatch, cf=cf, panel=_Panel())
    out = provisioning.provision(sid)
    assert out["status"] == "provisioned" and cf.steps[-2:] == ["a:ex.com", "a:www.ex.com"]


# --- NS --------------------------------------------------------------------------------------

def test_pending_zone_awaiting_ns_state_and_hint(monkeypatch):
    cf, panel = _env(monkeypatch, _CF(status="pending"))
    sid = _seed()
    out = provisioning.provision(sid)
    site = _site(sid)
    assert out["status"] == "awaiting_ns" and "a.ns.cf, b.ns.cf" in out["hint"]
    assert site.provision_step == "await_ns" and site.cf_name_servers == ["a.ns.cf", "b.ns.cf"]
    assert site.ns_waiting_since is not None and cf.checks == 1 and panel.calls == []


def test_activation_check_is_throttled_to_once_per_hour(monkeypatch):
    cf, _ = _env(monkeypatch, _CF(status="pending"))
    sid = _seed()
    provisioning.provision(sid)
    provisioning.provision(sid)
    assert cf.checks == 1                                   # второй вызов в тот же час — без PUT
    with db.SessionLocal() as s:
        s.get(Site, sid).ns_checked_at = datetime.now(timezone.utc) - timedelta(hours=2)
        s.commit()
    provisioning.provision(sid)
    assert cf.checks == 2


def test_long_wait_warns_operator(monkeypatch):
    _env(monkeypatch, _CF(status="pending"))
    sid = _seed(ns_waiting_since=datetime.now(timezone.utc) - timedelta(hours=50))
    out = provisioning.provision(sid)
    assert out["waiting_hours"] >= 50 and "⚠" in out["hint"]


def test_zone_activates_after_ns_then_provision_finishes(monkeypatch):
    cf, _ = _env(monkeypatch, _CF(status="pending"))
    sid = _seed()
    assert provisioning.provision(sid)["status"] == "awaiting_ns"
    cf.zone_status = "active"                               # CF заметил NS
    out = provisioning.provision(sid)
    site = _site(sid)
    assert out["status"] == "provisioned" and site.ns_waiting_since is None and site.status == "content"


def test_moved_zone_is_error_not_awaiting_ns(monkeypatch):
    _env(monkeypatch, _CF(status="moved"))
    out = provisioning.provision(_seed())
    assert out["status"] == "error" and "moved" in out["error"]


# --- карточка сайта -----------------------------------------------------------------------------

def test_card_shows_ns_to_set_and_origin_state(client):
    sid = _seed(provision_step="await_ns", cf_name_servers=["a.ns.cf", "b.ns.cf"],
                ns_waiting_since=datetime.now(timezone.utc))
    html = client.get(f"/sites/{sid}").text
    assert "ждёт NS у регистратора" in html and "a.ns.cf, b.ns.cf" in html
    with db.SessionLocal() as s:
        site = s.get(Site, sid)
        site.status, site.origin_https, site.provision_step = "content", "none", "done"
        s.commit()
    html = client.get(f"/sites/{sid}").text
    assert "Cloudflare в режиме flexible" in html
    with db.SessionLocal() as s:
        s.get(Site, sid).origin_https = "origin_ca"
        s.commit()
    assert "Cloudflare в режиме strict" in client.get(f"/sites/{sid}").text


# --- гард открытого origin (S5-14) ---------------------------------------------------------------

def test_default_vhost_page_on_unknown_host_warns(monkeypatch, origin_probe):
    _env(monkeypatch)
    out = provisioning.provision(_seed())                 # фикстура origin_probe: http=200 на любой Host
    assert "на чужой адрес (стандартная заглушка aaPanel)" in out["warnings"][0]
    unknown = [r for r in origin_probe.requests if r.headers["host"].endswith(".invalid")]
    assert unknown and unknown[0].url.host == IP


def test_closed_origin_on_unknown_host_has_no_warning(monkeypatch, origin_probe):
    """default-vhost 444: на чужой Host — обрыв; на наш Host — нормальный ответ."""
    def handler(req):
        if req.headers["host"].endswith(".invalid"):
            raise httpx.RemoteProtocolError("empty reply")
        return httpx.Response(200, text="test-nonce")      # наш vhost отдаёт маркер
    monkeypatch.setattr(provisioning, "_origin_client",
                        lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    _env(monkeypatch)
    assert "warnings" not in provisioning.provision(_seed())


def test_delete_file_failure_keeps_provisioned_with_warning(monkeypatch, origin_probe):
    """Сбой удаления маркера не роняет провижн: provisioned + предупреждение оператору."""
    class _BadDelete(_Panel):
        def __call__(self, path, data=None):
            if "DeleteFile" in path:
                self.calls.append((path, data))
                return {"status": False, "msg": "permission denied"}
            return super().__call__(path, data)

    _env(monkeypatch, panel=_BadDelete())
    out = provisioning.provision(_seed())
    assert out["status"] == "provisioned"
    assert any("не удалён из папки сайта" in w for w in out["warnings"])
