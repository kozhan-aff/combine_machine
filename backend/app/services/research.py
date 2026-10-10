"""Досье конкурентов для сайта (спека 2026-10-10 §4): выдача по 4 запросам на языке рынка -> 5 живых
страниц на запрос -> текст/структура/факты/CSS-токены (+ скриншоты по тумблеру) -> site_research.
Логика здесь; транспорт — A-Parser, SearXNG, Browserless. Пустое досье = причина словами, не исключение.

Два нюанса живой выдачи и безопасности:
- A-Parser (SE::Google) отдаёт только редиректы `https://www.google.com/goto?url=…` — хост у всех один,
  поэтому шум/бренд/дедуп по такому URL судить нельзя: сначала скачиваем, реальный домен берём со страницы
  (canonical / og:url), и уже по нему фильтруем.
- URL приходят из выдачи (чужой ввод) и уходят в A-Parser/Browserless, которые стоят в LAN бокса —
  перед любым запросом URL проходит `_safe_url` (SSRF-гард): только http(s), не localhost/.local/.internal,
  не приватный/loopback/link-local IP-литерал, а для имени — ВСЕ его DNS-адреса обязаны быть глобальными
  (ошибка резолвера = небезопасно). Небезопасный URL молча пропускается, как шум."""
import ipaddress
import logging
import re
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

from app.config import settings
from app.services.competitor import _NOISE, _norm
from app.services.locales import t, country_name, supported
from app.services import research_extract as rx

log = logging.getLogger(__name__)
KINDS = ("review", "comparison", "howto", "market")
PER_QUERY = 5
MIN_WORDS = 300
_EXTRA_NOISE = ("apps.apple.", "play.google.", "chrome.google.", "github.", "amazon.", "aliexpress.")

# Резолвер — функция, а не `_resolve = socket.getaddrinfo`: ссылка, захваченная при импорте, обходила бы
# подмену `socket.getaddrinfo` рубильником сети в тестах (conftest). Шов для тестов остаётся.
def _resolve(*a, **kw):
    return socket.getaddrinfo(*a, **kw)


_EMPTY_REASON = ("ни одной живой страницы конкурентов ни по одному запросу "
                 "(SERP пуст или страницы не скачались) — генерация без досье не идёт")
_TAG_RE = re.compile(r"<(link|meta)\b[^>]*>", re.I)
# og:site_name / twitter:site площадок, чьи страницы не конкуренты (лента канала, видео, пост). Сравнение — по
# `_norm` целиком: «Telegram», «@Telegram», «Яндекс Дзен» -> telegram / яндексдзен.
_PLATFORMS = frozenset(_norm(x) for x in (
    "telegram", "youtube", "vk", "vkontakte", "reddit", "facebook", "twitter", "x", "tiktok", "instagram",
    "dzen", "яндекс дзен", "pikabu", "habr"))
_MAX_HTML = 600_000      # потолок перед extract_all: faq() квадратичен на патологическом HTML (мегабайты без тегов)


def _aparser():
    from app.integrations.aparser import AParserClient
    return AParserClient()


def _searxng():
    from app.integrations.searxng import SearxngClient
    return SearxngClient()


def _browserless():
    from app.integrations.browserless import BrowserlessClient
    return BrowserlessClient()


def queries_for(brand: str, lang: str, country: str | None) -> list[tuple[str, str]]:
    if not supported(lang):
        raise ValueError(f"язык «{lang}» без словаря запросов — добавь в services/locales.py")
    c = country_name(lang, country)          # пустая страна -> «лучший VPN» без хвоста, дефолтов не плодим
    return [("review", t(lang, "q_review", brand=brand)),
            ("comparison", t(lang, "q_comparison", brand=brand)),
            ("howto", t(lang, "q_howto", brand=brand)),
            ("market", t(lang, "q_market", country=c).strip())]


