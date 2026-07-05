"""Ahrefs API v3 client (MetricsProvider + donor analysis). Transport only.

Base: https://api.ahrefs.com/v3/  |  Auth: Bearer (settings.AHREFS_API_KEY).
Endpoints (site-explorer): domain-rating, backlinks-stats, refdomains, anchors, backlinks.
Cost: paid premium, billed per row — batch and cache into Domain.*.
The Ahrefs MCP in the Claude chat is NOT this; the app needs its own API key.
Verify exact endpoint/field names against your API tier.
"""
from app.integrations.base import BaseClient


class AhrefsClient(BaseClient):
    def __init__(self, api_key: str):
        super().__init__("https://api.ahrefs.com/v3")
        self.api_key = api_key

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}"}

    # --- MetricsProvider (Stage B) ---
    def get_metrics(self, domain: str) -> dict:
        # TODO: domain-rating + refdomains + backlinks-stats + traffic ->
        # {dr, referring_domains, backlinks, organic_traffic}
        raise NotImplementedError

    def get_metrics_batch(self, domains: list[str]) -> dict[str, dict]:
        raise NotImplementedError

    # --- Donor analysis (Stage C) ---
    def get_refdomains(self, domain: str, live_only: bool = True) -> list[dict]:
        """Referring domains; use live filter to compute live_referring_domains."""
        raise NotImplementedError

    def get_anchors(self, domain: str) -> list[dict]:
        """Anchor distribution -> compute spam_anchor_ratio (money/foreign/adult share)."""
        raise NotImplementedError

    def get_backlinks(self, domain: str, live_only: bool = True) -> list[dict]:
        """Backlinks with type/context -> sitewide/footer & dofollow ratios."""
        raise NotImplementedError

    def ping(self) -> bool:
        raise NotImplementedError
