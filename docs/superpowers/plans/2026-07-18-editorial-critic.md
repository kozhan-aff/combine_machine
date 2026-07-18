# LLM-критик редактуры (Спека 4) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Второй LLM-вызов оценивает черновик страницы (балл + список замечаний) ДО
того, как человек его откроет — advisory-слой, не трогающий гейт редактуры.

**Architecture:** Новый сервис `services/content_critic.py` (логика оценки + defensive-
парсер построчного ответа LLM) поверх уже существующего `integrations/llm.py`
(транспорт, без изменений). Три новые nullable-колонки на `Page` (миграция `0023`).
Панельный роут + блок в `page_edit.html` показывают результат, ничего не блокируют.

**Tech Stack:** Python 3.12, SQLAlchemy 2.x, Alembic, FastAPI/Jinja2 (существующий стек,
без новых зависимостей).

## Global Constraints

- **Хард-гейт 1 (редактура) неприкосновенен.** `critique_page()` не импортирует и не
  пишет `Page.status`. Ни один роут этой фичи не может вызвать `mark_edited`.
- **Формат ответа LLM НЕ проверен вживую** (бокс/LiteLLM недоступны в этой итерации) —
  парсер обязан быть defensive: `score=None`/`issues=[]`/`error=<текст>` на ЛЮБОЙ
  неожиданный ввод, никогда не бросать исключение наружу `critique_page()`, никогда не
  подставлять `0` как «оценено плохо» при непонятном ответе.
- **Критик синхронный**, НЕ через `services/jobs.py` (один LLM-запрos на страницу,
  секунды — не минуты, как Discovery/Score).
- **Русский язык** во всех UI-строках/сообщениях/комментариях.
- Дизайн — `docs/superpowers/specs/2026-07-18-editorial-critic-design.md` (полный
  контекст решений, не пересказывать в задачах).

---

### Task 1: Модель, миграция, сервис оценки

**Files:**
- Modify: `backend/app/models/site.py` (класс `Page`, добавить 3 колонки)
- Create: `backend/alembic/versions/0023_page_critic.py`
- Create: `backend/app/services/content_critic.py`
- Test: `backend/tests/test_content_critic.py`

**Interfaces:**
- Produces: `content_critic.critique_page(page_id: int) -> dict` — возвращает
  `{"score": float | None, "issues": list[str], "error": str | None}`. Пишет
  `page.critic_score` (0.0–1.0 или None), `page.critic_notes` (`{"issues": [...]}`
  или None), `page.critic_checked_at` (`datetime.now(timezone.utc)`, ВСЕГДА
  проставляется при вызове — факт попытки оценки, даже если оценка не удалась).
  Коммитит сама (открывает свою `SessionLocal()`, как `content.mark_edited`).
- Produces: `content_critic._parse_critique(text: str) -> dict` — чистая функция
  (без сети/БД), `{"score": float | None, "issues": list[str]}`. Публичная (с
  подчёркиванием, но тестируется напрямую — см. Step 2).
- Consumes: `app.integrations.llm.LlmClient().complete(system, prompt) -> str`
  (существующий метод, сигнатура не меняется).
- Consumes: `app.models.site.Page` — читает `body`, `lang`, `offer_id`; пишет
  `critic_score`, `critic_notes`, `critic_checked_at`.
- Consumes: `app.models.offer.Offer` — читает `brand` по `page.offer_id` (для
  промпта критика, тот же паттерн, что `content.generate_site` резолвит `brand`).

- [ ] **Step 1: Написать миграцию**

Создать `backend/alembic/versions/0023_page_critic.py`:

```python
"""page critic score/notes/checked_at

Revision ID: 0023_page_critic
Revises: 0022_offer_settings
Create Date: 2026-07-18
"""
from alembic import op
import sqlalchemy as sa

revision = "0023_page_critic"
down_revision = "0022_offer_settings"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("pages", sa.Column("critic_score", sa.Float(), nullable=True))
    op.add_column("pages", sa.Column("critic_notes", sa.JSON(), nullable=True))
    op.add_column("pages", sa.Column(
        "critic_checked_at", sa.DateTime(timezone=True), nullable=True))


def downgrade():
    op.drop_column("pages", "critic_checked_at")
    op.drop_column("pages", "critic_notes")
    op.drop_column("pages", "critic_score")
```

