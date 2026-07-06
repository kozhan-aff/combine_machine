"""acquirability: lane/acquire_*/visitors/tic + max_whois_per_run

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-06
"""
from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("domains", sa.Column("lane", sa.String(8)))
    op.add_column("domains", sa.Column("acquire_deadline", sa.DateTime(timezone=True)))
    op.add_column("domains", sa.Column("acquire_price", sa.Numeric()))
    op.add_column("domains", sa.Column("price_checked_at", sa.DateTime(timezone=True)))
    op.add_column("domains", sa.Column("visitors", sa.Integer()))
    op.add_column("domains", sa.Column("tic", sa.Integer()))
    op.add_column("scoring_settings",
                  sa.Column("max_whois_per_run", sa.Integer(), nullable=False, server_default="200"))


def downgrade() -> None:
    op.drop_column("scoring_settings", "max_whois_per_run")
    for col in ("tic", "visitors", "price_checked_at", "acquire_price", "acquire_deadline", "lane"):
        op.drop_column("domains", col)
