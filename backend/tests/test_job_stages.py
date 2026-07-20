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
    """Пока скорится домен, в реестре видно, на какой стадии воронки он висит.

    Task 9: score_pending больше не зовёт score_domain по одному — весь батч уходит ОДНИМ
    вызовом _run_waves (здесь батч из 1 домена, так что перехват на этом уровне равносилен
    перехвату на уровне score_domain, как было до Task 9)."""
    _seed(1)
    seen = []

    def fake_run_waves(states, clients, st, whois_budget, ahrefs_budget, run=None):
        jobs.report(run, stage="whois")                  # так репортит _run_waves/_run_concurrent
        seen.append(jobs.progress("score")["stage"])
        return [{"domain": "d0.ru"}]

    monkeypatch.setattr(scoring, "_run_waves", fake_run_waves)
    monkeypatch.setattr(scoring, "_make_clients", lambda: {})
    assert scoring.score_pending(limit=10) == 1
    assert seen == ["whois"]
    p = jobs.progress("score")
    assert p["status"] == "done" and p["total"] == 1
    assert [s["key"] for s in p["stages"]] == [s["key"] for s in scoring.FUNNEL_STAGES]


def test_score_pending_stops_on_cancel(monkeypatch):
    """Стоп-кнопка: прогон завершается cancelled, реестр честно закрывается.

    Task 9: волны обрабатывают домены КОНКУРЕНТНО (workers=12 в _wave_whois) — гарантия
    дореформенного последовательного цикла «ровно один домен успел, остальные 4 даже не
    начаты» здесь физически не воспроизводима (это смена модели конкурентности, не
    регрессия — см. task-9-brief.md). Проверяем то, что осталось настоящим внешним
    контрактом: cancel не проглатывается молча, job закрывается как cancelled с тем
    done/total, что успели отчитать до отмены."""
    _seed(5)

    def fake_run_waves(states, clients, st, whois_budget, ahrefs_budget, run=None):
        jobs.report(run, done=1, total=len(states))       # как реально отчиталась бы волна
        jobs.request_cancel("score")                      # человек нажал «стоп»
        if jobs.cancelled(run):
            raise jobs.Cancelled()
        return [{}]

    monkeypatch.setattr(scoring, "_run_waves", fake_run_waves)
    monkeypatch.setattr(scoring, "_make_clients", lambda: {})
    scoring.score_pending(limit=5)
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
    """Wayback лежал -> домен оценён вслепую; штамповать его нельзя (спека §1.5).

    «Чистый» домен обязан нести wayback_checked=True: отсутствие ошибок чистотой НЕ является
    (аудит F2 — пустой архив ошибки не даёт). Три состояния истории — в test_history_verdict."""
    d = Domain(domain="x.ru", score_breakdown={"errors": ["wayback:ConnectError"]})
    assert "Wayback" in scoring.blind_reason(d)
    clean = Domain(domain="y.ru", wayback_checked=True, prior_flags={},
                   score_breakdown={"errors": []})
    assert scoring.blind_reason(clean) is None