Открой соседний файл `backend/alembic/versions/0022_offer_settings.py` ПЕРЕД этим —
свериться с точной формой докстринга/`down_revision` в этом репо (в проекте revision
id ЭТО ИМЯ ФАЙЛА без `.py`, не дефолтный alembic-хэш — см. `Dev Commands` в
CLAUDE.md), скопировать конвенцию буквально.

- [ ] **Step 2: Добавить колонки в модель `Page`**

В `backend/app/models/site.py`, класс `Page`, сразу после существующих
`index_status`/`index_checked_at` (сохраняя тот же стиль соседних полей):

```python
    # Спека 4 (2026-07-18): advisory-оценка черновика вторым LLM-вызовом, ДО того как
    # человек открыл страницу. НЕ гейт — mark_edited работает независимо от этих полей.
    critic_score: Mapped[float | None] = mapped_column(Float)          # 0.0–1.0
    critic_notes: Mapped[dict | None] = mapped_column(JSON)            # {"issues": [str]}
    critic_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
```

Проверь импорты вверху файла — `Float`/`JSON` из `sqlalchemy`, скорее всего уже
импортированы для других моделей этого файла (например `Domain`); если нет — добавь
в существующую строку импорта `sqlalchemy`, не создавай новую.

- [ ] **Step 3: Написать падающий тест парсера**

`backend/tests/test_content_critic.py`:

```python
"""Критик редактуры (Спека 4, 2026-07-18) — advisory-оценка черновика. НЕ гейт:
mark_edited работает независимо от critic_score/critic_notes. Формат ответа LLM НЕ
проверен вживую (бокс недоступен в этой итерации) — парсер обязан быть defensive."""
from app.services.content_critic import _parse_critique


def test_parse_critique_reads_score_and_issues():
    text = "БАЛЛ: 72\n- нет конкретных цифр по тарифам\n- слабое disclosure"
    out = _parse_critique(text)
    assert out["score"] == 0.72
    assert out["issues"] == ["нет конкретных цифр по тарифам", "слабое disclosure"]


def test_parse_critique_handles_missing_score_gracefully():
    text = "тут какой-то мусор без баллов"
    out = _parse_critique(text)
    assert out["score"] is None
    assert out["issues"] == []


def test_parse_critique_handles_empty_text():
    out = _parse_critique("")
    assert out["score"] is None
    assert out["issues"] == []


def test_parse_critique_clamps_out_of_range_score():
    # LLM может ошибиться и написать 150 вместо 0-100 — не позволяем score вылезти за [0, 1]
    out = _parse_critique("БАЛЛ: 150\n- всё отлично")
    assert out["score"] == 1.0
```

- [ ] **Step 4: Запустить тест, убедиться что падает**

Run: `.venv/bin/python -m pytest backend/tests/test_content_critic.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.content_critic'`

- [ ] **Step 5: Написать `_parse_critique`**

В `backend/app/services/content_critic.py`:

```python
"""LLM-критик редактуры (Спека 4, 2026-07-18): второй, более дешёвый LLM-вызов
оценивает черновик страницы ДО того, как человек его откроет — advisory-слой, НЕ
гейт. mark_edited (content.py) работает независимо от полей этого модуля.

Формат ответа LLM (простой построчный, НЕ строгий JSON — см. design doc) НЕ проверен
вживую: LiteLLM (192.168.1.77:4000) недоступен в этой итерации (тот же бокс, что и
A-Parser/панель). Парсер `_parse_critique` НАМЕРЕННО defensive — любой неожиданный
ввод даёт score=None/issues=[], никогда не бросает исключение и никогда не подставляет
0 как «оценено плохо». Первый живой прогон ОБЯЗАН сверить реальный формат и поправить
промпт/парсер при расхождении — см. docs/superpowers/specs/2026-07-18-editorial-critic-design.md.
"""
import re

_SCORE_RE = re.compile(r"БАЛЛ:\s*(\d+)", re.I)


def _parse_critique(text: str) -> dict:
    """Построчный ответ критика -> {"score": float|None в [0,1], "issues": [str]}.
    Никогда не бросает исключение — на любой неразбираемый текст даёт score=None."""
    score = None
    m = _SCORE_RE.search(text or "")
    if m:
        raw = int(m.group(1))
        score = max(0, min(100, raw)) / 100.0
    issues = [line[2:].strip() for line in (text or "").splitlines()
              if line.strip().startswith("- ") and line[2:].strip()]
    return {"score": score, "issues": issues}
```

