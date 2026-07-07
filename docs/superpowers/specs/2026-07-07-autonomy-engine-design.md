# Спек 3 «Комбайн» — движок автономии (полуавто до гейтов)

**Статус:** дизайн одобрен (2026-07-07), готов к плану.
**Родитель:** «Комбайн» — машина полного цикла VPN-портфеля. Третий суб-спек: после
«Мозга M1» (Спек 1) и «Лица» (Спек 2) — **автономия**. Панель уже говорит по-русски и
показывает прогресс; теперь она сама двигает конвейер по стадиям, останавливаясь у
человеческих гейтов.

**Цель:** оператор включает стадии «автоматом», шедулер прогоняет каждую **безопасную**
стадию (discovery→score→очередь→провижн→черновик→публикация→мониторинг) до ближайшего
человеческого гейта; экран «Автопилот» показывает, что машина сделала и что ждёт человека.
Три гейта (курация, деньги, редактура) остаются ручными. Каждая авто-стадия под капом.

---

## §A. Модель автономии — конфиг тумблеров + капов

**Проблема:** сегодня шедулер (`workers/scheduler.py`) хардкодит `discovery + score`
раз в сутки, без настроек. Нужна конфигурируемая автономия по стадиям.

**Решение:** новый single-row конфиг (как `scoring_settings`) —
`backend/app/models/autonomy.py::AutonomySettings`, читается/пишется через
`backend/app/services/autonomy.py` (`get_autonomy()` / `update_autonomy(**kw)` /
`reset_autonomy()`, паттерн 1-в-1 с `services/settings.py`).

Поля (все с дефолтами):
- `autopilot_on: bool = False` — мастер-выключатель. Шедулер свипает, только если True. Ручной «прогнать сейчас» его минует.
- `sweep_interval_min: int = 60` — минимум минут между авто-свипами (throttle, кламп `[5, 1440]`).
- По стадии `auto_<stage>: bool` — `auto_discovery`, `auto_score`, `auto_queue`, `auto_provision`, `auto_generate`, `auto_publish`, `auto_check_index` (все дефолт False).
- По стадии `cap_<stage>: int` — `cap_score` (дефолт 20), `cap_queue` (10), `cap_provision` (5), `cap_generate` (5), `cap_publish` (5), `cap_check_index` (20). Кламп `[0, 500]`. **У `discovery` капа НЕТ** — это bulk-pull фида, не по-доменная стадия; только тумблер.

**Стадии автономии = только БЕЗОПАСНЫЕ переходы.** Три гейта НЕ представлены тумблером
(их нельзя переключить в «авто» в этом спеке — всегда человек): курация `scored→approved/rejected`,
деньги `confirm_order`/`execute`/`mark_caught`, редактура `mark_edited` (`draft→edited`).

Миграция `0004_autonomy` — создать таблицу `autonomy_settings` (single-row, seed при
первом `get_autonomy()`, как `scoring_settings`). Плюс таблица run-лога (см. §D).

---

## §B. Оркестратор — sweep-движок

**Новый файл** `backend/app/services/orchestrator.py`. Одна публичная функция:

```
def run_sweep(trigger: str = "cron", on_progress=None, respect_master: bool = True) -> dict
```

- Читает `get_autonomy()`. Если `respect_master` и не `autopilot_on` → вернуть `{"skipped": "autopilot_off"}` без работы. (Ручной запуск зовёт с `respect_master=False`.)
- **Single-flight замок (кросс-процесс):** шедулер (воркер-контейнер) и ручной свип (процесс панели) — разные процессы, один общий Postgres. Перед работой атомарно захватить замок: вставить строку `AutonomyRun(status="running", trigger=…)` ТОЛЬКО если нет незавершённой строки моложе `STALE_MIN=15` (крашнутый воркер → строка «running» старше 15 мин считается протухшей и перекрывается). Не захватили → вернуть `{"skipped": "already_running"}`. Возвращает `run_id`.
- По каждой **включённой** авто-стадии в порядке конвейера: найти подходящие сущности (запрос по статусу), продвинуть до капа, вызвав **существующий** безопасный сервис; собрать счётчик и ошибки. Ошибка одной сущности не топит стадию/свип (log + продолжаем; ошибка в run-лог).
- В конце — записать в строку `AutonomyRun`: `finished_at`, `status="done"|"failed"`, `counts` (JSON по стадиям), `errors` (JSON-список). Вернуть сводку.
- `on_progress(done, total, current)` — для прогрессбара ручного запуска (стадии как шаги).

