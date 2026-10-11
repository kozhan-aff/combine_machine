"""Офлайн-тесты деплой-сервиса: мокаем subprocess.run (git в сеть не ходит).
Проверяем ЛОГИКУ машины состояний, скраббинг токена, single-flight, отсутствие git clean."""
import subprocess
import pytest

from app.config import settings
from app.services import deploy


def _cp(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


@pytest.fixture
def creds():
    old = settings.GITHUB_TOKEN, settings.GITHUB_REPO
    settings.GITHUB_TOKEN, settings.GITHUB_REPO = "SECRETTOK", "o/r"
    yield
    settings.GITHUB_TOKEN, settings.GITHUB_REPO = old


class Router:
    """Стейтфул-роутер команд: log отдаёт old до pull/checkout, new после.
    Записывает все вызванные argv для проверок (напр. что git clean не звался)."""
    def __init__(self, *, dirty=False, pull_rc=0, pull_err="", changed_files="app/x.py",
                 alembic_rc=0, alembic_err="", fetch_rc=0):
        self.dirty, self.pull_rc, self.pull_err = dirty, pull_rc, pull_err
        self.changed_files, self.alembic_rc, self.alembic_err = changed_files, alembic_rc, alembic_err
        self.fetch_rc = fetch_rc
        self.updated = False
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        if argv[0] == "alembic":
            return _cp(self.alembic_rc, stderr=self.alembic_err)
        a = argv
        if "log" in a and "-1" in a:
            h = "newhash" if self.updated else "oldhash"
            return _cp(0, stdout=f"{h}\nsubj\n2026-07-10")
        if "rev-parse" in a and "--abbrev-ref" in a:
            return _cp(0, stdout="main")
        if "status" in a and "--porcelain" in a:
            return _cp(0, stdout=" M app/x.py\n" if self.dirty else "")
        if "rev-list" in a:
            return _cp(0, stdout="0\t2")               # ahead 0, behind 2
        if "diff" in a and "--name-only" in a:
            return _cp(0, stdout=self.changed_files)
        if "pull" in a:
            self.updated = self.pull_rc == 0
            return _cp(self.pull_rc, stderr=self.pull_err)
        if "fetch" in a:
            return _cp(self.fetch_rc, stderr="" if self.fetch_rc == 0 else "fetch boom")
        if "checkout" in a:
            self.updated = True
            return _cp(0)
        if "reset" in a:
            return _cp(0)
        return _cp(0)


def _patch(monkeypatch, router):
    monkeypatch.setattr(deploy.subprocess, "run", router)
    return router


def test_status_clean(creds, monkeypatch):
    _patch(monkeypatch, Router(dirty=False))
    s = deploy.deploy_status()
    assert s["branch"] == "main" and s["hash"] == "oldhash"
    assert s["dirty"] is False and s["behind"] == 2 and s["detached"] is False


def test_status_dirty(creds, monkeypatch):
    _patch(monkeypatch, Router(dirty=True))
    assert deploy.deploy_status()["dirty"] is True


def test_pull_ff_success(creds, monkeypatch):
    _patch(monkeypatch, Router(pull_rc=0, changed_files="app/x.py"))
    out = deploy.git_pull()
    assert out["ok"] and out["old"] == "oldhash" and out["new"] == "newhash"
    assert out["needs_rebuild"] is False and out["alembic_warn"] == ""


def test_pull_dirty_suggests_force(creds, monkeypatch):
    _patch(monkeypatch, Router(pull_rc=1, pull_err="Your local changes would be overwritten"))
    out = deploy.git_pull()
    assert out["ok"] is False and out.get("needs_force") is True


def test_pull_detects_rebuild(creds, monkeypatch):
    _patch(monkeypatch, Router(pull_rc=0, changed_files="backend/requirements.txt"))
    out = deploy.git_pull()
    assert out["needs_rebuild"] is True and out["compose_hint"] == ""


def test_pull_detects_changed_compose_file(creds, monkeypatch):
    """Новый том или сервис в docker-compose.yml без `docker compose up -d` не появится — панель обязана
    сказать об этом сама (так воркер получает папку правил)."""
    _patch(monkeypatch, Router(pull_rc=0, changed_files="docker-compose.yml\nbackend/app/x.py"))
    out = deploy.git_pull()
    assert out["ok"] and out["needs_rebuild"] is False
    assert "`" not in out["compose_hint"]             # обратная кавычка в PowerShell — перенос строки
    assert out["compose_hint"] == ("изменился docker-compose.yml — нужно пересоздать контейнеры: docker compose up -d "
                                   "(новые тома/сервисы)")


def test_pull_compose_file_elsewhere_in_the_tree_is_not_ours(creds, monkeypatch):
    _patch(monkeypatch, Router(pull_rc=0, changed_files="docs/docker-compose.yml\nbackend/Dockerfile"))
    out = deploy.git_pull()
    assert out["compose_hint"] == "" and out["needs_rebuild"] is True


def test_pull_alembic_failure_is_not_ok(creds, monkeypatch):
    """F22/F23: упавшая миграция -> ok=False (раньше пряталась в alembic_warn, а ok молча
    оставался True — деплой с провалившейся миграцией красился зелёным, см. audit 2026-07-14)."""
    _patch(monkeypatch, Router(pull_rc=0, alembic_rc=1, alembic_err="migration boom"))
    out = deploy.git_pull()
    assert out["ok"] is False and "migration boom" in out["alembic_warn"]


def test_pull_alembic_timeout_is_not_ok_and_has_banner(creds, monkeypatch):
    """S22 (аудит 2026-07-18): миграция вышла за timeout -> subprocess.TimeoutExpired.
    Раньше ловился только FileNotFoundError, и TimeoutExpired улетал голым 500 в /admin/pull.
    Теперь ok=False + понятный баннер, деплой честно «с проблемой», а не крэш."""
    r = Router(pull_rc=0)
    base = r.__call__

    def router(argv, **kw):
        if argv and argv[0] == "alembic":
            raise subprocess.TimeoutExpired(cmd="alembic upgrade head", timeout=120)
        return base(argv, **kw)

    monkeypatch.setattr(deploy.subprocess, "run", router)
    out = deploy.git_pull()
    assert out["ok"] is False
    assert "120" in out["alembic_warn"] and "прерван" in out["alembic_warn"]


def test_force_pull_no_git_clean(creds, monkeypatch):
    r = _patch(monkeypatch, Router())
    out = deploy.git_force_pull()
    assert out["ok"] and out.get("forced") is True
    assert not any("clean" in argv for argv in r.calls), "git clean НЕ должен вызываться"
    assert any("checkout" in argv and "-f" in argv for argv in r.calls)


def test_token_scrubbed(creds, monkeypatch):
    _patch(monkeypatch, Router(pull_rc=1, pull_err="fatal https://x-access-token:SECRETTOK@github.com"))
    out = deploy.git_pull()
    assert "SECRETTOK" not in out["error"] and "***" in out["error"]


def test_no_token(monkeypatch):
    old = settings.GITHUB_TOKEN
    settings.GITHUB_TOKEN = ""
    try:
        assert deploy.git_pull()["ok"] is False
        assert deploy.git_force_pull()["ok"] is False
    finally:
        settings.GITHUB_TOKEN = old


def test_pull_updates_origin_main_tracking_ref(creds, monkeypatch):
    """РЕГРЕССИЯ (S21, аудит 2026-07-18). Явный-URL pull без destination-refspec НЕ
    обновлял refs/remotes/origin/main -> deploy_status() после успешного pull видел
    устаревший ahead/behind. Проверяем, что refspec явно просит обновить tracking-ref."""
    router = _patch(monkeypatch, Router(pull_rc=0))
    deploy.git_pull()
    pull_call = next(c for c in router.calls if "pull" in c)
    assert "+main:refs/remotes/origin/main" in pull_call


def test_force_pull_updates_origin_main_tracking_ref(creds, monkeypatch):
    """РЕГРЕССИЯ (S21, аудит 2026-07-18). Тот же баг в git_force_pull(): явный-URL fetch
    без destination-refspec не обновлял refs/remotes/origin/main."""
    router = _patch(monkeypatch, Router(fetch_rc=0))
    deploy.git_force_pull()
    fetch_call = next(c for c in router.calls if "fetch" in c)
    assert "+main:refs/remotes/origin/main" in fetch_call


def test_single_flight(creds, monkeypatch):
    _patch(monkeypatch, Router())
    deploy._LOCK.acquire()
    try:
        out = deploy.git_pull()
        assert out["ok"] is False and "уже идёт" in out["error"]
    finally:
        deploy._LOCK.release()


def test_pull_refused_while_paid_job_runs(creds, monkeypatch):
    """F8-16: pull/force-pull перезапускают процессы (--reload/watchfiles) и обрывают идущую
    платную волну — пока задача жива, git не трогаем вовсе."""
    from app.services import jobs
    r = _patch(monkeypatch, Router(pull_rc=0))
    with jobs.track("score"):
        for fn in (deploy.git_pull, deploy.git_force_pull):
            out = fn()
            assert out["ok"] is False and "score" in out["error"] and "old" not in out
    assert r.calls == []                      # ни одной git/alembic-команды
    assert deploy.git_pull()["ok"]            # задача закончилась — обновление проходит


def test_stale_job_does_not_block_deploy(creds, monkeypatch):
    """Труп (контейнер убит, сердцебиения нет) не запирает деплой навсегда."""
    from datetime import datetime, timedelta, timezone
    import app.db as db
    from app.models.job import JobRun
    with db.SessionLocal() as s:
        old = datetime.now(timezone.utc) - timedelta(hours=1)
        s.add(JobRun(name="score", status="running", started_at=old, updated_at=old))
        s.commit()
    _patch(monkeypatch, Router(pull_rc=0))
    assert deploy.git_pull()["ok"]


def test_spawn_refuses_honestly_while_pull_is_running():
    """Minor: во время pull spawn() не возвращает True при проглоченном AlreadyRunning."""
    from app.services import jobs
    ran = []
    deploy._LOCK.acquire()
    try:
        assert jobs.spawn("discovery", lambda: ran.append(1)) is False
    finally:
        deploy._LOCK.release()
    jobs._drain()
    assert ran == []


def test_run_score_during_pull_names_git_as_the_reason(client):
    """Идёт git pull: сообщение называет причину, а не лживое «Проверка уже идёт»."""
    from urllib.parse import unquote_plus
    deploy._LOCK.acquire()
    try:
        r = client.post("/run/score", data={"n": 1}, follow_redirects=False)
    finally:
        deploy._LOCK.release()
    loc = unquote_plus(r.headers["location"])
    assert "обновление программы" in loc and "уже идёт" not in loc
