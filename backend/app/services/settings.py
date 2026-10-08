"""Рантайм-настройки воронки: читать/писать single-row scoring_settings.

get_settings() возвращает effective-словарь (сидит дефолтами из scoring_config при
отсутствии строки). Пороги валидируются по диапазонам, чтобы UI не записал мусор.
"""
import json
import re

from app.services import scoring_config as cfg

_KEYS_NUM = ("min_referring_domains", "min_age_years", "approve_at", "manual_review_at",
             "max_whois_per_run",
             "min_dr", "max_links_per_run", "max_deep_per_run", "spam_anchor_max", "units_floor")
_BOUNDS = {                       # (min, max) для валидации ползунков
    "min_referring_domains": (0, 100000),
    "min_age_years": (0.0, 30.0),
    "approve_at": (0.0, 1.0),
    "manual_review_at": (0.0, 1.0),
    "max_whois_per_run": (1, 5000),
    "min_dr": (0.0, 100.0),
    "max_links_per_run": (1, 5000),
    "max_deep_per_run": (0, 500),        # 0 = W6 выключен: анкоры не проверены, в пакет домен не попадёт
    "spam_anchor_max": (0.0, 1.0),
    "units_floor": (0, 2_000_000),       # 0 = пола нет; 2 млн — месячный лимит units
}
_LIST_MAX = 200


def _defaults() -> dict:
    return {
        "min_referring_domains": cfg.PREFILTER["min_referring_domains"],
        "min_age_years": cfg.MIN_AGE_YEARS,
        "approve_at": cfg.DECISION["approve_at"],
        "manual_review_at": cfg.DECISION["manual_review_at"],
        "max_whois_per_run": cfg.MAX_WHOIS_PER_RUN,
        "sources_enabled": dict(cfg.SOURCES_ENABLED),
        "weights": dict(cfg.WEIGHTS),
        "min_dr": cfg.MIN_DR,
        "tld_allowlist": list(cfg.TLD_ALLOWLIST),
        "brand_tokens": list(cfg.BRAND_TOKENS),
        "emd_sets": [],
        "max_links_per_run": cfg.MAX_LINKS_PER_RUN,
        "max_deep_per_run": cfg.MAX_DEEP_PER_RUN,
        "spam_anchor_max": cfg.SPAM_ANCHOR_MAX,
        "units_floor": cfg.UNITS_FLOOR,
        "discovery_opts": {},
    }


def _clean_weights(raw, base: dict | None = None) -> dict:
    """Веса с UI -> валидный словарь. Ключи — только известные компоненты (чужие игнорим:
    неизвестный ключ не с чем перемножать, compute_score упал бы на KeyError).

    `base` — на что опираться для НЕ переданных ключей. Из UI приходят все семь, но частичный
    POST (API, скрипт) не должен молча ронять остальные веса к дефолтам: база — то, что сейчас
    записано, а не то, что зашито в коде.

    Вырожденный набор (всё по нулю / мусор) НЕ записываем: он обнулил бы score всем доменам
    разом и тихо превратил бы воронку в «всё отклонено». В таком случае — дефолты."""
    b = {**cfg.WEIGHTS, **(base or {})}
    if not isinstance(raw, dict):
        return {k: b[k] for k in cfg.WEIGHTS}
    out = {}
    for k in cfg.WEIGHTS:                       # порядок и состав ключей задаёт код, не форма
        try:
            out[k] = max(0.0, min(1.0, float(raw.get(k, b[k]))))
        except (TypeError, ValueError):
            out[k] = b[k]
    return out if sum(out.values()) > 0 else dict(cfg.WEIGHTS)


