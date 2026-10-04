"""Волны v2 (t0 / avail / risk / links / history / deep) — юнит-тесты на фейках, без сети."""
import json
import pathlib
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
               "searxng": type("S", (), {"indexed_echo": lambda self, x: True})(),
               "webrisk": type("WR", (), {"configured": True, "threats": lambda self, d: []})(),
               "ahrefs": type("Ah", (), {"units_left": lambda self: 2_000_000,
                                         "batch": lambda self, ds: {d: {} for d in ds}})()}
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


class FakeWR:
    def __init__(self, configured=True, threats=(), boom=False):
        self.configured, self._t, self.boom, self.calls = configured, list(threats), boom, 0

    def threats(self, d):
        self.calls += 1
        if self.boom:
            raise RuntimeError("webrisk down")
        return list(self._t)


class FakeBL:
    def __init__(self, listed=False):
        self.listed, self.calls = listed, 0

    def is_blacklisted(self, d):
        self.calls += 1
        return self.listed


def test_risk_without_webrisk_key_is_blind_not_rejected(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "SPAMHAUS_DQS_KEY", "")
    s, bl = _state("ok.com"), FakeBL()
    scoring._risk_one(s, {"webrisk": FakeWR(configured=False), "blacklist": bl})
    assert s.alive and "webrisk:not_configured" in s.sig["errors"]
    assert bl.calls == 0                                   # бесплатный Spamhaus — только некоммерческий


def test_risk_threat_rejects_as_blacklist_but_leaves_blacklisted_column_alone():
    """1.5: угроза Web Risk — улика в `webrisk_threats`; колонку `blacklisted` (сигнал Spamhaus)
    Web Risk не пишет: без DQS её никто бы не перепроверил, и чистый ответ её потом не снял бы."""
    s = _state("bad.com")
    scoring._risk_one(s, {"webrisk": FakeWR(threats=["MALWARE"]), "blacklist": FakeBL()})
    assert s.reject_reason == "blacklist" and s.sig["webrisk_threats"] == ["MALWARE"]
    assert "blacklisted" not in s.sig


def test_risk_webrisk_error_is_recorded_domain_stays():
    s = _state("ok.com")
    scoring._risk_one(s, {"webrisk": FakeWR(boom=True), "blacklist": FakeBL()})
    assert s.alive and "webrisk:RuntimeError" in s.sig["errors"]


def test_risk_webrisk_circuit_opens_after_three_failures():
    """3.2: лежащий Web Risk — после 3 сбоев ПОДРЯД без сети до конца прогона (счётчик на
    инстансе клиента, детерминированно, без таймингов). Домены едут дальше «вслепую»."""
    wr = FakeWR(boom=True)
    states = [_state(f"d{i}.com") for i in range(5)]
    for s in states:
        scoring._risk_one(s, {"webrisk": wr, "blacklist": FakeBL()})
    assert wr.calls == 3
    assert [s.sig["errors"][-1] for s in states] == ["webrisk:RuntimeError"] * 3 + ["webrisk:circuit_open"] * 2
    assert all(s.alive for s in states)


def test_risk_dqs_key_enables_spamhaus(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "SPAMHAUS_DQS_KEY", "k")
    s, bl = _state("spam.com"), FakeBL(listed=True)
    scoring._risk_one(s, {"webrisk": FakeWR(), "blacklist": bl})
    assert s.reject_reason == "blacklist" and bl.calls == 1 and s.sig["blacklisted"] is True


def test_decide_never_auto_approves_with_risk_error():
    sig = {"wayback_checked": True, "age_years": 9, "deep_checked": True,
           "errors": ["webrisk:not_configured"]}
    assert scoring._decide(0.95, sig, 0.4) == "scored"            # Р2: одобряет только человек


def test_blind_reason_names_webrisk_and_keeps_domain_out_of_bulk():
    d = Domain(domain="blind.com", wayback_checked=True, prior_flags={}, age_years=9.0,
               score_breakdown={"errors": ["webrisk:not_configured"], "history_evidence": []})
    assert "Web Risk" in scoring.blind_reason(d)
    assert scoring.bulk_ok(d) is False


def test_make_clients_has_every_breaker_lock():
    """Находка R2-15, урок v1 (2026-07-21: при переходе на волны лок получили не все клиенты риска):
    предохранитель без своего лока в _make_clients — тихая гонка на счётчике под 12 потоками волны,
    а сьют зелёный (фейки передают клиентов сами). Каждый предохранитель — свой лок; новый
    (Задача 12: _llm_lock) дописывается сюда."""
    c = scoring._make_clients()
    for lock in ("_whois_lock", "_rdap_lock", "_webrisk_lock", "_llm_lock"):
        assert hasattr(c.get(lock), "acquire"), lock


def test_webrisk_breaker_locks_both_the_gate_check_and_the_increment():
    """R2-15, урок v1: гонку на счётчике предохранителя ловит детерминированный спай-лок, а не
    тайминг. Каждая из 3 попыток Web Risk до срабатывания берёт `_webrisk_lock` дважды (гейт-чек и
    инкремент), 4-я — один раз (гейт-чек: канал уже закрыт). Пропуск любого входа — непокрытая
    гонка под 12 потоками волны risk."""
    import threading

    class SpyLock:
        def __init__(self):
            self._real, self.enters = threading.Lock(), 0

        def __enter__(self):
            self._real.acquire()
            self.enters += 1

        def __exit__(self, *a):
            self._real.release()
    lock, wr = SpyLock(), FakeWR(boom=True)
    for i in range(4):
        scoring._risk_one(_state(f"d{i}.com"), {"webrisk": wr, "blacklist": FakeBL(),
                                               "_webrisk_lock": lock})
    assert wr.calls == 3 and wr.threat_failures == 3
    assert lock.enters == 3 * 2 + 1


# --- W4 «ссылки» (Задача 11) -------------------------------------------------------------------

ROW = {"domain_rating": 0.0, "refdomains": 717, "refdomains_dofollow": 358, "refips_subnets": 198,
       "backlinks": 801, "org_traffic": 0}


class FakeAh:
    """Ahrefs API: batch (W4), остаток units (пол Р3), анкоры и история трафика (W6, Задача 13).
    `boom` роняет batch и anchors, `history_boom` — только metrics_history."""
    def __init__(self, data=None, boom=False, anchors=None, history=None, history_boom=False,
                 units=2_000_000):
        self.data, self.boom, self.history_boom, self.units = data or {}, boom, history_boom, units
        self._anchors, self._history = anchors or [], history or []
        self.batches, self.deep_calls, self.units_calls = [], 0, 0

    def units_left(self):
        self.units_calls += 1
        return self.units

    def batch(self, domains):
        self.batches.append(list(domains))
        if self.boom:
            raise RuntimeError("ahrefs down")
        return {d: self.data[d] for d in domains if d in self.data}

    def anchors(self, d, limit=50):
        self.deep_calls += 1
        if self.boom:
            raise RuntimeError("ahrefs down")
        return self._anchors

    def metrics_history(self, d, years=5, today=None):
        if self.history_boom:
            raise RuntimeError("history down")
        return self._history


