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
(заказ ушёл, ответ не дошёл) + кнопка «повторить» = второе списание. Каналов три: backorder (ставка, .RU/.РФ), optimizator (регистрация свободных) и «registrar» —
ШОВ под международный выкуп (integrations/registrar.py: протокол + заглушка «не настроен»; провайдера
выбирает оператор). Канал по умолчанию берётся из таблицы зона→канал (/settings, `zone_channels`).
Денежный гейт (confirm → TTL → execute, баланс, maybe_sent) ОДИН на все каналы.
"""
import logging

log = logging.getLogger(__name__)

_PROVIDERS = {"backorder", "optimizator", "registrar"}
AUCTION_SOURCE = "namesilo_auction"     # Domain.source: лот аукциона просроченных NameSilo (ставка, не регистрация)
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

# Общий дедлайн ручного опроса /queue/poll (сек): синхронный роут не должен вешать поток на ~93 с (S3-04).
POLL_DEADLINE_SEC = 20.0


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


class ChannelUnavailable(ValueError):
    """Для домена нет рабочего канала выкупа (зона вне канала / канал не настроен). Отказ ДО денег."""


def _zone_allowlist() -> list:
    from app.services.settings import get_settings
    return get_settings()["tld_allowlist"]


def _zone_channels() -> dict:
    from app.services.settings import get_settings
    return get_settings()["zone_channels"]


def channel_for(domain: str, provider: str | None = None) -> str:
    """Канал выкупа домена. `provider=None` — по таблице зона→канал (нет записи -> «registrar»);
    явный провайдер (кнопка «в очередь») принимается, если он совместим с зоной — это проверяет
    `check_route`, а не таблица: оператор вправе пустить свободный .com через optimizator."""
    if provider is not None:
        if provider not in _PROVIDERS:
            raise ValueError(f"unknown provider {provider!r} (ожидается {sorted(_PROVIDERS)})")
        return provider
    from app.services.domain_filters import zone_of
    return _zone_channels().get(zone_of(domain.lower())) or "registrar"


def check_route(d, provider: str) -> None:
    """Отказ ДО денег, если домен нельзя купить этим каналом: зона вне белого списка v2 (TransitionDenied)
    или канал её не продаёт / не настроен (ChannelUnavailable). Зовётся на create, confirm И execute —
    заказ, подтверждённый до смены правил (.ru после «РФ исключена»), не должен доехать до кассы (S3-06)."""
    from app.services import transitions
    transitions.refuse_closed_zone(d, _zone_allowlist())
    if provider == "backorder":
        from app.integrations.backorder import zone_of as bo_zone
        if bo_zone(d.domain) is None:
            raise ChannelUnavailable(
                f"«{d.domain}»: backorder продаёт только .RU/.РФ — для этой зоны канал другой "
                f"(таблица зона→канал в /settings)")
    elif provider == "registrar":
        from app.integrations.registrar import get_registrar
        if not get_registrar().configured:
            raise ChannelUnavailable(
                f"«{d.domain}»: канал международного выкупа не настроен (провайдера регистратора "
                f"выбирает оператор) — пока купить можно руками: «купил руками» на экране Домены")


def _scrub_text(s) -> str:
    """Секреты провайдеров — долой из любого текста, который попадёт в БД или на /queue (S3-05).
    Применяется и ПРИ ЗАПИСИ, и при показе: старые строки result.error уже могут нести api_key."""
    from app.integrations.optimizator import scrub
    from app.services.diagnostics import _scrub
    return _scrub(scrub(str(s)))


def _shortfall(o, bal) -> str | None:
    """Причина отказа ДО отправки заказа (S3-09). `bal` — (сумма, валюта) или None.
    Неизвестный баланс (None) — НЕ блокируем: судить нечем, провайдер откажет сам.
    Известный баланс блокирует: (1) валюта баланса пуста или не совпала с валютой заказа —
    суммы несравнимы, отказ; (2) сумма на счёте меньше замороженной стоимости — отказ."""
    if bal is None or o.cost is None:
        return None
    amount, cur = bal
    want = o.cost_currency or "RUB"
    if not cur or want != cur:
        # деньги: сравнить суммы в разных/неизвестных валютах нельзя — отказ ДО отправки,
        # а не молчаливый пропуск проверки
        return (f"валюта баланса у провайдера ({cur or 'не указана'}) не совпала с валютой заказа "
                f"({want}) — судить о достаточности средств нельзя; заказ не отправлен (деньги не ушли)")
    if float(amount) < float(o.cost):
        return (f"на счёте у провайдера {float(amount):.2f} {cur}, а заказ стоит {float(o.cost):.2f} {cur} — "
                f"пополни баланс и повтори (деньги не ушли)")
    return None


def _balance_of(client) -> float | None:
    """Баланс рублёвого канала или None, если провайдер его не отдал (капча, таймаут, нет поля)."""
    try:
        b = client.balance()
    except Exception:  # noqa: BLE001 — баланс информационный: недоступен -> не блокируем, судить нечем
        return None
    return float(b) if b is not None else None


def _confirm_expired(o) -> bool:
    """Подтверждение человека протухло (config.ACQ_CONFIRM_TTL_HOURS) или не имеет отметки времени
    (старый код) — тогда оно НЕ считается решением: цена/тариф устарели, нужно подтвердить заново."""
    from datetime import datetime, timedelta, timezone
    from app.config import settings

    t = getattr(o, "confirmed_at", None)
    if t is None:
        return True
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t < datetime.now(timezone.utc) - timedelta(hours=settings.ACQ_CONFIRM_TTL_HOURS)


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


def _settle(db, o, **values) -> bool:
    """Записать исход строки, ТОЛЬКО ЕСЛИ она не сдвинулась с того снимка, по которому мы судили.
    True — записали; False — строку у нас увели, и мы не пишем НИЧЕГО.

    Поллинг судит по СНИМКУ: строки выбираются одним SELECT'ом ДО цикла, а пишутся по одной, с
    коммитом на каждой (SessionLocal живёт с expire_on_commit=False — снимок сам не освежается).
    Пока мы решаем, ту же строку успевает заново заклеймить живой execute («↻ повторить»: два
    sync-роута панели, FastAPI гоняет их в threadpool ПАРАЛЛЕЛЬНО) — и наш вердикт становится
    вердиктом о прошлом.

    СТОРОЖИТЬ ОДИН СТАТУС МАЛО — ЭТО ABA (ревью Задачи 8, раунд 3). Прошлый сторож был условным
    UPDATE'ом `WHERE id=N AND status=<статус из снимка>`, и цикл `failed -> ordering -> failed`
    (параллельный повтор, кончившийся `AmbiguousSend`) возвращал строку в ТОТ ЖЕ статус: условие
    проходило, и поллинг дописывал своё поверх. А «своё» собрано из снимка, и оно роняло ДВА
    факта, которых в снимке не было:
      * `maybe_sent` — только что поставленный флаг «деньги МОГЛИ уйти». Основная ветка снимает его
        (`res.pop`) правдой провайдера — но правда эта прочитана ДО цикла (`client_orders()`), то
        есть ДО того, как повтор отправил новый заказ: про него она не знает НИЧЕГО. Флаг гас, с
        ним разблокировалась отмена (`cancel_order` запирает только maybe_sent-строки), домен
        уезжал в `approved` — обратно в очередь выкупа, где его подтвердят и оплатят ВТОРОЙ раз.
      * `provider_order_id` — усыновление elid'а («заказа у нас нет, а у провайдера по домену
        что-то лежит»). На оплаченную строку садился elid ЧУЖОГО, мёртвого заказа («Перекрыт» с
        прошлого цикла), и машина дальше следила не за тем заказом.
    Оба — РЕШЕНИЯ, принятые по снимку, а не значения, которые можно домержить к свежей строке:
    мержем `result` (перечитать и дописать) закрылся бы первый и остался бы второй. Поэтому
    сторожим ПОЛНЫЙ ПРООБРАЗ — всё, по чему выносился вердикт: статус, `result`, elid. Сдвинулось
    что-нибудь — молчим: следующая сверка возьмёт СВЕЖИЙ ответ провайдера (он уже будет знать про
    новый заказ) и рассудит строку правдой, а не догадкой. Строка не теряется — она остаётся в
    `ordered|failed|ordering`, то есть в выборке поллинга.

    Перечитываем СВОЕЙ ЖЕ сессией, и здесь две мины:
      * `populate_existing` ОБЯЗАТЕЛЕН. Без него `select()` в сессии, где объект уже загружен,
        отдаёт его из identity map, НЕ освежая атрибуты, — перечитка оказалась бы фикцией, а
        сторож бумажным (проверено прогоном, не верой).
      * `with_for_update()` — настоящий замок на PostgreSQL (держится до коммита, так что чтение
        прообраза и запись исхода — один атомарный акт), но на SQLite это NO-OP. Значит замок
        тестами НЕ доказывается; тестами доказывается СРАВНЕНИЕ ПРООБРАЗА (что мы не роняем чужой
        `maybe_sent`). Честно: от гонки в проде страхует замок, от гонки в тесте — сам факт, что
        мы перечитали строку после чужого коммита.
    """
    from sqlalchemy import select
    from app.models.domain import AcquisitionOrder

    # Прообраз — с ТОГО САМОГО объекта, по которому вынесены все решения выше. Снимаем ДО
    # перечитки: populate_existing перезапишет его атрибуты свежими.
    judged = (o.status, dict(o.result or {}), o.provider_order_id)
    fresh = db.execute(
        select(AcquisitionOrder)
        .where(AcquisitionOrder.id == o.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalars().first()
    if fresh is None:                                 # строки нет — писать некуда
        return False
    if (fresh.status, dict(fresh.result or {}), fresh.provider_order_id) != judged:
        return False                                  # строку увели: наш вердикт — о прошлом
    for k, v in values.items():
        setattr(fresh, k, v)                          # пишем под замком, коммитит вызывающий
    return True


def create_order(domain_id: int, provider: str | None = None) -> int:
    """Поставить approved-домен в очередь выкупа (pending_confirm). Идемпотентно по домену.

    `provider=None` — канал по зоне домена (таблица зона→канал); зона вне белого списка v2 или канал,
    который её не продаёт / не настроен, — отказ СРАЗУ, а не мёртвый pending_confirm (S3-06).

    Возвращает id заказа (существующего открытого или нового). Не тратит денег —
    только заявка, ждущая подтверждения человеком."""
    from sqlalchemy.exc import IntegrityError
    from app.db import SessionLocal
    from app.models.domain import Domain, AcquisitionOrder
    from app.services import transitions

    if provider is not None and provider not in _PROVIDERS:
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
        provider = channel_for(d.domain, provider)
        check_route(d, provider)                    # зона/канал — отказ ДО заявки и ДО денег
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

    Только ставит confirmed_by_human=True (+ confirmed_at — подтверждение ПРОТУХАЕТ через
    config.ACQ_CONFIRM_TTL_HOURS, S3-07); заказ провайдеру НЕ шлёт (это execute). Подтвердить можно
    и `failed`-заказ: так человек заново принимает решение после отказа или просрочки (цена и тариф
    перезамораживаются), а не «↻ повторяет» старое."""
    import math

    from app.db import SessionLocal
    from app.models.domain import Domain, AcquisitionOrder
    from app.services import transitions

    # Читаем состояние и валидируем, НЕ держа транзакцию открытой на время сетевого вызова.
    with SessionLocal() as db:
        o = db.get(AcquisitionOrder, order_id)
        if o is None:
            raise ValueError(f"order {order_id} not found")
        if o.status not in ("pending_confirm", "failed"):
            return {"order_id": order_id, "status": o.status,
                    "note": "подтвердить можно заказ в статусе pending_confirm/failed"}
        provider = o.provider
        d = db.get(Domain, o.domain_id)
        domain = d.domain if d else None
        source = d.source if d else None
        # ГРЯЗЬ НЕ ПОКУПАЕМ — и на самом гейте тоже, не только на входе в очередь (create_order).
        # Заявка на грязный домен могла быть заведена ДО этого фикса: тогда она уже лежит в
        # /queue, и именно ЗДЕСЬ, на кнопке «✓ подтвердить выкуп», человек тратит деньги.
        # Проверка идёт до pick_tariff: за грязный домен мы даже сетку тарифов не спрашиваем.
        if d is not None:
            transitions.refuse_dirty(d)
            check_route(d, provider)                 # зона/канал — до сети и до денег (S3-06)
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
    elif provider == "optimizator":
        # Цена ФИКСИРОВАНА (не выбор человека, в отличие от backorder-тира), но всё
        # равно показываем и фиксируем ДО отправки — денежный гейт требует видимой
        # суммы, а не "система решит на исполнении". price_id/period_id: None — это
        # backorder-специфичные поля (тир сетки тарифов), у optimizator их нет; ниже
        # существующий код обращается к tier["price_id"]/tier["period_id"]
        # БЕЗУСЛОВНО, когда tier is not None — без этих двух ключей он упал бы KeyError.
        from app.integrations.optimizator import (OptimizatorClient, OptimizatorError,
                                                  OptimizatorAmbiguous, ZoneNotSold, scrub)
        from app.services.domain_filters import zone_of
        if domain is None:
            raise ValueError(f"order {order_id}: домен не найден")
        zone = zone_of(domain)      # co.uk, а не последняя метка (S3-08)
        try:
            price = OptimizatorClient().prices(zone)
        except ZoneNotSold as e:
            raise ValueError(f"{domain}: {e} — зона не поддерживается этим каналом") from None
        except (OptimizatorError, OptimizatorAmbiguous) as e:
            raise ValueError(f"optimizator: цену зоны .{zone} получить не удалось: {scrub(e)}"[:200]) from None
        tier = {"price": price["price_registration"], "price_id": None, "period_id": None,
                "currency": price.get("currency") or "RUB"}
    elif provider == "registrar":
        from app.integrations.registrar import RegistrarAmbiguous, RegistrarError, get_registrar
        if domain is None:
            raise ValueError(f"order {order_id}: домен не найден")
        try:
            # аукцион просроченных NameSilo: котировка = текущая ставка (регистрация там недоступна)
            q = (get_registrar().price(domain, auction=True) if source == AUCTION_SOURCE
                 else get_registrar().price(domain))
        except (RegistrarError, RegistrarAmbiguous) as e:
            raise ValueError(f"registrar: {e}"[:200]) from None
        if not q.currency:
            raise ValueError(f"registrar: в котировке «{domain}» не указана валюта — "
                             f"сумму заказа заморозить нельзя")
        tier = {"price": q.amount, "price_id": None, "period_id": None, "currency": q.currency}
        if source == AUCTION_SOURCE:
            # Лот аукциона: списание = ПОТОЛОК ставки (решение человека, bid_rub; не ниже текущей) + год
            # продления (спека §6). Замораживаем ИМЕННО эту сумму, чтобы гейт, баланс и экран считали
            # полное списание, а не текущую ставку. Потолок и продление — в result (для execute).
            try:
                renew = get_registrar().renew_price(domain)
            except (RegistrarError, RegistrarAmbiguous) as e:
                raise ValueError(f"registrar: {e}"[:200]) from None
            if renew.currency != q.currency:
                raise ValueError(f"registrar: валюта продления ({renew.currency}) не совпала с валютой ставки "
                                 f"({q.currency}) — сумму списания не посчитать")
            ceiling = float(bid_rub) if bid_rub else float(q.amount)
            if ceiling < float(q.amount):
                raise ValueError(f"registrar: потолок ставки {ceiling:.2f} {q.currency} ниже текущей "
                                 f"{float(q.amount):.2f} — подними потолок")
            tier["price"] = ceiling + float(renew.amount)
            tier["auction"] = {"auction_ceiling": ceiling, "renew": float(renew.amount)}

    from datetime import datetime, timezone
    with SessionLocal() as db:
        o = db.get(AcquisitionOrder, order_id)
        if o is None or o.status not in ("pending_confirm", "failed"):   # состояние сменилось, пока ходили в сеть
            return {"order_id": order_id, "status": o.status if o else "gone",
                    "note": "не в статусе pending_confirm/failed"}
        if tier is not None:
            o.cost = tier["price"]                   # ФАКТИЧЕСКИЙ тир, а не желаемая сумма
            o.cost_currency = tier.get("currency") or "RUB"   # тиры backorder — рубли
            o.result = {**(o.result or {}),
                        "price_id": tier["price_id"], "period_id": tier["period_id"],
                        **(tier.get("auction") or {})}
        elif bid_rub is not None:
            o.cost = bid_rub
            o.cost_currency = o.cost_currency or "RUB"
        o.confirmed_by_human = True                  # HARD GATE поднят человеком
        o.confirmed_at = datetime.now(timezone.utc)  # …и протухнет через ACQ_CONFIRM_TTL_HOURS
        db.commit()
        return {"order_id": order_id, "status": o.status, "confirmed_by_human": True,
                "bid_rub": float(o.cost) if o.cost is not None else None,
                "currency": o.cost_currency}


