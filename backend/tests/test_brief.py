"""Бриф писателя из досье конкурентов: темы, факты с источником, пробелы, промпт и его потолок
(план Б, задача 3). Строки досье — SimpleNamespace, БД не нужна."""
from types import SimpleNamespace

from app.services import brief
from app.services.page_doc import WORDS


def row(kind="review", rank=1, heads=(), numbers=(), tables=None, faq=None, words=1200, final_url=None):
    """Строка досье в форме `SiteResearch`: heads — пары (tag, text), numbers — пары (value, ctx)."""
    domain = f"{kind}{rank}.example"
    return SimpleNamespace(
        kind=kind, rank=rank, url=f"https://{domain}/page", final_url=final_url, domain=domain, words=words,
        headings=[[tag, text] for tag, text in heads], tables=tables, faq=faq,
        numbers=[{"value": v, "ctx": c} for v, c in numbers], text="")


def _text(b, kind="review", promo=(None, None), vertical=None, country="Германия"):
    return brief.brief_text(b, brand="Durev VPN", kind=kind, title="Durev VPN: обзор", lang_name="немецкий",
                            country=country, promo=promo, vertical=vertical)


def test_topics_threshold_scales():
    # 5 источников: порог ceil(0.6 * 5) = 3. «Цена» — у трёх (в разном регистре и с пунктуацией), «Скорость» — у двух
    rows = [row(rank=1, heads=[("h1", "Обзор"), ("h2", "Цена"), ("h3", "Скорость")]),
            row(rank=2, heads=[("h2", "  ЦЕНА: "), ("h2", "Скорость")]),
            row(rank=3, heads=[("h3", "Цена?"), ("h2", "цена")]),      # повтор внутри источника не считается дважды
            row(rank=4, heads=[("h2", "Серверы")]),
            row(rank=5, heads=[("h2", "Поддержка")])]
    topics = brief.build_brief(rows, "review")["topics"]
    assert topics == [{"title": "цена", "count": 3}]

    # 2 источника: порог max(2, ceil(1.2)) = 2 — тема у обоих проходит, у одного нет
    two = brief.build_brief(rows[:2], "review")["topics"]
    assert {"title": "скорость", "count": 2} in two and {"title": "цена", "count": 2} in two
    assert "обзор" not in [x["title"] for x in two]          # h1 в темы не идёт, и он у одного


def test_topics_sorted_by_count_and_capped():
    heads = [("h2", f"Тема {i}") for i in range(30)]
    rows = [row(rank=1, heads=heads + [("h2", "Общая")]), row(rank=2, heads=heads + [("h2", "Общая")]),
            row(rank=3, heads=[("h2", "Общая")])]
    topics = brief.build_brief(rows, "review")["topics"]
    assert len(topics) == 20 and topics[0] == {"title": "общая", "count": 3}
    assert [x["count"] for x in topics] == sorted((x["count"] for x in topics), reverse=True)


def test_kind_without_sources_borrows_market():
    rows = [row("market", 1, heads=[("h2", "Лучшие VPN")]), row("market", 2, heads=[("h2", "Лучшие VPN")]),
            row("howto", 1, heads=[("h2", "Установка")])]
    b = brief.build_brief(rows, "review")
    assert [s["domain"] for s in b["sources"]] == ["market1.example", "market2.example"]
    assert [s["n"] for s in b["sources"]] == [1, 2]
    assert b["borrowed"] is True and b["gaps"] == []
    assert b["topics"] == [{"title": "лучшие vpn", "count": 2}]


def test_single_own_source_is_kept_first_and_market_added():
    rows = [row("market", 1), row("review", 1), row("market", 2)]
    b = brief.build_brief(rows, "review")
    assert [(s["n"], s["domain"]) for s in b["sources"]] == [
        (1, "review1.example"), (2, "market1.example"), (3, "market2.example")]
    assert b["borrowed"] is True


def test_enough_own_sources_do_not_borrow():
    rows = [row("review", 1), row("review", 2), row("market", 1)]
    b = brief.build_brief(rows, "review")
    assert [s["domain"] for s in b["sources"]] == ["review1.example", "review2.example"]
    assert b["borrowed"] is False


def test_single_source_has_outline_not_topics():
    heads = [("h1", "Обзор")] + [("h2", f"Раздел {i}") for i in range(30)]
    b = brief.build_brief([row(rank=1, heads=heads)], "review")
    assert b["topics"] == []
    assert b["outlines"] == [{"n": 1, "headings": ["Обзор"] + [f"Раздел {i}" for i in range(24)]}]   # первые 25


def test_sources_prefer_final_url():
    b = brief.build_brief([row(rank=1, final_url="https://final.example/x", words=900), row(rank=2)], "review")
    assert b["sources"][0] == {"n": 1, "url": "https://final.example/x", "domain": "review1.example", "words": 900}
    assert b["sources"][1]["url"] == "https://review2.example/page"


