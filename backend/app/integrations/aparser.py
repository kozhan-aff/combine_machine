"""A-Parser client — whois / SERP / keywords / coarse-DR. Transport only.

See docs/api/aparser.md. Local box :9091. Request: POST /API {password, action, data}.
Useful parsers: Net::Whois (M2 free-check), SE::Google/Yandex (M1/M4), WordStat (M4).
"""
import re
from datetime import datetime, timezone

from app.config import settings
from app.integrations.base import BaseClient

# .ru/.рф (TCI): 'created: 2010.11.15'; gTLD: 'Creation Date: 2004-03-15T...'
_RE_RU = re.compile(r"created:\s*(\d{4})\.(\d{2})\.(\d{2})", re.I)
_RE_GTLD = re.compile(r"creation date:\s*(\d{4})-(\d{2})-(\d{2})", re.I)


def _parse_whois_created(text: str) -> datetime | None:
    """Дата регистрации из whois-ответа (.ru или gTLD). Самая ранняя найденная, UTC. None если нет."""
    found = []
    for rx in (_RE_RU, _RE_GTLD):
        for y, mo, dy in rx.findall(text or ""):
            try:
                found.append(datetime(int(y), int(mo), int(dy), tzinfo=timezone.utc))
            except ValueError:
                pass
    return min(found) if found else None


# маркеры «домен свободен» (нет регистрации). Список расширяемый — сверить с живым
# ответом A-Parser Net::Whois на первом прогоне (см. спек §J).
_FREE_MARKERS = ("no entries found", "not found", "no match", "no object found",
                 "available for registration", "нет данных", "not registered")
_REG_MARKERS = ("nserver", "registrar", "person:", "org:", "paid-till", "domain:")


def _parse_whois_available(text: str) -> bool | None:
    """True — домен свободен (нет записи); False — занят; None — не определить."""
    low = (text or "").lower()
    if any(m in low for m in _FREE_MARKERS):
        return True
    if _RE_RU.search(low) or _RE_GTLD.search(low) or any(m in low for m in _REG_MARKERS):
        return False
    return None


class AParserClient(BaseClient):
    def __init__(self):
        super().__init__(settings.APARSER_URL)
        self.password = settings.APARSER_API_KEY

    def _call(self, action: str, data: dict | None = None) -> dict:
        body: dict = {"password": self.password, "action": action}
        if data is not None:
            body["data"] = data
        r = self.request("POST", f"{self.base_url}/API", json=body)
        return r.json()

    def info(self) -> dict:
        """Version + installed parsers list."""
        return self._call("info")

    def ping(self) -> bool:
        return self._call("ping").get("data") == "pong"

    @staticmethod
    def _result_string(res: dict) -> str:
        """oneRequest envelope -> data.resultString ('' если формат иной)."""
        data = res.get("data")
        if isinstance(data, dict):
            return data.get("resultString") or ""
        return ""

    def serp_urls(self, query: str, limit: int = 10) -> list[str]:
        """Топ органической выдачи по ключу (SE::Google), URL в порядке ранга, деду́п.

        A-Parser ходит через ротируемые прокси — пробивает антибот там, где сырой GET
        падает. resultString — URL по строкам. См. docs/api/aparser.md."""
        res = self._call("oneRequest", {"query": query, "parser": "SE::Google",
                                        "configPreset": "default", "preset": "default"})
        seen: set[str] = set()
        out: list[str] = []
        for ln in self._result_string(res).splitlines():
            u = ln.strip()
            if u.startswith("http") and u not in seen:
                seen.add(u)
                out.append(u)
        return out[:limit]

    def fetch_html(self, url: str) -> str | None:
        """Скачать страницу по URL (Net::HTTP через прокси). Возвращает HTML или None.

        resultString = 'СТАТУС\\nзаголовки\\n\\nHTML' — режем по первой пустой строке,
        проверяем 200. JS не рендерит (сырой GET), но для структуры H2/H3 хватает."""
        res = self._call("oneRequest", {"query": url, "parser": "Net::HTTP",
                                        "configPreset": "default", "preset": "default"})
        head, sep, body = self._result_string(res).partition("\n\n")
        if not sep or not head.lstrip().startswith("200"):
            return None
        return body or None

    def whois_probe(self, domain: str) -> dict:
        """Один Net::Whois-вызов -> доступность + дата регистрации.
        available: True свободен / False занят / None не определить. created: дата или None.
        Сетевой/транспортный сбой пробрасывается — ловит вызывающий код (_funnel -> sig["errors"])."""
        res = self._call("oneRequest", {"query": domain, "parser": "Net::Whois",
                                        "configPreset": "default", "preset": "default"})
        text = self._result_string(res)
        return {"available": _parse_whois_available(text), "created": _parse_whois_created(text)}

    def whois_created(self, domain: str) -> datetime | None:
        """Дата регистрации (обёртка над whois_probe для обратной совместимости)."""
        return self.whois_probe(domain)["created"]
