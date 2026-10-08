"""M4/M5 — сборка статического сайта: тема по сиду домена, CSS, документ страницы, sitemap/robots.

Что и зачем:
  * `theme_for(domain)` — палитра, шрифтовой стек, раскладка (top/center/side), порядок блоков и
    ПРЕФИКС css-классов выбираются детерминированно по хэшу домена. Инвариант независимости
    портфеля (footprint): два сайта не должны отдавать побайтово один и тот же CSS/разметку.
  * `render_document` — полный HTML: doctype, lang, title, description, canonical, Open Graph,
    favicon, один общий CSS-файл, шапка с навигацией ПО СТРАНИЦАМ ЭТОГО ЖЕ САЙТА, h1 из title,
    футер. Ссылок на другие домены портфеля здесь нет и быть не может (nav — только свой сайт).
  * `build_assets` / `build_site_files` — набор файлов для деплоя (assets/*, sitemap.xml, robots.txt).
Внешних запросов нет: ни CDN, ни веб-шрифтов — только системные стеки.
Санитайзер тела страницы (content._sanitize) сюда не входит: builder получает УЖЕ чистый HTML.
"""
import hashlib
import hmac
import html
import re
from dataclasses import dataclass, field

from app.services.locales import OG_LOCALE, norm_lang, t

TEMPLATE_VERSION = "1"        # менять при изменении разметки/CSS: попадает в assets/.version

# bg/sf — фон и «поверхность», fg/mu — текст и приглушённый, ac/ac2 — акценты, acf — текст на акценте
PALETTES = [
    dict(name="ocean", bg="#ffffff", surface="#f1f6fb", fg="#14202b", muted="#5a6b7a", accent="#1f6fb2",
         accent2="#4aa3df", accent_fg="#ffffff", border="#d9e3ec"),
    dict(name="forest", bg="#fbfdfb", surface="#eff6f1", fg="#16241b", muted="#566a5c", accent="#2e7d4f",
         accent2="#8cc9a1", accent_fg="#ffffff", border="#d6e4da"),
    dict(name="sunset", bg="#fffdfa", surface="#fdf3ea", fg="#2b1d14", muted="#7a6455", accent="#c4571c",
         accent2="#f2a65a", accent_fg="#ffffff", border="#efdccb"),
    dict(name="plum", bg="#fdfcff", surface="#f4f0fa", fg="#221b2e", muted="#675d78", accent="#6b3fa0",
         accent2="#b68ae0", accent_fg="#ffffff", border="#e3dbef"),
    dict(name="slate", bg="#fafafa", surface="#eef0f2", fg="#1c2024", muted="#626a73", accent="#3d4a5c",
         accent2="#8fa0b5", accent_fg="#ffffff", border="#dcdfe3"),
    dict(name="rose", bg="#fffcfd", surface="#fbeff2", fg="#2a1519", muted="#7a5a62", accent="#b02a4d",
         accent2="#ee8aa4", accent_fg="#ffffff", border="#f0d9df"),
]
FONTS = [
    ('system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif',
     'system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif'),
    ('Georgia,"Times New Roman",Times,serif', '"Helvetica Neue",Arial,sans-serif'),
    ('"Trebuchet MS","Lucida Grande",Verdana,sans-serif', 'Georgia,"Times New Roman",serif'),
    ('"Segoe UI",Tahoma,Verdana,sans-serif', '"Segoe UI",Tahoma,Verdana,sans-serif'),
]
LAYOUTS = ("top", "center", "side")
RADII = ("4px", "10px", "16px")


