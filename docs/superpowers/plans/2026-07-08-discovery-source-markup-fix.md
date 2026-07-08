# Discovery-источники cctld/reg_ru/sweb — починка разметки Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Почистить discovery-парсинг трёх выключенных источников (`cctld`, `reg_ru`,
`sweb`) — каждый сейчас даёт либо мусор, либо ноль строк — так, чтобы после починки
пользователь мог включить их вручную через `/settings` и получить реальные кандидаты.

**Architecture:** `cctld.py` переходит с regex-по-HTML-лендингу на скачивание и
распаковку ежедневных ZIP-архивов (реальный реестр). `regru_drops.py`/`sweb_drops.py`
переходят с общего "любой домен-подобный токен на странице" regex + A-Parser-транспорта
на точечный якорь-regex (конкретный CSS-класс/пара label-value) + прямой httpx
(`BaseClient`, как cctld/backorder) — оба сайта живьём отдают 200 без анти-бота, A-Parser
не нужен для 1 запроса за прогон.

**Tech Stack:** Python 3.12, `httpx` (уже зависимость, через `app.integrations.base.BaseClient`),
стдлиб `re`/`zipfile`/`io`. Никаких новых зависимостей.

## Global Constraints

- `integrations/` = транспорт + парсинг в одном файле (существующий паттерн этих
  клиентов — не менять).
- Никаких новых зависимостей (без BeautifulSoup/lxml) — только стдлиб `re`/`zipfile`/`io`
  и уже используемый `httpx` через `BaseClient`.
- `SOURCES_ENABLED` в `backend/app/services/scoring_config.py` НЕ менять — все три
  источника остаются выключены (`cctld: False, reg_ru: False, sweb: False`);
  пользователь включит их вручную через `/settings` после живого прогона на боксе.
- Пагинацию НЕ добавлять — только страница 1 для `reg_ru`/`sweb`. Если пагинация
  понадобится позже, транспорт для этих двух источников нужно будет пересмотреть
  обратно на A-Parser (ротация прокси) — вне рамок этого плана.
- Тесты офлайн, без сети (`backend/tests/conftest.py` — автousе-фикстура режет живые
  источники по умолчанию). Мокать транспорт через `monkeypatch.setattr(client, "request", ...)`
  — установленный в проекте паттерн (см. `backend/tests/test_sources.py::test_list_dropping_string_links_below_min_warns`).
  Оффлайн-фикстура НЕ требует изменений для этого плана.
- После каждой задачи: `.venv/bin/python -m pytest backend/tests/ -q` (сейчас 184 passed,
  0 failed) и `.venv/bin/python -m pyflakes backend/app backend/tests` (чисто, без вывода).
- Контракт возврата `list_dropping() -> list[dict]` не меняется: каждый dict — ровно
  `{"domain": str, "source": str, "referring_domains": None}`. `discovery.py` не трогаем —
  `_collect()` уже дёргает `Client().list_dropping()`/`ping()` универсально.
- Порядок задач важен (см. ниже) — `regru_drops.py`/`sweb_drops.py` сейчас импортируют
  `from app.integrations.cctld import _parse_domains`; эта функция удаляется только в
  последней задаче (cctld), когда на неё уже никто не ссылается. Если делать в обратном
  порядке, тест-сьют сломается между задачами.

---

### Task 1: `regru_drops.py` — точечный якорь + прямой httpx

**Files:**
- Modify: `backend/app/integrations/regru_drops.py` (полностью переписать)
- Test: `backend/tests/test_sources.py` (добавить тесты в конец файла)

**Interfaces:**
- Consumes: `app.integrations.base.BaseClient.__init__(self, base_url: str = "")` и
  `BaseClient.request(self, method: str, url: str, **kwargs) -> httpx.Response`
  (`.text` — строка тела ответа; уже используется `cctld.py`/`backorder.py` так же).
- Produces: `RegruDropsClient` — публичные методы `list_dropping(self) -> list[dict]`
  (каждый dict: `{"domain": str, "source": "reg_ru", "referring_domains": None}`) и
  `ping(self) -> bool`. Никто из более ранних задач это не потребляет (задач до этой нет).
  `discovery.py::_sources()` уже маппит `"reg_ru": RegruDropsClient` и зовёт эти два
  метода универсально — менять `discovery.py` не нужно.

- [ ] **Step 1: Написать падающий тест на извлечение доменов из реальной разметки таблицы**

Добавить в конец `backend/tests/test_sources.py`:

