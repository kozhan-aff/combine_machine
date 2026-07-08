"""diag-alert: кэш + фильтр external + баннер + фоновый цикл. run_diagnostics замокан — сети нет."""
from datetime import datetime

import pytest

import app.services.diag_cache as diag_cache


@pytest.fixture(autouse=True)
def _reset_diag_cache():
    """Кэш — модульный глобал, живёт весь pytest-сеанс. Чистим до и после каждого
    теста, чтобы тесты не текли друг в друга и наружу (иначе баннер всплывёт в чужом рендере)."""
    diag_cache._checks = None
    diag_cache._checked_at = None
    yield
    diag_cache._checks = None
    diag_cache._checked_at = None


def _fake(*rows):
    """rows = (key, label, status)... -> фабрика замены run_diagnostics()."""
    checks = [{"key": k, "label": lbl, "status": st, "role": "", "module": "M1",
               "critical": True, "ms": 1, "error": None} for k, lbl, st in rows]
    return lambda: checks


def test_alert_none_before_first_refresh():
    assert diag_cache.alert() is None


def test_refresh_caches_and_sets_time(monkeypatch):
    monkeypatch.setattr(diag_cache, "run_diagnostics", _fake(("aparser", "A-Parser", "ok")))
    out = diag_cache.refresh()
    assert [c["key"] for c in out] == ["aparser"]
    a = diag_cache.alert()
    assert a is not None
    assert a["down"] == []                       # всё ok -> баннера не будет
    assert isinstance(a["checked_at"], datetime)


def test_external_filter_excludes_db_and_skip(monkeypatch):
    monkeypatch.setattr(diag_cache, "run_diagnostics", _fake(
        ("aparser", "A-Parser", "fail"),
        ("db", "PostgreSQL", "fail"),          # внутренний — исключён
        ("searxng", "SearXNG", "skip")))        # нет кред — не авария
    diag_cache.refresh()
    a = diag_cache.alert()
    assert a["down"] == ["A-Parser"]
    assert a["sig"] == "aparser"


def test_sig_is_sorted_and_tracks_set(monkeypatch):
    monkeypatch.setattr(diag_cache, "run_diagnostics", _fake(
        ("cloudflare", "Cloudflare", "fail"),
        ("aparser", "A-Parser", "fail")))
    diag_cache.refresh()
    assert diag_cache.alert()["sig"] == "aparser,cloudflare"   # sorted keys, детерминировано


def test_diag_refresh_redirects_to_referer_and_updates_cache(client, monkeypatch):
    calls = {"n": 0}

    def fake():
        calls["n"] += 1
        return [{"key": "aparser", "label": "A-Parser", "status": "fail", "role": "",
                 "module": "M1", "critical": True, "ms": 1, "error": None}]

    monkeypatch.setattr("app.services.diag_cache.run_diagnostics", fake)
    r = client.post("/diag/refresh", headers={"referer": "http://testserver/domains"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("http://testserver/domains")
    assert "msg=" in r.headers["location"]
    assert calls["n"] >= 1
    assert diag_cache.alert()["down"] == ["A-Parser"]


def test_diag_view_populates_cache(client, monkeypatch):
    monkeypatch.setattr("app.services.diag_cache.run_diagnostics",
                        lambda: [{"key": "llm", "label": "LiteLLM", "status": "ok", "role": "",
                                  "module": "M4", "critical": True, "ms": 1, "error": None}])
    r = client.get("/diag")
    assert r.status_code == 200
    assert diag_cache.alert() is not None      # /diag прогнал refresh -> кэш заполнен


def test_banner_renders_when_external_down(client, monkeypatch):
    monkeypatch.setattr("app.services.diag_cache.run_diagnostics",
                        lambda: [{"key": "aparser", "label": "A-Parser", "status": "fail",
                                  "role": "", "module": "M1", "critical": True, "ms": 1, "error": None}])
    diag_cache.refresh()
    r = client.get("/")                          # баннер глобальный — виден на дашборде, не только /diag
    assert 'id="diag-alert"' in r.text
    assert "A-Parser" in r.text
    assert "/diag/refresh" in r.text


def test_no_banner_when_all_ok(client, monkeypatch):
    monkeypatch.setattr("app.services.diag_cache.run_diagnostics",
                        lambda: [{"key": "aparser", "label": "A-Parser", "status": "ok",
                                  "role": "", "module": "M1", "critical": True, "ms": 1, "error": None}])
    diag_cache.refresh()
    r = client.get("/")
    assert 'id="diag-alert"' not in r.text


def test_diag_refresh_strips_stale_flash_keeps_filters(client, monkeypatch):
    monkeypatch.setattr("app.services.diag_cache.run_diagnostics", lambda: [])
    r = client.post("/diag/refresh",
                    headers={"referer": "http://testserver/domains?err=boom&status=approved"},
                    follow_redirects=False)
    assert r.status_code == 303
    loc = r.headers["location"]
    assert "err=" not in loc            # прежний flash выброшен
    assert "status=approved" in loc     # фильтр пользователя сохранён
    assert "msg=" in loc                # свежее подтверждение добавлено


def test_diag_loop_fires_refresh_on_startup(monkeypatch):
    """lifespan запускает _diag_loop, который дёргает refresh() на старте.
    Входим в TestClient как контекст-менеджер — только он гоняет lifespan
    (обычная фикстура client этого НЕ делает, поэтому 170 существующих тестов сеть не трогают)."""
    import threading
    from fastapi.testclient import TestClient
    from app.main import app

    fired = threading.Event()
    monkeypatch.setattr("app.services.diag_cache.refresh", lambda: fired.set() or [])
    with TestClient(app):
        assert fired.wait(timeout=5)
