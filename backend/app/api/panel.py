"""HTML-панель — пошаговый пульт конвейера (оффер → M1 → выкуп → M3 → M4 → M5).

Server-rendered Jinja, формы POST -> redirect (no-JS friendly). Результат действия
передаётся назад через ?msg=/?err= (без сессий). Длинные прогоны (Discovery/Score/
Recheck/Sweep) уходят в фон через services.jobs — роут отвечает 303 сразу, панель
поллит GET /api/jobs/live; остальные действия синхронны (норм для одного оператора).

Гейты (PLAN §2) живут в сервисах; панель их только отражает:
  - деньги: 'purchased' ставит ЧЕЛОВЕК кнопкой (никакого авто-заказа);
  - редактура: publish берёт только 'edited', draft -> edited делает ЧЕЛОВЕК в редакторе.
"""
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit, parse_qsl, urlencode

from fastapi import APIRouter, Request, Depends, Form
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, func, or_
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_session
from app.models.domain import Domain
from app.models.offer import Offer, SiteOffer
from app.models.site import Site, Page
from app.services import diag_cache

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))
from app.services.labels import status_ru as _status_ru, reject_ru as _reject_ru, lane_ru as _lane_ru
templates.env.filters["status_ru"] = _status_ru
templates.env.filters["reject_ru"] = _reject_ru
templates.env.filters["lane_ru"] = _lane_ru
templates.env.globals["diag_alert"] = diag_cache.alert   # баннер в base.html читает кэш
router = APIRouter()

# ручная курация из шортлиста. 'purchased' = оператор купил домен руками — этот клик
# и ЕСТЬ money-gate (заказ провайдеру отсюда не уходит). См. CLAUDE.md, правило 2.
_MANUAL_STATUSES = {"approved", "rejected", "purchased"}

_JOBS = ("discovery", "score", "recheck", "sweep")   # известные джобы реестра


def _back(url: str, msg: str | None = None, err: str | None = None) -> RedirectResponse:
    """303-редирект назад с flash-текстом в query (?msg= / ?err=)."""
    if err:
        url += ("&" if "?" in url else "?") + "err=" + quote(str(err)[:400])
    elif msg:
        url += ("&" if "?" in url else "?") + "msg=" + quote(msg[:400])
    return RedirectResponse(url, status_code=303)


def _domain_counts(db: Session) -> dict:
    return dict(db.execute(select(Domain.status, func.count()).group_by(Domain.status)).all())


def _page_counts(db: Session, site_id: int | None = None) -> dict:
    stmt = select(Page.status, func.count()).group_by(Page.status)
    if site_id is not None:
        stmt = stmt.where(Page.site_id == site_id)
    return dict(db.execute(stmt).all())


def _sites_overview(db: Session) -> list[dict]:
    """Сайты + имя домена + сводка страниц — для дашборда."""
    out = []
    for s in db.execute(select(Site).order_by(Site.id)).scalars().all():
        d = db.get(Domain, s.domain_id)
        pc = _page_counts(db, s.id)
        indexed = db.scalar(select(func.count()).select_from(Page).where(
            Page.site_id == s.id, Page.index_status == "indexed")) or 0
        out.append({"site": s, "domain": d.domain if d else f"#{s.domain_id}",
                    "pages": pc, "indexed": indexed})
    return out


def _next_steps(db: Session) -> list[dict]:
    """Подсказки «что дальше» — превращают воронку в понятные шаги."""
    dc = _domain_counts(db)
    pc = _page_counts(db)
    offers_active = db.scalar(select(func.count()).select_from(Offer).where(Offer.active.is_(True))) or 0
    steps = []
    if not offers_active:
        steps.append({"href": "/offers", "text": "Добавь оффер — это вход машины: без него контенту не на что ссылаться."})
    if dc.get("discovered"):
        steps.append({"href": "/domains/pool?status=discovered", "text": f"{dc['discovered']} доменов ждут скоринга — запусти ▶ Score (лучшие по RD пойдут первыми)."})
    if dc.get("scored"):
        steps.append({"href": "/domains", "text": f"{dc['scored']} отскорено — просмотри и реши ✓ approve / ✗ reject."})
    if dc.get("approved"):
        steps.append({"href": "/domains", "text": f"{dc['approved']} одобрено — купи домен руками у провайдера, потом отметь 🛒 куплен."})
    purchased_no_site = db.execute(
        select(Domain).where(Domain.status == "purchased")
        .where(~Domain.id.in_(select(Site.domain_id)))).scalars().all()
    if purchased_no_site:
        steps.append({"href": "/domains/pool?status=purchased", "text": f"{len(purchased_no_site)} купленных без сайта — нажми «создать сайт»."})
    for s in db.execute(select(Site).where(Site.status == "provisioning")).scalars().all():
        steps.append({"href": f"/sites/{s.id}", "text": f"Сайт #{s.id}: запусти Provision (Cloudflare + aaPanel)."})
    for s in db.execute(select(Site).where(Site.status == "content")).scalars().all():
        n_pages = db.scalar(select(func.count()).select_from(Page).where(Page.site_id == s.id)) or 0
        if not n_pages:
            steps.append({"href": f"/sites/{s.id}", "text": f"Сайт #{s.id}: сгенерируй черновики (M4, ~1–2 мин)."})
    if pc.get("draft"):
        steps.append({"href": "/", "text": f"{pc['draft']} черновиков ждут редактуры — открой страницу, вычитай, сохрани как edited (гейт)."})
    if pc.get("edited"):
        steps.append({"href": "/", "text": f"{pc['edited']} отредактировано — публикуй сайт (M5)."})
    if pc.get("published"):
        unknown = db.scalar(select(func.count()).select_from(Page).where(
            Page.status == "published", Page.index_status != "indexed")) or 0
        if unknown:
            steps.append({"href": "/", "text": f"{unknown} опубликованных ещё не в индексе — проверяй «индексация» (site:)."})
    if not steps:
        steps.append({"href": "/domains", "text": "Очередь пуста: запусти ↻ Discovery за свежими дропами."})
    return steps


