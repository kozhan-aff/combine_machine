"""Гарды статусов сайта и ворота JSON-двойника: S6-12/S7-05 (generate не перескакивает провижн и не
откатывает published), S5-09 (publish требует провиженный сайт), S4-11/S7-15 (/api/.../provision
проходит тот же CF-write-гейт и не даёт голый 500)."""
import pytest

import app.db as db
from app.config import settings
from app.models.domain import Domain
from app.models.offer import Offer
from app.models.site import Page, Site
from app.services import content, orchestrator, publish


def _site(status="provisioning", name=None, doc_root="/www/wwwroot/g.com", pages=()) -> int:
    with db.SessionLocal() as s:
        d = Domain(domain="g.com", source="dropcatch", status="purchased")
        s.add(d)
        s.commit()
        off = Offer(brand="NordVPN", affiliate_link="https://ex.com/aff", active=True)
        s.add(off)
        s.commit()
        site = Site(domain_id=d.id, status=status, aapanel_site_name=name, doc_root=doc_root,
                    offer_id=off.id)
        s.add(site)
        s.commit()
        for i, st in enumerate(pages):
            s.add(Page(site_id=site.id, url_path=f"/p{i}", title="t", status=st, body="<p>x</p>"))
        s.commit()
        return site.id


@pytest.fixture
def llm(monkeypatch):
    monkeypatch.setattr("app.integrations.llm.LlmClient.complete", lambda self, system, prompt, **kw: "<p>t</p>")


@pytest.fixture
def no_panel_writes(monkeypatch):
    calls = []
    monkeypatch.setattr("app.integrations.aapanel.AaPanelClient.__init__", lambda self: None)
    monkeypatch.setattr("app.integrations.aapanel.AaPanelClient.write_file",
                        lambda self, path, body: calls.append(path))
    return calls


# --- generate -------------------------------------------------------------------------------

def test_generate_refused_for_unprovisioned_site_and_status_untouched(llm):
    sid = _site("provisioning")
    with pytest.raises(ValueError, match="сначала provision"):
        content.generate_site(sid)
    with db.SessionLocal() as s:
        assert s.get(Site, sid).status == "provisioning"     # НЕ перескочил в content
        assert s.query(Page).count() == 0


def test_generate_does_not_regress_published_site(llm):
    sid = _site("published", name="g.com")
    assert content.generate_site(sid) == 3
    with db.SessionLocal() as s:
        assert s.get(Site, sid).status == "published"


def test_generate_keeps_content_status(llm):
    sid = _site("content", name="g.com")
    content.generate_site(sid)
    with db.SessionLocal() as s:
        assert s.get(Site, sid).status == "content"


# --- publish --------------------------------------------------------------------------------

def test_publish_refused_for_unprovisioned_site(no_panel_writes):
    sid = _site("provisioning", pages=("edited",))
    out = publish.publish_site(sid)
    assert out["status"] == "not_provisioned" and "provision" in out["hint"]
    assert no_panel_writes == []
    with db.SessionLocal() as s:
        assert s.query(Page).one().status == "edited" and s.get(Site, sid).status == "provisioning"


def test_publish_refused_without_vhost_or_doc_root(no_panel_writes):
    """status=content, но vhost не создан / нет doc_root (legacy-строка) — раньше AttributeError на rstrip."""
    assert publish.publish_site(_site("content", name=None, pages=("edited",)))["status"] == "not_provisioned"
    with db.SessionLocal() as s:
        s.query(Page).delete(); s.query(Site).delete(); s.query(Domain).delete(); s.commit()
    assert publish.publish_site(_site("content", name="g.com", doc_root=None,
                                      pages=("edited",)))["status"] == "not_provisioned"
    assert no_panel_writes == []


def test_publish_edit_gate_still_answers_first(no_panel_writes):
    sid = _site("provisioning", pages=("draft",))
    assert publish.publish_site(sid)["status"] == "no_edited_pages"


def test_publish_works_for_provisioned_site(no_panel_writes):
    sid = _site("content", name="g.com", pages=("edited",))
    assert publish.publish_site(sid)["status"] == "published" and \
        len([w for w in no_panel_writes if w.endswith("/index.html")]) == 1


def test_sweep_publish_stage_reports_unprovisioned_as_error(no_panel_writes):
    _site("provisioning", pages=("edited",))
    done, errs = orchestrator._stage_publish(5)
    assert done == 0 and len(errs) == 1 and "provision" in errs[0]


# --- /api ворота ----------------------------------------------------------------------------

def test_api_provision_blocked_without_configured_auth(client, monkeypatch):
    import app.services.provisioning as provisioning
    monkeypatch.setattr(provisioning, "provision",
                        lambda sid: pytest.fail("provision() не должен вызываться без гейта"))
    monkeypatch.setattr(settings, "PANEL_USER", "")
    monkeypatch.setattr(settings, "PANEL_PASS", "")
    assert client.post("/api/sites/1/provision").status_code == 403


def test_api_provision_proceeds_with_auth_and_maps_errors(client, monkeypatch):
    import app.services.provisioning as provisioning
    monkeypatch.setattr(settings, "PANEL_USER", "u")
    monkeypatch.setattr(settings, "PANEL_PASS", "p")
    monkeypatch.setattr(provisioning, "provision", lambda sid: {"status": "provisioned"})
    assert client.post("/api/sites/1/provision", auth=("u", "p")).json() == {"status": "provisioned"}

    def boom(sid):
        raise RuntimeError("Cloudflare 403 POST /zones: 9109:denied")
    monkeypatch.setattr(provisioning, "provision", boom)
    r = client.post("/api/sites/1/provision", auth=("u", "p"))
    assert r.status_code == 502 and "9109" in r.json()["detail"]      # не голый 500

    def nf(sid):
        raise ValueError("site 1 not found")
    monkeypatch.setattr(provisioning, "provision", nf)
    assert client.post("/api/sites/1/provision", auth=("u", "p")).status_code == 404


def test_api_generate_and_publish_refusals_are_409(client, llm, no_panel_writes):
    sid = _site("provisioning", pages=("edited",))
    assert client.post(f"/api/sites/{sid}/generate").status_code == 409
    r = client.post(f"/api/sites/{sid}/publish")
    assert r.status_code == 409 and "provision" in r.json()["detail"]


def test_panel_generate_shows_refusal(client, llm):
    sid = _site("provisioning")
    r = client.post(f"/sites/{sid}/generate", follow_redirects=False)
    from urllib.parse import unquote
    assert r.status_code == 303 and "сначала provision" in unquote(r.headers["location"])


def test_card_disables_generate_until_provisioned(client):
    sid = _site("provisioning")
    html = client.get(f"/sites/{sid}").text
    assert "сначала шаг 3: провижн" in html
