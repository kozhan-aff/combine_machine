"""Грязь не доезжает до кассы (аудит F9+F13).

Коридор, который эти тесты закрывают, был сквозным и открытым: РКН-домен → кнопка «↩ вернуть
в approved» (панель предлагала её и для грязи) → «Готовы к выкупу» БЕЗ метки → очередь выкупа
БЕЗ метки → ставка → покупка. `reject_reason` при этом даже не стирался — он просто нигде не
показывался там, где решают о деньгах.

Каждая регрессия ниже ПРОХОДИЛА на c989674 (то есть машина реально пускала грязь к деньгам).
"""
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

import app.db as db
from app.models.domain import Domain, AcquisitionOrder


def _add(**kw) -> int:
    """Домен в БД. kw — ровно те поля, которые пишет воронка (scoring.score_domain)."""
    with db.SessionLocal() as s:
        d = Domain(source="backorder", **kw)
        s.add(d)
        s.commit()
        s.refresh(d)
        return d.id


def _status(did: int) -> str:
    with db.SessionLocal() as s:
        return s.get(Domain, did).status


# --- политика без БД: чистая, ORM ей не нужен ----------------------------------

def test_dirty_reason_sees_verdict_and_raw_signals():
    """Грязь читается и из вердикта (`reject_reason`), и из сырых сигналов.

    Одного `reject_reason` мало: ручная реабилитация его НЕ стирала (домен уезжал в approved
    с живым «rkn» на борту), а значит поле и колонки могут разъехаться — на живой базе уже
    разъехались. Порог («низкий скор») грязью НЕ является: такой домен возвращают руками.
    """
    from types import SimpleNamespace as NS
    from app.services.transitions import dirty_reason

    def d(**kw):
        return NS(**{"domain": "x.ru", "status": "rejected", "reject_reason": None,
                     "rkn_listed": None, "blacklisted": None, "prior_flags": {},
                     "wayback_checked": True, **kw})

    assert dirty_reason(d(reject_reason="rkn")) == "rkn"
    assert dirty_reason(d(reject_reason="history_dirty")) == "history_dirty"
    assert dirty_reason(d(reject_reason="feed_flag")) == "feed_flag"
    assert dirty_reason(d(rkn_listed=True)) == "rkn"              # вердикт стёрли, сигнал остался
    assert dirty_reason(d(blacklisted=True)) == "blacklist"
    assert dirty_reason(d(prior_flags={"casino": True})) == "history_dirty"
    # НЕ грязь: порог крутится на /settings, «занят» — чужая покупка, «не проверяли» — не факт
    assert dirty_reason(d(reject_reason="low_score")) is None
    assert dirty_reason(d(reject_reason="too_young")) is None
    assert dirty_reason(d(reject_reason="not_acquirable")) is None
    assert dirty_reason(d(blacklisted=None)) is None


def test_manual_transition_checks_source_status_not_only_target():
    """`set_status_action` смотрел ТОЛЬКО целевой статус — отсюда и весь коридор."""
    from types import SimpleNamespace as NS
    from app.services.transitions import TransitionDenied, check

    def d(status, **kw):
        return NS(**{"domain": "x.ru", "status": status, "reject_reason": None,
                     "rkn_listed": None, "blacklisted": None, "prior_flags": {},
                     "wayback_checked": True, **kw})

    check(d("scored"), "approved")                     # гейт курации — законный переход
    check(d("approved"), "purchased")                    # «купил руками» — денежный гейт человека
    with pytest.raises(TransitionDenied):
        check(d("discovered"), "purchased")              # сырьё мимо воронки не покупают
    with pytest.raises(TransitionDenied):
        check(d("purchased"), "approved")                # деньги потрачены — назад не отматывают
    with pytest.raises(TransitionDenied):
        check(d("purchasing"), "rejected")               # домен держит живой заказ — это M2


# --- РЕГРЕССИЯ 1: «↩ вернуть в approved» для грязи ------------------------------

def test_rkn_domain_cannot_be_returned_to_approved(client):
    """ПРОХОДИЛО на c989674: rejected/rkn -> POST set-status approved -> домен в «Готовы к выкупу».

    Это первый шаг коридора: дальше домен неотличим от честно одобренного.
    """
    did = _add(domain="rkn.ru", status="rejected", reject_reason="rkn", rkn_listed=True)
    r = client.post(f"/domains/{did}/set-status", data={"status": "approved"},
                    follow_redirects=False)
    assert r.status_code == 303                          # панель отвечает флэшем, а не 500
    assert _status(did) == "rejected"                    # решение воронки не затёрто


def test_history_dirty_domain_cannot_be_returned_to_approved(client):
    did = _add(domain="casino.ru", status="rejected", reject_reason="history_dirty",
               prior_flags={"casino": True}, wayback_checked=True)
    client.post(f"/domains/{did}/set-status", data={"status": "approved"}, follow_redirects=False)
    assert _status(did) == "rejected"


