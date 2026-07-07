# M1 Audit Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Починить находки аудита M1 (поиск доноров → оценка) из `docs/superpowers/specs/2026-07-07-m1-audit-findings.md`: 3 Critical, 7 Important, 13 Minor.

**Architecture:** Точечные фиксы в существующих модулях `integrations/` (транспорт) и `services/` (логика), без новой схемы БД. Каждая задача — TDD: сначала падающий тест, потом минимальная реализация, оффлайн на SQLite-харнессе.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.x, pytest, оффлайн-SQLite (`backend/tests/conftest.py`), httpx+tenacity.

## Global Constraints

- **Оффлайн-тесты:** весь харнесс на in-memory SQLite; `conftest.py` autouse-фикстура патчит `cfg.SOURCES_ENABLED` в backorder-only и глушит живую сеть — НИ ОДИН тест не ходит в сеть. Моки интеграций — per-test.
- **Прогон тестов:** `.venv/bin/python -m pytest backend/tests/ -q` (из корня репо). Линт: `.venv/bin/python -m pyflakes backend/app backend/tests`. Оба должны быть зелёными после каждой задачи. Стартовый baseline: **148 passed, pyflakes чист**.
- **Конвенции:** `integrations/` = только транспорт, логика в `services/`; каждый модуль имеет `if __name__ == "__main__"` self-check без сети — при правке функции обновлять и его.
- **Хард-гейты не трогать:** деньги (`confirmed_by_human`) и редактура (`edited`) — вне этого плана; ничего в acquisition/publish не менять.
- **Формат whois A-Parser (снят вживую 2026-07-07):** свёртка одной строкой `<domain> - registered: 0|1, expire: DD.MM.YYYY|none, creation: DD.MM.YYYY|none` (НЕ сырой whois). `registered: 0` = свободен, `registered: 1` = занят.
- **Данные бокса:** бокс = Windows + Docker Desktop, `D:\combine_machine`, PowerShell; A-Parser `:9091` медленный (холодный whois до ~30с). Живая разметка cctld/reg_ru/sweb НЕ выверена — эти источники выключаем дефолтом.

---

### Task 1: whois-парсер под реальный формат A-Parser (Critical C1)

Сейчас `_parse_whois_available`/`_parse_whois_created` не понимают свёртку A-Parser → `None/None` на ЛЮБОМ домене → T1 (приобретаемость+возраст) мёртв, сырые источники вечно unresolved, free-lane недостижим. **Побочно чинит I2 (starvation):** свёртка `registered: 0|1` TLD-агностична, поэтому после фикса `available` определяется всегда (кроме сетевого сбоя, который и так raise) — вечно-unresolved домены исчезают.

**Files:**
- Modify: `backend/app/integrations/aparser.py:12-43` (регексы + `_parse_whois_created` + `_parse_whois_available` + `__main__` self-check)
- Test: `backend/tests/test_sources.py` (добавить кейсы на свёртку)

**Interfaces:**
- Consumes: ничего нового.
- Produces: `_parse_whois_available(text) -> bool | None` и `_parse_whois_created(text) -> datetime | None` — сигнатуры без изменений; `whois_probe(domain) -> {"available": bool|None, "created": datetime|None}` без изменений. Меняется только внутренняя логика парсинга.

- [ ] **Step 1: Написать падающие тесты на живые строки свёртки**

Добавить в `backend/tests/test_sources.py`:

```python
def test_whois_svertka_taken():
    from app.integrations.aparser import _parse_whois_available, _parse_whois_created
    txt = "python.org - registered: 1, expire: 28.03.2033, creation: 27.03.1995\n"
    assert _parse_whois_available(txt) is False
    d = _parse_whois_created(txt)
    assert (d.year, d.month, d.day) == (1995, 3, 27) and d.tzinfo is not None


def test_whois_svertka_free():
    from app.integrations.aparser import _parse_whois_available, _parse_whois_created
    txt = "free-drop-nonexistent-2026.ru - registered: 0, expire: none, creation: none\n"
    assert _parse_whois_available(txt) is True
    assert _parse_whois_created(txt) is None


def test_whois_svertka_free_rf():
    from app.integrations.aparser import _parse_whois_available
    assert _parse_whois_available("пример.рф - registered: 0, expire: none, creation: none\n") is True


def test_whois_old_format_still_works():
    # старый сырой whois — фолбэк (пресет A-Parser может отдать иное)
    from app.integrations.aparser import _parse_whois_available, _parse_whois_created
    txt = "domain: X.RU\ncreated: 2010.11.15\nnserver: ns.x.ru"
    assert _parse_whois_available(txt) is False
    assert _parse_whois_created(txt).year == 2010
```

- [ ] **Step 2: Прогнать — упадут**

Run: `.venv/bin/python -m pytest backend/tests/test_sources.py -q -k svertka`
Expected: FAIL (`test_whois_svertka_taken` даёт `available=None`, не `False`).

- [ ] **Step 3: Реализовать парсер свёртки с фолбэком на старый формат**

В `backend/app/integrations/aparser.py` заменить блок регексов и обе функции:

