"""cctld.ru — реестр освобождающихся .ru/.рф (авторитетный сырой список). Транспорт + парс.

Лендинг https://cctld.ru/service/dellist/ НЕ содержит список доменов — только ссылки на
ежедневные ZIP-архивы (RUDelList<YYYYMMDD>.zip / RFDelList<YYYYMMDD>.zip), которые и есть
реестр: один домен на строку, уже punycode для .рф (проверено вживую 2026-07-08 — 9463 +
438 строк, UTF-8, 0 пустых, 0 дублей, 100% строк — валидные домены). Дату не строим сами
(таймзона/выходные/факт публикации) — регэкспом достаём актуальные href прямо со страницы.
"""
import io
import logging
import re
import zipfile
from datetime import datetime, timezone

from app.integrations.base import BaseClient

logger = logging.getLogger(__name__)

_ZIP_HREF = re.compile(r'href="([^"]*(?:RUDelList|RFDelList)\d{8}\.zip)"')
# Дата дропа — В ИМЕНИ архива (RUDelList20260714.zip = список, освобождающийся 14.07.2026).
# Раньше мы её выбрасывали, и домен уезжал в БД без дедлайна. Тогда whois честно отвечал
# «занят» (он и должен быть занят — ДО дропа!), а вердикт без дедлайна судить не мог и слал
# домен в rejected/not_acquirable. Так весь реестр (~9.5 тыс. строк) был обречён в отказ,
# ни разу не дождавшись своего дропа (дебаг 2026-07-13).
_ZIP_DATE = re.compile(r'(?:RUDelList|RFDelList)(\d{8})\.zip')


def _deadline_from(url: str) -> datetime | None:
    m = _ZIP_DATE.search(url)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y%m%d").replace(tzinfo=timezone.utc)
    except ValueError:                      # имя есть, дата в нём — мусор
        return None


class CctldClient(BaseClient):
    URL = "https://cctld.ru/service/dellist/"

    def __init__(self):
        super().__init__("https://cctld.ru")

    def _zip_urls(self) -> list[str]:
        r = self.request("GET", self.URL)
        hrefs = _ZIP_HREF.findall(r.text)
        return [h if h.startswith("http") else f"{self.base_url}{h}" for h in hrefs]

    def _domains_from_zip(self, url: str) -> list[str]:
        r = self.request("GET", url)
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        text = zf.read(zf.namelist()[0]).decode("utf-8")
        return [ln.strip() for ln in text.splitlines() if ln.strip()]

    def list_dropping(self) -> list[dict]:
        rows: list[dict] = []
        for url in self._zip_urls():
            try:
                names = self._domains_from_zip(url)
            except Exception as e:  # noqa: BLE001 — один битый/недоступный zip не должен ронять другой
                logger.warning("cctld: не удалось скачать/распаковать %s: %s", url, e)
                continue
            deadline = _deadline_from(url)      # дата дропа = дата архива, см. _ZIP_DATE
            if deadline is None:
                logger.warning("cctld: не разобрал дату дропа из %s — домены пойдут без дедлайна", url)
            rows.extend({"domain": d, "source": "cctld", "referring_domains": None,
                         "acquire_deadline": deadline} for d in names)
        return rows

    def ping(self) -> bool:
        return bool(self._zip_urls())