```python
def test_regru_drops_extracts_only_domain_cells(monkeypatch):
    """Живая проверка (2026-07-08): b-table__cell_node_first — единственный класс на
    ячейке с доменом (у дат тот же базовый класс, но без node_first; заголовок 'Домен' —
    <th>, не <td>). Общий regex-по-странице ловил бы reg.ru/yandex.ru из футера —
    якорь на класс не должен."""
    from app.integrations.regru_drops import RegruDropsClient
    c = RegruDropsClient()
    html = (
        '<footer><a href="https://reg.ru">reg.ru</a> '
        '<a href="https://yandex.ru">yandex.ru</a></footer>'
        '<table><tr>'
        '<td class="b-table__cell b-table__cell_type_content b-table__cell_node_first">'
        'first-drop.ru</td>'
        '<td class="b-table__cell b-table__cell_type_content">03.06.2022</td>'
        '</tr><tr>'
        '<td class="b-table__cell b-table__cell_type_content b-table__cell_node_first">'
        'second-drop.ru</td>'
        '<td class="b-table__cell b-table__cell_type_content">07.07.2026</td>'
        '</tr></table>'
    )

    class _Resp:
        text = html
    monkeypatch.setattr(c, "request", lambda method, url, **kw: _Resp())
    rows = c.list_dropping()
    domains = [r["domain"] for r in rows]
    assert domains == ["first-drop.ru", "second-drop.ru"]
    assert "reg.ru" not in domains and "yandex.ru" not in domains
    assert all(r["source"] == "reg_ru" and r["referring_domains"] is None for r in rows)


def test_regru_drops_ping(monkeypatch):
    from app.integrations.regru_drops import RegruDropsClient
    c = RegruDropsClient()

    class _Ok:
        text = '<td class="b-table__cell_node_first">x.ru</td>'
    monkeypatch.setattr(c, "request", lambda method, url, **kw: _Ok())
    assert c.ping() is True

    class _Empty:
        text = "<html>ничего нет</html>"
    monkeypatch.setattr(c, "request", lambda method, url, **kw: _Empty())
    assert c.ping() is False
```

- [ ] **Step 2: Запустить тест, убедиться что падает**

Run: `.venv/bin/python -m pytest backend/tests/test_sources.py::test_regru_drops_extracts_only_domain_cells -v`
Expected: FAIL — `ModuleNotFoundError`/`AttributeError` (класс ещё не переписан) или
`ImportError` на старый `from app.integrations.aparser import AParserClient` внутри
`RegruDropsClient`, вызванный монки-патчем несуществующего `request`.

- [ ] **Step 3: Переписать `regru_drops.py`**

Заменить содержимое файла целиком на:

```python
"""reg.ru/domain/deleted — HTML-витрина дропов, только страница 1.

Прямой httpx (BaseClient), не A-Parser: живая проверка (2026-07-08) — страница отдаёт
200 без анти-бота даже с дефолтным httpx User-Agent (`python-httpx/x.x`), JS-рендер не
нужен. Один запрос за прогон discovery — тот же низкообъёмный профиль, что у cctld,
ротация прокси A-Parser не требуется. Если позже добавим пагинацию (десятки/сотни
запросов за прогон) — транспорт нужно будет пересмотреть обратно на A-Parser, см.
docs/superpowers/specs/2026-07-08-discovery-source-markup-fix-design.md.
"""
import re
from app.integrations.base import BaseClient

_URL = "https://www.reg.ru/domain/deleted/"
# Ячейка с доменом — единственная с этим классом (у ячеек с датами тот же базовый
# класс, но без node_first; заголовок таблицы "Домен" — <th>, не <td>, сюда не попадает).
_DOMAIN_CELL = re.compile(r'b-table__cell_node_first">\s*([^<]*?)\s*</td>')


class RegruDropsClient(BaseClient):
    def __init__(self):
        super().__init__("https://www.reg.ru")

    def list_dropping(self) -> list[dict]:
        r = self.request("GET", _URL)
        return [{"domain": d, "source": "reg_ru", "referring_domains": None}
                for d in _DOMAIN_CELL.findall(r.text)]

    def ping(self) -> bool:
        r = self.request("GET", _URL)
        return bool(_DOMAIN_CELL.search(r.text))
```

- [ ] **Step 4: Запустить тест, убедиться что проходит**