def _pool_counts(db: Session, s: dict) -> dict:
    """Сколько доменов пула проходит каждый гейт при текущих порогах (превью эффекта).

    Правила счёта зеркалят воронку (scoring._funnel), иначе превью врёт:
    T0 режет только ИЗВЕСТНЫЙ RD < порога — NULL (сырой список без RD) проходит.
    """
    from datetime import datetime, timezone, timedelta
    total = db.scalar(select(func.count()).select_from(Domain)) or 0
    rd = db.scalar(select(func.count()).select_from(Domain).where(
        or_(Domain.referring_domains.is_(None),
            Domain.referring_domains >= s["min_referring_domains"]))) or 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=365.25 * s["min_age_years"])
    age = db.scalar(select(func.count()).select_from(Domain).where(
        Domain.whois_created.is_not(None), Domain.whois_created <= cutoff)) or 0
    approve = db.scalar(select(func.count()).select_from(Domain).where(
        Domain.score >= s["approve_at"])) or 0
    manual = db.scalar(select(func.count()).select_from(Domain).where(
        Domain.score >= s["manual_review_at"], Domain.score < s["approve_at"])) or 0
    return {"total": total, "rd": rd, "age": age, "approve": approve, "manual": manual}


def _gates(db: Session) -> dict:
    """Счётчики «ждёт тебя» у трёх человеческих гейтов (для экрана Автопилот + Пульта)."""
    from app.models.domain import AcquisitionOrder
    curate = db.scalar(select(func.count()).select_from(Domain).where(Domain.status == "scored")) or 0
    money = db.scalar(select(func.count()).select_from(AcquisitionOrder).where(
        AcquisitionOrder.status == "pending_confirm", AcquisitionOrder.confirmed_by_human.is_(False))) or 0
    edit = db.scalar(select(func.count()).select_from(Page).where(Page.status == "draft")) or 0
    return {"curate": curate, "money": money, "edit": edit}


# ============================================================================
# ЭКРАНЫ
# ============================================================================
@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_session)):
    from app.services import jobs
    from app.services.autonomy import get_autonomy
    from app.services.orchestrator import last_finished_sweep_at
    dc = _domain_counts(db)
    return templates.TemplateResponse(request, "dashboard.html", {
        "active": "dash",
        "dc": dc, "d_total": sum(dc.values()),
        "pc": _page_counts(db),
        "offers_active": db.scalar(select(func.count()).select_from(Offer).where(Offer.active.is_(True))) or 0,
        "offers_total": db.scalar(select(func.count()).select_from(Offer)) or 0,
        "sites": _sites_overview(db),
        "steps": _next_steps(db),
        "autopilot": get_autonomy(), "gates": _gates(db), "last_sweep": last_finished_sweep_at(),
        "last_runs": {name: jobs.last(name) for name in _JOBS},
    })


_URGENT_DAYS = 3        # «дроп на носу»: окно ловли (DROP_GRACE=2 дня) плюс сутки запаса


def _deadline_utc(d):
    """acquire_deadline, приведённый к aware-UTC. SQLite отдаёт naive, PostgreSQL — aware;
    голое сравнение с now(tz) роняет TypeError. Тот же приём — в scoring.acquirability_verdict."""
    from datetime import timezone
    dl = d.acquire_deadline
    if dl is not None and dl.tzinfo is None:
        dl = dl.replace(tzinfo=timezone.utc)
    return dl


def _expired(d, now) -> bool:
    """Окно дропа ЗАКРЫТО — домен уже упущен (его продлили или перехватили).

    Такой домен доезжает до инбокса и живёт там до перепроверки: для lane='bid' воронка T1
    короткозамыкает лейном и приобретаемость на скоринге не судит вовсе. Держать его наверху
    как «срочный» — значит звать оператора решать судьбу покойника (ревью 2026-07-13)."""
    from app.services.scoring import DROP_GRACE
    dl = _deadline_utc(d)
    return dl is not None and dl < now - DROP_GRACE


def _urgent(d, soon, now) -> bool:
    """Дедлайн дропа на носу. Срочность = БЛИЗКИЙ дедлайн, а не наличие дедлайна: у каждого
    backorder-домена дедлайн есть всегда, и «янтарная полоса у всех» ничего не выделяет.
    Просроченный дедлайн — НЕ срочность: купить уже нельзя, торопиться некуда."""
    dl = _deadline_utc(d)
    if dl is None or _expired(d, now):
        return False
    return dl <= soon


