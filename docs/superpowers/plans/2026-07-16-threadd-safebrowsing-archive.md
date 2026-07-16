# Тред D — SafeBrowsing hard-reject + Archive pre-gate — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **Пост-ревью поправка.** Archive-часть этого плана (Task 2, Steps 4/5 — `skip_wayback`
> и связанные тесты) реализована как написано, но финальное ревью нашло её нерабочей и
> бесполезной (`times="none"` парсится в `None`, не `0` — условие никогда не срабатывает;
> и даже сработай оно, `classify_history` и так мгновенно выходит на пустом архиве —
> нечего было бы экономить). Убрана целиком коммитом `b6d50d0` после этого плана.
> Source of truth — `docs/superpowers/specs/2026-07-16-threadd-safebrowsing-archive-design.md`
> (поправка в самом верху файла). SafeBrowsing (Task 2, Steps 3/5/6) — реализован как
> написано, ревью одобрило полностью, не тронут.

**Goal:** Add two live-verified cheap A-Parser signals to the M1 scoring funnel —
`SE::Google::SafeBrowsing` as a hard-reject (like RKN/blacklist), and `Rank::Archive`
(preset `no_proxy`) as a pre-check that skips the expensive real Wayback fetch when it
confirms zero snapshots, WITHOUT changing what "zero history" means for the score.

**Architecture:** Two new transport methods on the existing `AParserClient`
(`backend/app/integrations/aparser.py`), consumed by `scoring._funnel` at the same
points RKN/blacklist/Wayback already live. No new DB columns, no new `/settings`
toggle, no new `FUNNEL_STAGES` entry — both checks live inside the existing `risk` and
`history` stages.

**Tech Stack:** Python 3.12, SQLAlchemy 2.x, pytest (hermetic — the `_no_live_network`
autouse fixture blocks real network; all A-Parser interaction in tests is mocked via
fake client objects, matching the existing `test_funnel.py`/`test_aparser.py` style).

## Global Constraints

- Design source of truth: `docs/superpowers/specs/2026-07-16-threadd-safebrowsing-archive-design.md`
  — read it before Task 2, it documents WHY (audit F2 invariant: empty history must
  stay `wayback_checked=False` / `unknown`, never `clean`).
- `Rank::Archive` MUST be called with `preset: "no_proxy"` literally — the `default`
  preset on the live A-Parser box is proxy-broken (archive.org 502s through the shared
  proxy pool). This is a hardcoded literal in the client method, not a setting.
- Never add "safebrowsing"/"archive" to `cfg.WEIGHTS` or `compute_score`'s `comp` dict
  — `history_cleanliness` stays exactly `1.0 if wayback_checked else 0.5`, untouched.
- Every new failure path (`except Exception`) must append to `sig["errors"]` with the
  `"<name>:<ExceptionType>"` format already used by `rkn:`/`blacklist:`/`wayback:` —
  this is what `_decide`'s risk-guard and `blind_reason`/`_BLIND_RU` key off of.
- Hard gates untouched: this plan touches ONLY M1 scoring (`Domain.status` transitions
  `discovered→scored/approved/rejected`). Nothing here calls `confirm_order`,
  `execute_confirmed_order`, or `mark_edited`.

---

## File Structure

| File | Change |
|---|---|
| `backend/app/integrations/aparser.py` | Add `_RE_SAFEBROWSING`, `_RE_ARCHIVE` regexes; `_parse_safebrowsing`, `_parse_archive` module functions; `AParserClient.safebrowsing_check`, `AParserClient.archive_probe` methods |
| `backend/tests/test_aparser.py` | Unit tests for the two parsers + two probe methods |
| `backend/app/services/scoring.py` | `_BLIND_RU` +1 entry; `_decide` risk-guard tuple +1 prefix; `_funnel` — safebrowsing check in `risk` stage, archive pre-check in `history` stage |
| `backend/app/services/labels.py` | `REJECT_RU["safebrowsing"]` + self-check assert |
| `backend/app/services/transitions.py` | `DIRTY_REASONS` +1 entry |
| `backend/tests/test_funnel.py` | Extend `_clients()`/`_clients_whois_raises()` fixtures with `safebrowsing`/`archive_times` params; new tests for hard-reject, pass-through, error/blind-guard, skip-wayback, has-snapshots, archive-error-fallback |
| `backend/tests/test_transitions.py` | +1 assert in `test_dirty_reason_sees_verdict_and_raw_signals` |

