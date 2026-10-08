"""Ранги доменов (W2d-cc-ranks): Common Crawl domain-ranks + Majestic как замена Ahrefs DR в authority.
Герметично: gz-файлы собираются в памяти, транспорт — httpx.MockTransport, сети нет."""
import gzip
from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy import select

import app.db as db
from app.integrations import crawlrank
from app.integrations.crawlrank import RankClient, domain_to_rev, pick_release, release_key, rev_to_domain
from app.models.domain import Domain
from app.models.domain_rank import DomainRank
from app.services import domain_ranks, scoring
from app.services import settings as st

HEAD = "#harmonicc_pos\t#harmonicc_val\t#pr_pos\t#pr_val\t#host_rev\t#n_hosts"


def _row(hp, hv, pp, pv, rev, n):
    return f"{hp}\t{hv}\t{pp}\t{pv}\t{rev}\t{n}"


# 10 «глобальных» строк графа; pr_pos 1..10 -> N = 10
CC = [HEAD,
      _row(1, 9.0, 1, 0.9, "com.google", 100),
      _row(2, 8.0, 2, 0.8, "com.top", 50),
      _row(3, 7.0, 3, 0.7, "uk.co.example", 7),
      _row(4, 6.0, 4, 0.6, "com.mid", 3),
      _row(5, 5.0, 5, 0.5, "net.foo", 1),
      _row(6, 4.0, 6, 0.4, "com.other1", 1),
      _row(7, 3.0, 7, 0.3, "com.other2", 1),
      _row(8, 2.0, 8, 0.2, "com.other3", 1),
      _row(9, 1.0, 9, 0.1, "com.other4", 1),
      _row(10, 0.5, 10, 0.05, "com.other5", 1)]


@pytest.fixture(autouse=True)
def _small_files(monkeypatch):
    # реальные пороги «файл не усечён» — миллионы строк; фикстуры маленькие
    monkeypatch.setattr(domain_ranks, "CC_MIN_ROWS", 10)
    monkeypatch.setattr(domain_ranks, "MAJESTIC_MIN_ROWS", 3)


def _add(*domains, status="discovered"):
    with db.SessionLocal() as s:
        for d in domains:
            s.add(Domain(domain=d, source="dropcatch", status=status, referring_domains=50, lane="bid"))
        s.commit()


def _rows(source="cc"):
    with db.SessionLocal() as s:
        return {r.domain: r for r in s.scalars(select(DomainRank).where(DomainRank.source == source))}


class _Fake:
    """Подмена RankClient: строки CC/Majestic из памяти, адрес среза управляется тестом."""
    def __init__(self, cc=CC, release="cc-main-2026-jul-aug-sep", majestic=None):
        self.cc, self.release, self.majestic = cc, release, majestic
        self.streams = 0

    def latest_ranks_url(self):
        return f"https://x/{self.release}-domain-ranks.txt.gz", self.release

    def iter_gz_lines(self, url):
        self.streams += 1
        yield from self.cc

    def iter_text_lines(self, url):
        yield from (self.majestic or [])


# ------------------------------------------------------------------ нотация и срезы
def test_reverse_notation_roundtrip_including_multilabel_suffix():
    assert rev_to_domain("com.example") == "example.com"
    assert rev_to_domain("uk.co.example") == "example.co.uk"
    assert rev_to_domain("COM.Example\n") == "example.com"
    assert domain_to_rev("example.co.uk") == "uk.co.example"
    assert domain_to_rev(rev_to_domain("de.shop.beispiel")) == "de.shop.beispiel"


def test_pick_release_takes_latest_regardless_of_list_order():
    ids = [{"id": "cc-main-2025-dec-jan-feb"}, {"id": "cc-main-2026-jul-aug-sep"},
           {"id": "cc-main-2026-feb-mar-apr"}]
    assert pick_release(ids) == "cc-main-2026-jul-aug-sep"
    assert pick_release(list(reversed(ids))) == "cc-main-2026-jul-aug-sep"
    assert pick_release(["cc-main-2026-feb-mar-apr", "cc-main-2025-dec-jan-feb"]) == "cc-main-2026-feb-mar-apr"
    # переход года: dec-jan-feb 2025 заканчивается в 2026 и новее aug-sep-oct 2025
    assert release_key("cc-main-2025-dec-jan-feb") > release_key("cc-main-2025-aug-sep-oct")
    assert pick_release(["weird-a", "weird-b"]) == "weird-a"          # не распознали — порядок списка
    assert pick_release([]) is None and pick_release(None) is None