@router.get("/domains", response_class=HTMLResponse)
def domains_view(request: Request, db: Session = Depends(get_session)):
    """Инбокс решений: только то, где ждут ТЕБЯ. Полный реестр — /domains/pool."""
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import case
    from app.services import jobs
    from app.services.scoring import blind_reason, stale_donors, DROP_GRACE

    now = datetime.now(timezone.utc)
    # Срочность важнее score: домен, дропающийся завтра, теряется, пока мы любуемся красивым.
    # НО «ближайший дедлайн» ASC — это самая РАННЯЯ дата, то есть УПУЩЕННЫЙ дроп: он встал бы
    # первой строкой инбокса и звал бы решать судьбу покойника. Значит ярус — раньше даты:
    #   0 — окно дропа живое (ловится сейчас или впереди)  ← ради них экран и существует
    #   1 — даты нет (сырьё): купить ещё можно, просто неизвестно когда
    #   2 — окно ЗАКРЫТО: купить уже нельзя, решать нечего
    #
    # ВНИМАНИЕ: пара 1↔2 здесь ПЕРЕВЁРНУТА относительно scoring.score_pending — и это осознанно,
    # не рассинхрон. Там ярус ранжирует ТРАТУ WHOIS (на покойника whois ещё имеет смысл — он его
    # и отбракует; на бездатное сырьё — в последнюю очередь). Здесь ярус ранжирует ВНИМАНИЕ
    # ОПЕРАТОРА, а покойник внимания не стоит вовсе. Не «выравнивай» их.
    tier = case((Domain.acquire_deadline.is_(None), 1),
                (Domain.acquire_deadline < now - DROP_GRACE, 2),   # окно закрыто — купить нельзя
                else_=0)
    order = (tier, Domain.acquire_deadline.asc(), Domain.score.desc().nulls_last())
    inbox = db.execute(select(Domain).where(Domain.status == "scored").order_by(*order)).scalars().all()
    ready = db.execute(select(Domain).where(Domain.status == "approved").order_by(*order)).scalars().all()
    counts = _domain_counts(db)
    soon = now + timedelta(days=_URGENT_DAYS)
    urgent = sum(1 for d in inbox + ready if _urgent(d, soon, now))
    reasons = dict(db.execute(
        select(Domain.reject_reason, func.count()).where(Domain.status == "rejected")
        .group_by(Domain.reject_reason)).all())
    # «отсеял ПОРОГ» и «объективная грязь» — разные природы отказа: первое крутится на
    # /settings, второе не крутится ничем. Считаем здесь, а не в Jinja.
    thr = sum(n for code, n in reasons.items() if code in ("low_rd", "too_young", "low_score"))
    return templates.TemplateResponse(request, "domains.html", {
        "active": "domains",
        # тройка: домен + причина «вслепую» + признак срочности. Все три решения приняты в
        # Python — в Jinja нет ни tz-нормализации, ни доступа к blind_reason.
        "inbox": [(d, blind_reason(d), _urgent(d, soon, now)) for d in inbox],
        # окно дропа закрыто — купить уже нельзя. Домен уехал вниз и не «срочный», но выглядит
        # обычным кандидатом: без метки его можно одобрить (в т.ч. пакетом) и пойти покупать
        # покойника. Множеством, а не флагом в кортеже, — нужно и в «готовы к выкупу».
        "expired_ids": {d.id for d in inbox + ready if _expired(d, now)},
        "ready": ready,
        "counts": counts, "total": sum(counts.values()),
        "gates": _gates(db),
        "offers_active": db.scalar(select(func.count()).select_from(Offer)
                                   .where(Offer.active.is_(True))) or 0,
        "urgent": urgent, "urgent_days": _URGENT_DAYS,
        "stale": stale_donors(db=db),
        "reasons": reasons, "reasons_total": sum(reasons.values()), "reasons_thr": thr,
        "site_by_domain": dict(db.execute(select(Site.domain_id, Site.id)).all()),
        # переживает location.reload() поллера: без этого упавшая discovery/score/recheck
        # молча исчезает из виду после того, как #machine схлопнется на busy->idle (Task 4).
        "last_runs": {name: jobs.last(name) for name in ("discovery", "score", "recheck")},
    })


@router.get("/domains/pool", response_class=HTMLResponse)
def domains_pool_view(request: Request, status: str | None = None, min_score: float | None = None,
                      limit: int = 200, show_all: bool = False, db: Session = Depends(get_session)):
    """Полный реестр — для расследований, а не для ежедневной работы."""
    limit = max(1, min(limit, 1000))            # серверный кап: не тянуть всю таблицу в память
    stmt = select(Domain)
    if status:
        stmt = stmt.where(Domain.status == status)
    elif not show_all:                          # по умолчанию только приобретаемые
        stmt = stmt.where(or_(Domain.reject_reason.is_(None),
                              Domain.reject_reason != "not_acquirable"))
    if min_score is not None:
        stmt = stmt.where(Domain.score >= min_score)
    rows = db.execute(stmt.order_by(Domain.score.desc().nulls_last(),
                                    Domain.referring_domains.desc().nulls_last())
                      .limit(limit)).scalars().all()
    counts = _domain_counts(db)
    return templates.TemplateResponse(request, "pool.html", {
        "active": "domains", "rows": rows, "counts": counts, "total": sum(counts.values()),
        "site_by_domain": dict(db.execute(select(Site.domain_id, Site.id)).all()),
        "f_status": status or "", "f_min_score": "" if min_score is None else min_score,
        "f_limit": limit, "show_all": show_all,
    })


def _bulk_candidates(db: Session, min_score: float):
    """(чистые к одобрению, сколько отсеяно как «вслепую»)."""
    from app.services.scoring import blind_reason
    rows = db.execute(select(Domain).where(Domain.status == "scored",
                                           Domain.score >= min_score)).scalars().all()
    clean = [d for d in rows if not blind_reason(d)]
    return clean, len(rows) - len(clean)


@router.get("/domains/bulk-preview")
def bulk_preview(min_score: float = 0.8, db: Session = Depends(get_session)):
    from fastapi.responses import JSONResponse
    clean, blind = _bulk_candidates(db, max(0.0, min(1.0, min_score)))
    return JSONResponse({"n": len(clean), "blind": blind})


