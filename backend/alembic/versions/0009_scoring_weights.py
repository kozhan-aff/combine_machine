"""веса критериев оценки донора — в рантайм-настройки

Жалоба оператора (2026-07-13): «в настройках стоят все пункты, по которым идёт оценка донора —
нет возможности скорректировать». Так и было: /settings крутил только ПОРОГИ (min RD, возраст,
approve/manual, бюджеты), а сами веса (история/возраст/RD/эхо/DR) были зашиты в
scoring_config.WEIGHTS. Оператор видел, ПО ЧЕМУ его судят, но не мог изменить ни один вес.

Пустой {} = «дефолты из scoring_config» (settings.get_settings подставит их), поэтому миграция
не обязана знать текущие значения весов и не ломает уже настроенную строку.

Revision ID: 0009
Revises: 0008
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("scoring_settings",
                  sa.Column("weights", postgresql.JSONB(astext_type=sa.Text()),
                            nullable=True, server_default="{}"))


def downgrade() -> None:
    op.drop_column("scoring_settings", "weights")