Run: `.venv/bin/python -m pytest backend/tests/test_sources.py::test_regru_drops_extracts_only_domain_cells backend/tests/test_sources.py::test_regru_drops_ping -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Прогнать весь сьют и pyflakes**

Run: `.venv/bin/python -m pytest backend/tests/ -q`
Expected: `186 passed` (было 184 до этого плана, +2 новых теста этой задачи), 0 failed.

Run: `.venv/bin/python -m pyflakes backend/app backend/tests`
Expected: пусто (без вывода)

- [ ] **Step 6: Commit**

```bash
git add backend/app/integrations/regru_drops.py backend/tests/test_sources.py
git commit -m "fix(discovery): reg_ru — точечный якорь на b-table__cell_node_first, прямой httpx"
```

---

### Task 2: `sweb_drops.py` — якорь на label+value + прямой httpx

**Files:**
- Modify: `backend/app/integrations/sweb_drops.py` (полностью переписать)
- Test: `backend/tests/test_sources.py` (добавить тесты в конец файла)

**Interfaces:**
- Consumes: то же самое, что Task 1 (`app.integrations.base.BaseClient`). Независимо от
  Task 1 — `RegruDropsClient` не переиспользуется.
- Produces: `SwebDropsClient` — `list_dropping(self) -> list[dict]` (dict:
  `{"domain": str, "source": "sweb", "referring_domains": None}`) и `ping(self) -> bool`.
  `discovery.py::_sources()` уже маппит `"sweb": SwebDropsClient` — не трогаем.

- [ ] **Step 1: Написать падающий тест на регрессию с датами**

Добавить в конец `backend/tests/test_sources.py`:

```python
def test_sweb_drops_excludes_dates_sharing_class_with_domain(monkeypatch):
    """Регрессия (найдена при ревью спеки, 2026-07-08): domains-deleted__text — ОБЩИЙ
    класс для домена И обеих дат (регистрация/освобождение), в отличие от reg.ru тут нет
    отдельного "первая колонка" маркера. Наивный якорь на класс поймал бы '07.07.2026'
    как домен (три числовые группы через точку проходят простую проверку домен-формы).
    Якорь на label='Домен' должен вернуть только реальные домены."""
    from app.integrations.sweb_drops import SwebDropsClient
    c = SwebDropsClient()
    html = (
        '<li class="domains-deleted__item">'
        '<div class="domains-deleted__item-row">'
        '<span class="domains-deleted__label">Домен</span>'
        '<span class="domains-deleted__text">first-drop.ru</span></div>'
        '<div class="domains-deleted__item-row">'
        '<span class="domains-deleted__label">Первичная регистрация</span>'
        '<span class="domains-deleted__text">03.06.2022</span></div>'
        '<div class="domains-deleted__item-row">'
        '<span class="domains-deleted__label">Дата освобождения</span>'
        '<span class="domains-deleted__text">07.07.2026</span></div>'
        '</li>'
        '<li class="domains-deleted__item">'
        '<div class="domains-deleted__item-row">'
        '<span class="domains-deleted__label">Домен</span>'
        '<span class="domains-deleted__text">second-drop.ru</span></div>'
        '<div class="domains-deleted__item-row">'
        '<span class="domains-deleted__label">Первичная регистрация</span>'
        '<span class="domains-deleted__text">12.11.2012</span></div>'
        '<div class="domains-deleted__item-row">'
        '<span class="domains-deleted__label">Дата освобождения</span>'
        '<span class="domains-deleted__text">07.07.2026</span></div>'
        '</li>'
    )

    class _Resp:
        text = html
    monkeypatch.setattr(c, "request", lambda method, url, **kw: _Resp())
    rows = c.list_dropping()
    domains = [r["domain"] for r in rows]
    assert domains == ["first-drop.ru", "second-drop.ru"]
    assert "03.06.2022" not in domains
    assert "07.07.2026" not in domains
    assert "12.11.2012" not in domains
    assert all(r["source"] == "sweb" and r["referring_domains"] is None for r in rows)


def test_sweb_drops_ping(monkeypatch):
    from app.integrations.sweb_drops import SwebDropsClient
    c = SwebDropsClient()

    class _Ok:
        text = ('<span class="domains-deleted__label">Домен</span>'
                '<span class="domains-deleted__text">x.ru</span>')
    monkeypatch.setattr(c, "request", lambda method, url, **kw: _Ok())
    assert c.ping() is True

    class _Empty:
        text = "<html>ничего нет</html>"
    monkeypatch.setattr(c, "request", lambda method, url, **kw: _Empty())
    assert c.ping() is False
