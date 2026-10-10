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


def _http_error(code: int, message: str = "") -> httpx.HTTPStatusError:
    """Отказ шлюза, как его бросает httpx.raise_for_status: в тексте исключения — ссылка на MDN,
    слова самого шлюза — только в теле ответа."""
    req = httpx.Request("POST", "http://llm.example/v1/chat/completions")
    resp = httpx.Response(code, json={"error": {"message": message}} if message else {}, request=req)
    return httpx.HTTPStatusError(f"Client error '{code}' for url '{req.url}'\nFor more information check: "
                                 f"https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/{code}",
                                 request=req, response=resp)


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
    first, retry = calls[0]["prompt"], calls[1]["prompt"]
    # просьба о повторе — ВЫШЕ брифа: бриф кончается данными конкурентов, после него нашего текста нет
    assert retry.endswith(first) and retry != first
    note = retry[: -len(first)]
    assert note.startswith("## Повтор\nПредыдущий ответ не прошёл проверку схемы: в ответе нет JSON-объекта")
    assert "ТОЛЬКО" in note and "без текста до и после" in note
    assert "не прошёл проверку схемы" not in calls[2]["prompt"]     # ошибка одной страницы не течёт в следующую
    assert jobs.last("generate")["status"] == "done"


def test_writer_two_failures_creates_no_page_and_warns(monkeypatch):
    site_id = _site()
    calls = _llm(monkeypatch, default=VALID[: len(VALID) // 2])     # обрезанный JSON, каждый раз
    assert content.generate_site(site_id) == 0
    assert _pages(site_id) == [] and len(calls) == 6                # по два вызова на страницу, не больше
    last = jobs.last("generate")
    assert last["status"] == "done_warn"
    assert "написано 0 из 3" in last["message"]
    # одна причина на три страницы — одна запись со списком путей, а не три копии текста ошибки
    assert "/, /vs, /setup — ответ писателя не прошёл схему" in last["message"]
    assert last["message"].count("не прошёл схему") == 1


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


def test_client_failure_stops_the_batch_and_keeps_written_pages(monkeypatch):
    site_id = _site()
    calls = _llm(monkeypatch, VALID, httpx.ReadTimeout("шлюз молчит"))
    assert content.generate_site(site_id) == 1
    assert len(calls) == 2                                          # «/setup» в лежащий шлюз не ходила
    assert [p.url_path for p in _pages(site_id)] == ["/"]
    last = jobs.last("generate")
    assert last["status"] == "done_warn"
    assert "написано 1 из 3" in last["message"] and "модель недоступна — прогон остановлен" in last["message"]
    assert "не начато страниц: 1" in last["message"] and "/vs — писатель не ответил: ReadTimeout" in last["message"]


def test_http_4xx_fails_one_page_and_the_batch_continues(monkeypatch):
    """Отказ 4xx — про ЭТУ страницу (промпт длинен, 422): порядок scaffold() постоянный, и остановка
    пачки на «/» навсегда закрыла бы «/vs» и «/setup»."""
    site_id = _site()
    calls = _llm(monkeypatch, _http_error(400, "prompt is too long: 250000 tokens"))
    assert content.generate_site(site_id) == 2
    assert len(calls) == 3                                          # отказ не повторяем, остальные пишем
    assert [p.url_path for p in _pages(site_id)] == ["/vs", "/setup"]
    last = jobs.last("generate")
    assert last["status"] == "done_warn" and "написано 2 из 3" in last["message"]
    assert "/ — модель отклонила запрос: HTTP 400: prompt is too long: 250000 tokens" in last["message"]
    assert "mozilla" not in last["message"] and "модель недоступна" not in last["message"]


@pytest.mark.parametrize("code", [503, 500, 429, 408])
def test_http_5xx_and_throttling_stop_the_batch(monkeypatch, code):
    site_id = _site()
    calls = _llm(monkeypatch, VALID, _http_error(code, "upstream is down"))
    assert content.generate_site(site_id) == 1 and len(calls) == 2
    last = jobs.last("generate")
    assert last["status"] == "done_warn" and "модель недоступна — прогон остановлен" in last["message"]
    assert f"/vs — писатель не ответил: HTTP {code}: upstream is down" in last["message"]
    assert "mozilla" not in last["message"]


def test_batch_message_fits_registry_limit_by_trimming_reasons():
    """Реестр режет message до 400 символов вслепую. Худший случай — все пометки и длинные причины —
    обязан уложиться сам: под нож идут причины (каждая — с хвоста, с «…»), не счётчики."""
    failed = [("/", "первая причина " + "а" * 600), ("/vs", "вторая причина " + "б" * 600),
              ("/setup", "короткая причина")]
    msg = content._batch_message(0, 3, down=True, not_started=2, hand_edited=3, truncated=True, failed=failed)
    assert len(msg) <= 400
    for part in ("написано 0 из 3", "не начато страниц: 2", "правлены вручную: 3", "правила письма обрезаны",
                 "/ — первая причина ааа", "/vs — вторая причина ббб", "/setup — короткая причина"):
        assert part in msg, part
    assert msg.count("…") == 2                                      # короткая причина цела, место отдано длинным
    assert len(msg) > 380                                           # и место не пропадает зря

    one = content._batch_message(0, 1, down=False, not_started=0, hand_edited=0, truncated=False,
                                 failed=[("/", "в" * 900)])
    assert len(one) == 400 and one.endswith("…")
    short = content._batch_message(2, 3, down=False, not_started=0, hand_edited=0, truncated=False,
                                   failed=[("/", "причина")])
    assert short == "написано 2 из 3; не написаны: / — причина"


def test_reasons_survive_cancel(monkeypatch):
    site_id = _site()
    _llm(monkeypatch, "мусор", "мусор",                             # «/» провалена, на «/vs» жмут «стоп»
         during=lambda n: n == 2 and jobs.request_cancel("generate"))
    assert content.generate_site(site_id) is None                   # отмена — не ошибка, track её гасит
    assert [p.url_path for p in _pages(site_id)] == ["/vs"]
    last = jobs.last("generate")
    assert last["status"] == "cancelled"
    assert "написано 1 из 3" in last["message"] and "/ — ответ писателя не прошёл схему" in last["message"]


def test_reasons_survive_insert_race(monkeypatch):
    site_id = _site()

    def other_run_inserts_vs(n):
        if n == 2:                                                  # пока модель пишет «/vs», её вставил другой прогон
            with db.SessionLocal() as s:
                s.add(Page(site_id=site_id, url_path="/vs", title="чужая", status="draft", body=OLD_BODY))
                s.commit()

    _llm(monkeypatch, "мусор", "мусор", during=other_run_inserts_vs)
    with pytest.raises(ValueError, match="создаёт другой прогон"):
        content.generate_site(site_id)
    last = jobs.last("generate")
    assert last["status"] == "failed" and "создаёт другой прогон" in last["error"]
    assert "/ — ответ писателя не прошёл схему" in last["message"]


def test_truncated_guides_are_reported(monkeypatch):
    from app.services import guides
    seen = []
    monkeypatch.setattr(guides, "load_guides", lambda lang, kind, role=None: seen.append(role) or {
        "text": "ПРАВИЛО", "files": ["a.md"], "truncated": kind == "howto", "pending": []})
    site_id = _site()
    calls = _llm(monkeypatch)
    assert content.generate_site(site_id) == 3
    assert all("ПРАВИЛО" in c["system"] for c in calls) and seen == ["writer"] * 3
    last = jobs.last("generate")
    assert last["status"] == "done" and last["message"] == "написано 3 из 3; правила письма обрезаны по лимиту"


def test_rules_without_digest_are_reported_and_not_sent(monkeypatch, _own_guides):
    """Файл правил без выжимки в задание не идёт (сырым — никогда), и прогон говорит об этом словами."""
    from app.services import guides
    _own_guides.mkdir()
    (_own_guides / "10-тон.md").write_text("ПРАВИЛО-ТОНА", encoding="utf-8")
    (_own_guides / "20-структура.md").write_text("ПРАВИЛО-СТРУКТУРЫ", encoding="utf-8")
    guides.build_digests()
    (_own_guides / "20-структура.md").write_text("ПРАВИЛО-СТРУКТУРЫ изменили", encoding="utf-8")   # выжимка устарела
    (_own_guides / "30-новый.md").write_text("ПРАВИЛО-НОВОЕ", encoding="utf-8")                     # выжимки нет
    site_id = _site()
    calls = _llm(monkeypatch)
    assert content.generate_site(site_id) == 3
    for c in calls:
        assert "ПРАВИЛО-ТОНА" in c["system"]
        assert "ПРАВИЛО-СТРУКТУРЫ" not in c["system"] and "ПРАВИЛО-НОВОЕ" not in c["system"]
    last = jobs.last("generate")
    assert last["status"] == "done"
    assert last["message"] == "написано 3 из 3; правила письма: 2 файла без выжимки — не учтены"


def test_writer_gets_only_the_digests_of_its_role(monkeypatch, _own_guides):
    from app.services import guides
    _own_guides.mkdir()
    for name in ("писателю", "критику", "обоим", "никому"):
        (_own_guides / f"{name}.md").write_text(f"ПРАВИЛО-{name}", encoding="utf-8")
    guides.build_digests()                                         # короткие файлы — дословно, роль both
    guides.set_role("писателю.md", "writer"); guides.set_role("критику.md", "critic")
    guides.set_role("никому.md", "skip")
    site_id = _site()
    calls = _llm(monkeypatch)
    content.generate_site(site_id)
    for c in calls:
        assert "ПРАВИЛО-писателю" in c["system"] and "ПРАВИЛО-обоим" in c["system"]
        assert "ПРАВИЛО-критику" not in c["system"] and "ПРАВИЛО-никому" not in c["system"]
    assert not jobs.last("generate")["message"]                    # ждущих выжимки нет — сказать нечего


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


def test_write_doc_client_failure_is_not_retried(monkeypatch):
    from app.integrations.llm import LlmClient
    calls = _llm(monkeypatch, httpx.ReadTimeout("шлюз молчит"))
    with pytest.raises(content.WriterDown) as e:
        content.write_doc(LlmClient(), system="SYS", prompt="PROMPT")
    assert "ReadTimeout" in str(e.value) and "шлюз молчит" in str(e.value)
    assert len(calls) == 1                                          # второго 600-секундного ожидания нет

    calls = _llm(monkeypatch, "мусор", RuntimeError("шлюз упал"))    # схема, затем сбой клиента на повторе
    with pytest.raises(content.WriterDown, match="шлюз упал"):
        content.write_doc(LlmClient(), system="SYS", prompt="PROMPT")
    assert len(calls) == 2


def test_write_doc_4xx_is_a_page_failure_not_writer_down(monkeypatch):
    (doc, err), calls = _write(monkeypatch, _http_error(422, "context length exceeded"))
    assert doc is None and err == "модель отклонила запрос: HTTP 422: context length exceeded"
    assert len(calls) == 1                                          # тот же запрос даст тот же отказ
    (doc, err), _ = _write(monkeypatch, _http_error(404))           # шлюз без тела ошибки
    assert err == "модель отклонила запрос: HTTP 404"


def test_write_doc_puts_remarks_and_retry_note_above_the_brief(monkeypatch):
    (doc, err), calls = _write(monkeypatch, "мусор", issues=["Нет таблицы", "Число 99 без источника"])
    assert err is None and doc is not None
    remarks = "## Замечания редактора, которые нужно устранить\n- Нет таблицы\n- Число 99 без источника\n\n"
    assert calls[0]["prompt"] == remarks + "PROMPT"
    second = calls[1]["prompt"]
    assert second.startswith(remarks + "## Повтор\nПредыдущий ответ не прошёл проверку схемы")
    assert second.endswith("\n\nPROMPT")                            # после брифа — ничего нашего


def test_write_doc_empty_answer_is_a_failed_attempt(monkeypatch):
    (doc, err), calls = _write(monkeypatch, "", "   ")
    assert doc is None and "пустой ответ" in err
    assert [c["prompt"] for c in calls] == ["PROMPT", "PROMPT"]     # схема не при чём — повтор без добавки


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
    """Правила письма — одна плоская папка (с 2026-10-10): выжимка каждого файла идёт в задание любой
    страницы (короткий файл — дословно)."""
    from app.services import guides
    (_own_guides / "ru").mkdir(parents=True)
    (_own_guides / "10-тон.md").write_text("ПРАВИЛО-ТОНА", encoding="utf-8")
    (_own_guides / "20-структура.txt").write_text("ПРАВИЛО-СТРУКТУРЫ", encoding="utf-8")
    (_own_guides / "ru" / "old.md").write_text("ПРАВИЛО-ИЗ-ПОДПАПКИ", encoding="utf-8")
    assert guides.build_digests()["built"] == 2
    site_id = _site()
    calls = _llm(monkeypatch)
    content.generate_site(site_id)
    assert len(calls) == 3
    for c in calls:
        assert "ПРАВИЛО-ТОНА" in c["system"] and "ПРАВИЛО-СТРУКТУРЫ" in c["system"]
        assert "ПРАВИЛО-ИЗ-ПОДПАПКИ" not in c["system"]                    # подпапок больше нет
        assert "Russian" in c["system"] and "DE" in c["system"]            # язык вывода и рынок названы
        assert "DUREV20" in c["prompt"] and "скидка 20% на первый год" in c["prompt"]
        assert "Durev VPN" in c["prompt"]


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


def test_rewrite_overwrites_manual_edit_only_on_explicit_request(monkeypatch):
    site_id = _site()
    ids = _add_pages(site_id, status="edited")
    content.save_draft(ids["/vs"], "<p>Оператор переписал сравнение руками — страница без blocks.</p>")
    calls = _llm(monkeypatch)
    assert content.generate_site(site_id, rewrite=True) == 2            # по умолчанию правка цела
    vs = {p.url_path: p for p in _pages(site_id)}["/vs"]
    assert "руками" in vs.body and vs.blocks_stale is True and len(calls) == 2

    # второй прогон — другой текст: вернувшийся знак в знак прежним переписыванием не считается
    calls = _llm(monkeypatch, default=json.dumps({**DOC, "pros": ["Цена 5.99 в месяц", "Другая редакция"]},
                                                 ensure_ascii=False))
    assert content.generate_site(site_id, rewrite=True, overwrite_manual=True) == 3 and len(calls) == 3
    vs = {p.url_path: p for p in _pages(site_id)}["/vs"]
    assert "<h2>Скорость</h2>" in vs.body and vs.blocks_stale is False and vs.id == ids["/vs"]
    assert jobs.last("generate")["message"] == ""


def test_overwrite_manual_still_spares_edit_made_while_model_wrote(monkeypatch):
    site_id = _site()
    ids = _add_pages(site_id, status="draft")
    edited = "<p>Правка, сохранённая посреди прогона: о ней оператор, ставя галочку, не знал.</p>"
    _llm(monkeypatch, during=lambda n: n == 0 and content.save_draft(ids["/"], edited))
    assert content.generate_site(site_id, rewrite=True, overwrite_manual=True) == 2
    assert _pages(site_id)[0].body == edited


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
    прогона, не затирается."""
    site_id = _site()
    ids = _add_pages(site_id, status="draft")
    edited = "<p>Оператор переписал главную руками, пока модель думала над ней.</p>"
    _llm(monkeypatch, during=lambda n: n == 0 and content.save_draft(ids["/"], edited))
    assert content.generate_site(site_id, rewrite=True) == 2
    home = {p.url_path: p for p in _pages(site_id)}["/"]
    assert home.body == edited and home.blocks is None and home.blocks_stale is True
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
    assert prompt.startswith("## Замечания редактора, которые нужно устранить\n"
                             "- Нет таблицы сравнения\n- Число 99 без источника\n\n")     # выше брифа
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

    out = content.rewrite_page(stale_id, ["x"], overwrite_manual=True)      # явное решение оператора
    assert out == {"page_id": stale_id, "ok": True, "error": None} and len(calls) == 1
    with db.SessionLocal() as s:
        p = s.get(Page, stale_id)
        assert "<h2>Скорость</h2>" in p.body and p.blocks_stale is False


def test_rewrite_page_failure_keeps_page_and_returns_reason(monkeypatch):
    site_id = _site()
    ids = _add_pages(site_id, status="edited", critic_score=0.4)
    _llm(monkeypatch, default="не JSON")
    out = content.rewrite_page(ids["/"], ["x"])
    assert out["ok"] is False and "не прошёл схему" in out["error"]
    home = _pages(site_id)[0]
    assert home.body == OLD_BODY and home.status == "edited" and home.critic_score == 0.4

    _llm(monkeypatch, httpx.ConnectError("шлюз лежит"))             # сбой клиента — тот же ответ, не исключение
    out = content.rewrite_page(ids["/"], ["x"])
    assert out["ok"] is False and "писатель не ответил: ConnectError" in out["error"]
    assert _pages(site_id)[0].body == OLD_BODY


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


def test_manual_edit_marks_legacy_page_too(monkeypatch):
    """Флаг значит «тело правили руками», а не «рендер блоков устарел»: у страниц старого пути blocks
    нет, но правка оператора не должна пропасть при следующем переписывании."""
    ids = _add_pages(_site(dossier=False), status="draft")
    content.save_draft(ids["/"], OLD_BODY)                      # сохранили без изменений — не правка
    content.mark_edited(ids["/vs"], OLD_BODY)
    content.save_draft(ids["/setup"], "<p>Правка страницы старого пути: блоков у неё нет и не было.</p>")
    with db.SessionLocal() as s:
        assert s.get(Page, ids["/"]).blocks_stale is False and s.get(Page, ids["/vs"]).blocks_stale is False
        assert s.get(Page, ids["/setup"]).blocks_stale is True
    content.mark_edited(ids["/"], OLD_BODY + "<p>Редактор дописал абзац при одобрении.</p>")
    with db.SessionLocal() as s:
        assert s.get(Page, ids["/"]).blocks_stale is True


def test_mark_edited_without_body_keeps_blocks_fresh(monkeypatch):
    p = _written_page(monkeypatch)
    assert content.mark_edited(p.id)["status"] == "edited"
    assert _pages(p.site_id)[0].blocks_stale is False
    content.mark_edited(p.id, p.body)                           # одобрили как лежит, через форму
    assert _pages(p.site_id)[0].blocks_stale is False
    content.mark_edited(p.id, p.body + "<p>Редактор дописал абзац.</p>")
    got = _pages(p.site_id)[0]
    assert got.blocks_stale is True and got.status == "edited"


# --- переписывание под оффер САЙТА ---

def _old_offer_pages(site_id: int, paths=PATHS, **over) -> tuple[int, dict]:
    """Страницы сайта, написанные под ПРЕЖНИЙ оффер (TestVPN); сам сайт привязан к Durev VPN.
    -> (id прежнего оффера, {путь: id})."""
    with db.SessionLocal() as s:
        old = Offer(brand="TestVPN", affiliate_link="https://testvpn.example/aff", language="ru", active=True)
        s.add(old); s.commit()
        rows = [Page(**{**dict(site_id=site_id, url_path=path, title="Старый заголовок", status="published",
                               body=OLD_BODY, lang="ru", offer_id=old.id), **over}) for path in paths]
        s.add_all(rows); s.commit()
        return old.id, {p.url_path: p.id for p in rows}


def test_rewrite_writes_under_the_site_offer_not_the_old_pages_offer(monkeypatch):
    """Сайт перепривязали к другому офферу, досье собрано под него. «Переписать тексты» пишет про оффер
    сайта и записывает его в строку: иначе текст про прежний бренд, критик сверяет бренд по Page.offer_id и
    одобряет, а публикация ставит ссылку прежнего оффера."""
    from app.services.vertical_data import vertical_block
    site_id = _site()
    old_id, ids = _old_offer_pages(site_id)
    with db.SessionLocal() as s:
        site_offer_id = s.get(Site, site_id).offer_id
    assert site_offer_id != old_id
    calls = _llm(monkeypatch)
    assert content.generate_site(site_id, rewrite=True) == 3
    fact = vertical_block("Durev VPN").splitlines()[1]              # первая строка фактов бренда сайта
    for c in calls:
        assert "бренд — Durev VPN" in c["prompt"] and "TestVPN" not in c["prompt"]
        assert fact in c["prompt"] and "скидка 20% на первый год" in c["prompt"]
    assert {p.offer_id for p in _pages(site_id)} == {site_offer_id}
    # круг критика пишет под оффер, записанный в странице, — теперь это оффер сайта
    calls = _llm(monkeypatch, default=json.dumps({**DOC, "pros": ["Другая редакция"]}, ensure_ascii=False))
    assert content.rewrite_page(ids["/"], ["x"])["ok"] is True
    assert "бренд — Durev VPN" in calls[0]["prompt"] and _pages(site_id)[0].offer_id == site_offer_id


def test_rewrite_skipped_pages_keep_their_offer(monkeypatch):
    """Страницу, которую переписывание не тронуло (правлена руками), не перепривязываем: её текст — про прежний оффер."""
    site_id = _site()
    old_id, ids = _old_offer_pages(site_id)
    with db.SessionLocal() as s:
        s.get(Page, ids["/vs"]).blocks_stale = True
        s.commit()
    _llm(monkeypatch)
    assert content.generate_site(site_id, rewrite=True) == 2
    by_path = {p.url_path: p for p in _pages(site_id)}
    assert by_path["/vs"].offer_id == old_id and by_path["/vs"].body == OLD_BODY
    assert by_path["/"].offer_id != old_id and by_path["/setup"].offer_id != old_id


def test_backfill_without_rewrite_still_inherits_the_pages_offer(monkeypatch):
    """Дозаполнение недостающих страниц (без rewrite) — под тот же оффер и язык, что уже написанные."""
    site_id = _site()
    old_id, ids = _old_offer_pages(site_id, paths=("/",))
    calls = _llm(monkeypatch)
    assert content.generate_site(site_id) == 2
    assert all("бренд — TestVPN" in c["prompt"] for c in calls)
    assert {p.offer_id for p in _pages(site_id)} == {old_id}
    assert _pages(site_id)[0].body == OLD_BODY


def test_rewrite_without_site_offer_falls_back_to_the_pages_offer(monkeypatch):
    site_id = _site()
    old_id, ids = _old_offer_pages(site_id)
    with db.SessionLocal() as s:
        s.get(Site, site_id).offer_id = None
        s.commit()
    calls = _llm(monkeypatch)
    assert content.generate_site(site_id, rewrite=True) == 3
    assert all("бренд — TestVPN" in c["prompt"] for c in calls)
    assert {p.offer_id for p in _pages(site_id)} == {old_id}


# --- тот же текст — не переписывание ---

def test_rewrite_that_returns_the_same_text_changes_nothing(monkeypatch):
    """Писатель вернул знак в знак прежний текст: строка, её статус и заметки критика не тронуты (иначе
    «переписывание» стирало бы отказ критика, ничего не изменив)."""
    site_id = _site()
    _llm(monkeypatch)
    assert content.generate_site(site_id) == 3
    stamp = datetime.now(timezone.utc)
    notes = {"pass": False, "issues": ["вода"], "refused_fp": "0123456789abcdef"}
    with db.SessionLocal() as s:
        for p in s.query(Page).filter(Page.site_id == site_id):
            p.status, p.critic_notes, p.critic_score, p.critic_checked_at = "edited", notes, 0.3, stamp
        s.commit()
    before = [(p.title, p.body, p.blocks) for p in _pages(site_id)]

    calls = _llm(monkeypatch)                                       # тот же DOC, что и в первый раз
    assert content.generate_site(site_id, rewrite=True) == 0
    assert len(calls) == 3
    for p, old in zip(_pages(site_id), before):
        assert (p.title, p.body, p.blocks) == old and p.status == "edited"
        assert p.critic_notes == notes and p.critic_score == 0.3 and p.critic_checked_at is not None
    last = jobs.last("generate")
    assert last["status"] == "done_warn" and "написано 0 из 3" in last["message"]
    assert "/, /vs, /setup — писатель вернул прежний текст" in last["message"]

    pid = _pages(site_id)[0].id
    out = content.rewrite_page(pid, ["x"])
    assert out == {"page_id": pid, "ok": False, "error": "писатель вернул прежний текст"}
    assert _pages(site_id)[0].critic_notes == notes and _pages(site_id)[0].status == "edited"
    # другой текст — настоящее переписывание
    _llm(monkeypatch, default=json.dumps({**DOC, "pros": ["Другая редакция"]}, ensure_ascii=False))
    assert content.rewrite_page(pid, ["x"])["ok"] is True
    assert _pages(site_id)[0].critic_notes is None and _pages(site_id)[0].status == "draft"


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
