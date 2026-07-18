# Тред D, продолжение — indexed_echo фолбэк через A-Parser + закрытие SecurityTrails/Yandex — план реализации

> **Для агентов-исполнителей:** ОБЯЗАТЕЛЬНЫЙ САБ-СКИЛЛ — superpowers:subagent-driven-development
> (рекомендовано) или superpowers:executing-plans, задача за задачей. Чекбоксы (`- [ ]`) — трекинг.

**Цель:** закрыть оставшиеся два пункта Треда D (`SecurityTrails`, `SERP-fallback через
SE::Google/Yandex`) — либо реализовать (если живая проба подтвердит надёжность), либо
честно задокументировать как исследованные и отклонённые (по прецеденту `OpenPageRank`).

**Архитектура:** живая read-only проба A-Parser-форматов (Task 1, мирроит уже проверенный
жанр `aparser_probe_threadd.py`) → решение по её РЕАЛЬНОМУ результату определяет одну из
двух полностью прописанных веток Task 2 (вписать фолбэк ИЛИ не писать код вовсе) → doc-
closure в CLAUDE.md (Task 3), фиксирующая исход для обоих оставшихся кандидатов.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.x, pytest (оффлайн SQLite-харнесс,
autouse `_no_live_network`), A-Parser HTTP API (`:9091`).

## Global Constraints

- Хард-гейты (деньги/редактура) этой работой не затрагиваются вовсе (M1-скоринг, не
  M2/M4/M5) — тем не менее ни один шаг не должен их касаться даже случайно.
- **Живые A-Parser-форматы — только по факту пробы**, никогда не угадывать (CLAUDE.md,
  дважды дорого стоившее правило: whois-возраст, дата дропа cctld). Task 2 реализуется
  СТРОГО по тому, что реально напечатала проба Task 1, а не по ожиданию.
- `indexed_echo` — мягкий сигнал скоринга (вес в `compute_score`), НЕ risk-guard и НЕ
  history-blind-гейт. Успешный фолбэк не должен порождать запись в `sig["errors"]` с
  префиксом `searxng:` — иначе панель солжёт «эхо НЕ проверено» поверх реально
  полученного ответа (см. дизайн, п.3; `_BLIND_RU["searxng"]`,
  `backend/app/services/scoring.py:70`).
- Никакого нового runtime-бюджета/тумблера под фолбэк (в отличие от Ahrefs) — он
  бесплатный и срабатывает только при отказе обычно стабильного SearXNG.
- Тесты воронки гоняют через `scoring.score_domain(did, clients=...)`, НЕ напрямую
  `_funnel` (реальная конвенция `test_funnel.py`, а не иллюстративная).
- pyflakes чист (`backend/app`, `backend/tests`), полный сьют зелёный
  (`.venv/bin/python -m pytest backend/tests/ -q` либо `docker compose run --rm backend
  pytest backend/tests/ -q`), русский язык везде (комментарии, коммиты, доки).
- Коммиты: `git commit -F -` с heredoc, трейлер `Co-Authored-By: Claude Opus 4.8
  <noreply@anthropic.com>`.
- **Работа идёт прямо в `main`** (сознательно, см. финальную Task 4): значит КАЖДЫЙ
  коммит обязан оставлять сьют зелёным — нет ветки, которая прикрыла бы промежуточную
  красноту.
- **Перед Task 1** контроллер записывает базовый коммит:
  `BASE=$(git rev-parse HEAD)` — в леджер (`.superpowers/sdd/progress.md`). Финальное
  ревью (Task 4) судит диапазон `BASE..HEAD`; `HEAD~N` не использовать — задачи
  многокоммитные.

---

### Task 1: живая проба — `site:` через SE::Google + диагностика SecurityTrails/Yandex

**Files:**
- Modify: `backend/scripts/aparser_probe_threadd.py`

**Interfaces:**
- Потребляет: `AParserClient._call(action, data)` (уже существует,
  `backend/app/integrations/aparser.py:123`) — тот же приватный вызов, что уже
  использует этот скрипт для существующих `CANDIDATES`.
- Производит: печатает сырые JSON-конверты в stdout. Ничего в БД, ничего в воронку —
  read-only network probe. Результат (успех/отказ по каждому кандидату) идёт в отчёт
  задачи текстом — Task 2 читает его оттуда, не гадает.

- [ ] **Step 1: добавить блок пробы «Тред D, продолжение» в конец скрипта**

Дописать в `backend/scripts/aparser_probe_threadd.py` (после существующих `CANDIDATES`/
`probe`/`main`, ничего в них не меняя):