def test_release_key_year_boundary_slices_with_two_digit_end_year():
    # реальные id CC на стыке годов: «2024-25-dec-jan-feb» (старт декабрь 2024, конец февраль 2025)
    assert release_key("cc-main-2024-25-dec-jan-feb") == (2025, 2)
    assert release_key("cc-main-2022-23-nov-dec-jan") == (2023, 1)
    assert release_key("cc-main-2024-25-dec-jan-feb") > release_key("cc-main-2024-aug-sep-oct")
    # такой срез — самый свежий, пока нет следующего: pick_release его не пропускает
    assert pick_release(["cc-main-2024-aug-sep-oct", "cc-main-2024-25-dec-jan-feb",
                         "cc-main-2024-may-jun-jul"]) == "cc-main-2024-25-dec-jan-feb"


def test_refresh_closes_transaction_before_streaming(monkeypatch):
    # idle-in-transaction на время многогигабайтного стрима держал бы локи на domains/domain_ranks
    _add("mid.com")
    seen = {}
    real = domain_ranks.pool_domains

    def spy(dbs):
        seen["db"] = dbs
        return real(dbs)
    monkeypatch.setattr(domain_ranks, "pool_domains", spy)

    class Probe(_Fake):
        def iter_gz_lines(self, url):
            seen["in_tx"] = seen["db"].in_transaction()
            return super().iter_gz_lines(url)
    domain_ranks.refresh(Probe())
    assert seen["in_tx"] is False
    domain_ranks.refresh(Probe(), force=False)                            # ветка с проверкой покрытия
    assert seen["in_tx"] is False


def test_latest_ranks_url_from_graphinfo_not_hardcoded(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "CC_RANKS_URL", "")
    monkeypatch.setattr(settings, "CC_GRAPH_BASE_URL", "https://mirror/g/")
    seen = []

    def h(req):
        seen.append(str(req.url))
        return httpx.Response(200, json=[{"id": "cc-main-2030-jan-feb-mar"}, {"id": "cc-main-2029-oct-nov-dec"}])
    c = RankClient()
    c._client = httpx.Client(transport=httpx.MockTransport(h))
    url, rel = c.latest_ranks_url()
    assert rel == "cc-main-2030-jan-feb-mar"
    assert url == "https://mirror/g/cc-main-2030-jan-feb-mar/domain/cc-main-2030-jan-feb-mar-domain-ranks.txt.gz"
    assert seen == [settings.CC_GRAPHINFO_URL]


def test_latest_ranks_url_explicit_override_skips_graphinfo(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "CC_RANKS_URL", "https://m/x/cc-main-2031-a-domain-ranks.txt.gz")
    c = RankClient()
    c._client = httpx.Client(transport=httpx.MockTransport(lambda r: pytest.fail("graphinfo не нужен")))
    assert c.latest_ranks_url() == ("https://m/x/cc-main-2031-a-domain-ranks.txt.gz", "cc-main-2031-a")


# ------------------------------------------------------------------ потоковый gzip
def _client_serving(body: bytes, chunk=7):
    def h(req):
        return httpx.Response(200, content=body, headers={"Content-Type": "application/gzip"})
    c = RankClient()
    c._client = httpx.Client(transport=httpx.MockTransport(h))
    return c


def test_iter_gz_lines_streams_small_chunks_and_multi_member():
    text = "\n".join(CC) + "\n"
    body = gzip.compress(text[:100].encode()) + gzip.compress(text[100:].encode())    # два gzip-члена
    got = list(_client_serving(body).iter_gz_lines("https://x/f.gz"))
    assert got == CC


def test_iter_gz_lines_last_line_without_newline_and_utf8():
    got = list(_client_serving(gzip.compress("a\nб-строка".encode())).iter_gz_lines("https://x/f.gz"))
    assert got == ["a", "б-строка"]


def test_iter_gz_lines_truncated_stream_raises_not_silently_ends():
    body = gzip.compress(("\n".join(CC) * 50).encode())
    with pytest.raises(EOFError):
        list(_client_serving(body[:-12]).iter_gz_lines("https://x/f.gz"))


