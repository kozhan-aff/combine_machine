# Спек 3 «Комбайн» — движок автономии: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Панель сама двигает конвейер по включённым «авто»-стадиям (discovery→score→queue→provision→generate→publish→check_index) до ближайшего человеческого гейта, под по-стадийными капами, с экраном «Автопилот» и кросс-процессным run-логом.

**Architecture:** Новый single-row конфиг `AutonomySettings` (тумблеры+капы, паттерн `scoring_settings`) и run-лог `AutonomyRun` (кросс-процессная видимость воркер↔панель). Новый `services/orchestrator.py::run_sweep()` — тонкий диспетчер поверх УЖЕ существующих безопасных сервисов: детерминированная таблица стадий, single-flight DB-замок, учёт в `AutonomyRun`. Никакой новой бизнес-логики. Шедулер переделан на частый тик (5 мин) + throttle из конфига, читаемого свежим каждый тик. Экран «Автопилот» в guarded-роутере панели.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.0, Jinja2, Alembic, APScheduler; тесты — офлайн SQLite-харнесс (`backend/tests/conftest.py`), pytest.

## Global Constraints

Скопировано дословно из спека §G/§F — каждая задача неявно включает эти требования:

- **Денежный гейт — ручной ВСЕГДА.** Оркестратор НИКОГДА не вызывает `acquisition.confirm_order`, `acquisition.execute_confirmed_order`, `acquisition.mark_caught`, а также money-байпас-роуты (`pipeline.py POST /domains/{id}/purchase`, `panel.py set-status=purchased`).
- **Редактурный гейт цел.** Оркестратор НИКОГДА не вызывает `content.mark_edited`. Публикация берёт только страницы `edited`.
- **Оркестратор — только оркестрация:** запрос-подходящих-сущностей + вызов существующего сервиса + учёт. Логика скоринга/провижна/контента остаётся в своих сервисах.
- **Каждая авто-стадия под капом**, кроме `discovery` (bulk-pull фида — только тумблер, капа нет).
- **Single-flight:** пересекающиеся свипы (воркер-контейнер + ручной запуск из панели) не дублируют работу — DB-замок через `AutonomyRun`, `STALE_MIN=15` мин на протухший «running».
- **Конфиг читается свежим каждый тик** — тумблеры/интервал применяются без рестарта воркера.
- **Ручной свип** (`respect_master=False`) уважает по-стадийные тумблеры, но минует мастер-выключатель.
- **Безопасность не ослаблять:** новые POST-роуты в том же guarded-роутере панели (CSRF same-origin из `main.py` + опц. Basic-auth).
- **Панель — светлая CMS, шильдик, русский UI;** только CSS-переменные из `base.html` (не хардкодить цвета). Каждый контрол подписан тем, что делает.
- **Тесты офлайн+детерминированы:** SQLite-харнесс, сетевые сервисы мокаются (monkeypatch). Прогон: `.venv/bin/python -m pytest backend/tests/ -q`; линт: `.venv/bin/python -m pyflakes backend/app backend/tests` — чисто.
- Дефолты (спек §A): `sweep_interval_min=60` кламп `[5,1440]`; капы `cap_score=20 / cap_queue=10 / cap_provision=5 / cap_generate=5 / cap_publish=5 / cap_check_index=20`, кламп `[0,500]`; все `auto_*` и `autopilot_on` дефолт `False`.

---

## File Structure

**Создаются:**
- `backend/app/models/autonomy.py` — `AutonomySettings` (single-row конфиг) + `AutonomyRun` (run-лог). Обе таблицы в одной миграции.
- `backend/app/services/autonomy.py` — `get_autonomy()` / `update_autonomy(**kw)` / `reset_autonomy()` (зеркало `services/settings.py`).
- `backend/app/services/orchestrator.py` — `run_sweep()` + single-flight-замок + таблица стадий.
- `backend/alembic/versions/0004_autonomy.py` — миграция (revision `0004`, down `0003`).
- `backend/app/templates/autopilot.html` — экран «Автопилот».
- `backend/tests/test_autonomy_config.py`, `backend/tests/test_orchestrator.py`, `backend/tests/test_scheduler.py`, `backend/tests/test_autopilot_panel.py`.

**Модифицируются:**
- `backend/app/workers/scheduler.py` — тик+throttle вместо суточного `m1_cycle`.
- `backend/app/api/panel.py` — роуты `/autopilot`, `/autopilot/settings`, `/autopilot/run`; whitelist `"sweep"`; `_gates()` helper; контекст дашборда; локализация чипов.
- `backend/app/templates/base.html` — пункт «Автопилот» в сайдбаре.
- `backend/app/templates/dashboard.html` — полоска статуса автопилота; локализация draft/edited в обзоре сайтов.
- `backend/app/templates/domains.html` — чипы статусов через `|status_ru`.
- `backend/app/models/domain.py` — уточнить коммент enum `AcquisitionOrder.status`.
- `backend/app/api/pipeline.py` — коммент-предупреждение у money-байпаса.
- `backend/tests/conftest.py` — импорт `app.models.autonomy` для `create_all`.

---

### Task 1: Конфиг автономии — модели + сервис + миграция

**Files:**
- Create: `backend/app/models/autonomy.py`
- Create: `backend/app/services/autonomy.py`
- Create: `backend/alembic/versions/0004_autonomy.py`
- Modify: `backend/tests/conftest.py:19-25` (импорт модели)
- Test: `backend/tests/test_autonomy_config.py`

**Interfaces:**
- Produces:
  - `app.models.autonomy.AutonomySettings` (таблица `autonomy_settings`, single-row id=1) с полями: `autopilot_on:bool`, `sweep_interval_min:int`, `auto_discovery/auto_score/auto_queue/auto_provision/auto_generate/auto_publish/auto_check_index:bool`, `cap_score/cap_queue/cap_provision/cap_generate/cap_publish/cap_check_index:int`, `updated_at`.
  - `app.models.autonomy.AutonomyRun` (таблица `autonomy_run`): `id`, `started_at:datetime(tz)`, `finished_at:datetime|None`, `trigger:str`, `status:str`, `counts:dict`, `errors:list`.
  - `app.services.autonomy.get_autonomy() -> dict` — все ключи выше (без `id`/`updated_at`), правильные типы.
  - `app.services.autonomy.update_autonomy(**kw) -> dict` — bool-ключи через `bool()`, int-ключи с клампом по границам, неизвестные игнор. Возвращает `get_autonomy()`.
  - `app.services.autonomy.reset_autonomy() -> dict` — сброс к дефолтам.

- [ ] **Step 1: Написать падающий тест конфига**

Создать `backend/tests/test_autonomy_config.py`:

```python
"""Конфиг автономии: дефолты (seed при первом чтении), кламп границ, bool-приведение."""
from app.services import autonomy


def test_get_autonomy_seeds_defaults():
    a = autonomy.get_autonomy()
    assert a["autopilot_on"] is False
    assert a["sweep_interval_min"] == 60
    for stage in ("discovery", "score", "queue", "provision", "generate", "publish", "check_index"):
        assert a[f"auto_{stage}"] is False
    assert a["cap_score"] == 20 and a["cap_queue"] == 10 and a["cap_provision"] == 5
    assert a["cap_generate"] == 5 and a["cap_publish"] == 5 and a["cap_check_index"] == 20
    assert "cap_discovery" not in a          # у discovery капа нет


def test_update_autonomy_clamps_and_coerces():
    a = autonomy.update_autonomy(sweep_interval_min=2, cap_score=9999, autopilot_on="on", auto_score=True)
    assert a["sweep_interval_min"] == 5       # кламп нижней границы [5,1440]
    assert a["cap_score"] == 500              # кламп верхней границы [0,500]
    assert a["autopilot_on"] is True          # "on" -> True
    assert a["auto_score"] is True


def test_update_autonomy_ignores_unknown_keys():
    autonomy.update_autonomy(bogus_key=123, cap_queue=7)
    a = autonomy.get_autonomy()
    assert "bogus_key" not in a and a["cap_queue"] == 7


def test_reset_autonomy_restores_defaults():
    autonomy.update_autonomy(autopilot_on=True, cap_score=1, auto_publish=True)
    a = autonomy.reset_autonomy()
    assert a["autopilot_on"] is False and a["cap_score"] == 20 and a["auto_publish"] is False
```

- [ ] **Step 2: Прогнать — убедиться, что падает**

Run: `.venv/bin/python -m pytest backend/tests/test_autonomy_config.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.autonomy'` (или `app.models.autonomy`).

- [ ] **Step 3: Написать модель `backend/app/models/autonomy.py`**

