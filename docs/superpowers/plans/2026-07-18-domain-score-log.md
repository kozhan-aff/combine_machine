# История решений скоринга по домену (domain_score_log) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Append-only лог каждого прогона `scoring.score_domain()` по домену — история
решений воронки, не только последний снимок `Domain.score_breakdown`.

**Architecture:** Новая таблица `domain_score_log` (миграция `0024`), новая модель
`DomainScoreLog` (свой файл, как `JobRun` в `models/job.py`). Одна точка записи —
`scoring.score_domain()`, на каждом из трёх существующих путей возврата (unresolved/
rejected/scored). Один read-эндпоинт — `GET /api/domains/{id}/score-history`, тот же
файл/паттерн, что уже существующий `GET /api/jobs/live`.

**Tech Stack:** Python 3.12, SQLAlchemy 2.x, Alembic, FastAPI (существующий стек, без
новых зависимостей).

## Global Constraints

- **Хард-гейты не затрагиваются.** Эта фича — чисто наблюдательная (append-only лог),
  ни денежный, ни редактурный гейт в её коде не участвуют. Ни `content_critic`-style
  риска здесь нет вообще: фича не пишет в `Domain.status`/`Page.status`, только
  создаёт НОВЫЕ строки в НОВОЙ таблице.
- **Не логировать `recheck_acquirability()`** — она не зовёт `score_domain()`/
  `_funnel()`, другая форма данных, вне скоупа (design doc, «Что НЕ делаем»).
- **Без retention/TTL** в этой итерации — объём мал (рескор не чаще раза в сутки на
  домен).
- Дизайн — `docs/superpowers/specs/2026-07-18-domain-score-log-design.md` (полный
  контекст решений, не пересказывать в задачах).
- Русский язык в UI/сообщениях/комментариях, как везде в проекте.

---

### Task 1: Модель, миграция, запись из `score_domain()`

**Files:**
- Create: `backend/app/models/domain_score_log.py`
- Create: `backend/alembic/versions/0024_domain_score_log.py`
- Modify: `backend/app/services/scoring.py` (функция `score_domain()`, строки ~556-676)
- Test: `backend/tests/test_domain_score_log.py`

**Interfaces:**
- Produces: `DomainScoreLog` (SQLAlchemy модель, `backend/app/models/domain_score_log.py`)
  — колонки `id`, `domain_id: int` (FK `domains.id`), `run_id: int | None` (FK
  `job_run.id`), `outcome: str` (`'unresolved'|'rejected'|'scored'`),
  `reject_reason: str | None`, `score: float | None`, `sig: dict` (JSONB),
  `created_at: datetime`.
- Consumes: `app.models.domain.Domain` (`domains.id`) и `app.models.job.JobRun`
  (`job_run.id`) — оба уже существуют, только читаются как FK-таргеты, не меняются.
- Consumes/Produces внутри `scoring.py`: `score_domain()` уже существует (сигнатура
  `score_domain(domain_id, clients=None, whois_budget=None, ahrefs_budget=None, run:
  int | None = None) -> dict` — НЕ МЕНЯЕТСЯ), эта задача добавляет запись в
  `domain_score_log` на каждом из трёх `return`, ничего не меняя в возвращаемом
  словаре и не меняя существующую логику принятия решений.

- [ ] **Step 1: Написать модель `DomainScoreLog`**

Создать `backend/app/models/domain_score_log.py`:

