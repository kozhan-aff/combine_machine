"""Волновой оркестратор скоринга: FunnelState, Budget, конкурентный харнесс, волны."""
import threading
import time
from datetime import datetime, timedelta, timezone

from app.services import scoring


def test_budget_take_is_thread_safe_under_contention():
    """20 потоков разбирают бюджет в 10 — ровно 10 успешных take(), не больше и не меньше
    (гонка на невзвешенном инкременте дала бы >10 при обычном [int])."""
    budget = scoring.Budget(10)
    taken = []
    lock = threading.Lock()

    def worker():
        ok = budget.take()
        with lock:
            taken.append(ok)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert taken.count(True) == 10
    assert taken.count(False) == 10


def test_list_budget_adapts_legacy_list_in_place():
    """score_domain() принимает [int] снаружи (легаси-контракт) — адаптер обязан мутировать
    ТОТ ЖЕ список, не свою копию, иначе внешний вызывающий не увидит расход."""
    box = [2]
    b = scoring._ListBudget(box)
    assert b.take() is True and box == [1]
    assert b.take() is True and box == [0]
    assert b.take() is False and box == [0]


def test_wave_t0_rejects_feed_flag_and_leaves_rd_to_links_wave():
    """W0 не судит RD: в v2 его даёт Ahrefs в W4 «ссылки» (RD из строки домена — не наблюдение
    этого прогона). Домен с низким RD из строки проходит W0 живым."""
    st = {"min_referring_domains": 5, "tld_allowlist": ["com"], "brand_tokens": []}
    flagged = scoring.FunnelState(domain_id=1, domain="a.com", lane=None,
                                  referring_domains=10, acquire_deadline=None,
                                  feed_flags={"rkn": True})
    low_rd = scoring.FunnelState(domain_id=2, domain="b.com", lane=None,
                                 referring_domains=1, acquire_deadline=None,
                                 feed_flags=None)
    ok = scoring.FunnelState(domain_id=3, domain="c.com", lane=None,
                             referring_domains=50, acquire_deadline=None,
                             feed_flags=None)
    states = [flagged, low_rd, ok]
    scoring._wave_t0(states, st)
    assert flagged.alive is False and flagged.reject_reason == "feed_flag"
    assert low_rd.alive is True and low_rd.reject_reason is None
    assert ok.alive is True and ok.reject_reason is None


def test_run_concurrent_calls_fn_only_on_alive_and_survives_one_failure():
    """Находка ревью Task 1 (2026-07-21): _run_concurrent — общий харнесс, на котором
    поедут ВСЕ следующие волны (whois/risk/history/ahrefs), отгружался без прямого
    теста — только косвенно через _wave_t0, который его даже не вызывает (T0 без сети,
    без пула). Проверяем сам харнесс: мёртвые не трогаются, сбой одного домена не топит
    остальных."""
    calls = []
    lock = threading.Lock()

    def fn(s):
        if s.domain == "boom.ru":
            raise RuntimeError("boom")
        with lock:
            calls.append(s.domain)

    dead = scoring.FunnelState(domain_id=1, domain="dead.ru", lane=None,
                               referring_domains=None, acquire_deadline=None,
                               feed_flags=None, alive=False)
    boom = scoring.FunnelState(domain_id=2, domain="boom.ru", lane=None,
                               referring_domains=None, acquire_deadline=None,
                               feed_flags=None)
    ok = scoring.FunnelState(domain_id=3, domain="ok.ru", lane=None,
                             referring_domains=None, acquire_deadline=None,
                             feed_flags=None)
    scoring._run_concurrent([dead, boom, ok], workers=4, run=None, stage="whois", fn=fn)
    assert calls == ["ok.ru"]        # dead пропущен (fn не вызван), boom упал и не помешал ok


