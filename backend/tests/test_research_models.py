"""Миграция 0036: досье, макет, блоки страницы, тумблеры автопилота (план А, спека 2026-10-10)."""
import app.db as db
from app.models.research import SiteResearch, SiteTheme
from app.models.site import Page, Site
from app.models.domain import Domain
from app.services.autonomy import get_autonomy, update_autonomy


def _site() -> int:
    with db.SessionLocal() as s:
        d = Domain(domain="r.com", source="list", status="purchased")
        s.add(d); s.commit()
        site = Site(domain_id=d.id, status="content", doc_root="/www/wwwroot/r.com")
        s.add(site); s.commit()
        return site.id


def test_research_and_theme_rows_roundtrip():
    sid = _site()
    with db.SessionLocal() as s:
        s.add(SiteResearch(site_id=sid, kind="review", query="x обзор", rank=1, url="https://a/1",
                           final_url="https://a/1", domain="a", words=320, headings=[["h2", "Цена"]],
                           tables=[], faq=[{"q": "Сколько?", "a": "5"}], numbers=[{"value": "5", "ctx": "5 $ в месяц"}],
                           css_tokens={"fonts": ["Inter"]}, text="пять долларов", note=None))
        s.add(SiteTheme(site_id=sid, version=1, status="ok", theme={"name": "x"}, layout_html="<html>{{content}}</html>",
                        css=".a{}", brief="b", model="sonnet", screenshots={}))
        s.commit()
        r = s.query(SiteResearch).filter_by(site_id=sid).one()
        assert r.faq[0]["q"] == "Сколько?" and r.css_tokens["fonts"] == ["Inter"] and r.fetched_at is not None
        t = s.query(SiteTheme).filter_by(site_id=sid).one()
        assert t.version == 1 and t.created_at is not None


def test_page_blocks_default_and_stale_flag():
    sid = _site()
    with db.SessionLocal() as s:
        p = Page(site_id=sid, url_path="/", title="t", body="<p>x</p>")
        s.add(p); s.commit()
        assert p.blocks is None and p.blocks_stale is False
        p.blocks = {"sections": []}; p.blocks_stale = True; s.commit()
        assert s.get(Page, p.id).blocks == {"sections": []}


def test_autonomy_has_new_toggles_with_safe_defaults():
    a = get_autonomy()
    assert a["auto_research"] is False and a["auto_design"] is False and a["auto_edit"] is False
    assert a["cap_research"] == 5 and a["cap_design"] == 3
    assert update_autonomy(auto_edit=True, cap_research=999)["cap_research"] == 500   # кламп