**Таблица стадий (детерминированно, единственный источник истины оркестратора):**

| Стадия | флаг | кап | Кого берём (input) | Зовём сервис | Переход |
|--------|------|-----|--------------------|--------------|---------|
| discovery | `auto_discovery` | — | (нет) | `discovery.run_discovery()` | → строки `discovered` |
| score | `auto_score` | `cap_score` | `Domain.status=='discovered'` | `scoring.score_pending(limit=cap_score)` | `discovered → approved\|scored\|rejected` |
| queue | `auto_queue` | `cap_queue` | `Domain.status=='approved'` без открытого заказа, до капа | `acquisition.create_order(domain_id)` на каждый | `approved → purchasing` (+ заказ `pending_confirm`) |
| provision | `auto_provision` | `cap_provision` | (а) `Domain.status=='purchased'` без `Site` → `create_site_for`; (б) `Site.status=='provisioning'` → `provision` | `provisioning.create_site_for(domain_id)` + `provisioning.provision(site_id)` | `purchased →` сайт `provisioning → content` |
| generate | `auto_generate` | `cap_generate` | `Site.status=='content'` с 0 страниц | `content.generate_site(site_id, use_competitor=True)` | `content →` страницы `draft` |
| publish | `auto_publish` | `cap_publish` | `Site` с ≥1 `edited`-страницей, не полностью опубликован | `publish.publish_site(site_id)` | `edited`-страницы `→ published` |
| check_index | `auto_check_index` | `cap_check_index` | `Site` с `published`-страницами | `publish.check_index(site_id)` | `Page.index_status` обновлён |

**ЖЁСТКОЕ ПРАВИЛО (инвариант, покрыт регрессией):** оркестратор НИКОГДА не вызывает
`acquisition.confirm_order`, `acquisition.execute_confirmed_order`, `acquisition.mark_caught`
(денежный гейт) и `content.mark_edited` (редактурный гейт). Эти функции — только через
человеческие роуты панели. Money-байпас-роуты (`pipeline.py /purchase`, `set-status=purchased`)
оркестратор тоже не зовёт (см. §E).

**Поток авто-одобрения (зафиксировано, не сюрприз):** `scoring` сам ставит `approved`
сильным чистым доменам (Спек 1, `_decide`) — не только человек через курацию. Значит
`queue`-стадия подхватывает И авто-одобренные, И прошедшие ручную курацию → все едут в
`pending_confirm` на **денежный гейт**, где человек и решает про деньги. Курационный гейт
(`scored→approved/rejected`) остаётся для пограничных `scored`. Любое решение о покупке
человек видит на денежном гейте. Не устраивает — оператор выключает `auto_queue`.

**«Provision» = две под-операции под одним флагом:** сначала `create_site_for` для
`purchased`-доменов без сайта, затем `provision` для сайтов в `provisioning`. `provision()`
идемпотентен и возвращает `awaiting_ns` пока NS не прокинулся — свип просто повторит на
следующем тике (это правильное авто-поведение, не ошибка). Кап считает суммарно действия provision-стадии.

---

## §C. Шедулер — частый тик + throttle из конфига

**Переделать** `backend/app/workers/scheduler.py` (APScheduler `BlockingScheduler`,
воркер-контейнер уже есть в `docker-compose.yml`). Вместо хардкод-cron `m1_cycle`:

```
TICK_MIN = 5                                   # фиксированный частый тик
def tick():
    cfg = get_autonomy()
    if not cfg["autopilot_on"]:
        return                                 # мастер выкл — тумблер применяется сразу
    last = last_finished_sweep_at()            # из autonomy_run
    if last is not None and (now - last).total_seconds() < cfg["sweep_interval_min"] * 60:
        return                                 # throttle: рано
    orchestrator.run_sweep(trigger="cron")     # single-flight внутри
sched.add_job(tick, "interval", minutes=TICK_MIN, id="autopilot_tick",
              misfire_grace_time=TICK_MIN*60)
```

