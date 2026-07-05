"""aaPanel client (provisioning engine on the VPS). Transport only.

Base: settings.AAPANEL_URL (e.g. https://HOST:8888).
Enable API in aaPanel settings + add this app's IP to the whitelist (127.0.0.1 if same host).

Auth (every request, per docs/api/aapanel.md — authoritative PDF, NOT the HTML docs):
    request_time  = str(int(time.time()))                     # SECONDS, not milliseconds
    request_token = md5(request_time + md5(api_sk))           # chained md5, hexdigest
    POST both fields alongside the endpoint's own params; PERSIST COOKIES across requests
    (one httpx.Client per AaPanelClient instance = one cookie jar). Responses are JSON.

    AAPANEL_API_KEY must be the RAW api_sk — on 7.x that's the `token_crypt` field of
    /www/server/panel/config/api.json (verified: md5(token_crypt) == the `token` field).
    Do NOT use the `token` field: it is already md5(api_sk), so our chained md5 would
    hash it twice and the panel rejects it with "Secret key verification failed". The
    panel verifies md5(request_time + token) where token == md5(api_sk). Also note the
    panel port is NOT always 8888 (this box: 18839) and the API IP-whitelist (limit_addr)
    is exact-match on the caller's public IP — a domain/DDNS entry is NOT resolved.

We use aaPanel for the vhost + origin SSL only; DNS stays on Cloudflare.
Endpoint styles: legacy `/data?action=...` and current `/v2/data?action=...` — see list_sites().
"""
import hashlib
import json
import time

import httpx

from app.config import settings
from app.integrations.base import BaseClient


