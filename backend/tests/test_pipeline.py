"""End-to-end pipeline logic on SQLite with mocked integrations.

Proves the two HARD GATES from PLAN §2 hold in the real code paths:
  1. Editorial gate — publish deploys ONLY 'edited' pages, never 'draft'.
  2. Purchase is a human action (status flips only via the explicit endpoint).
Plus discovery idempotency and scoring persistence (JSONB round-trip on SQLite).
"""
import time

import app.db as db
from app.models.domain import Domain


def _add(obj):
    with db.SessionLocal() as s:
        s.add(obj)
        s.commit()
        s.refresh(obj)
        return obj.id


def test_discovery_upsert_idempotent(monkeypatch):
    from app.services import discovery
    from app.services.settings import update_settings
    # мультиисточник (Task 4): офлайн-тест бьёт только backorder, остальные — реальные
    # HTTP/A-Parser клиенты, их нужно выключить, иначе _collect уйдёт в сеть.
    update_settings(sources_enabled={"backorder": True, "cctld": False, "reg_ru": False, "sweb": False})
    rows = [
        {"domainname": "Clean-Drop.com", "links": "12"},
        {"domainname": "second.ru", "links": 3},
        {"domainname": "bad_underscore.ru", "links": 5},  # junk char -> skipped
    ]
    monkeypatch.setattr("app.integrations.backorder.BackorderClient.list_dropping",
                        lambda self, min_links=1: rows)
    assert discovery.run_discovery() == 2   # 2 valid, 1 junk dropped
    assert discovery.run_discovery() == 0   # re-run inserts nothing (idempotent)


def _funnel_clients(whois_dt, rkn=False, wb_flags=None):
    """Мок-клиенты в форме, которую ждёт scoring._funnel (см. test_funnel.py::_clients).
    _gather_signals больше нет — воронка теперь ступенчатая, поэтому мокаем клиенты, а
    не внутреннюю функцию сбора сигналов. whois_probe отдаёт «занят, но с датой» —
    домен-заглушка получает lane="bid" (см. вызовы ниже), чтобы приобретаемость
    (Task 4) не блокировала гейт T1 до RKN/Wayback."""
    class _W:  # aparser
        def whois_probe(self, dom): return {"available": False, "created": whois_dt}
    class _R:
        def is_listed(self, dom): return rkn
    class _Bl:
        def is_blacklisted(self, dom): return False
    class _S:
        def indexed_echo(self, dom): return True
    class _Wb:
        def classify_history(self, dom):
            return wb_flags or {"prior_flags": {}, "wayback_checked": True,
                                "first_seen": None, "age_years": 10.0}
    return {"aparser": _W(), "rkn": _R(), "blacklist": _Bl(), "searxng": _S(),
            "wayback": _Wb(), "opr": None}


def test_scoring_persists_and_jsonb_roundtrips():
    from app.services import scoring
    did = _add(Domain(domain="oldclean.com", source="backorder",
                      referring_domains=30, status="discovered", lane="bid"))
    from datetime import datetime, timezone, timedelta
    old = datetime.now(timezone.utc) - timedelta(days=365 * 10)   # старше min_age_years -> проходит T1
    out = scoring.score_domain(did, clients=_funnel_clients(old))
    assert out["status"] in ("approved", "scored")
    with db.SessionLocal() as s:
        d = s.get(Domain, did)
        assert d.score is not None and d.status == out["status"]
        assert isinstance(d.score_breakdown, dict)      # JSONB dict survived the round-trip
        assert d.prior_flags == {}


def test_scoring_hard_reject_on_rkn():
    from app.services import scoring
    did = _add(Domain(domain="blocked.ru", source="backorder", status="discovered", lane="bid"))
    # whois=None -> T1 пропущен без возраста; RKN=True рубит на T2, Wayback не вызывается
    out = scoring.score_domain(did, clients=_funnel_clients(None, rkn=True))
    assert out["status"] == "rejected" and out["score"] == 0.0
    assert out["reject_reason"] == "rkn"
    with db.SessionLocal() as s:
        assert s.get(Domain, did).clean is False


