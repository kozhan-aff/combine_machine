# Надёжный git-деплой из панели — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Пользователь надёжно получает обновления из панели во всех реалистичных ситуациях (грязное дерево, расхождение, detached HEAD, старый код воркера) без консоли; единственное исключение — пересборка под новые зависимости — детектится и сопровождается точной инструкцией.

**Architecture:** Вся git-логика уезжает из `panel.py` в тестируемый `services/deploy.py` (машина состояний: статус → безопасный ff-pull → деструктивный force-reset → alembic → детект пересборки). Панель остаётся git-only (без доступа к Docker — безопаснее для LAN-панели без пароля). Воркер получает авто-reload через `watchfiles` (уже в образе), чтобы его код тоже подхватывался живьём.

**Tech Stack:** Python 3.12, FastAPI + Jinja2, `subprocess` (git/alembic), `threading.Lock` (single-flight), watchfiles (транзитивно через uvicorn[standard]). Тесты — офлайн, мокают `subprocess.run`.

## Global Constraints

Каждая задача неявно включает (дословно из спеки `docs/superpowers/specs/2026-07-10-git-deploy-from-ui-design.md` и CLAUDE.md):

- **Логика в `services/`** (`deploy.py` — git-оркестрация); `panel.py`-хендлеры — тонкие обёртки.
- **`GITHUB_TOKEN` НИКОГДА не в argv** (идёт через `http.extraheader` в env git); **скраббится** (`replace(token, "***")`) во ВСЕХ возвращаемых/логируемых строках.
- **Панель git-only:** НЕ трогает Docker (никакого docker-socket/CLI). Пересборка под новые зависимости — вне UI by design (детект + инструкция).
- **force-pull НИКОГДА не вызывает `git clean`** — untracked-файлы (`.env`, `backend/aapanel.pem`, оба gitignored) обязаны выживать.
- **Single-flight:** параллельные обновления запрещены (модульный `threading.Lock`, non-blocking acquire).
- **Хард-гейты (деньги/редактура) не затрагиваются** — это деплой-инфра, не пайплайн.
- **Дизайн-контракт панели:** `diag.html` — только классы из `base.html` (`.station`/`.card`/`.btn-acc`/`.btn-bad`); diag-станции остаются inline (`<div class="what">`, стилизуется `.station div.what`); UI на русском; холодная палитра.
- **Тесты офлайн/герметичны:** мокать `subprocess.run` (git в тестах в сеть НЕ ходит); `pyflakes` чист; baseline `207 passed`.
- **Без новых зависимостей сверх `watchfiles`** (уже транзитивно в образе через `uvicorn[standard]`; добавляем явной строкой в requirements как страховку).
- **Таймауты на subprocess:** fetch 30с, pull/reset/checkout 120с, alembic 120с, status-команды 10с.

### Точные dict-контракты (эталон — все задачи ссылаются сюда)

```
deploy_status() -> {branch:str, hash:str, subject:str, date:str, dirty:bool,
                    ahead:int, behind:int, detached:bool}  ИЛИ  {error:str}
git_pull() / git_force_pull() -> {ok:bool, old:str, new:str, subject:str,
                    needs_rebuild:bool, alembic_warn:str, ...}  где на неуспехе
                    {ok:False, error:str, needs_force?:bool}; force добавляет forced:True
```

---

## Задачи и файлы

| Задача | Файлы | Дело |
|---|---|---|
| 1 | Create `backend/app/services/deploy.py`, Create `backend/tests/test_deploy.py` | ядро: статус + ff-pull + force-reset + детект + скраб + single-flight (TDD, мок subprocess) |
| 2 | Modify `backend/app/api/panel.py`, Modify `backend/app/templates/diag.html`, Modify `backend/tests/test_web_fixes.py` (или новый `test_deploy_panel.py`) | тонкие хендлеры /admin/pull (переписать) + /admin/force-pull (новый) + /diag статус; UI статус-строка + кнопка force |
| 3 | Modify `docker-compose.yml`, Modify `backend/requirements.txt`, Modify `docs/DEPLOY.md` | воркер → watchfiles; watchfiles явной строкой; доки под новый поток |

