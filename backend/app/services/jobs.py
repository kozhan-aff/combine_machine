"""Реестр длинных задач — строка в БД на прогон (кросс-процессный).

Раньше это был dict в памяти процесса backend: свип автопилота крутится в процессе worker,
поэтому панель его не видела вовсе — «машина работает» было непроверяемо на глаз. Теперь
и панель, и воркер пишут в одну таблицу job_run.

КОНТРАКТ:
  track(name, trigger=, stages=)  контекст-менеджер вокруг работы; сам закрывает строку
                                  (done / failed / cancelled). Занято -> AlreadyRunning.
  spawn(name, target) -> bool     запустить target() в фоне (панель); False если уже идёт.
  report(name, ...)               обновить прогресс/стадию текущего прогона; вне track — no-op.
  cancelled(name) -> bool         сервис спрашивает между элементами; True -> поднять Cancelled.
  request_cancel(name)            кнопка «стоп».
  live() / last(name)             что идёт сейчас / итог последнего прогона.

ТЕРМИНАЛЬНЫЙ КОНТРАКТ (JS-компонент держится его же):
  status == "running"    -> идёт; done/total/current/stages — прогресс;
  status == "failed"     -> упал; error — текст, done — где встал, stage — на какой стадии;
  status == "cancelled"  -> остановлен человеком на done/total;
  status == "done"       -> успех, ДАЖЕ если done == total == 0 (discovery без кандидатов).
  done/total — только отображение, не признак терминала.

Замок — частичный уникальный индекс (name) WHERE status='running'. Строку, чей updated_at
старше STALE_MIN, считаем оборванной (контейнер перезапустили).

ГДЕ ГАСИМ ПРОТУХШЕЕ (_reap): ТОЛЬКО на пути захвата замка — _open() и is_running(). НИКОГДА
на пути чтения (live/progress/last): панель поллит live() раз в 1.5с, а стадия свипа `generate`
(LLM по 5 сайтам) законно молчит дольше STALE_MIN — реап в live() пометил бы ЖИВУЮ задачу
упавшей и отпустил бы замок под вторую. Чтение только считает флаг stale: «оборвалась» — это
показ, а не мутация. Гасим ровно тогда, когда замок кому-то реально понадобился.
"""
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

STALE_MIN = 15                       # как STALE_MIN оркестратора: running без обновлений = труп
# по потоку на КАЖДОЕ имя джоба (discovery|score|recheck|sweep). Меньше — и третий одновременный
# запуск молча ляжет в очередь пула: строки в реестре ещё нет, панель ничего не рисует, кнопка
# выглядит сломанной. Один оператор, четыре кнопки — четыре потока.
_EXEC = ThreadPoolExecutor(max_workers=4)
# уже отданные в пул, но ещё не открывшие свою строку в БД (см. spawn) — гонка своего процесса
_INFLIGHT: set[str] = set()
# RLock (не Lock): is_running() теперь тоже смотрит в _INFLIGHT (см. её докстринг и правку ниже),
# а spawn() зовёт is_running() уже ДЕРЖА _INFLIGHT_LOCK — обычный Lock тут бы сам себя запер.
_INFLIGHT_LOCK = threading.RLock()
# ДОПОЛНЕНИЕ ПРОТИВ СПЕКИ (эмпирически обнаружено при выполнении этой задачи, не было в брифе):
# сериализует ЛЮБОЙ доступ к job_run в пределах ЭТОГО процесса. На офлайн SQLite-харнессе тестов
# `sqlite_db` держит один-единственный физический коннекшен на StaticPool (нужно, чтобы фоновый
# поток spawn() и вызывающий поток видели ОДНУ строку). `PRAGMA compile_options` на этой машине
# показал `THREADSAFE=2` («multi-thread»): SQLite сам документирует этот режим как небезопасный,
# если ОДИН коннекшен используют 2+ потока ОДНОВРЕМЕННО, — воспроизводимый segfault (100% на
# `test_jobs.py`, который реально гоняет `spawn()` в фоновом потоке, пока вызывающий поток
# параллельно поллит `is_running()`/`report()` тем же коннекшеном). Старый in-memory jobs.py
# никогда не касался БД из фонового потока — гонка физически не могла возникнуть. RLock (не
# Lock) — `progress()` вызывает `last()` изнутри уже захваченного лока тем же потоком.
# На проде (PostgreSQL, свой пул на подключение) эта гонка невозможна в принципе — лок там
# не более чем безвредная микро-сериализация редких админ-операций; кросс-процессный замок
# по-прежнему держит частичный уникальный индекс в БД, этот RLock его не подменяет.
_DB_LOCK = threading.RLock()
# ДОПОЛНЕНИЕ ПРОТИВ СПЕКИ (см. выше): Future каждого spawn() — тестовый харнесс дренирует их
# в _drain() ПЕРЕД тем, как снести SQLite-движок (см. conftest.py::_drain_background_jobs).
# Без этого фоновый поток теста, который не дождался is_running()==False сам (реальный кейс:
# test_autopilot_panel.py::test_autopilot_run_starts_job поллит побочный эффект, а не реестр),
# доживает до drop_all() СЛЕДУЮЩЕГО теста и рвёт общий коннекшен.
_FUTURES: list = []
_log = logging.getLogger(__name__)


