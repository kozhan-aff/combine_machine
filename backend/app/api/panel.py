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
from fastapi.concurrency import run_in_threadpool
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.guards import require_cf_write
from app.config import settings
from app.db import get_session, SessionLocal
from app.models.cloudflare import (
    CloudflareAccount, CloudflareCertificatePackMirror, CloudflareConnection,
    CloudflareConnectionAccount, CloudflareDnsRecordMirror, CloudflareZoneMirror,
)
from app.models.domain import Domain
from app.models.offer import Offer, SiteOffer
from app.models.site import Site, Page
from app.services import cf_sync, diag_cache, locales

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))
from app.services.labels import (status_ru as _status_ru, reject_ru as _reject_ru,
                                 lane_ru as _lane_ru, index_ru as _index_ru,
                                 site_status_ru as _site_status_ru,
                                 source_ru as _source_ru, source_badge as _source_badge)
templates.env.filters["status_ru"] = _status_ru
templates.env.filters["site_status_ru"] = _site_status_ru   # у сайта `published` — «опубликован», не «на сайте»
templates.env.filters["source_ru"] = _source_ru
templates.env.filters["source_badge"] = _source_badge
templates.env.filters["reject_ru"] = _reject_ru
templates.env.filters["lane_ru"] = _lane_ru
templates.env.filters["index_ru"] = _index_ru
templates.env.globals["site_langs"] = sorted(locales.TEXTS)   # языки шаблона сайта (select «Генерация»)
templates.env.globals["diag_alert"] = diag_cache.alert   # баннер в base.html читает кэш
router = APIRouter()

# ручная курация из шортлиста. 'purchased' = оператор купил домен руками — этот клик
# и ЕСТЬ money-gate (заказ провайдеру отсюда не уходит). См. CLAUDE.md, правило 2.
_MANUAL_STATUSES = {"approved", "rejected", "purchased"}

_JOBS = ("discovery", "score", "recheck", "sweep", "cf_sync", "generate", "edit", "domain_lists", "domain_ranks", "research",
         "guides_digest")   # известные джобы реестра


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
        steps.append({"href": "/offers", "text": "Добавь оффер: без него текстам не на что ссылаться."})
    if dc.get("discovered"):
        steps.append({"href": "/domains/pool?status=discovered", "text": f"{dc['discovered']} доменов найдено и не проверено — нажми «Проверить домены»."})
    if dc.get("scored"):
        steps.append({"href": "/domains", "text": f"{dc['scored']} доменов ждут решения — одобри или отклони."})
    if dc.get("approved"):
        steps.append({"href": "/domains", "text": f"{dc['approved']} одобрено — нажми «К покупке» или купи сам и отметь «Уже купил сам»."})
    purchased_no_site = db.execute(
        select(Domain).where(Domain.status == "purchased")
        .where(~Domain.id.in_(select(Site.domain_id)))).scalars().all()
    if purchased_no_site:
        steps.append({"href": "/domains/pool?status=purchased", "text": f"{len(purchased_no_site)} купленных без сайта — нажми «Создать сайт»."})
    for s in db.execute(select(Site).where(Site.status == "provisioning")).scalars().all():
        steps.append({"href": f"/sites/{s.id}", "text": f"Сайт #{s.id}: нажми «Поднять сайт»."})
    for s in db.execute(select(Site).where(Site.status == "content")).scalars().all():
        n_pages = db.scalar(select(func.count()).select_from(Page).where(Page.site_id == s.id)) or 0
        if not n_pages:
            steps.append({"href": f"/sites/{s.id}", "text": f"Сайт #{s.id}: нажми «Написать тексты»."})
    if pc.get("draft"):
        steps.append({"href": "/", "text": f"{pc['draft']} черновиков ждут вычитки — открой страницу, вычитай и нажми «Одобрить»."})
    if pc.get("edited"):
        steps.append({"href": "/", "text": f"{pc['edited']} страниц вычитано — нажми «Опубликовать» в карточке сайта."})
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
            steps.append({"href": "/", "text": f"{never} страниц на сайте ещё не проверялись в поиске — нажми «Проверить индексацию» в карточке сайта."})
        if blind:
            steps.append({"href": "/diag", "text": f"{blind} страниц проверить не удалось: поиск (SearXNG) не ответил. Это про поисковик, а не про сайт — нажми «Проверить связь» и повтори."})
        if missing:
            steps.append({"href": "/", "text": f"{missing} страниц на сайте ещё нет в поиске — на это уходят дни, проверяй время от времени."})
    if not steps:
        steps.append({"href": "/domains", "text": "Дел нет: нажми «Найти домены» — соберём свежие дропы."})
    return steps


def _pool_counts(db: Session, s: dict) -> dict:
    """Сколько доменов пула проходит каждый гейт при текущих порогах (превью эффекта).

    Правила счёта зеркалят волны (scoring._run_waves), иначе превью врёт: RD судит W4 и режет только
    ИЗВЕСТНЫЙ RD < порога — NULL (ещё не спрошен) проходит; возраст — W5, по старшей из даты
    RDAP/whois и первого снимка (Р5). Архив РФ-пула v1 (`legacy_ru`, миграция 0025) машина больше не
    судит — в превью его нет (находка R2-16): тысячи старых .ru раздували бы каждый счётчик.
    """
    from datetime import datetime, timezone, timedelta
    live = or_(Domain.reject_reason.is_(None), Domain.reject_reason != "legacy_ru")

    def n(*where) -> int:
        return db.scalar(select(func.count()).select_from(Domain).where(live, *where)) or 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=365.25 * s["min_age_years"])
    return {"total": n(),
            "rd": n(or_(Domain.referring_domains.is_(None),
                        Domain.referring_domains >= s["min_referring_domains"])),
            # возраст — старшая из даты RDAP/whois и первого снимка (Р5): age_years хранит решающий
            "age": n(or_(Domain.whois_created <= cutoff, Domain.age_years >= s["min_age_years"])),
            "approve": n(Domain.score >= s["approve_at"]),
            "manual": n(Domain.score >= s["manual_review_at"], Domain.score < s["approve_at"])}


def _ranks_view(db: Session) -> dict:
    """Блок «Ранги доменов» на /settings: что загружено + итог последней загрузки (ошибку разбора видно в панели)."""
    from app.services import domain_ranks, jobs
    try:
        last = jobs.last("domain_ranks")
    except Exception:  # noqa: BLE001
        last = None
    try:
        return {"overview": domain_ranks.overview(db), "last": last}
    except Exception:  # noqa: BLE001 — таблицы нет (миграция не накачена) не должно ронять /settings
        db.rollback()
        return {"overview": [], "last": last}


