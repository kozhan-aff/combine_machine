"""Сердцебиение воркера (F8-11): строка job_run name='worker' с живым updated_at.

Воркер — отдельный процесс; при выключенном автопилоте tick() ничего не пишет, и по БД было
не отличить «воркер жив, автопилот выключен» от «контейнер мёртв». Теперь воркер раз в минуту
трогает свою строку, а панель (/diag, Пульт) и docker healthcheck смотрят на её возраст.
Строка не 'running' (status='done'), поэтому single-flight индекс, live() и реап её не касаются.

CLI для healthcheck контейнера: `python -m app.services.heartbeat` -> exit 0, если сердце свежее.
"""
import sys
from datetime import datetime, timedelta, timezone

NAME = "worker"
BEAT_SEC = 60            # как часто воркер бьёт
STALE_SEC = 180          # старше — воркер считаем мёртвым (три пропущенных удара)


def beat(autopilot_on: bool | None = None) -> None:
    from sqlalchemy import select
    from app.db import SessionLocal
    from app.models.job import JobRun

    now = datetime.now(timezone.utc)
    msg = "" if autopilot_on is None else ("автопилот включён" if autopilot_on else "автопилот выключен")
    with SessionLocal() as db:
        row = db.execute(select(JobRun).where(JobRun.name == NAME)).scalars().first()
        if row is None:
            row = JobRun(name=NAME, trigger="auto", status="done", started_at=now, updated_at=now,
                         finished_at=now)
            db.add(row)
        row.updated_at, row.message = now, msg
        db.commit()


def status() -> dict:
    """{'alive': bool, 'age_sec': int|None, 'note': str} — для /diag и healthcheck."""
    from sqlalchemy import select
    from app.db import SessionLocal
    from app.models.job import JobRun

    with SessionLocal() as db:
        row = db.execute(select(JobRun).where(JobRun.name == NAME)).scalars().first()
        if row is None or row.updated_at is None:
            return {"alive": False, "age_sec": None, "note": ""}
        at = row.updated_at if row.updated_at.tzinfo else row.updated_at.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - at).total_seconds()
        return {"alive": age <= STALE_SEC, "age_sec": int(age), "note": row.message or ""}


def _cli() -> int:
    try:
        return 0 if status()["alive"] else 1
    except Exception:  # noqa: BLE001 — БД недоступна = не здоров
        return 1


if __name__ == "__main__":
    sys.exit(_cli())