Порядок: 1 → 2 (consumes deploy) → 3 (инфра/доки, независима, последней).

---

### Task 1: `services/deploy.py` — ядро деплоя (статус, ff-pull, force, детект)

**Files:**
- Create: `backend/app/services/deploy.py`
- Test: `backend/tests/test_deploy.py`

**Interfaces:**
- Consumes: `app.config.settings` (`GITHUB_TOKEN`, `GITHUB_REPO`).
- Produces: `deploy_status()`, `git_pull()`, `git_force_pull()` (dict-контракты см. Global Constraints). Хендлеры Задачи 2 их вызывают.

- [ ] **Step 1: Написать падающие тесты `backend/tests/test_deploy.py`**

```python
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
    r = _patch(monkeypatch, Router(pull_rc=0, changed_files="app/x.py"))
    out = deploy.git_pull()
    assert out["ok"] and out["old"] == "oldhash" and out["new"] == "newhash"
    assert out["needs_rebuild"] is False and out["alembic_warn"] == ""


def test_pull_dirty_suggests_force(creds, monkeypatch):
    _patch(monkeypatch, Router(pull_rc=1, pull_err="Your local changes would be overwritten"))
    out = deploy.git_pull()
    assert out["ok"] is False and out.get("needs_force") is True


def test_pull_detects_rebuild(creds, monkeypatch):
    _patch(monkeypatch, Router(pull_rc=0, changed_files="backend/requirements.txt"))
    assert deploy.git_pull()["needs_rebuild"] is True


def test_pull_alembic_warn_nonfatal(creds, monkeypatch):
    _patch(monkeypatch, Router(pull_rc=0, alembic_rc=1, alembic_err="migration boom"))
    out = deploy.git_pull()
    assert out["ok"] is True and "migration boom" in out["alembic_warn"]


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


def test_single_flight(creds, monkeypatch):
    _patch(monkeypatch, Router())
    deploy._LOCK.acquire()
    try:
        out = deploy.git_pull()
        assert out["ok"] is False and "уже идёт" in out["error"]
    finally:
        deploy._LOCK.release()
```

- [ ] **Step 2: Прогнать — падают (модуля нет)**

Run: `.venv/bin/python -m pytest backend/tests/test_deploy.py -q`
Expected: FAIL (ModuleNotFoundError: app.services.deploy)

- [ ] **Step 3: Написать `backend/app/services/deploy.py`**

