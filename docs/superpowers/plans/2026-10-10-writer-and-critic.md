# План Б: писатель по досье и критик с авто-редактурой

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this
> plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** страницы сайта пишутся по досье конкурентов и правилам оператора в структурном виде (`PageDoc`),
критик проверяет их кодом и моделью и при тумблере `auto_edit` сам ставит `edited`.

**Architecture:** бриф собирает код из `site_research` (темы, факты с источниками, таблицы, FAQ); модель
зовётся как чистая функция и возвращает JSON по схеме `PageDoc`; `Page.blocks` хранит JSON, `Page.body` —
его рендер в обычный HTML (редактор, санитайзер, publish не меняются). Критик = проверки кодом + чек-лист
моделью; `edited` ставит только `content.mark_edited` (единственный путь). Старый путь генерации (один
промпт → HTML) остаётся в сервисе для сайта без досье, но панель и автопилот без досье генерацию не запускают.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2, Pydantic v2, Alembic, nh3, httpx. Новых зависимостей нет.

**Spec:** `docs/superpowers/specs/2026-10-10-content-design-from-competitors-design.md` (§4.5, §6, §8, §9, §10).
План А (досье, правила письма) влит: `docs/superpowers/plans/2026-10-10-research-dossier-and-guides.md`.

## Global Constraints

- Гейт публикации: `publish_site` берёт только `edited`. Статус `edited` ставит ТОЛЬКО `content.mark_edited`;
  критик зовёт её, второго пути нет. При `auto_edit=false` критик статус не трогает никогда.
- Денежный гейт (`confirmed_by_human`) не затрагивается ничем.
- `integrations/` = транспорт, логика в `services/`. Секреты только в `.env`/экран ключей.
- Тесты герметичны: сеть рубит `_no_live_network`, ключи обнуляет `_no_paid_keys`. LLM в тестах — только
  подмена `LlmClient.complete` (monkeypatch), как в `backend/tests/test_m45_fixes.py`.
- Существующие тесты `generate_site` (старый путь без досье) проходят БЕЗ правок в файлах тестов — условие
  безопасности. Исключение: `test_content_critic.py` и `test_panel_critic.py` — формат ответа критика
  меняется по спеке, их переписываем.
- Язык интерфейса, комментариев, сообщений — русский. Комментарии — в плотности окружающего кода.
- Никаких новых зависимостей. Миграции: `NNNN_описание.py`, `revision`/`down_revision` строками;
  последняя — `0037_offer_promo_terms`.
- Не гадать форматы: ответ модели разбирается защитно; непарсящийся ответ = отказ с причиной, не «0 баллов».
- Запуск: `.venv/bin/python -m pytest backend/tests -q -p no:cacheprovider < /dev/null`,
  линтер `.venv/bin/python -m pyflakes backend/app backend/tests`. База до плана: **1756 passed**.
- Дизайн панели — `docs/DESIGN.md`: новых CSS-классов не заводить, только существующие
  (`.btn-sm`, `.hint`, `.badge`, `.led`, `details`).

## Review Focus

