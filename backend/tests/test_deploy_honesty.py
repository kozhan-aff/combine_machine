"""Регрессия аудита 2026-07-14 (F22 + F23 + F29): деплой не должен рисовать зелёное на
упавшей миграции, и models/__init__.py не должен прятать таблицы от alembic autogenerate.

(a) alembic returncode != 0 -> deploy.git_pull()/_post_update() возвращают ok=False, и
    панель (_pull_banner через /admin/pull) показывает err=, а не msg=.
(b) Base.metadata (в чистом процессе, как его видит alembic/env.py — `from app import
    models` и НИЧЕГО больше) содержит все 11 таблиц проекта. Внутри самого pytest-процесса
    conftest.py уже импортирует app.models.settings/autonomy/job НАПРЯМУЮ (чтобы
    create_all видел все таблицы) — это маскирует дыру в models/__init__.py, поэтому
    проверка идёт в ИЗОЛИРОВАННОМ subprocess, а не через Base.metadata текущего процесса.
"""
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote

import pytest

from app.config import settings
from app.services import deploy

_BACKEND = Path(__file__).resolve().parent.parent  # backend/


def _cp(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


@pytest.fixture
def creds():
    old = settings.GITHUB_TOKEN, settings.GITHUB_REPO
    settings.GITHUB_TOKEN, settings.GITHUB_REPO = "SECRETTOK", "o/r"
    yield
    settings.GITHUB_TOKEN, settings.GITHUB_REPO = old


class Router:
    """git pull проходит чисто; alembic падает (напр. FK-нарушение на проде) — ровно тот
    сценарий, который до фикса деплой красил зелёным."""
    def __init__(self):
        self.updated = False

    def __call__(self, argv, **kw):
        if argv[0] == "alembic":
            return _cp(1, stderr="FATAL: relation violates foreign key constraint")
        a = argv
        if "log" in a and "-1" in a:
            h = "newhash" if self.updated else "oldhash"
            return _cp(0, stdout=f"{h}\nsubj\n2026-07-14")
        if "rev-parse" in a and "--abbrev-ref" in a:
            return _cp(0, stdout="main")
        if "status" in a and "--porcelain" in a:
            return _cp(0, stdout="")
        if "rev-list" in a:
            return _cp(0, stdout="0\t0")
        if "diff" in a and "--name-only" in a:
            return _cp(0, stdout="app/x.py")
        if "pull" in a:
            self.updated = True
            return _cp(0)
        return _cp(0)


def test_pull_migration_failure_returns_not_ok(creds, monkeypatch):
    """F22/F23: git pull прошёл, но alembic upgrade head упал -> ok=False, не True."""
    monkeypatch.setattr(deploy.subprocess, "run", Router())
    out = deploy.git_pull()
    assert out["ok"] is False
    assert "constraint" in out["alembic_warn"]
    # git САМ отработал: hash сменился — это не «git pull не удался», это «БД отстала от кода»
    assert out["old"] == "oldhash" and out["new"] == "newhash"


def test_pull_migration_failure_shows_red_banner_not_green(client, monkeypatch):
    """F22/F23: провалившаяся миграция -> /admin/pull редиректит с err=, НЕ с msg=.
    До фикса _pull_banner проверял только r['ok'], который _post_update ВСЕГДА возвращал
    True -> оператор видел зелёное «Обновлено» с провалом миграции мелким суффиксом."""
    from app.services import deploy as deploy_mod
    monkeypatch.setattr(deploy_mod, "git_pull", lambda: {
        "ok": False, "old": "aaa1111", "new": "bbb2222", "subject": "миграция сломана",
        "needs_rebuild": False, "alembic_warn": "FATAL: foreign key violation",
    })
    r = client.post("/admin/pull", follow_redirects=False)
    loc = r.headers["location"]
    assert "err=" in loc, f"ожидали красный баннер, получили: {loc}"
    assert "msg=" not in loc, f"миграция упала, но баннер зелёный: {loc}"
    assert "foreign key violation" in unquote(loc)


_EXPECTED_TABLES = {
    "domains", "acquisition_orders", "sites", "pages", "offers", "site_offers",
    "index_history", "scoring_settings", "autonomy_settings", "autonomy_run", "job_run",
}


def test_models_init_registers_all_project_tables():
    """F22/F23/F29 (контекст #2): alembic/env.py строит target_metadata РОВНО через
    `from app import models` в чистом процессе — значит models/__init__.py, а не
    транзитивные импорты сервисов, определяет, что видит autogenerate. Забытый модуль
    здесь означает DROP TABLE на реально используемых таблицах при следующем
    `alembic revision --autogenerate`.

    Проверяем в subprocess: сам тестовый процесс (через conftest.py) уже импортировал
    app.models.settings/autonomy/job напрямую для create_all — Base.metadata текущего
    процесса уже "починен" их наличием независимо от того, что делает models/__init__.py.
    """
    script = (
        "from app.db import Base\n"
        "from app import models\n"
        "print(','.join(sorted(Base.metadata.tables.keys())))\n"
    )
    r = subprocess.run([sys.executable, "-c", script], cwd=_BACKEND,
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    actual = set(r.stdout.strip().split(","))
    missing = _EXPECTED_TABLES - actual
    assert not missing, (
        f"models/__init__.py не регистрирует: {sorted(missing)} — "
        "alembic autogenerate предложит их DROP как осиротевшие"
    )