```python
"""Append-only история решений scoring.score_domain() по домену. Каждый прогон
воронки (T0-T3) добавляет НОВУЮ строку — Domain.score_breakdown остаётся "последний
снимок" для UI-бейджей, эта таблица хранит историю ВСЕХ прогонов, не только
последнего. Расширяет уже работающий паттерн job_run (кросс-процессный реестр в
PostgreSQL), не новая инфраструктура. См.
docs/superpowers/specs/2026-07-18-domain-score-log-design.md."""
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class DomainScoreLog(Base):
    __tablename__ = "domain_score_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    domain_id: Mapped[int] = mapped_column(ForeignKey("domains.id"), index=True)
    # nullable: score_domain() бывает вызван вне трекнутого job_run (ручной "▶" на
    # одном домене из /domains, см. panel.py POST /domains/{domain_id}/score).
    run_id: Mapped[int | None] = mapped_column(ForeignKey("job_run.id"), nullable=True)
    outcome: Mapped[str] = mapped_column(String(16))          # unresolved|rejected|scored
    reject_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    sig: Mapped[dict] = mapped_column(JSONB)                  # полный снимок sig из _funnel()
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("ix_domain_score_log_domain_created", "domain_id", "created_at"),
    )
```

- [ ] **Step 2: Написать миграцию**

Прочитай `backend/alembic/versions/0023_page_critic.py` (создана в этой же сессии,
самая свежая — точная актуальная конвенция) ПЕРЕД этим шагом за формой докстринга/
`revision`/`down_revision`. Создать `backend/alembic/versions/0024_domain_score_log.py`:

```python
"""domain_score_log — append-only история решений score_domain()"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0024_domain_score_log"
down_revision = "0023_page_critic"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "domain_score_log",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("domain_id", sa.Integer(), sa.ForeignKey("domains.id"), nullable=False),
        sa.Column("run_id", sa.Integer(), sa.ForeignKey("job_run.id"), nullable=True),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("reject_reason", sa.String(32), nullable=True),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("sig", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_domain_score_log_domain_created", "domain_score_log",
                    ["domain_id", "created_at"])


def downgrade():
    op.drop_index("ix_domain_score_log_domain_created", table_name="domain_score_log")
    op.drop_table("domain_score_log")
```

Сверься сам с реальной формой `down_revision` в `0023_page_critic.py` — если там
`down_revision = "0022_offer_settings"`, значит `0023_page_critic` — текущая голова,
и `0024`-миграция выше указывает на нее верно; если в репозитории уже появилась
более новая миграция после `0023`, поправь `down_revision` на неё.

- [ ] **Step 3: Написать падающий тест на запись в `score_domain()`**

`backend/tests/test_domain_score_log.py`:

```python
"""Append-only история решений score_domain() (domain_score_log). Каждый прогон
воронки добавляет НОВУЮ строку, ничего не перезаписывает — это и есть смысл фичи,
см. docs/superpowers/specs/2026-07-18-domain-score-log-design.md."""
from types import SimpleNamespace

from sqlalchemy import select

import app.db as db
from app.models.domain import Domain
from app.models.domain_score_log import DomainScoreLog


def _seed_domain(**kw) -> int:
    with db.SessionLocal() as s:
        d = Domain(domain="scorelog.ru", source="backorder", status="discovered", **kw)
        s.add(d); s.commit(); s.refresh(d)
        return d.id


def test_score_domain_logs_rejected_outcome(monkeypatch):
    from app.services import scoring
    monkeypatch.setattr(scoring, "_funnel", lambda d, c, st, sig, *a, **kw: "low_rd")
    monkeypatch.setattr(scoring, "_make_clients", lambda: {})
    did = _seed_domain()
    scoring.score_domain(did)
    with db.SessionLocal() as s:
        rows = s.execute(select(DomainScoreLog).where(
            DomainScoreLog.domain_id == did)).scalars().all()
        assert len(rows) == 1
        assert rows[0].outcome == "rejected"
        assert rows[0].reject_reason == "low_rd"
        assert rows[0].score == 0.0


def test_score_domain_logs_unresolved_outcome(monkeypatch):
    from app.services import scoring

    def _fake_funnel(d, c, st, sig, *a, **kw):
        sig["acquirability_unresolved"] = True
        sig["unresolved_why"] = "waiting"
        return None
    monkeypatch.setattr(scoring, "_funnel", _fake_funnel)
    monkeypatch.setattr(scoring, "_make_clients", lambda: {})
    did = _seed_domain()
    scoring.score_domain(did)
    with db.SessionLocal() as s:
        rows = s.execute(select(DomainScoreLog).where(
            DomainScoreLog.domain_id == did)).scalars().all()
        assert len(rows) == 1
        assert rows[0].outcome == "unresolved"
        assert rows[0].score is None


def test_repeated_scoring_appends_not_overwrites(monkeypatch):
    """РЕГРЕССИЯ — прямая проверка смысла фичи: второй вызов score_domain() на том
    же домене добавляет ВТОРУЮ строку, не перезаписывает первую."""
    from app.services import scoring
    monkeypatch.setattr(scoring, "_funnel", lambda d, c, st, sig, *a, **kw: "low_rd")
    monkeypatch.setattr(scoring, "_make_clients", lambda: {})
    did = _seed_domain()
    scoring.score_domain(did)
    with db.SessionLocal() as s:
        d = s.get(Domain, did)
        d.status = "discovered"          # score_domain() судит только discovered/scored/rejected
        s.commit()
    scoring.score_domain(did)
    with db.SessionLocal() as s:
        rows = s.execute(select(DomainScoreLog).where(
            DomainScoreLog.domain_id == did)).scalars().all()
        assert len(rows) == 2
```

