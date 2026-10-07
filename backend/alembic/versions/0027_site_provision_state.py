"""sites: шаг провижна, origin_https, NS-ожидание (S4-02/04, S5-04, S7-08)"""
from alembic import op
import sqlalchemy as sa

revision = "0027_site_provision_state"
down_revision = "0026_secret_override"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("sites", sa.Column("origin_https", sa.String(16)))
    op.add_column("sites", sa.Column("provision_step", sa.String(24)))
    op.add_column("sites", sa.Column("cf_name_servers", sa.JSON()))
    op.add_column("sites", sa.Column("ns_waiting_since", sa.DateTime(timezone=True)))
    op.add_column("sites", sa.Column("ns_checked_at", sa.DateTime(timezone=True)))


def downgrade():
    for c in ("ns_checked_at", "ns_waiting_since", "cf_name_servers", "provision_step", "origin_https"):
        op.drop_column("sites", c)
