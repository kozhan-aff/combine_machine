"""PageDoc: разбор ответа писателя (ограда, проза, обрезка, схема) и рендер блоков в HTML (план Б, задача 2)."""
import copy
import json

import pytest

from app.services import page_doc
from app.services.content import _sanitize
from app.services.locales import TEXTS, t

LABELS = ("lbl_score", "lbl_for", "lbl_not_for", "lbl_pros", "lbl_cons", "lbl_steps", "lbl_faq")

FULL = {
    "meta": {"title": "Durev VPN: обзор и честный тест",
             "description": "Проверили скорость, приватность и цену Durev VPN — кому он подойдёт, а кому нет."},
    "verdict": {"score": 8.5, "summary": "Быстрый и недорогой сервис.",
                "for_whom": "Тем, кто смотрит стриминг", "not_for_whom": "Тем, кому нужен выделенный IP"},
    "pros": ["Скорость до 900 Мбит/с", "Цена & скидки"],
    "cons": ["Мало серверов в Азии"],
    "sections": [
        {"h2": "Скорость", "paragraphs": ["Замеры \"утром\" и 'вечером'.", "5 > 3, а 2 < 4."],
         "bullets": ["WireGuard", "OpenVPN"],
         "h3s": [{"h3": "Стриминг", "paragraphs": ["Работает с каталогом США."]}]},
        {"h2": "Приватность", "paragraphs": ["Логи не ведутся — аудит пройден."]},
    ],
    "table": {"columns": ["Сервис", "Серверы"], "rows": [["Durev", "3200"], ["Другой", ""]]},
    "steps": [{"title": "Скачай приложение", "text": "С официального сайта.", "platform": "Windows"},
              {"title": "Войди", "text": "По коду из письма."}],
    "faq": [{"q": "Есть ли пробный период?", "a": "Да, 7 дней."}],
    "sources": ["https://example.com/a"],
}


def _data(**over):
    return {**copy.deepcopy(FULL), **over}


def _doc(**over):
    return page_doc.PageDoc.model_validate(_data(**over))


def _raw(data=FULL):
    return json.dumps(data, ensure_ascii=False)


def _error(raw) -> str:
    with pytest.raises(ValueError) as e:
        page_doc.parse(raw)
    return str(e.value)


# --- parse: где в ответе документ ---

def test_parse_strips_fence_and_prose():
    doc = page_doc.parse("Вот ответ:\n```json\n" + _raw() + "\n```\nготово")
    assert doc.meta.title == FULL["meta"]["title"] and len(doc.sections) == 2
    assert doc.verdict.score == 8.5 and doc.steps[1].platform is None


def test_parse_ignores_unknown_top_level_keys():
    assert page_doc.parse(_raw({**FULL, "мысли": "лишнее поле модели"})).faq[0].q.startswith("Есть")


def test_parse_skips_braces_in_prose_before_json():
    raw = 'Схема ответа {meta, sections}, пример поля: {"h2": "…"}. Документ:\n' + _raw()
    assert page_doc.parse(raw).sections[0].h2 == "Скорость"


def test_parse_skips_think_block_before_fenced_json():
    raw = '<think>{"a":1}</think>\n```json\n' + _raw() + "\n```"
    assert page_doc.parse(raw).meta.title == FULL["meta"]["title"]


def test_parse_ignores_trailing_prose_with_brace():
    assert page_doc.parse(_raw() + "\nГотово :-} {конец}").cons == ["Мало серверов в Азии"]


def test_parse_takes_first_of_two_documents():
    second = _data(meta={**FULL["meta"], "title": "Второй вариант той же страницы"})
    assert page_doc.parse(_raw() + "\n\n" + _raw(second)).meta.title == FULL["meta"]["title"]


