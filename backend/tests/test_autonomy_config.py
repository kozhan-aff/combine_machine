"""Конфиг автономии: дефолты (seed при первом чтении), кламп границ, bool-приведение."""
from app.services import autonomy


def test_get_autonomy_seeds_defaults():
    a = autonomy.get_autonomy()
    assert a["autopilot_on"] is False
    assert a["sweep_interval_min"] == 60
    for stage in ("discovery", "score", "queue", "provision", "generate", "publish", "check_index"):
        assert a[f"auto_{stage}"] is False
    assert a["cap_score"] == 20 and a["cap_queue"] == 10 and a["cap_provision"] == 5
    assert a["cap_generate"] == 5 and a["cap_publish"] == 5 and a["cap_check_index"] == 20
    assert "cap_discovery" not in a          # у discovery капа нет


def test_update_autonomy_clamps_and_coerces():
    a = autonomy.update_autonomy(sweep_interval_min=2, cap_score=9999, autopilot_on="on", auto_score=True)
    assert a["sweep_interval_min"] == 5       # кламп нижней границы [5,1440]
    assert a["cap_score"] == 500              # кламп верхней границы [0,500]
    assert a["autopilot_on"] is True          # "on" -> True
    assert a["auto_score"] is True


def test_update_autonomy_ignores_unknown_keys():
    autonomy.update_autonomy(bogus_key=123, cap_queue=7)
    a = autonomy.get_autonomy()
    assert "bogus_key" not in a and a["cap_queue"] == 7


def test_reset_autonomy_restores_defaults():
    autonomy.update_autonomy(autopilot_on=True, cap_score=1, auto_publish=True)
    a = autonomy.reset_autonomy()
    assert a["autopilot_on"] is False and a["cap_score"] == 20 and a["auto_publish"] is False


def test_autonomy_status_column_fits_terminal_statuses():
    # На SQLite VARCHAR-длина не enforce-ится, поэтому проверяем САМУ схему, а не INSERT:
    # колонка обязана вмещать самый длинный терминальный статус свипа.
    from app.models.autonomy import AutonomyRun
    longest = "completed_with_errors"          # 21 символ, orchestrator.py:377
    assert AutonomyRun.__table__.c.status.type.length >= len(longest)
