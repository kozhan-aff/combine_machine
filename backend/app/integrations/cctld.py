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
from app.integrations.base import BaseClient

logger = logging.getLogger(__name__)

_ZIP_HREF = re.compile(r'href="([^"]*(?:RUDelList|RFDelList)\d{8}\.zip)"')


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
        domains: list[str] = []
        for url in self._zip_urls():
            try:
                domains.extend(self._domains_from_zip(url))
            except Exception as e:  # noqa: BLE001 — один битый/недоступный zip не должен ронять другой
                logger.warning("cctld: не удалось скачать/распаковать %s: %s", url, e)
        return [{"domain": d, "source": "cctld", "referring_domains": None} for d in domains]

    def ping(self) -> bool:
        return bool(self._zip_urls())
