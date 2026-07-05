"""Registrar nameserver management (.ru: reg.ru / nic.ru). Transport only.

Needed by M3 provisioning step 2: after creating a Cloudflare zone, point the
domain's NS to Cloudflare's nameservers, then wait for propagation.

reg.ru API v2: https://api.reg.ru/api/regru2/  (username/password or signature+SSL cert)
  method for NS update: domain/update_nss (set ns0/ns1 to Cloudflare's)
nic.ru has its own API. Some resellers expose NS changes too.
"""
from app.config import settings
from app.integrations.base import BaseClient


class RegistrarClient(BaseClient):
    def __init__(self):
        super().__init__("https://api.reg.ru/api/regru2")
        self.username = settings.REGRU_USERNAME
        self.password = settings.REGRU_PASSWORD

    def set_nameservers(self, domain: str, nameservers: list[str]) -> dict:
        """Point domain NS to Cloudflare's assigned nameservers. TODO."""
        raise NotImplementedError

    def ping(self) -> bool:
        raise NotImplementedError