```python
# .ru/.рф/gTLD сырой whois (фолбэк): 'created: 2010.11.15' / 'Creation Date: 2004-03-15T...'
_RE_RU = re.compile(r"created:\s*(\d{4})\.(\d{2})\.(\d{2})", re.I)
_RE_GTLD = re.compile(r"creation date:\s*(\d{4})-(\d{2})-(\d{2})", re.I)
# свёртка A-Parser Net::Whois (основной формат, снят вживую 2026-07-07):
#   '<domain> - registered: 0|1, expire: ..., creation: DD.MM.YYYY|none'
_RE_SVERTKA_REG = re.compile(r"registered:\s*([01])\b", re.I)
_RE_SVERTKA_CREATION = re.compile(r"creation:\s*(\d{2})\.(\d{2})\.(\d{4})", re.I)


def _parse_whois_created(text: str) -> datetime | None:
    """Дата регистрации из whois-ответа. Свёртка A-Parser (DD.MM.YYYY) или сырой whois
    (.ru YYYY.MM.DD / gTLD YYYY-MM-DD). Самая ранняя найденная, UTC. None если нет."""
    found = []
    for dy, mo, y in _RE_SVERTKA_CREATION.findall(text or ""):           # DD.MM.YYYY
        try:
            found.append(datetime(int(y), int(mo), int(dy), tzinfo=timezone.utc))
        except ValueError:
            pass
    for rx in (_RE_RU, _RE_GTLD):                                        # YYYY.MM.DD / YYYY-MM-DD
        for y, mo, dy in rx.findall(text or ""):
            try:
                found.append(datetime(int(y), int(mo), int(dy), tzinfo=timezone.utc))
            except ValueError:
                pass
    return min(found) if found else None


# маркеры сырого whois (фолбэк, если пресет отдаёт не свёртку)
_FREE_MARKERS = ("no entries found", "not found", "no match", "no object found",
                 "available for registration", "нет данных", "not registered")
_REG_MARKERS = ("nserver", "registrar", "person:", "org:", "paid-till", "domain:")


def _parse_whois_available(text: str) -> bool | None:
    """True — свободен, False — занят, None — не определить.
    Свёртка A-Parser 'registered: 0|1' приоритетна; иначе — маркеры сырого whois."""
    low = (text or "").lower()
    m = _RE_SVERTKA_REG.search(low)
    if m:
        return m.group(1) == "0"                     # 0 = свободен, 1 = занят
    if any(w in low for w in _FREE_MARKERS):
        return True
    if _RE_RU.search(low) or _RE_GTLD.search(low) or any(w in low for w in _REG_MARKERS):
        return False
    return None
```

- [ ] **Step 4: Обновить `__main__` self-check в aparser.py**

Заменить существующий `if __name__ == "__main__"` блок (если есть; иначе добавить в конец файла) на:

```python
if __name__ == "__main__":  # pure whois-parse self-check (no network)
    assert _parse_whois_available("x.ru - registered: 1, expire: none, creation: 01.02.2020") is False
    assert _parse_whois_available("x.ru - registered: 0, expire: none, creation: none") is True
    assert _parse_whois_created("x.ru - registered: 1, expire: none, creation: 01.02.2020").year == 2020
    assert _parse_whois_created("x.ru - registered: 0, expire: none, creation: none") is None
    assert _parse_whois_available("No entries found") is True                      # фолбэк
    assert _parse_whois_available("nserver: ns1.x.ru") is False                    # фолбэк
    assert _parse_whois_available("мусор без маркеров") is None
    print("aparser whois-parse ok")
```

- [ ] **Step 5: Прогнать тесты — зелёные**

Run: `.venv/bin/python -m pytest backend/tests/test_sources.py -q` затем `.venv/bin/python backend/app/integrations/aparser.py`
Expected: PASS + `aparser whois-parse ok`. Существующие `test_whois_created_ru/gtld/junk` и `test_parse_whois_available` остаются зелёными (фолбэк-ветки сохранены).

- [ ] **Step 6: Полный прогон + коммит**

Run: `.venv/bin/python -m pytest backend/tests/ -q` (Expected: 152 passed) и `.venv/bin/python -m pyflakes backend/app backend/tests` (чисто).

```bash
git add backend/app/integrations/aparser.py backend/tests/test_sources.py
git commit -m "фикс(M1 C1): whois-парсер под реальную свёртку A-Parser (registered/creation)"
```

---

### Task 2: blacklist fail-closed + видимость в /diag (Critical C2)

Spamhaus-гейт молча пропускает всё как «чистое»: с системного резолвера тест-поинт даёт NXDOMAIN → «не листнут», `None` не попадает в `errors` → risk-guard молчит, а blacklist вообще нет в `/diag`. Делаем fail-closed: контрольный запрос тест-поинта (он ВСЕГДА листнут); если резолвер его не видит — проверка недоступна → raise → risk-guard уводит в manual.

**Files:**
- Modify: `backend/app/integrations/blacklist.py:18-50` (`_resolve` различает errno, control-probe, `is_blacklisted`)
- Modify: `backend/app/services/scoring.py:146-155` (None → `sig["errors"]`)
- Modify: `backend/app/services/diagnostics.py:36-45` (добавить строку blacklist в `_spec`)
- Test: `backend/tests/test_m1_fixes.py` (fail-closed поведение)

**Interfaces:**
- Consumes: ничего нового.
- Produces: `BlacklistClient.is_blacklisted(domain) -> bool | None` — теперь RAISE `RuntimeError`, если резолвер не видит Spamhaus (было: тихий `False`). `None` только на транзиентном dnspython-сбое.

- [ ] **Step 1: Падающий тест на fail-closed**

Добавить в `backend/tests/test_m1_fixes.py`:

```python
def test_blacklist_raises_when_resolver_cannot_reach_spamhaus(monkeypatch):
    from app.integrations import blacklist
    c = blacklist.BlacklistClient()
    # резолвер отдаёт NXDOMAIN даже на тест-поинт (публичный резолвер заблокирован Spamhaus)
    monkeypatch.setattr(c, "_resolve", lambda host: None)
    import pytest
    with pytest.raises(RuntimeError):
        c.is_blacklisted("example.com")


def test_blacklist_none_goes_to_errors_and_downgrades(monkeypatch):
    # is_blacklisted вернул None (транзиент) -> в sig.errors -> risk-guard -> manual scored
    from app.services import scoring
    sig = {"errors": []}
    st = {"approve_at": 0.7, "manual_review_at": 0.4}
    # прямой юнит на _decide: approved + blacklist-ошибка -> scored
    sig_err = {"errors": ["blacklist:unavailable"]}
    assert scoring._decide(0.9, sig_err, 0.7, 0.4) == "scored"
```

- [ ] **Step 2: Прогнать — упадёт первый**

Run: `.venv/bin/python -m pytest backend/tests/test_m1_fixes.py -q -k blacklist`
Expected: `test_blacklist_raises...` FAIL (сейчас возвращает False, не raise).

- [ ] **Step 3: Реализовать fail-closed в blacklist.py**

В `backend/app/integrations/blacklist.py` заменить `_resolve` (stdlib-ветку) и `is_blacklisted`:

