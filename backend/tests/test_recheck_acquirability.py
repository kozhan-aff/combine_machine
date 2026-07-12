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
    assert v(True, None, NOW, lane="free") == "free"
    assert v(None, None, NOW, lane="free") == "unknown"   # whois промолчал — не врём в обе стороны
    assert v(False, None, NOW, lane="free") == "taken"    # свободный домен занят -> его выкупили
    assert v(False, NOW - timedelta(days=5), NOW, lane="bid") == "taken"    # дроп давно прошёл
    assert v(False, NOW + timedelta(days=3), NOW, lane="bid") == "waiting"  # дроп впереди — норма


def test_verdict_does_not_reject_a_drop_on_its_drop_day():
    """САМЫЙ ДОРОГОЙ БАГ. delete_date в фиде — ДАТА без времени, и _parse_deadline делает
    из неё 00:00 UTC дня дропа. Значит уже в 00:01 «дедлайн в будущем» ложно, а домен ещё
    занят — реестр освобождает его днём. Без запаса мы выбрасывали бы дроп РОВНО В ТОТ ДЕНЬ,
    когда его можно ловить."""
    v = scoring.acquirability_verdict
    midnight = NOW.replace(hour=0, minute=0, second=0, microsecond=0)   # 00:00 сегодняшнего дропа
    assert v(False, midnight, NOW, lane="bid") == "waiting", "выбросили дроп в день его дропа!"
    # и на следующий день, если реестр задержался
    assert v(False, midnight - timedelta(days=1), NOW, lane="bid") == "waiting"
    # а вот когда запас исчерпан — домен действительно продлили или перехватили
    assert v(False, midnight - timedelta(days=4), NOW, lane="bid") == "taken"


def test_verdict_never_rejects_a_bid_domain_without_deadline():
    """Для bid-домена «занят» — НОРМАЛЬНОЕ состояние (он ждёт своего дропа). Дедлайн теряется,
    если фид отдал непарсящийся delete_date (_parse_deadline -> None). Судить не по чему ->
    молчим. Иначе один дрейф формата фида отбраковал бы ВЕСЬ список дропов за прогон."""
    v = scoring.acquirability_verdict
    assert v(False, None, NOW, lane="bid") == "unknown"
    assert v(False, None, NOW, lane="free") == "taken"    # а свободный домен занять — можно только выкупив


def test_grace_boundary_is_pinned():
    """Сама величина запаса зажата с обеих сторон: иначе DROP_GRACE — магическая константа,
    которую можно сдвинуть, не уронив ни одного теста."""
    v, g = scoring.acquirability_verdict, scoring.DROP_GRACE
    eps = timedelta(minutes=1)
    assert v(False, NOW - g + eps, NOW, lane="bid") == "waiting"   # ещё внутри запаса
    assert v(False, NOW - g - eps, NOW, lane="bid") == "taken"     # запас исчерпан


def test_verdict_handles_naive_deadline_from_db():
    """SQLite отдаёт datetime без tzinfo — сравнение с aware now упало бы TypeError."""
    naive = (NOW + timedelta(days=3)).replace(tzinfo=None)
    assert scoring.acquirability_verdict(False, naive, NOW, lane="bid") == "waiting"


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


def test_drop_on_its_drop_day_is_not_rejected(sqlite_db, monkeypatch):
    """Сквозной случай самого дорогого бага: дедлайн = 00:00 СЕГОДНЯШНЕГО дня (так фид и
    отдаёт — датой без времени), домен ещё занят. Прогон не должен его выбросить."""
    did = _add(domain="today.ru", status="approved", lane="bid",
               acquire_deadline=NOW.replace(hour=0, minute=0, second=0, microsecond=0))
    _whois(monkeypatch, {"today.ru": False})
    r = scoring.recheck_acquirability()
    assert r["taken"] == 0 and r["waiting"] == 1
    with db.SessionLocal() as s:
        d = s.get(Domain, did)
        assert d.status == "approved", "дроп выброшен в день, когда его можно ловить!"
        assert d.reject_reason is None


def test_bid_domain_without_deadline_is_never_rejected(sqlite_db, monkeypatch):
    """Дрейф формата фида (delete_date не распарсился -> None) не должен отбраковать
    ВЕСЬ список дропов за один прогон. Но отметку он получает — см. следующий тест."""
    did = _add(domain="nodate.ru", status="approved", lane="bid", acquire_deadline=None)
    _whois(monkeypatch, {"nodate.ru": False})
    r = scoring.recheck_acquirability()
    assert r["unknown"] == 1 and r["taken"] == 0
    with db.SessionLocal() as s:
        assert s.get(Domain, did).status == "approved"


