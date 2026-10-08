"""Shared HTTP helper for integration clients. Transport only."""
import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

# Потолок ожидания по Retry-After (S4-07). CF блокирует на окно до 5 минут: спать столько внутри
# синхронного вызова нельзя, поэтому 429 с Retry-After БОЛЬШЕ потолка не ретраится вовсе —
# исключение летит наверх сразу, вызывающий (sync) решает, прервать ли прогон.
MAX_RETRY_AFTER = 120.0


def retry_after(exc: BaseException) -> float | None:
    """Retry-After в секундах из 429-ответа (только числовая форма; дата — не наш случай)."""
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429:
        try:
            return max(0.0, float(exc.response.headers.get("Retry-After", "")))
        except ValueError:
            return None
    return None


def _is_retryable(exc: BaseException) -> bool:
    """Ретраим только транспортные ошибки и серверные 5xx/429; 4xx (кроме 429) — нет:
    повторять 404/401/400 бессмысленно, а reraise=True отдаёт наружу исходное исключение,
    а не RetryError. 429 с Retry-After дольше потолка — тоже нет: ретраи 1 с + 2 с до блокировки
    на окно только жгут запросы (S4-07)."""
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code == 429:
            ra = retry_after(exc)
            return ra is None or ra <= MAX_RETRY_AFTER
        return code >= 500
    return False


class NotModified(Exception):
    """Источник ответил 304: файл с прошлого забора не менялся (условный GET, S1-11)."""


def conditional_get(client, url: str, **kwargs):
    """GET с If-None-Match/If-Modified-Since по `client.validators` ({etag, last_modified} с прошлого
    успешного забора, ставит вызывающий). 304 -> NotModified. Новые валидаторы кладёт обратно в
    `client.validators` — вызывающий сохранит их ПОСЛЕ успешной обработки (иначе упавший прогон
    «потерял» бы день: следующий получил бы 304 на файл, который так и не разобрали)."""
    v = getattr(client, "validators", None) or {}
    headers = dict(kwargs.pop("headers", None) or {})
    if v.get("etag"):
        headers["If-None-Match"] = v["etag"]
    if v.get("last_modified"):
        headers["If-Modified-Since"] = v["last_modified"]
    try:
        r = client.request("GET", url, headers=headers, **kwargs)
    except httpx.HTTPStatusError as e:   # реальный транспорт: raise_for_status() превращает 304 в исключение
        if e.response.status_code == 304:
            raise NotModified(url) from e
        raise
    if r.status_code == 304:
        raise NotModified(url)
    hdr = getattr(r, "headers", None) or {}
    client.validators = {"etag": hdr.get("ETag"), "last_modified": hdr.get("Last-Modified")}
    return r


_backoff = wait_exponential(multiplier=1, max=10)


def _wait(retry_state) -> float:
    """Экспоненциальная пауза, но не короче Retry-After, если сервер его назвал (S4-07)."""
    base = _backoff(retry_state)
    ra = retry_after(retry_state.outcome.exception()) if retry_state.outcome else None
    return max(base, min(ra, MAX_RETRY_AFTER)) if ra is not None else base


class BaseClient:
    def __init__(self, base_url: str = "", timeout: "float | httpx.Timeout" = 30.0):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(timeout=timeout, follow_redirects=True)

    def _request_once(self, method: str, url: str, **kwargs) -> httpx.Response:
        """Одна попытка, БЕЗ ретрая — для вызывающих, которым нужно пересобрать
        request-scoped данные (например, подпись с окном свежести) на каждую попытку
        ретрая самим (см. AaPanelClient._post, S16, аудит 2026-07-18)."""
        resp = self._client.request(method, url, **kwargs)
        resp.raise_for_status()
        return resp

    @retry(stop=stop_after_attempt(3), wait=_wait,
           retry=retry_if_exception(_is_retryable), reraise=True)
    def request(self, method: str, url: str, **kwargs) -> httpx.Response:
        return self._request_once(method, url, **kwargs)

    def ping(self) -> bool:
        """Lightweight auth/connectivity check. Implement per client."""
        raise NotImplementedError
