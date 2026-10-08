"""Воронка скоринга дёшево→дорого: ранний выход, reject_reason, дорогой Wayback только для выживших."""
from datetime import datetime, timezone, timedelta
import app.db as db
from app.models.domain import Domain
from app.services import scoring


def _mk(**kw):
    with db.SessionLocal() as s:
        d = Domain(domain=kw.pop("domain", "x.com"), source=kw.pop("source", "cctld"),
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


def _clients(whois_dt=None, wayback=None, bl=False, whois=None, whois_raises=False):
    """whois: dict {"available":..., "created":...} (новый формат, приобретаемость известна
    явно). whois_dt: старый позиционный аргумент (только дата) — оборачивается в
    {"available": False, "created": whois_dt} (занят, но с датой регистрации — для тестов,
    доходящих до W3+ через lane="bid" на тестовом Domain). whois_raises=True — whois_probe
    бросает (недоступен). bl — ответ Spamhaus (в воронке зовётся только с DQS-ключом)."""
    pr = whois if whois is not None else {"available": False, "created": whois_dt}
    class _W:  # aparser
        def whois_probe(self, dom):
            if whois_raises:
                raise RuntimeError("whois timeout")
            return pr
    class _B:
        def is_blacklisted(self, dom): return bl
    return {"aparser": _W(), "blacklist": _B(),
            "wayback": wayback,
            "webrisk": type("WR", (), {"configured": True, "threats": lambda self, d: []})(),
            "ahrefs": type("Ah", (), {"units_left": lambda self: 2_000_000,
                                      "batch": lambda self, ds: {d: {} for d in ds},
                                      "anchors": lambda self, d, limit=50: [
                                          {"anchor": d, "refdomains": 10, "is_spam": False}],
                                      "metrics_history": lambda self, d, years=5, today=None: []})()}


def _clients_whois_raises(wb, bl=False):
    """Как _clients, но whois_probe падает (недоступен) — для Finding-1 фолбэка."""
    class _W:  # aparser
        def whois_probe(self, dom): raise RuntimeError("whois timeout")
    class _B:
        def is_blacklisted(self, dom): return bl
    return {"aparser": _W(), "blacklist": _B(),
            "wayback": wb,
            "webrisk": type("WR", (), {"configured": True, "threats": lambda self, d: []})(),
            "ahrefs": type("Ah", (), {"units_left": lambda self: 2_000_000,
                                      "batch": lambda self, ds: {d: {} for d in ds}})()}


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


def test_too_young_rejects_in_history_wave_by_the_older_date():
    """Р5: W2 возраст только записывает — Wayback зовётся и для молодого по RDAP/whois домена:
    у перехваченного домена это дата ПОСЛЕДНЕЙ регистрации. Отказ too_young — в волне истории и
    только если молоды ОБЕ даты; молодая регистрация при старом архиве — не отказ."""
    young = datetime.now(timezone.utc) - timedelta(days=365)   # 1 год
    did = _mk(domain="young.com", referring_domains=5, lane="bid")
    wb = _Wayback(age_years=1.0)
    out = scoring.score_domain(did, clients=_clients(young, wb))
    assert out["status"] == "rejected" and out["reject_reason"] == "too_young"
    assert wb.calls == 1
    did = _mk(domain="recaught.com", referring_domains=5, lane="bid")
    out = scoring.score_domain(did, clients=_clients(young, _Wayback(age_years=9.0)))
    assert out["reject_reason"] is None and out["status"] == "scored"
    with db.SessionLocal() as s:
        d = s.get(Domain, did)
    assert float(d.age_years) == 9.0 and d.score_breakdown["age_source"] == "wayback"


def test_feed_flag_rejects_first():
    did = _mk(domain="blocked.com", referring_domains=50, feed_flags={"rkn": True})
    wb = _Wayback()
    out = scoring.score_domain(did, clients=_clients(None, wb))
    assert out["reject_reason"] == "feed_flag" and wb.calls == 0


def test_low_rd_rejects():
    """RD судит W4 по ответу Ahrefs (v2), до дорогой истории. Лейн bid: без лейна домен ушёл
    в unresolved ещё на W2 (whois «занят», даты дропа нет)."""
    did = _mk(domain="thin.com", referring_domains=0, lane="bid")
    wb = _Wayback()
    from app.services import settings as st
    st.update_settings(min_referring_domains=1)
    thin = type("Ah", (), {"units_left": lambda self: 2_000_000,
                           "batch": lambda self, ds: {d: {"refdomains": 0} for d in ds}})()
    out = scoring.score_domain(did, clients={**_clients(None, wb), "ahrefs": thin})
    assert out["reject_reason"] == "low_rd" and wb.calls == 0


def test_whois_none_falls_through_to_wayback_age():
    did = _mk(domain="nowhois.com", referring_domains=3000, lane="bid")
    wb = _Wayback()
    out = scoring.score_domain(did, clients=_clients(None, wb))   # whois не отдал дату
    assert wb.calls == 1                                          # дошли до T3
    assert out["status"] == "scored"                             # чистый сильный домен
    with db.SessionLocal() as s:
        d = s.get(Domain, did)
    assert float(d.age_years) == 9.0                             # возраст — фолбэком из Wayback


def test_clean_strong_domain_is_scored_and_bulk_ok():
    """Чистый сильный домен: скоринг ставит максимум `scored` (одобряет только человек, Р2), а
    без единой дыры в проверках пакет его берёт."""
    did = _mk(domain="good.com", referring_domains=3000, lane="bid")
    wb = _Wayback()
    old = datetime.now(timezone.utc) - timedelta(days=365 * 9)
    out = scoring.score_domain(did, clients=_clients(old, wb))
    assert wb.calls == 1 and out["status"] == "scored" and out["reject_reason"] is None
    with db.SessionLocal() as s:
        assert scoring.bulk_ok(s.get(Domain, did)) is True


def test_blacklist_rejects_before_wayback(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "SPAMHAUS_DQS_KEY", "k")       # Spamhaus в воронке — только с DQS
    did = _mk(domain="blacklisted.com", referring_domains=50, lane="bid")
    wb = _Wayback()
    old = datetime.now(timezone.utc) - timedelta(days=365 * 8)
    out = scoring.score_domain(did, clients=_clients(old, wb, bl=True))
    assert out["status"] == "rejected" and out["reject_reason"] == "blacklist"
    assert wb.calls == 0            # blacklist — W3, Wayback до неё не доходит


def test_blacklist_none_downgrades_via_funnel(monkeypatch):
    """Ревью C2: строка `blacklisted is None -> errors.append("blacklist:unavailable")` прогнана
    полной воронкой на иначе-сильном домене (профиль test_clean_strong_domain_is_scored_and_bulk_ok).
    Авто-одобрения нет (Р2), поэтому «понижение» теперь значит: домен `scored`, с пометкой
    «вслепую» и ВНЕ пакета. Spamhaus в воронке — только с DQS-ключом (v2), поэтому ключ задан."""
    from app.config import settings
    from app.services import scoring_config as cfg
    monkeypatch.setattr(settings, "SPAMHAUS_DQS_KEY", "k")
    did = _mk(domain="bl-none.com", referring_domains=3000, lane="bid")
    wb = _Wayback()
    old = datetime.now(timezone.utc) - timedelta(days=365 * 9)
    out = scoring.score_domain(did, clients=_clients(old, wb, bl=None))
    assert "blacklist:unavailable" in out["errors"]
    assert out["score"] >= cfg.DECISION["approve_at"]      # сильный — исключает правило, а не балл
    assert out["status"] == "scored"                        # не rejected — не hard-reject
    assert wb.calls == 1                                    # blacklist:unavailable не блокирует T3
    with db.SessionLocal() as s:
        d = s.get(Domain, did)
        assert scoring.blind_reason(d) == "блэклист НЕ проверен" and scoring.bulk_ok(d) is False


def test_history_dirty_rejects_after_wayback():
    did = _mk(domain="dirtyhist.com", referring_domains=50, lane="bid")
    wb = _WaybackDirty()
    old = datetime.now(timezone.utc) - timedelta(days=365 * 8)   # T0-T2 пройдены
    out = scoring.score_domain(did, clients=_clients(old, wb))
    assert out["status"] == "rejected" and out["reject_reason"] == "history_dirty"
    assert wb.calls == 1            # дошли до T3 — там и отклонились


def test_low_score_reject():
    did = _mk(domain="weak.com", referring_domains=1, lane="bid", dr=0)   # DR 0 известен: не «нет данных» (0.5)
    wb = _WaybackWeak()
    old_enough = datetime.now(timezone.utc) - timedelta(days=1150)   # ~3.15 года, чуть старше порога
    out = scoring.score_domain(did, clients=_clients(old_enough, wb))
    assert out["status"] == "rejected" and out["reject_reason"] == "low_score"
    assert wb.calls == 1            # дошли до compute_score — отклонил composite score, не воронка


def test_runtime_approve_at_never_makes_scoring_approve():
    """Р2: `approve_at` больше не участвует в решении скоринга — это «порог сильного кандидата»
    для превью и пакета. Даже порог на самом дне (клампится к manual_review_at) не даёт машине
    поставить `approved`: одобряет только человек."""
    from app.services import settings as st
    did = _mk(domain="runtime-approve.com", referring_domains=100, lane="bid")
    wb = _Wayback()
    old = datetime.now(timezone.utc) - timedelta(days=365 * 9)
    st.update_settings(approve_at=0.0)
    out = scoring.score_domain(did, clients=_clients(old, wb))
    assert out["score"] > st.get_settings()["approve_at"]
    assert out["status"] == "scored" and out["reject_reason"] is None


def test_runtime_thresholds_can_reject_previously_approved_score():
    """Тот же сильный домен: подняв ОБА порога выше его score, получаем rejected/low_score —
    не «застрявший approved» из статических cfg.DECISION."""
    from app.services import settings as st
    did = _mk(domain="runtime-reject.com", referring_domains=100, lane="bid")
    wb = _Wayback()
    old = datetime.now(timezone.utc) - timedelta(days=365 * 9)
    st.update_settings(manual_review_at=0.9, approve_at=0.95)
    out = scoring.score_domain(did, clients=_clients(old, wb))
    assert out["status"] == "rejected" and out["reject_reason"] == "low_score"


def test_runtime_min_age_years_rejects_too_young():
    """Spec §G: рантайм min_age_years из /settings — 4-летний (и по whois, и по архиву) домен
    отклоняется too_young при поднятом пороге в 5 лет. Гейт — в волне истории (Р5)."""
    from app.services import settings as st
    did = _mk(domain="four-years.com", referring_domains=50, lane="bid")
    wb = _Wayback(age_years=4.0)
    st.update_settings(min_age_years=5.0)
    four_years = datetime.now(timezone.utc) - timedelta(days=365 * 4)
    out = scoring.score_domain(did, clients=_clients(four_years, wb))
    assert out["status"] == "rejected" and out["reject_reason"] == "too_young"
    assert wb.calls == 1                          # Р5: возраст судит волна истории по старшей дате


def test_too_young_fallback_from_wayback_when_whois_fails():
    """Finding 1: whois упал (T1 без даты) -> возраст добираем из Wayback (T3); если
    фолбэк-возраст < порога — reject too_young, а не тихий проскок в compute_score."""
    did = _mk(domain="whoisdown.com", referring_domains=50, lane="bid")
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
        s.add(Domain(domain="taken.com", source="cctld", status="discovered", lane=None,
                     referring_domains=None)); s.commit()
        did = s.execute(_id_of("taken.com")).scalar_one()
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
        s.add(Domain(domain="free.com", source="reg_ru", status="discovered", lane=None)); s.commit()
        did = s.execute(_id_of("free.com")).scalar_one()
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
        s.add(Domain(domain="oops.com", source="cctld", status="discovered", lane=None)); s.commit()
        did = s.execute(_id_of("oops.com")).scalar_one()
    out = scoring.score_domain(did, clients)
    assert out.get("unresolved") is True and wb.calls == 0
    with db.SessionLocal() as s:
        assert s.get(Domain, did).status == "discovered"      # не сдвинулся


def test_raw_source_future_deadline_stays_discovered():
    # сырой домен, whois «занят», но дедлайн дропа в будущем -> ждём дропа, не reject
    future = datetime.now(timezone.utc) + timedelta(days=5)
    did = _mk(domain="dropping.com", lane=None, source="cctld",
              referring_domains=10, acquire_deadline=future)
    wb = _Wayback()
    out = scoring.score_domain(did, _clients(whois={"available": False, "created": None}, wayback=wb))
    assert out.get("unresolved") is True
    assert out["status"] == "discovered"
    assert wb.calls == 0                      # дорогой Wayback не тронут


def test_raw_source_no_deadline_is_not_rejected():
    """Парная регрессия к тесту выше: без дедлайна и без лейна домен НЕ выбрасывается.
    Занятость сырого домена до дропа — норма, а не приговор (дебаг 2026-07-13)."""
    did = _mk(domain="taken.com", lane=None, source="cctld", referring_domains=10)
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
    did = _mk(domain="expired.com", lane=None, source="cctld",
              referring_domains=10, acquire_deadline=past)
    wb = _Wayback()
    out = scoring.score_domain(did, _clients(whois={"available": False, "created": None}, wayback=wb))
    assert out["status"] == "rejected" and out["reject_reason"] == "not_acquirable"
    assert wb.calls == 0


def test_raw_source_sniped_after_drop_is_not_acquirable_not_too_young():
    """Баг из аудита 2026-07-18 (S1): сырой домен дропнулся и тут же перехвачен снайпером —
    whois отвечает данными НОВОГО владельца (available=False, created=пару дней назад).
    Дедлайн дропа уже прошёл (за пределами DROP_GRACE) -> вердикт «занят» первичен.

    БЫЛО: возраст (несколько дней, << min_age_years) проверялся ДО вердикта приобретаемости
    и возвращал too_young раньше, чем код успевал дойти до not_acquirable — оператор видел
    «режет порог», шёл ослаблять min_age_years в /settings, и это ничего не спасало: домен
    был не наш, дело не в пороге. СТАЛО: для не-bid лейна вердикт приобретаемости считается
    раньше возраста; taken -> not_acquirable сразу, возраст для этого случая не смотрим."""
    past_deadline = datetime.now(timezone.utc) - timedelta(days=5)
    recent_created = datetime.now(timezone.utc) - timedelta(days=2)   # снайпер зарегистрировал только что
    did = _mk(domain="sniped.com", lane=None, source="cctld",
              referring_domains=10, acquire_deadline=past_deadline)
    wb = _Wayback()
    out = scoring.score_domain(
        did, _clients(whois={"available": False, "created": recent_created}, wayback=wb))
    assert out["status"] == "rejected" and out["reject_reason"] == "not_acquirable"
    assert wb.calls == 0                      # not_acquirable — ранний выход, дорогой Wayback не тронут


def test_drop_day_deadline_is_not_rejected():
    """Дедлайн = 00:00 СЕГОДНЯШНЕГО дня (именно так фид отдаёт delete_date — датой без
    времени), домен ещё занят: реестр освободит его в течение дня. Отбраковать здесь =
    выбросить дроп ровно в тот день, когда его можно ловить."""
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    did = _mk(domain="dropping-today.com", lane=None, source="cctld",
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
        s.add_all([Domain(domain=f"r{i}.com", source="cctld", status="discovered", lane=None,
                          referring_domains=None) for i in range(2)]); s.commit()
    scoring.score_pending(limit=10)
    with db.SessionLocal() as s:
        still = s.execute(_count_discovered()).scalar()
    assert still == 1                                          # один не обработан (бюджет исчерпан)


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
        s.add(Domain(domain="waits.com", source="cctld", status="discovered", lane=None,
                     acquire_deadline=future))                      # дроп впереди -> не берём
        s.add(Domain(domain="today.com", source="cctld", status="discovered", lane=None,
                     acquire_deadline=datetime.now(timezone.utc)))  # дроп настал -> берём
        s.add(Domain(domain="bid.com", source="backorder", status="discovered", lane="bid",
                     referring_domains=50, acquire_deadline=future))  # bid -> берём всегда
        s.commit()

    seen = []
    monkeypatch.setattr(scoring, "_run_waves",
                        lambda states, *a, **kw: seen.extend(s.domain_id for s in states) or [])
    monkeypatch.setattr(scoring, "_make_clients", lambda: {})
    scoring.score_pending(limit=50)

    with db.SessionLocal() as s:
        picked = {s.get(Domain, i).domain for i in seen}
    assert picked == {"today.com", "bid.com"}, f"взяли лишнее/потеряли нужное: {picked}"


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
        s.add(Domain(domain="tomorrow.com", source="cctld", status="discovered", lane=None,
                     acquire_deadline=now + timedelta(days=1)))    # дроп ЗАВТРА — ещё занят
        s.commit()

    seen = []
    monkeypatch.setattr(scoring, "_run_waves",
                        lambda states, *a, **kw: seen.extend(s.domain_id for s in states) or [])
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
        s.add(Domain(domain="just-dropped.com", source="cctld", status="discovered", lane=None,
                     acquire_deadline=now - timedelta(hours=3)))   # дроп уже случился
        s.commit()

    seen = []
    monkeypatch.setattr(scoring, "_run_waves",
                        lambda states, *a, **kw: seen.extend(s.domain_id for s in states) or [])
    monkeypatch.setattr(scoring, "_make_clients", lambda: {})
    scoring.score_pending(limit=50)

    with db.SessionLocal() as s:
        picked = {s.get(Domain, i).domain for i in seen}
    assert picked == {"just-dropped.com"}, f"созревший дроп должен уйти в скоринг: {picked}"


def test_unresolved_domain_remembers_it_was_checked(sqlite_db):
    """Whois ОТВЕТИЛ («занят», дроп впереди) — ответ детерминированный, завтра будет тот же.
    Факт сверки обязан осесть в БД, иначе следующий прогон платит за него заново."""
    from datetime import datetime, timedelta, timezone
    from app.services import scoring
    import app.db as db
    from app.models.domain import Domain

    future = datetime.now(timezone.utc) + timedelta(days=10)
    did = _mk(domain="waits.com", lane=None, source="cctld", referring_domains=10,
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
        s.add(Domain(domain="fresh.com", source="reg_ru", status="discovered", lane=None,
                     acquire_deadline=None, acquirability_checked_at=None))       # ни разу
        s.add(Domain(domain="cooled.com", source="reg_ru", status="discovered", lane=None,
                     acquire_deadline=None,
                     acquirability_checked_at=now - scoring.RECHECK_EVERY - timedelta(hours=1)))
        s.add(Domain(domain="justnow.com", source="sweb", status="discovered", lane=None,
                     acquire_deadline=None,
                     acquirability_checked_at=now - timedelta(minutes=5)))        # только что
        s.commit()

    seen = []
    monkeypatch.setattr(scoring, "_run_waves",
                        lambda states, *a, **kw: seen.extend(s.domain_id for s in states) or [])
    monkeypatch.setattr(scoring, "_make_clients", lambda: {})
    scoring.score_pending(limit=50)
    with db.SessionLocal() as s:
        picked = {s.get(Domain, i).domain for i in seen}
    # свежий и остывший — берём (вдруг дроп уже случился); только что спрошенный — нет
    assert picked == {"fresh.com", "cooled.com"}, picked


def test_empty_score_run_explains_why(sqlite_db, monkeypatch):
    """Пустой прогон Score теперь ШТАТЕН (все ждут дропа) и обязан назвать причину — тот же
    стандарт, что уже применён к перепроверке."""
    from datetime import datetime, timedelta, timezone
    from app.services import jobs, scoring
    import app.db as db
    from app.models.domain import Domain

    future = datetime.now(timezone.utc) + timedelta(days=9)
    with db.SessionLocal() as s:
        s.add(Domain(domain="waits.com", source="cctld", status="discovered", lane=None,
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
            s.add(Domain(domain=f"pool{i}.com", source="reg_ru", status="discovered", lane=None,
                         acquire_deadline=None,
                         acquirability_checked_at=now - timedelta(days=3)))
        s.add(Domain(domain="dropstoday.com", source="cctld", status="discovered", lane=None,
                     acquire_deadline=now))     # дроп СЕГОДНЯ — его нельзя пропустить
        s.commit()

    seen = []
    monkeypatch.setattr(scoring, "_run_waves",
                        lambda states, *a, **kw: seen.extend(s.domain_id for s in states) or [])
    monkeypatch.setattr(scoring, "_make_clients", lambda: {})
    scoring.score_pending(limit=2)              # места мало — очередь решает всё

    with db.SessionLocal() as s:
        picked = [s.get(Domain, i).domain for i in seen]
    assert picked[0] == "dropstoday.com", f"drop-day домен вытеснен кулдаун-пулом: {picked}"


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
        s.add(Domain(domain="expired.com", source="backorder", status="discovered", lane="bid",
                     referring_domains=9999,                       # ещё и жирный — соблазн взять
                     acquire_deadline=now - timedelta(days=30)))   # дроп УПУЩЕН месяц назад
        s.add(Domain(domain="todays.com", source="backorder", status="discovered", lane="bid",
                     referring_domains=10,
                     acquire_deadline=now))                        # дроп СЕГОДНЯ
        s.commit()

    seen = []
    monkeypatch.setattr(scoring, "_run_waves",
                        lambda states, *a, **kw: seen.extend(s.domain_id for s in states) or [])
    monkeypatch.setattr(scoring, "_make_clients", lambda: {})
    scoring.score_pending(limit=1)                                 # место ровно одно

    with db.SessionLocal() as s:
        picked = [s.get(Domain, i).domain for i in seen]
    assert picked == ["todays.com"], f"упущенный дроп обогнал сегодняшний: {picked}"


def test_unresolved_reports_why_it_could_not_decide(sqlite_db):
    """Панель не должна угадывать причину сниффингом errors: ветка «whois ответил, но ответ не
    разобрали» (available=None) исключения не бросает и в errors ничего не пишет — а панель
    заявляла бы «домен занят», то есть факт, которого никто не устанавливал."""
    from app.services import scoring

    did = _mk(domain="murky.com", lane=None, source="cctld", referring_domains=10)
    out = scoring.score_domain(did, _clients(whois={"available": None, "created": None},
                                             wayback=_Wayback()))
    assert out["unresolved"] is True and out["why"] == "whois_unclear"
    assert not out["errors"]              # исключения НЕ было — errors пуст, сниффинг слеп

    did2 = _mk(domain="down.com", lane=None, source="cctld", referring_domains=10)
    out2 = scoring.score_domain(did2, _clients(whois_raises=True, wayback=_Wayback()))
    assert out2["why"] == "whois_failed"


def test_taken_undated_is_not_reported_as_unparsed_whois(sqlite_db):
    """Ревью 2026-07-13. Вердикт 'unknown' имеет ДВА источника: whois не разобран (available=None)
    и whois РАЗОБРАН («занят»), но дата дропа и лейн неизвестны. Склеив их, панель писала бы
    «ответ не разобран (формат TLD?)» про всю массу cctld/витрин (lane=NULL) — и оператор пошёл
    бы чинить несуществующую поломку парсинга A-Parser на .ru."""
    from app.services import scoring
    did = _mk(domain="undated.com", lane=None, source="cctld", referring_domains=10)  # дедлайна нет
    out = scoring.score_domain(did, _clients(whois={"available": False, "created": None},
                                             wayback=_Wayback()))
    assert out["unresolved"] is True
    assert out["why"] == "taken_undated"      # занят — это УСТАНОВЛЕННЫЙ факт, а не «не разобрали»


def test_wayback_down_is_explained_in_job_message(monkeypatch):
    """archive.org лёг: домены уходят unresolved, а в сообщении задачи — причина (не тишина)."""
    from types import SimpleNamespace
    from app.services import jobs, scoring
    for name in ("_wave_t0", "_paid_gate", "_wave_avail", "_wave_risk", "_wave_lists", "_wave_ranks", "_wave_probe",
                 "_wave_links", "_persist_links", "_wave_deep", "_checkpoint"):
        monkeypatch.setattr(scoring, name, lambda *a, **k: [])

    def _history(states, *a, **k):
        for s in states:
            s.unresolved_why, s.alive = "wayback_down", False
    monkeypatch.setattr(scoring, "_wave_history", _history)
    monkeypatch.setattr(scoring, "_commit_result", lambda s, run, st: {})
    monkeypatch.setattr(jobs, "cancelled", lambda run: False)
    msgs = []
    monkeypatch.setattr(jobs, "report", lambda run, **kw: msgs.append(kw.get("message")))
    states = [SimpleNamespace(alive=True, unresolved_why=None) for _ in range(3)]
    scoring._run_waves(states, {}, {}, None, None, object(), notes=[])
    assert any(m and "archive.org недоступен — 3 доменов ждут следующего прогона" in m for m in msgs)
