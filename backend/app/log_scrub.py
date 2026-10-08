"""Маска секретов в query-строке для логов и текстов ошибок.

NameSilo принимает ключ ТОЛЬКО в query (`?key=…`, код 120 иначе), а httpx на INFO пишет ПОЛНЫЙ URL.
Уровень WARNING (main.py) — первая линия защиты, этот фильтр — вторая: если кто-то подымет уровень
логов до INFO/DEBUG, ключ в строку журнала всё равно не попадёт."""
import logging
import re

_KEY = re.compile(r"((?:[?&]|\b)(?:key|api_key|apikey)=)[^&\s'\"<>]+", re.I)


def mask_key_param(text) -> str:
    return _KEY.sub(r"\1***", str(text))


class KeyMaskFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = mask_key_param(record.getMessage())
            record.args = ()
        except Exception:  # noqa: BLE001 — фильтр логов не должен ронять приложение
            pass
        return True


_NAMES = ("httpx", "httpcore", "httpcore.http11", "httpcore.connection", "hpack")


def install() -> None:
    """Идемпотентно навесить фильтр на логгеры HTTP-стека."""
    for n in _NAMES:
        lg = logging.getLogger(n)
        if not any(isinstance(f, KeyMaskFilter) for f in lg.filters):
            lg.addFilter(KeyMaskFilter())
