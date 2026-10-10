"""Публикация и переписывание страниц (план Б, задача 4, круг правок 1): `published` получает только
то тело, что реально отрисовано; переписанная, но ещё живая на сайте страница не выпадает из меню,
sitemap и проверки индекса, а её файл не перерисовывается непросмотренным текстом."""
from datetime import datetime, timezone

import pytest

import app.db as db
from app.models.domain import Domain
from app.models.offer import Offer
from app.models.site import Page, Site
from app.services import content, publish

ROOT = "/www/wwwroot/rw.com"
STAMP = datetime(2026, 10, 1, tzinfo=timezone.utc)
REVIEWED = "<p>Reviewed body {path} that a human has approved for the site.</p>"
REWRITTEN = "<p>Model rewrite {path}: nobody has read this text yet at all.</p>"


def _site(pages) -> dict:
    """pages: (путь, статус, published_at) -> {путь: id}; id сайта — под ключом "site"."""
    with db.SessionLocal() as s:
        d = Domain(domain="rw.com", source="dropcatch", status="live")
        off = Offer(brand="NordVPN", affiliate_link="https://ex.com/aff", active=True)
        s.add_all([d, off]); s.commit()
        site = Site(domain_id=d.id, status="published", aapanel_site_name="rw.com", doc_root=ROOT,
                    offer_id=off.id)
        s.add(site); s.commit()
        rows = [Page(site_id=site.id, url_path=path, title=f"T {path}", status=status, lang="en",
                     offer_id=off.id, body=REVIEWED.format(path=path), published_at=at)
                for path, status, at in pages]
        s.add_all(rows); s.commit()
        return {"site": site.id, **{p.url_path: p.id for p in rows}}


def _page(page_id: int) -> Page:
    with db.SessionLocal() as s:
        return s.get(Page, page_id)


def _rewrite(page_id: int) -> None:
    """То, что делает писатель при переписывании: новое тело, статус draft (см. content._apply_doc)."""
    with db.SessionLocal() as s:
        p = s.get(Page, page_id)
        p.body, p.status = REWRITTEN.format(path=p.url_path), "draft"
        s.commit()


@pytest.fixture
def writes(monkeypatch):
    """Журнал записей в aaPanel; `log.hook(path)` зовётся перед каждой записью."""
    class Log(list):
        hook = None
    log = Log()

    def write_file(self, path, body):
        if log.hook:
            log.hook(path)
        log.append((path, body))
    monkeypatch.setattr("app.integrations.aapanel.AaPanelClient.__init__", lambda self: None)
    monkeypatch.setattr("app.integrations.aapanel.AaPanelClient.write_file", write_file)
    return log


@pytest.fixture(params=[False, True], ids=["expire_off", "expire_on"])
def session_mode(request):
    """Ворота публикации не должны держаться на настройке сессии: при expire_on_commit=True объекты
    после каждого commit перечитываются из БД, при False — остаются снимком момента чтения."""
    db.SessionLocal.configure(expire_on_commit=request.param)
    yield request.param
    db.SessionLocal.configure(expire_on_commit=False)


def _serp_empty(monkeypatch):
    payload = {"results": [{"url": "https://other.example/"}], "unresponsive_engines": []}
    monkeypatch.setattr("app.integrations.searxng.SearxngClient.search_full",
                        lambda self, q, **kw: payload, raising=False)


# --- (2) published — только за отрисованное тело ---

def test_page_rewritten_during_publish_is_not_stamped_published(writes):
    ids = _site([("/", "edited", None), ("/vs", "edited", None)])
    # publish уже прочитал edited-страницы и пишет файл «/»; в этот момент писатель коммитит перезапись
    writes.hook = lambda path: path == f"{ROOT}/index.html" and _rewrite(ids["/"])
    out = publish.publish_site(ids["site"])
    home = _page(ids["/"])
    assert home.status == "draft" and home.body == REWRITTEN.format(path="/") and home.published_at is None
    assert out["pages"] == ["/vs"] and out["status"] == "partial"
    assert out["failed"] == {"/": "страница изменилась во время публикации — на сайте записана прежняя версия, "
                                  "опубликуй её ещё раз"}
    assert "/" in out["written"]                                    # файл лёг — с прежним, вычитанным текстом
    body = dict(writes)[f"{ROOT}/index.html"]
    assert "Reviewed body /" in body and "Model rewrite" not in body
    assert _page(ids["/vs"]).status == "published" and _page(ids["/vs"]).published_at is not None
    assert "rw.com/</loc>" not in dict(writes)[f"{ROOT}/sitemap.xml"]