def test_run_concurrent_raises_cancelled_when_stop_requested():
    """Отмена (кнопка «✕ Отменить») проверяется после каждого завершения внутри волны,
    не только между волнами — request_cancel ДО старта харнесса должен оборвать волну
    ещё на первом завершившемся домене, а не тихо доработать весь пакет.

    НЕ ловим jobs.Cancelled сами: jobs.track() ловит его ВНУТРИ своего generator'а
    (except Cancelled -> _close(..., "cancelled"), БЕЗ re-raise) — если поймать
    исключение раньше, до границы `with`, track() увидит нормальный выход и закроет
    прогон как "done", а не "cancelled" (поймано на этом самом тесте при первом
    написании)."""
    from app.services import jobs

    states = [scoring.FunnelState(domain_id=i, domain=f"c{i}.ru", lane=None,
                                  referring_domains=None, acquire_deadline=None,
                                  feed_flags=None) for i in range(5)]
    with jobs.track("score") as run:
        jobs.request_cancel("score")
        scoring._run_concurrent(states, workers=2, run=run, stage="whois",
                                fn=lambda s: None)
    assert jobs.last("score")["status"] == "cancelled"


class _FakeAparserWhois:
    def __init__(self, available=False, created=None, fail_times=0):
        self.available = available
        self.created = created
        self.fail_times = fail_times
        self.calls = 0
        self.whois_failures = 0

    def whois_probe(self, domain):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError("timeout")
        return {"available": self.available, "created": self.created}

    def safebrowsing_check(self, domain):
        return False


def _clients_aparser_only(**kw):
    return {"aparser": _FakeAparserWhois(**kw), "_whois_lock": threading.Lock()}


def test_wave_avail_records_young_age_but_does_not_reject_bid_domain():
    """Р5: W2 возраст только записывает. Молодая дата регистрации у bid-домена — не отказ: у
    перехваченного домена это дата ПОСЛЕДНЕЙ регистрации; судит волна истории по старшей дате."""
    st = {"min_age_years": 3.0}
    young = datetime.now(timezone.utc) - timedelta(days=200)
    s = scoring.FunnelState(domain_id=1, domain="young.com", lane="bid",
                            referring_domains=5, acquire_deadline=None, feed_flags=None)
    clients = _clients_aparser_only(available=False, created=young)
    scoring._wave_avail([s], clients, budget=None, st=st, run=None)
    assert s.alive is True and s.reject_reason is None
    assert s.sig["whois_created"] == young and s.sig["age_years"] < 1


def test_wave_avail_marks_free_lane_and_survives():
    st = {"min_age_years": 3.0}
    old = datetime.now(timezone.utc) - timedelta(days=365 * 10)
    s = scoring.FunnelState(domain_id=2, domain="free.com", lane=None,
                            referring_domains=5, acquire_deadline=None, feed_flags=None)
    clients = _clients_aparser_only(available=True, created=old)
    scoring._wave_avail([s], clients, budget=None, st=st, run=None)
    assert s.alive is True and s.sig["lane"] == "free"


def test_wave_avail_budget_exhausted_marks_unresolved_without_network_call():
    st = {"min_age_years": 3.0}
    s = scoring.FunnelState(domain_id=3, domain="over.com", lane=None,
                            referring_domains=5, acquire_deadline=None, feed_flags=None)
    aparser = _FakeAparserWhois(available=True)
    clients = {"aparser": aparser, "_whois_lock": threading.Lock()}
    budget = scoring.Budget(0)
    scoring._wave_avail([s], clients, budget=budget, st=st, run=None)
    assert s.alive is False and s.unresolved_why == "budget"
    assert aparser.calls == 0          # бюджет исчерпан ДО сети — вызова не было


class _SlowAlwaysFailAparser:
    """Всегда падает, с искусственной задержкой в whois_probe — форсирует РЕАЛЬНОЕ
    перекрытие потоков. Без задержки первый воркер успевал бы отработать (гейт-чек +
    инкремент) до того, как остальные 11 вообще стартовали бы — счётчик тогда растёт
    строго последовательно, и тест "whois_failures <= LIMIT" проходит ОДИНАКОВО что с
    замком, что без него (находка ревью Task 2, 2026-07-21: сломанный замок ТЕРЯЕТ
    инкременты -> счётчик становится МЕНЬШЕ -> тоже <= LIMIT -> ложный зелёный)."""
    def __init__(self):
        self._calls_lock = threading.Lock()   # bookkeeping самой фикстуры, не код под тестом
        self.calls = 0
        self.whois_failures = 0

    def whois_probe(self, domain):
        with self._calls_lock:
            self.calls += 1
        time.sleep(0.02)
        raise RuntimeError("timeout")


