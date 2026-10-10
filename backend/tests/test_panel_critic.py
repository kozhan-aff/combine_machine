"""HTTP-уровень критика (план Б, задача 6): кнопка «Вычитать» в редакторе страницы и показ вердикта.
Кнопка статус страницы не меняет никогда — одобряет только «Одобрить» (content.mark_edited)."""
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
from app.models.site import Site, Page
from app.services import autonomy, content, content_critic, page_doc

SENT = "NordVPN работает стабильно, подключается быстро и помогает спокойно смотреть любимые сериалы в поездках. "
BODY = f"<h2>Скорость</h2><p>{SENT * 60}</p><h2>Приватность</h2><p>{SENT * 60}</p>"


@pytest.fixture(autouse=True)
def _own_guides(tmp_path, monkeypatch):
    (tmp_path / "guides").mkdir()        # пустая папка правил: «правил нет», а не «папка не видна»
    monkeypatch.setattr(settings, "CONTENT_GUIDES_DIR", str(tmp_path / "guides"))
    return tmp_path / "guides"


def _seed_page(body=BODY, domain="critroute.xyz") -> int:
    with db.SessionLocal() as s:
        d = Domain(domain=domain, source="list", status="purchased")
        o = Offer(brand="NordVPN", affiliate_link="https://ref/nord")
        s.add_all([d, o]); s.commit()
        site = Site(domain_id=d.id, status="content", offer_id=o.id)
        s.add(site); s.commit()
        s.add(SiteResearch(site_id=site.id, kind="review", query="nordvpn review", rank=1, url="https://r1.example/p",
                           domain="r1.example", words=900, text="от 3.39 в месяц"))
        p = Page(site_id=site.id, url_path="/", title="NordVPN: обзор", status="draft", body=body, lang="ru",
                 offer_id=o.id)
        s.add(p); s.commit()
        return p.id


def _page(pid: int) -> Page:
    with db.SessionLocal() as s:
        return s.get(Page, pid)


def _answer(monkeypatch, **verdict):
    monkeypatch.setattr("app.integrations.llm.LlmClient.complete",
                        lambda self, system, prompt, **kw: json.dumps(verdict, ensure_ascii=False))


def test_critique_route_writes_verdict_and_keeps_draft(client, monkeypatch):
    _answer(monkeypatch, **{"pass": False, "score": 80, "issues": ["вода во вступлении"]})
    pid = _seed_page()
    r = client.post(f"/pages/{pid}/critique")
    assert r.status_code == 200          # redirect + follow (см. паттерн test_panel_toctou.py)
    p = _page(pid)
    assert p.critic_score == 0.8 and p.critic_notes["model"] == ["вода во вступлении"]
    assert p.status == "draft"           # ГЕЙТ НЕ ТРОНУТ
    assert "вода во вступлении" in r.text and "Вердикт записан: есть замечания." in r.text


@pytest.mark.parametrize("auto_edit", [False, True])
def test_critique_route_never_approves_even_on_pass(client, monkeypatch, auto_edit):
    """Кнопка «Вычитать» — подсказка: при вердикте «pass» страница остаётся черновиком и при выключенном,
    и при включённом тумблере auto_edit."""
    autonomy.update_autonomy(auto_edit=auto_edit)
    _answer(monkeypatch, **{"pass": True, "score": 95, "issues": []})
    pid = _seed_page()
    r = client.post(f"/pages/{pid}/critique")
    assert r.status_code == 200
    p = _page(pid)
    assert p.critic_notes["pass"] is True and p.status == "draft"
    assert "замечаний нет" in r.text


