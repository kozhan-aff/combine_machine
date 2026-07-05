# DEPLOY — Docker, обновление через git, канал управления/дебага

Три вещи: (1) как поднять в Docker, (2) как обновлять через git, (3) через что Claude
подключается к живому приложению — управляет, дебажит, катит фичи.

---

## 1. Docker — поднять стек

Стек в `docker-compose.yml`: `db` (postgres:16) + `backend` (FastAPI :8000) + `worker`
(шедулер M1). Миграции применяются автоматически при старте backend
(`alembic upgrade head` идемпотентен), БД гейтится healthcheck'ом.

```bash
cp .env.example .env          # заполнить креды (LLM/searxng уже на дефолтах бокса)
docker compose up --build     # db -> (ждём healthy) -> migrate -> uvicorn + worker
# панель:   http://<host>:8000/
# health:   http://<host>:8000/health
```

Полезное:
```bash
docker compose logs -f backend          # логи API
docker compose ps                        # статус контейнеров
docker compose run --rm backend pytest -q          # тесты в контейнере
docker compose run --rm backend alembic upgrade head   # миграции вручную
docker compose exec db psql -U portfolio             # SQL-консоль
docker compose down            # стоп (данные в volume pgdata сохраняются)
```

**Где поднимать: на боксе `192.168.1.77`.** Там уже LiteLLM/SearXNG → приложение
ходит к ним по localhost (быстрее, дефолты в `.env` совпадают), бокс всегда включён.
Панель `:8000` **выставлена на LAN** (`192.168.1.77:8000:8000` — привязка к LAN-интерфейсу
бокса, не `0.0.0.0`) — открывается с Mac по `http://192.168.1.77:8000/`. Авторизации у
панели нет, поэтому это ОК только за NAT домашнего роутера: **не пробрасывать `:8000` в
интернет**. ⚠️ Без авторизации любой в LAN может гонять пайплайн, включая `POST /admin/pull`
(git-pull + перезагрузка кода) — за пределами доверенной домашней сети добавь auth
(Basic-auth middleware / APIKey Depends) или ограничь source-IP в `DOCKER-USER`. Хочешь
приватно — верни `127.0.0.1:8000:8000` и ходи через SSH-туннель (§3). Postgres `:5432`
остаётся loopback-only. (Docker публикует порт в обход хостового фаервола — работает даже
при default-deny ufw. Если IP бокса сменится по DHCP — привязка не поднимется, зарезервируй
192.168.1.77 на роутере.)

---

## 2. Обновление через git

Репо: **github.com/kozhan-aff/combine_machine** (private). Локально уже `git init`
+ первый коммит + `origin` настроен. Первый пуш (нужен GitHub PAT или `gh auth`):
```bash
git push -u origin main
```
`.env` в `.gitignore` (секреты не коммитим) — на хосте кладётся отдельно из `.env.example`.

**Цикл обновления (dev на Mac → деплой на бокс):**
```
[Mac] правим код -> commit -> push
[box] git pull -> docker compose up -d --build   # migrate прогонится сам
```
Одной строкой с Mac (когда есть SSH, §3):
```bash
ssh box 'cd ~/vpn-portfolio && git pull && docker compose up -d --build'
```

Заметки:
- backend монтирует `./backend` volume + `--reload` → в dev правки кода подхватываются
  без пересборки; пересборка (`--build`) нужна только при смене `requirements.txt`/Dockerfile.
- Миграции: создаём на Mac (`alembic revision --autogenerate -m "..."`), коммитим файл;
  на боксе применяются автоматически при рестарте backend.
- Откат: `git revert` + `up -d --build`. Данные БД в volume, миграции — только вперёд
  (для отката данных — `alembic downgrade`).

### Кнопка «Обновить из git» в панели (без консоли)
Панель → **Диагностика** → «⇩ Обновить из git» делает `git pull --ff-only` + `alembic
upgrade head` прямо из контейнера; код подхватывает `--reload`. Работает так:
- `docker-compose.yml` монтирует весь репо `.:/repo` (с `.git`), в образе стоит `git`.
- Тянем по **HTTPS с fine-grained PAT** (не монтируем SSH-ключ в контейнер). Настройка:
  1. github.com → Settings → Developer settings → Fine-grained tokens → Generate:
     доступ только к репо `combine_machine`, права **Contents: Read-only**.
  2. Вписать в `.env` бокса: `GITHUB_TOKEN=github_pat_...` (и `GITHUB_REPO=kozhan-aff/combine_machine`).
  3. `docker compose up -d --build` (Dockerfile изменился — нужен пересбор).
- Ограничения: `--ff-only` (не затрёт локальные правки — упадёт с сообщением в баннере);
  смена `requirements.txt`/Dockerfile всё равно требует `--build` руками. Токен в баннер не попадает.
- Безопасность: эндпойнт POST-only, панель слушает только `127.0.0.1` (§3).

---

## 3. Канал управления / дебага для Claude

Claude работает как CLI на Mac и управляет через инструмент Bash. Значит «подключиться к
приложению» = дотянуться до него из Mac. Приложение на боксе (§1), бокс в LAN → достаётся
напрямую. Четыре канала, каждый под свою задачу:

| Канал | Порт/инструмент | Для чего | Что нужно настроить |
|---|---|---|---|
| **HTTP API** | туннель `ssh -L 8000:localhost:8000 box` → `localhost:8000` | гонять пайплайн (offers/purchase/site/generate/edit/publish/check-index), панель, /health | порт биндится на 127.0.0.1 бокса |
| **SSH** | `ssh user@192.168.1.77` (22) | **главный debug-канал**: `docker compose logs/exec/ps`, `alembic`, `psql`, рестарт, деплой | включить SSH на боксе + `ssh-copy-id` ключ Mac'а |
| **Git** | remote + `ssh box git pull` | катить новые фичи (правлю на Mac → пуш → pull+rebuild на боксе) | §2 |
| **Postgres** | `ssh box docker compose exec db psql` | смотреть/чинить данные напрямую при дебаге | localhost-only, не наружу |

**Единственная настройка, которую надо сделать руками:** включить SSH на боксе и залить
ключ Mac'а —
```bash
ssh-copy-id user@192.168.1.77          # с Mac, один раз
echo "Host box" >> ~/.ssh/config; echo "  HostName 192.168.1.77" >> ~/.ssh/config; echo "  User <user>" >> ~/.ssh/config
```
После этого Claude из Bash делает всё: `ssh box 'docker compose logs -f backend'`,
`curl 192.168.1.77:8000/...`, деплой, миграции, SQL.

**Безопасность:**
- 8000/5432 — только LAN (firewall бокса). Панель без авторизации — в интернет НЕ выставлять.
- Нужен доступ не из LAN (Claude в облаке/другая сеть)? Не открывать порт, а туннель:
  `ssh -L 8000:localhost:8000 box` → работать через `localhost:8000`.
- Прод-выкладка сайтов идёт на отдельный VPS (Cloudflare origin) — это не тот бокс;
  бокс = движок машины (оркестратор + LLM/SERP), VPS = где живут сами affiliate-сайты.

**Дебаг-петля Claude на боксе:** `ssh box docker compose logs -f backend` (смотрю ошибку)
→ правлю на Mac → `git push` → `ssh box 'git pull && docker compose up -d --build'` →
`curl 192.168.1.77:8000/...` (проверяю) → повтор. Для быстрых итераций без пересборки
хватает volume+`--reload`: `git pull` подхватится сам.
