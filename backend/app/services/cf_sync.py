"""Cloudflare read-only sync: наблюдаем внешнюю правду в mirror-таблицы. Никаких мутаций CF,
никаких побочных эффектов на Domain/Site (unmanaged зона read-only). Пустой список при успехе —
missing_since, НЕ deleted; ошибка GET — last_error_safe без затирания прежнего значения."""
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import httpx

from app.config import settings
from app.integrations.cloudflare import CloudflareClient
from app.services.cf_secret import resolve_secret_ref
from app.services import cf_legacy
from app.services.cf_legacy import LEGACY_SECRET_REF
from app.models.site import Site
from app.models.cloudflare import (
    CloudflareConnection, CloudflareAccount, CloudflareConnectionAccount,
    CloudflareCapabilityObservation, CloudflareZoneMirror,
    CloudflareZoneSettingObservation, CloudflareDnsRecordMirror,
    CloudflareCertificatePackMirror,
)

_OBSERVED_SETTINGS = ("ssl", "always_use_https", "min_tls_version", "tls_1_3", "http3",
                      "0rtt", "development_mode")  # per-setting GET, read-only
# universal_ssl НЕ здесь: у него отдельный эндпоинт /ssl/universal/settings (аудит F1.2)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _safe(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"[:500]


def _stringify(v) -> str | None:
    if v is None:
        return None
    return v if isinstance(v, str) else str(v)


def _observe(db, conn_id, account_hex, resource_type, resource_id, capability, outcome, err=None):
    db.add(CloudflareCapabilityObservation(
        connection_id=conn_id, cloudflare_account_id=account_hex,
        resource_type=resource_type, resource_id=resource_id, capability=capability,
        outcome=outcome, safe_error=err))


def _upsert_zone(db, account_hex: str, z: dict) -> CloudflareZoneMirror:
    m = db.query(CloudflareZoneMirror).filter_by(cf_zone_id=z["id"]).one_or_none()
    if m is None:
        m = CloudflareZoneMirror(cf_zone_id=z["id"], cloudflare_account_id=account_hex,
                                 name=z.get("name", ""))
        db.add(m)
    m.cloudflare_account_id = account_hex
    m.name = z.get("name", m.name)
    m.status = z.get("status", "unknown")
    m.plan_name = (z.get("plan") or {}).get("name")
    m.paused = z.get("paused")
    ns = z.get("name_servers")
    if ns:
        m.name_servers_json = ns
    orig = z.get("original_name_servers")
    if orig:  # НЕ подменяем наблюдаемым authoritative NS — это отдельное поле
        m.original_name_servers_json = orig
    m.zones_seen_at = _now()
    m.missing_since = None
    m.last_error_safe = None
    return m


def _reconcile_missing(db, account_hex: str, seen_zone_ids: set) -> None:
    """Зоны, что были у аккаунта, но в успешном списке отсутствуют — missing_since, НЕ deleted.
    Omission доказывает только недоступность/пропажу; delete подтверждается лишь точечным GET
    (это уже P3). Здесь — консервативно: помечаем missing, статус не меняем на deleted."""
    rows = (db.query(CloudflareZoneMirror)
              .filter_by(cloudflare_account_id=account_hex).all())
    for m in rows:
        if m.cf_zone_id not in seen_zone_ids and m.missing_since is None:
            m.missing_since = _now()


# --- сетевая часть sync: параллельное чтение зон + лимитер + предохранители (S4-05/07/08) -----
_WORKERS = 5          # потоки чтения деталей зон (4-6): 20 зон x 3 чтения 28 с -> 5.6 с (замер аудита)
_RPS = 8.0            # запросов/с на connection (лимит CF 1200/5 мин = 4/с в среднем; burst покрывает малые sync)
_BURST = 20
_TRANSPORT_STOP = 3   # столько ПОДРЯД сбоев соединения — connection прерываем (как whois/safebrowsing в M1)


class _SyncAborted(Exception):
    """CF недоступен или лимит исчерпан — прогон connection прерван, остаток зон НЕ судим."""


class _Limiter:
    """Токен-бакет: общий на connection, потокобезопасный. rps<=0 — без ограничения."""

    def __init__(self, rps: float, burst: int):
        self.rps, self.burst = rps, float(burst)
        self.tokens, self.t = float(burst), time.monotonic()
        self.lock = threading.Lock()

    def acquire(self) -> None:
        if self.rps <= 0:
            return
        while True:
            with self.lock:
                now = time.monotonic()
                self.tokens = min(self.burst, self.tokens + (now - self.t) * self.rps)
                self.t = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                wait = (1 - self.tokens) / self.rps
            time.sleep(wait)


