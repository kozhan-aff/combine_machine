"""autonomy: autonomy_settings (тумблеры/капы) + autonomy_run (run-лог свипов)

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-07
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "autonomy_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("autopilot_on", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("sweep_interval_min", sa.Integer(), nullable=False, server_default="60"),
        sa.Column("auto_discovery", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("auto_score", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("auto_queue", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("auto_provision", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("auto_generate", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("auto_publish", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("auto_check_index", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("cap_score", sa.Integer(), nullable=False, server_default="20"),
        sa.Column("cap_queue", sa.Integer(), nullable=False, server_default="10"),
        sa.Column("cap_provision", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("cap_generate", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("cap_publish", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("cap_check_index", sa.Integer(), nullable=False, server_default="20"),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_table(
        "autonomy_run",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("trigger", sa.String(16), nullable=False, server_default="cron"),
        sa.Column("status", sa.String(16), nullable=False, server_default="running"),
        sa.Column("counts", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("errors", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
    )


def downgrade() -> None:
    op.drop_table("autonomy_run")
    op.drop_table("autonomy_settings")
