"""Воронка скоринга дёшево→дорого: ранний выход, reject_reason, дорогой Wayback только для выживших."""
from datetime import datetime, timezone, timedelta
import app.db as db
from app.models.domain import Domain
from app.services import scoring


def _mk(**kw):
    with db.SessionLocal() as s:
        d = Domain(domain=kw.pop("domain", "x.ru"), source=kw.pop("source", "cctld"),
                   status="discovered", **kw)
        s.add(d); s.commit(); s.refresh(d)
        return d.id


class _Wayback:
    def __init__(self, age_years: float = 9.0):
        self.calls = 0
        self.age_years = age_years
    def classify_history(self, domain):
        self.calls += 1
        return {"prior_flags": {c: False for c in ("adult", "pharma", "casino", "gambling", "spam")},
                "first_seen": None, "age_years": self.age_years, "wayback_checked": True, "sampled": 5}


def _clients(whois_dt=None, wayback=None, rkn=False, bl=False, indexed_echo=True,
             whois=None, whois_raises=False):
    """whois: dict {"available":..., "created":...} (новый формат, приобретаемость известна
    явно). whois_dt: старый позиционный аргумент (только дата) — оборачивается в
    {"available": False, "created": whois_dt} (занят, но с датой регистрации — для тестов,
    доходящих до T2/T3 через lane="bid" на тестовом Domain). whois_raises=True — whois_probe
    бросает (недоступен)."""
    pr = whois if whois is not None else {"available": False, "created": whois_dt}
    class _W:  # aparser
        def whois_probe(self, dom):
            if whois_raises:
                raise RuntimeError("whois timeout")
            return pr
    class _R:
        def is_listed(self, dom): return rkn
    class _B:
        def is_blacklisted(self, dom): return bl
    class _S:
        def indexed_echo(self, dom): return indexed_echo
    return {"aparser": _W(), "rkn": _R(), "blacklist": _B(), "searxng": _S(),
            "wayback": wayback}


def _clients_whois_raises(wb, rkn=False, bl=False, indexed_echo=True):
    """Как _clients, но whois_probe падает (недоступен) — для Finding-1 фолбэка."""
    class _W:  # aparser
        def whois_probe(self, dom): raise RuntimeError("whois timeout")
    class _R:
        def is_listed(self, dom): return rkn
    class _B:
        def is_blacklisted(self, dom): return bl
    class _S:
        def indexed_echo(self, dom): return indexed_echo
    return {"aparser": _W(), "rkn": _R(), "blacklist": _B(), "searxng": _S(),
            "wayback": wb}


def _id_of(domain: str):
    from sqlalchemy import select
    return select(Domain.id).where(Domain.domain == domain)


def _count_discovered():
    from sqlalchemy import select, func
    return select(func.count()).select_from(Domain).where(Domain.status == "discovered")


class _WaybackDirty:
    """Грязная история (casino) — доживает до T3, там и отклоняется."""
    def __init__(self): self.calls = 0
    def classify_history(self, domain):
        self.calls += 1
        return {"prior_flags": {"adult": False, "pharma": False, "casino": True,
                                 "gambling": False, "spam": False},
                "first_seen": None, "age_years": 9.0, "wayback_checked": True, "sampled": 5}


class _WaybackWeak:
    """Чистая, но НЕ проверенная история (checked=False) — history_cleanliness=0.5,
    не 1.0; возраст не переопределяет (whois его уже дал) — для low_score теста."""
    def __init__(self): self.calls = 0
    def classify_history(self, domain):
        self.calls += 1
        return {"prior_flags": {c: False for c in ("adult", "pharma", "casino", "gambling", "spam")},
                "first_seen": None, "age_years": None, "wayback_checked": False, "sampled": 0}


class _WaybackYoung:
    """Чистая история, но фолбэк-возраст из Wayback моложе порога — для Finding-1 теста
    (whois недоступен, T3 даёт единственную оценку возраста)."""
    def __init__(self): self.calls = 0
    def classify_history(self, domain):
        self.calls += 1
        return {"prior_flags": {c: False for c in ("adult", "pharma", "casino", "gambling", "spam")},
                "first_seen": None, "age_years": 1.0, "wayback_checked": True, "sampled": 5}


def test_too_young_rejects_before_wayback():
    did = _mk(domain="young.ru", referring_domains=5, lane="bid")
    wb = _Wayback()
    young = datetime.now(timezone.utc) - timedelta(days=365)   # 1 год
    out = scoring.score_domain(did, clients=_clients(young, wb))
    assert out["status"] == "rejected" and out["reject_reason"] == "too_young"
    assert wb.calls == 0            # ЯДРО: дорогой Wayback НЕ вызван для молодого домена