def test_iter_gz_lines_http_error_propagates():
    c = RankClient()
    c._client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    with pytest.raises(httpx.HTTPStatusError):
        list(c.iter_gz_lines("https://x/f.gz"))


# ------------------------------------------------------------------ разбор
def test_parse_cc_selects_only_pool_by_reverse_name_and_reports_n():
    wanted = {domain_to_rev(d) for d in ("example.co.uk", "mid.com", "missing.com")}
    found, total, max_pos = domain_ranks.parse_cc(CC, wanted)
    assert set(found) == {"example.co.uk", "mid.com"}                  # missing.com в графе нет
    assert found["example.co.uk"] == {"harmonic": 7.0, "pagerank": 0.7, "n_hosts": 7, "pr_pos": 3}
    assert total == 10 and max_pos == 10


def test_parse_cc_reads_columns_by_header_names_not_positions():
    swapped = ["#host_rev\t#n_hosts\t#pr_val\t#pr_pos\t#harmonicc_val\t#harmonicc_pos",
               "com.mid\t3\t0.6\t4\t6.0\t4"]
    found, total, _ = domain_ranks.parse_cc(swapped, {"com.mid"})
    assert found["mid.com"]["pagerank"] == 0.6 and found["mid.com"]["pr_pos"] == 4 and total == 1


def test_parse_cc_header_without_host_rev_is_format_error_and_garbage_rows_skipped():
    with pytest.raises(ValueError, match="host_rev"):
        domain_ranks.parse_cc(["#a\t#b", "1\t2"], set())
    found, total, _ = domain_ranks.parse_cc([HEAD, "битая строка", _row(1, 1, 1, "x", "com.ok", "y")], {"com.ok"})
    assert found["ok.com"]["pagerank"] is None and found["ok.com"]["n_hosts"] is None and total == 1


def test_parse_majestic_by_header_names_and_pool_only():
    lines = ["GlobalRank,TldRank,Domain,TLD,RefSubNets", "1,1,google.com,com,9", "500,2,Mid.com,com,3",
             "900,3,other.com,com,2", "bad"]
    found, total = domain_ranks.parse_majestic(lines, {"mid.com", "google.com"})
    assert found == {"google.com": 1, "mid.com": 500} and total == 3
    with pytest.raises(ValueError, match="GlobalRank"):
        domain_ranks.parse_majestic(["a,b", "1,2"], set())


# ------------------------------------------------------------------ хранение: три состояния
def test_refresh_stores_only_pool_with_percentile_and_absent_rows():
    _add("mid.com", "example.co.uk", "ghost.com")
    out = domain_ranks.refresh(_Fake())
    assert out["cc"]["status"] == "updated" and out["cc"]["matched"] == 2 and out["cc"]["absent"] == 1
    rows = _rows()
    assert set(rows) == {"mid.com", "example.co.uk", "ghost.com"}       # ровно пул, не граф
    assert rows["mid.com"].pct == pytest.approx(0.6)                     # 1 - 4/10
    assert rows["mid.com"].harmonic_centrality == 6.0 and rows["mid.com"].n_hosts == 3
    assert rows["mid.com"].release == "cc-main-2026-jul-aug-sep"
    ghost = rows["ghost.com"]
    assert ghost.pagerank is None and ghost.pct is None                   # «проверили — в графе нет»


def test_refresh_excludes_legacy_ru_from_pool():
    _add("mid.com")
    with db.SessionLocal() as s:
        s.add(Domain(domain="old.ru", source="backorder", status="rejected", reject_reason="legacy_ru")); s.commit()
    domain_ranks.refresh(_Fake())
    assert set(_rows()) == {"mid.com"}


def test_refresh_empty_pool_is_skipped_not_wiping_anything():
    out = domain_ranks.refresh(_Fake())
    assert out["cc"]["status"] == "skipped" and _rows() == {}


def test_refresh_truncated_file_keeps_previous_slice(monkeypatch):
    _add("mid.com")
    domain_ranks.refresh(_Fake())
    short = _Fake(cc=CC[:4], release="cc-main-2099-x")
    monkeypatch.setattr(domain_ranks, "CC_MIN_ROWS", 10)
    out = domain_ranks.refresh(short)
    assert out["cc"]["status"] == "error" and "строк" in out["cc"]["error"]
    assert _rows()["mid.com"].pct == pytest.approx(0.6)                   # прошлый срез цел, не «нет в графе»


