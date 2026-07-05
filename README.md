# VPN Affiliate Portfolio

Машина полного цикла для портфеля VPN affiliate-сайтов: поиск и скоринг доменов →
выкуп → провижн (Cloudflare + aaPanel) → генерация и публикация контента → мониторинг.

**Агенту (Claude Code):** читай `CLAUDE.md`, затем `BUILD_SPEC.md`, `PLAN.md`, и `docs/api/README.md` (референсы интеграций + локальная инфра).

## Быстрый старт
```bash
cp .env.example .env      # заполнить ключи
docker compose up --build # поднимет db + backend
# backend: http://localhost:8000/health
docker compose run --rm backend python scripts/smoke.py  # проверка коннективности к внешним API
```

## Что где
- `backend/app/models/` — модель данных (домены, сайты, страницы, офферы, ...).
- `backend/app/integrations/` — клиенты внешних API (транспорт).
- `backend/app/services/` — бизнес-логика по модулям M1–M6.
- `scripts/smoke.py` — smoke-тест доступа к сервисам.
- `docs/api/` — референсы всех интеграций (endpoints/auth/примеры) + `README.md`-индекс с локальной инфрой.

## Ресурсы (сводка — детали в `docs/api/README.md`)
- **Метрики доменов** — бесплатный стек (Wayback / РКН / SearXNG / OpenPageRank); платного Ahrefs API нет.
- **Контент** — LiteLLM `192.168.1.77:4000` (mistral-large + ollama, без ключа).
- **SERP** — SearXNG `192.168.1.77:8080` (free); **whois/keywords** — A-Parser `:9091`.
- **Discovery** — backorder.ru (публичный фид, без auth). **GSC** исключён из v1 (индексация — ручной `site:`).

Состояние: скелет + разведанные интеграции; бизнес-логика в TODO. Порядок сборки — в `CLAUDE.md`.