```python
import socket
from app.config import settings

_ZONES = ("dbl.spamhaus.org", "multi.surbl.org")
_TESTPOINT = "test.dbl.spamhaus.org"     # всегда листнут (127.0.1.2) — контроль доступности


class BlacklistClient:
    _control_ok: bool | None = None       # кэш контроля на процесс

    def _resolve(self, host: str) -> str | None:
        """A-запись IP или None на NXDOMAIN. Транзиентный сбой (не NXDOMAIN) — RAISE,
        чтобы не трактовать недоступность как «чисто»."""
        if settings.DNS_RESOLVER:
            import dns.resolver
            resolver = dns.resolver.Resolver(configure=False)
            resolver.nameservers = [settings.DNS_RESOLVER]
            try:
                return resolver.resolve(host, "A")[0].address
            except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
                return None
        try:
            return socket.gethostbyname(host)
        except socket.gaierror as e:
            if e.errno in (socket.EAI_NONAME, getattr(socket, "EAI_NODATA", socket.EAI_NONAME)):
                return None                   # настоящий NXDOMAIN — не листнут в этой зоне
            raise                             # резолвер/сеть отвалились — вверх, не «чисто»

    def _ensure_control(self) -> None:
        """Тест-поинт Spamhaus всегда листнут; если наш резолвер его не видит —
        публичный резолвер заблокирован, проверка бессмысленна → RAISE (fail-closed)."""
        if BlacklistClient._control_ok is None:
            try:
                ip = self._resolve(_TESTPOINT)
            except OSError:
                ip = None
            BlacklistClient._control_ok = bool(ip and ip.startswith("127."))
        if not BlacklistClient._control_ok:
            raise RuntimeError(
                "blacklist: резолвер не видит Spamhaus (тест-поинт не листнут) — задай DNS_RESOLVER")

    def is_blacklisted(self, domain: str) -> bool | None:
        """True = листнут, False = чист на всех зонах, None = транзиентный сбой резолвера."""
        self._ensure_control()
        for zone in _ZONES:
            try:
                ip = self._resolve(f"{domain}.{zone}")
            except OSError:
                return None                   # транзиент -> None -> scoring уведёт в errors
            if ip is None:
                continue
            if ip.startswith("127.255.255."):
                raise RuntimeError("Spamhaus blocked public resolver — задай DNS_RESOLVER")
            if ip.startswith("127."):
                return True
        return False

    def ping(self) -> bool:
        try:
            return socket.gethostbyname(_TESTPOINT).startswith("127.")
        except OSError:
            return False
```

- [ ] **Step 4: None → errors в scoring `_funnel`**

В `backend/app/services/scoring.py` в T2-блоке blacklist (сейчас ~строки 146-151) добавить обработку None ПОСЛЕ существующего try/except:

```python
    try:
        sig["blacklisted"] = c["blacklist"].is_blacklisted(d.domain)
        if sig["blacklisted"] is True:
            return "blacklist"
    except Exception as e:  # noqa: BLE001
        sig["errors"].append(f"blacklist:{type(e).__name__}")
    if sig.get("blacklisted") is None and "blacklisted" in sig:
        sig["errors"].append("blacklist:unavailable")   # транзиент -> risk-guard -> manual
```

- [ ] **Step 5: Добавить blacklist в /diag**

В `backend/app/services/diagnostics.py` в списке `_spec()` после строки `aparser` добавить:

```python
        ("blacklist", "Spamhaus/SURBL", "M1 · спам-лист", "1", "M1", False,
         lambda: __import__("app.integrations.blacklist", fromlist=["x"]).BlacklistClient().ping()),
```

(`critical=False` — сбой blacklist уводит домен в manual, не роняет конвейер.)

- [ ] **Step 6: Прогнать + коммит**

Run: `.venv/bin/python -m pytest backend/tests/ -q` (Expected: 154 passed), `pyflakes` чисто, `.venv/bin/python backend/app/integrations/blacklist.py` (если есть self-check — обновить тест-поинт в нём на `_TESTPOINT`).

```bash
git add backend/app/integrations/blacklist.py backend/app/services/scoring.py backend/app/services/diagnostics.py backend/tests/test_m1_fixes.py
git commit -m "фикс(M1 C2): blacklist fail-closed (контроль тест-поинта) + None→errors + /diag"
```

---

### Task 3: cctld deadline-aware + выключить сырые источники дефолтом (Critical C3)

После фикса C1 whois начнёт распознавать `registered: 1` у ещё-не-освободившихся дропов → сырой домен с будущей датой дропа уходил бы в `not_acquirable` НАВСЕГДА («логическая бомба»). Плюс cctld/reg_ru/sweb собирают мусор / недоступны с непроверенной разметкой — выключаем дефолтом.

**Files:**
- Modify: `backend/app/services/scoring.py:127-137` (deadline-aware ветка `av is False`)
- Modify: `backend/app/services/scoring_config.py:54` (`SOURCES_ENABLED` — сырые в False)
- Test: `backend/tests/test_funnel.py` (deadline-aware)

**Interfaces:**
- Consumes: `_clients(whois={"available": False, "created": None})` (существующий хелпер теста), `Domain.acquire_deadline`.
- Produces: поведение `_funnel` — при `av is False` для сырого источника (`d.lane != "bid"`) с будущим `acquire_deadline` возвращает `None` и ставит `sig["acquirability_unresolved"]=True` (домен остаётся `discovered`); при прошедшем/отсутствующем дедлайне — `"not_acquirable"` (как раньше).

- [ ] **Step 1: Падающие тесты на deadline-aware**

Добавить в `backend/tests/test_funnel.py`:

```python
def test_raw_source_future_deadline_stays_discovered():
    # сырой домен, whois «занят», но дедлайн дропа в будущем -> ждём дропа, не reject
    future = datetime.now(timezone.utc) + timedelta(days=5)
    did = _mk(domain="dropping.ru", lane=None, source="cctld",
              referring_domains=10, acquire_deadline=future)
    wb = _Wayback()
    out = scoring.score_domain(did, _clients(whois={"available": False, "created": None}, wayback=wb))
    assert out.get("unresolved") is True
    assert out["status"] == "discovered"
    assert wb.calls == 0                      # дорогой Wayback не тронут


def test_raw_source_no_deadline_taken_is_not_acquirable():
    did = _mk(domain="taken.ru", lane=None, source="cctld", referring_domains=10)
    wb = _Wayback()
    out = scoring.score_domain(did, _clients(whois={"available": False, "created": None}, wayback=wb))
    assert out["status"] == "rejected" and out["reject_reason"] == "not_acquirable"
    assert wb.calls == 0
```

