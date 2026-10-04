"""Веса критериев — рантайм, а не константа в коде.

Жалоба оператора (2026-07-13): «в настройках стоят все пункты, по которым идёт оценка донора —
нет возможности скорректировать». Веса жили в scoring_config.WEIGHTS и не менялись ничем.
"""
from app.services import scoring
from app.services import scoring_config as cfg
from app.services.settings import get_settings, update_settings

SIG = {"wayback_checked": True, "prior_flags": {}, "age_years": 8,
       "referring_domains": 3000, "dr": None}
ZERO = dict.fromkeys(cfg.WEIGHTS, 0.0)


def test_weights_default_to_config(sqlite_db):
    assert get_settings()["weights"] == cfg.WEIGHTS


def test_weights_actually_move_the_score(sqlite_db):
    """Ползунок обязан менять РЕЗУЛЬТАТ, а не только показания на экране: ровно этим болели
    пороги до фикса 2026-07 (двигали превью-счётчики, но не статус)."""
    base = scoring.compute_score(SIG)["score"]
    dr_only = scoring.compute_score(SIG, {**ZERO, "authority": 1.0})["score"]
    assert dr_only == 0.0                   # DR не дан, а он теперь ЕДИНСТВЕННЫЙ критерий
    assert base > 0.5                       # с дефолтными весами тот же домен — сильный
    rd_only = scoring.compute_score(SIG, {**ZERO, "rd": 1.0})["score"]
    assert rd_only > 0.9                    # RD=3000 = RD_FULL -> почти полный балл


def test_weights_are_normalised_so_thresholds_keep_meaning(sqlite_db):
    """Сумма весов не обязана быть 1.0. Без нормировки оператор, выкрутивший все ползунки в 1.0,
    получил бы score в разы больше — и пороги стали бы значить совсем не то, что показывают."""
    all_ones = dict.fromkeys(cfg.WEIGHTS, 1.0)
    doubled = {k: v * 2 for k, v in cfg.WEIGHTS.items()}
    assert scoring.compute_score(SIG, doubled)["score"] == scoring.compute_score(SIG)["score"]
    assert 0.0 <= scoring.compute_score(SIG, all_ones)["score"] <= 1.0


def test_degenerate_weights_fall_back_to_defaults(sqlite_db):
    """Все нули обнулили бы score ВСЕМ доменам и тихо превратили воронку в «всё отклонено»."""
    update_settings(weights=dict.fromkeys(cfg.WEIGHTS, 0.0))
    assert get_settings()["weights"] == cfg.WEIGHTS


def test_saved_weights_reach_the_funnel(sqlite_db, client):
    """Сквозной путь: форма -> БД -> get_settings -> compute_score."""
    r = client.post("/settings/save", data={
        "min_referring_domains": 10, "min_age_years": 3.0, "approve_at": 0.7,
        "manual_review_at": 0.35, "max_whois_per_run": 200,
        "nominet": "on", "w_history_cleanliness": 0.5, "w_topical_fit": 0.2, "w_age": 0.0,
        "w_rd": 0.5, "w_authority": 0.0, "w_anchor_quality": 0.3, "w_traffic_history": 0.1,
    }, follow_redirects=False)
    assert r.status_code == 303
    w = get_settings()["weights"]
    assert w["history_cleanliness"] == 0.5 and w["age"] == 0.0 and w["authority"] == 0.0
    assert w["topical_fit"] == 0.2 and w["anchor_quality"] == 0.3 and w["traffic_history"] == 0.1


def test_form_without_weights_does_not_wipe_them(sqlite_db, client):
    """Старая форма/скрипт без полей w_* не должны ОБНУЛИТЬ шкалу оценки."""
    update_settings(weights={**cfg.WEIGHTS, "history_cleanliness": 0.9})
    client.post("/settings/save", data={
        "min_referring_domains": 10, "min_age_years": 3.0, "approve_at": 0.7,
        "manual_review_at": 0.35, "max_whois_per_run": 200,
        "nominet": "on"}, follow_redirects=False)
    assert get_settings()["weights"]["history_cleanliness"] == 0.9
