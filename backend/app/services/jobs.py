"""Реестр длинных задач — строка в БД на прогон (кросс-процессный).

Раньше это был dict в памяти процесса backend: свип автопилота крутится в процессе worker,
поэтому панель его не видела вовсе — «машина работает» было непроверяемо на глаз. Теперь
и панель, и воркер пишут в одну таблицу job_run.

КОНТРАКТ (задача адресуется ПО ИМЕНИ снаружи и ПО run_id изнутри — см. «Лизинг» ниже):
  track(name, trigger=, stages=)  контекст-менеджер вокруг работы; отдаёт run_id; сам закрывает
                                  строку (done / failed / cancelled). Занято -> AlreadyRunning.
  spawn(name, target) -> bool     запустить target() в фоне (панель); False если уже идёт.
  report(run_id, ...)             обновить прогресс/стадию СВОЕГО прогона; run_id=None — no-op.
  cancelled(run_id) -> bool       сервис спрашивает между элементами; True -> поднять Cancelled.
  request_cancel(name)            кнопка «стоп» (оператор знает имя, не id).
  is_running(name) / live() / last(name)   замок занят? / что идёт / итог последнего прогона.

ТЕРМИНАЛЬНЫЙ КОНТРАКТ (JS-компонент держится его же):
  status == "running"    -> идёт; done/total/current/stages — прогресс;
  status == "failed"     -> упал; error — текст, done — где встал, stage — на какой стадии;
  status == "cancelled"  -> остановлен человеком на done/total;
  status == "done"       -> успех, ДАЖЕ если done == total == 0 (discovery без кандидатов).
  done/total — только отображение, не признак терминала.

ЛИЗИНГ ЗАМКА (аудит F17). Замок — частичный уникальный индекс (name) WHERE status='running'.
Держится он не фактом строки, а СЕРДЦЕБИЕНИЕМ: `track` поднимает демон-тред, который раз в
HEARTBEAT_SEC трогает `updated_at` СВОЕЙ строки — независимо от того, репортит ли сервис прогресс.
Строку, молчащую дольше STALE_MIN, считаем трупом (контейнер убили) и гасим.

  ПОЧЕМУ ТРЕД, А НЕ «пусть сервис сам тикает». Приложение синхронное, а молчание стадий — это
  ОДИН блокирующий вызов: `generate` сидит внутри llm.complete() минутами, `score` — внутри
  whois-запроса A-Parser. Между вызовами тикать негде: тикать надо ВО ВРЕМЯ вызова. Блокирующий
  сокет отпускает GIL, поэтому тред тикает и сквозь LLM. Цена — по демон-треду на идущий джоб
  (их максимум 4) и один UPDATE в минуту.

  ПОЧЕМУ updated_at, А НЕ НОВАЯ КОЛОНКА lease_until. Колонка уже значит ровно это — «когда о
  задаче в последний раз было слышно». У неё просто не было говорящего: `onupdate` нет, а
  report() зовётся только на границах стадий. Отдельный lease_until был бы вторым именем той же
  величины (и второй миграцией).

  ЧИСЛА. HEARTBEAT_SEC=60, STALE_MIN=5 — то есть труп объявляется после ПЯТИ пропущенных ударов.
  Раньше стояло 15 минут БЕЗ сердцебиения, и это был баг в обе стороны: порог короче живой
  стадии (score с капом 200 доменов × ~60 с/домен ≈ 200 минут молчания — CLAUDE.md) убивал
  ЖИВОГО, а честный «безсердцебиенный» порог пришлось бы задрать за 3.5 часа — и тогда упавший
  контейнер запирал бы воронку на полсмены. С сердцебиением молчание перестало быть уликой
  работы: живой тикает всегда, поэтому порог можно держать коротким.

ГДЕ ГАСИМ ПРОТУХШЕЕ (_reap): ТОЛЬКО на пути захвата замка — _open() и is_running(). НИКОГДА
на пути чтения (live/progress/last): панель поллит live() раз в 1.5с, и реап на чтении сам бы
гасил задачу, которая законно молчит. Чтение только считает флаг stale: «оборвалась» — это
показ, а не мутация. Гасим ровно тогда, когда замок кому-то реально понадобился, — и теперь это
безопасно: живая задача к тому моменту уже подала признак жизни.

ФЕНСИНГ ЗОМБИ. Если замок у нас всё-таки отобрали (сердцебиение не дошло — БД лежала), процесс
не имеет права ни писать в чужой прогон, ни продолжать работу:
  · report/_close адресуются run_id и обновляют строку УСЛОВНО (WHERE id AND status='running') —
    раньше report искал строку ПО ИМЕНИ и зомби дописывал прогресс в живой прогон ПРЕЕМНИКА;
  · cancelled(run_id) отвечает True, если строка больше не наша, — сервис между элементами
    поднимает Cancelled и уходит. Замок потерян -> работу прекрати.
"""
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

