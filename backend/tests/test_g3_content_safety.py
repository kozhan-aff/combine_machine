"""G3 (аудит 2026-10-07): безопасность контента, ссылок и публикации.

S6-06/S5-08 rel партнёрских ссылок в теле; S6-16/S7-11 гейт редактуры (непустое тело, статус,
черновик отдельно от одобрения); S5-05/S7-18 HTTP-проверка публикации и постраничный исход;
S5-16/S6-07 нет оффера / мёртвый CTA; S6-13/S7-12 явная привязка оффера (Site.offer_id);
S6-07 раскрытие рядом со ссылкой на языке страницы; S7-16 ротация check_index; S7-10 live/
monitoring; S6-15 критик не судит о раскрытии; S6-11/F8-07/S7-14 генерация — джоб реестра с
коммитом по странице.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as N
from urllib.parse import unquote

import pytest
from sqlalchemy import select

import app.db as db
from app.config import settings
from app.integrations.aapanel import AaPanelClient
from app.integrations.llm import LlmClient
from app.models.domain import Domain
from app.models.offer import Offer
from app.models.site import Page, Site
from app.services import content, jobs, orchestrator as orch, publish

LONG = "<h2>Обзор</h2><p>Достаточно длинный вычитанный текст страницы для прохождения гейта.</p>"


@pytest.fixture(autouse=True)
def _panel(monkeypatch):
    monkeypatch.setattr(settings, "AAPANEL_URL", "https://127.0.0.1:8888")
    monkeypatch.setattr(settings, "AAPANEL_CA_BUNDLE", "")
    monkeypatch.setattr(settings, "AAPANEL_API_KEY", "testsk")


def _offer(brand="NordVPN", link="https://ex.com/aff", active=True, promo=None, lang="en") -> int:
    with db.SessionLocal() as s:
        o = Offer(brand=brand, affiliate_link=link, active=active, promo_code=promo, language=lang)
        s.add(o)
        s.commit()
        return o.id


def _site(status="content", offer_id=None, domain="g3.com", dstatus="purchased") -> int:
    with db.SessionLocal() as s:
        d = Domain(domain=domain, source="backorder", status=dstatus)
        s.add(d)
        s.commit()
        site = Site(domain_id=d.id, status=status, doc_root=f"/www/wwwroot/{domain}",
                    aapanel_site_name=domain, offer_id=offer_id)
        s.add(site)
        s.commit()
        return site.id


def _page(site_id, path="/", status="draft", body=LONG, offer_id=None, lang="en", **kw) -> int:
    with db.SessionLocal() as s:
        p = Page(site_id=site_id, url_path=path, title="t", status=status, body=body,
                 offer_id=offer_id, lang=lang, **kw)
        s.add(p)
        s.commit()
        return p.id


def _status(pid):
    with db.SessionLocal() as s:
        return s.get(Page, pid).status


# ── S6-06 / S5-08: rel ссылок в теле ──────────────────────────────────────────

def test_body_links_get_sponsored_nofollow_and_foreign_rel_is_replaced():
    out = content._sanitize('<p><a href="https://nordvpn.com/?aff=1">Nord</a> '
                            '<a href="https://x.y" rel="dofollow">x</a> '
                            '<a href="https://z.y" rel="sponsored">z</a></p>')
    assert out.count('rel="sponsored nofollow noopener"') == 3
    assert "dofollow" not in out and "noreferrer" not in out


def test_cta_and_body_link_both_sponsored_in_rendered_page():
    off = N(brand="Nord", affiliate_link="https://ex.com/aff", promo_code=None, active=True)
    html_ = content.render_html(N(title="t", body='<p><a href="https://p.com">p</a></p>'), off, lang="en")
    assert html_.count('rel="sponsored nofollow noopener"') == 2


def test_safe_url_requires_host():
    assert not content.is_safe_url("https:")
    assert not content.is_safe_url("https:///x")
    assert content.is_safe_url("https://ex.com/a")


# ── S6-16 / S7-11: гейт редактуры ─────────────────────────────────────────────

def test_mark_edited_refuses_empty_and_short_body():
    pid = _page(_site(), body="")
    for bad in ("", "<p> </p>", "<script>alert(1)</script><p>коротко</p>"):
        with pytest.raises(ValueError, match="симв"):
            content.mark_edited(pid, bad)
    with pytest.raises(ValueError):
        content.mark_edited(pid)               # body=None: лежащий пустой текст тоже не одобрить
    assert _status(pid) == "draft"


def test_mark_edited_refuses_published_page_and_keeps_it_published():
    pid = _page(_site(), status="published")
    with pytest.raises(ValueError, match="published"):
        content.mark_edited(pid, LONG)
    assert _status(pid) == "published"


def test_mark_edited_without_body_approves_existing_text():
    pid = _page(_site())
    assert content.mark_edited(pid)["status"] == "edited"


def test_save_draft_never_approves_and_demotes_edited():
    pid = _page(_site(), status="edited")
    assert content.save_draft(pid, "<p>правка в процессе</p>")["status"] == "draft"
    assert _status(pid) == "draft"             # непросмотренная правка не уедет под старой отметкой
    with db.SessionLocal() as s:
        s.get(Page, pid).status = "published"
        s.commit()
    with pytest.raises(ValueError):
        content.save_draft(pid, "x")


def test_panel_save_with_lost_or_empty_body_is_refused_and_draft_route_does_not_approve(client):
    pid = _page(_site())
    r = client.post(f"/pages/{pid}/draft", data={"body": "<p>черновик</p>"}, follow_redirects=False)
    assert r.status_code == 303 and _status(pid) == "draft"
    # потерянное поле / очищенная textarea не одобряют страницу и не затирают лежащий текст
    with db.SessionLocal() as s:
        s.get(Page, pid).body = LONG
        s.commit()
    for data in ({}, {"body": ""}):
        r = client.post(f"/pages/{pid}/save", data=data, follow_redirects=False)
        assert "симв" in unquote(r.headers["location"]) and _status(pid) == "draft"
    with db.SessionLocal() as s:
        assert "Достаточно длинный" in s.get(Page, pid).body
    r = client.post(f"/pages/{pid}/save", data={"body": LONG}, follow_redirects=False)
    assert _status(pid) == "edited"


def test_sweep_never_makes_pages_edited():
    """Контроль инварианта: ни одна автостадия не имеет пути в edited."""
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(orch))
    names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | \
            {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert not names & {"mark_edited", "save_draft"}      # у автопилота нет пути в edited


# ── S5-05 / S7-18 / S5-16 / S6-07: публикация ─────────────────────────────────

def _fake_panel(monkeypatch, fail_paths=()):
    writes = {}

    def _post(self, path, data=None):
        if "CreateFile" in path:
            return {"status": True}
        if "SaveFileBody" in path:
            if any(data["path"].endswith(f) for f in fail_paths):
                return {"status": False, "msg": "Configuration file not exist"}
            writes[data["path"]] = data["data"]
            return {"status": True}
        raise AssertionError(path)  # pragma: no cover
    monkeypatch.setattr(AaPanelClient, "_post", _post)
    return writes


def _live(monkeypatch, writes, mode="ok"):
    """Мок siteprobe.fetch: 'ok' отдаёт записанный документ; 'stub' — заглушку панели; 502; 'down'."""
    monkeypatch.setattr(settings, "PUBLISH_VERIFY", True)
    urls = []

    def fetch(url, timeout=15.0):
        urls.append(url)
        if mode == "down":
            raise ConnectionError("dns")
        if mode == "502":
            return 502, "bad gateway"
        if mode == "stub":
            return 200, "<html>Site is created successfully!</html>"
        path = url.split("//", 1)[1].split("/", 1)[1].split("?")[0]
        for k, v in writes.items():
            if k.endswith(("/" + path.strip("/") + "/index.html") if path.strip("/") else "/index.html"):
                return 200, v
        return 404, ""
    monkeypatch.setattr("app.integrations.siteprobe.fetch", fetch)
    return urls


def test_publish_verifies_live_and_marks_domain_live(monkeypatch):
    oid = _offer()
    sid = _site(offer_id=oid)
    pid = _page(sid, status="edited", offer_id=oid)
    writes = _fake_panel(monkeypatch)
    urls = _live(monkeypatch, writes)
    out = publish.publish_site(sid)
    assert out["status"] == "published" and out["pages"] == ["/"] and urls
    assert urls[0].startswith("https://g3.com/?_pv=")
    with db.SessionLocal() as s:
        assert s.get(Page, pid).status == "published"
        assert s.get(Domain, s.get(Site, sid).domain_id).status == "live"     # S7-10
        assert s.get(Site, sid).status == "published"


@pytest.mark.parametrize("mode", ["stub", "502", "down"])
def test_publish_unverified_page_stays_edited_and_site_not_published(monkeypatch, mode):
    oid = _offer()
    sid = _site(offer_id=oid)
    pid = _page(sid, status="edited", offer_id=oid)
    writes = _fake_panel(monkeypatch)
    _live(monkeypatch, writes, mode)
    out = publish.publish_site(sid)
    assert out["status"] == "failed" and out["written"] == ["/"] and "/" in out["unverified"]
    assert out["pages"] == []
    with db.SessionLocal() as s:
        assert s.get(Page, pid).status == "edited"
        assert s.get(Site, sid).status == "content"
        assert s.get(Domain, s.get(Site, sid).domain_id).status == "purchased"


def test_publish_without_offer_is_blocked_before_writing_and_sweep_surfaces_it(monkeypatch):
    sid = _site()
    pid = _page(sid, status="edited")
    writes = _fake_panel(monkeypatch)
    out = publish.publish_site(sid)
    assert out["status"] == "failed" and "нет оффера" in out["failed"]["/"]
    assert not writes and _status(pid) == "edited"                  # в интернет не ушла
    sid2 = _site(domain="g3b.com")
    _page(sid2, status="edited")
    done, errs = orch._stage_publish(10)
    assert any("нет оффера" in e for e in errs)


def test_publish_blocks_inactive_offer_without_reserve_url_but_allows_with_it(monkeypatch):
    oid = _offer(active=False)
    sid = _site(offer_id=oid)
    pid = _page(sid, status="edited", offer_id=oid)
    writes = _fake_panel(monkeypatch)
    out = publish.publish_site(sid)
    assert out["status"] == "failed" and "выключен" in out["failed"]["/"]
    assert not writes and _status(pid) == "edited"
    from app.models.offer import OfferSettings
    with db.SessionLocal() as s:
        s.add(OfferSettings(id=1, reserve_offer_url="https://ex.com/reserve"))
        s.commit()
    assert publish.publish_site(sid)["status"] == "published"


def test_publish_blocks_page_whose_cta_would_vanish(monkeypatch):
    oid = _offer(link="javascript:alert(1)")
    sid = _site(offer_id=oid)
    pid = _page(sid, status="edited", offer_id=oid)
    writes = _fake_panel(monkeypatch)
    out = publish.publish_site(sid)
    assert out["status"] == "failed" and "CTA" in out["failed"]["/"] and not writes
    assert _status(pid) == "edited"


def test_legacy_page_does_not_fall_back_to_foreign_global_offer(monkeypatch):
    _offer(brand="Чужой")                      # активный оффер портфеля, к сайту НЕ привязан
    sid = _site()
    _page(sid, status="edited", offer_id=None, lang=None)
    writes = _fake_panel(monkeypatch)
    out = publish.publish_site(sid)
    assert out["failed"] and not writes


# ── S6-07: раскрытие в языке страницы и рядом со ссылкой ──────────────────────

def test_disclosure_is_in_offer_block_and_in_page_language():
    off = N(brand="Nord", affiliate_link="https://ex.com/a", promo_code="X1", active=True)
    en = content.render_html(N(title="t", body="<p>x</p>"), off, lang="en")
    block = en[en.index('<aside class="offer">'):en.index("</aside>")]
    assert "affiliate links" in block and "Go to Nord" in block and "Promo code" in block
    assert "Раскрытие" not in en
    assert "affiliate links" in en[:en.index("<article>")]            # и над текстом
    ru = content.render_html(N(title="t", body="<p>x</p>"), off, lang="ru")
    assert "Раскрытие:" in ru[ru.index('<aside'):ru.index("</aside>")] and "Перейти к Nord" in ru
    de = content.render_html(N(title="t", body="<p>x</p>"), off, lang="de-DE")
    assert "affiliate links" in de                                    # язык без словаря -> en, не ru


# ── S6-13 / S7-12: оффер сайта — явно ─────────────────────────────────────────

def test_generate_uses_site_offer_not_earliest_active(monkeypatch):
    _offer(brand="Первый")                                  # id меньше, но сайту не привязан
    mine = _offer(brand="Мой")
    sid = _site(offer_id=mine)
    monkeypatch.setattr(LlmClient, "complete", lambda self, sy, pr, **kw: f"<p>{pr}</p>")
    assert content.generate_site(sid) == 3
    with db.SessionLocal() as s:
        pages = s.execute(select(Page).where(Page.site_id == sid)).scalars().all()
        assert all("Мой" in p.body and "Первый" not in p.body and p.offer_id == mine for p in pages)


def test_generate_refuses_without_bound_offer_even_if_portfolio_has_one(monkeypatch):
    _offer()
    sid = _site()
    monkeypatch.setattr(LlmClient, "complete", lambda self, sy, pr, **kw: "<p>x</p>")
    with pytest.raises(ValueError, match="оффер не привязан"):
        content.generate_site(sid)
    with db.SessionLocal() as s:
        assert s.query(Page).count() == 0


def test_panel_generate_refusal_and_attach_sets_site_offer(client, monkeypatch):
    oid = _offer()
    sid = _site()
    r = client.post(f"/sites/{sid}/generate", data={"lang": "en"}, follow_redirects=False)
    assert "Оффер не привязан" in unquote(r.headers["location"])
    client.post(f"/sites/{sid}/attach-offer", data={"offer_id": oid}, follow_redirects=False)
    with db.SessionLocal() as s:
        assert s.get(Site, sid).offer_id == oid
    ran = []
    monkeypatch.setattr(jobs, "spawn", lambda name, fn: ran.append(name) or True)
    r = client.post(f"/sites/{sid}/generate", data={"lang": "en"}, follow_redirects=False)
    assert ran == ["generate"] and "msg=" in r.headers["location"]


def test_generate_stage_reports_site_without_offer():
    sid = _site()
    done, errs = orch._stage_generate(5)
    assert done == 0 and errs == [f"site#{sid}: оффер не привязан — генерация пропущена"]


# ── F8-07 / S6-11 / S7-14: генерация — джоб, коммит по странице ───────────────

def test_generation_commits_each_page_and_reports_progress(monkeypatch):
    oid = _offer()
    sid = _site(offer_id=oid)
    calls = []

    def complete(self, sy, pr, **kw):
        calls.append(1)
        if len(calls) == 3:
            raise RuntimeError("LLM упал на 3-й странице")
        return "<p>ok</p>"
    monkeypatch.setattr(LlmClient, "complete", complete)
    with pytest.raises(RuntimeError):
        content.generate_site(sid)
    with db.SessionLocal() as s:
        assert s.query(Page).filter_by(site_id=sid).count() == 2     # оплаченные токены не потеряны
    last = jobs.last("generate")
    assert last["status"] == "failed"

    monkeypatch.setattr(LlmClient, "complete", lambda self, sy, pr, **kw: "<p>ok</p>")
    assert content.generate_site(sid) == 1                          # дозаполнение
    last = jobs.last("generate")
    assert last["status"] == "done" and last["done"] == last["total"] == 1


def test_generation_is_cancellable_between_pages(monkeypatch):
    oid = _offer()
    sid = _site(offer_id=oid)

    def complete(self, sy, pr, **kw):
        jobs.request_cancel("generate")
        return "<p>ok</p>"
    monkeypatch.setattr(LlmClient, "complete", complete)
    content.generate_site(sid)
    with db.SessionLocal() as s:
        assert s.query(Page).filter_by(site_id=sid).count() == 1
    assert jobs.last("generate")["status"] == "cancelled"


# ── S7-16 / S7-10: ротация check_index ────────────────────────────────────────

def _published(domain, checked_ago=None, idx="not_indexed"):
    sid = _site(status="published", domain=domain)
    ck = None if checked_ago is None else datetime.now(timezone.utc) - checked_ago
    _page(sid, status="published", index_status=idx, index_checked_at=ck)
    return sid


def test_check_index_rotates_oldest_first_and_respects_cooldown(monkeypatch):
    never = _published("a.com")
    old = _published("b.com", timedelta(hours=7))
    fresh = _published("c.com", timedelta(minutes=30))              # cooldown не вышел
    indexed = _published("d.com", timedelta(days=1), idx="indexed")  # indexed — раз в 3 дня
    seen = []
    monkeypatch.setattr("app.services.publish.check_index",
                        lambda sid, only_due=False: seen.append((sid, only_due)) or {"pages": {"/": "not_indexed"}})
    done, errs, _ = orch._stage_check_index(1)
    assert seen == [(never, True)]                                  # cap=1: самый давний (NULL) первым
    seen.clear()
    orch._stage_check_index(10)
    assert [s for s, _ in seen] == [never, old]                     # fresh и indexed пропущены
    assert fresh not in [s for s, _ in seen] and indexed not in [s for s, _ in seen]


def test_check_index_rotation_takes_longest_unchecked_not_lowest_id(monkeypatch):
    # id1 проверен недавно (но просрочен), id2 — давно: при cap=1 обязан идти id2, не меньший id
    recent = _published("r.com", timedelta(hours=7))
    ancient = _published("s.com", timedelta(days=5))
    assert recent < ancient
    seen = []
    monkeypatch.setattr("app.services.publish.check_index",
                        lambda sid, only_due=False: seen.append(sid) or {"pages": {"/": "not_indexed"}})
    orch._stage_check_index(1)
    assert seen == [ancient]


def test_check_index_stage_stops_when_engines_are_down(monkeypatch):
    _published("a.com")
    _published("b.com")
    seen = []
    monkeypatch.setattr("app.services.publish.check_index",
                        lambda sid, only_due=False: seen.append(sid) or {"pages": {"/": "unknown"}, "all_unknown": True})
    done, errs, extra = orch._stage_check_index(10)
    assert len(seen) == 1 and extra == {"index_unknown": 1}         # не долбим мёртвые движки


def test_check_index_only_due_and_first_indexed_page_starts_monitoring(monkeypatch):
    sid = _published("m.com", timedelta(hours=7))
    _page(sid, "/fresh", status="published", index_checked_at=datetime.now(timezone.utc))
    asked = []
    monkeypatch.setattr("app.integrations.searxng.SearxngClient.search_full",
                        lambda self, q, **kw: asked.append(q) or
                        {"results": [{"url": "https://m.com/"}], "unresponsive_engines": []})
    out = publish.check_index(sid, only_due=True)
    assert list(out["pages"]) == ["/"] and len(asked) == 1
    with db.SessionLocal() as s:
        assert s.get(Site, sid).status == "monitoring"


# ── S6-15: критик не судит о раскрытии ────────────────────────────────────────

def test_critic_prompt_has_no_disclosure_criterion_and_drops_false_issue(monkeypatch):
    from app.services import content_critic
    assert "disclosure" not in content_critic._SYSTEM_PROMPT.lower().replace("не оценивай", "")
    pid = _page(_site())
    monkeypatch.setattr(LlmClient, "complete", lambda self, sy, pr, **kw:
                        "БАЛЛ: 70\n- Отсутствует пометка о партнёрской ссылке (disclosure)\n- мало фактов")
    out = content_critic.critique_page(pid)
    assert out["issues"] == ["мало фактов"]


def test_sweep_publish_blocked_site_with_lower_id_does_not_starve_queue(monkeypatch):
    sid_bad = _site(domain="bad.com")                       # id меньше, оффера нет -> заблокирован
    _page(sid_bad, status="edited")
    oid = _offer()
    sid_ok = _site(domain="good.com", offer_id=oid)
    pid_ok = _page(sid_ok, status="edited", offer_id=oid)
    _fake_panel(monkeypatch)
    _live(monkeypatch, [], "ok") if False else None
    for _ in range(3):
        done, errs = orch._stage_publish(1)
    assert _status(pid_ok) != "edited"                      # нормальный сайт дошёл до публикации
    assert any("нет оффера" in e for e in errs)             # причина блокировки по-прежнему видна
