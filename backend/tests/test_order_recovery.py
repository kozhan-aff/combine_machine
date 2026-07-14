"""Заказ не застревает в отправке и не исчезает после неё (аудит F11 + F12).

Два состояния, из которых машина не умела выходить, — и оба про деньги:

F11. `ordering` — ТРАНЗИЕНТНЫЙ claim: execute забирает им строку и держит секунды, пока ходит к
     провайдеру. Убей процесс в этом окне (git-pull-деплой перезапускает контейнер, OOM, docker
     restart) — и строка остаётся в `ordering` навсегда. Её не берёт execute (claim пускает только
     `pending_confirm|failed`), не снимает cancel (те же два), не видел поллинг (`ordered|failed`).
     Домен под ней вечно висит в `purchasing`, заявку на него не завести, а деньги, возможно, ушли
     — и об этом не спрашивает НИКТО. Единственный честный разбор — правда провайдера.

F12. `cancel_order` писал `o.status = 'cancelled'` слепым ORM-UPDATE по первичному ключу. Между
     его чтением статуса и этой записью помещается ВЕСЬ execute (кнопки «отправить» и «отменить»
     — два sync-роута, панель гоняет их в threadpool параллельно; отправка ходит в сеть, отмена
     нет — и легко приезжает второй). Оплаченный заказ становился `cancelled`, а `cancelled`
     поллинг не опрашивает: заказ исчезал из машины навсегда, домен уезжал в `approved` и был бы
     куплен второй раз.
"""
from datetime import datetime, timedelta, timezone

import pytest

import app.db as db
from app.integrations import backorder
from app.models.domain import AcquisitionOrder, Domain
from app.services import acquisition


class _ProcessKilled(BaseException):
    """Процесс убили ровно в момент отправки. BaseException — потому что это НЕ ошибка провайдера:
    `execute` ловит `except Exception` и аккуратно пишет `failed`, а убитый процесс не пишет
    ничего. Пройти сквозь широкий except обязана только такая ловушка (тот же приём, что у
    рубильника сети в conftest)."""


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


def _grid(monkeypatch):
    monkeypatch.setattr(backorder.BackorderClient, "tariffs",
                        lambda self, zone=".RU", refresh=False: [
                            {"price_id": "4769", "period_id": "3442", "price": 190.0}])


def _send_and_die(monkeypatch, name="crash.ru") -> tuple[int, int]:
    """Строка, застрявшая в `ordering`, — ЖИВЫМ путём, а не сеянием руками.

    Проходим весь денежный путь настоящим кодом (заявка → подтверждение человеком со ставкой →
    отправка) и убиваем процесс ровно там, где его убивает деплой: между атомарным claim'ом
    (`-> ordering`, уже закоммичен) и ответом провайдера. Именно это состояние и оставляет за
    собой перезапуск контейнера, и никакое другое: claim коммитится отдельно, чтобы параллельный
    клик не послал второй платный заказ.
    """
    _grid(monkeypatch)
    monkeypatch.setattr(backorder.BackorderClient, "find_order", lambda self, domain: None)

    def _die(self, domain, price_id, period_id):
        raise _ProcessKilled("контейнер перезапустили в момент отправки")
    monkeypatch.setattr(backorder.BackorderClient, "order", _die)

    did = _approved(name)
    oid = acquisition.create_order(did, "backorder")
    acquisition.confirm_order(oid, 190)               # ГЕЙТ поднимает человек — как в жизни
    with pytest.raises(_ProcessKilled):
        acquisition.execute_confirmed_order(oid)

    # Про `claimed_at` тут НАМЕРЕННО ни слова: помощник изображает состояние, а не фикс. Иначе он
    # падал бы на старом коде первым (колонки не было) и прятал бы за собой ровно то поведение,
    # которое тесты обязаны доказать: до фикса застрявшую отправку не разбирал НИКТО. Часы claim'а
    # сторожит отдельный тест (`test_poll_leaves_a_live_send_alone`).
    with db.SessionLocal() as s:
        assert s.get(AcquisitionOrder, oid).status == "ordering", (
            "claim коммитится отдельно от отправки — он обязан пережить смерть процесса")
    return did, oid


_DEAD_FOR = timedelta(hours=6)     # заведомо дольше STUCK_CLAIM_MIN; сторожит гард в тесте ниже


