# План: фиксы по аудиту — «машина не должна врать»

> **Для агентов:** исполнять через `superpowers:subagent-driven-development` (в проекте так
> делали 6+ раз). Спека — `docs/superpowers/specs/2026-07-14-audit-fixes-design.md`.

**Цель:** закрыть подтверждённые находки внешнего аудита. База — `main` @ `5c05a1e`, 327 тестов.

**Архитектура:** точечные фиксы в существующих слоях. `integrations/` — только транспорт (там
появляются валидаторы конвертов), логика — в `services/`. Никаких новых модулей, кроме одного:
`services/transitions.py` (единая политика переходов статуса домена).

## Global Constraints

- Хард-гейты не двигаются: заказ провайдеру — только при `confirmed_by_human=true`; публикация —
  только из `edited`; `mark_edited` зовёт человек. Оркестратор их не зовёт.
- Тесты герметичны (autouse-фикстура рубит сеть; ловушки — наследники `BaseException`).
- pyflakes чист. UI на русском. CSS-классы — только из `base.html`.
- Коммит-мессаджи через `git commit -F -` (heredoc), не `-m`.
- **Каждая задача несёт регрессию, которая ПАДАЕТ на текущем `main`.** Тест, зелёный до фикса, —
  это не тест.
- Каждая задача = один коммит. Ревью — `combine-reviewer` (opus) после каждой.

---

## ВОЛНА 1 — правда о домене (первой: только здесь машина врёт АВТОМАТИЧЕСКИ)

### Задача 1: Wayback смотрит на всю жизнь домена, а не на его молодость (F1 + F7)

**Files:** `backend/app/integrations/wayback.py`, `backend/tests/test_wayback_window.py` (создать)

Корень: `get_snapshots` шлёт `limit=400` без `matchType`; CDX отдаёт первые 400 записей **по
возрастанию** → у плотно архивируемого домена окно упирается в 1999–2005. Живой замер:
`lenta.ru → 199911…200512`. Домен, ставший казино за 3 года до дропа, получает
`wayback_checked=True` и **авто-одобряется**.

- [ ] **Шаг 1. Падающий тест.** Фейковый CDX отдаёт 400 записей 1999–2005 + записи 2023–2025 за
      пределами лимита. Утверждение: выборка снимков **содержит capture из последнего года**.
      На текущем коде падает (последний год не запрашивается вовсе).
- [ ] **Шаг 2. Фикс.** Забирать окнами по времени, а не первые N: несколько CDX-запросов с
      `from`/`to` (напр. по годам-эпохам: первый год, середина, **последние 24 месяца**), плюс
      `matchType=domain` для поддоменов/внутренних URL. Последний capture — **обязателен** в
      выборке. Сохранять бюджет: суммарно тот же порядок запросов.
- [ ] **Шаг 3. Evidence.** `classify_history` возвращает `evidence: [{url, timestamp, cats}]`;
      `score_domain` кладёт их в `score_breakdown["history_evidence"]`. Без этого куратор не может
      проверить вердикт (а F3 доказал, что вердикт ошибается).
- [ ] **Шаг 4.** Тесты: покрытие последних лет; `matchType=domain` ловит `/casino/` при чистом
      корне; evidence непусты. Прогон, pyflakes, коммит.

### Задача 2: непроверенная история ≠ чистая (F2)

**Files:** `backend/app/services/scoring.py`, `backend/app/api/panel.py`,
`backend/app/templates/domains.html`, `backend/tests/test_history_verdict.py` (создать)

Корень: `blind_reason()` смотрит только `score_breakdown.errors`, а Wayback без снимков возвращает
`wayback_checked=False` **без ошибки**. Репро: `score=0.825, status=scored, blind_reason=None` →
домен в пакетном одобрении.

- [ ] **Шаг 1. Падающий тест:** `wayback_checked=False, errors=[]` → домен **не** в
      `_bulk_candidates` и помечен «история НЕ проверена».
- [ ] **Шаг 2.** Ввести явный вердикт истории вместо вывода по `errors`:
      `history_verdict(d) -> "clean" | "dirty" | "unknown"` (в `scoring.py`). `unknown` ⟸
      `wayback_checked` ложен ИЛИ покрытие не дотянуло. `blind_reason` строится на нём + на
      `errors` (для rkn/blacklist/searxng), а не только на `errors`.
