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
_NUM_MAX_LEN = 20             # запись длиннее — не число, а поток цифр: помечаем, не разбирая (и без int())

# Числа-идентификаторы — не факты, источник им не нужен. Перед поиском чисел заменяются пробелом.
# Список намеренно узкий: «имя + любое число» прятало бы выдуманные факты («на WireGuard 450 Мбит/с»,
# «для Android 4500 отзывов», рейтинг «iOS 4.7»), поэтому у каждого имени — только его настоящие значения.
#  * имя и число — в одной строке (пробел, не `\s`): перевод строки — граница блока, и ячейка «Android»
#    не прячет число из соседней ячейки;
#  * у каждой ветки левая граница (`(?<!\w)` / `(?<![\w.,])`): «Транспорт 300» — не «порт 300», а на
#    потоке цифр движок не начинает разбор с каждой позиции (иначе квадратичное время);
#  * END — за числом нет продолжения («4,5», «4500»); NOUNIT — за ним нет единицы или счётного слова.
_UNIT = (r"(?:%|[кмг]бит|[кмгт]б(?!\w)|[kmgt]bit|[kmgt]bps|[kmgt]b(?!\w)|мс(?!\w)|ms(?!\w)|отзыв|оцен|зв[её]зд"
         r"|устройств|стран|сервер|локац|пользовател|клиент|руб|device|server|countr|location|user|review"
         r"|rating|star)")
_OCTET = r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)"
_IDENT_RE = re.compile(r"""
      (?<!\w)(?:AES|SHA|RSA)[ \xa0-]?(?:128|192|256|384|512|1024|2048|3072|4096)END    # шифр и длина ключа: AES-256
    | (?<!\w)SHA[ \xa0-]?[123]END                                                    # семейство: SHA-1, SHA-2
    | (?<!\w)(?:TLS|SSL|IKEv)[ \xa0-]?\d{1,2}(?:\.\d)?END                            # версия протокола: TLS 1.3
    | (?<!\w)(?:WireGuard|OpenVPN)[ \xa0-]?\d{1,2}(?:\.\d{1,2}){1,2}END NOUNIT       # только версия с точкой: OpenVPN 2.6
    | (?<!\w)Wi-?Fi[ \xa0-]?[4-7]END NOUNIT                                          # поколение: Wi-Fi 6
    | (?<!\w)iOS[ \xa0-]?(?:9|1\d|2[0-6])(?:\.\d){0,2}END NOUNIT                     # iOS 9–26 (ниже — рейтинг магазина)
    | (?<!\w)macOS[ \xa0-]?(?:1\d|2[0-6])(?:\.\d{1,2}){0,2}END NOUNIT                # macOS 10–26
    | (?<!\w)Android[ \xa0-]?(?:[5-9]|1\d|20)(?:\.\d)?END NOUNIT                     # Android 5–20 (ниже — рейтинг)
    | (?<!\w)Windows[ \xa0-]?(?:7|8\.1|8|10|11)END NOUNIT                            # Windows 7, 8, 8.1, 10, 11
    | (?<![\w.,])(?:32|64|128|192|256|384|512|1024|2048|3072|4096)[ \xa0-]?(?:bit|бит)\w*   # разрядность: 256-bit
    | (?<![\w/.,])24/7(?![\w/])                                                      # круглосуточно
    | (?<![\w.,])(?:OCTET\.){3}OCTET(?!\.?\d)(?!\w)                                  # IPv4: ровно четыре октета до 255
    | (?<![\w.,])(?:0?[1-9]|[12]\d|3[01])\.(?:0?[1-9]|1[0-2])\.(?:19|20)\d\d(?!\w)END    # дата дд.мм.гггг
    | (?<![\w.,-])(?:19|20)\d\d-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])(?![\w-])   # дата гггг-мм-дд
    | (?<!\w)(?:порт[аеуы]?|ports?)[ \xa0]+\d{1,5}END NOUNIT                         # номер порта
    | (?<![\w.,/])(?:10|\d(?:[.,]\d)?)[ \xa0]*/[ \xa0]*10(?![\d/])END                # оценка вердикта: 8.5/10, не больше 10
    | (?<!\w)(?-i:[48]K)(?!\w)(?![ \xa0]?(?:\+|UNIT))                                # разрешение 4K/8K, но не «4K серверов»
""".replace("NOUNIT", r"(?![ \xa0]?UNIT)").replace("UNIT", _UNIT).replace("END", r"(?![.,]?\d)")
    .replace("OCTET", _OCTET), re.I | re.X)


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


def _compose(text: str) -> str:
    """NFKC (лигатуры, полноширинные знаки, буква + отдельный диакритический знак -> одна буква) и без
    невидимых знаков. Делается над ВСЕМ текстом до разбиения на слова: «й» в виде «и» + знак краткости
    иначе рвёт слово пополам, и одинаковый текст не совпал бы ни одним шинглом."""
    return unicodedata.normalize("NFKC", text).translate(_DROP_INVISIBLE)


def _fold(text: str) -> str:
    """Регистр и «ё» = «е» (и конечная сигма — обычной: `lower()` различает их по месту в слове)."""
    return text.lower().replace("ё", "е").replace("ς", "σ")


