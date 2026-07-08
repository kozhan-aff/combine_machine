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
