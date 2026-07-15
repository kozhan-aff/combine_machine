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


def _safe_errors(body: dict) -> str:
    """Форматировать ошибки CF-envelope без утечки Authorization/raw-response (аудит §4)."""
    errs = body.get("errors") or []
    parts = [f"{e.get('code', '')}:{e.get('message', '')}" for e in errs if isinstance(e, dict)]
    return "; ".join(p for p in parts if p.strip(":")) or "unknown error"


class CloudflareClient(BaseClient):
    def __init__(self):
        super().__init__("https://api.cloudflare.com/client/v4")
        self.token = settings.CLOUDFLARE_API_TOKEN
        self.account_id = settings.CLOUDFLARE_ACCOUNT_ID

    @classmethod
    def with_token(cls, token: str, account_id: str = "") -> "CloudflareClient":
        """Клиент с ЯВНЫМ токеном/аккаунтом — не из глобального settings singleton (аудит §4.1).

        Нужен account-aware P0: разные CloudflareConnection в БД несут разные secret_ref,
        а singleton на settings знает только про один .env-токен.
        """
        c = cls()
        c.token = token
        c.account_id = account_id
        return c

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}

    @staticmethod
    def _result(resp: httpx.Response):
        """Unwrap the Cloudflare v4 envelope; raise on success == false."""
        data = resp.json()
        if not data.get("success"):
            raise RuntimeError(data.get("errors"))
        return data.get("result")

    def _paginate(self, path: str, params: dict | None = None) -> list:
        """Собрать все страницы по `result_info`. HTTP 2xx недостаточно — envelope
        `success` проверяется на КАЖДОЙ странице (пусто ≠ ошибка ≠ not-found, аудит §2)."""
        out: list = []
        page = 1
        params = dict(params or {})
        while True:
            params["page"] = page
            params.setdefault("per_page", 50)
            resp = self.request("GET", f"{self.base_url}{path}",
                                headers=self._headers(), params=params)
            body = resp.json()
            if not body.get("success"):
                raise RuntimeError(f"cloudflare {path}: " + _safe_errors(body))
            out.extend(body.get("result") or [])
            info = body.get("result_info") or {}
            total = info.get("total_pages")
            if not total or page >= total:
                break
            page += 1
        return out

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

    def update_a_record(self, zone_id: str, record_id: str, name: str, ip: str,
                        proxied: bool = True) -> dict:
        """PATCH an existing A record's content/proxied (re-provision to a new origin)."""
        resp = self.request(
            "PATCH",
            f"{self.base_url}/zones/{zone_id}/dns_records/{record_id}",
            headers=self._headers(),
            json={"type": "A", "name": name, "content": ip, "proxied": proxied, "ttl": 1},
        )
        return self._result(resp)

    def ensure_a_record(self, zone_id: str, name: str, ip: str, proxied: bool = True) -> dict:
        """Idempotent: create the A record for `name`, or reconcile an existing one.

        A duplicate identical POST 400s, so check-then-act. If the record already exists
        but points at a different origin (content) or has the wrong proxied flag — e.g. a
        re-provision after VPS_ORIGIN_IP changed — PATCH it so the site doesn't silently
        keep pointing at the old origin.
        """
        existing = self.list_dns(zone_id, type="A", name=name)
        if existing:
            rec = existing[0]
            if rec.get("content") != ip or bool(rec.get("proxied")) != bool(proxied):
                return self.update_a_record(zone_id, rec["id"], name, ip, proxied=proxied)
            return rec
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

    # --- P0 read-only: account-aware verify/discovery (services/cf_sync.py, задача 4) ---
    #
    # Ничего ниже не мутирует Cloudflare. `find_zone(domain)` выше НЕ трогается —
    # им пользуется provisioning.py. `find_zone_in_account` — отдельный account-scoped
    # метод для sync-сервиса (несколько CloudflareConnection/аккаунтов одновременно).

    def verify_token(self, token_kind: str, account_id: str = "") -> dict:
        """GET /user/tokens/verify (user-owned) либо /accounts/{id}/tokens/verify
        (account-owned) — разные токены проверяются РАЗНЫМИ эндпоинтами."""
        if token_kind == "account":
            if not account_id:
                raise ValueError("account-owned токен требует account_id для verify")
            path = f"/accounts/{account_id}/tokens/verify"
        else:
            path = "/user/tokens/verify"
        resp = self.request("GET", f"{self.base_url}{path}", headers=self._headers())
        return self._result(resp)

    def list_accounts_paginated(self) -> list:
        """GET /accounts — все аккаунты, видимые этому токену, все страницы."""
        return self._paginate("/accounts")

    def list_zones_paginated(self, account_id: str) -> list:
        """GET /zones?account.id=... — все зоны аккаунта, все страницы (счёт бывает > 50)."""
        return self._paginate("/zones", {"account.id": account_id})

    def find_zone_in_account(self, name: str, account_id: str) -> dict | None:
        """Account-scoped поиск зоны по имени. НЕ путать с legacy `find_zone(domain)`
        выше (без account.id, используется provisioning.py) — этот всегда фильтрует
        по конкретному аккаунту, т.к. sync видит несколько CloudflareConnection сразу."""
        zones = self._paginate("/zones", {"name": name, "account.id": account_id})
        return zones[0] if zones else None

    def list_dns_paginated(self, zone_id: str, type: str | None = None,
                           name: str | None = None) -> list:
        """GET /zones/{zone_id}/dns_records, все страницы (в отличие от `list_dns` выше,
        который берёт только первую)."""
        params: dict = {}
        if type:
            params["type"] = type
        if name:
            params["name"] = name
        return self._paginate(f"/zones/{zone_id}/dns_records", params)

    def get_zone_setting(self, zone_id: str, setting_id: str) -> dict:
        """GET /zones/{zone_id}/settings/{setting_id} — ТОЛЬКО per-setting.
        Batch `/zones/{id}/settings` deprecated, EOL 2026-09-15 — не добавлять."""
        resp = self.request("GET", f"{self.base_url}/zones/{zone_id}/settings/{setting_id}",
                            headers=self._headers())
        return self._result(resp)

    def list_universal_certificate_packs(self, zone_id: str) -> list:
        """GET /zones/{zone_id}/ssl/certificate_packs — read-only статус Universal SSL."""
        return self._paginate(f"/zones/{zone_id}/ssl/certificate_packs")

    def get_dnssec(self, zone_id: str) -> dict:
        """GET /zones/{zone_id}/dnssec — read-only статус DNSSEC."""
        resp = self.request("GET", f"{self.base_url}/zones/{zone_id}/dnssec",
                            headers=self._headers())
        return self._result(resp)
