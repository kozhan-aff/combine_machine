"""Реестр задач в БД: single-flight между процессами, стадии, отмена, итог прогона."""
import pytest

from app.services import jobs

STAGES = [{"key": "rd", "label": "RD из фида"},
          {"key": "whois", "label": "whois-возраст"},
          {"key": "history", "label": "Wayback-история"}]


def test_track_writes_row_and_closes_done():
    with jobs.track("score", stages=STAGES) as run:
        jobs.report(run, done=1, total=3, current="a.ru", stage="whois")
        p = jobs.progress("score")
        assert p["running"] is True and p["done"] == 1 and p["current"] == "a.ru"
        st = {s["key"]: s["state"] for s in p["stages"]}
        assert st == {"rd": "done", "whois": "active", "history": "pending"}
    p = jobs.progress("score")
    assert p["status"] == "done" and p["running"] is False and p["error"] is None


def test_single_flight_between_processes():
    """Второй track на тот же джоб отбивается индексом — это и есть замок панель↔воркер."""
    with jobs.track("discovery"):
        with pytest.raises(jobs.AlreadyRunning):
            with jobs.track("discovery"):
                pass


def test_double_spawn_rejected_before_row_exists():
    """Гонка своего процесса: строку job_run открывает уже ПОТОК, а spawn проверяет замок из
    потока вызывающего. Без _INFLIGHT второй клик проскакивал в это окно, и панель врала
    «запущено», хотя второй прогон тут же умирал об AlreadyRunning."""
    import threading
    gate = threading.Event()
    assert jobs.spawn("score", lambda: gate.wait(5)) is True
    assert jobs.spawn("score", lambda: gate.wait(5)) is False   # без гонок, сразу
    gate.set()
    jobs._reset()      # _INFLIGHT живёт на процесс, а не на БД — иначе течёт в соседний тест


def test_cancel_marks_cancelled_and_keeps_progress():
    with jobs.track("recheck") as run:
        jobs.report(run, done=34, total=100)
        assert jobs.cancelled(run) is False
        jobs.request_cancel("recheck")     # кнопка знает ИМЯ джоба, а не id прогона
        assert jobs.cancelled(run) is True
        if jobs.cancelled(run):
            raise jobs.Cancelled()          # так делает сервис между доменами
    p = jobs.progress("recheck")
    assert p["status"] == "cancelled" and p["done"] == 34 and p["total"] == 100


def test_failure_records_stage_where_it_broke():
    """Упавшая задача обязана показать, НА КАКОЙ стадии встала (макет new-03)."""
    with pytest.raises(RuntimeError):
        with jobs.track("score", stages=STAGES) as run:
            jobs.report(run, done=18, total=100, stage="whois")
            raise RuntimeError("A-Parser timeout")
    p = jobs.progress("score")
    assert p["status"] == "failed" and "timeout" in p["error"] and p["done"] == 18
    assert p["stage"] == "whois"


def test_live_shows_running_jobs_only():
    with jobs.track("sweep", trigger="auto"):
        names = [j["name"] for j in jobs.live()]
        assert names == ["sweep"]
        assert jobs.live()[0]["trigger"] == "auto"
    assert jobs.live() == []


def test_last_returns_finished_run_with_message():
    with jobs.track("recheck") as run:
        jobs.report(run, done=200, total=200, message="занято 3 из отобранных")
    assert jobs.last("recheck")["message"] == "занято 3 из отобранных"
    assert jobs.last("discovery") is None


def test_report_outside_track_is_noop():
    """score_domain по одной кнопке и юнит-тесты зовут report без открытого прогона."""
    jobs.report(None, done=1, total=1)        # нет прогона — нечего адресовать, не падаем
    assert jobs.progress("score")["running"] is False


def test_stale_run_is_shown_not_killed():
    """Чтение реестра НЕ гасит прогон: поллер панели зовёт live() каждые 1.5с, а стадия свипа
    (LLM по 5 сайтам) законно молчит дольше STALE_MIN. Реап на пути чтения убил бы ЖИВУЮ задачу
    и отпустил замок под вторую. live() только помечает stale; гасит — попытка занять замок."""
    from datetime import timedelta

    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models.job import JobRun

    with jobs.track("sweep"):
        with SessionLocal() as db:                       # состарить строку на месте
            r = db.execute(select(JobRun)).scalars().one()
            r.updated_at = jobs._utcnow() - timedelta(minutes=jobs.STALE_MIN + 1)
            db.commit()
        live = jobs.live()
        assert live[0]["status"] == "running"            # НЕ убита чтением
        assert live[0]["stale"] is True                  # но честно помечена «оборвалась»
        assert jobs.is_running("sweep") is False         # захват замка её гасит...
    assert jobs.live() == []


def test_track_closes_done_warn_when_body_signals():
    """Тело (свип) может заявить свой терминальный исход: «прошёл, но с замечаниями» — не
    зелёный `done`, но и не `failed` (F2.2). track подхватывает то, что сказало jobs.finish()."""
    with jobs.track("sweep") as run:
        jobs.finish(run, "done_warn")         # тело: «прошёл, но с замечаниями»
    p = jobs.progress("sweep")
    assert p["status"] == "done_warn"


def test_reaped_run_frees_the_lock():
    """Убитый контейнер не должен запирать джоб навсегда."""
    from datetime import timedelta

    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models.job import JobRun

    with jobs.track("discovery"):
        with SessionLocal() as db:
            r = db.execute(select(JobRun)).scalars().one()
            r.updated_at = jobs._utcnow() - timedelta(minutes=jobs.STALE_MIN + 1)
            db.commit()
        with jobs.track("discovery"):                    # ...и следующий прогон стартует
            assert jobs.progress("discovery")["running"] is True
