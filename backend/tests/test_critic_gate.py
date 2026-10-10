"""Гейт редактуры под критиком (план Б, задача 6): `edit_site` — вычитка черновиков, круги переписывания
и одобрение через `content.mark_edited`. Отказ закрытый: сбой, молчание или мусор вместо вердикта, любое
замечание кода или модели, изменившаяся страница — «не прошла». Сам критик одобряет только страницу, чьё
тело — рендер проверенной структуры, и одним условным UPDATE. LLM — только подмена LlmClient.complete;
писателя и критика различаем по модели вызова."""
import json
import re
from pathlib import Path

import httpx
import pytest
from sqlalchemy import event

import app.db as db
from app.config import settings
from app.models.domain import Domain
from app.models.offer import Offer
from app.models.research import SiteResearch
from app.models.site import Page, Site
from app.services import autonomy, content, content_critic, jobs, page_doc, publish

SENT = "Durev VPN работает стабильно, подключается быстро и помогает спокойно смотреть любимые сериалы в поездках. "
# предложение источника длиннее шингла (12 слов): дословный перенос в текст страницы — копирование
COPIED = ("Сервис держит соединение на загруженных серверах даже вечером когда большинство пользователей "
          "одновременно включает потоковое видео высокого качества")
PASS = json.dumps({"pass": True, "score": 90, "issues": []})
FAIL = json.dumps({"pass": False, "score": 40, "issues": ["мало конкретики про скорость"]}, ensure_ascii=False)
OTHER_BODY = "<h2>Правка</h2><p>Оператор переписал страницу руками, пока критик читал прежний текст.</p>"


def _doc(mark: str = "", n: int = 60) -> dict:
    """Страница писателя, к которой у проверок кодом нет претензий: бренд назван, язык русский, объём в
    границах обзора, чисел нет. `mark` — вставка в первый абзац (чтобы различать тексты и подмешивать копию)."""
    return {
        "meta": {"title": "Durev VPN: обзор и честный тест",
                 "description": "Проверили скорость и приватность Durev VPN — кому он подойдёт, а кому нет."},
        "sections": [{"h2": "Скорость", "paragraphs": [(mark + " " if mark else "") + SENT * n]},
                     {"h2": "Приватность", "paragraphs": [SENT * n]}],
    }


GOOD = json.dumps(_doc("Вторая редакция текста."), ensure_ascii=False)


def _body(doc: dict) -> str:
    return content._sanitize(page_doc.render_blocks(page_doc.PageDoc.model_validate(doc), "review", "ru"))


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """Правила письма — из пустой tmp-папки; у писателя и критика разные модели — по ним подмена LLM
    понимает, кто звонит."""
    monkeypatch.setattr(settings, "CONTENT_GUIDES_DIR", str(tmp_path / "guides"))
    monkeypatch.setattr(settings, "LLM_WRITER_MODEL", "writer-m")
    monkeypatch.setattr(settings, "LLM_CRITIC_MODEL", "critic-m")


def _site(paths=("/",), mark: str = "", **page) -> tuple[int, dict]:
    """Сайт с оффером, досье и страницами-черновиками писателя (blocks + их рендер). -> (site_id, {путь: id})."""
    with db.SessionLocal() as s:
        o = Offer(brand="Durev VPN", affiliate_link="https://durevpn.example/aff", language="ru", country="DE",
                  promo_code="DUREV20", promo_terms="скидка 20% на первый год", active=True)
        d = Domain(domain="gate.xyz", source="list", status="purchased", market_lang="ru")
        s.add_all([o, d]); s.commit()
        site = Site(domain_id=d.id, status="content", doc_root="/www/wwwroot/gate.xyz", offer_id=o.id)
        s.add(site); s.commit()
        for kind in ("review", "comparison", "howto"):
            s.add(SiteResearch(site_id=site.id, kind=kind, query=f"durev vpn {kind}", rank=1,
                               url=f"https://{kind}1.example/page", domain=f"{kind}1.example", words=1200,
                               headings=[["h2", "Скорость"]], numbers=[{"value": "5.99", "ctx": "цена 5.99 в месяц"}],
                               text=f"цена 5.99 в месяц. {COPIED}."))
        doc = _doc(mark)
        rows = [Page(**{**dict(site_id=site.id, url_path=path, title=doc["meta"]["title"], status="draft",
                               body=_body(doc), blocks=doc, lang="ru", offer_id=o.id), **page}) for path in paths]
        s.add_all(rows); s.commit()
        return site.id, {p.url_path: p.id for p in rows}


def _page(pid: int) -> Page:
    with db.SessionLocal() as s:
        return s.get(Page, pid)


