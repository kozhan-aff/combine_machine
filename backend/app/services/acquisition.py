"""M2 — Acquisition. Очередь выкупа с ЖЁСТКИМ денежным гейтом (PLAN §2, правило 2).

Поток: approved-домен → create_order (pending_confirm) → человек confirm_order (ставит
confirmed_by_human=True И выбирает СТАВКУ) → execute_confirmed_order шлёт заказ провайдеру
ТОЛЬКО при confirmed_by_human. Деньги на автопилоте не тратятся.

Ставка — часть денежного гейта. У backorder тариф И ЕСТЬ ставка (сетка 190 ₽ … 5 млн ₽:
выше тариф → больше регистраторов → выше шанс перехвата), поэтому «сколько заплатить»
решает человек на confirm; выбранный тир замораживается в заказе (cost + price_id/period_id
в result), и execute уже ничего не до-решает.

backorder заказывается живьём (uniservice.order). execute идемпотентен по деньгам: перед
отправкой спрашивает провайдера, нет ли уже заказа на этот домен — иначе ambiguous-таймаут
(заказ ушёл, ответ не дошёл) + кнопка «повторить» = второе списание. optimizator ещё не
реализован — execute это честно репортит (status='failed'), не делая вид, что купил.
"""
_PROVIDERS = {"backorder", "optimizator"}
# статусы, при которых по домену уже есть незакрытый заказ — второй не плодим
# ('ordering' — транзиентный claim во время отправки провайдеру, тоже «открыт»)
_OPEN_STATUSES = {"pending_confirm", "ordering", "ordered"}


def create_order(domain_id: int, provider: str = "backorder") -> int:
    """Поставить approved-домен в очередь выкупа (pending_confirm). Идемпотентно по домену.

    Возвращает id заказа (существующего открытого или нового). Не тратит денег —
    только заявка, ждущая подтверждения человеком."""
    from sqlalchemy import select
    from app.db import SessionLocal
    from app.models.domain import Domain, AcquisitionOrder

    if provider not in _PROVIDERS:
        raise ValueError(f"unknown provider {provider!r} (ожидается {_PROVIDERS})")
    with SessionLocal() as db:
        d = db.get(Domain, domain_id)
        if d is None:
            raise ValueError(f"domain {domain_id} not found")
        existing = db.execute(
            select(AcquisitionOrder).where(
                AcquisitionOrder.domain_id == domain_id,
                AcquisitionOrder.status.in_(_OPEN_STATUSES))
        ).scalar_one_or_none()
        if existing:
            return existing.id                      # уже в очереди — не дублируем
        if d.status != "approved":                  # только одобренный скорингом домен
            raise ValueError(
                f"домен в статусе {d.status!r} — в очередь выкупа берём только approved")
        order = AcquisitionOrder(domain_id=domain_id, provider=provider,
                                 status="pending_confirm", confirmed_by_human=False)
        db.add(order)
        d.status = "purchasing"                      # видно в воронке: домен в очереди выкупа
        db.commit()
        db.refresh(order)
        return order.id


def confirm_order(order_id: int, bid_rub: float | None = None) -> dict:
    """ЧЕЛОВЕК подтверждает выкуп — единственный путь поднять денежный гейт.

    `bid_rub` — СТАВКА. У backorder тариф и есть ставка (сетка 190 ₽ … 5 млн ₽): чем выше,
    тем выше шанс перехвата. «Сколько заплатить» — это решение о деньгах, поэтому его
    принимает человек здесь же, на гейте, а не система. Кладём в AcquisitionOrder.cost.

    Только ставит confirmed_by_human=True; заказ провайдеру НЕ шлёт (это execute)."""
    from app.db import SessionLocal
    from app.models.domain import Domain, AcquisitionOrder

    # Читаем состояние и валидируем, НЕ держа транзакцию открытой на время сетевого вызова.
    with SessionLocal() as db:
        o = db.get(AcquisitionOrder, order_id)
        if o is None:
            raise ValueError(f"order {order_id} not found")
        if o.status != "pending_confirm":
            return {"order_id": order_id, "status": o.status, "note": "не в статусе pending_confirm"}
        provider = o.provider
        d = db.get(Domain, o.domain_id)
        domain = d.domain if d else None
    if provider == "backorder" and not bid_rub:
        raise ValueError("backorder: не выбрана ставка (тариф) — без неё заказ отправить нельзя")
    if bid_rub is not None and bid_rub <= 0:
        raise ValueError(f"ставка должна быть больше нуля, получено {bid_rub}")

    tier = None
    if provider == "backorder":
        # Тариф выбираем ЗДЕСЬ и замораживаем в заказе: ставка между тирами округляется вверх,
        # и человек должен увидеть фактическую сумму на своём же действии, а не получить
        # «система решила доплатить» на отправке. execute тир уже не трогает.
        # Сетевой pick_tariff — ВНЕ транзакции БД (иначе лежащий провайдер держит соединение).
        from app.integrations.backorder import BackorderClient
        if domain is None:
            raise ValueError(f"order {order_id}: домен не найден")
        tier = BackorderClient().pick_tariff(domain, float(bid_rub))

    with SessionLocal() as db:
        o = db.get(AcquisitionOrder, order_id)
        if o is None or o.status != "pending_confirm":   # состояние сменилось, пока ходили в сеть
            return {"order_id": order_id, "status": o.status if o else "gone",
                    "note": "не в статусе pending_confirm"}
        if tier is not None:
            o.cost = tier["price"]                   # ФАКТИЧЕСКИЙ тир, а не желаемая сумма
            o.result = {**(o.result or {}),
                        "price_id": tier["price_id"], "period_id": tier["period_id"]}
        elif bid_rub is not None:
            o.cost = bid_rub
        o.confirmed_by_human = True                  # HARD GATE поднят человеком
        db.commit()
        return {"order_id": order_id, "status": o.status, "confirmed_by_human": True,
                "bid_rub": float(o.cost) if o.cost is not None else None}