---

### Task 1: A-Parser client — SafeBrowsing + Archive transport methods

**Files:**
- Modify: `backend/app/integrations/aparser.py:24-29` (insert new regexes after `_RE_AHREFS`, before `def _parse_ahrefs`)
- Modify: `backend/app/integrations/aparser.py:185-206` (insert two new methods on `AParserClient`, after `ahrefs_probe`, before the closing of the class / `if __name__` block)
- Test: `backend/tests/test_aparser.py`

**Interfaces:**
- Produces: `AParserClient.safebrowsing_check(domain: str) -> bool | None` and
  `AParserClient.archive_probe(domain: str) -> dict` (keys `times: int|None`,
  `first: str|None`, `last: str|None`) — Task 2 calls both as `c["aparser"].<method>(d.domain)`.

- [ ] **Step 1: Write the failing parser unit tests**

Add to `backend/tests/test_aparser.py` (same file that already imports `_parse_ahrefs, AParserClient` — extend that import line):

```python
from app.integrations.aparser import (
    _parse_ahrefs, _parse_safebrowsing, _parse_archive, AParserClient,
)


def test_parse_safebrowsing_flagged():
    assert _parse_safebrowsing("zudpopo.ru: 1\n") is True


def test_parse_safebrowsing_clean():
    assert _parse_safebrowsing("dswjcndwijnwld23234212djf.ru: 0\n") is False


def test_parse_safebrowsing_no_match_returns_none():
    assert _parse_safebrowsing("garbage response") is None


def test_parse_safebrowsing_empty_string():
    assert _parse_safebrowsing("") is None


def test_parse_archive_with_history():
    out = _parse_archive("google.com: 11.11.1998 - 16.07.2026 (19936104 times)\n")
    assert out == {"times": 19936104, "first": "11.11.1998", "last": "16.07.2026"}


def test_parse_archive_none_history():
    out = _parse_archive("dswjcndwijnwld23234212djf.ru: none - none (none times)\n")
    assert out == {"times": None, "first": None, "last": None}


def test_parse_archive_no_match_returns_all_none():
    out = _parse_archive("garbage response, not the expected format")
    assert out == {"times": None, "first": None, "last": None}


def test_parse_archive_empty_string():
    out = _parse_archive("")
    assert out == {"times": None, "first": None, "last": None}


def test_safebrowsing_check_sends_expected_parser(monkeypatch):
    seen = {}

    def fake_call(self, action, data):
        seen["action"], seen["data"] = action, data
        return {"success": 1, "data": {"resultString": "example.com: 0\n"}}

    monkeypatch.setattr(AParserClient, "_call", fake_call)
    c = AParserClient()
    assert c.safebrowsing_check("example.com") is False
    assert seen["data"]["parser"] == "SE::Google::SafeBrowsing"
    assert seen["data"]["query"] == "example.com"


def test_archive_probe_uses_no_proxy_preset(monkeypatch):
    seen = {}

    def fake_call(self, action, data):
        seen["data"] = data
        return {"success": 1, "data": {"resultString": "example.com: none - none (none times)\n"}}

    monkeypatch.setattr(AParserClient, "_call", fake_call)
    c = AParserClient()
    out = c.archive_probe("example.com")
    assert out["times"] is None
    assert seen["data"]["parser"] == "Rank::Archive"
    assert seen["data"]["preset"] == "no_proxy"          # НЕ "default" — тот сломан через прокси
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `docker compose run --rm backend pytest backend/tests/test_aparser.py -v -k "safebrowsing or archive"`
Expected: FAIL — `ImportError: cannot import name '_parse_safebrowsing'` (function doesn't exist yet).

- [ ] **Step 3: Implement the regexes and parsers**

In `backend/app/integrations/aparser.py`, insert right after the `_RE_AHREFS = re.compile(...)` block (currently ends at line 27, right before `def _parse_ahrefs`):

```python
# SE::Google::SafeBrowsing resultString: '<domain>: 0|1\n' (1 = Google-флаг вредонос/фишинг),
# живьём проверено 2026-07-16 на обоих тестовых доменах (оба вернули 0).
_RE_SAFEBROWSING = re.compile(r":\s*([01])\s*$")

