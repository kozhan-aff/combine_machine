# LLM — референс интеграции (LiteLLM)

> **ПОДТВЕРЖДЕНО вживую 2026-07-05.** Движок контента (M4) — **LiteLLM**, OpenAI-совместимый шлюз,
> на локальном боксе `http://192.168.1.77:4000`. Не placeholder «порт 8833» — тот на этом хосте не поднят.
> Клиент держим с настраиваемым `base_url`/`model` (эндпойнт может переехать/сменить IP).

## Назначение
Генерация контента в **M4** (`services/content.py`): черновик страницы по структуре + промпту,
обогащённому данными вертикали. Основной операционный расход — токены (см. PLAN §2: качество важнее).

## Что за сервер
**LiteLLM proxy** — фронтит несколько бэкендов под единым OpenAI-совместимым API. Доступные модели
(`GET /v1/models`, отдаётся **без ключа**):
- `mistral` → `mistral/mistral-large-latest` — **Mistral Cloud, платно** (биллится на их Mistral-аккаунт), ctx 262k. **Путь качества** (PLAN §2).
- `mistral-small` → Mistral Cloud, дешевле.
- `ollama/*`, `ollama/llama2` → **локальная Ollama** (:11434, модель `qwen3.6:35b-a3b`) — **бесплатно**, путь чернового объёма/резерва.

Рекомендация: чистовой контент — `mistral` (качество, но платно у них); массовые черновики/эксперименты — `ollama/*` (free локально).

## Config (`.env`)
```
LLM_BASE_URL=http://192.168.1.77:4000
LLM_API_KEY=            # ПУСТО — на текущем инстансе auth не требуется
LLM_MODEL=mistral       # или mistral-small | ollama/<model>
```

## Auth
На текущем инстансе LiteLLM **ключ не нужен** (`/v1/models`, `/v1/chat/completions`, `/health` отвечают без токена —
проверено). Если позже включат master-key — добавить `Authorization: Bearer $LLM_API_KEY`. Клиент шлёт заголовок,
только если `LLM_API_KEY` непустой.

## Основной эндпойнт (OpenAI chat completions — ПОДТВЕРЖДЁН)
```
POST http://192.168.1.77:4000/v1/chat/completions
Content-Type: application/json
```
Тело:
```json
{
  "model": "mistral",
  "messages": [
    {"role": "system", "content": "<роль/тон/структура — стабильная часть>"},
    {"role": "user",   "content": "<данные страницы + инструкция>"}
  ],
  "temperature": 0.7,
  "max_tokens": 2048,
  "stream": false
}
```
Реальный ответ (сокращён):
```json
{
  "id": "708da137...", "model": "mistral", "object": "chat.completion",
  "choices": [{"finish_reason": "stop", "index": 0,
               "message": {"role": "assistant", "content": "<текст>"}}],
  "usage": {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14}
}
```
Извлекаем `choices[0].message.content`; логируем `usage.total_tokens` (учёт расхода, особенно для платного `mistral`).

## Контроль стоимости
- Разделять system-промпт (стабильный) и данные страницы (переменные).
- Только контекст текущей страницы, без всей истории.
- Логировать `usage` на вызов → видимость в панели. Отдельно считать платный `mistral` vs free `ollama/*`.
- Тайм-ауты: облачный `mistral` быстрый; локальная `ollama` на длинных текстах медленная (60–300 c). Retry с backoff, но
  повтор длинной генерации удваивает расход.

## `ping()` (для smoke.py)
`GET {LLM_BASE_URL}/v1/models` → 200 + `data[]`. Не тратит токены. (Есть и `GET /health` → 200.)

## Прочие эндпойнты LiteLLM (полезное)
- `GET /model/info` — какие модели и на какой бэкенд мапятся (ctx-лимиты и т.п.).
- `GET /health` — живость.
- LiteLLM также умеет OpenAI-совместимые `/v1/embeddings` (если понадобятся эмбеддинги под похожесть контента).

## Готчи
- `mistral` тратит **чужие облачные кредиты Mistral** — не гнать им массовый объём; черновики → `ollama/*`.
- Инстанс без auth и на LAN — любой в сети может жечь его токены. Если станет проблемой — включить master-key.
- IP `192.168.1.77` — локальный бокс, может смениться. Держать в `.env`, не хардкодить.
- Стриминг `stream:true` (SSE) для MVP не нужен.

## Источники
Проверено вживую: `GET /v1/models`, `GET /model/info`, `POST /v1/chat/completions` на `192.168.1.77:4000`.
LiteLLM docs: https://docs.litellm.ai/docs/ · OpenAI chat ref: https://platform.openai.com/docs/api-reference/chat/create