def test_page_reapproved_with_other_body_during_publish_is_not_stamped(writes):
    """Статус снова edited, но тело уже другое: на сайте лежит не оно — published ставить не за что."""
    ids = _site([("/", "edited", None)])

    def reedit(path):
        if path == f"{ROOT}/index.html":
            content.mark_edited(ids["/"], "<p>Second approved text, different from the one being uploaded.</p>")
    writes.hook = reedit
    out = publish.publish_site(ids["site"])
    assert out["status"] == "failed" and out["pages"] == [] and "изменилась во время публикации" in out["failed"]["/"]
    home = _page(ids["/"])
    assert home.status == "edited" and "Second approved text" in home.body and home.published_at is None
    with db.SessionLocal() as s:
        assert s.get(Site, ids["site"]).published_at is None       # сайт не считается опубликованным


def test_unchanged_page_is_published_as_before(writes):
    ids = _site([("/", "edited", None), ("/vs", "draft", None)])
    out = publish.publish_site(ids["site"])
    assert out["status"] == "published" and out["pages"] == ["/"] and out["failed"] == {}
    assert _page(ids["/"]).status == "published" and _page(ids["/"]).published_at is not None
    assert _page(ids["/vs"]).status == "draft"


# --- ворота: в write_file уходит только вычитанное тело, при любой настройке сессии ---

def test_page_rewritten_before_its_file_is_written_never_reaches_the_site(writes, session_mode):
    ids = _site([("/", "edited", None), ("/vs", "edited", None)])
    done = []

    def rewrite_vs_early(path):                                     # первый же ассет: до файлов страниц
        if not done:
            done.append(_rewrite(ids["/vs"]))
    writes.hook = rewrite_vs_early
    out = publish.publish_site(ids["site"])
    assert out["pages"] == ["/"] and out["status"] == "partial"
    assert "изменилась" in out["failed"]["/vs"] and "/vs" not in out["written"]
    assert not any("Model rewrite" in body for _, body in writes)   # непрочитанный текст не ушёл никуда
    assert f"{ROOT}/vs/index.html" not in dict(writes)              # вычитанного тела у «/vs» уже нет — не пишем
    vs = _page(ids["/vs"])
    assert vs.status == "draft" and vs.published_at is None and vs.body == REWRITTEN.format(path="/vs")
    assert _page(ids["/"]).status == "published"


def test_published_neighbour_rewritten_during_upload_is_not_rerendered(writes, session_mode):
    ids = _site([("/", "published", STAMP), ("/vs", "edited", None)])
    # набор живых страниц уже выбран, идёт запись «/vs» — в этот момент писатель переписывает «/»
    writes.hook = lambda path: path == f"{ROOT}/vs/index.html" and _rewrite(ids["/"])
    out = publish.publish_site(ids["site"])
    assert out["pages"] == ["/vs"]
    assert not any("Model rewrite" in body for _, body in writes)
    assert f"{ROOT}/index.html" not in dict(writes)                 # сосед уже draft — его файл не трогаем
    assert "rw.com/</loc>" in dict(writes)[f"{ROOT}/sitemap.xml"]   # но адрес живой и из sitemap не уходит
    assert _page(ids["/"]).status == "draft"


