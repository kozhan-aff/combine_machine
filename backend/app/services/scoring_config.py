"""Tunable thresholds and weights for donor scoring. See docs/DONORS.md.

v1 runs on the FREE stack (Wayback/RKN/Spamhaus/SearXNG) + Ahrefs DR/backlinks/
referring-domains via A-Parser (RuCapcha Turnstile-solver — live-verified 2026-07-08,
see docs/superpowers/specs/2026-07-08-ahrefs-dr-design.md). Every component lands in
Domain.score_breakdown for transparency.

Ahrefs is called ONLY for T3 survivors the discovery feed didn't already give a
referring-domains count for (cctld/reg_ru/sweb — backorder domains keep trusting the
feed's own RD, no duplicate paid call), and only under the runtime `max_ahrefs_per_run`
budget (services/settings.py) — it costs real money per captcha-solve.
"""

# Stage B — light pre-filter (drop obvious garbage before the heavy Wayback pass).
# Lenient on RD: the backorder feed already gives >=1 donor, and the project takes
# domains for clean history, NOT for link juice.
PREFILTER = {
    "min_referring_domains": 1,   # from feed `links`
    "min_dr_proxy": 0.0,          # Ahrefs DR 0..100; 0 = don't gate on it
}

# Stage E — hard rejects (score -> 0, status rejected regardless of the rest)
# spam included: project invariant — ANY dirty-history flag rejects (see CLAUDE.md).
HARD_REJECT_FLAGS = ("adult", "pharma", "casino", "gambling", "spam")  # prior_flags categories
# also hard-reject on: rkn_listed, blacklisted is True.
# Здесь БЫЛИ ещё `prior_flags.topic_switch` и `trademark_risk` — оба удалены (аудит 2026-07-14):
# первый не мог добавить ни одного отказа (подмножество категорий выше), у второго не было ни
# одного производителя. Проверка, которой нет, не должна выглядеть работающей — см. compute_score.

# Stage F — composite weights (positives; sum = 1.0). Free-stack + Ahrefs (live-verified
# 2026-07-08, see docs/superpowers/specs/2026-07-08-ahrefs-dr-design.md).
# `authority` (DR) now carries real weight — Ahrefs replaces the old free DR-proxy path
# (see docs/api/openpagerank.md, deprecated).
WEIGHTS = {
    "history_cleanliness": 0.35,  # from Wayback prior_flags (spam etc.)
    "age": 0.18,                  # Wayback first_seen, normalized by AGE_FULL
    "rd_proxy": 0.27,             # referring_domains (feed `links` or Ahrefs `domains`), log-normalized
    "indexed_echo": 0.08,         # still in the index (SearXNG site:)
    "authority": 0.12,            # Ahrefs DR, normalized by DR_FULL
}

# Normalization anchors ("full credit" points) for the 0..1 components
NORM = {
    "DR_FULL": 30.0,     # Ahrefs DR (0-100 scale) — 30+ is already strong for a drop-candidate,
                         # NOT calibrated to sites like Wikipedia (DR 97, off the scale on purpose)
    "AGE_FULL": 8.0,     # years
    "RD_FULL": 3000.0,   # referring domains (log scale) — spreads real drop RD, was 100 (clamped all)
}

# Decision thresholds on final score (0..1). Between review and approve -> manual review.
DECISION = {
    "approve_at": 0.70,
    "manual_review_at": 0.40,   # below this -> reject
}

# Дефолты для рантайм-настроек (services/settings.py сидит из них при первом обращении).
MIN_AGE_YEARS = 3.0                                          # T1 whois-гейт: моложе — reject too_young
SOURCES_ENABLED = {"backorder": True, "cctld": False, "reg_ru": False, "sweb": False}  # сырые витрины выключены до выверки живой разметки (аудит 2026-07-07)
MAX_WHOIS_PER_RUN = 200        # кап whois-пробоев за один прогон проверки (защита от сырого cctld)
MAX_AHREFS_PER_RUN = 50         # кап платных Ahrefs-вызовов (капча за штуку) за прогон; 0 = выключить
