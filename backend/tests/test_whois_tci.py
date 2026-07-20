"""Прямой whois TCI (.RU/.РФ/.SU) — транспорт + маршрутизатор. Сеть замокана
(рубильник `_no_live_network` в conftest всё равно не пустил бы: сырой TCP TCI
падал бы громко на socket.getaddrinfo, а не тихо уезжал в интернет)."""
from datetime import date

from app.integrations.whois_tci import TciWhoisClient, _parse

_TAKEN = """% TCI Whois Service. Terms of use:

domain:        USEFULSCRIPT.RU
state:         REGISTERED, DELEGATED, VERIFIED
registrar:     REGRU-RU
created:       2011-06-17T08:12:35Z
paid-till:     2026-06-17T09:12:35Z
free-date:     2026-07-21
source:        TCI
"""
_FREE = ("% TCI Whois Service. Terms of use:\n\n"
         "No entries found for the selected source(s).\n")
_INVALID = "% TCI Whois Service. Terms of use:\n\nInvalid request.\n"


def test_parse_taken_domain():
    r = _parse(_TAKEN)
    assert r["available"] is False
    assert r["created"].year == 2011
    assert r["free_date"] == date(2026, 7, 21)


def test_parse_free_domain():
    r = _parse(_FREE)
    assert r["available"] is True
    assert r["created"] is None and r["free_date"] is None


def test_parse_invalid_request_is_unknown():
    """Invalid request — НЕ свободен и НЕ занят. Кириллический .рф без punycode
    отвечает именно так; трактовать его как свободный = утащить домен в выкуп."""
    assert _parse(_INVALID)["available"] is None


def test_parse_junk_is_unknown():
    assert _parse("")["available"] is None
    assert _parse("совершенно посторонний текст")["available"] is None


def test_handles_only_tci_zones():
    """example.com у TCI отвечает 'No entries found' — дословно как свободный .ru.
    Маршрутизация по зоне ОБЯЗАНА резать это до запроса (живая проба 2026-07-19)."""
    c = TciWhoisClient()
    assert c.handles("clara-c.ru") is True
    assert c.handles("ok.su") is True
    assert c.handles("xn--80aswg.xn--p1ai") is True
    assert c.handles("сайт.рф") is True          # нормализуется в punycode
    assert c.handles("example.com") is False
    assert c.handles("example.net") is False


def test_handles_rejects_empty_label():
    """MINOR (повторное ревью 2026-07-20): пустая метка перед точкой (".ru") проходила
    бы `handles` как валидный домен второго уровня — `_punycode` на "label empty"
    бросает UnicodeError, фолбэк отдаёт строку как есть, `split` даёт ['', 'ru'].
    Направление безопасное (TCI ответит 'Invalid request.' -> available=None), но
    стучаться в сеть с мусором незачем — режем на `handles()`."""
    c = TciWhoisClient()
    assert c.handles(".ru") is False
    assert c.handles(".su") is False


def test_handles_rejects_third_level_domains():
    """CRITICAL (ревью 2026-07-19): TCI обслуживает СТРОГО второй уровень. `endswith`
    по хвосту пропускал бы третий уровень в TCI — а там 'No entries found' дословно как
    у свободного .ru, хотя домен ЗАНЯТ (поддомен под чужой делегированной зоной). Живая
    проба: shop.com.ru/www.msk.ru -> 'No entries found'; сами com.ru/msk.ru/pp.ru/spb.ru
    — валидные домены ВТОРОГО уровня (полная запись) — их занижать до TLD-суффикса нельзя."""
    c = TciWhoisClient()
    assert c.handles("shop.com.ru") is False
    assert c.handles("www.msk.ru") is False
    assert c.handles("x.pp.ru") is False
    assert c.handles("y.spb.ru") is False
    assert c.handles("com.ru") is True     # сам по себе валидный домен второго уровня в зоне ru
    assert c.handles("msk.ru") is True
    assert c.handles("pp.ru") is True
    assert c.handles("spb.ru") is True


# --- маршрутизатор ---------------------------------------------------------------

class _FakeTci:
    def __init__(self, result=None, boom=False):
        self.result, self.boom, self.calls = result, boom, []

    def handles(self, domain):
        return TciWhoisClient().handles(domain)

    def probe(self, domain):
        self.calls.append(domain)
        if self.boom:
            raise OSError("соединение отклонено")
        return self.result