def test_feed_flag_rejects_first():
    did = _mk(domain="blocked.ru", referring_domains=50, feed_flags={"rkn": True})
    wb = _Wayback()
    out = scoring.score_domain(did, clients=_clients(None, wb))
    assert out["reject_reason"] == "feed_flag" and wb.calls == 0


def test_low_rd_rejects():
    did = _mk(domain="thin.ru", referring_domains=0)
    wb = _Wayback()
    from app.services import settings as st
    st.update_settings(min_referring_domains=1)
    out = scoring.score_domain(did, clients=_clients(None, wb))
    assert out["reject_reason"] == "low_rd" and wb.calls == 0


def test_rkn_rejects_before_wayback():
    did = _mk(domain="rkn.ru", referring_domains=50, lane="bid")
    wb = _Wayback()
    old = datetime.now(timezone.utc) - timedelta(days=365 * 8)
    out = scoring.score_domain(did, clients=_clients(old, wb, rkn=True))
    assert out["reject_reason"] == "rkn" and wb.calls == 0


def test_whois_none_falls_through_to_wayback_age():
    did = _mk(domain="nowhois.ru", referring_domains=3000, lane="bid")
    wb = _Wayback()
    out = scoring.score_domain(did, clients=_clients(None, wb))   # whois не отдал дату
    assert wb.calls == 1                                          # дошли до T3
    assert out["status"] in ("approved", "scored")               # чистый сильный домен
    with db.SessionLocal() as s:
        d = s.get(Domain, did)
    assert float(d.age_years) == 9.0                             # возраст — фолбэком из Wayback


def test_clean_strong_domain_approved():
    did = _mk(domain="good.ru", referring_domains=3000, lane="bid")
    wb = _Wayback()
    old = datetime.now(timezone.utc) - timedelta(days=365 * 9)
    out = scoring.score_domain(did, clients=_clients(old, wb))
    assert wb.calls == 1 and out["status"] == "approved" and out["reject_reason"] is None


def test_blacklist_rejects_before_wayback():
    did = _mk(domain="blacklisted.ru", referring_domains=50, lane="bid")
    wb = _Wayback()
    old = datetime.now(timezone.utc) - timedelta(days=365 * 8)   # T1 пройден
    out = scoring.score_domain(did, clients=_clients(old, wb, bl=True))
    assert out["status"] == "rejected" and out["reject_reason"] == "blacklist"
    assert wb.calls == 0            # blacklist — T2, Wayback (T3) до неё не доходит


def test_blacklist_none_downgrades_via_funnel():
    """Ревью C2 (Important gap): строка `blacklisted is None -> errors.append("blacklist:unavailable")`
    в _funnel была покрыта только юнитом на _decide напрямую (test_m1_fixes.py), а не реальной
    проводкой через score_domain/_funnel. Прогоняем полную воронку с blacklist-клиентом,
    отдающим None (транзиент), на иначе-сильном домене (тот же профиль, что и в
    test_clean_strong_domain_approved) — без строки-фикса errors остался бы пуст и статус
    остался бы approved, тест бы упал."""
    did = _mk(domain="bl-none.ru", referring_domains=3000, lane="bid")
    wb = _Wayback()
    old = datetime.now(timezone.utc) - timedelta(days=365 * 9)
    out = scoring.score_domain(did, clients=_clients(old, wb, bl=None))
    assert "blacklist:unavailable" in out["errors"]
    assert out["status"] == "scored"        # downgrade from approved (не rejected — не hard-reject)
    assert wb.calls == 1                    # blacklist:unavailable не блокирует T3


def test_history_dirty_rejects_after_wayback():
    did = _mk(domain="dirtyhist.ru", referring_domains=50, lane="bid")
    wb = _WaybackDirty()
    old = datetime.now(timezone.utc) - timedelta(days=365 * 8)   # T0-T2 пройдены
    out = scoring.score_domain(did, clients=_clients(old, wb))
    assert out["status"] == "rejected" and out["reject_reason"] == "history_dirty"
    assert wb.calls == 1            # дошли до T3 — там и отклонились


def test_low_score_reject():
    did = _mk(domain="weak.ru", referring_domains=1, lane="bid")
    wb = _WaybackWeak()
    old_enough = datetime.now(timezone.utc) - timedelta(days=1150)   # ~3.15 года, чуть старше порога
    out = scoring.score_domain(did, clients=_clients(old_enough, wb, indexed_echo=False))
    assert out["status"] == "rejected" and out["reject_reason"] == "low_score"
    assert wb.calls == 1            # дошли до compute_score — отклонил composite score, не воронка


