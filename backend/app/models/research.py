"""Досье конкурентов (site_research) и дизайн-макет сайта (site_theme) — спека 2026-10-10 §4.4, §7.5."""
from datetime import datetime
from sqlalchemy import String, Text, Integer, JSON, ForeignKey, DateTime, Index, func
from sqlalchemy.orm import Mapped, mapped_column
from app.db import Base


class SiteResearch(Base):
    __tablename__ = "site_research"

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))          # review | comparison | howto | market
    query: Mapped[str] = mapped_column(String(255))
    rank: Mapped[int] = mapped_column(Integer)             # позиция в выдаче (1..)
    url: Mapped[str] = mapped_column(Text)
    final_url: Mapped[str | None] = mapped_column(Text)
    domain: Mapped[str | None] = mapped_column(String(255))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    words: Mapped[int] = mapped_column(Integer, default=0)
    headings: Mapped[list | None] = mapped_column(JSON)    # [[tag, text], ...]
    tables: Mapped[list | None] = mapped_column(JSON)      # [[[cell, ...], ...], ...]
    faq: Mapped[list | None] = mapped_column(JSON)         # [{"q","a"}]
    numbers: Mapped[list | None] = mapped_column(JSON)     # [{"value","ctx"}]
    css_tokens: Mapped[dict | None] = mapped_column(JSON)  # {"fonts","colors","container_px","components"}
    text: Mapped[str | None] = mapped_column(Text)         # видимый текст целиком — для шинглов критика
    screenshot_path: Mapped[str | None] = mapped_column(String(512))
    note: Mapped[str | None] = mapped_column(Text)         # причина пропуска/отказа словами

    __table_args__ = (Index("ix_site_research_site_kind", "site_id", "kind"),)


class SiteTheme(Base):
    __tablename__ = "site_theme"

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(16), default="ok")   # ok | failed
    theme: Mapped[dict | None] = mapped_column(JSON)
    layout_html: Mapped[str | None] = mapped_column(Text)
    css: Mapped[str | None] = mapped_column(Text)
    brief: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(String(64))
    screenshots: Mapped[dict | None] = mapped_column(JSON)          # {"1366": path, "390": path}
    operator_note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
