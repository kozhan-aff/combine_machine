"""Google Web Risk Lookup API — транспорт. Замена Safe Browsing: тот «for non-commercial use
only», наш affiliate-бизнес коммерческий (docs/v2/research/metrics-history.md).

Один URL на запрос. Чистый -> {}, угроза -> {"threat": {"threatTypes": [...]}}. Бесплатно до
100 тыс. вызовов в месяц. КЛЮЧ — В ЗАГОЛОВКЕ X-Goog-Api-Key, не в query: ключ в URL утекал бы в
текст HTTPStatusError, в логи и в /diag.

Формат ответа — ПО ДОКУМЕНТАЦИИ, живьём не снят (ключа нет; живые образцы — Задача 17, инвариант 7).
Поэтому разбор строгий: незнакомая форма — ValueError (W3 пишет `webrisk:ValueError`, домен
«вслепую»), а не [] — тихое «чисто» отправило бы непроверенный домен в пакет.
"""
from app.config import settings
from app.integrations.base import BaseClient

URL = "https://webrisk.googleapis.com/v1/uris:search"
THREATS = ("MALWARE", "SOCIAL_ENGINEERING", "UNWANTED_SOFTWARE")


class WebRiskClient(BaseClient):
    def __init__(self, api_key: str | None = None):
        super().__init__("", timeout=20.0)
        self.api_key = settings.WEBRISK_API_KEY if api_key is None else api_key

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def threats(self, domain: str) -> list[str]:
        params = [("threatTypes", t) for t in THREATS] + [("uri", f"http://{domain}/")]
        r = self.request("GET", URL, params=params, headers={"X-Goog-Api-Key": self.api_key})
        data = r.json()
        if not isinstance(data, dict):
            raise ValueError("webrisk: ответ не объект")
        threat = data.get("threat")
        if threat is None:
            return []                                   # {} — чистый URL
        if not isinstance(threat, dict) or not isinstance(threat.get("threatTypes"), list):
            raise ValueError("webrisk: незнакомая форма 'threat'")
        return [str(t) for t in threat["threatTypes"]]

    def ping(self) -> bool:
        return self.configured and self.threats("example.com") == []
