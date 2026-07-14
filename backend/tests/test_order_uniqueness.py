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