```python
"""Автономия: single-row конфиг тумблеров/капов + run-лог свипов (кросс-процессный).

AutonomySettings — как scoring_settings (id=1, seed дефолтами через services/autonomy).
AutonomyRun — единственный способ шедулеру (воркер-контейнер) и панели видеть одно и то
же: in-memory jobs.py живёт лишь в процессе панели. started_at ставится Python-side
(tz-aware) — не server_default: иначе SQLite вернул бы naive-строку и сломал сравнение с
now(tz) в single-flight-замке.
"""
from datetime import datetime, timezone
from sqlalchemy import Integer, String, Boolean, DateTime, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from app.db import Base


class AutonomySettings(Base):
    __tablename__ = "autonomy_settings"

    id: Mapped[int] = mapped_column(primary_key=True)                 # всегда 1
    autopilot_on: Mapped[bool] = mapped_column(Boolean, default=False)      # мастер-выключатель
    sweep_interval_min: Mapped[int] = mapped_column(Integer, default=60)    # throttle между авто-свипами

    auto_discovery: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_score: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_queue: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_provision: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_generate: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_publish: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_check_index: Mapped[bool] = mapped_column(Boolean, default=False)

    cap_score: Mapped[int] = mapped_column(Integer, default=20)
    cap_queue: Mapped[int] = mapped_column(Integer, default=10)
    cap_provision: Mapped[int] = mapped_column(Integer, default=5)
    cap_generate: Mapped[int] = mapped_column(Integer, default=5)
    cap_publish: Mapped[int] = mapped_column(Integer, default=5)
    cap_check_index: Mapped[int] = mapped_column(Integer, default=20)
    # у discovery капа НЕТ — bulk-pull фида, не по-доменная стадия

    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True),
                                                        server_default=func.now(), onupdate=func.now())


class AutonomyRun(Base):
    __tablename__ = "autonomy_run"

    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    trigger: Mapped[str] = mapped_column(String(16), default="cron")     # cron | manual
    status: Mapped[str] = mapped_column(String(16), default="running")   # running | done | failed
    counts: Mapped[dict] = mapped_column(JSONB, default=dict)            # {stage: n}
    errors: Mapped[list] = mapped_column(JSONB, default=list)           # ["stage: текст", ...]
```

- [ ] **Step 4: Написать сервис `backend/app/services/autonomy.py`**

```python
"""Рантайм-конфиг автономии: читать/писать single-row autonomy_settings (id=1).

Паттерн 1-в-1 с services/settings.py. Дефолты — константы здесь (отдельный config-модуль
не нужен, YAGNI). update_autonomy валидирует диапазоны, чтобы UI не записал мусор.
"""
_BOOL_KEYS = ("autopilot_on", "auto_discovery", "auto_score", "auto_queue",
              "auto_provision", "auto_generate", "auto_publish", "auto_check_index")
_INT_BOUNDS = {                       # (min, max) для клампа
    "sweep_interval_min": (5, 1440),
    "cap_score": (0, 500), "cap_queue": (0, 500), "cap_provision": (0, 500),
    "cap_generate": (0, 500), "cap_publish": (0, 500), "cap_check_index": (0, 500),
}
_DEFAULTS = {
    "autopilot_on": False, "sweep_interval_min": 60,
    "auto_discovery": False, "auto_score": False, "auto_queue": False,
    "auto_provision": False, "auto_generate": False, "auto_publish": False,
    "auto_check_index": False,
    "cap_score": 20, "cap_queue": 10, "cap_provision": 5,
    "cap_generate": 5, "cap_publish": 5, "cap_check_index": 20,
}


def _row(db):
    """Вернуть (создав при отсутствии) строку autonomy_settings id=1 с дефолтами."""
    from app.models.autonomy import AutonomySettings
    row = db.get(AutonomySettings, 1)
    if row is None:
        row = AutonomySettings(id=1, **_DEFAULTS)
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


def get_autonomy() -> dict:
    from app.db import SessionLocal
    with SessionLocal() as db:
        r = _row(db)
        out = {k: bool(getattr(r, k)) for k in _BOOL_KEYS}
        out["sweep_interval_min"] = int(r.sweep_interval_min)
        for k in ("cap_score", "cap_queue", "cap_provision",
                  "cap_generate", "cap_publish", "cap_check_index"):
            out[k] = int(getattr(r, k))
        return out


def update_autonomy(**kw) -> dict:
    """Записать переданные ключи: bool через bool(), int с клампом. Неизвестные игнор."""
    from app.db import SessionLocal
    with SessionLocal() as db:
        r = _row(db)
        for k in _BOOL_KEYS:
            if k in kw:
                setattr(r, k, bool(kw[k]))
        for k, (lo, hi) in _INT_BOUNDS.items():
            if k in kw and kw[k] is not None:
                setattr(r, k, max(lo, min(hi, int(kw[k]))))
        db.commit()
    return get_autonomy()


def reset_autonomy() -> dict:
    from app.db import SessionLocal
    with SessionLocal() as db:
        r = _row(db)
        for k, v in _DEFAULTS.items():
            setattr(r, k, v)
        db.commit()
    return get_autonomy()
```

- [ ] **Step 5: Зарегистрировать модель в conftest**

В `backend/tests/conftest.py` добавить импорт рядом с прочими моделями (после строки `import app.models.settings` на строке 21) и в кортеж `_REGISTER_TABLES` (строка 25):

```python
import app.models.settings
import app.models.autonomy
```

И расширить кортеж:

```python
_REGISTER_TABLES = (app.models.domain, app.models.site, app.models.offer, app.models.monitoring, app.models.settings, app.models.autonomy)
```

- [ ] **Step 6: Прогнать — тесты проходят**

Run: `.venv/bin/python -m pytest backend/tests/test_autonomy_config.py -q`
Expected: PASS (4 passed).

- [ ] **Step 7: Написать миграцию `backend/alembic/versions/0004_autonomy.py`**

```python
"""autonomy: autonomy_settings (тумблеры/капы) + autonomy_run (run-лог свипов)

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-07
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "autonomy_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("autopilot_on", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("sweep_interval_min", sa.Integer(), nullable=False, server_default="60"),
        sa.Column("auto_discovery", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("auto_score", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("auto_queue", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("auto_provision", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("auto_generate", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("auto_publish", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("auto_check_index", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("cap_score", sa.Integer(), nullable=False, server_default="20"),
        sa.Column("cap_queue", sa.Integer(), nullable=False, server_default="10"),
        sa.Column("cap_provision", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("cap_generate", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("cap_publish", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("cap_check_index", sa.Integer(), nullable=False, server_default="20"),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_table(
        "autonomy_run",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("trigger", sa.String(16)),
        sa.Column("status", sa.String(16)),
        sa.Column("counts", postgresql.JSONB()),
        sa.Column("errors", postgresql.JSONB()),
    )


def downgrade() -> None:
    op.drop_table("autonomy_run")
    op.drop_table("autonomy_settings")
```

- [ ] **Step 8: Проверить, что миграция парсится (импортируется)**

Run: `.venv/bin/python -c "import backend.alembic.versions.__init__" 2>/dev/null; .venv/bin/python - <<'PY'
import importlib.util, pathlib
p = pathlib.Path("backend/alembic/versions/0004_autonomy.py")
spec = importlib.util.spec_from_file_location("m0004", p)
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
assert m.revision == "0004" and m.down_revision == "0003"
print("0004 imports ok")
PY`
Expected: `0004 imports ok`.

- [ ] **Step 9: Полный прогон + линт**

Run: `.venv/bin/python -m pytest backend/tests/ -q && .venv/bin/python -m pyflakes backend/app backend/tests`
Expected: все тесты PASS, pyflakes без вывода.

- [ ] **Step 10: Commit**

```bash
git add backend/app/models/autonomy.py backend/app/services/autonomy.py \
        backend/alembic/versions/0004_autonomy.py backend/tests/conftest.py \
        backend/tests/test_autonomy_config.py
git commit -m "спек 3 задача 1: конфиг автономии — AutonomySettings/AutonomyRun + services/autonomy + миграция 0004

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Оркестратор — single-flight-замок + учёт прогонов

**Files:**
- Create: `backend/app/services/orchestrator.py`
- Test: `backend/tests/test_orchestrator.py`

**Interfaces:**
- Consumes: `app.models.autonomy.AutonomyRun`, `app.db.SessionLocal`.
- Produces (используется Task 3, 4):
  - `orchestrator.STALE_MIN = 15` — минут, после которых «running»-строка считается протухшей.
  - `orchestrator._acquire_lock(trigger: str) -> int | None` — атомарно вставить `AutonomyRun(status="running", trigger=…)` ТОЛЬКО если нет незавершённой строки моложе `STALE_MIN`; вернуть `id` новой строки или `None` (замок занят).
  - `orchestrator._finish_run(run_id: int, status: str, counts: dict, errors: list) -> None` — проставить `finished_at`/`status`/`counts`/`errors`.
  - `orchestrator.last_finished_sweep_at() -> datetime | None` — максимум `finished_at` завершённых прогонов (для throttle шедулера), tz-aware или `None`.

- [ ] **Step 1: Написать падающий тест замка/учёта**

Создать `backend/tests/test_orchestrator.py`:

```python
"""Оркестратор: single-flight-замок (AutonomyRun) + учёт прогонов."""
from datetime import datetime, timezone, timedelta

import app.db as db
from app.models.autonomy import AutonomyRun
from app.services import orchestrator as orch


def test_acquire_lock_inserts_running_row():
    run_id = orch._acquire_lock("cron")
    assert run_id is not None
    with db.SessionLocal() as s:
        r = s.get(AutonomyRun, run_id)
        assert r.status == "running" and r.trigger == "cron" and r.finished_at is None


def test_acquire_lock_blocked_by_fresh_running():
    first = orch._acquire_lock("cron")
    second = orch._acquire_lock("manual")           # свежий running держит замок
    assert first is not None and second is None


