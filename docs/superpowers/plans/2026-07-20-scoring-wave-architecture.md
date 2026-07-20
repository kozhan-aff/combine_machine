# Волновая архитектура воронки скоринга — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Заменить последовательный по-доменный `_funnel()` в `services/scoring.py` на
настоящие волны (whois → risk → history → ahrefs), где КАЖДАЯ стадия прогоняется
конкурентно (`ThreadPoolExecutor`) на всём выжившем пуле, прежде чем начинается следующая
— вместо того чтобы каждый домен целиком проходил все стадии, один за другим.

**Architecture:** Домены пакета превращаются в список `FunnelState` (лёгкий снэпшот, не
ORM-объект). Каждая волна — функция `_wave_X(states, ...)`, мутирующая `state.alive`/
`state.reject_reason`/`state.sig` IN PLACE через общий конкурентный харнесс
`_run_concurrent()`; между волнами `_checkpoint()` коммитит в БД домены, вышедшие из
конвейера на этой волне (реджект/unresolved), и репортит прогресс/волновую историю.
`score_domain()` — внешний контракт НЕ меняется (используется тестами и панелью
напрямую): внутри он строит пакет из ОДНОГО `FunnelState` и прогоняет его через тот же
код волн.

**Tech Stack:** Python 3.12, `concurrent.futures.ThreadPoolExecutor`, `threading.Lock`,
SQLAlchemy 2.x, существующий `services/jobs.py` (реестр прогонов), pytest.

## Global Constraints

- Два хард-гейта проекта (деньги — `confirmed_by_human`; редактура — `edited`) этим
  планом НЕ затрагиваются — весь план внутри M1-скоринга, до выкупа.
- Границы конкурентности — ХАРДКОД-константы в коде, НЕ `/settings` (решение
  пользователя, зафиксировано в спеке §2): `_CONCURRENCY = {"whois": 12, "risk": 12,
  "history": 4, "ahrefs": 2}`.
- Внешний контракт `score_domain(domain_id: int, clients: dict | None = None,
  whois_budget=None, ahrefs_budget=None, run: int | None = None) -> dict` НЕ меняется —
  ни сигнатура, ни форма возвращаемого словаря, ни семантика `whois_budget`/
  `ahrefs_budget` как `[int]`-списков (легаси-контракт, использует `test_funnel.py`).
  Все ~31 существующих теста в `backend/tests/test_funnel.py` обязаны остаться зелёными
  БЕЗ ИЗМЕНЕНИЙ в самом файле теста.
- `integrations/` = только транспорт, логика — в `services/` (конвенция CLAUDE.md).
- UI и комментарии — на русском.
- Тесты — офлайн, SQLite-харнесс, `_no_live_network` рубильник (см.
  `backend/tests/conftest.py`) — новые тесты не должны требовать сеть.
- Коммиты — по завершении каждой задачи, с трейлером
  `Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>`.
- Дизайн: `docs/superpowers/specs/2026-07-20-scoring-wave-architecture-design.md`.

---

### Task 1: `FunnelState`, `Budget`, конкурентный харнесс, T0-волна

Фундамент: типы данных и общий код запуска волны, который переиспользуют ВСЕ следующие
задачи. `_wave_t0` — первая (и самая простая — без сети) волна, чтобы проверить харнесс
на реальном, но тривиальном случае.

**Files:**
- Modify: `backend/app/services/scoring.py` (добавить в конец файла, ПЕРЕД
  `if __name__ == "__main__":` на строке 1042)
- Test: `backend/tests/test_scoring_waves.py` (создать)

**Interfaces:**
- Produces:
  - `class FunnelState` — поля `domain_id: int`, `domain: str`, `lane: str | None`,
    `referring_domains: int | None`, `acquire_deadline: datetime | None`,
    `feed_flags: dict | None`, `sig: dict` (default `{"errors": []}`),
    `reject_reason: str | None` (default `None`), `unresolved_why: str | None`
    (default `None`), `alive: bool` (default `True`).
  - `class Budget` — `Budget(n: int)`, метод `.take() -> bool` (потокобезопасно).
  - `class _ListBudget` — адаптер `[int]`-списка (легаси-контракт `score_domain`) под тот
    же протокол `.take() -> bool`.
  - `_CONCURRENCY: dict[str, int]` — `{"whois": 12, "risk": 12, "history": 4, "ahrefs": 2}`.
  - `_run_concurrent(states: list[FunnelState], workers: int, run: int | None, stage: str,
    fn: Callable[[FunnelState], None]) -> None` — гоняет `fn` на всех `alive` состояниях
    пулом `workers` потоков, репортит прогресс, проверяет отмену.
  - `_wave_t0(states: list[FunnelState], st: dict) -> None`.

- [ ] **Step 1: Написать падающий тест на `Budget`/`_ListBudget`/`_wave_t0`**

```python
# backend/tests/test_scoring_waves.py
"""Волновой оркестратор скоринга: FunnelState, Budget, конкурентный харнесс, волны."""
import threading

from app.services import scoring


def test_budget_take_is_thread_safe_under_contention():
    """20 потоков разбирают бюджет в 10 — ровно 10 успешных take(), не больше и не меньше
    (гонка на невзвешенном инкременте дала бы >10 при обычном [int])."""
    budget = scoring.Budget(10)
    taken = []
    lock = threading.Lock()

    def worker():
        ok = budget.take()
        with lock:
            taken.append(ok)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert taken.count(True) == 10
    assert taken.count(False) == 10


def test_list_budget_adapts_legacy_list_in_place():
    """score_domain() принимает [int] снаружи (легаси-контракт) — адаптер обязан мутировать
    ТОТ ЖЕ список, не свою копию, иначе внешний вызывающий не увидит расход."""
    box = [2]
    b = scoring._ListBudget(box)
    assert b.take() is True and box == [1]
    assert b.take() is True and box == [0]
    assert b.take() is False and box == [0]


def test_wave_t0_rejects_feed_flag_and_low_rd_without_touching_alive_ones():
    st = {"min_referring_domains": 5}
    flagged = scoring.FunnelState(domain_id=1, domain="a.ru", lane=None,
                                  referring_domains=10, acquire_deadline=None,
                                  feed_flags={"rkn": True})
    low_rd = scoring.FunnelState(domain_id=2, domain="b.ru", lane=None,
                                 referring_domains=1, acquire_deadline=None,
                                 feed_flags=None)
    ok = scoring.FunnelState(domain_id=3, domain="c.ru", lane=None,
                             referring_domains=50, acquire_deadline=None,
                             feed_flags=None)
    states = [flagged, low_rd, ok]
    scoring._wave_t0(states, st)
    assert flagged.alive is False and flagged.reject_reason == "feed_flag"
    assert low_rd.alive is False and low_rd.reject_reason == "low_rd"
    assert ok.alive is True and ok.reject_reason is None
```

- [ ] **Step 2: Запустить тест, убедиться что падает**

Run: `docker compose run --rm backend pytest backend/tests/test_scoring_waves.py -v`
Expected: FAIL — `AttributeError: module 'app.services.scoring' has no attribute 'Budget'`
(и аналогично для `_ListBudget`/`FunnelState`/`_wave_t0`).

- [ ] **Step 3: Реализовать в `backend/app/services/scoring.py`**

Добавить `import threading` и `from concurrent.futures import ThreadPoolExecutor,
as_completed` к существующим импортам вверху файла (после `from datetime import
timedelta` на строке 9), и `from dataclasses import dataclass, field`.

Вставить перед строкой `if __name__ == "__main__":` (строка 1042):

```python
# ============================================================================
# Волновая архитектура (2026-07-20): дёшево->дорого волнами на ВЕСЬ пул, а не
# по-доменно. См. docs/superpowers/specs/2026-07-20-scoring-wave-architecture-design.md.
# ============================================================================

# Границы конкурентности — ХАРДКОД, не /settings (решение пользователя): "если домен
# занят whois — скипаем в этой волне" — сами лимиты волн оператор не крутит.
# history=4 — вежливость к archive.org (проектная ценность, не число для тюнинга).
# ahrefs=2 — капча за штуку, дорого и хрупко к нагрузке.
_CONCURRENCY = {"whois": 12, "risk": 12, "history": 4, "ahrefs": 2}


@dataclass
class FunnelState:
    """Домен на пути через волны — лёгкий снэпшот, НЕ ORM-объект: волны держат его в
    памяти между несколькими вызовами без открытой сессии/транзакции. Финализация
    (commit в БД) происходит ОТДЕЛЬНО, в момент выхода из конвейера (см. _commit_result)."""
    domain_id: int
    domain: str
    lane: str | None
    referring_domains: int | None
    acquire_deadline: "datetime | None"
    feed_flags: dict | None
    sig: dict = field(default_factory=lambda: {"errors": []})
    reject_reason: str | None = None
    unresolved_why: str | None = None
    alive: bool = True


class Budget:
    """Потокобезопасный счётчик бюджета — замена сегодняшнему [int] (безопасен только
    при последовательном доступе). N потоков волны конкурентно зовут .take(); ровно N
    успешных, если бюджет == N — не больше (голый `box[0] -= 1` под конкурентностью мог
    бы пропустить декремент из-за гонки read-modify-write)."""
    def __init__(self, n: int):
        self._n = n
        self._lock = threading.Lock()

    def take(self) -> bool:
        with self._lock:
            if self._n <= 0:
                return False
            self._n -= 1
            return True


class _ListBudget:
    """Адаптер легаси-контракта score_domain() ([int]-список) под протокол Budget.take().
    Мутирует ТОТ ЖЕ список (не копию) — вызывающий код (тесты, ручной вызов из панели)
    видит расход бюджета в своём списке, как и раньше."""
    def __init__(self, box: list):
        self._box = box

    def take(self) -> bool:
        if self._box[0] <= 0:
            return False
        self._box[0] -= 1
        return True


def _run_concurrent(states: list, workers: int, run: "int | None", stage: str, fn) -> None:
    """Гоняет fn(state) на всех ALIVE states пулом `workers` потоков. fn мутирует state
    IN PLACE (sig/reject_reason/unresolved_why/alive) и НЕ касается БД — коммит только
    в _checkpoint, после того как волна целиком завершилась.

    Прогресс: stage репортится ОДИН раз в начале (флип чипа волны в реестре) — done/total
    без stage= на каждом тике (report() с stage= делает лишний SELECT stages на КАЖДЫЙ
    вызов, см. jobs.py:330-334; сотни тиков волны не должны множить это на сотни
    SELECT'ов). Отмена проверяется после КАЖДОГО завершения — как и в сегодняшнем
    последовательном score_pending (по одной проверке на домен), просто теперь через
    as_completed вместо for-цикла.
    """
    from app.services import jobs
    alive = [s for s in states if s.alive]
    if not alive:
        return
    jobs.report(run, stage=stage, done=0, total=len(alive))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(fn, s): s for s in alive}
        done = 0
        for fut in as_completed(futures):
            s = futures[fut]
            try:
                fut.result()
            except Exception:  # noqa: BLE001 — сбой одного домена не топит волну
                logging.getLogger(__name__).exception("%s упал для %s", stage, s.domain)
            done += 1
            jobs.report(run, done=done, total=len(alive))
            if jobs.cancelled(run):
                ex.shutdown(wait=False, cancel_futures=True)
                raise jobs.Cancelled()


def _wave_t0(states: list, st: dict) -> None:
    """T0 — фид, без сети, мгновенно. Без пула: I/O нет, конкурентность не нужна."""
    for s in states:
        if not s.alive:
            continue
        if s.feed_flags and any(s.feed_flags.get(k) for k in ("rkn", "judicial", "block")):
            s.reject_reason = "feed_flag"
            s.alive = False
        elif (s.referring_domains is not None
              and s.referring_domains < st["min_referring_domains"]):
            s.reject_reason = "low_rd"
            s.alive = False
```

