"""Aggregate model imports so Alembic sees all tables."""
from app.models.domain import Domain, AcquisitionOrder
from app.models.site import Site, Page
from app.models.offer import Offer, SiteOffer
from app.models.monitoring import IndexHistory
from app.models import cloudflare  # noqa: F401 — регистрирует 8 mirror-таблиц на Base.metadata

__all__ = [
    "Domain", "AcquisitionOrder", "Site", "Page", "Offer", "SiteOffer", "IndexHistory",
    "cloudflare",
]
