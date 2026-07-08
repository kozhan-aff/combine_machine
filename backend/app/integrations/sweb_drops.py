"""sweb.ru/domains/deleted — HTML-витрина дропов, только страница 1.

Прямой httpx (BaseClient) — тот же профиль и обоснование, что у regru_drops.py (1 запрос
за прогон, антибота нет, A-Parser не нужен для такого объёма).

ВАЖНО: класс `domains-deleted__text` НЕ уникален для домена — тот же класс используют
обе даты (регистрация/освобождение) в соседних полях того же item-блока. Наивный якорь
на этот класс поймал бы даты (DD.MM.YYYY) как "домены". Якорим на конкретную пару
label+value, где label буквально "Домен".
"""
import re
from app.integrations.base import BaseClient

_URL = "https://sweb.ru/domains/deleted/"
_DOMAIN_FIELD = re.compile(
    r'domains-deleted__label">Домен</span>\s*'
    r'<span class="domains-deleted__text">\s*([^<]*?)\s*</span>'
)


class SwebDropsClient(BaseClient):
    def __init__(self):
        super().__init__("https://sweb.ru")

    def list_dropping(self) -> list[dict]:
        r = self.request("GET", _URL)
        return [{"domain": d, "source": "sweb", "referring_domains": None}
                for d in _DOMAIN_FIELD.findall(r.text)]

    def ping(self) -> bool:
        r = self.request("GET", _URL)
        return bool(_DOMAIN_FIELD.search(r.text))
