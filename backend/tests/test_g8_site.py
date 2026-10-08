"""G8 (S5-07, S6-02..05, S7-06/07, F8-10/10b, S5-15): язык сайта, статический шаблон, независимость
темы по сиду домена, графика (SVG), sandbox <img>, sitemap/robots, порядок деплоя assets→страницы."""
import re
import xml.etree.ElementTree as ET
from types import SimpleNamespace as N

import pytest

import app.db as db
from app.integrations.llm import LlmClient
from app.models.domain import Domain
from app.models.offer import Offer
from app.models.site import Page, Site
from app.services import content, locales, publish, site_builder as sb

CYR = re.compile("[А-Яа-яЁё]")


# ── helpers ───────────────────────────────────────────────────────────────────

def _site(domain="g8.com", market_lang=None, offer_lang=None, status="content", pages=(),
          brand="NordVPN") -> int:
    with db.SessionLocal() as s:
        d = Domain(domain=domain, source="dropcatch", status="purchased", market_lang=market_lang)
        s.add(d)
        s.commit()
        off = Offer(brand=brand, affiliate_link="https://ex.com/aff", active=True, language=offer_lang)
        s.add(off)
        s.commit()
        site = Site(domain_id=d.id, status=status, aapanel_site_name=domain,
                    doc_root=f"/www/wwwroot/{domain}", offer_id=off.id)
        s.add(site)
        s.commit()
        for path, st in pages:
            s.add(Page(site_id=site.id, url_path=path, title=f"T {path}", status=st, lang="en",
                       offer_id=off.id, body="<p>Body of the page that is long enough.</p>"))
        s.commit()
        return site.id


@pytest.fixture
def llm_spy(monkeypatch):
    calls = []

    def _complete(self, system, prompt, **kw):
        calls.append((system, prompt))
        return "<h2>h</h2><p>text</p>"
    monkeypatch.setattr(LlmClient, "complete", _complete)
    return calls


@pytest.fixture
def writes(monkeypatch):
    log = []
    monkeypatch.setattr("app.integrations.aapanel.AaPanelClient.__init__", lambda self: None)
    monkeypatch.setattr("app.integrations.aapanel.AaPanelClient.write_file",
                        lambda self, path, body: log.append((path, body)))
    return log


# ── (1) язык сайта ────────────────────────────────────────────────────────────

def test_autopilot_language_comes_from_market_not_ru(llm_spy):
    sid = _site(market_lang="de-DE", offer_lang="en")
    assert content.generate_site(sid) == 3                  # без lang — как зовёт автопилот
    with db.SessionLocal() as s:
        pages = s.query(Page).filter_by(site_id=sid).all()
        assert {p.lang for p in pages} == {"de"}            # рынок домена > язык оффера
        assert any("einrichten" in p.title for p in pages)
        assert not any(CYR.search(p.title) for p in pages)
    assert all("German" in sy and "Output language: German" in pr for sy, pr in llm_spy)
    assert not any(CYR.search(sy) for sy, _ in llm_spy)     # промпт не прошит по-русски


def test_language_falls_back_to_offer_then_en(llm_spy):
    sid = _site(offer_lang="es")
    content.generate_site(sid)
    with db.SessionLocal() as s:
        assert {p.lang for p in s.query(Page).filter_by(site_id=sid)} == {"es"}
    sid2 = _site(domain="g9.com")
    content.generate_site(sid2)
    with db.SessionLocal() as s:
        assert {p.lang for p in s.query(Page).filter_by(site_id=sid2)} == {"en"}   # не ru


def test_unsupported_market_language_is_refused_not_silently_english(llm_spy):
    sid = _site(market_lang="pl")
    with pytest.raises(ValueError, match="pl"):
        content.generate_site(sid)
    assert llm_spy == []


def test_locale_dictionary_is_complete_for_all_languages():
    base = set(locales.TEXTS["en"])
    for lg, d in locales.TEXTS.items():
        assert set(d) == base, lg
        assert lg in locales.LANG_NAMES and lg in locales.OG_LOCALE
    assert {"en", "de", "es", "fr", "nl", "pt", "it", "ru"} <= set(locales.TEXTS)


# ── (2)(5) документ страницы ──────────────────────────────────────────────────

OFFER = N(brand="Nord", affiliate_link="https://ex.com/aff", promo_code="X1", active=True)


def _doc(domain, lang, path="/", title="Nord title", body="<h2>Sub</h2><p>First paragraph here.</p>"):
    ctx = sb.make_ctx(domain, [("/", "Home"), ("/vs", "Vs")], lang, "NordVPN")
    return content.render_html(N(title=title, body=body, url_path=path), OFFER, lang=lang, ctx=ctx)


