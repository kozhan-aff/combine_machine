# Наблюдаемость машины + M1 как инбокс — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Панель показывает, что машина делает прямо сейчас (включая автопилот из другого процесса), задачу можно остановить, а экран M1 из дампа на 5827 строк становится инбоксом решений.

**Architecture:** Реестр длинных задач переезжает из `dict` в памяти backend в таблицу `job_run` PostgreSQL — это единственный способ увидеть работу воркера, который живёт в отдельном процессе. Сервисы (`run_discovery` / `score_pending` / `recheck_acquirability` / `run_sweep`) сами открывают запись реестра через контекст-менеджер `jobs.track()` и репортят стадию воронки, поэтому прогресс появляется у любого вызывающего — кнопки, оркестратора, cron. UI рисует один компонент «карточка задачи» и на Пульте, и на M1.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.x, Alembic, Jinja2, PostgreSQL 16 (тесты — SQLite in-memory, StaticPool, `check_same_thread=False`).

**Спека:** `docs/superpowers/specs/2026-07-12-panel-m1-observability-design.md`
**Макеты:** `docs/design/*.png` — читать через Read (PNG). Ключевые:
`new-01-пульт-машина-занята.png`, `new-02-пульт-машина-простой.png`,
`new-03-карточка-состояния.png`, `new-04-домены-инбокс.png`,
`new-05-домены-инбокс-пусто.png`, `new-06-причины-отказа.png`, `02-домены-m1.png`.

## Global Constraints

- **Хард-гейты не трогаются.** Оркестратор и любой сервис НИКОГДА не зовут `confirm_order` / `execute_confirmed_order` / `mark_caught` (деньги) и `mark_edited` (редактура). Пакетное одобрение (Task 6) — это `scored → approved`, деньги при этом не тратятся, гейт выкупа остаётся.
- **`integrations/` = только транспорт**, логика в `services/`.
- **Тесты герметичны:** autouse-фикстура `_no_live_network` в `backend/tests/conftest.py` режет живую сеть; ловушки наследуют `BaseException` (на `Exception` их съедают широкие `except Exception` в приложении). Не ослаблять.
- **pyflakes чист:** `.venv/bin/python -m pyflakes backend/app backend/tests`.
- **Тесты:** `.venv/bin/python -m pytest backend/tests/ -q` — сейчас 276 passed. После каждой задачи должно быть зелено.
- **CSS-контракт:** весь CSS инлайн в `backend/app/templates/base.html`; контент-шаблоны — только семантика. Новые классы объявлять в `base.html`, не по месту.
- **Дизайн:** светлая холодная CMS, единственный акцент `--acc #2563c9`, IBM Plex Sans (UI) / JetBrains Mono (числа, домены). Тёмная тема и тёплые/оранжевые акценты запрещены. UI на русском.
- **Коммиты:** сообщение через heredoc (`git commit -F - <<'EOF'`), НЕ через `-m` — бэктики в `-m` ловит zsh.
- **Уже сделано, не делать заново:** «Гейты машины» в подвале сайдбара (`base.html:346-350`) и строка баланса backorder на `/queue` (`queue.html:25-28`) существуют. Макет их отрисовал с живой панели. Спека §7 в этой части описывает существующее.

---

## Структура файлов

| Файл | Задача | Ответственность |
|---|---|---|
| `backend/app/models/job.py` | создать (T1) | модель `JobRun` + частичный уникальный индекс = single-flight между процессами |
| `backend/alembic/versions/0007_job_run.py` | создать (T1) | миграция (голова сейчас `0006`) |
| `backend/app/services/jobs.py` | переписать (T1) | реестр на БД: `track` / `spawn` / `report` / `cancelled` / `live` / `last` |
| `backend/app/services/discovery.py` | править (T2) | `run_discovery()` сам открывает `track`, стадии = источники |
| `backend/app/services/scoring.py` | править (T2) | `score_pending` / `recheck_acquirability` под `track`; `_funnel` репортит стадию; `blind_reason()` |
| `backend/app/services/orchestrator.py` | править (T3) | `run_sweep` под `track`, стадии = `STAGES` |
| `backend/app/api/panel.py` | править (T3, T5, T6, T7) | `spawn`, `/api/jobs/live`, `/run/{job}/cancel`, возврат на `Referer`, инбокс, пул, причины |
| `backend/app/templates/base.html` | править (T4) | CSS карточки задачи + JS-поллер + тонкая полоса в шапке |
| `backend/app/templates/dashboard.html` | править (T5) | блок «Машина сейчас» (занята / простой) |
| `backend/app/templates/domains.html` | переписать (T6, T7) | строка действий, инбокс, готовы к выкупу, модалка причин |
| `backend/app/templates/pool.html` | создать (T6) | полный реестр доменов (нынешняя таблица целиком) |
| `docs/DESIGN.md`, `CLAUDE.md`, спека | править (T8) | зафиксировать новые классы, состояние, поправки |

---

## Task 1: Реестр задач в БД (модель + миграция + `jobs.py`)

Ядро. Совместимость сохраняется намеренно: `jobs.start(name, target)` остаётся как шим,
оборачивающий `target` в `track` — поэтому панель и сервисы, которые ещё передают
`on_progress`, продолжают работать, и 276 тестов остаются зелёными. Шим сносится в Task 3.

**Files:**
- Create: `backend/app/models/job.py`
- Create: `backend/alembic/versions/0007_job_run.py`
- Rewrite: `backend/app/services/jobs.py`
- Modify: `backend/tests/conftest.py:17-26` (регистрация модели для `create_all`)
- Test: `backend/tests/test_jobs_registry.py` (новый), `backend/tests/test_jobs.py` (остаётся как есть — проверка обратной совместимости)

**Interfaces:**
- Produces:
  - `jobs.AlreadyRunning`, `jobs.Cancelled` — исключения.
  - `jobs.track(name: str, *, trigger: str = "manual", stages: list | None = None)` — контекст-менеджер; `AlreadyRunning`, если такой джоб уже идёт.
  - `jobs.spawn(name: str, target) -> bool` — фоновый поток; `False`, если уже идёт.
  - `jobs.report(name, done=None, total=None, current=None, stage=None, message=None) -> None` — no-op вне `track`.
  - `jobs.cancelled(name) -> bool`, `jobs.request_cancel(name) -> bool`.
  - `jobs.live() -> list[dict]`, `jobs.last(name) -> dict | None`, `jobs.progress(name) -> dict`, `jobs.is_running(name) -> bool`.
  - `jobs.start(name, target) -> bool` — legacy-шим (удаляется в Task 3).
  - Форма dict: `{"name","trigger","status","stage","stages","done","total","current","message","error","cancel_requested","running","started_at","finished_at"}`.
  - `stages` = `[{"key": str, "label": str, "state": "done"|"active"|"pending"|"skip"}]`.

**Отступление от спеки §3.1:** параметр `lock=False` для свипа не нужен. Частичный уникальный
индекс срабатывает на любой `running`-строке, а протухшие (контейнер убили) гасит `_reap()` —
замок не может повиснуть навсегда. Спека правится в Task 8.

- [ ] **Step 1: Написать падающий тест реестра**

Создать `backend/tests/test_jobs_registry.py`:

```python
"""Реестр задач в БД: single-flight между процессами, стадии, отмена, итог прогона."""
import pytest

from app.services import jobs

STAGES = [{"key": "rd", "label": "RD из фида"},
          {"key": "whois", "label": "whois-возраст"},
          {"key": "history", "label": "Wayback-история"}]


def test_track_writes_row_and_closes_done():
    with jobs.track("score", stages=STAGES):
        jobs.report("score", done=1, total=3, current="a.ru", stage="whois")
        p = jobs.progress("score")
        assert p["running"] is True and p["done"] == 1 and p["current"] == "a.ru"
        st = {s["key"]: s["state"] for s in p["stages"]}
        assert st == {"rd": "done", "whois": "active", "history": "pending"}
    p = jobs.progress("score")
    assert p["status"] == "done" and p["running"] is False and p["error"] is None


def test_single_flight_between_processes():
    """Второй track на тот же джоб отбивается индексом — это и есть замок панель↔воркер."""
    with jobs.track("discovery"):
        with pytest.raises(jobs.AlreadyRunning):
            with jobs.track("discovery"):
                pass


def test_cancel_marks_cancelled_and_keeps_progress():
    with jobs.track("recheck"):
        jobs.report("recheck", done=34, total=100)
        assert jobs.cancelled("recheck") is False
        jobs.request_cancel("recheck")
        assert jobs.cancelled("recheck") is True
        if jobs.cancelled("recheck"):
            raise jobs.Cancelled()          # так делает сервис между доменами
    p = jobs.progress("recheck")
    assert p["status"] == "cancelled" and p["done"] == 34 and p["total"] == 100


def test_failure_records_stage_where_it_broke():
    """Упавшая задача обязана показать, НА КАКОЙ стадии встала (макет new-03)."""
    with pytest.raises(RuntimeError):
        with jobs.track("score", stages=STAGES):
            jobs.report("score", done=18, total=100, stage="whois")
            raise RuntimeError("A-Parser timeout")
    p = jobs.progress("score")
    assert p["status"] == "failed" and "timeout" in p["error"] and p["done"] == 18
    assert p["stage"] == "whois"


def test_live_shows_running_jobs_only():
    with jobs.track("sweep", trigger="auto"):
        names = [j["name"] for j in jobs.live()]
        assert names == ["sweep"]
        assert jobs.live()[0]["trigger"] == "auto"
    assert jobs.live() == []


def test_last_returns_finished_run_with_message():
    with jobs.track("recheck"):
        jobs.report("recheck", done=200, total=200, message="занято 3 из отобранных")
    assert jobs.last("recheck")["message"] == "занято 3 из отобранных"
    assert jobs.last("discovery") is None


def test_report_outside_track_is_noop():
    """score_domain по одной кнопке и юнит-тесты зовут report без открытого прогона."""
    jobs.report("score", done=1, total=1)     # не должно падать
    assert jobs.progress("score")["running"] is False
```

- [ ] **Step 2: Прогнать — убедиться, что падает**

Run: `.venv/bin/python -m pytest backend/tests/test_jobs_registry.py -q`
Expected: FAIL — `AttributeError: module 'app.services.jobs' has no attribute 'track'`

- [ ] **Step 3: Модель `JobRun`**

Создать `backend/app/models/job.py`:

```python
"""Реестр длинных задач — строка в БД на прогон, а не dict в памяти процесса.

Старый jobs.py жил внутри процесса backend, поэтому свип автопилота (процесс worker)
панели был невидим вовсе. Здесь пишут оба.

started_at/updated_at ставим Python-side tz-aware (как AutonomyRun, не server_default):
на SQLite server_default вернул бы naive-строку и сломал сравнение с now(tz) при отсечке
протухших прогонов.
"""
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Index, Integer, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class JobRun(Base):
    __tablename__ = "job_run"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(32), index=True)          # discovery|score|recheck|sweep
    trigger: Mapped[str] = mapped_column(String(16), default="manual")  # manual (кнопка) | auto (воркер)
    status: Mapped[str] = mapped_column(String(16), default="running")  # running|done|failed|cancelled
    stage: Mapped[str] = mapped_column(String(32), default="")          # ключ текущей стадии
    stages: Mapped[list] = mapped_column(JSONB, default=list)           # [{key,label,state}] — чипы
    done: Mapped[int] = mapped_column(Integer, default=0)
    total: Mapped[int] = mapped_column(Integer, default=0)              # 0 = неопределённый режим
    current: Mapped[str] = mapped_column(String(255), default="")       # что в работе (домен/источник)
    message: Mapped[str] = mapped_column(String(400), default="")       # итог прогона
    error: Mapped[str | None] = mapped_column(String(400), nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # single-flight МЕЖДУ ПРОЦЕССАМИ: воркер не запустит второй score, пока идёт ручной
    # (сегодня может — и они вдвоём жгут квоту A-Parser).
    __table_args__ = (
        Index("uq_job_run_running", "name", unique=True,
              postgresql_where=text("status = 'running'"),
              sqlite_where=text("status = 'running'")),
    )
```

- [ ] **Step 4: Зарегистрировать модель в тестовом харнессе**

`backend/tests/conftest.py` — добавить импорт и в кортеж `_REGISTER_TABLES` (иначе `create_all`
не создаст таблицу и все тесты реестра упадут на `no such table: job_run`):

```python
import app.models.autonomy
import app.models.job
# reference the modules so their table-registration side effect (create_all needs
# every table, incl. index_history from publish.check_index) isn't seen as a dead import
_REGISTER_TABLES = (app.models.domain, app.models.site, app.models.offer, app.models.monitoring,
                    app.models.settings, app.models.autonomy, app.models.job)
```