def test_facts_carry_source_number_and_cap():
    many = [(str(i), f"контекст {i}") for i in range(20)]
    rows = [row(rank=1, numbers=[("30", "30 дней возврата"), ("30", "30 стран"), ("5.99", "от 5.99 $")]),
            row(rank=2, numbers=[("30", "гарантия 30 дней")] + many)]
    facts = brief.build_brief(rows, "review")["facts"]
    # дубль (value, n) схлопнут, то же значение из другого источника — отдельный факт со своим номером
    assert facts[:3] == [{"value": "30", "ctx": "30 дней возврата", "n": 1}, {"value": "5.99", "ctx": "от 5.99 $", "n": 1},
                         {"value": "30", "ctx": "гарантия 30 дней", "n": 2}]
    assert len([f for f in facts if f["n"] == 2]) == 15           # не более 15 на источник

    five = [row(rank=r, numbers=[(f"{r}{i:02d}", "ctx") for i in range(20)]) for r in range(1, 6)]
    assert len(brief.build_brief(five, "review")["facts"]) == 60    # не более 60 всего


def test_tables_and_questions():
    t = [["План", "Цена", "Срок"], ["Месяц", "12.99", "1"], ["Год", "4.99", "12"]]
    rows = [row(rank=1, tables=[t, []], faq=[{"q": "Есть ли пробный период?", "a": "Да"}, {"q": "Какая цена?", "a": "5"}]),
            row(rank=2, tables=[t] * 8, faq=[{"q": " есть ли ПРОБНЫЙ период ", "a": "Нет"}]
                + [{"q": f"Вопрос {i}?", "a": "x"} for i in range(20)])]
    b = brief.build_brief(rows, "review")
    assert b["tables"][0] == {"n": 1, "columns": ["План", "Цена", "Срок"], "rows": 2}
    assert len(b["tables"]) == 6 and b["tables"][1]["n"] == 2
    assert b["questions"][:3] == ["Есть ли пробный период?", "Какая цена?", "Вопрос 0?"]   # дубль по норм. тексту снят
    assert len(b["questions"]) == 15


def test_none_fields_are_empty():
    r = row(rank=1)
    r.headings = r.tables = r.faq = r.numbers = None
    b = brief.build_brief([r, row(rank=2)], "review")
    assert (b["topics"], b["outlines"], b["facts"], b["tables"], b["questions"], b["gaps"]) == ([], [], [], [], [], [])
    assert brief.allowed_numbers([r]) == set()
    assert brief.build_brief([], "review") == {"sources": [], "topics": [], "outlines": [], "facts": [], "tables": [],
                                               "questions": [], "gaps": [], "borrowed": False}


def test_gaps_are_market_only_headings():
    rows = [row("review", 1, heads=[("h2", "Цена"), ("h3", "Возврат денег")]),
            row("review", 2, heads=[("h2", "Скорость")]),
            row("market", 1, heads=[("h2", "Цена!"), ("h2", "Роутеры"), ("h3", "Мелочь"), ("h2", "возврат денег")]),
            row("market", 2, heads=[("h2", "РОУТЕРЫ"), ("h2", "Торренты")])]
    b = brief.build_brief(rows, "review")
    # «цена» и «возврат денег» есть у источников типа (в т.ч. как h3), h3 рынка пробелом не считается, дубль снят
    assert b["gaps"] == ["роутеры", "торренты"] and b["borrowed"] is False
    assert len(b["sources"]) == 2                              # рынок источником не стал

    wide = rows[:2] + [row("market", 1, heads=[("h2", f"Тема {i}") for i in range(15)])]
    assert len(brief.build_brief(wide, "review")["gaps"]) == 10


def test_brief_text_has_promo_terms_and_word_bounds():
    rows = [row(rank=1, heads=[("h2", "Цена")], numbers=[("5.99", "от 5.99 $ в месяц")],
                tables=[[["План", "Цена"], ["Год", "4.99"]]], faq=[{"q": "Есть ли пробный период?", "a": "Да"}]),
            row(rank=2, heads=[("h2", "Цена")]),
            row("market", 1, heads=[("h2", "Роутеры")])]
    txt = _text(brief.build_brief(rows, "review"), promo=("SAVE10", "скидка 10% на годовой тариф"),
                vertical="Бренд: Durev VPN.\n- Серверы: 3200 в 60 странах.")
    assert "SAVE10" in txt and "скидка 10% на годовой тариф" in txt and "единственный бонус" in txt
    lo, hi = WORDS["review"]
    assert str(lo) in txt and str(hi) in txt
    assert "Durev VPN" in txt and "немецкий" in txt and "Германия" in txt and "Durev VPN: обзор" in txt
    assert "Серверы: 3200 в 60 странах" in txt
    assert "[1] https://review1.example/page" in txt and "[2] https://review2.example/page" in txt
    assert "5.99 — от 5.99 $ в месяц — [1]" in txt             # значение — контекст — [n]
    assert "План | Цена" in txt and "Есть ли пробный период?" in txt and "роутеры" in txt
    for header in ("Задача", "Оффер", "Факты бренда", "Источники", "Общие темы", "Структуры конкурентов",
                   "Факты с источником", "Таблицы конкурентов", "Вопросы", "Пробелы рынка", "Объём"):
        assert f"## {header}" in txt, header
    assert _text(brief.build_brief(rows, "howto"), kind="howto").count(str(WORDS["howto"][0])) == 1