1. Модель вернула JSON в ```-ограде, с текстом до/после или обрезанный: писатель обязан снять ограду,
   повторить один раз с текстом ошибки и на втором провале НЕ создавать страницу и НЕ затирать старую.
2. Критик не ответил / ответил мусором: вердикт `pass=false`, `edited` не ставится (отказ закрытый).
3. `auto_edit=false`: ни ручная кнопка «Вычитать», ни стадия автопилота не меняют `Page.status`.
4. Страница с ручной правкой (`blocks_stale=true`): автопилот её не переписывает и не перерисовывает из
   блоков; проверки кодом идут по `body`.
5. Досье с 1–2 источниками на тип (живой случай Durev VPN): бриф не падает и не пуст — порог «общих тем»
   масштабируется, тип без своих источников берёт рыночные.

---

### Task 1: Досье — повтор скачивания, кириллические площадки, пауза для пустого досье

**Files:**
- Modify: `backend/app/services/research.py`, `backend/app/services/orchestrator.py`, `backend/app/models/site.py`
- Create: `backend/alembic/versions/0038_site_research_checked_at.py`
- Test: `backend/tests/test_research.py`, `backend/tests/test_orchestrator.py`

**Interfaces:**
- Produces: `Site.research_checked_at: datetime | None`; `research._fetch(ap, url) -> str | None`
  (до 3 попыток); `research.recently_empty(db, site_id) -> bool`.

Контекст: живой прогон 2026-10-10 показал, что ~половина `google.com/goto?url=…` падает с 502 от прокси
A-Parser (`fetch_html` возвращает `None`), а пустое досье свип пересобирает каждый час впустую.

- [ ] **Step 1: тесты (красные).** В `test_research.py`:
  - `test_fetch_retries_none_then_succeeds`: фейковый `ap.fetch_html` отдаёт `None, None, "<html>…"` →
    `research._fetch` возвращает HTML, вызовов 3; четвёртого вызова нет при трёх `None` (результат `None`).
  - `test_fetch_retries_exception`: первый вызов бросает `RuntimeError`, второй отдаёт HTML → HTML.
  - `test_cyrillic_platform_names_filtered`: `_platform_or_brand(["Телеграм"], "durevvpn")`,
    `["Ютуб"]`, `["ВКонтакте"]`, `["Дзен"]`, `["Рутуб"]` → `True`; `["ProPrivacy"]` → `False`.
  - `test_build_dossier_stamps_research_checked_at`: после `build_dossier` (и с источниками, и пустого)
    `Site.research_checked_at` не `None`; при отмене (`jobs.Cancelled`) не меняется.
  - В `test_orchestrator.py`: `test_stage_research_skips_recently_empty_site` — сайт `content` без строк
    досье с `research_checked_at = now - 2h` стадией не берётся (вызовов `build_dossier` 0); с
    `now - 25h` берётся.
  - Тест `test_resolver_is_late_bound_and_failure_is_unsafe` не должен зависеть от сети при регрессии:
    внутри него подменить `socket.getaddrinfo` на функцию, бросающую `socket.gaierror`, и проверить
    `_safe_url("http://example.com/") is False`.
- [ ] **Step 2:** запустить, убедиться, что падают по причине «нет функции/поля».
- [ ] **Step 3: реализация.**
  - `research._fetch(ap, url, attempts=3)`: цикл; исключение логируется `log.warning` и считается попыткой;
    между попытками `time.sleep(1)` через модульный шов `_sleep = time.sleep` (тест его глушит);
    первый непустой ответ возвращается. `build_dossier` зовёт `_fetch` вместо прямого `ap.fetch_html`.
  - `_PLATFORMS`: добавить нормализованные `телеграм, telegram, ютуб, вконтакте, вк, дзен, рутуб,
    одноклассники, яндексдзен` (через тот же `_norm`; убедиться, что `_norm` не выбрасывает кириллицу —
    если выбрасывает, расширить на `str.isalnum`).
  - Модель: `research_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))` в `Site`;
    миграция `0038` (`add_column`/`drop_column`, `down_revision = "0037_offer_promo_terms"`).
  - `build_dossier`: в конце (обе ветки — `done` и `empty`, не `cancelled`) ставит
    `site.research_checked_at = now(UTC)` той же/отдельной короткой сессией.
  - `recently_empty(db, site_id)`: строк досье нет И `research_checked_at` моложе 24 часов
    (`EMPTY_RETRY_HOURS = 24`, константа модуля; naive-время трактовать как UTC — как в `is_fresh`).
  - `_stage_research`: список сайтов фильтруется `not research.is_fresh(db, sid) and not
    research.recently_empty(db, sid)`.
- [ ] **Step 4:** весь сьют зелёный, pyflakes чист.
- [ ] **Step 5:** коммит `feat(research): повтор скачивания при сбое прокси, кириллические площадки, пауза для пустого досье`.

---

### Task 2: `PageDoc` и рендер блоков в HTML

**Files:**
- Create: `backend/app/services/page_doc.py`
- Modify: `backend/app/services/locales.py`
- Test: `backend/tests/test_page_doc.py`

**Interfaces:**
- Produces:
  - `class PageDoc(BaseModel)` (ниже);
  - `parse(raw: str) -> PageDoc` — снимает ```-ограду и текст вокруг крайних `{`…`}`, валидирует;
    при провале бросает `ValueError` с коротким человекочитаемым текстом ошибки (≤ 600 симв.);
  - `render_blocks(doc: PageDoc, kind: str, lang: str) -> str` — HTML-фрагмент;
  - `doc_text(doc: PageDoc) -> str` — весь видимый текст одной строкой (для критика);
  - `WORDS = {"review": (1500, 2200), "comparison": (1200, 1800), "howto": (900, 1400)}`.

```python
class Meta(BaseModel):
    title: str = Field(min_length=10, max_length=110)
    description: str = Field(min_length=40, max_length=200)

class Verdict(BaseModel):
    score: float = Field(ge=1, le=10)
    summary: str
    for_whom: str
    not_for_whom: str

class H3(BaseModel):
    h3: str
    paragraphs: list[str] = Field(min_length=1)

class Section(BaseModel):
    h2: str
    paragraphs: list[str] = Field(min_length=1)
    bullets: list[str] | None = None
    h3s: list[H3] | None = None

class Table(BaseModel):
    columns: list[str] = Field(min_length=2, max_length=6)
    rows: list[list[str]] = Field(min_length=1, max_length=20)

class Step(BaseModel):
    title: str
    text: str
    platform: str | None = None

class Faq(BaseModel):
    q: str
    a: str

class PageDoc(BaseModel):
    model_config = ConfigDict(extra="ignore")
    meta: Meta
    verdict: Verdict | None = None
    pros: list[str] = []
    cons: list[str] = []
    sections: list[Section] = Field(min_length=2)
    table: Table | None = None
    steps: list[Step] | None = None
    faq: list[Faq] = []
    sources: list[str] = []
```
Валидатор `Table`: каждая строка той же длины, что `columns` (иначе `ValueError("в таблице строка N: …")`).

