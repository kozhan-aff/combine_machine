"""autonomy_run.status VARCHAR(16)->(32): вмещает completed_with_errors (аудит 2026-07-15, P0).

VARCHAR(16) отклонял 21-символьный терминальный статус на PostgreSQL (SQLite длину не
проверяет — тесты этого не ловили). Только расширение, данные не трогаются."""
from alembic import op
import sqlalchemy as sa

revision = "0019_autonomy_status_width"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("autonomy_run", "status",
                    existing_type=sa.String(16), type_=sa.String(32),
                    existing_nullable=False, existing_server_default=sa.text("'running'"))


def downgrade() -> None:
    op.alter_column("autonomy_run", "status",
                    existing_type=sa.String(32), type_=sa.String(16),
                    existing_nullable=False, existing_server_default=sa.text("'running'"))