def test_brief_text_without_promo_has_no_promo_section():
    b = brief.build_brief([row(rank=1), row(rank=2)], "review")
    for promo in ((None, None), ("", ""), ("SAVE10", None)):   # код без условий описать нечем — как и в старом промпте
        txt = _text(b, promo=promo, country=None)
        assert "ромокод" not in txt and "бонус" not in txt and "SAVE10" not in txt
        assert "Гео" not in txt
    # разделы без данных не печатаются пустыми; «Факты бренда» — исключение: об отсутствии данных сказано явно
    for header in ("Общие темы", "Структуры конкурентов", "Факты с источником", "Таблицы конкурентов", "Вопросы",
                   "Пробелы рынка"):
        assert f"## {header}" not in txt
    assert "## Источники" in txt and "## Источники" not in _text(brief.build_brief([], "review"))
    assert "## Факты бренда" in txt and "нет проверенных данных — не выдумывай характеристики" in txt


def test_brief_text_marks_borrowed_sources():
    borrowed = _text(brief.build_brief([row("market", 1), row("market", 2)], "review"))
    own = _text(brief.build_brief([row(rank=1), row(rank=2)], "review"))
    assert "рыночн" in borrowed and "рыночн" not in own


def _heavy(ctx_len: int):
    """5 источников: по 25 длинных заголовков и по 20 чисел с контекстом `ctx_len` символов."""
    return [row(rank=r, heads=[("h2", f"Заголовок {r}-{i} " + "слово " * 12) for i in range(25)],
                numbers=[(f"{r}{i:02d}", f"ф{r}{i:02d} " + "я" * ctx_len) for i in range(20)]) for r in range(1, 6)]


def test_brief_text_is_capped():
    b = brief.build_brief(_heavy(250), "review")
    assert len(b["facts"]) == 60 and len(b["outlines"]) == 5
    assert len(brief.brief_text(dict(b, outlines=[], facts=[]), brand="B", kind="review", title="T", lang_name="русский",
                                country=None, promo=(None, None), vertical=None)) < 2000
    txt = _text(b)
    assert len(txt) <= brief.MAX_CHARS == 24_000
    # сначала режутся структуры (с последнего источника): все 60 фактов на месте, первая структура цела
    assert all(f"ф{f['value']} " in txt for f in b["facts"])
    assert "Заголовок 1-24" in txt and "Заголовок 5-0" not in txt
    assert "## Объём" in txt and "[5] https://review5.example/page" in txt
    assert len(b["outlines"]) == 5 and len(b["facts"]) == 60    # бриф-словарь обрезкой не испорчен

    # структур не осталось, а всё ещё длинно — режутся факты с конца
    big = brief.build_brief(_heavy(700), "review")
    txt = _text(big)
    assert len(txt) <= brief.MAX_CHARS
    assert "Структуры конкурентов" not in txt and "## Факты с источником" in txt
    assert f"ф{big['facts'][0]['value']} " in txt and f"ф{big['facts'][-1]['value']} " not in txt
    assert "## Объём" in txt


def test_norm_number_and_allowed_numbers():
    assert brief.norm_number("1 500") == "1500"
    assert brief.norm_number("1 500") == brief.norm_number("1 500") == "1500"
    assert brief.norm_number("1,5") == "1.5" and brief.norm_number("1.50") == "1.5"
    assert brief.norm_number("30.0") == "30" and brief.norm_number("100") == "100" and brief.norm_number("1500") == "1500"

    rows = [row(rank=1, numbers=[("5.990", "от 5.990 $"), ("5500", "5500 серверов")]),
            row("market", 1, numbers=[("7", "7 устройств")])]      # числа берутся со всех строк, не только источников типа
    vertical = "- Серверы: 1 500 в 60 странах.\n- Цена от: 1,5 $; возврат в течение 30 дней."
    got = brief.allowed_numbers(rows, vertical, None, "скидка 10% и 3 000 бонусов")
    assert got == {"5.99", "5500", "7", "1500", "60", "1.5", "30", "10", "3000"}
