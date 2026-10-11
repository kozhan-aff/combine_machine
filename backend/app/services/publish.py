"""M5 — Publish & Monitor. Deploy ONLY 'edited' pages -> docroot (aaPanel file API).

Hard gate: a page publishes ONLY from status 'edited' (never 'draft'). Then index
monitoring: GSC URL Inspection -> SearXNG `site:` как вспомогательный; IndexNow-пинг после публикации. See PLAN §2.
"""
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, unquote

from app.services.labels import site_status_ru


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
    # Порядок важен: `?`/`#` режем ДО unquote. Наоборот — и `%3F`/`%23` внутри имени сегмента
    # превратились бы в разделители, обрубив путь по букве, которая частью хвоста не была.
    p = (path or "").split("?", 1)[0].split("#", 1)[0]
    p = unquote(p).strip().lower()
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


def _pick_offer(db, site):
    """Оффер для legacy-страницы без offer_id: тот, что явно привязан к сайту. Глобального
    «первого активного оффера» нет (S6-13/S5-16): нет привязки -> None -> публикация громко
    предупредит, а не поставит чужую партнёрку."""
    from app.services.content import site_offer
    return site_offer(db, site)


def live_clause():
    """Условие «страница отдаётся с сайта»: у неё есть файл на сервере. Это НЕ то же, что статус
    `published`: переписанная писателем страница уходит в draft, а её прежний файл остаётся онлайн до
    повторной публикации. По этому условию собираются ссылки меню, sitemap и проверка индекса;
    перерисовывать же файл можно только из вычитанного тела — см. фазу 3 publish_site.
    `status == published` в условии — для строк без published_at (старые данные)."""
    from sqlalchemy import or_
    from app.models.site import Page
    return or_(Page.published_at.is_not(None), Page.status == "published")


# Статусы сайта, в которые публикуем: provision() уже довёл инфраструктуру до `content`.
PUBLISH_STATUSES = frozenset({"content", "published", "monitoring"})


def _verify_live(domain: str, url_path: str, build_id: str) -> str | None:
    """HTTP-проверка после записи (S5-05/S7-18): домен отдаёт 200 и ИМЕННО нашу версию
    (метка build-id). None = проверено, иначе причина словами. Провал не значит «записи нет» —
    значит «опубликованной считать нельзя»: чаще всего NS ещё не делегированы или vhost
    отдаёт заглушку панели."""
    from app.integrations import siteprobe
    url = f"https://{domain}{url_path if url_path.startswith('/') else '/' + url_path}"
    url += ("&" if "?" in url else "?") + f"_pv={build_id}"       # мимо кэша CDN
    try:
        status, text = siteprobe.fetch(url)
    except Exception as e:  # noqa: BLE001 — DNS/TLS/таймаут: причина оператору
        return f"{type(e).__name__}: {e}"[:200]
    if status != 200:
        return f"HTTP {status}"
    if f"content='{build_id}'" not in text and f'content="{build_id}"' not in text:
        return "отдаётся не записанная версия (заглушка панели, старый кэш или не та папка сайта)"
    return None