@dataclass(frozen=True)
class Theme:
    pal: dict
    font: str
    head_font: str
    layout: str
    radius: str
    prefix: str
    hero_first: bool          # картинка над h1 или под ним
    benefits_first: bool      # блок иконок преимуществ до или после статьи

    def k(self, name: str) -> str:
        """Имя css-класса: у каждого домена целиком своё (хеш от соли сайта), без общей схемы
        «префикс-роль» — иначе по регулярке `k[0-9a-f]{4}-(hd|nav|…)` сайты портфеля сцепляются."""
        return "c" + hashlib.sha256(f"{self.prefix}:{name}".encode()).hexdigest()[:8]

    def asset(self, name: str) -> str:
        """Путь файла в webroot: имена ассетов сайта тоже выводятся из сида (не общие site.css/hero.svg)."""
        ext = "." + name.rsplit(".", 1)[1] if "." in name else ""
        return f"assets/a{hashlib.sha256(f'{self.prefix}/{name}'.encode()).hexdigest()[:10]}{ext}"


def _seed(domain: str) -> str:
    d = (domain or "").strip().lower()
    return d[4:] if d.startswith("www.") else d


def theme_for(domain: str) -> Theme:
    h = hashlib.sha256(_seed(domain).encode()).digest()
    font, head = FONTS[h[2] % len(FONTS)]
    return Theme(pal=PALETTES[h[0] % len(PALETTES)], font=font, head_font=head,
                 layout=LAYOUTS[h[1] % len(LAYOUTS)], radius=RADII[h[3] % len(RADII)],
                 prefix=h.hex(),   # ПОЛНЫЙ дайджест сида (соль имён), не 16 бит — коллизии префикса исключены
                  hero_first=bool(h[6] & 1), benefits_first=bool(h[7] & 1))


@dataclass
class SiteContext:
    domain: str = ""                      # пусто -> одиночный документ без canonical/OG/шапки
    theme: Theme = field(default_factory=lambda: theme_for(""))
    nav: list = field(default_factory=list)      # [(url_path, подпись)] — страницы ЭТОГО сайта
    assets: bool = False                  # True: CSS/картинки лежат файлами (деплой), иначе inline
    chart: tuple | None = None            # (ширина, высота) графика фактов, если он есть в assets


def make_ctx(domain: str, nav: list, lang: str, brand: str = "") -> SiteContext:
    from app.services.vertical_data import facts_for
    return SiteContext(domain=_seed(domain), theme=theme_for(domain), nav=nav, assets=True,
                       chart=chart_size() if brand and facts_for(brand) else None)


# ── CSS ───────────────────────────────────────────────────────────────────────

_BASE_CSS = """*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--fg);font:1.0625rem/1.65 var(--font)}
.§shell{max-width:var(--w);margin:0 auto;padding:0 1rem}
.§hd{display:flex;flex-wrap:wrap;gap:.5rem 1.5rem;align-items:center;justify-content:space-between;padding:1rem 0;border-bottom:1px solid var(--bd)}
.§brand{font:700 1.15rem var(--hfont);color:var(--fg);text-decoration:none}
.§nav{display:flex;flex-wrap:wrap;gap:.25rem 1.1rem}
.§nav a{color:var(--mu);text-decoration:none;padding:.2rem 0;border-bottom:2px solid transparent}
.§nav a:hover,.§nav a[aria-current]{color:var(--fg);border-color:var(--ac)}
h1,h2,h3,h4{font-family:var(--hfont);line-height:1.25}
h1{font-size:clamp(1.65rem,4vw,2.35rem);margin:1.25rem 0 .75rem}
h2{margin-top:2rem}
a{color:var(--ac)}
img{max-width:100%;height:auto}
.§hero{display:block;width:100%;border-radius:var(--r);margin:1.25rem 0 0}
article table{border-collapse:collapse;display:block;overflow-x:auto}
th,td{border:1px solid var(--bd);padding:.5rem .75rem;text-align:left}
blockquote{margin:1rem 0;padding:.1rem 1rem;border-left:3px solid var(--ac);color:var(--mu)}
code,pre{background:var(--sf);border-radius:4px}
pre{padding:1rem;overflow:auto}
.§disc{font-size:.92rem;color:var(--mu);border-left:3px solid var(--ac);padding:.2rem .75rem;margin:1rem 0}
.§ben{list-style:none;display:grid;grid-template-columns:repeat(auto-fit,minmax(10rem,1fr));gap:1rem;padding:0;margin:1.5rem 0}
.§ben li{background:var(--sf);border:1px solid var(--bd);border-radius:var(--r);padding:1rem;text-align:center;font-weight:600}
.§ben img{display:block;margin:0 auto .5rem}
.§off{background:var(--sf);border:1px solid var(--bd);border-radius:var(--r);padding:1rem 1.25rem;margin:2rem 0}
.§off a{display:inline-block;background:var(--ac);color:var(--acf);padding:.6rem 1.1rem;border-radius:var(--r);text-decoration:none;font-weight:600}
.§fig{margin:1.5rem 0}
.§fig figcaption{color:var(--mu);font-size:.9rem}
.§ft{border-top:1px solid var(--bd);margin-top:3rem;padding:1.5rem 0 2.5rem;color:var(--mu);font-size:.9rem}
.§ft p{margin:.4rem 0}
"""
_LAYOUT_CSS = {
    "top": "",
    "center": ".§hd{flex-direction:column;text-align:center}.§nav{justify-content:center}h1{text-align:center}",
    "side": ("@media(min-width:62rem){.§wrap{display:grid;grid-template-columns:14rem minmax(0,1fr);gap:2.5rem}"
             ".§hd{flex-direction:column;align-items:flex-start;justify-content:flex-start;border-bottom:0;"
             "border-right:1px solid var(--bd);padding-right:1.5rem}.§nav{flex-direction:column}}"),
}
_WIDTH = {"top": "48rem", "center": "44rem", "side": "72rem"}


