"""domain_list: списки чистоты доменов (UT1 / blocklistproject), W2c-ut1"""
from alembic import op
import sqlalchemy as sa

revision = "0033_domain_list"
down_revision = "0032_domain_indexes"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "domain_list",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("domain", sa.String(255), nullable=False),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("domain", "category", "source", name="uq_domain_list"),
    )
    op.create_index("ix_domain_list_domain", "domain_list", ["domain"])
    op.create_index("ix_domain_list_src_cat", "domain_list", ["source", "category"])


def downgrade():
    op.drop_index("ix_domain_list_src_cat", table_name="domain_list")
    op.drop_index("ix_domain_list_domain", table_name="domain_list")
    op.drop_table("domain_list")
