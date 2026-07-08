"""M1b — Domain/donor scoring. Implements the funnel in docs/DONORS.md on the FREE stack.

Order: pre-filter -> history (Wayback) -> risk (RKN, blacklist) -> indexed_echo (SearXNG)
-> composite score + breakdown -> status approved | scored(manual) | rejected.
`compute_score` is pure (unit-tested below); `score_domain` does the I/O + DB write.
"""
import logging
import math
from app.services import scoring_config as cfg


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


def _decide(score: float, sig: dict, approve_at: float, manual_review_at: float) -> str:
    """Pure: score threshold -> status, plus the two invariant downgrade guards below.
    Factored out (2026-07 review, Finding 1) so BOTH `compute_score` (static cfg.DECISION)
    and `score_domain` (runtime /settings thresholds) decide through the same logic — the
    live sliders used to only move preview counters, never the actual stored status."""
    status = ("approved" if score >= approve_at
              else "scored" if score >= manual_review_at
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
    return status


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
    status = _decide(score, sig, cfg.DECISION["approve_at"], cfg.DECISION["manual_review_at"])
    return {"score": score, "status": status,
            "breakdown": {"components": comp, "weights": cfg.WEIGHTS}}


def _make_clients() -> dict:
    """Собрать интеграционные клиенты один раз на прогон (переиспользуются между доменами)."""
    from app.integrations.wayback import WaybackClient
    from app.integrations.rkn import RknClient
    from app.integrations.blacklist import BlacklistClient
    from app.integrations.searxng import SearxngClient
    from app.integrations.aparser import AParserClient
    return {
        "wayback": WaybackClient(), "rkn": RknClient(), "blacklist": BlacklistClient(),
        "searxng": SearxngClient(), "aparser": AParserClient(),
    }


def _funnel(d, c, st, sig, whois_budget=None, ahrefs_budget=None) -> str | None:
    """Ступени дёшево→дорого с ранним выходом. Возвращает reject_reason или None,
    наполняя sig. Приобретаемость — гейт на T1: whois решает free/занят для сырых
    источников (backorder объявляет lane=bid сам). Дорогой Wayback (T3) — только для
    приобретаемых выживших. sig['acquirability_unresolved']=True → оставить domain discovered."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)

    # T0 — фид (0 стоимости)
    if d.feed_flags and any(d.feed_flags.get(k) for k in ("rkn", "judicial", "block")):
        return "feed_flag"
    if d.referring_domains is not None and d.referring_domains < st["min_referring_domains"]:
        return "low_rd"

    # T1 — приобретаемость + возраст (ОДИН whois-вызов, под бюджетом)
    if whois_budget is not None and whois_budget[0] <= 0:
        sig["acquirability_unresolved"] = True     # бюджет whois на прогон исчерпан — оставить discovered
        return None
    age_known = False
    try:
        if whois_budget is not None:
            whois_budget[0] -= 1
        pr = c["aparser"].whois_probe(d.domain)
    except Exception as e:  # noqa: BLE001
        sig["errors"].append(f"whois:{type(e).__name__}")
        if d.lane != "bid":                         # сырому источнику whois нужен для лейна
            sig["acquirability_unresolved"] = True
            return None
        pr = {"available": None, "created": None}   # bid: лейн из источника, продолжаем без возраста

    wc = pr.get("created")
    sig["whois_created"] = wc
    if wc is not None:
        age_known = True
        age = (now - wc).days / 365.25
        sig["age_years"] = round(age, 2)
        if age < st["min_age_years"]:
            return "too_young"

    if d.lane == "bid":
        sig["lane"] = "bid"
    else:
        av = pr.get("available")
        if av is True:
            sig["lane"] = "free"                    # свободен к регистрации
        elif av is False:
            # занят СЕЙЧАС. Для сырого источника это может быть дропающийся домен, ещё
            # зарегистрированный до своей даты: известный будущий дедлайн -> ждём, оставляем
            # discovered (перепробуем после дропа). Нет дедлайна / он в прошлом -> реально занят.
            dl = d.acquire_deadline
            if dl is not None and dl.tzinfo is None:
                dl = dl.replace(tzinfo=timezone.utc)
            if dl is not None and dl > now:
                sig["acquirability_unresolved"] = True
                return None
            return "not_acquirable"                 # занят, купить нельзя
        else:                                       # av is None — не определили
            sig["acquirability_unresolved"] = True
            return None

    # T2 — риск (средне): РКН + Spamhaus + indexed_echo
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
    if sig.get("blacklisted") is None and "blacklisted" in sig:
        sig["errors"].append("blacklist:unavailable")   # транзиент -> risk-guard -> manual
    try:
        sig["indexed_echo"] = c["searxng"].indexed_echo(d.domain)
    except Exception as e:  # noqa: BLE001
        sig["errors"].append(f"searxng:{type(e).__name__}")

    # T3 — история (дорого): только для приобретаемых выживших
    try:
        hist = c["wayback"].classify_history(d.domain)
        pf = hist.get("prior_flags") or {}
        sig["prior_flags"] = pf
        sig["wayback_checked"] = hist.get("wayback_checked")     # сохраняем ДО возможного выхода
        sig["first_seen"] = hist.get("first_seen")
        if sig.get("whois_created") is None and hist.get("age_years") is not None:
            sig["age_years"] = hist["age_years"]           # whois приоритетнее; Wayback — фолбэк
        if any(pf.get(k) for k in cfg.HARD_REJECT_FLAGS) or pf.get("topic_switch"):
            return "history_dirty"
    except Exception as e:  # noqa: BLE001
        sig["errors"].append(f"wayback:{type(e).__name__}")

    # непроверяемый по whois возраст всё равно проходит гейт молодости (ПОСЛЕ history_dirty)
    if not age_known and sig.get("age_years") is not None and sig["age_years"] < st["min_age_years"]:
        return "too_young"

    # T3b — Ahrefs (дорого, капча за деньги): ТОЛЬКО если фид не дал RD (cctld/reg_ru/
    # sweb — у backorder RD уже есть, повторно не проверяем) и бюджет жив.
    # ahrefs_budget=None -> НЕ вызываем (в отличие от whois_budget=None=безлимит — Ahrefs
    # платный, дефолт должен быть "выключено", а не "неограниченно").
    if d.referring_domains is None and ahrefs_budget is not None and ahrefs_budget[0] > 0:
        ahrefs_budget[0] -= 1
        try:
            ah = c["aparser"].ahrefs_probe(d.domain)
            sig["dr"] = ah["dr"]
            sig["ahrefs_backlinks"] = ah["backlinks"]
            if ah["referring_domains"] is not None:
                sig["referring_domains"] = ah["referring_domains"]
        except Exception as e:  # noqa: BLE001
            sig["errors"].append(f"ahrefs:{type(e).__name__}")
    return None


def score_domain(domain_id: int, clients: dict | None = None, whois_budget=None,
                 ahrefs_budget=None) -> dict:
    """Полная воронка для одного домена. whois_budget — мутабельный [int] или None (без лимита)."""
    from app.db import SessionLocal
    from app.models.domain import Domain
    from app.services.settings import get_settings

    st = get_settings()
    with SessionLocal() as db:
        d = db.get(Domain, domain_id)
        if d is None:
            raise ValueError(f"domain {domain_id} not found")
        if d.status not in ("discovered", "scored", "rejected"):
            return {"domain": d.domain, "status": d.status, "skipped": "status"}
        c = clients or _make_clients()
        sig: dict = {"errors": []}
        sig["trademark_risk"] = d.trademark_risk
        reject = _funnel(d, c, st, sig, whois_budget, ahrefs_budget)

        if sig.get("acquirability_unresolved"):
            # приобретаемость не определена (whois сбой/непонятно/бюджет) — НЕ пишем,
            # домен остаётся discovered, следующий прогон перепробьёт (см. спек §D).
            return {"domain": d.domain, "status": d.status, "unresolved": True,
                    "errors": sig.get("errors", [])}

        if reject:
            result = {"score": 0.0, "status": "rejected", "breakdown": {"funnel_reject": reject}}
        else:
            sig.setdefault("referring_domains", d.referring_domains)
            result = compute_score(sig)
            if "hard_reject" not in result["breakdown"]:
                # Finding 1 (2026-07 review): re-decide with the RUNTIME /settings thresholds
                # (not just cfg.DECISION) so the approve/manual-review sliders actually govern
                # the stored status, not only the /settings preview counters. Hard-rejects
                # (score 0.0) are excluded — they must never be "rescued" by a low threshold.
                result = {**result, "status": _decide(result["score"], sig,
                                                      st["approve_at"], st["manual_review_at"])}

        d.lane = sig.get("lane") or d.lane
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
        if sig.get("referring_domains") is not None:
            d.referring_domains = sig["referring_domains"]
        d.clean = result["status"] != "rejected"
        d.score = result["score"]
        d.score_breakdown = {**result["breakdown"], "errors": sig.get("errors", []),
                             "ahrefs_backlinks": sig.get("ahrefs_backlinks")}
        d.status = result["status"]
        d.reject_reason = reject or ("low_score" if result["status"] == "rejected" else None)
        db.commit()
        return {"domain": d.domain, **result, "reject_reason": d.reject_reason,
                "errors": sig.get("errors", [])}


def score_pending(limit: int = 100, on_progress=None) -> int:
    """Score all `discovered` domains; return count processed. on_progress(done,total,current)."""
    from sqlalchemy import select
    from app.db import SessionLocal
    from app.models.domain import Domain
    from app.services.settings import get_settings

    st = get_settings()
    with SessionLocal() as db:
        rows = db.execute(
            select(Domain.id, Domain.domain).where(Domain.status == "discovered")
            .order_by(Domain.referring_domains.desc().nulls_last())  # лучшие кандидаты первыми
            .limit(limit)
        ).all()
    clients = _make_clients()  # один набор клиентов на весь прогон, не на домен
    whois_budget = [int(st["max_whois_per_run"])]   # общий на прогон: cctld (RD=null) идёт последним
    ahrefs_budget = [int(st["max_ahrefs_per_run"])]  # общий на прогон: платная капча, 0 = выключено
    total = len(rows)
    for i, (did, name) in enumerate(rows, 1):
        # репорт ДО скоринга: total известен сразу (бар не висит в 0/0), а current —
        # домен, который сейчас в работе (whois/Wayback идут секунды, оператор видит кого).
        if on_progress:
            on_progress(i - 1, total, name)
        try:
            score_domain(did, clients, whois_budget, ahrefs_budget)
        except Exception:  # noqa: BLE001 — падение одного домена не топит батч (как в оркестраторе)
            logging.getLogger(__name__).exception("score_domain %s упал", name)
    if on_progress:
        on_progress(total, total, "")   # все готовы
    return total


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