def test_acquire_lock_overrides_stale_running():
    with db.SessionLocal() as s:                    # протухший running (старше STALE_MIN)
        stale = AutonomyRun(status="running", trigger="cron",
                            started_at=datetime.now(timezone.utc) - timedelta(minutes=orch.STALE_MIN + 1))
        s.add(stale); s.commit()
    assert orch._acquire_lock("cron") is not None   # протухший не блокирует


def test_finish_run_records_summary():
    run_id = orch._acquire_lock("manual")
    orch._finish_run(run_id, "done", {"score": 3}, ["queue: boom"])
    with db.SessionLocal() as s:
        r = s.get(AutonomyRun, run_id)
        assert r.status == "done" and r.finished_at is not None
        assert r.counts == {"score": 3} and r.errors == ["queue: boom"]


def test_last_finished_sweep_at_returns_latest():
    assert orch.last_finished_sweep_at() is None     # пусто -> None
    rid = orch._acquire_lock("cron")
    orch._finish_run(rid, "done", {}, [])
    got = orch.last_finished_sweep_at()
    assert got is not None and got.tzinfo is not None
```

- [ ] **Step 2: Прогнать — убедиться, что падает**

Run: `.venv/bin/python -m pytest backend/tests/test_orchestrator.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.orchestrator'`.

- [ ] **Step 3: Написать замок/учёт в `backend/app/services/orchestrator.py`**

```python
"""M-оркестратор автономии. Двигает конвейер по включённым «авто»-стадиям до гейтов.

Тонкий диспетчер: НИКАКОЙ новой бизнес-логики — только (1) запрос подходящих сущностей,
(2) вызов существующего безопасного сервиса, (3) учёт. Три человеческих гейта (курация,
деньги, редактура) он НЕ трогает — см. _FORBIDDEN в докстринге run_sweep.
"""
from datetime import datetime, timezone, timedelta

STALE_MIN = 15   # «running»-строка старше этого — крашнутый воркер, замок протух


def _acquire_lock(trigger: str) -> int | None:
    """Single-flight: вставить running-строку, если нет свежей незавершённой. Вернуть id|None.

    ponytail: check-then-insert; на Postgres окно гонки ~мс — при тике раз в 5 мин и редком
    ручном свипе это неопасно. Упрётся — заменить на pg_advisory_lock (но SQLite-тесты его
    не умеют, потому не сейчас). STALE_MIN перекрывает зависший running крашнутого воркера.
    """
    from sqlalchemy import select
    from app.db import SessionLocal
    from app.models.autonomy import AutonomyRun

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=STALE_MIN)
    with SessionLocal() as db:
        fresh = db.execute(
            select(AutonomyRun.id).where(
                AutonomyRun.status == "running", AutonomyRun.started_at > cutoff)
        ).first()
        if fresh is not None:
            return None                              # замок держит свежий running
        run = AutonomyRun(status="running", trigger=trigger,
                          started_at=datetime.now(timezone.utc), counts={}, errors=[])
        db.add(run)
        db.commit()
        db.refresh(run)
        return run.id


def _finish_run(run_id: int, status: str, counts: dict, errors: list) -> None:
    from app.db import SessionLocal
    from app.models.autonomy import AutonomyRun

    with SessionLocal() as db:
        r = db.get(AutonomyRun, run_id)
        if r is None:
            return
        r.status = status
        r.finished_at = datetime.now(timezone.utc)
        r.counts = counts
        r.errors = errors
        db.commit()


def last_finished_sweep_at():
    """Максимум finished_at завершённых прогонов (для throttle шедулера) или None."""
    from sqlalchemy import select, func
    from app.db import SessionLocal
    from app.models.autonomy import AutonomyRun

    with SessionLocal() as db:
        return db.scalar(select(func.max(AutonomyRun.finished_at)))
```

- [ ] **Step 4: Прогнать — тесты проходят**

Run: `.venv/bin/python -m pytest backend/tests/test_orchestrator.py -q`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/orchestrator.py backend/tests/test_orchestrator.py
git commit -m "спек 3 задача 2: оркестратор — single-flight-замок + учёт прогонов (AutonomyRun)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Оркестратор — таблица стадий + run_sweep + гейт-инварианты

**Files:**
- Modify: `backend/app/services/orchestrator.py` (добавить стадии + `run_sweep`)
- Test: `backend/tests/test_orchestrator.py` (дописать)

**Interfaces:**
- Consumes (из Task 2): `_acquire_lock`, `_finish_run`. Существующие сервисы: `discovery.run_discovery()`, `scoring.score_pending(limit=)`, `acquisition.create_order(domain_id)`, `provisioning.create_site_for(domain_id)` / `provisioning.provision(site_id)`, `content.generate_site(site_id, use_competitor=True)`, `publish.publish_site(site_id)` / `publish.check_index(site_id)`.
- Produces (используется Task 4, 5):
  - `orchestrator.STAGES` — список кортежей `(key, flag_attr, cap_attr|None, handler)` в порядке конвейера.
  - `orchestrator.run_sweep(trigger="cron", on_progress=None, respect_master=True) -> dict` — `{"skipped": "autopilot_off"}` | `{"skipped": "already_running"}` | `{"run_id", "status", "counts", "errors"}`.

- [ ] **Step 1: Дописать падающие тесты стадий + инвариантов**

Добавить в конец `backend/tests/test_orchestrator.py`:

```python
# --- стадии + run_sweep -----------------------------------------------------
from app.models.domain import Domain, AcquisitionOrder
from app.models.site import Site, Page
from app.services import autonomy


def _enable(**stages):
    """Включить мастер + перечисленные auto_<stage>=True, остальные оставить как есть."""
    autonomy.update_autonomy(autopilot_on=True, **stages)


def test_sweep_skipped_when_autopilot_off():
    autonomy.update_autonomy(autopilot_on=False)
    assert orch.run_sweep(trigger="cron") == {"skipped": "autopilot_off"}


def test_manual_sweep_bypasses_master_but_respects_toggles():
    autonomy.update_autonomy(autopilot_on=False, auto_score=False)
    out = orch.run_sweep(trigger="manual", respect_master=False)   # мастер выкл — но ручной идёт
    assert "run_id" in out and out["counts"] == {}                 # ни одна стадия не включена


def test_queue_stage_moves_approved_to_purchasing_up_to_cap():
    with db.SessionLocal() as s:
        for i in range(3):
            s.add(Domain(domain=f"appr-{i}.ru", source="backorder", status="approved"))
        s.commit()
    autonomy.update_autonomy(cap_queue=2)
    _enable(auto_queue=True)
    out = orch.run_sweep(trigger="cron")
    assert out["counts"]["queue"] == 2                             # ровно до капа
    with db.SessionLocal() as s:
        from sqlalchemy import select, func
        purchasing = s.scalar(select(func.count()).select_from(Domain).where(Domain.status == "purchasing"))
        approved = s.scalar(select(func.count()).select_from(Domain).where(Domain.status == "approved"))
        orders = s.scalar(select(func.count()).select_from(AcquisitionOrder))
        assert purchasing == 2 and approved == 1 and orders == 2


def test_score_stage_passes_cap_as_limit(monkeypatch):
    seen = {}
    monkeypatch.setattr("app.services.scoring.score_pending",
                        lambda limit=100, on_progress=None: seen.setdefault("limit", limit) or 4)
    autonomy.update_autonomy(cap_score=7)
    _enable(auto_score=True)
    out = orch.run_sweep(trigger="cron")
    assert seen["limit"] == 7 and out["counts"]["score"] == 4


def test_provision_stage_two_suboperations(monkeypatch):
    calls = []
    monkeypatch.setattr("app.services.provisioning.create_site_for", lambda did: calls.append(("create", did)) or 1)
    monkeypatch.setattr("app.services.provisioning.provision", lambda sid: calls.append(("prov", sid)) or {})
    with db.SessionLocal() as s:
        d = Domain(domain="buy.ru", source="backorder", status="purchased")
        s.add(d); s.commit()
        s.add(Site(domain_id=d.id, status="provisioning")); s.commit()   # уже есть сайт в provisioning
        d2 = Domain(domain="buy2.ru", source="backorder", status="purchased")
        s.add(d2); s.commit()                                            # покупка без сайта
    _enable(auto_provision=True)
    orch.run_sweep(trigger="cron")
    kinds = {c[0] for c in calls}
    assert "create" in kinds and "prov" in kinds                        # обе под-операции сработали


def test_generate_stage_uses_competitor(monkeypatch):
    seen = {}
    monkeypatch.setattr("app.services.content.generate_site",
                        lambda site_id, use_competitor=False: seen.update(sid=site_id, uc=use_competitor) or 3)
    with db.SessionLocal() as s:
        d = Domain(domain="g.ru", source="backorder", status="purchased")
        s.add(d); s.commit()
        s.add(Site(domain_id=d.id, status="content")); s.commit()       # content без страниц
    _enable(auto_generate=True)
    orch.run_sweep(trigger="cron")
    assert seen.get("uc") is True                                       # спек: use_competitor=True


def test_gate_invariants_never_cross_human_gates(monkeypatch):
    """ЖЁСТКО: свип со ВСЕМИ тумблерами не двигает scored/draft и не зовёт гейт-функции."""
    for fn in ("confirm_order", "execute_confirmed_order", "mark_caught"):
        monkeypatch.setattr(f"app.services.acquisition.{fn}",
                            lambda *a, **k: (_ for _ in ()).throw(AssertionError(f"gate {fn} called")))
    monkeypatch.setattr("app.services.content.mark_edited",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("editorial gate called")))
    # offline: сетевые bulk-стадии в no-op, чтобы тумблеры можно было включить все
    monkeypatch.setattr("app.services.discovery.run_discovery", lambda on_progress=None: 0)
    monkeypatch.setattr("app.services.scoring.score_pending", lambda limit=100, on_progress=None: 0)
    with db.SessionLocal() as s:
        s.add(Domain(domain="scored.ru", source="backorder", status="scored"))
        d = Domain(domain="site.ru", source="backorder", status="purchased")
        s.add(d); s.commit()
        site = Site(domain_id=d.id, status="content"); s.add(site); s.commit()
        s.add(Page(site_id=site.id, url_path="/", status="draft", body="<p>x</p>")); s.commit()
    _enable(auto_discovery=True, auto_score=True, auto_queue=True, auto_provision=True,
            auto_generate=True, auto_publish=True, auto_check_index=True)
    monkeypatch.setattr("app.services.provisioning.create_site_for", lambda did: 0)
    monkeypatch.setattr("app.services.provisioning.provision", lambda sid: {})
    monkeypatch.setattr("app.services.content.generate_site", lambda site_id, use_competitor=False: 0)
    monkeypatch.setattr("app.services.publish.publish_site", lambda sid: {})
    monkeypatch.setattr("app.services.publish.check_index", lambda sid: {})
    orch.run_sweep(trigger="cron")   # не бросает (гейт-функции не вызваны)
    with db.SessionLocal() as s:
        from sqlalchemy import select, func
        scored = s.scalar(select(Domain.status).where(Domain.domain == "scored.ru"))
        draft = s.scalar(select(Page.status).where(Page.url_path == "/"))
        purchased_extra = s.scalar(select(func.count()).select_from(Domain).where(Domain.status == "purchased"))
        assert scored == "scored"        # курационный гейт: scored не двинулся
        assert draft == "draft"          # редактурный гейт: draft не стал edited
        assert purchased_extra == 1      # money-байпас: свип НЕ наплодил purchased (только исходный)


def test_single_flight_second_sweep_skipped():
    _enable()                            # мастер вкл, стадий нет
    orch._acquire_lock("cron")           # держим замок вручную (свежий running)
    assert orch.run_sweep(trigger="cron") == {"skipped": "already_running"}
```

