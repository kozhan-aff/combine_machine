"""Один ОТКРЫТЫЙ заказ на домен — инвариант БД, а не удача проверки в коде (аудит F10).

Проверка `create_order` («нет ли уже открытого заказа?») — это SELECT, а решение о вставке —
отдельный COMMIT. Между ними есть окно, и в нём живёт ВТОРОЙ писатель: кнопка «в очередь»
(sync-роут в threadpool — два клика идут параллельно) и стадия `queue` автопилотного свипа,
которая крутится В ДРУГОМ ПРОЦЕССЕ (worker) и зовёт тот же `create_order`. Под READ COMMITTED
второй транзакции чужая незакоммиченная строка НЕ ВИДНА: обе видят «заказа нет», обе видят
домен `approved` — и обе вставляют заказ. Дальше человек подтверждает каждый и платит дважды.

Поэтому «одна заявка на домен» переезжает в БД (частичный уникальный индекс — тот же приём,
что single-flight у `job_run`), а `create_order` учится проигрывать гонку: поймал IntegrityError
— вернул id ЧУЖОГО заказа, ровно как если бы увидел его в SELECT.
"""
import pathlib

import pytest
from sqlalchemy.exc import IntegrityError

import app.db as db
from app.integrations import backorder
from app.models.domain import AcquisitionOrder, Domain
from app.services import acquisition, transitions


def _approved(name="drop.ru") -> int:
    with db.SessionLocal() as s:
        d = Domain(domain=name, source="backorder", status="approved")
        s.add(d)
        s.commit()
        s.refresh(d)
        return d.id


def _collapsed_pair(name="dup.ru") -> tuple[int, int, int]:
    """Состояние, которое оставляет за собой миграция 0010: у домена ВЫЖИВШИЙ открытый заказ и
    рядом — схлопнутый дубль (`failed` + `maybe_sent`, деньги за него МОГЛИ уйти).

    Сеем РУКАМИ, потому что живой код такого состояния не производит и произвести не может:
    заказы создаёт только `create_order`, а он требует домен в `approved`; домен под живым
    заказом висит в `purchasing`, и единственный путь обратно в `approved` — `cancel_order`,
    который ту самую строку и закрывает (`failed` -> `cancelled`). Значит `failed` рядом с
    открытым заказом = наследство: дубли, которые старый код принимал до индекса (аудит F10) и
    которые схлопывает миграция 0010. Проверено перечислением ВСЕХ писателей `AcquisitionOrder.
    status` (все в services/acquisition.py) и политики `MANUAL_TRANSITIONS`.

    Гарды в коде при этом общие — они сторожат ИНВАРИАНТ БД, а не «строки от миграции»: любой
    писатель, двигающий заказ из `failed` в открытый статус, обязан спросить, свободен ли домен.
    """
    with db.SessionLocal() as s:
        d = Domain(domain=name, source="backorder", status="purchasing")
        s.add(d)
        s.commit()
        s.refresh(d)
        tier = {"price_id": "4769", "period_id": "3442"}
        winner = AcquisitionOrder(domain_id=d.id, provider="backorder", status="ordered",
                                  provider_order_id="111", confirmed_by_human=True, result=tier)
        loser = AcquisitionOrder(  # ровно то, что пишет 0010: status=failed + note + maybe_sent
            domain_id=d.id, provider="backorder", status="failed", provider_order_id="222",
            confirmed_by_human=True,
            result={**tier, "maybe_sent": True, "note": "дубль открытого заказа на домен, "
                                                        "закрыт миграцией 0010"})
        s.add_all([winner, loser])
        s.commit()
        return d.id, winner.id, loser.id


def _orders(domain_id: int) -> list[AcquisitionOrder]:
    from sqlalchemy import select
    with db.SessionLocal() as s:
        return list(s.execute(select(AcquisitionOrder)
                              .where(AcquisitionOrder.domain_id == domain_id)
                              .order_by(AcquisitionOrder.id)).scalars().all())


def test_db_refuses_a_second_open_order(sqlite_db):
    """САМ ИНВАРИАНТ: вторая открытая заявка на домен не ложится в базу — БД её отвергает.

    Вставляем ровно ту строку, которую коммитит проигравшая гонку транзакция (её SELECT чужой
    заявки не увидел, поэтому она честно дошла до INSERT). До индекса база принимала её молча.
    """
    did = _approved()
    acquisition.create_order(did, "backorder")           # заявка №1 — обычным путём

    with db.SessionLocal() as s:
        s.add(AcquisitionOrder(domain_id=did, provider="backorder",
                               status="pending_confirm", confirmed_by_human=False))
        with pytest.raises(IntegrityError):
            s.commit()
    assert len(_orders(did)) == 1                        # вторая заявка не существует


