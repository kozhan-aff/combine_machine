"""Лизинг замка задач (аудит F17): замок держится СЕРДЦЕБИЕНИЕМ, а не фактом строки.

Баг: `updated_at` не трогал никто, кроме report(), а report() зовётся только на границах
стадий. Стадия `generate` — это ОДИН вызов LLM (минуты), стадия `score` с капом 200 доменов —
это ~200 минут whois (CLAUDE.md: «воронка ~60 с/домен»). Обе молчали дольше STALE_MIN, и первый
же, кто хотел занять замок, честно принимал живую задачу за труп, гасил её строку и уезжал
работать ВТОРЫМ: два свипа в двух процессах = дубли страниц и двойной счёт LLM.

ПОЧЕМУ ЗДЕСЬ ГОЛЫЙ Thread, А НЕ jobs.spawn (главная ловушка этих тестов). `spawn` кладёт имя
джоба в `_INFLIGHT` — сет СВОЕГО процесса — и второй `spawn` отбивается об него, ДАЖЕ не
заглянув в БД. Тест на spawn+spawn был бы зелёным и на сломанном коде: он проверял бы
внутрипроцессный сет вместо замка. А чинить надо МЕЖПРОЦЕССНЫЙ случай (свип живёт в worker'е,
кнопку жмут в backend'е). Голый `track` в потоке — это ровно «чужой процесс держит строку»:
`_INFLIGHT` пуст, и вопрос «занято?» уходит туда, где ему и место, — в БД.
"""
import threading
from datetime import timedelta

from sqlalchemy import select

from app.db import SessionLocal
from app.models.job import JobRun
from app.services import jobs

# Насколько состариваем строку. КОНСТАНТА, а не STALE_MIN + 1: фикстура, отмеренная от самого
# порога, сидит к нему вплотную и краснеет/зеленеет от правки порога, а не от поведения (урок
# ветки, оплаченный шесть раз; тот же приём — `_DEAD_FOR` в test_order_recovery.py). Что 90 минут
# заведомо больше порога, стережёт test_threshold_tolerates_missed_beats ниже.
_SILENT_FOR = timedelta(minutes=90)


def _age(run_id: int) -> None:
    """Сдвигаем ЧАСЫ, а не состояние: строку поставил живой код (track), молчание — это просто
    время. Так выглядит задача, которая полтора часа сидит внутри одного вызова LLM/whois."""
    with SessionLocal() as db:
        r = db.get(JobRun, run_id)
        r.updated_at = jobs._utcnow() - _SILENT_FOR
        db.commit()


def _updated_at(run_id: int):
    """SQLite отдаёт DateTime naive, PostgreSQL — tz-aware; голое сравнение с now(tz) роняет
    TypeError. Нормализуем — тем же приёмом, что jobs._is_stale."""
    from datetime import timezone
    with SessionLocal() as db:
        u = db.execute(select(JobRun.updated_at).where(JobRun.id == run_id)).scalar()
    return u if u is None or u.tzinfo else u.replace(tzinfo=timezone.utc)


def _wait_until(pred, timeout: float = 3.0) -> bool:
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return False


def test_threshold_tolerates_missed_beats():
    """Порог обязан переживать НЕСКОЛЬКО пропущенных ударов.

    Гард на числа: порог короче интервала сердцебиения — это ровно исходный баг (живой не
    успевает подать признак жизни, и его убивают). Три удара — запас на паузу GC, затык пула
    и медленную БД."""
    assert jobs.STALE_MIN * 60 >= jobs.HEARTBEAT_SEC * 3, (
        "порог протухания короче трёх ударов сердца — живую задачу снова начнут убивать")


def test_live_but_silent_job_keeps_its_lock(monkeypatch):
    """РЕГРЕССИЯ F17. Задача молчит дольше порога (одна длинная стадия), но подаёт признаки
    жизни — замок остаётся за ней, и второй прогон НЕ стартует."""
    # 60 с в тест не помещаются. raising=False НАМЕРЕННО: до фикса константы нет вовсе, и тест
    # обязан краснеть на ПОВЕДЕНИИ (замок отдали живой задаче), а не на отсутствии имени.
    monkeypatch.setattr(jobs, "HEARTBEAT_SEC", 0.05, raising=False)
    opened, release = threading.Event(), threading.Event()
    box = {}

    def _worker():
        # «Процесс воркера»: свип внутри стадии generate. НИ ОДНОГО report — он и не должен
        # быть нужен, чтобы удержать замок: работа идёт, просто рассказать о ней пока нечего.
        with jobs.track("sweep") as run:
            box["run"] = run
            opened.set()
            release.wait(5)

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    try:
        assert opened.wait(3), "прогон не открылся"
        run = box["run"]

        _age(run)                                       # молчит полтора часа...
        assert _wait_until(lambda: _updated_at(run) > jobs._utcnow() - _SILENT_FOR / 2), (
            "живая задача не подала признаков жизни: сердцебиения нет, реап сочтёт её трупом")

        # Пульт/воркер пробуют занять замок — путь, на котором и происходило убийство.
        assert jobs.is_running("sweep") is True, "замок отдан ЖИВОЙ задаче — её строку погасили"
        assert jobs.spawn("sweep", lambda: None) is False, (
            "второй свип поехал поверх живого: дубли страниц и двойной счёт LLM")
        with SessionLocal() as db:
            r = db.get(JobRun, run)
            assert r.status == "running", "живую задачу пометили упавшей"
    finally:
        release.set()
        t.join(timeout=5)
        jobs._reset()      # _INFLIGHT живёт на процесс — на сломанном коде spawn мог его занять


