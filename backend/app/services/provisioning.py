"""M3 — Provisioning. Cloudflare (zone -> NS -> SSL-режим -> proxied A) + aaPanel (vhost). Idempotent.

Every step checks-before-creates and stores ids on the Site, so re-running is safe.
The NS change at the registrar is async: the first run pushes the CF name servers to the
registrar (NameSilo, when configured) and returns them for a manual fallback; re-run once the
zone is active to finish DNS + vhost + SSL. See BUILD_SPEC §7 M3 and docs/PIPELINE.md.
"""
from datetime import datetime, timedelta, timezone

import hashlib
import secrets

import httpx

from app.config import settings

DOCROOT_BASE = "/www/wwwroot"  # ponytail: aaPanel default; make configurable if VPS layout differs


def docroot_for(domain: str) -> str:
    return f"{DOCROOT_BASE}/{domain}"


def default_offer_id(db, domain) -> int | None:
    """Оффер по умолчанию для нового сайта — чтобы генерация не ждала ручной привязки (скрытый
    четвёртый гейт: стадия generate молча пропускала сайт без оффера). Правило простое и
    предсказуемое: активный оффер с языком рынка домена (`Domain.market_lang`), иначе —
    единственный активный оффер портфеля. Несколько кандидатов без совпадения языка — None:
    выбор за оператором (карточка сайта), машина не гадает. Оператор вправе переназначить."""
    from sqlalchemy import select
    from app.models.offer import Offer

    offers = db.execute(select(Offer).where(Offer.active.is_(True)).order_by(Offer.id)).scalars().all()
    lang = (getattr(domain, "market_lang", None) or "").strip().lower()
    if lang:
        same = [o for o in offers if (o.language or "").strip().lower() == lang]
        if same:
            return same[0].id
    return offers[0].id if len(offers) == 1 else None


def create_site_for(domain_id: int) -> int:
    """Make a Site row for a purchased domain (idempotent). Returns site_id.
    Оффер подставляется по умолчанию (default_offer_id), если сайт создаётся впервые."""
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
                        origin_ip=settings.VPS_ORIGIN_IP or None, doc_root=docroot_for(d.domain),
                        offer_id=default_offer_id(db, d))
            db.add(site)
            db.commit()
            db.refresh(site)
        return site.id


def _push_ns_to_registrar(domain: str, ns: list[str]) -> str | None:
    """Записать NS Cloudflare у регистратора через шов Registrar (NameSilo). Денег не тратит.
    Возвращает строку для подсказки оператору или None, если регистратор не настроен (тогда NS
    остаются ручным шагом — подсказка прежняя). Любой сбой (домен не в этом аккаунте, 301, сеть) —
    тоже строка, не исключение: ожидание NS не должно падать из-за регистратора."""
    if len(ns) < 2:
        return None
    try:
        from app.integrations.registrar import get_registrar
        r = get_registrar()
        if not getattr(r, "configured", False):
            return None
        out = r.set_nameservers(domain, ns)
        if out.get("verified"):
            return f"NS записаны у регистратора ({getattr(r, 'name', 'registrar')}), ждём активацию зоны Cloudflare"
        return (f"NS отправлены регистратору ({getattr(r, 'name', 'registrar')}), но сверка показала "
                f"{', '.join(out.get('nameservers') or []) or 'пусто'} — проверь в кабинете")
    except Exception as e:  # noqa: BLE001
        return f"NS у регистратора не записались ({type(e).__name__}: {str(e)[:120]}) — пропиши руками"


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
    """Грубая проба «хоть что-то отвечает» (без 5xx). Для ДОКАЗАТЕЛЬСТВА, что отвечает именно наш
    vhost, она не годится — см. probe_marker. Остаётся для origin_exposure_warning, где важен
    сам факт ответа на неизвестный Host."""
    ext = {"sni_hostname": domain} if https else {}
    try:
        with _origin_client() as c:
            r = c.get(f"{'https' if https else 'http'}://{ip}/", headers={"Host": domain}, extensions=ext)
    except httpx.HTTPError as e:
        return False, f"{type(e).__name__}: {e}"[:200]
    return r.status_code < 500, f"HTTP {r.status_code}"


