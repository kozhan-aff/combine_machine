"""Regression tests for the M4/M5 review fixes (see fix-m45-brief). Offline SQLite +
mocked integrations, same harness as test_pipeline.py. Covers:
  (1) offer pick is deterministic & identical in generate vs publish,
  (2) an empty LLM page is skipped, not fatal to the batch,
  (3) render_html re-sanitizes the body (egress),
  (4) index check matches by HOST, not substring,
  (5) render_html honours the generation language.
"""
from types import SimpleNamespace

import app.db as db
from app.models.domain import Domain
from app.models.site import Site, Page
from app.models.offer import Offer, SiteOffer


def _add(obj):
    with db.SessionLocal() as s:
        s.add(obj)
        s.commit()
        s.refresh(obj)
        return obj.id


def _make_site(domain="review.ru", status="content"):
    did = _add(Domain(domain=domain, source="backorder", status="approved"))
    return _add(Site(domain_id=did, status=status, doc_root=f"/www/wwwroot/{domain}"))


# ── (1) offer sync: content and publish must pick the SAME brand ───────────────
def test_offer_pick_is_consistent_between_generate_and_publish(monkeypatch):
    from app.services.content import generate_site
    from app.services.publish import _pick_offer

    site_id = _make_site()
    # NordVPN inserted first -> lower id -> both order_by(Offer.id) picks must land on it
    nord = _add(Offer(brand="NordVPN", affiliate_link="https://ex.com/nord", active=True))
    surf = _add(Offer(brand="Surfshark", affiliate_link="https://ex.com/surf", active=True))
    for oid in (nord, surf):
        _add(SiteOffer(site_id=site_id, offer_id=oid))

    # echo the prompt (which carries "Бренд: <brand>") into the page body
    monkeypatch.setattr("app.integrations.llm.LlmClient.complete",
                        lambda self, system, prompt, **kw: f"<p>{prompt}</p>")
    assert generate_site(site_id) == 3

    with db.SessionLocal() as s:
        body = s.query(Page).filter(Page.site_id == site_id).first().body
        picked = _pick_offer(s, site_id).brand
    assert "NordVPN" in body and "Surfshark" not in body    # content written about NordVPN
    assert picked == "NordVPN"                               # link will go to NordVPN too


# ── (2) empty LLM output for one page must not abort the whole batch ───────────
def test_empty_llm_page_is_skipped_not_fatal(monkeypatch):
    from app.services.content import generate_site

    site_id = _make_site(domain="empty.ru")
    oid = _add(Offer(brand="NordVPN", affiliate_link="https://ex.com/nord", active=True))
    _add(SiteOffer(site_id=site_id, offer_id=oid))

    # the "/vs" comparison page comes back empty; the other two are fine
    monkeypatch.setattr(
        "app.integrations.llm.LlmClient.complete",
        lambda self, system, prompt, **kw: "" if "против конкурентов" in prompt else "<h2>ok</h2><p>t</p>")

    created = generate_site(site_id)          # no exception, partial batch commits
    assert created == 2
    with db.SessionLocal() as s:
        paths = {p.url_path for p in s.query(Page).filter(Page.site_id == site_id).all()}
    assert paths == {"/", "/setup"}           # the empty "/vs" page was skipped


def test_llm_complete_returns_empty_on_null_or_missing_content(monkeypatch):
    from app.integrations.llm import LlmClient

    def _resp(payload):
        return SimpleNamespace(json=lambda: payload)

    llm = LlmClient()
    monkeypatch.setattr(llm, "request",
                        lambda *a, **k: _resp({"choices": [{"message": {"content": None}}]}))
    assert llm.complete("s", "p") == ""       # content: null -> "" (no TypeError)
    monkeypatch.setattr(llm, "request", lambda *a, **k: _resp({}))
    assert llm.complete("s", "p") == ""       # no choices -> "" (no KeyError)


# ── (3) render_html re-sanitizes the body on egress ───────────────────────────
def test_render_html_sanitizes_hostile_body():
    from app.services.content import render_html
    page = SimpleNamespace(title="T", body="<h2>ok</h2><script>alert(1)</script>")
    out = render_html(page)
    assert "<script" not in out.lower() and "alert" not in out
    assert "<h2>ok</h2>" in out


# ── (5) render_html honours the generation language ───────────────────────────
def test_render_html_uses_lang():
    from app.services.content import render_html
    page = SimpleNamespace(title="T", body="<p>x</p>")
    assert "<html lang='en'>" in render_html(page, None, lang="en")
    assert "<html lang='ru'>" in render_html(page, None)          # default
    assert "<html lang='ru'>" in render_html(page, None, lang="")  # empty -> default


# ── (4) index check matches by host, not substring ────────────────────────────
def test_host_matches_rejects_substring_domain():
    from app.integrations.searxng import host_matches
    assert host_matches("https://mydomain.ru/", "mydomain.ru")
    assert host_matches("https://www.mydomain.ru/x", "mydomain.ru")   # subdomain ok
    assert not host_matches("https://notmydomain.ru.evil.com/", "mydomain.ru")
    assert not host_matches("https://evil.com/?u=mydomain.ru", "mydomain.ru")
    assert not host_matches(None, "mydomain.ru")


def test_check_index_matches_by_host(monkeypatch):
    from app.services.publish import check_index

    site_id = _make_site(domain="mydomain.ru", status="published")
    _add(Page(site_id=site_id, url_path="/", title="home", status="published", body="<p>x</p>"))

    # мок отдаёт ответ SearXNG целиком (results + unresponsive_engines) — check_index читает
    # здоровье движков из того же ответа, что и результаты. Пустой список «мёртвых» = движки
    # ответили, значит пустая выдача — законное «нет», а не «не знаю» (см. test_index_truth.py).
    def _serp(results):
        monkeypatch.setattr("app.integrations.searxng.SearxngClient.search_full",
                            lambda self, q, **kw: {"results": results, "unresponsive_engines": []})

    # a foreign URL that merely CONTAINS the domain as a substring must NOT count as indexed
    _serp([{"url": "https://notmydomain.ru.evil.com/"}])
    assert check_index(site_id)["pages"]["/"] == "not_indexed"

    # the real host does count
    _serp([{"url": "https://mydomain.ru/"}])
    assert check_index(site_id)["pages"]["/"] == "indexed"
