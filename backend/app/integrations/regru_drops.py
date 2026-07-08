"""reg.ru/domain/deleted — HTML-витрина дропов, только страница 1.

Прямой httpx (BaseClient), не A-Parser: живая проверка (2026-07-08) — страница отдаёт
200 без анти-бота даже с дефолтным httpx User-Agent (`python-httpx/x.x`), JS-рендер не
нужен. Один запрос за прогон discovery — тот же низкообъёмный профиль, что у cctld,
ротация прокси A-Parser не требуется. Если позже добавим пагинацию (десятки/сотни
запросов за прогон) — транспорт нужно будет пересмотреть обратно на A-Parser, см.
docs/superpowers/specs/2026-07-08-discovery-source-markup-fix-design.md.
"""
import re
from app.integrations.base import BaseClient

_URL = "https://www.reg.ru/domain/deleted/"
# Ячейка с доменом — единственная с этим классом (у ячеек с датами тот же базовый
# класс, но без node_first; заголовок таблицы "Домен" — <th>, не <td>, сюда не попадает).
_DOMAIN_CELL = re.compile(r'b-table__cell_node_first">\s*([^<]*?)\s*</td>')


class RegruDropsClient(BaseClient):
    def __init__(self):
        super().__init__("https://www.reg.ru")

    def list_dropping(self) -> list[dict]:
        r = self.request("GET", _URL)
        return [{"domain": d, "source": "reg_ru", "referring_domains": None}
                for d in _DOMAIN_CELL.findall(r.text)]

    def ping(self) -> bool:
        r = self.request("GET", _URL)
        return bool(_DOMAIN_CELL.search(r.text))