class _FakeAparser:
    def __init__(self):
        self.calls = []

    def whois_probe(self, domain):
        self.calls.append(domain)
        return {"available": False, "created": None}


def test_router_uses_tci_for_ru():
    from app.services import whois
    tci = _FakeTci({"available": True, "created": None, "free_date": None})
    ap = _FakeAparser()
    r = whois.probe("clara-c.ru", {"tci": tci, "aparser": ap})
    assert r["available"] is True
    assert r["whois_source"] == "tci"
    assert tci.calls == ["clara-c.ru"] and ap.calls == []


def test_router_uses_aparser_for_com():
    """Регресс на ловушку: .com НЕ должен уходить в TCI ни при каких условиях."""
    from app.services import whois
    tci = _FakeTci({"available": True, "created": None, "free_date": None})
    ap = _FakeAparser()
    r = whois.probe("example.com", {"tci": tci, "aparser": ap})
    assert r["whois_source"] == "aparser"
    assert tci.calls == [] and ap.calls == ["example.com"]


def test_router_falls_back_visibly_when_tci_fails():
    """Сбой TCI не глотается молча — фолбэк помечен в whois_source."""
    from app.services import whois
    tci, ap = _FakeTci(boom=True), _FakeAparser()
    r = whois.probe("clara-c.ru", {"tci": tci, "aparser": ap})
    assert r["whois_source"] == "aparser_fallback"
    assert ap.calls == ["clara-c.ru"]
    assert r["free_date"] is None


class _FlakyTci:
    """Фейк с настраиваемым исходом НА КАЖДЫЙ вызов (в отличие от `_FakeTci`, у
    которого `boom` фиксирован на весь фейк) — нужен тестам предохранителя, где
    сбой/успех чередуются по заранее заданному списку."""
    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.calls = []

    def handles(self, domain):
        return True

    def probe(self, domain):
        self.calls.append(domain)
        outcome = self._outcomes.pop(0)
        if outcome == "boom":
            raise OSError("соединение отклонено")
        return outcome


_OK = {"available": False, "created": None, "free_date": None}


def test_circuit_breaker_skips_tci_after_three_consecutive_failures():
    """IMPORTANT 1 (повторное ревью 2026-07-20): блэкхол на 43/tcp (пакеты дропаются,
    не connection refused — типично для фильтрации исходящего 43/tcp на боксе
    Windows+Docker Desktop) платил бы полным _TIMEOUT на КАЖДЫЙ .ru-домен и ВСЁ РАВНО
    уходил в A-Parser — на капе max_whois_per_run=200 это ~33 минуты чистого ожидания
    за прогон. После 3 сбоев ПОДРЯД предохранитель обязан сработать: четвёртый домен
    уходит в A-Parser МИНУЯ tci.probe() вообще (список исходов намеренно ровно из 3
    элементов — если бы предохранитель не сработал, 4-й вызов уронил бы тест
    IndexError'ом на пустом списке)."""
    from app.services import whois
    tci = _FlakyTci(["boom", "boom", "boom"])
    ap = _FakeAparser()
    clients = {"tci": tci, "aparser": ap}

    for i in range(3):
        r = whois.probe(f"fail{i}.ru", clients)
        assert r["whois_source"] == "aparser_fallback"
    assert len(tci.calls) == 3

    r = whois.probe("fourth.ru", clients)
    assert r["whois_source"] == "aparser_fallback"
    assert len(tci.calls) == 3     # предохранитель сработал — tci.probe() на 4-й домен НЕ звали
    assert ap.calls == ["fail0.ru", "fail1.ru", "fail2.ru", "fourth.ru"]


def test_circuit_breaker_resets_on_success_between_failures():
    """Успешный ответ TCI между сбоями сбрасывает счётчик — предохранитель не должен
    срабатывать раньше времени на череде "2 сбоя / успех / 2 сбоя" (всего 4 сбоя,
    но НИ РАЗУ не подряд 3)."""
    from app.services import whois
    tci = _FlakyTci(["boom", "boom", _OK, "boom", "boom"])
    ap = _FakeAparser()
    clients = {"tci": tci, "aparser": ap}

    assert whois.probe("a.ru", clients)["whois_source"] == "aparser_fallback"   # сбой 1
    assert whois.probe("b.ru", clients)["whois_source"] == "aparser_fallback"   # сбой 2
    assert whois.probe("c.ru", clients)["whois_source"] == "tci"                # успех -> сброс
    assert whois.probe("d.ru", clients)["whois_source"] == "aparser_fallback"   # сбой 1 (заново)
    assert whois.probe("e.ru", clients)["whois_source"] == "aparser_fallback"   # сбой 2 (заново)
    assert len(tci.calls) == 5     # предохранитель НЕ сработал — до 3 подряд не дошли ни разу


