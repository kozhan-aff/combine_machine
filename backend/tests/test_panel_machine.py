"""Компонент «машина сейчас»: контейнеры на месте, поллер один, старой полосы нет."""


def test_base_ships_machine_bar_and_poller(client):
    html = client.get("/offers").text          # любой экран без #machine
    assert 'id="mbar"' in html
    assert "/api/jobs/live" in html            # поллер живёт в base.html — работает везде


def test_domains_has_full_machine_container(client):
    html = client.get("/domains").text
    assert 'id="machine"' in html


def test_old_progress_widget_is_gone(client):
    html = client.get("/domains").text
    assert 'id="prog"' not in html
    assert "/run/discovery/progress" not in html


def test_autopilot_uses_the_same_component(client):
    """У /autopilot был СВОЙ #prog с поллером /run/sweep/progress — роут снесён, значит
    экран обязан переехать на общий компонент, иначе останется мёртвая полоса."""
    html = client.get("/autopilot").text
    assert 'id="machine"' in html
    assert 'id="prog"' not in html and "/run/sweep/progress" not in html


def test_dashboard_shows_machine_block_and_last_runs(client):
    from app.services import jobs
    with jobs.track("recheck"):
        jobs.report("recheck", done=200, total=200,
                    message="занято уже 3 из отобранных — ушли в rejected")
    html = client.get("/").text
    assert 'id="machine"' in html and 'id="machine-idle"' in html
    assert "Машина сейчас" in html
    assert "занято уже 3 из отобранных" in html      # итог последнего прогона виден в простое
    assert "Простаивает" in html