- [ ] **Step 5: Переписать `backend/app/services/jobs.py`**

Заменить файл целиком:

```python
"""Реестр длинных задач — строка в БД на прогон (кросс-процессный).

Раньше это был dict в памяти процесса backend: свип автопилота крутится в процессе worker,
поэтому панель его не видела вовсе — «машина работает» было непроверяемо на глаз. Теперь
и панель, и воркер пишут в одну таблицу job_run.

КОНТРАКТ:
  track(name, trigger=, stages=)  контекст-менеджер вокруг работы; сам закрывает строку
                                  (done / failed / cancelled). Занято -> AlreadyRunning.
  spawn(name, target) -> bool     запустить target() в фоне (панель); False если уже идёт.
  report(name, ...)               обновить прогресс/стадию текущего прогона; вне track — no-op.
  cancelled(name) -> bool         сервис спрашивает между элементами; True -> поднять Cancelled.
  request_cancel(name)            кнопка «стоп».
  live() / last(name)             что идёт сейчас / итог последнего прогона.

ТЕРМИНАЛЬНЫЙ КОНТРАКТ (JS-компонент держится его же):
  status == "running"    -> идёт; done/total/current/stages — прогресс;
  status == "failed"     -> упал; error — текст, done — где встал, stage — на какой стадии;
  status == "cancelled"  -> остановлен человеком на done/total;
  status == "done"       -> успех, ДАЖЕ если done == total == 0 (discovery без кандидатов).
  done/total — только отображение, не признак терминала.

Замок — частичный уникальный индекс (name) WHERE status='running'. Строку, чей updated_at
старше STALE_MIN, считаем оборванной (контейнер перезапустили) и гасим при следующем
открытии: иначе замок повис бы навсегда и джоб нельзя было бы запустить уже никогда.
"""
import logging
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

STALE_MIN = 15                       # как STALE_MIN оркестратора: running без обновлений = труп
_EXEC = ThreadPoolExecutor(max_workers=2)
_log = logging.getLogger(__name__)


class AlreadyRunning(RuntimeError):
    """Такая задача уже идёт (замок держит другая вкладка или воркер)."""


class Cancelled(Exception):
    """Человек нажал «стоп». Сервис поднимает это между элементами, track ловит."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _reap(db) -> None:
    """Погасить оборванные прогоны — иначе замок вечен, и джоб больше не запустить."""
    from sqlalchemy import select
    from app.models.job import JobRun
    cutoff = _utcnow() - timedelta(minutes=STALE_MIN)
    stale = db.execute(select(JobRun).where(JobRun.status == "running",
                                            JobRun.updated_at < cutoff)).scalars().all()
    for r in stale:
        r.status = "failed"
        r.error = "оборвалась: процесс перезапустили"
        r.finished_at = _utcnow()
    if stale:
        db.commit()


def _running(db, name: str):
    from sqlalchemy import select
    from app.models.job import JobRun
    return db.execute(select(JobRun).where(JobRun.name == name,
                                           JobRun.status == "running")).scalars().first()


def _as_dict(r) -> dict:
    return {"name": r.name, "trigger": r.trigger, "status": r.status, "stage": r.stage,
            "stages": r.stages or [], "done": r.done, "total": r.total, "current": r.current,
            "message": r.message, "error": r.error, "cancel_requested": r.cancel_requested,
            "running": r.status == "running",
            "started_at": r.started_at, "finished_at": r.finished_at}


def _blank() -> dict:
    return {"name": "", "trigger": "", "status": "", "stage": "", "stages": [], "done": 0,
            "total": 0, "current": "", "message": "", "error": None, "cancel_requested": False,
            "running": False, "started_at": None, "finished_at": None}


def _open(name: str, trigger: str, stages: list | None) -> int | None:
    """Атомарно занять замок; вернуть id строки или None (уже идёт)."""
    from sqlalchemy.exc import IntegrityError
    from app.db import SessionLocal
    from app.models.job import JobRun
    with SessionLocal() as db:
        _reap(db)
        row = JobRun(name=name, trigger=trigger, status="running",
                     stages=[{**s, "state": s.get("state", "pending")} for s in (stages or [])])
        db.add(row)
        try:
            db.commit()
        except IntegrityError:              # частичный уникальный индекс: этот джоб уже идёт
            db.rollback()
            return None
        return row.id


def _close(run_id: int, status: str, error: str | None = None) -> None:
    from app.db import SessionLocal
    from app.models.job import JobRun
    with SessionLocal() as db:
        r = db.get(JobRun, run_id)
        if r is None:
            return
        r.status = status
        r.error = error
        r.finished_at = _utcnow()
        r.updated_at = _utcnow()
        if status == "done":                # успех — все чипы гасим в «пройдено»
            r.stages = [s if s.get("state") == "skip" else {**s, "state": "done"}
                        for s in (r.stages or [])]
        db.commit()


@contextmanager
def track(name: str, *, trigger: str = "manual", stages: list | None = None):
    """Обернуть работу записью реестра. Кто бы ни позвал сервис — панель, оркестратор,
    cron воркера — прогресс появляется бесплатно."""
    run_id = _open(name, trigger, stages)
    if run_id is None:
        raise AlreadyRunning(name)
    try:
        yield run_id
    except Cancelled:
        _close(run_id, "cancelled")         # остановлен человеком — это не ошибка
    except BaseException as e:              # BaseException: ловушки сети в тестах — тоже финал
        _close(run_id, "failed", f"{type(e).__name__}: {e}"[:200])
        raise
    else:
        _close(run_id, "done")


def _advance(stages: list, active: str) -> list:
    """Стадии до активной — пройдены, активная — active, после — pending; skip не трогаем.

    Пересчёт, а не «только вперёд»: скоринг гоняет воронку заново на КАЖДОМ домене, и чипы
    обязаны это показывать (новый домен снова начинает с RD).
    """
    out, seen = [], False
    for s in stages or []:
        if s.get("state") == "skip":
            out.append(s)
            continue
        if s["key"] == active:
            seen = True
            out.append({**s, "state": "active"})
        else:
            out.append({**s, "state": "pending" if seen else "done"})
    return out


def report(name: str, done: int | None = None, total: int | None = None,
           current: str | None = None, stage: str | None = None,
           message: str | None = None) -> None:
    """Обновить текущий прогон. Вне track (одиночный score_domain, юнит-тест) — no-op."""
    from app.db import SessionLocal
    with SessionLocal() as db:
        r = _running(db, name)
        if r is None:
            return
        if done is not None:
            r.done = done
        if total is not None:
            r.total = total
        if current is not None:
            r.current = current[:255]
        if message is not None:
            r.message = message[:400]
        if stage is not None:
            r.stage = stage
            r.stages = _advance(r.stages, stage)
        r.updated_at = _utcnow()
        db.commit()


def cancelled(name: str) -> bool:
    from app.db import SessionLocal
    with SessionLocal() as db:
        r = _running(db, name)
        return bool(r and r.cancel_requested)


def request_cancel(name: str) -> bool:
    """Кнопка «стоп»: помечаем прогон; сервис увидит это между элементами."""
    from app.db import SessionLocal
    with SessionLocal() as db:
        r = _running(db, name)
        if r is None:
            return False
        r.cancel_requested = True
        r.updated_at = _utcnow()
        db.commit()
        return True


def is_running(name: str) -> bool:
    from app.db import SessionLocal
    with SessionLocal() as db:
        return _running(db, name) is not None


def live() -> list[dict]:
    """Все идущие задачи — Пульту и полосе в шапке."""
    from sqlalchemy import select
    from app.db import SessionLocal
    from app.models.job import JobRun
    with SessionLocal() as db:
        _reap(db)
        rows = db.execute(select(JobRun).where(JobRun.status == "running")
                          .order_by(JobRun.started_at)).scalars().all()
        return [_as_dict(r) for r in rows]


def last(name: str) -> dict | None:
    """Итог последнего ЗАВЕРШЁННОГО прогона (Пульт в простое)."""
    from sqlalchemy import select
    from app.db import SessionLocal
    from app.models.job import JobRun
    with SessionLocal() as db:
        r = db.execute(select(JobRun).where(JobRun.name == name, JobRun.status != "running")
                       .order_by(JobRun.id.desc()).limit(1)).scalars().first()
        return _as_dict(r) if r else None


def progress(name: str) -> dict:
    """Текущий прогон, иначе последний завершённый, иначе пустая форма."""
    from app.db import SessionLocal
    with SessionLocal() as db:
        r = _running(db, name)
        if r is not None:
            return _as_dict(r)
    return last(name) or _blank()


def spawn(name: str, target) -> bool:
    """Запустить target() в фоне. False — уже идёт (проверка до старта: у одного оператора
    остаточная гонка безвредна, а внутри её всё равно ловит AlreadyRunning)."""
    if is_running(name):
        return False

    def _run():
        try:
            target()
        except AlreadyRunning:
            pass                            # гонку выиграл кто-то другой — молча уходим
        except Exception:                   # noqa: BLE001 — track уже записал failed
            _log.exception("джоб %s упал", name)

    _EXEC.submit(_run)
    return True


def start(name: str, target) -> bool:
    """LEGACY-ШИМ (удаляется в Task 3): оборачивает target в track сам, чтобы вызывающие,
    которые ещё передают on_progress, продолжали работать."""
    def _wrapped():
        with track(name):
            target()
    return spawn(name, _wrapped)


def _reset() -> None:                       # только для тестов
    from sqlalchemy import delete
    from app.db import SessionLocal
    from app.models.job import JobRun
    with SessionLocal() as db:
        db.execute(delete(JobRun))
        db.commit()
```

- [ ] **Step 6: Миграция 0007**

Создать `backend/alembic/versions/0007_job_run.py`:

```python
"""job_run: реестр длинных задач в БД — кросс-процессный прогресс, стадии, стоп

In-memory реестр жил в процессе backend, поэтому свип автопилота (процесс worker) панели
был не виден. Частичный уникальный индекс (name) WHERE status='running' — single-flight
между процессами: воркер не запустит второй score поверх ручного.

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-12
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "job_run",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(32), nullable=False),
        sa.Column("trigger", sa.String(16), nullable=False, server_default="manual"),
        sa.Column("status", sa.String(16), nullable=False, server_default="running"),
        sa.Column("stage", sa.String(32), nullable=False, server_default=""),
        sa.Column("stages", JSONB, nullable=False, server_default="[]"),
        sa.Column("done", sa.Integer, nullable=False, server_default="0"),
        sa.Column("total", sa.Integer, nullable=False, server_default="0"),
        sa.Column("current", sa.String(255), nullable=False, server_default=""),
        sa.Column("message", sa.String(400), nullable=False, server_default=""),
        sa.Column("error", sa.String(400), nullable=True),
        sa.Column("cancel_requested", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_job_run_name", "job_run", ["name"])
    op.create_index("uq_job_run_running", "job_run", ["name"], unique=True,
                    postgresql_where=sa.text("status = 'running'"))


def downgrade() -> None:
    op.drop_index("uq_job_run_running", table_name="job_run")
    op.drop_index("ix_job_run_name", table_name="job_run")
    op.drop_table("job_run")
```

- [ ] **Step 7: Прогнать тесты — новые зелёные, старые не сломаны**

Run: `.venv/bin/python -m pytest backend/tests/ -q`
Expected: PASS — 276 старых + 7 новых = 283 passed. `test_jobs.py` (legacy) обязан остаться
зелёным: он и проверяет, что шим `start()` сохранил старый контракт.

Run: `.venv/bin/python -m pyflakes backend/app backend/tests`
Expected: пусто.

- [ ] **Step 8: Проверить цепочку миграций**

Run: `grep -h "^revision\|^down_revision" backend/alembic/versions/*.py | paste - -`
Expected: линейная цепочка `0001 → … → 0007`, одна голова.

- [ ] **Step 9: Коммит**

```bash
git add backend/app/models/job.py backend/alembic/versions/0007_job_run.py \
        backend/app/services/jobs.py backend/tests/conftest.py backend/tests/test_jobs_registry.py
git commit -F - <<'EOF'
feat(jobs): реестр задач в БД — кросс-процессный прогресс, стадии, стоп (T1)

jobs.py был dict в памяти процесса backend, а свип автопилота крутится в процессе
worker — панель его не видела вовсе. Теперь строка job_run на прогон, пишут оба.
Частичный уникальный индекс (name) WHERE status='running' = single-flight МЕЖДУ
процессами: воркер больше не запустит второй score поверх ручного (сегодня может,
и они вдвоём жгут квоту A-Parser). Протухшие running-строки гасит _reap, иначе
убитый контейнер оставил бы замок навсегда.

start() пока сохранён шимом поверх track — вызывающие с on_progress работают, 276
старых тестов зелёные. Шим уходит в T3.
EOF
```

