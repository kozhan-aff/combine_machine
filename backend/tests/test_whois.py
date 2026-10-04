"""whois-маршрутизатор v2 (services/whois.py): RDAP, иначе whois:43 через A-Parser; предохранители
каналов. Тесты предохранителя A-Parser перенесены из удалённого test_whois_tci.py (на зоне .mx — у
неё нет RDAP), сквозные тесты «score_domain -> маршрут» — на RDAP."""
import threading
from datetime import datetime, timedelta, timezone

import pytest

import app.db as db
from app.models.domain import Domain
from app.services import scoring, whois

NOW = datetime.now(timezone.utc)
_AP_OK = {"available": False, "created": None}
_FREE = {"exists": False, "status": [], "registered_at": None}


class _Rdap:
    """RDAP-фейк: зоны с RDAP; исход lookup — по списку, "boom" — исключение."""
    def __init__(self, outcomes=(), zones=("com",)):
        self._outcomes, self.zones, self.calls = list(outcomes), zones, []

    def has_rdap(self, d):
        return d.rsplit(".", 1)[-1] in self.zones

    def lookup(self, d):
        self.calls.append(d)
        out = self._outcomes.pop(0)
        if out == "boom":
            raise OSError("rdap timeout")
        return out


class _FlakyAparser:
    """whois_probe с исходом на каждый вызов по списку ("boom" — исключение)."""
    def __init__(self, outcomes=()):
        self._outcomes = list(outcomes)
        self.calls = []

    def whois_probe(self, domain):
        self.calls.append(domain)
        outcome = self._outcomes.pop(0)
        if outcome == "boom":
            raise OSError("connect error")
        return outcome


class _SpyLock:
    """Настоящий Lock, который считает входы: доказывает, что предохранитель берёт лок вокруг
    ОБЕИХ операций (гейт-чек и запись счётчика) — детерминированно, без таймингов."""
    def __init__(self):
        self._real = threading.Lock()
        self.enters = 0

    def __enter__(self):
        self._real.acquire()
        self.enters += 1

    def __exit__(self, *a):
        self._real.release()


def test_probe_routes_rdap_then_aparser():
    reg = NOW - timedelta(days=4000)
    rdap = _Rdap([{"exists": True, "status": ["pending delete"], "registered_at": reg}])
    ap = _FlakyAparser([_AP_OK])
    c = {"rdap": rdap, "aparser": ap}
    assert whois.probe("x.com", c) == {"available": False, "created": reg, "free_date": None,
                                       "whois_source": "rdap", "status": ["pending delete"]}
    assert whois.probe("x.mx", c) == {"available": False, "created": None, "free_date": None,
                                      "whois_source": "aparser", "status": []}
    assert rdap.calls == ["x.com"] and ap.calls == ["x.mx"]


def test_aparser_whois_circuit_breaker_skips_after_three_consecutive_failures():
    """A-Parser упал -> без предохранителя КАЖДЫЙ домен зоны без RDAP платил бы полный
    ретрай-шторм BaseClient. Первые 3 вызова — РЕАЛЬНЫЕ попытки (исходный OSError), 4-й —
    предохранитель открыт (CircuitOpen, whois_probe не звали: исходов ровно 3, иначе IndexError)."""
    ap = _FlakyAparser(["boom", "boom", "boom"])
    clients = {"rdap": None, "aparser": ap}
    for i in range(3):
        with pytest.raises(OSError):
            whois.probe(f"fail{i}.mx", clients)
    assert len(ap.calls) == 3
    with pytest.raises(whois.CircuitOpen):
        whois.probe("fourth.mx", clients)
    assert len(ap.calls) == 3