def _set(pid: int, **fields) -> None:
    """Прямая запись в строку страницы — «другая сессия» (редактор панели, другой прогон писателя)."""
    with db.SessionLocal() as s:
        page = s.get(Page, pid)
        for name, value in fields.items():
            setattr(page, name, value)
        s.commit()


def _llm(monkeypatch, *critic, writer=(), critic_default=PASS, during=None) -> dict:
    """Подмена LlmClient.complete: у критика и писателя свои очереди ответов (дальше — ответ по умолчанию),
    исключение в очереди бросается. `during(who, n)` зовётся внутри вызова. -> {"critic": [...], "writer": [...]}."""
    calls = {"critic": [], "writer": []}
    queues = {"critic": list(critic), "writer": list(writer)}
    default = {"critic": critic_default, "writer": GOOD}

    def complete(self, system, prompt, **kw):
        who = {"critic-m": "critic", "writer-m": "writer"}[kw.get("model")]
        calls[who].append({"system": system, "prompt": prompt, "timeout": self._client.timeout.read})
        if during:
            during(who, len(calls[who]) - 1)
        ans = queues[who].pop(0) if queues[who] else default[who]
        if isinstance(ans, Exception):
            raise ans
        return ans

    monkeypatch.setattr("app.integrations.llm.LlmClient.complete", complete)
    return calls


def _spy_mark_edited(monkeypatch) -> list:
    seen, real = [], content.mark_edited

    def spy(*args, **kwargs):
        seen.append((args, kwargs))
        return real(*args, **kwargs)

    monkeypatch.setattr(content, "mark_edited", spy)
    return seen


def _http_error(code: int) -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "http://llm.example/v1/chat/completions")
    return httpx.HTTPStatusError(f"HTTP {code}", request=req, response=httpx.Response(code, json={}, request=req))


# --- одобрение: только через mark_edited и только при включённом тумблере ---

def test_pass_with_auto_edit_marks_edited_via_mark_edited(monkeypatch):
    site_id, ids = _site()
    seen = _spy_mark_edited(monkeypatch)
    calls = _llm(monkeypatch)
    out = content_critic.edit_site(site_id, auto_edit=True)
    assert out == {"reviewed": 1, "edited": 1, "rewritten": 0, "failed": 0, "manual": 0}
    p = _page(ids["/"])
    # один вызов, и не «одобрить как лежит», а «одобрить, если тело всё ещё ровно то, что читал критик»
    assert seen == [((ids["/"],), {"expected_body": p.body})]
    assert p.status == "edited" and p.blocks_stale is False
    assert p.critic_notes == {"pass": True, "issues": [], "code": [], "model": [], "round": 0}
    assert p.critic_score == 0.9 and p.critic_checked_at is not None
    assert len(calls["critic"]) == 1 and not calls["writer"]
    last = jobs.last("edit")
    assert last["status"] == "done"
    assert "вычитано 1, одобрено 1, переписано 0, с замечаниями 0" in last["message"]


def test_pass_without_auto_edit_keeps_draft(monkeypatch):
    site_id, ids = _site()
    seen = _spy_mark_edited(monkeypatch)
    _llm(monkeypatch)
    out = content_critic.edit_site(site_id, auto_edit=False)
    assert out == {"reviewed": 1, "edited": 0, "rewritten": 0, "failed": 0, "manual": 0}
    assert seen == []
    p = _page(ids["/"])
    assert p.status == "draft" and p.critic_notes["pass"] is True    # вердикт есть, одобряет человек
    last = jobs.last("edit")
    assert last["status"] == "done" and "ждут одобрения человеком: 1" in last["message"]


def test_auto_edit_none_reads_the_operator_toggle(monkeypatch):
    site_id, ids = _site()
    _llm(monkeypatch)
    assert content_critic.edit_site(site_id)["edited"] == 0          # тумблер по умолчанию выключен
    assert _page(ids["/"]).status == "draft"
    autonomy.update_autonomy(auto_edit=True)
    assert content_critic.edit_site(site_id)["edited"] == 1
    assert _page(ids["/"]).status == "edited"


@pytest.mark.parametrize("flag", ["false", "on", 1, 0, [True]])
def test_auto_edit_must_be_the_boolean_true(monkeypatch, flag):
    """Строка из формы («false» тоже истинна), единица, список — не разрешение одобрять."""
    site_id, ids = _site()
    seen = _spy_mark_edited(monkeypatch)
    _llm(monkeypatch)
    assert content_critic.edit_site(site_id, auto_edit=flag)["edited"] == 0
    assert seen == [] and _page(ids["/"]).status == "draft"