- [ ] **Step 2: Прогнать — упадёт первый**

Run: `.venv/bin/python -m pytest backend/tests/test_funnel.py -q -k deadline`
Expected: `test_raw_source_future_deadline...` FAIL (сейчас `not_acquirable`, не unresolved).

- [ ] **Step 3: Реализовать deadline-aware ветку**

В `backend/app/services/scoring.py` в блоке T1 заменить ветку выбора лейна (сейчас ~строки 127-137):

```python
    if d.lane == "bid":
        sig["lane"] = "bid"
    else:
        av = pr.get("available")
        if av is True:
            sig["lane"] = "free"                    # свободен к регистрации
        elif av is False:
            # занят СЕЙЧАС. Для сырого источника это может быть дропающийся домен, ещё
            # зарегистрированный до своей даты: известный будущий дедлайн -> ждём, оставляем
            # discovered (перепробуем после дропа). Нет дедлайна / он в прошлом -> реально занят.
            dl = d.acquire_deadline
            if dl is not None and dl.tzinfo is None:
                dl = dl.replace(tzinfo=timezone.utc)
            if dl is not None and dl > now:
                sig["acquirability_unresolved"] = True
                return None
            return "not_acquirable"                 # занят, купить нельзя
        else:                                       # av is None — не определили
            sig["acquirability_unresolved"] = True
            return None
```

- [ ] **Step 4: Выключить сырые источники дефолтом**

В `backend/app/services/scoring_config.py:54` заменить:

```python
SOURCES_ENABLED = {"backorder": True, "cctld": False, "reg_ru": False, "sweb": False}  # сырые витрины выключены до выверки живой разметки (аудит 2026-07-07)
```

(conftest патчит `SOURCES_ENABLED` в backorder-only, поэтому `test_settings.py::test_get_settings_seeds_defaults` остаётся верным — обе стороны сравнения видят патченный дефолт.)

- [ ] **Step 5: Прогнать + коммит**

Run: `.venv/bin/python -m pytest backend/tests/ -q` (Expected: 156 passed), `pyflakes` чисто.

```bash
git add backend/app/services/scoring.py backend/app/services/scoring_config.py backend/tests/test_funnel.py
git commit -m "фикс(M1 C3): deadline-aware not_acquirable + сырые источники выкл дефолтом"
```

---

### Task 4: IDNA-канонизация всех источников (Important I1+I3, Minor M3)

`.рф` из backorder молча выбрасывается ASCII-регексом; punycode и кириллица одного домена дают ДВЕ строки; `www.example.ru` ≠ `example.ru`. Единая канонизация в одной точке (punycode).

**Files:**
- Modify: `backend/app/services/discovery.py:13,30-44,93-101` (`canonical_domain` + `normalize_row` + дедуп-петля)
- Test: `backend/tests/test_sources.py`

**Interfaces:**
- Consumes: stdlib `str.encode("idna")`.
- Produces: `canonical_domain(raw: str) -> str | None` в `discovery.py` — punycode-канон или None для мусора; `normalize_row` теперь валидирует через него; `run_discovery` канонизирует ключ дедупа для ВСЕХ источников.

- [ ] **Step 1: Падающие тесты**

Добавить в `backend/tests/test_sources.py`:

```python
def test_canonical_domain():
    from app.services.discovery import canonical_domain
    assert canonical_domain("Пример.РФ") == "xn--e1afmkfd.xn--p1ai"
    assert canonical_domain("xn--e1afmkfd.xn--p1ai") == "xn--e1afmkfd.xn--p1ai"   # уже punycode
    assert canonical_domain("www.Example.COM.") == "example.com"                   # www + регистр + точка
    assert canonical_domain("under_score.ru") is None                             # мусор
    assert canonical_domain("") is None
    assert canonical_domain("support@mail.ru") is None                            # e-mail — не домен


def test_normalize_row_keeps_rf():
    from app.services.discovery import normalize_row
    nr = normalize_row({"domainname": "пример.рф", "links": "7"})
    assert nr is not None and nr["domain"] == "xn--e1afmkfd.xn--p1ai"
```

- [ ] **Step 2: Прогнать — упадут**

Run: `.venv/bin/python -m pytest backend/tests/test_sources.py -q -k "canonical or keeps_rf"`
Expected: FAIL (`canonical_domain` не существует; `normalize_row("пример.рф")` даёт None).

- [ ] **Step 3: Реализовать `canonical_domain` и подключить в normalize_row**

В `backend/app/services/discovery.py` заменить `_DOMAIN_RE` и `normalize_row`:

```python
_DOMAIN_RE = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)+$")   # проверяем punycode-форму (ASCII)


def canonical_domain(raw) -> str | None:
    """Единая канон-форма домена для ВСЕХ источников: lower, без www./точки, IDN→punycode.
    None если не домен (мусор, e-mail, пустое, недопустимые метки)."""
    s = (raw or "").strip().lower().rstrip(".")
    if s.startswith("www."):
        s = s[4:]
    if not s or len(s) > 253 or "@" in s or " " in s:
        return None
    try:
        puny = s.encode("idna").decode("ascii")
    except (UnicodeError, ValueError):
        return None                       # пустая метка, >63, недопустимый символ
    return puny if _DOMAIN_RE.match(puny) else None


def normalize_row(row: dict) -> dict | None:
    """Одна строка фида backorder -> нормализованный кандидат (или None если мусор).
    backorder — bid-лейн из источника; тянем дедлайн/visitors/tic."""
    domain = canonical_domain(row.get("domainname"))
    if domain is None:
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

- [ ] **Step 4: Канонизировать ключ дедупа для всех источников**

В `backend/app/services/discovery.py` в `run_discovery` заменить дедуп-петлю (сейчас ~строки 93-101):

```python
    best: dict[str, dict] = {}
    for r in rows:
        d = canonical_domain(r.get("domain"))      # единый ключ: сырые источники тоже канонятся
        if not d:
            continue
        r["domain"] = d
        cur = best.get(d)
        if cur is None or (r.get("referring_domains") or 0) > (cur.get("referring_domains") or 0):
            best[d] = r
    candidates = best