def test_runtime_approve_at_downgrades_high_scorer_to_scored():
    """Finding 1 (2026-07 review): рантайм /settings approve_at (не только cfg.DECISION)
    должен реально управлять статусом, а не только превью-счётчиками на /settings."""
    from app.services import settings as st
    did = _mk(domain="runtime-approve.ru", referring_domains=100, lane="bid")
    wb = _Wayback()
    old = datetime.now(timezone.utc) - timedelta(days=365 * 9)
    st.update_settings(approve_at=0.99)
    out = scoring.score_domain(did, clients=_clients(old, wb))
    assert 0.40 < out["score"] < 0.99             # ~0.87 — сильный, но не «approve по-новому»
    assert out["status"] == "scored" and out["reject_reason"] is None


def test_runtime_thresholds_can_reject_previously_approved_score():
    """Тот же сильный домен: подняв ОБА порога выше его score, получаем rejected/low_score —
    не «застрявший approved» из статических cfg.DECISION."""
    from app.services import settings as st
    did = _mk(domain="runtime-reject.ru", referring_domains=100, lane="bid")
    wb = _Wayback()
    old = datetime.now(timezone.utc) - timedelta(days=365 * 9)
    st.update_settings(manual_review_at=0.9, approve_at=0.95)
    out = scoring.score_domain(did, clients=_clients(old, wb))
    assert out["status"] == "rejected" and out["reject_reason"] == "low_score"


def test_runtime_min_age_years_rejects_too_young():
    """Spec §G (был пропущен): рантайм min_age_years из /settings уже используется в _funnel
    (T1) — 4-летний домен отклоняется too_young при поднятом пороге в 5 лет."""
    from app.services import settings as st
    did = _mk(domain="four-years.ru", referring_domains=50, lane="bid")
    wb = _Wayback()
    st.update_settings(min_age_years=5.0)
    four_years = datetime.now(timezone.utc) - timedelta(days=365 * 4)
    out = scoring.score_domain(did, clients=_clients(four_years, wb))
    assert out["status"] == "rejected" and out["reject_reason"] == "too_young"
    assert wb.calls == 0                          # too_young — T1, дешёвый Wayback не вызван


def test_too_young_fallback_from_wayback_when_whois_fails():
    """Finding 1: whois упал (T1 без даты) -> возраст добираем из Wayback (T3); если
    фолбэк-возраст < порога — reject too_young, а не тихий проскок в compute_score."""
    did = _mk(domain="whoisdown.ru", referring_domains=50, lane="bid")
    wb = _WaybackYoung()
    out = scoring.score_domain(did, clients=_clients_whois_raises(wb))
    assert out["status"] == "rejected" and out["reject_reason"] == "too_young"
    assert wb.calls == 1            # фолбэк-возраст пришёл именно из Wayback


def test_raw_registered_without_deadline_waits_instead_of_rejecting(monkeypatch, sqlite_db):
    """БЫЛО: сырой домен, whois=занят, дедлайна нет → not_acquirable (выброс).
    СТАЛО: остаётся discovered. Wayback по-прежнему НЕ вызывается (ранний выход тот же).

    Почему изменено (дебаг 2026-07-13): cctld — реестр ОСВОБОЖДАЮЩИХСЯ доменов, и до своего
    дропа такой домен ОБЯЗАН быть занят. Трактовать это как «занят навсегда» — значит слать
    в rejected весь реестр (~9.5 тыс. строк), ни разу не дождавшись дропа. Дедлайн теперь
    приходит из имени архива (integrations/cctld.py), а домен без дедлайна и без лейна —
    случай «судить не по чему»: молчим и ждём, а не выбрасываем."""
    from app.services import scoring
    import app.db as db
    from app.models.domain import Domain
    wb = _Wayback()   # счётчик .calls (как в других тестах файла)
    clients = _clients(whois={"available": False, "created": None}, wayback=wb)
    with db.SessionLocal() as s:
        s.add(Domain(domain="taken.ru", source="cctld", status="discovered", lane=None,
                     referring_domains=None)); s.commit()
        did = s.execute(_id_of("taken.ru")).scalar_one()
    out = scoring.score_domain(did, clients)
    assert out["status"] == "discovered" and out.get("unresolved") is True
    assert wb.calls == 0                      # дорогой Wayback по-прежнему не тронут


def test_raw_free_gets_free_lane(monkeypatch, sqlite_db):
    """Сырой домен, whois=свободен → lane=free, доходит до Wayback (возраст из Wayback)."""
    from app.services import scoring
    import app.db as db
    from app.models.domain import Domain
    wb = _Wayback(age_years=10.0)
    clients = _clients(whois={"available": True, "created": None}, wayback=wb)
    with db.SessionLocal() as s:
        s.add(Domain(domain="free.ru", source="reg_ru", status="discovered", lane=None)); s.commit()
        did = s.execute(_id_of("free.ru")).scalar_one()
    scoring.score_domain(did, clients)
    with db.SessionLocal() as s:
        d = s.get(Domain, did)
    assert d.lane == "free" and wb.calls == 1