def _safe_url(url: str) -> bool:
    """SSRF-гард: можно ли отдавать этот URL A-Parser/Browserless (они сидят в LAN бокса)."""
    try:
        u = urlparse((url or "").strip())
        host = (u.hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    if u.scheme not in ("http", "https") or not host:
        return False
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        # 2130706433 / 0x7f000001 / 127.1 — числовые формы IP, которые ip_address не разбирает, но резолвер съест
        if re.fullmatch(r"[0-9.]+|0x[0-9a-f.]+", host):
            return False
        return _resolves_global(host)
    ip = getattr(ip, "ipv4_mapped", None) or ip
    # то же правило, что для имён: только глобальный адрес (is_private не ловит CGNAT 100.64/10)
    return ip.is_global and not ip.is_multicast


def _resolves_global(host: str) -> bool:
    """Имя годится, только если ВСЕ его адреса глобальные: A-запись на 192.168.x — тот же SSRF, что и литерал."""
    try:
        infos = _resolve(host, None, proto=socket.IPPROTO_TCP)
        addrs = [ipaddress.ip_address(sa[0].split("%")[0]) for _f, _t, _p, _c, sa in infos]
    except Exception:  # noqa: BLE001  gaierror, мусор от резолвера — всё равно небезопасно
        return False
    if not addrs:
        return False
    for ip in addrs:
        ip = getattr(ip, "ipv4_mapped", None) or ip
        if not ip.is_global or ip.is_multicast:
            return False
    return True


def _serp(query: str, lang: str) -> list[str]:
    urls: list[str] = []
    try:
        urls = _aparser().serp_urls(query, limit=10)
    except Exception as e:  # noqa: BLE001
        log.warning("research: A-Parser SERP %r: %s", query, e)
    if not urls:
        try:
            urls = [r.get("url") for r in _searxng().search(query, language=lang) if r.get("url")]
        except Exception as e:  # noqa: BLE001
            log.warning("research: SearXNG %r: %s", query, e)
    return urls[:10]


def _host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower().removeprefix("www.")
    except ValueError:
        return ""


def _is_goto(url: str) -> bool:
    """Редирект выдачи A-Parser: хост ничего не говорит о реальной странице."""
    u = urlparse(url)
    return (u.hostname or "").lower().endswith("google.com") and u.path.startswith("/goto")


def _blocked_host(host: str, brand_key: str, own_host: str) -> bool:
    """Шум (соцсети, магазины приложений…), сайт самого бренда или домен оффера — не конкурент."""
    if not host:
        return True
    if any(n.rstrip(".") in host for n in _NOISE + _EXTRA_NOISE):
        return True
    if own_host and (host == own_host or host.endswith("." + own_host)):
        return True
    return bool(brand_key) and brand_key in _norm(host)


def _attr(tag: str, name: str) -> str | None:
    m = re.search(rf"""\b{name}\s*=\s*(?:"([^"]*)"|'([^']*)')""", tag, re.I)
    return (m.group(1) or m.group(2)) if m else None


def _absolute(href: str | None) -> bool:
    return bool(href) and href.startswith(("http://", "https://")) and bool(_host(href))


def _real_page(html: str, url: str) -> tuple[str, str, list[str]]:
    """(домен, final_url, имена площадки) реальной страницы за goto-редиректом — по canonical / og:url.
    Домен «из ссылок» не берём: обзор бренда ссылается на сайт бренда не меньше трёх раз.
    Относительный canonical (живой случай: лента t.me/s/durevvpn отдаёт `/s/durevvpn?before=…`) резолвится
    `urljoin` от абсолютного og:url; нет ни того ни другого -> ("", url, …): домен — заглушка goto{rank}.
    Имена площадки (og:site_name, twitter:site) отдаются наружу, только когда canonical пуст/относителен, —
    по ним `_platform_or_brand` отсеивает страницу, чей домен по canonical не установлен."""
    canonical = og_url = None
    names: list[str] = []
    for m in _TAG_RE.finditer(html or ""):
        tag = m.group(0)
        low = tag.lower()
        if low.startswith("<link"):
            if canonical is None and (_attr(tag, "rel") or "").lower() == "canonical":
                canonical = (_attr(tag, "href") or "").strip()
            continue
        key = (_attr(tag, "property") or _attr(tag, "name") or "").lower()
        val = (_attr(tag, "content") or "").strip()
        if key == "og:url" and og_url is None:
            og_url = val
        elif key in ("og:site_name", "twitter:site") and val:
            names.append(val)
    if _absolute(canonical):              # домен установлен — судят фильтры по хосту, имена площадки не нужны
        return _host(canonical), canonical, []
    if _absolute(og_url):
        final = urljoin(og_url, canonical) if canonical else og_url
        if _absolute(final):
            return _host(final), final, names
        return _host(og_url), og_url, names
    return "", url, names


def _platform_or_brand(names: list[str], brand_key: str) -> bool:
    """og:site_name / twitter:site — площадка (Telegram, YouTube…) или сам бренд -> не конкурент."""
    for n in names:
        k = _norm(n)
        if k and (k in _PLATFORMS or (brand_key and brand_key in k)):
            return True
    return False


def _css_for(ap, html: str, url: str) -> list[str]:
    out = []
    for link in rx.stylesheet_links(html, url):
        if not _safe_url(link):          # ссылки на стили — тоже чужой ввод; LAN/loopback не тянем
            continue
        try:
            css = ap.fetch_html(link)
        except Exception:  # noqa: BLE001
            css = None
        if css and len(css) <= 300_000:
            out.append(css)
    return out


def _shot(bl, url: str, site_id: int, kind: str, rank: int) -> tuple[str | None, str | None]:
    """(путь, замечание). Browserless вниз — замечание, досье без скриншота."""
    try:
        # только первый экран: полностраничный PNG длинной страницы выходит за потолок шлюза 2000×2000 px и
        # не читается критиком (docs/v2/research/research-live-formats-2026-10.md §2–3); подпись на /settings/keys
        # тоже обещает «первый экран»
        png = bl.screenshot(url, width=1366, height=768, full_page=False)
        d = Path(settings.RESEARCH_DIR) / str(site_id)
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{kind}-{rank}.png"
        p.write_bytes(png)
        return str(p), None
    except Exception as e:  # noqa: BLE001
        return None, f"скриншот не снят: {type(e).__name__}: {e}"[:200]


def is_fresh(db, site_id: int) -> bool:
    from sqlalchemy import select, func
    from app.models.research import SiteResearch
    newest = db.scalar(select(func.max(SiteResearch.fetched_at)).where(SiteResearch.site_id == site_id))
    if newest is None:
        return False
    if newest.tzinfo is None:
        newest = newest.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - newest < timedelta(days=settings.RESEARCH_MAX_AGE_DAYS)


def dossier(db, site_id: int) -> list:
    from sqlalchemy import select
    from app.models.research import SiteResearch
    rows = db.execute(select(SiteResearch).where(SiteResearch.site_id == site_id)).scalars().all()
    order = {k: i for i, k in enumerate(KINDS)}
    return sorted(rows, key=lambda r: (order.get(r.kind, 9), r.rank))


def summary(db, site_id: int) -> dict:
    rows = dossier(db, site_id)
    kinds: dict = {}
    for r in rows:
        kinds[r.kind] = kinds.get(r.kind, 0) + 1
    return {"rows": len(rows), "kinds": kinds, "fresh": is_fresh(db, site_id) if rows else False,
            "fetched_at": max((r.fetched_at for r in rows), default=None),
            "screenshots": sum(1 for r in rows if r.screenshot_path)}


def build_dossier(site_id: int, *, force: bool = False) -> dict:
    from app.db import SessionLocal
    from app.models.site import Site
    from app.models.domain import Domain
    from app.models.research import SiteResearch
    from app.services import jobs
    from app.services.content import site_offer
    from app.services.locales import resolve_lang

    with SessionLocal() as db:
        site = db.get(Site, site_id)
        if site is None:
            raise ValueError(f"site {site_id} not found")
        if not force and is_fresh(db, site_id):
            return {"status": "fresh", "rows": summary(db, site_id)["rows"], "reason": None, "warnings": []}
        offer = site_offer(db, site)
        if offer is None:
            raise ValueError(f"сайт #{site_id}: оффер не привязан — досье не по чему собирать")
        dom = db.get(Domain, site.domain_id)
        lang = resolve_lang(None, dom.market_lang if dom else None, offer.language)
        brand, country, aff = offer.brand, offer.country, offer.affiliate_link
    queries = queries_for(brand, lang, country)
    brand_key = _norm(brand)
    own_host = _host(aff or "")
    shots_on = str(settings.RESEARCH_SCREENSHOTS).strip().lower() in ("1", "true", "yes", "on")
    ap = _aparser()
    bl = _browserless() if shots_on else None
    warnings: list[str] = []
    rows: list[dict] = []
    was_cancelled = False

    with jobs.track("research", trigger="manual") as run:
        jobs.report(run, done=0, total=len(queries))
        try:
            for qi, (kind, query) in enumerate(queries):
                if jobs.cancelled(run):
                    raise jobs.Cancelled()
                jobs.report(run, done=qi, total=len(queries), current=query)
                seen_domains: set[str] = set()
                rank = 0
                for url in _serp(query, lang):
                    if rank >= PER_QUERY:
                        break
                    if not _safe_url(url):
                        continue
                    goto = _is_goto(url)
                    host = _host(url)
                    if not goto:                       # обычный URL: шум/бренд/дедуп — ДО скачивания
                        if _blocked_host(host, brand_key, own_host) or host in seen_domains:
                            continue
                    try:
                        html = ap.fetch_html(url)
                    except Exception as e:  # noqa: BLE001
                        log.warning("research: fetch %s: %s", url, e)
                        html = None
                    if not html:
                        continue
                    final_url, reliable = url, True
                    if goto:                           # редирект: судим по реальной странице
                        host, final_url, names = _real_page(html, url)
                        reliable = bool(host)
                        if _platform_or_brand(names, brand_key):     # лента канала / страница бренда
                            continue
                        if reliable and (_blocked_host(host, brand_key, own_host) or host in seen_domains):
                            continue
                    html = html[:_MAX_HTML]
                    data = rx.extract_all(html, _css_for(ap, html, final_url))
                    if data["words"] < MIN_WORDS:
                        continue
                    rank += 1
                    if reliable:
                        seen_domains.add(host)
                    else:                              # домен не установлен -> заглушка, дедуп по ней не делаем
                        host = f"goto{rank}"
                    shot_url = final_url if _safe_url(final_url) else url
                    shot, note = _shot(bl, shot_url, site_id, kind, rank) if bl else (None, None)
                    if note:
                        warnings.append(f"{kind}#{rank}: {note}")
                    rows.append({"site_id": site_id, "kind": kind, "query": query, "rank": rank, "url": url,
                                 "final_url": final_url, "domain": host[:255], "screenshot_path": shot,
                                 "note": note, **data})
                if rank == 0:
                    warnings.append(f"{kind}: по запросу «{query}» ни одной живой страницы")
            jobs.report(run, done=len(queries), total=len(queries), current="")
            if not rows:           # причина и замечания должны дожить до панели: dict из spawn никто не читает
                jobs.report(run, message=_EMPTY_REASON)
                jobs.finish(run, "done_warn")
            elif warnings:
                jobs.report(run, message=f"{len(rows)} источников; замечания: {'; '.join(warnings)}")
                jobs.finish(run, "done_warn")
            else:
                jobs.report(run, message=f"{len(rows)} источников")
        except jobs.Cancelled:
            was_cancelled = True
            jobs.report(run, message="отменено оператором — прежнее досье не тронуто")
            raise

    if was_cancelled:        # track глотает Cancelled; прежнее досье не трогаем, недособранное не сохраняем
        return {"status": "empty", "rows": 0, "reason": "сборка досье отменена оператором — прежние данные не тронуты",
                "warnings": warnings}
    if not rows:
        return {"status": "empty", "rows": 0, "reason": _EMPTY_REASON, "warnings": warnings}
    with SessionLocal() as db:
        db.query(SiteResearch).filter(SiteResearch.site_id == site_id).delete()
        db.add_all(SiteResearch(**r) for r in rows)
        db.commit()
    return {"status": "done", "rows": len(rows), "reason": None, "warnings": warnings}