Прочитай сам актуальный `scoring.py` перед этим шагом — сигнатура `_funnel(d, c, st,
sig, whois_budget=None, ahrefs_budget=None, run=None)` и `_make_clients()` должны
совпасть с реальными именами/сигнатурами в файле (см. Task Interfaces выше); если
`_make_clients` называется иначе — адаптируй монки-патч под реальное имя.

- [ ] **Step 4: Запустить тест, убедиться что падает**

Run: `.venv/bin/python -m pytest backend/tests/test_domain_score_log.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.models.domain_score_log'`
(или падение на отсутствии строк в `domain_score_log`, если модель уже существует к
этому моменту прогона, но запись в `score_domain()` ещё не добавлена)

- [ ] **Step 5: Добавить запись в `score_domain()`**

Прочитай сам `backend/app/services/scoring.py` целиком в районе `score_domain()`
(строки ~556-676 на момент написания этого плана) — вот три точки, куда нужно
вставить запись. Импорт `DomainScoreLog` — рядом с существующими локальными
импортами в начале функции (`from app.models.domain import Domain` и т.п.).

**Путь 1 — unresolved** (сейчас строки ~579-597, `if sig.get("acquirability_unresolved"):`
блок, ПЕРЕД `return {"domain": d.domain, "status": d.status, "unresolved": True, ...}`):

```python
        if sig.get("acquirability_unresolved"):
            if sig.get("acquirability_checked_at"):
                d.acquirability_checked_at = sig["acquirability_checked_at"]
            db.add(DomainScoreLog(domain_id=d.id, run_id=run, outcome="unresolved",
                                  reject_reason=None, score=None, sig=sig))
            db.commit()
            return {"domain": d.domain, "status": d.status, "unresolved": True,
                    "why": sig.get("unresolved_why"), "errors": sig.get("errors", [])}
```

(объединяет существующий `if sig.get("acquirability_checked_at"): d.commit()` с
новой записью в ОДИН commit — не два подряд коммита без причины; удали старый
отдельный `db.commit()` внутри вложенного if, если он там был написан отдельной
строкой — смотри реальный текущий код).

**Путь 2/3 — rejected/scored** (в конце функции, сейчас строки ~667-676, СРАЗУ ПЕРЕД
финальным `db.commit()`):

```python
        d.status = result["status"]
        d.reject_reason = reject or ("low_score" if result["status"] == "rejected" else None)
        d.scored_at = datetime.now(timezone.utc)
        db.add(DomainScoreLog(
            domain_id=d.id, run_id=run,
            outcome="rejected" if result["status"] == "rejected" else "scored",
            reject_reason=d.reject_reason, score=result["score"], sig=sig))
        db.commit()
        return {"domain": d.domain, **result, "reject_reason": d.reject_reason,
                "errors": sig.get("errors", [])}
```

