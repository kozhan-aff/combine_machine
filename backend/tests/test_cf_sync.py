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


def test_accounts_read_failure_marks_connection_error(monkeypatch):
    """S7 (аудит 2026-07-18): verify_token прошёл (status=ok), но листинг аккаунтов упал.
    Без явного error-статуса settings_cloudflare.html показывал зелёный «ok» и прятал
    last_error_safe (виден только при status=='error') — отказ синка был невидим оператору.
    Теперь conn.status='error', ошибка видна."""
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "tok")

    class _AccBoom(_FakeCF):
        def list_accounts_paginated(self):
            raise RuntimeError("accounts read denied")

    with SessionLocal() as db:
        c = _seed_conn(db)
        _CURRENT[0] = _AccBoom([])
        monkeypatch.setattr(cf_sync, "CloudflareClient", _AccBoom)
        cf_sync.sync_connection(db, c)
        db.refresh(c)
        assert c.status == "error"
        assert c.last_error_safe and "accounts read denied" in c.last_error_safe


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


def test_universal_ssl_uses_dedicated_endpoint(sqlite_db):
    # universal_ssl больше НЕ обычный zone-setting: свой эндпоинт возвращает {enabled: bool}.
    from app.services import cf_sync
    from app.models.cloudflare import CloudflareZoneMirror
    from app.db import SessionLocal

    KNOWN = set(cf_sync._OBSERVED_SETTINGS)   # universal_ssl СЮДА больше не входит

    class FakeCF:
        def get_zone_setting(self, zid, sid):
            assert sid in KNOWN, f"universal_ssl не должен идти через settings-эндпоинт: {sid}"
            return {"value": "on", "editable": True}
        def get_universal_ssl(self, zid):
            return {"enabled": True}
        def list_dns_paginated(self, zid): return []
        def list_universal_certificate_packs(self, zid): return []
        def get_dnssec(self, zid): return {"status": "active"}

    with SessionLocal() as db:
        m = CloudflareZoneMirror(cf_zone_id="z1", cloudflare_account_id="a1", name="ex.ru")
        db.add(m); db.commit()
        cf_sync._sync_zone_details(db, FakeCF(), m)
        db.commit()
        assert m.universal_ssl_status == "on"


def test_universal_ssl_not_in_observed_settings():
    from app.services import cf_sync
    assert "universal_ssl" not in cf_sync._OBSERVED_SETTINGS


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


def test_legacy_import_token_kind_is_user_even_with_account_id(sqlite_db, monkeypatch):
    # Наличие CLOUDFLARE_ACCOUNT_ID НЕ делает токен account-owned (аудит F1.1).
    from app.config import settings
    from app.services import cf_legacy
    from app.models.cloudflare import CloudflareConnection
    from app.db import SessionLocal
    monkeypatch.setattr(settings, "CLOUDFLARE_API_TOKEN", "tok_live_1234", raising=False)
    monkeypatch.setattr(settings, "CLOUDFLARE_ACCOUNT_ID", "acc_hex_dead", raising=False)
    with SessionLocal() as db:
        cid = cf_legacy.import_legacy_connection(db)
        conn = db.get(CloudflareConnection, cid)
        assert conn.token_kind == "user"              # НЕ "account"
        assert conn.owner_cf_account_id == "acc_hex_dead"   # но account_id сохранён для листинга зон


def test_dns_error_recorded_not_swallowed(sqlite_db):
    from app.services import cf_sync
    from app.models.cloudflare import CloudflareZoneMirror
    from app.db import SessionLocal

    class FakeCF:
        def list_dns_paginated(self, zid): raise RuntimeError("dns boom")
        def get_zone_setting(self, zid, sid): return {"value": "on", "editable": True}
        def get_universal_ssl(self, zid): return {"enabled": True}
        def list_universal_certificate_packs(self, zid): raise RuntimeError("cert boom")
        def get_dnssec(self, zid): return {"status": "active"}

    with SessionLocal() as db:
        m = CloudflareZoneMirror(cf_zone_id="z9", cloudflare_account_id="a1", name="ex.ru")
        db.add(m); db.commit()
        cf_sync._sync_zone_details(db, FakeCF(), m)
        db.commit()
        assert m.dns_error_safe and "dns boom" in m.dns_error_safe
        assert m.cert_error_safe and "cert boom" in m.cert_error_safe
