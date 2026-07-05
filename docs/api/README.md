# API-референсы интеграций

Собрано 2026-07-05 из официальных доков/источников (веб-ресёрч + живые проверки где возможно).
Каждый файл — implementation-ready: base URL, auth, эндпойнты с примерами запрос/ответ, лимиты,
готчи, открытые вопросы, ссылки на источники. Неподтверждённое помечено **UNVERIFIED** внутри.

Это референс под реализацию `integrations/` (транспорт). Логика — в `services/` (см. [BUILD_SPEC](../../BUILD_SPEC.md)).

## Карта: сервис → модуль → доступ

| Референс | Модуль | Что даёт | Доступ / креды | Стоимость | Увер. |
|---|---|---|---|---|---|
| [backorder.md](backorder.md) | M1 discovery + M2 выкуп | дроп-лист (`links=N` доноров, `tomorrow=1`) + backorder-заказ | **discovery — без auth**; заказ — login/pass | лист free; домен от ~190₽ | высокая (gap: имена тонких фильтров) |
| [openpagerank.md](openpagerank.md) | M1 скоринг (DR-прокси) | Open PageRank 0–10 (замена Ahrefs DR) | free API-ключ `API-OPR` | free (лимит уточнить) | эндпойнт высокая; лимит средняя |
| [wayback.md](wayback.md) | M1 скоринг (история) | снапшоты → prior_flags, topic-switch, возраст | без ключа | free (тяжело/вежливо) | высокая |
| [rkn.md](rkn.md) | M1 скоринг (hard-reject) | в реестре РКН? | без ключа (дамп/список) | free | средняя (источник устарел — см. ниже) |
| [blacklist.md](blacklist.md) | M1 скоринг (hard-flag) | Spamhaus DBL / SURBL | **нужен свой резолвер** или DQS-ключ | free до лимита | высокая (коды); средняя (лимиты) |
| [optimizator.md](optimizator.md) | M2 выкуп (свободные) | регистрация освободившихся (nic.ru/reg.ru) | `api_key` + `nicd` | рег-сбор | высокая (gap: failure-стейты) |
| [cloudflare.md](cloudflare.md) | M3 провижн | зона → NS → proxied A → SSL | scoped Bearer-токен | free-tier | высокая |
| [aapanel.md](aapanel.md) | M3 провижн | vhost + Let's Encrypt SSL | api_sk (md5-токен) + IP-whitelist | есть | auth/site высокая; SSL средняя |
| [llm.md](llm.md) | M4 контент | генерация (LiteLLM: mistral-large + ollama) | base_url, без ключа | mistral платно / ollama free | **подтверждён** (LiteLLM :4000) |
| [searxng.md](searxng.md) | M1 indexed_echo + M4 SERP | бесплатная выдача (`site:`, конкуренты) | без ключа | **free** | **подтверждён** (:8080 JSON) |
| [aparser.md](aparser.md) | M2 whois + M1 SERP/DR/история + M4 keywords | Net::Whois, SE::Google/Yandex/DDG, Rank::Ahrefs/MOZ, Check::RKN, WordStat | API-пароль `APARSER_API_KEY` | **free** (локально) | **подтверждён** (:9091, ping+info live) |

GSC (M5-мониторинг) — **исключён из v1** по решению пользователя; при возврате к автомониторингу
рассмотреть Bing Webmaster Tools API как бесплатную замену.

## Локальная инфра — бокс 192.168.1.77 (проверено вживую 2026-07-05)

Просканирована подсеть; Docker API (2375) закрыт → контейнеры напрямую не перечислить (нужен SSH/expose).
Достижимого MCP-эндпойнта не найдено. Но по открытым портам — богатый **бесплатный** стек:

