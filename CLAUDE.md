# CLAUDE.md — начни отсюда

Стартовый скелет проекта **VPN Affiliate Portfolio** — машины полного цикла для
портфеля VPN affiliate-сайтов. Ты (Claude Code) подхватываешь проект с этого каркаса.

## Порядок чтения
1. Этот файл — правила, состояние, что делать первым.
2. `docs/PIPELINE.md` — вся логическая цепочка, диаграмма, state machine, гейты, пути отказа.
3. `docs/DONORS.md` — методика оценки доменов/доноров (ядро скоринга).
4. `docs/SERVICES.md` — какие сервисы нужны, где браузер/SERP-API/MCP.
5. `BUILD_SPEC.md` — детальный тех-бриф (схема БД, спеки API, помодульная логика).
6. `PLAN.md` — стратегия и фазы.
7. `docs/api/README.md` — живые референсы интеграций (endpoints/auth/примеры) + локальная инфра 192.168.1.77.

## Что за проект (в одном абзаце)
Пользователь описывает офферы (бренд, промокод, ссылка, гео, язык). Система: находит
освобождающиеся домены → **скорит их качество и чистоту истории** → выкупает (с ручным
подтверждением) → поднимает на Cloudflare + aaPanel → пишет и публикует контент под
гео/язык через LLM → мониторит индексацию → ведёт жизненный цикл. Замкнутый цикл, 6 модулей.

## Два жёстких правила, зашитых в код (НЕ обходить)
На них держится жизнеспособность (см. PLAN.md §2):
1. **Гейт редактуры:** страница публикуется ТОЛЬКО из статуса `edited`. Никакого авто-паблиша
   неотредактированного AI. `draft -> edited` делает человек.
2. **Гейт выкупа:** заказ уходит провайдеру ТОЛЬКО при `confirmed_by_human=true`. Деньги не тратятся на автопилоте.

## Требования к качеству (инварианты в коде)
- Сайты **тематически связные**: контент соответствует офферу и цели ссылки. Никаких дорвеев.
- Сайты **независимы**: без авто-перелинковки между сайтами портфеля ради веса (footprint).
- Домены — за **чистую историю** (не адалт/фарма/казино/спам в прошлом; не в РКН; не в блэклистах),
  а не за «сок». Проверка истории — обязательный шаг скоринга (см. docs/DONORS.md).
- Контент = AI-черновик + реальные данные вертикали (замеры/сравнения) + ручная редактура + disclosure.

## Архитектура (6 модулей, services/)
- **M1 Domain Intelligence** — discovery из 4 бесплатных источников (backorder-фид с RD/флагами + cctld.ru сырой реестр + reg.ru/sweb витрины через A-Parser fetch_html; дедуп по домену, больший RD выигрывает) + **ступенчатая воронка скоринга дёшево→дорого с ранним выходом**: T0 RD/флаги фида → T1 whois-возраст (A-Parser Net::Whois) → T2 РКН/Spamhaus/indexed_echo → T3 Wayback-история ТОЛЬКО для выживших → скор. Причина отказа — `Domain.reject_reason` (low_rd|feed_flag|too_young|rkn|blacklist|history_dirty|not_acquirable|low_score). Пороги (min RD, min возраст, approve/manual) — рантайм, single-row `scoring_settings`, экран `/settings` с превью-счётчиками. DR-прокси OpenPageRank отпал, см. ниже; платный ссылочный профиль — опц. стадия.
- **M2 Acquisition** — очередь выкупа + ручной гейт. Ценные дропы → backorder (ставка); свободные чистые → optimizator (гарантия).
- **M3 Provisioning** — Cloudflare (зона → **смена NS у регистратора** → DNS proxied) + aaPanel (vhost + origin-SSL). Идемпотентно.
- **M4 Content** — генерация под гео/язык через LLM (LiteLLM `192.168.1.77:4000`: mistral-large/ollama) + обогащение + опц. структура от конкурента из SERP (SearXNG) + гейт редактуры + вставка офферов.
- **M5 Publish & Monitor** — деплой + проверка индексации (v1: ручной `site:` через SearXNG; GSC исключён, позже — Bing WMT).
- **M6 Lifecycle** — перформанс, отбраковка с 301, миграции. (Не в MVP.)

Роль внешних кусков: **aaPanel** — движок провижна по API (не фреймворк панели). **Cloudflare** —
DNS + маскировка origin (NS домена переключаем на CF). **LLM** — LiteLLM `192.168.1.77:4000` (OpenAI-совм., mistral-large + ollama, без ключа). **SearXNG** `:8080` — free SERP (indexed_echo, конкуренты). Всё локально на боксе 192.168.1.77 (см. `docs/api/README.md`).

## Стек
Python 3.12 + FastAPI + SQLAlchemy 2.x + Pydantic v2; PostgreSQL 16; Alembic; httpx; Docker Compose.
Панель для MVP минимальная (FastAPI+HTMX хватит). Форма управления свободная: Docker+Telegram-бот
или веб-панель — на твоё усмотрение, главное надёжность пайплайна.

