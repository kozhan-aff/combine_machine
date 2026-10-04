"""compute_score v2: компоненты, нейтральные значения, жёсткие отказы, PBN, живой спам-дроп;
авто-одобрения нет (Р2) — гард «возраст неизвестен» переехал в пакет."""
from datetime import datetime, timezone

import pytest

from app.models.domain import Domain
from app.services import scoring_config as cfg
from app.services.scoring import _decide, blind_reason, bulk_ok, compute_score

BASE = {"wayback_checked": True, "prior_flags": {}, "age_years": 10.0, "deep_checked": True}


def test_weights_sum_to_one_and_keys():
    assert set(cfg.WEIGHTS) == {"history_cleanliness", "topical_fit", "age", "rd", "authority",
                                "anchor_quality", "traffic_history"}
    assert abs(sum(cfg.WEIGHTS.values()) - 1.0) < 1e-9


@pytest.mark.parametrize("sig,code", [
    ({"blacklisted": True}, "blacklisted"),
    ({"webrisk_threats": ["MALWARE"]}, "webrisk"),
    ({"trademark_risk": True}, "trademark"),
    ({"prior_flags": {"casino": True}}, "prior_casino"),
])
def test_hard_rejects(sig, code):
    r = compute_score({**BASE, **sig})
    assert r["status"] == "rejected" and r["score"] == 0.0 and code in r["breakdown"]["hard_reject"]


def test_missing_soft_signals_are_neutral():
    comp = compute_score(dict(BASE))["breakdown"]["components"]
    assert comp["topical_fit"] == 0.5 and comp["anchor_quality"] == 0.5 and comp["traffic_history"] == 0.5
    assert comp["rd"] == 0.0 and comp["authority"] == 0.0


def test_pbn_suspect_halves_rd():
    clean = compute_score({**BASE, "referring_domains": 717, "ref_subnets": 600})
    pbn = compute_score({**BASE, "referring_domains": 717, "ref_subnets": 198})
    assert pbn["breakdown"]["pbn_suspect"] is True and clean["breakdown"]["pbn_suspect"] is False
    assert abs(pbn["breakdown"]["components"]["rd"] * 2 - clean["breakdown"]["components"]["rd"]) < 1e-9


def test_pbn_penalty_keeps_live_spam_drop_below_strong_threshold():
    """Живой профиль 2026-10-01 (pharmaindustrie.com): RD 717, подсети 198 — спам-сетка. DR 5 —
    ровно такой домен проходит фильтр DR на входе discovery. Без штрафа PBN он взял бы порог
    сильного кандидата approve_at (0.7095), со штрафом — нет (0.6356)."""
    sig = {**BASE, "dr": 5.0, "referring_domains": 717}
    pbn = compute_score({**sig, "ref_subnets": 198})
    clean = compute_score({**sig, "ref_subnets": 600})
    assert pbn["breakdown"]["pbn_suspect"] is True
    assert pbn["score"] == pytest.approx(0.6356, abs=1e-4)
    assert clean["score"] == pytest.approx(0.7095, abs=1e-4)
    assert pbn["score"] < cfg.DECISION["approve_at"] <= clean["score"]


def test_strong_clean_domain_scores_high_but_is_only_scored():
    r = compute_score({**BASE, "dr": 35.0, "referring_domains": 900, "ref_subnets": 700,
                       "topical_relevance": 0.9, "spam_anchor_ratio": 0.02, "peak_traffic": 4000})
    assert r["score"] >= 0.85 and r["status"] == "scored"           # одобряет только человек (Р2)


def test_decide_never_approves_only_scored_or_rejected():
    """Р2: авто-одобрения нет вообще — даже максимальный балл со всеми проверками даёт `scored`."""
    assert _decide(1.0, {"wayback_checked": True, "errors": [], "age_years": 20}, 0.4) == "scored"
    assert _decide(0.4, {}, 0.4) == "scored"
    assert _decide(0.39, {}, 0.4) == "rejected"


def test_domain_without_any_age_is_blind_and_out_of_bulk():
    """Гард «возраст неизвестен» жил в _decide; авто-одобрения больше нет (Р2), и его держит
    пакет: возраста не дал никто (ни RDAP/whois, ни архив) — домен «вслепую», в пакет не идёт."""
    kw = dict(domain="noage.com", wayback_checked=True, prior_flags={},
              score_breakdown={"errors": [], "history_evidence": []})
    d = Domain(**kw)
    assert blind_reason(d) == "возраст НЕ проверен: возраста нет ни из RDAP/whois, ни из архива"
    assert bulk_ok(d) is False
    reg = datetime(2010, 1, 1, tzinfo=timezone.utc)
    assert blind_reason(Domain(**kw, whois_created=reg)) is None     # возраст из RDAP/whois
    assert blind_reason(Domain(**kw, first_seen=reg)) is None        # возраст из архива
    assert bulk_ok(Domain(**kw, age_years=9.0)) is True
