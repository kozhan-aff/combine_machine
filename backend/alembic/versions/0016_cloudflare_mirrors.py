"""cloudflare P0: 8 read-only mirror-таблиц + UNIQUE(sites.domain_id) (Cloudflare Control Center,
задача 1: docs/superpowers/plans/2026-07-14-cloudflare-p0.md)

ЧТО СОЗДАЁТ. Восемь новых таблиц под read-only "зеркало" состояния Cloudflare — они хранят ТОЛЬКО
то, что реально пришло от API (или честно пусто/ошибка), а не то, что мы туда положили: connection
(секрет НЕ хранится, только `secret_ref` — env:... либо file:...), account (по внешнему hex-ID),
connection_account (M:N токен↔аккаунт + capability-флаги is_primary_for_*), capability_observation
(журнал проверок токена), zone_mirror (зона + NS/DNSSEC/parent-DS/TLS-наблюдения), zone_setting_
observation (settings API, БЕЗ brotli — deprecated 2024-08-15), dns_record_mirror, certificate_pack_
mirror. `cloudflare_account_id` в дочерних таблицах — ВНЕШНИЙ hex Cloudflare, НЕ FK на локальный
`cloudflare_accounts.id`: sync (задача 4) может наблюдать зону раньше, чем появится строка аккаунта,
и жёсткий FK уронил бы эту легитимную последовательность.

ПОЧЕМУ ДЕДУП ДО UNIQUE(sites.domain_id). Как и 0014 (uq_page_per_path), это НЕ первая миграция на
чистую БД — сайт как продукт уже прогонялся на живом боксе, а провижн Site мог создаваться дважды
гонкой ДВУХ ПРОЦЕССОВ (кнопка в панели vs. стадия автопилотного свипа в воркере): SELECT «сайт уже
есть для этого домена» под READ COMMITTED не видит чужую незакоммиченную строку — оба честно видели
«сайта нет» и оба его создавали. UNIQUE(domain_id) без предварительного схлопывания дублей упал бы
на грязной живой базе и уронил git-pull-деплой (тот же урок, что 0010/0014). Cloudflare-привязка
(`cf_zone_mirror_id`) обязана указывать на ОДИН сайт домена — иначе backfill (задача 4) не знал бы,
какой из дублей-Site обновлять.

КОГО ОСТАВЛЯЕМ при дедупе. В отличие от Page (где выживал published > edited > draft), у Site нет
явной иерархии статусов, но есть прокси "сколько в него вложено" — число сгенерированных страниц:
оставляем сайт с БОЛЬШИМ количеством Page (тай-брейк — меньший id, старейший). У проигравших Page
переносим на keeper, а при коллизии url_path (после 0014 на sites уже действует uq_page_per_path)
дубли удаляем, а не переносим (перенос лёг бы в тот же уникальный индекс и уронил миграцию).
Коллизия проверяется НЕ только «проигравший против keeper»: при 3+ дублях-Site на один домен два
РАЗНЫХ проигравших могут делить url_path, которого нет у keeper — это тоже коллизия, ранжируем
все страницы группы (keeper, url_path) разом (своя страница keeper — первая, иначе детерминированный
тай-брейк по site_id/id) и удаляем всё, что не заняло первое место, ДО переноса.

Revision ID: 0016
Revises: 0015
Create Date: 2026-07-15
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


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
    #
    # Коллизия url_path проверяется по ВСЕМ страницам, метящим в одного keeper, а не только
    # "проигравший против keeper": при 3+ дублях-Site на один домен два РАЗНЫХ проигравших могут
    # делить url_path, которого нет у keeper (напр. оба клонировали один шаблон и оба сгенерили
    # /about) — сверка только с keeper это не ловит, перенос обеих строк на keeper.site_id упал бы
    # на uq_page_per_path (ревью Задачи 1, F-миграция-0016). Поэтому ранжируем ВСЕ страницы внутри
    # группы (keeper, url_path): свою страницу keeper (если есть) оставляем первой, иначе —
    # детерминированный тай-брейк по site_id/id; всё, что не заняло первое место, удаляется ДО
    # переноса — тогда UPDATE ниже переносит не более одной страницы на путь.
    op.execute("""
        WITH ranked AS (
            SELECT s.id, s.domain_id,
                   FIRST_VALUE(s.id) OVER (
                       PARTITION BY s.domain_id
                       ORDER BY (SELECT count(*) FROM pages p WHERE p.site_id = s.id) DESC, s.id ASC
                   ) AS keeper
            FROM sites s
        ),
        page_dupes AS (
            SELECT pg.id AS page_id,
                   ROW_NUMBER() OVER (
                       PARTITION BY r.keeper, pg.url_path
                       ORDER BY (pg.site_id = r.keeper) DESC, pg.site_id ASC, pg.id ASC
                   ) AS dup_rank
            FROM pages pg
            JOIN ranked r ON r.id = pg.site_id
        )
        DELETE FROM pages
        WHERE id IN (SELECT page_id FROM page_dupes WHERE dup_rank > 1);
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
