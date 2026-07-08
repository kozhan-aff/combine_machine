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


def test_normalize_row_captures_acquirability():
    from app.services.discovery import normalize_row
    nr = normalize_row({"domainname": "drop.ru", "links": "7",
                        "delete_date": "2026-07-10", "visitors": "120", "yandex_tic": "30"})
    assert nr["lane"] == "bid" and nr["referring_domains"] == 7
    assert nr["acquire_deadline"] is not None and nr["visitors"] == 120 and nr["tic"] == 30
    # мусорный дедлайн не роняет строку
    assert normalize_row({"domainname": "d2.ru", "delete_date": "нет"})["acquire_deadline"] is None


def test_run_discovery_persists_acquirability(monkeypatch):
    from app.services import discovery
    import app.db as db
    from app.models.domain import Domain
    from sqlalchemy import select
    monkeypatch.setattr("app.services.discovery._collect", lambda enabled, on_progress=None: [
        {"domain": "bo.ru", "source": "backorder", "referring_domains": 5, "lane": "bid",
         "acquire_deadline": None, "visitors": 10, "tic": 20, "feed_flags": {"rkn": False}}])
    assert discovery.run_discovery() == 1
    with db.SessionLocal() as s:
        d = s.execute(select(Domain).where(Domain.domain == "bo.ru")).scalar_one()
        assert d.lane == "bid" and d.visitors == 10 and d.tic == 20


def test_canonical_domain():
    from app.services.discovery import canonical_domain
    assert canonical_domain("Пример.РФ") == "xn--e1afmkfd.xn--p1ai"
    assert canonical_domain("xn--e1afmkfd.xn--p1ai") == "xn--e1afmkfd.xn--p1ai"   # уже punycode
    assert canonical_domain("www.Example.COM.") == "example.com"                   # www + регистр + точка
    assert canonical_domain("under_score.ru") is None                             # мусор
    assert canonical_domain("") is None
    assert canonical_domain("support@mail.ru") is None                            # e-mail — не домен


def test_normalize_row_keeps_rf():
    from app.services.discovery import normalize_row
    nr = normalize_row({"domainname": "пример.рф", "links": "7"})
    assert nr is not None and nr["domain"] == "xn--e1afmkfd.xn--p1ai"


def test_existing_discovered_row_enriched_on_rediscovery(monkeypatch):
    import app.db as db
    from app.models.domain import Domain
    from app.services import discovery
    # день 1: домен без RD/lane (как из сырого источника)
    with db.SessionLocal() as s:
        s.add(Domain(domain="drop.ru", source="cctld", status="discovered",
                     referring_domains=None, lane=None)); s.commit()
    # день 2: тот же домен пришёл из backorder с RD и lane
    monkeypatch.setattr(discovery, "_collect", lambda enabled, on_progress=None: [
        {"domain": "drop.ru", "source": "backorder", "referring_domains": 42, "lane": "bid",
         "acquire_deadline": None, "visitors": None, "tic": None, "feed_flags": {}}])
    discovery.run_discovery()
    with db.SessionLocal() as s:
        d = s.execute(__import__("sqlalchemy").select(Domain)).scalars().one()
        assert d.referring_domains == 42 and d.lane == "bid"     # обогатилось, не пропущено


def test_normalize_row_sentinels_and_price():
    from app.services.discovery import normalize_row
    nr = normalize_row({"domainname": "x.ru", "links": 5, "visitors": -1, "yandex_tic": -1, "price": 190})
    assert nr["visitors"] is None and nr["tic"] is None and nr["price"] == 190.0


def test_list_dropping_string_links_below_min_warns(monkeypatch, caplog):
    """M2 инвариант (см. ревью Task 7): фид иногда отдаёт links строкой ("0") даже когда
    запрошен min_links=1 (серверный фильтр не сработал). Safe int-parse не должен падать
    TypeError на сравнении str<int, но и не должен молчать — обязан залогировать warning."""
    from app.integrations.backorder import BackorderClient
    c = BackorderClient()

    class _Resp:
        def json(self):
            return [{"domainname": "x.ru", "links": "0", "delete_date": "2026-07-10"}]

    monkeypatch.setattr(c, "request", lambda *a, **k: _Resp())
    with caplog.at_level("WARNING", logger="app.integrations.backorder"):
        rows = c.list_dropping(min_links=1)   # не должно бросить исключение
    assert rows == [{"domainname": "x.ru", "links": "0", "delete_date": "2026-07-10"}]
    assert any("backorder" in r.message and "links" in r.message for r in caplog.records)


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


def test_regru_drops_extracts_only_domain_cells(monkeypatch):
    """Живая проверка (2026-07-08): b-table__cell_node_first — единственный класс на
    ячейке с доменом (у дат тот же базовый класс, но без node_first; заголовок 'Домен' —
    <th>, не <td>). Общий regex-по-странице ловил бы reg.ru/yandex.ru из футера —
    якорь на класс не должен."""
    from app.integrations.regru_drops import RegruDropsClient
    c = RegruDropsClient()
    html = (
        '<footer><a href="https://reg.ru">reg.ru</a> '
        '<a href="https://yandex.ru">yandex.ru</a></footer>'
        '<table><tr>'
        '<td class="b-table__cell b-table__cell_type_content b-table__cell_node_first">'
        'first-drop.ru</td>'
        '<td class="b-table__cell b-table__cell_type_content">03.06.2022</td>'
        '</tr><tr>'
        '<td class="b-table__cell b-table__cell_type_content b-table__cell_node_first">'
        'second-drop.ru</td>'
        '<td class="b-table__cell b-table__cell_type_content">07.07.2026</td>'
        '</tr></table>'
    )

    class _Resp:
        text = html
    monkeypatch.setattr(c, "request", lambda method, url, **kw: _Resp())
    rows = c.list_dropping()
    domains = [r["domain"] for r in rows]
    assert domains == ["first-drop.ru", "second-drop.ru"]
    assert "reg.ru" not in domains and "yandex.ru" not in domains
    assert all(r["source"] == "reg_ru" and r["referring_domains"] is None for r in rows)


def test_regru_drops_ping(monkeypatch):
    from app.integrations.regru_drops import RegruDropsClient
    c = RegruDropsClient()

    class _Ok:
        text = '<td class="b-table__cell_node_first">x.ru</td>'
    monkeypatch.setattr(c, "request", lambda method, url, **kw: _Ok())
    assert c.ping() is True

    class _Empty:
        text = "<html>ничего нет</html>"
    monkeypatch.setattr(c, "request", lambda method, url, **kw: _Empty())
    assert c.ping() is False