def _lists_view(db: Session) -> dict:
    """Блок «Списки чистоты» на /settings: что загружено + сколько кандидатов пула попало бы под отказ."""
    from app.services import domain_lists, jobs
    try:
        last = jobs.last("domain_lists")      # итог последней загрузки: ошибку разбора видно не только в логе воркера
    except Exception:  # noqa: BLE001
        last = None
    try:
        return {"overview": domain_lists.overview(db), **domain_lists.pool_counts(db), "last": last}
    except Exception:  # noqa: BLE001 — таблицы нет (миграция не накачена) не должно ронять /settings
        db.rollback()
        return {"overview": [], "hard": 0, "any": 0, "last": last}


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
def _worker_status() -> dict:
    """Сердцебиение воркера для /diag и Пульта (F8-11). Сбой чтения БД — «неизвестно», не 500."""
    from app.services import heartbeat
    try:
        return heartbeat.status()
    except Exception:  # noqa: BLE001
        return {"alive": False, "age_sec": None, "note": ""}


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
        "worker": _worker_status(),
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

    Такой домен доезжает до инбокса и живёт там до перепроверки: для lane='bid' воронка W2
    короткозамыкает лейном и приобретаемость на скоринге не судит вовсе. Держать его наверху
    как «срочный» — значит звать оператора решать судьбу покойника (ревью 2026-07-13).
    Дедлайн в v2 — только дата дропа из источника (DropCatch/Nominet): проекции whois больше нет
    (её давал TCI, удалён вместе с РФ)."""
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


INBOX_PAGE = 300   # строк инбокса M1 на страницу (F8-12)


@router.get("/domains", response_class=HTMLResponse)
def domains_view(request: Request, lang: str | None = None, page: int = 1,
                 db: Session = Depends(get_session)):
    """Инбокс решений: только то, где ждут ТЕБЯ. Полный реестр — /domains/pool.

    `?lang=xx` — фильтр по языку прошлого сайта. Счётчик «на решении» и список языков — ДО
    фильтра; при выбранном языке форма пакета скрыта: пакет взял бы и невидимые домены других
    языков (находка 1.6)."""
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import case
    from app.services import jobs
    from app.services.scoring import (blind_reason, emd_newreg, history_evidence,
                                      history_note, history_verdict, list_hits, stale_donors, topic_far,
                                      DROP_GRACE)
    from app.services.settings import get_settings
    from app.services.transitions import dirty_reason, zone_closed
    settings = get_settings()                        # одно чтение настроек на страницу
    allow = settings["tld_allowlist"]

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
    inbox_total = len(inbox)
    langs = sorted({d.market_lang for d in inbox + ready if d.market_lang})
    if lang:
        inbox = [d for d in inbox if d.market_lang == lang]
        ready = [d for d in ready if d.market_lang == lang]
    counts = _domain_counts(db)
    soon = now + timedelta(days=_URGENT_DAYS)
    urgent = sum(1 for d in inbox + ready if _urgent(d, soon, now))
    # Страница инбокса (F8-12): тяжёлый расчёт вердиктов/улик идёт по КАЖДОЙ показанной строке, а при
    # v2-потоке scored растёт без предела. Счётчики выше (urgent/langs/inbox_total) считаны по ВСЕМУ
    # списку, режем только отрисовку; порядок уже общий, так что страницы не пересекаются.
    inbox_n = len(inbox)
    pages = max(1, -(-inbox_n // INBOX_PAGE))
    page = max(1, min(page, pages))
    inbox = inbox[(page - 1) * INBOX_PAGE: page * INBOX_PAGE]
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
        # `ok` — РЕЗУЛЬТАТ _bulk_eligible(d, allow) (зона + bulk_ok), ТОТ ЖЕ предикат, что решает
        # пакетное одобрение (_bulk_candidates ниже). Шаблон обязан подписывать «история чистая» ПО ЭТОМУ ФЛАГУ,
        # а не реконструировать условие из blind/hist на месте — иначе два места молча
        # разъедутся (см. bulk_ok).
        "inbox": [(d, blind_reason(d), _urgent(d, soon, now), history_verdict(d),
                   history_evidence(d), _bulk_eligible(d, allow), history_note(d)) for d in inbox],
        "inbox_total": inbox_total, "inbox_n": inbox_n, "page": page, "pages": pages,
        "page_size": INBOX_PAGE, "langs": langs, "f_lang": lang or "",
        # прошлая тема далека от VPN (инвариант 4) — пометка в инбоксе и в «Готовы к покупке»
        "far_ids": {d.id for d in inbox + ready if topic_far(d)},
        # попадание в списки чистоты UT1/blocklistproject (мягкий сигнал): id -> категории
        "list_hit_cats": {d.id: list_hits(d) for d in inbox + ready if list_hits(d)},
        # EMD-новорег с пустым архивом (R2-14) — нейтральное «архив пуст», а не «⚠ НЕ проверена»
        "newreg_ids": {d.id for d in inbox if emd_newreg(d)},
        # Р2: «пакет от скора» по умолчанию = «порог сильного кандидата» из /settings
        "bulk_default": settings["approve_at"],
        # зона вне белого списка: «✓ Одобрить» политика отвергнет (R2-19) — строка рисует «зона не в
        # белом списке» вместо кнопки и не пишет «история чистая» (тот же zone_closed, что у политики)
        "closed_ids": {d.id for d in inbox + ready if zone_closed(d, allow)},
        # окно дропа закрыто — купить уже нельзя. Домен уехал вниз и не «срочный», но выглядит
        # обычным кандидатом: без метки его можно одобрить (в т.ч. пакетом) и пойти покупать
        # покойника. Множеством, а не флагом в кортеже, — нужно и в «готовы к выкупу».
        "expired_ids": {d.id for d in inbox + ready if _expired(d, now)},
        "ready": ready,
        # ГРЯЗЬ НА ВИТРИНЕ ВЫКУПА. «Готовы к покупке» — экран, с которого ИДУТ ТРАТИТЬ ДЕНЬГИ, и
        # до этого фикса отмытый РКН-домен (approved + reject_reason='rkn') стоял здесь без
        # единой метки (аудит F13). Сервисы его теперь не пустят ни в очередь, ни в «купил
        # руками» — но оператор обязан УВИДЕТЬ причину, а не упереться в отказ на клике.
        # Причина — по-русски, из того же словаря, что и везде (labels.reject_ru).
        #
        # ИНБОКС (`scored`) — тоже: `bulk_ok` грязь из пакета исключает, но кнопка «✓ Одобрить»
        # у такой строки оставалась и вела в ГАРАНТИРОВАННЫЙ отказ политики (ревью Задачи 6,
        # Minor 6). Кнопка, которая не может сработать, — то же ложное предложение, что и
        # «↩ Вернуть в одобренные» для РКН-домена в реестре.
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
                      limit: int = 200, page: int = 1, show_all: bool = False,
                      db: Session = Depends(get_session)):
    """Полный реестр — для расследований, а не для ежедневной работы. `limit` — размер страницы,
    `page` — номер (F8-12: раньше строки за топ-1000 по score были недостижимы)."""
    limit = max(1, min(limit, 1000))            # серверный кап: не тянуть всю таблицу в память
    stmt = select(Domain)
    if status:
        stmt = stmt.where(Domain.status == status)
    elif not show_all:                          # по умолчанию только приобретаемые
        stmt = stmt.where(or_(Domain.reject_reason.is_(None),
                              Domain.reject_reason != "not_acquirable"))
    if min_score is not None:
        stmt = stmt.where(Domain.score >= min_score)
    from app.services.settings import get_settings
    from app.services.transitions import dirty_reason, zone_closed
    allow = get_settings()["tld_allowlist"]          # одно чтение настроек на страницу
    matched = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    pages = max(1, -(-matched // limit))
    page = max(1, min(page, pages))
    # id в хвосте сортировки — стабильные страницы при равных score/RD (иначе строка могла бы
    # переехать между страницами или пропасть)
    rows = db.execute(stmt.order_by(Domain.score.desc().nulls_last(),
                                    Domain.referring_domains.desc().nulls_last(), Domain.id)
                      .limit(limit).offset((page - 1) * limit)).scalars().all()
    counts = _domain_counts(db)
    return templates.TemplateResponse(request, "pool.html", {
        "active": "domains", "rows": rows, "page": page, "pages": pages, "matched": matched, "counts": counts, "total": sum(counts.values()),
        "site_by_domain": dict(db.execute(select(Site.domain_id, Site.id)).all()),
        # какие строки грязные — решает ПОЛИТИКА, а не шаблон по списку кодов: реестр рисует
        # кнопки действий, и «↩ Вернуть в одобренные» для РКН-домена (аудит F9) была именно тут.
        # Jinja не имеет права переизобретать этот предикат — разъедется молча.
        "dirty_by_id": {d.id: _reject_ru(r) for d in rows if (r := dirty_reason(d)) is not None},
        # зона вне белого списка (R2-19): тот же предикат, что у политики, — кнопку «↩ вернуть в
        # approved» шаблон не рисует (она вела в гарантированный отказ)
        "closed_ids": {d.id for d in rows if d.status in ("rejected", "scored") and zone_closed(d, allow)},
        "f_status": status or "", "f_min_score": "" if min_score is None else min_score,
        "f_limit": limit, "show_all": show_all,
        # query пейджера: пустые фильтры пропущены (`min_score=` → 422 на float|None)
        "pager_qs": urlencode({k: v for k, v in (("status", status), ("min_score", min_score),
                               ("limit", limit), ("show_all", 1 if show_all else None))
                               if v not in (None, "")}),
    })


def _bulk_eligible(d, allow) -> bool:
    """Годен ли scored-домен к пакетному одобрению: зона в белом списке И `bulk_ok`. ОДИН предикат
    для пакета (_bulk_candidates) и для строки инбокса (domains_view): строка подписывает «история
    чистая» и рисует «✓ Одобрить» именно по нему, иначе домен вне белого списка (оператор сузил
    allowlist после скоринга) получал бы кнопку, которую политика гарантированно отвергнет."""
    from app.services.scoring import bulk_ok
    from app.services.transitions import zone_closed
    return not zone_closed(d, allow) and bulk_ok(d)


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
    from app.services.settings import get_settings
    allow = get_settings()["tld_allowlist"]          # одно чтение настроек на пакет
    rows = db.execute(select(Domain).where(Domain.status == "scored",
                                           Domain.score >= min_score)).scalars().all()
    # Зона вне белого списка (R2-19): политика не пустит такой домен в approved — пакет его не
    # берёт (иначе падал бы отказом политики) и считает в «пропущено».
    ok = [d for d in rows if _bulk_eligible(d, allow)]
    return ok, len(rows) - len(ok)


def _bulk_threshold(raw: str | None) -> float:
    """Порог пакета из поля инбокса: число -> в [0, 1]; пусто или мусор -> approve_at из /settings
    («порог сильного кандидата»), а не зашитые 0.8 (иначе при approve_at=0.9 пакет брал бы НИЖЕ
    видимого оператору порога). ОДИН разбор для превью-счётчика и самого пакета: очищенное поле
    шлёт `?min_score=`, и float-параметр превью отвечал 422 — счётчик показывал «undefined», хотя
    POST то же поле принимал (финальное ревью, minor «г»)."""
    from app.services.settings import get_settings
    try:
        threshold = float(raw)
    except (TypeError, ValueError):
        threshold = get_settings()["approve_at"]
    return max(0.0, min(1.0, threshold))


@router.get("/domains/bulk-preview")
def bulk_preview(min_score: str = "", db: Session = Depends(get_session)):
    from fastapi.responses import JSONResponse
    ok, skipped = _bulk_candidates(db, _bulk_threshold(min_score))
    return JSONResponse({"n": len(ok), "skipped": skipped})


@router.post("/domains/bulk-approve")
def bulk_approve_action(min_score: str = Form(""), db: Session = Depends(get_session)):
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
    # Очищенное поле формы приходит пустой строкой — порог по умолчанию approve_at (_bulk_threshold)
    ok, skipped = _bulk_candidates(db, _bulk_threshold(min_score))
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
        msg += (f" · пропущено (не всё проверено, тема далека от VPN, домен из ключевых слов или "
                f"зона не из списка): {skipped} — реши по ним сам в строке")
    if denied:
        return _back("/domains", err=f"{msg} · не одобрено {len(denied)}: {denied[0]}")
    return _back("/domains", msg=msg)


@router.get("/diag", response_class=HTMLResponse)
def diag_view(request: Request):
    from app.services import deploy as _deploy
    from app.services.diagnostics import PING_TIMEOUT
    checks, checked_at = diag_cache.get()   # кэш мгновенно (живой прогон — только кнопкой и фоном)
    ok = sum(1 for c in checks if c["status"] == "ok")
    crit_down = [c["label"] for c in checks if c.get("critical") and c["status"] == "fail"]
    return templates.TemplateResponse(request, "diag.html", {
        "active": "diag", "checks": checks, "ok": ok, "total": len(checks),
        "crit_down": crit_down, "timeout": PING_TIMEOUT, "checked_at": checked_at,
        "repo": settings.GITHUB_REPO, "can_pull": bool(settings.GITHUB_TOKEN),
        "status": _deploy.deploy_status(), "worker": _worker_status(),
    })


@router.post("/diag/refresh")
def diag_refresh(request: Request):
    """Явная «проверить снова» (на /diag и в баннере): синхронный прогон диагностики (≤20с, пинги
    параллельны, single-flight, TTL-кэш проб сброшен), редирект назад — баннер отражает свежий кэш."""
    diag_cache.refresh(force=True)
    raw = request.headers.get("referer") or "/"
    p = urlsplit(raw)
    # выбрасываем прежние flash-параметры: иначе старый ?err= подавит «перепроверено», а повторные клики пухнут URL
    q = urlencode([(k, v) for k, v in parse_qsl(p.query) if k not in ("msg", "err")])
    back = urlunsplit((p.scheme, p.netloc, p.path or "/", q, ""))
    return _back(back, msg="Связь проверена заново")


def _require_cf_write(request: Request) -> None:
    """Тонкая обёртка над app.api.guards.require_cf_write (общий гейт панели и /api).
    `request` не используется — параметр под роуты-потребители, которые зовут гейт первой строкой."""
    require_cf_write()


def _settings_page(request: Request, db: Session, emd_draft: str | None = None,
                   form_err: str | None = None, status_code: int = 200,
                   draft: dict | None = None):
    """Экран /settings. Остаток units Ahrefs — из кэша диагностики, без сети (находка 3.4).
    Наборы EMD — json.dumps без \\u-экранирования (|tojson прятал «grátis»); `emd_draft` —
    непринятый ввод оператора после ошибки JSON (находка 6.1).

    `draft` — ВСЁ остальное, что оператор отправил (числа, веса, тумблеры, тексты зон и брендов):
    при ошибке форма возвращается с его значениями поверх сохранённых, иначе он чинит JSON, жмёт
    «Сохранить» — и прочие правки молча теряются. В БД draft не пишется, только рисуется."""
    import json
    from app.services import settings as st
    s = st.get_settings()
    draft = draft or {}
    s.update({k: v for k, v in draft.get("nums", {}).items() if v is not None})
    s["weights"] = {**s["weights"], **draft.get("weights", {})}
    if "sources" in draft:
        s["sources_enabled"] = draft["sources"]
    emd_text = emd_draft if emd_draft is not None else json.dumps(s["emd_sets"], ensure_ascii=False,
                                                                  indent=1)
    tld_text = draft.get("tld_allowlist")
    brand_text = draft.get("brand_tokens")
    return templates.TemplateResponse(request, "settings.html", {
        "active": "settings", "s": s, "counts": _pool_counts(db, s), "lists": _lists_view(db), "ranks": _ranks_view(db),
        "units_left": diag_cache.value("ahrefs"), "emd_text": emd_text, "form_err": form_err,
        "tld_text": tld_text if tld_text is not None else "\n".join(s["tld_allowlist"]),
        "brand_text": brand_text if brand_text is not None else "\n".join(s["brand_tokens"])},
        status_code=status_code)


@router.get("/settings", response_class=HTMLResponse)
def settings_view(request: Request, db: Session = Depends(get_session)):
    return _settings_page(request, db)


def _guides_err(e: Exception) -> str:
    """Отказ действия с правилами письма — словами. OSError (нет прав на папку, `.digest` занят файлом) —
    тоже причина для оператора, а не голый 500."""
    if isinstance(e, OSError):
        return f"папка правил не принимает запись: {e.strerror or type(e).__name__}"
    return str(e)


@router.get("/guides", response_class=HTMLResponse)
def guides_view(request: Request):
    """Правила письма оператора (content_guides/): файлы, кому идёт каждый и его выжимка; загрузка, удаление.
    Отдельный пункт меню — это вход машины наравне с офферами: по выжимкам пишет писатель и судит критик."""
    from app.services import guides, jobs
    writer, critic = guides.load_guides(role="writer"), guides.load_guides(role="critic")
    waiting = len(writer["pending"])                     # ждущие выжимки — одни и те же для обеих ролей
    return templates.TemplateResponse(request, "guides.html", {
        "active": "guides", "guides": guides.status(), "limit": guides.LIMIT, "roles": guides.ROLE_RU,
        "writer": writer, "critic": critic, "run": jobs.progress("guides_digest"),
        # папки нет вовсе (том не подключён, неверный путь) — экран говорит это словами, с путём
        "missing": writer["missing"], "guides_dir": str(guides.guides_dir()),
        "waiting": guides.no_digest_ru(waiting, "в задание не попадает", "в задание не попадают") if waiting else ""})


@router.post("/guides/digest")
def guides_digest_action(force: str = Form("")):
    """Сжать правила письма — фоновая задача `guides_digest`: по обращению к модели на каждый большой файл,
    у которого нет актуальной выжимки (`force` — на все). Запускается только отсюда: воркер папку правил
    видит только на чтение."""
    from app.services import guides, jobs
    ok = jobs.spawn("guides_digest", lambda: guides.build_digests(force=bool(force)))
    if not ok:
        return _back("/guides", err=jobs.busy_msg("Правила уже сжимаются — дождись, полоса вверху покажет ход"))
    return _back("/guides", msg="Правила сжимаются в фоне: большой файл — одно обращение к модели, до нескольких "
                                "минут. Ход — в полосе вверху; когда она исчезнет, обнови страницу.")


@router.post("/guides/role")
def guides_role_action(rel: str = Form(""), role: str = Form("")):
    """Кому идут правила файла — решение оператора: сжатие эту роль больше не меняет."""
    from app.services import guides
    try:
        guides.set_role(rel, role)
    except (ValueError, OSError) as e:
        return _back("/guides", err=_guides_err(e))
    return _back("/guides", msg=f"{rel} — кому: {guides.ROLE_RU[role]}")


@router.get("/guides/digest/{name}", response_class=HTMLResponse)
def guide_digest_view(request: Request, name: str):
    """Выжимка одного файла правил: посмотреть и поправить. Имя — только существующий файл папки."""
    from app.services import guides
    try:
        text = guides.read_digest(name)
    except ValueError as e:
        return _back("/guides", err=str(e))
    row = next((g for g in guides.status() if g["rel"] == name), None)
    if row is None:
        return _back("/guides", err=f"файла {name} нет")
    return templates.TemplateResponse(request, "guide_digest.html", {
        "active": "guides", "g": row, "text": text, "roles": guides.ROLE_RU, "max": guides.current_cap() * 2})


@router.post("/guides/digest/{name}")
def guide_digest_save_action(name: str, text: str = Form("")):
    """Сохранить выжимку, написанную или поправленную оператором. Пустой текст — отказ от своей версии."""
    from app.services import guides
    try:
        saved = guides.save_digest(name, text)
    except (ValueError, OSError) as e:
        return _back("/guides", err=_guides_err(e))
    if not saved:
        return _back("/guides", msg=f"{name}: своей выжимки больше нет — файл сожмёт машина («Сжать правила»)")
    cut = len(saved) < len(text.replace("\r\n", "\n").strip())
    return _back(f"/guides/digest/{quote(name)}",
                 msg="Выжимка сохранена" + (f" — текст обрезан до {len(saved)} симв." if cut else ""))


@router.post("/guides/upload")
async def guides_upload_action(request: Request):
    """Загрузить файлы правил письма в content_guides/ (можно несколько разом или папку целиком).
    Имя санируется в guides.save_guide; чужие расширения внутри выбранной папки пропускаются словами."""
    from app.services import guides
    form = await request.form()
    ups = [u for u in form.getlist("file") if getattr(u, "filename", "")]
    if not ups:
        return _back("/guides", err="файл не выбран")
    saved, errs = [], []
    for up in ups:
        try:
            saved.append(guides.save_guide(up.filename, await up.read()))
        except ValueError as e:
            errs.append(str(e))
        except OSError as e:
            errs.append(f"«{up.filename}»: {_guides_err(e)}")
    done = f"Сохранено файлов: {len(saved)}" + (f" ({', '.join(saved)})" if saved else "")
    if errs:            # часть не прошла — говорим и что легло, и что нет (flash показывает одно из двух)
        return _back("/guides", err=f"{done}. Не приняты: " + "; ".join(errs))
    return _back("/guides", msg=done)


@router.post("/guides/delete")
def guides_delete_action(rel: str = Form("")):
    from app.services import guides
    try:
        guides.delete_guide(rel)
    except (ValueError, OSError) as e:
        return _back("/guides", err=_guides_err(e))
    return _back("/guides", msg=f"Удалён {rel}")


def _keys_page(request: Request, form_err: str | None = None, draft: dict | None = None,
               status_code: int = 200):
    """Экран «Ключи и доступы». В шаблон уходят только маски секретов (api_keys.describe);
    `draft` — введённое НЕ-секретное (при ошибке валидации форма не теряет адреса/модели)."""
    from app.services import api_keys
    return templates.TemplateResponse(request, "settings_keys.html", {
        "active": "settings", "groups": api_keys.describe(), "form_err": form_err,
        "draft": draft or {}, "auth_configured": bool(settings.PANEL_USER and settings.PANEL_PASS)},
        status_code=status_code)


@router.get("/settings/keys", response_class=HTMLResponse)
def settings_keys_view(request: Request):
    return _keys_page(request)


def _keys_save(request: Request, updates: dict, resets: set):
    """Синхронная часть сохранения (БД, рендер) — вызывается из async-роута через threadpool,
    чтобы не блокировать event loop."""
    from app.services import api_keys
    # Жёсткий гейт (по прецеденту _require_cf_write): ключи — это деньги и доступ к инфраструктуре,
    # а плоская LAN-панель без Basic-auth пустила бы к ним любого в сети. Раньше любого разбора формы.
    if not (settings.PANEL_USER and settings.PANEL_PASS):
        return _keys_page(request, status_code=403,
                          form_err="Не сохранено ничего: задай PANEL_USER и PANEL_PASS в .env — без "
                                   "пароля на панель менять ключи нельзя (панель открыта всей сети).")
    try:
        res = api_keys.save(updates, resets)
    except ValueError as e:                    # текст ValueError — наш, без значений
        draft = {k: v.strip() for k, v in updates.items() if v.strip() and not api_keys.EDITABLE[k].secret}
        return _keys_page(request, form_err=f"Не сохранено ничего: {e}", draft=draft, status_code=400)
    except RuntimeError as e:
        return _keys_page(request, form_err=str(e), status_code=500)
    if not res["changed"] and not res["reset"]:
        return _back("/settings/keys", msg="Ничего не изменено: все поля пустые")
    parts = []
    if res["changed"]:
        parts.append("Сохранено: " + ", ".join(res["changed"]))
    if res["reset"]:
        parts.append("Возвращено из .env: " + ", ".join(res["reset"]))
    return _back("/settings/keys", msg=" · ".join(parts))


@router.post("/settings/keys")
async def settings_keys_save(request: Request):
    """Сохранить ключи/адреса. Берём из формы ТОЛЬКО ключи белого списка (`v_<KEY>` — значение,
    `r_<KEY>` — «сбросить к .env»): посторонние поля (PANEL_PASS, DATABASE_URL, что угодно) не
    читаются вовсе. Пустое значение = не менять. Значения не попадают ни в flash, ни в лог."""
    from app.services import api_keys
    form = await request.form()

    def _str(name):
        v = form.get(name)
        return v if isinstance(v, str) else ""

    updates = {k: _str("v_" + k) for k in api_keys.EDITABLE}
    resets = {k for k in api_keys.EDITABLE if _str("r_" + k)}
    return await run_in_threadpool(_keys_save, request, updates, resets)


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
    if any(o["provider"] == "backorder" and o["status"] in ("pending_confirm", "failed")
           and not o["confirmed"] for o in orders):
        c = BackorderClient()
        # Сетка и баланс — независимые сбои: упавший баланс не должен писать «подтверждать
        # нечем» над рабочим селектором ставки, и наоборот. Панель не падает ни от одного.
        try:
            for z in {o["zone"] for o in orders if o["zone"]}:   # None — сетки нет и не будет
                grids[z] = c.tariffs(z)
        except Exception as e:  # noqa: BLE001
            bo_err = f"ставки не загрузились: {type(e).__name__}: {e}"[:200]
        try:
            balance = c.balance()
        except Exception as e:  # noqa: BLE001 — баланс информационный, подтверждать не мешает
            bo_err = (bo_err + " · " if bo_err else "") + f"баланс: {type(e).__name__}"[:80]
    from app.integrations.registrar import get_registrar
    reg = get_registrar()
    return templates.TemplateResponse(request, "queue.html", {
        "active": "queue", "orders": orders, "grids": grids,
        # кто продаёт и почём — видно в строке ДО клика (фикс-цена регистратора; аукцион — ставка лота)
        "quotes": acquisition.registrar_quotes(orders),
        "reg_name": acquisition.REGISTRAR_RU.get(reg.name, reg.name) if reg.configured else None,
        "balance": balance, "bo_err": bo_err,
        "channels": acquisition.channel_status(orders),
        # Сколько машина ЖДЁТ, прежде чем счесть отправку оборвавшейся. Из константы, а не числом
        # в шаблоне: очередь обязана называть оператору тот же срок, по которому судит сверка
        # (ревью Задачи 8, минор 3) — разъедься они, и человек в промежутке решит, что кнопка
        # сломана: бейдж пишет «заказ уходит провайдеру…», а сверка отвечает «не трогали».
        "stuck_after_min": acquisition.STUCK_CLAIM_MIN,
        "n_pending": sum(1 for o in orders if o["status"] == "pending_confirm"),
    })


def _critic_cell(page) -> dict | None:
    """Вердикт критика для строки таблицы страниц: {"led", "label", "title"} и необязательная вторая строка
    "sub"; None — страницу не вычитывали. Подпись — одна короткая строка, подробности — в `title`: таблица
    обязана умещаться по ширине. Записанный вердикт показываем, только если он относится к нынешнему
    тексту (`verdict_is_fresh`): после правки он про другой текст, и «прошла» у него было бы неправдой."""
    from app.services import content_critic
    notes = page.critic_notes
    if page.critic_checked_at is None or not isinstance(notes, dict):
        return None
    if not content_critic.verdict_is_fresh(page):
        if page.status != "draft":
            return None                  # одобренной странице совет «вычитай заново» ни к чему
        return {"led": "led-off", "label": "устарел", "title": "текст изменён после вычитки — вычитай заново"}
    if notes.get("error"):
        # вердикта нет: критик не ответил или упала проверка — это не «замечания к тексту»
        return {"led": "led-warn", "label": "не проверена", "title": f"вычитка не состоялась: {notes['error']}"}
    if notes.get("pass") is True:
        title = "у проверок кодом и у редактора-модели замечаний нет"
        if page.status != "draft":
            return {"led": "led-ok", "label": "прошла", "title": title}     # уже одобрена — ждать некого
        if notes.get("note"):
            # прошла, но критик её сам не одобряет (правлена руками, старый способ) — почему, в заметке
            return {"led": "led-ok", "label": "прошла", "sub": "одобряешь ты", "title": str(notes["note"])}
        return {"led": "led-ok", "label": "прошла",
                "title": title + ". Страница остаётся черновиком, пока её не одобрят"}
    issues = [str(x) for x in notes.get("issues") or []]
    title = "; ".join(issues[:3]) + (f" … и ещё {len(issues) - 3}" if len(issues) > 3 else "")
    rounds = notes.get("round")
    if type(rounds) is int and rounds > 0:
        title += f" · переписана по замечаниям: {rounds} из {content_critic.MAX_ROUNDS}"
    return {"led": "led-todo", "label": f"{len(issues)} замеч." if issues else "не прошла", "title": title}


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
    from app.services import jobs, publish, research
    from app.services.autonomy import get_autonomy
    return templates.TemplateResponse(request, "site.html", {
        "active": "dash",
        "site": site, "domain": d.domain if d else f"#{site.domain_id}",
        "pages": pages, "pc": pc, "attached": attached, "all_offers": all_offers,
        "page_offers": page_offers, "reserve_configured": reserve_configured,
        "research": research.summary(db, site_id), "research_rows": research.dossier(db, site_id),
        "research_last": jobs.last("research"),
        "critic": {p.id: _critic_cell(p) for p in pages}, "auto_edit": get_autonomy()["auto_edit"],
        # оффер САЙТА — про него пишет писатель; остальные привязанные показываем вторым планом
        "own_offer": db.get(Offer, site.offer_id) if site.offer_id is not None else None,
        # «живые» — у которых есть файл на сайте (publish.live_clause): переписанная страница — draft
        # в базе, но сайт от этого неопубликованным не стал
        "n_live": db.scalar(select(func.count()).select_from(Page).where(
            Page.site_id == site_id, publish.live_clause())) or 0,
    })


@router.get("/pages/{page_id}", response_class=HTMLResponse)
def page_edit_view(request: Request, page_id: int, db: Session = Depends(get_session)):
    page = db.get(Page, page_id)
    if page is None:
        return _back("/", err=f"страница #{page_id} не найдена")
    site = db.get(Site, page.site_id)
    d = db.get(Domain, site.domain_id) if site else None
    from app.services import content_critic
    return templates.TemplateResponse(request, "page_edit.html", {
        "active": "dash",
        "page": page, "site": site, "domain": d.domain if d else "",
        # вердикт критика показываем, только если он относится к нынешнему тексту страницы
        "verdict_fresh": content_critic.verdict_is_fresh(page), "max_rounds": content_critic.MAX_ROUNDS,
        # отпечаток показанных заголовка и тела едет скрытым полем формы: сохранить и одобрить можно только
        # ту страницу, с которой редактор был открыт (пока он открыт, её мог переписать писатель)
        "seen_fp": content_critic.fingerprint(page.title, page.body),
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
    return _back_here(request, err=None if ok else jobs.busy_msg("«Найти домены» уже идёт"))


@router.post("/domains/add-list")
def domains_add_list(domains: str = Form("")):
    from app.services import discovery
    r = discovery.add_list(domains)
    cut = f", сверх {discovery._LIST_MAX} за раз отброшено {r['cut']}" if r["cut"] else ""
    return _back("/domains/pool", msg=f"Добавлено {r['added']}, уже были {r['known']}, "
                                      f"не домены {r['bad']}{cut} — новые проверит «Проверить домены»")


@router.post("/run/score")
def run_score_action(request: Request, n: int = Form(5)):
    from app.services import jobs, scoring
    ok = jobs.spawn("score", lambda: scoring.score_pending(limit=n))
    return _back_here(request, err=None if ok else jobs.busy_msg("«Проверить домены» уже идёт"))


@router.post("/run/recheck")
def run_recheck_action(request: Request, n: int = Form(200)):
    """Перепроверить whois'ом отобранных доноров: не выкупили ли их. Денег не тратит."""
    from app.services import jobs, scoring
    ok = jobs.spawn("recheck", lambda: scoring.recheck_acquirability(limit=n))
    return _back_here(request, err=None if ok else jobs.busy_msg("«Проверить, не заняты ли» уже идёт"))