- [ ] **Step 4: Запустить тесты, убедиться что проходят**

Run: `docker compose run --rm backend pytest backend/tests/test_scoring_waves.py -v`
Expected: `4 passed`

- [ ] **Step 5: pyflakes + существующий сьют не сломан**

Run: `.venv/bin/python -m pyflakes backend/app backend/tests`
Expected: чисто (без новых предупреждений; `datetime`/`ThreadPoolExecutor`/`as_completed`
использованы — не unused import).

Run: `docker compose run --rm backend pytest backend/tests/test_funnel.py -q`
Expected: все существующие тесты по-прежнему проходят (код этой задачи не трогает
`_funnel`/`score_domain`).

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/scoring.py backend/tests/test_scoring_waves.py
git commit -m "$(cat <<'EOF'
feat(scoring): FunnelState/Budget/конкурентный харнесс + T0-волна

Фундамент волновой архитектуры (см. спеку 2026-07-20): FunnelState —
снэпшот домена между волнами, Budget — потокобезопасный счётчик,
_run_concurrent — общий харнесс пула+прогресса+отмены на все будущие
волны. _wave_t0 — первая волна на новом коде, без сети.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: `_wave_whois` — волна приобретаемости и возраста

Перенос T1 из `_funnel()` (сегодня `scoring.py:478-570`) на конкурентный харнесс.
Добавляет потокобезопасность предохранителям TCI/A-Parser whois (сегодня — простые
инстанс-атрибуты, безопасные только при последовательном вызове; под конкурентностью 12
воркеров гонка на `+= 1`/`= 0` могла бы занизить счётчик сбоев или залогировать
срабатывание предохранителя дважды).

**Files:**
- Modify: `backend/app/services/whois.py` (добавить `lock` параметр в `probe()`/
  `_aparser_whois()`)
- Modify: `backend/app/services/scoring.py` (добавить `_wave_whois`, добавить
  `"_whois_lock"` в `_make_clients()`)
- Test: `backend/tests/test_scoring_waves.py` (добавить)

**Interfaces:**
- Consumes: `FunnelState`, `Budget`/`_ListBudget`, `_run_concurrent`, `_CONCURRENCY` (Task 1);
  `acquirability_verdict(available, acquire_deadline, now, *, lane)` (уже существует,
  `scoring.py:338`); `_deadline_from_whois(existing, free_date, now, lane)` (уже
  существует, `scoring.py:412`); `whois_router.probe(domain, clients)` (модуль `whois.py`).
- Produces: `_wave_whois(states: list[FunnelState], clients: dict, budget, st: dict, run:
  int | None) -> None`. Мутирует `state.sig`, `state.acquire_deadline`,
  `state.reject_reason`, `state.unresolved_why`, `state.alive`.

- [ ] **Step 1: Написать падающий тест**

Добавить в `backend/tests/test_scoring_waves.py`:

```python
from datetime import datetime, timedelta, timezone


class _FakeAparserWhois:
    def __init__(self, available=False, created=None, fail_times=0):
        self.available = available
        self.created = created
        self.fail_times = fail_times
        self.calls = 0
        self.whois_failures = 0

    def whois_probe(self, domain):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError("timeout")
        return {"available": self.available, "created": self.created}

    def safebrowsing_check(self, domain):
        return False


def _clients_no_tci(**kw):
    return {"aparser": _FakeAparserWhois(**kw),
            "tci": type("T", (), {"handles": lambda self, d: False})(),
            "_whois_lock": threading.Lock()}


def test_wave_whois_rejects_too_young_bid_domain():
    st = {"min_age_years": 3.0}
    young = datetime.now(timezone.utc) - timedelta(days=200)
    s = scoring.FunnelState(domain_id=1, domain="young.ru", lane="bid",
                            referring_domains=5, acquire_deadline=None, feed_flags=None)
    clients = _clients_no_tci(available=False, created=young)
    scoring._wave_whois([s], clients, budget=None, st=st, run=None)
    assert s.alive is False and s.reject_reason == "too_young"


def test_wave_whois_marks_free_lane_and_survives():
    st = {"min_age_years": 3.0}
    old = datetime.now(timezone.utc) - timedelta(days=365 * 10)
    s = scoring.FunnelState(domain_id=2, domain="free.ru", lane=None,
                            referring_domains=5, acquire_deadline=None, feed_flags=None)
    clients = _clients_no_tci(available=True, created=old)
    scoring._wave_whois([s], clients, budget=None, st=st, run=None)
    assert s.alive is True and s.sig["lane"] == "free"


def test_wave_whois_budget_exhausted_marks_unresolved_without_network_call():
    st = {"min_age_years": 3.0}
    s = scoring.FunnelState(domain_id=3, domain="over.ru", lane=None,
                            referring_domains=5, acquire_deadline=None, feed_flags=None)
    aparser = _FakeAparserWhois(available=True)
    clients = {"aparser": aparser, "tci": type("T", (), {"handles": lambda self, d: False})(),
               "_whois_lock": threading.Lock()}
    budget = scoring.Budget(0)
    scoring._wave_whois([s], clients, budget=budget, st=st, run=None)
    assert s.alive is False and s.unresolved_why == "budget"
    assert aparser.calls == 0          # бюджет исчерпан ДО сети — вызова не было


def test_wave_whois_concurrent_batch_does_not_corrupt_breaker_counter():
    """20 доменов, whois всегда падает, конкурентность 12 — предохранитель обязан
    сработать РОВНО на пороге, счётчик не должен убегать выше лимита из-за гонки."""
    st = {"min_age_years": 3.0}
    states = [scoring.FunnelState(domain_id=i, domain=f"d{i}.ru", lane=None,
                                  referring_domains=5, acquire_deadline=None,
                                  feed_flags=None) for i in range(20)]
    aparser = _FakeAparserWhois(fail_times=10_000)  # всегда падает
    clients = {"aparser": aparser, "tci": type("T", (), {"handles": lambda self, d: False})(),
               "_whois_lock": threading.Lock()}
    scoring._wave_whois(states, clients, budget=None, st=st, run=None)
    assert all(not s.alive and s.unresolved_why == "whois_failed" for s in states)
    assert aparser.whois_failures <= 3    # предохранитель ограничивает, не растёт без края
```

- [ ] **Step 2: Запустить, убедиться что падает**

Run: `docker compose run --rm backend pytest backend/tests/test_scoring_waves.py -v -k wave_whois`
Expected: FAIL — `AttributeError: ... has no attribute '_wave_whois'`

- [ ] **Step 3: Добавить `lock` в `backend/app/services/whois.py`**

Добавить `import threading` и `from contextlib import nullcontext` к импортам вверху
файла (после `import logging` на строке 36).

Заменить функцию `_aparser_whois` (строки 46-63) на:

```python
def _aparser_whois(ap, domain: str, lock=None) -> dict:
    """whois_probe с предохранителем — см. модульный докстринг. `lock` — общий на волну
    (создаётся в _make_clients под ключом "_whois_lock"): под конкурентностью несколько
    потоков волны могут одновременно читать/писать ap.whois_failures на ОДНОМ инстансе
    клиента — голый += 1 не атомарен (LOAD/ADD/STORE — GIL может переключить поток между
    ними), гонка занижала бы счётчик или логировала срабатывание предохранителя дважды.
    lock=None (вызов вне волны, тесты) — используем no-op контекст, поведение как раньше."""
    cm = lock if lock is not None else nullcontext()
    with cm:
        breaker_open = getattr(ap, "whois_failures", 0) >= _APARSER_WHOIS_FAILURE_LIMIT
    if breaker_open:
        raise RuntimeError("A-Parser whois: предохранитель сработал, канал пропускается до конца прогона")
    try:
        pr = ap.whois_probe(domain)
        with cm:
            ap.whois_failures = 0          # канал жив — счётчик сбоев сброшен
        return pr
    except Exception:
        with cm:
            ap.whois_failures = getattr(ap, "whois_failures", 0) + 1
            tripped = ap.whois_failures >= _APARSER_WHOIS_FAILURE_LIMIT
        if tripped:
            _log.warning(
                "A-Parser whois: %d сбоев подряд — предохранитель сработал, "
                "до конца прогона канал пропускается", _APARSER_WHOIS_FAILURE_LIMIT)
        raise
```

Заменить функцию `probe` (строки 66-110) на:

```python
def probe(domain: str, clients: dict) -> dict:
    """{"available", "created", "free_date", "whois_source"}.

    Надмножество контракта AParserClient.whois_probe() — старые потребители
    ключей available/created не ломаются. whois_source показывает, ЧЕМ судили
    домен: сбой TCI молча не превращается в «A-Parser так решил».

    `clients.get("_whois_lock")` — общий лок волны (см. scoring._make_clients),
    None вне волны (одиночный score_domain, юнит-тесты) — nullcontext, поведение
    как раньше (потокобезопасность не нужна при последовательном вызове).

    Предохранитель (см. модульный докстринг): пока `tci.consecutive_failures`
    < `_TCI_FAILURE_LIMIT`, TCI пробуется как обычно; успешный ответ сбрасывает
    счётчик в 0. Как только порог достигнут — TCI больше не вызывается вовсе
    (даже для доменов, что ему принадлежат по зоне), и это ОДИН раз логируется
    в момент срабатывания — не на каждый последующий домен."""
    cm = clients.get("_whois_lock") or nullcontext()
    tci = clients.get("tci")
    if tci is not None and tci.handles(domain):
        with cm:
            breaker_open = getattr(tci, "consecutive_failures", 0) >= _TCI_FAILURE_LIMIT
        if breaker_open:
            source = "aparser_fallback"    # предохранитель уже сработал в этом прогоне — TCI не трогаем
        else:
            try:
                result = {**tci.probe(domain), "whois_source": "tci"}
                with cm:
                    tci.consecutive_failures = 0    # канал жив — счётчик сбоев сброшен
                return result
            except Exception as e:                # noqa: BLE001 — сбой канала, не приговор домену
                _log.warning("TCI whois сбой для %s (%s: %s) — фолбэк на A-Parser",
                             domain, type(e).__name__, e)
                with cm:
                    tci.consecutive_failures = getattr(tci, "consecutive_failures", 0) + 1
                    tripped = tci.consecutive_failures >= _TCI_FAILURE_LIMIT
                if tripped:
                    _log.warning(
                        "TCI whois: %d сбоев подряд — предохранитель сработал, "
                        "до конца прогона TCI пропускается, whois идёт через A-Parser",
                        _TCI_FAILURE_LIMIT)
                source = "aparser_fallback"
        pr = _aparser_whois(clients["aparser"], domain, cm if clients.get("_whois_lock") else None)
    else:
        source = "aparser"
        pr = _aparser_whois(clients["aparser"], domain, cm if clients.get("_whois_lock") else None)
    return {"available": pr.get("available"), "created": pr.get("created"),
            "free_date": None, "whois_source": source}
```

- [ ] **Step 4: Добавить `_wave_whois` в `backend/app/services/scoring.py`**

Добавить `"_whois_lock": threading.Lock()` в словарь, который возвращает `_make_clients()`
(строки 332-335):

```python
    return {
        "wayback": WaybackClient(), "rkn": RknClient(), "blacklist": BlacklistClient(),
        "searxng": SearxngClient(), "aparser": AParserClient(), "tci": TciWhoisClient(),
        "_whois_lock": threading.Lock(),
    }
```