def test_wave_avail_breaker_lock_has_no_lost_increments_under_real_overlap():
    """20 доменов, конкурентность 12, whois всегда падает С ЗАДЕРЖКОЙ (форсирует настоящее
    перекрытие потоков, не последовательный проход) — интеграционный смоук-тест волны под
    реальной нагрузкой, а НЕ ловец гонки на счётчике (честно, по итогам повторной проверки
    ревью, 2026-07-21): read-modify-write внутри `with cm:` — пара строк без I/O между ними,
    и вручную повторено 30/30 и 60/60 прогонов БЕЗ лока (`nullcontext`) с тем же результатом
    "не потеряно ни одного инкремента" — таймингом эту гонку не форсировать за разумное
    число прогонов. Настоящую гарантию, что _aparser_whois берёт лок вокруг ОБЕИХ операций,
    даёт детерминированный `test_aparser_whois_breaker_locks_both_the_gate_check_and_the_increment`
    ниже (спай-лок, считает реальные входы, не зависит от таймингов ОС). Этот тест остаётся
    полезным как регрессия на бухгалтерию волны (breaker реально трипает и реально
    останавливает часть вызовов под конкурентной нагрузкой, домены помечаются корректно)."""
    st = {"min_age_years": 3.0}
    states = [scoring.FunnelState(domain_id=i, domain=f"slow{i}.com", lane=None,
                                  referring_domains=5, acquire_deadline=None,
                                  feed_flags=None) for i in range(20)]
    aparser = _SlowAlwaysFailAparser()
    clients = {"aparser": aparser,
              "_whois_lock": threading.Lock()}
    scoring._wave_avail(states, clients, budget=None, st=st, run=None)
    assert all(not s.alive and s.unresolved_why == "whois_failed" for s in states)
    assert aparser.whois_failures == aparser.calls    # держится и без гонки — см. докстринг
    assert 3 <= aparser.whois_failures < 20           # предохранитель реально сработал и что-то остановил


class _SpyLock:
    """Обёртка над настоящим Lock, которая ЗАПИСЫВАЕТ каждый вход — доказывает, что
    _aparser_whois реально берёт лок вокруг ОБЕИХ операций (гейт-чек чтения И запись
    счётчика), а не только вокруг одной из них. Гонка на голом += 1 под GIL слишком
    редкая, чтобы ловить её таймингом надёжно за разумное число прогонов (перепроверено
    ревью Task 3 и независимо здесь: 30-60 прогонов соседнего теста БЕЗ лока — 0 потерянных
    инкрементов что с локом, что без него) — этот тест проверяет структуру блокировки
    напрямую, детерминированно, а не полагается на то, чтобы гонку "поймать" вовремя."""
    def __init__(self):
        self._real = threading.Lock()
        self.enters = 0

    def __enter__(self):
        self._real.acquire()
        self.enters += 1

    def __exit__(self, *a):
        self._real.release()


def test_aparser_whois_breaker_locks_both_the_gate_check_and_the_increment():
    from app.services import whois as whois_router

    class _AlwaysFails:
        def whois_probe(self, d):
            raise RuntimeError("timeout")

    lock = _SpyLock()
    ap = _AlwaysFails()
    for _ in range(3):          # ровно до порога (_APARSER_WHOIS_FAILURE_LIMIT=3)
        try:
            whois_router._aparser_whois(ap, "x.ru", lock)
        except RuntimeError:
            pass
    # каждая из 3 попыток (ДО срабатывания) берёт лок дважды: гейт-чек чтения + запись
    # инкремента. Пропуск любого из двух входов означает непокрытую гонку.
    assert lock.enters == 6
    assert ap.whois_failures == 3


