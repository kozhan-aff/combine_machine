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


def test_parse_domains_extracts_ru():
    from app.integrations.cctld import _parse_domains
    html = "<tr><td>Example-1.RU</td></tr><tr><td>второй.рф</td></tr> мусор foo.com bar"
    got = _parse_domains(html)
    assert "example-1.ru" in got and "второй.рф" in got
    assert "foo.com" not in got          # берём только .ru/.рф/.su


def test_run_discovery_dedups_across_sources(monkeypatch):
    from app.services import discovery
    import app.db as db
    from sqlalchemy import select
    from app.models.domain import Domain
    monkeypatch.setattr("app.services.discovery._collect", lambda enabled, on_progress=None: [
        {"domain": "dup.ru", "source": "cctld", "referring_domains": None},
        {"domain": "dup.ru", "source": "backorder", "referring_domains": 42, "feed_flags": {"rkn": False}},
        {"domain": "solo.ru", "source": "cctld", "referring_domains": None},
    ])
    n = discovery.run_discovery()
    assert n == 2
    with db.SessionLocal() as s:
        rows = {d.domain: d for d in s.execute(select(Domain)).scalars().all()}
    assert rows["dup.ru"].referring_domains == 42     # выиграла строка с бо́льшим RD (backorder)


def test_collect_logs_and_survives_source_failure(monkeypatch, caplog):
    """Finding 5 (финальное ревью): падение одного источника не должно тонуть молча —
    остальные источники всё равно собираются (continue-семантика), но в лог уходит warning
    с именем источника, иначе первый прод-инцидент недебажим."""
    from app.services import discovery

    class _Boom:
        def list_dropping(self):
            raise RuntimeError("feed timeout")

    class _Ok:
        def list_dropping(self):
            return [{"domain": "alive.ru", "source": "cctld", "referring_domains": None}]

    monkeypatch.setattr(discovery, "_sources", lambda: {"backorder": _Boom, "cctld": _Ok})
    with caplog.at_level("WARNING", logger="app.services.discovery"):
        rows = discovery._collect({"backorder": True, "cctld": True})
    assert rows == [{"domain": "alive.ru", "source": "cctld", "referring_domains": None}]
    assert any("backorder" in r.message and "feed timeout" in r.message for r in caplog.records)