Добавить после `_wave_t0` (конец Task 1):

```python
def _whois_one(s: FunnelState, clients: dict, budget, st: dict) -> None:
    """Тело T1 для ОДНОГО домена — вызывается конкурентно из _wave_whois. Прямой перенос
    сегодняшнего _funnel T1 (scoring.py, было строки 478-570), без изменения логики: budget
    вместо мутируемого [int], state вместо ORM Domain + sig."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)

    if budget is not None and not budget.take():
        s.unresolved_why = "budget"
        s.alive = False
        return

    age_known = False
    age = None
    try:
        pr = whois_router.probe(s.domain, clients)
    except Exception as e:  # noqa: BLE001
        s.sig["errors"].append(f"whois:{type(e).__name__}")
        if s.lane != "bid":
            s.unresolved_why = "whois_failed"
            s.alive = False
            return
        pr = {"available": None, "created": None}

    if pr.get("available") is not None:
        s.sig["acquirability_checked_at"] = now
    s.sig["whois_source"] = pr.get("whois_source")

    prev_deadline = s.acquire_deadline
    s.acquire_deadline = _deadline_from_whois(s.acquire_deadline, pr.get("free_date"), now, s.lane)
    if s.acquire_deadline != prev_deadline:
        s.sig["deadline_source"] = "whois_projection"

    wc = pr.get("created")
    s.sig["whois_created"] = wc
    if wc is not None:
        age_known = True
        age = (now - wc).days / 365.25
        s.sig["age_years"] = round(age, 2)
        s.sig["age_source"] = "whois"

    if s.lane == "bid":
        s.sig["lane"] = "bid"
        if age_known and age < st["min_age_years"]:
            s.reject_reason = "too_young"
            s.alive = False
        return

    v = acquirability_verdict(pr.get("available"), s.acquire_deadline, now, lane=s.lane)
    if v == "taken":
        s.reject_reason = "not_acquirable"
        s.alive = False
    elif v == "free":
        s.sig["lane"] = "free"
        if age_known and age < st["min_age_years"]:
            s.reject_reason = "too_young"
            s.alive = False
    else:
        s.unresolved_why = ("waiting" if v == "waiting"
                            else "whois_unclear" if pr.get("available") is None
                            else "taken_undated")
        s.alive = False


def _wave_whois(states: list, clients: dict, budget, st: dict, run) -> None:
    """T1 — приобретаемость + возраст, конкурентно на весь выживший после T0 пул."""
    _run_concurrent(states, _CONCURRENCY["whois"], run, "whois",
                    lambda s: _whois_one(s, clients, budget, st))
```

- [ ] **Step 5: Запустить тесты, убедиться что проходят**

Run: `docker compose run --rm backend pytest backend/tests/test_scoring_waves.py -v -k wave_whois`
Expected: `4 passed`

- [ ] **Step 6: Регрессия whois.py**

Run: `docker compose run --rm backend pytest backend/tests/test_whois_tci.py -q`
Expected: все существующие тесты проходят без изменений (новый `lock`-параметр
опционален, `clients.get("_whois_lock")` отсутствует в их фикстурах -> `nullcontext()`,
поведение идентично старому).

- [ ] **Step 7: pyflakes**

Run: `.venv/bin/python -m pyflakes backend/app backend/tests`
Expected: чисто.

- [ ] **Step 8: Commit**

```bash
git add backend/app/services/whois.py backend/app/services/scoring.py backend/tests/test_scoring_waves.py
git commit -m "$(cat <<'EOF'
feat(scoring): волна whois — конкурентный T1 + потокобезопасный предохранитель

_wave_whois переносит T1 (_funnel) на _run_concurrent (12 потоков).
whois.py::probe()/_aparser_whois() принимают опциональный lock — под
конкурентностью гонка на голом += 1 счётчика предохранителя могла
занизить его или залогировать срабатывание дважды; lock=None (вне
волны) — поведение как раньше.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: `_wave_risk` — волна РКН/блэклист/SafeBrowsing/эхо

Перенос T2 из `_funnel()` (сегодня `scoring.py:572-616`), включая существующий
предохранитель SafeBrowsing (`_APARSER_SAFEBROWSING_LIMIT`, строки 451-459, 590-608) —
та же потокобезопасность, что и в Task 2, но для `ap.safebrowsing_failures`.

**Files:**
- Modify: `backend/app/services/scoring.py`
- Test: `backend/tests/test_scoring_waves.py`

**Interfaces:**
- Consumes: `FunnelState`, `_run_concurrent`, `_CONCURRENCY` (Task 1); `clients["rkn"]
  .is_listed(domain)`, `clients["blacklist"].is_blacklisted(domain)`,
  `clients["aparser"].safebrowsing_check(domain)`, `clients["searxng"]
  .indexed_echo(domain)` (существующие интерфейсы клиентов, без изменений).
- Produces: `_wave_risk(states: list[FunnelState], clients: dict, run: int | None) ->
  None`.

- [ ] **Step 1: Написать падающий тест**

Добавить в `backend/tests/test_scoring_waves.py`:

```python
class _FakeRiskClients:
    def __init__(self, rkn=False, bl=False, sb=False, echo=True, sb_fail_times=0):
        self.rkn, self.bl, self.sb, self.echo = rkn, bl, sb, echo
        self.sb_fail_times = sb_fail_times
        self.sb_calls = 0
        self.safebrowsing_failures = 0

    def is_listed(self, d): return self.rkn
    def is_blacklisted(self, d): return self.bl
    def indexed_echo(self, d): return self.echo
    def safebrowsing_check(self, d):
        self.sb_calls += 1
        if self.sb_calls <= self.sb_fail_times:
            raise RuntimeError("timeout")
        return self.sb


def _risk_clients(**kw):
    ap = _FakeRiskClients(**kw)
    return {"rkn": ap, "blacklist": ap, "aparser": ap, "searxng": ap,
           "_safebrowsing_lock": threading.Lock()}


def test_wave_risk_rejects_rkn():
    s = scoring.FunnelState(domain_id=1, domain="a.ru", lane=None, referring_domains=5,
                            acquire_deadline=None, feed_flags=None)
    scoring._wave_risk([s], _risk_clients(rkn=True), run=None)
    assert s.alive is False and s.reject_reason == "rkn"


def test_wave_risk_fills_echo_without_rejecting():
    s = scoring.FunnelState(domain_id=2, domain="b.ru", lane=None, referring_domains=5,
                            acquire_deadline=None, feed_flags=None)
    scoring._wave_risk([s], _risk_clients(echo=True), run=None)
    assert s.alive is True and s.sig["indexed_echo"] is True


def test_wave_risk_safebrowsing_breaker_survives_concurrency():
    states = [scoring.FunnelState(domain_id=i, domain=f"d{i}.ru", lane=None,
                                  referring_domains=5, acquire_deadline=None,
                                  feed_flags=None) for i in range(20)]
    clients = _risk_clients(sb_fail_times=10_000)
    scoring._wave_risk(states, clients, run=None)
    assert clients["aparser"].safebrowsing_failures <= 3
    assert all(s.alive for s in states)   # SafeBrowsing-сбой не отбраковывает, только errors
    assert any("safebrowsing:" in e for s in states for e in s.sig["errors"])
```

- [ ] **Step 2: Запустить, убедиться что падает**

Run: `docker compose run --rm backend pytest backend/tests/test_scoring_waves.py -v -k wave_risk`
Expected: FAIL — `AttributeError: ... has no attribute '_wave_risk'`

- [ ] **Step 3: Реализовать `_wave_risk`**

Добавить в `backend/app/services/scoring.py`, после `_wave_whois`:

```python
def _risk_one(s: FunnelState, clients: dict, sb_lock) -> None:
    """Тело T2 для ОДНОГО домена: РКН -> блэклист -> SafeBrowsing (с предохранителем,
    та же схема, что _APARSER_SAFEBROWSING_LIMIT в _funnel) -> indexed_echo. Прямой
    перенос scoring.py T2 (было строки 572-616): rkn/blacklist отбраковывают, echo — нет."""
    from contextlib import nullcontext
    cm = sb_lock if sb_lock is not None else nullcontext()
    try:
        s.sig["rkn_listed"] = clients["rkn"].is_listed(s.domain)
        if s.sig["rkn_listed"]:
            s.reject_reason = "rkn"
            s.alive = False
            return
    except Exception as e:  # noqa: BLE001
        s.sig["errors"].append(f"rkn:{type(e).__name__}")
    try:
        s.sig["blacklisted"] = clients["blacklist"].is_blacklisted(s.domain)
        if s.sig["blacklisted"] is True:
            s.reject_reason = "blacklist"
            s.alive = False
            return
    except Exception as e:  # noqa: BLE001
        s.sig["errors"].append(f"blacklist:{type(e).__name__}")
    if s.sig.get("blacklisted") is None and "blacklisted" in s.sig:
        s.sig["errors"].append("blacklist:unavailable")

    ap = clients["aparser"]
    with cm:
        breaker_open = getattr(ap, "safebrowsing_failures", 0) >= _APARSER_SAFEBROWSING_LIMIT
    if breaker_open:
        s.sig["errors"].append("safebrowsing:circuit_open")
    else:
        try:
            s.sig["safebrowsing_flagged"] = ap.safebrowsing_check(s.domain)
            with cm:
                ap.safebrowsing_failures = 0
            if s.sig["safebrowsing_flagged"] is True:
                s.reject_reason = "safebrowsing"
                s.alive = False
                return
        except Exception as e:  # noqa: BLE001
            s.sig["errors"].append(f"safebrowsing:{type(e).__name__}")
            with cm:
                ap.safebrowsing_failures = getattr(ap, "safebrowsing_failures", 0) + 1
                tripped = ap.safebrowsing_failures >= _APARSER_SAFEBROWSING_LIMIT
            if tripped:
                logging.getLogger(__name__).warning(
                    "A-Parser SafeBrowsing: %d сбоев подряд — предохранитель сработал, "
                    "до конца прогона пропускается", _APARSER_SAFEBROWSING_LIMIT)
        if s.sig.get("safebrowsing_flagged") is None and "safebrowsing_flagged" in s.sig:
            s.sig["errors"].append("safebrowsing:unavailable")

    try:
        s.sig["indexed_echo"] = clients["searxng"].indexed_echo(s.domain)
    except Exception as e:  # noqa: BLE001
        s.sig["errors"].append(f"searxng:{type(e).__name__}")


def _wave_risk(states: list, clients: dict, run) -> None:
    """T2 — РКН/блэклист/SafeBrowsing/эхо, конкурентно на весь выживший после whois пул.
    Эхо не отбраковывает (сигнал score, не гейт), но живёт в этой же волне — тот же
    сетевой поход, отдельный пул был бы лишней сложностью без причины."""
    lock = clients.get("_safebrowsing_lock")
    _run_concurrent(states, _CONCURRENCY["risk"], run, "risk",
                    lambda s: _risk_one(s, clients, lock))
```

Добавить `"_safebrowsing_lock": threading.Lock()` в `_make_clients()` рядом с
`"_whois_lock"` (та же правка, что в Task 2, строка теперь):

```python
    return {
        "wayback": WaybackClient(), "rkn": RknClient(), "blacklist": BlacklistClient(),
        "searxng": SearxngClient(), "aparser": AParserClient(), "tci": TciWhoisClient(),
        "_whois_lock": threading.Lock(), "_safebrowsing_lock": threading.Lock(),
    }
