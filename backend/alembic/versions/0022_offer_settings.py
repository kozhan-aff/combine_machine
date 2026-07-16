"""offer_settings: резервный URL при выключенном оффере (F3, аудит 2026-07-15)."""
from alembic import op
import sqlalchemy as sa

revision = "0022_offer_settings"
down_revision = "0021_site_last_attempt"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "offer_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("reserve_offer_url", sa.Text(), nullable=True),
    )
    op.execute("INSERT INTO offer_settings (id, reserve_offer_url) VALUES (1, NULL)")


def downgrade() -> None:
    op.drop_table("offer_settings")
