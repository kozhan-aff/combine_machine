"""secret_override — ключи и адреса сервисов, заданные из панели (поверх .env)"""
from alembic import op
import sqlalchemy as sa

revision = "0026_secret_override"
down_revision = "0025_v2_m1"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "secret_override",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade():
    op.drop_table("secret_override")