```

- [ ] **Step 5: Обновить `__main__` self-check в discovery.py**

Заменить существующий self-check-блок на:

```python
if __name__ == "__main__":  # pure normalize self-check (no network)
    nr = normalize_row({"domainname": "Example.COM.", "links": "12"})
    assert nr["domain"] == "example.com" and nr["referring_domains"] == 12 and nr["lane"] == "bid"
    assert normalize_row({"domainname": "пример.рф", "links": 3})["domain"] == "xn--e1afmkfd.xn--p1ai"
    assert normalize_row({"domainname": "under_score.ru", "links": 1}) is None
    assert normalize_row({"domainname": "", "links": 5}) is None
    assert canonical_domain("www.a.ru") == "a.ru" and canonical_domain("x@y.ru") is None
    print("discovery normalize_row ok")
```

- [ ] **Step 6: Прогнать + коммит**

Быстрая проверка канона в реальном интерпретаторе:
Run: `.venv/bin/python -c "print('пример.рф'.encode('idna'))"` (Expected: `b'xn--e1afmkfd.xn--p1ai'`).
Run: `.venv/bin/python -m pytest backend/tests/ -q` (Expected: 158 passed), `pyflakes` чисто.

```bash
git add backend/app/services/discovery.py backend/tests/test_sources.py
git commit -m "фикс(M1 I1/I3/M3): единая IDNA-канонизация доменов всех источников"
```

---

### Task 5: Wayback порог покрытия + сохранение сигналов (Important I4, Minor M10+M11)

1 скачанный снапшот из 5 сейчас = «история проверена» → казино-дроп с одним чистым ранним снапшотом может auto-approve. Ставим порог покрытия. Плюс: `history_dirty`-путь теряет `wayback_checked/first_seen/age` (M10); стоп-словам не хватает RU-казино-брендов (M11).

**Files:**
- Modify: `backend/app/integrations/wayback.py:15-35,90-104` (стоп-слова + порог покрытия + `__main__`)
- Modify: `backend/app/services/scoring.py:158-165` (сохранить sig до `history_dirty`-return)
- Test: `backend/tests/test_m1_fixes.py`

**Interfaces:**
- Consumes: `WaybackClient.classify_history(domain, sample=5, polite=1.0)`.
- Produces: `classify_history` — `wayback_checked=True` только при `ok >= sample//2 + 1` (иначе False, как при пустом); поле `sampled` уже возвращается.

- [ ] **Step 1: Падающий тест на порог покрытия**

Добавить в `backend/tests/test_m1_fixes.py`:

```python
def test_wayback_partial_coverage_not_checked(monkeypatch):
    # 5 снапшотов в CDX, но скачался лишь 1 (остальные 429) -> НЕ «проверено»
    from app.integrations import wayback
    c = wayback.WaybackClient()
    monkeypatch.setattr(c, "get_snapshots", lambda dom, limit=400: [
        {"timestamp": "20150101000000", "original": f"http://x/{i}"} for i in range(5)])
    calls = {"n": 0}
    def _fetch(ts, orig):
        calls["n"] += 1
        if calls["n"] == 1:
            return "clean vpn review"
        raise RuntimeError("429")
    monkeypatch.setattr(c, "_fetch_raw", _fetch)
    h = c.classify_history("x.ru", sample=5, polite=0)
    assert h["sampled"] == 1 and h["wayback_checked"] is False


def test_wayback_ru_casino_brands():
    from app.integrations.wayback import _classify_text
    assert "casino" in _classify_text("Вулкан казино играть онлайн")   # бренд + слово
    assert "casino" in _classify_text("Azino777 и joycasino бонусы")
```

- [ ] **Step 2: Прогнать — упадут**

Run: `.venv/bin/python -m pytest backend/tests/test_m1_fixes.py -q -k wayback`
Expected: FAIL (`sampled==1` даёт `wayback_checked=True`; брендов нет в стоп-словах).

- [ ] **Step 3: Порог покрытия + бренды в wayback.py**

В `backend/app/integrations/wayback.py` в словарь `STOPWORDS["casino"]` добавить бренды:

```python
    "casino": ["casino", "roulette", "slots", "jackpot", "blackjack", "baccarat",
               "free spins", "casino bonus", "azino", "joycasino", "vulkan casino",
               "казино", "рулетк", "слот", "игровые автоматы", "джекпот",
               "азартны", "игровой клуб", "вулкан", "азино", "пинап", "пин ап"],
```

В `classify_history` заменить финальный блок (после цикла сэмплирования, где `ok` посчитан):

```python
        checked = ok >= (sample // 2 + 1)      # «проверено» только при покрытии большинства
        if not checked:
            # мало данных (систематический троттлинг archive.org) — нельзя выдавать чистый
            # вердикт по паре снапшотов; sig-гард в scoring уведёт в manual
            return {"prior_flags": {}, "first_seen": first_seen, "age_years": age_years,
                    "wayback_checked": False, "sampled": ok}

        all_cats = set().union(*cats_by_time) if cats_by_time else set()
        flags = {c: (c in all_cats) for c in STOPWORDS}
        early = cats_by_time[0] if cats_by_time else set()
        later = set().union(*cats_by_time[len(cats_by_time) // 2:]) if cats_by_time else set()
        flags["topic_switch"] = bool((later - early) & {"adult", "pharma", "casino", "gambling"})
        return {"prior_flags": flags, "first_seen": first_seen, "age_years": age_years,
                "wayback_checked": True, "sampled": ok}
```

Удалить прежнюю ветку `if ok == 0:` (её покрывает новый `if not checked:` — при ok==0 порог тоже не достигнут).

- [ ] **Step 4: Сохранить sig до history_dirty-return (M10)**

В `backend/app/services/scoring.py` в T3-блоке переставить порядок так, чтобы sig заполнялся ДО раннего выхода:

