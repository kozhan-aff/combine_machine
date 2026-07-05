"""Cloudflare API v4 client. Transport only.

Base: https://api.cloudflare.com/client/v4/
Auth: Bearer token (settings.CLOUDFLARE_API_TOKEN), account-scoped
(zone creation needs account-level Zone:Edit). See docs/api/cloudflare.md.

Every response is wrapped in the Cloudflare envelope:
    {"success": bool, "result": ..., "errors": [...], "messages": [...]}
`_result()` unwraps it and raises RuntimeError on success == false.

Orchestration (polling status until "active", registrar NS change, ordering
of proxied records vs. zone activation) lives in services/, not here.
"""
import httpx

from app.config import settings
from app.integrations.base import BaseClient

# Fields of a zone object that the provisioning service consumes.
_ZONE_FIELDS = ("id", "status", "name_servers")


class CloudflareClient(BaseClient):
    def __init__(self):
        super().__init__("https://api.cloudflare.com/client/v4")
        self.token = settings.CLOUDFLARE_API_TOKEN
        self.account_id = settings.CLOUDFLARE_ACCOUNT_ID

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}

    @staticmethod
    def _result(resp: httpx.Response):
        """Unwrap the Cloudflare v4 envelope; raise on success == false."""
        data = resp.json()
        if not data.get("success"):
            raise RuntimeError(data.get("errors"))
        return data.get("result")

    @staticmethod
    def _zone(result: dict) -> dict:
        """Project a zone object down to the fields we consume."""
        return {k: result.get(k) for k in _ZONE_FIELDS}

    # --- connectivity ---------------------------------------------------

    def ping(self) -> bool:
        """GET /user/tokens/verify — token valid, reachable, status active."""
        resp = self.request("GET", f"{self.base_url}/user/tokens/verify", headers=self._headers())
        result = self._result(resp)
        return result.get("status") == "active"

    # --- zones ----------------------------------------------------------

    def find_zone(self, domain: str) -> dict | None:
        """GET /zones?name={domain} (exact match). None if no zone exists."""
        resp = self.request(
            "GET",
            f"{self.base_url}/zones",
            headers=self._headers(),
            params={"name": domain},
        )
        zones = self._result(resp) or []
        if not zones:
            return None
        return self._zone(zones[0])

    def create_zone(self, domain: str) -> dict:
        """POST /zones — status is 'pending' until NS delegation is detected."""
        resp = self.request(
            "POST",
            f"{self.base_url}/zones",
            headers=self._headers(),
            json={"name": domain, "account": {"id": self.account_id}},
        )
        return self._zone(self._result(resp))

    def ensure_zone(self, domain: str) -> dict:
        """Idempotent: return the existing zone or create it."""
        return self.find_zone(domain) or self.create_zone(domain)

    def get_zone(self, zone_id: str) -> dict:
        """GET /zones/{zone_id} — used to poll status pending -> active."""
        resp = self.request("GET", f"{self.base_url}/zones/{zone_id}", headers=self._headers())
        return self._zone(self._result(resp))

    # --- DNS records ------------------------------------------------------

    def list_dns(self, zone_id: str, type: str | None = None, name: str | None = None) -> list:
        """GET /zones/{zone_id}/dns_records with optional type/name filters."""
        params = {}
        if type is not None:
            params["type"] = type
        if name is not None:
            params["name"] = name
        resp = self.request(
            "GET",
            f"{self.base_url}/zones/{zone_id}/dns_records",
            headers=self._headers(),
            params=params,
        )
        return self._result(resp) or []

    def add_a_record(self, zone_id: str, name: str, ip: str, proxied: bool = True) -> dict:
        """POST an A record. proxied=true masks the origin IP; ttl=1 = automatic."""
        resp = self.request(
            "POST",
            f"{self.base_url}/zones/{zone_id}/dns_records",
            headers=self._headers(),
            json={"type": "A", "name": name, "content": ip, "proxied": proxied, "ttl": 1},
        )
        return self._result(resp)

    def ensure_a_record(self, zone_id: str, name: str, ip: str, proxied: bool = True) -> dict:
        """Idempotent: return the existing A record for `name` or create one.

        A duplicate identical POST 400s, so check-then-act.
        """
        existing = self.list_dns(zone_id, type="A", name=name)
        if existing:
            return existing[0]
        return self.add_a_record(zone_id, name, ip, proxied=proxied)

    def add_txt_record(self, zone_id: str, name: str, content: str) -> dict:
        """POST a TXT record (GSC / provider verification tokens)."""
        resp = self.request(
            "POST",
            f"{self.base_url}/zones/{zone_id}/dns_records",
            headers=self._headers(),
            json={"type": "TXT", "name": name, "content": content, "ttl": 1},
        )
        return self._result(resp)

    # --- zone settings ---------------------------------------------------

    def set_ssl(self, zone_id: str, mode: str = "full") -> bool:
        """PATCH /zones/{zone_id}/settings/ssl. Start at 'full'; upgrade to
        'strict' only after a valid origin cert is installed on aaPanel."""
        resp = self.request(
            "PATCH",
            f"{self.base_url}/zones/{zone_id}/settings/ssl",
            headers=self._headers(),
            json={"value": mode},
        )
        self._result(resp)
        return True