---

## Task 2: Сервисы M1 под `track` — стадии и стоп

`run_discovery` / `score_pending` / `recheck_acquirability` перестают принимать `on_progress`
и сами открывают запись реестра. Это и есть ответ на «автопилот невидим»: колбэк прокидывала
только панель, а `track` работает у любого вызывающего.

**Files:**
- Modify: `backend/app/services/discovery.py` (`_collect`, `run_discovery`)
- Modify: `backend/app/services/scoring.py` (`_funnel`, `score_domain`, `score_pending`, `recheck_acquirability`, новый `blind_reason`)
- Modify: `backend/tests/test_jobs.py`, `backend/tests/test_sources.py`, `backend/tests/test_recheck_acquirability.py`
- Test: `backend/tests/test_job_stages.py` (новый)

**Interfaces:**
- Consumes: `jobs.track`, `jobs.report`, `jobs.cancelled`, `jobs.Cancelled` (Task 1).
- Produces:
  - `discovery.run_discovery() -> int` (параметра `on_progress` больше НЕТ)
  - `discovery._collect(enabled: dict) -> list[dict]`
  - `scoring.score_pending(limit: int = 100) -> int`
  - `scoring.recheck_acquirability(limit: int = 200) -> dict`
  - `scoring.score_domain(domain_id, clients=None, whois_budget=None, ahrefs_budget=None, job: str | None = None) -> dict`
  - `scoring.FUNNEL_STAGES: list[dict]` — чипы воронки
  - `scoring.blind_reason(d) -> str | None` — «домен оценён при недоступной проверке»

- [ ] **Step 1: Написать падающий тест стадий и отмены**

Создать `backend/tests/test_job_stages.py`:

```python
"""Стадии воронки и стоп-кнопка: чипы показывают, что делается прямо сейчас."""
from app.db import SessionLocal
from app.models.domain import Domain
from app.services import discovery, jobs, scoring


def _seed(n: int) -> None:
    with SessionLocal() as db:
        db.add_all([Domain(domain=f"d{i}.ru", source="backorder", status="discovered",
                           referring_domains=100 - i) for i in range(n)])
        db.commit()


def test_score_pending_reports_funnel_stages(monkeypatch):
    """Пока скорится домен, в реестре видно, на какой стадии воронки он висит."""
    _seed(1)
    seen = []

    def fake_score(did, clients=None, whois_budget=None, ahrefs_budget=None, job=None):
        jobs.report(job, stage="whois")                  # так репортит _funnel
        seen.append(jobs.progress("score")["stage"])
        return {"domain": "d0.ru"}

    monkeypatch.setattr(scoring, "score_domain", fake_score)
    monkeypatch.setattr(scoring, "_make_clients", lambda: {})
    assert scoring.score_pending(limit=10) == 1
    assert seen == ["whois"]
    p = jobs.progress("score")
    assert p["status"] == "done" and p["total"] == 1
    assert [s["key"] for s in p["stages"]] == [s["key"] for s in scoring.FUNNEL_STAGES]


def test_score_pending_stops_on_cancel(monkeypatch):
    """Стоп-кнопка: прогон завершается cancelled, оставшиеся домены не трогаются."""
    _seed(5)
    scored = []

    def fake_score(did, clients=None, whois_budget=None, ahrefs_budget=None, job=None):
        scored.append(did)
        jobs.request_cancel("score")                      # человек нажал «стоп» на первом домене
        return {}

    monkeypatch.setattr(scoring, "score_domain", fake_score)
    monkeypatch.setattr(scoring, "_make_clients", lambda: {})
    scoring.score_pending(limit=5)
    assert len(scored) == 1                              # второй домен уже не начали
    p = jobs.progress("score")
    assert p["status"] == "cancelled" and p["done"] == 1 and p["total"] == 5


def test_discovery_stages_are_sources(monkeypatch):
    """Чипы discovery — включённые источники + дедуп + запись."""
    from app.services.settings import update_settings
    update_settings(sources_enabled={"backorder": True, "cctld": False,
                                     "reg_ru": False, "sweb": False})
    monkeypatch.setattr("app.integrations.backorder.BackorderClient.list_dropping",
                        lambda self, min_links=1: [])
    assert discovery.run_discovery() == 0
    p = jobs.progress("discovery")
    assert p["status"] == "done" and p["error"] is None
    assert [s["key"] for s in p["stages"]] == ["backorder", "dedup", "save"]


def test_blind_reason_flags_unverified_history():
    """Wayback лежал -> домен оценён вслепую; штамповать его нельзя (спека §1.5)."""
    d = Domain(domain="x.ru", score_breakdown={"errors": ["wayback:ConnectError"]})
    assert "Wayback" in scoring.blind_reason(d)
    clean = Domain(domain="y.ru", score_breakdown={"errors": []})
    assert scoring.blind_reason(clean) is None
```

- [ ] **Step 2: Прогнать — падает**

Run: `.venv/bin/python -m pytest backend/tests/test_job_stages.py -q`
Expected: FAIL — `AttributeError: module 'app.services.scoring' has no attribute 'FUNNEL_STAGES'`

- [ ] **Step 3: `discovery.py` — стадии-источники**

Заменить сигнатуры `_collect` и `run_discovery` (`backend/app/services/discovery.py:82` и `:114`):

```python
_SOURCE_RU = {"backorder": "backorder", "cctld": "cctld", "reg_ru": "reg.ru", "sweb": "sweb"}


def _collect(enabled: dict) -> list[dict]:
    """Собрать строки со всех включённых источников. Сбой одного источника не топит остальные.

    Стадию репортим ПЕРЕД походом в источник: сбор идёт секунды, и оператор должен видеть,
    кого именно сейчас опрашиваем (jobs.report вне track — no-op, юнит-тесты не ломаются).
    """
    from app.services import jobs
    rows: list[dict] = []
    for name, Client in _sources().items():
        if not enabled.get(name):
            continue
        jobs.report("discovery", stage=name, current=f"собираю: {_SOURCE_RU.get(name, name)}")
        ...                                    # тело цикла не меняется
    return rows


def run_discovery() -> int:
    """Собрать включённые источники, дедуп по domain (выигрывает бо́льший RD), upsert новых +
    обогащение уже известных discovered-строк. Прогресс — сам, через jobs.track: тогда его
    видно и когда discovery зовёт оркестратор из воркера, а не панель кнопкой."""
    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError
    from app.db import SessionLocal
    from app.models.domain import Domain
    from app.services import jobs
    from app.services.settings import get_settings

    enabled = get_settings()["sources_enabled"]
    stages = ([{"key": k, "label": _SOURCE_RU[k]} for k in _SOURCE_RU if enabled.get(k)]
              + [{"key": "dedup", "label": "дедуп"}, {"key": "save", "label": "запись"}])
    with jobs.track("discovery", stages=stages):
        rows = _collect(enabled)
        jobs.report("discovery", stage="dedup")
        best: dict[str, dict] = {}
        ...                                    # дедуп как был
        if not candidates:
            jobs.report("discovery", done=0, total=0, current="", message="нет кандидатов")
            return 0
        jobs.report("discovery", stage="save")
        ...                                    # запись как была
        jobs.report("discovery", done=1, total=1, current="", message=f"собрано {n} доменов")
        return n
```

Важно: `trigger` для discovery/score, запущенных ИЗ оркестратора, остаётся `manual` — свип
уже помечен `auto` своей строкой; вложенный джоб пометит себя сам, если понадобится. Не
усложняем.

- [ ] **Step 4: `scoring.py` — чипы воронки, стоп, `blind_reason`**

Добавить рядом с `_decide` (верх файла):

```python
# Чипы воронки в панели: ключ -> подпись. Порядок = порядок проверок в _funnel.
# Ahrefs шестой: он платный (капча за штуку) и при max_ahrefs_per_run=0 помечается skip —
# честнее показать выключенную стадию, чем спрятать её.
FUNNEL_STAGES = [
    {"key": "rd", "label": "RD из фида"},
    {"key": "whois", "label": "whois-возраст"},
    {"key": "risk", "label": "РКН/блэклист"},
    {"key": "echo", "label": "эхо в индексе"},
    {"key": "history", "label": "Wayback-история"},
    {"key": "ahrefs", "label": "Ahrefs (платно)"},
]

# Проверки, чей отказ означает «домен судили ВСЛЕПУЮ». Гарды в _decide не дают авто-approve
# без Wayback — домен уходит в scored, то есть В ИНБОКС К ЧЕЛОВЕКУ, и там неотличим от честно
# проверенного. Человек штампует непроверенное, думая, что машина посмотрела историю.
_BLIND_RU = {
    "wayback": "история НЕ проверена: Wayback был недоступен",
    "rkn": "РКН НЕ проверен: реестр не ответил",
    "blacklist": "блэклист НЕ проверен",
    "searxng": "эхо в индексе НЕ проверено",
}


def blind_reason(d) -> str | None:
    """Домен оценён при недоступной проверке — в пакет одобрения он не идёт (спека §1.5)."""
    for e in (d.score_breakdown or {}).get("errors") or []:
        head = str(e).split(":", 1)[0]
        if head in _BLIND_RU:
            return _BLIND_RU[head]
    return None
```

В `_funnel(d, c, st, sig, whois_budget=None, ahrefs_budget=None, job=None)` — добавить параметр
`job` и репорты стадий ровно там, где сегодня стоят комментарии ступеней:

```python
    from app.services import jobs
    if job:
        jobs.report(job, stage="rd")
    ...                                        # T0 — RD/флаги фида
    if job:
        jobs.report(job, stage="whois")
    ...                                        # T1 — whois
    if job:
        jobs.report(job, stage="risk")
    ...                                        # T2 — РКН + Spamhaus
    if job:
        jobs.report(job, stage="echo")
    ...                                        # indexed_echo
    if job:
        jobs.report(job, stage="history")
    ...                                        # T3 — Wayback
    if job:
        jobs.report(job, stage="ahrefs")
    ...                                        # T3b — Ahrefs
```

`score_domain(domain_id, clients=None, whois_budget=None, ahrefs_budget=None, job=None)` —
прокинуть `job` в `_funnel(..., job=job)`.

`score_pending`:

```python
def score_pending(limit: int = 100) -> int:
    """Скорит `discovered` домены. Прогресс и стадии — через jobs.track (см. services/jobs.py).
    Между доменами смотрит стоп-кнопку: «Проверить весь пул» — это часы работы и квоты
    A-Parser, прервать это должно быть можно без рестарта контейнера."""
    from sqlalchemy import select
    from app.db import SessionLocal
    from app.models.domain import Domain
    from app.services import jobs
    from app.services.settings import get_settings

    st = get_settings()
    with SessionLocal() as db:
        rows = db.execute(
            select(Domain.id, Domain.domain).where(Domain.status == "discovered")
            .order_by(Domain.referring_domains.desc().nulls_last())
            .limit(limit)
        ).all()
    stages = [dict(s) for s in FUNNEL_STAGES]
    if int(st["max_ahrefs_per_run"]) == 0:
        stages[-1]["state"] = "skip"           # платная стадия выключена — так и покажем
    clients = _make_clients()
    whois_budget = [int(st["max_whois_per_run"])]
    ahrefs_budget = [int(st["max_ahrefs_per_run"])]
    total = len(rows)
    with jobs.track("score", stages=stages):
        for i, (did, name) in enumerate(rows, 1):
            if jobs.cancelled("score"):
                raise jobs.Cancelled()
            jobs.report("score", done=i - 1, total=total, current=name)
            try:
                score_domain(did, clients, whois_budget, ahrefs_budget, job="score")
            except Exception:  # noqa: BLE001 — падение одного домена не топит батч
                logging.getLogger(__name__).exception("score_domain %s упал", name)
        jobs.report("score", done=total, total=total, current="",
                    message=f"прогнано {total} доменов через воронку")
    return total
```

`recheck_acquirability(limit=200)` — тот же приём: `on_progress` убрать, обернуть в
`with jobs.track("recheck", stages=[{"key": "whois", "label": "whois по донорам"}])`,
проверять `jobs.cancelled("recheck")` в цикле, а сводку писать `jobs.report(..., message=...)`
внутри `track` (сегодня её лепит `panel.py` — она переезжает сюда):

