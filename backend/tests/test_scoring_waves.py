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


def test_wave_t0_rejects_feed_flag_and_low_rd_without_touching_alive_ones():
    st = {"min_referring_domains": 5}
    flagged = scoring.FunnelState(domain_id=1, domain="a.ru", lane=None,
                                  referring_domains=10, acquire_deadline=None,
                                  feed_flags={"rkn": True})
    low_rd = scoring.FunnelState(domain_id=2, domain="b.ru", lane=None,
                                 referring_domains=1, acquire_deadline=None,
                                 feed_flags=None)
    ok = scoring.FunnelState(domain_id=3, domain="c.ru", lane=None,
                             referring_domains=50, acquire_deadline=None,
                             feed_flags=None)
    states = [flagged, low_rd, ok]
    scoring._wave_t0(states, st)
    assert flagged.alive is False and flagged.reject_reason == "feed_flag"
    assert low_rd.alive is False and low_rd.reject_reason == "low_rd"
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


def _clients_no_tci(**kw):
    return {"aparser": _FakeAparserWhois(**kw),
            "tci": type("T", (), {"handles": lambda self, d: False})(),
            "_whois_lock": threading.Lock()}


def test_wave_whois_rejects_too_young_bid_domain():
    st = {"min_age_years": 3.0}
    young = datetime.now(timezone.utc) - timedelta(days=200)
    s = scoring.FunnelState(domain_id=1, domain="young.ru", lane="bid",
                            referring_domains=5, acquire_deadline=None, feed_flags=None)
    clients = _clients_no_tci(available=False, created=young)
    scoring._wave_whois([s], clients, budget=None, st=st, run=None)
    assert s.alive is False and s.reject_reason == "too_young"


def test_wave_whois_marks_free_lane_and_survives():
    st = {"min_age_years": 3.0}
    old = datetime.now(timezone.utc) - timedelta(days=365 * 10)
    s = scoring.FunnelState(domain_id=2, domain="free.ru", lane=None,
                            referring_domains=5, acquire_deadline=None, feed_flags=None)
    clients = _clients_no_tci(available=True, created=old)
    scoring._wave_whois([s], clients, budget=None, st=st, run=None)
    assert s.alive is True and s.sig["lane"] == "free"


def test_wave_whois_budget_exhausted_marks_unresolved_without_network_call():
    st = {"min_age_years": 3.0}
    s = scoring.FunnelState(domain_id=3, domain="over.ru", lane=None,
                            referring_domains=5, acquire_deadline=None, feed_flags=None)
    aparser = _FakeAparserWhois(available=True)
    clients = {"aparser": aparser, "tci": type("T", (), {"handles": lambda self, d: False})(),
               "_whois_lock": threading.Lock()}
    budget = scoring.Budget(0)
    scoring._wave_whois([s], clients, budget=budget, st=st, run=None)
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


def test_wave_whois_breaker_lock_has_no_lost_increments_under_real_overlap():
    """20 доменов, конкурентность 12, whois всегда падает С ЗАДЕРЖКОЙ (форсирует
    настоящее перекрытие потоков, не последовательный проход). Проверяем ИНВАРИАНТ
    ЗАМКА напрямую: ни один инкремент не потерян (whois_failures == calls) — именно
    это гонка read-modify-write без лока и ломает. Предохранитель реально остановил
    часть волны (calls < 20), но точное число попыток до срабатывания (обычно 12,
    ширина пула) НЕ проверяем — оно зависит от планировщика ОС и было бы флаки-тестом."""
    st = {"min_age_years": 3.0}
    states = [scoring.FunnelState(domain_id=i, domain=f"slow{i}.ru", lane=None,
                                  referring_domains=5, acquire_deadline=None,
                                  feed_flags=None) for i in range(20)]
    aparser = _SlowAlwaysFailAparser()
    clients = {"aparser": aparser, "tci": type("T", (), {"handles": lambda self, d: False})(),
              "_whois_lock": threading.Lock()}
    scoring._wave_whois(states, clients, budget=None, st=st, run=None)
    assert all(not s.alive and s.unresolved_why == "whois_failed" for s in states)
    assert aparser.whois_failures == aparser.calls    # ни один инкремент не потерян под локом
    assert 3 <= aparser.whois_failures < 20           # предохранитель реально сработал и что-то остановил


class _SpyLock:
    """Обёртка над настоящим Lock, которая ЗАПИСЫВАЕТ каждый вход — доказывает, что
    _aparser_whois реально берёт лок вокруг ОБЕИХ операций (гейт-чек чтения И запись
    счётчика), а не только вокруг одной из них. Гонка на голом += 1 под GIL слишком
    редкая, чтобы ловить её таймингом надёжно за разумное число прогонов (проверено
    вручную: даже БЕЗ лока 5/5 прогонов соседнего теста не потеряли ни одного
    инкремента) — этот тест проверяет структуру блокировки напрямую, детерминированно."""
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
