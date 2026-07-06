# Умная воронка discovery + прогресс + версия — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Переделать M1 в многоступенчатую воронку (дёшево→дорого: whois-возраст → риск → Wayback только для лучших) с рантайм-порогами и /settings UI, добавить плагинные источники дропов, прогресс длинных задач и блок версии.

**Architecture:** Скоринг остаётся: чистая `compute_score` + оркестрация в `score_domain`, но оркестрация становится ступенчатой с ранним выходом (записывает `reject_reason`, дорогие клиенты не трогаются для отсеянных). Discovery — плагинные адаптеры источников с дедупом. Пороги живут в single-row таблице `scoring_settings`, редактируются на `/settings`. Длинные задачи гоняются в фоновом потоке с in-memory реестром прогресса, панель поллит JSON. Версия читается из git в контейнере.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.x, Pydantic v2, Alembic, Jinja2, httpx. Тесты — pytest на оффлайн SQLite (`backend/tests/conftest.py`), сеть мокается.

## Global Constraints

- **Хард-гейты не трогать:** M2 деньги (`confirmed_by_human`), M4 редактура (публикация только из `edited`). Воронка — это M1, гейтов не касается.
- **`compute_score` остаётся чистой** (без I/O), её сигнатура и unit-тесты не меняются. Ступени/ранний выход — только в `score_domain`.
- **Дорогой шаг T3 (Wayback) — ТОЛЬКО для выживших T0–T2.** Это ядро экономии; регрессия должна доказывать, что отсеянный на T1/T2 домен не дошёл до Wayback.
- **Тесты оффлайн:** SQLite-харнесс `backend/tests/conftest.py`; любая сеть мокается через monkeypatch. Прогон: `.venv/bin/python -m pytest backend/tests/ -q` из корня репо. pyflakes чистый: `.venv/bin/python -m pyflakes backend/app backend/tests`.
- **Новые модели регистрируются в conftest** (`import app.models.<name>` + добавить в `_REGISTER_TABLES`), иначе `create_all` не создаст таблицу в тестах.
- **Панель — светлая CMS, принцип шильдика:** каждый контрол подписан тем, что он делает; русский UI; CSS-классы из `base.html` (`.station/.plate/.what/.go/.card/.btn/.btn-amber/.badge/.b-*/.led/.flash/.rl`). Новых глобальных стилей по минимуму.
- **Безопасность панели не ослаблять:** CSRF same-origin guard и Basic-auth в `main.py` остаются; новые POST-роуты автоматически под ними.
- **reject_reason enum:** `low_rd | feed_flag | too_young | rkn | blacklist | history_dirty | low_score`.
- **sources_enabled ключи:** `backorder | cctld | reg_ru | sweb`.

---

## Файловая карта

