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
import threading
import time
from datetime import datetime, timedelta, timezone

import httpx
from tenacity import Retrying, retry_if_exception, stop_after_attempt, wait_exponential

from app.config import settings
from app.integrations.base import BaseClient, _is_retryable


def _md5(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()


def _make_token(api_sk: str, request_time: str) -> str:
    """request_token = md5( str(request_time) + md5(api_sk) ) — chained, order matters."""
    return _md5(request_time + _md5(api_sk))


# -- предохранитель авторизации (S5-01/S7-02/F8-01) ---------------------------------------
# aaPanel банит IP на час после 20 неудачных проверок подряд («20 consecutive verification
# failures, prohibited for 1 hour»). Каждый отказ whitelist/подписи — это очко в этот счётчик,
# а фоновый /diag (раз в 5 мин), свип провижна и публикации слали новые попытки вслепую и
# сами вели IP к бану. Поэтому после первого отказа авторизации в панель НЕ ходим до конца
# паузы: сеть не трогаем вовсе, причину отдаём исключением (её видит /diag).
# Состояние — в памяти ПРОЦЕССА (панель и воркер — разные процессы, у каждого свой счётчик;
# общий стор в БД — отдельный шаг, см. отчёт аудита S5-01 п.4).
BAN_PAUSE_SEC = 61 * 60    # «prohibited for 1 hour» — раньше бесполезно
AUTH_PAUSE_SEC = 15 * 60   # IP вне whitelist / протухший api_sk: чинит человек, не повторы
# Маркеры — по тексту msg (локализован, но на ЭТОТ класс отказов другого сигнала у панели нет);
# ошибка в сторону пропуска безопасна — просто не включится пауза.
_BAN_MARKERS = ("consecutive verification", "prohibited")
_AUTH_MARKERS = ("verification failed", "ip validation")

_block_lock = threading.Lock()
_block: dict = {}   # {"until": monotonic, "reason": str, "fp": str} — пусто = панель открыта


class AaPanelBlocked(RuntimeError):
    """Панель на паузе после отказа авторизации: запрос в сеть НЕ уходил."""


def _fingerprint() -> str:
    """Отпечаток адреса+ключа: сменил ключ/URL на «Ключи и доступы» — пауза по старым снимается."""
    return _md5(f"{settings.AAPANEL_URL}|{settings.AAPANEL_API_KEY}")


def blocked_reason() -> str | None:
    """Текст причины паузы (без секретов) или None, если панель открыта. Истёкшую паузу снимает."""
    with _block_lock:
        if not _block:
            return None
        left = _block["until"] - time.monotonic()
        if left <= 0 or _block["fp"] != _fingerprint():
            _block.clear()
            return None
        until = datetime.now(timezone.utc) + timedelta(seconds=left)
        hint = " — добавь IP в whitelist" if "ip validation" in _block["reason"].lower() else ""
        return f"{_block['reason']}{hint} · пауза до {until:%H:%M} UTC"


def require_open() -> None:
    """Префлайт для вызывающих (provision/publish): панель на паузе — упасть ДО побочных шагов
    (зона/DNS в Cloudflare), а не после, и без единого запроса в сеть."""
    why = blocked_reason()
    if why:
        raise AaPanelBlocked(f"aaPanel: {why}")


def reset_block() -> None:
    with _block_lock:
        _block.clear()


def _note_failure(msg: str) -> None:
    """Отказ авторизации в теле ответа -> включить паузу. Прочие отказы (нет прав на файл,
    «сайт уже есть») паузу не вызывают: они не копят счётчик банов."""
    low = msg.lower()
    if any(m in low for m in _BAN_MARKERS):
        pause = BAN_PAUSE_SEC
    elif any(m in low for m in _AUTH_MARKERS):
        pause = AUTH_PAUSE_SEC
    else:
        return
    with _block_lock:
        _block.update(until=time.monotonic() + pause, reason=msg[:90], fp=_fingerprint())


# Записи: повтор после ReadTimeout опасен (запрос мог дойти и исполниться — второй AddSite/
# DeleteSite/выпуск сертификата); ретраим только сбой СОЕДИНЕНИЯ, когда запрос не уходил.
# GetTaskCount (ping) — одна попытка: следующий цикл /diag и есть повтор, а 3×connect-таймаут
# держал бы поток дольше PING_TIMEOUT. Чтения (getData) — по общему правилу _is_retryable.
_WRITE_ACTIONS = ("AddSite", "DeleteSite", "SetSSL", "apply_cert_api", "CreateFile", "SaveFileBody", "AddDomain", "DeleteFile")


def _connect_only(exc: BaseException) -> bool:
    return isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout))


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


