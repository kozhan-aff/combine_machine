"""Парсеры источников дропов + whois-даты. Оффлайн, на фикстурах-строках."""
from datetime import timezone
import pytest
from app.integrations.aparser import _parse_whois_created


def test_whois_created_ru():
    txt = "domain: EXAMPLE.RU\ncreated: 2010.11.15\npaid-till: 2026.11.15\n"
    d = _parse_whois_created(txt)
    assert d is not None and (d.year, d.month, d.day) == (2010, 11, 15)
    assert d.tzinfo == timezone.utc


def test_whois_created_gtld():
    txt = "Domain Name: EXAMPLE.COM\nCreation Date: 2004-03-15T05:00:00Z\n"
    d = _parse_whois_created(txt)
    assert (d.year, d.month, d.day) == (2004, 3, 15)


def test_whois_created_junk_is_none():
    assert _parse_whois_created("no date here at all") is None
    assert _parse_whois_created("") is None


def test_whois_svertka_taken():
    from app.integrations.aparser import _parse_whois_available, _parse_whois_created
    txt = "python.org - registered: 1, expire: 28.03.2033, creation: 27.03.1995\n"
    assert _parse_whois_available(txt) is False
    d = _parse_whois_created(txt)
    assert (d.year, d.month, d.day) == (1995, 3, 27) and d.tzinfo is not None


def test_whois_svertka_free():
    from app.integrations.aparser import _parse_whois_available, _parse_whois_created
    txt = "free-drop-nonexistent-2026.ru - registered: 0, expire: none, creation: none\n"
    assert _parse_whois_available(txt) is True
    assert _parse_whois_created(txt) is None


def test_whois_svertka_free_rf():
    from app.integrations.aparser import _parse_whois_available
    assert _parse_whois_available("пример.рф - registered: 0, expire: none, creation: none\n") is True


def test_whois_old_format_still_works():
    # старый сырой whois — фолбэк (пресет A-Parser может отдать иное)
    from app.integrations.aparser import _parse_whois_available, _parse_whois_created
    txt = "domain: X.RU\ncreated: 2010.11.15\nnserver: ns.x.ru"
    assert _parse_whois_available(txt) is False
    assert _parse_whois_created(txt).year == 2010


def test_parse_whois_available():
    from app.integrations.aparser import _parse_whois_available
    assert _parse_whois_available("No entries found for the selected source.") is True
    assert _parse_whois_available("Not found") is True
    assert _parse_whois_available(
        "domain: EXAMPLE.RU\ncreated: 2010.11.15\nnserver: ns1.example.ru") is False
    assert _parse_whois_available("registrar: RU-CENTER\nperson: Private") is False
    assert _parse_whois_available("какой-то мусор без маркеров") is None
    assert _parse_whois_available("") is None


def test_whois_probe_shapes(monkeypatch):
    from app.integrations import aparser
    c = aparser.AParserClient()
    monkeypatch.setattr(c, "_call", lambda *a, **k: {"data": {"resultString": "No entries found"}})
    assert c.whois_probe("free.ru") == {"available": True, "created": None}
    monkeypatch.setattr(c, "_call", lambda *a, **k: {
        "data": {"resultString": "domain: X.RU\ncreated: 2010.11.15\nnserver: ns.x.ru"}})
    pr = c.whois_probe("taken.ru")
    assert pr["available"] is False and pr["created"] is not None


def test_whois_probe_propagates_transport_error(monkeypatch):
    """M1 приобретаемость: сетевой сбой пробрасывается наружу — ловит _funnel (sig["errors"]),
    транспортный слой не глотает исключения (контракт как у остальных методов клиента)."""
    from app.integrations import aparser
    c = aparser.AParserClient()

    def _boom(*a, **k):
        raise RuntimeError("network down")
    monkeypatch.setattr(c, "_call", _boom)
    with pytest.raises(RuntimeError, match="network down"):
        c.whois_probe("x.ru")
    with pytest.raises(RuntimeError, match="network down"):
        c.whois_created("x.ru")


def test_canonical_domain():
    from app.services.discovery import canonical_domain
    assert canonical_domain("Пример.РФ") == "xn--e1afmkfd.xn--p1ai"
    assert canonical_domain("xn--e1afmkfd.xn--p1ai") == "xn--e1afmkfd.xn--p1ai"   # уже punycode
    assert canonical_domain("www.Example.COM.") == "example.com"                   # www + регистр + точка
    assert canonical_domain("under_score.ru") is None                             # мусор
    assert canonical_domain("") is None
    assert canonical_domain("support@mail.ru") is None                            # e-mail — не домен

