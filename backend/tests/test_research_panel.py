"""Карточка сайта: блок «Досье», кнопка сборки уходит в фон, пересборка с force."""
import app.db as db
from app.models.domain import Domain
from app.models.offer import Offer
from app.models.research import SiteResearch
from app.models.site import Site
from app.services import research


def _site() -> int:
    with db.SessionLocal() as s:
        o = Offer(brand="Durev VPN", affiliate_link="https://durevpn.com", language="ru", active=True)
        d = Domain(domain="t.xyz", source="list", status="purchased", market_lang="ru")
        s.add_all([o, d]); s.commit()
        site = Site(domain_id=d.id, status="content", doc_root="/x", offer_id=o.id, provision_step="done")
        s.add(site); s.commit()
        return site.id


def test_card_shows_empty_dossier_and_button(client):
    sid = _site()
    html = client.get(f"/sites/{sid}").text
    assert "Досье конкурентов" in html and f'action="/sites/{sid}/research"' in html and "досье не собрано" in html
    assert "Без досье тексты не пишутся" in html      # с плана Б это правда: панель и автопилот без досье не пишут


def test_card_lists_sources_and_rebuild(client):
    sid = _site()
    with db.SessionLocal() as s:
        s.add(SiteResearch(site_id=sid, kind="review", query="Durev VPN обзор", rank=1, url="https://a.com/1",
                           domain="a.com", words=800, headings=[["h2", "Цена"]], screenshot_path="/r/1/review-1.png"))
        s.commit()
    html = client.get(f"/sites/{sid}").text
    assert "a.com" in html and "800" in html and "пересобрать" in html


def test_button_spawns_job_and_force_passes_through(client, monkeypatch):
    sid = _site()
    calls = []
    monkeypatch.setattr(research, "build_dossier", lambda s, force=False: calls.append((s, force)) or {"status": "done"})
    from app.services import jobs
    monkeypatch.setattr(jobs, "spawn", lambda name, target: (calls.append(name), target())[0] or True)
    r = client.post(f"/sites/{sid}/research", data={"force": "1"}, follow_redirects=False)
    assert r.status_code == 303 and f"/sites/{sid}" in r.headers["location"] and "msg=" in r.headers["location"]
    assert calls == ["research", (sid, True)]      # spawn получил имя research, сервис — force=True


def test_empty_card_shows_reason_of_last_failed_run(client):
    from datetime import datetime, timezone
    from app.models.job import JobRun
    sid = _site()
    now = datetime.now(timezone.utc)
    with db.SessionLocal() as s:
        s.add(JobRun(name="research", status="done_warn", message="ни одной живой страницы",
                     started_at=now, updated_at=now, finished_at=now))
        s.commit()
    html = client.get(f"/sites/{sid}").text
    assert "Последняя сборка" in html and "ни одной живой страницы" in html
