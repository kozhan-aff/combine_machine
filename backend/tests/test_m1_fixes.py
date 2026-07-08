"""Регрессии на подтверждённые баги M1 (см. брифинг ревью). Оффлайн, без сети.

C1 spam-история -> hard-reject; C2 Wayback не «проверено» без реально скачанных снапшотов;
I1 ошибка RKN/blacklist не даёт auto-approve; I2 DNS_RESOLVER + sentinel-raise;
I3 обрезанный дамп РКН не кэшируется молча; I4 гонка discovery не теряет батч;
M-2 retry не ретраит 4xx и отдаёт исходное исключение.
"""
import socket
import time as _time

import httpx
import pytest


# ---------- C1: spam в истории — hard reject ----------

def test_spam_history_hard_rejected():
    from app.services.scoring import compute_score
    out = compute_score({"wayback_checked": True, "prior_flags": {"spam": True},
                         "age_years": 12, "referring_domains": 500, "indexed_echo": True})
    assert out["status"] == "rejected" and out["score"] == 0.0
    assert "prior_spam" in out["breakdown"]["hard_reject"]


# ---------- C2: Wayback «проверено» только при успешном фетче ----------

def test_wayback_unchecked_when_no_snapshots(monkeypatch):
    from app.integrations.wayback import WaybackClient
    w = WaybackClient()
    monkeypatch.setattr(w, "get_snapshots", lambda domain, **k: [])
    out = w.classify_history("nosnap.com", polite=0.0)
    assert out["wayback_checked"] is False and out["sampled"] == 0


def test_wayback_unchecked_when_all_fetches_fail(monkeypatch):
    from app.integrations.wayback import WaybackClient
    w = WaybackClient()
    snaps = [{"timestamp": "20180101000000", "original": "http://x.com/"},
             {"timestamp": "20200101000000", "original": "http://x.com/"}]
    monkeypatch.setattr(w, "get_snapshots", lambda domain, **k: snaps)

    def boom(ts, orig):
        raise RuntimeError("archive.org throttled")
    monkeypatch.setattr(w, "_fetch_raw", boom)

    out = w.classify_history("throttled.com", polite=0.0)
    assert out["wayback_checked"] is False and out["sampled"] == 0
    assert out["age_years"] is not None   # возраст из CDX есть, но история НЕ проверена


def test_wayback_checked_when_a_fetch_succeeds(monkeypatch):
    from app.integrations.wayback import WaybackClient
    w = WaybackClient()
    snaps = [{"timestamp": "20180101000000", "original": "http://x.com/"},
             {"timestamp": "20200101000000", "original": "http://x.com/"}]
    monkeypatch.setattr(w, "get_snapshots", lambda domain, **k: snaps)
    monkeypatch.setattr(w, "_fetch_raw", lambda ts, orig: "clean vpn review, fast servers")
    # sample=2: покрытие считается от запрошенного sample (Task 5, I4) — с дефолтным
    # sample=5 при 2 реальных снапшотах порог покрытия недостижим даже при 100% успехе.
    out = w.classify_history("ok.com", sample=2, polite=0.0)
    assert out["wayback_checked"] is True and out["sampled"] >= 1
    assert out["prior_flags"]["spam"] is False


def test_wayback_partial_coverage_not_checked(monkeypatch):
    # 5 снапшотов в CDX, но скачался лишь 1 (остальные 429) -> НЕ «проверено»
    from app.integrations import wayback
    c = wayback.WaybackClient()
    monkeypatch.setattr(c, "get_snapshots", lambda dom, limit=400: [
        {"timestamp": "20150101000000", "original": f"http://x/{i}"} for i in range(5)])
    calls = {"n": 0}
    def _fetch(ts, orig):
        calls["n"] += 1
        if calls["n"] == 1:
            return "clean vpn review"
        raise RuntimeError("429")
    monkeypatch.setattr(c, "_fetch_raw", _fetch)
    h = c.classify_history("x.ru", sample=5, polite=0)
    assert h["sampled"] == 1 and h["wayback_checked"] is False