- [ ] **Step 2: Прогнать — убедиться, что новые падают**

Run: `.venv/bin/python -m pytest backend/tests/test_orchestrator.py -q`
Expected: FAIL — `AttributeError: module 'app.services.orchestrator' has no attribute 'run_sweep'`.

- [ ] **Step 3: Дописать стадии + run_sweep в `backend/app/services/orchestrator.py`**

Добавить в конец файла (после `last_finished_sweep_at`):

```python
# --- стадии: каждая = запрос кандидатов + вызов существующего сервиса + учёт ---------
# handler(cap) -> (сделано:int, ошибки:list[str]). cap=None только у discovery.

def _stage_discovery(cap):
    from app.services import discovery
    return discovery.run_discovery(), []


def _stage_score(cap):
    from app.services import scoring
    return scoring.score_pending(limit=cap), []


def _stage_queue(cap):
    """approved-домены (у них по определению нет открытого заказа) -> create_order, до капа."""
    from sqlalchemy import select
    from app.db import SessionLocal
    from app.models.domain import Domain
    from app.services import acquisition

    done, errs = 0, []
    with SessionLocal() as db:
        ids = [r[0] for r in db.execute(
            select(Domain.id).where(Domain.status == "approved").order_by(Domain.id).limit(cap)).all()]
    for did in ids:
        try:
            acquisition.create_order(did)      # деньги НЕ тратит — только заявка pending_confirm
            done += 1
        except Exception as e:  # noqa: BLE001
            errs.append(f"domain#{did}: {type(e).__name__}: {e}")
    return done, errs


def _stage_provision(cap):
    """Две под-операции под общим капом: (а) purchased без сайта -> create_site_for;
    (б) сайт в provisioning -> provision (идемпотентен, awaiting_ns = норм, повторим)."""
    from sqlalchemy import select
    from app.db import SessionLocal
    from app.models.domain import Domain
    from app.models.site import Site
    from app.services import provisioning

    done, errs = 0, []
    with SessionLocal() as db:
        purchased = [r[0] for r in db.execute(
            select(Domain.id).where(Domain.status == "purchased",
                                    ~Domain.id.in_(select(Site.domain_id)))
            .order_by(Domain.id).limit(cap)).all()]
        prov_ids = [r[0] for r in db.execute(
            select(Site.id).where(Site.status == "provisioning").order_by(Site.id).limit(cap)).all()]
    for did in purchased:
        if done >= cap:
            break
        try:
            provisioning.create_site_for(did)
            done += 1
        except Exception as e:  # noqa: BLE001
            errs.append(f"domain#{did}: {type(e).__name__}: {e}")
    for sid in prov_ids:
        if done >= cap:
            break
        try:
            provisioning.provision(sid)
            done += 1
        except Exception as e:  # noqa: BLE001
            errs.append(f"site#{sid}: {type(e).__name__}: {e}")
    return done, errs


def _stage_generate(cap):
    """Сайты status=content без страниц -> generate_site(use_competitor=True)."""
    from sqlalchemy import select
    from app.db import SessionLocal
    from app.models.site import Site, Page
    from app.services import content

    done, errs = 0, []
    with SessionLocal() as db:
        ids = [r[0] for r in db.execute(
            select(Site.id).where(Site.status == "content",
                                  ~Site.id.in_(select(Page.site_id)))
            .order_by(Site.id).limit(cap)).all()]
    for sid in ids:
        try:
            content.generate_site(sid, use_competitor=True)
            done += 1
        except Exception as e:  # noqa: BLE001
            errs.append(f"site#{sid}: {type(e).__name__}: {e}")
    return done, errs


def _stage_publish(cap):
    """Сайты с ≥1 edited-страницей -> publish_site (публикует все edited; гейт держится в сервисе)."""
    from sqlalchemy import select
    from app.db import SessionLocal
    from app.models.site import Site, Page
    from app.services import publish

    done, errs = 0, []
    with SessionLocal() as db:
        ids = [r[0] for r in db.execute(
            select(Site.id).where(Site.id.in_(
                select(Page.site_id).where(Page.status == "edited")))
            .order_by(Site.id).limit(cap)).all()]
    for sid in ids:
        try:
            publish.publish_site(sid)
            done += 1
        except Exception as e:  # noqa: BLE001
            errs.append(f"site#{sid}: {type(e).__name__}: {e}")
    return done, errs


def _stage_check_index(cap):
    """Сайты с published-страницами -> check_index (site: через SearXNG)."""
    from sqlalchemy import select
    from app.db import SessionLocal
    from app.models.site import Site, Page
    from app.services import publish

    done, errs = 0, []
    with SessionLocal() as db:
        ids = [r[0] for r in db.execute(
            select(Site.id).where(Site.id.in_(
                select(Page.site_id).where(Page.status == "published")))
            .order_by(Site.id).limit(cap)).all()]
    for sid in ids:
        try:
            publish.check_index(sid)
            done += 1
        except Exception as e:  # noqa: BLE001
            errs.append(f"site#{sid}: {type(e).__name__}: {e}")
    return done, errs


# порядок конвейера — единственный источник истины оркестратора
STAGES = [
    ("discovery", "auto_discovery", None, _stage_discovery),
    ("score", "auto_score", "cap_score", _stage_score),
    ("queue", "auto_queue", "cap_queue", _stage_queue),
    ("provision", "auto_provision", "cap_provision", _stage_provision),
    ("generate", "auto_generate", "cap_generate", _stage_generate),
    ("publish", "auto_publish", "cap_publish", _stage_publish),
    ("check_index", "auto_check_index", "cap_check_index", _stage_check_index),
]


def run_sweep(trigger: str = "cron", on_progress=None, respect_master: bool = True) -> dict:
    """Прогнать включённые авто-стадии до гейтов. respect_master=False у ручного запуска.

    ЖЁСТКО: зовёт ТОЛЬКО безопасные сервисы из STAGES. НИКОГДА — confirm_order/
    execute_confirmed_order/mark_caught (деньги) и mark_edited (редактура): эти три гейта
    двигает только человек через роуты панели. Ошибка одной сущности не топит стадию/свип.
    """
    from app.services.autonomy import get_autonomy

    cfg = get_autonomy()
    if respect_master and not cfg["autopilot_on"]:
        return {"skipped": "autopilot_off"}
    run_id = _acquire_lock(trigger)
    if run_id is None:
        return {"skipped": "already_running"}

    enabled = [s for s in STAGES if cfg[s[1]]]
    total = len(enabled)
    counts, errors, status = {}, [], "done"
    for i, (key, _flag, cap_attr, handler) in enumerate(enabled):
        if on_progress:
            on_progress(i, total, key)
        cap = cfg[cap_attr] if cap_attr else None
        try:
            n, errs = handler(cap)
            counts[key] = n
            errors += [f"{key}: {e}" for e in errs]
        except Exception as e:  # noqa: BLE001 — стадия целиком упала (не одна сущность)
            errors.append(f"{key}: {type(e).__name__}: {e}")
            status = "failed"
    if on_progress:
        on_progress(total, total, "")
    _finish_run(run_id, status, counts, errors)
    return {"run_id": run_id, "status": status, "counts": counts, "errors": errors}
```

