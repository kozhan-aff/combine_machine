# Резервный оффер при публикации — Implementation Plan

> **Для агентов-исполнителей:** ОБЯЗАТЕЛЬНЫЙ СУБ-НАВЫК — используй
> superpowers:subagent-driven-development (рекомендуется) или
> superpowers:executing-plans, чтобы исполнять этот план задача-за-задачей. Шаги
> размечены чекбоксами (`- [ ]`). Модель-исполнитель — **Sonnet 5**: код в шагах
> дан целиком, домысливать нечего; где нужно предварительно прочитать файл — это
> сказано явным шагом.

**Goal:** Продолжение Фазы 3 (F3, аудит 2026-07-15): при публикации страницы, чей
зафиксированный оффер выключен (`Offer.active=False`), подставлять общий резервный URL
вместо мёртвой ссылки — не трогая бренд/промокод в тексте и не переоценивая сам `offer_id`.

**Architecture:** Новая single-row настройка `OfferSettings.reserve_offer_url` (паттерн
`ScoringSettings`/`AutonomySettings`). Подмена ссылки происходит в `content.render_html()`
рядом с уже существующей F28-проверкой безопасности `href`; `publish.py` читает настройку
один раз на вызов `publish_site()` и передаёт вниз. UI — малая форма на `/offers`.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.x, Alembic, Jinja2. Тесты офлайн:
`backend/tests/` (SQLite через `Base.metadata.create_all`, autouse `_no_live_network`).

Спек (полный дизайн, уже одобрен пользователем):
`docs/superpowers/specs/2026-07-16-reserve-offer-fallback-design.md`.

## Global Constraints (проверяет combine-reviewer на КАЖДОЙ задаче)

- **Денежный гейт:** заказ провайдеру ТОЛЬКО при `confirmed_by_human=true`. Эта работа его не
  касается вообще.
- **Редактурный гейт:** публикация ТОЛЬКО из `Page.status == "edited"`; `draft→edited`
  (`mark_edited`) — только человек. Эта работа НЕ меняет условия публикации (какие страницы
  публикуются), только то, какую ссылку несёт уже публикуемая страница.
- **F26 неприкосновенен:** `p.offer_id` остаётся зафиксированным фактом истории (страница про
  бренд A не должна сослаться на бренд B). Резервная подмена меняет ТОЛЬКО `href`, никогда
  `offer.brand`/`offer.promo_code`/сам `offer_id`.
- **F28 неприкосновенен:** `is_safe_url()` — единственный судья, что можно вставить в `href`.
  Резервный URL проверяется ТЕМ ЖЕ хелпером, в ДВУХ точках (сохранение настройки — на входе;
  `render_html` — на выходе, defense in depth), как уже сделано для `affiliate_link`.
- **`integrations/` = только транспорт, логика в `services/`.** В этой работе новых
  integrations-вызовов нет вообще (нет проверки живости ссылки — см. «Не в этом плане»).
- **Секреты только в `.env`** — не касается этой работы.
- **Тесты герметичны:** живой сети нет; тест проверяет реальное поведение (никаких моков
  `render_html`/`is_safe_url` — только моки внешних систем: aaPanel, LLM). `pyflakes` чист.
- **UI на русском; светлая холодная CMS, акцент `#2563c9`;** CSS-классы только из `base.html`
  (`.station`/`.plate`/`.what`/`.go`/`.f`/`.note`/`.badge`/`.b-warn` — уже существуют, новых не
  плодить). Прогрессивный «шильдик»: короткий лейбл → `title`/`<span class="note">` → полный
  абзац в `.what`.
- **Миграции линейны;** голова сейчас `0021_site_last_attempt`. Перед созданием новой миграции
  сверить: `.venv/bin/alembic -c backend/alembic.ini heads`.