```python
# --- Тред D, продолжение (2026-07-17): indexed_echo фолбэк + закрытие ---------------
# SecurityTrails/SE::Yandex. См. docs/superpowers/specs/
# 2026-07-17-threadd-serp-fallback-design.md. Тоже read-only, тоже безопасно гонять
# сколько угодно (SE::Google/Yandex — через прокси, та же вежливость, что у остальных
# SERP-вызовов в этом файле).

# Заведомо не индексирован (мусорный тестовый домен) / реальный живой домен / заведомо
# индексированный контрольный домен — тот же принцип трёх точек, что у SafeBrowsing/
# Archive проб (см. CANDIDATES выше).
SITE_QUERY_DOMAINS = ["dswjcndwijnwld23234212djf.ru", "zudpopo.ru", "wikipedia.org"]


def probe_site_query(client: AParserClient, parser: str, preset: str, domain: str) -> None:
    query = f"site:{domain}"
    try:
        res = client._call("oneRequest", {"query": query, "parser": parser,
                                          "configPreset": "default", "preset": preset})
        print(f"  [{parser} preset={preset}] query={query!r}")
        print(f"    raw: {res!r}")
    except Exception as e:  # noqa: BLE001 — диагностика, показываем любой сбой как есть
        print(f"  [{parser} preset={preset}] query={query!r} -> ОШИБКА: {type(e).__name__}: {e}")


def probe_preset(client: AParserClient, parser: str, preset: str) -> None:
    try:
        res = client._call("getParserPreset", {"parser": parser, "preset": preset})
        print(f"  [getParserPreset {parser}/{preset}]")
        print(f"    raw: {res!r}")
    except Exception as e:  # noqa: BLE001
        print(f"  [getParserPreset {parser}/{preset}] -> ОШИБКА: {type(e).__name__}: {e}")


def probe_serp_urls_prod(client: AParserClient, domain: str) -> None:
    """Тот же ПРОДОВЫЙ путь, что вызовет ветка А: публичный serp_urls() с его реальным
    парсингом (строки resultString, начинающиеся с 'http'). Сырой конверт из
    probe_site_query показывает транспорт; этот вызов показывает, что увидит scoring.py —
    без зазора на интерпретацию «а распарсится ли»."""
    try:
        urls = client.serp_urls(f"site:{domain}", limit=5)
        print(f"  [serp_urls] site:{domain} -> {len(urls)} URL: {urls!r}")
    except Exception as e:  # noqa: BLE001
        print(f"  [serp_urls] site:{domain} -> ОШИБКА: {type(e).__name__}: {e}")


def probe_continuation() -> None:
    client = AParserClient()

    print("=== SE::Google site: (кандидат в фолбэк для indexed_echo) ===")
    # 2 прохода: в пробе 2026-07-16 TrustCheck/Compromised давали 0–50% успеха —
    # ОДИН сэмпл на домен не отличает «надёжно» от «через раз». 6 сэмплов отличают.
    for attempt in (1, 2):
        print(f"--- попытка {attempt} (сырой конверт) ---")
        for d in SITE_QUERY_DOMAINS:
            probe_site_query(client, "SE::Google", "default", d)
    print("--- продовый путь: serp_urls(), парсинг как в ветке А ---")
    for d in SITE_QUERY_DOMAINS:
        probe_serp_urls_prod(client, d)

    print()
    print("=== SecurityTrails::Domain — конфиг пресета: нужен свой API-ключ и задан ли он? ===")
    probe_preset(client, "SecurityTrails::Domain", "default")

    print()
    print("=== SE::Yandex — default сначала (ожидаем повтор ReadTimeout из пробы 2026-07-16) ===")
    for d in SITE_QUERY_DOMAINS[:2]:
        probe_site_query(client, "SE::Yandex", "default", d)
    print("--- пробуем непрокси-пресет по аналогии с Rank::Archive/no_proxy ---")
    probe_preset(client, "SE::Yandex", "no_proxy")   # мог не существовать — ошибка ожидаема
    for d in SITE_QUERY_DOMAINS[:2]:
        probe_site_query(client, "SE::Yandex", "no_proxy", d)
```

И заменить `main()` (текущее тело — строки 49–57 скрипта, сверено 2026-07-17):

```python
def main() -> int:
    args = sys.argv[1:]
    if args and args[0] == "--only-continuation":
        # старый свип CANDIDATES на 8 парсеров содержит 6 подтверждённо мёртвых
        # (100% ReadTimeout / recaptcha, проба 2026-07-16) — гонять их заново значит
        # платить минуты таймаутов за уже известный ответ. Этот флаг гоняет ТОЛЬКО
        # continuation-блок.
        probe_continuation()
        return 0
    domains = args or ["example.com"]
    client = AParserClient()
    for d in domains:
        print(f"=== {d} ===")
        for parser, tmpl in CANDIDATES:
            probe(client, parser, tmpl.format(domain=d))
        print()
    probe_continuation()
    return 0
```

(Два реальных изменения относительно прежнего тела: флаг `--only-continuation` и вызов
`probe_continuation()` перед `return 0`. Существующие `CANDIDATES`/`probe` не трогать.)

- [ ] **Step 2: pyflakes**

Run: `.venv/bin/python -m pyflakes backend/scripts/aparser_probe_threadd.py`
Expected: без вывода.

- [ ] **Step 3: реальный живой прогон на боксе**

Run (на боксе или с машины с доступом к A-Parser `:9091` и реальным
`APARSER_API_KEY` в `.env` — Mac этой сессии доступ имеет, живой дебаг 2026-07-13/17
шёл именно с него):
```
PYTHONPATH=backend python backend/scripts/aparser_probe_threadd.py --only-continuation
```
(НЕ передавать домен позиционным аргументом — это запустит старый свип 8 кандидатов,
из которых 6 подтверждённо мёртвых, и прогон утонет в известных таймаутах.)

