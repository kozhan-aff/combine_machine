"""Воронка скоринга дёшево→дорого: ранний выход, reject_reason, дорогой Wayback только для выживших."""
from datetime import datetime, timezone, timedelta
import app.db as db
from app.models.domain import Domain
from app.services import scoring


def _mk(**kw):
    with db.SessionLocal() as s:
        d = Domain(domain=kw.pop("domain", "x.ru"), source="cctld", status="discovered", **kw)
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
            "wayback": wayback, "opr": None}


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
            "wayback": wb, "opr": None}


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


def test_raw_registered_rejects_not_acquirable_before_wayback(monkeypatch, sqlite_db):
    """Сырой домен, whois=занят → not_acquirable, Wayback НЕ вызывался."""
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
    assert out["status"] == "rejected" and out["reject_reason"] == "not_acquirable"
    assert wb.calls == 0


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
