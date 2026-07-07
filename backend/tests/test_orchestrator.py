"""Оркестратор: single-flight-замок (AutonomyRun) + учёт прогонов."""
from datetime import datetime, timezone, timedelta

import app.db as db
from app.models.autonomy import AutonomyRun
from app.services import orchestrator as orch


def test_acquire_lock_inserts_running_row():
    run_id = orch._acquire_lock("cron")
    assert run_id is not None
    with db.SessionLocal() as s:
        r = s.get(AutonomyRun, run_id)
        assert r.status == "running" and r.trigger == "cron" and r.finished_at is None


def test_acquire_lock_blocked_by_fresh_running():
    first = orch._acquire_lock("cron")
    second = orch._acquire_lock("manual")           # свежий running держит замок
    assert first is not None and second is None


def test_acquire_lock_overrides_stale_running():
    with db.SessionLocal() as s:                    # протухший running (старше STALE_MIN)
        stale = AutonomyRun(status="running", trigger="cron",
                            started_at=datetime.now(timezone.utc) - timedelta(minutes=orch.STALE_MIN + 1))
        s.add(stale); s.commit()
    assert orch._acquire_lock("cron") is not None   # протухший не блокирует


def test_finish_run_records_summary():
    run_id = orch._acquire_lock("manual")
    orch._finish_run(run_id, "done", {"score": 3}, ["queue: boom"])
    with db.SessionLocal() as s:
        r = s.get(AutonomyRun, run_id)
        assert r.status == "done" and r.finished_at is not None
        assert r.counts == {"score": 3} and r.errors == ["queue: boom"]


def test_last_finished_sweep_at_returns_latest():
    assert orch.last_finished_sweep_at() is None     # пусто -> None
    rid = orch._acquire_lock("cron")
    orch._finish_run(rid, "done", {}, [])
    got = orch.last_finished_sweep_at()
    assert got is not None and got.tzinfo is not None
