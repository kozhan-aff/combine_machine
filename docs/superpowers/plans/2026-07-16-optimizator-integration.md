# Optimizator.ru — второй канал выкупа (M2) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the already-partially-wired Optimizator acquisition channel (`_PROVIDERS
= {"backorder", "optimizator"}` already exists in `acquisition.py`) actually work:
real transport, idempotent execute, stuck-claim recovery, price visibility on confirm,
and a UI path to actually choose this provider — WITHOUT ever weakening the money gate.

**Architecture:** New `OptimizatorClient` (transport-only, mirrors `BackorderClient`'s
shape) plugged into the EXISTING `execute_confirmed_order`/`confirm_order`/`poll_orders`
functions in `acquisition.py` at the branch points that already exist or are explicitly
documented as missing. No new DB columns/migrations — `AcquisitionOrder.provider`
already supports this value.

**Tech Stack:** Python 3.12, SQLAlchemy 2.x, httpx, pytest (hermetic — `_no_live_network`
autouse fixture; every test mocks `OptimizatorClient`, matching how existing tests mock
`BackorderClient` — see `test_m23_fixes.py`, `test_order_recovery.py`).

## Global Constraints

- Design source of truth: `docs/superpowers/specs/2026-07-16-optimizator-integration-design.md`
  — read it before every task, especially the "Идемпотентность денег" table and the
  three `[РЕШЕНИЕ]`-tagged spots this plan's own self-review caught and fixed
  (`check_domain` never returns a None-sentinel — it raises like every other method;
  the `tier` dict for optimizator MUST include `price_id: None, period_id: None` or
  the existing `confirm_order` code KeyErrors; the new recovery loop MUST go through
  the existing `_settle()` helper, never a bare `o.status = ...; db.commit()`).
- **Hard money gate untouched.** `execute_confirmed_order`'s `confirmed_by_human`
  check, the atomic `ordering` claim, `refuse_dirty`/`dirty_reason`, and
  `uq_open_order_per_domain` all sit ABOVE the provider branch and are not touched by
  any task in this plan — every task's diff must stay below that gate, never above it.
- No real `register()` call against the live Optimizator API in any task or test —
  balance is 0 ₽ and the NIC-D anketa is not yet transferred to Optimizator's partner
  management (both organizational blockers, see spec). Tests mock the transport;
  nothing here talks to `optimizator.ru` over the network.
- `OptimizatorClient.register()` bypasses `BaseClient`'s retry wrapper (same reason as
  `BackorderClient.order()`: 3 retries would mean 3 attempts at a money-spending call).

---

## File Structure

| File | Change |
|---|---|
| `backend/app/integrations/optimizator.py` | Full rewrite: `OptimizatorError`, `OptimizatorAmbiguous`, `ping/balance/prices/check_nicd/register/order_status/check_domain` |
| `backend/tests/test_optimizator.py` | New — unit tests for the client (mirrors `test_aparser.py`/backorder client test style) |
| `backend/app/services/acquisition.py` | `execute_confirmed_order` optimizator branch (idempotency + ambiguous handling); `confirm_order` optimizator price-freeze branch; `poll_orders` optimizator stuck-claim recovery loop |
| `backend/tests/test_m23_fixes.py` or new `test_optimizator_acquisition.py` | Tests for the execute/confirm branches (implementer picks whichever existing file's fixtures fit better — see Task 2 note) |
| `backend/tests/test_order_recovery.py` | New tests for the optimizator recovery loop, alongside the existing backorder F11/F12 tests |
| `backend/app/templates/domains.html`, `pool.html` | Provider `<select>` on the queue-add form, preselected by `Domain.lane` |
| `backend/app/templates/queue.html` | Confirm form branches by `o.provider` (backorder tariff select vs. optimizator fixed-price text) |
| `backend/app/api/panel.py` | (Task 4, optional) `/diag` gets an `OptimizatorClient().ping()` row alongside backorder's |

---

### Task 1: `OptimizatorClient` — transport

**Files:**
- Modify: `backend/app/integrations/optimizator.py` (full rewrite of the existing stub)
- Test: `backend/tests/test_optimizator.py` (new)

**Interfaces:**
- Produces: `OptimizatorError(message, error_id=None)`, `OptimizatorAmbiguous(...)`,
  and `OptimizatorClient.{ping, balance, prices, check_nicd, register, order_status,
  check_domain}` — Task 2/3 consume all of these.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_optimizator.py`:

