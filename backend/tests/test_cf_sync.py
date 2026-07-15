from app.db import SessionLocal
from app.models.cloudflare import (CloudflareConnection, CloudflareConnectionAccount,
                                    CloudflareZoneMirror)
from app.models.domain import Domain
from app.models.site import Site
import app.services.cf_sync as cf_sync


class _FakeCF:
    def __init__(self, zones, dns=None, boom_zone=False):
        self._zones, self._dns, self._boom = zones, dns or [], boom_zone
    @classmethod
    def with_token(cls, *a, **k): return _CURRENT[0]
    def verify_token(self, kind, account_id=""): return {"status": "active"}
    def list_accounts_paginated(self): return [{"id": "accHEX", "name": "Acc"}]
    def list_zones_paginated(self, account_id):
        if self._boom: raise RuntimeError("token scope")
        return self._zones
    def find_zone_in_account(self, name, account_id):
        return next((z for z in self._zones if z["name"] == name), None)
    def list_dns_paginated(self, zone_id, type=None, name=None): return self._dns
    def get_zone_setting(self, zone_id, setting_id):
        val = "on" if setting_id == "universal_ssl" else "off"
        return {"id": setting_id, "value": val, "editable": True}
    def list_universal_certificate_packs(self, zone_id): return []
    def get_dnssec(self, zone_id): return {"status": "disabled"}

_CURRENT = [None]


def _seed_conn(db):
    c = CloudflareConnection(label="t", secret_ref="env:CLOUDFLARE_API_TOKEN",
                             token_kind="user", status="unverified")
    db.add(c); db.commit(); return c


def test_upsert_does_not_duplicate_zone(monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "tok")
    z = {"id": "zid1", "name": "a.ru", "status": "active",
         "account": {"id": "accHEX"}, "name_servers": ["ns1", "ns2"]}
    _CURRENT[0] = _FakeCF([z])
    monkeypatch.setattr(cf_sync, "CloudflareClient", _FakeCF)
    with SessionLocal() as db:
        c = _seed_conn(db)
        cf_sync.sync_connection(db, c)
        cf_sync.sync_connection(db, c)
        assert db.query(CloudflareZoneMirror).filter_by(cf_zone_id="zid1").count() == 1


def test_failed_zone_list_does_not_mark_deleted(monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "tok")
    z = {"id": "zid1", "name": "a.ru", "status": "active", "account": {"id": "accHEX"}}
    with SessionLocal() as db:
        c = _seed_conn(db)
        _CURRENT[0] = _FakeCF([z]); monkeypatch.setattr(cf_sync, "CloudflareClient", _FakeCF)
        cf_sync.sync_connection(db, c)
        # второй прогон — token scope сузился, list падает
        _CURRENT[0] = _FakeCF([z], boom_zone=True)
        cf_sync.sync_connection(db, c)
        m = db.query(CloudflareZoneMirror).filter_by(cf_zone_id="zid1").one()
        assert m.status != "deleted"


def test_empty_zone_list_marks_missing_not_deleted(monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "tok")
    z = {"id": "zid1", "name": "a.ru", "status": "active", "account": {"id": "accHEX"}}
    with SessionLocal() as db:
        c = _seed_conn(db)
        _CURRENT[0] = _FakeCF([z]); monkeypatch.setattr(cf_sync, "CloudflareClient", _FakeCF)
        cf_sync.sync_connection(db, c)
        _CURRENT[0] = _FakeCF([])  # успешный, но пустой
        cf_sync.sync_connection(db, c)
        m = db.query(CloudflareZoneMirror).filter_by(cf_zone_id="zid1").one()
        assert m.status != "deleted" and m.missing_since is not None