def test_create_order_loses_the_race_gracefully(sqlite_db, monkeypatch):
    """Гонка двух `create_order` (клик + свип воркера): второй возвращает ЧУЖОЙ заказ, не 500.

    Момент гонки воспроизводим точно: чужая транзакция коммитится ПОСЛЕ нашего SELECT (мы её не
    видели) и ДО нашего INSERT. Хук вешаем на `transitions.set_status` — это и есть та точка
    внутри `create_order`. Победитель делает то же, что сделал бы настоящий второй процесс:
    двигает домен в `purchasing` и кладёт заявку.
    """
    did = _approved()
    winner: dict = {}
    real_set_status = transitions.set_status

    def racing_set_status(d, target):
        if not winner:                                   # ровно один раз — это гонка, а не цикл
            with db.SessionLocal() as other:             # ДРУГОЙ процесс: свой сеанс, свой коммит
                other.get(Domain, did).status = "purchasing"
                o = AcquisitionOrder(domain_id=did, provider="backorder",
                                     status="pending_confirm", confirmed_by_human=False)
                other.add(o)
                other.commit()
                winner["id"] = o.id
        return real_set_status(d, target)

    monkeypatch.setattr(transitions, "set_status", racing_set_status)
    oid = acquisition.create_order(did, "backorder")

    assert oid == winner["id"], "проигравший обязан вернуть заказ победителя, а не завести свой"
    assert len(_orders(did)) == 1, "на домене осталась ровно одна открытая заявка"


def test_closed_orders_do_not_block_a_new_one(sqlite_db):
    """Индекс ЧАСТИЧНЫЙ: снятая заявка домен не запирает — иначе передумавший оператор не смог
    бы поставить его в очередь заново (а `cancel_order` ровно для этого и возвращает `approved`)."""
    did = _approved()
    first = acquisition.create_order(did, "backorder")
    acquisition.cancel_order(first)                      # pending_confirm -> cancelled, домен -> approved

    second = acquisition.create_order(did, "backorder")
    assert second != first
    assert [o.status for o in _orders(did)] == ["cancelled", "pending_confirm"]


def test_maybe_sent_order_leaves_no_room_for_a_second(sqlite_db, monkeypatch):
    """`failed` + `maybe_sent` (деньги МОГЛИ уйти) в индекс не входит — и не должен: домен под
    таким заказом и так заперт политикой статусов.

    Живой путь: заказ ушёл в неизвестность (AmbiguousSend) -> `failed`+`maybe_sent`. Отмена
    такого заказа запрещена (`cancel_order`), значит домен остаётся в `purchasing`, а из
    `purchasing` заявку не заводят вовсе. Второго заказа на домен, чей первый мог долететь до
    провайдера, не бывает — проверяем это ЖИВЫМ путём, а не верой в комментарий.
    """
    monkeypatch.setattr(backorder.BackorderClient, "tariffs",
                        lambda self, zone=".RU", refresh=False: [
                            {"price_id": "4769", "period_id": "3442", "price": 190.0}])
    monkeypatch.setattr(backorder.BackorderClient, "find_order", lambda self, domain: None)

    def _amb(self, d, price_id, period_id):
        raise backorder.AmbiguousSend("связь оборвалась: ReadTimeout")
    monkeypatch.setattr(backorder.BackorderClient, "order", _amb)

    did = _approved()
    oid = acquisition.create_order(did, "backorder")
    acquisition.confirm_order(oid, 190)
    assert acquisition.execute_confirmed_order(oid)["maybe_sent"] is True

    with pytest.raises(transitions.TransitionDenied):    # домен в purchasing — второй заявке нет пути
        acquisition.create_order(did, "backorder")
    assert len(_orders(did)) == 1


# --- писатели, двигающие заказ ИЗ `failed` В открытый статус (ревью Задачи 7, Critical) ------
# Индекс запер `pending_confirm|ordering|ordered`, но про это узнал только `create_order`.
# `poll_orders` (фантом «в полёте» -> ordered) и `execute_confirmed_order` (ретрай failed ->
# ordering) продолжали писать вслепую — и на домене со схлопнутым дублем ловили IntegrityError.