Каждый тик читает конфиг **свежим** из БД → тумблеры/интервал применяются без рестарта
воркера. `m1_cycle` удаляется (его поведение = `auto_discovery + auto_score` через
оркестратор). Воркер по-прежнему свой процесс, общий с панелью Postgres.

---

## §D. Run-лог + экран «Автопилот»

**Модель** `backend/app/models/autonomy.py::AutonomyRun` (та же миграция 0004):
`id`, `started_at` (tz), `finished_at` (tz, nullable), `trigger` (`cron|manual`),
`status` (`running|done|failed`), `counts` (JSON: `{stage: n}`), `errors` (JSON-список строк).

**Кросс-процессность:** run-лог в БД — единственный способ шедулеру (воркер) и панели
показать одно и то же (in-memory `jobs.py` живёт только в процессе панели). `jobs.py`
используется лишь для прогрессбара РУЧНОГО свипа (тот идёт в процессе панели).

**Новый экран «Автопилот»** (`GET /autopilot`, шаблон `autopilot.html`, пункт в сайдбаре
`base.html`) — кокпит комбайна:
- **Мастер + throttle:** переключатель `autopilot_on` + интервал (форма `POST /autopilot/settings`).
- **По-стадийные тумблеры + капы:** для каждой из 7 стадий — вкл/выкл (руками|автоматом) + слайдер капа (у discovery капа нет). Шильдик: каждая стадия подписана, что делает и до какого гейта дойдёт. Сохранение тем же `POST /autopilot/settings`.
- **«Ждёт тебя»** (счётчики у гейтов, с deep-ссылками):
  - курация: `count(Domain.status=='scored')` → `/domains?status=scored`
  - деньги: `count(AcquisitionOrder pending_confirm & not confirmed)` → `/queue`
  - редактура: `count(Page.status=='draft')` → сайты с черновиками
- **Последние N прогонов** (`autonomy_run` desc): время, триггер, счётчики по стадиям, ошибки.
- **«Прогнать сейчас»** — `POST /autopilot/run` → `jobs.start("sweep", lambda: orchestrator.run_sweep(trigger="manual", respect_master=False, on_progress=…))`; прогрессбар из Спек 2 (добавить `"sweep"` в whitelist `/run/{job}/progress`). Ручной свип уважает по-стадийные тумблеры, но минует мастер.

**Пульт** (`dashboard.html`) — компактная полоска статуса автопилота: вкл/выкл, время
последнего свипа, «ждёт тебя: N/N/N», ссылка на «Автопилот».

**Безопасность:** новые POST-роуты (`/autopilot/settings`, `/autopilot/run`) — в том же
guarded-роутере панели (CSRF same-origin + Basic-auth), как все админ-действия.

---

## §E. Улучшения существующего (все три — одобрено)

1. **Сверить enum-коммент `AcquisitionOrder.status`** (`models/domain.py:72-73`): в коде
   есть транзиентный `ordering` (`acquisition.py:14`) и `cancelled` (`acquisition.py:169`),
   в комменте их нет. Дополнить коммент до
   `pending_confirm | ordering | ordered | caught | failed | cancelled`. (Только коммент, без логики.)
2. **Зафиксировать money-байпас как ручной override.** Роуты `POST /domains/{id}/purchase`
   (`pipeline.py:57-64`) и `set-status=purchased` (`panel.py`) позволяют человеку пометить
   домен `purchased` мимо очереди — это осознанный ручной обход (человек = денежный гейт).
   Добавить коммент-предупреждение у обоих: «ручной override, оркестратор НЕ зовёт». Плюс
   регрессия: оркестратор ни при каких тумблерах не двигает домен в `purchased`.
3. **Долокализовать остаток Спек 2:** фильтр-чипы статусов на `/domains` (`domains.html`,
   `{{ st }}` → `{{ st|status_ru }}`, счётчик сохранить) и прозовые счётчики draft/edited в
   `dashboard.html`/`site.html` — через `status_ru` или явные русские слова. Мелкая правка.

