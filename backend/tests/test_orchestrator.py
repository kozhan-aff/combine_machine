"""Оркестратор: журнал свипов + учёт прогонов. ЗАМОК свипа держит реестр задач (jobs), а не
AutonomyRun — его второй, более слабый single-flight снят в Задаче 11 (F17): он судил по
`started_at`, то есть любой свип длиннее 15 минут объявлял сам себя протухшим. Тесты на сам
замок и его лизинг — в test_job_lease.py."""
import app.db as db
from app.models.autonomy import AutonomyRun
from app.services import orchestrator as orch


def test_start_run_opens_journal_row():
    run_id = orch._start_run("cron")
    assert run_id is not None
    with db.SessionLocal() as s:
        r = s.get(AutonomyRun, run_id)
        assert r.status == "running" and r.trigger == "cron" and r.finished_at is None


def test_finish_run_records_summary():
    run_id = orch._start_run("manual")
    orch._finish_run(run_id, "done", {"score": 3}, ["queue: boom"])
    with db.SessionLocal() as s:
        r = s.get(AutonomyRun, run_id)
        assert r.status == "done" and r.finished_at is not None
        assert r.counts == {"score": 3} and r.errors == ["queue: boom"]


def test_last_finished_sweep_at_returns_latest():
    assert orch.last_finished_sweep_at() is None     # пусто -> None
    rid = orch._start_run("cron")
    orch._finish_run(rid, "done", {}, [])
    got = orch.last_finished_sweep_at()
    assert got is not None and got.tzinfo is not None


# --- стадии + run_sweep -----------------------------------------------------
from app.models.domain import Domain, AcquisitionOrder
from app.models.site import Site, Page
from app.services import autonomy


def _enable(**stages):
    """Включить мастер + перечисленные auto_<stage>=True, остальные оставить как есть."""
    autonomy.update_autonomy(autopilot_on=True, **stages)


def test_sweep_skipped_when_autopilot_off():
    autonomy.update_autonomy(autopilot_on=False)
    assert orch.run_sweep(trigger="cron") == {"skipped": "autopilot_off"}


def test_manual_sweep_bypasses_master_but_respects_toggles():
    autonomy.update_autonomy(autopilot_on=False, auto_score=False)
    out = orch.run_sweep(trigger="manual", respect_master=False)   # мастер выкл — но ручной идёт
    assert "run_id" in out and out["counts"] == {}                 # ни одна стадия не включена


def test_queue_stage_moves_approved_to_purchasing_up_to_cap():
    with db.SessionLocal() as s:
        for i in range(3):
            s.add(Domain(domain=f"appr-{i}.ru", source="backorder", status="approved"))
        s.commit()
    autonomy.update_autonomy(cap_queue=2)
    _enable(auto_queue=True)
    out = orch.run_sweep(trigger="cron")
    assert out["counts"]["queue"] == 2                             # ровно до капа
    with db.SessionLocal() as s:
        from sqlalchemy import select, func
        purchasing = s.scalar(select(func.count()).select_from(Domain).where(Domain.status == "purchasing"))
        approved = s.scalar(select(func.count()).select_from(Domain).where(Domain.status == "approved"))
        orders = s.scalar(select(func.count()).select_from(AcquisitionOrder))
        assert purchasing == 2 and approved == 1 and orders == 2


def test_score_stage_passes_cap_as_limit(monkeypatch):
    seen = {}
    monkeypatch.setattr("app.services.scoring.score_pending",
                        lambda limit=100: seen.update(limit=limit) or 4)
    autonomy.update_autonomy(cap_score=7)
    _enable(auto_score=True)
    out = orch.run_sweep(trigger="cron")
    assert seen["limit"] == 7 and out["counts"]["score"] == 4


def test_provision_stage_two_suboperations(monkeypatch):
    calls = []
    monkeypatch.setattr("app.services.provisioning.create_site_for", lambda did: calls.append(("create", did)) or 1)
    monkeypatch.setattr("app.services.provisioning.provision", lambda sid: calls.append(("prov", sid)) or {})
    with db.SessionLocal() as s:
        d = Domain(domain="buy.ru", source="backorder", status="purchased")
        s.add(d); s.commit()
        s.add(Site(domain_id=d.id, status="provisioning")); s.commit()   # уже есть сайт в provisioning
        d2 = Domain(domain="buy2.ru", source="backorder", status="purchased")
        s.add(d2); s.commit()                                            # покупка без сайта
    _enable(auto_provision=True)
    orch.run_sweep(trigger="cron")
    kinds = {c[0] for c in calls}
    assert "create" in kinds and "prov" in kinds                        # обе под-операции сработали


