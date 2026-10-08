"""RDAP — транспорт. Сервер зоны берётся из бутстрапа IANA (data.iana.org/rdap/dns.json).

Живой факт 2026-10-01: у домена в pending delete RDAP отдаёт статус 'pending delete' и
`registration` = дата ПЕРВОЙ регистрации (pharmaindustrie.com -> 1998) — возраст до дропа виден.
404 = домена нет (свободен). В бутстрапе нет .mx/.co/.cl/.nz/.de… — для них NoRdap, вызывающий
идёт whois:43 через A-Parser (services/whois.py).

Сбой бутстрапа запоминается на жизнь клиента (клиент живёт один прогон): один warning и статичная
карта `_FALLBACK`. Без этого каждый has_rdap/lookup снова шёл бы в IANA (3 попытки × 20 с) под
общим локом — 12 потоков волны в очереди, и вставали бы даже .mx, которым IANA не нужна.
"""
import logging
import re
import threading
import time
from datetime import datetime, timezone

import httpx

from app.integrations.base import BaseClient, retry_after

logger = logging.getLogger(__name__)

BOOTSTRAP_URL = "https://data.iana.org/rdap/dns.json"

# Серверы зон белого списка, сняты живьём 2026-10-01. Только запасной путь, когда IANA не ответила:
# карта может устареть, основной источник — бутстрап.
_FALLBACK = {
    "com": "https://rdap.verisign.com/com/v1/",
    "net": "https://rdap.verisign.com/net/v1/",
    "org": "https://rdap.publicinterestregistry.org/rdap/",
    "uk": "https://rdap.nominet.uk/uk/",
    "online": "https://rdap.radix.host/rdap/",
    "site": "https://rdap.radix.host/rdap/",
    "xyz": "https://rdap.centralnic.com/xyz/",
    "si": "https://rdap.register.si/",
    "nl": "https://rdap.sidn.nl/",
    "in": "https://rdap.nixiregistry.in/rdap/",
}


class NoRdap(Exception):
    """У зоны нет RDAP-сервера в бутстрапе IANA."""


class RdapThrottled(RuntimeError):
    """Сервер зоны ответил 429 (или просит подождать дольше, чем мы стоим в слоте). Это НЕ падение
    канала: счётчик предохранителя его не считает (services/whois.guarded, soft), домен
    откладывается до следующего прогона (S1-06)."""


# Минимальный интервал между запросами к серверу зоны, с. Живые замеры 2026-10-07: SIDN (.nl)
# отвечает 429 уже на ВТОРОЙ запрос подряд (Retry-After нет), Nominet RDAP (.uk) держит ~1,7 запр/с
# на 12 потоков; Verisign (.com) — 68 запр/с без отказов. Остальные зоны не ограничиваем.
_ZONE_INTERVAL = {"nl": 1.5, "uk": 0.5}
_COOLDOWN_DEFAULT = 3.0      # пауза зоны после 429 без Retry-After
_MAX_WAIT = 15.0             # дольше этого поток волны на cooldown зоны не стоит
_clock = time.monotonic      # тесты подменяют вместе с _sleep
_sleep = time.sleep


_FRACTION = re.compile(r"\.(\d+)")


def _iso(s) -> datetime | None:
    """RDAP-дата -> aware datetime (UTC, если смещения нет) или None.

    Python 3.10 `fromisoformat` не понимает `Z` и берёт дробь секунд только из 3 или 6 цифр, а живой
    CentralNic (.xyz) отдаёт `2014-03-20T12:59:17.0Z` — дробь дополняется (или режется) до 6 цифр.
    Дата без смещения считается UTC: наивная дата дальше уронила бы `now - registered_at` TypeError'ом.
    """
    if not s:
        return None
    txt = str(s).strip().replace("Z", "+00:00")
    txt = _FRACTION.sub(lambda m: "." + (m.group(1) + "000000")[:6], txt, count=1)
    try:
        dt = datetime.fromisoformat(txt)
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