@pytest.mark.parametrize("lang", ["de", "es", "fr", "nl", "pt", "it", "en"])
def test_no_russian_template_strings_for_foreign_lang(lang):
    out = _doc("best-vpn.com", lang)
    assert not CYR.search(out), CYR.search(out)
    assert f"<html lang='{lang}'>" in out


def test_document_has_full_head_nav_h1_footer():
    out = _doc("www.Best-VPN.com", "de", path="/vs", title="Nord vs. Rest")
    assert out.startswith("<!doctype html>")
    assert "<title>Nord vs. Rest</title>" in out and "<h1>Nord vs. Rest</h1>" in out
    assert "<meta name='description' content='First paragraph here.'>" in out
    assert "<link rel='canonical' href='https://best-vpn.com/vs/'>" in out
    assert "og:title" in out and "og:image" in out and "og:locale' content='de_DE'" in out
    assert "name='viewport'" in out and "rel='icon'" in out and "/" + sb.theme_for("best-vpn.com").asset("s.css") in out
    assert "<header" in out and "<nav" in out and "href='/vs/' aria-current='page'" in out
    assert "<footer" in out and "Affiliate-Links" in out
    assert "<style" not in out                              # один общий CSS-файл


def test_unique_title_and_description_per_page():
    a = _doc("x.com", "en", "/", "Title A", "<p>Alpha text.</p>")
    b = _doc("x.com", "en", "/vs", "Title B", "<p>Beta text.</p>")
    assert "content='Alpha text.'" in a and "content='Beta text.'" in b
    assert "<title>Title A</title>" in a and "<title>Title B</title>" in b
    assert "canonical' href='https://x.com/'" in a and "https://x.com/vs/" in b


def test_description_truncates_on_word_boundary():
    d = sb.description_of("<p>" + "word " * 100 + "</p>", "t")
    assert len(d) <= 156 and d.endswith("…") and "  " not in d


def test_sponsored_rel_kept_on_cta_and_body_links():
    out = _doc("x.com", "en", body='<p><a href="https://p.com">p</a></p>')
    assert out.count('rel="sponsored nofollow noopener"') == 2


def test_no_external_requests_in_html():
    out = _doc("x.com", "de")
    out = re.sub(r"<link rel='canonical'[^>]*>", "", out)
    out = re.sub(r"<meta property='og:[^>]*>", "", out)
    out = out.replace('href="https://ex.com/aff"', "")      # единственная ссылка оффера
    assert not re.search(r"https?://", out), re.findall(r".{20}https?://.{20}", out)
    assert "<script" not in out and "@import" not in out


# ── (3) независимость: тема по сиду домена ───────────────────────────────────

DOMAINS = ["alpha-vpn.com", "beta-secure.net", "gamma-net.io", "delta-guard.org", "epsilon.eu", "zeta-web.co"]


def test_theme_is_deterministic_and_varies_across_portfolio():
    assert sb.theme_for("alpha-vpn.com") == sb.theme_for("www.ALPHA-vpn.com")
    themes = [sb.theme_for(d) for d in DOMAINS]
    assert len({t.pal["name"] for t in themes}) >= 3
    assert len({t.prefix for t in themes}) == len(DOMAINS)
    css = {d: sb.build_css(sb.theme_for(d)) for d in DOMAINS}
    assert len(set(css.values())) == len(DOMAINS)            # нет одинакового файла на всех
    # классы разметки тоже разные: нет общего «.hd»
    docs = {d: _doc(d, "en") for d in DOMAINS}
    assert len({re.search(r"<header class='([^']+)'", x).group(1) for x in docs.values()}) == len(DOMAINS)


def test_three_langs_by_three_seeds_render():
    for lg in ("de", "es", "fr"):
        outs = [_doc(d, lg) for d in DOMAINS[:3]]
        assert len(set(outs)) == 3
        assert all(not CYR.search(o) for o in outs)


def test_site_pages_do_not_link_to_other_portfolio_domains():
    out = _doc("alpha-vpn.com", "en")
    hosts = set(re.findall(r"https?://([^/'\" ]+)", out))
    assert hosts <= {"alpha-vpn.com", "ex.com"}


def _A(domain, name):
    return sb.theme_for(domain).asset(name)