- [ ] **Step 6: Запустить тест, убедиться что проходит**

Run: `.venv/bin/python -m pytest backend/tests/test_content_critic.py -v`
Expected: 4 passed

- [ ] **Step 7: Написать падающий тест `critique_page` (LLM замокан)**

Добавить в тот же файл — нужна БД (SQLite-харнесс, как в остальных тестах сервисов,
посмотри `backend/tests/test_content.py` или аналог за паттерном фикстур `db`/
`SessionLocal`, создания `Domain`/`Site`/`Page`/`Offer` для теста):

```python
import app.db as db
from app.models.offer import Offer, SiteOffer
from app.models.site import Site, Page
from app.models.domain import Domain


def _seed_page(body="текст черновика", lang="ru", with_offer=True) -> int:
    with db.SessionLocal() as s:
        d = Domain(domain="crit.ru", source="backorder", status="purchased")
        s.add(d); s.commit(); s.refresh(d)
        site = Site(domain_id=d.id, status="content")
        s.add(site); s.commit(); s.refresh(site)
        offer_id = None
        if with_offer:
            o = Offer(brand="NordVPN", affiliate_link="https://ref/nord")
            s.add(o); s.commit(); s.refresh(o)
            offer_id = o.id
        p = Page(site_id=site.id, url_path="/", title="t", status="draft",
                body=body, lang=lang, offer_id=offer_id)
        s.add(p); s.commit(); s.refresh(p)
        return p.id


def test_critique_page_writes_score_and_notes(monkeypatch):
    from app.services import content_critic
    monkeypatch.setattr(
        "app.integrations.llm.LlmClient.complete",
        lambda self, system, prompt, **kw: "БАЛЛ: 60\n- маловато конкретики")
    pid = _seed_page()
    out = content_critic.critique_page(pid)
    assert out["score"] == 0.6
    assert out["issues"] == ["маловато конкретики"]
    assert out["error"] is None
    with db.SessionLocal() as s:
        p = s.get(Page, pid)
        assert p.critic_score == 0.6
        assert p.critic_notes == {"issues": ["маловато конкретики"]}
        assert p.critic_checked_at is not None
        assert p.status == "draft"          # ГЕЙТ НЕ ТРОНУТ


def test_critique_page_handles_empty_llm_response(monkeypatch):
    """LlmClient.complete уже возвращает "" на фильтр/blocked (см. integrations/llm.py)
    — критик обязан честно сказать "не смог оценить", а не подставить 0 как результат."""
    from app.services import content_critic
    monkeypatch.setattr(
        "app.integrations.llm.LlmClient.complete",
        lambda self, system, prompt, **kw: "")
    pid = _seed_page()
    out = content_critic.critique_page(pid)
    assert out["score"] is None
    assert out["error"] is not None
    with db.SessionLocal() as s:
        p = s.get(Page, pid)
        assert p.critic_score is None
        assert p.critic_checked_at is not None   # факт ПОПЫТКИ зафиксирован
        assert p.status == "draft"


def test_critique_page_raises_on_missing_page():
    from app.services import content_critic
    import pytest
    with pytest.raises(ValueError):
        content_critic.critique_page(999999)
```

Прочитай сам существующий тест другого сервиса (например `test_content.py` или
`test_content_contract.py`) за ТОЧНЫМ паттерном создания `Domain`/`Site`/`Offer` в
этом репо — их конструкторы/обязательные поля могли отличаться от фрагмента выше,
адаптируй под реальные сигнатуры моделей.

- [ ] **Step 8: Запустить, убедиться что падает**

Run: `.venv/bin/python -m pytest backend/tests/test_content_critic.py -v`
Expected: FAIL — `AttributeError`/`ImportError` на `critique_page`

- [ ] **Step 9: Написать `critique_page`**

Добавить в `backend/app/services/content_critic.py`:

```python
_SYSTEM_PROMPT = (
    "Ты — редактор VPN-сайта. Оцени черновик страницы по пяти критериям: "
    "(1) тема соответствует бренду/офферу, (2) есть конкретные факты/цифры "
    "вертикали, а не только общие фразы, (3) язык текста соответствует "
    "заявленному, (4) есть пометка о партнёрской ссылке (disclosure), "
    "(5) текст не выглядит как общая AI-вода без содержания. "
    "Ответь СТРОГО в формате: первая строка 'БАЛЛ: <число от 0 до 100>', "
    "затем каждое замечание отдельной строкой, начинающейся с '- '. "
    "Никакого другого текста."
)


def _critique_prompt(body: str, lang: str, brand: str | None) -> str:
    return (
        f"Бренд/оффер: {brand or 'не указан'}\n"
        f"Ожидаемый язык: {lang}\n"
        f"Текст черновика:\n{body}"
    )


def critique_page(page_id: int) -> dict:
    """Оценить черновик страницы вторым LLM-вызовом (advisory, НЕ гейт — status не
    трогается). Пишет critic_score/critic_notes/critic_checked_at, коммитит сама.
    Возвращает {"score": float|None, "issues": [str], "error": str|None}."""
    from datetime import datetime, timezone
    from app.db import SessionLocal
    from app.models.site import Page
    from app.models.offer import Offer
    from app.integrations.llm import LlmClient

    with SessionLocal() as db:
        page = db.get(Page, page_id)
        if page is None:
            raise ValueError(f"page {page_id} not found")
        brand = None
        if page.offer_id:
            offer = db.get(Offer, page.offer_id)
            brand = offer.brand if offer else None

        error = None
        try:
            text = LlmClient().complete(
                _SYSTEM_PROMPT, _critique_prompt(page.body or "", page.lang or "ru", brand))
        except Exception as e:  # noqa: BLE001 — критик advisory, сбой не должен падать наружу
            text = ""
            error = f"{type(e).__name__}: {e}"

        parsed = _parse_critique(text)
        if not text.strip() and error is None:
            error = "пустой ответ LLM (фильтр/blocked) — оценка недоступна"

        page.critic_score = parsed["score"]
        page.critic_notes = {"issues": parsed["issues"]} if parsed["issues"] else None
        page.critic_checked_at = datetime.now(timezone.utc)
        db.commit()
        return {"score": parsed["score"], "issues": parsed["issues"], "error": error}
```

- [ ] **Step 10: Запустить, убедиться что проходит**

Run: `.venv/bin/python -m pytest backend/tests/test_content_critic.py -v`
Expected: 7 passed

- [ ] **Step 11: Прогнать миграцию + полный сьют**

Run:
```
docker compose run --rm backend alembic upgrade head
.venv/bin/python -m pytest backend/tests/ -q
.venv/bin/python -m pyflakes backend/app backend/tests
```

Если `docker compose` недоступен в этой среде (как весь этот прогон — без бокса),
пропусти реальный upgrade и вместо этого прогони `.venv/bin/python -m pytest
backend/tests/ -q` — SQLite-харнесс тестов создаёт схему из моделей напрямую
(проверь как это устроено в `backend/tests/conftest.py`), реальный alembic upgrade
не обязателен для зелёного тестового прогона; но ОБЯЗАТЕЛЬНО проверь синтаксис файла
миграции (`python -c "import ast; ast.parse(open('backend/alembic/versions/0023_page_critic.py').read())"`
или просто импортом модуля) — синтаксическая ошибка в миграции не всплывёт в
SQLite-харнессе тестов, если он не грузит alembic-цепочку напрямую.

Expected: весь сьют зелёный (baseline на момент этой задачи + 7 новых тестов),
pyflakes чист.

- [ ] **Step 12: Commit**

```bash
git add backend/app/models/site.py backend/alembic/versions/0023_page_critic.py \
       backend/app/services/content_critic.py backend/tests/test_content_critic.py
git commit -F - <<'EOF'
feat(content_critic): LLM-критик редактуры — модель + сервис оценки (Спека 4, задача 1)

Advisory-оценка черновика вторым LLM-вызовом ДО открытия человеком: балл + список
замечаний. НЕ гейт — mark_edited работает независимо, критик не трогает page.status.
Формат ответа LLM не проверен вживую (бокс недоступен) — парсер defensive по design doc.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
```

---

### Task 2: Панельный роут + UI