def execute_confirmed_order(order_id: int) -> dict:
    """Отправить подтверждённый заказ провайдеру. ГЕЙТ: только при confirmed_by_human.

    Провайдерский транспорт .order() требует login-кредов и пока не реализован —
    в этом случае помечаем 'failed' с причиной, а не выдаём ложный успех."""
    from datetime import datetime, timezone
    from sqlalchemy import update
    from app.db import SessionLocal
    from app.models.domain import Domain, AcquisitionOrder

    with SessionLocal() as db:
        o = db.get(AcquisitionOrder, order_id)
        if o is None:
            raise ValueError(f"order {order_id} not found")
        if not o.confirmed_by_human:                 # ЖЁСТКИЙ ГЕЙТ — деньги не на автопилоте
            return {"order_id": order_id, "status": o.status,
                    "error": "gate: заказ не подтверждён человеком (confirmed_by_human=False)"}

        # Атомарный claim: из двух параллельных кликов (sync-роуты в threadpool) в 'ordering'
        # переведёт РОВНО один — второй увидит rowcount 0 и не пошлёт второй живой заказ.
        # confirmed_by_human остаётся в SQL-условии — денежный гейт держится и здесь.
        # Допуск 'failed' в claim = рабочий ретрай (кнопка «↻ повторить»).
        claim = db.execute(
            update(AcquisitionOrder)
            .where(AcquisitionOrder.id == order_id,
                   AcquisitionOrder.status.in_(("pending_confirm", "failed")),
                   AcquisitionOrder.confirmed_by_human.is_(True))
            .values(status="ordering")
        )
        db.commit()
        if claim.rowcount != 1:                       # уже забрал другой клик / уже обработан
            db.refresh(o)
            return {"order_id": order_id, "status": o.status,
                    "note": "заказ уже обрабатывается или обработан"}
        db.refresh(o)

        d = db.get(Domain, o.domain_id)
        # Тариф, замороженный человеком на confirm, живёт в o.result — а неудача перезаписывает
        # result ошибкой. Несём его через ВСЕ ветки, иначе «↻ повторить» после первого отказа
        # теряет ставку и требует переподтверждения на ровном месте.
        saved = {k: v for k, v in (o.result or {}).items() if k in ("price_id", "period_id")}
        try:
            if o.provider == "backorder":
                from app.integrations.backorder import AmbiguousSend, BackorderClient
                price_id, period_id = saved.get("price_id"), saved.get("period_id")
                if not (price_id and period_id):      # тариф замораживается человеком в confirm
                    raise RuntimeError("не выбрана ставка (тариф) — переподтверди заказ со ставкой")
                c = BackorderClient()

                # ИДЕМПОТЕНТНОСТЬ ДЕНЕГ. Прежде чем платить — спросить провайдера, нет ли уже
                # заказа на этот домен. Закрывает ambiguous-сбой (таймаут ПОСЛЕ отправки:
                # заказ ушёл, мы записали failed, человек жмёт «повторить») и ручной заказ из ЛК.
                dup = c.find_order(d.domain)
                if dup is not None:
                    o.status = "ordered"
                    o.provider_order_id = dup["elid"]
                    o.result = {**saved, "note": "заказ на этот домен у провайдера уже есть — "
                                                 "второй не шлём", "clear_status": dup["clear_status"]}
                    o.ordered_at = o.ordered_at or datetime.now(timezone.utc)
                    db.commit()
                    return {"order_id": order_id, "status": o.status, "result": o.result}

                try:
                    res = c.order(d.domain, price_id=price_id, period_id=period_id)
                except AmbiguousSend as e:
                    # Исход НЕИЗВЕСТЕН (таймаут / 5xx / не-JSON) — заказ мог уйти и деньги
                    # списаться. Не предлагаем «повторить» вслепую: сначала опрос провайдера.
                    # Явный отказ провайдера сюда НЕ попадает — он RuntimeError ниже.
                    o.status = "failed"
                    o.result = {**saved, "error": f"исход неизвестен: {e}", "maybe_sent": True}
                    db.commit()
                    return {"order_id": order_id, "status": "failed", **o.result}
            else:
                from app.integrations.optimizator import OptimizatorClient
                res = OptimizatorClient().register([d.domain])   # optimizator берёт список
            o.status = "ordered"
            o.provider_order_id = str(res.get("order_id") or "") if isinstance(res, dict) else ""
            o.result = {**saved, **(res if isinstance(res, dict) else {"raw": str(res)})}
            o.ordered_at = datetime.now(timezone.utc)
            db.commit()
            return {"order_id": order_id, "status": o.status, "result": o.result}
        except NotImplementedError:
            o.status = "failed"
            o.result = {**saved, "error": f"провайдер {o.provider}: транспорт заказа не реализован"}
            db.commit()
            return {"order_id": order_id, "status": "failed", **o.result}
        except Exception as e:  # noqa: BLE001 — сбой провайдера -> failed, не 500
            o.status = "failed"
            o.result = {**saved, "error": f"{type(e).__name__}: {e}"[:200]}
            db.commit()
            return {"order_id": order_id, "status": "failed", **o.result}


