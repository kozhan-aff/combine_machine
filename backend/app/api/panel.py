"""HTML-панель — пошаговый пульт конвейера (оффер → M1 → выкуп → M3 → M4 → M5).

Server-rendered Jinja, формы POST -> redirect (no-JS friendly). Результат действия
передаётся назад через ?msg=/?err= (без сессий). Тяжёлые действия синхронны —
нормально для одного оператора; при батчах уводить в worker.

Гейты (PLAN §2) живут в сервисах; панель их только отражает:
  - деньги: 'purchased' ставит ЧЕЛОВЕК кнопкой (никакого авто-заказа);
  - редактура: publish берёт только 'edited', draft -> edited делает ЧЕЛОВЕК в редакторе.
"""
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_session
from app.models.domain import Domain
from app.models.offer import Offer, SiteOffer
from app.models.site import Site, Page

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))
router = APIRouter()

# ручная курация из шортлиста. 'purchased' = оператор купил домен руками — этот клик
# и ЕСТЬ money-gate (заказ провайдеру отсюда не уходит). См. CLAUDE.md, правило 2.
_MANUAL_STATUSES = {"approved", "rejected", "purchased"}


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
        steps.append({"href": "/domains?status=discovered", "text": f"{dc['discovered']} доменов ждут скоринга — запусти ▶ Score (лучшие по RD пойдут первыми)."})
    if dc.get("scored"):
        steps.append({"href": "/domains?status=scored", "text": f"{dc['scored']} отскорено — просмотри и реши ✓ approve / ✗ reject."})
    if dc.get("approved"):
        steps.append({"href": "/domains?status=approved", "text": f"{dc['approved']} одобрено — купи домен руками у провайдера, потом отметь 🛒 куплен."})
    purchased_no_site = db.execute(
        select(Domain).where(Domain.status == "purchased")
        .where(~Domain.id.in_(select(Site.domain_id)))).scalars().all()
    if purchased_no_site:
        steps.append({"href": "/domains?status=purchased", "text": f"{len(purchased_no_site)} купленных без сайта — нажми «создать сайт»."})
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


# ============================================================================
# ЭКРАНЫ
# ============================================================================
@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_session)):
    dc = _domain_counts(db)
    return templates.TemplateResponse(request, "dashboard.html", {
        "active": "dash",
        "dc": dc, "d_total": sum(dc.values()),
        "pc": _page_counts(db),
        "offers_active": db.scalar(select(func.count()).select_from(Offer).where(Offer.active.is_(True))) or 0,
        "offers_total": db.scalar(select(func.count()).select_from(Offer)) or 0,
        "sites": _sites_overview(db),
        "steps": _next_steps(db),
    })


@router.get("/domains", response_class=HTMLResponse)
def domains_view(request: Request, status: str | None = None, min_score: float | None = None,
                 limit: int = 200, db: Session = Depends(get_session)):
    stmt = select(Domain)
    if status:
        stmt = stmt.where(Domain.status == status)
    if min_score is not None:
        stmt = stmt.where(Domain.score >= min_score)
    stmt = stmt.order_by(Domain.score.desc().nulls_last(),
                         Domain.referring_domains.desc().nulls_last()).limit(limit)
    rows = db.execute(stmt).scalars().all()
    counts = _domain_counts(db)
    site_by_domain = dict(db.execute(select(Site.domain_id, Site.id)).all())
    return templates.TemplateResponse(request, "domains.html", {
        "active": "domains",
        "rows": rows, "counts": counts, "total": sum(counts.values()),
        "site_by_domain": site_by_domain,
        "f_status": status or "", "f_min_score": "" if min_score is None else min_score,
        "f_limit": limit,
    })


@router.get("/diag", response_class=HTMLResponse)
def diag_view(request: Request):
    from app.services.diagnostics import run_diagnostics, PING_TIMEOUT
    checks = run_diagnostics()
    ok = sum(1 for c in checks if c["status"] == "ok")
    return templates.TemplateResponse(request, "diag.html", {
        "active": "diag", "checks": checks, "ok": ok, "total": len(checks),
        "timeout": PING_TIMEOUT,
        "repo": settings.GITHUB_REPO, "can_pull": bool(settings.GITHUB_TOKEN),
    })


