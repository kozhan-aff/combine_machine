"""Site.last_attempt_at — кап провижна на attempts + ротация (аудит F2.1)."""
from alembic import op
import sqlalchemy as sa

revision = "0021_site_last_attempt"
down_revision = "0020_zone_detail_errors"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sites", sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("sites", "last_attempt_at")