Ожидаемая длительность: SE::Google-блок — секунды-минуты; SE::Yandex-блок будет
честно висеть на ReadTimeout'ах (это и есть ожидаемый результат для `default`) —
не прерывать прогон досрочно, дождаться всех блоков.

Если этот прогон делает автономный агент без прямого доступа к боксу/`.env` с реальным
ключом — статус `NEEDS_CONTEXT`, а не догадка по докам. Записать в отчёт задачи **весь**
вывод трёх новых блоков (`SE::Google site:` включая `serp_urls`-хвост,
`SecurityTrails::Domain`, `SE::Yandex`) дословно — Task 2 читает эти строки, не
переспрашивает.

- [ ] **Step 4: сформулировать вывод пробы в отчёте задачи (без интерпретации «на глаз»)**

Отчёт задачи должен явно ответить на четыре вопроса построчно:
1. `SE::Google site:` (транспорт) — на всех ли ШЕСТИ сэмплах (3 домена × 2 попытки)
   `success:1` БЕЗ recaptcha/`ReadTimeout`/`Invalid query` хотя бы на одном? Да/Нет.
   Если попытки 1 и 2 дали РАЗНЫЙ исход на одном домене — это «через раз», ответ «Нет».
2. **Контроль формата** — продовый `serp_urls("site:wikipedia.org")` вернул НЕПУСТОЙ
   список URL? Да/Нет. (`success:1` с пустым парсингом на заведомо индексированном
   контроле означает, что фолбэк будет уверенно врать «не индексирован» всем подряд —
   класс лжи F2/F6, хуже честной слепоты. Пустой список у мусорного
   `dswjcndwijnwld23234212djf.ru` — наоборот, ОЖИДАЕМЫЙ правильный ответ.)
3. `SecurityTrails::Domain` — в конфиге пресета (поле `options` конверта) есть строка
   вида «API key»/«token»/аналог, и она пустая, ИЛИ конфиг не содержит такого поля
   вовсе (то есть таймаут — протокольный, не из-за отсутствующих кредов)? Кратко, с
   цитатой сырого JSON.
4. `SE::Yandex` — существует ли пресет `no_proxy` для этого парсера (не даёт ли
   `getParserPreset` ошибку), и если да — чинит ли он `ReadTimeout` на пробных доменах?
   Да/Нет/пресета нет.

- [ ] **Step 5: записать живые факты в дизайн-док (улики переживают отчёт задачи)**

Дописать в конец `docs/superpowers/specs/2026-07-17-threadd-serp-fallback-design.md`
секцию с РЕАЛЬНЫМИ результатами прогона (по образцу секции «Контекст»/«живые факты»
дизайн-дока 2026-07-16 — именно та запись сегодня спасла от повторного исследования
уже мёртвых кандидатов):

```markdown
## Живые факты (заполнено по итогам Task 1, 2026-07-17)

- `SE::Google` `site:` (6 сэмплов: 3 домена × 2 попытки): <исход по каждому сэмплу>
- `serp_urls("site:wikipedia.org")` (продовый парсинг): <N URL / пусто / ошибка>
- `serp_urls("site:dswjcndwijnwld23234212djf.ru")`: <ожидаемо пусто / иное>
- `SecurityTrails::Domain` getParserPreset: <есть ли поле ключа, заполнено ли; цитата>
- `SE::Yandex` default: <исход>; пресет `no_proxy`: <существует? чинит ли таймаут?>
- Вывод: ветка <А|Б> по критерию Task 2.
```

(Плейсхолдеры в угловых скобках заполняются ФАКТАМИ из вывода Step 3 — это
единственное место плана, где текст пишется по результату, а не заранее.)

- [ ] **Step 6: commit**

```bash
git add backend/scripts/aparser_probe_threadd.py docs/superpowers/specs/2026-07-17-threadd-serp-fallback-design.md
git commit -F - <<'EOF'
chore(Тред D): живая проба indexed_echo-фолбэка (SE::Google site:) + диагностика
SecurityTrails/SE::Yandex

Read-only проба, без парсинга — CLAUDE.md требует живого формата перед кодом.
Результат зафиксирован в дизайн-доке (секция «Живые факты») и определяет, какая
ветка Task 2 (вписать фолбэк или закрыть кандидат без кода) будет реализована.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
```

---

### Task 2: indexed_echo фолбэк ИЛИ закрытие без кода — по факту Task 1

**Files:**
- Modify: `backend/app/services/scoring.py` (стадия `echo`, строки ~497–502 на момент
  написания плана)
- Modify: `backend/tests/test_funnel.py` (`_clients` helper + новые тесты)

**Interfaces:**
- Потребляет: `AParserClient.serp_urls(query: str, limit: int = 10) -> list[str]` — УЖЕ
  существует и уже используется в проде (`backend/app/services/competitor.py`, M4), эта
  задача его переиспользует, новой транспортной функции не пишет.