def _clean_list(raw) -> list[str]:
    """Список с UI (textarea: строки/запятые/пробелы) или готовый список -> нижний регистр, без
    точек по краям, без пустых и дублей. Порядок оператора сохраняется.

    Берём только СТРОКИ: `str(None)` дал бы зону «none», `str(True)` — «true», и обе молча ушли бы
    в белый список/генератор имён. Не-строка внутри списка отбрасывается; скаляр не-строка вместо
    списка (`true`, `5`) — ValueError, а не TypeError на итерации (панель показывает ValueError)."""
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = re.split(r"[\s,;]+", raw)
    elif not isinstance(raw, (list, tuple)):
        raise ValueError(f"ожидается список или строка, получено {type(raw).__name__}")
    out = []
    for x in raw:
        if not isinstance(x, str):
            continue
        t = x.strip().strip(".").lower()
        if t and t not in out:
            out.append(t)
    return out[:_LIST_MAX]


def _clean_emd_sets(raw) -> list[dict]:
    """Наборы EMD (JSON-текст с UI или список) -> валидный список. Невалидный JSON — ValueError:
    панель покажет ошибку, а сохранённые наборы НЕ затрутся пустотой.

    Строка вместо списка в `keywords` — это ОДИН ключ: перебор строки дал бы буквы
    (`"mejor vpn"` -> m.com, e.com, j.com…). `tlds` строкой разбирает `_clean_list`, как textarea.

    Ключ с точкой — ValueError, а не тихая чистка: `best.vpn` + зона `com` дал бы имя `best.vpn.com`,
    а такого РЕГИСТРИРУЕМОГО имени не бывает (это поддомен) — генератор EMD выдавал бы мусор, который
    не купить. Оператор должен увидеть опечатку и поправить её, а не гадать, куда делся ключ."""
    if isinstance(raw, str):
        raw = json.loads(raw) if raw.strip() else []       # JSONDecodeError — подкласс ValueError
    if not isinstance(raw, list):
        raise ValueError("наборы EMD: ожидается JSON-список")
    out = []
    for s in raw[:50]:
        if not isinstance(s, dict):
            continue
        kw_raw = s.get("keywords")
        if kw_raw is None:
            kw_raw = []
        elif isinstance(kw_raw, str):
            kw_raw = [kw_raw]
        elif not isinstance(kw_raw, (list, tuple)):
            raise ValueError(f"наборы EMD: keywords — список или строка, получено {type(kw_raw).__name__}")
        kws = [k.strip()[:60] for k in kw_raw if isinstance(k, str) and k.strip()][:50]
        if any("." in k for k in kws):
            raise ValueError("наборы EMD: ключ не может содержать точку — получилось бы имя "
                             "вида best.vpn.com, а не регистрируемый домен")
        tlds = _clean_list(s.get("tlds"))[:20]
        if kws and tlds:
            out.append({"market": str(s.get("market") or "")[:16],
                        "lang": str(s.get("lang") or "")[:8].lower(),
                        "keywords": kws, "tlds": tlds})
    return out


def _discovery_view(opts) -> dict:
    """discovery_opts (JSONB, частично пустой) -> эффективные max_candidates_per_run и name_filters."""
    o = opts or {}
    try:
        cap = int(o.get("max_candidates_per_run", cfg.MAX_CANDIDATES_PER_RUN))
    except (TypeError, ValueError):
        cap = cfg.MAX_CANDIDATES_PER_RUN
    return {"max_candidates_per_run": max(0, min(cap, 50_000)),
            "name_filters": _clean_name_filters(o.get("name_filters")),
            "zone_channels": _clean_zone_channels(o.get("zone_channels", cfg.ZONE_CHANNELS))}


def _clean_zone_channels(raw) -> dict:
    """Таблица зона -> канал выкупа (M2). Неизвестный канал — ValueError (опечатка в деньгах не должна
    молча уводить заказ не туда); пустые ключи и не-строки отбрасываем."""
    if not isinstance(raw, dict):
        raise ValueError("зона -> канал: ожидается словарь")
    out = {}
    for z, ch in raw.items():
        zone = str(z).strip().strip(".").lower()
        if not zone:
            continue
        if ch not in cfg.ACQ_CHANNELS:
            raise ValueError(f"зона .{zone}: неизвестный канал {ch!r} (допустимы {cfg.ACQ_CHANNELS})")
        out[zone] = ch
    return out


