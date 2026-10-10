"""Писатель по досье (план Б, задача 4): JSON по схеме PageDoc -> Page.blocks + рендер в Page.body,
один повтор с текстом ошибки схемы, переписывание страниц на месте, blocks_stale от ручной правки.
LLM — только подмена LlmClient.complete очередью ответов."""
import json
from datetime import datetime, timezone

import httpx
import pytest

import app.db as db
from app.config import settings
from app.models.domain import Domain
from app.models.offer import Offer
from app.models.research import SiteResearch
from app.models.site import Page, Site
from app.services import content, jobs, page_doc

DOC = {
    "meta": {"title": "Durev VPN: обзор и честный тест",
             "description": "Проверили скорость, приватность и цену Durev VPN — кому он подойдёт, а кому нет."},
    "verdict": {"score": 8, "summary": "Быстрый и недорогой сервис.",
                "for_whom": "Тем, кто смотрит стриминг", "not_for_whom": "Тем, кому нужен выделенный IP"},
    "pros": ["Цена 5.99 в месяц"], "cons": ["Мало серверов в Азии"],
    "sections": [{"h2": "Скорость", "paragraphs": ["Замеров в источниках нет — пишем без цифр."]},
                 {"h2": "Приватность", "paragraphs": ["Логи не ведутся."], "bullets": ["WireGuard", "OpenVPN"]}],
    "faq": [{"q": "Есть ли пробный период?", "a": "Да."}],
    "sources": ["https://review1.example/page"],
}
VALID = json.dumps(DOC, ensure_ascii=False)
OLD_BODY = "<h2>Старый текст</h2><p>Его писали по старому промпту, он достаточно длинный для гейта.</p>"
PATHS = ("/", "/vs", "/setup")


@pytest.fixture(autouse=True)
def _own_guides(tmp_path, monkeypatch):
    """Правила письма — из пустой tmp-папки: тесты не зависят от content_guides/ оператора."""
    monkeypatch.setattr(settings, "CONTENT_GUIDES_DIR", str(tmp_path / "guides"))
    return tmp_path / "guides"


def _site(dossier=True, status="content") -> int:
    with db.SessionLocal() as s:
        o = Offer(brand="Durev VPN", affiliate_link="https://durevpn.example/aff", language="ru", country="DE",
                  promo_code="DUREV20", promo_terms="скидка 20% на первый год", active=True)
        d = Domain(domain="t.xyz", source="list", status="purchased", market_lang="ru")
        s.add_all([o, d]); s.commit()
        site = Site(domain_id=d.id, status=status, doc_root="/www/wwwroot/t.xyz", offer_id=o.id)
        s.add(site); s.commit()
        if dossier:
            for kind in ("review", "comparison", "howto"):
                for rank in (1, 2):
                    s.add(SiteResearch(
                        site_id=site.id, kind=kind, query=f"durev vpn {kind}", rank=rank,
                        url=f"https://{kind}{rank}.example/page", domain=f"{kind}{rank}.example", words=1200,
                        headings=[["h2", "Цена"], ["h2", "Скорость"]],
                        numbers=[{"value": "5.99", "ctx": "цена 5.99 в месяц"}], text="цена 5.99 в месяц"))
            s.commit()
        return site.id


def _pages(site_id: int) -> list[Page]:
    with db.SessionLocal() as s:
        return s.query(Page).filter(Page.site_id == site_id).order_by(Page.id).all()


def _add_pages(site_id: int, status="published", **over) -> dict:
    """Три страницы сайта «как после прошлого прогона»; -> {путь: id}."""
    with db.SessionLocal() as s:
        offer_id = s.get(Site, site_id).offer_id
        rows = [Page(**{**dict(site_id=site_id, url_path=path, title="Старый заголовок", status=status,
                               body=OLD_BODY, lang="ru", offer_id=offer_id), **over}) for path in PATHS]
        s.add_all(rows); s.commit()
        return {p.url_path: p.id for p in rows}