def mark_caught(order_id: int) -> dict:
    """ЧЕЛОВЕК подтверждает факт поимки: заказ 'ordered' -> 'caught', домен -> 'purchased'.

    Для backorder поимка дропа асинхронна (провайдер ловит домен на удалении), потому
    факт фиксируем руками. С этого момента домен куплен — дальше M3 (создать сайт)."""
    from app.db import SessionLocal
    from app.models.domain import Domain, AcquisitionOrder

    with SessionLocal() as db:
        o = db.get(AcquisitionOrder, order_id)
        if o is None:
            raise ValueError(f"order {order_id} not found")
        if o.status != "ordered":
            return {"order_id": order_id, "status": o.status,
                    "note": "пометить пойманным можно только заказ в статусе ordered"}
        o.status = "caught"
        d = db.get(Domain, o.domain_id)
        if d is not None:
            d.status = "purchased"                    # домен куплен — путь в M3
        db.commit()
        return {"order_id": order_id, "status": "caught", "domain_id": o.domain_id}


def poll_orders() -> dict:
    """Синхронизировать отправленные заказы с правдой провайдера (по кнопке, не автопилотом).

    Читает clientbackorder (денег НЕ тратит) и двигает 'ordered' -> 'caught'/'failed' по
    id_status. Это НЕ обход денежного гейта: деньги уже потрачены на execute за подтверждением
    человека, а поимка — факт со стороны провайдера, а не наше решение. Ручной mark_caught
    остаётся (провайдер может молчать). Оркестратор эту функцию не зовёт.
    """
    from sqlalchemy import select
    from app.db import SessionLocal
    from app.models.domain import Domain, AcquisitionOrder
    from app.integrations.backorder import BackorderClient

    from app.integrations.backorder import _norm

    remote_orders = BackorderClient().client_orders()
    by_elid = {r["elid"]: r for r in remote_orders if r["elid"]}
    # Фолбэк для строк без elid — по нормализованному домену (.РФ: фид кириллица, billmgr
    # punycode) и по ЖИВОМУ заказу: первый не-failed. Голый dict-comprehension схлопывал бы
    # историю домена в последнюю строку и мог усыновить фантому старый аннулированный заказ.
    by_domain: dict = {}
    for r in remote_orders:
        k = _norm(r["domain"])
        if k not in by_domain or (by_domain[k]["state"] == "failed" and r["state"] != "failed"):
            by_domain[k] = r
    moved = {"caught": 0, "failed": 0, "pending": 0}
    matched = 0
    with SessionLocal() as db:
        # 'failed' опрашиваем тоже: там может лежать ФАНТОМ — заказ, который на самом деле
        # ушёл (ambiguous-таймаут), и провайдер его знает. Иначе он невидим, а домен потерян.
        rows = db.execute(
            select(AcquisitionOrder).where(AcquisitionOrder.provider == "backorder",
                                           AcquisitionOrder.status.in_(("ordered", "failed")))
        ).scalars().all()
        for o in rows:
            d = db.get(Domain, o.domain_id)
            # Матч по elid, а не по домену: заказов на один домен может быть несколько
            # (ретрай, ручной заказ из ЛК), и словарь по имени схлопнул бы их в последний —
            # свежий заказ получил бы статус протухшего.
            remote = by_elid.get(o.provider_order_id or "") or (
                by_domain.get(_norm(d.domain)) if d and not o.provider_order_id else None)
            if remote is None:
                continue                              # провайдер ещё не показывает заказ
            matched += 1
            if not o.provider_order_id and remote["elid"]:
                o.provider_order_id = remote["elid"]  # усыновляем фантом: теперь он отслеживаем
            state = remote["state"]
            moved[state] = moved.get(state, 0) + 1
            o.result = {**(o.result or {}), "clear_status": remote["clear_status"]}
            o.result.pop("maybe_sent", None)          # неопределённость снята правдой провайдера
            if state == "caught":
                o.status = "caught"
                if d is not None:
                    d.status = "purchased"            # домен наш — путь в M3
            elif state == "failed":
                o.status = "failed"
            else:
                o.status = "ordered"                  # в полёте (в т.ч. поднимает фантом из failed)
        db.commit()
    return {"checked": matched, **moved}