@router.post("/settings/lists/refresh")
def lists_refresh(request: Request):
    """Ручная загрузка списков чистоты (не ждать ночи 03:30 UTC): первый прогон надо увидеть глазами."""
    from app.services import domain_lists, jobs
    ok = jobs.spawn("domain_lists", domain_lists.refresh)
    return _back_here(request, err=None if ok else jobs.busy_msg("Загрузка списков уже идёт"))


@router.post("/settings/ranks/refresh")
def ranks_refresh(request: Request):
    """Ручная загрузка рангов (Common Crawl + Majestic, если включён): файл читается потоком и может идти
    долго — фоновая задача с прогрессом, не ждём ночи/месяца."""
    from app.services import domain_ranks, jobs
    ok = jobs.spawn("domain_ranks", domain_ranks.refresh)
    return _back_here(request, err=None if ok else jobs.busy_msg("Загрузка рангов уже идёт"))


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
    return _back_here(request, err=None if ok else jobs.busy_msg("«Обновить из Cloudflare» уже идёт"))


@router.post("/run/{job}/cancel")
def run_cancel_action(request: Request, job: str):
    from app.services import jobs
    if job not in _JOBS:
        raise HTTPException(status_code=404, detail=f"неизвестный джоб: {job}")
    jobs.request_cancel(job)          # сервис увидит флаг между элементами и честно завершится
    return _back_here(request)


