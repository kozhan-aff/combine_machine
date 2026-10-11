"""G4: воронка M1 — отмена и сбой потока (S2-10), проба архива до платной W4 (S2-06/S2-14), тема вне
слота Wayback (S2-09), оплаченные результаты сохраняются и не покупаются дважды (S2-06/S2-07),
«слепые» возвращаются в очередь (S2-07), без ключа Ahrefs волны W4/W6 пропускаются (S1-01)."""
import threading
import time
from datetime import timedelta

import pytest

import app.db as db
from app.models.domain import Domain
from app.services import jobs, scoring
from tests.test_waves_v2 import (NOW, ROW, STRONG, AgedWB, FakeAh, FakeLLM, FakeRdap, FakeWB, _full_clients,
                                 _mk, _st, _state)


# ---- S2-10: поток волны ----

def test_unhandled_exception_in_thread_marks_domain_not_alive():
    s = _state("boom.com")

    def fn(st):
        raise KeyError("x")
    scoring._run_concurrent([s], 2, None, "avail", fn)
    assert not s.alive and s.unresolved_why == "avail_failed"
    assert s.sig["errors"] == ["avail:KeyError"]


def test_cancel_does_not_wait_for_running_threads(monkeypatch):
    release = threading.Event()
    states = [_state(f"c{i}.com") for i in range(3)]
    flag = {"n": 0}

    def cancelled(run, **kw):
        flag["n"] += 1
        return flag["n"] > 1          # первая проверка — «нет», дальше — отмена

    monkeypatch.setattr(jobs, "cancelled", cancelled)
    t0 = time.monotonic()
    try:
        with pytest.raises(jobs.Cancelled):
            scoring._run_concurrent(states, 3, 1, "history", lambda s: release.wait(30))
        assert time.monotonic() - t0 < 5          # раньше ждал бы все потоки (здесь — 30 с)
    finally:
        release.set()


# ---- S2-06 / S2-14: проба архива до платной W4 ----

class ProbeWB(AgedWB):
    def __init__(self, probe, **kw):
        super().__init__(**kw)
        self._probe, self.probe_calls = probe, 0

    def probe(self, d):
        self.probe_calls += 1
        if isinstance(self._probe, Exception):
            raise self._probe
        return self._probe


def _young(domain="young.com", years=1.0):
    s = _state(domain)
    s.sig.update({"age_years": years, "age_source": "whois"})
    return s


def test_probe_rejects_young_before_paid_wave():
    s = _young()
    wb = ProbeWB({"first_seen": NOW, "age_years": 1.0, "archive_empty": False, "snapshots": 3})
    scoring._wave_probe([s], {"wayback": wb}, _st(min_age_years=3.0), None)
    assert s.reject_reason == "too_young" and not s.alive


def test_probe_older_snapshot_overrides_young_rdap():
    s = _young()
    wb = ProbeWB({"first_seen": NOW, "age_years": 12.0, "archive_empty": False, "snapshots": 100})
    scoring._wave_probe([s], {"wayback": wb}, _st(min_age_years=3.0), None)
    assert s.alive and s.sig["age_years"] == 12.0 and s.sig["age_source"] == "wayback"


def test_probe_ambiguous_empty_does_not_reject():
    s = _young()
    wb = ProbeWB({"first_seen": None, "age_years": None, "archive_empty": False, "snapshots": 0})
    scoring._wave_probe([s], {"wayback": wb}, _st(min_age_years=3.0), None)
    assert s.alive and s.reject_reason is None


def test_probe_confirmed_empty_archive_with_young_rdap_rejects():
    s = _young()
    wb = ProbeWB({"first_seen": None, "age_years": None, "archive_empty": True, "snapshots": 0})
    scoring._wave_probe([s], {"wayback": wb}, _st(min_age_years=3.0), None)
    assert s.reject_reason == "too_young"


def test_probe_failure_is_silent_and_emd_is_never_young():
    s, e = _young(), _young("emd.com")
    e.source = "emd"
    scoring._wave_probe([s], {"wayback": ProbeWB(RuntimeError("503"))}, _st(), None)
    assert s.alive and s.sig["errors"] == []
    wb = ProbeWB({"first_seen": NOW, "age_years": 0.5, "archive_empty": False, "snapshots": 1})
    scoring._wave_probe([e], {"wayback": wb}, _st(min_age_years=3.0), None)
    assert e.alive


def test_young_domain_costs_no_ahrefs_units():
    """Сквозь воронку: too_young по пробе отсекается ДО платной W4."""
    did = _mk("tooyoung.com", deadline=NOW + timedelta(days=2))
    ah = FakeAh({"tooyoung.com": ROW})
    wb = ProbeWB({"first_seen": NOW, "age_years": 0.5, "archive_empty": False, "snapshots": 1})
    out = scoring.score_domain(did, clients=_full_clients(
        FakeRdap(exists=True, registered=NOW - timedelta(days=100)), ah, wb))
    assert out["reject_reason"] == "too_young" and ah.batches == []


