"""Волновой оркестратор скоринга: FunnelState, Budget, конкурентный харнесс, волны."""
import threading

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