```python
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
        dirty = bool(_git(["status", "--porcelain"], timeout=10).stdout.strip())
        ahead = behind = 0
        ab = _git(["rev-list", "--left-right", "--count", "HEAD...origin/main"], timeout=10)
        parts = ab.stdout.split() if ab.returncode == 0 else []
        if len(parts) == 2:
            ahead, behind = int(parts[0]), int(parts[1])
        return {"branch": branch, "hash": h or "—", "subject": subject or "—", "date": date or "—",
                "dirty": dirty, "ahead": ahead, "behind": behind, "detached": branch == "HEAD"}
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {_scrub(str(e))}"[:150]}


def _detect_rebuild(old: str, new: str) -> bool:
    """Менялись ли requirements.txt/Dockerfile в old..new → нужна ручная пересборка образа."""
    if not old or not new or old == new:
        return False
    diff = _git(["diff", "--name-only", f"{old}..{new}"], timeout=10)
    files = diff.stdout.split() if diff.returncode == 0 else []
    return any(f in ("backend/requirements.txt", "backend/Dockerfile") for f in files)


def _post_update(old: str) -> dict:
    """Общий хвост pull/force: alembic (мягко) + новый hash/subject + детект пересборки."""
    try:
        mig = subprocess.run(["alembic", "upgrade", "head"], cwd=_APP,
                             capture_output=True, text=True, timeout=120)
        alembic_warn = "" if mig.returncode == 0 else _scrub(mig.stderr.strip())[:150]
    except FileNotFoundError:
        alembic_warn = "alembic не установлен в контейнере — миграции пропущены (пересобери образ)"
    cur = deploy_status()
    new = cur.get("hash", "")
    return {"ok": True, "old": old, "new": new, "subject": cur.get("subject", ""),
            "needs_rebuild": _detect_rebuild(old, new), "alembic_warn": alembic_warn}


def git_pull() -> dict:
    """Безопасный путь: fetch → pull --ff-only → alembic → детект. needs_force при грязи/расхождении."""
    if not settings.GITHUB_TOKEN:
        return {"ok": False, "error": "GITHUB_TOKEN не задан в .env — нечем авторизовать git pull"}
    if not _LOCK.acquire(blocking=False):
        return {"ok": False, "error": "обновление уже идёт — подожди завершения"}
    try:
        old = deploy_status().get("hash", "")
        try:
            pull = _git(["pull", "--ff-only", _clean_url(), "main"], timeout=120, env=_git_env())
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
        env = _git_env()
        old = deploy_status().get("hash", "")
        try:
            fetch = _git(["fetch", _clean_url(), "main"], timeout=30, env=env)
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
```

- [ ] **Step 4: Прогнать тесты деплоя — зелёные**

Run: `.venv/bin/python -m pytest backend/tests/test_deploy.py -q`
Expected: PASS (все 10 тестов)

- [ ] **Step 5: Полный сьют + pyflakes**

Run: `.venv/bin/python -m pytest backend/tests/ -q` → `217 passed` (207 + 10 новых)
Run: `.venv/bin/python -m pyflakes backend/app backend/tests` → чисто

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/deploy.py backend/tests/test_deploy.py
git commit -m "feat(deploy): services/deploy.py — статус, ff-pull, force-reset, детект (T1)"
```

---

### Task 2: `panel.py` хендлеры + `diag.html` UI

**Files:**
- Modify: `backend/app/api/panel.py` (переписать `/admin/pull` ~526-569; добавить `/admin/force-pull`; `diag_view` ~190-202 добавить статус)
- Modify: `backend/app/templates/diag.html` (статус-строка ~9-17; кнопка force в станции ~72-82)
- Test: `backend/tests/test_deploy_panel.py` (новый)

**Interfaces:**
- Consumes: `deploy.deploy_status()`, `deploy.git_pull()`, `deploy.git_force_pull()` (Task 1).
- Produces: роут `POST /admin/force-pull`; `diag_view` кладёт `status=deploy.deploy_status()` в контекст.

- [ ] **Step 1: Написать падающие тесты `backend/tests/test_deploy_panel.py`**

```python
"""Хендлеры деплоя через TestClient: мокаем deploy-сервис, проверяем баннеры/редиректы/рендер."""
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
```

- [ ] **Step 2: Прогнать — падают (нет /admin/force-pull, нет "позади origin")**

Run: `.venv/bin/python -m pytest backend/tests/test_deploy_panel.py -q`
Expected: FAIL (404 на force-pull / текста нет)

- [ ] **Step 3: Переписать хендлеры в `panel.py`**

Заменить весь блок `@router.post("/admin/pull")` … по конец `git_pull_action` (строки ~526-569) на:

```python
def _pull_banner(r: dict):
    """Единый баннер из dict deploy.git_pull()/git_force_pull()."""
    if not r.get("ok"):
        return _back("/diag", err=r.get("error", "обновление не удалось"))
    warn = f" ⚠ миграции: {r['alembic_warn']}" if r.get("alembic_warn") else ""
    rebuild = " · нужна пересборка образа: docker compose up -d --build" if r.get("needs_rebuild") else ""
    verb = "Принудительно обновлено" if r.get("forced") else "Обновлено"
    subj = r.get("subject", "")
    if r["old"] == r["new"]:
        return _back("/diag", msg=f"Уже свежая версия: {r['new']} «{subj}»{warn}{rebuild}")
    return _back("/diag", msg=f"{verb}: {r['old']}→{r['new']} «{subj}»{warn}{rebuild}")


