"""M3 — Provisioning. Cloudflare (zone -> NS -> SSL-режим -> proxied A) + aaPanel (vhost). Idempotent.

Every step checks-before-creates and stores ids on the Site, so re-running is safe.
The NS change at the registrar is external/async: the first run returns the CF name
servers to set (manually, or via reg.ru later); re-run once the zone is active to
finish DNS + vhost + SSL. See BUILD_SPEC §7 M3 and docs/PIPELINE.md.
"""
from datetime import datetime, timedelta, timezone

import secrets

import httpx

from app.config import settings

DOCROOT_BASE = "/www/wwwroot"  # ponytail: aaPanel default; make configurable if VPS layout differs


def docroot_for(domain: str) -> str:
    return f"{DOCROOT_BASE}/{domain}"


def create_site_for(domain_id: int) -> int:
    """Make a Site row for a purchased domain (idempotent). Returns site_id."""
    from sqlalchemy import select
    from app.db import SessionLocal
    from app.models.domain import Domain
    from app.models.site import Site

    with SessionLocal() as db:
        d = db.get(Domain, domain_id)
        if d is None:
            raise ValueError(f"domain {domain_id} not found")
        if d.status not in {"purchased", "live"}:    # только реально купленный домен
            raise ValueError("сайт можно создать только для купленного домена")
        site = db.execute(select(Site).where(Site.domain_id == domain_id)).scalar_one_or_none()
        if site is None:
            site = Site(domain_id=domain_id, status="provisioning",
                        origin_ip=settings.VPS_ORIGIN_IP or None, doc_root=docroot_for(d.domain))
            db.add(site)
            db.commit()
            db.refresh(site)
        return site.id


# --- origin: проба и сертификат ---------------------------------------------------------

NS_CHECK_EVERY = timedelta(hours=1)    # activation_check у CF ограничен по частоте (≈ раз в час на free)
NS_STALE_AFTER = timedelta(hours=48)   # pending-зона дольше — подсказываем проверить регистратора
PENDING_ZONE = {"pending", "initializing"}   # ждём NS; moved/deleted/deactivated — отдельная беда


def _origin_client() -> httpx.Client:
    """Клиент для проб origin по IP. verify=False сознательно: мы стучимся на голый IP со своим
    SNI/Host и проверяем только «отвечает ли vhost»; Origin CA/самоподписанный сертификат
    публичные корни всё равно не примут. Секретов в этих запросах нет. Фабрика — шов для тестов."""
    return httpx.Client(verify=False, timeout=httpx.Timeout(10.0, connect=5.0), follow_redirects=False)


def probe_origin(ip: str, domain: str, *, https: bool) -> tuple[bool, str]:
    """Финальная проверка шага (S4-02/S7-08): домен РАЗРЕШАЕТСЯ НА ORIGIN (минуя DNS и Cloudflare):
    GET <scheme>://<origin_ip>/ с Host=<домен> (и SNI=<домен> для https). Ок = ответ без 5xx.
    Ограничение: дефолтный vhost aaPanel на HTTP тоже отвечает 200 — но HTTPS на чужой Host он
    закрывает без ответа (S5-04), поэтому HTTPS-проба различает «наш vhost с сертификатом»."""
    ext = {"sni_hostname": domain} if https else {}
    try:
        with _origin_client() as c:
            r = c.get(f"{'https' if https else 'http'}://{ip}/", headers={"Host": domain}, extensions=ext)
    except httpx.HTTPError as e:
        return False, f"{type(e).__name__}: {e}"[:200]
    return r.status_code < 500, f"HTTP {r.status_code}"


def origin_exposure_warning(ip: str) -> str | None:
    """S5-14 (гард): origin по неизвестному Host не должен отвечать содержательно. Дефолтный vhost
    aaPanel отдаёт заглушку «Site is created successfully!» — отпечаток панели, по которому сканеры
    (Censys/Shodan) связывают IP со всем портфелем. Не блокирует провижн, только предупреждает:
    лечится ОДИН раз на VPS (default-vhost с 444 + файрвол 80/443 только для IP Cloudflare — см.
    docs/v2/origin-hardening-runbook.md), а не на каждом сайте."""
    ok, detail = probe_origin(ip, f"unlisted-{secrets.token_hex(4)}.invalid", https=False)
    if ok and detail.startswith("HTTP 2"):
        return (f"origin {ip} отвечает {detail} на неизвестный Host (дефолтная заглушка aaPanel) — "
                "сделай default-vhost 444 и файрвол 80/443 только для Cloudflare (runbook origin-hardening)")
    return None


