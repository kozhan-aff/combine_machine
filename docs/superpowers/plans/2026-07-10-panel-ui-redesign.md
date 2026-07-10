# Панель КОМБАЙН: редизайн (холодная минимальная CMS) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Перекрасить и уплотнить панель в холодную минимальную CMS (нейтральная палитра + синий акцент, IBM Plex Sans, прогрессивный шильдик, воронка-лента) без единого изменения бэкенд-логики.

**Architecture:** Весь визуальный контракт живёт в одном файле `backend/app/templates/base.html` (`<style>` + каркас-разметка сайдбара). Контент-шаблоны наследуют классы контракта. Меняем только представление: токены/шрифты/компоненты в base.html + точечная переверстка вербозных блоков (сайдбар-подписи, станции, воронка) в контент-шаблонах. Ни один роут, обработчик формы, `name=`/`action=` не трогается.

**Tech Stack:** FastAPI + Jinja2, инлайн-CSS в base.html, Google Fonts (IBM Plex Sans + JetBrains Mono + Unbounded), офлайн-харнесс pytest (TestClient рендерит шаблоны — регресс-гейт), grep-инварианты, Playwright-скриншоты для визуального ревью.

## Global Constraints

Каждая задача неявно включает эти требования (дословно из спеки `docs/superpowers/specs/2026-07-10-panel-ui-redesign.md` и CLAUDE.md):

- **Светлая CMS.** Новая палитра светлая; тёмное/индустриальное ЗАПРЕЩЕНО.
- **Прогрессивный шильдик.** Инвариант «каждый контрол подписан» СОХРАНЯЕТСЯ: лейбл всегда виден / фраза в `title` при наведении / полный абзац в `<details>` по клику. Не удалять пояснения — прятать за прогрессивное раскрытие.
- **CSS-контракт един.** Весь CSS в `base.html`. Контент-шаблоны — только семантика + классы контракта. НЕ расширять контракт новыми inline-стилями на месте (существующие inline-стили по ходу можно подчистить, но не добавлять новые цвета/токены в шаблоны).
- **UI на русском.** Все подписи, тултипы, `<summary>` — по-русски.
- **Только представление.** НЕ трогать роуты, обработчики форм, значения `name=`/`action=`, Jinja-переменные контекста, блоки `{% block %}`. Хард-гейты (деньги/редактура) вне области.
- **Кириллица обязательна** (IBM Plex Sans её несёт).
- **Доступность.** `:focus-visible` остаётся (в акцентном цвете); контраст текста на фоне ≥ AA; `@media (prefers-reduced-motion)` уважать (уже есть — не ломать).
- **Регресс-гейт после КАЖДОЙ задачи:** `.venv/bin/python -m pytest backend/tests/ -q` → `207 passed` (TestClient рендерит шаблоны — падение = сломанная разметка). CSS/HTML юнит-тестами не покрывается — гейты этого плана: pytest-рендер + grep-инварианты + визуальный скриншот-ревью.

### Токен-таблица (эталон — все задачи ссылаются сюда)

```
--bg:#f7f8fa;        --panel:#ffffff;     --panel2:#f2f4f7;
--line:#e6e8ec;      --line2:#d5d9e0;
--ink:#1a1d23;       --mut:#5c626d;       --dim:#9aa1ad;
--acc:#2563c9;       --acc2:#1d4ed8;      --acc-soft:#e8f0fe;
--ok:#137a43;        --ok-soft:#e3f4ea;
--bad:#c62b2b;       --bad-soft:#fbe9e9;
--warn:#9a6b0f;      --warn-soft:#fbf1da;
--info:#0e7490;      --info-soft:#e0f2f7;
--violet:#6d3fc4;    --violet-soft:#efe9fb;
--r:8px;             --r-sm:6px;          --shadow:0 1px 2px rgba(20,25,35,.04);
--ui:"IBM Plex Sans","Segoe UI Variable","SF Pro Text",system-ui,-apple-system,sans-serif;
--mono:"JetBrains Mono","Cascadia Mono",Consolas,"SF Mono",Menlo,ui-monospace,monospace;
--brand:"Unbounded","IBM Plex Sans",system-ui,sans-serif;
```