def test_panel_actions(client, monkeypatch):
    did = _add(Domain(domain="curate-me.ru", source="backorder", status="scored"))
    # manual curation: valid transition sticks (303 -> redirect back to /)
    r = client.post(f"/domains/{did}/set-status", data={"status": "approved"},
                    follow_redirects=False)
    assert r.status_code == 303
    with db.SessionLocal() as s:
        assert s.get(Domain, did).status == "approved"
    # guard: a status outside the manual allowlist is ignored (no arbitrary transitions)
    client.post(f"/domains/{did}/set-status", data={"status": "live"}, follow_redirects=False)
    with db.SessionLocal() as s:
        assert s.get(Domain, did).status == "approved"   # unchanged
    # Score button starts a BACKGROUND job now (Task 6) — route returns 303 immediately with
    # a "запущен" flash; the actual scoring runs on jobs' worker thread. Mock the service (no
    # live Wayback in the test) and wait for the job to finish before asserting it ran with n=7.
    from app.services import jobs
    jobs._reset()                     # изоляция: реестр глобальный, чистим от соседних тестов
    called = {}
    monkeypatch.setattr("app.services.scoring.score_pending",
                        lambda limit=5, on_progress=None: called.setdefault("n", limit))
    r = client.post("/run/score", data={"n": "7"}, follow_redirects=False)
    assert r.status_code == 303 and "msg=" in r.headers["location"]
    for _ in range(50):
        if not jobs.is_running("score"):
            break
        time.sleep(0.02)
    assert called["n"] == 7


def test_run_score_double_start_and_progress_route(client, monkeypatch):
    """Роут-уровень Task 6: второй POST /run/score при живом джобе -> err-флэш «уже идёт»;
    GET /run/score/progress отдаёт форму реестра; неизвестный джоб -> 404.
    Детерминизм: воркер держится на threading.Event, не на sleep."""
    import threading
    from urllib.parse import unquote
    from app.services import jobs
    jobs._reset()
    gate = threading.Event()
    monkeypatch.setattr("app.services.scoring.score_pending",
                        lambda limit=5, on_progress=None: gate.wait(5))  # 5с — предохранитель
    r1 = client.post("/run/score", data={"n": "3"}, follow_redirects=False)
    assert r1.status_code == 303 and "msg=" in r1.headers["location"]
    # джоб гарантированно жив (гейт не отпущен) -> двойной старт отклонён с err-флэшем
    r2 = client.post("/run/score", data={"n": "3"}, follow_redirects=False)
    assert r2.status_code == 303 and "Score уже идёт" in unquote(r2.headers["location"])
    p = client.get("/run/score/progress").json()
    assert set(p) == {"running", "done", "total", "current", "message", "error"}
    assert p["running"] is True and p["error"] is None
    gate.set()                                             # отпускаем воркер
    for _ in range(50):
        if not jobs.is_running("score"):
            break
        time.sleep(0.02)
    assert client.get("/run/score/progress").json()["running"] is False
    assert client.get("/run/nope/progress").status_code == 404   # только известные джобы