def _age_the_claim(order_id: int) -> None:
    """Сдвигаем ЧАСЫ, а не состояние: строку и `claimed_at` поставил живой код (см. `_send_and_die`),
    протухание claim'а — это просто время. Без сдвига поллинг ОБЯЗАН считать отправку живой и не
    трогать её (это отдельный тест ниже) — иначе он вырвет строку у execute, который сейчас в
    полёте, и откроет ей путь на повторную отправку: второе списание.

    Возраст — константой, а не `STUCK_CLAIM_MIN + 5`: фикстура, отмеренная от самого порога,
    сидела бы вплотную к нему и краснела/зеленела от правки порога, а не от поведения (урок ветки).
    Что 6 часов больше порога, проверяет гард в `test_poll_leaves_a_live_send_alone`.
    """
    with db.SessionLocal() as s:
        o = s.get(AcquisitionOrder, order_id)
        o.claimed_at = datetime.now(timezone.utc) - _DEAD_FOR
        s.commit()


# --- F11: застрявшая отправка -------------------------------------------------------------


def test_poll_adopts_a_stuck_send_the_provider_knows(sqlite_db, monkeypatch):
    """Заказ ДОЛЕТЕЛ, а ответ — нет: провайдер держит его в полёте, у нас строка в `ordering`.

    До фикса поллинг выбирал только `ordered|failed` — застрявшую отправку не видел никто, домен
    навсегда оставался в `purchasing`, а оплаченный заказ машине был неизвестен: его никто не
    сверял, поимку никто не ждал. Сверка обязана усыновить его по elid.
    """
    did, oid = _send_and_die(monkeypatch)
    _age_the_claim(oid)
    monkeypatch.setattr(backorder.BackorderClient, "client_orders", lambda self: [
        {"elid": "777", "domain": "crash.ru", "id_status": "2", "clear_status": "Не оплачен",
         "state": "pending", "tariff": "190"},
    ])

    r = acquisition.poll_orders()

    o = _orders(did)[0]
    assert o.status == "ordered", "застрявшую отправку не разобрал никто — заказ потерян"
    assert o.provider_order_id == "777", "заказ не усыновлён: поимку по нему никто не отследит"
    assert o.claimed_at is None, "исход есть — claim обязан закрыться"
    assert r["pending"] == 1 and r["checked"] == 1
    with db.SessionLocal() as s:
        assert s.get(Domain, did).status == "purchasing"   # домен в выкупе — заказ жив


def test_poll_frees_a_stuck_send_the_provider_never_got(sqlite_db, monkeypatch):
    """Заказ НЕ долетел: провайдер про домен не знает. Строка обязана перестать быть могилой.

    До фикса домен становился узником навсегда: отправить нельзя (claim берёт только
    pending_confirm/failed), снять нельзя (тот же список), поллинг строку не видит, а из
    `purchasing` новую заявку не завести. Правда провайдера («заказа нет») — та же, на основании
    которой execute считает себя вправе ТРАТИТЬ деньги; значит её достаточно и чтобы вернуть
    строке выход.
    """
    did, oid = _send_and_die(monkeypatch)
    _age_the_claim(oid)
    monkeypatch.setattr(backorder.BackorderClient, "client_orders", lambda self: [])

    r = acquisition.poll_orders()

    o = _orders(did)[0]
    assert o.status == "failed", "строка так и осталась могилой — домен заперт навсегда"
    assert "maybe_sent" not in (o.result or {}), (
        "провайдер ОТВЕТИЛ, что заказа нет, — неизвестности больше нет (иначе отмена заперта зря)")
    assert (o.result or {}).get("error"), "человек должен прочитать в очереди, что произошло"
    assert r["lost"] == 1, "оператор жмёт сверку ИЗ-ЗА этой строки — молчать о ней нельзя"

    # ВЫХОД ЕСТЬ: заявка снимается, домен возвращается в очередь кандидатов.
    assert acquisition.cancel_order(oid)["status"] == "cancelled"
    with db.SessionLocal() as s:
        assert s.get(Domain, did).status == "approved"
    acquisition.create_order(did, "backorder")        # домен снова покупаем — не узник