class _Gate:
    """Общее состояние чтения зон одной connection: лимитер, счётчик сбоев, отказанные способности."""

    def __init__(self):
        self.limiter = _Limiter(_RPS, _BURST)
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.abort_reason: str | None = None
        self.transport_fails = 0
        self.denied: set[str] = set()

    def call(self, fn, cap: str | None = None):
        """("ok", значение) | ("err", исключение) | ("skip", None) — способность уже отказана (403)."""
        if self.stop.is_set():
            return "err", _SyncAborted(self.abort_reason or "прервано")
        if cap and cap in self.denied:
            return "skip", None
        self.limiter.acquire()
        try:
            v = fn()
        except Exception as exc:  # noqa: BLE001 — ошибка зоны не рушит соседей
            self._note_error(exc, cap)
            return "err", exc
        with self.lock:
            self.transport_fails = 0
        return "ok", v

    def _note_error(self, exc: Exception, cap: str | None) -> None:
        code = getattr(getattr(exc, "response", None), "status_code", None)
        with self.lock:
            if isinstance(exc, httpx.TransportError):
                self.transport_fails += 1
                if self.transport_fails >= _TRANSPORT_STOP and not self.stop.is_set():
                    self.abort_reason = (f"Cloudflare недоступен: {self.transport_fails} сбоев "
                                         f"соединения подряд ({type(exc).__name__})")
                    self.stop.set()
                return
            self.transport_fails = 0
            if code == 429 and not self.stop.is_set():
                ra = exc.response.headers.get("Retry-After")
                self.abort_reason = ("Cloudflare: лимит запросов (429)"
                                     + (f", повтор через {ra} с" if ra else ""))
                self.stop.set()
            elif code == 403 and cap:
                self.denied.add(cap)   # токен без права на этот эндпоинт — дальше не стучимся


def _fetch_zone_details(cf, zid: str, gate: _Gate) -> dict:
    """Все чтения деталей ОДНОЙ зоны (сеть, без БД — безопасно в потоке). Каждое в своём try:
    падение одного не рушит соседние."""
    # lambda: атрибут клиента разыменовывается ВНУТРИ gate.call (его try), не при сборке аргументов
    d = {"dns": gate.call(lambda: cf.list_dns_paginated(zid)), "settings": {}}
    for sid in _OBSERVED_SETTINGS:
        d["settings"][sid] = gate.call(lambda sid=sid: cf.get_zone_setting(zid, sid))
    d["universal"] = gate.call(lambda: cf.get_universal_ssl(zid), cap="universal_ssl")
    d["packs"] = gate.call(lambda: cf.list_universal_certificate_packs(zid), cap="cert_packs")
    d["dnssec"] = gate.call(lambda: cf.get_dnssec(zid))
    return d


def _skip_msg(cap: str) -> str:
    return f"пропущено: токен без прав на {cap} (403 раньше в этом прогоне)"