Рендер (весь текст — через `html.escape`; модель отдаёт простой текст, HTML в строках не допускается и
экранируется): порядок `verdict → pros/cons → table → sections → steps → faq`.
- verdict: `<p><strong>{lbl_score}: {score:g}/10.</strong> {summary}</p><p><strong>{lbl_for}:</strong>
  {for_whom}</p><p><strong>{lbl_not_for}:</strong> {not_for_whom}</p>`;
- pros/cons: `<h3>{lbl_pros}</h3><ul><li>…</li></ul>` и то же для `lbl_cons` (пустой список не рисуется);
- table: `<table><thead><tr><th>…</th></tr></thead><tbody><tr><td>…</td></tr></tbody></table>`;
- section: `<h2>…</h2>` + `<p>` на абзац + `<ul>` для bullets + `<h3>`/`<p>` для h3s;
- steps: `<h2>{lbl_steps}</h2><ol><li><strong>{title}</strong>{ (platform)} — {text}</li></ol>`;
- faq: `<h2>{lbl_faq}</h2>` + на пару `<h3>{q}</h3><p>{a}</p>`.
Все теги уже в `content._ALLOWED_TAGS` — `content._sanitize(render_blocks(...))` обязан вернуть строку
без потерь (тест). `<h1>` не выводится: заголовок страницы рисует шапка.

Локали: в `TEXTS` каждого из 8 языков добавить `lbl_score, lbl_for, lbl_not_for, lbl_pros, lbl_cons,
lbl_steps, lbl_faq` (ru: «Оценка», «Кому подойдёт», «Кому не подойдёт», «Плюсы», «Минусы», «Пошагово»,
«Вопросы и ответы»; en: «Score», «Best for», «Not for», «Pros», «Cons», «Step by step», «FAQ»; остальные
языки — естественные эквиваленты).

- [ ] **Step 1: тесты.** `test_page_doc.py`:
  - `test_parse_strips_fence_and_prose`: строка «Вот ответ:\n```json\n{…}\n```\nготово» парсится;
  - `test_parse_reports_missing_field`: JSON без `sections` → `ValueError`, в тексте есть `sections`;
  - `test_parse_rejects_ragged_table`;
  - `test_parse_truncated_json_is_value_error` (обрезанный на середине JSON);
  - `test_render_all_blocks_survive_sanitizer`: документ со всеми блоками → `_sanitize(html) == html`,
    порядок блоков, ярлыки на `ru` и `en`, нет `<h1>`;
  - `test_render_escapes_model_html`: `<script>` в абзаце → в выводе `&lt;script&gt;`;
  - `test_doc_text_contains_everything`: текст включает verdict, ячейки таблицы, шаги, FAQ;
  - `test_labels_exist_in_all_languages`: для каждого языка `TEXTS` есть все 7 ключей.
- [ ] **Step 2:** красные. **Step 3:** реализация. **Step 4:** сьют + pyflakes.
- [ ] **Step 5:** коммит `feat(M4): схема PageDoc и рендер блоков страницы`.

---

### Task 3: Бриф из досье

**Files:**
- Create: `backend/app/services/brief.py`
- Test: `backend/tests/test_brief.py`

**Interfaces:**
- Consumes: `research.dossier(db, site_id) -> list[SiteResearch]` (поля `kind, rank, url, final_url, domain,
  words, headings [[tag, text]], tables [[[cell]]], faq [{q,a}], numbers [{value, ctx}], text`);
  `vertical_data.vertical_block(brand)`; `guides.load_guides(lang, kind) -> {"text","files","truncated"}`.
- Produces:
  - `build_brief(rows: list, kind: str) -> dict` — чистая функция над строками досье:
    `{"sources": [{"n", "url", "domain", "words"}], "topics": [{"title", "count"}], "outlines":
    [{"n", "headings": [str]}], "facts": [{"value", "ctx", "n"}], "tables": [{"n", "columns": [str],
    "rows": int}], "questions": [str], "gaps": [str], "borrowed": bool}`;
  - `brief_text(brief: dict, *, brand: str, kind: str, title: str, lang_name: str, country: str | None,
    promo: tuple[str | None, str | None], vertical: str | None) -> str` — пользовательский промпт;
  - `allowed_numbers(rows: list, *extra_texts: str | None) -> set[str]` — нормализованные числа досье и
    доп. текстов (блок фактов вертикали, условия промокода).
  - `norm_number(s: str) -> str` — `"1 500"`/`"1,5"`/`"1.50"` → `"1500"`/`"1.5"`/`"1.5"`.

Правила сборки:
- Источники типа: строки досье с `kind == <тип страницы>`. Если их меньше 2 — добавить рыночные
  (`kind == "market"`), `borrowed=True` (тип без своих источников пишется по рынку — живой случай
  малого бренда). Нумерация `n` — сквозная, с 1.
- `topics`: заголовки h2/h3 всех источников, нормализация `lower` + схлопывание пробелов + срез
  пунктуации по краям; тема = заголовок, встреченный у `count ≥ max(2, ceil(0.6 * N))` источников;
  при `N < 2` — темы пусты, работает `outlines`. Сортировка по `count` убыв., не более 20.
