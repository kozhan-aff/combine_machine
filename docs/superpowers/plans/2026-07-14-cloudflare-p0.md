# План: Cloudflare Control Center — фаза P0 (read-only правда)

> **Для агентов:** REQUIRED SUB-SKILL — исполнять через `superpowers:subagent-driven-development`
> (в проекте так делали 6+ раз) либо `superpowers:executing-plans`. Шаги — чекбоксы (`- [ ]`).
> Источник требований — аудит `.codex/cloudflare_panel_review` (README/DONOR_AUDIT/
> FUNCTIONAL_INVENTORY/INTEGRATION_PLAN/RECHECK_2026-07-14). Стиль — как
> `docs/superpowers/plans/2026-07-14-audit-fixes.md`.

**Цель:** дать вкладке Cloudflare правдивую read-only основу — безопасную модель данных
(8 mirror-таблиц + `secret_ref` без хранения токена в БД), account-aware транспорт с полной
пагинацией и verify по типу токена, sync-сервис, который отличает «пусто» от «ошибки» и никогда не
метит зоны удалёнными по пустому списку, и read-only экран `/settings/cloudflare`. **Ни одной
Cloudflare-мутации** в этой фазе.

**Архитектура:** новый предметный модуль моделей `models/cloudflare.py`; транспорт остаётся в
`integrations/cloudflare.py` (только read-методы, mutation-методы не трогаем и не добавляем);
бизнес-логика синхронизации — в новом `services/cf_sync.py`; `services/provisioning.py` остаётся
единственным владельцем цепочки zone→NS→DNS→aaPanel→TLS и в P0 **не меняется**. Экран —
под-вкладка `/settings` по образцу `/domains`↔`/domains/pool` (без нового пункта сайдбара).

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.x (`Mapped[...]`), Pydantic v2, PostgreSQL 16 +
Alembic, httpx (через `BaseClient` с ретраем), Jinja2 + HTMX-панель. Тесты — pytest на
SQLite-harness, герметичные.

## Global Constraints

- **Хард-гейты проекта не двигаются ни одной фазой Cloudflare.** Заказ провайдеру — только при
  `confirmed_by_human=true`; публикация — только из `edited`; `mark_edited` зовёт человек;
  оркестратор их не зовёт. Ни одна `cf_*`-задача их не касается.
- **P0 — ТОЛЬКО read-only. Ни один шаг плана не вызывает Cloudflare PATCH/POST/DELETE — только
  GET/verify.** Массовая мутация (Always HTTPS / SSL strict / TLS) запрещена этой фазой ЯВНО
  (аудит §9 «Нельзя начинать с массовой кнопки TLS/HTTPS», §11). `set_ssl`/`add_a_record`/
  `update_a_record`/`add_txt_record`/`create_zone` в `cloudflare.py` не вызываются из нового кода.
- **Номер миграции ПЕРЕПРОВЕРИТЬ перед исполнением:** задача 1 называет `0015`, но перед `Create`
  выполнить `ls backend/alembic/versions/` — голова Alembic могла сдвинуться, если между написанием
  плана и исполнением прошли другие ветки. Прецедент — Задача 11 в `2026-07-14-audit-fixes.md`:
  бриф называл `0012`, реальным свободным номером оказался `0014`. Взять фактический `head`+1,
  `down_revision` = фактический head.
- Тесты герметичны: autouse-фикстура `_no_live_network` рубит сеть (ловушки — наследники
  `BaseException`); юнит-тесты транспорта подменяют `request`/`_client` на **инстансе** клиента.
- pyflakes чист (`.venv/bin/python -m pyflakes backend/app backend/tests`). UI на русском.
  CSS-классы — только из `base.html` (новые не заводить — запрет `docs/DESIGN.md`).
- Коммит-мессаджи через `git commit -F -` (heredoc), не `-m`. Каждая задача = один коммит.
- Ревью — `combine-reviewer` (opus) после каждой задачи.
- **Каждая задача несёт регрессию, которая ПАДАЕТ до фикса** (на текущем `head`). Тест, зелёный
  до реализации, — это не тест.

---

## Задача 1: модель данных — 8 mirror-таблиц + UNIQUE `Site.domain_id` + импорт legacy .env

**Files:**
- Create: `backend/app/models/cloudflare.py`
- Modify: `backend/app/models/__init__.py`, `backend/app/models/site.py`
- Create: `backend/alembic/versions/0015_cloudflare_mirrors.py` (**номер перепроверить**, см. Global
  Constraints)
- Create: `backend/app/services/cf_legacy.py` (импорт .env-connection как legacy-строки)
- Test: `backend/tests/test_cf_models.py`

**Interfaces:**
- Produces (для задач 2–7): ORM-классы `CloudflareConnection`, `CloudflareAccount`,
  `CloudflareConnectionAccount`, `CloudflareCapabilityObservation`, `CloudflareZoneMirror`,
  `CloudflareZoneSettingObservation`, `CloudflareDnsRecordMirror`, `CloudflareCertificatePackMirror`;
  новые поля `Site.cf_zone_mirror_id: int | None`, `Site.cloudflare_account_id: str | None`; функция
  `cf_legacy.import_legacy_connection(db) -> int | None` (id созданной/существующей legacy-строки
  либо None, если `CLOUDFLARE_API_TOKEN` пуст).
- Consumes: `app.db.Base`, `settings`.

**Проектные решения (зафиксировать при ревью):**
- `cloudflare_account_id` во всех дочерних таблицах — это **внешний hex-ID Cloudflare** (String),
  а НЕ локальный PK. Связь с `CloudflareAccount` — по внешнему id, без жёсткого FK: sync может
  наблюдать зону раньше, чем появится строка аккаунта, и identity аккаунта в проекте — его внешний
  hex (аудит §2, тест «account ID всегда внешний hex, не локальный PK»).
- `secret` НЕ хранится в БД даже зашифрованным — только `secret_ref` (задача 2 его резолвит).
- **Новые колонки `Site.cf_zone_mirror_id`/`Site.cloudflare_account_id` создаются ПУСТЫМИ** — на
  момент миграции mirror-таблицы ещё пусты, привязывать не к чему. Их **backfill** для legacy-Site
  (§2.6 аудита) делает `cf_sync._backfill_site_links` в конце `sync_all` (задача 4), когда зоны уже
  наблюдены: `Site.cf_zone_id` (legacy) → `cf_zone_mirror_id` + `cloudflare_account_id` из mirror
  (внешний hex), fallback на `settings.CLOUDFLARE_ACCOUNT_ID`. Здесь колонки только заводятся.
- `brotli` намеренно отсутствует (Zone Setting API deprecated 2024-08-15).
- Партиал-unique дублируются под оба диалекта (`postgresql_where`+`sqlite_where`) — как
  `uq_open_order_per_domain`/`uq_job_run_running`.

- [ ] **Шаг 1. Падающий тест** (`test_cf_models.py`):

```python
import math
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
    with SessionLocal() as db:
        c = CloudflareConnection(label="c", secret_ref="env:CLOUDFLARE_API_TOKEN",
                                 token_kind="user", status="unverified")
        db.add(c); db.commit()
        db.add(CloudflareConnectionAccount(connection_id=c.id, cloudflare_account_id="acc",
                                           status="ok", is_primary_for_read=True))
        db.commit()
        db.add(CloudflareConnectionAccount(connection_id=c.id, cloudflare_account_id="acc",
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
```

- [ ] **Шаг 2. Запустить — убедиться, что падает.** `docker compose run --rm backend pytest
  backend/tests/test_cf_models.py -q` → FAIL (`ModuleNotFoundError: app.models.cloudflare`).

- [ ] **Шаг 3. Модель** — `backend/app/models/cloudflare.py` (стиль job.py: python-side `_utcnow`,
  партиал-unique под оба диалекта):