# Джобы, что гонят домены через волны скоринга (W0–W6) — им и
# нужна живая раскладка исхода, у discovery/sweep/cf_sync domain_score_log вообще не пишется.
_FUNNEL_JOBS = ("score", "recheck")


def _funnel_tally(db: Session, run_id: int) -> dict | None:
    """Живая раскладка исходов ЭТОГО прогона по domain_score_log — сколько уже отсеяно
    ДО дорогого Wayback (W0–W4: зоны/бренды, доступность, риск, ссылки) и сколько реально дошло
    до него (scored — Wayback пройден по определению; rejected/history_dirty — дошёл и там
    отклонён историей; rejected/low_score — дошёл, история чистая, но не дотянул итоговый
    балл, см. scoring._commit_result). Чипы волн в jobCard() показывают только ТЕКУЩУЮ волну —
    без этого счётчика оператор не видел ничего, что подтверждает: дешёвые волны реально
    отсеивают быстро, а не «все домены идут по кругу». None, если для этого прогона ещё нет ни
    одной строки (свежий старт) —
    карточка ничего не покажет, а не нарисует нулевую раскладку как будто уже что-то
    посчитано."""
    from app.models.domain_score_log import DomainScoreLog
    rows = db.execute(
        select(DomainScoreLog.outcome, DomainScoreLog.reject_reason, func.count())
        .where(DomainScoreLog.run_id == run_id)
        .group_by(DomainScoreLog.outcome, DomainScoreLog.reject_reason)
    ).all()
    if not rows:
        return None
    total = 0
    scored = unresolved = 0
    by_reason: dict[str, int] = {}
    reached_wayback = 0
    for outcome, reason, n in rows:
        total += n
        if outcome == "scored":
            scored += n
            reached_wayback += n           # scored всегда прошёл W5 (Wayback) — таков порядок волн
        elif outcome == "unresolved":
            unresolved += n
        elif outcome == "rejected":
            label = _reject_ru(reason) if reason else "?"
            by_reason[label] = by_reason.get(label, 0) + n
            # v2: too_young решает W5 (история, Р5), spam_anchors — W6 (после истории): оба уже
            # сожгли Wayback, как и history_dirty/low_score.
            # history_dirty и low_score рождаются ТОЛЬКО когда домен дошёл до истории (W5) —
            # history_dirty на самой W5, low_score позже, на самом _decide()
            # по уже посчитанному score (scoring._commit_result: `reject_reason = reject or
            # ("low_score" if rejected)`, т.е. low_score — это "остальное всё прошли, score
            # не дотянул"). Без low_score здесь счётчик "решено дёшево" завышался бы —
            # ровно те домены, что реально сожгли Wayback, попадали в "дёшево" и рисовали
            # оператору успокаивающую (и неверную) картину (находка ревью 2026-07-20).
            if reason in ("history_dirty", "too_young", "low_score", "spam_anchors"):
                reached_wayback += n
    return {"total": total, "scored": scored, "unresolved": unresolved,
            "reached_wayback": reached_wayback, "before_wayback": total - reached_wayback,
            "by_reason": by_reason}


