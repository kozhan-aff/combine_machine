"""RKN registry check. See docs/api/rkn.md.

Reject domains in the Russian blocked registry. Source: settings.RKN_SOURCE_URL
(antizapret export = plain UTF-8 domain list; z-i mirror froze 2025-10). Loaded once,
refreshed daily, membership checked locally (domain + parent suffixes).
"""
import time
from app.config import settings
from app.integrations.base import BaseClient

_REFRESH_SEC = 24 * 3600
_MIN_DUMP_LINES = 1000   # реестр РКН — сотни тысяч доменов; меньше = пустой/обрезанный ответ


def _normalize(domain: str) -> str:
    d = domain.strip().lower().lstrip("*.").rstrip(".")
    return d


def _match(domain: str, blocked: set[str]) -> bool:
    """Listed if the domain itself or any of its parent suffixes is blocked."""
    labels = _normalize(domain).split(".")
    for i in range(len(labels) - 1):        # down to the last two labels
        if ".".join(labels[i:]) in blocked:
            return True
    return False


class RknClient(BaseClient):
    # class-level cache: the dump is loaded once and shared across instances
    # (score_pending makes a client per domain — don't re-download the dump each time).
    _blocked: set[str] = set()
    _loaded_at: float | None = None

    def __init__(self):
        super().__init__()
        self.source_url = settings.RKN_SOURCE_URL

    def _ensure_loaded(self) -> None:
        now = time.monotonic()
        if RknClient._loaded_at is None or (now - RknClient._loaded_at) > _REFRESH_SEC:
            r = self.request("GET", self.source_url)
            blocked = {_normalize(ln) for ln in r.text.splitlines()
                       if ln.strip() and "." in ln}
            # sanity: пустой/обрезанный ответ (HTTP 200, но мало строк) НЕ кэшируем на сутки
            # и не выключаем проверку молча. Есть валидный кэш — оставляем его; нет — падаем.
            if len(blocked) < _MIN_DUMP_LINES:
                if RknClient._loaded_at is None:
                    raise RuntimeError(
                        f"RKN dump подозрительно мал ({len(blocked)} строк) — не кэширую, "
                        f"проверка не должна молча выключаться")
                return  # оставляем прежний валидный кэш до следующего refresh
            RknClient._blocked = blocked
            RknClient._loaded_at = now

    def is_listed(self, domain: str) -> bool:
        self._ensure_loaded()
        return _match(domain, RknClient._blocked)

    def ping(self) -> bool:
        # source reachable? fetch first bytes, don't pull the whole dump
        r = self.request("GET", self.source_url, headers={"Range": "bytes=0-1023"})
        return len(r.content) > 0


if __name__ == "__main__":  # tiny self-check for the matcher (no network)
    b = {"example.com", "sub.blocked.ru"}
    assert _match("example.com", b) is True
    assert _match("www.example.com", b) is True          # parent blocked -> child listed
    assert _match("sub.blocked.ru", b) is True
    assert _match("blocked.ru", b) is False               # child blocked != parent listed
    assert _match("clean.org", b) is False
    print("rkn _match ok")
