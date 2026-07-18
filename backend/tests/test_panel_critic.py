"""HTTP-уровневый тест роута критика (Спека 4, задача 2). НЕ гейт: mark_edited
работает независимо, роут /pages/{id}/critique не трогает page.status."""
from datetime import datetime, timezone

import app.db as db
from app.models.domain import Domain
from app.models.site import Site, Page


def _seed_page() -> int:
    with db.SessionLocal() as s:
        d = Domain(domain="critroute.ru", source="backorder", status="purchased")
        s.add(d); s.commit(); s.refresh(d)
        site = Site(domain_id=d.id, status="content")
        s.add(site); s.commit(); s.refresh(site)
        p = Page(site_id=site.id, url_path="/", title="t", status="draft",
                body="черновик", lang="ru")
        s.add(p); s.commit(); s.refresh(p)
        return p.id


def test_critique_route_writes_score_and_redirects(client, monkeypatch):
    monkeypatch.setattr(
        "app.integrations.llm.LlmClient.complete",
        lambda self, system, prompt, **kw: "БАЛЛ: 80\n- норм")
    pid = _seed_page()
    r = client.post(f"/pages/{pid}/critique")
    assert r.status_code == 200          # redirect + follow (см. паттерн test_panel_toctou.py)
    with db.SessionLocal() as s:
        p = s.get(Page, pid)
        assert p.critic_score == 0.8
        assert p.status == "draft"       # ГЕЙТ НЕ ТРОНУТ


def test_critique_route_survives_llm_error(client, monkeypatch):
    def boom(self, system, prompt, **kw):
        raise RuntimeError("LLM недоступен")
    monkeypatch.setattr("app.integrations.llm.LlmClient.complete", boom)
    pid = _seed_page()
    r = client.post(f"/pages/{pid}/critique")
    assert r.status_code == 200          # не 500 — критик advisory, сбой не должен ронять UI


def test_page_edit_view_shows_critic_score_when_present(client):
    pid = _seed_page()
    with db.SessionLocal() as s:
        p = s.get(Page, pid)
        p.critic_score = 0.45
        p.critic_notes = {"issues": ["слабое disclosure"]}
        # critique_page() ВСЕГДА пишет checked_at вместе со score/notes (см.
        # content_critic.py) — в реальности эти три поля появляются атомарно.
        p.critic_checked_at = datetime.now(timezone.utc)
        s.commit()
    r = client.get(f"/pages/{pid}")
    assert r.status_code == 200
    assert "45" in r.text
    assert "слабое disclosure" in r.text