# --- воронка целиком: _funnel -> whois.probe -> TCI, без единого касания A-Parser ------------

class _FunnelWayback:
    """Чистая старая история — домен доживает до approved, но это не суть теста."""
    def classify_history(self, domain):
        return {"prior_flags": {c: False for c in ("adult", "pharma", "casino", "gambling", "spam")},
                "first_seen": None, "age_years": 9.0, "wayback_checked": True, "sampled": 5}


def _mk_ru_domain(**kw):
    import app.db as db
    from app.models.domain import Domain
    with db.SessionLocal() as s:
        d = Domain(domain=kw.pop("domain", "tcigood.ru"), source=kw.pop("source", "cctld"),
                   status="discovered", **kw)
        s.add(d); s.commit(); s.refresh(d)
        return d.id


def test_funnel_uses_tci_for_ru_without_touching_aparser():
    """IMPORTANT (ревью 2026-07-19): нулевое интеграционное покрытие связки
    `_funnel -> whois.probe -> TCI` — все фейки в остальных тестовых файлах ставят
    `handles -> False`. Опечатка в ключе "tci" (services/scoring.py::_make_clients)
    отключила бы маршрутизацию целиком, и сьют остался бы зелёным. Требование спеки
    §Тестирование: домен .ru проходит T1 БЕЗ единого обращения к A-Parser."""
    from datetime import datetime, timedelta, timezone
    import app.db as db
    from app.models.domain import Domain
    from app.services import scoring

    did = _mk_ru_domain(domain="tcigood.ru", referring_domains=3000, lane="bid")
    old = datetime.now(timezone.utc) - timedelta(days=365 * 9)
    tci = _FakeTci({"available": False, "created": old, "free_date": None})
    ap = _FakeAparser()
    clients = {
        "aparser": ap, "tci": tci,
        "rkn": type("R", (), {"is_listed": lambda self, d: False})(),
        "blacklist": type("B", (), {"is_blacklisted": lambda self, d: False})(),
        "searxng": type("S", (), {"indexed_echo": lambda self, d: True})(),
        "wayback": _FunnelWayback(),
    }
    out = scoring.score_domain(did, clients=clients)

    assert ap.calls == []                       # A-Parser не звали — TCI закрыл whois целиком
    assert tci.calls == ["tcigood.ru"]
    assert out["status"] in ("approved", "scored") and out["reject_reason"] is None

    with db.SessionLocal() as s:
        d = s.get(Domain, did)
    assert d.score_breakdown["whois_source"] == "tci"      # видно оператору после фикса Задачи 1


def test_funnel_whois_decides_acquirability_for_non_bid_domain():
    """MINOR 2 (повторное ревью 2026-07-20): тест выше идёт по lane="bid", где для
    bid-лейна приобретаемость берётся из ИСТОЧНИКА (backorder), а не из whois — T1
    короткозамкнут лейном (см. `_funnel`), ответ TCI до `acquirability_verdict` не
    доходит. Именно денежный путь, ради которого чинился CRITICAL прошлого ревью
    (TCI сказал available=True -> вердикт "free" -> домен в очередь выкупа), сквозным
    тестом не был закрыт. Здесь источник cctld БЕЗ лейна (discovery его не проставляет
    сырым источникам) — acquirability_verdict реально читает ответ TCI."""
    from datetime import datetime, timedelta, timezone
    import app.db as db
    from app.models.domain import Domain
    from app.services import scoring

    did = _mk_ru_domain(domain="tcifree.ru", referring_domains=3000)   # lane не задан (NULL)
    old = datetime.now(timezone.utc) - timedelta(days=365 * 9)
    tci = _FakeTci({"available": True, "created": old, "free_date": None})
    ap = _FakeAparser()
    clients = {
        "aparser": ap, "tci": tci,
        "rkn": type("R", (), {"is_listed": lambda self, d: False})(),
        "blacklist": type("B", (), {"is_blacklisted": lambda self, d: False})(),
        "searxng": type("S", (), {"indexed_echo": lambda self, d: True})(),
        "wayback": _FunnelWayback(),
    }
    out = scoring.score_domain(did, clients=clients)

    assert ap.calls == []                       # A-Parser не звали — TCI закрыл whois целиком
    assert tci.calls == ["tcifree.ru"]
    assert out["status"] in ("approved", "scored") and out["reject_reason"] is None

    with db.SessionLocal() as s:
        d = s.get(Domain, did)
    assert d.lane == "free"                                 # приобретаемость решил whois, не источник
    assert d.score_breakdown["whois_source"] == "tci"