- [ ] **Шаг 3.** `_bulk_candidates` исключает `history != "clean"`. В инбоксе домен с `unknown`
      несёт то же предупреждение, что и «вслепую».
- [ ] **Шаг 4.** `clean` в модели (`d.clean = status != "rejected"`) — переименовать смысл или
      перестать использовать как «чистая история»: сейчас это лишь «не отклонён».
- [ ] Тесты, pyflakes, коммит.

### Задача 3: классификатор истории по видимому тексту + мёртвые флаги (F3 + F4 + F5)

**Files:** `backend/app/integrations/wayback.py`, `backend/app/services/scoring.py`,
`backend/app/services/scoring_config.py`, `backend/tests/test_wayback_classify.py` (создать)

- [ ] **Шаг 1. Падающие тесты (репро подтверждены):**
      `<script>casino roulette casino</script>` + `<h1>Мебель</h1>` → **не** `casino`;
      `<a title="casino"><img alt="casino">` → **не** `casino`.
- [ ] **Шаг 2.** Классифицировать **видимый текст**: вырезать `<script>`/`<style>`/атрибуты
      (`nh3` уже в зависимостях, либо простой strip), считать по тексту + `<title>`.
- [ ] **Шаг 3. `topic_switch` — удалить.** Доказано: `(later − early) ∩ HARD_FLAGS` — строгое
      подмножество уже сработавшего hard-reject, не может добавить ни одного отказа. Флаг
      притворяется проверкой, которой нет. Удалить из `wayback.py` и из `compute_score`.
      (Тематическая преемственность донора инвариантом НЕ является — CLAUDE.md требует чистой
      истории, а не сохранения темы.)
- [ ] **Шаг 4. `trademark_risk` — удалить ветку.** Потребляется как hard-reject, **не имеет ни
      одного производителя** → всегда `NULL`, ветка мертва. Либо удалить из `compute_score`, либо
      реализовать расчёт — но реализация вслепую (по докстрингу) уже однажды похоронила cctld,
      поэтому в этой волне: **удалить**, оставив колонку и запись в CLAUDE.md.
- [ ] Тесты, pyflakes, коммит.

### Задача 4: A-Parser не имеет права молчать (F6)

**Files:** `backend/app/integrations/aparser.py`, `backend/app/services/scoring.py`,
`backend/tests/test_aparser_envelope.py` (создать)

Корень: `_call` не проверяет конверт. `{"success":0,"msg":"Auth failed"}` → пустой `resultString` →
`whois_probe` возвращает `{available: None, created: None}` **без исключения**. Следствие тяжелее
находки: `created=None` → **гейт `too_young` не применяется вовсе**, bid-домен едет дальше «как
проверенный», `errors` пуст → метка «вслепую» молчит.

- [ ] **Шаг 1. Падающий тест:** `_call` при `{"success":0,"msg":"Auth failed"}` → `RuntimeError` с
      текстом msg. Второй: домен, чей whois упал, **не** может быть авто-одобрен (нет возраста →
      нет права на approve).
- [ ] **Шаг 2. Фикс транспорта:** в `_call` — `if body.get("success") != 1: raise RuntimeError(...)`.
      Ошибка сама долетит до `sig["errors"]` как `whois:RuntimeError` (обработчик уже есть).
- [ ] **Шаг 3.** Добавить `whois:` и `ahrefs:` в префиксы `_BLIND_RU` — сейчас их там нет, и сбой
      whois не помечает домен «оценённым вслепую».
- [ ] **Шаг 4. Гард возраста:** если возраст неизвестен (whois не дал даты) — домен **не** может
      получить `approved` автоматически (как сейчас с Wayback). Добавить в `_decide`.
- [ ] Тесты, pyflakes, коммит.

---

## ВОЛНА 2 — деньги и состояния

### Задача 5: ставка обязана быть конечным числом (F8)

**Files:** `backend/app/services/acquisition.py`, `backend/tests/test_bid_validation.py` (создать)

Репро: `bid=nan` и `bid=inf` проходят **оба** гарда (`not bid_rub` → False; `bid_rub <= 0` → False),
`pick_tariff` не находит подходящий тир и возвращает последний → **5 000 000 ₽**.

- [ ] **Шаг 1. Падающие тесты:** `nan`, `inf`, `-inf`, `0`, отрицательная, ставка выше верхнего
      тира → `ValueError` **до** `pick_tariff`.