```

- [ ] **Step 4: Запустить тесты**

Run: `docker compose run --rm backend pytest backend/tests/test_scoring_waves.py -v -k wave_risk`
Expected: `3 passed`

- [ ] **Step 5: Регрессия + pyflakes**

Run: `docker compose run --rm backend pytest backend/tests/test_funnel.py -q`
Run: `.venv/bin/python -m pyflakes backend/app backend/tests`
Expected: всё чисто, `_funnel`/`score_domain` этой задачей не тронуты.

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/scoring.py backend/tests/test_scoring_waves.py
git commit -m "$(cat <<'EOF'
feat(scoring): волна risk — РКН/блэклист/SafeBrowsing/эхо конкурентно

_wave_risk переносит T2 на _run_concurrent (12 потоков), включая
предохранитель SafeBrowsing под тем же потокобезопасным локом, что
whois-предохранитель (Task 2). Эхо (indexed_echo) живёт в этой же
волне — не отбраковывает, но тот же сетевой поход.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: `_wave_history` — волна Wayback

Перенос T3 (`scoring.py:617-646`), включая пост-историйный фолбэк-гейт возраста.
Конкурентность 4 — жёсткий, некрутящийся потолок вежливости к archive.org.

**Files:**
- Modify: `backend/app/services/scoring.py`
- Test: `backend/tests/test_scoring_waves.py`

**Interfaces:**
- Consumes: `FunnelState`, `_run_concurrent`, `_CONCURRENCY` (Task 1); `clients["wayback"]
  .classify_history(domain) -> dict` (существующий интерфейс, без изменений);
  `cfg.HARD_REJECT_FLAGS` (`scoring_config.py:24`).
- Produces: `_wave_history(states: list[FunnelState], clients: dict, st: dict, run: int
  | None) -> None`.

- [ ] **Step 1: Написать падающий тест**

Добавить в `backend/tests/test_scoring_waves.py`:

```python
class _FakeWayback:
    def __init__(self, dirty=False, age_years=9.0, checked=True):
        self.dirty, self.age_years, self.checked = dirty, age_years, checked
        self.calls = 0

    def classify_history(self, domain):
        self.calls += 1
        pf = {"adult": False, "pharma": False, "casino": self.dirty,
              "gambling": False, "spam": False}
        return {"prior_flags": pf, "first_seen": None, "age_years": self.age_years,
                "wayback_checked": self.checked, "sampled": 5}


def test_wave_history_rejects_dirty():
    s = scoring.FunnelState(domain_id=1, domain="a.ru", lane=None, referring_domains=5,
                            acquire_deadline=None, feed_flags=None)
    st = {"min_age_years": 3.0}
    scoring._wave_history([s], {"wayback": _FakeWayback(dirty=True)}, st, run=None)
    assert s.alive is False and s.reject_reason == "history_dirty"


def test_wave_history_age_fallback_rejects_too_young_when_whois_had_no_age():
    s = scoring.FunnelState(domain_id=2, domain="b.ru", lane=None, referring_domains=5,
                            acquire_deadline=None, feed_flags=None)
    st = {"min_age_years": 3.0}
    scoring._wave_history([s], {"wayback": _FakeWayback(age_years=1.0)}, st, run=None)
    assert s.alive is False and s.reject_reason == "too_young"
    assert s.sig["age_source"] == "wayback"


def test_wave_history_keeps_whois_age_over_wayback_fallback():
    s = scoring.FunnelState(domain_id=3, domain="c.ru", lane=None, referring_domains=5,
                            acquire_deadline=None, feed_flags=None)
    s.sig["whois_created"] = "2010-01-01"      # whois УЖЕ дал возраст — Wayback не должен его затирать
    st = {"min_age_years": 3.0}
    scoring._wave_history([s], {"wayback": _FakeWayback(age_years=1.0)}, st, run=None)
    assert s.alive is True
    assert "age_source" not in s.sig or s.sig.get("age_source") != "wayback"
```

- [ ] **Step 2: Запустить, убедиться что падает**

Run: `docker compose run --rm backend pytest backend/tests/test_scoring_waves.py -v -k wave_history`
Expected: FAIL — `AttributeError: ... has no attribute '_wave_history'`

- [ ] **Step 3: Реализовать `_wave_history`**

Добавить в `backend/app/services/scoring.py`, после `_wave_risk`:

```python
def _history_one(s: FunnelState, clients: dict, st: dict) -> None:
    """Тело T3 для ОДНОГО домена: Wayback-история + категорийный hard-reject + фолбэк
    возраста (только если whois его не дал). Прямой перенос T3 (было строки 617-646)."""
    try:
        hist = clients["wayback"].classify_history(s.domain)
        pf = hist.get("prior_flags") or {}
        s.sig["prior_flags"] = pf
        s.sig["wayback_checked"] = hist.get("wayback_checked")
        s.sig["history_evidence"] = hist.get("evidence") or []
        s.sig["sampled"] = hist.get("sampled")
        s.sig["first_seen"] = hist.get("first_seen")
        if s.sig.get("whois_created") is None and hist.get("age_years") is not None:
            s.sig["age_years"] = hist["age_years"]
            s.sig["age_source"] = "wayback"
        if any(pf.get(k) for k in cfg.HARD_REJECT_FLAGS):
            s.reject_reason = "history_dirty"
            s.alive = False
            return
    except Exception as e:  # noqa: BLE001
        s.sig["errors"].append(f"wayback:{type(e).__name__}")

    # непроверяемый по whois возраст всё равно проходит гейт молодости (ПОСЛЕ history_dirty)
    if (s.sig.get("whois_created") is None
            and s.sig.get("age_years") is not None
            and s.sig["age_years"] < st["min_age_years"]):
        s.reject_reason = "too_young"
        s.alive = False


def _wave_history(states: list, clients: dict, st: dict, run) -> None:
    """T3 — Wayback-история, конкурентно на весь выживший после risk пул. Конкурентность
    жёстко 4 — вежливость к archive.org, некрутящаяся константа (не /settings)."""
    _run_concurrent(states, _CONCURRENCY["history"], run, "history",
                    lambda s: _history_one(s, clients, st))
```

- [ ] **Step 4: Запустить тесты**

Run: `docker compose run --rm backend pytest backend/tests/test_scoring_waves.py -v -k wave_history`
Expected: `3 passed`

- [ ] **Step 5: Регрессия + pyflakes**

Run: `docker compose run --rm backend pytest backend/tests/test_funnel.py -q`
Run: `.venv/bin/python -m pyflakes backend/app backend/tests`

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/scoring.py backend/tests/test_scoring_waves.py
git commit -m "$(cat <<'EOF'
feat(scoring): волна history — Wayback конкурентно, потолок вежливости 4

_wave_history переносит T3 на _run_concurrent (жёстко 4 потока —
вежливость к archive.org, не крутящаяся настройка). Категорийный
hard-reject и пост-историйный фолбэк возраста — без изменения логики.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: `_wave_ahrefs` — волна платного ссылочного профиля

Перенос T3b (`scoring.py:647-661`). Единственная волна, где по умолчанию бюджет = None
(значит "не вызывать вовсе"), в отличие от whois (`None` = безлимит).

**Files:**
- Modify: `backend/app/services/scoring.py`
- Test: `backend/tests/test_scoring_waves.py`

**Interfaces:**
- Consumes: `FunnelState`, `Budget`/`_ListBudget`, `_run_concurrent`, `_CONCURRENCY`
  (Task 1); `clients["aparser"].ahrefs_probe(domain) -> {"dr", "backlinks",
  "referring_domains"}` (существующий интерфейс).
- Produces: `_wave_ahrefs(states: list[FunnelState], clients: dict, budget, run: int |
  None) -> None`.

- [ ] **Step 1: Написать падающий тест**

Добавить в `backend/tests/test_scoring_waves.py`:

```python
class _FakeAhrefs:
    def __init__(self, dr=5.0, backlinks=100, rd=50, raises=False):
        self.dr, self.backlinks, self.rd, self.raises = dr, backlinks, rd, raises
        self.calls = 0

    def ahrefs_probe(self, domain):
        self.calls += 1
        if self.raises:
            raise RuntimeError("captcha failed")
        return {"dr": self.dr, "backlinks": self.backlinks, "referring_domains": self.rd}


def test_wave_ahrefs_skips_domain_with_feed_rd():
    s = scoring.FunnelState(domain_id=1, domain="a.ru", lane=None, referring_domains=500,
                            acquire_deadline=None, feed_flags=None)
    ap = _FakeAhrefs()
    scoring._wave_ahrefs([s], {"aparser": ap}, scoring.Budget(50), run=None)
    assert ap.calls == 0 and "dr" not in s.sig


def test_wave_ahrefs_probes_when_feed_has_no_rd_and_budget_available():
    s = scoring.FunnelState(domain_id=2, domain="b.ru", lane=None, referring_domains=None,
                            acquire_deadline=None, feed_flags=None)
    ap = _FakeAhrefs(dr=7.0)
    scoring._wave_ahrefs([s], {"aparser": ap}, scoring.Budget(50), run=None)
    assert ap.calls == 1 and s.sig["dr"] == 7.0


def test_wave_ahrefs_none_budget_means_disabled():
    s = scoring.FunnelState(domain_id=3, domain="c.ru", lane=None, referring_domains=None,
                            acquire_deadline=None, feed_flags=None)
    ap = _FakeAhrefs()
    scoring._wave_ahrefs([s], {"aparser": ap}, budget=None, run=None)
    assert ap.calls == 0


def test_wave_ahrefs_failure_does_not_reject():
    s = scoring.FunnelState(domain_id=4, domain="d.ru", lane=None, referring_domains=None,
                            acquire_deadline=None, feed_flags=None)
    ap = _FakeAhrefs(raises=True)
    scoring._wave_ahrefs([s], {"aparser": ap}, scoring.Budget(50), run=None)
    assert s.alive is True
    assert any("ahrefs:" in e for e in s.sig["errors"])
```

- [ ] **Step 2: Запустить, убедиться что падает**

Run: `docker compose run --rm backend pytest backend/tests/test_scoring_waves.py -v -k wave_ahrefs`
Expected: FAIL — `AttributeError: ... has no attribute '_wave_ahrefs'`

- [ ] **Step 3: Реализовать `_wave_ahrefs`**

Добавить в `backend/app/services/scoring.py`, после `_wave_history`:

```python
def _ahrefs_one(s: FunnelState, clients: dict, budget) -> None:
    """Тело T3b для ОДНОГО домена: Ahrefs ТОЛЬКО если фид не дал RD и бюджет жив.
    Прямой перенос T3b (было строки 647-661). Никогда не отбраковывает."""
    if s.referring_domains is not None or budget is None or not budget.take():
        return
    try:
        ah = clients["aparser"].ahrefs_probe(s.domain)
        s.sig["dr"] = ah["dr"]
        s.sig["ahrefs_backlinks"] = ah["backlinks"]
        if ah["referring_domains"] is not None:
            s.sig["referring_domains"] = ah["referring_domains"]
    except Exception as e:  # noqa: BLE001
        s.sig["errors"].append(f"ahrefs:{type(e).__name__}")


def _wave_ahrefs(states: list, clients: dict, budget, run) -> None:
    """T3b — Ahrefs, конкурентно (потолок 2 — капча за штуку, дорого и хрупко к нагрузке)
    на весь выживший после history пул. budget=None -> волна не вызывает Ahrefs вовсе
    (отличие от whois, где None = безлимит) — Ahrefs платный, дефолт "выключено"."""
    _run_concurrent(states, _CONCURRENCY["ahrefs"], run, "ahrefs",
                    lambda s: _ahrefs_one(s, clients, budget))