class AlreadyRunning(RuntimeError):
    """Такая задача уже идёт (замок держит другая вкладка или воркер)."""


class Cancelled(Exception):
    """Человек нажал «стоп». Сервис поднимает это между элементами, track ловит."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _reap(db) -> None:
    """Погасить оборванные прогоны — иначе замок вечен, и джоб больше не запустить."""
    from sqlalchemy import select
    from app.models.job import JobRun
    cutoff = _utcnow() - timedelta(minutes=STALE_MIN)
    stale = db.execute(select(JobRun).where(JobRun.status == "running",
                                            JobRun.updated_at < cutoff)).scalars().all()
    for r in stale:
        r.status = "failed"
        r.error = "оборвалась: процесс перезапустили"
        r.finished_at = _utcnow()
    if stale:
        db.commit()


def _running(db, name: str):
    from sqlalchemy import select
    from app.models.job import JobRun
    return db.execute(select(JobRun).where(JobRun.name == name,
                                           JobRun.status == "running")).scalars().first()


def _is_stale(r) -> bool:
    """running-строка без обновлений дольше STALE_MIN = контейнер убили. Только ПОКАЗ.

    Дату нормализуем: SQLite отдаёт DateTime naive, PostgreSQL — tz-aware; голое сравнение
    с now(tz) роняет TypeError (тот же приём — в scoring.acquirability_verdict)."""
    if r.status != "running" or r.updated_at is None:
        return False
    u = r.updated_at
    if u.tzinfo is None:
        u = u.replace(tzinfo=timezone.utc)
    return u < _utcnow() - timedelta(minutes=STALE_MIN)


def _as_dict(r) -> dict:
    # ДОПОЛНЕНИЕ ПРОТИВ СПЕКИ (эмпирически обнаружено, не было в брифе): started_at/finished_at
    # отдаём ISO-строкой, не сырым datetime. Панель (backend/app/api/panel.py, вне периметра этой
    # задачи — Task 2/3) на роуте `GET /run/{job}/progress` делает `JSONResponse(jobs.progress(job))`
    # БЕЗ jsonable_encoder — с сырым datetime это 100% `TypeError: Object of type datetime is not
    # JSON serializable` (воспроизведено: test_pipeline.py::test_run_score_double_start_and_
    # progress_route). Старый _blank() отдавал только JSON-безопасные running/done/total/current/
    # message/error — новый более широкий контракт (см. брифа «Форма dict») обязан остаться
    # JSON-безопасным СРАЗУ, раз этот же роут его сериализует напрямую и трогать panel.py в этой
    # задаче нельзя. ISO-строка — тот же формат, в котором это в любом случае уйдёт по HTTP.
    return {"name": r.name, "trigger": r.trigger, "status": r.status, "stage": r.stage,
            "stages": r.stages or [], "done": r.done, "total": r.total, "current": r.current,
            "message": r.message, "error": r.error, "cancel_requested": r.cancel_requested,
            "running": r.status == "running", "stale": _is_stale(r),
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "finished_at": r.finished_at.isoformat() if r.finished_at else None}


def _blank() -> dict:
    return {"name": "", "trigger": "", "status": "", "stage": "", "stages": [], "done": 0,
            "total": 0, "current": "", "message": "", "error": None, "cancel_requested": False,
            "running": False, "stale": False, "started_at": None, "finished_at": None}


def _open(name: str, trigger: str, stages: list | None) -> int | None:
    """Атомарно занять замок; вернуть id строки или None (уже идёт)."""
    from sqlalchemy.exc import IntegrityError
    from app.db import SessionLocal
    from app.models.job import JobRun
    with _DB_LOCK, SessionLocal() as db:
        _reap(db)
        row = JobRun(name=name, trigger=trigger, status="running",
                     stages=[{**s, "state": s.get("state", "pending")} for s in (stages or [])])
        db.add(row)
        try:
            db.commit()
        except IntegrityError:              # частичный уникальный индекс: этот джоб уже идёт
            db.rollback()
            return None
        return row.id


def _close(run_id: int, status: str, error: str | None = None) -> None:
    from app.db import SessionLocal
    from app.models.job import JobRun
    with _DB_LOCK, SessionLocal() as db:
        r = db.get(JobRun, run_id)
        if r is None:
            return
        r.status = status
        r.error = error
        r.finished_at = _utcnow()
        r.updated_at = _utcnow()
        if status == "done":                # успех — все чипы гасим в «пройдено»
            r.stages = [s if s.get("state") == "skip" else {**s, "state": "done"}
                        for s in (r.stages or [])]
        db.commit()


@contextmanager
def track(name: str, *, trigger: str = "manual", stages: list | None = None):
    """Обернуть работу записью реестра. Кто бы ни позвал сервис — панель, оркестратор,
    cron воркера — прогресс появляется бесплатно."""
    run_id = _open(name, trigger, stages)
    if run_id is None:
        raise AlreadyRunning(name)
    try:
        yield run_id
    except Cancelled:
        _close(run_id, "cancelled")         # остановлен человеком — это не ошибка
    except BaseException as e:              # BaseException: ловушки сети в тестах — тоже финал
        _close(run_id, "failed", f"{type(e).__name__}: {e}"[:200])
        raise
    else:
        _close(run_id, "done")


def _advance(stages: list, active: str) -> list:
    """Стадии до активной — пройдены, активная — active, после — pending; skip не трогаем.

    Пересчёт, а не «только вперёд»: скоринг гоняет воронку заново на КАЖДОМ домене, и чипы
    обязаны это показывать (новый домен снова начинает с RD).
    """
    out, seen = [], False
    for s in stages or []:
        if s.get("state") == "skip":
            out.append(s)
            continue
        if s["key"] == active:
            seen = True
            out.append({**s, "state": "active"})
        else:
            out.append({**s, "state": "pending" if seen else "done"})
    return out


def report(name: str, done: int | None = None, total: int | None = None,
           current: str | None = None, stage: str | None = None,
           message: str | None = None) -> None:
    """Обновить текущий прогон. Вне track (одиночный score_domain, юнит-тест) — no-op."""
    from app.db import SessionLocal
    with _DB_LOCK, SessionLocal() as db:
        r = _running(db, name)
        if r is None:
            return
        if done is not None:
            r.done = done
        if total is not None:
            r.total = total
        if current is not None:
            r.current = current[:255]
        if message is not None:
            r.message = message[:400]
        if stage is not None:
            r.stage = stage
            r.stages = _advance(r.stages, stage)
        r.updated_at = _utcnow()
        db.commit()


def cancelled(name: str) -> bool:
    from app.db import SessionLocal
    with _DB_LOCK, SessionLocal() as db:
        r = _running(db, name)
        return bool(r and r.cancel_requested)


def request_cancel(name: str) -> bool:
    """Кнопка «стоп»: помечаем прогон; сервис увидит это между элементами."""
    from app.db import SessionLocal
    with _DB_LOCK, SessionLocal() as db:
        r = _running(db, name)
        if r is None:
            return False
        r.cancel_requested = True
        r.updated_at = _utcnow()
        db.commit()
        return True


def is_running(name: str) -> bool:
    """Путь ЗАХВАТА замка (зовёт spawn) — значит здесь протухшее гасим: иначе убитый контейнер
    запер бы джоб навсегда (is_running -> True -> spawn отказывает -> _open, который умеет реап,
    не вызывается НИКОГДА).

    ДОПОЛНЕНИЕ ПРОТИВ СПЕКИ (эмпирически обнаружено, не было в брифе): `name in _INFLIGHT`
    считаем running тоже. Без этого — окно в несколько миллисекунд между тем, как spawn()
    вернул True вызывающему потоку, и тем, как САМ фоновый поток дошёл до track()->_open()
    и реально вставил строку — is_running() успевала увидеть «строки ещё нет» и отдать False.
    Легаси-тесты (test_jobs.py) поллят ИМЕННО is_running() как сигнал «уже можно читать финал»,
    без начальной паузы: на быстрых моках (boom() падает мгновенно, пустой фид discovery)
    первая же проверка успевала попасть в это окно и ловила пустую/недописанную форму —
    воспроизведено 100% в проходе всего файла (test_error_is_captured, test_zero_candidates_
    discovery_reaches_terminal). Старый in-memory jobs.py такого окна не имел вовсе: start()
    выставлял running=True СИНХРОННО в вызывающем потоке, до _EXEC.submit(). _INFLIGHT
    закрывается ТОЛЬКО в spawn()'s _run().finally — то есть строго ПОСЛЕ track()'s _close(),
    так что «True из-за _INFLIGHT» никогда не переживает реальное завершение."""
    with _INFLIGHT_LOCK:
        if name in _INFLIGHT:
            return True
    from app.db import SessionLocal
    with _DB_LOCK, SessionLocal() as db:
        _reap(db)
        return _running(db, name) is not None


def live() -> list[dict]:
    """Все идущие задачи — Пульту и полосе в шапке. ЧИТАЕТ, не мутирует: реапа здесь нет
    намеренно (см. шапку модуля) — протухшее помечается флагом stale, а не убивается."""
    from sqlalchemy import select
    from app.db import SessionLocal
    from app.models.job import JobRun
    with _DB_LOCK, SessionLocal() as db:
        rows = db.execute(select(JobRun).where(JobRun.status == "running")
                          .order_by(JobRun.started_at)).scalars().all()
        return [_as_dict(r) for r in rows]


def last(name: str) -> dict | None:
    """Итог последнего ЗАВЕРШЁННОГО прогона (Пульт в простое)."""
    from sqlalchemy import select
    from app.db import SessionLocal
    from app.models.job import JobRun
    with _DB_LOCK, SessionLocal() as db:
        r = db.execute(select(JobRun).where(JobRun.name == name, JobRun.status != "running")
                       .order_by(JobRun.id.desc()).limit(1)).scalars().first()
        return _as_dict(r) if r else None


def progress(name: str) -> dict:
    """Текущий прогон, иначе последний завершённый, иначе пустая форма."""
    from app.db import SessionLocal
    with _DB_LOCK:
        with SessionLocal() as db:
            r = _running(db, name)
            if r is not None:
                return _as_dict(r)
        return last(name) or _blank()


def spawn(name: str, target) -> bool:
    """Запустить target() в фоне. False — уже идёт.

    _INFLIGHT — не дубль замка, а закрытие ГОНКИ СОБСТВЕННОГО ПРОЦЕССА. Строку job_run создаёт
    сам сервис (target -> track -> _open) уже В ПОТОКЕ, а `is_running()` смотрит в БД из потока
    вызывающего — между ними окно в несколько миллисекунд, и второй клик по кнопке в него
    пролезает: spawn возвращает True, панель молчит про «уже идёт», а второй прогон умирает
    внутри об AlreadyRunning. Работы он не сделает (индекс держит), но панель СОВРЁТ про запуск —
    ровно та болезнь, которую эта ветка лечит.

    Межпроцессную гонку (панель против воркера) по-прежнему судит индекс, а не этот сет.
    """
    with _INFLIGHT_LOCK:
        if name in _INFLIGHT or is_running(name):
            return False
        _INFLIGHT.add(name)

    def _run():
        try:
            target()
        except AlreadyRunning:
            pass                            # гонку выиграл другой ПРОЦЕСС — молча уходим
        except Exception:                   # noqa: BLE001 — track уже записал failed
            _log.exception("джоб %s упал", name)
        finally:
            with _INFLIGHT_LOCK:
                _INFLIGHT.discard(name)

    fut = _EXEC.submit(_run)
    # ДОПОЛНЕНИЕ ПРОТИВ СПЕКИ (см. шапку модуля): регистрируем Future, чтобы тестовый харнесс
    # мог дождаться РЕАЛЬНОГО завершения потока (_drain), а не только флага в БД.
    with _INFLIGHT_LOCK:
        _FUTURES.append(fut)
    return True


def _drain(timeout: float = 2.0) -> None:    # только для тестового харнесса (conftest.py)
    """Дождаться РЕАЛЬНОГО завершения всех фоновых потоков, отданных spawn().

    ДОПОЛНЕНИЕ ПРОТИВ СПЕКИ (см. шапку модуля): нужно, потому что не каждый тест дожидается
    is_running()==False сам (test_autopilot_run_starts_job поллит побочный эффект внутри
    target(), а не реестр — thread ещё дописывает "done" в job_run, когда тест уже вернулся).
    Тестовый sqlite_db потом рвёт движок из-под пишущего потока: на этой машине SQLite собран
    THREADSAFE=2 («multi-thread»), где такое — задокументированный segfault, не гипотетический.
    Best-effort: таймаут проглатываем — это страховка харнесса, а не проверка корректности."""
    with _INFLIGHT_LOCK:
        futs, _FUTURES[:] = list(_FUTURES), []
    for f in futs:
        try:
            f.result(timeout=timeout)
        except Exception:
            pass


def _reset() -> None:                       # только для тестов
    from sqlalchemy import delete
    from app.db import SessionLocal
    from app.models.job import JobRun
    _drain()                                # добить фоновые потоки перед чисткой _INFLIGHT/БД
    with _INFLIGHT_LOCK:
        _INFLIGHT.clear()                   # иначе имя от прошлого теста блокирует spawn
    with _DB_LOCK, SessionLocal() as db:
        db.execute(delete(JobRun))
        db.commit()