def sync_connection(db, conn: CloudflareConnection, *, run=None) -> None:
    """run — id прогона job-реестра (jobs.track), НЕ обязателен: без него (юнит-тесты,
    прямой вызов) отмена просто не проверяется. С ним — между зонами внутри аккаунта, где и
    копится основное время (F18-паттерн: без этой проверки кнопка «✕ Отменить» была тихим
    no-op — sync доезжал до конца независимо от неё, см. audit-fixes Задача 12)."""
    from app.services import jobs
    # 1. secret_ref → токен (значение секрета никогда не в last_error_safe)
    try:
        token = resolve_secret_ref(conn.secret_ref)
    except Exception as exc:
        conn.status = "error"
        conn.last_error_safe = _safe(exc)
        db.commit()
        return
    # 2. клиент с ЯВНЫМ токеном (не из глобального singleton)
    cf = CloudflareClient.with_token(token, conn.owner_cf_account_id or "")
    # 3. verify по token_kind (verify_token сам отвергает status != active — S4-10)
    try:
        cf.verify_token(conn.token_kind, conn.owner_cf_account_id or "")
        conn.status = "ok"
        conn.verified_at = _now()
        conn.last_error_safe = None
        _observe(db, conn.id, conn.owner_cf_account_id, "account", conn.owner_cf_account_id,
                 "token_active", "allowed")
    except Exception as exc:
        conn.status = "error"
        conn.last_error_safe = _safe(exc)
        _observe(db, conn.id, conn.owner_cf_account_id, "account", conn.owner_cf_account_id,
                 "token_active", "denied", _safe(exc))
        db.commit()
        return
    # 4. аккаунты: user-токен листает все; account-токен знает ровно свой один
    if conn.token_kind == "account":
        accounts, accounts_ok, accounts_err = [{"id": conn.owner_cf_account_id, "name": None}], True, None
    else:
        try:
            accounts, accounts_ok, accounts_err = cf.list_accounts_paginated(), True, None
        except Exception as exc:
            accounts, accounts_ok, accounts_err = [], False, _safe(exc)
            # verify_token прошёл (conn.status="ok"), но листинг аккаунтов упал — без явного
            # error-статуса settings_cloudflare.html показывает зелёный «ok» и НЕ рендерит
            # last_error_safe (он виден только при status=='error'), и отказ синка полностью
            # невидим: ни одной зоны не наблюдалось, а оператор думает, что всё синхронно.
            # Ставим error как на уровне аккаунта/зоны ниже (acc_row.status="error").
            conn.status = "error"
            conn.last_error_safe = accounts_err
            _observe(db, conn.id, None, "user", None, "accounts_read", "denied", accounts_err)
        else:
            if not accounts:
                # S4-01: боевой user-токен без Account:Read получает GET /accounts -> 200 [] — цикл
                # по аккаунтам не выполнялся, а connection оставалась зелёной. Это деградация, а не
                # «аккаунтов нет»: работаем по account_id из connection/настроек (если он известен)
                # и честно предупреждаем.
                accounts_ok = False
                fallback = conn.owner_cf_account_id or (
                    settings.CLOUDFLARE_ACCOUNT_ID if conn.secret_ref == LEGACY_SECRET_REF else "")
                accounts = [{"id": fallback, "name": None}] if fallback else []
                conn.status = "warn"
                conn.last_error_safe = (
                    "0 аккаунтов: GET /accounts пуст (токену не хватает Account:Read)"
                    + ("; зоны читаются по account_id из настроек" if fallback else
                       "; account_id не задан (CLOUDFLARE_ACCOUNT_ID) — зоны не читаются"))
                _observe(db, conn.id, None, "user", None, "accounts_read", "denied",
                         conn.last_error_safe)
    gate = _Gate()
    for a in accounts:
        acc_hex = a.get("id")
        if not acc_hex:
            continue
        acc_row = db.query(CloudflareAccount).filter_by(cf_account_id=acc_hex).one_or_none()
        if acc_row is None:
            acc_row = CloudflareAccount(cf_account_id=acc_hex)
            db.add(acc_row)
        acc_row.name = a.get("name") or acc_row.name
        acc_row.last_synced_at = _now()
        ca = (db.query(CloudflareConnectionAccount)
                .filter_by(connection_id=conn.id, cloudflare_account_id=acc_hex).one_or_none())
        if ca is None:
            ca = CloudflareConnectionAccount(connection_id=conn.id, cloudflare_account_id=acc_hex)
            db.add(ca)
        ca.last_probed_at = _now()
        caps = {"token_active": "allowed",
                "accounts_read": "allowed" if accounts_ok else "denied"}
        if accounts_ok:
            _observe(db, conn.id, acc_hex, "account", acc_hex, "accounts_read", "allowed")
        # 5. зоны аккаунта
        try:
            zones = cf.list_zones_paginated(acc_hex)
            caps["zones_read"] = "allowed"
            _observe(db, conn.id, acc_hex, "account", acc_hex, "zones_read", "allowed")
            seen = set()
            # Сетевое чтение деталей — в пуле (S4-05), запись в БД — здесь, в основном потоке,
            # по порядку зон и с commit на зону: ошибка/отмена посреди списка не теряет сделанное.
            with ThreadPoolExecutor(max_workers=_WORKERS) as ex:
                futs = [(z, ex.submit(_fetch_zone_details, cf, z["id"], gate)) for z in zones]
                try:
                    for z, fut in futs:
                        # между зонами (не внутри — одна зона это один связный поход к деталям),
                        # тот же контракт, что discovery._collect/orchestrator.run_sweep между
                        # своими элементами. Одна connection может нести 51+ зону — без этой
                        # проверки кнопка молчала бы, пока не переберёт их ВСЕ.
                        details = fut.result()
                        if run is not None and jobs.cancelled(run):
                            raise jobs.Cancelled()
                        if gate.stop.is_set():
                            break
                        m = _upsert_zone(db, acc_hex, z)
                        seen.add(z["id"])
                        _apply_zone_details(db, m, details)     # 6. детали зоны
                        db.commit()
                finally:
                    for _, f in futs:
                        f.cancel()
            if gate.stop.is_set():
                raise _SyncAborted(gate.abort_reason)
            _reconcile_missing(db, acc_hex, seen)  # пусто/пропажа → missing_since, НЕ deleted
            acc_row.status = "ok"
            acc_row.last_error_safe = None
            ca.status = "ok"
            ca.last_error_safe = None
        except jobs.Cancelled:
            # сохранить то, что успели отнаблюдать до отмены — не откатывать честную работу
            ca.capabilities_json = caps
            db.commit()
            raise
        except _SyncAborted as ab:
            # прерывание (CF лёг / 429): остаток зон не наблюдён — reconcile НЕ делаем (иначе
            # необойдённые зоны получили бы missing_since); connection краснеет с причиной.
            conn.status = "error"
            conn.last_error_safe = str(ab)[:500]
            ca.status = "error"
            ca.last_error_safe = str(ab)[:500]
            ca.capabilities_json = caps
            break
        except Exception as exc:
            caps["zones_read"] = "denied"
            acc_row.status = "error"
            acc_row.last_error_safe = _safe(exc)
            ca.status = "error"
            ca.last_error_safe = _safe(exc)
            _observe(db, conn.id, acc_hex, "account", acc_hex, "zones_read", "unknown", _safe(exc))
            # НЕ трогаем существующие zone mirrors — omission ≠ deleted
        ca.capabilities_json = caps
    db.commit()  # 7.


