"""Извлечение досье из HTML конкурента: текст, заголовки, таблицы, FAQ, числа, CSS-токены (спека §4.3)."""
from pathlib import Path
from app.services import research_extract as rx

FX = Path(__file__).parent / "fixtures" / "research"
SAMPLE = """<html><head><title>Обзор X</title><link rel="stylesheet" href="/s/a.css"><link rel=stylesheet href="https://cdn/b.css">
<style>body{font-family:Inter,Arial;color:#222} .wrap{max-width:1120px} h1{color:#c0392b}</style></head>
<body><nav><h2>Меню</h2></nav><h1>X: обзор</h1><h2>Цена и тарифы</h2><p>От 5.99 $ в месяц при оплате за 2 года, 30 дней на возврат.</p>
<h3>Серверы</h3><p>5500 серверов в 60 странах.</p>
<table><tr><th>План</th><th>Цена</th></tr><tr><td>Год</td><td>4.99 $</td></tr></table>
<details><summary>Есть ли бесплатный тариф?</summary><p>Нет, только 30 дней возврата.</p></details>
<h3>Работает ли с Netflix?</h3><p>Да, в наших тестах — да.</p>
<div class="pros-cons"><ul class="pros"><li>быстро</li></ul></div><script>var casino = 1</script></body></html>"""


def test_visible_text_strips_scripts_and_keeps_title():
    t = rx.visible_text(SAMPLE)
    assert "casino" not in t and "Обзор X" in t and "5500 серверов" in t


def test_headings_skip_nav_and_keep_order():
    assert rx.headings(SAMPLE) == [["h1", "X: обзор"], ["h2", "Цена и тарифы"], ["h3", "Серверы"], ["h3", "Работает ли с Netflix?"]]


def test_tables_and_faq():
    assert rx.tables(SAMPLE) == [[["План", "Цена"], ["Год", "4.99 $"]]]
    f = rx.faq(SAMPLE)
    assert {"q": "Есть ли бесплатный тариф?", "a": "Нет, только 30 дней возврата."} in f
    assert {"q": "Работает ли с Netflix?", "a": "Да, в наших тестах — да."} in f


def test_numbers_with_context():
    n = rx.numbers(rx.visible_text(SAMPLE))
    vals = [x["value"] for x in n]
    assert "5.99" in vals and "30" in vals and "5500" in vals
    assert any("в месяц" in x["ctx"] for x in n if x["value"] == "5.99")


def test_stylesheet_links_and_css_tokens():
    assert rx.stylesheet_links(SAMPLE, "https://ex.com/r/") == ["https://ex.com/s/a.css", "https://cdn/b.css"]
    # живые формы ссылок (research-live-formats-2026-10): протокол-относительная и корневая
    assert rx.stylesheet_links('<link rel="stylesheet" href="//telegram.org/css/tw.css">', "https://t.me/x") == ["https://telegram.org/css/tw.css"]
    assert rx.stylesheet_links('<link rel="stylesheet" href="/_next/static/css/a.css">', "https://host.io/p/q") == ["https://host.io/_next/static/css/a.css"]
    tok = rx.css_tokens(SAMPLE, [".x{font-family:'Roboto Slab';max-width:960px;color:#c0392b;color:#c0392b;background:#fff}"])
    assert tok["fonts"][0] in ("Inter", "Roboto Slab") and "#c0392b" in tok["colors"] and "#fff" not in tok["colors"]
    assert tok["container_px"] in (960, 1120) and "pros_cons" in tok["components"] and "table" in tok["components"] and "faq" in tok["components"]


def test_broken_html_does_not_raise():
    bad = "<table><tr><td>a<h2>x</h3></details><summary>q?<nav></nav></nav></footer><<<>>"
    assert isinstance(rx.extract_all(bad, []), dict) and rx.headings("") == [] and rx.numbers("") == []


def test_extract_all_on_live_fixture():
    html = (FX / "competitor_1.html").read_text(encoding="utf-8", errors="replace")
    d = rx.extract_all(html, [(FX / "competitor_1.css").read_text(encoding="utf-8", errors="replace")])
    # живая фикстура — лента Telegram-канала: в ней нет ни одного h1–h3 (только <h5>), заголовков законно 0
    assert d["words"] >= 300 and isinstance(d["headings"], list) and isinstance(d["css_tokens"]["fonts"], list)
    assert set(d) == {"text", "words", "headings", "tables", "faq", "numbers", "css_tokens"}