def _key_and_csr(domain: str) -> tuple[str, str]:
    """Локальный ECDSA P-256 ключ + CSR (инвариант v2 №5: ключ рождается у нас и не покидает
    VPS — Cloudflare видит только CSR). Возвращает (key_pem, csr_pem)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    csr = (x509.CertificateSigningRequestBuilder()
           .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, domain)]))
           .add_extension(x509.SubjectAlternativeName(
               [x509.DNSName(domain), x509.DNSName(f"www.{domain}")]), critical=False)
           .sign(key, hashes.SHA256()))
    return (key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                              serialization.NoEncryption()).decode(),
            csr.public_bytes(serialization.Encoding.PEM).decode())


def _install_origin_cert(cf, ap, domain: str) -> None:
    """Origin CA на ЭТОТ домен (apex+www) -> SetSSL в aaPanel. Любой отказ -> исключение
    (вызывающий оставляет CF во flexible). Один сертификат на домен — не multi-SAN портфеля."""
    key_pem, csr_pem = _key_and_csr(domain)
    res = cf.create_origin_certificate(csr_pem, [domain, f"www.{domain}"]) or {}
    cert = res.get("certificate")
    if not cert:
        raise RuntimeError("Cloudflare Origin CA вернул ответ без certificate")
    ap.set_ssl(domain, key_pem, cert)


def _ensure_setting(cf, zone_id: str, sid: str, want, problems: list) -> None:
    """Идемпотентно: читаем, пишем только при расхождении. Сбой — в problems, а не исключением:
    рабочий vhost настройка зоны не роняет, но молча исчезать ей нельзя (как и ssl_error, F16)."""
    try:
        cur = (cf.get_zone_setting(zone_id, sid) or {}).get("value")
        if cur != want:
            cf.set_zone_setting(zone_id, sid, want)
    except Exception as e:  # noqa: BLE001
        problems.append(f"{sid}: {type(e).__name__}: {e}"[:200])


def _target_ssl_mode(origin_https: str | None) -> str:
    """Режим CF ВЫВОДИТСЯ из состояния origin (S4-02/S5-04/F8-09): full/strict без HTTPS на origin = 525."""
    return {"origin_ca": "strict", "ok": "full"}.get(origin_https or "", "flexible")


def _apply_ssl_mode(cf, zone_id: str, origin_https: str | None) -> str:
    """Привести режим SSL зоны в соответствие origin и вернуть итоговый режим.

    flexible — единственное, что безопасно без HTTPS на origin: понижаем и full/strict, и off
    (старый «не откатывать strict» (S6, 2026-07-18) касался только случая, когда origin ДОКАЗАННО
    отвечает по HTTPS — там strict по-прежнему не трогаем)."""
    want = _target_ssl_mode(origin_https)
    cur = (cf.get_zone_setting(zone_id, "ssl") or {}).get("value")
    if want == "full" and cur in ("full", "strict"):
        return cur                     # оператор осознанно поднял до strict — не откатываем
    if cur != want:
        cf.set_ssl(zone_id, want)
    return want


def _hours(since: datetime | None) -> int:
    if since is None:
        return 0
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    return int((datetime.now(timezone.utc) - since).total_seconds() // 3600)


def provision(site_id: int) -> dict:
    """Idempotent provision of one site. Re-run after setting NS at the registrar (the autopilot
    stage does it every sweep: pending-zone -> throttled activation_check, no blocking wait).

    Порядок шагов (каждый check-before-create, состояние — в Site.provision_step):
      zone -> await_ns -> [preflight aaPanel] -> vhost(+www) -> origin_tls -> режим SSL CF ->
      DNS (apex+www, proxied) -> verify (HTTP/HTTPS по IP origin) -> done (status=content).
    DNS идёт ПОСЛЕ vhost и режима SSL: трафик на домен появляется, когда origin уже готов и CF не
    в full над origin без :443 (иначе окно 525/дефолтной заглушки aaPanel)."""
    from app.db import SessionLocal
    from app.models.site import Site
    from app.models.domain import Domain
    from app.models.cloudflare import CloudflareZoneMirror
    from app.integrations.cloudflare import CloudflareClient
    from app.integrations.aapanel import AaPanelClient, require_open

    with SessionLocal() as db:
        site = db.get(Site, site_id)
        if site is None:
            raise ValueError(f"site {site_id} not found")
        d = db.get(Domain, site.domain_id)
        if d is None:
            raise ValueError(f"domain {site.domain_id} not found")
        domain = d.domain
        # ponytail: клиент CF — глобальный .env-токен/аккаунт. Мультиаккаунтный выбор
        # (CloudflareConnection.is_primary_for_provision + CloudflareClient.with_token) — отдельное
        # решение оператора (S4-03/S7-17: политика распределения, лимит новых зон); шов — здесь.
        cf = CloudflareClient()

        # 1. Cloudflare zone (idempotent: reuse if it already exists)
        site.provision_step = "zone"
        zone = cf.ensure_zone(domain)
        site.cf_zone_id = zone["id"]
        site.cloudflare_account_id = site.cloudflare_account_id or (
            getattr(cf, "account_id", "") or settings.CLOUDFLARE_ACCOUNT_ID or None)
        mirror = db.query(CloudflareZoneMirror).filter_by(cf_zone_id=zone["id"]).one_or_none()
        if mirror is not None:
            site.cf_zone_mirror_id = mirror.id
        db.commit()

        # 2. NS у регистратора — внешний шаг на часы. Статус из ensure_zone берём как есть (лишний
        # get_zone после него был впустую, S4-12); moved/deleted/deactivated — не «ждём NS», а отказ.
        status = zone.get("status")
        if status in PENDING_ZONE:
            now = datetime.now(timezone.utc)
            site.provision_step = "await_ns"
            site.cf_name_servers = zone.get("name_servers")
            site.ns_waiting_since = site.ns_waiting_since or now
            last = site.ns_checked_at
            if last is not None and last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            if last is None or now - last >= NS_CHECK_EVERY:
                # Просим CF перепроверить NS сейчас (иначе его плановый опрос — часы). Не чаще раза
                # в час; сбой не фатален — NS всё равно ждут человека. Автопилотный свип доезжает сюда
                # сам: ожидание — это повторные вызовы provision(), а не блокирующий цикл.
                site.ns_checked_at = now
                try:
                    cf.activation_check(zone["id"])
                    status = (cf.get_zone(zone["id"]) or {}).get("status", status)
                except Exception:  # noqa: BLE001
                    pass
            if status in PENDING_ZONE:
                db.commit()
                waited = _hours(site.ns_waiting_since)
                ns = ", ".join(zone.get("name_servers") or [])
                hint = f"пропиши у регистратора NS Cloudflare: {ns} — дальше провижн сам заметит активацию"
                if waited >= NS_STALE_AFTER.total_seconds() // 3600:
                    hint += (f". ⚠ Ждём уже {waited} ч — проверь, что NS у регистратора записаны верно "
                             "(pending-зона Cloudflare удаляется через неделю)")
                return {"status": "awaiting_ns", "domain": domain, "step": "await_ns",
                        "name_servers": zone.get("name_servers"), "waiting_hours": waited, "hint": hint}
        if status != "active":
            site.provision_step = "zone"
            db.commit()
            return {"status": "error", "domain": domain, "step": "zone",
                    "error": f"зона Cloudflare в статусе «{status}» (ждали active) — проверь зону в "
                             "дашборде: moved/deleted/deactivated сами не лечатся"}
        site.ns_waiting_since = None

        ip = settings.VPS_ORIGIN_IP
        if not ip:
            return {"status": "error", "domain": domain, "error": "VPS_ORIGIN_IP не задан"}

        # 3. aaPanel vhost (idempotent) + www-алиас.
        # ensure_site БОЛЬШЕ НЕ МОЛЧИТ: отказ панели (нет прав, кончилось место, протух api_sk)
        # приходил как HTTP 200 + {"status": false} и раньше проезжал мимо — сайт объявлялся
        # `content` без vhost'а, и человек шёл писать контент для несуществующего сайта (F14).
        # Теперь это RuntimeError, и он летит наверх НЕ ПОЙМАННЫМ — намеренно:
        #   · `site.status` остаётся `provisioning` — карточка не врёт, что инфраструктура готова;
        #   · провижн ИДЕМПОТЕНТЕН: зона CF уже сохранена (шаг 1 коммитит cf_zone_id), а до DNS мы
        #     ещё не дошли — CF-зона без vhost не получает трафика; ensure_zone/ensure_a_record —
        #     check-before-create, ensure_site пропускает уже созданный сайт. Оператор чинит причину,
        #     жмёт «Provision» ещё раз — и дело доезжает с того же места, ничего не дублируя.
        # Префлайт: панель на паузе (отказ авторизации/бан) — падаем ДО обращения к панели (S5-01).
        # Стоит ЗДЕСЬ, а не в начале: зона CF и выдача NS (awaiting_ns, шаг на часы) от панели не
        # зависят — пауза не должна лишать оператора списка NS для регистратора.
        site.provision_step = "vhost"
        db.commit()
        require_open()
        ap = AaPanelClient()
        root = site.doc_root or docroot_for(domain)
        ap.ensure_site(domain, root, aliases=[f"www.{domain}"])
        site.aapanel_site_name = domain
        site.doc_root = root
        db.commit()

        problems: list[str] = []   # сбои шагов, которые не роняют vhost, но не должны исчезать молча

        # 4. origin TLS: (опц.) наш Origin CA -> проба HTTPS по IP -> origin_https.
        site.provision_step = "origin_tls"
        if settings.ORIGIN_CA_AUTO and site.origin_https != "origin_ca":
            try:
                _install_origin_cert(cf, ap, domain)
                site.origin_https = "origin_ca"
            except Exception as e:  # noqa: BLE001 — без сертификата остаёмся во flexible, сайт жив
                problems.append(f"Origin CA: {type(e).__name__}: {e}"[:300])
        https_ok, https_detail = probe_origin(ip, domain, https=True)
        if https_ok:
            if site.origin_https != "origin_ca":
                site.origin_https = "ok"      # HTTPS отвечает, но сертификат не наш -> full, не strict
        else:
            if site.origin_https == "origin_ca":
                problems.append(f"Origin CA установлен, но HTTPS на origin не отвечает ({https_detail})")
            site.origin_https = "none"
        db.commit()

        # 5. режим SSL CF — из состояния origin; always_use_https/min_tls — идемпотентные шаги.
        # Заминка на edge-режиме НЕ роняет рабочий vhost, но и молча исчезать больше не смеет:
        # причина — в `site.ssl_error` (видна на карточке), успех затирает прошлую ошибку в NULL.
        ssl_mode = None
        try:
            ssl_mode = _apply_ssl_mode(cf, zone["id"], site.origin_https)
        except Exception as e:  # noqa: BLE001 — an SSL hiccup must not block a working vhost
            problems.append(f"ssl: {type(e).__name__}: {e}"[:300])
        _ensure_setting(cf, zone["id"], "always_use_https", "on", problems)
        _ensure_setting(cf, zone["id"], "min_tls_version", "1.2", problems)
        site.ssl_error = "; ".join(problems)[:500] or None

        # 6. DNS: proxied A на apex и www -> VPS origin (маскирует IP origin). Только теперь, когда
        # vhost и режим SSL готовы.
        site.provision_step = "dns"
        db.commit()
        cf.ensure_a_record(zone["id"], domain, ip, proxied=True)
        cf.ensure_a_record(zone["id"], f"www.{domain}", ip, proxied=True)

        # 7. Финальная проверка: домен на origin отвечает по HTTP (и по HTTPS, если заявлено).
        # Без неё «готов» было словом, а не фактом (S4-02/S7-08).
        site.provision_step = "verify"
        http_ok, http_detail = probe_origin(ip, domain, https=False)
        if not http_ok:
            db.commit()
            return {"status": "error", "domain": domain, "step": "verify",
                    "error": f"vhost не отвечает по HTTP на origin {ip} с Host={domain}: {http_detail} — "
                             "сайт не объявлен готовым, повтор провижна безопасен"}

        site.provision_step = "done"
        if site.status == "provisioning":     # published/monitoring НЕ откатываем в content (S6-12)
            site.status = "content"           # ready for M4
        db.commit()
        out = {"status": "provisioned", "domain": domain, "site_id": site.id,
               "cf_zone_id": site.cf_zone_id, "doc_root": root,
               "origin_https": site.origin_https, "ssl_mode": ssl_mode}
        warn = origin_exposure_warning(ip)
        if warn:
            out["warnings"] = [warn]
        if site.ssl_error:
            out["ssl_error"] = site.ssl_error
        return out


if __name__ == "__main__":  # pure helper self-check (no network/DB)
    assert docroot_for("example.ru") == "/www/wwwroot/example.ru"
    print("provisioning docroot_for ok")
