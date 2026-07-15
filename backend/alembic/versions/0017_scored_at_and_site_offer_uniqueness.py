"""domains.scored_at + UNIQUE(site_offers.site_id, offer_id) (аудит F24 + F25, Задача 17)

ЧТО ЧИНИТ.

F25 (scoring.py, отдельным коммитом): рескор без нового Ahrefs-наблюдения терял `authority` —
`sig["dr"]` не подхватывал уже сохранённый `d.dr`, и второй проход воронки считал авторитетность
от нуля, хотя Ahrefs про этот домен уже спрашивали. Схемной правки та часть не требует (это
`sig.setdefault`, не колонка) — сюда войдёт лишь спутник из того же брифа: колонка `scored_at`,
без которой непонятно, КОГДА домен последний раз довели до решения (approved/scored/rejected).
`discovered_at` — это момент открытия, не скоринга; `score_breakdown` времени не несёт вовсе.
Пишет только `score_domain`, в конце, рядом с `db.commit()` — тот же приём, что уже даёт
`acquirability_checked_at` (0006).

F24: `site_offers` привязывается ДВУМЯ писателями, панелью (`panel.py::attach_offer`) и API-
двойником (`pipeline.py::attach_offer`), и оба гейтят дубль ОДИНАКОВО — SELECT «эта пара
(site_id, offer_id) уже есть?», а вставка отдельным COMMIT'ом. Та же гонка ДВУХ ПРОЦЕССОВ, что
уже закрывали для `Site.domain_id` (0016, uq_site_per_domain) и `Page.url_path` (0014,
uq_page_per_path): под READ COMMITTED чужая незакоммиченная строка не видна, оба писателя
честно видят «нет» и оба вставляют. Дубль здесь не косметика — M4 вставляет блок оффера
(промокод/ссылка) в контент по КАЖДОЙ строке `site_offers` сайта, и дублирующая строка
продублировала бы сам оффер на странице.

ЖИВАЯ БАЗА МОЖЕТ БЫТЬ УЖЕ ГРЯЗНОЙ (урок 0010/0014/0016): CREATE UNIQUE INDEX на существующих
дублях уронит git-pull-деплой. У `SiteOffer` нет статусной иерархии (в отличие от `Page`,
где published > edited > draft) — оставляем МЕНЬШИЙ id (старейшая привязка), проигравших
удаляем. Ничто в проекте не ссылается на `site_offers.id` как на родителя (нет
`ForeignKey("site_offers.id")` ни в одной модели) — в отличие от 0014/0016, тут не нужно
чистить детей перед удалением родителя.

Revision ID: 0017
Revises: 0016
Create Date: 2026-07-15
"""
from alembic import op
import sqlalchemy as sa

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("domains", sa.Column("scored_at", sa.DateTime(timezone=True), nullable=True))

    # Дедуп ДО уникального индекса: оставляем меньший id на (site_id, offer_id), остальное — дубль
    # той же гонки SELECT-then-INSERT, что чинили 0014/0016.
    op.execute("""
        DELETE FROM site_offers
         WHERE id NOT IN (
             SELECT MIN(id) FROM site_offers GROUP BY site_id, offer_id
         )
    """)
    op.create_index("uq_site_offer", "site_offers", ["site_id", "offer_id"], unique=True)


def downgrade() -> None:
    op.drop_index("uq_site_offer", table_name="site_offers")
    op.drop_column("domains", "scored_at")
