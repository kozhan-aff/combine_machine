"""Tunable thresholds and weights for donor scoring. See docs/DONORS.md.

v2 (docs/v2/02-m1-discovery-scoring-spec.md): RDAP/whois, Google Web Risk, Ahrefs API v3
(batch-analysis в W4, анкоры и история трафика в W6 — под капами /settings и полом остатка
units), Wayback + LLM. Every component lands in Domain.score_breakdown for transparency.
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
# also hard-reject on: blacklisted is True, webrisk_threats (Google Web Risk), trademark_risk
# (бренд-токен в имени — его ставит W0 по domain_filters.brand_hit). В v1 `trademark_risk` был
# призраком (ни одного производителя, аудит 2026-07-14), в v2 производитель есть. `topic_switch`
# удалён насовсем: подмножество категорий выше, не мог добавить ни одного отказа.

# Stage F v2 — сумма 1.0 (docs/v2/02-m1-discovery-scoring-spec.md §3.2). Нет сигнала у
# topical_fit/anchor_quality/traffic_history -> 0.5 (нейтрально), см. compute_score.
WEIGHTS = {
    "history_cleanliness": 0.25,  # Wayback: проверена и чиста
    "topical_fit": 0.15,          # W5 LLM: близость прошлой темы к VPN/приватности/софту (expired domain abuse)
    "age": 0.12,
    "rd": 0.18,                   # W4 refdomains, лог-шкала, ×0.5 при подозрении на PBN
    "authority": 0.10,            # DR — главный честный сигнал на спам-дропах
    "anchor_quality": 0.12,       # W6: 1 − доля спам-анкоров
    "traffic_history": 0.08,      # W6: пик органического трафика за 5 лет
}
NORM = {"DR_FULL": 30.0, "AGE_FULL": 8.0, "RD_FULL": 3000.0, "TRAFFIC_FULL": 5000.0}
# PBN/спам-сетка: живые спам-дропы дают refips_subnets/refdomains ≈ 0.27 (2026-10-01).
# ponytail: порог стартовый — калибровать по водопаду первого живого прогона.
PBN_SUBNET_RATIO = 0.3
PBN_MIN_RD = 20

# Decision thresholds on final score (0..1). Between review and approve -> manual review.
DECISION = {
    "approve_at": 0.70,
    "manual_review_at": 0.40,   # below this -> reject
}

# Дефолты для рантайм-настроек (services/settings.py сидит из них при первом обращении).
MIN_AGE_YEARS = 3.0                                          # T1 whois-гейт: моложе — reject too_young
SOURCES_ENABLED = {"dropcatch": False, "nominet": True, "mx": True, "emd": True}  # dropcatch — после проверки ToS оператором
MAX_WHOIS_PER_RUN = 200        # кап whois-пробоев за один прогон проверки (защита от сырого cctld)

# ---- v2: международные домены (docs/v2/02-m1-discovery-scoring-spec.md) ----
MIN_DR = 5.0                 # фильтр по DR на входе discovery: основная масса дропов — DR 0–4 со спамом
TLD_ALLOWLIST = ["com", "net", "org", "online", "xyz", "site", "co.uk", "mx", "co", "si", "nl", "in"]
BRAND_TOKENS = ["nordvpn", "expressvpn", "surfshark", "protonvpn", "cyberghost", "ipvanish",
                "privateinternetaccess", "mullvad", "windscribe", "hotspotshield", "tunnelbear",
                "purevpn", "vyprvpn", "hidemyass", "atlasvpn", "privadovpn", "hideme", "strongvpn",
                "zenmate", "avast", "kaspersky", "norton"]
MAX_LINKS_PER_RUN = 500      # W4 Ahrefs batch-analysis: 25 units/домен
MAX_DEEP_PER_RUN = 20        # W6 анкоры + история трафика: ~1,1 тыс. units/домен; 0 = выключить
SPAM_ANCHOR_MAX = 0.2        # доля спам-анкоров (по refdomains), выше — отказ spam_anchors
TOPIC_FAR_BELOW = 0.3        # близость прошлой темы к VPN ниже — пакет не берёт (инвариант 4, Р2)
UNITS_FLOOR = 300_000        # пол остатка units Ahrefs в месяце: ниже — W4/W6 не тратят (автопилот — раз в час)