Роль акцента `--acc` (синий) = «система/действие»: primary-кнопки, активный пункт меню, ссылки, фокус, гейт-индикаторы. Статусные цвета (ok/bad/warn/info/violet) — семантические.

---

## Задачи и файлы

| Задача | Файлы | Дело |
|---|---|---|
| 1 | `base.html` (`<style>` + `<link>`) | холодные токены, IBM Plex Sans, полный рекрас всех компонентов |
| 2 | `base.html` (сайдбар CSS + nav-разметка) | компактный сайдбар: убрать вечные `.sub`, описания → `title`, 248→200px |
| 3 | `base.html` (+ `details.what` CSS), `domains.html`, `settings.html`, `autopilot.html` | станции: абзац `.what` → сворачиваемый `<details>` |
| 4 | `dashboard.html`, `base.html` (воронка CSS) | воронка-лента, сгруппированная по модулям, с гейт-точками |
| 5 | все шаблоны + `docs/DESIGN.md` (new) | `.btn-amber`→`.btn-acc`, зачистка стрэгглеров, дизайн-система в доке |

Порядок жёсткий: T1 (токены) — фундамент; T2/T3/T4 зависят от токенов T1; T5 — финальная зачистка. Выполнять 1→2→3→4→5.

---

### Task 1: base.html — холодные токены, IBM Plex Sans, полный рекрас

**Files:**
- Modify: `backend/app/templates/base.html` (строки 9 — `<link>` шрифтов; 13–26 — `:root`; и все правила `<style>`, ссылающиеся на старые токены/хардкод-хексы)

**Interfaces:**
- Produces: холодные CSS-переменные `--acc*`, `--ui`, обновлённые значения `--info/--violet/--r/--shadow` и т.д. — на них опираются T2–T4. Класс `.btn-amber` в этой задаче ОСТАЁТСЯ (перекрашивается в синий через `var(--acc)`, переименование — в T5). Класс `.stat .v.hot` остаётся (имя нейтральное; перекрашивается в acc).

- [ ] **Step 1: Заменить `<link>` Google Fonts (строка 9)**

Было:
```html
<link href="https://fonts.googleapis.com/css2?family=Golos+Text:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600;700&family=Unbounded:wght@500;700&display=swap" rel="stylesheet">
```
Стало:
```html
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600;700&family=Unbounded:wght@500;700&display=swap" rel="stylesheet">
```

- [ ] **Step 2: Заменить весь блок `:root` (строки 13–26) на холодные токены**

```css
  :root {
    --bg:#f7f8fa; --panel:#ffffff; --panel2:#f2f4f7;
    --line:#e6e8ec; --line2:#d5d9e0;
    --ink:#1a1d23; --mut:#5c626d; --dim:#9aa1ad;
    --acc:#2563c9; --acc2:#1d4ed8; --acc-soft:#e8f0fe;
    --ok:#137a43; --ok-soft:#e3f4ea;
    --bad:#c62b2b; --bad-soft:#fbe9e9;
    --warn:#9a6b0f; --warn-soft:#fbf1da;
    --info:#0e7490; --info-soft:#e0f2f7;
    --violet:#6d3fc4; --violet-soft:#efe9fb;
    --ui:"IBM Plex Sans","Segoe UI Variable","SF Pro Text",system-ui,-apple-system,sans-serif;
    --mono:"JetBrains Mono","Cascadia Mono",Consolas,"SF Mono",Menlo,ui-monospace,monospace;
    --brand:"Unbounded","IBM Plex Sans",system-ui,sans-serif;
    --r:8px; --r-sm:6px; --shadow:0 1px 2px rgba(20,25,35,.04);
  }
```

Также обновить комментарий-шапку `<style>` (строки 11–12): вместо «СВЕТЛАЯ CMS: тёплая бумага…» написать `/* СВЕТЛАЯ CMS: холодная нейтраль, белые карточки, синий акцент. Шрифты — Google Fonts, офлайн — фолбэк на системные. */`.

- [ ] **Step 3: Глобальные замены токен-ссылок во всём `<style>` (порядок важен — длинные первыми)**

Выполнить как четыре replace-all по base.html:
1. `var(--amber-soft)` → `var(--acc-soft)`
2. `var(--amber2)` → `var(--acc2)`
3. `var(--amber)` → `var(--acc)`
4. `var(--disp)` → `var(--ui)`

