"""Provisioned sites and their content pages. See BUILD_SPEC.md §5."""
from datetime import datetime
from sqlalchemy import String, Text, Boolean, ForeignKey, DateTime, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db import Base


class Site(Base):
    __tablename__ = "sites"

    id: Mapped[int] = mapped_column(primary_key=True)
    domain_id: Mapped[int] = mapped_column(ForeignKey("domains.id"))
    status: Mapped[str] = mapped_column(String(32), default="provisioning", index=True)
    # provisioning | content | published | monitoring | pruned

    cf_zone_id: Mapped[str | None] = mapped_column(String(64))
    origin_ip: Mapped[str | None] = mapped_column(String(64))
    aapanel_site_name: Mapped[str | None] = mapped_column(String(255))
    doc_root: Mapped[str | None] = mapped_column(String(512))

    # Последний отказ на шаге SSL провижна (смена режима на Cloudflare). Не блокирует сайт —
    # vhost работает и на 80 — но при origin-only-80 именно этот шаг решает, поедет ли HTTPS,
    # и раньше он молча глотался (`except Exception: pass`). NULL = SSL встал (или ещё не
    # пробовали); текст = провижн доехал, но HTTPS под вопросом. Показывается на /sites/{id}.
    ssl_error: Mapped[str | None] = mapped_column(Text)

    niche: Mapped[str | None] = mapped_column(String(255))
    template: Mapped[str | None] = mapped_column(String(255))

    gsc_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    sitemap_submitted: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    pages: Mapped[list["Page"]] = relationship(back_populates="site")


class Page(Base):
    __tablename__ = "pages"

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"))
    url_path: Mapped[str] = mapped_column(String(512))
    title: Mapped[str | None] = mapped_column(String(512))
    status: Mapped[str] = mapped_column(String(32), default="draft")   # draft | edited | published
    # HARD GATE: published only allowed from `edited`
    body: Mapped[str | None] = mapped_column(Text)

    index_status: Mapped[str] = mapped_column(String(32), default="unknown")  # unknown | indexed | not_indexed
    index_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    site: Mapped["Site"] = relationship(back_populates="pages")
