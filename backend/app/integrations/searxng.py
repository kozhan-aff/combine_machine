"""SearXNG client — free SERP (self-hosted meta-search). Transport only.

See docs/api/searxng.md. Used for M1 indexed_echo (site:) and M4 competitor SERP.
"""
from app.config import settings
from app.integrations.base import BaseClient


class SearxngClient(BaseClient):
    def __init__(self):
        super().__init__(settings.SEARXNG_URL)

    def search(self, query: str, language: str | None = None, pageno: int = 1) -> list[dict]:
        params = {"q": query, "format": "json", "pageno": pageno}
        if language:
            params["language"] = language
        r = self.request("GET", f"{self.base_url}/search", params=params)
        # note: number_of_results is often 0 (SearXNG quirk) — use len(results)
        return r.json().get("results", [])

    def indexed_echo(self, domain: str) -> bool:
        """M1 indexed_echo: is old content of this domain still in the index?

        # ponytail: degrades to False (no bonus, never a wrong approve) when the box's
        # SearXNG engines are CAPTCHA-blocked. For this .ru/RU project the box MUST have
        # Yandex enabled (settings.yml) — Google/Brave/DDG/Startpage rate-limit the box
        # IP and don't answer `site:`; Yandex indexes .ru best and doesn't block RU.
        """
        results = self.search(f"site:{domain}")
        return any(domain in (r.get("url") or "") for r in results)

    def unresponsive_engines(self, probe: str = "test") -> list:
        """Which engines are currently blocked/erroring (e.g. [['duckduckgo','CAPTCHA']]).
        A green ping() only proves transport — use this to see if SERP actually works."""
        r = self.request("GET", f"{self.base_url}/search",
                         params={"q": probe, "format": "json"})
        return r.json().get("unresponsive_engines", [])

    def ping(self) -> bool:
        # NB: transport/reachability only. Returns True even if every engine is blocked
        # and results are empty — call unresponsive_engines() to check SERP health.
        r = self.request("GET", f"{self.base_url}/search",
                         params={"q": "ping", "format": "json"})
        return "results" in r.json()