def test_refresh_stream_error_keeps_previous_slice():
    _add("mid.com")
    domain_ranks.refresh(_Fake())

    class Broken(_Fake):
        def iter_gz_lines(self, url):
            yield from CC[:3]
            raise EOFError("gzip оборван")
    out = domain_ranks.refresh(Broken(release="cc-main-2099-x"))
    assert out["cc"]["status"] == "error" and "EOFError" in out["cc"]["error"]
    assert _rows()["mid.com"].release == "cc-main-2026-jul-aug-sep"


def test_refresh_same_release_and_covered_pool_is_not_restreamed_unless_pool_grew_or_forced():
    _add("mid.com")
    f = _Fake()
    domain_ranks.refresh(f)
    assert f.streams == 1
    assert domain_ranks.refresh(f, force=False)["cc"]["status"] == "not_modified" and f.streams == 1
    _add("new.com")                                                       # пул вырос — новичок «без данных»
    assert domain_ranks.refresh(f, force=False)["cc"]["status"] == "updated" and f.streams == 2
    assert "new.com" in _rows()
    domain_ranks.refresh(f, force=True)                                   # кнопка — всегда перечитывает
    assert f.streams == 3
    f.release = "cc-main-2026-oct-nov-dec"                                # новый срез — перечитываем
    domain_ranks.refresh(f, force=False)
    assert f.streams == 4 and _rows()["mid.com"].release == "cc-main-2026-oct-nov-dec"


def test_refresh_removes_domains_that_left_pool():
    _add("mid.com", "top.com")
    domain_ranks.refresh(_Fake())
    with db.SessionLocal() as s:
        s.query(Domain).filter(Domain.domain == "top.com").delete(); s.commit()
    domain_ranks.refresh(_Fake())
    assert set(_rows()) == {"mid.com"}                                    # таблица не копит мусор


def test_majestic_loaded_only_when_enabled_and_keeps_only_hits():
    _add("mid.com", "top.com")
    maj = ["GlobalRank,TldRank,Domain,TLD", "1,1,google.com,com", "500,2,mid.com,com", "900,3,x.com,com"]
    f = _Fake(majestic=maj)
    assert "majestic" not in domain_ranks.refresh(f)                      # по умолчанию выкл
    st.update_settings(rank_majestic=True)
    out = domain_ranks.refresh(f)
    assert out["majestic"]["status"] == "updated" and out["majestic"]["matched"] == 1
    assert {d: r.rank_pos for d, r in _rows("majestic").items()} == {"mid.com": 500}


def test_majestic_short_file_error_does_not_break_cc():
    _add("mid.com")
    st.update_settings(rank_majestic=True)
    out = domain_ranks.refresh(_Fake(majestic=["GlobalRank,Domain", "1,a.com"]))
    assert out["cc"]["status"] == "updated" and out["majestic"]["status"] == "error"


# ------------------------------------------------------------------ чтение и нормировка
def test_lookup_none_when_not_loaded_and_three_states_when_loaded():
    assert domain_ranks.lookup(["mid.com"]) is None                       # не загружено — «не знаем»
    _add("mid.com", "ghost.com")
    domain_ranks.refresh(_Fake())
    got = domain_ranks.lookup(["MID.com", "ghost.com", "newcomer.com"])
    assert got["MID.com"]["cc_checked"] and got["MID.com"]["pct"] == pytest.approx(0.6)
    assert got["ghost.com"]["cc_checked"] and got["ghost.com"]["pct"] is None     # нет в графе (данные)
    assert got["newcomer.com"]["cc_checked"] is False                             # не проверяли (нет данных)