NOT_OURS = "маркера нашего vhost нет"   # ответ есть, но это не наш сайт (дефолтный/чужой vhost)


def _new_nonce() -> str:
    return secrets.token_hex(12)       # шов для тестов: фикстура подменяет на константу


# Соль имени — константа, НЕ секрет панели: ключ aaPanel не должен влиять на публичный путь файла
# (смена ключа меняла бы имя, а производное от секрета в URL — лишняя утечка информации).
_MARKER_SALT = "cm-probe-v1"


def marker_name(domain: str) -> str:
    """Имя маркер-файла в docroot: стабильное для домена (повтор провижна перезаписывает один файл, а
    не копит их) и разное у сайтов (общий путь на весь портфель был бы отпечатком)."""
    h = hashlib.sha256(f"{domain}|{_MARKER_SALT}".encode()).hexdigest()[:12]
    return f"cm-probe-{h}.txt"


def probe_marker(ip: str, host: str, *, https: bool, name: str, nonce: str) -> tuple[bool, str]:
    """ДОКАЗАТЕЛЬСТВО, что Host=<host> на origin обслуживает НАШ vhost (S4-02/S7-08/S5-04): GET
    <scheme>://<ip>/<маркер> с Host (и SNI для https) и сверка ТЕЛА с nonce, который мы только что
    положили в docroot. Любой код без nonce — не наш сайт: при единственном ssl-vhost на :443 nginx
    отдаёт неизвестный SNI ему, а дефолтный vhost aaPanel на :80 отвечает 200 на любой Host — оба
    дали бы «успех» по коду ответа. 5xx/обрыв — отдельная причина."""
    ext = {"sni_hostname": host} if https else {}
    try:
        with _origin_client() as c:
            r = c.get(f"{'https' if https else 'http'}://{ip}/{name}", headers={"Host": host}, extensions=ext)
    except httpx.HTTPError as e:
        return False, f"{type(e).__name__}: {e}"[:200]
    if r.status_code == 200 and nonce in r.text:
        return True, "HTTP 200"
    if r.status_code < 500:
        return False, f"HTTP {r.status_code}, {NOT_OURS}"
    return False, f"HTTP {r.status_code}"


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
            ns_note = None
            if last is None or now - last >= NS_CHECK_EVERY:
                # Сначала сами записываем NS у регистратора (NameSilo changeNameServers; идемпотентно,
                # не настроен/не наш домен — просто подсказка человеку), потом просим CF перепроверить
                # NS сейчас (иначе его плановый опрос — часы). Не чаще раза в час; сбой не фатален.
                # Автопилотный свип доезжает сюда сам: ожидание — это повторные вызовы provision().
                site.ns_checked_at = now
                ns_note = _push_ns_to_registrar(domain, zone.get("name_servers") or [])
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
                if ns_note:
                    hint = f"{ns_note}; NS Cloudflare: {ns}"
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
        warnings: list[str] = []   # предупреждения оператору (не про SSL): www без алиаса, открытый origin

        # Маркер-файл: все пробы origin ниже сверяют его ТЕЛО (nonce), а не код ответа. Файл кладём
        # в docroot САЙТА: vhost с другим root/чужой vhost/дефолтная заглушка его не отдадут.
        # Сбой записи летит наверх, как отказ ensure_site (провижн идемпотентен, повтор безопасен).
        nonce, mname = _new_nonce(), marker_name(domain)
        ap.write_file(f"{root.rstrip('/')}/{mname}", nonce)
        www = f"www.{domain}"
        # www-алиас: у НОВОГО vhost он приходит в AddSite, у существующего (ensure_site возвращает
        # exists и aliases игнорирует) — дописываем через AddDomain. Судья — проба по Host=www:
        # DNS на www создаём только если алиас подтверждён (иначе посетитель получил бы чужой vhost).
        www_ok, www_detail = probe_marker(ip, www, https=False, name=mname, nonce=nonce)
        if not www_ok:
            try:
                ap.add_domains(domain, [www])
            except Exception as e:  # noqa: BLE001 — ручка не сверена вживую; решает повторная проба
                warnings.append(f"AddDomain {www}: {type(e).__name__}: {e}"[:200])
            www_ok, www_detail = probe_marker(ip, www, https=False, name=mname, nonce=nonce)
        if not www_ok:
            warnings.append(f"{www} не обслуживается vhost'ом на origin ({www_detail}) — A-запись www НЕ "
                            "создана; добавь домен-алиас в aaPanel и повтори провижн")

        # 4. origin TLS: (опц.) наш Origin CA -> проба HTTPS по IP -> origin_https.
        site.provision_step = "origin_tls"
        if settings.ORIGIN_CA_AUTO and site.origin_https != "origin_ca":
            try:
                _install_origin_cert(cf, ap, domain)
                site.origin_https = "origin_ca"
            except Exception as e:  # noqa: BLE001 — без сертификата остаёмся во flexible, сайт жив
                problems.append(f"Origin CA: {type(e).__name__}: {e}"[:300])
        # «ok» только с доказательством: по HTTPS на ЭТОТ Host+SNI отдаётся наш маркер. Иначе (в т.ч.
        # 200 от чужого/дефолтного ssl-vhost) режим не выше flexible — иначе посетители нового домена
        # увидели бы по HTTPS чужой сайт портфеля (footprint).
        https_ok, https_detail = probe_marker(ip, domain, https=True, name=mname, nonce=nonce)
        if https_ok:
            if site.origin_https != "origin_ca":
                site.origin_https = "ok"      # HTTPS наш, но сертификат не Origin CA -> full, не strict
        else:
            if site.origin_https == "origin_ca":
                problems.append(f"Origin CA установлен, но HTTPS на origin не отвечает ({https_detail})")
            elif NOT_OURS in https_detail:
                problems.append(f"HTTPS на origin отдаёт чужой/дефолтный vhost ({https_detail}) — CF "
                                "остаётся во flexible")
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
        if www_ok:
            cf.ensure_a_record(zone["id"], www, ip, proxied=True)

        # 7. Финальная проверка: наш vhost (маркер в теле) отвечает на Host=домен по HTTP.
        # Без неё «готов» было словом, а не фактом (S4-02/S7-08).
        site.provision_step = "verify"
        http_ok, http_detail = probe_marker(ip, domain, https=False, name=mname, nonce=nonce)
        if not http_ok:
            db.commit()
            return {"status": "error", "domain": domain, "step": "verify",
                    "error": f"наш vhost не отвечает по HTTP на origin {ip} с Host={domain}: {http_detail} — "
                             "сайт не объявлен готовым, повтор провижна безопасен"}

        # Проба прошла — маркер больше не нужен: публичный файл с nonce в docroot боевого сайта
        # (отпечаток) удаляем. Best-effort: сбой удаления не роняет провижн (имя стабильно,
        # повторный провижн перезапишет/удалит тот же файл).
        try:
            ap.delete_file(f"{root.rstrip('/')}/{mname}")
        except Exception as e:  # noqa: BLE001
            warnings.append(f"маркер {mname} не удалён из docroot: {type(e).__name__}: {e}"[:200])

        site.provision_step = "done"
        if site.status == "provisioning":     # published/monitoring НЕ откатываем в content (S6-12)
            site.status = "content"           # ready for M4
        db.commit()
        out = {"status": "provisioned", "domain": domain, "site_id": site.id,
               "cf_zone_id": site.cf_zone_id, "doc_root": root,
               "origin_https": site.origin_https, "ssl_mode": ssl_mode, "www": www_ok}
        warn = origin_exposure_warning(ip)
        if warn:
            warnings.append(warn)
        if warnings:
            out["warnings"] = warnings
        if site.ssl_error:
            out["ssl_error"] = site.ssl_error
        return out


if __name__ == "__main__":  # pure helper self-check (no network/DB)
    assert docroot_for("example.ru") == "/www/wwwroot/example.ru"
    print("provisioning docroot_for ok")