def test_portfolio_footprint_no_shared_asset_names_class_scheme_or_marker():
    """Инвариант 4: у двух сайтов портфеля нет общих путей assets, маркера и схемы имён классов."""
    ds = ["alpha-vpn.com", "beta-secure.net", "gamma.org", "delta-vpn.io"]
    fl = {d: sb.build_assets(d, "en", "NordVPN", "Home") for d in ds}
    for i, a in enumerate(ds):
        assert not any(n.endswith((".version", "site.css", "hero.svg")) for n in fl[a])
        for b in ds[i + 1:]:
            assert not set(fl[a]) & set(fl[b]), (a, b)
            ca, cb = sb.build_css(sb.theme_for(a)), sb.build_css(sb.theme_for(b))
            assert not set(re.findall(r"\.(c[0-9a-f]{8})", ca)) & set(re.findall(r"\.(c[0-9a-f]{8})", cb))
    css = fl[ds[0]][_A(ds[0], "s.css")]
    assert "§" not in css and not re.search(r"\.k[0-9a-f]{4}-", css)


# ── (4) графика ───────────────────────────────────────────────────────────────

def test_assets_are_deterministic_valid_svg_and_versioned():
    a = sb.build_assets("alpha-vpn.com", "en", "NordVPN", "Home")
    assert a == sb.build_assets("alpha-vpn.com", "en", "NordVPN", "Home")
    assert a != sb.build_assets("beta-secure.net", "en", "NordVPN", "Home")
    for name, body in a.items():
        if name.endswith(".svg"):
            root = ET.fromstring(body)
            assert root.tag.endswith("svg"), name
            assert "http" not in body.replace("http://www.w3.org/2000/svg", ""), name
    assert 'width="1200" height="630"' in a[_A("alpha-vpn.com", "og.svg")]
    chart = _A("alpha-vpn.com", "chart-servers.svg")
    assert chart in a and "NordVPN" in a[chart]
    assert re.fullmatch(r"[0-9a-f]{64}\n", a[sb.marker_path("alpha-vpn.com")])
    assert chart not in sb.build_assets("alpha-vpn.com", "en", "NoSuchBrand")


def test_home_has_benefit_icons_and_vs_has_chart_with_local_img_only():
    home, vs = _doc("x.com", "de"), _doc("x.com", "de", path="/vs")
    assert sum(f"/{_A('x.com', f'icon-{n}.svg')}" in home for n in ("privacy", "speed", "access")) == 3
    assert "Privatsphäre" in home
    assert f"/{_A('x.com', 'chart-servers.svg')}" in vs and "Balkendiagramm" in vs
    for out in (home, vs):
        for src in re.findall(r"<img[^>]*src='([^']+)'", out):
            assert re.fullmatch(r"/assets/a[0-9a-f]{10}\.svg", src)
        assert all("loading='lazy'" in tag and "alt=" in tag and "width=" in tag
                   for tag in re.findall(r"<img[^>]*>", out))


def test_image_provider_seam():
    from app.services import images
    assert isinstance(images.get_provider(), images.SvgLocalProvider)
    with pytest.raises(ValueError):
        images.get_provider("dall-e")


# ── санитайзер <img> ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("src", [
    "https://evil.com/a.svg", "http://x.com/assets/a.svg", "//evil.com/assets/a.svg",
    "data:image/svg+xml;base64,AAAA", "javascript:alert(1)", "assets/../secret.svg",
    "/assets/a.png", "assets/a.svg?x=1", "vbscript:x"])
def test_img_with_non_local_src_is_stripped(src):
    out = content._sanitize(f'<p>t<img src="{src}" alt="a" onerror="x()"></p>')
    assert "<img" not in out and "onerror" not in out


def test_img_local_svg_kept_and_normalized():
    out = content._sanitize('<img src="assets/hero.svg" alt="Hi" width="10" height="x" onload="1">')
    assert out == '<img src="/assets/hero.svg" alt="Hi" width="10" loading="lazy">'
    assert content._sanitize('<img src="/assets/icon-a.svg" alt="">').startswith('<img src="/assets/icon-a.svg"')


# ── (4)(6) sitemap/robots + порядок деплоя ───────────────────────────────────

def test_sitemap_and_robots_valid():
    f = sb.build_site_files("www.Alpha-VPN.com", ["/vs", "/", "/setup", "/vs"])
    root = ET.fromstring(f["sitemap.xml"])
    locs = [e.text for e in root.iter("{http://www.sitemaps.org/schemas/sitemap/0.9}loc")]
    assert locs == ["https://alpha-vpn.com/", "https://alpha-vpn.com/setup/", "https://alpha-vpn.com/vs/"]
    assert f["robots.txt"].rstrip().endswith("Sitemap: https://alpha-vpn.com/sitemap.xml")
    assert "sitemap.xml" not in sb.build_site_files("a.com", [])   # нечего перечислять


