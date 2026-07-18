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


def _fail_msg(res) -> str | None:
    """Текст отказа из конверта aaPanel — или None, если отказа нет.

    aaPanel, как и A-Parser, отвечает **HTTP 200 даже на отказ**: сбой живёт в ТЕЛЕ
    ({"status": false, "msg": "permission denied"}), `raise_for_status` его не видит.
    Судим ТОЛЬКО по булеву `status` — ровно как велит docs/api/aapanel.md («Branch on the
    boolean status fields, not on message strings»: msg локализован, на CN-сборках он
    по-китайски).

    Что НЕ отказ (иначе валидатор сломал бы рабочий провижн):
      · не-dict — законный ответ: /ajax?action=GetTaskCount отдаёт голый int, getData на
        части сборок — список;
      · dict БЕЗ ключа `status` — успех большинства ручек: AddSite отвечает
        {"siteStatus": true, ...}, getData — {"data": [...], "where": ..., "page": ...};
      · `data: []` — «сайтов нет», а не «спросить не смог».
    """
    if isinstance(res, dict) and res.get("status") is False:
        return str(res.get("msg") or res)[:200]
    return None


def _ok(res, what: str, also: str | None = None):
    """Пропустить успешный ответ, отказ — поднять RuntimeError (глотать его нельзя).

    Поднимается ВНЕ `@retry` (`BaseClient.request` уже вернул ответ): иначе один отказ
    «permission denied» превратился бы в ТРИ попытки создать сайт — шум и риск полусоздания.
    `also` — сопутствующая причина (см. write_file), чтобы оператор увидел первопричину,
    а не только последнее звено.
    """
    msg = _fail_msg(res)
    if msg is None:
        return res
    raise RuntimeError(f"aaPanel {what}: {msg}" + (f" [{also}]" if also else ""))