@router.post("/admin/pull")
def git_pull_action():
    from app.services import deploy
    return _pull_banner(deploy.git_pull())


@router.post("/admin/force-pull")
def git_force_pull_action():
    from app.services import deploy
    return _pull_banner(deploy.git_force_pull())
```

(`/admin/check-updates` ниже НЕ трогаем — работает как есть.)

- [ ] **Step 4: `diag_view` — добавить статус в контекст**

В `diag_view` (~190-202) заменить строку контекста `"version": _version.current_version(),` на
`"status": _deploy.deploy_status(),` и импорт вверху функции `from app.services import version as _version`
заменить/дополнить на `from app.services import deploy as _deploy`. (Строку `from app.services import
version as _version` можно удалить, если `_version` больше не используется в функции — проверь; в diag
он был только для version-контекста.)

- [ ] **Step 5: `diag.html` — статус-строка (заменить блок ~9-17)**

```html
<div class="card" style="max-width:920px; margin-bottom:14px">
  <b>Версия кода:</b>
  {% if status.error %}<span style="color:var(--bad)">git недоступен — {{ status.error }}</span>
  {% else %}<code>{{ status.hash }}</code> · {{ status.subject }} · <span class="hint">{{ status.date }}</span>
  <br><b>Ветка:</b> <code>{{ status.branch }}</code> ·
    {% if status.detached %}<span style="color:var(--bad)">detached HEAD — нужна ⚠ Принудительно обновить</span>
    {% elif status.dirty %}<span style="color:var(--bad)">⚠ грязно (есть локальные правки — обычный pull откажет)</span>
    {% elif status.ahead and status.behind %}<span style="color:var(--bad)">⚠ разошлось (впереди {{ status.ahead }}, позади {{ status.behind }})</span>
    {% elif status.behind %}<span style="color:var(--acc2)">позади origin на {{ status.behind }}</span>
    {% elif status.ahead %}<span class="hint">впереди origin на {{ status.ahead }}</span>
    {% else %}<span style="color:var(--ok)">✓ чисто · актуально</span>{% endif %}
  {% endif %}
  {% if can_pull %}
  <form method="post" action="/admin/check-updates" style="display:inline; margin-left:12px">
    <button class="btn" title="git ls-remote — сравнить с origin/main">проверить обновления</button>
  </form>{% endif %}
</div>
```

- [ ] **Step 6: `diag.html` — кнопка force в станции (заменить блок `.go` ~72-82)**

```html
  <div class="go">
    {% if can_pull %}
    <form method="post" action="/admin/pull"
          onsubmit="return confirm('Забрать свежий код из git (main) и применить миграции?')">
      <button class="btn-acc" title="git pull --ff-only + alembic upgrade head; результат — баннером сверху">⇩ Обновить из git</button>
    </form>
    <form method="post" action="/admin/force-pull"
          onsubmit="return confirm('Принудительно привести код к origin/main. Локальные правки на боксе будут потеряны (.env и ключи сохранятся). Продолжить?')">
      <button class="btn-bad" title="git fetch + checkout -f -B main до origin/main — чинит грязное/разошедшееся/detached дерево, когда обычный pull отказал">⚠ Принудительно обновить</button>
    </form>
    {% else %}
    <span class="hint">Кнопки выключены: задай <b>GITHUB_TOKEN</b> (fine-grained PAT, read-only Contents)
      в <code>.env</code> и смонтируй репозиторий (<code>.:/repo</code>) — см. docs/DEPLOY.md.</span>
    {% endif %}
  </div>