def test_parse_truncated_json_is_value_error():
    raw = _raw()
    # обрыв посреди sections: вложенные целые объекты (meta, verdict) за документ не принимаются
    assert _error("```json\n" + raw[: len(raw) // 2]).startswith("ответ: Invalid JSON")
    assert _error(raw[:30]).startswith("ответ: Invalid JSON")          # обрыв до первой «}»


def test_parse_without_json_object_is_value_error():
    for raw in ("", "   ", "Извини, не могу помочь.", "} скобка не та"):
        assert _error(raw) == "в ответе нет JSON-объекта"


def test_parse_non_string_is_value_error():
    for raw in (None, _raw().encode(), 5, FULL):
        assert _error(raw) == "ответ модели не строка"


def test_parse_object_of_wrong_shape_reports_missing_fields():
    msg = _error('Вот: {"title": "Обзор", "text": "…"}')
    assert "meta: Field required" in msg and "sections: Field required" in msg


# --- parse: схема ---

def test_parse_reports_missing_field():
    assert "sections" in _error(_raw({k: v for k, v in FULL.items() if k != "sections"}))


def test_parse_rejects_ragged_table():
    msg = _error(_raw(_data(table={"columns": ["A", "B"], "rows": [["1", "2"], ["1", "2", "3"]]})))
    assert "в таблице строка 2" in msg


def test_parse_error_is_short_and_lists_at_most_five_fields():
    bad = {"meta": {"title": "x", "description": "y"}, "verdict": {"score": 99}, "sections": [],
           "table": {"columns": ["одна"], "rows": []}, "faq": [{"q": "?"}] * 30}
    msg = _error(_raw(bad))
    assert len(msg) <= 600 and "meta.title" in msg
    assert msg.count("; ") == 4 and "ещё" in msg                  # пять ошибок + хвост «и ещё N»


def test_parse_coerces_numbers_to_strings():
    data = _data(table={"columns": ["Сервис", "Серверы", "Цена"], "rows": [["Durev", 3200, 2.49]]},
                 pros=[900], steps=[{"title": 1, "text": 2, "platform": 11}], faq=[{"q": 2026, "a": 7}])
    data["sections"][0]["bullets"] = [256, "WireGuard"]
    doc = page_doc.parse(_raw(data))
    assert doc.table.rows == [["Durev", "3200", "2.49"]] and doc.pros == ["900"]
    assert doc.sections[0].bullets == ["256", "WireGuard"]
    assert (doc.steps[0].title, doc.steps[0].text, doc.steps[0].platform) == ("1", "2", "11")
    assert (doc.faq[0].q, doc.faq[0].a) == ("2026", "7")
    assert "<td>3200</td><td>2.49</td>" in page_doc.render_blocks(doc, "comparison", "ru")


def test_parse_drops_empty_list_items_and_strips_strings():
    data = _data(pros=["", "  Плюс  ", " \n "], cons=["", "\t"], sources=["", "https://example.com/a"])
    data["sections"][0].update(h2="  Скорость ", paragraphs=["", "  Первый абзац. ", "   ", None],
                               bullets=[" ", ""])
    data["sections"][0]["h3s"][0]["paragraphs"] = ["Абзац под h3.", ""]
    data["steps"][0]["platform"] = "  "
    doc = page_doc.parse(_raw(data))
    assert doc.pros == ["Плюс"] and doc.cons == [] and doc.sources == ["https://example.com/a"]
    s = doc.sections[0]
    assert (s.h2, s.paragraphs, s.bullets) == ("Скорость", ["Первый абзац."], [])
    assert s.h3s[0].paragraphs == ["Абзац под h3."]
    assert doc.table.rows[1] == ["Другой", ""]                     # пустая ячейка таблицы — законна
    out = page_doc.render_blocks(doc, "review", "ru")
    assert "<h2>Скорость</h2>\n<p>Первый абзац.</p>\n<h3>Стриминг</h3>" in out      # <ul> без пунктов нет
    assert t("ru", "lbl_cons") not in out and "<p></p>" not in out and "<li></li>" not in out
    assert "<li><strong>Скачай приложение</strong> — С официального сайта.</li>" in out


def test_parse_rejects_blocks_left_without_text():
    def broken(path, value):
        data = _data()
        node = data
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = value
        return _error(_raw(data))

    assert "sections.0.paragraphs" in broken(("sections", 0, "paragraphs"), ["", "  "])
    assert "sections.0.h3s.0.paragraphs" in broken(("sections", 0, "h3s", 0, "paragraphs"), [" "])
    for path in (("sections", 1, "h2"), ("sections", 0, "h3s", 0, "h3"), ("faq", 0, "q"), ("faq", 0, "a"),
                 ("steps", 0, "title"), ("steps", 1, "text"), ("verdict", "summary"),
                 ("verdict", "for_whom"), ("verdict", "not_for_whom")):
        for empty in ("", "  \n"):
            assert ".".join(map(str, path)) in broken(path, empty)


# --- рендер ---

def test_render_all_blocks_survive_sanitizer():
    doc = _doc()
    ru = page_doc.render_blocks(doc, "review", "ru")
    en = page_doc.render_blocks(doc, "review", "en")
    for out in (ru, en):
        assert _sanitize(out) == out                               # санитайзер ничего не срезал
        assert "<h1" not in out
    assert ru == "\n".join([
        "<p><strong>Оценка: 8.5/10.</strong> Быстрый и недорогой сервис.</p>",
        "<p><strong>Кому подойдёт:</strong> Тем, кто смотрит стриминг</p>",
        "<p><strong>Кому не подойдёт:</strong> Тем, кому нужен выделенный IP</p>",
        "<h3>Плюсы</h3>", "<ul>", "<li>Скорость до 900 Мбит/с</li>", "<li>Цена &amp; скидки</li>", "</ul>",
        "<h3>Минусы</h3>", "<ul>", "<li>Мало серверов в Азии</li>", "</ul>",
        "<table>", "<thead><tr><th>Сервис</th><th>Серверы</th></tr></thead>", "<tbody>",
        "<tr><td>Durev</td><td>3200</td></tr>", "<tr><td>Другой</td><td></td></tr>", "</tbody>", "</table>",
        "<h2>Скорость</h2>", "<p>Замеры \"утром\" и 'вечером'.</p>", "<p>5 &gt; 3, а 2 &lt; 4.</p>",
        "<ul>", "<li>WireGuard</li>", "<li>OpenVPN</li>", "</ul>",
        "<h3>Стриминг</h3>", "<p>Работает с каталогом США.</p>",
        "<h2>Приватность</h2>", "<p>Логи не ведутся — аудит пройден.</p>",
        "<h2>Пошагово</h2>", "<ol>", "<li><strong>Скачай приложение</strong> (Windows) — С официального сайта.</li>",
        "<li><strong>Войди</strong> — По коду из письма.</li>", "</ol>",
        "<h2>Вопросы и ответы</h2>", "<h3>Есть ли пробный период?</h3>", "<p>Да, 7 дней.</p>",
    ])
    for label in ("Score: 8.5/10.", "Best for:", "Not for:", "<h3>Pros</h3>", "<h3>Cons</h3>",
                  "<h2>Step by step</h2>", "<h2>FAQ</h2>"):
        assert label in en
    assert "8/10." in page_doc.render_blocks(_doc(verdict={**FULL["verdict"], "score": 8}), "review", "ru")


def test_render_starts_every_block_on_its_own_line():
    out = page_doc.render_blocks(_doc(), "review", "ru")
    for tag in ("<h2>", "<h3>", "<p>", "<ul>", "<ol>", "<table>", "<li>", "<thead>", "<tbody>"):
        assert out.count(tag) and out.count(tag) == ("\n" + out).count("\n" + tag), tag
    assert not out.startswith("\n") and not out.endswith("\n") and "\n\n" not in out


def test_render_skips_absent_blocks():
    out = page_doc.render_blocks(_doc(verdict=None, pros=[], cons=[], table=None, steps=None, faq=[]),
                                 "howto", "ru")
    assert out.startswith("<h2>Скорость</h2>") and out.endswith("аудит пройден.</p>")
    for tag in ("<table", "<ol", "<strong"):
        assert tag not in out
    for key in LABELS:
        assert t("ru", key) not in out


def test_render_writes_non_breaking_space_as_entity():
    sec = {"h2": "Цена\xa0и\xa0тарифы", "paragraphs": ["2,49\xa0$ в\xa0месяц"]}
    out = page_doc.render_blocks(_doc(sections=[sec, sec]), "review", "fr")
    assert "<h2>Цена&nbsp;и&nbsp;тарифы</h2>\n<p>2,49&nbsp;$ в&nbsp;месяц</p>" in out
    assert "\xa0" not in out and _sanitize(out) == out
    assert "Цена и тарифы 2,49 $ в месяц" in page_doc.doc_text(_doc(sections=[sec, sec]))


def test_render_is_sanitizer_fixed_point_for_control_characters():
    doc = _doc(sections=[{"h2": "a\x00b\r\nc\rd\xa0e", "paragraphs": ["одна\r\nдве\rтри\x00.\xa0конец"]}] * 2,
               pros=["x\r\ny", "\x00n\xa0b\r"], faq=[{"q": "q\rq", "a": "a\x00a\r\na"}])
    out = page_doc.render_blocks(doc, "review", "ru")
    assert _sanitize(out) == out
    assert "<h2>ab\nc\nd&nbsp;e</h2>\n<p>одна\nдве\nтри.&nbsp;конец</p>" in out     # \r — перевод строки, не склейка
    assert "<li>x\ny</li>\n<li>n&nbsp;b</li>" in out and "<h3>q\nq</h3>\n<p>aa\na</p>" in out
    assert "\r" not in out and "\x00" not in out
    # и на всех символах до U+3000 разом: что бы ни прислала модель, body == _sanitize(body)
    chars = "".join(chr(c) for c in range(0x3000)) + "﻿�\U0001F600"
    wide = page_doc.render_blocks(_doc(pros=["до " + chars + " после"]), "review", "ru")
    assert _sanitize(wide) == wide


HOSTILE = ("<script>alert(1)</script><img src=x onerror=alert(1)><a href=\"javascript:alert(1)\">жми</a>"
           " \"кавычки\" 'апострофы' & амперсанд")


def test_render_escapes_model_html_in_every_field():
    sec = {"h2": HOSTILE, "paragraphs": [HOSTILE], "bullets": [HOSTILE],
           "h3s": [{"h3": HOSTILE, "paragraphs": [HOSTILE]}]}
    doc = _doc(verdict={"score": 7, "summary": HOSTILE, "for_whom": HOSTILE, "not_for_whom": HOSTILE},
               pros=[HOSTILE], cons=[HOSTILE], sections=[sec, sec],
               table={"columns": [HOSTILE, HOSTILE], "rows": [[HOSTILE, HOSTILE]]},
               steps=[{"title": HOSTILE, "text": HOSTILE, "platform": HOSTILE}],
               faq=[{"q": HOSTILE, "a": HOSTILE}])
    out = page_doc.render_blocks(doc, "review", "ru")
    for tag in ("<script", "<img", "<a ", "<a>", "onerror=alert(1)>"):
        assert tag not in out.replace("&lt;img src=x onerror=alert(1)&gt;", "")
    escaped = ("&lt;script&gt;alert(1)&lt;/script&gt;&lt;img src=x onerror=alert(1)&gt;"
               "&lt;a href=\"javascript:alert(1)\"&gt;жми&lt;/a&gt; \"кавычки\" 'апострофы' &amp; амперсанд")
    # verdict 3 + pros/cons 2 + шапка и ячейки таблицы 4 + секции 2×5 + шаг 3 + FAQ 2
    assert out.count(escaped) == 24
    assert _sanitize(out) == out


# --- текст для критика, константы, локали ---

def test_doc_text_contains_everything():
    text = page_doc.doc_text(_doc())
    for piece in ("Быстрый и недорогой сервис.", "Тем, кто смотрит стриминг", "Тем, кому нужен выделенный IP",
                  "Цена & скидки", "Мало серверов в Азии", "Сервис", "3200", "Скорость", "5 > 3, а 2 < 4.",
                  "OpenVPN", "Стриминг", "Работает с каталогом США.", "Скачай приложение", "Windows",
                  "По коду из письма.", "Есть ли пробный период?", "Да, 7 дней."):
        assert piece in text
    assert "\n" not in text and "  " not in text and "<" not in text.replace("2 < 4", "")
    assert page_doc.doc_text(_doc(verdict=None, table=None, steps=None, faq=[])).startswith("Скорость до 900")


def test_word_bounds_per_page_kind():
    assert page_doc.WORDS == {"review": (1500, 2200), "comparison": (1200, 1800), "howto": (900, 1400)}


def test_labels_exist_in_all_languages():
    assert len(TEXTS) == 8
    for lang, texts in TEXTS.items():
        for key in LABELS:
            assert texts.get(key, "").strip(), f"{lang}: нет {key}"
    assert [TEXTS["ru"][k] for k in LABELS] == ["Оценка", "Кому подойдёт", "Кому не подойдёт", "Плюсы",
                                                 "Минусы", "Пошагово", "Вопросы и ответы"]
    assert [TEXTS["en"][k] for k in LABELS] == ["Score", "Best for", "Not for", "Pros", "Cons",
                                                 "Step by step", "FAQ"]
