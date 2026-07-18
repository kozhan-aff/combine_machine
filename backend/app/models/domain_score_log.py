"""Append-only история решений scoring.score_domain() по домену. Каждый прогон
воронки (T0-T3) добавляет НОВУЮ строку — Domain.score_breakdown остаётся "последний
снимок" для UI-бейджей, эта таблица хранит историю ВСЕХ прогонов, не только
последнего. Расширяет уже работающий паттерн job_run (кросс-процессный реестр в
PostgreSQL), не новая инфраструктура. См.
docs/superpowers/specs/2026-07-18-domain-score-log-design.md."""
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class DomainScoreLog(Base):
    __tablename__ = "domain_score_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    domain_id: Mapped[int] = mapped_column(ForeignKey("domains.id"), index=True)
    # nullable: score_domain() бывает вызван вне трекнутого job_run (ручной "▶" на
    # одном домене из /domains, см. panel.py POST /domains/{domain_id}/score).
    run_id: Mapped[int | None] = mapped_column(ForeignKey("job_run.id"), nullable=True)
    outcome: Mapped[str] = mapped_column(String(16))          # unresolved|rejected|scored
    reject_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    sig: Mapped[dict] = mapped_column(JSONB)                  # полный снимок sig из _funnel()
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("ix_domain_score_log_domain_created", "domain_id", "created_at"),
    )
