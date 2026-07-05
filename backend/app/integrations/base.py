"""Shared HTTP helper for integration clients. Transport only."""
import httpx
from tenacity import retry, stop_after_attempt, wait_exponential


class BaseClient:
    def __init__(self, base_url: str = "", timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(timeout=timeout, follow_redirects=True)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, max=10))
    def request(self, method: str, url: str, **kwargs) -> httpx.Response:
        resp = self._client.request(method, url, **kwargs)
        resp.raise_for_status()
        return resp

    def ping(self) -> bool:
        """Lightweight auth/connectivity check. Implement per client."""
        raise NotImplementedError