- `outlines`: по каждому источнику первые 25 заголовков (структура конкурента для полноты).
- `facts`: числа источников с контекстом, без дублей по `(value, n)`, не более 15 на источник, 60 всего.
- `tables`: первая строка таблицы как колонки, число строк; не более 6.
- `questions`: вопросы FAQ источников без дублей (по нормализованному тексту), не более 15.
- `gaps`: нормализованные h2 рыночных источников, которых нет ни в одном заголовке источников типа,
  не более 10 (при `borrowed=True` — пусто).
- `brief_text`: разделы «Задача», «Оффер» (бренд, гео, язык, промокод и его условия — «единственный
  бонус, о котором можно писать»), «Факты бренда» (vertical или «нет проверенных данных — не выдумывай
  характеристики»), «Источники [n] url», «Общие темы», «Структуры конкурентов», «Факты с источником
  (значение — контекст — [n])», «Таблицы конкурентов», «Вопросы», «Пробелы рынка», и требование объёма
  из `page_doc.WORDS[kind]`. Весь бриф ≤ 24 000 символов: при превышении режутся `outlines`, затем `facts`.
- `allowed_numbers`: `numbers[].value` всех строк + все числа (regex `research_extract._NUM_RE`) из
  `extra_texts`; всё через `norm_number`.

- [ ] **Step 1: тесты.** Фабрика строк досье в тесте (`SimpleNamespace`, без БД):
  - `test_topics_threshold_scales`: 5 источников, тема у 3 → в `topics`; у 2 → нет; при 2 источниках
    тема у обоих → есть;
  - `test_kind_without_sources_borrows_market`: review-строк 0, market 2 → `sources` из рынка,
    `borrowed is True`, `gaps == []`;
  - `test_single_source_has_outline_not_topics`;
  - `test_facts_carry_source_number_and_cap`;
  - `test_gaps_are_market_only_headings`;
  - `test_brief_text_has_promo_terms_and_word_bounds` и `test_brief_text_without_promo_has_no_promo_section`;
  - `test_brief_text_is_capped` (60 фактов × длинный контекст + 5 × 25 заголовков → ≤ 24 000);
  - `test_norm_number_and_allowed_numbers` (`"1 500"`, `"1,5"`, `"30"` из vertical-текста).
- [ ] **Step 2:** красные. **Step 3:** реализация. **Step 4:** сьют + pyflakes.
- [ ] **Step 5:** коммит `feat(M4): бриф страницы из досье конкурентов`.

---

### Task 4: Писатель — `PageDoc` по брифу, переписывание, `blocks_stale`

**Files:**
- Modify: `backend/app/services/content.py`, `backend/app/config.py`, `backend/app/services/api_keys.py`,
  `backend/app/services/vertical_data.py`
- Test: `backend/tests/test_writer.py`, `backend/tests/test_api_keys.py`

**Interfaces:**
- Consumes: `page_doc.parse/render_blocks/WORDS`, `brief.build_brief/brief_text`, `guides.load_guides`,
  `research.dossier`.
- Produces:
  - `settings.LLM_WRITER_MODEL: str = ""` (пусто → `LLM_MODEL`), `settings.LLM_CRITIC_MODEL: str = ""`
    (пусто → `LLM_MODEL`); оба — поля экрана ключей в группе LLM (подсказки по образцу `LLM_MODEL`);
  - `content.write_doc(llm, *, system: str, prompt: str, issues: list[str] | None = None)
    -> tuple[PageDoc | None, str | None]` — до двух вызовов модели; `(doc, None)` или `(None, причина)`;
  - `content.writer_system(lang, country, guides_text) -> str`;
  - `content.generate_site(site_id, lang=None, vertical_data=None, use_competitor=False, rewrite=False) -> int`
    — прежняя сигнатура + `rewrite`;
  - `content.rewrite_page(page_id: int, issues: list[str]) -> dict` — `{"page_id", "ok": bool, "error"}`;
  - `Page.blocks`/`Page.blocks_stale` заполняются; `save_draft` и `mark_edited(body=…)` ставят
    `blocks_stale=True`, если тело изменилось.

Поведение `generate_site`:
- Фаза 1 как сейчас + `rows = research.dossier(db, site_id)` (в той же короткой сессии; строки
  отсоединить `db.expunge_all()` или собрать брифы внутри сессии).
- **Досье пусто → старый путь без изменений** (весь нынешний код цикла). Комментарий: панель и автопилот
  без досье сюда не приходят (Task 7); ветка жива для API/скриптов и существующих тестов.