HEARTBEAT_SEC = 60                   # как часто живой прогон трогает свой updated_at
STALE_MIN = 5                        # молчит дольше -> труп. 5 пропущенных ударов (см. шапку)
# по потоку на КАЖДОЕ имя джоба (discovery|score|recheck|sweep|cf_sync). Меньше — и третий
# одновременный запуск молча ляжет в очередь пула: строки в реестре ещё нет, панель ничего не
# рисует, кнопка выглядит сломанной. Один оператор, пять кнопок — пять потоков.
_EXEC = ThreadPoolExecutor(max_workers=5)
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
    # отдаём ISO-строкой, не сырым datetime. Изначально обнаружено на ныне снесённом роуте
    # `GET /run/{job}/progress` (Task 3 его удалил) — `JSONResponse(...)` без jsonable_encoder
    # на сыром datetime даёт 100% `TypeError: Object of type datetime is not JSON serializable`.
    # Актуальный держатель этого решения СЕГОДНЯ — `dashboard.html` (last_runs): там `r.finished_at`
    # рендерится сервер-сайд через Jinja срезом строки (`r.finished_at[8:10] + '.' + ...`), в обход
    # jsonable_encoder — вернуть сюда сырой datetime сломает именно этот срез с TypeError.
    # `/api/jobs/live` (panel.py) от формата не зависит — там jsonable_encoder есть и сам бы
    # сериализовал datetime, не в нём подвох. ISO-строка — тот же формат, в котором это всё равно
    # уйдёт по HTTP, так что менять её здесь не с чем.
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


def _own(run_id: int | None, **values) -> bool:
    """Условно обновить СВОЮ строку: WHERE id=run_id AND status='running'.

    Условие — не украшение. Если замок у нас отобрали (реап решил, что мы труп), строка уже
    не 'running', и мы обязаны промахнуться, а не переписать чужую работу. Слепая ORM-запись
    по PK («прочитал -> решил -> записал») ровно этим и опасна — см. `_settle` в
    services/acquisition.py, там тот же приём и та же причина. Возвращает True, если строка
    ещё наша (rowcount).
    """
    from sqlalchemy import update
    from app.db import SessionLocal
    from app.models.job import JobRun
    if run_id is None:                      # вне track (одиночный score_domain, юнит-тест)
        return False
    with _DB_LOCK, SessionLocal() as db:
        res = db.execute(update(JobRun)
                         .where(JobRun.id == run_id, JobRun.status == "running")
                         .values(updated_at=_utcnow(), **values))
        db.commit()
        return res.rowcount > 0


def _heartbeat(run_id: int) -> tuple[threading.Thread, threading.Event]:
    """Демон-тред: пока идёт работа — раз в HEARTBEAT_SEC трогать updated_at (см. шапку).

    Ошибки БД глотаем и продолжаем: сердцебиение — страховка замка, а не работа. Если БД
    лежит дольше STALE_MIN, замок у нас честно отберут, и об этом узнает cancelled() (фенсинг).
    """
    stop = threading.Event()

    def _beat():
        while not stop.wait(HEARTBEAT_SEC):
            try:
                if not _own(run_id):        # строка больше не наша — стучать некуда
                    return
            except Exception:               # noqa: BLE001
                _log.exception("сердцебиение прогона #%s не дошло", run_id)

    t = threading.Thread(target=_beat, name=f"jobs-heartbeat-{run_id}", daemon=True)
    t.start()
    return t, stop


def _close(run_id: int, status: str, error: str | None = None) -> None:
    """Закрыть СВОЙ прогон — условно (см. _own): строку, которую у нас уже отобрал реап,
    зомби не воскрешает и не перекрашивает в 'done'. Оператор видит «оборвалась» — так и было."""
    from sqlalchemy import select
    from app.db import SessionLocal
    from app.models.job import JobRun
    values: dict = {"status": status, "error": error, "finished_at": _utcnow()}
    if status == "done":                    # успех — все чипы гасим в «пройдено»
        with _DB_LOCK, SessionLocal() as db:
            stages = db.execute(select(JobRun.stages).where(JobRun.id == run_id)).scalar()
        values["stages"] = [s if s.get("state") == "skip" else {**s, "state": "done"}
                            for s in (stages or [])]
    _own(run_id, **values)


