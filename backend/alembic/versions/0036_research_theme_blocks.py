"""досье конкурентов, дизайн-макет, блоки страницы, тумблеры research/design/edit (спека 2026-10-10)"""
import sqlalchemy as sa
from alembic import op

revision = "0036_research_theme_blocks"
down_revision = "0035_drop_dead_overrides"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "site_research",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("site_id", sa.Integer, sa.ForeignKey("sites.id"), nullable=False, index=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("query", sa.String(255), nullable=False),
        sa.Column("rank", sa.Integer, nullable=False),
        sa.Column("url", sa.Text, nullable=False),
        sa.Column("final_url", sa.Text),
        sa.Column("domain", sa.String(255)),
        sa.Column("fetched_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("words", sa.Integer, nullable=False, server_default="0"),
        sa.Column("headings", sa.JSON), sa.Column("tables", sa.JSON), sa.Column("faq", sa.JSON),
        sa.Column("numbers", sa.JSON), sa.Column("css_tokens", sa.JSON),
        sa.Column("text", sa.Text), sa.Column("screenshot_path", sa.String(512)), sa.Column("note", sa.Text),
    )
    op.create_index("ix_site_research_site_kind", "site_research", ["site_id", "kind"])
    op.create_table(
        "site_theme",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("site_id", sa.Integer, sa.ForeignKey("sites.id"), nullable=False, index=True),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("status", sa.String(16), nullable=False, server_default="ok"),
        sa.Column("theme", sa.JSON), sa.Column("layout_html", sa.Text), sa.Column("css", sa.Text),
        sa.Column("brief", sa.Text), sa.Column("model", sa.String(64)), sa.Column("screenshots", sa.JSON),
        sa.Column("operator_note", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.add_column("pages", sa.Column("blocks", sa.JSON))
    op.add_column("pages", sa.Column("blocks_stale", sa.Boolean, nullable=False, server_default=sa.false()))
    for col in ("auto_research", "auto_design", "auto_edit"):
        op.add_column("autonomy_settings", sa.Column(col, sa.Boolean, nullable=False, server_default=sa.false()))
    op.add_column("autonomy_settings", sa.Column("cap_research", sa.Integer, nullable=False, server_default="5"))
    op.add_column("autonomy_settings", sa.Column("cap_design", sa.Integer, nullable=False, server_default="3"))


def downgrade():
    for col in ("cap_design", "cap_research", "auto_edit", "auto_design", "auto_research"):
        op.drop_column("autonomy_settings", col)
    op.drop_column("pages", "blocks_stale")
    op.drop_column("pages", "blocks")
    op.drop_table("site_theme")
    op.drop_index("ix_site_research_site_kind", table_name="site_research")
    op.drop_table("site_research")