@router.post("/domains/bulk-approve")
def bulk_approve_action(min_score: float = Form(0.8), db: Session = Depends(get_session)):
    """Пакетное одобрение — это КЛИК ЧЕЛОВЕКА, гейт курации на месте (деньги не тратятся:
    approved != куплен). Домены, оценённые вслепую (Wayback лежал), в пакет НЕ попадают —
    иначе пакет стал бы обходом того самого гейта, ради которого он существует."""
    clean, blind = _bulk_candidates(db, max(0.0, min(1.0, min_score)))
    for d in clean:
        d.status = "approved"
    db.commit()
    msg = f"Одобрено пакетом: {len(clean)}"
    if blind:
        msg += f" · пропущено «вслепую»: {blind} — их реши руками"
    return _back("/domains", msg=msg)


@router.get("/diag", response_class=HTMLResponse)
def diag_view(request: Request):
    from app.services import deploy as _deploy
    from app.services.diagnostics import PING_TIMEOUT
    checks = diag_cache.refresh()   # та же цена (живой прогон) + кладём в кэш -> баннер консистентен с /diag
    ok = sum(1 for c in checks if c["status"] == "ok")
    crit_down = [c["label"] for c in checks if c.get("critical") and c["status"] == "fail"]
    return templates.TemplateResponse(request, "diag.html", {
        "active": "diag", "checks": checks, "ok": ok, "total": len(checks),
        "crit_down": crit_down, "timeout": PING_TIMEOUT,
        "repo": settings.GITHUB_REPO, "can_pull": bool(settings.GITHUB_TOKEN),
        "status": _deploy.deploy_status(),
    })


@router.post("/diag/refresh")
def diag_refresh(request: Request):
    """Кнопка «перепроверить» в баннере: синхронный прогон диагностики (≤20с, пинги
    параллельны), редирект назад — оператор остаётся на своём экране, баннер отражает свежий кэш."""
    diag_cache.refresh()
    raw = request.headers.get("referer") or "/"
    p = urlsplit(raw)
    # выбрасываем прежние flash-параметры: иначе старый ?err= подавит «перепроверено», а повторные клики пухнут URL
    q = urlencode([(k, v) for k, v in parse_qsl(p.query) if k not in ("msg", "err")])
    back = urlunsplit((p.scheme, p.netloc, p.path or "/", q, ""))
    return _back(back, msg="Статусы внешних инструментов перепроверены")


@router.get("/settings", response_class=HTMLResponse)
def settings_view(request: Request, db: Session = Depends(get_session)):
    from app.services import settings as st
    s = st.get_settings()
    return templates.TemplateResponse(request, "settings.html", {
        "active": "settings", "s": s, "counts": _pool_counts(db, s)})


@router.get("/autopilot", response_class=HTMLResponse)
def autopilot_view(request: Request, db: Session = Depends(get_session)):
    from app.services.autonomy import get_autonomy
    from app.models.autonomy import AutonomyRun
    runs = db.execute(select(AutonomyRun).order_by(AutonomyRun.id.desc()).limit(10)).scalars().all()
    return templates.TemplateResponse(request, "autopilot.html", {
        "active": "autopilot", "a": get_autonomy(), "gates": _gates(db), "runs": runs})


@router.get("/settings/preview")
def settings_preview(min_rd: int = 1, min_age: float = 3.0, approve: float = 0.7,
                     manual: float = 0.4, db: Session = Depends(get_session)):
    from fastapi.responses import JSONResponse
    clamp01 = lambda v: max(0.0, min(1.0, v))
    s = {"min_referring_domains": max(0, min_rd), "min_age_years": max(0.0, min_age),
         "approve_at": clamp01(approve), "manual_review_at": clamp01(manual)}
    return JSONResponse(_pool_counts(db, s))


@router.get("/offers", response_class=HTMLResponse)
def offers_view(request: Request, db: Session = Depends(get_session)):
    rows = db.execute(select(Offer).order_by(Offer.id)).scalars().all()
    return templates.TemplateResponse(request, "offers.html", {"active": "offers", "rows": rows})


@router.get("/queue", response_class=HTMLResponse)
def queue_view(request: Request):
    from app.services import acquisition
    from app.integrations.backorder import BackorderClient, zone_of
    orders = acquisition.list_orders()

    # Сетка ставок для селектора подтверждения + баланс счёта. Провайдер может лежать —
    # тогда очередь всё равно рендерится, а подтверждать нечем: причина видна в шапке.
    grids, balance, bo_err = {}, None, ""
    for o in orders:                          # зона — до похода в сеть: иначе сбой на первой
        o["zone"] = zone_of(o["domain"])      # заявке оставил бы остальные строки без зоны
    if any(o["status"] == "pending_confirm" for o in orders):
        c = BackorderClient()
        # Сетка и баланс — независимые сбои: упавший баланс не должен писать «подтверждать
        # нечем» над рабочим селектором ставки, и наоборот. Панель не падает ни от одного.
        try:
            for z in {o["zone"] for o in orders if o["zone"]}:   # None — сетки нет и не будет
                grids[z] = c.tariffs(z)
        except Exception as e:  # noqa: BLE001
            bo_err = f"сетка тарифов: {type(e).__name__}: {e}"[:200]
        try:
            balance = c.balance()
        except Exception as e:  # noqa: BLE001 — баланс информационный, подтверждать не мешает
            bo_err = (bo_err + " · " if bo_err else "") + f"баланс: {type(e).__name__}"[:80]
    return templates.TemplateResponse(request, "queue.html", {
        "active": "queue", "orders": orders, "grids": grids,
        "balance": balance, "bo_err": bo_err,
        "n_pending": sum(1 for o in orders if o["status"] == "pending_confirm"),
    })