def build_css(theme: Theme) -> str:
    p = theme.pal
    root = (f":root{{--bg:{p['bg']};--sf:{p['surface']};--fg:{p['fg']};--mu:{p['muted']};--ac:{p['accent']};"
            f"--ac2:{p['accent2']};--acf:{p['accent_fg']};--bd:{p['border']};--r:{theme.radius};"
            f"--font:{theme.font};--hfont:{theme.head_font};--w:{_WIDTH[theme.layout]}}}\n")
    out = root + _BASE_CSS + _LAYOUT_CSS[theme.layout] + "\n"
    return re.sub(r"§(\w+)", lambda m: theme.k(m.group(1)), out)


# ── мелочи страницы ───────────────────────────────────────────────────────────

def page_href(url_path: str) -> str:
    """'/vs' -> '/vs/' (файл лежит как <slug>/index.html, canonical и nav — с косой на конце)."""
    s = (url_path or "").strip("/")
    return f"/{s}/" if s else "/"


def nav_label(url_path: str, title: str, lang: str) -> str:
    key = {"/": "nav_review", "/vs": "nav_comparison", "/setup": "nav_howto"}.get(
        "/" + (url_path or "").strip("/"))
    return t(lang, key) if key else (title or url_path)[:40]


def description_of(body: str | None, title: str | None, limit: int = 155) -> str:
    """Мета-описание: первый непустой <p> тела, без тегов, ≤155 символов по границе слова."""
    m = re.search(r"<p[^>]*>(.*?)</p>", body or "", re.S | re.I)
    txt = html.unescape(re.sub(r"<[^>]+>", " ", m.group(1) if m else "")).split()
    txt = " ".join(txt) or (title or "")
    if len(txt) > limit:
        txt = txt[:limit].rsplit(" ", 1)[0].rstrip(",;:.-—") + "…"
    return txt


def chart_size() -> tuple:
    from app.services.vertical_data import VPN_FACTS
    return 760, 56 + 34 * len(VPN_FACTS)


CUR = " aria-current='page'"


def _e(s) -> str:
    return html.escape(str(s or ""), quote=True)


