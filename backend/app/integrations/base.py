"""Shared HTTP helper for integration clients. Transport only."""
import atexit
import threading

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

# Потолок ожидания по Retry-After (S4-07). CF блокирует на окно до 5 минут: спать столько внутри
# синхронного вызова нельзя, поэтому 429 с Retry-After БОЛЬШЕ потолка не ретраится вовсе —
# исключение летит наверх сразу, вызывающий (sync) решает, прервать ли прогон.
MAX_RETRY_AFTER = 120.0

# Connect-таймаут короче read (F8-06): мёртвый хост не должен стоить 3 x 30 с. Явный httpx.Timeout
# от подкласса (CF, Wayback) не трогаем; число -> read/write/pool = оно, connect = min(оно, это).
CONNECT_TIMEOUT = 8.0

# Методы, которые безопасно повторить после обрыва (RFC 9110 §9.2.2). POST/PATCH — нет: платный
# LLM-вызов, создание записи, заказ повторились бы дважды. Нужен повтор — `retry=True` на вызове.
_IDEMPOTENT = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})


def retry_after(exc: BaseException) -> float | None:
    """Retry-After в секундах из 429/503-ответа (только числовая форма; дата — не наш случай)."""
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (429, 503):
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
        if code == 429 or code == 503:
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


def _user_agent() -> str:
    """Контактный User-Agent (F8-06): архивам/реестрам нужен способ написать оператору, а не «python-httpx»."""
    from app.config import settings
    contact = (settings.CONTACT_EMAIL or "").strip()
    return "combine-machine/2.0" + (f" (+mailto:{contact})" if contact else "")


def _as_timeout(timeout: "float | httpx.Timeout") -> httpx.Timeout:
    if isinstance(timeout, httpx.Timeout):
        return timeout
    return httpx.Timeout(timeout, connect=min(float(timeout), CONNECT_TIMEOUT))


# Пул долгоживущих httpx.Client (F8-14, S5-11): keep-alive/TLS reuse между вызовами сервисов,
# которые создают клиента на каждый вызов. Ключ — (класс, base_url, таймаут, UA): смена URL из панели
# («Ключи и сервисы» меняет settings на лету) даёт НОВЫЙ ключ -> новое соединение, старое живёт,
# пока им пользуются соседние потоки, и вытесняется при переполнении (закрывает его GC/atexit —
# явный close() под чужим запросом уронил бы его). Секреты в ключ не входят: они уходят заголовками
# на каждый запрос. httpx.Client потокобезопасен.
_POOL: dict[tuple, httpx.Client] = {}
_POOL_LOCK = threading.Lock()
_POOL_MAX = 16


def _pooled_client(cls: type, base_url: str, timeout: httpx.Timeout) -> httpx.Client:
    ua = _user_agent()
    key = (cls.__name__, base_url, repr(timeout), ua)
    with _POOL_LOCK:
        cl = _POOL.get(key)
        if cl is None or cl.is_closed:
            if len(_POOL) >= _POOL_MAX:
                _POOL.pop(next(iter(_POOL)))      # самый старый; не закрываем — см. выше
            cl = _POOL[key] = httpx.Client(timeout=timeout, follow_redirects=True,
                                           headers={"User-Agent": ua})
        return cl


def close_pool() -> None:
    """Закрыть и забыть все пулированные соединения (выход процесса; изоляция тестов)."""
    with _POOL_LOCK:
        clients = list(_POOL.values())
        _POOL.clear()
    for cl in clients:
        try:
            cl.close()
        except Exception:  # noqa: BLE001 — закрытие best-effort
            pass


atexit.register(close_pool)


class BaseClient:
    # True — httpx.Client берётся из общего пула (см. _pooled_client). Только для клиентов, у которых
    # состояние авторизации уходит заголовками на каждый запрос, а не живёт в cookie-jar клиента.
    POOLED = False

    def __init__(self, base_url: str = "", timeout: "float | httpx.Timeout" = 30.0):
        self.base_url = base_url.rstrip("/")
        t = _as_timeout(timeout)
        if self.POOLED:
            self._client = _pooled_client(type(self), self.base_url, t)
        else:
            self._client = httpx.Client(timeout=t, follow_redirects=True,
                                        headers={"User-Agent": _user_agent()})

    def _request_once(self, method: str, url: str, **kwargs) -> httpx.Response:
        """Одна попытка, БЕЗ ретрая — для вызывающих, которым нужно пересобрать
        request-scoped данные (например, подпись с окном свежести) на каждую попытку
        ретрая самим (см. AaPanelClient._post, S16, аудит 2026-07-18)."""
        resp = self._client.request(method, url, **kwargs)
        resp.raise_for_status()
        return resp

    @retry(stop=stop_after_attempt(3), wait=_wait,
           retry=retry_if_exception(_is_retryable), reraise=True)
    def _request_retrying(self, method: str, url: str, **kwargs) -> httpx.Response:
        return self._request_once(method, url, **kwargs)

    def request(self, method: str, url: str, *, retry: bool | None = None, **kwargs) -> httpx.Response:
        """`retry=None` — по методу: GET/HEAD/PUT/DELETE повторяются (3 попытки), POST/PATCH — нет
        (F8-06: платный LLM, создание записей, заказы не должны исполниться дважды после обрыва).
        Безопасный по смыслу POST (запрос на чтение) включают `retry=True`."""
        if retry is None:
            retry = method.upper() in _IDEMPOTENT
        if retry:
            return self._request_retrying(method, url, **kwargs)
        return self._request_once(method, url, **kwargs)

    def ping(self) -> bool:
        """Lightweight auth/connectivity check. Implement per client."""
        raise NotImplementedError