# Rank::Archive resultString (preset no_proxy — default сломан через прокси, см. дизайн-документ):
# '<domain>: <first|none> - <last|none> (<times|none> times)\n', даты dd.mm.yyyy.
# Живьём проверено 2026-07-16: google.com (19936104), wikipedia.org (393880),
# yandex.ru (506844), zudpopo.ru (19, 2023-2026).
_RE_ARCHIVE = re.compile(
    r":\s*(?P<first>none|\d{2}\.\d{2}\.\d{4})\s*-\s*(?P<last>none|\d{2}\.\d{2}\.\d{4})"
    r"\s*\((?P<times>none|\d+)\s*times\)",
    re.I,
)


def _parse_safebrowsing(text: str) -> bool | None:
    """True = зафлагован Google, False = чист, None = формат не распознан (вызывающий
    код трактует как «не проверено», НЕ как «чисто» — см. scoring._funnel)."""
    m = _RE_SAFEBROWSING.search(text or "")
    return None if not m else m.group(1) == "1"


def _parse_archive(text: str) -> dict:
    """times=0 -> вызывающий код вправе пропустить дорогой Wayback-фетч; times=None ->
    формат не распознан/сбой, фолбэк на реальный Wayback как раньше."""
    m = _RE_ARCHIVE.search(text or "")
    if not m:
        return {"times": None, "first": None, "last": None}
    times = m.group("times")
    return {
        "times": None if times.lower() == "none" else int(times),
        "first": None if m.group("first").lower() == "none" else m.group("first"),
        "last": None if m.group("last").lower() == "none" else m.group("last"),
    }
```

Then, inside `class AParserClient`, right after the existing `ahrefs_probe` method (currently lines 185-206, immediately before the `if __name__ == "__main__":` block at line 209):

```python
    def safebrowsing_check(self, domain: str) -> bool | None:
        """SE::Google::SafeBrowsing — не SERP-скрейпинг, прямой lookup, recaptcha не
        задевает (в отличие от TrustCheck/Compromised, живьём проверено 2026-07-16)."""
        res = self._call("oneRequest", {
            "query": domain, "parser": "SE::Google::SafeBrowsing",
            "configPreset": "default", "preset": "default",
        })
        return _parse_safebrowsing(self._result_string(res))

    def archive_probe(self, domain: str) -> dict:
        """Rank::Archive, ОБЯЗАТЕЛЬНО preset=no_proxy — default бьётся в archive.org
        через прокси-пул и получает 502 на каждую попытку (живьём подтверждено
        2026-07-16: getParserPreset показал useproxy=1 у default, логи oneRequest —
        502 Bad Gateway на всех прокси). no_proxy живьём подтверждён рабочим."""
        res = self._call("oneRequest", {
            "query": domain, "parser": "Rank::Archive",
            "configPreset": "default", "preset": "no_proxy",
        })
        return _parse_archive(self._result_string(res))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `docker compose run --rm backend pytest backend/tests/test_aparser.py -v -k "safebrowsing or archive"`
Expected: PASS (10 new tests).