def test_deterministic_unknown_does_not_starve_the_queue(sqlite_db, monkeypatch):
    """ГОЛОДАНИЕ. whois ОТВЕТИЛ («занят»), но у bid-домена нет дедлайна — судить не по чему,
    и завтра ответ будет ТОТ ЖЕ. Не поставив отметку, мы бы навечно оставили такой домен в
    голове nulls_first-очереди: при бюджете меньше числа таких доменов (авария «фид сменил
    формат delete_date») перепроверка НИКОГДА не дошла бы до остального списка и молча
    выродилась в no-op — ровно в аварии, ради которой её и укрепляли."""
    from app.services.settings import update_settings
    _add(domain="nodate.ru", status="approved", lane="bid", acquire_deadline=None)
    _add(domain="normal.ru", status="approved", lane="free")
    update_settings(max_whois_per_run=1)                 # бюджета хватает ровно на один домен
    _whois(monkeypatch, {"nodate.ru": False, "normal.ru": True})

    calls = _whois(monkeypatch, {"nodate.ru": False, "normal.ru": True})
    scoring.recheck_acquirability()
    assert calls == ["nodate.ru"]                        # первый прогон съел неразрешимый

    calls2 = _whois(monkeypatch, {"nodate.ru": False, "normal.ru": True})
    scoring.recheck_acquirability()
    assert calls2 == ["normal.ru"], "неразрешимый домен навечно занял голову очереди"


def test_transient_whois_failure_is_retried_next_run(sqlite_db, monkeypatch):
    """А вот СБОЙ (сеть/A-Parser) транзиентен: отметку не ставим, домен возвращается в
    следующий прогон. Иначе сбой молча «освежил» бы весь список."""
    from app.services.settings import update_settings
    did = _add(domain="boom.ru", status="approved", lane="free")
    _add(domain="other.ru", status="approved", lane="free")
    update_settings(max_whois_per_run=1)
    _whois(monkeypatch, {"boom.ru": RuntimeError("A-Parser лёг")})
    scoring.recheck_acquirability()
    with db.SessionLocal() as s:
        assert s.get(Domain, did).acquirability_checked_at is None

    calls = _whois(monkeypatch, {"boom.ru": True, "other.ru": True})
    scoring.recheck_acquirability()
    assert calls == ["boom.ru"], "упавший домен не вернулся в очередь"


def test_domain_taken_into_purchase_mid_run_is_not_rejected(sqlite_db, monkeypatch):
    """ГОНКА: пока шёл whois, человек отправил домен в выкуп (create_order -> purchasing).
    Голый UPDATE перезатёр бы его нашим rejected и разъехался с живым заказом."""
    did = _add(domain="racing.ru", status="approved", lane="free")

    def probe(self, domain):
        with db.SessionLocal() as s:                     # имитируем клик оператора во время whois
            s.get(Domain, did).status = "purchasing"
            s.commit()
        return {"available": False, "created": None}     # whois говорит «занят»
    monkeypatch.setattr("app.integrations.aparser.AParserClient.whois_probe", probe)

    r = scoring.recheck_acquirability()
    assert r["taken"] == 0, "сводка соврала об отбраковке, которой не было"
    with db.SessionLocal() as s:
        d = s.get(Domain, did)
        assert d.status == "purchasing" and d.reject_reason is None


def test_unknown_does_not_touch_status_or_stamp(sqlite_db, monkeypatch):
    """whois не ответил -> НЕ помечаем проверенным: домен остаётся протухшим и вернётся
    в следующий прогон. Иначе сбой A-Parser молча «освежил» бы весь список."""
    did = _add(domain="mute.ru", status="approved", lane="free")
    _whois(monkeypatch, {"mute.ru": None})
    r = scoring.recheck_acquirability()
    # checked = сколько whois-вызовов сделали (= расход бюджета), а не сколько записали:
    # иначе сводка в панели не сходится (free+waiting+taken+unknown != checked) и врёт про квоту.
    assert r["unknown"] == 1 and r["checked"] == 1
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


def test_zero_budget_calls_nothing(sqlite_db, monkeypatch):
    """budget == 0 = «whois не звать вообще» — та же семантика, что в воронке. Раньше здесь
    было наоборот (0 -> без ограничения), что молча сняло бы кран с квоты A-Parser."""
    _add(domain="x.ru", status="approved", lane="free")
    monkeypatch.setattr("app.services.settings.get_settings",
                        lambda: {"max_whois_per_run": 0})
    calls = _whois(monkeypatch, {"x.ru": True})
    assert scoring.recheck_acquirability()["checked"] == 0 and calls == []


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
