"""Git-деплой из панели: статус дерева, безопасный ff-pull, деструктивный force-reset.
Вся git-логика (раньше инлайн в panel.py) здесь — тестируемо (мокаем subprocess.run).
Панель git-only: НЕ трогает Docker. GITHUB_TOKEN идёт через http.extraheader (не argv) и
скраббится во всех выводах. Single-flight: одно обновление за раз. force НИКОГДА не git clean —
untracked (.env/.pem) выживают."""
import base64
import os
import subprocess
import threading

from app.config import settings

_REPO = "/repo"          # весь репозиторий (с .git) смонтирован сюда (docker-compose .:/repo)
_APP = "/app"            # backend/ смонтирован сюда; cwd для alembic
_LOCK = threading.Lock()  # single-flight


def _scrub(s: str) -> str:
    tok = settings.GITHUB_TOKEN
    return s.replace(tok, "***") if tok else s


def _git_env() -> dict:
    """Authorization через http.extraheader — токен НЕ в argv (виден в ps/procfs)."""
    basic = base64.b64encode(f"x-access-token:{settings.GITHUB_TOKEN}".encode()).decode()
    return {**os.environ, "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
            "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}"}


def _clean_url() -> str:
    return f"https://github.com/{settings.GITHUB_REPO}.git"


def _git(args, *, timeout=120, env=None):
    return subprocess.run(["git", "-C", _REPO, "-c", "safe.directory=/repo", *args],
                          capture_output=True, text=True, timeout=timeout, env=env)


def deploy_status() -> dict:
    """Локально (без сети), дёшево. {branch,hash,subject,date,dirty,ahead,behind,detached} | {error}.
    ahead/behind — против ЛОКАЛЬНОГО origin/main (свеж после fetch/check/pull; 0 если ref нет)."""
    try:
        head = _git(["log", "-1", "--format=%h%n%s%n%cs"], timeout=10)
        if head.returncode != 0:
            return {"error": _scrub((head.stderr or "git error").strip())[:150]}
        h, subject, date = (head.stdout.strip().split("\n") + ["", "", ""])[:3]
        branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], timeout=10).stdout.strip() or "—"
        dirty = bool(_git(["status", "--porcelain", "--untracked-files=no"], timeout=10).stdout.strip())
        ahead = behind = 0
        ab = _git(["rev-list", "--left-right", "--count", "HEAD...origin/main"], timeout=10)
        parts = ab.stdout.split() if ab.returncode == 0 else []
        if len(parts) == 2:
            ahead, behind = int(parts[0]), int(parts[1])
        return {"branch": branch, "hash": h or "—", "subject": subject or "—", "date": date or "—",
                "dirty": dirty, "ahead": ahead, "behind": behind, "detached": branch == "HEAD"}
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {_scrub(str(e))}"[:150]}


# Что оператору сделать руками после pull, если изменился сам состав контейнеров: панель этого не умеет,
# а без пересоздания новый том (так воркер получает папку правил) или сервис просто не появится.
COMPOSE_HINT = ("изменился docker-compose.yml — нужен `docker compose up -d` "
                "(пересоздать контейнеры: новые тома/сервисы)")


def _changed_files(old: str, new: str) -> list[str]:
    if not old or not new or old == new:
        return []
    diff = _git(["diff", "--name-only", f"{old}..{new}"], timeout=10)
    return diff.stdout.split() if diff.returncode == 0 else []


def _detect_rebuild(old: str, new: str) -> dict:
    """Что в old..new требует рук оператора: {"image": менялись requirements.txt/Dockerfile — пересборка
    образа, "compose": менялся docker-compose.yml — пересоздать контейнеры (`docker compose up -d`)}."""
    files = _changed_files(old, new)
    return {"image": any(f in ("backend/requirements.txt", "backend/Dockerfile") for f in files),
            "compose": "docker-compose.yml" in files}


def _post_update(old: str) -> dict:
    """Общий хвост pull/force: alembic + новый hash/subject + детект пересборки.

    ok=False, если миграция упала или alembic не найден — код репозитория мог обновиться,
    а схема БД нет, и это НЕ успешный деплой (F22/F23/F29, аудит 2026-07-14): раньше
    ok всегда был True, а провал алембика молча прятался маленьким суффиксом в
    alembic_warn — git-pull с упавшей миграцией на боксе красился зелёным «Обновлено»."""
    try:
        mig = subprocess.run(["alembic", "upgrade", "head"], cwd=_APP,
                             capture_output=True, text=True, timeout=120)
        ok = mig.returncode == 0
        alembic_warn = "" if ok else _scrub(mig.stderr.strip())[:150]
    except FileNotFoundError:
        ok = False
        alembic_warn = "alembic не установлен в контейнере — миграции пропущены (пересобери образ)"
    except subprocess.TimeoutExpired:
        # Миграция вышла за 120с (медленный DDL на населённой таблице / блокировка PG):
        # subprocess убивает alembic на середине. Без этой ветки TimeoutExpired улетал бы
        # в /admin/pull голым 500 (нет app-level handler), а схема могла остаться
        # ЧАСТИЧНО накатанной при уже сдвинутом git HEAD — ровно то расхождение код/схема,
        # которое докстринг этой функции запрещает выдавать за чистый успех. ok=False —
        # деплой честно «с проблемой», баннер объясняет что случилось.
        ok = False
        alembic_warn = "миграция не завершилась за 120с и была прервана — проверь схему БД вручную"
    cur = deploy_status()
    new = cur.get("hash", "")
    manual = _detect_rebuild(old, new)
    return {"ok": ok, "old": old, "new": new, "subject": cur.get("subject", ""),
            "needs_rebuild": manual["image"], "compose_hint": COMPOSE_HINT if manual["compose"] else "",
            "alembic_warn": alembic_warn}


