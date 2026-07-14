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
# Открытые статусы заказа — `OPEN_ORDER_STATUSES` в app/models/domain.py (оттуда же собран
# предикат уникального индекса: код и БД обязаны говорить об одном и том же).

# Через сколько минут claim `ordering` считается ТРУПОМ, а не живой отправкой.
#
# `ordering` живёт ровно столько, сколько execute ходит к провайдеру. Считаем ПОТОЛОК этого
# похода по таймаутам транспорта (integrations/base.py + backorder.py):
#   find_order -> client_orders -> _billmgr(retry=True): BaseClient.request = 3 попытки по
#     httpx-таймауту 30 с + бэкоффы (~2 с и ~4 с, wait_exponential max=10) ≈ 96 с;
#   order() -> _billmgr(retry=False): один запрос, тот же таймаут 30 с (ретрая нет намеренно —
#     повтор платного заказа = второе списание).
# Итого живой execute физически не переживает ~2 минут. 15 минут — семикратный запас (и та же
# величина, что STALE_MIN у job_run: «running без обновлений = контейнер убили»). Ошибиться
# ДОЛГИМ порогом дёшево: оператор подождёт и нажмёт «сверить» ещё раз. Ошибиться КОРОТКИМ —
# значит разобрать строку, которую прямо сейчас держит живой execute: он допишет свой исход
# поверх, а мы успеем открыть строке путь на повтор. Это прямой путь заплатить дважды.
STUCK_CLAIM_MIN = 15


def _claim_expired(o) -> bool:
    """Claim `ordering` протух: строку держит не живой execute, а труп процесса.

    NULL = claim СТАРОГО кода (до миграции 0011, колонки не было) — заведомо труп: та строка
    пережила деплой, который эту колонку и добавил, а деплой перезапускает контейнеры вместе со
    всеми execute в полёте.

    Дату нормализуем: SQLite отдаёт naive datetime, PostgreSQL — tz-aware; голое сравнение с
    now(tz) роняет TypeError (тот же приём — в jobs._is_stale и scoring.acquirability_verdict).
    """
    from datetime import datetime, timedelta, timezone

    t = getattr(o, "claimed_at", None)
    if t is None:
        return True
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t < datetime.now(timezone.utc) - timedelta(minutes=STUCK_CLAIM_MIN)


def _open_order_id(db, domain_id: int, except_id: int | None = None) -> int | None:
    """id ДРУГОГО открытого заказа на этот домен — или None. Гард инварианта БД.

    Индекс `uq_open_order_per_domain` запирает `pending_confirm|ordering|ordered` — а писателей,
    двигающих заказ В открытый статус, трое: `create_order` (новая заявка), `execute_confirmed_
    order` (ретрай `failed` -> `ordering`) и `poll_orders` (фантом `failed` -> `ordered`, заказ
    всё-таки долетел). Первый упирается в политику статусов домена, двум последним ничто не
    мешает: строка `failed` МОЖЕТ соседствовать с открытым заказом того же домена — легаси-дубли,
    которые старый код принимал до индекса и которые схлопывает миграция 0010 (выживший остаётся
    открытым, дубль уезжает в `failed`+`maybe_sent`). Слепой UPDATE ловил там IntegrityError:
    поллинг ронял ВСЮ пачку, ретрай — SQL-трейс в баннер панели (ревью Задачи 7).

    Спрашивать БД до записи, а не ловить IntegrityError постфактум, — потому что оба вызывающих
    обязаны СКАЗАТЬ человеку, почему движения не будет, а не просто уцелеть.
    """
    from sqlalchemy import select
    from app.models.domain import OPEN_ORDER_STATUSES, AcquisitionOrder

    q = select(AcquisitionOrder.id).where(
        AcquisitionOrder.domain_id == domain_id,
        AcquisitionOrder.status.in_(OPEN_ORDER_STATUSES))
    if except_id is not None:
        q = q.where(AcquisitionOrder.id != except_id)
    return db.execute(q).scalars().first()


