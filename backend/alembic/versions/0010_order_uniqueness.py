"""acquisition_orders: один ОТКРЫТЫЙ заказ на домен — частичный уникальный индекс (аудит F10)

Проверка «нет ли уже заявки на этот домен» в create_order — это SELECT, а вставка — отдельный
COMMIT. Писателей у очереди двое И В РАЗНЫХ ПРОЦЕССАХ (кнопка «в очередь» в панели и стадия
queue автопилотного свипа в воркере), под READ COMMITTED чужая незакоммиченная строка невидима —
обе транзакции честно видели «заказа нет» и обе вставляли. Человек подтверждал каждую заявку и
платил за домен дважды. Тот же приём, что single-flight у job_run (0007).

ЖИВАЯ БАЗА МОЖЕТ БЫТЬ УЖЕ ГРЯЗНОЙ: дубли, которые старый код принимал, никуда не делись, и
CREATE UNIQUE INDEX на них упал бы — уронив git-pull-деплой. Поэтому сперва схлопываем дубли,
и НЕ одинаково:
  · лишний `pending_confirm` — провайдеру он не уходил НИКОГДА (это доденежная заявка), денег за
    ним нет: закрываем в `cancelled`;
  · лишний `ordering`/`ordered` — деньги МОГЛИ уйти: закрываем в `failed` + `maybe_sent`, то есть
    в «исход неизвестен». Это не сокрытие: строка остаётся в /queue со своим provider_order_id,
    `failed` опрашивается poll_orders (заказ найдётся у провайдера по elid), а отмена такого
    заказа запрещена — спрятать реально оплаченный нельзя.
Выживает самый «продвинутый» заказ домена (ordered > ordering > pending_confirm), при равенстве —
самый старый (наименьший id): именно его оператор видит в /queue.

ЧТО СХЛОПНУТАЯ СТРОКА МОЖЕТ, А ЧЕГО НЕТ (не преувеличивать — ревью Задачи 7). Пока ВЫЖИВШИЙ заказ
домена открыт, дубль в открытый статус не поднять: инвариант «одна открытая заявка на домен» —
это и запрет БД, и гард в services/acquisition (`_open_order_id`), который стоит и в poll_orders,
и в «↻ повторить». Обе кнопки честно скажут, что домен держит заказ #N, — но НЕ поднимут (до
этих гардов обе падали IntegrityError'ом: поллинг терял всю пачку, повтор выдавал SQL-трейс).
Дубль оживает сам, когда выживший закроется (пойман / не вышло / снят): тогда поллинг поднимет
его в `ordered`, а повтор снова доступен. До тех пор он — видимая запись о деньгах, которые
могли уйти, и разбирать её человеку.

Revision ID: 0010
Revises: 0009
Create Date: 2026-07-14
"""
from alembic import op
import sqlalchemy as sa

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

_NOTE = ("дубль открытого заказа на домен, закрыт миграцией 0010 "
         "(инвариант: одна открытая заявка на домен)")


def upgrade() -> None:
    # 1. Схлопнуть уже существующие дубли, иначе индекс не создастся на живой базе.
    op.execute(sa.text("""
        WITH ranked AS (
            SELECT id,
                   row_number() OVER (
                       PARTITION BY domain_id
                       ORDER BY CASE status WHEN 'ordered' THEN 0
                                            WHEN 'ordering' THEN 1
                                            ELSE 2 END, id) AS rn
            FROM acquisition_orders
            WHERE status IN ('pending_confirm', 'ordering', 'ordered')
        )
        UPDATE acquisition_orders o
           SET status = CASE WHEN o.status = 'pending_confirm' THEN 'cancelled' ELSE 'failed' END,
               result = COALESCE(o.result, '{}'::jsonb) ||
                        CASE WHEN o.status = 'pending_confirm'
                             THEN jsonb_build_object('note', :note)
                             ELSE jsonb_build_object('note', :note, 'maybe_sent', true) END
          FROM ranked r
         WHERE o.id = r.id AND r.rn > 1
    """).bindparams(note=_NOTE))

    # 2. Сам инвариант. Предикат — дословно OPEN_ORDER_STATUSES из app/models/domain.py.
    op.create_index("uq_open_order_per_domain", "acquisition_orders", ["domain_id"], unique=True,
                    postgresql_where=sa.text(
                        "status IN ('pending_confirm', 'ordering', 'ordered')"))


def downgrade() -> None:
    # Схлопнутые дубли назад не раскрываются (и не должны: заплатить дважды — не то состояние,
    # к которому откатываются). Снимаем только запрет.
    op.drop_index("uq_open_order_per_domain", table_name="acquisition_orders")
