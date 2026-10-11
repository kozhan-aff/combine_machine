"""Панель под писателя и критика (план Б, задача 7): тексты пишутся только по досье, кнопки «Переписать
тексты» и «Вычитать критиком» уходят в фон, карточка сайта показывает вердикт критика, тумблер
«критик сам одобряет тексты» на экране автопилота включается."""
import json
import re
from datetime import datetime, timezone
from urllib.parse import unquote

import pytest

import app.db as db
from app.config import settings
from app.models.domain import Domain
from app.models.offer import Offer
from app.models.research import SiteResearch
from app.models.site import Page, Site
from app.services import autonomy, content, content_critic, jobs, page_doc

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)
CONFIRM = ("Переписать тексты сайта? Все страницы вернутся в черновики. На сайте останется прежняя версия, "
           "пока не опубликуешь новую.")
SENT = "Durev VPN работает стабильно, подключается быстро и помогает спокойно смотреть любимые сериалы в поездках. "


def _site(dossier: bool = True, status: str = "content", offer: bool = True, domain: str = "wp.xyz") -> int:
    with db.SessionLocal() as s:
        o = Offer(brand="Durev VPN", affiliate_link="https://durevpn.example/aff", language="ru", active=True)
        d = Domain(domain=domain, source="list", status="purchased", market_lang="ru")
        s.add_all([o, d]); s.commit()
        site = Site(domain_id=d.id, status=status, doc_root=f"/www/wwwroot/{domain}", aapanel_site_name=domain,
                    offer_id=o.id if offer else None, provision_step="done")
        s.add(site); s.commit()
        if dossier:
            s.add(SiteResearch(site_id=site.id, kind="review", query="durev vpn обзор", rank=1,
                               url="https://c.example/1", domain="c.example", words=900))
            s.commit()
        return site.id


def _page(site_id: int, path: str = "/", status: str = "draft", **kw) -> int:
    with db.SessionLocal() as s:
        p = Page(**{**dict(site_id=site_id, url_path=path, title=f"T {path}", status=status, lang="ru",
                           body="<p>текст</p>"), **kw})
        s.add(p); s.commit()
        return p.id


def _judged(site_id: int, path: str, notes: dict, **kw) -> int:
    """Страница с вердиктом критика к её НЫНЕШНЕМУ тексту: в заметках — отпечаток заголовка и тела, как
    его пишет критик (без него карточка считает вердикт устаревшим)."""
    pid = _page(site_id, path, critic_checked_at=NOW, **kw)
    with db.SessionLocal() as s:
        page = s.get(Page, pid)
        page.critic_notes = {**notes, "fp": content_critic.fingerprint(page.title, page.body)}
        s.commit()
    return pid


def _spawned(monkeypatch, ok: bool = True) -> list:
    """Подмена jobs.spawn: записывает имя задачи и (если задача принята) выполняет её тут же."""
    calls = []

    def spawn(name, target):
        calls.append(name)
        if ok:
            target()
        return ok

    monkeypatch.setattr(jobs, "spawn", spawn)
    return calls


def _writer(monkeypatch) -> list:
    seen = []
    monkeypatch.setattr(content, "generate_site", lambda site_id, **kw: seen.append((site_id, kw)) or 3)
    return seen


def _flash(r) -> str:
    assert r.status_code == 303
    return unquote(r.headers["location"])


def _tag(html: str, marker: str) -> str:
    """Открывающий тег, внутри которого встретился marker (кнопка, input)."""
    m = re.search(r"<[^<>]*" + re.escape(marker) + r"[^<>]*>", html)
    assert m, f"нет тега с {marker!r}"
    return m.group(0)


def _button(html: str, label: str) -> str:
    """Открывающий тег кнопки с такой подписью."""
    m = re.search(r"<button\b([^<>]*)>\s*" + re.escape(label), html)
    assert m, f"нет кнопки {label!r}"
    return m.group(1)


