"""Досье конкурентов: запросы по языку, SERP с запасным SearXNG, фильтры, порог слов, свежесть, скриншоты,
пустое досье — причина словами (спека §4)."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import socket

import pytest

import app.db as db
from app.config import settings
from app.models.domain import Domain
from app.models.offer import Offer
from app.models.research import SiteResearch
from app.models.site import Site
from app.services import jobs, research

LONG = "<html><body><h2>Цена</h2><p>" + "слово " * 350 + "5.99 $ в месяц</p><table><tr><th>a</th></tr><tr><td>b</td></tr></table></body></html>"
LONG_PRIVATE_CSS = LONG.replace("<body>", '<head><link rel="stylesheet" href="http://192.168.1.77:8000/x.css"></head><body>')
SHORT = "<html><body><p>мало слов</p></body></html>"
FIXTURES = Path(__file__).parent / "fixtures" / "research"
_REAL_RESOLVE = research._resolve       # до autouse-подмены: проверяем настоящий шов


def _canon(url: str) -> str:
    return LONG.replace("<body>", f'<head><link rel="canonical" href="{url}"></head><body>')


def _site(lang="ru") -> int:
    with db.SessionLocal() as s:
        o = Offer(brand="Durev VPN", affiliate_link="https://durevpn.com", language=lang, country="RU", active=True)
        d = Domain(domain="t.xyz", source="list", status="purchased", market_lang=lang)
        s.add_all([o, d]); s.commit()
        site = Site(domain_id=d.id, status="content", doc_root="/www/wwwroot/t.xyz", offer_id=o.id)
        s.add(site); s.commit()
        return site.id


class FakeAP:
    def __init__(self, urls, pages):
        self.urls, self.pages, self.calls, self.fetched = urls, pages, [], []
    def serp_urls(self, query, limit=10):
        self.calls.append(query); return list(self.urls)
    def fetch_html(self, url):
        self.fetched.append(url)
        return self.pages.get(url)


class FakeSX:
    def __init__(self, results): self.results = results
    def search(self, query, language=None, pageno=1): return [{"url": u} for u in self.results]


class FakeBL:
    def __init__(self, boom=False): self.boom, self.shots, self.kw = boom, [], []
    def screenshot(self, url, **kw):
        if self.boom: raise RuntimeError("down")
        self.shots.append(url); self.kw.append(kw); return b"\x89PNG"


@pytest.fixture(autouse=True)
def _dns(monkeypatch):
    """Офлайн-резолвер: *.lan-bad.test -> LAN-адрес, *.v6map.test -> ::ffff:10.0.0.1, *.nxdomain.test -> сбой, остальное — публичный."""
    def fake(host, port, *a, **kw):
        if host.endswith(".nxdomain.test"):
            raise socket.gaierror("nx")
        ip = ("192.168.1.5" if host.endswith(".lan-bad.test")
              else "::ffff:10.0.0.1" if host.endswith(".v6map.test") else "93.184.216.34")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]
    monkeypatch.setattr(research, "_resolve", fake)


@pytest.fixture(autouse=True)
def pauses(monkeypatch):
    """Паузы между попытками `_fetch` не спим — собираем: несбывшаяся страница иначе стоит 2 с на тест."""
    out = []
    monkeypatch.setattr(research, "_sleep", out.append)
    return out


class SeqAP:
    """`fetch_html` отдаёт ответы по очереди; исключение в очереди — бросается."""
    def __init__(self, *answers): self.answers, self.calls = list(answers), 0
    def fetch_html(self, url):
        self.calls += 1
        a = self.answers.pop(0)
        if isinstance(a, Exception): raise a
        return a


def _checked_at(sid, value=...):
    """Прочитать (или выставить) Site.research_checked_at; SQLite отдаёт naive — сравниваем без зоны."""
    with db.SessionLocal() as s:
        site = s.get(Site, sid)
        if value is not ...:
            site.research_checked_at = value
            s.commit()
        got = site.research_checked_at
        return got.replace(tzinfo=None) if got else None


@pytest.fixture
def wire(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "RESEARCH_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "RESEARCH_SCREENSHOTS", False)
    def _w(ap, sx=None, bl=None):
        monkeypatch.setattr(research, "_aparser", lambda: ap)
        monkeypatch.setattr(research, "_searxng", lambda: sx or FakeSX([]))
        monkeypatch.setattr(research, "_browserless", lambda: bl or FakeBL())
    return _w


def test_queries_follow_language_and_country():
    q = dict(research.queries_for("Durev VPN", "ru", "RU"))
    assert q["review"] == "Durev VPN обзор" and q["comparison"] == "Durev VPN против конкурентов"
    assert q["howto"] == "как настроить Durev VPN" and q["market"] == "лучший VPN Россия"
    qe = dict(research.queries_for("Durev VPN", "en", "US"))
    assert qe["review"] == "Durev VPN review" and qe["market"] == "best VPN United States"
    assert dict(research.queries_for("Durev VPN", "ru", None))["market"] == "лучший VPN"   # без хвоста
    with pytest.raises(ValueError):
        research.queries_for("X", "pl", None)


def test_every_language_has_query_templates():
    from app.services import locales
    for lang in locales.TEXTS:
        assert len(research.queries_for("B", lang, None)) == 4


def test_build_filters_noise_brand_short_pages_and_stores_rows(wire):
    sid = _site()
    unsafe = ["http://192.168.1.77:8000/x", "http://localhost/x", "ftp://a.com/x", "http://x.lan-bad.test/"]
    urls = ["https://durevpn.com/", "https://reddit.com/r/x", *unsafe, "https://www.a.com/1", "https://a.com/2",
            "https://b.com/short", "https://c.com/ok", "https://d.com/none"]
    pages = {"https://www.a.com/1": LONG, "https://a.com/2": LONG, "https://b.com/short": SHORT,
             "https://c.com/ok": LONG_PRIVATE_CSS, "https://durevpn.com/": LONG, **{u: LONG for u in unsafe}}
    ap = FakeAP(urls, pages)
    wire(ap)
    out = research.build_dossier(sid)
    assert out["status"] == "done" and len(ap.calls) == 4
    # SSRF-гард: небезопасные страницы и CSS-ссылка на LAN-адрес даже не запрашивались
    assert not set(unsafe) & set(ap.fetched) and "http://192.168.1.77:8000/x.css" not in ap.fetched
    assert "https://c.com/ok" in ap.fetched
    with db.SessionLocal() as s:
        rows = research.dossier(s, sid)
        by_kind = {}
        for r in rows:
            by_kind.setdefault(r.kind, []).append(r)
        assert set(by_kind) == set(research.KINDS)
        review = by_kind["review"]
        assert [r.url for r in review] == ["https://www.a.com/1", "https://c.com/ok"]   # a.com (с www и без) один раз, brand/reddit/short/none/unsafe — нет
        assert review[0].rank == 1 and review[1].rank == 2 and review[0].words >= 300 and review[0].numbers
        assert research.summary(s, sid)["rows"] == 8 and research.is_fresh(s, sid)


def test_goto_redirects_are_judged_by_fetched_page(wire):
    """A-Parser отдаёт google.com/goto?url=… — хост у всех один, судить можно только по скачанной странице."""
    sid = _site()
    g1, g2, g3 = (f"https://www.google.com/goto?url={x}" for x in ("one", "two", "three"))
    pages = {g1: _canon("https://durevpn.com/x"),          # сайт бренда -> отсев
             g2: _canon("https://reviewer.com/durev")}     # g3 -> 502 (None) -> пропуск
    wire(FakeAP([g1, g2, g3], pages))
    assert research.build_dossier(sid)["status"] == "done"
    with db.SessionLocal() as s:
        rows = research.dossier(s, sid)
        assert len(rows) == 4 and {r.domain for r in rows} == {"reviewer.com"}
        assert {r.url for r in rows} == {g2} and {r.final_url for r in rows} == {"https://reviewer.com/durev"}


def test_goto_without_canonical_is_kept_with_placeholder_domain(wire):
    """Без canonical/og:url домен — заглушка, даже если страница 3+ раза ссылается на сайт бренда."""
    sid = _site()
    g = "https://www.google.com/goto?url=zzz"
    wire(FakeAP([g], {g: LONG + '<a href="https://durevpn.com/a">1</a>' * 3}))
    assert research.build_dossier(sid)["status"] == "done"
    with db.SessionLocal() as s:
        r = research.dossier(s, sid)[0]
        assert r.domain == "goto1" and r.final_url == g


def test_goto_live_telegram_feed_with_relative_canonical_is_noise(wire):
    """Живая фикстура: лента t.me/s/durevvpn за goto — canonical относительный (`/s/durevvpn?before=…`),
    og:url нет, зато og:site_name="Telegram". Раньше шла в досье конкурентом №1 под заглушкой goto1."""
    sid = _site()
    g = "https://www.google.com/goto?url=tg"
    html = (FIXTURES / "competitor_1.html").read_text(encoding="utf-8")
    wire(FakeAP([g], {g: html}))
    out = research.build_dossier(sid)
    assert out["status"] == "empty" and out["rows"] == 0
    assert any("ни одной живой страницы" in w for w in out["warnings"])
    with db.SessionLocal() as s:
        assert research.dossier(s, sid) == []


@pytest.mark.parametrize("meta", ['<meta property="og:site_name" content="Telegram">',
                                  '<meta name="twitter:site" content="@YouTube">',
                                  '<meta property="og:site_name" content="Durev VPN — официальный блог">'])
def test_goto_without_canonical_platform_or_brand_site_name_is_dropped(wire, meta):
    sid = _site()
    g = "https://www.google.com/goto?url=sn"
    wire(FakeAP([g], {g: LONG.replace("<body>", f"<head>{meta}</head><body>")}))
    assert research.build_dossier(sid)["status"] == "empty"


def test_relative_canonical_resolves_against_absolute_og_url():
    html = ('<link rel="canonical" href="/review/durev"><meta property="og:url" content="https://rev.com/x">'
            '<meta property="og:site_name" content="Rev">')
    assert research._real_page(html, "g")[:2] == ("rev.com", "https://rev.com/review/durev")
    assert research._real_page('<link rel="canonical" href="https://www.b.com/p">', "g") == ("b.com", "https://www.b.com/p", [])


def test_resolver_is_late_bound_and_failure_is_unsafe(monkeypatch):
    """`_resolve` не ссылка на socket.getaddrinfo, захваченная при импорте (та обходила рубильник сети)."""
    assert _REAL_RESOLVE is not socket.getaddrinfo
    def boom(*a, **kw):
        raise socket.gaierror("nx")
    monkeypatch.setattr(socket, "getaddrinfo", boom)      # так же подменяет резолвер и рубильник conftest
    monkeypatch.setattr(research, "_resolve", _REAL_RESOLVE)
    assert research._safe_url("http://example.com/") is False


def test_safe_url_guard():
    ok = ["https://a.com/x", "http://sub.example.org/"]
    bad = ["ftp://a.com/x", "http://localhost/x", "http://127.0.0.1/", "http://192.168.1.77:8000/", "http://10.0.0.5/",
           "http://169.254.169.254/latest", "http://[::1]/", "http://printer.local/", "http://db.internal/",
           "http://2130706433/", "http:///x", "javascript:alert(1)", "",
           "http://x.lan-bad.test/", "http://x.v6map.test/", "http://x.nxdomain.test/",
           "http://100.64.1.1/", "http://[::ffff:10.0.0.1]/", "http://0.0.0.0/"]
    assert all(research._safe_url(u) for u in ok)
    assert not any(research._safe_url(u) for u in bad)


def test_serp_falls_back_to_searxng_when_aparser_empty(wire):
    sid = _site()
    wire(FakeAP([], {"https://e.org/": LONG}), FakeSX(["https://e.org/"]))
    assert research.build_dossier(sid)["status"] == "done"
    with db.SessionLocal() as s:
        assert {r.url for r in research.dossier(s, sid)} == {"https://e.org/"}


def test_all_fetches_fail_is_empty_with_reason_not_exception(wire):
    sid = _site()
    wire(FakeAP(["https://www.google.com/goto?url=abc"], {}))
    out = research.build_dossier(sid)
    assert out["status"] == "empty" and "ни одной" in out["reason"]
    last = jobs.last("research")          # из панели (jobs.spawn) dict никто не читает — причина обязана быть в job_run
    assert last["status"] == "done_warn" and "ни одной" in last["message"]
    with db.SessionLocal() as s:
        assert research.dossier(s, sid) == [] and research.summary(s, sid)["rows"] == 0


def test_fresh_dossier_is_not_refetched_unless_forced(wire):
    sid = _site()
    ap = FakeAP(["https://a.com/1"], {"https://a.com/1": LONG})
    wire(ap)
    research.build_dossier(sid)
    assert research.build_dossier(sid)["status"] == "fresh" and len(ap.calls) == 4
    with db.SessionLocal() as s:
        for r in s.query(SiteResearch).all():
            r.fetched_at = datetime.now(timezone.utc) - timedelta(days=31)
        s.commit()
    assert research.build_dossier(sid)["status"] == "done" and len(ap.calls) == 8
    assert research.build_dossier(sid, force=True)["status"] == "done" and len(ap.calls) == 12
    with db.SessionLocal() as s:
        assert len(research.dossier(s, sid)) == 4          # пересборка заменяет строки, не копит


def test_screenshots_when_enabled_and_browserless_down_is_warning(wire, monkeypatch, tmp_path):
    sid = _site()
    monkeypatch.setattr(settings, "RESEARCH_SCREENSHOTS", True)
    bl = FakeBL()
    wire(FakeAP(["https://a.com/1"], {"https://a.com/1": LONG}), bl=bl)
    research.build_dossier(sid)
    assert jobs.last("research")["status"] == "done" and "4" in jobs.last("research")["message"]
    assert bl.shots == ["https://a.com/1"] * 4
    # только первый экран: полностраничный PNG выходит за потолок шлюза 2000×2000 (live-formats §2–3)
    assert all(kw.get("full_page") is False for kw in bl.kw)
    with db.SessionLocal() as s:
        r = research.dossier(s, sid)[0]
        assert r.screenshot_path.endswith(".png") and (tmp_path / str(sid)).exists()
        assert research.summary(s, sid)["screenshots"] == 4
    wire(FakeAP(["https://a.com/1"], {"https://a.com/1": LONG}), bl=FakeBL(boom=True))
    out = research.build_dossier(sid, force=True)
    assert out["status"] == "done" and any("скриншот" in w for w in out["warnings"])
    last = jobs.last("research")
    assert last["status"] == "done_warn" and "скриншот" in last["message"]
    with db.SessionLocal() as s:
        r = research.dossier(s, sid)[0]
        assert r.screenshot_path is None and "скриншот" in (r.note or "")


def test_fetch_retries_none_then_succeeds(pauses):
    """Живой прогон 2026-10-10: ~половина goto-ссылок падает с 502 от прокси A-Parser (fetch_html -> None)."""
    ap = SeqAP(None, None, LONG)
    assert research._fetch(ap, "https://a.com/") == LONG and ap.calls == 3
    assert pauses == [1, 1]                                # пауза МЕЖДУ попытками, не после последней
    ap = SeqAP(None, None, None, LONG)
    assert research._fetch(ap, "https://a.com/") is None and ap.calls == 3      # четвёртой попытки нет
    assert len(pauses) == 4


def test_fetch_does_not_retry_exception(pauses):
    """Исключение уже прошло 3 транспортных попытки в BaseClient; повтор после таймаута дублирует задание (S2-12)."""
    ap = SeqAP(RuntimeError("ReadTimeout"), LONG)
    assert research._fetch(ap, "https://a.com/") is None and ap.calls == 1 and pauses == []


def test_build_dossier_retries_failed_fetch(wire, pauses):
    """`build_dossier` качает через `_fetch`: страница, не отдавшаяся с первого раза, попадает в досье ТОГО ЖЕ
    запроса. URL у всех четырёх запросов один, а дедуп доменов — по запросу: без повтора первый тип (review)
    остался бы пустым, остальные три скачались бы и так — поэтому сверяем типы, паузы и число скачиваний."""
    sid = _site()
    url = "https://a.com/1"
    class Flaky(FakeAP):
        def fetch_html(self, u):
            self.fetched.append(u)
            return self.pages.get(u) if len(self.fetched) >= 2 else None       # только самый первый ответ — 502
    ap = Flaky([url], {url: LONG})
    wire(ap)
    assert research.build_dossier(sid)["status"] == "done"
    with db.SessionLocal() as s:
        assert [r.kind for r in research.dossier(s, sid)] == list(research.KINDS)
    assert pauses == [1] and ap.fetched.count(url) == len(research.KINDS) + 1   # один повтор, не больше


def test_cancel_between_urls_stops_fetching_and_keeps_old_dossier(wire, monkeypatch):
    """«Отменить» не ждёт конца запроса (до 10 URL по три попытки): проверка — перед каждой страницей."""
    sid = _site()
    wire(FakeAP(["https://old.com/1"], {"https://old.com/1": LONG}))
    research.build_dossier(sid)
    ap = FakeAP(["https://a.com/1", "https://b.com/1"], {"https://a.com/1": LONG, "https://b.com/1": LONG})
    wire(ap)
    monkeypatch.setattr(jobs, "cancelled", lambda run: bool(ap.fetched))       # флаг встал после первой страницы
    assert "отменена" in research.build_dossier(sid, force=True)["reason"]
    assert ap.fetched == ["https://a.com/1"] and jobs.last("research")["status"] == "cancelled"
    with db.SessionLocal() as s:
        assert {r.url for r in research.dossier(s, sid)} == {"https://old.com/1"}


def test_cyrillic_platform_names_filtered():
    for name in ("Телеграм", "Ютуб", "ВКонтакте", "Дзен", "Рутуб"):
        assert research._platform_or_brand([name], "durevvpn") is True, name
    assert research._platform_or_brand(["ProPrivacy"], "durevvpn") is False


def test_build_dossier_stamps_research_checked_at(wire, monkeypatch):
    sid = _site()
    old = datetime(2026, 1, 1)
    assert _checked_at(sid) is None
    wire(FakeAP(["https://a.com/1"], {"https://a.com/1": LONG}))
    _checked_at(sid, old)
    with monkeypatch.context() as m:                       # отмена: отметка «проверяли» не двигается
        m.setattr(jobs, "cancelled", lambda run: True)
        assert "отменена" in research.build_dossier(sid)["reason"]
    assert _checked_at(sid) == old
    assert research.build_dossier(sid)["status"] == "done" and _checked_at(sid) > old      # с источниками
    _checked_at(sid, old)
    wire(FakeAP(["https://a.com/1"], {}))
    assert research.build_dossier(sid, force=True)["status"] == "empty" and _checked_at(sid) > old   # пустое


def test_recently_checked_judges_the_stamp_not_the_rows(wire):
    sid = _site()
    now = datetime.now(timezone.utc)
    with db.SessionLocal() as s:
        assert research.recently_checked(s, sid) is False          # ни разу не собирали — брать
    _checked_at(sid, (now - timedelta(hours=2)).replace(tzinfo=None))        # naive трактуется как UTC
    with db.SessionLocal() as s:
        assert research.recently_checked(s, sid) is True
    _checked_at(sid, now - timedelta(hours=research.EMPTY_RETRY_HOURS + 1))
    with db.SessionLocal() as s:
        assert research.recently_checked(s, sid) is False          # пауза вышла
    wire(FakeAP(["https://a.com/1"], {"https://a.com/1": LONG}))
    research.build_dossier(sid)
    with db.SessionLocal() as s:
        assert research.dossier(s, sid) and research.recently_checked(s, sid) is True   # строки есть — пауза та же