def test_threshold_reject_is_still_returnable(client):
    """ЧТО ЛОМАЕТСЯ, когда запрет срабатывает: НИЧЕГО у законно отклонённых.

    Домен, отсеянный ПОРОГОМ (низкий скор), — не грязь: порог крутится на /settings, и вернуть
    такой домен в оборот руками оператор вправе. Запрет, который заодно запер бы и его, был бы
    не фиксом, а новой поломкой.
    """
    did = _add(domain="weak.ru", status="rejected", reject_reason="low_score", score=0.35)
    client.post(f"/domains/{did}/set-status", data={"status": "approved"}, follow_redirects=False)
    assert _status(did) == "approved"


def test_rescoring_is_the_honest_way_back(monkeypatch):
    """Дверь для грязи не заперта наглухо — она просто не открывается КНОПКОЙ.

    Единственный путь обратно в оборот — перескор: воронка берёт домены из `rejected`, и если
    проверки сегодня говорят «чист», она сама чистит `reject_reason` и сигналы. Реабилитацию
    даёт машина по новым уликам, а не человек по настроению.
    """
    from app.services import scoring
    from app.services.transitions import dirty_reason

    did = _add(domain="unblocked.ru", status="rejected", reject_reason="rkn", rkn_listed=True,
               lane="bid", referring_domains=300)

    class _WB:
        def classify_history(self, dom):
            return {"prior_flags": {}, "wayback_checked": True, "sampled": 3,
                    "evidence": [{"url": dom, "timestamp": "20150101", "cats": [], "chars": 900}],
                    "first_seen": None, "age_years": None}
    clients = {
        "wayback": _WB(),
        "rkn": type("R", (), {"is_listed": lambda self, d: False})(),        # РКН разблокировал
        "blacklist": type("B", (), {"is_blacklisted": lambda self, d: False})(),
        "searxng": type("S", (), {"indexed_echo": lambda self, d: True})(),
        "aparser": type("A", (), {"whois_probe": lambda self, d: {
            "available": False, "created": datetime(2008, 1, 1, tzinfo=timezone.utc)}})(),
    }
    out = scoring.score_domain(did, clients=clients)
    assert out["reject_reason"] is None and out["status"] in ("approved", "scored")
    with db.SessionLocal() as s:
        d = s.get(Domain, did)
        assert dirty_reason(d) is None                    # улики переписаны — домен снова чист
        assert d.rkn_listed is False


# --- РЕГРЕССИЯ 2: грязный домен в очереди выкупа --------------------------------

def test_create_order_refuses_dirty_domain():
    """ПРОХОДИЛО на c989674: отмытый (approved + reject_reason='rkn') домен вставал в очередь.

    Такое состояние — не выдумка фикстуры: ровно его и производила кнопка «↩ вернуть в approved»,
    которая `reject_reason` не трогала. На живой базе такие домены уже есть — гард в M2 их ловит.
    """
    from app.services import acquisition
    from app.services.transitions import TransitionDenied

    did = _add(domain="laundered.ru", status="approved", reject_reason="rkn", rkn_listed=True,
               score=0.9)
    with pytest.raises(TransitionDenied):
        acquisition.create_order(did, "backorder")
    assert _status(did) == "approved"                     # статус не тронут, заявки нет
    with db.SessionLocal() as s:
        assert s.execute(select(AcquisitionOrder)).first() is None


def test_confirm_order_refuses_dirty_domain(client):
    """Заявка на грязный домен могла быть заведена ДО фикса — тогда её ждала кнопка
    «✓ подтвердить выкуп», а это и есть касса. Гард стоит и на самом денежном гейте."""
    from app.services import acquisition
    from app.services.transitions import TransitionDenied

    # ровно то, что лежит в базе после прохода по старому коридору
    did = _add(domain="legacy.ru", status="purchasing", reject_reason="rkn", rkn_listed=True)
    with db.SessionLocal() as s:
        o = AcquisitionOrder(domain_id=did, provider="backorder", status="pending_confirm",
                             confirmed_by_human=False)
        s.add(o)
        s.commit()
        oid = o.id
    with pytest.raises(TransitionDenied):
        acquisition.confirm_order(oid, 190)
    with db.SessionLocal() as s:
        assert s.get(AcquisitionOrder, oid).confirmed_by_human is False   # гейт НЕ поднят


# --- РЕГРЕССИЯ 3: mark_purchased из любого статуса ------------------------------

def test_mark_purchased_refuses_discovered(client):
    """ПРОХОДИЛО на c989674: `POST /api/domains/{id}/purchase` ставил 'purchased' из ЛЮБОГО
    статуса — воронку можно было обойти целиком, включая проверку истории."""
    did = _add(domain="raw.ru", status="discovered")
    r = client.post(f"/api/domains/{did}/purchase")
    assert r.status_code == 409
    assert _status(did) == "discovered"