def _mk(domain, source="nominet", lane="bid", deadline=None, **kw):
    with db.SessionLocal() as s:
        d = Domain(domain=domain, source=source, lane=lane, status="discovered",
                   acquire_deadline=deadline, **kw)
        s.add(d)
        s.commit()
        return d.id


def _full_clients(rdap, ah, wb, **extra):
    """Клиенты всей воронки на фейках: Web Risk настроен и чист, Spamhaus без DQS не зовётся."""
    return {"rdap": rdap, "aparser": FakeAp(), "webrisk": FakeWR(), "blacklist": FakeBL(),
            "ahrefs": ah, "wayback": wb, **extra}


def test_links_fills_signals_and_rejects_low_rd():
    a, b = _state("a.com"), _state("b.com")
    ah = FakeAh({"a.com": ROW, "b.com": {**ROW, "refdomains": 0}})
    scoring._wave_links([a, b], {"ahrefs": ah}, _st(min_referring_domains=1), None, None)
    assert a.alive and a.sig["referring_domains"] == 717 and a.sig["ref_subnets"] == 198
    assert a.sig["dr"] == 0.0 and a.sig["organic_traffic"] == 0
    assert b.reject_reason == "low_rd"


def test_links_batches_of_100_skip_emd_and_unresolve_missing():
    """Строки домена нет в ответе — как сбой: unresolved до следующего прогона, а не «вслепую» дальше
    (без RD скор ушёл бы в low_score навсегда)."""
    states = [_state(f"d{i}.com") for i in range(150)] + [_state("emd.com", source="emd", lane="free")]
    ah = FakeAh({})
    scoring._wave_links(states, {"ahrefs": ah}, _st(), None, None)
    assert [len(b) for b in ah.batches] == [100, 50]
    assert all("emd.com" not in b for b in ah.batches)
    assert states[0].unresolved_why == "ahrefs_missing" and not states[0].alive
    assert states[0].sig["errors"] == ["ahrefs:missing"] and states[150].alive


def test_links_budget_overflow_is_unresolved_not_judged_blind():
    states = [_state(f"d{i}.com") for i in range(3)]
    scoring._wave_links(states, {"ahrefs": FakeAh({})}, _st(), scoring.Budget(2), None)
    assert states[2].unresolved_why == "links_budget" and not states[2].alive


def test_links_batch_error_unresolves_rest_and_stops_sending():
    """1.8: упавшая пачка не «вслепую дальше» (без RD скор ниже порога -> low_score навсегда),
    а unresolved до следующего прогона. Следующие пачки не шлются: протухший ключ даёт 401 на каждой."""
    states = [_state(f"d{i}.com") for i in range(150)]
    ah = FakeAh(boom=True)
    scoring._wave_links(states, {"ahrefs": ah}, _st(), None, None)
    assert len(ah.batches) == 1
    assert all(s.unresolved_why == "ahrefs_failed" and not s.alive for s in states)
    assert states[149].sig["errors"] == ["ahrefs:RuntimeError"]


def test_links_empty_dr_does_not_erase_dr_from_discovery():
    """4.12: DR пришёл из discovery (строка домена), Ahrefs в W4 поля не отдал — пустое значение
    сохранённый DR не затирает."""
    did = _mk("dr-kept.com", deadline=NOW + timedelta(days=2), dr=12)
    ah = FakeAh({"dr-kept.com": {**ROW, "domain_rating": None}})
    scoring.score_domain(did, clients=_full_clients(
        FakeRdap(exists=True, registered=NOW - timedelta(days=4000)), ah, AgedWB()))
    with db.SessionLocal() as s:
        d = s.get(Domain, did)
        assert float(d.dr) == 12.0 and d.referring_domains == 717


def test_links_batch_error_keeps_domain_discovered_and_unstamped():
    """1.8 + 2.7: сбой Ahrefs — домен остаётся discovered и БЕЗ отметки сверки занятости:
    `scorable` вернёт free-лейн только через сутки после отметки, а оценить его надо следующим
    прогоном."""
    did = _mk("libre.mx", source="mx", lane="free")
    out = scoring.score_domain(did, clients=_full_clients(FakeRdap(), FakeAh(boom=True), AgedWB()))
    assert out["unresolved"] is True and out["why"] == "ahrefs_failed"
    with db.SessionLocal() as s:
        d = s.get(Domain, did)
        assert d.status == "discovered" and d.acquirability_checked_at is None


def test_links_missing_row_keeps_domain_discovered_and_unstamped():
    """Строки домена нет в ответе batch: домен остаётся discovered и без отметки сверки занятости —
    оценится следующим прогоном (решение координатора 2026-10-02)."""
    did = _mk("ghost-row.mx", source="mx", lane="free")
    out = scoring.score_domain(did, clients=_full_clients(FakeRdap(), FakeAh({}), AgedWB()))
    assert out["unresolved"] is True and out["why"] == "ahrefs_missing"
    with db.SessionLocal() as s:
        d = s.get(Domain, did)
        assert d.status == "discovered" and d.acquirability_checked_at is None


def test_score_pending_takes_links_cap_from_settings(monkeypatch):
    """2.9: кап W4 — из /settings. Кап 1 и два домена -> один оценён, второй ждёт следующего прогона."""
    from app.services.settings import update_settings
    update_settings(max_links_per_run=1)
    for name in ("cap-a.com", "cap-b.com"):
        _mk(name, deadline=NOW + timedelta(days=2))
    ah = FakeAh({"cap-a.com": ROW, "cap-b.com": ROW})
    clients = _full_clients(FakeRdap(exists=True, registered=NOW - timedelta(days=4000)), ah, AgedWB())
    monkeypatch.setattr(scoring, "_make_clients", lambda: clients)
    scoring.score_pending(limit=10)
    assert [len(b) for b in ah.batches] == [1]
    with db.SessionLocal() as s:
        left = [d.domain for d in s.query(Domain).filter(Domain.status == "discovered")]
    assert len(left) == 1


def test_units_floor_skips_paid_wave_and_says_why(monkeypatch):
    """Р3: остаток units ниже пола -> W4 не тратит ничего, домены ждут следующего прогона (без
    отметки сверки), причина — в сообщении задачи. Остаток неизвестен (None) — то же самое.
    R2-10: решено ОДИН раз в начале прогона — W2 (RDAP) по таким доменам даже не ходила."""
    from app.services import jobs
    for units in (100_000, None):
        name = f"floor-{units}.com"
        did = _mk(name, deadline=NOW + timedelta(days=2))
        ah = FakeAh({name: ROW}, units=units)
        rdap = FakeRdap(exists=True, registered=NOW - timedelta(days=4000))
        clients = _full_clients(rdap, ah, AgedWB())
        monkeypatch.setattr(scoring, "_make_clients", lambda: clients)
        scoring.score_pending(limit=10)
        assert ah.batches == [] and ah.units_calls == 1 and rdap.calls == 0, units
        with db.SessionLocal() as s:
            d = s.get(Domain, did)
            assert d.status == "discovered" and d.acquirability_checked_at is None, units
    msg = jobs.last("score")["message"]
    assert "Ahrefs: остаток units неизвестен — платные волны пропущены" in msg, msg