def execute_confirmed_order(order_id: int) -> dict:
    """Отправить подтверждённый заказ провайдеру. ГЕЙТ: только при confirmed_by_human.

    Провайдерский транспорт .order() требует login-кредов и пока не реализован —
    в этом случае помечаем 'failed' с причиной, а не выдаём ложный успех."""
    from datetime import datetime, timezone
    from sqlalchemy import update
    from sqlalchemy.exc import IntegrityError
    from app.db import SessionLocal
    from datetime import timedelta
    from app.config import settings
    from app.models.domain import Domain, AcquisitionOrder
    from app.services import transitions

    with SessionLocal() as db:
        o = db.get(AcquisitionOrder, order_id)
        if o is None:
            raise ValueError(f"order {order_id} not found")
        if not o.confirmed_by_human:                 # ЖЁСТКИЙ ГЕЙТ — деньги не на автопилоте
            return {"order_id": order_id, "status": o.status,
                    "error": "gate: заказ не подтверждён человеком (confirmed_by_human=False)"}
        if _confirm_expired(o):
            # Подтверждение протухло: опускаем гейт ЯВНО (в UI вернётся «✓ подтвердить»), а не просто
            # отказываем — иначе «↻ повторить» висела бы рядом с вечным отказом.
            o.confirmed_by_human = False
            db.commit()
            return {"order_id": order_id, "status": o.status,
                    "error": f"подтверждение просрочено (больше {settings.ACQ_CONFIRM_TTL_HOURS} ч) — цена и "
                             f"тариф могли измениться; подтверди заказ заново"}

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
            check_route(d, o.provider)               # зона/канал на КАССЕ: подтверждённый до смены правил заказ не уходит

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
                       AcquisitionOrder.confirmed_by_human.is_(True),
                       AcquisitionOrder.confirmed_at >= datetime.now(timezone.utc)
                       - timedelta(hours=settings.ACQ_CONFIRM_TTL_HOURS))
                .values(status="ordering", claimed_at=datetime.now(timezone.utc))
                .execution_options(synchronize_session=False)   # WHERE c datetime: ORM-evaluator не сравнивает naive/aware; ниже db.refresh(o)
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
                 if k in ("price_id", "period_id", "maybe_sent", "registrar_ctx",
                                    "auction_ceiling", "renew")}
        bought_now = False      # регистратор ответил «зарегистрирован» синхронно: это уже покупка, не заявка
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

                bal = _balance_of(c)
                short = _shortfall(o, (bal, "RUB") if bal is not None else None)
                if short:
                    o.status = "failed"
                    o.result = {**saved, "error": short}
                    db.commit()
                    return {"order_id": order_id, "status": "failed", **o.result}
                try:
                    res = c.order(d.domain, price_id=price_id, period_id=period_id)
                except AmbiguousSend as e:
                    # Исход НЕИЗВЕСТЕН (таймаут / 5xx / не-JSON) — заказ мог уйти и деньги
                    # списаться. Не предлагаем «повторить» вслепую: сначала опрос провайдера.
                    # Явный отказ провайдера сюда НЕ попадает — он RuntimeError ниже.
                    o.status = "failed"
                    o.result = {**saved, "error": f"исход неизвестен: {_scrub_text(e)}", "maybe_sent": True}
                    db.commit()
                    return {"order_id": order_id, "status": "failed", **o.result}
            elif o.provider == "optimizator":
                from app.integrations.optimizator import OptimizatorClient, OptimizatorError, OptimizatorAmbiguous
                c = OptimizatorClient()
                # ИДЕМПОТЕНТНОСТЬ. У API нет «список заказов»/«заказ по домену» (в отличие
                # от backorder.find_order) — единственная замена: check_domain успешен
                # ТОЛЬКО для доменов под нашей анкетой. Успех = «уже наш», второй
                # reg_domains не шлём. ЧИСТЫЙ отказ провайдера (OptimizatorError, напр. "домен
                # не найден под нашей анкетой") безопасно трактуем как "не наш" и продолжаем к
                # register() — если домен и правда уже наш, ответит сам reg_domains (его
                # OptimizatorError упадёт в except ниже). НЕОПРЕДЕЛЁННОСТЬ (OptimizatorAmbiguous
                # — сеть/формат, а не ответ провайдера) — это НЕ "подтверждённо не наш": слать
                # register() поверх неё рискованно (а вдруг уже наш и это повторная попытка),
                # поэтому она обрывает ветку СРАЗУ, не доходя до register() (см. except ниже).
                try:
                    existing = c.check_domain(d.domain)
                except OptimizatorError:
                    existing = None
                except OptimizatorAmbiguous as e:
                    # НЕ смогли проверить — human должен решить сам, а не мы вслепую. НЕ шлём
                    # register(), падаем в failed с заблокированным retry-без-проверки (тот же
                    # контракт, что у самого register(): неизвестность -> maybe_sent -> нельзя
                    # тихо отменить). `saved` НЕ трогаем — maybe_sent от прошлой попытки (если
                    # была) остаётся вытесненным явным True ниже в любом случае.
                    o.status = "failed"
                    o.result = {**saved, "error": f"не удалось проверить владение доменом "
                                                  f"(check_domain): {_scrub_text(e)}", "maybe_sent": True}
                    db.commit()
                    return {"order_id": order_id, "status": "failed", **o.result}
                # Дошли сюда ТОЛЬКО через успех/OptimizatorError выше — что-то реально узнали
                # (домен наш или домен не наш), неизвестности нет, снимаем флаг прошлой ambiguous.
                saved.pop("maybe_sent", None)
                if existing is not None:
                    o.status = "ordered"
                    o.result = {**saved, "note": "домен уже под нашей анкетой (check_domain) — "
                                                 "второй reg_domains не шлём",
                                "data_end": existing.get("data_end")}
                    o.ordered_at = o.ordered_at or datetime.now(timezone.utc)
                    db.commit()
                    return {"order_id": order_id, "status": o.status, "result": o.result}
                # ПОТОЛОК ЦЕНЫ (S3-07): register() цены не получает и платит текущую — сверяем с
                # замороженной на confirm суммой и баланс, всё ДО отправки (чистый отказ, деньги целы).
                from app.services.domain_filters import zone_of
                cur_price = c.prices(zone_of(d.domain))["price_registration"]
                if o.cost is not None and float(cur_price) > float(o.cost):
                    o.status = "failed"
                    o.result = {**saved, "error": f"цена у провайдера выросла: {float(cur_price):.2f} > "
                                                  f"подтверждённых {float(o.cost):.2f} — подтверди заказ заново"}
                    db.commit()
                    return {"order_id": order_id, "status": "failed", **o.result}
                bal = _balance_of(c)
                short = _shortfall(o, (bal, "RUB") if bal is not None else None)
                if short:
                    o.status = "failed"
                    o.result = {**saved, "error": short}
                    db.commit()
                    return {"order_id": order_id, "status": "failed", **o.result}
                try:
                    res = c.register([d.domain])
                except OptimizatorAmbiguous as e:
                    o.status = "failed"
                    o.result = {**saved, "error": f"исход неизвестен: {_scrub_text(e)}", "maybe_sent": True}
                    db.commit()
                    return {"order_id": order_id, "status": "failed", **o.result}
                # OptimizatorError (чистый отказ) падает в общий except Exception ниже —
                # деньги не ушли, "↻ повторить" безопасен, сообщение уже читаемое
                # (OptimizatorError.__str__ несёт error_id).
            else:
                # «registrar» — ШОВ под международный выкуп (integrations/registrar.get_registrar).
                # Тот же гейт, что выше (confirm → TTL → claim); сам регистратор ИДЕМПОТЕНТЕН
                # (Registrar.register: домен уже наш -> успех без второго списания).
                from app.integrations.registrar import RegistrarAmbiguous, get_registrar
                r = get_registrar()
                is_auction = d.source == AUCTION_SOURCE
                q = r.price(d.domain, auction=True) if is_auction else r.price(d.domain)
                if o.cost is not None and (not q.currency or q.currency != (o.cost_currency or "RUB")):
                    # валюта котировки не совпала с подтверждённой (или не указана): потолок цены
                    # проверить нечем — НЕ молчим, отказ ДО отправки
                    o.status = "failed"
                    o.result = {**saved, "error": f"валюта котировки регистратора ({q.currency or 'не указана'}) "
                                                  f"не совпала с подтверждённой ({o.cost_currency or 'RUB'}) — "
                                                  f"потолок цены не проверить; подтверди заказ заново"}
                    db.commit()
                    return {"order_id": order_id, "status": "failed", **o.result}
                # лот аукциона: o.cost = потолок + продление, а сравнивать текущую ставку надо с ПОТОЛКОМ
                ceiling = float(saved["auction_ceiling"]) if is_auction and saved.get("auction_ceiling") is not None else None
                if is_auction and ceiling is None:
                    o.status = "failed"
                    o.result = {**saved, "error": "в заказе нет замороженного потолка ставки — подтверди заказ заново"}
                    db.commit()
                    return {"order_id": order_id, "status": "failed", **o.result}
                if is_auction:
                    rn = r.renew_price(d.domain)
                    if rn.currency != q.currency or float(rn.amount) > float(saved.get("renew") or 0):
                        o.status = "failed"
                        o.result = {**saved, "error": f"цена продления у регистратора изменилась "
                                                      f"({float(rn.amount):.2f} {rn.currency}) — подтверди заказ заново"}
                        db.commit()
                        return {"order_id": order_id, "status": "failed", **o.result}
                limit = ceiling if is_auction else (float(o.cost) if o.cost is not None else None)
                if limit is not None and float(q.amount) > limit:
                    o.status = "failed"
                    o.result = {**saved, "error": f"цена у регистратора выросла: {float(q.amount):.2f} "
                                                  f"{q.currency} > подтверждённых {limit:.2f} — "
                                                  f"подтверди заказ заново"}
                    db.commit()
                    return {"order_id": order_id, "status": "failed", **o.result}
                bal = r.balance()
                short = _shortfall(o, tuple(bal) if bal is not None else None)
                if short:
                    o.status = "failed"
                    o.result = {**saved, "error": short}
                    db.commit()
                    return {"order_id": order_id, "status": "failed", **o.result}
                # контекст для сверки неизвестного исхода (баланс ДО и момент отправки) — пишется
                # вместе с maybe_sent; _poll_registrar судит по нему правдой регистратора
                ctx = {"balance_before": float(bal.amount) if bal is not None else None,
                       "sent_at": datetime.now(timezone.utc).isoformat()}
                try:
                    if is_auction:       # ставка на аукционе: потолок — подтверждённая человеком сумма
                        res = r.bid(d.domain, ceiling)
                    else:
                        res = r.register(d.domain, 1)
                except RegistrarAmbiguous as e:
                    o.status = "failed"
                    o.result = {**saved, "error": f"исход неизвестен: {_scrub_text(e)}", "maybe_sent": True,
                                "registrar_ctx": {**ctx, "auction": is_auction}}
                    db.commit()
                    return {"order_id": order_id, "status": "failed", **o.result}
                saved.pop("maybe_sent", None)     # регистратор ответил успехом: неопределённости нет
                # registerDomain у регистратора СИНХРОННЫЙ: ответ 300 = домен уже наш. Держать его в
                # `ordered` и ждать ручного «✓ пойман» — лишний клик и лишняя пауза в цикле. Ставка на
                # аукционе — другое дело: итог известен позже, остаётся `ordered`.
                bought_now = not is_auction
            o.status = "caught" if bought_now else "ordered"
            o.provider_order_id = str(res.get("order_id") or "") if isinstance(res, dict) else ""
            o.result = {**saved, **(res if isinstance(res, dict) else {"raw": str(res)})}
            o.ordered_at = datetime.now(timezone.utc)
            if bought_now:
                d.status = "purchased"            # запись факта (см. transitions.py): деньги уже списаны
            db.commit()
            if bought_now:
                _site_after_purchase(o.domain_id)
            return {"order_id": order_id, "status": o.status, "result": o.result}
        except NotImplementedError:
            o.status = "failed"
            o.result = {**saved, "error": f"провайдер {o.provider}: транспорт заказа не реализован"}
            db.commit()
            return {"order_id": order_id, "status": "failed", **o.result}
        except Exception as e:  # noqa: BLE001 — сбой провайдера -> failed, не 500
            o.status = "failed"
            o.result = {**saved, "error": _scrub_text(f"{type(e).__name__}: {e}")[:200]}
            db.commit()
            return {"order_id": order_id, "status": "failed", **o.result}


