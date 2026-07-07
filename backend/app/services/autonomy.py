"""Рантайм-конфиг автономии: читать/писать single-row autonomy_settings (id=1).

Паттерн 1-в-1 с services/settings.py. Дефолты — константы здесь (отдельный config-модуль
не нужен, YAGNI). update_autonomy валидирует диапазоны, чтобы UI не записал мусор.
"""
_BOOL_KEYS = ("autopilot_on", "auto_discovery", "auto_score", "auto_queue",
              "auto_provision", "auto_generate", "auto_publish", "auto_check_index")
_INT_BOUNDS = {                       # (min, max) для клампа
    "sweep_interval_min": (5, 1440),
    "cap_score": (0, 500), "cap_queue": (0, 500), "cap_provision": (0, 500),
    "cap_generate": (0, 500), "cap_publish": (0, 500), "cap_check_index": (0, 500),
}
_DEFAULTS = {
    "autopilot_on": False, "sweep_interval_min": 60,
    "auto_discovery": False, "auto_score": False, "auto_queue": False,
    "auto_provision": False, "auto_generate": False, "auto_publish": False,
    "auto_check_index": False,
    "cap_score": 20, "cap_queue": 10, "cap_provision": 5,
    "cap_generate": 5, "cap_publish": 5, "cap_check_index": 20,
}


def _row(db):
    """Вернуть (создав при отсутствии) строку autonomy_settings id=1 с дефолтами."""
    from app.models.autonomy import AutonomySettings
    row = db.get(AutonomySettings, 1)
    if row is None:
        row = AutonomySettings(id=1, **_DEFAULTS)
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


def get_autonomy() -> dict:
    from app.db import SessionLocal
    with SessionLocal() as db:
        r = _row(db)
        out = {k: bool(getattr(r, k)) for k in _BOOL_KEYS}
        out["sweep_interval_min"] = int(r.sweep_interval_min)
        for k in ("cap_score", "cap_queue", "cap_provision",
                  "cap_generate", "cap_publish", "cap_check_index"):
            out[k] = int(getattr(r, k))
        return out


def update_autonomy(**kw) -> dict:
    """Записать переданные ключи: bool через bool(), int с клампом. Неизвестные игнор."""
    from app.db import SessionLocal
    with SessionLocal() as db:
        r = _row(db)
        for k in _BOOL_KEYS:
            if k in kw:
                setattr(r, k, bool(kw[k]))
        for k, (lo, hi) in _INT_BOUNDS.items():
            if k in kw and kw[k] is not None:
                setattr(r, k, max(lo, min(hi, int(kw[k]))))
        db.commit()
    return get_autonomy()


def reset_autonomy() -> dict:
    from app.db import SessionLocal
    with SessionLocal() as db:
        r = _row(db)
        for k, v in _DEFAULTS.items():
            setattr(r, k, v)
        db.commit()
    return get_autonomy()