```python
"""Cloudflare Control Center — mirror-таблицы (P0, read-only правда).

Секрет токена НЕ хранится в БД даже зашифрованным — только `secret_ref` (env:... либо safe
filename в allowlisted read-only mount), резолвится в services/cf_secret.py. `cloudflare_account_id`
во всех дочерних строках — ВНЕШНИЙ hex-ID Cloudflare, не локальный PK (identity аккаунта = его hex).
brotli намеренно отсутствует: Zone Setting API deprecated 2024-08-15. Пустой список при ошибке НЕ
значит «удалено» — sync ставит missing_since/last_error_safe, а не status='deleted' (аудит §2)."""
from __future__ import annotations
from datetime import datetime, timezone

from sqlalchemy import ForeignKey, Index, String, Text, Boolean, Integer, DateTime, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class CloudflareConnection(Base):
    __tablename__ = "cloudflare_connections"
    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(String(120), default="")
    auth_type: Mapped[str] = mapped_column(String(24), default="bearer_token")
    token_kind: Mapped[str] = mapped_column(String(16), default="user")  # user|account
    owner_cf_account_id: Mapped[str | None] = mapped_column(String(64))  # для account-owned токена
    secret_ref: Mapped[str] = mapped_column(String(255))  # env:NAME | file:basename
    token_fingerprint: Mapped[str | None] = mapped_column(String(128))   # sha256 первых N; НЕ токен
    token_hint: Mapped[str | None] = mapped_column(String(24))           # напр. "...AB12" (хвост)
    status: Mapped[str] = mapped_column(String(24), default="unverified")  # unverified|ok|error
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(32))
    last_error_safe: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow,
                                                 onupdate=_utcnow)


class CloudflareAccount(Base):
    __tablename__ = "cloudflare_accounts"
    id: Mapped[int] = mapped_column(primary_key=True)
    cf_account_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)  # внешний hex
    name: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(24), default="unknown")
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_safe: Mapped[str | None] = mapped_column(Text)


class CloudflareConnectionAccount(Base):
    __tablename__ = "cloudflare_connection_accounts"
    id: Mapped[int] = mapped_column(primary_key=True)
    connection_id: Mapped[int] = mapped_column(ForeignKey("cloudflare_connections.id"), index=True)
    cloudflare_account_id: Mapped[str] = mapped_column(String(64), index=True)  # внешний hex
    status: Mapped[str] = mapped_column(String(24), default="unknown")
    capabilities_json: Mapped[dict | None] = mapped_column(JSONB)
    is_primary_for_read: Mapped[bool] = mapped_column(Boolean, default=False)
    is_primary_for_provision: Mapped[bool] = mapped_column(Boolean, default=False)
    last_probed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_safe: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (
        Index("uq_conn_account", "connection_id", "cloudflare_account_id", unique=True),
        Index("uq_primary_read_per_account", "cloudflare_account_id", unique=True,
              postgresql_where=text("is_primary_for_read"),
              sqlite_where=text("is_primary_for_read")),
        Index("uq_primary_provision_per_account", "cloudflare_account_id", unique=True,
              postgresql_where=text("is_primary_for_provision"),
              sqlite_where=text("is_primary_for_provision")),
    )


class CloudflareCapabilityObservation(Base):
    __tablename__ = "cloudflare_capability_observations"
    id: Mapped[int] = mapped_column(primary_key=True)
    connection_id: Mapped[int] = mapped_column(ForeignKey("cloudflare_connections.id"), index=True)
    cloudflare_account_id: Mapped[str | None] = mapped_column(String(64), index=True)
    resource_type: Mapped[str] = mapped_column(String(16))   # account|zone
    resource_id: Mapped[str | None] = mapped_column(String(64))
    capability: Mapped[str] = mapped_column(String(48))       # token_active|accounts_read|...
    outcome: Mapped[str] = mapped_column(String(12), default="unknown")  # allowed|denied|unknown
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    safe_error: Mapped[str | None] = mapped_column(Text)


class CloudflareZoneMirror(Base):
    __tablename__ = "cloudflare_zone_mirrors"
    id: Mapped[int] = mapped_column(primary_key=True)
    cloudflare_account_id: Mapped[str] = mapped_column(String(64), index=True)  # внешний hex
    cf_zone_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)  # внешний hex
    name: Mapped[str] = mapped_column(String(255), index=True)
    status: Mapped[str] = mapped_column(String(16), default="unknown")
    # initializing|pending|active|moved|deleted|unknown
    plan_name: Mapped[str | None] = mapped_column(String(64))
    name_servers_json: Mapped[list | None] = mapped_column(JSONB)
    original_name_servers_json: Mapped[list | None] = mapped_column(JSONB)
    observed_authoritative_ns_json: Mapped[list | None] = mapped_column(JSONB)
    ns_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ns_error_safe: Mapped[str | None] = mapped_column(Text)
    parent_ds_json: Mapped[dict | None] = mapped_column(JSONB)
    parent_ds_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    parent_ds_error_safe: Mapped[str | None] = mapped_column(Text)
    paused: Mapped[bool | None] = mapped_column(Boolean)
    dnssec_status: Mapped[str | None] = mapped_column(String(24))
    dnssec_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dnssec_error_safe: Mapped[str | None] = mapped_column(Text)
    universal_ssl_status: Mapped[str | None] = mapped_column(String(24))
    origin_tls_status: Mapped[str] = mapped_column(String(12), default="unknown")  # unknown|ready|failed
    origin_tls_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    origin_tls_error_safe: Mapped[str | None] = mapped_column(Text)
    origin_cert_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    zones_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    missing_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_safe: Mapped[str | None] = mapped_column(Text)


class CloudflareZoneSettingObservation(Base):
    __tablename__ = "cloudflare_zone_setting_observations"
    id: Mapped[int] = mapped_column(primary_key=True)
    cloudflare_zone_id: Mapped[str] = mapped_column(String(64), index=True)  # внешний hex зоны
    setting_id: Mapped[str] = mapped_column(String(64))
    value_json: Mapped[dict | list | str | int | bool | None] = mapped_column(JSONB)
    editable: Mapped[bool | None] = mapped_column(Boolean)
    status: Mapped[str] = mapped_column(String(16), default="unknown")  # observed|unsupported|error|unknown
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    error_safe: Mapped[str | None] = mapped_column(Text)
    desired_profile_version: Mapped[str | None] = mapped_column(String(32))
    drift_status: Mapped[str | None] = mapped_column(String(16))  # P2 заполняет; P0 оставляет NULL
    __table_args__ = (
        Index("uq_zone_setting", "cloudflare_zone_id", "setting_id", unique=True),
    )


class CloudflareDnsRecordMirror(Base):
    __tablename__ = "cloudflare_dns_record_mirrors"
    id: Mapped[int] = mapped_column(primary_key=True)
    cloudflare_zone_id: Mapped[str] = mapped_column(String(64), index=True)
    cf_record_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    type: Mapped[str] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(String(255))
    content: Mapped[str | None] = mapped_column(Text)   # content ИЛИ сериализованный data
    ttl: Mapped[int | None] = mapped_column(Integer)
    proxied: Mapped[bool | None] = mapped_column(Boolean)
    managed_role: Mapped[str | None] = mapped_column(String(24))  # apex_origin — ТОЛЬКО M3/adoption
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    missing_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_safe: Mapped[str | None] = mapped_column(Text)


class CloudflareCertificatePackMirror(Base):
    __tablename__ = "cloudflare_certificate_pack_mirrors"
    id: Mapped[int] = mapped_column(primary_key=True)
    cloudflare_zone_id: Mapped[str] = mapped_column(String(64), index=True)
    cf_pack_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    type: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[str | None] = mapped_column(String(24))
    hosts_json: Mapped[list | None] = mapped_column(JSONB)
    certificates_json: Mapped[list | None] = mapped_column(JSONB)  # [{issuer,fingerprint,not_before,expires_on}]
    validation_errors_safe: Mapped[str | None] = mapped_column(Text)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    missing_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
```

- [ ] **Шаг 4. Site: новые колонки + relationship-нейтральные поля** — `models/site.py`, добавить
  рядом с `cf_zone_id` (legacy оставить read-only на время backfill):

```python
    cf_zone_mirror_id: Mapped[int | None] = mapped_column(ForeignKey("cloudflare_zone_mirrors.id"))
    cloudflare_account_id: Mapped[str | None] = mapped_column(String(64))  # desired target (внешний hex)
```

- [ ] **Шаг 5. Регистрация моделей** — `models/__init__.py`: добавить
  `from app.models import cloudflare  # noqa: F401` (иначе `create_all`/autogenerate не увидят
  таблицы — тот же урок, что F23 в audit-fixes).

- [ ] **Шаг 6. Миграция** `0015_cloudflare_mirrors.py` (**перепроверить номер!**). `upgrade()`:
  `op.create_table(...)` для всех 8 таблиц (типы как в модели; JSONB под postgres — в тестах
  SQLite `create_all` из conftest создаёт схему, миграция гоняется на проде), затем индексы
  (`op.create_index(...)` с `postgresql_where`/`sqlite_where` для трёх партиал-unique), затем
  колонки Site и дедуп перед UNIQUE:

```python
# файлу нужен импорт JSONB: `from sqlalchemy.dialects import postgresql` (рядом с `import sqlalchemy as sa`)

def upgrade() -> None:
    op.create_table(
        "cloudflare_connections",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("label", sa.String(length=120), nullable=False, server_default=""),
        sa.Column("auth_type", sa.String(length=24), nullable=False, server_default="bearer_token"),
        sa.Column("token_kind", sa.String(length=16), nullable=False, server_default="user"),
        sa.Column("owner_cf_account_id", sa.String(length=64), nullable=True),
        sa.Column("secret_ref", sa.String(length=255), nullable=False),
        sa.Column("token_fingerprint", sa.String(length=128), nullable=True),
        sa.Column("token_hint", sa.String(length=24), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="unverified"),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=32), nullable=True),
        sa.Column("last_error_safe", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "cloudflare_accounts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("cf_account_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="unknown"),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_safe", sa.Text(), nullable=True),
    )
    op.create_index("ix_cloudflare_accounts_cf_account_id", "cloudflare_accounts",
                    ["cf_account_id"], unique=True)
    op.create_table(
        "cloudflare_connection_accounts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("connection_id", sa.Integer(),
                  sa.ForeignKey("cloudflare_connections.id"), nullable=False),
        sa.Column("cloudflare_account_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="unknown"),
        sa.Column("capabilities_json", postgresql.JSONB(), nullable=True),
        sa.Column("is_primary_for_read", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_primary_for_provision", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("last_probed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_safe", sa.Text(), nullable=True),
    )
    op.create_index("ix_cloudflare_connection_accounts_connection_id",
                    "cloudflare_connection_accounts", ["connection_id"])
    op.create_index("ix_cloudflare_connection_accounts_cloudflare_account_id",
                    "cloudflare_connection_accounts", ["cloudflare_account_id"])
    op.create_index("uq_conn_account", "cloudflare_connection_accounts",
                    ["connection_id", "cloudflare_account_id"], unique=True)
    op.create_index("uq_primary_read_per_account", "cloudflare_connection_accounts",
                    ["cloudflare_account_id"], unique=True,
                    postgresql_where=sa.text("is_primary_for_read"),
                    sqlite_where=sa.text("is_primary_for_read"))
    op.create_index("uq_primary_provision_per_account", "cloudflare_connection_accounts",
                    ["cloudflare_account_id"], unique=True,
                    postgresql_where=sa.text("is_primary_for_provision"),
                    sqlite_where=sa.text("is_primary_for_provision"))
    op.create_table(
        "cloudflare_capability_observations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("connection_id", sa.Integer(),
                  sa.ForeignKey("cloudflare_connections.id"), nullable=False),
        sa.Column("cloudflare_account_id", sa.String(length=64), nullable=True),
        sa.Column("resource_type", sa.String(length=16), nullable=False),
        sa.Column("resource_id", sa.String(length=64), nullable=True),
        sa.Column("capability", sa.String(length=48), nullable=False),
        sa.Column("outcome", sa.String(length=12), nullable=False, server_default="unknown"),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("safe_error", sa.Text(), nullable=True),
    )
    op.create_index("ix_cloudflare_capability_observations_connection_id",
                    "cloudflare_capability_observations", ["connection_id"])
    op.create_index("ix_cloudflare_capability_observations_cloudflare_account_id",
                    "cloudflare_capability_observations", ["cloudflare_account_id"])
    op.create_table(
        "cloudflare_zone_mirrors",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("cloudflare_account_id", sa.String(length=64), nullable=False),
        sa.Column("cf_zone_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="unknown"),
        sa.Column("plan_name", sa.String(length=64), nullable=True),
        sa.Column("name_servers_json", postgresql.JSONB(), nullable=True),
        sa.Column("original_name_servers_json", postgresql.JSONB(), nullable=True),
        sa.Column("observed_authoritative_ns_json", postgresql.JSONB(), nullable=True),
        sa.Column("ns_observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ns_error_safe", sa.Text(), nullable=True),
        sa.Column("parent_ds_json", postgresql.JSONB(), nullable=True),
        sa.Column("parent_ds_observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("parent_ds_error_safe", sa.Text(), nullable=True),
        sa.Column("paused", sa.Boolean(), nullable=True),
        sa.Column("dnssec_status", sa.String(length=24), nullable=True),
        sa.Column("dnssec_observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dnssec_error_safe", sa.Text(), nullable=True),
        sa.Column("universal_ssl_status", sa.String(length=24), nullable=True),
        sa.Column("origin_tls_status", sa.String(length=12), nullable=False, server_default="unknown"),
        sa.Column("origin_tls_observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("origin_tls_error_safe", sa.Text(), nullable=True),
        sa.Column("origin_cert_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("zones_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("missing_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_safe", sa.Text(), nullable=True),
    )
    op.create_index("ix_cloudflare_zone_mirrors_cloudflare_account_id",
                    "cloudflare_zone_mirrors", ["cloudflare_account_id"])
    op.create_index("ix_cloudflare_zone_mirrors_cf_zone_id",
                    "cloudflare_zone_mirrors", ["cf_zone_id"], unique=True)
    op.create_index("ix_cloudflare_zone_mirrors_name", "cloudflare_zone_mirrors", ["name"])
    op.create_table(
        "cloudflare_zone_setting_observations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("cloudflare_zone_id", sa.String(length=64), nullable=False),
        sa.Column("setting_id", sa.String(length=64), nullable=False),
        sa.Column("value_json", postgresql.JSONB(), nullable=True),
        sa.Column("editable", sa.Boolean(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="unknown"),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("error_safe", sa.Text(), nullable=True),
        sa.Column("desired_profile_version", sa.String(length=32), nullable=True),
        sa.Column("drift_status", sa.String(length=16), nullable=True),
    )
    op.create_index("ix_cloudflare_zone_setting_observations_cloudflare_zone_id",
                    "cloudflare_zone_setting_observations", ["cloudflare_zone_id"])
    op.create_index("uq_zone_setting", "cloudflare_zone_setting_observations",
                    ["cloudflare_zone_id", "setting_id"], unique=True)
    op.create_table(
        "cloudflare_dns_record_mirrors",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("cloudflare_zone_id", sa.String(length=64), nullable=False),
        sa.Column("cf_record_id", sa.String(length=64), nullable=False),
        sa.Column("type", sa.String(length=16), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("ttl", sa.Integer(), nullable=True),
        sa.Column("proxied", sa.Boolean(), nullable=True),
        sa.Column("managed_role", sa.String(length=24), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("missing_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_safe", sa.Text(), nullable=True),
    )
    op.create_index("ix_cloudflare_dns_record_mirrors_cloudflare_zone_id",
                    "cloudflare_dns_record_mirrors", ["cloudflare_zone_id"])
    op.create_index("ix_cloudflare_dns_record_mirrors_cf_record_id",
                    "cloudflare_dns_record_mirrors", ["cf_record_id"], unique=True)
    op.create_table(
        "cloudflare_certificate_pack_mirrors",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("cloudflare_zone_id", sa.String(length=64), nullable=False),
        sa.Column("cf_pack_id", sa.String(length=64), nullable=False),
        sa.Column("type", sa.String(length=32), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=True),
        sa.Column("hosts_json", postgresql.JSONB(), nullable=True),
        sa.Column("certificates_json", postgresql.JSONB(), nullable=True),
        sa.Column("validation_errors_safe", sa.Text(), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("missing_since", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_cloudflare_certificate_pack_mirrors_cloudflare_zone_id",
                    "cloudflare_certificate_pack_mirrors", ["cloudflare_zone_id"])
    op.create_index("ix_cloudflare_certificate_pack_mirrors_cf_pack_id",
                    "cloudflare_certificate_pack_mirrors", ["cf_pack_id"], unique=True)
    op.add_column("sites", sa.Column("cf_zone_mirror_id", sa.Integer(), nullable=True))
    op.add_column("sites", sa.Column("cloudflare_account_id", sa.String(length=64), nullable=True))
    # Дедуп перед UNIQUE(domain_id): гонка panel/worker могла создать два Site на один Domain.
    # Keeper = сайт с наибольшим числом страниц (тай-брейк — меньший id). Страницы проигравших
    # переносим на keeper, при коллизии url_path (uq_page_per_path из 0014) — коллизии удаляем.
    op.execute("""
        WITH ranked AS (
            SELECT s.id, s.domain_id,
                   ROW_NUMBER() OVER (
                       PARTITION BY s.domain_id
                       ORDER BY (SELECT count(*) FROM pages p WHERE p.site_id = s.id) DESC, s.id ASC
                   ) AS rn,
                   FIRST_VALUE(s.id) OVER (
                       PARTITION BY s.domain_id
                       ORDER BY (SELECT count(*) FROM pages p WHERE p.site_id = s.id) DESC, s.id ASC
                   ) AS keeper
            FROM sites s
        )
        DELETE FROM pages
        WHERE site_id IN (SELECT id FROM ranked WHERE rn > 1)
          AND EXISTS (
              SELECT 1 FROM pages k
              JOIN ranked r ON r.keeper = k.site_id
              WHERE r.id = pages.site_id AND k.url_path = pages.url_path
          );
    """)
    op.execute("""
        WITH ranked AS (
            SELECT s.id AS loser, s.domain_id,
                   FIRST_VALUE(s.id) OVER (
                       PARTITION BY s.domain_id
                       ORDER BY (SELECT count(*) FROM pages p WHERE p.site_id = s.id) DESC, s.id ASC
                   ) AS keeper
            FROM sites s
        )
        UPDATE pages SET site_id = r.keeper
        FROM ranked r WHERE pages.site_id = r.loser AND r.loser <> r.keeper;
    """)
    op.execute("""
        WITH ranked AS (
            SELECT s.id AS loser, s.domain_id,
                   FIRST_VALUE(s.id) OVER (
                       PARTITION BY s.domain_id
                       ORDER BY (SELECT count(*) FROM pages p WHERE p.site_id = s.id) DESC, s.id ASC
                   ) AS keeper
            FROM sites s
        )
        DELETE FROM sites WHERE id IN (SELECT loser FROM ranked WHERE loser <> keeper);
    """)
    op.create_index("uq_site_per_domain", "sites", ["domain_id"], unique=True)


def downgrade() -> None:
    op.drop_index("uq_site_per_domain", table_name="sites")
    op.drop_column("sites", "cloudflare_account_id")
    op.drop_column("sites", "cf_zone_mirror_id")
    # drop в обратном порядке; drop_table снимает и свои индексы (кроме уже снятого sites-индекса выше)
    op.drop_table("cloudflare_certificate_pack_mirrors")
    op.drop_table("cloudflare_dns_record_mirrors")
    op.drop_table("cloudflare_zone_setting_observations")
    op.drop_table("cloudflare_zone_mirrors")
    op.drop_table("cloudflare_capability_observations")
    op.drop_table("cloudflare_connection_accounts")
    op.drop_table("cloudflare_accounts")
    op.drop_table("cloudflare_connections")
```

  Docstring вверху файла — развёрнутое обоснование (что создаёт, почему дедуп до UNIQUE, что будет
  с грязными продовыми Site-дублями), блок `Revision ID/Revises/Create Date` в конце docstring —
  как в 0014.

- [ ] **Шаг 7. Импорт legacy .env-connection** — `services/cf_legacy.py`:

```python
"""Импорт существующей пары CLOUDFLARE_API_TOKEN/CLOUDFLARE_ACCOUNT_ID как legacy-Connection.
secret_ref = 'env:CLOUDFLARE_API_TOKEN' (сам токен в БД НЕ пишется). Идемпотентно: повторный
вызов не плодит строк. .env fallback НЕ удаляется — старый провижн (P1) им ещё пользуется."""
import hashlib
from app.config import settings
from app.models.cloudflare import CloudflareConnection

LEGACY_SECRET_REF = "env:CLOUDFLARE_API_TOKEN"


def import_legacy_connection(db) -> int | None:
    token = settings.CLOUDFLARE_API_TOKEN
    if not token:
        return None
    existing = (db.query(CloudflareConnection)
                  .filter_by(secret_ref=LEGACY_SECRET_REF).first())
    if existing:
        return existing.id
    fp = hashlib.sha256(token.encode()).hexdigest()
    conn = CloudflareConnection(
        label="legacy .env",
        secret_ref=LEGACY_SECRET_REF,
        token_kind="account" if settings.CLOUDFLARE_ACCOUNT_ID else "user",
        owner_cf_account_id=settings.CLOUDFLARE_ACCOUNT_ID or None,
        token_fingerprint=fp,
        token_hint="..." + token[-4:] if len(token) >= 4 else None,
        status="unverified",
    )
    db.add(conn)
    db.commit()
    return conn.id
```

  Тест: `import_legacy_connection` дважды → одна строка; `secret_ref` == `env:CLOUDFLARE_API_TOKEN`;
  токен нигде в строке БД не встречается (`token not in repr(conn.__dict__)`); при пустом
  `CLOUDFLARE_API_TOKEN` → `None` и 0 строк.

- [ ] **Шаг 8.** Прогон `pytest backend/tests/test_cf_models.py -q` → PASS; полный сьют; pyflakes;
  коммит:

```bash
git add backend/app/models/cloudflare.py backend/app/models/__init__.py \
  backend/app/models/site.py backend/alembic/versions/0015_cloudflare_mirrors.py \
  backend/app/services/cf_legacy.py backend/tests/test_cf_models.py
git commit -F - <<'EOF'
feat(cf/P0): mirror-модель Cloudflare + UNIQUE Site.domain_id + импорт legacy .env

8 read-only mirror-таблиц (connection/account/zone/dns/settings/certs/capability),
secret_ref вместо токена в БД, партиал-unique «один primary на роль», UNIQUE(domain_id)
с дедупом Site-гонки panel/worker. import_legacy_connection() поднимает текущую .env-пару
как строку без удаления fallback. Ни одной Cloudflare-мутации.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
```

---

## Задача 2: `secret_ref` resolver — env: и safe filename, без утечки значения

**Files:**
- Create: `backend/app/services/cf_secret.py`
- Modify: `backend/app/config.py` (добавить `CLOUDFLARE_SECRETS_DIR: str = ""`)
- Test: `backend/tests/test_cf_secret.py`

**Interfaces:**
- Produces: `resolve_secret_ref(ref: str) -> str` (возвращает токен) и исключение
  `SecretRefError(Exception)`. Правило `ref`: `env:NAME` (только `[A-Z0-9_]`) либо `file:BASENAME`
  (basename в allowlisted read-only каталоге `settings.CLOUDFLARE_SECRETS_DIR`, по умолчанию
  `/run/secrets/cloudflare`). Задача 3 (транспорт) зовёт `resolve_secret_ref(conn.secret_ref)`.
- Consumes: `settings`.

**Защиты (все обязательны, все с тестом):** запрет абсолютных путей и `..`; запрет symlink-escape за
пределы каталога; отказ на oversized-файл (> 8 KiB); снятие ровно ОДНОГО trailing newline;
**resolved value НИКОГДА не попадает в текст ошибки** (в ошибке — только `ref`/имя, не содержимое).

- [ ] **Шаг 1. Падающий тест** (`test_cf_secret.py`):

