"""OpenPageRank (DomCop) client — free coarse DR-proxy. Transport only.

See docs/api/openpagerank.md. Replaces Ahrefs DR for the M1 pre-filter gate.
Gives a 0..10 authority number only — NOT a backlink profile.
"""
from app.config import settings
from app.integrations.base import BaseClient


class OpenPageRankClient(BaseClient):
    def __init__(self):
        super().__init__("https://openpagerank.com/api/v1.0")
        self.api_key = settings.OPENPAGERANK_API_KEY

    def _headers(self) -> dict:
        return {"API-OPR": self.api_key}

    def get_page_rank(self, domains: list[str]) -> dict[str, float]:
        """domain -> page_rank_decimal (0..10). Batch up to 100 per call."""
        params = [("domains[]", d) for d in domains[:100]]
        r = self.request("GET", f"{self.base_url}/getPageRank",
                         params=params, headers=self._headers())
        out: dict[str, float] = {}
        for row in r.json().get("response", []):
            if row.get("status_code") == 200:
                out[row["domain"]] = float(row.get("page_rank_decimal") or 0.0)
        return out

    def ping(self) -> bool:
        if not self.api_key:
            return False  # needs a free API-OPR key
        r = self.request("GET", f"{self.base_url}/getPageRank",
                         params=[("domains[]", "example.com")], headers=self._headers())
        return r.json().get("status_code") == 200