```python
    # T3 — история (дорого): только для приобретаемых выживших
    try:
        hist = c["wayback"].classify_history(d.domain)
        pf = hist.get("prior_flags") or {}
        sig["prior_flags"] = pf
        sig["wayback_checked"] = hist.get("wayback_checked")     # сохраняем ДО возможного выхода
        sig["first_seen"] = hist.get("first_seen")
        if sig.get("whois_created") is None and hist.get("age_years") is not None:
            sig["age_years"] = hist["age_years"]
        if any(pf.get(k) for k in cfg.HARD_REJECT_FLAGS) or pf.get("topic_switch"):
            return "history_dirty"
    except Exception as e:  # noqa: BLE001
        sig["errors"].append(f"wayback:{type(e).__name__}")
```

- [ ] **Step 5: Обновить `__main__` self-check в wayback.py**

В существующем `if __name__ == "__main__"` блоке добавить строки на бренды (перед `print`):

```python
    assert "casino" in _classify_text("Вулкан казино, азино777 бонусы")
```

- [ ] **Step 6: Прогнать + коммит**

Run: `.venv/bin/python -m pytest backend/tests/ -q` (Expected: 160 passed), `.venv/bin/python backend/app/integrations/wayback.py` (Expected: `wayback _classify_text ok`), `pyflakes` чисто.

```bash
git add backend/app/integrations/wayback.py backend/app/services/scoring.py backend/tests/test_m1_fixes.py
git commit -m "фикс(M1 I4/M10/M11): порог покрытия Wayback + сохранение сигналов + RU-казино-бренды"
```

---

### Task 6: обогащение existing-строк + видимость источников + m1_live (Important I5+I6+I7, Minor M1+M5)

`_insert` вставляет только новые → строка в БД никогда не обогащается (RD/дедлайн/lane из фида теряются при повторе). Тихое пусто источника невидимо. `m1_live.py` падает. Плюс: сентинелы фида пишутся как данные (M1); per-domain `price` из фида выбрасывается (M5).

**Files:**
- Modify: `backend/app/services/discovery.py:56-79,110-139` (обогащение + логи)
- Modify: `backend/app/services/discovery.py:30-44` (M1 сентинелы, M5 price в normalize_row)
- Modify: `backend/app/integrations/aparser.py:89-99` (fetch_html — статус в лог)
- Modify: `backend/scripts/m1_live.py:23-28,48-57` (импорты + reject_reason)
- Test: `backend/tests/test_sources.py`

**Interfaces:**
- Consumes: `Domain` (status/referring_domains/lane/acquire_deadline/feed_flags/visitors/tic/acquire_price/price).
- Produces: `run_discovery` — при повторной встрече `discovered`-строки дозаполняет NULL-поля и повышает RD; счётчики строк на источник в логах; `normalize_row` возвращает `price` и чистит сентинелы (`-1 → None`).

- [ ] **Step 1: Падающий тест на обогащение existing**

Добавить в `backend/tests/test_sources.py`:

```python
def test_existing_discovered_row_enriched_on_rediscovery(monkeypatch):
    import app.db as db
    from app.models.domain import Domain
    from app.services import discovery
    # день 1: домен без RD/lane (как из сырого источника)
    with db.SessionLocal() as s:
        s.add(Domain(domain="drop.ru", source="cctld", status="discovered",
                     referring_domains=None, lane=None)); s.commit()
    # день 2: тот же домен пришёл из backorder с RD и lane
    monkeypatch.setattr(discovery, "_collect", lambda enabled, on_progress=None: [
        {"domain": "drop.ru", "source": "backorder", "referring_domains": 42, "lane": "bid",
         "acquire_deadline": None, "visitors": None, "tic": None, "feed_flags": {}}])
    discovery.run_discovery()
    with db.SessionLocal() as s:
        d = s.execute(__import__("sqlalchemy").select(Domain)).scalars().one()
        assert d.referring_domains == 42 and d.lane == "bid"     # обогатилось, не пропущено


def test_normalize_row_sentinels_and_price():
    from app.services.discovery import normalize_row
    nr = normalize_row({"domainname": "x.ru", "links": 5, "visitors": -1, "yandex_tic": -1, "price": 190})
    assert nr["visitors"] is None and nr["tic"] is None and nr["price"] == 190.0
```

(тест обогащения зовёт `run_discovery` с патченным `_collect` — источники в сеть не идут.)

- [ ] **Step 2: Прогнать — упадут**

Run: `.venv/bin/python -m pytest backend/tests/test_sources.py -q -k "enriched or sentinels"`
Expected: FAIL (RD остаётся None; `visitors=-1`; `price` отсутствует в normalize_row).

- [ ] **Step 3: Сентинелы + price в normalize_row**

В `backend/app/services/discovery.py` в `normalize_row` заменить `return`:

```python
    def _int(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    def _pos(v):                    # сентинелы фида (-1 = «нет данных») -> None
        n = _int(v)
        return n if n is not None and n >= 0 else None

    def _price(v):
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None
    return {"domain": domain, "source": "backorder", "referring_domains": _int(row.get("links")) or 0,
            "lane": "bid", "acquire_deadline": _parse_deadline(row.get("delete_date")),
            "visitors": _pos(row.get("visitors")), "tic": _pos(row.get("yandex_tic")),
            "price": _price(row.get("price"))}
```

- [ ] **Step 4: Обогащение existing + видимость в run_discovery**

В `backend/app/services/discovery.py` заменить `_insert` и `_collect`-логи:

В `_collect` после `rows.extend(...)`/сбора каждого источника добавить счётчик (заменить тело цикла):

```python
    rows: list[dict] = []
    for name, Client in _sources().items():
        if not enabled.get(name):
            continue
        if on_progress:
            on_progress(0, 1, f"собираю: {name}")
        before = len(rows)
        try:
            if name == "backorder":
                for r in Client().list_dropping():
                    nr = normalize_row(r)
                    if nr:
                        nr["feed_flags"] = {k: bool(r.get(k)) for k in ("rkn", "judicial", "block")}
                        rows.append(nr)
            else:
                rows.extend(Client().list_dropping())
        except Exception as e:  # noqa: BLE001
            logger.warning("discovery source %s failed: %s", name, e)
            continue
        got = len(rows) - before
        logger.info("discovery source %s: %d строк", name, got)
        if got == 0:
            logger.warning("discovery source %s дал 0 строк (пусто/сломана разметка?)", name)
    return rows
```

