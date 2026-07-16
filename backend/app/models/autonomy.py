"""Автономия: single-row конфиг тумблеров/капов + run-лог свипов (кросс-процессный).

AutonomySettings — как scoring_settings (id=1, seed дефолтами через services/autonomy).
AutonomyRun — единственный способ шедулеру (воркер-контейнер) и панели видеть одно и то
же: in-memory jobs.py живёт лишь в процессе панели. started_at ставится Python-side
(tz-aware) — не server_default: иначе SQLite вернул бы naive-строку и сломал сравнение с
now(tz) в single-flight-замке.
"""
from datetime import datetime, timezone
from sqlalchemy import Integer, String, Boolean, DateTime, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from app.db import Base


class AutonomySettings(Base):
    __tablename__ = "autonomy_settings"

    id: Mapped[int] = mapped_column(primary_key=True)                 # всегда 1
    autopilot_on: Mapped[bool] = mapped_column(Boolean, default=False)      # мастер-выключатель
    sweep_interval_min: Mapped[int] = mapped_column(Integer, default=60)    # throttle между авто-свипами

    auto_discovery: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_score: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_queue: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_provision: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_generate: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_publish: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_check_index: Mapped[bool] = mapped_column(Boolean, default=False)

    cap_score: Mapped[int] = mapped_column(Integer, default=20)
    cap_queue: Mapped[int] = mapped_column(Integer, default=10)
    cap_provision: Mapped[int] = mapped_column(Integer, default=5)
    cap_generate: Mapped[int] = mapped_column(Integer, default=5)
    cap_publish: Mapped[int] = mapped_column(Integer, default=5)
    cap_check_index: Mapped[int] = mapped_column(Integer, default=20)
    # у discovery капа НЕТ — bulk-pull фида, не по-доменная стадия

    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True),
                                                        server_default=func.now(), onupdate=func.now())


class AutonomyRun(Base):
    __tablename__ = "autonomy_run"

    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    trigger: Mapped[str] = mapped_column(String(16), default="cron")     # cron | manual
    status: Mapped[str] = mapped_column(String(32), default="running")   # running|done|failed|cancelled|completed_with_errors
    counts: Mapped[dict] = mapped_column(JSONB, default=dict)            # {stage: n}
    errors: Mapped[list] = mapped_column(JSONB, default=list)           # ["stage: текст", ...]
