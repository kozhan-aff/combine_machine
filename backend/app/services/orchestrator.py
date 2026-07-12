"""M-оркестратор автономии. Двигает конвейер по включённым «авто»-стадиям до гейтов.

Тонкий диспетчер: НИКАКОЙ новой бизнес-логики — только (1) запрос подходящих сущностей,
(2) вызов существующего безопасного сервиса, (3) учёт. Три человеческих гейта (курация,
деньги, редактура) он НЕ трогает — см. _FORBIDDEN в докстринге run_sweep.
"""
from datetime import datetime, timezone, timedelta

STALE_MIN = 15   # «running»-строка старше этого — крашнутый воркер, замок протух


def _acquire_lock(trigger: str) -> int | None:
    """Single-flight: атомарно вставить running-строку, если нет свежей незавершённой.

    Один INSERT..SELECT..WHERE NOT EXISTS — окно гонки сужено до одной SQL-команды; остаточная
    гонка READ COMMITTED (~мс) остаётся, но последствия ограничены капами и STALE_MIN. При >1
    воркере добавить partial unique index или ON CONFLICT. Возвращает id новой строки или None
    (замок занят).
    """
    from sqlalchemy import select, exists, insert, literal
    from app.db import SessionLocal
    from app.models.autonomy import AutonomyRun

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=STALE_MIN)
    fresh = select(AutonomyRun.id).where(
        AutonomyRun.status == "running", AutonomyRun.started_at > cutoff)
    src = select(
        literal(now, AutonomyRun.started_at.type),
        literal(trigger, AutonomyRun.trigger.type),
        literal("running", AutonomyRun.status.type),
        literal({}, AutonomyRun.counts.type),
        literal([], AutonomyRun.errors.type),
    ).where(~exists(fresh))
    stmt = insert(AutonomyRun).from_select(
        ["started_at", "trigger", "status", "counts", "errors"], src
    ).returning(AutonomyRun.id)
    with SessionLocal() as db:
        run_id = db.execute(stmt).scalar_one_or_none()
        db.commit()
        return run_id


def _finish_run(run_id: int, status: str, counts: dict, errors: list) -> None:
    from app.db import SessionLocal
    from app.models.autonomy import AutonomyRun

    with SessionLocal() as db:
        r = db.get(AutonomyRun, run_id)
        if r is None:
            return
        r.status = status
        r.finished_at = datetime.now(timezone.utc)
        r.counts = counts
        r.errors = errors
        db.commit()


def last_finished_sweep_at() -> datetime | None:
    """Максимум finished_at завершённых прогонов (для throttle шедулера) или None."""
    from sqlalchemy import select, func
    from app.db import SessionLocal
    from app.models.autonomy import AutonomyRun

    with SessionLocal() as db:
        result = db.scalar(select(func.max(AutonomyRun.finished_at)))
        if result is None:
            return None
        # Ensure tz-aware datetime (SQLAlchemy may return naive from func.max)
        if result.tzinfo is None:
            result = result.replace(tzinfo=timezone.utc)
        return result


# --- стадии: каждая = запрос кандидатов + вызов существующего сервиса + учёт ---------
# handler(cap) -> (сделано:int, ошибки:list[str]). cap=None только у discovery.

def _stage_discovery(cap):
    from app.services import discovery
    return discovery.run_discovery(), []


def _stage_score(cap):
    from app.services import scoring
    return scoring.score_pending(limit=cap), []


def _stage_queue(cap):
    """approved-домены (у них по определению нет открытого заказа) -> create_order, до капа."""
    from sqlalchemy import select
    from app.db import SessionLocal
    from app.models.domain import Domain
    from app.services import acquisition

    done, errs = 0, []
    with SessionLocal() as db:
        ids = [r[0] for r in db.execute(
            select(Domain.id).where(Domain.status == "approved").order_by(Domain.id).limit(cap)).all()]
    for did in ids:
        try:
            acquisition.create_order(did)      # деньги НЕ тратит — только заявка pending_confirm
            done += 1
        except Exception as e:  # noqa: BLE001
            errs.append(f"domain#{did}: {type(e).__name__}: {e}")
    return done, errs


def _stage_provision(cap):
    """Две под-операции под общим капом: (а) purchased без сайта -> create_site_for;
    (б) сайт в provisioning -> provision (идемпотентен, awaiting_ns = норм, повторим)."""
    from sqlalchemy import select
    from app.db import SessionLocal
    from app.models.domain import Domain
    from app.models.site import Site
    from app.services import provisioning

    done, errs = 0, []
    with SessionLocal() as db:
        purchased = [r[0] for r in db.execute(
            select(Domain.id).where(Domain.status == "purchased",
                                    ~Domain.id.in_(select(Site.domain_id)))
            .order_by(Domain.id).limit(cap)).all()]
        prov_ids = [r[0] for r in db.execute(
            select(Site.id).where(Site.status == "provisioning").order_by(Site.id).limit(cap)).all()]
    for did in purchased:
        if done >= cap:
            break
        try:
            provisioning.create_site_for(did)
            done += 1
        except Exception as e:  # noqa: BLE001
            errs.append(f"domain#{did}: {type(e).__name__}: {e}")
    for sid in prov_ids:
        if done >= cap:
            break
        try:
            provisioning.provision(sid)
            done += 1
        except Exception as e:  # noqa: BLE001
            errs.append(f"site#{sid}: {type(e).__name__}: {e}")
    return done, errs