def _site_after_purchase(domain_id: int) -> int | None:
    """Домен куплен -> сразу карточка сайта (M3), без отдельного клика «создать сайт». Шов между M2 и
    M3: раньше `purchased`-домен лежал, пока оператор не найдёт кнопку на /domains/pool или не включит
    стадию provision автопилота. Сбой здесь НЕ роняет фиксацию покупки (она уже в БД) — сайт создаст
    стадия provision или кнопка; денег этот шаг не тратит."""
    try:
        from app.services import provisioning
        return provisioning.create_site_for(domain_id)
    except Exception as e:  # noqa: BLE001
        log.warning("сайт для domain#%s не создан автоматически: %s: %s", domain_id, type(e).__name__, e)
        return None


def mark_caught(order_id: int) -> dict:
    """ЧЕЛОВЕК подтверждает факт поимки: заказ 'ordered' -> 'caught', домен -> 'purchased'.

    Для backorder и аукциона поимка асинхронна (провайдер ловит домен на удалении / торги идут),
    потому факт фиксируем руками. С этого момента домен куплен — карточка сайта (M3) заводится сразу."""
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
        domain_id = o.domain_id
    site_id = _site_after_purchase(domain_id)
    return {"order_id": order_id, "status": "caught", "domain_id": domain_id, "site_id": site_id}