- [ ] **Step 5: Run full aparser test file + pyflakes**

Run: `docker compose run --rm backend pytest backend/tests/test_aparser.py -v`
Expected: all PASS (existing ahrefs/whois tests untouched).
Run: `.venv/bin/python -m pyflakes backend/app/integrations/aparser.py`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add backend/app/integrations/aparser.py backend/tests/test_aparser.py
git commit -m "feat(M1): SafeBrowsing + Archive(no_proxy) A-Parser transport methods

Живьём проверенные форматы (Тред D, 2026-07-16). Archive ОБЯЗАН звать preset
no_proxy — default сломан через прокси-пул (502 на archive.org)."
```

---

### Task 2: Wire into the scoring funnel — hard-reject + Wayback pre-gate

**Files:**
- Modify: `backend/app/services/scoring.py:58-70` (`_BLIND_RU` — add `safebrowsing` entry)
- Modify: `backend/app/services/scoring.py:220-224` (`_decide` risk-guard — add `"safebrowsing:"` prefix)
- Modify: `backend/app/services/scoring.py:468-522` (`_funnel` — safebrowsing check in `risk` stage; archive pre-check wrapping the `history` stage)
- Modify: `backend/app/services/labels.py:26-30` (`REJECT_RU["safebrowsing"]`)
- Modify: `backend/app/services/transitions.py:53` (`DIRTY_REASONS`)
- Test: `backend/tests/test_funnel.py` (fixture extension + new tests)
- Test: `backend/tests/test_transitions.py:51` (one new assert line)

**Interfaces:**
- Consumes: `AParserClient.safebrowsing_check(domain) -> bool | None` and
  `AParserClient.archive_probe(domain) -> dict` from Task 1 — called as
  `c["aparser"].safebrowsing_check(d.domain)` / `c["aparser"].archive_probe(d.domain)`,
  matching how `_funnel` already calls `c["aparser"].whois_probe`/`ahrefs_probe`.
- Produces: `Domain.reject_reason == "safebrowsing"` (new hard-reject value, flows
  through `dirty_reason()`/`bulk_ok()`/`acquisition.py` unchanged, no new code needed
  there — they already key off `reject_reason`/`DIRTY_REASONS`).

- [ ] **Step 1: Write the failing funnel tests**

First, extend the two fixture builders in `backend/tests/test_funnel.py` (current
signatures at the top of the file) to accept two new optional params. Replace the
current `_clients` and `_clients_whois_raises` functions with:

```python
def _clients(whois_dt=None, wayback=None, rkn=False, bl=False, indexed_echo=True,
             whois=None, whois_raises=False, safebrowsing=False, archive_times=None):
    """whois: dict {"available":..., "created":...} (новый формат, приобретаемость известна
    явно). whois_dt: старый позиционный аргумент (только дата) — оборачивается в
    {"available": False, "created": whois_dt} (занят, но с датой регистрации — для тестов,
    доходящих до T2/T3 через lane="bid" на тестовом Domain). whois_raises=True — whois_probe
    бросает (недоступен). safebrowsing: True = зафлагован, False = чист, None = падает
    (исключение). archive_times: None = Archive не смог определить (совпадает с сегодняшним
    поведением — Wayback всё равно запускается), 0 = архив честно пуст (Wayback пропускается),
    >0 = есть снимки (Wayback запускается)."""
    pr = whois if whois is not None else {"available": False, "created": whois_dt}
    class _W:  # aparser
        def whois_probe(self, dom):
            if whois_raises:
                raise RuntimeError("whois timeout")
            return pr
        def safebrowsing_check(self, dom):
            if safebrowsing is None:
                raise RuntimeError("safebrowsing timeout")
            return safebrowsing
        def archive_probe(self, dom):
            return {"times": archive_times, "first": None, "last": None}
    class _R:
        def is_listed(self, dom): return rkn
    class _B:
        def is_blacklisted(self, dom): return bl
    class _S:
        def indexed_echo(self, dom): return indexed_echo
    return {"aparser": _W(), "rkn": _R(), "blacklist": _B(), "searxng": _S(),
            "wayback": wayback}