Заменить `_insert` (обогащение discovered-строк):

```python
    def _insert(db) -> int:
        rows_by_name = {n: c for n, c in candidates.items()}
        existing = {d.domain: d for d in db.execute(
            select(Domain).where(Domain.domain.in_(rows_by_name))
        ).scalars().all()}
        fresh = [n for n in rows_by_name if n not in existing]
        # обогащение уже известных, но ещё НЕ обработанных (discovered) строк: дозаполняем
        # NULL-поля и повышаем RD. Статус/reject_reason не трогаем (re-run не откатывает).
        for name, d in existing.items():
            if d.status != "discovered":
                continue
            c = rows_by_name[name]
            new_rd = c.get("referring_domains") or 0
            if new_rd > (d.referring_domains or 0):
                d.referring_domains = new_rd
            for attr in ("lane", "acquire_deadline", "feed_flags", "visitors", "tic"):
                if getattr(d, attr, None) is None and c.get(attr) is not None:
                    setattr(d, attr, c.get(attr))
        db.add_all(Domain(
            domain=candidates[n]["domain"], source=candidates[n]["source"],
            referring_domains=candidates[n].get("referring_domains"),
            feed_flags=candidates[n].get("feed_flags"),
            lane=candidates[n].get("lane"),
            acquire_deadline=candidates[n].get("acquire_deadline"),
            visitors=candidates[n].get("visitors"), tic=candidates[n].get("tic"),
            acquire_price=(candidates[n].get("price")
                           or (__import__("app.services.pricing", fromlist=["x"]).cached_backorder_price()
                               if candidates[n].get("source") == "backorder" else None)),
        ) for n in fresh)
        db.commit()
        return len(fresh)
```

- [ ] **Step 5: fetch_html различает не-200 + починка m1_live**

В `backend/app/integrations/aparser.py` в `fetch_html` (логировать не-200 вместо тихого None):

```python
    def fetch_html(self, url: str) -> str | None:
        """Скачать страницу (Net::HTTP через прокси). HTML или None (с логом статуса)."""
        import logging
        res = self._call("oneRequest", {"query": url, "parser": "Net::HTTP",
                                        "configPreset": "default", "preset": "default"})
        head, sep, body = self._result_string(res).partition("\n\n")
        head_norm = head.replace("\r", "").lstrip()
        ok = head_norm.startswith("200") or head_norm.upper().startswith("HTTP/") and " 200" in head_norm.split("\n")[0]
        if not sep or not ok:
            logging.getLogger(__name__).warning("fetch_html %s: не-200 (%r)", url, head_norm[:80])
            return None
        return body or None
```

В `backend/scripts/m1_live.py` добавить недостающие импорты моделей (после существующих `import app.models.*`):

```python
import app.models.settings   # noqa: F401  (scoring_settings — иначе get_settings падает)
import app.models.autonomy   # noqa: F401
```

и в строке печати результата добавить `reject_reason` (в f-строку вывода домена):

```python
          f"-> {r['score']:.4f} {d.status} {d.reject_reason or ''}{err}")
```

- [ ] **Step 6: Прогнать + коммит**

Run: `.venv/bin/python -m pytest backend/tests/ -q` (Expected: 162 passed), `pyflakes` чисто, `.venv/bin/python backend/app/services/discovery.py` (self-check ok).

```bash
git add backend/app/services/discovery.py backend/app/integrations/aparser.py backend/scripts/m1_live.py backend/tests/test_sources.py
git commit -m "фикс(M1 I5/I6/I7/M1/M5): обогащение existing + видимость источников + m1_live + сентинелы/price"
```

---

### Task 7: скоринг-гарды + тестовые дыры (Minor M2,M6,M7,M8,M9,M12,M13 + §8)

Пакет мелких, но реальных гардов вокруг скоринга и настроек; каждый — свой тест.

**Files:**
- Modify: `backend/app/services/settings.py:56-68` (M7 clamp, M13 min)
- Modify: `backend/app/services/scoring.py:196,218-233,256-261` (M8 trademark, M9 gate, M12 isolation)
- Modify: `backend/app/integrations/backorder.py:49-63` (M2 инвариант фильтра)
- Modify: `CLAUDE.md` (M6 enum reject_reason)
- Test: `backend/tests/test_settings.py`, `backend/tests/test_m1_fixes.py`

**Interfaces:**
- Consumes: существующие.
- Produces: `update_settings` кламп `approve_at = max(approve_at, manual_review_at)` и нижняя граница `max_whois_per_run >= 1`; `score_domain` скорит только `discovered`/`scored`/`rejected`; `score_pending` изолирует падение одного домена.

- [ ] **Step 1: Падающие тесты**

Добавить в `backend/tests/test_settings.py`:

```python
def test_approve_clamped_above_manual():
    from app.services import settings
    out = settings.update_settings(approve_at=0.3, manual_review_at=0.8)
    assert out["approve_at"] >= out["manual_review_at"]      # инверсия не записывается


def test_max_whois_min_one():
    from app.services import settings
    assert settings.update_settings(max_whois_per_run=0)["max_whois_per_run"] >= 1
```

Добавить в `backend/tests/test_m1_fixes.py`:

```python
def test_score_only_discovered_status(monkeypatch):
    import app.db as db
    from app.models.domain import Domain
    from app.services import scoring
    with db.SessionLocal() as s:
        d = Domain(domain="live.ru", source="backorder", status="live", lane="bid")
        s.add(d); s.commit(); did = d.id
    out = scoring.score_domain(did)
    assert out.get("skipped") == "status"                   # live не рескорится
    with db.SessionLocal() as s:
        assert s.get(Domain, did).status == "live"          # статус цел


def test_score_pending_isolates_failure(monkeypatch):
    import app.db as db
    from app.models.domain import Domain
    from app.services import scoring
    with db.SessionLocal() as s:
        s.add_all([Domain(domain=f"d{i}.ru", source="backorder", status="discovered",
                          lane="bid", referring_domains=i) for i in range(3)]); s.commit()
    calls = {"n": 0}
    real = scoring.score_domain
    def _boom(did, clients=None, whois_budget=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return real(did, clients, whois_budget)
    monkeypatch.setattr(scoring, "score_domain", _boom)
    # не должно упасть, остальные 2 обработаны
    n = scoring.score_pending(limit=10)
    assert n == 3 and calls["n"] == 3
```

