"""Экран «Автопилот»: рендер, сохранение настроек, запуск ручного свипа, whitelist прогресса."""
import app.db as db
from app.models.domain import Domain, AcquisitionOrder
from app.models.site import Site, Page
from app.services import autonomy


def test_autopilot_renders(client):
    r = client.get("/autopilot")
    assert r.status_code == 200
    assert "Мастер-выключатель" in r.text     # станция мастера (не сайдбар — контент экрана)
    assert "на курацию" in r.text             # блок «ждёт тебя»


def test_autopilot_settings_save(client):
    r = client.post("/autopilot/settings", data={
        "autopilot_on": "on", "sweep_interval_min": 30,
        "auto_score": "on", "cap_score": 15}, follow_redirects=False)
    assert r.status_code == 303
    a = autonomy.get_autonomy()
    assert a["autopilot_on"] is True and a["sweep_interval_min"] == 30
    assert a["auto_score"] is True and a["cap_score"] == 15
    assert a["auto_publish"] is False          # непереданный чекбокс -> False


def test_autopilot_unchecked_toggle_turns_off(client):
    autonomy.update_autonomy(auto_queue=True)
    client.post("/autopilot/settings", data={"sweep_interval_min": 60}, follow_redirects=False)
    assert autonomy.get_autonomy()["auto_queue"] is False   # чекбокс не пришёл -> выкл


def test_autopilot_run_starts_job(client, monkeypatch):
    seen = {}
    monkeypatch.setattr("app.services.orchestrator.run_sweep",
                        lambda **k: seen.update(k) or {"run_id": 1, "status": "done", "counts": {}, "errors": []})
    r = client.post("/autopilot/run", follow_redirects=False)
    assert r.status_code == 303
    import time
    for _ in range(50):                        # джоб фоновый — дождаться исполнения
        if seen:
            break
        time.sleep(0.02)
    assert seen.get("trigger") == "manual" and seen.get("respect_master") is False


def test_sweep_progress_whitelisted(client):
    assert client.get("/run/sweep/progress").status_code == 200
    assert client.get("/run/bogus/progress").status_code == 404


def test_gates_counts(client):
    with db.SessionLocal() as s:
        s.add(Domain(domain="sc.ru", source="backorder", status="scored"))
        d = Domain(domain="q.ru", source="backorder", status="purchasing")
        s.add(d); s.commit()
        s.add(AcquisitionOrder(domain_id=d.id, provider="backorder",
                               status="pending_confirm", confirmed_by_human=False))
        site = Site(domain_id=d.id, status="content"); s.add(site); s.commit()
        s.add(Page(site_id=site.id, url_path="/", status="draft")); s.commit()
    html = client.get("/autopilot").text
    assert "/domains?status=scored" in html and "/queue" in html


def test_dashboard_shows_autopilot_strip(client):
    # сайдбар (Task 5) уже содержит «Автопилот»/href — проверяем текст самой полоски
    autonomy.update_autonomy(autopilot_on=True)
    html = client.get("/").text
    assert "✈ Автопилот: вкл" in html          # бейдж мастера в полоске
    assert "последний свип" in html and "ждёт тебя" in html