def _clients_whois_raises(wb, rkn=False, bl=False, indexed_echo=True,
                          safebrowsing=False, archive_times=None):
    """Как _clients, но whois_probe падает (недоступен) — для Finding-1 фолбэка."""
    class _W:  # aparser
        def whois_probe(self, dom): raise RuntimeError("whois timeout")
        def safebrowsing_check(self, dom):
            if safebrowsing is None:
                raise RuntimeError("safebrowsing timeout")
            return safebrowsing
        def archive_probe(self, dom):
            return {"times": archive_times, "first": None, "last": None}
    class _R:
        def is_listed(self, dom): return rkn
    class _B:
        def is_blacklisted(self, dom): return bl
    class _S:
        def indexed_echo(self, dom): return indexed_echo
    return {"aparser": _W(), "rkn": _R(), "blacklist": _B(), "searxng": _S(),
            "wayback": wb}
```

(Default `safebrowsing=False, archive_times=None` — every one of the ~31 existing
`_clients(...)` call sites in this file keeps its exact current behavior: SafeBrowsing
never rejects, Archive never skips Wayback, `wb.calls` assertions are unaffected.)

Now add the new tests (append to `backend/tests/test_funnel.py`; use whatever helper
this file already uses to invoke `_funnel` and read `reject_reason`/`sig` — follow the
exact call pattern of the nearest existing rkn/blacklist test in this file for the
harness plumbing, e.g. `SimpleNamespace`/`Domain` id + `scoring._funnel(...)`):

```python
def test_safebrowsing_flagged_hard_rejects():
    wb = _Wayback()
    did = _mk(domain="bad.ru")
    with db.SessionLocal() as s:
        d = s.get(Domain, did)
        reject = scoring._funnel(d, _clients(safebrowsing=True, wayback=wb), s,
                                  {"min_age_years": 0}, {"errors": []})
    assert reject == "safebrowsing"
    assert wb.calls == 0        # отсеян ДО Wayback — как rkn/blacklist


def test_safebrowsing_clean_proceeds_to_wayback():
    wb = _Wayback()
    did = _mk(domain="clean.ru")
    with db.SessionLocal() as s:
        d = s.get(Domain, did)
        reject = scoring._funnel(d, _clients(safebrowsing=False, wayback=wb), s,
                                  {"min_age_years": 0}, {"errors": []})
    assert reject is None
    assert wb.calls == 1


def test_safebrowsing_error_does_not_reject_and_is_logged():
    wb = _Wayback()
    did = _mk(domain="unknown-sb.ru")
    with db.SessionLocal() as s:
        d = s.get(Domain, did)
        sig = {"errors": []}
        reject = scoring._funnel(d, _clients(safebrowsing=None, wayback=wb), s,
                                  {"min_age_years": 0}, sig)
    assert reject is None
    assert any(e.startswith("safebrowsing:") for e in sig["errors"])


def test_archive_confirms_empty_skips_wayback():
    wb = _Wayback()
    did = _mk(domain="never-archived.ru")
    with db.SessionLocal() as s:
        d = s.get(Domain, did)
        sig = {"errors": []}
        reject = scoring._funnel(d, _clients(archive_times=0, wayback=wb), s,
                                  {"min_age_years": 0}, sig)
    assert reject is None
    assert wb.calls == 0                    # дорогой фетч не звался
    assert sig["wayback_checked"] is False  # ИДЕНТИЧНО честно-пустому Wayback сегодня
    assert sig["sampled"] == 0
    assert sig["history_evidence"] == []


