"""Структурная страница писателя: схема `PageDoc` (JSON-ответ модели), её защитный разбор и рендер
блоков в обычный HTML для `Page.body` (спека 2026-10-10 §6.2). Чистые функции: без сети, БД и LLM.

Модель отдаёт ПРОСТОЙ текст: любая разметка в строках экранируется, теги ставит только рендер —
и только из `content._ALLOWED_TAGS`, поэтому `content._sanitize` возвращает результат как есть.
"""
import html
import json
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, ValidationError, model_validator

from app.services.locales import t

# границы объёма по типу страницы, слов; выход за них — замечание критика, не отказ разбора
WORDS = {"review": (1500, 2200), "comparison": (1200, 1800), "howto": (900, 1400)}

_MAX_ERRORS, _MAX_ERROR_LEN = 5, 600


def _drop_empty(value):
    """Пустые элементы списка строк (""/пробелы/null) молча выбрасываются ДО проверки длины списка:
    один шальной "" — не ошибка, а список совсем без текста — ошибка схемы."""
    if not isinstance(value, list):
        return value
    return [x for x in value if not (x is None or isinstance(x, str) and not x.strip())]


Texts = Annotated[list[str], BeforeValidator(_drop_empty)]


class _Model(BaseModel):
    # модель пишет 3200 вместо "3200" и оставляет пробелы по краям — это не повод жечь единственный
    # повтор писателя: числа в строковых полях приводятся к строке, края обрезаются
    model_config = ConfigDict(coerce_numbers_to_str=True, str_strip_whitespace=True)


class Meta(_Model):
    title: str = Field(min_length=10, max_length=110)
    description: str = Field(min_length=40, max_length=200)


class Verdict(_Model):
    score: float = Field(ge=1, le=10)
    summary: str = Field(min_length=1)
    for_whom: str = Field(min_length=1)
    not_for_whom: str = Field(min_length=1)


class H3(_Model):
    h3: str = Field(min_length=1)
    paragraphs: Texts = Field(min_length=1)


class Section(_Model):
    h2: str = Field(min_length=1)
    paragraphs: Texts = Field(min_length=1)
    bullets: Texts | None = None
    h3s: list[H3] | None = None


class Table(_Model):
    columns: list[str] = Field(min_length=2, max_length=6)
    rows: list[list[str]] = Field(min_length=1, max_length=20)      # пустая ячейка законна

    @model_validator(mode="after")
    def _rows_match_columns(self):
        for n, row in enumerate(self.rows, 1):
            if len(row) != len(self.columns):
                raise ValueError(f"в таблице строка {n}: ячеек {len(row)}, а колонок {len(self.columns)}")
        return self


class Step(_Model):
    title: str = Field(min_length=1)
    text: str = Field(min_length=1)
    platform: str | None = None


class Faq(_Model):
    q: str = Field(min_length=1)
    a: str = Field(min_length=1)


class PageDoc(_Model):
    model_config = ConfigDict(extra="ignore")
    meta: Meta
    verdict: Verdict | None = None
    pros: Texts = []
    cons: Texts = []
    sections: list[Section] = Field(min_length=2)
    table: Table | None = None
    steps: list[Step] | None = None
    faq: list[Faq] = []
    sources: Texts = []


def _find_doc(raw: str) -> dict | None:
    """Первый JSON-объект с ключом meta или sections. Перебор по каждой «{»: скобки в прозе,
    черновик в <think> и хвост после документа разбору не мешают."""
    decoder = json.JSONDecoder()
    pos = raw.find("{")
    while pos >= 0:
        try:
            obj, _ = decoder.raw_decode(raw, pos)
        except (ValueError, RecursionError):
            obj = None
        if isinstance(obj, dict) and ("sections" in obj or "meta" in obj):
            return obj
        pos = raw.find("{", pos + 1)
    return None


def _short(e: ValidationError) -> str:
    errors = e.errors(include_url=False, include_input=False)
    parts = []
    for err in errors[:_MAX_ERRORS]:
        path = ".".join(str(p) for p in err["loc"]) or "ответ"     # битый/обрезанный JSON — ошибка без пути
        parts.append(f"{path}: {err['msg'].removeprefix('Value error, ')}")
    msg = "; ".join(parts)
    if len(errors) > _MAX_ERRORS:
        msg += f" (и ещё {len(errors) - _MAX_ERRORS})"
    return msg[:_MAX_ERROR_LEN]


