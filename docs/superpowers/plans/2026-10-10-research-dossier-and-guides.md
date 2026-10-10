# План А: досье конкурентов, правила письма, миграция 0036

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Сайт в статусе `content` получает «досье конкурентов» (SERP → 5 страниц → текст, заголовки,
таблицы, FAQ, цифры, CSS-токены, скриншоты по тумблеру) и правила письма оператора из `content_guides/`;
обе вещи видны в панели и готовы для планов Б (писатель/критик) и В (дизайнер).

**Architecture:** Чистые функции извлечения (`research_extract.py`) отделены от оркестровки с БД и сетью
(`research.py`); транспорт Browserless — отдельный клиент в `integrations/`. Правила письма — файловая
папка в репо + загрузка через `/settings`. Одна миграция `0036` заводит ВСЕ таблицы/колонки трёх планов
(`site_research`, `site_theme`, `Page.blocks`, тумблеры автопилота), чтобы планы Б/В миграций не трогали.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2, Pydantic v2, Alembic, httpx (MockTransport в тестах),
nh3, stdlib `html.parser`/`re`. Тесты: pytest офлайн, SQLite StaticPool (`backend/tests/conftest.py`).

**Spec:** `docs/superpowers/specs/2026-10-10-content-design-from-competitors-design.md` (§3, §4, §5, §8, §10, §12 п.1–4).

## Global Constraints

- Язык кода, комментариев, UI, тестов — русский (docstrings и flash-тексты по-русски, как во всём репо).
- `integrations/` = только транспорт; логика в `services/` (CLAUDE.md «Конвенции»).
- Тесты герметичны: `_no_live_network` режет сеть; клиенты подменяются `monkeypatch`/`httpx.MockTransport`.
- Миграция: ID секвенциальный `0036_research_theme_blocks`, `down_revision = "0035_drop_dead_overrides"`.
- Никаких новых зависимостей в `requirements.txt`.
- Формат внешних ответов — только по живому образцу (Задача 1); не гадать.
- Гейты (деньги, `edited`) этим планом не трогаются; `publish_site` и `mark_edited` не редактируются.
- Запуск тестов локально: `.venv/bin/python -m pytest backend/tests -q -p no:cacheprovider < /dev/null`;
  pyflakes: `.venv/bin/python -m pyflakes backend/app backend/tests` — чисто после каждой задачи.
- Коммиты с `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`; пуш в `origin/main` разрешён оператором.

## Review Focus

1. Загрузка файла правил с именем `../../.env` или `ru/../x.md` должна отбиваться 303+err, файл не создаётся — тест в Задаче 4.
2. SERP отдаёт только `google.com/goto?url=…`-редиректы, `fetch_html` возвращает `None` на все — досье «пустое» с причиной словами, не исключение — тест в Задаче 7.
3. Страница конкурента без `<table>`/`<h2>` и с 50 словами текста — отсекается как «не живая» (порог 300 слов), ранг следующих не сдвигает нумерацию запроса — тест в Задаче 7.
4. `RESEARCH_SCREENSHOTS=true`, но Browserless не отвечает — строки досье сохраняются без `screenshot_path`, причина в `note`, прогон `done` — тест в Задаче 7.
5. Папка `content_guides/` отсутствует на диске (свежий клон без каталога) — `load_guides` отдаёт пустой текст и `files=[]`, а не `FileNotFoundError` — тест в Задаче 3.

---

### Task 1: Живые пробы форматов → фикстуры

**Files:**
- Create: `backend/tests/fixtures/research/aparser_serp_durev.json`
- Create: `backend/tests/fixtures/research/competitor_1.html`, `competitor_2.html`, `competitor_1.css`
- Create: `backend/tests/fixtures/research/browserless_screenshot.json`, `browserless_function.json`
- Create: `docs/v2/research/research-live-formats-2026-10.md`

**Interfaces:**
- Produces: фикстуры с живыми образцами; заметка о форматах, которую читают Задачи 5–7.

- [ ] **Step 1: SERP и страницы конкурентов через A-Parser (на боксе)**

```bash
ssh Sereja0210@192.168.1.77 "cd /d D:\combine_machine && docker compose exec -T backend python -" <<'EOF'
import json, sys
from app.integrations.aparser import AParserClient
ap = AParserClient()
urls = ap.serp_urls("Durev VPN обзор", limit=10)
print(json.dumps({"query": "Durev VPN обзор", "urls": urls}, ensure_ascii=False))
got = 0
for u in urls:
    h = ap.fetch_html(u)
    if h and len(h) > 5000:
        got += 1
        sys.stdout.write(f"\n===HTML{got} {u}\n" + h[:400000])
    if got == 2: break
EOF
```
Сохранить JSON первой строки в `aparser_serp_durev.json`; тела после маркеров `===HTML1`/`===HTML2` —
в `competitor_1.html`/`competitor_2.html` (целиком, без обрезки, если < 400 КБ). Из `competitor_1.html`
взять первый `<link rel="stylesheet" href=…>`, скачать тем же `fetch_html` и сохранить `competitor_1.css`
(если CSS инлайновый — положить пустой файл и записать это в заметку).

- [ ] **Step 2: Browserless `/screenshot` и `/function` (на боксе)**

```bash
ssh Sereja0210@192.168.1.77 "curl -s -o D:\claude-code\workspace\probe.png -w \"%{http_code} %{content_type} %{size_download}\" -X POST http://127.0.0.1:3000/screenshot -H \"Content-Type: application/json\" -d \"{\\\"url\\\":\\\"https://tunnelnotes.xyz/\\\",\\\"options\\\":{\\\"fullPage\\\":true,\\\"type\\\":\\\"png\\\"},\\\"viewport\\\":{\\\"width\\\":1366,\\\"height\\\":768}}\""
ssh Sereja0210@192.168.1.77 "curl -s -w \" %{http_code}\" -X POST http://127.0.0.1:3000/function -H \"Content-Type: application/javascript\" --data-binary \"export default async function ({ page }) { await page.setViewport({width: 390, height: 800}); await page.setContent('<html><body style=\\\"margin:0\\\"><div style=\\\"width:900px\\\">x</div></body></html>'); const w = await page.evaluate(() => [document.documentElement.scrollWidth, window.innerWidth]); return { data: { scrollWidth: w[0], innerWidth: w[1] }, type: 'application/json' }; }\""
```
Записать в `browserless_screenshot.json`: `{"status": <код>, "content_type": "<тип>", "bytes": <размер>}`;
в `browserless_function.json` — тело ответа второй команды как есть. Если `/function` требует другой
формат (ответ 4xx) — попробовать `-H "Content-Type: application/json"` с `{"code": "<тот же модуль>"}` и
зафиксировать рабочий вариант в заметке.

- [ ] **Step 3: Читает ли Claude Code через шлюз PNG из `/workspace`**

```bash
ssh Sereja0210@192.168.1.77 "curl -s -X POST http://127.0.0.1:3033/v1/chat/completions -H \"Content-Type: application/json\" -d \"{\\\"model\\\":\\\"sonnet\\\",\\\"messages\\\":[{\\\"role\\\":\\\"user\\\",\\\"content\\\":\\\"Открой файл /workspace/probe.png инструментом Read и опиши одним предложением, что на нём. Если инструмент недоступен — ответь ровно: READ_DENIED\\\"}]}\""
```
Результат (описание или `READ_DENIED`) — в заметку: от него зависит §7.2 спеки (план В).

- [ ] **Step 4: Заметка о форматах**

`docs/v2/research/research-live-formats-2026-10.md`: дата, три команды выше, что вернули (коды, типы,
размеры, форма JSON `/function`, вердикт по Read PNG), какие URL в SERP (есть ли `goto`-редиректы и
отдаёт ли `fetch_html` по ним HTML).

- [ ] **Step 5: Commit**

```bash
git add backend/tests/fixtures/research docs/v2/research/research-live-formats-2026-10.md
git commit -m "fixtures(research): живые образцы SERP/HTML/CSS конкурентов, Browserless screenshot+function, проба Read PNG через шлюз"
```

---

### Task 2: Миграция 0036 и модели

**Files:**
- Create: `backend/app/models/research.py`
- Create: `backend/alembic/versions/0036_research_theme_blocks.py`
- Modify: `backend/app/models/site.py` (класс `Page`, после `critic_checked_at`)
- Modify: `backend/app/models/autonomy.py` (после `auto_check_index`, после `cap_check_index`)
- Modify: `backend/app/models/__init__.py`
- Modify: `backend/app/services/autonomy.py` (`_BOOL_KEYS`, `_INT_BOUNDS`, `_DEFAULTS`)
- Test: `backend/tests/test_research_models.py`

**Interfaces:**
- Produces: `SiteResearch`, `SiteTheme` (ORM), `Page.blocks: dict|None`, `Page.blocks_stale: bool`,
  `AutonomySettings.auto_research/auto_design/auto_edit: bool`, `cap_research/cap_design: int`;
  `get_autonomy()` отдаёт новые ключи.

- [ ] **Step 1: Failing test**

```python
# backend/tests/test_research_models.py
"""Миграция 0036: досье, макет, блоки страницы, тумблеры автопилота (план А, спека 2026-10-10)."""
import app.db as db
from app.models.research import SiteResearch, SiteTheme
from app.models.site import Page, Site
from app.models.domain import Domain
from app.services.autonomy import get_autonomy, update_autonomy


def _site() -> int:
    with db.SessionLocal() as s:
        d = Domain(domain="r.com", source="list", status="purchased")
        s.add(d); s.commit()
        site = Site(domain_id=d.id, status="content", doc_root="/www/wwwroot/r.com")
        s.add(site); s.commit()
        return site.id


def test_research_and_theme_rows_roundtrip():
    sid = _site()
    with db.SessionLocal() as s:
        s.add(SiteResearch(site_id=sid, kind="review", query="x обзор", rank=1, url="https://a/1",
                           final_url="https://a/1", domain="a", words=320, headings=[["h2", "Цена"]],
                           tables=[], faq=[{"q": "Сколько?", "a": "5"}], numbers=[{"value": "5", "ctx": "5 $ в месяц"}],
                           css_tokens={"fonts": ["Inter"]}, text="пять долларов", note=None))
        s.add(SiteTheme(site_id=sid, version=1, status="ok", theme={"name": "x"}, layout_html="<html>{{content}}</html>",
                        css=".a{}", brief="b", model="sonnet", screenshots={}))
        s.commit()
        r = s.query(SiteResearch).filter_by(site_id=sid).one()
        assert r.faq[0]["q"] == "Сколько?" and r.css_tokens["fonts"] == ["Inter"] and r.fetched_at is not None
        t = s.query(SiteTheme).filter_by(site_id=sid).one()
        assert t.version == 1 and t.created_at is not None


def test_page_blocks_default_and_stale_flag():
    sid = _site()
    with db.SessionLocal() as s:
        p = Page(site_id=sid, url_path="/", title="t", body="<p>x</p>")
        s.add(p); s.commit()
        assert p.blocks is None and p.blocks_stale is False
        p.blocks = {"sections": []}; p.blocks_stale = True; s.commit()
        assert s.get(Page, p.id).blocks == {"sections": []}


def test_autonomy_has_new_toggles_with_safe_defaults():
    a = get_autonomy()
    assert a["auto_research"] is False and a["auto_design"] is False and a["auto_edit"] is False
    assert a["cap_research"] == 5 and a["cap_design"] == 3
    assert update_autonomy(auto_edit=True, cap_research=999)["cap_research"] == 500   # кламп
```