# --- «Написать тексты»: только по досье ---

def test_generate_refused_without_dossier(client, monkeypatch):
    sid = _site(dossier=False)
    calls, seen = _spawned(monkeypatch), _writer(monkeypatch)
    loc = _flash(client.post(f"/sites/{sid}/generate", data={"lang": ""}, follow_redirects=False))
    assert "err=" in loc and "Сначала изучи конкурентов (шаг 4): без этого писать не по чему" in loc
    assert calls == [] and seen == []


def test_generate_with_dossier_spawns_writer_without_use_competitor(client, monkeypatch):
    sid = _site()
    calls, seen = _spawned(monkeypatch), _writer(monkeypatch)
    loc = _flash(client.post(f"/sites/{sid}/generate", data={"lang": "ru"}, follow_redirects=False))
    assert "msg=" in loc and calls == ["generate"]
    assert seen == [(sid, {"lang": "ru"})]          # структуру конкурентов несёт досье


def test_generate_refusals_keep_their_order(client, monkeypatch):
    """Статус и оффер отвечают раньше досье: чинить их надо первыми, досье без оффера и не собрать."""
    calls = _spawned(monkeypatch)
    early = _site(dossier=False, status="provisioning")
    assert "сначала «Поднять сайт»" in _flash(client.post(f"/sites/{early}/generate", follow_redirects=False))
    with db.SessionLocal() as s:
        s.get(Site, early).status = "content"
        s.get(Site, early).offer_id = None
        s.commit()
    assert "Оффер не привязан" in _flash(client.post(f"/sites/{early}/generate", follow_redirects=False))
    assert calls == []


# --- «Переписать тексты» ---

def test_rewrite_spawns_writer_in_rewrite_mode(client, monkeypatch):
    sid = _site()
    _page(sid)
    calls, seen = _spawned(monkeypatch), _writer(monkeypatch)
    loc = _flash(client.post(f"/sites/{sid}/rewrite", follow_redirects=False))
    assert calls == ["generate"] and seen == [(sid, {"rewrite": True, "overwrite_manual": False})]
    assert "msg=" in loc and "вернутся в черновики" in loc and "в прежнем виде" in loc


def test_rewrite_checkbox_lets_the_writer_touch_hand_edited_pages(client, monkeypatch):
    sid = _site()
    _page(sid)
    _spawned(monkeypatch)
    seen = _writer(monkeypatch)
    client.post(f"/sites/{sid}/rewrite", data={"overwrite_manual": "on"}, follow_redirects=False)
    assert seen == [(sid, {"rewrite": True, "overwrite_manual": True})]


def test_rewrite_refusals(client, monkeypatch):
    calls, seen = _spawned(monkeypatch), _writer(monkeypatch)
    assert "не найден" in _flash(client.post("/sites/999/rewrite", follow_redirects=False))
    no_dossier = _site(dossier=False)
    assert "Сначала изучи конкурентов (шаг 4)" in _flash(
        client.post(f"/sites/{no_dossier}/rewrite", follow_redirects=False))
    with db.SessionLocal() as s:
        s.add(SiteResearch(site_id=no_dossier, kind="review", query="q", rank=1, url="https://c.example/1"))
        s.get(Site, no_dossier).offer_id = None
        s.commit()
    assert "Оффер не привязан" in _flash(client.post(f"/sites/{no_dossier}/rewrite", follow_redirects=False))
    with db.SessionLocal() as s:
        s.get(Site, no_dossier).status = "provisioning"
        s.commit()
    assert "сначала «Поднять сайт»" in _flash(client.post(f"/sites/{no_dossier}/rewrite", follow_redirects=False))
    assert calls == [] and seen == []


