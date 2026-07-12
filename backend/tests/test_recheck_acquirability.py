"""Перепроверка занятости доноров (M1c): список протухает — домены выкупают другие.

Скоринг решает приобретаемость один раз (T1); эта перепроверка возвращается к ней позже.
Сеть замокана (рубильник в conftest всё равно не пустил бы).
"""
from datetime import datetime, timedelta, timezone

import pytest

import app.db as db
from app.models.domain import Domain
from app.services import scoring


NOW = datetime.now(timezone.utc)


def _add(**kw):
    d = Domain(source="backorder", **kw)
    with db.SessionLocal() as s:
        s.add(d); s.commit(); s.refresh(d)
        return d.id


def _whois(monkeypatch, by_domain: dict):
    """A-Parser отдаёт available по имени домена; вызовы считаем."""
    calls: list = []

    def probe(self, domain):
        calls.append(domain)
        if isinstance(by_domain.get(domain), Exception):
            raise by_domain[domain]
        return {"available": by_domain.get(domain), "created": None}
    monkeypatch.setattr("app.integrations.aparser.AParserClient.whois_probe", probe)
    return calls


# --- вердикт: единственное место, где решается «можно ли ещё купить» ------------

def test_verdict_free_taken_waiting_unknown():
    v = scoring.acquirability_verdict
    assert v(True, None, NOW) == "free"
    assert v(None, None, NOW) == "unknown"          # whois промолчал — не врём в обе стороны
    assert v(False, None, NOW) == "taken"           # занят, дедлайна нет -> его выкупили
    assert v(False, NOW - timedelta(days=1), NOW) == "taken"    # дроп прошёл, а домен занят
    assert v(False, NOW + timedelta(days=3), NOW) == "waiting"  # дроп ещё не наступил — норма


def test_verdict_handles_naive_deadline_from_db():
    """SQLite отдаёт datetime без tzinfo — сравнение с aware now упало бы TypeError."""
    naive = (NOW + timedelta(days=3)).replace(tzinfo=None)
    assert scoring.acquirability_verdict(False, naive, NOW) == "waiting"


# --- перепроверка ---------------------------------------------------------------

def test_taken_donor_is_rejected(sqlite_db, monkeypatch):
    """Одобренный домен успели выкупить -> rejected/not_acquirable. Ради этого всё и делалось."""
    did = _add(domain="gone.ru", status="approved", lane="free")
    _whois(monkeypatch, {"gone.ru": False})
    r = scoring.recheck_acquirability()
    assert r["taken"] == 1 and r["checked"] == 1
    with db.SessionLocal() as s:
        d = s.get(Domain, did)
        assert d.status == "rejected" and d.reject_reason == "not_acquirable"
        assert d.acquirability_checked_at is not None


def test_free_donor_survives_and_is_stamped(sqlite_db, monkeypatch):
    did = _add(domain="ok.ru", status="approved", lane="free")
    _whois(monkeypatch, {"ok.ru": True})
    r = scoring.recheck_acquirability()
    assert r["free"] == 1
    with db.SessionLocal() as s:
        d = s.get(Domain, did)
        assert d.status == "approved" and d.acquirability_checked_at is not None


def test_drop_before_its_date_is_not_rejected(sqlite_db, monkeypatch):
    """backorder-дроп ЗАНЯТ до своей delete_date — это норма, а не протухание.
    Отбраковать его здесь = выкинуть из списка все ценные дропы."""
    did = _add(domain="drop.ru", status="approved", lane="bid",
               acquire_deadline=NOW + timedelta(days=5))
    _whois(monkeypatch, {"drop.ru": False})
    r = scoring.recheck_acquirability()
    assert r["waiting"] == 1 and r["taken"] == 0
    with db.SessionLocal() as s:
        assert s.get(Domain, did).status == "approved"


def test_unknown_does_not_touch_status_or_stamp(sqlite_db, monkeypatch):
    """whois не ответил -> НЕ помечаем проверенным: домен остаётся протухшим и вернётся
    в следующий прогон. Иначе сбой A-Parser молча «освежил» бы весь список."""
    did = _add(domain="mute.ru", status="approved", lane="free")
    _whois(monkeypatch, {"mute.ru": None})
    r = scoring.recheck_acquirability()
    assert r["unknown"] == 1 and r["checked"] == 0
    with db.SessionLocal() as s:
        d = s.get(Domain, did)
        assert d.status == "approved" and d.acquirability_checked_at is None