def test_whois_fail_stays_discovered(sqlite_db):
    """whois упал на сыром домене → остаётся discovered, не rejected, Wayback не вызван."""
    from app.services import scoring
    import app.db as db
    from app.models.domain import Domain
    wb = _Wayback()
    clients = _clients(whois_raises=True, wayback=wb)
    with db.SessionLocal() as s:
        s.add(Domain(domain="oops.ru", source="cctld", status="discovered", lane=None)); s.commit()
        did = s.execute(_id_of("oops.ru")).scalar_one()
    out = scoring.score_domain(did, clients)
    assert out.get("unresolved") is True and wb.calls == 0
    with db.SessionLocal() as s:
        assert s.get(Domain, did).status == "discovered"      # не сдвинулся


def test_raw_source_future_deadline_stays_discovered():
    # сырой домен, whois «занят», но дедлайн дропа в будущем -> ждём дропа, не reject
    future = datetime.now(timezone.utc) + timedelta(days=5)
    did = _mk(domain="dropping.ru", lane=None, source="cctld",
              referring_domains=10, acquire_deadline=future)
    wb = _Wayback()
    out = scoring.score_domain(did, _clients(whois={"available": False, "created": None}, wayback=wb))
    assert out.get("unresolved") is True
    assert out["status"] == "discovered"
    assert wb.calls == 0                      # дорогой Wayback не тронут


def test_raw_source_no_deadline_is_not_rejected():
    """Парная регрессия к тесту выше: без дедлайна и без лейна домен НЕ выбрасывается.
    Занятость сырого домена до дропа — норма, а не приговор (дебаг 2026-07-13)."""
    did = _mk(domain="taken.ru", lane=None, source="cctld", referring_domains=10)
    wb = _Wayback()
    out = scoring.score_domain(did, _clients(whois={"available": False, "created": None}, wayback=wb))
    assert out["status"] == "discovered" and out.get("unresolved") is True
    assert wb.calls == 0


def test_raw_source_past_deadline_is_not_acquirable():
    # сырой домен, whois «занят», дедлайн дропа прошёл ДАВНО (за пределами DROP_GRACE)
    # -> реально занят, ждать нечего. Было -1 день, стало -5: delete_date в фиде — ДАТА без
    # времени (00:00 дня дропа), поэтому сутки после дедлайна ещё НЕ значат «домен потерян»
    # (реестр освобождает его в течение дня). Запас — scoring.DROP_GRACE, см. соседний тест.
    past = datetime.now(timezone.utc) - timedelta(days=5)
    did = _mk(domain="expired.ru", lane=None, source="cctld",
              referring_domains=10, acquire_deadline=past)
    wb = _Wayback()
    out = scoring.score_domain(did, _clients(whois={"available": False, "created": None}, wayback=wb))
    assert out["status"] == "rejected" and out["reject_reason"] == "not_acquirable"
    assert wb.calls == 0


def test_drop_day_deadline_is_not_rejected():
    """Дедлайн = 00:00 СЕГОДНЯШНЕГО дня (именно так фид отдаёт delete_date — датой без
    времени), домен ещё занят: реестр освободит его в течение дня. Отбраковать здесь =
    выбросить дроп ровно в тот день, когда его можно ловить."""
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    did = _mk(domain="dropping-today.ru", lane=None, source="cctld",
              referring_domains=10, acquire_deadline=today)
    out = scoring.score_domain(did, _clients(whois={"available": False, "created": None}))
    assert out.get("unresolved") is True
    assert out["status"] == "discovered", "дроп выброшен в день его дропа"


def test_whois_budget_caps_run(monkeypatch, sqlite_db):
    """max_whois_per_run=1 + 2 сырых домена → whois только у одного, второй остаётся discovered."""
    from app.services import scoring
    from app.services.settings import update_settings
    import app.db as db
    from app.models.domain import Domain
    update_settings(max_whois_per_run=1)
    wb = _Wayback(age_years=10.0)
    clients = _clients(whois={"available": True, "created": None}, wayback=wb)
    # score_pending строит клиентов сама (_make_clients) — здесь нет параметра для их подмены,
    # поэтому подменяем сам _make_clients, чтобы прогон был офлайн (без реального A-Parser/Wayback).
    monkeypatch.setattr(scoring, "_make_clients", lambda: clients)
    with db.SessionLocal() as s:
        s.add_all([Domain(domain=f"r{i}.ru", source="cctld", status="discovered", lane=None,
                          referring_domains=None) for i in range(2)]); s.commit()
    scoring.score_pending(limit=10)
    with db.SessionLocal() as s:
        still = s.execute(_count_discovered()).scalar()
    assert still == 1                                          # один не обработан (бюджет исчерпан)


