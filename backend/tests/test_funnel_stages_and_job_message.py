"""FUNNEL_STAGES (5, не 6 — эхо слито в risk) и API-контракт, питающий волновую
waterfall-строку в jobCard(). НЕ визуальные тесты: клиентский JS/CSS здесь не
исполняется и не проверяется — это отдельно подтверждено живым рендером через
chrome-devtools (см. review Task 10, 2026-07-21)."""
from datetime import datetime, timezone
from fastapi.testclient import TestClient

from app.main import app
from app.db import SessionLocal
from app.models.job import JobRun
from app.services import scoring


def test_task10_funnel_stages_has_5_not_6():
    """Task 10: FUNNEL_STAGES drops 'echo' — now 5 stages, not 6."""
    assert len(scoring.FUNNEL_STAGES) == 5
    keys = [s["key"] for s in scoring.FUNNEL_STAGES]
    assert keys == ["rd", "whois", "risk", "history", "ahrefs"]
    assert "echo" not in keys
    # Verify risk label now includes echo
    risk_stage = next(s for s in scoring.FUNNEL_STAGES if s["key"] == "risk")
    assert "эхо" in risk_stage["label"]


def test_task10_job_card_shows_waterfall_for_running_score():
    """/api/jobs/live отдаёт message/stages для running score-джобы — контракт, на
    котором держится jobCard() (клиентский рендер этим тестом не покрыт)."""
    client = TestClient(app)

    # Seed a running score job with waterfall message
    with SessionLocal() as db:
        stages = [
            {
                "key": s["key"],
                "label": s["label"],
                "state": "done" if i < 2 else ("active" if i == 2 else "pending"),
            }
            for i, s in enumerate(scoring.FUNNEL_STAGES)
        ]

        job = JobRun(
            name="score",
            status="running",
            message="RD: 5510 → 4310 · whois: 4310 → 3800 · risk: идёт, 1200/3800",
            done=2,
            total=5,
            current="example.ru",
            started_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            stages=stages,
        )
        db.add(job)
        db.commit()

    # Verify waterfall message appears in the JSON API
    # (jobCard() is rendered client-side with JSON from /api/jobs/live)
    response = client.get("/api/jobs/live")
    assert response.status_code == 200
    data = response.json()
    jobs = data.get("jobs", [])
    assert len(jobs) > 0

    score_job = next((j for j in jobs if j["name"] == "score"), None)
    assert score_job is not None
    assert score_job["status"] == "running"
    assert score_job["message"] == "RD: 5510 → 4310 · whois: 4310 → 3800 · risk: идёт, 1200/3800"
    assert len(score_job["stages"]) == 5  # Verify 5 stages in the job


def test_task10_stages_have_correct_structure():
    """Task 10: Each stage has key and label for chip rendering."""
    for stage in scoring.FUNNEL_STAGES:
        assert "key" in stage
        assert "label" in stage
        assert isinstance(stage["key"], str)
        assert isinstance(stage["label"], str)