@router.get("/sites/{site_id}", response_class=HTMLResponse)
def site_view(request: Request, site_id: int, db: Session = Depends(get_session)):
    site = db.get(Site, site_id)
    if site is None:
        return _back("/", err=f"сайт #{site_id} не найден")
    d = db.get(Domain, site.domain_id)
    pages = db.execute(select(Page).where(Page.site_id == site_id).order_by(Page.id)).scalars().all()
    attached = db.execute(
        select(Offer).join(SiteOffer, SiteOffer.offer_id == Offer.id)
        .where(SiteOffer.site_id == site_id)).scalars().all()
    all_offers = db.execute(select(Offer).where(Offer.active.is_(True))).scalars().all()
    pc = _page_counts(db, site_id)
    return templates.TemplateResponse(request, "site.html", {
        "active": "dash",
        "site": site, "domain": d.domain if d else f"#{site.domain_id}",
        "pages": pages, "pc": pc, "attached": attached, "all_offers": all_offers,
    })


@router.get("/pages/{page_id}", response_class=HTMLResponse)
def page_edit_view(request: Request, page_id: int, db: Session = Depends(get_session)):
    page = db.get(Page, page_id)
    if page is None:
        return _back("/", err=f"страница #{page_id} не найдена")
    site = db.get(Site, page.site_id)
    d = db.get(Domain, site.domain_id) if site else None
    return templates.TemplateResponse(request, "page_edit.html", {
        "active": "dash",
        "page": page, "site": site, "domain": d.domain if d else "",
    })


# ============================================================================
# ДЕЙСТВИЯ (POST -> redirect c msg/err)
# ============================================================================
def _back_here(request: Request, msg: str | None = None, err: str | None = None):
    """Вернуть оператора на страницу, с которой он нажал кнопку (запуск есть и на Пульте,
    и на M1). Свои query-параметры чистим: старый ?err= иначе подавит новый ?msg=."""
    raw = request.headers.get("referer") or "/domains"
    p = urlsplit(raw)
    q = urlencode([(k, v) for k, v in parse_qsl(p.query) if k not in ("msg", "err")])
    return _back(urlunsplit(("", "", p.path or "/domains", q, "")), msg=msg, err=err)


@router.post("/run/discovery")
def run_discovery_action(request: Request):
    from app.services import discovery, jobs
    ok = jobs.spawn("discovery", discovery.run_discovery)
    # запущено — баннера НЕТ: прогресс показывает карточка задачи (спека §8)
    return _back_here(request, err=None if ok else "Поиск дропов уже идёт")


@router.post("/run/score")
def run_score_action(request: Request, n: int = Form(5)):
    from app.services import jobs, scoring
    ok = jobs.spawn("score", lambda: scoring.score_pending(limit=n))
    return _back_here(request, err=None if ok else "Проверка уже идёт")


@router.post("/run/recheck")
def run_recheck_action(request: Request, n: int = Form(200)):
    """Перепроверить whois'ом отобранных доноров: не выкупили ли их. Денег не тратит."""
    from app.services import jobs, scoring
    ok = jobs.spawn("recheck", lambda: scoring.recheck_acquirability(limit=n))
    return _back_here(request, err=None if ok else "Перепроверка уже идёт")


@router.post("/run/{job}/cancel")
def run_cancel_action(request: Request, job: str):
    from fastapi import HTTPException
    from app.services import jobs
    if job not in _JOBS:
        raise HTTPException(status_code=404, detail=f"неизвестный джоб: {job}")
    jobs.request_cancel(job)          # сервис увидит флаг между элементами и честно завершится
    return _back_here(request)


@router.get("/api/jobs/live")
def jobs_live():
    """Что машина делает прямо сейчас + итог последнего прогона каждой задачи.
    Один эндпоинт на всю панель: карточки на Пульте/M1 и тонкая полоса в шапке."""
    from fastapi.responses import JSONResponse
    from app.services import jobs
    return JSONResponse(jsonable_encoder({
        "jobs": jobs.live(),
        "last": {name: jobs.last(name) for name in _JOBS},
    }))


@router.post("/domains/{domain_id}/score")
def score_one_action(domain_id: int):
    from app.services import scoring
    try:
        out = scoring.score_domain(domain_id)
        if out.get("unresolved"):
            name = out.get("domain", domain_id)
            # Причину берём КОДОМ из сервиса, а не сниффингом errors: ветка «whois ответил, но
            # ответ не разобрали» (available=None — нестандартный TLD, пустой ответ) исключения
            # не бросает и в errors не пишет, и панель заявляла бы «домен занят» о факте, который
            # никто не устанавливал. Ровно ту ложь и правим.
            return _back("/domains", msg=f"{name}: " + {
                "waiting": "домен ещё занят — дроп не наступил. Воронка вернётся к нему "
                           "в день дропа (без даты — в течение суток)",
                "whois_failed": "whois не ответил (A-Parser) — домен остался в поиске, "
                                "попробуйте позже",
                "whois_unclear": "whois ответил, но ответ не разобран (формат TLD?) — "
                                 "домен остался в поиске, занятость НЕ установлена",
                "taken_undated": "домен занят, но дата его дропа неизвестна — вернёмся к нему "
                                 "по расписанию (раз в сутки), вдруг освободится",
                "budget": "исчерпан бюджет whois на прогон (см. max_whois_per_run в /settings) — "
                          "домен остался в поиске",
            }.get(out.get("why"), "приобретаемость не определена — домен остался в поиске"))
        return _back("/domains", msg=f"скор: {out.get('domain', domain_id)} -> "
                                     f"{out.get('status')} ({out.get('score')})")
    except Exception as e:  # noqa: BLE001
        return _back("/domains", err=f"score #{domain_id}: {e}")


