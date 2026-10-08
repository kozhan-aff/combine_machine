"""Списки чистоты доменов (UT1 / blocklistproject, W2c-ut1): разбор, атомарное обновление, условный GET,
мягкий сигнал list_hits в волне risk, жёсткий отказ только по настройке, закрытый пакет одобрения.
Герметично: никакой сети — архивы собираются в памяти, транспорт — httpx.MockTransport."""
import io
import os
import tarfile
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select, func

import app.db as db
from app.integrations.base import NotModified
from app.integrations.domainlists import DomainListClient
from app.models.domain import Domain
from app.models.domain_list import DomainList
from app.services import domain_lists, scoring
from app.services import settings as st

ALLOW = ["com", "net", "co.uk"]


def _tar(tmp_path, lines, member="gambling/domains"):
    p = tmp_path / "x.tar.gz"
    data = "\n".join(lines).encode()
    with tarfile.open(p, "w:gz") as tf:
        ti = tarfile.TarInfo(member)
        ti.size = len(data)
        tf.addfile(ti, io.BytesIO(data))
    return str(p)


def _fill(source, cat, domains):
    with db.SessionLocal() as s:
        domain_lists.load_list(s, source, cat, domains)
        s.commit()


def _count(**kw):
    with db.SessionLocal() as s:
        q = select(func.count()).select_from(DomainList)
        for k, v in kw.items():
            q = q.where(getattr(DomainList, k) == v)
        return s.scalar(q)


# ------------------------------------------------------------------ разбор
def test_normalize_hosts_format_comments_subdomains_and_zones():
    n = domain_lists.normalize
    assert n("Casino-X.COM", ALLOW) == "casino-x.com"
    assert n("0.0.0.0 bet.net", ALLOW) == "bet.net"
    assert n("www.bet.com", ALLOW) == "bet.com"
    assert n("# comment", ALLOW) is None and n("", ALLOW) is None
    assert n("x.com # tail", ALLOW) == "x.com"
    assert n("sub.bet.com", ALLOW) is None              # поддомен — не регистрируемое имя
    assert n("bet.ru", ALLOW) is None                   # зона вне белого списка
    assert n("bet.co.uk", ALLOW) == "bet.co.uk"
    assert n("bet.com/path", ALLOW) is None             # URL с путём — не домен


def test_iter_ut1_reads_domains_member_and_rejects_foreign_archive(tmp_path):
    got = [x.strip() for x in domain_lists.iter_ut1(_tar(tmp_path, ["a.com", "b.net"]))]
    assert got == ["a.com", "b.net"]
    with pytest.raises(ValueError, match="нет файла domains"):
        list(domain_lists.iter_ut1(_tar(tmp_path, ["a.com"], member="gambling/urls")))


def test_list_url_uses_configurable_base(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "DOMAIN_LISTS_UT1_URL", "https://mirror.example/ut1/")
    assert domain_lists.list_url("ut1", "adult") == "https://mirror.example/ut1/adult.tar.gz"
    assert domain_lists.list_url("blp", "porn").endswith("/porn-nl.txt")


# ------------------------------------------------------------------ обновление таблицы
def test_load_list_diffs_and_is_scoped_to_source_and_category():
    _fill("ut1", "gambling", ["a.com", "b.com"])
    _fill("blp", "gambling", ["a.com"])                  # тот же домен из другого источника — отдельная строка
    _fill("ut1", "gambling", ["b.com", "c.com"])         # a.com ушёл, c.com пришёл
    with db.SessionLocal() as s:
        got = set(s.scalars(select(DomainList.domain).where(DomainList.source == "ut1")))
    assert got == {"b.com", "c.com"} and _count(source="blp") == 1