def _md5(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()


def _make_token(api_sk: str, request_time: str) -> str:
    """request_token = md5( str(request_time) + md5(api_sk) ) — chained, order matters."""
    return _md5(request_time + _md5(api_sk))


class AaPanelClient(BaseClient):
    def __init__(self):
        super().__init__(settings.AAPANEL_URL)
        self.api_sk = settings.AAPANEL_API_KEY
        # aaPanel serves a self-signed cert on :8888. Pin it via AAPANEL_CA_BUNDLE (copy
        # /www/server/panel/ssl/certificate.pem locally, set its path) to keep MITM
        # protection. FAIL CLOSED: for any NON-loopback panel we refuse to silently fall
        # back to verify=False — that's a MITM hole for a token-bearing client. verify=False
        # is tolerated only for a same-host 127.0.0.1 panel. Single client = single cookie jar.
        from urllib.parse import urlparse
        host = (urlparse(settings.AAPANEL_URL).hostname or "").lower()
        ca = getattr(settings, "AAPANEL_CA_BUNDLE", "") or ""
        if ca:
            # Pin the panel's self-signed cert. check_hostname=False on purpose: the cert's
            # CN/SAN won't match a bare IP, so hostname matching would fail — and it buys
            # nothing here. Pinning to THIS exact cert is the real MITM defense (an attacker's
            # cert won't validate against this CA file). This is what makes an IP-based
            # https://VPS_IP:8888 panel usable with verification instead of verify=False.
            import ssl
            ctx = ssl.create_default_context(cafile=ca)
            ctx.check_hostname = False
            verify: object = ctx
        elif host in {"127.0.0.1", "localhost", "::1"}:
            verify = False
        else:
            raise RuntimeError(
                f"aaPanel {host!r} is not loopback and AAPANEL_CA_BUNDLE is unset — refusing "
                "verify=False (MITM risk). Set AAPANEL_CA_BUNDLE to the panel's cert path.")
        # follow_redirects=False: never let a redirect carry the auth token to another host.
        self._client = httpx.Client(timeout=30, follow_redirects=False, verify=verify)

    # -- auth / transport ---------------------------------------------------

    def _auth(self) -> dict:
        """The two auth fields every request must carry. request_time computed ONCE
        and the same string used in both the field and the token."""
        t = str(int(time.time()))
        return {"request_time": t, "request_token": _make_token(self.api_sk, t)}

    def _post(self, path: str, data: dict | None = None) -> dict:
        """POST form fields (endpoint params + auth) to base_url+path, return parsed JSON.

        Note: a few endpoints (e.g. /ajax?action=GetTaskCount) return a bare JSON
        scalar, so callers should not assume dict blindly.
        """
        payload = {**(data or {}), **self._auth()}
        resp = self.request("POST", f"{self.base_url}{path}", data=payload)
        return resp.json()

    # -- system -------------------------------------------------------------

    def ping(self) -> bool:
        """Cheap health check: panel reachable, key valid, IP whitelisted.

        /ajax?action=GetTaskCount is the cheapest endpoint per docs/api/aapanel.md
        (returns a bare JSON integer, e.g. 0). Auth/whitelist failures come back as
        {"status": false, "msg": "..."} — treat any dict with status=false as failure.
        """
        try:
            res = self._post("/ajax?action=GetTaskCount")
        except (httpx.HTTPError, ValueError):
            return False
        if isinstance(res, dict):
            return res.get("status") is not False
        return isinstance(res, int)  # bare task count => healthy

    # -- websites -----------------------------------------------------------

    def list_sites(self) -> list:
        """All sites; each row has at least `name` (primary domain) and `id`.

        Legacy path `/data?action=getData&table=sites`; current panels (7.x+) also
        expose `/v2/data?action=getData&table=sites` with identical params/response —
        if the legacy path ever 404s, switch to the /v2/data form.
        """
        res = self._post(
            "/data?action=getData&table=sites",
            {"table": "sites", "p": 1, "limit": 1000, "type": -1, "search": ""},
        )
        if isinstance(res, dict):
            return res.get("data") or []
        return res if isinstance(res, list) else []

    def site_exists(self, name: str) -> bool:
        return any(s.get("name") == name for s in self.list_sites())

    def add_site(self, domain: str, path: str, php_version: str = "00", port: int = 80) -> dict:
        """Create an nginx vhost. version="00" = pure static (no PHP) — our default.

        Not idempotent by itself (duplicate => status/msg error); use ensure_site().
        """
        return self._post(
            "/site?action=AddSite",
            {
                "webname": json.dumps({"domain": domain, "domainlist": [], "count": 0}),
                "path": path,
                "type_id": 0,
                "type": "PHP",  # "PHP" even for static; version "00" disables PHP
                "version": php_version,
                "port": port,
                "ps": domain,
                "ftp": "false",
                "sql": "false",
            },
        )

    def ensure_site(self, domain: str, path: str, **kw) -> dict:
        """Idempotent create: skip if a site named `domain` already exists."""
        if self.site_exists(domain):
            return {"exists": True, "name": domain}
        return self.add_site(domain, path, **kw)

    def apply_ssl(self, domain: str, site_name: str) -> dict:
        # UNVERIFIED (see docs/api/aapanel.md) — the /acme flow is not in the official
        # PDF; field semantics sourced from the PHP reference lib + aaPanel source.
        # In particular `auth_to` may need the site NAME instead of the id on some
        # builds, and the step-1 response key names for key/cert may vary.
        site_id = next(
            (s.get("id") for s in self.list_sites() if s.get("name") == site_name), None
        )
        if site_id is None:
            return {"status": False, "msg": f"site not found: {site_name}"}

        # Step 1 — issue Let's Encrypt cert (http-01: domain must already resolve here).
        issued = self._post(
            "/acme?action=apply_cert_api",
            {
                "domains": json.dumps([domain]),
                "id": site_id,
                "auth_to": site_id,  # UNVERIFIED: PHP lib uses id; retry with site_name if issuance fails
                "auth_type": "http",
                "auto_wildcard": 0,
            },
        )
        key = issued.get("private_key") if isinstance(issued, dict) else None
        cert = (issued.get("cert") or issued.get("fullchain")) if isinstance(issued, dict) else None
        if not key or not cert:
            return {"status": False, "msg": "apply_cert_api did not return key/cert", "detail": issued}

        # Step 2 — deploy to the vhost. NB: the cert PEM goes in the field named `csr`
        # (aaPanel misnomer — it is the full certificate chain, not a signing request).
        return self._post(
            "/site?action=SetSSL",
            {"type": 1, "siteName": site_name, "key": key, "csr": cert},
        )

    def delete_site(self, site_name: str, site_id: int, remove_dir: bool = True,
                    remove_ftp: bool = True, remove_db: bool = True) -> dict:
        """Teardown (M6). VERIFIED 7.x: DeleteSite validates id(int), webname, path(int) —
        the flags MUST be integers (empty strings fail with 'path must be integer').
        path=1 also deletes the docroot; pass remove_dir=False to keep files (301 migration)."""
        return self._post(
            "/site?action=DeleteSite",
            {"id": site_id, "webname": site_name, "path": int(remove_dir),
             "ftp": int(remove_ftp), "database": int(remove_db)},
        )

    # -- files (M5 deploy) --------------------------------------------------

    def write_file(self, path: str, content: str) -> dict:
        """Write a file to the VPS, creating it + parent dirs first. Deploys pages to docroot.

        VERIFIED live (aaPanel 7.x, files.py): SaveFileBody EDITS ONLY — it refuses a
        non-existent path with "Configuration file not exist". CreateFile makes the parent
        dirs (os.makedirs) AND an empty file. So the order is CreateFile → SaveFileBody.
        CreateFile is idempotent-for-our-purposes: on re-publish it returns "Requested file
        exists!", which we ignore, and SaveFileBody then overwrites the body.
        """
        self._post("/files?action=CreateFile", {"path": path})  # +parent dirs, empty file
        return self._post("/files?action=SaveFileBody",
                          {"path": path, "data": content, "encoding": "utf-8"})


if __name__ == "__main__":
    # Offline regression guard for the auth-token math (no network).
    # Expected values precomputed once with the documented formula:
    #   md5("testsk") = 9beb31c70be882d0979dc43175c15ffb
    #   md5("1700000000" + md5("testsk")) = 22332a559ab5358ecb177c76c0ad44bc
    sk, t = "testsk", str(1700000000)
    assert _md5(sk) == "9beb31c70be882d0979dc43175c15ffb"
    token = _make_token(sk, t)
    assert token == "22332a559ab5358ecb177c76c0ad44bc", token
    # Guard the CHAINING ORDER: md5(md5(sk) + t) is the wrong order and must differ.
    assert token != _md5(_md5(sk) + t) == "1aef9221269fa2694568cd0ef7cdb4c6"
    print("aapanel auth-token self-check OK:", token)
