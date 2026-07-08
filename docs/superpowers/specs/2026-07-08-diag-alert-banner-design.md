# Спек: глобальный баннер «внешние инструменты недоступны» (diag-alert)

Дата: 2026-07-08. Статус: дизайн одобрен в брейншторме, готов к плану.

## 1. Цель

Оператор видит проблему связи с внешними инструментами **на любом экране панели**,
а не только зайдя на `/diag`. Если что-то внешнее упало — вверху страницы баннер:

```
⚠ Нет связи: A-Parser, Cloudflare · проверено 14:32 UTC   [↻ перепроверить]  [×]
```

Кнопка «перепроверить» тут же перезапускает диагностику; крестик прячет баннер
до конца сессии браузера (или до изменения набора упавших).

## 2. Что уже есть (не трогаем логику)

- `services/diagnostics.py::run_diagnostics()` — параллельный пинг всех интеграций,
  статусы `ok/fail/skip` + latency; `_spec()` — список из 10 проверок.
- Экран `/diag` (`panel.py::diag_view`) — живой прогон при каждом открытии.
- Flash-механизм `?msg=/?err=` → `.flash msg/.flash err` в `base.html` (строки ~344–347).
- APScheduler-воркер (`workers/scheduler.py`) — **отдельный процесс** docker-compose.

## 3. Принятые решения (из брейншторма)

| Вопрос | Решение |
|---|---|
| Свежесть статуса | Кэш + фоновая автопроверка каждые 5 мин (не живой пинг на каждый запрос — Wayback ~15с). |
| Поведение баннера | Закрываемый на сессию браузера; всплывает снова при НОВОМ наборе упавших или новой сессии. |
| Что попадает в баннер | **Все внешние инструменты** — всё, что не живёт в контейнерах комбайна. Флаг `critical` из `_spec()` НЕ используется как фильтр. |

### Правило включения: «внешний» = не наш контейнер

Из 10 проверок `_spec()` единственная внутренняя — `db` (PostgreSQL в
docker-compose комбайна; если он упал, панель сама отдаёт 500 и баннер бессмыслен).
Остальные 9 — внешние: A-Parser, LiteLLM, SearXNG (бокс), backorder, Wayback,
РКН/antizapret, Spamhaus/SURBL (публичные), Cloudflare, aaPanel (провайдеры).

Механика: константа в `diagnostics.py`
```python
_NON_EXTERNAL = {"db"}  # живёт в docker-compose комбайна; всё остальное — внешнее
```
Деривация по ключу вместо 8-го поля в кортеже `_spec()` — не меняем арность,
которую распаковывают `_run_one`, `run_diagnostics` и self-check.

В баннер попадают только `status == "fail"`. `skip` (нет кред в .env) — не авария,
не нудим; это видно на `/diag`.

### Ключевое архитектурное ограничение

APScheduler-воркер — **отдельный процесс**: его тик не может обновить память
панели. Поэтому автопроверка — фоновая asyncio-задача **внутри процесса панели**
(lifespan FastAPI), интервал тот же 5 минут.

## 4. Компоненты

### 4.1 `services/diag_cache.py` — новый модуль (кэш + алерт)

Модульное состояние + `threading.Lock` (uvicorn — один процесс; роут и фоновая
задача пишут из разных потоков).

```python
REFRESH_SEC = 300  # тот же ритм, что тик автопилота

def refresh() -> list[dict]:
    """Прогоняет run_diagnostics(), кладёт результат+время в кэш, возвращает checks."""

def alert() -> dict | None:
    """None, пока кэша нет (до первой проверки). Иначе:
    {"down": [лейблы external-fail, в порядке _spec()],
     "sig":  "aparser,cloudflare",   # sorted keys через запятую — сигнатура набора
     "checked_at": datetime}         # UTC, время последней проверки
    down может быть пуст — тогда баннер не рендерится."""
```

`checked_at` — `datetime.now(timezone.utc)` на момент завершения refresh.

### 4.2 Фоновый цикл — `main.py` lifespan

```python
@asynccontextmanager
async def lifespan(app):
    task = asyncio.create_task(_diag_loop())
    yield
    task.cancel()

async def _diag_loop():
    while True:
        try:
            await asyncio.to_thread(diag_cache.refresh)
        except Exception:
            pass  # диагностика не должна ронять панель; следующий цикл попробует снова
        await asyncio.sleep(diag_cache.REFRESH_SEC)
```

Первая проверка — сразу на старте (в фоне, старт панели не ждёт 20с пингов).
`FastAPI(title=..., lifespan=lifespan)`.

### 4.3 Jinja-global — `panel.py`

Рядом с фильтрами `status_ru/...` (panel.py:29–31):
```python
templates.env.globals["diag_alert"] = diag_cache.alert
```
Шаблон зовёт `diag_alert()` при рендере — чтение из кэша, мгновенно.