**Files:**
- Modify: `backend/app/api/panel.py` (новый роут)
- Modify: `backend/app/templates/page_edit.html` (блок критика)
- Test: `backend/tests/test_content_critic.py` (добавить HTTP-уровневые тесты) ИЛИ
  новый `backend/tests/test_panel_critic.py` — на усмотрение имплементера, смотри
  как организованы соседние панельные тесты (например `test_panel_toctou.py` —
  маленький отдельный файл на конкретную фичу, это предпочтительный паттерн в этом
  репо для нового панельного роута).

**Interfaces:**
- Consumes: `content_critic.critique_page(page_id: int) -> dict` из Task 1 (сигнатура
  зафиксирована, не меняется).
- Consumes: `_back(path, msg=None, err=None)` — существующий хелпер панели
  (`panel.py`, используется во ВСЕХ POST-роутах для редиректа с флеш-сообщением;
  прочитай его сигнатуру в файле, не угадывай).
- Produces: роут `POST /pages/{page_id}/critique`, редиректит на `GET /pages/{page_id}`.

- [ ] **Step 1: Написать падающий HTTP-тест**

```python
"""HTTP-уровневый тест роута критика (Спека 4, задача 2)."""
import app.db as db
from app.config import settings
from app.models.domain import Domain
from app.models.offer import Offer
from app.models.site import Site, Page


def _seed_page() -> int:
    with db.SessionLocal() as s:
        d = Domain(domain="critroute.ru", source="backorder", status="purchased")
        s.add(d); s.commit(); s.refresh(d)
        site = Site(domain_id=d.id, status="content")
        s.add(site); s.commit(); s.refresh(site)
        p = Page(site_id=site.id, url_path="/", title="t", status="draft",
                body="черновик", lang="ru")
        s.add(p); s.commit(); s.refresh(p)
        return p.id


def test_critique_route_writes_score_and_redirects(client, monkeypatch):
    monkeypatch.setattr(
        "app.integrations.llm.LlmClient.complete",
        lambda self, system, prompt, **kw: "БАЛЛ: 80\n- норм")
    pid = _seed_page()
    r = client.post(f"/pages/{pid}/critique")
    assert r.status_code == 200          # redirect + follow (см. паттерн test_panel_toctou.py)
    with db.SessionLocal() as s:
        p = s.get(Page, pid)
        assert p.critic_score == 0.8
        assert p.status == "draft"       # ГЕЙТ НЕ ТРОНУТ


def test_critique_route_survives_llm_error(client, monkeypatch):
    def boom(self, system, prompt, **kw):
        raise RuntimeError("LLM недоступен")
    monkeypatch.setattr("app.integrations.llm.LlmClient.complete", boom)
    pid = _seed_page()
    r = client.post(f"/pages/{pid}/critique")
    assert r.status_code == 200          # не 500 — критик advisory, сбой не должен ронять UI


def test_page_edit_view_shows_critic_score_when_present(client):
    pid = _seed_page()
    with db.SessionLocal() as s:
        p = s.get(Page, pid)
        p.critic_score = 0.45
        p.critic_notes = {"issues": ["слабое disclosure"]}
        s.commit()
    r = client.get(f"/pages/{pid}")
    assert r.status_code == 200
    assert "45" in r.text
    assert "слабое disclosure" in r.text
```

Прочитай `backend/tests/conftest.py` за фикстурой `client` (TestClient) — уже
используется во всех панельных тестах (`test_panel_toctou.py`, `test_cf_panel.py`)
— и `_no_panel_auth`/аналог, отключающий Basic-auth по умолчанию (нужно ли явно
что-то настраивать для этого роута — проверь, `_require_cf_write` НЕ нужен, это не
CF-мутация).

- [ ] **Step 2: Запустить, убедиться что падает**

Run: `.venv/bin/python -m pytest backend/tests/test_panel_critic.py -v` (или где ты
разместил файл)
Expected: FAIL — 404 (роута ещё нет) / `AssertionError` на отсутствие критик-блока в HTML

- [ ] **Step 3: Добавить роут в `panel.py`**

Рядом с `@router.post("/pages/{page_id}/save")` (найди его в файле — прочитай
соседний код за стилем, `page_id` там уже используется как имя параметра пути):

```python
@router.post("/pages/{page_id}/critique")
def critique_page_action(page_id: int):
    """Advisory-оценка черновика вторым LLM-вызовом (Спека 4). НЕ гейт: результат
    только показывается на экране редактуры, mark_edited им не связан."""
    from app.services import content_critic
    try:
        content_critic.critique_page(page_id)
    except ValueError as e:
        return _back(f"/pages/{page_id}", err=str(e))
    except Exception as e:  # noqa: BLE001 — критик advisory, сбой не должен ронять UI
        return _back(f"/pages/{page_id}", err=f"критик: {e}")
    return _back(f"/pages/{page_id}", msg="Черновик оценён")
```

