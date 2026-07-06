"""In-memory реестр прогресса длинных задач (один оператор). Без очереди/персистентности.

start(name, target) гоняет target в фоне (ThreadPoolExecutor на 1 воркер), запрещает
двойной старт одного имени, ловит исключение в error. Панель поллит progress(name).
Джоб живёт в памяти — рестарт контейнера его теряет (допустимо).
"""
import threading
from concurrent.futures import ThreadPoolExecutor

_LOCK = threading.Lock()
_EXEC = ThreadPoolExecutor(max_workers=1)
_STATE: dict[str, dict] = {}


def _blank() -> dict:
    return {"running": False, "done": 0, "total": 0, "current": "", "message": "", "error": None}


def report(name: str, done: int, total: int, current: str = "", message: str = "") -> None:
    with _LOCK:
        s = _STATE.setdefault(name, _blank())
        s.update(done=done, total=total, current=current, message=message)


def is_running(name: str) -> bool:
    with _LOCK:
        return _STATE.get(name, _blank())["running"]


def progress(name: str) -> dict:
    with _LOCK:
        return dict(_STATE.get(name, _blank()))


def start(name: str, target) -> bool:
    """Запустить target() в фоне под именем name. False если уже идёт."""
    with _LOCK:
        if _STATE.get(name, _blank())["running"]:
            return False
        _STATE[name] = {**_blank(), "running": True}

    def _run():
        try:
            target()
        except Exception as e:  # noqa: BLE001 — фиксируем в error, не роняем воркер
            with _LOCK:
                _STATE[name]["error"] = f"{type(e).__name__}: {e}"[:200]
        finally:
            with _LOCK:
                _STATE[name]["running"] = False

    _EXEC.submit(_run)
    return True


def _reset() -> None:                 # только для тестов
    with _LOCK:
        _STATE.clear()