- **Досье есть → новый путь:** для каждой спеки `scaffold()`:
  - `rewrite=False`: только отсутствующие пути (как сейчас);
  - `rewrite=True`: ещё и существующие страницы со статусом `draft|edited|published`, у которых
    `blocks_stale` ложно. Страницы с `blocks_stale=True` пропускаются (ручная правка не затирается),
    их число уходит в сообщение задачи.
  - `system = writer_system(lang, country, guides.load_guides(lang, kind)["text"])`;
    `prompt = brief.brief_text(brief.build_brief(rows, kind), …)`;
    `doc, err = write_doc(llm, system=system, prompt=prompt)`;
  - `err` → страница НЕ создаётся и НЕ меняется; причина копится и уходит в
    `jobs.report(run, message=…)` + `jobs.finish(run, "done_warn")`;
  - успех → `body = _sanitize(page_doc.render_blocks(doc, kind, lang))`, `title = doc.meta.title`,
    `blocks = doc.model_dump()`, `blocks_stale=False`, `status="draft"`, `critic_score/critic_notes/
    critic_checked_at = None`; новая страница — `INSERT` (та же обработка `IntegrityError`), существующая —
    `UPDATE` той же строки. Коммит по странице.
- `LlmClient(timeout=600)` для нового пути (страница на 2000 слов через шлюз идёт минуты), модель —
  `settings.LLM_WRITER_MODEL or settings.LLM_MODEL`, передаётся `complete(..., model=…)`.

`write_doc`: вызов 1 → `page_doc.parse`; `ValueError e` → вызов 2 с добавкой к промпту
«Предыдущий ответ не прошёл проверку схемы: {e}. Верни ТОЛЬКО исправленный JSON.»; снова провал →
`(None, "ответ писателя не прошёл схему: …")`. Пустой ответ модели — тоже провал попытки. Если переданы
`issues` (переписывание по замечаниям критика) — они идут в промпт первым вызовом блоком
«Замечания редактора, которые нужно устранить: …».

`writer_system`: роль редактора; язык и рынок; правила письма оператора блоком (если есть); запреты:
не выдумывать числа и характеристики, которых нет в брифе; каждую использованную цифру подтверждать
URL в `sources`; не копировать формулировки источников; слово «неизвестно» в фактах бренда не
превращать в цифры; формат — ТОЛЬКО JSON по схеме (схема текстом: поля и какие блоки для какого типа:
review — `verdict`, `pros`, `cons`; comparison — `table`, `pros`, `cons`; howto — `steps`), без
Markdown-ограды, строки без HTML.

`rewrite_page(page_id, issues)`: находит страницу, её сайт, оффер (`Page.offer_id`), язык (`Page.lang`),
тип по `url_path` через `scaffold()`; нет досье или `blocks_stale` → `{"ok": False, "error": …}`;
иначе тот же путь с `issues`; успех обновляет строку как выше.

`vertical_block`: строки, чьё значение — строка `"неизвестно"`, пропускаются (а не печатаются);
`protocols`/`extras` — пустые списки тоже пропускаются. Self-check модуля остаётся зелёным.

- [ ] **Step 1: тесты.** `test_writer.py` (фикстура: сайт `content` + оффер + 2 строки `SiteResearch`
  на тип; LLM — monkeypatch `LlmClient.complete` очередью ответов; валидный JSON — константа в тесте):
  - `test_generate_with_dossier_writes_blocks_and_rendered_body`: 3 страницы `draft`, `blocks` — dict с
    `sections`, `body` содержит `<h2>`, `title` из `meta.title`;
  - `test_generate_without_dossier_uses_legacy_path`: ответ-HTML, `blocks is None`;
  - `test_writer_retries_once_with_schema_error`: ответы `["мусор", валидный]` → страница есть, во втором
    промпте есть «не прошёл проверку схемы»;
  - `test_writer_two_failures_creates_no_page_and_warns`: страницы нет, `jobs.last("generate")` —
    `done_warn`, в message причина;
  - `test_rewrite_updates_existing_pages_in_place`: 3 опубликованные страницы → `generate_site(rewrite=True)`
    → те же id, `status == "draft"`, новый `body`, `critic_*` сброшены, возвращено 3;
  - `test_rewrite_skips_manually_edited_page` (`blocks_stale=True` → не тронута);
  - `test_rewrite_failure_keeps_old_page_intact`;
  - `test_save_draft_marks_blocks_stale` и `test_mark_edited_without_body_keeps_blocks_fresh`;
  - `test_rewrite_page_passes_issues_to_prompt`;
  - `test_writer_uses_writer_model` (`LLM_WRITER_MODEL="opus"` → `model="opus"` в kwargs);
  - `test_prompt_has_guides_and_promo_terms` (правила из tmp-папки через `CONTENT_GUIDES_DIR`);
  - `test_vertical_block_skips_unknown_fields`.
  В `test_api_keys.py` — новые поля есть в реестре.
- [ ] **Step 2:** красные. **Step 3:** реализация. **Step 4:** сьют (существующие тесты `generate_site`
  без правок) + pyflakes.
- [ ] **Step 5:** коммит `feat(M4): писатель по досье — PageDoc, переписывание страниц, blocks_stale`.

---

### Task 5: Критик — проверки кодом

**Files:**
- Modify: `backend/app/services/content_critic.py`
- Test: `backend/tests/test_critic_checks.py`