def test_aparser_whois_circuit_breaker_resets_on_success_between_failures():
    """Успех между сбоями сбрасывает счётчик: «2 сбоя / успех / 2 сбоя» — ни разу 3 подряд."""
    ap = _FlakyAparser(["boom", "boom", _AP_OK, "boom", "boom"])
    clients = {"rdap": None, "aparser": ap}
    with pytest.raises(OSError):
        whois.probe("a.mx", clients)
    with pytest.raises(OSError):
        whois.probe("b.mx", clients)
    assert whois.probe("c.mx", clients)["available"] is False
    with pytest.raises(OSError):
        whois.probe("d.mx", clients)
    with pytest.raises(OSError):
        whois.probe("e.mx", clients)
    assert len(ap.calls) == 5


def test_rdap_circuit_breaker_skips_after_three_consecutive_failures():
    """3.2: тот же предохранитель у RDAP lookup — лежащий RDAP-сервер не превращает волну из 12
    потоков в ретрай-шторм на каждый .com."""
    rdap = _Rdap(["boom", "boom", "boom"])
    clients = {"rdap": rdap, "aparser": _FlakyAparser()}
    for i in range(3):
        with pytest.raises(OSError):
            whois.probe(f"fail{i}.com", clients)
    with pytest.raises(whois.CircuitOpen):
        whois.probe("fourth.com", clients)
    assert len(rdap.calls) == 3


def test_rdap_circuit_breaker_resets_on_success_between_failures():
    rdap = _Rdap(["boom", "boom", _FREE, "boom", "boom"])
    clients = {"rdap": rdap, "aparser": _FlakyAparser()}
    for name in ("a.com", "b.com"):
        with pytest.raises(OSError):
            whois.probe(name, clients)
    assert whois.probe("c.com", clients)["available"] is True        # 404 — свободен, не сбой
    for name in ("d.com", "e.com"):
        with pytest.raises(OSError):
            whois.probe(name, clients)
    assert len(rdap.calls) == 5


def test_rdap_breaker_locks_both_the_gate_check_and_the_increment():
    """Каждая из 3 попыток до срабатывания берёт лок дважды: гейт-чек и инкремент. Пропуск любого
    из входов — непокрытая гонка на счётчике под 12 потоками волны."""
    lock = _SpyLock()
    rdap = _Rdap(["boom", "boom", "boom"])
    for _ in range(3):
        with pytest.raises(OSError):
            whois.probe("x.com", {"rdap": rdap, "aparser": _FlakyAparser(), "_rdap_lock": lock})
    assert lock.enters == 6 and rdap.lookup_failures == 3


def test_make_clients_wires_rdap_and_its_lock():
    """Опечатка в ключе `rdap` в _make_clients отключила бы RDAP целиком (все домены ушли бы в
    платный A-Parser), а сьют остался бы зелёным: фейки передают клиентов сами."""
    from app.integrations.rdap import RdapClient
    c = scoring._make_clients()
    assert isinstance(c["rdap"], RdapClient)
    assert "_rdap_lock" in c and "_whois_lock" in c and "tci" not in c


# --- воронка целиком: score_domain -> _wave_avail -> whois.probe -> RDAP ----------------------

class _FunnelWayback:
    def classify_history(self, domain):
        return {"prior_flags": {c: False for c in ("adult", "pharma", "casino", "gambling", "spam")},
                "first_seen": None, "age_years": 9.0, "wayback_checked": True, "sampled": 5}


def _mk(**kw):
    with db.SessionLocal() as s:
        d = Domain(source=kw.pop("source", "list"), status="discovered", **kw)
        s.add(d); s.commit(); s.refresh(d)
        return d.id


def _funnel_clients(rdap, ap):
    return {"rdap": rdap, "aparser": ap,
            "rkn": type("R", (), {"is_listed": lambda self, d: False})(),
            "blacklist": type("B", (), {"is_blacklisted": lambda self, d: False})(),
            "searxng": type("S", (), {"indexed_echo": lambda self, d: True})(),
            "wayback": _FunnelWayback(),
            "ahrefs": type("Ah", (), {"units_left": lambda self: 2_000_000,
                                      "batch": lambda self, ds: {d: {} for d in ds}})()}