- [ ] **Step 2: Run → FAIL** (`ModuleNotFoundError: app.models.research`)

Run: `.venv/bin/python -m pytest backend/tests/test_research_models.py -q -p no:cacheprovider < /dev/null`

- [ ] **Step 3: Модели**

```python
# backend/app/models/research.py
"""Досье конкурентов (site_research) и дизайн-макет сайта (site_theme) — спека 2026-10-10 §4.4, §7.5."""
from datetime import datetime
from sqlalchemy import String, Text, Integer, JSON, ForeignKey, DateTime, Index, func
from sqlalchemy.orm import Mapped, mapped_column
from app.db import Base


class SiteResearch(Base):
    __tablename__ = "site_research"

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))          # review | comparison | howto | market
    query: Mapped[str] = mapped_column(String(255))
    rank: Mapped[int] = mapped_column(Integer)             # позиция в выдаче (1..)
    url: Mapped[str] = mapped_column(Text)
    final_url: Mapped[str | None] = mapped_column(Text)
    domain: Mapped[str | None] = mapped_column(String(255))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    words: Mapped[int] = mapped_column(Integer, default=0)
    headings: Mapped[list | None] = mapped_column(JSON)    # [[tag, text], ...]
    tables: Mapped[list | None] = mapped_column(JSON)      # [[[cell, ...], ...], ...]
    faq: Mapped[list | None] = mapped_column(JSON)         # [{"q","a"}]
    numbers: Mapped[list | None] = mapped_column(JSON)     # [{"value","ctx"}]
    css_tokens: Mapped[dict | None] = mapped_column(JSON)  # {"fonts","colors","container_px","components"}
    text: Mapped[str | None] = mapped_column(Text)         # видимый текст целиком — для шинглов критика
    screenshot_path: Mapped[str | None] = mapped_column(String(512))
    note: Mapped[str | None] = mapped_column(Text)         # причина пропуска/отказа словами

    __table_args__ = (Index("ix_site_research_site_kind", "site_id", "kind"),)


class SiteTheme(Base):
    __tablename__ = "site_theme"

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(16), default="ok")   # ok | failed
    theme: Mapped[dict | None] = mapped_column(JSON)
    layout_html: Mapped[str | None] = mapped_column(Text)
    css: Mapped[str | None] = mapped_column(Text)
    brief: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(String(64))
    screenshots: Mapped[dict | None] = mapped_column(JSON)          # {"1366": path, "390": path}
    operator_note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
```

В `backend/app/models/site.py`, класс `Page`, после `critic_checked_at`:
```python
    # Спека 2026-10-10 §6.2: структурный ответ писателя (PageDoc как dict); body — его рендер.
    # blocks_stale=True — body правили руками в редакторе, рендер из blocks его не затирает.
    blocks: Mapped[dict | None] = mapped_column(JSON)
    blocks_stale: Mapped[bool] = mapped_column(Boolean, default=False)
```

В `backend/app/models/autonomy.py` после `auto_check_index`:
```python
    # спека 2026-10-10 §8: досье конкурентов, дизайн-макет, критик ставит edited сам
    auto_research: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_design: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_edit: Mapped[bool] = mapped_column(Boolean, default=False)
```
после `cap_check_index`:
```python
    cap_research: Mapped[int] = mapped_column(Integer, default=5)
    cap_design: Mapped[int] = mapped_column(Integer, default=3)
```

В `backend/app/models/__init__.py`: добавить `from app.models.research import SiteResearch, SiteTheme` и
оба имени в `__all__`.

В `backend/app/services/autonomy.py`:
```python
_BOOL_KEYS = ("autopilot_on", "auto_discovery", "auto_score", "auto_queue",
              "auto_provision", "auto_generate", "auto_publish", "auto_check_index",
              "auto_research", "auto_design", "auto_edit")
_INT_BOUNDS = {
    "sweep_interval_min": (5, 1440),
    "cap_score": (0, 500), "cap_queue": (0, 500), "cap_provision": (0, 500),
    "cap_generate": (0, 500), "cap_publish": (0, 500), "cap_check_index": (0, 500),
    "cap_research": (0, 500), "cap_design": (0, 500),
}
_DEFAULTS = {
    "autopilot_on": False, "sweep_interval_min": 60,
    "auto_discovery": False, "auto_score": False, "auto_queue": False,
    "auto_provision": False, "auto_generate": False, "auto_publish": False,
    "auto_check_index": False, "auto_research": False, "auto_design": False, "auto_edit": False,
    "cap_score": 20, "cap_queue": 10, "cap_provision": 5,
    "cap_generate": 5, "cap_publish": 5, "cap_check_index": 20, "cap_research": 5, "cap_design": 3,
}
```
(Проверить, что `get_autonomy()` отдаёт все ключи `_DEFAULTS` — если он перечисляет колонки явно, добавить новые.)

- [ ] **Step 4: Миграция**

```python
# backend/alembic/versions/0036_research_theme_blocks.py
"""досье конкурентов, дизайн-макет, блоки страницы, тумблеры research/design/edit (спека 2026-10-10)"""
import sqlalchemy as sa
from alembic import op

revision = "0036_research_theme_blocks"
down_revision = "0035_drop_dead_overrides"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "site_research",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("site_id", sa.Integer, sa.ForeignKey("sites.id"), nullable=False, index=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("query", sa.String(255), nullable=False),
        sa.Column("rank", sa.Integer, nullable=False),
        sa.Column("url", sa.Text, nullable=False),
        sa.Column("final_url", sa.Text),
        sa.Column("domain", sa.String(255)),
        sa.Column("fetched_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("words", sa.Integer, nullable=False, server_default="0"),
        sa.Column("headings", sa.JSON), sa.Column("tables", sa.JSON), sa.Column("faq", sa.JSON),
        sa.Column("numbers", sa.JSON), sa.Column("css_tokens", sa.JSON),
        sa.Column("text", sa.Text), sa.Column("screenshot_path", sa.String(512)), sa.Column("note", sa.Text),
    )
    op.create_index("ix_site_research_site_kind", "site_research", ["site_id", "kind"])
    op.create_table(
        "site_theme",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("site_id", sa.Integer, sa.ForeignKey("sites.id"), nullable=False, index=True),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("status", sa.String(16), nullable=False, server_default="ok"),
        sa.Column("theme", sa.JSON), sa.Column("layout_html", sa.Text), sa.Column("css", sa.Text),
        sa.Column("brief", sa.Text), sa.Column("model", sa.String(64)), sa.Column("screenshots", sa.JSON),
        sa.Column("operator_note", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.add_column("pages", sa.Column("blocks", sa.JSON))
    op.add_column("pages", sa.Column("blocks_stale", sa.Boolean, nullable=False, server_default=sa.false()))
    for col in ("auto_research", "auto_design", "auto_edit"):
        op.add_column("autonomy_settings", sa.Column(col, sa.Boolean, nullable=False, server_default=sa.false()))
    op.add_column("autonomy_settings", sa.Column("cap_research", sa.Integer, nullable=False, server_default="5"))
    op.add_column("autonomy_settings", sa.Column("cap_design", sa.Integer, nullable=False, server_default="3"))


def downgrade():
    for col in ("cap_design", "cap_research", "auto_edit", "auto_design", "auto_research"):
        op.drop_column("autonomy_settings", col)
    op.drop_column("pages", "blocks_stale")
    op.drop_column("pages", "blocks")
    op.drop_table("site_theme")
    op.drop_index("ix_site_research_site_kind", table_name="site_research")
    op.drop_table("site_research")
```

- [ ] **Step 5: Run → PASS**, затем весь сьют и pyflakes.

- [ ] **Step 6: Commit**

```bash
git add backend/app/models backend/alembic/versions/0036_research_theme_blocks.py backend/app/services/autonomy.py backend/tests/test_research_models.py
git commit -m "feat(db): миграция 0036 — site_research, site_theme, Page.blocks, тумблеры auto_research/auto_design/auto_edit"
```

---

### Task 3: Правила письма — `services/guides.py`

**Files:**
- Create: `backend/app/services/guides.py`
- Modify: `backend/app/config.py` (рядом с `LLM_*`): `CONTENT_GUIDES_DIR: str = ""`
- Test: `backend/tests/test_guides.py`

**Interfaces:**
- Produces:
  - `SUBDIRS: tuple[str, ...] = ("", "ru", "en", "de", "es", "fr", "nl", "pt", "it", "review", "comparison", "howto")`
  - `guides_dir() -> pathlib.Path`
  - `list_guides() -> list[dict]` — `[{"rel": "ru/10-тон.md", "size": int, "mtime": iso}]`
  - `load_guides(lang: str | None, kind: str | None, limit: int = 40_000) -> dict` —
    `{"text": str, "files": [rel, ...], "truncated": bool}`
  - `save_guide(subdir: str, filename: str, data: bytes) -> str` (rel-путь) / `ValueError`
  - `delete_guide(rel: str) -> None` / `ValueError`
  - `MAX_FILE = 200 * 1024`, `ALLOWED_EXT = (".md", ".txt")`

- [ ] **Step 1: Failing tests**

```python
# backend/tests/test_guides.py
"""content_guides/: склейка правил письма по языку/типу страницы, лимит, безопасная загрузка (спека §5)."""
import pytest
from app.config import settings
from app.services import guides


@pytest.fixture
def gdir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "CONTENT_GUIDES_DIR", str(tmp_path))
    return tmp_path


def test_load_merges_root_then_lang_then_kind_in_alpha_order(gdir):
    (gdir / "20-b.md").write_text("корень B")
    (gdir / "10-a.md").write_text("корень A")
    (gdir / "ru").mkdir(); (gdir / "ru" / "тон.md").write_text("по-русски")
    (gdir / "review").mkdir(); (gdir / "review" / "обзор.txt").write_text("структура обзора")
    (gdir / "en").mkdir(); (gdir / "en" / "x.md").write_text("english only")
    (gdir / "ru" / "скрытый.pdf").write_bytes(b"%PDF")              # чужое расширение не читаем
    r = guides.load_guides("ru", "review")
    assert r["files"] == ["10-a.md", "20-b.md", "ru/тон.md", "review/обзор.txt"] and not r["truncated"]
    assert r["text"].index("корень A") < r["text"].index("корень B") < r["text"].index("по-русски") < r["text"].index("структура обзора")
    assert "english only" not in r["text"] and "--- ru/тон.md ---" in r["text"]


def test_load_without_folder_is_empty_not_error(gdir, monkeypatch):
    monkeypatch.setattr(settings, "CONTENT_GUIDES_DIR", str(gdir / "нет-такой"))
    assert guides.load_guides("ru", "review") == {"text": "", "files": [], "truncated": False}


def test_limit_drops_files_from_the_end_and_flags(gdir):
    (gdir / "a.md").write_text("A" * 30)
    (gdir / "b.md").write_text("B" * 30)
    r = guides.load_guides(None, None, limit=50)
    assert r["files"] == ["a.md"] and r["truncated"] is True and "B" not in r["text"]


def test_save_and_delete_are_confined_to_whitelist(gdir):
    rel = guides.save_guide("ru", "10-тон.md", "текст".encode())
    assert rel == "ru/10-тон.md" and (gdir / "ru" / "10-тон.md").read_text() == "текст"
    assert guides.list_guides()[0]["rel"] == "ru/10-тон.md"
    for sub, name in (("..", "x.md"), ("ru", "../x.md"), ("ru", "..\\x.md"), ("ru", ".env"),
                      ("ru", "a b.md"), ("pl", "x.md"), ("ru", "x.exe"), ("ru", "")):
        with pytest.raises(ValueError):
            guides.save_guide(sub, name, b"x")
    with pytest.raises(ValueError):
        guides.save_guide("ru", "big.md", b"x" * (guides.MAX_FILE + 1))
    assert not (gdir.parent / "x.md").exists()
    guides.delete_guide("ru/10-тон.md")
    assert guides.list_guides() == []
    with pytest.raises(ValueError):
        guides.delete_guide("../README.md")
```

