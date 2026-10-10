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
from app.models.offer import Offer
from app.services import autonomy


def _offer_id() -> int:
    with db.SessionLocal() as s:
        o = Offer(brand="NordVPN", affiliate_link="https://ex.com/aff", active=True)
        s.add(o); s.commit()
        return o.id


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


def _dossier(site_id: int) -> None:
    """Строка досье конкурентов: без неё стадия генерации сайт не берёт (спека 2026-10-10 §4.5)."""
    from app.models.research import SiteResearch
    with db.SessionLocal() as s:
        s.add(SiteResearch(site_id=site_id, kind="review", query="q", rank=1, url="https://c.example/1"))
        s.commit()


def test_generate_stage_writes_by_dossier(monkeypatch):
    seen = []
    monkeypatch.setattr("app.services.content.generate_site",
                        lambda site_id, **kw: seen.append((site_id, kw)) or 3)
    with db.SessionLocal() as s:
        d = Domain(domain="g.ru", source="backorder", status="purchased")
        s.add(d); s.commit()
        site = Site(domain_id=d.id, status="content", offer_id=_offer_id()); s.add(site); s.commit()   # content без страниц
        sid = site.id
    _dossier(sid)
    _enable(auto_generate=True)
    out = orch.run_sweep(trigger="cron")
    assert seen == [(sid, {})]          # структуру конкурентов несёт досье — use_competitor не передаётся
    assert out["counts"]["generate"] == 1 and out["errors"] == []


def test_gate_invariants_never_cross_human_gates(monkeypatch):
    """ЖЁСТКО: свип со всеми тумблерами, кроме auto_edit, не двигает scored/draft и не зовёт гейт-функции.
    auto_edit — отдельное решение оператора (критик одобряет сам); без него вычитка не запускается вовсе."""
    for fn in ("confirm_order", "execute_confirmed_order", "mark_caught"):
        monkeypatch.setattr(f"app.services.acquisition.{fn}",
                            lambda *a, **k: (_ for _ in ()).throw(AssertionError(f"gate {fn} called")))
    monkeypatch.setattr("app.services.content.mark_edited",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("editorial gate called")))
    monkeypatch.setattr("app.services.content_critic.edit_site",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("critic called without auto_edit")))
    # offline: сетевые bulk-стадии в no-op, чтобы тумблеры можно было включить все
    monkeypatch.setattr("app.services.discovery.run_discovery", lambda: 0)
    monkeypatch.setattr("app.services.scoring.score_pending", lambda limit=100: 0)
    with db.SessionLocal() as s:
        s.add(Domain(domain="scored.ru", source="backorder", status="scored"))
        d = Domain(domain="site.ru", source="backorder", status="purchased")
        s.add(d); s.commit()
        site = Site(domain_id=d.id, status="content", offer_id=_offer_id()); s.add(site); s.commit()
        s.add(Page(site_id=site.id, url_path="/", status="draft", body="<p>x</p>")); s.commit()
        sid = site.id
    _dossier(sid)
    _enable(auto_discovery=True, auto_score=True, auto_queue=True, auto_provision=True,
            auto_generate=True, auto_publish=True, auto_check_index=True)
    monkeypatch.setattr("app.services.provisioning.create_site_for", lambda did: 0)
    monkeypatch.setattr("app.services.provisioning.provision", lambda sid: {})
    monkeypatch.setattr("app.services.content.generate_site", lambda site_id, **kw: 2)
    monkeypatch.setattr("app.services.publish.publish_site", lambda sid: {})
    monkeypatch.setattr("app.services.publish.check_index", lambda sid, only_due=False: {})
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


def _content_site() -> int:
    """Сайт status=content с оффером, без страниц и без досье."""
    with db.SessionLocal() as s:
        d = Domain(domain="r.ru", source="backorder", status="purchased")
        s.add(d); s.commit()
        site = Site(domain_id=d.id, status="content", offer_id=_offer_id())
        s.add(site); s.commit()
        return site.id