- **Коммиты:** heredoc `git commit -F -` (не `-m`), трейлер
  `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.

## Порядок

Строгий: **Task 1 → 2 → 3**. Task 2 зависит от модели `OfferSettings` из Task 1 (импортирует
её в `publish.py`). Task 3 зависит от Task 1 (тот же класс) и логически продолжает Task 2
(UI для настройки, которую Task 2 уже умеет читать и применять).

## File Structure

| Файл | Задача | Ответственность изменения |
|---|---|---|
| `backend/app/models/offer.py` | 1 | Класс `OfferSettings` (single-row) |
| `backend/alembic/versions/0022_offer_settings.py` | 1 | Таблица + сид строки `id=1` |
| `backend/app/services/content.py` | 2 | `render_html`: параметр `reserve_url` + логика подмены; фикс 2 существующих `SimpleNamespace` без `.active` |
| `backend/app/services/publish.py` | 2 | `publish_site`: читает `OfferSettings` один раз, передаёт в `render_html` |
| `backend/tests/test_content_contract.py` | 2, 3 | Юнит- и публикационные тесты подмены; тесты UI-формы |
| `backend/app/api/panel.py` | 3 | `offers_view` отдаёт текущий URL; новый роут `/offers/reserve-url`; `site_view` отдаёт `reserve_configured` |
| `backend/app/templates/offers.html` | 3 | Форма резервного URL (паттерн `.station`) |
| `backend/app/templates/site.html` | 3 | Title бейджа «оффер выключен» упоминает статус резерва |

---

## Setup (один раз, до Task 1)

- [ ] **Ветка от main**

```bash
cd /Users/kozhan/Documents/PROJECTS/combine_machine
git checkout main && git pull --ff-only
git checkout -b feat/reserve-offer-fallback
.venv/bin/python -m pytest backend/tests/ -q   # baseline: 595 passed
```

Ожидается: `595 passed`. Если нет — СТОП, база не та.

---

## Task 1 — `OfferSettings`: single-row настройка резервного URL

**Почему:** Резервный URL — общий на весь портфель, редактируется оператором на лету (не через
`.env`+рестарт). Проект уже решает такие задачи ОДНИМ паттерном: single-row таблица
(`ScoringSettings`, `AutonomySettings`) — id всегда `1`, читается/пишется напрямую, дефолт
`NULL`/пустое значение = «настройка не задана, старое поведение».

**Files:**
- Modify: `backend/app/models/offer.py`
- Create: `backend/alembic/versions/0022_offer_settings.py`
- Test: `backend/tests/test_content_contract.py` (добавить один тест)

**Interfaces:**
- Produces: `OfferSettings` (SQLAlchemy-модель, `__tablename__ = "offer_settings"`,
  `id: int` PK, `reserve_offer_url: str | None`). Task 2 читает `db.get(OfferSettings, 1)`.
  Task 3 читает/пишет то же поле через панель.

- [ ] **Step 1: Написать падающий тест**

Открой `backend/tests/test_content_contract.py` и добавь в конец файла:

```python
def test_offer_settings_singleton_roundtrip():
    """OfferSettings — single-row (id=1) конфиг, тот же паттерн, что ScoringSettings/
    AutonomySettings. Пока таблицы/класса нет — падает импортом."""
    from app.models.offer import OfferSettings
    with db.SessionLocal() as s:
        s.add(OfferSettings(id=1, reserve_offer_url="https://reserve.example/compare"))
        s.commit()
    with db.SessionLocal() as s:
        row = s.get(OfferSettings, 1)
        assert row.reserve_offer_url == "https://reserve.example/compare"


def test_offer_settings_defaults_to_no_row():
    """Дефолт — строки НЕТ вовсе (не создаётся автоматически при create_all). Публикация и UI
    обязаны трактовать отсутствие строки как reserve_offer_url=None, не падать."""
    from app.models.offer import OfferSettings
    with db.SessionLocal() as s:
        assert s.get(OfferSettings, 1) is None
```

- [ ] **Step 2: Прогнать — убедиться, что падает**

Run: `.venv/bin/python -m pytest backend/tests/test_content_contract.py::test_offer_settings_singleton_roundtrip backend/tests/test_content_contract.py::test_offer_settings_defaults_to_no_row -v`
Expected: FAIL (`ImportError: cannot import name 'OfferSettings'`).

- [ ] **Step 3: Добавить класс в модель**

Открой `backend/app/models/offer.py` целиком (36 строк). В конце файла (после класса
`SiteOffer`) добавь:

```python


class OfferSettings(Base):
    """Single-row (id=1) рантайм-конфиг публикации офферов — тот же паттерн, что
    ScoringSettings/AutonomySettings (single-row, редактируется на лету через UI).

    reserve_offer_url: общий URL на весь портфель, на который публикация подставит ссылку
    ВМЕСТО ссылки уже выключенного оффера (F3, аудит 2026-07-15). NULL = резерв не настроен —
    сегодняшнее поведение (мёртвая ссылка остаётся как есть). Меняется ТОЛЬКО href — бренд/
    промокод в тексте страницы не трогаются (offer_id остаётся зафиксированным фактом истории,
    F26)."""
    __tablename__ = "offer_settings"

    id: Mapped[int] = mapped_column(primary_key=True)                  # всегда 1
    reserve_offer_url: Mapped[str | None] = mapped_column(Text)
```

`Text` уже импортирован в шапке файла (`from sqlalchemy import String, Text, Boolean,
ForeignKey, Index`) — новый импорт не нужен.

- [ ] **Step 4: Прогнать — тесты зелёные**

Run: `.venv/bin/python -m pytest backend/tests/test_content_contract.py::test_offer_settings_singleton_roundtrip backend/tests/test_content_contract.py::test_offer_settings_defaults_to_no_row -v`
Expected: PASS. (Схема регистрируется автоматически: `backend/tests/conftest.py` уже импортирует
`app.models.offer` целиком до `create_all` — новый класс в том же модуле подхватится без правок
конфтеста.)

- [ ] **Step 5: Миграция для прода**

Сверить голову: `.venv/bin/alembic -c backend/alembic.ini heads` → ожидается
`0021_site_last_attempt`. Создать `backend/alembic/versions/0022_offer_settings.py`:

```python
"""offer_settings: резервный URL при выключенном оффере (F3, аудит 2026-07-15)."""
from alembic import op
import sqlalchemy as sa

revision = "0022_offer_settings"
down_revision = "0021_site_last_attempt"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "offer_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("reserve_offer_url", sa.Text(), nullable=True),
    )
    op.execute("INSERT INTO offer_settings (id, reserve_offer_url) VALUES (1, NULL)")


def downgrade() -> None:
    op.drop_table("offer_settings")
