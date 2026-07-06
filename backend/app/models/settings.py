"""Рантайм-настройки скоринга (single-row, id=1). Дефолты — в scoring_config.py.

Пороги воронки редактируются на /settings; сервис settings.py читает/пишет эту строку.
"""
from datetime import datetime
from sqlalchemy import Integer, Numeric, DateTime, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from app.db import Base


class ScoringSettings(Base):
    __tablename__ = "scoring_settings"

    id: Mapped[int] = mapped_column(primary_key=True)                # всегда 1
    min_referring_domains: Mapped[int] = mapped_column(Integer, default=1)
    min_age_years: Mapped[float] = mapped_column(Numeric, default=3.0)
    approve_at: Mapped[float] = mapped_column(Numeric, default=0.70)
    manual_review_at: Mapped[float] = mapped_column(Numeric, default=0.40)
    max_whois_per_run: Mapped[int] = mapped_column(Integer, default=200)  # кап whois-вызовов за прогон
    sources_enabled: Mapped[dict] = mapped_column(JSONB, default=dict)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True),
                                                        server_default=func.now(), onupdate=func.now())