def test_publish_still_refuses_draft(monkeypatch):
    """Регрессия гейта: вердикт «pass» без одобрения страницу публикуемой не делает."""
    site_id, ids = _site()
    _llm(monkeypatch)
    content_critic.edit_site(site_id, auto_edit=False)
    assert _page(ids["/"]).critic_notes["pass"] is True
    assert publish.publish_site(site_id)["status"] == "no_edited_pages"
    assert _page(ids["/"]).status == "draft"


# --- круги переписывания ---

def test_fail_rewrites_then_passes(monkeypatch):
    site_id, ids = _site()
    calls = _llm(monkeypatch, FAIL, PASS)
    out = content_critic.edit_site(site_id, auto_edit=True)
    assert out == {"reviewed": 1, "edited": 1, "rewritten": 1, "failed": 0, "manual": 0}
    assert len(calls["critic"]) == 2 and len(calls["writer"]) == 1
    assert "мало конкретики про скорость" in calls["writer"][0]["prompt"]      # замечание дошло до писателя
    p = _page(ids["/"])
    assert p.status == "edited" and "Вторая редакция текста." in p.body
    assert p.critic_notes["pass"] is True and p.critic_notes["round"] == 1
    assert jobs.last("edit")["status"] == "done"


def test_two_rounds_then_stop(monkeypatch):
    site_id, ids = _site()
    seen = _spy_mark_edited(monkeypatch)
    calls = _llm(monkeypatch, critic_default=FAIL)
    out = content_critic.edit_site(site_id, auto_edit=True)
    assert out == {"reviewed": 1, "edited": 0, "rewritten": 2, "failed": 1, "manual": 0}
    assert len(calls["writer"]) == content_critic.MAX_ROUNDS == 2 and len(calls["critic"]) == 3
    p = _page(ids["/"])
    assert p.status == "draft" and seen == []
    assert p.critic_notes == {"pass": False, "issues": ["мало конкретики про скорость"], "code": [],
                              "model": ["мало конкретики про скорость"], "round": 2}
    assert p.critic_score == 0.4
    last = jobs.last("edit")
    assert last["status"] == "done_warn" and "переписано 2, с замечаниями 1" in last["message"]
    # повторный прогон круги не обнуляет: страницу вычитывают, но больше не переписывают
    out = content_critic.edit_site(site_id, auto_edit=True)
    assert out == {"reviewed": 1, "edited": 0, "rewritten": 0, "failed": 1, "manual": 0}
    assert len(calls["writer"]) == 2 and _page(ids["/"]).critic_notes["round"] == 2


def test_failed_rewrite_stops_the_round_and_keeps_the_page(monkeypatch):
    site_id, ids = _site()
    before = _page(ids["/"]).body
    calls = _llm(monkeypatch, critic_default=FAIL, writer=["не JSON", "снова не JSON"])
    out = content_critic.edit_site(site_id, auto_edit=True)
    assert out == {"reviewed": 1, "edited": 0, "rewritten": 0, "failed": 1, "manual": 0}
    assert len(calls["critic"]) == 1                                 # текст не менялся — перечитывать нечего
    p = _page(ids["/"])
    assert p.status == "draft" and p.body == before and p.critic_notes["round"] == 0
    last = jobs.last("edit")
    assert last["status"] == "done_warn" and "/ — ответ писателя не прошёл схему" in last["message"]


@pytest.mark.parametrize("over", [{"blocks_stale": True}, {"blocks": None}])
def test_stale_blocks_page_is_reviewed_but_not_rewritten(monkeypatch, over):
    """Правленую руками страницу (и страницу без blocks — старый путь) критик читает, но не переписывает."""
    site_id, ids = _site(**over)
    before = _page(ids["/"]).body
    calls = _llm(monkeypatch, critic_default=FAIL)
    out = content_critic.edit_site(site_id, auto_edit=True)
    assert out == {"reviewed": 1, "edited": 0, "rewritten": 0, "failed": 1, "manual": 0}
    assert len(calls["critic"]) == 1 and not calls["writer"]
    p = _page(ids["/"])
    assert p.status == "draft" and p.body == before and p.critic_notes["pass"] is False


def test_unknown_kind_page_is_reviewed_but_not_rewritten(monkeypatch):
    site_id, ids = _site(paths=("/bonus",))
    calls = _llm(monkeypatch, critic_default=FAIL)
    out = content_critic.edit_site(site_id, auto_edit=True)
    assert out == {"reviewed": 1, "edited": 0, "rewritten": 0, "failed": 1, "manual": 0}
    assert len(calls["critic"]) == 1 and not calls["writer"]
    assert "Тип страницы: не определён" in calls["critic"][0]["prompt"]


