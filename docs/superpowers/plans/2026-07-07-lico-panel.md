# Спек 2 «Лицо» Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Панель говорит по-русски одним голосом (центральная display-мапа), длинные задачи показывают визуальный прогрессбар, /diag читается как дашборд здоровья, закрыты три косметических долга Спек 1.

**Architecture:** Одна точка правды переводов — `services/labels.py` (мапы статусов/reject/лейнов + Jinja-фильтры). Прогресс и /diag — только представление: механику (`jobs.py`, `run_diagnostics`, терминальный контракт) не трогаем, обогащаем данные и переписываем шаблоны/CSS. Всё офлайн-тестируемо на SQLite-харнессе; визуал бара проверяется глазами (Playwright).

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.x, Jinja2, pytest (оффлайн SQLite `backend/tests/conftest.py`).

## Global Constraints

- **Спек:** `docs/superpowers/specs/2026-07-06-lico-panel-design.md` — источник истины.
- **Дизайн-контракт:** светлая CMS, принцип шильдика, ТОЛЬКО CSS-переменные из `base.html` (`--amber`#e05e10 / `--amber-soft`#fdeee2 / `--amber2` / `--ok`#1a8f4d / `--bad`#d43737 / `--mut` / `--panel2` / `--line` / `--mono` …). Никакого тёмного/индустриального. Новые подписи — по-русски.
- **Локализация — одна точка правды** (`services/labels.py`), не разбросанные литералы. Неизвестный ключ → возвращается сам (фолбэк). `None`/пусто → `""`.
- **НЕ переводим собственные имена сервисов:** Cloudflare, aaPanel, LiteLLM, SearXNG, Wayback, A-Parser, PostgreSQL, РКН/antizapret, Alembic, git, backorder/optimizator (провайдеры). Переводим названия статусов/действий/reject-причин.
- **CSS-класс бейджа остаётся на СЫРОМ значении:** текст `{{ x|status_ru }}`, класс `b-{{ x }}` (цвет привязан к enum, не к переводу).
- **Терминальный контракт прогресса держать байт-в-байт:** `error` проверяется ПЕРВЫМ (без reload), `running==false` без ошибки = готово (класс done → reload), `total==0 && running` = неопределённый режим.
- **Безопасность панели не ослаблять:** CSRF same-origin + Basic-auth в `main.py`, роуты в тех же роутерах.
- **Тесты офлайн+детерминированы:** прогон `.venv/bin/python -m pytest backend/tests/ -q` из корня репо; pyflakes чистый: `.venv/bin/python -m pyflakes backend/app backend/tests`. Сеть мокается.
- **Хард-гейты не трогать:** деньги (`confirmed_by_human`), публикация из `edited`.

---

## Файловая карта

- `backend/app/services/labels.py` — **создать**: `STATUS_RU`/`REJECT_RU`/`LANE_RU` + `status_ru`/`reject_ru`/`lane_ru` (Task 1).
- `backend/app/api/panel.py` — регистрация фильтров на `templates` (Task 1); `diag_view` +`crit_down` (Task 4); `score_one_action` unresolved-флеш (Task 5).
- `backend/app/templates/{domains,queue,site,dashboard}.html` — замена литералов на фильтры (Task 2); `domains.html` тултип `price_checked_at` (Task 5).
- `backend/app/templates/base.html` — CSS `.progress` (Task 3).
- `backend/app/templates/domains.html` — разметка `#prog` + JS `poll` (Task 3).
- `backend/app/services/diagnostics.py` — кортеж `_spec()` +module/critical, прокидка (Task 4).
- `backend/app/templates/diag.html` — группировка + сводка + бар отклика (Task 4).
- `backend/app/integrations/backorder.py` — `period[0].get("id")` (Task 5).
- Тесты: **создать** `backend/tests/test_labels.py`; правки `test_web_fixes.py`, `test_pricing.py`.

---

## Task 1: labels.py — центральная display-мапа + Jinja-фильтры

**Files:**
- Create: `backend/app/services/labels.py`
- Modify: `backend/app/api/panel.py` (после строки 27, где создан `templates`)
- Test: `backend/tests/test_labels.py` (создать)

**Interfaces:**
- Produces: `status_ru(v: str|None) -> str`, `reject_ru(v: str|None) -> str`, `lane_ru(v: str|None) -> str` — перевод или сырое значение (фолбэк); `None`/пусто → `""`.
- Produces: словари-константы `STATUS_RU`, `REJECT_RU`, `LANE_RU`.
- Produces: Jinja-фильтры `status_ru`/`reject_ru`/`lane_ru` на объекте `panel.templates`.

- [ ] **Step 1: Тест покрытия и фолбэка**

Создать `backend/tests/test_labels.py`:
```python
"""labels.py — одна точка правды переводов статусов/reject/лейнов для панели."""


def test_status_ru_covers_domain_lifecycle():
    from app.services.labels import status_ru
    for s in ["discovered", "scored", "approved", "rejected",
              "purchasing", "purchased", "live"]:
        assert status_ru(s) and status_ru(s) != s   # переведён, не сырой


def test_status_ru_covers_order_site_page():
    from app.services.labels import status_ru
    for s in ["pending_confirm", "ordered", "caught", "failed", "cancelled",  # заказ M2
              "provisioning", "content", "published",                          # сайт
              "draft", "edited"]:                                              # страница
        assert status_ru(s) and status_ru(s) != s


def test_reject_ru_covers_all_reasons():
    from app.services.labels import reject_ru
    for r in ["low_rd", "feed_flag", "too_young", "rkn", "blacklist",
              "history_dirty", "low_score", "not_acquirable"]:
        assert reject_ru(r) and reject_ru(r) != r


def test_lane_and_fallback_and_none():
    from app.services.labels import status_ru, reject_ru, lane_ru
    assert lane_ru("bid") == "ставка" and lane_ru("free") == "свободный"
    assert status_ru("weird_unknown") == "weird_unknown"      # неизвестный → сырой
    assert status_ru(None) == "" and reject_ru(None) == "" and lane_ru(None) == ""
    assert status_ru("") == ""


def test_filters_registered_on_templates():
    from app.api.panel import templates
    assert templates.env.filters["status_ru"]("approved") == "одобрен"
    assert templates.env.filters["reject_ru"]("not_acquirable") == "нельзя купить"
    assert templates.env.filters["lane_ru"]("bid") == "ставка"
```

- [ ] **Step 2: Прогнать — падает (нет модуля)**

Run: `.venv/bin/python -m pytest backend/tests/test_labels.py -q`
Expected: FAIL (`ModuleNotFoundError: app.services.labels`).

- [ ] **Step 3: Создать labels.py**

Создать `backend/app/services/labels.py`:
```python
"""Одна точка правды человекочитаемых подписей панели: статусы (домен/заказ/сайт/страница),
причины отклонения, лейны выкупа. Регистрируются как Jinja-фильтры в panel.py.

Правило: неизвестный ключ возвращается как есть (не роняем шаблон), None/пусто → "".
CSS-класс бейджа остаётся на СЫРОМ значении — переводим только текст.
"""

# Все статусы конвейера в одной плоской мапе (значения enum не конфликтуют между
# домен/заказ/сайт/страница; "published" общий для сайта и страницы — смысл один).
STATUS_RU = {
    # домен (M1–M2)
    "discovered": "найден", "scored": "оценён", "approved": "одобрен",
    "rejected": "отклонён", "purchasing": "в очереди", "purchased": "куплен",
    "live": "живой",
    # заказ выкупа (M2)
    "pending_confirm": "ждёт подтверждения", "ordered": "отправлен",
    "caught": "пойман", "failed": "ошибка", "cancelled": "отменён",
    # сайт (M3–M5)
    "provisioning": "поднимается", "content": "контент", "published": "опубликован",
    # страница (M4–M5)
    "draft": "черновик", "edited": "отредактирован",
}

REJECT_RU = {
    "low_rd": "мало доноров", "feed_flag": "флаг источника", "too_young": "моложе порога",
    "rkn": "реестр РКН", "blacklist": "блэклист", "history_dirty": "грязная история",
    "low_score": "низкий скор", "not_acquirable": "нельзя купить",
}

LANE_RU = {"bid": "ставка", "free": "свободный"}


def status_ru(v):
    return STATUS_RU.get(v, v) if v else ""


def reject_ru(v):
    return REJECT_RU.get(v, v) if v else ""


def lane_ru(v):
    return LANE_RU.get(v, v) if v else ""


if __name__ == "__main__":  # self-check без БД
    assert status_ru("approved") == "одобрен" and status_ru("zzz") == "zzz"
    assert status_ru(None) == "" and lane_ru("bid") == "ставка"
    assert reject_ru("not_acquirable") == "нельзя купить"
    print("labels ok")
```

- [ ] **Step 4: Зарегистрировать фильтры в panel.py**

В `backend/app/api/panel.py`, сразу после строки 27 (`templates = Jinja2Templates(...)`):
```python
from app.services.labels import status_ru as _status_ru, reject_ru as _reject_ru, lane_ru as _lane_ru
templates.env.filters["status_ru"] = _status_ru
templates.env.filters["reject_ru"] = _reject_ru
templates.env.filters["lane_ru"] = _lane_ru
```

- [ ] **Step 5: Прогнать — проходит**

Run: `.venv/bin/python -m pytest backend/tests/test_labels.py -q`
Expected: PASS (5 passed).
Затем pyflakes: `.venv/bin/python -m pyflakes backend/app backend/tests` → чисто.

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/labels.py backend/app/api/panel.py backend/tests/test_labels.py
git commit -m "лицо: labels.py — display-мапа статусов/reject/лейнов + Jinja-фильтры"
```

---

## Task 2: Локализация шаблонов через фильтры

**Files:**
- Modify: `backend/app/templates/domains.html` (бейдж статуса, лейн-колонка, тултип, легенда, станции)
- Modify: `backend/app/templates/queue.html` (бейдж статуса заказа)
- Modify: `backend/app/templates/site.html` (бейджи сайта/страницы)
- Modify: `backend/app/templates/dashboard.html` (бейдж сайта)
- Test: `backend/tests/test_web_fixes.py` (рендер-ассерт)

**Interfaces:**
- Consumes: фильтры `status_ru`/`reject_ru`/`lane_ru` (Task 1).

- [ ] **Step 1: Тест — /domains показывает русские подписи**

В `backend/tests/test_web_fixes.py` добавить:
```python
def test_domains_localized_labels(client, sqlite_db):
    import app.db as db
    from app.models.domain import Domain
    with db.SessionLocal() as s:
        s.add(Domain(domain="loc.ru", source="backorder", status="approved",
                     lane="bid", referring_domains=100))
        s.add(Domain(domain="rej.ru", source="cctld", status="rejected",
                     reject_reason="not_acquirable"))
        s.commit()
    html = client.get("/domains?show_all=1").text
    assert "одобрен" in html and "отклонён" in html       # статусы по-русски
    assert "ставка" in html                                # лейн по-русски
    assert "нельзя купить" in html and "not_acquirable" in html  # reject: фраза + код
```

- [ ] **Step 2: Прогнать — падает (нет русских подписей)**

Run: `.venv/bin/python -m pytest backend/tests/test_web_fixes.py::test_domains_localized_labels -q`
Expected: FAIL (`assert "одобрен" in html` — сейчас там "approved").

- [ ] **Step 3: domains.html — бейдж статуса + reject фраза+код**

В `backend/app/templates/domains.html` заменить блок статуса (строки ~154–155):
```html
      <td><span class="badge b-{{ d.status if d.status in ['approved','scored','rejected','discovered','purchased','purchasing','live'] else 'default' }}">{{ d.status|status_ru }}</span>
        {% if d.reject_reason %}<span class="hint" title="причина отклонения (reject_reason)">{{ d.reject_reason|reject_ru }} <code>{{ d.reject_reason }}</code></span>{% endif %}</td>
```

- [ ] **Step 4: domains.html — лейн-колонка + тултип источника**

Заменить лейн-ячейку (строка ~156):
```html
      <td><span class="hint">{{ d.lane|lane_ru or '—' }}</span></td>
```
И в тултипе бейджа-источника (строка ~150) заменить тернар лейна:
```html
        title="источник: {{ d.source or '—' }} · лейн: {{ d.lane|lane_ru or '—' }}{% if d.acquire_deadline %} · дедлайн {{ d.acquire_deadline.strftime('%d.%m') }}{% endif %}{% if d.acquire_price %} · цена {{ '%.0f'|format(d.acquire_price|float) }}{% endif %}"
```

- [ ] **Step 5: domains.html — легенда (фразы + коды), станции, эмпти**

В легенде (строки ~83–98) заменить текст бейджей на русский, оставив CSS-класс на сыром:
```html
    <span class="badge b-discovered">{{ 'discovered'|status_ru }}</span><span class="who">машина</span>
      <span class="desc">найден в фиде, ещё не оценён — ждёт ▶ Запуск проверки</span>
    <span class="badge b-scored">{{ 'scored'|status_ru }}</span><span class="who">машина</span>
      <span class="desc">оценён, но не дотянул до авто-одобрения — реши сам: ✓ или ✗</span>
    <span class="badge b-approved">{{ 'approved'|status_ru }}</span><span class="who">машина / ты</span>
      <span class="desc">чистый и годный к выкупу — купи руками у провайдера, потом отметь 🛒</span>
    <span class="badge b-rejected">{{ 'rejected'|status_ru }}</span><span class="who">машина / ты</span>
      <span class="desc">отклонён — причина в колонке «статус» (фраза + <code>код</code>):
      <code>low_rd</code> мало доноров, <code>feed_flag</code> флаг источника, <code>too_young</code>
      моложе порога, <code>rkn</code> реестр РКН, <code>blacklist</code> блэклист,
      <code>history_dirty</code> грязная история, <code>low_score</code> низкий скор,
      <code>not_acquirable</code> нельзя купить (занят, не на бэкордере)</span>
    <span class="badge b-purchased">{{ 'purchased'|status_ru }}</span><span class="who">только ты</span>
      <span class="desc">куплен вручную — money-gate: система сама деньги не тратит</span>
    <span class="badge b-live">{{ 'live'|status_ru }}</span><span class="who">машина</span>
      <span class="desc">сайт на домене опубликован и работает</span>
```
Заменить заголовки станций (строки 11 и 28) и заголовок H2-подсказку (строка 6):
```html
<!-- строка 6 -->
  <span class="hint">конвейер отбора: поиск дропов → проверка → твоё решение → покупка руками</span></h2>
<!-- строка 11 (plate) -->
    <div class="plate">↻ Поиск дропов — найти кандидатов <span class="mod">M1a · 4 источника (выбор в Настройках)</span></div>
<!-- строка 19 (кнопка) -->
        <button class="btn-amber">↻ Найти дропы</button>
<!-- строка 28 (plate) -->
    <div class="plate">▶ Запуск проверки — оценить очередь <span class="mod">M1b · RD → возраст → РКН/блэклист → эхо → Wayback</span></div>
<!-- строка 39 (кнопка) -->
        <button class="btn-amber">▶ Запустить проверку</button>
```
И эмпти-строку (строки ~219–220):
```html
<div class="empty">Пусто{{ ' по фильтру «' + f_status + '»' if f_status }}. Начни со станции
  «↻ Поиск дропов» — фид принесёт свежие дропы, затем «▶ Запуск проверки» их оценит.</div>
```

- [ ] **Step 6: queue.html — бейдж статуса заказа**

В `backend/app/templates/queue.html` заменить бейдж (строка ~30–31) — текст через фильтр, класс и title-тултип оставить:
```html
      <td><span class="badge {{ _b.get(o.status, 'b-default') }}"
            title="{{ {'pending_confirm':'ждёт подтверждения человеком','ordered':'отправлен провайдеру','caught':'домен пойман','failed':'ошибка/провайдер не готов','cancelled':'заявка снята'}.get(o.status, o.status) }}">{{ o.status|status_ru }}</span></td>
```
И эмпти (строка ~63) — бейдж approved:
```html
<div class="empty">Очередь пуста. Одобренные домены (статус <span class="badge b-approved">{{ 'approved'|status_ru }}</span>)
```

- [ ] **Step 7: site.html + dashboard.html — бейджи сайта/страницы**

В `backend/app/templates/site.html` заменить текст бейджа сайта (строка ~12) и страницы (строка ~134) на фильтр:
```html
<!-- строка ~12 -->
  <span class="badge b-{{ site.status if site.status in ['provisioning','content','published'] else 'default' }}"
        title="{{ {'provisioning':'ждёт провижна (шаг 3)','content':'инфраструктура готова — контент и редактура','published':'страницы опубликованы'}.get(site.status, site.status) }}">{{ site.status|status_ru }}</span>
<!-- строка ~134 -->
      <td><span class="badge b-{{ p.status if p.status in ['draft','edited','published'] else 'default' }}"
            title="{{ {'draft':'AI-черновик — публикация его не возьмёт','edited':'вычитано человеком — готово к публикации','published':'опубликовано на сайте'}.get(p.status, p.status) }}">{{ p.status|status_ru }}</span></td>
```
В `backend/app/templates/dashboard.html` заменить текст бейджей сайта (строки ~67–68 и ~82):
```html
<!-- строка ~67–68 -->
      <span class="badge b-{{ s.site.status if s.site.status in ['provisioning','content','published'] else 'default' }}"
            title="{{ {'provisioning':'ждёт провижна: Cloudflare + vhost','content':'инфраструктура готова — этап контента','published':'страницы опубликованы'}.get(s.site.status, s.site.status) }}">{{ s.site.status|status_ru }}</span>
<!-- строка ~82 -->
<div class="card empty">Сайтов пока нет. Путь: домен со статусом <span class="badge b-purchased">{{ 'purchased'|status_ru }}</span> → кнопка «＋ создать сайт» на экране <a href="/domains">Домены</a>.</div>
```

- [ ] **Step 8: Прогнать тест + весь набор**

Run: `.venv/bin/python -m pytest backend/tests/test_web_fixes.py::test_domains_localized_labels -q`
Expected: PASS.
Затем весь набор: `.venv/bin/python -m pytest backend/tests/ -q` → все проходят; pyflakes чисто.

- [ ] **Step 9: Commit**

```bash
git add backend/app/templates/ backend/tests/test_web_fixes.py
git commit -m "лицо: локализация статусов/лейнов/reject в шаблонах (фильтры), станции по-русски"
```

---

## Task 3: Визуальный прогрессбар

**Files:**
- Modify: `backend/app/templates/base.html` (CSS `.progress` в блок `<style>`)
- Modify: `backend/app/templates/domains.html` (разметка `#prog` + JS `poll`)

**Interfaces:**
- Consumes: `/run/{job}/progress` → `{running, done, total, current, error}` (не менять).
- Produces: CSS-класс `.progress` (переиспользуемый компонент бара).

- [ ] **Step 1: CSS-компонент `.progress` в base.html**

В `backend/app/templates/base.html`, в конце блока `<style>` (перед `</style>`, после правила `code {…}` на строке ~277) добавить:
```css
  /* ---- прогресс длинных задач: трек + заливка + подпись/процент ---- */
  .progress { display:none; align-items:center; gap:12px; margin:10px 0 4px;
              padding:9px 14px; border:1px solid var(--line); border-radius:var(--r);
              background:var(--panel); box-shadow:var(--shadow); }
  .progress.show { display:flex; }
  .progress .track { flex:1; height:10px; border-radius:999px; background:var(--panel2);
                     border:1px solid var(--line); overflow:hidden; min-width:120px; }
  .progress .fill { height:100%; width:0; border-radius:999px; background:var(--amber);
                    transition:width .3s ease; }
  .progress .lbl { font-size:12.5px; color:var(--mut); white-space:nowrap; }
  .progress .lbl b { color:var(--ink); font-weight:600; }
  .progress .pct { font-family:var(--mono); font-size:12px; color:var(--mut);
                   font-variant-numeric:tabular-nums; white-space:nowrap; }
  .progress.done .fill { background:var(--ok); width:100%; }
  .progress.done .pct  { color:var(--ok); }
  .progress.err  { border-color:#f2c4c4; background:var(--bad-soft); }
  .progress.err .fill  { background:var(--bad); }
  .progress.err .lbl, .progress.err .pct { color:#a32626; }
  /* неопределённый режим (total неизвестен, но задача идёт): бегущая полоса */
  .progress.indet .fill { width:35%; }
  @media (prefers-reduced-motion:no-preference) {
    .progress.indet .fill { animation:indet 1.1s ease-in-out infinite; }
    @keyframes indet { 0%{margin-left:-35%} 100%{margin-left:100%} }
  }
```

- [ ] **Step 2: Разметка `#prog` в domains.html**

В `backend/app/templates/domains.html` заменить строку 50 (`<div id="prog" class="hint" …>`) на структуру бара:
```html
<div id="prog" class="progress">
  <span class="lbl"></span>
  <div class="track"><div class="fill"></div></div>
  <span class="pct"></span>
</div>
```

- [ ] **Step 3: Апгрейд JS `poll` (терминальный контракт байт-в-байт)**

В `backend/app/templates/domains.html` заменить весь `<script>`-блок (строки ~51–78) на:
```html
<script>
// Терминальный контракт (см. services/jobs.py): error ПЕРВЫМ (иначе частичный done/total
// маскирует падение), терминал = running==false, total==0 && running = неопределённый режим.
// Меняем только представление — рисуем визуальный бар (см. .progress в base.html).
var JOB_RU = {discovery:'Поиск дропов', score:'Запуск проверки'};
function bar(){ return document.getElementById('prog'); }
function render(job, p){
  var el = bar(), lbl = el.querySelector('.lbl'), fill = el.querySelector('.fill'),
      pct = el.querySelector('.pct'), name = JOB_RU[job] || job;
  el.className = 'progress show';                       // сброс состояний
  if (p.error){ el.classList.add('err');               // стоп: без reload, оператор видит
    lbl.innerHTML='<b>'+name+'</b> — ошибка'; pct.textContent=''; fill.style.width='100%';
    fill.style.marginLeft='0'; return; }
  if (p.running){
    if (p.total>0){ var d=Math.round(p.done/p.total*100);
      fill.style.width=d+'%'; fill.style.marginLeft='0';
      lbl.innerHTML='<b>'+name+'</b>'+(p.current?' — '+p.current:'');
      pct.textContent=p.done+'/'+p.total;
    } else { el.classList.add('indet');                // total неизвестен — бегущая полоса
      fill.style.width=''; lbl.innerHTML='<b>'+name+'</b>'+(p.current?' — '+p.current:'');
      pct.textContent='…'; }
    return;
  }
  el.classList.add('done');                            // терминал без ошибки — успех
  lbl.innerHTML='<b>'+name+'</b> — готово'; pct.textContent=p.done;
}
function poll(job){
  fetch('/run/'+job+'/progress').then(r=>r.json()).then(p=>{
    render(job, p);
    if (p.error) return;                               // стоп на ошибке
    if (p.running){ setTimeout(()=>poll(job), 1500); }
    else { setTimeout(()=>location.reload(), 800); }   // успех — обновить таблицу
  }).catch(()=>{                                        // сеть моргнула — не молчим, повторяем
    var el=bar(), lbl=el.querySelector('.lbl');
    el.className='progress show'; lbl.innerHTML='<b>'+(JOB_RU[job]||job)+'</b> — нет связи, повторяю…';
    setTimeout(()=>poll(job), 1500);
  });
}
// старт-проверка: если джоб уже идёт (после сабмита) — поллим; если упал, пока оператора
// не было (перезагрузка) — показываем ошибку сразу, не тонет молча.
['discovery','score'].forEach(j => fetch('/run/'+j+'/progress').then(r=>r.json())
  .then(p=>{ if (p.running) poll(j); else if (p.error) render(j, p); }).catch(()=>{}));
</script>
```

- [ ] **Step 4: Прогнать серверный контракт + весь набор**

Прогрессбар — JS, юнит-тестами не покрываем; серверный контракт `/run/{job}/progress` уже покрыт `test_jobs.py`. Убедиться, что ничего не сломалось:
Run: `.venv/bin/python -m pytest backend/tests/ -q`
Expected: все проходят (число как в Task 2). pyflakes чисто.

- [ ] **Step 5: Визуальная проверка глазами (Playwright)**

Ручной шаг (как в Спек 1): поднять локальный serve на офлайн-SQLite (скрипт-паттерн из `conftest.py`: JSONB→JSON hook + импорт моделей + `create_all` + `SessionLocal.configure`), засеять пару discovered-доменов, запустить Discovery/Score, снять скриншот идущего бара (заливка + процент/неопределённый) и завершённого (зелёный «готово»). Проверить: светлая CMS, бар в стиле карточек, состояния done/err читаемы. Контролирующий агент делает это сам после ревью кода.

- [ ] **Step 6: Commit**

```bash
git add backend/app/templates/base.html backend/app/templates/domains.html
git commit -m "лицо: визуальный прогрессбар (CSS .progress + апгрейд poll JS, контракт сохранён)"
```

---

## Task 4: Умный дашборд /diag

**Files:**
- Modify: `backend/app/services/diagnostics.py` (`_spec` кортеж +module/critical; `_run_one`/`run_diagnostics` прокидка)
- Modify: `backend/app/api/panel.py` (`diag_view` — `crit_down` + группировка данных)
- Modify: `backend/app/templates/diag.html` (группы по модулям + сводка + бар отклика)
- Test: `backend/tests/test_web_fixes.py`

**Interfaces:**
- Consumes: `run_diagnostics()` возвращает список диктов; каждый теперь с `module`/`critical`.
- Produces: результат-дикт `_run_one` содержит `module: str`, `critical: bool`.
- Produces: `diag_view` передаёт в шаблон `crit_down: list[str]` (labels упавших критичных).

- [ ] **Step 1: Тест — module/critical в результате + crit_down**

В `backend/tests/test_web_fixes.py` добавить:
```python
def test_diag_spec_has_module_and_critical():
    from app.services.diagnostics import _spec
    for row in _spec():
        assert len(row) == 7                      # key,label,role,need_cred,module,critical,fn
        key, label, role, need_cred, module, critical, fn = row
        assert module in ("M1", "M3", "M4", "M5", "инфра")
        assert isinstance(critical, bool)


def test_run_diagnostics_propagates_module_critical(monkeypatch):
    from app.services import diagnostics as dg
    spec = [("x", "X", "M1 · тест", "1", "M1", True, lambda: True)]
    out = dg.run_diagnostics(specs=spec)
    assert out[0]["module"] == "M1" and out[0]["critical"] is True and out[0]["status"] == "ok"


def test_diag_view_flags_critical_down(client, monkeypatch):
    from app.services import diagnostics as dg
    spec = [("a", "Крит", "M1 · крит", "1", "M1", True, lambda: (_ for _ in ()).throw(RuntimeError("down"))),
            ("b", "Опц", "M3 · опц", "1", "M3", False, lambda: (_ for _ in ()).throw(RuntimeError("down")))]
    monkeypatch.setattr(dg, "_spec", lambda: spec)
    html = client.get("/diag").text
    assert "Крит" in html                          # упавший критичный виден в сводке
```

- [ ] **Step 2: Прогнать — падает (кортеж 5-местный)**

Run: `.venv/bin/python -m pytest backend/tests/test_web_fixes.py -k "diag_spec or propagates or critical_down" -q`
Expected: FAIL (`assert len(row) == 7` — сейчас 5).

- [ ] **Step 3: Расширить `_spec()` кортежами (+module/critical)**

В `backend/app/services/diagnostics.py` заменить `_spec()` (весь return-список). Формат `(key, label, role, need_cred, module, critical, fn)`:
```python
    return [
        ("cloudflare", "Cloudflare", "M3 · зоны/DNS", settings.CLOUDFLARE_API_TOKEN, "M3", False,
         lambda: __import__("app.integrations.cloudflare", fromlist=["x"]).CloudflareClient().ping()),
        ("aapanel", "aaPanel", "M3 · vhost/файлы", settings.AAPANEL_API_KEY, "M3", False,
         lambda: __import__("app.integrations.aapanel", fromlist=["x"]).AaPanelClient().ping()),
        ("llm", "LiteLLM", "M4 · контент", settings.LLM_BASE_URL, "M4", True,
         lambda: __import__("app.integrations.llm", fromlist=["x"]).LlmClient().ping()),
        ("searxng", "SearXNG", "M1/M5 · SERP/индекс", settings.SEARXNG_URL, "M5", False,
         lambda: __import__("app.integrations.searxng", fromlist=["x"]).SearxngClient().ping()),
        ("backorder", "Backorder", "M1 · discovery", "1", "M1", True,
         lambda: __import__("app.integrations.backorder", fromlist=["x"]).BackorderClient().ping()),
        ("wayback", "Wayback", "M1 · история", "1", "M1", True,
         lambda: __import__("app.integrations.wayback", fromlist=["x"]).WaybackClient().ping()),
        ("rkn", "РКН (antizapret)", "M1 · блок-лист", settings.RKN_SOURCE_URL, "M1", True,
         lambda: __import__("app.integrations.rkn", fromlist=["x"]).RknClient().ping()),
        ("aparser", "A-Parser", "M1 · whois/лейн + fetch", settings.APARSER_API_KEY, "M1", True,
         lambda: __import__("app.integrations.aparser", fromlist=["x"]).AParserClient().ping()),
        ("db", "PostgreSQL", "БД конвейера", settings.DATABASE_URL, "инфра", True, _db_ping),
    ]
```
Обновить докстринг `_spec` первой строкой на: `"""Список проверок: (key, label, role, need_cred, module, critical, factory→ping)."""` (остальное сохранить).

- [ ] **Step 4: Прокинуть module/critical в `_run_one`, `run_diagnostics` и self-check**

В `backend/app/services/diagnostics.py` заменить сигнатуру и тело `_run_one`:
```python
def _run_one(key, label, role, need_cred, module, critical, fn) -> dict:
    base = {"key": key, "label": label, "role": role, "module": module, "critical": critical}
    if not need_cred:
        return {**base, "status": "skip", "ms": None, "error": "нет кредов в .env"}
    t0 = time.monotonic()
    try:
        ok = bool(fn())
        return {**base, "status": "ok" if ok else "fail",
                "ms": int((time.monotonic() - t0) * 1000), "error": None}
    except Exception as e:  # noqa: BLE001 — любой сбой интеграции = красный, не 500
        return {**base, "status": "fail",
                "ms": int((time.monotonic() - t0) * 1000),
                "error": f"{type(e).__name__}: {e}"[:200]}
```
`run_diagnostics` распаковывает кортеж явно в ДВУХ местах + строит fallback-дикт на таймаут (он тоже должен нести `module`/`critical`, иначе группировка в шаблоне уронит проверку без модуля). Заменить тело `run_diagnostics` целиком (докстринг сохранить):
```python
    specs = specs if specs is not None else _spec()
    results: dict[int, dict] = {}
    ex = ThreadPoolExecutor(max_workers=len(specs) or 1)
    try:
        futs = {ex.submit(_run_one, k, lbl, role, cred, mod, crit, fn): i
                for i, (k, lbl, role, cred, mod, crit, fn) in enumerate(specs)}
        for fut, i in futs.items():
            k, lbl, role, cred, mod, crit, fn = specs[i]
            try:
                results[i] = fut.result(timeout=PING_TIMEOUT)
            except FutTimeout:
                results[i] = {"key": k, "label": lbl, "role": role, "module": mod,
                              "critical": crit, "status": "fail",
                              "ms": int(PING_TIMEOUT * 1000), "error": f"timeout > {PING_TIMEOUT:.0f}s"}
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    return [results[i] for i in range(len(specs))]
```
И обновить `__main__` self-check (его specs — 5-местные кортежи, сломаются на новой распаковке) — заменить список specs на 7-местные:
```python
    specs = [
        ("a", "A", "role", "1", "M1", True, lambda: True),
        ("b", "B", "role", "1", "M1", True, lambda: False),
        ("c", "C", "role", "", "M1", False, lambda: True),                        # skip: нет кред
        ("d", "D", "role", "1", "M1", True, lambda: (_ for _ in ()).throw(RuntimeError("boom"))),
        ("e", "E", "role", "1", "M1", True, lambda: time.sleep(2)),               # timeout
    ]
```

- [ ] **Step 5: `diag_view` — считать crit_down**

В `backend/app/api/panel.py`, `diag_view` (строки 171–182) заменить тело:
```python
@router.get("/diag", response_class=HTMLResponse)
def diag_view(request: Request):
    from app.services import version as _version
    from app.services.diagnostics import run_diagnostics, PING_TIMEOUT
    checks = run_diagnostics()
    ok = sum(1 for c in checks if c["status"] == "ok")
    crit_down = [c["label"] for c in checks if c.get("critical") and c["status"] == "fail"]
    return templates.TemplateResponse(request, "diag.html", {
        "active": "diag", "checks": checks, "ok": ok, "total": len(checks),
        "crit_down": crit_down, "timeout": PING_TIMEOUT,
        "repo": settings.GITHUB_REPO, "can_pull": bool(settings.GITHUB_TOKEN),
        "version": _version.current_version(),
    })
```

- [ ] **Step 6: diag.html — сводка + группировка по модулям + бар отклика**

В `backend/app/templates/diag.html` заменить H2-подсказку (строка ~5) на сводку здоровья:
```html
<h2><span class="idx">◉</span> Диагностика интеграций
  <span class="hint">{{ ok }} / {{ total }} на связи ·
    {% if crit_down %}<b style="color:var(--bad)">критичные: {{ crit_down|join(', ') }} ✗</b>{% else %}<b style="color:var(--ok)">критичные: все ✓</b>{% endif %}</span></h2>
```
Заменить блок таблицы (строки ~19–36, от `<div class="wrap"...>` до закрывающего `</div>` таблицы) на группировку по модулям. Каждый модуль — свой заголовок и таблица:
```html
{% set mod_titles = {'M1':'M1 — поиск и скоринг','M3':'M3 — провижн','M4':'M4 — контент','M5':'M5 — публикация','инфра':'Инфраструктура'} %}
{% for mod in ['M1','M3','M4','M5','инфра'] %}
  {% set group = checks|selectattr('module','equalto',mod)|list %}
  {% if group %}
  <h2 style="font-size:13px; margin:20px 0 8px; color:var(--mut)">{{ mod_titles[mod] }}</h2>
  <div class="wrap" style="margin-bottom:8px">
  <table>
    <thead><tr>
      <th>сервис</th><th>зачем он конвейеру</th><th>статус</th><th class="num">отклик</th><th>детали ошибки</th>
    </tr></thead>
    <tbody>
    {% for c in group %}
      <tr>
        <td class="dom">{{ c.label }}{% if c.critical %}<span class="hint" title="критичная зависимость конвейера" style="color:var(--amber2)"> ●</span>{% endif %}</td>
        <td class="hint">{{ c.role }}</td>
        <td>
          {% if c.status == 'ok' %}<span class="led led-ok"></span>OK
          {% elif c.status == 'skip' %}<span class="led led-off"></span><span class="hint" title="в .env нет кредов — пинг бессмысленен">пропущен</span>
          {% elif c.critical %}<span class="led led-bad"></span><b style="color:var(--bad)">FAIL</b>
          {% else %}<span class="led led-bad"></span><span class="hint" title="опциональная зависимость — деградируемо">FAIL (опц.)</span>{% endif %}
        </td>
        <td class="num">
          {% if c.ms is not none %}<span class="hint">{{ c.ms }} мс</span>
            <span class="meter" style="width:48px" title="доля от таймаута пинга ({{ timeout|int }} с)"><i class="m-mid" style="width:{{ [ (c.ms/(timeout*10))|round|int, 100]|min }}%"></i></span>
          {% else %}<span class="hint">—</span>{% endif %}
        </td>
        <td class="hint" style="max-width:420px; overflow:hidden; text-overflow:ellipsis">{{ c.error or '' }}</td>
      </tr>
    {% endfor %}
    </tbody>
  </table>
  </div>
  {% endif %}
{% endfor %}
```
Пояснение легенды под таблицами (строка ~37–39) — оставить как есть, дополнив маркером критичности:
```html
<p class="hint" style="margin:0 0 22px"><span class="led led-ok"></span>OK — отвечает и креды верны ·
  <span class="led led-bad"></span>FAIL — не отвечает/отверг (текст справа) ·
  <span class="led led-off"></span>пропущен — заполни креды в <code>.env</code> ·
  <span style="color:var(--amber2)">●</span> критичная зависимость (без неё конвейер стоит).</p>
```
(Отклик: `timeout` — это `PING_TIMEOUT` в секундах; `c.ms` в миллисекундах. `c.ms/(timeout*10)` даёт долю в процентах от таймаута; `m-low` если больше половины таймаута — `c.ms > timeout*500`.)

- [ ] **Step 7: Прогнать тесты + весь набор**

Run: `.venv/bin/python -m pytest backend/tests/test_web_fixes.py -k "diag" -q`
Expected: PASS.
Затем весь набор: `.venv/bin/python -m pytest backend/tests/ -q` → все проходят; pyflakes чисто.

- [ ] **Step 8: Commit**

```bash
git add backend/app/services/diagnostics.py backend/app/api/panel.py backend/app/templates/diag.html backend/tests/test_web_fixes.py
git commit -m "лицо: /diag как дашборд — модули + критичность + сводка здоровья + бар отклика"
```

---

## Task 5: Мелкий полиш (три долга Спек 1)

**Files:**
- Modify: `backend/app/templates/domains.html` (тултип: `price_checked_at`)
- Modify: `backend/app/api/panel.py` (`score_one_action` — unresolved-флеш)
- Modify: `backend/app/integrations/backorder.py` (`get_tariffs` — `.get("id")`)
- Test: `backend/tests/test_web_fixes.py`, `backend/tests/test_pricing.py`

**Interfaces:**
- Consumes: `Domain.price_checked_at` (Спек 1); `score_domain(...)` возвращает `{"unresolved": True, "status": "discovered", "domain": …}` без `score` при неопределённой приобретаемости.

- [ ] **Step 1: Тест — single-score unresolved даёт русский флеш, не «(None)»**

В `backend/tests/test_web_fixes.py` добавить:
```python
def test_single_score_unresolved_flash(client, monkeypatch):
    import app.db as db
    from app.models.domain import Domain
    from app.services import scoring
    with db.SessionLocal() as s:
        s.add(Domain(domain="unres.ru", source="cctld", status="discovered", lane=None)); s.commit()
        did = s.execute(__import__("sqlalchemy").select(Domain.id).where(Domain.domain=="unres.ru")).scalar_one()
    monkeypatch.setattr(scoring, "score_domain",
                        lambda domain_id: {"domain": "unres.ru", "status": "discovered", "unresolved": True})
    r = client.post(f"/domains/{did}/score", follow_redirects=False)
    assert r.status_code in (302, 303)
    loc = r.headers["location"]
    assert "None" not in loc and ("whois" in loc or "%" in loc)   # русский флеш, без "(None)"
```

- [ ] **Step 2: Прогнать — падает (сейчас «discovered (None)»)**

Run: `.venv/bin/python -m pytest backend/tests/test_web_fixes.py::test_single_score_unresolved_flash -q`
Expected: FAIL (в location есть `None`).

- [ ] **Step 3: `score_one_action` — ветка unresolved**

В `backend/app/api/panel.py` заменить тело `score_one_action` (строки 283–290). Статус во флеше остаётся сырым (техническое сообщение оператору; локализация бейджей — в таблице). Ключевое — ветка `if out.get("unresolved")` до строки формата:
```python
def score_one_action(domain_id: int):
    from app.services import scoring
    try:
        out = scoring.score_domain(domain_id)
        if out.get("unresolved"):
            return _back("/domains", msg=f"{out.get('domain', domain_id)}: whois не определил — "
                                         "домен остался в поиске, перепроверю на следующем прогоне")
        return _back("/domains", msg=f"скор: {out.get('domain', domain_id)} -> "
                                     f"{out.get('status')} ({out.get('score')})")
    except Exception as e:  # noqa: BLE001
        return _back("/domains", err=f"score #{domain_id}: {e}")
```

- [ ] **Step 4: `price_checked_at` в тултип домена**

В `backend/app/templates/domains.html`, в тултипе бейджа-источника (строка ~150, после блока цены) добавить сегмент обновления:
```html
{% if d.acquire_price %} · цена {{ '%.0f'|format(d.acquire_price|float) }}{% endif %}{% if d.price_checked_at %} · обновлено {{ d.price_checked_at.strftime('%d.%m') }}{% endif %}"
```
(Заменяется закрывающая часть `title="…"` — добавляется `{% if d.price_checked_at %}…{% endif %}` перед закрывающей кавычкой.)

- [ ] **Step 5: `period[0].get("id")` хардненинг в backorder**

В `backend/app/integrations/backorder.py`, в `get_tariffs`, заменить строку возврата `period_id` (bracket-доступ `period[0]["id"]` → `.get("id")` с None-гардом). Найти строки:
```python
        return {"price": price, "price_id": str(d.get("id") or "") or None,
                "period_id": str(period[0]["id"]) if period and isinstance(period[0], dict) else None}
```
и заменить на:
```python
        return {"price": price, "price_id": str(d.get("id") or "") or None,
                "period_id": (str(period[0].get("id")) if period and isinstance(period[0], dict)
                              and period[0].get("id") is not None else None)}
```

- [ ] **Step 6: Тест хардненинга в test_pricing.py**

В `backend/tests/test_pricing.py` добавить:
`get_tariffs` берёт JSON через `self.request("GET", …).json()`. Мокаем `request`, возвращая
ответ с `.json()` → тариф, у которого `period[0]` без ключа `"id"`:
```python
def test_get_tariffs_survives_period_without_id(monkeypatch):
    from app.integrations import backorder

    class _Resp:
        def json(self):
            return {"id": 7, "price": 490, "period": [{"cost": 490}]}   # period без "id"

    c = backorder.BackorderClient()
    monkeypatch.setattr(c, "request", lambda *a, **k: _Resp())
    out = c.get_tariffs()                 # не должно падать KeyError
    assert out["period_id"] is None       # мягкий None вместо KeyError
```

- [ ] **Step 7: Прогнать — проходит + весь набор**

Run: `.venv/bin/python -m pytest backend/tests/test_web_fixes.py::test_single_score_unresolved_flash backend/tests/test_pricing.py -q`
Expected: PASS.
Затем весь набор: `.venv/bin/python -m pytest backend/tests/ -q` → все проходят; pyflakes чисто.

- [ ] **Step 8: Commit**

```bash
git add backend/app/templates/domains.html backend/app/api/panel.py backend/app/integrations/backorder.py backend/tests/
git commit -m "лицо: полиш — price_checked_at в тултип, unresolved русский флеш, period.get(id)"
```

---

## Самопроверка плана (для контролирующего агента)

- **Спек §A** → Task 1 (labels.py) + Task 2 (применение). §B → Task 3. §C → Task 4. §D → Task 5. §E/§F/§G — инварианты в Global Constraints + распределены по задачам.
- **Порядок:** Task 1 (фундамент) обязателен до Task 2 (шаблоны зовут фильтры). Task 3/4/5 независимы между собой, но после Task 1 (Task 4/5 фильтры не нужны, но пусть labels уже есть). Дозволен порядок 1→2→3→4→5.
- **Риск Task 4 Step 4:** распаковка спека в `run_diagnostics` — прочитать реальный код (использует ли `*spec` или явную 5-местную распаковку). Наиболее вероятен фикс-раунд здесь.
- **Риск Task 5 Step 6:** форма транспорта `get_tariffs` — прочитать реальный `backorder.py`, адаптировать мок.
- **Ручной шаг:** Task 3 Step 5 — визуальная проверка бара глазами (Playwright), делает контролирующий агент.