def test_reapproved_page_publishes_its_new_approved_body(writes, session_mode):
    """Обратная сторона ворот: тело, одобренное человеком уже после выбора страниц, — вычитанное.
    Публикуется оно, а не снимок, который успели прочитать."""
    ids = _site([("/", "edited", None), ("/vs", "edited", None)])
    approved = "<p>Second approved text of the comparison page, read by a human.</p>"
    done = []

    def reapprove_vs_early(path):
        if not done:
            done.append(content.mark_edited(ids["/vs"], approved))
    writes.hook = reapprove_vs_early
    out = publish.publish_site(ids["site"])
    assert sorted(out["pages"]) == ["/", "/vs"] and out["failed"] == {}
    assert "Second approved text" in dict(writes)[f"{ROOT}/vs/index.html"]
    assert _page(ids["/vs"]).status == "published" and _page(ids["/vs"]).body == approved


def test_stamp_is_committed_at_once_with_site_and_domain_status(writes):
    """Отметка коммитится сразу (блокировка строки не висит всю выгрузку) и вместе со статусом сайта и
    домена: что бы ни случилось со следующей страницей, опубликованная остаётся опубликованной, а сайт
    с опубликованной страницей — опубликованным."""
    ids = _site([("/", "edited", None), ("/vs", "edited", None)])
    with db.SessionLocal() as s:
        site = s.get(Site, ids["site"])
        site.status = "content"
        s.get(Domain, site.domain_id).status = "purchased"
        s.commit()

    class Crash(BaseException):
        """Не отказ записи (его ловит постраничный except), а авария посреди фазы 3: процесс убили."""

    def crash(path):
        if path == f"{ROOT}/vs/index.html":
            raise Crash()
    writes.hook = crash
    with pytest.raises(Crash):
        publish.publish_site(ids["site"])
    assert _page(ids["/"]).status == "published" and _page(ids["/"]).published_at is not None
    assert _page(ids["/vs"]).status == "edited"
    with db.SessionLocal() as s:
        site = s.get(Site, ids["site"])
        assert site.status == "published" and site.published_at is not None
        assert s.get(Domain, site.domain_id).status == "live"


def test_later_page_failure_keeps_per_page_outcome_and_site_status(writes):
    ids = _site([("/", "edited", None), ("/vs", "edited", None), ("/setup", "edited", None)])
    with db.SessionLocal() as s:
        site = s.get(Site, ids["site"])
        site.status = "content"
        s.get(Domain, site.domain_id).status = "purchased"
        s.commit()

    def fail_vs(path):
        if path == f"{ROOT}/vs/index.html":
            raise RuntimeError("disk full")
    writes.hook = fail_vs
    out = publish.publish_site(ids["site"])
    assert out["pages"] == ["/", "/setup"] and out["status"] == "partial" and "disk full" in out["failed"]["/vs"]
    assert out["written"] == ["/", "/setup"] and out["unverified"] == {}
    assert _page(ids["/vs"]).status == "edited" and _page(ids["/setup"]).status == "published"
    with db.SessionLocal() as s:
        site = s.get(Site, ids["site"])
        assert site.status == "published" and s.get(Domain, site.domain_id).status == "live"


# --- (3) живая, но переписанная страница остаётся в меню, sitemap и проверке индекса ---

def test_rewritten_live_pages_stay_in_nav_sitemap_and_index_check(writes, monkeypatch):
    ids = _site([("/", "published", STAMP), ("/vs", "published", STAMP), ("/setup", "published", STAMP),
                 ("/blog", "draft", None)])                         # «/blog» на сайт не выходила никогда
    for path in ("/", "/vs", "/setup"):
        _rewrite(ids[path])
    content.mark_edited(ids["/"])                                   # человек вычитал только главную
    out = publish.publish_site(ids["site"])
    assert out["status"] == "published" and out["pages"] == ["/"]

    files = dict(writes)
    home = files[f"{ROOT}/index.html"]
    assert "href='/vs/'" in home and "href='/setup/'" in home and "/blog" not in home
    assert "Model rewrite /:" in home                               # вычитанное тело главной — на сайте
    sitemap = files[f"{ROOT}/sitemap.xml"]
    for loc in ("rw.com/</loc>", "rw.com/vs/</loc>", "rw.com/setup/</loc>"):
        assert loc in sitemap
    assert "blog" not in sitemap
    # файлы «/vs» и «/setup» не тронуты: их нынешнее тело никто не читал, на сайте остаётся прежнее
    assert f"{ROOT}/vs/index.html" not in files and f"{ROOT}/setup/index.html" not in files
    assert not any("Model rewrite /vs" in body or "Model rewrite /setup" in body for body in files.values())
    assert _page(ids["/vs"]).status == "draft" and _page(ids["/setup"]).status == "draft"

    _serp_empty(monkeypatch)
    checked = publish.check_index(ids["site"])
    assert set(checked["pages"]) == {"/", "/vs", "/setup"}
    assert _page(ids["/vs"]).index_checked_at is not None and _page(ids["/blog"]).index_checked_at is None