```python
import pytest
from app.services.cf_secret import resolve_secret_ref, SecretRefError


def test_env_ref(monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "tok-abc")
    assert resolve_secret_ref("env:CLOUDFLARE_API_TOKEN") == "tok-abc"


def test_env_ref_bad_name():
    with pytest.raises(SecretRefError):
        resolve_secret_ref("env:PATH; rm -rf /")


def test_env_missing():
    with pytest.raises(SecretRefError):
        resolve_secret_ref("env:DEFINITELY_UNSET_VAR_XYZ")


def test_file_ref_reads_and_strips_one_newline(tmp_path, monkeypatch):
    monkeypatch.setattr("app.services.cf_secret.settings.CLOUDFLARE_SECRETS_DIR", str(tmp_path))
    (tmp_path / "cf").write_text("secret-value\n")
    assert resolve_secret_ref("file:cf") == "secret-value"


def test_file_ref_rejects_traversal(tmp_path, monkeypatch):
    monkeypatch.setattr("app.services.cf_secret.settings.CLOUDFLARE_SECRETS_DIR", str(tmp_path))
    for bad in ("file:../etc/passwd", "file:/etc/passwd", "file:sub/nested"):
        with pytest.raises(SecretRefError):
            resolve_secret_ref(bad)


def test_file_ref_rejects_symlink_escape(tmp_path, monkeypatch):
    import os
    outside = tmp_path / "outside.txt"; outside.write_text("leak")
    secdir = tmp_path / "sec"; secdir.mkdir()
    os.symlink(outside, secdir / "link")
    monkeypatch.setattr("app.services.cf_secret.settings.CLOUDFLARE_SECRETS_DIR", str(secdir))
    with pytest.raises(SecretRefError):
        resolve_secret_ref("file:link")


def test_file_ref_rejects_oversized(tmp_path, monkeypatch):
    monkeypatch.setattr("app.services.cf_secret.settings.CLOUDFLARE_SECRETS_DIR", str(tmp_path))
    (tmp_path / "big").write_bytes(b"x" * (8 * 1024 + 1))
    with pytest.raises(SecretRefError):
        resolve_secret_ref("file:big")


def test_error_never_leaks_value(tmp_path, monkeypatch):
    monkeypatch.setattr("app.services.cf_secret.settings.CLOUDFLARE_SECRETS_DIR", str(tmp_path))
    (tmp_path / "big").write_bytes(b"SUPERSECRET" * 1000)
    try:
        resolve_secret_ref("file:big")
    except SecretRefError as e:
        assert "SUPERSECRET" not in str(e)
```

- [ ] **Шаг 2. Запуск → FAIL** (`ModuleNotFoundError`).

- [ ] **Шаг 3. Реализация** — `services/cf_secret.py`:

```python
"""Резолвер secret_ref для Cloudflare-токенов. Токен НИКОГДА не хранится в БД — только ссылка.
Формы: env:NAME (имя [A-Z0-9_]) | file:BASENAME (в allowlisted read-only каталоге). Значение секрета
не попадает ни в один текст ошибки/fingerprint/лог (аудит §2)."""
import os
import re
from app.config import settings

_MAX_BYTES = 8 * 1024
_ENV_NAME = re.compile(r"^[A-Z0-9_]+$")


class SecretRefError(Exception):
    """Проблема с secret_ref. Сообщение содержит только ref/имя, НИКОГДА не значение секрета."""


def resolve_secret_ref(ref: str) -> str:
    if not isinstance(ref, str) or ":" not in ref:
        raise SecretRefError(f"плохой secret_ref: {ref!r}")
    scheme, _, rest = ref.partition(":")
    if scheme == "env":
        if not _ENV_NAME.match(rest):
            raise SecretRefError(f"недопустимое имя env-переменной: {rest!r}")
        val = os.environ.get(rest)
        if not val:
            raise SecretRefError(f"env-переменная не задана: {rest}")
        return val.rstrip("\n") if val.endswith("\n") else val
    if scheme == "file":
        base = settings.CLOUDFLARE_SECRETS_DIR or "/run/secrets/cloudflare"
        # basename без разделителей и .. — только простое имя файла
        if not rest or "/" in rest or "\\" in rest or rest in (".", "..") or "\x00" in rest:
            raise SecretRefError(f"недопустимое имя secret-файла: {rest!r}")
        root = os.path.realpath(base)
        full = os.path.realpath(os.path.join(root, rest))
        # symlink-escape / traversal: реальный путь обязан лежать строго внутри root
        if full != os.path.join(root, rest) and not full.startswith(root + os.sep):
            raise SecretRefError(f"secret-файл вне разрешённого каталога: {rest!r}")
        if not full.startswith(root + os.sep):
            raise SecretRefError(f"secret-файл вне разрешённого каталога: {rest!r}")
        if not os.path.isfile(full):
            raise SecretRefError(f"secret-файл не найден: {rest!r}")
        if os.path.getsize(full) > _MAX_BYTES:
            raise SecretRefError(f"secret-файл слишком велик: {rest!r}")
        with open(full, "r", encoding="utf-8") as fh:
            data = fh.read(_MAX_BYTES + 1)
        if len(data.encode("utf-8")) > _MAX_BYTES:
            raise SecretRefError(f"secret-файл слишком велик: {rest!r}")
        return data[:-1] if data.endswith("\n") else data
    raise SecretRefError(f"неизвестная схема secret_ref: {scheme!r}")
```

  (Двойная проверка `startswith(root + os.sep)` — намеренно: первая ловит подмену realpath у
  symlink, вторая — общий escape; обе дают одинаковое безопасное сообщение без значения.)

- [ ] **Шаг 4. config.py:** добавить строку `CLOUDFLARE_SECRETS_DIR: str = ""` рядом с
  `CLOUDFLARE_ACCOUNT_ID` (строка ~39).

- [ ] **Шаг 5.** `pytest backend/tests/test_cf_secret.py -q` → PASS; pyflakes; коммит:

```bash
git add backend/app/services/cf_secret.py backend/app/config.py backend/tests/test_cf_secret.py
git commit -F - <<'EOF'
feat(cf/P0): secret_ref resolver (env:/file:) без утечки значения

Токен резолвится по ссылке, в БД не хранится. Защиты: env-имя [A-Z0-9_], file-basename без
../symlink-escape/абсолютных путей, отказ на >8 KiB, снятие одного trailing newline, значение
секрета никогда не в тексте ошибки.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
```

---

## Задача 3: транспорт — explicit connection/account, pagination, verify по token_kind, account-scoped find_zone

**Files:**
- Modify: `backend/app/integrations/cloudflare.py`
- Test: `backend/tests/test_cloudflare_transport.py` (создать — сейчас transport-теста CF нет вовсе)

**Interfaces:**
- Consumes: `resolve_secret_ref` (задача 2), `CloudflareConnection` (задача 1).
- Produces (для задачи 4):
  - `CloudflareClient.with_token(token: str, account_id: str = "") -> CloudflareClient` — конструктор
    с ЯВНЫМ токеном, не из глобального singleton.
  - `verify_token(self, token_kind: str, account_id: str = "") -> dict` — user → `GET
    /user/tokens/verify`; account → `GET /accounts/{account_id}/tokens/verify`.
  - `list_accounts_paginated(self) -> list[dict]`
  - `list_zones_paginated(self, account_id: str) -> list[dict]`
  - `find_zone_in_account(self, name: str, account_id: str) -> dict | None` (account-scoped; НЕ
    трогает существующий legacy `find_zone(domain)`)
  - `list_dns_paginated(self, zone_id: str, type: str | None = None, name: str | None = None) -> list[dict]`
  - `get_zone_setting(self, zone_id: str, setting_id: str) -> dict` (per-setting GET; batch не добавлять)
  - `list_universal_certificate_packs(self, zone_id: str) -> list[dict]`
  - `get_dnssec(self, zone_id: str) -> dict`
  - `_paginate(self, path: str, params: dict | None = None) -> list[dict]` (по `result_info`)

**Инварианты:** `Authorization` и raw-response никогда в логах/ошибках; HTTP 2xx недостаточно —
envelope `success` проверяется всегда (`_result` уже это делает, переиспользовать); empty list ≠
error ≠ not-found; batch Zone Settings endpoint НЕ добавляется (deprecated, EOL 2026-09-15).

- [ ] **Шаг 1. Падающие тесты** (`test_cloudflare_transport.py`) — подмена `request` на инстансе
  (рубильник сети их не трогает):

```python
import httpx, json, pytest
from app.integrations.cloudflare import CloudflareClient


def _resp(payload, status=200):
    return httpx.Response(status, json=payload, request=httpx.Request("GET", "http://x"))


def test_envelope_success_false_raises_on_http_200():
    c = CloudflareClient.with_token("tok")
    c.request = lambda *a, **k: _resp({"success": False, "errors": [{"message": "bad"}], "result": None})
    with pytest.raises(Exception):
        c.list_accounts_paginated()


def test_pagination_collects_all_pages_over_50():
    c = CloudflareClient.with_token("tok")
    calls = {"n": 0}
    def fake(method, url, **kw):
        calls["n"] += 1
        page = kw["params"]["page"]
        total_pages = 2
        results = [{"id": f"z{page}-{i}"} for i in range(50 if page == 1 else 5)]
        return _resp({"success": True, "result": results,
                      "result_info": {"page": page, "total_pages": total_pages}})
    c.request = fake
    zones = c.list_zones_paginated("acct1")
    assert len(zones) == 55 and calls["n"] == 2


def test_user_vs_account_verify_endpoint():
    seen = {}
    c = CloudflareClient.with_token("tok")
    def fake(method, url, **kw):
        seen["url"] = url
        return _resp({"success": True, "result": {"status": "active"}})
    c.request = fake
    c.verify_token("user")
    assert seen["url"].endswith("/user/tokens/verify")
    c.verify_token("account", "acctHEX")
    assert seen["url"].endswith("/accounts/acctHEX/tokens/verify")


def test_find_zone_in_account_filters_by_account():
    c = CloudflareClient.with_token("tok")
    def fake(method, url, **kw):
        assert kw["params"]["account.id"] == "acctHEX"
        return _resp({"success": True, "result": [{"id": "z1", "name": "a.ru", "status": "active",
                                                   "account": {"id": "acctHEX"}}],
                      "result_info": {"page": 1, "total_pages": 1}})
    c.request = fake
    z = c.find_zone_in_account("a.ru", "acctHEX")
    assert z["id"] == "z1"


def test_empty_list_is_not_error():
    c = CloudflareClient.with_token("tok")
    c.request = lambda *a, **k: _resp({"success": True, "result": [],
                                       "result_info": {"page": 1, "total_pages": 1}})
    assert c.list_zones_paginated("acctHEX") == []


def test_token_never_in_repr_or_headers_leak():
    c = CloudflareClient.with_token("SECRET-TOKEN")
    assert "SECRET-TOKEN" not in repr(c.__dict__.get("account_id", ""))
    assert c._headers()["Authorization"] == "Bearer SECRET-TOKEN"  # только в заголовке, нигде ещё
```

- [ ] **Шаг 2. Запуск → FAIL** (`with_token` / `list_accounts_paginated` не существуют).

- [ ] **Шаг 3. Реализация** — дописать в `integrations/cloudflare.py` (существующие методы
  `find_zone/create_zone/ensure_zone/set_ssl/add_a_record/...` НЕ трогать — это legacy для P1):

