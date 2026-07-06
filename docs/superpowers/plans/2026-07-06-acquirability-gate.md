# Мозг M1: приобретаемость как гейт — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ввести приобретаемость как ранний гейт воронки M1 — whois-пробой ставит лейн выкупа (`bid`/`free`) или отсекает `not_acquirable` до дорогих проверок; дорогой Wayback только на приобретаемых; + цена бэкордера, индикаторы зависимостей, кнопки полного прогона.

**Architecture:** whois-вызов в воронке (T1) становится двойным — доступность (свободен/занят) + возраст. Сырой домен «занят» → reject `not_acquirable`; «свободен» → лейн `free`; whois-сбой/непонятно/сверх бюджета → домен остаётся `discovered` (перепроверится). backorder объявляет `lane=bid` из источника. whois-бюджет на прогон (`max_whois_per_run`) + сортировка RD-desc демотируют сырой cctld.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.x, Pydantic v2, Alembic, Jinja2, httpx. Тесты — pytest на оффлайн SQLite (`backend/tests/conftest.py`), сеть мокается.

## Global Constraints

- **Спек:** `docs/superpowers/specs/2026-07-06-acquirability-gate-design.md` — источник истины.
- **`compute_score` остаётся чистой** (без I/O); её сигнатура и unit-тесты (`__main__` в scoring.py) не меняются. Гейт приобретаемости — только в `_funnel`/`score_domain`.
- **Дорогой Wayback (T3) — ТОЛЬКО для приобретаемых выживших T0–T2.** Регрессия доказывает `wb.calls == 0` на `not_acquirable` и на whois-сбое.
- **Хард-гейты не трогать:** деньги (`confirmed_by_human`), редактура (публикация из `edited`). Это M1.
- **Тесты оффлайн+детерминированы:** SQLite-харнесс; любая сеть мокается через monkeypatch. Прогон: `.venv/bin/python -m pytest backend/tests/ -q` из корня репо. pyflakes чистый: `.venv/bin/python -m pyflakes backend/app backend/tests`. Никаких `sleep`-гонок.
- **reject_reason enum:** `low_rd | feed_flag | too_young | rkn | blacklist | history_dirty | low_score | not_acquirable`.
- **lane значения:** `bid | free | null`. **sources_enabled ключи:** `backorder | cctld | reg_ru | sweb`.
- **whois-сбой/непонятно/сверх бюджета → домен остаётся `discovered`** (не `rejected`, не `scored`): `sig["acquirability_unresolved"]=True`, `score_domain` ничего не пишет.
- **Панель — светлая CMS, принцип шильдика, русский UI.** CSS-переменные из `base.html`: `--amber`(#e05e10) / `--amber-soft`(#fdeee2) / `--amber2` / `--mut` / `--panel2` / `--line` / `--mono`. Новые контролы — сразу по-русски.
- **Безопасность панели не ослаблять:** CSRF same-origin + Basic-auth в `main.py` остаются; новые POST-роуты в том же router.
- **conftest уже регистрирует** `app.models.settings` — новых моделей нет, доп. регистрация не нужна.

---

## Файловая карта

- `backend/app/models/domain.py` — +6 колонок в `Domain` (Task 1).
- `backend/app/models/settings.py` — +`max_whois_per_run` (Task 1).
- `backend/app/services/scoring_config.py` — +`MAX_WHOIS_PER_RUN` (Task 1).
- `backend/app/services/settings.py` — +`max_whois_per_run` в bounds/defaults/get (Task 1).
- `backend/alembic/versions/0003_acquirability.py` — миграция (Task 1).
- `backend/app/integrations/aparser.py` — +`whois_probe` + `_parse_whois_available` (Task 2).
- `backend/app/services/discovery.py` — `normalize_row` +lane/deadline/visitors/tic, `_insert` пишет их (Task 3), +acquire_price из кэша (Task 5).
- `backend/app/services/scoring.py` — `_funnel` T1 приобретаемость + `score_domain`/`score_pending` бюджет (Task 4).
- `backend/app/integrations/backorder.py` — `get_tariffs` реализация (Task 5).
- `backend/app/services/pricing.py` — `refresh_backorder_prices` + кэш тарифа (Task 5).
- `backend/app/services/diagnostics.py` — +A-Parser +БД в `_spec` (Task 6).
- `backend/app/api/panel.py` — колонки/фильтр/кнопки/слайдер/refresh-роут (Task 7).
- `backend/app/templates/{domains,settings}.html` + `base.html` — бейджи/колонки/слайдер (Task 7).
- Тесты: `test_settings.py`, `test_sources.py`, `test_funnel.py`, новый `test_pricing.py`, `test_web_fixes.py`.

---

## Task 1: Данные — колонки Domain + max_whois_per_run + миграция 0003

**Files:**
- Modify: `backend/app/models/domain.py` (класс `Domain`, после блока `# funnel bookkeeping`)
- Modify: `backend/app/models/settings.py` (класс `ScoringSettings`)
- Modify: `backend/app/services/scoring_config.py` (конец файла)
- Modify: `backend/app/services/settings.py` (`_KEYS_NUM`, `_BOUNDS`, `_defaults`, `get_settings`)
- Create: `backend/alembic/versions/0003_acquirability.py`
- Test: `backend/tests/test_settings.py`

**Interfaces:**
- Produces: `Domain.lane: str|None`, `Domain.acquire_deadline: datetime|None`, `Domain.acquire_price: float|None`, `Domain.price_checked_at: datetime|None`, `Domain.visitors: int|None`, `Domain.tic: int|None`.
- Produces: `get_settings()` возвращает доп. ключ `max_whois_per_run: int`; `update_settings(max_whois_per_run=…)` клампит в `[0, 5000]`.

- [ ] **Step 1: Тест на новый порог в настройках**

В `backend/tests/test_settings.py` добавить:
```python
def test_max_whois_per_run_default_and_clamp():
    from app.services.settings import get_settings, update_settings
    assert get_settings()["max_whois_per_run"] == 200          # дефолт
    assert update_settings(max_whois_per_run=50)["max_whois_per_run"] == 50
    assert update_settings(max_whois_per_run=999999)["max_whois_per_run"] == 5000  # верхний кламп
    assert update_settings(max_whois_per_run=-5)["max_whois_per_run"] == 0         # нижний кламп
```

- [ ] **Step 2: Прогнать — падает (нет ключа / колонки)**

Run: `.venv/bin/python -m pytest backend/tests/test_settings.py::test_max_whois_per_run_default_and_clamp -q`
Expected: FAIL (`KeyError: 'max_whois_per_run'` или `no such column`).

- [ ] **Step 3: Колонки Domain**

В `backend/app/models/domain.py`, в классе `Domain` после строки `feed_flags: Mapped[dict | None] = mapped_column(JSONB)...`:
```python
    # приобретаемость (Мозг M1)
    lane: Mapped[str | None] = mapped_column(String(8))              # bid | free | null
    acquire_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # дедлайн ловли (backorder delete_date)
    acquire_price: Mapped[float | None] = mapped_column(Numeric)     # базовая цена выкупа (backorder тариф)
    price_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    visitors: Mapped[int | None] = mapped_column(Integer)            # инфо-сигнал из фида (вес 0)
    tic: Mapped[int | None] = mapped_column(Integer)                 # Яндекс ТИЦ из фида (вес 0)
```
И обновить комментарий у `reject_reason` — добавить `|not_acquirable` в перечень.

- [ ] **Step 4: Колонка max_whois_per_run в модели настроек**

В `backend/app/models/settings.py`, в классе `ScoringSettings` после `manual_review_at`:
```python
    max_whois_per_run: Mapped[int] = mapped_column(Integer, default=200)  # кап whois-вызовов за прогон
```

- [ ] **Step 5: Дефолт в scoring_config**

В конец `backend/app/services/scoring_config.py`:
```python
MAX_WHOIS_PER_RUN = 200        # кап whois-пробоев за один прогон проверки (защита от сырого cctld)
```

- [ ] **Step 6: Настройки — bounds/defaults/get**

В `backend/app/services/settings.py`:
- `_KEYS_NUM` → добавить `"max_whois_per_run"`:
```python
_KEYS_NUM = ("min_referring_domains", "min_age_years", "approve_at", "manual_review_at", "max_whois_per_run")
```
- `_BOUNDS` → добавить строку:
```python
    "max_whois_per_run": (0, 5000),
```
- `_defaults()` → добавить в возвращаемый dict:
```python
        "max_whois_per_run": cfg.MAX_WHOIS_PER_RUN,
```
- `get_settings()` → в возвращаемый dict добавить:
```python
            "max_whois_per_run": int(r.max_whois_per_run),
```

- [ ] **Step 7: Миграция 0003**

`backend/alembic/versions/0003_acquirability.py`:
```python
"""acquirability: lane/acquire_*/visitors/tic + max_whois_per_run

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-06
"""
from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("domains", sa.Column("lane", sa.String(8)))
    op.add_column("domains", sa.Column("acquire_deadline", sa.DateTime(timezone=True)))
    op.add_column("domains", sa.Column("acquire_price", sa.Numeric()))
    op.add_column("domains", sa.Column("price_checked_at", sa.DateTime(timezone=True)))
    op.add_column("domains", sa.Column("visitors", sa.Integer()))
    op.add_column("domains", sa.Column("tic", sa.Integer()))
    op.add_column("scoring_settings",
                  sa.Column("max_whois_per_run", sa.Integer(), nullable=False, server_default="200"))


def downgrade() -> None:
    op.drop_column("scoring_settings", "max_whois_per_run")
    for col in ("tic", "visitors", "price_checked_at", "acquire_price", "acquire_deadline", "lane"):
        op.drop_column("domains", col)
```

- [ ] **Step 8: Прогнать тест + весь сьют + pyflakes**

Run: `.venv/bin/python -m pytest backend/tests/test_settings.py -q && .venv/bin/python -m pytest backend/tests/ -q && .venv/bin/python -m pyflakes backend/app backend/tests`
Expected: всё зелёное, pyflakes без вывода.

- [ ] **Step 9: Commit**

```bash
git add backend/app/models/domain.py backend/app/models/settings.py backend/app/services/scoring_config.py backend/app/services/settings.py backend/alembic/versions/0003_acquirability.py backend/tests/test_settings.py
git commit -m "M1 приобретаемость: колонки domain (lane/acquire_*/visitors/tic) + max_whois_per_run + миграция 0003"
```

---

## Task 2: whois_probe — доступность + возраст одним вызовом

**Files:**
- Modify: `backend/app/integrations/aparser.py` (+`_parse_whois_available`, +`whois_probe`; `whois_created` делегирует)
- Test: `backend/tests/test_sources.py`

**Interfaces:**
- Consumes: `_parse_whois_created(text) -> datetime|None` (уже есть), `_result_string`, `_call` (уже есть).
- Produces: `AParserClient.whois_probe(domain: str) -> {"available": bool|None, "created": datetime|None}`. `available=True` — домен свободен (нет записи); `False` — занят; `None` — не определить.
- Produces: `_parse_whois_available(text: str) -> bool|None` (модульная чистая функция).

- [ ] **Step 1: Тест парсера доступности**

В `backend/tests/test_sources.py` добавить:
```python
def test_parse_whois_available():
    from app.integrations.aparser import _parse_whois_available
    assert _parse_whois_available("No entries found for the selected source.") is True
    assert _parse_whois_available("Not found") is True
    assert _parse_whois_available(
        "domain: EXAMPLE.RU\ncreated: 2010.11.15\nnserver: ns1.example.ru") is False
    assert _parse_whois_available("registrar: RU-CENTER\nperson: Private") is False
    assert _parse_whois_available("какой-то мусор без маркеров") is None
    assert _parse_whois_available("") is None


def test_whois_probe_shapes(monkeypatch):
    from app.integrations import aparser
    c = aparser.AParserClient()
    monkeypatch.setattr(c, "_call", lambda *a, **k: {"data": {"resultString": "No entries found"}})
    assert c.whois_probe("free.ru") == {"available": True, "created": None}
    monkeypatch.setattr(c, "_call", lambda *a, **k: {
        "data": {"resultString": "domain: X.RU\ncreated: 2010.11.15\nnserver: ns.x.ru"}})
    pr = c.whois_probe("taken.ru")
    assert pr["available"] is False and pr["created"] is not None
```

- [ ] **Step 2: Прогнать — падает**

Run: `.venv/bin/python -m pytest backend/tests/test_sources.py -k whois_probe -q` (и `-k _parse_whois_available`)
Expected: FAIL (`_parse_whois_available`/`whois_probe` не существуют).

- [ ] **Step 3: Реализация в aparser.py**

В `backend/app/integrations/aparser.py`, после `_parse_whois_created` (перед классом):
```python
# маркеры «домен свободен» (нет регистрации). Список расширяемый — сверить с живым
# ответом A-Parser Net::Whois на первом прогоне (см. спек §J).
_FREE_MARKERS = ("no entries found", "not found", "no match", "no object found",
                 "available for registration", "нет данных", "not registered")
_REG_MARKERS = ("nserver", "registrar", "person:", "org:", "paid-till", "domain:")


def _parse_whois_available(text: str) -> bool | None:
    """True — домен свободен (нет записи); False — занят; None — не определить."""
    low = (text or "").lower()
    if any(m in low for m in _FREE_MARKERS):
        return True
    if _RE_RU.search(low) or _RE_GTLD.search(low) or any(m in low for m in _REG_MARKERS):
        return False
    return None
```
В классе `AParserClient` заменить метод `whois_created` и добавить `whois_probe`:
```python
    def whois_probe(self, domain: str) -> dict:
        """Один Net::Whois-вызов -> доступность + дата регистрации.
        available: True свободен / False занят / None не определить. created: дата или None."""
        res = self._call("oneRequest", {"query": domain, "parser": "Net::Whois",
                                        "configPreset": "default", "preset": "default"})
        text = self._result_string(res)
        return {"available": _parse_whois_available(text), "created": _parse_whois_created(text)}

    def whois_created(self, domain: str) -> datetime | None:
        """Дата регистрации (обёртка над whois_probe для обратной совместимости)."""
        return self.whois_probe(domain)["created"]
```

- [ ] **Step 4: Прогнать — зелено + весь сьют + pyflakes**

Run: `.venv/bin/python -m pytest backend/tests/test_sources.py -q && .venv/bin/python -m pyflakes backend/app backend/tests`
Expected: PASS, pyflakes чисто.

- [ ] **Step 5: Commit**

```bash
git add backend/app/integrations/aparser.py backend/tests/test_sources.py
git commit -m "M1 приобретаемость: whois_probe (доступность + возраст одним вызовом)"
```

---

## Task 3: Discovery — сохранить дедлайн/visitors/tic + метить backorder lane=bid

**Files:**
- Modify: `backend/app/services/discovery.py` (`normalize_row`, +`_parse_deadline`, `_insert`)
- Test: `backend/tests/test_sources.py`

**Interfaces:**
- Consumes: колонки `Domain.lane/acquire_deadline/visitors/tic` (Task 1).
- Produces: `normalize_row(row)` теперь кладёт `lane="bid"`, `acquire_deadline`, `visitors`, `tic`. `run_discovery` персистит их. `_parse_deadline(val) -> datetime|None`.

- [ ] **Step 1: Тест нормализации + персиста**

В `backend/tests/test_sources.py` добавить:
```python
def test_normalize_row_captures_acquirability():
    from app.services.discovery import normalize_row
    nr = normalize_row({"domainname": "drop.ru", "links": "7",
                        "delete_date": "2026-07-10", "visitors": "120", "yandex_tic": "30"})
    assert nr["lane"] == "bid" and nr["referring_domains"] == 7
    assert nr["acquire_deadline"] is not None and nr["visitors"] == 120 and nr["tic"] == 30
    # мусорный дедлайн не роняет строку
    assert normalize_row({"domainname": "d2.ru", "delete_date": "нет"})["acquire_deadline"] is None


def test_run_discovery_persists_acquirability(monkeypatch):
    from app.services import discovery
    import app.db as db
    from app.models.domain import Domain
    from sqlalchemy import select
    monkeypatch.setattr("app.services.discovery._collect", lambda enabled, on_progress=None: [
        {"domain": "bo.ru", "source": "backorder", "referring_domains": 5, "lane": "bid",
         "acquire_deadline": None, "visitors": 10, "tic": 20, "feed_flags": {"rkn": False}}])
    assert discovery.run_discovery() == 1
    with db.SessionLocal() as s:
        d = s.execute(select(Domain).where(Domain.domain == "bo.ru")).scalar_one()
        assert d.lane == "bid" and d.visitors == 10 and d.tic == 20
```

- [ ] **Step 2: Прогнать — падает**

Run: `.venv/bin/python -m pytest backend/tests/test_sources.py -k "acquirability" -q`
Expected: FAIL (`KeyError: 'lane'` / колонки не пишутся).

- [ ] **Step 3: normalize_row + _parse_deadline**

В `backend/app/services/discovery.py`, после импортов добавить:
```python
from datetime import datetime, timezone


def _parse_deadline(val) -> datetime | None:
    """backorder delete_date -> datetime UTC. Формат выверить на живом фиде (спек §J);
    парсим устойчиво: ISO-дата/дату-время, иначе None."""
    s = str(val or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d.%m.%Y"):
        try:
            return datetime.strptime(s[:19], fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None
```
Заменить тело `normalize_row`:
```python
def normalize_row(row: dict) -> dict | None:
    """Одна строка фида backorder -> нормализованный кандидат (или None если мусор).
    backorder — bid-лейн из источника; тянем дедлайн/visitors/tic (раньше выбрасывались)."""
    domain = (row.get("domainname") or "").strip().lower().rstrip(".")
    if not domain or len(domain) > 253 or not _DOMAIN_RE.match(domain):
        return None
    def _int(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return None
    return {"domain": domain, "source": "backorder", "referring_domains": _int(row.get("links")) or 0,
            "lane": "bid", "acquire_deadline": _parse_deadline(row.get("delete_date")),
            "visitors": _int(row.get("visitors")), "tic": _int(row.get("yandex_tic"))}
```

- [ ] **Step 4: _insert пишет новые поля**

В `run_discovery`, в функции `_insert`, заменить конструктор `Domain(...)`:
```python
        db.add_all(Domain(
            domain=candidates[n]["domain"], source=candidates[n]["source"],
            referring_domains=candidates[n].get("referring_domains"),
            feed_flags=candidates[n].get("feed_flags"),
            lane=candidates[n].get("lane"),
            acquire_deadline=candidates[n].get("acquire_deadline"),
            visitors=candidates[n].get("visitors"), tic=candidates[n].get("tic"),
        ) for n in fresh)
```

- [ ] **Step 5: Прогнать тесты + весь сьют + pyflakes**

Run: `.venv/bin/python -m pytest backend/tests/test_sources.py -q && .venv/bin/python -m pytest backend/tests/ -q && .venv/bin/python -m pyflakes backend/app backend/tests`
Expected: всё зелёное.

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/discovery.py backend/tests/test_sources.py
git commit -m "M1 приобретаемость: discovery сохраняет дедлайн/visitors/tic + backorder lane=bid"
```

---

## Task 4: Воронка T1 — приобретаемость + whois-кап + Wayback только на приобретаемых

**Files:**
- Modify: `backend/app/services/scoring.py` (`_funnel`, `score_domain`, `score_pending`)
- Modify: `backend/tests/test_funnel.py` (фейки whois_created → whois_probe; выставить `lane`)
- Test: `backend/tests/test_funnel.py` (новые кейсы)

**Interfaces:**
- Consumes: `AParserClient.whois_probe` (Task 2), `Domain.lane` (Task 1), `get_settings()["max_whois_per_run"]` (Task 1).
- Produces: `_funnel(d, c, st, sig, whois_budget=None) -> str|None`; `score_domain(domain_id, clients=None, whois_budget=None) -> dict`; `score_pending(limit, on_progress)` с бюджетом. Новый reject `not_acquirable`. `sig["acquirability_unresolved"]=True` → домен оставить `discovered`. `sig["lane"]` несёт лейн для записи.

- [ ] **Step 1: Новые тесты воронки**

В `backend/tests/test_funnel.py` добавить (используя существующие фейки-паттерны файла; `_Wayback` — счётчик вызовов из этого файла):
```python
def test_raw_registered_rejects_not_acquirable_before_wayback(monkeypatch, sqlite_db):
    """Сырой домен, whois=занят → not_acquirable, Wayback НЕ вызывался."""
    from app.services import scoring
    import app.db as db
    from app.models.domain import Domain
    wb = _Wayback()   # счётчик .calls (как в других тестах файла)
    clients = _clients(whois={"available": False, "created": None}, wayback=wb)
    with db.SessionLocal() as s:
        s.add(Domain(domain="taken.ru", source="cctld", status="discovered", lane=None,
                     referring_domains=None)); s.commit()
        did = s.execute(_id_of("taken.ru")).scalar_one()
    out = scoring.score_domain(did, clients)
    assert out["status"] == "rejected" and out["reject_reason"] == "not_acquirable"
    assert wb.calls == 0


def test_raw_free_gets_free_lane(monkeypatch, sqlite_db):
    """Сырой домен, whois=свободен → lane=free, доходит до Wayback (возраст из Wayback)."""
    from app.services import scoring
    import app.db as db
    from app.models.domain import Domain
    wb = _Wayback(age_years=10.0)
    clients = _clients(whois={"available": True, "created": None}, wayback=wb)
    with db.SessionLocal() as s:
        s.add(Domain(domain="free.ru", source="reg_ru", status="discovered", lane=None)); s.commit()
        did = s.execute(_id_of("free.ru")).scalar_one()
    scoring.score_domain(did, clients)
    with db.SessionLocal() as s:
        d = s.get(Domain, did)
    assert d.lane == "free" and wb.calls == 1


def test_whois_fail_stays_discovered(sqlite_db):
    """whois упал на сыром домене → остаётся discovered, не rejected, Wayback не вызван."""
    from app.services import scoring
    import app.db as db
    from app.models.domain import Domain
    wb = _Wayback()
    clients = _clients(whois_raises=True, wayback=wb)
    with db.SessionLocal() as s:
        s.add(Domain(domain="oops.ru", source="cctld", status="discovered", lane=None)); s.commit()
        did = s.execute(_id_of("oops.ru")).scalar_one()
    out = scoring.score_domain(did, clients)
    assert out.get("unresolved") is True and wb.calls == 0
    with db.SessionLocal() as s:
        assert s.get(Domain, did).status == "discovered"      # не сдвинулся


def test_whois_budget_caps_run(sqlite_db):
    """max_whois_per_run=1 + 2 сырых домена → whois только у одного, второй остаётся discovered."""
    from app.services import scoring
    from app.services.settings import update_settings
    import app.db as db
    from app.models.domain import Domain
    update_settings(max_whois_per_run=1)
    wb = _Wayback(age_years=10.0)
    clients = _clients(whois={"available": True, "created": None}, wayback=wb)
    with db.SessionLocal() as s:
        s.add_all([Domain(domain=f"r{i}.ru", source="cctld", status="discovered", lane=None,
                          referring_domains=None) for i in range(2)]); s.commit()
    scoring.score_pending(limit=10)
    with db.SessionLocal() as s:
        still = s.execute(_count_discovered()).scalar()
    assert still == 1                                          # один не обработан (бюджет исчерпан)
```
Хелперы `_clients(...)`, `_Wayback`, `_id_of`, `_count_discovered` — переиспользовать/адаптировать из уже существующих в файле. Если `_clients` строит набор клиентов, у фейка A-Parser метод должен быть **`whois_probe`** (возвращает переданный dict или бросает при `whois_raises`), а НЕ `whois_created`.

- [ ] **Step 2: Адаптировать существующие фейки и кейсы**

В `backend/tests/test_funnel.py` **прочитать** и обновить все существующие фейки/фикстуры:
- фейк A-Parser: метод `whois_created` → `whois_probe`, возвращающий `{"available": ..., "created": <старая дата>}`. Для доменов, которые в старых тестах доходили до T2/T3, выставить `available=False` (занят — но у них есть дата регистрации) И на тестовом `Domain` поставить **`lane="bid"`** (backorder-лейн из источника обходит гейт приобретаемости; занятость там не важна, whois нужен для возраста). Иначе новый гейт вернёт `not_acquirable` и старые ассерты (доход до Wayback, too_young и т.п.) сломаются.
- поведенческие ассерты (`wb.calls==0` на feed_flag/low_rd/too_young/rkn/blacklist, `history_dirty`, `low_score`, too_young-фолбэк из Wayback) — **сохранить по смыслу**.

- [ ] **Step 3: Прогнать — новые падают, суть ясна**

Run: `.venv/bin/python -m pytest backend/tests/test_funnel.py -q`
Expected: новые 4 теста FAIL (нет логики приобретаемости), адаптированные проходят если фейки уже на `whois_probe`.

- [ ] **Step 4: Переписать T1 в _funnel**

В `backend/app/services/scoring.py` заменить сигнатуру и блок T1 функции `_funnel`. Полная новая функция:
```python
def _funnel(d, c, st, sig, whois_budget=None) -> str | None:
    """Ступени дёшево→дорого с ранним выходом. Возвращает reject_reason или None,
    наполняя sig. Приобретаемость — гейт на T1: whois решает free/занят для сырых
    источников (backorder объявляет lane=bid сам). Дорогой Wayback (T3) — только для
    приобретаемых выживших. sig['acquirability_unresolved']=True → оставить domain discovered."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)

    # T0 — фид (0 стоимости)
    if d.feed_flags and any(d.feed_flags.get(k) for k in ("rkn", "judicial", "block")):
        return "feed_flag"
    if d.referring_domains is not None and d.referring_domains < st["min_referring_domains"]:
        return "low_rd"

    # T1 — приобретаемость + возраст (ОДИН whois-вызов, под бюджетом)
    if whois_budget is not None and whois_budget[0] <= 0:
        sig["acquirability_unresolved"] = True     # бюджет whois на прогон исчерпан — оставить discovered
        return None
    age_known = False
    try:
        if whois_budget is not None:
            whois_budget[0] -= 1
        pr = c["aparser"].whois_probe(d.domain)
    except Exception as e:  # noqa: BLE001
        sig["errors"].append(f"whois:{type(e).__name__}")
        if d.lane != "bid":                         # сырому источнику whois нужен для лейна
            sig["acquirability_unresolved"] = True
            return None
        pr = {"available": None, "created": None}   # bid: лейн из источника, продолжаем без возраста

    wc = pr.get("created")
    sig["whois_created"] = wc
    if wc is not None:
        age_known = True
        age = (now - wc).days / 365.25
        sig["age_years"] = round(age, 2)
        if age < st["min_age_years"]:
            return "too_young"

    if d.lane == "bid":
        sig["lane"] = "bid"
    else:
        av = pr.get("available")
        if av is True:
            sig["lane"] = "free"                    # свободен к регистрации
        elif av is False:
            return "not_acquirable"                 # занят, не на backorder — купить нельзя
        else:                                       # av is None — не определили
            sig["acquirability_unresolved"] = True
            return None

    # T2 — риск (средне): РКН + Spamhaus + indexed_echo
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

    # T3 — история (дорого): только для приобретаемых выживших
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

    # непроверяемый по whois возраст всё равно проходит гейт молодости (ПОСЛЕ history_dirty)
    if not age_known and sig.get("age_years") is not None and sig["age_years"] < st["min_age_years"]:
        return "too_young"

    if c["opr"] is not None:
        try:
            sig["dr"] = c["opr"].get_page_rank([d.domain]).get(d.domain)
        except Exception as e:  # noqa: BLE001
            sig["errors"].append(f"opr:{type(e).__name__}")
    return None
```

- [ ] **Step 5: score_domain — обработать unresolved + записать lane**

В `backend/app/services/scoring.py`, в `score_domain` заменить сигнатуру и тело от вызова `_funnel` до записи:
```python
def score_domain(domain_id: int, clients: dict | None = None, whois_budget=None) -> dict:
    """Полная воронка для одного домена. whois_budget — мутабельный [int] или None (без лимита)."""
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
        reject = _funnel(d, c, st, sig, whois_budget)

        if sig.get("acquirability_unresolved"):
            # приобретаемость не определена (whois сбой/непонятно/бюджет) — НЕ пишем,
            # домен остаётся discovered, следующий прогон перепробьёт (см. спек §D).
            return {"domain": d.domain, "status": d.status, "unresolved": True,
                    "errors": sig.get("errors", [])}

        if reject:
            result = {"score": 0.0, "status": "rejected", "breakdown": {"funnel_reject": reject}}
        else:
            sig.setdefault("referring_domains", d.referring_domains)
            result = compute_score(sig)
            if "hard_reject" not in result["breakdown"]:
                result = {**result, "status": _decide(result["score"], sig,
                                                      st["approve_at"], st["manual_review_at"])}

        d.lane = sig.get("lane") or d.lane
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

- [ ] **Step 6: score_pending — завести бюджет whois**

В `backend/app/services/scoring.py`, `score_pending`: добавить чтение настроек и бюджет:
```python
def score_pending(limit: int = 100, on_progress=None) -> int:
    """Score all `discovered` domains; return count processed. on_progress(done,total,current)."""
    from sqlalchemy import select
    from app.db import SessionLocal
    from app.models.domain import Domain
    from app.services.settings import get_settings

    st = get_settings()
    with SessionLocal() as db:
        rows = db.execute(
            select(Domain.id, Domain.domain).where(Domain.status == "discovered")
            .order_by(Domain.referring_domains.desc().nulls_last())  # лучшие кандидаты первыми
            .limit(limit)
        ).all()
    clients = _make_clients()
    whois_budget = [int(st["max_whois_per_run"])]   # общий на прогон: cctld (RD=null) идёт последним
    total = len(rows)
    for i, (did, name) in enumerate(rows, 1):
        if on_progress:
            on_progress(i - 1, total, name)
        score_domain(did, clients, whois_budget)
    if on_progress:
        on_progress(total, total, "")
    return total
```

- [ ] **Step 7: Прогнать — всё зелено (2×) + pyflakes**

Run: `.venv/bin/python -m pytest backend/tests/ -q && .venv/bin/python -m pytest backend/tests/ -q && .venv/bin/python -m pyflakes backend/app backend/tests`
Expected: одинаково зелёное оба раза (детерминизм), pyflakes чисто. Также проверить чистую функцию: `.venv/bin/python -m app.services.scoring` (из `backend/`, PYTHONPATH) печатает `scoring compute_score ok` — `compute_score` не тронут.

- [ ] **Step 8: Commit**

```bash
git add backend/app/services/scoring.py backend/tests/test_funnel.py
git commit -m "M1 приобретаемость: воронка T1 (лейн/not_acquirable/whois-fail→discovered) + whois-кап"
```

---

## Task 5: Цена бэкордера — get_tariffs + refresh_backorder_prices

**Files:**
- Modify: `backend/app/integrations/backorder.py` (`get_tariffs`)
- Create: `backend/app/services/pricing.py`
- Modify: `backend/app/services/discovery.py` (`_insert` ставит `acquire_price` из кэша)
- Test: `backend/tests/test_pricing.py`

**Interfaces:**
- Consumes: `Domain.acquire_price/price_checked_at` (Task 1), `Domain.source` (backorder).
- Produces: `BackorderClient.get_tariffs() -> dict` с ключом `price: float|None` (+`price_id`,`period_id`). `pricing.refresh_backorder_prices() -> int` (обновлённых доменов). `pricing.cached_backorder_price() -> float|None`.

- [ ] **Step 1: Тест цены**

`backend/tests/test_pricing.py`:
```python
def test_get_tariffs_parses_price(monkeypatch):
    from app.integrations import backorder
    c = backorder.BackorderClient()
    class _R:
        @staticmethod
        def json(): return {"id": "42", "period": [{"id": "7"}], "cost": "199.00"}
    monkeypatch.setattr(c, "request", lambda *a, **k: _R())
    t = c.get_tariffs()
    assert t["price"] == 199.0 and t["price_id"] == "42" and t["period_id"] == "7"


def test_refresh_prices_only_backorder(monkeypatch, sqlite_db):
    from app.services import pricing
    import app.db as db
    from app.models.domain import Domain
    monkeypatch.setattr("app.integrations.backorder.BackorderClient.get_tariffs",
                        lambda self: {"price": 199.0, "price_id": "42", "period_id": "7"})
    with db.SessionLocal() as s:
        s.add_all([Domain(domain="bo.ru", source="backorder", status="discovered"),
                   Domain(domain="fr.ru", source="cctld", status="discovered")])
        s.commit()
    assert pricing.refresh_backorder_prices() == 1              # только backorder-домен
    with db.SessionLocal() as s:
        bo = s.execute(_dom("bo.ru")).scalar_one(); fr = s.execute(_dom("fr.ru")).scalar_one()
    assert float(bo.acquire_price) == 199.0 and bo.price_checked_at is not None
    assert fr.acquire_price is None                              # сырой не трогаем


def _dom(name):
    from sqlalchemy import select
    from app.models.domain import Domain
    return select(Domain).where(Domain.domain == name)
```

- [ ] **Step 2: Прогнать — падает**

Run: `.venv/bin/python -m pytest backend/tests/test_pricing.py -q`
Expected: FAIL (`get_tariffs` NotImplemented / нет `pricing`).

- [ ] **Step 3: get_tariffs в backorder.py**

В `backend/app/integrations/backorder.py` заменить заглушку `get_tariffs`:
```python
    def get_tariffs(self) -> dict:
        """Базовая цена бэкордера .ru из публичного тарифного JSON (без auth).
        price_id=id, period_id=period[0].id, price — базовая стоимость. Поля цены сверить
        на живом ответе (спек §J): пробуем cost/price/sum."""
        r = self.request("GET", f"{self.base_url}/manimg/userdata/json/price_ru_backorder.ru.json")
        d = r.json() if hasattr(r, "json") else {}
        d = d[0] if isinstance(d, list) and d else d
        if not isinstance(d, dict):
            return {"price": None, "price_id": None, "period_id": None}
        period = d.get("period") or []
        price_raw = d.get("cost") or d.get("price") or d.get("sum")
        try:
            price = float(price_raw) if price_raw is not None else None
        except (TypeError, ValueError):
            price = None
        return {"price": price, "price_id": str(d.get("id") or "") or None,
                "period_id": str(period[0]["id"]) if period and isinstance(period[0], dict) else None}
```

- [ ] **Step 4: services/pricing.py**

`backend/app/services/pricing.py`:
```python
"""Цена выкупа бэкордер-доменов: базовый тариф (публичный JSON) → acquire_price.

Живую аукционную per-domain цену бесплатный фид не отдаёт (спек §L) — храним базовую.
Кэш тарифа на процесс, чтобы discovery проставлял цену при вставке без лишних запросов;
кнопка «Обновить цены» перечитывает и обновляет всех backorder-доменов.
"""
_TARIFF: dict = {"price": None}          # кэш на процесс; refresh перезаписывает


def cached_backorder_price() -> float | None:
    return _TARIFF.get("price")


def refresh_backorder_prices() -> int:
    """Перечитать тариф, проставить acquire_price/price_checked_at всем backorder-доменам.
    Возвращает число обновлённых. Дёшево (один публичный JSON), денег не тратит."""
    from datetime import datetime, timezone
    from sqlalchemy import select
    from app.db import SessionLocal
    from app.models.domain import Domain
    from app.integrations.backorder import BackorderClient

    price = BackorderClient().get_tariffs().get("price")
    _TARIFF["price"] = price
    if price is None:
        return 0
    now = datetime.now(timezone.utc)
    n = 0
    with SessionLocal() as db:
        for d in db.execute(select(Domain).where(Domain.source == "backorder")).scalars():
            d.acquire_price = price
            d.price_checked_at = now
            n += 1
        db.commit()
    return n
```

- [ ] **Step 5: discovery ставит цену из кэша**

В `backend/app/services/discovery.py`, в `_insert`, конструктор `Domain(...)` — добавить `acquire_price`:
```python
            acquire_price=(__import__("app.services.pricing", fromlist=["x"]).cached_backorder_price()
                           if candidates[n].get("source") == "backorder" else None),
```
(ленивый импорт, чтобы не тянуть pricing на уровне модуля.)

- [ ] **Step 6: Прогнать тесты + весь сьют + pyflakes**

Run: `.venv/bin/python -m pytest backend/tests/test_pricing.py -q && .venv/bin/python -m pytest backend/tests/ -q && .venv/bin/python -m pyflakes backend/app backend/tests`
Expected: зелёное, pyflakes чисто.

- [ ] **Step 7: Commit**

```bash
git add backend/app/integrations/backorder.py backend/app/services/pricing.py backend/app/services/discovery.py backend/tests/test_pricing.py
git commit -m "M1 приобретаемость: цена бэкордера (get_tariffs + refresh_backorder_prices, кэш)"
```

---

## Task 6: Индикаторы зависимостей — A-Parser + БД в /diag

**Files:**
- Modify: `backend/app/services/diagnostics.py` (`_spec`, +`_db_ping`)
- Test: `backend/tests/test_web_fixes.py` (или новый `test_diag.py`)

**Interfaces:**
- Consumes: `AParserClient.ping()` (уже есть), `SessionLocal`.
- Produces: `_spec()` включает записи с ключами `aparser` и `db`. `_db_ping() -> bool`.

- [ ] **Step 1: Тест на присутствие индикаторов**

В `backend/tests/test_web_fixes.py` добавить:
```python
def test_diag_includes_aparser_and_db():
    from app.services.diagnostics import _spec, _db_ping
    keys = {s[0] for s in _spec()}
    assert "aparser" in keys and "db" in keys
    assert _db_ping() is True            # SELECT 1 на тестовой SQLite отвечает
```

- [ ] **Step 2: Прогнать — падает**

Run: `.venv/bin/python -m pytest backend/tests/test_web_fixes.py -k "aparser_and_db" -q`
Expected: FAIL (`_db_ping` нет / ключей нет).

- [ ] **Step 3: Реализация в diagnostics.py**

В `backend/app/services/diagnostics.py`, перед `_spec` добавить:
```python
def _db_ping() -> bool:
    from sqlalchemy import text
    from app.db import SessionLocal
    with SessionLocal() as db:
        return db.execute(text("SELECT 1")).scalar() == 1
```
В `_spec()`, в возвращаемый список добавить две записи (A-Parser — после `searxng`; БД — в конец):
```python
        ("aparser", "A-Parser", "M1 · whois/лейн + fetch", settings.APARSER_API_KEY,
         lambda: __import__("app.integrations.aparser", fromlist=["x"]).AParserClient().ping()),
```
и последней строкой списка:
```python
        ("db", "PostgreSQL", "БД конвейера", settings.DATABASE_URL, _db_ping),
```

- [ ] **Step 4: Прогнать тест + весь сьют + pyflakes**

Run: `.venv/bin/python -m pytest backend/tests/test_web_fixes.py -q && .venv/bin/python -m pyflakes backend/app backend/tests`
Expected: PASS, pyflakes чисто.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/diagnostics.py backend/tests/test_web_fixes.py
git commit -m "M1: индикаторы A-Parser + БД в /diag (несущие для гейта приобретаемости)"
```

---

## Task 7: Панель — бейджи, колонки, дефолт-фильтр, полный прогон, цена, слайдер

**Files:**
- Modify: `backend/app/templates/base.html` (CSS `.src-badge`)
- Modify: `backend/app/templates/domains.html` (бейдж, колонки, кнопки, легенда)
- Modify: `backend/app/templates/settings.html` (слайдер max_whois_per_run)
- Modify: `backend/app/api/panel.py` (`domains_view` фильтр, `settings_save`, +роут refresh-prices)
- Test: `backend/tests/test_web_fixes.py`

**Interfaces:**
- Consumes: `Domain.lane/acquire_deadline/acquire_price/reject_reason` (Tasks 1,4), `pricing.refresh_backorder_prices` (Task 5), `get_settings()["max_whois_per_run"]` (Task 1).
- Produces: `/domains?show_all=1` показывает `not_acquirable`; POST `/admin/refresh-prices`; `settings_save` принимает `max_whois_per_run`.

- [ ] **Step 1: Тесты панели**

В `backend/tests/test_web_fixes.py` добавить:
```python
def test_domains_hides_not_acquirable_by_default(client, sqlite_db):
    import app.db as db
    from app.models.domain import Domain
    with db.SessionLocal() as s:
        s.add_all([
            Domain(domain="ok.ru", source="backorder", status="approved", lane="bid"),
            Domain(domain="no.ru", source="cctld", status="rejected", reject_reason="not_acquirable"),
        ]); s.commit()
    body = client.get("/domains").text
    assert "ok.ru" in body and "no.ru" not in body            # по умолчанию скрыт
    assert "no.ru" in client.get("/domains?show_all=1").text   # под фильтром виден


def test_refresh_prices_route(client, monkeypatch):
    monkeypatch.setattr("app.services.pricing.refresh_backorder_prices", lambda: 3)
    r = client.post("/admin/refresh-prices", follow_redirects=False)
    assert r.status_code == 303 and "3" in r.headers["location"]


def test_settings_save_accepts_max_whois(client, sqlite_db):
    from app.services.settings import get_settings
    client.post("/settings/save", data={
        "min_referring_domains": 1, "min_age_years": 3, "approve_at": 0.7,
        "manual_review_at": 0.4, "max_whois_per_run": 77, "backorder": "on"})
    assert get_settings()["max_whois_per_run"] == 77
```

- [ ] **Step 2: Прогнать — падает**

Run: `.venv/bin/python -m pytest backend/tests/test_web_fixes.py -k "not_acquirable or refresh_prices or max_whois" -q`
Expected: FAIL (фильтра/роута/поля нет).

- [ ] **Step 3: domains_view — скрыть not_acquirable по умолчанию**

В `backend/app/api/panel.py`, `domains_view` — добавить параметр `show_all` и фильтр. Заменить сигнатуру и начало (важно: `col != 'x'` в SQL НЕ вернёт строки с NULL `reject_reason`, поэтому приобретаемые = `IS NULL OR != not_acquirable` через `or_`, который в panel.py уже импортирован):
```python
@router.get("/domains", response_class=HTMLResponse)
def domains_view(request: Request, status: str | None = None, min_score: float | None = None,
                 limit: int = 200, show_all: bool = False, db: Session = Depends(get_session)):
    limit = max(1, min(limit, 1000))
    stmt = select(Domain)
    if status:
        stmt = stmt.where(Domain.status == status)
    elif not show_all:                          # по умолчанию только приобретаемые
        stmt = stmt.where(or_(Domain.reject_reason.is_(None),
                              Domain.reject_reason != "not_acquirable"))
    if min_score is not None:
        stmt = stmt.where(Domain.score >= min_score)
```
И в контекст шаблона (в `return templates.TemplateResponse`) добавить ключ:
```python
        "show_all": show_all,
```

- [ ] **Step 4: Роут обновления цен**

В `backend/app/api/panel.py`, рядом с другими `/admin/*` роутами добавить:
```python
@router.post("/admin/refresh-prices")
def refresh_prices_action():
    from app.services.pricing import refresh_backorder_prices
    n = refresh_backorder_prices()
    return _back("/domains", msg=f"Цены бэкордера обновлены: {n} доменов"
                 if n else "Цена бэкордера недоступна (тариф не прочитан)")
```

- [ ] **Step 5: settings_save принимает max_whois_per_run**

В `backend/app/api/panel.py`, `settings_save`: добавить параметр и проброс:
```python
def settings_save(min_referring_domains: int = Form(...), min_age_years: float = Form(...),
                  approve_at: float = Form(...), manual_review_at: float = Form(...),
                  max_whois_per_run: int = Form(200),
                  backorder: str = Form(""), cctld: str = Form(""),
                  reg_ru: str = Form(""), sweb: str = Form("")):
    from app.services import settings as st
    st.update_settings(min_referring_domains=min_referring_domains, min_age_years=min_age_years,
                       approve_at=approve_at, manual_review_at=manual_review_at,
                       max_whois_per_run=max_whois_per_run,
                       sources_enabled={"backorder": bool(backorder), "cctld": bool(cctld),
                                        "reg_ru": bool(reg_ru), "sweb": bool(sweb)})
    return _back("/settings", msg="Настройки сохранены")
```

- [ ] **Step 6: CSS бейджа в base.html**

В `backend/app/templates/base.html`, в блок `<style>` (рядом с `.badge`) добавить:
```css
  .src-badge { font-family:var(--mono); font-size:10.5px; font-weight:600; padding:1px 5px;
               border-radius:4px; margin-right:5px; letter-spacing:.3px; }
  .src-bid   { background:var(--amber); color:#fff; }                 /* backorder = ставка */
  .src-free  { background:var(--panel2); color:var(--mut); border:1px solid var(--line); }
```

- [ ] **Step 7: Бейдж, колонки, легенда в domains.html**

В `backend/app/templates/domains.html`:

(а) в ячейке домена — бейдж перед именем. Заменить `<td class="dom">{{ d.domain }}` на:
```html
      <td class="dom">
        <span class="src-badge {{ 'src-bid' if d.lane=='bid' else 'src-free' }}"
          title="источник: {{ d.source or '—' }} · лейн: {{ 'ставка' if d.lane=='bid' else 'свободный' if d.lane=='free' else '—' }}{% if d.acquire_deadline %} · дедлайн {{ d.acquire_deadline.strftime('%d.%m') }}{% endif %}{% if d.acquire_price %} · цена {{ '%.0f'|format(d.acquire_price|float) }}{% endif %}"
          >{{ {'backorder':'bo','cctld':'cc','reg_ru':'rg','sweb':'sw'}.get(d.source, (d.source or '?')[:2]) }}</span>{{ d.domain }}
```
(б) в `<thead>` после `<th>статус</th>` добавить `<th>лейн</th><th class="num">цена</th>`.
(в) в `<tbody>`, после ячейки статуса (`<td>...reject_reason...</td>`) добавить:
```html
      <td><span class="hint">{{ 'ставка' if d.lane=='bid' else 'свободный' if d.lane=='free' else '—' }}</span></td>
      <td class="num">{{ '%.0f'|format(d.acquire_price|float) if d.acquire_price is not none else '—' }}</td>
```
(г) кнопка полного прогона — ОТДЕЛЬНОЙ формой (не внутри батч-формы: иначе два `name="n"` и FastAPI возьмёт не тот). Сразу после закрывающего `</form>` батч-формы Score, внутри её `.go`:
```html
        <form method="post" action="/run/score" style="margin-left:10px">
          <input type="hidden" name="n" value="100000">
          <button class="btn-amber"
            title="прогнать проверку по всему пулу discovered (бюджет — max_whois_per_run на /settings)">Проверить весь пул</button>
        </form>
```
(д) в станции Discovery, в `.go` рядом с кнопкой Discovery — отдельная форма обновления цен:
```html
      <form method="post" action="/admin/refresh-prices" style="margin-top:6px">
        <button class="btn" title="перечитать базовую цену бэкордера и проставить приобретаемым">Обновить цены бэкордера</button>
      </form>
```
(е) в чипах (`<div class="chips">`) после чипа «все» добавить переключатель:
```html
  <a class="chip {{ 'on' if show_all }}" href="/domains?show_all=1"
     title="показать в том числе непокупаемые (not_acquirable)">+ непокупаемые</a>
```
(ж) в легенде, в описании rejected, добавить в перечень причин: `<code>not_acquirable</code> нельзя купить (занят, не на бэкордере)`.

- [ ] **Step 8: Слайдер max_whois_per_run в settings.html**

В `backend/app/templates/settings.html`, перед станцией «Источники дропов» добавить станцию:
```html
  <div class="station">
    <div class="plate">Кап whois за прогон — <b>защита от сырого cctld</b></div>
    <div class="what">За один запуск проверки пробьём whois не более этого числа доменов
      (лучшие по RD — первыми). Остальные подождут следующего прогона.</div>
    <div class="go">
      <input type="range" name="max_whois_per_run" min="0" max="2000" step="50"
             value="{{ s.max_whois_per_run }}"
             oninput="document.getElementById('v_whois').textContent=this.value" style="flex:1 1 200px">
      <b id="v_whois">{{ s.max_whois_per_run }}</b>
      <span class="hint">whois-вызовов за прогон максимум</span>
    </div>
  </div>
```

- [ ] **Step 9: Прогнать тесты + весь сьют + pyflakes**

Run: `.venv/bin/python -m pytest backend/tests/test_web_fixes.py -q && .venv/bin/python -m pytest backend/tests/ -q && .venv/bin/python -m pyflakes backend/app backend/tests`
Expected: зелёное, pyflakes чисто.

- [ ] **Step 10: Проверка глазами (Playwright)**

Отрендерить `/domains`, `/settings`, `/diag` (локальный serve статикой, как в прошлой итерации: скрипт-рендер через TestClient + http.server + Playwright-скриншот). Убедиться: бейдж `[bo]` оранжевый у backorder, серые у сырых; колонки лейн/цена; кнопка «Проверить весь пул»; слайдер капа; чип «+ непокупаемые»; в `/diag` строки A-Parser и PostgreSQL. Временные PNG удалить, не коммитить.

- [ ] **Step 11: Commit**

```bash
git add backend/app/templates/base.html backend/app/templates/domains.html backend/app/templates/settings.html backend/app/api/panel.py backend/tests/test_web_fixes.py
git commit -m "M1 приобретаемость: панель — бейдж источника + колонки лейн/цена + полный прогон + кап-слайдер + refresh цен"
```

---

## Финал

После Task 7 — общий whole-branch review (subagent-driven), затем обновить `CLAUDE.md` (состояние: Мозг M1 приобретаемости готов) и `git push origin main`.
