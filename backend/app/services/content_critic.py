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
from datetime import datetime, timezone

from app.services import research_extract
from app.services.brief import norm_number
from app.services.locales import norm_lang
from app.services.page_doc import WORDS
from app.services.research_extract import _NUM_RE

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

SHINGLE = 12                  # слов подряд, совпавших с источником, — уже копия, а не общий оборот речи
_COPY_MAX = 3                 # столько скопированных фраз цитируем: дальше оператору и так всё ясно
_NUMS_MAX = 10                # столько чисел без источника перечисляем в замечании
_SMALL_INT = 12               # целые до этого — счёт шагов, оценки, пункты списка, а не факты
_RU_MIN, _LATIN_MAX = 0.6, 0.2   # доля кириллицы среди букв: русскому тексту не меньше, прочим не больше
_WORD_RE = re.compile(r"\w+")


def visible_text(body_html: str) -> str:
    """HTML тела страницы -> текст, который видит читатель: теги сняты (блочные слов не склеивают),
    сущности раскрыты, пробелы схлопнуты. Тот же разбор, каким досье читает конкурентов, — страница и
    её источники сравниваются в одном виде."""
    return research_extract.visible_text(body_html)


def _copy_issues(text: str, sources: list[str]) -> list[str]:
    """Шинглы по SHINGLE слов текста против каждого источника. Один проход по тексту и по одному на
    источник (поиск в словаре) — страница в 2500 слов против пяти источников по 7000 считается за
    миллисекунды. Источник короче шингла ничего не даёт: его range пуст."""
    spans = [m.span() for m in _WORD_RE.finditer(text)]
    low = [text[a:b].lower() for a, b in spans]
    first = {}                                   # шингл текста -> позиция его первого вхождения
    for i in range(len(low) - SHINGLE + 1):
        first.setdefault(tuple(low[i:i + SHINGLE]), i)
    hits = set()
    for src in sources if first else ():
        sw = [w.lower() for w in _WORD_RE.findall(src or "")]
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
        out.append(f"копирование источника: «{text[spans[pos][0]:spans[pos + SHINGLE - 1][1]]}»")
        free = pos + SHINGLE
        if len(out) == _COPY_MAX:
            break
    return out


def _brand_issues(text: str, brand: str | None) -> list[str]:
    """Бренд оффера обязан быть назван в тексте (регистр и лишние пробелы не различаем). Раскрытие
    партнёрства здесь НЕ проверяется: его ставит шаблон (`render_html`), см. S6-15 у `_DISCLOSURE_RE`."""
    name = " ".join((brand or "").split())
    if name and name.casefold() not in " ".join(text.split()).casefold():
        return [f"в тексте нет бренда {name}"]
    return []


def _lang_issues(text: str, lang: str) -> list[str]:
    """Язык по доле кириллицы среди букв: `ru` — не меньше `_RU_MIN`, остальные языки проекта (все на
    латинице) — не больше `_LATIN_MAX`. Латинский бренд и названия протоколов русскому тексту не мешают."""
    # ponytail: только алфавит; en от de так не отличить — это ловит чек-лист модели.
    letters = [c for c in text if c.isalpha()]
    if not letters:
        # пустая страница не должна сойти за чистую только потому, что в ней нечего мерить
        return ["язык: в тексте нет букв"]
    share = sum("\u0400" <= c <= "\u04ff" for c in letters) / len(letters)
    code = norm_lang(lang)
    if code == "ru":
        if share < _RU_MIN:
            return [f"язык: кириллицы {share:.0%} букв, для ru нужно не меньше {_RU_MIN:.0%}"]
    elif share > _LATIN_MAX:
        return [f"язык: кириллицы {share:.0%} букв, для {code} допустимо не больше {_LATIN_MAX:.0%}"]
    return []


def _volume_issues(text: str, kind: str | None) -> list[str]:
    """Число слов против границ типа страницы (`page_doc.WORDS`). Тип без границ или неизвестный путь
    (`kind is None`) — проверка пропускается: мерить не с чем."""
    if kind not in WORDS:
        return []
    lo, hi = WORDS[kind]
    n = len(_WORD_RE.findall(text))
    return [] if lo <= n <= hi else [f"объём {n} слов, нужно {lo}–{hi}"]


def _number_issues(text: str, allowed: set[str]) -> list[str]:
    """Числа текста, которых нет среди разрешённых (`brief.allowed_numbers`: досье, факты вертикали,
    условия промокода), — «факт без источника». Обе стороны сравниваются в виде `norm_number`. Не
    считаются фактами целые до `_SMALL_INT` и годы «прошлый — текущий — следующий» (UTC на момент вызова)."""
    year = datetime.now(timezone.utc).year
    bad = []
    for raw in _NUM_RE.findall(text):
        v = norm_number(raw)
        if v in allowed or v in bad:
            continue
        if v.isdigit() and (int(v) <= _SMALL_INT or year - 1 <= int(v) <= year + 1):
            continue
        bad.append(v)
    if not bad:
        return []
    more = f" и ещё {len(bad) - _NUMS_MAX}" if len(bad) > _NUMS_MAX else ""
    return [f"числа без источника: {', '.join(bad[:_NUMS_MAX])}{more}"]


def code_checks(*, text: str, kind: str | None, lang: str, brand: str | None,
                sources: list[str], allowed: set[str]) -> list[str]:
    """Все проверки кодом над видимым текстом страницы (`visible_text`). `sources` — тексты источников
    досье, `allowed` — `brief.allowed_numbers`. Список замечаний; пусто = код претензий не имеет."""
    text = text or ""
    return [*_copy_issues(text, sources), *_brand_issues(text, brand), *_lang_issues(text, lang),
            *_volume_issues(text, kind), *_number_issues(text, allowed)]