def test_wave_avail_actually_runs_concurrently_not_serially():
    """Конкурентность волны — подсчётом одновременных входов, а не таймингом (находка 5.17:
    прежний порог 0.5 с флапал под нагрузкой). Барьер на _CONCURRENCY["avail"] участников
    пропускает, только если столько вызовов РЕАЛЬНО стоят в whois одновременно; последовательный
    обход упёрся бы в таймаут барьера, и домены упали бы. Пик выше пула — тоже провал."""
    n = scoring._CONCURRENCY["avail"]
    barrier = threading.Barrier(n, timeout=10)
    lock, active, peak = threading.Lock(), [0], [0]

    class _MeetingAparser:
        def whois_probe(self, d):
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            try:
                barrier.wait()
            finally:
                with lock:
                    active[0] -= 1
            return {"available": True, "created": datetime.now(timezone.utc) - timedelta(days=3650)}

    states = [scoring.FunnelState(domain_id=i, domain=f"slow{i}.com", lane=None,
                                  referring_domains=5, acquire_deadline=None,
                                  feed_flags=None) for i in range(2 * n)]
    clients = {"aparser": _MeetingAparser(), "_whois_lock": threading.Lock()}
    scoring._wave_avail(states, clients, budget=None, st={"min_age_years": 3.0}, run=None)
    assert peak[0] == n                     # ровно пул: и не последовательно, и не шире
    assert all(s.alive for s in states)


class _FakeWayback:
    def __init__(self, dirty=False, age_years=9.0, checked=True):
        self.dirty, self.age_years, self.checked = dirty, age_years, checked
        self.calls = 0

    def classify_history(self, domain):
        self.calls += 1
        pf = {"adult": False, "pharma": False, "casino": self.dirty,
              "gambling": False, "spam": False}
        return {"prior_flags": pf, "first_seen": None, "age_years": self.age_years,
                "wayback_checked": self.checked, "sampled": 5}


def test_wave_history_rejects_dirty():
    """Находка ревью Task 4: отказ — тоже вердикт, и улики обязаны дойти до записи ДАЖЕ
    когда домен отклонён (иначе инбокс показал бы «грязная история» без единого снимка,
    подтверждающего вердикт) — assert только на alive/reject_reason это не ловил."""
    s = scoring.FunnelState(domain_id=1, domain="a.ru", lane=None, referring_domains=5,
                            acquire_deadline=None, feed_flags=None)
    st = {"min_age_years": 3.0}
    scoring._wave_history([s], {"wayback": _FakeWayback(dirty=True)}, st, run=None)
    assert s.alive is False and s.reject_reason == "history_dirty"
    assert s.sig["wayback_checked"] is True and s.sig["sampled"] == 5


def test_wave_history_age_fallback_rejects_too_young_when_whois_had_no_age():
    s = scoring.FunnelState(domain_id=2, domain="b.ru", lane=None, referring_domains=5,
                            acquire_deadline=None, feed_flags=None)
    st = {"min_age_years": 3.0}
    scoring._wave_history([s], {"wayback": _FakeWayback(age_years=1.0)}, st, run=None)
    assert s.alive is False and s.reject_reason == "too_young"
    assert s.sig["age_source"] == "wayback"


def test_wave_history_takes_older_of_whois_and_wayback_age():
    """Р5: возраст для решения — старшая из даты RDAP/whois и первого снимка. Архивный возраст
    больше whois-ного — побеждает архив; меньше — остаётся whois-ный. Раньше whois всегда
    перебивал архив, и перехваченный домен с долгой историей выглядел молодым."""
    st = {"min_age_years": 3.0}
    recaught = scoring.FunnelState(domain_id=3, domain="c.com", lane=None, referring_domains=5,
                                   acquire_deadline=None, feed_flags=None)
    recaught.sig.update({"whois_created": datetime(2023, 1, 1, tzinfo=timezone.utc),
                         "age_years": 3.5, "age_source": "whois"})
    old = scoring.FunnelState(domain_id=4, domain="d.com", lane=None, referring_domains=5,
                              acquire_deadline=None, feed_flags=None)
    old.sig.update({"whois_created": datetime(2010, 1, 1, tzinfo=timezone.utc),
                    "age_years": 16.0, "age_source": "whois"})
    scoring._wave_history([recaught, old], {"wayback": _FakeWayback(age_years=9.0)}, st, run=None)
    assert recaught.alive and recaught.sig["age_years"] == 9.0
    assert recaught.sig["age_source"] == "wayback"
    assert old.alive and old.sig["age_years"] == 16.0 and old.sig["age_source"] == "whois"


# ============================================================================
# _commit_result tests — БД-трогающие, используют real SessionLocal() с SQLite
# ============================================================================

import app.db as db
from app.models.domain import Domain
from app.models.domain_score_log import DomainScoreLog