def test_publish_deploys_assets_then_pages_then_sitemap(writes):
    sid = _site(pages=[("/", "edited"), ("/vs", "edited"), ("/setup", "draft")])
    out = publish.publish_site(sid)
    assert out["status"] == "published" and sorted(out["pages"]) == ["/", "/vs"]
    paths = [p.replace("/www/wwwroot/g8.com/", "") for p, _ in writes]
    first_page = paths.index("index.html")
    assets = [p for p in paths if p.startswith("assets/")]
    assert paths[:len(assets)] == assets and all(paths.index(a) < first_page for a in assets)
    assert paths.index(sb.marker_path("g8.com")) == len(assets) - 1                 # маркер — последним
    assert paths[-2:] == ["robots.txt", "sitemap.xml"] or paths[-2:] == ["sitemap.xml", "robots.txt"]
    body = dict(writes)
    sm = body["/www/wwwroot/g8.com/sitemap.xml"]
    assert "/vs/" in sm and "/setup/" not in sm                               # draft не в sitemap
    home = body["/www/wwwroot/g8.com/index.html"]
    assert "href='/vs/'" in home and "/setup/" not in home                    # nav — только живые
    assert f"rel='stylesheet' href='/{_A('g8.com', 's.css')}'" in home


def test_publish_is_idempotent_same_bytes(writes):
    sid = _site(pages=[("/", "edited")])
    publish.publish_site(sid)
    first = dict(writes)
    with db.SessionLocal() as s:                                              # вернуть в edited
        for p in s.query(Page).filter_by(site_id=sid):
            p.status = "edited"
        s.commit()
    writes.clear()
    publish.publish_site(sid)
    for rel in (_A("g8.com", "s.css"), sb.marker_path("g8.com")):
        assert dict(writes)[f"/www/wwwroot/g8.com/{rel}"] == first[f"/www/wwwroot/g8.com/{rel}"]


def test_asset_failure_leaves_pages_untouched_and_reports_written(monkeypatch):
    log = []

    def _w(self, path, body):
        if path.endswith(_A("g8.com", "hero.svg")):
            raise RuntimeError("disk full")
        log.append(path)
    monkeypatch.setattr("app.integrations.aapanel.AaPanelClient.__init__", lambda self: None)
    monkeypatch.setattr("app.integrations.aapanel.AaPanelClient.write_file", _w)
    sid = _site(pages=[("/", "edited")])
    out = publish.publish_site(sid)
    assert out["status"] == "failed" and "disk full" in out["failed"]["/"]
    assert not any(p.endswith("index.html") for p in log)                    # страницы не тронуты
    assert out["assets_written"] == [_A("g8.com", "s.css"), _A("g8.com", "favicon.svg")]
    with db.SessionLocal() as s:
        assert s.query(Page).filter_by(site_id=sid).one().status == "edited"


def test_published_home_gets_nav_link_to_later_page(writes):
    sid = _site(pages=[("/", "edited"), ("/vs", "draft")])
    publish.publish_site(sid)
    assert "href='/vs/'" not in dict(writes)["/www/wwwroot/g8.com/index.html"]
    with db.SessionLocal() as s:                                              # человек вычитал /vs
        s.query(Page).filter_by(site_id=sid, url_path="/vs").one().status = "edited"
        s.commit()
    writes.clear()
    publish.publish_site(sid)
    assert "href='/vs/'" in dict(writes)["/www/wwwroot/g8.com/index.html"]   # «/» перерисована
    with db.SessionLocal() as s:
        assert s.query(Page).filter_by(site_id=sid, url_path="/").one().status == "published"


def test_published_page_body_not_changed_when_republishing_siblings(writes):
    sid = _site(pages=[("/", "published"), ("/vs", "edited")])
    publish.publish_site(sid)
    home = dict(writes)["/www/wwwroot/g8.com/index.html"]
    assert "Body of the page that is long enough." in home and "href='/vs/'" in home
    sm = dict(writes)["/www/wwwroot/g8.com/sitemap.xml"]
    assert "g8.com/</loc>" in sm and "g8.com/vs/</loc>" in sm                # ранее опубликованная — в sitemap


def test_theme_salt_is_full_digest_not_16_bit_prefix():
    """Minor: два домена с совпавшими 16 битами старого префикса не должны делить имена классов/ассетов."""
    import hashlib
    seen = {}
    pair = None
    for i in range(5000):
        d = f"collide{i}.com"
        key = hashlib.sha256(d.encode()).digest()[4:6]
        if key in seen:
            pair = (seen[key], d)
            break
        seen[key] = d
    assert pair, "коллизия 16 бит находится за ~300 доменов"
    a, b = theme_for(pair[0]), theme_for(pair[1])
    assert a.k("hd") != b.k("hd") and a.asset("site.css") != b.asset("site.css")
    assert len(a.prefix) == 64
