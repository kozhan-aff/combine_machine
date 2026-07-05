"""optimizator.ru client (register freed domains). Transport only.

Reseller layer over nic.ru (RU-CENTER) and reg.ru. For registering ALREADY-FREE
domains (not for winning a competitive drop auction).

Method reg_domains:
    GET http://optimizator.ru/?a=api&sa=reg_domains&api_key=KEY&nicd=NICD
        &domains=DOMAINS&enc=utf8
    - api_key (required), nicd (RU-CENTER account number, required),
      domains (space-separated, up to 30), enc (utf8|cp1251)
    - Response: [{"order_id": N}]. Order status via a separate method.
"""
from app.config import settings
from app.integrations.base import BaseClient


class OptimizatorClient(BaseClient):
    def __init__(self):
        super().__init__("http://optimizator.ru")
        self.api_key = settings.OPTIMIZATOR_API_KEY
        self.nicd = settings.OPTIMIZATOR_NICD

    def register(self, domains: list[str]) -> list[dict]:
        # HARD GATE: only after confirmed_by_human. Max 30 per call.
        raise NotImplementedError

    def order_status(self, order_id: int) -> dict:
        raise NotImplementedError

    def ping(self) -> bool:
        raise NotImplementedError
