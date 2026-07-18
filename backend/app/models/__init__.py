"""Aggregate model imports so Alembic sees all tables.

alembic/env.py делает РОВНО `from app import models` в чистом процессе — значит именно
ЭТОТ файл (а не транзитивные импорты сервисов) определяет, что видит autogenerate.
Забытый модуль здесь = autogenerate считает его таблицы осиротевшими и предлагает DROP
(аудит 2026-07-14, F22/F23/F29): settings/autonomy/job сюда не попадали, хотя активно
используются кодом — они регистрировались на Base.metadata только случайно, через other
import chains (напр. services/jobs.py), которых обычный тестовый процесс уже натянул."""
from app.models.domain import Domain, AcquisitionOrder
from app.models.site import Site, Page
from app.models.offer import Offer, SiteOffer
from app.models.monitoring import IndexHistory
from app.models.settings import ScoringSettings
from app.models.autonomy import AutonomySettings, AutonomyRun
from app.models.job import JobRun
from app.models.domain_score_log import DomainScoreLog
from app.models import cloudflare  # noqa: F401 — регистрирует 8 mirror-таблиц на Base.metadata

__all__ = [
    "Domain", "AcquisitionOrder", "Site", "Page", "Offer", "SiteOffer", "IndexHistory",
    "ScoringSettings", "AutonomySettings", "AutonomyRun", "JobRun", "DomainScoreLog",
    "cloudflare",
]
