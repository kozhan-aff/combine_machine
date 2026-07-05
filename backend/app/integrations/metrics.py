"""MetricsProvider interface + factory.

Source of domain metrics is pluggable because it is a budget decision:
Ahrefs API is powerful but paid per-row (expensive); checktrust and similar are
cheaper for bulk donor checks. Select via settings.METRICS_PROVIDER.

NOTE: the Ahrefs MCP connector available in the Claude chat is NOT usable here —
this app needs its own Ahrefs API key/subscription.
"""
from typing import Protocol
from app.config import settings


class MetricsProvider(Protocol):
    def get_metrics(self, domain: str) -> dict: ...
    def get_metrics_batch(self, domains: list[str]) -> dict[str, dict]: ...


def get_metrics_provider() -> MetricsProvider:
    if settings.METRICS_PROVIDER == "ahrefs":
        from app.integrations.ahrefs import AhrefsClient
        return AhrefsClient(settings.AHREFS_API_KEY)
    if settings.METRICS_PROVIDER == "checktrust":
        from app.integrations.checktrust import CheckTrustClient
        return CheckTrustClient(settings.CHECKTRUST_API_KEY)
    raise ValueError(f"Unknown METRICS_PROVIDER: {settings.METRICS_PROVIDER}")
