# Diag-Alert Banner Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Показать глобальный баннер «нет связи с внешними инструментами» на любом экране панели, когда что-то внешнее упало, с кнопкой «перепроверить» и крестиком «скрыть до конца сессии».

**Architecture:** Новый модуль `services/diag_cache.py` кэширует результат `run_diagnostics()` (медленный, Wayback ~15с) + вычисляет `alert()` (список упавших внешних инструментов). Фоновая asyncio-задача в lifespan панели обновляет кэш раз в 5 мин (шедулер-воркер — отдельный процесс, до памяти панели не достаёт). Jinja-global `diag_alert()` читает кэш при рендере; баннер в `base.html` рендерится над flash-блоком, крестик прячет его через `sessionStorage` по сигнатуре набора упавших.

**Tech Stack:** Python 3.12, FastAPI, Jinja2, offline-SQLite тест-харнесс (`backend/tests/conftest.py`), pytest.

**Spec:** `docs/superpowers/specs/2026-07-08-diag-alert-banner-design.md`

## Global Constraints

- **Правило включения:** «внешний» = всё из `_spec()` КРОМЕ `db` (PostgreSQL в docker-compose комбайна). Дериватор — константа `_NON_EXTERNAL = {"db"}` в `diag_cache.py`; арность кортежей `_spec()` НЕ менять (её распаковывают `_run_one`, `run_diagnostics`, self-check).
- **В баннер попадают только `status == "fail"`.** `skip` (нет кред в .env) — не авария, не нудим.
- **Свежесть = кэш + фон, НЕ живой пинг на запрос.** `REFRESH_SEC = 300`.
- **Дизайн-контракт:** светлая CMS (тёплая бумага + белые карточки, один оранжевый акцент); тёмное/индустриальное ЗАПРЕЩЕНО; принцип «шильдика» — каждый контрол подписан ровно тем, что делает (title-подсказки на обеих кнопках); CSS-классы только из `base.html` (`.flash.warn` — новый вариант рядом с `.flash.err`, литеральный hex как у `.flash.msg`/`.flash.err`); UI на русском.
- **Гигиена тестов:** оффлайн-SQLite-харнесс остаётся герметичным (сеть не трогаем — `run_diagnostics` замокан во всех тестах); pyflakes чист; вывод тестов чистый.
- **Гейты не затрагиваются:** фича — чистая диагностика панели; никаких вызовов confirm/execute/mark_edited/оркестратора.
- **Прогон тестов:** `.venv/bin/python -m pytest backend/tests/ -q` · линт: `.venv/bin/python -m pyflakes backend/app backend/tests`.

---

## File Structure

- **Create** `backend/app/services/diag_cache.py` — кэш `run_diagnostics()` + `alert()` (единственная новая логика).
- **Create** `backend/tests/test_diag_alert.py` — все тесты фичи (unit + render + lifespan-смоук).
- **Modify** `backend/app/main.py` — lifespan + фоновый `_diag_loop`.
- **Modify** `backend/app/api/panel.py` — Jinja-global `diag_alert`, роут `POST /diag/refresh`, `diag_view` кормит кэш.
- **Modify** `backend/app/templates/base.html` — CSS `.flash.warn` + баннер над flash-блоком.

---

### Task 1: `diag_cache.py` — кэш + алерт

**Files:**
- Create: `backend/app/services/diag_cache.py`
- Create: `backend/tests/test_diag_alert.py`

**Interfaces:**
- Consumes: `app.services.diagnostics.run_diagnostics() -> list[dict]` (каждый dict имеет ключи `key`, `label`, `status` среди прочих).
- Produces:
  - `REFRESH_SEC: int = 300`
  - `refresh() -> list[dict]` — прогоняет `run_diagnostics()`, кладёт результат+время (UTC) в модульный кэш под `threading.Lock`, возвращает checks.
  - `alert() -> dict | None` — `None` пока кэша нет; иначе `{"down": [label...], "sig": "ключи,через,запятую", "checked_at": datetime}`. `down` только по внешним `fail` в порядке `_spec()`; `sig` = sorted keys через запятую.
  - Модульные имена для тестов/фикстур: `run_diagnostics` (импортирован в namespace модуля — monkeypatch-точка), `_checks`, `_checked_at`.