def test_research_stage_sits_before_generate_and_picks_sites_without_fresh_dossier(monkeypatch):
    from app.services import orchestrator, research
    keys = [s[0] for s in orchestrator.STAGES]
    assert keys.index("research") == keys.index("generate") - 1 and keys.index("research") > keys.index("provision")
    assert orchestrator.STAGE_RU["research"] == "досье"
    sid = _content_site()
    calls = []
    monkeypatch.setattr(research, "build_dossier", lambda s, force=False: calls.append(s) or {"status": "done", "rows": 4, "warnings": []})
    done, errs, extra = orchestrator._stage_research(cap=5)
    assert done == 1 and errs == [] and calls == [sid]
    monkeypatch.setattr(research, "build_dossier", lambda s, force=False: {"status": "empty", "rows": 0, "reason": "пусто", "warnings": []})
    done, errs, extra = orchestrator._stage_research(cap=5)
    assert done == 0 and extra.get("research_empty") == 1 and "пусто" in errs[0]
    assert orchestrator.COUNT_RU["research_empty"] == "досье пустое"


def test_stage_research_skips_recently_empty_site(monkeypatch):
    """Пустое досье свип пересобирал каждый час впустую: сутки после сборки сайт не берётся."""
    from datetime import datetime, timedelta, timezone
    from app.services import orchestrator, research
    sid = _content_site()
    calls = []
    monkeypatch.setattr(research, "build_dossier", lambda s, force=False: calls.append(s) or
                        {"status": "empty", "rows": 0, "reason": "пусто", "warnings": []})

    def checked(hours_ago):
        with db.SessionLocal() as s:
            s.get(Site, sid).research_checked_at = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
            s.commit()

    checked(2)
    assert orchestrator._stage_research(cap=5) == (0, [], {}) and calls == []
    checked(25)
    done, errs, extra = orchestrator._stage_research(cap=5)
    assert calls == [sid] and done == 0 and extra == {"research_empty": 1}


def test_stage_research_skips_recently_checked_site_with_stale_dossier(monkeypatch):
    """Протухшее досье, чья пересборка вышла пустой, старые строки сохраняет: пауза обязана держаться и для него."""
    from datetime import datetime, timedelta, timezone
    from app.config import settings
    from app.models.research import SiteResearch
    from app.services import orchestrator, research
    sid = _content_site()
    now = datetime.now(timezone.utc)
    with db.SessionLocal() as s:
        s.add(SiteResearch(site_id=sid, kind="review", query="q", rank=1, url="https://old.com/1",
                           fetched_at=now - timedelta(days=settings.RESEARCH_MAX_AGE_DAYS + 1)))
        s.get(Site, sid).research_checked_at = now - timedelta(hours=2)
        s.commit()
        assert not research.is_fresh(s, sid)
    calls = []
    monkeypatch.setattr(research, "build_dossier", lambda s, force=False: calls.append(s) or
                        {"status": "empty", "rows": 0, "reason": "пусто", "warnings": []})
    assert orchestrator._stage_research(cap=5) == (0, [], {}) and calls == []
    with db.SessionLocal() as s:
        assert [r.url for r in research.dossier(s, sid)] == ["https://old.com/1"]
        s.get(Site, sid).research_checked_at = now - timedelta(hours=25)
        s.commit()
    orchestrator._stage_research(cap=5)
    assert calls == [sid]


def test_research_stage_propagates_already_running(monkeypatch):
    import pytest
    from app.services import jobs, orchestrator, research
    _content_site()
    monkeypatch.setattr(research, "build_dossier",
                        lambda s, force=False: (_ for _ in ()).throw(jobs.AlreadyRunning("research")))
    with pytest.raises(jobs.AlreadyRunning):
        orchestrator._stage_research(cap=5)