```python
"""OptimizatorClient — транспорт (M2, второй канал выкупа). Формат ответа живьём
проверен 2026-07-16 (balance/prices/check_nicd реальным ключом); reg_domains/
check_order/check_domain/renew_domains — по документации (не тратим деньги на тест).
См. docs/superpowers/specs/2026-07-16-optimizator-integration-design.md."""
import httpx
import pytest

from app.integrations.optimizator import OptimizatorClient, OptimizatorError, OptimizatorAmbiguous


def _client(monkeypatch, response_json, status=200):
    """Мокает httpx на уровне request(): OptimizatorClient использует GET-запросы
    через BaseClient.request, кроме register() (тот идёт напрямую через httpx —
    см. Step 3). Один хелпер покрывает все методы, кроме register()."""
    class _Resp:
        def __init__(self):
            self.status_code = status
        def json(self):
            return response_json
        def raise_for_status(self):
            if status >= 400:
                raise httpx.HTTPStatusError("boom", request=None, response=self)

    def fake_request(self, method, url, **kw):
        return _Resp()

    monkeypatch.setattr("app.integrations.base.BaseClient.request", fake_request)
    return OptimizatorClient()


def test_balance_parses_real_live_format(monkeypatch):
    c = _client(monkeypatch, [{"balance": 0}])
    assert c.balance() == 0


def test_prices_parses_real_live_format(monkeypatch):
    c = _client(monkeypatch, [{"domain": "RU", "price_registration": 179, "price_renewal": 199}])
    out = c.prices("ru")
    assert out == {"domain": "RU", "price_registration": 179, "price_renewal": 199}


def test_ping_true_on_success(monkeypatch):
    c = _client(monkeypatch, [{"balance": 0}])
    assert c.ping() is True


def test_check_nicd_false_on_411(monkeypatch):
    c = _client(monkeypatch, [{"error": "Указанная анкета не находится под нашим "
                                        "управлением", "error_id": 411}])
    assert c.check_nicd() is False


def test_check_nicd_true_on_success(monkeypatch):
    c = _client(monkeypatch, [{"nicd": "11/NIC-D"}])
    assert c.check_nicd() is True


def test_check_nicd_reraises_other_error_ids(monkeypatch):
    c = _client(monkeypatch, [{"error": "неизвестный ключ API", "error_id": 401})
    with pytest.raises(OptimizatorError):
        c.check_nicd()


def test_error_shaped_response_raises_optimizator_error(monkeypatch):
    c = _client(monkeypatch, [{"error": "недостаточно средств", "error_id": 42}])
    with pytest.raises(OptimizatorError) as exc:
        c.balance()
    assert exc.value.error_id == 42


def test_order_status_unwraps_array(monkeypatch):
    c = _client(monkeypatch, [{"order_id": 7, "state": "completed"}])
    assert c.order_status(7) == {"order_id": 7, "state": "completed"}


def test_check_domain_unwraps_array(monkeypatch):
    c = _client(monkeypatch, [{"data_end": "02.12.2016", "domain": "A.RU"}])
    assert c.check_domain("a.ru") == {"data_end": "02.12.2016", "domain": "A.RU"}


def test_register_unwraps_array(monkeypatch):
    def fake_get(self, url, **kw):
        class _R:
            def raise_for_status(self): pass
            def json(self): return [{"order_id": 99}]
        return _R()
    monkeypatch.setattr("httpx.Client.get", fake_get)
    c = OptimizatorClient()
    assert c.register(["a.ru"]) == {"order_id": 99}


def test_register_raises_on_error(monkeypatch):
    def fake_get(self, url, **kw):
        class _R:
            def raise_for_status(self): pass
            def json(self): return [{"error": "нет анкеты под управлением", "error_id": 411}]
        return _R()
    monkeypatch.setattr("httpx.Client.get", fake_get)
    c = OptimizatorClient()
    with pytest.raises(OptimizatorError):
        c.register(["a.ru"])


def test_register_transport_failure_raises_ambiguous(monkeypatch):
    def fake_get(self, url, **kw):
        raise httpx.TimeoutException("timed out")
    monkeypatch.setattr("httpx.Client.get", fake_get)
    c = OptimizatorClient()
    with pytest.raises(OptimizatorAmbiguous):
        c.register(["a.ru"])
```

**Note for implementer:** the `_client()` helper above monkeypatches
`BaseClient.request` — confirm this actually matches how `BaseClient.request` is
defined (method signature, whether it takes `method, url` positionally) by reading
`backend/app/integrations/base.py` before relying on it; adjust the fake if the real
signature differs. Same caution for the raw-`httpx.Client.get` mock used for
`register()`'s tests (Step 3 explains why `register()` bypasses `BaseClient`).

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=backend .venv/bin/python -m pytest backend/tests/test_optimizator.py -v`
(or `docker compose run --rm backend pytest backend/tests/test_optimizator.py -v` if
Docker is available)
Expected: FAIL — `ImportError` (module still raises `NotImplementedError`, no
`OptimizatorError`/`OptimizatorAmbiguous` exist yet).

- [ ] **Step 3: Implement the client**

Replace `backend/app/integrations/optimizator.py` entirely with:

```python
"""optimizator.ru client (register already-free domains via RU-CENTER reseller API).
Transport only. Second M2 acquisition channel — "свободные чистые → optimizator
(гарантия)" (CLAUDE.md), complementing backorder's competitive-bid channel.

Live-verified format (2026-07-16, real key): GET/POST
http://optimizator.ru/?a=api&sa=<action>&api_key=KEY -> JSON ARRAY, even for single
values. Error shape (NOT documented anywhere in the site's text, found live via
check_nicd on an untransferred anketa): [{"error": "...", "error_id": 411}].

Documented-but-not-live-tested actions (reg_domains/renew_domains spend money;
check_order/check_domain need an existing order under this nicd, which doesn't exist
yet — balance is 0 and the anketa is not transferred, see design doc "Блокеры"):
reg_domains, check_order, check_domain, renew_domains. Same envelope shape as the
three actions that WERE live-verified (balance/prices/check_nicd) — same provider,
same wrapper, reasonable to trust the shape.

See docs/superpowers/specs/2026-07-16-optimizator-integration-design.md.
"""
import httpx

from app.config import settings
from app.integrations.base import BaseClient


class OptimizatorError(Exception):
    """Провайдер вернул {"error": ..., "error_id": ...} — ЧИСТЫЙ отказ (HTTP успешен,
    ответ разобран, провайдер explicitly сказал "нет"). Деньги НЕ ушли — безопасно
    показать человеку и безопасно позволить retry."""
    def __init__(self, message: str, error_id: int | None = None):
        super().__init__(f"{message} (error_id={error_id})" if error_id else message)
        self.error_id = error_id


class OptimizatorAmbiguous(Exception):
    """Транспорт упал (timeout/5xx/соединение) ПОСЛЕ отправки денежного запроса —
    исход НЕИЗВЕСТЕН, как AmbiguousSend у backorder. НЕ давать retry вслепую."""