def test_authority_from_rank_linear_between_thresholds_and_states():
    f = domain_ranks.authority_from_rank
    assert f({"cc_checked": True, "pct": 0.775}, 0.6, 0.95, False)[0] == pytest.approx(0.5)
    assert f({"cc_checked": True, "pct": 0.5}, 0.6, 0.95, False)[0] == 0.0       # ниже нижнего порога
    assert f({"cc_checked": True, "pct": 0.99}, 0.6, 0.95, False)[0] == 1.0      # выше верхнего — потолок
    v, s = f({"cc_checked": True, "pct": None}, 0.6, 0.95, False)
    assert v == 0.0 and s["source"] == "cc_absent"                               # в графе нет — ноль
    v, s = f({"cc_checked": False, "pct": None}, 0.6, 0.95, False)
    assert v is None and s["source"] is None                                     # не проверяли — нет данных
    assert f({"cc_checked": True, "pct": 0.775, "majestic": 10}, 0.6, 0.95, False)[0] == pytest.approx(0.5)
    v, s = f({"cc_checked": True, "pct": 0.775, "majestic": 10}, 0.6, 0.95, True)
    assert v == pytest.approx(0.5 + domain_ranks.MAJESTIC_BONUS) and s["source"] == "cc+majestic"
    assert f({"cc_checked": True, "pct": 0.99, "majestic": 10}, 0.6, 0.95, True)[0] == 1.0   # потолок
    v, s = f({"cc_checked": False, "majestic": 10}, 0.6, 0.95, True)
    assert v == domain_ranks.MAJESTIC_BONUS and s["source"] == "majestic"        # бонус без CC-базы


# ------------------------------------------------------------------ волна и compute_score
def _state(domain, alive=True):
    return scoring.FunnelState(domain_id=1, domain=domain, lane="bid", referring_domains=50,
                               acquire_deadline=None, feed_flags=None, alive=alive)


def _wave_st(**kw):
    return {**st.get_settings(), **kw}


def test_wave_ranks_no_signal_when_not_loaded_unchecked_or_dead():
    a = _state("mid.com")
    scoring._wave_ranks([a], _wave_st())
    assert "rank_authority" not in a.sig and "rank" not in a.sig          # не загружено
    _add("mid.com")
    domain_ranks.refresh(_Fake())
    new, dead = _state("newcomer.com"), _state("mid.com", alive=False)
    scoring._wave_ranks([new, dead], _wave_st())
    assert "rank_authority" not in new.sig and "rank_authority" not in dead.sig


def test_wave_ranks_writes_normalised_authority_using_runtime_thresholds():
    _add("mid.com", "ghost.com")
    domain_ranks.refresh(_Fake())
    a, g = _state("mid.com"), _state("ghost.com")
    scoring._wave_ranks([a, g], _wave_st())
    assert a.sig["rank_authority"] == pytest.approx(0.0)                  # pct 0.6 == нижний порог по умолчанию
    assert g.sig["rank_authority"] == 0.0 and g.sig["rank"]["source"] == "cc_absent"
    b = _state("mid.com")
    scoring._wave_ranks([b], _wave_st(rank_pct_low=0.2, rank_pct_full=1.0))
    assert b.sig["rank_authority"] == pytest.approx(0.5)                  # (0.6-0.2)/0.8 — порог рантайм
    assert b.sig["rank"]["pct"] == pytest.approx(0.6) and b.sig["rank"]["release"]


def test_wave_ranks_db_failure_is_error_not_verdict(monkeypatch):
    def boom(_):
        raise RuntimeError("db")
    monkeypatch.setattr(domain_ranks, "lookup", boom)
    a = _state("mid.com")
    scoring._wave_ranks([a], _wave_st())
    assert a.alive and "ranks:RuntimeError" in a.sig["errors"] and "rank_authority" not in a.sig


SIG = {"wayback_checked": True, "prior_flags": {}, "age_years": 8, "referring_domains": 100}


def _auth(sig):
    out = scoring.compute_score({**SIG, **sig})["breakdown"]
    return out["components"]["authority"], out["authority_source"]


def test_compute_score_authority_rank_beats_dr_dr_is_fallback_none_is_neutral():
    assert _auth({"rank_authority": 0.8, "dr": 5.0}) == (0.8, "rank")     # ранг вместо Ahrefs DR
    assert _auth({"rank_authority": 0.0, "dr": 30.0}) == (0.0, "rank")    # «нет в графе» — ноль, DR не спасает
    assert _auth({"dr": 15.0}) == (0.5, "dr")                             # рангов нет — DR как раньше
    assert _auth({}) == (0.5, None)                                       # нет данных — не ноль
    assert _auth({"dr": 0.0}) == (0.0, "dr")                              # DR=0 известен — это ноль


