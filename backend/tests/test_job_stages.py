"""Стадии воронки и стоп-кнопка: чипы показывают, что делается прямо сейчас."""
from app.db import SessionLocal
from app.models.domain import Domain
from app.services import discovery, jobs, scoring


def _seed(n: int) -> None:
    with SessionLocal() as db:
        db.add_all([Domain(domain=f"d{i}.ru", source="backorder", status="discovered",
                           referring_domains=100 - i) for i in range(n)])
        db.commit()


def test_score_pending_reports_funnel_stages(monkeypatch):
    """Пока скорится домен, в реестре видно, на какой стадии воронки он висит."""
    _seed(1)
    seen = []

    def fake_score(did, clients=None, whois_budget=None, ahrefs_budget=None, job=None):
        jobs.report(job, stage="whois")                  # так репортит _funnel
        seen.append(jobs.progress("score")["stage"])
        return {"domain": "d0.ru"}

    monkeypatch.setattr(scoring, "score_domain", fake_score)
    monkeypatch.setattr(scoring, "_make_clients", lambda: {})
    assert scoring.score_pending(limit=10) == 1
    assert seen == ["whois"]
    p = jobs.progress("score")
    assert p["status"] == "done" and p["total"] == 1
    assert [s["key"] for s in p["stages"]] == [s["key"] for s in scoring.FUNNEL_STAGES]


def test_score_pending_stops_on_cancel(monkeypatch):
    """Стоп-кнопка: прогон завершается cancelled, оставшиеся домены не трогаются."""
    _seed(5)
    scored = []

    def fake_score(did, clients=None, whois_budget=None, ahrefs_budget=None, job=None):
        scored.append(did)
        jobs.request_cancel("score")                      # человек нажал «стоп» на первом домене
        return {}

    monkeypatch.setattr(scoring, "score_domain", fake_score)
    monkeypatch.setattr(scoring, "_make_clients", lambda: {})
    scoring.score_pending(limit=5)
    assert len(scored) == 1                              # второй домен уже не начали
    p = jobs.progress("score")
    assert p["status"] == "cancelled" and p["done"] == 1 and p["total"] == 5


def test_discovery_stages_are_sources(monkeypatch):
    """Чипы discovery — включённые источники + дедуп + запись."""
    from app.services.settings import update_settings
    update_settings(sources_enabled={"backorder": True, "cctld": False,
                                     "reg_ru": False, "sweb": False})
    monkeypatch.setattr("app.integrations.backorder.BackorderClient.list_dropping",
                        lambda self, min_links=1: [])
    assert discovery.run_discovery() == 0
    p = jobs.progress("discovery")
    assert p["status"] == "done" and p["error"] is None
    assert [s["key"] for s in p["stages"]] == ["backorder", "dedup", "save"]
    assert p["message"] == "нет кандидатов"


def test_blind_reason_flags_unverified_history():
    """Wayback лежал -> домен оценён вслепую; штамповать его нельзя (спека §1.5)."""
    d = Domain(domain="x.ru", score_breakdown={"errors": ["wayback:ConnectError"]})
    assert "Wayback" in scoring.blind_reason(d)
    clean = Domain(domain="y.ru", score_breakdown={"errors": []})
    assert scoring.blind_reason(clean) is None