def test_load_list_refuses_empty_and_halved_list_and_keeps_old_set(monkeypatch):
    monkeypatch.setattr(domain_lists, "SHRINK_MIN", 4)
    _fill("ut1", "adult", [f"d{i}.com" for i in range(10)])
    with db.SessionLocal() as s:
        with pytest.raises(ValueError, match="пуст"):
            domain_lists.load_list(s, "ut1", "adult", [])
        with pytest.raises(ValueError, match="усох"):
            domain_lists.load_list(s, "ut1", "adult", ["d0.com", "d1.com"])
        s.rollback()
    assert _count(source="ut1", category="adult") == 10          # битая отдача базу не очистила


def test_lookup_none_when_not_loaded_and_categories_when_loaded():
    assert domain_lists.lookup(["a.com"]) is None                # «не знаем», не «чисто»
    _fill("ut1", "gambling", ["a.com"])
    _fill("blp", "phishing", ["a.com", "p.com"])
    assert domain_lists.lookup(["A.com", "clean.com", "p.com"]) == {
        "A.com": ["gambling", "phishing"], "clean.com": [], "p.com": ["phishing"]}


# ------------------------------------------------------------------ refresh + условный GET
class _FakeClient:
    """download(url, validators) -> (путь, валидаторы) | NotModified | исключение, по таблице."""
    def __init__(self, tmp_path, plan):
        self.tmp, self.plan, self.calls = tmp_path, plan, []

    def download(self, url, validators=None):
        self.calls.append((url, validators))
        what = self.plan(url)
        if what == "304":
            raise NotModified(url)
        if what == "boom":
            raise RuntimeError("500")
        path = self.tmp / f"dl{len(self.calls)}"
        if url.endswith(".tar.gz"):
            path = _tar(self.tmp, what, member="x/domains")
            return path, {"etag": "E1", "last_modified": "LM1"}
        path.write_text("\n".join(what))
        return str(path), {"etag": "E1", "last_modified": "LM1"}


ONE_LIST = [("blp", "gambling", "gambling")]


def test_refresh_loads_then_second_run_sends_validators_and_304_is_not_modified(tmp_path):
    fc = _FakeClient(tmp_path, lambda url: ["0.0.0.0 bet.com", "sub.bet.com", "x.ru", "bet2.net"])
    r = domain_lists.refresh(fc, ONE_LIST)
    assert r[0]["status"] == "updated" and r[0]["total"] == 2        # поддомен и .ru не в таблице
    assert fc.calls[0][1] is None
    fc2 = _FakeClient(tmp_path, lambda url: "304")
    r2 = domain_lists.refresh(fc2, ONE_LIST)
    assert r2[0]["status"] == "not_modified"
    assert fc2.calls[0][1]["etag"] == "E1" and fc2.calls[0][1]["last_modified"] == "LM1"
    assert _count() == 2                                              # 304 базу не тронул


def test_refresh_ignores_validators_when_zone_allowlist_changed(tmp_path):
    domain_lists.refresh(_FakeClient(tmp_path, lambda u: ["bet.com"]), ONE_LIST)
    st.update_settings(tld_allowlist=["com", "net", "xyz"])            # другой срез файла
    fc = _FakeClient(tmp_path, lambda u: ["bet.com", "z.xyz"])
    domain_lists.refresh(fc, ONE_LIST)
    assert fc.calls[0][1] is None and _count() == 2


def test_refresh_failed_list_does_not_save_validators_nor_stop_others(tmp_path):
    plan = lambda url: "boom" if "gambling" in url else ["ok.com"]      # noqa: E731
    fc = _FakeClient(tmp_path, plan)
    r = domain_lists.refresh(fc, [("blp", "gambling", "gambling"), ("blp", "porn", "adult")])
    assert [x["status"] for x in r] == ["error", "updated"]
    assert "blp:gambling" not in st.get_list_state() and "blp:adult" in st.get_list_state()
    # упавший список при следующем заходе идёт БЕЗ валидаторов — день не «потерян»
    fc2 = _FakeClient(tmp_path, lambda u: ["ok.com"])
    domain_lists.refresh(fc2, [("blp", "gambling", "gambling")])
    assert fc2.calls[0][1] is None