def test_poll_leaves_a_live_send_alone(sqlite_db, monkeypatch):
    """ГАРД ПОРОГА (а не регрессия бага): свежий claim — это ЖИВОЙ execute в полёте у провайдера.

    Разобрать такую строку значит вынести вердикт за того, кто прямо сейчас платит: мы поставим
    ей `failed`, человек нажмёт «↻ повторить» — и заказ уйдёт ВТОРОЙ раз. Поэтому поллинг судит
    только протухший claim (STUCK_CLAIM_MIN). Тест держит порог: обнули его — тест покраснеет.

    Он же сторожит ПИСАТЕЛЯ часов: не поставь execute `claimed_at` в claim'е — и любая отправка
    выглядела бы протухшей с первой секунды (NULL = труп), а поллинг вырывал бы строки у живых.
    """
    assert acquisition.STUCK_CLAIM_MIN < _DEAD_FOR.total_seconds() / 60, (
        "порог протухания дорос до возраста, которым тесты F11 «убивают» claim, — те тесты стали "
        "фикцией: они гоняли бы поллинг по ЖИВОЙ отправке")
    did, oid = _send_and_die(monkeypatch)             # claim поставлен ТОЛЬКО ЧТО, не старим
    with db.SessionLocal() as s:
        assert s.get(AcquisitionOrder, oid).claimed_at is not None, (
            "claim без часов неотличим от трупа — поллинг разберёт живую отправку")
    # Провайдер отвечает «перекрыт» — самый соблазнительный ответ: разбери мы строку сейчас, она
    # стала бы `failed`, и «↻ повторить» отправил бы заказ, который прямо сейчас в полёте.
    monkeypatch.setattr(backorder.BackorderClient, "client_orders", lambda self: [
        {"elid": "777", "domain": "crash.ru", "id_status": "3", "clear_status": "Перекрыт",
         "state": "failed", "tariff": "190"},
    ])

    r = acquisition.poll_orders()

    o = _orders(did)[0]
    assert o.status == "ordering", "у живого execute вырвали строку — прямой путь заплатить дважды"
    assert o.provider_order_id is None and r["checked"] == 0
    assert r["sending"] == 1, "строку пропустили молча — оператор решит, что сверка сломана"


def test_queue_shows_the_stuck_send(client, monkeypatch):
    """Очередь обязана НАЗВАТЬ застрявшую отправку, дать из неё выход — и не путать её с живой.

    До фикса строка рендерилась сырым `ordering` без единой кнопки: статус, которого нет в
    словаре подписей, и пустая колонка действия. Единственный экран денежного пути молчал о том,
    что заказ мог уйти."""
    _did, oid = _send_and_die(monkeypatch)

    # СВЕЖИЙ claim — это ЖИВАЯ отправка, и очередь обязана назвать срок ожидания вслух: до
    # STUCK_CLAIM_MIN сверка честно отвечает «не трогали», и оператор, которому пообещали «пару
    # минут», решит, что кнопка сломана (ревью Задачи 8, минор 3).
    live = client.get("/queue").text
    assert "заказ уходит провайдеру" in live, "живую отправку показали как обрыв — паника на ровном месте"
    assert "отправка оборвалась" not in live         # (слово живёт и в JS прогресса — метим строку заказа)
    assert f"через {acquisition.STUCK_CLAIM_MIN} мин" in live, (
        "сколько ждать разбора — не сказано; UI и сверка обязаны судить по одному сроку")

    _age_the_claim(oid)

    html = client.get("/queue").text
    assert "отправляется" in html, "сырой `ordering` в UI — машина говорит на своём языке, не на нашем"
    assert "отправка оборвалась — исход неизвестен" in html
    assert "↻ сверить с провайдером" in html, "из застрявшей отправки нет выхода — строка-могила"