(вставляется ПОСЛЕ уже существующих строк, которые ставят `d.status`/
`d.reject_reason`/`d.scored_at` — сам код этих строк НЕ меняется, только добавляется
`db.add(DomainScoreLog(...))` перед уже существующим `db.commit()`.)

Убедись, что `DomainScoreLog` импортирован в начале функции `score_domain()`:
```python
    from app.models.domain_score_log import DomainScoreLog
```

- [ ] **Step 6: Запустить тест, убедиться что проходит**

Run: `.venv/bin/python -m pytest backend/tests/test_domain_score_log.py -v`
Expected: 3 passed

- [ ] **Step 7: Прогнать полный сьют + pyflakes**

Run:
```
.venv/bin/python -m pytest backend/tests/ -q
.venv/bin/python -m pyflakes backend/app backend/tests
```
Узнай текущий baseline фактическим прогоном ПЕРЕД своими изменениями (не полагайся
на число из этого плана — оно устареет). Ожидаемый итог: весь сьют зелёный (baseline
+ 3 новых теста), pyflakes чист. Проверь миграцию тем же способом, что использовался
для `0023_page_critic` в этой же сессии (ast.parse/importlib на файл миграции — реальный
`alembic upgrade head` недоступен без docker на этой машине; опиши в отчёте, как
проверил).

- [ ] **Step 8: Commit**

```bash
git add backend/app/models/domain_score_log.py backend/alembic/versions/0024_domain_score_log.py \
       backend/app/services/scoring.py backend/tests/test_domain_score_log.py
git commit -F - <<'EOF'
feat(domain_score_log): история решений score_domain() — append-only лог (задача 1)

Каждый прогон воронки (unresolved/rejected/scored) добавляет строку в новую таблицу,
не перезаписывает Domain.score_breakdown. Расширяет паттерн job_run, не новая
инфраструктура. Хард-гейты не затронуты — чисто наблюдательный лог.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
```

---

### Task 2: Read-эндпоинт

**Files:**
- Modify: `backend/app/api/panel.py` (новый роут, рядом с `GET /api/jobs/live`)
- Test: `backend/tests/test_domain_score_log.py` (добавить HTTP-уровневые тесты в тот
  же файл, что Task 1)

**Interfaces:**
- Consumes: `app.models.domain_score_log.DomainScoreLog` из Task 1 (колонки
  зафиксированы, не меняются).
- Produces: `GET /api/domains/{domain_id}/score-history?limit=50` — JSON-массив,
  `created_at DESC`, поля `id`/`run_id`/`outcome`/`reject_reason`/`score`/`sig`/
  `created_at` (сериализация через `jsonable_encoder`, тот же паттерн, что
  `/api/jobs/live`).

- [ ] **Step 1: Написать падающий HTTP-тест**

Добавить в `backend/tests/test_domain_score_log.py`:

```python
def test_score_history_endpoint_returns_newest_first(client):
    from app.services import scoring
    import time as _t
    did = _seed_domain()
    with db.SessionLocal() as s:
        s.add(DomainScoreLog(domain_id=did, run_id=None, outcome="rejected",
                             reject_reason="low_rd", score=0.0, sig={"a": 1}))
        s.commit()
    with db.SessionLocal() as s:
        s.add(DomainScoreLog(domain_id=did, run_id=None, outcome="scored",
                             reject_reason=None, score=0.8, sig={"b": 2}))
        s.commit()
    r = client.get(f"/api/domains/{did}/score-history")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 2
    assert body[0]["outcome"] == "scored"     # новая запись первая
    assert body[1]["outcome"] == "rejected"


def test_score_history_endpoint_empty_for_domain_without_history(client):
    did = _seed_domain()
    r = client.get(f"/api/domains/{did}/score-history")
    assert r.status_code == 200
    assert r.json() == []


def test_score_history_endpoint_respects_limit(client):
    did = _seed_domain()
    with db.SessionLocal() as s:
        for i in range(3):
            s.add(DomainScoreLog(domain_id=did, run_id=None, outcome="rejected",
                                 reject_reason="low_rd", score=0.0, sig={}))
        s.commit()
    r = client.get(f"/api/domains/{did}/score-history?limit=2")
    assert len(r.json()) == 2
```