- [ ] **Step 2: Run → FAIL** (`ImportError: guides`).

- [ ] **Step 3: Реализация**

```python
# backend/app/services/guides.py
"""Правила письма оператора — папка content_guides/ (спека 2026-10-10 §5).

Корень действует на всё, подпапка языка (ru/en/…) — на язык вывода, подпапка типа (review/
comparison/howto) — на тип страницы. Файлы по алфавиту; итог — один текст для системного промпта
писателя и критика. Загрузка из панели пишет СЮДА ЖЕ, путь строится только из белого списка подпапок
и санированного имени — никаких `..`, абсолютных путей, чужих расширений.
"""
import re
from datetime import datetime, timezone
from pathlib import Path

from app.config import settings

LANGS = ("ru", "en", "de", "es", "fr", "nl", "pt", "it")
KINDS = ("review", "comparison", "howto")
SUBDIRS = ("",) + LANGS + KINDS
ALLOWED_EXT = (".md", ".txt")
MAX_FILE = 200 * 1024
_NAME_RE = re.compile(r"^[A-Za-z0-9А-Яа-яЁё._-]{1,80}$")


def guides_dir() -> Path:
    """Явный CONTENT_GUIDES_DIR, иначе /repo/content_guides (бокс, compose монтирует репо в /repo),
    иначе <корень репо>/content_guides от этого файла (локальный запуск). Может не существовать."""
    if settings.CONTENT_GUIDES_DIR:
        return Path(settings.CONTENT_GUIDES_DIR)
    repo = Path("/repo/content_guides")
    if repo.is_dir():
        return repo
    return Path(__file__).resolve().parents[3] / "content_guides"


def _files_in(sub: str) -> list[Path]:
    d = guides_dir() / sub if sub else guides_dir()
    if not d.is_dir():
        return []
    return sorted(p for p in d.iterdir() if p.is_file() and p.suffix.lower() in ALLOWED_EXT and not p.name.startswith("."))


def _rel(p: Path) -> str:
    return p.relative_to(guides_dir()).as_posix()


def list_guides() -> list[dict]:
    out = []
    for sub in SUBDIRS:
        for p in _files_in(sub):
            st = p.stat()
            out.append({"rel": _rel(p), "size": st.st_size,
                        "mtime": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(timespec="minutes")})
    return out


def load_guides(lang: str | None, kind: str | None, limit: int = 40_000) -> dict:
    """{"text", "files", "truncated"}: корень + язык + тип, по алфавиту, с разделителями `--- rel ---`.
    Превышение лимита режет файлы С КОНЦА целиком (пол-файла правил хуже, чем его отсутствие)."""
    subs = [""] + ([lang] if lang in LANGS else []) + ([kind] if kind in KINDS else [])
    parts, files, total, truncated = [], [], 0, False
    for sub in subs:
        for p in _files_in(sub):
            body = p.read_text(encoding="utf-8", errors="replace").strip()
            chunk = f"--- {_rel(p)} ---\n{body}\n"
            if total + len(chunk) > limit:
                truncated = True
                break
            parts.append(chunk); files.append(_rel(p)); total += len(chunk)
        if truncated:
            break
    return {"text": "\n".join(parts), "files": files, "truncated": truncated}


def _check_name(filename: str) -> str:
    name = (filename or "").strip()
    if not _NAME_RE.match(name) or ".." in name or "/" in name or "\\" in name \
            or name.startswith(".") or not name.lower().endswith(ALLOWED_EXT):
        raise ValueError("имя файла: только буквы/цифры/._-, расширение .md или .txt")
    return name


def save_guide(subdir: str, filename: str, data: bytes) -> str:
    sub = (subdir or "").strip()
    if sub not in SUBDIRS:
        raise ValueError(f"подпапка «{sub}» не из списка: корень, {', '.join(LANGS)}, {', '.join(KINDS)}")
    name = _check_name(filename)
    if len(data) > MAX_FILE:
        raise ValueError(f"файл больше {MAX_FILE // 1024} КБ")
    d = guides_dir() / sub if sub else guides_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_bytes(data)
    return _rel(d / name)


def delete_guide(rel: str) -> None:
    sub, _, name = (rel or "").replace("\\", "/").rpartition("/")
    if sub not in SUBDIRS:
        raise ValueError("путь вне папки правил")
    name = _check_name(name)
    p = (guides_dir() / sub if sub else guides_dir()) / name
    if not p.is_file():
        raise ValueError(f"файла {rel} нет")
    p.unlink()
```

В `config.py` рядом с `LLM_*`:
```python
    CONTENT_GUIDES_DIR: str = ""      # пусто -> /repo/content_guides (бокс) или <репо>/content_guides
```

- [ ] **Step 4: Run → PASS**; pyflakes.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/guides.py backend/app/config.py backend/tests/test_guides.py
git commit -m "feat(M4): правила письма из content_guides/ — склейка по языку/типу, лимит, безопасная запись"
```

---

### Task 4: Загрузка правил на `/settings`

**Files:**
- Modify: `backend/app/api/panel.py` (`_settings_page` — контекст; новые роуты после `settings_view`)
- Modify: `backend/app/templates/settings.html` (новая `.station` после последней, перед `</form>` — вне формы `/settings/save`)
- Test: `backend/tests/test_guides_panel.py`

**Interfaces:**
- Consumes: `guides.list_guides/save_guide/delete_guide/SUBDIRS`.
- Produces: `POST /settings/guides/upload` (multipart: `subdir`, `file`), `POST /settings/guides/delete` (`rel`).

- [ ] **Step 1: Failing tests**

```python
# backend/tests/test_guides_panel.py
"""Карточка «Правила письма» на /settings: список, загрузка, удаление; мусорные пути не проходят."""
import pytest
from app.config import settings
from app.services import guides


@pytest.fixture
def gdir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "CONTENT_GUIDES_DIR", str(tmp_path))
    return tmp_path


def test_settings_lists_guides_and_upload_form(client, gdir):
    (gdir / "10-тон.md").write_text("x")
    html = client.get("/settings").text
    assert "Правила письма" in html and "10-тон.md" in html and 'action="/settings/guides/upload"' in html


def test_upload_writes_file_and_redirects(client, gdir):
    r = client.post("/settings/guides/upload", data={"subdir": "ru"},
                    files={"file": ("20-структура.md", "## Структура\n".encode(), "text/markdown")},
                    follow_redirects=False)
    assert r.status_code == 303 and "msg=" in r.headers["location"]
    assert (gdir / "ru" / "20-структура.md").read_text() == "## Структура\n"


@pytest.mark.parametrize("subdir,name", [("..", "x.md"), ("ru", "../x.md"), ("ru", ".env"), ("ru", "x.exe")])
def test_upload_rejects_bad_paths(client, gdir, subdir, name):
    r = client.post("/settings/guides/upload", data={"subdir": subdir},
                    files={"file": (name, b"x", "text/plain")}, follow_redirects=False)
    assert r.status_code == 303 and "err=" in r.headers["location"]
    assert guides.list_guides() == [] and not (gdir.parent / "x.md").exists()


def test_delete_removes_only_listed_file(client, gdir):
    guides.save_guide("", "a.md", b"a")
    r = client.post("/settings/guides/delete", data={"rel": "a.md"}, follow_redirects=False)
    assert r.status_code == 303 and guides.list_guides() == []
    r = client.post("/settings/guides/delete", data={"rel": "../README.md"}, follow_redirects=False)
    assert "err=" in r.headers["location"]
```

- [ ] **Step 2: Run → FAIL** (404 на роуты / нет текста в шаблоне).

- [ ] **Step 3: Роуты и шаблон**

В `panel.py` после `settings_view`:
```python
@router.post("/settings/guides/upload")
async def guides_upload_action(request: Request):
    """Загрузить файл правил письма в content_guides/<subdir>/ (спека 2026-10-10 §5).
    Путь строится ТОЛЬКО из белого списка подпапок и санированного имени — см. guides.save_guide."""
    from app.services import guides
    form = await request.form()
    up = form.get("file")
    subdir = form.get("subdir") if isinstance(form.get("subdir"), str) else ""
    if up is None or not getattr(up, "filename", ""):
        return _back("/settings", err="правила: файл не выбран")
    data = await up.read()
    try:
        rel = guides.save_guide(subdir, up.filename, data)
    except ValueError as e:
        return _back("/settings", err=f"правила: {e}")
    return _back("/settings", msg=f"Правила письма: сохранён {rel} ({len(data)} байт)")


@router.post("/settings/guides/delete")
def guides_delete_action(rel: str = Form("")):
    from app.services import guides
    try:
        guides.delete_guide(rel)
    except ValueError as e:
        return _back("/settings", err=f"правила: {e}")
    return _back("/settings", msg=f"Правила письма: удалён {rel}")
```
В `_settings_page` добавить в контекст шаблона: `"guides": guides.list_guides(), "guide_subdirs": guides.SUBDIRS`
(импорт `from app.services import guides` внутри функции, как остальные).

В `settings.html` после закрывающего `</form>` формы `/settings/save`:
```html
<div class="station" style="max-width:760px; margin-top:14px">
  <div class="plate">M4 · Правила письма — <b>{{ guides|length }} файлов</b></div>
  <details class="what"><summary>зачем это</summary>
    <div class="what-body">Твои рекомендации, как писать тексты: склеиваются в системный промпт писателя
    и критика. Корень — на всё, подпапка языка — на язык вывода, подпапка типа — на тип страницы.
    Порядок — по имени файла (префикс 10-, 20-). Лежат в <code>content_guides/</code> репо.</div></details>
  {% if guides %}
  <table class="data" style="margin:8px 0"><thead><tr><th>файл</th><th>размер</th><th>изменён</th><th></th></tr></thead>
  <tbody>{% for g in guides %}<tr><td>{{ g.rel }}</td><td>{{ g.size }} Б</td><td>{{ g.mtime }}</td>
    <td><form class="inline" method="post" action="/settings/guides/delete"><input type="hidden" name="rel" value="{{ g.rel }}">
      <button class="btn-sm" title="удалить файл правил">✕</button></form></td></tr>{% endfor %}</tbody></table>
  {% endif %}
  <form class="go" method="post" action="/settings/guides/upload" enctype="multipart/form-data" style="display:flex; gap:8px; align-items:center">
    <select name="subdir" title="куда: корень — на всё; язык; тип страницы">
      {% for s in guide_subdirs %}<option value="{{ s }}">{{ s or 'корень (на всё)' }}</option>{% endfor %}</select>
    <input type="file" name="file" accept=".md,.txt" required>
    <button class="btn-sm btn-acc" title="загрузить .md/.txt до 200 КБ">↑ Загрузить</button>
  </form>