@router.get("/api/jobs/live")
def jobs_live():
    """Что машина делает прямо сейчас + итог последнего прогона каждой задачи.
    Один эндпоинт на всю панель: карточки на Пульте/M1 и тонкая полоса в шапке."""
    from fastapi.responses import JSONResponse
    from app.services import jobs
    live_jobs = jobs.live()
    with SessionLocal() as db:
        for j in live_jobs:
            if j["name"] in _FUNNEL_JOBS and j.get("id") is not None:
                j["tally"] = _funnel_tally(db, j["id"])
    return JSONResponse(jsonable_encoder({
        "jobs": live_jobs,
        "last": {name: jobs.last(name) for name in _JOBS},
    }))


@router.get("/api/domains/{domain_id}/score-history")
def domain_score_history(domain_id: int, limit: int = 50):
    """История решений score_domain() по домену — новые сверху. Append-only лог
    (domain_score_log), не перезаписывается на рескоре, в отличие от
    Domain.score_breakdown (последний снимок для UI-бейджей)."""
    from fastapi.responses import JSONResponse
    from app.models.domain_score_log import DomainScoreLog

    with SessionLocal() as db:
        rows = db.execute(
            select(DomainScoreLog).where(DomainScoreLog.domain_id == domain_id)
            .order_by(DomainScoreLog.created_at.desc()).limit(limit)
        ).scalars().all()
        return JSONResponse(jsonable_encoder([{
            "id": r.id, "run_id": r.run_id, "outcome": r.outcome,
            "reject_reason": r.reject_reason, "score": r.score, "sig": r.sig,
            "created_at": r.created_at,
        } for r in rows]))


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
                "waiting": "домен ещё занят — дроп не наступил. Проверим его снова "
                           "в день дропа (без даты — в течение суток)",
                "whois_failed": "whois не ответил — домен остался в найденных, "
                                "попробуй позже",
                "whois_unclear": "whois ответил непонятно — домен остался в найденных, "
                                 "свободен ли он, НЕ установлено",
                "taken_undated": "домен занят, а дата дропа неизвестна — проверим снова "
                                 "через сутки, вдруг освободится",
                "budget": "лимит проверок whois исчерпан (см. Настройки) — "
                          "домен остался в найденных",
                "ahrefs_failed": "Ahrefs не ответил — домен остался в найденных, "
                                 "проверим в следующий раз",
                "units_floor": "остаток единиц Ahrefs неизвестен или ниже минимума (см. Настройки) — "
                               "домен остался в найденных",
                "ahrefs_missing": "Ahrefs не дал данных по домену — домен остался в найденных, "
                                  "проверим в следующий раз",
                "ahrefs_no_key": "ключ Ahrefs не задан (см. «Ключи и доступы») — "
                                 "домен остался в найденных",
            }.get(out.get("why"), "не удалось понять, свободен ли домен — он остался в найденных"))
        # статус и причину — словами из общего словаря, а не сырыми ключами
        res = _status_ru(out.get("status"))
        if out.get("reject_reason"):
            res += f" ({_reject_ru(out['reject_reason'])})"
        if out.get("score") is not None:          # у домена из ключевых слов оценки нет — не пишем «оценка None»
            res += f", оценка {out['score']}"
        return _back("/domains", msg=f"{out.get('domain', domain_id)} проверен: {res}")
    except Exception as e:  # noqa: BLE001
        return _back("/domains", err=f"проверка домена #{domain_id}: {e}")


@router.post("/domains/{domain_id}/set-status")
def set_status_action(domain_id: int, status: str = Form(...), db: Session = Depends(get_session)):
    """Ручная курация. 'purchased' здесь — money-gate человека мимо очереди; оркестратор
    (services/orchestrator) этот роут НЕ зовёт.

    `_MANUAL_STATUSES` — это whitelist КНОПОК (какие цели вообще есть у панели). Сам переход
    судит политика (services/transitions): она смотрит ИСХОДНЫЙ статус и грязь. Раньше здесь
    не было ничего, кроме whitelist'а целей, — и «↩ Вернуть в одобренные» отмывала РКН-домен
    одним кликом (аудит F9).
    """
    from app.services import transitions
    if status not in _MANUAL_STATUSES:
        return _back("/domains", err=f"так изменить статус нельзя: {status!r}")
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
    return _back("/domains", msg=f"Цены backorder обновлены: {n} доменов"
                 if n else "Цена backorder недоступна (тариф не прочитан)")


@router.post("/domains/{domain_id}/make-site")
def make_site_action(domain_id: int):
    from app.services import provisioning
    try:
        sid = provisioning.create_site_for(domain_id)
        return _back(f"/sites/{sid}", msg="Сайт создан. Дальше: привяжи оффер и нажми «Поднять сайт».")
    except Exception as e:  # noqa: BLE001
        return _back("/domains", err=f"создание сайта: {e}")


# --- M2 очередь выкупа (структурный путь: очередь + подтверждение + отправка) ------
@router.post("/domains/{domain_id}/queue")
def queue_add_action(domain_id: int, provider: str = Form("")):
    from app.services import acquisition
    try:
        oid = acquisition.create_order(domain_id, provider or None)
        return _back("/queue", msg=f"Домен поставлен к покупке (заказ #{oid}). Деньги спишутся только после твоего подтверждения.")
    except Exception as e:  # noqa: BLE001
        return _back("/domains", err=f"к покупке: {e}")


@router.post("/queue/{order_id}/confirm")
def queue_confirm_action(order_id: int, bid_rub: float = Form(0)):
    from app.services import acquisition
    try:
        r = acquisition.confirm_order(order_id, bid_rub or None)
        bid, cur = r.get("bid_rub"), r.get("currency")
        # «Ставка» — только там, где её задал человек в форме (тариф backorder, потолок аукциона).
        # Фиксированная цена (optimizator, обычная регистрация) ставкой не зовётся: у неё нет ни
        # торга, ни продления — флеш о деньгах обязан называть сумму тем, чем она является.
        is_bid = bool(bid_rub)
        if not bid:
            tail = ""
        elif cur in (None, "RUB"):
            tail = f", ставка {bid:.0f} ₽" if is_bid else f", сумма {bid:.0f} ₽"
        elif is_bid:   # аукцион NameSilo: полное списание (потолок + продление) в валюте котировки
            tail = f", к списанию до {bid:.2f} {cur} (ставка + год продления)"
        else:
            tail = f", к списанию не больше {bid:.2f} {cur}"
        return _back("/queue", msg=f"Заказ #{order_id} подтверждён{tail}. Теперь нажми «Отправить заказ».")
    except Exception as e:  # noqa: BLE001
        return _back("/queue", err=f"подтверждение: {e}")


