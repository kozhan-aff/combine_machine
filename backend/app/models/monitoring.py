"""Indexation history over time (from GSC URL Inspection). See BUILD_SPEC.md §5."""
from datetime import datetime
from sqlalchemy import String, ForeignKey, DateTime, func
from sqlalchemy.orm import Mapped, mapped_column
from app.db import Base


class IndexHistory(Base):
    __tablename__ = "index_history"

    id: Mapped[int] = mapped_column(primary_key=True)
    page_id: Mapped[int] = mapped_column(ForeignKey("pages.id"))
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    index_status: Mapped[str] = mapped_column(String(32))
    coverage_state: Mapped[str | None] = mapped_column(String(255))
