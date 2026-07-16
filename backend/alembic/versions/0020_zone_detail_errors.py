"""zone-mirror: dns_error_safe/cert_error_safe — ошибки DNS/cert не глотать (аудит F1.3)."""
from alembic import op
import sqlalchemy as sa

revision = "0020_zone_detail_errors"
down_revision = "0019_autonomy_status_width"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("cloudflare_zone_mirrors", sa.Column("dns_error_safe", sa.Text(), nullable=True))
    op.add_column("cloudflare_zone_mirrors", sa.Column("cert_error_safe", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("cloudflare_zone_mirrors", "cert_error_safe")
    op.drop_column("cloudflare_zone_mirrors", "dns_error_safe")
