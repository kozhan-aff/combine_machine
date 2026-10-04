"""registry.mx — ежедневный список УЖЕ удалённых .mx/.com.mx/.org.mx, транспорт.

Живой формат 2026-10-01: первая строка — штамп времени ("01/10/26 03:00:16 GMT-6"), затем
заголовок `Dominio,Disponible`, ~620 строк. Удалённый = свободен к регистрации -> лейн free.
"""
import csv

from app.integrations.base import BaseClient

URL = "https://www.registry.mx/report/domain_deleted_list.csv"


def parse_deleted(text: str) -> list[dict]:
    lines = text.splitlines()
    # Перед заголовком идёт штамп времени — ищем заголовок, а не берём первую строку.
    start = next((i for i, ln in enumerate(lines) if ln.lower().startswith("dominio,")), None)
    if start is None:
        return []
    out = []
    for row in csv.DictReader(lines[start:]):
        d = (row.get("Dominio") or "").strip().lower()
        if d and (row.get("Disponible") or "").strip().lower() == "true":
            out.append({"domain": d, "source": "mx", "lane": "free", "acquire_deadline": None})
    return out


class RegistryMxClient(BaseClient):
    def __init__(self):
        super().__init__("", timeout=60.0)

    def list_dropping(self) -> list[dict]:
        return parse_deleted(self.request("GET", URL).text)

    def ping(self) -> bool:
        return self.request("GET", URL).status_code == 200