def _poll_backorder() -> dict:
    """Сверка заказов backorder с правдой провайдера (см. poll_orders). Сеть — ТОЛЬКО если у нас есть
    что сверять (S3-04): строки провайдера в ordered/failed/ordering."""
    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError
    from app.db import SessionLocal
    from app.models.domain import Domain, AcquisitionOrder
    from app.integrations.backorder import BackorderClient, norm_domain

    with SessionLocal() as db:
        if not db.execute(select(AcquisitionOrder.id).where(
                AcquisitionOrder.provider == "backorder",
                AcquisitionOrder.status.in_(("ordered", "failed", "ordering"))).limit(1)).first():
            return {"checked": 0, "conflicts": 0, "sending": 0, "lost": 0,
                    "caught": 0, "failed": 0, "pending": 0}
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
    caught_ids: list[int] = []
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
            # СНИМОК, ПО КОТОРОМУ СУДИМ, — ОН ЖЕ УСЛОВИЕ ЗАПИСИ (см. _settle). Всё, что ниже,
            # решается по строке, прочитанной ДО цикла; пока мы решаем, её может заново заклеймить
            # живой execute («↻ повторить»). Ни одно решение не уезжает в БД слепой записью: все
            # три ветки исхода (lost / conflict / основная) идут через `_settle`, а он перед
            # записью сверяет ПОЛНЫЙ прообраз строки — статус, `result`, elid, — потому что цикл
            # `failed -> ordering -> failed` возвращает её в тот же статус (ABA) и сторожа по
            # одному статусу проходит насквозь.
            was = o.status
            d = db.get(Domain, o.domain_id)
            # Матч по elid, а не по домену: заказов на один домен может быть несколько
            # (ретрай, ручной заказ из ЛК), и словарь по имени схлопнул бы их в последний —
            # свежий заказ получил бы статус протухшего.
            remote = by_elid.get(o.provider_order_id or "") or (
                by_domain.get(norm_domain(d.domain)) if d and not o.provider_order_id else None)

            values: dict = {}
            done = ""                                 # что сделали со строкой — считаем ПОСЛЕ записи
            if remote is None:
                if was != "ordering":
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
                values = {"status": "failed",
                          "claimed_at": None,         # claim закрыт: живого execute за строкой нет
                          "result": {**{k: v for k, v in (o.result or {}).items()
                                        if k != "maybe_sent"},
                                     "error": "отправка оборвалась (процесс перезапустили?), а "
                                              "провайдер этого заказа не знает — до него не "
                                              "долетело, деньги не ушли. Можно повторить или "
                                              "снять заявку."}}
                done = "lost"
            else:
                state = remote["state"]
                # ОДНА ОТКРЫТАЯ ЗАЯВКА НА ДОМЕН (uq_open_order_per_domain, см. _open_order_id).
                # Поднять 'failed' обратно в 'ordered' — это движение В ОТКРЫТЫЙ статус, и если
                # домен уже держит другой открытый заказ (схлопнутый дубль легаси-базы рядом с
                # выжившим), UPDATE ловил IntegrityError. Оставляем строку в 'failed' и говорим
                # ЗАЧЕМ: заказ у провайдера жив, мы его видим, но вторым открытым в нашей БД он
                # быть не может. Строка не мертва — когда выживший закроется, следующий поллинг
                # поднимет и её. `maybe_sent` НЕ снимаем: деньги за дубль могли уйти, и отмена
                # обязана остаться запертой (иначе реально оплаченный заказ можно спрятать). elid
                # не усыновляем — матч мог прийти по имени домена, а у домена сейчас ДВА заказа:
                # чужой elid = ложь.
                # ...а вот 'ordering' спрашивать не о чем: он САМ открытый статус, то есть эта
                # строка и есть тот единственный открытый заказ домена (uq_open_order_per_domain
                # это гарантирует, а миграция 0010 схлопнула легаси-дубли). 'ordering' -> 'ordered'
                # остаётся внутри предиката индекса, второй открытой заявки не появляется. Ремень
                # (except IntegrityError ниже) всё равно на месте — гонку SELECT не закрывает.
                blocker = (_open_order_id(db, o.domain_id, except_id=o.id)
                           if state == "pending" and was == "failed" else None)
                if blocker is not None:
                    # Старую ошибку отправки выбрасываем: провайдер только что сказал, что заказ
                    # ЖИВ, — текст «не отправился» протух. Плюс в очереди колонка показывает
                    # `error or note` (queue.html), и стухшая ошибка перекрыла бы эту пометку —
                    # оператор так и не узнал бы, почему дубль не поднимается.
                    keep = {k: v for k, v in (o.result or {}).items() if k != "error"}
                    values = {"result": {
                        **keep, "clear_status": remote["clear_status"],
                        "note": f"провайдер держит этот заказ в полёте, но у домена уже есть "
                                f"открытый заказ #{blocker} — вторым открытым этот быть не может "
                                f"(иначе платим дважды). Разбери #{blocker}; когда он закроется, "
                                f"поллинг поднимет и этот."}}
                    done = "conflict"                 # статус не двигаем: строка остаётся 'failed'
                else:
                    res = {**(o.result or {}), "clear_status": remote["clear_status"]}
                    res.pop("maybe_sent", None)       # неопределённость снята правдой провайдера
                    # 'pending' у провайдера -> 'ordered' у нас: заказ в полёте (так же поднимается
                    # и фантом из 'failed', и застрявшая отправка из 'ordering').
                    values = {"result": res,
                              "claimed_at": None,     # исход есть -> claim закрыт (для 'ordering')
                              "status": {"caught": "caught",
                                         "failed": "failed"}.get(state, "ordered")}
                    if not o.provider_order_id and remote["elid"]:
                        values["provider_order_id"] = remote["elid"]   # усыновляем фантом
                    done = state
            try:
                if not _settle(db, o, **values):
                    # СТРОКУ УВЕЛИ ИЗ-ПОД СНИМКА, пока мы решали, — и решали мы, стало быть, про
                    # прошлое. Не пишем ничего: тот, кто её забрал, допишет свой исход сам.
                    db.rollback()
                    db.expire(o)                      # чем строка стала на самом деле
                    if o.status == "ordering":
                        sending += 1                  # её заклеймил живой execute — отправка в полёте
                    continue                          # ушла в ordered/cancelled/обратно в failed с
                                                      # новым исходом — это видно в очереди
                if done == "caught" and d is not None:
                    d.status = "purchased"            # домен наш — путь в M3
                    caught_ids.append(d.id)
                # КОММИТ ПОСТРОЧНО, а не один на весь цикл. Одна больная строка (инвариант,
                # гонка с параллельным execute) не должна отменять синхронизацию ВСЕГО портфеля:
                # раньше IntegrityError на дубле ронял пачку целиком — ни один заказ не
                # обновлялся, и так на каждом нажатии. Заказов у портфеля десятки, не миллионы;
                # цена лишних коммитов ничтожна рядом с ценой потерянной сверки.
                db.commit()
            except IntegrityError:                    # ремень поверх гарда: гонку он не закрывает
                db.rollback()                         # (SELECT и UPDATE — разные запросы)
                matched += 1
                conflicts += 1
                continue
            if done == "lost":
                lost += 1
            elif done == "conflict":
                matched += 1
                conflicts += 1
            else:
                matched += 1
                moved[done] = moved.get(done, 0) + 1
    for did in caught_ids:
        _site_after_purchase(did)                     # пойман -> карточка сайта сразу (шов M2->M3)
    return {"checked": matched, "conflicts": conflicts, "sending": sending, "lost": lost, **moved}