def test_history_empty_unconfirmed_archive_is_age_unverified_not_too_young():
    class EmptyWB:
        def __init__(self, confirmed):
            self.confirmed = confirmed

        def classify_history(self, d):
            return {"prior_flags": {}, "first_seen": None, "age_years": None, "wayback_checked": False,
                    "sampled": 0, "evidence": [], "texts": [], "archive_empty": self.confirmed}
    amb, conf = _young("amb.com", 1.0), _young("conf.com", 1.0)
    scoring._history_one(amb, {"wayback": EmptyWB(False)}, _st(min_age_years=3.0))
    scoring._history_one(conf, {"wayback": EmptyWB(True)}, _st(min_age_years=3.0))
    assert amb.alive and "age:unverified" in amb.sig["errors"] and amb.reject_reason is None
    assert conf.reject_reason == "too_young"


def test_wayback_unavailable_defers_domain_instead_of_blind_score():
    from app.integrations.wayback import WaybackUnavailable

    class DownWB:
        def classify_history(self, d):
            raise WaybackUnavailable("down")
    s = _state("later.com")
    scoring._history_one(s, {"wayback": DownWB()}, _st())
    assert not s.alive and s.unresolved_why == "wayback_down"


# ---- S2-09: тема вне слота Wayback ----

def test_topic_runs_after_all_wayback_calls():
    events = []

    class WB(FakeWB):
        def classify_history(self, d):
            events.append("wb")
            time.sleep(0.02)
            return super().classify_history(d)

    class LLM(FakeLLM):
        def complete(self, system, prompt, **kw):
            events.append("llm")
            return super().complete(system, prompt, **kw)
    states = [_state(f"t{i}.com") for i in range(6)]
    scoring._wave_history(states, {"wayback": WB(), "llm": LLM()}, _st(), None)
    assert events == ["wb"] * 6 + ["llm"] * 6
    assert all(s.sig.get("topic") == "vpn blog" for s in states) and all(not s.texts for s in states)


def test_llm_error_text_reaches_job_notes():
    class Forbidden(FakeLLM):
        def complete(self, system, prompt, **kw):
            raise RuntimeError("модель mistral: HTTP 403 — тариф")
    clients = {"wayback": FakeWB(), "llm": Forbidden()}
    notes = []
    scoring._wave_history([_state("a.com")], clients, _st(), None)
    scoring._llm_note(clients, notes)
    assert notes == ["LLM-тема: модель mistral: HTTP 403 — тариф"]


# ---- S1-01: без ключа Ahrefs ----

def test_no_key_waves_links_and_deep_do_nothing():
    from app.integrations.ahrefs import AhrefsClient
    s = _state("nokey.com")
    scoring._wave_links([s], {"ahrefs": AhrefsClient(api_key="")}, _st(), None, None)
    s.sig["wayback_checked"] = True
    scoring._wave_deep([s], {"ahrefs": AhrefsClient(api_key="")}, _st(), None, None)
    assert s.alive and s.unresolved_why is None and "deep_checked" not in s.sig


def test_no_key_domain_reaches_scored_without_rd_dr(monkeypatch):
    from app.integrations.ahrefs import AhrefsClient
    did = _mk("nokey2.com", deadline=NOW + timedelta(days=2))
    out = scoring.score_domain(did, clients=_full_clients(
        FakeRdap(exists=True, registered=NOW - timedelta(days=4000)), AhrefsClient(api_key=""), AgedWB()))
    assert out["status"] == "scored"
    with db.SessionLocal() as s:
        d = s.get(Domain, did)
        assert d.referring_domains is None and d.wayback_checked is True
        assert scoring.bulk_ok(d) is False and "спам-ссылки НЕ проверены" in scoring.blind_reason(d)


def test_web_risk_unconfigured_is_a_visible_job_note():
    class WR:
        configured = False
    notes = []
    scoring._wave_risk([_state("a.com")], {"webrisk": WR()}, None, notes)
    assert len(notes) == 1 and "Web Risk не настроен" in notes[0]


# ---- S2-06 / S2-07: оплаченное сохраняется и не покупается дважды ----

def test_paid_w4_is_persisted_and_not_bought_twice():
    did = _mk("paid.com", deadline=NOW + timedelta(days=2))
    ah = FakeAh({"paid.com": STRONG})
    clients = _full_clients(FakeRdap(exists=True, registered=NOW - timedelta(days=4000)), ah, AgedWB())
    scoring.score_domain(did, clients=clients)
    assert ah.batches == [["paid.com"]]
    with db.SessionLocal() as s:
        d = s.get(Domain, did)
        assert d.referring_domains == 900 and d.backlinks == 801
    scoring.score_domain(did, clients=clients)           # ручной перескор
    assert ah.batches == [["paid.com"]]                   # второй раз Ahrefs не зовём
    with db.SessionLocal() as s:
        assert s.get(Domain, did).referring_domains == 900