- Производит (только ветка А): `sig["indexed_echo"]` теперь может быть заполнен через
  фолбэк без записи `errors: "searxng:..."` — потребитель этого поля (`compute_score`,
  `_BLIND_RU`) не меняется, контракт поля тот же (bool), только источник другой.

**Читай сначала:** отчёт Task 1 (три ответа из Step 4). Реализуй РОВНО ОДНУ из веток
ниже — ту, что соответствует фактическому результату пробы. Не реализуй обе, не
реализуй «на всякий случай» ветку А, если проба показала recaptcha/таймаут хотя бы
один раз из трёх доменов — порог тот же, что уже развёл SafeBrowsing/Archive (надёжно)
от TrustCheck/Compromised (0–50%, отклонены) в прошлой итерации Треда D.

**Критерий выбора ветки (дословно из дизайна):**
- **Ветка А**, если ответ Task 1 на вопрос 1 — «Да» (ни разу не упёрлось в
  recaptcha/`ReadTimeout`/`Invalid query` ни на одном из 6 сэмплов) **И** на вопрос 2 —
  «Да» (продовый `serp_urls` на контрольном `wikipedia.org` дал непустой список — не
  только `success:1` конверта, но и реально распарсенные URL).
- **Ветка Б** — если хотя бы один из двух ответов «Нет». Транспортный сбой и «конверт
  ок, парсинг пуст на заведомо индексированном» — оба дисквалифицируют: второй даже
  хуже, фолбэк молча помечал бы ВСЕ домены «не индексирован» (уверенная ложь класса
  F2/F6 вместо честной слепоты).

---

#### Ветка А — фолбэк работает, вписываем в воронку

- [ ] **Step A1: расширить `_clients` в `test_funnel.py` под фолбэк**

В `backend/tests/test_funnel.py` заменить сигнатуру и тело `_clients` (строки 26–51 на
момент написания плана):

```python
def _clients(whois_dt=None, wayback=None, rkn=False, bl=False, indexed_echo=True,
             whois=None, whois_raises=False, safebrowsing=False,
             indexed_echo_raises=False, serp_fallback=None):
    """whois: dict {"available":..., "created":...} (новый формат, приобретаемость известна
    явно). whois_dt: старый позиционный аргумент (только дата) — оборачивается в
    {"available": False, "created": whois_dt} (занят, но с датой регистрации — для тестов,
    доходящих до T2/T3 через lane="bid" на тестовом Domain). whois_raises=True — whois_probe
    бросает (недоступен). safebrowsing: True = зафлагован, False = чист, None = падает
    (исключение). indexed_echo_raises=True — searxng.indexed_echo бросает (симулирует
    недоступность SearXNG, включает фолбэк). serp_fallback: None — aparser.serp_urls
    (фолбэк) не должен вызываться вовсе, если он всё же вызван — вернёт []; list[str] —
    фолбэк возвращает эти URL; "raise" — фолбэк тоже падает (оба источника мертвы)."""
    pr = whois if whois is not None else {"available": False, "created": whois_dt}
    class _W:  # aparser
        def __init__(self):
            self.serp_calls = 0
        def whois_probe(self, dom):
            if whois_raises:
                raise RuntimeError("whois timeout")
            return pr
        def safebrowsing_check(self, dom):
            if safebrowsing is None:
                raise RuntimeError("safebrowsing timeout")
            return safebrowsing
        def serp_urls(self, query, limit=10):
            self.serp_calls += 1
            if serp_fallback == "raise":
                raise RuntimeError("serp timeout")
            return serp_fallback or []
    class _R:
        def is_listed(self, dom): return rkn
    class _B:
        def is_blacklisted(self, dom): return bl
    class _S:
        def indexed_echo(self, dom):
            if indexed_echo_raises:
                raise RuntimeError("searxng timeout")
            return indexed_echo
    return {"aparser": _W(), "rkn": _R(), "blacklist": _B(), "searxng": _S(),
            "wayback": wayback}
```

`_clients_whois_raises` (строки 54–70) не трогать — фолбэк там не тестируется, только
`_clients`.

- [ ] **Step A2: написать падающие тесты**

Добавить в конец `test_funnel.py` (после блока «SafeBrowsing hard-reject + Archive
pre-gate», после `test_safebrowsing_error_does_not_reject_and_is_logged`):

