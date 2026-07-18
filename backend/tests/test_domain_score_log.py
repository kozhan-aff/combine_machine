"""Append-only история решений score_domain() (domain_score_log). Каждый прогон
воронки добавляет НОВУЮ строку, ничего не перезаписывает — это и есть смысл фичи,
см. docs/superpowers/specs/2026-07-18-domain-score-log-design.md."""
from sqlalchemy import select

import app.db as db
from app.models.domain import Domain
from app.models.domain_score_log import DomainScoreLog


def _seed_domain(**kw) -> int:
    with db.SessionLocal() as s:
        d = Domain(domain="scorelog.ru", source="backorder", status="discovered", **kw)
        s.add(d); s.commit(); s.refresh(d)
        return d.id


def test_score_domain_logs_rejected_outcome(monkeypatch):
    from app.services import scoring
    monkeypatch.setattr(scoring, "_funnel", lambda d, c, st, sig, *a, **kw: "low_rd")
    monkeypatch.setattr(scoring, "_make_clients", lambda: {})
    did = _seed_domain()
    scoring.score_domain(did)
    with db.SessionLocal() as s:
        rows = s.execute(select(DomainScoreLog).where(
            DomainScoreLog.domain_id == did)).scalars().all()
        assert len(rows) == 1
        assert rows[0].outcome == "rejected"
        assert rows[0].reject_reason == "low_rd"
        assert rows[0].score == 0.0


def test_score_domain_logs_unresolved_outcome(monkeypatch):
    from app.services import scoring

    def _fake_funnel(d, c, st, sig, *a, **kw):
        sig["acquirability_unresolved"] = True
        sig["unresolved_why"] = "waiting"
        return None
    monkeypatch.setattr(scoring, "_funnel", _fake_funnel)
    monkeypatch.setattr(scoring, "_make_clients", lambda: {})
    did = _seed_domain()
    scoring.score_domain(did)
    with db.SessionLocal() as s:
        rows = s.execute(select(DomainScoreLog).where(
            DomainScoreLog.domain_id == did)).scalars().all()
        assert len(rows) == 1
        assert rows[0].outcome == "unresolved"
        assert rows[0].score is None


def test_repeated_scoring_appends_not_overwrites(monkeypatch):
    """РЕГРЕССИЯ — прямая проверка смысла фичи: второй вызов score_domain() на том
    же домене добавляет ВТОРУЮ строку, не перезаписывает первую."""
    from app.services import scoring
    monkeypatch.setattr(scoring, "_funnel", lambda d, c, st, sig, *a, **kw: "low_rd")
    monkeypatch.setattr(scoring, "_make_clients", lambda: {})
    did = _seed_domain()
    scoring.score_domain(did)
    with db.SessionLocal() as s:
        d = s.get(Domain, did)
        d.status = "discovered"          # score_domain() судит только discovered/scored/rejected
        s.commit()
    scoring.score_domain(did)
    with db.SessionLocal() as s:
        rows = s.execute(select(DomainScoreLog).where(
            DomainScoreLog.domain_id == did)).scalars().all()
        assert len(rows) == 2
