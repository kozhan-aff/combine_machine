"""Шедулер-тик: мастер-выкл -> skip; throttle -> skip; иначе -> run_sweep(trigger=cron)."""
from datetime import datetime, timezone, timedelta

from app.workers import scheduler
from app.services import autonomy


def test_tick_skips_when_autopilot_off(monkeypatch):
    called = []
    monkeypatch.setattr("app.services.orchestrator.run_sweep", lambda **k: called.append(k))
    autonomy.update_autonomy(autopilot_on=False)
    scheduler.tick()
    assert called == []


def test_tick_skips_when_throttled(monkeypatch):
    called = []
    monkeypatch.setattr("app.services.orchestrator.run_sweep", lambda **k: called.append(k))
    # последний свип только что закончился -> интервал ещё не прошёл
    monkeypatch.setattr("app.services.orchestrator.last_finished_sweep_at",
                        lambda: datetime.now(timezone.utc))
    autonomy.update_autonomy(autopilot_on=True, sweep_interval_min=60)
    scheduler.tick()
    assert called == []


def test_tick_runs_when_due(monkeypatch):
    called = []
    monkeypatch.setattr("app.services.orchestrator.run_sweep", lambda **k: called.append(k))
    monkeypatch.setattr("app.services.orchestrator.last_finished_sweep_at",
                        lambda: datetime.now(timezone.utc) - timedelta(hours=2))
    autonomy.update_autonomy(autopilot_on=True, sweep_interval_min=60)
    scheduler.tick()
    assert called == [{"trigger": "cron"}]


def test_tick_runs_when_never_swept(monkeypatch):
    called = []
    monkeypatch.setattr("app.services.orchestrator.run_sweep", lambda **k: called.append(k))
    monkeypatch.setattr("app.services.orchestrator.last_finished_sweep_at", lambda: None)
    autonomy.update_autonomy(autopilot_on=True)
    scheduler.tick()
    assert called == [{"trigger": "cron"}]
