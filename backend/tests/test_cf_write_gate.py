"""Задача 6 (Cloudflare P0): server-side гейт для будущих CF-мутаций.

_require_cf_write(request) -> None поднимает 403, если PANEL_USER/PANEL_PASS не настроены
(аудит §11/§15 — same-origin недостаточен для мутаций на LAN-панели). В P0 единственный
CF-write — запуск sync-джоба (POST /settings/cloudflare/sync), а этот роут — Задача 5, которая
по явному порядку плана (docs/superpowers/plans/2026-07-14-cloudflare-p0.md, Задача 5 header)
исполняется ПОСЛЕ этой задачи и сама зовёт _require_cf_write. Поэтому гейт здесь проверяется
напрямую на уровне функции, а не через ещё не существующий HTTP-роут (эмпирически: POST на
/settings/cloudflare/sync сейчас отдаёт 404 «Not Found» независимо от PANEL_USER/PANEL_PASS —
это не то поведение, которое доказывает гейт). Прямой вызов честен: сигнатура `request: Request`
в реализации не используется вовсе (задел под будущие роуты), так что `None` вместо реального
Request ничего не подделывает — контракт целиком читается из `settings`.
"""
import pytest
from fastapi import HTTPException

from app.api import panel
from app.config import settings


def test_blocked_when_both_empty(monkeypatch):
    monkeypatch.setattr(settings, "PANEL_USER", "")
    monkeypatch.setattr(settings, "PANEL_PASS", "")
    with pytest.raises(HTTPException) as exc:
        panel._require_cf_write(None)
    assert exc.value.status_code == 403


@pytest.mark.parametrize("user, password", [("u", ""), ("", "p")])
def test_blocked_when_only_one_side_set(monkeypatch, user, password):
    """Половинчатая настройка (забыли одну из двух .env-переменных) — тоже отказ, не пропуск."""
    monkeypatch.setattr(settings, "PANEL_USER", user)
    monkeypatch.setattr(settings, "PANEL_PASS", password)
    with pytest.raises(HTTPException) as exc:
        panel._require_cf_write(None)
    assert exc.value.status_code == 403


def test_allowed_when_both_set(monkeypatch):
    monkeypatch.setattr(settings, "PANEL_USER", "u")
    monkeypatch.setattr(settings, "PANEL_PASS", "p")
    panel._require_cf_write(None)  # не поднимает исключение


# ===========================================================================
# S8 (аудит 2026-07-18): POST /sites/{id}/provision — НАСТОЯЩАЯ CF-мутация
# (зона + DNS + SSL-режим боевым токеном), которая до этого фикса не звала
# _require_cf_write вовсе — гейт висел только на read-only /settings/cloudflare/sync.
# Тесты ниже — первый end-to-end (HTTP-уровневый) потребитель гейта на этом роуте,
# по образцу test_cf_job.py::test_cf_sync_route_requires_configured_panel_auth.
# ===========================================================================
def _seed_provisioning_site() -> int:
    from app.db import SessionLocal
    from app.models.domain import Domain
    from app.models.site import Site
    with SessionLocal() as s:
        d = Domain(domain="gate-provision-test.ru", source="backorder", status="purchased")
        s.add(d)
        s.commit()
        s.refresh(d)
        site = Site(domain_id=d.id, status="provisioning",
                    doc_root="/www/wwwroot/gate-provision-test.ru")
        s.add(site)
        s.commit()
        s.refresh(site)
        return site.id


def test_provision_route_blocked_without_configured_auth(client, monkeypatch):
    """Без настроенных PANEL_USER/PANEL_PASS (дефолт autouse _no_panel_auth) запрос обязан
    остановиться на 403 ДО похода в provisioning.provision — сентинел ниже роняет тест, если
    гейт всё же пропустил вызов дальше."""
    import app.services.provisioning as provisioning

    def _boom(site_id):
        raise AssertionError("provisioning.provision() не должен вызываться — гейт обязан "
                             "остановить запрос раньше")
    monkeypatch.setattr(provisioning, "provision", _boom)
    sid = _seed_provisioning_site()

    monkeypatch.setattr(settings, "PANEL_USER", "")
    monkeypatch.setattr(settings, "PANEL_PASS", "")
    r = client.post(f"/sites/{sid}/provision", follow_redirects=False)
    assert r.status_code == 403


def test_provision_route_proceeds_with_configured_auth(client, monkeypatch):
    """С настроенными кредами гейт пропускает — запрос доходит до provisioning.provision()
    (застабленного здесь: реальный вызов упёрся бы в живые CF/aaPanel креды, которых в тестовом
    окружении нет — сути гейта это не касается, доказываем только что 403 не сработал)."""
    import app.services.provisioning as provisioning

    monkeypatch.setattr(provisioning, "provision", lambda site_id: {"status": "provisioned"})
    sid = _seed_provisioning_site()

    monkeypatch.setattr(settings, "PANEL_USER", "u")
    monkeypatch.setattr(settings, "PANEL_PASS", "p")
    r = client.post(f"/sites/{sid}/provision", auth=("u", "p"), follow_redirects=False)
    assert r.status_code == 303  # редирект на карточку сайта — гейт НЕ остановил запрос на 403
