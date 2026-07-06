# Спек: умная воронка discovery + прогресс + версия

Дата: 2026-07-06. Модуль: M1 (Domain Intelligence) + инфра панели.
Статус: одобрен пользователем (брейншторм 2026-07-06).

## Цель
Одним заходом закрыть 4 запроса оператора:
1. **Прогресс** длинных задач Discovery/Score (сейчас синхронно, без обратной связи).
2. **Больше бесплатных источников** дропов (не только backorder).
3. **Многоступенчатая воронка скоринга** дёшево→дорого с настраиваемыми порогами:
   сырой поток прунится дешёвыми проверками, дорогая история/DR — только для лучших.
4. **Версия**: блок «текущая версия» в /diag + уведомление после git-обновления.

## Решения из брейншторма (зафиксировано)
- Источники: **все 4 бесплатных** — cctld + backorder + reg.ru + sweb. nic-аукцион НЕ берём
  (платные лоты — это M2 выкуп, не бесплатное discovery).
- Возраст для дешёвого гейта: **whois created-date** (A-Parser `Net::Whois`); Wayback-возраст — фолбэк.
- Настройки: **минимум-гейты** (~6 крутилок), не полный контроль весов.
- Прогресс: **in-memory**, без очереди задач (один оператор).
- Конфиг порогов: **одна строка в БД**, не key-value.
- Отклонённые домены НЕ удаляются — видны с причиной (`reject_reason`).

## Инварианты (НЕ ломать)
- Два хард-гейта нетронуты: M2 деньги (`confirmed_by_human`), M4 редактура (`edited`).
  Воронка — это M1 скоринг, гейтов не касается.
- `compute_score` остаётся ЧИСТОЙ (unit-тест без I/O). Ступени/ранний выход — в `score_domain`.
- Wayback-вежливость / экономия квот: дорогой шаг (T3) выполняется ТОЛЬКО для выживших
  T0–T2 — это и есть основная выгода воронки.
- Оффлайн SQLite тест-харнесс сохраняется; сеть в тестах мокается.

---

## A. Модель данных (миграции Alembic)

`backend/app/models/domain.py`:
- `reject_reason: Mapped[str | None] = mapped_column(String(32))` — где домен отвалился:
  `low_rd | feed_flag | too_young | rkn | blacklist | history_dirty | low_score`. NULL если не отклонён.
  (`low_rd` — T0 мало доноров; `low_score` — T3 итоговый скор ниже manual-порога. Разные ступени.)
- `whois_created: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))` — дата
  регистрации из whois (первичный источник возраста; `age_years` считается из неё, Wayback — фолбэк).

Новая таблица `scoring_settings` (single-row, id=1):
```
id (pk, всегда 1)
min_referring_domains: int   = 1
min_age_years:         float = 3.0
approve_at:            float = 0.70
manual_review_at:      float = 0.40
sources_enabled:       JSONB = {"backorder": true, "cctld": true, "reg_ru": true, "sweb": true}
updated_at:            datetime
```
`scoring_config.py` остаётся как **дефолты/сид**: если строки нет — берём его значения.
Хелпер `get_settings()` в новом `services/settings.py`: читает строку, при отсутствии
создаёт из `scoring_config`. `update_settings(**kw)` — валидация диапазонов + запись.

Две миграции: (1) две колонки в `domains`; (2) таблица `scoring_settings` + сид-строка.

---

## B. Источники дропов — плагинный интерфейс

Контракт адаптера (`integrations/`): метод возвращает нормализованные строки
```python
list_dropping() -> list[dict]   # {"domain": str, "source": str, "referring_domains": int|None}
```

Источники:
- **backorder** (есть, `integrations/backorder.py`): JSON-фид, даёт RD (`links`) + фид-флаги
  (`rkn/judicial/block`). Нормализация уже в `services/discovery.py`.
- **cctld** (новый `integrations/cctld.py`): авторитетный реестр освобождающихся .ru/.рф с
  cctld.ru/service/dellist/. Формат выверить на живой странице (список/файл доменов). RD нет.
- **reg.ru** (новый `integrations/regru_drops.py`): HTML-витрина reg.ru/domain/deleted. Бот-защита →
  тянуть через **A-Parser** `fetch_html`, парсить таблицу доменов. RD нет.
- **sweb** (новый `integrations/sweb_drops.py`): HTML sweb.ru/domains/deleted, аналогично через A-Parser.

`services/discovery.py`:
- `SOURCES = {"backorder": ..., "cctld": ..., "reg_ru": ..., "sweb": ...}` (имя→адаптер+нормализатор).
- `run_discovery()` идёт по **включённым в settings** источникам, собирает строки, **дедупит по
  domain** (приоритет строки с бо́льшим RD — backorder важнее сырых списков), upsert как сейчас
  (batch + IntegrityError-ретрай уже есть). Фид-флаги backorder (rkn/judicial/block) сохраняем,
  чтобы T0 мог по ним резать.
