"""A-Parser client — whois / SERP / keywords / coarse-DR. Transport only.

See docs/api/aparser.md. Local box :9091. Request: POST /API {password, action, data}.
Useful parsers: Net::Whois (M2 free-check), SE::Google/Yandex (M1/M4), WordStat (M4).
"""
from app.config import settings
from app.integrations.base import BaseClient


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