def test_rewrite_and_generate_say_so_when_writer_is_busy(client, monkeypatch):
    sid = _site()
    _page(sid)
    calls, seen = _spawned(monkeypatch, ok=False), _writer(monkeypatch)
    for route in ("rewrite", "generate"):
        loc = _flash(client.post(f"/sites/{sid}/{route}", follow_redirects=False))
        assert "err=" in loc and "уже" in loc
    assert calls == ["generate", "generate"] and seen == []


# --- «Вычитать критиком» ---

def test_edit_spawns_critic_and_leaves_the_toggle_to_the_service(client, monkeypatch):
    sid = _site()
    _page(sid)
    calls, seen = _spawned(monkeypatch), []
    monkeypatch.setattr(content_critic, "edit_site", lambda *a, **kw: seen.append((a, kw)) or {})
    loc = _flash(client.post(f"/sites/{sid}/edit", follow_redirects=False))
    assert calls == ["edit"] and seen == [((sid,), {})]     # auto_edit не передан: сервис читает тумблер сам
    assert "msg=" in loc and "одобряешь ты" in loc
    autonomy.update_autonomy(auto_edit=True)
    assert "одобрит сам" in _flash(client.post(f"/sites/{sid}/edit", follow_redirects=False))


def _passing_draft(site_id: int, path: str = "/") -> int:
    """Черновик писателя, который вычитку ПРОХОДИТ (как в test_critic_gate): структура `blocks`, тело — её
    рендер, оффер записан, бренд назван, язык русский, объём в границах обзора, чисел нет."""
    doc = {"meta": {"title": "Durev VPN: обзор и честный тест",
                    "description": "Проверили скорость и приватность Durev VPN — кому он подойдёт, а кому нет."},
           "sections": [{"h2": "Скорость", "paragraphs": [SENT * 60]},
                        {"h2": "Приватность", "paragraphs": [SENT * 60]}]}
    body = content._sanitize(page_doc.render_blocks(page_doc.PageDoc.model_validate(doc), "review", "ru"))
    with db.SessionLocal() as s:
        offer_id = s.get(Site, site_id).offer_id
    return _page(site_id, path, title=doc["meta"]["title"], body=body, blocks=doc, offer_id=offer_id)


@pytest.fixture
def critic_says_pass(tmp_path, monkeypatch):
    """Кнопка идёт до конца без подмены критика: задача выполняется на месте, модель отвечает «прошла»,
    правила письма — из пустой папки. -> список вызовов `content.mark_edited` (настоящая функция работает)."""
    (tmp_path / "guides").mkdir()        # папка есть и пуста: «правил нет», а не «папка не видна процессу»
    monkeypatch.setattr(settings, "CONTENT_GUIDES_DIR", str(tmp_path / "guides"))
    monkeypatch.setattr(jobs, "spawn", lambda name, target: target() or True)
    monkeypatch.setattr("app.integrations.llm.LlmClient.complete",
                        lambda self, system, prompt, **kw: json.dumps({"pass": True, "score": 90, "issues": []}))
    seen, real = [], content.mark_edited

    def spy(*a, **kw):
        seen.append((a, kw))
        return real(*a, **kw)

    monkeypatch.setattr(content, "mark_edited", spy)
    return seen


def test_edit_button_never_approves_while_toggle_is_off(client, critic_says_pass):
    """Ворота кнопки: страница вычитку ПРОШЛА (вердикт записан), а статус не тронут — одобряет человек."""
    autonomy.update_autonomy(auto_edit=False)
    sid = _site()
    pid = _passing_draft(sid)
    client.post(f"/sites/{sid}/edit", follow_redirects=False)
    with db.SessionLocal() as s:
        page = s.get(Page, pid)
        assert page.critic_notes["pass"] is True, page.critic_notes     # без этого тест ничего не доказывал бы
        assert page.status == "draft"
    assert critic_says_pass == []