def test_funnel_routes_whois_through_rdap_without_touching_aparser():
    """Сквозной путь (перенос TCI-теста v1): RDAP-зона проходит W2 без единого обращения к
    A-Parser, источник виден оператору в score_breakdown."""
    reg = NOW - timedelta(days=365 * 9)
    did = _mk(domain="rdapgood.com", referring_domains=3000, lane="bid")
    rdap = _Rdap([{"exists": True, "status": ["pending delete"], "registered_at": reg}])
    ap = _FlakyAparser()
    out = scoring.score_domain(did, clients=_funnel_clients(rdap, ap))
    assert ap.calls == [] and rdap.calls == ["rdapgood.com"]
    assert out["status"] == "scored" and out["reject_reason"] is None
    with db.SessionLocal() as s:
        assert s.get(Domain, did).score_breakdown["whois_source"] == "rdap"


def test_funnel_rdap_decides_acquirability_for_non_bid_domain():
    """Денежный путь: домен без лейна (ручной список), RDAP 404 -> вердикт free -> лейн free —
    приобретаемость решил RDAP, а не источник."""
    did = _mk(domain="rdapfree.com", referring_domains=3000)
    rdap, ap = _Rdap([_FREE]), _FlakyAparser()
    out = scoring.score_domain(did, clients=_funnel_clients(rdap, ap))
    assert ap.calls == [] and out["reject_reason"] is None
    with db.SessionLocal() as s:
        d = s.get(Domain, did)
    assert d.lane == "free" and d.score_breakdown["whois_source"] == "rdap"


class _DownWayback:
    def classify_history(self, domain):
        raise RuntimeError("archive.org 503")


def test_funnel_wayback_down_does_not_reject_too_young_by_rdap_alone():
    """R2-1 сквозь воронку: RDAP — 1,5 года (у перехваченного домена это ПОСЛЕДНЯЯ регистрация),
    Wayback лежит. Вторая дата неизвестна — отказа too_young нет; после коммита домен «вслепую»:
    blind_reason про Wayback, пакет его не берёт."""
    reg = NOW - timedelta(days=550)
    did = _mk(domain="recaught.com", referring_domains=3000, lane="bid")
    rdap = _Rdap([{"exists": True, "status": ["pending delete"], "registered_at": reg}])
    out = scoring.score_domain(did, clients={**_funnel_clients(rdap, _FlakyAparser()),
                                             "wayback": _DownWayback()})
    assert rdap.calls == ["recaught.com"]
    assert out["reject_reason"] != "too_young" and "wayback:RuntimeError" in out["errors"]
    with db.SessionLocal() as s:
        d = s.get(Domain, did)
        assert "Wayback" in scoring.blind_reason(d) and scoring.bulk_ok(d) is False


def test_funnel_listed_pending_delete_domain_is_saved_as_bid_with_estimated_deadline():
    """R2-11 сквозь воронку: домен из списка (лейна нет), RDAP — pending delete. После коммита в
    строке домена лейн bid и оценка дедлайна (+5 суток): дальше его жизненный цикл закрывает
    обычный acquirability_verdict."""
    reg = NOW - timedelta(days=4000)
    did = _mk(domain="listed-drop.com", referring_domains=3000)
    rdap = _Rdap([{"exists": True, "status": ["pending delete"], "registered_at": reg}])
    scoring.score_domain(did, clients=_funnel_clients(rdap, _FlakyAparser()))
    with db.SessionLocal() as s:
        d = s.get(Domain, did)
    assert d.lane == "bid" and d.acquire_deadline is not None
    dl = d.acquire_deadline.replace(tzinfo=timezone.utc)      # SQLite отдаёт дату без пояса
    assert abs(dl - (NOW + timedelta(days=5))) < timedelta(minutes=5)