```

**Проверить точное имя down_revision:** открой `backend/alembic/versions/0021_site_last_attempt.py`,
возьми его `revision = "..."` дословно (в этом проекте имена ревизий — полные строки, как имя
файла без `.py`, не хеши) и подставь. На момент написания плана это
`"0021_site_last_attempt"` — если голова успела сдвинуться (кто-то влил другую миграцию раньше),
используй фактическую.

- [ ] **Step 6: Миграционная цепочка линейна**

Run: `.venv/bin/alembic -c backend/alembic.ini heads`
Expected: один head `0022_offer_settings`.

- [ ] **Step 7: Полный сьют + pyflakes**

Run: `.venv/bin/python -m pytest backend/tests/ -q && .venv/bin/python -m pyflakes backend/app backend/tests`
Expected: `597 passed` (595 + 2 новых теста), pyflakes без вывода.

- [ ] **Step 8: Commit**

```bash
git add backend/app/models/offer.py backend/alembic/versions/0022_offer_settings.py backend/tests/test_content_contract.py
git commit -F - <<'EOF'
feat(F3): OfferSettings — single-row настройка резервного URL

Новая таблица offer_settings (паттерн ScoringSettings/AutonomySettings): id=1,
reserve_offer_url (nullable). Дефолт NULL = резерв не настроен. Основа для
подмены мёртвой ссылки выключенного оффера при публикации (Task 2).

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
```

---

## Task 2 — Подмена ссылки на резерв при публикации

**Почему:** Оффер выключили после генерации страницы — публикация (по решению пользователя,
Фаза 3) не блокируется, но и не обязана публиковать заведомо мёртвую ссылку, если оператор
настроил резерв. Подмена — ТОЛЬКО `href`; бренд/промокод/сам факт «страница про X» остаются
как сгенерировано (F26).

**Files:**
- Modify: `backend/app/services/content.py`
- Modify: `backend/app/services/publish.py`
- Test: `backend/tests/test_content_contract.py`

**Interfaces:**
- Consumes: `OfferSettings` (Task 1).
- Produces: `render_html(page, offer=None, lang="ru", reserve_url=None)` — новый keyword-параметр
  с дефолтом `None` (обратная совместимость: единственный прод-вызов — `publish.py`, self-test
  внизу `content.py` его не передаёт и не должен сломаться).

- [ ] **Step 1: Прочитать текущий `render_html` и self-test**

Открой `backend/app/services/content.py`, найди `def render_html` (около строки 192) и блок
`if __name__ == "__main__":` в конце файла (около строки 216). Держи их перед глазами для
следующих шагов — номера строк ниже ориентировочные, вставляй по содержимому.

- [ ] **Step 2: Написать падающие тесты (юнит на `render_html` + интеграционные на `publish_site`)**

В `backend/tests/test_content_contract.py` добавь (после существующего
`test_render_html_never_emits_javascript_href`):

```python
def test_render_html_swaps_link_to_reserve_when_offer_inactive():
    """F3: offer.active=False + reserve_url задан и безопасен -> href подменяется на резерв,
    бренд/промокод в тексте кнопки НЕ меняются."""
    from types import SimpleNamespace
    from app.services.content import render_html

    page = SimpleNamespace(title="T", body="<p>x</p>")
    offer = SimpleNamespace(brand="DeadVPN", affiliate_link="https://ex.com/dead",
                            promo_code="X1", active=False)
    out = render_html(page, offer, reserve_url="https://reserve.example/compare")
    assert "https://reserve.example/compare" in out
    assert "ex.com/dead" not in out
    assert "DeadVPN" in out and "X1" in out


def test_render_html_keeps_dead_link_without_reserve_configured():
    """Без reserve_url (None, дефолт) поведение НЕ меняется — оффер выключен, ссылка остаётся
    его собственной (уже неактивной). Сегодняшний контракт."""
    from types import SimpleNamespace
    from app.services.content import render_html

    page = SimpleNamespace(title="T", body="<p>x</p>")
    offer = SimpleNamespace(brand="DeadVPN", affiliate_link="https://ex.com/dead",
                            promo_code="X1", active=False)
    out = render_html(page, offer)
    assert "ex.com/dead" in out


def test_render_html_active_offer_ignores_reserve_url():
    """Оффер активен -> reserve_url (даже если задан) не используется вообще."""
    from types import SimpleNamespace
    from app.services.content import render_html

    page = SimpleNamespace(title="T", body="<p>x</p>")
    offer = SimpleNamespace(brand="LiveVPN", affiliate_link="https://ex.com/live",
                            promo_code=None, active=True)
    out = render_html(page, offer, reserve_url="https://reserve.example/compare")
    assert "ex.com/live" in out
    assert "reserve.example" not in out


def test_render_html_never_emits_dangerous_reserve_url():
    """Defense in depth (F28, тот же принцип, что для affiliate_link): даже если резервный URL
    каким-то образом оказался в БД с опасной схемой (форма его отвергает — Task 3, но прямая
    запись в БД её обходит), render_html не должен вставить его в href."""
    from types import SimpleNamespace
    from app.services.content import render_html

    page = SimpleNamespace(title="T", body="<p>x</p>")
    offer = SimpleNamespace(brand="DeadVPN", affiliate_link="https://ex.com/dead",
                            promo_code=None, active=False)
    out = render_html(page, offer, reserve_url="javascript:alert(1)")
    assert "javascript:" not in out
    assert "<a href=" not in out            # ссылки нет вовсе — не «безопасная замена»