def parse(raw: str) -> PageDoc:
    """Ответ модели -> PageDoc: ограда ``` и текст вокруг документа отбрасываются.
    Любой провал — ValueError с коротким текстом: он уходит модели в повторный запрос и оператору."""
    if not isinstance(raw, str):
        raise ValueError("ответ модели не строка")
    raw = raw.strip()
    start = raw.find("{")
    if start < 0:
        raise ValueError("в ответе нет JSON-объекта")
    obj = _find_doc(raw)
    try:
        if obj is not None:
            return PageDoc.model_validate(obj)
        # документа не нашлось (обрезан, битый, не той формы): отдаём парсеру срез от первой «{» до
        # последней «}», чтобы в повторный запрос ушла настоящая ошибка JSON, а не «ничего нет»
        end = raw.rfind("}")
        return PageDoc.model_validate_json(raw[start:end + 1] if end > start else raw[start:])
    except ValidationError as e:
        raise ValueError(_short(e)) from None


def _e(text: str) -> str:
    # Текст отдаётся сразу в том виде, в каком его вернёт nh3, чтобы body после _sanitize совпадал с
    # рендером байт в байт: \r\n и одиночный \r — это \n, NUL выбрасывается, неразрывный пробел — &nbsp;.
    # quote=False: текст не попадает в атрибуты, а &quot;/&#x27; nh3 всё равно развернул бы обратно.
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    return html.escape(text, quote=False).replace("\xa0", "&nbsp;")


def _each(tag: str, items) -> list[str]:
    return [f"<{tag}>{_e(x)}</{tag}>" for x in items]


def _cells(tag: str, items) -> str:
    return "".join(_each(tag, items))


def render_blocks(doc: PageDoc, kind: str, lang: str) -> str:
    """HTML-фрагмент тела страницы: verdict → pros/cons → table → sections → steps → faq.
    Рисуется то, что есть в документе; `kind` состав блоков не меняет (какие блоки нужны типу
    страницы — забота брифа и критика). `<h1>` нет: заголовок страницы рисует шапка сайта.
    Каждый блочный элемент, пункт списка и строка таблицы — с новой строки: body читают и правят
    в редакторе панели."""
    out = []
    if doc.verdict:
        v = doc.verdict
        out += [f"<p><strong>{_e(t(lang, 'lbl_score'))}: {v.score:g}/10.</strong> {_e(v.summary)}</p>",
                f"<p><strong>{_e(t(lang, 'lbl_for'))}:</strong> {_e(v.for_whom)}</p>",
                f"<p><strong>{_e(t(lang, 'lbl_not_for'))}:</strong> {_e(v.not_for_whom)}</p>"]
    for key, items in (("lbl_pros", doc.pros), ("lbl_cons", doc.cons)):
        if items:
            out += [f"<h3>{_e(t(lang, key))}</h3>", "<ul>", *_each("li", items), "</ul>"]
    if doc.table:
        out += ["<table>", f"<thead><tr>{_cells('th', doc.table.columns)}</tr></thead>", "<tbody>",
                *(f"<tr>{_cells('td', row)}</tr>" for row in doc.table.rows), "</tbody>", "</table>"]
    for s in doc.sections:
        out += [f"<h2>{_e(s.h2)}</h2>", *_each("p", s.paragraphs)]
        if s.bullets:
            out += ["<ul>", *_each("li", s.bullets), "</ul>"]
        for h in s.h3s or []:
            out += [f"<h3>{_e(h.h3)}</h3>", *_each("p", h.paragraphs)]
    if doc.steps:
        out += [f"<h2>{_e(t(lang, 'lbl_steps'))}</h2>", "<ol>"]
        out += [f"<li><strong>{_e(st.title)}</strong>{f' ({_e(st.platform)})' if st.platform else ''}"
                f" — {_e(st.text)}</li>" for st in doc.steps]
        out.append("</ol>")
    if doc.faq:
        out.append(f"<h2>{_e(t(lang, 'lbl_faq'))}</h2>")
        for f in doc.faq:
            out += [f"<h3>{_e(f.q)}</h3>", f"<p>{_e(f.a)}</p>"]
    return "\n".join(out)


def doc_text(doc: PageDoc) -> str:
    """Весь текст тела страницы одной строкой, в порядке рендера — для критика (объём, шинглы,
    числа). Без ярлыков локали, без meta и sources: это текст модели, который читатель видит в теле."""
    parts = []
    if doc.verdict:
        parts += [doc.verdict.summary, doc.verdict.for_whom, doc.verdict.not_for_whom]
    parts += doc.pros + doc.cons
    if doc.table:
        parts += doc.table.columns
        for row in doc.table.rows:
            parts += row
    for s in doc.sections:
        parts += [s.h2, *s.paragraphs, *(s.bullets or [])]
        for h in s.h3s or []:
            parts += [h.h3, *h.paragraphs]
    for st in doc.steps or []:
        parts += [st.title, st.platform or "", st.text]
    for f in doc.faq:
        parts += [f.q, f.a]
    return " ".join(" ".join(parts).split())