def test_units_floor_message_and_zero_floor_means_no_floor(monkeypatch):
    from app.services import jobs
    from app.services.settings import update_settings
    did = _mk("low.com", deadline=NOW + timedelta(days=2))
    ah = FakeAh({"low.com": ROW}, units=100_000)
    clients = _full_clients(FakeRdap(exists=True, registered=NOW - timedelta(days=4000)), ah, AgedWB())
    monkeypatch.setattr(scoring, "_make_clients", lambda: clients)
    scoring.score_pending(limit=10)
    assert "Ahrefs: остаток 100 000 < пола 300 000 — платные волны пропущены" in jobs.last("score")["message"]
    update_settings(units_floor=0)                     # 0 — пола нет: остаток даже не спрашиваем
    ah.units_calls = 0
    scoring.score_pending(limit=10)
    assert ah.units_calls == 0 and ah.batches == [["low.com"]]
    with db.SessionLocal() as s:
        assert s.get(Domain, did).status == "scored"


def test_links_no_key_skips_avail_and_risk_for_non_emd(monkeypatch):
    """R2-10: платные волны не пойдут (ключа Ahrefs нет) — это известно ДО W2/W3. Не-EMD домен без
    W4 не решается, и RDAP с Web Risk за него тратились бы впустую на каждом свипе. Решено один раз
    после W0: домен ждёт следующего прогона без отметки сверки; EMD идёт как обычно (W4 у него нет)."""
    from app.integrations.ahrefs import AhrefsClient
    from app.services import jobs
    did = _mk("nokey.com", deadline=NOW + timedelta(days=2))
    _mk("nokey-emd.com", source="emd", lane="free")
    rdap, wr = FakeRdap(), FakeWR()
    clients = {**_full_clients(rdap, AhrefsClient(api_key=""), AgedWB()), "webrisk": wr}
    monkeypatch.setattr(scoring, "_make_clients", lambda: clients)
    scoring.score_pending(limit=10)
    assert rdap.calls == 1 and wr.calls == 1                # только EMD
    with db.SessionLocal() as s:
        d = s.get(Domain, did)
        assert d.status == "discovered" and d.acquirability_checked_at is None
    msg = jobs.last("score")["message"]
    assert "Ahrefs: ключ AHREFS_API_KEY не задан — платные волны пропущены" in msg, msg


def test_score_pending_selects_non_emd_up_to_links_cap(monkeypatch):
    """R2-10: домен сверх капа W4 всё равно ушёл бы в links_budget, оплатив W2/W3 (RDAP/whois:43,
    Web Risk). Выборка берёт не-EMD доменов не больше капа; остаток лимита добирают EMD — W4 у них
    нет."""
    from app.services.settings import update_settings
    update_settings(max_links_per_run=1)
    for name in ("sel-a.com", "sel-b.com"):
        _mk(name, deadline=NOW + timedelta(days=2))
    _mk("sel-emd.com", source="emd", lane="free")
    seen = []
    # гейт открыт (ключ+остаток): реальный клиент без ключа закрыл бы его и обнулил кап не-EMD
    monkeypatch.setattr(scoring, "_make_clients", lambda: {"ahrefs": FakeAh()})
    monkeypatch.setattr(scoring, "_run_waves",
                        lambda states, *a, **kw: seen.extend(s.domain for s in states) or [])
    scoring.score_pending(limit=10)
    assert len(seen) == 2 and "sel-emd.com" in seen


def test_links_batch_error_reason_goes_to_job_message(monkeypatch):
    """R2-9: причина сбоя пачки W4 — в сообщении задачи, с HTTP-кодом (401/403 — ключ не принят,
    400 — кривой запрос), а не только в логе скора: иначе оператор видит «прогнано N», а домены
    молча висят в поиске."""
    import httpx
    from app.services import jobs

    class Ah401(FakeAh):
        def batch(self, domains):
            self.batches.append(list(domains))
            raise httpx.HTTPStatusError("401", request=httpx.Request("POST", "https://api.ahrefs.com/v3"),
                                        response=httpx.Response(401))
    _mk("key-gone.com", deadline=NOW + timedelta(days=2))
    clients = _full_clients(FakeRdap(exists=True, registered=NOW - timedelta(days=4000)), Ah401(), AgedWB())
    monkeypatch.setattr(scoring, "_make_clients", lambda: clients)
    scoring.score_pending(limit=10)
    msg = jobs.last("score")["message"]
    assert "Ahrefs W4: HTTPStatusError 401 — 1 доменов ждут следующего прогона" in msg, msg


def test_links_wave_cancel_between_batches():
    from app.services import jobs
    states = [_state(f"c{i}.com") for i in range(150)]
    ah = FakeAh({})
    with jobs.track("score", stages=[dict(x) for x in scoring.FUNNEL_STAGES]) as run:
        jobs.request_cancel("score")
        scoring._wave_links(states, {"ahrefs": ah}, _st(), None, run)
    assert len(ah.batches) == 1 and jobs.last("score")["status"] == "cancelled"


def test_single_score_flash_names_paid_wave_reasons(client, monkeypatch):
    """«▶ перепроверить» один домен: причина платной волны названа, а не «приобретаемость не
    определена» — занятость тут ни при чём."""
    from urllib.parse import unquote
    did = _mk("flash.com")
    for why, words in (("ahrefs_failed", "Ahrefs не ответил"), ("units_floor", "ниже пола"),
                       ("ahrefs_missing", "не вернул данных"), ("ahrefs_no_key", "ключ Ahrefs")):
        monkeypatch.setattr(scoring, "score_domain", lambda domain_id, why=why: {
            "domain": "flash.com", "status": "discovered", "unresolved": True, "why": why})
        loc = unquote(client.post(f"/domains/{did}/score", follow_redirects=False).headers["location"])
        assert words in loc, loc


def test_paid_unresolved_after_avail_persists_bid_lane_with_estimated_deadline():
    """W2 перевела ручной домен без лейна в bid по статусу RDAP «pending delete» и оценила дедлайн,
    затем W4 не смогла (сбой Ahrefs) -> unresolved. Порядок волн делает это достижимым (W4 идёт после
    W2 для не-EMD). Без записи лейна в БД остались бы lane=NULL + будущий дедлайн: `scorable` не
    пускает такой домен до самого дропа, и оценка ждала бы его вместо следующего прогона."""
    from sqlalchemy import select
    did = _mk("pending-del.com", source="list", lane=None)
    rdap = FakeRdap(exists=True, registered=NOW - timedelta(days=4000))
    out = scoring.score_domain(did, clients=_full_clients(rdap, FakeAh(boom=True), AgedWB()))
    assert out["unresolved"] is True and out["why"] == "ahrefs_failed"
    with db.SessionLocal() as s:
        d = s.get(Domain, did)
        assert d.status == "discovered" and d.lane == "bid" and d.acquire_deadline is not None
        assert d.acquirability_checked_at is None
        # следующий свип видит домен: оценка ждёт не дропа, а ближайшего прогона
        assert s.scalar(select(Domain.id).where(Domain.id == did, scoring.scorable(NOW))) == did