def test_poll_report_names_both_kinds_of_stuck_send(client, monkeypatch):
    """Рапорт сверки — всё, что оператор видит после нажатия; про застрявшие отправки он обязан
    сказать ОБЕ новости: труп разобран, живую не трогали.

    Формы ответа поллинга не сторожил ни один тест (ревью Задачи 8, минор 4): переименуй ключ —
    и баннер молча схлопнется в «сверено 0», то есть в «кнопка не работает» ровно в том случае,
    ради которого её и жмут.
    """
    _did, dead = _send_and_die(monkeypatch, "lost.ru")
    _age_the_claim(dead)                              # труп: процесс убили давно
    _send_and_die(monkeypatch, "live.ru")             # живая отправка: claim свежий
    monkeypatch.setattr(backorder.BackorderClient, "client_orders", lambda self: [])

    html = client.post("/queue/poll", follow_redirects=True).text

    assert "застрявших отправок разобрано 1" in html, "разбор трупа прошёл молча"
    assert "отправок в полёте 1" in html, "про нетронутую живую отправку не сказали ни слова"


def test_poll_does_not_clobber_the_row_a_live_retry_just_paid_for(sqlite_db, monkeypatch):
    """Сверка судит по СНИМКУ, а execute тем временем клеймит ту же строку — снимок протухает.

    Гонка живая и дешёвая: «↻ обновить статусы» и «↻ повторить» — два sync-роута панели, FastAPI
    гоняет их в threadpool ПАРАЛЛЕЛЬНО. Строки сверка выбирает ОДНИМ SELECT'ом до цикла, а пишет по
    одной; между снимком и записью человек жмёт «повторить», и `failed`-строка уезжает в 'ordering',
    а оттуда — в 'ordered' с ОПЛАЧЕННЫМ заказом.

    До фикса сверка дописывала своё поверх — слепым UPDATE'ом по первичному ключу. У провайдера по
    этому домену лежит СТАРАЯ failed-запись («Перекрыт» с прошлого цикла), и её elid садился на
    только что оплаченную строку. Дальше цепочка достраивалась сама: следующая сверка судит строку
    по ЧУЖОМУ мёртвому заказу → 'failed' → в очереди снова «↻ повторить» → ВТОРОЙ платный заказ,
    пока первый в полёте.

    Всё живым кодом: старую запись отдаёт ОДИН фейк транспорта (`client_orders`) — тот самый, из
    которого читает и `find_order` (remote-`failed` он за живой заказ не считает, потому и шлёт
    новый), а 'ordered' рождает НАСТОЯЩИЙ execute, а не рука теста.
    """
    _grid(monkeypatch)
    # .РФ здесь не экзотика, а физика денег: фид отдаёт кириллицу, billmgr — punycode (для этого
    # norm_domain и написан). Она же — точка врезки: по КИРИЛЛИЦЕ сверка зовёт norm_domain ровно
    # из своего цикла, уже сняв снимок; запись провайдера приходит punycode'ом — на ней не срываемся.
    ours, theirs = "сайт.рф", "xn--80aswg.xn--p1ai"
    monkeypatch.setattr(backorder.BackorderClient, "client_orders", lambda self: [
        {"elid": "777", "domain": theirs, "id_status": "3", "clear_status": "Перекрыт",
         "state": "failed", "tariff": "190"}])

    def _refused(self, domain, price_id, period_id):
        raise RuntimeError("провайдер отказал: приём заказов закрыт")
    monkeypatch.setattr(backorder.BackorderClient, "order", _refused)

    did = _approved(ours)
    oid = acquisition.create_order(did, "backorder")
    acquisition.confirm_order(oid, 190)               # ГЕЙТ поднимает человек
    acquisition.execute_confirmed_order(oid)          # отказ провайдера -> failed, выход = «повторить»
    assert _orders(did)[0].status == "failed"

    # Повтор УДАЁТСЯ: настоящий find_order старую failed-запись за живой заказ не считает — заказ
    # уходит, ДЕНЬГИ СПИСЫВАЮТСЯ, у строки появляется свой elid.
    monkeypatch.setattr(backorder.BackorderClient, "order",
                        lambda self, domain, price_id, period_id: {"order_id": "999"})
    real_norm, raced = backorder.norm_domain, {}

    def racing_norm(domain):
        if domain == ours and not raced:              # мы уже внутри цикла сверки: снимок снят
            raced["yes"] = True
            acquisition.execute_confirmed_order(oid)  # ДРУГОЙ клик: «↻ повторить» — заказ ушёл
        return real_norm(domain)

    monkeypatch.setattr(backorder, "norm_domain", racing_norm)
    acquisition.poll_orders()

    assert raced, "гонку не воспроизвели — тест ничего не доказывает"
    o = _orders(did)[0]
    assert o.status == "ordered", "сверка переписала исход оплаченного заказа"
    assert o.provider_order_id == "999", (
        "на оплаченную строку сел elid ЧУЖОГО мёртвого заказа — машина следит не за тем заказом")

    # ...и вот чем это кончалось: следующая сверка судила бы строку по мёртвому «Перекрыт» и
    # вернула бы в очередь кнопку «↻ повторить».
    acquisition.poll_orders()
    assert _orders(did)[0].status == "ordered", (
        "оплаченный заказ помечен «не вышло» — оператор нажмёт «повторить» и заплатит второй раз")