def render_document(ctx: SiteContext, lang: str, *, title: str, description: str, url_path: str,
                    body: str, top_note: str, offer_block: str, footer_note: str,
                    build_id: str | None = None, brand: str = "") -> str:
    """Полный HTML страницы. body/top_note/offer_block уже безопасны (санитайзер/escape в content)."""
    th, k = ctx.theme, ctx.theme.k
    lg = norm_lang(lang)
    head = ["<meta charset='utf-8'>", "<meta name='viewport' content='width=device-width, initial-scale=1'>"]
    if build_id:
        head.append(f"<meta name='build-id' content='{_e(build_id)}'>")
    head.append(f"<title>{_e(title)}</title>")
    head.append(f"<meta name='description' content='{_e(description)}'>")
    if ctx.domain:
        url = f"https://{ctx.domain}{page_href(url_path)}"
        head += [f"<link rel='canonical' href='{_e(url)}'>",
                 "<meta property='og:type' content='article'>",
                 f"<meta property='og:title' content='{_e(title)}'>",
                 f"<meta property='og:description' content='{_e(description)}'>",
                 f"<meta property='og:url' content='{_e(url)}'>",
                 f"<meta property='og:locale' content='{OG_LOCALE[lg]}'>",
                 f"<meta property='og:site_name' content='{_e(ctx.domain)}'>",
                 "<meta name='twitter:card' content='summary_large_image'>"]
        if ctx.assets:
            head.append(f"<meta property='og:image' content='https://{ctx.domain}/{th.asset('og.svg')}'>")
    if ctx.assets:
        head += [f"<link rel='icon' type='image/svg+xml' href='/{th.asset('favicon.svg')}'>",
                 f"<link rel='stylesheet' href='/{th.asset('s.css')}'>"]
    else:
        head.append(f"<style>{build_css(th)}</style>")

    hero = ""
    if ctx.assets:
        hero = (f"<img class='{k('hero')}' src='/{th.asset('hero.svg')}' width='1200' height='360' loading='lazy' "
                f"alt='{_e(t(lg, 'alt_hero', title=title))}'>")
    h1 = f"<h1>{_e(title)}</h1>"
    lead = hero + h1 if th.hero_first else h1 + hero

    benefits = chart = ""
    if ctx.assets and (url_path or "/").strip("/") == "":
        items = "".join(
            f"<li><img src='/{th.asset(f'icon-{n}.svg')}' width='48' height='48' loading='lazy' "
            f"alt='{_e(t(lg, 'b_' + n))}'>{_e(t(lg, 'b_' + n))}</li>" for n in ("privacy", "speed", "access"))
        benefits = f"<ul class='{k('ben')}'>{items}</ul>"
    if ctx.assets and ctx.chart and (url_path or "").strip("/") == "vs":
        w, h = ctx.chart
        chart = (f"<figure class='{k('fig')}'><img src='/{th.asset('chart-servers.svg')}' width='{w}' height='{h}' "
                 f"loading='lazy' alt='{_e(t(lg, 'alt_chart'))}'>"
                 f"<figcaption>{_e(t(lg, 'facts_title'))} ({_e(t(lg, 'facts_servers'))})</figcaption></figure>")

    article = f"<article>{body}</article>"
    main_parts = [lead, top_note, (benefits + article) if th.benefits_first else (article + benefits),
                  chart, offer_block]

    header = ""
    if ctx.domain:
        cur = (url_path or "").strip("/")
        links = "".join(
            f"<a href='{_e(page_href(p))}'{CUR if p.strip('/') == cur else ''}>{_e(lbl)}</a>"
            for p, lbl in ctx.nav) if len(ctx.nav) > 1 else ""
        nav = f"<nav class='{k('nav')}' aria-label='{_e(t(lg, 'menu'))}'>{links}</nav>" if links else ""
        header = f"<header class='{k('hd')}'><a class='{k('brand')}' href='/'>{_e(ctx.domain)}</a>{nav}</header>"
    copy = f"<p>© {_e(ctx.domain)} · {_e(t(lg, 'footer'))}</p>" if ctx.domain else ""
    footer = f"<footer class='{k('ft')}'><small>{footer_note}</small>{copy}</footer>"
    return (f"<!doctype html><html lang='{_e(lg)}'><head>{''.join(head)}</head><body>"
            f"<div class='{k('shell')}'><div class='{k('wrap')}'>{header}<main>{''.join(main_parts)}</main></div>"
            f"{footer}</div></body></html>")