def _busy_jobs() -> list[str]:
    """Имена ЖИВЫХ длинных задач (score/discovery/sweep/generate/...), которые pull убьёт.

    Pull меняет *.py в смонтированном каталоге: uvicorn --reload (панель, где крутятся ручные
    задачи) и watchfiles (воркер, где идёт свип) перезапускают процесс и обрывают поток посреди
    ПЛАТНОЙ волны (Ahrefs units, A-Parser) — раньше защитой была устная заметка «не жми pull пока
    идёт score» (F8-16). Протухшие (stale — контейнер уже убит) не считаем: трупы не должны
    блокировать деплой навсегда. Реестр общий (job_run), так что видны задачи обоих процессов.
    Сбой чтения реестра — не повод запирать деплой (панель с лежащей БД и так не обновить)."""
    try:
        from app.services import jobs
        return [j["name"] for j in jobs.live() if not j.get("stale")]
    except Exception:  # noqa: BLE001
        return []


def _busy_error(busy: list[str]) -> dict:
    return {"ok": False, "error": "идут задачи: " + ", ".join(busy) + " — обновление оборвало бы их "
                                  "посреди работы (перезапуск процессов). Дождись конца или нажми "
                                  "«✕ Отменить» на карточке задачи и повтори."}


def git_pull() -> dict:
    """Безопасный путь: fetch → pull --ff-only → alembic → детект. needs_force при грязи/расхождении."""
    if not settings.GITHUB_TOKEN:
        return {"ok": False, "error": "GITHUB_TOKEN не задан в .env — нечем авторизовать git pull"}
    if not _LOCK.acquire(blocking=False):
        return {"ok": False, "error": "обновление уже идёт — подожди завершения"}
    try:
        if busy := _busy_jobs():  # под замком: jobs._open не стартует новое, пока замок занят
            return _busy_error(busy)
        old = deploy_status().get("hash", "")
        try:
            # S21 (аудит 2026-07-18): явный URL (не имя remote'а) + голое имя ветки пишет
            # ТОЛЬКО FETCH_HEAD — refs/remotes/origin/main не обновляется, а именно его
            # читает deploy_status() для ahead/behind. Явный destination-refspec чинит это,
            # не меняя поведение мержа (--ff-only по-прежнему мержит из FETCH_HEAD).
            pull = _git(["pull", "--ff-only", _clean_url(), "+main:refs/remotes/origin/main"],
                       timeout=120, env=_git_env())
        except FileNotFoundError:
            return {"ok": False, "error": "git не установлен в контейнере — пересобери образ (docker compose build)"}
        if pull.returncode != 0:
            return {"ok": False, "needs_force": True,
                    "error": "git pull не прошёл (дерево грязное или история разошлась): "
                             + _scrub((pull.stderr or pull.stdout).strip())[:250]
                             + ". Используй ⚠ Принудительно обновить."}
        return _post_update(old)
    finally:
        _LOCK.release()


def git_force_pull() -> dict:
    """Деструктив (по confirm): fetch → checkout -f -B main FETCH_HEAD → alembic → детект.
    checkout -f сбрасывает грязь и приводит к origin/main из ЛЮБОГО состояния; untracked
    (.env/.pem) выживают. git clean НЕ вызывается."""
    if not settings.GITHUB_TOKEN:
        return {"ok": False, "error": "GITHUB_TOKEN не задан в .env — нечем авторизовать"}
    if not _LOCK.acquire(blocking=False):
        return {"ok": False, "error": "обновление уже идёт — подожди завершения"}
    try:
        if busy := _busy_jobs():  # под замком: jobs._open не стартует новое, пока замок занят
            return _busy_error(busy)
        env = _git_env()
        old = deploy_status().get("hash", "")
        try:
            # S21: тот же явный-URL/refspec фикс, что в git_pull() — иначе refs/remotes/origin/main
            # не обновится и deploy_status() покажет устаревшие ahead/behind после force-pull.
            fetch = _git(["fetch", _clean_url(), "+main:refs/remotes/origin/main"], timeout=30, env=env)
        except FileNotFoundError:
            return {"ok": False, "error": "git не установлен в контейнере — пересобери образ (docker compose build)"}
        if fetch.returncode != 0:
            return {"ok": False, "error": "git fetch: " + _scrub((fetch.stderr or fetch.stdout).strip())[:250]}
        co = _git(["checkout", "-f", "-B", "main", "FETCH_HEAD"], timeout=120)
        if co.returncode != 0:
            return {"ok": False, "error": "git checkout: " + _scrub((co.stderr or co.stdout).strip())[:250]}
        r = _post_update(old)
        r["forced"] = True
        return r
    finally:
        _LOCK.release()