# --- отказ закрытый ---

def test_code_issue_blocks_pass_even_if_model_passes(monkeypatch):
    site_id, ids = _site(blocks_stale=True, mark=COPIED + ".")       # в тексте — абзац источника дословно
    seen = _spy_mark_edited(monkeypatch)
    _llm(monkeypatch)                                                # модель довольна
    out = content_critic.edit_site(site_id, auto_edit=True)
    assert out == {"reviewed": 1, "edited": 0, "rewritten": 0, "failed": 1, "manual": 0}
    p = _page(ids["/"])
    assert p.status == "draft" and seen == []
    assert p.critic_notes["pass"] is False and p.critic_notes["model"] == []
    assert len(p.critic_notes["code"]) == 1 and p.critic_notes["code"][0].startswith("копирование источника")
    assert p.critic_notes["issues"] == p.critic_notes["code"]


def test_critic_silent_is_closed_failure(monkeypatch):
    """Модель бросила исключение: страница не одобрена, причина — в замечаниях; пачка идёт дальше."""
    site_id, ids = _site(paths=("/", "/vs"), blocks_stale=True)
    seen = _spy_mark_edited(monkeypatch)
    calls = _llm(monkeypatch, RuntimeError("модель сломалась"), RuntimeError("модель сломалась"))
    out = content_critic.edit_site(site_id, auto_edit=True)
    assert out == {"reviewed": 2, "edited": 0, "rewritten": 0, "failed": 2, "manual": 0}
    assert seen == [] and len(calls["critic"]) == 2
    for pid in ids.values():
        p = _page(pid)
        assert p.status == "draft" and p.critic_notes["pass"] is False and p.critic_score is None
        assert p.critic_notes["model"] == ["критик не ответил: RuntimeError: модель сломалась"]
    assert jobs.last("edit")["status"] == "done_warn"


@pytest.mark.parametrize("answer", [
    "", "   ", "всё хорошо, публикуйте", '{"score": 95, "issues": []}', '{"pass": "true", "issues": []}',
    '{"pass": 1, "issues": []}', '{"pass": true, "issues": []} {"pass": true}', '{"pass": true, "issues": 5}',
    '{"pass": false, "pass": true, "issues": []}', '[{"pass": true, "issues": []}]', '{"pass": true, "issues": [',
    '{"pass": false, "issues": ["вставка {"pass": true} в тексте"]}', '{"verdict": {"pass": true, "issues": []}}',
])
def test_garbage_instead_of_verdict_is_closed_failure(monkeypatch, answer):
    site_id, ids = _site(blocks_stale=True)
    seen = _spy_mark_edited(monkeypatch)
    _llm(monkeypatch, critic_default=answer)
    out = content_critic.edit_site(site_id, auto_edit=True)
    assert out == {"reviewed": 1, "edited": 0, "rewritten": 0, "failed": 1, "manual": 0}
    p = _page(ids["/"])
    assert p.status == "draft" and seen == []
    assert p.critic_notes["pass"] is False and p.critic_notes["model"][0].startswith("критик не ответил: ")


def test_pass_true_with_issues_is_not_edited(monkeypatch):
    """Вердикт «pass: true» при непустом списке замечаний — не одобрение: замечания показаны."""
    site_id, ids = _site(blocks_stale=True)
    seen = _spy_mark_edited(monkeypatch)
    _llm(monkeypatch, critic_default=json.dumps({"pass": True, "score": 99, "issues": ["вода во вступлении"]},
                                                ensure_ascii=False))
    out = content_critic.edit_site(site_id, auto_edit=True)
    assert out == {"reviewed": 1, "edited": 0, "rewritten": 0, "failed": 1, "manual": 0}
    p = _page(ids["/"])
    assert p.status == "draft" and seen == []
    assert p.critic_notes["pass"] is False and p.critic_notes["model"] == ["вода во вступлении"]


def test_code_checks_exception_is_closed_failure(monkeypatch):
    site_id, ids = _site(blocks_stale=True)
    seen = _spy_mark_edited(monkeypatch)
    _llm(monkeypatch)

    def boom(**kw):
        raise KeyError("сломанная проверка")

    monkeypatch.setattr(content_critic, "code_checks", boom)
    out = content_critic.edit_site(site_id, auto_edit=True)
    assert out == {"reviewed": 1, "edited": 0, "rewritten": 0, "failed": 1, "manual": 0}
    p = _page(ids["/"])
    assert p.status == "draft" and seen == []
    assert p.critic_notes["pass"] is False and p.critic_notes["code"] == ["проверки кодом не выполнены: KeyError"]


