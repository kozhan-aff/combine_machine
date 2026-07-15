"""Свип честно считает и не голодает (аудит F19).

Три независимых бага в одном месте (`orchestrator.py`):
  А. `_stage_provision` выбирал `Site.status=='provisioning' ORDER BY id LIMIT cap` — сайты,
     застрявшие на `awaiting_ns` (ждут смены NS у регистратора — человек, не машина), навсегда
     занимают первые `cap` мест по id. Сайты с бОльшим id не получают ни одного шанса, пока
     первые не задеплоятся сами (а они не задеплоятся — там ждёт человек).
  Б. `_stage_generate` выбирал сайты «без единой страницы» — если `generate_site()` создал
     часть страниц (например LLM вернул пустое тело для одной из трёх спек), сайт застревал
     с недостающими страницами НАВСЕГДА: селектор больше никогда его не находил.
  В. Итоговый `status` свипа был только `done`/`failed`/`cancelled` — падение ОТДЕЛЬНЫХ
     сущностей внутри стадии (без падения самой стадии) выглядело неотличимо от «всё прошло
     идеально».
"""
import app.db as db
from app.models.domain import Domain
from app.models.site import Page, Site
from app.services import autonomy
from app.services import orchestrator as orch


def _purchased(name: str) -> int:
    with db.SessionLocal() as s:
        d = Domain(domain=name, source="backorder", status="purchased")
        s.add(d)
        s.commit()
        s.refresh(d)
        return d.id


def _site_provisioning(name: str) -> int:
    did = _purchased(name)
    with db.SessionLocal() as s:
        site = Site(domain_id=did, status="provisioning")
        s.add(site)
        s.commit()
        s.refresh(site)
        return site.id


def _site_content(name: str) -> int:
    did = _purchased(name)
    with db.SessionLocal() as s:
        site = Site(domain_id=did, status="content")
        s.add(site)
        s.commit()
        s.refresh(site)
        return site.id


def _add_page(site_id: int, url_path: str, status: str = "draft") -> None:
    with db.SessionLocal() as s:
        s.add(Page(site_id=site_id, url_path=url_path, title="t", status=status, body="<p>x</p>"))
        s.commit()


def _status(site_id: int) -> str:
    with db.SessionLocal() as s:
        return s.get(Site, site_id).status


def _page_count(site_id: int) -> int:
    with db.SessionLocal() as s:
        from sqlalchemy import func, select
        return s.scalar(select(func.count()).select_from(Page).where(Page.site_id == site_id))


# ── А + В: провижн честно считает и не голодает ────────────────────────────

def test_awaiting_ns_is_not_counted_as_succeeded(monkeypatch):
    """ПАДАЛО до фикса: `awaiting_ns` шёл в `done += 1` — «зона ждёт NS у регистратора»
    выглядело как настоящий успех провижна."""
    sid = _site_provisioning("wait.ru")
    monkeypatch.setattr(
        "app.services.provisioning.provision",
        lambda s: {"status": "awaiting_ns", "domain": "wait.ru", "name_servers": ["a", "b"]})

    done, errs, extra = orch._stage_provision(10)

    assert done == 0                                   # НЕ успех
    assert errs == []                                   # и не ошибка — просто ждёт человека
    assert extra == {"provision_awaiting": 1}            # но оператор об этом СЛЫШИТ
    assert _status(sid) == "provisioning"                # статус сайта не сдвинулся


def test_provision_error_status_is_not_counted_as_succeeded(monkeypatch):
    """ПАДАЛО до фикса: `{"status": "error", ...}` (напр. VPS_ORIGIN_IP не задан) тоже шёл
    в `done += 1` — реальный отказ выглядел как успех."""
    sid = _site_provisioning("noip.ru")
    monkeypatch.setattr(
        "app.services.provisioning.provision",
        lambda s: {"status": "error", "domain": "noip.ru", "error": "VPS_ORIGIN_IP не задан"})

    done, errs, extra = orch._stage_provision(10)

    assert done == 0
    assert len(errs) == 1 and "VPS_ORIGIN_IP" in errs[0]
    assert extra == {}
    assert _status(sid) == "provisioning"


