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
