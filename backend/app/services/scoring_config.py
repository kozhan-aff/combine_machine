"""Tunable thresholds and weights for donor scoring. See docs/DONORS.md.

v1 runs on the FREE stack (no backlink API): weight sits on clean history / age /
RD-proxy / indexed-echo, NOT on RD-quality/anchors/topical (those need Ahrefs and are
an optional later stage). All values are starting points — calibrate against real data.
Every component lands in Domain.score_breakdown for transparency.

# ponytail: OpenPageRank (DR-proxy) closed new free signups after the Keywords
# Everywhere acquisition (2026). DR is now informational-only — computed if an old
# OPR key still works, but carries ZERO weight. The free authority signal is RD
# (referring_domains from the backorder feed). Add DR weight back only if you get a
# paid key. RD_FULL is tuned so real dropped-domain RD (tens..thousands) spreads
# instead of clamping everyone to 1.0 (which pinned every clean domain to one score).
"""

# Stage B — light pre-filter (drop obvious garbage before the heavy Wayback pass).
# Lenient on RD: the backorder feed already gives >=1 donor, and the project takes
# domains for clean history, NOT for link juice.
PREFILTER = {
    "min_referring_domains": 1,   # from feed `links`
    "min_dr_proxy": 0.0,          # OpenPageRank 0..10; 0 = don't gate on it
}

# Stage E — hard rejects (score -> 0, status rejected regardless of the rest)
# spam included: project invariant — ANY dirty-history flag rejects (see CLAUDE.md).
HARD_REJECT_FLAGS = ("adult", "pharma", "casino", "gambling", "spam")  # prior_flags categories
# also hard-reject on: rkn_listed, blacklisted is True, prior_flags.topic_switch

# Stage F — composite weights (positives; sum = 1.0). Free-stack only.
# NB: `authority` (DR) is intentionally absent — OPR is dead-free (see header); its
# old 0.20 was folded into rd_proxy, the surviving free authority signal.
WEIGHTS = {
    "history_cleanliness": 0.40,  # from Wayback prior_flags (spam etc.)
    "age": 0.20,                  # Wayback first_seen, normalized by AGE_FULL
    "rd_proxy": 0.30,             # referring_domains (feed `links`), log-normalized
    "indexed_echo": 0.10,         # still in the index (SearXNG site:)
}

# Normalization anchors ("full credit" points) for the 0..1 components
NORM = {
    "DR_FULL": 6.0,      # OpenPageRank ~6 already strong (informational-only now)
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
SOURCES_ENABLED = {"backorder": True, "cctld": True, "reg_ru": True, "sweb": True}
MAX_WHOIS_PER_RUN = 200        # кап whois-пробоев за один прогон проверки (защита от сырого cctld)