def _clean_name_filters(raw) -> dict:
    """Фильтры имени с UI/API -> валидный словарь поверх дефолтов; мусор в числах -> дефолт."""
    from app.services.domain_filters import DEFAULT_NAME_FILTERS
    out = dict(DEFAULT_NAME_FILTERS)
    out["junk"] = list(DEFAULT_NAME_FILTERS["junk"])
    if not isinstance(raw, dict):
        return out
    for k, cast, lo, hi in (("max_label_len", int, 0, 63), ("max_digit_share", float, 0.0, 1.0),
                            ("max_hyphens", int, -1, 10)):
        try:
            out[k] = max(lo, min(hi, cast(raw[k]))) if k in raw else out[k]
        except (TypeError, ValueError):
            pass
    if "junk" in raw:
        out["junk"] = _clean_list(raw["junk"])
    return out


def get_source_state() -> dict:
    """Валидаторы условного GET источников discovery ({источник: {etag, last_modified}}), S1-11."""
    from app.db import SessionLocal
    with SessionLocal() as db:
        return dict((_row(db).discovery_opts or {}).get("source_state") or {})


def set_source_state(state: dict) -> None:
    from app.db import SessionLocal
    with SessionLocal() as db:
        r = _row(db)
        r.discovery_opts = {**(r.discovery_opts or {}), "source_state": state}
        db.commit()


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
            # устаревшие ключи в БД (backorder/cctld/…) не выключают молча новые источники v2
            "sources_enabled": {k: bool((r.sources_enabled or {}).get(k, v))
                                for k, v in cfg.SOURCES_ENABLED.items()},
            # пусто (миграция 0009 засеяла {}) -> дефолты из кода, а не нулевая шкала
            "weights": _clean_weights(r.weights or cfg.WEIGHTS),
            "min_dr": float(r.min_dr),
            "tld_allowlist": list(r.tld_allowlist or cfg.TLD_ALLOWLIST),   # пусто -> дефолт: пустой список убил бы всё
            "brand_tokens": list(r.brand_tokens or cfg.BRAND_TOKENS),
            "emd_sets": list(r.emd_sets or []),
            "max_links_per_run": int(r.max_links_per_run),
            "max_deep_per_run": int(r.max_deep_per_run),
            "spam_anchor_max": float(r.spam_anchor_max),
            "units_floor": int(r.units_floor),
            **_discovery_view(r.discovery_opts),
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
        for k in ("tld_allowlist", "brand_tokens"):
            if kw.get(k) is not None:
                setattr(r, k, _clean_list(kw[k]))
        if (kw.get("max_candidates_per_run") is not None or kw.get("name_filters") is not None
                or kw.get("zone_channels") is not None):
            cur = dict(r.discovery_opts or {})
            if kw.get("zone_channels") is not None:
                cur["zone_channels"] = _clean_zone_channels(kw["zone_channels"])   # ValueError до commit
            if kw.get("max_candidates_per_run") is not None:
                cur["max_candidates_per_run"] = max(0, min(int(kw["max_candidates_per_run"]), 50_000))
            if kw.get("name_filters") is not None:
                cur["name_filters"] = _clean_name_filters(kw["name_filters"])
            r.discovery_opts = cur
        if kw.get("emd_sets") is not None:
            r.emd_sets = _clean_emd_sets(kw["emd_sets"])   # ValueError -> выходим ДО commit
        if "sources_enabled" in kw and isinstance(kw["sources_enabled"], dict):
            r.sources_enabled = {s: bool(kw["sources_enabled"].get(s, False))
                                 for s in cfg.SOURCES_ENABLED}
        if "weights" in kw and kw["weights"] is not None:
            r.weights = _clean_weights(kw["weights"], base=dict(r.weights or {}))
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