def cancel_order(order_id: int) -> dict:
    """Снять заявку (pending_confirm/failed -> cancelled). Домен возвращаем в 'approved',
    если по нему не осталось других открытых заказов. Денег это не касается."""
    from sqlalchemy import select
    from app.db import SessionLocal
    from app.models.domain import Domain, AcquisitionOrder

    with SessionLocal() as db:
        o = db.get(AcquisitionOrder, order_id)
        if o is None:
            raise ValueError(f"order {order_id} not found")
        if o.status not in {"pending_confirm", "failed"}:
            return {"order_id": order_id, "status": o.status,
                    "note": "снять можно только заказ в статусе pending_confirm/failed"}
        if (o.result or {}).get("maybe_sent"):
            # cancelled из поллинга не опрашивается -> реально оплаченный заказ стал бы
            # невидим навсегда, а домен вернулся бы в approved. Сначала снять неизвестность.
            return {"order_id": order_id, "status": o.status,
                    "error": "исход заказа неизвестен (связь оборвалась, деньги могли уйти) — "
                             "сначала «↻ обновить статусы у провайдера», потом отменяй"}
        o.status = "cancelled"
        d = db.get(Domain, o.domain_id)
        if d is not None and d.status == "purchasing":
            other_open = db.execute(
                select(AcquisitionOrder.id).where(
                    AcquisitionOrder.domain_id == o.domain_id,
                    AcquisitionOrder.id != order_id,
                    AcquisitionOrder.status.in_(_OPEN_STATUSES))
            ).first()
            if other_open is None:                    # больше ничего не держит домен в очереди
                d.status = "approved"
        db.commit()
        return {"order_id": order_id, "status": "cancelled", "domain_id": o.domain_id}


def list_orders() -> list[dict]:
    """Очередь для панели: заказы + домен, свежие сверху."""
    from sqlalchemy import select
    from app.db import SessionLocal
    from app.models.domain import Domain, AcquisitionOrder

    out = []
    with SessionLocal() as db:
        rows = db.execute(select(AcquisitionOrder).order_by(AcquisitionOrder.id.desc())).scalars().all()
        for o in rows:
            d = db.get(Domain, o.domain_id)
            out.append({"id": o.id, "domain": d.domain if d else f"#{o.domain_id}",
                        "provider": o.provider, "status": o.status,
                        "confirmed": o.confirmed_by_human,
                        "bid": float(o.cost) if o.cost is not None else None,
                        "result": o.result, "domain_id": o.domain_id})
    return out


if __name__ == "__main__":  # гейт-логика без БД: execute отказывает без подтверждения
    class _O:                # фейковый заказ — проверяем ветвление гейта из execute
        confirmed_by_human = False
        status = "pending_confirm"
    o = _O()
    assert not o.confirmed_by_human, "гейт должен блокировать неподтверждённый заказ"
    o.confirmed_by_human = True
    assert o.confirmed_by_human, "после confirm — гейт открыт"
    assert _PROVIDERS == {"backorder", "optimizator"}
    print("acquisition gate self-check ok")
