"""Domain candidates, their scoring, and acquisition orders. See BUILD_SPEC.md §5 + docs/DONORS.md."""
from datetime import datetime
from sqlalchemy import String, Integer, Numeric, Boolean, Text, ForeignKey, DateTime, Index, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db import Base

# Заказ ещё НЕ закрыт: он либо ждёт человека (pending_confirm), либо уходит провайдеру
# (ordering — транзиентный claim), либо уже у него (ordered). Пока такой есть — второй заказ
# на тот же домен завести нельзя: это прямой путь заплатить за домен дважды.
# ЕДИНСТВЕННЫЙ источник истины: отсюда и предикат индекса, и проверка в services/acquisition.
#
# `failed` тут нет намеренно — это состояние ретрая, и повтор идемпотентен по деньгам (execute
# сперва спрашивает провайдера find_order'ом). НО ИЗ ЭТОГО НЕ СЛЕДУЕТ, что `failed` безопасен
# сам по себе. Правда — только про НОВЫЕ заявки: `create_order` до `failed`-домена не доходит,
# его останавливает политика статусов (домен висит в `purchasing`, оттуда заявку не заводят).
# А вот заказ ИЗ `failed` В открытый статус двигают ещё двое — `execute_confirmed_order`
# (ретрай -> `ordering`) и `poll_orders` (фантом всё-таки долетел -> `ordered`), — и рядом с
# ними `failed` может СОСЕДСТВОВАТЬ с открытым заказом того же домена (легаси-дубли, схлопнутые
# миграцией 0010). Оба обязаны спросить `acquisition._open_order_id` ПЕРЕД записью, иначе ловят
# IntegrityError (ревью Задачи 7). Раньше здесь было написано «домен под failed заперт политикой
# статусов» — это половина правды, выданная за всю.
#
# `ordering` из этого списка выходит ДВУМЯ путями: своим execute (он же его и поставил) и — если
# execute убили в полёте — поллингом, по протухшему `claimed_at` (аудит F11, см. ниже). Спрашивать
# `_open_order_id` поллингу тут не о чем: `ordering` УЖЕ открыт, то есть эта строка и есть
# единственный открытый заказ домена, а `ordering` -> `ordered` остаётся внутри предиката индекса.
#
# МЕНЯЕШЬ ЭТОТ КОРТЕЖ — ПИШИ НОВУЮ МИГРАЦИЮ: живой PostgreSQL держит предикат из 0010 и сам его
# не перечитает (в тестах `create_all` берёт предикат отсюда — и разъезд был бы не виден).
# Сторожит test_order_uniqueness.py::test_index_predicate_matches_the_migration.
OPEN_ORDER_STATUSES = ("pending_confirm", "ordering", "ordered")
_OPEN_ORDER_SQL = "status IN (%s)" % ", ".join(f"'{s}'" for s in OPEN_ORDER_STATUSES)