- [ ] **Шаг 2. Фикс:** `if not math.isfinite(bid_rub) or bid_rub <= 0: raise ValueError(...)`;
      верхняя граница — явный максимум (`MAX_BID_RUB`, из тарифной сетки зоны).
- [ ] Тесты, pyflakes, коммит.

### Задача 6: грязь не доезжает до кассы (F9 + F13)

**Files:** `backend/app/services/transitions.py` (создать), `backend/app/api/panel.py`,
`backend/app/api/pipeline.py`, `backend/app/services/acquisition.py`,
`backend/app/templates/{pool,domains,queue}.html`, `backend/tests/test_transitions.py` (создать)

Полный путь, который сейчас открыт: РКН-домен → кнопка «↩ вернуть в approved» (панель предлагает
её и для грязи) → «Готовы к выкупу» **без метки** → очередь выкупа **без метки** → ставка → покупка.
`reject_reason` не сбрасывается, но и **не показывается** там, где решают о деньгах.

- [ ] **Шаг 1. Падающие тесты:** `rejected/rkn` → `POST /domains/{id}/set-status approved` →
      отказ; `create_order` на домене с `reject_reason ∈ DIRTY` → отказ; `mark_purchased` на
      `discovered` → отказ.
- [ ] **Шаг 2. `services/transitions.py`:** единственное место, где меняется `Domain.status`.
      `DIRTY_REASONS = {"rkn", "blacklist", "history_dirty", "feed_flag"}` — из них выход в
      `approved` запрещён обычным действием.
- [ ] **Шаг 3.** `set_status_action` и `mark_purchased` ходят через политику (проверяют исходный
      статус, не только целевой). `create_order` отказывает домену с грязной причиной.
- [ ] **Шаг 4. UI:** в `pool.html` для грязных причин кнопка «вернуть» заменяется подписью
      «грязь — не возвращается»; `reject_reason` протянут в «Готовы к выкупу» и в `/queue`.
- [ ] Тесты, pyflakes, коммит.

### Задача 7: один открытый заказ на домен — инвариант БД (F10)

**Files:** `backend/alembic/versions/0010_order_uniqueness.py` (создать),
`backend/app/models/domain.py`, `backend/app/services/acquisition.py`,
`backend/tests/test_order_uniqueness.py` (создать)

- [ ] **Шаг 1. Падающий тест:** два `create_order` на один домен → одна строка (сейчас БД
      принимает две).
- [ ] **Шаг 2.** Partial unique — тот же приём, что уже применён для `job_run`:
      `Index("uq_open_order_per_domain", "domain_id", unique=True, postgresql_where=text("status IN ('pending_confirm','ordering','ordered')"), sqlite_where=...)`.
- [ ] **Шаг 3.** `create_order` ловит `IntegrityError` → возвращает id существующего заказа.
- [ ] Тесты, pyflakes, коммит.

### Задача 8: заказ не застревает и не теряется (F11 + F12)

**Files:** `backend/alembic/versions/0011_order_claim.py` (создать),
`backend/app/models/domain.py`, `backend/app/services/acquisition.py`,
`backend/app/templates/queue.html`, `backend/tests/test_order_recovery.py` (создать)

Два бага: (а) заказ, застрявший в `ordering` после краха, невидим для execute/poll/cancel и
навсегда держит домен в `purchasing`; (б) `cancel_order` пишет обычным UPDATE без проверки версии →
если ляжет после коммита execute, оплаченный заказ станет `cancelled` и **исчезнет из поллинга**.

- [ ] **Шаг 1. Падающие тесты:** заказ в `ordering` → `poll_orders` его **видит**; `cancel` после
      `execute` не превращает `ordered` в `cancelled`.
- [ ] **Шаг 2.** Колонка `claimed_at` (миграция). `poll_orders` включает `ordering` в выборку —
      **правда провайдера** (`find_order`) решает исход, как уже сделано для `maybe_sent`.
- [ ] **Шаг 3.** `cancel_order` — условный UPDATE с `rowcount` (как claim в execute), а не
      ORM-запись по PK.
- [ ] **Шаг 4.** `queue.html`: у `ordering` появляется бейдж «отправляется / исход неизвестен» и
      кнопка «сверить с провайдером».