def _unwrap(data) -> dict:
    """[{...}] -> {...}; поднимает OptimizatorError на форму {"error":..., "error_id":...}."""
    row = data[0] if isinstance(data, list) and data else {}
    if isinstance(row, dict) and "error" in row:
        raise OptimizatorError(row.get("error", "unknown error"), row.get("error_id"))
    return row if isinstance(row, dict) else {}


class OptimizatorClient(BaseClient):
    def __init__(self):
        super().__init__("http://optimizator.ru")
        self.api_key = settings.OPTIMIZATOR_API_KEY
        self.nicd = settings.OPTIMIZATOR_NICD

    def _get(self, action: str, **params) -> dict:
        p = {"a": "api", "sa": action, "api_key": self.api_key, **params}
        r = self.request("GET", self.base_url + "/", params=p)
        return _unwrap(r.json())

    def ping(self) -> bool:
        """Живость + auth — balance ничего не стоит (read-only)."""
        self._get("balance")
        return True

    def balance(self) -> float:
        return float(self._get("balance").get("balance") or 0)

    def prices(self, zone: str = "ru") -> dict:
        return self._get("prices", domain=zone)

    def check_nicd(self) -> bool:
        """True — анкета под управлением Optimizator. False — конкретно error_id=411
        (анкета не передана, живьём подтверждённый случай). Любая ДРУГАЯ ошибка —
        не гадаем её смысл, пробрасываем как есть (см. design doc)."""
        try:
            self._get("check_nicd", nicd=self.nicd)
            return True
        except OptimizatorError as e:
            if e.error_id == 411:
                return False
            raise

    def order_status(self, order_id: int) -> dict:
        return self._get("check_order", order_id=order_id)

    def check_domain(self, domain: str) -> dict:
        """Успех = домен под управлением нашей анкеты (может быть продлён). Как и все
        методы — бросает OptimizatorError/Ambiguous на отказ/сбой, нет отдельного
        None-сентинела (нет живых данных о форме ответа "домен не наш")."""
        return self._get("check_domain", domain=domain)

    def register(self, domains: list[str]) -> dict:
        """reg_domains — ДЕНЬГИ. Мимо retry BaseClient (как BackorderClient.order():
        3 ретрая = 3 попытки списания за одну команду). До 30 доменов, см. дока."""
        p = {"a": "api", "sa": "reg_domains", "api_key": self.api_key,
             "nicd": self.nicd, "domains": " ".join(domains), "enc": "utf8"}
        try:
            with httpx.Client(timeout=30.0) as client:
                r = client.get(self.base_url + "/", params=p)
                r.raise_for_status()
                return _unwrap(r.json())
        except OptimizatorError:
            raise
        except Exception as e:  # noqa: BLE001 — транспорт/JSON-сбой ПОСЛЕ отправки денежного запроса
            raise OptimizatorAmbiguous(f"{type(e).__name__}: {e}") from e
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=backend .venv/bin/python -m pytest backend/tests/test_optimizator.py -v`
Expected: PASS (14 tests). Fix the test file's mocking approach if Step 3's real
method signatures (e.g., `self.request(...)` param names) don't line up — the tests'
INTENT (what each asserts) is fixed by this brief; the exact monkeypatch mechanics
should match whatever `BaseClient.request`/`httpx.Client.get` actually look like.

- [ ] **Step 5: Full test file + pyflakes**

Run: `PYTHONPATH=backend .venv/bin/python -m pytest backend/tests/test_optimizator.py -v`
Run: `.venv/bin/python -m pyflakes backend/app/integrations/optimizator.py backend/tests/test_optimizator.py`
Expected: all green, clean.

- [ ] **Step 6: Commit**

```bash
git add backend/app/integrations/optimizator.py backend/tests/test_optimizator.py
git commit -m "feat(M2): OptimizatorClient — real transport for the second acquisition channel

balance/prices/check_nicd форматы проверены живьём реальным ключом 2026-07-16;
reg_domains/check_order/check_domain — по документации (баланс 0 ₽, анкета не
передана в управление — деньги не тратим на этом этапе). register() бросает
OptimizatorError (чистый отказ, retry безопасен) отдельно от OptimizatorAmbiguous
(транспорт упал, исход неизвестен) — тот же контракт, что AmbiguousSend у backorder."
```

---

### Task 2: Wire into `execute_confirmed_order` + `confirm_order`

**Files:**
- Modify: `backend/app/services/acquisition.py:441-443` (execute_confirmed_order optimizator branch)
- Modify: `backend/app/services/acquisition.py:273-282` (confirm_order — add optimizator price-freeze branch, parallel to the existing `if provider == "backorder":` block)
- Test: extend `backend/tests/test_m23_fixes.py` OR create `backend/tests/test_optimizator_acquisition.py` — **read `test_m23_fixes.py` and `test_order_recovery.py` first**; if their fixtures (`_add`, `_approved`, DB helpers) are easy to reuse, extend `test_m23_fixes.py` with an `_offline_optimizator` autouse-free fixture (don't make it autouse — it would monkeypatch a class the backorder tests never touch, but explicit is cleaner than sharing an autouse fixture across two unrelated providers); otherwise a new file mirroring the same helpers is fine.

**Interfaces:**
- Consumes: `OptimizatorClient`, `OptimizatorError`, `OptimizatorAmbiguous` from Task 1.
- Produces: no new public functions — modifies existing `execute_confirmed_order`/`confirm_order` bodies only.

- [ ] **Step 1: Write the failing tests**

```python
"""Optimizator branch of execute_confirmed_order/confirm_order — idempotency via
check_domain (no domain->order listing exists, unlike backorder.find_order), clean
rejection vs. ambiguous-transport distinction. See design doc "Идемпотентность денег"."""
import pytest