```python
    @classmethod
    def with_token(cls, token: str, account_id: str = "") -> "CloudflareClient":
        """Клиент с ЯВНЫМ токеном/аккаунтом — не из глобального settings singleton (аудит §4.1)."""
        c = cls()
        c.token = token
        c.account_id = account_id
        return c

    def _paginate(self, path: str, params: dict | None = None) -> list:
        """Собрать все страницы по result_info. 2xx недостаточно — success проверяет _result."""
        out: list = []
        page = 1
        params = dict(params or {})
        while True:
            params["page"] = page
            params.setdefault("per_page", 50)
            resp = self.request("GET", f"{self.base_url}{path}",
                                headers=self._headers(), params=params)
            body = resp.json()
            if not body.get("success"):
                raise RuntimeError(f"cloudflare {path}: " + _safe_errors(body))
            out.extend(body.get("result") or [])
            info = body.get("result_info") or {}
            total = info.get("total_pages")
            if not total or page >= total:
                break
            page += 1
        return out

    def verify_token(self, token_kind: str, account_id: str = "") -> dict:
        if token_kind == "account":
            if not account_id:
                raise ValueError("account-owned токен требует account_id для verify")
            path = f"/accounts/{account_id}/tokens/verify"
        else:
            path = "/user/tokens/verify"
        resp = self.request("GET", f"{self.base_url}{path}", headers=self._headers())
        return self._result(resp)

    def list_accounts_paginated(self) -> list:
        return self._paginate("/accounts")

    def list_zones_paginated(self, account_id: str) -> list:
        return self._paginate("/zones", {"account.id": account_id})

    def find_zone_in_account(self, name: str, account_id: str) -> dict | None:
        zones = self._paginate("/zones", {"name": name, "account.id": account_id})
        return zones[0] if zones else None

    def list_dns_paginated(self, zone_id: str, type: str | None = None,
                           name: str | None = None) -> list:
        params: dict = {}
        if type:
            params["type"] = type
        if name:
            params["name"] = name
        return self._paginate(f"/zones/{zone_id}/dns_records", params)

    def get_zone_setting(self, zone_id: str, setting_id: str) -> dict:
        # ТОЛЬКО per-setting GET. Batch /zones/{id}/settings deprecated (EOL 2026-09-15) — не добавлять.
        resp = self.request("GET", f"{self.base_url}/zones/{zone_id}/settings/{setting_id}",
                            headers=self._headers())
        return self._result(resp)

    def list_universal_certificate_packs(self, zone_id: str) -> list:
        return self._paginate(f"/zones/{zone_id}/ssl/certificate_packs")

    def get_dnssec(self, zone_id: str) -> dict:
        resp = self.request("GET", f"{self.base_url}/zones/{zone_id}/dnssec",
                            headers=self._headers())
        return self._result(resp)
```

  Плюс модульный helper (рядом с `_ZONE_FIELDS`), чтобы ошибки не тащили raw/Authorization:

```python
def _safe_errors(body: dict) -> str:
    errs = body.get("errors") or []
    parts = [f"{e.get('code', '')}:{e.get('message', '')}" for e in errs if isinstance(e, dict)]
    return "; ".join(p for p in parts if p.strip(":")) or "unknown error"
```

  Если существующий `_result` при `success=false` уже кидает — переиспользовать его в новых методах
  вместо повторной проверки (проверить его тело перед реализацией; тест
  `test_envelope_success_false_raises_on_http_200` покрывает оба случая).

- [ ] **Шаг 4.** `pytest backend/tests/test_cloudflare_transport.py -q` → PASS; pyflakes; коммит:

```bash
git add backend/app/integrations/cloudflare.py backend/tests/test_cloudflare_transport.py
git commit -F - <<'EOF'
feat(cf/P0): account-aware read-транспорт Cloudflare + полная пагинация

with_token() даёт явные креды вместо глобального singleton; verify по token_kind
(user vs account-owned endpoint); _paginate по result_info (51+ зон/аккаунтов/DNS/cert-packs);
find_zone_in_account() всегда фильтрует по account.id; per-setting Zone Settings GET (batch
deprecated не добавлен). Только read-методы, ни одной мутации. Токен/Authorization не в ошибках.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
```

---

## Задача 4: sync-сервис — upsert в mirror, «пусто ≠ удалено», unmanaged read-only

**Files:**
- Create: `backend/app/services/cf_sync.py`
- Test: `backend/tests/test_cf_sync.py`

**Interfaces:**
- Consumes: транспорт задачи 3 (`with_token`, `verify_token`, `list_*`, `get_*`),
  `resolve_secret_ref` (задача 2), модели задачи 1, `cf_legacy.import_legacy_connection`.
- Produces (для задач 5, 7):
  - `sync_all(db, *, report=None) -> dict` — точка входа джоба (импорт legacy → по каждой
    connection: verify → capability observations → accounts → zones → детали зон). `report` —
    опциональный колбэк прогресса (совместим с `jobs.report`-подписью, см. задачу 5).
  - `sync_connection(db, conn) -> None`
  - `_sync_zone_details(db, cf, m: CloudflareZoneMirror) -> None` — по одной зоне: DNS-записи,
    per-setting наблюдения, cert-паки, dnssec; каждый GET в своём try (падение одного не рушит
    соседние). Пишет `m.universal_ssl_status` из наблюдения setting `universal_ssl`.
  - `_upsert_zone(db, account_hex, z: dict) -> CloudflareZoneMirror`
  - `_reconcile_missing(db, account_hex, seen_zone_ids: set[str]) -> None` — помечает
    `missing_since`, НЕ `status='deleted'`.
  - `_backfill_site_links(db) -> None` — §2.6 backfill legacy-Site: `Site.cf_zone_id` →
    `cf_zone_mirror_id` + `cloudflare_account_id`; зовётся из `sync_all` перед финальным commit.

**Семантика (тесты обязательны):** успешный GET → запись `observed`; ошибка GET → `last_error_safe`
+ статус `unknown`/`error`, прежнее значение не затирается фиктивным; **пустой list зон при успехе
НЕ метит существующие зоны deleted** — только `missing_since` (omission = missing/inaccessible);
`observed_authoritative_ns` не подменяет `original_name_servers`; unmanaged зона (нет привязанного
`Site`) синхронизируется read-only и НЕ создаёт/не меняет `Domain`/`Site`; `cloudflare_account_id`
пишется как внешний hex, не локальный PK; `CloudflareConnectionAccount.capabilities_json`
заполняется исходами (`token_active`/`accounts_read`/`zones_read`) — иначе capability-чипы UI
(задача 7) пусты; `_backfill_site_links` связывает legacy-Site с mirror и НЕ трогает Site без
`cf_zone_id`.

- [ ] **Шаг 1. Падающие тесты** (`test_cf_sync.py`) — фейковый клиент, монки-патч
  `cf_sync.CloudflareClient`:

```python
from types import SimpleNamespace
from app.db import SessionLocal
from app.models.cloudflare import (CloudflareConnection, CloudflareConnectionAccount,
                                    CloudflareZoneMirror, CloudflareDnsRecordMirror)
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
```

- [ ] **Шаг 2. Запуск → FAIL** (`ModuleNotFoundError: app.services.cf_sync`).

- [ ] **Шаг 3. Реализация** — `services/cf_sync.py`. Спецификация (полные тела `_upsert_zone`/
  `_reconcile_missing` даны; sync_connection и детали зоны — по этой структуре):

```python
"""Cloudflare read-only sync: наблюдаем внешнюю правду в mirror-таблицы. Никаких мутаций CF,
никаких побочных эффектов на Domain/Site (unmanaged зона read-only). Пустой список при успехе —
missing_since, НЕ deleted; ошибка GET — last_error_safe без затирания прежнего значения."""
from datetime import datetime, timezone
from app.config import settings
from app.integrations.cloudflare import CloudflareClient
from app.services.cf_secret import resolve_secret_ref
from app.services import cf_legacy
from app.models.site import Site
from app.models.cloudflare import (
    CloudflareConnection, CloudflareAccount, CloudflareConnectionAccount,
    CloudflareCapabilityObservation, CloudflareZoneMirror,
    CloudflareZoneSettingObservation, CloudflareDnsRecordMirror,
    CloudflareCertificatePackMirror,
)

_OBSERVED_SETTINGS = ("ssl", "always_use_https", "min_tls_version", "tls_1_3", "http3",
                      "0rtt", "development_mode", "universal_ssl")  # per-setting GET, read-only


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _safe(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"[:500]


def _stringify(v) -> str | None:
    if v is None:
        return None
    return v if isinstance(v, str) else str(v)


def _observe(db, conn_id, account_hex, resource_type, resource_id, capability, outcome, err=None):
    db.add(CloudflareCapabilityObservation(
        connection_id=conn_id, cloudflare_account_id=account_hex,
        resource_type=resource_type, resource_id=resource_id, capability=capability,
        outcome=outcome, safe_error=err))


def _upsert_zone(db, account_hex: str, z: dict) -> CloudflareZoneMirror:
    m = db.query(CloudflareZoneMirror).filter_by(cf_zone_id=z["id"]).one_or_none()
    if m is None:
        m = CloudflareZoneMirror(cf_zone_id=z["id"], cloudflare_account_id=account_hex,
                                 name=z.get("name", ""))
        db.add(m)
    m.cloudflare_account_id = account_hex
    m.name = z.get("name", m.name)
    m.status = z.get("status", "unknown")
    m.plan_name = (z.get("plan") or {}).get("name")
    m.paused = z.get("paused")
    ns = z.get("name_servers")
    if ns:
        m.name_servers_json = ns
    orig = z.get("original_name_servers")
    if orig:  # НЕ подменяем наблюдаемым authoritative NS — это отдельное поле
        m.original_name_servers_json = orig
    m.zones_seen_at = _now()
    m.missing_since = None
    m.last_error_safe = None
    return m


def _reconcile_missing(db, account_hex: str, seen_zone_ids: set) -> None:
    """Зоны, что были у аккаунта, но в успешном списке отсутствуют — missing_since, НЕ deleted.
    Omission доказывает только недоступность/пропажу; delete подтверждается лишь точечным GET
    (это уже P3). Здесь — консервативно: помечаем missing, статус не меняем на deleted."""
    rows = (db.query(CloudflareZoneMirror)
              .filter_by(cloudflare_account_id=account_hex).all())
    for m in rows:
        if m.cf_zone_id not in seen_zone_ids and m.missing_since is None:
            m.missing_since = _now()
```

  Тело `sync_connection` / `_sync_zone_details` / `_backfill_site_links` (полный код):

