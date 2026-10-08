"""acquisition_orders: confirmed_at (TTL подтверждения) и cost_currency (валюта суммы) — G6, S3-07/S3-08"""
from alembic import op
import sqlalchemy as sa

revision = "0031_order_confirm_ttl"
down_revision = "0030_discovery_opts"
branch_labels = None
depends_on = None


def upgrade():
    # NULL у confirmed_at при confirmed_by_human=true = подтверждение СТАРОГО кода: считается
    # просроченным (строже, не слабее) — оператор подтвердит заново; задним числом время не выдумываем.
    op.add_column("acquisition_orders", sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("acquisition_orders", sa.Column("cost_currency", sa.String(8), nullable=True))
    # Все прошлые суммы — рубли (оба канала v1 российские).
    op.execute("UPDATE acquisition_orders SET cost_currency = 'RUB' WHERE cost IS NOT NULL")


def downgrade():
    op.drop_column("acquisition_orders", "cost_currency")
    op.drop_column("acquisition_orders", "confirmed_at")