def _seed_writer_page(domain="writer.xyz") -> int:
    """Страница писателя: тело — рендер её blocks; такую критик вправе одобрить сам."""
    doc = {"meta": {"title": "NordVPN: обзор сервиса"},
           "sections": [{"h2": "Скорость", "paragraphs": [SENT * 60]}, {"h2": "Приватность", "paragraphs": [SENT * 60]}]}
    pid = _seed_page(body=content._sanitize(page_doc.render_blocks(page_doc.PageDoc.model_validate(doc), "review", "ru")),
                     domain=domain)
    with db.SessionLocal() as s:
        p = s.get(Page, pid)
        p.title, p.blocks = doc["meta"]["title"], doc
        s.commit()
    return pid


def _flash(client, pid) -> str:
    return unquote(client.post(f"/pages/{pid}/critique", follow_redirects=False).headers["location"])


@pytest.mark.parametrize("auto_edit, then", [
    (True, "Вердикт записан. Критик одобрит страницу при следующей вычитке сайта."),
    (False, "Вердикт записан. Страница остаётся черновиком — одобряешь ты."),
])
def test_critique_flash_for_a_page_the_critic_may_approve(client, monkeypatch, auto_edit, then):
    autonomy.update_autonomy(auto_edit=auto_edit)
    _answer(monkeypatch, **{"pass": True, "score": 95, "issues": []})
    pid = _seed_writer_page()
    loc = _flash(client, pid)
    assert "msg=" in loc and loc.endswith(then)
    assert _page(pid).status == "draft" and "note" not in _page(pid).critic_notes


@pytest.mark.parametrize("auto_edit", [True, False])
def test_critique_flash_names_why_the_human_approves(client, monkeypatch, auto_edit, _own_guides):
    """Флеш не обещает «одобрит критик» странице, которую он сам не одобрит: правленой руками / написанной
    старым способом, с пробелом в правилах письма, с прежним отказом."""
    autonomy.update_autonomy(auto_edit=auto_edit)
    _answer(monkeypatch, **{"pass": True, "score": 95, "issues": []})
    by_hand = _seed_page()                                           # без blocks
    assert _flash(client, by_hand).endswith(
        "Вердикт записан. Одобряешь ты: текст правился вручную или написан старым способом.")
    writer_page = _seed_writer_page()
    (_own_guides / "новое.md").write_text("ПРАВИЛО без выжимки", encoding="utf-8")
    assert _flash(client, writer_page).endswith("Вердикт записан. Одобряешь ты: правила письма не сжаты (1 файл).")
    (_own_guides / "новое.md").unlink()
    with db.SessionLocal() as s:
        p = s.get(Page, writer_page)
        p.critic_notes = {**p.critic_notes, "refused_fp": content_critic.fingerprint(p.title, p.body)}
        s.commit()
    assert _flash(client, writer_page).endswith("Вердикт записан. Одобряешь ты: критик ранее отклонил этот текст.")
    html = client.get(f"/pages/{writer_page}").text
    assert "критик ранее отклонил этот текст — одобряет человек" in html


def test_critique_flash_when_the_page_did_not_pass(client, monkeypatch):
    autonomy.update_autonomy(auto_edit=True)
    _answer(monkeypatch, **{"pass": False, "score": 30, "issues": ["вода"]})
    assert _flash(client, _seed_writer_page()).endswith("Вердикт записан: есть замечания.")
    monkeypatch.setattr("app.integrations.llm.LlmClient.complete", lambda self, sy, pr, **kw: "Нельзя публиковать.")
    assert _flash(client, _seed_writer_page("w2.xyz")).endswith("Вердикт записан: есть замечания.")


# --- форма редактора: одобряется и сохраняется то, с чем страница была открыта ---

def _seen(html: str) -> str:
    return re.search(r'name="seen" value="([0-9a-f]{16})"', html).group(1)


def test_editor_form_carries_the_fingerprint_of_what_it_shows(client):
    pid = _seed_writer_page()
    p = _page(pid)
    assert _seen(client.get(f"/pages/{pid}").text) == content_critic.fingerprint(p.title, p.body)


