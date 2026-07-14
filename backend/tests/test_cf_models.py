"""Cloudflare Control Center P0 — mirror-модель. См. docs/superpowers/plans/2026-07-14-cloudflare-p0.md,
задача 1: 8 mirror-таблиц, UNIQUE Site.domain_id, импорт legacy .env-connection."""
import pytest
from sqlalchemy.exc import IntegrityError

from app.db import SessionLocal
from app.models.cloudflare import (
    CloudflareConnection, CloudflareAccount, CloudflareConnectionAccount,
    CloudflareZoneMirror,
)
from app.models.domain import Domain
from app.models.site import Site


def test_zone_mirror_cf_zone_id_is_unique():
    with SessionLocal() as db:
        db.add(CloudflareZoneMirror(cloudflare_account_id="acc_hex_1",
                                    cf_zone_id="zoneAAA", name="a.ru", status="active"))
        db.commit()
        db.add(CloudflareZoneMirror(cloudflare_account_id="acc_hex_2",
                                    cf_zone_id="zoneAAA", name="a.ru", status="active"))
        with pytest.raises(IntegrityError):
            db.commit()


def test_only_one_primary_for_read_per_account():
    """Один cloudflare_account_id может быть привязан к ДВУМ разным connection (несколько
    токенов на аккаунт), но is_primary_for_read=True — не более чем у одного из них. Тест
    обязан использовать два РАЗНЫХ connection: с одним и тем же connection обе строки делили бы
    (connection_id, cloudflare_account_id), и IntegrityError поднял бы композитный uq_conn_account
    ещё до того, как партиал uq_primary_read_per_account вообще был бы проверен — тест прошёл бы,
    даже не будь партиал-индекса (аудит ревью Задачи 1)."""
    with SessionLocal() as db:
        c1 = CloudflareConnection(label="c1", secret_ref="env:CLOUDFLARE_API_TOKEN",
                                  token_kind="user", status="unverified")
        c2 = CloudflareConnection(label="c2", secret_ref="file:second-token",
                                  token_kind="user", status="unverified")
        db.add_all([c1, c2]); db.commit()
        db.add(CloudflareConnectionAccount(connection_id=c1.id, cloudflare_account_id="acc",
                                           status="ok", is_primary_for_read=True))
        db.commit()
        db.add(CloudflareConnectionAccount(connection_id=c2.id, cloudflare_account_id="acc",
                                           status="ok", is_primary_for_read=True))
        with pytest.raises(IntegrityError):
            db.commit()


def test_site_domain_id_is_unique():
    with SessionLocal() as db:
        d = Domain(domain="dup.ru", status="purchased"); db.add(d); db.commit()
        db.add(Site(domain_id=d.id, status="provisioning")); db.commit()
        db.add(Site(domain_id=d.id, status="provisioning"))
        with pytest.raises(IntegrityError):
            db.commit()


def test_new_site_cf_columns_exist():
    with SessionLocal() as db:
        d = Domain(domain="cols.ru", status="purchased"); db.add(d); db.commit()
        s = Site(domain_id=d.id, cf_zone_mirror_id=None, cloudflare_account_id="acc_hex")
        db.add(s); db.commit()
        assert s.cloudflare_account_id == "acc_hex"


def test_account_id_is_external_hex_not_local_pk():
    """cloudflare_account_id в дочерних таблицах — ВНЕШНИЙ hex Cloudflare, не локальный PK
    CloudflareAccount.id. Sync может наблюдать зону раньше, чем появится строка аккаунта —
    без жёсткого FK на CloudflareAccount.id это не должно падать; а когда строка аккаунта
    появляется, дочерняя ссылка совпадает с ней по hex, а не по локальному PK."""
    with SessionLocal() as db:
        # Строки CloudflareAccount с этим hex ЕЩЁ НЕТ — привязка допустима (нет жёсткого FK).
        zm = CloudflareZoneMirror(cloudflare_account_id="deadbeef00112233",
                                  cf_zone_id="zoneBBB", name="b.ru", status="active")
        db.add(zm)
        db.commit()

        acc = CloudflareAccount(cf_account_id="deadbeef00112233", name="Acc")
        db.add(acc)
        db.commit()

        assert zm.cloudflare_account_id == acc.cf_account_id
        assert zm.cloudflare_account_id != acc.id  # совпадение по hex, не по локальному PK


def test_import_legacy_connection_idempotent(monkeypatch):
    from app.config import settings
    from app.services import cf_legacy

    monkeypatch.setattr(settings, "CLOUDFLARE_API_TOKEN", "sekrit-token-value-1234")
    monkeypatch.setattr(settings, "CLOUDFLARE_ACCOUNT_ID", "acc_hex_legacy")

    with SessionLocal() as db:
        id1 = cf_legacy.import_legacy_connection(db)
        id2 = cf_legacy.import_legacy_connection(db)
        assert id1 is not None
        assert id1 == id2

        rows = db.query(CloudflareConnection).all()
        assert len(rows) == 1
        conn = rows[0]
        assert conn.secret_ref == "env:CLOUDFLARE_API_TOKEN"
        # Токен нигде не осел в строке БД.
        assert "sekrit-token-value-1234" not in repr(conn.__dict__)


def test_import_legacy_connection_empty_token_is_noop(monkeypatch):
    from app.config import settings
    from app.services import cf_legacy

    monkeypatch.setattr(settings, "CLOUDFLARE_API_TOKEN", "")

    with SessionLocal() as db:
        result = cf_legacy.import_legacy_connection(db)
        assert result is None
        assert db.query(CloudflareConnection).count() == 0