```python
        jobs.report("recheck", done=checked, total=checked, current="",
                    message=f"проверено {checked}: свободны {free}, ждут дропа {waiting}, "
                            f"ЗАНЯТЫ {taken} (отбракованы), не определилось {unknown}")
```

Штамп времени в сообщении больше не нужен: `job_run.finished_at` датирует прогон сам.

- [ ] **Step 5: Переписать тесты, державшиеся за `on_progress`**

- `backend/tests/test_jobs.py` — удалить (его роль забрали `test_jobs_registry.py` и
  `test_job_stages.py`), КРОМЕ регрессии «ноль кандидатов»: перенести её в
  `test_job_stages.py::test_discovery_stages_are_sources`, дополнив проверкой
  `assert jobs.progress("discovery")["message"] == "нет кандидатов"`.
- `backend/tests/test_sources.py:95,122,156` — `lambda enabled, on_progress=None: [...]`
  заменить на `lambda enabled: [...]`.
- `backend/tests/test_recheck_acquirability.py` — вызовы `recheck_acquirability(..., on_progress=...)`
  заменить на `recheck_acquirability(...)`; проверки прогресса — через `jobs.progress("recheck")`.

- [ ] **Step 6: Прогнать тесты**

Run: `.venv/bin/python -m pytest backend/tests/ -q`
Expected: PASS (test_orchestrator/test_pipeline/test_autopilot_panel ещё держатся за
`on_progress` в `run_sweep`/панели — их чинит Task 3; если они падают на сигнатурах
`run_discovery`/`score_pending`, поправить моки в них здесь же).

Run: `.venv/bin/python -m pyflakes backend/app backend/tests` → пусто.

- [ ] **Step 7: Коммит**

```bash
git add backend/app/services/discovery.py backend/app/services/scoring.py backend/tests/
git commit -F - <<'EOF'
feat(m1): discovery/score/recheck сами открывают track — стадии и стоп (T2)

on_progress прокидывала только панель, поэтому те же сервисы, запущенные из воркера,
шли невидимо. Теперь прогресс открывает сам сервис: кто бы его ни позвал — кнопка,
оркестратор, cron — стадии видно.

Чипы воронки: RD -> whois -> РКН/блэклист -> эхо -> Wayback -> Ahrefs (платная стадия
помечается skip при max_ahrefs_per_run=0 — честнее, чем прятать). Между доменами
смотрим стоп: «Проверить весь пул» больше не билет в один конец.

blind_reason(): домен, оценённый при лежащем Wayback, уходит в scored и в инбоксе
неотличим от честно проверенного — человек штампует непроверенное. Теперь видно.
EOF
```

---

## Task 3: Оркестратор + HTTP-поверхность

**Files:**
- Modify: `backend/app/services/orchestrator.py:222-254` (`run_sweep`)
- Modify: `backend/app/api/panel.py:319-367`, `:695-703`
- Modify: `backend/app/services/jobs.py` (удалить шим `start`)
- Modify: `backend/tests/test_orchestrator.py`, `backend/tests/test_pipeline.py`, `backend/tests/test_autopilot_panel.py`
- Test: `backend/tests/test_jobs_api.py` (новый)

**Interfaces:**
- Consumes: `jobs.spawn`, `jobs.track`, `jobs.live`, `jobs.last`, `jobs.request_cancel`.
- Produces:
  - `orchestrator.run_sweep(trigger="cron", respect_master=True) -> dict` (без `on_progress`)
  - `orchestrator.STAGE_RU: dict[str, str]` — русские подписи стадий свипа
  - `GET /api/jobs/live -> {"jobs": [...], "last": {"discovery": {...}|None, ...}}`
  - `POST /run/{job}/cancel` — 303 назад
  - `POST /run/discovery|score|recheck`, `POST /autopilot/run` — 303 на `Referer`
  - Удалены: `GET /run/{job}/progress` (та самая 404-дыра на `recheck`), `jobs.start`

- [ ] **Step 1: Написать падающий тест API**

Создать `backend/tests/test_jobs_api.py`:

```python
"""HTTP-поверхность реестра: живой список, стоп, возврат туда, откуда нажали."""
from app.services import jobs


def test_live_lists_running_job(client):
    with jobs.track("score", stages=[{"key": "rd", "label": "RD из фида"}]):
        jobs.report("score", done=3, total=10, current="a.ru", stage="rd")
        r = client.get("/api/jobs/live")
        assert r.status_code == 200
        body = r.json()
        assert body["jobs"][0]["name"] == "score"
        assert body["jobs"][0]["done"] == 3 and body["jobs"][0]["current"] == "a.ru"
        assert body["jobs"][0]["stages"][0]["state"] == "active"


def test_live_reports_last_run_when_idle(client):
    with jobs.track("recheck"):
        jobs.report("recheck", done=200, total=200, message="занято 3")
    body = client.get("/api/jobs/live").json()
    assert body["jobs"] == []
    assert body["last"]["recheck"]["message"] == "занято 3"
    assert body["last"]["discovery"] is None


def test_cancel_sets_flag(client):
    with jobs.track("score"):
        r = client.post("/run/score/cancel", follow_redirects=False)
        assert r.status_code == 303
        assert jobs.cancelled("score") is True


def test_run_returns_to_page_it_was_pressed_on(client, monkeypatch):
    """Кнопки запуска есть и на Пульте — редирект обязан вернуть туда, откуда нажали."""
    monkeypatch.setattr(jobs, "spawn", lambda name, target: True)
    r = client.post("/run/discovery", headers={"referer": "http://x/"}, follow_redirects=False)
    assert r.headers["location"].startswith("/")
    assert "/domains" not in r.headers["location"]


def test_progress_route_is_gone(client):
    """Старый /run/{job}/progress пускал только discovery|score|sweep, а панель поллила
    ещё и recheck -> 404 -> полоса молча не появлялась. Роут заменён на /api/jobs/live."""
    assert client.get("/run/recheck/progress").status_code == 404
    assert client.get("/run/score/progress").status_code == 404
```

- [ ] **Step 2: Прогнать — падает**

Run: `.venv/bin/python -m pytest backend/tests/test_jobs_api.py -q`
Expected: FAIL — `/api/jobs/live` отдаёт 404.

- [ ] **Step 3: `run_sweep` под `track`**

`backend/app/services/orchestrator.py` — рядом со `STAGES` добавить подписи и переписать
цикл (`on_progress` уходит; `AutonomyRun` остаётся историей автопилота):

```python
STAGE_RU = {"discovery": "поиск", "score": "скоринг", "queue": "очередь",
            "provision": "провижн", "generate": "контент", "publish": "публикация",
            "check_index": "индексация"}


def run_sweep(trigger: str = "cron", respect_master: bool = True) -> dict:
    """Прогнать включённые авто-стадии до гейтов. respect_master=False у ручного запуска.

    ЖЁСТКО: зовёт ТОЛЬКО безопасные сервисы из STAGES. НИКОГДА — confirm_order/
    execute_confirmed_order/mark_caught (деньги) и mark_edited (редактура): эти три гейта
    двигает только человек через роуты панели. Ошибка одной сущности не топит стадию/свип.

    Прогресс пишет сам (jobs.track) — именно поэтому свип из воркера теперь виден Пульту.
    Выключенные тумблером стадии показываем как skip, а не прячем: «стадия отключена» и
    «стадия сломалась» — разные вещи, и оператор обязан их различать.
    """
    from app.services import jobs
    from app.services.autonomy import get_autonomy

    cfg = get_autonomy()
    if respect_master and not cfg["autopilot_on"]:
        return {"skipped": "autopilot_off"}
    run_id = _acquire_lock(trigger)
    if run_id is None:
        return {"skipped": "already_running"}

    enabled = [s for s in STAGES if cfg[s[1]]]
    stages = [{"key": k, "label": STAGE_RU[k],
               "state": "pending" if cfg[flag] else "skip"} for k, flag, _, _ in STAGES]
    total = len(enabled)
    counts, errors, status = {}, [], "done"
    try:
        with jobs.track("sweep", trigger="auto" if trigger == "cron" else "manual",
                        stages=stages):
            for i, (key, _flag, cap_attr, handler) in enumerate(enabled):
                jobs.report("sweep", done=i, total=total, stage=key,
                            current=STAGE_RU[key])
                cap = cfg[cap_attr] if cap_attr else None
                try:
                    n, errs = handler(cap)
                    counts[key] = n
                    errors += [f"{key}: {e}" for e in errs]
                except Exception as e:  # noqa: BLE001 — стадия целиком упала (не одна сущность)
                    errors.append(f"{key}: {type(e).__name__}: {e}")
                    status = "failed"
            jobs.report("sweep", done=total, total=total, current="",
                        message=f"стадий пройдено: {total}" + (f" · ошибок: {len(errors)}" if errors else ""))
    except jobs.AlreadyRunning:
        _finish_run(run_id, "done", {}, ["sweep: реестр занят другим прогоном"])
        return {"skipped": "already_running"}
    _finish_run(run_id, status, counts, errors)
    return {"run_id": run_id, "status": status, "counts": counts, "errors": errors}
```

- [ ] **Step 4: Панель — `spawn`, `/api/jobs/live`, `cancel`, возврат на `Referer`**

`backend/app/api/panel.py`. Заменить блок роутов `/run/*` (строки 319-367) на:

```python
def _back_here(request: Request, msg: str | None = None, err: str | None = None):
    """Вернуть оператора на страницу, с которой он нажал кнопку (запуск есть и на Пульте,
    и на M1). Свои query-параметры чистим: старый ?err= иначе подавит новый ?msg=."""
    raw = request.headers.get("referer") or "/domains"
    p = urlsplit(raw)
    q = urlencode([(k, v) for k, v in parse_qsl(p.query) if k not in ("msg", "err")])
    return _back(urlunsplit(("", "", p.path or "/domains", q, "")), msg=msg, err=err)


@router.post("/run/discovery")
def run_discovery_action(request: Request):
    from app.services import discovery, jobs
    ok = jobs.spawn("discovery", discovery.run_discovery)
    # запущено — баннера НЕТ: прогресс показывает карточка задачи (спека §8)
    return _back_here(request, err=None if ok else "Поиск дропов уже идёт")


@router.post("/run/score")
def run_score_action(request: Request, n: int = Form(5)):
    from app.services import jobs, scoring
    ok = jobs.spawn("score", lambda: scoring.score_pending(limit=n))
    return _back_here(request, err=None if ok else "Проверка уже идёт")


@router.post("/run/recheck")
def run_recheck_action(request: Request, n: int = Form(200)):
    """Перепроверить whois'ом отобранных доноров: не выкупили ли их. Денег не тратит."""
    from app.services import jobs, scoring
    ok = jobs.spawn("recheck", lambda: scoring.recheck_acquirability(limit=n))
    return _back_here(request, err=None if ok else "Перепроверка уже идёт")


@router.post("/run/{job}/cancel")
def run_cancel_action(request: Request, job: str):
    from fastapi import HTTPException
    from app.services import jobs
    if job not in _JOBS:
        raise HTTPException(status_code=404, detail=f"неизвестный джоб: {job}")
    jobs.request_cancel(job)          # сервис увидит флаг между элементами и честно завершится
    return _back_here(request)


@router.get("/api/jobs/live")
def jobs_live():
    """Что машина делает прямо сейчас + итог последнего прогона каждой задачи.
    Один эндпоинт на всю панель: карточки на Пульте/M1 и тонкая полоса в шапке."""
    from fastapi.responses import JSONResponse
    from app.services import jobs
    return JSONResponse(jsonable_encoder({
        "jobs": jobs.live(),
        "last": {name: jobs.last(name) for name in _JOBS},
    }))
```

Наверх файла (рядом с `_MANUAL_STATUSES`):

```python
_JOBS = ("discovery", "score", "recheck", "sweep")   # известные джобы реестра
```

Импорты: `from fastapi.encoders import jsonable_encoder` (datetime → ISO).

`autopilot_run_action` (строка ~695):

```python
@router.post("/autopilot/run")
def autopilot_run_action(request: Request):
    from app.services import jobs, orchestrator
    ok = jobs.spawn("sweep", lambda: orchestrator.run_sweep(trigger="manual",
                                                            respect_master=False))
    return _back_here(request, err=None if ok else "Свип уже идёт")
```

- [ ] **Step 5: Снести шим**

Удалить функцию `start()` из `backend/app/services/jobs.py` (Task 1, Step 5) — вызывающих не осталось.

- [ ] **Step 6: Починить тесты, державшиеся за `on_progress`**

