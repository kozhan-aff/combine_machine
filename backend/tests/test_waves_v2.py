"""Волны v2 (t0 / avail / risk / links / history / deep) — юнит-тесты на фейках, без сети."""
from datetime import datetime, timedelta, timezone

import app.db as db
from app.models.domain import Domain
from app.services import scoring
from app.services.settings import get_settings

NOW = datetime.now(timezone.utc)


def _st(**kw):
    return {**get_settings(), **kw}


def _state(domain, source="nominet", lane="bid", **kw):
    return scoring.FunnelState(domain_id=0, domain=domain, lane=lane, referring_domains=None,
                               acquire_deadline=kw.pop("acquire_deadline", None),
                               feed_flags=kw.pop("feed_flags", None), source=source)


class FakeRdap:
    def __init__(self, zones=("com", "uk", "net", "org"), exists=False, registered=None, boom=False,
                 status=("pending delete",)):
        self.zones, self.exists, self.registered, self.boom = zones, exists, registered, boom
        self.status, self.calls = list(status), 0

    def has_rdap(self, d):
        return d.rsplit(".", 1)[-1] in self.zones

    def lookup(self, d):
        self.calls += 1
        if self.boom:
            raise RuntimeError("rdap down")
        return {"exists": self.exists, "status": self.status if self.exists else [],
                "registered_at": self.registered if self.exists else None}


class FakeAp:
    def __init__(self, available=True, created=None):
        self.available, self.created, self.calls = available, created, 0

    def whois_probe(self, d):
        self.calls += 1
        return {"available": self.available, "created": self.created}


class AgedWB:
    """Wayback-фейк для волны истории: чистая проверенная история, первый снимок `age` лет назад."""
    def __init__(self, age=9.0):
        self.age = age

    def classify_history(self, d):
        return {"prior_flags": {}, "first_seen": NOW - timedelta(days=int(365.25 * self.age)),
                "age_years": self.age, "wayback_checked": True, "sampled": 5, "evidence": []}


def test_t0_feed_flag_zone_brand():
    s = [_state("flag.com", feed_flags={"block": True}), _state("x.ru", source="list"),
         _state("nordvpn-deals.com", source="list"), _state("ok.com", source="list")]
    scoring._wave_t0(s, _st())
    assert s[0].reject_reason == "feed_flag"
    assert (s[1].reject_reason, s[1].alive) == ("tld_closed", False)
    assert s[2].reject_reason == "trademark" and s[2].sig["trademark_risk"] is True
    assert s[3].alive and s[3].reject_reason is None


def test_avail_rdap_free_name_gets_free_lane_without_aparser():
    s, ap = _state("new-name.com", source="list", lane=None), FakeAp()
    scoring._avail_one(s, {"rdap": FakeRdap(exists=False), "aparser": ap}, None, _st())
    assert s.alive and s.sig["lane"] == "free" and s.sig["whois_source"] == "rdap" and ap.calls == 0


def test_avail_bid_pending_delete_keeps_original_age():
    reg = datetime(1998, 7, 29, 4, tzinfo=timezone.utc)
    s = _state("pharmaindustrie.com", lane="bid", acquire_deadline=NOW + timedelta(days=2))
    scoring._avail_one(s, {"rdap": FakeRdap(exists=True, registered=reg), "aparser": FakeAp()}, None, _st())
    assert s.alive and s.sig["whois_created"] == reg
    assert s.sig["age_years"] > 25 and s.sig["age_source"] == "whois"


def test_avail_records_young_registration_but_never_rejects_too_young():
    """Р5: W2 возраст только записывает. Дата RDAP у перехваченного и снова дропающегося домена —
    ПОСЛЕДНЯЯ регистрация; судит W5 по старшей из двух дат."""
    reg = NOW - timedelta(days=200)
    s = _state("recaught.com", lane="bid", acquire_deadline=NOW + timedelta(days=2))
    scoring._avail_one(s, {"rdap": FakeRdap(exists=True, registered=reg), "aparser": FakeAp()},
                       None, _st(min_age_years=3.0))
    assert s.alive and s.reject_reason is None and s.sig["whois_created"] == reg


def test_avail_free_lane_taken_is_not_acquirable():
    s = _state("taken.com", source="mx", lane="free")
    scoring._avail_one(s, {"rdap": FakeRdap(exists=True, registered=NOW - timedelta(days=30)),
                           "aparser": FakeAp()}, None, _st())
    assert s.reject_reason == "not_acquirable"


def test_avail_listed_domain_in_pending_delete_becomes_bid():
    """1.12: вставка из ExpiredDomains — лейна нет, RDAP говорит «pending delete». Это дроп, а не
    чужой занятый домен: лейн bid, домен идёт дальше, а не висит taken_undated до самого дропа.
    Без статуса удаления тот же «занят» остаётся нерешённым — это чужой живой домен."""
    reg = NOW - timedelta(days=4000)
    s = _state("dropping.com", source="list", lane=None)
    scoring._avail_one(s, {"rdap": FakeRdap(exists=True, registered=reg), "aparser": FakeAp()},
                       None, _st())
    assert s.alive and s.lane == "bid" and s.sig["lane"] == "bid"
    live = _state("someones.com", source="list", lane=None)
    scoring._avail_one(live, {"rdap": FakeRdap(exists=True, registered=reg, status=()),
                              "aparser": FakeAp()}, None, _st())
    assert not live.alive and live.unresolved_why == "taken_undated"


