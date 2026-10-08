"""sites.offer_id: оффер, привязанный к сайту явно (S6-13/S7-12)"""
from alembic import op
import sqlalchemy as sa

revision = "0028_site_offer"
down_revision = "0027_site_provision_state"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("sites", sa.Column("offer_id", sa.Integer()))
    op.create_foreign_key("fk_sites_offer_id", "sites", "offers", ["offer_id"], ["id"])
    # backfill: у уже привязанных сайтов основной оффер — самый ранний из site_offers (как
    # выбирал прежний код), чтобы существующие сайты не потеряли оффер после выкатки
    op.execute("UPDATE sites SET offer_id = (SELECT MIN(so.offer_id) FROM site_offers so "
               "WHERE so.site_id = sites.id) WHERE offer_id IS NULL")


def downgrade():
    op.drop_constraint("fk_sites_offer_id", "sites", type_="foreignkey")
    op.drop_column("sites", "offer_id")