Это перекрашивает в синий/меняет шрифт все компоненты, ссылающиеся на переменные: ссылки, фокус, `::selection`, primary-кнопку `.btn-amber`, `.b-scored`/`.b-edited`, `.chip.on`, `.progress .fill`, `.led-todo`, `.src-bid`, `.stat .v.hot`, `h2 .idx`, `.brand b`, `ol.steps`, легенду, `.preview a`, `input:focus`, `label.f` и т.д. — всё через переменные.

- [ ] **Step 4: Семантические переопределения (после Step 3 эти правила стали синими, но по спеке должны быть иными)**

`.b-provisioning` (строка ~107) — сейчас `color:var(--acc2); background:var(--acc-soft);` → должно быть warn:
```css
  .b-provisioning { color:var(--warn); background:var(--warn-soft); }
```
`.meter i.m-mid` (строка ~176) — сейчас `background:var(--acc);` → warn:
```css
  .m-ok { background:var(--ok); } .m-mid { background:var(--warn); } .m-low { background:var(--bad); }
```

- [ ] **Step 5: Зачистить хардкод тёплых хексов (не через переменные)**

`.rl.on` (строка ~53) — `border-color:#f4d9c2;` → `border-color:#c9dbf7;`
`.rl.on .sub` (строка ~55) — `color:#a8622f;` → `color:var(--acc2);` (косметика; сам `.sub` удаляется в T2)
`.flash.warn` (строка ~85) — целиком:
```css
  .flash.warn { border-color:var(--warn); background:var(--warn-soft); color:var(--warn); }
```
`tbody tr:hover td` (строка ~163) — `background:#fbfaf6;` → `background:var(--panel2);`

(Зелёные/красные хексы `.flash.msg`/`.flash.err`, `.b-live`, `.btn-ok`, `.btn-bad`, `.btn-buy` — семантические статус-цвета, НЕ «тёплая бумага», их не трогаем.)

- [ ] **Step 6: Прогнать регресс-гейт (шаблоны рендерятся)**

Run: `.venv/bin/python -m pytest backend/tests/ -q`
Expected: `207 passed` (ни одна разметка не сломана — правки чисто в CSS-значениях).

- [ ] **Step 7: Grep-инварианты холодной палитры**

Run:
```bash
grep -c -- '--amber' backend/app/templates/base.html          # переменная amber
grep -c 'var(--disp)' backend/app/templates/base.html
grep -Eo '#f6f4ef|#e8e4d9|#f4d9c2|#fbfaf6|#fbf3dd|#ecd9a8|#7a5d14|#a8622f|Golos Text' backend/app/templates/base.html | sort -u
```
Expected: первые две команды → `0`; третья → пусто. (`.btn-amber` класс-имя ещё встретится в grep по `amber` без `--` — это ОК, переименуем в T5. Проверяем именно `--amber` переменную.)

- [ ] **Step 8: Визуальный скриншот-ревью**

Поднять локальный serve и снять экраны Пульт / Домены / Настройки:
```bash
# из backend/: .venv/bin/uvicorn app.main:app --port 8010   (или docker compose up)
```
Playwright-плагином (`browser_navigate` → `browser_take_screenshot`) снять `/`, `/domains`, `/settings`. Глазами сверить: фон холодный серый (не бумага), акцент синий (ни следа оранжевого), шрифт IBM Plex Sans, данные на месте. Если live-БД под рукой нет — отрендерить те же роуты через TestClient в статические .html и снять их.

- [ ] **Step 9: Commit**

```bash
git add backend/app/templates/base.html
git commit -m "feat(panel): холодные токены + IBM Plex Sans + рекрас контракта (T1)"
```

---

### Task 2: Сайдбар — компактный, без вечных подписей

**Files:**
- Modify: `backend/app/templates/base.html` (`.shell` grid ~36; `.rl`/`.rl .sub` CSS ~46–68; nav-разметка `<nav class="rail">` ~311–336)

**Interfaces:**
- Consumes: холодные токены из T1.
- Produces: сайдбар шириной 200px, `.rl` в одну строку; класс `.sub` и его CSS удалены. `.gates`-блок внизу сохраняется.

