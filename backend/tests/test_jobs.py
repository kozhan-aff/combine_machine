"""In-memory реестр прогресса длинных задач: старт, отчёт, защита от двойного старта."""
import time
from app.services import jobs


def test_start_reports_and_finishes():
    jobs._reset()                      # тестовый сброс реестра
    def work():
        for i in range(3):
            jobs.report("score", i + 1, 3, current=f"d{i}.ru")
    jobs.start("score", work)
    for _ in range(50):
        if not jobs.is_running("score"):
            break
        time.sleep(0.02)
    p = jobs.progress("score")
    assert p["done"] == 3 and p["total"] == 3 and p["running"] is False


def test_double_start_rejected():
    jobs._reset()
    def slow():
        jobs.report("discovery", 0, 1)
        time.sleep(0.2)
    assert jobs.start("discovery", slow) is True
    assert jobs.start("discovery", slow) is False    # уже идёт — второй старт отклонён
    for _ in range(50):
        if not jobs.is_running("discovery"):
            break
        time.sleep(0.02)


def test_error_is_captured():
    jobs._reset()
    def boom():
        raise RuntimeError("kaboom")
    jobs.start("score", boom)
    for _ in range(50):
        if not jobs.is_running("score"):
            break
        time.sleep(0.02)
    assert "kaboom" in (jobs.progress("score")["error"] or "")


def test_zero_candidates_discovery_reaches_terminal(monkeypatch):
    """Регрессия Fix 3: пустой фид — run_discovery всё равно репортит терминальную форму
    (0, 0, «нет кандидатов»), а джоб в реестре доходит до running=False без error —
    JS-полоса по контракту jobs.py покажет «готово (0)», а не вечный running."""
    from app.services import discovery
    from app.services.settings import update_settings
    jobs._reset()
    update_settings(sources_enabled={"backorder": True, "cctld": False,
                                     "reg_ru": False, "sweb": False})
    monkeypatch.setattr("app.integrations.backorder.BackorderClient.list_dropping",
                        lambda self, min_links=1: [])          # фид пуст -> ноль кандидатов
    calls = []
    assert discovery.run_discovery(
        on_progress=lambda d, t, c: calls.append((d, t, c))) == 0
    assert calls[-1] == (0, 0, "нет кандидатов")               # терминальный репорт — последним
    assert (0, 1, "собираю: backorder") in calls              # по источнику отчитались во время сбора

    jobs.start("discovery", lambda: discovery.run_discovery(
        on_progress=lambda d, t, c: jobs.report("discovery", d, t, c)))
    for _ in range(50):
        if not jobs.is_running("discovery"):
            break
        time.sleep(0.02)
    p = jobs.progress("discovery")
    assert p["running"] is False and p["error"] is None and p["done"] == 0


def test_score_pending_reports_total_upfront(monkeypatch):
    """Регрессия бага «score: 0/0»: total и текущий домен сообщаются с ПЕРВОГО репорта,
    а не после того, как доскорится первый домен (whois/Wayback идут секунды — раньше всё
    это время бар висел в 0/0). score_domain замокан — тест про тайминг прогресса, не про сеть."""
    from app.services import scoring
    from app.db import SessionLocal
    from app.models.domain import Domain
    with SessionLocal() as db:
        db.add_all([Domain(domain=f"d{i}.ru", source="backorder", status="discovered",
                           referring_domains=i) for i in range(3)])
        db.commit()
    monkeypatch.setattr(scoring, "score_domain",
                        lambda did, clients=None, whois_budget=None: {"domain": "x"})
    monkeypatch.setattr(scoring, "_make_clients", lambda: {})
    calls = []
    n = scoring.score_pending(limit=10, on_progress=lambda d, t, c: calls.append((d, t, c)))
    assert n == 3
    assert calls[0][1] == 3 and calls[0][0] == 0       # total известен сразу, done=0 на старте
    assert calls[0][2]                                  # current непустой — видно, кого скорим
    assert calls[-1] == (3, 3, "")                      # финальный терминальный репорт