def test_paid_gate_unresolved_precedes_avail_so_no_lane_or_deadline_is_written(monkeypatch):
    """Пинит предпосылку, на которой держится комментарий в ветке unresolved: платные причины
    пол/ключ решаются ДО W2 (порядок волн), поэтому лейн/оценка дедлайна им недоступны и лейн не
    пишется. Сломает тест перестановка `_paid_gate` после W2."""
    did = _mk("gate-first.com", source="list", lane=None)
    rdap = FakeRdap(exists=True, registered=NOW - timedelta(days=4000))
    clients = _full_clients(rdap, FakeAh(units=1), AgedWB())
    monkeypatch.setattr(scoring, "_make_clients", lambda: clients)
    scoring.score_pending(limit=10)
    assert rdap.calls == 0
    with db.SessionLocal() as s:
        d = s.get(Domain, did)
        assert d.status == "discovered" and d.lane is None and d.acquire_deadline is None


def test_units_left_is_asked_once_per_run_when_floor_passes(monkeypatch):
    """Р3/R2-10: остаток units спрашивается ОДИН раз за прогон — в `_paid_gate` после W0, а не
    на каждой платной волне/пачке (лишний запрос на каждом домене свипа раз в час). Гард «один раз»
    на ПРОХОДЯЩЕМ пути: на падающем волна по таким доменам не идёт и второй вопрос не задала бы.
    W6 (Задача 13) спрашивает остаток ЕЩЁ раз — свежим запросом перед своей волной (W4 уже
    потратила units), поэтому здесь её кандидатов нет: порог manual_review_at недостижим."""
    from app.services.settings import update_settings
    update_settings(manual_review_at=1.0)
    for name in ("once-a.com", "once-b.com"):
        _mk(name, deadline=NOW + timedelta(days=2))
    ah = FakeAh({"once-a.com": ROW, "once-b.com": ROW})
    clients = _full_clients(FakeRdap(exists=True, registered=NOW - timedelta(days=4000)), ah, AgedWB())
    monkeypatch.setattr(scoring, "_make_clients", lambda: clients)
    scoring.score_pending(limit=10)
    assert ah.units_calls == 1 and [len(b) for b in ah.batches] == [2]


# --- платный гейт решается ДО выборки score_pending (fix round 1) -------------------------------

def _closed_gate_pool(monkeypatch, ah, rdap=None, n=25):
    """n не-EMD + 1 EMD, лимит выборки 20: до фикса не-EMD забирали весь лимит и EMD не получал слота."""
    ids = [_mk(f"held{i}.com", deadline=NOW + timedelta(days=2)) for i in range(n)]
    emd = _mk("emd-slot.com", source="emd", lane="free")
    rdap = rdap or FakeRdap()
    clients = _full_clients(rdap, ah, AgedWB())
    monkeypatch.setattr(scoring, "_make_clients", lambda: clients)
    return ids, emd, rdap


def _assert_held_and_emd_scored(ids, emd):
    with db.SessionLocal() as s:
        assert s.get(Domain, emd).status != "discovered"            # EMD получил слот
        held = [s.get(Domain, i) for i in ids]
        assert all(d.status == "discovered" and d.acquirability_checked_at is None for d in held)


def test_closed_gate_no_key_still_gives_emd_a_slot(monkeypatch):
    """I1: ключа нет -> не-EMD всё равно ждут (W4 им не светит), и они НЕ должны съедать лимит
    выборки: иначе каждый свип берёт тот же набор, а EMD (W4 ему не нужна) не доходит никогда."""
    from app.integrations.ahrefs import AhrefsClient
    from app.services import jobs
    ids, emd, rdap = _closed_gate_pool(monkeypatch, AhrefsClient(api_key=""))
    scoring.score_pending(limit=20)
    _assert_held_and_emd_scored(ids, emd)
    assert rdap.calls == 1                                          # только EMD
    assert "Ahrefs: ключ AHREFS_API_KEY не задан — платные волны пропущены" in jobs.last("score")["message"]


def test_closed_gate_units_floor_still_gives_emd_a_slot_and_asks_units_once(monkeypatch):
    from app.services import jobs
    ah = FakeAh(units=100_000)
    ids, emd, rdap = _closed_gate_pool(monkeypatch, ah)
    scoring.score_pending(limit=20)
    _assert_held_and_emd_scored(ids, emd)
    assert ah.units_calls == 1 and ah.batches == [] and rdap.calls == 1
    assert "Ahrefs: остаток 100 000 < пола 300 000 — платные волны пропущены" in jobs.last("score")["message"]


def test_open_gate_selection_unchanged_and_units_asked_once(monkeypatch):
    """Гейт открыт: прежнее поведение — не-EMD в пределах капа W4, один запрос остатка за прогон."""
    from app.services.settings import update_settings
    update_settings(max_links_per_run=2, manual_review_at=1.0)   # W6 без кандидатов: её свежий запрос units не в счёт
    ah = FakeAh({f"held{i}.com": ROW for i in range(3)})
    ids, emd, rdap = _closed_gate_pool(monkeypatch, ah, rdap=FakeRdap(
        exists=True, registered=NOW - timedelta(days=4000)), n=3)
    scoring.score_pending(limit=20)
    assert ah.units_calls == 1 and [len(b) for b in ah.batches] == [2]
    with db.SessionLocal() as s:
        left = [s.get(Domain, i).status for i in ids]
        assert left.count("discovered") == 1 and s.get(Domain, emd).status != "discovered"


# --- W5 «история + тема» (Задача 12) -----------------------------------------------------------

class FakeWB:
    """Wayback с текстами прочитанных снимков (для темы W5)."""
    def __init__(self, dirty=False, checked=True, texts=None, age=9.0):
        self.dirty, self.checked, self.age = dirty, checked, age
        self.texts = texts if texts is not None else [{"timestamp": "20190101000000", "text": "vpn reviews"}]

    def classify_history(self, d):
        flags = {c: False for c in ("adult", "pharma", "casino", "gambling", "spam")}
        flags["casino"] = self.dirty
        return {"prior_flags": flags, "first_seen": None, "age_years": self.age,
                "wayback_checked": self.checked, "sampled": 5, "evidence": [], "texts": self.texts}


class FakeLLM:
    def __init__(self, answer=None, boom=False):
        self.answer, self.boom, self.calls = answer, boom, 0

    def complete(self, system, prompt, **kw):
        self.calls += 1
        if self.boom:
            raise RuntimeError("llm down")
        return self.answer or ('{"snapshots":[{"year":2019,"lang":"pl","topic":"vpn","parked":false}],'
                               '"topic_summary":"vpn blog","vpn_adjacent":0.8}')


def test_history_llm_fills_soft_signals():
    s = _state("a.com")
    scoring._history_one(s, {"wayback": FakeWB(), "llm": FakeLLM()}, _st())
    assert s.alive and s.sig["market_lang"] == "pl" and s.sig["topical_relevance"] == 0.8
    assert s.sig["topic"] == "vpn blog" and "topic_unknown" not in s.sig