- [ ] **Step 1: Сузить колонку сайдбара**

`.shell` (строка ~36): `grid-template-columns:248px 1fr;` → `grid-template-columns:200px 1fr;`

- [ ] **Step 2: Переписать `.rl` под одну строку и удалить `.sub`-правила**

Заменить блок правил `.rl`/`.rl:hover`/`.rl .n`/`.rl .sub`/`.rl.on`/`.rl.on, .rl.on .n`/`.rl.on .sub` (строки ~47–55) на:
```css
  .rl { display:flex; align-items:baseline; padding:7px 10px; border-radius:var(--r);
        color:var(--ink); font-size:13.5px; font-weight:600; border:1px solid transparent; }
  .rl:hover { background:var(--panel2); text-decoration:none; }
  .rl .n { font-family:var(--mono); font-size:10px; color:var(--dim); font-weight:500;
           margin-right:9px; flex:none; }
  .rl.on { background:var(--acc-soft); border-color:#c9dbf7; }
  .rl.on, .rl.on .n { color:var(--acc2); }
```

В мобильном брейкпоинте (строка ~67) удалить правило `.rl .sub { display:none; }` (класс больше не существует).

- [ ] **Step 3: Переписать nav-разметку — описание в `title`, `.sub` убрать**

Для КАЖДОГО из 8 пунктов `<nav class="rail">` (строки ~311–336) перенести текст `.sub` в `title=` ссылки и убрать вложенные `<span>`. Образец (Пульт → применить ко всем):

Было:
```html
    <a class="rl {{ 'on' if active=='dash' }}" href="/">
      <span><span class="n">00</span>Пульт</span>
      <span class="sub">сводка воронки + что делать дальше</span></a>
```
Стало:
```html
    <a class="rl {{ 'on' if active=='dash' }}" href="/" title="сводка воронки + что делать дальше">
      <span class="n">00</span>Пульт</a>
```

Так же для остальных семи (Офферы / Домены·M1 / Выкуп / Сайты·M3–M5 / Диагностика / Настройки / Автопилот) — брать текущий текст их `.sub` как значение `title`, индекс `.n` и название оставить в теле ссылки.

- [ ] **Step 4: Регресс-гейт + grep**

Run: `.venv/bin/python -m pytest backend/tests/ -q` → `207 passed`
Run: `grep -c 'class="sub"' backend/app/templates/base.html` → `0`

- [ ] **Step 5: Скриншот-ревью**

Снять `/` (Playwright). Сверить: сайдбар уже (~200px), пункты в одну строку, описание всплывает в тултипе при наведении (`browser_hover` на пункт → тултип). `.gates`-блок внизу на месте.

- [ ] **Step 6: Commit**

```bash
git add backend/app/templates/base.html
git commit -m "feat(panel): компактный сайдбар, описания в title (T2)"
```

---

### Task 3: Станции — абзац `.what` в сворачиваемый `<details>`

**Files:**
- Modify: `backend/app/templates/base.html` (правила `.station .what` / `.station .what b` ~245–247 → заменить на `details.what`/`.what-body`)
- Modify: `backend/app/templates/domains.html` (блоки `class="what"` ~13, 29)
- Modify: `backend/app/templates/settings.html` (блоки `class="what"` — 8 станций)
- Modify: `backend/app/templates/autopilot.html` (блоки `class="what"`)

**Interfaces:**
- Consumes: холодные токены T1, класс `.station` из контракта.
- Produces: `<details class="what">` + `<summary>зачем это</summary>` + `<div class="what-body">` — свёрнутый по умолчанию блок пояснения. (diag.html / offers.html — короткие формы, их станции НЕ трогаем: там `.what` короткий, сворачивать нечего.)

- [ ] **Step 1: Заменить CSS `.station .what` на `details.what` (base.html ~245–247)**