def test_dead_job_still_frees_the_lock():
    """Обратная сторона: сердцебиение не должно сделать замок вечным.

    Убитый контейнер оставляет running-строку, за которой НЕТ живого процесса — стучать некому.
    Такую по-прежнему гасим, иначе джоб не запустить больше никогда."""
    dead = jobs._open("score", "auto", None)            # строка есть, процесса за ней нет
    assert dead is not None
    _age(dead)

    assert jobs.is_running("score") is False, "труп запер джоб навсегда"
    with SessionLocal() as db:
        assert db.get(JobRun, dead).status == "failed"
    with jobs.track("score"):                           # ...и следующий прогон стартует
        assert jobs.progress("score")["running"] is True


def test_zombie_cannot_write_into_the_live_run(monkeypatch):
    """РЕГРЕССИЯ F17. Замок у нас всё-таки отобрали (сердцебиение не дошло — БД лежала), а
    процесс жив и продолжает репортить. Он обязан писать в СВОЮ мёртвую строку.

    Раньше report() искал строку ПО ИМЕНИ («running с именем sweep») — то есть зомби находил не
    себя, а ЖИВОЙ ПРОГОН ПРЕЕМНИКА и дописывал прогресс в него. Панель показывала операторy
    чужие стадии и чужой счётчик как прогресс его нового свипа.

    Гоняем НАСТОЯЩИЙ run_sweep: адресация по имени — это то, как звал сервис, и сломано было
    именно там. Стадии подменяем, чтобы первую можно было задержать.
    """
    from app.services import autonomy, orchestrator as orch

    entered, release = threading.Event(), threading.Event()

    def _slow(cap):
        entered.set()
        release.wait(5)
        return 1, []

    monkeypatch.setattr(orch, "STAGES", [
        ("score", "auto_score", "cap_score", _slow),
        ("queue", "auto_queue", "cap_queue", lambda cap: (7, [])),
    ])
    autonomy.update_autonomy(autopilot_on=True, auto_score=True, auto_queue=True)

    t = threading.Thread(target=lambda: orch.run_sweep(trigger="cron"), daemon=True)
    t.start()
    try:
        assert entered.wait(3), "свип не дошёл до первой стадии"
        with SessionLocal() as db:
            zombie = db.execute(select(JobRun.id).where(JobRun.name == "sweep")).scalar()

        # БД лежала, удар не дошёл -> строку сочли трупом и отдали замок. Ровно так и остаётся
        # достижимым зомби после фикса: сердцебиение — страховка, а не гарантия.
        _age(zombie)
        assert jobs.is_running("sweep") is False       # реап забрал замок у молчащего
        successor = jobs._open("sweep", "manual", None)  # оператор запустил свой свип
        assert successor is not None and successor != zombie

        release.set()                                  # зомби доработал стадию и репортит дальше
        t.join(timeout=5)

        with SessionLocal() as db:
            live = db.get(JobRun, successor)
            assert live.status == "running", "зомби закрыл ЧУЖОЙ прогон"
            assert live.done == 0 and live.stage == "" and live.current == "", (
                "зомби дописал свой прогресс в живой прогон преемника — "
                f"панель показывает чужую работу как эту (done={live.done}, stage={live.stage!r})")
            assert db.get(JobRun, zombie).status == "failed"   # свою строку он не воскресил
    finally:
        release.set()
        t.join(timeout=5)


def test_zombie_stops_when_its_lock_is_gone(monkeypatch):
    """Мало не дать зомби писать — он обязан ОСТАНОВИТЬСЯ. Продолжать конвейер без замка значит
    гнать его вторым: те же сайты, те же страницы, второй счёт LLM. cancelled() отвечает «пора»
    не только на стоп-кнопку, но и на «строка больше не наша»."""
    from app.services import autonomy, orchestrator as orch

    entered, release = threading.Event(), threading.Event()
    ran = []

    def _slow(cap):
        entered.set()
        release.wait(5)
        return 1, []

    def _second(cap):
        ran.append("queue")               # ЭТА стадия не имеет права выполниться
        return 7, []

    monkeypatch.setattr(orch, "STAGES", [
        ("score", "auto_score", "cap_score", _slow),
        ("queue", "auto_queue", "cap_queue", _second),
    ])
    autonomy.update_autonomy(autopilot_on=True, auto_score=True, auto_queue=True)

    t = threading.Thread(target=lambda: orch.run_sweep(trigger="cron"), daemon=True)
    t.start()
    try:
        assert entered.wait(3), "свип не дошёл до первой стадии"
        with SessionLocal() as db:
            zombie = db.execute(select(JobRun.id).where(JobRun.name == "sweep")).scalar()
        _age(zombie)
        assert jobs.is_running("sweep") is False        # замок отобрали
        release.set()
        t.join(timeout=5)
        assert ran == [], "зомби продолжил конвейер без замка — вторая стадия поехала дублем"
    finally:
        release.set()
        t.join(timeout=5)