---

## §F. Границы (что НЕ входит → будущие спеки)

- **Авто-редактурный гейт** (auto quality-bar: реальные данные вертикали + disclosure +
  не-тонко + LLM-самокритика вместо человека на `draft→edited`) → **Спек 4**. В этом спеке
  редактура — человек.
- **Уведомления** (telegram/email когда машина упёрлась в гейт) → **Спек 5**.
- **Провайдер-поллинг `ordered→caught`** (доктрина PIPELINE.md хочет авто, код ручной) —
  нужен транспорт заказа бэкордера + login-креды; `mark_caught` остаётся ручным.
- **M6 lifecycle** (`prune_and_redirect`, 301, отбраковка) — стаб, не в этом спеке.
- **Денежный гейт — ручной ВСЕГДА**, даже в полном автопилоте (финальное решение о покупке за человеком).
- Известный пробел (замечу, не чиню): `Domain.status='live'` нигде не присваивается сегодня.

---

## §G. Инварианты и контракт

- **Оба хард-гейта целы:** оркестратор зовёт только безопасные идемпотентные сервисы
  (`run_discovery`/`score_pending`/`create_order`/`create_site_for`/`provision`/`generate_site`/
  `publish_site`/`check_index`); НИКОГДА `confirm_order`/`execute_confirmed_order`/`mark_caught`/
  `mark_edited`/money-байпас. Покрыто регрессией (scored-домен и draft-страница НЕ двигаются свипом).
- **Каждая авто-стадия под капом** (кроме discovery); свип детерминирован и герметичен.
- **Оркестратор — только оркестрация:** никакой новой бизнес-логики скоринга/провижна/
  контента; вся логика в существующих сервисах. Оркестратор = запрос-подходящих + вызов + учёт.
- **Single-flight:** пересекающиеся свипы (воркер+ручной) не дублируют работу (DB-замок).
- **Конфиг читается свежим каждый тик** (тумблеры применяются без рестарта воркера).
- **Панель — светлая CMS, шильдик, русский UI**, CSS-переменные `base.html`. Новый экран/полоска — сразу по-русски.
- **Безопасность не ослаблять:** CSRF same-origin + Basic-auth; новые роуты в том же роутере.
- **Тесты офлайн+детерминированы:** оркестратор на SQLite-харнессе, сетевые сервисы мокаются
  (monkeypatch `orchestrator`-зовомых функций или их клиентов). Прогон
  `.venv/bin/python -m pytest backend/tests/ -q`; pyflakes чистый.

---

## §H. Фазы (для плана)

1. **Данные + конфиг:** `AutonomySettings` + `AutonomyRun` модели, `services/autonomy.py`
   (get/update/reset), миграция `0004_autonomy`, тесты конфига (дефолты/кламп).
2. **Оркестратор:** `services/orchestrator.py::run_sweep` — таблица стадий, single-flight
   замок, учёт в `AutonomyRun`. Тесты: каждая стадия двигает правильные сущности до капа;
   **гейт-инварианты** (scored/draft/approved НЕ пересекаются); single-flight (второй свип skip).
3. **Шедулер:** переделать `workers/scheduler.py` на тик+throttle; тест `tick()`
   (autopilot_off → skip; throttle → skip; иначе зовёт run_sweep).
4. **Экран «Автопилот»:** модель-роуты (`/autopilot`, `/autopilot/settings`, `/autopilot/run`),
   шаблон (тумблеры+капы+«ждёт тебя»+run-лог+прогон), сайдбар, полоска на Пульте, whitelist
   `"sweep"` в прогресс-роуте. Тесты роутов + рендера. Ручная визуальная проверка (Playwright).
5. **Улучшения §E:** enum-коммент, money-байпас-комменты+регрессия, локализация чипов/счётчиков.

---

## §I. Открытые вопросы

- Точные дефолты капов (`cap_score=20` и т.д.) — прикинуты, крутятся на «Автопилоте» без пересмотра спека.
- `STALE_MIN=15` для протухшего замка — эвристика, подстроить при живом прогоне.
- Ничего не блокирует старт плана.