def _sync_zone_details(db, cf, m: CloudflareZoneMirror) -> None:
    """Детали ОДНОЙ зоны подряд (чтение + запись) — для точечного вызова и тестов."""
    _apply_zone_details(db, m, _fetch_zone_details(cf, m.cf_zone_id, _Gate()))


def _apply_zone_details(db, m: CloudflareZoneMirror, d: dict) -> None:
    """Записать прочитанные `_fetch_zone_details` детали зоны в зеркала (основной поток, БД)."""
    zid = m.cf_zone_id
    # DNS-записи: upsert по cf_record_id, missing_since (не delete) для исчезнувших
    kind, val = d["dns"]
    if kind == "ok":
        seen = set()
        # ОДИН SELECT на зону вместо N+1 по записи (S4-05)
        known = {r.cf_record_id: r for r in db.query(CloudflareDnsRecordMirror)
                 .filter(CloudflareDnsRecordMirror.cf_record_id.in_([x["id"] for x in val])).all()} \
            if val else {}
        for rec in val:
            r = known.get(rec["id"])
            if r is None:
                r = CloudflareDnsRecordMirror(cf_record_id=rec["id"], cloudflare_zone_id=zid)
                db.add(r)
            r.cloudflare_zone_id = zid
            r.type = rec.get("type", "")
            r.name = rec.get("name", "")
            r.content = rec.get("content")
            r.ttl = rec.get("ttl")
            r.proxied = rec.get("proxied")
            r.observed_at = _now()
            r.missing_since = None
            r.last_error_safe = None
            # managed_role НЕ выставляем — apex_origin только в M3/adoption
            seen.add(rec["id"])
        for r in db.query(CloudflareDnsRecordMirror).filter_by(cloudflare_zone_id=zid).all():
            if r.cf_record_id not in seen and r.missing_since is None:
                r.missing_since = _now()
        m.dns_error_safe = None
    else:
        m.dns_error_safe = _safe(val)   # не глотать: соседние GET не портим, но правда видна (F1.3)
    # per-setting наблюдения (batch endpoint deprecated — только по одному)
    obs_rows = {o.setting_id: o for o in
                db.query(CloudflareZoneSettingObservation).filter_by(cloudflare_zone_id=zid).all()}
    for sid, (kind, val) in d["settings"].items():
        obs = obs_rows.get(sid)
        if obs is None:
            obs = CloudflareZoneSettingObservation(cloudflare_zone_id=zid, setting_id=sid)
            db.add(obs)
        if kind == "ok":
            obs.value_json = val.get("value")
            obs.editable = val.get("editable")
            obs.status = "observed"
            obs.observed_at = _now()
            obs.error_safe = None
        else:
            obs.status = "error"
            obs.error_safe = _safe(val) if kind == "err" else _skip_msg("settings")
            obs.observed_at = _now()
    # Universal SSL — отдельный эндпоинт, не общий settings-цикл (аудит F1.2)
    kind, val = d["universal"]
    if kind == "ok":
        m.universal_ssl_status = "on" if val.get("enabled") else "off"
    else:
        # НЕ затираем прежний статус ошибкой — фиксируем на уровне зоны (см. F1.3)
        m.last_error_safe = _safe(val) if kind == "err" else _skip_msg("ssl/universal")
    # cert-паки
    kind, val = d["packs"]
    if kind == "ok":
        seen = set()
        for p in val:
            pm = db.query(CloudflareCertificatePackMirror).filter_by(cf_pack_id=p["id"]).one_or_none()
            if pm is None:
                pm = CloudflareCertificatePackMirror(cf_pack_id=p["id"], cloudflare_zone_id=zid)
                db.add(pm)
            pm.cloudflare_zone_id = zid
            pm.type = p.get("type")
            pm.status = p.get("status")
            pm.hosts_json = p.get("hosts")
            pm.certificates_json = p.get("certificates")
            pm.observed_at = _now()
            pm.missing_since = None
            seen.add(p["id"])
        for pm in db.query(CloudflareCertificatePackMirror).filter_by(cloudflare_zone_id=zid).all():
            if pm.cf_pack_id not in seen and pm.missing_since is None:
                pm.missing_since = _now()
        m.cert_error_safe = None
    else:
        m.cert_error_safe = _safe(val) if kind == "err" else _skip_msg("certificate_packs")   # аудит F1.3
    # dnssec
    kind, val = d["dnssec"]
    if kind == "ok":
        m.dnssec_status = val.get("status")
        m.dnssec_observed_at = _now()
        m.dnssec_error_safe = None
    else:
        m.dnssec_error_safe = _safe(val)