def _mk_domain(**kw):
    with db.SessionLocal() as s:
        d = Domain(domain=kw.pop("domain", "commit.ru"), source="cctld",
                   status="discovered", **kw)
        s.add(d); s.commit(); s.refresh(d)
        return d.id


def test_commit_result_writes_rejected_and_log_row():
    did = _mk_domain()
    s = scoring.FunnelState(domain_id=did, domain="commit.ru", lane=None,
                            referring_domains=5, acquire_deadline=None,
                            feed_flags=None)
    s.reject_reason = "rkn"
    s.alive = False
    out = scoring._commit_result(s, run=None, st={"approve_at": 0.7, "manual_review_at": 0.4})
    assert out["status"] == "rejected" and out["reject_reason"] == "rkn"
    with db.SessionLocal() as sess:
        d = sess.get(Domain, did)
        assert d.status == "rejected" and d.reject_reason == "rkn"
        log = sess.query(DomainScoreLog).filter_by(domain_id=did).one()
        assert log.outcome == "rejected" and log.reject_reason == "rkn"


def test_commit_result_writes_unresolved_and_leaves_domain_discovered():
    did = _mk_domain()
    s = scoring.FunnelState(domain_id=did, domain="commit2.ru", lane=None,
                            referring_domains=5, acquire_deadline=None,
                            feed_flags=None)
    s.unresolved_why = "waiting"
    s.alive = False
    out = scoring._commit_result(s, run=None, st={"approve_at": 0.7, "manual_review_at": 0.4})
    assert out["unresolved"] is True and out["why"] == "waiting"
    with db.SessionLocal() as sess:
        d = sess.get(Domain, did)
        assert d.status == "discovered"


def test_commit_result_computes_score_for_survivor():
    did = _mk_domain()
    s = scoring.FunnelState(domain_id=did, domain="commit3.ru", lane=None,
                            referring_domains=5000, acquire_deadline=None,
                            feed_flags=None)
    s.sig.update({"wayback_checked": True, "prior_flags": {}, "age_years": 10,
                 "indexed_echo": True, "dr": None})
    out = scoring._commit_result(s, run=None, st={"approve_at": 0.7, "manual_review_at": 0.4})
    assert out["status"] == "scored" and out["score"] > 0
    with db.SessionLocal() as sess:
        d = sess.get(Domain, did)
        assert float(d.score) == out["score"] and d.status == out["status"]


# ============================================================================
# _run_waves orchestrator tests
# ============================================================================

def test_run_waves_shrinks_pool_across_stages_and_writes_wave_history():
    """10 доменов -> половина выпадает на W2 (занят, лейна и даты нет -> не решить) -> итог:
    waterfall в job_run.message показывает уменьшение пула по волнам."""
    from app.services import jobs

    ids = [_mk_domain(domain=f"pool{i}.com", referring_domains=5) for i in range(10)]
    states = [scoring.FunnelState(domain_id=did, domain=f"pool{i}.com", lane=None,
                                  referring_domains=5, acquire_deadline=None, feed_flags=None)
             for i, did in enumerate(ids)]
    old = datetime.now(timezone.utc) - timedelta(days=365 * 10)

    class _Ap:
        def __init__(self):
            self.n = 0
        def whois_probe(self, d):
            self.n += 1
            # чётные — заняты без даты дропа и без лейна: W2 их не решает (taken_undated)
            return {"available": self.n % 2 != 0, "created": old}
        def safebrowsing_check(self, d): return False
    clients = {"aparser": _Ap(),
              "ahrefs": type("Ah", (), {"units_left": lambda self: 2_000_000,
                                        "batch": lambda self, ds: {d: {} for d in ds}})(),
              "rkn": type("R", (), {"is_listed": lambda self, d: False})(),
              "blacklist": type("B", (), {"is_blacklisted": lambda self, d: False})(),
              "searxng": type("S", (), {"indexed_echo": lambda self, d: True})(),
              "wayback": _FakeWayback(dirty=False, age_years=9.0),
              "_whois_lock": threading.Lock(), "_safebrowsing_lock": threading.Lock()}
    st = {"min_age_years": 3.0, "approve_at": 0.7, "manual_review_at": 0.4,
         "min_referring_domains": 1, "tld_allowlist": ["com"], "brand_tokens": []}

    with jobs.track("score", stages=[dict(x) for x in scoring.FUNNEL_STAGES]) as run:
        out = scoring._run_waves(states, clients, st, whois_budget=None,
                                 links_budget=None, run=run)
    assert len(out) == 10
    survived = [s for s in states if s.alive]
    assert 0 < len(survived) < 10          # реально сжалось, не всё выжило и не всё умерло
    last = jobs.last("score")
    assert "доступность" in last["message"] and ("->" in last["message"] or "→" in last["message"])
    # мини-полоски на чипах (2026-07-21): before/after написаны на КАЖДУЮ стадию, не только
    # в текстовый waterfall — jobCard() их и рисует.
    by_key = {s["key"]: s for s in last["stages"]}
    assert by_key["t0"]["before"] == 10 and by_key["t0"]["after"] == 10
    assert by_key["avail"]["before"] == 10 and by_key["avail"]["after"] == len(survived)
    assert by_key["history"]["before"] == by_key["history"]["after"] == len(survived)


