# SearXNG — бесплатный SERP (self-hosted мета-поиск)

> **ПОДТВЕРЖДЕНО вживую 2026-07-05** на локальном боксе `http://192.168.1.77:8080`.
> JSON-API включён. Агрегирует Google/Bing/DuckDuckGo/др. → **бесплатная замена платному SERP-API**
> (DataForSEO/SerpApi) без CAPTCHA и без сырого скрейпинга Google. Снимает open-вопрос из PLAN §9.

## Назначение в проекте
- **M1 `indexed_echo`:** `site:домен` — есть ли старый контент домена в индексе (прокси «не деиндексирован/не под баном»).
- **M4 конкурент-анализ:** выдача по целевым запросам гео/языка → структура/англы конкурентов (без браузера).
- **M4 keywords (частично):** подсказки/связанные запросы (в зависимости от включённых движков).

## Эндпойнт (подтверждён)
```
GET http://192.168.1.77:8080/search?q=<query>&format=json
```
Параметры (основные):
| Параметр | Значение | Заметка |
|---|---|---|
| `q` | строка запроса | поддерживает `site:domain`, обычные операторы |
| `format` | `json` | включён на этом инстансе (иначе 403) |
| `language` | напр. `ru-RU`, `en-US` | таргет гео/языка оффера |
| `pageno` | 1..N | пагинация |
| `engines` | напр. `google,bing` | ограничить источники |
| `categories` | напр. `general` | — |
| `time_range` | `day/week/month/year` | опц. |

## Пример ответа (сокращён)
```json
{
  "query": "site:pcmag.com vpn",
  "number_of_results": 0,
  "results": [
    {"title": "The Best VPNs...", "url": "https://www.pcmag.com/picks/...",
     "content": "...", "engine": "duckduckgo",
     "parsed_url": ["https","www.pcmag.com","/picks/...","",""]}
  ]
}
```
Берём: `results[].url` / `.title` / `.content` / `.engine`. **Готч:** `number_of_results` часто `0` (счётчик-квирк SearXNG) —
ориентироваться на длину `results[]`, а не на это поле.

## `indexed_echo` — как считать
`q=site:<domain>&format=json&language=<lang>` → если `results[]` содержит URL с этого домена → `indexed_echo=true`.
Пусто → `false` (осторожно: пусто может значить и «движок не отдал», не только деиндекс — не делать hard-reject только по этому).

## Лимиты / готчи
- Инстанс локальный, без auth на LAN — но SearXNG сам upstream-рейт-лимитится движками; не долбить пачками параллельно, добавить паузы/очередь.
- Набор движков и включённость `json` заданы в конфиге SearXNG на боксе — если запрос падает 403/пусто по конкретному движку, смотреть его `settings.yml`.
- `site:` в примере отработал через DuckDuckGo-движок — Google-движок у SearXNG периодически отваливается (rate-limit), полагаться на мульти-движок.
- IP `192.168.1.77` — в `.env`, не хардкодить (`SEARXNG_URL`).

## `ping()` (для smoke.py)
`GET {SEARXNG_URL}/search?q=test&format=json` → 200 + JSON с ключом `results`.

## Config (`.env`, добавить)
```
SEARXNG_URL=http://192.168.1.77:8080
```

## Источники
Проверено вживую: `/search?q=best+vpn&format=json` и `/search?q=site:pcmag.com+vpn&format=json`.
SearXNG search API: https://docs.searxng.org/dev/search_api.html
