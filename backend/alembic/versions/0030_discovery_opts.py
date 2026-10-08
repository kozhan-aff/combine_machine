"""scoring_settings.discovery_opts: резерв кандидатов без DR, фильтры имени, валидаторы условного GET (G4)"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0030_discovery_opts"
down_revision = "0029_page_publish_attempt"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("scoring_settings", sa.Column("discovery_opts", postgresql.JSONB(), nullable=False,
                                                server_default=sa.text("'{}'::jsonb")))


def downgrade():
    op.drop_column("scoring_settings", "discovery_opts")