- Возвращает разбивку: `{"total_new": N, "by_source": {...}}` для баннера/прогресса.

Замечание реализации: точные URL/HTML-селекторы cctld/reg.ru/sweb ВЫВЕРИТЬ на живых
страницах (некоторые под бот-защитой — тестировать через A-Parser бокса). Парсеры покрыть
тестами на **сохранённых HTML-фикстурах**, не живой сетью.

---

## C. Воронка скоринга — дёшево→дорого, ранний выход

`services/scoring.py` — `score_domain` переписывается в ступени; читает пороги из `get_settings()`.
До дорогого шага доходят только выжившие. На каждом отсеве пишем `reject_reason`, `status=rejected`,
и выходим (дорогие клиенты не трогаем).

| Ступень | Стоимость | Проверка | Гейт |
|---|---|---|---|
| **T0 фид** | 0 | RD из строки + фид-флаги | RD < `min_referring_domains` → `low_rd`(*); фид-флаг rkn/суд/блок → `feed_flag` |
| **T1 whois** | дёшево | `Net::Whois` → `whois_created` → возраст | возраст < `min_age_years` → `too_young`, стоп. whois не отдал → НЕ режем, идём дальше (Wayback-возраст на T3) |
| **T2 риск** | средне | РКН-реестр + Spamhaus + indexed_echo (SearXNG) — только lookups | rkn_listed / blacklisted → `rkn`/`blacklist`, стоп |
| **T3 история** | дорого | Wayback prior_flags + topic_switch + DR (OPR, вес=0) | грязный флаг → `history_dirty`; иначе `compute_score` → approved/scored/rejected(`low_score`) |

(*) T0 RD-гейт мягкий: сырые списки (cctld/reg/sweb) приходят без RD (`None`) — их НЕ режем на T0
по RD (нечего сравнивать), они проходят на T1, где отсекаются по возрасту. RD-гейт применяется
только когда RD известен (backorder).

`compute_score` (чистая) — без изменений в сигнатуре: та же композиция history/age/rd_proxy/indexed_echo.
Меняется только оркестрация в `score_domain`: последовательные try-ступени с early-return.
`score_pending(limit)` — тот же вход, внутри гоняет новую воронку; репортит прогресс (см. E).

whois-парсер (`integrations/aparser.py`, новый метод `whois_created(domain) -> datetime|None`):
- вызвать `Net::Whois` через `_call("oneRequest", {parser: "Net::Whois", ...})`;
- из `resultString` вытащить дату регистрации. Форматы: .ru/.рф — строка `created: YYYY.MM.DD`;
  gTLD — `Creation Date: ISO8601`. Регексп на оба, вернуть самую раннюю распарсенную дату или None.
- покрыть тестом на сохранённых whois-ответах (.ru и gTLD), None при мусоре.

`age_years` в `score_domain`: если есть `whois_created` → из неё; иначе Wayback `first_seen` (T3).

---

## D. Экран `/settings` — ползунки (принцип шильдика)

Светлая карточка (стиль как в `base.html`), группы по ступеням воронки. Каждый контрол
подписан ТЕМ, ЧТО ОТСЕКАЕТ, + **живой счётчик** «X из N доменов пула проходит этот гейт».

Контролы (минимум-гейты):
- **min RD** (T0) — «отсекает доноров-пустышки»; счётчик доменов с RD ≥ порога.
- **min возраст, лет** (T1) — «отсекает молодые домены до дорогой истории»; счётчик по `whois_created`.
- **порог approve** (T3) — «выше — авто-одобрение»; счётчик approved при текущем пороге.
- **порог manual** (T3) — «ниже — reject»; счётчик scored (manual) в вилке.
- **чекбоксы источников** — вкл/выкл backorder/cctld/reg_ru/sweb.
- Кнопка **«сбросить к дефолтам»** (сид из `scoring_config`).

Счётчики считаются по УЖЕ собранным доменам (быстрый SQL `count` с фильтром) — превью эффекта
порога без пере-скоринга. Сохранение — POST-форма `/settings/save` (CSRF-guard уже прикрывает,
Basic-auth тоже). Роут `GET /settings` + `POST /settings/save` + `POST /settings/reset`.
Пункт «Настройки» в сайдбаре.

---

## E. Прогресс длинных задач (Discovery/Score)

Проблема: `POST /run/discovery` и `POST /run/score` синхронны — блокируют до конца, оператор
не видит хода. Меняем на фоновый прогон + polling.

- **In-memory реестр** `services/jobs.py`: словарь `{job_name: {running, done, total, current, message,
  error, started}}` под `threading.Lock`. `job_name ∈ {"discovery","score"}`.
