"""F18: стоп-кнопка рисуется ЛЮБОЙ живой задаче, но `jobs.cancelled()` до Задачи 12 читали
только `score` и `recheck` (см. scoring.py). Нажатие «стоп» на discovery ставило флаг, который
никто не проверял — задача доезжала до конца и закрывалась как `done`, будто кнопки не было.

`run_sweep` (orchestrator.py) уже проверяет `jobs.cancelled(run)` между стадиями — это чинила
Задача 11 (F17, «замок джоба не отдаётся живой-но-молчащей задаче»), не эта. Тест на sweep ниже
не регрессия ЭТОЙ задачи: он подтверждает, что фикс Задачи 11 действительно останавливает свип
между стадиями (и не даёт этому поведению незаметно сломаться в будущем без падения теста).

Тот же баг нашёлся и в `cf_sync.py` (Cloudflare P0, Задача 8): `sync_all`/`sync_connection`
не проверяли `jobs.cancelled()` вовсе — кнопка «✕ Отменить» у джоба `cf_sync` была тихим
no-op, вторая connection (и все зоны внутри первой) отрабатывали независимо от нажатия."""
from app.db import SessionLocal
from app.models.cloudflare import CloudflareConnection
from app.models.domain import Domain
from app.services import autonomy, cf_sync, discovery, jobs, orchestrator as orch


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


def test_cf_sync_stops_between_connections_on_cancel(monkeypatch):
    """РЕГРЕССИЯ (Cloudflare P0, Задача 8): до фикса `sync_all` не звал `jobs.cancelled()`
    вовсе — вторая connection синкнулась бы независимо от нажатой кнопки «✕ Отменить», и
    прогон закрылся бы как `done`, а не `cancelled`."""
    monkeypatch.setattr(cf_sync.cf_legacy, "import_legacy_connection", lambda db: None)
    with SessionLocal() as db:
        db.add_all([
            CloudflareConnection(label="first", secret_ref="env:CLOUDFLARE_API_TOKEN",
                                 token_kind="user", status="unverified"),
            CloudflareConnection(label="second", secret_ref="env:CLOUDFLARE_API_TOKEN",
                                 token_kind="user", status="unverified"),
        ])
        db.commit()

    calls = []

    def fake_sync_connection(db, conn, *, run=None):
        calls.append(conn.label)
        jobs.request_cancel("cf_sync")              # человек нажал «стоп» во время первой connection

    monkeypatch.setattr(cf_sync, "sync_connection", fake_sync_connection)

    with SessionLocal() as db, jobs.track("cf_sync") as rid:
        cf_sync.sync_all(db, run=rid)

    assert calls == ["first"]                        # вторую connection даже не начали
    assert jobs.last("cf_sync")["status"] == "cancelled"


def test_cf_sync_stops_between_zones_within_one_connection_on_cancel(monkeypatch):
    """Зон внутри ОДНОЙ connection может быть 51+, а connections обычно 1-2 — без проверки
    МЕЖДУ ЗОНАМИ (не только между connections) кнопка оставалась бы почти-no-op на типичной
    топологии: одна connection дотягивает все свои зоны до конца, кнопка молчит, пока их не
    переберут все."""
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "tok")
    _CUR = [None]

    class _FakeCF:
        def __init__(self):
            self.zone_calls = []
        @classmethod
        def with_token(cls, *a, **k):
            return _CUR[0]
        def verify_token(self, kind, account_id=""):
            return {"status": "active"}
        def list_accounts_paginated(self):
            return [{"id": "accHEX", "name": "Acc"}]
        def list_zones_paginated(self, account_id):
            return [{"id": "z1", "name": "a.ru", "status": "active", "account": {"id": "accHEX"}},
                    {"id": "z2", "name": "b.ru", "status": "active", "account": {"id": "accHEX"}}]
        def find_zone_in_account(self, name, account_id):
            return None
        def list_dns_paginated(self, zone_id, type=None, name=None):
            self.zone_calls.append(zone_id)
            jobs.request_cancel("cf_sync")           # человек нажал «стоп» во время первой зоны
            return []
        def get_zone_setting(self, zone_id, setting_id):
            return {"id": setting_id, "value": "off", "editable": True}
        def list_universal_certificate_packs(self, zone_id):
            return []
        def get_dnssec(self, zone_id):
            return {"status": "disabled"}

    fake = _FakeCF()
    _CUR[0] = fake
    monkeypatch.setattr(cf_sync, "CloudflareClient", _FakeCF)

    with SessionLocal() as db:
        conn = CloudflareConnection(label="only", secret_ref="env:CLOUDFLARE_API_TOKEN",
                                    token_kind="user", status="unverified")
        db.add(conn); db.commit()

    with jobs.track("cf_sync") as rid:
        with SessionLocal() as db:
            c = db.query(CloudflareConnection).filter_by(label="only").one()
            cf_sync.sync_connection(db, c, run=rid)

    assert fake.zone_calls == ["z1"]                 # вторая зона (z2) не тронута
    assert jobs.last("cf_sync")["status"] == "cancelled"