| Порт | Сервис | Нам |
|---|---|---|
| 4000 | **LiteLLM** (OpenAI-совм. шлюз) | движок M4: `mistral` (=mistral-large, платно у них) + `ollama/*` (free). Без ключа. → [llm.md](llm.md) |
| 8080 | **SearXNG** (JSON включён) | **free SERP**: M1 `indexed_echo` + M4 конкуренты. Без ключа. → [searxng.md](searxng.md) |
| 11434 | **Ollama** 0.31.1 · `qwen3.6:35b-a3b` (36B MoE, ctx 262k) | локальный LLM (free), уже под LiteLLM |
| 9091 | **A-Parser** v1.2.2940 (137 парсеров) | whois (M2), SERP-фолбэк, keywords, coarse-DR — **API работает** (пароль в `.env` `APARSER_API_KEY`; `apollo11` был неточен). → [aparser.md](aparser.md) |
| 3000 | **Browserless** (headless Chrome, REST/WS) | JS-рендер/скриншоты для API-less задач (в рантайме избегаем — SERVICES.md — но есть) |
| 5678 | **n8n** | оркестрация воркфлоу (опция автоматизации/шедулинга) |
| 8811 | неопознан (TCP открыт, не HTTP) | ? |
| 2375 | Docker API | **закрыт** → удалённый `docker ps` недоступен |

**Влияние на план:** два open-вопроса PLAN §9 закрыты локально и бесплатно — движок контента (LiteLLM
`mistral-large`, качество по §2) и SERP-провайдер (SearXNG). Платный DataForSEO/SerpApi под M4 больше не нужен.

## Кросс-находки, влияющие на план

1. **Discovery не требует кредов.** `https://backorder.ru/json/?ext=1&disp=1&tomorrow=1&links=1&by=links&order=desc`
   отдаёт дроп-лист публично, без auth. → **M1 можно строить и тестить прямо сейчас, за ноль, без аккаунтов.**
2. **Часть скоринг-сигналов уже в фиде.** backorder-JSON несёт `links` (кол-во доноров), `yandex_tic`,
   `rkn`, `judicial`, `block`. Их берём как дешёвый пре-фильтр, но по принципу проекта — **перепроверяем сами**.
3. **Spamhaus нельзя через публичные резолверы** (8.8.8.8/1.1.1.1 отдают коды-ошибки). Нужен свой
   `unbound`/`bind` в compose-стеке, либо free DQS-ключ. Заложить в инфру.
4. **Основной РКН-дамп `zapret-info/z-i` заморожен с 2025-10-01** (~9 мес). Первичный источник —
   `antizapret.prostovpn.org/domains-export.txt` (UTF-8), z-i как конфигурируемый фолбэк со stale-guard.
   cp1251 + IDN `.рф` через `idna`.
5. **OpenPageRank куплен Keywords Everywhere** — точные лимиты free-тарифа плавают → **проверить живым ключом** до того, как завязывать пре-фильтр.
6. **NS-шаг у регистратора внешний и асинхронный.** CF отдаёт `name_servers[]` при создании зоны → их
   надо прописать у регистратора (reg.ru/nic.ru API) → дождаться `status=active` → только потом proxied-A.
   SSL: сначала `full`, потом `strict` (нужен валидный origin-cert от aaPanel LE или CF Origin CA).

## Открытые вопросы (подтвердить перед/во время реализации)

- **backorder:** точные имена тонких фильтров экспорта (зона/регистратор/возраст) — неизвестные
  параметры молча игнорируются; JSON-обёртка authed-ответов — сверить на первом реальном заказе.
- **aaPanel:** SSL `apply_cert_api` (`auth_to` = id или name сайта?) — на живой панели.
- **optimizator:** failure-стейты `check_order` (только `completed` задокументирован) + условие перевода
  анкеты RU-CENTER под управление (`check_nicd`).
- ~~A-Parser (192.168.1.77:9091)~~ — **закрыт:** API работает (v1.2.2940, 137 парсеров перечислены). Формат был верным; ломался сам пароль — `apollo11` неточен (регистр + пунктуация), рабочее значение в `.env` `APARSER_API_KEY`. Есть `Net::Whois` (M2-гейт), SE::Google/Yandex/DuckDuckGo (SERP-фолбэк), Rank::Ahrefs/MOZ/Majestic (coarse-DR), Check::RosKomNadzor/Rank::Archive (история). → [aparser.md](aparser.md).
- ~~LLM 8833~~ — **закрыт:** движок = LiteLLM `192.168.1.77:4000` (`mistral`/`ollama`, без ключа).
- ~~SERP-провайдер~~ — **закрыт:** SearXNG `192.168.1.77:8080` (free).
- **OpenPageRank:** актуальный free-лимит после миграции в KE.