def test_run_waves_cancellation_between_waves_preserves_partial_progress():
    """НЕ ловим jobs.Cancelled сами вокруг вызова: jobs.track() ловит его ВНУТРИ своего
    generator'а (except Cancelled -> _close(..., "cancelled"), БЕЗ re-raise) — поймай
    исключение раньше, до границы `with`, и track() увидит нормальный выход из `with`,
    закрыв прогон как "done", а не "cancelled" (найдено ревью Task 1, 2026-07-21, тот же
    паттерн уже сломал сходный тест в test_scoring_waves.py при первом написании)."""
    from app.services import jobs

    ids = [_mk_domain(domain=f"cancel{i}.com", referring_domains=5) for i in range(5)]
    states = [scoring.FunnelState(domain_id=did, domain=f"cancel{i}.com", lane=None,
                                  referring_domains=5, acquire_deadline=None, feed_flags=None)
             for i, did in enumerate(ids)]
    clients = {"aparser": type("Ap", (), {
                  "whois_probe": lambda self, d: {"available": True, "created": datetime.now(timezone.utc) - timedelta(days=3650)}})(),
              "_whois_lock": threading.Lock()}
    st = {"min_age_years": 3.0, "approve_at": 0.7, "manual_review_at": 0.4,
         "min_referring_domains": 1, "tld_allowlist": ["com"], "brand_tokens": []}

    with jobs.track("score", stages=[dict(x) for x in scoring.FUNNEL_STAGES]) as run:
        jobs.request_cancel("score")
        scoring._run_waves(states, clients, st, whois_budget=None,
                           links_budget=None, run=run)
    last = jobs.last("score")
    assert last["status"] == "cancelled"


# ============================================================================
# score_pending() batch wiring (Task 9) — SELECT builds FunnelState, ONE _run_waves call
# ============================================================================

def test_score_pending_builds_states_with_lane_and_rd_from_one_query(monkeypatch):
    """score_pending больше не должен грузить lane/referring_domains/acquire_deadline
    доменом по домену внутри волны — они обязаны прийти из ИСХОДНОГО SELECT (см. Task 9),
    иначе каждая волна платила бы отдельным SELECT на КАЖДЫЙ домен пачки."""
    did = _mk_domain(domain="batch1.com", referring_domains=5, lane="bid")

    captured = {}
    calls = {"n": 0}
    real_run_waves = scoring._run_waves

    def _spy(states, *a, **kw):
        calls["n"] += 1
        captured["states"] = list(states)
        return real_run_waves(states, *a, **kw)
    monkeypatch.setattr(scoring, "_run_waves", _spy)

    class _Ap:
        def whois_probe(self, d):
            return {"available": True, "created": datetime.now(timezone.utc) - timedelta(days=3650)}
        def safebrowsing_check(self, d): return False
    monkeypatch.setattr(scoring, "_make_clients", lambda: {
        "aparser": _Ap(),
        "rkn": type("R", (), {"is_listed": lambda self, d: False})(),
        "blacklist": type("B", (), {"is_blacklisted": lambda self, d: False})(),
        "searxng": type("S", (), {"indexed_echo": lambda self, d: True})(),
        "wayback": _FakeWayback(), "_whois_lock": threading.Lock(),
        "_safebrowsing_lock": threading.Lock()})

    scoring.score_pending(limit=10)
    assert calls["n"] == 1              # ОДИН вызов на весь батч, не по домену
    assert len(captured["states"]) == 1
    assert captured["states"][0].domain_id == did
    assert captured["states"][0].lane == "bid"
    assert captured["states"][0].referring_domains == 5