- [ ] Тесты, pyflakes, коммит.

---

## ВОЛНА 3 — не врать о внешнем мире

### Задача 9: aaPanel отказал — значит отказал (F14 + F16)

**Files:** `backend/app/integrations/aapanel.py`, `backend/app/services/provisioning.py`,
`backend/app/services/publish.py`, `backend/tests/test_aapanel_errors.py` (создать)

aaPanel сообщает ошибки как **HTTP 200 + `{"status": false}`**. `ensure_site()` и `write_file()`
вызываются как statement — результат не смотрится, следом безусловно `content` / `published`.
Асимметрия: отказ Cloudflare летит `RuntimeError`, отказ aaPanel — молчит.

- [ ] **Шаг 1. Падающие тесты:** фейковый aaPanel `{"status": false, "msg": "permission denied"}`
      → `Site` остаётся `provisioning`, `Page` остаётся `edited`, ошибка сохранена.
- [ ] **Шаг 2.** Валидатор конверта в `integrations/aapanel.py`: `_ok(res)` → `RuntimeError(msg)`
      при `status is False`. Прогнать через него `add_site`, `SetSSL`, `CreateFile` (кроме
      «Requested file exists!»), `SaveFileBody`.
- [ ] **Шаг 3.** `provision`/`publish_site` больше не глотают: `except Exception: pass` на смене
      SSL-режима заменить на запись ошибки в результат (`ssl_error`) — при 80-only origin именно
      этот шаг решает, работает ли HTTPS.
- [ ] Тесты, pyflakes, коммит.

### Задача 10: индексация не выдумывает (F15)

**Files:** `backend/app/services/publish.py`, `backend/app/integrations/searxng.py`,
`backend/tests/test_index_truth.py` (создать)

`host_matches` сравнивает только hostname → главная в выдаче помечает `/setup` проиндексированной.
Пустая выдача (движки словили CAPTCHA — код это сам документирует) → `not_indexed` вместо `unknown`.
Стадия крутится **автопилотом** → машина регулярно пишет вымысел в `IndexHistory`.

- [ ] **Шаг 1. Падающие тесты:** главная в выдаче → `/setup` **не** `indexed`; все движки мертвы →
      `unknown`, а не `not_indexed`.
- [ ] **Шаг 2.** Сравнивать **path**, а не только host. Использовать уже существующий
      `unresponsive_engines()`: пустая выдача + мёртвые движки → `index_status = "unknown"`
      (значение в модели уже есть и не используется).
- [ ] Тесты, pyflakes, коммит.

---

## ВОЛНА 4 — машина о самой себе

### Задача 11: замок не отдаётся живой задаче (F17)

**Files:** `backend/app/services/jobs.py`, `backend/app/models/job.py`,
`backend/alembic/versions/0012_job_lease.py` (создать), `backend/app/services/orchestrator.py`,
`backend/tests/test_job_lease.py` (создать)

**Мой баг.** Докстринг верен по букве («реап только на пути захвата замка»), но неверен по выводу:
путь захвата замка **и есть** тот момент, когда убийство живой-но-молчащей задачи причиняет вред.
`updated_at` без `onupdate`; `run_sweep` репортит только на границе стадий; стадия `score`
(200 доменов × whois) и `generate` (LLM) молчат дольше `STALE_MIN=15`. Итог: воркер внутри
`generate` → оператор жмёт «прогнать свип» → `_reap` **убивает живую строку** → два свипа в двух
процессах → дубли страниц и двойной счёт LLM.

- [ ] **Шаг 1. Падающий тест:** живая задача, молчащая дольше `STALE_MIN`, но шлющая heartbeat →
      замок **не** отдаётся; второй `spawn` получает `False`.
- [ ] **Шаг 2. Лизинг:** `report(run_id=...)` вместо поиска строки **по имени** (сейчас зомби
      пишет прогресс в чужой живой прогон). `track` держит heartbeat (touch `updated_at`
      независимо от прогресса, раз в ~60 с).
- [ ] **Шаг 3.** `_reap` судит по heartbeat, а не по «последнему репорту прогресса».
- [ ] **Шаг 4.** Убрать второй, более слабый замок `AutonomyRun` (судит по `started_at` → теряется
      у любого свипа длиннее 15 мин) — свести к тому же лизингу.