def test_wayback_ru_casino_brands():
    from app.integrations.wayback import _classify_text
    assert "casino" in _classify_text("Вулкан казино играть онлайн")   # бренд + слово
    assert "casino" in _classify_text("Azino777 и joycasino бонусы")


def test_wayback_brands_no_false_positive():
    from app.integrations.wayback import _classify_text
    # геология/туризм/фэшн — НЕ казино
    assert _classify_text("Извержение вулкана мощное, вулкан дымился неделю") == set()
    assert _classify_text("Фотосессия в стиле пинап, пин ап платья и винтаж") == set()
    # но однозначные бренды — ловятся
    assert "casino" in _classify_text("вулкан казино играть, азино777 бонус")


# ---------- I1: ошибка RKN/blacklist не даёт auto-approve ----------

def test_rkn_or_blacklist_error_caps_at_scored():
    from app.services.scoring import compute_score
    strong = {"wayback_checked": True, "prior_flags": {}, "age_years": 8,
              "referring_domains": 3000, "indexed_echo": True}
    # чистый прогон без ошибок -> approved (базовая линия)
    assert compute_score({**strong, "rkn_listed": False, "blacklisted": False,
                          "errors": []})["status"] == "approved"
    # проверка RKN упала (ключ сигнала отсутствует, ошибка в errors) -> не выше scored
    assert compute_score({**strong, "errors": ["rkn:ConnectError"]})["status"] == "scored"
    # проверка blacklist упала -> тоже scored
    assert compute_score({**strong, "errors": ["blacklist:RuntimeError"]})["status"] == "scored"


# ---------- I2: DNS_RESOLVER + sentinel заблокированного резолвера ----------

def test_blacklist_sentinel_raises(monkeypatch):
    from app.config import settings
    from app.integrations.blacklist import BlacklistClient
    monkeypatch.setattr(BlacklistClient, "_control_ok", None)  # без этого исход зависит от порядка тестов
    monkeypatch.setattr(settings, "DNS_RESOLVER", "")   # системный путь
    monkeypatch.setattr(socket, "gethostbyname", lambda host: "127.255.255.254")
    with pytest.raises(RuntimeError):
        BlacklistClient().is_blacklisted("whatever.com")


def test_blacklist_listed_and_clean(monkeypatch):
    from app.config import settings
    from app.integrations.blacklist import BlacklistClient
    monkeypatch.setattr(BlacklistClient, "_control_ok", None)
    monkeypatch.setattr(settings, "DNS_RESOLVER", "")
    monkeypatch.setattr(socket, "gethostbyname", lambda host: "127.0.1.2")
    assert BlacklistClient().is_blacklisted("spammy.com") is True

    def nx(host):
        raise socket.gaierror(socket.EAI_NONAME, "nxdomain")  # реальный NXDOMAIN несёт errno
    monkeypatch.setattr(socket, "gethostbyname", nx)
    assert BlacklistClient().is_blacklisted("clean.com") is False


def test_blacklist_uses_custom_resolver(monkeypatch):
    from app.config import settings
    from app.integrations.blacklist import BlacklistClient
    import dns.resolver
    monkeypatch.setattr(BlacklistClient, "_control_ok", None)
    monkeypatch.setattr(settings, "DNS_RESOLVER", "9.9.9.9")
    used = {}

    class _Rec:
        address = "127.0.0.2"

    class _Answer:
        def __getitem__(self, i):
            return _Rec()

    class FakeResolver:
        def __init__(self, configure=True):
            self.nameservers = []

        def resolve(self, host, rtype):
            used["ns"] = list(self.nameservers)
            return _Answer()

    monkeypatch.setattr(dns.resolver, "Resolver", FakeResolver)
    assert BlacklistClient().is_blacklisted("x.com") is True
    assert used["ns"] == ["9.9.9.9"]   # запрос ушёл через заданный резолвер