def _stage_generate(cap):
    """Сайты status=content без страниц -> generate_site(use_competitor=True)."""
    from sqlalchemy import select
    from app.db import SessionLocal
    from app.models.site import Site, Page
    from app.services import content

    done, errs = 0, []
    with SessionLocal() as db:
        ids = [r[0] for r in db.execute(
            select(Site.id).where(Site.status == "content",
                                  ~Site.id.in_(select(Page.site_id)))
            .order_by(Site.id).limit(cap)).all()]
    for sid in ids:
        try:
            content.generate_site(sid, use_competitor=True)
            done += 1
        except Exception as e:  # noqa: BLE001
            errs.append(f"site#{sid}: {type(e).__name__}: {e}")
    return done, errs


def _stage_publish(cap):
    """Сайты с ≥1 edited-страницей -> publish_site (публикует все edited; гейт держится в сервисе)."""
    from sqlalchemy import select
    from app.db import SessionLocal
    from app.models.site import Site, Page
    from app.services import publish

    done, errs = 0, []
    with SessionLocal() as db:
        ids = [r[0] for r in db.execute(
            select(Site.id).where(Site.id.in_(
                select(Page.site_id).where(Page.status == "edited")))
            .order_by(Site.id).limit(cap)).all()]
    for sid in ids:
        try:
            publish.publish_site(sid)
            done += 1
        except Exception as e:  # noqa: BLE001
            errs.append(f"site#{sid}: {type(e).__name__}: {e}")
    return done, errs


def _stage_check_index(cap):
    """Сайты с published-страницами -> check_index (site: через SearXNG)."""
    from sqlalchemy import select
    from app.db import SessionLocal
    from app.models.site import Site, Page
    from app.services import publish

    done, errs = 0, []
    with SessionLocal() as db:
        ids = [r[0] for r in db.execute(
            select(Site.id).where(Site.id.in_(
                select(Page.site_id).where(Page.status == "published")))
            .order_by(Site.id).limit(cap)).all()]
    for sid in ids:
        try:
            publish.check_index(sid)
            done += 1
        except Exception as e:  # noqa: BLE001
            errs.append(f"site#{sid}: {type(e).__name__}: {e}")
    return done, errs


# порядок конвейера — единственный источник истины оркестратора
STAGES = [
    ("discovery", "auto_discovery", None, _stage_discovery),
    ("score", "auto_score", "cap_score", _stage_score),
    ("queue", "auto_queue", "cap_queue", _stage_queue),
    ("provision", "auto_provision", "cap_provision", _stage_provision),
    ("generate", "auto_generate", "cap_generate", _stage_generate),
    ("publish", "auto_publish", "cap_publish", _stage_publish),
    ("check_index", "auto_check_index", "cap_check_index", _stage_check_index),
]

STAGE_RU = {"discovery": "поиск", "score": "скоринг", "queue": "очередь",
            "provision": "провижн", "generate": "контент", "publish": "публикация",
            "check_index": "индексация"}


def run_sweep(trigger: str = "cron", respect_master: bool = True) -> dict:
    """Прогнать включённые авто-стадии до гейтов. respect_master=False у ручного запуска.

    ЖЁСТКО: зовёт ТОЛЬКО безопасные сервисы из STAGES. НИКОГДА — confirm_order/
    execute_confirmed_order/mark_caught (деньги) и mark_edited (редактура): эти три гейта
    двигает только человек через роуты панели. Ошибка одной сущности не топит стадию/свип.

    Прогресс пишет сам (jobs.track) — именно поэтому свип из воркера теперь виден Пульту.
    Выключенные тумблером стадии показываем как skip, а не прячем: «стадия отключена» и
    «стадия сломалась» — разные вещи, и оператор обязан их различать.
    """
    from app.services import jobs
    from app.services.autonomy import get_autonomy

    cfg = get_autonomy()
    if respect_master and not cfg["autopilot_on"]:
        return {"skipped": "autopilot_off"}
    run_id = _acquire_lock(trigger)
    if run_id is None:
        return {"skipped": "already_running"}

    enabled = [s for s in STAGES if cfg[s[1]]]
    stages = [{"key": k, "label": STAGE_RU[k],
               "state": "pending" if cfg[flag] else "skip"} for k, flag, _, _ in STAGES]
    total = len(enabled)
    counts, errors, status = {}, [], "done"
    try:
        with jobs.track("sweep", trigger="auto" if trigger == "cron" else "manual",
                        stages=stages):
            for i, (key, _flag, cap_attr, handler) in enumerate(enabled):
                jobs.report("sweep", done=i, total=total, stage=key,
                            current=STAGE_RU[key])
                cap = cfg[cap_attr] if cap_attr else None
                try:
                    n, errs = handler(cap)
                    counts[key] = n
                    errors += [f"{key}: {e}" for e in errs]
                except jobs.AlreadyRunning:
                    # ЗАМОК СРАБОТАЛ ШТАТНО, а не сломался: оператор прямо сейчас гоняет свой
                    # score/discovery, и второй прогон поверх — ровно то, что мы запрещали
                    # (двое жгут квоту A-Parser). Пропустить стадию и сказать об этом честно;
                    # красить весь свип в failed = кричать волком на собственную защиту.
                    errors.append(f"{key}: пропущена — занята ручным прогоном")
                except Exception as e:  # noqa: BLE001 — стадия целиком упала (не одна сущность)
                    errors.append(f"{key}: {type(e).__name__}: {e}")
                    status = "failed"
            jobs.report("sweep", done=total, total=total, current="",
                        message=f"стадий пройдено: {total}" + (f" · ошибок: {len(errors)}" if errors else ""))
    except jobs.AlreadyRunning:
        # а ЭТОТ AlreadyRunning — от самого track("sweep"): свип уже идёт в другом процессе.
        _finish_run(run_id, "done", {}, ["sweep: реестр занят другим прогоном"])
        return {"skipped": "already_running"}
    _finish_run(run_id, status, counts, errors)
    return {"run_id": run_id, "status": status, "counts": counts, "errors": errors}
