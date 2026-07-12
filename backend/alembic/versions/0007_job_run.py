"""job_run: реестр длинных задач в БД — кросс-процессный прогресс, стадии, стоп

In-memory реестр жил в процессе backend, поэтому свип автопилота (процесс worker) панели
был не виден. Частичный уникальный индекс (name) WHERE status='running' — single-flight
между процессами: воркер не запустит второй score поверх ручного.

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-12
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "job_run",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(32), nullable=False),
        sa.Column("trigger", sa.String(16), nullable=False, server_default="manual"),
        sa.Column("status", sa.String(16), nullable=False, server_default="running"),
        sa.Column("stage", sa.String(32), nullable=False, server_default=""),
        sa.Column("stages", JSONB, nullable=False, server_default="[]"),
        sa.Column("done", sa.Integer, nullable=False, server_default="0"),
        sa.Column("total", sa.Integer, nullable=False, server_default="0"),
        sa.Column("current", sa.String(255), nullable=False, server_default=""),
        sa.Column("message", sa.String(400), nullable=False, server_default=""),
        sa.Column("error", sa.String(400), nullable=True),
        sa.Column("cancel_requested", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_job_run_name", "job_run", ["name"])
    op.create_index("uq_job_run_running", "job_run", ["name"], unique=True,
                    postgresql_where=sa.text("status = 'running'"))


def downgrade() -> None:
    op.drop_index("uq_job_run_running", table_name="job_run")
    op.drop_index("ix_job_run_name", table_name="job_run")
    op.drop_table("job_run")