# ---------- I3: обрезанный дамп РКН не кэшируется молча ----------

class _FakeResp:
    def __init__(self, text):
        self.text = text


def test_rkn_small_dump_raises_when_no_cache(monkeypatch):
    from app.integrations.rkn import RknClient
    monkeypatch.setattr(RknClient, "_loaded_at", None)
    monkeypatch.setattr(RknClient, "_blocked", set())
    c = RknClient()
    monkeypatch.setattr(c, "request", lambda *a, **k: _FakeResp("a.ru\nb.ru\n"))  # 2 строки
    with pytest.raises(RuntimeError):
        c.is_listed("test.ru")


def test_rkn_small_dump_keeps_old_cache(monkeypatch):
    from app.integrations.rkn import RknClient
    monkeypatch.setattr(RknClient, "_blocked", {"old-blocked.ru"})
    monkeypatch.setattr(RknClient, "_loaded_at", _time.monotonic() - 10 ** 9)  # устарел, но не None
    c = RknClient()
    monkeypatch.setattr(c, "request", lambda *a, **k: _FakeResp("only.ru\n"))   # мал -> не применять
    assert c.is_listed("old-blocked.ru") is True    # прежний валидный кэш сохранён
    assert c.is_listed("only.ru") is False          # обрезанный дамп НЕ затёр кэш


# ---------- I4: гонка двух discovery не теряет батч ----------

def test_discovery_survives_insert_race(monkeypatch):
    from sqlalchemy import select
    from sqlalchemy.orm import Session
    from app.services import discovery
    from app.services.settings import update_settings
    import app.db as db
    from app.models.domain import Domain

    # мультиисточник (Task 4): офлайн-тест бьёт только backorder — остальные источники
    # выключаем, иначе _collect уйдёт в реальную сеть (cctld/reg.ru/sweb через A-Parser).
    # Прогреваем settings ДО патча Session.execute, чтобы get_settings() внутри
    # run_discovery() не занял "первый" перехваченный вызов случайной строкой настроек.
    update_settings(sources_enabled={"backorder": True, "cctld": False, "reg_ru": False, "sweb": False})

    # как будто параллельный запуск уже вставил race.ru (до нашего COMMIT)
    with db.SessionLocal() as s:
        s.add(Domain(domain="race.ru", source="backorder", referring_domains=1))
        s.commit()

    rows = [{"domainname": "race.ru", "links": "5"},
            {"domainname": "fresh.ru", "links": "7"}]
    monkeypatch.setattr("app.integrations.backorder.BackorderClient.list_dropping",
                        lambda self, min_links=1: rows)

    # ПЕРВЫЙ SELECT именно по domains (existing) отдаём пустым (устаревшее чтение) ->
    # код попробует вставить дубль race.ru -> IntegrityError; остальные (включая
    # get_settings() внутри run_discovery и повторное чтение existing) — настоящие.
    real_execute = Session.execute
    state = {"fired": False}

    class _EmptyResult:
        def scalars(self):
            return self

        def all(self):
            return []

    def flaky_execute(self, statement, *a, **k):
        is_domains_select = "FROM domains" in str(statement)
        if is_domains_select and not state["fired"]:
            state["fired"] = True
            return _EmptyResult()
        return real_execute(self, statement, *a, **k)

    monkeypatch.setattr(Session, "execute", flaky_execute)
    inserted = discovery.run_discovery()
    monkeypatch.undo()   # снять патчи перед проверками

    assert inserted == 1   # досыпан только fresh.ru — батч не потерян
    with db.SessionLocal() as s:
        names = set(s.execute(select(Domain.domain)).scalars().all())
    assert names == {"race.ru", "fresh.ru"}


# ---------- M-2: retry не ретраит 4xx и отдаёт исходное исключение ----------

