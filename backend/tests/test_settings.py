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
