"""backorder.ru client (catch dropping domains). Transport only.

Runs on top of billmgr (ISPsystem BILLmanager). Полный референс — docs/api/backorder.md
(эндпойнты, все 14 id_status, форма заказа — сверены с офиц. докой и живым API 2026-07-11).

Два контура:
  A. Публичный (без auth): /json/ (drop-feed для M1), price_*.json (сетка тарифов).
  B. billmgr API: GET /manager/billmgr?func=...&out=json&authinfo=LOGIN:PASSWORD
     Ответ — конверт {"elem": [...]} (ПРОВЕРЕНО живьём), ошибка — {"error": {"msg": ...}}.

ТАРИФ = СТАВКА. У backorder не одна цена: сетка из ~35 тиров на зону (190 ₽ … 5 млн ₽,
type_id=63). Чем выше тариф, тем к большему числу регистраторов уйдёт заказ и тем выше
шанс перехвата. Поэтому выбор тарифа — это решение о деньгах: его принимает ЧЕЛОВЕК на
гейте подтверждения (services/acquisition.py), а не система.
"""
import math
import time

import httpx

from app.config import settings
from app.integrations.base import BaseClient

_BILLMGR = "https://backorder.ru/manager/billmgr"
_PRICE_JSON = "https://backorder.ru/manimg/userdata/json/price_ru_backorder.ru.json"

_ITYPE_BACKORDER = "63"       # тип услуги «освобождающийся домен» (== type_id тарифа)

# id_status провайдера -> состояние нашей машины M2 (docs/api/backorder.md §5).
# Не перечисленные (2 «не оплачен», 4, 5, 8, 10, 13) — заказ ещё в полёте: pending.
# 8 «Готов (домен в процессе передачи)» НАМЕРЕННО не caught: передача может сорваться, а
# caught терминален (домен -> purchased -> M3 начнёт провижнить). Ждём 11 — это ничего не стоит.
_STATE = {
    11: "caught",   # завершён: домен пойман — единственное терминальное «наш»
    3: "failed",    # перекрыт
    6: "failed",    # аннулирован (домен продлён владельцем)
    7: "failed",    # аннулирован (неудачная попытка регистрации) — не поймали
    9: "failed",    # аннулирован (удалён)
    12: "failed",   # аннулирован (приём заказа закрыт)
    14: "failed",   # аннулирован
}

# Сетка тарифов статична (id стабильны, сетка изредка пополняется) — кешируем на процесс,
# чтобы рендер /queue не дёргал JSON на каждую заявку.
_GRID_CACHE: dict[str, list[dict]] = {}
_BALANCE_CACHE: dict = {"at": 0.0, "value": None}      # /queue рендерится часто, счёт — редко


class AmbiguousSend(Exception):
    """Запрос ушёл, исход НЕИЗВЕСТЕН — заказ мог быть создан и оплачен.

    Отличается от обычного отказа принципиально: провайдер, вернувший структурный
    {"error": {...}}, заказ НЕ создал (повтор безопасен). А таймаут, HTTP-5xx от фронта
    billmgr и HTML-страница вместо JSON приходят и ПОСЛЕ успешного создания заказа —
    повторять вслепую нельзя, это второе списание.
    """


def norm_domain(domain: str) -> str:
    """Каноническая форма домена для сравнения. Ключ идемпотентности денег — сверять
    сырые строки нельзя: фид отдаёт .РФ кириллицей, а billmgr хранит punycode."""
    d = (domain or "").strip().rstrip(".").lower()
    try:
        return d.encode("idna").decode("ascii")
    except (UnicodeError, UnicodeDecodeError):
        return d


def zone_of(domain: str) -> str | None:
    """Зона тарифной сетки домена, или None для незнакомой зоны.

    Сетка backorder есть только под .RU и .РФ. Возврат None (а не «наверное .RU») —
    защита денег: заказ по чужой сетке = списание не того тарифа. .РФ приходит из фида
    кириллицей, но punycode тоже ловим.
    """
    d = (domain or "").strip().lower()
    if d.endswith(".рф") or d.endswith(".xn--p1ai"):
        return ".РФ"
    if d.endswith(".ru"):
        return ".RU"
    return None


def state_of(id_status) -> str:
    """id_status провайдера -> 'caught' | 'failed' | 'pending'."""
    try:
        return _STATE.get(int(id_status), "pending")
    except (TypeError, ValueError):
        return "pending"