def _llm(monkeypatch, *answers, default=VALID, during=None) -> list[dict]:
    """Подмена LlmClient.complete: ответы по очереди, дальше `default`; исключение в очереди — бросается.
    `during(n)` зовётся внутри вызова №n (с нуля) — «пока модель пишет». -> журнал вызовов."""
    calls, queue = [], list(answers)

    def complete(self, system, prompt, **kw):
        calls.append({"system": system, "prompt": prompt, "timeout": self._client.timeout.read, **kw})
        if during:
            during(len(calls) - 1)
        ans = queue.pop(0) if queue else default
        if isinstance(ans, Exception):
            raise ans
        return ans

    monkeypatch.setattr("app.integrations.llm.LlmClient.complete", complete)
    return calls


# --- новый путь: досье есть ---

def test_generate_with_dossier_writes_blocks_and_rendered_body(monkeypatch):
    site_id = _site()
    calls = _llm(monkeypatch)
    assert content.generate_site(site_id) == 3
    pages = _pages(site_id)
    assert [p.url_path for p in pages] == list(PATHS) and len(calls) == 3
    for p in pages:
        assert p.status == "draft" and p.blocks_stale is False
        assert isinstance(p.blocks, dict) and len(p.blocks["sections"]) == 2
        assert p.blocks == page_doc.PageDoc.model_validate(DOC).model_dump()
        assert "<h2>Скорость</h2>" in p.body and "<li>WireGuard</li>" in p.body
        assert p.title == DOC["meta"]["title"]
        assert p.lang == "ru" and p.offer_id is not None
    assert jobs.last("generate")["status"] == "done"
    assert all(c["timeout"] == 600 for c in calls)          # страница на 2000 слов идёт минуты
    # бриф собран из досье: источники своего типа, факт с номером источника
    assert "https://review1.example/page" in calls[0]["prompt"] and "5.99" in calls[0]["prompt"]
    assert "https://comparison1.example/page" in calls[1]["prompt"]
    assert "review1.example" not in calls[1]["prompt"]


def test_generate_without_dossier_uses_legacy_path(monkeypatch):
    site_id = _site(dossier=False)
    calls = _llm(monkeypatch, default="<h2>ok</h2><p>старый путь</p>")
    assert content.generate_site(site_id) == 3
    for p in _pages(site_id):
        assert p.blocks is None and p.blocks_stale is False
        assert p.body == "<h2>ok</h2><p>старый путь</p>" and p.status == "draft"
    assert all("model" not in c for c in calls)             # старый путь модель писателя не выбирает


def test_writer_accepts_fenced_json_with_prose_around(monkeypatch):
    site_id = _site()
    calls = _llm(monkeypatch, default=f"Вот страница:\n```json\n{VALID}\n```\nГотово, проверь.")
    assert content.generate_site(site_id) == 3
    assert len(calls) == 3                                   # ограда и текст вокруг — не повод для повтора
    assert all("<h2>Приватность</h2>" in p.body for p in _pages(site_id))


def test_writer_retries_once_with_schema_error(monkeypatch):
    site_id = _site()
    calls = _llm(monkeypatch, "мусор")
    assert content.generate_site(site_id) == 3
    assert len(_pages(site_id)) == 3 and len(calls) == 4
    assert "не прошёл проверку схемы" not in calls[0]["prompt"]
    retry = calls[1]["prompt"]
    assert retry.startswith(calls[0]["prompt"]) and "не прошёл проверку схемы: в ответе нет JSON-объекта" in retry
    assert "ТОЛЬКО" in retry and "без текста до и после" in retry
    assert "не прошёл проверку схемы" not in calls[2]["prompt"]     # ошибка одной страницы не течёт в следующую
    assert jobs.last("generate")["status"] == "done"


