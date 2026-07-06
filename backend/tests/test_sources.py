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
    monkeypatch.setattr("app.services.discovery._collect", lambda enabled: [
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