def test_score_pending_reports_honest_count_when_cancelled_after_partial_commits(monkeypatch):
    """Task 9 self-review (c). `_run_waves()` на отмене делает `raise jobs.Cancelled()` ДО
    своего `return results` — локальный список результатов теряется вместе со стеком
    развёртывания, ХОТЯ `_checkpoint()` внутри уже мог реально закоммитить в БД домены
    волной(ами) РАНЬШЕ той, где прилетела отмена. Если считать `done=len(results)` голым — при
    отмене он ВСЕГДА 0, даже если реально отброшено N доменов: контракт docstring'а («частичное
    число, не len(rows)») соврёт. Здесь 2 домена feed_flag (W0) + 3 not_acquirable (W2: лейн free,
    а домен занят) реально оседают в БД как rejected до отмены на волне risk."""
    ids = [_mk_domain(domain=f"flag{i}.com", feed_flags={"block": True}, lane="bid") for i in range(2)]
    ids += [_mk_domain(domain=f"taken{i}.com", referring_domains=5, lane="free") for i in range(3)]

    from app.services import jobs as jobs_mod
    real_wave_risk = scoring._wave_risk

    def spy_risk(states, clients, run):
        # к этому моменту W0 и W2 УЖЕ закоммитили все 5 (alive пуст) — отмена здесь
        # проверяет именно то, что происходит МЕЖДУ волнами, после реальных чекпоинтов.
        jobs_mod.request_cancel("score")
        return real_wave_risk(states, clients, run)
    monkeypatch.setattr(scoring, "_wave_risk", spy_risk)

    class _Ap:
        def whois_probe(self, d):
            return {"available": False, "created": datetime.now(timezone.utc) - timedelta(days=3650)}
    monkeypatch.setattr(scoring, "_make_clients", lambda: {
        "aparser": _Ap(), "_whois_lock": threading.Lock()})

    n = scoring.score_pending(limit=10)
    assert n == 5                        # все 5 реально осели в БД, не 0
    assert jobs_mod.last("score")["status"] == "cancelled"
    with db.SessionLocal() as s:
        statuses = {s.get(Domain, i).status for i in ids}
    assert statuses == {"rejected"}


def test_run_waves_reports_every_funnel_stage_in_order(monkeypatch):
    """Порядок чипов (FUNNEL_STAGES) и порядок волн — одно и то же: стадия, о которой волна не
    отчиталась, висела бы на панели «ожидает» вечно. Порядок проверяется по последовательности
    stage_key в jobs.report. Волны и финализация подменены пустышками — тест про конвейер, а не
    про сигналы, поэтому переживает смену набора волн (Задачи 9–13)."""
    from collections import defaultdict
    from app.services import jobs
    from app.services.settings import get_settings

    class _Quiet:
        """Клиент-пустышка: любой метод отвечает None (волны подменены, сеть не нужна)."""
        def __getattr__(self, name):
            return lambda *a, **k: None

    for name in [n for n in dir(scoring) if n.startswith("_wave_")]:
        monkeypatch.setattr(scoring, name, lambda alive, *a, **k: None)
    monkeypatch.setattr(scoring, "_commit_result", lambda s, run, st: {"domain": s.domain})
    real, keys = jobs.report, []

    def spy(run_id, **kw):
        if kw.get("stage_key"):
            keys.append(kw["stage_key"])
        return real(run_id, **kw)
    monkeypatch.setattr(jobs, "report", spy)
    states = [scoring.FunnelState(domain_id=i, domain=f"ord{i}.com", lane="bid", referring_domains=5,
                                  acquire_deadline=None, feed_flags=None) for i in range(2)]
    st = {**get_settings(), "units_floor": 0}
    out = scoring._run_waves(states, defaultdict(_Quiet), st, None, None, None)
    assert keys == [s["key"] for s in scoring.FUNNEL_STAGES]
    assert [r["domain"] for r in out] == ["ord0.com", "ord1.com"]