```

- [ ] **Step 4: Запустить тесты**

Run: `docker compose run --rm backend pytest backend/tests/test_scoring_waves.py -v -k wave_ahrefs`
Expected: `4 passed`

- [ ] **Step 5: Регрессия + pyflakes**

Run: `docker compose run --rm backend pytest backend/tests/test_funnel.py -q`
Run: `.venv/bin/python -m pyflakes backend/app backend/tests`

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/scoring.py backend/tests/test_scoring_waves.py
git commit -m "$(cat <<'EOF'
feat(scoring): волна ahrefs — платный ссылочный профиль конкурентно

_wave_ahrefs переносит T3b на _run_concurrent (потолок 2 — капча за
штуку). budget=None означает "не вызывать вовсе" (в отличие от whois,
где None = безлимит) — Ahrefs платный, дефолт выключен.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: `_commit_result` — финализация в БД

Перенос хвоста сегодняшнего `score_domain()` (после вызова `_funnel`, строки 684-811) в
отдельную функцию, принимающую `FunnelState` вместо ORM `Domain`+`sig`. Это единственное
место, где волновой код касается БД.

**Files:**
- Modify: `backend/app/services/scoring.py`
- Test: `backend/tests/test_scoring_waves.py`

**Interfaces:**
- Consumes: `FunnelState` (Task 1); `compute_score(sig, weights)` (существует,
  `scoring.py:276`); `_decide(score, sig, approve_at, manual_review_at)` (существует,
  `scoring.py:231`); `_jsonable(v)` (существует, `scoring.py:39`).
- Produces: `_commit_result(state: FunnelState, run: int | None, st: dict) -> dict` —
  возвращает СЛОВАРЬ В ТОЙ ЖЕ ФОРМЕ, что сегодняшний `score_domain()` (три варианта:
  `{"domain", "status", "skipped": "status"}` / `{"domain", "status", "unresolved": True,
  "why", "errors"}` / `{"domain", **result, "reject_reason", "errors"}`).

- [ ] **Step 1: Написать падающий тест**

Добавить в `backend/tests/test_scoring_waves.py`:

```python
import app.db as db
from app.models.domain import Domain
from app.models.domain_score_log import DomainScoreLog


def _mk_domain(**kw):
    with db.SessionLocal() as s:
        d = Domain(domain=kw.pop("domain", "commit.ru"), source="cctld",
                   status="discovered", **kw)
        s.add(d); s.commit(); s.refresh(d)
        return d.id


def test_commit_result_writes_rejected_and_log_row():
    did = _mk_domain()
    s = scoring.FunnelState(domain_id=did, domain="commit.ru", lane=None,
                            referring_domains=5, acquire_deadline=None, feed_flags=None)
    s.reject_reason = "rkn"
    s.alive = False
    out = scoring._commit_result(s, run=None, st={"approve_at": 0.7, "manual_review_at": 0.4})
    assert out["status"] == "rejected" and out["reject_reason"] == "rkn"
    with db.SessionLocal() as sess:
        d = sess.get(Domain, did)
        assert d.status == "rejected" and d.reject_reason == "rkn"
        log = sess.query(DomainScoreLog).filter_by(domain_id=did).one()
        assert log.outcome == "rejected" and log.reject_reason == "rkn"


def test_commit_result_writes_unresolved_and_leaves_domain_discovered():
    did = _mk_domain()
    s = scoring.FunnelState(domain_id=did, domain="commit2.ru", lane=None,
                            referring_domains=5, acquire_deadline=None, feed_flags=None)
    s.unresolved_why = "waiting"
    s.alive = False
    out = scoring._commit_result(s, run=None, st={"approve_at": 0.7, "manual_review_at": 0.4})
    assert out["unresolved"] is True and out["why"] == "waiting"
    with db.SessionLocal() as sess:
        d = sess.get(Domain, did)
        assert d.status == "discovered"


def test_commit_result_computes_score_for_survivor():
    did = _mk_domain()
    s = scoring.FunnelState(domain_id=did, domain="commit3.ru", lane=None,
                            referring_domains=5000, acquire_deadline=None, feed_flags=None)
    s.sig.update({"wayback_checked": True, "prior_flags": {}, "age_years": 10,
                 "indexed_echo": True, "dr": None})
    out = scoring._commit_result(s, run=None, st={"approve_at": 0.7, "manual_review_at": 0.4})
    assert out["status"] in ("approved", "scored") and out["score"] > 0
    with db.SessionLocal() as sess:
        d = sess.get(Domain, did)
        assert d.score == out["score"] and d.status == out["status"]
```

- [ ] **Step 2: Запустить, убедиться что падает**

Run: `docker compose run --rm backend pytest backend/tests/test_scoring_waves.py -v -k commit_result`
Expected: FAIL — `AttributeError: ... has no attribute '_commit_result'`

- [ ] **Step 3: Реализовать `_commit_result`**

Добавить в `backend/app/services/scoring.py`, после `_wave_ahrefs`:

```python
def _commit_result(state: FunnelState, run, st: dict) -> dict:
    """Записать итог ОДНОГО FunnelState в БД — прямой перенос хвоста сегодняшнего
    score_domain() (после вызова _funnel, было строки 684-811), но принимает state
    вместо только что вычисленного sig/reject внутри той же функции: волны финализируют
    домен в момент его выхода из конвейера (см. _run_waves), не в конце одной функции.

    Открывает СВОЮ сессию — тот же паттерн, что и раньше: разные domain_id — разные
    строки, конкурентная запись безопасна."""
    from datetime import datetime, timezone
    from app.db import SessionLocal
    from app.models.domain import Domain
    from app.models.domain_score_log import DomainScoreLog

    sig, reject = state.sig, state.reject_reason
    with SessionLocal() as db:
        d = db.get(Domain, state.domain_id)
        if d is None or d.status not in ("discovered", "scored", "rejected"):
            return {"domain": state.domain, "status": d.status if d else "gone",
                    "skipped": "status"}

        if state.unresolved_why is not None:
            if sig.get("acquirability_checked_at"):
                d.acquirability_checked_at = sig["acquirability_checked_at"]
            if sig.get("deadline_source"):
                d.score_breakdown = {**(d.score_breakdown or {}),
                                     "deadline_source": sig["deadline_source"]}
            if state.acquire_deadline != d.acquire_deadline:
                d.acquire_deadline = state.acquire_deadline
            db.add(DomainScoreLog(domain_id=d.id, run_id=run, outcome="unresolved",
                                  reject_reason=None, score=None, sig=_jsonable(sig)))
            db.commit()
            return {"domain": d.domain, "status": d.status, "unresolved": True,
                    "why": state.unresolved_why, "errors": sig.get("errors", [])}

        if state.acquire_deadline != d.acquire_deadline:
            d.acquire_deadline = state.acquire_deadline

        if reject:
            result = {"score": 0.0, "status": "rejected", "breakdown": {"funnel_reject": reject}}
        else:
            sig.setdefault("referring_domains", d.referring_domains)
            sig.setdefault("dr", float(d.dr) if d.dr is not None else None)
            result = compute_score(sig, st.get("weights"))
            if "hard_reject" not in result["breakdown"]:
                result = {**result, "status": _decide(result["score"], sig,
                                                      st["approve_at"], st["manual_review_at"])}

        for col in ("lane", "whois_created", "acquirability_checked_at", "prior_flags",
                    "wayback_checked", "first_seen", "age_years", "rkn_listed", "blacklisted",
                    "indexed_echo", "dr", "referring_domains"):
            v = sig.get(col)
            if v is not None:
                setattr(d, col, v)
        d.clean = result["status"] != "rejected"
        d.score = result["score"]
        prev = d.score_breakdown or {}

        def _kept(key):
            v = sig.get(key)
            return v if v is not None else prev.get(key)

        d.score_breakdown = {**result["breakdown"], "errors": sig.get("errors", []),
                             "ahrefs_backlinks": _kept("ahrefs_backlinks"),
                             "history_evidence": _kept("history_evidence") or [],
                             "sampled": _kept("sampled"),
                             "age_source": _kept("age_source"),
                             "whois_source": _kept("whois_source"),
                             "deadline_source": _kept("deadline_source")}
        d.status = result["status"]
        d.reject_reason = reject or ("low_score" if result["status"] == "rejected" else None)
        d.scored_at = datetime.now(timezone.utc)
        db.add(DomainScoreLog(
            domain_id=d.id, run_id=run,
            outcome="rejected" if result["status"] == "rejected" else "scored",
            reject_reason=d.reject_reason, score=result["score"], sig=_jsonable(sig)))
        db.commit()
        return {"domain": d.domain, **result, "reject_reason": d.reject_reason,
                "errors": sig.get("errors", [])}
```

- [ ] **Step 4: Запустить тесты**

Run: `docker compose run --rm backend pytest backend/tests/test_scoring_waves.py -v -k commit_result`
Expected: `3 passed`

- [ ] **Step 5: Регрессия + pyflakes**

Run: `docker compose run --rm backend pytest backend/tests/test_funnel.py -q`
Run: `.venv/bin/python -m pyflakes backend/app backend/tests`

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/scoring.py backend/tests/test_scoring_waves.py
git commit -m "$(cat <<'EOF'
feat(scoring): _commit_result — финализация FunnelState в БД

Перенос хвоста score_domain() (запись rejected/unresolved/scored +
DomainScoreLog) в отдельную функцию, принимающую FunnelState. Волны
финализируют домен в момент выхода из конвейера, не в конце одной
монолитной функции — см. следующую задачу (_run_waves).

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 7: `_run_waves` — оркестратор волн + волновая история

Собирает волны (Task 1-5) и финализацию (Task 6) в единый конвейер: гоняет волны по
порядку, между каждой коммитит вышедших (реджект/unresolved), выживших передаёт дальше,
пишет волновую историю в `job_run.message`.

**Files:**
- Modify: `backend/app/services/scoring.py`
- Test: `backend/tests/test_scoring_waves.py`

**Interfaces:**
- Consumes: все волны (Task 1-5), `_commit_result` (Task 6), `jobs.report`/
  `jobs.cancelled`/`jobs.Cancelled` (`services/jobs.py`, без изменений).
- Produces: `_run_waves(states: list[FunnelState], clients: dict, st: dict, whois_budget,
  ahrefs_budget, run: int | None) -> list[dict]` — список результатов `_commit_result` в
  порядке завершения (не важен вызывающему — `score_pending` использует только длину/факт
  прогона, `score_domain` — единственный элемент).

- [ ] **Step 1: Написать падающий тест**

Добавить в `backend/tests/test_scoring_waves.py`:

```python
def test_run_waves_shrinks_pool_across_stages_and_writes_wave_history():
    """100 -> 40 после whois (RKN не проверяем — часть отвалится раньше) -> итог: waterfall
    в job_run.message показывает уменьшение пула по волнам."""
    from app.services import jobs

    ids = [_mk_domain(domain=f"pool{i}.ru", referring_domains=5) for i in range(10)]
    states = [scoring.FunnelState(domain_id=did, domain=f"pool{i}.ru", lane=None,
                                  referring_domains=5, acquire_deadline=None, feed_flags=None)
             for i, did in enumerate(ids)]
    # чётные домены "слишком молоды" на whois — отвалятся на первой сетевой волне
    young = datetime.now(timezone.utc) - timedelta(days=100)
    old = datetime.now(timezone.utc) - timedelta(days=365 * 10)

    class _Ap:
        def __init__(self):
            self.n = 0
        def whois_probe(self, d):
            self.n += 1
            age = young if self.n % 2 == 0 else old
            return {"available": True, "created": age}
        def safebrowsing_check(self, d): return False
        def ahrefs_probe(self, d): return {"dr": 1.0, "backlinks": 0, "referring_domains": None}
    clients = {"aparser": _Ap(), "tci": type("T", (), {"handles": lambda self, d: False})(),
              "rkn": type("R", (), {"is_listed": lambda self, d: False})(),
              "blacklist": type("B", (), {"is_blacklisted": lambda self, d: False})(),
              "searxng": type("S", (), {"indexed_echo": lambda self, d: True})(),
              "wayback": _FakeWayback(dirty=False, age_years=9.0),
              "_whois_lock": threading.Lock(), "_safebrowsing_lock": threading.Lock()}
    st = {"min_age_years": 3.0, "approve_at": 0.7, "manual_review_at": 0.4,
         "min_referring_domains": 1}

    with jobs.track("score", stages=[dict(x) for x in scoring.FUNNEL_STAGES]) as run:
        out = scoring._run_waves(states, clients, st, whois_budget=None,
                                 ahrefs_budget=None, run=run)
    assert len(out) == 10
    survived = [s for s in states if s.alive]
    assert 0 < len(survived) < 10          # реально сжалось, не всё выжило и не всё умерло
    last = jobs.last("score")
    assert "whois" in last["message"] and "->" in last["message"] or "→" in last["message"]