class _AhrefsMock:
    def __init__(self, dr=42, backlinks=500, referring_domains=300, raises=False):
        self.calls = 0
        self.dr, self.backlinks, self.referring_domains = dr, backlinks, referring_domains
        self.raises = raises
    def ahrefs_probe(self, domain):
        self.calls += 1
        if self.raises:
            raise RuntimeError("captcha service down")
        return {"dr": self.dr, "backlinks": self.backlinks, "referring_domains": self.referring_domains}


def test_ahrefs_skipped_when_feed_has_referring_domains():
    """Домен уже с RD из фида (backorder-like) -> Ahrefs НЕ вызывается, даже с живым
    бюджетом — не дублируем платный вызов там, где фид уже дал число."""
    did = _mk(domain="hasrd.ru", referring_domains=500, source="backorder", lane="bid")
    wb = _Wayback()
    ah = _AhrefsMock()
    old = datetime.now(timezone.utc) - timedelta(days=365 * 9)
    clients = _clients(old, wb)
    clients["aparser"].ahrefs_probe = ah.ahrefs_probe   # прикрутить мок Ahrefs к тому же aparser-дублёру
    out = scoring.score_domain(did, clients=clients, ahrefs_budget=[50])
    assert ah.calls == 0
    assert out["status"] in ("approved", "scored")


def test_ahrefs_called_when_feed_has_no_referring_domains_and_budget_positive():
    """Домен без RD из фида (cctld/reg_ru/sweb-like), T3-выживший, живой бюджет ->
    Ahrefs вызывается, DR/RD из него попадают в sig и в итоге в Domain."""
    did = _mk(domain="nord.ru", referring_domains=None, source="cctld", lane="bid")
    wb = _Wayback()
    ah = _AhrefsMock(dr=55, backlinks=1000, referring_domains=200)
    old = datetime.now(timezone.utc) - timedelta(days=365 * 9)
    clients = _clients(old, wb)
    clients["aparser"].ahrefs_probe = ah.ahrefs_probe
    out = scoring.score_domain(did, clients=clients, ahrefs_budget=[50])
    assert ah.calls == 1
    assert out["breakdown"]["components"]["authority"] > 0.0
    with db.SessionLocal() as s:
        d = s.get(Domain, did)
    assert d.referring_domains == 200          # Ahrefs domains-count перезаписал None из фида
    assert d.score_breakdown["ahrefs_backlinks"] == 1000   # informational (out["breakdown"] — только
                                                           # result["breakdown"] из compute_score,
                                                           # ahrefs_backlinks живёт в d.score_breakdown)


def test_ahrefs_not_called_when_budget_is_none():
    """ahrefs_budget=None (не передан явно) -> Ahrefs НЕ вызывается (opt-in, платный)."""
    did = _mk(domain="nobudget.ru", referring_domains=None, source="cctld", lane="bid")
    wb = _Wayback()
    ah = _AhrefsMock()
    old = datetime.now(timezone.utc) - timedelta(days=365 * 9)
    clients = _clients(old, wb)
    clients["aparser"].ahrefs_probe = ah.ahrefs_probe
    scoring.score_domain(did, clients=clients)     # без ahrefs_budget
    assert ah.calls == 0


def test_ahrefs_not_called_when_budget_exhausted():
    did = _mk(domain="exhausted.ru", referring_domains=None, source="cctld", lane="bid")
    wb = _Wayback()
    ah = _AhrefsMock()
    old = datetime.now(timezone.utc) - timedelta(days=365 * 9)
    clients = _clients(old, wb)
    clients["aparser"].ahrefs_probe = ah.ahrefs_probe
    out = scoring.score_domain(did, clients=clients, ahrefs_budget=[0])
    assert ah.calls == 0
    assert out["status"] in ("approved", "scored", "rejected")   # не unresolved — не гейт приобретаемости


def test_ahrefs_failure_does_not_crash_funnel():
    did = _mk(domain="ahrefsdown.ru", referring_domains=None, source="cctld", lane="bid")
    wb = _Wayback()
    ah = _AhrefsMock(raises=True)
    old = datetime.now(timezone.utc) - timedelta(days=365 * 9)
    clients = _clients(old, wb)
    clients["aparser"].ahrefs_probe = ah.ahrefs_probe
    out = scoring.score_domain(did, clients=clients, ahrefs_budget=[50])
    assert ah.calls == 1
    assert any(e.startswith("ahrefs:") for e in out["errors"])
    assert out["breakdown"]["components"]["authority"] == 0.0   # сбой -> dr=None -> 0, не крэш