Было:
```css
  .station .what { color:var(--mut); font-size:12.5px; line-height:1.6; padding:12px 16px 4px;
    flex:1; }
  .station .what b { color:var(--ink); font-weight:600; }
```
Стало:
```css
  .station details.what { border-top:1px solid var(--line); margin-top:auto; }
  .station details.what > summary { cursor:pointer; list-style:none; user-select:none;
    padding:9px 16px; font-size:11.5px; font-weight:600; color:var(--dim); }
  .station details.what > summary::-webkit-details-marker { display:none; }
  .station details.what > summary::before { content:"▸ "; color:var(--acc); }
  .station details.what[open] > summary::before { content:"▾ "; }
  .station details.what > summary:hover { color:var(--ink); }
  .what-body { color:var(--mut); font-size:12.5px; line-height:1.6; padding:2px 16px 13px; }
  .what-body b { color:var(--ink); font-weight:600; }
```

- [ ] **Step 2: Обернуть `.what` в domains.html (2 станции, ~13 и ~29)**

Каждый `<div class="what">…содержимое…</div>` → 
```html
      <details class="what"><summary>зачем это</summary>
        <div class="what-body">…содержимое…</div></details>
```
Содержимое (текст + `<b>`-теги) переносить дословно. Применить к обеим станциям («↻ Поиск дропов» и «▶ Запуск проверки»).

- [ ] **Step 3: Обернуть `.what` в settings.html (8 станций)**

Тот же трансформ для каждого `<div class="what">…</div>` в settings.html (T0/T1/approve/manual/whois-кап/ahrefs-кап/источники/применить) — оборачиваем в `<details class="what"><summary>зачем это</summary><div class="what-body">…</div></details>`, содержимое дословно.

- [ ] **Step 4: Обернуть `.what` в autopilot.html**

Тот же трансформ для каждого `<div class="what">…</div>` в autopilot.html.