@contextmanager
def track(name: str, *, trigger: str = "manual", stages: list | None = None):
    """Обернуть работу записью реестра. Кто бы ни позвал сервис — панель, оркестратор,
    cron воркера — прогресс появляется бесплатно. Отдаёт run_id: им сервис адресует свои
    report()/cancelled() (по ИМЕНИ адресоваться нельзя — попадёшь в чужой прогон, см. шапку).

    На время работы держит сердцебиение — иначе молчащая стадия выглядит трупом, и первый же
    желающий занять замок её убьёт."""
    run_id = _open(name, trigger, stages)
    if run_id is None:
        raise AlreadyRunning(name)
    beat, stop = _heartbeat(run_id)
    try:
        yield run_id
    except Cancelled:
        _close(run_id, "cancelled")         # остановлен человеком — это не ошибка
    except BaseException as e:              # BaseException: ловушки сети в тестах — тоже финал
        _close(run_id, "failed", f"{type(e).__name__}: {e}"[:200])
        raise
    else:
        _close(run_id, "done")
    finally:
        # ГАСИМ ТРЕД ДО ВЫХОДА, а не бросаем демоном: иначе тестовый харнесс снесёт SQLite-движок
        # из-под живого писателя (тот же класс аварии, что лечит _drain, см. его докстринг).
        stop.set()
        beat.join(timeout=5)


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


def report(run_id: int | None, done: int | None = None, total: int | None = None,
           current: str | None = None, stage: str | None = None,
           message: str | None = None) -> None:
    """Обновить СВОЙ прогон (run_id из track). Вне track (одиночный score_domain, юнит-тест)
    run_id=None — no-op.

    АДРЕСАЦИЯ ПО id, А НЕ ПО ИМЕНИ (аудит F17). Раньше сюда шло имя джоба, и строка искалась
    как «running с таким именем» — то есть зомби (процесс, у которого замок уже отобрали)
    находил не свою мёртвую строку, а ЖИВОЙ прогон преемника и дописывал прогресс в него:
    панель показывала чужие домены и чужой счётчик как свои. Промах по id безвреден и молчалив,
    попадание в чужой прогон — нет.
    """
    if run_id is None:                       # вне track (одиночный score_domain, юнит-тест) — no-op
        return                               # раньше это глушил только _own в конце, но стадийная
        #                                      ветка ниже успевала открыть сессию и сделать
        #                                      SELECT ... WHERE id IS NULL — 6 холостых раундтрипов
        #                                      под _DB_LOCK на КАЖДЫЙ ручной score одного домена.
    from sqlalchemy import select
    from app.db import SessionLocal
    from app.models.job import JobRun
    values: dict = {}
    if done is not None:
        values["done"] = done
    if total is not None:
        values["total"] = total
    if current is not None:
        values["current"] = current[:255]
    if message is not None:
        values["message"] = message[:400]
    if stage is not None:
        values["stage"] = stage
        with _DB_LOCK, SessionLocal() as db:
            known = db.execute(select(JobRun.stages).where(JobRun.id == run_id)).scalar()
        values["stages"] = _advance(known, stage)
    _own(run_id, **values)


def cancelled(run_id: int | None) -> bool:
    """«Мне пора остановиться?» — сервис спрашивает между элементами.

    True в ДВУХ случаях, и второй — не про кнопку:
      · человек нажал «стоп» (cancel_requested);
      · строка больше не наша (реап решил, что мы труп, и отдал замок другому). Продолжать
        работу без замка — значит делать её ВТОРЫМ: два свипа в двух процессах = дубли страниц
        и двойной счёт LLM. Замок потерян -> уходим, а прогресс уже пишет преемник.
    Вне track (run_id=None) отменять нечего.
    """
    from sqlalchemy import select
    from app.db import SessionLocal
    from app.models.job import JobRun
    if run_id is None:
        return False
    with _DB_LOCK, SessionLocal() as db:
        r = db.execute(select(JobRun).where(JobRun.id == run_id,
                                            JobRun.status == "running")).scalars().first()
        return r is None or bool(r.cancel_requested)


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
