"""v2 M1: язык/тема домена, настройки международной воронки, память DR, архив РФ-пула"""
import json

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0025_v2_m1"
down_revision = "0024_domain_score_log"
branch_labels = None
depends_on = None

TLD_DEFAULT = ["com", "net", "org", "online", "xyz", "site", "co.uk", "mx", "co", "si", "nl", "in"]
BRANDS_DEFAULT = ["nordvpn", "expressvpn", "surfshark", "protonvpn", "cyberghost", "ipvanish",
                  "privateinternetaccess", "mullvad", "windscribe", "hotspotshield", "tunnelbear",
                  "purevpn", "vyprvpn", "hidemyass", "atlasvpn", "privadovpn", "hideme", "strongvpn",
                  "zenmate", "avast", "kaspersky", "norton"]
SOURCES_V2 = {"dropcatch": False, "nominet": True, "mx": True, "emd": True}
SOURCES_V1 = {"backorder": True, "cctld": False, "reg_ru": False, "sweb": False}

# РФ-кандидаты больше не нужны (v2 ушёл из РФ). Купленные и дальше — не трогаем; уже
# отклонённые — тоже (их причина — улика, а не мусор).
ARCHIVE_SQL = ("UPDATE domains SET status='rejected', reject_reason='legacy_ru' "
               "WHERE status IN ('discovered','scored','approved') "
               "AND (domain LIKE '%.ru' OR domain LIKE '%.su' OR domain LIKE '%.xn--p1ai')")
UNARCHIVE_SQL = ("UPDATE domains SET status='discovered', reject_reason=NULL "
                 "WHERE reject_reason='legacy_ru'")


def _jsonb(v):
    return sa.text("'" + json.dumps(v) + "'::jsonb")


def upgrade():
    op.add_column("domains", sa.Column("market_lang", sa.String(8), nullable=True))
    op.add_column("domains", sa.Column("topic", sa.String(120), nullable=True))
    op.add_column("scoring_settings", sa.Column("min_dr", sa.Numeric(), nullable=False, server_default="5"))
    op.add_column("scoring_settings", sa.Column("tld_allowlist", postgresql.JSONB(), nullable=False,
                                                server_default=_jsonb(TLD_DEFAULT)))
    op.add_column("scoring_settings", sa.Column("brand_tokens", postgresql.JSONB(), nullable=False,
                                                server_default=_jsonb(BRANDS_DEFAULT)))
    op.add_column("scoring_settings", sa.Column("emd_sets", postgresql.JSONB(), nullable=False,
                                                server_default=_jsonb([])))
    op.add_column("scoring_settings", sa.Column("max_links_per_run", sa.Integer(), nullable=False,
                                                server_default="500"))
    op.add_column("scoring_settings", sa.Column("max_deep_per_run", sa.Integer(), nullable=False,
                                                server_default="20"))
    op.add_column("scoring_settings", sa.Column("spam_anchor_max", sa.Numeric(), nullable=False,
                                                server_default="0.2"))
    # пол остатка units Ahrefs (решение оператора Р3): автопилот гоняет скоринг раз в час, капы
    # «на прогон» месячный бюджет не держат
    op.add_column("scoring_settings", sa.Column("units_floor", sa.Integer(), nullable=False,
                                                server_default="300000"))
    # память бесплатного DR (решение оператора Р4): DR один раз на домен, записи живут 4 суток
    op.create_table(
        "dr_seen",
        sa.Column("domain", sa.String(253), primary_key=True),
        sa.Column("dr", sa.Numeric(), nullable=True),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
    )
    # источники v2 и сброс весов: старые ключи (rd_proxy/indexed_echo) больше не существуют,
    # а смесь старых значений с новыми дефолтами дала бы шкалу, которую никто не задавал
    op.execute(sa.text("UPDATE scoring_settings SET sources_enabled = CAST(:s AS JSONB), "
                       "weights = CAST('{}' AS JSONB)").bindparams(s=json.dumps(SOURCES_V2)))
    op.execute(ARCHIVE_SQL)


def downgrade():
    op.execute(UNARCHIVE_SQL)
    op.execute(sa.text("UPDATE scoring_settings SET sources_enabled = CAST(:s AS JSONB)")
               .bindparams(s=json.dumps(SOURCES_V1)))
    op.drop_table("dr_seen")
    for col in ("units_floor", "spam_anchor_max", "max_deep_per_run", "max_links_per_run", "emd_sets",
                "brand_tokens", "tld_allowlist", "min_dr"):
        op.drop_column("scoring_settings", col)
    op.drop_column("domains", "topic")
    op.drop_column("domains", "market_lang")