def publish_site(site_id: int) -> dict:
    """Deploy every 'edited' page of a site. Refuses if there are none (the edit gate).

    Постраничный исход (S7-18): страница получает `published` ТОЛЬКО если файл записан И домен
    отдал именно эту версию (settings.PUBLISH_VERIFY). Отказ или непроверенная запись одной
    страницы не обрывает остальные и не теряет информацию: ответ несёт `pages` (опубликованные),
    `written` (легли на диск), `failed` и `unverified` с причинами. status: published | partial |
    failed. Непроверенная страница остаётся `edited` — повтор идемпотентен (write_file
    перезаписывает).

    Ворота держатся на свежем чтении, а не на снимке, сделанном при отборе: выгрузка идёт минуты, и
    писатель или редактор коммитят всё это время. Перед записью КАЖДОГО файла строка перечитывается —
    в файл идёт только тело, которое в этот момент `edited` (у соседей ради меню — `published`).
    `published` ставится условным UPDATE: строке, которая всё ещё `edited` и несёт ТО ЖЕ тело, что
    ушло в файл; иначе отметка легла бы на текст, которого на сайте нет и которого никто не читал, —
    такая страница уходит в `failed` с причиной. Отметка коммитится сразу, вместе со статусом сайта и
    домена: блокировка строки не держит редактор панели до конца выгрузки.
    """
    from sqlalchemy import select, update
    from app.config import settings
    from app.db import SessionLocal
    from app.models.site import Site, Page
    from app.models.domain import Domain
    from app.models.offer import Offer
    from app.integrations.aapanel import AaPanelClient, AaPanelBlocked
    from app.services.content import render_html, build_id_of, cta_link
    from app.services import site_builder
    from app.services.locales import norm_lang

    with SessionLocal() as db:
        site = db.get(Site, site_id)
        if site is None:
            raise ValueError(f"site {site_id} not found")
        dom = db.get(Domain, site.domain_id)
        domain = dom.domain
        pages = db.execute(select(Page).where(
            Page.site_id == site_id, Page.status == "edited")).scalars().all()
        if not pages:
            return {"status": "no_edited_pages",
                    "hint": "публикуются только вычитанные страницы"}
        # Любая попытка (в т.ч. отказ до записи) — отметка для ротации стадии publish в оркестраторе
        _attempt = datetime.now(timezone.utc)
        for p in pages:
            p.publish_attempted_at = _attempt
        # Отметку фиксируем СРАЗУ, отдельной короткой транзакцией: исключение ниже (_pick_offer,
        # сборка, сеть) откатило бы её, и ротация очереди застряла бы на «сломанном» сайте.
        db.commit()
        # S5-09/S6-12/S7-05: публиковать можно только в провиженный сайт. Раньше проверки не было —
        # файлы писались в docroot без vhost'а/зоны (CreateFile создаёт каталоги сам), страницы и
        # сайт помечались published; на legacy-строке без doc_root падал AttributeError на rstrip.
        if (site.status not in PUBLISH_STATUSES or not site.aapanel_site_name or not site.doc_root):
            db.commit()
            return {"status": "not_provisioned",
                    "hint": f"сайт ещё не поднят ({site_status_ru(site.status)}"
                            f"{'' if site.aapanel_site_name else ', на сервере его нет'}) — сначала «Поднять сайт»"}

        # F26 (аудит 2026-07-14): «текущий активный оффер сайта» НЕ пересчитывается при каждой
        # публикации — он мог смениться с момента генерации. Fallback — ИСКЛЮЧИТЕЛЬНО для
        # legacy-страниц (до миграции 0018) с пустым p.offer_id.
        fallback_offer = _pick_offer(db, site)
        # F3 (аудит 2026-07-15): выключенный оффер публикуется как есть (offer_id — зафиксированное
        # решение о бренде, F26), но подставляем общий резервный URL вместо мёртвой ссылки, если
        # оператор его настроил. Читаем ОДИН РАЗ на весь publish_site().
        from app.models.offer import OfferSettings
        _offer_settings = db.get(OfferSettings, 1)
        reserve_url = _offer_settings.reserve_offer_url if _offer_settings else None
        ap = AaPanelClient()
        now = datetime.now(timezone.utc)
        published, written, failed, unverified, warnings = [], [], {}, {}, []
        blocked = None
        # ── фаза 1: проверка и отбор. Ничего не пишем, пока не знаем, что публиковать ──────────
        ready = []                                           # (page, offer, lang)
        for p in pages:
            # Каждая страница несёт СВОЙ offer_id/lang, зафиксированные в момент генерации
            # (content.generate_site) — читаем их, а не «текущее» состояние сайта.
            offer = db.get(Offer, p.offer_id) if p.offer_id is not None else fallback_offer
            # <html lang=...>: приоритет — язык, под который страница реально писалась;
            # для legacy-страниц без p.lang — язык текущего оффера, иначе en (не ru: S6-02).
            lang = norm_lang(p.lang or (offer.language if offer and offer.language else None))
            if offer is not None and cta_link(offer, reserve_url) is None:
                # оффер привязан, а CTA молча не выйдет (пустая/небезопасная ссылка) — страница
                # без своей единственной ссылки не должна уходить в интернет (S6-07)
                failed[p.url_path] = (f"ссылка оффера «{offer.brand}» пуста или небезопасна — "
                                      "CTA не выведется; поправь оффер")
                continue
            if offer is None:
                # без оффера страница — просто текст без партнёрки: в интернет её не выпускаем
                # (иначе оператор узнаёт постфактум, а автопилот публикует без человека)
                failed[p.url_path] = ("у страницы нет оффера — публикация без партнёрской ссылки "
                                      "заблокирована; привяжи оффер к сайту")
                continue
            if not offer.active and not reserve_url:
                # оффер выключен, резервного URL нет: CTA повёл бы на мёртвую партнёрку
                failed[p.url_path] = (f"оффер «{offer.brand}» выключен и резервный URL не задан — "
                                      "включи оффер или задай резервный URL")
                continue
            ready.append((p, offer, lang))

        if not ready:
            db.commit()
            return {"domain": domain, "pages": [], "written": [], "failed": failed,
                    "unverified": {}, "warnings": warnings, "status": "failed"}

        # ── фаза 2: ассеты. Порядок «assets -> страницы -> sitemap»: страница никогда не ссылается
        # на ещё не лежащий CSS/картинку; провал ассетов = страницы НЕ трогаем (прежняя версия
        # сайта остаётся целой), оператору — список записанного. Атомарного rename через API
        # aaPanel не гарантируем (не проверено вживую), поэтому — порядок и честный отчёт.
        # Живые страницы сайта (не в этом прогоне) — в навигацию: все, у кого есть файл на сервере
        # (live_clause), в том числе переписанные и ещё не вычитанные. Файлы перерисовываем в фазе 3
        # только у тех, что в статусе published: их p.body — вычитанное (save_draft / mark_edited такие
        # страницы отказывают, а переписывание уводит страницу в draft), и nav не осиротит вышедшие позже.
        lang0, brand0 = ready[0][2], ready[0][1].brand
        live = db.execute(select(Page).where(Page.site_id == site_id, live_clause())).scalars().all()
        run_ids = {p.id for p, _, _ in ready}
        nav_src = {p.url_path: (p.title, lang) for p, _, lang in ready}
        for q in live:
            nav_src.setdefault(q.url_path, (q.title, q.lang or lang0))
        nav = [(path, site_builder.nav_label(path, ttl, lg))
               for path, (ttl, lg) in sorted(nav_src.items(), key=lambda kv: (kv[0].strip("/") != "", kv[0]))]
        ctx = site_builder.make_ctx(domain, nav, lang0, brand0)
        home_title = next((p.title for p, _, _ in ready if p.url_path.strip("/") == ""), ready[0][0].title)
        assets = site_builder.build_assets(domain, lang0, brand0, home_title or "")
        root = site.doc_root.rstrip("/")
        mk = site_builder.marker_path(domain)
        order = [f for f in assets if f != mk] + [mk]   # маркер — последним
        written_files = []
        try:
            for rel in order:
                ap.write_file(f"{root}/{rel}", assets[rel])
                written_files.append(rel)
        except AaPanelBlocked as e:
            db.commit()
            raise e
        except Exception as e:  # noqa: BLE001
            for p, _, _ in ready:
                failed[p.url_path] = (f"ассеты сайта не записаны ({type(e).__name__}: {e})"[:240]
                                      + "; страницы не тронуты")
            db.commit()
            return {"domain": domain, "pages": [], "written": [], "failed": failed, "unverified": {},
                    "warnings": warnings, "status": "failed", "assets_written": written_files}

        # ── фаза 3: страницы ──────────────────────────────────────────────────────────────────
        for p, offer, lang in ready:
            # Свежее чтение прямо перед рендером: страницу отобрали в начале, а с тех пор её могли
            # переписать (-> draft, непрочитанный текст) или одобрить заново (edited, новое вычитанное
            # тело — публикуем его). От настройки expire_on_commit это не зависит: читаем явно.
            db.refresh(p)
            if p.status != "edited":
                failed[p.url_path] = ("страница изменилась до записи файла — сейчас не выложена, "
                                      "вычитай и опубликуй её ещё раз")
                continue
            body = p.body                        # ровно это тело уходит в файл — по нему и ставим отметку
            # Рендер — под тем же except, что и запись: сбой шаблона на одной странице не должен
            # обрывать прогон, когда предыдущие страницы уже отмечены опубликованными.
            try:
                bid = build_id_of(render_html(p, offer, lang=lang, reserve_url=reserve_url, ctx=ctx))
                doc = render_html(p, offer, lang=lang, reserve_url=reserve_url, build_id=bid, ctx=ctx)
                ap.write_file(_target_path(site.doc_root, p.url_path), doc)
            except AaPanelBlocked as e:
                blocked = e                      # панель на паузе: остальным писать бесполезно
                break
            except Exception as e:  # noqa: BLE001 — отказ ОДНОЙ страницы не теряет остальные
                failed[p.url_path] = f"{type(e).__name__}: {e}"[:300]
                continue
            written.append(p.url_path)
            if settings.PUBLISH_VERIFY:
                why = _verify_live(domain, p.url_path, bid)
                if why:
                    unverified[p.url_path] = why
                    continue
            # `published` — только после подтверждения панелью И (если включено) самим доменом,
            # и только если строка всё ещё edited с тем телом, что ушло в файл (условный UPDATE: запись
            # и проверка домена шли секунды, а писатель/редактор коммитят в своих сессиях).
            stamped = db.execute(
                update(Page).where(Page.id == p.id, Page.status == "edited", Page.body == body)
                .values(status="published", published_at=now)
                .execution_options(synchronize_session=False)).rowcount
            if stamped == 1:
                # Коммит сразу: на PostgreSQL UPDATE держит блокировку строки до конца транзакции, и
                # сохранение этой страницы из панели висело бы всю оставшуюся выгрузку. Статус сайта и
                # домена — тем же коммитом: опубликованная страница без опубликованного сайта не
                # остаётся, что бы ни случилось со следующими.
                if site.status != "monitoring":
                    site.status = "published"
                site.published_at = now
                if dom.status == "purchased":    # S7-10: первая живая публикация -> домен live
                    dom.status = "live"
                db.commit()
            db.refresh(p)                        # дальше функция видит строку как в БД, а не как прочитали
            if stamped != 1:
                failed[p.url_path] = ("страница изменилась во время публикации — на сайте записана прежняя "
                                      "версия, опубликуй её ещё раз")
                continue
            published.append(p.url_path)

        # Навигация общая: ранее опубликованным страницам нужна ссылка на только что вышедшие.
        # Тело/статус/offer не меняем — тот же p.body и тот же offer_id; одинаковые байты при
        # неизменной nav (идемпотентно). Отказ записи — предупреждение, страница остаётся прежней.
        if blocked is None and published:
            for q in live:
                if q.id in run_ids:
                    continue
                db.refresh(q)                    # свежее чтение перед рендером — как у страниц прогона
                if q.status != "published":
                    # файл живой, но строку переписали (draft/edited): её нынешнее тело никто не
                    # публиковал — на сайт его не несём. Прежний файл остаётся со старым меню до
                    # повторной публикации страницы.
                    continue
                q_offer = db.get(Offer, q.offer_id) if q.offer_id is not None else fallback_offer
                if (q_offer is None or cta_link(q_offer, reserve_url) is None
                        or (not q_offer.active and not reserve_url)):
                    warnings.append(f"{q.url_path}: nav не обновлён (нет пригодного оффера)")
                    continue
                q_lang = norm_lang(q.lang or (q_offer.language if q_offer.language else None))
                try:
                    q_bid = build_id_of(render_html(q, q_offer, lang=q_lang, reserve_url=reserve_url, ctx=ctx))
                    ap.write_file(_target_path(site.doc_root, q.url_path),
                                  render_html(q, q_offer, lang=q_lang, reserve_url=reserve_url,
                                              build_id=q_bid, ctx=ctx))
                except AaPanelBlocked as e:
                    blocked = e
                    break
                except Exception as e:  # noqa: BLE001
                    warnings.append(f"{q.url_path}: nav не обновлён: {type(e).__name__}: {e}"[:200])

        # ── фаза 4: robots.txt + sitemap.xml по ЖИВЫМ страницам (не по edited) ─────────────────
        # Живая страница этого прогона, чья публикация не состоялась, из sitemap не уходит: по её
        # адресу по-прежнему отдаётся файл.
        if blocked is None and (published or live):
            urls = published + [q.url_path for q in live if q.url_path not in published]
            for rel, content in site_builder.build_site_files(domain, urls).items():
                try:
                    ap.write_file(f"{root}/{rel}", content)
                    written_files.append(rel)
                except Exception as e:  # noqa: BLE001 — не критично для страниц, но видно оператору
                    warnings.append(f"{rel} не записан: {type(e).__name__}: {e}"[:200])

        db.commit()                              # статус сайта и домена уже записан вместе с отметками страниц
        if blocked is not None:
            raise blocked
        _indexnow_ping(domain, published, written_files, warnings)
        out = {"domain": domain, "pages": published, "written": written,
               "failed": failed, "unverified": unverified, "warnings": warnings,
               "files": written_files}
        if published and not failed and not unverified:
            out["status"] = "published"
        else:
            out["status"] = "partial" if published else "failed"
        return out