- [ ] **Step 2: Прогнать — упадут**

Run: `.venv/bin/python -m pytest backend/tests/test_settings.py backend/tests/test_m1_fixes.py -q -k "clamp or whois_min or only_discovered or isolates"`
Expected: FAIL по всем четырём.

- [ ] **Step 3: settings clamp + min (M7, M13)**

В `backend/app/services/settings.py` в `update_settings` после цикла записи `_KEYS_NUM` добавить:

```python
        if r.max_whois_per_run < 1:
            r.max_whois_per_run = 1                 # 0 глушил бы скоринг целиком
        if r.approve_at < r.manual_review_at:
            r.approve_at = r.manual_review_at       # инверсия порогов -> approve не ниже manual
```

(поставить перед `db.commit()`.) Также изменить нижнюю границу `max_whois_per_run` в `_BOUNDS` на `(1, 5000)`.

- [ ] **Step 4: score_domain status-gate + trademark + isolation (M8, M9, M12)**

В `backend/app/services/scoring.py`:

(M9) в начало `score_domain`, сразу после `d = db.get(Domain, domain_id)` и проверки на None:

```python
        if d.status not in ("discovered", "scored", "rejected"):
            return {"domain": d.domain, "status": d.status, "skipped": "status"}
```

(M8) после `sig: dict = {"errors": []}` добавить перенос флага из БД:

```python
        sig["trademark_risk"] = d.trademark_risk
```

(M12) в `score_pending` обернуть вызов в try/except:

```python
    for i, (did, name) in enumerate(rows, 1):
        if on_progress:
            on_progress(i - 1, total, name)
        try:
            score_domain(did, clients, whois_budget)
        except Exception:  # noqa: BLE001 — падение одного домена не топит батч (как в оркестраторе)
            logging.getLogger(__name__).exception("score_domain %s упал", name)
    if on_progress:
```

(добавить `import logging` в начало `score_pending` или в шапку модуля — проверить, что не дублируется.)

- [ ] **Step 5: backorder инвариант фильтра (M2) + CLAUDE.md enum (M6)**

В `backend/app/integrations/backorder.py` в `list_dropping` после `rows = data if isinstance(data, list) else []` добавить мягкую проверку инварианта:

```python
        bad = [r for r in rows if isinstance(r, dict) and (r.get("links") or 0) < min_links]
        if bad:
            import logging
            logging.getLogger(__name__).warning(
                "backorder: %d/%d строк с links<%d — фильтр не применился?", len(bad), len(rows), min_links)
```

В `CLAUDE.md` в строке enum `reject_reason` (M1-блок) дописать `not_acquirable`:
`(low_rd|feed_flag|too_young|rkn|blacklist|history_dirty|not_acquirable|low_score)`.

- [ ] **Step 6: Прогнать + коммит**

Run: `.venv/bin/python -m pytest backend/tests/ -q` (Expected: 168 passed), `pyflakes` чисто.

```bash
git add backend/app/services/settings.py backend/app/services/scoring.py backend/app/integrations/backorder.py CLAUDE.md backend/tests/test_settings.py backend/tests/test_m1_fixes.py
git commit -m "фикс(M1 minor): пороги-кламп + status-gate рескора + isolation + trademark + backorder-инвариант"
```

---

## Заметки по объёму и решениям

- **I2 (starvation) НЕ отдельная задача:** свёртка A-Parser `registered: 0|1` TLD-агностична, поэтому Task 1 делает `available` определяемым всегда (сетевой сбой и так raise, не None). Плюс Task 3 (deadline-aware) снимает вторую причину. Отдельный attempt-счётчик с миграцией — over-engineering, не делаем.
- **Схема БД не меняется** ни в одной задаче — все поля уже есть в `Domain`. Миграций не добавляем.
- **cctld/reg_ru/sweb парсер реестра НЕ переписываем:** живая разметка не выверена (reg_ru отдал прокси-596, cctld — навигацию). Источники выключены дефолтом (Task 3); правильный парсер таблицы dellist — box-таск после того, как отказы станут видимыми (Task 6 I6). Оставлено в §10 спека.
- **M4 (tz в _parse_deadline)** не чиним: фид отдаёт голую дату (`2026-07-08`), сдвиг не стреляет; помечено в спеке.
- **DQS-ключ Spamhaus** (`SPAMHAUS_DQS_KEY` в .env) сейчас код не использует (публичные зоны). Task 2 делает fail-closed через контроль тест-поинта; полноценная разводка DQS-резолвера — отдельный box-таск (нужен формат DQS-зон), в этом плане не трогаем.

## Self-Review

**Покрытие спека:** C1→T1, C2→T2, C3→T3, I1/I3/M3→T4, I4/M10/M11→T5, I5/I6/I7/M1/M5→T6, M2/M6/M7/M8/M9/M12/M13→T7, I2→снят T1+T3 (обосновано). §8 тестовые дыры разложены по задачам (whois-строки T1, blacklist-None T2, deadline T3, .рф T4, wayback-partial T5, enrich T6, инверсия/рескор/isolation T7). M4/DQS/cctld-парсер — осознанные скипы (задокументированы).

**Плейсхолдеры:** нет — весь код приведён.

**Согласованность типов:** `canonical_domain(raw)->str|None` (T4) используется в T4/T6; `_parse_whois_available/created` сигнатуры неизменны (T1); `is_blacklisted->bool|None` + RAISE (T2) согласован с risk-guard `_decide` (проверяет префикс `blacklist:`); ожидаемые счётчики тестов (152→154→156→158→160→162→168) — накопительно от baseline 148, каждая задача добавляет свои.
