"""domain_ranks: срез рангов Common Crawl / Majestic по пулу кандидатов (W2d-cc-ranks)"""
from alembic import op
import sqlalchemy as sa

revision = "0034_domain_ranks"
down_revision = "0033_domain_list"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "domain_ranks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("domain", sa.String(255), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("harmonic_centrality", sa.Float(), nullable=True),
        sa.Column("pagerank", sa.Float(), nullable=True),
        sa.Column("n_hosts", sa.Integer(), nullable=True),
        sa.Column("pct", sa.Float(), nullable=True),
        sa.Column("rank_pos", sa.Integer(), nullable=True),
        sa.Column("release", sa.String(64), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("domain", "source", name="uq_domain_ranks"),
    )
    op.create_index("ix_domain_ranks_domain", "domain_ranks", ["domain"])
    op.create_index("ix_domain_ranks_source", "domain_ranks", ["source"])


def downgrade():
    op.drop_index("ix_domain_ranks_source", table_name="domain_ranks")
    op.drop_index("ix_domain_ranks_domain", table_name="domain_ranks")
    op.drop_table("domain_ranks")
