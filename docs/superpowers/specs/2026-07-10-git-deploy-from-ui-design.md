# Дизайн-спека: надёжный git-деплой из панели (без консоли)

**Дата:** 2026-07-10
**Статус:** утверждена к реализации
**Область:** `backend/app/services/deploy.py` (новый), `backend/app/api/panel.py` (хендлеры),
`backend/app/templates/diag.html`, `docker-compose.yml` (команда воркера), `docs/DEPLOY.md`,
тесты. Бэкенд-логика пайплайна/скоринга/гейтов НЕ трогается.

## Проблема

Кнопка «⇩ Обновить из git» в `/diag` делает `git pull --ff-only` + `alembic upgrade head`;
backend подхватывает код через `--reload`. Работает для чистого fast-forward, но в реальных
ситуациях оставляет пользователя без пути из UI:

1. **Грязное дерево / не-ff / detached HEAD** → `--ff-only` падает, баннер с ошибкой, дальше
   только консоль (`reset --hard`).
2. **Воркер** (`python -m app.workers.scheduler`, BlockingScheduler) БЕЗ reload → код шедулера/
   оркестратора кэшируется в `sys.modules` и остаётся старым до рестарта контейнера, который
   панель изнутри backend-контейнера сделать не может.
3. **Новые зависимости / Dockerfile** → `pull` не пересобирает образ; импорт новой библиотеки
   падает; молча, без предупреждения.
4. **Нет пред-статуса** — состояние дерева (branch, чисто/грязно, ahead/behind) не видно до пула,
   отказ становится сюрпризом.

## Цель

Пользователь получает обновления **из панели во всех реалистичных ситуациях, без консоли**.
Единственное честное исключение — пересборка образа под новые зависимости/Dockerfile (редко) —
детектится и сопровождается точной инструкцией, а не молчаливым отказом.

## Решение (обзор)

Панель остаётся **git-only** (без доступа к Docker — безопаснее: LAN-панель без пароля не
получает root над хостом). Три составляющие:
1. **Надёжная машина состояний обновления** с видимым пред-статусом и деструктивным
   fallback'ом `reset --hard` из UI.
2. **Авто-reload воркера** через `watchfiles` (уже в образе через `uvicorn[standard]`) — код
   шедулера подхватывается живьём, без Docker и пересборки.
3. **Честный детект** случая «нужна пересборка» (requirements.txt/Dockerfile в диффе).

## Компонент 1 — `services/deploy.py` (новый; вся git-логика уходит из panel.py сюда)

Конвенция проекта: логика в `services/`. Сейчас git-subprocess живёт инлайн в `panel.py` —
переносим в тестируемый сервис. Публичный интерфейс:

```python
def deploy_status() -> dict:
    """Локально (без сети), дёшево. {branch, hash, subject, date, dirty: bool,
    ahead: int, behind: int} или {error}. ahead/behind — против ЛОКАЛЬНОГО
    remote-tracking origin/main (свеж после любого fetch/check/pull; 0 до первого)."""

def git_pull() -> dict:
    """Безопасный путь. fetch origin main → (если чисто и ff-возможен) pull --ff-only →
    alembic upgrade head → детект пересборки. Возвращает:
    {ok: bool, old: str, new: str, message: str, needs_rebuild: bool,
     alembic_warn: str, needs_force: bool, error: str}.
    needs_force=True когда дерево грязное / история разошлась — панель предложит force."""

def git_force_pull() -> dict:
    """Деструктивный путь (по confirm). fetch origin main → checkout -B main <fetched> →
    reset --hard <fetched> → alembic → детект. НИКОГДА не git clean (untracked/.env/.pem
    выживают). Приводит к origin/main на ветке main из ЛЮБОГО состояния (грязь/расхождение/
    detached). Возвращает тот же dict."""
```

Внутренние детали:
- **Аутентификация git:** helper строит `git_env` с `http.extraheader = Authorization: Basic
  base64("x-access-token:TOKEN")` (как сейчас в panel.py) — токен НЕ в argv. `clean_url =
  https://github.com/{GITHUB_REPO}.git`. Скраб-функция `s.replace(token, "***")` на ВСЕ
  возвращаемые строки.