</div>
```

- [ ] **Step 4: Run → PASS**; весь сьют; pyflakes. Визуально: не требуется (контракт классов `.station/.plate/.what/.go/.data` из base.html).

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/panel.py backend/app/templates/settings.html backend/tests/test_guides_panel.py
git commit -m "feat(panel): карточка «Правила письма» на /settings — список, загрузка, удаление (белый список путей)"
```

---

### Task 5: Транспорт Browserless — `integrations/browserless.py`

**Files:**
- Create: `backend/app/integrations/browserless.py`
- Modify: `backend/app/config.py`: `BROWSERLESS_URL: str = "http://192.168.1.77:3000"`, `BROWSERLESS_TOKEN: str = ""`
- Test: `backend/tests/test_browserless.py`

**Interfaces:**
- Produces: `BrowserlessClient(BaseClient)` с
  - `screenshot(url: str, *, width: int = 1366, height: int = 768, full_page: bool = True) -> bytes`
  - `screenshot_html(html: str, *, width: int, height: int) -> bytes` (через `/screenshot` с `html` вместо `url`)
  - `overflow(html: str, width: int) -> dict` → `{"scrollWidth": int, "innerWidth": int}` (через `/function`)
  - `ping() -> bool` (`GET /json/version` → 200)
  - `class BrowserlessError(RuntimeError)`
- Форму тел запросов/ответов сверить с `docs/v2/research/research-live-formats-2026-10.md` (Задача 1); ниже — по документации Browserless v2, поправить при расхождении.

- [ ] **Step 1: Failing tests**

```python
# backend/tests/test_browserless.py
"""Browserless: /screenshot (PNG), /function (JSON), ping; ошибки — BrowserlessError, не падение."""
import json
import httpx
import pytest
from app.config import settings
from app.integrations.browserless import BrowserlessClient, BrowserlessError


@pytest.fixture
def mk(monkeypatch):
    monkeypatch.setattr(settings, "BROWSERLESS_URL", "http://bl:3000")
    monkeypatch.setattr(settings, "BROWSERLESS_TOKEN", "tok")

    def _mk(handler):
        c = BrowserlessClient()
        c._client = httpx.Client(transport=httpx.MockTransport(handler))
        return c
    return _mk


def test_screenshot_posts_url_viewport_and_returns_png(mk):
    seen = {}
    def h(req):
        seen["url"], seen["body"] = str(req.url), json.loads(req.content)
        return httpx.Response(200, content=b"\x89PNG...", headers={"content-type": "image/png"})
    png = mk(h).screenshot("https://ex.com/", width=1366, height=768)
    assert png.startswith(b"\x89PNG") and seen["url"] == "http://bl:3000/screenshot?token=tok"
    assert seen["body"]["url"] == "https://ex.com/" and seen["body"]["viewport"] == {"width": 1366, "height": 768}
    assert seen["body"]["options"] == {"fullPage": True, "type": "png"}


def test_screenshot_non_png_or_error_raises(mk):
    with pytest.raises(BrowserlessError):
        mk(lambda r: httpx.Response(500, text="boom")).screenshot("https://ex.com/")
    with pytest.raises(BrowserlessError):
        mk(lambda r: httpx.Response(200, text="<html>", headers={"content-type": "text/html"})).screenshot("https://ex.com/")


def test_overflow_runs_function_and_parses_json(mk):
    def h(req):
        assert req.url.path == "/function" and "scrollWidth" in req.content.decode()
        return httpx.Response(200, json={"scrollWidth": 900, "innerWidth": 390})
    assert mk(h).overflow("<div style='width:900px'>x</div>", 390) == {"scrollWidth": 900, "innerWidth": 390}


def test_ping(mk):
    assert mk(lambda r: httpx.Response(200, json={"Browser": "Chrome"})).ping() is True
    assert mk(lambda r: httpx.Response(503)).ping() is False
```

- [ ] **Step 2: Run → FAIL**.

- [ ] **Step 3: Реализация**

```python
# backend/app/integrations/browserless.py
"""Browserless (бокс :3000, контейнер `browserless`): скриншоты страниц конкурентов и макета, проверка
горизонтального переполнения. Только транспорт (спека 2026-10-10 §4.3, §7.4). Формы запросов — по
docs/v2/research/research-live-formats-2026-10.md."""
import json

from app.config import settings
from app.integrations.base import BaseClient


class BrowserlessError(RuntimeError):
    pass


class BrowserlessClient(BaseClient):
    POOLED = True

    def __init__(self, timeout: float = 60.0):
        super().__init__(settings.BROWSERLESS_URL, timeout=timeout)
        self.token = settings.BROWSERLESS_TOKEN

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}" + (f"?token={self.token}" if self.token else "")

    def _png(self, body: dict) -> bytes:
        r = self.request("POST", self._url("/screenshot"), json=body, retry=False)
        if r.status_code != 200 or not r.headers.get("content-type", "").startswith("image/"):
            raise BrowserlessError(f"screenshot: HTTP {r.status_code} {r.headers.get('content-type', '')} {r.text[:120]}")
        return r.content

    def screenshot(self, url: str, *, width: int = 1366, height: int = 768, full_page: bool = True) -> bytes:
        return self._png({"url": url, "options": {"fullPage": full_page, "type": "png"},
                          "viewport": {"width": width, "height": height}})

    def screenshot_html(self, html: str, *, width: int = 1366, height: int = 768) -> bytes:
        return self._png({"html": html, "options": {"fullPage": True, "type": "png"},
                          "viewport": {"width": width, "height": height}})

    def overflow(self, html: str, width: int) -> dict:
        code = ("export default async function ({ page }) {"
                f" await page.setViewport({{width: {int(width)}, height: 800}});"
                f" await page.setContent({json.dumps(html)});"
                " const w = await page.evaluate(() => [document.documentElement.scrollWidth, window.innerWidth]);"
                " return { data: { scrollWidth: w[0], innerWidth: w[1] }, type: 'application/json' }; }")
        r = self.request("POST", self._url("/function"), content=code.encode(),
                         headers={"Content-Type": "application/javascript"}, retry=False)
        if r.status_code != 200:
            raise BrowserlessError(f"function: HTTP {r.status_code} {r.text[:120]}")
        try:
            d = r.json()
        except ValueError:
            raise BrowserlessError("function: не-JSON ответ") from None
        return {"scrollWidth": int(d.get("scrollWidth", 0)), "innerWidth": int(d.get("innerWidth", 0))}

    def ping(self) -> bool:
        try:
            return self.request("GET", self._url("/json/version"), retry=False).status_code == 200
        except Exception:  # noqa: BLE001
            return False
```
Если `BaseClient.request` не принимает `retry`/`content` так, как здесь, — подстроить под его сигнатуру
(`backend/app/integrations/base.py:138+`), не меняя `BaseClient`.

- [ ] **Step 4: Run → PASS**; pyflakes.

- [ ] **Step 5: Commit**

```bash
git add backend/app/integrations/browserless.py backend/app/config.py backend/tests/test_browserless.py
git commit -m "feat(integrations): клиент Browserless — screenshot/html, overflow через /function, ping"
```

---

### Task 6: Извлечение из страницы — `services/research_extract.py`

**Files:**
- Create: `backend/app/services/research_extract.py`
- Test: `backend/tests/test_research_extract.py` (использует фикстуры Задачи 1 + синтетику)

**Interfaces:**
- Produces (все чистые, без сети/БД):
  - `visible_text(html: str) -> str` — делегирует `app.integrations.wayback._visible_text`
  - `headings(html: str, cap: int = 40) -> list[list[str]]` — `[["h2","Цена"], ...]`, h1–h3, без нав-мусора
  - `tables(html: str, cap: int = 5) -> list[list[list[str]]]`
  - `faq(html: str, cap: int = 20) -> list[dict]` — `[{"q","a"}]`
  - `numbers(text: str, cap: int = 60) -> list[dict]` — `[{"value": "5.99", "ctx": "… ±8 слов …"}]`
  - `stylesheet_links(html: str, base_url: str, cap: int = 3) -> list[str]`
  - `css_tokens(html: str, css_texts: list[str]) -> dict` — `{"fonts": [..3], "colors": [..6], "container_px": int|None, "components": [..]}`
  - `extract_all(html: str, css_texts: list[str]) -> dict` — все поля `SiteResearch` кроме url/rank/screenshot

- [ ] **Step 1: Failing tests**

```python
# backend/tests/test_research_extract.py
"""Извлечение досье из HTML конкурента: текст, заголовки, таблицы, FAQ, числа, CSS-токены (спека §4.3)."""
from pathlib import Path
from app.services import research_extract as rx

FX = Path(__file__).parent / "fixtures" / "research"
SAMPLE = """<html><head><title>Обзор X</title><link rel="stylesheet" href="/s/a.css"><link rel=stylesheet href="https://cdn/b.css">
<style>body{font-family:Inter,Arial;color:#222} .wrap{max-width:1120px} h1{color:#c0392b}</style></head>
<body><nav><h2>Меню</h2></nav><h1>X: обзор</h1><h2>Цена и тарифы</h2><p>От 5.99 $ в месяц при оплате за 2 года, 30 дней на возврат.</p>
<h3>Серверы</h3><p>5500 серверов в 60 странах.</p>
<table><tr><th>План</th><th>Цена</th></tr><tr><td>Год</td><td>4.99 $</td></tr></table>
<details><summary>Есть ли бесплатный тариф?</summary><p>Нет, только 30 дней возврата.</p></details>
<h3>Работает ли с Netflix?</h3><p>Да, в наших тестах — да.</p>
<div class="pros-cons"><ul class="pros"><li>быстро</li></ul></div><script>var casino = 1</script></body></html>"""


def test_visible_text_strips_scripts_and_keeps_title():
    t = rx.visible_text(SAMPLE)
    assert "casino" not in t and "Обзор X" in t and "5500 серверов" in t


def test_headings_skip_nav_and_keep_order():
    assert rx.headings(SAMPLE) == [["h1", "X: обзор"], ["h2", "Цена и тарифы"], ["h3", "Серверы"], ["h3", "Работает ли с Netflix?"]]


def test_tables_and_faq():
    assert rx.tables(SAMPLE) == [[["План", "Цена"], ["Год", "4.99 $"]]]
    f = rx.faq(SAMPLE)
    assert {"q": "Есть ли бесплатный тариф?", "a": "Нет, только 30 дней возврата."} in f
    assert {"q": "Работает ли с Netflix?", "a": "Да, в наших тестах — да."} in f


def test_numbers_with_context():
    n = rx.numbers(rx.visible_text(SAMPLE))
    vals = [x["value"] for x in n]
    assert "5.99" in vals and "30" in vals and "5500" in vals
    assert any("в месяц" in x["ctx"] for x in n if x["value"] == "5.99")


def test_stylesheet_links_and_css_tokens():
    assert rx.stylesheet_links(SAMPLE, "https://ex.com/r/") == ["https://ex.com/s/a.css", "https://cdn/b.css"]
    tok = rx.css_tokens(SAMPLE, [".x{font-family:'Roboto Slab';max-width:960px;color:#c0392b;color:#c0392b;background:#fff}"])
    assert tok["fonts"][0] in ("Inter", "Roboto Slab") and "#c0392b" in tok["colors"] and "#fff" not in tok["colors"]
    assert tok["container_px"] in (960, 1120) and "pros_cons" in tok["components"] and "table" in tok["components"] and "faq" in tok["components"]


def test_extract_all_on_live_fixture():
    html = (FX / "competitor_1.html").read_text(encoding="utf-8", errors="replace")
    d = rx.extract_all(html, [(FX / "competitor_1.css").read_text(encoding="utf-8", errors="replace")])
    assert d["words"] >= 300 and len(d["headings"]) >= 3 and isinstance(d["css_tokens"]["fonts"], list)
    assert set(d) == {"text", "words", "headings", "tables", "faq", "numbers", "css_tokens"}
```