def _indexnow_ping(domain: str, published: list, written_files: list, warnings: list) -> None:
    """Пинг IndexNow о только что вышедших страницах. Best-effort: сбой — предупреждение, публикацию
    не откатывает и не меняет ни статус страницы, ни сайта. Не пингуем, если файл-ключ не лёг на
    сайт (поисковик не сможет подтвердить владение) или функция выключена."""
    from app.config import settings
    from app.integrations.indexnow import IndexNowClient, IndexNowError
    from app.services import site_builder

    if not published or not settings.INDEXNOW_ENABLED:
        return
    key = site_builder.indexnow_key(domain)
    if not key:
        warnings.append("IndexNow: не задан INDEXNOW_SECRET — пинг пропущен")
        return
    if f"{key}.txt" not in written_files:
        warnings.append("IndexNow: файл-ключ не записан на сайт — пинг пропущен")
        return
    host = site_builder._seed(domain)
    urls = [f"https://{host}{site_builder.page_href(p)}" for p in published]
    try:
        IndexNowClient().submit(host, key, urls)
    except IndexNowError as e:
        warnings.append(str(e)[:200])
    except Exception as e:  # noqa: BLE001 — пинг не имеет права ронять уже состоявшуюся публикацию
        warnings.append(f"IndexNow: {type(e).__name__}: {e}"[:200])