def _backfill_site_links(db) -> None:
    """§2.6 backfill legacy-Site: связываем существующие Site.cf_zone_id с наблюдённым mirror и
    выставляем desired cloudflare_account_id. Идемпотентно, только для Site с непустым cf_zone_id.
    Инвариант: join строго по совпадению cf_zone_id (legacy == mirror) — иначе связь не ставим."""
    for s in db.query(Site).filter(Site.cf_zone_id.isnot(None)).all():
        if not s.cf_zone_id:
            continue
        m = db.query(CloudflareZoneMirror).filter_by(cf_zone_id=s.cf_zone_id).one_or_none()
        if m is not None:
            s.cf_zone_mirror_id = m.id
            if not s.cloudflare_account_id:
                s.cloudflare_account_id = m.cloudflare_account_id
        elif not s.cloudflare_account_id and settings.CLOUDFLARE_ACCOUNT_ID:
            s.cloudflare_account_id = settings.CLOUDFLARE_ACCOUNT_ID


def sync_all(db, *, report=None, run=None) -> dict:
    """run — id прогона job-реестра (см. panel.py::cloudflare_sync); пробрасываем в
    sync_connection, чтобы кнопка «✕ Отменить» слушалась И между connections, И между зонами
    внутри одной (обычно connections 1-2, зон внутри — 51+, без внутреннего чека кнопка была
    тихим no-op при типичной топологии)."""
    from app.services import jobs
    cf_legacy.import_legacy_connection(db)
    conns = db.query(CloudflareConnection).all()
    done = 0
    for conn in conns:
        # ПЕРЕД тяжёлой работой (sync_connection) для КАЖДОЙ connection, не после — иначе
        # последняя connection всё равно отработает вхолостую (тот же баг, что F18).
        if run is not None and jobs.cancelled(run):
            raise jobs.Cancelled()
        if report:
            report(done=done, total=len(conns), current=conn.label, stage="verify")
        sync_connection(db, conn, run=run)
        done += 1
    _backfill_site_links(db)  # §2.6: зоны уже наблюдены — можно связать legacy-Site
    db.commit()
    if report:
        report(done=done, total=len(conns), stage="zones")
    return {"connections": len(conns)}