class BackorderClient(BaseClient):
    def __init__(self):
        super().__init__("https://backorder.ru")
        self.login = settings.BACKORDER_LOGIN
        self.password = settings.BACKORDER_PASSWORD
        self.account_id = settings.BACKORDER_ACCOUNT_ID
        self.contact_id = settings.BACKORDER_CONTACT_ID

    # -- billmgr (authed) ---------------------------------------------------

    def _scrub(self, s: str) -> str:
        """Пароль/логин уходят в query — httpx кладёт полный URL в текст HTTPStatusError.
        Ни одна строка наружу (баннер, лог, o.result) не должна их содержать."""
        for secret in (self.password, self.login):
            if secret:
                s = s.replace(secret, "***")
        return s

    def _billmgr(self, func: str, *, retry: bool = True, timeout: float | None = None,
                 money: bool = False, **params) -> list[dict]:
        """GET billmgr?func=... -> список из конверта {"elem": [...]}.

        retry=False для денежных вызовов: BaseClient.request ретраит транспортные сбои
        3 раза, а повтор uniservice.order по таймауту = второй платный заказ.

        money=True меняет КЛАСС ошибки, а не только её текст: всё, где исход неизвестен
        (таймаут / HTTP-5xx / не-JSON), поднимается как AmbiguousSend — вызывающий обязан
        считать, что деньги могли уйти. Структурный {"error": {...}} остаётся обычным
        RuntimeError: провайдер явно отверг, заказа нет, повтор безопасен.
        """
        p = {"func": func, "out": "json", "authinfo": f"{self.login}:{self.password}", **params}
        kw = {"params": p} if timeout is None else {"params": p, "timeout": timeout}
        try:
            if retry:
                resp = self.request("GET", _BILLMGR, **kw)
            else:
                resp = self._client.request("GET", _BILLMGR, **kw)
                resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPStatusError as e:
            # 4xx = сервер ОБРАБОТАЛ этот же GET и отказал -> заказа точно нет, повтор безопасен.
            # Иначе (5xx / 408 / 429) исход неизвестен: фронт billmgr мог отдать 502 уже ПОСЛЕ
            # того, как заказ создан и оплачен. Не размазывать 4xx в ambiguous — кривые креды
            # давали бы неснимаемый «фантом» на ровном месте.
            code = e.response.status_code
            msg = self._scrub(f"backorder {func}: HTTP {code}")
            unknown = money and (code >= 500 or code in (408, 429))
            raise (AmbiguousSend(msg) if unknown else RuntimeError(msg)) from None
        except httpx.RequestError as e:
            # RequestError, а не TransportError: DecodingError/TooManyRedirects — тоже
            # RequestError, но НЕ TransportError, и приходят ПОСЛЕ обработки запроса сервером.
            msg = f"backorder {func}: связь оборвалась ({type(e).__name__})"
            raise (AmbiguousSend(msg) if money else RuntimeError(msg)) from None
        except ValueError:
            msg = f"backorder {func}: ответ не JSON (страница ошибки billmgr?)"
            raise (AmbiguousSend(msg) if money else RuntimeError(msg)) from None
        if isinstance(data, dict) and data.get("error"):
            err = data["error"]
            msg = err.get("msg") if isinstance(err, dict) else err
            # Явный отказ провайдера: заказ НЕ создан — это не ambiguous, повтор безопасен.
            raise RuntimeError(self._scrub(f"backorder {func}: {msg}"))
        elem = data.get("elem") if isinstance(data, dict) else None
        if isinstance(elem, list):
            return elem
        # Форма НЕ распознана: ни `error`, ни `elem`-список. Для read-вызовов (money=False)
        # это по-прежнему «пусто» — []. Но для денежного uniservice.order (money=True) форма
        # success-ответа НЕ подтверждена вживую (docs/api/backorder.md gotcha #9: может быть
        # {"doc":{...}} вместо {"elem":[...]}) — трактовать её как «пустой успех» значило бы
        # выдать заказ с пустым provider_order_id и БЕЗ maybe_sent, хотя ушли ли деньги —
        # неизвестно. Это ровно тот исход, ради которого существует AmbiguousSend (симметрично
        # optimizator._unwrap, который уже так делает). money-путь: неизвестная форма -> ambiguous.
        if money:
            raise AmbiguousSend(self._scrub(
                f"backorder {func}: неизвестная форма успешного ответа "
                f"(ни error, ни elem) — исход отправки неизвестен"))
        return []

    def balance(self, ttl: float = 60.0) -> float | None:
        """Остаток на лицевом счёте. uniservice.order идёт с paynow=on — при 0 ₽ заказ
        повиснет «Не оплачен», поэтому баланс показываем ДО отправки.

        Кеш на TTL + короткий таймаут без ретрая: это блокирующий вызов на рендере /queue
        (единственный экран денежного пути) — лежащий провайдер не должен вешать страницу
        на 3 ретрая × 30 с.
        """
        now = time.monotonic()
        if _BALANCE_CACHE["value"] is not None and now - _BALANCE_CACHE["at"] < ttl:
            return _BALANCE_CACHE["value"]
        elem = self._billmgr("accountinfo", retry=False, timeout=8.0)
        if not elem:
            return None
        try:
            val = float(elem[0].get("balance"))
        except (TypeError, ValueError):
            return None
        _BALANCE_CACHE.update(at=now, value=val)
        return val

    def client_orders(self) -> list[dict]:
        """Мои backorder-заказы + их статусы (источник правды для поллинга M2)."""
        out = []
        for e in self._billmgr("clientbackorder"):
            out.append({
                "elid": str(e.get("id") or ""),
                "domain": e.get("domainname") or "",
                "id_status": e.get("id_status"),
                "clear_status": e.get("clear_status") or e.get("status") or "",
                "state": state_of(e.get("id_status")),
                "tariff": e.get("tariff"),
            })
        return out

    # -- тарифы (публичный JSON, без auth) ----------------------------------

    def tariffs(self, zone: str = ".RU", refresh: bool = False) -> list[dict]:
        """Сетка ставок зоны: [{price_id, period_id, price}] по возрастанию цены.

        Фильтр type_id == "63" (освобождающиеся домены; 3 = обычная регистрация, 20 = DNS,
        28 = SSL — не наши). Зона — по полю grp («... в .RU» / «... в .РФ»).
        Цена берётся из period[0].price_num: верхнеуровневое `price` — строка вида
        "190.0000 RUB / 190", float() на ней падает.
        """
        if refresh or zone not in _GRID_CACHE:
            # timeout+без ретрая: сетка тянется на рендере /queue (денежный экран) — лежащий
            # price-JSON не должен вешать страницу на 3 ретрая × 30 с.
            r = self._client.request("GET", _PRICE_JSON, timeout=8.0)
            r.raise_for_status()
            raw = r.json()
            rows = raw if isinstance(raw, list) else []
            for z in (".RU", ".РФ"):
                grid = sorted((t for t in (self._tier(row, z) for row in rows) if t),
                              key=lambda t: t["price"])
                if grid:                      # пустую сетку НЕ кешируем: иначе сменившийся
                    _GRID_CACHE[z] = grid     # формат ответа навсегда ломает подтверждение
        return _GRID_CACHE.get(zone, [])

    @staticmethod
    def _tier(row, zone: str) -> dict | None:
        """Одна запись тарифа -> {price_id, period_id, price} или None (не наш тип/зона/битая)."""
        if not isinstance(row, dict) or str(row.get("type_id")) != _ITYPE_BACKORDER:
            return None
        if not (row.get("grp") or "").endswith(zone):
            return None
        period = row.get("period") or []
        if not (period and isinstance(period[0], dict)):
            return None
        try:
            price = float(period[0].get("price_num"))
        except (TypeError, ValueError):
            return None
        price_id, period_id = row.get("id"), period[0].get("id")
        if price_id is None or period_id is None:
            return None
        return {"price_id": str(price_id), "period_id": str(period_id), "price": price}

    def get_tariffs(self, zone: str = ".RU") -> dict:
        """Базовый (самый дешёвый) тариф зоны — цена «от», которую discovery проставляет
        домену как acquire_price. Форма ответа сохранена для services/pricing.py."""
        grid = self.tariffs(zone)
        if not grid:
            return {"price": None, "price_id": None, "period_id": None}
        return dict(grid[0])

    def pick_tariff(self, domain: str, bid_rub: float) -> dict:
        """Тариф под ставку: самый дешёвый тир зоны домена с ценой >= bid_rub.

        Зовётся на ПОДТВЕРЖДЕНИИ (человеком), не на отправке: выбранный тир замораживается
        в заказе, и execute уже ничего не «до-решает» за человека. Незнакомая зона / пустая
        сетка -> RuntimeError: молча заказать по чужому тарифу = списать не те деньги.

        Ставка обязана быть конечным положительным числом НЕ ВЫШЕ верхнего тира сетки ->
        иначе ValueError. Единственный вызывающий (services/acquisition.confirm_order) уже
        проверяет isfinite/>0 до сети — проверка здесь на случай прямого вызова (публичный
        метод). Раньше ставка выше сетки молча получала верхний тир (`return grid[-1]`) —
        опечатка с лишним нулём списывала счёт по максимальному тарифу без единого слова.
        Явный MAX_BID_RUB — эта же сетка: платить сверх её верхнего тира провайдеру всё
        равно нечем, поэтому это ошибка ввода, а не «округлим вверх, как получится».
        """
        if not math.isfinite(bid_rub) or bid_rub <= 0:
            raise ValueError(f"ставка должна быть конечным числом больше нуля, получено {bid_rub}")
        zone = zone_of(domain)
        if zone is None:
            raise RuntimeError(
                f"backorder: нет тарифной сетки под зону домена {domain!r} (есть только .RU/.РФ)")
        grid = self.tariffs(zone)
        if not grid:
            raise RuntimeError(f"backorder: пустая сетка тарифов для зоны {zone}")
        for t in grid:
            if t["price"] >= bid_rub:
                return dict(t)
        max_bid_rub = grid[-1]["price"]
        raise ValueError(
            f"ставка {bid_rub:.0f} ₽ выше максимума тарифной сетки {zone} "
            f"({max_bid_rub:.0f} ₽) — платить больше нечем")

    def find_order(self, domain: str) -> dict | None:
        """Есть ли у провайдера ЖИВОЙ заказ на этот домен. Ключ идемпотентности.

        Нужен, чтобы не заплатить дважды: ambiguous-сбой (заказ ушёл, ответ не дошёл)
        оставляет заказ у провайдера, а у нас — 'failed' с кнопкой «повторить». Спрашиваем
        провайдера, прежде чем слать. Ловит и заказ, размещённый руками из ЛК.

        Сравнение — по нормализованной (punycode) форме: фид отдаёт .РФ кириллицей, billmgr
        хранит punycode, и сырое сравнение строк молча пропустило бы дубль на всю зону .РФ.
        """
        want = norm_domain(domain)
        for r in self.client_orders():
            if norm_domain(r["domain"]) == want and r["state"] != "failed":
                return r
        return None

    # -- discovery (публичный фид, без auth) --------------------------------

    def list_dropping(self, min_links: int = 1, limit: int = 5000) -> list[dict]:
        """Domains freeing tomorrow with >=min_links donors (discovery source for M1).

        Public feed, no auth. Fields: domainname, links, delete_date, visitors,
        yandex_tic, x_value, rkn, judicial, block. See docs/api/backorder.md.
        """
        r = self.request("GET", f"{self.base_url}/json/", params={
            "ext": "1", "disp": "1", "tomorrow": "1",
            "links": str(min_links), "by": "links", "order": "desc",
        })
        data = r.json()
        rows = data if isinstance(data, list) else []

        def _links(row):    # фид отдаёт links строкой ("5") — сравнивать строку с int нельзя
            try:
                return int(row.get("links"))
            except (TypeError, ValueError):
                return 0
        bad = [row for row in rows if isinstance(row, dict) and _links(row) < min_links]
        if bad:
            import logging
            logging.getLogger(__name__).warning(
                "backorder: %d/%d строк с links<%d — фильтр не применился?", len(bad), len(rows), min_links)
        return rows[:limit]

    # -- ЗАКАЗ (деньги!) ----------------------------------------------------

    def order(self, domain: str, price_id: str, period_id: str) -> dict:
        """Разместить backorder-заказ. ДЕНЬГИ: paynow=on списывает с баланса.

        HARD GATE: звать ТОЛЬКО после AcquisitionOrder.confirmed_by_human == True
        (PLAN.md §2, правило 2). Вызывающий — services/acquisition.execute_confirmed_order.

        Форма — docs/api/backorder.md §4 (офиц. дока, 1:1). Меняем только period/price/
        domainname/payfrom/contact; остальное — константы, «менять не нужно».
        БЕЗ РЕТРАЯ: повтор по таймауту = второй платный заказ.
        """
        if not (price_id and period_id):
            raise RuntimeError("backorder.order: не задан тариф (price_id/period_id)")
        if not (self.account_id and self.contact_id):
            raise RuntimeError(
                "backorder.order: пусты BACKORDER_ACCOUNT_ID/BACKORDER_CONTACT_ID в .env "
                "(взять из accountinfo/domaincontact)")
        elem = self._billmgr(
            "uniservice.order", retry=False, money=True,
            period=period_id, price=price_id, domainname=domain,
            itype=_ITYPE_BACKORDER, sok="ok",
            payfrom=f"account{self.account_id}",   # литерал 'account' + id счёта слитно
            contact=self.contact_id, paynow="on", clientbackorder="yes",
        )
        # Ответ на успешный order — конверт с созданным заказом; id заказа = elid для поллинга.
        elid = str(elem[0].get("id") or "") if elem and isinstance(elem[0], dict) else ""
        return {"order_id": elid, "elem": elem}

    def ping(self) -> bool:
        # public discovery feed (no auth): domains freeing tomorrow with >=1 donor
        r = self.request("GET", f"{self.base_url}/json/",
                         params={"ext": "1", "disp": "1", "tomorrow": "1",
                                 "links": "1", "by": "links", "order": "desc"})
        return isinstance(r.json(), list)