## Dev Commands
```bash
# Start locally
docker compose up --build

# Run tests (offline, SQLite-harness, no network)
docker compose run --rm backend pytest backend/tests/ -q

# Run single test
docker compose run --rm backend pytest backend/tests/test_name.py::test_case -v

# Lint (pyflakes on app & tests)
.venv/bin/python -m pyflakes backend/app backend/tests

# Smoke test (check all integrations)
docker compose run --rm backend python backend/scripts/smoke.py
```

**Note:** Tests are hermetic (autouse-fixture blocks network). Live box testing on `192.168.1.77`.

## Текущее состояние (2026-07-09)
**Ahrefs DR/backlinks/referring-domains** (2026-07-09): Fully shipped — `AParserClient.ahrefs_probe()` 
(Rank::Ahrefs + RuCapcha Turnstile), DR weight added to scoring (authority=0.12, rebalanced all weights 
to sum=1.0), runtime budget cap `max_ahrefs_per_run` (default 50, 0=disabled). All 207 tests green, 
pyflakes clean, 0 Critical/Important findings. → `docs/superpowers/specs/2026-07-08-ahrefs-dr-design.md`.

**Dev agents & tools** (2026-07-09): `.claude/agents/combine-reviewer.md` created — project-aware ревьюер 
for per-task + whole-branch reviews, checks 8 hard invariants + design contract + test hygiene (read-only, 
opus-model). Smoke-tested on real diff `d6e69c2` (polish after Ahrefs). New plugins installed: 
oh-my-claudecode (OMC) + ecc (Extensible Claude Code); `ecc:code-reviewer` recommended for code quality 
checks. **Ponytail mode active** (full level).

**M1 воронка + операционка панели** (14 коммитов 2026-07-06, subagent-driven,
каждая задача прошла спек+качество ревью, финальное whole-branch ревью «Ready to merge»; 207/207 тестов):
- **Воронка T0–T3** (см. M1 выше): регрессия доказывает, что отсеянный на T0–T2 домен НЕ доходит до
  Wayback (`wb.calls == 0`); рантайм-пороги `/settings` реально управляют статусом (`_decide()` в
  scoring.py — та же логика для дефолтов и рантайма, downgrade-гарды сохранены).
- **`/settings`**: ползунки порогов + чекбоксы источников + живые счётчики «сколько пройдёт» (зеркалят
  воронку: NULL-RD проходит, как в T0). Миграция `0002_funnel` (alembic накатывается кнопкой git-pull).
- **Прогресс длинных задач**: Discovery/Score в фоне (`services/jobs.py`, in-memory реестр, double-start
  отклоняется), панель поллит `GET /run/{job}/progress`; ошибка джоба видна и после перезагрузки страницы.
- **Версия из git**: блок в `/diag` (`services/version.py`, git из `/repo` в контейнере), после
  git-pull баннер old→new (или честное «Уже свежая»), кнопка «проверить обновления» (ls-remote,
  токен только через extraheader-env, в баннерах скрабится).
- Тесты герметичны структурно: autouse-фикстура режет источники до backorder-only (живая сеть в
  тестах невозможна по умолчанию); pyflakes чистый. Дизайн проверен глазами (Playwright-скриншоты).
- НЕ проверено вживую (снять на первом прогоне): формат whois-ответа A-Parser на реальных TLD,
  разметка витрин cctld/reg.ru/sweb.

Ранее (2026-07-05): панель работает, петля M3→M4→M5 прогнана на моках (aaPanel вживую); оба гейта
держат. Полный аудит кода пройден (2026-07-05, 4 ревью-домена):
закрыты spam-история в auto-approve, «пустой» Wayback как чистый, тихое отключение RKN/Spamhaus,
рассинхрон оффера генерация↔публикация, застревание домена в purchasing, неатомарный execute,
CSRF на панели (same-origin guard) — детали в трёх аудит-коммитах от 2026-07-05. Сверх MVP
уже сделано и проверено вживую:
- **M4 information gain (§2):** `services/vertical_data.py` — датасет реальных VPN-фактов (7 брендов),
  подмешивается в генерацию; `services/competitor.py` — структура тем от топ-конкурента через **A-Parser**
  (SE::Google → Net::HTTP, ходит через прокси; Browserless в рантайме НЕ используем). Проверено на LiteLLM/A-Parser бокса.
- **M2 очередь выкупа:** `services/acquisition.py` + экран `/queue` — create→confirm(человек)→execute с
  жёстким денежным гейтом; живой заказ у провайдера ждёт login-кредов (execute честно репортит failed).
- **Auth:** Basic-auth на панель (`PANEL_USER`/`PANEL_PASS` в .env; пусто=выкл) — закрывает LAN-экспозицию.

HTML-панель — `app/api/panel.py` + `app/templates/`; JSON-двойник под `/api`. `/diag` пингует интеграции +
кнопка «Обновить из git» (`POST /admin/pull`, нужен GITHUB_TOKEN). Деплой: **бокс = Windows + Docker Desktop**,
репо `D:\combine_machine`, панель на LAN `http://192.168.1.77:8000/` (защищать Basic-auth; детали docs/DEPLOY.md).

