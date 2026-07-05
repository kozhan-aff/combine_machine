"""End-to-end pipeline logic on SQLite with mocked integrations.

Proves the two HARD GATES from PLAN §2 hold in the real code paths:
  1. Editorial gate — publish deploys ONLY 'edited' pages, never 'draft'.
  2. Purchase is a human action (status flips only via the explicit endpoint).
Plus discovery idempotency and scoring persistence (JSONB round-trip on SQLite).
"""
import app.db as db
from app.models.domain import Domain


def _add(obj):
    with db.SessionLocal() as s:
        s.add(obj)
        s.commit()
        s.refresh(obj)
        return obj.id


def test_discovery_upsert_idempotent(monkeypatch):
    from app.services import discovery
    rows = [
        {"domainname": "Clean-Drop.com", "links": "12"},
        {"domainname": "second.ru", "links": 3},
        {"domainname": "bad_underscore.ru", "links": 5},  # junk char -> skipped
    ]
    monkeypatch.setattr("app.integrations.backorder.BackorderClient.list_dropping",
                        lambda self, min_links=1: rows)
    assert discovery.run_discovery() == 2   # 2 valid, 1 junk dropped
    assert discovery.run_discovery() == 0   # re-run inserts nothing (idempotent)


def test_scoring_persists_and_jsonb_roundtrips(monkeypatch):
    from app.services import scoring
    did = _add(Domain(domain="oldclean.com", source="backorder",
                      referring_domains=30, status="discovered"))
    monkeypatch.setattr(scoring, "_gather_signals", lambda domain: {
        "wayback_checked": True, "prior_flags": {}, "age_years": 10.0,
        "indexed_echo": True, "rkn_listed": False, "blacklisted": False, "errors": []})
    out = scoring.score_domain(did)
    assert out["status"] in ("approved", "scored")
    with db.SessionLocal() as s:
        d = s.get(Domain, did)
        assert d.score is not None and d.status == out["status"]
        assert isinstance(d.score_breakdown, dict)      # JSONB dict survived the round-trip
        assert d.prior_flags == {}


def test_scoring_hard_reject_on_rkn(monkeypatch):
    from app.services import scoring
    did = _add(Domain(domain="blocked.ru", source="backorder", status="discovered"))
    monkeypatch.setattr(scoring, "_gather_signals", lambda domain: {
        "wayback_checked": True, "prior_flags": {}, "rkn_listed": True, "errors": []})
    out = scoring.score_domain(did)
    assert out["status"] == "rejected" and out["score"] == 0.0
    with db.SessionLocal() as s:
        assert s.get(Domain, did).clean is False


def test_panel_actions(client, monkeypatch):
    did = _add(Domain(domain="curate-me.ru", source="backorder", status="scored"))
    # manual curation: valid transition sticks (303 -> redirect back to /)
    r = client.post(f"/domains/{did}/set-status", data={"status": "approved"},
                    follow_redirects=False)
    assert r.status_code == 303
    with db.SessionLocal() as s:
        assert s.get(Domain, did).status == "approved"
    # guard: a status outside the manual allowlist is ignored (no arbitrary transitions)
    client.post(f"/domains/{did}/set-status", data={"status": "live"}, follow_redirects=False)
    with db.SessionLocal() as s:
        assert s.get(Domain, did).status == "approved"   # unchanged
    # Score button triggers the service (mock it — no live Wayback in the test)
    called = {}
    monkeypatch.setattr("app.services.scoring.score_pending",
                        lambda limit=5: called.setdefault("n", limit))
    assert client.post("/run/score", data={"n": "7"}, follow_redirects=False).status_code == 303
    assert called["n"] == 7


def test_edit_gate_and_publish(client, monkeypatch):
    # offer (the machine's input)
    offer_id = client.post("/offers", json={
        "brand": "NordVPN", "affiliate_link": "https://ex.com/aff", "promo_code": "SAVE10"
    }).json()["id"]

    # domain -> HUMAN purchase -> site
    did = _add(Domain(domain="review-site.com", source="backorder", status="approved"))
    assert client.post(f"/domains/{did}/purchase").json()["status"] == "purchased"
    site_id = client.post(f"/domains/{did}/site").json()["site_id"]

    # M4 generate (mock LiteLLM) -> 3 DRAFT pages
    monkeypatch.setattr("app.integrations.llm.LlmClient.complete",
                        lambda self, system, prompt, **kw: "<h2>Draft</h2><p>text</p>")
    assert client.post(f"/sites/{site_id}/generate").json()["created"] == 3

    # GATE 1: nothing is 'edited' yet -> publish refuses (no auto-publish of AI drafts)
    assert client.post(f"/sites/{site_id}/publish").json()["status"] == "no_edited_pages"

    # human edits exactly ONE page (the '/' review)
    pages = client.get(f"/sites/{site_id}/pages").json()
    home = next(p for p in pages if p["url_path"] == "/")
    assert client.post(f"/pages/{home['id']}/edit",
                       json={"body": "<h2>Edited</h2>"}).json()["status"] == "edited"

    # attach offer + publish (mock the aaPanel file write)
    client.post(f"/sites/{site_id}/offer", json={"offer_id": offer_id})
    writes = []
    monkeypatch.setattr("app.integrations.aapanel.AaPanelClient.write_file",
                        lambda self, path, content: (writes.append((path, content)), {"status": True})[1])
    pub = client.post(f"/sites/{site_id}/publish").json()

    # only the edited page went out — the 2 drafts were left untouched
    assert pub["status"] == "published" and pub["pages"] == ["/"]
    assert len(writes) == 1
    path, page_html = writes[0]
    assert path.endswith("/index.html")
    assert "SAVE10" in page_html and 'rel="sponsored nofollow"' in page_html and "Раскрытие" in page_html
    states = sorted(p["status"] for p in client.get(f"/sites/{site_id}/pages").json())
    assert states == ["draft", "draft", "published"]

    # M5 index check (mock SearXNG -> no hits)
    monkeypatch.setattr("app.integrations.searxng.SearxngClient.search",
                        lambda self, q, **kw: [])
    assert client.post(f"/sites/{site_id}/check-index").json()["pages"]["/"] == "not_indexed"