def _poll_optimizator(deadline: float) -> dict:
    """Восстановление зависших отправок optimizator (см. poll_orders). Сеть — только на строки
    `ordering` с протухшим claim; общий дедлайн опроса: не успели — остальное при следующей сверке."""
    import time
    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError
    from app.db import SessionLocal
    from app.models.domain import Domain, AcquisitionOrder

    matched = 0
    conflicts = 0
    sending = 0

    # --- optimizator: застрявший 'ordering' (тот же F11-класс, второй канал, см. докстринг выше) --
    # ДО транспорта (Task 1/2) execute для optimizator падал ДО сети (NotImplementedError) — окна
    # для застревания не было. Теперь оно есть: тот же риск, что у backorder, только источник
    # правды другой — у optimizator нет client_orders()/find_order (списка заказов), только
    # check_domain(domain) по одному домену за раз (design doc §3). Цикл СТРУКТУРНО зеркалит
    # backorder-цикл выше: свой снимок SELECT'ом до цикла, тот же пропуск свежего claim'а
    # (_claim_expired), запись ТОЛЬКО через _settle (тот же ABA-гард — F12), тот же ремень
    # IntegrityError поверх. `sending`/`matched` — те же переменные, что и у backorder-цикла: одна
    # правда для отчёта, независимо от канала.
    from app.integrations.optimizator import OptimizatorClient, OptimizatorError, OptimizatorAmbiguous

    with SessionLocal() as db:
        opt_rows = db.execute(
            select(AcquisitionOrder).where(
                AcquisitionOrder.provider == "optimizator",
                AcquisitionOrder.status == "ordering")
        ).scalars().all()
        oc = OptimizatorClient(quick=True)   # один запрос, 8 с: без ретраев 3 x 30 с
        for o in opt_rows:
            if time.monotonic() > deadline:
                raise TimeoutError(f"общий дедлайн опроса ({POLL_DEADLINE_SEC:.0f} с) вышел — остальные "
                                   f"строки разберёт следующая сверка")
            # Статус уже 'ordering' у ВСЕХ строк снимка (SQL-фильтр выше) — проверять его здесь
            # незачем, в отличие от backorder-цикла (там снимок смешивает ordered/failed/ordering).
            if not _claim_expired(o):
                sending += 1                          # живой execute в полёте — не трогаем
                continue
            d = db.get(Domain, o.domain_id)
            # check_domain различает ЧИСТЫЙ отказ провайдера (OptimizatorError — "домен не наш",
            # можно судить строку прямо сейчас) от НЕОПРЕДЕЛЁННОСТИ (OptimizatorAmbiguous —
            # сеть/формат, провайдер вообще не ответил). Отказ -> 'failed' ниже, человек увидит
            # строку в /queue и решит сам (design doc §3). Неизвестность — консервативно НЕ
            # выносим вердикт вообще: ложный 'failed' здесь опасен ровно как в execute, он
            # разблокировал бы отмену заказа, чья судьба на самом деле неизвестна. Строку не
            # трогаем — следующий прогон поллинга попробует снова.
            try:
                existing = oc.check_domain(d.domain) if d is not None else None
            except OptimizatorError:
                existing = None
            except OptimizatorAmbiguous:
                continue
            if existing is not None:
                # УСПЕХ: регистрация прошла до обрыва. 'caught'-пути здесь нет (в отличие от
                # backorder) — у optimizator нет фазы «поймали, но не наши»: домен либо уже под
                # нашей анкетой, либо нет. d.status НЕ трогаем: он уже 'purchasing', человек сам
                # переведёт в 'purchased' через mark_caught — симметрично backorder.
                values: dict = {
                    "status": "ordered", "claimed_at": None,
                    "result": {**(o.result or {}), "data_end": existing.get("data_end"),
                               "note": "восстановлено после обрыва: check_domain подтвердил "
                                       "регистрацию"}}
            else:
                values = {
                    "status": "failed", "claimed_at": None,
                    "result": {**{k: v for k, v in (o.result or {}).items() if k != "maybe_sent"},
                               "error": "отправка оборвалась (процесс перезапустили?), а "
                                        "check_domain регистрацию не подтвердил — деньги, "
                                        "возможно, не ушли. Можно повторить или снять заявку."}}
            try:
                if not _settle(db, o, **values):
                    # СТРОКУ УВЕЛИ ИЗ-ПОД СНИМКА — тот же ABA-гард, что и у backorder-цикла: не
                    # пишем ничего, следующая сверка возьмёт свежий ответ.
                    db.rollback()
                    continue
                db.commit()
            except IntegrityError:                    # ремень поверх гарда (симметрично backorder)
                db.rollback()
                matched += 1
                conflicts += 1
                continue
            matched += 1
    # `sending`/`lost` — про застрявшие отправки (F11), и молчать о них нельзя: оператор жмёт
    # «сверить» ИМЕННО из-за такой строки. `sending` — «не тронули, там живой execute»,
    # `lost` — «разобрали: провайдер про заказ не знает». Без них поллинг отвечал бы «сверено 0»
    # и выглядел сломанным ровно в том случае, ради которого его и позвали.
    return {"checked": matched, "conflicts": conflicts, "sending": sending}


