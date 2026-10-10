"""когда досье сайта собирали в последний раз: пауза перед повтором пустого досье"""
import sqlalchemy as sa
from alembic import op

revision = "0038_site_research_checked_at"
down_revision = "0037_offer_promo_terms"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("sites", sa.Column("research_checked_at", sa.DateTime(timezone=True)))


def downgrade():
    op.drop_column("sites", "research_checked_at")