def test_run_waves_cancellation_between_waves_preserves_partial_progress():
    """НЕ ловим jobs.Cancelled сами вокруг вызова: jobs.track() ловит его ВНУТРИ своего
    generator'а (except Cancelled -> _close(..., "cancelled"), БЕЗ re-raise) — поймай
    исключение раньше, до границы `with`, и track() увидит нормальный выход из `with`,
    закрыв прогон как "done", а не "cancelled" (найдено ревью Task 1, 2026-07-21, тот же
    паттерн уже сломал сходный тест в test_scoring_waves.py при первом написании)."""
    from app.services import jobs

    ids = [_mk_domain(domain=f"cancel{i}.ru", referring_domains=5) for i in range(5)]
    states = [scoring.FunnelState(domain_id=did, domain=f"cancel{i}.ru", lane=None,
                                  referring_domains=5, acquire_deadline=None, feed_flags=None)
             for i, did in enumerate(ids)]
    clients = {"aparser": type("Ap", (), {
                  "whois_probe": lambda self, d: {"available": True, "created": datetime.now(timezone.utc) - timedelta(days=3650)}})(),
              "tci": type("T", (), {"handles": lambda self, d: False})(),
              "_whois_lock": threading.Lock()}
    st = {"min_age_years": 3.0, "approve_at": 0.7, "manual_review_at": 0.4,
         "min_referring_domains": 1}

    with jobs.track("score", stages=[dict(x) for x in scoring.FUNNEL_STAGES]) as run:
        jobs.request_cancel("score")
        scoring._run_waves(states, clients, st, whois_budget=None,
                           ahrefs_budget=None, run=run)
    last = jobs.last("score")
    assert last["status"] == "cancelled"
```

- [ ] **Step 2: Запустить, убедиться что падает**

Run: `docker compose run --rm backend pytest backend/tests/test_scoring_waves.py -v -k run_waves`
Expected: FAIL — `AttributeError: ... has no attribute '_run_waves'`

- [ ] **Step 3: Реализовать `_run_waves`**

Добавить в `backend/app/services/scoring.py`, после `_commit_result`:

```python
def _wave_survivor_counts(states: list, stage_labels: list) -> str:
    """Собрать waterfall-строку 'RD: 10 → 6 · whois: 6 → 4 · ...' по факту, кто ещё alive
    ПОСЛЕ каждой волны — вызывается из _run_waves сразу после _checkpoint каждой стадии."""
    return " · ".join(stage_labels)


def _checkpoint(states: list, run, st: dict) -> list:
    """Финализировать в БД тех, кто вышел из конвейера НА ЭТОЙ волне (reject_reason ИЛИ
    unresolved_why выставлены), вернуть список результатов _commit_result. Выжившие
    (state.alive) остаются в states — вызывающий сам передаёт их в следующую волну."""
    out = []
    for s in states:
        if not s.alive:
            out.append(_commit_result(s, run, st))
    return out


def _run_waves(states: list, clients: dict, st: dict, whois_budget, ahrefs_budget,
               run) -> list:
    """Оркестратор: волны по порядку дёшево->дорого, между каждой — checkpoint (коммит
    вышедших, отчёт волновой истории), отмена проверяется между волнами (внутри волны —
    в _run_concurrent). Выжившие после ПОСЛЕДНЕЙ волны финализируются как решённые
    (score/approved/scored) — см. _commit_result. Возвращает результаты в порядке
    завершения (порядок не важен вызывающим — score_pending считает только длину,
    score_domain — единственный элемент списка)."""
    from app.services import jobs

    whois_b = whois_budget if whois_budget is None or hasattr(whois_budget, "take") \
        else _ListBudget(whois_budget)
    ahrefs_b = ahrefs_budget if ahrefs_budget is None or hasattr(ahrefs_budget, "take") \
        else _ListBudget(ahrefs_budget)

    results = []
    waterfall = []
    total0 = len(states)

    _wave_t0(states, st)
    if jobs.cancelled(run):
        raise jobs.Cancelled()
    results += _checkpoint(states, run, st)
    alive = [s for s in states if s.alive]
    waterfall.append(f"RD: {total0} → {len(alive)}")
    jobs.report(run, message=" · ".join(waterfall))

    _wave_whois(alive, clients, whois_b, st, run)
    if jobs.cancelled(run):
        raise jobs.Cancelled()
    results += _checkpoint(alive, run, st)
    before = len(alive); alive = [s for s in alive if s.alive]
    waterfall.append(f"whois: {before} → {len(alive)}")
    jobs.report(run, message=" · ".join(waterfall))

    _wave_risk(alive, clients, run)
    if jobs.cancelled(run):
        raise jobs.Cancelled()
    results += _checkpoint(alive, run, st)
    before = len(alive); alive = [s for s in alive if s.alive]
    waterfall.append(f"risk: {before} → {len(alive)}")
    jobs.report(run, message=" · ".join(waterfall))

    _wave_history(alive, clients, st, run)
    if jobs.cancelled(run):
        raise jobs.Cancelled()
    results += _checkpoint(alive, run, st)
    before = len(alive); alive = [s for s in alive if s.alive]
    waterfall.append(f"history: {before} → {len(alive)}")
    jobs.report(run, message=" · ".join(waterfall))

    _wave_ahrefs(alive, clients, ahrefs_b, run)
    if jobs.cancelled(run):
        raise jobs.Cancelled()
    # выжившие после ПОСЛЕДНЕЙ волны — все ещё alive (Ahrefs никогда не отбраковывает),
    # финализируем как решённых (compute_score внутри _commit_result)
    for s in alive:
        results.append(_commit_result(s, run, st))
    waterfall.append(f"ahrefs: {len(alive)} решено")
    jobs.report(run, message=" · ".join(waterfall))

    return results
```

- [ ] **Step 4: Запустить тесты**

Run: `docker compose run --rm backend pytest backend/tests/test_scoring_waves.py -v -k run_waves`
Expected: `2 passed`

- [ ] **Step 5: Полный оффлайн-сьют + pyflakes**

Run: `docker compose run --rm backend pytest backend/tests/ -q`
Run: `.venv/bin/python -m pyflakes backend/app backend/tests`
Expected: всё зелёное (ещё не рероутили `score_domain`/`score_pending` — они пока
пользуются старым `_funnel`, эта задача только собрала волновой конвейер рядом).

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/scoring.py backend/tests/test_scoring_waves.py
git commit -m "$(cat <<'EOF'
feat(scoring): _run_waves — оркестратор волн + волновая waterfall-история

Собирает волны T0-T3b в конвейер: между каждой — checkpoint (коммит
вышедших доменов, отчёт волновой истории в job_run.message), отмена
проверяется между волнами. Пока не подключён к score_domain/
score_pending — следующие две задачи их рероутят.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 8: Рероутинг `score_domain()` — контракт не меняется

Переключить `score_domain()` на волновой конвейер (батч из одного `FunnelState`), удалить
`_funnel()`. `test_funnel.py` — единственный судья: ни строчки изменений в нём, весь файл
обязан пройти.

**Files:**
- Modify: `backend/app/services/scoring.py:462-811` (удалить `_funnel`, переписать
  `score_domain`)
- Test: `backend/tests/test_funnel.py` (НЕ модифицировать — регрессия)

**Interfaces:**
- Consumes: `_run_waves` (Task 7).
- Produces: `score_domain(domain_id: int, clients: dict | None = None, whois_budget=None,
  ahrefs_budget=None, run: int | None = None) -> dict` — сигнатура и форма ответа БЕЗ
  ИЗМЕНЕНИЙ.

- [ ] **Step 1: Убедиться, что регрессионный тест уже существует и сейчас проходит на старом коде**

Run: `docker compose run --rm backend pytest backend/tests/test_funnel.py -v`
Expected: все тесты PASS (это baseline ДО рефакторинга — фиксируем текущее поведение).

- [ ] **Step 2: Удалить `_funnel()`, переписать `score_domain()`**

Удалить функцию `_funnel` целиком (сегодня строки 462-662 в `scoring.py`, включая
константу `_APARSER_SAFEBROWSING_LIMIT` на строке 459 — ПЕРЕНЕСТИ константу выше, она
нужна `_wave_risk` из Task 3, если ещё не перенесена туда буквально; проверить, что она
уже определена рядом с `_wave_risk`, а не только в удаляемом `_funnel`).

Заменить функцию `score_domain` (сегодня строки 665-811) на:

```python
def score_domain(domain_id: int, clients: dict | None = None, whois_budget=None,
                 ahrefs_budget=None, run: int | None = None) -> dict:
    """Полная воронка для ОДНОГО домена — внешний контракт идентичен дореформенному:
    та же сигнатура, та же форма ответа. Внутри строит батч из ОДНОГО FunnelState и
    прогоняет его через тот же волновой конвейер, что и score_pending (Task 9) —
    волны на батче размера 1 линеаризуются в тот же порядок стадий, что был у _funnel."""
    from app.db import SessionLocal
    from app.models.domain import Domain
    from app.services.settings import get_settings

    st = get_settings()
    with SessionLocal() as db:
        d = db.get(Domain, domain_id)
        if d is None:
            raise ValueError(f"domain {domain_id} not found")
        if d.status not in ("discovered", "scored", "rejected"):
            return {"domain": d.domain, "status": d.status, "skipped": "status"}
        state = FunnelState(domain_id=d.id, domain=d.domain, lane=d.lane,
                            referring_domains=d.referring_domains,
                            acquire_deadline=d.acquire_deadline, feed_flags=d.feed_flags)

    c = clients or _make_clients()
    results = _run_waves([state], c, st, whois_budget, ahrefs_budget, run)
    return results[0]