```python
# --- indexed_echo фолбэк через A-Parser SE::Google (Тред D, продолжение) ---------

def test_indexed_echo_fallback_used_when_searxng_fails():
    did = _mk(domain="fallback-ok.ru", referring_domains=3000, lane="bid")
    wb = _Wayback()
    old = datetime.now(timezone.utc) - timedelta(days=365 * 9)
    clients = _clients(old, wb, indexed_echo_raises=True,
                       serp_fallback=["https://fallback-ok.ru/"])
    out = scoring.score_domain(did, clients=clients)
    assert out["reject_reason"] is None
    assert not any(e.startswith("searxng:") for e in out["errors"])  # фолбэк спас
    assert clients["aparser"].serp_calls == 1


def test_indexed_echo_fallback_empty_is_not_indexed_not_blind():
    did = _mk(domain="fallback-empty.ru", referring_domains=3000, lane="bid")
    wb = _Wayback()
    old = datetime.now(timezone.utc) - timedelta(days=365 * 9)
    out = scoring.score_domain(did, clients=_clients(
        old, wb, indexed_echo_raises=True, serp_fallback=[]))
    assert out["reject_reason"] is None
    # пустая выдача через фолбэк — это ОТВЕТ "не индексирован", а не "не проверено"
    assert not any(e.startswith("searxng:") for e in out["errors"])


def test_indexed_echo_both_sources_fail_is_blind():
    did = _mk(domain="fallback-dead.ru", referring_domains=3000, lane="bid")
    wb = _Wayback()
    old = datetime.now(timezone.utc) - timedelta(days=365 * 9)
    out = scoring.score_domain(did, clients=_clients(
        old, wb, indexed_echo_raises=True, serp_fallback="raise"))
    assert out["reject_reason"] is None
    assert any(e.startswith("searxng:") for e in out["errors"])   # оба упали — вслепую


def test_indexed_echo_fallback_not_called_when_searxng_succeeds():
    did = _mk(domain="fallback-unused.ru", referring_domains=3000, lane="bid")
    wb = _Wayback()
    old = datetime.now(timezone.utc) - timedelta(days=365 * 9)
    clients = _clients(old, wb, indexed_echo=True, indexed_echo_raises=False)
    out = scoring.score_domain(did, clients=clients)
    assert out["reject_reason"] is None
    assert clients["aparser"].serp_calls == 0   # SearXNG жив — фолбэк не трогаем
```

- [ ] **Step A3: убедиться, что тесты падают (ровно ДВА из четырёх — сверено с текущим кодом)**

Run: `.venv/bin/python -m pytest backend/tests/test_funnel.py -k indexed_echo -v`
Expected (проверено против текущего `scoring.py:497-502` при написании плана):
- `test_indexed_echo_fallback_used_when_searxng_fails` — **FAIL**: текущий код на падении
  searxng пишет `searxng:RuntimeError` в errors (ассерт `not any(...)` падает); заодно
  `serp_calls == 1` не выполняется (фолбэка нет, вызовов 0).
- `test_indexed_echo_fallback_empty_is_not_indexed_not_blind` — **FAIL**: та же
  `searxng:`-ошибка в errors.
- `test_indexed_echo_both_sources_fail_is_blind` — **PASS уже сейчас** (текущий код и так
  пишет `searxng:`-ошибку). Это НЕ red-first тест, а регрессионный гард: его ценность —
  после Step A4 доказать, что фолбэк, упав, ВОЗВРАЩАЕТ слепую метку, а не съедает её.
- `test_indexed_echo_fallback_not_called_when_searxng_succeeds` — **PASS уже сейчас**
  (гард «фолбэк не дёргается зря»; станет содержательным после Step A4).

Никаких `AttributeError` на этом шаге не ожидается — все четыре теста доходят до
ассертов. Если падает что-то ДРУГОЕ или падают не те два теста — остановиться и
разобраться, не переходить к Step A4 (расхождение значит, что код воронки уже не тот,
против которого писался план).

- [ ] **Step A4: реализовать фолбэк в `scoring.py`**

В `backend/app/services/scoring.py` заменить блок `echo`-стадии (строки ~497–502):

```python
    jobs.report(run, stage="echo")
    # indexed_echo
    try:
        sig["indexed_echo"] = c["searxng"].indexed_echo(d.domain)
    except Exception as e:  # noqa: BLE001
        sig["errors"].append(f"searxng:{type(e).__name__}")
```

на:

```python
    jobs.report(run, stage="echo")
    # indexed_echo: SearXNG — первичный источник; A-Parser SE::Google (site:) — фолбэк,
    # когда SearXNG недоступен (живая проба 2026-07-17, docs/superpowers/specs/
    # 2026-07-17-threadd-serp-fallback-design.md). `serp_urls` уже существует и уже в
    # проде (M4 competitor.py) — переиспользуем, новой транспортной функции не пишем.
    # Успешный фолбэк НЕ пишет "searxng:"-ошибку: indexed_echo реально получен, просто
    # другим путём — иначе панель солгала бы "эхо НЕ проверено" поверх ответа, который
    # у нас есть (см. _BLIND_RU["searxng"]).
    try:
        sig["indexed_echo"] = c["searxng"].indexed_echo(d.domain)
    except Exception as e:  # noqa: BLE001
        try:
            sig["indexed_echo"] = bool(c["aparser"].serp_urls(f"site:{d.domain}", limit=1))
        except Exception:  # noqa: BLE001 — оба источника упали, вот теперь вслепую
            sig["errors"].append(f"searxng:{type(e).__name__}")
```

- [ ] **Step A5: тесты проходят**

Run: `.venv/bin/python -m pytest backend/tests/test_funnel.py -v`
Expected: все тесты файла (старые + 4 новых) PASS.

- [ ] **Step A6: полный сьют + pyflakes**

Run: `.venv/bin/python -m pytest backend/tests/ -q`
Expected: все тесты PASS. Это не допущение, а сверенная опись (grep по
`def indexed_echo` во всех тестах, 2026-07-17):
- Фейковые `searxng`, которые ВОЗВРАЩАЮТ значение и не бросают (фолбэк не активируется
  вовсе): `test_history_verdict.py:38`, `test_aparser_envelope.py:216`,
  `test_wayback_window.py:375`, `test_pipeline.py:52`, `test_m1_fixes.py:444`.