def test_autopilot_form_saves_new_toggles(client):
    r = client.post("/autopilot/settings", data={"auto_research": "on", "cap_research": "7", "auto_edit": "on",
                                                 "cap_design": "2"}, follow_redirects=False)
    assert r.status_code == 303
    a = autonomy.get_autonomy()
    assert a["auto_research"] is True and a["cap_research"] == 7 and a["auto_edit"] is True and a["cap_design"] == 2
    assert a["auto_design"] is False


# --- план Б: генерация только по досье, стадия «вычитка» -----------------------

def _draft(site_id: int, path: str = "/", **kw) -> int:
    with db.SessionLocal() as s:
        p = Page(**{**dict(site_id=site_id, url_path=path, status="draft", body="<p>x</p>"), **kw})
        s.add(p); s.commit()
        return p.id


def _site_in(status: str, domain: str) -> int:
    with db.SessionLocal() as s:
        d = Domain(domain=domain, source="backorder", status="purchased")
        s.add(d); s.commit()
        site = Site(domain_id=d.id, status=status, offer_id=_offer_id())
        s.add(site); s.commit()
        return site.id


def _spy_edit(monkeypatch, result=None) -> list:
    from app.services import content_critic
    calls = []
    out = result or {"reviewed": 1, "edited": 1, "rewritten": 0, "failed": 0}
    monkeypatch.setattr(content_critic, "edit_site",
                        lambda site_id, auto_edit=None: calls.append((site_id, auto_edit)) or dict(out))
    return calls


def test_edit_stage_sits_between_generate_and_publish():
    keys = [s[0] for s in orch.STAGES]
    assert keys.index("edit") == keys.index("generate") + 1 == keys.index("publish") - 1
    key, flag, cap, _ = orch.STAGES[keys.index("edit")]
    assert (flag, cap) == ("auto_edit", "cap_generate")
    assert orch.STAGE_RU["edit"] == "вычитка"
    assert orch.COUNT_RU["edit_failed"] == "вычитка: замечания"
    assert orch.COUNT_RU["generate_no_dossier"] == "нет досье"
    assert orch.COUNT_RU["generate_empty"] == "тексты не написаны"


def test_stage_generate_skips_site_without_dossier(monkeypatch):
    sid = _content_site()
    seen = []
    monkeypatch.setattr("app.services.content.generate_site", lambda site_id, **kw: seen.append(site_id) or 3)
    done, errs, extra = orch._stage_generate(5)
    assert seen == [] and done == 0 and extra == {"generate_no_dossier": 1}
    assert errs == [f"site#{sid}: нет досье — генерация пропущена (стадия «досье» или кнопка на карточке сайта)"]
    _dossier(sid)
    done, errs, extra = orch._stage_generate(5)
    assert seen == [sid] and done == 1 and errs == [] and extra == {}


def test_stage_generate_cap_is_not_eaten_by_sites_without_dossier(monkeypatch):
    """Сайт без досье сам из выборки не уходит: при LIMIT в SQL он занимал бы кап каждый свип."""
    stuck = _content_site()
    ready = _site_in("content", "ready.ru")
    later = _site_in("content", "later.ru")
    _dossier(ready); _dossier(later)
    seen = []
    monkeypatch.setattr("app.services.content.generate_site", lambda site_id, **kw: seen.append(site_id) or 3)
    done, errs, extra = orch._stage_generate(1)
    assert seen == [ready] and done == 1 and extra == {"generate_no_dossier": 1} and f"site#{stuck}" in errs[0]


def test_stage_generate_does_not_count_site_left_without_texts(monkeypatch):
    """Писатель вернул 0 (шлюз модели лежит), страниц по-прежнему нет: «сделано» тут было бы враньём —
    причина из итога задачи `generate` уходит в ошибки стадии."""
    from app.services import jobs
    sid = _content_site()
    _dossier(sid)

    def down(site_id, **kw):
        with jobs.track("generate") as run:
            jobs.report(run, message="написано 0 из 3; модель недоступна — прогон остановлен, не начато страниц: 2")
        return 0

    monkeypatch.setattr("app.services.content.generate_site", down)
    done, errs, extra = orch._stage_generate(5)
    assert done == 0 and extra == {"generate_empty": 1}
    assert errs == [f"site#{sid}: тексты не написаны — написано 0 из 3; модель недоступна — прогон остановлен, "
                    "не начато страниц: 2"]


