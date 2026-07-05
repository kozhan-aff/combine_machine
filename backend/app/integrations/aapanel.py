"""aaPanel client (provisioning engine on the VPS). Transport only.

Base: settings.AAPANEL_URL (e.g. https://HOST:8888).
Enable API in aaPanel settings + add this app's IP to the whitelist (127.0.0.1 if same host).

Auth (every request, per docs/api/aapanel.md — authoritative PDF, NOT the HTML docs):
    request_time  = str(int(time.time()))                     # SECONDS, not milliseconds
    request_token = md5(request_time + md5(api_sk))           # chained md5, hexdigest
    POST both fields alongside the endpoint's own params; PERSIST COOKIES across requests
    (one httpx.Client per AaPanelClient instance = one cookie jar). Responses are JSON.

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
        # aaPanel serves a self-signed cert on :8888, so default TLS verification
        # fails. Preferred (docs/api/aapanel.md "Gotchas"): pin the panel's own cert —
        # copy /www/server/panel/ssl/certificate.pem locally and set AAPANEL_CA_BUNDLE
        # to its path; that keeps MITM protection. verify=False fallback is only
        # acceptable for a same-host 127.0.0.1 panel, never a remote one.
        # Single persistent client = single cookie jar, required by the panel auth.
        verify: str | bool = getattr(settings, "AAPANEL_CA_BUNDLE", "") or False
        self._client = httpx.Client(timeout=30, follow_redirects=True, verify=verify)

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

    def delete_site(self, site_name: str, site_id: int) -> dict:
        """Teardown (M6). Empty path/ftp/database => keep docroot files, FTP, DB."""
        return self._post(
            "/site?action=DeleteSite",
            {"webname": site_name, "id": site_id, "path": "", "ftp": "", "database": ""},
        )

    # -- files (M5 deploy) --------------------------------------------------

    def write_file(self, path: str, content: str) -> dict:
        """Write a file to the VPS (creates the parent dir first). Deploys pages to docroot.

        # UNVERIFIED: aaPanel files API (/files?action=CreateDir | SaveFileBody) is not in
        # the researched spec — these are the standard endpoints; confirm on the live panel.
        """
        import posixpath
        parent = posixpath.dirname(path)
        if parent:
            self._post("/files?action=CreateDir", {"path": parent})
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