@router.post("/queue/{order_id}/buy")
def queue_buy_action(order_id: int, max_price: float = Form(...)):
    """Один клик вместо двух: confirm_order (денежный гейт — ЧЕЛОВЕК только что нажал) и сразу
    execute_confirmed_order. Гейт не ослаблен: confirmed_by_human ставит тот же confirm_order, а
    execute проверяет его тем же SQL-условием claim; просто между ними нет второй кнопки.

    СУММА ВИДНА ЧЕЛОВЕКУ ДО СПИСАНИЯ: оператор задаёт потолок `max_price` (валюта регистратора, USD).
    confirm_order замораживает котировку регистратора; выше потолка — НЕ отправляем, заказ остаётся
    подтверждённым с видимой ценой (строка /queue), дальше решает человек («▶ Отправить заказ» или снять).
    Любой отказ confirm — заказ остаётся как был; отказ execute — обычный failed с причиной."""
    from app.services import acquisition
    if not max_price or max_price <= 0:
        return _back("/queue", err="укажи, не дороже скольки покупать (USD) — без этого не покупаем")
    try:
        r = acquisition.confirm_order(order_id)
    except Exception as e:  # noqa: BLE001
        return _back("/queue", err=f"подтверждение: {e}")
    cost, cur = r.get("bid_rub"), r.get("currency")
    if cost is None:
        return _back("/queue", err=f"заказ #{order_id} подтверждён, но регистратор не назвал цену — "
                                   "заказ не отправлен; смотри строку заказа")
    if float(cost) > max_price:
        return _back("/queue", err=f"заказ #{order_id}: цена регистратора {float(cost):.2f} {cur or ''} выше твоего предела "
                                   f"{max_price:.2f} — НЕ отправлен. Цена записана в заказе: нажми "
                                   "«Отправить заказ», если согласен, или «Отменить»")
    try:
        x = acquisition.execute_confirmed_order(order_id)
    except Exception as e:  # noqa: BLE001
        return _back("/queue", err=f"заказ #{order_id} подтверждён, но отправка упала: {e} — нажми «Повторить»")
    if x.get("error"):
        return _back("/queue", err=f"заказ #{order_id} подтверждён, отправка: {x['error']}")
    paid = f" ({float(cost):.2f} {cur})" if cur else f" ({float(cost):.2f})"
    if x.get("status") == "caught":
        site = f" сайт #{x['site_id']} создан" if x.get("site_id") else " сайт создай кнопкой «Создать сайт»"
        return _back("/queue", msg=f"Домен куплен{paid}: заказ #{order_id},{site}. "
                                   "Дальше — «Поднять сайт» в карточке сайта.")
    return _back("/queue", msg=f"Заказ #{order_id} подтверждён и отправлен{paid} — "
                               f"статус: {_status_ru(x.get('status'))}. Итог покажет «Обновить статусы».")


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
        return _back("/queue", msg=f"Заказ #{order_id} отправлен — статус: "
                                   f"{_status_ru(r.get('status'))}.{' ' + note if note else ''} "
                                   "Нажми «Обновить статусы»: при нулевом балансе заказ "
                                   "останется «Не оплачен» и домен не купят.")
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
        dup = (f" · дублей {r['conflicts']} (у домена уже есть открытый заказ — "
               f"смотри пометку в строке)") if r.get("conflicts") else ""
        # «из них» цеплялось к «в полёте», а дубль как раз НЕ в полёте — он в `checked` (ревью
        # Задачи 7, раунд 3). Оговорку двигаем к тому числу, в которое дубль реально входит.
        checked = (f"наших заказов {r['checked']}"
                   + (" (дубли входят сюда же)" if r.get("conflicts") else ""))
        # Застрявшие отправки (F11) — ради них сверку и жмут, когда строка висит в «отправляется».
        # `lost` в `checked` не входит (провайдер про такой заказ НЕ знает — сверять было не с чем),
        # `sending` тоже (её не трогали) — потому оба отдельными слагаемыми, а не «из них».
        stuck = (f" · оборванных отправок {r['lost']} (провайдер про них не знает — "
                 f"заказа нет, деньги не ушли; можно повторить или отменить)") if r.get("lost") else ""
        live = (f" · отправляется прямо сейчас {r['sending']} — их не трогали") if r.get("sending") else ""
        # Сбой ОДНОГО провайдера (капча backorder, таймаут) не прячем за «сверено»: он назван в ответе (S3-04)
        errs = r.get("errors") or {}
        bad = "".join(f" · {name}: {msg}" for name, msg in errs.items())
        text = (f"Статусы обновлены: {checked} · "
                f"получено {r.get('caught', 0)} · не вышло {r.get('failed', 0)} · "
                f"ещё ждём {r.get('pending', 0)}{dup}{stuck}{live}.")
        if errs:
            return _back("/queue", err=f"Статусы обновлены не полностью{bad}. {text}")
        return _back("/queue", msg=text)
    except Exception as e:  # noqa: BLE001
        return _back("/queue", err=f"обновление статусов: {e}")


@router.post("/queue/{order_id}/caught")
def queue_caught_action(order_id: int):
    from app.services import acquisition
    try:
        r = acquisition.mark_caught(order_id)
        if r.get("site_id"):
            return _back(f"/sites/{r['site_id']}", msg=f"Заказ #{order_id}: домен куплен, сайт создан — нажми «Поднять сайт».")
        return _back("/queue", msg=f"Заказ #{order_id}: домен куплен — можно создавать сайт.")
    except Exception as e:  # noqa: BLE001
        return _back("/queue", err=f"домен получен: {e}")


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
            return _back("/queue", err=f"заказ #{order_id}: {r.get('note') or 'отменить нельзя'}")
        return _back("/queue", msg=f"Заказ #{order_id} отменён — {r.get('note') or 'домен не тронут'}.")
    except Exception as e:  # noqa: BLE001
        return _back("/queue", err=f"отмена: {e}")


@router.post("/offers/create")
def offer_create_action(brand: str = Form(...), affiliate_link: str = Form(...),
                        promo_code: str = Form(""), promo_terms: str = Form(""),
                        country: str = Form(""),
                        language: str = Form(""), db: Session = Depends(get_session)):
    from app.models.offer import promo_pair
    if not brand.strip() or not affiliate_link.strip():
        return _back("/offers", err="бренд и партнёрская ссылка обязательны")
    try:
        code, terms = promo_pair(promo_code, promo_terms)
    except ValueError as e:
        return _back("/offers", err=str(e))
    # F28 (аудит 2026-07-14): affiliate_link уходит в href опубликованной страницы почти как есть
    # (content.render_html только html.escape() — экранирует спецсимволы, НЕ схему). Без этой
    # проверки "javascript:alert(1)" сохранялся бы как валидный оффер и исполнялся по клику на
    # живом сайте. allowlist http/https — тут (создание) И в render_html (defense in depth: этот
    # роут можно обойти прямым API-вызовом, см. pipeline.py::create_offer).
    from app.services.content import is_safe_url
    if not is_safe_url(affiliate_link.strip()):
        return _back("/offers", err="партнёрская ссылка: разрешены только http/https")
    o = Offer(brand=brand.strip(), affiliate_link=affiliate_link.strip(),
              promo_code=code, promo_terms=terms, country=country.strip() or None,
              language=language.strip() or None)
    db.add(o)
    db.commit()
    return _back("/offers", msg=f"Оффер «{o.brand}» добавлен")


@router.post("/offers/{offer_id}/update")
def offer_update_action(offer_id: int, affiliate_link: str = Form(...), promo_code: str = Form(""),
                        promo_terms: str = Form(""), db: Session = Depends(get_session)):
    """Правка ссылки и промокода готового оффера. Бренд/гео/язык не меняем: под них уже написаны
    страницы (offer_id — факт истории, F26). Новые значения встанут при следующей публикации."""
    from app.models.offer import promo_pair
    from app.services.content import is_safe_url
    o = db.get(Offer, offer_id)
    if o is None:
        return _back("/offers", err=f"оффер #{offer_id} не найден")
    if not is_safe_url(affiliate_link.strip()):
        return _back("/offers", err="партнёрская ссылка: разрешены только http/https")
    try:
        o.promo_code, o.promo_terms = promo_pair(promo_code, promo_terms)
    except ValueError as e:
        return _back("/offers", err=str(e))
    o.affiliate_link = affiliate_link.strip()
    db.commit()
    return _back("/offers", msg=f"Оффер «{o.brand}» обновлён — на сайтах изменится при следующей публикации")


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
        return _back("/offers", err="резервный адрес: разрешены только http/https")
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
        return _back("/offers", err="Резервный адрес уже сохранён — обнови страницу")
    return _back("/offers", msg="Резервный адрес сохранён" if url else "Резервный адрес убран")


@router.post("/sites/{site_id}/attach-offer")
def attach_offer_action(site_id: int, offer_id: int = Form(...), db: Session = Depends(get_session)):
    # Явная привязка (S6-13/S7-12): Site.offer_id — оффер, про который пишутся страницы. Уже
    # сгенерированные страницы свой offer_id сохраняют (F26) — меняется только дальнейшая генерация.
    site = db.get(Site, site_id)
    offer = db.get(Offer, offer_id)
    if site is None or offer is None:
        return _back(f"/sites/{site_id}" if site else "/", err="сайт или оффер не найден")
    if not offer.active:
        return _back(f"/sites/{site_id}", err=f"оффер «{offer.brand}» выключен — привяжи включённый")
    site.offer_id = offer_id
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
        # Откат унёс и Site.offer_id — выставляем заново.
        db.rollback()
        db.get(Site, site_id).offer_id = offer_id
        db.commit()
        return _back(f"/sites/{site_id}", msg="Оффер уже привязан")
    return _back(f"/sites/{site_id}", msg="Оффер привязан")


@router.post("/sites/{site_id}/provision")
def provision_action(site_id: int, request: Request):
    """CF-write-гейт первой строкой (S8, аудит 2026-07-18): именно этот роут реально
    создаёт CF-зону + DNS-запись + меняет SSL-режим боевым токеном — в отличие от
    /settings/cloudflare/sync (read-only, но уже был гейтнут), эта настоящая мутация
    гейта не имела. Без настроенных PANEL_USER/PANEL_PASS плоская LAN-панель пускала
    бы к CF-операциям кого угодно, кто знает IP."""
    _require_cf_write(request)
    from app.services import provisioning
    try:
        r = provisioning.provision(site_id)
        if r.get("status") == "awaiting_ns":
            return _back(f"/sites/{site_id}", msg=f"Зона создана, ждёт NS: {r.get('hint', '')}")
        if r.get("status") == "error":
            return _back(f"/sites/{site_id}", err=r.get("error", "сайт не поднялся"))
        if r.get("ssl_error"):
            # Зелёный баннер «готов» поверх упавшего SSL/настроек зоны — ровно то враньё, от
            # которого лечим машину. Vhost поднят (потому не `error`), но HTTPS под вопросом:
            # говорим об этом красным и оставляем след на карточке (site.ssl_error).
            return _back(f"/sites/{site_id}", err=(
                "Сайт поднят, но HTTPS в Cloudflare настроился не полностью: "
                f"{r['ssl_error']}. Исправь причину и нажми «Поднять заново» — это безопасно."))
        tls = {"origin_ca": "свой сертификат на сервере, Cloudflare в режиме strict",
               "ok": "сервер отвечает по HTTPS, Cloudflare в режиме full"}.get(
            r.get("origin_https"), "на сервере HTTPS нет — Cloudflare в режиме flexible")
        warn = f" ⚠ {'; '.join(r['warnings'])}." if r.get("warnings") else ""
        return _back(f"/sites/{site_id}", msg=f"Сайт поднят: домен заведён в Cloudflare и на сервере, проверка пройдена. HTTPS: {tls}. Дальше — «Изучить конкурентов».{warn}")
    except Exception as e:  # noqa: BLE001 — нет кредов CF/aaPanel и т.п.
        return _back(f"/sites/{site_id}", err=f"поднять сайт: {e}")