- [ ] **Step 4: Прогнать — тесты проходят**

Run: `.venv/bin/python -m pytest backend/tests/test_orchestrator.py -q`
Expected: PASS (все, включая `test_gate_invariants_never_cross_human_gates` и `test_single_flight_second_sweep_skipped`).

- [ ] **Step 5: Полный прогон + линт**

Run: `.venv/bin/python -m pytest backend/tests/ -q && .venv/bin/python -m pyflakes backend/app backend/tests`
Expected: все PASS, pyflakes чисто.

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/orchestrator.py backend/tests/test_orchestrator.py
git commit -m "спек 3 задача 3: оркестратор run_sweep — таблица стадий + гейт-инварианты (регрессия)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Шедулер — частый тик + throttle из конфига

**Files:**
- Modify: `backend/app/workers/scheduler.py` (полная замена `m1_cycle`/`main`)
- Test: `backend/tests/test_scheduler.py`

**Interfaces:**
- Consumes: `autonomy.get_autonomy()`, `orchestrator.last_finished_sweep_at()`, `orchestrator.run_sweep(trigger="cron")`.
- Produces: `scheduler.TICK_MIN = 5`, `scheduler.tick() -> None`.

- [ ] **Step 1: Написать падающий тест тика**

Создать `backend/tests/test_scheduler.py`:

```python
"""Шедулер-тик: мастер-выкл -> skip; throttle -> skip; иначе -> run_sweep(trigger=cron)."""
from datetime import datetime, timezone, timedelta

from app.workers import scheduler
from app.services import autonomy


def test_tick_skips_when_autopilot_off(monkeypatch):
    called = []
    monkeypatch.setattr("app.services.orchestrator.run_sweep", lambda **k: called.append(k))
    autonomy.update_autonomy(autopilot_on=False)
    scheduler.tick()
    assert called == []


def test_tick_skips_when_throttled(monkeypatch):
    called = []
    monkeypatch.setattr("app.services.orchestrator.run_sweep", lambda **k: called.append(k))
    # последний свип только что закончился -> интервал ещё не прошёл
    monkeypatch.setattr("app.services.orchestrator.last_finished_sweep_at",
                        lambda: datetime.now(timezone.utc))
    autonomy.update_autonomy(autopilot_on=True, sweep_interval_min=60)
    scheduler.tick()
    assert called == []


def test_tick_runs_when_due(monkeypatch):
    called = []
    monkeypatch.setattr("app.services.orchestrator.run_sweep", lambda **k: called.append(k))
    monkeypatch.setattr("app.services.orchestrator.last_finished_sweep_at",
                        lambda: datetime.now(timezone.utc) - timedelta(hours=2))
    autonomy.update_autonomy(autopilot_on=True, sweep_interval_min=60)
    scheduler.tick()
    assert called == [{"trigger": "cron"}]


def test_tick_runs_when_never_swept(monkeypatch):
    called = []
    monkeypatch.setattr("app.services.orchestrator.run_sweep", lambda **k: called.append(k))
    monkeypatch.setattr("app.services.orchestrator.last_finished_sweep_at", lambda: None)
    autonomy.update_autonomy(autopilot_on=True)
    scheduler.tick()
    assert called == [{"trigger": "cron"}]
```

- [ ] **Step 2: Прогнать — убедиться, что падает**

Run: `.venv/bin/python -m pytest backend/tests/test_scheduler.py -q`
Expected: FAIL — `AttributeError: module 'app.workers.scheduler' has no attribute 'tick'`.

- [ ] **Step 3: Переписать `backend/app/workers/scheduler.py`**

Полная замена содержимого:

```python
"""Автопилот-воркер (APScheduler). Частый тик + throttle из конфига autonomy_settings.

Каждый тик читает конфиг СВЕЖИМ из БД -> тумблеры/интервал применяются без рестарта
воркера. Работу двигает orchestrator.run_sweep (single-flight внутри). Отдельный процесс
docker-compose `worker`, общий с панелью Postgres. Прежний суточный m1_cycle удалён —
его поведение = auto_discovery + auto_score через оркестратор.
"""
from datetime import datetime, timezone

from apscheduler.schedulers.blocking import BlockingScheduler

TICK_MIN = 5   # фиксированный частый тик; реальную частоту свипов задаёт sweep_interval_min


def tick() -> None:
    from app.services import orchestrator
    from app.services.autonomy import get_autonomy

    cfg = get_autonomy()
    if not cfg["autopilot_on"]:
        return                                          # мастер выкл — применяется сразу
    last = orchestrator.last_finished_sweep_at()
    if last is not None:
        if (datetime.now(timezone.utc) - last).total_seconds() < cfg["sweep_interval_min"] * 60:
            return                                      # throttle: рано для следующего свипа
    orchestrator.run_sweep(trigger="cron")              # single-flight внутри


def main() -> None:
    sched = BlockingScheduler(timezone="UTC")
    sched.add_job(tick, "interval", minutes=TICK_MIN, id="autopilot_tick",
                  misfire_grace_time=TICK_MIN * 60)
    print(f"[worker] autopilot tick every {TICK_MIN} min (throttle from autonomy_settings)", flush=True)
    sched.start()


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Прогнать — тесты проходят**

Run: `.venv/bin/python -m pytest backend/tests/test_scheduler.py -q`
Expected: PASS (4 passed).

- [ ] **Step 5: Полный прогон + линт**

Run: `.venv/bin/python -m pytest backend/tests/ -q && .venv/bin/python -m pyflakes backend/app backend/tests`
Expected: все PASS, pyflakes чисто.

- [ ] **Step 6: Commit**

```bash
git add backend/app/workers/scheduler.py backend/tests/test_scheduler.py
git commit -m "спек 3 задача 4: шедулер — тик 5 мин + throttle из конфига (тумблеры без рестарта)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: Экран «Автопилот» — роуты + шаблон + сайдбар + прогресс

**Files:**
- Modify: `backend/app/api/panel.py` (роуты `/autopilot`, `/autopilot/settings`, `/autopilot/run`; `_gates()`; whitelist `"sweep"`)
- Create: `backend/app/templates/autopilot.html`
- Modify: `backend/app/templates/base.html:329-331` (пункт сайдбара)
- Test: `backend/tests/test_autopilot_panel.py`

**Interfaces:**
- Consumes: `autonomy.get_autonomy()` / `update_autonomy()`, `orchestrator.run_sweep`, `jobs.start`/`jobs.report`/`jobs.progress`, `AutonomyRun`.
- Produces (используется Task 6): `panel._gates(db) -> dict` со ключами `curate`, `money`, `edit` (счётчики у гейтов).

- [ ] **Step 1: Написать падающие тесты роутов**

Создать `backend/tests/test_autopilot_panel.py`:

```python
"""Экран «Автопилот»: рендер, сохранение настроек, запуск ручного свипа, whitelist прогресса."""
import app.db as db
from app.models.domain import Domain, AcquisitionOrder
from app.models.site import Site, Page
from app.services import autonomy


def test_autopilot_renders(client):
    r = client.get("/autopilot")
    assert r.status_code == 200
    assert "Мастер-выключатель" in r.text     # станция мастера (не сайдбар — контент экрана)
    assert "на курацию" in r.text             # блок «ждёт тебя»


def test_autopilot_settings_save(client):
    r = client.post("/autopilot/settings", data={
        "autopilot_on": "on", "sweep_interval_min": 30,
        "auto_score": "on", "cap_score": 15}, follow_redirects=False)
    assert r.status_code == 303
    a = autonomy.get_autonomy()
    assert a["autopilot_on"] is True and a["sweep_interval_min"] == 30
    assert a["auto_score"] is True and a["cap_score"] == 15
    assert a["auto_publish"] is False          # непереданный чекбокс -> False


def test_autopilot_unchecked_toggle_turns_off(client):
    autonomy.update_autonomy(auto_queue=True)
    client.post("/autopilot/settings", data={"sweep_interval_min": 60}, follow_redirects=False)
    assert autonomy.get_autonomy()["auto_queue"] is False   # чекбокс не пришёл -> выкл


def test_autopilot_run_starts_job(client, monkeypatch):
    seen = {}
    monkeypatch.setattr("app.services.orchestrator.run_sweep",
                        lambda **k: seen.update(k) or {"run_id": 1, "status": "done", "counts": {}, "errors": []})
    r = client.post("/autopilot/run", follow_redirects=False)
    assert r.status_code == 303
    import time
    for _ in range(50):                        # джоб фоновый — дождаться исполнения
        if seen:
            break
        time.sleep(0.02)
    assert seen.get("trigger") == "manual" and seen.get("respect_master") is False


def test_sweep_progress_whitelisted(client):
    assert client.get("/run/sweep/progress").status_code == 200
    assert client.get("/run/bogus/progress").status_code == 404


def test_gates_counts(client):
    with db.SessionLocal() as s:
        s.add(Domain(domain="sc.ru", source="backorder", status="scored"))
        d = Domain(domain="q.ru", source="backorder", status="purchasing")
        s.add(d); s.commit()
        s.add(AcquisitionOrder(domain_id=d.id, provider="backorder",
                               status="pending_confirm", confirmed_by_human=False))
        site = Site(domain_id=d.id, status="content"); s.add(site); s.commit()
        s.add(Page(site_id=site.id, url_path="/", status="draft")); s.commit()
    html = client.get("/autopilot").text
    assert "/domains?status=scored" in html and "/queue" in html
```