def test_history_llm_failure_is_soft():
    s = _state("a.com")
    scoring._history_one(s, {"wayback": FakeWB(), "llm": FakeLLM(boom=True)}, _st())
    assert s.alive and s.sig.get("topic_unknown") is True and not s.sig["errors"]


def test_dirty_history_rejects_before_llm():
    s, llm = _state("a.com"), FakeLLM()
    scoring._history_one(s, {"wayback": FakeWB(dirty=True), "llm": llm}, _st())
    assert s.reject_reason == "history_dirty" and llm.calls == 0


def test_llm_circuit_opens_after_three_failures():
    """3.3: лежащий LiteLLM — после 3 сбоев ПОДРЯД без вызова до конца прогона (счётчик на
    инстансе клиента, детерминированно, без таймингов). Домены едут дальше с «тема не определена»."""
    llm = FakeLLM(boom=True)
    states = [_state(f"t{i}.com") for i in range(5)]
    for s in states:
        scoring._history_one(s, {"wayback": FakeWB(), "llm": llm}, _st())
    assert llm.calls == 3
    assert all(s.alive and s.sig["topic_unknown"] is True and not s.sig["errors"] for s in states)


def test_llm_breaker_locks_both_the_gate_check_and_the_increment():
    """R2-15, урок v1: гонку на счётчике предохранителя ловит детерминированный спай-лок, а не
    тайминг. Каждая из 3 попыток LLM до срабатывания берёт `_llm_lock` дважды (гейт-чек и
    инкремент), 4-я — один раз (гейт-чек: канал уже закрыт). Пропуск любого входа — непокрытая
    гонка под 4 потоками волны истории."""
    import threading

    class SpyLock:
        def __init__(self):
            self._real, self.enters = threading.Lock(), 0

        def __enter__(self):
            self._real.acquire()
            self.enters += 1

        def __exit__(self, *a):
            self._real.release()
    lock, llm = SpyLock(), FakeLLM(boom=True)
    for i in range(4):
        scoring._history_one(_state(f"t{i}.com"), {"wayback": FakeWB(), "llm": llm, "_llm_lock": lock},
                             _st())
    assert llm.calls == 3 and llm.classify_failures == 3
    assert lock.enters == 3 * 2 + 1


def test_emd_keeps_market_lang_of_its_set():
    """4.4: язык EMD — язык рынка набора (discovery); снимки прошлого сайта его не перезаписывают."""
    s = _state("mejorvpn.com", source="emd", lane="free")
    scoring._history_one(s, {"wayback": FakeWB(), "llm": FakeLLM()}, _st())
    assert "market_lang" not in s.sig and s.sig["topic"] == "vpn blog"


def test_llm_failure_on_rescore_clears_topic_but_keeps_relevance():
    """4.5: перескор со сбоем LLM не оставляет старую тему — «тема не определена» и в базе. А
    близость к VPN (0.1) остаётся: перескор при лежащем LLM не отмывает «тема далека от VPN»."""
    did = _mk("old-topic.com", deadline=NOW + timedelta(days=2), topic="casino reviews",
              topical_relevance=0.1, market_lang="en")
    scoring.score_domain(did, clients=_full_clients(
        FakeRdap(exists=True, registered=NOW - timedelta(days=4000)), FakeAh({"old-topic.com": ROW}),
        FakeWB(), llm=FakeLLM(boom=True)))
    with db.SessionLocal() as s:
        d = s.get(Domain, did)
        assert d.topic is None and float(d.topical_relevance) == 0.1
        assert d.score_breakdown["topic_unknown"] is True and d.market_lang == "en"


def test_llm_topic_reaches_the_domain_row():
    did = _mk("pl-blog.com", deadline=NOW + timedelta(days=2))
    scoring.score_domain(did, clients=_full_clients(
        FakeRdap(exists=True, registered=NOW - timedelta(days=4000)), FakeAh({"pl-blog.com": ROW}),
        FakeWB(), llm=FakeLLM()))
    with db.SessionLocal() as s:
        d = s.get(Domain, did)
        assert (d.market_lang, d.topic, float(d.topical_relevance)) == ("pl", "vpn blog", 0.8)
        assert d.score_breakdown["topic_unknown"] is None


def test_llm_not_asked_for_domain_rejected_too_young():
    """Гард «только выжившим»: молодой не-EMD отклонён too_young -> LLM не зовётся."""
    s, llm = _state("young.com"), FakeLLM()
    scoring._history_one(s, {"wayback": FakeWB(age=0.5), "llm": llm}, _st())
    assert s.reject_reason == "too_young" and llm.calls == 0


def test_llm_not_asked_when_history_not_checked():
    """Гард «только по проверенной истории»: wayback_checked=False -> LLM не зовётся."""
    s, llm = _state("a.com"), FakeLLM()
    scoring._history_one(s, {"wayback": FakeWB(checked=False), "llm": llm}, _st())
    assert llm.calls == 0 and "topic_unknown" not in s.sig


# --- W6 «анкоры», правила пакета, EMD (Задача 13) ---------------------------------------------

FX = pathlib.Path(__file__).parent / "fixtures" / "v2"
SPAM = json.loads((FX / "ahrefs_anchors_spam.json").read_text())["anchors"]
CLEAN = [{"anchor": "goodvpnblog.com", "refdomains": 80, "is_spam": False},
         {"anchor": "VPN speed test results", "refdomains": 40, "is_spam": False}]
HIST = json.loads((FX / "ahrefs_metrics_history.json").read_text())["metrics"]
STRONG = {**ROW, "domain_rating": 35.0, "refdomains": 900, "refips_subnets": 700}


def _strong(s):
    s.sig.update({"wayback_checked": True, "age_years": 10.0, "dr": 35.0, "referring_domains": 900,
                  "ref_subnets": 700, "topical_relevance": 0.9})
    return s


def test_deep_spam_rejects_and_records():
    s = _strong(_state("a.com"))
    scoring._wave_deep([s], {"ahrefs": FakeAh(anchors=SPAM, history=HIST)}, _st(), None, None)
    assert s.reject_reason == "spam_anchors" and s.sig["deep_checked"] is True
    assert s.sig["spam_anchors"] is True and s.sig["spam_anchor_ratio"] == 1.0
    assert len(s.sig["anchors"]) <= 10


def test_deep_clean_keeps_and_sets_peak():
    s = _strong(_state("a.com"))
    scoring._wave_deep([s], {"ahrefs": FakeAh(anchors=CLEAN, history=HIST)}, _st(), None, None)
    assert s.alive and s.sig["deep_checked"] is True and s.sig["peak_traffic"] == 1850
    assert s.sig["spam_anchors"] is False


def test_deep_only_for_promising_best_first_within_cap():
    weak = _state("weak.com")                       # пустые сигналы -> предварительный скор < manual_review_at
    good, better = _strong(_state("good.com")), _strong(_state("better.com"))
    good.sig["dr"] = 10.0                           # DR зажат на 30 — разница должна быть НИЖЕ потолка
    emd = _strong(_state("emd.com", source="emd", lane="free"))
    ah = FakeAh(anchors=CLEAN, history=HIST)
    scoring._wave_deep([weak, good, better, emd], {"ahrefs": ah}, _st(), scoring.Budget(1), None)
    assert better.sig["deep_checked"] is True                 # лучший предварительный — первым
    assert good.sig["deep_checked"] is False and weak.sig["deep_checked"] is False
    assert "deep_checked" not in emd.sig and ah.deep_calls == 1