### 4.4 Баннер — `base.html`

Над flash-блоком (перед строкой `{% set _msg = ... %}`):

```jinja
{% set _alert = diag_alert() %}
{% if _alert and _alert.down %}
<div class="flash warn" id="diag-alert" data-sig="{{ _alert.sig }}">
  <span class="tag">Связь</span>
  <span style="flex:1">Нет связи: {{ _alert.down|join(', ') }}
        · проверено {{ _alert.checked_at.strftime('%H:%M') }} UTC</span>
  <form method="post" action="/diag/refresh" style="display:inline">
    <button class="btn-sm" title="Прогнать диагностику ещё раз (до 20 секунд)">↻ перепроверить</button>
  </form>
  <button type="button" class="btn-sm" title="Скрыть до конца сессии браузера"
          onclick="sessionStorage.setItem('diagDismiss', this.closest('#diag-alert').dataset.sig); this.closest('#diag-alert').remove()">×</button>
</div>
<script>
  if (sessionStorage.getItem('diagDismiss') === document.getElementById('diag-alert').dataset.sig)
    document.getElementById('diag-alert').remove();
</script>
{% endif %}
```

- **Видим по умолчанию, JS прячет** (не наоборот): панель no-JS-friendly —
  без JS баннер просто всегда виден (безопасный дефолт), с JS скрипт стоит сразу
  после div и убирает его до отрисовки.
- Сигнатура = набор упавших: упало что-то НОВОЕ → sig другой → баннер всплывает
  снова. Новая сессия браузера → sessionStorage пуст → баннер виден.
- CSS-вариант `.flash.warn` (янтарный, в тон светлой CMS), рядом с `.flash.err`:
  ```css
  .flash.warn { border-color:#ecd9a8; background:#fbf3dd; color:#7a5d14; }
  ```
  Выравнивание кнопок бесплатно: у `.flash` уже `display:flex; gap:12px;
  align-items:baseline` (base.html:81–82). Текстовый span получает `flex:1`,
  чтобы кнопки прижались вправо.

### 4.5 `POST /diag/refresh` — `panel.py`

```python
@router.post("/diag/refresh")
def diag_refresh(request: Request):
    from app.services import diag_cache
    diag_cache.refresh()          # синхронно, ≤ PING_TIMEOUT (20с) — пинги параллельны
    back = request.headers.get("referer") or "/"
    return _back(back, msg="Статусы внешних инструментов перепроверены")
```

Redirect на Referer — оператор остаётся на своём экране; баннер после редиректа
отражает свежий кэш (исчезает, если всё поднялось). Same-origin CSRF-guard
уже покрывает POST. Кнопка при клике блокируется браузером на время сабмита —
дополнительного «спиннера» не делаем.

### 4.6 `/diag` тоже кормит кэш

`diag_view` вместо прямого `run_diagnostics()` зовёт `diag_cache.refresh()`
(та же цена — живой прогон, но результат попадает в кэш → баннер консистентен
с тем, что оператор только что видел на `/diag`).

## 5. Тесты (оффлайн, monkeypatch `run_diagnostics`)

1. `refresh()` кладёт checks+время; `alert()` до первого refresh — `None`.
2. Фильтр external: fail у `aparser`+`db` → в `down` только лейбл A-Parser
   (db исключён); `skip` не попадает в `down`.
3. `sig` детерминирована (sorted keys), меняется при изменении набора.
4. `POST /diag/refresh` → 303 на Referer, `?msg=`, кэш обновлён (фейковый счётчик вызовов).
5. Рендер: GET `/` при кэше с down → в HTML есть `id="diag-alert"`, лейблы,
   форма `/diag/refresh`, крестик; при пустом down / пустом кэше — баннера нет.
6. `diag_view` обновляет кэш (после GET `/diag` `alert()` не `None`).

Фоновый цикл — тривиальный клей, отдельный тест не пишем; проверяется смоуком
на боксе (баннер появился без захода на /diag).

## 6. Проверка глазами

Playwright-скриншоты: экран с баннером (замокать кэш с 2 упавшими), экран без
баннера, экран после «перепроверить» с flash-сообщением. Светлая CMS, шильдики
(title-подсказки на обеих кнопках) — по дизайн-контракту.

## 7. YAGNI — сознательно не делаем

- HTMX/JS-поллинг статуса в реальном времени — рендер из кэша при переходах достаточен.
- Серверное хранение «закрыто» (сессии/БД) — sessionStorage браузера хватает.
- Изменение арности `_spec()` (8-е поле external) — деривация по ключу проще.
- Телеграм/почта-уведомления — это Спек 5.
- Ретраи/анти-мигание (подтверждение падения двумя циклами) — если баннер начнёт
  мигать на нестабильном Wayback, добавим дебаунс отдельным полишем.