```

- [ ] **Step 3: Запустить `test_funnel.py` целиком**

Run: `docker compose run --rm backend pytest backend/tests/test_funnel.py -v`
Expected: `31 passed` (столько же, сколько в Step 1 — НИ ОДНОГО изменения в самом файле
теста).

Если что-то упало: НЕ трогать `test_funnel.py`, чинить расхождение поведения в
`score_domain`/волнах, пока весь файл не пройдёт как на Step 1.

- [ ] **Step 4: Полный сьют + pyflakes**

Run: `docker compose run --rm backend pytest backend/tests/ -q`
Run: `.venv/bin/python -m pyflakes backend/app backend/tests`
Expected: всё зелёное (кроме `test_sources.py`/других файлов, если они напрямую бьют по
`_funnel` — при находке такого случая точечно поправить ИХ вызов на `score_domain`, это
единственное допустимое исключение из "test_funnel.py не трогать").

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/scoring.py
git commit -m "$(cat <<'EOF'
refactor(scoring): score_domain() на волновой конвейер, _funnel() удалён

score_domain строит батч из ОДНОГО FunnelState и прогоняет через
_run_waves — внешний контракт (сигнатура, форма ответа) не изменился,
test_funnel.py (31 тест) проходит без единой правки в самом файле —
это и было условием безопасности рефакторинга.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 9: Рероутинг `score_pending()` — настоящие волны на весь пакет

Заменить сегодняшний последовательный цикл `for i, (did, name) in enumerate(rows, 1):
score_domain(...)` на ОДИН вызов `_run_waves()` на весь батч сразу — это и есть цель
всего плана: волны на пакет, не на домен.

**Files:**
- Modify: `backend/app/services/scoring.py` (функция `score_pending`, сегодня строки
  814-901)
- Test: `backend/tests/test_funnel.py` (регрессия, `test_score_pending...` тесты, строка
  382+), `backend/tests/test_scoring_waves.py` (новый тест на батч)

**Interfaces:**
- Consumes: `_run_waves` (Task 7), `FunnelState` (Task 1).
- Produces: `score_pending(limit: int = 100) -> int` — сигнатура и возврат (сколько РЕАЛЬНО
  прошло воронку) без изменений.

- [ ] **Step 1: Написать падающий тест на батчевую сборку**

Добавить в `backend/tests/test_scoring_waves.py`:

```python
def test_score_pending_builds_states_with_lane_and_rd_from_one_query(monkeypatch):
    """score_pending больше не должен грузить lane/referring_domains/acquire_deadline
    доменом по домену внутри волны — они обязаны прийти из ИСХОДНОГО SELECT (см. Task 9),
    иначе каждая волна платила бы отдельным SELECT на КАЖДЫЙ домен пачки."""
    from app.services import settings as st_mod
    did = _mk_domain(domain="batch1.ru", referring_domains=5, lane="bid")

    captured = {}
    real_run_waves = scoring._run_waves

    def _spy(states, *a, **kw):
        captured["states"] = list(states)
        return real_run_waves(states, *a, **kw)
    monkeypatch.setattr(scoring, "_run_waves", _spy)

    class _Ap:
        def whois_probe(self, d):
            return {"available": True, "created": datetime.now(timezone.utc) - timedelta(days=3650)}
        def safebrowsing_check(self, d): return False
    monkeypatch.setattr(scoring, "_make_clients", lambda: {
        "aparser": _Ap(), "tci": type("T", (), {"handles": lambda self, d: False})(),
        "rkn": type("R", (), {"is_listed": lambda self, d: False})(),
        "blacklist": type("B", (), {"is_blacklisted": lambda self, d: False})(),
        "searxng": type("S", (), {"indexed_echo": lambda self, d: True})(),
        "wayback": _FakeWayback(), "_whois_lock": threading.Lock(),
        "_safebrowsing_lock": threading.Lock()})

    scoring.score_pending(limit=10)
    assert len(captured["states"]) == 1
    assert captured["states"][0].lane == "bid"
    assert captured["states"][0].referring_domains == 5
```

- [ ] **Step 2: Запустить, убедиться что падает или уже проходит частично**

Run: `docker compose run --rm backend pytest backend/tests/test_scoring_waves.py -v -k batch_query`
Expected: FAIL, если `score_pending` пока строит `FunnelState` не из батч-запроса (до
этой задачи `score_pending` вообще не строит `FunnelState`, а зовёт `score_domain` в
цикле — тест провалится на `assert len(captured["states"]) == 1`, т.к. `_run_waves`
вызовется 1 раз НА ДОМЕН, а не 1 раз НА ВЕСЬ ПАКЕТ, что для `limit=10` с одним
подходящим доменом даст тот же результат случайно совпав — если тест зелёный уже здесь,
это ложный зелёный: убедиться подсчётом `_run_waves` вызовов, а не только states).

Уточнение шага (важно для корректности проверки): добавить в тот же тест счётчик вызовов
`_spy`:

```python
    calls = {"n": 0}
    def _spy(states, *a, **kw):
        calls["n"] += 1
        captured["states"] = list(states)
        return real_run_waves(states, *a, **kw)
    ...
    scoring.score_pending(limit=10)
    assert calls["n"] == 1              # ОДИН вызов на весь батч, не по домену
```

- [ ] **Step 3: Переписать `score_pending()`**

В `backend/app/services/scoring.py`, в функции `score_pending` заменить SELECT (сегодня
строки 844-850) и цикл (строки 879-901):

Заменить:
```python
        rows = db.execute(
            select(Domain.id, Domain.domain).where(Domain.status == "discovered", scorable(now))
            .order_by(tier,
                      Domain.acquire_deadline.asc(),
                      Domain.referring_domains.desc().nulls_last())
            .limit(limit)
        ).all()
```
на:
```python
        rows = db.execute(
            select(Domain.id, Domain.domain, Domain.lane, Domain.referring_domains,
                  Domain.acquire_deadline, Domain.feed_flags)
            .where(Domain.status == "discovered", scorable(now))
            .order_by(tier,
                      Domain.acquire_deadline.asc(),
                      Domain.referring_domains.desc().nulls_last())
            .limit(limit)
        ).all()
```

Заменить (после блока `idle_msg`, сегодня строки 879-901):
```python
    stages = [dict(s) for s in FUNNEL_STAGES]
    if int(st["max_ahrefs_per_run"]) == 0:
        stages[-1]["state"] = "skip"
    clients = _make_clients()
    whois_budget = [int(st["max_whois_per_run"])]
    ahrefs_budget = [int(st["max_ahrefs_per_run"])]
    total, done = len(rows), 0
    with jobs.track("score", stages=stages) as run:
        for i, (did, name) in enumerate(rows, 1):
            jobs.report(run, done=i - 1, total=total, current=name)
            if jobs.cancelled(run):
                raise jobs.Cancelled()
            try:
                score_domain(did, clients, whois_budget, ahrefs_budget, run=run)
            except Exception:  # noqa: BLE001 — падение одного домена не топит батч
                logging.getLogger(__name__).exception("score_domain %s упал", name)
            done = i
        jobs.report(run, done=total, total=total, current="",
                    message=idle_msg or f"прогнано {total} доменов через воронку")
    return done
```
на:
```python
    stages = [dict(s) for s in FUNNEL_STAGES]
    if int(st["max_ahrefs_per_run"]) == 0:
        stages[-1]["state"] = "skip"
    clients = _make_clients()
    whois_budget = [int(st["max_whois_per_run"])]
    ahrefs_budget = [int(st["max_ahrefs_per_run"])]
    total = len(rows)
    states = [FunnelState(domain_id=did, domain=name, lane=lane,
                          referring_domains=rd, acquire_deadline=deadline,
                          feed_flags=flags)
             for (did, name, lane, rd, deadline, flags) in rows]
    done = 0
    with jobs.track("score", stages=stages) as run:
        if not states:
            jobs.report(run, done=0, total=0, current="", message=idle_msg or "")
        else:
            results = _run_waves(states, clients, st, whois_budget, ahrefs_budget, run=run)
            done = len(results)
            jobs.report(run, done=total, total=total, current="",
                        message=idle_msg or f"прогнано {total} доменов через воронку")
    return done
```

- [ ] **Step 4: Запустить тесты**

Run: `docker compose run --rm backend pytest backend/tests/test_scoring_waves.py -v -k batch_query`
Expected: PASS (`calls["n"] == 1`).

Run: `docker compose run --rm backend pytest backend/tests/test_funnel.py -v -k score_pending`
Expected: существующие `score_pending`-тесты (строка 382+) проходят без изменений —
`score_pending` по-прежнему возвращает число реально прогнанных доменов, уважает
`whois_budget`/`ahrefs_budget` из `/settings`, репортит `idle_msg` на пустом пуле.

- [ ] **Step 5: Полный сьют + pyflakes**

Run: `docker compose run --rm backend pytest backend/tests/ -q`
Run: `.venv/bin/python -m pyflakes backend/app backend/tests`
Expected: всё зелёное. Если `test_jobs_api.py`/`test_inbox.py` (тесты, зависящие от формы
`job_run.stage`/`stages`/`message` во время работы) начинают падать — см. Task 10, там
явно обновляется ожидаемая форма прогресса; на этом шаге падение таких тестов означает,
что поведение прогресс-репортинга разошлось НЕЗАПЛАНИРОВАННО — расследовать, не просто
подгонять ожидания.

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/scoring.py backend/tests/test_scoring_waves.py
git commit -m "$(cat <<'EOF'
feat(scoring): score_pending() — настоящие волны на весь пакет разом

Цель всего плана: вместо цикла score_domain() по одному домену —
ОДИН вызов _run_waves() на весь батч. SELECT расширен (lane/RD/
deadline/feed_flags вместе с id/domain — без этого каждая волна
платила бы отдельным SELECT на домен). score_pending() контракт
(сигнатура, возврат) не изменился.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 10: UI — чипы волн, увеличенный блок прогресса, волновая waterfall-строка

Пользователь подтвердил направление и явно указал: **блок прогрессбара можно увеличить**.
`FUNNEL_STAGES` теряет отдельный чип "эхо" (слился в `risk`, Task 3); `_advance()`
(`jobs.py:282-298`) становится монотонным (волна реально проходит для всего батча один
раз, не "по домену заново"); `jobCard()` показывает волновую waterfall-строку из
`job_run.message` (сегодня это поле не рендерится для ИДУЩЕЙ задачи вообще, только для
завершённой — см. `dashboard.html:38-39`/`domains.html:60-61`).

**Files:**
- Modify: `backend/app/services/scoring.py` (`FUNNEL_STAGES`, строки 64-71)
- Modify: `backend/app/templates/base.html` (CSS `.job` блок ~строки 327-351; JS
  `jobCard()` ~строки 464-539)
- Test: `backend/tests/test_jobs_api.py`, `backend/tests/test_inbox.py` (обновить, если
  падают после Task 9 — форма `stages`/`message` изменилась)

**Interfaces:**
- Consumes: `job_run.message` (уже существующее поле, `String(400)`, `jobs.py:35`),
  теперь наполняемое волновой waterfall-строкой из `_run_waves` (Task 7) вместо только
  финальной сводки.

- [ ] **Step 1: Обновить `FUNNEL_STAGES` — убрать чип "эхо"**

В `backend/app/services/scoring.py`, заменить (строки 64-71):
```python
FUNNEL_STAGES = [
    {"key": "rd", "label": "RD из фида"},
    {"key": "whois", "label": "whois-возраст"},
    {"key": "risk", "label": "РКН/блэклист"},
    {"key": "echo", "label": "эхо в индексе"},
    {"key": "history", "label": "Wayback-история"},
    {"key": "ahrefs", "label": "Ahrefs (платно)"},
]
```
на:
```python
# Чипы волн в панели: ключ -> подпись. Порядок = порядок волн в _run_waves. "эхо" сюда
# больше не входит отдельным чипом (2026-07-20, волновая архитектура): indexed_echo —
# та же сетевая волна, что РКН/блэклист/SafeBrowsing (см. _wave_risk), отдельного прохода
# у него никогда не было даже в старом _funnel — просто раньше это не было видно оператору.
FUNNEL_STAGES = [
    {"key": "rd", "label": "RD из фида"},
    {"key": "whois", "label": "whois-возраст"},
    {"key": "risk", "label": "РКН/блэклист/эхо"},
    {"key": "history", "label": "Wayback-история"},
    {"key": "ahrefs", "label": "Ahrefs (платно)"},
]
```

- [ ] **Step 2: Запустить существующие тесты на прогресс/чипы, чтобы увидеть, что ломается**

Run: `docker compose run --rm backend pytest backend/tests/test_jobs_api.py backend/tests/test_inbox.py -v`
Expected: скорее всего PASS (эти тесты строят свои `stages` фикстуры вручную через
`jobs.track(..., stages=[{"key": "rd", ...}])`, не читают `scoring.FUNNEL_STAGES`
напрямую) — если что-то упадёт, ошибка укажет конкретную строку с ожиданием "эхо"/6
стадий, поправить точечно под 5 стадий.

- [ ] **Step 3: Увеличить блок `.job` в CSS**

В `backend/app/templates/base.html`, заменить (строки 328-329):
```css
  .job { background:var(--panel); border:1px solid var(--line); border-left:3px solid var(--acc);
         border-radius:var(--r); padding:14px 16px; margin-bottom:10px; }