def _poll_registrar(deadline: float) -> dict:
    """Сверка неизвестного исхода канала «registrar» (NameSilo) правдой регистратора: строки `failed` с
    `maybe_sent` и застрявшие `ordering` с протухшим claim. Только ЧТЕНИЕ (getDomainInfo -> listOrders ->
    баланс), ничего не отправляет. registered -> `ordered`; «не зарегистрирован (выверено)» -> флаг
    maybe_sent снят, но подтверждение ЧЕЛОВЕКА сброшено (повтор — только новым confirm); иначе строка
    остаётся как есть (отмена заблокирована). Записываем ТОЛЬКО через `_settle` (ABA-гард)."""
    import time
    from datetime import datetime, timezone
    from sqlalchemy import select
    from app.db import SessionLocal
    from app.integrations.registrar import get_registrar
    from app.models.domain import Domain, AcquisitionOrder

    out = {"checked": 0, "conflicts": 0, "sending": 0, "lost": 0, "pending": 0}
    bought_ids: list[int] = []
    with SessionLocal() as db:
        rows = db.execute(select(AcquisitionOrder).where(
            AcquisitionOrder.provider == "registrar",
            AcquisitionOrder.status.in_(("failed", "ordering")))).scalars().all()
        rows = [o for o in rows if o.status == "ordering" or (o.result or {}).get("maybe_sent")]
        if not rows:
            return out
        r = get_registrar()
        if not getattr(r, "configured", False) or not hasattr(r, "reconcile"):
            return out
        for o in rows:
            if time.monotonic() > deadline:
                raise TimeoutError(f"общий дедлайн опроса ({POLL_DEADLINE_SEC:.0f} с) вышел — остальные "
                                   f"строки разберёт следующая сверка")
            if o.status == "ordering" and not _claim_expired(o):
                out["sending"] += 1                       # живой execute в полёте — не трогаем
                continue
            d = db.get(Domain, o.domain_id)
            if d is None:
                continue
            ctx = (o.result or {}).get("registrar_ctx") or {}
            if ctx.get("auction"):
                # итог ставки на аукционе машина не выверяет (домен не «регистрируется» сразу) — только вручную
                verdict, note = "unknown", "ставка на аукционе: итог проверь на сайте NameSilo"
            else:
                try:
                    since = datetime.fromisoformat(ctx["sent_at"])
                except (KeyError, ValueError, TypeError):
                    since = o.claimed_at or datetime.now(timezone.utc)
                if since.tzinfo is None:
                    since = since.replace(tzinfo=timezone.utc)
                verdict, note = r.reconcile(d.domain, since, ctx.get("balance_before"))
            base = {k: v for k, v in (o.result or {}).items() if k != "maybe_sent"}
            if verdict == "registered":
                # регистрация подтверждена правдой регистратора (getDomainInfo: наш, Active) — это
                # уже покупка, как и у синхронного успеха execute: `caught` + домен `purchased`
                values = {"status": "caught", "claimed_at": None,
                          "ordered_at": o.ordered_at or datetime.now(timezone.utc),
                          "result": {**base, "note": f"восстановлено сверкой: {note}"}}
                bought_ids.append(o.domain_id)
            elif verdict == "not_registered":
                values = {"status": "failed", "claimed_at": None, "confirmed_by_human": False,
                          "result": {**base, "error": note}}
                out["lost"] += 1
            else:
                if o.status == "ordering":                # труп без вердикта: деньги могли уйти — отмена заперта
                    values = {"status": "failed", "claimed_at": None,
                              "result": {**base, "maybe_sent": True,
                                         "error": f"отправка оборвалась, исход не выяснен: {note}"}}
                else:
                    out["pending"] += 1
                    continue
            if not _settle(db, o, **values):
                db.rollback()
                out["conflicts"] += 1
                bought_ids = [i for i in bought_ids if i != o.domain_id]
                continue
            if o.domain_id in bought_ids:
                d.status = "purchased"
            db.commit()
            out["checked"] += 1
    for did in bought_ids:
        _site_after_purchase(did)
    return out


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

    ВЫХОД ИЗ 'ordering' ЕСТЬ И У optimizator (Task 3, второй канал — было ревью Задачи 8, минор 2,
    отложено до появления транспорта). До Task 1/2 execute для optimizator падал NotImplementedError
    ДО сети — до claim'а исход был уже известен, окна для застревания не было. Реальный транспорт
    открыл то же окно, что у backorder: процесс могли убить между claim'ом и ответом провайдера.
    Ниже — свой цикл по `provider == "optimizator"`: источник правды другой (нет client_orders()/
    find_order — списка заказов у optimizator нет), только check_domain(domain) по одному домену.
    Структура та же: снимок SELECT'ом, пропуск свежего claim'а, запись ТОЛЬКО через `_settle`.
    """
    import time
    deadline = time.monotonic() + POLL_DEADLINE_SEC
    out = {"checked": 0, "conflicts": 0, "sending": 0, "lost": 0, "caught": 0, "failed": 0, "pending": 0}
    errors: dict = {}
    # Провайдеры НЕЗАВИСИМЫ (S3-04): лежащий backorder (капча, 404 тарифов) не должен ни ронять опрос,
    # ни держать восстановление optimizator; сбой каждого — в `errors`, и оператор видит его в UI.
    for name, fn in (("backorder", _poll_backorder),
                     ("optimizator", lambda: _poll_optimizator(deadline)),
                     ("registrar", lambda: _poll_registrar(deadline))):
        try:
            part = fn()
        except Exception as e:  # noqa: BLE001 — сбой одного провайдера не топит опрос второго
            errors[name] = _scrub_text(f"{type(e).__name__}: {e}")[:200]
            continue
        for k, v in part.items():
            out[k] = out.get(k, 0) + v
    return {**out, "errors": errors}


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
        # ЧТО ЕЩЁ ДЕРЖИТ ДОМЕН, кроме снятой строки, — спрашиваем ПОСЛЕ захвата, в той же
        # транзакции, что и пишем (ревью Задачи 8, минор 1). Замок, взятый UPDATE'ом, держит
        # ТОЛЬКО НАШУ строку; соседняя строка того же домена (легаси-пара) живёт своей жизнью:
        # пока мы шли к записи, её мог заклеймить execute («↻ повторить»: failed -> ordering) или
        # поднять поллинг (фантом failed -> ordered). Ответ «домен больше ничем не держится»,
        # прочитанный ДО захвата, протухал ровно так же, как протухал статус нашей строки до F12,
        # — и домен уезжал в approved при ЖИВОМ открытом заказе, то есть обратно в очередь выкупа,
        # где его подтвердят и оплатят второй раз.
        blocker = _open_order_id(db, o.domain_id, except_id=order_id)
        unresolved = _unresolved_money_id(db, o.domain_id, except_id=order_id)
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
            # Подтверждение, у которого вышел срок, на экране = «не подтверждено»: гейт для исполнения
            # уже закрыт (execute его не примет), и кнопка обязана вернуться в «✓ подтвердить».
            expired = bool(o.confirmed_by_human) and _confirm_expired(o)
            out.append({"id": o.id, "domain": d.domain if d else f"#{o.domain_id}",
                        "provider": o.provider, "status": o.status,
                        "confirmed": bool(o.confirmed_by_human) and not expired,
                        "confirm_expired": expired,
                        "bid": float(o.cost) if o.cost is not None else None,
                        "currency": o.cost_currency or ("RUB" if o.cost is not None else None),
                        # ретроактивный скраб: в старых строках текст исключения мог унести api_key
                        "result": ({k: (_scrub_text(v) if isinstance(v, str) else v)
                                    for k, v in o.result.items()} if isinstance(o.result, dict) else o.result),
                        "domain_id": o.domain_id,
                        # источник и текущая ставка лота — для формы потолка аукциона в очереди
                        "source": d.source if d is not None else None,
                        "ask": float(d.acquire_price) if d is not None and d.acquire_price is not None else None,
                        "dirty": dirty_reason(d) if d is not None else None,
                        "stuck": o.status == "ordering" and _claim_expired(o)})
    return out


def channel_status(orders: list[dict] | None = None, deadline: float = 10.0) -> dict:
    """Баланс/доступность каналов, кроме backorder (его грузит сам экран вместе с сеткой тарифов), для
    шапки /queue (S3-09, S3-04). Сеть — только у настроенного канала (ключ задан или по нему есть заказы),
    параллельно и под общим дедлайном: лежащий провайдер не вешает денежный экран. Ошибка видна словами."""
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutTimeout
    from app.config import settings
    from app.integrations.registrar import get_registrar

    used = {o["provider"] for o in orders or ()}
    out: dict = {"registrar": {"configured": bool(get_registrar().configured)}}
    jobs: dict = {}
    if settings.OPTIMIZATOR_API_KEY or "optimizator" in used:
        def _opt():
            from app.integrations.optimizator import OptimizatorClient
            return OptimizatorClient(quick=True).balance()
        jobs["optimizator"] = (_opt, "RUB")
    if out["registrar"]["configured"]:
        jobs["registrar"] = (lambda: get_registrar().balance(), None)
    ex = ThreadPoolExecutor(max_workers=max(1, len(jobs)))
    try:
        futs = {name: ex.submit(fn) for name, (fn, _cur) in jobs.items()}
        for name, fut in futs.items():
            cur = jobs[name][1]
            try:
                b = fut.result(timeout=deadline)
                if isinstance(b, tuple):          # Money(amount, currency)
                    b, cur = b
                out.setdefault(name, {}).update(balance=b, currency=cur, error=None)
            except FutTimeout:
                out.setdefault(name, {}).update(balance=None, currency=cur, error=f"не ответил за {deadline:.0f} с")
            except Exception as e:  # noqa: BLE001 — баланс информационный: экран живёт и без него
                out.setdefault(name, {}).update(balance=None, currency=cur,
                                                error=_scrub_text(f"{type(e).__name__}: {e}")[:160])
    finally:
        ex.shutdown(wait=False)
    return out


if __name__ == "__main__":  # гейт-логика без БД: execute отказывает без подтверждения
    class _O:                # фейковый заказ — проверяем ветвление гейта из execute
        confirmed_by_human = False
        status = "pending_confirm"
    o = _O()
    assert not o.confirmed_by_human, "гейт должен блокировать неподтверждённый заказ"
    o.confirmed_by_human = True
    assert o.confirmed_by_human, "после confirm — гейт открыт"
    assert _PROVIDERS == {"backorder", "optimizator", "registrar"}
    print("acquisition gate self-check ok")