import app.db as db
from app.models.domain import Domain, AcquisitionOrder
from app.services import acquisition
from app.integrations.optimizator import OptimizatorError, OptimizatorAmbiguous


def _approved_optimizator(name="free-clean.ru") -> int:
    with db.SessionLocal() as s:
        d = Domain(domain=name, source="cctld", status="approved", lane="free")
        s.add(d)
        s.commit()
        s.refresh(d)
        return d.id


def test_confirm_freezes_price_for_optimizator(monkeypatch):
    monkeypatch.setattr(
        "app.integrations.optimizator.OptimizatorClient.prices",
        lambda self, zone="ru": {"domain": "RU", "price_registration": 179, "price_renewal": 199})
    did = _approved_optimizator()
    oid = acquisition.create_order(did, provider="optimizator")
    result = acquisition.confirm_order(oid)          # bid_rub НЕ передаём — optimizator его не требует
    assert result["confirmed_by_human"] is True
    with db.SessionLocal() as s:
        o = s.get(AcquisitionOrder, oid)
        assert o.cost == 179


def test_execute_registers_when_not_already_ours(monkeypatch):
    monkeypatch.setattr("app.integrations.optimizator.OptimizatorClient.prices",
                        lambda self, zone="ru": {"price_registration": 179})
    monkeypatch.setattr("app.integrations.optimizator.OptimizatorClient.check_domain",
                        lambda self, domain: (_ for _ in ()).throw(OptimizatorError("not found", 404)))
    monkeypatch.setattr("app.integrations.optimizator.OptimizatorClient.register",
                        lambda self, domains: {"order_id": 555})
    did = _approved_optimizator()
    oid = acquisition.create_order(did, provider="optimizator")
    acquisition.confirm_order(oid)
    result = acquisition.execute_confirmed_order(oid)
    assert result["status"] == "ordered"
    assert result["result"]["order_id"] == 555


def test_execute_skips_register_when_already_ours(monkeypatch):
    monkeypatch.setattr("app.integrations.optimizator.OptimizatorClient.prices",
                        lambda self, zone="ru": {"price_registration": 179})
    monkeypatch.setattr("app.integrations.optimizator.OptimizatorClient.check_domain",
                        lambda self, domain: {"data_end": "02.12.2027", "domain": domain.upper()})
    called = {"register": 0}
    def _register(self, domains):
        called["register"] += 1
        return {"order_id": 1}
    monkeypatch.setattr("app.integrations.optimizator.OptimizatorClient.register", _register)
    did = _approved_optimizator()
    oid = acquisition.create_order(did, provider="optimizator")
    acquisition.confirm_order(oid)
    result = acquisition.execute_confirmed_order(oid)
    assert result["status"] == "ordered"
    assert called["register"] == 0                   # НЕ шлём второй reg_domains


def test_execute_clean_rejection_leaves_retry_open(monkeypatch):
    monkeypatch.setattr("app.integrations.optimizator.OptimizatorClient.prices",
                        lambda self, zone="ru": {"price_registration": 179})
    monkeypatch.setattr("app.integrations.optimizator.OptimizatorClient.check_domain",
                        lambda self, domain: (_ for _ in ()).throw(OptimizatorError("not found", 404)))
    monkeypatch.setattr(
        "app.integrations.optimizator.OptimizatorClient.register",
        lambda self, domains: (_ for _ in ()).throw(OptimizatorError("недостаточно средств", 42)))
    did = _approved_optimizator()
    oid = acquisition.create_order(did, provider="optimizator")
    acquisition.confirm_order(oid)
    result = acquisition.execute_confirmed_order(oid)
    assert result["status"] == "failed"
    assert result["result"].get("maybe_sent") is not True     # чистый отказ — не ambiguous


def test_execute_ambiguous_send_sets_maybe_sent(monkeypatch):
    monkeypatch.setattr("app.integrations.optimizator.OptimizatorClient.prices",
                        lambda self, zone="ru": {"price_registration": 179})
    monkeypatch.setattr("app.integrations.optimizator.OptimizatorClient.check_domain",
                        lambda self, domain: (_ for _ in ()).throw(OptimizatorError("not found", 404)))
    monkeypatch.setattr(
        "app.integrations.optimizator.OptimizatorClient.register",
        lambda self, domains: (_ for _ in ()).throw(OptimizatorAmbiguous("timed out")))
    did = _approved_optimizator()
    oid = acquisition.create_order(did, provider="optimizator")
    acquisition.confirm_order(oid)
    result = acquisition.execute_confirmed_order(oid)
    assert result["status"] == "failed"
    assert result["result"]["maybe_sent"] is True


def test_execute_still_gates_on_confirmed_by_human():
    """ГЕЙТ НЕ ТРОНУТ — тот же тест, что уже есть для backorder, повторён для optimizator."""
    did = _approved_optimizator()
    oid = acquisition.create_order(did, provider="optimizator")
    result = acquisition.execute_confirmed_order(oid)     # НЕ подтверждён
    assert "gate" in (result.get("error") or "")
```

**Note for implementer:** confirm `Domain` actually has a `lane` column (used in
`_approved_optimizator` above) by checking `backend/app/models/domain.py` — if the
column name or default differs, adjust the fixture, not the test's intent.

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=backend .venv/bin/python -m pytest backend/tests/test_optimizator_acquisition.py -v`
(or wherever you placed them per the file-choice note above)
Expected: FAIL (`confirm_order` doesn't freeze `o.cost` for optimizator yet;
`execute_confirmed_order`'s optimizator branch still just calls bare `register()`
with no idempotency/ambiguous handling).

- [ ] **Step 3: Implement `confirm_order`'s optimizator branch**

In `backend/app/services/acquisition.py`, the current block (lines 273-282):

