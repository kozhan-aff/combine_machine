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

from fastapi import APIRouter, Request, Depends, Form, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_session, SessionLocal
from app.models.cloudflare import (
    CloudflareAccount, CloudflareCertificatePackMirror, CloudflareConnection,
    CloudflareConnectionAccount, CloudflareDnsRecordMirror, CloudflareZoneMirror,
)
from app.models.domain import Domain
from app.models.offer import Offer, SiteOffer
from app.models.site import Site, Page
from app.services import cf_sync, diag_cache

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))
from app.services.labels import (status_ru as _status_ru, reject_ru as _reject_ru,
                                 lane_ru as _lane_ru, index_ru as _index_ru)
templates.env.filters["status_ru"] = _status_ru
templates.env.filters["reject_ru"] = _reject_ru
templates.env.filters["lane_ru"] = _lane_ru
templates.env.filters["index_ru"] = _index_ru
templates.env.globals["diag_alert"] = diag_cache.alert   # баннер в base.html читает кэш
router = APIRouter()

# ручная курация из шортлиста. 'purchased' = оператор купил домен руками — этот клик
# и ЕСТЬ money-gate (заказ провайдеру отсюда не уходит). См. CLAUDE.md, правило 2.
_MANUAL_STATUSES = {"approved", "rejected", "purchased"}

_JOBS = ("discovery", "score", "recheck", "sweep", "cf_sync")   # известные джобы реестра


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
        # Три РАЗНЫХ состояния, и валить их в одно «ещё не в индексе» — врать: «не спросили»,
        # «спросили и не выяснили» и «спросили, в индексе нет» требуют разных действий оператора.
        def _idx(*cond):
            return db.scalar(select(func.count()).select_from(Page).where(
                Page.status == "published", *cond)) or 0
        never = _idx(Page.index_status == "unknown", Page.index_checked_at.is_(None))
        blind = _idx(Page.index_status == "unknown", Page.index_checked_at.isnot(None))
        missing = _idx(Page.index_status == "not_indexed")
        if never:
            steps.append({"href": "/", "text": f"{never} опубликованных страниц ещё не проверялись на индексацию — запусти «индексация» (site:)."})
        if blind:
            steps.append({"href": "/diag", "text": f"{blind} страниц проверить не удалось: движки SearXNG не ответили (CAPTCHA/лимит). Это про поисковик, а не про сайт — почини SearXNG и повтори проверку."})
        if missing:
            steps.append({"href": "/", "text": f"{missing} опубликованных страниц нет в индексе — попадание занимает дни, проверяй периодически."})
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
    from app.services.scoring import (blind_reason, bulk_ok, history_evidence, history_note,
                                      history_verdict, stale_donors, DROP_GRACE)
    from app.services.transitions import dirty_reason

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
        # строка инбокса: домен + причина «вслепую» + срочность + вердикт истории + улики.
        # Все решения приняты в Python — в Jinja нет ни tz-нормализации, ни доступа к скорингу.
        # Вердикт едет ОТДЕЛЬНО от blind: «не проверяли» и «проверили, и там грязь» — разные
        # вещи, а подпись «история чистая» не имеет права стоять ни под тем, ни под другим.
        # Улики (снимки Wayback, по которым машина судила) едут всегда, когда они есть: вердикт
        # ошибается — куратор должен мочь перепроверить и «грязно», и «чисто».
        # `ok` — РЕЗУЛЬТАТ bulk_ok(d), ТОТ ЖЕ предикат, что решает пакетное одобрение
        # (_bulk_candidates ниже). Шаблон обязан подписывать «история чистая» ПО ЭТОМУ ФЛАГУ,
        # а не реконструировать условие из blind/hist на месте — иначе два места молча
        # разъедутся (см. bulk_ok).
        "inbox": [(d, blind_reason(d), _urgent(d, soon, now), history_verdict(d),
                   history_evidence(d), bulk_ok(d), history_note(d)) for d in inbox],
        # окно дропа закрыто — купить уже нельзя. Домен уехал вниз и не «срочный», но выглядит
        # обычным кандидатом: без метки его можно одобрить (в т.ч. пакетом) и пойти покупать
        # покойника. Множеством, а не флагом в кортеже, — нужно и в «готовы к выкупу».
        "expired_ids": {d.id for d in inbox + ready if _expired(d, now)},
        "ready": ready,
        # ГРЯЗЬ НА ВИТРИНЕ ВЫКУПА. «Готовы к выкупу» — экран, с которого ИДУТ ТРАТИТЬ ДЕНЬГИ, и
        # до этого фикса отмытый РКН-домен (approved + reject_reason='rkn') стоял здесь без
        # единой метки (аудит F13). Сервисы его теперь не пустят ни в очередь, ни в «купил
        # руками» — но оператор обязан УВИДЕТЬ причину, а не упереться в отказ на клике.
        # Причина — по-русски, из того же словаря, что и везде (labels.reject_ru).
        #
        # ИНБОКС (`scored`) — тоже: `bulk_ok` грязь из пакета исключает, но кнопка «✓ одобрить»
        # у такой строки оставалась и вела в ГАРАНТИРОВАННЫЙ отказ политики (ревью Задачи 6,
        # Minor 6). Кнопка, которая не может сработать, — то же ложное предложение, что и
        # «↩ вернуть в approved» для РКН-домена в реестре.
        "dirty_by_id": {d.id: _reject_ru(r) for d in inbox + ready
                        if (r := dirty_reason(d)) is not None},
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
    from app.services.transitions import dirty_reason
    rows = db.execute(stmt.order_by(Domain.score.desc().nulls_last(),
                                    Domain.referring_domains.desc().nulls_last())
                      .limit(limit)).scalars().all()
    counts = _domain_counts(db)
    return templates.TemplateResponse(request, "pool.html", {
        "active": "domains", "rows": rows, "counts": counts, "total": sum(counts.values()),
        "site_by_domain": dict(db.execute(select(Site.domain_id, Site.id)).all()),
        # какие строки грязные — решает ПОЛИТИКА, а не шаблон по списку кодов: реестр рисует
        # кнопки действий, и «↩ вернуть в approved» для РКН-домена (аудит F9) была именно тут.
        # Jinja не имеет права переизобретать этот предикат — разъедется молча.
        "dirty_by_id": {d.id: _reject_ru(r) for d in rows if (r := dirty_reason(d)) is not None},
        "f_status": status or "", "f_min_score": "" if min_score is None else min_score,
        "f_limit": limit, "show_all": show_all,
    })


