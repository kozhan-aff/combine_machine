"""Структурная страница писателя: схема `PageDoc` (JSON-ответ модели), её защитный разбор и рендер
блоков в обычный HTML для `Page.body` (спека 2026-10-10 §6.2). Чистые функции: без сети, БД и LLM.

Модель отдаёт ПРОСТОЙ текст: любая разметка в строках экранируется, теги ставит только рендер —
и только из `content._ALLOWED_TAGS`, поэтому `content._sanitize` возвращает результат как есть.
"""
import html

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.services.locales import t

# границы объёма по типу страницы, слов; выход за них — замечание критика, не отказ разбора
WORDS = {"review": (1500, 2200), "comparison": (1200, 1800), "howto": (900, 1400)}

_MAX_ERRORS, _MAX_ERROR_LEN = 5, 600


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

    @model_validator(mode="after")
    def _rows_match_columns(self):
        for n, row in enumerate(self.rows, 1):
            if len(row) != len(self.columns):
                raise ValueError(f"в таблице строка {n}: ячеек {len(row)}, а колонок {len(self.columns)}")
        return self


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


def parse(raw: str) -> PageDoc:
    """Ответ модели -> PageDoc. Ограда ``` и текст вокруг срезаются по крайним «{»…«}».
    Любой провал — ValueError с коротким текстом: он уходит модели в повторный запрос и оператору."""
    raw = raw or ""
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end < start:
        raise ValueError("в ответе нет JSON-объекта")
    try:
        return PageDoc.model_validate_json(raw[start:end + 1])
    except ValidationError as e:
        errors = e.errors(include_url=False, include_input=False)
        parts = []
        for err in errors[:_MAX_ERRORS]:
            path = ".".join(str(p) for p in err["loc"]) or "ответ"     # битый/обрезанный JSON — ошибка без пути
            parts.append(f"{path}: {err['msg'].removeprefix('Value error, ')}")
        msg = "; ".join(parts)
        if len(errors) > _MAX_ERRORS:
            msg += f" (и ещё {len(errors) - _MAX_ERRORS})"
        raise ValueError(msg[:_MAX_ERROR_LEN]) from None


# NUL и \r nh3 выбрасывает/переписывает, неразрывный пробел сериализует как &nbsp; — отдаём сразу в
# том виде, в каком текст вернёт санитайзер, чтобы body после _sanitize совпадал с рендером байт в байт
_DROP = {0x00: None, 0x0D: None}


def _e(text: str) -> str:
    # quote=False: текст не попадает в атрибуты, а &quot;/&#x27; nh3 всё равно развернул бы обратно
    return html.escape(text.translate(_DROP), quote=False).replace("\xa0", "&nbsp;")


def _tags(tag: str, items) -> str:
    return "".join(f"<{tag}>{_e(x)}</{tag}>" for x in items)


def render_blocks(doc: PageDoc, kind: str, lang: str) -> str:
    """HTML-фрагмент тела страницы: verdict → pros/cons → table → sections → steps → faq.
    Рисуется то, что есть в документе; `kind` состав блоков не меняет (какие блоки нужны типу
    страницы — забота брифа и критика). `<h1>` нет: заголовок страницы рисует шапка сайта."""
    out = []
    if doc.verdict:
        v = doc.verdict
        out.append(f"<p><strong>{_e(t(lang, 'lbl_score'))}: {v.score:g}/10.</strong> {_e(v.summary)}</p>"
                   f"<p><strong>{_e(t(lang, 'lbl_for'))}:</strong> {_e(v.for_whom)}</p>"
                   f"<p><strong>{_e(t(lang, 'lbl_not_for'))}:</strong> {_e(v.not_for_whom)}</p>")
    for key, items in (("lbl_pros", doc.pros), ("lbl_cons", doc.cons)):
        if items:
            out.append(f"<h3>{_e(t(lang, key))}</h3><ul>{_tags('li', items)}</ul>")
    if doc.table:
        rows = "".join(f"<tr>{_tags('td', row)}</tr>" for row in doc.table.rows)
        out.append(f"<table><thead><tr>{_tags('th', doc.table.columns)}</tr></thead>"
                   f"<tbody>{rows}</tbody></table>")
    for s in doc.sections:
        out.append(f"<h2>{_e(s.h2)}</h2>{_tags('p', s.paragraphs)}")
        if s.bullets:
            out.append(f"<ul>{_tags('li', s.bullets)}</ul>")
        for h in s.h3s or []:
            out.append(f"<h3>{_e(h.h3)}</h3>{_tags('p', h.paragraphs)}")
    if doc.steps:
        items = "".join(
            f"<li><strong>{_e(st.title)}</strong>{f' ({_e(st.platform)})' if st.platform else ''}"
            f" — {_e(st.text)}</li>" for st in doc.steps)
        out.append(f"<h2>{_e(t(lang, 'lbl_steps'))}</h2><ol>{items}</ol>")
    if doc.faq:
        out.append(f"<h2>{_e(t(lang, 'lbl_faq'))}</h2>"
                   + "".join(f"<h3>{_e(f.q)}</h3><p>{_e(f.a)}</p>" for f in doc.faq))
    return "".join(out)


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