```
на:
```css
  .job { background:var(--panel); border:1px solid var(--line); border-left:3px solid var(--acc);
         border-radius:var(--r); padding:18px 20px; margin-bottom:14px; }
```

Добавить после существующего правила `.job-tally` (строка 347):
```css
  .job-tally { margin-bottom:10px; }
  .job-waterfall { margin-bottom:12px; font-size:13px; color:var(--ink);
                   font-family:var(--mono); line-height:1.6; }
```

- [ ] **Step 4: Показать волновую waterfall-строку в `jobCard()`**

В `backend/app/templates/base.html`, в функции `jobCard()`, заменить блок tally (строки
506-518):
```javascript
  if (j.status === 'running' && !j.stale && j.tally){
    // Чипы выше — путь ТЕКУЩЕГО домена (каждый новый снова начинает с RD, см. jobs._advance).
    // Это годится показать, что происходит СЕЙЧАС, но не отвечает на «а дешёвые стадии вообще
    // отсеивают быстро?» — для этого нужна раскладка по ВСЕМ обработанным доменам прогона.
    const t = j.tally;
    const info = document.createElement('div'); info.className = 'job-tally hint';
    info.textContent = t.total + ' обработано: ' + t.before_wayback + ' до Wayback (дёшево), '
                      + t.reached_wayback + ' дошли до Wayback';
    info.title = 'Живая раскладка исхода ЭТОГО прогона: сколько доменов решились на дешёвых ' +
                 'стадиях (RD из фида, whois-возраст, РКН/блэклист, SafeBrowsing) ДО дорогой ' +
                 'Wayback-истории, и сколько реально до неё дошло.';
    el.appendChild(info);
  }
```
на:
```javascript
  if (j.status === 'running' && !j.stale && j.name === 'score' && j.message){
    // Волновая waterfall: "RD: 5510 -> 4310 · whois: 4310 -> 3800 · risk: идёт, 1200/3800" —
    // сколько домена ВЫЖИЛО после каждой волны (не путь одного домена — чипы выше теперь
    // и так монотонны для всего батча, см. jobs._advance). Источник — job_run.message,
    // который _run_waves перезаписывает после каждой волны (services/scoring.py).
    const wf = document.createElement('div'); wf.className = 'job-waterfall';
    wf.textContent = j.message;
    wf.title = 'Сколько доменов пережило каждую волну целиком (не путь одного домена): ' +
               'воронка идёт дёшево->дорого волнами на весь пакет сразу, не по одному домену.';
    el.appendChild(wf);
  }
  if (j.status === 'running' && !j.stale && j.tally){
    const t = j.tally;
    const info = document.createElement('div'); info.className = 'job-tally hint';
    info.textContent = t.total + ' обработано: ' + t.before_wayback + ' до Wayback (дёшево), '
                      + t.reached_wayback + ' дошли до Wayback';
    info.title = 'Живая раскладка исхода ЭТОГО прогона по ПРИЧИНЕ отказа (дополняет строку ' +
                 'волн выше, которая считает по СТАДИИ).';
    el.appendChild(info);
  }
```

- [ ] **Step 5: Живой визуальный прогон (Playwright/chrome-devtools)**

Поднять панель на throwaway SQLite-харнессе (тот же приём, что и в предыдущей сессии —
`uvicorn.run` на in-memory SQLite, `PANEL_USER`/`PANEL_PASS` очищены), засеять `job_run`
строку с `name="score"`, `status="running"`, `message="RD: 100 → 60 · whois: 60 → 40 ·
risk: идёт, 15/40"`, стадии `FUNNEL_STAGES` (5 штук), открыть `/` через
`mcp__plugin_chrome-devtools-mcp_chrome-devtools__navigate_page` +
`take_screenshot` — визуально проверить, что: (а) блок `.job` заметно крупнее прежнего,
(б) waterfall-строка читается, (в) чипов волн 5 (не 6), (г) ничего не съехало по вёрстке
на узком экране (эмулировать 1280px и 1024px). Задокументировать результат в отчёте
задачи (без сохранения самого скриншота в репозиторий).

- [ ] **Step 6: Полный сьют + pyflakes**

Run: `docker compose run --rm backend pytest backend/tests/ -q`
Run: `.venv/bin/python -m pyflakes backend/app backend/tests`

- [ ] **Step 7: Commit**

```bash
git add backend/app/services/scoring.py backend/app/templates/base.html
git commit -m "$(cat <<'EOF'
feat(panel): волновая waterfall-строка + укрупнённый блок прогресса

FUNNEL_STAGES — 5 чипов волн (эхо слилось в risk, отдельного прохода
у него не было и в _funnel). .job крупнее (решение пользователя —
"блок прогрессбара можно увеличить"). jobCard() показывает волновую
waterfall (job_run.message от _run_waves) — сколько доменов выжило
после КАЖДОЙ волны, не путь одного текущего домена.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 11: Финальная регрессия, стресс на конкурентность, живой прогон — заметка

Последняя задача: полный прогон всего сьюта, точечный стресс-тест на реальную
конкурентность (не только логику, но и что параллельные потоки не роняют друг друга под
настоящим `ThreadPoolExecutor` с не-фейковой задержкой), и фиксация в `CLAUDE.md`
обязательного следующего шага — живой прогон на боксе.

**Files:**
- Test: `backend/tests/test_scoring_waves.py` (стресс-тест)
- Modify: `CLAUDE.md` (секция «Текущее состояние» — короткая запись)

**Interfaces:**
- Consumes: весь код Task 1-10. Ничего нового не производит.

- [ ] **Step 1: Стресс-тест на реальную конкурентность (не фейковую)**

Добавить в `backend/tests/test_scoring_waves.py`:

```python
import time


def test_wave_whois_actually_runs_concurrently_not_serially():
    """Сеть в тесте эмулирована time.sleep(0.05) на 24 домена. Последовательно это было бы
    >=1.2с; при конкурентности 12 — не больше ~0.15с (2 партии по 12). Пороговое значение
    щедрое (0.5с), чтобы не флапать на медленном CI, но 10x-разница гарантирует, что пул
    реально работает, а не притворяется."""
    st = {"min_age_years": 3.0}
    states = [scoring.FunnelState(domain_id=i, domain=f"slow{i}.ru", lane=None,
                                  referring_domains=5, acquire_deadline=None,
                                  feed_flags=None) for i in range(24)]

    class _SlowAparser:
        def whois_probe(self, d):
            time.sleep(0.05)
            return {"available": True, "created": datetime.now(timezone.utc) - timedelta(days=3650)}

    clients = {"aparser": _SlowAparser(),
              "tci": type("T", (), {"handles": lambda self, d: False})(),
              "_whois_lock": threading.Lock()}
    start = time.monotonic()
    scoring._wave_whois(states, clients, budget=None, st=st, run=None)
    elapsed = time.monotonic() - start
    assert elapsed < 0.5, f"волна заняла {elapsed:.2f}с — похоже на последовательный обход"
    assert all(s.alive for s in states)
```

- [ ] **Step 2: Запустить, убедиться что проходит**

Run: `docker compose run --rm backend pytest backend/tests/test_scoring_waves.py -v -k actually_runs_concurrently`
Expected: `1 passed` (elapsed заметно меньше 0.5с — реально означает конкурентность 12
живая, не фейковая последовательность).

- [ ] **Step 3: Полный оффлайн-сьют + pyflakes — финальная сверка**

Run: `docker compose run --rm backend pytest backend/tests/ -q`
Expected: все тесты проходят (ожидаемое итоговое число — на 700+, было 686+новые из этого
плана: ~4+9+3+3+4+3+2+1+1(batch)+1(concurrency) ≈ 30 новых + существующие).

Run: `.venv/bin/python -m pyflakes backend/app backend/tests`
Expected: чисто.

- [ ] **Step 4: Обновить `CLAUDE.md`**

Добавить в раздел «Что делать дальше» (после существующего пункта про первый прогон
воронки на боксе) строку:

```markdown
4. **Живая проверка волновой архитектуры скоринга** (влито 2026-07-2x, ветка на
   волновую воронку M1 — см. `docs/superpowers/specs/2026-07-20-scoring-wave-architecture-design.md`):
   офлайн-сьют зелёный, конкурентность 12/12/4/2 (whois/risk/history/ahrefs)
   проверена стресс-тестом на фейковой задержке — НЕ на реальном A-Parser/TCI/
   archive.org под нагрузкой. Прогнать `score_pending()` на боксе с реальным пулом,
   сверить: (а) реальный выигрыш по времени против дореформенного последовательного
   прогона, (б) предохранители TCI/A-Parser/SafeBrowsing не текут под конкурентностью
   (счётчики не убегают выше лимита — живой A-Parser может отвечать медленнее/иначе,
   чем фейковые клиенты тестов), (в) вежливость к archive.org (потолок 4) не создаёт
   троттлинга сильнее, чем сегодняшний последовательный обход.
```

- [ ] **Step 5: Commit**

```bash
git add backend/tests/test_scoring_waves.py CLAUDE.md
git commit -m "$(cat <<'EOF'
test(scoring): стресс на реальную конкурентность волн + заметка о живой проверке

Тест доказывает, что _wave_whois реально параллелит (10x разница
elapsed против последовательного времени), а не притворяется пулом.
CLAUDE.md: живой прогон на боксе — обязательный некодовый следующий
шаг (предохранители/вежливость под РЕАЛЬНОЙ, не фейковой, нагрузкой).

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

## Self-Review (проведён при написании плана)

1. **Покрытие спеки:** §1 (FunnelState) — Task 1; §2 (волны+конкурентность+вежливость) —
   Task 1-5; §3 (бюджеты+отмена) — Task 1 (Budget), Task 7 (`_run_waves` отмена между
   волнами, `_run_concurrent` отмена внутри волны, Task 1); §4 (финализация) — Task 6;
   §5 (score_domain контракт) — Task 8; §6 (UI) — Task 10. Всё покрыто.
2. **Плейсхолдеры:** нет TBD/TODO — каждый шаг несёт полный код.
3. **Типовая согласованность:** `FunnelState`/`Budget`/`_ListBudget` определены в Task 1,
   используются идентично во всех задачах 2-9; `_wave_X(states, ..., run)` — единая
   форма сигнатуры по всем волнам; `_commit_result`/`_run_waves` возвращаемые формы
   зафиксированы в Task 6/7 и не меняются в Task 8/9.
