"""Ранги доменов (Common Crawl web graph, Majestic Million): бесплатный сигнал authority вместо Ahrefs DR.

Хранится ТОЛЬКО срез по пулу кандидатов (десятки тысяч строк, не 133 млн): services/domain_ranks.py при
загрузке отбирает строки файла по набору доменов из БД. Строка с пустым pagerank (source='cc') —
«проверили: в графе краулинга домена нет» (это данные); отсутствие строки — «не проверяли» (нет данных).
"""
from datetime import datetime
from sqlalchemy import DateTime, Float, Index, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column
from app.db import Base


class DomainRank(Base):
    __tablename__ = "domain_ranks"
    __table_args__ = (UniqueConstraint("domain", "source", name="uq_domain_ranks"),
                      Index("ix_domain_ranks_source", "source"))

    id: Mapped[int] = mapped_column(primary_key=True)
    domain: Mapped[str] = mapped_column(String(255), index=True)       # нижний регистр, прямая нотация
    source: Mapped[str] = mapped_column(String(16))                    # cc | majestic
    harmonic_centrality: Mapped[float | None] = mapped_column(Float)   # cc: harmonicc_val
    pagerank: Mapped[float | None] = mapped_column(Float)              # cc: pr_val (NULL — в графе нет)
    n_hosts: Mapped[int | None] = mapped_column(Integer)               # cc: число хостов домена в графе
    pct: Mapped[float | None] = mapped_column(Float)                   # cc: перцентиль pagerank 0..1 (1 — топ)
    rank_pos: Mapped[int | None] = mapped_column(Integer)              # majestic: GlobalRank
    release: Mapped[str | None] = mapped_column(String(64))            # срез CC / дата Majestic
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