def test_edit_button_approves_a_passing_page_when_toggle_is_on(client, critic_says_pass):
    """Зеркало: та же страница, тумблер включён — критик одобряет её, и только через mark_edited."""
    autonomy.update_autonomy(auto_edit=True)
    sid = _site()
    pid = _passing_draft(sid)
    client.post(f"/sites/{sid}/edit", follow_redirects=False)
    with db.SessionLocal() as s:
        page = s.get(Page, pid)
        assert page.critic_notes["pass"] is True and page.status == "edited"
    assert len(critic_says_pass) == 1 and critic_says_pass[0][0][0] == pid


def test_edit_refusals_and_busy(client, monkeypatch):
    calls = _spawned(monkeypatch, ok=False)
    monkeypatch.setattr(content_critic, "edit_site", lambda *a, **kw: {})
    assert "не найден" in _flash(client.post("/sites/999/edit", follow_redirects=False))
    sid = _site()
    _page(sid, status="edited")
    assert "нет черновиков" in _flash(client.post(f"/sites/{sid}/edit", follow_redirects=False))
    assert calls == []
    _page(sid, "/vs")
    loc = _flash(client.post(f"/sites/{sid}/edit", follow_redirects=False))
    assert calls == ["edit"] and "err=" in loc and "уже идёт" in loc


def test_edit_job_is_known_to_the_registry_routes(client):
    assert client.post("/run/edit/cancel", follow_redirects=False).status_code != 404
    assert "edit:\'Вычитка\'" in client.get("/autopilot").text


def test_dashboard_lists_the_edit_job_among_last_runs(client):
    from app.models.job import JobRun
    with db.SessionLocal() as s:
        s.add(JobRun(name="edit", status="done", message="вычитано 3, одобрено 2", started_at=NOW,
                     updated_at=NOW, finished_at=NOW))
        s.commit()
    html = client.get("/").text
    assert "<b style=\"min-width:190px\">Вычитка</b>" in html and "вычитано 3, одобрено 2" in html


# --- карточка сайта ---

def test_card_buttons_and_their_states(client):
    sid = _site()
    _page(sid)
    html = client.get(f"/sites/{sid}").text
    assert "disabled" not in _button(html, "▶ Написать тексты")
    assert "▶ Сгенерировать черновики" not in html
    assert f'action="/sites/{sid}/rewrite"' in html and "disabled" not in _button(html, "✎ Переписать тексты")
    form = _tag(html, f'action="/sites/{sid}/rewrite"')
    assert CONFIRM in form.replace("&#39;", "'")
    assert 'class="row"' in form and "display:flex" not in form      # разметка — существующим классом
    assert "6 · Вычитка <small>ты или критик</small>" in html and "только человек" not in html
    box = _tag(html, 'name="overwrite_manual"')
    assert "checked" not in box and "и правленные вручную" in html
    assert "Страницы, которые ты правил сам, обычно не трогаются." in html
    assert f'action="/sites/{sid}/edit"' in html
    critic = _button(html, "✓ Вычитать критиком")
    assert "disabled" not in critic and "покажет вердикт — одобряешь ты" in critic
    autonomy.update_autonomy(auto_edit=True)
    assert "прошедшие проверку одобрит сам" in _button(client.get(f"/sites/{sid}").text, "✓ Вычитать критиком")


def test_card_blocks_writing_until_dossier_is_there(client):
    sid = _site(dossier=False)
    html = client.get(f"/sites/{sid}").text
    write = _button(html, "▶ Написать тексты")
    assert "disabled" in write and "Сначала шаг 4: изучи конкурентов." in write
    assert "Без этого тексты не пишутся" in html and "пишется вслепую" not in html
    assert "✎ Переписать тексты" not in html and "disabled" in _button(html, "✓ Вычитать критиком")   # страниц нет


def _rows(html: str) -> dict:
    """{путь: остаток строки таблицы страниц}."""
    return dict(re.findall(r'<td class="dom">([^<]+)</td>(.*?)</tr>', html, re.S))


def _critic_td(row: str) -> str:
    """Ячейка «критик» — третья после пути: заголовок, статус, критик."""
    return re.findall(r"<td.*?</td>", row, re.S)[2]


