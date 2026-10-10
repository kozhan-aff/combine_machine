"""Бриф писателя из досье конкурентов (спека 2026-10-10 §6.1) — код, не модель. Чистые функции над
строками `site_research`: без сети, БД и LLM.

`build_brief` собирает структуру (источники, общие темы, факты с номером источника, таблицы, вопросы,
пробелы рынка), `brief_text` печатает её пользовательским промптом писателя, `allowed_numbers` — числа,
которые писателю разрешено называть (ими критик ловит «факт без источника»).
"""
import string

from app.services.page_doc import WORDS
from app.services.research_extract import _NUM_RE

MAX_CHARS = 24_000            # потолок брифа; сверх него режутся структуры конкурентов, затем факты
MIN_OWN = 2                   # меньше стольких источников своего типа — пишем по рыночным
TOPICS_MAX, OUTLINE_MAX = 20, 25
FACTS_PER_SOURCE, FACTS_MAX = 15, 60
TABLES_MAX, COLUMNS_MAX, CELL_MAX = 6, 8, 60
QUESTIONS_MAX, QUESTION_LEN = 15, 300
GAPS_MAX = 10

_EDGE = string.punctuation + string.whitespace + "«»„“”‘’—–…·•"
_KIND_RU = {"review": "обзор", "comparison": "сравнение", "howto": "пошаговая инструкция"}


def _flat(s) -> str:
    return " ".join(str(s or "").split())


def _norm(s) -> str:
    """Ключ сравнения заголовков и вопросов: регистр, пробелы и пунктуация по краям не различают."""
    return _flat(s).lower().strip(_EDGE)


def _heads(row, tags: tuple) -> list[str]:
    """Тексты заголовков строки досье с тегом из `tags`. JSON из БД читаем защитно: None и кривые
    записи — пусто, а не падение."""
    return [_flat(h[1]) for h in row.headings or []
            if isinstance(h, (list, tuple)) and len(h) == 2 and h[0] in tags and _flat(h[1])]


def norm_number(s: str) -> str:
    """Число в сравнимом виде: без пробелов любого рода (обычный, U+00A0, U+202F), десятичная запятая ->
    точка, хвостовые нули дроби сняты. `"1 500"` -> `"1500"`, `"1,5"`/`"1.50"` -> `"1.5"`, `"30.0"` -> `"30"`."""
    v = "".join(str(s).split()).replace(",", ".")
    return v.rstrip("0").rstrip(".") if "." in v else v