def test_persist_links_keeps_paid_result_when_later_wave_dies(monkeypatch):
    did = _mk("diesafter.com", deadline=NOW + timedelta(days=2))
    ah = FakeAh({"diesafter.com": STRONG})

    def boom(*a, **kw):
        raise RuntimeError("worker restart")
    monkeypatch.setattr(scoring, "_wave_history", boom)
    with pytest.raises(RuntimeError):
        scoring.score_domain(did, clients=_full_clients(
            FakeRdap(exists=True, registered=NOW - timedelta(days=4000)), ah, AgedWB()))
    with db.SessionLocal() as s:
        d = s.get(Domain, did)
        assert d.referring_domains == 900 and d.status == "discovered"      # оплаченное не пропало


def test_cached_low_rd_still_rejects_without_paying():
    did = _mk("zero.com", deadline=NOW + timedelta(days=2), referring_domains=0, backlinks=0)
    ah = FakeAh({})
    out = scoring.score_domain(did, clients=_full_clients(
        FakeRdap(exists=True, registered=NOW - timedelta(days=4000)), ah, AgedWB()))
    assert out["reject_reason"] == "low_rd" and ah.batches == []


# ---- S2-07: слепые возвращаются в очередь ----

def _blind(name, errors=("wayback:RuntimeError",), scored_ago=timedelta(days=2), **kw):
    with db.SessionLocal() as s:
        d = Domain(domain=name, source="nominet", lane="bid", status="scored", score=0.55,
                   scored_at=NOW - scored_ago, age_years=9, referring_domains=300, dr=12,
                   acquire_deadline=NOW + timedelta(days=2),
                   score_breakdown={"errors": list(errors), "deep_checked": False}, **kw)
        s.add(d)
        s.commit()
        return d.id


def test_blind_domain_is_requeued_for_history_only(monkeypatch):
    did = _blind("blind.com")
    ah, wb = FakeAh({}), FakeWB()
    monkeypatch.setattr(scoring, "_make_clients", lambda: _full_clients(FakeRdap(), ah, wb, llm=FakeLLM()))
    scoring.score_pending(limit=10)
    assert wb.__class__ is FakeWB and ah.batches == [] and ah.deep_calls == 0
    with db.SessionLocal() as s:
        d = s.get(Domain, did)
        assert d.wayback_checked is True and d.status == "scored"
        assert "wayback:RuntimeError" not in d.score_breakdown["errors"]
        assert scoring.history_verdict(d) == "clean"


def test_blind_requeue_finds_dirt_and_rejects(monkeypatch):
    did = _blind("blind-dirty.com")
    monkeypatch.setattr(scoring, "_make_clients",
                        lambda: _full_clients(FakeRdap(), FakeAh({}), FakeWB(dirty=True), llm=FakeLLM()))
    scoring.score_pending(limit=10)
    with db.SessionLocal() as s:
        d = s.get(Domain, did)
        assert d.status == "rejected" and d.reject_reason == "history_dirty"


def test_blind_requeue_respects_cooldown_and_skips_clean(monkeypatch):
    fresh = _blind("fresh-blind.com", scored_ago=timedelta(hours=1))
    with db.SessionLocal() as s:
        s.add(Domain(domain="clean.com", source="nominet", status="scored", wayback_checked=True,
                     prior_flags={}, scored_at=NOW - timedelta(days=3), score_breakdown={"errors": []}))
        s.commit()
    calls = []

    class WB(FakeWB):
        def classify_history(self, d):
            calls.append(d)
            return super().classify_history(d)
    monkeypatch.setattr(scoring, "_make_clients", lambda: _full_clients(FakeRdap(), FakeAh({}), WB(), llm=FakeLLM()))
    scoring.score_pending(limit=10)
    assert calls == []
    with db.SessionLocal() as s:
        assert s.get(Domain, fresh).wayback_checked in (None, False)


def test_blind_requeue_still_blind_stays_scored(monkeypatch):
    did = _blind("still-blind.com")
    monkeypatch.setattr(scoring, "_make_clients",
                        lambda: _full_clients(FakeRdap(), FakeAh({}), FakeWB(checked=False), llm=FakeLLM()))
    scoring.score_pending(limit=10)
    with db.SessionLocal() as s:
        d = s.get(Domain, did)
        assert d.status == "scored" and not d.wayback_checked


def test_no_key_marks_paid_chips_skipped_and_waterfall(monkeypatch):
    from app.integrations.ahrefs import AhrefsClient
    _mk("chips.com", deadline=NOW + timedelta(days=2))
    monkeypatch.setattr(scoring, "_make_clients", lambda: _full_clients(
        FakeRdap(exists=True, registered=NOW - timedelta(days=4000)), AhrefsClient(api_key=""), AgedWB()))
    real, msgs = jobs.report, []

    def spy(run_id, **kw):
        if kw.get("message"):
            msgs.append(kw["message"])
        return real(run_id, **kw)
    monkeypatch.setattr(jobs, "report", spy)
    scoring.score_pending(limit=10)
    last = jobs.last("score")
    states = {s["key"]: s.get("state") for s in last["stages"]}
    assert states["links"] == "skip" and states["deep"] == "skip" and states["history"] != "skip"
    assert any("ссылки: пропущено (нет ключа Ahrefs)" in m for m in msgs)       # водопад живой задачи