def test_sync_does_not_touch_domain_or_site(monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "tok")
    z = {"id": "zid1", "name": "a.ru", "status": "active", "account": {"id": "accHEX"}}
    _CURRENT[0] = _FakeCF([z]); monkeypatch.setattr(cf_sync, "CloudflareClient", _FakeCF)
    with SessionLocal() as db:
        c = _seed_conn(db)
        cf_sync.sync_connection(db, c)
        assert db.query(Domain).count() == 0 and db.query(Site).count() == 0


def test_account_id_stored_is_external_hex(monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "tok")
    z = {"id": "zid1", "name": "a.ru", "status": "active", "account": {"id": "accHEX"}}
    _CURRENT[0] = _FakeCF([z]); monkeypatch.setattr(cf_sync, "CloudflareClient", _FakeCF)
    with SessionLocal() as db:
        c = _seed_conn(db)
        cf_sync.sync_connection(db, c)
        m = db.query(CloudflareZoneMirror).filter_by(cf_zone_id="zid1").one()
        assert m.cloudflare_account_id == "accHEX"


def test_connection_account_capabilities_recorded(monkeypatch):
    # без этого capability-чипы в UI (задача 7) всегда пусты
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "tok")
    z = {"id": "zid1", "name": "a.ru", "status": "active", "account": {"id": "accHEX"}}
    _CURRENT[0] = _FakeCF([z]); monkeypatch.setattr(cf_sync, "CloudflareClient", _FakeCF)
    with SessionLocal() as db:
        c = _seed_conn(db)
        cf_sync.sync_connection(db, c)
        ca = (db.query(CloudflareConnectionAccount)
                .filter_by(connection_id=c.id, cloudflare_account_id="accHEX").one())
        assert (ca.capabilities_json or {}).get("zones_read") == "allowed"


def test_universal_ssl_status_recorded_from_setting(monkeypatch):
    # SSL-колонка UI читает m.universal_ssl_status — sync обязан его писать из наблюдения setting
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "tok")
    z = {"id": "zid1", "name": "a.ru", "status": "active", "account": {"id": "accHEX"}}
    _CURRENT[0] = _FakeCF([z]); monkeypatch.setattr(cf_sync, "CloudflareClient", _FakeCF)
    with SessionLocal() as db:
        c = _seed_conn(db)
        cf_sync.sync_connection(db, c)
        m = db.query(CloudflareZoneMirror).filter_by(cf_zone_id="zid1").one()
        assert m.universal_ssl_status == "on"


def test_backfill_links_legacy_site_to_mirror(monkeypatch):
    # §2.6: legacy Site.cf_zone_id → cf_zone_mirror_id + cloudflare_account_id из mirror
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "tok")
    z = {"id": "zid1", "name": "a.ru", "status": "active", "account": {"id": "accHEX"}}
    _CURRENT[0] = _FakeCF([z]); monkeypatch.setattr(cf_sync, "CloudflareClient", _FakeCF)
    with SessionLocal() as db:
        d = Domain(domain="a.ru", status="purchased"); db.add(d); db.commit()
        db.add(Site(domain_id=d.id, status="live", cf_zone_id="zid1")); db.commit()
        _seed_conn(db)
        cf_sync.sync_all(db)
        s = db.query(Site).filter_by(cf_zone_id="zid1").one()
        m = db.query(CloudflareZoneMirror).filter_by(cf_zone_id="zid1").one()
        assert s.cf_zone_mirror_id == m.id and s.cloudflare_account_id == "accHEX"


def test_backfill_ignores_site_without_cf_zone_id(monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "tok")
    _CURRENT[0] = _FakeCF([]); monkeypatch.setattr(cf_sync, "CloudflareClient", _FakeCF)
    with SessionLocal() as db:
        d = Domain(domain="b.ru", status="purchased"); db.add(d); db.commit()
        db.add(Site(domain_id=d.id, status="live")); db.commit()
        _seed_conn(db)
        cf_sync.sync_all(db)
        s = db.query(Site).filter_by(domain_id=d.id).one()
        assert s.cf_zone_mirror_id is None and s.cloudflare_account_id is None