# --- F12: отмена, приехавшая после отправки ------------------------------------------------


def test_cancel_does_not_bury_an_order_that_was_just_sent(sqlite_db, monkeypatch):
    """Отмена, начатая ДО отправки, доезжает ПОСЛЕ неё — и хоронит оплаченный заказ.

    Гонка живая и дешёвая: «▶ отправить провайдеру» и «✗ отменить» — два sync-роута панели, FastAPI
    гоняет их в threadpool ПАРАЛЛЕЛЬНО; отправка ходит в сеть (сотни мс), отмена — нет. Человек,
    передумавший в последний момент, воспроизводит её одним лишним кликом.

    Момент гонки вживляем точно: чужой execute коммитится между чтением строки в `cancel_order` и
    её записью. Хук — на самом чтении (`Session.get`): это ЕДИНСТВЕННАЯ точка внутри отмены до
    захвата строки. Всё остальное (кто ещё держит домен) отмена спрашивает уже ПОД ЗАМКОМ, взятым
    условным UPDATE'ом (ревью Задачи 8, минор 1), и туда никакой параллельный execute не влезет —
    в этом и смысл замка. Отправку делает НАСТОЯЩИЙ execute — состояние `ordered` рождает живой
    код, а не рука теста.
    """
    from sqlalchemy.orm import Session

    _grid(monkeypatch)
    monkeypatch.setattr(backorder.BackorderClient, "find_order", lambda self, domain: None)
    monkeypatch.setattr(backorder.BackorderClient, "order",
                        lambda self, domain, price_id, period_id: {"order_id": "999"})

    did = _approved("race.ru")
    oid = acquisition.create_order(did, "backorder")
    acquisition.confirm_order(oid, 190)               # гейт поднят человеком: заказ готов уйти

    real_get = Session.get
    raced: dict = {}

    def racing_get(self, entity, ident, *a, **kw):
        row = real_get(self, entity, ident, *a, **kw)
        if entity is AcquisitionOrder and not raced:  # отмена прочитала строку — гонка ровно здесь
            raced["yes"] = True
            acquisition.execute_confirmed_order(oid)  # ДРУГОЙ клик: заказ ушёл, деньги списаны
        return row

    monkeypatch.setattr(Session, "get", racing_get)
    r = acquisition.cancel_order(oid)                 # до фикса: слепой UPDATE ... WHERE id=N

    assert raced, "гонку не воспроизвели — тест ничего не доказывает"
    o = _orders(did)[0]
    assert o.status == "ordered", (
        "отмена похоронила ОПЛАЧЕННЫЙ заказ: `cancelled` поллинг не опрашивает — заказ исчез бы "
        "из машины навсегда")
    assert o.provider_order_id == "999"
    assert r["status"] == "ordered" and r.get("note"), "человеку должны сказать, что снять не вышло"
    with db.SessionLocal() as s:
        assert s.get(Domain, did).status == "purchasing", (
            "домен вернулся в очередь кандидатов с оплаченным заказом на борту — купим второй раз")


def test_cancel_still_works_on_a_pending_order(sqlite_db):
    """...и обычная отмена этим не сломана: заявка снимается, домен возвращается в approved.

    Условный UPDATE легко превратить в «не снимает никогда» (лишнее условие, промах rowcount) —
    и снаружи это выглядело бы просто как «кнопка не работает»."""
    did = _approved("plain.ru")
    oid = acquisition.create_order(did, "backorder")

    r = acquisition.cancel_order(oid)

    assert r["status"] == "cancelled" and _orders(did)[0].status == "cancelled"
    with db.SessionLocal() as s:
        assert s.get(Domain, did).status == "approved"
