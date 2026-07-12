"""Реестр длинных задач — строка в БД на прогон, а не dict в памяти процесса.

Старый jobs.py жил внутри процесса backend, поэтому свип автопилота (процесс worker)
панели был невидим вовсе. Здесь пишут оба.

started_at/updated_at ставим Python-side tz-aware (как AutonomyRun, не server_default):
на SQLite server_default вернул бы naive-строку и сломал сравнение с now(tz) при отсечке
протухших прогонов.
"""
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Index, Integer, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class JobRun(Base):
    __tablename__ = "job_run"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(32), index=True)          # discovery|score|recheck|sweep
    trigger: Mapped[str] = mapped_column(String(16), default="manual")  # manual (кнопка) | auto (воркер)
    status: Mapped[str] = mapped_column(String(16), default="running")  # running|done|failed|cancelled
    stage: Mapped[str] = mapped_column(String(32), default="")          # ключ текущей стадии
    stages: Mapped[list] = mapped_column(JSONB, default=list)           # [{key,label,state}] — чипы
    done: Mapped[int] = mapped_column(Integer, default=0)
    total: Mapped[int] = mapped_column(Integer, default=0)              # 0 = неопределённый режим
    current: Mapped[str] = mapped_column(String(255), default="")       # что в работе (домен/источник)
    message: Mapped[str] = mapped_column(String(400), default="")       # итог прогона
    error: Mapped[str | None] = mapped_column(String(400), nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # single-flight МЕЖДУ ПРОЦЕССАМИ: воркер не запустит второй score, пока идёт ручной
    # (сегодня может — и они вдвоём жгут квоту A-Parser).
    __table_args__ = (
        Index("uq_job_run_running", "name", unique=True,
              postgresql_where=text("status = 'running'"),
              sqlite_where=text("status = 'running'")),
    )