def test_deep_prescore_uses_runtime_threshold():
    """2.8: кандидатов W6 отбирает рантайм-порог manual_review_at из /settings, а не статичный."""
    s, ah = _strong(_state("a.com")), FakeAh(anchors=CLEAN, history=HIST)
    scoring._wave_deep([s], {"ahrefs": ah}, _st(manual_review_at=0.99), None, None)
    assert ah.deep_calls == 0 and s.sig["deep_checked"] is False
    weak, ah2 = _state("weak.com"), FakeAh(anchors=CLEAN)
    weak.sig["wayback_checked"] = True
    scoring._wave_deep([weak], {"ahrefs": ah2}, _st(manual_review_at=0.0), None, None)
    assert ah2.deep_calls == 1


def test_deep_skips_domain_without_checked_history():
    """W6 не тратит units на домен с непроверенной историей: в пакет он и так не попадёт
    («вслепую»), а человек сначала разберётся с историей (решение координатора 2026-10-02)."""
    s, ah = _strong(_state("a.com")), FakeAh(anchors=CLEAN, history=HIST)
    s.sig["wayback_checked"] = False
    scoring._wave_deep([s], {"ahrefs": ah}, _st(), None, None)
    assert ah.deep_calls == 0 and ah.units_calls == 0 and s.sig["deep_checked"] is False


def test_deep_error_leaves_unchecked():
    s = _strong(_state("a.com"))
    scoring._wave_deep([s], {"ahrefs": FakeAh(boom=True)}, _st(), None, None)
    assert s.alive and s.sig["deep_checked"] is False and "deep:RuntimeError" in s.sig["errors"]


def test_deep_empty_anchors_with_donors_is_not_checked():
    """1.3: доноры есть (RD 900), а анкоров нет — это не «чисто», а «не проверено». Нет доноров
    (RD 0) — проверять нечего, и это честное «проверено»."""
    s = _strong(_state("a.com"))
    scoring._wave_deep([s], {"ahrefs": FakeAh(anchors=[], history=HIST)}, _st(), None, None)
    assert s.alive and s.sig["deep_checked"] is False and "deep:empty" in s.sig["errors"]
    none = _strong(_state("nodonors.com"))
    none.sig["referring_domains"] = 0
    scoring._wave_deep([none], {"ahrefs": FakeAh(anchors=[], history=HIST)}, _st(), None, None)
    assert none.sig["deep_checked"] is True and none.sig["spam_anchor_ratio"] is None


def test_deep_history_failure_keeps_paid_anchor_verdict():
    """3.7: анкоры и история трафика — отдельные вызовы: сбой истории не выбрасывает оплаченный
    вердикт по анкорам и «вслепую» домен не делает."""
    s = _strong(_state("a.com"))
    scoring._wave_deep([s], {"ahrefs": FakeAh(anchors=CLEAN, history_boom=True)}, _st(), None, None)
    assert s.sig["deep_checked"] is True and s.sig["spam_anchor_ratio"] == 0.0
    assert "deep_history:RuntimeError" in s.sig["errors"] and "peak_traffic" not in s.sig


def test_deep_judges_anchor_script_by_past_site_language():
    """Р1: прошлый сайт японский — японские анкоры на .com не спам."""
    jp = [{"anchor": "東京のブログ", "refdomains": 50, "is_spam": False}]
    s = _strong(_state("tokyoblog.com"))
    s.sig["market_lang"] = "ja"
    scoring._wave_deep([s], {"ahrefs": FakeAh(anchors=jp)}, _st(), None, None)
    assert s.alive and s.sig["spam_anchor_ratio"] == 0.0


def test_deep_unknown_language_alone_is_not_spam_anchors():
    """R2-2: язык прошлого сайта неизвестен (LLM упал этим прогоном, в базе пусто) — отказ, который
    держится ТОЛЬКО на правиле скрипта, не выносится: `spam_anchors` — вечная грязь, кнопкой не
    вернуть, а сбой LLM не отклоняет. Домен — «анкоры не проверены» (вне пакета, но не грязь).
    Флаг Ahrefs `is_spam` и стоп-слова отклоняют и без языка."""
    jp = [{"anchor": "東京のブログ", "refdomains": 50, "is_spam": False}]
    s = _strong(_state("tokyoblog.com"))
    scoring._wave_deep([s], {"ahrefs": FakeAh(anchors=jp, history=HIST)}, _st(), None, None)
    assert s.alive and s.reject_reason is None and s.sig["deep_checked"] is False
    assert "deep:lang_unknown" in s.sig["errors"] and "spam_anchors" not in s.sig
    for extra in ({"anchor": "best casino bonus", "refdomains": 50, "is_spam": False},
                  {"anchor": "東京", "refdomains": 50, "is_spam": True}):
        bad = _strong(_state("tokyospam.com"))
        scoring._wave_deep([bad], {"ahrefs": FakeAh(anchors=jp + [extra], history=HIST)}, _st(), None, None)
        assert bad.reject_reason == "spam_anchors", extra


def test_llm_down_japanese_anchors_are_not_dirt():
    """R2-2 сквозной: LLM лежит, язык прошлого сайта неизвестен — японские анкоры не делают домен
    грязным (раньше: `spam_anchors` навсегда, а на перескоре — пятно на ранее чистом домене)."""
    from app.services import transitions
    did = _mk("tokyo-down.com", deadline=NOW + timedelta(days=2))
    jp = [{"anchor": "東京のブログ", "refdomains": 50, "is_spam": False}]
    out = scoring.score_domain(did, clients=_full_clients(
        FakeRdap(exists=True, registered=NOW - timedelta(days=4000)),
        FakeAh({"tokyo-down.com": STRONG}, anchors=jp, history=HIST), FakeWB(), llm=FakeLLM(boom=True)))
    assert out["status"] == "scored" and out["reject_reason"] is None
    assert "deep:lang_unknown" in out["errors"]
    with db.SessionLocal() as s:
        d = s.get(Domain, did)
        assert transitions.dirty_reason(d) is None and d.score_breakdown["deep_checked"] is False
        assert scoring.bulk_ok(d) is False                  # «анкоры НЕ проверены» — только руками


def test_llm_down_uses_market_lang_from_previous_run(monkeypatch):
    """R2-2: LLM этого прогона лежит, но язык прошлого сайта известен с прошлого прогона (в БД) —
    японские анкоры японского прошлого сайта не спам, анкоры проверены. Оба входа воронки:
    перепроверка одного домена (score_domain) и пакетный прогон (score_pending)."""
    jp = [{"anchor": "東京のブログ", "refdomains": 50, "is_spam": False}]
    names = ("tokyo-one.com", "tokyo-batch.com")
    ids = [_mk(n, deadline=NOW + timedelta(days=2), market_lang="ja") for n in names]
    clients = _full_clients(FakeRdap(exists=True, registered=NOW - timedelta(days=4000)),
                            FakeAh({n: STRONG for n in names}, anchors=jp, history=HIST), FakeWB(),
                            llm=FakeLLM(boom=True))
    scoring.score_domain(ids[0], clients=clients)
    monkeypatch.setattr(scoring, "_make_clients", lambda: clients)
    scoring.score_pending(limit=10)
    with db.SessionLocal() as s:
        for did in ids:
            d = s.get(Domain, did)
            assert d.status == "scored" and d.score_breakdown["deep_checked"] is True, d.domain
            assert float(d.spam_anchor_ratio) == 0.0 and d.market_lang == "ja", d.domain