- [ ] **Step 5: Регресс-гейт (важно — рендер станций проверяется TestClient'ом)**

Run: `.venv/bin/python -m pytest backend/tests/ -q` → `207 passed`
(`test_autopilot_panel.py` рендерит `/autopilot` — если `<details>` сломает Jinja-разметку, тест упадёт.)
Run: `grep -c '<div class="what">' backend/app/templates/domains.html backend/app/templates/settings.html backend/app/templates/autopilot.html`
Expected: все → `0` (не осталось необёрнутых `.what`-дивов).

- [ ] **Step 6: Скриншот-ревью**

Снять `/domains` и `/settings` (Playwright). Свёрнуто: станция = заголовок + строка «▸ зачем это» + кнопки (~3 строки вместо ~10). Кликнуть `<summary>` (`browser_click`) → абзац раскрывается. Инвариант шильдика цел: пояснение доступно, просто прогрессивно.

- [ ] **Step 7: Commit**

```bash
git add backend/app/templates/base.html backend/app/templates/domains.html backend/app/templates/settings.html backend/app/templates/autopilot.html
git commit -m "feat(panel): станции — пояснение в сворачиваемый details (T3)"
```

---

### Task 4: Пульт — воронка-лента, сгруппированная по модулям

**Files:**
- Modify: `backend/app/templates/base.html` (правила `.funnel`/`.stat`/`.fun-arrow` ~178–189 → заменить на ленту)
- Modify: `backend/app/templates/dashboard.html` (блок `<div class="funnel">…</div>` ~20–61)

**Interfaces:**
- Consumes: холодные токены T1; контекст-переменные dashboard.html — `dc` (счётчики доменов), `pc` (счётчики страниц), `sites`, `offers_active`, `gates.money`.
- Produces: `.funnel`/`.fgroup`/`.fg-h`/`.fg-cells`/`.fcell`/`.fcell.gate` — CSS-классы ленты (используются только в dashboard.html).

- [ ] **Step 1: Заменить CSS воронки в base.html (~178–189)**

Удалить правила `.funnel`, `.stat`, `.stat:hover`, `.stat .v`, `.stat .v.hot`, `.stat .k`, `.stat a`, `.stat a:hover`, `.fun-arrow`. Вставить:
```css
  /* ---- воронка-лента: группы по модулям, гейт-точка где ждёт человек ---- */
  .funnel { display:flex; gap:16px; flex-wrap:wrap; align-items:stretch; }
  .fgroup { display:flex; flex-direction:column; gap:6px; }
  .fg-h { font-family:var(--mono); font-size:10px; font-weight:600; letter-spacing:.08em;
          text-transform:uppercase; color:var(--dim); padding-left:2px; }
  .fg-cells { display:flex; gap:1px; background:var(--line); border:1px solid var(--line);
              border-radius:var(--r); overflow:hidden; box-shadow:var(--shadow); }
  .fcell { background:var(--panel); padding:11px 15px; min-width:76px; color:inherit;
           position:relative; transition:background .12s ease; }
  a.fcell:hover { background:var(--panel2); text-decoration:none; }
  .fcell .v { font-family:var(--mono); font-size:22px; font-weight:700; line-height:1.1;
              color:var(--ink); font-variant-numeric:tabular-nums; }
  .fcell .k { font-size:10.5px; font-weight:600; color:var(--mut); margin-top:2px;
              white-space:nowrap; }
  .fcell.gate .v { color:var(--acc); }
  .fcell.gate::after { content:""; position:absolute; top:8px; right:8px; width:7px; height:7px;
                       border-radius:50%; background:var(--acc); }
```

- [ ] **Step 2: Переверстать воронку в dashboard.html (~20–61)**

Заменить весь `<div class="funnel">…</div>` на три группы-модуля. Гейт-точка `gate` включается там, где что-то ждёт человека (scored / подтверждение выкупа / draft-редактура):
```html
<div class="funnel">
  <div class="fgroup"><div class="fg-h">M1 · Добыча</div>
    <div class="fg-cells">
      <a class="fcell" href="/offers" title="офферы: бренды и партнёрские ссылки — вход машины">
        <div class="v">{{ offers_active }}</div><div class="k">офферы</div></a>
      <a class="fcell" href="/domains?status=discovered" title="найдены в фиде, ещё не оценены — ждут ▶ Проверку">
        <div class="v">{{ dc.get('discovered', 0) }}</div><div class="k">найдено</div></a>
      <a class="fcell {{ 'gate' if dc.get('scored') }}" href="/domains?status=scored" title="оценены машиной — ждут твоего ✓/✗">
        <div class="v">{{ dc.get('scored', 0) }}</div><div class="k">на решении</div></a>
      <a class="fcell" href="/domains?status=approved" title="одобрены — купить руками у провайдера">
        <div class="v">{{ dc.get('approved', 0) }}</div><div class="k">одобрено</div></a>
      <a class="fcell" href="/domains?status=purchased" title="куплены — дальше создать сайт">
        <div class="v">{{ dc.get('purchased', 0) }}</div><div class="k">куплено</div></a>
    </div></div>
  <div class="fgroup"><div class="fg-h">M2 · Выкуп</div>
    <div class="fg-cells">
      <a class="fcell {{ 'gate' if gates.money }}" href="/queue" title="заказы ждут твоего подтверждения выкупа (денежный гейт)">
        <div class="v">{{ gates.money }}</div><div class="k">подтвердить</div></a>
    </div></div>
  <div class="fgroup"><div class="fg-h">M3–M5 · Сайты</div>
    <div class="fg-cells">
      <a class="fcell" href="/#sites" title="карточки сайтов: провижн → контент → публикация">
        <div class="v">{{ sites|length }}</div><div class="k">сайтов</div></a>
      <span class="fcell {{ 'gate' if pc.get('draft') }}" title="AI-черновики: не публикуются, пока не вычитаешь (гейт публикации)">
        <div class="v">{{ pc.get('draft', 0) }}</div><div class="k">черновики</div></span>
      <span class="fcell" title="вычитаны человеком — публикация возьмёт только их">
        <div class="v">{{ pc.get('edited', 0) }}</div><div class="k">edited</div></span>
      <span class="fcell" title="опубликованы — следи за попаданием в индекс">
        <div class="v">{{ pc.get('published', 0) }}</div><div class="k">опубл.</div></span>
    </div></div>
</div>
```
(Строки M3–M5 draft/edited/published — `<span>` без href, как в оригинале: это счётчики страниц, отдельного списка нет.)

- [ ] **Step 3: Регресс-гейт**

Run: `.venv/bin/python -m pytest backend/tests/ -q` → `207 passed`
(`test_pipeline.py`/`test_funnel.py` рендерят Пульт — падение = сломанная переменная воронки.)

- [ ] **Step 4: Скриншот-ревью**

Снять `/` (Playwright). Сверить: три группы-модуля (M1 Добыча / M2 Выкуп / M3–M5 Сайты) с моно-заголовками; число крупным моно + одно слово-подпись; синяя гейт-точка `●` горит на «на решении» / «подтвердить» / «черновики» когда там ненуль; высота ленты ~110px (не 350); клик по ячейке ведёт в список стадии.

- [ ] **Step 5: Commit**

```bash
git add backend/app/templates/base.html backend/app/templates/dashboard.html
git commit -m "feat(panel): воронка-лента по модулям с гейт-точками (T4)"
```

---

### Task 5: Зачистка `.btn-amber`→`.btn-acc` + дизайн-система в доке

**Files:**
- Modify: `backend/app/templates/base.html` (+ 9 контент-шаблонов: `domains.html`, `queue.html`, `dashboard.html`, `settings.html`, `diag.html`, `autopilot.html`, `page_edit.html`, `offers.html`, `site.html`)
- Create: `docs/DESIGN.md`

**Interfaces:**
- Consumes: всё из T1–T4.
- Produces: честное имя primary-кнопки `.btn-acc`; документированная дизайн-система.

- [ ] **Step 1: Переименовать `.btn-amber` → `.btn-acc` во всех шаблонах**

Механическая замена строки `btn-amber` → `btn-acc` по всем файлам (селекторы в base.html `~130–131,141` и все `class="… btn-amber …"` в 9 контент-шаблонах):
```bash
grep -rl 'btn-amber' backend/app/templates/ | xargs sed -i '' 's/btn-amber/btn-acc/g'   # macOS sed
```
(На Windows-боксе замена не гоняется — это dev-правка в репо; бокс получит её git-pull'ом.)

- [ ] **Step 2: Grep — btn-amber не осталось**

Run: `grep -rc 'btn-amber' backend/app/templates/ | grep -v ':0' || echo "чисто"`
Expected: `чисто`

- [ ] **Step 3: Финальная зачистка стрэгглеров по всем шаблонам**

Run:
```bash
grep -rEn '#e05e10|#f6f4ef|#e8e4d9|Golos Text|var\(--amber|var\(--disp' backend/app/templates/ || echo "стрэгглеров нет"
```
Expected: `стрэгглеров нет`. Если что-то всплыло в контент-шаблоне (inline-стиль) — заменить на холодный токен-эквивалент.

- [ ] **Step 4: Написать `docs/DESIGN.md`**

Создать файл с содержимым:
````markdown
# DESIGN.md — дизайн-система панели КОМБАЙН

Холодная минимальная CMS для конвейера VPN-портфеля. Ориентир — панели управления
(Linear / GitHub / Vercel): плотно, холодно, структура видна с одного взгляда.
Весь визуальный контракт — инлайн в `backend/app/templates/base.html`. Контент-шаблоны
несут только семантику + классы контракта; новые цвета/токены в шаблонах не заводить.

## Токены (`:root` в base.html)

| Группа | Переменные |
|---|---|
| Фон/панели | `--bg #f7f8fa` · `--panel #fff` · `--panel2 #f2f4f7` |
| Линии | `--line #e6e8ec` · `--line2 #d5d9e0` |
| Текст | `--ink #1a1d23` · `--mut #5c626d` · `--dim #9aa1ad` |
| Акцент (система/действие) | `--acc #2563c9` · `--acc2 #1d4ed8` · `--acc-soft #e8f0fe` |
| Статусы | `--ok` зелёный · `--bad` красный · `--warn` жёлтый · `--info` бирюза · `--violet` выкуп |
| Форма | `--r 8px` · `--r-sm 6px` · `--shadow` (одна лёгкая) |

Акцент `--acc` — единственный «активный» цвет: primary-кнопки, активный пункт меню,
ссылки, фокус, гейт-индикаторы. Статусные цвета семантические, не декоративные.

## Типографика

- **UI:** IBM Plex Sans (`--ui`) — инженерный характер, полная кириллица.
- **Данные:** JetBrains Mono (`--mono`) — числа, коды, score/RD, шапки таблиц.
- **Логотип:** Unbounded (`--brand`) — единственная «характерная» деталь.

## Компоненты (классы контракта)

Кнопки `.btn`/`.btn-acc`(primary синий)/`.btn-ok`/`.btn-bad`/`.btn-buy`/`.btn-sm`;
бейджи `.badge .b-*` (статусы доменов/страниц); таблицы `.wrap`+`table`+`td.num`;
станции `.station`(`.plate`+`details.what`+`.go`); воронка `.funnel .fgroup .fcell.gate`;
легенда `details.legend`; чипы `.chip`; прогресс `.progress`; шаги `ol.steps`;
карточки сайтов `.grid .scard`; чеклист `ul.check`.

## Правило шильдика (прогрессивное раскрытие)

Каждый контрол подписан ровно тем, что он делает — в три уровня:
1. **Всегда** — короткий точный лейбл на контроле.
2. **Наведение** — одна фраза в `title=`.
3. **Клик** — полный абзац/причины в `<details>` (`details.what`, `details.legend`).

Не удалять пояснения ради лаконичности — прятать за уровень 2/3.

## Проверка правок

Правки дизайна проверяются глазами: локальный serve (`.venv/bin/uvicorn app.main:app`
или `docker compose up`) + Playwright-скриншоты всех экранов. Регресс разметки ловит
`pytest backend/tests/` (TestClient рендерит шаблоны). Запрещено: тёмная/индустриальная
тема, тёплая палитра, расширение CSS-контракта inline-стилями в контент-шаблонах.
````

- [ ] **Step 5: Регресс-гейт + финальные grep**

Run: `.venv/bin/python -m pytest backend/tests/ -q` → `207 passed`
Run: `grep -rc 'btn-acc' backend/app/templates/base.html` → ≥`1` (класс определён)
Run: `test -f docs/DESIGN.md && echo "DESIGN.md есть"` → `DESIGN.md есть`

- [ ] **Step 6: Финальный скриншот-ревью всех 10 экранов**

Playwright-обход: `/`, `/offers`, `/domains`, `/queue`, `/sites/<id>` (если есть сайт, иначе пропустить), `/diag`, `/settings`, `/autopilot`, `/domains` (пустой фильтр), редактор страницы (если есть черновик). Сверить сквозную согласованность: везде холодно, синий акцент, IBM Plex Sans, ни следа оранжевого/бумаги, primary-кнопки синие.

- [ ] **Step 7: Commit**

```bash
git add backend/app/templates/ docs/DESIGN.md
git commit -m "feat(panel): btn-acc rename, зачистка стрэгглеров, docs/DESIGN.md (T5)"
```

---

## Self-Review (проверка плана против спеки)

**Покрытие спеки:**
- Холодная палитра + синий акцент → T1 (Steps 2–5). ✓
- IBM Plex Sans + шрифт-линк → T1 (Steps 1–2). ✓
- Острее радиусы, плоская тень → T1 (Step 2, `--r`/`--shadow`). ✓
- Сайдбар 248→200, `.sub`→`title` → T2. ✓
- Прогрессивный шильдик (станции `.what`→details) → T3; правило задокументировано → T5 DESIGN.md. ✓
- Воронка-лента по модулям + гейт-точки → T4. ✓
- `.btn-amber`→`.btn-acc` (открытый вопрос спеки, выбран вариант «б») → T5. ✓
- `docs/DESIGN.md` (дух awesome-design-md) → T5. ✓
- Инварианты (светлая CMS, UI рус., только представление, доступность, кириллица) → Global Constraints + регресс-гейт каждой задачи. ✓
- Верификация (serve + Playwright, ни следа amber) → скриншот-шаг + grep в каждой задаче. ✓

**Плейсхолдеры:** нет TBD/TODO; весь код в шагах — финальный (offers-ячейка T4 пишет голое `{{ offers_active }}`).

**Согласованность имён:** `--acc`/`--acc2`/`--acc-soft`, `--ui`, `.btn-acc`, `.fcell.gate`, `details.what`/`.what-body` — используются единообразно во всех задачах, где встречаются. Класс `.btn-amber` намеренно живёт T1–T4 и переименовывается в T5 (interim-состояние оговорено в T1 Interfaces).

**Не в области (YAGNI):** отдельный CSS-файл/сборка токенов, тёмная тема, компонентная библиотека/Storybook, смена информационной архитектуры — исключены (см. спеку «Вне области»).