@router.post("/domains/{domain_id}/set-status")
def set_status_action(domain_id: int, status: str = Form(...), db: Session = Depends(get_session)):
    # ручной override: 'purchased' здесь — money-gate человека мимо очереди; оркестратор
    # (services/orchestrator) этот роут НЕ зовёт (двигает только до pending_confirm).
    if status in _MANUAL_STATUSES:      # guard: только ручные переходы курации
        d = db.get(Domain, domain_id)
        if d:
            d.status = status
            db.commit()
    return _back("/domains")


@router.post("/admin/refresh-prices")
def refresh_prices_action():
    from app.services.pricing import refresh_backorder_prices
    n = refresh_backorder_prices()
    return _back("/domains", msg=f"Цены бэкордера обновлены: {n} доменов"
                 if n else "Цена бэкордера недоступна (тариф не прочитан)")


@router.post("/domains/{domain_id}/make-site")
def make_site_action(domain_id: int):
    from app.services import provisioning
    try:
        sid = provisioning.create_site_for(domain_id)
        return _back(f"/sites/{sid}", msg="Сайт создан. Дальше: привяжи оффер и запусти Provision.")
    except Exception as e:  # noqa: BLE001
        return _back("/domains", err=f"создание сайта: {e}")


# --- M2 очередь выкупа (структурный путь: очередь + подтверждение + отправка) ------
@router.post("/domains/{domain_id}/queue")
def queue_add_action(domain_id: int, provider: str = Form("backorder")):
    from app.services import acquisition
    try:
        oid = acquisition.create_order(domain_id, provider)
        return _back("/queue", msg=f"Домен в очереди выкупа (заказ #{oid}). Подтверди — тогда уйдёт провайдеру.")
    except Exception as e:  # noqa: BLE001
        return _back("/domains", err=f"в очередь: {e}")


@router.post("/queue/{order_id}/confirm")
def queue_confirm_action(order_id: int, bid_rub: float = Form(0)):
    from app.services import acquisition
    try:
        r = acquisition.confirm_order(order_id, bid_rub or None)
        bid = r.get("bid_rub")
        return _back("/queue", msg=f"Заказ #{order_id} подтверждён человеком (гейт открыт)"
                                   f"{f', ставка {bid:.0f} ₽' if bid else ''}. Можно отправлять.")
    except Exception as e:  # noqa: BLE001
        return _back("/queue", err=f"подтверждение: {e}")


@router.post("/queue/{order_id}/execute")
def queue_execute_action(order_id: int):
    from app.services import acquisition
    try:
        r = acquisition.execute_confirmed_order(order_id)
        if r.get("error"):
            return _back("/queue", err=r["error"])
        if r.get("status") == "failed":
            return _back("/queue", err=f"заказ #{order_id}: {r.get('error') or 'провайдер отверг заказ'}")
        # paynow=on списывает с баланса: при 0 ₽ заказ создастся, но повиснет «Не оплачен» и
        # домен НЕ будет перехвачен. Сказать это сразу, а не оставлять узнавать через поллинг.
        note = (r.get("result") or {}).get("note") or ""
        return _back("/queue", msg=f"Заказ #{order_id} отправлен провайдеру — статус "
                                   f"{r.get('status')}.{' ' + note if note else ''} "
                                   "Проверь «↻ обновить статусы»: при нулевом балансе заказ "
                                   "повиснет «Не оплачен» и домен не перехватят.")
    except Exception as e:  # noqa: BLE001
        return _back("/queue", err=f"отправка: {e}")


@router.post("/queue/poll")
def queue_poll_action():
    from app.services import acquisition
    try:
        r = acquisition.poll_orders()
        return _back("/queue", msg=f"Сверено с провайдером: наших заказов {r['checked']} · "
                                   f"поймано {r.get('caught', 0)} · не вышло {r.get('failed', 0)} · "
                                   f"в полёте {r.get('pending', 0)}.")
    except Exception as e:  # noqa: BLE001
        return _back("/queue", err=f"опрос статусов: {e}")


@router.post("/queue/{order_id}/caught")
def queue_caught_action(order_id: int):
    from app.services import acquisition
    try:
        acquisition.mark_caught(order_id)
        return _back("/queue", msg=f"Заказ #{order_id}: домен помечен пойманным (purchased) — можно создавать сайт.")
    except Exception as e:  # noqa: BLE001
        return _back("/queue", err=f"поймать: {e}")


@router.post("/queue/{order_id}/cancel")
def queue_cancel_action(order_id: int):
    from app.services import acquisition
    try:
        acquisition.cancel_order(order_id)
        return _back("/queue", msg=f"Заказ #{order_id} снят — домен возвращён в approved.")
    except Exception as e:  # noqa: BLE001
        return _back("/queue", err=f"отмена: {e}")


