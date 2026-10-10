"""PageDoc: разбор ответа писателя (ограда, обрезка, схема) и рендер блоков в HTML (план Б, задача 2)."""
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
        {"h2": "Приватность", "paragraphs": ["Логи не ведутся — аудит 2025 года."]},
    ],
    "table": {"columns": ["Сервис", "Серверы"], "rows": [["Durev", "3200"], ["Другой", ""]]},
    "steps": [{"title": "Скачай приложение", "text": "С официального сайта.", "platform": "Windows"},
              {"title": "Войди", "text": "По коду из письма."}],
    "faq": [{"q": "Есть ли пробный период?", "a": "Да, 7 дней."}],
    "sources": ["https://example.com/a"],
}


def _doc(**over):
    return page_doc.PageDoc.model_validate({**copy.deepcopy(FULL), **over})


def _raw(data=FULL):
    return json.dumps(data, ensure_ascii=False)


def test_parse_strips_fence_and_prose():
    doc = page_doc.parse("Вот ответ:\n```json\n" + _raw() + "\n```\nготово")
    assert doc.meta.title == FULL["meta"]["title"] and len(doc.sections) == 2
    assert doc.verdict.score == 8.5 and doc.steps[1].platform is None


def test_parse_ignores_unknown_top_level_keys():
    assert page_doc.parse(_raw({**FULL, "мысли": "лишнее поле модели"})).faq[0].q.startswith("Есть")


def test_parse_reports_missing_field():
    data = {k: v for k, v in FULL.items() if k != "sections"}
    with pytest.raises(ValueError) as e:
        page_doc.parse(_raw(data))
    assert "sections" in str(e.value)


def test_parse_rejects_ragged_table():
    data = {**FULL, "table": {"columns": ["A", "B"], "rows": [["1", "2"], ["1", "2", "3"]]}}
    with pytest.raises(ValueError) as e:
        page_doc.parse(_raw(data))
    assert "в таблице строка 2" in str(e.value)


def test_parse_truncated_json_is_value_error():
    raw = _raw()
    with pytest.raises(ValueError) as e:
        page_doc.parse("```json\n" + raw[: len(raw) // 2])       # обрыв посреди sections
    assert str(e.value).startswith("ответ: Invalid JSON")
    with pytest.raises(ValueError):
        page_doc.parse(raw[:30])                                   # обрыв до первой «}»


def test_parse_without_json_object_is_value_error():
    for raw in ("", "Извини, не могу помочь.", "} наоборот {"):
        with pytest.raises(ValueError) as e:
            page_doc.parse(raw)
        assert "нет JSON-объекта" in str(e.value)


def test_parse_error_is_short_and_lists_at_most_five_fields():
    bad = {"meta": {"title": "x", "description": "y"}, "verdict": {"score": 99}, "sections": [],
           "table": {"columns": ["одна"], "rows": []}, "faq": [{"q": 1}] * 30}
    with pytest.raises(ValueError) as e:
        page_doc.parse(_raw(bad))
    msg = str(e.value)
    assert len(msg) <= 600 and "meta.title" in msg
    assert msg.count("; ") == 4 and "ещё" in msg                  # пять ошибок + хвост «и ещё N»


def test_render_all_blocks_survive_sanitizer():
    doc = _doc()
    ru = page_doc.render_blocks(doc, "review", "ru")
    en = page_doc.render_blocks(doc, "review", "en")
    for out in (ru, en):
        assert _sanitize(out) == out                               # санитайзер ничего не срезал
        assert "<h1" not in out
    order = [ru.index(x) for x in (
        "Быстрый и недорогой сервис.", f"<h3>{t('ru', 'lbl_pros')}</h3>", f"<h3>{t('ru', 'lbl_cons')}</h3>",
        "<table>", "<h2>Скорость</h2>", "<h3>Стриминг</h3>", "<h2>Приватность</h2>",
        f"<h2>{t('ru', 'lbl_steps')}</h2>", f"<h2>{t('ru', 'lbl_faq')}</h2>")]
    assert order == sorted(order)
    assert "<p><strong>Оценка: 8.5/10.</strong> Быстрый и недорогой сервис.</p>" in ru
    assert "<p><strong>Кому подойдёт:</strong> Тем, кто смотрит стриминг</p>" in ru
    assert "<p><strong>Кому не подойдёт:</strong> Тем, кому нужен выделенный IP</p>" in ru
    assert "<h3>Плюсы</h3><ul><li>Скорость до 900 Мбит/с</li><li>Цена &amp; скидки</li></ul>" in ru
    assert "<h3>Минусы</h3><ul><li>Мало серверов в Азии</li></ul>" in ru
    assert ("<table><thead><tr><th>Сервис</th><th>Серверы</th></tr></thead><tbody>"
            "<tr><td>Durev</td><td>3200</td></tr><tr><td>Другой</td><td></td></tr></tbody></table>") in ru
    assert ("<h2>Скорость</h2><p>Замеры \"утром\" и 'вечером'.</p><p>5 &gt; 3, а 2 &lt; 4.</p>"
            "<ul><li>WireGuard</li><li>OpenVPN</li></ul><h3>Стриминг</h3><p>Работает с каталогом США.</p>") in ru
    assert ("<h2>Пошагово</h2><ol><li><strong>Скачай приложение</strong> (Windows) — С официального сайта.</li>"
            "<li><strong>Войди</strong> — По коду из письма.</li></ol>") in ru
    assert "<h2>Вопросы и ответы</h2><h3>Есть ли пробный период?</h3><p>Да, 7 дней.</p>" in ru
    for label in ("Score: 8.5/10.", "Best for:", "Not for:", "<h3>Pros</h3>", "<h3>Cons</h3>",
                  "<h2>Step by step</h2>", "<h2>FAQ</h2>"):
        assert label in en
    assert "8/10." in page_doc.render_blocks(_doc(verdict={**FULL["verdict"], "score": 8}), "review", "ru")


def test_render_skips_absent_blocks():
    out = page_doc.render_blocks(_doc(verdict=None, pros=[], cons=[], table=None, steps=None, faq=[]),
                                 "howto", "ru")
    assert out.startswith("<h2>Скорость</h2>") and out.endswith("аудит 2025&nbsp;года.</p>")
    for tag in ("<table", "<ol", "<strong"):
        assert tag not in out
    for key in LABELS:
        assert t("ru", key) not in out


def test_render_escapes_model_html():
    sec = {"h2": "Заголовок <b>жирный</b>", "paragraphs": ["до <script>alert(1)</script> после"]}
    out = page_doc.render_blocks(_doc(sections=[sec, sec]), "review", "ru")
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in out and "<script" not in out
    assert "<h2>Заголовок &lt;b&gt;жирный&lt;/b&gt;</h2>" in out
    assert _sanitize(out) == out


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
