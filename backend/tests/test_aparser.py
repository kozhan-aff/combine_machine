"""AParserClient: whois-парсинг (существующее) + Ahrefs DR/backlinks/referring-domains
(живьём проверенный формат 2026-07-08 — см. docs/superpowers/specs/2026-07-08-ahrefs-dr-design.md)."""
from app.integrations.aparser import (
    _parse_ahrefs, _parse_safebrowsing, _parse_archive, AParserClient,
)


def test_parse_ahrefs_with_real_rating():
    # живой ответ: wikipedia.org: 97, 3421649284, 4000871
    out = _parse_ahrefs("wikipedia.org: 97, 3421649284, 4000871\n")
    assert out == {"dr": 97, "backlinks": 3421649284, "referring_domains": 4000871}


def test_parse_ahrefs_none_rating():
    # живой ответ: lev777.casino: none, 476, 268 — DR недоступен, но backlinks/domains есть
    out = _parse_ahrefs("lev777.casino: none, 476, 268\n")
    assert out == {"dr": None, "backlinks": 476, "referring_domains": 268}


def test_parse_ahrefs_none_rating_uppercase():
    out = _parse_ahrefs("example.com: NONE, 0, 0\n")
    assert out["dr"] is None


def test_parse_ahrefs_no_match_returns_all_none():
    out = _parse_ahrefs("garbage response, not the expected format")
    assert out == {"dr": None, "backlinks": None, "referring_domains": None}


def test_parse_ahrefs_empty_string():
    out = _parse_ahrefs("")
    assert out == {"dr": None, "backlinks": None, "referring_domains": None}


def test_ahrefs_probe_sends_expected_options(monkeypatch):
    """ahrefs_probe передаёт options МАССИВОМ {name,value} пар (не объектом — так
    принимает живой A-Parser API, выяснено методом проб 2026-07-08) и правильный
    Result format/Turnstile preset."""
    from app.config import settings
    monkeypatch.setattr(settings, "APARSER_PROXY_CHECKER", "Free Proxy 2")
    client = AParserClient()
    captured = {}

    def fake_call(action, data=None):
        captured["action"] = action
        captured["data"] = data
        return {"data": {"resultString": "test.ru: 15, 100, 50\n"}}

    monkeypatch.setattr(client, "_call", fake_call)
    out = client.ahrefs_probe("test.ru")

    assert captured["action"] == "oneRequest"
    assert captured["data"]["query"] == "test.ru"
    assert captured["data"]["parser"] == "Rank::Ahrefs"
    opts = {o["name"]: o["value"] for o in captured["data"]["options"]}
    assert opts["Use proxy"] is True
    assert opts["Proxy Checker"] == "Free Proxy 2"
    assert opts["Result format"] == "$query: $rating, $bl, $domains\n"
    assert opts["Util::Turnstile preset"] == "RuCapcha"
    assert out == {"dr": 15, "backlinks": 100, "referring_domains": 50}


def test_parse_safebrowsing_flagged():
    assert _parse_safebrowsing("zudpopo.ru: 1\n") is True


def test_parse_safebrowsing_clean():
    assert _parse_safebrowsing("dswjcndwijnwld23234212djf.ru: 0\n") is False


def test_parse_safebrowsing_no_match_returns_none():
    assert _parse_safebrowsing("garbage response") is None


def test_parse_safebrowsing_empty_string():
    assert _parse_safebrowsing("") is None


def test_parse_archive_with_history():
    out = _parse_archive("google.com: 11.11.1998 - 16.07.2026 (19936104 times)\n")
    assert out == {"times": 19936104, "first": "11.11.1998", "last": "16.07.2026"}


def test_parse_archive_none_history():
    out = _parse_archive("dswjcndwijnwld23234212djf.ru: none - none (none times)\n")
    assert out == {"times": None, "first": None, "last": None}


def test_parse_archive_no_match_returns_all_none():
    out = _parse_archive("garbage response, not the expected format")
    assert out == {"times": None, "first": None, "last": None}


def test_parse_archive_empty_string():
    out = _parse_archive("")
    assert out == {"times": None, "first": None, "last": None}


def test_safebrowsing_check_sends_expected_parser(monkeypatch):
    seen = {}

    def fake_call(self, action, data):
        seen["action"], seen["data"] = action, data
        return {"success": 1, "data": {"resultString": "example.com: 0\n"}}

    monkeypatch.setattr(AParserClient, "_call", fake_call)
    c = AParserClient()
    assert c.safebrowsing_check("example.com") is False
    assert seen["data"]["parser"] == "SE::Google::SafeBrowsing"
    assert seen["data"]["query"] == "example.com"


def test_archive_probe_uses_no_proxy_preset(monkeypatch):
    seen = {}

    def fake_call(self, action, data):
        seen["data"] = data
        return {"success": 1, "data": {"resultString": "example.com: none - none (none times)\n"}}

    monkeypatch.setattr(AParserClient, "_call", fake_call)
    c = AParserClient()
    out = c.archive_probe("example.com")
    assert out["times"] is None
    assert seen["data"]["parser"] == "Rank::Archive"
    assert seen["data"]["preset"] == "no_proxy"          # НЕ "default" — тот сломан через прокси
