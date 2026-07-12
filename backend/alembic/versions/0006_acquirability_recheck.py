"""recheck: когда домен в последний раз сверяли whois'ом на «всё ещё свободен»

Список доноров протухает — одобренный домен могут выкупить. Скоринг решает
приобретаемость один раз (T1), поэтому нужна отдельная перепроверка; эта колонка
хранит её время (NULL = после скоринга ни разу не проверяли).

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-12
"""
from alembic import op
import sqlalchemy as sa

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("domains",
                  sa.Column("acquirability_checked_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("domains", "acquirability_checked_at")
