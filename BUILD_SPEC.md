# BUILD_SPEC — VPN Affiliate Portfolio (для Claude Code)

> Технический бриф для реализации. Стратегический контекст — в `PLAN.md`.
> Использование с Claude Code: можно положить короткий `CLAUDE.md`, который ссылается на этот файл, или скармливать разделы по мере сборки модулей.

---

## 1. Что строим (одним абзацем)

Система управления портфелем VPN affiliate-сайтов. Замкнутый цикл: **discovery доменов → скоринг качества → выкуп (с ручным подтверждением) → провижн (Cloudflare + aaPanel) → генерация контента (LLM + редактура) → публикация + мониторинг индексации → жизненный цикл**. Управляется через собственное приложение (backend + панель), которое оркестрирует внешние сервисы по API.

**Принцип, зашитый в код (не обсуждается, просто соблюдается):** портфель немногих качественных сайтов, а не поток тонких сборников. Практически это значит два жёстких гейта в state machine: (1) контент нельзя опубликовать, минуя статус ручной редактуры; (2) выкуп домена нельзя выполнить без ручного подтверждения. Обоснование — в `PLAN.md §2`.

---

## 2. Стек

- **Backend:** Python 3.12 + FastAPI + SQLAlchemy 2.x + Pydantic v2 (pydantic-settings для конфига).
- **БД:** PostgreSQL 16. Миграции — Alembic.
- **Задачи/расписание:** для MVP — простые cron-энтрипойнты (APScheduler или отдельный worker-контейнер с loop). Позже — Celery + Redis (заложить структуру `workers/`, но не тащить Redis в MVP).
- **HTTP-клиент:** httpx (async).
- **Панель (frontend):** для MVP — минимальная. Варианты по возрастанию усилий: (a) FastAPI + Jinja/HTMX прямо в backend; (b) отдельный Nuxt. **Рекомендация для MVP — (a)**, чтобы усилия шли в пайплайн, а не в UI. Frontend выносим в `panel/` отдельным сервисом, только когда пайплайн работает.
- **Оркестрация:** docker-compose. Сервисы: `db`, `backend`, `worker` (позже — `panel`, `redis`).
- **Секреты:** `.env` через pydantic-settings. Никогда не коммитить. В репо — только `.env.example`.

---

## 3. Роль aaPanel (важно — прочитать перед провижном)

aaPanel — **не** фреймворк панели. Это control panel на VPS, которым наше приложение управляет по HTTP API. Разделение:

- **Cloudflare** держит DNS и проксирует (маскирует origin-IP VPS). A-запись домена → origin-IP VPS, `proxied=true`.
- **aaPanel** держит vhost на VPS: создаёт сайт (nginx-конфиг, docroot), выпускает origin-SSL (Let's Encrypt) при необходимости. DNS aaPanel **не** используем — он бы убил маскировку origin через Cloudflare.
- **Наше приложение** оркестрирует оба по API.

> Альтернатива «сделать всё плагином aaPanel» — отвергнута: плагины aaPanel завязаны на его конвенции и внутренности (которые меняются), хуже вайб-кодятся, дают связанность. Отдельное приложение, дёргающее API, — чище, портируемее, проще поддерживать.

---

## 4. Структура проекта

```
vpn-portfolio/
├── docker-compose.yml
├── .env.example
├── CLAUDE.md                     # короткий контекст, ссылка сюда
├── BUILD_SPEC.md                 # этот файл
├── PLAN.md                       # стратегия
├── backend/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── alembic/                  # миграции
│   └── app/
│       ├── main.py               # FastAPI entry
│       ├── config.py             # Settings (pydantic-settings, читает .env)
│       ├── db.py                 # engine + session
│       ├── models/               # SQLAlchemy: domains, sites, pages, offers, ...
│       ├── schemas/              # Pydantic DTO
│       ├── api/                  # routers: domains, sites, pages, offers, acquisition
│       ├── integrations/         # клиенты внешних API (тонкие, только транспорт)
│       │   ├── metrics.py        # интерфейс MetricsProvider + фабрика
│       │   ├── ahrefs.py         # реализация MetricsProvider (Ahrefs API v3)
│       │   ├── checktrust.py     # альт. реализация MetricsProvider (опц.)
│       │   ├── wayback.py        # проверка истории домена
│       │   ├── rkn.py            # проверка реестра РКН
│       │   ├── backorder.py      # backorder.ru (billmgr)
│       │   ├── optimizator.py    # optimizator.ru (reg над nic.ru/reg.ru)
│       │   ├── cloudflare.py     # зоны, DNS, TXT-верификация
│       │   ├── aapanel.py        # vhost, SSL на VPS
│       │   ├── gsc.py            # Google Search Console
│       │   └── llm.py            # порт 8833
│       ├── services/             # бизнес-логика по модулям
│       │   ├── discovery.py      # M1a: тянуть кандидатов
│       │   ├── scoring.py        # M1b: обогатить + скор
│       │   ├── acquisition.py    # M2: очередь выкупа + ручной гейт
│       │   ├── provisioning.py   # M3: CF + aaPanel
│       │   ├── content.py        # M4: генерация + обогащение + редактура
│       │   ├── publish.py        # M5: деплой + GSC + мониторинг индекса
│       │   └── lifecycle.py      # M6: перформанс, 301, миграции
│       └── workers/              # cron/loop-энтрипойнты
├── panel/                        # frontend (позже)
└── scripts/
    └── smoke.py                  # проверка коннективности всех API
```

**Конвенция:** `integrations/` — только транспорт (auth, запрос, парсинг ответа, ошибки). Вся логика — в `services/`. Так каждый внешний API легко мокать и заменять.

---

## 5. Модель данных (Postgres)

Ключевые таблицы. Типы упрощены; jsonb для гибких полей.

### `domains` — кандидаты и их скоринг
| поле | тип | описание |
|---|---|---|
| id | pk | |
| domain | text unique | |
| source | text | backorder / optimizator / list |
| status | text | discovered / scored / approved / rejected / purchasing / purchased / live / dropped |
| discovered_at | timestamptz | |
| dr | numeric null | domain rating (из metrics provider) |
| referring_domains | int null | |
| backlinks | int null | |
| organic_traffic | int null | |
| anchors | jsonb null | распределение анкоров |
| age_years | numeric null | |
| first_seen | date null | по веб-архиву |
| wayback_checked | bool default false | |
| prior_flags | jsonb null | {adult, pharma, casino, spam, gambling: bool} |
| rkn_listed | bool null | в реестре РКН |
| clean | bool null | итог проверки истории |
| score | numeric null | |
| score_breakdown | jsonb null | как сложился score |
| notes | text null | |

### `acquisition_orders` — попытки выкупа
| поле | тип | описание |
|---|---|---|
| id | pk | |
| domain_id | fk domains | |
| provider | text | backorder / optimizator |
| provider_order_id | text null | id заказа у провайдера |
| status | text | pending_confirm / ordered / caught / failed |
| cost | numeric null | |
| confirmed_by_human | bool default false | **гейт: без true не отправляем** |
| ordered_at | timestamptz null | |
| result | jsonb null | сырой ответ провайдера |

### `sites` — поднятые сайты
| поле | тип | описание |
|---|---|---|
| id | pk | |
| domain_id | fk domains | |
| status | text | provisioning / content / published / monitoring / pruned |
| cf_zone_id | text null | |
| origin_ip | text null | |
| aapanel_site_name | text null | |
| doc_root | text null | |
| niche | text null | угол/ниша |
| template | text null | какой шаблон/донор структуры |
| gsc_verified | bool default false | |
| sitemap_submitted | bool default false | |
| created_at / published_at | timestamptz | |

### `pages` — страницы контента
| поле | тип | описание |
|---|---|---|
| id | pk | |
| site_id | fk sites | |
| url_path | text | |
| title | text | |
| status | text | draft / **edited** / published (published только из edited) |
| body | text null | или путь к файлу |
| index_status | text | unknown / indexed / not_indexed |
| index_checked_at | timestamptz null | |
| created_at / published_at | timestamptz | |

### `offers` — affiliate-офферы (заметка «бренд + промокод + ссылка по странам»)
| поле | тип | описание |
|---|---|---|
| id | pk | |
| brand | text | напр. NordVPN |
| network | text null | партнёрская сеть/программа |
| promo_code | text null | |
| affiliate_link | text | |
| country | text null | ISO-код гео (null = дефолт/глобал) |
| payout_type | text null | CPA / RevShare / hybrid |
| payout_value | text null | |
| active | bool default true | |
| notes | text null | |

> Заводится заранее, до выбора партнёрок. Decoupled от сайтов, гео-aware: под одну страну — свой бренд/промо/ссылка.

### `site_offers` — какие офферы на каком сайте (M:N + гео)
| поле | тип | описание |
|---|---|---|
| site_id | fk sites | |
| offer_id | fk offers | |
| country | text null | гео-таргет размещения |
| placement | text null | где на сайте |

### `index_history` — динамика индексации
| поле | тип | описание |
|---|---|---|
| id | pk | |
| page_id | fk pages | |
| checked_at | timestamptz | |
| index_status | text | |
| coverage_state | text null | сырое из GSC URL Inspection |

---

## 6. Спеки интеграций

> Точные имена полей/эндпойнтов проверять по актуальной доке — здесь достаточно для каркаса. Все клиенты: таймауты, ретраи с backoff, единый формат ошибок, логирование запрос/ответ (без секретов).

### 6.1 MetricsProvider (Ahrefs / альтернатива) — `integrations/metrics.py`
Абстракция, потому что источник метрик — открытое решение и вопрос бюджета.
- Интерфейс: `get_metrics(domain) -> {dr, referring_domains, backlinks, organic_traffic, anchors}`; желательно `get_metrics_batch(domains)`.
- Реализация `ahrefs.py`: Ahrefs API v3, база `https://api.ahrefs.com/v3/`, Bearer-токен. Эндпойнты вида site-explorer (domain-rating, backlinks-stats, refdomains). **Важно: это платный премиум-тариф, тарификация по строкам — легко съесть бюджет. Ahrefs MCP-коннектор в чате Claude ≠ программный доступ для приложения; нужен отдельный API-ключ/подписка.**
- Альтернатива `checktrust.py` (или аналог): дешевле для пакетной проверки доноров; та же сигнатура интерфейса.
- Выбор провайдера — через `.env` (`METRICS_PROVIDER=ahrefs|checktrust`).

### 6.2 Wayback — `integrations/wayback.py`
- Проверка прошлого использования домена: был ли адалт/фарма/казино/гэмблинг/спам.
- CDX API веб-архива: список снапшотов; выборочно тянуть контент старых снапшотов и прогонять по стоп-словам/классификатору.
- **Тяжёлая операция** (много запросов): делать только по кандидатам, прошедшим первичный фильтр по метрикам. Кэшировать результат в `domains.prior_flags`.

### 6.3 РКН — `integrations/rkn.py`
- Проверка, нет ли домена в реестре запрещённых. Источник — выгрузка реестра. Флаг в `domains.rkn_listed`. Заблокированные — авто-reject.

### 6.4 backorder.ru — `integrations/backorder.py`
Поверх billmgr (ISPsystem BILLmanager). Документация: `doc.backorder.ru` (у сайта бот-защита — держать примеры запросов локально).
- **Авторизация:** логин/пароль аккаунта, параметром `authinfo=$login:$password`.
- **Тарифы (нужны для заказа):** тянуть из `https://backorder.ru/manimg/userdata/json/price_ru_backorder.ru.json`. Там `price_id = id`, `period_id = period[0].id`.
- **Заказ (backorder на дроп):**
  `GET https://backorder.ru/manager/billmgr?func=uniservice.order&out=json&period=$period_id&price=$price_id&domainname=$DOMAIN&itype=63&sok=ok&payfrom=account$ACCOUNT_ID&contact=$CONTACT_ID&paynow=on&clientbackorder=yes&authinfo=$login:$password`
  где `payfrom=account` + `ACCOUNT_ID` слитно.
- **Discovery:** сервис отдаёт список доменов, освобождающихся завтра, с ≥1 донором — это вход для M1. (Плюс фильтры РКН/суд/стоп-лист на их стороне, но перепроверяем у себя.)
- Тарифы от 190 ₽. Списание с баланса аккаунта.

### 6.5 optimizator.ru — `integrations/optimizator.py`
Реселлер-слой над nic.ru (RU-CENTER) и reg.ru. Для регистрации **уже освободившихся** доменов (не для перехвата аукциона).
- **Метод reg_domains:**
  `GET http://optimizator.ru/?a=api&sa=reg_domains&api_key=$KEY&nicd=$NICD&domains=$DOMAINS&enc=utf8`
  - `api_key` (обяз.), `nicd` (номер аккаунта RU-CENTER, обяз.), `domains` (через пробел, до 30), `enc` (utf8/cp1251).
  - Ответ: `[{"order_id": N}]`. Статус заказа — отдельным методом (`texts/16.html`).

### 6.6 Cloudflare — `integrations/cloudflare.py`
API v4, база `https://api.cloudflare.com/client/v4/`, Bearer-токен (scoped: Zone.DNS edit).
- Добавить зону: `POST /zones` (account_id, name=domain).
- DNS: `POST /zones/{zone_id}/dns_records` — A-запись на origin-IP VPS, `proxied=true` (маскировка origin).
- Верификация GSC: `POST /zones/{zone_id}/dns_records` TXT-записью (см. 6.8) — автоматизирует подтверждение прав в Search Console.

### 6.7 aaPanel — `integrations/aapanel.py`
База `https://$HOST:8888`. Включить API в настройках + IP-whitelist (добавить IP приложения; `127.0.0.1` если тот же хост).
- **Авторизация каждого запроса:** `request_time` = unix-время; `request_token = md5(request_time + md5(api_sk))`; слать оба как POST-поля. **Сохранять и переотправлять cookie** между запросами. Ответы — JSON.
- Ключевые действия: создать сайт (домен, docroot, PHP-версия или статик), выпустить/привязать SSL (Let's Encrypt), удалить/остановить сайт (для M6).
- Ориентир по покрытию методов — PHP-либа `AzozzALFiras/aapanel-api` (350+ методов, 27 модулей); нам нужен тонкий Python-клиент, покрывающий подмножество: system(check), website(add/list/delete), ssl(apply).
- Эндпойнты в двух видах: legacy action-based и `/v2/data?action=...&table=sites`. Использовать актуальные из доки; список сайтов — `table=sites`.

### 6.8 Google Search Console — `integrations/gsc.py`
- Авторизация: service account (JSON-ключ), домен добавляется как property.
- **Верификация прав:** TXT-запись через Cloudflare API (6.6) — автоматизируемо.
- **Сабмит sitemap:** `PUT https://www.googleapis.com/webmasters/v3/sites/{siteUrl}/sitemaps/{feedpath}`.
- **Мониторинг индексации:** URL Inspection API — `POST https://searchconsole.googleapis.com/v1/urlInspection/index:inspect` → пишем `pages.index_status` + строку в `index_history`. Учитывать квоты (лимит инспекций/день) — чекать пачками, не всё сразу.

### 6.9 LLM (порт 8833) — `integrations/llm.py`
- Кастомный эндпойнт для Mistral/Claude. **Уточнить формат** — вероятно OpenAI-совместимый (`POST $LLM_BASE_URL/v1/chat/completions`). Клиент с настраиваемым `base_url`, `model`, форматом.
- Использование в M4: генерация черновика по структуре + промпту, обогащённому реальными данными вертикали. Разделить system-промпт (роль/тон/структура) и данные. Не гнать всю историю — только нужный контекст на страницу (контроль стоимости).

---

## 7. Помодульная логика (services/)

**M1 — Domain Intelligence** (`discovery.py` + `scoring.py`)
1. `discovery`: тянуть кандидатов из backorder (освобождаются завтра, ≥1 донор) [+ опц. другие источники] → upsert в `domains` (status=discovered).
2. `scoring`: для новых — первичный фильтр по метрикам (MetricsProvider) → отсечь слабых → по прошедшим прогнать Wayback (`prior_flags`) + РКН (`rkn_listed`) → вычислить `score` + `score_breakdown` → status=scored. Грязные/в РКН → status=rejected. Порог/веса — конфиг.
   - Итог модуля: API-эндпойнт `GET /domains?status=scored&min_score=...` + вид в панели. **Полезно само по себе.**

**M2 — Acquisition** (`acquisition.py`)
- Из approved-доменов формировать `acquisition_orders` (status=pending_confirm). **Отправка провайдеру только при `confirmed_by_human=true`.** По подтверждению — вызвать backorder/optimizator, сохранить `provider_order_id`, отслеживать статус. Успех → `domains.status=purchased`.

**M3 — Provisioning** (`provisioning.py`)
- Вход: purchased-домен. Шаги (идемпотентно, каждый переигрываем): CF зона → A-запись (proxied) на origin-IP → aaPanel vhost + docroot → origin-SSL (если нужно). Записать `cf_zone_id`, `aapanel_site_name`, `doc_root`. status=content.

**M4 — Content Pipeline** (`content.py`)
- Скаффолд структуры сайта (можно от донора/конкурента из выдачи) → генерация через LLM с обогащением данными вертикали → создать `pages` (status=draft) → **чекпоинт ручной редактуры: draft→edited** (без него дальше нельзя) → простановка офферов из `offers`/`site_offers` с disclosure.

**M5 — Publish & Monitor** (`publish.py`)
- Только `edited`-страницы: деплой файлов на docroot → GSC verify (TXT через CF) → сабмит sitemap → status=published. Затем периодический URL Inspection → `index_status` + `index_history`. Ранний сигнал проблем.

**M6 — Lifecycle** (`lifecycle.py`, не в MVP)
- Трекинг перформанса по сайтам → отбраковка неудачных: 301 на рабочий (через aaPanel/nginx rewrite) → status=pruned → миграции.

---

## 8. Порядок сборки (для Claude Code)

**Фаза 0 — каркас.**
1. Репозиторий, `docker-compose.yml` (`db` + `backend`), `Dockerfile`, `requirements.txt`.
2. `config.py` (Settings из `.env`), `db.py`, базовые `models/` + первая миграция Alembic (все таблицы из §5).
3. `scripts/smoke.py`: по одному минимальному вызову к каждому внешнему API (metrics, backorder, optimizator, cloudflare, aapanel, gsc, llm) с проверкой авторизации → печатать OK/FAIL. **Не идти дальше, пока не позеленеет.**

**Фаза 1 — MVP.**
4. **M1 целиком** (discovery + scoring + API-эндпойнты + минимальный список в панели). Первый полезный результат.
5. **Петля на ОДНОМ домене (M3→M4→M5)**, покупка — руками. Провижн → генерация с редактурой → публикация → увидеть индексацию.
6. **Критерий готовности MVP:** скоринг выдаёт вменяемый шортлист; один реальный сайт поднят системой, с отредактированным контентом, опубликован, страницы в индексе.

**Дальше — Фазы 2–4 по `PLAN.md`.**

---

## 9. Переменные окружения (`.env.example`)

```
DATABASE_URL=postgresql+psycopg://user:pass@db:5432/portfolio

METRICS_PROVIDER=ahrefs
AHREFS_API_KEY=
CHECKTRUST_API_KEY=

BACKORDER_LOGIN=
BACKORDER_PASSWORD=
BACKORDER_ACCOUNT_ID=
BACKORDER_CONTACT_ID=

OPTIMIZATOR_API_KEY=
OPTIMIZATOR_NICD=

CLOUDFLARE_API_TOKEN=
CLOUDFLARE_ACCOUNT_ID=

AAPANEL_URL=https://HOST:8888
AAPANEL_API_KEY=
VPS_ORIGIN_IP=

GSC_SERVICE_ACCOUNT_JSON=/secrets/gsc.json

LLM_BASE_URL=http://HOST:8833
LLM_API_KEY=
LLM_MODEL=
```

---

## 10. Конвенции и non-goals

- **Два ручных гейта обязательны** (это принцип из PLAN §2 в коде): публикация только из `pages.status=edited`; выкуп только при `confirmed_by_human=true`. Никакого авто-паблиша неотредактированного AI и авто-траты денег.
- **DNS только через Cloudflare** (маскировка origin). aaPanel-DNS не использовать.
- **Сайты независимые.** Никакой авто-перелинковки между сайтами портфеля ради ссылочного веса (footprint).
- **Идемпотентность** провижна: любой шаг безопасно переигрывать.
- **Рейт-лимиты и стоимость:** беречь квоты metrics-провайдера (деньги!), Wayback (тяжесть), GSC (лимит инспекций). Пакетно, с backoff.
- **Секреты — только в `.env`**, не в коде и не в git.
- **integrations = транспорт, services = логика.** Внешние API за интерфейсами, чтобы мокать и заменять (особенно MetricsProvider).
- **Логировать** запрос/ответ внешних API без секретов — для отладки интеграций.
```