@pytest.mark.parametrize("action", ["save", "draft"])
def test_stale_editor_form_is_refused(client, action):
    """Оператор держит редактор открытым, писатель переписывает страницу, оператор жмёт «Одобрить» /
    «Сохранить черновик»: отказ с понятными словами, строка не тронута."""
    pid = _seed_writer_page()
    opened = client.get(f"/pages/{pid}").text
    old_body = _page(pid).body
    with db.SessionLocal() as s:                                      # писатель переписал страницу
        p = s.get(Page, pid)
        p.title, p.body = "NordVPN: новый заголовок писателя", old_body.replace("Скорость", "Быстрота")
        s.commit()
    rewritten = _page(pid)
    r = client.post(f"/pages/{pid}/{action}", data={"body": old_body, "seen": _seen(opened)}, follow_redirects=False)
    assert "err=" in r.headers["location"]
    assert "страница изменилась, пока ты её редактировал (её переписал писатель) — открой заново" in unquote(
        r.headers["location"])
    p = _page(pid)
    assert (p.status, p.title, p.body, bool(p.blocks_stale)) == ("draft", rewritten.title, rewritten.body, False)


def test_editor_form_normal_edit_and_approve(client):
    pid = _seed_writer_page()
    edited = _page(pid).body + "<p>Правка оператора, достаточно длинная для гейта редактуры.</p>"
    r = client.post(f"/pages/{pid}/draft", data={"body": edited, "seen": _seen(client.get(f"/pages/{pid}").text)},
                    follow_redirects=False)
    assert "msg=" in r.headers["location"] and _page(pid).body == edited and _page(pid).status == "draft"
    r = client.post(f"/pages/{pid}/save", data={"body": edited, "seen": _seen(client.get(f"/pages/{pid}").text)},
                    follow_redirects=False)
    assert "msg=" in r.headers["location"] and _page(pid).status == "edited" and _page(pid).body == edited


def test_editor_routes_without_the_field_work_as_before(client):
    """Старый клиент (или форма без поля) отпечатка не шлёт: текст формы записан и одобрен, как раньше."""
    pid = _seed_writer_page()
    old_body = _page(pid).body
    with db.SessionLocal() as s:
        s.get(Page, pid).body = old_body.replace("Скорость", "Быстрота")
        s.commit()
    r = client.post(f"/pages/{pid}/draft", data={"body": old_body}, follow_redirects=False)
    assert "msg=" in r.headers["location"] and _page(pid).body == old_body
    r = client.post(f"/pages/{pid}/save", data={"body": old_body}, follow_redirects=False)
    assert "msg=" in r.headers["location"] and _page(pid).status == "edited"


def test_critique_route_survives_llm_error(client, monkeypatch):
    def boom(self, system, prompt, **kw):
        raise RuntimeError("LLM недоступен")
    monkeypatch.setattr("app.integrations.llm.LlmClient.complete", boom)
    pid = _seed_page()
    r = client.post(f"/pages/{pid}/critique")
    assert r.status_code == 200          # не 500: сбой критика не роняет редактор
    p = _page(pid)
    assert p.status == "draft" and p.critic_notes["pass"] is False
    assert "критик не ответил: RuntimeError: LLM недоступен" in r.text


def test_critique_route_missing_page(client):
    assert client.post("/pages/999999/critique").status_code == 200


def _show(client, pid, score, notes, fresh=True) -> str:
    """Экран редактора страницы с записанным вердиктом. `fresh` — вердикт относится к нынешнему тексту
    (в заметках его отпечаток)."""
    with db.SessionLocal() as s:
        p = s.get(Page, pid)
        if fresh and isinstance(notes, dict):
            notes = {**notes, "fp": content_critic.fingerprint(p.title, p.body)}
        p.critic_score, p.critic_notes = score, notes
        p.critic_checked_at = datetime.now(timezone.utc)   # review_page пишет три поля вместе
        s.commit()
    r = client.get(f"/pages/{pid}")
    assert r.status_code == 200
    return r.text