def test_writer_two_failures_creates_no_page_and_warns(monkeypatch):
    site_id = _site()
    calls = _llm(monkeypatch, default=VALID[: len(VALID) // 2])     # обрезанный JSON, каждый раз
    assert content.generate_site(site_id) == 0
    assert _pages(site_id) == [] and len(calls) == 6                # по два вызова на страницу, не больше
    last = jobs.last("generate")
    assert last["status"] == "done_warn"
    assert "не прошёл схему" in last["message"] and "/vs" in last["message"]
    assert "написано 0 из 3" in last["message"]


def test_one_failed_page_does_not_stop_the_batch(monkeypatch):
    site_id = _site()
    _llm(monkeypatch, VALID, "мусор", "снова мусор")                # «/vs» провалена дважды
    assert content.generate_site(site_id) == 2
    assert [p.url_path for p in _pages(site_id)] == ["/", "/setup"]
    last = jobs.last("generate")
    assert last["status"] == "done_warn" and "написано 2 из 3" in last["message"]
    # повторный прогон дописывает только недостающую страницу
    calls = _llm(monkeypatch)
    assert content.generate_site(site_id) == 1 and len(calls) == 1
    assert [p.url_path for p in _pages(site_id)] == ["/", "/setup", "/vs"]


def test_generate_without_rewrite_keeps_existing_pages(monkeypatch):
    site_id = _site()
    with db.SessionLocal() as s:
        s.add(Page(site_id=site_id, url_path="/", title="Старый заголовок", status="edited", body=OLD_BODY,
                   lang="ru", offer_id=s.get(Site, site_id).offer_id))
        s.commit()
    calls = _llm(monkeypatch)
    assert content.generate_site(site_id) == 2 and len(calls) == 2
    home = _pages(site_id)[0]
    assert home.body == OLD_BODY and home.status == "edited" and home.blocks is None


# --- write_doc: попытки ---

def _write(monkeypatch, *answers, **kw):
    from app.integrations.llm import LlmClient
    calls = _llm(monkeypatch, *answers)
    return content.write_doc(LlmClient(), system="SYS", prompt="PROMPT", **kw), calls


def test_write_doc_exception_is_a_failed_attempt(monkeypatch):
    (doc, err), calls = _write(monkeypatch, httpx.ReadTimeout("шлюз молчит"))
    assert err is None and doc.meta.title == DOC["meta"]["title"]
    assert [c["prompt"] for c in calls] == ["PROMPT", "PROMPT"]     # схема не при чём — промпт без добавки

    (doc, err), calls = _write(monkeypatch, httpx.ReadTimeout("шлюз молчит"), RuntimeError("шлюз упал"))
    assert doc is None and "RuntimeError" in err and "шлюз упал" in err and len(calls) == 2


def test_write_doc_empty_answer_is_a_failed_attempt(monkeypatch):
    (doc, err), calls = _write(monkeypatch, "", "   ")
    assert doc is None and "пустой ответ" in err and len(calls) == 2


def test_write_doc_schema_error_names_the_field(monkeypatch):
    bad = json.dumps({**DOC, "sections": DOC["sections"][:1]}, ensure_ascii=False)
    (doc, err), calls = _write(monkeypatch, bad, bad)
    assert doc is None and err.startswith("ответ писателя не прошёл схему: sections")
    assert "sections" in calls[1]["prompt"]


def test_writer_uses_writer_model(monkeypatch):
    monkeypatch.setattr(settings, "LLM_MODEL", "base")
    monkeypatch.setattr(settings, "LLM_WRITER_MODEL", "opus")
    site_id = _site()
    calls = _llm(monkeypatch)
    content.generate_site(site_id)
    assert [c["model"] for c in calls] == ["opus"] * 3

    monkeypatch.setattr(settings, "LLM_WRITER_MODEL", "")           # пусто -> модель для текстов
    (_, err), calls = _write(monkeypatch)
    assert err is None and calls[0]["model"] == "base"


# --- промпты ---

def test_prompt_has_guides_and_promo_terms(monkeypatch, _own_guides):
    for sub, text in (("", "ПРАВИЛО-ОБЩЕЕ"), ("ru", "ПРАВИЛО-ЯЗЫКА"), ("en", "ПРАВИЛО-ЧУЖОГО-ЯЗЫКА"),
                      ("review", "ПРАВИЛО-ОБЗОРА"), ("howto", "ПРАВИЛО-ИНСТРУКЦИИ")):
        (_own_guides / sub).mkdir(parents=True, exist_ok=True)
        (_own_guides / sub / "rules.md").write_text(text, encoding="utf-8")
    site_id = _site()
    calls = _llm(monkeypatch)
    content.generate_site(site_id)
    review, vs, howto = calls
    for c in calls:
        assert "ПРАВИЛО-ОБЩЕЕ" in c["system"] and "ПРАВИЛО-ЯЗЫКА" in c["system"]
        assert "ПРАВИЛО-ЧУЖОГО-ЯЗЫКА" not in c["system"]
        assert "Russian" in c["system"] and "DE" in c["system"]            # язык вывода и рынок названы
        assert "DUREV20" in c["prompt"] and "скидка 20% на первый год" in c["prompt"]
        assert "Durev VPN" in c["prompt"]
    assert "ПРАВИЛО-ОБЗОРА" in review["system"] and "ПРАВИЛО-ОБЗОРА" not in vs["system"]
    assert "ПРАВИЛО-ИНСТРУКЦИИ" in howto["system"] and "ПРАВИЛО-ИНСТРУКЦИИ" not in review["system"]


def test_writer_system_names_every_pagedoc_field():
    """Схема в промпте — текстом, а PageDoc живёт в page_doc.py: имя поля, которого нет в промпте,
    модель не напишет (или напишет под другим именем, и pydantic его молча отбросит)."""
    system = content.writer_system("de", "DE", "")
    for model in (page_doc.PageDoc, page_doc.Meta, page_doc.Verdict, page_doc.Section, page_doc.H3,
                  page_doc.Table, page_doc.Step, page_doc.Faq):
        for name in model.model_fields:
            assert f'"{name}"' in system, f"{model.__name__}.{name}"
    assert "German" in system and "DE" in system
    assert "Правила письма оператора" not in system                 # правил нет — блока нет
    assert "ПРАВИЛО" in content.writer_system("de", None, "ПРАВИЛО") and "```" in system
    for kind in ("review", "comparison", "howto"):
        assert kind in system


# --- переписывание ---

def test_rewrite_updates_existing_pages_in_place(monkeypatch):
    site_id = _site(status="published")
    stamp = datetime(2026, 10, 1, tzinfo=timezone.utc)
    ids = _add_pages(site_id, critic_score=0.9, critic_notes={"issues": ["x"]}, critic_checked_at=stamp,
                     published_at=stamp, index_status="indexed")
    _llm(monkeypatch)
    assert content.generate_site(site_id, rewrite=True) == 3
    pages = _pages(site_id)
    assert {p.url_path: p.id for p in pages} == ids                 # те же строки, не новые
    for p in pages:
        assert p.status == "draft" and "<h2>Скорость</h2>" in p.body and "Старый текст" not in p.body
        assert p.title == DOC["meta"]["title"] and p.blocks["sections"] and p.blocks_stale is False
        assert p.critic_score is None and p.critic_notes is None and p.critic_checked_at is None
        assert p.published_at is not None and p.index_status == "indexed"       # история публикации цела
        assert p.lang == "ru" and p.offer_id is not None
    assert jobs.last("generate")["status"] == "done"


def test_rewrite_skips_manually_edited_page(monkeypatch):
    site_id = _site()
    ids = _add_pages(site_id, status="edited")
    with db.SessionLocal() as s:
        s.get(Page, ids["/vs"]).blocks_stale = True
        s.commit()
    calls = _llm(monkeypatch)
    assert content.generate_site(site_id, rewrite=True) == 2 and len(calls) == 2    # на правленую модель не зовём
    by_path = {p.url_path: p for p in _pages(site_id)}
    assert by_path["/vs"].body == OLD_BODY and by_path["/vs"].status == "edited" and by_path["/vs"].blocks_stale
    assert by_path["/"].status == "draft" and "Старый текст" not in by_path["/"].body
    last = jobs.last("generate")
    assert last["status"] == "done" and "правлены вручную: 1" in last["message"]


def test_rewrite_failure_keeps_old_page_intact(monkeypatch):
    site_id = _site(status="published")
    stamp = datetime(2026, 10, 1, tzinfo=timezone.utc)
    _add_pages(site_id, critic_score=0.9, critic_checked_at=stamp, blocks={"old": True})
    _llm(monkeypatch, VALID, "```json\n{\"meta\": ", "{}")             # «/vs»: обрезан, затем не той формы
    assert content.generate_site(site_id, rewrite=True) == 2
    vs = {p.url_path: p for p in _pages(site_id)}["/vs"]
    assert vs.body == OLD_BODY and vs.status == "published" and vs.title == "Старый заголовок"
    assert vs.blocks == {"old": True} and vs.critic_score == 0.9 and vs.critic_checked_at is not None
    last = jobs.last("generate")
    assert last["status"] == "done_warn" and "/vs" in last["message"]


def test_rewrite_does_not_overwrite_page_edited_while_model_wrote(monkeypatch):
    """Вызов модели идёт минуты, редактор панели всё это время открыт: правка, сохранённая посреди
    прогона, не затирается. Страницы старого пути (blocks нет) флага blocks_stale не получают —
    их ловит сравнение тела."""
    site_id = _site()
    ids = _add_pages(site_id, status="draft")
    edited = "<p>Оператор переписал главную руками, пока модель думала над ней.</p>"
    _llm(monkeypatch, during=lambda n: n == 0 and content.save_draft(ids["/"], edited))
    assert content.generate_site(site_id, rewrite=True) == 2
    home = {p.url_path: p for p in _pages(site_id)}["/"]
    assert home.body == edited and home.blocks is None
    last = jobs.last("generate")
    assert last["status"] == "done" and "правлены вручную: 1" in last["message"]


def test_rewrite_without_dossier_touches_nothing_and_says_so(monkeypatch):
    site_id = _site(dossier=False)
    _add_pages(site_id, status="edited")
    calls = _llm(monkeypatch)
    assert content.generate_site(site_id, rewrite=True) == 0 and calls == []
    assert all(p.body == OLD_BODY and p.status == "edited" for p in _pages(site_id))
    assert "досье" in jobs.last("generate")["message"]


def test_rewrite_page_passes_issues_to_prompt(monkeypatch):
    site_id = _site()
    ids = _add_pages(site_id, status="draft", critic_score=0.3, critic_notes={"issues": ["мало фактов"]})
    calls = _llm(monkeypatch)
    out = content.rewrite_page(ids["/vs"], ["Нет таблицы сравнения", "Число 99 без источника"])
    assert out == {"page_id": ids["/vs"], "ok": True, "error": None}
    assert len(calls) == 1
    prompt = calls[0]["prompt"]
    assert "Замечания редактора, которые нужно устранить" in prompt
    assert "- Нет таблицы сравнения" in prompt and "- Число 99 без источника" in prompt
    assert "https://comparison1.example/page" in prompt and "сравнение" in prompt       # тип — по url_path
    by_path = {p.url_path: p for p in _pages(site_id)}
    vs = by_path["/vs"]
    assert vs.id == ids["/vs"] and vs.status == "draft" and "<h2>Скорость</h2>" in vs.body
    assert vs.blocks["sections"] and vs.critic_score is None and vs.critic_notes is None
    assert by_path["/"].body == OLD_BODY and by_path["/"].critic_score == 0.3          # соседей не трогает


def test_rewrite_page_refuses_without_dossier_or_on_manual_edit(monkeypatch):
    calls = _llm(monkeypatch)
    bare = _add_pages(_site(dossier=False), status="draft")
    out = content.rewrite_page(bare["/"], ["x"])
    assert out["ok"] is False and "досье" in out["error"] and out["page_id"] == bare["/"]

    with db.SessionLocal() as s:                                    # второй сайт — свой домен
        d = Domain(domain="u.xyz", source="list", status="purchased", market_lang="ru")
        o = Offer(brand="Durev VPN", affiliate_link="https://durevpn.example/aff", language="ru", active=True)
        s.add_all([d, o]); s.commit()
        site = Site(domain_id=d.id, status="content", doc_root="/www/wwwroot/u.xyz", offer_id=o.id)
        s.add(site); s.commit()
        s.add(SiteResearch(site_id=site.id, kind="review", query="q", rank=1, url="https://a.example/"))
        p = Page(site_id=site.id, url_path="/", title="t", status="draft", body=OLD_BODY, lang="ru",
                 offer_id=o.id, blocks=DOC, blocks_stale=True)
        s.add(p); s.commit()
        stale_id = p.id
    out = content.rewrite_page(stale_id, ["x"])
    assert out["ok"] is False and "вручную" in out["error"]
    assert content.rewrite_page(10_000, ["x"])["ok"] is False
    assert calls == []                                              # ни один отказ не стоил вызова модели


def test_rewrite_page_failure_keeps_page_and_returns_reason(monkeypatch):
    site_id = _site()
    ids = _add_pages(site_id, status="edited", critic_score=0.4)
    _llm(monkeypatch, default="не JSON")
    out = content.rewrite_page(ids["/"], ["x"])
    assert out["ok"] is False and "не прошёл схему" in out["error"]
    home = _pages(site_id)[0]
    assert home.body == OLD_BODY and home.status == "edited" and home.critic_score == 0.4


# --- blocks_stale: ручная правка ---

def _written_page(monkeypatch) -> Page:
    site_id = _site()
    _llm(monkeypatch)
    content.generate_site(site_id)
    return _pages(site_id)[0]


def test_save_draft_marks_blocks_stale(monkeypatch):
    p = _written_page(monkeypatch)
    content.save_draft(p.id, p.body.replace("\n", "\r\n"))      # форма шлёт \r\n — это не правка
    assert _pages(p.site_id)[0].blocks_stale is False
    content.save_draft(p.id, p.body + "<p>Дописано руками.</p>")
    got = _pages(p.site_id)[0]
    assert got.blocks_stale is True and got.blocks == p.blocks and "Дописано руками" in got.body


def test_save_draft_on_legacy_page_has_nothing_to_mark(monkeypatch):
    ids = _add_pages(_site(dossier=False), status="draft")
    content.save_draft(ids["/"], "<p>Правка страницы старого пути: блоков у неё нет и не было.</p>")
    with db.SessionLocal() as s:
        assert s.get(Page, ids["/"]).blocks_stale is False


def test_mark_edited_without_body_keeps_blocks_fresh(monkeypatch):
    p = _written_page(monkeypatch)
    assert content.mark_edited(p.id)["status"] == "edited"
    assert _pages(p.site_id)[0].blocks_stale is False
    content.mark_edited(p.id, p.body)                           # одобрили как лежит, через форму
    assert _pages(p.site_id)[0].blocks_stale is False
    content.mark_edited(p.id, p.body + "<p>Редактор дописал абзац.</p>")
    got = _pages(p.site_id)[0]
    assert got.blocks_stale is True and got.status == "edited"


# --- факты бренда ---

def test_vertical_block_skips_unknown_fields(monkeypatch):
    from app.services import vertical_data as vd
    facts = {"brand": "Durev VPN", "servers": "неизвестно", "countries": 30, "jurisdiction": "неизвестно",
             "protocols": [], "no_logs": "заявлен, аудита нет", "audit": "неизвестно", "streaming": "Неизвестно",
             "devices": 5, "price_from": "$2.99/мес", "refund_days": "неизвестно", "extras": []}
    monkeypatch.setitem(vd.VPN_FACTS, "durevvpn", facts)
    block = vd.vertical_block("Durev VPN")
    assert "неизвестно" not in block.lower()
    for gone in ("Юрисдикция", "Протоколы", "аудиты", "Стриминг", "Ключевые фичи", "Серверы", "возврат"):
        assert gone not in block, gone
    assert "- Стран: 30." in block and "- No-logs: заявлен, аудита нет." in block
    assert "- Устройств одновременно: 5." in block and "- Цена от: $2.99/мес." in block

    monkeypatch.setitem(vd.VPN_FACTS, "durevvpn", {**facts, "servers": "900+", "refund_days": 30,
                                                    "price_from": "неизвестно"})
    block = vd.vertical_block("Durev VPN")
    assert "- Серверы: 900+ в 30 странах." in block and "- Возврат в течение 30 дней." in block
    assert "Цена" not in block

    empty = {k: ("Durev VPN" if k == "brand" else [] if k in ("protocols", "extras") else "неизвестно") for k in facts}
    monkeypatch.setitem(vd.VPN_FACTS, "durevvpn", empty)
    assert vd.vertical_block("Durev VPN") is None               # одни пропуски — фактов нет вовсе

    nord = vd.vertical_block("NordVPN")                         # заполненный бренд печатается как раньше
    assert "- Серверы: 5500+ в 60 странах." in nord
    assert "- Цена от: $3.09/мес (2 года); возврат в течение 30 дней." in nord
    assert "- Протоколы: NordLynx (WireGuard), OpenVPN, IKEv2." in nord