def _status_err(code):
    req = httpx.Request("GET", "http://x")
    return httpx.HTTPStatusError("x", request=req, response=httpx.Response(code, request=req))


def test_retry_predicate():
    from app.integrations.base import _is_retryable
    assert _is_retryable(_status_err(404)) is False
    assert _is_retryable(_status_err(429)) is True
    assert _is_retryable(_status_err(503)) is True
    assert _is_retryable(httpx.ConnectError("boom")) is True
    assert _is_retryable(ValueError("nope")) is False


def test_request_does_not_retry_4xx(monkeypatch):
    from app.integrations.base import BaseClient
    c = BaseClient("http://x")
    calls = {"n": 0}

    def fake(self, method, url, **kw):
        calls["n"] += 1
        return httpx.Response(404, request=httpx.Request(method, url))

    monkeypatch.setattr(httpx.Client, "request", fake)
    with pytest.raises(httpx.HTTPStatusError):   # исходное исключение, не RetryError
        c.request("GET", "http://x/y")
    assert calls["n"] == 1   # ровно одна попытка — 4xx не ретраится


# ---------- C2 (Critical): blacklist fail-closed — контроль тест-поинта ----------
# _control_ok — кэш на ПРОЦЕСС (класс BlacklistClient), общий между тестами этого файла.
# Сбрасываем его monkeypatch'ем в каждом тесте, трогающем реальный BlacklistClient, —
# иначе исход зависит от порядка запуска (предыдущий тест мог закэшировать True/False).

def test_blacklist_raises_when_resolver_cannot_reach_spamhaus(monkeypatch):
    from app.integrations import blacklist
    c = blacklist.BlacklistClient()
    monkeypatch.setattr(blacklist.BlacklistClient, "_control_ok", None)
    # резолвер отдаёт NXDOMAIN даже на тест-поинт (публичный резолвер заблокирован Spamhaus)
    monkeypatch.setattr(c, "_resolve", lambda host: None)
    import pytest
    with pytest.raises(RuntimeError):
        c.is_blacklisted("example.com")


def test_blacklist_none_goes_to_errors_and_downgrades(monkeypatch):
    # is_blacklisted вернул None (транзиент) -> в sig.errors -> risk-guard -> manual scored
    from app.services import scoring
    # прямой юнит на _decide: approved + blacklist-ошибка -> scored
    sig_err = {"errors": ["blacklist:unavailable"]}
    assert scoring._decide(0.9, sig_err, 0.7, 0.4) == "scored"


# ---------- M9: status-gate — рескорится только discovered/scored/rejected ----------

def test_score_only_discovered_status(monkeypatch):
    import app.db as db
    from app.models.domain import Domain
    from app.services import scoring
    with db.SessionLocal() as s:
        d = Domain(domain="live.ru", source="backorder", status="live", lane="bid")
        s.add(d); s.commit(); did = d.id
    out = scoring.score_domain(did)
    assert out.get("skipped") == "status"                   # live не рескорится
    with db.SessionLocal() as s:
        assert s.get(Domain, did).status == "live"          # статус цел


# ---------- M12: score_pending изолирует падение одного домена ----------

def test_score_pending_isolates_failure(monkeypatch):
    import app.db as db
    from app.models.domain import Domain
    from app.services import scoring
    with db.SessionLocal() as s:
        s.add_all([Domain(domain=f"d{i}.ru", source="backorder", status="discovered",
                          lane="bid", referring_domains=i) for i in range(3)]); s.commit()
    calls = {"n": 0}
    real = scoring.score_domain
    def _boom(did, clients=None, whois_budget=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return real(did, clients, whois_budget)
    monkeypatch.setattr(scoring, "score_domain", _boom)
    # не должно упасть, остальные 2 обработаны
    n = scoring.score_pending(limit=10)
    assert n == 3 and calls["n"] == 3
