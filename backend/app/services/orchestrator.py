"""M-оркестратор автономии. Двигает конвейер по включённым «авто»-стадиям до гейтов.

Тонкий диспетчер: НИКАКОЙ новой бизнес-логики — только (1) запрос подходящих сущностей,
(2) вызов существующего безопасного сервиса, (3) учёт. Три человеческих гейта (курация,
деньги, редактура) он НЕ трогает — см. _FORBIDDEN в докстринге run_sweep.
"""
from datetime import datetime, timezone, timedelta

STALE_MIN = 15   # «running»-строка старше этого — крашнутый воркер, замок протух


def _acquire_lock(trigger: str) -> int | None:
    """Single-flight: вставить running-строку, если нет свежей незавершённой. Вернуть id|None.

    ponytail: check-then-insert; на Postgres окно гонки ~мс — при тике раз в 5 мин и редком
    ручном свипе это неопасно. Упрётся — заменить на pg_advisory_lock (но SQLite-тесты его
    не умеют, потому не сейчас). STALE_MIN перекрывает зависший running крашнутого воркера.
    """
    from sqlalchemy import select
    from app.db import SessionLocal
    from app.models.autonomy import AutonomyRun

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=STALE_MIN)
    with SessionLocal() as db:
        fresh = db.execute(
            select(AutonomyRun.id).where(
                AutonomyRun.status == "running", AutonomyRun.started_at > cutoff)
        ).first()
        if fresh is not None:
            return None                              # замок держит свежий running
        run = AutonomyRun(status="running", trigger=trigger,
                          started_at=datetime.now(timezone.utc), counts={}, errors=[])
        db.add(run)
        db.commit()
        db.refresh(run)
        return run.id


def _finish_run(run_id: int, status: str, counts: dict, errors: list) -> None:
    from app.db import SessionLocal
    from app.models.autonomy import AutonomyRun

    with SessionLocal() as db:
        r = db.get(AutonomyRun, run_id)
        if r is None:
            return
        r.status = status
        r.finished_at = datetime.now(timezone.utc)
        r.counts = counts
        r.errors = errors
        db.commit()


def last_finished_sweep_at():
    """Максимум finished_at завершённых прогонов (для throttle шедулера) или None."""
    from sqlalchemy import select, func
    from app.db import SessionLocal
    from app.models.autonomy import AutonomyRun

    with SessionLocal() as db:
        result = db.scalar(select(func.max(AutonomyRun.finished_at)))
        if result is None:
            return None
        # Ensure tz-aware datetime (SQLAlchemy may return naive from func.max)
        if result.tzinfo is None:
            result = result.replace(tzinfo=timezone.utc)
        return result