def test_publish_uses_reserve_url_for_deactivated_offer(monkeypatch):
    """Сквозной сценарий через publish_site(): оффер выключен ПОСЛЕ генерации, резерв настроен
    ДО публикации -> опубликованный файл несёт резервную ссылку, не мёртвую."""
    from app.services import content, publish
    from app.integrations.aapanel import AaPanelClient
    from app.models.offer import OfferSettings

    site_id = _make_site(domain="reserve-on.ru")
    offer_id = _add_offer("DeadVPN", "ru", link="https://ex.com/dead")
    with db.SessionLocal() as s:
        s.add(SiteOffer(site_id=site_id, offer_id=offer_id))
        s.commit()

    monkeypatch.setattr("app.integrations.llm.LlmClient.complete",
                        lambda self, system, prompt, **kw: "<p>текст</p>")
    assert content.generate_site(site_id, lang="ru") == 3

    with db.SessionLocal() as s:
        for p in s.query(Page).filter_by(site_id=site_id).all():
            p.status = "edited"
        s.query(Offer).filter_by(id=offer_id).first().active = False   # выключаем ПОСЛЕ генерации
        s.add(OfferSettings(id=1, reserve_offer_url="https://reserve.example/compare"))
        s.commit()

    written = {}

    def _post(self, path, data=None):
        if "CreateFile" in path:
            return {"status": True, "msg": "Created successfully!"}
        if "SaveFileBody" in path:
            written[data["path"]] = data["data"]
            return {"status": True, "msg": "Saved successfully!"}
        raise AssertionError(path)  # pragma: no cover

    monkeypatch.setattr(AaPanelClient, "_post", _post)
    out = publish.publish_site(site_id)
    assert out["status"] == "published"

    home = written["/www/wwwroot/reserve-on.ru/index.html"]
    assert "https://reserve.example/compare" in home
    assert "ex.com/dead" not in home
    assert "DeadVPN" in home                       # бренд в тексте кнопки не поменялся


def test_publish_keeps_dead_link_when_no_reserve_row(monkeypatch):
    """Без строки OfferSettings вовсе (обычное состояние до Task 3/первой настройки) publish_site
    не падает и не меняет поведение — мёртвая ссылка остаётся как есть."""
    from app.services import content, publish
    from app.integrations.aapanel import AaPanelClient

    site_id = _make_site(domain="reserve-off.ru")
    offer_id = _add_offer("DeadVPN2", "ru", link="https://ex.com/dead2")
    with db.SessionLocal() as s:
        s.add(SiteOffer(site_id=site_id, offer_id=offer_id))
        s.commit()

    monkeypatch.setattr("app.integrations.llm.LlmClient.complete",
                        lambda self, system, prompt, **kw: "<p>текст</p>")
    content.generate_site(site_id, lang="ru")

    with db.SessionLocal() as s:
        for p in s.query(Page).filter_by(site_id=site_id).all():
            p.status = "edited"
        s.query(Offer).filter_by(id=offer_id).first().active = False
        s.commit()
        # OfferSettings НЕ создаём вовсе — проверяем именно этот случай

    written = {}

    def _post(self, path, data=None):
        if "CreateFile" in path:
            return {"status": True, "msg": "Created successfully!"}
        if "SaveFileBody" in path:
            written[data["path"]] = data["data"]
            return {"status": True, "msg": "Saved successfully!"}
        raise AssertionError(path)  # pragma: no cover

    monkeypatch.setattr(AaPanelClient, "_post", _post)
    out = publish.publish_site(site_id)
    assert out["status"] == "published"
    home = written["/www/wwwroot/reserve-off.ru/index.html"]
    assert "ex.com/dead2" in home    # сегодняшнее поведение сохранено
```

**Важно:** `_make_site`/`_add_offer` — уже существующие хелперы этого файла (см. верх
`test_content_contract.py`), `db`/`Offer`/`SiteOffer`/`Page` уже импортированы в шапке файла —
новых импортов в тестах не добавляй, они там уже есть.

- [ ] **Step 3: Прогнать — падают**

Run: `.venv/bin/python -m pytest backend/tests/test_content_contract.py -v -k "reserve"`
Expected: FAIL — `render_html() got an unexpected keyword argument 'reserve_url'`.

- [ ] **Step 4: КРИТИЧНО — почини ДВА существующих фейка-оффера без `.active` ДО того, как менять `render_html`**

Новый код обращается к `offer.active`. Два места в кодовой базе создают оффер-подобный
`SimpleNamespace` БЕЗ поля `active` — если их не поправить сейчас, они упадут
`AttributeError: 'SimpleNamespace' object has no attribute 'active'` после Step 5, и это будет
выглядеть как новая поломка, хотя на деле — забытый шаг этого плана.

**Место 1** — `backend/tests/test_content_contract.py`, тест
`test_render_html_never_emits_javascript_href` (уже существует в файле). Было:

```python
    offer = SimpleNamespace(brand="Evil", affiliate_link="javascript:alert(1)", promo_code=None)
```

Стало:

```python
    offer = SimpleNamespace(brand="Evil", affiliate_link="javascript:alert(1)", promo_code=None,
                            active=True)