def test_page_edit_view_shows_groups_round_and_score(client):
    pid = _seed_page()
    html = _show(client, pid, 0.45, {"pass": False, "issues": ["объём 300 слов, нужно 1500–2200", "вода"],
                                     "code": ["объём 300 слов, нужно 1500–2200"], "model": ["вода"], "round": 1})
    assert "критик: замечания" in html and "45/100" in html
    assert f"круг 1 из {content_critic.MAX_ROUNDS}" in html
    code, model = html.index("проверки кодом"), html.index("редактор-модель")
    assert code < html.index("объём 300 слов, нужно 1500–2200") < model < html.index("<li>вода</li>")


def test_page_edit_view_shows_pass(client):
    pid = _seed_page()
    html = _show(client, pid, 0.9, {"pass": True, "issues": [], "code": [], "model": [], "round": 0})
    assert "критик: pass" in html and "90/100" in html and "критик: замечания" not in html
    assert "одобряет человек" not in html


def test_page_edit_view_shows_manual_note(client):
    pid = _seed_page()
    note = "одобряет человек: текст правился вручную или написан старым способом"
    html = _show(client, pid, 0.9, {"pass": True, "issues": [], "code": [], "model": [], "round": 0, "note": note})
    assert "критик: pass" in html and note in html


def test_page_edit_view_shows_closed_failure_without_score(client):
    pid = _seed_page()
    note = "критик не ответил: пустой ответ модели"
    html = _show(client, pid, None, {"pass": False, "issues": [note], "code": [], "model": [note], "round": 0,
                                     "error": "пустой ответ модели"})
    # вычитка не состоялась — это не замечания к тексту: то же слово, что на карточке сайта
    assert "критик: не проверена" in html and "критик: замечания" not in html
    assert note in html and "/100" not in html


def test_page_edit_view_hides_verdict_of_another_text(client):
    """Правка руками после вычитки: значок «критик: pass» на экране гейта относился бы к тексту, которого
    уже нет, — вместо вердикта сказано, что он устарел."""
    pid = _seed_page()
    notes = {"pass": True, "issues": [], "code": [], "model": [], "round": 0}
    assert "критик: pass" in _show(client, pid, 0.9, notes)
    content.save_draft(pid, BODY + "<p>Правка оператора после вычитки.</p>")
    html = client.get(f"/pages/{pid}").text
    assert "текст изменён после вычитки — вердикт устарел" in html
    assert "критик: pass" not in html and "90/100" not in html
    # и отрицательный вердикт к чужому тексту не показываем
    html = _show(client, pid, 0.2, {"pass": False, "issues": ["вода"], "code": [], "model": ["вода"], "round": 1,
                                    "fp": "0" * 16}, fresh=False)
    assert "вердикт устарел" in html and "<li>вода</li>" not in html and "критик: замечания" not in html


def test_route_verdict_is_shown_as_fresh(client, monkeypatch):
    _answer(monkeypatch, **{"pass": True, "score": 95, "issues": []})
    pid = _seed_page()
    r = client.post(f"/pages/{pid}/critique")
    assert "критик: pass" in r.text and "вердикт устарел" not in r.text


def test_page_edit_view_shows_old_format_notes(client):
    """Строки, оценённые прежним критиком ({"issues": [...]} без отпечатка текста): вердикт считается
    устаревшим, «pass» не рисуется; без заметок вовсе — «вердикта нет»."""
    pid = _seed_page()
    html = _show(client, pid, 0.45, {"issues": ["слабое вступление"]}, fresh=False)
    assert "вердикт устарел" in html and "критик: pass" not in html
    html = _show(client, pid, 0.9, None)                 # прежний критик не ответил: заметок нет вовсе
    assert "критик: pass" not in html and "критик: замечания" not in html and "вердикта нет" in html
    never_reviewed = client.get(f"/pages/{_seed_page(domain='fresh.xyz')}").text    # страницу не вычитывали вовсе
    assert "вердикт" not in never_reviewed.split("Вычитать")[1].split("</form>")[0]
