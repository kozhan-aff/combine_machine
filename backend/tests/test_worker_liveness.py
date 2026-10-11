"""Живость воркера и журнала свипа (F8-11, S7-20): сердцебиение, /diag, реап оборванных AutonomyRun."""
from datetime import datetime, timedelta, timezone

import app.db as db
from app.models.autonomy import AutonomyRun
from app.models.job import JobRun
from app.services import heartbeat, jobs, orchestrator
from app.workers import scheduler


def test_no_heartbeat_means_unknown_then_alive_after_beat():
    assert heartbeat.status() == {"alive": False, "age_sec": None, "note": ""}
    heartbeat.beat(False)
    st = heartbeat.status()
    assert st["alive"] and st["age_sec"] <= 2 and st["note"] == "автопилот выключен"
    heartbeat.beat(True)                       # вторая запись обновляет ту же строку
    with db.SessionLocal() as s:
        assert s.query(JobRun).filter_by(name="worker").count() == 1
    assert heartbeat.status()["note"] == "автопилот включён"


def test_stale_heartbeat_is_dead():
    heartbeat.beat()
    with db.SessionLocal() as s:
        r = s.query(JobRun).filter_by(name="worker").one()
        r.updated_at = datetime.now(timezone.utc) - timedelta(seconds=heartbeat.STALE_SEC + 60)
        s.commit()
    assert heartbeat.status()["alive"] is False
    assert heartbeat._cli() == 1


def test_heartbeat_row_is_invisible_to_job_registry():
    """Строка сердца не 'running': не попадает в live() и не занимает single-flight замок."""
    heartbeat.beat()
    assert jobs.live() == []
    assert jobs.is_running("worker") is False


def test_diag_and_dashboard_show_worker_state(client, monkeypatch):
    from app.services import diag_cache
    monkeypatch.setattr(diag_cache, "get", lambda: ([], None))
    assert "фоновый процесс: нет сигнала" in client.get("/diag").text
    heartbeat.beat(False)
    for url, alive in (("/diag", "фоновый процесс: работает"), ("/", "фоновый процесс: работает")):
        html = client.get(url).text
        assert alive in html and "автопилот выключен" in html, url
    with db.SessionLocal() as s:
        r = s.query(JobRun).filter_by(name="worker").one()
        r.updated_at = datetime.now(timezone.utc) - timedelta(minutes=10)
        s.commit()
    assert "фоновый процесс: не отвечает уже 10 мин" in client.get("/diag").text


def test_scheduler_heartbeat_job_beats_with_autopilot_flag():
    scheduler.heartbeat()
    assert heartbeat.status()["alive"]


def _running_run():
    with db.SessionLocal() as s:
        r = AutonomyRun(trigger="cron", status="running", counts={}, errors=[])
        s.add(r)
        s.commit()
        return r.id


def test_worker_start_reaps_orphan_autonomy_runs():
    rid = _running_run()
    scheduler.reap_on_start()
    with db.SessionLocal() as s:
        r = s.get(AutonomyRun, rid)
        assert r.status == "failed" and r.finished_at is not None
        assert any("оборвался" in e for e in r.errors)


def test_start_reap_leaves_alive_manual_sweep_alone():
    """Ручной свип панели (другой процесс) жив — его журнал воркер при старте не трогает."""
    rid = _running_run()
    with jobs.track("sweep"):
        scheduler.reap_on_start()
    with db.SessionLocal() as s:
        assert s.get(AutonomyRun, rid).status == "running"


def test_new_sweep_closes_corpses_but_not_itself(monkeypatch):
    corpse = _running_run()
    from app.services import autonomy
    cfg = {**autonomy.get_autonomy(), "autopilot_on": True}
    monkeypatch.setattr(autonomy, "get_autonomy", lambda: cfg)
    for flag in [k for k in cfg if k.startswith("auto_")]:
        cfg[flag] = False
    res = orchestrator.run_sweep(trigger="manual")
    with db.SessionLocal() as s:
        assert s.get(AutonomyRun, corpse).status == "failed"
        if res.get("run_id"):
            assert s.get(AutonomyRun, res["run_id"]).status != "failed"