```

Также обновить текст `<div class="what">` станции (~67-71): добавить фразу про force и рекомендацию
Basic-auth. Заменить содержимое `<div class="what">…</div>` на:
```html
  <div class="what"><b>⇩ Обновить</b> = <b>git pull --ff-only</b> из main + <b>alembic upgrade head</b>
    прямо в контейнере; backend и worker подхватят код авто-релоадом — консоль не нужна. Если дерево
    грязное/история разошлась — обычный pull откажет, тогда <b>⚠ Принудительно обновить</b> (fetch +
    reset до origin/main; локальные правки потеряются, <code>.env</code> и ключи — нет). Смена
    <b>requirements.txt/Dockerfile</b> всё равно требует <code>docker compose up -d --build</code>
    (баннер предупредит). LAN-панель с деструктивной кнопкой стоит закрыть Basic-auth
    (<code>PANEL_USER</code>/<code>PANEL_PASS</code> в .env).</div>
```

- [ ] **Step 7: Прогнать тесты панели + полный сьют**

Run: `.venv/bin/python -m pytest backend/tests/test_deploy_panel.py -q` → PASS (4)
Run: `.venv/bin/python -m pytest backend/tests/ -q` → `221 passed` (217 + 4)
Run: `.venv/bin/python -m pyflakes backend/app backend/tests` → чисто

- [ ] **Step 8: Commit**

```bash
git add backend/app/api/panel.py backend/app/templates/diag.html backend/tests/test_deploy_panel.py
git commit -m "feat(deploy): хендлеры /admin/pull|force-pull + статус-строка /diag (T2)"
```

---

### Task 3: Инфра — воркер watchfiles + requirements + DEPLOY.md

**Files:**
- Modify: `docker-compose.yml` (команда воркера ~68)
- Modify: `backend/requirements.txt` (добавить `watchfiles`)
- Modify: `docs/DEPLOY.md` (раздел про кнопку + новый поток)

**Interfaces:** нет (конфиг/доки).

- [ ] **Step 1: `docker-compose.yml` — воркер под watchfiles**

Заменить строку воркера (~68):
```yaml
    command: python -m app.workers.scheduler
```
на:
```yaml
    # watchfiles (в образе через uvicorn[standard]) перезапускает шедулер при правке *.py в /app —
    # код воркера подхватывается после git-pull живьём, без пересборки/рестарта контейнера.
    command: watchfiles 'python -m app.workers.scheduler' /app
```

- [ ] **Step 2: `backend/requirements.txt` — watchfiles явной строкой**

Добавить строку (после `uvicorn[standard]>=0.32`):
```
watchfiles>=0.24          # авто-reload воркера (worker command) — уже тянется uvicorn[standard], фиксируем явно
```

- [ ] **Step 3: Проверить, что образ содержит watchfiles (иначе пересборка)**

Run: `docker compose run --rm backend python -c "import watchfiles; print(watchfiles.__version__)"`
Expected: печатает версию (watchfiles уже в образе). Если ModuleNotFoundError — образ старый, нужен
`docker compose build` (это dev-проверка; на боксе активация = `docker compose up -d --build`).
Примечание: если docker недоступен в dev-среде — пропустить, проверка выполнится на боксе при активации.

- [ ] **Step 4: `docs/DEPLOY.md` — обновить раздел «Кнопка Обновить из git»**

Заменить подраздел «### Кнопка «Обновить из git» в панели (без консоли)» (строки ~74-85) на:

```markdown
### Обновление из панели (без консоли)
Панель → **Диагностика** показывает статус дерева (ветка · чисто/грязно · позади/впереди origin)
и две кнопки:
- **⇩ Обновить из git** — `git pull --ff-only` из main + `alembic upgrade head` в контейнере.
  backend (`--reload`) и worker (`watchfiles`) подхватывают код живьём. Безопасно: не затирает
  локальное, при грязи/расхождении честно откажет и предложит force.