def test_card_shows_critic_verdict_per_page(client):
    sid = _site()
    _judged(sid, "/", {"pass": True, "issues": [], "round": 0})
    _judged(sid, "/vs", {"pass": False, "issues": ["мало конкретики про скорость", "нет цены"], "round": 1})
    _page(sid, "/setup")
    html = client.get(f"/sites/{sid}").text
    assert ">критик</th>" in html
    rows = {path: _critic_td(row) for path, row in _rows(html).items()}
    assert "led-ok" in rows["/"] and ">прошла</td>" in rows["/"]
    assert ">2 замеч.</td>" in rows["/vs"] and "led-todo" in rows["/vs"]
    assert "мало конкретики про скорость; нет цены" in rows["/vs"] and "переписана по замечаниям: 1 из 2" in rows["/vs"]
    assert rows["/setup"] == '<td><span class="hint">—</span></td>'
    assert "вычитано — уйдёт при публикации" in html


def test_card_critic_cell_keeps_long_lists_in_the_tooltip(client):
    sid = _site()
    _judged(sid, "/", {"pass": False, "issues": [f"з{i}" for i in range(5)]})
    cell = _critic_td(_rows(client.get(f"/sites/{sid}").text)["/"])
    assert ">5 замеч.</td>" in cell and 'title="з0; з1; з2 … и ещё 2"' in cell


def test_card_critic_cell_tells_passed_for_human_from_not_reviewed(client):
    """«Прошла, но всё ещё черновик» и «вычитка не состоялась» — разные состояния, и оба не «замечания»."""
    sid = _site()
    note = "одобряет человек: текст правился вручную или написан старым способом"
    _judged(sid, "/", {"pass": True, "issues": [], "note": note})
    _judged(sid, "/vs", {"pass": False, "issues": ["критик не ответил: ReadTimeout"], "error": "ReadTimeout"})
    rows = {path: _critic_td(row) for path, row in _rows(client.get(f"/sites/{sid}").text).items()}
    # «прошла» — основной строкой, «одобряешь ты» — второй: одной строкой колонка не умещалась на 1024px
    assert '></span>прошла<div class="hint">одобряешь ты</div></td>' in rows["/"]
    assert f'title="{note}"' in rows["/"] and "led-ok" in rows["/"]
    assert ">не проверена</td>" in rows["/vs"] and "led-warn" in rows["/vs"]
    assert 'title="вычитка не состоялась: ReadTimeout"' in rows["/vs"] and "замеч" not in rows["/vs"]


def test_card_shows_a_verdict_only_for_the_text_it_was_given_to(client):
    """Правка после вычитки: «прошла» относилась бы к тексту, которого на странице уже нет."""
    sid = _site()
    passed = _judged(sid, "/", {"pass": True, "issues": []})
    judged = _judged(sid, "/vs", {"pass": False, "issues": ["вода"]})
    _page(sid, "/setup", critic_checked_at=NOW, critic_notes={"pass": True, "issues": []})   # заметки без отпечатка
    rows = {path: _critic_td(row) for path, row in _rows(client.get(f"/sites/{sid}").text).items()}
    assert ">прошла</td>" in rows["/"] and ">1 замеч.</td>" in rows["/vs"]
    stale = '<td title="текст изменён после вычитки — вычитай заново"><span class="led led-off"></span>устарел</td>'
    assert rows["/setup"] == stale
    for pid in (passed, judged):
        content.save_draft(pid, "<p>Оператор переписал абзац руками уже после того, как критик прочёл страницу.</p>")
    rows = {path: _critic_td(row) for path, row in _rows(client.get(f"/sites/{sid}").text).items()}
    assert rows["/"] == stale and rows["/vs"] == stale