class RdapClient(BaseClient):
    def __init__(self):
        super().__init__("", timeout=20.0)
        self._servers: dict | None = None
        self._lock = threading.Lock()       # волна avail зовёт клиент из 12 потоков
        self._zone_next: dict = {}          # зона -> не раньше этого момента (интервал + cooldown 429)

    def _bootstrap(self) -> dict:
        """{tld: base_url}. В IANA — не больше одного запроса на жизнь клиента: запоминается и
        удачный ответ, и сбой (тогда статичная карта `_FALLBACK`)."""
        with self._lock:
            if self._servers is None:
                try:
                    data = self.request("GET", BOOTSTRAP_URL).json()
                    self._servers = {t.lower(): urls[0] for tlds, urls in data.get("services") or []
                                     for t in tlds if urls}
                except Exception as e:  # noqa: BLE001 — любой сбой IANA: карта, а не шторм повторов
                    logger.warning("RDAP: бутстрап IANA недоступен (%s) — до конца прогона "
                                   "статичная карта зон белого списка", type(e).__name__)
                    self._servers = dict(_FALLBACK)
            return self._servers

    def has_rdap(self, domain: str) -> bool:
        return domain.rsplit(".", 1)[-1].lower() in self._bootstrap()

    def request(self, method: str, url: str, **kwargs):
        """429 не ретраим на транспортном уровне (SIDN: 1+2 с ретраев вхолостую, Retry-After нет) —
        его обрабатывает `lookup` по зоне. Транспортные ошибки и 5xx — прежний ретрай BaseClient."""
        try:
            return self._request_once(method, url, **kwargs)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429:
                raise
        except httpx.TransportError:
            pass
        return super().request(method, url, **kwargs)

    def _slot(self, zone: str, pause: float = 0.0) -> None:
        """Место в очереди зоны: интервал между запросами и cooldown после 429 общие на всех
        потоков. Ждать дольше `_MAX_WAIT` не станем — отказ (`RdapThrottled`), слот не засыпает."""
        with self._lock:
            now = _clock()
            if pause:
                self._zone_next[zone] = max(self._zone_next.get(zone, 0.0), now + pause)
            at = max(self._zone_next.get(zone, 0.0), now)
            if at - now > _MAX_WAIT:
                raise RdapThrottled(f"RDAP .{zone}: сервер просит подождать {at - now:.0f} с")
            self._zone_next[zone] = at + _ZONE_INTERVAL.get(zone, 0.0)
        if at > now:
            _sleep(at - now)

    def lookup(self, domain: str) -> dict:
        zone = domain.rsplit(".", 1)[-1].lower()
        base = self._bootstrap().get(zone)
        if base is None:
            raise NoRdap(domain)
        url, pause = base.rstrip("/") + "/domain/" + domain, 0.0
        for attempt in range(2):
            self._slot(zone, pause)
            try:
                r = self.request("GET", url, headers={"Accept": "application/rdap+json"})
                break
            except httpx.HTTPStatusError as e:
                code = e.response.status_code
                if code == 404:
                    return {"exists": False, "status": [], "registered_at": None}
                if code != 429:
                    raise
                ra = retry_after(e)
                pause = ra if ra is not None else _COOLDOWN_DEFAULT
                if attempt == 1 or pause > _MAX_WAIT:
                    with self._lock:           # соседи по зоне тоже подождут
                        self._zone_next[zone] = max(self._zone_next.get(zone, 0.0), _clock() + pause)
                    raise RdapThrottled(f"RDAP .{zone}: 429, повтор через {pause:.0f} с") from e
        d = r.json()
        reg = next((e.get("eventDate") for e in d.get("events") or []
                    if e.get("eventAction") == "registration"), None)
        return {"exists": True, "status": [str(s).lower() for s in d.get("status") or []],
                "registered_at": _iso(reg)}

    def ping(self) -> bool:
        """Живой пинг — напрямую в IANA, мимо запомненной карты: иначе /diag зеленел бы на
        `_FALLBACK` при лежащем бутстрапе."""
        return bool(self.request("GET", BOOTSTRAP_URL).json().get("services"))
