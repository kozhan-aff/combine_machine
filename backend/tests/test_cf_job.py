"""Задача 5 (Cloudflare P0): cf_sync расширяет job-реестр — имена/capacity/лейблы + роут запуска.

_JOBS/ThreadPoolExecutor/JOB_RU обновлены синхронно (три независимых литерала + размер пула).
Роут запуска (POST /settings/cloudflare/sync) — единственный вызывающий _require_cf_write
(задача 6): гейт был доказан только юнит-тестом напрямую, здесь — реальный HTTP end-to-end.
"""
from app.api import panel
from app.services import jobs


def test_cf_sync_is_known_job():
    assert "cf_sync" in panel._JOBS


def test_cancel_route_accepts_cf_sync(client):
    # неизвестное имя -> 404; cf_sync должно НЕ быть 404 (спавна нет -> «нет задачи», но не 404)
    r = client.post("/run/cf_sync/cancel")
    assert r.status_code != 404


def test_jobs_live_survives_cf_sync_in_registry(client):
    """/api/jobs/live читает jobs.last(name) для КАЖДОГО имени в _JOBS — с пятым именем в
    списке (и ни одного прогона cf_sync ни разу) роут обязан не падать, а отдать last=None."""
    r = client.get("/api/jobs/live")
    assert r.status_code == 200
    assert r.json()["last"]["cf_sync"] is None


def test_dashboard_survives_cf_sync_in_registry(client):
    """Пульт собирает last_runs по _JOBS для баннера «оборвалась после reload» — не должен
    упасть от лишнего (ещё ни разу не запускавшегося) имени в реестре."""
    r = client.get("/")
    assert r.status_code == 200


def test_cf_sync_route_requires_configured_panel_auth(client, monkeypatch):
    """GATE_REQUIREMENT (ревью Задачи 6): реальный POST на роут запуска cf_sync без
    настроенных PANEL_USER/PANEL_PASS -> 403. Через TestClient, не прямой вызов функции —
    это первый end-to-end потребитель _require_cf_write."""
    from app.config import settings
    monkeypatch.setattr(settings, "PANEL_USER", "")
    monkeypatch.setattr(settings, "PANEL_PASS", "")
    r = client.post("/settings/cloudflare/sync", follow_redirects=False)
    assert r.status_code == 403
    assert jobs.is_running("cf_sync") is False   # гейт сработал ДО spawn — джоб не стартовал


def test_cf_sync_route_spawns_job_when_auth_configured(client, monkeypatch):
    """С настроенным auth (и корректным Basic-заголовком — иначе на них же ловит транспортная
    проверка в main.py) роут доходит до spawn и запускает cf_sync в фоне (мутаций CF нет —
    это read-only sync)."""
    from app.config import settings
    monkeypatch.setattr(settings, "PANEL_USER", "u")
    monkeypatch.setattr(settings, "PANEL_PASS", "p")
    monkeypatch.setattr(panel.cf_sync, "sync_all",
                        lambda db, report=None, run=None: {"connections": 0})
    r = client.post("/settings/cloudflare/sync", auth=("u", "p"), follow_redirects=False)
    assert r.status_code == 303
    jobs._drain()
    assert jobs.last("cf_sync") is not None
    assert jobs.last("cf_sync")["status"] == "done"