def test_units_floor_skips_deep_and_says_why():
    """Р3: остаток units ниже пола перед W6 — анкоры не проверяются (домен вне пакета), причина —
    в пояснениях водопада (а через них — в сообщении задачи, см. тест W4)."""
    s, ah, notes = _strong(_state("a.com")), FakeAh(anchors=CLEAN, units=100_000), []
    scoring._wave_deep([s], {"ahrefs": ah}, _st(), None, None, notes)
    assert ah.deep_calls == 0 and s.alive and s.sig["deep_checked"] is False
    assert notes == ["Ahrefs: остаток 100 000 < пола 300 000 — платные волны пропущены"]


def test_score_pending_takes_deep_cap_from_settings(monkeypatch):
    """2.9: кап W6 — из /settings. 0 = W6 выключен: ни одного платного вызова анкоров, домен —
    «анкоры не проверены»."""
    from app.services.settings import update_settings
    update_settings(max_deep_per_run=0)
    did = _mk("nodeep.com", deadline=NOW + timedelta(days=2))
    ah = FakeAh({"nodeep.com": STRONG}, anchors=CLEAN, history=HIST)
    clients = _full_clients(FakeRdap(exists=True, registered=NOW - timedelta(days=4000)), ah, FakeWB(),
                            llm=FakeLLM())
    monkeypatch.setattr(scoring, "_make_clients", lambda: clients)
    scoring.score_pending(limit=10)
    assert ah.deep_calls == 0
    with db.SessionLocal() as s:
        d = s.get(Domain, did)
        assert d.status == "scored" and d.score_breakdown["deep_checked"] is False


def test_blind_reason_names_unchecked_anchors():
    """Р2: гард «без проверенных анкоров не одобрять» переехал из _decide в пакет. Домен,
    оценённый до W6 (ключа нет), — тоже «не проверены». У EMD ссылок нет — проверять нечего."""
    base = dict(wayback_checked=True, prior_flags={}, age_years=9.0)
    d = Domain(domain="anchors.com", score=0.8, score_breakdown={"errors": [], "deep_checked": False}, **base)
    assert "анкоры" in scoring.blind_reason(d) and scoring.bulk_ok(d) is False
    legacy = Domain(domain="v1.com", score=0.8, score_breakdown={"errors": []}, **base)
    assert "анкоры" in scoring.blind_reason(legacy)
    emd = Domain(domain="emd.com", score_breakdown={"errors": [], "emd": True}, **base)
    assert scoring.blind_reason(emd) is None


def test_far_past_topic_and_emd_stay_out_of_bulk():
    """Р2 + инвариант 4: прошлая тема далека от VPN (< 0.3) — такой домен человек одобряет только
    руками, глядя на тему; «тема не определена» (None) пакет не закрывает. EMD (балла нет) — тоже
    только руками."""
    base = dict(wayback_checked=True, prior_flags={}, age_years=9.0, score=0.8,
                score_breakdown={"errors": [], "deep_checked": True})
    assert scoring.bulk_ok(Domain(domain="near.com", topical_relevance=0.6, **base)) is True
    assert scoring.bulk_ok(Domain(domain="unknown.com", topical_relevance=None, **base)) is True
    far = Domain(domain="far.com", topical_relevance=0.1, **base)
    assert scoring.topic_far(far) is True and scoring.bulk_ok(far) is False
    emd = Domain(domain="emd.com", **{**base, "score": None, "score_breakdown": {"errors": [], "emd": True}})
    assert scoring.bulk_ok(emd) is False


def test_e2e_live_spam_drop_is_rejected_for_spam_anchors():
    did = _mk("pharmaindustrie.com", deadline=NOW + timedelta(days=2))
    out = scoring.score_domain(did, clients=_full_clients(
        FakeRdap(exists=True, registered=datetime(1998, 7, 29, tzinfo=timezone.utc)),
        FakeAh({"pharmaindustrie.com": ROW}, anchors=SPAM, history=HIST), FakeWB(), llm=FakeLLM()))
    assert out["status"] == "rejected" and out["reject_reason"] == "spam_anchors"


def test_e2e_clean_strong_domain_is_scored_and_lands_in_bulk():
    """Р2: машина ставит максимум `scored`; чистый сильный, полностью проверенный домен попадает в
    пакетное одобрение — одобряет его человек."""
    from app.api import panel
    did = _mk("goodvpnblog.com", source="list", lane=None)
    out = scoring.score_domain(did, clients=_full_clients(
        FakeRdap(exists=False),
        FakeAh({"goodvpnblog.com": STRONG}, anchors=CLEAN, history=[{"date": "2023-01-01", "org_traffic": 4000}]),
        FakeWB(age=10.0), llm=FakeLLM(answer='{"snapshots":[],"topic_summary":"vpn","vpn_adjacent":0.9}')))
    assert out["status"] == "scored" and out["reject_reason"] is None
    with db.SessionLocal() as s:
        ok, skipped = panel._bulk_candidates(s, 0.0)
        assert [d.domain for d in ok] == ["goodvpnblog.com"] and skipped == 0


def test_e2e_emd_is_scored_without_score_and_never_in_bulk():
    """EMD — `scored` без балла («решение за тобой»): ни W4, ни W6 не тратятся, пакет его не берёт
    даже при чистой истории (1.13)."""
    from app.api import panel
    did = _mk("mejorvpn.com", source="emd", lane="free", market_lang="es")
    ah = FakeAh({})
    out = scoring.score_domain(did, clients=_full_clients(FakeRdap(exists=False), ah, FakeWB(), llm=FakeLLM()))
    assert out["status"] == "scored" and out["score"] is None
    assert ah.batches == [] and ah.deep_calls == 0
    with db.SessionLocal() as s:
        d = s.get(Domain, did)
        assert d.score is None and d.score_breakdown.get("emd") is True and d.market_lang == "es"
        assert scoring.bulk_ok(d) is False and panel._bulk_candidates(s, 0.0) == ([], 0)