- [ ] **Step 2: Run → FAIL**.

- [ ] **Step 3: Реализация**

```python
# backend/app/services/research_extract.py
"""Чистые функции извлечения досье из HTML/CSS конкурента (спека 2026-10-10 §4.3). Без сети и БД:
stdlib html.parser + re + nh3 (через wayback._visible_text — судим по ВИДИМОМУ тексту)."""
import re
from html.parser import HTMLParser
from urllib.parse import urljoin

from app.integrations.wayback import _visible_text

_NAV_TAGS = {"nav", "header", "footer", "aside"}
_NUM_RE = re.compile(r"(?<![\w.])(\d{1,3}(?:[ \u00a0]\d{3})+|\d+(?:[.,]\d+)?)(?![\w])")
_FONT_RE = re.compile(r"font-family\s*:\s*([^;}]+)", re.I)
_COLOR_RE = re.compile(r"#(?:[0-9a-f]{6}|[0-9a-f]{3})\b", re.I)
_MAXW_RE = re.compile(r"max-width\s*:\s*(\d{3,4})px", re.I)
_LINK_RE = re.compile(r"<link\b[^>]*>", re.I)
_HREF_RE = re.compile(r"href\s*=\s*[\"']?([^\"' >]+)", re.I)
_STYLE_RE = re.compile(r"<style\b[^>]*>(.*?)</style>", re.I | re.S)
_WHITE_BLACK = {"#fff", "#ffffff", "#000", "#000000"}


def visible_text(html: str) -> str:
    return _visible_text(html or "")


class _Walker(HTMLParser):
    """Один проход: заголовки (вне nav/header/footer/aside), таблицы, details/summary."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.heads, self.tables, self.details = [], [], []
        self._skip = 0
        self._h = None          # (tag, buf)
        self._tbl = None        # текущая таблица: [[cells]]
        self._row = None
        self._cell = None
        self._det = None        # {"q": buf|None, "a": buf, "in_summary": bool}

    def handle_starttag(self, tag, attrs):
        if tag in _NAV_TAGS:
            self._skip += 1
        if self._skip:
            return
        if tag in ("h1", "h2", "h3"):
            self._h = (tag, [])
        elif tag == "table":
            self._tbl = []
        elif tag == "tr" and self._tbl is not None:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []
        elif tag == "details":
            self._det = {"q": None, "a": [], "in_summary": False}
        elif tag == "summary" and self._det is not None:
            self._det["q"], self._det["in_summary"] = [], True

    def handle_endtag(self, tag):
        if tag in _NAV_TAGS and self._skip:
            self._skip -= 1
            return
        if self._skip:
            return
        if tag in ("h1", "h2", "h3") and self._h and self._h[0] == tag:
            txt = " ".join("".join(self._h[1]).split())
            if txt:
                self.heads.append([tag, txt])
            self._h = None
        elif tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None and self._tbl is not None:
            if any(self._row):
                self._tbl.append(self._row)
            self._row = None
        elif tag == "table" and self._tbl is not None:
            if self._tbl:
                self.tables.append(self._tbl)
            self._tbl = None
        elif tag == "summary" and self._det is not None:
            self._det["in_summary"] = False
        elif tag == "details" and self._det is not None:
            q = " ".join("".join(self._det["q"] or []).split())
            a = " ".join("".join(self._det["a"]).split())
            if q and a:
                self.details.append({"q": q, "a": a})
            self._det = None

    def handle_data(self, data):
        if self._skip:
            return
        if self._h:
            self._h[1].append(data)
        if self._cell is not None:
            self._cell.append(data)
        if self._det is not None:
            (self._det["q"] if self._det["in_summary"] else self._det["a"]).append(data)


def _walk(html: str) -> _Walker:
    w = _Walker()
    try:
        w.feed(html or "")
    except Exception:  # noqa: BLE001 — кривой HTML: берём, что успели собрать
        pass
    return w


def headings(html: str, cap: int = 40) -> list[list[str]]:
    out, seen = [], set()
    for tag, txt in _walk(html).heads:
        if 1 <= len(txt.split()) <= 16 and txt.lower() not in seen:
            seen.add(txt.lower()); out.append([tag, txt])
        if len(out) >= cap:
            break
    return out


def tables(html: str, cap: int = 5) -> list[list[list[str]]]:
    return [t[:40] for t in _walk(html).tables if len(t) >= 2][:cap]


_Q_RE = re.compile(r"\?\s*$")


def faq(html: str, cap: int = 20) -> list[dict]:
    """<details>/<summary> + заголовок с «?» и первый абзац за ним (упрощённо: следующий блок текста)."""
    w = _walk(html)
    out = list(w.details)
    # h2/h3 с вопросом: ответ — текст между этим заголовком и следующим
    pat = re.compile(r"<(h[23])\b[^>]*>(.*?)</\1>(.*?)(?=<h[1-3]\b|</body|$)", re.I | re.S)
    for _, q_html, a_html in pat.findall(html or ""):
        q = " ".join(_visible_text(q_html).split())
        if not _Q_RE.search(q):
            continue
        a = " ".join(_visible_text(a_html).split())[:600]
        if a:
            out.append({"q": q, "a": a})
    uniq, seen = [], set()
    for x in out:
        if x["q"].lower() not in seen:
            seen.add(x["q"].lower()); uniq.append(x)
    return uniq[:cap]


def numbers(text: str, cap: int = 60) -> list[dict]:
    words = (text or "").split()
    out = []
    for i, w in enumerate(words):
        m = _NUM_RE.search(w)
        if not m:
            continue
        val = m.group(1).replace("\u00a0", " ").replace(" ", "").replace(",", ".")
        ctx = " ".join(words[max(0, i - 8): i + 9])
        out.append({"value": val, "ctx": ctx})
        if len(out) >= cap:
            break
    return out


def stylesheet_links(html: str, base_url: str, cap: int = 3) -> list[str]:
    out = []
    for tag in _LINK_RE.findall(html or ""):
        if "stylesheet" not in tag.lower():
            continue
        m = _HREF_RE.search(tag)
        if m:
            out.append(urljoin(base_url, m.group(1)))
        if len(out) >= cap:
            break
    return out


def _top(counter: dict, n: int) -> list:
    return [k for k, _ in sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[:n]]


def css_tokens(html: str, css_texts: list[str]) -> dict:
    css = "\n".join(css_texts or []) + "\n" + "\n".join(_STYLE_RE.findall(html or ""))
    fonts, colors, widths = {}, {}, []
    for fam in _FONT_RE.findall(css):
        first = fam.split(",")[0].strip().strip("'\"")
        if first and first.lower() not in ("inherit", "initial", "sans-serif", "serif", "monospace", "system-ui"):
            fonts[first] = fonts.get(first, 0) + 1
    for c in _COLOR_RE.findall(css):
        c = c.lower()
        if c not in _WHITE_BLACK:
            colors[c] = colors.get(c, 0) + 1
    widths = [int(x) for x in _MAXW_RE.findall(css) if 600 <= int(x) <= 1600]
    low = (html or "").lower()
    comps = []
    if "<table" in low:
        comps.append("table")
    if "pros" in low and "cons" in low:
        comps.append("pros_cons")
    if "<details" in low or "faq" in low:
        comps.append("faq")
    if "rating" in low or "star" in low or "★" in low:
        comps.append("rating")
    if "sticky" in css.lower() or "position:fixed" in css.lower().replace(" ", ""):
        comps.append("sticky_cta")
    return {"fonts": _top(fonts, 3), "colors": _top(colors, 6),
            "container_px": sorted(widths)[len(widths) // 2] if widths else None, "components": comps}


def extract_all(html: str, css_texts: list[str]) -> dict:
    text = visible_text(html)
    return {"text": text, "words": len(text.split()), "headings": headings(html), "tables": tables(html),
            "faq": faq(html), "numbers": numbers(text), "css_tokens": css_tokens(html, css_texts)}
```

- [ ] **Step 4: Run → PASS** (если живая фикстура даёт < 3 заголовков — это факт о странице: ослабить порог
теста до `>= 1`, не менять код); pyflakes.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/research_extract.py backend/tests/test_research_extract.py
git commit -m "feat(M4): извлечение досье из HTML/CSS конкурента — текст, заголовки, таблицы, FAQ, числа, токены"
```

---

### Task 7: Сборка досье — `services/research.py` + запросы в locales

**Files:**
- Create: `backend/app/services/research.py`
- Modify: `backend/app/services/locales.py` (ключи `q_review`, `q_comparison`, `q_howto`, `q_market` во ВСЕХ языках `TEXTS`)
- Modify: `backend/app/config.py`: `RESEARCH_DIR: str = "/workspace/combine/research"`, `RESEARCH_SCREENSHOTS: bool = False`, `RESEARCH_MAX_AGE_DAYS: int = 30`
- Modify: `backend/app/services/competitor.py`: `outline_for` остаётся (план Б его удалит), `extract_headings` не трогаем
- Test: `backend/tests/test_research.py`

**Interfaces:**
- Consumes: `research_extract.extract_all/stylesheet_links`, `BrowserlessClient`, `AParserClient.serp_urls/fetch_html`,
  `SearxngClient.search`, `jobs.track/report/cancelled`, `locales.t`.
- Produces:
  - `KINDS = ("review", "comparison", "howto", "market")`; `PER_QUERY = 5`; `MIN_WORDS = 300`
  - `queries_for(brand: str, lang: str, country: str | None) -> list[tuple[str, str]]` — `[(kind, query)]`
  - `build_dossier(site_id: int, *, force: bool = False) -> dict` —
    `{"status": "done"|"fresh"|"empty", "rows": int, "reason": str|None, "warnings": [str]}`
  - `dossier(db, site_id: int) -> list[SiteResearch]` (отсортировано kind, rank)
  - `is_fresh(db, site_id: int) -> bool`
  - `summary(db, site_id: int) -> dict` — `{"rows": int, "kinds": {kind: n}, "fresh": bool, "fetched_at": datetime|None, "screenshots": int}`

- [ ] **Step 1: Failing tests**

```python
# backend/tests/test_research.py
"""Досье конкурентов: запросы по языку, SERP с запасным SearXNG, фильтры, порог слов, свежесть, скриншоты,
пустое досье — причина словами (спека §4)."""
from datetime import datetime, timedelta, timezone

import pytest

import app.db as db
from app.config import settings
from app.models.domain import Domain
from app.models.offer import Offer
from app.models.research import SiteResearch
from app.models.site import Site
from app.services import research