def test_refresh_bad_archive_is_error_and_keeps_previous_set(tmp_path):
    _fill("ut1", "gambling", ["old.com"])
    class C(_FakeClient):
        def download(self, url, validators=None):
            p = tmp_path / "bad.tar.gz"
            p.write_bytes(b"not a tar")
            return str(p), {}
    r = domain_lists.refresh(C(tmp_path, None), [("ut1", "gambling", "gambling")])
    assert r[0]["status"] == "error" and _count(source="ut1") == 1
    assert not os.path.exists(tmp_path / "bad.tar.gz")               # временный файл чистит refresh


def test_refresh_deletes_temp_file(tmp_path):
    seen = {}
    class C(_FakeClient):
        def download(self, url, validators=None):
            p, v = super().download(url, validators)
            seen["p"] = p
            return p, v
    domain_lists.refresh(C(tmp_path, lambda u: ["a.com"]), ONE_LIST)
    assert not os.path.exists(seen["p"])


# ------------------------------------------------------------------ транспорт
def _client_with(handler):
    c = DomainListClient()
    c._client = httpx.Client(transport=httpx.MockTransport(handler))
    return c


def test_download_streams_to_file_returns_validators_and_sends_conditional_headers():
    seen = {}
    def h(req):
        seen.update(req.headers)
        return httpx.Response(200, content=b"a.com\nb.com\n", headers={"ETag": '"v1"', "Last-Modified": "Mon"})
    path, v = _client_with(h).download("https://x/y.txt", {"etag": '"v0"', "last_modified": "Sun"})
    try:
        assert open(path, "rb").read() == b"a.com\nb.com\n"
    finally:
        os.unlink(path)
    assert v == {"etag": '"v1"', "last_modified": "Mon"}
    assert seen["if-none-match"] == '"v0"' and seen["if-modified-since"] == "Sun"


def test_download_304_and_5xx_raise_and_leave_no_temp_file(monkeypatch):
    import tempfile
    made = []
    real = tempfile.mkstemp
    monkeypatch.setattr(tempfile, "mkstemp", lambda **k: (lambda fd_p: (made.append(fd_p[1]), fd_p)[1])(real(**k)))
    with pytest.raises(NotModified):
        _client_with(lambda r: httpx.Response(304)).download("https://x/y")
    with pytest.raises(httpx.HTTPStatusError):
        _client_with(lambda r: httpx.Response(500)).download("https://x/y")
    assert made and not any(os.path.exists(p) for p in made)


# ------------------------------------------------------------------ волна risk
def _state(domain, alive=True):
    return scoring.FunnelState(domain_id=1, domain=domain, lane="bid", referring_domains=5,
                               acquire_deadline=None, feed_flags=None, alive=alive)


def test_wave_lists_soft_signal_does_not_reject_by_default():
    _fill("ut1", "gambling", ["bet.com"])
    a, b = _state("bet.com"), _state("clean.com")
    scoring._wave_lists([a, b], {})
    assert a.sig["list_hits"] == ["gambling"] and a.alive and a.reject_reason is None
    assert b.sig["list_hits"] == [] and b.alive


def test_wave_lists_hard_reject_only_for_hard_categories_and_only_when_enabled():
    _fill("ut1", "gambling", ["bet.com"])
    _fill("ut1", "phishing", ["phish.com"])
    on = {"hard_reject_lists": True}
    g, p = _state("bet.com"), _state("phish.com")
    scoring._wave_lists([g, p], on)
    assert g.reject_reason == "list_hit" and not g.alive
    assert p.alive and p.sig["list_hits"] == ["phishing"]            # фишинг шумный — только мягкий сигнал
    off = _state("bet.com")
    scoring._wave_lists([off], {"hard_reject_lists": False})
    assert off.alive and off.reject_reason is None