```

- [ ] **Step 2: Запустить тест, убедиться что падает**

Run: `.venv/bin/python -m pytest backend/tests/test_sources.py::test_sweb_drops_excludes_dates_sharing_class_with_domain -v`
Expected: FAIL (старая реализация через `AParserClient`/`_parse_domains` не даст
ожидаемый результат при монки-патче `request`, которого у старого класса даже нет —
`AttributeError`).

- [ ] **Step 3: Переписать `sweb_drops.py`**

Заменить содержимое файла целиком на:

```python
"""sweb.ru/domains/deleted — HTML-витрина дропов, только страница 1.

Прямой httpx (BaseClient) — тот же профиль и обоснование, что у regru_drops.py (1 запрос
за прогон, антибота нет, A-Parser не нужен для такого объёма).

ВАЖНО: класс `domains-deleted__text` НЕ уникален для домена — тот же класс используют
обе даты (регистрация/освобождение) в соседних полях того же item-блока. Наивный якорь
на этот класс поймал бы даты (DD.MM.YYYY) как "домены". Якорим на конкретную пару
label+value, где label буквально "Домен".
"""
import re
from app.integrations.base import BaseClient

_URL = "https://sweb.ru/domains/deleted/"
_DOMAIN_FIELD = re.compile(
    r'domains-deleted__label">Домен</span>\s*'
    r'<span class="domains-deleted__text">\s*([^<]*?)\s*</span>'
)


class SwebDropsClient(BaseClient):
    def __init__(self):
        super().__init__("https://sweb.ru")

    def list_dropping(self) -> list[dict]:
        r = self.request("GET", _URL)
        return [{"domain": d, "source": "sweb", "referring_domains": None}
                for d in _DOMAIN_FIELD.findall(r.text)]

    def ping(self) -> bool:
        r = self.request("GET", _URL)
        return bool(_DOMAIN_FIELD.search(r.text))
```

- [ ] **Step 4: Запустить тест, убедиться что проходит**

Run: `.venv/bin/python -m pytest backend/tests/test_sources.py::test_sweb_drops_excludes_dates_sharing_class_with_domain backend/tests/test_sources.py::test_sweb_drops_ping -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Прогнать весь сьют и pyflakes**

Run: `.venv/bin/python -m pytest backend/tests/ -q`
Expected: `188 passed` (186 после Task 1, +2 новых теста этой задачи), 0 failed.

Run: `.venv/bin/python -m pyflakes backend/app backend/tests`
Expected: пусто

- [ ] **Step 6: Commit**

```bash
git add backend/app/integrations/sweb_drops.py backend/tests/test_sources.py
git commit -m "fix(discovery): sweb — якорь на label='Домен', не даёт датам протечь как доменам"
```

---

### Task 3: `cctld.py` — переход на скачивание ZIP-реестра + уборка мёртвого кода

**Files:**
- Modify: `backend/app/integrations/cctld.py` (полностью переписать)
- Modify: `backend/tests/test_sources.py:90-95` (удалить `test_parse_domains_extracts_ru`
  — тестирует функцию `_parse_domains`, которая в этой задаче удаляется; после Task 1/2
  на неё больше никто не ссылается)
- Modify: `backend/tests/conftest.py:53` (поправить устаревший комментарий — reg_ru/sweb
  больше не ходят через A-Parser с Task 1/2)
- Test: `backend/tests/test_sources.py` (добавить новые тесты в конец файла)

**Interfaces:**
- Consumes: `app.integrations.base.BaseClient` — та же база, что в Task 1/2.
  ВАЖНО: к моменту этой задачи `regru_drops.py`/`sweb_drops.py` уже НЕ импортируют
  `app.integrations.cctld._parse_domains` (сделано в Task 1/2) — удалять функцию
  безопасно.
- Produces: `CctldClient` — `list_dropping(self) -> list[dict]` (dict:
  `{"domain": str, "source": "cctld", "referring_domains": None}`) и `ping(self) -> bool`.
  Финальная задача этого плана — после неё `discovery.py` не меняется, все три клиента
  готовы к ручному включению через `/settings`.

- [ ] **Step 1: Удалить устаревший тест на `_parse_domains`**

В `backend/tests/test_sources.py` удалить строки 90-95 (функция
`test_parse_domains_extracts_ru`):

```python
def test_parse_domains_extracts_ru():
    from app.integrations.cctld import _parse_domains
    html = "<tr><td>Example-1.RU</td></tr><tr><td>второй.рф</td></tr> мусор foo.com bar"
    got = _parse_domains(html)
    assert "example-1.ru" in got and "второй.рф" in got
    assert "foo.com" not in got          # берём только .ru/.рф/.su
```