- `backend/app/models/domain.py` — +колонки `reject_reason`, `whois_created`, `feed_flags` (Task 1).
- `backend/app/models/settings.py` — новая модель `ScoringSettings` (Task 1).
- `backend/app/services/settings.py` — get/update/reset настроек (Task 1).
- `backend/app/services/scoring_config.py` — +дефолты `MIN_AGE_YEARS`, `SOURCES_ENABLED` (Task 1).
- `backend/alembic/versions/0002_funnel.py` — миграция (Task 1).
- `backend/app/integrations/aparser.py` — +`whois_created()` + парсер даты (Task 2).
- `backend/app/services/scoring.py` — рефактор `score_domain` в воронку (Task 3).
- `backend/app/integrations/cctld.py`, `regru_drops.py`, `sweb_drops.py` — источники (Task 4).
- `backend/app/services/discovery.py` — мультиисточник + дедуп + feed_flags (Task 4).
- `backend/app/api/panel.py` — роуты /settings, /run/* async, /admin/check-updates (Tasks 5,6,7).
- `backend/app/templates/settings.html` — экран настроек (Task 5); `base.html` — пункт сайдбара (Task 5).
- `backend/app/services/jobs.py` — реестр прогресса (Task 6); `domains.html` — JS-полоса (Task 6).
- `backend/app/services/version.py` — версия из git (Task 7); `diag.html` — блок версии (Task 7).
- `backend/tests/test_funnel.py`, `test_sources.py`, `test_settings.py`, `test_jobs.py`, `test_version.py` — новые тесты.

---

## Task 1: Модель данных + настройки

**Files:**
- Modify: `backend/app/models/domain.py` (класс `Domain`: +3 колонки)
- Modify: `backend/app/services/scoring_config.py` (+2 дефолта)
- Create: `backend/app/models/settings.py`
- Create: `backend/app/services/settings.py`
- Create: `backend/alembic/versions/0002_funnel.py`
- Modify: `backend/tests/conftest.py` (регистрация новой модели)
- Test: `backend/tests/test_settings.py`

**Interfaces:**
- Produces: `Domain.reject_reason: str|None`, `Domain.whois_created: datetime|None`, `Domain.feed_flags: dict|None`.
- Produces: `app.services.settings.get_settings() -> dict` с ключами `min_referring_domains:int, min_age_years:float, approve_at:float, manual_review_at:float, sources_enabled:dict`.
- Produces: `app.services.settings.update_settings(**kw) -> dict`, `reset_settings() -> dict`.

- [ ] **Step 1: Добавить дефолты в scoring_config.py**

В конец `backend/app/services/scoring_config.py`:
```python
# Дефолты для рантайм-настроек (services/settings.py сидит из них при первом обращении).
MIN_AGE_YEARS = 3.0                                          # T1 whois-гейт: моложе — reject too_young
SOURCES_ENABLED = {"backorder": True, "cctld": True, "reg_ru": True, "sweb": True}
```

- [ ] **Step 2: Добавить колонки в Domain**

В `backend/app/models/domain.py`, класс `Domain`, после блока `# decision (Stage F)` (рядом с `notes`):
```python
    # funnel bookkeeping
    reject_reason: Mapped[str | None] = mapped_column(String(32))    # low_rd|feed_flag|too_young|rkn|blacklist|history_dirty|low_score
    whois_created: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # дата регистрации (первичный возраст)
    feed_flags: Mapped[dict | None] = mapped_column(JSONB)           # сырые флаги источника: {rkn, judicial, block}
```

- [ ] **Step 3: Создать модель ScoringSettings**

`backend/app/models/settings.py`:
```python
"""Рантайм-настройки скоринга (single-row, id=1). Дефолты — в scoring_config.py.

Пороги воронки редактируются на /settings; сервис settings.py читает/пишет эту строку.
"""
from datetime import datetime
from sqlalchemy import Integer, Numeric, DateTime, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from app.db import Base


class ScoringSettings(Base):
    __tablename__ = "scoring_settings"

    id: Mapped[int] = mapped_column(primary_key=True)                # всегда 1
    min_referring_domains: Mapped[int] = mapped_column(Integer, default=1)
    min_age_years: Mapped[float] = mapped_column(Numeric, default=3.0)
    approve_at: Mapped[float] = mapped_column(Numeric, default=0.70)
    manual_review_at: Mapped[float] = mapped_column(Numeric, default=0.40)
    sources_enabled: Mapped[dict] = mapped_column(JSONB, default=dict)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True),
                                                        server_default=func.now(), onupdate=func.now())
```

- [ ] **Step 4: Сервис settings.py**

`backend/app/services/settings.py`:
```python
"""Рантайм-настройки воронки: читать/писать single-row scoring_settings.

get_settings() возвращает effective-словарь (сидит дефолтами из scoring_config при
отсутствии строки). Пороги валидируются по диапазонам, чтобы UI не записал мусор.
"""
from app.services import scoring_config as cfg

_KEYS_NUM = ("min_referring_domains", "min_age_years", "approve_at", "manual_review_at")
_BOUNDS = {                       # (min, max) для валидации ползунков
    "min_referring_domains": (0, 100000),
    "min_age_years": (0.0, 30.0),
    "approve_at": (0.0, 1.0),
    "manual_review_at": (0.0, 1.0),
}


def _defaults() -> dict:
    return {
        "min_referring_domains": cfg.PREFILTER["min_referring_domains"],
        "min_age_years": cfg.MIN_AGE_YEARS,
        "approve_at": cfg.DECISION["approve_at"],
        "manual_review_at": cfg.DECISION["manual_review_at"],
        "sources_enabled": dict(cfg.SOURCES_ENABLED),
    }


def _row(db):
    """Вернуть (создав при отсутствии) строку scoring_settings id=1, засеянную дефолтами."""
    from app.models.settings import ScoringSettings
    row = db.get(ScoringSettings, 1)
    if row is None:
        d = _defaults()
        row = ScoringSettings(id=1, **d)
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


def get_settings() -> dict:
    from app.db import SessionLocal
    with SessionLocal() as db:
        r = _row(db)
        return {
            "min_referring_domains": int(r.min_referring_domains),
            "min_age_years": float(r.min_age_years),
            "approve_at": float(r.approve_at),
            "manual_review_at": float(r.manual_review_at),
            "sources_enabled": dict(r.sources_enabled or cfg.SOURCES_ENABLED),
        }


def update_settings(**kw) -> dict:
    """Записать переданные ключи с валидацией диапазонов. Неизвестные ключи игнор."""
    from app.db import SessionLocal
    with SessionLocal() as db:
        r = _row(db)
        for k in _KEYS_NUM:
            if k in kw and kw[k] is not None:
                lo, hi = _BOUNDS[k]
                v = max(lo, min(hi, type(lo)(kw[k])))
                setattr(r, k, v)
        if "sources_enabled" in kw and isinstance(kw["sources_enabled"], dict):
            r.sources_enabled = {s: bool(kw["sources_enabled"].get(s, False))
                                 for s in cfg.SOURCES_ENABLED}
        db.commit()
    return get_settings()


def reset_settings() -> dict:
    from app.db import SessionLocal
    with SessionLocal() as db:
        r = _row(db)
        for k, v in _defaults().items():
            setattr(r, k, v)
        db.commit()
    return get_settings()
```

- [ ] **Step 5: Миграция 0002**

`backend/alembic/versions/0002_funnel.py`:
```python
"""funnel: reject_reason/whois_created/feed_flags + scoring_settings

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-06
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("domains", sa.Column("reject_reason", sa.String(32)))
    op.add_column("domains", sa.Column("whois_created", sa.DateTime(timezone=True)))
    op.add_column("domains", sa.Column("feed_flags", postgresql.JSONB()))
    op.create_table(
        "scoring_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("min_referring_domains", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("min_age_years", sa.Numeric(), nullable=False, server_default="3.0"),
        sa.Column("approve_at", sa.Numeric(), nullable=False, server_default="0.70"),
        sa.Column("manual_review_at", sa.Numeric(), nullable=False, server_default="0.40"),
        sa.Column("sources_enabled", postgresql.JSONB()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.execute(
        "INSERT INTO scoring_settings (id, sources_enabled) VALUES "
        "(1, '{\"backorder\": true, \"cctld\": true, \"reg_ru\": true, \"sweb\": true}')"
    )


def downgrade() -> None:
    op.drop_table("scoring_settings")
    op.drop_column("domains", "feed_flags")
    op.drop_column("domains", "whois_created")
    op.drop_column("domains", "reject_reason")
```

- [ ] **Step 6: Зарегистрировать модель в conftest**

В `backend/tests/conftest.py` рядом с другими импортами моделей добавить `import app.models.settings` и включить в кортеж:
```python
import app.models.settings
_REGISTER_TABLES = (app.models.domain, app.models.site, app.models.offer,
                    app.models.monitoring, app.models.settings)
```

- [ ] **Step 7: Тест настроек**

`backend/tests/test_settings.py`:
```python
"""Рантайм-настройки воронки: сид дефолтов, обновление с валидацией, сброс."""
from app.services import settings as st
from app.services import scoring_config as cfg


def test_get_settings_seeds_defaults():
    s = st.get_settings()
    assert s["min_age_years"] == cfg.MIN_AGE_YEARS
    assert s["approve_at"] == cfg.DECISION["approve_at"]
    assert s["sources_enabled"] == cfg.SOURCES_ENABLED


def test_update_and_reset():
    st.update_settings(min_age_years=5, approve_at=0.8,
                       sources_enabled={"backorder": True, "cctld": False, "reg_ru": False, "sweb": False})
    s = st.get_settings()
    assert s["min_age_years"] == 5.0 and s["approve_at"] == 0.8
    assert s["sources_enabled"]["cctld"] is False
    st.reset_settings()
    assert st.get_settings()["min_age_years"] == cfg.MIN_AGE_YEARS


def test_update_clamps_out_of_range():
    st.update_settings(approve_at=9.9, min_age_years=-4)
    s = st.get_settings()
    assert s["approve_at"] == 1.0 and s["min_age_years"] == 0.0
```

- [ ] **Step 8: Прогон + коммит**

Run: `.venv/bin/python -m pytest backend/tests/test_settings.py -q` → PASS. Затем весь сьют + pyflakes.
```bash
git add backend/app/models/domain.py backend/app/models/settings.py backend/app/services/settings.py backend/app/services/scoring_config.py backend/alembic/versions/0002_funnel.py backend/tests/conftest.py backend/tests/test_settings.py
git commit -m "M1: scoring_settings + funnel-колонки domains (reject_reason/whois_created/feed_flags)"
```

---

## Task 2: whois в A-Parser (возраст домена)

**Files:**
- Modify: `backend/app/integrations/aparser.py` (+`whois_created`, +`_parse_whois_created`)
- Test: `backend/tests/test_sources.py` (создать; whois-часть)

**Interfaces:**
- Produces: `AParserClient.whois_created(domain: str) -> datetime|None` (tz-aware UTC).
- Produces: `app.integrations.aparser._parse_whois_created(text: str) -> datetime|None` (чистая, для тестов).

- [ ] **Step 1: Тест парсера whois-даты**

`backend/tests/test_sources.py`:
```python
"""Парсеры источников дропов + whois-даты. Оффлайн, на фикстурах-строках."""
from datetime import timezone
from app.integrations.aparser import _parse_whois_created


def test_whois_created_ru():
    txt = "domain: EXAMPLE.RU\ncreated: 2010.11.15\npaid-till: 2026.11.15\n"
    d = _parse_whois_created(txt)
    assert d is not None and (d.year, d.month, d.day) == (2010, 11, 15)
    assert d.tzinfo == timezone.utc


def test_whois_created_gtld():
    txt = "Domain Name: EXAMPLE.COM\nCreation Date: 2004-03-15T05:00:00Z\n"
    d = _parse_whois_created(txt)
    assert (d.year, d.month, d.day) == (2004, 3, 15)


def test_whois_created_junk_is_none():
    assert _parse_whois_created("no date here at all") is None
    assert _parse_whois_created("") is None
```

- [ ] **Step 2: Прогон — падает (нет функции)**

Run: `.venv/bin/python -m pytest backend/tests/test_sources.py -q`
Expected: FAIL (ImportError `_parse_whois_created`).

- [ ] **Step 3: Реализация парсера + метода**

В `backend/app/integrations/aparser.py` добавить импорты вверху и функции:
```python
import re
from datetime import datetime, timezone

# .ru/.рф (TCI): 'created: 2010.11.15'; gTLD: 'Creation Date: 2004-03-15T...'
_RE_RU = re.compile(r"created:\s*(\d{4})\.(\d{2})\.(\d{2})", re.I)
_RE_GTLD = re.compile(r"creation date:\s*(\d{4})-(\d{2})-(\d{2})", re.I)


def _parse_whois_created(text: str) -> datetime | None:
    """Дата регистрации из whois-ответа (.ru или gTLD). Самая ранняя найденная, UTC. None если нет."""
    found = []
    for rx in (_RE_RU, _RE_GTLD):
        for y, mo, dy in rx.findall(text or ""):
            try:
                found.append(datetime(int(y), int(mo), int(dy), tzinfo=timezone.utc))
            except ValueError:
                pass
    return min(found) if found else None
```
И метод в классе `AParserClient`:
```python
    def whois_created(self, domain: str) -> datetime | None:
        """Дата регистрации домена через Net::Whois (дёшево). None если whois не отдал дату."""
        res = self._call("oneRequest", {"query": domain, "parser": "Net::Whois",
                                        "configPreset": "default", "preset": "default"})
        return _parse_whois_created(self._result_string(res))
```

- [ ] **Step 4: Прогон — проходит**

Run: `.venv/bin/python -m pytest backend/tests/test_sources.py -q` → PASS (whois-тесты).

- [ ] **Step 5: Коммит**

```bash
git add backend/app/integrations/aparser.py backend/tests/test_sources.py
git commit -m "M1: A-Parser whois_created — возраст домена из даты регистрации"
```

---

## Task 3: Рефактор скоринга в воронку T0–T3

**Files:**
- Modify: `backend/app/services/scoring.py` (`_make_clients`, `score_domain`; новый `_funnel`; удалить `_gather_signals`)
- Test: `backend/tests/test_funnel.py` (создать)

**Interfaces:**
- Consumes: `settings.get_settings()` (Task 1), `AParserClient.whois_created` (Task 2), `Domain.feed_flags/whois_created/reject_reason` (Task 1).
- Produces: `score_domain(domain_id, clients=None) -> dict` (+`reject_reason`), `_make_clients() -> dict` (с ключом `aparser`), `_funnel(d, clients, st, sig) -> str|None`.
- Не меняет: `compute_score`, `score_pending(limit)`.

- [ ] **Step 1: Тесты воронки**

`backend/tests/test_funnel.py`:
```python
"""Воронка скоринга дёшево→дорого: ранний выход, reject_reason, дорогой Wayback только для выживших."""
from datetime import datetime, timezone, timedelta
import app.db as db
from app.models.domain import Domain
from app.services import scoring


def _mk(**kw):
    with db.SessionLocal() as s:
        d = Domain(domain=kw.pop("domain", "x.ru"), source="cctld", status="discovered", **kw)
        s.add(d); s.commit(); s.refresh(d)
        return d.id


class _Wayback:
    def __init__(self): self.calls = 0
    def classify_history(self, domain):
        self.calls += 1
        return {"prior_flags": {c: False for c in ("adult", "pharma", "casino", "gambling", "spam")},
                "first_seen": None, "age_years": 9.0, "wayback_checked": True, "sampled": 5}


def _clients(whois_dt, wb, rkn=False, bl=False):
    class _W:  # aparser
        def whois_created(self, dom): return whois_dt
    class _R:
        def is_listed(self, dom): return rkn
    class _B:
        def is_blacklisted(self, dom): return bl
    class _S:
        def indexed_echo(self, dom): return True
    return {"aparser": _W(), "rkn": _R(), "blacklist": _B(), "searxng": _S(),
            "wayback": wb, "opr": None}


def test_too_young_rejects_before_wayback():
    did = _mk(domain="young.ru", referring_domains=5)
    wb = _Wayback()
    young = datetime.now(timezone.utc) - timedelta(days=365)   # 1 год
    out = scoring.score_domain(did, clients=_clients(young, wb))
    assert out["status"] == "rejected" and out["reject_reason"] == "too_young"
    assert wb.calls == 0            # ЯДРО: дорогой Wayback НЕ вызван для молодого домена


def test_feed_flag_rejects_first():
    did = _mk(domain="blocked.ru", referring_domains=50, feed_flags={"rkn": True})
    wb = _Wayback()
    out = scoring.score_domain(did, clients=_clients(None, wb))
    assert out["reject_reason"] == "feed_flag" and wb.calls == 0


def test_low_rd_rejects():
    did = _mk(domain="thin.ru", referring_domains=0)
    wb = _Wayback()
    from app.services import settings as st
    st.update_settings(min_referring_domains=1)
    out = scoring.score_domain(did, clients=_clients(None, wb))
    assert out["reject_reason"] == "low_rd" and wb.calls == 0


def test_rkn_rejects_before_wayback():
    did = _mk(domain="rkn.ru", referring_domains=50)
    wb = _Wayback()
    old = datetime.now(timezone.utc) - timedelta(days=365 * 8)
    out = scoring.score_domain(did, clients=_clients(old, wb, rkn=True))
    assert out["reject_reason"] == "rkn" and wb.calls == 0


def test_whois_none_falls_through_to_wayback_age():
    did = _mk(domain="nowhois.ru", referring_domains=3000)
    wb = _Wayback()
    out = scoring.score_domain(did, clients=_clients(None, wb))   # whois не отдал дату
    assert wb.calls == 1                                          # дошли до T3
    assert out["status"] in ("approved", "scored")               # чистый сильный домен
    with db.SessionLocal() as s:
        d = s.get(Domain, did)
    assert float(d.age_years) == 9.0                             # возраст — фолбэком из Wayback


def test_clean_strong_domain_approved():
    did = _mk(domain="good.ru", referring_domains=3000)
    wb = _Wayback()
    old = datetime.now(timezone.utc) - timedelta(days=365 * 9)
    out = scoring.score_domain(did, clients=_clients(old, wb))
    assert wb.calls == 1 and out["status"] == "approved" and out["reject_reason"] is None
```

- [ ] **Step 2: Прогон — падает**

Run: `.venv/bin/python -m pytest backend/tests/test_funnel.py -q`
Expected: FAIL (score_domain пока без reject_reason / ступеней).

- [ ] **Step 3: Переписать оркестрацию в scoring.py**

Заменить `_make_clients` (добавить aparser), УДАЛИТЬ `_gather_signals`, заменить тело `score_domain` на воронку. `compute_score`, `score_pending` НЕ трогать. Полный вид:
```python
def _make_clients() -> dict:
    """Собрать интеграционные клиенты один раз на прогон (переиспользуются между доменами)."""
    from app.config import settings
    from app.integrations.wayback import WaybackClient
    from app.integrations.rkn import RknClient
    from app.integrations.blacklist import BlacklistClient
    from app.integrations.searxng import SearxngClient
    from app.integrations.openpagerank import OpenPageRankClient
    from app.integrations.aparser import AParserClient
    return {
        "wayback": WaybackClient(), "rkn": RknClient(), "blacklist": BlacklistClient(),
        "searxng": SearxngClient(), "aparser": AParserClient(),
        "opr": OpenPageRankClient() if settings.OPENPAGERANK_API_KEY else None,
    }


def _funnel(d, c, st, sig) -> str | None:
    """Ступени дёшево→дорого с ранним выходом. Возвращает reject_reason или None,
    попутно наполняя sig посчитанными сигналами. Дорогой Wayback (T3) — только для выживших."""
    from datetime import datetime, timezone

    # T0 — фид (0 стоимости): сохранённые флаги источника + RD
    if d.feed_flags and any(d.feed_flags.get(k) for k in ("rkn", "judicial", "block")):
        return "feed_flag"
    if d.referring_domains is not None and d.referring_domains < st["min_referring_domains"]:
        return "low_rd"

    # T1 — whois (дёшево): возраст
    try:
        wc = c["aparser"].whois_created(d.domain)
        sig["whois_created"] = wc
        if wc is not None:
            age = (datetime.now(timezone.utc) - wc).days / 365.25
            sig["age_years"] = round(age, 2)
            if age < st["min_age_years"]:
                return "too_young"
    except Exception as e:  # noqa: BLE001
        sig["errors"].append(f"whois:{type(e).__name__}")

    # T2 — риск (средне): РКН + Spamhaus + indexed_echo (lookups)
    try:
        sig["rkn_listed"] = c["rkn"].is_listed(d.domain)
        if sig["rkn_listed"]:
            return "rkn"
    except Exception as e:  # noqa: BLE001
        sig["errors"].append(f"rkn:{type(e).__name__}")
    try:
        sig["blacklisted"] = c["blacklist"].is_blacklisted(d.domain)
        if sig["blacklisted"] is True:
            return "blacklist"
    except Exception as e:  # noqa: BLE001
        sig["errors"].append(f"blacklist:{type(e).__name__}")
    try:
        sig["indexed_echo"] = c["searxng"].indexed_echo(d.domain)
    except Exception as e:  # noqa: BLE001
        sig["errors"].append(f"searxng:{type(e).__name__}")

    # T3 — история (дорого): Wayback + DR, только для выживших
    try:
        hist = c["wayback"].classify_history(d.domain)
        pf = hist.get("prior_flags") or {}
        if any(pf.get(k) for k in cfg.HARD_REJECT_FLAGS) or pf.get("topic_switch"):
            sig["prior_flags"] = pf
            return "history_dirty"
        sig["prior_flags"] = pf
        sig["wayback_checked"] = hist.get("wayback_checked")
        sig["first_seen"] = hist.get("first_seen")
        if sig.get("whois_created") is None and hist.get("age_years") is not None:
            sig["age_years"] = hist["age_years"]           # whois приоритетнее; Wayback — фолбэк
    except Exception as e:  # noqa: BLE001
        sig["errors"].append(f"wayback:{type(e).__name__}")

    if c["opr"] is not None:
        try:
            sig["dr"] = c["opr"].get_page_rank([d.domain]).get(d.domain)
        except Exception as e:  # noqa: BLE001
            sig["errors"].append(f"opr:{type(e).__name__}")
    return None


def score_domain(domain_id: int, clients: dict | None = None) -> dict:
    """Полная воронка для одного домена: ступени -> скор/reject -> запись. Возвращает разбор."""
    from app.db import SessionLocal
    from app.models.domain import Domain
    from app.services.settings import get_settings

    st = get_settings()
    with SessionLocal() as db:
        d = db.get(Domain, domain_id)
        if d is None:
            raise ValueError(f"domain {domain_id} not found")
        c = clients or _make_clients()
        sig: dict = {"errors": []}
        reject = _funnel(d, c, st, sig)

        if reject:
            result = {"score": 0.0, "status": "rejected", "breakdown": {"funnel_reject": reject}}
        else:
            sig.setdefault("referring_domains", d.referring_domains)
            result = compute_score(sig)

        d.whois_created = sig.get("whois_created")
        d.prior_flags = sig.get("prior_flags")
        d.wayback_checked = bool(sig.get("wayback_checked"))
        d.first_seen = sig.get("first_seen")
        d.age_years = sig.get("age_years")
        d.rkn_listed = sig.get("rkn_listed")
        d.blacklisted = sig.get("blacklisted")
        d.indexed_echo = sig.get("indexed_echo")
        if sig.get("dr") is not None:
            d.dr = sig["dr"]
        d.clean = result["status"] != "rejected"
        d.score = result["score"]
        d.score_breakdown = {**result["breakdown"], "errors": sig.get("errors", [])}
        d.status = result["status"]
        d.reject_reason = reject or ("low_score" if result["status"] == "rejected" else None)
        db.commit()
        return {"domain": d.domain, **result, "reject_reason": d.reject_reason,
                "errors": sig.get("errors", [])}
```

- [ ] **Step 4: Прогон целиком**

Run: `.venv/bin/python -m pytest backend/tests/ -q`
Expected: PASS. Существующие `test_m1_fixes.py` (compute_score) остаются зелёными — сигнатура не менялась. Если упал старый тест, ссылавшийся на `_gather_signals` — обнови его на новый путь (в `test_m1_fixes` `_gather_signals` не используется).

- [ ] **Step 5: pyflakes + коммит**

```bash
.venv/bin/python -m pyflakes backend/app backend/tests
git add backend/app/services/scoring.py backend/tests/test_funnel.py
git commit -m "M1: воронка скоринга T0-T3 с ранним выходом — Wayback только для выживших"
```

---

## Task 4: Источники дропов + дедуп

**Files:**
- Create: `backend/app/integrations/cctld.py`, `backend/app/integrations/regru_drops.py`, `backend/app/integrations/sweb_drops.py`
- Modify: `backend/app/services/discovery.py` (мультиисточник + дедуп + feed_flags backorder)
- Test: `backend/tests/test_sources.py` (дополнить)

**Interfaces:**
- Consumes: `settings.get_settings()["sources_enabled"]` (Task 1), `AParserClient.fetch_html` (есть).
- Produces: у каждого источника `list_dropping() -> list[dict]` со строками `{domain, source, referring_domains|None, feed_flags|None}`; чистый `_parse_domains(text) -> list[str]`.
- Produces: `discovery.run_discovery() -> int` (кол-во новых; читает включённые источники, дедупит по domain).

- [ ] **Step 1: Тесты парсеров витрин (на фикстурах-строках)**

Дополнить `backend/tests/test_sources.py`:
```python
def test_parse_domains_extracts_ru():
    from app.integrations.cctld import _parse_domains
    html = "<tr><td>Example-1.RU</td></tr><tr><td>второй.рф</td></tr> мусор foo.com bar"
    got = _parse_domains(html)
    assert "example-1.ru" in got and "второй.рф" in got
    assert "foo.com" not in got          # берём только .ru/.рф/.su


def test_run_discovery_dedups_across_sources(monkeypatch):
    from app.services import discovery
    import app.db as db
    from sqlalchemy import select
    from app.models.domain import Domain
    monkeypatch.setattr("app.services.discovery._collect", lambda enabled: [
        {"domain": "dup.ru", "source": "cctld", "referring_domains": None},
        {"domain": "dup.ru", "source": "backorder", "referring_domains": 42, "feed_flags": {"rkn": False}},
        {"domain": "solo.ru", "source": "cctld", "referring_domains": None},
    ])
    n = discovery.run_discovery()
    assert n == 2
    with db.SessionLocal() as s:
        rows = {d.domain: d for d in s.execute(select(Domain)).scalars().all()}
    assert rows["dup.ru"].referring_domains == 42     # выиграла строка с бо́льшим RD (backorder)
```

- [ ] **Step 2: Прогон — падает**

Run: `.venv/bin/python -m pytest backend/tests/test_sources.py -q` → FAIL (нет `_parse_domains`, `_collect`).

- [ ] **Step 3: Источник cctld (+ общий парсер доменов)**

`backend/app/integrations/cctld.py`:
```python
"""cctld.ru — реестр освобождающихся .ru/.рф (авторитетный сырой список). Транспорт + парс.

Без RD-сигнала. URL/формат выверить на живой странице cctld.ru/service/dellist/ —
парсер устойчив к разметке (тянет домен-подобные токены .ru/.рф/.su из текста).
"""
import re
from app.integrations.base import BaseClient

_DOM = re.compile(r"\b([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.(?:ru|su|xn--p1ai|рф))\b", re.I | re.U)


def _parse_domains(text: str) -> list[str]:
    """Все .ru/.рф/.su домены из текста (список или HTML-таблица), нижний регистр, деду́п."""
    seen, out = set(), []
    for m in _DOM.findall(text or ""):
        d = m.lower().rstrip(".")
        if d not in seen:
            seen.add(d); out.append(d)
    return out


class CctldClient(BaseClient):
    URL = "https://cctld.ru/service/dellist/"          # выверить: может отдавать файл-список

    def __init__(self):
        super().__init__("https://cctld.ru")

    def list_dropping(self) -> list[dict]:
        r = self.request("GET", self.URL)
        return [{"domain": d, "source": "cctld", "referring_domains": None}
                for d in _parse_domains(r.text)]

    def ping(self) -> bool:
        r = self.request("GET", self.URL)
        return bool(_parse_domains(r.text))
```

- [ ] **Step 4: Источники reg.ru и sweb (через A-Parser — бот-защита)**

`backend/app/integrations/regru_drops.py`:
```python
"""reg.ru/domain/deleted — HTML-витрина дропов. Под бот-защитой → тянем через A-Parser.

Парсер доменов переиспользуем из cctld (устойчив к разметке). Без RD.
"""
from app.integrations.aparser import AParserClient
from app.integrations.cctld import _parse_domains

_URL = "https://www.reg.ru/domain/deleted/"


class RegruDropsClient:
    def list_dropping(self) -> list[dict]:
        html = AParserClient().fetch_html(_URL) or ""
        return [{"domain": d, "source": "reg_ru", "referring_domains": None}
                for d in _parse_domains(html)]

    def ping(self) -> bool:
        return bool(AParserClient().fetch_html(_URL))
```
`backend/app/integrations/sweb_drops.py` — идентично, класс `SwebDropsClient`, `source="sweb"`, `_URL = "https://sweb.ru/domains/deleted/"`.

- [ ] **Step 5: Мультиисточник + дедуп + feed_flags в discovery.py**

Переписать `run_discovery` в `backend/app/services/discovery.py`, сохранив `normalize_row` и IntegrityError-ретрай. Добавить сбор из включённых источников и дедуп:
```python
def _sources():
    from app.integrations.backorder import BackorderClient
    from app.integrations.cctld import CctldClient
    from app.integrations.regru_drops import RegruDropsClient
    from app.integrations.sweb_drops import SwebDropsClient
    return {"backorder": BackorderClient, "cctld": CctldClient,
            "reg_ru": RegruDropsClient, "sweb": SwebDropsClient}


def _collect(enabled: dict) -> list[dict]:
    """Собрать строки со всех включённых источников. Сбой одного источника не топит остальные."""
    rows: list[dict] = []
    for name, Client in _sources().items():
        if not enabled.get(name):
            continue
        try:
            if name == "backorder":                         # даёт RD + фид-флаги
                for r in Client().list_dropping():
                    nr = normalize_row(r)
                    if nr:
                        nr["feed_flags"] = {k: bool(r.get(k)) for k in ("rkn", "judicial", "block")}
                        rows.append(nr)
            else:
                rows.extend(Client().list_dropping())
        except Exception:  # noqa: BLE001 — один источник упал, остальные идут
            continue
    return rows


def run_discovery(min_links: int = 1) -> int:
    """Собрать включённые источники, дедуп по domain (выигрывает бо́льший RD), upsert новых."""
    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError
    from app.db import SessionLocal
    from app.models.domain import Domain
    from app.services.settings import get_settings

    rows = _collect(get_settings()["sources_enabled"])
    best: dict[str, dict] = {}
    for r in rows:
        d = r.get("domain")
        if not d:
            continue
        cur = best.get(d)
        if cur is None or (r.get("referring_domains") or 0) > (cur.get("referring_domains") or 0):
            best[d] = r
    candidates = best
    if not candidates:
        return 0

    def _insert(db) -> int:
        existing = set(db.execute(
            select(Domain.domain).where(Domain.domain.in_(candidates))
        ).scalars().all())
        fresh = [n for n in candidates if n not in existing]
        db.add_all(Domain(domain=candidates[n]["domain"], source=candidates[n]["source"],
                          referring_domains=candidates[n].get("referring_domains"),
                          feed_flags=candidates[n].get("feed_flags")) for n in fresh)
        db.commit()
        return len(fresh)

    with SessionLocal() as db:
        try:
            return _insert(db)
        except IntegrityError:
            db.rollback()
            return _insert(db)
```
`normalize_row` оставить как есть (нормализует одну строку backorder). `_DOMAIN_RE` и её импорты — без изменений.

- [ ] **Step 6: Прогон + коммит**

Run: `.venv/bin/python -m pytest backend/tests/ -q` → PASS. Проверить `test_discovery_survives_insert_race` в `test_m1_fixes.py` — он мокает `BackorderClient.list_dropping` и зовёт `run_discovery`; при включённом дедупе он должен пройти (обнови мок `sources_enabled` при необходимости через `settings.update_settings(sources_enabled={"backorder":True,...False})`, если тест ловит другие источники по сети). pyflakes чистый.
```bash
git add backend/app/integrations/cctld.py backend/app/integrations/regru_drops.py backend/app/integrations/sweb_drops.py backend/app/services/discovery.py backend/tests/test_sources.py
git commit -m "M1: плагинные источники дропов (cctld/reg.ru/sweb) + дедуп по domain + feed_flags"
```

---

## Task 5: Экран /settings (ползунки)

**Files:**
- Create: `backend/app/templates/settings.html`
- Modify: `backend/app/api/panel.py` (роуты `/settings`, `/settings/save`, `/settings/reset`, `/settings/preview`)
- Modify: `backend/app/templates/base.html` (пункт сайдбара «Настройки»)
- Test: `backend/tests/test_web_fixes.py` (дополнить — рендер и сохранение)

**Interfaces:**
- Consumes: `settings.get_settings/update_settings/reset_settings` (Task 1).
- Produces: HTML-экран + `GET /settings/preview?...` → JSON счётчиков `{rd, age, approve, manual}`.

- [ ] **Step 1: Функция счётчиков + роуты в panel.py**

Добавить в `backend/app/api/panel.py`:
```python
def _pool_counts(db, s: dict) -> dict:
    """Сколько доменов пула проходит каждый гейт при текущих порогах (превью эффекта)."""
    from datetime import datetime, timezone, timedelta
    total = db.scalar(select(func.count()).select_from(Domain)) or 0
    rd = db.scalar(select(func.count()).select_from(Domain).where(
        Domain.referring_domains >= s["min_referring_domains"])) or 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=365.25 * s["min_age_years"])
    age = db.scalar(select(func.count()).select_from(Domain).where(
        Domain.whois_created.is_not(None), Domain.whois_created <= cutoff)) or 0
    approve = db.scalar(select(func.count()).select_from(Domain).where(
        Domain.score >= s["approve_at"])) or 0
    manual = db.scalar(select(func.count()).select_from(Domain).where(
        Domain.score >= s["manual_review_at"], Domain.score < s["approve_at"])) or 0
    return {"total": total, "rd": rd, "age": age, "approve": approve, "manual": manual}


@router.get("/settings", response_class=HTMLResponse)
def settings_view(request: Request, db: Session = Depends(get_session)):
    from app.services import settings as st
    s = st.get_settings()
    return templates.TemplateResponse(request, "settings.html", {
        "active": "settings", "s": s, "counts": _pool_counts(db, s)})


@router.get("/settings/preview")
def settings_preview(min_rd: int = 1, min_age: float = 3.0, approve: float = 0.7,
                     manual: float = 0.4, db: Session = Depends(get_session)):
    from fastapi.responses import JSONResponse
    s = {"min_referring_domains": max(0, min_rd), "min_age_years": max(0.0, min_age),
         "approve_at": approve, "manual_review_at": manual}
    return JSONResponse(_pool_counts(db, s))


@router.post("/settings/save")
def settings_save(min_referring_domains: int = Form(...), min_age_years: float = Form(...),
                  approve_at: float = Form(...), manual_review_at: float = Form(...),
                  backorder: str = Form(""), cctld: str = Form(""),
                  reg_ru: str = Form(""), sweb: str = Form("")):
    from app.services import settings as st
    st.update_settings(min_referring_domains=min_referring_domains, min_age_years=min_age_years,
                       approve_at=approve_at, manual_review_at=manual_review_at,
                       sources_enabled={"backorder": bool(backorder), "cctld": bool(cctld),
                                        "reg_ru": bool(reg_ru), "sweb": bool(sweb)})
    return _back("/settings", msg="Настройки сохранены")


@router.post("/settings/reset")
def settings_reset():
    from app.services import settings as st
    st.reset_settings()
    return _back("/settings", msg="Настройки сброшены к дефолтам")
```

- [ ] **Step 2: Шаблон settings.html (ползунки, шильдики, счётчики)**

`backend/app/templates/settings.html`:
```html
{% extends "base.html" %}
{% block title %}Настройки{% endblock %}
{% block content %}
<h2><span class="idx">⚙</span> Настройки воронки
  <span class="hint">пороги скоринга · пул: {{ counts.total }} доменов · счётчики обновляются при сдвиге</span></h2>

<form method="post" action="/settings/save" class="card" style="max-width:760px">
  <div class="station">
    <div class="plate">T0 · min доноров (RD) — <b>отсекает пустышки</b></div>
    <div class="what">Домены с RD ниже порога отбраковываются на входе (0 = не резать по RD).
      Сырые списки без RD проходят на T1.</div>
    <input type="range" name="min_referring_domains" min="0" max="50" step="1" value="{{ s.min_referring_domains }}"
           oninput="pv()"> <b id="v_rd">{{ s.min_referring_domains }}</b>
    <span class="hint">проходит: <b id="c_rd">{{ counts.rd }}</b> / {{ counts.total }}</span>
  </div>
  <div class="station">
    <div class="plate">T1 · min возраст, лет — <b>отсекает молодые до дорогой истории</b></div>
    <div class="what">Возраст из whois (дата регистрации). Моложе порога → reject, не доходя до Wayback.</div>
    <input type="range" name="min_age_years" min="0" max="15" step="0.5" value="{{ s.min_age_years }}"
           oninput="pv()"> <b id="v_age">{{ s.min_age_years }}</b>
    <span class="hint">с известным возрастом проходит: <b id="c_age">{{ counts.age }}</b> / {{ counts.total }}</span>
  </div>
  <div class="station">
    <div class="plate">T3 · порог approve — <b>выше = авто-одобрение</b></div>
    <div class="what">Итоговый скор ≥ порога → домен одобряется автоматически.</div>
    <input type="range" name="approve_at" min="0" max="1" step="0.05" value="{{ s.approve_at }}"
           oninput="pv()"> <b id="v_approve">{{ s.approve_at }}</b>
    <span class="hint">уже approved: <b id="c_approve">{{ counts.approve }}</b></span>
  </div>
  <div class="station">
    <div class="plate">T3 · порог manual — <b>ниже = reject</b></div>
    <div class="what">Скор между manual и approve → ручной разбор (scored); ниже manual → reject.</div>
    <input type="range" name="manual_review_at" min="0" max="1" step="0.05" value="{{ s.manual_review_at }}"
           oninput="pv()"> <b id="v_manual">{{ s.manual_review_at }}</b>
    <span class="hint">в вилке ручного разбора: <b id="c_manual">{{ counts.manual }}</b></span>
  </div>
  <div class="station">
    <div class="plate">Источники дропов — <b>какие списки собирает Discovery</b></div>
    <label><input type="checkbox" name="backorder" {{ 'checked' if s.sources_enabled.backorder }}> backorder (с RD)</label>
    <label><input type="checkbox" name="cctld" {{ 'checked' if s.sources_enabled.cctld }}> cctld (сырой .ru)</label>
    <label><input type="checkbox" name="reg_ru" {{ 'checked' if s.sources_enabled.reg_ru }}> reg.ru</label>
    <label><input type="checkbox" name="sweb" {{ 'checked' if s.sources_enabled.sweb }}> sweb</label>
  </div>
  <div class="go">
    <button class="btn-amber" title="сохранить пороги и включённые источники">Сохранить</button>
  </div>
</form>
<form method="post" action="/settings/reset" style="margin-top:10px"
      onsubmit="return confirm('Сбросить все пороги к дефолтам?')">
  <button class="btn" title="вернуть стартовые значения из scoring_config">Сбросить к дефолтам</button>
</form>

<script>
function pv() {
  for (const k of ['rd','age','approve','manual']) {
    const map = {rd:'min_referring_domains',age:'min_age_years',approve:'approve_at',manual:'manual_review_at'};
    document.getElementById('v_'+k).textContent = document.querySelector('[name='+map[k]+']').value;
  }
  const q = new URLSearchParams({
    min_rd: document.querySelector('[name=min_referring_domains]').value,
    min_age: document.querySelector('[name=min_age_years]').value,
    approve: document.querySelector('[name=approve_at]').value,
    manual: document.querySelector('[name=manual_review_at]').value});
  fetch('/settings/preview?'+q).then(r=>r.json()).then(c=>{
    document.getElementById('c_rd').textContent = c.rd;
    document.getElementById('c_age').textContent = c.age;
    document.getElementById('c_approve').textContent = c.approve;
    document.getElementById('c_manual').textContent = c.manual;
  });
}
</script>
{% endblock %}
```

- [ ] **Step 3: Пункт сайдбара**

В `backend/app/templates/base.html` в блоке `<nav class="rail">` добавить пункт рядом с «Диагностика» (тот же паттерн `.rl`, `active == 'settings'` → класс `on`):
```html
      <a class="rl {{ 'on' if active=='settings' }}" href="/settings">
        <span><span class="n">⚙</span>Настройки</span>
        <span class="sub">пороги воронки и источники</span></a>
```

- [ ] **Step 4: Тест рендера и сохранения**

Дополнить `backend/tests/test_web_fixes.py`:
```python
def test_settings_render_and_save(client):
    assert client.get("/settings").status_code == 200
    r = client.post("/settings/save", data={
        "min_referring_domains": 2, "min_age_years": 4, "approve_at": 0.75,
        "manual_review_at": 0.4, "cctld": "on"}, follow_redirects=False)
    assert r.status_code == 303
    from app.services import settings as st
    s = st.get_settings()
    assert s["min_age_years"] == 4.0 and s["sources_enabled"]["backorder"] is False


def test_settings_preview_json(client):
    r = client.get("/settings/preview?min_rd=1&min_age=3&approve=0.7&manual=0.4")
    assert r.status_code == 200 and "total" in r.json()
```

- [ ] **Step 5: Прогон + коммит**

Run: `.venv/bin/python -m pytest backend/tests/ -q` → PASS. pyflakes чистый.
```bash
git add backend/app/api/panel.py backend/app/templates/settings.html backend/app/templates/base.html backend/tests/test_web_fixes.py
git commit -m "M1: экран /settings — ползунки порогов воронки + источники + живые счётчики"
```

---

## Task 6: Прогресс длинных задач (Discovery/Score)

**Files:**
- Create: `backend/app/services/jobs.py`
- Modify: `backend/app/services/discovery.py` (+`on_progress` в run_discovery), `backend/app/services/scoring.py` (+`on_progress` в score_pending)
- Modify: `backend/app/api/panel.py` (роуты `/run/discovery`, `/run/score` — фоновый старт; `/run/{job}/progress`)
- Modify: `backend/app/templates/domains.html` (JS-полоса прогресса)
- Test: `backend/tests/test_jobs.py`

**Interfaces:**
- Produces: `jobs.start(name, target)`, `jobs.progress(name) -> dict`, `jobs.report(name, done, total, current="", message="")`, `jobs.is_running(name) -> bool`.
- Изменяет: `run_discovery(min_links=1, on_progress=None)`, `score_pending(limit=100, on_progress=None)` — колбэк `on_progress(done, total, current)`.

- [ ] **Step 1: Тест реестра задач**

`backend/tests/test_jobs.py`:
```python
"""In-memory реестр прогресса длинных задач: старт, отчёт, защита от двойного старта."""
import time
from app.services import jobs


def test_start_reports_and_finishes():
    jobs._reset()                      # тестовый сброс реестра
    def work():
        for i in range(3):
            jobs.report("score", i + 1, 3, current=f"d{i}.ru")
    jobs.start("score", work)
    for _ in range(50):
        if not jobs.is_running("score"):
            break
        time.sleep(0.02)
    p = jobs.progress("score")
    assert p["done"] == 3 and p["total"] == 3 and p["running"] is False


def test_double_start_rejected():
    jobs._reset()
    def slow():
        jobs.report("discovery", 0, 1)
        time.sleep(0.2)
    assert jobs.start("discovery", slow) is True
    assert jobs.start("discovery", slow) is False    # уже идёт — второй старт отклонён
    for _ in range(50):
        if not jobs.is_running("discovery"):
            break
        time.sleep(0.02)


def test_error_is_captured():
    jobs._reset()
    def boom():
        raise RuntimeError("kaboom")
    jobs.start("score", boom)
    for _ in range(50):
        if not jobs.is_running("score"):
            break
        time.sleep(0.02)
    assert "kaboom" in (jobs.progress("score")["error"] or "")
```

- [ ] **Step 2: Прогон — падает**

Run: `.venv/bin/python -m pytest backend/tests/test_jobs.py -q` → FAIL (нет модуля).

- [ ] **Step 3: Реестр jobs.py**

`backend/app/services/jobs.py`:
```python
"""In-memory реестр прогресса длинных задач (один оператор). Без очереди/персистентности.

start(name, target) гоняет target в фоне (ThreadPoolExecutor на 1 воркер), запрещает
двойной старт одного имени, ловит исключение в error. Панель поллит progress(name).
Джоб живёт в памяти — рестарт контейнера его теряет (допустимо).
"""
import threading
from concurrent.futures import ThreadPoolExecutor

_LOCK = threading.Lock()
_EXEC = ThreadPoolExecutor(max_workers=1)
_STATE: dict[str, dict] = {}


def _blank() -> dict:
    return {"running": False, "done": 0, "total": 0, "current": "", "message": "", "error": None}


def report(name: str, done: int, total: int, current: str = "", message: str = "") -> None:
    with _LOCK:
        s = _STATE.setdefault(name, _blank())
        s.update(done=done, total=total, current=current, message=message)


def is_running(name: str) -> bool:
    with _LOCK:
        return _STATE.get(name, _blank())["running"]


def progress(name: str) -> dict:
    with _LOCK:
        return dict(_STATE.get(name, _blank()))


def start(name: str, target) -> bool:
    """Запустить target() в фоне под именем name. False если уже идёт."""
    with _LOCK:
        if _STATE.get(name, _blank())["running"]:
            return False
        _STATE[name] = {**_blank(), "running": True}

    def _run():
        try:
            target()
        except Exception as e:  # noqa: BLE001 — фиксируем в error, не роняем воркер
            with _LOCK:
                _STATE[name]["error"] = f"{type(e).__name__}: {e}"[:200]
        finally:
            with _LOCK:
                _STATE[name]["running"] = False

    _EXEC.submit(_run)
    return True


def _reset() -> None:                 # только для тестов
    with _LOCK:
        _STATE.clear()
```

- [ ] **Step 4: Колбэк прогресса в score_pending и run_discovery**

В `backend/app/services/scoring.py` — `score_pending`:
```python
def score_pending(limit: int = 100, on_progress=None) -> int:
    """Score all `discovered` domains; return count processed. on_progress(done,total,current)."""
    from sqlalchemy import select
    from app.db import SessionLocal
    from app.models.domain import Domain

    with SessionLocal() as db:
        ids = db.execute(
            select(Domain.id).where(Domain.status == "discovered")
            .order_by(Domain.referring_domains.desc().nulls_last())
            .limit(limit)
        ).scalars().all()
    clients = _make_clients()
    total = len(ids)
    for i, did in enumerate(ids, 1):
        out = score_domain(did, clients)
        if on_progress:
            on_progress(i, total, out.get("domain", ""))
    return total
```
В `backend/app/services/discovery.py` — `run_discovery` принимает `on_progress=None` и после успешного `_insert` зовёт `on_progress(1, 1, "готово")` (discovery одноразовый, не по-доменный):
```python
def run_discovery(min_links: int = 1, on_progress=None) -> int:
    ...  # тело как в Task 4
    with SessionLocal() as db:
        try:
            n = _insert(db)
        except IntegrityError:
            db.rollback()
            n = _insert(db)
    if on_progress:
        on_progress(1, 1, f"собрано {n}")
    return n
```

- [ ] **Step 5: Роуты — фоновый старт + прогресс**

Заменить в `backend/app/api/panel.py` синхронные `run_discovery_action`/`run_score_action` на фоновый старт и добавить progress-роут:
```python
@router.post("/run/discovery")
def run_discovery_action():
    from app.services import discovery, jobs
    ok = jobs.start("discovery", lambda: discovery.run_discovery(
        on_progress=lambda d, t, c: jobs.report("discovery", d, t, c)))
    return _back("/domains", msg="Discovery запущен…" if ok else None,
                 err=None if ok else "Discovery уже идёт")


@router.post("/run/score")
def run_score_action(n: int = Form(5)):
    from app.services import scoring, jobs
    ok = jobs.start("score", lambda: scoring.score_pending(
        limit=n, on_progress=lambda d, t, c: jobs.report("score", d, t, c)))
    return _back("/domains", msg="Score запущен…" if ok else None,
                 err=None if ok else "Score уже идёт")


@router.get("/run/{job}/progress")
def run_progress(job: str):
    from fastapi.responses import JSONResponse
    from app.services import jobs
    return JSONResponse(jobs.progress(job))
```

- [ ] **Step 6: JS-полоса в domains.html**

В `backend/app/templates/domains.html` рядом с кнопками Discovery/Score вставить контейнер и скрипт:
```html
<div id="prog" class="hint" style="margin:8px 0"></div>
<script>
function poll(job) {
  fetch('/run/'+job+'/progress').then(r=>r.json()).then(p=>{
    const el = document.getElementById('prog');
    if (p.running) {
      el.textContent = job+': '+p.done+'/'+p.total+(p.current ? ' — '+p.current : '');
      setTimeout(()=>poll(job), 1500);
    } else if (p.done && p.total) {
      el.textContent = job+': готово ('+p.done+')'; setTimeout(()=>location.reload(), 800);
    } else if (p.error) { el.textContent = job+': ошибка — '+p.error; }
  });
}
// стартуем опрос, если джоб уже идёт (после сабмита формы)
['discovery','score'].forEach(j => fetch('/run/'+j+'/progress').then(r=>r.json())
  .then(p=>{ if (p.running) poll(j); }));
</script>
```

- [ ] **Step 7: Прогон + правка старых тестов**

Run: `.venv/bin/python -m pytest backend/tests/ -q`. Если `test_pipeline.py` ассертил синхронный результат `POST /run/score` («обработано N») — обновить: теперь роут возвращает 303 с «Score запущен…», а сам прогон тестировать через `scoring.score_pending(...)` напрямую. pyflakes чистый.
```bash
git add backend/app/services/jobs.py backend/app/services/scoring.py backend/app/services/discovery.py backend/app/api/panel.py backend/app/templates/domains.html backend/tests/test_jobs.py backend/tests/test_pipeline.py
git commit -m "M1: прогресс длинных задач — фоновый прогон + in-memory реестр + JS-полоса"
```

---

## Task 7: Блок версии + уведомление об обновлении

**Files:**
- Create: `backend/app/services/version.py`
- Modify: `backend/app/api/panel.py` (`diag_view` передаёт версию; `/admin/pull` — old→new; `/admin/check-updates`)
- Modify: `backend/app/templates/diag.html` (блок версии + кнопка «проверить обновления»)
- Test: `backend/tests/test_version.py`

**Interfaces:**
- Produces: `version.current_version() -> dict` (`{hash, subject, date}` или `{error}`); `version._parse(hash, subject, date) -> dict` (чистая).

- [ ] **Step 1: Тест парсера версии**

`backend/tests/test_version.py`:
```python
"""Версия из git: парсер вывода (без запуска git)."""
from app.services.version import _parse


def test_parse_ok():
    v = _parse("a1b2c3d", "M1: воронка скоринга", "2026-07-06")
    assert v == {"hash": "a1b2c3d", "subject": "M1: воронка скоринга", "date": "2026-07-06"}


def test_parse_empty():
    v = _parse("", "", "")
    assert v["hash"] == "—"
```

- [ ] **Step 2: Прогон — падает**, затем реализация `version.py`:

`backend/app/services/version.py`:
```python
"""Текущая версия кода из git в контейнере (репо смонтировано /repo). Дёшево, локально."""
import subprocess


def _parse(h: str, subject: str, date: str) -> dict:
    return {"hash": h.strip() or "—", "subject": subject.strip() or "—", "date": date.strip() or "—"}


def current_version() -> dict:
    """{hash, subject, date} последнего коммита /repo; {error} если git недоступен."""
    try:
        out = subprocess.run(
            ["git", "-C", "/repo", "-c", "safe.directory=/repo", "log", "-1", "--format=%h%n%s%n%cs"],
            capture_output=True, text=True, timeout=10)
        if out.returncode != 0:
            return {"error": (out.stderr or "git error").strip()[:150]}
        parts = (out.stdout.strip().split("\n") + ["", "", ""])[:3]
        return _parse(*parts)
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"[:150]}
```

- [ ] **Step 3: diag_view передаёт версию + check-updates роут**

В `backend/app/api/panel.py` `diag_view` добавить в контекст `"version": __import__("app.services.version", fromlist=["x"]).current_version()`. Новый роут:
```python
@router.post("/admin/check-updates")
def check_updates_action():
    import base64 as _b64, os, subprocess
    from app.services.version import current_version
    if not settings.GITHUB_TOKEN:
        return _back("/diag", err="GITHUB_TOKEN не задан — нечем проверить удалёнку")
    basic = _b64.b64encode(f"x-access-token:{settings.GITHUB_TOKEN}".encode()).decode()
    env = {**os.environ, "GIT_CONFIG_COUNT": "1",
           "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
           "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}"}
    try:
        r = subprocess.run(["git", "-C", "/repo", "ls-remote",
                            f"https://github.com/{settings.GITHUB_REPO}.git", "main"],
                           capture_output=True, text=True, timeout=20, env=env)
        remote = (r.stdout.split() or [""])[0][:7]
        cur = current_version().get("hash", "")
        if not remote:
            return _back("/diag", err="не удалось прочитать удалёнку")
        same = remote.startswith(cur) or cur.startswith(remote)
        return _back("/diag", msg=f"Текущая {cur} — {'актуально' if same else 'доступна новее '+remote}")
    except Exception as e:  # noqa: BLE001
        return _back("/diag", err=f"check-updates: {type(e).__name__}")
```
В `/admin/pull` — снять HEAD до pull и показать old→new. Перед `pull = subprocess.run(...)` добавить:
```python
    from app.services.version import current_version
    old = current_version().get("hash", "")
```
и в успешной ветке заменить баннер на:
```python
        new = current_version().get("hash", "")
        return _back("/diag", msg=f"Обновлено: {old}→{new} «{current_version().get('subject','')}»{warn}")
```

- [ ] **Step 4: Блок версии в diag.html**

В `backend/app/templates/diag.html` после `<h2>…Диагностика…</h2>` вставить:
```html
<div class="card" style="max-width:920px; margin-bottom:14px">
  <b>Версия кода:</b>
  {% if version.error %}<span style="color:var(--bad)">git недоступен — {{ version.error }}</span>
  {% else %}<code>{{ version.hash }}</code> · {{ version.subject }} · <span class="hint">{{ version.date }}</span>{% endif %}
  {% if can_pull %}
  <form method="post" action="/admin/check-updates" style="display:inline; margin-left:12px">
    <button class="btn" title="git ls-remote — сравнить с origin/main">проверить обновления</button>
  </form>{% endif %}
</div>
```

- [ ] **Step 5: Прогон + коммит**

Run: `.venv/bin/python -m pytest backend/tests/ -q` → PASS. pyflakes чистый.
```bash
git add backend/app/services/version.py backend/app/api/panel.py backend/app/templates/diag.html backend/tests/test_version.py
git commit -m "M1: блок версии в /diag + old→new после git-pull + проверка обновлений"
```

---

## Self-Review (проведён при написании)

- **Покрытие спека:** A(данные)→T1; whois→T2; воронка T0–T3→T3; источники+дедуп→T4; /settings ползунки→T5; прогресс→T6; версия→T7. Все разделы спека покрыты.
- **Инвариант «Wayback только для выживших»:** доказывается `test_too_young_rejects_before_wayback` / `test_feed_flag_rejects_first` / `test_rkn_rejects_before_wayback` (счётчик `wb.calls == 0`).
- **Типы согласованы:** `get_settings()` ключи (`min_referring_domains/min_age_years/approve_at/manual_review_at/sources_enabled`) одинаковы в Tasks 1/3/5; `on_progress(done,total,current)` одинаков в Tasks 6; `reject_reason` enum один во всех задачах.
- **Плейсхолдеров нет:** весь код приведён; «выверить URL/селектор» помечено как рантайм-проверка (парсеры тестируются на фикстурах, не сетью) — это не заглушка кода, а честная граница.

## Открытые вопросы (не блокируют, проверить в реализации)
- Точные URL/формат cctld/reg.ru/sweb на живых страницах бокса (парсер устойчив к разметке; транспорт — выверить, при бот-защите A-Parser).
- Объём cctld при большом пуле: whois на всём списке — если тяжело, ограничить discovery-батч (отметить, если всплывёт).
- `test_pipeline.py` / `test_discovery_survives_insert_race`: свериться, что мультиисточниковый discovery и async-роуты не ломают их ожидания; поправить моки/ассерты по месту (Task 4 Step 6, Task 6 Step 7).
