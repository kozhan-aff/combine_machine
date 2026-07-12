"""M1b — Domain/donor scoring. Implements the funnel in docs/DONORS.md on the FREE stack.

Order: pre-filter -> history (Wayback) -> risk (RKN, blacklist) -> indexed_echo (SearXNG)
-> composite score + breakdown -> status approved | scored(manual) | rejected.
`compute_score` is pure (unit-tested below); `score_domain` does the I/O + DB write.
"""
import logging
import math
from datetime import timedelta

from app.services import scoring_config as cfg

# Запас после дедлайна дропа, прежде чем считать домен потерянным навсегда.
#
# `delete_date` в фиде backorder — ДАТА без времени ("2026-07-08", см. docs/api/backorder.md),
# и discovery._parse_deadline превращает её в 00:00 UTC дня дропа. Значит уже в 00:01 того же
# дня условие «дедлайн в будущем» ложно — а домен ещё зарегистрирован: реестр освобождает его
# в течение дня. Без этого запаса перепроверка отбраковывала бы дроп РОВНО В ТОТ ДЕНЬ, когда
# его можно ловить, то есть выбрасывала бы самые ценные домены. Запас покрывает и полуночное
# усечение даты, и сдвиг релиза в реестре на сутки.
DROP_GRACE = timedelta(days=2)


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


def acquirability_verdict(available, acquire_deadline, now, *, lane) -> str:
    """whois-доступность + дедлайн ловли -> 'free' | 'taken' | 'waiting' | 'unknown'.

    ЕДИНСТВЕННОЕ место, где решается «можно ли ещё купить». Его зовут и воронка (T1, при
    первом скоринге), и перепроверка (recheck_acquirability, потом) — двух версий правды
    здесь быть не должно.

    'waiting' — домен занят СЕЙЧАС, и это нормально: дроп ещё не наступил. Так выглядит
    любой backorder-кандидат до своей delete_date.
    'taken' — занят, и ждать больше нечего: дедлайн с запасом прошёл (домен продлили или
    перехватили) либо свободный домен кто-то зарегистрировал. Для отобранного донора это
    и есть протухание.

    ОСТОРОЖНО: 'taken' стоит дорого — домен уходит в rejected. Поэтому в каждом сомнении
    отвечаем 'unknown'/'waiting', а не 'taken': потерянный ценный дроп хуже лишней проверки.
    Именно поэтому `lane` — обязательный именованный аргумент: с дефолтом None вызывающий,
    забывший его передать, получал бы 'taken' на bid-домене, то есть ровно тот баг.
    """
    from datetime import timezone
    if available is True:
        return "free"
    if available is None:
        return "unknown"
    # available is False — домен ЗАНЯТ сейчас. Навсегда ли — решает дедлайн дропа.
    dl = acquire_deadline
    if dl is not None and dl.tzinfo is None:          # из БД дата может прийти naive
        dl = dl.replace(tzinfo=timezone.utc)
    if dl is None:
        # Для bid-лейна «занят» — НОРМАЛЬНОЕ состояние: домен ждёт своего дропа, он и должен
        # быть зарегистрирован. Без даты дропа судить не по чему, а цена ошибки — выброшенный
        # ценный дроп. Молчим. (Дедлайн теряется, если фид отдал непарсящийся delete_date.)
        return "unknown" if lane == "bid" else "taken"
    if now <= dl + DROP_GRACE:
        return "waiting"                             # дроп ещё не наступил или идёт прямо сейчас
    return "taken"                                   # дедлайн с запасом прошёл, а домен занят


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

    # whois по этому домену только что состоялся — зафиксируем факт сверки, иначе свежеотскоренный
    # донор уходит в БД с checked_at=NULL, встаёт в самую голову nulls_first-очереди перепроверки,
    # и первый же её клик тратит квоту A-Parser на домен, чей whois-ответ получен пять минут назад
    # (а подсказка «N не сверялись» краснеет сразу после Score).
    if pr.get("available") is not None:
        sig["acquirability_checked_at"] = now

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
        # сюда попадаем только при lane != "bid" (bid короткозамкнут выше), но передаём явно:
        # вердикт судит bid-домены иначе, и умолчания здесь стоят слишком дорого
        v = acquirability_verdict(pr.get("available"), d.acquire_deadline, now, lane=d.lane)
        if v == "free":
            sig["lane"] = "free"                    # свободен к регистрации
        elif v == "taken":
            return "not_acquirable"                 # занят, купить нельзя
        else:
            # waiting — дроп ещё не наступил (перепробуем после даты);
            # unknown — whois не дал ответа. И то и другое: оставить discovered.
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
        # факт сверки приобретаемости: скоринг уже сходил в whois — перепроверке незачем
        # повторять это следующим же кликом
        d.acquirability_checked_at = sig.get("acquirability_checked_at") or d.acquirability_checked_at
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


# статусы, где домен — ЕЩЁ НАШ КАНДИДАТ на покупку и им не владеет другая машина.
# purchasing/purchased НЕ трогаем: там живой заказ, его статусом управляет M2 (иначе
# перепроверка отбраковала бы домен из-под оформленного выкупа).
_RECHECK_STATUSES = ("approved", "scored")


def stale_donors(days: int = 3, db=None) -> int:
    """Сколько отобранных доноров давно (или ни разу) не сверялись с whois. Для подписи кнопки.

    `db` — уже открытая сессия (панель отдаёт свою из DI, чтобы не плодить соединение
    на каждый рендер /domains)."""
    from datetime import datetime, timezone
    from sqlalchemy import select, func, or_
    from app.db import SessionLocal
    from app.models.domain import Domain

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    stmt = select(func.count(Domain.id)).where(
        Domain.status.in_(_RECHECK_STATUSES),
        or_(Domain.acquirability_checked_at.is_(None),
            Domain.acquirability_checked_at < cutoff))
    if db is not None:
        return db.execute(stmt).scalar_one()
    with SessionLocal() as s:
        return s.execute(stmt).scalar_one()