def test_card_says_you_approve_only_while_the_page_is_a_draft(client):
    """«Одобряешь ты» и «остаётся черновиком» — про черновик. Одобренной человеком странице это не пишем."""
    note = "одобряет человек: текст правился вручную или написан старым способом"
    body = "<p>" + SENT * 4 + "</p>"
    sid = _site()
    held = _judged(sid, "/", {"pass": True, "issues": [], "note": note}, body=body)
    plain = _judged(sid, "/vs", {"pass": True, "issues": []}, body=body)
    changed = _judged(sid, "/setup", {"pass": True, "issues": [], "note": note}, body=body)
    rows = {path: _critic_td(row) for path, row in _rows(client.get(f"/sites/{sid}").text).items()}
    assert "одобряешь ты" in rows["/"] and "остаётся черновиком" in rows["/vs"]
    content.mark_edited(held)                                            # человек одобрил как лежит
    content.mark_edited(plain)
    content.mark_edited(changed, body="<p>" + SENT * 5 + "</p>")          # человек одобрил свою правку
    with db.SessionLocal() as s:
        assert {s.get(Page, x).status for x in (held, plain, changed)} == {"edited"}
    rows = {path: _critic_td(row) for path, row in _rows(client.get(f"/sites/{sid}").text).items()}
    for path in ("/", "/vs"):
        assert ">прошла</td>" in rows[path]
        assert "одобряешь ты" not in rows[path] and "остаётся черновиком" not in rows[path] and note not in rows[path]
    # человек одобрил свою правку: прежний вердикт к этому тексту не относится, а совет «вычитай заново»
    # одобренной странице ни к чему — прочерк
    assert rows["/setup"] == '<td><span class="hint">—</span></td>'


def test_card_stale_verdict_advice_is_only_for_a_draft(client):
    """«Устарел — вычитай заново» — про черновик. У одобренной и опубликованной страницы с вердиктом к
    прежнему тексту в колонке прочерк."""
    sid = _site()
    for path, status in (("/", "draft"), ("/vs", "edited"), ("/setup", "published")):
        _page(sid, path, status=status, critic_checked_at=NOW, critic_notes={"pass": True, "issues": []})  # без отпечатка
    rows = {path: _critic_td(row) for path, row in _rows(client.get(f"/sites/{sid}").text).items()}
    assert ">устарел</td>" in rows["/"] and "вычитай заново" in rows["/"]
    for path in ("/vs", "/setup"):
        assert rows[path] == '<td><span class="hint">—</span></td>'


# --- редактор страницы: кнопка «Вычитать» и значок вердикта ---

@pytest.mark.parametrize("auto_edit, then", [
    (False, "Вердикт записан. Страница остаётся черновиком — одобряешь ты."),
    (True, "Вердикт записан. Критик одобрит страницу при следующей вычитке сайта."),
])
def test_editor_critique_flash_says_who_approves_next(client, critic_says_pass, auto_edit, then):
    autonomy.update_autonomy(auto_edit=auto_edit)
    pid = _passing_draft(_site())
    loc = _flash(client.post(f"/pages/{pid}/critique", follow_redirects=False))
    assert "msg=" in loc and then in loc
    with db.SessionLocal() as s:
        assert s.get(Page, pid).status == "draft" and s.get(Page, pid).critic_notes["pass"] is True
    assert critic_says_pass == []                 # сама кнопка не одобряет ни при каком тумблере


def test_editor_badge_tells_failed_check_from_remarks(client):
    sid = _site()
    pid = _judged(sid, "/", {"pass": False, "issues": ["критик не ответил: ReadTimeout"], "code": [],
                             "model": ["критик не ответил: ReadTimeout"], "error": "ReadTimeout"})
    html = client.get(f"/pages/{pid}").text
    badge = _tag(html, "Вычитка не состоялась: ReadTimeout")
    assert "критик: не проверена" in html and "критик: замечания" not in html and "b-warn" in badge


