"""AParserClient: whois-парсинг (существующее) + Ahrefs DR/backlinks/referring-domains
(живьём проверенный формат 2026-07-08 — см. docs/superpowers/specs/2026-07-08-ahrefs-dr-design.md)."""
from app.integrations.aparser import _parse_ahrefs, AParserClient


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