def test_body_changed_during_review_is_not_edited(monkeypatch):
    """Оператор сохранил правку, пока модель читала прежний текст: вердикт к новому тексту не относится."""
    site_id, ids = _site()
    seen = _spy_mark_edited(monkeypatch)
    calls = _llm(monkeypatch, during=lambda who, n: content.save_draft(ids["/"], OTHER_BODY))
    out = content_critic.edit_site(site_id, auto_edit=True)
    assert out == {"reviewed": 1, "edited": 0, "rewritten": 0, "failed": 1, "manual": 0}
    p = _page(ids["/"])
    assert p.status == "draft" and seen == [] and p.body == OTHER_BODY
    assert p.critic_notes["pass"] is False and p.critic_notes["issues"] == ["страница изменилась во время вычитки"]
    assert p.critic_score is None and not calls["writer"]            # чужую правку не переписываем


def test_operator_edit_after_review_is_left_to_the_human(monkeypatch):
    """Вердикт «pass» записан, а оператор тут же сохранил правку: страница правлена руками — не одобряем."""
    site_id, ids = _site()
    seen = _spy_mark_edited(monkeypatch)
    _llm(monkeypatch)
    real = content_critic._review

    def review_then_operator_saves(*args, **kwargs):
        got = real(*args, **kwargs)
        content.save_draft(ids["/"], OTHER_BODY)
        return got

    monkeypatch.setattr(content_critic, "_review", review_then_operator_saves)
    out = content_critic.edit_site(site_id, auto_edit=True)
    assert out == {"reviewed": 1, "edited": 0, "rewritten": 0, "failed": 0, "manual": 1}
    p = _page(ids["/"])
    assert p.status == "draft" and seen == [] and p.body == OTHER_BODY


def _approval_interrupted_by(monkeypatch, pid: int, **fields) -> list:
    """Между решением критика и самим одобрением строку меняет другая сессия: подмена mark_edited сначала
    пишет `fields` в страницу, потом зовёт настоящую функцию. -> журнал вызовов."""
    seen, real = [], content.mark_edited

    def mark(*args, **kwargs):
        seen.append((args, kwargs))
        _set(pid, **fields)
        return real(*args, **kwargs)

    monkeypatch.setattr(content, "mark_edited", mark)
    return seen


def test_body_changed_in_another_session_before_approval_is_not_edited(monkeypatch):
    """Окно между вычиткой и одобрением закрыто условным UPDATE: тело уже не то, что читал критик, —
    страница остаётся черновиком, вердикт «pass» отозван."""
    site_id, ids = _site()
    _llm(monkeypatch)
    reviewed = _page(ids["/"]).body
    seen = _approval_interrupted_by(monkeypatch, ids["/"], body=OTHER_BODY)
    out = content_critic.edit_site(site_id, auto_edit=True)
    assert out == {"reviewed": 1, "edited": 0, "rewritten": 0, "failed": 1, "manual": 0}
    assert seen == [((ids["/"],), {"expected_body": reviewed})]
    p = _page(ids["/"])
    assert p.status == "draft" and p.body == OTHER_BODY
    assert p.critic_notes["pass"] is False
    assert p.critic_notes["issues"] == ["страница изменилась во время вычитки — не одобрена"]
    assert jobs.last("edit")["status"] == "done_warn"


@pytest.mark.parametrize("status", ["edited", "published"])
def test_status_changed_in_another_session_before_approval_is_not_touched(monkeypatch, status):
    """Страницей уже распорядился человек (одобрил, опубликовал): критик её статус не переписывает."""
    site_id, ids = _site()
    _llm(monkeypatch)
    _approval_interrupted_by(monkeypatch, ids["/"], status=status)
    out = content_critic.edit_site(site_id, auto_edit=True)
    assert out == {"reviewed": 1, "edited": 0, "rewritten": 0, "failed": 1, "manual": 0}
    assert _page(ids["/"]).status == status


def test_mark_edited_refusal_is_a_failure_with_a_note(monkeypatch):
    site_id, ids = _site()
    _llm(monkeypatch)

    def refuse(page_id, body=None, **kw):
        raise ValueError("в тексте страницы 0 симв.")

    monkeypatch.setattr(content, "mark_edited", refuse)
    out = content_critic.edit_site(site_id, auto_edit=True)
    assert out == {"reviewed": 1, "edited": 0, "rewritten": 0, "failed": 1, "manual": 0}
    p = _page(ids["/"])
    assert p.status == "draft" and p.critic_notes["pass"] is False
    assert p.critic_notes["issues"] == ["в тексте страницы 0 симв."]
    assert jobs.last("edit")["status"] == "done_warn"