## Что делать дальше

**Ближайшее (блокирует MVP):**

1. **Спека 4 — LLM-критик редактуры** — автоматическая оценка качества контента перед гейтом 
   (может заменить/дополнить человеческую редактуру, но гейт НИКОГДА не убирается).

2. **Тред D — дешёвые критерии скоринга** — SE::Google::SafeBrowsing, Rank::Archive, 
   SecurityTrails, SERP-fallback через SE::Google/Yandex (live тесты A-Parser форматов).

3. **Первый прогон воронки на боксе:**
   - Обновить бокс: `git pull --ff-only origin main` (кнопка git-pull в `/diag`)
   - `/settings` выставить пороги → Discovery → Score → разобрать `/domains` с reject_reason
   - Live-разметка cctld/reg.ru/sweb проверяется впервые (смотреть docker-логи при ошибках)

4. **Первый домен через полную петлю** (M1→M5):
   - Ручной покуп одобренного дропа → карточка сайта (M3 provisioning)
   - M4 контент-генерация → гейт редактуры (человек)
   - M5 публикация + проверка индексации (`site:` через SearXNG)
   - Критерий MVP: сайт поднят системой, контент отредактирован, опубликован, в индексе

**Блокеры кред:** `CLOUDFLARE_API_TOKEN` + `CLOUDFLARE_ACCOUNT_ID` в `.env` бокса; 
`backend/aapanel.pem` на боксе (gitignored).

**Infrastructure & Tooling:**
- `combine-reviewer` встроить в SDD-pipeline (`subagent-driven-development`)
- Smoke-test на боксе: `/diag` доступен, все интеграции пингуются
- Опционально: smoke-команда `/smoke` (в `.claude/commands/smoke.md`)

## Панель: дизайн
**Светлая современная CMS** — по фидбеку пользователя тёмный/индустриальный стиль НЕ используем.
Тёплая бумага + белые карточки, левый сайдбар, один оранжевый акцент; шрифты Golos Text (UI) /
JetBrains Mono (данные) / Unbounded (лого) — Google Fonts с фолбэком на системные. Принцип «шильдика»:
каждый контрол подписан ровно тем, что он делает (станции с описаниями, легенды статусов, полные
подписи на кнопках, title-подсказки). Весь визуальный слой — в `templates/base.html` (держать
CSS-контракт классов); контент-шаблоны — только семантика. Правки дизайна проверять глазами:
локальный serve + Playwright-скриншоты всех экранов.

## Открытые вопросы — статус на 2026-07-05 (детали в `docs/api/README.md`)
**Решено:**
- **LLM:** LiteLLM `192.168.1.77:4000` — `mistral`(=mistral-large, качество §2) + `ollama/*`(free), OpenAI-совм., **без ключа**. → `integrations/llm.py`.
- **Метрики:** free-путь — Wayback (история) + РКН + Spamhaus + RD из фида backorder (авторитетность). OpenPageRank DR-прокси **отпал** (free-регистрация закрыта после покупки Keywords Everywhere, 2026) → DR informational-only, вес=0. Платный Ahrefs/DataForSEO/KeywordsEverywhere — опц. стадия позже.
- **SERP/keyword:** SearXNG `192.168.1.77:8080` (free, локально) [+ A-Parser опц.]. Платный SERP-API не нужен.
- **Локализация:** один домен = одно гео/язык (дефолт, чище footprint).

**Ещё открыто:**
- Креды для полной петли (НЕ блокируют старт M1): Cloudflare token, aaPanel api_sk, backorder login (для выкупа), свой DNS-резолвер для Spamhaus. (OpenPageRank-ключ больше не получить — free-регистрация закрыта; DR из скоринга исключён.)
- Бизнес: VPN-партнёрки, углы/ниши, фреймворк панели.

## Конвенции
- `integrations/` = только транспорт; логика в `services/`.
- Секреты только в `.env`. Провижн идемпотентен. Беречь квоты/деньги (OpenPageRank free-лимит, Wayback вежливость, LLM-токены на mistral), httpx+backoff.
- Браузер в рантайме не используем (см. docs/SERVICES.md); SERP — через **SearXNG** (self-hosted мета-поиск), не сырой скрейпинг Google.
- Spamhaus DBL/SURBL — только через свой резолвер (публичные 8.8.8.8/1.1.1.1 блокируются). РКН — antizapret (z-i заморожен).
- **Доки библиотек при коде/дебаге — через context7** (MCP `plugin:context7:context7`): для вопросов по
  FastAPI/SQLAlchemy/Pydantic/httpx/Alembic и любым внешним API сначала свериться со свежей докой,
  не полагаться на память. Живой UI-дебаг — Playwright/chrome-devtools плагины.
- Бокс — Windows (PowerShell): в командах для пользователя не использовать `~`, `#`-комментарии в
  одной строке с командой и unix-пути; репо на боксе — `D:\combine_machine`.
