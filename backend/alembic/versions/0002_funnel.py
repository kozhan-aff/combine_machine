"""funnel: reject_reason/whois_created/feed_flags + scoring_settings

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-06
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("domains", sa.Column("reject_reason", sa.String(32)))
    op.add_column("domains", sa.Column("whois_created", sa.DateTime(timezone=True)))
    op.add_column("domains", sa.Column("feed_flags", postgresql.JSONB()))
    op.create_table(
        "scoring_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("min_referring_domains", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("min_age_years", sa.Numeric(), nullable=False, server_default="3.0"),
        sa.Column("approve_at", sa.Numeric(), nullable=False, server_default="0.70"),
        sa.Column("manual_review_at", sa.Numeric(), nullable=False, server_default="0.40"),
        sa.Column("sources_enabled", postgresql.JSONB()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    # Витрины (cctld/reg_ru/sweb) — сырьё без RD/лейна/дедлайна; платный Ahrefs зовётся ровно
    # для доменов БЕЗ RD (только эти источники его не дают) -> включать их с нуля значит молча
    # жечь деньги на капчу с самой первой установки. Рантайм-дефолт SOURCES_ENABLED в
    # scoring_config.py уже держит их выключенными — INSERT обязан ему соответствовать, а не
    # расходиться (аудит 2026-07-14, F21). Уже накатанные базы правит корректирующая 0015.
    op.execute(
        "INSERT INTO scoring_settings (id, sources_enabled) VALUES "
        "(1, '{\"backorder\": true, \"cctld\": false, \"reg_ru\": false, \"sweb\": false}')"
    )


def downgrade() -> None:
    op.drop_table("scoring_settings")
    op.drop_column("domains", "feed_flags")
    op.drop_column("domains", "whois_created")
    op.drop_column("domains", "reject_reason")