```python
def sync_connection(db, conn: CloudflareConnection) -> None:
    # 1. secret_ref → токен (значение секрета никогда не в last_error_safe)
    try:
        token = resolve_secret_ref(conn.secret_ref)
    except Exception as exc:
        conn.status = "error"
        conn.last_error_safe = _safe(exc)
        db.commit()
        return
    # 2. клиент с ЯВНЫМ токеном (не из глобального singleton)
    cf = CloudflareClient.with_token(token, conn.owner_cf_account_id or "")
    # 3. verify по token_kind
    try:
        cf.verify_token(conn.token_kind, conn.owner_cf_account_id or "")
        conn.status = "ok"
        conn.verified_at = _now()
        conn.last_error_safe = None
        _observe(db, conn.id, conn.owner_cf_account_id, "account", conn.owner_cf_account_id,
                 "token_active", "allowed")
    except Exception as exc:
        conn.status = "error"
        conn.last_error_safe = _safe(exc)
        _observe(db, conn.id, conn.owner_cf_account_id, "account", conn.owner_cf_account_id,
                 "token_active", "denied", _safe(exc))
        db.commit()
        return
    # 4. аккаунты: user-токен листает все; account-токен знает ровно свой один
    if conn.token_kind == "account":
        accounts, accounts_ok, accounts_err = [{"id": conn.owner_cf_account_id, "name": None}], True, None
    else:
        try:
            accounts, accounts_ok, accounts_err = cf.list_accounts_paginated(), True, None
        except Exception as exc:
            accounts, accounts_ok, accounts_err = [], False, _safe(exc)
            conn.last_error_safe = accounts_err
            _observe(db, conn.id, None, "user", None, "accounts_read", "denied", accounts_err)
    for a in accounts:
        acc_hex = a.get("id")
        if not acc_hex:
            continue
        acc_row = db.query(CloudflareAccount).filter_by(cf_account_id=acc_hex).one_or_none()
        if acc_row is None:
            acc_row = CloudflareAccount(cf_account_id=acc_hex)
            db.add(acc_row)
        acc_row.name = a.get("name", acc_row.name)
        acc_row.last_synced_at = _now()
        ca = (db.query(CloudflareConnectionAccount)
                .filter_by(connection_id=conn.id, cloudflare_account_id=acc_hex).one_or_none())
        if ca is None:
            ca = CloudflareConnectionAccount(connection_id=conn.id, cloudflare_account_id=acc_hex)
            db.add(ca)
        ca.last_probed_at = _now()
        caps = {"token_active": "allowed",
                "accounts_read": "allowed" if accounts_ok else "denied"}
        if accounts_ok:
            _observe(db, conn.id, acc_hex, "account", acc_hex, "accounts_read", "allowed")
        # 5. зоны аккаунта
        try:
            zones = cf.list_zones_paginated(acc_hex)
            caps["zones_read"] = "allowed"
            _observe(db, conn.id, acc_hex, "account", acc_hex, "zones_read", "allowed")
            seen = set()
            for z in zones:
                m = _upsert_zone(db, acc_hex, z)
                seen.add(z["id"])
                _sync_zone_details(db, cf, m)     # 6. детали зоны
            _reconcile_missing(db, acc_hex, seen)  # пусто/пропажа → missing_since, НЕ deleted
            acc_row.status = "ok"
            acc_row.last_error_safe = None
            ca.status = "ok"
            ca.last_error_safe = None
        except Exception as exc:
            caps["zones_read"] = "denied"
            acc_row.status = "error"
            acc_row.last_error_safe = _safe(exc)
            ca.status = "error"
            ca.last_error_safe = _safe(exc)
            _observe(db, conn.id, acc_hex, "account", acc_hex, "zones_read", "unknown", _safe(exc))
            # НЕ трогаем существующие zone mirrors — omission ≠ deleted
        ca.capabilities_json = caps
    db.commit()  # 7.


def _sync_zone_details(db, cf, m: CloudflareZoneMirror) -> None:
    """Детали одной зоны. Каждый GET в своём try — падение одного не рушит соседние."""
    zid = m.cf_zone_id
    # DNS-записи: upsert по cf_record_id, missing_since (не delete) для исчезнувших
    try:
        records = cf.list_dns_paginated(zid)
        seen = set()
        for rec in records:
            r = db.query(CloudflareDnsRecordMirror).filter_by(cf_record_id=rec["id"]).one_or_none()
            if r is None:
                r = CloudflareDnsRecordMirror(cf_record_id=rec["id"], cloudflare_zone_id=zid)
                db.add(r)
            r.cloudflare_zone_id = zid
            r.type = rec.get("type", "")
            r.name = rec.get("name", "")
            r.content = rec.get("content")
            r.ttl = rec.get("ttl")
            r.proxied = rec.get("proxied")
            r.observed_at = _now()
            r.missing_since = None
            r.last_error_safe = None
            # managed_role НЕ выставляем — apex_origin только в M3/adoption
            seen.add(rec["id"])
        for r in db.query(CloudflareDnsRecordMirror).filter_by(cloudflare_zone_id=zid).all():
            if r.cf_record_id not in seen and r.missing_since is None:
                r.missing_since = _now()
    except Exception:
        pass  # ошибка DNS одной зоны — соседние зоны/детали не портим
    # per-setting наблюдения (batch endpoint deprecated — только по одному)
    for sid in _OBSERVED_SETTINGS:
        obs = (db.query(CloudflareZoneSettingObservation)
                 .filter_by(cloudflare_zone_id=zid, setting_id=sid).one_or_none())
        if obs is None:
            obs = CloudflareZoneSettingObservation(cloudflare_zone_id=zid, setting_id=sid)
            db.add(obs)
        try:
            s = cf.get_zone_setting(zid, sid)
            obs.value_json = s.get("value")
            obs.editable = s.get("editable")
            obs.status = "observed"
            obs.observed_at = _now()
            obs.error_safe = None
            if sid == "universal_ssl":  # зеркалим в zone mirror для SSL-колонки UI
                m.universal_ssl_status = _stringify(s.get("value"))
        except Exception as exc:
            obs.status = "error"
            obs.error_safe = _safe(exc)
            obs.observed_at = _now()
    # cert-паки
    try:
        packs = cf.list_universal_certificate_packs(zid)
        seen = set()
        for p in packs:
            pm = db.query(CloudflareCertificatePackMirror).filter_by(cf_pack_id=p["id"]).one_or_none()
            if pm is None:
                pm = CloudflareCertificatePackMirror(cf_pack_id=p["id"], cloudflare_zone_id=zid)
                db.add(pm)
            pm.cloudflare_zone_id = zid
            pm.type = p.get("type")
            pm.status = p.get("status")
            pm.hosts_json = p.get("hosts")
            pm.certificates_json = p.get("certificates")
            pm.observed_at = _now()
            pm.missing_since = None
            seen.add(p["id"])
        for pm in db.query(CloudflareCertificatePackMirror).filter_by(cloudflare_zone_id=zid).all():
            if pm.cf_pack_id not in seen and pm.missing_since is None:
                pm.missing_since = _now()
    except Exception:
        pass
    # dnssec
    try:
        d = cf.get_dnssec(zid)
        m.dnssec_status = d.get("status")
        m.dnssec_observed_at = _now()
        m.dnssec_error_safe = None
    except Exception as exc:
        m.dnssec_error_safe = _safe(exc)


def _backfill_site_links(db) -> None:
    """§2.6 backfill legacy-Site: связываем существующие Site.cf_zone_id с наблюдённым mirror и
    выставляем desired cloudflare_account_id. Идемпотентно, только для Site с непустым cf_zone_id.
    Инвариант: join строго по совпадению cf_zone_id (legacy == mirror) — иначе связь не ставим."""
    for s in db.query(Site).filter(Site.cf_zone_id.isnot(None)).all():
        if not s.cf_zone_id:
            continue
        m = db.query(CloudflareZoneMirror).filter_by(cf_zone_id=s.cf_zone_id).one_or_none()
        if m is not None:
            s.cf_zone_mirror_id = m.id
            if not s.cloudflare_account_id:
                s.cloudflare_account_id = m.cloudflare_account_id
        elif not s.cloudflare_account_id and settings.CLOUDFLARE_ACCOUNT_ID:
            s.cloudflare_account_id = settings.CLOUDFLARE_ACCOUNT_ID
```

  `sync_all(db, *, report=None)`:

```python
def sync_all(db, *, report=None) -> dict:
    cf_legacy.import_legacy_connection(db)
    conns = db.query(CloudflareConnection).all()
    done = 0
    for conn in conns:
        if report:
            report(done=done, total=len(conns), current=conn.label, stage="verify")
        sync_connection(db, conn)
        done += 1
    _backfill_site_links(db)  # §2.6: зоны уже наблюдены — можно связать legacy-Site
    db.commit()
    if report:
        report(done=done, total=len(conns), stage="zones")
    return {"connections": len(conns)}
```

- [ ] **Шаг 4.** `pytest backend/tests/test_cf_sync.py -q` → PASS; pyflakes; коммит:

```bash
git add backend/app/services/cf_sync.py backend/tests/test_cf_sync.py
git commit -F - <<'EOF'
feat(cf/P0): read-only sync в mirror — «пусто ≠ удалено», unmanaged без побочных эффектов

verify → capability observations (capabilities_json на connection-account) → accounts → zones →
DNS/settings/certs/dnssec, всё upsert по внешним id; universal_ssl зеркалится в zone mirror.
Пустой список зон при успехе = missing_since, ошибка list = last_error_safe без затирания;
observed authoritative NS не подменяет original NS; Domain/Site не создаются, только §2.6-backfill
связывает legacy Site.cf_zone_id с mirror.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
```

---

## Задача 5: job-реестр — разрешить `cf_sync` (имена, capacity, лейблы, роут запуска)

> **⚠ ПОРЯДОК ИСПОЛНЕНИЯ: Задачу 6 выполнить ПЕРЕД этой.** Роут `cloudflare_sync` ниже зовёт
> `_require_cf_write(request)`, а этот helper определяется в Задаче 6. Если делать 5 раньше 6,
> шаг «pyflakes чист» упадёт с `F821 undefined name`, а вызов роута — `NameError`. Исполнять как
> 1→2→3→4→**6→5**→7→8. (Interfaces ниже помечают `_require_cf_write` как «задача 6» осознанно.)

**Files:**
- Modify: `backend/app/api/panel.py` (строки ~43 `_JOBS`, ~293 дублирующий литерал, ~571
  `jobs_live`, + новый роут запуска), `backend/app/services/jobs.py` (ThreadPoolExecutor ~70),
  `backend/app/templates/base.html` (`JOB_RU` ~436)
- Test: `backend/tests/test_cf_job.py`

**Interfaces:**
- Consumes: `cf_sync.sync_all` (задача 4), `jobs.spawn/track/report/cancelled/last` (существуют).
- Produces (для задачи 7): роут `POST /settings/cloudflare/sync` → `jobs.spawn("cf_sync", ...)`;
  имя `cf_sync` во всех реестрах/лейблах; стадии `verify`/`zones` с русскими подписями.

**Разведка показала три независимых литерала имён задач + размер пула = 4** — все обновить
синхронно (иначе `cf_sync` не появится в UI/cancel/live).

- [ ] **Шаг 1. Падающие тесты** (`test_cf_job.py`):

```python
from app.api import panel


def test_cf_sync_is_known_job():
    assert "cf_sync" in panel._JOBS


def test_cancel_route_accepts_cf_sync(client):
    # неизвестное имя → 404; cf_sync должно НЕ быть 404 (спавна нет — вернёт «нет задачи», но не 404)
    r = client.post("/run/cf_sync/cancel")
    assert r.status_code != 404
```

  (Плюс — тест, что `jobs_live()`/dashboard не падают с `cf_sync` в `_JOBS`.)

- [ ] **Шаг 2. Запуск → FAIL** (`cf_sync` не в `_JOBS`).

