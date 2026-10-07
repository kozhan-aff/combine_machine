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


# CF: connect короткий — лежащий/блокированный CF не должен держать вызов 30 с на попытку (S4-08).
_TIMEOUT = httpx.Timeout(15.0, connect=5.0)


class CloudflareError(httpx.HTTPStatusError):
    """4xx/5xx Cloudflare С ТЕЛОМ (S4-06). Подкласс HTTPStatusError — ретрай-политика BaseClient
    (`response.status_code`, Retry-After) работает как раньше, но в тексте теперь код и сообщение
    CF («1061:The zone already exists», «9109:Unauthorized…»), а не безликое «Client error 400»."""

    def __init__(self, response: httpx.Response):
        try:
            body = response.json()
        except ValueError:
            body = {}
        body = body if isinstance(body, dict) else {}
        self.codes = [e.get("code") for e in (body.get("errors") or []) if isinstance(e, dict)]
        reason = _safe_errors(body) if body.get("errors") else f"HTTP {response.status_code}"
        super().__init__(
            f"Cloudflare {response.status_code} {response.request.method} "
            f"{response.request.url.path}: {reason}",
            request=response.request, response=response)


def _safe_errors(body: dict) -> str:
    """Форматировать ошибки CF-envelope без утечки Authorization/raw-response (аудит §4)."""
    errs = body.get("errors") or []
    parts = [f"{e.get('code', '')}:{e.get('message', '')}" for e in errs if isinstance(e, dict)]
    return "; ".join(p for p in parts if p.strip(":")) or "unknown error"


