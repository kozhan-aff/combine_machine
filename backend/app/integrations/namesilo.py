"""NameSilo — международный регистратор (канал «registrar»). ТОЛЬКО транспорт; гейт, write-ahead
(claim `ordering`), TTL подтверждения и maybe_sent живут в services/acquisition и общие для всех каналов.

Спека: docs/v2/research/namesilo-api-spec.md. Что важно помнить:
  * Автоматизация ходит на `/apibatch` (на `/api` — нарушение ToS, риск бана аккаунта).
  * Ключ — ТОЛЬКО в query (код 120 иначе) и ТОЛЬКО через `params=`: в исключения, логи, БД и UI он
    не попадает (`_clean` + log_scrub + diagnostics._scrub).
  * Два бэкенда: `reply.code` приходит то числом, то строкой, HTTP 401 бывает с нормальным JSON-телом.
    Читаем тело при ЛЮБОМ статусе, `int(code)`, `as_list()` для «один элемент = объект, много = список».
  * У registerDomain нет ни `cost`, ни ключа идемпотентности: идемпотентность держится на уникальности
    домена в реестре + НАШЕЙ adopt-проверке (getDomainInfo ДО отправки). Платный вызов — БЕЗ ретраев.
  * Форматы listAuctions/bidAuction/listOrders в спеке НЕ сняты живьём (ключа нет): парсеры строгие
    по обязательному (`domain`), остальное — по именам из документации; всё помечено «[не сверено]».
    Первый живой образец -> фикстура -> правка парсера (инвариант «не гадать форматы»).
"""
import json
import threading
import time
from datetime import datetime, timedelta, timezone

import httpx

from app.config import settings
from app.integrations.base import _user_agent
from app.integrations.registrar import Money, RegistrarAmbiguous, RegistrarError
from app.log_scrub import install as _install_log_scrub, mask_key_param

_install_log_scrub()

CURRENCY = "USD"
SANDBOX_URL = "https://sandbox.namesilo.com/api"
BATCH = 200                  # checkRegisterAvailability: до 200 доменов за вызов
MIN_INTERVAL = 1.0           # >= 1 с между вызовами (рекомендация NameSilo: <= 1 запрос/с на IP)
READ_TIMEOUT = httpx.Timeout(30.0, connect=8.0)
MONEY_TIMEOUT = httpx.Timeout(75.0, connect=8.0)    # спека: 60-90 с на registerDomain, без автоповтора
READ_ATTEMPTS = 3
RECONCILE_MIN_AGE = timedelta(minutes=15)   # спека §5.7: «не зарегистрирован» — только после серии 30 с/2/5/15 мин
AUCTION_PAGE_SIZE = 500      # живьём 2026-10-10: pageSize=500 отдаёт 500 лотов одной страницей
AUCTION_MAX_PAGES = 4        # 2000 лотов за прогон discovery: ~4 с при 1 запросе/с
FIND_AUCTION_MAX_PAGES = 10
# Операции аукционов живут НЕ на /api и НЕ на /apibatch (там 107 Invalid API operation — снято живьём
# 2026-10-10), а на /public/api. Остальные операции — по base_url (apibatch/sandbox).
AUCTION_URL = "https://www.namesilo.com/public/api"
AUCTION_OPS = frozenset({"listAuctions", "viewAuction", "viewAuctions", "bidAuction", "bulkBidAuction",
                         "buyNowAuction", "watchAuction"})
# Зоны без WHOIS privacy (спека §7): private=1 там — ошибка или тихий no-op, параметр не шлём.
NO_PRIVACY = frozenset({"ac", "am", "asia", "at", "ca", "de", "eu", "film", "in", "it", "mx", "nyc",
                        "pro", "sh", "top", "travel", "uk", "us", "vote", "ws"})