def test_mark_purchased_refuses_dirty_approved(client):
    """«🛒 купил руками» — тоже деньги. Отмытому домену этот путь закрыт так же, как очередь."""
    did = _add(domain="laundered2.ru", status="approved", reject_reason="history_dirty",
               prior_flags={"adult": True}, wayback_checked=True)
    r = client.post(f"/api/domains/{did}/purchase")
    assert r.status_code == 409
    assert _status(did) == "approved"


def test_mark_purchased_still_works_for_clean_approved(client):
    """Ручной выкуп чистого одобренного домена — законный путь MVP, он обязан жить."""
    did = _add(domain="clean.ru", status="approved", wayback_checked=True, score=0.85)
    assert client.post(f"/api/domains/{did}/purchase").json()["status"] == "purchased"


# --- РЕГРЕССИЯ 4: грязь невидима там, где решают о деньгах ----------------------

def test_ready_to_buy_shows_dirt_and_hides_buy_buttons(client):
    """«Готовы к выкупу» — витрина, с которой ИДУТ ПОКУПАТЬ. На c989674 отмытый домен стоял
    здесь без единой метки: ни причины отказа, ни следа РКН."""
    _add(domain="laundered.ru", status="approved", reject_reason="rkn", rkn_listed=True, score=0.9)
    html = client.get("/domains").text
    assert "реестр РКН" in html                          # причина названа по-русски
    assert "выкуп запрещён" in html
    # обе денежные кнопки для этой строки сняты (ни в очередь, ни «купил руками»)
    assert "＋ в очередь выкупа" not in html and "🛒 купил руками" not in html


def test_queue_shows_dirt_and_hides_confirm(client):
    """Очередь выкупа — последний экран перед деньгами. Легаси-заявка на грязный домен обязана
    выглядеть как грязная, а не как обычная строка со ставкой."""
    did = _add(domain="legacy.ru", status="purchasing", reject_reason="rkn", rkn_listed=True)
    with db.SessionLocal() as s:
        s.add(AcquisitionOrder(domain_id=did, provider="backorder", status="pending_confirm",
                               confirmed_by_human=False))
        s.commit()
    html = client.get("/queue").text
    assert "выкуп запрещён" in html and "реестр РКН" in html
    assert "✓ подтвердить выкуп" not in html            # селектор ставки не предлагается


def test_pool_offers_rescore_instead_of_return_for_dirt(client):
    """В реестре кнопка «↩ вернуть в approved» для грязи заменена подписью и перескором;
    у отклонённого ПОРОГОМ домена она остаётся."""
    _add(domain="rkn.ru", status="rejected", reject_reason="rkn", rkn_listed=True)
    dirty_html = client.get("/domains/pool?status=rejected").text
    assert "грязь — не возвращается" in dirty_html
    assert "↩ вернуть в approved" not in dirty_html
    assert "▶ перепроверить" in dirty_html               # честный путь назад — через воронку

    _add(domain="weak.ru", status="rejected", reject_reason="low_score", score=0.3)
    both_html = client.get("/domains/pool?status=rejected").text
    assert "↩ вернуть в approved" in both_html           # порог — возвращается


def test_autopilot_sweep_survives_denied_domain():
    """ЧТО ЛОМАЕТСЯ, когда запрет срабатывает В АВТОПИЛОТЕ: ничего — но это надо доказать.

    Стадия `queue` свипа гоняет create_order по ВСЕМ approved-доменам. Отмытый домен теперь
    отвергается — и если бы отказ ронял стадию, один грязный легаси-домен останавливал бы выкуп
    всего портфеля. Он его не роняет: чистые домены заявляются, грязный отказ уходит в ошибки
    свипа под своим именем (оператор видит причину, а не молчание).
    """
    from app.services import orchestrator

    dirty = _add(domain="laundered.ru", status="approved", reject_reason="rkn", rkn_listed=True,
                 score=0.9)
    clean = _add(domain="clean.ru", status="approved", score=0.9, wayback_checked=True)
    done, errs = orchestrator._stage_queue(10)
    assert done == 1 and _status(clean) == "purchasing"     # чистый домен свип НЕ потерял
    assert _status(dirty) == "approved"                     # грязный не тронут и не куплен
    assert len(errs) == 1 and "rkn" in errs[0]              # отказ назван, а не проглочен


def test_bulk_approve_never_stamps_dirt(client):
    """Единый предикат: новое основание «нельзя» прошло ЧЕРЕЗ bulk_ok, а не мимо него —
    иначе пакет и строка инбокса разъехались бы молча (эта ветка ловила такое трижды)."""
    from app.services import scoring

    _add(domain="scored-rkn.ru", status="scored", score=0.95, rkn_listed=True,
         wayback_checked=True)
    with db.SessionLocal() as s:
        d = s.execute(select(Domain)).scalar_one()
        assert scoring.bulk_ok(d) is False
    assert client.get("/domains/bulk-preview?min_score=0.8").json() == {"n": 0, "skipped": 1}
    client.post("/domains/bulk-approve", data={"min_score": 0.8}, follow_redirects=False)
    with db.SessionLocal() as s:
        assert s.execute(select(Domain)).scalar_one().status == "scored"
