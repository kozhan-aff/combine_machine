"""HTTP-поверхность реестра: живой список, стоп, возврат туда, откуда нажали."""
import app.db as db
from app.models.domain import Domain
from app.models.domain_score_log import DomainScoreLog
from app.services import jobs


def test_live_lists_running_job(client):
    with jobs.track("score", stages=[{"key": "rd", "label": "RD из фида"}]) as run:
        jobs.report(run, done=3, total=10, current="a.ru", stage="rd")
        r = client.get("/api/jobs/live")
        assert r.status_code == 200
        body = r.json()
        assert body["jobs"][0]["name"] == "score"
        assert body["jobs"][0]["done"] == 3 and body["jobs"][0]["current"] == "a.ru"
        assert body["jobs"][0]["stages"][0]["state"] == "active"


def _mk_domain(name="tally.ru") -> int:
    with db.SessionLocal() as s:
        d = Domain(domain=name, source="cctld", status="discovered")
        s.add(d); s.commit(); s.refresh(d)
        return d.id


def _seed_log(domain_id, run_id, outcome, reject_reason=None, n=1):
    with db.SessionLocal() as s:
        for _ in range(n):
            s.add(DomainScoreLog(domain_id=domain_id, run_id=run_id, outcome=outcome,
                                 reject_reason=reject_reason, score=None, sig={}))
        s.commit()


def test_live_includes_funnel_tally_for_score_job(client):
    """Живая раскладка исхода прогона (F: индикатор так себе работал — все стадии выглядели
    «на 1 домен разом», без видимости, что дешёвые реально отсеивают быстро)."""
    did = _mk_domain()
    with jobs.track("score") as run:
        _seed_log(did, run, "rejected", "low_rd", n=3)
        _seed_log(did, run, "rejected", "history_dirty", n=1)
        _seed_log(did, run, "rejected", "low_score", n=1)
        _seed_log(did, run, "scored", n=2)
        _seed_log(did, run, "unresolved", n=1)
        jobs.report(run, done=8, total=100, current="x.ru")
        body = client.get("/api/jobs/live").json()
        t = body["jobs"][0]["tally"]
        assert t["total"] == 8
        assert t["scored"] == 2 and t["unresolved"] == 1
        # low_score рождается ТОЛЬКО когда _funnel() прошёл ДО КОНЦА без раннего выхода
        # (scoring.py:799) — значит Wayback уже сожжён, ровно как у history_dirty (находка
        # ревью 2026-07-20: без low_score здесь счётчик "решено дёшево" завышался бы).
        assert t["reached_wayback"] == 4      # 2 scored + 1 history_dirty + 1 low_score
        assert t["before_wayback"] == 4        # остальное — до Wayback не дошло
        assert t["by_reason"]["мало доноров"] == 3     # reject_ru("low_rd")


def test_live_tally_absent_before_any_row_and_for_non_funnel_jobs(client):
    """Свежий старт (ещё ни одной строки в domain_score_log) — tally=None, не нулевая
    раскладка как будто уже что-то посчитано. discovery/sweep/cf_sync его вообще не считают."""
    with jobs.track("score") as run:
        jobs.report(run, done=0, total=100)
        body = client.get("/api/jobs/live").json()
        assert body["jobs"][0]["tally"] is None
    did = _mk_domain("tally2.ru")
    with jobs.track("discovery") as run2:
        jobs.report(run2, done=1, total=1)
        _seed_log(did, run2, "scored", n=1)     # даже если бы строки были — discovery не считаем
        body = client.get("/api/jobs/live").json()
        assert "tally" not in body["jobs"][0]


def test_live_reports_last_run_when_idle(client):
    with jobs.track("recheck") as run:
        jobs.report(run, done=200, total=200, message="занято 3")
    body = client.get("/api/jobs/live").json()
    assert body["jobs"] == []
    assert body["last"]["recheck"]["message"] == "занято 3"
    assert body["last"]["discovery"] is None


def test_cancel_sets_flag(client):
    with jobs.track("score") as run:
        r = client.post("/run/score/cancel", follow_redirects=False)
        assert r.status_code == 303
        assert jobs.cancelled(run) is True


def test_run_returns_to_page_it_was_pressed_on(client, monkeypatch):
    """Кнопки запуска есть и на Пульте — редирект обязан вернуть туда, откуда нажали.

    Referer ОБЯЗАН быть http://testserver/...: csrf_guard (main.py) отбивает 403, если хост
    Referer'а не равен Host. С чужим Referer'ом тест проверял бы 403, а не редирект."""
    monkeypatch.setattr(jobs, "spawn", lambda name, target: True)
    r = client.post("/run/discovery", headers={"referer": "http://testserver/"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/"          # вернулись на Пульт, а не на жёсткий /domains


def test_run_falls_back_when_no_referer(client, monkeypatch):
    """Referrer-Policy может срезать заголовок — тогда возвращаемся на /domains, а не в никуда."""
    monkeypatch.setattr(jobs, "spawn", lambda name, target: True)
    r = client.post("/run/score", data={"n": "5"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/domains"


def test_progress_route_is_gone(client):
    """Старый /run/{job}/progress пускал только discovery|score|sweep, а панель поллила
    ещё и recheck -> 404 -> полоса молча не появлялась. Роут заменён на /api/jobs/live."""
    assert client.get("/run/recheck/progress").status_code == 404
    assert client.get("/run/score/progress").status_code == 404