def build_brief(rows: list, kind: str) -> dict:
    """Строки досье сайта -> бриф страницы типа `kind`. Нумерация источников `n` сквозная, с 1, в порядке
    строк (досье отдаёт их по позиции в выдаче)."""
    own = [r for r in rows if r.kind == kind]
    market = [r for r in rows if r.kind == "market"]
    # тип без своих источников (живой случай малого бренда) пишется по рынку; занимать нечего — не заняли
    borrowed = len(own) < MIN_OWN and bool(market)
    srcs = own + market if borrowed else own

    sources = [{"n": n, "url": r.final_url or r.url, "domain": r.domain, "words": r.words or 0}
               for n, r in enumerate(srcs, 1)]

    # тема = заголовок h2/h3, встреченный у 60% источников, но не меньше чем у двух: один источник тем не
    # даёт (счётчик не выше 1) — тогда структуру несут outlines. Внутри источника повтор не считается.
    counts: dict[str, int] = {}
    for r in srcs:
        for key in dict.fromkeys(_norm(h) for h in _heads(r, ("h2", "h3"))):
            if key:
                counts[key] = counts.get(key, 0) + 1
    need = max(2, -(-3 * len(srcs) // 5))          # ceil(0.6 * N) в целых, без плавающей точки
    topics = [{"title": k, "count": c}             # сортировка устойчива: при равном count — порядок появления
              for k, c in sorted(counts.items(), key=lambda kv: -kv[1]) if c >= need][:TOPICS_MAX]

    outlines = []
    for n, r in enumerate(srcs, 1):
        heads = _heads(r, ("h1", "h2", "h3"))[:OUTLINE_MAX]
        if heads:
            outlines.append({"n": n, "headings": heads})

    facts = []
    for n, r in enumerate(srcs, 1):
        seen = set()                               # дубль — то же значение у того же источника
        for x in r.numbers or []:
            if len(facts) >= FACTS_MAX or len(seen) >= FACTS_PER_SOURCE:
                break
            value = _flat(x.get("value")) if isinstance(x, dict) else ""
            if value and value not in seen:
                seen.add(value)
                facts.append({"value": value, "ctx": _flat(x.get("ctx")), "n": n})

    # от таблицы берём структуру, не данные: колонки и число строк. Ячейка шапки режется — вёрстка
    # таблицами кладёт в «ячейку» целые абзацы.
    tables = []
    for n, r in enumerate(srcs, 1):
        for tbl in r.tables or []:
            head = tbl[0] if isinstance(tbl, list) and tbl and isinstance(tbl[0], list) else []
            columns = [_flat(c)[:CELL_MAX] for c in head][:COLUMNS_MAX]
            if any(columns) and len(tables) < TABLES_MAX:
                tables.append({"n": n, "columns": columns, "rows": len(tbl) - 1})

    questions, seen_q = [], set()
    for r in srcs:
        for x in r.faq or []:
            q = _flat(x.get("q"))[:QUESTION_LEN] if isinstance(x, dict) else ""
            key = _norm(q)
            if key and key not in seen_q and len(questions) < QUESTIONS_MAX:
                seen_q.add(key)
                questions.append(q)

    # пробел = h2 общей выдачи, которого нет ни в одном заголовке источников типа. Когда рынок сам стал
    # источником, сравнивать его не с чем.
    gaps = []
    if not borrowed:
        have = {_norm(h) for r in own for h in _heads(r, ("h1", "h2", "h3"))}
        for r in market:
            for key in map(_norm, _heads(r, ("h2",))):
                if key and key not in have and key not in gaps:
                    gaps.append(key)
        gaps = gaps[:GAPS_MAX]

    return {"sources": sources, "topics": topics, "outlines": outlines, "facts": facts, "tables": tables,
            "questions": questions, "gaps": gaps, "borrowed": borrowed}


def _render(brief: dict, outlines: list, facts: list, *, brand: str, kind: str, title: str, lang_name: str,
            country: str | None, promo: tuple, vertical: str | None) -> str:
    """Текст брифа с заданными структурами и фактами (их режет `brief_text`). Раздел без данных не
    печатается; исключение — «Факты бренда»: об отсутствии проверенных данных модели говорим явно."""
    n_src = len(brief["sources"])
    code, terms = promo
    offer = [f"Бренд: {brand}"] + ([f"Гео: {country}"] if country else []) + [f"Язык текста: {lang_name}"]
    if code and terms:      # код без условий описать нечем — как и в старом промпте (content._page_prompt)
        offer += [f"Промокод: {code}",
                  "Условия промокода — единственный бонус, о котором можно писать (переведи на язык текста, "
                  f"других скидок и условий не выдумывай): {terms}"]
    sections = [
        ("Задача", [
            f"Напиши страницу «{title}». Тип страницы: {_KIND_RU.get(kind, kind)}; бренд — {brand}.",
            "Ниже разбор страниц конкурентов из поисковой выдачи: что они покрывают и какие факты приводят. "
            "Раскрой общие темы полнее и конкретнее конкурентов и своими словами — их текст не копируй.",
            "Числа бери только из фактов этого брифа; URL источника [n] каждого использованного факта "
            "перечисли в sources."]),
        ("Оффер", offer),
        ("Факты бренда", [vertical.strip() if vertical and vertical.strip() else
                          "По бренду нет проверенных данных — не выдумывай характеристики (серверы, страны, "
                          "цены, протоколы): опирайся только на факты из источников этого брифа."]),
        ("Источники", ([] if not n_src else
                       (["Источников по этому типу страницы меньше двух — добавлены страницы общего рыночного "
                         "запроса: темы и факты бери из них, структуру строй под свой тип страницы."]
                        if brief["borrowed"] else [])
                       + [f"[{s['n']}] {s['url']}" for s in brief["sources"]])),
        ("Общие темы", [f"- {x['title']} — у {x['count']} из {n_src} источников" for x in brief["topics"]]),
        ("Структуры конкурентов", [f"[{o['n']}] " + "; ".join(o["headings"]) for o in outlines]),
        ("Факты с источником (значение — контекст — [n])",
         [f"- {f['value']} — {f['ctx']} — [{f['n']}]" for f in facts]),
        ("Таблицы конкурентов", [f"[{x['n']}] колонки: {' | '.join(x['columns'])}; строк: {x['rows']}"
                                 for x in brief["tables"]]),
        ("Вопросы", [f"- {q}" for q in brief["questions"]]),
        ("Пробелы рынка", ([] if not brief["gaps"] else
                           ["Темы общей выдачи по нише, которых нет у конкурентов по этому типу страницы, — "
                            "раскрой уместные:"] + [f"- {g}" for g in brief["gaps"]])),
        ("Объём", [f"От {WORDS[kind][0]} до {WORDS[kind][1]} слов по сумме текста страницы."]
         if kind in WORDS else []),
    ]
    return "\n\n".join(f"## {name}\n" + "\n".join(lines) for name, lines in sections if lines)


def brief_text(brief: dict, *, brand: str, kind: str, title: str, lang_name: str, country: str | None,
               promo: tuple[str | None, str | None], vertical: str | None) -> str:
    """Пользовательский промпт писателя. Не длиннее MAX_CHARS: сверх потолка сначала уходят структуры
    конкурентов (с последнего источника), затем факты (с конца); словарь `brief` не меняется."""
    outlines, facts = list(brief["outlines"]), list(brief["facts"])
    while True:
        text = _render(brief, outlines, facts, brand=brand, kind=kind, title=title, lang_name=lang_name,
                       country=country, promo=promo, vertical=vertical)
        if len(text) <= MAX_CHARS or not (outlines or facts):
            return text[:MAX_CHARS]     # остальные разделы ограничены сборкой; срез — страховка потолка
        (outlines or facts).pop()


def allowed_numbers(rows: list, *extra_texts: str | None) -> set[str]:
    """Числа, которые писатель вправе назвать: `numbers` ВСЕХ строк досье плюс числа из доп. текстов
    (блок фактов вертикали, условия промокода). Всё в виде `norm_number`."""
    out = {norm_number(x["value"]) for r in rows for x in r.numbers or [] if isinstance(x, dict) and x.get("value")}
    for text in extra_texts:
        out.update(norm_number(m) for m in _NUM_RE.findall(text or ""))
    out.discard("")
    return out