LONG = "<html><body><h2>Цена</h2><p>" + "слово " * 350 + "5.99 $ в месяц</p><table><tr><th>a</th></tr><tr><td>b</td></tr></table></body></html>"
SHORT = "<html><body><p>мало слов</p></body></html>"


def _site(lang="ru") -> int:
    with db.SessionLocal() as s:
        o = Offer(brand="Durev VPN", affiliate_link="https://durevpn.com", language=lang, country="RU", active=True)
        d = Domain(domain="t.xyz", source="list", status="purchased", market_lang=lang)
        s.add_all([o, d]); s.commit()
        site = Site(domain_id=d.id, status="content", doc_root="/www/wwwroot/t.xyz", offer_id=o.id)
        s.add(site); s.commit()
        return site.id


class FakeAP:
    def __init__(self, urls, pages):
        self.urls, self.pages, self.calls = urls, pages, []
    def serp_urls(self, query, limit=10):
        self.calls.append(query); return list(self.urls)
    def fetch_html(self, url):
        return self.pages.get(url)


class FakeSX:
    def __init__(self, results): self.results = results
    def search(self, query, language=None, pageno=1): return [{"url": u} for u in self.results]


class FakeBL:
    def __init__(self, boom=False): self.boom, self.shots = boom, []
    def screenshot(self, url, **kw):
        if self.boom: raise RuntimeError("down")
        self.shots.append(url); return b"\x89PNG"