_sleep = time.sleep          # тесты подменяют, чтобы не спать
_monotonic = time.monotonic
_now = lambda: datetime.now(timezone.utc)   # noqa: E731
_throttle_lock = threading.Lock()
_last_call = [0.0]

OK_CODES = frozenset({300, 301, 302})
# Чистый отказ (ответ разобран, деньги не двигались) — спека §4.
_CLEAN = frozenset({101, 102, 103, 104, 105, 106, 107, 108, 109, 114, 116, 117, 118, 119, 200, 263, 267})
_AUTH = frozenset({110, 111, 112, 113})
# Исход денежной операции неизвестен — спека §4.
_AMBIGUOUS = frozenset({115, 201, 400, 261, 262, 210})
_NOOP = frozenset({250, 251, 252, 253, 255, 256})     # «уже в нужном состоянии» — не ошибка

_CODE_RU = {
    114: "неверный синтаксис домена", 116: "неверный sandbox-аккаунт", 117: "платёжный профиль не найден",
    118: "платёжный профиль не верифицирован", 119: "недостаточно средств на балансе NameSilo",
    200: "домен неактивен или не принадлежит аккаунту", 263: "неверное число лет",
    267: "зона не поддерживается NameSilo", 110: "неверный ключ API", 111: "нет доступа субаккаунту",
    112: "неверный пользователь", 113: "IP не в списке разрешённых ключа (API Manager)",
}


class NameSiloError(RegistrarError):
    """Чистый отказ NameSilo (код разобран, платной операции не было / деньги не двигались)."""
    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


class NameSiloAuthError(NameSiloError):
    """Ключ/IP/доступ (110-113): останавливать модуль и звать оператора, не долбить повторами."""


class NameSiloReadError(RegistrarError):
    """Чтение не удалось (сеть/5xx/не-JSON/служебный код) — платной операции не было, повтор безопасен."""


class NameSiloAmbiguous(RegistrarAmbiguous):
    """Исход ПЛАТНОЙ операции неизвестен (таймаут/5xx/408/429/не-JSON/115/201/400/261/262/210)."""


def as_list(x) -> list:
    """JSON NameSilo: один дочерний элемент приходит объектом, несколько — списком."""
    if x is None or x == "":
        return []
    return x if isinstance(x, list) else [x]