def recheck_acquirability(limit: int = 200, on_progress=None) -> dict:
    """Перепроверить whois'ом отобранных доноров: не выкупил ли их кто-то за это время.

    ЗАЧЕМ. Скоринг решает приобретаемость ОДИН раз (T1) и больше к ней не возвращается.
    Но список доноров протухает: домен, одобренный неделю назад, сегодня может быть уже
    зарегистрирован другим — а мы держим его как «готов к выкупу» и однажды поставим на него
    ставку впустую. Отдельного прохода для этого не было; это он.

    Занятый (и ждать нечего) -> rejected/not_acquirable. Свободный / ещё не дропнувшийся —
    остаётся кандидатом, только помечается свежепроверенным. Не определилось (whois молчит,
    сбой) -> НЕ трогаем ни статус, ни отметку: домен остаётся протухшим и попадёт в следующий
    прогон. Денег не тратит, гейтов не касается.

    Бюджет — `max_whois_per_run` с /settings, СВОЙ на прогон (не общий со скорингом: джобы
    single-flight по имени, поэтому Score и Перепроверка могут идти одновременно и взять по
    капу каждый — суммарно до 2× квоты A-Parser). Самые протухшие проверяются первыми.
    """
    from datetime import datetime, timezone
    from sqlalchemy import select, update
    from app.db import SessionLocal
    from app.models.domain import Domain
    from app.services.settings import get_settings

    # checked == сколько whois-вызовов реально сделали == расход бюджета. Обычно он же = сумма
    # free+waiting+taken+unknown; расходится ровно на домены, которые между whois и записью
    # успели уйти в выкуп (см. декремент taken по rowcount ниже) — их отбраковки не было.
    out = {"checked": 0, "free": 0, "waiting": 0, "taken": 0, "unknown": 0}
    budget = int(get_settings()["max_whois_per_run"])
    if budget <= 0:                                   # семантика та же, что в воронке:
        return out                                    # 0 = whois не звать вообще
    with SessionLocal() as db:
        ids = db.execute(
            select(Domain.id).where(Domain.status.in_(_RECHECK_STATUSES))
            # протухшие первыми; id — вторичный ключ, иначе порядок внутри NULL-корзины
            # не определён и прогоны могут топтаться по одним и тем же доменам
            .order_by(Domain.acquirability_checked_at.asc().nulls_first(), Domain.id.asc())
            .limit(min(limit, budget))
        ).scalars().all()

    c = _make_clients()
    total = len(ids)
    for i, did in enumerate(ids, 1):
        with SessionLocal() as db:
            d = db.get(Domain, did)
            if d is None or d.status not in _RECHECK_STATUSES:
                continue                              # статус увели, пока шли (напр. в выкуп)
            name, deadline, lane = d.domain, d.acquire_deadline, d.lane
        if on_progress:
            on_progress(i - 1, total, name)           # репорт ДО вызова: whois идёт секунды

        now = datetime.now(timezone.utc)
        out["checked"] += 1                           # вызов состоялся — бюджет потрачен
        try:
            pr = c["aparser"].whois_probe(name)
        except Exception:  # noqa: BLE001 — падение одного домена не топит батч
            logging.getLogger(__name__).exception("whois-перепроверка %s упала", name)
            out["unknown"] += 1
            continue        # СБОЙ (сеть/A-Parser) — транзиентен. Отметку не ставим: вернёмся.

        # lane обязателен: для bid-домена «занят» — НОРМА (ждёт своего дропа), и без него
        # вердикт отбраковал бы живой дроп.
        v = acquirability_verdict(pr.get("available"), deadline, now, lane=lane)
        out[v] += 1
        if v == "unknown" and pr.get("available") is None:
            continue        # whois ОТВЕТИЛ, но невнятно. Не штампуем — пробуем ещё раз позже.
        # Прочий unknown (bid без дедлайна) whois ОТВЕТИЛ по существу: «занят». Судить не по
        # чему, но ответ ДЕТЕРМИНИРОВАННЫЙ — завтра будет ровно тот же. Такой домен обязан
        # получить отметку, иначе он вечно висит в голове nulls_first-очереди и выедает весь
        # бюджет: если таких доменов больше бюджета (а это ровно авария «фид сменил формат
        # delete_date»), перепроверка никогда не дойдёт до остального списка и молча выродится
        # в no-op. Статус не трогаем — домен остаётся кандидатом; счётчик unknown в сводке
        # покажет оператору, что что-то не так.

        # Атомарно и только из «наших» статусов: между whois-раундтрипом и записью человек
        # мог отправить домен в выкуп (create_order -> purchasing). Голый UPDATE перезатёр бы
        # его нашим rejected и разъехался с живым заказом; rowcount==0 = домен уже не наш.
        with SessionLocal() as db:
            vals = {"acquirability_checked_at": now}
            if v == "taken":
                vals |= {"status": "rejected", "reject_reason": "not_acquirable"}
            res = db.execute(update(Domain)
                             .where(Domain.id == did, Domain.status.in_(_RECHECK_STATUSES))
                             .values(**vals))
            db.commit()
        if v == "taken" and res.rowcount == 0:
            out["taken"] -= 1     # домен успели увести в выкуп — отбраковки НЕ было, не врём

    if on_progress:
        on_progress(total, total, "")
    return out


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