- `backend/tests/test_orchestrator.py:90,134,135` — моки `lambda limit=100, on_progress=None: ...`
  → `lambda limit=100: ...`; `lambda on_progress=None: 0` → `lambda: 0`.
- `backend/tests/test_pipeline.py:106,126` — то же.
- `backend/tests/test_autopilot_panel.py` — если проверяет баннер «Свип запущен…», заменить
  проверку на «редирект 303 и `jobs.is_running('sweep')` / карточка задачи», т.к. баннера у
  запуска больше нет (спека §8).

- [ ] **Step 7: Прогнать**

Run: `.venv/bin/python -m pytest backend/tests/ -q` → PASS
Run: `.venv/bin/python -m pyflakes backend/app backend/tests` → пусто
Run: `grep -rn "on_progress" backend/app/` → пусто (колбэков не осталось)

- [ ] **Step 8: Коммит**

```bash
git add backend/app/services/orchestrator.py backend/app/services/jobs.py \
        backend/app/api/panel.py backend/tests/
git commit -F - <<'EOF'
feat(panel): свип под track + /api/jobs/live + стоп + возврат туда, откуда нажали (T3)

Свип автопилота теперь пишет в реестр сам — Пульт впервые может показать работу
воркера. Выключенные тумблером стадии помечаются skip, а не прячутся: «отключена»
и «сломалась» — разные вещи.

/run/{job}/progress снесён вместе с его багом: он пускал только discovery|score|sweep,
а domains.html поллил ещё и recheck -> 404 -> полоса перепроверки не появлялась НИКОГДА.
Замена — /api/jobs/live (все задачи разом + итоги последних прогонов).

Баннеры «запущено…» убраны: их работу делает карточка задачи. /run/* возвращают на
страницу, с которой нажали, — кнопки запуска есть и на Пульте.
EOF
```

---

## Task 4: Компонент «карточка задачи» + полоса в шапке

**Files:**
- Modify: `backend/app/templates/base.html` (CSS + JS-поллер + контейнеры)
- Modify: `backend/app/templates/domains.html` (снять старый `#prog` и его скрипт: строки 81-140)
- Test: `backend/tests/test_panel_machine.py` (новый)

**Interfaces:**
- Consumes: `GET /api/jobs/live` (Task 3).
- Produces: DOM-контракт — `<div id="machine" data-machine>` (полные карточки; Пульт и M1) и
  `<div id="mbar">` (тонкая полоса в шапке, рендерится всегда, когда нет `#machine`).
  CSS-классы: `.job`, `.job.err`, `.job.cancelled`, `.job-head`, `.job-chips`, `.chip-st`,
  `.chip-st.done|.active|.pending|.skip`, `.job-bar`, `.job-fill`, `.job-num`, `.mbar`.

Макет — `docs/design/new-03-карточка-состояния.png` (три состояния) и
`02-домены-m1.png` (карточка в работе).

- [ ] **Step 1: Тест рендера**

Создать `backend/tests/test_panel_machine.py`:

```python
"""Компонент «машина сейчас»: контейнеры на месте, поллер один, старой полосы нет."""


def test_base_ships_machine_bar_and_poller(client):
    html = client.get("/offers").text          # любой экран без #machine
    assert 'id="mbar"' in html
    assert "/api/jobs/live" in html            # поллер живёт в base.html — работает везде


def test_domains_has_full_machine_container(client):
    html = client.get("/domains").text
    assert 'id="machine"' in html


def test_old_progress_widget_is_gone(client):
    html = client.get("/domains").text
    assert 'id="prog"' not in html
    assert "/run/discovery/progress" not in html
```

- [ ] **Step 2: Прогнать — падает**

Run: `.venv/bin/python -m pytest backend/tests/test_panel_machine.py -q`
Expected: FAIL — `assert 'id="mbar"' in html`

- [ ] **Step 3: CSS в `base.html`**

Добавить в `<style>` (рядом с `.progress`, который удаляется вместе со своими правилами
298-320 — старую полосу больше никто не рисует):

```css
  /* ---- машина сейчас: одна карточка задачи, и на Пульте, и на M1 ---- */
  .job { background:var(--panel); border:1px solid var(--line); border-left:3px solid var(--acc);
         border-radius:var(--r); padding:14px 16px; margin-bottom:10px; }
  .job.err { border-left-color:var(--bad); }
  .job.cancelled { border-left-color:var(--line2); }
  .job-head { display:flex; align-items:center; gap:10px; flex-wrap:wrap; margin-bottom:10px; }
  .job-head .nm { font-weight:700; font-size:14px; }
  .job-head .cur { font-family:var(--mono); font-size:12.5px; color:var(--ink); }
  .job-head form { margin-left:auto; }
  .job-chips { display:flex; gap:6px; flex-wrap:wrap; margin-bottom:10px; }
  .chip-st { font-family:var(--mono); font-size:11px; padding:3px 9px; border-radius:999px;
             border:1px solid var(--line); color:var(--mut); background:var(--panel2); }
  .chip-st.done   { color:var(--ok); background:var(--ok-soft); border-color:#bfe6cd; }
  .chip-st.active { color:#fff; background:var(--acc); border-color:var(--acc); }
  .chip-st.fail   { color:var(--bad); background:var(--bad-soft); border-color:#f2c4c4; }
  .chip-st.skip   { opacity:.45; }
  .job-bar { display:flex; align-items:center; gap:14px; }
  .job-track { flex:1; height:10px; border-radius:999px; background:var(--panel2); overflow:hidden; }
  .job-fill { height:100%; width:0; border-radius:999px; background:var(--acc); transition:width .3s; }
  .job.err .job-fill { background:var(--bad); }
  .job-num { font-family:var(--mono); font-size:12.5px; color:var(--mut); white-space:nowrap; }
  .job-num b { font-size:16px; color:var(--acc); }
  .job.err .job-num b { color:var(--bad); }
  /* тонкая полоса в шапке — на экранах без #machine */
  .mbar { display:none; align-items:center; gap:12px; margin:0 0 14px; padding:8px 14px;
          border:1px solid var(--line); border-left:3px solid var(--acc);
          border-radius:var(--r); background:var(--panel); font-size:12.5px; }
  .mbar.show { display:flex; }
  .mbar .job-track { height:6px; }
```

- [ ] **Step 4: Контейнер + поллер в `base.html`**

Сразу после блока flash (`base.html:374`), перед `{% block content %}`:

```html
<div class="mbar" id="mbar"></div>
<script>
// Один поллер на всю панель. Терминальный контракт — см. services/jobs.py:
// status running -> идёт; failed -> упал (error + stage, где встал); cancelled -> остановлен
// человеком; done -> успех, ДАЖЕ при done==total==0.
// Данные приходят из фида (имена доменов) — только textContent, никогда innerHTML.
const JOB_RU = {discovery:'Поиск дропов', score:'Запуск проверки',
                recheck:'Перепроверка занятости', sweep:'Свип автопилота'};
function jobCard(j){
  const el = document.createElement('div');
  el.className = 'job' + (j.status==='failed' ? ' err' : j.status==='cancelled' ? ' cancelled' : '');
  const head = document.createElement('div'); head.className = 'job-head';
  const nm = document.createElement('span'); nm.className = 'nm';
  nm.textContent = JOB_RU[j.name] || j.name; head.appendChild(nm);
  const badge = document.createElement('span');
  badge.className = 'badge ' + (j.status==='failed' ? 'b-rejected' : 'b-scored');
  badge.textContent = j.status==='failed' ? 'ОШИБКА' : j.status==='cancelled' ? 'ОСТАНОВЛЕНА' : 'ИДЁТ';
  head.appendChild(badge);
  if (j.trigger === 'auto'){
    const a = document.createElement('span'); a.className = 'badge b-default';
    a.textContent = '✈ автопилот'; head.appendChild(a);
  }
  const cur = document.createElement('span'); cur.className = 'cur';
  cur.textContent = j.status==='failed' ? (j.error || '') : (j.current ? 'сейчас: ' + j.current : '');
  head.appendChild(cur);
  if (j.status === 'running'){                       // стоп — только у живой задачи
    const f = document.createElement('form');
    f.method = 'post'; f.action = '/run/' + j.name + '/cancel';
    const b = document.createElement('button');
    b.className = 'btn-sm btn-bad'; b.textContent = '✕ Отменить';
    f.appendChild(b); head.appendChild(f);
  }
  el.appendChild(head);
  if ((j.stages || []).length){
    const chips = document.createElement('div'); chips.className = 'job-chips';
    for (const s of j.stages){
      const c = document.createElement('span');
      const failedHere = j.status === 'failed' && s.key === j.stage;
      c.className = 'chip-st ' + (failedHere ? 'fail' : s.state);
      c.textContent = (s.state==='done' ? '✓ ' : failedHere ? '✕ ' : s.state==='active' ? '● ' : '○ ') + s.label;
      chips.appendChild(c);
    }
    el.appendChild(chips);
  }
  const bar = document.createElement('div'); bar.className = 'job-bar';
  const track = document.createElement('div'); track.className = 'job-track';
  const fill = document.createElement('div'); fill.className = 'job-fill';
  const pct = j.total > 0 ? Math.round(j.done / j.total * 100) : 0;
  fill.style.width = (j.status==='running' && j.total===0 ? 35 : pct) + '%';
  track.appendChild(fill); bar.appendChild(track);
  const num = document.createElement('div'); num.className = 'job-num';
  const b = document.createElement('b');
  b.textContent = j.status==='failed' ? 'встала' : j.status==='cancelled' ? 'остановлена' : pct + '%';
  num.appendChild(b);
  num.appendChild(document.createTextNode(j.total ? ' ' + j.done + ' / ' + j.total : ''));
  bar.appendChild(num); el.appendChild(bar);
  return el;
}
function renderMachine(data){
  const full = document.getElementById('machine'), bar = document.getElementById('mbar');
  const jobs = data.jobs || [];
  if (full){
    const idle = document.getElementById('machine-idle');
    full.replaceChildren(...jobs.map(jobCard));
    if (idle) idle.style.display = jobs.length ? 'none' : '';
    if (bar) bar.className = 'mbar';
    return;
  }
  if (!bar) return;
  if (!jobs.length){ bar.className = 'mbar'; bar.replaceChildren(); return; }
  bar.className = 'mbar show';
  const j = jobs[0];
  const nm = document.createElement('b'); nm.textContent = JOB_RU[j.name] || j.name;
  const track = document.createElement('div'); track.className = 'job-track';
  const fill = document.createElement('div'); fill.className = 'job-fill';
  fill.style.width = (j.total > 0 ? Math.round(j.done / j.total * 100) : 35) + '%';
  track.appendChild(fill);
  const num = document.createElement('span'); num.className = 'job-num';
  num.textContent = j.total ? j.done + ' / ' + j.total : '…';
  const a = document.createElement('a'); a.href = '/'; a.className = 'hint';
  a.textContent = 'на Пульт →';
  bar.replaceChildren(nm, track, num, a);
}
let machineTimer = null, machineWasBusy = false;
function pollMachine(){
  fetch('/api/jobs/live').then(r => r.json()).then(d => {
    const busy = (d.jobs || []).length > 0;
    renderMachine(d);
    // задача только что закончилась -> перечитать страницу: таблицы/счётчики устарели
    if (machineWasBusy && !busy){ location.reload(); return; }
    machineWasBusy = busy;
    machineTimer = setTimeout(pollMachine, busy ? 1500 : 5000);
  }).catch(() => { machineTimer = setTimeout(pollMachine, 3000); });
}
pollMachine();
</script>
```

- [ ] **Step 5: Снять старую полосу с `domains.html`**

Удалить из `backend/app/templates/domains.html` строки 81-140 целиком (`<div id="prog">` +
`<script>` с `poll()`): их работу забрал компонент из `base.html`. Вместо них — контейнер:

```html
<div id="machine"></div>
```

- [ ] **Step 6: Прогнать тесты + глаза**

Run: `.venv/bin/python -m pytest backend/tests/ -q` → PASS
Run: `.venv/bin/python -m pyflakes backend/app backend/tests` → пусто

Глазами (обязательно — это визуальная задача): отрендерить `/domains` с открытым `job_run`
в трёх состояниях (running / failed / cancelled) через TestClient в статический HTML,
поднять `python -m http.server` и снять Playwright-скриншоты. Сверить с
`docs/design/new-03-карточка-состояния.png`: у упавшей карточки чип стадии, на которой
встала, помечен `✕`, полоса красная, число подписано «встала».

- [ ] **Step 7: Коммит**

