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
    assert dirty_reason(d(reject_reason="safebrowsing")) == "safebrowsing"
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


def test_create_order_refuses_dirty_even_with_open_order():
    """ПРОХОДИЛО на ce3bc36: у грязного домена с уже открытой заявкой create_order возвращал её
    id как УСПЕХ — гард стоял ПОСЛЕ раннего возврата по `existing`.

    Денег это не тратило, но стадия `queue` автопилота засчитывала такой домен заявленным
    (done += 1), а оператор не слышал ни слова о том, что в очереди выкупа лежит РКН-домен.
    «Грязь в очереди» не имеет права быть тихим успехом.
    """
    from app.services import acquisition
    from app.services.transitions import TransitionDenied

    did = _add(domain="legacy-open.ru", status="approved", reject_reason="rkn", rkn_listed=True)
    with db.SessionLocal() as s:
        s.add(AcquisitionOrder(domain_id=did, provider="backorder", status="pending_confirm",
                               confirmed_by_human=False))
        s.commit()
    with pytest.raises(TransitionDenied):
        acquisition.create_order(did, "backorder")


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


def test_stage_queue_dirt_does_not_starve_the_cap():
    """ПРОХОДИЛО на ce3bc36: стадия `queue` автопилота вставала НАМЕРТВО.

    Отмытые легаси-домены из `approved` больше не уходят (политика их отвергает, статус им никто
    не двигает) — и `ORDER BY id LIMIT cap` по сырому `approved` отдавал стадии РОВНО ИХ: они
    сидят в голове id-порядка и выедали весь кап каждый свип. Чистый домен не попадал в очередь
    НИКОГДА (прогон ревьюера: три свипа подряд, done=0 errs=10, clean.ru всё ещё approved).

    Фикстура ПЛОТНАЯ намеренно: грязных ≥ cap (10 = дефолт cap_queue). Прежний тест брал одного
    грязного при капе 10 — форма фикстуры прятала поведение, ровно тот же урок ветки.
    """
    from app.services import orchestrator

    cap = 10
    dirty = [_add(domain=f"laundered{i}.ru", status="approved", reject_reason="rkn",
                  rkn_listed=True, score=0.9) for i in range(cap + 2)]   # ПЛОТНО: 12 > cap
    clean = _add(domain="clean.ru", status="approved", score=0.9, wayback_checked=True)

    done, errs, extra = orchestrator._stage_queue(cap)

    assert done == 1 and _status(clean) == "purchasing"  # чистый домен в очередь ПОПАЛ
    assert all(_status(x) == "approved" for x in dirty)  # грязные не тронуты и не куплены
    assert errs == []                                    # это не ошибка стадии, а её защита
    assert extra == {"queue_dirty": len(dirty)}          # ...но оператор об этом СЛЫШИТ


def test_sweep_counts_report_skipped_dirt(client):
    """Счётчик грязи доезжает до журнала свипов, а не теряется в тупле стадии."""
    from app.services import orchestrator

    from app.services import autonomy

    _add(domain="laundered.ru", status="approved", reject_reason="rkn", rkn_listed=True, score=0.9)
    _add(domain="clean.ru", status="approved", score=0.9, wayback_checked=True)
    autonomy.update_autonomy(autopilot_on=True, auto_discovery=False, auto_score=False,
                             auto_queue=True, auto_provision=False, auto_generate=False,
                             auto_publish=False, auto_check_index=False, cap_queue=10)

    out = orchestrator.run_sweep(trigger="manual")
    assert out["counts"]["queue"] == 1 and out["counts"]["queue_dirty"] == 1
    assert client.get("/autopilot").text.count("грязь пропущена") >= 1   # по-русски, не queue_dirty


def test_bulk_approve_never_stamps_dirt(client):
    """СТРАХОВКА (не репро): единый предикат `bulk_ok` спрашивает и про грязь.

    Честно о происхождении: состояние «scored + rkn_listed=True» машина породить НЕ МОЖЕТ —
    воронка при РКН выходит на T2 сразу в `rejected`, а пакет берёт только `scored`. Фикстура
    ниже сконструирована, живого коридора здесь не было. Гард всё равно нужен — defense in
    depth: после Critical-2 сигнальные колонки ПЕРЕЖИВАЮТ перескор, и домен со старым
    `rkn_listed=True`, заново вышедший в `scored` по свежему score, — состояние достижимое.
    """
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