# --- квота: воронка не платит whois'ом дважды за детерминированный ответ ---------

def test_score_pending_skips_domains_whose_drop_is_still_ahead(sqlite_db, monkeypatch):
    """Ревью 2026-07-13, Important 1. Не-bid домен ДО своего дропа гарантированно занят
    (реестр освобождающихся на то и реестр) — вердикт вернёт waiting, домен останется
    discovered. Брать его в прогон = купить whois'ом ответ, который уже известен. С cctld,
    везущим дедлайн, таких доменов ~9.5 тыс.: один «весь пул» выжигал бы весь max_whois_per_run
    на них с нулевым продвижением."""
    from datetime import datetime, timedelta, timezone
    from app.services import scoring
    import app.db as db
    from app.models.domain import Domain

    future = datetime.now(timezone.utc) + timedelta(days=10)
    with db.SessionLocal() as s:
        s.add(Domain(domain="waits.ru", source="cctld", status="discovered", lane=None,
                     acquire_deadline=future))                      # дроп впереди -> не берём
        s.add(Domain(domain="today.ru", source="cctld", status="discovered", lane=None,
                     acquire_deadline=datetime.now(timezone.utc)))  # дроп настал -> берём
        s.add(Domain(domain="bid.ru", source="backorder", status="discovered", lane="bid",
                     referring_domains=50, acquire_deadline=future))  # bid -> берём всегда
        s.commit()

    seen = []
    monkeypatch.setattr(scoring, "score_domain",
                        lambda did, *a, **kw: seen.append(did) or {})
    monkeypatch.setattr(scoring, "_make_clients", lambda: {})
    scoring.score_pending(limit=50)

    with db.SessionLocal() as s:
        picked = {s.get(Domain, i).domain for i in seen}
    assert picked == {"today.ru", "bid.ru"}, f"взяли лишнее/потеряли нужное: {picked}"


def test_scorable_excludes_domain_whose_drop_is_tomorrow(sqlite_db, monkeypatch):
    """F20 (аудит 2026-07-14). `scorable()` сравнивал `acquire_deadline <= now + DROP_GRACE` —
    это не «дроп наступил с запасом», а «дроп наступит В ПРЕДЕЛАХ DROP_GRACE ВПЕРЕДИ». С
    DROP_GRACE=2 дня дроп ЗАВТРА уже проходил в выборку, хотя такой домен гарантированно ещё
    занят (реестр освобождающихся на то и реестр) — whois впустую. `DROP_GRACE` здесь вообще
    не нужен: окно ловли открывается РОВНО когда `acquire_deadline <= now`, это другая граница,
    чем верхний запас в acquirability_verdict (там DROP_GRACE — окно ПОСЛЕ дропа, не трогаем)."""
    from datetime import datetime, timedelta, timezone
    from app.services import scoring
    import app.db as db
    from app.models.domain import Domain

    now = datetime.now(timezone.utc)
    with db.SessionLocal() as s:
        s.add(Domain(domain="tomorrow.ru", source="cctld", status="discovered", lane=None,
                     acquire_deadline=now + timedelta(days=1)))    # дроп ЗАВТРА — ещё занят
        s.commit()

    seen = []
    monkeypatch.setattr(scoring, "score_domain", lambda did, *a, **kw: seen.append(did) or {})
    monkeypatch.setattr(scoring, "_make_clients", lambda: {})
    scoring.score_pending(limit=50)

    with db.SessionLocal() as s:
        picked = {s.get(Domain, i).domain for i in seen}
    assert picked == set(), f"дроп ЗАВТРА не должен браться в скоринг: {picked}"


def test_scorable_includes_domain_whose_drop_already_happened(sqlite_db, monkeypatch):
    """Обратная сторона теста выше: дроп УЖЕ наступил (несколько часов назад) — окно ловли
    открыто, whois впервые может ответить «свободен». Фикс не обязан перегибать в другую
    сторону и терять уже созревший дроп."""
    from datetime import datetime, timedelta, timezone
    from app.services import scoring
    import app.db as db
    from app.models.domain import Domain

    now = datetime.now(timezone.utc)
    with db.SessionLocal() as s:
        s.add(Domain(domain="just-dropped.ru", source="cctld", status="discovered", lane=None,
                     acquire_deadline=now - timedelta(hours=3)))   # дроп уже случился
        s.commit()

    seen = []
    monkeypatch.setattr(scoring, "score_domain", lambda did, *a, **kw: seen.append(did) or {})
    monkeypatch.setattr(scoring, "_make_clients", lambda: {})
    scoring.score_pending(limit=50)

    with db.SessionLocal() as s:
        picked = {s.get(Domain, i).domain for i in seen}
    assert picked == {"just-dropped.ru"}, f"созревший дроп должен уйти в скоринг: {picked}"


