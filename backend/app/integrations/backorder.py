"""backorder.ru client (catch dropping domains). Transport only.

Runs on top of billmgr (ISPsystem BILLmanager). Docs: doc.backorder.ru
(site has bot protection — keep request examples here).

Auth: account login/password via query param  authinfo=LOGIN:PASSWORD
Tariffs (needed to order): GET https://backorder.ru/manimg/userdata/json/price_ru_backorder.ru.json
    price_id  = id
    period_id = period[0].id
Order (backorder on a drop):
    GET https://backorder.ru/manager/billmgr?func=uniservice.order&out=json
        &period=PERIOD_ID&price=PRICE_ID&domainname=DOMAIN&itype=63&sok=ok
        &payfrom=accountACCOUNT_ID&contact=CONTACT_ID&paynow=on&clientbackorder=yes
        &authinfo=LOGIN:PASSWORD
    (payfrom = 'account' + ACCOUNT_ID, concatenated)
Discovery: service also exposes domains freeing tomorrow with >=1 donor — feed M1.
"""
from app.config import settings
from app.integrations.base import BaseClient


class BackorderClient(BaseClient):
    def __init__(self):
        super().__init__("https://backorder.ru")
        self.login = settings.BACKORDER_LOGIN
        self.password = settings.BACKORDER_PASSWORD
        self.account_id = settings.BACKORDER_ACCOUNT_ID
        self.contact_id = settings.BACKORDER_CONTACT_ID

    def get_tariffs(self) -> dict:
        """Базовая цена бэкордера .ru из публичного тарифного JSON (без auth).
        price_id=id, period_id=period[0].id, price — базовая стоимость. Поля цены сверить
        на живом ответе (спек §J): пробуем cost/price/sum."""
        r = self.request("GET", f"{self.base_url}/manimg/userdata/json/price_ru_backorder.ru.json")
        d = r.json() if hasattr(r, "json") else {}
        d = d[0] if isinstance(d, list) and d else d
        if not isinstance(d, dict):
            return {"price": None, "price_id": None, "period_id": None}
        period = d.get("period") or []
        price_raw = d.get("cost") or d.get("price") or d.get("sum")
        try:
            price = float(price_raw) if price_raw is not None else None
        except (TypeError, ValueError):
            price = None
        return {"price": price, "price_id": str(d.get("id") or "") or None,
                "period_id": (str(period[0].get("id")) if period and isinstance(period[0], dict)
                              and period[0].get("id") is not None else None)}

    def list_dropping(self, min_links: int = 1, limit: int = 5000) -> list[dict]:
        """Domains freeing tomorrow with >=min_links donors (discovery source for M1).

        Public feed, no auth. Fields: domainname, links, delete_date, visitors,
        yandex_tic, x_value, rkn, judicial, block. See docs/api/backorder.md.
        """
        r = self.request("GET", f"{self.base_url}/json/", params={
            "ext": "1", "disp": "1", "tomorrow": "1",
            "links": str(min_links), "by": "links", "order": "desc",
        })
        data = r.json()
        rows = data if isinstance(data, list) else []
        return rows[:limit]

    def order(self, domain: str, price_id: str, period_id: str) -> dict:
        # HARD GATE: only call after AcquisitionOrder.confirmed_by_human is True.
        raise NotImplementedError

    def ping(self) -> bool:
        # public discovery feed (no auth): domains freeing tomorrow with >=1 donor
        r = self.request("GET", f"{self.base_url}/json/",
                         params={"ext": "1", "disp": "1", "tomorrow": "1",
                                 "links": "1", "by": "links", "order": "desc"})
        return isinstance(r.json(), list)
