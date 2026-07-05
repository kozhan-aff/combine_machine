"""Affiliate offers (brand + promo + link by geo) and site<->offer mapping.

This is the user-facing input of the whole machine: describe offers, the pipeline
builds sites around them. Decoupled from sites and geo-aware. See BUILD_SPEC.md §5.
"""
from sqlalchemy import String, Text, Boolean, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column
from app.db import Base


class Offer(Base):
    __tablename__ = "offers"

    id: Mapped[int] = mapped_column(primary_key=True)
    brand: Mapped[str] = mapped_column(String(128))                 # e.g. NordVPN
    network: Mapped[str | None] = mapped_column(String(128))        # affiliate network/program
    promo_code: Mapped[str | None] = mapped_column(String(64))
    affiliate_link: Mapped[str] = mapped_column(Text)
    country: Mapped[str | None] = mapped_column(String(8))          # ISO geo, null = default/global
    language: Mapped[str | None] = mapped_column(String(8))         # ISO lang
    payout_type: Mapped[str | None] = mapped_column(String(32))     # CPA | RevShare | hybrid
    payout_value: Mapped[str | None] = mapped_column(String(64))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str | None] = mapped_column(Text)


class SiteOffer(Base):
    __tablename__ = "site_offers"

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"))
    offer_id: Mapped[int] = mapped_column(ForeignKey("offers.id"))
    country: Mapped[str | None] = mapped_column(String(8))          # geo-target for placement
    placement: Mapped[str | None] = mapped_column(String(64))
