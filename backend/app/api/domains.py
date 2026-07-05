"""Domains router — scored shortlist for the panel (M1 output).

    GET /domains?status=scored&min_score=0.7  -> candidate domains, best score first.
"""
from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.db import get_session
from app.models.domain import Domain

router = APIRouter(prefix="/domains", tags=["domains"])


@router.get("/")
def list_domains(status: str | None = None, min_score: float | None = None,
                 limit: int = 100, db: Session = Depends(get_session)):
    limit = max(1, min(limit, 1000))   # серверный кап: не тянуть всю таблицу в память
    stmt = select(Domain)
    if status:
        stmt = stmt.where(Domain.status == status)
    if min_score is not None:
        stmt = stmt.where(Domain.score >= min_score)
    stmt = stmt.order_by(Domain.score.desc().nulls_last()).limit(limit)
    rows = db.execute(stmt).scalars().all()
    return [
        {
            "id": d.id, "domain": d.domain, "status": d.status,
            "score": float(d.score) if d.score is not None else None,
            "dr": float(d.dr) if d.dr is not None else None,
            "referring_domains": d.referring_domains,
            "clean": d.clean,
        }
        for d in rows
    ]