class Domain(Base):
    __tablename__ = "domains"

    id: Mapped[int] = mapped_column(primary_key=True)
    domain: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    source: Mapped[str | None] = mapped_column(String(32))          # backorder | optimizator | list
    status: Mapped[str] = mapped_column(String(32), default="discovered", index=True)
    # discovered | scored | approved | rejected | purchasing | purchased | live | dropped
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # metrics (Stage B)
    dr: Mapped[float | None] = mapped_column(Numeric)
    referring_domains: Mapped[int | None] = mapped_column(Integer)
    backlinks: Mapped[int | None] = mapped_column(Integer)
    organic_traffic: Mapped[int | None] = mapped_column(Integer)

    # donor quality (Stage C)
    live_referring_domains: Mapped[int | None] = mapped_column(Integer)
    anchors: Mapped[dict | None] = mapped_column(JSONB)             # anchor distribution
    spam_anchor_ratio: Mapped[float | None] = mapped_column(Numeric)
    topical_relevance: Mapped[float | None] = mapped_column(Numeric)  # 0..1

    # history (Stage D)
    age_years: Mapped[float | None] = mapped_column(Numeric)
    first_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    wayback_checked: Mapped[bool] = mapped_column(Boolean, default=False)
    prior_flags: Mapped[dict | None] = mapped_column(JSONB)          # {adult, pharma, casino, spam, gambling}
    indexed_echo: Mapped[bool | None] = mapped_column(Boolean)       # old content still indexed

    # risk (Stage E)
    rkn_listed: Mapped[bool | None] = mapped_column(Boolean)
    blacklisted: Mapped[bool | None] = mapped_column(Boolean)        # Spamhaus DBL / SURBL
    # ЛЕГАСИ-КОЛОНКА, всегда NULL: производителя у неё не было НИКОГДА, а hard-reject по ней в
    # compute_score был (аудит 2026-07-14, F5) — гейт притворялся проверкой юр-риска, которой нет.
    # Ветку отказа сняли, колонку оставили: она ничего не ломает и ждёт настоящей реализации.
    trademark_risk: Mapped[bool | None] = mapped_column(Boolean)

    # decision (Stage F)
    clean: Mapped[bool | None] = mapped_column(Boolean)
    score: Mapped[float | None] = mapped_column(Numeric)
    score_breakdown: Mapped[dict | None] = mapped_column(JSONB)
    notes: Mapped[str | None] = mapped_column(Text)

    # funnel bookkeeping
    reject_reason: Mapped[str | None] = mapped_column(String(32))    # low_rd|feed_flag|too_young|rkn|blacklist|history_dirty|low_score|not_acquirable
    whois_created: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # дата регистрации (первичный возраст)
    feed_flags: Mapped[dict | None] = mapped_column(JSONB)           # сырые флаги источника: {rkn, judicial, block}

    # приобретаемость (Мозг M1)
    lane: Mapped[str | None] = mapped_column(String(8))              # bid | free | null
    acquire_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # дедлайн ловли (backorder delete_date)
    acquire_price: Mapped[float | None] = mapped_column(Numeric)     # базовая цена выкупа (backorder тариф)
    price_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # когда в последний раз whois'ом сверяли, что домен ВСЁ ЕЩЁ можно купить. Скоринг решает
    # приобретаемость один раз (T1) и больше не возвращается — а список доноров протухает:
    # одобренный вчера домен сегодня может быть уже зарегистрирован кем-то другим. NULL = ни
    # разу не перепроверяли после скоринга.
    acquirability_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    visitors: Mapped[int | None] = mapped_column(Integer)            # инфо-сигнал из фида (вес 0)
    tic: Mapped[int | None] = mapped_column(Integer)                 # Яндекс ТИЦ из фида (вес 0)

    orders: Mapped[list["AcquisitionOrder"]] = relationship(back_populates="domain")


class AcquisitionOrder(Base):
    __tablename__ = "acquisition_orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    domain_id: Mapped[int] = mapped_column(ForeignKey("domains.id"))
    provider: Mapped[str] = mapped_column(String(32))               # backorder | optimizator
    provider_order_id: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="pending_confirm")
    # pending_confirm | ordering | ordered | caught | failed | cancelled
    cost: Mapped[float | None] = mapped_column(Numeric)
    confirmed_by_human: Mapped[bool] = mapped_column(Boolean, default=False)  # HARD GATE
    ordered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # КОГДА execute ЗАБРАЛ строку на отправку (claim `-> ordering`). `ordering` — статус
    # ТРАНЗИЕНТНЫЙ: он живёт секунды между claim'ом и ответом провайдера. Убей процесс в этом
    # окне (деплой перезапустил контейнер, OOM, docker restart) — и строка останется в
    # `ordering` НАВСЕГДА: execute её не берёт (claim пускает только pending_confirm/failed),
    # cancel не снимает (тоже только эти два), поллинг её не видел вовсе. Домен под ней вечно
    # висит в `purchasing`, а деньги, возможно, ушли (аудит F11).
    # Отметка времени и отличает ЖИВУЮ отправку от трупа: пока claim свеж — строку держит живой
    # execute, и трогать её нельзя (забрать чужой заказ = заплатить дважды); протухший claim
    # (acquisition.STUCK_CLAIM_MIN) разбирает поллинг ПРАВДОЙ ПРОВАЙДЕРА. NULL у `ordering` =
    # claim старого кода (до миграции 0011) = заведомо переживший рестарт труп, см. 0011.
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[dict | None] = mapped_column(JSONB)

    domain: Mapped["Domain"] = relationship(back_populates="orders")

    # ОДИН открытый заказ на домен — инвариант БД, а не удача SELECT'а в create_order (аудит F10).
    # «Нет ли уже заявки» и вставка новой — два разных запроса, а писателей у очереди двое И В
    # РАЗНЫХ ПРОЦЕССАХ: кнопка «в очередь» (панель) и стадия queue автопилотного свипа (worker).
    # Под READ COMMITTED чужая незакоммиченная строка не видна — обе транзакции честно видят
    # «заказа нет» и обе вставляют. Дальше человек подтверждает КАЖДУЮ заявку и платит дважды.
    # Частичный уникальный индекс — тот же приём, что single-flight у job_run; предикат обоим
    # диалектам, иначе тест зелен на SQLite, а прод дыряв (или наоборот).
    __table_args__ = (
        Index("uq_open_order_per_domain", "domain_id", unique=True,
              postgresql_where=text(_OPEN_ORDER_SQL),
              sqlite_where=text(_OPEN_ORDER_SQL)),
    )