- [ ] **Step 1: Написать падающие тесты**

Создать `backend/tests/test_diag_alert.py`:

```python
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
```

- [ ] **Step 2: Прогнать — убедиться, что падает**

Run: `.venv/bin/python -m pytest backend/tests/test_diag_alert.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.diag_cache'`.

- [ ] **Step 3: Реализовать модуль**

Создать `backend/app/services/diag_cache.py`:

```python
"""Кэш диагностики внешних инструментов + алерт для глобального баннера панели.

run_diagnostics() медленный (Wayback ~15с) — не гоняем на каждый запрос. Фоновая
задача в main.py дёргает refresh() раз в REFRESH_SEC; Jinja-global diag_alert() читает
alert() из кэша мгновенно. Роут-рефреш и фоновая задача пишут из разных потоков — под _LOCK.
"""
import threading
from datetime import datetime, timezone

from app.services.diagnostics import run_diagnostics

REFRESH_SEC = 300  # тот же ритм, что тик автопилота

_NON_EXTERNAL = {"db"}  # PostgreSQL живёт в docker-compose комбайна; всё остальное — внешнее

_LOCK = threading.Lock()
_checks: list[dict] | None = None
_checked_at: datetime | None = None


def refresh() -> list[dict]:
    """Прогоняет run_diagnostics(), кладёт результат+время в кэш, возвращает checks."""
    global _checks, _checked_at
    checks = run_diagnostics()
    now = datetime.now(timezone.utc)
    with _LOCK:
        _checks = checks
        _checked_at = now
    return checks


def alert() -> dict | None:
    """None, пока кэша нет (до первой проверки). Иначе dict для баннера; down может быть
    пуст (всё поднялось) — тогда баннер не рендерится."""
    with _LOCK:
        if _checks is None:
            return None
        down = [c for c in _checks
                if c["key"] not in _NON_EXTERNAL and c["status"] == "fail"]
        return {
            "down": [c["label"] for c in down],           # лейблы в порядке _spec()
            "sig": ",".join(sorted(c["key"] for c in down)),
            "checked_at": _checked_at,
        }
```

- [ ] **Step 4: Прогнать — убедиться, что зелёные**

Run: `.venv/bin/python -m pytest backend/tests/test_diag_alert.py -q`
Expected: PASS (4 passed).

- [ ] **Step 5: Линт + коммит**

```bash
.venv/bin/python -m pyflakes backend/app backend/tests
git add backend/app/services/diag_cache.py backend/tests/test_diag_alert.py
git commit -m "diag-alert: кэш run_diagnostics + alert() (external-fail, sig)"
```

---

### Task 2: `panel.py` — Jinja-global, `POST /diag/refresh`, `diag_view` кормит кэш

**Files:**
- Modify: `backend/app/api/panel.py` (импорты ~22; регистрация global после фильтров, строки 29–31; `diag_view` строки 188–200; новый роут после `diag_view`)
- Modify: `backend/tests/test_diag_alert.py` (добавить тесты)

**Interfaces:**
- Consumes: `diag_cache.refresh()`, `diag_cache.alert()` (Task 1); `_back(url, msg=...)` (panel.py:39); `PING_TIMEOUT` (diagnostics).
- Produces:
  - Jinja-global `diag_alert` (= `diag_cache.alert`) — используется баннером в Task 3.
  - Роут `POST /diag/refresh` — синхронный `refresh()`, 303-редирект на Referer с `?msg=`.

- [ ] **Step 1: Написать падающие тесты**

Дописать в `backend/tests/test_diag_alert.py`:

```python
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
```

Примечание для реализатора: `referer` в тесте — полный `http://testserver/...`, иначе same-origin CSRF-guard (`main.py:csrf_guard`) вернёт 403 (относительный referer имеет пустой netloc ≠ host `testserver`).

- [ ] **Step 2: Прогнать — убедиться, что падает**

Run: `.venv/bin/python -m pytest backend/tests/test_diag_alert.py -k "refresh_redirects or view_populates" -q`
Expected: FAIL — `POST /diag/refresh` даёт 405/404 (роута нет); `diag_view` не заполняет кэш (`alert()` == None).

- [ ] **Step 3: Реализовать — импорт + Jinja-global**

В `backend/app/api/panel.py` после строки 21 (`from app.config import settings`) добавить импорт:

```python
from app.services import diag_cache
```

После строки 31 (последний `templates.env.filters[...]`) добавить регистрацию global:

```python
templates.env.globals["diag_alert"] = diag_cache.alert   # баннер в base.html читает кэш
```

- [ ] **Step 4: Реализовать — `diag_view` кормит кэш**

В `backend/app/api/panel.py` заменить строки 191–192:

```python
    from app.services.diagnostics import run_diagnostics, PING_TIMEOUT
    checks = run_diagnostics()
```

на:

```python
    from app.services.diagnostics import PING_TIMEOUT
    checks = diag_cache.refresh()   # та же цена (живой прогон) + кладём в кэш -> баннер консистентен с /diag
```

- [ ] **Step 5: Реализовать — роут `POST /diag/refresh`**

В `backend/app/api/panel.py` сразу после `diag_view` (после строки 200, перед `@router.get("/settings"...)`) добавить:

```python
@router.post("/diag/refresh")
def diag_refresh(request: Request):
    """Кнопка «перепроверить» в баннере: синхронный прогон диагностики (≤20с, пинги
    параллельны), редирект назад — оператор остаётся на своём экране, баннер отражает свежий кэш."""
    diag_cache.refresh()
    back = request.headers.get("referer") or "/"
    return _back(back, msg="Статусы внешних инструментов перепроверены")
```

- [ ] **Step 6: Прогнать — убедиться, что зелёные**

Run: `.venv/bin/python -m pytest backend/tests/test_diag_alert.py -q`
Expected: PASS (6 passed).

- [ ] **Step 7: Линт + коммит**

```bash
.venv/bin/python -m pyflakes backend/app backend/tests
git add backend/app/api/panel.py backend/tests/test_diag_alert.py
git commit -m "diag-alert: Jinja-global diag_alert + POST /diag/refresh + /diag кормит кэш"
```

---

### Task 3: `base.html` — CSS `.flash.warn` + баннер

**Files:**
- Modify: `backend/app/templates/base.html` (CSS после строки 84; баннер перед строкой 344)
- Modify: `backend/tests/test_diag_alert.py` (добавить render-тесты)

**Interfaces:**
- Consumes: Jinja-global `diag_alert()` (Task 2); класс `.flash` и `.btn-sm` (уже в base.html); `diag_cache.refresh()` для наполнения кэша в тесте.
- Produces: DOM-узел `id="diag-alert"` с `data-sig`, формой `POST /diag/refresh`, крестиком; виден по умолчанию, JS прячет по `sessionStorage`.

- [ ] **Step 1: Написать падающие render-тесты**

Дописать в `backend/tests/test_diag_alert.py`:

```python
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
```

- [ ] **Step 2: Прогнать — убедиться, что падает**

Run: `.venv/bin/python -m pytest backend/tests/test_diag_alert.py -k "banner_renders or no_banner" -q`
Expected: FAIL — `id="diag-alert"` не найден (баннера в шаблоне ещё нет).

- [ ] **Step 3: Реализовать — CSS `.flash.warn`**

В `backend/app/templates/base.html` после строки 84 (`.flash.err { ... }`) добавить:

```css
  .flash.warn { border-color:#ecd9a8; background:#fbf3dd; color:#7a5d14; }
```

- [ ] **Step 4: Реализовать — баннер над flash-блоком**

В `backend/app/templates/base.html` перед строкой 344 (`{% set _msg = request.query_params.get('msg') %}`), сразу под `<main>` (строка 343), вставить:

```jinja
{% set _alert = diag_alert() %}
{% if _alert and _alert.down %}
<div class="flash warn" id="diag-alert" data-sig="{{ _alert.sig }}">
  <span class="tag">Связь</span>
  <span style="flex:1">Нет связи: {{ _alert.down|join(', ') }}
        · проверено {{ _alert.checked_at.strftime('%H:%M') }} UTC</span>
  <form method="post" action="/diag/refresh" style="display:inline">
    <button class="btn-sm" title="Прогнать диагностику ещё раз (до 20 секунд)">↻ перепроверить</button>
  </form>
  <button type="button" class="btn-sm" title="Скрыть до конца сессии браузера"
          onclick="sessionStorage.setItem('diagDismiss', this.closest('#diag-alert').dataset.sig); this.closest('#diag-alert').remove()">×</button>
</div>
<script>
  if (sessionStorage.getItem('diagDismiss') === document.getElementById('diag-alert').dataset.sig)
    document.getElementById('diag-alert').remove();
</script>
{% endif %}
```

Заметки:
- **Видим по умолчанию, JS прячет** — без JS баннер просто всегда виден (безопасный дефолт); скрипт стоит сразу после div и убирает его по совпадению `sessionStorage['diagDismiss']` с текущим `data-sig` до отрисовки.
- Текстовый span получает `flex:1` — у `.flash` уже `display:flex; gap:12px` (base.html:81–82), так кнопки прижимаются вправо.
- Сигнатура = набор упавших: упало что-то НОВОЕ → sig другой → баннер всплывает снова.

- [ ] **Step 5: Прогнать — убедиться, что зелёные**

Run: `.venv/bin/python -m pytest backend/tests/test_diag_alert.py -q`
Expected: PASS (8 passed).

- [ ] **Step 6: Линт + коммит**

```bash
.venv/bin/python -m pyflakes backend/app backend/tests
git add backend/app/templates/base.html backend/tests/test_diag_alert.py
git commit -m "diag-alert: баннер .flash.warn в base.html + sessionStorage-dismiss"
```

---

### Task 4: `main.py` — фоновый цикл автопроверки (lifespan)

**Files:**
- Modify: `backend/app/main.py` (импорты 2–9; lifespan + `_diag_loop` перед `app = FastAPI(...)`; строка 11)
- Modify: `backend/tests/test_diag_alert.py` (добавить смоук-тест цикла)

**Interfaces:**
- Consumes: `diag_cache.refresh`, `diag_cache.REFRESH_SEC` (Task 1).
- Produces: фоновая задача `_diag_loop`, стартующая с приложением (lifespan), обновляющая кэш каждые `REFRESH_SEC`.

- [ ] **Step 1: Написать падающий смоук-тест**

Дописать в `backend/tests/test_diag_alert.py`:

```python
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
```

- [ ] **Step 2: Прогнать — убедиться, что падает**

Run: `.venv/bin/python -m pytest backend/tests/test_diag_alert.py::test_diag_loop_fires_refresh_on_startup -q`
Expected: FAIL — `fired.wait(timeout=5)` вернёт `False` (lifespan/цикла нет), assert падает.

- [ ] **Step 3: Реализовать — lifespan + цикл**

В `backend/app/main.py` заменить блок импортов (строки 2–9):

```python
import base64
import secrets
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from starlette.responses import Response

from app.config import settings
```

на:

```python
import asyncio
import base64
import secrets
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from starlette.responses import Response

from app.config import settings
from app.services import diag_cache


async def _diag_loop():
    """Фоново обновляет кэш диагностики раз в REFRESH_SEC — держит глобальный баннер
    свежим. Шедулер-воркер — отдельный процесс docker-compose, до памяти панели не
    достаёт, поэтому автопроверка живёт здесь, внутри процесса панели."""
    while True:
        try:
            await asyncio.to_thread(diag_cache.refresh)
        except Exception:  # noqa: BLE001 — диагностика не должна ронять панель; следующий цикл повторит
            pass
        await asyncio.sleep(diag_cache.REFRESH_SEC)


@asynccontextmanager
async def lifespan(app):
    task = asyncio.create_task(_diag_loop())   # первая проверка сразу, в фоне (старт не ждёт 20с пингов)
    yield
    task.cancel()
```

Затем заменить строку `app = FastAPI(title="VPN Affiliate Portfolio")` на:

```python
app = FastAPI(title="VPN Affiliate Portfolio", lifespan=lifespan)
```

- [ ] **Step 4: Прогнать — убедиться, что зелёные (весь файл фичи)**

Run: `.venv/bin/python -m pytest backend/tests/test_diag_alert.py -q`
Expected: PASS (9 passed).

- [ ] **Step 5: Прогнать весь сьют + линт (регрессия)**

Run: `.venv/bin/python -m pytest backend/tests/ -q`
Expected: PASS (все существующие тесты + 9 новых; ни один не сломан — обычная фикстура `client` lifespan не гоняет).
Run: `.venv/bin/python -m pyflakes backend/app backend/tests`
Expected: пусто.

- [ ] **Step 6: Коммит**

```bash
git add backend/app/main.py backend/tests/test_diag_alert.py
git commit -m "diag-alert: фоновый цикл автопроверки в lifespan панели"
```

---

## Проверка глазами (после Task 3, до финального ревью)

Локальный serve + Playwright-скриншоты (по дизайн-контракту, см. spec §6):
1. Экран с баннером — замокать кэш с 2 упавшими (`diag_cache.refresh()` c fake на 2 fail), GET `/` → скрин: янтарный `.flash.warn`, лейблы, обе кнопки с title.
2. Экран без баннера — всё ok → баннера нет.
3. После «перепроверить» — POST `/diag/refresh` (или клик) → flash-сообщение «Статусы… перепроверены», баннер отражает свежий кэш.

Критерий: светлая CMS, шильдики (title на «↻ перепроверить» и «×»), баннер над flash-блоком, ничего тёмного/индустриального.

## Self-Review (заполняется автором плана)

**Spec coverage:**
- §4.1 diag_cache (refresh/alert/REFRESH_SEC/_NON_EXTERNAL/Lock) → Task 1. ✓
- §4.2 lifespan-цикл → Task 4. ✓
- §4.3 Jinja-global → Task 2 Step 3. ✓
- §4.4 баннер + `.flash.warn` + sessionStorage-dismiss → Task 3. ✓
- §4.5 `POST /diag/refresh` → Task 2 Step 5. ✓
- §4.6 `/diag` кормит кэш → Task 2 Step 4. ✓
- §5 тесты 1–6 → Task 1 (1–3), Task 2 (4, 6), Task 3 (5); §5 «фоновый цикл смоуком» → Task 4 добавляет дешёвый детерминированный Event-тест (усиление, не противоречит). ✓
- §6 проверка глазами → блок выше. ✓

**Type consistency:** `refresh() -> list[dict]`, `alert() -> dict|None` с ключами `down/sig/checked_at` — одинаково в Task 1 (реализация), Task 2 (global = `diag_cache.alert`), Task 3 (`_alert.down/_alert.sig/_alert.checked_at`). Монки-точка `app.services.diag_cache.run_diagnostics` (импортирована в namespace модуля) — консистентна во всех тестах. ✓

**Placeholder scan:** нет TBD/TODO/«handle edge cases»; каждый шаг с кодом содержит полный код. ✓