def test_whois_failure_does_not_sink_the_batch(sqlite_db, monkeypatch):
    """Падение одного домена не топит прогон (как в скоринге/оркестраторе)."""
    _add(domain="boom.ru", status="approved", lane="free")
    _add(domain="fine.ru", status="approved", lane="free")
    _whois(monkeypatch, {"boom.ru": RuntimeError("A-Parser лёг"), "fine.ru": True})
    r = scoring.recheck_acquirability()
    assert r["unknown"] == 1 and r["free"] == 1


def test_only_selected_donors_are_touched(sqlite_db, monkeypatch):
    """purchasing/purchased не трогаем — ими распоряжается выкуп (M2), иначе перепроверка
    отбраковала бы домен из-под оформленного заказа. discovered — дело скоринга."""
    for st in ("purchasing", "purchased", "discovered", "rejected"):
        _add(domain=f"{st}.ru", status=st, lane="free")
    _add(domain="approved.ru", status="approved", lane="free")
    _add(domain="scored.ru", status="scored", lane="free")
    calls = _whois(monkeypatch, {"approved.ru": True, "scored.ru": True})
    scoring.recheck_acquirability()
    assert sorted(calls) == ["approved.ru", "scored.ru"], f"полезли не в свои статусы: {calls}"


def test_budget_caps_whois_calls(sqlite_db, monkeypatch):
    """Бюджет — общий max_whois_per_run: перепроверка не должна выжечь квоту A-Parser."""
    from app.services.settings import update_settings
    for i in range(5):
        _add(domain=f"d{i}.ru", status="approved", lane="free")
    update_settings(max_whois_per_run=2)
    calls = _whois(monkeypatch, {f"d{i}.ru": True for i in range(5)})
    scoring.recheck_acquirability()
    assert len(calls) == 2


def test_stalest_are_checked_first(sqlite_db, monkeypatch):
    """Первыми идут те, кого не сверяли дольше всех (NULL — дольше всех)."""
    from app.services.settings import update_settings
    _add(domain="fresh.ru", status="approved", acquirability_checked_at=NOW)
    _add(domain="old.ru", status="approved", acquirability_checked_at=NOW - timedelta(days=9))
    _add(domain="never.ru", status="approved")
    update_settings(max_whois_per_run=2)
    calls = _whois(monkeypatch, {"never.ru": True, "old.ru": True, "fresh.ru": True})
    scoring.recheck_acquirability()
    assert calls == ["never.ru", "old.ru"], f"проверили не самых протухших: {calls}"


def test_stale_donors_counter(sqlite_db):
    _add(domain="never.ru", status="approved")                                   # ни разу
    _add(domain="old.ru", status="approved", acquirability_checked_at=NOW - timedelta(days=9))
    _add(domain="fresh.ru", status="approved", acquirability_checked_at=NOW)     # свежий
    _add(domain="bought.ru", status="purchased")                                 # не донор
    assert scoring.stale_donors(days=3) == 2


# --- панель ---------------------------------------------------------------------

def test_panel_recheck_runs_and_reports(client, monkeypatch):
    """Кнопка запускает фоновый джоб; итог виден в progress.message (его показывает бар)."""
    from app.services import jobs
    jobs._reset()
    _add(domain="gone.ru", status="approved", lane="free")
    _whois(monkeypatch, {"gone.ru": False})

    r = client.post("/run/recheck", data={"n": "10"}, follow_redirects=False)
    assert r.status_code == 303
    for _ in range(200):                       # джоб в ThreadPoolExecutor — дождаться
        if not jobs.is_running("recheck"):
            break
        import time; time.sleep(0.02)
    p = jobs.progress("recheck")
    assert p["error"] is None and "ЗАНЯТЫ 1" in p["message"]
    with db.SessionLocal() as s:
        from sqlalchemy import select
        d = s.execute(select(Domain).where(Domain.domain == "gone.ru")).scalar_one()
        assert d.status == "rejected" and d.reject_reason == "not_acquirable"


def test_panel_domains_shows_stale_counter(client, sqlite_db):
    _add(domain="never.ru", status="approved")
    assert "не сверялись 3+ дня" in client.get("/domains").text


@pytest.fixture(autouse=True)
def _reset_jobs():
    from app.services import jobs
    jobs._reset()
    yield
    jobs._reset()