def test_poll_does_not_raise_a_duplicate_into_ordered(sqlite_db, monkeypatch):
    """Поллинг не поднимает дубль в 'ordered', пока домен держит другой открытый заказ — и НЕ
    теряет из-за него всю пачку.

    До фикса: у схлопнутого дубля elid есть, провайдер честно отвечает «в полёте» -> poll ставил
    ему 'ordered' -> IntegrityError на ОДНОМ коммите цикла -> ни один заказ портфеля не
    синхронизировался, и так на каждом нажатии, вечно.
    """
    did, win, lose = _collapsed_pair()
    other = _approved("other.ru")                        # обычный заказ портфеля — он ОБЯЗАН съехать
    with db.SessionLocal() as s:
        s.get(Domain, other).status = "purchasing"
        s.add(AcquisitionOrder(domain_id=other, provider="backorder", status="ordered",
                               provider_order_id="333", confirmed_by_human=True))
        s.commit()

    monkeypatch.setattr(backorder.BackorderClient, "client_orders", lambda self: [
        {"elid": "111", "domain": "dup.ru", "id_status": "2", "clear_status": "Не оплачен",
         "state": "pending", "tariff": "190"},
        {"elid": "222", "domain": "dup.ru", "id_status": "2", "clear_status": "Не оплачен",
         "state": "pending", "tariff": "190"},
        {"elid": "333", "domain": "other.ru", "id_status": "11", "clear_status": "Завершён",
         "state": "caught", "tariff": "190"},
    ])
    r = acquisition.poll_orders()                        # до фикса — IntegrityError наружу

    by_id = {o.id: o for o in _orders(did)}
    assert by_id[win].status == "ordered"                # выживший в полёте — как и был
    assert by_id[lose].status == "failed", "дубль подняли в открытый статус — инвариант пробит"
    res = by_id[lose].result or {}
    # ровно то, что покажет очередь: queue.html рендерит `error or note or clear_status`
    assert f"#{win}" in (res.get("error") or res.get("note") or ""), \
        "оператор должен видеть в очереди, ПОЧЕМУ дубль не поднят"
    assert res.get("maybe_sent") is True, (
        "деньги за дубль могли уйти — неизвестность снимать нельзя, иначе отмена его спрячет")
    assert r["conflicts"] == 1
    with db.SessionLocal() as s:                         # ПАЧКА НЕ ПОТЕРЯНА: чужой заказ синхронизирован
        assert s.get(Domain, other).status == "purchased"
    assert r["caught"] == 1


def test_retry_refuses_while_another_order_is_open(sqlite_db, monkeypatch):
    """«↻ повторить» на схлопнутом дубле отвечает человеку текстом, а не SQL-трейсом.

    До фикса атомарный claim ('failed' -> 'ordering') стоял ВНЕ try и падал IntegrityError'ом
    прямо в баннер панели: у строки, про которую известно, что деньги МОГЛИ уйти, не оставалось
    вообще ни одного выхода (отмена заперта maybe_sent, повтор падает, поллинг падает).
    """
    def _no_money(self, domain, price_id, period_id):    # сеть не нужна: до отправки не дойдём
        raise AssertionError("повтор обязан отказать ДО провайдера — деньги не трогаем")
    monkeypatch.setattr(backorder.BackorderClient, "order", _no_money)

    did, win, lose = _collapsed_pair()
    r = acquisition.execute_confirmed_order(lose)        # до фикса — IntegrityError наружу

    assert f"#{win}" in r["error"], "человек должен узнать, какой заказ держит домен"
    by_id = {o.id: o for o in _orders(did)}
    assert by_id[lose].status == "failed"                # дубль не тронут — ни статус, ни флаг
    assert (by_id[lose].result or {}).get("maybe_sent") is True
    assert by_id[win].status == "ordered"


def test_index_predicate_matches_the_migration(sqlite_db):
    """Предикат индекса живёт в ДВУХ местах: `_OPEN_ORDER_SQL` (модель — её `create_all` видят
    тесты) и текст миграции 0010 (её исполняет живой PostgreSQL). Разъедутся — тест зелен, прод
    дыряв: добавленный в OPEN_ORDER_STATUSES статус запирался бы только в SQLite."""
    from app.models.domain import _OPEN_ORDER_SQL
    mig = (pathlib.Path(__file__).resolve().parents[1]
           / "alembic" / "versions" / "0010_order_uniqueness.py").read_text(encoding="utf-8")
    assert _OPEN_ORDER_SQL in mig, (
        "предикат уникального индекса разошёлся с OPEN_ORDER_STATUSES — меняешь список статусов, "
        "пиши НОВУЮ миграцию (живой PG старую уже накатил, повторно она не выполнится)")