def test_archive_has_snapshots_still_runs_wayback():
    wb = _Wayback()
    did = _mk(domain="has-history.ru")
    with db.SessionLocal() as s:
        d = s.get(Domain, did)
        reject = scoring._funnel(d, _clients(archive_times=19, wayback=wb), s,
                                  {"min_age_years": 0}, {"errors": []})
    assert reject is None
    assert wb.calls == 1                   # архив не заменяет Wayback, только решает, звать ли его


def test_archive_error_falls_through_to_wayback():
    """archive_times=None имитирует и «формат не распознан», и реальную ошибку —
    в обоих случаях _funnel не имеет права угадывать, фолбэк на настоящий Wayback."""
    wb = _Wayback()
    did = _mk(domain="archive-unclear.ru")
    with db.SessionLocal() as s:
        d = s.get(Domain, did)
        reject = scoring._funnel(d, _clients(archive_times=None, wayback=wb), s,
                                  {"min_age_years": 0}, {"errors": []})
    assert reject is None
    assert wb.calls == 1
```

**Note for implementer:** read the nearest existing test in `test_funnel.py` calling
`scoring._funnel(...)` directly to confirm the exact positional/keyword argument order
this file's harness uses (the signature shown above — `(d, clients, db, settings_dict,
sig)` — is illustrative of the shape; match whatever this file's existing calls
actually pass, do not guess if it differs).

- [ ] **Step 2: Run tests to verify they fail**

Run: `docker compose run --rm backend pytest backend/tests/test_funnel.py -v -k "safebrowsing or archive"`
Expected: FAIL — `TypeError`/`AttributeError` (fixture doesn't yet accept the new
params / `_funnel` doesn't yet call the new methods).

- [ ] **Step 3: Wire SafeBrowsing into the `risk` stage**

In `backend/app/services/scoring.py`, right after the existing blacklist block
(current lines 476-483, ending at the `sig["errors"].append("blacklist:unavailable")`
line) and before `jobs.report(run, stage="echo")` (line 485), insert:

```python
    try:
        sig["safebrowsing_flagged"] = c["aparser"].safebrowsing_check(d.domain)
        if sig["safebrowsing_flagged"] is True:
            return "safebrowsing"
    except Exception as e:  # noqa: BLE001
        sig["errors"].append(f"safebrowsing:{type(e).__name__}")
    if sig.get("safebrowsing_flagged") is None and "safebrowsing_flagged" in sig:
        sig["errors"].append("safebrowsing:unavailable")  # формат не распознан -> risk-guard
```

- [ ] **Step 4: Wire the Archive pre-gate into the `history` stage**

Still in `scoring.py`, the current block (lines 492-516) reads:

```python
    jobs.report(run, stage="history")
    # T3 — история (дорого): только для приобретаемых выживших
    try:
        hist = c["wayback"].classify_history(d.domain)
        pf = hist.get("prior_flags") or {}
        sig["prior_flags"] = pf
        sig["wayback_checked"] = hist.get("wayback_checked")     # сохраняем ДО возможного выхода
        # чем именно подтверждён вердикт истории — снимки, которые реально смотрели. Кладём
        # ДО возможного выхода в history_dirty: отказ — тоже вердикт, и он тоже ошибается.
        sig["history_evidence"] = hist.get("evidence") or []
        # сколько снимков РЕАЛЬНО скачали в ЭТОМ прогоне — 0 отличает «архив честно пуст» от
        # «этот прогон вообще не оставил числа» (см. blind_reason: домены, отскоренные до
        # появления этого поля, не имеют права носить утверждение о пустом архиве).
        sig["sampled"] = hist.get("sampled")
        sig["first_seen"] = hist.get("first_seen")
        if sig.get("whois_created") is None and hist.get("age_years") is not None:
            sig["age_years"] = hist["age_years"]           # whois приоритетнее; Wayback — фолбэк
            # ...и бейдж обязан сказать об этом ПРАВДУ: гейт молодости ПРИМЕНЁН (ниже, по этому
            # самому числу), а вот занятость домена не сверял никто. Прежний текст «гейт не
            # применялся» был ложью ровно в том состоянии, где показывался (ревью Задачи 4).
            sig["age_source"] = "wayback"
        if any(pf.get(k) for k in cfg.HARD_REJECT_FLAGS):
            return "history_dirty"
    except Exception as e:  # noqa: BLE001
        sig["errors"].append(f"wayback:{type(e).__name__}")