def test_stage_generate_zero_is_fine_when_pages_are_all_there(monkeypatch):
    """0 при полном наборе страниц (их дописал параллельный прогон) — не «тексты не написаны»."""
    sid = _content_site()
    _dossier(sid)
    _draft(sid, "/")

    def racer(site_id, **kw):
        _draft(site_id, "/vs"); _draft(site_id, "/setup")
        return 0

    monkeypatch.setattr("app.services.content.generate_site", racer)
    assert orch._stage_generate(5) == (1, [], {})


def test_stage_edit_takes_unreviewed_drafts_only(monkeypatch):
    from datetime import datetime, timezone
    autonomy.update_autonomy(auto_edit=True)
    fresh = _site_in("content", "fresh.ru"); _draft(fresh)                       # черновик, критик не читал
    seen_before = _site_in("content", "seen.ru")                                # черновик с замечаниями — уже вычитан
    _draft(seen_before, critic_checked_at=datetime.now(timezone.utc), critic_notes={"pass": False, "issues": ["x"]})
    approved = _site_in("content", "appr.ru"); _draft(approved, status="edited")  # одобрена — вычитывать нечего
    early = _site_in("provisioning", "early.ru"); _draft(early)                  # инфраструктура не готова
    live = _site_in("published", "live.ru"); _draft(live)                        # переписанная страница живого сайта
    calls = _spy_edit(monkeypatch)
    done, errs, extra = orch._stage_edit(10)
    assert calls == [(fresh, True), (live, True)]
    assert done == 2 and errs == [] and extra == {}


def test_stage_edit_respects_cap_and_counts_remarks(monkeypatch):
    autonomy.update_autonomy(auto_edit=True)
    sids = [_site_in("content", f"cap{i}.ru") for i in range(3)]
    for sid in sids:
        _draft(sid)
    calls = _spy_edit(monkeypatch, {"reviewed": 2, "edited": 1, "rewritten": 1, "failed": 1})
    done, errs, extra = orch._stage_edit(2)
    assert [c[0] for c in calls] == sids[:2]            # кап — на сайты
    assert done == 2 and errs == [] and extra == {"edit_failed": 2}


def test_stage_edit_entity_error_does_not_sink_the_stage(monkeypatch):
    from app.services import content_critic
    autonomy.update_autonomy(auto_edit=True)
    bad = _site_in("content", "bad.ru"); _draft(bad)
    good = _site_in("content", "good.ru"); _draft(good)

    def edit(site_id, auto_edit=None):
        if site_id == bad:
            raise RuntimeError("шлюз модели не ответил")
        return {"reviewed": 1, "edited": 1, "rewritten": 0, "failed": 0}

    monkeypatch.setattr(content_critic, "edit_site", edit)
    done, errs, extra = orch._stage_edit(5)
    assert done == 1 and errs == [f"site#{bad}: RuntimeError: шлюз модели не ответил"] and extra == {}


def test_stage_edit_propagates_already_running(monkeypatch):
    import pytest
    from app.services import content_critic, jobs
    autonomy.update_autonomy(auto_edit=True)
    _draft(_site_in("content", "busy.ru"))
    monkeypatch.setattr(content_critic, "edit_site",
                        lambda site_id, auto_edit=None: (_ for _ in ()).throw(jobs.AlreadyRunning("edit")))
    with pytest.raises(jobs.AlreadyRunning):
        orch._stage_edit(5)


def test_stage_edit_handler_does_nothing_while_toggle_is_off(monkeypatch):
    """Вторая линия защиты: даже вызванный напрямую, обработчик без тумблера критика не зовёт."""
    autonomy.update_autonomy(auto_edit=False)
    _draft(_site_in("content", "off.ru"))
    calls = _spy_edit(monkeypatch)
    assert orch._stage_edit(5) == (0, [], {}) and calls == []


