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
    assert calls == [(0, 0, "нет кандидатов")]                 # терминальный репорт был

    jobs.start("discovery", lambda: discovery.run_discovery(
        on_progress=lambda d, t, c: jobs.report("discovery", d, t, c)))
    for _ in range(50):
        if not jobs.is_running("discovery"):
            break
        time.sleep(0.02)
    p = jobs.progress("discovery")
    assert p["running"] is False and p["error"] is None and p["done"] == 0