def _unresolved_money_id(db, domain_id: int, except_id: int | None = None) -> int | None:
    """id заказа этого домена, про который известно «деньги МОГЛИ уйти» (`failed`+`maybe_sent`).

    ОТКРЫТЫМ такой заказ не считается (в индекс не входит) — и не должен: домен под ним и так
    заперт политикой статусов, он висит в `purchasing`, а заявку заводят только из `approved`.
    Но домен он ДЕРЖИТ, и вот зачем про него спрашивает `cancel_order`: у домена может оказаться
    ВТОРАЯ строка — открытый заказ (легаси-пара, которую схлопывает миграция 0010: выживший
    остаётся открытым, дубль уезжает в `failed`+`maybe_sent`). Сняв выжившего, отмена смотрела
    только на ОТКРЫТЫЕ заказы, дубль открытым не был — и домен уезжал в `approved`, то есть
    обратно в очередь выкупа, где его подтвердят и оплатят ВТОРОЙ раз, не зная, оплачен ли он
    уже. Ровно от этого `cancel_order` запирает отмену САМИХ maybe_sent-строк; через соседнюю
    строку запрет обходился (ревью Задачи 7, Important).

    Узником домен от этого не становится: у неизвестности есть выход, и он тот же, что у самой
    maybe_sent-строки, — «↻ обновить статусы у провайдера» (`poll_orders` спрашивает backorder по
    elid и снимает флаг ПРАВДОЙ провайдера) или «↻ повторить» (`execute` сперва зовёт find_order).
    Как только флаг снят, эта строка снимается обычной отменой и домен возвращается в `approved`.

    Ходим по строкам, а не фильтруем JSON в SQL: заказов у домена единицы, а `result->>'maybe_sent'`
    — диалект PG, тесты же крутятся на SQLite (правда БД обязана быть одна на обеих).
    """
    from sqlalchemy import select
    from app.models.domain import AcquisitionOrder

    q = select(AcquisitionOrder).where(AcquisitionOrder.domain_id == domain_id,
                                       AcquisitionOrder.status == "failed")
    if except_id is not None:
        q = q.where(AcquisitionOrder.id != except_id)
    for o in db.execute(q).scalars():
        if (o.result or {}).get("maybe_sent"):
            return o.id
    return None


def create_order(domain_id: int, provider: str = "backorder") -> int:
    """Поставить approved-домен в очередь выкупа (pending_confirm). Идемпотентно по домену.

    Возвращает id заказа (существующего открытого или нового). Не тратит денег —
    только заявка, ждущая подтверждения человеком."""
    from sqlalchemy.exc import IntegrityError
    from app.db import SessionLocal
    from app.models.domain import Domain, AcquisitionOrder
    from app.services import transitions

    if provider not in _PROVIDERS:
        raise ValueError(f"unknown provider {provider!r} (ожидается {_PROVIDERS})")
    with SessionLocal() as db:
        d = db.get(Domain, domain_id)
        if d is None:
            raise ValueError(f"domain {domain_id} not found")
        # ГРЯЗЬ — ПЕРВЫМ ДЕЛОМ, до поиска существующей заявки. Легаси-домен (грязный, попавший
        # в очередь ДО фикса) уже держит открытый заказ — и ранний возврат `existing.id` отдавал
        # бы его вызывающему как УСПЕХ: стадия `queue` автопилота честно считала такой домен
        # заявленным (done += 1), а оператор не слышал ни слова о том, что в очереди выкупа
        # лежит РКН-домен (ревью Задачи 6, Important 4). Денег это не тратит — но тихим успехом
        # быть не должно.
        transitions.refuse_dirty(d)
        existing = _open_order_id(db, domain_id)
        if existing is not None:
            return existing                         # уже в очереди — не дублируем
        # approved -> purchasing через политику (services/transitions): она проверяет и ИСХОДНЫЙ
        # статус (как раньше — только approved), и ГРЯЗЬ (ещё раз — set_status зовёт тот же
        # refuse_dirty). Отклонённый за РКН домен доезжал сюда отмытым кнопкой «↩ вернуть в
        # approved», которая reject_reason не стирала (аудит F9). Такие домены на живой базе
        # уже есть — вход в очередь для них закрыт.
        # ДО db.add: отказ политики не должен оставлять за собой заявку-призрак в сессии.
        transitions.set_status(d, "purchasing")      # видно в воронке: домен в очереди выкупа
        order = AcquisitionOrder(domain_id=domain_id, provider=provider,
                                 status="pending_confirm", confirmed_by_human=False)
        db.add(order)
        try:
            db.commit()
        except IntegrityError:
            # ПРОИГРАЛИ ГОНКУ. Между нашим SELECT и этим COMMIT заявку на тот же домен успел
            # закоммитить ДРУГОЙ писатель — их двое и в разных процессах (кнопка «в очередь» в
            # панели и стадия queue автопилотного свипа в воркере), а под READ COMMITTED чужая
            # незакоммиченная строка невидима: оба честно видели «заказа нет». Уникальный индекс
            # оставил в живых ровно один заказ — ведём себя ровно так, как если бы увидели его в
            # SELECT: отдаём чужой id. Домен победитель уже перевёл в purchasing (наш UPDATE
            # откатился вместе с INSERT), так что состояние согласовано.
            db.rollback()
            existing = _open_order_id(db, domain_id)
            if existing is None:                    # индекс сработал, а заказа нет — не наш случай
                raise
            return existing
        db.refresh(order)
        return order.id


