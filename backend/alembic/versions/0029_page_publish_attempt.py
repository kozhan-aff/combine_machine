"""pages.publish_attempted_at: ротация стадии publish по давности последней попытки (G3)"""
from alembic import op
import sqlalchemy as sa

revision = "0029_page_publish_attempt"
down_revision = "0028_site_offer"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("pages", sa.Column("publish_attempted_at", sa.DateTime(timezone=True)))


def downgrade():
    op.drop_column("pages", "publish_attempted_at")