def test_avail_budget_spent_only_on_aparser_zones():
    ap = FakeAp(available=True)
    c = {"rdap": FakeRdap(), "aparser": ap}
    s_com, s_mx = _state("libre.com", source="list", lane=None), _state("libre.mx", source="mx", lane="free")
    zero = scoring.Budget(0)
    scoring._avail_one(s_com, c, zero, _st())
    scoring._avail_one(s_mx, c, zero, _st())
    assert s_com.alive                                     # RDAP бесплатный — бюджет не нужен
    assert s_mx.unresolved_why == "budget" and ap.calls == 0
    s_mx2 = _state("libre2.mx", source="mx", lane="free")
    scoring._avail_one(s_mx2, c, scoring.Budget(1), _st())
    assert s_mx2.alive and s_mx2.sig["whois_source"] == "aparser" and ap.calls == 1


def test_avail_rdap_error_unresolves_non_bid_but_bid_continues():
    c = {"rdap": FakeRdap(boom=True), "aparser": FakeAp()}
    s1, s2 = _state("a.com", source="list", lane=None), _state("b.com", lane="bid")
    scoring._avail_one(s1, c, None, _st())
    scoring._avail_one(s2, c, None, _st())
    assert s1.unresolved_why == "whois_failed" and any(e.startswith("whois:") for e in s1.sig["errors"])
    assert s2.alive


def test_avail_rdap_circuit_opens_after_three_failures():
    """3.2, урок v1 (TCI): после 3 сбоев lookup ПОДРЯД канал до конца прогона не вызывается —
    домены сразу получают whois:circuit_open, без сети и без ретрай-шторма. Последовательно, без
    таймингов: счётчик живёт на инстансе клиента."""
    rdap = FakeRdap(boom=True)
    c = {"rdap": rdap, "aparser": FakeAp()}
    states = [_state(f"d{i}.com", source="list", lane=None) for i in range(5)]
    for s in states:
        scoring._avail_one(s, c, None, _st())
    assert rdap.calls == 3                                  # 4-й и 5-й — без сети
    assert [s.sig["errors"][-1] for s in states] == ["whois:RuntimeError"] * 3 + ["whois:circuit_open"] * 2
    assert all(s.unresolved_why == "whois_failed" for s in states)


def test_avail_naive_date_is_utc_and_broken_date_is_a_whois_failure():
    """3.6: наивная дата whois:43 считается UTC (раньше `now - wc` падал TypeError'ом, волна глотала
    исключение, и домен шёл дальше «живым без вердикта»); дата, которую не посчитать, — сбой whois."""
    s = _state("naive.mx", source="mx", lane="free")
    scoring._avail_one(s, {"rdap": FakeRdap(), "aparser": FakeAp(available=True, created=datetime(2010, 1, 1))},
                       None, _st())
    assert s.alive and s.sig["lane"] == "free" and s.sig["whois_created"].tzinfo is not None
    assert s.sig["age_years"] > 15
    bad = _state("broken.mx", source="mx", lane="free")
    scoring._avail_one(bad, {"rdap": FakeRdap(), "aparser": FakeAp(available=True, created="2010-01-01")},
                       None, _st())
    assert not bad.alive and bad.unresolved_why == "whois_failed"
    assert bad.sig["errors"] == ["whois:AttributeError"]


def test_history_age_is_the_older_of_rdap_and_first_snapshot():
    """Р5: перехваченный и снова дропающийся домен — RDAP 2023 (последняя регистрация), первый
    снимок ~15 лет назад. Возраст ~15 по архиву, отказа too_young нет. Молоды обе даты — отказ
    здесь, в волне истории."""
    s = _state("recaught.com")
    s.sig.update({"whois_created": datetime(2023, 1, 1, tzinfo=timezone.utc), "age_years": 3.0,
                  "age_source": "whois"})
    scoring._history_one(s, {"wayback": AgedWB(age=15.0)}, _st(min_age_years=5.0))
    assert s.alive and s.sig["age_years"] == 15.0 and s.sig["age_source"] == "wayback"
    young = _state("young.com")
    young.sig.update({"whois_created": NOW - timedelta(days=365), "age_years": 1.0,
                      "age_source": "whois"})
    scoring._history_one(young, {"wayback": AgedWB(age=1.0)}, _st(min_age_years=5.0))
    assert young.reject_reason == "too_young" and not young.alive