def _writer_refusal(db: Session, site_id: int) -> RedirectResponse | None:
    """Почему писателя запускать нельзя — готовым редиректом с причиной (None — можно). Общий для
    «Написать тексты» и «Переписать тексты»: явный отказ отдаём сразу, а не потом в карточке задачи."""
    from app.services import content, research
    site = db.get(Site, site_id)
    if site is None:
        return _back("/", err=f"сайт #{site_id} не найден")
    if content.status_refusal(site):
        return _back(f"/sites/{site_id}", err=f"написать тексты: {content.status_refusal(site)}")
    has_offer = content.site_offer(db, site) is not None or db.scalar(
        select(Page.id).where(Page.site_id == site_id, Page.offer_id.is_not(None)).limit(1))
    if not has_offer:
        return _back(f"/sites/{site_id}", err="Оффер не привязан или выключен: привяжи включённый "
                     "оффер на шаге 2 — без него страницы получились бы про чужой бренд.")
    if not research.has_dossier(db, site_id):
        # спека 2026-10-10 §4.5: темы, факты и цифры писатель берёт из досье — без него только выдумывать
        return _back(f"/sites/{site_id}", err="Сначала изучи конкурентов (шаг 4): без этого писать не по чему.")
    return None


@router.post("/sites/{site_id}/generate")
def generate_action(site_id: int, lang: str = Form(""), db: Session = Depends(get_session)):
    """Генерация — фоновая задача `generate` (S6-11/S7-14): LLM пишет минуты, держать ради этого
    HTTP-запрос нельзя. Пишет только по досье конкурентов: структуру тем несёт оно, отдельный
    `use_competitor` больше не нужен."""
    from app.services import content, jobs
    refusal = _writer_refusal(db, site_id)
    if refusal:
        return refusal
    ok = jobs.spawn("generate", lambda: content.generate_site(site_id, lang=lang or None))
    if not ok:
        return _back(f"/sites/{site_id}", err=jobs.busy_msg("Тексты уже пишутся — дождись на Пульте"))
    return _back(f"/sites/{site_id}", msg="Тексты пишутся в фоне: ход — на Пульте. "
                 "Дальше — вычитка: публикуются только одобренные страницы.")


@router.post("/sites/{site_id}/rewrite")
def rewrite_action(site_id: int, overwrite_manual: str = Form(""), db: Session = Depends(get_session)):
    """Переписать существующие страницы по досье — та же фоновая задача `generate`. Страницы возвращаются
    в черновики; файлы опубликованных остаются на сайте до новой публикации. Галочка `overwrite_manual` —
    явное решение оператора переписать и то, что он правил руками."""
    from app.services import content, jobs
    refusal = _writer_refusal(db, site_id)
    if refusal:
        return refusal
    manual = bool(overwrite_manual)
    ok = jobs.spawn("generate", lambda: content.generate_site(site_id, rewrite=True, overwrite_manual=manual))
    if not ok:
        return _back(f"/sites/{site_id}", err=jobs.busy_msg("Тексты уже пишутся — дождись на Пульте"))
    return _back(f"/sites/{site_id}", msg="Тексты переписываются в фоне: ход — на Пульте. Страницы вернутся в "
                 "черновики; опубликованные останутся на сайте в прежнем виде, пока не опубликуешь новые.")


@router.post("/sites/{site_id}/edit")
def edit_action(site_id: int, db: Session = Depends(get_session)):
    """Вычитка черновиков сайта критиком — фоновая задача `edit`. Одобрять ли прошедшие страницы, решает
    тумблер автопилота: сервис читает его сам (auto_edit не передаём), выключен — статус не меняется."""
    from app.services import content_critic, jobs
    from app.services.autonomy import get_autonomy
    if db.get(Site, site_id) is None:
        return _back("/", err=f"сайт #{site_id} не найден")
    if not db.scalar(select(Page.id).where(Page.site_id == site_id, Page.status == "draft").limit(1)):
        return _back(f"/sites/{site_id}", err="Вычитывать нечего: у сайта нет черновиков.")
    ok = jobs.spawn("edit", lambda: content_critic.edit_site(site_id))
    if not ok:
        return _back(f"/sites/{site_id}", err=jobs.busy_msg("Вычитка уже идёт (возможно, другого сайта) — дождись на Пульте"))
    then = ("прошедшие проверку страницы одобрит сам" if get_autonomy()["auto_edit"]
            else "вердикт появится в таблице страниц — одобряешь ты")
    return _back(f"/sites/{site_id}", msg=f"Критик читает черновики в фоне: {then}. Ход — на Пульте.")


@router.post("/sites/{site_id}/research")
def research_action(site_id: int, force: str = Form(""), db: Session = Depends(get_session)):
    """Досье конкурентов — фоновая задача `research` (спека 2026-10-10 §4): SERP + 5 страниц на запрос."""
    from app.services import jobs, research
    from app.services.content import site_offer as content_site_offer
    site = db.get(Site, site_id)
    if site is None:
        return _back("/", err=f"сайт #{site_id} не найден")
    if content_site_offer(db, site) is None:
        return _back(f"/sites/{site_id}", err="изучить конкурентов: оффер не привязан или выключен — искать не по чему")
    ok = jobs.spawn("research", lambda: research.build_dossier(site_id, force=bool(force)))
    if not ok:
        return _back(f"/sites/{site_id}", err=jobs.busy_msg("Конкурентов уже изучают (возможно, для другого сайта) — дождись на Пульте"))
    return _back(f"/sites/{site_id}", msg="Конкурентов изучаем в фоне: 4 запроса, до 5 страниц на каждый. Ход — на Пульте.")


@router.post("/pages/{page_id}/save")
def page_save_action(page_id: int, body: str = Form(""), seen: str = Form(""),
                     db: Session = Depends(get_session)):
    """ОДОБРИТЬ (гейт): draft -> edited. Тело — ровно то, что редактор видит в форме. Потерянное
    поле и очищенная textarea для FastAPI неразличимы (пустое значение = «нет поля»), поэтому обе
    ситуации дают пустое тело, и гейт его не пропускает (S7-11): старый текст молча не одобряется.
    `seen` — отпечаток заголовка и тела, с которыми форма была открыта: страницу, переписанную с тех пор
    писателем, сервис не одобрит (иначе — старый текст формы под новым заголовком, которого никто не видел)."""
    from app.services import content
    p = db.get(Page, page_id)
    sid = p.site_id if p else None
    try:
        # ЧЕЛОВЕК прошёл гейт: draft -> edited (+ sanitize)
        content.mark_edited(page_id, body, seen_fp=seen or None)
        return _back(f"/sites/{sid}", msg="Страница одобрена — можно публиковать.")
    except Exception as e:  # noqa: BLE001
        return _back(f"/pages/{page_id}", err=f"сохранение: {e}")


@router.post("/pages/{page_id}/draft")
def page_draft_action(page_id: int, body: str = Form(""), seen: str = Form(""),
                      db: Session = Depends(get_session)):
    """Сохранить правку КАК ЧЕРНОВИК, без одобрения (S6-16): статус не edited, публикация не возьмёт.
    `seen` — как у «Одобрить»: форма, открытая до переписывания страницы, текст писателя не затирает."""
    from app.services import content
    try:
        content.save_draft(page_id, body, seen_fp=seen or None)
        return _back(f"/pages/{page_id}", msg="Сохранено. Страница остаётся черновиком: на сайт не попадёт, пока не одобришь.")
    except Exception as e:  # noqa: BLE001
        return _back(f"/pages/{page_id}", err=f"сохранение: {e}")


@router.post("/pages/{page_id}/critique")
def critique_page_action(page_id: int):
    """Кнопка «Вычитать»: проверки кодом + вердикт модели по одной странице (план Б). Подсказка человеку:
    сама кнопка статус страницы не меняет ни при каком вердикте и ни при каком тумблере. Кто одобрит
    прошедшую страницу дальше — критик при вычитке сайта или только человек (текст правлен вручную, правила
    письма не сжаты, критик этому тексту уже отказывал), — флеш говорит как есть."""
    from app.services import content_critic
    from app.services.autonomy import get_autonomy
    try:
        v = content_critic.critique_page(page_id)
    except ValueError as e:
        return _back(f"/pages/{page_id}", err=str(e))
    except Exception as e:  # noqa: BLE001 — сбой критика не должен ронять редактор
        return _back(f"/pages/{page_id}", err=f"критик: {e}")
    if v["error"]:
        return _back(f"/pages/{page_id}", err=f"вычитка не состоялась: {v['error']}")
    if not v["pass"]:
        return _back(f"/pages/{page_id}", msg="Вердикт записан: есть замечания.")
    if v["note"]:
        # критик сам эту страницу не одобрит — говорим почему, а не обещаем «одобрит критик»
        why = v["note"].removeprefix("одобряет человек: ").removesuffix(" — одобряет человек")
        return _back(f"/pages/{page_id}", msg=f"Вердикт записан. Одобряешь ты: {why}.")
    then = ("Критик одобрит страницу при следующей вычитке сайта." if get_autonomy()["auto_edit"] is True
            else "Страница остаётся черновиком — одобряешь ты.")
    return _back(f"/pages/{page_id}", msg=f"Вердикт записан. {then}")


@router.post("/sites/{site_id}/publish")
def publish_action(site_id: int):
    from app.services import publish
    try:
        r = publish.publish_site(site_id)
        if r.get("status") == "no_edited_pages":
            return _back(f"/sites/{site_id}",
                         err="Публиковать нечего: вычитанных страниц нет — сначала вычитай черновики.")
        if r.get("status") == "not_provisioned":
            return _back(f"/sites/{site_id}", err=f"Публикация отложена: {r.get('hint', 'сайт ещё не поднят')}.")
        warn = (" ⚠ " + "; ".join(r["warnings"])) if r.get("warnings") else ""
        # причина отказа — как есть: файл страницы, переписанной во время публикации, на сайт лёг
        problems = [f"{k}: {v}" for k, v in (r.get("failed") or {}).items()] + \
                   [f"{k}: записана, но домен не подтвердил — {v}" for k, v in (r.get("unverified") or {}).items()]
        if r.get("status") in ("partial", "failed"):
            done = f"Опубликовано: {', '.join(r.get('pages', [])) or 'ничего'}. " if r.get("pages") else ""
            return _back(f"/sites/{site_id}", err=f"{done}Не опубликовано — {'; '.join(problems)}. "
                         f"Повторить можно — это безопасно.{warn}")
        return _back(f"/sites/{site_id}", msg=f"Опубликовано и проверено на домене: {', '.join(r.get('pages', []))}.{warn}")
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
        src = sorted(set((r.get("sources") or {}).values()))
        tail = f" (источник: {', '.join(src)})" if src else ""
        if r.get("gsc_note"):   # GSC отвалился -> проверка ушла в SearXNG; причину показываем, не глотаем
            tail += f". GSC недоступен: {r['gsc_note']}"
        return _back(f"/sites/{site_id}", msg=f"Индексация — {s}{tail}")
    except Exception as e:  # noqa: BLE001
        return _back(f"/sites/{site_id}", err=f"индексация: {e}")