- [ ] **Шаг 5.** Unique `(site_id, url_path)` на `pages` (миграция) — страховка от дублей.
- [ ] Тесты, pyflakes, коммит.

### Задача 12: стоп-кнопка не врёт (F18)

**Files:** `backend/app/services/discovery.py`, `backend/app/services/orchestrator.py`,
`backend/tests/test_cancel_coverage.py` (создать)

`jobs.cancelled()` читают только `score` и `recheck`. Кнопка рисуется **любой** живой задаче →
нажатие ставит флаг, задача доезжает до конца и закрывается как `done`.

- [ ] **Шаг 1. Падающие тесты:** cancel discovery → остановка между источниками, статус
      `cancelled`; cancel sweep → остановка между стадиями, статус `cancelled`.
- [ ] **Шаг 2.** Проверка `jobs.cancelled(...)` между источниками в `_collect` и между стадиями в
      `run_sweep`.
- [ ] Тесты, pyflakes, коммит.

### Задача 13: свип честно считает и не голодает (F19)

**Files:** `backend/app/services/orchestrator.py`, `backend/tests/test_sweep_outcomes.py` (создать)

`awaiting_ns`, `{"status":"error"}` и `created == 0` считаются `done += 1`; первые `awaiting_ns`
сайты занимают кап **вечно** (NS никто не автоматизирует); сайт с 1 из 3 страниц исключён из
следующих генераций навсегда.

- [ ] **Шаг 1. Падающие тесты:** `awaiting_ns` **не** считается `succeeded`; сайт с частично
      созданными страницами попадает в следующую генерацию; при ошибках сущностей свип не `done`,
      а `completed_with_errors`.
- [ ] **Шаг 2.** Счётчики `attempted/succeeded/awaiting/failed` вместо одного `done`. Селектор
      генерации: сайты, где страниц **меньше ожидаемого**, а не «нет ни одной».
- [ ] **Шаг 3.** Fairness: `awaiting_ns`-сайты не занимают кап провижна (они ждут человека).
- [ ] Тесты, pyflakes, коммит.

### Задача 14: воронка не платит за домен до его дропа (F20)

**Files:** `backend/app/services/scoring.py`, `backend/tests/test_funnel.py`

**Мой баг, пропущенный ревью прошлой ветки.** Ветка «дата известна» в `scorable()` использует
`deadline <= now + DROP_GRACE` — то есть берёт домены, чей дроп **завтра/послезавтра**. Они
гарантированно заняты → whois впустую.

- [ ] **Шаг 1. Падающий тест:** дроп завтра → домен **не** в выборке `score_pending`.
- [ ] **Шаг 2. Фикс:** `deadline <= now` (окно ловли открылось). `DROP_GRACE` остаётся **верхней**
      границей в `acquirability_verdict` — это разные границы, не путать.
- [ ] Тесты, pyflakes, коммит.

---

## ВОЛНА 5 — гигиена (дёшево, но кусается)

### Задача 15: свежая установка не жжёт деньги (F21 + F30)

**Files:** `backend/alembic/versions/0002_funnel.py`, `backend/alembic/versions/0013_source_defaults.py`
(создать), `backend/app/services/discovery.py`, `backend/tests/test_fresh_install.py` (создать)

Миграция `0002` вставляет `{"cctld": true, "reg_ru": true, "sweb": true}`, хотя рантайм-дефолты
объявляют их `false`. **Денежное плечо:** платный Ahrefs зовётся ровно для доменов **без RD**, а RD
даёт только backorder-фид → витрины и есть единственный источник расходов на капчу.

- [ ] **Шаг 1. Падающий тест:** миграции с нуля → `sources_enabled` = только backorder.
- [ ] **Шаг 2.** Исправить `INSERT` в `0002` + корректирующая миграция `0013` для уже накатанных БД
      (выключить витрины, если оператор их не включал осознанно — безопаснее выключить).
- [ ] **Шаг 3. (F30)** `canonical_domain`: отвергать leading/trailing hyphen, битый punycode,
      числовой/односимвольный TLD, IP. Мусорная строка иначе доедет до whois/Ahrefs.
- [ ] Тесты, pyflakes, коммит.

### Задача 16: деплой не рисует зелёное на упавшей миграции (F22 + F23 + F29)