def test_stage_edit_stops_when_toggle_is_switched_off_midway(monkeypatch):
    """Вычитка сайта — минуты модели; оператор, снявший тумблер посреди стадии, вправе ждать, что следующий
    сайт критик уже не одобрит."""
    from app.services import content_critic
    autonomy.update_autonomy(auto_edit=True)
    first = _site_in("content", "one.ru"); _draft(first)
    _draft(_site_in("content", "two.ru"))
    calls = []

    def edit(site_id, auto_edit=None):
        calls.append(site_id)
        autonomy.update_autonomy(auto_edit=False)
        return {"reviewed": 1, "edited": 1, "rewritten": 0, "failed": 0}

    monkeypatch.setattr(content_critic, "edit_site", edit)
    done, errs, extra = orch._stage_edit(5)
    assert calls == [first] and done == 1


def test_stage_edit_off_when_auto_edit_false(monkeypatch):
    """Полный свип со всеми стадиями, кроме вычитки: критик не зовётся ни разу, черновик остаётся черновиком."""
    monkeypatch.setattr("app.services.discovery.run_discovery", lambda: 0)
    monkeypatch.setattr("app.services.scoring.score_pending", lambda limit=100: 0)
    monkeypatch.setattr("app.services.research.build_dossier", lambda s, force=False: {"status": "done", "rows": 1})
    monkeypatch.setattr("app.services.content.generate_site", lambda site_id, **kw: 2)
    monkeypatch.setattr("app.services.publish.publish_site", lambda sid: {})
    monkeypatch.setattr("app.services.publish.check_index", lambda sid, only_due=False: {})
    sid = _content_site()
    _dossier(sid)
    pid = _draft(sid)
    calls = _spy_edit(monkeypatch)
    _enable(auto_discovery=True, auto_score=True, auto_queue=True, auto_provision=True, auto_research=True,
            auto_generate=True, auto_publish=True, auto_check_index=True, auto_edit=False)
    out = orch.run_sweep(trigger="cron")
    assert calls == [] and "edit" not in out["counts"]
    with db.SessionLocal() as s:
        assert s.get(Page, pid).status == "draft"


def test_sweep_with_auto_edit_runs_critic_after_generate_and_before_publish(monkeypatch):
    order = []
    monkeypatch.setattr("app.services.content.generate_site", lambda site_id, **kw: order.append("generate") or 2)
    monkeypatch.setattr("app.services.content_critic.edit_site",
                        lambda site_id, auto_edit=None: order.append(("edit", auto_edit)) or
                        {"reviewed": 1, "edited": 0, "rewritten": 0, "failed": 1})
    monkeypatch.setattr("app.services.publish.publish_site", lambda sid: order.append("publish") or {})
    sid = _content_site()
    _dossier(sid)
    _draft(sid)
    _draft(_site_in("content", "pub.ru"), status="edited")     # одобренная страница — чтобы публикации было что брать
    _enable(auto_generate=True, auto_edit=True, auto_publish=True)
    out = orch.run_sweep(trigger="cron")
    assert order == ["generate", ("edit", True), "publish"]
    assert out["counts"]["edit"] == 1 and out["counts"]["edit_failed"] == 1


def test_publish_stage_reports_page_failure_in_its_own_words(monkeypatch):
    """Файл записан, но отметка не легла (страницу переписали во время публикации): приставка «не записана»
    была неправдой — причина идёт как есть, с путём."""
    why = "страница изменилась во время публикации — на сайте записана прежняя версия, опубликуй её ещё раз"
    monkeypatch.setattr("app.services.publish.publish_site",
                        lambda sid: {"status": "partial", "pages": ["/vs"], "failed": {"/": why},
                                     "unverified": {}, "warnings": []})
    sid = _site_in("content", "pw.ru")
    _draft(sid, status="edited")
    done, errs = orch._stage_publish(5)
    assert done == 1 and errs == [f"site#{sid}/: {why}"]
