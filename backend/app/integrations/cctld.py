"""cctld.ru — реестр освобождающихся .ru/.рф (авторитетный сырой список). Транспорт + парс.

Без RD-сигнала. URL/формат выверить на живой странице cctld.ru/service/dellist/ —
парсер устойчив к разметке (тянет домен-подобные токены .ru/.рф/.su из текста).
"""
import re
from app.integrations.base import BaseClient

_DOM = re.compile(
    r"\b([a-zа-яё0-9](?:[a-zа-яё0-9-]{0,61}[a-zа-яё0-9])?\.(?:ru|su|xn--p1ai|рф))\b",
    re.I | re.U,
)  # кириллица в классе символов — regex-верификация IDN (второй.рф) без punycode;
   # re.I корректно кейс-фолдит кириллицу (проверено: РФ/рф/Рф — все матчатся)


def _parse_domains(text: str) -> list[str]:
    """Все .ru/.рф/.su домены из текста (список или HTML-таблица), нижний регистр, деду́п."""
    seen, out = set(), []
    for m in _DOM.findall(text or ""):
        d = m.lower().rstrip(".")
        if d not in seen:
            seen.add(d); out.append(d)
    return out


class CctldClient(BaseClient):
    URL = "https://cctld.ru/service/dellist/"          # выверить: может отдавать файл-список

    def __init__(self):
        super().__init__("https://cctld.ru")

    def list_dropping(self) -> list[dict]:
        r = self.request("GET", self.URL)
        return [{"domain": d, "source": "cctld", "referring_domains": None}
                for d in _parse_domains(r.text)]

    def ping(self) -> bool:
        r = self.request("GET", self.URL)
        return bool(_parse_domains(r.text))