# ── набор файлов сайта ────────────────────────────────────────────────────────

def marker_path(domain: str) -> str:
    """Путь маркера версии сайта (деплоится ПОСЛЕДНИМ среди assets)."""
    return theme_for(domain).asset("v")


def build_assets(domain: str, lang: str, brand: str = "", home_title: str = "",
                 provider: str = "svg-local") -> dict:
    """{'assets/site.css': ..., ...} — общие файлы сайта. Детерминированно по домену/языку."""
    from app.services.images import get_provider
    from app.services.vertical_data import VPN_FACTS, facts_for
    th, seed, img = theme_for(domain), _seed(domain), get_provider(provider)
    pal = th.pal
    files = {
        th.asset("s.css"): build_css(th),
        th.asset("favicon.svg"): img.favicon(seed, pal, seed[:1]),
        th.asset("hero.svg"): img.hero(seed, pal),
        th.asset("og.svg"): img.og(seed, pal, home_title or seed),
    }
    for n in ("privacy", "speed", "access"):
        files[th.asset(f"icon-{n}.svg")] = img.icon(n, pal)
    if brand and facts_for(brand):
        items = sorted(((v["brand"], int(re.sub(r"\D", "", str(v["servers"])) or 0))
                        for v in VPN_FACTS.values()), key=lambda x: -x[1])
        files[th.asset("chart-servers.svg")] = img.bar_chart(
            f"{t(lang, 'facts_title')} — {t(lang, 'facts_servers')}", items, pal)
    digest = hashlib.sha256("".join(f"{p}\0{c}\0" for p, c in sorted(files.items())).encode()).hexdigest()[:16]
    # маркер версии: имя и формат выводятся из сида (общий /assets/.version сцепил бы сайты портфеля)
    files[marker_path(domain)] = hashlib.sha256(f"{TEMPLATE_VERSION}:{seed}:{digest}".encode()).hexdigest() + "\n"
    return files


def indexnow_key(domain: str) -> str | None:
    """Ключ IndexNow сайта: 32 hex = HMAC-SHA256(INDEXNOW_SECRET, сид домена). Детерминирован без БД
    и без общего ключа на портфель (ключ-на-всех связал бы сайты). Сам ключ публичен (протокол требует
    файл `/<key>.txt`), но без секрета установки схему не угадать и по чужому ключу не сопоставить
    сайты портфеля (инвариант независимости). Секрет пуст -> None: файла-ключа и пинга нет."""
    from app.config import settings
    secret = (settings.INDEXNOW_SECRET or "").strip()
    if not secret:
        return None
    return hmac.new(secret.encode(), _seed(domain).encode(), hashlib.sha256).hexdigest()[:32]


def build_site_files(domain: str, url_paths: list) -> dict:
    """robots.txt (со ссылкой на sitemap), sitemap.xml по ОПУБЛИКОВАННЫМ страницам и файл-ключ IndexNow."""
    d = _seed(domain)
    key = indexnow_key(domain)
    out = {"robots.txt": f"User-agent: *\nAllow: /\n\nSitemap: https://{d}/sitemap.xml\n"}
    if key:
        out[f"{key}.txt"] = key
    if url_paths:
        locs = "".join(f"<url><loc>{_e(f'https://{d}{page_href(p)}')}</loc></url>"
                       for p in sorted(set(url_paths), key=lambda x: (x != "/", x)))
        out["sitemap.xml"] = ("<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
                              f"<urlset xmlns=\"http://www.sitemaps.org/schemas/sitemap/0.9\">{locs}</urlset>\n")
    return out