def test_editor_pass_tooltip_mentions_draft_only_for_a_draft(client):
    body = "<p>" + SENT * 4 + "</p>"
    pid = _judged(_site(), "/", {"pass": True, "issues": [], "code": [], "model": []}, body=body)
    html = client.get(f"/pages/{pid}").text
    assert "критик: прошла" in html and "остаётся черновиком, пока её не одобрят" in html
    content.mark_edited(pid)
    html = client.get(f"/pages/{pid}").text
    assert "критик: прошла" in html and "остаётся черновиком, пока её не одобрят" not in html


def test_card_marks_rewritten_page_that_is_still_live(client):
    """Переписанная страница — черновик в базе, а по её адресу отдаётся прежний файл: карточка говорит об
    этом у статуса и не прячет проверку индексации."""
    sid = _site(status="published")
    _page(sid, "/", status="draft", published_at=NOW)
    _page(sid, "/vs", status="draft")
    html = client.get(f"/sites/{sid}").text
    rows = _rows(html)
    assert re.search(r'<div class="hint"[^>]*>на сайте прежняя версия</div>', rows["/"])    # отдельной строкой
    assert "на сайте прежняя версия" not in rows["/vs"]
    check = _button(html, "▶ Проверить индексацию")
    assert "disabled" not in check and "Проверять нечего" not in check


def test_card_index_button_stays_off_for_a_site_never_published(client):
    sid = _site()
    _page(sid)
    assert "disabled" in _button(client.get(f"/sites/{sid}").text, "▶ Проверить индексацию")


def test_publish_flash_reports_page_failure_in_its_own_words(client, monkeypatch):
    why = "страница изменилась во время публикации — на сайте записана прежняя версия, опубликуй её ещё раз"
    monkeypatch.setattr("app.services.publish.publish_site",
                        lambda sid: {"status": "failed", "pages": [], "failed": {"/": why}, "unverified": {},
                                     "warnings": []})
    loc = _flash(client.post(f"/sites/{_site()}/publish", follow_redirects=False))
    assert f"/: {why}" in loc and "не записана" not in loc


# --- карточка сайта: действующий оффер ---

def _offer_step(html: str) -> str:
    """Шаг «2 · Оффер» карточки."""
    return html[html.index("2 · Оффер <small>"):html.index("3 · Поднять сайт")]


def test_card_names_the_offer_the_site_writes_about(client):
    """Раньше все привязанные офферы стояли равными бейджами под «генерация пишет про этот бренд»."""
    from app.models.offer import SiteOffer
    sid = _site()
    with db.SessionLocal() as s:
        old = Offer(brand="NordVPN", affiliate_link="https://nord.example/aff", active=True)
        s.add(old); s.commit()
        own = s.get(Site, sid).offer_id
        s.add_all([SiteOffer(site_id=sid, offer_id=old.id), SiteOffer(site_id=sid, offer_id=own)]); s.commit()
    step = _offer_step(client.get(f"/sites/{sid}").text)
    assert re.search(r"Пишем про: <span class=\"badge b-approved\"[^>]*>Durev VPN</span>", step)
    assert re.search(r'<span class="hint"[^>]*>ещё привязаны: NordVPN</span>', step)
    assert "Привязано:" not in step and "led-ok" in step
    assert step.count("badge b-approved") == 1                     # бейдж — только у действующего


def test_card_shows_site_offer_even_without_a_link_row(client):
    """Оффер записан прямо в сайт (Site.offer_id), строки привязки нет: шаг сделан, а не «выбери бренд»."""
    step = _offer_step(client.get(f"/sites/{_site()}").text)
    assert "Пишем про:" in step and "Durev VPN" in step and "ещё привязаны" not in step
    assert "led-ok" in step and "Выбери бренд" not in step and "написаны под" not in step


