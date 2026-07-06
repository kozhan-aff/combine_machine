"""M1b — Domain/donor scoring. Implements the funnel in docs/DONORS.md on the FREE stack.

Order: pre-filter -> history (Wayback) -> risk (RKN, blacklist) -> indexed_echo (SearXNG)
-> composite score + breakdown -> status approved | scored(manual) | rejected.
`compute_score` is pure (unit-tested below); `score_domain` does the I/O + DB write.
"""
import math
from app.services import scoring_config as cfg


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


def compute_score(sig: dict) -> dict:
    """Pure: signals -> {score, status, breakdown}. No I/O. See scoring_config for knobs."""
    pf = sig.get("prior_flags") or {}

    # --- hard rejects (Stage E) ---
    reasons = []
    if sig.get("rkn_listed"):
        reasons.append("rkn_listed")
    if sig.get("blacklisted") is True:
        reasons.append("blacklisted")
    if sig.get("trademark_risk"):
        reasons.append("trademark_risk")
    reasons += [f"prior_{c}" for c in cfg.HARD_REJECT_FLAGS if pf.get(c)]
    if pf.get("topic_switch"):
        reasons.append("topic_switch")
    if reasons:
        return {"score": 0.0, "status": "rejected", "breakdown": {"hard_reject": reasons}}

    # --- composite (Stage F) ---
    n = cfg.NORM
    comp = {
        # spam (как и остальная грязная история) уже отсеян hard-reject'ом выше —
        # уцелевший домен чист: полный балл при проверенной истории, половина при непроверенной.
        "history_cleanliness": 1.0 if sig.get("wayback_checked") else 0.5,
        "authority": _clamp((sig.get("dr") or 0.0) / n["DR_FULL"]),
        "age": _clamp((sig.get("age_years") or 0.0) / n["AGE_FULL"]),
        "rd_proxy": _clamp(math.log10((sig.get("referring_domains") or 0) + 1)
                           / math.log10(n["RD_FULL"] + 1)),
        "indexed_echo": 1.0 if sig.get("indexed_echo") else 0.0,
    }
    score = round(_clamp(sum(cfg.WEIGHTS[k] * comp[k] for k in cfg.WEIGHTS)), 4)
    status = ("approved" if score >= cfg.DECISION["approve_at"]
              else "scored" if score >= cfg.DECISION["manual_review_at"]
              else "rejected")
    # core invariant (CLAUDE.md): never AUTO-approve a domain whose history we could not
    # verify — a successful Wayback pass is mandatory. If it failed/absent, downgrade to
    # manual review. (Emergent from the weights today, but pinned so reweighting can't break it.)
    if status == "approved" and not sig.get("wayback_checked"):
        status = "scored"
    # risk-guard: если проверка RKN или blacklist упала (ключ сигнала отсутствует, ошибка
    # осела в errors), нельзя подтверждать чистоту автоматом — уводим в ручной `scored`.
    if status == "approved" and any(
            e.startswith(("rkn:", "blacklist:")) for e in (sig.get("errors") or [])):
        status = "scored"
    return {"score": score, "status": status,
            "breakdown": {"components": comp, "weights": cfg.WEIGHTS}}


def _make_clients() -> dict:
    """Собрать интеграционные клиенты один раз на прогон (переиспользуются между доменами)."""
    from app.config import settings
    from app.integrations.wayback import WaybackClient
    from app.integrations.rkn import RknClient
    from app.integrations.blacklist import BlacklistClient
    from app.integrations.searxng import SearxngClient
    from app.integrations.openpagerank import OpenPageRankClient
    from app.integrations.aparser import AParserClient
    return {
        "wayback": WaybackClient(), "rkn": RknClient(), "blacklist": BlacklistClient(),
        "searxng": SearxngClient(), "aparser": AParserClient(),
        "opr": OpenPageRankClient() if settings.OPENPAGERANK_API_KEY else None,
    }