def test_edit_gate_and_publish(client, monkeypatch):
    # offer (the machine's input); JSON API lives under /api
    offer_id = client.post("/api/offers", json={
        "brand": "NordVPN", "affiliate_link": "https://ex.com/aff", "promo_code": "SAVE10"
    }).json()["id"]

    # domain -> HUMAN purchase -> site
    did = _add(Domain(domain="review-site.com", source="backorder", status="approved"))
    assert client.post(f"/api/domains/{did}/purchase").json()["status"] == "purchased"
    site_id = client.post(f"/api/domains/{did}/site").json()["site_id"]

    # M4 generate (mock LiteLLM) -> 3 DRAFT pages
    monkeypatch.setattr("app.integrations.llm.LlmClient.complete",
                        lambda self, system, prompt, **kw: "<h2>Draft</h2><p>text</p>")
    assert client.post(f"/api/sites/{site_id}/generate").json()["created"] == 3

    # GATE 1: nothing is 'edited' yet -> publish refuses (no auto-publish of AI drafts)
    assert client.post(f"/api/sites/{site_id}/publish").json()["status"] == "no_edited_pages"

    # human edits exactly ONE page (the '/' review)
    pages = client.get(f"/api/sites/{site_id}/pages").json()
    home = next(p for p in pages if p["url_path"] == "/")
    assert client.post(f"/api/pages/{home['id']}/edit",
                       json={"body": "<h2>Edited</h2><script>alert('xss')</script>"}
                       ).json()["status"] == "edited"

    # attach offer + publish (mock the aaPanel file write)
    client.post(f"/api/sites/{site_id}/offer", json={"offer_id": offer_id})
    # aaPanel client fails closed for non-loopback URLs w/o a CA bundle — use loopback in the test.
    # CA_BUNDLE тоже зануляем: в локальном .env может лежать контейнерный путь (/app/aapanel.pem),
    # которого нет на этой машине, — тест не должен зависеть от .env.
    from app.config import settings
    monkeypatch.setattr(settings, "AAPANEL_URL", "https://127.0.0.1:8888")
    monkeypatch.setattr(settings, "AAPANEL_CA_BUNDLE", "")
    writes = []
    monkeypatch.setattr("app.integrations.aapanel.AaPanelClient.write_file",
                        lambda self, path, content: (writes.append((path, content)), {"status": True})[1])
    pub = client.post(f"/api/sites/{site_id}/publish").json()

    # only the edited page went out — the 2 drafts were left untouched
    assert pub["status"] == "published" and pub["pages"] == ["/"]
    assert len(writes) == 1
    path, page_html = writes[0]
    assert path.endswith("/index.html")
    assert "SAVE10" in page_html and 'rel="sponsored nofollow"' in page_html and "Раскрытие" in page_html
    assert "<script" not in page_html.lower() and "xss" not in page_html   # sanitized on edit
    states = sorted(p["status"] for p in client.get(f"/api/sites/{site_id}/pages").json())
    assert states == ["draft", "draft", "published"]

    # M5 index check (mock SearXNG -> no hits)
    monkeypatch.setattr("app.integrations.searxng.SearxngClient.search",
                        lambda self, q, **kw: [])
    assert client.post(f"/api/sites/{site_id}/check-index").json()["pages"]["/"] == "not_indexed"


def test_acquisition_queue_and_gate():
    """M2: очередь + денежный гейт — execute отказывает без подтверждения человеком."""
    from app.services import acquisition
    from app.models.domain import Domain, AcquisitionOrder
    did = _add(Domain(domain="buy-me.ru", source="backorder", status="approved"))
    oid = acquisition.create_order(did, "backorder")
    with db.SessionLocal() as s:
        o = s.get(AcquisitionOrder, oid)
        assert o.status == "pending_confirm" and o.confirmed_by_human is False
        assert s.get(Domain, did).status == "purchasing"          # видно в воронке
    assert acquisition.create_order(did, "backorder") == oid       # идемпотентно, без дублей
    # ГЕЙТ: execute до подтверждения отказывает, статус не меняется
    r = acquisition.execute_confirmed_order(oid)
    assert "gate" in (r.get("error") or "")
    with db.SessionLocal() as s:
        assert s.get(AcquisitionOrder, oid).status == "pending_confirm"
    # человек подтверждает -> гейт открыт
    acquisition.confirm_order(oid)
    with db.SessionLocal() as s:
        assert s.get(AcquisitionOrder, oid).confirmed_by_human is True
    # execute идёт к провайдеру; транспорт не готов -> честный failed (не ложный успех)
    r = acquisition.execute_confirmed_order(oid)
    assert r["status"] == "failed" and "implement" in (r.get("error") or "").lower()