- Единственный бросающий — `test_rescore.py:114`: ловушка `raise AssertionError("T0
  обязан отклонить ДО эха")`, недостижимая по построению теста (T0 отклоняет раньше).
- Страховка на случай, если какая-то ловушка всё же сработает: у её фейкового
  `aparser` нет `serp_urls` → `AttributeError` → его ловит ВНУТРЕННИЙ `except Exception`
  фолбэка → в errors ложится та же `searxng:`-запись, что и до этой правки. То есть
  наблюдаемое поведение для любого существующего теста идентично текущему —
  сьют не может сломаться этой правкой by construction.

Если вопреки описи что-то падает — не глушить стабом вслепую, сначала прочитать
падение: расхождение с описью значит, что с момента написания плана в тесты въехал
новый фейковый клиент.

Run: `.venv/bin/python -m pyflakes backend/app backend/tests`
Expected: без вывода.

- [ ] **Step A7: commit**

```bash
git add backend/app/services/scoring.py backend/tests/test_funnel.py
git commit -F - <<'EOF'
feat(M1): indexed_echo фолбэк через A-Parser SE::Google при недоступности SearXNG

Живая проба 2026-07-17 подтвердила: SE::Google site:-запрос (тот же путь, что уже в
проде у M4 competitor.py через serp_urls) не упирается в recaptcha/таймаут. SearXNG
остаётся первичным источником; фолбэк — только страховка от единой точки отказа,
без нового рантайм-бюджета (бесплатный путь, редкий триггер). Успешный фолбэк не
помечает домен как "оценённый вслепую" — сигнал реально получен.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
```

---

#### Ветка Б — фолбэк ненадёжен, закрываем без кода

- [ ] **Step B1: код не пишем.** `scoring.py`/`test_funnel.py` не трогать вовсе — правка
ограничивается Task 3 (документация закрытия). Эта ветка существует, чтобы Task 2 не
считался «незавершённым» при отрицательном результате пробы: закрытие с доказательством
— тоже результат, а не пропуск задачи.

- [ ] **Step B2:** в отчёте задачи процитировать конкретную причину дисквалификации из
Task 1 — либо ошибку транспорта (recaptcha / `ReadTimeout` / `Invalid query`, с указанием
домена и попытки), либо провал контроля формата (`serp_urls("site:wikipedia.org")` пуст
при `success:1`) — чтобы Task 3 могла сослаться на неё дословно, как дизайн-документ уже
ссылается на пробу 2026-07-16.

---

### Task 3: doc-closure — CLAUDE.md фиксирует исход обоих кандидатов + итог ночной ветки

**Files:**
- Modify: `CLAUDE.md`

**Interfaces:**
- Потребляет: отчёты Task 1 и Task 2 (какая ветка была реализована).
- Производит: обновлённый `CLAUDE.md` — актуальный roadmap («Что делать дальше» больше
  не содержит закрытый пункт «Тред D») + changelog-запись, фиксирующая и уже смержённую
  ночную работу (SafeBrowsing/Optimizator — сейчас нигде в CLAUDE.md не отражена как
  влитая), и исход этой задачи.

- [ ] **Step 1: убрать закрытый пункт из «Ближайшее (блокирует MVP)»**

В `CLAUDE.md`, раздел `## Что делать дальше`, заменить:

```markdown
1. **Спека 4 — LLM-критик редактуры** — автоматическая оценка качества контента перед гейтом 
   (может заменить/дополнить человеческую редактуру, но гейт НИКОГДА не убирается).

2. **Тред D — дешёвые критерии скоринга** — SE::Google::SafeBrowsing, Rank::Archive, 
   SecurityTrails, SERP-fallback через SE::Google/Yandex (live тесты A-Parser форматов).

3. **Первый прогон воронки на боксе:**
```

на:

```markdown
1. **Спека 4 — LLM-критик редактуры** — автоматическая оценка качества контента перед гейтом 
   (может заменить/дополнить человеческую редактуру, но гейт НИКОГДА не убирается).

2. **Первый прогон воронки на боксе:**
```

**ВНИМАНИЕ, хвостовые пробелы (сверено `cat -e` 2026-07-17):** в живом CLAUDE.md строки
`…контента перед гейтом ` и `…SafeBrowsing, Rank::Archive, ` заканчиваются ПРОБЕЛОМ перед
переводом строки. Edit по old_string обязан включать эти пробелы дословно, иначе матч не
найдётся. Если Edit всё равно не находит текст — сверить живые строки через
`grep -n "Тред D — дешёвые" CLAUDE.md` и взять old_string из файла, не из плана.

И вторым Edit'ом привести в порядок номер следующего пункта (иначе в исходнике останется
дырка 1→2→4; рендер markdown её скроет, но файл читают и люди, и Claude — как текст):

заменить:
```markdown
4. **Первый домен через полную петлю** (M1→M5):
```
на:
```markdown
3. **Первый домен через полную петлю** (M1→M5):
```