def _funnel(d, c, st, sig) -> str | None:
    """Ступени дёшево→дорого с ранним выходом. Возвращает reject_reason или None,
    попутно наполняя sig посчитанными сигналами. Дорогой Wayback (T3) — только для выживших."""
    from datetime import datetime, timezone

    # T0 — фид (0 стоимости): сохранённые флаги источника + RD
    if d.feed_flags and any(d.feed_flags.get(k) for k in ("rkn", "judicial", "block")):
        return "feed_flag"
    if d.referring_domains is not None and d.referring_domains < st["min_referring_domains"]:
        return "low_rd"

    # T1 — whois (дёшево): возраст
    age_known = False
    try:
        wc = c["aparser"].whois_created(d.domain)
        sig["whois_created"] = wc
        if wc is not None:
            age_known = True
            age = (datetime.now(timezone.utc) - wc).days / 365.25
            sig["age_years"] = round(age, 2)
            if age < st["min_age_years"]:
                return "too_young"
    except Exception as e:  # noqa: BLE001
        sig["errors"].append(f"whois:{type(e).__name__}")

    # T2 — риск (средне): РКН + Spamhaus + indexed_echo (lookups)
    try:
        sig["rkn_listed"] = c["rkn"].is_listed(d.domain)
        if sig["rkn_listed"]:
            return "rkn"
    except Exception as e:  # noqa: BLE001
        sig["errors"].append(f"rkn:{type(e).__name__}")
    try:
        sig["blacklisted"] = c["blacklist"].is_blacklisted(d.domain)
        if sig["blacklisted"] is True:
            return "blacklist"
    except Exception as e:  # noqa: BLE001
        sig["errors"].append(f"blacklist:{type(e).__name__}")
    try:
        sig["indexed_echo"] = c["searxng"].indexed_echo(d.domain)
    except Exception as e:  # noqa: BLE001
        sig["errors"].append(f"searxng:{type(e).__name__}")

    # T3 — история (дорого): Wayback + DR, только для выживших
    try:
        hist = c["wayback"].classify_history(d.domain)
        pf = hist.get("prior_flags") or {}
        if any(pf.get(k) for k in cfg.HARD_REJECT_FLAGS) or pf.get("topic_switch"):
            sig["prior_flags"] = pf
            return "history_dirty"
        sig["prior_flags"] = pf
        sig["wayback_checked"] = hist.get("wayback_checked")
        sig["first_seen"] = hist.get("first_seen")
        if sig.get("whois_created") is None and hist.get("age_years") is not None:
            sig["age_years"] = hist["age_years"]           # whois приоритетнее; Wayback — фолбэк
    except Exception as e:  # noqa: BLE001
        sig["errors"].append(f"wayback:{type(e).__name__}")

    # whois недоступен (упал/None) -> возраст добираем из Wayback first_seen (см. T3 выше).
    # Консервативно: непроверяемый по whois возраст всё равно должен пройти гейт молодости —
    # если фолбэк-возраст из Wayback < порога, отклоняем здесь (ПОСЛЕ history_dirty, чтобы
    # грязная история репортилась как history_dirty, а не задним числом как too_young).
    if not age_known and sig.get("age_years") is not None and sig["age_years"] < st["min_age_years"]:
        return "too_young"

    if c["opr"] is not None:
        try:
            sig["dr"] = c["opr"].get_page_rank([d.domain]).get(d.domain)
        except Exception as e:  # noqa: BLE001
            sig["errors"].append(f"opr:{type(e).__name__}")
    return None


def score_domain(domain_id: int, clients: dict | None = None) -> dict:
    """Полная воронка для одного домена: ступени -> скор/reject -> запись. Возвращает разбор."""
    from app.db import SessionLocal
    from app.models.domain import Domain
    from app.services.settings import get_settings

    st = get_settings()
    with SessionLocal() as db:
        d = db.get(Domain, domain_id)
        if d is None:
            raise ValueError(f"domain {domain_id} not found")
        c = clients or _make_clients()
        sig: dict = {"errors": []}
        reject = _funnel(d, c, st, sig)

        if reject:
            result = {"score": 0.0, "status": "rejected", "breakdown": {"funnel_reject": reject}}
        else:
            sig.setdefault("referring_domains", d.referring_domains)
            result = compute_score(sig)

        d.whois_created = sig.get("whois_created")
        d.prior_flags = sig.get("prior_flags")
        d.wayback_checked = bool(sig.get("wayback_checked"))
        d.first_seen = sig.get("first_seen")
        d.age_years = sig.get("age_years")
        d.rkn_listed = sig.get("rkn_listed")
        d.blacklisted = sig.get("blacklisted")
        d.indexed_echo = sig.get("indexed_echo")
        if sig.get("dr") is not None:
            d.dr = sig["dr"]
        d.clean = result["status"] != "rejected"
        d.score = result["score"]
        d.score_breakdown = {**result["breakdown"], "errors": sig.get("errors", [])}
        d.status = result["status"]
        d.reject_reason = reject or ("low_score" if result["status"] == "rejected" else None)
        db.commit()
        return {"domain": d.domain, **result, "reject_reason": d.reject_reason,
                "errors": sig.get("errors", [])}


def score_pending(limit: int = 100) -> int:
    """Score all `discovered` domains; return count processed."""
    from sqlalchemy import select
    from app.db import SessionLocal
    from app.models.domain import Domain

    with SessionLocal() as db:
        ids = db.execute(
            select(Domain.id).where(Domain.status == "discovered")
            .order_by(Domain.referring_domains.desc().nulls_last())  # лучшие кандидаты первыми
            .limit(limit)
        ).scalars().all()
    clients = _make_clients()  # один набор клиентов на весь прогон, не на домен
    for did in ids:
        score_domain(did, clients)
    return len(ids)


if __name__ == "__main__":  # pure-function self-check (no I/O)
    # clean old domain -> manual review at least
    clean = compute_score({"wayback_checked": True, "prior_flags": {},
                           "dr": 4.0, "age_years": 10, "referring_domains": 30,
                           "indexed_echo": True})
    assert clean["status"] in ("approved", "scored"), clean
    # casino history -> hard reject
    dirty = compute_score({"wayback_checked": True, "prior_flags": {"casino": True},
                           "dr": 9.0, "age_years": 15, "referring_domains": 500})
    assert dirty["status"] == "rejected" and dirty["score"] == 0.0, dirty
    # RKN -> hard reject regardless of quality
    rkn = compute_score({"rkn_listed": True, "dr": 6, "age_years": 12,
                         "referring_domains": 200, "wayback_checked": True, "prior_flags": {}})
    assert rkn["status"] == "rejected", rkn
    # empty/unknown -> low score, rejected
    empty = compute_score({})
    assert empty["status"] == "rejected", empty
    # INVARIANT: unverified history never auto-approves, even with huge RD
    unverified = compute_score({"referring_domains": 5000, "wayback_checked": False,
                                "prior_flags": {}})
    assert unverified["status"] != "approved", unverified
    # weights sum to 1.0
    assert abs(sum(cfg.WEIGHTS.values()) - 1.0) < 1e-9
    print("scoring compute_score ok:", clean["score"], dirty["score"], rkn["score"], empty["score"])