class AaPanelClient(BaseClient):
    def __init__(self):
        super().__init__(settings.AAPANEL_URL)
        # BaseClient just opened a default httpx.Client; we replace it with a pinned one
        # below, so close the throwaway to avoid leaking an unused connection pool.
        self._client.close()
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
            # Fail fast with a readable error: load_verify_locations raises a bare
            # FileNotFoundError without the path, useless in the /diag banner.
            from pathlib import Path
            if not Path(ca).is_file():
                raise RuntimeError(
                    f"AAPANEL_CA_BUNDLE={ca!r} — файл не найден на этом хосте "
                    "(путь из контейнера? скопируй cert панели и поправь .env)")
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

        Читающая ручка, но конверт проверяем и здесь: на ней стоит ИДЕМПОТЕНТНОСТЬ. Отказ,
        принятый за «сайтов нет» (`res["data"]` отсутствует -> `[]`), заставил бы ensure_site
        создавать уже существующий сайт. Пустой `data: []` — законный ответ, он проходит.
        """
        res = _ok(self._post(
            "/data?action=getData&table=sites",
            {"table": "sites", "p": 1, "limit": 1000, "type": -1, "search": ""},
        ), "getData table=sites")
        if isinstance(res, dict):
            return res.get("data") or []
        return res if isinstance(res, list) else []

    def site_exists(self, name: str) -> bool:
        return any(s.get("name") == name for s in self.list_sites())

    def add_site(self, domain: str, path: str, php_version: str = "00", port: int = 80) -> dict:
        """Create an nginx vhost. version="00" = pure static (no PHP) — our default.

        Not idempotent by itself (duplicate => status/msg error); use ensure_site().
        Отказ (нет прав, «сайт уже есть», кончилось место) -> RuntimeError: раньше он
        возвращался обычным словарём, provision его не смотрел и объявлял сайт готовым.
        Успех приходит БЕЗ ключа `status` ({"siteStatus": true, ...}) — _ok его пропускает.
        """
        return _ok(self._post(
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
        ), "AddSite")

    def ensure_site(self, domain: str, path: str, **kw) -> dict:
        """Idempotent create: skip if a site named `domain` already exists.

        Отказ AddSite ещё не значит «провижн сорван». Самый частый его повод — «сайт уже есть»
        (docs/api/aapanel.md: AddSite на существующем домене отвечает status/msg-ошибкой, текст
        китайский — «网站已存在»), то есть ЖЕЛАЕМОЕ СОСТОЯНИЕ УЖЕ ДОСТИГНУТО. Так бывает, когда
        сайт появился МЕЖДУ нашим списком и AddSite (параллельный свип, оператор руками) или
        когда getData его не показал (там же, каверат: на части сборок список видит не все типы
        проектов). Уронить тут RuntimeError — значит запереть сайт в вечном `provisioning`:
        каждый прогон свипа будет заново звать AddSite и заново получать тот же отказ.

        Судить об этом по ТЕКСТУ msg нельзя — он локализован, и это ровно тот урок, ради
        которого валидатор смотрит на булев `status` (см. _fail_msg). Поэтому спрашиваем
        панель ещё раз: сайт есть — значит есть, кто бы что ни ответил. Панель остаётся
        источником правды, локаль ни при чём. Настоящий отказ (нет прав, протух api_sk, кончилось
        место) сайта не породит — второй `site_exists` вернёт False, и RuntimeError полетит
        наверх, как и должен.
        """
        if self.site_exists(domain):
            return {"exists": True, "name": domain}
        try:
            return self.add_site(domain, path, **kw)
        except RuntimeError:
            if not self.site_exists(domain):
                raise
            return {"exists": True, "name": domain}

    def apply_ssl(self, domain: str, site_name: str) -> dict:
        """Issue + deploy an origin cert. Успех -> dict, ЛЮБОЙ отказ -> RuntimeError.

        Контракт единый и звучит вслух: раньше половина отказов возвращалась словарём
        {"status": False, ...} — вызывающему коду пришлось бы его разбирать, и он бы этого
        не делал (ровно так и молчал провижн). Метод пока не вызывается из services/ (M3
        держит SSL на стороне Cloudflare), но его первый же потребитель должен получить
        отказ отказом, а не «пустым сертификатом».
        """
        # UNVERIFIED (see docs/api/aapanel.md) — the /acme flow is not in the official
        # PDF; field semantics sourced from the PHP reference lib + aaPanel source.
        # In particular `auth_to` may need the site NAME instead of the id on some
        # builds, and the step-1 response key names for key/cert may vary.
        site_id = next(
            (s.get("id") for s in self.list_sites() if s.get("name") == site_name), None
        )
        if site_id is None:
            # шаг называем ТОТ, на котором встали: до SetSSL дело не дошло, упали на поиске id
            # в списке сайтов — иначе оператор пойдёт чинить не ту ручку.
            raise RuntimeError(f"aaPanel apply_ssl: site not found: {site_name}")

        # Step 1 — issue Let's Encrypt cert (http-01: domain must already resolve here).
        issued = _ok(self._post(
            "/acme?action=apply_cert_api",
            {
                "domains": json.dumps([domain]),
                "id": site_id,
                "auth_to": site_id,  # UNVERIFIED: PHP lib uses id; retry with site_name if issuance fails
                "auth_type": "http",
                "auto_wildcard": 0,
            },
        ), "apply_cert_api")
        key = issued.get("private_key") if isinstance(issued, dict) else None
        cert = (issued.get("cert") or issued.get("fullchain")) if isinstance(issued, dict) else None
        if not key or not cert:
            # Конверт может быть «успешным», а ключа/цепочки в нём нет (имена полей на части
            # сборок другие — UNVERIFIED выше). Ставить в SetSSL пустой сертификат нельзя.
            raise RuntimeError(f"aaPanel apply_cert_api: ответ без key/cert: {str(issued)[:160]}")

        # Step 2 — deploy to the vhost. NB: the cert PEM goes in the field named `csr`
        # (aaPanel misnomer — it is the full certificate chain, not a signing request).
        return _ok(self._post(
            "/site?action=SetSSL",
            {"type": 1, "siteName": site_name, "key": key, "csr": cert},
        ), "SetSSL")

    def delete_site(self, site_name: str, site_id: int, remove_dir: bool = True,
                    remove_ftp: bool = True, remove_db: bool = True) -> dict:
        """Teardown (M6). VERIFIED 7.x: DeleteSite validates id(int), webname, path(int) —
        the flags MUST be integers (empty strings fail with 'path must be integer').
        path=1 also deletes the docroot; pass remove_dir=False to keep files (301 migration).

        Как все write-методы файла — проверяем конверт через _ok(): панель отвечает HTTP 200
        даже на отказ ({"status": false, "msg": ...}), и без _ok() провалившийся teardown
        (протухший api_sk / нет прав / ошибка БД) вернулся бы как успех, а вызывающий M6
        пометил бы сайт снесённым, пока vhost/файлы ещё живы (инвариант файла: «отказ —
        поднять RuntimeError, глотать его нельзя»)."""
        return _ok(self._post(
            "/site?action=DeleteSite",
            {"id": site_id, "webname": site_name, "path": int(remove_dir),
             "ftp": int(remove_ftp), "database": int(remove_db)},
        ), "DeleteSite")

    # -- files (M5 deploy) --------------------------------------------------

    def write_file(self, path: str, content: str) -> dict:
        """Write a file to the VPS, creating it + parent dirs first. Deploys pages to docroot.

        VERIFIED live (aaPanel 7.x, files.py): SaveFileBody EDITS ONLY — it refuses a
        non-existent path with "Configuration file not exist". CreateFile makes the parent
        dirs (os.makedirs) AND an empty file. So the order is CreateFile → SaveFileBody.
        CreateFile is idempotent-for-our-purposes: on re-publish it returns "Requested file
        exists!", which we ignore, and SaveFileBody then overwrites the body.

        ГРАНИЦА «пусто ≠ сбой» здесь проходит по CreateFile, и она НЕ судится по конверту.
        «Requested file exists!» приезжает тем же `{"status": false, "msg": ...}`, что и
        настоящий отказ, — отличить их можно только по ТЕКСТУ msg, а текст локализован
        (docs/api/aapanel.md: «Chinese response text… Branch on the boolean status, not on
        message strings»). Валидатор на подстроке сломал бы ПОВТОРНУЮ публикацию на первой
        же не-английской сборке панели — то есть саму идемпотентность, ради которой этот
        CreateFile и вызывается.

        Поэтому судим по тому, кто знает правду: **тело файла кладёт SaveFileBody**. Успех
        SaveFileBody = страница на диске (что бы ни ответил CreateFile). Настоящий отказ
        CreateFile (нет прав, диск полон) не проскочит: файла не появится, и SaveFileBody
        честно упадёт — «Configuration file not exist». Причину CreateFile несём в тексте
        рядом, чтобы оператор увидел ПЕРВОПРИЧИНУ, а не только последнее звено.
        """
        created = self._post("/files?action=CreateFile", {"path": path})  # +parent dirs, empty file
        saved = self._post("/files?action=SaveFileBody",
                           {"path": path, "data": content, "encoding": "utf-8"})
        why = _fail_msg(created)
        return _ok(saved, "SaveFileBody", also=f"CreateFile: {why}" if why else None)


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
