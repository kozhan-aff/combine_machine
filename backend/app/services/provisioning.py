"""M3 — Provisioning. Cloudflare (zone -> NS -> proxied A -> SSL) + aaPanel (vhost). Idempotent.

Every step checks-before-creates and stores ids on the Site, so re-running is safe.
The NS change at the registrar is external/async: the first run returns the CF name
servers to set (manually, or via reg.ru later); re-run once the zone is active to
finish DNS + vhost + SSL. See BUILD_SPEC §7 M3 and docs/PIPELINE.md.
"""
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


def provision(site_id: int) -> dict:
    """Idempotent provision of one site. Re-run after setting NS at the registrar."""
    from app.db import SessionLocal
    from app.models.site import Site
    from app.models.domain import Domain
    from app.integrations.cloudflare import CloudflareClient
    from app.integrations.aapanel import AaPanelClient

    with SessionLocal() as db:
        site = db.get(Site, site_id)
        if site is None:
            raise ValueError(f"site {site_id} not found")
        d = db.get(Domain, site.domain_id)
        if d is None:
            raise ValueError(f"domain {site.domain_id} not found")
        domain = d.domain
        cf = CloudflareClient()

        # 1. Cloudflare zone (idempotent: reuse if it already exists)
        zone = cf.ensure_zone(domain)
        site.cf_zone_id = zone["id"]
        db.commit()

        # 2. NS must be active before anything else (external step at the registrar)
        if zone.get("status") != "active":
            zone = cf.get_zone(zone["id"])
        if zone.get("status") != "active":
            return {"status": "awaiting_ns", "domain": domain,
                    "name_servers": zone.get("name_servers"),
                    "hint": "пропиши эти NS у регистратора, потом повтори provision()"}

        # 3. Proxied A record -> VPS origin (masks the origin IP)
        ip = settings.VPS_ORIGIN_IP
        if not ip:
            return {"status": "error", "domain": domain, "error": "VPS_ORIGIN_IP не задан"}
        cf.ensure_a_record(zone["id"], domain, ip, proxied=True)

        # 4. aaPanel vhost (idempotent).
        # ensure_site БОЛЬШЕ НЕ МОЛЧИТ: отказ панели (нет прав, кончилось место, протух api_sk)
        # приходил как HTTP 200 + {"status": false} и раньше проезжал мимо — сайт объявлялся
        # `content` без vhost'а, и человек шёл писать контент для несуществующего сайта (F14).
        # Теперь это RuntimeError, и он летит наверх НЕ ПОЙМАННЫМ — намеренно:
        #   · `site.status` остаётся `provisioning` (commit ниже не выполнится, сессия
        #     откатится) — карточка не врёт, что инфраструктура готова;
        #   · провижн ИДЕМПОТЕНТЕН, значит недоделанное доводит повтор: зона CF уже сохранена
        #     (шаг 1 коммитит cf_zone_id), ensure_zone/ensure_a_record — check-before-create,
        #     ensure_site пропускает уже созданный сайт. Оператор чинит причину, жмёт
        #     «Provision» ещё раз — и дело доезжает с того же места, ничего не дублируя.
        ap = AaPanelClient()
        root = site.doc_root or docroot_for(domain)
        ap.ensure_site(domain, root)
        site.aapanel_site_name = domain
        site.doc_root = root

        # 5. SSL: Cloudflare edge 'full' now; upgrade to 'strict' once origin cert is valid.
        # Заминка на edge-режиме НЕ роняет рабочий vhost (сайт на :80 уже поднят) — но и молча
        # исчезать больше не смеет: при origin, слушающем только :80, ровно этот режим решает,
        # поедет ли HTTPS. Пишем причину в `site.ssl_error` (её видно на карточке сайта) и
        # отдаём наверх, а не в `pass`. Успех затирает прошлую ошибку в NULL — починенное не
        # должно висеть обвинением.
        ssl_error = None
        try:
            current = cf.get_zone_setting(zone["id"], "ssl")
            current_mode = (current or {}).get("value")
            if current_mode not in ("full", "strict"):
                cf.set_ssl(zone["id"], "full")
            # уже "full" или "strict" — ничего не трогаем: idempotent no-op, и режим,
            # который оператор осознанно поднял до "strict" после установки origin-
            # сертификата, больше НЕ откатывается тихо обратно на "full" при повторном
            # provision() (S6, аудит 2026-07-18).
        except Exception as e:  # noqa: BLE001  # an SSL hiccup must not block a working vhost
            ssl_error = f"{type(e).__name__}: {e}"[:500]
        site.ssl_error = ssl_error

        site.status = "content"   # ready for M4
        db.commit()
        out = {"status": "provisioned", "domain": domain, "site_id": site.id,
               "cf_zone_id": site.cf_zone_id, "doc_root": root}
        if ssl_error:
            out["ssl_error"] = ssl_error
        return out


if __name__ == "__main__":  # pure helper self-check (no network/DB)
    assert docroot_for("example.ru") == "/www/wwwroot/example.ru"
    print("provisioning docroot_for ok")