```bash
git add backend/app/templates/base.html backend/app/templates/domains.html backend/tests/test_panel_machine.py
git commit -F - <<'EOF'
feat(ui): карточка задачи с чипами стадий — один компонент на всю панель (T4)

Три состояния из макета new-03: идёт (чип активной стадии залит акцентом), упала
(чип стадии, НА КОТОРОЙ встала, помечен ✕; полоса красная; «встала 18/100» вместо
голого «ошибка»), остановлена человеком.

Поллер живёт в base.html, поэтому тонкая полоса «машина работает» видна на ЛЮБОМ
экране, а полные карточки — там, где есть #machine (Пульт, M1). Старый #prog из
domains.html снесён вместе с его 404-поллингом recheck.
EOF
```

---

## Task 5: Пульт — блок «Машина сейчас»

**Files:**
- Modify: `backend/app/api/panel.py` (`dashboard()` — отдать итоги последних прогонов)
- Modify: `backend/app/templates/dashboard.html`
- Test: `backend/tests/test_panel_machine.py` (дополнить)

**Interfaces:**
- Consumes: `jobs.last(name)` (Task 1), DOM-контракт `#machine` / `#machine-idle` (Task 4).
- Produces: в контексте `dashboard.html` — `last_runs: dict[str, dict | None]` для четырёх джобов.

Макеты: `new-01-пульт-машина-занята.png`, `new-02-пульт-машина-простой.png`.

- [ ] **Step 1: Тест**

Дописать в `backend/tests/test_panel_machine.py`:

```python
def test_dashboard_shows_machine_block_and_last_runs(client):
    from app.services import jobs
    with jobs.track("recheck"):
        jobs.report("recheck", done=200, total=200,
                    message="занято уже 3 из отобранных — ушли в rejected")
    html = client.get("/").text
    assert 'id="machine"' in html and 'id="machine-idle"' in html
    assert "Машина сейчас" in html
    assert "занято уже 3 из отобранных" in html      # итог последнего прогона виден в простое
    assert "Простаивает" in html
```

- [ ] **Step 2: Прогнать — падает**

Run: `.venv/bin/python -m pytest backend/tests/test_panel_machine.py::test_dashboard_shows_machine_block_and_last_runs -q`
Expected: FAIL — `assert 'id="machine"' in html`

- [ ] **Step 3: Панель отдаёт итоги**

`backend/app/api/panel.py`, в `dashboard()` — добавить в контекст:

```python
    from app.services import jobs
    ...
        "last_runs": {name: jobs.last(name) for name in _JOBS},
```

- [ ] **Step 4: Шаблон**

`backend/app/templates/dashboard.html` — сразу после карточки автопилота, ПЕРЕД воронкой:

```html
<h2><span class="idx">◐</span> Машина сейчас
  <span class="hint">что крутится прямо сейчас — в реальном времени</span></h2>

{# карточки активных задач рисует поллер из base.html; пока пусто — показываем итоги прогонов #}
<div id="machine"></div>

<div class="card" id="machine-idle">
  <div class="row" style="border-bottom:1px solid var(--line); padding-bottom:8px; margin-bottom:8px">
    <span class="badge b-default">Простаивает</span>
    <span class="hint">задач в работе нет — ниже последние прогоны</span>
  </div>
  {% for key, ru in [('discovery','Поиск дропов'), ('score','Проверка'),
                     ('recheck','Перепроверка занятости'), ('sweep','Свип автопилота')] %}
    {% set r = last_runs.get(key) %}
    <div class="row">
      <b style="min-width:190px">{{ ru }}</b>
      {% if r %}
        <span class="hint" style="flex:1">
          {% if r.status == 'failed' %}<span style="color:var(--bad)">упала: {{ r.error }}</span>
          {% elif r.status == 'cancelled' %}остановлена на {{ r.done }} / {{ r.total }}
          {% else %}{{ r.message or 'готово' }}{% endif %}
        </span>
        <span class="hint num">{{ r.finished_at.strftime('%d.%m %H:%M') if r.finished_at else '—' }}</span>
      {% else %}
        <span class="hint" style="flex:1">ни разу не запускалась</span>
      {% endif %}
    </div>
  {% endfor %}
  <div class="row" style="margin-top:10px">
    <form method="post" action="/run/discovery" class="inline">
      <button class="btn btn-acc btn-sm" title="собрать свежие дропы из включённых источников">↻ Найти дропы</button>
    </form>
    <form method="post" action="/run/score" class="inline">
      <input type="hidden" name="n" value="5">
      <button class="btn btn-sm" title="прогнать 5 доменов через воронку скоринга">▶ Запустить проверку</button>
    </form>
  </div>
</div>
```

- [ ] **Step 5: Прогнать + глаза**

Run: `.venv/bin/python -m pytest backend/tests/ -q` → PASS
Скриншот `/` в двух состояниях (две активные задачи / простой), сверить с
`new-01-пульт-машина-занята.png` и `new-02-пульт-машина-простой.png`.

- [ ] **Step 6: Коммит**

```bash
git add backend/app/api/panel.py backend/app/templates/dashboard.html backend/tests/test_panel_machine.py
git commit -F - <<'EOF'
feat(ui): Пульт — блок «Машина сейчас» (T5)

Впервые видно работу воркера: свип автопилота рисуется теми же карточками, что и
ручные запуски, с чипом «✈ автопилот». В простое — итоги последних прогонов
человеческим языком («занято уже 3 из отобранных — ушли в rejected») и кнопки
быстрого запуска прямо с Пульта.
EOF
```

---

## Task 6: M1 — инбокс решений, пакетное одобрение, полный пул

**Files:**
- Modify: `backend/app/api/panel.py` (`domains_view`, новые `/domains/pool`, `/domains/bulk-approve`, `/domains/bulk-preview`)
- Rewrite: `backend/app/templates/domains.html`
- Create: `backend/app/templates/pool.html`
- Test: `backend/tests/test_inbox.py` (новый)

**Interfaces:**
- Consumes: `scoring.blind_reason` (Task 2), `#machine` (Task 4).
- Produces:
  - `GET /domains` — инбокс (`scored`, сортировка по `acquire_deadline` asc nulls last, затем `score` desc) + «готовы к выкупу» (`approved`)
  - `GET /domains/pool` — полный реестр (нынешняя таблица целиком)
  - `POST /domains/bulk-approve` (`min_score: float = Form(0.8)`)
  - `GET /domains/bulk-preview?min_score=` → `{"n": int, "blind": int}`

Макеты: `new-04-домены-инбокс.png`, `new-05-домены-инбокс-пусто.png`, `02-домены-m1.png`.

- [ ] **Step 1: Тесты инбокса**

Создать `backend/tests/test_inbox.py`:

```python
"""Инбокс M1: срочность важнее красоты, вслепую не штампуем, пакет обходит только чистых."""
from datetime import datetime, timedelta, timezone

from app.db import SessionLocal
from app.models.domain import Domain


def _add(**kw) -> None:
    with SessionLocal() as db:
        db.add(Domain(source="backorder", **kw))
        db.commit()


def test_inbox_sorts_by_drop_deadline_not_score(client):
    """Домен с дропом завтра стоит выше красивого с дропом через месяц — иначе его теряют."""
    soon = datetime.now(timezone.utc) + timedelta(days=2)
    later = datetime.now(timezone.utc) + timedelta(days=30)
    _add(domain="urgent.ru", status="scored", score=0.60, acquire_deadline=soon)
    _add(domain="pretty.ru", status="scored", score=0.95, acquire_deadline=later)
    html = client.get("/domains").text
    assert html.index("urgent.ru") < html.index("pretty.ru")


def test_blind_domain_is_flagged_in_inbox(client):
    _add(domain="blind.ru", status="scored", score=0.9,
         score_breakdown={"errors": ["wayback:ConnectError"]})
    html = client.get("/domains").text
    assert "история НЕ проверена" in html
    assert "/domains/1/score" in html          # кнопка «перепроверить» рядом


def test_bulk_approve_skips_blind_domains(client):
    """Пакет — решение человека, но НЕ обход гейта: непроверенное в него не попадает."""
    _add(domain="clean.ru", status="scored", score=0.9, score_breakdown={"errors": []})
    _add(domain="blind.ru", status="scored", score=0.9,
         score_breakdown={"errors": ["wayback:ConnectError"]})
    _add(domain="weak.ru", status="scored", score=0.5, score_breakdown={"errors": []})
    r = client.post("/domains/bulk-approve", data={"min_score": 0.8}, follow_redirects=False)
    assert r.status_code == 303
    with SessionLocal() as db:
        st = {d.domain: d.status for d in db.query(Domain).all()}
    assert st == {"clean.ru": "approved", "blind.ru": "scored", "weak.ru": "scored"}


def test_bulk_preview_counts(client):
    _add(domain="clean.ru", status="scored", score=0.9, score_breakdown={"errors": []})
    _add(domain="blind.ru", status="scored", score=0.9,
         score_breakdown={"errors": ["wayback:ConnectError"]})
    body = client.get("/domains/bulk-preview?min_score=0.8").json()
    assert body == {"n": 1, "blind": 1}


def test_empty_inbox_explains_next_step(client):
    html = client.get("/domains").text
    assert "Решать нечего" in html


def test_pool_holds_full_registry(client):
    _add(domain="raw.ru", status="discovered")
    assert "raw.ru" in client.get("/domains/pool").text
    assert "raw.ru" not in client.get("/domains").text   # сырьё в инбокс не лезет
```

- [ ] **Step 2: Прогнать — падает**

Run: `.venv/bin/python -m pytest backend/tests/test_inbox.py -q`
Expected: FAIL — `/domains/pool` отдаёт 404.

- [ ] **Step 3: Роуты панели**

`backend/app/api/panel.py` — заменить `domains_view` и добавить три роута:

```python
_URGENT_DAYS = 3        # «дроп на носу» — столько же, сколько DROP_GRACE в scoring, с запасом


@router.get("/domains", response_class=HTMLResponse)
def domains_view(request: Request, db: Session = Depends(get_session)):
    """Инбокс решений: только то, где ждут ТЕБЯ. Полный реестр — /domains/pool."""
    from datetime import datetime, timedelta, timezone
    from app.services.scoring import blind_reason, stale_donors

    # срочность важнее score: домен, дропающийся завтра, теряется, пока мы любуемся красивым
    order = (Domain.acquire_deadline.asc().nulls_last(), Domain.score.desc().nulls_last())
    inbox = db.execute(select(Domain).where(Domain.status == "scored").order_by(*order)).scalars().all()
    ready = db.execute(select(Domain).where(Domain.status == "approved").order_by(*order)).scalars().all()
    counts = _domain_counts(db)
    soon = datetime.now(timezone.utc) + timedelta(days=_URGENT_DAYS)
    urgent = sum(1 for d in inbox + ready
                 if d.acquire_deadline is not None and d.acquire_deadline <= soon)
    reasons = dict(db.execute(
        select(Domain.reject_reason, func.count()).where(Domain.status == "rejected")
        .group_by(Domain.reject_reason)).all())
    # «отсеял ПОРОГ» и «объективная грязь» — разные природы отказа: первое крутится на
    # /settings, второе не крутится ничем. Считаем здесь, а не в Jinja.
    thr = sum(n for code, n in reasons.items() if code in ("low_rd", "too_young", "low_score"))
    return templates.TemplateResponse(request, "domains.html", {
        "active": "domains",
        "inbox": [(d, blind_reason(d)) for d in inbox],
        "ready": ready,
        "counts": counts, "total": sum(counts.values()),
        "gates": _gates(db),
        "offers_active": db.scalar(select(func.count()).select_from(Offer)
                                   .where(Offer.active.is_(True))) or 0,
        "urgent": urgent, "urgent_days": _URGENT_DAYS,
        "stale": stale_donors(db=db),
        "reasons": reasons, "reasons_total": sum(reasons.values()), "reasons_thr": thr,
        "site_by_domain": dict(db.execute(select(Site.domain_id, Site.id)).all()),
    })


@router.get("/domains/pool", response_class=HTMLResponse)
def domains_pool_view(request: Request, status: str | None = None, min_score: float | None = None,
                      limit: int = 200, show_all: bool = False, db: Session = Depends(get_session)):
    """Полный реестр — для расследований, а не для ежедневной работы."""
    limit = max(1, min(limit, 1000))            # серверный кап: не тянуть всю таблицу в память
    stmt = select(Domain)
    if status:
        stmt = stmt.where(Domain.status == status)
    elif not show_all:                          # по умолчанию только приобретаемые
        stmt = stmt.where(or_(Domain.reject_reason.is_(None),
                              Domain.reject_reason != "not_acquirable"))
    if min_score is not None:
        stmt = stmt.where(Domain.score >= min_score)
    rows = db.execute(stmt.order_by(Domain.score.desc().nulls_last(),
                                    Domain.referring_domains.desc().nulls_last())
                      .limit(limit)).scalars().all()
    counts = _domain_counts(db)
    return templates.TemplateResponse(request, "pool.html", {
        "active": "domains", "rows": rows, "counts": counts, "total": sum(counts.values()),
        "site_by_domain": dict(db.execute(select(Site.domain_id, Site.id)).all()),
        "f_status": status or "", "f_min_score": "" if min_score is None else min_score,
        "f_limit": limit, "show_all": show_all,
    })


def _bulk_candidates(db: Session, min_score: float):
    """(чистые к одобрению, сколько отсеяно как «вслепую»)."""
    from app.services.scoring import blind_reason
    rows = db.execute(select(Domain).where(Domain.status == "scored",
                                           Domain.score >= min_score)).scalars().all()
    clean = [d for d in rows if not blind_reason(d)]
    return clean, len(rows) - len(clean)


@router.get("/domains/bulk-preview")
def bulk_preview(min_score: float = 0.8, db: Session = Depends(get_session)):
    from fastapi.responses import JSONResponse
    clean, blind = _bulk_candidates(db, max(0.0, min(1.0, min_score)))
    return JSONResponse({"n": len(clean), "blind": blind})


@router.post("/domains/bulk-approve")
def bulk_approve_action(min_score: float = Form(0.8), db: Session = Depends(get_session)):
    """Пакетное одобрение — это КЛИК ЧЕЛОВЕКА, гейт курации на месте (деньги не тратятся:
    approved != куплен). Домены, оценённые вслепую (Wayback лежал), в пакет НЕ попадают —
    иначе пакет стал бы обходом того самого гейта, ради которого он существует."""
    clean, blind = _bulk_candidates(db, max(0.0, min(1.0, min_score)))
    for d in clean:
        d.status = "approved"
    db.commit()
    msg = f"Одобрено пакетом: {len(clean)}"
    if blind:
        msg += f" · пропущено «вслепую»: {blind} — их реши руками"
    return _back("/domains", msg=msg)
```