def _f(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _zone_tail(domain: str) -> str:
    return domain.rsplit(".", 1)[-1].lower()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(v) -> datetime | None:
    """Даты аукционов [не сверено]: ISO, `YYYY-MM-DD[ HH:MM[:SS]]` или unix-секунды."""
    if v in (None, ""):
        return None
    s = str(v).strip()
    if s.isdigit() and len(s) >= 9:
        try:
            return datetime.fromtimestamp(int(s), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


class NameSiloClient:
    name = "namesilo"

    def __init__(self):
        # свой httpx.Client без follow_redirects: ключ в query не должен уехать по редиректу на чужой хост
        self._client = httpx.Client(timeout=READ_TIMEOUT, follow_redirects=False,
                                    headers={"User-Agent": _user_agent()})
        self.api_key = settings.NAMESILO_API_KEY
        self.allow_premium = bool(settings.NAMESILO_ALLOW_PREMIUM)
        self.contact_id = (settings.NAMESILO_CONTACT_ID or "").strip()
        self.base_url = (SANDBOX_URL if settings.NAMESILO_SANDBOX
                         else (settings.NAMESILO_BASE_URL or "").rstrip("/"))

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    # ---- транспорт ----------------------------------------------------------------------------

    def _clean(self, text) -> str:
        """Текст без ключа: и по значению, и по шаблону `key=…` (ключ могли сменить)."""
        s = mask_key_param(text)
        return s.replace(self.api_key, "***") if self.api_key else s

    def _throttle(self) -> None:
        with _throttle_lock:
            wait = MIN_INTERVAL - (_monotonic() - _last_call[0])
            if wait > 0:
                _sleep(wait)
            _last_call[0] = _monotonic()

    def _once(self, op: str, params: dict, money: bool):
        """Один HTTP-вызов -> (reply: dict, code: int). Любой сбой — NameSiloAmbiguous (money) либо
        NameSiloReadError (чтение). Тело читаем при любом HTTP-статусе (401 несёт нормальный JSON)."""
        fail = NameSiloAmbiguous if money else NameSiloReadError
        if not self.api_key:
            raise NameSiloError("NAMESILO_API_KEY не задан", 109)
        if not self.base_url.lower().startswith("https://"):
            raise NameSiloError("NameSilo: адрес API должен быть https:// (ключ идёт в query)", 101)
        self._throttle()
        q = {"version": 1, "type": "json", "key": self.api_key, **params}
        base = AUCTION_URL if (op in AUCTION_OPS and not settings.NAMESILO_SANDBOX) else self.base_url
        try:
            r = self._client.get(f"{base}/{op}", params=q,
                                 timeout=MONEY_TIMEOUT if money else READ_TIMEOUT)
        except httpx.HTTPError as e:
            raise fail(self._clean(f"{op}: {type(e).__name__}: {e}")[:200]) from None
        st = r.status_code
        if st in (408, 429) or st >= 500:
            raise fail(f"{op}: HTTP {st}")
        try:
            body = r.json()
            reply = body["reply"]
            code = int(reply["code"])
        except (ValueError, KeyError, TypeError):
            raise fail(f"{op}: не-JSON/неожиданное тело ответа (HTTP {st})") from None
        if not isinstance(reply, dict):
            raise fail(f"{op}: неожиданное тело ответа (HTTP {st})")
        return reply, code

    def _detail(self, reply: dict) -> str:
        return self._clean(str(reply.get("detail") or reply.get("message") or ""))[:160]

    def _call(self, op: str, params: dict | None = None, *, money: bool = False) -> dict:
        """Вернуть reply при кодах успеха (300/301/302/250-256), иначе исключение по таблице спеки.
        Чтение: ретраи (3 попытки, пауза >= 1 с) на транспорте/5xx/115/201/400. Деньги: ОДНА попытка."""
        params = params or {}
        attempts = 1 if money else READ_ATTEMPTS
        last: Exception | None = None
        for i in range(attempts):
            if i:
                _sleep(2 ** i)
            try:
                reply, code = self._once(op, params, money)
            except NameSiloReadError as e:
                last = e
                continue
            if code in OK_CODES or code in _NOOP:
                return reply
            self._raise_code(op, code, reply, money)
            # чтение с «временным» кодом -> повторяем
            last = NameSiloReadError(f"{op}: код {code} {self._detail(reply)}".strip())
        raise last or NameSiloReadError(f"{op}: нет ответа")

    def _raise_code(self, op: str, code: int, reply: dict, money: bool) -> None:
        det = self._detail(reply)
        why = _CODE_RU.get(code, det)
        if code in _AUTH:
            raise NameSiloAuthError(f"{op}: {code} {why}", code)
        if code == 210 and reply.get("claims"):
            raise NameSiloError(f"{op}: 210 нужно TMCH-уведомление (claims) — подтверждай вручную на сайте NameSilo", 210)
        if money and code in _AMBIGUOUS:
            raise NameSiloAmbiguous(f"{op}: код {code} {det}".strip())
        if code in _CLEAN or code == 254 or code == 280:
            raise NameSiloError(f"{op}: {code} {why}".strip(), code)
        if money:                                   # неизвестный код на платной операции — НЕ отказ, а неизвестность
            raise NameSiloAmbiguous(f"{op}: неизвестный код {code} {det}".strip())
        if code in _AMBIGUOUS:
            return                                  # чтение: временный код, вызывающий повторит
        raise NameSiloError(f"{op}: код {code} {det}".strip(), code)

    # ---- чтение -------------------------------------------------------------------------------

    def ping(self) -> bool:
        """getAccountBalance: денег не тратит. True — ключ и IP приняты и баланс есть в ответе."""
        return self.balance() is not None

    def balance(self) -> Money | None:
        """None — поля balance в ответе нет (неизвестность, а не 0 — урок optimizator)."""
        b = _f(self._call("getAccountBalance").get("balance"))
        return Money(b, CURRENCY) if b is not None else None

    def check_many(self, domains: list[str]) -> dict:
        """{домен: {"status": available|unavailable|invalid, price, premium, duration}} — батчи по 200."""
        out: dict = {}
        names = [d.strip().lower() for d in domains if d and d.strip()]
        for i in range(0, len(names), BATCH):
            part = names[i:i + BATCH]
            reply = self._call("checkRegisterAvailability", {"domains": ",".join(part)})
            for row in as_list(reply.get("available")):
                if not isinstance(row, dict) or not row.get("domain"):
                    continue
                out[str(row["domain"]).lower()] = {
                    "status": "available", "price": _f(row.get("price")),
                    "premium": 1 if str(row.get("premium", "0")) in ("1", "true", "True") else 0,
                    "duration": int(_f(row.get("duration")) or 0) or None}
            for key in ("unavailable", "invalid"):
                for row in as_list(reply.get(key)):
                    d = row.get("domain") if isinstance(row, dict) else row
                    if d:
                        out.setdefault(str(d).lower(), {"status": key})
        return out

    def check_available(self, domain: str) -> bool:
        return self.check_many([domain]).get(domain.strip().lower(), {}).get("status") == "available"

    def price(self, domain: str, auction: bool = False) -> Money:
        """Цена регистрации на 1 год в USD (свежая котировка checkRegisterAvailability). Отказ, если
        домен не доступен, премиум (без флага оператора) или цена дана за другой срок.
        `auction=True` — домен с аукциона просроченных: котировка = текущая ставка."""
        d = domain.strip().lower()
        if auction:
            a = self.find_auction(d)
            return Money(a["bid"], CURRENCY)
        row = self.check_many([d]).get(d)
        if not row or row["status"] != "available":
            raise NameSiloError(f"{d}: недоступен для регистрации в NameSilo "
                                f"({(row or {}).get('status', 'нет в ответе')})")
        if row["premium"] and not self.allow_premium:
            raise NameSiloError(f"{d}: премиум-домен (цена {row['price']} {CURRENCY}) — отказ; "
                                f"разрешить можно флагом NAMESILO_ALLOW_PREMIUM на экране ключей")
        if row["price"] is None or row["price"] <= 0:
            raise NameSiloError(f"{d}: в котировке нет цены")
        if row["duration"] != 1:
            raise NameSiloError(f"{d}: цена дана за {row['duration']} лет, а регистрируем на 1 год — не сверить")
        return Money(row["price"], CURRENCY)

    def renew_price(self, domain: str) -> Money:
        """Цена продления зоны на 1 год (getPrices, чтение). Победитель аукциона платит ставку И год
        продления (спека §6), поэтому гейт обязан показать человеку сумму с ним. Нет цены — отказ."""
        from app.services.domain_filters import zone_of
        d = domain.strip().lower()
        zone = zone_of(d)
        row = self._call("getPrices").get(zone)       # [?] ключ составных зон (co.uk) не сверен — тогда отказ
        v = _f(row.get("renew")) if isinstance(row, dict) else None
        if v is None or v <= 0:
            raise NameSiloError(f"{d}: в getPrices нет цены продления зоны .{zone} — сумму списания не посчитать")
        return Money(v, CURRENCY)

    def domain_info(self, domain: str) -> dict | None:
        """reply getDomainInfo; None — код 200 (домен не наш / неактивен: нормальный ответ)."""
        try:
            return self._call("getDomainInfo", {"domain": domain})
        except NameSiloError as e:
            if e.code == 200:
                return None
            raise

    @staticmethod
    def _ns_of(info: dict) -> list[str]:
        out = []
        for n in as_list(info.get("nameservers")):
            v = n.get("nameserver") if isinstance(n, dict) else n
            if v:
                out.append(str(v).strip().lower().rstrip("."))
        return out

    # ---- регистрация (ДЕНЬГИ) -----------------------------------------------------------------

    def register(self, domain: str, period: int = 1) -> dict:
        """registerDomain на 1 год. Порядок: (1) adopt — getDomainInfo: домен уже наш -> успех БЕЗ отправки;
        (2) один запрос /apibatch/registerDomain, БЕЗ ретраев; (3) успех только 300/301/302 + domain +
        order_amount. Неизвестный исход -> NameSiloAmbiguous (вызывающий ставит maybe_sent)."""
        d = domain.strip().lower()
        if period != 1:
            raise NameSiloError(f"{d}: регистрируем строго на 1 год (period={period})", 263)
        info = self.domain_info(d)          # чтение до отправки: сбой = чистый отказ, ничего не ушло
        if info is not None:
            status = str(info.get("status") or "")
            if status.lower() == "active":
                return {"order_id": "", "domain": d, "adopted": True, "currency": CURRENCY,
                        "nameservers": self._ns_of(info),
                        "note": "домен уже наш в NameSilo (getDomainInfo) — второй registerDomain не шлём"}
            raise NameSiloError(f"{d}: домен есть в аккаунте, но статус «{status}» — разбери вручную")
        params = {"domain": d, "years": 1, "auto_renew": 0}
        if _zone_tail(d) not in NO_PRIVACY:
            params["private"] = 1
        if self.contact_id:
            params["contact_id"] = self.contact_id
        reply = self._call("registerDomain", params, money=True)     # money: одна попытка, мимо ретраев
        code = int(reply["code"])
        got = str(reply.get("domain") or "").strip().lower()
        amount = _f(reply.get("order_amount"))
        if got != d or amount is None:
            # 300 без domain/order_amount или про другой домен — форма успеха не подтверждена
            raise NameSiloAmbiguous(f"registerDomain: код {code}, но domain/order_amount не подтверждены "
                                    f"(domain={got!r}, order_amount={reply.get('order_amount')!r})")
        warns = []
        if code == 301:
            warns.append("301: NS не приняты, взяты дефолтные NameSilo — выполни changeNameServers и сверь")
        if code == 302:
            warns.append("302: контакт с ошибкой, WHOIS на профиле по умолчанию")
        return {"order_id": "", "domain": d, "order_amount": amount, "currency": CURRENCY, "code": code,
                "warnings": warns}

    def set_nameservers(self, domain: str, ns: list[str]) -> dict:
        names = [n.strip().rstrip(".") for n in ns if n and n.strip()]
        if not 2 <= len(names) <= 13:
            raise NameSiloError(f"{domain}: нужно от 2 до 13 NS, получено {len(names)}")
        params = {"domain": domain.strip().lower(), **{f"ns{i}": n for i, n in enumerate(names, 1)}}
        self._call("changeNameServers", params)
        info = self.domain_info(domain.strip().lower())
        have = set(self._ns_of(info)) if info else set()
        want = {n.lower() for n in names}
        return {"ok": True, "verified": have == want, "nameservers": sorted(have)}

    def reconcile(self, domain: str, since: datetime, balance_before: float | None) -> tuple[str, str]:
        """Сверка неизвестного исхода registerDomain: getDomainInfo -> listOrders -> баланс.
        ("registered"|"not_registered"|"unknown", пояснение). "not_registered" — только если с отправки
        прошло >= RECONCILE_MIN_AGE, заказа нет, а баланс не изменился; любое расхождение — "unknown"
        (остаётся maybe_sent, отмена заблокирована). Повтор отправки — только новым подтверждением."""
        d = domain.strip().lower()
        try:
            info = self.domain_info(d)
            if info is not None:
                if str(info.get("status") or "").lower() == "active":
                    return "registered", "getDomainInfo: домен Active у нас"
                return "unknown", f"getDomainInfo: статус «{info.get('status')}»"
            if _now() - since < RECONCILE_MIN_AGE:
                return "unknown", "рано судить: серия сверок (30 с / 2 / 5 / 15 мин) ещё не закончилась"
            frm = (since - timedelta(days=1)).strftime("%Y-%m-%d")
            to = (_now() + timedelta(days=1)).strftime("%Y-%m-%d")
            lo = self._call("listOrders", {"date_from": frm, "date_to": to})     # [не сверено] ключи ответа
            for o in as_list(lo.get("order") or lo.get("orders") or lo.get("entry")):
                num = o.get("order_number") if isinstance(o, dict) else None
                if not num:
                    continue
                det = self._call("orderDetails", {"order_number": num})
                text = json.dumps(det, ensure_ascii=False).lower()
                if d in text and "registration" in text:
                    return "unknown", f"в заказе №{num} есть регистрация {d}, но домен ещё не Active"
            bal = self.balance()
        except (RegistrarError, RegistrarAmbiguous) as e:
            return "unknown", f"сверка не удалась: {self._clean(e)}"[:200]
        if bal is None or balance_before is None:
            return "unknown", "баланс до отправки или сейчас неизвестен"
        if abs(bal.amount - float(balance_before)) <= 0.005:
            return "not_registered", ("выверено: домена нет, заказа нет, баланс не менялся — "
                                      "повтор только новым подтверждением человека")
        return "unknown", f"баланс изменился ({balance_before} -> {bal.amount}), а домена нет — разбор вручную"

    # ---- аукционы просроченных (typeId=3) -----------------------------------------------------

    @staticmethod
    def _parse_auction(row: dict) -> dict | None:
        """Поля живого ответа (фикстура namesilo_list_auctions_live.json, 2026-10-10): id, domain, currentBid,
        maxBid, openingBid, hasBids, domainCreatedOn, auctionEndsOnUtc. Старые имена из документации оставлены
        запасными. Обязателен только `domain`."""
        if not isinstance(row, dict) or not row.get("domain"):
            return None
        pick = lambda *ks: next((row[k] for k in ks if row.get(k) not in (None, "")), None)   # noqa: E731
        return {"domain": str(row["domain"]).strip().lower(),
                "auction_id": pick("id", "auctionId", "auction_id"),
                "bid": _f(pick("currentBid", "current_bid", "bid", "price", "openingBid")),
                "opening": _f(pick("openingBid", "opening_bid")),
                "max_bid": _f(pick("maxBid", "max_bid")),
                "has_bids": bool(row.get("hasBids")) if row.get("hasBids") is not None else None,
                "end": _parse_dt(pick("auctionEndsOnUtc", "endDate", "end_date", "closeDate", "ends", "end",
                                      "auctionEndsOn")),
                "created": _parse_dt(pick("domainCreatedOn", "created", "createdDate", "registered"))}

    def list_auctions(self, page: int = 1, page_size: int = AUCTION_PAGE_SIZE,
                      domain: str | None = None) -> list[dict]:
        params = {"typeId": 3, "statusId": 2, "page": page, "pageSize": page_size}
        if domain:
            params["domainName"] = domain          # фильтр по имени — из документации list-auctions
        reply = self._call("listAuctions", params)
        raw = reply.get("body")                    # живой формат: reply.body = список лотов
        if raw is None:
            raw = reply.get("auctions") if reply.get("auctions") is not None else reply.get("auction")
        if isinstance(raw, dict) and "auction" in raw:
            raw = raw["auction"]
        return [a for a in (self._parse_auction(r) for r in as_list(raw)) if a]

    def list_dropping(self) -> list[dict]:
        """Источник discovery: аукционы просроченных доменов NameSilo (дата создания сохраняется).
        Строки в формате автоисточников: lane=bid, acquire_deadline = конец аукциона (UTC).
        Лоты с уже прошедшим концом (живьём statusId=2 держит и такие) — пропускаем."""
        rows: list[dict] = []
        now = _utcnow()
        for page in range(1, AUCTION_MAX_PAGES + 1):
            part = self.list_auctions(page)
            # created -> Domain.whois_created (возраст сохраняется — главное преимущество лота),
            # bid -> Domain.acquire_price (текущая ставка на момент discovery)
            rows += [{"domain": a["domain"], "source": "namesilo_auction", "lane": "bid",
                      "acquire_deadline": a["end"], "created": a.get("created"),
                      "bid": a.get("bid")} for a in part if not (a["end"] and a["end"] < now)]
            if len(part) < AUCTION_PAGE_SIZE:
                break
        return rows

    def find_auction(self, domain: str) -> dict:
        """Свежая запись аукциона по домену: сначала фильтр domainName, затем постраничный просмотр.
        Нет — чистый отказ."""
        d = domain.strip().lower()
        for a in self.list_auctions(1, 50, domain=d):
            if a["domain"] == d:
                return a
        for page in range(1, FIND_AUCTION_MAX_PAGES + 1):
            part = self.list_auctions(page)
            for a in part:
                if a["domain"] == d:
                    if a["auction_id"] is None or a["bid"] is None:
                        raise NameSiloError(f"{d}: в записи аукциона нет id или ставки — формат не сверен")
                    return a
            if len(part) < AUCTION_PAGE_SIZE:
                break
        raise NameSiloError(f"{d}: на аукционах просроченных NameSilo не найден (мог закрыться)")

    def bid(self, domain: str, max_bid: float) -> dict:
        """bidAuction — ДЕНЬГИ (ставка с proxyBid блокирует средства). Только после confirmed_by_human
        (execute_confirmed_order). Потолок = подтверждённая сумма: текущая ставка выше -> отказ ДО отправки.
        Без ретраев. [не сверено] имена параметров auctionId/bid — неверное имя даст чистый отказ 105/106."""
        a = self.find_auction(domain)
        if a.get("end") and a["end"] < _utcnow():
            raise NameSiloError(f"{domain}: аукцион уже завершён ({a['end']:%Y-%m-%d %H:%M} UTC) — ставка не "
                                "отправлена; сними заявку или дождись нового лота")
        if a["bid"] > float(max_bid):
            raise NameSiloError(f"{domain}: ставка на аукционе выросла ({a['bid']:.2f} > подтверждённых "
                                f"{float(max_bid):.2f} {CURRENCY}) — подтверди заказ заново")
        # Документация bid-auction: `bid` пишется в историю как есть, `proxyBid` — потолок для автоставок.
        # Потолок человека — это proxyBid; сама ставка — минимальный шаг над текущей (hasBids) или
        # стартовая. Слать потолок как `bid` значило бы платить максимум сразу. [шаг ставки не сверен:
        # слишком низкий `bid` даст чистый отказ кодом, не списание]
        step = (a["bid"] or 0.0) + 1.0 if a.get("has_bids") else max(a.get("opening") or 0.0, a["bid"] or 0.0, 1.0)
        bid_now = min(float(max_bid), step)
        self._call("bidAuction", {"auctionId": a["auction_id"], "bid": f"{bid_now:.2f}",
                                  "proxyBid": f"{float(max_bid):.2f}"}, money=True)
        return {"order_id": str(a["auction_id"]), "domain": a["domain"], "auction_id": a["auction_id"],
                "bid": float(max_bid), "bid_now": bid_now, "currency": CURRENCY,
                "note": "ставка принята (proxy до потолка); итог аукциона — после его завершения"}