# Перепроверка индексации (S7-16): ждать нечего у только что опубликованной, а вот страница,
# уже найденная в индексе, не требует вопросов каждый час — иначе ~60 site:-запросов/час к
# поисковикам, которые и так отвечают «too many requests».
INDEX_RECHECK = timedelta(hours=6)             # не в индексе / не выяснено
INDEX_RECHECK_INDEXED = timedelta(days=3)      # уже в индексе


def _aware(dt):
    return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def index_due(page, now) -> bool:
    """Пора ли переспрашивать индекс у страницы (cooldown после последней проверки)."""
    ck = _aware(page.index_checked_at)
    if ck is None:
        return True
    return now - ck >= (INDEX_RECHECK_INDEXED if page.index_status == "indexed" else INDEX_RECHECK)


# Суточная квота GSC URL Inspection на свойство — 2000; оставляем запас на ручные проверки в консоли.
GSC_SITE_DAILY_CAP = 1900


def _gsc_used_today(db, site_id: int, now) -> int:
    """Сколько GSC-вопросов по сайту задано с UTC-полуночи. Строки GSC отличает непустой
    coverage_state (SearXNG его не пишет) — отдельная таблица счётчика ради этого не нужна."""
    from sqlalchemy import select, func
    from app.models.site import Page
    from app.models.monitoring import IndexHistory
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return db.execute(select(func.count(IndexHistory.id)).join(Page, Page.id == IndexHistory.page_id)
                      .where(Page.site_id == site_id, IndexHistory.coverage_state.is_not(None),
                             IndexHistory.checked_at >= midnight)).scalar_one()