# --- self-update: git pull + миграции (панель localhost-only, POST-only) -----
# ponytail: тянем по HTTPS с fine-grained PAT — не монтируем SSH-ключ в контейнер.
# Требует volume `.:/repo` + git в образе (см. docker-compose/Dockerfile).
def _pull_banner(r: dict):
    """Единый баннер из dict deploy.git_pull()/git_force_pull().

    r["ok"] честно отражает и git, и алембик (F22/F23/F29): упавшая миграция — красный
    err=, НЕ зелёный msg=, даже если код при этом обновился (git pull сам прошёл)."""
    rebuild = " · нужно пересобрать контейнеры: docker compose up -d --build" if r.get("needs_rebuild") else ""
    if r.get("compose_hint"):
        rebuild += f" · {r['compose_hint']}"         # состав контейнеров сменился — текст даёт deploy
    if not r.get("ok"):
        if "old" not in r:
            # git pull/fetch/checkout не прошёл сам по себе — до алембика не дошли
            return _back("/diag", err=r.get("error", "обновление не удалось"))
        # git отработал (код обновлён или уже был свежим), но МИГРАЦИЯ ПРОВАЛИЛАСЬ —
        # код и схема БД разъехались, это не успешный деплой.
        subj = r.get("subject", "")
        transition = f"{r['old']}→{r['new']}" if r["old"] != r["new"] else r["new"]
        warn = r.get("alembic_warn") or "обновление базы не выполнено"
        return _back("/diag", err=f"Программа обновлена ({transition} «{subj}»), но БАЗА "
                                   f"НЕ ОБНОВИЛАСЬ: {warn}{rebuild}")
    verb = "Обновлено принудительно" if r.get("forced") else "Обновлено"
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
        return _back("/diag", err="токен GitHub (GITHUB_TOKEN) не задан — проверить обновления нечем")
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
            return _back("/diag", err="не удалось узнать последнюю версию" + (f": {detail}" if detail else ""))
        if not cur:
            # current_version() упал (git в контейнере недоступен) — пустая cur делает
            # remote.startswith(cur) тривиально True для ЛЮБОГО remote: без этой ветки
            # мы бы соврали «актуально», хотя текущую версию не смогли определить вовсе.
            return _back("/diag", err="не удалось определить текущую версию (git в контейнере недоступен)")
        same = remote.startswith(cur) or cur.startswith(remote)
        return _back("/diag", msg=f"Версия {cur} — {'свежая' if same else 'есть новее: ' + remote}")
    except Exception as e:  # noqa: BLE001
        return _back("/diag", err=f"проверка обновлений: {type(e).__name__}")


@router.post("/settings/save")
def settings_save(request: Request, db: Session = Depends(get_session),
                  min_referring_domains: int = Form(...), min_age_years: float = Form(...),
                  approve_at: float = Form(...), manual_review_at: float = Form(...),
                  max_whois_per_run: int | None = Form(None),
                  min_dr: float | None = Form(None), max_links_per_run: int | None = Form(None),
                  max_deep_per_run: int | None = Form(None), units_floor: int | None = Form(None),
                  units_daily_cap: int | None = Form(None),
                  spam_anchor_max: float | None = Form(None),
                  tld_allowlist: str | None = Form(None), brand_tokens: str | None = Form(None),
                  emd_sets: str | None = Form(None), v2_lists: str = Form(""),
                  hard_reject_lists: str = Form(""),
                  rank_pct_low: float | None = Form(None), rank_pct_full: float | None = Form(None),
                  rank_majestic: str = Form(""),
                  dropcatch: str = Form(""), nominet: str = Form(""),
                  mx: str = Form(""), emd: str = Form(""), namesilo_auction: str = Form(""),
                  w_history_cleanliness: float | None = Form(None),
                  w_topical_fit: float | None = Form(None), w_age: float | None = Form(None),
                  w_rd: float | None = Form(None), w_authority: float | None = Form(None),
                  w_anchor_quality: float | None = Form(None),
                  w_traffic_history: float | None = Form(None)):
    from app.services import settings as st
    # веса — опциональны: форма без них (старый шаблон, curl из скрипта) не должна ОБНУЛЯТЬ
    # шкалу оценки. None -> ключ не передаём, update_settings оставит прежние.
    weights = {k: v for k, v in (("history_cleanliness", w_history_cleanliness),
                                 ("topical_fit", w_topical_fit), ("age", w_age), ("rd", w_rd),
                                 ("authority", w_authority), ("anchor_quality", w_anchor_quality),
                                 ("traffic_history", w_traffic_history)) if v is not None}
    # Пустую textarea FastAPI отдаёт как «поля нет» (None). Форма v2 несёт маркер `v2_lists`: значит
    # эти поля в ней БЫЛИ, и пустое — это «очистить», а не «не трогать» (находка 6.1). Форма без
    # маркера (старый шаблон, curl) списки не трогает.
    if v2_lists:
        tld_allowlist, brand_tokens, emd_sets = tld_allowlist or "", brand_tokens or "", emd_sets or ""
    try:
        st.update_settings(min_referring_domains=min_referring_domains, min_age_years=min_age_years,
                           approve_at=approve_at, manual_review_at=manual_review_at,
                           max_whois_per_run=max_whois_per_run,
                           min_dr=min_dr, max_links_per_run=max_links_per_run,
                           max_deep_per_run=max_deep_per_run, units_floor=units_floor,
                           units_daily_cap=units_daily_cap,
                           spam_anchor_max=spam_anchor_max, tld_allowlist=tld_allowlist,
                           brand_tokens=brand_tokens, emd_sets=emd_sets,
                           sources_enabled={"dropcatch": bool(dropcatch), "nominet": bool(nominet), "mx": bool(mx), "emd": bool(emd),
                                            "namesilo_auction": bool(namesilo_auction)},
                           weights=weights or None,
                           # чекбокс без маркера v2_lists (старый шаблон, curl) настройку не трогает
                           hard_reject_lists=bool(hard_reject_lists) if v2_lists else None,
                           rank_pct_low=rank_pct_low, rank_pct_full=rank_pct_full,
                           rank_majestic=bool(rank_majestic) if v2_lists else None)
    except ValueError as e:
        # Ничего не сохранено (update_settings падает до commit). Ввод оператора не теряем: редирект
        # унёс бы его JSON в никуда — отдаём форму заново с его текстом и причиной.
        draft = {"nums": {"min_referring_domains": min_referring_domains, "min_age_years": min_age_years,
                          "approve_at": approve_at, "manual_review_at": manual_review_at,
                          "max_whois_per_run": max_whois_per_run, "min_dr": min_dr,
                          "max_links_per_run": max_links_per_run, "max_deep_per_run": max_deep_per_run,
                          "units_floor": units_floor, "spam_anchor_max": spam_anchor_max,
                          "hard_reject_lists": bool(hard_reject_lists) if v2_lists else None,
                          "rank_pct_low": rank_pct_low, "rank_pct_full": rank_pct_full,
                          "rank_majestic": bool(rank_majestic) if v2_lists else None},
                 "weights": weights,
                 "sources": {"dropcatch": bool(dropcatch), "nominet": bool(nominet),
                             "mx": bool(mx), "emd": bool(emd),
                             "namesilo_auction": bool(namesilo_auction)},
                 # без маркера v2_lists этих полей в форме не было — не подменяем их пустотой
                 "tld_allowlist": tld_allowlist if v2_lists else None,
                 "brand_tokens": brand_tokens if v2_lists else None}
        return _settings_page(request, db, emd_draft=emd_sets, status_code=400, draft=draft,
                              form_err=f"Не сохранено ничего: {e}. Наборы ключевых слов — список в "
                                       "формате JSON, пример — под этим полем.")
    return _back("/settings", msg="Настройки сохранены")


@router.post("/settings/reset")
def settings_reset():
    from app.services import settings as st
    st.reset_settings()
    return _back("/settings", msg="Стандартные настройки возвращены")


@router.post("/autopilot/settings")
def autopilot_settings_save(
        autopilot_on: str = Form(""), sweep_interval_min: int = Form(60),
        auto_discovery: str = Form(""), auto_score: str = Form(""), auto_queue: str = Form(""),
        auto_provision: str = Form(""), auto_generate: str = Form(""), auto_publish: str = Form(""),
        auto_check_index: str = Form(""),
        auto_research: str = Form(""), auto_design: str = Form(""), auto_edit: str = Form(""),
        cap_score: int = Form(20), cap_queue: int = Form(10), cap_provision: int = Form(5),
        cap_generate: int = Form(5), cap_publish: int = Form(5), cap_check_index: int = Form(20),
        cap_research: int = Form(5), cap_design: int = Form(3)):
    from app.services.autonomy import update_autonomy
    update_autonomy(
        autopilot_on=bool(autopilot_on), sweep_interval_min=sweep_interval_min,
        auto_discovery=bool(auto_discovery), auto_score=bool(auto_score), auto_queue=bool(auto_queue),
        auto_provision=bool(auto_provision), auto_generate=bool(auto_generate),
        auto_publish=bool(auto_publish), auto_check_index=bool(auto_check_index),
        auto_research=bool(auto_research), auto_design=bool(auto_design), auto_edit=bool(auto_edit),
        cap_score=cap_score, cap_queue=cap_queue, cap_provision=cap_provision,
        cap_generate=cap_generate, cap_publish=cap_publish, cap_check_index=cap_check_index,
        cap_research=cap_research, cap_design=cap_design)
    return _back("/autopilot", msg="Настройки автопилота сохранены")


@router.post("/autopilot/run")
def autopilot_run_action(request: Request):
    from app.services import jobs, orchestrator
    ok = jobs.spawn("sweep", lambda: orchestrator.run_sweep(trigger="manual",
                                                            respect_master=False))
    return _back_here(request, err=None if ok else jobs.busy_msg("Проход уже идёт"))