(Эту функцию мы удаляем из `cctld.py` в Step 3 — на неё больше никто не ссылается
после Task 1/2.)

- [ ] **Step 2: Написать падающие тесты на новую zip-логику**

Добавить в конец `backend/tests/test_sources.py`:

```python
def _make_zip(filename: str, lines: list[str]) -> bytes:
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(filename, "\n".join(lines))
    return buf.getvalue()


def test_cctld_downloads_both_zips_and_lists_domains(monkeypatch):
    """Живая проверка (2026-07-08): лендинг не содержит домены — только ссылки на
    ежедневные ZIP (RUDelList<YYYYMMDD>.zip / RFDelList<YYYYMMDD>.zip), внутри — простой
    текстовый список, один домен на строку."""
    from app.integrations.cctld import CctldClient
    c = CctldClient()
    landing = (
        '<a href="/files/docs/pendingdelete/RUDelList20260708.zip">RU</a> '
        '<a href="/files/docs/pendingdelete/RFDelList20260708.zip">RF</a>'
    )
    ru_zip = _make_zip("RUDelList20260708.txt", ["one.ru", "two.ru"])
    rf_zip = _make_zip("RFDelList20260708.txt", ["xn--e1afmkfd.xn--p1ai"])

    class _Resp:
        def __init__(self, text=None, content=None):
            self.text = text
            self.content = content

    def fake_request(method, url, **kw):
        if url.endswith("RUDelList20260708.zip"):
            return _Resp(content=ru_zip)
        if url.endswith("RFDelList20260708.zip"):
            return _Resp(content=rf_zip)
        return _Resp(text=landing)

    monkeypatch.setattr(c, "request", fake_request)
    rows = c.list_dropping()
    domains = {r["domain"] for r in rows}
    assert domains == {"one.ru", "two.ru", "xn--e1afmkfd.xn--p1ai"}
    assert all(r["source"] == "cctld" and r["referring_domains"] is None for r in rows)


def test_cctld_partial_zip_failure_logs_and_returns_other(monkeypatch, caplog):
    """Спека: если один zip не скачался/битый — не ронять весь источник, вернуть то,
    что получилось от другого, и залогировать warning (иначе тихий частичный отказ
    недебажим — тот же класс бага, что уже чинили в discovery.py I6)."""
    from app.integrations.cctld import CctldClient
    c = CctldClient()
    landing = (
        '<a href="/files/docs/pendingdelete/RUDelList20260708.zip">RU</a> '
        '<a href="/files/docs/pendingdelete/RFDelList20260708.zip">RF</a>'
    )
    ru_zip = _make_zip("RUDelList20260708.txt", ["ok.ru"])

    class _Resp:
        def __init__(self, text=None, content=None):
            self.text = text
            self.content = content

    def fake_request(method, url, **kw):
        if url.endswith("RUDelList20260708.zip"):
            return _Resp(content=ru_zip)
        if url.endswith("RFDelList20260708.zip"):
            return _Resp(content=b"not a zip file")
        return _Resp(text=landing)

    monkeypatch.setattr(c, "request", fake_request)
    with caplog.at_level("WARNING", logger="app.integrations.cctld"):
        rows = c.list_dropping()
    assert [r["domain"] for r in rows] == ["ok.ru"]
    assert any("cctld" in r.message for r in caplog.records)


def test_cctld_ping_true_when_zip_href_found(monkeypatch):
    from app.integrations.cctld import CctldClient
    c = CctldClient()

    class _Resp:
        text = '<a href="/files/docs/pendingdelete/RUDelList20260708.zip">RU</a>'
    monkeypatch.setattr(c, "request", lambda method, url, **kw: _Resp())
    assert c.ping() is True


def test_cctld_ping_false_when_no_zip_href(monkeypatch):
    from app.integrations.cctld import CctldClient
    c = CctldClient()

    class _Resp:
        text = "<html>ничего нет</html>"
    monkeypatch.setattr(c, "request", lambda method, url, **kw: _Resp())
    assert c.ping() is False
```

- [ ] **Step 3: Запустить тесты, убедиться что падают**