- [ ] **Шаг 3. Правки:**
  - `panel.py:43`: `_JOBS = ("discovery", "score", "recheck", "sweep", "cf_sync")`.
  - `panel.py:~293`: дублирующий литерал `("discovery", "score", "recheck")` — оставить как есть
    (это дашбордный подсписок M1; `cf_sync` туда не нужен) ИЛИ, если контроллер хочет карточку на
    дашборде, добавить — по умолчанию НЕ добавляем (Cloudflare-задача живёт на своём экране).
  - `jobs.py:70`: `_EXEC = ThreadPoolExecutor(max_workers=5)` + обновить комментарий (пять имён:
    discovery|score|recheck|sweep|cf_sync).
  - `base.html:436`: в `JOB_RU` добавить `cf_sync:'Синхронизация Cloudflare'`.
  - Новый роут в `panel.py` рядом с прочими `jobs.spawn`:

```python
@router.post("/settings/cloudflare/sync")
def cloudflare_sync(request: Request):
    _require_cf_write(request)  # задача 6 — гейт мутаций/CF-write
    def _job():
        with jobs.track("cf_sync", trigger="manual",
                        stages=[{"key": "verify", "label": "Проверка токенов", "state": "pending"},
                                {"key": "zones", "label": "Зоны и записи", "state": "pending"}]) as rid:
            with SessionLocal() as db:
                cf_sync.sync_all(db, report=lambda **kw: jobs.report(rid, **kw))
    ok = jobs.spawn("cf_sync", _job)
    return _redirect("/settings/cloudflare", msg="Синхронизация запущена" if ok else None,
                     err=None if ok else "Задача уже выполняется")
```

  (Импорты `cf_sync`, `SessionLocal` — добавить вверху panel.py, если ещё нет.)

- [ ] **Шаг 4.** `pytest backend/tests/test_cf_job.py -q` → PASS; полный сьют (проверить, что
  дашборд/`jobs_live` не сломались); pyflakes; коммит:

```bash
git add backend/app/api/panel.py backend/app/services/jobs.py \
  backend/app/templates/base.html backend/tests/test_cf_job.py
git commit -F - <<'EOF'
feat(cf/P0): cf_sync в job-реестре (имена/capacity/лейблы) + роут запуска

_JOBS/JOB_RU/ThreadPoolExecutor синхронно расширены пятым именем cf_sync; стадии verify/zones
с русскими подписями; POST /settings/cloudflare/sync за CF-write-гейтом (задача 6). Мутации CF
через transient spawn не запускаются — только read-only sync.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
```

---

## Задача 6: server-side gate — CF-write требует настроенный PANEL_USER/PANEL_PASS

**Files:**
- Modify: `backend/app/api/panel.py` (helper `_require_cf_write`)
- Test: `backend/tests/test_cf_write_gate.py`

**Interfaces:**
- Produces: `_require_cf_write(request) -> None` — поднимает `HTTPException(403)`, если
  `settings.PANEL_USER`/`settings.PANEL_PASS` пусты. Используется в задаче 5 (sync-роут) и всеми
  будущими CF-мутациями P1+.
- Consumes: `settings`.

**Почему сейчас, хотя мутаций ещё нет:** аудит §11/§15 — «Cloudflare mutation endpoints должны иметь
server-side hard gate на настроенный auth, не полагаться на same-origin». Гейт вводится и
тестируется в P0, чтобы P1 не забыл его постфактум. В P0 он охраняет единственный CF-write —
запуск sync (пишет в наши mirror-таблицы, ходит в CF-аккаунт по токену).

- [ ] **Шаг 1. Падающие тесты** (`test_cf_write_gate.py`):

```python
import pytest
from app.config import settings


def test_cf_sync_blocked_without_panel_auth(client, monkeypatch):
    monkeypatch.setattr(settings, "PANEL_USER", "")
    monkeypatch.setattr(settings, "PANEL_PASS", "")
    r = client.post("/settings/cloudflare/sync")
    assert r.status_code == 403


def test_cf_sync_allowed_with_panel_auth(client, monkeypatch):
    monkeypatch.setattr(settings, "PANEL_USER", "u")
    monkeypatch.setattr(settings, "PANEL_PASS", "p")
    r = client.post("/settings/cloudflare/sync", follow_redirects=False)
    assert r.status_code in (302, 303)  # редирект, не 403
```

  (Autouse `_no_panel_auth` выключает Basic-проверку транспорта — но `_require_cf_write` читает
  `settings` напрямую и от неё не зависит; тесты явно ставят/снимают значения.)

- [ ] **Шаг 2. Запуск → FAIL** (`_require_cf_write` не существует / роут не 403).

- [ ] **Шаг 3. Реализация** — в `panel.py`:

```python
def _require_cf_write(request: Request) -> None:
    """Hard gate: любой CF-write требует НАСТРОЕННЫЙ panel auth. Same-origin недостаточен
    (аудит §11/§15). Транспортная Basic-проверка может стоять отдельно; здесь — что auth ВООБЩЕ
    сконфигурирован, иначе LAN-экспозиция открывает мутации кому угодно."""
    if not (settings.PANEL_USER and settings.PANEL_PASS):
        raise HTTPException(status_code=403,
                            detail="Cloudflare-операции требуют настроенных PANEL_USER/PANEL_PASS")
```

- [ ] **Шаг 4.** `pytest backend/tests/test_cf_write_gate.py -q` → PASS; pyflakes; коммит:

```bash
git add backend/app/api/panel.py backend/tests/test_cf_write_gate.py
git commit -F - <<'EOF'
feat(cf/P0): server-side гейт — CF-write требует настроенный panel auth

_require_cf_write() поднимает 403, если PANEL_USER/PANEL_PASS пусты. Вводится в P0 (охраняет
запуск sync), чтобы P1+ мутации наследовали гейт, а не добавляли его постфактум. Same-origin
недостаточен (аудит §11/§15).

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
```

---

## Задача 7: UI — read-only `/settings/cloudflare` («Подключения» + «Аккаунты и зоны»)

**Files:**
- Create: `backend/app/templates/settings_cloudflare.html`
- Modify: `backend/app/api/panel.py` (роут `GET /settings/cloudflare`), `backend/app/templates/
  settings.html` (chip-переключатель сверху)
- Test: `backend/tests/test_cf_panel.py`

**Interfaces:**
- Consumes: модели задачи 1 (read), `_require_cf_write` (задача 6, для кнопки sync).
- Produces: экран `/settings/cloudflare` (`active="settings"`, подсветка родительского пункта —
  как `/domains/pool`), контекст `{connections, accounts, zones}`.

**Дизайн (docs/DESIGN.md, только классы из base.html):** переключатель «Воронка / Cloudflare» —
пара `<a class="chip {{ 'on' if ... }}">` под `h2` (паттерн `pool.html:46-53`). Раздел
«Подключения» — `.station`/`.plate`/`details.what` карточки: capability как `.badge .b-*`/
`.chip.on` (allowed/denied/unknown), инструкция про `secret_ref` (НЕ форма ввода токена). Раздел
«Аккаунты и зоны» — `.card`+`.wrap`+`table` со статусами `.led-ok/.led-bad/.led-warn`.

- [ ] **Шаг 1. Падающие тесты** (`test_cf_panel.py`):

```python
def test_cloudflare_tab_renders(client):
    r = client.get("/settings/cloudflare")
    assert r.status_code == 200
    assert "Подключения" in r.text and "Аккаунты и зоны" in r.text


def test_no_token_input_field(client):
    # secret_ref — инструкция, НЕ поле ввода токена в HTTP UI (аудит §13.1)
    r = client.get("/settings/cloudflare")
    assert 'name="token"' not in r.text and 'type="password"' not in r.text


def test_zone_table_shows_unknown_not_fake_clean(client):
    # зона с origin_tls_status=unknown показывается как «не проверено», не «ок»
    from app.db import SessionLocal
    from app.models.cloudflare import CloudflareZoneMirror
    with SessionLocal() as db:
        db.add(CloudflareZoneMirror(cloudflare_account_id="acc", cf_zone_id="z1",
                                    name="x.ru", status="active", origin_tls_status="unknown"))
        db.commit()
    r = client.get("/settings/cloudflare")
    assert "x.ru" in r.text


def test_capability_chips_render(client):
    # capabilities_json живёт на connection-account, НЕ на connection — чипы обязаны появиться
    from app.db import SessionLocal
    from app.models.cloudflare import CloudflareConnection, CloudflareConnectionAccount
    with SessionLocal() as db:
        c = CloudflareConnection(label="c1", secret_ref="env:CLOUDFLARE_API_TOKEN",
                                 token_kind="user", status="ok")
        db.add(c); db.commit()
        db.add(CloudflareConnectionAccount(connection_id=c.id, cloudflare_account_id="accHEX",
                                           status="ok", capabilities_json={"zones_read": "allowed"}))
        db.commit()
    r = client.get("/settings/cloudflare")
    assert "zones_read" in r.text


def test_zone_table_has_dns_cert_columns_and_ssl_value(client):
    # аудит §11: колонки DNS/cert существуют; universal_ssl_status доезжает в колонку SSL,
    # а не теряется из-за разъезда заголовков/ячеек
    from app.db import SessionLocal
    from app.models.cloudflare import (CloudflareZoneMirror, CloudflareDnsRecordMirror,
                                        CloudflareCertificatePackMirror)
    with SessionLocal() as db:
        db.add(CloudflareZoneMirror(cloudflare_account_id="acc", cf_zone_id="zc1",
                                    name="cols.ru", status="active", universal_ssl_status="active"))
        db.add(CloudflareDnsRecordMirror(cloudflare_zone_id="zc1", cf_record_id="rec1",
                                         type="A", name="cols.ru"))
        db.add(CloudflareCertificatePackMirror(cloudflare_zone_id="zc1", cf_pack_id="pk1",
                                               status="active"))
        db.commit()
    r = client.get("/settings/cloudflare")
    assert ">DNS<" in r.text and ">cert<" in r.text
    assert "cols.ru" in r.text and "active" in r.text  # SSL-значение видно
```

- [ ] **Шаг 2. Запуск → FAIL** (роут 404).

- [ ] **Шаг 3. Роут** — `panel.py`:

  (Импорты в panel.py: `func` из sqlalchemy, `CloudflareConnectionAccount`,
  `CloudflareDnsRecordMirror`, `CloudflareCertificatePackMirror` — добавить, если ещё нет.)