def _bulk_candidates(db: Session, min_score: float):
    """(годные к одобрению, сколько ПРОПУЩЕНО пакетом).

    Гейт истории — через `scoring.bulk_ok`, ОДИН предикат для пакета и для подписи «история
    чистая» в строке инбокса (см. domains_view). Раньше это условие было реконструировано
    здесь И в Jinja независимо — ровно та связка, на которой аудит поймал F2 (пустой Wayback
    ошибки не даёт → «вслепую» не определялось → штамповали как чистое); любое новое значение
    вердикта/причины отсева развело бы их снова, молча.

    Второе число раньше звалось `blind` — и это имя ВРАЛО: `bulk_ok` отсеивает не только
    «проверить не удалось», но и «проверили, и там грязь» (с F9 — ещё и РКН/блэклист). Домен,
    отсеянный за казино в истории, объявлялся оператору «оценённым вслепую». Считаем то, что
    считаем: сколько строк пакет НЕ ТРОНУЛ.
    """
    from app.services.scoring import bulk_ok
    rows = db.execute(select(Domain).where(Domain.status == "scored",
                                           Domain.score >= min_score)).scalars().all()
    ok = [d for d in rows if bulk_ok(d)]
    return ok, len(rows) - len(ok)


@router.get("/domains/bulk-preview")
def bulk_preview(min_score: float = 0.8, db: Session = Depends(get_session)):
    from fastapi.responses import JSONResponse
    ok, skipped = _bulk_candidates(db, max(0.0, min(1.0, min_score)))
    return JSONResponse({"n": len(ok), "skipped": skipped})


