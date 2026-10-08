"""domains: индексы под сортировки/фильтры панели и воронки (F8-12)"""
from alembic import op

revision = "0032_domain_indexes"
down_revision = "0031_order_confirm_ttl"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index("ix_domains_status_deadline", "domains", ["status", "acquire_deadline"])
    op.create_index("ix_domains_status_score", "domains", ["status", "score"])
    op.create_index("ix_domains_reject_reason", "domains", ["reject_reason"])
    op.create_index("ix_domains_lane", "domains", ["lane"])


def downgrade():
    op.drop_index("ix_domains_lane", table_name="domains")
    op.drop_index("ix_domains_reject_reason", table_name="domains")
    op.drop_index("ix_domains_status_score", table_name="domains")
    op.drop_index("ix_domains_status_deadline", table_name="domains")