def test_rescore_with_ahrefs_down_does_not_launder_spam_anchors():
    """1.4: отказ `spam_anchors` -> перескор, на котором Ahrefs упал (W6 не дошла) -> улика в
    score_breakdown цела, домен по-прежнему грязный и в оборот кнопкой не возвращается."""
    import pytest
    from app.services import transitions
    did = _mk("spammy.com", deadline=NOW + timedelta(days=2))
    clients = _full_clients(FakeRdap(exists=True, registered=NOW - timedelta(days=4000)),
                            FakeAh({"spammy.com": STRONG}, anchors=SPAM, history=HIST), FakeWB(), llm=FakeLLM())
    assert scoring.score_domain(did, clients=clients)["reject_reason"] == "spam_anchors"
    class _AnchorsDown(FakeAh):
        def anchors(self, d, limit=50):
            raise RuntimeError("ahrefs down")
    out = scoring.score_domain(did, clients={**clients, "ahrefs": _AnchorsDown({"spammy.com": STRONG})})
    assert out["reject_reason"] is None and "deep:RuntimeError" in out["errors"]
    with db.SessionLocal() as s:
        d = s.get(Domain, did)
        assert d.score_breakdown["spam_anchors"] is True               # улику не стёрли
        assert transitions.dirty_reason(d) == "spam_anchors"
        with pytest.raises(transitions.TransitionDenied):
            transitions.check(d, "approved")


def test_rescore_with_llm_down_keeps_far_topic_out_of_bulk():
    """Сбой LLM на перескоре не снимает исключение «прошлая тема далека от VPN»: близость 0.1 из
    прошлого прогона сохранена, и полностью проверенный в остальном домен в пакет не попадает."""
    from app.api import panel
    did = _mk("far-topic.com", deadline=NOW + timedelta(days=2), topic="casino reviews",
              topical_relevance=0.1)
    out = scoring.score_domain(did, clients=_full_clients(
        FakeRdap(exists=True, registered=NOW - timedelta(days=4000)),
        FakeAh({"far-topic.com": STRONG}, anchors=CLEAN, history=HIST), FakeWB(),
        llm=FakeLLM(boom=True)))
    assert out["status"] == "scored"
    with db.SessionLocal() as s:
        d = s.get(Domain, did)
        assert float(d.topical_relevance) == 0.1 and d.topic is None
        assert scoring.blind_reason(d) is None and scoring.topic_far(d) is True
        assert panel._bulk_candidates(s, 0.0) == ([], 1)


class CountingWB(FakeWB):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.calls = 0

    def classify_history(self, d):
        self.calls += 1
        return super().classify_history(d)


def test_e2e_early_exit_spends_nothing_expensive():
    """Спека §8: домен, отсеянный на W0–W3, не тратит ни units Ahrefs, ни запросов к archive.org.
    Бесплатный запрос остатка units — один раз в начале прогона, у переживших W0 (R2-10)."""
    for name, rdap, wr in (("nordvpn-deals.com", FakeRdap(exists=False), FakeWR()),            # W0 бренд
                           ("taken-free.com", FakeRdap(exists=True, registered=NOW), FakeWR()),  # W2 занят
                           ("malware.com", FakeRdap(exists=False), FakeWR(threats=["MALWARE"]))):  # W3 риск
        did = _mk(name, "mx" if name == "taken-free.com" else "list", "free" if name == "taken-free.com" else None)
        ah, wb = FakeAh({name: ROW}), CountingWB()
        out = scoring.score_domain(did, clients={**_full_clients(rdap, ah, wb), "webrisk": wr})
        assert out["status"] == "rejected", name
        assert ah.batches == [] and ah.deep_calls == 0 and wb.calls == 0, name
        assert ah.units_calls == (0 if name == "nordvpn-deals.com" else 1), name


class _UnitsDrain(FakeAh):
    """Остаток units падает ниже пола между началом прогона и W6 (W4 потратила): первый ответ — выше пола."""
    def units_left(self):
        super().units_left()
        return 2_000_000 if self.units_calls == 1 else 100_000


def test_deep_floor_is_a_fresh_units_query_not_the_cached_gate(monkeypatch):
    """Контроль 1 (Задача 11 + 13): `_paid_gate` кэширует решение начала прогона, но перед W6 остаток
    спрашивается ЗАНОВО — W4 уже потратила units. Кэш гейта (открыт) не должен пропустить W6 при
    остатке ниже пола: ровно два запроса units_left, анкоры не куплены, причина — в сообщении."""
    from app.services import jobs
    did = _mk("drain.com", deadline=NOW + timedelta(days=2))
    ah = _UnitsDrain({"drain.com": STRONG}, anchors=CLEAN, history=HIST)
    clients = _full_clients(FakeRdap(exists=True, registered=NOW - timedelta(days=4000)), ah, FakeWB(),
                            llm=FakeLLM())
    monkeypatch.setattr(scoring, "_make_clients", lambda: clients)
    scoring.score_pending(limit=10)
    assert ah.units_calls == 2 and ah.deep_calls == 0
    with db.SessionLocal() as s:
        d = s.get(Domain, did)
        assert d.status == "scored" and d.score_breakdown["deep_checked"] is False
    assert "остаток 100 000 < пола 300 000" in jobs.last("score")["message"]


def test_trademark_domain_is_rehabilitated_after_brand_token_removed():
    """Контроль 2: `trademark` теперь в DIRTY_REASONS (кнопкой не вернуть), единственный путь назад —
    перескор. Оператор убрал бренд-токен из /settings -> W0 бренд-проверку ПРОШЛА и пишет
    trademark_risk=False (раньше колонка оставалась True навсегда: пишется только не-None). Причина
    `trademark` снимается, домен не залипает грязным."""
    from app.services import transitions
    from app.services.settings import get_settings, update_settings
    did = _mk("nordvpn-deals.com", source="list", lane=None, deadline=NOW + timedelta(days=2))
    clients = _full_clients(FakeRdap(exists=True, registered=NOW - timedelta(days=4000)),
                            FakeAh({"nordvpn-deals.com": STRONG}, anchors=CLEAN, history=HIST), FakeWB(),
                            llm=FakeLLM())
    assert scoring.score_domain(did, clients=clients)["reject_reason"] == "trademark"
    with db.SessionLocal() as s:
        d = s.get(Domain, did)
        assert d.trademark_risk is True and transitions.dirty_reason(d) == "trademark"
    update_settings(brand_tokens=[t for t in get_settings()["brand_tokens"] if t != "nordvpn"])
    out = scoring.score_domain(did, clients=clients)
    assert out["reject_reason"] != "trademark"
    with db.SessionLocal() as s:
        d = s.get(Domain, did)
        assert d.trademark_risk is False and d.reject_reason != "trademark"
        assert transitions.dirty_reason(d) is None


def test_trademark_flag_is_not_cleared_when_the_brand_check_did_not_run():
    """Грязь не отмывается перескором, где бренд-проверка НЕ выполнялась: отказ W0 раньше бренда
    (зона вне белого списка) оставляет trademark_risk как был."""
    did = _mk("nordvpn-deals.com", source="list", lane=None, trademark_risk=True)
    out = scoring.score_domain(did, clients=_full_clients(FakeRdap(), FakeAh(), FakeWB()),
                               whois_budget=None)
    assert out["status"] == "rejected"
    from app.services.settings import update_settings
    update_settings(tld_allowlist=["net"])           # .com закрыта: W0 режет по зоне ДО бренда
    out = scoring.score_domain(did, clients=_full_clients(FakeRdap(), FakeAh(), FakeWB()))
    assert out["reject_reason"] == "tld_closed"
    with db.SessionLocal() as s:
        assert s.get(Domain, did).trademark_risk is True