def test_published_sibling_is_still_rerendered_for_nav(writes):
    """Прежнее поведение: страница в статусе published (тело вычитано) перерисовывается ради меню."""
    ids = _site([("/", "published", STAMP), ("/vs", "edited", None), ("/setup", "published", STAMP)])
    _rewrite(ids["/setup"])
    publish.publish_site(ids["site"])
    files = dict(writes)
    assert "href='/vs/'" in files[f"{ROOT}/index.html"] and "href='/setup/'" in files[f"{ROOT}/index.html"]
    assert f"{ROOT}/setup/index.html" not in files


def test_failed_republish_keeps_live_url_in_sitemap(writes):
    """Переписанная и вычитанная страница, чья публикация в этом прогоне не состоялась, всё ещё отдаётся
    по своему адресу (прежним файлом) — из sitemap она не уходит."""
    ids = _site([("/", "edited", None), ("/vs", "edited", STAMP)])

    def fail_vs(path):
        if path == f"{ROOT}/vs/index.html":
            raise RuntimeError("disk full")
    writes.hook = fail_vs
    out = publish.publish_site(ids["site"])
    assert out["pages"] == ["/"] and "disk full" in out["failed"]["/vs"]
    assert "rw.com/vs/</loc>" in dict(writes)[f"{ROOT}/sitemap.xml"]


def test_autopilot_index_stage_picks_site_with_rewritten_live_pages(monkeypatch):
    from app.services import orchestrator as orch
    ids = _site([("/", "published", STAMP), ("/blog", "draft", None)])
    _rewrite(ids["/"])
    seen = []
    monkeypatch.setattr(publish, "check_index", lambda sid, only_due=False: seen.append(sid) or {"pages": {}})
    done, errs, _ = orch._stage_check_index(5)
    assert seen == [ids["site"]] and done == 1 and errs == []

    with db.SessionLocal() as s:                                    # сайт без единой вышедшей страницы — мимо
        s.get(Page, ids["/"]).published_at = None
        s.commit()
    seen.clear()
    orch._stage_check_index(5)
    assert seen == []


# --- сбой рендера одной страницы не обрывает прогон ---

def test_render_failure_of_one_page_does_not_abort_the_run(writes, monkeypatch):
    """Рендер стоял вне постраничного try: шаблон, упавший на второй странице, обрывал прогон исключением
    уже после того, как первая получила отметку, — без sitemap и без отчёта по страницам."""
    ids = _site([("/", "edited", None), ("/vs", "edited", None)])
    real = content.render_html

    def render(page, *a, **kw):
        if page.url_path == "/vs":
            raise RuntimeError("шаблон сломан")
        return real(page, *a, **kw)

    monkeypatch.setattr(content, "render_html", render)
    out = publish.publish_site(ids["site"])
    assert out["status"] == "partial" and out["pages"] == ["/"] and out["written"] == ["/"]
    assert out["failed"] == {"/vs": "RuntimeError: шаблон сломан"}
    assert _page(ids["/"]).status == "published" and _page(ids["/vs"]).status == "edited"
    paths = [path for path, _ in writes]
    assert f"{ROOT}/vs/index.html" not in paths
    sitemap = dict(writes)[f"{ROOT}/sitemap.xml"]
    assert "rw.com/</loc>" in sitemap and "/vs" not in sitemap