Прочитай сам `_back()` в файле — если сигнатура НЕ принимает `msg=`/`err=` именно
такими именами, адаптируй под реальную (в файле уже десятки вызовов `_back(...)` —
скопируй паттерн с самого близкого соседнего роута, например `save_page_action`,
дословно).

- [ ] **Step 4: Добавить блок в `page_edit.html`**

Внутри `{% block content %}`, ПЕРЕД `<form method="post" action="/pages/{{ page.id }}/save">`:

```html
<form method="post" action="/pages/{{ page.id }}/critique" style="margin-bottom:14px">
  <button class="btn" title="второй LLM-вызов — подсказка человеку, не решение; сохранить как edited можно независимо от балла">🔍 Оценить черновик</button>
  {% if page.critic_checked_at %}
    {% if page.critic_score is not none %}
      <span class="badge" title="балл критика: чем выше, тем меньше правок скорее всего нужно — это ПОДСКАЗКА, не гейт">
        критик: {{ (page.critic_score * 100)|round|int }}/100
      </span>
      {% if page.critic_notes and page.critic_notes.issues %}
        <ul class="hint" style="margin:6px 0 0">
          {% for issue in page.critic_notes.issues %}
            <li>{{ issue }}</li>
          {% endfor %}
        </ul>
      {% endif %}
    {% else %}
      <span class="hint">критик не смог оценить черновик (сбой LLM) — открой и решай сам</span>
    {% endif %}
  {% endif %}
</form>
```

Прочитай сам `templates/base.html` за списком доступных CSS-классов (`.badge`/`.btn`/
`.hint` — держать CSS-контракт, НЕ изобретать новые классы в контент-шаблоне, см.
конвенцию «Панель: дизайн» в CLAUDE.md). Если `.badge` требует модификатор (`b-draft`
и т.п., как у `page.status` чуть выше в этом же файле) — не добавляй его здесь: у
критика нет статусной семантики draft/edited/published, это отдельная сущность,
голый `.badge` без модификатора допустим, если он не задаёт неверный цвет по
умолчанию — проверь глазами (см. Step 5).

- [ ] **Step 5: Запустить тесты**

Run: `.venv/bin/python -m pytest backend/tests/test_panel_critic.py -v`
Expected: 3 passed

- [ ] **Step 6: Прогнать полный сьют + pyflakes**

Run:
```
.venv/bin/python -m pytest backend/tests/ -q
.venv/bin/python -m pyflakes backend/app backend/tests
```
Expected: весь сьют зелёный, pyflakes чист.

- [ ] **Step 7: Глазами проверить вёрстку**

Панель недоступна на боксе в этой итерации — отрендерить `/pages/{id}` через
TestClient в статический HTML и открыть локально (паттерн уже описан в CLAUDE.md,
секция «Панель: дизайн»: «рендерить роуты через TestClient+SQLite-харнесс в
статический HTML, поднимать `python -m http.server`»). Убедиться, что блок критика
не ломает существующую вёрстку формы сохранения, шильдик (`title=`) читается.

- [ ] **Step 8: Commit**

```bash
git add backend/app/api/panel.py backend/app/templates/page_edit.html backend/tests/
git commit -F - <<'EOF'
feat(panel): роут + UI для LLM-критика редактуры (Спека 4, задача 2)

POST /pages/{id}/critique — синхронная advisory-оценка, редирект с msg/err. Блок на
page_edit.html показывает балл+замечания, не блокирует форму сохранения/mark_edited.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
```

---

## После обеих задач

Финальное whole-branch ревью через `combine-reviewer` (opus) — проверить ОБЯЗАТЕЛЬНО:
редакторский гейт нигде не тронут (`mark_edited` не вызывается ни из
`content_critic.py`, ни из нового роута), денежный гейт вне скоупа (подтвердить
факт отсутствия каких-либо `confirm_order`/`execute_confirmed_order`/`mark_caught`),
дизайн-контракт панели (CSS-классы только из `base.html`, шильдик, русский UI) держит.
