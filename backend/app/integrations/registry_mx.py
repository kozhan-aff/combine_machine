"""registry.mx — ежедневный список УЖЕ удалённых .mx/.com.mx/.org.mx, транспорт.

Живой формат 2026-10-01: первая строка — штамп времени ("01/10/26 03:00:16 GMT-6"), затем
заголовок `Dominio,Disponible`, ~620 строк. Удалённый = свободен к регистрации -> лейн free.
"""
import csv

from app.integrations.base import BaseClient, conditional_get

URL = "https://www.registry.mx/report/domain_deleted_list.csv"


def parse_deleted(text: str) -> list[dict]:
    lines = text.splitlines()
    # Перед заголовком идёт штамп времени — ищем заголовок, а не берём первую строку.
    start = next((i for i, ln in enumerate(lines) if ln.lower().startswith("dominio,")), None)
    if start is None:
        # Законных нулей у источника нет: нет заголовка = сменился формат, а не «пустой день».
        raise ValueError("registry.mx: сменился формат, не найден заголовок Dominio")
    reader = csv.DictReader(lines[start:])
    # Ключи сводим к нижнему регистру — тот же регистр, что и при поиске заголовка выше.
    reader.fieldnames = [(f or "").strip().lower() for f in (reader.fieldnames or [])]
    for col in ("dominio", "disponible"):
        if col not in reader.fieldnames:
            raise ValueError(f"registry.mx: сменился формат, нет колонки {col.capitalize()}")
    out = []
    for row in reader:
        d = (row.get("dominio") or "").strip().lower()
        if d and (row.get("disponible") or "").strip().lower() == "true":
            out.append({"domain": d, "source": "mx", "lane": "free", "acquire_deadline": None})
    return out


class RegistryMxClient(BaseClient):
    def __init__(self):
        super().__init__("", timeout=60.0)
        self.validators: dict | None = None   # Last-Modified с прошлого забора (S1-11)

    def list_dropping(self) -> list[dict]:
        return parse_deleted(conditional_get(self, URL).text)

    def ping(self) -> bool:
        # HEAD, а не GET: /diag пингует каждые 5 минут (фон), полный CSV — ~288 скачиваний в сутки
        # у небольшого ccTLD-реестра. Живой HEAD 2026-10-04 -> 200, Content-Length/Last-Modified.
        return self.request("HEAD", URL).status_code == 200
