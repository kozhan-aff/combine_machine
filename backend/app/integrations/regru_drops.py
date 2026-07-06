"""reg.ru/domain/deleted — HTML-витрина дропов. Под бот-защитой → тянем через A-Parser.

Парсер доменов переиспользуем из cctld (устойчив к разметке). Без RD.
"""
from app.integrations.aparser import AParserClient
from app.integrations.cctld import _parse_domains

_URL = "https://www.reg.ru/domain/deleted/"


class RegruDropsClient:
    def list_dropping(self) -> list[dict]:
        html = AParserClient().fetch_html(_URL) or ""
        return [{"domain": d, "source": "reg_ru", "referring_domains": None}
                for d in _parse_domains(html)]

    def ping(self) -> bool:
        return bool(AParserClient().fetch_html(_URL))