**Interfaces:**
- Consumes: `brief.allowed_numbers/norm_number`, `page_doc.WORDS`, `research_extract._NUM_RE`.
- Produces: `content_critic.code_checks(*, text: str, kind: str | None, lang: str, brand: str | None,
  sources: list[str], allowed: set[str]) -> list[str]` — чистая функция, список замечаний (пусто = чисто);
  `content_critic.visible_text(body_html: str) -> str`.

Проверки (каждая — отдельная маленькая функция, замечание — русская фраза):
1. **Копирование.** Шинглы по 12 слов (слова: `\w+`, lower) текста против шинглов каждого источника;
   пересечение непусто → «копирование источника: «…первые 12 слов…»» (до 3 фраз). Источники короче
   12 слов игнорируются.
2. **Бренд.** `brand` задан и не встречается в тексте (без учёта регистра) → «в тексте нет бренда …».
   Раскрытие партнёрства НЕ проверяем — его ставит шаблон (`render_html`), см. S6-15 в модуле.
3. **Язык.** Доля кириллицы среди букв: `lang == "ru"` требует ≥ 0.6; остальные языки — ≤ 0.2.
   `# ponytail: только алфавит; en от de так не отличить — это ловит чек-лист модели.`
4. **Объём.** `kind` в `WORDS` → число слов вне границ → «объём N слов, нужно A–B». `kind is None`
   (неизвестный путь) — проверка пропускается.
5. **Числа без источника.** Числа текста (`_NUM_RE`, `norm_number`) минус `allowed`; игнорируются
   целые `≤ 12` (счёт шагов, оценки, списки) и годы `текущий год − 1 … + 1`; остаток → одно замечание
   «числа без источника: 37, 4500 …» (до 10 значений).

- [ ] **Step 1: тесты.** `test_critic_checks.py`:
  - `test_shingle_copy_detected_and_quoted`, `test_paraphrase_is_not_copy`, `test_short_source_ignored`;
  - `test_missing_brand`, `test_brand_case_insensitive`;
  - `test_language_ru_ok_en_flagged` и обратный;
  - `test_volume_bounds_per_kind`, `test_volume_skipped_for_unknown_kind`;
  - `test_unsourced_number_flagged`, `test_small_ints_and_year_ignored`,
    `test_number_formats_match_allowed` (`"1 500"` в тексте против `"1500"` в `allowed`);
  - `test_clean_text_has_no_issues`;
  - `test_visible_text_strips_tags_and_entities`.
- [ ] **Step 2:** красные. **Step 3:** реализация (существующий код модуля пока не трогать).
- [ ] **Step 4:** сьют + pyflakes. **Step 5:** коммит `feat(M4): критик — проверки кодом (копирование, язык, объём, числа)`.

---

### Task 6: Критик — вердикт модели, круги переписывания, `auto_edit`

**Files:**
- Modify: `backend/app/services/content_critic.py`, `backend/app/api/panel.py` (только роут
  `/pages/{id}/critique`, если меняется форма ответа), `backend/app/templates/page_edit.html`
- Test: `backend/tests/test_content_critic.py` (переписать), `backend/tests/test_panel_critic.py`
  (поправить под новый формат), `backend/tests/test_critic_gate.py` (новый)

**Interfaces:**
- Consumes: `code_checks`, `visible_text`, `content.rewrite_page`, `content.mark_edited`,
  `content.scaffold`, `research.dossier`, `brief.allowed_numbers`, `guides.load_guides`,
  `autonomy.get_autonomy()["auto_edit"]`, `settings.LLM_CRITIC_MODEL`.
- Produces:
  - `content_critic.parse_verdict(text: str) -> dict | None` — `{"pass": bool, "issues": [str], "score":
    float | None}` (score 0–1) или `None`, если JSON не разобрался / нет булева `pass`;
  - `content_critic.review_page(page_id: int) -> dict` — `{"pass": bool, "issues": [str], "code": [str],
    "model": [str], "score": float | None, "error": str | None}`; пишет `critic_score`, `critic_notes =
    {"pass", "issues", "code", "model", "round"}`, `critic_checked_at`; `Page.status` НЕ трогает;
  - `content_critic.critique_page(page_id)` — остаётся как тонкая обёртка над `review_page` (кнопка
    редактора страницы), ключи ответа `score/issues/error` сохраняются;
  - `content_critic.edit_site(site_id: int, auto_edit: bool | None = None) -> dict` — задача реестра
    `edit`: `{"reviewed": n, "edited": n, "rewritten": n, "failed": n}`; `MAX_ROUNDS = 2`.

`review_page`:
1. Страница, оффер (бренд), язык, тип по `url_path` (через `content.scaffold(brand, None, lang)`), строки
   досье сайта; `allowed = allowed_numbers(rows, vertical_block(brand), offer.promo_terms)`.