# ------------------------------------------------------------------ сквозь воронку
def _funnel_clients():
    from tests.test_funnel import _clients, _Wayback
    return _clients(datetime(2012, 1, 1, tzinfo=timezone.utc), _Wayback(age_years=9.0))


def _mk(domain):
    with db.SessionLocal() as s:
        d = Domain(domain=domain, source="dropcatch", status="discovered", referring_domains=50, lane="bid")
        s.add(d); s.commit(); s.refresh(d)
        return d.id


def test_funnel_uses_rank_for_authority_and_persists_summary():
    did = _mk("example.co.uk")
    domain_ranks.refresh(_Fake())                                         # пул = {example.co.uk}; pct = 0.7
    out = scoring.score_domain(did, clients=_funnel_clients())
    with db.SessionLocal() as s:
        d = s.get(Domain, did)
    bd = d.score_breakdown
    assert bd["components"]["authority"] == pytest.approx((0.7 - 0.6) / 0.35, abs=1e-3)
    assert bd["authority_source"] == "rank" and bd["rank"]["source"] == "cc" and bd["rank"]["n_hosts"] == 7
    assert out["status"] in ("scored", "rejected") and out["status"] != "approved"   # авто-approve нет


def test_funnel_without_ranks_authority_is_no_data_not_zero():
    did = _mk("nobody.com")
    scoring.score_domain(did, clients=_funnel_clients())
    with db.SessionLocal() as s:
        bd = s.get(Domain, did).score_breakdown
    assert bd["components"]["authority"] == 0.5 and bd["authority_source"] is None and bd["rank"] is None


def test_rescore_without_ranks_keeps_previous_rank_summary():
    did = _mk("example.co.uk")
    domain_ranks.refresh(_Fake())
    scoring.score_domain(did, clients=_funnel_clients())
    with db.SessionLocal() as s:
        s.query(DomainRank).delete(); s.get(Domain, did).status = "discovered"; s.commit()
    scoring.score_domain(did, clients=_funnel_clients())
    with db.SessionLocal() as s:
        bd = s.get(Domain, did).score_breakdown
    assert bd["rank"]["source"] == "cc"                                   # улика прошлого прогона жива
    # и authority считается ПО НЕЙ, а не молча падает в нейтральные 0.5 (breakdown не противоречит себе)
    assert bd["authority_source"] == "rank"
    assert bd["components"]["authority"] == pytest.approx((0.7 - 0.6) / 0.35, abs=1e-3)


def test_blind_retry_state_keeps_rank_authority():
    # «вслепую»-повтор (retry_blind_history) гоняет только W5: authority не должна сдвигаться на 0.5
    did = _mk("example.co.uk")
    domain_ranks.refresh(_Fake())
    scoring.score_domain(did, clients=_funnel_clients())
    with db.SessionLocal() as s:
        d = s.get(Domain, did)
        before = d.score_breakdown["components"]["authority"]
        state = scoring._blind_state(d)
    scoring._commit_result(state, None, st.get_settings())
    with db.SessionLocal() as s:
        bd = s.get(Domain, did).score_breakdown
    assert bd["components"]["authority"] == before
    assert bd["authority_source"] == "rank" and bd["rank"]["source"] == "cc"


def test_cc_absent_authority_zero_survives_blind_retry():
    did = _mk("nobody.com")
    domain_ranks.refresh(_Fake())                                         # проверили: в графе нет -> 0.0
    scoring.score_domain(did, clients=_funnel_clients())
    with db.SessionLocal() as s:
        d = s.get(Domain, did)
        assert d.score_breakdown["components"]["authority"] == 0.0
        state = scoring._blind_state(d)
    scoring._commit_result(state, None, st.get_settings())
    with db.SessionLocal() as s:
        bd = s.get(Domain, did).score_breakdown
    assert bd["components"]["authority"] == 0.0 and bd["rank"]["source"] == "cc_absent"