- [ ] **Step 2: Прогнать — убедиться, что падает**

Run: `.venv/bin/python -m pytest backend/tests/test_autopilot_panel.py -q`
Expected: FAIL — 404 на `/autopilot` (роут ещё не зарегистрирован).

- [ ] **Step 3: Добавить `_gates()` helper и роуты в `backend/app/api/panel.py`**

После функции `_pool_counts` (строка 129) добавить helper:

```python
def _gates(db: Session) -> dict:
    """Счётчики «ждёт тебя» у трёх человеческих гейтов (для экрана Автопилот + Пульта)."""
    from app.models.domain import AcquisitionOrder
    curate = db.scalar(select(func.count()).select_from(Domain).where(Domain.status == "scored")) or 0
    money = db.scalar(select(func.count()).select_from(AcquisitionOrder).where(
        AcquisitionOrder.status == "pending_confirm", AcquisitionOrder.confirmed_by_human.is_(False))) or 0
    edit = db.scalar(select(func.count()).select_from(Page).where(Page.status == "draft")) or 0
    return {"curate": curate, "money": money, "edit": edit}
```

После роута `settings_view` (строка 195, в секции ЭКРАНЫ) добавить экран «Автопилот»:

```python
@router.get("/autopilot", response_class=HTMLResponse)
def autopilot_view(request: Request, db: Session = Depends(get_session)):
    from app.services.autonomy import get_autonomy
    from app.models.autonomy import AutonomyRun
    runs = db.execute(select(AutonomyRun).order_by(AutonomyRun.id.desc()).limit(10)).scalars().all()
    return templates.TemplateResponse(request, "autopilot.html", {
        "active": "autopilot", "a": get_autonomy(), "gates": _gates(db), "runs": runs})
```

В секции ДЕЙСТВИЯ (после `settings_reset`, конец файла) добавить POST-роуты:

```python
@router.post("/autopilot/settings")
def autopilot_settings_save(
        autopilot_on: str = Form(""), sweep_interval_min: int = Form(60),
        auto_discovery: str = Form(""), auto_score: str = Form(""), auto_queue: str = Form(""),
        auto_provision: str = Form(""), auto_generate: str = Form(""), auto_publish: str = Form(""),
        auto_check_index: str = Form(""),
        cap_score: int = Form(20), cap_queue: int = Form(10), cap_provision: int = Form(5),
        cap_generate: int = Form(5), cap_publish: int = Form(5), cap_check_index: int = Form(20)):
    from app.services.autonomy import update_autonomy
    update_autonomy(
        autopilot_on=bool(autopilot_on), sweep_interval_min=sweep_interval_min,
        auto_discovery=bool(auto_discovery), auto_score=bool(auto_score), auto_queue=bool(auto_queue),
        auto_provision=bool(auto_provision), auto_generate=bool(auto_generate),
        auto_publish=bool(auto_publish), auto_check_index=bool(auto_check_index),
        cap_score=cap_score, cap_queue=cap_queue, cap_provision=cap_provision,
        cap_generate=cap_generate, cap_publish=cap_publish, cap_check_index=cap_check_index)
    return _back("/autopilot", msg="Настройки автопилота сохранены")


@router.post("/autopilot/run")
def autopilot_run_action():
    from app.services import orchestrator, jobs
    ok = jobs.start("sweep", lambda: orchestrator.run_sweep(
        trigger="manual", respect_master=False,
        on_progress=lambda d, t, c: jobs.report("sweep", d, t, c)))
    return _back("/autopilot", msg="Свип запущен…" if ok else None,
                 err=None if ok else "Свип уже идёт")
```

Расширить whitelist в `run_progress` (строка 282): заменить кортеж на

```python
    if job not in ("discovery", "score", "sweep"):   # только известные джобы, не эхо любого пути
```

- [ ] **Step 4: Создать шаблон `backend/app/templates/autopilot.html`**

```html
{% extends "base.html" %}
{% block title %}Автопилот{% endblock %}
{% block content %}
<h2><span class="idx">✈</span> Автопилот
  <span class="hint">комбайн двигает конвейер сам — до человеческих гейтов; деньги и редактура всегда за тобой</span></h2>

<div class="funnel" style="margin-bottom:6px">
  <div class="stat"><a href="/domains?status=scored" title="ждут курации: реши ✓ approve / ✗ reject">
    <div class="v {{ 'hot' if gates.curate }}">{{ gates.curate }}</div>
    <div class="k">на курацию</div><div class="k" style="color:var(--dim)">гейт отбора</div></a></div>
  <span class="fun-arrow">▸</span>
  <div class="stat"><a href="/queue" title="ждут подтверждения выкупа — денежный гейт (только ты)">
    <div class="v {{ 'hot' if gates.money }}">{{ gates.money }}</div>
    <div class="k">на подтверждение</div><div class="k" style="color:var(--dim)">денежный гейт</div></a></div>
  <span class="fun-arrow">▸</span>
  <div class="stat"><a href="/#sites" title="черновики ждут редактуры — публикация возьмёт только edited">
    <div class="v {{ 'hot' if gates.edit }}">{{ gates.edit }}</div>
    <div class="k">на редактуру</div><div class="k" style="color:var(--dim)">гейт редактуры</div></a></div>
</div>

<div id="prog" class="progress">
  <span class="lbl"></span><div class="track"><div class="fill"></div></div><span class="pct"></span>
</div>
<form method="post" action="/autopilot/run" style="margin:6px 0 18px">
  <button class="btn-amber" title="прогнать включённые стадии один раз прямо сейчас (минует мастер-выключатель, уважает тумблеры стадий)">▶ Прогнать сейчас</button>
  <span class="hint">разовый свип: пройдёт по включённым ниже стадиям до ближайшего гейта</span>
</form>

<form method="post" action="/autopilot/settings" style="max-width:820px; display:grid; gap:14px">
  <div class="station">
    <div class="plate">Мастер-выключатель — <b>шедулер свипает автоматически</b></div>
    <div class="what">Включено → воркер прогоняет включённые стадии по расписанию (throttle ниже).
      Выключено → машина двигается только кнопкой «Прогнать сейчас». Разовый прогон мастер минует.</div>
    <div class="go">
      <label><input type="checkbox" name="autopilot_on" {{ 'checked' if a.autopilot_on }}> автопилот включён</label>
      <span class="hint" style="margin-left:16px">интервал между авто-свипами, мин:</span>
      <input type="number" name="sweep_interval_min" min="5" max="1440" value="{{ a.sweep_interval_min }}"
             style="width:90px; padding:2px 8px">
    </div>
  </div>

  {% set stages = [
    ('discovery','Поиск дропов','собрать свежие домены из включённых источников','discovered', none),
    ('score','Проверка','прогнать воронку скоринга; сильные и чистые уйдут в approved','до гейта курации', a.cap_score),
    ('queue','В очередь выкупа','поставить одобренные в очередь заказов','до денежного гейта', a.cap_queue),
    ('provision','Провижн','поднять сайт: Cloudflare + aaPanel (идемпотентно)','provisioning → content', a.cap_provision),
    ('generate','Черновики','сгенерировать AI-черновики страниц','до гейта редактуры', a.cap_generate),
    ('publish','Публикация','выложить страницы','только уже edited', a.cap_publish),
    ('check_index','Индексация','проверить попадание в индекс (site:)','обновить статус', a.cap_check_index)
  ] %}
  {% for key, name, what, gate, cap in stages %}
  <div class="station">
    <div class="plate">Стадия · {{ name }} — <b>{{ gate }}</b></div>
    <div class="what">{{ what }}.</div>
    <div class="go">
      <label><input type="checkbox" name="auto_{{ key }}" {{ 'checked' if a['auto_' + key] }}> автоматом</label>
      {% if cap is not none %}
      <span class="hint" style="margin-left:16px">кап за свип:</span>
      <input type="number" name="cap_{{ key }}" min="0" max="500" value="{{ cap }}"
             style="width:80px; padding:2px 8px">
      {% else %}
      <span class="hint" style="margin-left:16px">капа нет — bulk-pull всего фида</span>
      {% endif %}
    </div>
  </div>
  {% endfor %}

  <div class="station">
    <div class="plate">Применить — <b>записать конфиг автопилота</b></div>
    <div class="what">Тумблеры и капы применяются со следующего тика воркера (без рестарта).</div>
    <div class="go"><button class="btn-amber" title="сохранить конфиг автопилота">Сохранить</button></div>
  </div>
</form>

<h2 style="margin-top:26px"><span class="idx">≡</span> Последние прогоны
  <span class="hint">что машина сделала за свип; ошибки — если стадия споткнулась</span></h2>
<div class="card">
  <table class="tbl">
    <thead><tr><th>#</th><th>старт</th><th>триггер</th><th>статус</th><th>по стадиям</th><th>ошибки</th></tr></thead>
    <tbody>
    {% for r in runs %}
      <tr>
        <td>{{ r.id }}</td>
        <td class="mono">{{ r.started_at.strftime('%d.%m %H:%M') if r.started_at else '' }}</td>
        <td>{{ 'ручной' if r.trigger == 'manual' else 'по расписанию' }}</td>
        <td><span class="badge b-{{ 'approved' if r.status == 'done' else 'rejected' if r.status == 'failed' else 'discovered' }}">{{ r.status }}</span></td>
        <td class="mono">{% for k, v in (r.counts or {}).items() %}{{ k }}:{{ v }} {% endfor %}</td>
        <td class="hint">{{ (r.errors or [])|length }}{% if r.errors %} · {{ r.errors[0] }}{% endif %}</td>
      </tr>
    {% else %}
      <tr><td colspan="6" class="hint">ещё не было прогонов</td></tr>
    {% endfor %}
    </tbody>
  </table>
</div>

<script>
// прогрессбар свипа: тот же терминальный контракт, что и в domains.html (см. services/jobs.py).
var JOB_RU = {sweep:'Свип автопилота'};
function bar(){ return document.getElementById('prog'); }
function setLabel(lbl, name, tail){
  var b = document.createElement('b'); b.textContent = name;
  lbl.replaceChildren(b);
  if (tail) lbl.appendChild(document.createTextNode(' — ' + tail));
}
function render(job, p){
  var el = bar(), lbl = el.querySelector('.lbl'), fill = el.querySelector('.fill'),
      pct = el.querySelector('.pct'), name = JOB_RU[job] || job;
  el.className = 'progress show';
  if (p.error){ el.classList.add('err');
    setLabel(lbl, name, 'ошибка: ' + p.error); pct.textContent=''; fill.style.width='100%'; return; }
  if (p.running){
    if (p.total>0){ fill.style.width=Math.round(p.done/p.total*100)+'%';
      setLabel(lbl, name, p.current || ''); pct.textContent=p.done+'/'+p.total; }
    else { el.classList.add('indet'); fill.style.width=''; setLabel(lbl, name, p.current || ''); pct.textContent='…'; }
    return;
  }
  el.classList.add('done'); fill.style.width='100%';
  setLabel(lbl, name, 'готово'); pct.textContent=p.done;
}
function poll(job){
  fetch('/run/'+job+'/progress').then(r=>r.json()).then(p=>{
    render(job, p);
    if (p.error) return;
    if (p.running){ setTimeout(()=>poll(job), 1500); }
    else { setTimeout(()=>location.reload(), 800); }
  }).catch(()=>{ setTimeout(()=>poll(job), 1500); });
}
fetch('/run/sweep/progress').then(r=>r.json())
  .then(p=>{ if (p.running) poll('sweep'); else if (p.error) render('sweep', p); }).catch(()=>{});
</script>
{% endblock %}
```