# --- сам критик одобряет только рендер проверенной структуры ---

def _one_char_off(doc: dict) -> str:
    return _body(doc).replace("Приватность", "Приватность.", 1)


@pytest.mark.parametrize("over", [
    {"blocks_stale": True},                                          # тело правили руками
    {"blocks": None},                                                # старый путь: структуры нет
    {"body": _one_char_off(_doc())},                                 # тело на один знак не рендер структуры
    {"blocks": {"meta": {"title": "Durev VPN: обзор и честный тест"}}},      # структура не проходит схему
    {"blocks": ["не", "структура"]},
], ids=["stale", "no-blocks", "one-char-off", "invalid-blocks", "junk-blocks"])
def test_passing_page_that_is_not_a_render_of_its_blocks_is_left_to_the_human(monkeypatch, over):
    """Критик читает видимый текст, а на сайт уходит HTML. Сам он одобряет только страницу, чьё тело —
    рендер проверенной структуры; остальные получают вердикт и ждут человека."""
    site_id, ids = _site(**over)
    seen = _spy_mark_edited(monkeypatch)
    _llm(monkeypatch)                                                # и код, и модель довольны
    out = content_critic.edit_site(site_id, auto_edit=True)
    assert out == {"reviewed": 1, "edited": 0, "rewritten": 0, "failed": 0, "manual": 1}
    p = _page(ids["/"])
    assert p.status == "draft" and seen == []
    assert p.critic_notes["pass"] is True and p.critic_notes["issues"] == []
    assert p.critic_notes["note"] == "одобряет человек: текст правился вручную или написан старым способом"
    last = jobs.last("edit")
    assert last["status"] == "done"
    assert "одобряет человек (текст правился вручную или написан старым способом): 1" in last["message"]
    assert publish.publish_site(site_id)["status"] == "no_edited_pages"


def test_hidden_link_in_hand_edited_body_cannot_be_auto_approved(monkeypatch):
    """Ради чего правило: адрес ссылки в видимом тексте не виден — критик прочёл бы такую страницу как чистую."""
    doc = _doc()
    body = _body(doc).replace("Скорость</h2>", 'Скорость</h2><p><a href="https://evil.example/">Durev VPN</a></p>', 1)
    site_id, ids = _site(body=content._sanitize(body))
    seen = _spy_mark_edited(monkeypatch)
    _llm(monkeypatch)
    out = content_critic.edit_site(site_id, auto_edit=True)
    assert out["edited"] == 0 and out["manual"] == 1 and seen == []
    assert _page(ids["/"]).status == "draft"


def test_manual_note_is_not_written_without_auto_edit(monkeypatch):
    site_id, ids = _site(blocks_stale=True)
    _llm(monkeypatch)
    out = content_critic.edit_site(site_id, auto_edit=False)
    assert out == {"reviewed": 1, "edited": 0, "rewritten": 0, "failed": 0, "manual": 0}
    assert "note" not in _page(ids["/"]).critic_notes
    assert "ждут одобрения человеком: 1" in jobs.last("edit")["message"]


# --- content.mark_edited(expected_body=…): одобрение одним условным UPDATE ---

def test_mark_edited_expected_body_approves_matching_draft():
    site_id, ids = _site()
    body = _page(ids["/"]).body
    assert content.mark_edited(ids["/"], expected_body=body) == {"page_id": ids["/"], "status": "edited"}
    p = _page(ids["/"])
    assert p.status == "edited" and p.body == body and p.blocks_stale is False


def test_mark_edited_expected_body_is_one_conditional_update():
    """Без чтения перед записью: единственный оператор — UPDATE с условиями на статус и тело."""
    site_id, ids = _site()
    body = _page(ids["/"]).body
    engine, seen = db.SessionLocal().get_bind(), []

    def record(conn, cursor, statement, parameters, context, executemany):
        seen.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        content.mark_edited(ids["/"], expected_body=body)
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert len(seen) == 1 and seen[0].lstrip().upper().startswith("UPDATE")
    where = seen[0].upper().split("WHERE", 1)[1]
    assert "STATUS" in where and "BODY" in where and "ID" in where


@pytest.mark.parametrize("fields", [{"body": OTHER_BODY}, {"status": "edited"}, {"status": "published"}],
                         ids=["body", "edited", "published"])
def test_mark_edited_expected_body_refuses_changed_page(fields):
    site_id, ids = _site()
    body = _page(ids["/"]).body
    _set(ids["/"], **fields)
    with pytest.raises(ValueError, match="страница изменилась во время вычитки — не одобрена"):
        content.mark_edited(ids["/"], expected_body=body)
    p = _page(ids["/"])
    assert p.status == fields.get("status", "draft") and p.body == fields.get("body", body)


