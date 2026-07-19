"""Прямой whois TCI (.RU/.РФ/.SU) — транспорт + маршрутизатор. Сеть замокана
(рубильник `_no_live_network` в conftest всё равно не пустил бы: сырой TCP TCI
падал бы громко на socket.getaddrinfo, а не тихо уезжал в интернет)."""
from datetime import date

from app.integrations.whois_tci import TciWhoisClient, _parse

_TAKEN = """% TCI Whois Service. Terms of use:

domain:        USEFULSCRIPT.RU
state:         REGISTERED, DELEGATED, VERIFIED
registrar:     REGRU-RU
created:       2011-06-17T08:12:35Z
paid-till:     2026-06-17T09:12:35Z
free-date:     2026-07-21
source:        TCI
"""
_FREE = ("% TCI Whois Service. Terms of use:\n\n"
         "No entries found for the selected source(s).\n")
_INVALID = "% TCI Whois Service. Terms of use:\n\nInvalid request.\n"


def test_parse_taken_domain():
    r = _parse(_TAKEN)
    assert r["available"] is False
    assert r["created"].year == 2011
    assert r["free_date"] == date(2026, 7, 21)


def test_parse_free_domain():
    r = _parse(_FREE)
    assert r["available"] is True
    assert r["created"] is None and r["free_date"] is None


def test_parse_invalid_request_is_unknown():
    """Invalid request — НЕ свободен и НЕ занят. Кириллический .рф без punycode
    отвечает именно так; трактовать его как свободный = утащить домен в выкуп."""
    assert _parse(_INVALID)["available"] is None


def test_parse_junk_is_unknown():
    assert _parse("")["available"] is None
    assert _parse("совершенно посторонний текст")["available"] is None


def test_handles_only_tci_zones():
    """example.com у TCI отвечает 'No entries found' — дословно как свободный .ru.
    Маршрутизация по зоне ОБЯЗАНА резать это до запроса (живая проба 2026-07-19)."""
    c = TciWhoisClient()
    assert c.handles("clara-c.ru") is True
    assert c.handles("ok.su") is True
    assert c.handles("xn--80aswg.xn--p1ai") is True
    assert c.handles("сайт.рф") is True          # нормализуется в punycode
    assert c.handles("example.com") is False
    assert c.handles("example.net") is False


# --- маршрутизатор ---------------------------------------------------------------

class _FakeTci:
    def __init__(self, result=None, boom=False):
        self.result, self.boom, self.calls = result, boom, []

    def handles(self, domain):
        return TciWhoisClient().handles(domain)

    def probe(self, domain):
        self.calls.append(domain)
        if self.boom:
            raise OSError("соединение отклонено")
        return self.result


class _FakeAparser:
    def __init__(self):
        self.calls = []

    def whois_probe(self, domain):
        self.calls.append(domain)
        return {"available": False, "created": None}


def test_router_uses_tci_for_ru():
    from app.services import whois
    tci = _FakeTci({"available": True, "created": None, "free_date": None})
    ap = _FakeAparser()
    r = whois.probe("clara-c.ru", {"tci": tci, "aparser": ap})
    assert r["available"] is True
    assert r["whois_source"] == "tci"
    assert tci.calls == ["clara-c.ru"] and ap.calls == []


def test_router_uses_aparser_for_com():
    """Регресс на ловушку: .com НЕ должен уходить в TCI ни при каких условиях."""
    from app.services import whois
    tci = _FakeTci({"available": True, "created": None, "free_date": None})
    ap = _FakeAparser()
    r = whois.probe("example.com", {"tci": tci, "aparser": ap})
    assert r["whois_source"] == "aparser"
    assert tci.calls == [] and ap.calls == ["example.com"]


def test_router_falls_back_visibly_when_tci_fails():
    """Сбой TCI не глотается молча — фолбэк помечен в whois_source."""
    from app.services import whois
    tci, ap = _FakeTci(boom=True), _FakeAparser()
    r = whois.probe("clara-c.ru", {"tci": tci, "aparser": ap})
    assert r["whois_source"] == "aparser_fallback"
    assert ap.calls == ["clara-c.ru"]
    assert r["free_date"] is None