- [ ] **Step 4: `pool.html` — перенести нынешнюю таблицу**

Создать `backend/app/templates/pool.html`: перенести из старого `domains.html` целиком блоки
«Легенда» (строки 142-174), «чипы фильтров» (176-197) и таблицу (199-284), заменив в чипах и
форме фильтра `action="/domains"` на `action="/domains/pool"` и ссылки `href="/domains?..."`
на `href="/domains/pool?..."`. Шапка:

```html
{% extends "base.html" %}
{% block title %}Пул доменов{% endblock %}
{% block content %}
<h2><span class="idx">02</span> Полный реестр доменов
  <span class="hint">все домены во всех статусах — для расследований;
    ежедневная работа — <a href="/domains">инбокс M1</a></span></h2>
```

- [ ] **Step 5: `domains.html` — переписать под инбокс**

Заменить содержимое `backend/app/templates/domains.html` (макет `new-04-домены-инбокс.png`):

```html
{% extends "base.html" %}
{% block title %}Домены · M1{% endblock %}
{% block content %}

<h2><span class="idx">02</span> Домены · M1
  <span class="hint">поиск дропов → проверка → твоё решение → покупка руками</span></h2>

{# ---- воронка: клик по «отклонено» открывает разбор причин (Task 7) ---- #}
<div class="funnel" style="margin-bottom:14px">
  <div class="fgroup"><div class="fg-h">M1 · Добыча</div>
    <div class="fg-cells">
      <a class="fcell" href="/offers" title="офферы — вход машины"><div class="v">{{ offers_active }}</div><div class="k">офферы</div></a>
      <a class="fcell" href="/domains/pool?status=discovered" title="сырьё из фида, ещё не оценено"><div class="v">{{ counts.get('discovered', 0) }}</div><div class="k">найдено</div></a>
      <span class="fcell {{ 'gate' if inbox }}" title="ждут твоего решения — список ниже"><div class="v">{{ inbox|length }}</div><div class="k">на решении</div></span>
      <span class="fcell" title="одобрены — можно ставить в очередь выкупа"><div class="v">{{ ready|length }}</div><div class="k">одобрено</div></span>
      <a class="fcell" href="/domains/pool?status=purchased" title="куплены — дальше создать сайт"><div class="v">{{ counts.get('purchased', 0) }}</div><div class="k">куплено</div></a>
    </div></div>
</div>

{# ---- действия: три станции схлопнуты в одну карточку (макет 02) ---- #}
<div class="card" style="margin-bottom:14px">
  <div class="row">
    <form class="inline" method="post" action="/run/discovery">
      <button class="btn btn-acc" title="забрать свежие дропы из включённых источников">↻ Найти дропы</button></form>
    <form class="inline" method="post" action="/admin/refresh-prices">
      <button class="btn btn-sm" title="перечитать базовую цену бэкордера">цены</button></form>
    <span class="sep"></span>
    <form class="inline" method="post" action="/run/score" style="display:flex; gap:8px; align-items:center">
      <label class="f">проверить <input type="number" name="n" value="5" min="1" max="50" style="width:64px"></label>
      <button class="btn btn-acc" title="прогнать N доменов через воронку скоринга">▶ Запустить</button>
    </form>
    <form class="inline" method="post" action="/run/score">
      <input type="hidden" name="n" value="100000">
      <button class="btn" title="весь пул discovered; бюджет — max_whois_per_run на /settings">весь пул</button></form>
    <span class="sep"></span>
    <form class="inline" method="post" action="/run/recheck">
      <input type="hidden" name="n" value="200">
      <button class="btn" title="whois по донорам: занятые уходят в rejected">⟳ Перепроверить занятость</button></form>
    {% if stale %}<span class="hint" style="color:var(--acc2)">{{ stale }} не сверялись 3+ дня</span>{% endif %}
    <a class="hint" href="/domains/pool" style="margin-left:auto">полный реестр — все домены →</a>
  </div>
  <details class="what"><summary>что делают эти три действия</summary>
    <div class="what-body">
      <b>Поиск дропов</b> забирает освобождающиеся домены из включённых источников
      (см. <a href="/settings">/settings</a>), дедуплицирует и складывает как <b>найдено</b>.
      Ничего не оценивает и не покупает.<br>
      <b>Проверка</b> гоняет их по воронке дёшево→дорого: RD из фида → whois-возраст →
      РКН/блэклист → эхо в индексе → история Wayback → Ahrefs. Чистые и сильные — сразу
      <b>одобрено</b>, спорные попадают <b>к тебе на решение</b>, грязные — <b>отклонено</b>.<br>
      <b>Перепроверка занятости</b> сверяет whois'ом уже отобранных доноров: список протухает —
      домен, одобренный неделю назад, мог зарегистрировать кто-то другой. Денег не тратит.
    </div></details>
</div>

{# карточки идущих задач рисует поллер из base.html #}
<div id="machine"></div>

{# ---- ИНБОКС ---- #}
<h2><span class="idx">◉</span> Ждёт твоего решения
  <span class="hint">{{ inbox|length }} доменов</span>
  {% if urgent %}<span class="badge b-scored" title="дедлайн дропа на носу — решай сегодня">⌛ через {{ urgent_days }} дня дроп: {{ urgent }}</span>{% endif %}
  <span class="hint" style="margin-left:auto">сортировка по срочности дропа, а не по score</span>
</h2>

{% if inbox %}
<div class="card" style="margin-bottom:12px">
  <form method="post" action="/domains/bulk-approve" style="display:flex; gap:12px; align-items:center; flex-wrap:wrap">
    <label class="f">Одобрить все со score ≥
      <input type="number" id="bulk-score" name="min_score" value="0.80" step="0.05" min="0" max="1" style="width:80px"></label>
    <button class="btn btn-acc" title="перевести подходящие домены в approved — это твоё решение, деньги не тратятся">✓ Одобрить пакет</button>
    <span class="hint">попадёт <b id="bulk-n">…</b> доменов</span>
    <span class="hint" style="color:var(--acc2); margin-left:auto">⚠ помеченные «вслепую» в пакет не попадают</span>
  </form>
</div>
<script>
// живой счётчик «сколько попадёт» — иначе пакетное решение принимается вслепую
const bs = document.getElementById('bulk-score');
function bulkPreview(){
  fetch('/domains/bulk-preview?min_score=' + encodeURIComponent(bs.value))
    .then(r => r.json())
    .then(d => { document.getElementById('bulk-n').textContent = d.n; });
}
bs.addEventListener('input', bulkPreview); bulkPreview();
</script>

<div class="wrap">
<table>
  <tbody>
  {% for d, blind in inbox %}
    <tr class="{{ 'urgent' if d.acquire_deadline }}">
      <td style="width:120px">
        {% if d.acquire_deadline %}
          <div class="hint" style="font-size:10px; letter-spacing:.08em">СРОК ДРОПА</div>
          <div style="font-weight:700; color:var(--acc2)">{{ d.acquire_deadline.strftime('%d.%m') }}</div>
        {% else %}<span class="hint">— без дедлайна</span>{% endif %}
      </td>
      <td class="dom">
        <span class="src-badge {{ 'src-bid' if d.lane=='bid' else 'src-free' }}"
              title="источник: {{ d.source or '—' }}">{{ {'backorder':'bo','cctld':'cc','reg_ru':'rg','sweb':'sw'}.get(d.source, '?') }}</span>{{ d.domain }}
        <a href="https://web.archive.org/web/*/{{ d.domain }}" target="_blank" rel="noopener"
           title="посмотреть историю домена глазами" style="opacity:.6">⌛</a>
        <div class="hint">
          score <b>{{ '%.2f'|format(d.score|float) if d.score is not none else '—' }}</b> ·
          RD <b>{{ d.referring_domains if d.referring_domains is not none else '—' }}</b> ·
          {{ '%.0f'|format(d.age_years|float) if d.age_years is not none else '—' }} лет
        </div>
        {% if blind %}
          <div class="blind" title="машина не смогла проверить этот сигнал — одобрять вслепую нельзя">
            ⚠ {{ blind }}
            <form class="inline" method="post" action="/domains/{{ d.id }}/score">
              <button class="btn-sm" title="прогнать проверки для этого домена заново">▶ перепроверить</button></form>
          </div>
        {% else %}<div class="hint" style="color:var(--ok)">история чистая</div>{% endif %}
      </td>
      <td style="width:220px; text-align:right">
        <form class="inline" method="post" action="/domains/{{ d.id }}/set-status">
          <input type="hidden" name="status" value="approved">
          <button class="btn-sm btn-ok" title="домен годен — пойдёт в очередь выкупа">✓ одобрить</button></form>
        <form class="inline" method="post" action="/domains/{{ d.id }}/set-status">
          <input type="hidden" name="status" value="rejected">
          <button class="btn-sm btn-bad" title="домен не годится — убрать из воронки">✗ отклонить</button></form>
      </td>
    </tr>
  {% endfor %}
  </tbody>
</table>
</div>
{% else %}
<div class="card empty" style="text-align:center">
  <div style="font-size:15px; font-weight:700; margin-bottom:6px">Решать нечего</div>
  <div class="hint" style="margin-bottom:14px">Все оценённые домены разобраны. Запусти проверку —
    воронка отскорит свежие дропы, и спорные вернутся сюда на твоё решение.</div>
  <form class="inline" method="post" action="/run/score">
    <input type="hidden" name="n" value="5">
    <button class="btn btn-acc">▶ Запустить проверку</button></form>
  <form class="inline" method="post" action="/run/discovery">
    <button class="btn">↻ Найти дропы</button></form>
</div>
{% endif %}

{# ---- ГОТОВЫ К ВЫКУПУ ---- #}
<h2><span class="idx">→</span> Готовы к выкупу <span class="hint">{{ ready|length }} · мост в M2</span></h2>
{% if ready %}
<div class="wrap">
<table>
  <thead><tr><th>домен</th><th class="num">цена</th><th class="num">дроп</th><th>следующий шаг</th></tr></thead>
  <tbody>
  {% for d in ready %}
    <tr>
      <td class="dom">{{ d.domain }} <span class="hint">score {{ '%.2f'|format(d.score|float) if d.score is not none else '—' }}</span></td>
      <td class="num">{{ '%.0f'|format(d.acquire_price|float) if d.acquire_price is not none else '—' }}</td>
      <td class="num">{{ d.acquire_deadline.strftime('%d.%m') if d.acquire_deadline else '—' }}</td>
      <td>
        <form class="inline" method="post" action="/domains/{{ d.id }}/queue">
          <button class="btn-sm btn-acc" title="заявка → подтверждение человеком → отправка провайдеру">＋ в очередь выкупа</button></form>
        <form class="inline" method="post" action="/domains/{{ d.id }}/set-status">
          <input type="hidden" name="status" value="purchased">
          <button class="btn-sm btn-buy" title="я уже купил домен руками — система деньги не тратит">🛒 купил руками</button></form>
      </td>
    </tr>
  {% endfor %}
  </tbody>
</table>
</div>
{% else %}
<div class="card empty">Одобренных доменов нет — они появятся здесь после твоего решения в инбоксе.</div>
{% endif %}

{% endblock %}
```

