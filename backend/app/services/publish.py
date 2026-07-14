"""M5 — Publish & Monitor. Deploy ONLY 'edited' pages -> docroot (aaPanel file API).

Hard gate: a page publishes ONLY from status 'edited' (never 'draft'). Then index
monitoring via SearXNG `site:` (GSC excluded from v1 — manual/free check). See PLAN §2.
"""
from datetime import datetime, timezone
from urllib.parse import urlparse, unquote


def _norm_path(path: str | None) -> str:
    """Канон пути для сравнения «та же ли это страница».

    Одну и ту же страницу выдача показывает в РАЗНЫХ формах, и слишком строгое сравнение —
    такая же ложь, как слишком слабое, только в другую сторону («страница в индексе, а машина
    говорит нет» — оператор чинит то, что работает). ОДНОЙ И ТОЙ ЖЕ страницей считаем:
      · со слэшем на конце и без             (/setup/  == /setup)
      · с `index.html` и без                 (/setup/index.html == /setup)
      · в percent-encoding и без             (/%D0%B0 == /а)
      · в разном регистре                    (/Setup == /setup — наши слуги всегда lowercase,
                                              а движки возвращают путь как попало)
      · с ?utm=… и #якорем                   (трекинг-хвост и фрагмент — не страница)
      · http vs https                        (схему не смотрим вовсе)
    РАЗНЫМИ страницами остаются пути, различающиеся хоть одним сегментом: /setup != /setup/windows
    и != /. Ради этого всё и затевалось: главная в выдаче раньше помечала `/setup`
    проиндексированной — машина заявляла, что видела страницу в индексе, ни разу её там не увидев.
    """
    p = unquote(path or "").split("?", 1)[0].split("#", 1)[0].strip().lower()
    if p.endswith("/index.html") or p == "index.html":
        p = p[: -len("index.html")]
    return "/" + p.strip("/")


def _same_page(url: str | None, domain: str, url_path: str) -> bool:
    """URL из выдачи = ИМЕННО эта страница этого сайта (хост И путь), а не просто этот сайт.

    Хост судит host_matches (он же бережёт от mydomain.ru.evil.com); `www.` и поддомены он
    считает тем же сайтом — своих поддоменов мы не поднимаем, а www — законный алиас.
    """
    from app.integrations.searxng import host_matches
    if not host_matches(url, domain):
        return False
    return _norm_path(urlparse(url or "").path) == _norm_path(url_path)


def _target_path(doc_root: str, url_path: str) -> str:
    """docroot + url_path -> index.html file path. '/' -> docroot/index.html."""
    sub = url_path.strip("/")
    return f"{doc_root.rstrip('/')}/{sub + '/' if sub else ''}index.html"


def _pick_offer(db, site_id: int):
    from sqlalchemy import select
    from app.models.offer import Offer, SiteOffer
    # deterministic order_by(Offer.id): MUST match content.generate_site's pick, or the page
    # is written about one brand and the sponsored link points at another.
    off = db.execute(
        select(Offer).join(SiteOffer, SiteOffer.offer_id == Offer.id)
        .where(SiteOffer.site_id == site_id, Offer.active.is_(True))
        .order_by(Offer.id).limit(1)
    ).scalar_one_or_none()
    if off is None:  # fall back to any active offer
        off = db.execute(
            select(Offer).where(Offer.active.is_(True)).order_by(Offer.id).limit(1)
        ).scalar_one_or_none()
    return off


def publish_site(site_id: int) -> dict:
    """Deploy every 'edited' page of a site. Refuses if there are none (the edit gate)."""
    from sqlalchemy import select
    from app.db import SessionLocal
    from app.models.site import Site, Page
    from app.models.domain import Domain
    from app.integrations.aapanel import AaPanelClient
    from app.services.content import render_html

    with SessionLocal() as db:
        site = db.get(Site, site_id)
        if site is None:
            raise ValueError(f"site {site_id} not found")
        domain = db.get(Domain, site.domain_id).domain
        pages = db.execute(select(Page).where(
            Page.site_id == site_id, Page.status == "edited")).scalars().all()
        if not pages:
            return {"status": "no_edited_pages",
                    "hint": "гейт: публикуются только страницы в статусе 'edited'"}

        offer = _pick_offer(db, site_id)
        # <html lang=...>: offer carries the target ISO language (one domain = one geo/lang);
        # no site.language column, so derive from the offer and default to 'ru'.
        lang = (offer.language if offer and offer.language else "ru")
        ap = AaPanelClient()
        now = datetime.now(timezone.utc)
        published = []
        for p in pages:
            # `published` СТАВИТСЯ ТОЛЬКО ПОСЛЕ ТОГО, КАК ПАНЕЛЬ ПОДТВЕРДИЛА ЗАПИСЬ. Раньше отказ
            # aaPanel (HTTP 200 + {"status": false}) не смотрел никто: страница помечалась
            # опубликованной, сайт — `published`, а в docroot не было ничего. Дальше M5 честно
            # спрашивал у поисковика про несуществующий URL и писал `not_indexed` — машина
            # расследовала последствия собственного вранья (F14/F16).
            # Теперь write_file поднимает RuntimeError, и он летит наверх НЕ ПОЙМАННЫМ: db.commit()
            # ниже не выполняется -> страницы остаются `edited`, сайт — в прежнем статусе.
            # Файлы страниц, успевших записаться до отказа, лежат на диске — и это не рассинхрон:
            # write_file идемпотентен (CreateFile+SaveFileBody перезаписывают тело), повторная
            # публикация просто положит их снова. Лучше записать дважды, чем соврать один раз.
            ap.write_file(_target_path(site.doc_root, p.url_path), render_html(p, offer, lang=lang))
            p.status = "published"
            p.published_at = now
            published.append(p.url_path)

        site.status = "published"
        site.published_at = now
        db.commit()
        return {"status": "published", "domain": domain, "pages": published}


