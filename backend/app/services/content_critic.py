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
import html
import re
import unicodedata
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import nh3

from app.services.brief import norm_number
from app.services.locales import norm_lang, supported
from app.services.page_doc import WORDS
from app.services.research_extract import NUM_RE

_SCORE_RE = re.compile(r"БАЛЛ:\s*(\d+)", re.I)
# Раскрытие партнёрства добавляет render_html на КАЖДУЮ публикуемую страницу детерминированно
# (services/locales + content.render_html), а в body его нет по построению. Поэтому критик не
# вправе судить о нём по тексту черновика: «нет disclosure» на каждой странице — ложь (S6-15).
_DISCLOSURE_RE = re.compile(r"disclosure|дисклоужер|раскрыти|пометк\w*\s+о\s+партн", re.I)


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


_SYSTEM_PROMPT = (
    "Ты — редактор VPN-сайта. Оцени черновик страницы по четырём критериям: "
    "(1) тема соответствует бренду/офферу, (2) есть конкретные факты/цифры "
    "вертикали, а не только общие фразы, (3) язык текста соответствует "
    "заявленному, (4) текст не выглядит как общая AI-вода без содержания. "
    "Пометку о партнёрских ссылках НЕ оценивай: её добавляет шаблон страницы. "
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
        parsed["issues"] = [i for i in parsed["issues"] if not _DISCLOSURE_RE.search(i)]
        if not text.strip() and error is None:
            error = "пустой ответ LLM (фильтр/blocked) — оценка недоступна"

        page.critic_score = parsed["score"]
        page.critic_notes = {"issues": parsed["issues"]} if parsed["issues"] else None
        page.critic_checked_at = datetime.now(timezone.utc)
        db.commit()
        return {"score": parsed["score"], "issues": parsed["issues"], "error": error}


# ── Проверки кодом (план Б, задача 5) ────────────────────────────────────────────────────────────────
# Детерминированная половина критика: чистые функции над видимым текстом страницы, без БД, сети и LLM.
# Каждая проверка возвращает список замечаний — русских фраз, которые оператор читает на карточке сайта;
# пустой список = чисто. То, что кодом не проверить (смысл, вода, тон), судит чек-лист модели.
# На кривой вход (None, не тот тип) проверки не падают: критик отказывает замечанием, а не исключением.

SHINGLE = 12                  # слов подряд, совпавших с источником, — уже копия, а не общий оборот речи
_COPY_MAX = 3                 # столько скопированных фраз цитируем: дальше оператору и так всё ясно
_NUMS_MAX = 10                # столько чисел без источника перечисляем в замечании
_SMALL_INT = 12               # целые до этого — счёт шагов, оценки, пункты списка, а не факты
_RU_MIN, _LATIN_MAX = 0.5, 0.2   # доля кириллицы среди букв: русскому тексту не меньше (инструкция с
                                 # латинскими названиями кнопок даёт 54–55%), латинским языкам не больше
_FOREIGN_MAX = 0.1            # доля букв вне латиницы и кириллицы: выше — текст уехал в чужой алфавит

# Невидимые знаки внутри слова (мягкий перенос, пробелы нулевой ширины, BOM) слово не рвут и в сравнение
# не идут: иначе копия, прошитая ими, не совпала бы с источником ни одним шинглом.
_INVISIBLE = "\xad\u200b\u200c\u200d\ufeff"
_WORD_RE = re.compile(rf"\w+(?:[{_INVISIBLE}]+\w+)*")
_DROP_INVISIBLE = dict.fromkeys(map(ord, _INVISIBLE))

# Запись «5,000» / «6.000» / «2,000,000»: группы по три цифры после точки или запятой, первая группа не
# ноль («0.500» — дробь). Может быть тысячами, поэтому под «малое целое» и «год» не подпадает никогда.
_THOUSANDS_RE = re.compile(r"(?!0+[.,])\d{1,3}(?:[.,]\d{3})+")
# Слово-множитель сразу за числом: «6 тысяч» — это шесть тысяч, а не «малое целое».
_MAGNITUDE_RE = re.compile(
    r"\s?(тысяч\w*|тыс\.?|млн\.?|млрд\.?|миллион\w*|миллиард\w*|thousands?|millions?|billions?)(?!\w)", re.I)
_POWERS = (("тыс", 3), ("thousand", 3), ("млн", 6), ("миллион", 6), ("million", 6),
           ("млрд", 9), ("миллиард", 9), ("billion", 9))
_GLUED_K_RE = re.compile(r"(?<![\w.])(\d+(?:[.,]\d+)?)[Kk](?!\w)")     # «5K» — слитно, NUM_RE его не видит

# Числа-идентификаторы — не факты, источник им не нужен. Перед поиском чисел заменяются пробелом.
# Имя и число — только в одной строке (пробел, не `\s`): перевод строки — граница блока, и ячейка
# «Android» не должна спрятать число из соседней ячейки.
_IDENT_RE = re.compile(r"""
      (?<!\w)(?:AES|SHA|RSA|TLS|SSL|IKEv|OpenVPN|WireGuard|iOS|Android|Windows|macOS|Wi-?Fi)[ \xa0-]?\d+(?:\.\d+)*
                                                    # алгоритм, протокол, версия ОС: AES-256, TLS 1.3, iOS 17.4
    | \d+[ \xa0-]?(?:bit|бит)\w*                    # разрядность: 256-bit, 256 бит
    | (?<![\w/])24/7(?![\w/])                       # круглосуточно
    | (?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])    # IPv4: 1.1.1.1
    | (?<![\d.])\d{1,2}\.\d{1,2}\.\d{4}(?!\d)       # дата дд.мм.гггг
    | (?<!\d)\d{4}-\d{2}-\d{2}(?!\d)                # дата гггг-мм-дд
    | (?:порт|port)[ \xa0]+\d+                      # номер порта
    | \d+(?:[.,]\d+)?[ \xa0]*/[ \xa0]*10(?!\d)      # оценка вердикта из шаблона: 8.5/10
    | (?<!\w)(?-i:[48]K)(?!\w)                      # разрешение видео 4K/8K (заглавная K), не «4 тысячи»
""", re.I | re.X)


def visible_text(body_html: str) -> str:
    """HTML тела страницы -> текст, который видит читатель. Каждый тег становится ПЕРЕВОДОМ СТРОКИ, а не
    пробелом: соседние ячейки `<td>10</td><td>111</td>` иначе читались бы одним числом «10 111» (пробел —
    законный разделитель тысяч, перевод строки — нет). Перенос строки в самой разметке — обычный пробел,
    как в браузере: абзац остаётся одной строкой. Скрипты и стили nh3 выбрасывает целиком, сущности
    раскрываются один раз, пробелы внутри строки схлопнуты, пустые строки убраны."""
    if not isinstance(body_html, str):
        return ""
    flat = " ".join(body_html.split()).replace("<", "\n<")
    flat = flat.encode("utf-8", "replace").decode("utf-8")       # одиночный суррогат уронил бы nh3
    text = html.unescape(nh3.clean(flat, tags=set(), attributes={}))
    lines = (" ".join(line.split()) for line in text.splitlines())
    return "\n".join(line for line in lines if line)


def _norm_word(w: str) -> str:
    """Слово в сравнимом виде — ОДНО правило для страницы и источника: NFKC (лигатуры, полноширинные
    знаки), без невидимых знаков, регистр, «ё» = «е»."""
    return unicodedata.normalize("NFKC", w).translate(_DROP_INVISIBLE).lower().replace("ё", "е")


def _copy_issues(text: str, sources: list[str]) -> list[str]:
    """Шинглы по SHINGLE слов текста против каждого источника. Один проход по тексту и по одному на
    источник (поиск в словаре) — страница в 2500 слов против пяти источников по 7000 считается за
    десятки миллисекунд. Источник короче шингла ничего не даёт: его range пуст."""
    if not sources:
        return []
    spans = [m.span() for m in _WORD_RE.finditer(text)]
    low = [_norm_word(text[a:b]) for a, b in spans]
    first = {}                                   # шингл текста -> позиция его первого вхождения
    for i in range(len(low) - SHINGLE + 1):
        first.setdefault(tuple(low[i:i + SHINGLE]), i)
    hits = set()
    for src in sources if first else ():
        sw = [_norm_word(w) for w in _WORD_RE.findall(src)]
        for i in range(len(sw) - SHINGLE + 1):
            pos = first.get(tuple(sw[i:i + SHINGLE]))
            if pos is not None:
                hits.add(pos)
    # кусок в 30 скопированных слов — это 19 шинглов со сдвигом на слово; цитируем фразы встык, без
    # перекрытий, и так, как они стоят на странице (с пунктуацией — оператор найдёт их поиском)
    out, free = [], 0
    for pos in sorted(hits):
        if pos < free:
            continue
        quote = " ".join(text[spans[pos][0]:spans[pos + SHINGLE - 1][1]].split())
        out.append(f"копирование источника: «{quote}»")
        free = pos + SHINGLE
        if len(out) == _COPY_MAX:
            break
    return out


def _brand_issues(text: str, brand: str | None) -> list[str]:
    """Бренд оффера обязан быть назван в тексте. Ищем его буквы и цифры подряд, между ними — что угодно,
    кроме букв: «Durev VPN» находится и как «DurevVPN», и как «durev-vpn», и разорванным тегом. Границы
    слова обязательны: «PIA» внутри «utopia» — не бренд. Раскрытие партнёрства здесь НЕ проверяется: его
    ставит шаблон (`render_html`), см. S6-15 у `_DISCLOSURE_RE`."""
    chars = [re.escape(c) for c in brand or "" if c.isalnum()]
    if not chars or re.search(r"(?<!\w)" + r"\W*".join(chars) + r"(?!\w)", text, re.I):
        return []                                # бренд без букв и цифр искать нечем — проверка пропущена
    return [f"в тексте нет бренда {' '.join(brand.split())}"]


def _lang_issues(text: str, lang: str) -> list[str]:
    """Язык по алфавиту букв. Кириллица: `ru` — не меньше `_RU_MIN`, остальные языки проекта (все на
    латинице) — не больше `_LATIN_MAX`; латинский бренд и названия протоколов русскому тексту не мешают.
    Буквы третьего алфавита (иероглифы, греческий) сверх `_FOREIGN_MAX` — замечание при любом языке."""
    # ponytail: только алфавит; en от de так не отличить — это ловит чек-лист модели.
    cyr = lat = other = 0
    for c in text:
        if not c.isalpha():
            continue
        if "\u0400" <= c <= "\u052f":
            cyr += 1
        elif c < "\u0250" or "\u1e00" <= c <= "\u1eff":      # латиница с диакритикой всех языков проекта
            lat += 1
        else:
            other += 1
    total = cyr + lat + other
    if not total:
        # пустая страница не должна сойти за чистую только потому, что в ней нечего мерить
        return ["язык: в тексте нет букв"]
    out, share = [], cyr / total
    code = norm_lang(lang) if supported(lang) else None
    if code == "ru":
        if share < _RU_MIN:
            out.append(f"язык: кириллицы {share:.0%} букв, для ru нужно не меньше {_RU_MIN:.0%}")
    elif share > _LATIN_MAX:
        # язык не задан или без словаря: меряем как латинский, но говорим об этом прямо, а не «для en»
        whom = f"для {code}" if code else (
            f"код языка «{lang.strip()}» неизвестен — для латиницы" if lang.strip()
            else "язык страницы не задан — для латиницы")
        out.append(f"язык: кириллицы {share:.0%} букв, {whom} допустимо не больше {_LATIN_MAX:.0%}")
    if other / total > _FOREIGN_MAX:
        out.append(f"язык: посторонний алфавит — {other / total:.0%} букв не латиница и не кириллица")
    return out


def _volume_issues(text: str, kind: str | None) -> list[str]:
    """Число слов против границ типа страницы (`page_doc.WORDS`). Тип без границ или неизвестный путь
    (`kind is None`) — проверка пропускается: мерить не с чем."""
    if not isinstance(kind, str) or kind not in WORDS:
        return []
    lo, hi = WORDS[kind]
    n = len(_WORD_RE.findall(text))
    return [] if lo <= n <= hi else [f"объём {n} слов, нужно {lo}–{hi}"]


def _scaled(value: str, power: int) -> str:
    """«6» и степень 3 -> «6000» (в виде `norm_number`): так «6 тысяч» сверяется с «6000» источника."""
    try:
        return norm_number(format(Decimal(value).scaleb(power), "f"))
    except InvalidOperation:
        return ""


def _number_issues(text: str, allowed: set[str]) -> list[str]:
    """Числа текста, которых нет среди разрешённых (`brief.allowed_numbers`: досье, факты вертикали,
    условия промокода), — «факт без источника». Обе стороны сравниваются в виде `norm_number`; у
    неоднозначной записи достаточно одного совпавшего прочтения. Не считаются фактами идентификаторы
    (`_IDENT_RE`), целые до `_SMALL_INT` и годы «прошлый — текущий — следующий» (UTC на момент вызова) —
    кроме записей с разделителем тысяч и чисел со словом-множителем. В замечании числа стоят так, как
    написаны на странице."""
    year = datetime.now(timezone.utc).year
    clean = _IDENT_RE.sub(" ", text)
    found = sorted((*NUM_RE.finditer(clean), *_GLUED_K_RE.finditer(clean)), key=lambda m: m.start(1))
    bad, seen = [], set()
    for m in found:
        raw = shown = m.group(1)
        value = norm_number(raw)
        readings = {value}
        thousands = bool(_THOUSANDS_RE.fullmatch(raw))
        if thousands:
            readings.add(re.sub(r"[.,]", "", raw))       # «5,500»: дробь 5.5 или 5500 — годится любое
        power = 0
        if m.re is _GLUED_K_RE:
            power, shown = 3, m.group()
        else:
            word = _MAGNITUDE_RE.match(clean, m.end(1))
            if word:
                name = word.group(1).lower()
                power = next((p for prefix, p in _POWERS if name.startswith(prefix)), 3)
                shown = f"{raw} {word.group(1)}"
        if power:
            readings |= {_scaled(r, power) for r in readings}
        exempt = (not thousands and not power and value.isdigit()
                  and (int(value) <= _SMALL_INT or year - 1 <= int(value) <= year + 1))
        shown = " ".join(shown.split())          # неразрывный пробел в замечании — обычным
        if exempt or readings & allowed or shown in seen:
            continue
        seen.add(shown)
        bad.append(shown)
    if not bad:
        return []
    more = f" и ещё {len(bad) - _NUMS_MAX}" if len(bad) > _NUMS_MAX else ""
    return [f"числа без источника: {', '.join(bad[:_NUMS_MAX])}{more}"]


def _strings(value) -> list[str]:
    """Вход «список строк» защитно: None — пусто, одна строка — список из неё, не-строки отброшены."""
    if isinstance(value, str):
        return [value]
    try:
        return [x for x in value or () if isinstance(x, str)]
    except TypeError:                            # не перечисляется вовсе (число и т.п.)
        return []


def code_checks(*, text: str, kind: str | None, lang: str, brand: str | None,
                sources: list[str], allowed: set[str]) -> list[str]:
    """Все проверки кодом над видимым текстом страницы (`visible_text`). `sources` — тексты источников
    досье, `allowed` — `brief.allowed_numbers`. Список замечаний; пусто = код претензий не имеет.
    Кривой вход не роняет: не-строка читается как пустое значение, и проверки говорят об этом сами."""
    text = text if isinstance(text, str) else ""
    lang = lang if isinstance(lang, str) else ""
    brand = brand if isinstance(brand, str) else None
    return [*_copy_issues(text, _strings(sources)), *_brand_issues(text, brand), *_lang_issues(text, lang),
            *_volume_issues(text, kind), *_number_issues(text, set(_strings(allowed)))]