def test_unresolved_domain_remembers_it_was_checked(sqlite_db):
    """Whois ОТВЕТИЛ («занят», дроп впереди) — ответ детерминированный, завтра будет тот же.
    Факт сверки обязан осесть в БД, иначе следующий прогон платит за него заново."""
    from datetime import datetime, timedelta, timezone
    from app.services import scoring
    import app.db as db
    from app.models.domain import Domain

    future = datetime.now(timezone.utc) + timedelta(days=10)
    did = _mk(domain="waits.ru", lane=None, source="cctld", referring_domains=10,
              acquire_deadline=future)
    wb = _Wayback()
    out = scoring.score_domain(did, _clients(whois={"available": False, "created": None}, wayback=wb))
    assert out.get("unresolved") is True
    with db.SessionLocal() as s:
        d = s.get(Domain, did)
        assert d.status == "discovered"                   # статус не тронут
        assert d.acquirability_checked_at is not None     # но сверку запомнили
    assert wb.calls == 0


def test_domain_without_deadline_gets_rechecked_after_cooldown(sqlite_db, monkeypatch):
    """Ревью 2026-07-13, CRITICAL. «Спросили один раз — больше не спрашиваем» здесь смертельно:
    витрины reg.ru/sweb дату дропа НЕ отдают, а «занят сегодня» без даты не говорит ничего о том,
    когда домен освободится. С одним шансом такой домен НИКОГДА не увидел бы собственного дропа —
    вся популяция reg.ru/sweb навсегда оседала бы в discovered. Поэтому здесь КУЛДАУН."""
    from datetime import datetime, timedelta, timezone
    from app.services import scoring
    import app.db as db
    from app.models.domain import Domain

    now = datetime.now(timezone.utc)
    with db.SessionLocal() as s:
        s.add(Domain(domain="fresh.ru", source="reg_ru", status="discovered", lane=None,
                     acquire_deadline=None, acquirability_checked_at=None))       # ни разу
        s.add(Domain(domain="cooled.ru", source="reg_ru", status="discovered", lane=None,
                     acquire_deadline=None,
                     acquirability_checked_at=now - scoring.RECHECK_EVERY - timedelta(hours=1)))
        s.add(Domain(domain="justnow.ru", source="sweb", status="discovered", lane=None,
                     acquire_deadline=None,
                     acquirability_checked_at=now - timedelta(minutes=5)))        # только что
        s.commit()

    seen = []
    monkeypatch.setattr(scoring, "score_domain", lambda did, *a, **kw: seen.append(did) or {})
    monkeypatch.setattr(scoring, "_make_clients", lambda: {})
    scoring.score_pending(limit=50)
    with db.SessionLocal() as s:
        picked = {s.get(Domain, i).domain for i in seen}
    # свежий и остывший — берём (вдруг дроп уже случился); только что спрошенный — нет
    assert picked == {"fresh.ru", "cooled.ru"}, picked


def test_empty_score_run_explains_why(sqlite_db, monkeypatch):
    """Пустой прогон Score теперь ШТАТЕН (все ждут дропа) и обязан назвать причину — тот же
    стандарт, что уже применён к перепроверке."""
    from datetime import datetime, timedelta, timezone
    from app.services import jobs, scoring
    import app.db as db
    from app.models.domain import Domain

    future = datetime.now(timezone.utc) + timedelta(days=9)
    with db.SessionLocal() as s:
        s.add(Domain(domain="waits.ru", source="cctld", status="discovered", lane=None,
                     acquire_deadline=future))
        s.commit()
    monkeypatch.setattr(scoring, "_make_clients", lambda: {})
    assert scoring.score_pending(limit=50) == 0
    msg = jobs.last("score")["message"]
    assert "оценивать нечего" in msg and "ждут своего дропа" in msg