def test_inbox_hides_approve_for_dirty_row(client):
    """ПРОХОДИЛО на ce3bc36: у грязной `scored`-строки инбокса оставалась «✓ одобрить».

    Пакет её уже не трогал (`bulk_ok`), политика бы отказала — то есть кнопка вела в
    ГАРАНТИРОВАННЫЙ отказ. Ложное предложение одобрить, ровно как «↩ вернуть» в реестре.
    """
    _add(domain="scored-rkn.ru", status="scored", score=0.95, rkn_listed=True, wayback_checked=True)
    html = client.get("/domains").text
    assert "✓ одобрить" not in html
    assert "выкуп запрещён — грязь" in html and "реестр РКН" in html
    assert "▶ перепроверить" in html                      # честный путь назад на месте


def test_inbox_keeps_approve_for_clean_row(client):
    """ЧТО ЛОМАЕТСЯ у чистого домена: ничего — гейт курации остаётся кнопкой человека."""
    _add(domain="clean.ru", status="scored", score=0.75, wayback_checked=True)
    assert "✓ одобрить" in client.get("/domains").text


# --- РЕГРЕССИЯ 5: КАССА (execute) не спрашивала про грязь -----------------------

def test_execute_refuses_dirty_before_sending_money(monkeypatch):
    """ПРОХОДИЛО на ce3bc36: ЖИВОЙ ПЛАТНЫЙ ЗАКАЗ на грязный домен уходил провайдеру.

    Заказ, подтверждённый ДО фикса, приходит на отправку с `confirmed_by_human=True` и уже
    замороженным тарифом — ни create_order, ни confirm_order его больше не увидят, а
    `execute_confirmed_order` про `dirty_reason` не спрашивал ВООБЩЕ. Единственной защитой было
    условие в шаблоне queue.html; прямой POST /queue/{id}/execute (старая вкладка, «↻ повторить»
    после failed) шёл мимо него.

    Гард стоит ДО атомарного claim'а: иначе заказ уезжал бы в транзиентный 'ordering', из
    которого его не снять (cancel_order берёт только pending_confirm/failed).
    """
    import app.integrations.backorder as bo
    from app.services import acquisition
    from app.services.transitions import TransitionDenied

    sent = []

    class _Spy:                                  # если провайдера позвали — деньги ушли
        def find_order(self, dom):
            sent.append(("find", dom))
            return None
        def order(self, dom, price_id=None, period_id=None):
            sent.append(("ORDER", dom))          # <-- живой платный заказ
            return {"order_id": "666"}
    monkeypatch.setattr(bo, "BackorderClient", _Spy)

    did = _add(domain="legacy-rkn.ru", status="purchasing", reject_reason="rkn", rkn_listed=True)
    with db.SessionLocal() as s:
        o = AcquisitionOrder(domain_id=did, provider="backorder", status="pending_confirm",
                             confirmed_by_human=True, cost=190,      # гейт поднят ДО фикса
                             result={"price_id": 1, "period_id": 2})  # тариф заморожен
        s.add(o)
        s.commit()
        oid = o.id

    with pytest.raises(TransitionDenied):
        acquisition.execute_confirmed_order(oid)

    assert sent == []                            # провайдера НЕ звали — деньги не потрачены
    with db.SessionLocal() as s:
        o = s.get(AcquisitionOrder, oid)
        assert o.status == "pending_confirm"     # НЕ 'ordering': отказ раньше claim'а
        assert o.provider_order_id in (None, "")


def test_execute_still_sends_clean_confirmed_order(monkeypatch):
    """ЧТО ЛОМАЕТСЯ у чистого заказа: ничего — денежный путь обязан ехать."""
    import app.integrations.backorder as bo
    from app.services import acquisition

    sent = []

    class _Spy:
        def find_order(self, dom):
            return None
        def order(self, dom, price_id=None, period_id=None):
            sent.append(dom)
            return {"order_id": "777"}
    monkeypatch.setattr(bo, "BackorderClient", _Spy)

    did = _add(domain="clean.ru", status="purchasing", score=0.9, wayback_checked=True)
    with db.SessionLocal() as s:
        o = AcquisitionOrder(domain_id=did, provider="backorder", status="pending_confirm",
                             confirmed_by_human=True, cost=190,
                             result={"price_id": 1, "period_id": 2})
        s.add(o)
        s.commit()
        oid = o.id

    r = acquisition.execute_confirmed_order(oid)
    assert r["status"] == "ordered" and sent == ["clean.ru"]


# --- РЕГРЕССИЯ 6: перескор ОТМЫВАЛ грязь (ранний выход воронки стирал улики) ----