```python
@router.get("/settings/cloudflare", response_class=HTMLResponse)
def settings_cloudflare_view(request: Request):
    with SessionLocal() as db:
        conns = db.query(CloudflareConnection).order_by(CloudflareConnection.id).all()
        accounts = db.query(CloudflareAccount).order_by(CloudflareAccount.name).all()
        zones = (db.query(CloudflareZoneMirror)
                   .order_by(CloudflareZoneMirror.name).all())
        # capability-чипы: capabilities_json живёт на CloudflareConnectionAccount (НЕ на
        # CloudflareConnection) — агрегируем по connection (allowed побеждает denied/unknown).
        caps_by_conn: dict[int, dict] = {}
        for ca in db.query(CloudflareConnectionAccount).all():
            d = caps_by_conn.setdefault(ca.connection_id, {})
            for k, v in (ca.capabilities_json or {}).items():
                if d.get(k) != "allowed":
                    d[k] = v
        conn_rows = [{"c": c, "caps": caps_by_conn.get(c.id, {})} for c in conns]
        # привязка зоны к Site — по внешнему hex зоны (backfill P0): Site.cf_zone_id (legacy) ИЛИ
        # Site.cf_zone_mirror_id (новое); собрать карту для колонки «Site»
        by_zone = {}
        for s in db.query(Site).all():
            if s.cf_zone_id:
                by_zone.setdefault(s.cf_zone_id, s)
        # DNS/cert-паки для колонок «DNS»/«cert» (аудит §11) — счётчики non-missing по зоне
        dns_counts = dict(db.query(CloudflareDnsRecordMirror.cloudflare_zone_id,
                                   func.count(CloudflareDnsRecordMirror.id))
                            .filter(CloudflareDnsRecordMirror.missing_since.is_(None))
                            .group_by(CloudflareDnsRecordMirror.cloudflare_zone_id).all())
        cert_counts = dict(db.query(CloudflareCertificatePackMirror.cloudflare_zone_id,
                                    func.count(CloudflareCertificatePackMirror.id))
                             .filter(CloudflareCertificatePackMirror.missing_since.is_(None))
                             .group_by(CloudflareCertificatePackMirror.cloudflare_zone_id).all())
        rows = [{"z": z, "site": by_zone.get(z.cf_zone_id),
                 "dns": dns_counts.get(z.cf_zone_id, 0),
                 "certs": cert_counts.get(z.cf_zone_id, 0)} for z in zones]
    return templates.TemplateResponse("settings_cloudflare.html", {
        "request": request, "active": "settings",
        "conn_rows": conn_rows, "accounts": accounts, "rows": rows,
        "auth_configured": bool(settings.PANEL_USER and settings.PANEL_PASS),
    })
```

- [ ] **Шаг 4. Шаблон** — `settings_cloudflare.html` (компактно, реальные классы base.html):

```jinja
{% extends "base.html" %}
{% block content %}
<h2>Настройки <span class="hint">Cloudflare — только наблюдение, без изменений в CF</span></h2>
<div class="chips">
  <a class="chip" href="/settings">Воронка</a>
  <a class="chip on" href="/settings/cloudflare">Cloudflare</a>
</div>

<section class="station">
  <div class="plate">
    <b>Подключения</b>
    <details class="what"><summary>зачем</summary>
      Токен не хранится в БД — только ссылка <code>secret_ref</code>
      (<code>env:CLOUDFLARE_API_TOKEN</code> либо <code>file:&lt;имя&gt;</code> в
      read-only каталоге). Полный токен не идёт через веб-форму.</details>
  </div>
  {% if not auth_configured %}
    <p class="err">Cloudflare-операции недоступны: задайте PANEL_USER/PANEL_PASS в .env.</p>
  {% endif %}
  <div class="wrap"><table>
    <tr><th>Метка</th><th>secret_ref</th><th>Тип</th><th>Статус</th><th>Возможности</th></tr>
    {% for cr in conn_rows %}{% set c = cr.c %}
    <tr>
      <td>{{ c.label }}</td><td class="num">{{ c.secret_ref }}</td><td>{{ c.token_kind }}</td>
      <td>{% if c.status == 'ok' %}<span class="led-ok">ok</span>
          {% elif c.status == 'error' %}<span class="led-bad">error</span>
          {% else %}<span class="led-warn">не проверен</span>{% endif %}</td>
      <td>
        {# capabilities_json — на connection-account; в роут пришло агрегированным в cr.caps #}
        {% for cap, outcome in cr.caps.items() %}<span class="chip {{ 'on' if outcome == 'allowed' }}">{{ cap }}</span>{% endfor %}
        {% if not cr.caps %}—{% endif %}
      </td>
    </tr>
    {% endfor %}
  </table></div>
  <form method="post" action="/settings/cloudflare/sync">
    <button class="btn btn-acc" {{ 'disabled' if not auth_configured }}>Синхронизировать</button>
  </form>
</section>

<section class="station">
  <div class="plate"><b>Аккаунты и зоны</b>
    <details class="what"><summary>зачем</summary>
      Наблюдаемая правда Cloudflare. «не проверено» — не «ок»: пока origin TLS не подтверждён прямой
      проверкой, SSL показан как unknown (аудит RECHECK §3).</details>
  </div>
  <div class="wrap"><table>
    <tr><th>Зона</th><th>Аккаунт</th><th>Site</th><th>CF-статус</th><th>NS</th><th>DNS</th>
        <th>SSL</th><th>origin TLS</th><th>DNSSEC</th><th>cert</th><th>drift</th>
        <th>наблюдалось</th></tr>
    {% for r in rows %}{% set z = r.z %}
    <tr>
      <td>{{ z.name }}</td>
      <td class="num">{{ z.cloudflare_account_id }}</td>
      <td>{{ r.site.id if r.site else '—' }}</td>
      <td>{{ z.status }}{% if z.missing_since %} <span class="led-warn">пропала</span>{% endif %}</td>
      <td class="num">{{ (z.name_servers_json or [])|join(', ') or '—' }}</td>
      <td class="num">{{ r.dns }}</td>
      <td>{% if z.universal_ssl_status %}{{ z.universal_ssl_status }}
          {% else %}<span class="led-warn">не проверено</span>{% endif %}</td>
      <td>{% if z.origin_tls_status == 'ready' %}<span class="led-ok">ready</span>
          {% elif z.origin_tls_status == 'failed' %}<span class="led-bad">failed</span>
          {% else %}<span class="led-warn">не проверено</span>{% endif %}</td>
      <td>{{ z.dnssec_status or '—' }}</td>
      <td class="num">{{ r.certs or '—' }}</td>
      <td>{{ z.drift_status or '—' }}</td>
      <td class="num">{{ z.zones_seen_at.strftime('%m-%d %H:%M') if z.zones_seen_at else '—' }}</td>
    </tr>
    {% endfor %}
  </table></div>
</section>
{% endblock %}
```

- [ ] **Шаг 5. Chip в `settings.html`:** добавить сразу под `h2` тот же блок `.chips` с активной
  «Воронка» и ссылкой на «Cloudflare» (переключатель виден с обеих сторон).

- [ ] **Шаг 6.** Проверить глазами (Playwright-скриншот через TestClient+SQLite, см. CLAUDE.md
  «Панель: дизайн»). `pytest backend/tests/test_cf_panel.py -q` → PASS; pyflakes; коммит:

```bash
git add backend/app/templates/settings_cloudflare.html backend/app/templates/settings.html \
  backend/app/api/panel.py backend/tests/test_cf_panel.py
git commit -F - <<'EOF'
feat(cf/P0): read-only вкладка /settings/cloudflare (Подключения + Аккаунты и зоны)

Chip-переключатель Воронка/Cloudflare (паттерн /domains↔/domains/pool, без нового пункта
сайдбара). Подключения — capability-чипы и secret_ref-инструкция, НЕ форма ввода токена. Таблица
зон показывает origin TLS как «не проверено», а не фиктивное «ок». Только классы base.html.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
```

---

## Задача 8: финализация волны P0 — тесты аудита §12, pyflakes, ревью целиком

**Files:**
- Test: `backend/tests/test_cf_p0_acceptance.py` (сводные acceptance-проверки §12/§13)
- (правки — только если ревью что-то вскрыло)

**Interfaces:** Consumes всё из задач 1–7. Ничего нового не производит.

- [ ] **Шаг 1. Acceptance-тесты §12/§13.** Точечно уже покрыты (задачи 3/4): envelope
  `success=false`, пагинация 51+ зон/аккаунтов, empty≠error, user/account verify-endpoint,
  `find_zone_in_account` по account.id, upsert без дублей, failed/empty list ≠ deleted, external
  hex, unmanaged без Domain/Site side-effect. **Здесь — литеральный код недостающих сценариев**
  (`editable=false`, «два connection не смешивают headers», «cert packs в child mirrors», DNS 51+,
  timeout не утекает токеном, zone A capability ≠ zone B):

```python
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
    def with_token(cls, *a, **k): return _CUR[0]
    def verify_token(self, kind, account_id=""): return {"status": "active"}
    def list_accounts_paginated(self): return [{"id": "accHEX", "name": "Acc"}]
    def list_zones_paginated(self, account_id): return self._zones
    def find_zone_in_account(self, name, account_id):
        return next((z for z in self._zones if z["name"] == name), None)
    def list_dns_paginated(self, zone_id, type=None, name=None): return self._dns
    def get_zone_setting(self, zone_id, setting_id):
        return {"id": setting_id, "value": "off", "editable": self._editable}
    def list_universal_certificate_packs(self, zone_id): return self._packs
    def get_dnssec(self, zone_id): return {"status": "disabled"}

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
```

  MVP §13 (подключить `secret_ref` → реальные account id + все зоны 51+ → какая зона у какого Site
  → drift HTTPS/TLS/SSL как observed без мутаций) — проверяется вживую на боксе после мержа
  (транспорт/пагинация/sync уже покрыты кодом выше и в задачах 3/4).

- [ ] **Шаг 2.** Полный сьют: `docker compose run --rm backend pytest backend/tests/ -q` — весь
  зелёный (было 327 + новые).

- [ ] **Шаг 3.** `.venv/bin/python -m pyflakes backend/app backend/tests` — чисто.

- [ ] **Шаг 4.** Финальное ревью всей волны P0 через `combine-reviewer` (opus): 8 хард-инвариантов
  + подтвердить, что **нигде не вызывается Cloudflare PATCH/POST/DELETE** (grep по `set_ssl`,
  `create_zone`, `add_a_record`, `update_a_record`, `add_txt_record` — 0 новых call-sites),
  `provisioning.py` не изменён, оба хард-гейта проекта нетронуты.

- [ ] **Шаг 5.** Коммит acceptance-набора:

```bash
git add backend/tests/test_cf_p0_acceptance.py
git commit -F - <<'EOF'
test(cf/P0): acceptance-набор аудита §12/§13 + проверка «ни одной CF-мутации»

Сводные transport/sync-проверки (пагинация 51+, empty≠error, missing≠deleted, external hex,
unmanaged read-only, per-setting settings, zone-scoped capability) и MVP-критерии §13.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
```

---

## Дальше (не в этом плане)

- **P1** — account-aware provisioning и origin-TLS state machine (прямой TLS/SNI-probe `:443` до
  `ssl=strict`, вкладка перестаёт врать про SSL); `provisioning.py` мигрирует на
  `find_zone_in_account` и `Site.cloudflare_account_id`.
- **P2** — desired-state профиль и diff-preview (drift-расчёт) **без единой мутации**.
- **P3** — durable подтверждаемые операции/items (мутации за human-подтверждением, resource-scoped
  capability перед каждой мутацией).
- **P4** — миграции origin/registrar/transfer (NS-автоматика, `integrations/registrar.py`).
- **P5** — опциональные performance/security/email/analytics-настройки.