def test_drop_day_domain_outranks_the_cooldown_pool(sqlite_db, monkeypatch):
    """Ревью 2026-07-13, Important 1. Кулдаун вернул бездедлайновым доменам ПРАВО на скоринг, но
    без приоритета они отбирают у drop-day доменов ОЧЕРЕДЬ: RD есть только у backorder, у cctld/
    витрин он NULL, и при n=5 суточный пул (тысячи строк) вытеснял бы домен, дропнувшийся СЕГОДНЯ,
    не «поздно», а никогда. Срочность обязана быть первым ключом сортировки."""
    from datetime import datetime, timedelta, timezone
    from app.services import scoring
    import app.db as db
    from app.models.domain import Domain

    now = datetime.now(timezone.utc)
    with db.SessionLocal() as s:
        for i in range(6):                      # кулдаун-пул: без даты, давно не сверялись
            s.add(Domain(domain=f"pool{i}.ru", source="reg_ru", status="discovered", lane=None,
                         acquire_deadline=None,
                         acquirability_checked_at=now - timedelta(days=3)))
        s.add(Domain(domain="dropstoday.ru", source="cctld", status="discovered", lane=None,
                     acquire_deadline=now))     # дроп СЕГОДНЯ — его нельзя пропустить
        s.commit()

    seen = []
    monkeypatch.setattr(scoring, "score_domain", lambda did, *a, **kw: seen.append(did) or {})
    monkeypatch.setattr(scoring, "_make_clients", lambda: {})
    scoring.score_pending(limit=2)              # места мало — очередь решает всё

    with db.SessionLocal() as s:
        picked = [s.get(Domain, i).domain for i in seen]
    assert picked[0] == "dropstoday.ru", f"drop-day домен вытеснен кулдаун-пулом: {picked}"


def test_expired_drop_does_not_outrank_todays_drop(sqlite_db, monkeypatch):
    """Ревью 2026-07-13, финал. «Ближайший дедлайн» ASC — это САМАЯ РАННЯЯ дата, то есть
    ПРОТУХШИЙ дроп месячной давности. Он вставал в голову очереди перед сегодняшним и жёг на
    покойника полный дорогой путь (whois+РКН+Wayback ≈ 60 с); для lane='bid' воронка его даже
    не отбракует — T1 короткозамкнут лейном. Ярус срочности обязан идти раньше самой даты."""
    from datetime import datetime, timedelta, timezone
    from app.services import scoring
    import app.db as db
    from app.models.domain import Domain

    now = datetime.now(timezone.utc)
    with db.SessionLocal() as s:
        s.add(Domain(domain="expired.ru", source="backorder", status="discovered", lane="bid",
                     referring_domains=9999,                       # ещё и жирный — соблазн взять
                     acquire_deadline=now - timedelta(days=30)))   # дроп УПУЩЕН месяц назад
        s.add(Domain(domain="todays.ru", source="backorder", status="discovered", lane="bid",
                     referring_domains=10,
                     acquire_deadline=now))                        # дроп СЕГОДНЯ
        s.commit()

    seen = []
    monkeypatch.setattr(scoring, "score_domain", lambda did, *a, **kw: seen.append(did) or {})
    monkeypatch.setattr(scoring, "_make_clients", lambda: {})
    scoring.score_pending(limit=1)                                 # место ровно одно

    with db.SessionLocal() as s:
        picked = [s.get(Domain, i).domain for i in seen]
    assert picked == ["todays.ru"], f"упущенный дроп обогнал сегодняшний: {picked}"


def test_unresolved_reports_why_it_could_not_decide(sqlite_db):
    """Панель не должна угадывать причину сниффингом errors: ветка «whois ответил, но ответ не
    разобрали» (available=None) исключения не бросает и в errors ничего не пишет — а панель
    заявляла бы «домен занят», то есть факт, которого никто не устанавливал."""
    from app.services import scoring

    did = _mk(domain="murky.ru", lane=None, source="cctld", referring_domains=10)
    out = scoring.score_domain(did, _clients(whois={"available": None, "created": None},
                                             wayback=_Wayback()))
    assert out["unresolved"] is True and out["why"] == "whois_unclear"
    assert not out["errors"]              # исключения НЕ было — errors пуст, сниффинг слеп

    did2 = _mk(domain="down.ru", lane=None, source="cctld", referring_domains=10)
    out2 = scoring.score_domain(did2, _clients(whois_raises=True, wayback=_Wayback()))
    assert out2["why"] == "whois_failed"


def test_taken_undated_is_not_reported_as_unparsed_whois(sqlite_db):
    """Ревью 2026-07-13. Вердикт 'unknown' имеет ДВА источника: whois не разобран (available=None)
    и whois РАЗОБРАН («занят»), но дата дропа и лейн неизвестны. Склеив их, панель писала бы
    «ответ не разобран (формат TLD?)» про всю массу cctld/витрин (lane=NULL) — и оператор пошёл
    бы чинить несуществующую поломку парсинга A-Parser на .ru."""
    from app.services import scoring
    did = _mk(domain="undated.ru", lane=None, source="cctld", referring_domains=10)  # дедлайна нет
    out = scoring.score_domain(did, _clients(whois={"available": False, "created": None},
                                             wayback=_Wayback()))
    assert out["unresolved"] is True
    assert out["why"] == "taken_undated"      # занят — это УСТАНОВЛЕННЫЙ факт, а не «не разобрали»