2. `code = code_checks(text=visible_text(page.body), …, sources=[r.text for r in rows])`.
3. Модель (`LlmClient(timeout=300)`, `model = settings.LLM_CRITIC_MODEL or settings.LLM_MODEL`): системный
   промпт — роль выпускающего редактора + правила письма оператора (`load_guides(lang, kind)`) как
   чек-лист + критерии «тема соответствует бренду и типу страницы; текст конкретнее и полезнее общих
   фраз; нет выдуманных характеристик; язык — заявленный; раскрытие партнёрства не оценивать»; ответ —
   ТОЛЬКО JSON `{"pass": true|false, "score": 0-100, "issues": ["…"]}`. Пользовательский — бренд, тип,
   язык, текст страницы (`visible_text`, не более 30 000 симв.).
4. Сбой вызова, пустой ответ или `parse_verdict is None` → `model = ["критик не ответил: <причина>"]`,
   `pass=False`, `error` заполнен (**отказ закрытый**). Замечания про disclosure отфильтровать
   (`_DISCLOSURE_RE`, как сейчас).
5. `pass = not code and verdict["pass"] and not model_issues_when_fail`; точное правило:
   `pass = (code == []) and verdict is not None and verdict["pass"] is True`.
6. `critic_notes["round"]` — сколько раз страницу переписывали по замечаниям (хранится в notes,
   `edit_site` его увеличивает).

`edit_site(site_id, auto_edit=None)` (внутри `jobs.track("edit")`, прогресс по страницам, отмена между
страницами):
- `auto_edit is None` → `get_autonomy()["auto_edit"]`.
- Страницы сайта со `status == "draft"`. Для каждой: `v = review_page(id)`; пока `not v["pass"]` и
  страница не `blocks_stale` и есть `blocks` и `round < MAX_ROUNDS`: `content.rewrite_page(id,
  v["issues"])` → при `ok` `round += 1`, `rewritten += 1`, снова `review_page`; при не-`ok` — стоп круга.
- `v["pass"]` и `auto_edit` → `content.mark_edited(id)` (исключение `ValueError` → замечание в notes,
  `failed += 1`), `edited += 1`. `v["pass"]` и не `auto_edit` → страница остаётся `draft` с вердиктом
  «pass» (человек одобряет сам).
- Не `pass` после кругов → `failed += 1`, страница `draft`, замечания в `critic_notes`.
- Итог в `jobs.report(message=…)`: «вычитано N, одобрено N, переписано N, с замечаниями N»; при
  `failed` — `jobs.finish(run, "done_warn")`.
- Модуль НЕ пишет `Page.status` напрямую нигде (тест грепает исходник: в `content_critic.py` нет
  подстроки `status = "edited"` / `.status = `).

`page_edit.html`: блок критика показывает «pass»/«замечания», круг, раздельно «проверки кодом» и
«редактор-модель» (существующие классы, без новых).

- [ ] **Step 1: тесты.**
  `test_content_critic.py` (переписать под JSON): `parse_verdict` — валидный, в ```-ограде, без `pass`
  → `None`, мусор → `None`, score клампится в 0–1; `review_page` пишет поля и не меняет статус;
  пустой ответ модели → `pass False`, `error`; отсутствующая страница → `ValueError`;
  замечание про disclosure отфильтровано.
  `test_critic_gate.py` (сайт + оффер + досье + 1–3 страницы-черновика с `blocks`; LLM — очередь ответов,
  различать писателя и критика по системному промпту):
  - `test_pass_with_auto_edit_marks_edited_via_mark_edited` (шпион на `content.mark_edited` — вызван 1 раз);
  - `test_pass_without_auto_edit_keeps_draft`;
  - `test_fail_rewrites_then_passes` (критик: fail → писатель → pass; `round == 1`, `edited == 1`);
  - `test_two_rounds_then_stop` (критик всегда fail → переписано ровно 2 раза, страница `draft`,
    замечания сохранены, `done_warn`);
  - `test_code_issue_blocks_pass_even_if_model_passes` (в тексте скопированный абзац источника);
  - `test_critic_silent_is_closed_failure` (модель бросает исключение → не `edited`);
  - `test_stale_blocks_page_is_reviewed_but_not_rewritten`;
  - `test_published_and_edited_pages_untouched`;
  - `test_publish_still_refuses_draft` (регрессия гейта: `publish_site` на сайте с одними `draft` не
    публикует ничего — взять готовый паттерн из `test_g3_content_safety.py`/`test_pipeline.py`);
  - `test_critic_module_never_assigns_status` (чтение исходника).
  `test_panel_critic.py` — под новый формат (роут и отображение).
- [ ] **Step 2:** красные. **Step 3:** реализация; старые `_parse_critique`, `_SCORE_RE`, `_SYSTEM_PROMPT`,
  `_critique_prompt` удалить вместе с их тестами; шапку модуля переписать под новую роль.
- [ ] **Step 4:** сьют + pyflakes. **Step 5:** коммит `feat(M4): критик — вердикт модели, круги переписывания, auto_edit через mark_edited`.

---

### Task 7: Автопилот и панель — стадия «вычитка», кнопки, требование досье

**Files:**
- Modify: `backend/app/services/orchestrator.py`, `backend/app/api/panel.py`,
  `backend/app/templates/site.html`, `backend/app/templates/autopilot.html`,
  `backend/app/templates/base.html` (только словарь `JOB_RU` и чипы стадий, если они там)
- Test: `backend/tests/test_orchestrator.py`, `backend/tests/test_writer_panel.py`

**Interfaces:**
- Consumes: `content.generate_site(rewrite=…)`, `content_critic.edit_site`, `research.dossier`.
- Produces: стадия `("edit", "auto_edit", "cap_generate", _stage_edit)` в `STAGES` между `generate` и
  `publish`; `STAGE_RU["edit"] = "вычитка"`; `COUNT_RU["edit_failed"] = "вычитка: замечания"`,
  `COUNT_RU["generate_no_dossier"] = "нет досье"`; роуты `POST /sites/{id}/rewrite`,
  `POST /sites/{id}/edit`; `"edit"` в `_JOBS`; `JOB_RU["edit"] = "Вычитка"`.

- `_stage_generate`: сайт без строк досье пропускается: счётчик `generate_no_dossier`, ошибка
  «site#N: нет досье — генерация пропущена (стадия «досье» или кнопка на карточке сайта)». Тесты
  оркестратора, где генерация шла без досье, получают строку досье в фикстуре (это правка под новое
  поведение спеки §4.5, не ослабление).
- `_stage_edit(cap)`: сайты со статусом `content|published|monitoring`, у которых есть `draft`-страница с
  `critic_checked_at IS NULL` (ещё не вычитана — страница с замечаниями не крутится вечно), до `cap`
  сайтов; `content_critic.edit_site(sid, auto_edit=True)`; `jobs.AlreadyRunning` пробрасывается (как в
  соседних стадиях); возврат `(done, errs, {"edit_failed": n})`. Кап — `cap_generate`
  (`# ponytail: общий кап с генерацией; свой — когда вычитка станет узким местом`).
  Стадия идёт только при `auto_edit=true` (обычный механизм тумблеров `STAGES`).