@router.post("/domains/bulk-approve")
def bulk_approve_action(min_score: float = Form(0.8), db: Session = Depends(get_session)):
    """Пакетное одобрение — это КЛИК ЧЕЛОВЕКА, гейт курации на месте (деньги не тратятся:
    approved != куплен). Домены, чью историю не подтвердили (Wayback лежал) или подтвердили как
    грязную, в пакет НЕ попадают — иначе пакет стал бы обходом того самого гейта, ради которого
    он существует.

    Перевод — через политику (services/transitions), хотя `bulk_ok` грязь уже отсеял: пакет
    двигает статус ПАЧКОЙ, и это последнее место, где стоит перепроверить себя перед записью.
    Отказ политики здесь — это баг рассинхрона предикатов, а не рабочая ветка: он обязан быть
    ВИДЕН оператору, а не проглочен молча.
    """
    from app.services import transitions
    ok, skipped = _bulk_candidates(db, max(0.0, min(1.0, min_score)))
    approved, denied = 0, []
    for d in ok:
        try:
            transitions.set_status(d, "approved")
            approved += 1
        except transitions.TransitionDenied as e:
            denied.append(str(e))
    db.commit()
    msg = f"Одобрено пакетом: {approved}"
    if skipped:
        msg += f" · пропущено (историю не подтвердить или она грязная): {skipped} — их реши руками"
    if denied:
        return _back("/domains", err=f"{msg} · политика отвергла {len(denied)}: {denied[0]}")
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


def _require_cf_write(request: Request) -> None:
    """Hard gate: любой CF-write требует НАСТРОЕННЫЙ panel auth. Same-origin недостаточен
    (аудит §11/§15) — панель живёт на LAN, а same-origin ничего не доказывает про то, кто
    физически может достучаться до порта. Транспортная Basic-проверка (если включена) стоит
    отдельно; здесь проверяется, что auth ВООБЩЕ сконфигурирован — иначе плоская LAN-экспозиция
    открывает Cloudflare-мутации кому угодно, кто знает IP. `request` не используется сейчас —
    параметр под будущие роуты-потребители (P1+ мутации), которые зовут этот гейт первой строкой,
    как и запуск sync (задача 5)."""
    if not (settings.PANEL_USER and settings.PANEL_PASS):
        raise HTTPException(status_code=403,
                            detail="Cloudflare-операции требуют настроенных PANEL_USER/PANEL_PASS")


@router.get("/settings", response_class=HTMLResponse)
def settings_view(request: Request, db: Session = Depends(get_session)):
    from app.services import settings as st
    s = st.get_settings()
    return templates.TemplateResponse(request, "settings.html", {
        "active": "settings", "s": s, "counts": _pool_counts(db, s)})


