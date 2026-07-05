"""Shared HTTP helper for integration clients. Transport only."""
import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential


def _is_retryable(exc: BaseException) -> bool:
    """Ретраим только транспортные ошибки и серверные 5xx/429; 4xx (кроме 429) — нет:
    повторять 404/401/400 бессмысленно, а reraise=True отдаёт наружу исходное исключение,
    а не RetryError."""
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return code == 429 or code >= 500
    return False


class BaseClient:
    def __init__(self, base_url: str = "", timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(timeout=timeout, follow_redirects=True)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, max=10),
           retry=retry_if_exception(_is_retryable), reraise=True)
    def request(self, method: str, url: str, **kwargs) -> httpx.Response:
        resp = self._client.request(method, url, **kwargs)
        resp.raise_for_status()
        return resp

    def ping(self) -> bool:
        """Lightweight auth/connectivity check. Implement per client."""
        raise NotImplementedError