def confirm_order(order_id: int, bid_rub: float | None = None) -> dict:
    """ЧЕЛОВЕК подтверждает выкуп — единственный путь поднять денежный гейт.

    `bid_rub` — СТАВКА. У backorder тариф и есть ставка (сетка 190 ₽ … 5 млн ₽): чем выше,
    тем выше шанс перехвата. «Сколько заплатить» — это решение о деньгах, поэтому его
    принимает человек здесь же, на гейте, а не система. Кладём в AcquisitionOrder.cost.

    Только ставит confirmed_by_human=True; заказ провайдеру НЕ шлёт (это execute)."""
    import math

    from app.db import SessionLocal
    from app.models.domain import Domain, AcquisitionOrder
    from app.services import transitions

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
        # ГРЯЗЬ НЕ ПОКУПАЕМ — и на самом гейте тоже, не только на входе в очередь (create_order).
        # Заявка на грязный домен могла быть заведена ДО этого фикса: тогда она уже лежит в
        # /queue, и именно ЗДЕСЬ, на кнопке «✓ подтвердить выкуп», человек тратит деньги.
        # Проверка идёт до pick_tariff: за грязный домен мы даже сетку тарифов не спрашиваем.
        if d is not None:
            transitions.refuse_dirty(d)
    if provider == "backorder" and not bid_rub:
        raise ValueError("backorder: не выбрана ставка (тариф) — без неё заказ отправить нельзя")
    # `not math.isfinite` ловит nan/inf/-inf: `not bid_rub` их не видит (nan truthy, `nan<=0`
    # ложно для ЛЮБОГО сравнения) — без этой проверки мусор долетал до pick_tariff и молча
    # оседал верхним тиром сетки (в проде 5 000 000 ₽). Проверка ДО сетевого pick_tariff —
    # решение о деньгах фиксируется здесь, а не в форме панели (Pydantic разбирает строки
    # "nan"/"inf"/"1e400" в float молча, гард в UI ничего бы не поймал).
    if bid_rub is not None and (not math.isfinite(bid_rub) or bid_rub <= 0):
        raise ValueError(f"ставка должна быть конечным числом больше нуля, получено {bid_rub}")

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
    from sqlalchemy.exc import IntegrityError
    from app.db import SessionLocal
    from app.models.domain import Domain, AcquisitionOrder
    from app.services import transitions

    with SessionLocal() as db:
        o = db.get(AcquisitionOrder, order_id)
        if o is None:
            raise ValueError(f"order {order_id} not found")
        if not o.confirmed_by_human:                 # ЖЁСТКИЙ ГЕЙТ — деньги не на автопилоте
            return {"order_id": order_id, "status": o.status,
                    "error": "gate: заказ не подтверждён человеком (confirmed_by_human=False)"}

        # ЗДЕСЬ И ЕСТЬ КАССА — и грязь про неё не спрашивали вовсе (ревью Задачи 6, Critical 1).
        # Заказ на грязный домен, подтверждённый ДО фикса (confirmed_by_human уже True), уходил
        # провайдеру и списывал деньги: гарды стояли на create_order/confirm_order, то есть на
        # входе и на гейте, а на САМОЙ ОТПРАВКЕ — ни одного. Единственной защитой было условие
        # в шаблоне queue.html, а прямой POST /queue/{id}/execute (старая вкладка, повтор после
        # failed) шёл мимо него.
        #
        # ДО атомарного claim'а: иначе заказ уже переведён в 'ordering', а отправка отменена —
        # заявка залипает в транзиентном статусе, из которого её не снять (cancel_order берёт
        # только pending_confirm/failed).
        d = db.get(Domain, o.domain_id)
        if d is not None:
            transitions.refuse_dirty(d)              # TransitionDenied -> роут покажет причину

        # ОДНА ОТКРЫТАЯ ЗАЯВКА НА ДОМЕН (uq_open_order_per_domain, см. _open_order_id).
        # «↻ повторить» двигает заказ из 'failed' в 'ordering' — ОТКРЫТЫЙ статус. Если домен
        # уже держит другой открытый заказ (схлопнутый дубль легаси-базы соседствует с
        # выжившим), claim ниже падал IntegrityError'ом МИМО try — SQL-трейсом в баннер панели.
        # Отвечаем человеку словами: повторять нечего, пока жив первый заказ. Выход у строки
        # есть — когда выживший закроется (пойман / не вышло / снят), повтор и поллинг снова
        # станут ей доступны.
        blocker = _open_order_id(db, o.domain_id, except_id=order_id)
        if blocker is not None:
            return {"order_id": order_id, "status": o.status,
                    "error": f"у домена уже есть открытый заказ #{blocker} — второй в полёте "
                             f"держать нельзя (это прямой путь заплатить дважды). Разбери "
                             f"#{blocker} («↻ обновить статусы»), потом вернись к этому."}

        # Атомарный claim: из двух параллельных кликов (sync-роуты в threadpool) в 'ordering'
        # переведёт РОВНО один — второй увидит rowcount 0 и не пошлёт второй живой заказ.
        # confirmed_by_human остаётся в SQL-условии — денежный гейт держится и здесь.
        # Допуск 'failed' в claim = рабочий ретрай (кнопка «↻ повторить»).
        #
        # claimed_at — ЧАСЫ НА CLAIM'Е (аудит F11). Без них 'ordering' — вечная камера: убитый в
        # момент отправки процесс оставлял строку, которую не берёт ни execute (claim пускает
        # только pending_confirm/failed), ни cancel, ни поллинг, а домен под ней навсегда заперт
        # в 'purchasing'. Отметка позволяет поллингу отличить ЖИВУЮ отправку (свежий claim —
        # руками не трогать, execute сейчас в полёте) от трупа (протух -> разбираем правдой
        # провайдера). Ставим В ТОМ ЖЕ UPDATE, что и статус: claim и его время — один факт.
        try:
            claim = db.execute(
                update(AcquisitionOrder)
                .where(AcquisitionOrder.id == order_id,
                       AcquisitionOrder.status.in_(("pending_confirm", "failed")),
                       AcquisitionOrder.confirmed_by_human.is_(True))
                .values(status="ordering", claimed_at=datetime.now(timezone.utc))
            )
            db.commit()
        except IntegrityError:
            # РЕМЕНЬ ПОВЕРХ ГАРДА (такой же, как у поллинга): гонку SELECT-гард не закрывает —
            # это разные запросы. Два клика «↻ повторить» по ДВУМ `failed`-строкам одного домена
            # (легаси-пара) оба честно видят «открытого заказа нет» и оба claim'ят 'ordering';
            # индекс оставляет одного. Проигравший обязан получить слова, а не SQL-трейс в баннер.
            db.rollback()
            other = _open_order_id(db, o.domain_id, except_id=order_id)
            return {"order_id": order_id, "status": db.get(AcquisitionOrder, order_id).status,
                    "error": (f"домен только что занял другой заказ "
                              f"{f'#{other} ' if other else ''}— второй в полёте держать нельзя "
                              f"(это прямой путь заплатить дважды). Разбери его "
                              f"(«↻ обновить статусы»), потом вернись к этому.")}
        if claim.rowcount != 1:                       # уже забрал другой клик / уже обработан
            db.refresh(o)
            return {"order_id": order_id, "status": o.status,
                    "note": "заказ уже обрабатывается или обработан"}
        db.refresh(o)
        # CLAIM ЗАКРЫВАЕТСЯ ВМЕСТЕ С ИСХОДОМ — одной строкой на все ветки ниже (их пять: дубль
        # усыновлён / отправлено / ambiguous / транспорта нет / сбой провайдера). Каждая из них
        # заканчивается db.commit(), и этот None уезжает в тот же UPDATE: строка вышла из
        # 'ordering', живого execute за ней больше нет. Отдельной строчкой в каждой ветке было бы
        # пять мест забыть. А если процесс умрёт ДО коммита — изменение просто не доедет до базы,
        # claimed_at останется стоять, и поллинг увидит труп ровно тогда, когда claim протухнет.
        o.claimed_at = None

        # Несём через ВСЕ ветки исхода:
        #  price_id/period_id — тариф, замороженный человеком на confirm (иначе «↻ повторить»
        #    после отказа теряет ставку и требует переподтверждения на ровном месте);
        #  maybe_sent — неизвестность НЕ должна сниматься сама. Если повтор снова упал (напр.
        #    провайдер лежит и find_order не ответил), флаг обязан выжить: иначе отмена снова
        #    разблокируется и реально оплаченный заказ можно спрятать. Снимает флаг только
        #    правда провайдера — успешный find_order/order или poll_orders.
        saved = {k: v for k, v in (o.result or {}).items()
                 if k in ("price_id", "period_id", "maybe_sent")}
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
                # Провайдер ОТВЕТИЛ — его список заказов и есть правда, неизвестности больше
                # нет. Снимаем флаг и когда дубль найден, и когда честно сказано «заказа нет»:
                # если мы доверяем этому ответу настолько, что на его основании ТРАТИМ деньги
                # (шлём новый заказ), то и разблокировать отмену он вправе. Иначе постоянный
                # отказ провайдера (приём закрыт / домен ушёл) запирал заявку навсегда: повтор
                # падает вечно, отмена заперта, а create_order не берёт домен из 'purchasing'.
                saved.pop("maybe_sent", None)
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

    ЗДЕСЬ ЖЕ — ЕДИНСТВЕННЫЙ ВЫХОД ИЗ ЗАСТРЯВШЕЙ ОТПРАВКИ (аудит F11). Строка в 'ordering', чей
    claim протух (процесс убили между claim'ом и ответом провайдера), не видна больше НИКОМУ:
    execute claim'ит только pending_confirm/failed, cancel снимает только их же. Разбираем её тем
    же способом, что и фантом-'failed', — ПРАВДОЙ ПРОВАЙДЕРА, а не догадкой: заказ у него есть →
    усыновляем (домен ловится, деньги не потеряны); заказа нет → 'failed', и человек волен
    повторить или снять. Свежий claim НЕ ТРОГАЕМ: за ним стоит живой execute, и отобрать у него
    строку значит открыть ей путь на повторную отправку — второе списание.
    """
    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError
    from app.db import SessionLocal
    from app.models.domain import Domain, AcquisitionOrder
    from app.integrations.backorder import BackorderClient, norm_domain

    remote_orders = BackorderClient().client_orders()
    by_elid = {r["elid"]: r for r in remote_orders if r["elid"]}
    # Фолбэк для строк без elid — по нормализованному домену (.РФ: фид кириллица, billmgr
    # punycode) и по ЖИВОМУ заказу: первый не-failed. Голый dict-comprehension схлопывал бы
    # историю домена в последнюю строку и мог усыновить фантому старый аннулированный заказ.
    by_domain: dict = {}
    for r in remote_orders:
        k = norm_domain(r["domain"])
        if k not in by_domain or (by_domain[k]["state"] == "failed" and r["state"] != "failed"):
            by_domain[k] = r
    moved = {"caught": 0, "failed": 0, "pending": 0}
    matched = 0
    conflicts = 0
    sending = 0                                       # живые отправки: их пропускаем, см. ниже
    lost = 0                                          # застрявшие отправки, о которых провайдер не знает
    with SessionLocal() as db:
        # 'failed' опрашиваем тоже: там может лежать ФАНТОМ — заказ, который на самом деле
        # ушёл (ambiguous-таймаут), и провайдер его знает. Иначе он невидим, а домен потерян.
        # 'ordering' — тоже: там лежит ЗАСТРЯВШАЯ отправка (процесс убили в момент заказа),
        # которую больше не разберёт никто (аудит F11).
        rows = db.execute(
            select(AcquisitionOrder).where(
                AcquisitionOrder.provider == "backorder",
                AcquisitionOrder.status.in_(("ordered", "failed", "ordering")))
        ).scalars().all()
        for o in rows:
            # ЖИВУЮ ОТПРАВКУ НЕ ТРОГАЕМ. Свежий claim = execute прямо сейчас в полёте у
            # провайдера (или вот-вот допишет исход). Разобрать такую строку значит вынести за
            # него вердикт и открыть строке путь на «↻ повторить» — а его заказ в это время
            # долетает и списывает деньги. Ждём, пока claim протухнет (STUCK_CLAIM_MIN): труп
            # никуда не убежит, а живой execute допишет исход сам.
            if o.status == "ordering" and not _claim_expired(o):
                sending += 1
                continue
            d = db.get(Domain, o.domain_id)
            # Матч по elid, а не по домену: заказов на один домен может быть несколько
            # (ретрай, ручной заказ из ЛК), и словарь по имени схлопнул бы их в последний —
            # свежий заказ получил бы статус протухшего.
            remote = by_elid.get(o.provider_order_id or "") or (
                by_domain.get(norm_domain(d.domain)) if d and not o.provider_order_id else None)
            if remote is None:
                if o.status != "ordering":
                    continue                          # провайдер ещё не показывает заказ
                # ЗАСТРЯВШАЯ ОТПРАВКА, КОТОРОЙ ПРОВАЙДЕР НЕ ЗНАЕТ. Он ОТВЕТИЛ (список заказов
                # пришёл) и этого домена в нём нет — значит заказа у него нет и деньги не ушли.
                # Это ровно тот ответ, на основании которого execute считает себя вправе ТРАТИТЬ
                # (find_order -> None -> шлём новый заказ): один источник правды, одно доверие к
                # нему. Отсюда и `maybe_sent` снимается, если он там был от прошлой ambiguous-
                # попытки: неизвестность снята той же правдой провайдера.
                # Ставим 'failed' — и у строки СНОВА ЕСТЬ ВЫХОД: «↻ повторить» (сперва спросит
                # провайдера) или «✗ отменить» (домен вернётся в approved). До этого фикса выхода
                # не было вовсе: домен оставался в 'purchasing' навечно.
                o.status = "failed"
                o.claimed_at = None                   # claim закрыт: живого execute за строкой нет
                o.result = {**{k: v for k, v in (o.result or {}).items() if k != "maybe_sent"},
                            "error": "отправка оборвалась (процесс перезапустили?), а провайдер "
                                     "этого заказа не знает — до него не долетело, деньги не "
                                     "ушли. Можно повторить или снять заявку."}
                db.commit()
                lost += 1
                continue
            matched += 1
            state = remote["state"]

            # ОДНА ОТКРЫТАЯ ЗАЯВКА НА ДОМЕН (uq_open_order_per_domain, см. _open_order_id).
            # Поднять 'failed' обратно в 'ordered' — это движение В ОТКРЫТЫЙ статус, и если домен
            # уже держит другой открытый заказ (схлопнутый дубль легаси-базы рядом с выжившим),
            # UPDATE ловил IntegrityError. Оставляем строку в 'failed' и говорим ЗАЧЕМ: заказ у
            # провайдера жив, мы его видим, но вторым открытым в нашей БД он быть не может.
            # Строка не мертва — когда выживший закроется, следующий поллинг поднимет и её.
            # `maybe_sent` НЕ снимаем: деньги за дубль могли уйти, и отмена обязана остаться
            # запертой (иначе реально оплаченный заказ можно спрятать). elid не усыновляем —
            # матч мог прийти по имени домена, а у домена сейчас ДВА заказа: чужой elid = ложь.
            # ...а вот 'ordering' спрашивать не о чем: он САМ открытый статус, то есть эта строка
            # и есть тот единственный открытый заказ домена (uq_open_order_per_domain это
            # гарантирует, а миграция 0010 схлопнула легаси-дубли). 'ordering' -> 'ordered'
            # остаётся внутри предиката индекса, второй открытой заявки не появляется. Ремень
            # (except IntegrityError ниже) всё равно на месте — гонку SELECT не закрывает.
            blocker = (_open_order_id(db, o.domain_id, except_id=o.id)
                       if state == "pending" and o.status == "failed" else None)
            try:
                if blocker is not None:
                    conflicts += 1
                    # Старую ошибку отправки выбрасываем: провайдер только что сказал, что заказ
                    # ЖИВ, — текст «не отправился» протух. Плюс в очереди колонка показывает
                    # `error or note` (queue.html), и стухшая ошибка перекрыла бы эту пометку —
                    # оператор так и не узнал бы, почему дубль не поднимается.
                    keep = {k: v for k, v in (o.result or {}).items() if k != "error"}
                    o.result = {**keep, "clear_status": remote["clear_status"],
                                "note": f"провайдер держит этот заказ в полёте, но у домена уже "
                                        f"есть открытый заказ #{blocker} — вторым открытым этот "
                                        f"быть не может (иначе платим дважды). Разбери #{blocker}; "
                                        f"когда он закроется, поллинг поднимет и этот."}
                    db.commit()
                    continue

                if not o.provider_order_id and remote["elid"]:
                    o.provider_order_id = remote["elid"]   # усыновляем фантом: теперь он отслеживаем
                o.result = {**(o.result or {}), "clear_status": remote["clear_status"]}
                o.result.pop("maybe_sent", None)      # неопределённость снята правдой провайдера
                o.claimed_at = None                   # исход есть -> claim закрыт (для 'ordering')
                if state == "caught":
                    o.status = "caught"
                    if d is not None:
                        d.status = "purchased"        # домен наш — путь в M3
                elif state == "failed":
                    o.status = "failed"
                else:
                    o.status = "ordered"              # в полёте (в т.ч. поднимает фантом из failed)
                # КОММИТ ПОСТРОЧНО, а не один на весь цикл. Одна больная строка (инвариант,
                # гонка с параллельным execute) не должна отменять синхронизацию ВСЕГО портфеля:
                # раньше IntegrityError на дубле ронял пачку целиком — ни один заказ не
                # обновлялся, и так на каждом нажатии. Заказов у портфеля десятки, не миллионы;
                # цена лишних коммитов ничтожна рядом с ценой потерянной сверки.
                db.commit()
                moved[state] = moved.get(state, 0) + 1
            except IntegrityError:                    # ремень поверх гарда: гонку он не закрывает
                db.rollback()                         # (SELECT и UPDATE — разные запросы)
                conflicts += 1
    # `sending`/`lost` — про застрявшие отправки (F11), и молчать о них нельзя: оператор жмёт
    # «сверить» ИМЕННО из-за такой строки. `sending` — «не тронули, там живой execute»,
    # `lost` — «разобрали: провайдер про заказ не знает». Без них поллинг отвечал бы «сверено 0»
    # и выглядел сломанным ровно в том случае, ради которого его и позвали.
    return {"checked": matched, "conflicts": conflicts, "sending": sending, "lost": lost, **moved}


def cancel_order(order_id: int) -> dict:
    """Снять заявку (pending_confirm/failed -> cancelled). Домен возвращаем в 'approved' только
    если по нему не осталось НИ открытого заказа, НИ заказа с неизвестным исходом. Денег не тратит.

    Снятие — УСЛОВНЫЙ UPDATE с проверкой rowcount (как claim в execute), а не ORM-запись по PK:
    иначе отмена, начатая до отправки, доезжает ПОСЛЕ неё и хоронит оплаченный заказ (аудит F12).
    """
    from sqlalchemy import update
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
            # cancelled из поллинга не опрашивается -> реально оплаченный заказ стал бы невидим
            # навсегда, а домен вернулся бы в approved. Выход из неизвестности — «↻ повторить»:
            # execute первым делом спрашивает провайдера (find_order) и либо усыновит уже
            # существующий заказ, либо честно отправит новый (значит первого не было).
            return {"order_id": order_id, "status": o.status,
                    "error": "исход заказа неизвестен (связь оборвалась, деньги могли уйти) — "
                             "нажми «↻ повторить»: сначала спросим провайдера, нет ли там заказа"}
        # Что ЕЩЁ держит домен, кроме снимаемой строки. Спрашиваем ДО мутации: except_id и так
        # исключает наш заказ, но состояние читается одно и то же и до, и после — так честнее.
        blocker = _open_order_id(db, o.domain_id, except_id=order_id)
        unresolved = _unresolved_money_id(db, o.domain_id, except_id=order_id)

        # ЗДЕСЬ ХОРОНИЛИ ОПЛАЧЕННЫЙ ЗАКАЗ (аудит F12). Проверки выше — это ЧТЕНИЕ, а `o.status =
        # 'cancelled'` было слепой ORM-записью по первичному ключу: UPDATE ... WHERE id=N, без
        # единого слова о том, в каком статусе строка была, когда мы решались. Между чтением и
        # записью помещается ВЕСЬ execute (кнопки «отправить» и «отменить» — два sync-роута,
        # панель гоняет их в threadpool параллельно; отправка ходит в сеть, отмена — нет, и легко
        # приезжает второй): заказ уходит провайдеру, деньги списываются, строка становится
        # 'ordered' — и наша отмена молча переписывает её в 'cancelled'. А 'cancelled' поллинг не
        # опрашивает: оплаченный заказ исчезает из машины навсегда, домен уезжает обратно в
        # approved и будет куплен ВТОРОЙ РАЗ.
        # Условие в SQL делает снятие атомарным: строку снимает тот, кто застал её открытой для
        # снятия. Проиграл — не пишем ничего и говорим человеку, что статус изменился.
        cancelled = db.execute(
            update(AcquisitionOrder)
            .where(AcquisitionOrder.id == order_id,
                   AcquisitionOrder.status.in_(("pending_confirm", "failed")))
            .values(status="cancelled")
        )
        if cancelled.rowcount != 1:
            db.rollback()
            fresh = db.get(AcquisitionOrder, order_id)
            return {"order_id": order_id, "status": fresh.status if fresh else "gone",
                    "note": "заказ изменил статус, пока мы его снимали (его отправили или уже "
                            "обработали) — снять можно только pending_confirm/failed"}
        # ...и ту же гонку перечитываем для `maybe_sent`: провайдер мог лечь, «↻ повторить» —
        # обернуться мгновенным AmbiguousSend (connect refused), и строка стать «деньги могли
        # уйти» между нашим db.get и этим UPDATE. Теперь строку держит НАШ замок (UPDATE взял её
        # до коммита), так что перечитанный result — последняя правда, а не гонка. Флаг всплыл —
        # откатываемся: прятать заказ, про который неизвестно, оплачен ли он, нельзя.
        db.expire(o)
        if (o.result or {}).get("maybe_sent"):
            db.rollback()
            fresh = db.get(AcquisitionOrder, order_id)
            return {"order_id": order_id, "status": fresh.status if fresh else "gone",
                    "error": "исход заказа неизвестен (связь оборвалась, деньги могли уйти) — "
                             "нажми «↻ повторить»: сначала спросим провайдера, нет ли там заказа"}
        d = db.get(Domain, o.domain_id)
        note = None
        if d is not None and d.status == "purchasing":
            if blocker is None and unresolved is None:
                d.status = "approved"                 # домен больше ничем не держится в очереди
                note = "домен возвращён в approved"
            elif blocker is not None:
                note = (f"домен остаётся в очереди (purchasing): его держит открытый заказ "
                        f"#{blocker}")
            else:
                # НЕ возвращаем в approved: рядом лежит строка «деньги МОГЛИ уйти». Вернуть домен
                # в очередь выкупа = позвать человека подтвердить и оплатить его второй раз, не
                # зная, оплачен ли он уже. Отмену самих maybe_sent-строк мы запрещаем ровно от
                # этого — через соседнюю строку запрет обходился (ревью Задачи 7, Important).
                # Путь разбора у человека есть — он назван прямо здесь, в тексте (см. также
                # _unresolved_money_id): правда провайдера снимает неизвестность.
                #
                # ВЫХОДА ДВА, и назвать надо ОБА. «↻ обновить статусы» ищет заказ у провайдера по
                # elid — но дубль, схлопнутый миграцией 0010 из `ordering`, до провайдера мог и не
                # долететь, elid'а у него нет, и поллинг такую строку просто не находит (checked:0).
                # Оператор жмёт названную кнопку, НИЧЕГО не происходит, и он остаётся без подсказки.
                # Второй выход рабочий и в этом случае: «↻ повторить» сперва зовёт find_order —
                # заказа у провайдера нет, флаг снимается, заказ уходит (гейт цел: жмёт человек).
                note = (f"домен остаётся в очереди (purchasing): у заказа #{unresolved} неизвестен "
                        f"исход — деньги могли уйти. Разбери его: «↻ обновить статусы у провайдера» "
                        f"спросит backorder по номеру заказа; если провайдер про заказ не знает "
                        f"(номера нет — до него не долетело), то «↻ повторить» — он переспросит и "
                        f"отправит заново. Как снимется неизвестность, заказ снимается, а домен "
                        f"возвращается в approved")
        db.commit()
        return {"order_id": order_id, "status": "cancelled", "domain_id": o.domain_id, "note": note}


def list_orders() -> list[dict]:
    """Очередь для панели: заказы + домен, свежие сверху.

    `dirty` — причина, по которой домен НЕЛЬЗЯ покупать (или None). Очередь — последний экран
    перед деньгами, и до этого фикса грязный домен выглядел здесь обычной строкой со ставкой:
    reject_reason в базе был, но в UI денежного пути его не показывал никто (аудит F13).

    `stuck` — отправка ЗАСТРЯЛА: claim протух, живого execute за строкой нет (F11). Отличать её
    от идущей прямо сейчас отправки обязана и очередь: «отправляется» и «отправка оборвалась,
    исход неизвестен» — разные новости для человека, у которого на кону деньги.
    """
    from sqlalchemy import select
    from app.db import SessionLocal
    from app.models.domain import Domain, AcquisitionOrder
    from app.services.transitions import dirty_reason

    out = []
    with SessionLocal() as db:
        rows = db.execute(select(AcquisitionOrder).order_by(AcquisitionOrder.id.desc())).scalars().all()
        for o in rows:
            d = db.get(Domain, o.domain_id)
            out.append({"id": o.id, "domain": d.domain if d else f"#{o.domain_id}",
                        "provider": o.provider, "status": o.status,
                        "confirmed": o.confirmed_by_human,
                        "bid": float(o.cost) if o.cost is not None else None,
                        "result": o.result, "domain_id": o.domain_id,
                        "dirty": dirty_reason(d) if d is not None else None,
                        "stuck": o.status == "ordering" and _claim_expired(o)})
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
