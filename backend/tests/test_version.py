"""Версия из git: парсер вывода + баннер check-updates (subprocess замокан).
/admin/pull и /admin/force-pull теперь тонкие обёртки над services/deploy.py — их
логика покрыта test_deploy.py (git/alembic-машина) и test_deploy_panel.py (роут-баннеры)."""
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


# --- роут /admin/check-updates: git не ходит в сеть, ls-remote замокан -----
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


def test_check_updates_unknown_current_version_does_not_claim_fresh(client, monkeypatch):
    """Ревью-минор #10: current_version() упал (git в контейнере недоступен) -> cur == "" —
    без гварда remote.startswith("") тривиально True для ЛЮБОГО remote, и код лгал «актуально».
    Теперь честный err-баннер вместо ложного «актуально»."""
    monkeypatch.setattr(settings, "GITHUB_TOKEN", "TESTTOKEN123")

    def run(argv, **kw):
        if "log" in argv:
            return _R(128, "", "fatal: not a git repository")   # current_version() errors
        if "ls-remote" in argv:
            return _R(0, "deadbee1\trefs/heads/main\n", "")
        raise AssertionError(f"неожиданный вызов subprocess: {argv}")

    monkeypatch.setattr(subprocess, "run", run)
    loc = _flash(client.post("/admin/check-updates", follow_redirects=False))
    assert "не удалось определить текущую версию" in loc
    assert "актуально" not in loc
