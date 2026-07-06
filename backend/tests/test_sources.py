"""Парсеры источников дропов + whois-даты. Оффлайн, на фикстурах-строках."""
from datetime import timezone
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