@pytest.fixture
def wire(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "RESEARCH_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "RESEARCH_SCREENSHOTS", False)
    def _w(ap, sx=None, bl=None):
        monkeypatch.setattr(research, "_aparser", lambda: ap)
        monkeypatch.setattr(research, "_searxng", lambda: sx or FakeSX([]))
        monkeypatch.setattr(research, "_browserless", lambda: bl or FakeBL())
    return _w


def test_queries_follow_language_and_country():
    q = dict(research.queries_for("Durev VPN", "ru", "RU"))
    assert q["review"] == "Durev VPN обзор" and q["comparison"] == "Durev VPN против конкурентов"
    assert q["howto"] == "как настроить Durev VPN" and q["market"] == "лучший VPN Россия"
    qe = dict(research.queries_for("Durev VPN", "en", "US"))
    assert qe["review"] == "Durev VPN review" and qe["market"] == "best VPN United States"
    with pytest.raises(ValueError):
        research.queries_for("X", "pl", None)


def test_build_filters_noise_brand_short_pages_and_stores_rows(wire):
    sid = _site()
    urls = ["https://durevpn.com/", "https://reddit.com/r/x", "https://a.com/1", "https://a.com/2",
            "https://b.com/short", "https://c.com/ok", "https://d.com/none"]
    pages = {"https://a.com/1": LONG, "https://a.com/2": LONG, "https://b.com/short": SHORT, "https://c.com/ok": LONG}
    ap = FakeAP(urls, pages)
    wire(ap)
    out = research.build_dossier(sid)
    assert out["status"] == "done" and len(ap.calls) == 4
    with db.SessionLocal() as s:
        rows = research.dossier(s, sid)
        by_kind = {}
        for r in rows:
            by_kind.setdefault(r.kind, []).append(r)
        assert set(by_kind) == set(research.KINDS)
        review = by_kind["review"]
        assert [r.url for r in review] == ["https://a.com/1", "https://c.com/ok"]   # a.com один раз, brand/reddit/short/none — нет
        assert review[0].rank == 1 and review[1].rank == 2 and review[0].words >= 300 and review[0].numbers
        assert research.summary(s, sid)["rows"] == 8 and research.is_fresh(s, sid)


def test_serp_falls_back_to_searxng_when_aparser_empty(wire):
    sid = _site()
    wire(FakeAP([], {"https://x.com/": LONG}), FakeSX(["https://x.com/"]))
    assert research.build_dossier(sid)["status"] == "done"
    with db.SessionLocal() as s:
        assert {r.url for r in research.dossier(s, sid)} == {"https://x.com/"}


def test_all_fetches_fail_is_empty_with_reason_not_exception(wire):
    sid = _site()
    wire(FakeAP(["https://www.google.com/goto?url=abc"], {}))
    out = research.build_dossier(sid)
    assert out["status"] == "empty" and "ни одной" in out["reason"]
    with db.SessionLocal() as s:
        assert research.dossier(s, sid) == [] and research.summary(s, sid)["rows"] == 0


def test_fresh_dossier_is_not_refetched_unless_forced(wire):
    sid = _site()
    ap = FakeAP(["https://a.com/1"], {"https://a.com/1": LONG})
    wire(ap)
    research.build_dossier(sid)
    assert research.build_dossier(sid)["status"] == "fresh" and len(ap.calls) == 4
    with db.SessionLocal() as s:
        for r in s.query(SiteResearch).all():
            r.fetched_at = datetime.now(timezone.utc) - timedelta(days=31)
        s.commit()
    assert research.build_dossier(sid)["status"] == "done" and len(ap.calls) == 8
    assert research.build_dossier(sid, force=True)["status"] == "done" and len(ap.calls) == 12
    with db.SessionLocal() as s:
        assert len(research.dossier(s, sid)) == 4          # пересборка заменяет строки, не копит


def test_screenshots_when_enabled_and_browserless_down_is_warning(wire, monkeypatch, tmp_path):
    sid = _site()
    monkeypatch.setattr(settings, "RESEARCH_SCREENSHOTS", True)
    bl = FakeBL()
    wire(FakeAP(["https://a.com/1"], {"https://a.com/1": LONG}), bl=bl)
    research.build_dossier(sid)
    assert bl.shots == ["https://a.com/1"] * 4
    with db.SessionLocal() as s:
        r = research.dossier(s, sid)[0]
        assert r.screenshot_path.endswith(".png") and (tmp_path / str(sid)).exists()
        assert research.summary(s, sid)["screenshots"] == 4
    wire(FakeAP(["https://a.com/1"], {"https://a.com/1": LONG}), bl=FakeBL(boom=True))
    out = research.build_dossier(sid, force=True)
    assert out["status"] == "done" and any("скриншот" in w for w in out["warnings"])
    with db.SessionLocal() as s:
        r = research.dossier(s, sid)[0]
        assert r.screenshot_path is None and "скриншот" in (r.note or "")
```

- [ ] **Step 2: Run → FAIL**.

- [ ] **Step 3: locales — запросы**

В `backend/app/services/locales.py` в каждый язык `TEXTS` добавить четыре ключа (`{brand}`, `{country}`):

| lang | q_review | q_comparison | q_howto | q_market |
|---|---|---|---|---|
| en | `{brand} review` | `{brand} vs alternatives` | `how to set up {brand}` | `best VPN {country}` |
| ru | `{brand} обзор` | `{brand} против конкурентов` | `как настроить {brand}` | `лучший VPN {country}` |
| de | `{brand} Test` | `{brand} Vergleich` | `{brand} einrichten` | `bester VPN {country}` |
| es | `{brand} opiniones` | `{brand} vs alternativas` | `cómo configurar {brand}` | `mejor VPN {country}` |
| fr | `{brand} avis` | `{brand} comparatif` | `configurer {brand}` | `meilleur VPN {country}` |
| nl | `{brand} review` | `{brand} vergelijking` | `{brand} instellen` | `beste VPN {country}` |
| pt | `{brand} análise` | `{brand} comparação` | `como configurar {brand}` | `melhor VPN {country}` |
| it | `{brand} recensione` | `{brand} confronto` | `come configurare {brand}` | `miglior VPN {country}` |

Названия стран для `{country}` — новый словарь в `locales.py`:
```python
COUNTRY_NAMES = {
    "ru": {"RU": "Россия", "KZ": "Казахстан", "BY": "Беларусь", "UA": "Украина", "DE": "Германия", "US": "США", "GB": "Великобритания"},
    "en": {"US": "United States", "GB": "UK", "DE": "Germany", "RU": "Russia", "NL": "Netherlands", "MX": "Mexico", "ES": "Spain"},
}
def country_name(lang: str, code: str | None) -> str:
    """Название страны на языке запроса; неизвестное — ISO-код как есть, пусто — ''."""
    if not code:
        return ""
    return COUNTRY_NAMES.get(norm_lang(lang), {}).get(code.upper(), code.upper())
```

- [ ] **Step 4: Реализация `research.py`**

```python
# backend/app/services/research.py
"""Досье конкурентов для сайта (спека 2026-10-10 §4): выдача по 4 запросам на языке рынка -> 5 живых
страниц на запрос -> текст/структура/факты/CSS-токены (+ скриншоты по тумблеру) -> site_research.
Логика здесь; транспорт — A-Parser, SearXNG, Browserless. Пустое досье = причина словами, не исключение."""
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from app.config import settings
from app.services.competitor import _NOISE, _norm
from app.services.locales import t, country_name, supported
from app.services import research_extract as rx

log = logging.getLogger(__name__)
KINDS = ("review", "comparison", "howto", "market")
PER_QUERY = 5
MIN_WORDS = 300
_EXTRA_NOISE = ("apps.apple.", "play.google.", "chrome.google.", "github.", "amazon.", "aliexpress.")


def _aparser():
    from app.integrations.aparser import AParserClient
    return AParserClient()


def _searxng():
    from app.integrations.searxng import SearxngClient
    return SearxngClient()


def _browserless():
    from app.integrations.browserless import BrowserlessClient
    return BrowserlessClient()


def queries_for(brand: str, lang: str, country: str | None) -> list[tuple[str, str]]:
    if not supported(lang):
        raise ValueError(f"язык «{lang}» без словаря запросов — добавь в services/locales.py")
    c = country_name(lang, country)          # пустая страна -> «лучший VPN» без хвоста, дефолтов не плодим
    return [("review", t(lang, "q_review", brand=brand)),
            ("comparison", t(lang, "q_comparison", brand=brand)),
            ("howto", t(lang, "q_howto", brand=brand)),
            ("market", t(lang, "q_market", country=c).strip())]


def _serp(query: str, lang: str) -> list[str]:
    urls: list[str] = []
    try:
        urls = _aparser().serp_urls(query, limit=10)
    except Exception as e:  # noqa: BLE001
        log.warning("research: A-Parser SERP %r: %s", query, e)
    if not urls:
        try:
            urls = [r.get("url") for r in _searxng().search(query, language=lang) if r.get("url")]
        except Exception as e:  # noqa: BLE001
            log.warning("research: SearXNG %r: %s", query, e)
    return urls[:10]


def _is_noise(url: str, brand_key: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return True
    if any(n.rstrip(".") in host for n in _NOISE + _EXTRA_NOISE):
        return True
    return bool(brand_key) and brand_key in _norm(host)


def _css_for(ap, html: str, url: str) -> list[str]:
    out = []
    for link in rx.stylesheet_links(html, url):
        try:
            css = ap.fetch_html(link)
        except Exception:  # noqa: BLE001
            css = None
        if css and len(css) <= 300_000:
            out.append(css)
    return out


def _shot(bl, url: str, site_id: int, kind: str, rank: int) -> tuple[str | None, str | None]:
    """(путь, замечание). Browserless вниз — замечание, досье без скриншота."""
    try:
        png = bl.screenshot(url, width=1366, height=768, full_page=True)
        d = Path(settings.RESEARCH_DIR) / str(site_id)
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{kind}-{rank}.png"
        p.write_bytes(png)
        return str(p), None
    except Exception as e:  # noqa: BLE001
        return None, f"скриншот не снят: {type(e).__name__}: {e}"[:200]


def is_fresh(db, site_id: int) -> bool:
    from sqlalchemy import select, func
    from app.models.research import SiteResearch
    newest = db.scalar(select(func.max(SiteResearch.fetched_at)).where(SiteResearch.site_id == site_id))
    if newest is None:
        return False
    if newest.tzinfo is None:
        newest = newest.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - newest < timedelta(days=settings.RESEARCH_MAX_AGE_DAYS)


def dossier(db, site_id: int) -> list:
    from sqlalchemy import select
    from app.models.research import SiteResearch
    rows = db.execute(select(SiteResearch).where(SiteResearch.site_id == site_id)).scalars().all()
    order = {k: i for i, k in enumerate(KINDS)}
    return sorted(rows, key=lambda r: (order.get(r.kind, 9), r.rank))


def summary(db, site_id: int) -> dict:
    rows = dossier(db, site_id)
    kinds: dict = {}
    for r in rows:
        kinds[r.kind] = kinds.get(r.kind, 0) + 1
    return {"rows": len(rows), "kinds": kinds, "fresh": is_fresh(db, site_id) if rows else False,
            "fetched_at": max((r.fetched_at for r in rows), default=None),
            "screenshots": sum(1 for r in rows if r.screenshot_path)}


def build_dossier(site_id: int, *, force: bool = False) -> dict:
    from app.db import SessionLocal
    from app.models.site import Site
    from app.models.domain import Domain
    from app.models.offer import Offer
    from app.models.research import SiteResearch
    from app.services import jobs
    from app.services.content import site_offer
    from app.services.locales import resolve_lang

    with SessionLocal() as db:
        site = db.get(Site, site_id)
        if site is None:
            raise ValueError(f"site {site_id} not found")
        if not force and is_fresh(db, site_id):
            return {"status": "fresh", "rows": summary(db, site_id)["rows"], "reason": None, "warnings": []}
        offer = site_offer(db, site)
        if offer is None:
            raise ValueError(f"сайт #{site_id}: оффер не привязан — досье не по чему собирать")
        dom = db.get(Domain, site.domain_id)
        lang = resolve_lang(None, dom.market_lang if dom else None, offer.language)
        brand, country = offer.brand, offer.country
    queries = queries_for(brand, lang, country)
    brand_key = _norm(brand)
    shots_on = str(settings.RESEARCH_SCREENSHOTS).strip().lower() in ("1", "true", "yes", "on")
    ap = _aparser()
    bl = _browserless() if shots_on else None
    warnings: list[str] = []
    rows: list[dict] = []

    with jobs.track("research", trigger="manual") as run:
        jobs.report(run, done=0, total=len(queries))
        for qi, (kind, query) in enumerate(queries):
            if jobs.cancelled(run):
                raise jobs.Cancelled()
            jobs.report(run, done=qi, total=len(queries), current=query)
            seen_domains: set[str] = set()
            rank = 0
            for url in _serp(query, lang):
                if rank >= PER_QUERY:
                    break
                if _is_noise(url, brand_key):
                    continue
                host = (urlparse(url).hostname or "").lower()
                if host in seen_domains:
                    continue
                try:
                    html = ap.fetch_html(url)
                except Exception as e:  # noqa: BLE001
                    log.warning("research: fetch %s: %s", url, e)
                    html = None
                if not html:
                    continue
                data = rx.extract_all(html, _css_for(ap, html, url))
                if data["words"] < MIN_WORDS:
                    continue
                seen_domains.add(host)
                rank += 1
                shot, note = _shot(bl, url, site_id, kind, rank) if bl else (None, None)
                if note:
                    warnings.append(f"{kind}#{rank}: {note}")
                rows.append({"site_id": site_id, "kind": kind, "query": query, "rank": rank, "url": url,
                             "final_url": url, "domain": host, "screenshot_path": shot, "note": note, **data})
            if rank == 0:
                warnings.append(f"{kind}: по запросу «{query}» ни одной живой страницы")
        jobs.report(run, done=len(queries), total=len(queries), current="")

    if not rows:
        return {"status": "empty", "rows": 0, "reason": "ни одной живой страницы конкурентов ни по одному запросу "
                "(SERP пуст или страницы не скачались) — генерация без досье не идёт", "warnings": warnings}
    with SessionLocal() as db:
        db.query(SiteResearch).filter(SiteResearch.site_id == site_id).delete()
        db.add_all(SiteResearch(**r) for r in rows)
        db.commit()
    return {"status": "done", "rows": len(rows), "reason": None, "warnings": warnings}
```
`config.py`:
```python
    RESEARCH_DIR: str = "/workspace/combine/research"   # общая папка со шлюзом :3033 (скриншоты для дизайнера)
    RESEARCH_SCREENSHOTS: bool = False                  # Browserless: снимать первый экран конкурентов
    RESEARCH_MAX_AGE_DAYS: int = 30
```

- [ ] **Step 5: Run → PASS**; весь сьют; pyflakes. Если `jobs.track("research")` требует регистрации имени
в белом списке джобов (см. `jobs.py` / роут `/run/{job}/progress` и `FUNNEL_STAGES`-подобные словари в
`panel.py`), добавить `research` туда же с подписью «досье».

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/research.py backend/app/services/locales.py backend/app/config.py backend/tests/test_research.py
git commit -m "feat(M4): досье конкурентов — 4 запроса на языке рынка, A-Parser→SearXNG, фильтры, 5 страниц/запрос, свежесть 30 дн., скриншоты по тумблеру"
```

---

### Task 8: Ключи, compose, .env.example

**Files:**
- Modify: `backend/app/services/api_keys.py` (группа `m45`: четыре поля)
- Modify: `docker-compose.yml` (backend и worker: bind общей папки со шлюзом)
- Modify: `.env.example`, `.gitignore` (`/research_data/`)
- Modify: `backend/tests/conftest.py` (`_no_paid_keys`: `RESEARCH_SCREENSHOTS=False`)
- Test: `backend/tests/test_api_keys.py` (дописать один тест)

**Interfaces:** `EDITABLE` содержит `BROWSERLESS_URL`, `BROWSERLESS_TOKEN`, `RESEARCH_SCREENSHOTS`, `RESEARCH_DIR`.

- [ ] **Step 1: Failing test** (в конец `test_api_keys.py`)

```python
def test_research_keys_are_editable_and_screenshots_is_choice():
    from app.services import api_keys
    for k in ("BROWSERLESS_URL", "BROWSERLESS_TOKEN", "RESEARCH_SCREENSHOTS", "RESEARCH_DIR"):
        assert k in api_keys.EDITABLE, k
    assert api_keys.EDITABLE["RESEARCH_SCREENSHOTS"].kind == "choice"
    assert api_keys.EDITABLE["BROWSERLESS_TOKEN"].secret is True
```

- [ ] **Step 2: Run → FAIL**.

- [ ] **Step 3: Поля, compose, env**

В группу `m45` `GROUPS` (после `SEARXNG_URL`):
```python
      Field("BROWSERLESS_URL", "Browserless · адрес", "Контейнер browserless на боксе, например "
            "http://192.168.1.77:3000 — скриншоты конкурентов и проверка макета.", kind="url"),
      Field("BROWSERLESS_TOKEN", "Browserless · токен", "Если контейнер запущен с TOKEN=…; пусто — без токена.",
            secret=True),
      Field("RESEARCH_SCREENSHOTS", "Досье · скриншоты конкурентов", "true — при сборке досье Browserless снимает "
            "первый экран каждой страницы (дизайнер видит их). false (по умолчанию) — только текст и CSS-токены.",
            kind="choice", choices=("false", "true")),
      Field("RESEARCH_DIR", "Досье · папка скриншотов", "Внутри контейнера backend; по умолчанию "
            "/workspace/combine/research — та же папка смонтирована в шлюз :3033 как /workspace/combine."),
```
`docker-compose.yml`, сервисы `backend` и `worker`, в `volumes:` добавить:
```yaml
      - ${RESEARCH_HOST_DIR:-./research_data}:/workspace/combine
```
`.env.example`: `RESEARCH_HOST_DIR=D:/claude-code/workspace/combine` (комментарий: «та же папка, что у шлюза
claude-code как /workspace; на Mac можно не задавать — ./research_data»), `RESEARCH_SCREENSHOTS=false`,
`BROWSERLESS_URL=http://192.168.1.77:3000`. `.gitignore`: `/research_data/`.
`conftest._no_paid_keys`: `monkeypatch.setattr(settings, "RESEARCH_SCREENSHOTS", False)`.

- [ ] **Step 4: Run → PASS**; `docker compose config -q` локально проходит; pyflakes.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/api_keys.py docker-compose.yml .env.example .gitignore backend/tests/conftest.py backend/tests/test_api_keys.py
git commit -m "feat(keys): Browserless и досье на /settings/keys; общая папка скриншотов со шлюзом в compose"
```

---

### Task 9: Карточка сайта — блок «Досье» и кнопка сборки

**Files:**
- Modify: `backend/app/api/panel.py` (`site_view` — контекст; новый роут `POST /sites/{site_id}/research`)
- Modify: `backend/app/templates/site.html` (новый `<li>` «3½ · Досье конкурентов» между провижном и генерацией)
- Test: `backend/tests/test_research_panel.py`

**Interfaces:**
- Consumes: `research.build_dossier`, `research.summary`, `research.dossier`, `jobs.spawn`.
- Produces: `POST /sites/{id}/research` (Form `force: str = ""`), контекст `research` (summary) и `research_rows`.

- [ ] **Step 1: Failing tests**

```python
# backend/tests/test_research_panel.py
"""Карточка сайта: блок «Досье», кнопка сборки уходит в фон, пересборка с force."""
import app.db as db
from app.models.domain import Domain
from app.models.offer import Offer
from app.models.research import SiteResearch
from app.models.site import Site
from app.services import research


def _site() -> int:
    with db.SessionLocal() as s:
        o = Offer(brand="Durev VPN", affiliate_link="https://durevpn.com", language="ru", active=True)
        d = Domain(domain="t.xyz", source="list", status="purchased", market_lang="ru")
        s.add_all([o, d]); s.commit()
        site = Site(domain_id=d.id, status="content", doc_root="/x", offer_id=o.id, provision_step="done")
        s.add(site); s.commit()
        return site.id


def test_card_shows_empty_dossier_and_button(client):
    sid = _site()
    html = client.get(f"/sites/{sid}").text
    assert "Досье конкурентов" in html and f'action="/sites/{sid}/research"' in html and "досье не собрано" in html


def test_card_lists_sources_and_rebuild(client):
    sid = _site()
    with db.SessionLocal() as s:
        s.add(SiteResearch(site_id=sid, kind="review", query="Durev VPN обзор", rank=1, url="https://a.com/1",
                           domain="a.com", words=800, headings=[["h2", "Цена"]], screenshot_path="/r/1/review-1.png"))
        s.commit()
    html = client.get(f"/sites/{sid}").text
    assert "a.com" in html and "800" in html and "пересобрать" in html


def test_button_spawns_job_and_force_passes_through(client, monkeypatch):
    sid = _site()
    calls = []
    monkeypatch.setattr(research, "build_dossier", lambda s, force=False: calls.append((s, force)) or {"status": "done"})
    from app.services import jobs
    monkeypatch.setattr(jobs, "spawn", lambda name, target: (calls.append(name), target())[0] or True)
    r = client.post(f"/sites/{sid}/research", data={"force": "1"}, follow_redirects=False)
    assert r.status_code == 303 and f"/sites/{sid}" in r.headers["location"] and "msg=" in r.headers["location"]
    assert calls == ["research", (sid, True)]      # spawn получил имя research, сервис — force=True
```

- [ ] **Step 2: Run → FAIL**.

- [ ] **Step 3: Роут, контекст, шаблон**

`panel.py`, после `generate_action`:
```python
@router.post("/sites/{site_id}/research")
def research_action(site_id: int, force: str = Form(""), db: Session = Depends(get_session)):
    """Досье конкурентов — фоновая задача `research` (спека 2026-10-10 §4): SERP + 5 страниц на запрос."""
    from app.services import research, jobs
    site = db.get(Site, site_id)
    if site is None:
        return _back("/", err=f"сайт #{site_id} не найден")
    if site.offer_id is None and content_site_offer(db, site) is None:
        return _back(f"/sites/{site_id}", err="досье: оффер не привязан — не по чему искать конкурентов")
    ok = jobs.spawn("research", lambda: research.build_dossier(site_id, force=bool(force)))
    if not ok:
        return _back(f"/sites/{site_id}", err=jobs.busy_msg("Досье уже собирается — дождись на Пульте"))
    return _back(f"/sites/{site_id}", msg="Досье собирается в фоне: 4 запроса × до 5 страниц; прогресс — на Пульте.")
```
(`content_site_offer` = `from app.services.content import site_offer as content_site_offer` внутри функции.)

В `site_view` добавить в контекст: `"research": research.summary(db, site_id), "research_rows": research.dossier(db, site_id)`
(импорт `from app.services import research` внутри функции).

`site.html`, новый `<li>` после шага «3 · Провижн» и до «4 · Генерация»:
```html
    <li>
      <span class="st"><span class="led {{ 'led-ok' if research.rows else ('led-todo' if prov_done else 'led-off') }}"></span></span>
      <div class="body"><div class="t">3½ · Досье конкурентов <small>M4 · SERP</small></div>
        <div class="h">{% if research.rows %}Источников: {{ research.rows }}
          ({% for k, n in research.kinds.items() %}{{ k }}: {{ n }}{{ ', ' if not loop.last }}{% endfor %}),
          скриншотов: {{ research.screenshots }}, собрано {{ research.fetched_at.strftime('%d.%m %H:%M') if research.fetched_at }}{{ '' if research.fresh else ' — устарело, пересобрать' }}.
          {% else %}досье не собрано: выдача по 4 запросам на языке рынка, до 5 живых страниц на запрос —
          текст, заголовки, таблицы, FAQ, цифры, CSS-токены. Без досье генерация не идёт.{% endif %}</div>
        {% if research_rows %}<details class="what"><summary>источники</summary><div class="what-body">
          <table class="data"><thead><tr><th>тип</th><th>#</th><th>домен</th><th>слов</th><th>заголовков</th><th></th></tr></thead><tbody>
          {% for r in research_rows %}<tr><td>{{ r.kind }}</td><td>{{ r.rank }}</td>
            <td><a href="{{ r.url }}" rel="noopener" target="_blank">{{ r.domain }}</a></td><td>{{ r.words }}</td>
            <td>{{ r.headings|length if r.headings else 0 }}</td><td>{{ '📷' if r.screenshot_path }}{{ r.note or '' }}</td></tr>{% endfor %}
          </tbody></table></div></details>{% endif %}
      </div>
      <div class="act">
        <form class="inline" method="post" action="/sites/{{ site.id }}/research">
          {% if research.rows %}<input type="hidden" name="force" value="1">{% endif %}
          <button class="btn-sm {{ '' if research.rows else 'btn-acc' }}" {{ 'disabled' if not prov_done }}
                  title="собрать досье конкурентов по выдаче (A-Parser → SearXNG){{ '' if prov_done else ' — сначала провижн' }}">
            {{ '⟳ пересобрать досье' if research.rows else '▶ Собрать досье' }}</button>
        </form>
      </div>
    </li>
```

- [ ] **Step 4: Run → PASS**; весь сьют; pyflakes. Визуально: отрендерить `/sites/{id}` через TestClient в
HTML и посмотреть скриншотом (Playwright), что `<li>` встал в ленту шагов без переполнения.

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/panel.py backend/app/templates/site.html backend/tests/test_research_panel.py
git commit -m "feat(panel): блок «Досье конкурентов» на карточке сайта — сводка, источники, сборка/пересборка в фоне"
```

---

### Task 10: Стадия `research` в автопилоте

**Files:**
- Modify: `backend/app/services/orchestrator.py` (`_stage_research`, `STAGES`, `STAGE_RU`, `COUNT_RU`)
- Modify: `backend/app/templates/autopilot.html` (строка стадии)
- Modify: `backend/app/api/panel.py` (`autopilot_settings_save`: новые Form-поля)
- Test: `backend/tests/test_orchestrator.py` (дописать)

**Interfaces:**
- Produces: `_stage_research(cap) -> tuple[int, list[str], dict]` — сайты `status=content` без свежего досье;
  `STAGES` содержит `("research", "auto_research", "cap_research", _stage_research)` перед `generate`.
  `auto_design`/`auto_edit`/`cap_design` в форме автопилота сохраняются, стадий `design`/критика в этом плане нет (планы Б/В).

- [ ] **Step 1: Failing tests** (в конец `test_orchestrator.py`; стиль — как соседние тесты файла)

```python
def test_research_stage_sits_before_generate_and_picks_sites_without_fresh_dossier(monkeypatch):
    from app.services import orchestrator, research
    keys = [s[0] for s in orchestrator.STAGES]
    assert keys.index("research") == keys.index("generate") - 1 and keys.index("research") > keys.index("provision")
    assert orchestrator.STAGE_RU["research"] == "досье"
    sid = _content_site()                          # хелпер файла: сайт status=content с оффером
    calls = []
    monkeypatch.setattr(research, "build_dossier", lambda s, force=False: calls.append(s) or {"status": "done", "rows": 4, "warnings": []})
    done, errs, extra = orchestrator._stage_research(cap=5)
    assert done == 1 and errs == [] and calls == [sid]
    monkeypatch.setattr(research, "build_dossier", lambda s, force=False: {"status": "empty", "rows": 0, "reason": "пусто", "warnings": []})
    done, errs, extra = orchestrator._stage_research(cap=5)
    assert done == 0 and extra.get("research_empty") == 1 and "пусто" in errs[0]


def test_autopilot_form_saves_new_toggles(client):
    r = client.post("/autopilot/settings", data={"auto_research": "on", "cap_research": "7", "auto_edit": "on",
                                                 "cap_design": "2"}, follow_redirects=False)
    assert r.status_code == 303
    from app.services.autonomy import get_autonomy
    a = get_autonomy()
    assert a["auto_research"] is True and a["cap_research"] == 7 and a["auto_edit"] is True and a["cap_design"] == 2
```
Если в файле нет хелпера `_content_site()`, добавить его по образцу `_site()` из `test_research_panel.py`.

- [ ] **Step 2: Run → FAIL**.

- [ ] **Step 3: Стадия, таблица, форма, шаблон**

`orchestrator.py` перед `_stage_generate`:
```python
def _stage_research(cap):
    """Сайты status=content без свежего досье -> research.build_dossier (спека 2026-10-10 §4, §8).
    «Пустое» досье — не ошибка стадии, а отдельный счётчик research_empty + причина словами: сайт
    остаётся в content, генерация его не возьмёт (план Б), оператор видит почему на карточке."""
    from sqlalchemy import select
    from app.db import SessionLocal
    from app.models.site import Site
    from app.services import research, jobs

    done, errs, empty = 0, [], 0
    with SessionLocal() as db:
        ids = [sid for (sid,) in db.execute(select(Site.id).where(Site.status == "content").order_by(Site.id)).all()
               if not research.is_fresh(db, sid)][:cap]
    for sid in ids:
        try:
            out = research.build_dossier(sid)
        except jobs.AlreadyRunning:
            raise
        except Exception as e:  # noqa: BLE001
            errs.append(f"site#{sid}: {type(e).__name__}: {e}")
            continue
        if out["status"] == "empty":
            empty += 1
            errs.append(f"site#{sid}: досье пустое — {out['reason']}")
        else:
            done += 1
    extra = {"research_empty": empty} if empty else {}
    return done, errs, extra
```
Если соседние стадии возвращают пару `(done, errs)`, а не тройку — вернуть пару и положить `research_empty`
тем же способом, каким `_stage_provision` отдаёт `provision_awaiting` (см. `extra` там).

`STAGES`: вставить `("research", "auto_research", "cap_research", _stage_research),` перед `generate`.
`STAGE_RU["research"] = "досье"`; `COUNT_RU["research_empty"] = "досье пустое"`.

`autopilot.html`, в `stages` после `provision`:
```
    ('research','Досье конкурентов','собрать выдачу и 5 страниц конкурентов на запрос: текст, структура, факты, CSS','content, без свежего досье', a.cap_research),
```
`panel.py` `autopilot_settings_save`: добавить параметры `auto_research: str = Form("")`, `auto_design: str = Form("")`,
`auto_edit: str = Form("")`, `cap_research: int = Form(5)`, `cap_design: int = Form(3)` и передать их в
`update_autonomy(...)` как `bool(...)`/int. В `autopilot.html` рядом с мастер-тумблером добавить две
строки без стадий (стадии придут планами Б/В):
```html
  <label><input type="checkbox" name="auto_edit" {{ 'checked' if a.auto_edit }}> критик ставит <b>edited</b> сам (план Б — пока без эффекта)</label>
  <label><input type="checkbox" name="auto_design" {{ 'checked' if a.auto_design }}> рисовать макет автоматом (план В — пока без эффекта)</label>
  <input type="hidden" name="cap_design" value="{{ a.cap_design }}">
```

- [ ] **Step 4: Run → PASS**; весь сьют; pyflakes.

- [ ] **Step 5: Commit, push, бокс**

```bash
git add backend/app/services/orchestrator.py backend/app/templates/autopilot.html backend/app/api/panel.py backend/tests/test_orchestrator.py
git commit -m "feat(autopilot): стадия research перед generate; тумблеры auto_research/auto_design/auto_edit"
git push origin main
```
Бокс: `POST /admin/pull` (миграция 0036 накатится сама), затем `docker compose up -d` (новый bind-том).
Живая проверка: на карточке `tunnelnotes.xyz` привязать оффер Durev VPN (создать на `/offers`:
бренд `Durev VPN`, язык `ru`, гео `RU`, ссылка `https://durevpn.com`), нажать «▶ Собрать досье», дождаться
на Пульте, проверить таблицу источников и `job_run.message`; включить `RESEARCH_SCREENSHOTS=true` на
`/settings/keys`, пересобрать, убедиться, что PNG лежат в `D:\claude-code\workspace\combine\research\<site_id>\`.
Результат (сколько источников, какие домены, есть ли `goto`-редиректы без HTML) — в
`docs/v2/research/research-live-formats-2026-10.md`.