**Files:** `backend/app/services/deploy.py`, `backend/app/models/__init__.py`,
`backend/scripts/smoke.py`, `backend/tests/test_deploy_honesty.py` (создать)

- [ ] **Шаг 1. Падающие тесты:** `alembic returncode != 0` → `ok=False` и красный баннер;
      `Base.metadata.tables` содержит все 11 таблиц.
- [ ] **Шаг 2.** `ok = (mig.returncode == 0)`; панель показывает `err=`, а не `msg=`.
- [ ] **Шаг 3.** `models/__init__.py` импортирует `settings`, `autonomy`, `job` — иначе
      `autogenerate` предложит **DROP** четырёх таблиц.
- [ ] **Шаг 4. (F29)** `smoke.py`: `sys.exit(1)` при любом обязательном `[FAIL]` — либо удалить
      скрипт целиком (его вытеснил `/diag`, который M3 честно пингует) и убрать команду из
      CLAUDE.md. **Решение принимает контроллер.**
- [ ] Тесты, pyflakes, коммит.

### Задача 17: скоринг не теряет доказательства (F25 + F24)

**Files:** `backend/app/services/scoring.py`, `backend/app/models/domain.py`,
`backend/alembic/versions/0014_scored_at.py` (создать), `backend/tests/test_rescore.py` (создать)

Репро: 1-й скоринг `0.8946` (authority=1.0) → 2-й `0.7746` (authority=0.0) — `dr` не поднимается из
БД. Сигналы (`prior_flags`, `age_years`, `rkn_listed`…) пишутся **без гарда на None** → ранний
reject стирает проверенную историю.

- [ ] **Шаг 1. Падающие тесты:** повторный скоринг не меняет `authority` без нового наблюдения;
      ранний reject **не** стирает `prior_flags`/`age_years`.
- [ ] **Шаг 2.** `sig.setdefault("dr", d.dr)` (как уже сделано для `referring_domains`); запись
      сигналов — только если `sig` реально их содержит.
- [ ] **Шаг 3.** Колонка `scored_at` (миграция) — сейчас непонятно, когда домен оценён.
- [ ] **Шаг 4. (F24)** unique: `Site.domain_id`, `(SiteOffer.site_id, offer_id)`.
- [ ] Тесты, pyflakes, коммит.

### Задача 18: страница помнит, на каком языке и под какой оффер написана (F26 + F27 + F28)

**Files:** `backend/app/models/site.py`, `backend/alembic/versions/0015_page_brief.py` (создать),
`backend/app/services/content.py`, `backend/app/services/publish.py`, `backend/app/api/panel.py`,
`backend/tests/test_content_contract.py` (создать)

`lang` берётся из **текущего** оффера в момент публикации; колонки языка у страницы нет; scaffold и
CTA всегда русские; `country` в промпт не попадает. Русский текст опубликуется с `<html lang="en">`,
если поменять оффер.

- [ ] **Шаг 1. Падающие тесты:** страница, сгенерированная под оффер A на `ru`, публикуется с `ru`
      и брендом A, даже если активным стал оффер B; `javascript:` в `affiliate_link` → отказ.
- [ ] **Шаг 2.** Колонки `Page.lang`, `Page.offer_id` (миграция) — фиксируются при генерации;
      публикация берёт их, а не «текущий активный оффер».
- [ ] **Шаг 3.** Убрать из промпта требование «замеры скорости», пока в `vertical_data` нет
      замеров (**F27**): сейчас промпт прямо провоцирует выдуманные цифры, а гейт редактуры
      превращается в «поймай галлюцинацию».
- [ ] **Шаг 4. (F28)** allowlist схем `http/https` при создании оффера и в `render_html`.
- [ ] Тесты, pyflakes, коммит.

---

## Порядок и остановки

Волны 1–2 — обязательны до возобновления пакетного одобрения и живого выкупа. Волны 3–5 можно
дробить. **После каждой волны — прогон на боксе**, а не только зелёный сьют: этот аудит доказал,
что 327 зелёных тестов уживались со всеми 30 находками.

## Safe mode на время работ (операционное, без кода)

- не пользоваться пакетным одобрением (до Задачи 2);
- не возвращать домены из `rejected` в `approved` (до Задачи 6);
- не включать мастер-тумблер автопилота (до Задачи 11);
- живой выкуп — только последовательно, со сверкой в ЛК провайдера (до Задач 5, 7, 8).
