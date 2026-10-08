"""Кэш диагностики внешних инструментов + алерт для глобального баннера панели.

run_diagnostics() медленный (Wayback ~15с) — не гоняем на каждый запрос. Фоновая
задача в main.py дёргает refresh() раз в REFRESH_SEC; Jinja-global diag_alert() читает
alert() из кэша мгновенно. Роут-рефреш и фоновая задача пишут из разных потоков — под _LOCK.
"""
import threading
from datetime import datetime, timezone

from app.services import diagnostics
from app.services.diagnostics import run_diagnostics

REFRESH_SEC = 300  # тот же ритм, что тик автопилота

_NON_EXTERNAL = {"db"}  # PostgreSQL живёт в docker-compose комбайна; всё остальное — внешнее

_LOCK = threading.Lock()
_REFRESH_LOCK = threading.Lock()   # single-flight: один живой прогон за раз
_checks: list[dict] | None = None
_checked_at: datetime | None = None


def refresh(force: bool = False) -> list[dict]:
    """Прогоняет run_diagnostics(), кладёт результат+время в кэш, возвращает checks.

    Single-flight (F8-08): GET /diag, кнопка и фоновый цикл раньше запускали каждый свой
    прогон на 16 потоков — и каждый бил aaPanel/Wayback/LLM. Если прогон уже идёт, второй не
    стартует: ждём первый и отдаём его результат. force — явная «проверить снова»: заодно
    сбрасывает TTL-кэш дорогих проб (LLM/SearXNG), иначе кнопка вернула бы 10-минутной давности."""
    global _checks, _checked_at
    if not _REFRESH_LOCK.acquire(blocking=False):
        with _REFRESH_LOCK:      # дождаться идущего прогона
            pass
        with _LOCK:
            return list(_checks or [])
    try:
        if force:
            diagnostics.reset_probe_cache()
        checks = run_diagnostics()
        now = datetime.now(timezone.utc)
        with _LOCK:
            _checks = checks
            _checked_at = now
        return checks
    finally:
        _REFRESH_LOCK.release()


def get() -> tuple[list[dict], datetime | None]:
    """Для GET /diag: кэш МГНОВЕННО (S7-13/F8-08/S1-12). Живой прогон — только на холодном старте
    (кэша ещё нет), дальше его ведёт фоновый цикл; кэш старше 2×REFRESH_SEC дополнительно
    обновляется в фоне, страница при этом не ждёт."""
    with _LOCK:
        checks, at = _checks, _checked_at
    if checks is None:
        checks = refresh()
        with _LOCK:
            at = _checked_at
    elif at is not None and (datetime.now(timezone.utc) - at).total_seconds() > 2 * REFRESH_SEC:
        threading.Thread(target=refresh, daemon=True, name="diag-refresh").start()
    return checks, at


def value(key: str):
    """Число из последней проверки `key` (остаток units Ahrefs) или None, пока кэша нет. Без сети:
    экран /settings не ждёт внешний сервис на рендере (находка 3.4)."""
    with _LOCK:
        for c in _checks or ():
            if c["key"] == key:
                return c.get("value")
    return None


def alert() -> dict | None:
    """None, пока кэша нет (до первой проверки). Иначе dict для баннера; down может быть
    пуст (всё поднялось) — тогда баннер не рендерится."""
    with _LOCK:
        if _checks is None:
            return None
        # Только КРИТИЧНЫЕ (R2-18): лежащий некритичный источник (Wayback, Nominet, registry.mx, DropCatch,
        # Spamhaus, Cloudflare/aaPanel до подпроекта 2, Ahrefs с нулевым остатком units до месячного
        # сброса) — строка на /diag, а не баннер на всех
        # экранах: иначе он горел бы неделями, и его перестали бы читать.
        down = [c for c in _checks
                if c["key"] not in _NON_EXTERNAL and c.get("critical") and c["status"] == "fail"]
        return {
            "down": [c["label"] for c in down],           # лейблы в порядке _spec()
            "sig": ",".join(sorted(c["key"] for c in down)),
            "checked_at": _checked_at,
        }
