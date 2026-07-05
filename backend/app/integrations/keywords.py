"""Keyword data client (volumes for content targeting). Transport only.

Options: Yandex Wordstat API (RU, via Yandex OAuth token) or DataForSEO Keywords or Ahrefs.
Used by M4 to pick angles/keywords per geo/lang before generating content.
"""
from app.config import settings
from app.integrations.base import BaseClient


class KeywordsClient(BaseClient):
    def __init__(self):
        super().__init__()
        self.wordstat_token = settings.YANDEX_WORDSTAT_TOKEN

    def volumes(self, phrases: list[str], geo: str) -> dict[str, int]:
        """Return search volume per phrase. TODO."""
        raise NotImplementedError

    def ping(self) -> bool:
        raise NotImplementedError