def tunnel_mode() -> bool:
    """AAPANEL_TUNNEL=1|true|yes|on. Значение с экрана ключей — СТРОКА, `bool("false")` было бы True."""
    return str(settings.AAPANEL_TUNNEL or "").strip().lower() in {"1", "true", "yes", "on"}


def _tunnel_host_ok(host: str) -> bool:
    """В туннельном режиме AAPANEL_URL обязан вести на ВНУТРЕННИЙ адрес (имя сервиса compose,
    loopback, частный IP, host.docker.internal). Публичный адрес = оператор включил флаг, но
    запросы пойдут в обход туннеля прямо в интернет и снова упрутся в whitelist (и копят бан)."""
    import ipaddress
    if host in {"localhost", "host.docker.internal"} or "." not in host:   # одно слово = имя сервиса
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private


class AaPanelClient(BaseClient):
    def __init__(self):
        if not settings.AAPANEL_URL:
            raise RuntimeError("AAPANEL_URL не задан — укажи адрес панели в .env / «Ключи и доступы»")
        # BaseClient.__init__ не зовём: он открыл бы лишний httpx.Client, который сразу
        # пришлось бы закрывать (S5-11) — нужен только base_url, клиент ставим ниже сами.
        self.base_url = settings.AAPANEL_URL.rstrip("/")
        self.api_sk = settings.AAPANEL_API_KEY
        # aaPanel serves a self-signed cert on :8888. Pin it via AAPANEL_CA_BUNDLE (copy
        # /www/server/panel/ssl/certificate.pem locally, set its path) to keep MITM
        # protection. FAIL CLOSED: for any NON-loopback panel we refuse to silently fall
        # back to verify=False — that's a MITM hole for a token-bearing client. verify=False
        # is tolerated only for a same-host 127.0.0.1 panel. Single client = single cookie jar.
        from urllib.parse import urlparse
        host = (urlparse(settings.AAPANEL_URL).hostname or "").lower()
        ca = getattr(settings, "AAPANEL_CA_BUNDLE", "") or ""
        self.tunnel = tunnel_mode()
        if self.tunnel:
            # Через туннель серт панели (CN=*.aapanel.com) не совпадёт с именем сервиса, поэтому
            # hostname-проверка выключена (см. ниже), и ЕДИНСТВЕННАЯ защита от подмены — пиннинг
            # по CA-файлу. Значит verify=False здесь недопустим даже для 127.0.0.1.
            if not _tunnel_host_ok(host):
                raise RuntimeError(
                    f"AAPANEL_TUNNEL=1, но AAPANEL_URL ведёт на внешний адрес {host!r} — туннель обойдён. "
                    "Укажи https://aapanel-tunnel:18839 (имя сервиса compose) или выключи AAPANEL_TUNNEL.")
            if not ca:
                raise RuntimeError(
                    "AAPANEL_TUNNEL=1 требует AAPANEL_CA_BUNDLE (certificate.pem панели) — без пиннинга "
                    "TLS через туннель не проверяется вовсе; verify=False в этом режиме запрещён.")
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
        elif host in {"127.0.0.1", "localhost", "::1"} and not self.tunnel:
            verify = False
        else:
            raise RuntimeError(
                f"aaPanel {host!r} is not loopback and AAPANEL_CA_BUNDLE is unset — refusing "
                "verify=False (MITM risk). Set AAPANEL_CA_BUNDLE to the panel's cert path.")
        # follow_redirects=False: never let a redirect carry the auth token to another host.
        # connect отдельно и короткий: лежащий VPS не должен держать вызов 30 с на каждую попытку (S5-10).
        self._client = httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0),
                                    follow_redirects=False, verify=verify)

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

        Ретрай ЗДЕСЬ, а не на BaseClient.request (S16, аудит 2026-07-18): подпись
        aaPanel (`_auth()`) живёт короткое окно свежести (clock-skew), а
        wait_exponential-backoff между попытками (до ~10с) мог успеть её состарить,
        если ретрай слал ТОТ ЖЕ payload, собранный один раз до первой попытки —
        транзиентный сетевой сбой тогда отклонялся панелью как auth-fail, и оператор
        шёл чинить несуществующий протухший api_sk. Каждая попытка заново строит
        payload -> свежий request_time/request_token. Вызываем
        `self._request_once` (БЕЗ собственного ретрая), а не `self.request` — иначе
        ретраи бы удвоились (3 попытки _post x 3 попытки request = 9).

        Перед сетью — предохранитель авторизации (`blocked_reason`): на паузе запрос не уходит.
        Записи (`_NO_RETRY_ACTIONS`) повторяются только при сбое соединения, не при ReadTimeout.
        """
        require_open()
        is_write = any(f"action={a}" in path for a in _WRITE_ACTIONS)
        attempts = 1 if "action=GetTaskCount" in path else 3
        for attempt in Retrying(stop=stop_after_attempt(attempts), wait=wait_exponential(multiplier=1, max=10),
                                retry=retry_if_exception(_connect_only if is_write else _is_retryable),
                                reraise=True):
            with attempt:
                payload = {**(data or {}), **self._auth()}
                resp = self._request_once("POST", f"{self.base_url}{path}", data=payload)
        try:
            res = resp.json()
        except ValueError:
            # HTML вместо JSON (security entrance, API выключен, прокси-страница): голый
            # JSONDecodeError «Expecting value: line 1 column 1» ничего не говорит (S5-17).
            snippet = " ".join(resp.text.split())[:120]
            raise RuntimeError(f"aaPanel {path.split('?')[0]}: ответ не JSON (HTTP {resp.status_code}): "
                               f"{snippet!r}") from None
        if isinstance(res, dict) and res.get("status") is False:
            _note_failure(str(res.get("msg") or ""))
        return res

    # -- system -------------------------------------------------------------

    def ping(self) -> bool:
        """Cheap health check: panel reachable, key valid, IP whitelisted.

        /ajax?action=GetTaskCount is the cheapest endpoint per docs/api/aapanel.md
        (returns a bare JSON integer, e.g. 0). Auth/whitelist failures come back as
        {"status": false, "msg": "..."}.

        Отказ НЕ глотается (S5-02/F8-01): причина летит исключением — «IP validation failed»,
        «prohibited for 1 hour», «Secret key verification failed», пауза предохранителя или
        «панель недоступна» — и /diag показывает её текстом, а не безликим fail.
        """
        try:
            res = self._post("/ajax?action=GetTaskCount")
        except httpx.HTTPError as e:
            hint = ""
            if getattr(self, "tunnel", False) and _connect_only(e):
                # туннель не поднят: нет ключа/known_hosts (сайдкар вышел сам) либо VPS не отвечает по SSH
                hint = (" — SSH-туннель не отвечает: проверь `docker compose --profile tunnel ps` и "
                        "`docker compose logs aapanel-tunnel` (ключ, known_hosts, доступ к VPS), "
                        "см. docs/v2/aapanel-tunnel-runbook.md")
            raise RuntimeError(f"aaPanel недоступна: {type(e).__name__} {e}".strip() + hint) from e
        if isinstance(res, dict):
            _ok(res, "GetTaskCount")
            return True
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

    def add_site(self, domain: str, path: str, php_version: str = "00", port: int = 80,
                 aliases: list[str] | None = None) -> dict:
        """Create an nginx vhost. version="00" = pure static (no PHP) — our default.

        Not idempotent by itself (duplicate => status/msg error); use ensure_site().
        Отказ (нет прав, «сайт уже есть», кончилось место) -> RuntimeError: раньше он
        возвращался обычным словарём, provision его не смотрел и объявлял сайт готовым.
        Успех приходит БЕЗ ключа `status` ({"siteStatus": true, ...}) — _ok его пропускает.

        `aliases` — доп. имена vhost'а (www.<домен>, S5-12): идут в `domainlist` webname. Форма
        поля `count` для непустого списка по докам не подтверждена живьём — берём число алиасов
        (так в аудите); пустой список — прежний проверенный вид (`[]`, 0).
        """
        al = list(aliases or [])
        return _ok(self._post(
            "/site?action=AddSite",
            {
                "webname": json.dumps({"domain": domain, "domainlist": al, "count": len(al)}),
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
        except (RuntimeError, httpx.TransportError):
            # TransportError — AddSite не ретраится на ReadTimeout (мог исполниться): панель
            # решает, есть ли сайт; нет — исходная ошибка летит наверх, следующий тик повторит.
            if not self.site_exists(domain):
                raise
            return {"exists": True, "name": domain}

    def add_domains(self, site_name: str, domains: list[str]) -> dict:
        """Дописать алиасы (www.<домен>) в УЖЕ существующий vhost: ensure_site у готового сайта
        aliases игнорирует. Отказ конверта -> RuntimeError.

        UNVERIFIED вживую (/site?action=AddDomain: id + webname + domain, несколько имён через
        перевод строки — по исходникам панели). Ответ несёт СПИСОК по каждому домену, а не общий
        status: «домен уже привязан» там норма, поэтому построчно не судим. Судья — не этот ответ,
        а проба провижна (маркер-файл по Host=алиас): подтвердила — алиас есть, нет — www без DNS."""
        site_id = next((s.get("id") for s in self.list_sites() if s.get("name") == site_name), None)
        if site_id is None:
            raise RuntimeError(f"aaPanel AddDomain: site not found: {site_name}")
        return _ok(self._post(
            "/site?action=AddDomain",
            {"id": site_id, "webname": site_name, "domain": "\n".join(domains)},
        ), "AddDomain")

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

        # Step 2 — deploy to the vhost.
        return self.set_ssl(site_name, key, cert)

    def set_ssl(self, site_name: str, key_pem: str, cert_pem: str) -> dict:
        """Положить готовый сертификат+ключ на vhost (SetSSL). Используется и ACME-веткой
        apply_ssl, и выпуском Cloudflare Origin CA (services/provisioning). Отказ -> RuntimeError.

        NB: PEM сертификата/цепочки идёт в поле `csr` (aaPanel misnomer — это сертификат, не запрос).
        Формат SetSSL по docs/api/aapanel.md ещё НЕ проверен вживую — поэтому выпуск Origin CA
        в провижне выключен флагом ORIGIN_CA_AUTO."""
        return _ok(self._post(
            "/site?action=SetSSL",
            {"type": 1, "siteName": site_name, "key": key_pem, "csr": cert_pem},
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
        поднять RuntimeError, глотать его нельзя»).

        DeleteSite не ретраится на ReadTimeout (S5-10): повтор после реально удалившего первого
        вызова дал бы ложное «сайт не найден». Таймаут -> спрашиваем панель: сайта нет — удалён."""
        try:
            return _ok(self._post(
                "/site?action=DeleteSite",
                {"id": site_id, "webname": site_name, "path": int(remove_dir),
                 "ftp": int(remove_ftp), "database": int(remove_db)},
            ), "DeleteSite")
        except httpx.TransportError:
            if self.site_exists(site_name):
                raise
            return {"status": True, "msg": "deleted (подтверждено списком сайтов после таймаута)"}

    # -- files (M5 deploy) --------------------------------------------------

    def delete_file(self, path: str) -> dict:
        """Удалить файл (маркер пробы провижна). UNVERIFIED вживую: /files?action=DeleteFile, поле path
        (по исходникам панели). Отказ конверта -> RuntimeError; вызывающий ловит его best-effort."""
        return _ok(self._post("/files?action=DeleteFile", {"path": path}), "DeleteFile")

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