# --- Задача 2: free-date заполняет пустой acquire_deadline -----------------------------------

def test_free_date_fills_missing_deadline_only():
    """free-date заполняет ПУСТОЙ acquire_deadline. Уже известный АКТУАЛЬНЫЙ дедлайн
    (из фида backorder — источник с ценой и лейном, ещё не просроченный) не трогается."""
    from datetime import datetime, timedelta, timezone
    from app.services import scoring

    now = datetime.now(timezone.utc)
    known = now + timedelta(days=12)             # дедлайн из фида, ещё не наступил
    fd = (now + timedelta(days=1)).date()
    for existing, expect in ((None, fd), (known, known.date())):
        got = scoring._deadline_from_whois(existing, fd, now, None)
        assert got.date() == expect


# --- финальное ревью 2026-07-20: находки 1/2/6 (протухшая проекция терминально хоронит домен) -

def test_deadline_from_whois_refreshes_stale_projection_forward():
    """IMPORTANT 1: протухшая проекция (дедлайн прошёл с запасом, домен всё ещё занят —
    владелец продлил) обновляется НОВЫМ free-date, если он дальше старого. Раньше
    `existing is not None` выбрасывал свежий ответ целиком, и вердикт судил по трупу
    даты (см. docstring _deadline_from_whois)."""
    from datetime import datetime, timedelta, timezone
    from app.services import scoring

    now = datetime.now(timezone.utc)
    stale = now - timedelta(days=30)                          # протух давно, с запасом DROP_GRACE
    fresh_fd = (now + timedelta(days=60)).date()
    got = scoring._deadline_from_whois(stale, fresh_fd, now, None)
    assert got.date() == fresh_fd


def test_deadline_from_whois_does_not_move_backward_or_within_grace():
    """Свежий free_date РАНЬШЕ старой (уже протухшей) даты не двигает дедлайн назад,
    а дедлайн, который ещё в пределах DROP_GRACE, не считается протухшим."""
    from datetime import datetime, timedelta, timezone
    from app.services import scoring

    now = datetime.now(timezone.utc)
    stale = now - timedelta(days=30)
    earlier_fd = (now - timedelta(days=40)).date()             # дальше в прошлом, чем stale
    assert scoring._deadline_from_whois(stale, earlier_fd, now, None) == stale

    within_grace = now - timedelta(hours=6)                    # прошёл, но ещё в пределах запаса
    later_fd = (now + timedelta(days=60)).date()
    assert scoring._deadline_from_whois(within_grace, later_fd, now, None) == within_grace


def test_deadline_from_whois_never_touches_bid_lane():
    """Регресс (денежный путь M2): для lane='bid' дедлайн из фида backorder не
    обновляется НИКОГДА, даже если он давно просрочен, а whois принёс свежий free-date
    вперёд — иначе перехваченный конкурентом домен получил бы free-date НОВОГО владельца
    и завис бы в пуле на год вместо честного not_acquirable."""
    from datetime import datetime, timedelta, timezone
    from app.services import scoring

    now = datetime.now(timezone.utc)
    stale = now - timedelta(days=30)
    fresh_fd = (now + timedelta(days=60)).date()
    assert scoring._deadline_from_whois(stale, fresh_fd, now, "bid") == stale


def test_deadline_from_whois_never_touches_free_lane():
    """Регресс (ревью 2026-07-20): lane='free' — второй законный терминал. whois уже
    подтвердил, что домен БЫЛ свободен; если он снова занят, значит его КУПИЛИ, и это
    честный not_acquirable, а не устаревшая проекция. Обновляя дедлайн, мы бы подставили
    free-date НОВОГО владельца, вердикт стал бы waiting, и перекупленный домен висел бы
    в инбоксе кандидатом к выкупу вечно. Целевая популяция находки — lane=NULL."""
    from datetime import datetime, timedelta, timezone
    from app.services import scoring

    now = datetime.now(timezone.utc)
    stale = now - timedelta(days=30)
    fresh_fd = (now + timedelta(days=60)).date()
    assert scoring._deadline_from_whois(stale, fresh_fd, now, "free") == stale
    # а для бездедлайнового пула (lane=NULL) — обновляется, это и есть смысл фикса
    assert scoring._deadline_from_whois(stale, fresh_fd, now, None).date() == fresh_fd


