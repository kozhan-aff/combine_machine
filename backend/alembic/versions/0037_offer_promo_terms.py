"""условия промокода в оффере: что даёт код — для текста и подписи у кнопки"""
import sqlalchemy as sa
from alembic import op

revision = "0037_offer_promo_terms"
down_revision = "0036_research_theme_blocks"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("offers", sa.Column("promo_terms", sa.Text))


def downgrade():
    op.drop_column("offers", "promo_terms")
