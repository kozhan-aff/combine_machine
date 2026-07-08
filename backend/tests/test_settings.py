"""Рантайм-настройки воронки: сид дефолтов, обновление с валидацией, сброс."""
from app.services import settings as st
from app.services import scoring_config as cfg


def test_get_settings_seeds_defaults():
    s = st.get_settings()
    assert s["min_age_years"] == cfg.MIN_AGE_YEARS
    assert s["approve_at"] == cfg.DECISION["approve_at"]
    assert s["sources_enabled"] == cfg.SOURCES_ENABLED


def test_update_and_reset():
    st.update_settings(min_age_years=5, approve_at=0.8,
                       sources_enabled={"backorder": True, "cctld": False, "reg_ru": False, "sweb": False})
    s = st.get_settings()
    assert s["min_age_years"] == 5.0 and s["approve_at"] == 0.8
    assert s["sources_enabled"]["cctld"] is False
    st.reset_settings()
    assert st.get_settings()["min_age_years"] == cfg.MIN_AGE_YEARS


def test_update_clamps_out_of_range():
    st.update_settings(approve_at=9.9, min_age_years=-4)
    s = st.get_settings()
    assert s["approve_at"] == 1.0 and s["min_age_years"] == 0.0


def test_default_test_sources_are_backorder_only_offline_guard():
    """Finding 4 (финальное ревью, структурный офлайн-гвард в conftest): без единого явного
    update_settings() дефолт, который видят тесты, — только backorder; cctld/reg_ru/sweb
    (A-Parser) выключены, чтобы будущий тест discovery.run_discovery() не мог тихо уйти
    в живую сеть. Ожидание захардкожено (не сверяется с cfg.SOURCES_ENABLED), чтобы тест
    реально проверял конкретный безопасный дефолт, а не совпадение с самим патчем."""
    s = st.get_settings()
    assert s["sources_enabled"] == {"backorder": True, "cctld": False,
                                    "reg_ru": False, "sweb": False}


def test_max_whois_per_run_default_and_clamp():
    from app.services.settings import get_settings, update_settings
    assert get_settings()["max_whois_per_run"] == 200          # дефолт
    assert update_settings(max_whois_per_run=50)["max_whois_per_run"] == 50
    assert update_settings(max_whois_per_run=999999)["max_whois_per_run"] == 5000  # верхний кламп
    assert update_settings(max_whois_per_run=-5)["max_whois_per_run"] == 1         # нижний кламп (M13, >=1)


def test_approve_clamped_above_manual():
    from app.services import settings
    out = settings.update_settings(approve_at=0.3, manual_review_at=0.8)
    assert out["approve_at"] >= out["manual_review_at"]      # инверсия не записывается


def test_max_whois_min_one():
    from app.services import settings
    assert settings.update_settings(max_whois_per_run=0)["max_whois_per_run"] >= 1


def test_max_ahrefs_per_run_default_and_zero_is_legal():
    """В отличие от max_whois_per_run (нижний кламп >=1), max_ahrefs_per_run — платный
    капча-вызов, 0 должен быть легальным значением (полностью выключает Ahrefs-
    обогащение), а не клампиться вверх до 1."""
    from app.services.settings import get_settings, update_settings
    assert get_settings()["max_ahrefs_per_run"] == 50           # дефолт
    assert update_settings(max_ahrefs_per_run=0)["max_ahrefs_per_run"] == 0
    assert update_settings(max_ahrefs_per_run=10)["max_ahrefs_per_run"] == 10
    assert update_settings(max_ahrefs_per_run=999999)["max_ahrefs_per_run"] == 1000  # верхний кламп
    assert update_settings(max_ahrefs_per_run=-5)["max_ahrefs_per_run"] == 0         # клампится к 0, НЕ к 1