# ------------------------------------------------------------------ настройки, панель, воркер
def test_rank_settings_defaults_roundtrip_validation_and_reset():
    s0 = st.get_settings()
    assert (s0["rank_pct_low"], s0["rank_pct_full"], s0["rank_majestic"]) == (0.6, 0.95, False)
    s1 = st.update_settings(rank_pct_low=0.5, rank_majestic=True)
    assert s1["rank_pct_low"] == 0.5 and s1["rank_pct_full"] == 0.95 and s1["rank_majestic"] is True
    st.update_settings(min_dr=9)                                           # прочие ключи не сбрасывают
    assert st.get_settings()["rank_majestic"] is True
    with pytest.raises(ValueError, match="пороги рангов"):
        st.update_settings(rank_pct_low=0.9, rank_pct_full=0.8)
    with pytest.raises(ValueError):
        st.update_settings(rank_pct_full=1.5)
    assert st.get_settings()["rank_pct_low"] == 0.5                         # отказ ничего не записал
    assert st.reset_settings()["rank_pct_low"] == 0.6


BASE = {"min_referring_domains": 1, "min_age_years": 3.0, "approve_at": 0.7, "manual_review_at": 0.4}


def test_settings_form_saves_rank_thresholds_checkbox_marker_and_rejects_bad_range(client):
    client.post("/settings/save", data={**BASE, "v2_lists": "1", "rank_pct_low": "0.5", "rank_pct_full": "0.9",
                                        "rank_majestic": "on"}, follow_redirects=False)
    s = st.get_settings()
    assert (s["rank_pct_low"], s["rank_pct_full"], s["rank_majestic"]) == (0.5, 0.9, True)
    client.post("/settings/save", data=BASE, follow_redirects=False)       # без маркера — тумблер не трогаем
    assert st.get_settings()["rank_majestic"] is True
    r = client.post("/settings/save", data={**BASE, "v2_lists": "1", "rank_pct_low": "0.95",
                                            "rank_pct_full": "0.6"})
    assert r.status_code == 400 and "пороги рангов" in r.text
    assert st.get_settings()["rank_pct_low"] == 0.5


def test_settings_page_shows_ranks_block_states_and_attribution(client):
    html = client.get("/settings").text
    assert 'name="rank_pct_low"' in html and "ранги не загружены" in html and "CC BY 3.0" in html
    assert 'action="/settings/ranks/refresh"' in html
    _add("mid.com")
    domain_ranks.refresh(_Fake())
    html = client.get("/settings").text
    assert "cc: <b>1</b>" in html and "последняя загрузка" in html


def test_ranks_refresh_button_spawns_domain_ranks_job(client, monkeypatch):
    from app.services import jobs
    seen = []
    monkeypatch.setattr(jobs, "spawn", lambda name, target: seen.append((name, target)) or True)
    r = client.post("/settings/ranks/refresh", follow_redirects=False)
    assert r.status_code in (302, 303) and seen[0][0] == "domain_ranks" and seen[0][1] is domain_ranks.refresh


def test_scheduler_runs_ranks_non_forced_and_swallows_already_running(monkeypatch):
    from app.services import jobs
    from app.workers import scheduler
    seen = []
    monkeypatch.setattr(domain_ranks, "refresh", lambda **kw: seen.append(kw))
    scheduler.refresh_domain_ranks()
    assert seen == [{"force": False}]                                       # cron не перечитывает без нужды

    def busy(**kw):
        raise jobs.AlreadyRunning("domain_ranks")
    monkeypatch.setattr(domain_ranks, "refresh", busy)
    scheduler.refresh_domain_ranks()


def test_keys_whitelist_config_and_migration_agree():
    import pathlib
    from app.config import settings
    from app.services import api_keys
    keys = {f.key for _, _, _, fields in api_keys.GROUPS for f in fields}
    assert {"CC_RANKS_URL", "CC_GRAPHINFO_URL", "CC_GRAPH_BASE_URL", "MAJESTIC_URL"} <= keys
    assert all(hasattr(settings, k) for k in ("CC_RANKS_URL", "CC_GRAPHINFO_URL", "CC_GRAPH_BASE_URL", "MAJESTIC_URL"))
    assert settings.CC_RANKS_URL == ""                                      # срез не зашит в конфиг
    m = (pathlib.Path(__file__).parents[1] / "alembic/versions/0034_domain_ranks.py").read_text()
    assert 'down_revision = "0033_domain_list"' in m and "uq_domain_ranks" in m
    cols = {c.name for c in DomainRank.__table__.columns}
    assert cols == {"id", "domain", "source", "harmonic_centrality", "pagerank", "n_hosts", "pct", "rank_pos",
                    "release", "updated_at"}
    assert all(f'"{c}"' in m for c in cols if c != "id") and crawlrank
