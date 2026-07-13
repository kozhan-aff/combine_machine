"""backfill lane + вернуть домены, выброшенные вердиктом из-за lane=NULL

Найдено на живом боксе 2026-07-13. `lane` заполняется только с коммита 69ef659 (2026-07-06);
у записей старше него он NULL. Старый acquirability_verdict трактовал любой не-bid лейн как
free («домен обязан быть свободен, а он занят -> его выкупили») и слал такие домены в
rejected/not_acquirable. Так утекли ЛУЧШИЕ домены базы — clara-c.ru (score 0.89, RD 2219,
возраст 16 лет) и ещё 28.

Код уже починен (scoring.acquirability_verdict: 'taken' только при lane='free'). Эта миграция
чинит ДАННЫЕ, которые он успел испортить:
  1) backorder без лейна -> lane='bid' (фид всегда bid, normalize_row ставит его безусловно);
  2) выброшенные по not_acquirable С lane=NULL -> обратно в discovered, чтобы воронка их
     пересудила. Домены с lane='bid' и прошедшим дедлайном НЕ трогаем: их отбраковка законна
     (дроп прошёл, домен занят), и возврат гонял бы их по кругу.

Revision ID: 0008
Revises: 0007
"""
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ПОРЯДОК ВАЖЕН: возврат ищет домены ПО lane IS NULL — сделай backfill первым, и искать
    # станет нечего (все backorder уже были бы 'bid'), а испорченные записи остались бы в
    # rejected навсегда.
    # Возвращаем ТОЛЬКО тех, кого есть чем судить после возврата:
    #   · source='backorder' — следующая строка проставит им lane='bid', а дедлайн у них есть
    #     из delete_date (это и есть 29 потерянных: clara-c.ru и Co.);
    #   · либо дедлайн уже известен — вердикт сможет сказать waiting/taken по дате.
    # Легаси сырых витрин БЕЗ дедлайна не воскрешаем: взять дату им неоткуда (старые zip-листы
    # cctld не переиздаются, дозаполнение в discovery._insert сработает только для домена из
    # ТЕКУЩЕГО листа), вердикт вечно отвечал бы 'unknown', и домен навсегда осел бы в discovered
    # балластом — накручивая плитку «найдено» и налог на квоту A-Parser.
    op.execute("""
        UPDATE domains SET status = 'discovered', reject_reason = NULL
        WHERE status = 'rejected' AND reject_reason = 'not_acquirable' AND lane IS NULL
          AND (source = 'backorder' OR acquire_deadline IS NOT NULL)
    """)
    op.execute("UPDATE domains SET lane = 'bid' WHERE source = 'backorder' AND lane IS NULL")


def downgrade() -> None:
    # Необратимо по природе: до апгрейда мы не знали, какие именно домены были отбракованы
    # ошибочно, а какие — законно. Откат вернул бы в rejected и невиновных. Данные, испорченные
    # багом, откатывать назад в испорченное состояние незачем.
    pass
