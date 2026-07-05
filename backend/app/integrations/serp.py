"""SERP data client (competitor analysis for content templates). Transport only.

Use a SERP API — DO NOT scrape Google SERPs with a raw browser (CAPTCHA + ToS).
Provider via settings.SEO_DATA_PROVIDER: dataforseo | serpapi.
- DataForSEO SERP: https://api.dataforseo.com/v3/serp/... (Basic auth login/password)
- SerpApi: https://serpapi.com/search (api_key)
Used by M4 to pull top competitors for a keyword and inform page structure.
"""
from app.config import settings
from app.integrations.base import BaseClient


class SerpClient(BaseClient):
    def __init__(self):
        super().__init__()
        self.provider = settings.SEO_DATA_PROVIDER

    def top_results(self, keyword: str, geo: str, lang: str, n: int = 10) -> list[dict]:
        """Top organic results for a keyword in a geo/lang. TODO (branch on provider)."""
        raise NotImplementedError

    def ping(self) -> bool:
        raise NotImplementedError
