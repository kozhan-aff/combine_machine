"""page critic score/notes/checked_at (Спека 4, 2026-07-18)"""
from alembic import op
import sqlalchemy as sa

revision = "0023_page_critic"
down_revision = "0022_offer_settings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("pages", sa.Column("critic_score", sa.Float(), nullable=True))
    op.add_column("pages", sa.Column("critic_notes", sa.JSON(), nullable=True))
    op.add_column("pages", sa.Column(
        "critic_checked_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("pages", "critic_checked_at")
    op.drop_column("pages", "critic_notes")
    op.drop_column("pages", "critic_score")