def test_provision_fairness_awaiting_site_does_not_starve_a_later_site(monkeypatch):
    """ПАДАЛО до фикса: запрос был `ORDER BY id LIMIT cap`. При cap=1 в выборку попадал
    ТОЛЬКО первый по id сайт — если он вечно `awaiting_ns`, второй сайт не получал ни единого
    шанса НИКОГДА, сколько бы свипов ни прогонялось (тот же класс бага, что и
    `test_stage_queue_dirt_does_not_starve_the_cap` в test_transitions.py, только для провижна).
    """
    stuck = _site_provisioning("stuck.ru")      # меньший id — вечно awaiting_ns
    _site_provisioning("fresh.ru")              # больший id — реально может задеплоиться

    def fake_provision(site_id):
        if site_id == stuck:
            return {"status": "awaiting_ns", "domain": "stuck.ru", "name_servers": []}
        return {"status": "provisioned", "domain": "fresh.ru", "site_id": site_id}

    monkeypatch.setattr("app.services.provisioning.provision", fake_provision)

    done, errs, extra = orch._stage_provision(1)   # кап=1 — старый код даже не увидел бы fresh

    assert done == 1                                # fresh реально обработан В ЭТОМ ЖЕ свипе
    assert errs == []
    assert extra == {"provision_awaiting": 1}       # stuck честно посчитан как «ждёт», не потерян


def test_sweep_status_is_completed_with_errors_when_an_entity_fails(monkeypatch):
    """ПАДАЛО до фикса: одна упавшая сущность внутри стадии (стадия не падает целиком)
    оставляла `status == 'done'` — неотличимо от идеального прогона."""
    did = _purchased("boom.ru")   # purchased без сайта -> create_site_for попытается его создать

    def boom(domain_id):
        raise RuntimeError("aaPanel недоступен")

    monkeypatch.setattr("app.services.provisioning.create_site_for", boom)
    autonomy.update_autonomy(autopilot_on=True, auto_provision=True, cap_provision=10)

    out = orch.run_sweep(trigger="cron")

    assert out["status"] == "completed_with_errors"     # НЕ "done" — сущность упала
    assert out["errors"] and f"domain#{did}" in out["errors"][0] and "aaPanel недоступен" in out["errors"][0]
    assert out["counts"]["provision"] == 0               # никакой лжи об успехе


def test_sweep_status_stays_done_when_nothing_fails():
    """Контроль: при чистом прогоне статус остаётся 'done' — новая ветка не красит всё подряд."""
    autonomy.update_autonomy(autopilot_on=True, auto_provision=False, auto_discovery=False,
                             auto_score=False, auto_queue=False, auto_generate=False,
                             auto_publish=False, auto_check_index=False)
    out = orch.run_sweep(trigger="cron")
    assert out["status"] == "done" and out["errors"] == []


# ── Б: генерация дозаполняет недостающие страницы, а не бросает сайт навсегда ──

def test_generate_stage_reselects_site_with_partial_pages(monkeypatch):
    """ПАДАЛО до фикса: селектор был «нет ни одной страницы» — сайт с 1 из 3 страниц
    больше никогда не выбирался стадией `generate`."""
    sid = _site_content("partial.ru")
    _add_page(sid, "/")                       # 1 из 3 (scaffold даёт "/", "/vs", "/setup")
    assert _page_count(sid) == 1

    seen = []
    monkeypatch.setattr(
        "app.services.content.generate_site",
        lambda site_id, use_competitor=False: seen.append(site_id) or 2)

    done, errs = orch._stage_generate(10)

    assert seen == [sid]                       # сайт С ЧАСТЬЮ страниц ПОПАЛ в выборку
    assert done == 1 and errs == []


def test_generate_stage_skips_a_fully_generated_site(monkeypatch):
    """Контроль: сайт со ВСЕМИ ожидаемыми страницами больше не выбирается (не жжём LLM зря)."""
    sid = _site_content("full.ru")
    for path in ("/", "/vs", "/setup"):
        _add_page(sid, path)
    assert _page_count(sid) == 3

    seen = []
    monkeypatch.setattr(
        "app.services.content.generate_site",
        lambda site_id, use_competitor=False: seen.append(site_id) or 0)

    done, errs = orch._stage_generate(10)

    assert seen == [] and done == 0 and errs == []


def test_generate_site_fills_only_the_missing_page(monkeypatch):
    """Интеграционно (без моков orchestrator, реальный content.generate_site): повторный вызов
    на сайте с 1 из 3 страниц дозаполняет недостающие, а не дублирует уже существующую."""
    from app.integrations.llm import LlmClient
    from app.services import content

    sid = _site_content("fill.ru")
    _add_page(sid, "/")

    monkeypatch.setattr(LlmClient, "complete", lambda self, system, prompt, **kw: "<p>черновик</p>")
    created = content.generate_site(sid)

    assert created == 2                        # только /vs и /setup — "/" не тронута
    assert _page_count(sid) == 3
