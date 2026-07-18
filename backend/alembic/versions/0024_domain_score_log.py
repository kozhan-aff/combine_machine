"""domain_score_log — append-only история решений score_domain()"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0024_domain_score_log"
down_revision = "0023_page_critic"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "domain_score_log",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("domain_id", sa.Integer(), sa.ForeignKey("domains.id"), nullable=False),
        sa.Column("run_id", sa.Integer(), sa.ForeignKey("job_run.id"), nullable=True),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("reject_reason", sa.String(32), nullable=True),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("sig", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_domain_score_log_domain_created", "domain_score_log",
                    ["domain_id", "created_at"])


def downgrade():
    op.drop_index("ix_domain_score_log_domain_created", table_name="domain_score_log")
    op.drop_table("domain_score_log")
