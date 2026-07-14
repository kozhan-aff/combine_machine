"""Cloudflare Control Center P0 — задача 7: read-only экран /settings/cloudflare.

Секции «Подключения» (capability-чипы, secret_ref-инструкция БЕЗ поля ввода токена) и
«Аккаунты и зоны» (наблюдённая правда, origin TLS/SSL «не проверено» ≠ фиктивное «ок»).
Ни одной формы, мутирующей Cloudflare — единственная форма отправляет на уже существующий
POST /settings/cloudflare/sync (задача 5/6)."""
from app.db import SessionLocal
from app.models.cloudflare import (
    CloudflareConnection, CloudflareConnectionAccount, CloudflareZoneMirror,
    CloudflareDnsRecordMirror, CloudflareCertificatePackMirror,
)


def test_cloudflare_tab_renders(client):
    r = client.get("/settings/cloudflare")
    assert r.status_code == 200
    assert "Подключения" in r.text and "Аккаунты и зоны" in r.text


def test_no_token_input_field(client):
    # secret_ref — инструкция, НЕ поле ввода токена в HTTP UI (аудит §13.1)
    r = client.get("/settings/cloudflare")
    assert 'name="token"' not in r.text and 'type="password"' not in r.text


def test_zone_table_shows_unknown_not_fake_clean(client):
    # зона с origin_tls_status=unknown показывается как «не проверено», не «ок»
    with SessionLocal() as db:
        db.add(CloudflareZoneMirror(cloudflare_account_id="acc", cf_zone_id="z1",
                                    name="x.ru", status="active", origin_tls_status="unknown"))
        db.commit()
    r = client.get("/settings/cloudflare")
    assert "x.ru" in r.text
    assert "не проверено" in r.text


def test_capability_chips_render(client):
    # capabilities_json живёт на connection-account, НЕ на connection — чипы обязаны появиться
    with SessionLocal() as db:
        c = CloudflareConnection(label="c1", secret_ref="env:CLOUDFLARE_API_TOKEN",
                                 token_kind="user", status="ok")
        db.add(c); db.commit()
        db.add(CloudflareConnectionAccount(connection_id=c.id, cloudflare_account_id="accHEX",
                                           status="ok", capabilities_json={"zones_read": "allowed"}))
        db.commit()
    r = client.get("/settings/cloudflare")
    assert "zones_read" in r.text


def test_zone_table_has_dns_cert_columns_and_ssl_value(client):
    # аудит §11: колонки DNS/cert существуют; universal_ssl_status доезжает в колонку SSL,
    # а не теряется из-за разъезда заголовков/ячеек
    with SessionLocal() as db:
        db.add(CloudflareZoneMirror(cloudflare_account_id="acc", cf_zone_id="zc1",
                                    name="cols.ru", status="active", universal_ssl_status="active"))
        db.add(CloudflareDnsRecordMirror(cloudflare_zone_id="zc1", cf_record_id="rec1",
                                         type="A", name="cols.ru"))
        db.add(CloudflareCertificatePackMirror(cloudflare_zone_id="zc1", cf_pack_id="pk1",
                                               status="active"))
        db.commit()
    r = client.get("/settings/cloudflare")
    assert ">DNS<" in r.text and ">cert<" in r.text
    assert "cols.ru" in r.text and "active" in r.text  # SSL-значение видно


def test_settings_page_links_to_cloudflare_tab(client):
    """Переключатель виден с обеих сторон (шаг 5 брифа) — не тупик."""
    r = client.get("/settings")
    assert '/settings/cloudflare' in r.text