- Старт: роут кладёт задачу в `ThreadPoolExecutor(max_workers=1)` (свой на процесс), возвращает
  redirect сразу. **Двойной старт запрещён**: если `running` — баннер «уже идёт».
- Прогон обновляет реестр по мере доменов (`done/total`, `current=domain`, `message`).
- `GET /run/{job}/progress` → JSON `{running, done, total, current, message, error}`.
- Панель (`domains.html`): маленький inline-JS — если джоб `running`, каждые ~1.5с poll →
  рисует полосу «Score: 3/5 — history example.ru», по завершении перезагружает страницу.
  Ломает no-JS **только для этих двух кнопок**; один оператор, современный браузер — ок.
- Джоб живёт в памяти; рестарт контейнера его теряет (допустимо — перезапустить кнопкой).

Гейты/семантика скоринга не меняются — только обёртка выполнения.

---

## F. Версия / self-update

- **Блок в `/diag`**: текущая версия — из git в контейнере (репо смонтировано `/repo`):
  `git -C /repo rev-parse --short HEAD` + `git -C /repo log -1 --format=%s` + дата `%cs`.
  Дёшево (локально), показываем всегда. Хелпер `services/version.py: current_version() -> dict`.
- **Уведомление после pull** (`panel.py /admin/pull`): вместо «Обновлено: <хвост>» —
  «Обновлено: `old7`→`new7` «`subject`»» (old = HEAD до pull, снять до `git pull`).
- **Кнопка «проверить обновления»** (в /diag): `git -C /repo ls-remote origin main` → сравнить
  с HEAD → «актуально» / «доступна новее (`remote7`)». On-demand (одна сетевая операция),
  роут `POST /admin/check-updates`. Токен — тем же способом через extraheader (как в pull).

Замечание: durable-фикс git-кнопки (fetch+reset --hard против EOL-фантома контейнера) —
уже отдельно обсуждён с оператором; в этот спек не входит, но может быть смежной правкой `/admin/pull`.

---

## G. Тестирование (оффлайн SQLite, сеть мокается)

Ключевые регрессии:
- **Ранний выход воронки**: whois-мок = 1 год → домен `reject_reason=too_young`, статус rejected,
  **Wayback-клиент НЕ вызван** (проверить мок-счётчиком). Это ядро экономии.
- whois-мок падает → на возрасте НЕ режем, доходим до T3, Wayback-возраст как фолбэк.
- T2: rkn_listed → `reject_reason=rkn`, T3 не вызван.
- Дедуп источников: один домен из backorder(RD=50)+cctld(RD=None) → одна запись, RD=50.
- Settings переопределяют дефолты: `min_age_years=5` режет 4-летний домен.
- `whois_created` парсер: .ru `created: YYYY.MM.DD` и gTLD ISO → datetime; мусор → None.
- Парсеры витрин reg.ru/sweb на сохранённых HTML-фикстурах → список доменов.
- Прогресс-реестр: старт→done растёт до total; двойной старт отклоняется; progress-endpoint отдаёт JSON.
- Версия: `current_version()` парсит git-вывод (мок subprocess) → {hash, subject, date}.
- `compute_score` self-check и существующие тесты остаются зелёными (сигнатура не менялась).

---

## H. Порядок реализации (фазы, subagent-driven)

1. **Данные**: миграции (2 колонки + `scoring_settings`), модель, `services/settings.py` (get/update/reset).
2. **whois**: `AParserClient.whois_created()` + парсер даты + тест.
3. **Воронка**: рефактор `score_domain` в T0–T3 с ранним выходом; `reject_reason`; age из whois.
   Обновить тесты скоринга.
4. **Источники**: cctld/reg_ru/sweb адаптеры + дедуп в `run_discovery` + `sources_enabled`.
5. **/settings UI**: роуты + шаблон (ползунки, счётчики, reset) + пункт сайдбара.
6. **Прогресс**: `services/jobs.py` + фоновый прогон discovery/score + progress-endpoint + JS-полоса.
7. **Версия**: `services/version.py` + блок в /diag + уведомление в /admin/pull + check-updates.

Каждая фаза — свежий имплементер + таск-ревью (spec+quality), фиксы Critical/Important, затем
финальный whole-branch ревью. Гейты и оффлайн-тесты — контрактом в каждый диспатч.

---

## Открытые вопросы (выверить на реализации, не блокируют)
- Точные URL/селекторы cctld/reg.ru/sweb — проверить на живых страницах бокса (бот-защита → A-Parser).
- Формат whois-даты для не-.ru TLD в конкретном A-Parser Net::Whois пресете — снять живой ответ.
- Объём cctld-списка (десятки тыс.): не грузит ли T1-whois на всём пуле — при необходимости
  ограничить discovery-батч или whois только для топ-N по иным дешёвым сигналам (отметить, если всплывёт).
