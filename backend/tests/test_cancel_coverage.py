"""F18: стоп-кнопка рисуется ЛЮБОЙ живой задаче, но `jobs.cancelled()` до Задачи 12 читали
только `score` и `recheck` (см. scoring.py). Нажатие «стоп» на discovery ставило флаг, который
никто не проверял — задача доезжала до конца и закрывалась как `done`, будто кнопки не было.

`run_sweep` (orchestrator.py) уже проверяет `jobs.cancelled(run)` между стадиями — это чинила
Задача 11 (F17, «замок джоба не отдаётся живой-но-молчащей задаче»), не эта. Тест на sweep ниже
не регрессия ЭТОЙ задачи: он подтверждает, что фикс Задачи 11 действительно останавливает свип
между стадиями (и не даёт этому поведению незаметно сломаться в будущем без падения теста)."""
from app.db import SessionLocal
from app.models.domain import Domain
from app.services import autonomy, discovery, jobs, orchestrator as orch


def test_discovery_stops_between_sources_on_cancel(monkeypatch):
    """Cancel во время первого источника -> второй источник не опрашивается, статус cancelled.

    РЕГРЕССИЯ F18: до фикса `_collect` не звала `jobs.cancelled()` вовсе — cctld был бы опрошен,
    несмотря на нажатую кнопку, а прогон закрылся бы как `done`.
    """
    from app.services.settings import update_settings
    update_settings(sources_enabled={"backorder": True, "cctld": True,
                                     "reg_ru": False, "sweb": False})

    cctld_calls = []

    def fake_backorder(self, min_links=1, limit=5000):
        jobs.request_cancel("discovery")           # человек нажал «стоп» во время backorder
        return []

    def fake_cctld(self):
        cctld_calls.append(True)                   # не должно случиться
        return []

    monkeypatch.setattr("app.integrations.backorder.BackorderClient.list_dropping", fake_backorder)
    monkeypatch.setattr("app.integrations.cctld.CctldClient.list_dropping", fake_cctld)

    discovery.run_discovery()

    assert cctld_calls == []                        # второй источник даже не начали
    p = jobs.progress("discovery")
    assert p["status"] == "cancelled"
    assert p["stage"] == "backorder"                 # застыли на источнике, где нажали «стоп»


def test_sweep_stops_between_stages_on_cancel(monkeypatch):
    """Cancel во время стадии `score` -> стадия `queue` не запускается, статус cancelled.

    ПОДТВЕРЖДЕНИЕ фикса Задачи 11 (F17), не новая регрессия этой задачи: `run_sweep` уже читает
    `jobs.cancelled(run)` между стадиями (см. orchestrator.py, комментарий «шаг 4 F17» у
    `_start_run`). Тест фиксирует это поведение тестом с воспроизводимым шагом — до Задачи 11
    флаг `cancel_requested` не проверялся нигде в свипе, и `queue` отработал бы следом за `score`.
    """
    with SessionLocal() as s:
        s.add(Domain(domain="stop-queue.ru", source="backorder", status="approved"))
        s.commit()

    def fake_score_pending(limit=100):
        jobs.request_cancel("sweep")                # человек нажал «стоп» во время score
        return 0

    monkeypatch.setattr("app.services.scoring.score_pending", fake_score_pending)
    autonomy.update_autonomy(autopilot_on=True, auto_score=True, auto_queue=True, cap_queue=10)

    out = orch.run_sweep(trigger="cron")

    assert out["status"] == "cancelled"
    with SessionLocal() as s:
        d = s.query(Domain).filter_by(domain="stop-queue.ru").one()
        assert d.status == "approved"               # queue-стадия не тронула домен — не дошли до неё