- [ ] **Step 2: демоут текущего «Текущее состояние» в «Предыдущее» (по конвенции файла)**

Заменить заголовок:
```markdown
## Текущее состояние (2026-07-13, вечер) — ЖИВОЙ ДЕБАГ НА БОКСЕ
```
на:
```markdown
## Предыдущее состояние (2026-07-13, вечер) — ЖИВОЙ ДЕБАГ НА БОКСЕ
```
(Только заголовок секции — `##` → `## Предыдущее`, весь текст ниже него не трогать;
конвенция файла — самая свежая работа всегда под `## Текущее состояние`, предыдущая
демоутится на `## Предыдущее состояние`, см. уже существующие блоки 2026-07-12/2026-07-10.)

- [ ] **Step 3: вставить новую секцию `## Текущее состояние` ПЕРЕД демоутнутой**

Вставить непосредственно перед строкой `## Предыдущее состояние (2026-07-13, вечер) —
ЖИВОЙ ДЕБАГ НА БОКСЕ` (получившейся после Step 2 — это первая секция в файле сразу
после `## Что делать дальше`/шапки, следующая по порядку `## Предыдущее состояние
(2026-07-12)` идёт ниже неё и не трогается) следующий блок.

**Если была реализована Ветка А (фолбэк вписан):**

```markdown
## Текущее состояние (2026-07-17)

**Тред D закрыт полностью.** Ветки `feat/threadd-safebrowsing-archive` (SafeBrowsing
hard-reject + Optimizator-параллельная ветка `feat/optimizator-integration`) смержены
в `main` (2e88fe5) в ночь 2026-07-16→17, автономным прогоном subagent-driven-
development с self-paced итерациями (без диалога) — обе прошли `combine-reviewer`
(opus) с вердиктом «Готово к мержу». Итог ночи:
- **SafeBrowsing** — жёсткий отказ по Google Safe Browsing в воронке скоринга,
  реализован и работает (`reject_reason="safebrowsing"`).
- **Archive pre-gate** — реализован по плану, но повторное ревью нашло его
  архитектурно нерабочим (парсинг `"none times"` → `None`, не `0`; и даже рабочий не
  дал бы экономии — Wayback и так мгновенно выходит на пустом архиве) — убран из
  воронки, транспорт (`archive_probe`) остался неиспользуемым заделом.
- **Optimizator.ru** — второй канал выкупа доменов (nic.ru/RU-CENTER), денежный гейт,
  восстановление зависших заказов через `_settle()`. Независимое ревью (Codex) нашло и
  закрыло денежный баг: `OptimizatorAmbiguous` (исход неизвестен) и `OptimizatorError`
  (чистый отказ) ловились одним `except`, что могло разблокировать отмену заказа с
  невыясненной судьбой. Живой заказ ни разу не отправлялся — баланс 0 ₽, анкета
  `5014480/NIC-D` ещё не передана в управление на стороне nic.ru (организационный
  блокер, не код).
- **Живой аудит бокса в этой итерации НЕ проводился** — панель (`:8000`) недоступна
  с Mac в этой сессии (проверено `nc`, порт закрыт), в отличие от A-Parser (`:9091`,
  отвечал). Актуальные операционные цифры (офферы/выкуп/сайты/edited/публикация) —
  задача следующего прогона с реальным доступом к панели/БД, не додумываются
  (тот же принцип, что и для A-Parser-форматов: только по факту живой проверки).
  Последний подтверждённый живой аудит бокса — см. секцию «Предыдущее состояние
  (2026-07-13, вечер)» ниже, она сохраняет силу до следующей реальной проверки.
- **Оставшиеся два пункта Треда D закрыты этой же итерацией:** `SecurityTrails::Domain`
  и `SE::Yandex` (в т.ч. с проверкой непрокси-пресета по аналогии с `Rank::Archive`)
  — оба подтверждённо нерабочие на этом A-Parser-инстансе (см. живую пробу
  2026-07-17); `indexed_echo` получил рабочий фолбэк через `SE::Google` (`site:`-запрос,
  переиспользует уже существующий в проде `serp_urls`) — срабатывает только при
  отказе SearXNG, самого SearXNG не меняет, не помечает успешный фолбэк как «оценено
  вслепую». Тред D больше не в бэклоге.
```

**Если была реализована Ветка Б (фолбэк не вписан, оба кандидата закрыты без кода):**

