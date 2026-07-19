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


def test_handles_rejects_third_level_domains():
    """CRITICAL (ревью 2026-07-19): TCI обслуживает СТРОГО второй уровень. `endswith`
    по хвосту пропускал бы третий уровень в TCI — а там 'No entries found' дословно как
    у свободного .ru, хотя домен ЗАНЯТ (поддомен под чужой делегированной зоной). Живая
    проба: shop.com.ru/www.msk.ru -> 'No entries found'; сами com.ru/msk.ru/pp.ru/spb.ru
    — валидные домены ВТОРОГО уровня (полная запись) — их занижать до TLD-суффикса нельзя."""
    c = TciWhoisClient()
    assert c.handles("shop.com.ru") is False
    assert c.handles("www.msk.ru") is False
    assert c.handles("x.pp.ru") is False
    assert c.handles("y.spb.ru") is False
    assert c.handles("com.ru") is True     # сам по себе валидный домен второго уровня в зоне ru
    assert c.handles("msk.ru") is True
    assert c.handles("pp.ru") is True
    assert c.handles("spb.ru") is True


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


# --- воронка целиком: _funnel -> whois.probe -> TCI, без единого касания A-Parser ------------

class _FunnelWayback:
    """Чистая старая история — домен доживает до approved, но это не суть теста."""
    def classify_history(self, domain):
        return {"prior_flags": {c: False for c in ("adult", "pharma", "casino", "gambling", "spam")},
                "first_seen": None, "age_years": 9.0, "wayback_checked": True, "sampled": 5}


def _mk_ru_domain(**kw):
    import app.db as db
    from app.models.domain import Domain
    with db.SessionLocal() as s:
        d = Domain(domain=kw.pop("domain", "tcigood.ru"), source=kw.pop("source", "cctld"),
                   status="discovered", **kw)
        s.add(d); s.commit(); s.refresh(d)
        return d.id


def test_funnel_uses_tci_for_ru_without_touching_aparser():
    """IMPORTANT (ревью 2026-07-19): нулевое интеграционное покрытие связки
    `_funnel -> whois.probe -> TCI` — все фейки в остальных тестовых файлах ставят
    `handles -> False`. Опечатка в ключе "tci" (services/scoring.py::_make_clients)
    отключила бы маршрутизацию целиком, и сьют остался бы зелёным. Требование спеки
    §Тестирование: домен .ru проходит T1 БЕЗ единого обращения к A-Parser."""
    from datetime import datetime, timedelta, timezone
    import app.db as db
    from app.models.domain import Domain
    from app.services import scoring

    did = _mk_ru_domain(domain="tcigood.ru", referring_domains=3000, lane="bid")
    old = datetime.now(timezone.utc) - timedelta(days=365 * 9)
    tci = _FakeTci({"available": False, "created": old, "free_date": None})
    ap = _FakeAparser()
    clients = {
        "aparser": ap, "tci": tci,
        "rkn": type("R", (), {"is_listed": lambda self, d: False})(),
        "blacklist": type("B", (), {"is_blacklisted": lambda self, d: False})(),
        "searxng": type("S", (), {"indexed_echo": lambda self, d: True})(),
        "wayback": _FunnelWayback(),
    }
    out = scoring.score_domain(did, clients=clients)

    assert ap.calls == []                       # A-Parser не звали — TCI закрыл whois целиком
    assert tci.calls == ["tcigood.ru"]
    assert out["status"] in ("approved", "scored") and out["reject_reason"] is None

    with db.SessionLocal() as s:
        d = s.get(Domain, did)
    assert d.score_breakdown["whois_source"] == "tci"      # видно оператору после фикса Задачи 1
