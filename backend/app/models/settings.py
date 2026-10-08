"""Рантайм-настройки скоринга (single-row, id=1). Дефолты — в scoring_config.py.

Пороги воронки редактируются на /settings; сервис settings.py читает/пишет эту строку.
"""
from datetime import datetime
from sqlalchemy import Integer, Numeric, DateTime, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from app.db import Base


class ScoringSettings(Base):
    __tablename__ = "scoring_settings"

    id: Mapped[int] = mapped_column(primary_key=True)                # всегда 1
    min_referring_domains: Mapped[int] = mapped_column(Integer, default=1)
    min_age_years: Mapped[float] = mapped_column(Numeric, default=3.0)
    approve_at: Mapped[float] = mapped_column(Numeric, default=0.70)
    manual_review_at: Mapped[float] = mapped_column(Numeric, default=0.40)
    max_whois_per_run: Mapped[int] = mapped_column(Integer, default=200)  # кап whois-вызовов за прогон
    # ЛЕГАСИ v1 (капча A-Parser Rank::Ahrefs): в v2 W4 — Ahrefs API с капом max_links_per_run, см. 0025
    max_ahrefs_per_run: Mapped[int] = mapped_column(Integer, default=50)
    sources_enabled: Mapped[dict] = mapped_column(JSONB, default=dict)
    # веса критериев оценки донора (семь компонентов v2 — scoring_config.WEIGHTS).
    # Были зашиты в scoring_config.WEIGHTS — оператор видел, ПО ЧЕМУ его судят, но не мог
    # изменить НИ ОДИН вес (жалоба 2026-07-13). Сумма не обязана быть 1.0: compute_score
    # нормирует её сам, иначе один сдвинутый ползунок ломал бы шкалу 0..1.
    weights: Mapped[dict] = mapped_column(JSONB, default=dict)
    # v2 (миграция 0025)
    min_dr: Mapped[float] = mapped_column(Numeric, default=5)
    tld_allowlist: Mapped[list] = mapped_column(JSONB, default=list)
    brand_tokens: Mapped[list] = mapped_column(JSONB, default=list)
    emd_sets: Mapped[list] = mapped_column(JSONB, default=list)
    max_links_per_run: Mapped[int] = mapped_column(Integer, default=500)
    max_deep_per_run: Mapped[int] = mapped_column(Integer, default=20)
    spam_anchor_max: Mapped[float] = mapped_column(Numeric, default=0.2)
    # пол остатка units Ahrefs (решение оператора Р3): ниже — W4/W6 не тратят units
    units_floor: Mapped[int] = mapped_column(Integer, default=300000)
    # v2 G4 (миграция 0030): {"max_candidates_per_run": int, "name_filters": {...}, "source_state": {...}}.
    # Один JSONB вместо россыпи колонок: резерв без DR (S1-01), фильтры имени (S1-10), валидаторы
    # условного GET источников (S1-11 — служебное, оператор не правит).
    discovery_opts: Mapped[dict] = mapped_column(JSONB, default=dict)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True),
                                                        server_default=func.now(), onupdate=func.now())