def test_wave_lists_no_signal_when_lists_not_loaded_and_skips_dead_states():
    a = _state("bet.com")
    scoring._wave_lists([a], {"hard_reject_lists": True})
    assert "list_hits" not in a.sig and a.alive                       # незнание не пишется как «чисто»
    _fill("ut1", "gambling", ["bet.com"])
    dead = _state("bet.com", alive=False)
    scoring._wave_lists([dead], {"hard_reject_lists": True})
    assert "list_hits" not in dead.sig and dead.reject_reason is None  # уже отклонённого (напр. Web Risk) не трогаем


def test_wave_lists_db_failure_is_error_not_verdict(monkeypatch):
    def boom(_):
        raise RuntimeError("db")
    monkeypatch.setattr(domain_lists, "lookup", boom)
    a = _state("bet.com")
    scoring._wave_lists([a], {"hard_reject_lists": True})
    assert a.alive and "lists:RuntimeError" in a.sig["errors"] and "list_hits" not in a.sig


# ------------------------------------------------------------------ воронка целиком
def _mk(domain):
    with db.SessionLocal() as s:
        d = Domain(domain=domain, source="dropcatch", status="discovered", referring_domains=50, lane="bid")
        s.add(d); s.commit(); s.refresh(d)
        return d.id


def _funnel_clients():
    from tests.test_funnel import _clients, _Wayback
    return _clients(datetime(2012, 1, 1, tzinfo=timezone.utc), _Wayback(age_years=9.0))


def test_funnel_persists_list_hits_in_breakdown_and_closes_bulk_but_stays_scored():
    _fill("ut1", "phishing", ["listed.com"])
    did = _mk("listed.com")
    out = scoring.score_domain(did, clients=_funnel_clients())
    assert out["status"] == "scored"                                   # мягкий сигнал не отклоняет
    with db.SessionLocal() as s:
        d = s.get(Domain, did)
    assert d.score_breakdown["list_hits"] == ["phishing"] and scoring.list_hits(d) == ["phishing"]
    assert scoring.bulk_ok(d) is False                                 # нет пакетного одобрения при попадании
    clean = _mk("clean.com")
    scoring.score_domain(clean, clients=_funnel_clients())
    with db.SessionLocal() as s:
        c = s.get(Domain, clean)
    assert c.score_breakdown["list_hits"] == [] and scoring.bulk_ok(c) is True


def test_funnel_hard_reject_saves_reason_and_evidence_and_is_dirty():
    _fill("ut1", "gambling", ["casino.com"])
    st.update_settings(hard_reject_lists=True)
    did = _mk("casino.com")
    out = scoring.score_domain(did, clients=_funnel_clients())
    assert out["status"] == "rejected" and out["reject_reason"] == "list_hit"
    with db.SessionLocal() as s:
        d = s.get(Domain, did)
    from app.services.transitions import dirty_reason
    assert d.score_breakdown["list_hits"] == ["gambling"] and dirty_reason(d) == "list_hit"


def test_rescore_with_empty_lists_does_not_wash_previous_hits():
    """«Перескор не отмывает»: списки в этот прогон не загружены (lookup None) — прежние попадания живы."""
    _fill("ut1", "phishing", ["listed.com"])
    did = _mk("listed.com")
    scoring.score_domain(did, clients=_funnel_clients())
    with db.SessionLocal() as s:
        s.query(DomainList).delete(); s.commit()
        s.get(Domain, did).status = "discovered"; s.commit()
    scoring.score_domain(did, clients=_funnel_clients())
    with db.SessionLocal() as s:
        assert s.get(Domain, did).score_breakdown["list_hits"] == ["phishing"]


def test_bulk_ok_ignores_missing_key_for_old_rows():
    assert scoring.list_hits(SimpleNamespace(score_breakdown=None)) == []
    assert scoring.list_hits(SimpleNamespace(score_breakdown={})) == []