@router.post("/offers/create")
def offer_create_action(brand: str = Form(...), affiliate_link: str = Form(...),
                        promo_code: str = Form(""), country: str = Form(""),
                        language: str = Form(""), db: Session = Depends(get_session)):
    if not brand.strip() or not affiliate_link.strip():
        return _back("/offers", err="бренд и партнёрская ссылка обязательны")
    o = Offer(brand=brand.strip(), affiliate_link=affiliate_link.strip(),
              promo_code=promo_code.strip() or None, country=country.strip() or None,
              language=language.strip() or None)
    db.add(o)
    db.commit()
    return _back("/offers", msg=f"Оффер «{o.brand}» добавлен")


@router.post("/offers/{offer_id}/toggle")
def offer_toggle_action(offer_id: int, db: Session = Depends(get_session)):
    o = db.get(Offer, offer_id)
    if o:
        o.active = not o.active
        db.commit()
    return _back("/offers")


@router.post("/sites/{site_id}/attach-offer")
def attach_offer_action(site_id: int, offer_id: int = Form(...), db: Session = Depends(get_session)):
    exists = db.execute(select(SiteOffer).where(
        SiteOffer.site_id == site_id, SiteOffer.offer_id == offer_id)).scalar_one_or_none()
    if not exists:
        db.add(SiteOffer(site_id=site_id, offer_id=offer_id))
        db.commit()
    return _back(f"/sites/{site_id}", msg="Оффер привязан")


@router.post("/sites/{site_id}/provision")
def provision_action(site_id: int):
    from app.services import provisioning
    try:
        r = provisioning.provision(site_id)
        if r.get("status") == "awaiting_ns":
            ns = ", ".join(r.get("name_servers") or [])
            return _back(f"/sites/{site_id}",
                         msg=f"Зона создана, ждёт NS. Пропиши у регистратора: {ns} — потом повтори Provision.")
        if r.get("status") == "error":
            return _back(f"/sites/{site_id}", err=r.get("error", "provision error"))
        return _back(f"/sites/{site_id}", msg="Provision готов: DNS proxied + vhost + SSL. Дальше — генерация.")
    except Exception as e:  # noqa: BLE001 — нет кредов CF/aaPanel и т.п.
        return _back(f"/sites/{site_id}", err=f"provision: {e}")


@router.post("/sites/{site_id}/generate")
def generate_action(site_id: int, lang: str = Form("ru")):
    from app.services import content
    try:
        # use_competitor=True: подмешать карту тем от топ-конкурента (A-Parser, best-effort)
        n = content.generate_site(site_id, lang=lang, use_competitor=True)
        return _back(f"/sites/{site_id}",
                     msg=f"Сгенерировано {n} черновиков. Дальше — редактура (гейт: publish берёт только edited).")
    except Exception as e:  # noqa: BLE001
        return _back(f"/sites/{site_id}", err=f"генерация: {e}")


@router.post("/pages/{page_id}/save")
def page_save_action(page_id: int, body: str = Form(""), db: Session = Depends(get_session)):
    from app.services import content
    p = db.get(Page, page_id)
    sid = p.site_id if p else None
    try:
        content.mark_edited(page_id, body)   # ЧЕЛОВЕК прошёл гейт: draft -> edited (+ sanitize)
        return _back(f"/sites/{sid}", msg="Страница сохранена как edited — можно публиковать.")
    except Exception as e:  # noqa: BLE001
        return _back(f"/pages/{page_id}", err=f"сохранение: {e}")


@router.post("/sites/{site_id}/publish")
def publish_action(site_id: int):
    from app.services import publish
    try:
        r = publish.publish_site(site_id)
        if r.get("status") == "no_edited_pages":
            return _back(f"/sites/{site_id}",
                         err="Гейт редактуры: нет страниц в статусе edited — сначала вычитай черновики.")
        return _back(f"/sites/{site_id}", msg=f"Опубликовано: {', '.join(r.get('pages', []))}")
    except Exception as e:  # noqa: BLE001
        return _back(f"/sites/{site_id}", err=f"публикация: {e}")


@router.post("/sites/{site_id}/check-index")
def check_index_action(site_id: int):
    from app.services import publish
    try:
        r = publish.check_index(site_id)
        pages = r.get("pages", {})
        if not pages:
            return _back(f"/sites/{site_id}", msg="Нет опубликованных страниц для проверки.")
        s = ", ".join(f"{k}: {v}" for k, v in pages.items())
        return _back(f"/sites/{site_id}", msg=f"Индексация — {s}")
    except Exception as e:  # noqa: BLE001
        return _back(f"/sites/{site_id}", err=f"индексация: {e}")


# --- self-update: git pull + миграции (панель localhost-only, POST-only) -----
# ponytail: тянем по HTTPS с fine-grained PAT — не монтируем SSH-ключ в контейнер.
# Требует volume `.:/repo` + git в образе (см. docker-compose/Dockerfile).
def _pull_banner(r: dict):
    """Единый баннер из dict deploy.git_pull()/git_force_pull()."""
    if not r.get("ok"):
        return _back("/diag", err=r.get("error", "обновление не удалось"))
    warn = f" ⚠ миграции: {r['alembic_warn']}" if r.get("alembic_warn") else ""
    rebuild = " · нужна пересборка образа: docker compose up -d --build" if r.get("needs_rebuild") else ""
    verb = "Принудительно обновлено" if r.get("forced") else "Обновлено"
    subj = r.get("subject", "")
    if r["old"] == r["new"]:
        return _back("/diag", msg=f"Уже свежая версия: {r['new']} «{subj}»{warn}{rebuild}")
    return _back("/diag", msg=f"{verb}: {r['old']}→{r['new']} «{subj}»{warn}{rebuild}")