```

Replace it with:

```python
    jobs.report(run, stage="history")
    # Archive — не сигнал чистоты, только решение «стоит ли звать дорогой Wayback-фетч».
    # times=0 подтверждено -> Wayback пропускается, но wayback_checked всё равно False
    # (см. дизайн-документ: это ДОЛЖНО вести себя ИДЕНТИЧНО честно-пустому Wayback).
    skip_wayback = False
    try:
        arch = c["aparser"].archive_probe(d.domain)
        sig["archive_times"] = arch.get("times")
        skip_wayback = arch.get("times") == 0
    except Exception as e:  # noqa: BLE001
        sig["errors"].append(f"archive:{type(e).__name__}")

    if skip_wayback:
        sig["wayback_checked"] = False
        sig["sampled"] = 0
        sig["history_evidence"] = []
    else:
        # T3 — история (дорого): только для приобретаемых выживших
        try:
            hist = c["wayback"].classify_history(d.domain)
            pf = hist.get("prior_flags") or {}
            sig["prior_flags"] = pf
            sig["wayback_checked"] = hist.get("wayback_checked")     # сохраняем ДО возможного выхода
            # чем именно подтверждён вердикт истории — снимки, которые реально смотрели. Кладём
            # ДО возможного выхода в history_dirty: отказ — тоже вердикт, и он тоже ошибается.
            sig["history_evidence"] = hist.get("evidence") or []
            # сколько снимков РЕАЛЬНО скачали в ЭТОМ прогоне — 0 отличает «архив честно пуст» от
            # «этот прогон вообще не оставил числа» (см. blind_reason: домены, отскоренные до
            # появления этого поля, не имеют права носить утверждение о пустом архиве).
            sig["sampled"] = hist.get("sampled")
            sig["first_seen"] = hist.get("first_seen")
            if sig.get("whois_created") is None and hist.get("age_years") is not None:
                sig["age_years"] = hist["age_years"]           # whois приоритетнее; Wayback — фолбэк
                # ...и бейдж обязан сказать об этом ПРАВДУ: гейт молодости ПРИМЕНЁН (ниже, по этому
                # самому числу), а вот занятость домена не сверял никто. Прежний текст «гейт не
                # применялся» был ложью ровно в том состоянии, где показывался (ревью Задачи 4).
                sig["age_source"] = "wayback"
            if any(pf.get(k) for k in cfg.HARD_REJECT_FLAGS):
                return "history_dirty"
        except Exception as e:  # noqa: BLE001
            sig["errors"].append(f"wayback:{type(e).__name__}")
```

- [ ] **Step 5: Extend the risk-guard and blind-reason dictionaries**

In `scoring.py`, `_BLIND_RU` (currently lines 58-70), add one entry right after
`"blacklist": "блэклист НЕ проверен",` (line 68):

```python
    "safebrowsing": "Google Safe Browsing НЕ проверен: сервис не ответил",
```

In `_decide()` (currently lines 220-224):

```python
    # risk-guard: если проверка RKN, blacklist или SafeBrowsing упала (ключ сигнала
    # отсутствует, ошибка осела в errors), нельзя подтверждать чистоту автоматом —
    # уводим в ручной `scored`.
    if status == "approved" and any(
            e.startswith(("rkn:", "blacklist:", "safebrowsing:"))
            for e in (sig.get("errors") or [])):
        status = "scored"