def test_queue_panel_actions(client):
    """Экран /queue и действия: add из панели -> рендер -> гейт на execute -> confirm."""
    from sqlalchemy import select
    from app.models.domain import Domain, AcquisitionOrder
    did = _add(Domain(domain="q-panel.ru", source="backorder", status="approved"))
    assert client.post(f"/domains/{did}/queue", data={"provider": "backorder"},
                       follow_redirects=False).status_code == 303
    with db.SessionLocal() as s:
        oid = s.execute(select(AcquisitionOrder.id)).scalar_one()
    r = client.get("/queue")
    assert r.status_code == 200 and "q-panel.ru" in r.text and "подтвердить выкуп" in r.text
    # execute до подтверждения -> err-flash (гейт)
    r = client.post(f"/queue/{oid}/execute", follow_redirects=False)
    assert r.status_code == 303 and "err=" in r.headers["location"]
    # confirm -> execute (провайдер не готов, но 303 без 500)
    client.post(f"/queue/{oid}/confirm", follow_redirects=False)
    assert client.post(f"/queue/{oid}/execute", follow_redirects=False).status_code == 303


def test_panel_basic_auth(client, monkeypatch):
    """Basic-auth: выкл по умолчанию; включённый — 401 без кредов, 200 с верными, /health открыт."""
    from app.config import settings
    assert client.get("/health").status_code == 200          # auth off -> открыто
    monkeypatch.setattr(settings, "PANEL_USER", "op")
    monkeypatch.setattr(settings, "PANEL_PASS", "s3cret")
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 401 and "Basic" in r.headers.get("www-authenticate", "")
    assert client.get("/health").status_code == 200          # /health всегда открыт (мониторинг)
    assert client.get("/", auth=("op", "s3cret")).status_code == 200   # верные креды
    assert client.get("/", auth=("op", "wrong")).status_code == 401    # неверный пароль
    assert client.get("/", auth=("nope", "s3cret")).status_code == 401 # неверный логин


def test_panel_screens_render(client, monkeypatch):
    """Каждый HTML-экран панели отвечает 200 и содержит свои ключевые элементы."""
    # data: domain in every interesting status + site + page
    for i, st in enumerate(["discovered", "scored", "approved", "purchased"]):
        _add(Domain(domain=f"screen-{st}.ru", source="backorder", status=st,
                    referring_domains=10 + i))
    offer_id = client.post("/api/offers", json={
        "brand": "TestVPN", "affiliate_link": "https://ex.com/a", "promo_code": "T10"}).json()["id"]
    with db.SessionLocal() as s:
        from sqlalchemy import select
        did = s.execute(select(Domain.id).where(Domain.status == "purchased")).scalar_one()
    site_id = client.post(f"/api/domains/{did}/site").json()["site_id"]
    monkeypatch.setattr("app.integrations.llm.LlmClient.complete",
                        lambda self, system, prompt, **kw: "<h2>D</h2>")
    client.post(f"/api/sites/{site_id}/generate")

    r = client.get("/")                      # пульт: воронка + шаги + сайты
    assert r.status_code == 200
    assert "Воронка" in r.text and "Что дальше" in r.text and "screen-purchased.ru" in r.text

    r = client.get("/domains")               # M1: тулбар + контекстные действия
    assert r.status_code == 200
    assert "Поиск дропов" in r.text and "set-status" in r.text and "make-site" not in r.text
    r = client.get("/domains?status=purchased")
    assert f"/sites/{site_id}" in r.text     # purchased с сайтом -> ссылка на карточку

    r = client.get("/offers")
    assert r.status_code == 200 and "TestVPN" in r.text and "offers/create" in r.text

    r = client.get(f"/sites/{site_id}")      # карточка: чеклист этапов
    assert r.status_code == 200
    assert "Provision" in r.text and "Редактура" in r.text and "publish" in r.text

    with db.SessionLocal() as s:
        from app.models.site import Page
        from sqlalchemy import select
        pid = s.execute(select(Page.id).limit(1)).scalar_one()
    r = client.get(f"/pages/{pid}")          # редактор
    assert r.status_code == 200 and "EDITED" in r.text

    # form-действия панели: сохранение страницы через гейт + привязка оффера
    r = client.post(f"/pages/{pid}/save", data={"body": "<h2>ок</h2><script>x</script>"},
                    follow_redirects=False)
    assert r.status_code == 303
    with db.SessionLocal() as s:
        from app.models.site import Page
        p = s.get(Page, pid)
        assert p.status == "edited" and "<script" not in p.body
    assert client.post(f"/sites/{site_id}/attach-offer", data={"offer_id": offer_id},
                       follow_redirects=False).status_code == 303