def test_funnel_fills_empty_deadline_from_tci_and_stays_waiting():
    """IMPORTANT 2: сквозной тест на _funnel (не только на чистый хелпер) — .ru-домен
    lane=NULL без дедлайна, TCI отвечает "занят" + free_date впереди. После score_domain
    дедлайн должен лечь в БД, а домен остаться discovered с unresolved_why='waiting'
    (а не 'taken_undated', как было бы без даты)."""
    from datetime import datetime, timedelta, timezone
    import app.db as db
    from app.models.domain import Domain
    from app.services import scoring

    did = _mk_ru_domain(domain="tciwait.ru", referring_domains=3000)   # lane=NULL, дедлайна нет
    fd = (datetime.now(timezone.utc) + timedelta(days=10)).date()
    tci = _FakeTci({"available": False, "created": None, "free_date": fd})
    ap = _FakeAparser()
    clients = {
        "aparser": ap, "tci": tci,
        "rkn": type("R", (), {"is_listed": lambda self, d: False})(),
        "blacklist": type("B", (), {"is_blacklisted": lambda self, d: False})(),
        "searxng": type("S", (), {"indexed_echo": lambda self, d: True})(),
        "wayback": _FunnelWayback(),
    }
    out = scoring.score_domain(did, clients=clients)

    assert out.get("unresolved") is True
    assert out.get("why") == "waiting"
    with db.SessionLocal() as s:
        d = s.get(Domain, did)
    assert d.status == "discovered"
    assert d.acquire_deadline is not None
    assert d.acquire_deadline.date() == fd
    # Дедлайн, заполненный из free-date, — ТАКАЯ ЖЕ проекция «освободится, если не
    # продлят», как и обновлённый: панель обязана подписать его иначе, чем дату из фида.
    # Домен при этом остаётся discovered (waiting), то есть идёт по пути unresolved,
    # где score_breakdown целиком не собирается — ключ кладётся точечно.
    assert d.score_breakdown["deadline_source"] == "whois_projection"


def test_funnel_refreshes_stale_deadline_and_does_not_reject_renewed_domain():
    """IMPORTANT 1 через полную воронку: домен с ПРОТУХШИМ дедлайном (из бездедлайнового
    пула — уже ждал свой дроп) на этом прогоне отвечает whois'ом "всё ещё занят" СО СВЕЖИМ
    free-date вперёд (владелец продлил). До фикса это терминально хоронило домен в
    rejected/not_acquirable; после — дедлайн обновляется, домен остаётся в discovered,
    ожидая уже НОВОГО срока."""
    from datetime import datetime, timedelta, timezone
    import app.db as db
    from app.models.domain import Domain
    from app.services import scoring

    now = datetime.now(timezone.utc)
    stale = now - timedelta(days=30)
    did = _mk_ru_domain(domain="tcirenewed.ru", referring_domains=3000, acquire_deadline=stale)
    fresh_fd = (now + timedelta(days=60)).date()
    tci = _FakeTci({"available": False, "created": None, "free_date": fresh_fd})
    ap = _FakeAparser()
    clients = {
        "aparser": ap, "tci": tci,
        "rkn": type("R", (), {"is_listed": lambda self, d: False})(),
        "blacklist": type("B", (), {"is_blacklisted": lambda self, d: False})(),
        "searxng": type("S", (), {"indexed_echo": lambda self, d: True})(),
        "wayback": _FunnelWayback(),
    }
    out = scoring.score_domain(did, clients=clients)

    assert out.get("unresolved") is True
    assert out.get("why") == "waiting"                # НЕ not_acquirable/rejected
    with db.SessionLocal() as s:
        d = s.get(Domain, did)
    assert d.status == "discovered"
    assert d.reject_reason is None
    assert d.acquire_deadline.date() == fresh_fd       # проекция сдвинулась вперёд
    assert d.score_breakdown["deadline_source"] == "whois_projection"