```python
    tier = None
    if provider == "backorder":
        # Тариф выбираем ЗДЕСЬ и замораживаем в заказе: ставка между тирами округляется вверх,
        # и человек должен увидеть фактическую сумму на своём же действии, а не получить
        # «система решила доплатить» на отправке. execute тир уже не трогает.
        # Сетевой pick_tariff — ВНЕ транзакции БД (иначе лежащий провайдер держит соединение).
        from app.integrations.backorder import BackorderClient
        if domain is None:
            raise ValueError(f"order {order_id}: домен не найден")
        tier = BackorderClient().pick_tariff(domain, float(bid_rub))
```

becomes:

```python
    tier = None
    if provider == "backorder":
        # Тариф выбираем ЗДЕСЬ и замораживаем в заказе: ставка между тирами округляется вверх,
        # и человек должен увидеть фактическую сумму на своём же действии, а не получить
        # «система решила доплатить» на отправке. execute тир уже не трогает.
        # Сетевой pick_tariff — ВНЕ транзакции БД (иначе лежащий провайдер держит соединение).
        from app.integrations.backorder import BackorderClient
        if domain is None:
            raise ValueError(f"order {order_id}: домен не найден")
        tier = BackorderClient().pick_tariff(domain, float(bid_rub))
    elif provider == "optimizator":
        # Цена ФИКСИРОВАНА (не выбор человека, в отличие от backorder-тира), но всё
        # равно показываем и фиксируем ДО отправки — денежный гейт требует видимой
        # суммы, а не "система решит на исполнении". price_id/period_id: None — это
        # backorder-специфичные поля (тир сетки тарифов), у optimizator их нет; ниже
        # существующий код обращается к tier["price_id"]/tier["period_id"]
        # БЕЗУСЛОВНО, когда tier is not None — без этих двух ключей он упал бы KeyError.
        from app.integrations.optimizator import OptimizatorClient
        if domain is None:
            raise ValueError(f"order {order_id}: домен не найден")
        zone = domain.rsplit(".", 1)[-1]
        price = OptimizatorClient().prices(zone)
        tier = {"price": price["price_registration"], "price_id": None, "period_id": None}
```

- [ ] **Step 4: Implement `execute_confirmed_order`'s optimizator branch**

The current code (around line 441-443):

```python
            else:
                from app.integrations.optimizator import OptimizatorClient
                res = OptimizatorClient().register([d.domain])   # optimizator берёт список
```

becomes:

```python
            else:
                from app.integrations.optimizator import OptimizatorClient, OptimizatorError, OptimizatorAmbiguous
                c = OptimizatorClient()
                # ИДЕМПОТЕНТНОСТЬ. У API нет «список заказов»/«заказ по домену» (в отличие
                # от backorder.find_order) — единственная замена: check_domain успешен
                # ТОЛЬКО для доменов под нашей анкетой. Успех = «уже наш», второй
                # reg_domains не шлём. check_domain, как и все методы клиента, бросает на
                # отказ/сбой — нет отдельного None-сентинела (нет живых данных о формате
                # "домен не наш"). Эта проверка денег не тратит, поэтому безопасно просто
                # продолжить к register() на любом исключении: если домен и правда уже
                # наш, ответит reg_domains (его OptimizatorError упадёт в except ниже).
                try:
                    existing = c.check_domain(d.domain)
                except (OptimizatorError, OptimizatorAmbiguous):
                    existing = None
                saved.pop("maybe_sent", None)
                if existing is not None:
                    o.status = "ordered"
                    o.result = {**saved, "note": "домен уже под нашей анкетой (check_domain) — "
                                                 "второй reg_domains не шлём",
                                "data_end": existing.get("data_end")}
                    o.ordered_at = o.ordered_at or datetime.now(timezone.utc)
                    db.commit()
                    return {"order_id": order_id, "status": o.status, "result": o.result}
                try:
                    res = c.register([d.domain])
                except OptimizatorAmbiguous as e:
                    o.status = "failed"
                    o.result = {**saved, "error": f"исход неизвестен: {e}", "maybe_sent": True}
                    db.commit()
                    return {"order_id": order_id, "status": "failed", **o.result}
                # OptimizatorError (чистый отказ) падает в общий except Exception ниже —
                # деньги не ушли, "↻ повторить" безопасен, сообщение уже читаемое
                # (OptimizatorError.__str__ несёт error_id).
```

The lines immediately after this block (currently `o.status = "ordered"; o.provider_order_id
= ...; o.result = {...}`) stay EXACTLY as they are — `res` is still the unwrapped
`{"order_id": N}` dict that code already expects.

- [ ] **Step 5: Run tests to verify they pass**

