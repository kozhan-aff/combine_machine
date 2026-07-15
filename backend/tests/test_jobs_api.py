"""HTTP-поверхность реестра: живой список, стоп, возврат туда, откуда нажали."""
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