def check_index(site_id: int) -> dict:
    """SearXNG `site:` check for each published page -> pages.index_status + index_history.

    Стадия крутится АВТОПИЛОТОМ (orchestrator._stage_check_index), поэтому каждая её неточность
    — не единичная ошибка, а вымысел, который машина регулярно и молча пишет в IndexHistory.
    Три исхода, и «не знаю» больше не притворяется «нет» (аудит F15):

      indexed     — в выдаче есть ИМЕННО эта страница (хост И путь, см. _same_page).
      not_indexed — поисковик по этому запросу ОТВЕТИЛ (выдача непустая, либо пустая при живых
                    движках), и нашей страницы у него нет. Это знание.
      unknown     — выдача пуста И хоть один движок не ответил (CAPTCHA/лимит). Спросить не
                    удалось; «не нашли» и «не спросили» — разные вещи.

    Почему `unknown` только при ПУСТОЙ выдаче, а не при любом мёртвом движке: на живом боксе часть
    движков лежит ПОСТОЯННО (сверка 2026-07-14: brave — «too many requests», startpage — CAPTCHA),
    и правило «умер любой → не знаю» означало бы, что машина не скажет `not_indexed` НИКОГДА —
    та же ложь, вид сбоку. Непустая выдача доказывает, что запрос обслужен: движок ответил и
    нашей страницы не показал.

    Застрять в `unknown` навсегда страница не может: выборка ниже берёт ВСЕ published-страницы
    независимо от index_status — следующая проверка переспросит. Мёртвый SearXNG — беда
    поисковика, а не приговор сайту.
    """
    from sqlalchemy import select
    from app.db import SessionLocal
    from app.models.site import Site, Page
    from app.models.domain import Domain
    from app.models.monitoring import IndexHistory
    from app.integrations.searxng import SearxngClient

    with SessionLocal() as db:
        site = db.get(Site, site_id)
        if site is None:
            raise ValueError(f"site {site_id} not found")
        domain = db.get(Domain, site.domain_id).domain
        pages = db.execute(select(Page).where(
            Page.site_id == site_id, Page.status == "published")).scalars().all()

        sx = SearxngClient()
        now = datetime.now(timezone.utc)
        out = {}
        for p in pages:
            q = f"site:{domain}{p.url_path if p.url_path != '/' else ''}"
            data = sx.search_full(q)            # результаты и здоровье движков — из ОДНОГО ответа
            results = data.get("results") or []
            dead = data.get("unresponsive_engines") or []
            if any(_same_page(r.get("url"), domain, p.url_path) for r in results):
                p.index_status = "indexed"
            elif not results and dead:
                p.index_status = "unknown"
            else:
                p.index_status = "not_indexed"
            # время ставим и у `unknown`: попытка БЫЛА. Пустой index_checked_at остаётся
            # значить ровно одно — «не проверялось ни разу» (панель их и различает).
            p.index_checked_at = now
            db.add(IndexHistory(page_id=p.id, checked_at=now, index_status=p.index_status))
            out[p.url_path] = p.index_status
        db.commit()
        return {"domain": domain, "pages": out}


if __name__ == "__main__":  # pure path helper self-check
    assert _target_path("/www/wwwroot/ex.ru", "/") == "/www/wwwroot/ex.ru/index.html"
    assert _target_path("/www/wwwroot/ex.ru/", "/vs") == "/www/wwwroot/ex.ru/vs/index.html"
    assert _target_path("/www/wwwroot/ex.ru", "/setup/") == "/www/wwwroot/ex.ru/setup/index.html"
    assert _same_page("https://www.ex.ru/setup/?utm=1#a", "ex.ru", "/setup")
    assert not _same_page("https://ex.ru/", "ex.ru", "/setup")
    print("publish _target_path / _same_page ok")