def test_generate_stage_uses_competitor(monkeypatch):
    seen = {}
    monkeypatch.setattr("app.services.content.generate_site",
                        lambda site_id, use_competitor=False: seen.update(sid=site_id, uc=use_competitor) or 3)
    with db.SessionLocal() as s:
        d = Domain(domain="g.ru", source="backorder", status="purchased")
        s.add(d); s.commit()
        s.add(Site(domain_id=d.id, status="content")); s.commit()       # content без страниц
    _enable(auto_generate=True)
    orch.run_sweep(trigger="cron")
    assert seen.get("uc") is True                                       # спек: use_competitor=True


def test_gate_invariants_never_cross_human_gates(monkeypatch):
    """ЖЁСТКО: свип со ВСЕМИ тумблерами не двигает scored/draft и не зовёт гейт-функции."""
    for fn in ("confirm_order", "execute_confirmed_order", "mark_caught"):
        monkeypatch.setattr(f"app.services.acquisition.{fn}",
                            lambda *a, **k: (_ for _ in ()).throw(AssertionError(f"gate {fn} called")))
    monkeypatch.setattr("app.services.content.mark_edited",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("editorial gate called")))
    # offline: сетевые bulk-стадии в no-op, чтобы тумблеры можно было включить все
    monkeypatch.setattr("app.services.discovery.run_discovery", lambda: 0)
    monkeypatch.setattr("app.services.scoring.score_pending", lambda limit=100: 0)
    with db.SessionLocal() as s:
        s.add(Domain(domain="scored.ru", source="backorder", status="scored"))
        d = Domain(domain="site.ru", source="backorder", status="purchased")
        s.add(d); s.commit()
        site = Site(domain_id=d.id, status="content"); s.add(site); s.commit()
        s.add(Page(site_id=site.id, url_path="/", status="draft", body="<p>x</p>")); s.commit()
    _enable(auto_discovery=True, auto_score=True, auto_queue=True, auto_provision=True,
            auto_generate=True, auto_publish=True, auto_check_index=True)
    monkeypatch.setattr("app.services.provisioning.create_site_for", lambda did: 0)
    monkeypatch.setattr("app.services.provisioning.provision", lambda sid: {})
    monkeypatch.setattr("app.services.content.generate_site", lambda site_id, use_competitor=False: 0)
    monkeypatch.setattr("app.services.publish.publish_site", lambda sid: {})
    monkeypatch.setattr("app.services.publish.check_index", lambda sid: {})
    out = orch.run_sweep(trigger="cron")
    # хендлеры ловят Exception — проглоченный AssertionError гейт-заглушки осел бы в errors.
    # Пустые errors + done доказывают: ни одна гейт-функция не была вызвана нигде в свипе.
    assert out["errors"] == [] and out["status"] == "done", out
    with db.SessionLocal() as s:
        from sqlalchemy import select, func
        scored = s.scalar(select(Domain.status).where(Domain.domain == "scored.ru"))
        draft = s.scalar(select(Page.status).where(Page.url_path == "/"))
        purchased_extra = s.scalar(select(func.count()).select_from(Domain).where(Domain.status == "purchased"))
        assert scored == "scored"        # курационный гейт: scored не двинулся
        assert draft == "draft"          # редактурный гейт: draft не стал edited
        assert purchased_extra == 1      # money-байпас: свип НЕ наплодил purchased (только исходный)


def test_single_flight_second_sweep_skipped():
    """Замок свипа — реестровый (jobs), и держит его ЧУЖОЙ ИДУЩИЙ ПРОГОН, а не строка AutonomyRun."""
    from app.services import jobs
    _enable()                            # мастер вкл, стадий нет
    with jobs.track("sweep"):            # «свип уже идёт в другом процессе»
        assert orch.run_sweep(trigger="cron") == {"skipped": "already_running"}


def test_skipped_sweep_leaves_no_trace_in_the_journal():
    """Свипа НЕ БЫЛО — значит и записи о нём быть не должно.

    Раньше отбитый замком свип всё равно писал строку AutonomyRun со статусом `done` и
    `finished_at`. Она (а) висела в журнале /autopilot как состоявшийся прогон, (б) двигала
    last_finished_sweep_at — и шедулер откладывал СЛЕДУЮЩИЙ свип, приняв несостоявшийся за
    только что отработавший. Несделанная работа не имеет права выглядеть сделанной."""
    from sqlalchemy import func, select

    from app.services import jobs
    _enable()
    with jobs.track("sweep"):
        assert orch.run_sweep(trigger="cron") == {"skipped": "already_running"}
    with db.SessionLocal() as s:
        assert s.scalar(select(func.count()).select_from(AutonomyRun)) == 0
    assert orch.last_finished_sweep_at() is None      # throttle шедулера не сдвинут