- **Детект пересборки:** `git -C /repo diff --name-only {old}..{new}` — если среди путей есть
  `backend/requirements.txt` или `backend/Dockerfile` → `needs_rebuild=True`, в message
  добавляется «код обновлён; нужна пересборка образа: `docker compose up -d --build`».
- **alembic:** `alembic upgrade head` в `cwd=/app`, timeout 120; падение → `alembic_warn`
  (не блокирует, код уже обновлён).
- **force-pull, точная последовательность** (цель — origin/main на ветке main из любого
  состояния, секреты целы):
  ```
  git -C /repo fetch <clean_url> main            # FETCH_HEAD = tip origin/main
  git -C /repo checkout -B main FETCH_HEAD        # ветку main привязать к свежему tip
  git -C /repo reset --hard FETCH_HEAD            # рабочее дерево = tip (сбрасывает грязь)
  ```
  `reset --hard`/`checkout -B` НЕ трогают untracked → `.env`, `backend/aapanel.pem` выживают.
  `git clean` НЕ вызывается никогда.
- **Single-flight замок:** модульный `threading.Lock` (или паттерн `services/jobs.py`) — второй
  вызов `git_pull`/`git_force_pull` во время идущего возвращает `{error: "обновление уже идёт"}`.
- **Таймауты:** fetch 30с, pull/reset/checkout 120с, alembic 120с, status-команды 10с.

## Компонент 2 — Авто-reload воркера (`docker-compose.yml`)

`watchfiles` уже в образе (транзитивно через `uvicorn[standard]`). Меняем команду воркера:

```yaml
# было:  command: python -m app.workers.scheduler
# стало:
command: watchfiles 'python -m app.workers.scheduler' /app
```

watchfiles следит за `/app` (== `./backend`, куда `git pull` кладёт код) и перезапускает процесс
шедулера при любой правке `.py`. Прерванный свип безопасен: `run_sweep` single-flight +
идемпотентен, следующий тик продолжит. Для явности добавить `watchfiles` отдельной строкой в
`backend/requirements.txt` (страховка на случай, если uvicorn перестанет тянуть его транзитивно).

**Разовая активация фичи:** `docker compose up -d` (пересоздать воркер с новой командой). Если
`watchfiles` добавлен в requirements и его нет в текущем образе — `docker compose up -d --build`.
После этого — всё из UI.

## Компонент 3 — UI в `/diag` (`diag.html`)

Расширяем существующую станцию «Обновить из git» (холодный контракт: `.station` + `details.what`,
`.btn-acc` primary, `.btn-bad` деструктив, итоги — существующий flash-баннер сверху). `/diag`
GET передаёт `deploy_status()` в шаблон.

Статус-строка (всегда видна, локальная, без сети):
- **Версия:** `hash · subject · date` (как сейчас).
- **Ветка:** `main · ✓ чисто · актуально|позади на N|впереди на N` ИЛИ `⚠ грязно` ИЛИ
  `⚠ разошлось (±N)` ИЛИ `detached HEAD`. Красная подсветка при грязно/разошлось/detached.

Кнопки:
- **`⇩ Обновить из git`** → `POST /admin/pull` → `deploy.git_pull()`. Основная (`.btn-acc`).
- **`⚠ Принудительно обновить`** → `POST /admin/force-pull` → `deploy.git_force_pull()`.
  `.btn-bad`, JS-`confirm("Локальные правки на боксе будут потеряны, .env сохранится. Продолжить?")`.
  Доступна всегда (на случай застревания), но её роль подсвечена, когда статус грязный/разошёлся.
- **`⟳ Проверить обновления`** → `POST /admin/check-updates` (как сейчас, ls-remote; опц.
  дополнить «позади на N» через `git rev-list --count`).

Итоги — во flash-баннере (old→new, что применилось, `needs_rebuild`-подсказка, `alembic_warn`),
через существующий `_back("/diag", msg=/err=)`. Токен всегда скраббится.

## Компонент 4 — Хендлеры `panel.py` (тонкие обёртки)

