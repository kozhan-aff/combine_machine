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
    monkeypatch.setattr("app.services.discovery._collect", lambda enabled: [
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
    monkeypatch.setattr(discovery, "_collect", lambda enabled: [
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


def test_sweb_drops_excludes_dates_sharing_class_with_domain(monkeypatch):
    """Регрессия (найдена при ревью спеки, 2026-07-08): domains-deleted__text — ОБЩИЙ
    класс для домена И обеих дат (регистрация/освобождение), в отличие от reg.ru тут нет
    отдельного "первая колонка" маркера. Наивный якорь на класс поймал бы '07.07.2026'
    как домен (три числовые группы через точку проходят простую проверку домен-формы).
    Якорь на label='Домен' должен вернуть только реальные домены."""
    from app.integrations.sweb_drops import SwebDropsClient
    c = SwebDropsClient()
    html = (
        '<li class="domains-deleted__item">'
        '<div class="domains-deleted__item-row">'
        '<span class="domains-deleted__label">Домен</span>'
        '<span class="domains-deleted__text">first-drop.ru</span></div>'
        '<div class="domains-deleted__item-row">'
        '<span class="domains-deleted__label">Первичная регистрация</span>'
        '<span class="domains-deleted__text">03.06.2022</span></div>'
        '<div class="domains-deleted__item-row">'
        '<span class="domains-deleted__label">Дата освобождения</span>'
        '<span class="domains-deleted__text">07.07.2026</span></div>'
        '</li>'
        '<li class="domains-deleted__item">'
        '<div class="domains-deleted__item-row">'
        '<span class="domains-deleted__label">Домен</span>'
        '<span class="domains-deleted__text">second-drop.ru</span></div>'
        '<div class="domains-deleted__item-row">'
        '<span class="domains-deleted__label">Первичная регистрация</span>'
        '<span class="domains-deleted__text">12.11.2012</span></div>'
        '<div class="domains-deleted__item-row">'
        '<span class="domains-deleted__label">Дата освобождения</span>'
        '<span class="domains-deleted__text">07.07.2026</span></div>'
        '</li>'
    )

    class _Resp:
        text = html
    monkeypatch.setattr(c, "request", lambda method, url, **kw: _Resp())
    rows = c.list_dropping()
    domains = [r["domain"] for r in rows]
    assert domains == ["first-drop.ru", "second-drop.ru"]
    assert "03.06.2022" not in domains
    assert "07.07.2026" not in domains
    assert "12.11.2012" not in domains
    assert all(r["source"] == "sweb" and r["referring_domains"] is None for r in rows)


def test_sweb_drops_ping(monkeypatch):
    from app.integrations.sweb_drops import SwebDropsClient
    c = SwebDropsClient()

    class _Ok:
        text = ('<span class="domains-deleted__label">Домен</span>'
                '<span class="domains-deleted__text">x.ru</span>')
    monkeypatch.setattr(c, "request", lambda method, url, **kw: _Ok())
    assert c.ping() is True

    class _Empty:
        text = "<html>ничего нет</html>"
    monkeypatch.setattr(c, "request", lambda method, url, **kw: _Empty())
    assert c.ping() is False


def _make_zip(filename: str, lines: list[str]) -> bytes:
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(filename, "\n".join(lines))
    return buf.getvalue()


def test_cctld_downloads_both_zips_and_lists_domains(monkeypatch):
    """Живая проверка (2026-07-08): лендинг не содержит домены — только ссылки на
    ежедневные ZIP (RUDelList<YYYYMMDD>.zip / RFDelList<YYYYMMDD>.zip), внутри — простой
    текстовый список, один домен на строку."""
    from app.integrations.cctld import CctldClient
    c = CctldClient()
    landing = (
        '<a href="/files/docs/pendingdelete/RUDelList20260708.zip">RU</a> '
        '<a href="/files/docs/pendingdelete/RFDelList20260708.zip">RF</a>'
    )
    ru_zip = _make_zip("RUDelList20260708.txt", ["one.ru", "two.ru"])
    rf_zip = _make_zip("RFDelList20260708.txt", ["xn--e1afmkfd.xn--p1ai"])

    class _Resp:
        def __init__(self, text=None, content=None):
            self.text = text
            self.content = content

    def fake_request(method, url, **kw):
        if url.endswith("RUDelList20260708.zip"):
            return _Resp(content=ru_zip)
        if url.endswith("RFDelList20260708.zip"):
            return _Resp(content=rf_zip)
        return _Resp(text=landing)

    monkeypatch.setattr(c, "request", fake_request)
    rows = c.list_dropping()
    domains = {r["domain"] for r in rows}
    assert domains == {"one.ru", "two.ru", "xn--e1afmkfd.xn--p1ai"}
    assert all(r["source"] == "cctld" and r["referring_domains"] is None for r in rows)


def test_cctld_partial_zip_failure_logs_and_returns_other(monkeypatch, caplog):
    """Спека: если один zip не скачался/битый — не ронять весь источник, вернуть то,
    что получилось от другого, и залогировать warning (иначе тихий частичный отказ
    недебажим — тот же класс бага, что уже чинили в discovery.py I6)."""
    from app.integrations.cctld import CctldClient
    c = CctldClient()
    landing = (
        '<a href="/files/docs/pendingdelete/RUDelList20260708.zip">RU</a> '
        '<a href="/files/docs/pendingdelete/RFDelList20260708.zip">RF</a>'
    )
    ru_zip = _make_zip("RUDelList20260708.txt", ["ok.ru"])

    class _Resp:
        def __init__(self, text=None, content=None):
            self.text = text
            self.content = content

    def fake_request(method, url, **kw):
        if url.endswith("RUDelList20260708.zip"):
            return _Resp(content=ru_zip)
        if url.endswith("RFDelList20260708.zip"):
            return _Resp(content=b"not a zip file")
        return _Resp(text=landing)

    monkeypatch.setattr(c, "request", fake_request)
    with caplog.at_level("WARNING", logger="app.integrations.cctld"):
        rows = c.list_dropping()
    assert [r["domain"] for r in rows] == ["ok.ru"]
    assert any("cctld" in r.message for r in caplog.records)


def test_cctld_ping_true_when_zip_href_found(monkeypatch):
    from app.integrations.cctld import CctldClient
    c = CctldClient()

    class _Resp:
        text = '<a href="/files/docs/pendingdelete/RUDelList20260708.zip">RU</a>'
    monkeypatch.setattr(c, "request", lambda method, url, **kw: _Resp())
    assert c.ping() is True


def test_cctld_ping_false_when_no_zip_href(monkeypatch):
    from app.integrations.cctld import CctldClient
    c = CctldClient()

    class _Resp:
        text = "<html>ничего нет</html>"
    monkeypatch.setattr(c, "request", lambda method, url, **kw: _Resp())
    assert c.ping() is False


def test_cctld_carries_drop_deadline_from_zip_name(monkeypatch):
    """Дата дропа cctld живёт В ИМЕНИ архива (RUDelList20260714.zip) — и обязана доехать до
    домена. Без неё whois говорит «занят» (домен ДО дропа и должен быть занят), вердикт судить
    не по чему, и весь реестр (~9.5 тыс.) уезжает в rejected/not_acquirable, ни разу не
    дождавшись дропа. Ровно это и было на живом боксе (дебаг 2026-07-13)."""
    from datetime import datetime, timezone
    from app.integrations.cctld import CctldClient

    c = CctldClient()
    monkeypatch.setattr(c, "_zip_urls", lambda: ["https://cctld.ru/files/RUDelList20260714.zip"])
    monkeypatch.setattr(c, "_domains_from_zip", lambda url: ["alpha.ru", "beta.ru"])
    rows = c.list_dropping()
    assert [r["domain"] for r in rows] == ["alpha.ru", "beta.ru"]
    assert all(r["acquire_deadline"] == datetime(2026, 7, 14, tzinfo=timezone.utc) for r in rows)


def test_cctld_unparsable_zip_name_yields_no_deadline(monkeypatch):
    """Разметка съехала — домены всё равно едут, просто без дедлайна (и вердикт их не
    выбрасывает, а ждёт). Тихо ронять источник целиком тут нельзя."""
    from app.integrations.cctld import CctldClient

    c = CctldClient()
    monkeypatch.setattr(c, "_zip_urls", lambda: ["https://cctld.ru/files/mystery.zip"])
    monkeypatch.setattr(c, "_domains_from_zip", lambda url: ["alpha.ru"])
    assert c.list_dropping() == [{"domain": "alpha.ru", "source": "cctld",
                                  "referring_domains": None, "acquire_deadline": None}]
