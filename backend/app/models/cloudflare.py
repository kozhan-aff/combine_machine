"""Cloudflare Control Center — mirror-таблицы (P0, read-only правда).

Секрет токена НЕ хранится в БД даже зашифрованным — только `secret_ref` (env:... либо safe
filename в allowlisted read-only mount), резолвится в services/cf_secret.py (следующие задачи).
`cloudflare_account_id` во всех дочерних строках — ВНЕШНИЙ hex-ID Cloudflare, не локальный PK
(identity аккаунта в проекте = его внешний hex): sync может наблюдать зону раньше, чем появится
строка CloudflareAccount, поэтому дочерние таблицы НЕ держат жёсткий FK на CloudflareAccount.id.
brotli намеренно отсутствует среди zone-настроек: Zone Setting API deprecated 2024-08-15. Пустой
список ответа API при ошибке транспорта НЕ значит «зона/запись удалена» — sync-сервис (задача 4)
ставит `missing_since`/`last_error_safe`, а не молча `status='deleted'` (аудит §2).

P0 — ТОЛЬКО модель + миграция + импорт legacy .env-connection как строки. Ни одного сетевого
Cloudflare-запроса эта задача не делает."""
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
