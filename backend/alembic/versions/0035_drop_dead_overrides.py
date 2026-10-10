"""secret_override: убрать значения ключей v1, которых больше нет в конфиге (из панели их не удалить)"""
from alembic import op

revision = "0035_drop_dead_overrides"
down_revision = "0034_domain_ranks"
branch_labels = None
depends_on = None

DEAD = ("SEO_DATA_PROVIDER", "DATAFORSEO_LOGIN", "DATAFORSEO_PASSWORD", "SERPAPI_KEY",
        "YANDEX_WORDSTAT_TOKEN", "REGRU_USERNAME", "REGRU_PASSWORD", "BROWSERLESS_URL", "N8N_URL")


def upgrade():
    keys = ", ".join(f"'{k}'" for k in DEAD)
    op.execute(f"DELETE FROM secret_override WHERE key IN ({keys})")


def downgrade():
    pass    # значения удалены безвозвратно — поля в конфиге всё равно отсутствуют