- [ ] **Step 5: Добавить пункт «Автопилот» в сайдбар `backend/app/templates/base.html`**

После блока «Настройки» (строки 329-331) добавить пункт. Заменить:

```html
    <a class="rl {{ 'on' if active=='settings' }}" href="/settings">
      <span><span class="n">⚙</span>Настройки</span>
      <span class="sub">пороги воронки и источники</span></a>
  </nav>
```

на:

```html
    <a class="rl {{ 'on' if active=='settings' }}" href="/settings">
      <span><span class="n">⚙</span>Настройки</span>
      <span class="sub">пороги воронки и источники</span></a>
    <a class="rl {{ 'on' if active=='autopilot' }}" href="/autopilot">
      <span><span class="n">✈</span>Автопилот</span>
      <span class="sub">тумблеры стадий, капы, «ждёт тебя»</span></a>
  </nav>
```

- [ ] **Step 6: Прогнать — тесты проходят**

Run: `.venv/bin/python -m pytest backend/tests/test_autopilot_panel.py -q`
Expected: PASS (7 passed).

- [ ] **Step 7: Полный прогон + линт**

Run: `.venv/bin/python -m pytest backend/tests/ -q && .venv/bin/python -m pyflakes backend/app backend/tests`
Expected: все PASS, pyflakes чисто.

- [ ] **Step 8: Commit**

```bash
git add backend/app/api/panel.py backend/app/templates/autopilot.html \
        backend/app/templates/base.html backend/tests/test_autopilot_panel.py
git commit -m "спек 3 задача 5: экран «Автопилот» — тумблеры/капы + «ждёт тебя» + run-лог + прогон

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 6: Полоска статуса автопилота на Пульте + визуальная проверка

**Files:**
- Modify: `backend/app/api/panel.py` (контекст `dashboard`)
- Modify: `backend/app/templates/dashboard.html` (полоска статуса)
- Test: `backend/tests/test_autopilot_panel.py` (дописать)

**Interfaces:**
- Consumes: `panel._gates(db)` (Task 5), `autonomy.get_autonomy()`, `orchestrator.last_finished_sweep_at()`.

- [ ] **Step 1: Дописать падающий тест полоски**

Добавить в `backend/tests/test_autopilot_panel.py`:

```python
def test_dashboard_shows_autopilot_strip(client):
    # сайдбар (Task 5) уже содержит «Автопилот»/href — проверяем текст самой полоски
    autonomy.update_autonomy(autopilot_on=True)
    html = client.get("/").text
    assert "✈ Автопилот: вкл" in html          # бейдж мастера в полоске
    assert "последний свип" in html and "ждёт тебя" in html
```

- [ ] **Step 2: Прогнать — убедиться, что падает**

Run: `.venv/bin/python -m pytest backend/tests/test_autopilot_panel.py::test_dashboard_shows_autopilot_strip -q`
Expected: FAIL — `assert "Автопилот" in html` (полоски ещё нет).

- [ ] **Step 3: Расширить контекст роута `dashboard` в `backend/app/api/panel.py`**

В `dashboard` (строки 135-146) добавить в контекст автопилот-данные. Заменить тело функции:

```python
@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_session)):
    from app.services.autonomy import get_autonomy
    from app.services.orchestrator import last_finished_sweep_at
    dc = _domain_counts(db)
    return templates.TemplateResponse(request, "dashboard.html", {
        "active": "dash",
        "dc": dc, "d_total": sum(dc.values()),
        "pc": _page_counts(db),
        "offers_active": db.scalar(select(func.count()).select_from(Offer).where(Offer.active.is_(True))) or 0,
        "offers_total": db.scalar(select(func.count()).select_from(Offer)) or 0,
        "sites": _sites_overview(db),
        "steps": _next_steps(db),
        "autopilot": get_autonomy(), "gates": _gates(db), "last_sweep": last_finished_sweep_at(),
    })
```

- [ ] **Step 4: Добавить полоску статуса в `backend/app/templates/dashboard.html`**

Сразу после `{% block content %}` (строка 3, перед `<h2>… Воронка конвейера`) вставить:

```html
<div class="card" style="display:flex; align-items:center; gap:16px; flex-wrap:wrap; margin-bottom:14px">
  <span class="badge b-{{ 'approved' if autopilot.autopilot_on else 'discovered' }}"
        title="мастер-выключатель автопилота">✈ Автопилот: {{ 'вкл' if autopilot.autopilot_on else 'выкл' }}</span>
  <span class="hint">последний свип:
    <b>{{ last_sweep.strftime('%d.%m %H:%M') if last_sweep else '—' }}</b></span>
  <span class="hint">ждёт тебя —
    курация: <b>{{ gates.curate }}</b> ·
    подтверждение: <b>{{ gates.money }}</b> ·
    редактура: <b>{{ gates.edit }}</b></span>
  <a class="btn" href="/autopilot" title="открыть кокпит автопилота: тумблеры стадий, капы, прогоны"
     style="margin-left:auto">→ Автопилот</a>
</div>
```

- [ ] **Step 5: Прогнать — тесты проходят**

Run: `.venv/bin/python -m pytest backend/tests/test_autopilot_panel.py -q`
Expected: PASS (8 passed).

- [ ] **Step 6: Визуальная проверка (Playwright)**

Поднять панель локально на SQLite и снять скриншоты `/autopilot` и `/` (полоска). Проверить глазами: светлая CMS, оранжевый акцент, станции подписаны, полоска на Пульте читается, прогрессбар присутствует. Команды (bash):

```bash
cd backend && DATABASE_URL="sqlite:///./_visual.db" PANEL_USER="" PANEL_PASS="" \
  ../.venv/bin/python -c "
import app.db as db
from app.db import Base
import app.models.domain, app.models.site, app.models.offer, app.models.monitoring, app.models.settings, app.models.autonomy
Base.metadata.create_all(db.engine)
print('schema ready')
"
../.venv/bin/uvicorn app.main:app --port 8011 &
```

Затем через Playwright-плагин: navigate `http://localhost:8011/autopilot`, screenshot; navigate `http://localhost:8011/`, screenshot. После проверки — остановить uvicorn и удалить `backend/_visual.db`.

Ожидаемо: обе страницы в светлой теме, станции автопилота с плашками/описаниями, полоска статуса на Пульте с бейджем «✈ Автопилот: выкл» и счётчиками «ждёт тебя».

- [ ] **Step 7: Убрать визуальные артефакты + финальный прогон**

```bash
rm -f backend/_visual.db
.venv/bin/python -m pytest backend/tests/ -q && .venv/bin/python -m pyflakes backend/app backend/tests
```
Expected: все PASS, pyflakes чисто, `_visual.db` удалён (не коммитить).

- [ ] **Step 8: Commit**

