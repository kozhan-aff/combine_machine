"""SearXNG client — free SERP (self-hosted meta-search). Transport only.

See docs/api/searxng.md. Used for M1 indexed_echo (site:) and M4 competitor SERP.
"""
from urllib.parse import urlparse

from app.config import settings
from app.integrations.base import BaseClient


def host_matches(url: str | None, domain: str) -> bool:
    """True iff url's host IS domain or a subdomain of it. Substring match ('domain in url')
    false-positives on hostile URLs like https://mydomain.ru.evil.com/, so compare hostnames."""
    host = (urlparse(url or "").hostname or "").lower()
    domain = domain.lower()
    return host == domain or host.endswith("." + domain)


class SearxngClient(BaseClient):
    def __init__(self):
        super().__init__(settings.SEARXNG_URL)

    def search_full(self, query: str, language: str | None = None, pageno: int = 1) -> dict:
        """Весь JSON ответа /search: и `results`, и `unresponsive_engines` — ОДНИМ запросом.

        SearXNG кладёт здоровье движков в ТОТ ЖЕ ответ, что и результаты (живая сверка с боксом
        192.168.1.77:8080, 2026-07-14: ключи `results` + `unresponsive_engines`, напр.
        `[['brave','too many requests'], ['startpage','CAPTCHA']]`). Кто судит пустую выдачу,
        обязан спрашивать здоровье У ТОГО ЖЕ ЗАПРОСА: движки блокируют ИМЕННО оператор `site:`,
        отвечая на обычный запрос как ни в чём не бывало, — отдельный проб-запрос («жив ли движок
        вообще?») ответил бы «все живы» ровно тогда, когда `site:` словил CAPTCHA.
        """
        params = {"q": query, "format": "json", "pageno": pageno}
        if language:
            params["language"] = language
        r = self.request("GET", f"{self.base_url}/search", params=params)
        # note: number_of_results is often 0 (SearXNG quirk) — use len(results)
        return r.json()

    def search(self, query: str, language: str | None = None, pageno: int = 1) -> list[dict]:
        return self.search_full(query, language=language, pageno=pageno).get("results", [])

    def indexed_echo(self, domain: str) -> bool:
        """M1 indexed_echo: is old content of this domain still in the index?

        # ponytail: degrades to False (no bonus, never a wrong approve) when the box's
        # SearXNG engines are CAPTCHA-blocked. For this .ru/RU project the box MUST have
        # Yandex enabled (settings.yml) — Google/Brave/DDG/Startpage rate-limit the box
        # IP and don't answer `site:`; Yandex indexes .ru best and doesn't block RU.
        """
        results = self.search(f"site:{domain}")
        return any(host_matches(r.get("url"), domain) for r in results)

    def unresponsive_engines(self, probe: str = "test") -> list:
        """Which engines are currently blocked/erroring (e.g. [['duckduckgo','CAPTCHA']]).
        A green ping() only proves transport — use this to see if SERP actually works.

        Здоровье ПРОБНОГО запроса, не твоего: судить по нему пустую выдачу `site:` нельзя
        (см. search_full). Для проверки индексации M5 берёт `unresponsive_engines` из ответа
        на СВОЙ запрос.
        """
        return self.search_full(probe).get("unresponsive_engines", [])

    def ping(self) -> bool:
        # NB: transport/reachability only. Returns True even if every engine is blocked
        # and results are empty — call unresponsive_engines() to check SERP health.
        r = self.request("GET", f"{self.base_url}/search",
                         params={"q": "ping", "format": "json"})
        return "results" in r.json()
