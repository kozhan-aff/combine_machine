"""W6: доля спам-анкоров (по refdomains) и пик трафика. Живой спам-профиль 2026-10-01."""
import json
import pathlib

from app.services.link_signals import peak_traffic, spam_anchor_ratio

FX = pathlib.Path(__file__).parent / "fixtures" / "v2"


def test_live_spam_profile_is_fully_spam():
    anchors = json.loads((FX / "ahrefs_anchors_spam.json").read_text())["anchors"]
    assert spam_anchor_ratio(anchors, "pharmaindustrie.com") == 1.0


def test_ratio_weighted_by_refdomains_word_boundaries_and_scripts():
    anchors = [{"anchor": "Seoul travel guide", "refdomains": 60, "is_spam": False},   # «seo» в «Seoul» — не спам
               {"anchor": "best casino bonus", "refdomains": 20, "is_spam": False},
               {"anchor": "официальный сайт", "refdomains": 20, "is_spam": False}]  # кириллица на латинском домене
    assert spam_anchor_ratio(anchors, "travel.com") == 0.4
    assert spam_anchor_ratio(anchors[2:], "xn--80ak6aa92e.com") == 0.0          # IDN-домен: кириллица — норма
    assert spam_anchor_ratio([], "a.com") is None
    assert spam_anchor_ratio([{"anchor": "x", "refdomains": 0}], "a.com") is None


def test_plural_stop_words():
    """4.3: множественное число и производные — тоже спам."""
    for text in ("online casinos list", "porno videos", "free slots", "payday loans"):
        assert spam_anchor_ratio([{"anchor": text, "refdomains": 5}], "a.com") == 1.0, text


def test_script_of_the_past_site_language_is_not_spam():
    """Р1: японский анкор на .com — норма для японского прошлого сайта и спам для испанского.
    Без языка прошлого сайта нелатинский скрипт на латинском домене — спам, как и было."""
    jp = [{"anchor": "東京のブログ", "refdomains": 30, "is_spam": False}]
    assert spam_anchor_ratio(jp, "tokyoblog.com", "ja") == 0.0
    assert spam_anchor_ratio(jp, "tokyoblog.com", "es") == 1.0
    assert spam_anchor_ratio(jp, "tokyoblog.com") == 1.0
    ru = [{"anchor": "официальный сайт", "refdomains": 10}]
    assert spam_anchor_ratio(ru, "site.com", "uk") == 0.0 and spam_anchor_ratio(ru, "site.com", "ja") == 1.0
    kr = [{"anchor": "서울 여행", "refdomains": 10}]
    assert spam_anchor_ratio(kr, "seoultrip.com", "ko") == 0.0
    assert spam_anchor_ratio(kr, "seoultrip.com", "zh") == 1.0
    assert spam_anchor_ratio([{"anchor": "casino 東京", "refdomains": 5}], "a.com", "ja") == 1.0   # стоп-слово — всегда спам
    # R2-2: scripts=False — доля без правила скрипта (язык прошлого сайта неизвестен)
    assert spam_anchor_ratio(jp, "tokyoblog.com", scripts=False) == 0.0
    assert spam_anchor_ratio([{"anchor": "casino 東京", "refdomains": 5}], "a.com", scripts=False) == 1.0


def test_peak_traffic():
    hist = json.loads((FX / "ahrefs_metrics_history.json").read_text())["metrics"]
    assert peak_traffic(hist) == 10439698 and peak_traffic([]) is None
