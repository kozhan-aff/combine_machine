"""IndexNow — пинг поисковиков о новых/изменённых URL (Bing, Yandex, Seznam, Naver и др.). ТОЛЬКО транспорт.

Протокол: POST JSON {host, key, keyLocation, urlList} на единую точку входа; владение сайтом
доказывается файлом `https://host/<key>.txt` с самим ключом (его деплоит site_builder вместе с сайтом).
Ответ: 200/202 — принято; 400/403/422/429 — отказ. Google IndexNow не читает (его закрывает GSC).
POST не повторяем (BaseClient по умолчанию): пинг — best-effort, следующая публикация пнёт снова.
"""
import httpx

from app.config import settings
from app.integrations.base import BaseClient

MAX_URLS = 10000            # лимит протокола на один POST


class IndexNowError(Exception):
    pass


class IndexNowClient(BaseClient):
    def __init__(self):
        super().__init__("", timeout=20.0)

    def submit(self, host: str, key: str, urls: list[str]) -> int:
        """-> число отправленных URL. Пустой список — не ходит в сеть."""
        urls = list(dict.fromkeys(urls))[:MAX_URLS]
        if not urls:
            return 0
        payload = {"host": host, "key": key, "keyLocation": f"https://{host}/{key}.txt", "urlList": urls}
        try:
            r = self.request("POST", settings.INDEXNOW_URL, json=payload)
        except httpx.HTTPStatusError as e:
            raise IndexNowError(f"IndexNow: HTTP {e.response.status_code}") from e
        except httpx.HTTPError as e:
            raise IndexNowError(f"IndexNow: {type(e).__name__}") from e
        if r.status_code not in (200, 202):
            raise IndexNowError(f"IndexNow: HTTP {r.status_code}")
        return len(urls)