- [ ] **Step 6: CSS для новых классов — в `base.html`**

```css
  .row { display:flex; align-items:center; gap:10px; flex-wrap:wrap; }
  .sep { width:1px; height:22px; background:var(--line); }
  tr.urgent td:first-child { border-left:3px solid var(--acc2); }
  .blind { margin-top:5px; font-size:12px; color:var(--warn); background:var(--warn-soft);
           border:1px solid var(--warn); border-radius:var(--r-sm); padding:4px 9px;
           display:inline-flex; align-items:center; gap:8px; }
```

(`.row` может уже существовать — проверить `grep -n "^\s*\.row" base.html` и не дублировать.)

- [ ] **Step 7: Прогнать + глаза**

Run: `.venv/bin/python -m pytest backend/tests/ -q` → PASS
Run: `.venv/bin/python -m pyflakes backend/app backend/tests` → пусто
Скриншоты `/domains` (полный инбокс, пустой инбокс) и `/domains/pool`; сверить с
`new-04-домены-инбокс.png` и `new-05-домены-инбокс-пусто.png`.

- [ ] **Step 8: Коммит**

```bash
git add backend/app/api/panel.py backend/app/templates/ backend/tests/test_inbox.py
git commit -F - <<'EOF'
feat(m1): экран стал инбоксом решений, полный реестр уехал в /domains/pool (T6)

Было: 5827 строк по score, из них 5605 сырья — 18 доменов, ждущих решения, тонули
в дампе. Стало: инбокс, отсортированный по СРОЧНОСТИ ДРОПА (домен, дропающийся
завтра, теряется, пока любуешься красивым с дропом через месяц).

Домен, оценённый при лежащем Wayback, помечен «история НЕ проверена» и в пакетное
одобрение НЕ попадает — иначе пакет стал бы обходом гейта курации, ради которого
он и существует. Гейт остаётся человеческим: approved != куплен, деньги не тратятся.
EOF
```

---

## Task 7: Модалка «разбор причин отказа»

**Files:**
- Modify: `backend/app/templates/domains.html` (плитка «отклонено» + `<dialog>`)
- Modify: `backend/app/templates/base.html` (CSS модалки и полос)
- Test: `backend/tests/test_inbox.py` (дополнить)

**Interfaces:**
- Consumes: `reasons: dict[str, int]`, `reasons_total: int` из `domains_view` (Task 6).

Макет: `new-06-причины-отказа.png`. Ключевое — легенда делит отсев на «режет порог (можно
ослабить)» и «грязь/РКН (не трогать)»: это превращает статистику в решение по порогам.

- [ ] **Step 1: Тест**

Дописать в `backend/tests/test_inbox.py`:

```python
def test_reject_reasons_split_threshold_from_dirt(client):
    """Разбор обязан различать «отсеял мой порог» и «объективная грязь» — иначе непонятно,
    что вообще можно крутить на /settings."""
    _add(domain="a.ru", status="rejected", reject_reason="low_rd")
    _add(domain="b.ru", status="rejected", reject_reason="low_rd")
    _add(domain="c.ru", status="rejected", reject_reason="history_dirty")
    html = client.get("/domains").text
    assert "Мало доноров" in html and "Грязная история" in html
    assert "режет порог" in html and "не трогать" in html
    assert "настроить пороги" in html
```

- [ ] **Step 2: Прогнать — падает**

Run: `.venv/bin/python -m pytest backend/tests/test_inbox.py::test_reject_reasons_split_threshold_from_dirt -q`
Expected: FAIL — `assert "Мало доноров" in html`

- [ ] **Step 3: Плитка + модалка в `domains.html`**

В блок воронки (Task 6, Step 5) добавить шестую плитку, открывающую `<dialog>`:

```html
      <button class="fcell" onclick="document.getElementById('why').showModal()"
              title="разобрать, почему домены не прошли воронку">
        <div class="v" style="color:var(--bad)">{{ counts.get('rejected', 0) }}</div>
        <div class="k">отклонено</div></button>
```

В конец `{% block content %}` — сама модалка (`<dialog>` — нативный, зависимостей не тянет):

```html
{% set RU = {'low_rd': ('Мало доноров', 'thr'), 'too_young': ('Молодой домен', 'thr'),
             'low_score': ('Низкий скор', 'thr'), 'history_dirty': ('Грязная история', 'dirt'),
             'rkn': ('РКН', 'dirt'), 'blacklist': ('Блэклист', 'dirt'),
             'feed_flag': ('Флаг источника', 'dirt'),
             'not_acquirable': ('Занят', 'taken')} %}
<dialog id="why" class="modal">
  <form method="dialog" style="display:flex; align-items:start">
    <div style="flex:1">
      <h3 style="margin:0">Отклонено — разбор причин</h3>
      <div class="hint">почему домены не прошли воронку · всего
        <b style="color:var(--bad)">{{ reasons_total }}</b></div>
    </div>
    <button class="btn-sm" title="закрыть">×</button>
  </form>
  <div class="why-grid">
    {% for code, n in reasons.items()|sort(attribute='1', reverse=true) %}
      {% set ru, kind = RU.get(code, (code, 'taken')) %}
      <div class="why-nm">{{ ru }} <code>{{ code or '—' }}</code></div>
      <div class="why-bar"><i class="k-{{ kind }}"
           style="width:{{ (n / reasons_total * 100)|round|int if reasons_total else 0 }}%"></i></div>
      <div class="num"><b>{{ n }}</b>
        <span class="hint">{{ (n / reasons_total * 100)|round|int if reasons_total else 0 }}%</span></div>
    {% endfor %}
  </div>
  <div class="why-foot">
    <span><i class="sw k-thr"></i> режет порог — <b>{{ reasons_thr }}</b>
      ({{ (reasons_thr / reasons_total * 100)|round|int if reasons_total else 0 }}%): можно ослабить</span>
    <span><i class="sw k-dirt"></i> грязь/РКН — не трогать</span>
    <a class="btn btn-acc btn-sm" href="/settings" style="margin-left:auto">⚙ настроить пороги →</a>
  </div>
</dialog>
```

- [ ] **Step 4: CSS модалки в `base.html`**

```css
  /* ---- разбор причин отказа (модалка) ---- */
  .modal { border:1px solid var(--line); border-radius:var(--r); padding:20px 22px;
           min-width:min(560px, 92vw); box-shadow:0 24px 60px rgba(15,23,42,.18); }
  .modal::backdrop { background:rgba(15,23,42,.35); }
  .why-grid { display:grid; grid-template-columns:auto 1fr auto; gap:9px 14px;
              align-items:center; margin:16px 0; }
  .why-nm { font-size:13px; font-weight:600; }
  .why-nm code { font-family:var(--mono); font-size:10.5px; color:var(--dim); font-weight:400; }
  .why-bar { height:9px; border-radius:999px; background:var(--panel2); overflow:hidden; }
  .why-bar i { display:block; height:100%; border-radius:999px; }
  .k-thr  { background:var(--warn); }        /* отсеял ПОРОГ — его можно ослабить */
  .k-dirt { background:var(--bad); }         /* объективная грязь — трогать нечего */
  .k-taken{ background:var(--line2); }       /* домен занят другими */
  .why-foot { display:flex; align-items:center; gap:18px; flex-wrap:wrap; font-size:12px;
              color:var(--mut); border-top:1px solid var(--line); padding-top:13px; }
  .why-foot .sw { display:inline-block; width:9px; height:9px; border-radius:2px; margin-right:5px; }
```

- [ ] **Step 5: Прогнать + глаза**

Run: `.venv/bin/python -m pytest backend/tests/ -q` → PASS
Скриншот открытой модалки, сверить с `new-06-причины-отказа.png`.

- [ ] **Step 6: Коммит**

```bash
git add backend/app/templates/domains.html backend/app/templates/base.html backend/tests/test_inbox.py
git commit -F - <<'EOF'
feat(m1): разбор причин отказа — обратная связь по порогам (T7)

189 отклонённых молчали о том, не режет ли порог лишнего. Модалка раскладывает
отсев по reject_reason и — главное — делит его на две природы: «режет порог
(можно ослабить)» и «грязь/РКН (не трогать)». Статистика превращается в решение:
видно, сколько отсева — твой выбор на /settings, а сколько объективно.
EOF
```

---

## Task 8: Документация

**Files:**
- Modify: `docs/DESIGN.md` (новые классы и двухуровневый шильдик)
- Modify: `CLAUDE.md` (текущее состояние)
- Modify: `docs/superpowers/specs/2026-07-12-panel-m1-observability-design.md` (две поправки)

- [ ] **Step 1: Поправить спеку**

- §3.1: убрать упоминание `track("sweep", lock=False)` — параметра нет. Замок держит частичный
  уникальный индекс, а протухшие `running`-строки гасит `_reap()` (`STALE_MIN = 15`).
- §7: переписать из «добавляем» в «уже есть» — «Гейты машины» (`base.html:346-350`) и строка
  баланса backorder (`queue.html:25-28`) существовали до этой ветки; макет отрисовал их с
  живой панели.

- [ ] **Step 2: `docs/DESIGN.md`**

Добавить раздел «Компоненты» с новыми классами: `.job` / `.job-head` / `.job-chips` / `.chip-st`
(состояния `done` / `active` / `pending` / `skip` / `fail`) / `.job-bar` / `.mbar` — карточка
задачи; `.blind` — предупреждение «оценён вслепую»; `tr.urgent` — срочность дропа; `.modal` /
`.why-grid` / `.why-foot` — разбор причин. Зафиксировать поправку к шильдику: короткое пояснение
видно всегда, полный абзац — в одном `<details>` на карточку (уровней два, не три).

- [ ] **Step 3: `CLAUDE.md`**

Обновить «Текущее состояние»: реестр задач в БД (`job_run`, миграция 0007) — свип автопилота
впервые виден панели; стадии воронки в чипах; стоп-кнопка; M1 = инбокс решений с сортировкой по
срочности дропа; пометка «оценён вслепую»; пакетное одобрение (гейт курации на месте);
разбор причин отказа; полный реестр на `/domains/pool`. Дописать в «Что делать дальше»: волна 2
(карточка сайта как чек-лист, диагностика по модулям), волна 3 (WYSIWYG-редактор) — §11 спеки.

- [ ] **Step 4: Финальная проверка**

```bash
.venv/bin/python -m pytest backend/tests/ -q
.venv/bin/python -m pyflakes backend/app backend/tests
grep -rn "on_progress" backend/app/            # пусто
grep -rn "jobs.start\|/run/.*progress" backend/app/   # пусто
```

- [ ] **Step 5: Коммит**

```bash
git add docs/ CLAUDE.md
git commit -F - <<'EOF'
docs: наблюдаемость машины + M1 как инбокс — состояние, дизайн-система, поправки спеки (T8)
EOF
```

---

## Проверка плана против спеки

| Требование спеки | Задача |
|---|---|
| §3 реестр в PG, single-flight между процессами, `_reap` | T1 |
| §3.2 контракт `track`/`spawn`/`report`/`cancelled`/`live`/`last` | T1 |
| §3.3 терминальный контракт | T1 (тесты), T4 (JS держится того же) |
| §4 чипы стадий, 3 состояния карточки | T2 (данные), T4 (вид) |
| §5 Пульт «Машина сейчас» + простой + возврат на `Referer` | T3 (роуты), T5 (вид) |
| §6 действия одной строкой, инбокс, срочность, «вслепую», пакет, пустое состояние, `/domains/pool` | T6 |
| §6 блок 4 — разбор причин с делением «порог / грязь» | T7 |
| §7 гейты в сайдбаре, баланс backorder | **уже в коде** — не задача |
| §8 баннеры у запуска задач убраны | T3 |
| §1.6 стоп-кнопка | T1 (флаг), T2 (сервисы смотрят), T3 (роут), T4 (кнопка) |
| §9 дизайн-контракт, новые классы в `base.html` | T4, T6, T7, T8 |
| §10 тесты | по задаче в каждой |
