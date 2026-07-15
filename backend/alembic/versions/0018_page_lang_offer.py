"""pages: lang + offer_id — страница помнит, под что реально писалась (F26, аудит 2026-07-14)

ЧТО ЧИНИТ. `publish.publish_site` пересчитывал «текущий активный оффер сайта» ЗАНОВО в момент
публикации (`_pick_offer` — ТА ЖЕ query, что и `content.generate_site`), и брал `lang` из НЕГО
же (`offer.language or "ru"`). Если набор `SiteOffer` сайта менялся между генерацией и
публикацией (оператор добавил/сменил оффер с меньшим `Offer.id` — сортировка `order_by(Offer.id)`
детерминирована, но не стабильна во времени), выбор в момент публикации мог дать ДРУГОЙ оффер,
чем тот, про который реально писал LLM. Итог: страница, написанная про бренд A на английском,
публикуется со ссылкой на бренд B и `<html lang="ru">` — контент и ссылка рассинхронизированы,
а разметка языка страницы врёт.

Добавляем на `pages` то, что должно было фиксироваться с самого начала: `lang` (язык генерации)
и `offer_id` (какой именно оффер был использован для ЭТОЙ страницы). Пишет ТОЛЬКО
`content.generate_site`, в момент создания строки — это факт истории («под что писали»), не
текущее состояние сайта. `publish_site` теперь читает их С САМОЙ СТРАНИЦЫ.

NULLABLE, БЕЗ BACKFILL. Существующие страницы (созданные до этой миграции) не несут этой
информации — восстанавливать её задним числом нечем (LLM-вызов, породивший конкретный body,
уже произошёл и не переигрывается). Для NULL `publish_site` держит прежнее поведение
(`_pick_offer` — «текущий активный оффер сайта») как явный fallback только для legacy-строк,
а не тихую деградацию.

`offer_id` — FK на `offers.id` БЕЗ `ondelete`: в проекте нет роута, удаляющего Offer (только
`toggle` active/inactive), так что сиротская ссылка при удалении оффера — не сценарий,
который наблюдался вживую (в отличие от 0014/0016/0017, где дедуп был обязателен).

Revision ID: 0018
Revises: 0017
Create Date: 2026-07-15
"""
from alembic import op
import sqlalchemy as sa

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("pages", sa.Column("lang", sa.String(length=8), nullable=True))
    op.add_column("pages", sa.Column("offer_id", sa.Integer(), nullable=True))
    op.create_foreign_key("fk_pages_offer_id", "pages", "offers", ["offer_id"], ["id"])


def downgrade() -> None:
    op.drop_constraint("fk_pages_offer_id", "pages", type_="foreignkey")
    op.drop_column("pages", "offer_id")
    op.drop_column("pages", "lang")
