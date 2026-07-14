"""Cloudflare Control Center P0 — mirror-модель. См. docs/superpowers/plans/2026-07-14-cloudflare-p0.md,
задача 1: 8 mirror-таблиц, UNIQUE Site.domain_id, импорт legacy .env-connection."""
import pathlib

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


def test_migration_0016_dedup_partitions_collisions_by_keeper_and_url_path():
    """Текст-гард на дедуп-DELETE миграции 0016 — по образцу
    test_page_uniqueness.py::test_migration_deletes_index_history_before_pages (0014):
    сама миграция Postgres-only и в pytest-сьюте не гоняется (SQLite-харнесс — create_all, а
    не alembic upgrade), поэтому единственный автоматический сторож — текст.

    Инвариант: коллизия url_path обязана считаться ROW_NUMBER()-ом, партиционированным СРАЗУ по
    (keeper, url_path) для ВСЕХ страниц, которые метят в keeper (и страниц самого keeper, и
    страниц любого числа проигравших) — а не проверяться попарно "проигравший против keeper".
    При 3+ Site-дублях на один домен два РАЗНЫХ проигравших могут делить url_path, которого нет
    у keeper: старая (до фикса ревью Задачи 1) форма это не ловила, коллизия доживала до
    `UPDATE pages SET site_id = keeper` и падала на `uq_page_per_path` (0014) — UniqueViolation
    рвал транзакцию миграции и ронял git-pull-деплой (F-migration-0016, воспроизведено на живом
    Postgres 16, см. отчёт ревью). Откат к старой форме молча проходит весь остальной сьют —
    ни одного другого теста, который это заметил бы, нет.
    """
    path = (pathlib.Path(__file__).resolve().parents[1] / "alembic" / "versions"
            / "0016_cloudflare_mirrors.py")
    src = path.read_text(encoding="utf-8")
    up = src[src.index("def upgrade"):src.index("def downgrade")]

    first = up.find("op.execute(")
    assert first != -1, "миграция обязана дедуплицировать Page ДО UNIQUE(sites.domain_id)"
    second = up.find("op.execute(", first + 1)
    assert second != -1, "ожидались минимум два op.execute (дедуп-DELETE, затем перенос UPDATE)"
    dedup_query = up[first:second]

    assert "PARTITION BY r.keeper, pg.url_path" in dedup_query, (
        "первый op.execute обязан ранжировать ROW_NUMBER() по ЕДИНОЙ группе (keeper, url_path) "
        "по всем страницам сразу — иначе два РАЗНЫХ проигравших с общим url_path не поймаются "
        "как коллизия, и перенос ниже уронит uq_page_per_path на живом Postgres"
    )