Run: `PYTHONPATH=backend .venv/bin/python -m pytest backend/tests/test_optimizator_acquisition.py backend/tests/test_m23_fixes.py backend/tests/test_bid_validation.py -v`
Expected: all PASS — new tests green, existing backorder tests in `test_m23_fixes.py` unaffected (this task doesn't touch the `if provider == "backorder":` branches).

- [ ] **Step 6: Full suite + pyflakes**

Run: `PYTHONPATH=backend .venv/bin/python -m pytest backend/tests/ -q`
Run: `.venv/bin/python -m pyflakes backend/app backend/tests`
Expected: all green, clean.

- [ ] **Step 7: Commit**

```bash
git add backend/app/services/acquisition.py backend/tests/test_optimizator_acquisition.py
git commit -m "feat(M2): wire OptimizatorClient into execute_confirmed_order + confirm_order

confirm_order замораживает фиксированную цену (prices) для optimizator — денежный
гейт требует видимой суммы. execute_confirmed_order: check_domain как замена
отсутствующему find_order (идемпотентность без листинга заказов), OptimizatorError
(чистый отказ) и OptimizatorAmbiguous (транспорт упал, maybe_sent) — тот же контракт,
что у backorder AmbiguousSend. Хард-гейт confirmed_by_human/refuse_dirty/атомарный
claim — НЕ трогали, они выше этой ветки."
```

---

### Task 3: `poll_orders()` — stuck-`ordering` recovery for optimizator

**Files:**
- Modify: `backend/app/services/acquisition.py` (append optimizator recovery loop inside `poll_orders`, after the existing backorder loop, before the function's final `return`)
- Test: `backend/tests/test_order_recovery.py` (extend — this file already covers the exact same F11/F12 class of bug for backorder; new tests belong alongside them)

**Interfaces:**
- Consumes: `OptimizatorClient.check_domain`, `OptimizatorError`, `OptimizatorAmbiguous`
  from Task 1; the existing `_settle(db, o, **values)` and `_claim_expired(o)` helpers
  already in `acquisition.py` (module-level, no import needed — same file).

- [ ] **Step 1: Write the failing tests**

Read `backend/tests/test_order_recovery.py` FIRST — it already has `_ProcessKilled`,
`_approved`, `_orders` helpers for simulating a killed-mid-execute scenario for
backorder. Add analogous tests for optimizator (adapt the helpers' `source`/`provider`
values; match this file's existing structure rather than the illustrative sketch below
if they differ):

```python
def test_optimizator_stuck_ordering_recovers_when_registered(monkeypatch):
    """Процесс убили между claim и ответом ПОСЛЕ реальной регистрации — check_domain
    подтверждает "наш", строка выходит в ordered, а не висит вечно (F11 для optimizator)."""
    monkeypatch.setattr("app.integrations.optimizator.OptimizatorClient.check_domain",
                        lambda self, domain: {"data_end": "02.12.2027", "domain": domain.upper()})
    did = _approved("recovered.ru")
    # ... завести заказ provider="optimizator", подтвердить, вручную перевести в
    # 'ordering' с claimed_at в прошлом (протухший claim) — см. как это делает
    # существующий backorder-тест в этом же файле для 'ordering' + STUCK_CLAIM_MIN.
    result = acquisition.poll_orders()
    # assert строка теперь 'ordered'


def test_optimizator_stuck_ordering_recovers_when_not_registered(monkeypatch):
    """Тот же обрыв, но регистрация не прошла — check_domain падает, строка уходит в
    failed (деньги не считаем потраченными — не подтверждено, что списание было на
    отправке)."""
    monkeypatch.setattr(
        "app.integrations.optimizator.OptimizatorClient.check_domain",
        lambda self, domain: (_ for _ in ()).throw(
            __import__("app.integrations.optimizator", fromlist=["OptimizatorError"]).OptimizatorError("not found", 404)))
    did = _approved("lost.ru")
    # ... аналогично, protухший 'ordering' с provider="optimizator"
    result = acquisition.poll_orders()
    # assert строка теперь 'failed', claimed_at is None


def test_optimizator_fresh_ordering_claim_is_not_touched():
    """Свежий claim (execute только что в полёте) — recovery-цикл его не трогает,
    как и у backorder."""
    # ... 'ordering' с claimed_at = now() (не протухший)
    result = acquisition.poll_orders()
    # assert строка осталась 'ordering', sending-счётчик учёл её (если функция его
    # возвращает для optimizator тоже — смотри по факту итоговой реализации Step 2)
```

**Note for implementer:** the sketch above uses an awkward dynamic import for
`OptimizatorError` inside a lambda purely to keep this brief self-contained — in the
real test file just `from app.integrations.optimizator import OptimizatorError` at
the top, matching how this file already imports `backorder`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=backend .venv/bin/python -m pytest backend/tests/test_order_recovery.py -v -k optimizator`
Expected: FAIL (no recovery loop for optimizator exists yet — those rows stay `ordering` forever).

- [ ] **Step 3: Implement the recovery loop**

In `backend/app/services/acquisition.py`, inside `poll_orders()`, insert a new block
AFTER the existing backorder `with SessionLocal() as db: ... for o in rows: ...`
block (which ends right before the function's closing `return {"checked": matched,
...}` — currently around line 661-666) and BEFORE that `return`:

```python
    # --- optimizator: застрявший 'ordering' (F11-класс, но для второго канала) ---
    # ДО этого транспорта (Task 1/2) execute для optimizator падал ДО отправки
    # (NotImplementedError) — окна для застревания не было. Теперь оно есть: тот же
    # риск, что и у backorder, только источник правды другой (нет client_orders()/
    # find_order — check_domain по одному домену, см. design doc).
    from app.integrations.optimizator import OptimizatorClient, OptimizatorError, OptimizatorAmbiguous
    opt_stuck = 0
    opt_recovered = 0
    with SessionLocal() as db:
        rows = db.execute(
            select(AcquisitionOrder).where(
                AcquisitionOrder.provider == "optimizator",
                AcquisitionOrder.status == "ordering")
        ).scalars().all()
        oc = OptimizatorClient()
        for o in rows:
            if not _claim_expired(o):
                sending += 1
                continue
            d = db.get(Domain, o.domain_id)
            try:
                existing = oc.check_domain(d.domain) if d else None
            except (OptimizatorError, OptimizatorAmbiguous):
                existing = None
            if existing is not None:
                values = {"status": "ordered", "claimed_at": None,
                          "result": {**(o.result or {}), "data_end": existing.get("data_end"),
                                     "note": "восстановлено после обрыва: check_domain "
                                             "подтвердил регистрацию"}}
            else:
                values = {"status": "failed", "claimed_at": None,
                          "result": {**{k: v for k, v in (o.result or {}).items()
                                        if k != "maybe_sent"},
                                     "error": "отправка оборвалась, check_domain не "
                                              "подтвердил регистрацию — деньги, возможно, "
                                              "не ушли. Можно повторить или снять заявку."}}
            if not _settle(db, o, **values):
                db.rollback()
                continue
            db.commit()
            opt_stuck += 1
            if values["status"] == "ordered":
                opt_recovered += 1
    matched += opt_stuck
```

**Note for implementer:** `sending`, `matched` are already local variables in this
function from the backorder loop above — reuse them (don't shadow with new locals) so
the final `return` line's counts stay accurate. Read the full existing function body
first to confirm exact variable names/scoping before inserting — this brief's code is
complete and correct in intent, but verify it slots into the REAL surrounding scope
without a stray re-declaration.

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=backend .venv/bin/python -m pytest backend/tests/test_order_recovery.py -v`
Expected: all PASS (existing backorder F11/F12 tests + new optimizator tests).

- [ ] **Step 5: Full suite + pyflakes**

Run: `PYTHONPATH=backend .venv/bin/python -m pytest backend/tests/ -q`
Run: `.venv/bin/python -m pyflakes backend/app backend/tests`
Expected: all green, clean.

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/acquisition.py backend/tests/test_order_recovery.py
git commit -m "fix(M2): recover stuck optimizator 'ordering' claims via check_domain

Closes the gap the poll_orders docstring already flagged: real transport (Task 1/2)
opens a window where a killed process leaves an optimizator order stuck in 'ordering'
forever. Recovery reuses the existing _settle() ABA-safe helper — same race
protection as the backorder F11 fix, check_domain instead of client_orders() as the
source of truth (no domain->order listing exists for this provider)."
```

---

### Task 4: UI — provider choice on queue-add, provider-aware confirm form, optional `/diag` ping

**Files:**
- Modify: `backend/app/templates/domains.html:254`, `backend/app/templates/pool.html:130`
  (queue-add form — add provider select)
- Modify: `backend/app/templates/queue.html:83-93` (confirm form — branch by provider)
- Modify: `backend/app/api/panel.py` (optional — `/diag` ping row for Optimizator)
- Test: read `backend/tests/test_web_fixes.py`/`test_order_uniqueness.py` first (both
  already touch queue-related rendering) and extend whichever fits; also add/extend a
  `/diag` test if that route has one.

**Interfaces:**
- Consumes: `Domain.lane` (existing column) for the provider preselect; `o.provider`
  (existing field, already rendered as plain text in `queue.html:56`) for the confirm
  form branch.

- [ ] **Step 1: Write the failing tests**

Read the nearest existing test that renders `/domains` or `/queue` through the panel's
`TestClient` (in `test_web_fixes.py`/`test_order_uniqueness.py`) and copy its harness
setup. Add:

```python
def test_queue_add_form_has_provider_select(client):
    """Кнопка «＋ в очередь выкупа» должна давать выбор канала — сегодня форма
    всегда шлёт provider=backorder неявно, второй канал был недостижим из панели."""
    # ... создать approved-домен, GET /domains, найти его форму /domains/{id}/queue
    # assert '<select name="provider"' in response.text (в контексте этой формы)


def test_queue_add_preselects_optimizator_for_free_lane():
    """lane='free' -> optimizator предвыбран (CLAUDE.md: «свободные чистые → optimizator»)."""
    # ... approved-домен с lane="free", GET /domains, найти <option value="optimizator" selected>


def test_confirm_form_hides_tariff_select_for_optimizator():
    """Заказ provider=optimizator в pending_confirm НЕ должен показывать backorder-
    тарифную сетку (её не существует для этого канала) — иначе бессмысленный выбор."""
    # ... создать order provider="optimizator" status="pending_confirm", GET /queue
    # assert 'name="bid_rub"' NOT in the row for this specific order
    # assert цена (o.cost, если уже выставлена confirm_order'ом — тут ещё pending,
    # так что скорее просто отсутствие тарифного select и наличие текста с фикс. ценой,
    # если UI её уже как-то показывает; сверить с фактическим содержимым шаблона)
```

**Note for implementer:** these three tests are deliberately sketched, not literal —
the panel's HTML structure (exact form/row markup) must be read from the actual
templates before asserting substrings, or the tests will be testing a fiction. Write
them against the REAL rendered HTML, not this brief's guess of it.

- [ ] **Step 2: Run tests to verify they fail**

Expected: FAIL (no provider select exists in either template yet).

- [ ] **Step 3: Add the provider select to the queue-add form**

In `backend/app/templates/domains.html`, the current form (line 254):

```html
          <form class="inline" method="post" action="/domains/{{ d.id }}/queue">
            <button class="btn-sm btn-acc" title="заявка → подтверждение человеком → отправка провайдеру">＋ в очередь выкупа</button></form>
```

becomes:

```html
          <form class="inline" method="post" action="/domains/{{ d.id }}/queue" style="display:flex; gap:6px; align-items:center">
            <select name="provider" title="куда отправлять заказ: backorder — ставка на перехват (ценные дропы), optimizator — фиксированная цена (свободные чистые)">
              <option value="backorder" {{ 'selected' if d.lane != 'free' }}>backorder</option>
              <option value="optimizator" {{ 'selected' if d.lane == 'free' }}>optimizator</option>
            </select>
            <button class="btn-sm btn-acc" title="заявка → подтверждение человеком → отправка провайдеру">＋ в очередь выкупа</button></form>
```

Apply the SAME change to `backend/app/templates/pool.html:130` (identical form
structure — confirm via `Read` before editing, in case the two files' markup has
diverged since this brief was written).

- [ ] **Step 4: Branch the confirm form by provider**

In `backend/app/templates/queue.html`, the current unconditional block (lines 83-93,
inside the `{% elif o.status == 'pending_confirm' and not o.confirmed %}` branch)
renders a `<select name="bid_rub">` tariff picker. Wrap it:

```html
        {% elif o.status == 'pending_confirm' and not o.confirmed %}
          {% if o.provider == 'backorder' %}
          <form class="inline" method="post" action="/queue/{{ o.id }}/confirm"
                onsubmit="return confirm('Подтвердить выкуп {{ o.domain }} со ставкой ' + this.bid_rub.options[this.bid_rub.selectedIndex].text + '? Это денежный гейт — дальше заказ уйдёт провайдеру и сумма спишется с баланса.')">
            <select name="bid_rub" title="тариф backorder = ставка: выше тариф → больше регистраторов → выше шанс перехвата">
              <!-- ...существующие option'ы тарифов, без изменений... -->
            </select>
            <button class="btn-sm btn-acc" title="поднять денежный гейт">✓ подтвердить</button>
          </form>
          {% else %}
          <form class="inline" method="post" action="/queue/{{ o.id }}/confirm"
                onsubmit="return confirm('Подтвердить выкуп {{ o.domain }} через optimizator? Фиксированная цена спишется с баланса при отправке. Это денежный гейт.')">
            <button class="btn-sm btn-acc" title="optimizator: цена фиксированная (не выбор человека) — подтверждение поднимает денежный гейт, цену зафиксирует confirm_order">✓ подтвердить (фикс. цена)</button>
          </form>
          {% endif %}
```

**Note for implementer:** the "существующие option'ы тарифов" comment marks where the
CURRENT tariff `<option>` loop already lives in the file — copy it verbatim into the
`{% if o.provider == 'backorder' %}` branch, do not reconstruct it from scratch (read
the actual current lines 85-92 for the exact Jinja loop over tariffs).

- [ ] **Step 5 (optional): `/diag` Optimizator ping**

Read `backend/app/api/panel.py`'s `diag_view`/`diag_refresh` (around lines 390-405)
to find where `BackorderClient().ping()` (or equivalent) is already pinged, and add
an `OptimizatorClient().ping()` row alongside it, following that exact pattern
(read-only, real network call, honest fail — no `skip`). If this doesn't cleanly fit
the existing structure without deeper changes than a one-row addition, skip it and
note the deferral in the task report — it's explicitly optional per the design doc.

- [ ] **Step 6: Run tests to verify they pass**

Run whatever command Step 1's harness uses (likely `PYTHONPATH=backend .venv/bin/python -m pytest backend/tests/test_web_fixes.py backend/tests/test_order_uniqueness.py -v`, adjust to wherever the new tests actually live).
Expected: all PASS.

- [ ] **Step 7: Full suite + pyflakes + a visual sanity check**

Run: `PYTHONPATH=backend .venv/bin/python -m pytest backend/tests/ -q`
Run: `.venv/bin/python -m pyflakes backend/app backend/tests`
Expected: all green, clean. If Playwright/chrome-devtools tooling is available in
this environment, render `/domains` and `/queue` once each for a domain with
`lane='free'` and an `optimizator`-provider pending order, to confirm the templates
actually produce sane HTML (design-system classes only, per CLAUDE.md's `/DESIGN.md`
contract) — if not available, note that as a concern in the task report rather than
skipping silently.

- [ ] **Step 8: Commit**

```bash
git add backend/app/templates/domains.html backend/app/templates/pool.html \
        backend/app/templates/queue.html
# (+ backend/app/api/panel.py if Step 5 was done)
git commit -m "feat(M2): panel UI for the optimizator acquisition channel

Queue-add gets a provider selector (preselected by Domain.lane — 'free' suggests
optimizator, matching CLAUDE.md's 'свободные чистые → optimizator'); queue confirm
form no longer shows a meaningless backorder tariff picker for optimizator orders."
```

---

## Self-Review Notes (completed during plan authoring)

- **Spec coverage:** all 6 architecture sections of the design doc map to a task:
  §1 transport → Task 1; §2 execute + §4 confirm-price → Task 2; §3 poll_orders → Task
  3; §5 UI + §6 diag → Task 4. Nothing in the spec's "Критерии приёмки" is missing a
  task.
  Placeholder scan: no TBD/TODO. Two spots are deliberately marked "adjust to the real
  file" rather than fully literal (Task 2's fixture column check, Task 4's HTML
  assertions) — these are read-first-then-write instructions for the implementer, not
  missing requirements; the tests' INTENT is fully specified in every case.
- **Type consistency:** `OptimizatorClient.register()` returns `{"order_id": N}`
  consistently across Task 1's implementation/tests and Task 2's consumption
  (`res.get("order_id")`, unchanged existing code). `check_domain()` raises rather
  than returning `None` consistently in Task 1's implementation, Task 2's
  `execute_confirmed_order` try/except, and Task 3's recovery loop try/except — this
  is the exact inconsistency this plan's own spec-authoring self-review caught and
  fixed before writing task code, and it is now uniform across all three tasks.
- **Money-safety cross-check:** every task that reaches `execute_confirmed_order`'s
  branch or `poll_orders`' new loop sits below the existing `confirmed_by_human`
  gate/`refuse_dirty`/`uq_open_order_per_domain` — none of the diffs in this plan
  touch those lines. Task 3 reuses `_settle()` rather than a bare UPDATE, matching
  the ABA-safety already proven for backorder (F11/F12) — a second bespoke
  race-handling implementation for the same class of bug was rejected in favor of
  reusing the audited one.

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-16-optimizator-integration.md`.
Proceeding with **Subagent-Driven Development**, same as Thread D earlier tonight —
fresh implementer per task, task-reviewer after each (combine-reviewer, opus, given
the money-adjacency), final whole-branch review — branch left ready for morning
review, no merge/push without explicit sign-off.