# ------------------------------------------------------------------ настройки и панель
def test_hard_reject_lists_default_off_roundtrip_and_reset():
    assert st.get_settings()["hard_reject_lists"] is False
    assert st.update_settings(hard_reject_lists=True)["hard_reject_lists"] is True
    st.update_settings(min_dr=7)                                       # прочие ключи флаг не сбрасывают
    assert st.get_settings()["hard_reject_lists"] is True
    st.set_source_state({"x": {}}); st.set_list_state({"k": {"etag": "1"}})
    assert st.get_settings()["hard_reject_lists"] is True              # служебные ключи соседи не затирают
    assert st.reset_settings()["hard_reject_lists"] is False


BASE = {"min_referring_domains": 1, "min_age_years": 3.0, "approve_at": 0.7, "manual_review_at": 0.4}


def test_settings_form_checkbox_and_marker_semantics(client):
    client.post("/settings/save", data={**BASE, "v2_lists": "1", "hard_reject_lists": "on"},
                follow_redirects=False)
    assert st.get_settings()["hard_reject_lists"] is True
    client.post("/settings/save", data=BASE, follow_redirects=False)   # форма без маркера — не трогает
    assert st.get_settings()["hard_reject_lists"] is True
    client.post("/settings/save", data={**BASE, "v2_lists": "1"}, follow_redirects=False)  # снят чекбокс
    assert st.get_settings()["hard_reject_lists"] is False


def test_settings_page_shows_counter_of_candidates_that_would_be_hit(client):
    _fill("ut1", "gambling", ["bet.com"])
    _fill("blp", "phishing", ["ph.com"])
    for dom, status in (("bet.com", "scored"), ("ph.com", "discovered"), ("fine.com", "scored"),
                        ("bet.com.x", "x")):
        with db.SessionLocal() as s:
            s.add(Domain(domain=dom, source="dropcatch", status=status)); s.commit()
    with db.SessionLocal() as s:
        c = domain_lists.pool_counts(s)
    assert c == {"hard": 1, "any": 2}
    html = client.get("/settings").text
    assert 'name="hard_reject_lists"' in html and "CC BY-SA 4.0" in html
    assert "под отказ попало бы кандидатов пула: <b>1</b>" in html


def test_settings_page_without_lists_says_so(client):
    assert "списки ещё не загружены" in client.get("/settings").text


def test_inbox_shows_list_hit_category(client):
    with db.SessionLocal() as s:
        s.add(Domain(domain="listed.com", source="dropcatch", status="scored", score=0.8,
                     score_breakdown={"list_hits": ["adult"]}))
        s.commit()
    assert "в списках чистоты: adult" in client.get("/domains").text


def test_keys_whitelist_and_config_have_list_urls():
    from app.config import settings
    from app.services import api_keys
    keys = {f.key for _, _, _, fields in api_keys.GROUPS for f in fields}
    assert {"DOMAIN_LISTS_UT1_URL", "DOMAIN_LISTS_BLP_URL"} <= keys
    assert settings.DOMAIN_LISTS_UT1_URL.startswith("https://dsi.ut-capitole.fr/")


def test_migration_0033_chain_and_model_agree():
    import pathlib
    m = (pathlib.Path(__file__).parents[1] / "alembic/versions/0033_domain_list.py").read_text()
    assert 'down_revision = "0032_domain_indexes"' in m and "uq_domain_list" in m
    assert {c.name for c in DomainList.__table__.columns} == {"id", "domain", "category", "source", "updated_at"}


def test_scheduler_job_swallows_already_running(monkeypatch):
    from app.services import jobs
    from app.workers import scheduler
    def busy(*a, **k):
        raise jobs.AlreadyRunning("domain_lists")
    monkeypatch.setattr(domain_lists, "refresh", busy)
    scheduler.refresh_domain_lists()                                   # не падает
