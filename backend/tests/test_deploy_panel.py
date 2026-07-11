"""Хендлеры деплоя через TestClient: мокаем deploy-сервис, проверяем баннеры/редиректы/рендер."""
from urllib.parse import unquote

from app.services import deploy


def test_force_pull_success_banner(client, monkeypatch):
    monkeypatch.setattr(deploy, "git_force_pull",
                        lambda: {"ok": True, "old": "aaa", "new": "bbb", "subject": "s",
                                 "needs_rebuild": False, "alembic_warn": "", "forced": True})
    r = client.post("/admin/force-pull", follow_redirects=False)
    assert r.status_code == 303
    assert "msg=" in r.headers["location"] and "aaa" in r.headers["location"]


def test_pull_error_banner(client, monkeypatch):
    monkeypatch.setattr(deploy, "git_pull",
                        lambda: {"ok": False, "needs_force": True, "error": "грязно, жми force"})
    r = client.post("/admin/pull", follow_redirects=False)
    assert r.status_code == 303 and "err=" in r.headers["location"]


def test_pull_rebuild_hint(client, monkeypatch):
    monkeypatch.setattr(deploy, "git_pull",
                        lambda: {"ok": True, "old": "a", "new": "b", "subject": "s",
                                 "needs_rebuild": True, "alembic_warn": ""})
    r = client.post("/admin/pull", follow_redirects=False)
    assert "up%20-d%20--build" in r.headers["location"] or "--build" in r.headers["location"]


def test_diag_renders_status(client, monkeypatch):
    monkeypatch.setattr(deploy, "deploy_status",
                        lambda: {"branch": "main", "hash": "abc", "subject": "s", "date": "2026-07-10",
                                 "dirty": False, "ahead": 0, "behind": 2, "detached": False})
    r = client.get("/diag")
    assert r.status_code == 200 and "позади origin на 2" in r.text


def test_pull_noop_already_fresh(client, monkeypatch):
    """old == new → честное «Уже свежая», не «Обновлено: X→X»."""
    monkeypatch.setattr(deploy, "git_pull",
                        lambda: {"ok": True, "old": "same123", "new": "same123", "subject": "s",
                                 "needs_rebuild": False, "alembic_warn": ""})
    loc = unquote(client.post("/admin/pull", follow_redirects=False).headers["location"])
    assert "Уже свежая" in loc and "Обновлено" not in loc


def test_pull_success_old_to_new(client, monkeypatch):
    """old != new → «Обновлено: old→new»."""
    monkeypatch.setattr(deploy, "git_pull",
                        lambda: {"ok": True, "old": "aaa1111", "new": "bbb2222", "subject": "s",
                                 "needs_rebuild": False, "alembic_warn": ""})
    loc = unquote(client.post("/admin/pull", follow_redirects=False).headers["location"])
    assert "Обновлено: aaa1111→bbb2222" in loc