@router.get("/offers", response_class=HTMLResponse)
def offers_view(request: Request, db: Session = Depends(get_session)):
    rows = db.execute(select(Offer).order_by(Offer.id)).scalars().all()
    return templates.TemplateResponse(request, "offers.html", {"active": "offers", "rows": rows})


@router.get("/queue", response_class=HTMLResponse)
def queue_view(request: Request):
    from app.services import acquisition
    orders = acquisition.list_orders()
    return templates.TemplateResponse(request, "queue.html", {
        "active": "queue", "orders": orders,
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
@router.post("/run/discovery")
def run_discovery_action():
    from app.services import discovery
    try:
        n = discovery.run_discovery()
        return _back("/domains", msg=f"Discovery: +{n} новых доменов из фида")
    except Exception as e:  # noqa: BLE001 — любая ошибка интеграции -> баннер, не 500
        return _back("/domains", err=f"discovery: {e}")


@router.post("/run/score")
def run_score_action(n: int = Form(5)):
    from app.services import scoring
    try:
        k = scoring.score_pending(limit=n)
        return _back("/domains", msg=f"Score: обработано {k} доменов")
    except Exception as e:  # noqa: BLE001
        return _back("/domains", err=f"score: {e}")


@router.post("/domains/{domain_id}/score")
def score_one_action(domain_id: int):
    from app.services import scoring
    try:
        out = scoring.score_domain(domain_id)
        return _back("/domains", msg=f"скор: {out.get('domain', domain_id)} -> "
                                     f"{out.get('status')} ({out.get('score')})")
    except Exception as e:  # noqa: BLE001
        return _back("/domains", err=f"score #{domain_id}: {e}")


@router.post("/domains/{domain_id}/set-status")
def set_status_action(domain_id: int, status: str = Form(...), db: Session = Depends(get_session)):
    if status in _MANUAL_STATUSES:      # guard: только ручные переходы курации
        d = db.get(Domain, domain_id)
        if d:
            d.status = status
            db.commit()
    return _back("/domains")


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
def queue_confirm_action(order_id: int):
    from app.services import acquisition
    try:
        acquisition.confirm_order(order_id)
        return _back("/queue", msg=f"Заказ #{order_id} подтверждён человеком (гейт открыт). Можно отправлять.")
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
            return _back("/queue", err=f"заказ #{order_id}: {r.get('error') or 'провайдер не готов (нужны login-креды)'}")
        return _back("/queue", msg=f"Заказ #{order_id} отправлен провайдеру — статус {r.get('status')}.")
    except Exception as e:  # noqa: BLE001
        return _back("/queue", err=f"отправка: {e}")


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
@router.post("/admin/pull")
def git_pull_action():
    import subprocess
    if not settings.GITHUB_TOKEN:
        return _back("/diag", err="GITHUB_TOKEN не задан в .env — нечем авторизовать git pull")
    tok = settings.GITHUB_TOKEN
    url = f"https://x-access-token:{tok}@github.com/{settings.GITHUB_REPO}.git"
    scrub = lambda s: s.replace(tok, "***")  # никогда не светить токен в баннере
    try:
        pull = subprocess.run(
            ["git", "-C", "/repo", "-c", "safe.directory=/repo", "pull", "--ff-only", url, "main"],
            capture_output=True, text=True, timeout=120)
        if pull.returncode != 0:
            return _back("/diag", err=f"git pull: {scrub((pull.stderr or pull.stdout).strip())[:300]}")
        head = (pull.stdout.strip().splitlines() or ["ok"])[-1]
        # миграции идемпотентны; код подхватит uvicorn --reload (следит за /app)
        mig = subprocess.run(["alembic", "upgrade", "head"], cwd="/app",
                             capture_output=True, text=True, timeout=120)
        warn = "" if mig.returncode == 0 else f" ⚠ alembic: {scrub(mig.stderr.strip())[:150]}"
        return _back("/diag", msg=f"Обновлено: {scrub(head)[:200]}{warn}")
    except FileNotFoundError:
        return _back("/diag", err="git не установлен в контейнере — пересобери образ (docker compose build)")
    except Exception as e:  # noqa: BLE001
        return _back("/diag", err=f"update: {scrub(str(e))[:200]}")
