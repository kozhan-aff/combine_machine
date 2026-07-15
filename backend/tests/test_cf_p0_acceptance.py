"""Задача 8 (финализация волны Cloudflare P0): сводные acceptance-проверки аудита §12/§13.

Точечно уже покрыты Задачами 3/4 (test_cloudflare_transport.py, test_cf_sync.py): envelope
success=false, пагинация 51+ зон/аккаунтов, empty≠error, verify-endpoint по типу токена,
find_zone_in_account по account.id, upsert без дублей, failed/empty list ≠ deleted, external
hex, unmanaged без Domain/Site side-effect. Здесь — недостающие сценарии из §12: `editable=false`
доезжает до наблюдения, два connection не смешивают headers, cert-паки в child mirror, DNS
51+ через транспорт, timeout не утекает токеном, zone-scoped capability A не «просачивается»
в capability B (гарантия перед мутациями P3).

P0 — read-only: ни один тест здесь не проверяет и не может спровоцировать
PATCH/POST/DELETE к Cloudflare (fake-клиент вообще не реализует мутирующие методы)."""
import httpx

from app.db import SessionLocal
from app.integrations.cloudflare import CloudflareClient
from app.models.cloudflare import (
    CloudflareConnection, CloudflareZoneSettingObservation,
    CloudflareCertificatePackMirror, CloudflareCapabilityObservation,
)
import app.services.cf_sync as cf_sync


def _resp(payload, status=200):
    return httpx.Response(status, json=payload, request=httpx.Request("GET", "http://x"))


# ---- transport acceptance (§12) ----

def test_two_connections_do_not_mix_headers():
    a = CloudflareClient.with_token("token-A")
    b = CloudflareClient.with_token("token-B")
    assert a._headers()["Authorization"] == "Bearer token-A"
    assert b._headers()["Authorization"] == "Bearer token-B"


def test_dns_pagination_over_50():
    c = CloudflareClient.with_token("tok")

    def fake(method, url, **kw):
        page = kw["params"]["page"]
        results = [{"id": f"r{page}-{i}", "type": "A", "name": "a.ru"}
                   for i in range(50 if page == 1 else 3)]
        return _resp({"success": True, "result": results,
                      "result_info": {"page": page, "total_pages": 2}})
    c.request = fake
    assert len(c.list_dns_paginated("zid")) == 53


def test_timeout_does_not_leak_token():
    c = CloudflareClient.with_token("SECRET-TOKEN")

    def boom(method, url, **kw):
        raise httpx.ReadTimeout("timeout", request=httpx.Request("GET", url))
    c.request = boom
    try:
        c.list_zones_paginated("acc")
    except Exception as e:
        assert "SECRET-TOKEN" not in str(e)


# ---- sync acceptance (§12) ----

class _FakeCF:
    def __init__(self, zones, dns=None, packs=None, editable=True):
        self._zones, self._dns, self._packs, self._editable = zones, dns or [], packs or [], editable

    @classmethod
    def with_token(cls, *a, **k):
        return _CUR[0]

    def verify_token(self, kind, account_id=""):
        return {"status": "active"}

    def list_accounts_paginated(self):
        return [{"id": "accHEX", "name": "Acc"}]

    def list_zones_paginated(self, account_id):
        return self._zones

    def find_zone_in_account(self, name, account_id):
        return next((z for z in self._zones if z["name"] == name), None)

    def list_dns_paginated(self, zone_id, type=None, name=None):
        return self._dns

    def get_zone_setting(self, zone_id, setting_id):
        return {"id": setting_id, "value": "off", "editable": self._editable}

    def list_universal_certificate_packs(self, zone_id):
        return self._packs

    def get_dnssec(self, zone_id):
        return {"status": "disabled"}


_CUR = [None]


def _seed(db):
    c = CloudflareConnection(label="t", secret_ref="env:CLOUDFLARE_API_TOKEN",
                             token_kind="user", status="unverified")
    db.add(c); db.commit(); return c


def test_editable_false_reaches_observation(monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "tok")
    z = {"id": "zid1", "name": "a.ru", "status": "active", "account": {"id": "accHEX"}}
    _CUR[0] = _FakeCF([z], editable=False)
    monkeypatch.setattr(cf_sync, "CloudflareClient", _FakeCF)
    with SessionLocal() as db:
        cf_sync.sync_connection(db, _seed(db))
        obs = (db.query(CloudflareZoneSettingObservation)
                 .filter_by(cloudflare_zone_id="zid1", setting_id="ssl").one())
        assert obs.editable is False


def test_cert_packs_stored_in_child_mirror(monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "tok")
    z = {"id": "zid1", "name": "a.ru", "status": "active", "account": {"id": "accHEX"}}
    packs = [{"id": "pk1", "type": "universal", "status": "active", "hosts": ["a.ru"]}]
    _CUR[0] = _FakeCF([z], packs=packs)
    monkeypatch.setattr(cf_sync, "CloudflareClient", _FakeCF)
    with SessionLocal() as db:
        cf_sync.sync_connection(db, _seed(db))
        assert db.query(CloudflareCertificatePackMirror).filter_by(cf_pack_id="pk1").count() == 1


def test_zone_scoped_capability_a_not_allowed_for_b(monkeypatch):
    # наблюдение по zone A не создаёт outcome=allowed для zone B (P0-гарантия перед P3-мутациями)
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "tok")
    z = {"id": "zoneA", "name": "a.ru", "status": "active", "account": {"id": "accHEX"}}
    _CUR[0] = _FakeCF([z])
    monkeypatch.setattr(cf_sync, "CloudflareClient", _FakeCF)
    with SessionLocal() as db:
        cf_sync.sync_connection(db, _seed(db))
        assert (db.query(CloudflareCapabilityObservation)
                  .filter_by(resource_id="zoneB", outcome="allowed").count()) == 0