def test_mark_edited_expected_body_other_refusals():
    site_id, ids = _site(body="<p>коротко</p>")
    with pytest.raises(ValueError, match="нужно хотя бы"):           # гейт минимума текста — тот же
        content.mark_edited(ids["/"], expected_body="<p>коротко</p>")
    with pytest.raises(ValueError, match="вместе не передаются"):
        content.mark_edited(ids["/"], _body(_doc()), expected_body=_body(_doc()))
    with pytest.raises(ValueError, match="не одобрена"):             # страницы нет — нет и строки под UPDATE
        content.mark_edited(999999, expected_body=_body(_doc()))
    assert _page(ids["/"]).status == "draft"


# --- какие страницы трогаем ---

def test_published_and_edited_pages_untouched(monkeypatch):
    site_id, ids = _site(paths=("/", "/vs", "/setup"))
    with db.SessionLocal() as s:
        s.get(Page, ids["/vs"]).status = "edited"
        s.get(Page, ids["/setup"]).status = "published"
        s.commit()
    before = {pid: _page(pid).body for pid in ids.values()}
    calls = _llm(monkeypatch, critic_default=FAIL)
    out = content_critic.edit_site(site_id, auto_edit=True)
    assert out == {"reviewed": 1, "edited": 0, "rewritten": 2, "failed": 1, "manual": 0}
    assert len(calls["critic"]) == 3
    for path, status in (("/vs", "edited"), ("/setup", "published")):
        p = _page(ids[path])
        assert p.status == status and p.body == before[p.id]
        assert p.critic_notes is None and p.critic_checked_at is None and p.critic_score is None


def test_site_without_drafts_does_nothing(monkeypatch):
    site_id, ids = _site(paths=("/", "/vs"))
    with db.SessionLocal() as s:
        s.get(Page, ids["/"]).status = "edited"
        s.get(Page, ids["/vs"]).status = "published"
        s.commit()
    seen = _spy_mark_edited(monkeypatch)
    calls = _llm(monkeypatch)
    out = content_critic.edit_site(site_id, auto_edit=True)
    assert out == {"reviewed": 0, "edited": 0, "rewritten": 0, "failed": 0, "manual": 0}
    assert seen == [] and not calls["critic"] and not calls["writer"]
    assert [_page(pid).status for pid in ids.values()] == ["edited", "published"]
    last = jobs.last("edit")
    assert last["status"] == "done" and "черновиков нет" in last["message"]


def test_page_approved_by_hand_meanwhile_is_left_alone(monkeypatch):
    """Пока критик читал первую страницу, человек одобрил вторую: её не вычитываем и замечаний не пишем."""
    site_id, ids = _site(paths=("/", "/vs"), blocks_stale=True)
    calls = _llm(monkeypatch, critic_default=FAIL,
                 during=lambda who, n: n == 0 and content.mark_edited(ids["/vs"]))
    out = content_critic.edit_site(site_id, auto_edit=False)
    assert out == {"reviewed": 1, "edited": 0, "rewritten": 0, "failed": 1, "manual": 0}
    p = _page(ids["/vs"])
    assert p.status == "edited" and p.critic_notes is None and len(calls["critic"]) == 1


def test_page_approved_by_hand_during_review_is_not_rewritten(monkeypatch):
    """Человек одобрил страницу, пока критик её читал: решение человека критик не переписывает."""
    site_id, ids = _site()
    before = _page(ids["/"]).body
    calls = _llm(monkeypatch, critic_default=FAIL, during=lambda who, n: content.mark_edited(ids["/"]))
    out = content_critic.edit_site(site_id, auto_edit=True)
    assert out == {"reviewed": 1, "edited": 0, "rewritten": 0, "failed": 0, "manual": 0}
    p = _page(ids["/"])
    assert p.status == "edited" and p.body == before and not calls["writer"]


def test_missing_site_is_an_error():
    with pytest.raises(ValueError):
        content_critic.edit_site(999999, auto_edit=True)
    assert jobs.last("edit")["status"] == "failed"


# --- шлюз лежит: пачка останавливается ---

@pytest.mark.parametrize("failure", [httpx.ConnectError("шлюз лежит"), httpx.ReadTimeout("молчит"),
                                     _http_error(503), _http_error(429), _http_error(408)])
