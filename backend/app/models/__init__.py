"""Aggregate model imports so Alembic sees all tables."""
from app.models.domain import Domain, AcquisitionOrder
from app.models.site import Site, Page
from app.models.offer import Offer, SiteOffer
from app.models.monitoring import IndexHistory

__all__ = [
    "Domain", "AcquisitionOrder", "Site", "Page", "Offer", "SiteOffer", "IndexHistory",
]