def test_history_wayback_down_does_not_judge_age_by_rdap_alone():
    """R2-1: Wayback не ответил (archive.org регулярно отдаёт 429/503) — вторая дата возраста
    НЕИЗВЕСТНА, а не «молода». Отказ too_young по одной дате RDAP окончателен и потерял бы
    перехваченный дроп; домен идёт дальше «вслепую», `wayback:` держит его вне пакета."""
    class DownWB:
        def classify_history(self, d):
            raise RuntimeError("archive.org 503")
    s = _state("recaught.com")
    s.sig.update({"whois_created": NOW - timedelta(days=550), "age_years": 1.5,
                  "age_source": "whois"})
    scoring._history_one(s, {"wayback": DownWB()}, _st(min_age_years=3.0))
    assert s.alive and s.reject_reason is None
    # age:unverified — этот домен при ручном перескоре иначе вернулся бы в пакет без улик
    assert s.sig["errors"] == ["wayback:RuntimeError", "age:unverified"]
    assert s.sig["age_source"] == "whois"


def test_history_emd_is_never_too_young():
    """R2-14: EMD — новорег, «молодость» — его суть: гейт too_young его не судит, даже если у
    имени был короткий прошлый сайт в архиве."""
    s = _state("mejorvpn.com", source="emd", lane="free")
    scoring._history_one(s, {"wayback": AgedWB(age=1.0)}, _st(min_age_years=5.0))
    assert s.alive and s.reject_reason is None


def test_avail_listed_domain_turned_bid_gets_estimated_deadline():
    """R2-11: домен из списка, которого W2 перевела в bid по статусу RDAP, без даты дропа никогда
    не закрылся бы обычным путём acquirability_verdict. Оценка: pending delete — 5 суток,
    redemption period (30 выкупа + 5 удаления) — 35. Известную дату дропа оценка не трогает."""
    reg = NOW - timedelta(days=4000)

    def turned(status, **kw):
        s = _state("dropping.com", source="list", lane=None, **kw)
        scoring._avail_one(s, {"rdap": FakeRdap(exists=True, registered=reg, status=status),
                               "aparser": FakeAp()}, None, _st())
        assert s.alive and s.lane == "bid"
        return s.sig.get("acquire_deadline")
    assert abs(turned(("pending delete",)) - (NOW + timedelta(days=5))) < timedelta(minutes=5)
    late = turned(("redemption period", "pending delete"))
    assert abs(late - (NOW + timedelta(days=35))) < timedelta(minutes=5)
    assert turned(("pending delete",), acquire_deadline=NOW + timedelta(days=2)) is None
    assert scoring.acquirability_verdict(False, NOW + timedelta(days=5), NOW + timedelta(days=8),
                                         lane="bid") == "taken"    # дедлайн прошёл — цикл закрыт


class _CleanAp(FakeAp):
    def safebrowsing_check(self, d):
        return False


class _DownWB:
    def classify_history(self, d):
        raise RuntimeError("archive.org 503")


def test_history_down_young_rdap_marks_age_unverified_and_keeps_domain_out_of_bulk():
    """Фикс ревью: ручной перескор домена, ранее отклонённого too_young (колонка wayback_checked
    осталась True), при упавшем Wayback не получает гейта молодости; без метки history_verdict =
    clean, blind_reason = None и молодой домен вернулся бы в пакет по ОТСУТСТВИЮ улик."""
    with db.SessionLocal() as ses:
        d = Domain(domain="rescored-young.com", source="nominet", status="rejected",
                   reject_reason="too_young", lane="bid", referring_domains=3000,
                   wayback_checked=True, prior_flags={}, score=0.0,
                   acquire_deadline=NOW + timedelta(days=2))
        ses.add(d); ses.commit(); did = d.id
    rdap = FakeRdap(exists=True, registered=NOW - timedelta(days=200))
    clients = {"rdap": rdap, "aparser": _CleanAp(), "wayback": _DownWB(),
               "rkn": type("R", (), {"is_listed": lambda self, x: False})(),
               "blacklist": type("B", (), {"is_blacklisted": lambda self, x: False})(),
               "searxng": type("S", (), {"indexed_echo": lambda self, x: True})()}
    out = scoring.score_domain(did, clients=clients)
    assert rdap.calls == 1                                  # RDAP реально звали, не NoRdap-путь
    assert out["reject_reason"] != "too_young" and "age:unverified" in out["errors"]
    with db.SessionLocal() as ses:
        d = ses.get(Domain, did)
        assert d.status == "scored" and d.wayback_checked is True
        assert "архив не ответил, а по RDAP домен моложе порога" in scoring.blind_reason(d)
        assert scoring.bulk_ok(d) is False


def test_history_down_emd_or_old_rdap_age_does_not_mark_age_unverified():
    emd = _state("mejorvpn.com", source="emd", lane="free")
    emd.sig.update({"whois_created": NOW - timedelta(days=100), "age_years": 0.3, "age_source": "whois"})
    scoring._history_one(emd, {"wayback": _DownWB()}, _st(min_age_years=5.0))
    old = _state("old.com")
    old.sig.update({"whois_created": NOW - timedelta(days=3650), "age_years": 10.0, "age_source": "whois"})
    scoring._history_one(old, {"wayback": _DownWB()}, _st(min_age_years=5.0))
    assert emd.sig["errors"] == ["wayback:RuntimeError"]
    assert old.sig["errors"] == ["wayback:RuntimeError"]