@router.post("/admin/pull")
def git_pull_action():
    from app.services import deploy
    return _pull_banner(deploy.git_pull())


@router.post("/admin/force-pull")
def git_force_pull_action():
    from app.services import deploy
    return _pull_banner(deploy.git_force_pull())


@router.post("/admin/check-updates")
def check_updates_action():
    import base64 as _b64
    import os
    import subprocess
    from app.services.version import current_version
    if not settings.GITHUB_TOKEN:
        return _back("/diag", err="GITHUB_TOKEN не задан — нечем проверить удалёнку")
    # тот же паттерн, что и /admin/pull: токен НЕ в argv, а через http.extraheader в env git.
    basic = _b64.b64encode(f"x-access-token:{settings.GITHUB_TOKEN}".encode()).decode()
    git_env = {
        **os.environ,
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
    }
    try:
        r = subprocess.run(["git", "-C", "/repo", "ls-remote",
                            f"https://github.com/{settings.GITHUB_REPO}.git", "main"],
                           capture_output=True, text=True, timeout=20, env=git_env)
        remote = (r.stdout.split() or [""])[0][:7]
        cur = current_version().get("hash", "")
        if r.returncode != 0 or not remote:
            # как в /admin/pull: детали в баннер, но токен никогда не светим
            detail = (r.stderr or "").strip().replace(settings.GITHUB_TOKEN, "***")[:200]
            return _back("/diag", err="не удалось прочитать удалёнку" + (f": {detail}" if detail else ""))
        if not cur:
            # current_version() упал (git в контейнере недоступен) — пустая cur делает
            # remote.startswith(cur) тривиально True для ЛЮБОГО remote: без этой ветки
            # мы бы соврали «актуально», хотя текущую версию не смогли определить вовсе.
            return _back("/diag", err="не удалось определить текущую версию (git в контейнере недоступен)")
        same = remote.startswith(cur) or cur.startswith(remote)
        return _back("/diag", msg=f"Текущая {cur} — {'актуально' if same else 'доступна новее ' + remote}")
    except Exception as e:  # noqa: BLE001
        return _back("/diag", err=f"check-updates: {type(e).__name__}")


@router.post("/settings/save")
def settings_save(min_referring_domains: int = Form(...), min_age_years: float = Form(...),
                  approve_at: float = Form(...), manual_review_at: float = Form(...),
                  max_whois_per_run: int = Form(200), max_ahrefs_per_run: int = Form(50),
                  backorder: str = Form(""), cctld: str = Form(""),
                  reg_ru: str = Form(""), sweb: str = Form(""),
                  w_history_cleanliness: float | None = Form(None),
                  w_rd_proxy: float | None = Form(None), w_age: float | None = Form(None),
                  w_indexed_echo: float | None = Form(None),
                  w_authority: float | None = Form(None)):
    from app.services import settings as st
    # веса — опциональны: форма без них (старый шаблон, curl из скрипта) не должна ОБНУЛЯТЬ
    # шкалу оценки. None -> ключ не передаём, update_settings оставит прежние.
    weights = {k: v for k, v in (("history_cleanliness", w_history_cleanliness),
                                 ("rd_proxy", w_rd_proxy), ("age", w_age),
                                 ("indexed_echo", w_indexed_echo),
                                 ("authority", w_authority)) if v is not None}
    st.update_settings(min_referring_domains=min_referring_domains, min_age_years=min_age_years,
                       approve_at=approve_at, manual_review_at=manual_review_at,
                       max_whois_per_run=max_whois_per_run, max_ahrefs_per_run=max_ahrefs_per_run,
                       sources_enabled={"backorder": bool(backorder), "cctld": bool(cctld),
                                        "reg_ru": bool(reg_ru), "sweb": bool(sweb)},
                       weights=weights or None)
    return _back("/settings", msg="Настройки сохранены")


@router.post("/settings/reset")
def settings_reset():
    from app.services import settings as st
    st.reset_settings()
    return _back("/settings", msg="Настройки сброшены к дефолтам")


@router.post("/autopilot/settings")
def autopilot_settings_save(
        autopilot_on: str = Form(""), sweep_interval_min: int = Form(60),
        auto_discovery: str = Form(""), auto_score: str = Form(""), auto_queue: str = Form(""),
        auto_provision: str = Form(""), auto_generate: str = Form(""), auto_publish: str = Form(""),
        auto_check_index: str = Form(""),
        cap_score: int = Form(20), cap_queue: int = Form(10), cap_provision: int = Form(5),
        cap_generate: int = Form(5), cap_publish: int = Form(5), cap_check_index: int = Form(20)):
    from app.services.autonomy import update_autonomy
    update_autonomy(
        autopilot_on=bool(autopilot_on), sweep_interval_min=sweep_interval_min,
        auto_discovery=bool(auto_discovery), auto_score=bool(auto_score), auto_queue=bool(auto_queue),
        auto_provision=bool(auto_provision), auto_generate=bool(auto_generate),
        auto_publish=bool(auto_publish), auto_check_index=bool(auto_check_index),
        cap_score=cap_score, cap_queue=cap_queue, cap_provision=cap_provision,
        cap_generate=cap_generate, cap_publish=cap_publish, cap_check_index=cap_check_index)
    return _back("/autopilot", msg="Настройки автопилота сохранены")


@router.post("/autopilot/run")
def autopilot_run_action(request: Request):
    from app.services import jobs, orchestrator
    ok = jobs.spawn("sweep", lambda: orchestrator.run_sweep(trigger="manual",
                                                            respect_master=False))
    return _back_here(request, err=None if ok else "Свип уже идёт")
