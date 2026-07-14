"""Domain candidates, their scoring, and acquisition orders. See BUILD_SPEC.md §5 + docs/DONORS.md."""
from datetime import datetime
from sqlalchemy import String, Integer, Numeric, Boolean, Text, ForeignKey, DateTime, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db import Base


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
    result: Mapped[dict | None] = mapped_column(JSONB)

    domain: Mapped["Domain"] = relationship(back_populates="orders")