- `POST /admin/pull` → `r = deploy.git_pull()`; сформировать `_back("/diag", msg|err)` из dict.
- `POST /admin/force-pull` (НОВЫЙ) → `r = deploy.git_force_pull()`; аналогично.
- `POST /admin/check-updates` → как сейчас (можно перенести в deploy, необязательно).
- `/diag` GET (`diag_view`) → добавить `status = deploy.deploy_status()` в контекст.

Гейт `GITHUB_TOKEN` (нет токена → err) остаётся в начале pull/force-pull.

## Безопасность

- **Деструктив под confirm + POST-only + красная кнопка.** `.env`/`.pem` (gitignored, untracked)
  переживают `reset --hard`; `git clean` не вызывается.
- **Авторизация — усиленная рекомендация** (не форс): появилась деструктивная кнопка на
  LAN-панели. В `deploy_status`-строке и в `docs/DEPLOY.md` — явная рекомендация закрыть панель
  `PANEL_USER`/`PANEL_PASS` (механизм уже есть). Не включаем принудительно — выбор пользователя.
- **Токен** GITHUB_TOKEN скраббится во всех выводах, никогда не в argv (extraheader-env).

## Отказы — у каждого понятный баннер (тупика в консоль нет, кроме пересборки)

| Ситуация | Баннер |
|---|---|
| нет GITHUB_TOKEN | «токен не задан в .env — нечем авторизовать» |
| грязно / не-ff (git_pull) | «дерево грязное / история разошлась — используй ⚠ Принудительно обновить» |
| alembic упал | «код обновлён; ⚠ миграции: …» (msg, не err) |
| новые зависимости/Dockerfile | «код обновлён; нужна пересборка: `docker compose up -d --build`» |
| git не установлен | «пересобери образ (docker compose build)» |
| обновление уже идёт | «обновление уже идёт — подожди завершения» |
| force успешен | «Принудительно обновлено: old→new «subject»» |

## Тестирование (офлайн, мок subprocess)

Тесты мокают `subprocess.run` (как остальные тесты мокают сеть — autouse-фикстура режет живую
сеть; git тоже subprocess). Проверяем ЛОГИКУ машины состояний, не живой git:
- `deploy_status()`: парсит branch/dirty/ahead/behind из canned-вывода git (чисто/грязно/detached).
- `git_pull()`: (а) чисто+behind → ff-pull ок → детект без пересборки → message old→new;
  (б) грязно/не-ff (pull returncode≠0) → `needs_force=True`, подсказка про force;
  (в) `requirements.txt` в `diff --name-only` → `needs_rebuild=True`, подсказка про пересборку;
  (г) alembic returncode≠0 → `alembic_warn` заполнен, `ok` не рушится.
- `git_force_pull()`: последовательность fetch→checkout -B→reset мокнута → success dict; проверить,
  что `git clean` НЕ среди вызванных команд.
- **Скраббинг:** токен в stderr мока → в возвращённом message заменён на `***`.
- **Single-flight:** второй вызов при захваченном замке → `{error: "обновление уже идёт"}`.
- Хендлеры `panel.py`: через TestClient POST `/admin/force-pull` с мокнутым `deploy` → 303 на
  `/diag` с ожидаемым msg/err; `/diag` GET рендерит статус-строку.

`.venv/bin/python -m pytest backend/tests/ -q` (сейчас 207) + pyflakes чист после каждой задачи.

## Вне области (YAGNI)

- Docker-контроль из панели (socket/pipe) — отклонён по безопасности (LAN-панель без пароля не
  должна получать root над хостом).
- Хелпер-служба на боксе — отклонён (постоянный сервис = лишняя точка отказа против «надёжно
  всегда»).
- Авто-пересборка образа под новые зависимости — вне UI by design (детект + инструкция).
- `pip install` живьём в контейнер под новую зависимость — отклонён (эфемерно, теряется при
  пересоздании контейнера → скрытый футган; честная пересборка предсказуемее).
- Принудительное включение Basic-auth — остаётся выбором пользователя (рекомендация, не форс).