@router.get("/settings/cloudflare", response_class=HTMLResponse)
def settings_cloudflare_view(request: Request):
    """Read-only экран правды Cloudflare (задача 7, P0). Ни одной формы, мутирующей CF —
    единственное действие на странице — уже существующий запуск sync (задача 5/6)."""
    with SessionLocal() as db:
        conns = db.query(CloudflareConnection).order_by(CloudflareConnection.id).all()
        accounts = db.query(CloudflareAccount).order_by(CloudflareAccount.name).all()
        zones = (db.query(CloudflareZoneMirror)
                   .order_by(CloudflareZoneMirror.name).all())
        # capability-чипы: capabilities_json живёт на CloudflareConnectionAccount (НЕ на
        # CloudflareConnection) — агрегируем по connection (allowed побеждает denied/unknown).
        caps_by_conn: dict[int, dict] = {}
        for ca in db.query(CloudflareConnectionAccount).all():
            d = caps_by_conn.setdefault(ca.connection_id, {})
            for k, v in (ca.capabilities_json or {}).items():
                if d.get(k) != "allowed":
                    d[k] = v
        conn_rows = [{"c": c, "caps": caps_by_conn.get(c.id, {})} for c in conns]
        # привязка зоны к Site — по внешнему hex зоны (backfill P0): Site.cf_zone_id (legacy) —
        # _backfill_site_links (cf_sync.py) ставит cf_zone_mirror_id ТОЛЬКО рядом с cf_zone_id,
        # так что для колонки «Site» достаточно единственного ключа
        by_zone = {}
        for s in db.query(Site).all():
            if s.cf_zone_id:
                by_zone.setdefault(s.cf_zone_id, s)
        # DNS/cert-паки для колонок «DNS»/«cert» (аудит §11) — счётчики non-missing по зоне
        dns_counts = dict(db.query(CloudflareDnsRecordMirror.cloudflare_zone_id,
                                   func.count(CloudflareDnsRecordMirror.id))
                            .filter(CloudflareDnsRecordMirror.missing_since.is_(None))
                            .group_by(CloudflareDnsRecordMirror.cloudflare_zone_id).all())
        cert_counts = dict(db.query(CloudflareCertificatePackMirror.cloudflare_zone_id,
                                    func.count(CloudflareCertificatePackMirror.id))
                             .filter(CloudflareCertificatePackMirror.missing_since.is_(None))
                             .group_by(CloudflareCertificatePackMirror.cloudflare_zone_id).all())
        rows = [{"z": z, "site": by_zone.get(z.cf_zone_id),
                 "dns": dns_counts.get(z.cf_zone_id, 0),
                 "certs": cert_counts.get(z.cf_zone_id, 0)} for z in zones]
        # «Аккаунт» в таблице зон — читаемое имя, если аккаунт уже наблюдён; иначе сырой hex
        acct_names = {a.cf_account_id: a.name for a in accounts if a.name}
    return templates.TemplateResponse(request, "settings_cloudflare.html", {
        "active": "settings",
        "conn_rows": conn_rows, "accounts": accounts, "acct_names": acct_names, "rows": rows,
        "auth_configured": bool(settings.PANEL_USER and settings.PANEL_PASS),
    })


