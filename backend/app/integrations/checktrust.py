"""Alternative MetricsProvider (checktrust.ru or similar). Cheaper bulk donor checks.

Same interface as AhrefsClient. Fill in once the provider is chosen.
"""
from app.integrations.base import BaseClient


class CheckTrustClient(BaseClient):
    def __init__(self, api_key: str):
        super().__init__()
        self.api_key = api_key

    def get_metrics(self, domain: str) -> dict:
        raise NotImplementedError

    def get_metrics_batch(self, domains: list[str]) -> dict[str, dict]:
        raise NotImplementedError

    def ping(self) -> bool:
        raise NotImplementedError
