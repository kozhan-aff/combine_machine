"""Масштаб панели (F8-12): пагинация реестра и инбокса, gzip, индексы домена."""
import re

import app.db as db
from app.api import panel
from app.models.domain import Domain

CHECKED = {"errors": [], "deep_checked": True}


def _bulk(n, **kw):
    with db.SessionLocal() as s:
        s.add_all(Domain(domain=f"d{i:04d}.com", score=1 - i / 10000, **kw) for i in range(n))
        s.commit()


def _names(html):
    return re.findall(r"d\d{4}\.com", html)


def test_pool_pages_reach_rows_beyond_the_first_page(client):
    """Раньше строки за топ-limit были недостижимы вовсе; страницы не пересекаются, ничего не теряют."""
    _bulk(25, status="discovered")
    p1 = client.get("/domains/pool?limit=10&page=1&status=discovered").text
    p3 = client.get("/domains/pool?limit=10&page=3&status=discovered").text
    assert sorted(set(_names(p1))) == [f"d{i:04d}.com" for i in range(10)]
    assert sorted(set(_names(p3))) == [f"d{i:04d}.com" for i in range(20, 25)]   # хвост страницы 3
    assert "страница 3 из 3" in p3 and "найдено 25" in p3
    # номер за пределом — не 500 и не пусто, а последняя страница
    assert "страница 3 из 3" in client.get("/domains/pool?limit=10&page=99&status=discovered").text


def test_pool_filters_survive_in_pager_links_and_counters_stay_total(client):
    _bulk(15, status="discovered")
    html = client.get("/domains/pool?limit=10&status=discovered&min_score=0.1").text
    assert "status=discovered&amp;min_score=0.1&amp;limit=10&amp;page=2" in html
    assert "<b>15</b>" in html          # плитка статуса считает ВСЮ таблицу, не страницу


def test_inbox_is_paged_but_counters_cover_everything(client, monkeypatch):
    monkeypatch.setattr(panel, "INBOX_PAGE", 10)
    _bulk(25, status="scored", score_breakdown=CHECKED)
    p1 = client.get("/domains").text
    p3 = client.get("/domains?page=3").text
    assert len(set(_names(p1))) == 10 and len(set(_names(p3))) == 5
    assert "25 доменов · страница 1 из 3" in p1
    assert 'class="v">25</div><div class="k">на решении' in p1     # шапка — по всему инбоксу


def test_responses_are_gzipped(client):
    _bulk(30, status="discovered")
    r = client.get("/domains/pool", headers={"accept-encoding": "gzip"})
    assert r.headers.get("content-encoding") == "gzip" and "d0000.com" in r.text


def test_domain_indexes_declared_and_migration_matches():
    names = {i.name for i in Domain.__table__.indexes}
    want = {"ix_domains_status_deadline", "ix_domains_status_score", "ix_domains_reject_reason", "ix_domains_lane"}
    assert want <= names
    import pathlib
    mig = (pathlib.Path(__file__).parents[1] / "alembic/versions/0032_domain_indexes.py").read_text()
    for n in want:
        assert n in mig