def _copy_issues(text: str, sources: list[str]) -> list[str]:
    """Шинглы по SHINGLE слов текста против каждого источника; обе стороны приводятся ОДНИМ правилом
    (`_compose`, затем `_fold`) целиком и только потом режутся на слова. Один проход по тексту и по одному
    на источник (поиск в словаре) — страница в 2500 слов против пяти источников по 7000 считается за
    десятки миллисекунд. Источник короче шингла ничего не даёт: его range пуст."""
    if not sources:
        return []
    shown = _compose(text)
    norm = _fold(shown)
    spans = [m.span() for m in _WORD_RE.finditer(norm)]
    low = [norm[a:b] for a, b in spans]
    first = {}                                   # шингл текста -> позиция его первого вхождения
    for i in range(len(low) - SHINGLE + 1):
        first.setdefault(tuple(low[i:i + SHINGLE]), i)
    hits = set()
    for src in sources if first else ():
        sw = _WORD_RE.findall(_fold(_compose(src)))
        for i in range(len(sw) - SHINGLE + 1):
            pos = first.get(tuple(sw[i:i + SHINGLE]))
            if pos is not None:
                hits.add(pos)
    # кусок в 30 скопированных слов — это 19 шинглов со сдвигом на слово; цитируем фразы встык, без
    # перекрытий, и так, как они стоят на странице (с пунктуацией и регистром — оператор найдёт их
    # поиском). `lower()` длину почти никогда не меняет; если изменил (турецкая «İ») — позиции слов
    # к исходному регистру уже не приложить, цитируем приведённый текст.
    quoted = shown if len(shown) == len(norm) else norm
    out, free = [], 0
    for pos in sorted(hits):
        if pos < free:
            continue
        quote = " ".join(quoted[spans[pos][0]:spans[pos + SHINGLE - 1][1]].split())
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


def _number_tokens(text: str):
    """Числа текста по порядку: (запись как на странице, запись самого числа, степень множителя).
    Множитель — слово сразу за числом («6 тысяч» -> 3) или слитная K («5K» -> 3); без него степень 0."""
    for m in sorted((*NUM_RE.finditer(text), *_GLUED_K_RE.finditer(text)), key=lambda m: m.start(1)):
        raw = m.group(1)
        if m.re is _GLUED_K_RE:
            yield m.group(), raw, 3
            continue
        word = _MAGNITUDE_RE.match(text, m.end(1))
        if word:
            name = word.group(1).lower()
            yield f"{raw} {word.group(1)}", raw, next((p for prefix, p in _POWERS if name.startswith(prefix)), 3)
        else:
            yield raw, raw, 0


def _readings(raw: str, power: int) -> set[str]:
    """Сравнимые прочтения записи числа (вид `norm_number`). Запись с разделителем тысяч («6,000») —
    это тысячи; дробью она читается, только если дробь не целая: «5,500» — и 5500, и 5.5, а «6,000» —
    только 6000 (малые целые почти всегда есть среди разрешённых, и «6» пропускало бы любые «6,000»).
    С множителем прочтение одно — умноженное: «6 тысяч» — это 6000, голого «6» среди разрешённых мало."""
    value = norm_number(raw)
    out = {value}
    if _THOUSANDS_RE.fullmatch(raw):
        out = {re.sub(r"[.,]", "", raw)} | ({value} if "." in value else set())
    if power:
        out = {_scaled(r, power) for r in out} - {""}
    return out


def _source_magnitudes(sources: list[str]) -> set[str]:
    """Числа с множителем из текстов источников, уже умноженные: «около 6 тыс. серверов» -> «6000».
    В досье такое число лежит как «6», поэтому узаконить «6 тысяч» на странице может только сам текст."""
    out = set()
    for src in sources:
        for _, raw, power in _number_tokens(_IDENT_RE.sub(" ", src)):
            if power and len(raw) <= _NUM_MAX_LEN:
                out |= _readings(raw, power)
    return out


def _number_issues(text: str, allowed: set[str], sources: list[str]) -> list[str]:
    """Числа текста, которых нет среди разрешённых (`brief.allowed_numbers`: досье, факты вертикали,
    условия промокода) и умноженных чисел источников (`_source_magnitudes`), — «факт без источника».
    Обе стороны сравниваются в виде `norm_number`; у неоднозначной записи достаточно одного совпавшего
    прочтения (`_readings`). Не считаются фактами идентификаторы (`_IDENT_RE`), целые до `_SMALL_INT` и
    годы «прошлый — текущий — следующий» (UTC на момент вызова) — кроме записей с разделителем тысяч и
    чисел с множителем. В замечании числа стоят так, как написаны на странице."""
    year = datetime.now(timezone.utc).year
    known = None                                 # разрешённые + множители источников; считаем по требованию
    bad, seen = [], set()
    for shown, raw, power in _number_tokens(_IDENT_RE.sub(" ", text)):
        if len(raw) > _NUM_MAX_LEN:
            # поток цифр (зациклившаяся модель): не число и не повод для int() — на 3.11+ он бросает
            # ValueError уже на 4300 цифрах. Помечаем, показав начало.
            shown, ok = raw[:12] + "…", False
        else:
            value = norm_number(raw)
            ok = (not power and not _THOUSANDS_RE.fullmatch(raw) and value.isdigit()
                  and (int(value) <= _SMALL_INT or year - 1 <= int(value) <= year + 1))
            if not ok:
                if known is None:
                    known = allowed | _source_magnitudes(sources)
                ok = bool(_readings(raw, power) & known)
        shown = " ".join(shown.split())          # неразрывный пробел в замечании — обычным
        if ok or shown in seen:
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
    sources = _strings(sources)
    return [*_copy_issues(text, sources), *_brand_issues(text, brand), *_lang_issues(text, lang),
            *_volume_issues(text, kind), *_number_issues(text, set(_strings(allowed)), sources)]
