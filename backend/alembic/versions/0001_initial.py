"""initial schema — all tables from BUILD_SPEC §5 / app.models

Revision ID: 0001
Revises:
Create Date: 2026-07-05

Hand-written (no live DB at authoring time). Validate offline:
    alembic upgrade head --sql
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "domains",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("domain", sa.String(255), nullable=False),
        sa.Column("source", sa.String(32)),
        sa.Column("status", sa.String(32), nullable=False, server_default="discovered"),
        sa.Column("discovered_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("dr", sa.Numeric()),
        sa.Column("referring_domains", sa.Integer()),
        sa.Column("backlinks", sa.Integer()),
        sa.Column("organic_traffic", sa.Integer()),
        sa.Column("live_referring_domains", sa.Integer()),
        sa.Column("anchors", postgresql.JSONB()),
        sa.Column("spam_anchor_ratio", sa.Numeric()),
        sa.Column("topical_relevance", sa.Numeric()),
        sa.Column("age_years", sa.Numeric()),
        sa.Column("first_seen", sa.DateTime(timezone=True)),
        sa.Column("wayback_checked", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("prior_flags", postgresql.JSONB()),
        sa.Column("indexed_echo", sa.Boolean()),
        sa.Column("rkn_listed", sa.Boolean()),
        sa.Column("blacklisted", sa.Boolean()),
        sa.Column("trademark_risk", sa.Boolean()),
        sa.Column("clean", sa.Boolean()),
        sa.Column("score", sa.Numeric()),
        sa.Column("score_breakdown", postgresql.JSONB()),
        sa.Column("notes", sa.Text()),
    )
    op.create_index("ix_domains_domain", "domains", ["domain"], unique=True)
    op.create_index("ix_domains_status", "domains", ["status"])

    op.create_table(
        "acquisition_orders",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("domain_id", sa.Integer(), sa.ForeignKey("domains.id"), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("provider_order_id", sa.String(64)),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending_confirm"),
        sa.Column("cost", sa.Numeric()),
        sa.Column("confirmed_by_human", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("ordered_at", sa.DateTime(timezone=True)),
        sa.Column("result", postgresql.JSONB()),
    )

    op.create_table(
        "sites",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("domain_id", sa.Integer(), sa.ForeignKey("domains.id"), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="provisioning"),
        sa.Column("cf_zone_id", sa.String(64)),
        sa.Column("origin_ip", sa.String(64)),
        sa.Column("aapanel_site_name", sa.String(255)),
        sa.Column("doc_root", sa.String(512)),
        sa.Column("niche", sa.String(255)),
        sa.Column("template", sa.String(255)),
        sa.Column("gsc_verified", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("sitemap_submitted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("published_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_sites_status", "sites", ["status"])

    op.create_table(
        "pages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("site_id", sa.Integer(), sa.ForeignKey("sites.id"), nullable=False),
        sa.Column("url_path", sa.String(512), nullable=False),
        sa.Column("title", sa.String(512)),
        sa.Column("status", sa.String(32), nullable=False, server_default="draft"),
        sa.Column("body", sa.Text()),
        sa.Column("index_status", sa.String(32), nullable=False, server_default="unknown"),
        sa.Column("index_checked_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("published_at", sa.DateTime(timezone=True)),
    )

    op.create_table(
        "offers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("brand", sa.String(128), nullable=False),
        sa.Column("network", sa.String(128)),
        sa.Column("promo_code", sa.String(64)),
        sa.Column("affiliate_link", sa.Text(), nullable=False),
        sa.Column("country", sa.String(8)),
        sa.Column("language", sa.String(8)),
        sa.Column("payout_type", sa.String(32)),
        sa.Column("payout_value", sa.String(64)),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("notes", sa.Text()),
    )

    op.create_table(
        "site_offers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("site_id", sa.Integer(), sa.ForeignKey("sites.id"), nullable=False),
        sa.Column("offer_id", sa.Integer(), sa.ForeignKey("offers.id"), nullable=False),
        sa.Column("country", sa.String(8)),
        sa.Column("placement", sa.String(64)),
    )

    op.create_table(
        "index_history",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("page_id", sa.Integer(), sa.ForeignKey("pages.id"), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("index_status", sa.String(32), nullable=False),
        sa.Column("coverage_state", sa.String(255)),
    )


def downgrade() -> None:
    op.drop_table("index_history")
    op.drop_table("site_offers")
    op.drop_table("offers")
    op.drop_table("pages")
    op.drop_table("sites")
    op.drop_table("acquisition_orders")
    op.drop_index("ix_domains_status", table_name="domains")
    op.drop_index("ix_domains_domain", table_name="domains")
    op.drop_table("domains")
