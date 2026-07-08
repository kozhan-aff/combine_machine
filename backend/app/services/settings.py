"""Рантайм-настройки воронки: читать/писать single-row scoring_settings.

get_settings() возвращает effective-словарь (сидит дефолтами из scoring_config при
отсутствии строки). Пороги валидируются по диапазонам, чтобы UI не записал мусор.
"""
from app.services import scoring_config as cfg

_KEYS_NUM = ("min_referring_domains", "min_age_years", "approve_at", "manual_review_at", "max_whois_per_run")
_BOUNDS = {                       # (min, max) для валидации ползунков
    "min_referring_domains": (0, 100000),
    "min_age_years": (0.0, 30.0),
    "approve_at": (0.0, 1.0),
    "manual_review_at": (0.0, 1.0),
    "max_whois_per_run": (1, 5000),
}


def _defaults() -> dict:
    return {
        "min_referring_domains": cfg.PREFILTER["min_referring_domains"],
        "min_age_years": cfg.MIN_AGE_YEARS,
        "approve_at": cfg.DECISION["approve_at"],
        "manual_review_at": cfg.DECISION["manual_review_at"],
        "max_whois_per_run": cfg.MAX_WHOIS_PER_RUN,
        "sources_enabled": dict(cfg.SOURCES_ENABLED),
    }


def _row(db):
    """Вернуть (создав при отсутствии) строку scoring_settings id=1, засеянную дефолтами."""
    from app.models.settings import ScoringSettings
    row = db.get(ScoringSettings, 1)
    if row is None:
        d = _defaults()
        row = ScoringSettings(id=1, **d)
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


def get_settings() -> dict:
    from app.db import SessionLocal
    with SessionLocal() as db:
        r = _row(db)
        return {
            "min_referring_domains": int(r.min_referring_domains),
            "min_age_years": float(r.min_age_years),
            "approve_at": float(r.approve_at),
            "manual_review_at": float(r.manual_review_at),
            "max_whois_per_run": int(r.max_whois_per_run),
            "sources_enabled": dict(r.sources_enabled or cfg.SOURCES_ENABLED),
        }


def update_settings(**kw) -> dict:
    """Записать переданные ключи с валидацией диапазонов. Неизвестные ключи игнор."""
    from app.db import SessionLocal
    with SessionLocal() as db:
        r = _row(db)
        for k in _KEYS_NUM:
            if k in kw and kw[k] is not None:
                lo, hi = _BOUNDS[k]
                v = max(lo, min(hi, type(lo)(kw[k])))
                setattr(r, k, v)
        if "sources_enabled" in kw and isinstance(kw["sources_enabled"], dict):
            r.sources_enabled = {s: bool(kw["sources_enabled"].get(s, False))
                                 for s in cfg.SOURCES_ENABLED}
        if r.max_whois_per_run < 1:
            r.max_whois_per_run = 1                 # 0 глушил бы скоринг целиком
        if r.approve_at < r.manual_review_at:
            r.approve_at = r.manual_review_at       # инверсия порогов -> approve не ниже manual
        db.commit()
    return get_settings()


def reset_settings() -> dict:
    from app.db import SessionLocal
    with SessionLocal() as db:
        r = _row(db)
        for k, v in _defaults().items():
            setattr(r, k, v)
        db.commit()
    return get_settings()