Прочитай `backend/tests/conftest.py` за фикстурой `client` (используется во всех
панельных HTTP-тестах, например `test_panel_toctou.py`) — импортируй её так же, как
там (обычно фикстура не требует явного импорта, только присутствия в сигнатуре
теста, но сверься сам).

- [ ] **Step 2: Запустить, убедиться что падает**

Run: `.venv/bin/python -m pytest backend/tests/test_domain_score_log.py -v`
Expected: FAIL — 404 (роута ещё нет)

- [ ] **Step 3: Добавить роут**

Прочитай `backend/app/api/panel.py` в районе `GET /api/jobs/live` (строки ~662-671 на
момент написания этого плана) за точным стилем. Добавить рядом:

```python
@router.get("/api/domains/{domain_id}/score-history")
def domain_score_history(domain_id: int, limit: int = 50):
    """История решений score_domain() по домену — новые сверху. Append-only лог
    (domain_score_log), не перезаписывается на рескоре, в отличие от
    Domain.score_breakdown (последний снимок для UI-бейджей)."""
    from fastapi.responses import JSONResponse
    from sqlalchemy import select
    from app.db import SessionLocal
    from app.models.domain_score_log import DomainScoreLog

    with SessionLocal() as db:
        rows = db.execute(
            select(DomainScoreLog).where(DomainScoreLog.domain_id == domain_id)
            .order_by(DomainScoreLog.created_at.desc()).limit(limit)
        ).scalars().all()
        return JSONResponse(jsonable_encoder([{
            "id": r.id, "run_id": r.run_id, "outcome": r.outcome,
            "reject_reason": r.reject_reason, "score": r.score, "sig": r.sig,
            "created_at": r.created_at,
        } for r in rows]))
```

`jsonable_encoder` уже импортирован в файле (используется `/api/jobs/live` рядом) —
сверься сам, что импорт есть вверху файла, не дублируй.

- [ ] **Step 4: Запустить тесты**

Run: `.venv/bin/python -m pytest backend/tests/test_domain_score_log.py -v`
Expected: 6 passed (3 из Task 1 + 3 новых)

- [ ] **Step 5: Прогнать полный сьют + pyflakes**

Run:
```
.venv/bin/python -m pytest backend/tests/ -q
.venv/bin/python -m pyflakes backend/app backend/tests
```
Expected: весь сьют зелёный, pyflakes чист.

- [ ] **Step 6: Commit**

```bash
git add backend/app/api/panel.py backend/tests/test_domain_score_log.py
git commit -F - <<'EOF'
feat(panel): GET /api/domains/{id}/score-history — read-эндпоинт domain_score_log (задача 2)

Тот же паттерн, что /api/jobs/live: JSON-список, новые записи сверху, limit по
умолчанию 50.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
```

---

## После обеих задач

Финальное whole-branch ревью через `combine-reviewer` — проверить: хард-гейты вне
скоупа (подтвердить факт grep'ом — ни `confirm_order`/`execute_confirmed_order`/
`mark_caught`/`mark_edited` в диффе), `score_domain()`'s существующая логика решений
НЕ изменена (только добавлены `db.add(DomainScoreLog(...))` перед уже существующими
`db.commit()`), append-only гарантия реально держит (тест `test_repeated_scoring_appends_not_overwrites`
доказывает).