def _clients(**over):
    """Клиенты воронки. По умолчанию всё чисто — тест портит ровно ту проверку, что изучает."""
    c = {
        "wayback": type("W", (), {"classify_history": lambda self, d: {
            "prior_flags": {}, "wayback_checked": True, "sampled": 3, "evidence": [],
            "first_seen": None, "age_years": None}})(),
        "rkn": type("R", (), {"is_listed": lambda self, d: False})(),
        "blacklist": type("B", (), {"is_blacklisted": lambda self, d: False})(),
        "searxng": type("S", (), {"indexed_echo": lambda self, d: True})(),
        "aparser": type("A", (), {"whois_probe": lambda self, d: {
            "available": True, "created": datetime(2008, 1, 1, tzinfo=timezone.utc)}})(),
    }
    return {**c, **over}


def test_rescore_early_exit_does_not_erase_rkn_evidence():
    """ПРОХОДИЛО на ce3bc36: «▶ перепроверить» ОТМЫВАЛА РКН-домен — и это была кнопка, которую
    мы сами поставили как «честный путь назад».

    Воронка выходит на T1 (whois: домен занят -> not_acquirable). РКН/блэклист/Wayback при таком
    выходе НЕ ВЫПОЛНЯЮТСЯ — а score_domain писал их колонки безусловно, то есть клал туда None.
    Улики исчезали, `dirty_reason` возвращал None, политика РАЗРЕШАЛА «↩ вернуть в approved» —
    и дальше домен ехал в очередь и на ставку, ни разу не показав, что он в реестре РКН.
    """
    from app.services import scoring, transitions

    did = _add(domain="rkn-taken.ru", status="rejected", reject_reason="rkn", rkn_listed=True,
               lane="free", referring_domains=300, wayback_checked=True,
               prior_flags={}, score=0.0)
    taken = type("A", (), {"whois_probe": lambda self, d: {   # занят -> ранний выход на T1
        "available": False, "created": datetime(2008, 1, 1, tzinfo=timezone.utc)}})()
    out = scoring.score_domain(did, clients=_clients(aparser=taken))

    assert out["reject_reason"] == "not_acquirable"       # вердикт этого прогона — про занятость
    with db.SessionLocal() as s:
        d = s.get(Domain, did)
        assert d.rkn_listed is True                       # улику НЕ СТЁРЛИ: РКН никто не спрашивал
        assert transitions.dirty_reason(d) == "rkn"       # домен по-прежнему грязный
        with pytest.raises(transitions.TransitionDenied):
            transitions.check(d, "approved")              # ...и в оборот не возвращается


def test_rescore_t0_exit_does_not_erase_history_evidence():
    """Тот же корень с другого входа: поднял min_rd в /settings -> перескор -> low_rd на T0.

    T0 не зовёт вообще ничего. Грязная ИСТОРИЯ (prior_flags) и блэклист обязаны пережить это —
    иначе «ослабь порог обратно» возвращало бы домен уже отмытым.
    """
    from app.services import scoring, transitions
    from app.services.settings import update_settings

    did = _add(domain="casino-lowrd.ru", status="rejected", reject_reason="history_dirty",
               prior_flags={"casino": True}, wayback_checked=True, blacklisted=True,
               lane="bid", referring_domains=5, score=0.0,
               # снимки, по которым вынесен вердикт: они тоже не должны исчезнуть — иначе
               # инбокс пишет «история грязная — смотри снимки», а смотреть нечего
               score_breakdown={"history_evidence": [{"url": "casino-lowrd.ru", "when": "2015"}],
                                "errors": []})
    update_settings(min_referring_domains=100)            # порог подняли — домен не проходит T0

    out = scoring.score_domain(did, clients=_clients())
    assert out["reject_reason"] == "low_rd"
    with db.SessionLocal() as s:
        d = s.get(Domain, did)
        assert d.prior_flags == {"casino": True} and d.blacklisted is True   # обе улики целы
        assert scoring.history_verdict(d) == "dirty"      # история — по-прежнему подтверждённая грязь
        assert transitions.dirty_reason(d) is not None    # (называет 'blacklist' — он проверяется раньше)
        assert d.score_breakdown["history_evidence"] == [{"url": "casino-lowrd.ru", "when": "2015"}]


def test_rescore_that_actually_ran_the_checks_still_rehabilitates():
    """ЧТО ЛОМАЕТСЯ от запрета стирать улики: НИЧЕГО у настоящей реабилитации.

    Проверка, которая ОТРАБОТАЛА и сказала «чист», кладёт False — и домен выходит из грязи.
    Правило звучит «не стирай непроверенное», а не «не верь проверкам».
    """
    from app.services import scoring, transitions

    did = _add(domain="unblocked2.ru", status="rejected", reject_reason="rkn", rkn_listed=True,
               lane="bid", referring_domains=300)
    out = scoring.score_domain(did, clients=_clients())
    assert out["reject_reason"] is None
    with db.SessionLocal() as s:
        d = s.get(Domain, did)
        assert d.rkn_listed is False and transitions.dirty_reason(d) is None