def _searxng_verdict(sx, domain: str, url_path: str) -> str:
    """`site:`-проверка через SearXNG -> indexed | not_indexed | unknown (правила — в check_index)."""
    q = f"site:{domain}{url_path if url_path != '/' else ''}"
    data = sx.search_full(q)                # результаты и здоровье движков — из ОДНОГО ответа
    results = data.get("results") or []
    dead = data.get("unresponsive_engines") or []
    if any(_same_page(r.get("url"), domain, url_path) for r in results):
        return "indexed"
    if not results and dead:
        return "unknown"
    return "not_indexed"


def check_index(site_id: int, only_due: bool = False) -> dict:
    """Проверка индексации каждой живой страницы (publish.live_clause: файл на сайте есть, даже если
    строку с тех пор переписали в draft) -> pages.index_status + index_history.

    Источники по порядку: Google Search Console URL Inspection (если задан ключ сервис-аккаунта и
    свойство сайта доступно) -> SearXNG `site:` как вспомогательный (GSC не настроен / нет доступа к
    свойству / исчерпана квота / сбой). Ничего не отвечает — `unknown`, а не выдуманное «нет».

    Стадия крутится АВТОПИЛОТОМ (orchestrator._stage_check_index), поэтому каждая её неточность
    — не единичная ошибка, а вымысел, который машина регулярно и молча пишет в IndexHistory.
    Три исхода, и «не знаю» не притворяется «нет» (аудит F15):

      indexed     — GSC: вердикт PASS; SearXNG: в выдаче ИМЕННО эта страница (хост И путь, _same_page).
      not_indexed — источник ОТВЕТИЛ и страницы в индексе нет (GSC: NEUTRAL/FAIL; SearXNG: выдача
                    непустая либо пустая при живых движках). Это знание.
      unknown     — спросить не удалось: GSC без вердикта (PARTIAL/пусто); SearXNG — пустая выдача
                    И хоть один движок не ответил (CAPTCHA/лимит).

    Почему у SearXNG `unknown` только при ПУСТОЙ выдаче, а не при любом мёртвом движке: часть движков
    лежит ПОСТОЯННО (brave — «too many requests», startpage — CAPTCHA), и правило «умер любой → не
    знаю» означало бы, что `not_indexed` не прозвучит НИКОГДА. Непустая выдача доказывает, что запрос
    обслужен.

    GSC-строка истории несёт coverage_state (текст Google) — по ней же считается суточная квота.
    Статус сайта: published -> monitoring только когда страница реально `indexed`; больше ничего
    check_index не двигает (ни страниц, ни домена).

    Застрять в `unknown` навсегда страница не может: выборка берёт ВСЕ живые страницы независимо
    от index_status — следующая проверка (по cooldown) переспросит.
    """
    from sqlalchemy import select
    from app.db import SessionLocal
    from app.models.site import Site, Page
    from app.models.domain import Domain
    from app.models.monitoring import IndexHistory
    from app.integrations import gsc as gsc_mod
    from app.integrations.searxng import SearxngClient
    from app.services.site_builder import page_href

    with SessionLocal() as db:
        site = db.get(Site, site_id)
        if site is None:
            raise ValueError(f"site {site_id} not found")
        domain = db.get(Domain, site.domain_id).domain
        pages = db.execute(select(Page).where(Page.site_id == site_id, live_clause())).scalars().all()

        sx = SearxngClient()
        now = datetime.now(timezone.utc)
        if only_due:                      # автопилот: только те, у кого вышел cooldown (S7-16)
            pages = [p for p in pages if index_due(p, now)]
        gsc_on = gsc_mod.configured()
        gsc = gsc_mod.GscClient() if gsc_on else None
        gsc_left = GSC_SITE_DAILY_CAP - _gsc_used_today(db, site_id, now) if gsc_on else 0
        out, sources, details, gsc_why = {}, {}, {}, None
        for p in pages:
            status, cov, src = None, None, "searxng"
            if gsc is not None and gsc_left > 0:
                try:
                    r = gsc.inspect(domain, f"https://{domain}{page_href(p.url_path)}")
                    gsc_left -= 1
                    cov = r["coverage_state"] or r["verdict"] or "?"
                    status = {True: "indexed", False: "not_indexed", None: "unknown"}[r["indexed"]]
                    src = "gsc"
                    details[p.url_path] = {"coverage_state": r["coverage_state"], "last_crawl": r["last_crawl"]}
                except gsc_mod.GscQuota:
                    gsc_left, gsc_why = 0, "квота GSC исчерпана"
                except gsc_mod.GscNoAccess:
                    gsc, gsc_why = None, "аккаунт не добавлен в свойство GSC этого сайта"
                except gsc_mod.GscError as e:
                    gsc, gsc_why = None, str(e)[:120]
            if status is None:
                status = _searxng_verdict(sx, domain, p.url_path)
            p.index_status = status
            # время ставим и у `unknown`: попытка БЫЛА. Пустой index_checked_at остаётся
            # значить ровно одно — «не проверялось ни разу» (панель их и различает).
            p.index_checked_at = now
            db.add(IndexHistory(page_id=p.id, checked_at=now, index_status=status, coverage_state=cov))
            out[p.url_path], sources[p.url_path] = status, src
        # S7-10: первая страница в индексе -> сайт под мониторингом (published -> monitoring)
        if site.status == "published" and any(v == "indexed" for v in out.values()):
            site.status = "monitoring"
        db.commit()
        res = {"domain": domain, "pages": out, "sources": sources, "details": details,
               # все проверенные страницы `unknown` -> источники молчат; автопилот не долбит дальше
               "all_unknown": bool(out) and all(v == "unknown" for v in out.values())}
        if gsc_why:
            res["gsc_note"] = gsc_why
        return res


if __name__ == "__main__":  # pure path helper self-check
    assert _target_path("/www/wwwroot/ex.ru", "/") == "/www/wwwroot/ex.ru/index.html"
    assert _target_path("/www/wwwroot/ex.ru/", "/vs") == "/www/wwwroot/ex.ru/vs/index.html"
    assert _target_path("/www/wwwroot/ex.ru", "/setup/") == "/www/wwwroot/ex.ru/setup/index.html"
    assert _same_page("https://www.ex.ru/setup/?utm=1#a", "ex.ru", "/setup")
    assert not _same_page("https://ex.ru/", "ex.ru", "/setup")
    print("publish _target_path / _same_page ok")