class CloudflareClient(BaseClient):
    def __init__(self):
        super().__init__("https://api.cloudflare.com/client/v4", timeout=_TIMEOUT)
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

    def _request_once(self, method: str, url: str, **kwargs) -> httpx.Response:
        """Как у BaseClient, но 4xx/5xx сохраняют тело CF в исключении (S4-06): `raise_for_status`
        выбрасывал его раньше, чем `_result` успевал прочитать envelope."""
        resp = self._client.request(method, url, **kwargs)
        if resp.status_code >= 400:
            raise CloudflareError(resp)
        return resp

    @staticmethod
    def _result(resp: httpx.Response):
        """Unwrap the Cloudflare v4 envelope; raise on success == false."""
        data = resp.json()
        if not data.get("success"):
            raise RuntimeError(_safe_errors(data))
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

    def ping_detail(self) -> str:
        """Активность токена + права, ЕСЛИ токен может прочитать собственные политики (F8-18).

        Возвращает пометку для /diag. Права записи (создать зону, DNS) иначе вскрылись бы только
        на первом provision. verify отдаёт id токена; GET /user/tokens/{id} читает политики, но
        сам требует права на чтение токенов — типичный zone-токен его не имеет (403), тогда честно
        пишем «права записи не проверены». Только чтение; формат policies[].permission_groups[].name
        — по публичной документации CF, не по живому образцу: разбор защитный."""
        res = self._result(self.request("GET", f"{self.base_url}/user/tokens/verify",
                                        headers=self._headers())) or {}
        if res.get("status") != "active":
            raise RuntimeError(f"токен не активен: {res.get('status')}")
        base = "токен активен"
        try:
            pol = self._result(self.request(
                "GET", f"{self.base_url}/user/tokens/{res.get('id')}", headers=self._headers()))
            names = [str(g.get("name") or "") for p in (pol or {}).get("policies") or []
                     for g in p.get("permission_groups") or []]
        except Exception:  # noqa: BLE001 — нет права читать свои политики: не ошибка токена
            names = []
        if not names:
            return f"{base}; права записи не проверены (токен не читает свои политики)"
        write = [n for n in names if "write" in n.lower() or "edit" in n.lower()]
        if not write:
            raise RuntimeError(f"токен активен, но только на чтение: {', '.join(names)[:120]}")
        missing = [k for k in ("Zone", "DNS") if not any(k.lower() in n.lower() for n in write)]
        return (f"{base}; права записи: {', '.join(write)[:100]}"
                + (f"; ⚠ не найдено: {'/'.join(missing)}" if missing else ""))

    # --- zones ----------------------------------------------------------

    def find_zone(self, domain: str) -> dict | None:
        """GET /zones?name={domain} (exact match). None if no zone exists.

        С заданным аккаунтом ищем ТОЛЬКО в нём: user-токен видит зоны всех аккаунтов, и зона,
        заведённая в чужом аккаунте, молча переиспользовалась бы (S4-03)."""
        params = {"name": domain}
        if self.account_id:
            params["account.id"] = self.account_id
        resp = self.request(
            "GET",
            f"{self.base_url}/zones",
            headers=self._headers(),
            params=params,
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
        """Idempotent: return the existing zone or create it.

        Гонка панель/воркер: между find и create зону успел завести параллельный прогон — CF отвечает
        1061 «zone already exists». Это достигнутое желаемое состояние: ищем ещё раз (S4-06)."""
        found = self.find_zone(domain)
        if found:
            return found
        try:
            return self.create_zone(domain)
        except CloudflareError as e:
            if 1061 in e.codes:
                found = self.find_zone(domain)
                if found:
                    return found
            raise

    def get_zone(self, zone_id: str) -> dict:
        """GET /zones/{zone_id} — used to poll status pending -> active."""
        resp = self.request("GET", f"{self.base_url}/zones/{zone_id}", headers=self._headers())
        return self._zone(self._result(resp))

    def activation_check(self, zone_id: str) -> bool:
        """PUT /zones/{id}/activation_check — просим CF перепроверить NS сейчас, не дожидаясь его
        планового опроса (S4-04). CF ограничивает частоту (≈ раз в час на free) — троттлит
        вызывающий (services/provisioning)."""
        self._result(self.request("PUT", f"{self.base_url}/zones/{zone_id}/activation_check",
                                  headers=self._headers()))
        return True

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

    def set_zone_setting(self, zone_id: str, setting_id: str, value) -> bool:
        """PATCH /zones/{id}/settings/{setting_id} — always_use_https, min_tls_version и т.п.
        Вызывающий сперва читает (`get_zone_setting`) и пишет только при расхождении."""
        self._result(self.request("PATCH", f"{self.base_url}/zones/{zone_id}/settings/{setting_id}",
                                  headers=self._headers(), json={"value": value}))
        return True

    def create_origin_certificate(self, csr_pem: str, hostnames: list[str],
                                  validity_days: int = 5475) -> dict:
        """POST /certificates — Cloudflare Origin CA для ОДНОГО домена (инвариант v2 №5: один
        сертификат на домен, multi-SAN раскрыл бы портфель). Нужны права «SSL and Certificates:
        Edit» у токена. Возвращает result (certificate = PEM сертификата). Приватный ключ CF не
        видит: CSR сделан локально."""
        return self._result(self.request(
            "POST", f"{self.base_url}/certificates", headers=self._headers(),
            json={"hostnames": hostnames, "requested_validity": validity_days,
                  "request_type": "origin-ecc", "csr": csr_pem}))

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
        result = self._result(resp) or {}
        if result.get("status") != "active":
            # просроченный/отключённый токен отвечает 200 и status!='active' (S4-10): без этой
            # проверки sync рисовал бы зелёное «ok» (ping() выше уже сверяет так же).
            raise RuntimeError(f"токен не активен: {result.get('status')}")
        return result

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

    def get_universal_ssl(self, zone_id: str) -> dict:
        """GET /zones/{zone_id}/ssl/universal/settings — read-only статус Universal SSL.
        У него ОТДЕЛЬНЫЙ эндпоинт (не /settings/universal_ssl), ответ {enabled: bool}."""
        resp = self.request("GET", f"{self.base_url}/zones/{zone_id}/ssl/universal/settings",
                            headers=self._headers())
        return self._result(resp)