Run: `.venv/bin/python -m pytest backend/tests/test_sources.py::test_cctld_downloads_both_zips_and_lists_domains backend/tests/test_sources.py::test_cctld_partial_zip_failure_logs_and_returns_other backend/tests/test_sources.py::test_cctld_ping_true_when_zip_href_found backend/tests/test_sources.py::test_cctld_ping_false_when_no_zip_href -v`
Expected: FAIL — старый `CctldClient` не знает про zip-логику, `list_dropping()` вернёт
не то (регэксп по лендингу словит навигационный мусор, не будет пустым, но домены не
совпадут с ожидаемыми `{"one.ru", "two.ru", ...}").

- [ ] **Step 4: Переписать `cctld.py`**

Заменить содержимое файла целиком на:

```python
"""cctld.ru — реестр освобождающихся .ru/.рф (авторитетный сырой список). Транспорт + парс.

Лендинг https://cctld.ru/service/dellist/ НЕ содержит список доменов — только ссылки на
ежедневные ZIP-архивы (RUDelList<YYYYMMDD>.zip / RFDelList<YYYYMMDD>.zip), которые и есть
реестр: один домен на строку, уже punycode для .рф (проверено вживую 2026-07-08 — 9463 +
438 строк, UTF-8, 0 пустых, 0 дублей, 100% строк — валидные домены). Дату не строим сами
(таймзона/выходные/факт публикации) — регэкспом достаём актуальные href прямо со страницы.
"""
import io
import logging
import re
import zipfile
from app.integrations.base import BaseClient

logger = logging.getLogger(__name__)

_ZIP_HREF = re.compile(r'href="([^"]*(?:RUDelList|RFDelList)\d{8}\.zip)"')


class CctldClient(BaseClient):
    URL = "https://cctld.ru/service/dellist/"

    def __init__(self):
        super().__init__("https://cctld.ru")

    def _zip_urls(self) -> list[str]:
        r = self.request("GET", self.URL)
        hrefs = _ZIP_HREF.findall(r.text)
        return [h if h.startswith("http") else f"{self.base_url}{h}" for h in hrefs]

    def _domains_from_zip(self, url: str) -> list[str]:
        r = self.request("GET", url)
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        text = zf.read(zf.namelist()[0]).decode("utf-8")
        return [ln.strip() for ln in text.splitlines() if ln.strip()]

    def list_dropping(self) -> list[dict]:
        domains: list[str] = []
        for url in self._zip_urls():
            try:
                domains.extend(self._domains_from_zip(url))
            except Exception as e:  # noqa: BLE001 — один битый/недоступный zip не должен ронять другой
                logger.warning("cctld: не удалось скачать/распаковать %s: %s", url, e)
        return [{"domain": d, "source": "cctld", "referring_domains": None} for d in domains]

    def ping(self) -> bool:
        return bool(self._zip_urls())
```

- [ ] **Step 5: Запустить тесты, убедиться что проходят**

Run: `.venv/bin/python -m pytest backend/tests/test_sources.py::test_cctld_downloads_both_zips_and_lists_domains backend/tests/test_sources.py::test_cctld_partial_zip_failure_logs_and_returns_other backend/tests/test_sources.py::test_cctld_ping_true_when_zip_href_found backend/tests/test_sources.py::test_cctld_ping_false_when_no_zip_href -v`
Expected: PASS (4 passed)

- [ ] **Step 6: Поправить устаревший комментарий в `conftest.py`**

В `backend/tests/conftest.py` найти (около строки 53) докстринг фикстуры
`_default_sources_backorder_only`, строку:

```
    воронки видит только backorder включённым — многоисточниковые cctld/reg_ru/sweb (A-Parser)
```

Заменить на (reg_ru/sweb больше не ходят через A-Parser после Task 1/2 — только cctld
исторически не ходил, теперь и reg_ru/sweb тоже прямой httpx):

```
    воронки видит только backorder включённым — многоисточниковые cctld/reg_ru/sweb (httpx)
```

- [ ] **Step 7: Прогнать весь сьют и pyflakes**

Run: `.venv/bin/python -m pytest backend/tests/ -q`
Expected: `191 passed` (188 после Task 2, минус 1 удалённый тест
`test_parse_domains_extracts_ru` из Step 1, плюс 4 новых теста этой задачи), 0 failed.

Run: `.venv/bin/python -m pyflakes backend/app backend/tests`
Expected: пусто (без вывода)

- [ ] **Step 8: Commit**

```bash
git add backend/app/integrations/cctld.py backend/tests/test_sources.py backend/tests/conftest.py
git commit -m "fix(discovery): cctld — качаем ежедневный ZIP-реестр вместо regex по лендингу"
```
