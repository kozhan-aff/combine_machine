# VPN Affiliate Portfolio · КОМБАЙН

Машина полного цикла для портфеля VPN affiliate-сайтов: поиск и скоринг доменов →
выкуп → провижн (Cloudflare + aaPanel) → генерация и публикация контента → мониторинг.
Управляется веб-панелью (FastAPI + Jinja, светлая CMS-тема, каждый контрол подписан).

**Агенту (Claude Code):** читай `CLAUDE.md`, затем `BUILD_SPEC.md`, `PLAN.md`, и `docs/api/README.md` (референсы интеграций + локальная инфра).

## Быстрый старт
```bash
cp .env.example .env      # заполнить ключи
docker compose up --build # поднимет db + backend + worker
# панель:  http://192.168.1.77:8000/   (бокс; LAN-only, без auth — не выставлять в интернет)
# health:  http://192.168.1.77:8000/health
docker compose run --rm backend pytest -q   # тесты пайплайна (SQLite, сеть замокана)
```
Панель сама ведёт по шагам: Пульт → Офферы → Домены (M1) → карточка сайта (M3–M5).
Диагностика (`/diag`) пингует все интеграции и умеет обновлять код кнопкой «Обновить из git».

## Что где
- `backend/app/api/panel.py` + `backend/app/templates/` — HTML-панель (экраны + действия).
- `backend/app/api/` (остальное) — JSON API под `/api` (та же логика, для скриптов).
- `backend/app/models/` — модель данных (домены, сайты, страницы, офферы, ...).
- `backend/app/integrations/` — клиенты внешних API (только транспорт).
- `backend/app/services/` — бизнес-логика по модулям M1–M6 (+ `diagnostics.py` для /diag).
  - `discovery.py` — v2-поиск: DropCatch / Nominet / registry.mx / EMD / ручной список → белый
    список зон → бесплатный DR Ahrefs на входе (память `dr_seen`);
  - `scoring.py` — воронка из 6 волн (`t0 → avail → risk → links → history → deep`), скор и
    правила пакетного одобрения (`approved` ставит только человек);
  - `domain_filters.py`, `link_signals.py`, `history_llm.py` — чистые функции: зоны/бренды/EMD,
    спам-анкоры и пик трафика, разбор ответа LLM о языке и теме прошлого сайта.
- `backend/app/integrations/{ahrefs,rdap,webrisk,dropcatch,nominet,registry_mx}.py` — клиенты v2
  (Ahrefs API v3, RDAP с бутстрапом IANA, Google Web Risk, списки дропов).
- `backend/tests/` — пайплайн-тесты на SQLite (гейты, скоринг, публикация, экраны).
- `docs/api/` — референсы всех интеграций (endpoints/auth/примеры) + `README.md`-индекс.
- `docs/DEPLOY.md` — деплой на бокс, обновление через git, каналы доступа.

## Ресурсы (сводка — детали в `docs/api/README.md`)
- **Метрики доменов (v2)** — Ahrefs API v3 (DR на входе, batch W4, анкоры W6 — под капами и полом units), RDAP/whois, Google Web Risk, Wayback + LLM-тема; Spamhaus — только с DQS.
- **Контент** — LiteLLM `192.168.1.77:4000` (mistral-large + ollama, без ключа).
- **SERP** — SearXNG `192.168.1.77:8080` (free); **whois/keywords** — A-Parser `:9091`.
- **Discovery (v2)** — DropCatch (после проверки ToS), Nominet (.uk), registry.mx, EMD, ручной список. **GSC** исключён (индексация — ручной `site:`).
- **Прод-VPS** (origin для сайтов) — aaPanel, проверен вживую; Cloudflare — DNS + маскировка origin.

## Два жёстких правила (зашиты в код, не обходить)
1. **Гейт редактуры:** публикуются только страницы `edited` — черновик AI наружу не выходит.
2. **Гейт выкупа:** деньги тратит только человек (`confirmed_by_human` / кнопка «куплен» в панели).

Состояние: панель работает (M1 + петля M3→M5 на моках проверена), интеграции разведаны
и задокументированы; впереди — первый реальный домен через полную петлю. Порядок — в `CLAUDE.md`.