```

- [ ] **Step 6: Add the label and the dirty-reason gate**

In `backend/app/services/labels.py`, `REJECT_RU` (currently lines 26-30):

```python
REJECT_RU = {
    "low_rd": "мало доноров", "feed_flag": "флаг источника", "too_young": "моложе порога",
    "rkn": "реестр РКН", "blacklist": "блэклист", "history_dirty": "грязная история",
    "low_score": "низкий скор", "not_acquirable": "нельзя купить",
    "safebrowsing": "Google Safe Browsing",
}
```

Add to the file's existing `if __name__ == "__main__":` self-check block (near the
existing `assert reject_ru("not_acquirable") == "нельзя купить"` line):

```python
    assert reject_ru("safebrowsing") == "Google Safe Browsing"
```

In `backend/app/services/transitions.py:53`:

```python
DIRTY_REASONS = frozenset({"rkn", "blacklist", "history_dirty", "feed_flag", "safebrowsing"})
```

In `backend/tests/test_transitions.py`, in `test_dirty_reason_sees_verdict_and_raw_signals`
(current assertions around line 51-56), add:

```python
    assert dirty_reason(d(reject_reason="safebrowsing")) == "safebrowsing"
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `docker compose run --rm backend pytest backend/tests/test_funnel.py backend/tests/test_transitions.py backend/tests/test_scoring_weights.py -v`
Expected: all PASS (new tests + no regressions on the ~31 existing `_clients(...)` call
sites in `test_funnel.py`).

- [ ] **Step 8: Run the full test suite + pyflakes**

Run: `docker compose run --rm backend pytest backend/tests/ -q`
Expected: all green, no regressions anywhere (M2/M3/M4/M5 tests untouched by this
change, but a full run is cheap insurance — this touches a shared `_funnel` function).
Run: `.venv/bin/python -m pyflakes backend/app backend/tests`
Expected: clean.

- [ ] **Step 9: Commit**

```bash
git add backend/app/services/scoring.py backend/app/services/labels.py \
        backend/app/services/transitions.py backend/tests/test_funnel.py \
        backend/tests/test_transitions.py
git commit -m "feat(M1): wire SafeBrowsing hard-reject + Archive pre-gate into scoring funnel

SafeBrowsing hard-rejects like RKN/blacklist (reject_reason=safebrowsing, DIRTY_REASONS,
risk-guard, оценён-вслепую бейдж — тот же контракт). Archive(no_proxy) экономит дорогой
Wayback-фетч, когда честно подтвердил 0 снимков — wayback_checked остаётся False,
вердикт истории остаётся 'unknown', скор не меняется (Тред D, 2026-07-16)."
```

---

## Self-Review Notes (completed during plan authoring)

- **Spec coverage:** SafeBrowsing hard-reject ✓ (Task 2 Steps 3/5/6). Archive pre-gate
  with `no_proxy` preset ✓ (Task 1 Step 3, Task 2 Step 4). Risk-guard/blind-badge
  consistency for SafeBrowsing failures ✓ (Task 2 Step 5 — this was the gap the design
  doc's own self-review caught before this plan was written). No `/settings` toggle, no
  weight change, no new `FUNNEL_STAGES` entry — confirmed absent from every task above.
- **Placeholder scan:** no TBD/TODO; the one deliberately-underspecified spot (Task 2
  Step 1's "match whatever this file's existing calls actually pass") is flagged
  in-line for the implementer to resolve by reading the file, not a placeholder for
  missing requirements — the test bodies themselves are complete.
- **Type consistency:** `archive_probe` returns `{"times": int|None, "first": str|None,
  "last": str|None}` consistently across Task 1's implementation, its tests, and Task
  2's consumption (`arch.get("times")`). `safebrowsing_check` returns `bool | None`
  consistently everywhere it's referenced.

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-16-threadd-safebrowsing-archive.md`.
Proceeding with **Subagent-Driven Development** per this session's established pattern
(fresh implementer per task, task-reviewer after each, final whole-branch review) —
no merge/push without explicit sign-off (branch left ready for morning review).