- Панель:
  - `POST /sites/{id}/generate`: нет строк досье → отказ «Сначала собери досье конкурентов (шаг 3½):
    без него писать не по чему»; `use_competitor` больше не передаётся (досье его заменяет).
  - `POST /sites/{id}/rewrite`: те же проверки + `jobs.spawn("generate", lambda:
    content.generate_site(site_id, rewrite=True))`; сообщение называет, что опубликованные страницы
    вернутся в черновики и на сайте останется прежняя версия до новой публикации. Кнопка с
    `onsubmit="return confirm(…)"`.
  - `POST /sites/{id}/edit`: `jobs.spawn("edit", lambda: content_critic.edit_site(site_id))`; занятость —
    `jobs.busy_msg`.
  - `site.html`: на шаге контента — кнопки «✎ Переписать по досье» и «✓ Вычитать критиком» (`.btn-sm`,
    `title` с объяснением; «Вычитать» подписана по состоянию тумблера: «одобрит сам» / «только вердикт —
    одобряешь ты»); в таблице страниц — колонка «критик»: `pass` (led-ok), «N замечаний» (led-todo,
    `title` = первые замечания), круг, «—» если не вычитана. Текст-легенда статуса `edited` — «вычитано
    (человеком или критиком)».
  - `autopilot.html`: тумблер `auto_edit` перестаёт быть `disabled`, подпись «критик вычитывает и сам
    ставит edited» + `title`: гейт публикации остаётся в коде — публикуется только edited; чип стадии
    `edit` на своём месте. `auto_design` остаётся выключенным до плана В.
- [ ] **Step 1: тесты.** `test_orchestrator.py`: порядок `STAGES` (edit между generate и publish);
  `test_stage_generate_skips_site_without_dossier`; `test_stage_edit_takes_unreviewed_drafts_only`;
  `test_stage_edit_off_when_auto_edit_false` (свип с выключенным тумблером не зовёт `edit_site`).
  `test_writer_panel.py`: отказ генерации без досье; `rewrite` и `edit` спавнят задачи (подмена
  `jobs.spawn`), отказ при занятой задаче; карточка сайта показывает вердикт и кнопки; `/autopilot`
  — чекбокс `auto_edit` без `disabled`, `auto_design` с `disabled`; сохранение `auto_edit` работает.
- [ ] **Step 2:** красные. **Step 3:** реализация. **Step 4:** сьют + pyflakes.
- [ ] **Step 5:** коммит `feat(autopilot,panel): стадия вычитки, переписывание по досье, генерация требует досье`.

---

### Task 8 (контроллер, после мержа): Durev VPN и живой прогон

Не субагент: живые данные и бокс. Факты Durev VPN снять с официального сайта (что не подтверждено —
`"неизвестно"`), добавить в `vertical_data.VPN_FACTS` + алиасы, обновить `AS_OF`; на боксе задать
`LLM_WRITER_MODEL`/`LLM_CRITIC_MODEL`; `tunnelnotes.xyz`: «Переписать по досье» → «Вычитать» при
`auto_edit=true` → публикация; итог и живые форматы ответа писателя/критика — в
`docs/v2/research/research-live-formats-2026-10.md` и `docs/v2/CLAUDE.md`.