```

**Место 2** — `backend/app/services/content.py`, самотест внизу файла (`if __name__ ==
"__main__":`). Было:

```python
    off = N(brand="NordVPN", affiliate_link="https://ex.com/aff?x=1", promo_code="SAVE10")
    ...
    evil = N(brand="Evil", affiliate_link="javascript:alert(1)", promo_code=None)
```

Стало:

```python
    off = N(brand="NordVPN", affiliate_link="https://ex.com/aff?x=1", promo_code="SAVE10",
            active=True)
    ...
    evil = N(brand="Evil", affiliate_link="javascript:alert(1)", promo_code=None, active=True)
```

(Найди обе строки по содержимому — `N(brand="NordVPN"` и `N(brand="Evil"` — в файле они
встречаются по одному разу каждая.)

- [ ] **Step 5: Реализовать подмену в `render_html`**

`backend/app/services/content.py`, функция `render_html` (найди по `def render_html`). Было:

```python
def render_html(page, offer=None, lang: str = "ru") -> str:
    """Wrap an edited page into a full HTML doc with offer link (sponsored) + disclosure. For M5.

    lang: <html lang=...> for the generation language (publish passes it down). Body is
    re-sanitized here (egress) so any writer that skipped _sanitize can't leak hostile HTML.
    """
    offer_block = ""
    # F28: не рендерим ссылку с опасной схемой (javascript:/data:/...), даже если она как-то
    # обошла проверку на создании оффера — это последний рубеж перед публикацией. Оффер без
    # безопасной ссылки лучше показать без ссылки (только бренд/промокод потеряны — не XSS),
    # чем опубликовать страницу с исполняемым href.
    if offer is not None and is_safe_url(offer.affiliate_link):
        promo = f" Промокод: <b>{html.escape(offer.promo_code)}</b>." if offer.promo_code else ""
        offer_block = (f'<p class="offer"><a href="{html.escape(offer.affiliate_link)}" '
                       f'rel="sponsored nofollow">Перейти к {html.escape(offer.brand)}</a>.{promo}</p>')
    return (
```

Стало:

```python
def render_html(page, offer=None, lang: str = "ru", reserve_url: str | None = None) -> str:
    """Wrap an edited page into a full HTML doc with offer link (sponsored) + disclosure. For M5.

    lang: <html lang=...> for the generation language (publish passes it down). Body is
    re-sanitized here (egress) so any writer that skipped _sanitize can't leak hostile HTML.

    reserve_url: F3 (аудит 2026-07-15) — если offer.active=False, ссылка подменяется на этот
    общий резервный URL (если задан). offer_id зафиксирован при генерации и остаётся фактом
    истории (F26) — меняется ТОЛЬКО href, бренд/промокод в тексте не трогаются.
    """
    offer_block = ""
    if offer is not None:
        link = offer.affiliate_link
        if not offer.active and reserve_url:
            link = reserve_url
        # F28: не рендерим ссылку с опасной схемой (javascript:/data:/...), даже если она как-то
        # обошла проверку на создании оффера/сохранении резерва — это последний рубеж перед
        # публикацией. Без безопасной ссылки лучше показать блок без ссылки (только бренд/промокод
        # потеряны — не XSS), чем опубликовать страницу с исполняемым href.
        if is_safe_url(link):
            promo = f" Промокод: <b>{html.escape(offer.promo_code)}</b>." if offer.promo_code else ""
            offer_block = (f'<p class="offer"><a href="{html.escape(link)}" '
                           f'rel="sponsored nofollow">Перейти к {html.escape(offer.brand)}</a>.{promo}</p>')
    return (
```

(Остальное тело функции — `return (...)` — не трогай, оно не меняется.)

- [ ] **Step 6: Прогнать юнит-тесты `render_html` — зелёные**

Run: `.venv/bin/python -m pytest backend/tests/test_content_contract.py -v -k "render_html"`
Expected: PASS (включая уже существующий `test_render_html_never_emits_javascript_href`, который
Step 4 уже поправил).

- [ ] **Step 7: Прочитать `publish_site` и подключить чтение настройки**

Открой `backend/app/services/publish.py`, найди `def publish_site` и строку
`fallback_offer = _pick_offer(db, site_id)` внутри неё (около строки 97). СРАЗУ ПОСЛЕ этой
строки добавь:

```python
        # F3 (аудит 2026-07-15): выключенный оффер публикуется как есть (offer_id — зафиксированное
        # решение о бренде, F26), но подставляем общий резервный URL вместо мёртвой ссылки, если
        # оператор его настроил. Читаем ОДИН РАЗ на весь publish_site() — резерв общий для всего
        # портфеля, не per-странице.
        from app.models.offer import OfferSettings
        _offer_settings = db.get(OfferSettings, 1)
        reserve_url = _offer_settings.reserve_offer_url if _offer_settings else None
```

Дальше в теле функции найди вызов `render_html(p, offer, lang=lang)` внутри цикла `for p in
pages:` (около строки 121). Было:

```python
            ap.write_file(_target_path(site.doc_root, p.url_path), render_html(p, offer, lang=lang))
```

Стало:

```python
            ap.write_file(_target_path(site.doc_root, p.url_path),
                          render_html(p, offer, lang=lang, reserve_url=reserve_url))
```

- [ ] **Step 8: Прогнать интеграционные тесты publish — зелёные**

Run: `.venv/bin/python -m pytest backend/tests/test_content_contract.py -v -k "reserve"`
Expected: все PASS (юнит + интеграционные).

- [ ] **Step 9: Полный сьют + pyflakes**

Run: `.venv/bin/python -m pytest backend/tests/ -q && .venv/bin/python -m pyflakes backend/app backend/tests`
Expected: `603 passed` (597 после Task 1 + 6 новых тестов Step 2), pyflakes без вывода.
Отдельно убедись, что старые тесты этого файла (`test_publish_uses_offer_and_lang_captured_at_
generation`, `test_publish_legacy_page_without_offer_id_falls_back_to_current_offer`,
`test_render_html_never_emits_javascript_href`) всё ещё зелёные — F26/F28 не потревожены.

- [ ] **Step 10: Commit**

```bash
git add backend/app/services/content.py backend/app/services/publish.py backend/tests/test_content_contract.py
git commit -F - <<'EOF'
feat(F3): публикация подставляет резервный URL для выключенного оффера

render_html получил reserve_url: offer.active=False + резерв настроен -> href
подменяется на резерв, бренд/промокод в тексте не меняются (F26 не тронут).
publish_site читает OfferSettings один раз на вызов. Без настроенного резерва
поведение не меняется (мёртвая ссылка остаётся, как раньше).

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
```

---

## Task 3 — UI: форма резервного URL на `/offers` + уточнение бейджа

**Почему:** Task 2 умеет ПРИМЕНИТЬ настройку, но оператору негде её задать. Плюс уже
существующий бейдж «оффер выключен» (Фаза 3, коммит `5750bc5`) должен сказать, есть ли резерв,
а не просто «оффер выключен» без контекста, что будет со ссылкой дальше.

**Files:**
- Modify: `backend/app/api/panel.py`
- Modify: `backend/app/templates/offers.html`
- Modify: `backend/app/templates/site.html`
- Test: `backend/tests/test_content_contract.py`

**Interfaces:**
- Consumes: `OfferSettings` (Task 1), логика подмены (Task 2, для смыслового соответствия
  текста бейджа реальности).
- Produces: роут `POST /offers/reserve-url`; контекст шаблона `offers.html` получает
  `reserve_offer_url: str` (текущее значение или `""`); контекст `site.html` получает
  `reserve_configured: bool`.

- [ ] **Step 1: Прочитать текущие роуты**

Открой `backend/app/api/panel.py`, найди `def offers_view` (около строки 507) и `def site_view`
(около строки 549) — держи их перед глазами.

- [ ] **Step 2: Падающие тесты**

В `backend/tests/test_content_contract.py` добавь (в конец файла):

```python
def test_offers_view_shows_current_reserve_url(client):
    """GET /offers отражает уже сохранённый reserve_offer_url в значении поля формы."""
    from app.models.offer import OfferSettings
    with db.SessionLocal() as s:
        s.add(OfferSettings(id=1, reserve_offer_url="https://reserve.example/current"))
        s.commit()
    r = client.get("/offers")
    assert r.status_code == 200
    assert "https://reserve.example/current" in r.text


def test_offers_view_renders_when_no_settings_row(client):
    """Строки OfferSettings ещё нет вовсе (обычное состояние до первой настройки) -> страница
    не падает, поле формы просто пустое."""
    r = client.get("/offers")
    assert r.status_code == 200


def test_reserve_url_save_persists_and_reflects_on_reload(client):
    r = client.post("/offers/reserve-url",
                     data={"reserve_offer_url": "https://reserve.example/new"},
                     follow_redirects=False)
    assert r.status_code in (302, 303)
    from app.models.offer import OfferSettings
    with db.SessionLocal() as s:
        row = s.get(OfferSettings, 1)
        assert row is not None and row.reserve_offer_url == "https://reserve.example/new"
    r2 = client.get("/offers")
    assert "https://reserve.example/new" in r2.text


def test_reserve_url_save_rejects_javascript_scheme(client):
    """Defense in depth, третья точка (после affiliate_link на создании оффера и render_html):
    резервный URL проверяется тем же is_safe_url на входе."""
    r = client.post("/offers/reserve-url",
                     data={"reserve_offer_url": "javascript:alert(1)"},
                     follow_redirects=False)
    assert r.status_code in (302, 303)
    from app.models.offer import OfferSettings
    with db.SessionLocal() as s:
        row = s.get(OfferSettings, 1)
        assert row is None or row.reserve_offer_url != "javascript:alert(1)"


def test_reserve_url_save_empty_clears_existing(client):
    """Пустое значение формы -> reserve_offer_url снова NULL (возврат к сегодняшнему поведению)."""
    from app.models.offer import OfferSettings
    with db.SessionLocal() as s:
        s.add(OfferSettings(id=1, reserve_offer_url="https://reserve.example/old"))
        s.commit()
    client.post("/offers/reserve-url", data={"reserve_offer_url": ""}, follow_redirects=False)
    with db.SessionLocal() as s:
        row = s.get(OfferSettings, 1)
        assert row.reserve_offer_url is None


def test_site_badge_title_mentions_reserve_when_configured(client, monkeypatch):
    """Бейдж «оффер выключен» на карточке сайта называет судьбу ссылки: резерв настроен -> одна
    формулировка title, не настроен -> другая."""
    from app.services import content
    from app.models.offer import OfferSettings

    site_id = _make_site(domain="badge-reserve.ru")
    offer_id = _add_offer("DeadVPN3", "ru", link="https://ex.com/dead3")
    with db.SessionLocal() as s:
        s.add(SiteOffer(site_id=site_id, offer_id=offer_id))
        s.commit()
    monkeypatch.setattr("app.integrations.llm.LlmClient.complete",
                        lambda self, system, prompt, **kw: "<p>текст</p>")
    content.generate_site(site_id, lang="ru")
    with db.SessionLocal() as s:
        s.query(Offer).filter_by(id=offer_id).first().active = False
        s.commit()

    r = client.get(f"/sites/{site_id}")
    assert "резервный URL не настроен" in r.text

    with db.SessionLocal() as s:
        s.add(OfferSettings(id=1, reserve_offer_url="https://reserve.example/compare"))
        s.commit()

    r2 = client.get(f"/sites/{site_id}")
    assert "поведёт на резервный URL" in r2.text
```

**Фикстура `client`** — уже определена в `backend/tests/conftest.py` (используется другими
тестами этого файла, напр. `test_offer_create_rejects_javascript_scheme`); просто прими её
параметром, как в остальных тестах файла.

- [ ] **Step 3: Прогнать — падают**

Run: `.venv/bin/python -m pytest backend/tests/test_content_contract.py -v -k "offers_view or reserve_url_save or badge_title"`
Expected: FAIL (роута `/offers/reserve-url` нет — 404; `reserve_offer_url` не в контексте
`offers.html` — `KeyError`/пустой рендер; `reserve_configured` не в контексте `site.html`).

- [ ] **Step 4: Роут `offers_view` отдаёт текущее значение**

`backend/app/api/panel.py`, функция `offers_view` (около строки 507). Было:

```python
@router.get("/offers", response_class=HTMLResponse)
def offers_view(request: Request, db: Session = Depends(get_session)):
    rows = db.execute(select(Offer).order_by(Offer.id)).scalars().all()
    return templates.TemplateResponse(request, "offers.html", {"active": "offers", "rows": rows})
```

Стало:

```python
@router.get("/offers", response_class=HTMLResponse)
def offers_view(request: Request, db: Session = Depends(get_session)):
    rows = db.execute(select(Offer).order_by(Offer.id)).scalars().all()
    from app.models.offer import OfferSettings
    os_row = db.get(OfferSettings, 1)
    return templates.TemplateResponse(request, "offers.html", {
        "active": "offers", "rows": rows,
        "reserve_offer_url": os_row.reserve_offer_url if os_row else "",
    })
```

- [ ] **Step 5: Новый роут сохранения**

В `backend/app/api/panel.py` сразу ПОСЛЕ функции `offer_toggle_action` (найди её по
`@router.post("/offers/{offer_id}/toggle")`, она заканчивается через несколько строк) добавь:

```python


@router.post("/offers/reserve-url")
def offer_reserve_url_save(reserve_offer_url: str = Form(""), db: Session = Depends(get_session)):
    """F3 (аудит 2026-07-15): резервный URL для страниц с выключенным офером. Тот же is_safe_url,
    что и affiliate_link на создании оффера (F28, defense in depth — вторая точка в render_html)."""
    from app.models.offer import OfferSettings
    from app.services.content import is_safe_url
    url = reserve_offer_url.strip()
    if url and not is_safe_url(url):
        return _back("/offers", err="резервный URL: разрешены только http/https")
    row = db.get(OfferSettings, 1)
    if row is None:
        row = OfferSettings(id=1)
        db.add(row)
    row.reserve_offer_url = url or None
    db.commit()
    return _back("/offers", msg="Резервный URL сохранён" if url else "Резервный URL очищен")
```

- [ ] **Step 6: `site_view` отдаёт `reserve_configured`**

`backend/app/api/panel.py`, функция `site_view`. Найди блок (уже существующий из коммита F3):

```python
    offer_ids = {p.offer_id for p in pages if p.offer_id is not None}
    page_offers = {o.id: o for o in db.execute(
        select(Offer).where(Offer.id.in_(offer_ids))).scalars().all()} if offer_ids else {}
    return templates.TemplateResponse(request, "site.html", {
        "active": "dash",
        "site": site, "domain": d.domain if d else f"#{site.domain_id}",
        "pages": pages, "pc": pc, "attached": attached, "all_offers": all_offers,
        "page_offers": page_offers,
    })
```

Стало:

```python
    offer_ids = {p.offer_id for p in pages if p.offer_id is not None}
    page_offers = {o.id: o for o in db.execute(
        select(Offer).where(Offer.id.in_(offer_ids))).scalars().all()} if offer_ids else {}
    from app.models.offer import OfferSettings
    _os_row = db.get(OfferSettings, 1)
    reserve_configured = bool(_os_row and _os_row.reserve_offer_url)
    return templates.TemplateResponse(request, "site.html", {
        "active": "dash",
        "site": site, "domain": d.domain if d else f"#{site.domain_id}",
        "pages": pages, "pc": pc, "attached": attached, "all_offers": all_offers,
        "page_offers": page_offers, "reserve_configured": reserve_configured,
    })
```

- [ ] **Step 7: Форма на `offers.html`**

Открой `backend/app/templates/offers.html` целиком (69 строк). В конце файла, ПОСЛЕ закрывающего
`</div>` станции «＋ Новый оффер» (последняя строка файла), добавь новую станцию:

```html


<div class="station" style="max-width:920px">
  <div class="plate">⛑ Резервный URL — страховка на случай выключенного оффера <span class="mod">публикация</span></div>
  <div class="what">Если оффер, под который писалась страница, потом выключили — публикация НЕ
    блокируется (offer_id зафиксирован как факт истории, не переоценивается), но ссылка на кнопке
    будет мёртвой. Задай сюда общий URL (напр. сводное сравнение VPN) — при СЛЕДУЮЩЕЙ публикации
    такой страницы кнопка поведёт туда вместо мёртвой ссылки. Бренд/промокод в тексте не меняются.
    Пусто = сегодняшнее поведение (мёртвая ссылка остаётся как есть). Уже опубликованные страницы
    эта настройка не чинит — republish делает оператор вручную.</div>
  <div class="go">
    <form method="post" action="/offers/reserve-url" style="display:flex; gap:14px; align-items:end; flex-wrap:wrap">
      <label class="f">резервный URL <span class="note">http/https, пусто — очистить</span>
        <input name="reserve_offer_url" placeholder="https://…" value="{{ reserve_offer_url or '' }}" style="width:340px"></label>
      <button class="btn-sm" title="сохранить (пусто — очистить, вернуться к сегодняшнему поведению)">сохранить</button>
    </form>
  </div>
</div>
```

- [ ] **Step 8: Уточнить title бейджа на `site.html`**

Открой `backend/app/templates/site.html`, найди (уже существующий блок из коммита F3):

```html
          {% set po = page_offers.get(p.offer_id) %}
          {% if po and not po.active %}
          <span class="badge b-warn" title="оффер «{{ po.brand }}», под который писалась эта страница, сейчас выключен — ссылка/промокод на странице ведёт на неактивный оффер. Публикация это не блокирует (решение оператора) — при необходимости перепривяжи сайт к другому офферу и перегенерируй.">оффер выключен</span>
          {% endif %}</td>
```

Замени на:

```html
          {% set po = page_offers.get(p.offer_id) %}
          {% if po and not po.active %}
          <span class="badge b-warn" title="оффер «{{ po.brand }}», под который писалась эта страница, сейчас выключен. {{ 'При следующей публикации ссылка поведёт на резервный URL.' if reserve_configured else 'Ссылка мертва — резервный URL не настроен (задай на экране «Офферы»).' }} Публикация это не блокирует (решение оператора) — при необходимости перепривяжи сайт к другому офферу и перегенерируй.">оффер выключен</span>
          {% endif %}</td>
```

- [ ] **Step 9: Прогнать все новые тесты — зелёные**

Run: `.venv/bin/python -m pytest backend/tests/test_content_contract.py -v -k "offers_view or reserve_url_save or badge_title"`
Expected: все PASS.

- [ ] **Step 10: Полный сьют + pyflakes**

Run: `.venv/bin/python -m pytest backend/tests/ -q && .venv/bin/python -m pyflakes backend/app backend/tests`
Expected: `609 passed` (603 после Task 2 + 6 новых тестов Step 2), pyflakes без вывода.

- [ ] **Step 11: Глазами проверить шаблоны**

Роут CF-настроек в проекте принято проверять рендером через TestClient+SQLite-харнесс в
статический HTML (см. `docs/superpowers/specs/2026-07-10-panel-ui-redesign.md`). Для этой
задачи достаточно точечно:

```bash
.venv/bin/python -c "
from fastapi.testclient import TestClient
from app.main import app
c = TestClient(app)
r = c.get('/offers')
assert r.status_code == 200 and 'Резервный URL' in r.text
print('offers.html ok')
"
```

Expected: `offers.html ok` без исключений.

- [ ] **Step 12: Commit**

```bash
git add backend/app/api/panel.py backend/app/templates/offers.html backend/app/templates/site.html backend/tests/test_content_contract.py
git commit -F - <<'EOF'
feat(F3): UI для резервного URL на /offers + уточнение бейджа

Форма на /offers (паттерн .station/.plate/.what/.go, is_safe_url на входе —
третья точка defense-in-depth после создания оффера и render_html). Бейдж
«оффер выключен» на карточке сайта теперь называет судьбу ссылки: резерв
настроен -> «поведёт на резервный URL», не настроен -> «мертва, задай на
Офферах». CSS-классы только существующие, новых не введено.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
```

---

## Финальное ревью ветки

- [ ] **Whole-branch review** через `combine-reviewer` (opus): собрать пакет
  `git diff main...HEAD`, проверить F26/F28 не нарушены, миграция `0022` линейна, дизайн-контракт
  (`.station`/`.badge`/`.b-warn` — существующие классы), гигиена тестов.
- [ ] **Полный сьют + pyflakes** финально: `.venv/bin/python -m pytest backend/tests/ -q`
  (ожидается `609 passed`) + pyflakes чист.
- [ ] **superpowers:finishing-a-development-branch** — мерж/PR по выбору пользователя.
- [ ] Оператору на боксе: git-pull → миграция `0022` → на `/offers` задать (или оставить пустым)
  резервный URL.