```bash
git add backend/app/api/panel.py backend/app/templates/dashboard.html backend/tests/test_autopilot_panel.py
git commit -m "спек 3 задача 6: полоска статуса автопилота на Пульте + визуальная проверка

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 7: Улучшения §E — enum-коммент, money-байпас-предупреждения, локализация чипов

**Files:**
- Modify: `backend/app/models/domain.py:73` (коммент enum)
- Modify: `backend/app/api/pipeline.py:62` (коммент money-байпас)
- Modify: `backend/app/api/panel.py:301-308` (коммент money-байпас)
- Modify: `backend/app/templates/domains.html:146-149` (чипы через `|status_ru`)
- Modify: `backend/app/templates/dashboard.html:70-72` (локализация draft/edited/published в обзоре сайтов)
- Test: `backend/tests/test_autopilot_panel.py` (дописать локализацию чипов)

**Interfaces:**
- Consumes: `status_ru` (Jinja-фильтр, зарегистрирован в `panel.py:29`).
- Примечание: регрессия «оркестратор не двигает домен в purchased» уже покрыта в Task 3 (`test_gate_invariants_never_cross_human_gates`) — здесь только комменты + локализация.

- [ ] **Step 1: Написать падающий тест локализации чипов**

Добавить в `backend/tests/test_autopilot_panel.py`:

```python
def test_domains_filter_chips_localized(client):
    with db.SessionLocal() as s:
        s.add(Domain(domain="chip.ru", source="backorder", status="approved"))
        s.commit()
    html = client.get("/domains").text
    # чип статуса: видимый текст локализован, счётчик на месте
    assert "одобрен" in html                       # approved -> одобрен (через status_ru)
    assert ">approved <b>" not in html             # сырой английский в тексте чипа исчез
```

- [ ] **Step 2: Прогнать — убедиться, что падает**

Run: `.venv/bin/python -m pytest backend/tests/test_autopilot_panel.py::test_domains_filter_chips_localized -q`
Expected: FAIL — `assert ">approved <b>" not in html` (чип показывает сырой `approved`).

- [ ] **Step 3: Локализовать чипы в `backend/app/templates/domains.html`**

Заменить цикл чипов (строки 146-151):

```html
  {% for st in ['discovered','scored','approved','rejected','purchased','live'] %}
    {% if counts.get(st) %}
    <a class="chip {{ 'on' if f_status==st }}" href="/domains?status={{ st }}"
       title="показать только домены в статусе {{ st }}">{{ st }} <b>{{ counts[st] }}</b></a>
    {% endif %}
  {% endfor %}
```

на (видимый текст локализован, `title` держит сырой код для оператора):

```html
  {% for st in ['discovered','scored','approved','rejected','purchased','live'] %}
    {% if counts.get(st) %}
    <a class="chip {{ 'on' if f_status==st }}" href="/domains?status={{ st }}"
       title="показать только домены в статусе {{ st }} ({{ st|status_ru }})">{{ st|status_ru }} <b>{{ counts[st] }}</b></a>
    {% endif %}
  {% endfor %}
```

- [ ] **Step 4: Локализовать счётчики страниц в обзоре сайтов `backend/app/templates/dashboard.html`**

Заменить строки 70-72 (сводка страниц в карточке сайта):

```html
        стр.: {{ s.pages.get('draft', 0) }} draft ·
        {{ s.pages.get('edited', 0) }} edited ·
```

(и следующая строка с `published`) на русские слова:

```html
        стр.: {{ s.pages.get('draft', 0) }} черновик ·
        {{ s.pages.get('edited', 0) }} вычитано ·
```

Найти в тех же строках `published` и заменить подпись на `опубликовано` (сохранив число `{{ s.pages.get('published', 0) }}`). Точную третью строку сверить при правке — не менять числа, только английские подписи.

- [ ] **Step 5: Уточнить коммент enum в `backend/app/models/domain.py`**

Заменить строку 73:

```python
    # pending_confirm | ordered | caught | failed
```

на (добавить транзиентный `ordering` и `cancelled`, которые есть в acquisition.py):

```python
    # pending_confirm | ordering | ordered | caught | failed | cancelled
```

- [ ] **Step 6: Коммент-предупреждение у money-байпаса в `backend/app/api/pipeline.py`**

Заменить строку 62:

```python
    d.status = "purchased"   # ponytail: MVP buys by hand — the human action IS the money gate
```

на:

```python
    # ручной override money-gate: человек пометил домен купленным мимо очереди. Осознанный
    # обход (человек = денежный гейт). Оркестратор (services/orchestrator) этот роут НЕ зовёт.
    d.status = "purchased"   # ponytail: MVP buys by hand — the human action IS the money gate
```

- [ ] **Step 7: Коммент-предупреждение у ручной курации в `backend/app/api/panel.py`**

В `set_status_action` (строки 301-308) добавить предупреждение перед сменой статуса. Заменить:

```python
@router.post("/domains/{domain_id}/set-status")
def set_status_action(domain_id: int, status: str = Form(...), db: Session = Depends(get_session)):
    if status in _MANUAL_STATUSES:      # guard: только ручные переходы курации
```

на:

```python
@router.post("/domains/{domain_id}/set-status")
def set_status_action(domain_id: int, status: str = Form(...), db: Session = Depends(get_session)):
    # ручной override: 'purchased' здесь — money-gate человека мимо очереди; оркестратор
    # (services/orchestrator) этот роут НЕ зовёт (двигает только до pending_confirm).
    if status in _MANUAL_STATUSES:      # guard: только ручные переходы курации
```

- [ ] **Step 8: Прогнать — тесты проходят**

Run: `.venv/bin/python -m pytest backend/tests/test_autopilot_panel.py -q`
Expected: PASS (включая `test_domains_filter_chips_localized`).

- [ ] **Step 9: Полный прогон + линт**

Run: `.venv/bin/python -m pytest backend/tests/ -q && .venv/bin/python -m pyflakes backend/app backend/tests`
Expected: все PASS, pyflakes чисто.

- [ ] **Step 10: Commit**

```bash
git add backend/app/models/domain.py backend/app/api/pipeline.py backend/app/api/panel.py \
        backend/app/templates/domains.html backend/app/templates/dashboard.html \
        backend/tests/test_autopilot_panel.py
git commit -m "спек 3 задача 7: §E — enum-коммент, money-байпас-предупреждения, локализация чипов/счётчиков

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review

**1. Spec coverage** (спек §A–§H):
- §A конфиг тумблеров/капов → Task 1 (модель + сервис + дефолты/кламп). ✓
- §B оркестратор (таблица стадий, single-flight, ЖЁСТКОЕ правило, авто-одобрение→денежный гейт, provision=2 под-операции) → Task 2 (замок) + Task 3 (стадии, run_sweep, гейт-инварианты). ✓
- §C шедулер (TICK_MIN=5 + throttle, свежий конфиг, m1_cycle удалён) → Task 4. ✓
- §D run-лог + экран «Автопилот» (мастер+throttle, тумблеры+капы, «ждёт тебя» с deep-ссылками, последние N прогонов, «прогнать сейчас» + прогрессбар, whitelist "sweep", безопасность) → Task 5; Пульт-полоска → Task 6. ✓
- §E (enum-коммент, money-байпас-комменты+регрессия, локализация чипов/счётчиков) → Task 7 (комменты+локализация) + Task 3 (регрессия orchestrator-never-purchases). ✓
- §F границы (auto-editorial=Спек4, уведомления=Спек5, provider-polling, M6, деньги ручные) — вне плана, не реализуем. ✓
- §G инварианты (оба гейта, капы, single-flight, свежий конфиг, безопасность, офлайн-тесты) — покрыты Task 3 инвариант-тестом + структурой. ✓

**2. Placeholder scan:** нет TBD/TODO; каждый шаг с кодом несёт полный код; тесты с реальными ассертами; команды с ожидаемым выводом. ✓

**3. Type consistency:**
- `get_autonomy()` ключи (`autopilot_on`, `sweep_interval_min`, `auto_<stage>`, `cap_<stage>` кроме discovery) едины в Task 1 (сервис), Task 3 (`run_sweep` читает `cfg[flag]`/`cfg[cap_attr]`), Task 4 (`cfg["autopilot_on"]`/`cfg["sweep_interval_min"]`), Task 5 (форма). ✓
- `run_sweep(trigger, on_progress, respect_master)` сигнатура едина: Task 3 определяет, Task 4 зовёт `trigger="cron"`, Task 5 зовёт `trigger="manual", respect_master=False`. ✓
- `_acquire_lock`/`_finish_run`/`last_finished_sweep_at` определены Task 2, использованы Task 3/4. ✓
- `STAGES` кортеж `(key, flag_attr, cap_attr, handler)` — `run_sweep` распаковывает как `(key, _flag, cap_attr, handler)`. ✓
- `_gates(db)` ключи `curate/money/edit` — Task 5 определяет, Task 5 (autopilot.html) и Task 6 (dashboard) потребляют. ✓
- Прогресс-джоб `"sweep"` — whitelist (Task 5) совпадает с `jobs.start("sweep", …)` (Task 5) и poll в autopilot.html. ✓

Замечание для исполнителя: Task 7 Step 4 требует свериться с фактической третьей строкой (`published`) в `dashboard.html` при правке — grep перед заменой, менять только английские подписи, не числа.
