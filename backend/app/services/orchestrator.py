"""M-оркестратор автономии. Двигает конвейер по включённым «авто»-стадиям до гейтов.

Тонкий диспетчер: НИКАКОЙ новой бизнес-логики — только (1) запрос подходящих сущностей,
(2) вызов существующего безопасного сервиса, (3) учёт. Три человеческих гейта (курация,
деньги, редактура) он НЕ трогает — см. _FORBIDDEN в докстринге run_sweep.
"""
from datetime import datetime, timezone, timedelta

STALE_MIN = 15   # «running»-строка старше этого — крашнутый воркер, замок протух


def _acquire_lock(trigger: str) -> int | None:
    """Single-flight: атомарно вставить running-строку, если нет свежей незавершённой.

    Один INSERT..SELECT..WHERE NOT EXISTS — окно гонки check-then-insert закрыто на
    уровне БД (два конкурентных вызова из воркер-тика и ручного свипа больше не могут
    оба увидеть «свободно» и вставить по строке): вся проверка и вставка — одна команда
    для БД, а не SELECT и INSERT по отдельности из Python. Работает и на Postgres, и на
    SQLite (RETURNING поддержан с 3.35, тестовый движок ≥ этого). STALE_MIN перекрывает
    зависший running крашнутого воркера. Возвращает id новой строки или None (замок занят).
    """
    from sqlalchemy import select, exists, insert, literal
    from app.db import SessionLocal
    from app.models.autonomy import AutonomyRun

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=STALE_MIN)
    fresh = select(AutonomyRun.id).where(
        AutonomyRun.status == "running", AutonomyRun.started_at > cutoff)
    src = select(
        literal(now, AutonomyRun.started_at.type),
        literal(trigger, AutonomyRun.trigger.type),
        literal("running", AutonomyRun.status.type),
        literal({}, AutonomyRun.counts.type),
        literal([], AutonomyRun.errors.type),
    ).where(~exists(fresh))
    stmt = insert(AutonomyRun).from_select(
        ["started_at", "trigger", "status", "counts", "errors"], src
    ).returning(AutonomyRun.id)
    with SessionLocal() as db:
        run_id = db.execute(stmt).scalar_one_or_none()
        db.commit()
        return run_id


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


def last_finished_sweep_at() -> datetime | None:
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