def test_card_tells_when_pages_were_written_for_another_offer(client):
    sid = _site()
    with db.SessionLocal() as s:
        old = Offer(brand="NordVPN", affiliate_link="https://nord.example/aff", active=True)
        s.add(old); s.commit()
        old_id, own = old.id, s.get(Site, sid).offer_id
    _page(sid, "/", offer_id=old_id)
    _page(sid, "/vs", offer_id=old_id)
    _page(sid, "/setup", offer_id=own)
    step = _offer_step(client.get(f"/sites/{sid}").text)
    assert re.search(r'<div class="hint"[^>]*>страницы написаны под NordVPN — «Переписать тексты» '
                     r"перепишет их под Durev VPN</div>", step)
    with db.SessionLocal() as s:                                   # все страницы под оффер сайта — строки нет
        for page in s.query(Page).filter(Page.site_id == sid):
            page.offer_id = own
        s.commit()
    assert "написаны под" not in _offer_step(client.get(f"/sites/{sid}").text)


def test_card_marks_a_switched_off_site_offer_and_keeps_legacy_sites_as_before(client):
    from app.models.offer import SiteOffer
    sid = _site()
    with db.SessionLocal() as s:
        s.get(Offer, s.get(Site, sid).offer_id).active = False
        s.commit()
    step = _offer_step(client.get(f"/sites/{sid}").text)
    assert "Пишем про:" in step and ">выключен</span>" in step
    legacy = _site(domain="legacy.xyz", offer=False)               # сайт без Site.offer_id, только привязка
    with db.SessionLocal() as s:
        o = Offer(brand="Proton VPN", affiliate_link="https://proton.example/aff", active=True)
        s.add(o); s.commit()
        s.add(SiteOffer(site_id=legacy, offer_id=o.id)); s.commit()
    step = _offer_step(client.get(f"/sites/{legacy}").text)
    assert "Привязано:" in step and "Proton VPN" in step and "Пишем про:" not in step


# --- автопилот ---

def test_autopilot_auto_edit_is_live_and_auto_design_still_locked(client):
    html = client.get("/autopilot").text
    assert "disabled" not in _tag(html, 'name="auto_edit"') and "checked" not in _tag(html, 'name="auto_edit"')
    assert html.count('name="auto_edit"') == 1
    assert "disabled" in _tag(html, 'name="auto_design"')
    assert "критик сам одобряет тексты" in html and '<div class="plate">Вычитка — ' in html
    assert "редактура всегда за тобой" not in html
    assert "деньги тратятся только после твоего подтверждения; тексты одобряешь ты или критик — если включишь" in html
    assert "включится с планом Б" not in html
    # стадия стоит на своём месте конвейера: после черновиков, перед публикацией
    plate = '<div class="plate">'
    assert html.index(plate + "Написать тексты — ") < html.index(plate + "Вычитка — ") < html.index(plate + "Опубликовать — ")


def test_autopilot_saves_auto_edit_and_shows_it_checked(client):
    r = client.post("/autopilot/settings", data={"auto_edit": "on", "sweep_interval_min": 60}, follow_redirects=False)
    assert r.status_code == 303 and autonomy.get_autonomy()["auto_edit"] is True
    assert "checked" in _tag(client.get("/autopilot").text, 'name="auto_edit"')
    client.post("/autopilot/settings", data={"sweep_interval_min": 60}, follow_redirects=False)
    assert autonomy.get_autonomy()["auto_edit"] is False


def test_autopilot_journal_names_the_new_counters(client):
    from app.models.autonomy import AutonomyRun
    with db.SessionLocal() as s:
        s.add(AutonomyRun(started_at=NOW, finished_at=NOW, trigger="cron", status="completed_with_errors",
                          counts={"edit": 2, "edit_failed": 1, "generate_no_dossier": 1, "generate_empty": 1},
                          errors=[]))
        s.commit()
    html = client.get("/autopilot").text
    for label in ("вычитка:2", "вычитка: замечания:1", "нет разбора конкурентов:1", "тексты не написаны:1"):
        assert label in html
