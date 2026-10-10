"""Чистые функции извлечения досье из HTML/CSS конкурента (спека 2026-10-10 §4.3). Без сети и БД:
stdlib html.parser + re + nh3 (через wayback._visible_text — судим по ВИДИМОМУ тексту)."""
import re
from bisect import bisect_left, bisect_right
from html.parser import HTMLParser
from urllib.parse import urljoin

from app.integrations.wayback import _visible_text

_NAV_TAGS = {"nav", "header", "footer", "aside"}
# Число в свободном тексте, ОДНА группа (потребители зовут findall). Три записи: группы тысяч через пробел
# (обычный, неразрывный, узкий неразрывный) — «1 500»; несколько групп через точку/запятую — «1,500,000»
# (однозначно тысячи); простое целое или дробь — «5.99». Одна группа «5,500» неоднозначна (дробь или
# тысячи) и остаётся дробью: оба прочтения учитывает brief.allowed_numbers.
_NUM_RE = re.compile(r"(?<![\w.])(\d{1,3}(?:[ \xa0\u202f]\d{3})+|\d{1,3}(?:[.,]\d{3}){2,}|\d+(?:[.,]\d+)?)(?![\w])")
NUM_RE = _NUM_RE            # публичное имя: регулярку читают бриф и критик
_GROUPS_RE = re.compile(r"\d{1,3}(?:[.,]\d{3}){2,}")
_WORD_RE = re.compile(r"\S+")
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
        # ответ — первый абзац (<p>) за заголовком; без <p> — весь кусок до следующего заголовка
        p = re.search(r"<p\b[^>]*>.*?</p>", a_html, re.I | re.S)
        a = " ".join(_visible_text(p.group(0) if p else a_html).split())[:600]
        if a:
            out.append({"q": q, "a": a})
    uniq, seen = [], set()
    for x in out:
        if x["q"].lower() not in seen:
            seen.add(x["q"].lower()); uniq.append(x)
    return uniq[:cap]


def number_value(raw: str) -> str:
    """Запись числа -> значение: без пробелов любого рода; несколько групп тысяч («1,500,000», «1.500.000»)
    — только цифры; иначе десятичная запятая -> точка."""
    v = "".join(str(raw).split())
    return re.sub(r"[.,]", "", v) if _GROUPS_RE.fullmatch(v) else v.replace(",", ".")


def numbers(text: str, cap: int = 60) -> list[dict]:
    """Числа видимого текста с контекстом ±8 слов. Ищем по ВСЕМУ тексту, а не по словам: «1 500 серверов» —
    одно число 1500, а не «1» и «500» (так терялись все числа с разделителем тысяч)."""
    text = text or ""
    spans = [m.span() for m in _WORD_RE.finditer(text)]
    starts = [a for a, _ in spans]
    words = [text[a:b] for a, b in spans]
    out = []
    for m in _NUM_RE.finditer(text):
        i = bisect_right(starts, m.start(1)) - 1      # слово, в котором число начинается…
        j = bisect_left(starts, m.end(1)) - 1         # …и в котором кончается: «1 500» — это два слова
        out.append({"value": number_value(m.group(1)), "ctx": " ".join(words[max(0, i - 8): j + 9])})
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
