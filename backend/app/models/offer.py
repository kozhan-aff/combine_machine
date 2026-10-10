"""Affiliate offers (brand + promo + link by geo) and site<->offer mapping.

This is the user-facing input of the whole machine: describe offers, the pipeline
builds sites around them. Decoupled from sites and geo-aware. See BUILD_SPEC.md §5.
"""
from sqlalchemy import String, Text, Boolean, ForeignKey, Index
from sqlalchemy.orm import Mapped, mapped_column
from app.db import Base


class Offer(Base):
    __tablename__ = "offers"

    id: Mapped[int] = mapped_column(primary_key=True)
    brand: Mapped[str] = mapped_column(String(128))                 # e.g. NordVPN
    network: Mapped[str | None] = mapped_column(String(128))        # affiliate network/program
    promo_code: Mapped[str | None] = mapped_column(String(64))
    # что даёт промокод («-65% на 2 года»): идёт в промпт писателя и в подпись у кнопки. Обязателен,
    # если промокод задан (promo_pair) — иначе модель придумывает бонус сама.
    promo_terms: Mapped[str | None] = mapped_column(Text)
    affiliate_link: Mapped[str] = mapped_column(Text)
    country: Mapped[str | None] = mapped_column(String(8))          # ISO geo, null = default/global
    language: Mapped[str | None] = mapped_column(String(8))         # ISO lang
    payout_type: Mapped[str | None] = mapped_column(String(32))     # CPA | RevShare | hybrid
    payout_value: Mapped[str | None] = mapped_column(String(64))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str | None] = mapped_column(Text)


def promo_pair(promo_code: str | None, promo_terms: str | None) -> tuple[str | None, str | None]:
    """(промокод, условия) в каноническом виде. Промокод без условий — ValueError; условия без
    промокода не хранятся: описывать нечего."""
    code, terms = (promo_code or "").strip() or None, (promo_terms or "").strip() or None
    if code and not terms:
        raise ValueError("у промокода нет условий: напиши, что он даёт (бонус, на что действует, срок)")
    return code, (terms if code else None)


class SiteOffer(Base):
    __tablename__ = "site_offers"

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"))
    offer_id: Mapped[int] = mapped_column(ForeignKey("offers.id"))
    country: Mapped[str | None] = mapped_column(String(8))          # geo-target for placement
    placement: Mapped[str | None] = mapped_column(String(64))

    # ОДИН ОФФЕР НА САЙТ НЕ БОЛЬШЕ ОДНОГО РАЗА (F24, аудит 2026-07-14). И panel.py, и pipeline.py
    # привязывают оффер SELECT'ом «уже есть?» + отдельным INSERT — та же гонка ДВУХ ПРОЦЕССОВ,
    # что уже чинили для Site.domain_id (uq_site_per_domain) и Page.url_path (uq_page_per_path):
    # под READ COMMITTED чужая незакоммиченная строка невидима, оба писателя честно видят «нет»
    # и оба вставляют. Дубль здесь не безобиден — вставка оффера в контент (M4) прошлась бы по
    # обеим строкам и продублировала бы блок с офером/промокодом на странице.
    __table_args__ = (Index("uq_site_offer", "site_id", "offer_id", unique=True),)


class OfferSettings(Base):
    """Single-row (id=1) рантайм-конфиг публикации офферов — тот же паттерн, что
    ScoringSettings/AutonomySettings (single-row, редактируется на лету через UI).

    reserve_offer_url: общий URL на весь портфель, на который публикация подставит ссылку
    ВМЕСТО ссылки уже выключенного оффера (F3, аудит 2026-07-15). NULL = резерв не настроен —
    сегодняшнее поведение (мёртвая ссылка остаётся как есть). Меняется ТОЛЬКО href — бренд/
    промокод в тексте страницы не трогаются (offer_id остаётся зафиксированным фактом истории,
    F26)."""
    __tablename__ = "offer_settings"

    id: Mapped[int] = mapped_column(primary_key=True)                  # всегда 1
    reserve_offer_url: Mapped[str | None] = mapped_column(Text)