- **⚠ Принудительно обновить** — `git fetch` + `git checkout -f -B main FETCH_HEAD`: приводит код
  к origin/main из ЛЮБОГО состояния (грязное/разошедшееся/detached дерево). Локальные правки
  теряются; `git clean` не вызывается → `.env` и `backend/aapanel.pem` (untracked) сохраняются.
  Под JS-confirm.

Как работает: `docker-compose.yml` монтирует весь репо `.:/repo` (с `.git`), в образе стоит `git`;
тянем по HTTPS с fine-grained PAT (`GITHUB_TOKEN` в `.env`, права Contents: Read-only) через
`http.extraheader` — токен не в argv и скраббится в баннерах. Воркер обёрнут в `watchfiles`, поэтому
его код тоже обновляется без рестарта контейнера.

**Ограничение (единственный консольный случай):** смена `requirements.txt`/`Dockerfile` требует
пересборки образа — панель это детектит по диффу и пишет в баннер «нужна пересборка: `docker compose
up -d --build`».

**Активация фичи на боксе (один раз):** `docker compose up -d --build` (образ получит `watchfiles`
из requirements + воркер пересоздастся с новой командой). Дальше — всё из UI.

**Безопасность:** появилась деструктивная кнопка на LAN-панели без авторизации — настоятельно
закрой Basic-auth: `PANEL_USER`+`PANEL_PASS` в `.env` бокса (см. `app/main.py`).
```

- [ ] **Step 5: Регресс (доки/конфиг код не ломают) + pyflakes**

Run: `.venv/bin/python -m pytest backend/tests/ -q` → `221 passed`
Run: `.venv/bin/python -m pyflakes backend/app backend/tests` → чисто

- [ ] **Step 6: Commit**

```bash
git add docker-compose.yml backend/requirements.txt docs/DEPLOY.md
git commit -m "feat(deploy): воркер под watchfiles + DEPLOY.md под новый UI-поток (T3)"
```

---

## Self-Review (проверка плана против спеки)

**Покрытие спеки:**
- Машина состояний (статус → ff-pull → force → alembic → детект) → T1 `deploy.py`. ✓
- `deploy_status/git_pull/git_force_pull` контракты → T1 (dict-формы дословно). ✓
- Single-flight, скраббинг токена, extraheader-env, таймауты → T1. ✓
- force = fetch + checkout -f -B (без git clean, untracked целы) → T1 + тест `test_force_pull_no_git_clean`. ✓
- Детект requirements.txt/Dockerfile → T1 `_detect_rebuild` + тест. ✓
- Хендлеры /admin/pull (переписан) + /admin/force-pull (новый) + /diag статус → T2. ✓
- UI: статус-строка (branch/dirty/ahead/behind/detached) + кнопка force + текст → T2 diag.html. ✓
- Воркер watchfiles + requirements + DEPLOY.md + рекомендация auth → T3. ✓
- Тесты офлайн (мок subprocess), pyflakes, baseline 207→221 → все задачи. ✓

**Плейсхолдеры:** нет TBD/TODO; весь код и тесты — финальные.

**Согласованность имён:** `deploy_status`/`git_pull`/`git_force_pull`/`_post_update`/`_detect_rebuild`/
`_scrub`/`_git_env`/`_clean_url`/`_LOCK`/`_pull_banner` — единообразны между T1 (определение) и T2
(потребление). Dict-ключи (`ok`/`old`/`new`/`subject`/`needs_rebuild`/`alembic_warn`/`needs_force`/
`forced`/`error`/`branch`/`dirty`/`ahead`/`behind`/`detached`) совпадают в T1, тестах и T2-баннере/шаблоне.

**Числа тестов:** 207 baseline → +10 (T1) = 217 → +4 (T2) = 221. Сверено в шагах.

**Вне области (YAGNI):** Docker-контроль из панели, хелпер-служба, авто-пересборка, live pip install,
принудительный Basic-auth — исключены (см. спеку «Вне области»).
