"""Minimal HTMX/Jinja panel — the M1 shortlist view (BUILD_SPEC §2).

Server-rendered, no JS deps: filters are a plain GET form (full-page reload).
# ponytail: no HTMX yet — add hx-get on the form if live refresh is wanted.
"""
from pathlib import Path
from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from app.db import get_session
from app.models.domain import Domain

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))
router = APIRouter()

# human curation from the shortlist. 'purchased' = the operator bought it by hand — that
# click IS the money gate (no provider order is fired here). See CLAUDE.md rule 2.
_MANUAL_STATUSES = {"approved", "rejected", "purchased"}


@router.get("/", response_class=HTMLResponse)
def panel(request: Request, status: str | None = None, min_score: float | None = None,
          limit: int = 200, db: Session = Depends(get_session)):
    stmt = select(Domain)
    if status:
        stmt = stmt.where(Domain.status == status)
    if min_score is not None:
        stmt = stmt.where(Domain.score >= min_score)
    stmt = stmt.order_by(Domain.score.desc().nulls_last()).limit(limit)
    rows = db.execute(stmt).scalars().all()
    counts = dict(db.execute(select(Domain.status, func.count()).group_by(Domain.status)).all())
    return templates.TemplateResponse(request, "panel.html", {   # new Starlette signature
        "rows": rows, "counts": counts, "total": sum(counts.values()),
        "f_status": status or "", "f_min_score": "" if min_score is None else min_score,
        "f_limit": limit,
    })


# --- actions (POST -> redirect back; no-JS friendly) -------------------------
# ponytail: run synchronously — fine for one operator. Move to the worker/queue if a
# batch (Wayback ~5s/domain) starts making the request hang.
@router.post("/run/discovery")
def run_discovery_action():
    from app.services import discovery
    discovery.run_discovery()
    return RedirectResponse("/", status_code=303)


@router.post("/run/score")
def run_score_action(n: int = Form(5)):
    from app.services import scoring
    scoring.score_pending(limit=n)
    return RedirectResponse("/", status_code=303)


@router.post("/domains/{domain_id}/set-status")
def set_status_action(domain_id: int, status: str = Form(...), db: Session = Depends(get_session)):
    if status in _MANUAL_STATUSES:      # guard: only human-curation transitions allowed here
        d = db.get(Domain, domain_id)
        if d:
            d.status = status
            db.commit()
    return RedirectResponse("/", status_code=303)