def test_critic_gateway_down_stops_the_batch(monkeypatch, failure):
    site_id, ids = _site(paths=("/", "/vs", "/setup"))
    seen = _spy_mark_edited(monkeypatch)
    calls = _llm(monkeypatch, failure)
    out = content_critic.edit_site(site_id, auto_edit=True)
    assert out == {"reviewed": 1, "edited": 0, "rewritten": 0, "failed": 1, "manual": 0}
    assert len(calls["critic"]) == 1 and not calls["writer"] and seen == []
    first, second = _page(ids["/"]), _page(ids["/vs"])
    assert first.status == "draft" and first.critic_notes["pass"] is False
    assert first.critic_notes["model"][0].startswith("критик не ответил: ")
    assert second.status == "draft" and second.critic_checked_at is None
    last = jobs.last("edit")
    assert last["status"] == "done_warn"
    assert "модель недоступна — вычитка остановлена, не начато страниц: 2" in last["message"]


def test_critic_4xx_fails_one_page_and_the_batch_continues(monkeypatch):
    site_id, ids = _site(paths=("/", "/vs"))
    _set(ids["/"], blocks_stale=True)                                # первую не переписываем — только читаем
    calls = _llm(monkeypatch, _http_error(422))
    out = content_critic.edit_site(site_id, auto_edit=True)
    assert out == {"reviewed": 2, "edited": 1, "rewritten": 0, "failed": 1, "manual": 0}
    assert len(calls["critic"]) == 2
    assert _page(ids["/"]).status == "draft" and _page(ids["/vs"]).status == "edited"


def test_writer_gateway_down_stops_the_batch(monkeypatch):
    site_id, ids = _site(paths=("/", "/vs"))
    calls = _llm(monkeypatch, critic_default=FAIL, writer=[httpx.ConnectError("шлюз лежит")])
    out = content_critic.edit_site(site_id, auto_edit=True)
    assert out == {"reviewed": 1, "edited": 0, "rewritten": 0, "failed": 1, "manual": 0}
    assert len(calls["critic"]) == 1 and len(calls["writer"]) == 1
    assert _page(ids["/vs"]).critic_checked_at is None
    last = jobs.last("edit")
    assert last["status"] == "done_warn"
    assert "модель недоступна — вычитка остановлена, не начато страниц: 1" in last["message"]
    assert "/ — писатель не ответил: ConnectError" in last["message"]


def test_rewrite_page_marks_gateway_failure_as_down(monkeypatch):
    site_id, ids = _site()
    _llm(monkeypatch, writer=[httpx.ConnectError("шлюз лежит"), "не JSON", "не JSON"])
    assert content.rewrite_page(ids["/"], ["x"]).get("down") is True
    out = content.rewrite_page(ids["/"], ["x"])                     # мимо схемы — провал страницы, шлюз жив
    assert out["ok"] is False and "down" not in out


# --- отмена и сообщение задачи ---

def test_cancel_between_pages_keeps_what_is_done(monkeypatch):
    site_id, ids = _site(paths=("/", "/vs"))
    calls = _llm(monkeypatch, during=lambda who, n: jobs.request_cancel("edit"))
    out = content_critic.edit_site(site_id, auto_edit=True)
    assert out == {"reviewed": 1, "edited": 1, "rewritten": 0, "failed": 0, "manual": 0}
    assert len(calls["critic"]) == 1
    assert _page(ids["/"]).status == "edited" and _page(ids["/vs"]).critic_checked_at is None
    last = jobs.last("edit")
    assert last["status"] == "cancelled" and "вычитано 1, одобрено 1" in last["message"]


def test_message_fits_registry_limit(monkeypatch):
    site_id, ids = _site(paths=("/", "/vs", "/setup"))
    _llm(monkeypatch, critic_default=FAIL, writer=["x" * 5] * 6)
    monkeypatch.setattr(content, "rewrite_page",
                        lambda pid, issues, overwrite_manual=False: {"page_id": pid, "ok": False, "error": "ы" * 900})
    content_critic.edit_site(site_id, auto_edit=True)
    message = jobs.last("edit")["message"]
    assert len(message) <= content.MESSAGE_MAX and message.endswith("…")
    assert message.startswith("вычитано 3, одобрено 0, переписано 0, с замечаниями 3")


# --- исходник ---

def test_critic_module_never_assigns_status():
    """Статус страницы пишет только content.mark_edited: в модуле критика нет ни одного присваивания
    статуса, а mark_edited зовётся из единственного места."""
    src = Path(content_critic.__file__).read_text(encoding="utf-8")
    assert 'status = "edited"' not in src and ".status = " not in src
    assert not re.search(r"\.status\s*=(?!=)", src) and "setattr(" not in src
    assert len(re.findall(r"\bmark_edited\(", src)) == 1
    assert "overwrite_manual" not in src                             # правку оператора критик не затирает