@router.get("/autopilot", response_class=HTMLResponse)
def autopilot_view(request: Request, db: Session = Depends(get_session)):
    from app.services.autonomy import get_autonomy
    from app.services.orchestrator import COUNT_RU
    from app.models.autonomy import AutonomyRun
    runs = db.execute(select(AutonomyRun).order_by(AutonomyRun.id.desc()).limit(10)).scalars().all()
    return templates.TemplateResponse(request, "autopilot.html", {
        "active": "autopilot", "a": get_autonomy(), "gates": _gates(db), "runs": runs,
        # ключи counts — не только стадии (queue_dirty: сколько грязных обошла стадия очереди),
        # и оператор читает журнал по-русски, а не по именам функций
        "count_ru": COUNT_RU})


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
    from app.models.offer import OfferSettings
    os_row = db.get(OfferSettings, 1)
    return templates.TemplateResponse(request, "offers.html", {
        "active": "offers", "rows": rows,
        "reserve_offer_url": os_row.reserve_offer_url if os_row else "",
    })


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
        # Сколько машина ЖДЁТ, прежде чем счесть отправку оборвавшейся. Из константы, а не числом
        # в шаблоне: очередь обязана называть оператору тот же срок, по которому судит сверка
        # (ревью Задачи 8, минор 3) — разъедься они, и человек в промежутке решит, что кнопка
        # сломана: бейдж пишет «заказ уходит провайдеру…», а сверка отвечает «не трогали».
        "stuck_after_min": acquisition.STUCK_CLAIM_MIN,
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
    # F3 (аудит 2026-07-15): p.offer_id зафиксирован при генерации и НЕ переоценивается публикацией
    # (см. комментарий Page.offer_id) — если оффер выключили ПОСЛЕ генерации, страница молча
    # опубликует ссылку на выключенный оффер. Публикация намеренно не блокируется (решение
    # пользователя), но карточка сайта обязана это ПОКАЗАТЬ — иначе оператор узнает только
    # постфактум с уже опубликованной мёртвой ссылкой.
    offer_ids = {p.offer_id for p in pages if p.offer_id is not None}
    page_offers = {o.id: o for o in db.execute(
        select(Offer).where(Offer.id.in_(offer_ids))).scalars().all()} if offer_ids else {}
    from app.models.offer import OfferSettings
    _os_row = db.get(OfferSettings, 1)
    reserve_configured = bool(_os_row and _os_row.reserve_offer_url)
    return templates.TemplateResponse(request, "site.html", {
        "active": "dash",
        "site": site, "domain": d.domain if d else f"#{site.domain_id}",
        "pages": pages, "pc": pc, "attached": attached, "all_offers": all_offers,
        "page_offers": page_offers, "reserve_configured": reserve_configured,
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


@router.post("/settings/cloudflare/sync")
def cloudflare_sync(request: Request):
    """Ручной запуск read-only Cloudflare sync (P0 — НИ ОДНОЙ CF-мутации, только наблюдение
    внешней правды в mirror-таблицы). CF-write-гейт первой строкой (задача 6): без настроенных
    PANEL_USER/PANEL_PASS плоская LAN-панель пускала бы к Cloudflare-операциям кого угодно, кто
    знает IP — до чтения формы, до spawn."""
    _require_cf_write(request)
    from app.services import jobs

    def _job():
        with jobs.track("cf_sync", trigger="manual",
                        stages=[{"key": "verify", "label": "Проверка токенов", "state": "pending"},
                                {"key": "zones", "label": "Зоны и записи", "state": "pending"}]) as rid:
            with SessionLocal() as db:
                cf_sync.sync_all(db, report=lambda **kw: jobs.report(rid, **kw), run=rid)
    ok = jobs.spawn("cf_sync", _job)
    return _back_here(request, err=None if ok else "Синхронизация уже идёт")


@router.post("/run/{job}/cancel")
def run_cancel_action(request: Request, job: str):
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
    """Ручная курация. 'purchased' здесь — money-gate человека мимо очереди; оркестратор
    (services/orchestrator) этот роут НЕ зовёт.

    `_MANUAL_STATUSES` — это whitelist КНОПОК (какие цели вообще есть у панели). Сам переход
    судит политика (services/transitions): она смотрит ИСХОДНЫЙ статус и грязь. Раньше здесь
    не было ничего, кроме whitelist'а целей, — и «↩ вернуть в approved» отмывала РКН-домен
    одним кликом (аудит F9).
    """
    from app.services import transitions
    if status not in _MANUAL_STATUSES:
        return _back("/domains", err=f"недопустимая цель перехода: {status!r}")
    d = db.get(Domain, domain_id)
    if d is None:
        return _back("/domains", err=f"домен #{domain_id} не найден")
    try:
        transitions.set_status(d, status)
    except transitions.TransitionDenied as e:
        db.rollback()
        return _back("/domains", err=str(e))
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
        # Конфликт — не «ошибка сверки», а найденный дубль: провайдер держит заказ в полёте, а
        # домен уже занят другим открытым заказом (одна открытая заявка на домен). Молчать о нём
        # нельзя: за такой строкой стоят деньги, которые могли уйти.
        #
        # `checked` — сколько НАШИХ строк провайдер вообще знает, и конфликтные среди них: они
        # тоже нашлись, просто не поехали. Поэтому «из них», а не отдельным слагаемым — иначе
        # дубль считался бы дважды и разбивка не сходилась с итогом (ревью Задачи 7, минор 4).
        dup = (f" · дублей не поднято {r['conflicts']} (у домена уже есть открытый заказ — "
               f"смотри пометку в очереди)") if r.get("conflicts") else ""
        # «из них» цеплялось к «в полёте», а дубль как раз НЕ в полёте — он в `checked` (ревью
        # Задачи 7, раунд 3). Оговорку двигаем к тому числу, в которое дубль реально входит.
        checked = (f"наших заказов {r['checked']}"
                   + (" (дубли входят сюда же)" if r.get("conflicts") else ""))
        # Застрявшие отправки (F11) — ради них сверку и жмут, когда строка висит в «отправляется».
        # `lost` в `checked` не входит (провайдер про такой заказ НЕ знает — сверять было не с чем),
        # `sending` тоже (её не трогали) — потому оба отдельными слагаемыми, а не «из них».
        stuck = (f" · застрявших отправок разобрано {r['lost']} (провайдер про них не знает — "
                 f"заказа нет, деньги не ушли; можно повторить или снять)") if r.get("lost") else ""
        live = (f" · отправок в полёте {r['sending']} — не трогали: их прямо сейчас шлёт провайдеру "
                f"живая отправка, вердикт за неё выносить нельзя") if r.get("sending") else ""
        return _back("/queue", msg=f"Сверено с провайдером: {checked} · "
                                   f"поймано {r.get('caught', 0)} · не вышло {r.get('failed', 0)} · "
                                   f"в полёте {r.get('pending', 0)}{dup}{stuck}{live}.")
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
        r = acquisition.cancel_order(order_id)
        # Отмена НЕ всегда возвращает домен в approved (его может держать открытый заказ или
        # заказ с неизвестным исходом — деньги могли уйти), и отменить она может не всё
        # (maybe_sent заперт). Сказать это словами, а не рапортовать успех вслепую.
        if r.get("error"):
            return _back("/queue", err=f"отмена заказа #{order_id}: {r['error']}")
        if r.get("status") != "cancelled":
            return _back("/queue", err=f"заказ #{order_id}: {r.get('note') or 'снять нельзя'}")
        return _back("/queue", msg=f"Заказ #{order_id} снят — {r.get('note') or 'домен не тронут'}.")
    except Exception as e:  # noqa: BLE001
        return _back("/queue", err=f"отмена: {e}")


@router.post("/offers/create")
def offer_create_action(brand: str = Form(...), affiliate_link: str = Form(...),
                        promo_code: str = Form(""), country: str = Form(""),
                        language: str = Form(""), db: Session = Depends(get_session)):
    if not brand.strip() or not affiliate_link.strip():
        return _back("/offers", err="бренд и партнёрская ссылка обязательны")
    # F28 (аудит 2026-07-14): affiliate_link уходит в href опубликованной страницы почти как есть
    # (content.render_html только html.escape() — экранирует спецсимволы, НЕ схему). Без этой
    # проверки "javascript:alert(1)" сохранялся бы как валидный оффер и исполнялся по клику на
    # живом сайте. allowlist http/https — тут (создание) И в render_html (defense in depth: этот
    # роут можно обойти прямым API-вызовом, см. pipeline.py::create_offer).
    from app.services.content import is_safe_url
    if not is_safe_url(affiliate_link.strip()):
        return _back("/offers", err="партнёрская ссылка: разрешены только http/https")
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


@router.post("/offers/reserve-url")
def offer_reserve_url_save(reserve_offer_url: str = Form(""), db: Session = Depends(get_session)):
    """F3 (аудит 2026-07-15): резервный URL для страниц с выключенным офером. Тот же is_safe_url,
    что и affiliate_link на создании оффера (F28, defense in depth — вторая точка в render_html)."""
    from app.models.offer import OfferSettings
    from app.services.content import is_safe_url
    url = reserve_offer_url.strip()
    if url and not is_safe_url(url):
        return _back("/offers", err="резервный URL: разрешены только http/https")
    row = db.get(OfferSettings, 1)
    if row is None:
        row = OfferSettings(id=1)
        db.add(row)
    row.reserve_offer_url = url or None
    try:
        db.commit()
    except IntegrityError:
        # гонка на первом сохранении (двойной клик до появления строки id=1): второй
        # коммит бьётся о PK — дружелюбный редирект вместо голого 500, как у всех
        # прочих write-роутов этого файла.
        db.rollback()
        return _back("/offers", err="Резервный URL уже сохранён — обнови страницу")
    return _back("/offers", msg="Резервный URL сохранён" if url else "Резервный URL очищен")


@router.post("/sites/{site_id}/attach-offer")
def attach_offer_action(site_id: int, offer_id: int = Form(...), db: Session = Depends(get_session)):
    exists = db.execute(select(SiteOffer).where(
        SiteOffer.site_id == site_id, SiteOffer.offer_id == offer_id)).scalar_one_or_none()
    if not exists:
        db.add(SiteOffer(site_id=site_id, offer_id=offer_id))
        try:
            db.commit()
        except IntegrityError:
            # TOCTOU на uq_site_offer (F24): под READ COMMITTED оба конкурентных запроса
            # видят «нет» и оба вставляют — второй коммит бьётся об уникальный индекс.
            # Дружелюбно, а не голым 500: оффер уже привязан — это и был желаемый исход.
            db.rollback()
            return _back(f"/sites/{site_id}", msg="Оффер уже привязан")
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
        if r.get("ssl_error"):
            # Зелёный баннер «готов: DNS + vhost + SSL» поверх упавшего SSL — это ровно то
            # враньё, от которого лечим машину. Vhost поднят (потому не `error`), но HTTPS под
            # вопросом: говорим об этом красным и оставляем след на карточке (site.ssl_error).
            return _back(f"/sites/{site_id}", err=(
                "Provision прошёл (зона + A-запись + vhost), но SSL-режим Cloudflare НЕ "
                f"переключился: {r['ssl_error']}. HTTPS может не работать — почини причину "
                "и нажми Provision ещё раз (идемпотентно)."))
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
        # Вердикт — через labels.index_ru: сырое `unknown` во флеше оператор прочтёт как «нет».
        s = ", ".join(f"{k}: {_index_ru(v)}" for k, v in pages.items())
        return _back(f"/sites/{site_id}", msg=f"Индексация — {s}")
    except Exception as e:  # noqa: BLE001
        return _back(f"/sites/{site_id}", err=f"индексация: {e}")


# --- self-update: git pull + миграции (панель localhost-only, POST-only) -----
# ponytail: тянем по HTTPS с fine-grained PAT — не монтируем SSH-ключ в контейнер.
# Требует volume `.:/repo` + git в образе (см. docker-compose/Dockerfile).
def _pull_banner(r: dict):
    """Единый баннер из dict deploy.git_pull()/git_force_pull().

    r["ok"] честно отражает и git, и алембик (F22/F23/F29): упавшая миграция — красный
    err=, НЕ зелёный msg=, даже если код при этом обновился (git pull сам прошёл)."""
    rebuild = " · нужна пересборка образа: docker compose up -d --build" if r.get("needs_rebuild") else ""
    if not r.get("ok"):
        if "old" not in r:
            # git pull/fetch/checkout не прошёл сам по себе — до алембика не дошли
            return _back("/diag", err=r.get("error", "обновление не удалось"))
        # git отработал (код обновлён или уже был свежим), но МИГРАЦИЯ ПРОВАЛИЛАСЬ —
        # код и схема БД разъехались, это не успешный деплой.
        subj = r.get("subject", "")
        transition = f"{r['old']}→{r['new']}" if r["old"] != r["new"] else r["new"]
        warn = r.get("alembic_warn") or "код обновлён, миграция не выполнена"
        return _back("/diag", err=f"Код обновлён ({transition} «{subj}»), но МИГРАЦИЯ "
                                   f"ПРОВАЛИЛАСЬ: {warn}{rebuild}")
    verb = "Принудительно обновлено" if r.get("forced") else "Обновлено"
    subj = r.get("subject", "")
    if r["old"] == r["new"]:
        return _back("/diag", msg=f"Уже свежая версия: {r['new']} «{subj}»{rebuild}")
    return _back("/diag", msg=f"{verb}: {r['old']}→{r['new']} «{subj}»{rebuild}")


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
