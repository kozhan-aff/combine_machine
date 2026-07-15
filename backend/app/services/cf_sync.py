"""Cloudflare read-only sync: наблюдаем внешнюю правду в mirror-таблицы. Никаких мутаций CF,
никаких побочных эффектов на Domain/Site (unmanaged зона read-only). Пустой список при успехе —
missing_since, НЕ deleted; ошибка GET — last_error_safe без затирания прежнего значения."""
from datetime import datetime, timezone
from app.config import settings
from app.integrations.cloudflare import CloudflareClient
from app.services.cf_secret import resolve_secret_ref
from app.services import cf_legacy
from app.models.site import Site
from app.models.cloudflare import (
    CloudflareConnection, CloudflareAccount, CloudflareConnectionAccount,
    CloudflareCapabilityObservation, CloudflareZoneMirror,
    CloudflareZoneSettingObservation, CloudflareDnsRecordMirror,
    CloudflareCertificatePackMirror,
)

_OBSERVED_SETTINGS = ("ssl", "always_use_https", "min_tls_version", "tls_1_3", "http3",
                      "0rtt", "development_mode", "universal_ssl")  # per-setting GET, read-only


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
    # 3. verify по token_kind
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
            conn.last_error_safe = accounts_err
            _observe(db, conn.id, None, "user", None, "accounts_read", "denied", accounts_err)
    for a in accounts:
        acc_hex = a.get("id")
        if not acc_hex:
            continue
        acc_row = db.query(CloudflareAccount).filter_by(cf_account_id=acc_hex).one_or_none()
        if acc_row is None:
            acc_row = CloudflareAccount(cf_account_id=acc_hex)
            db.add(acc_row)
        acc_row.name = a.get("name", acc_row.name)
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
            for z in zones:
                # между зонами (не внутри — одна зона это один связный поход к деталям),
                # тот же контракт, что discovery._collect/orchestrator.run_sweep между
                # своими элементами. Одна connection может нести 51+ зону — без этой
                # проверки кнопка молчала бы, пока не переберёт их ВСЕ.
                if run is not None and jobs.cancelled(run):
                    raise jobs.Cancelled()
                m = _upsert_zone(db, acc_hex, z)
                seen.add(z["id"])
                _sync_zone_details(db, cf, m)     # 6. детали зоны
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
    """Детали одной зоны. Каждый GET в своём try — падение одного не рушит соседние."""
    zid = m.cf_zone_id
    # DNS-записи: upsert по cf_record_id, missing_since (не delete) для исчезнувших
    try:
        records = cf.list_dns_paginated(zid)
        seen = set()
        for rec in records:
            r = db.query(CloudflareDnsRecordMirror).filter_by(cf_record_id=rec["id"]).one_or_none()
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
    except Exception:
        pass  # ошибка DNS одной зоны — соседние зоны/детали не портим
    # per-setting наблюдения (batch endpoint deprecated — только по одному)
    for sid in _OBSERVED_SETTINGS:
        obs = (db.query(CloudflareZoneSettingObservation)
                 .filter_by(cloudflare_zone_id=zid, setting_id=sid).one_or_none())
        if obs is None:
            obs = CloudflareZoneSettingObservation(cloudflare_zone_id=zid, setting_id=sid)
            db.add(obs)
        try:
            s = cf.get_zone_setting(zid, sid)
            obs.value_json = s.get("value")
            obs.editable = s.get("editable")
            obs.status = "observed"
            obs.observed_at = _now()
            obs.error_safe = None
            if sid == "universal_ssl":  # зеркалим в zone mirror для SSL-колонки UI
                m.universal_ssl_status = _stringify(s.get("value"))
        except Exception as exc:
            obs.status = "error"
            obs.error_safe = _safe(exc)
            obs.observed_at = _now()
    # cert-паки
    try:
        packs = cf.list_universal_certificate_packs(zid)
        seen = set()
        for p in packs:
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
    except Exception:
        pass
    # dnssec
    try:
        d = cf.get_dnssec(zid)
        m.dnssec_status = d.get("status")
        m.dnssec_observed_at = _now()
        m.dnssec_error_safe = None
    except Exception as exc:
        m.dnssec_error_safe = _safe(exc)


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