```markdown
## Текущее состояние (2026-07-17)

**Тред D закрыт полностью.** Ветки `feat/threadd-safebrowsing-archive` (SafeBrowsing
hard-reject) и `feat/optimizator-integration` (второй канал выкупа) смержены в `main`
(2e88fe5) в ночь 2026-07-16→17, автономным прогоном subagent-driven-development с
self-paced итерациями (без диалога) — обе прошли `combine-reviewer` (opus) с
вердиктом «Готово к мержу». Итог ночи:
- **SafeBrowsing** — жёсткий отказ по Google Safe Browsing в воронке скоринга,
  реализован и работает (`reject_reason="safebrowsing"`).
- **Archive pre-gate** — реализован по плану, но повторное ревью нашло его
  архитектурно нерабочим (парсинг `"none times"` → `None`, не `0`; и даже рабочий не
  дал бы экономии — Wayback и так мгновенно выходит на пустом архиве) — убран из
  воронки, транспорт (`archive_probe`) остался неиспользуемым заделом.
- **Optimizator.ru** — второй канал выкупа доменов (nic.ru/RU-CENTER), денежный гейт,
  восстановление зависших заказов через `_settle()`. Независимое ревью (Codex) нашло и
  закрыло денежный баг: `OptimizatorAmbiguous` (исход неизвестен) и `OptimizatorError`
  (чистый отказ) ловились одним `except`, что могло разблокировать отмену заказа с
  невыясненной судьбой. Живой заказ ни разу не отправлялся — баланс 0 ₽, анкета
  `5014480/NIC-D` ещё не передана в управление на стороне nic.ru (организационный
  блокер, не код).
- **Живой аудит бокса в этой итерации НЕ проводился** — панель (`:8000`) недоступна
  с Mac в этой сессии (проверено `nc`, порт закрыт), в отличие от A-Parser (`:9091`,
  отвечал). Актуальные операционные цифры (офферы/выкуп/сайты/edited/публикация) —
  задача следующего прогона с реальным доступом к панели/БД, не додумываются
  (тот же принцип, что и для A-Parser-форматов: только по факту живой проверки).
  Последний подтверждённый живой аудит бокса — см. секцию «Предыдущее состояние
  (2026-07-13, вечер)» ниже, она сохраняет силу до следующей реальной проверки.
- **Оставшиеся два пункта Треда D закрыты этой же итерацией — БЕЗ нового кода.**
  Живая проба 2026-07-17 подтвердила: `SecurityTrails::Domain`, `SE::Yandex`
  (включая непрокси-пресет по аналогии с `Rank::Archive`) И `SE::Google` (site:-запрос,
  кандидат в фолбэк для `indexed_echo`) — все нерабочие/ненадёжные на этом A-Parser-
  инстансе (см. отчёт задачи). `indexed_echo` остаётся единственным источником —
  SearXNG (стабилен, всегда зелёный на `/diag`). Тред D закрыт как исследованный и
  отклонённый, по прецеденту `OpenPageRank`/`topic_switch`/`trademark_risk` — не
  притворяемся рабочим тем, что не работает.
```

- [ ] **Step 4: commit**

```bash
git add CLAUDE.md
git commit -F - <<'EOF'
docs: Тред D закрыт — CLAUDE.md фиксирует ночной мерж + исход SecurityTrails/Yandex

CLAUDE.md не отражал уже смержённые ветки SafeBrowsing/Optimizator (мерж прошёл без
док-коммита по решению контроллера — ожидали решения пользователя по CLAUDE.md).
Эта запись закрывает разрыв и фиксирует финальный исход обоих оставшихся пунктов
Треда D. Roadmap-пункт «Тред D» убран из «Ближайшее» — весь список закрыт.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
```

---

### Task 4: финальное whole-branch ревью + push

Работа идёт прямо в `main`, отдельной ветки нет — сознательно: три маленькие
последовательные задачи над уже смержённым кодом, без параллельной работы над другой
веткой в это же время (чтобы не повторить гонку чекаутов из прошлой ночи). Значит
«whole-branch» здесь = диапазон `BASE..HEAD`, где `BASE` записан контроллером в леджер
ПЕРЕД Task 1 (см. Global Constraints). НЕ использовать `main..HEAD` (пусто — мы В main)
и не `HEAD~N` (задачи многокоммитные, N угадывать нельзя).

- [ ] **Step 1: собрать review-package**

```bash
/Users/kozhan/.claude/plugins/cache/claude-plugins-official/superpowers/6.1.1/skills/subagent-driven-development/scripts/review-package "$BASE" HEAD
```
Скрипт печатает путь к файлу пакета (коммиты + stat + полный diff) — передать его
ревьюеру, не вклеивать diff в промпт.

- [ ] **Step 2: диспатч `combine-reviewer` (opus)**

Субагент `combine-reviewer`, модель opus. В промпте: путь к пакету из Step 1, пути к
дизайн-доку и этому плану (требования), явное напоминание диапазона `BASE..HEAD`.
Ревьюер read-only — рабочее дерево после него обязано остаться чистым (`git status`).

- [ ] **Step 3: разбор находок**

- Critical/Important → один фикс-агент на ВЕСЬ список (не по агенту на находку),
  прогон покрывающих тестов в отчёте фикса, затем повторное ревью изменённого куска.
- Minor → на решение пользователя, в код не тащить молча (прецедент: два Minor из
  ночных ревью diagnostics.py/poll_orders так и оставлены пользователю).

- [ ] **Step 4: финальная зелень + push**

```bash
.venv/bin/python -m pytest backend/tests/ -q     # ожидаемо: все PASS
.venv/bin/python -m pyflakes backend/app backend/tests   # ожидаемо: пусто
git push origin main
```
Push — только после вердикта «Готово к мержу» (или «С правками» с закрытыми
правками). Если ревью упёрлось в «Нет» — остановиться и вернуть решение пользователю,
не продавливать.
