"""Версия из git: парсер вывода + баннеры pull/check-updates (subprocess замокан)."""
import subprocess
from urllib.parse import unquote

from app.config import settings
from app.services.version import _parse


def test_parse_ok():
    v = _parse("a1b2c3d", "M1: воронка скоринга", "2026-07-06")
    assert v == {"hash": "a1b2c3d", "subject": "M1: воронка скоринга", "date": "2026-07-06"}


def test_parse_empty():
    v = _parse("", "", "")
    assert v["hash"] == "—"


# --- роуты /admin/pull и /admin/check-updates: git/alembic не запускаются -----
class _R:
    def __init__(self, code=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = code, out, err


def _fake_run(log_hashes, lsremote=None):
    """Диспетчер вместо subprocess.run: git log отдаёт хэши по очереди (последний —
    навсегда), pull/alembic — успех, ls-remote — заданный результат."""
    logs = list(log_hashes)

    def run(argv, **kw):
        if argv[0] == "alembic":
            return _R(0)
        if "log" in argv:
            h = logs.pop(0) if len(logs) > 1 else logs[0]
            return _R(0, f"{h}\nтема коммита\n2026-07-06\n")
        if "pull" in argv:
            return _R(0, "ok")
        if "ls-remote" in argv:
            return lsremote
        raise AssertionError(f"неожиданный вызов subprocess: {argv}")

    return run


def _flash(resp) -> str:
    assert resp.status_code == 303
    return unquote(resp.headers["location"])


def test_pull_noop_says_already_fresh(client, monkeypatch):
    """no-op pull (HEAD не сдвинулся) — честное «Уже свежая», не «Обновлено: X→X»."""
    monkeypatch.setattr(settings, "GITHUB_TOKEN", "TESTTOKEN123")
    monkeypatch.setattr(subprocess, "run", _fake_run(["a1b2c3d"]))
    loc = _flash(client.post("/admin/pull", follow_redirects=False))
    assert "Уже свежая" in loc
    assert "Обновлено" not in loc


def test_pull_with_change_says_old_to_new(client, monkeypatch):
    monkeypatch.setattr(settings, "GITHUB_TOKEN", "TESTTOKEN123")
    monkeypatch.setattr(subprocess, "run", _fake_run(["aaa1111", "bbb2222"]))
    loc = _flash(client.post("/admin/pull", follow_redirects=False))
    assert "Обновлено: aaa1111→bbb2222" in loc


def test_check_updates_failure_surfaces_scrubbed_stderr(client, monkeypatch):
    """ls-remote упал — детали (returncode/stderr) в баннере, токен вычищен."""
    tok = "SECRETTOKEN999"
    monkeypatch.setattr(settings, "GITHUB_TOKEN", tok)
    monkeypatch.setattr(subprocess, "run", _fake_run(
        ["a1b2c3d"],
        lsremote=_R(128, "", f"fatal: Authentication failed using token {tok}")))
    loc = _flash(client.post("/admin/check-updates", follow_redirects=False))
    assert "не удалось прочитать удалёнку: fatal: Authentication failed" in loc
    assert tok not in loc
