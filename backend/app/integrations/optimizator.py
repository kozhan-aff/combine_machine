"""optimizator.ru client (register already-free domains via RU-CENTER reseller API).
Transport only. Second M2 acquisition channel — "свободные чистые → optimizator
(гарантия)" (CLAUDE.md), complementing backorder's competitive-bid channel.

Live-verified format (2026-07-16, real key): GET/POST
http://optimizator.ru/?a=api&sa=<action>&api_key=KEY -> JSON ARRAY, even for single
values. Error shape (NOT documented anywhere in the site's text, found live via
check_nicd on an untransferred anketa): [{"error": "...", "error_id": 411}].

Documented-but-not-live-tested actions (reg_domains/renew_domains spend money;
check_order/check_domain need an existing order under this nicd, which doesn't exist
yet — balance is 0 and the anketa is not transferred, see design doc "Блокеры"):
reg_domains, check_order, check_domain, renew_domains. Same envelope shape as the
three actions that WERE live-verified (balance/prices/check_nicd) — same provider,
same wrapper, reasonable to trust the shape.

See docs/superpowers/specs/2026-07-16-optimizator-integration-design.md.
"""
import re

import httpx

from app.config import settings
from app.integrations.base import BaseClient

# Цены провайдера — в рублях (док: «регистрация ≈ 179 руб.»). Валюта явная, а не подразумеваемая:
# у международных каналов она будет другой, и сумма без валюты на денежном экране — ловушка (S3-08).
CURRENCY = "RUB"
# Таймаут «быстрых» чтений (баланс, сверка на /queue): без ретрая 3 x 30 с на рендере денежного экрана.
QUICK_TIMEOUT = 8.0

_KEY_IN_URL = re.compile(r"(api_key=)[^&\s'\"]+", re.I)


def scrub(text: str) -> str:
    """Затереть api_key/nicd в тексте (S3-05). httpx кладёт ПОЛНЫЙ URL с ?api_key=… в текст
    HTTPStatusError, а текст исключения уезжал в acquisition_orders.result и на /queue.
    Режем и по значению из настроек, и по шаблону `api_key=…` (ключ могли успеть сменить)."""
    s = _KEY_IN_URL.sub(r"\1***", str(text))
    for secret in (settings.OPTIMIZATOR_API_KEY, settings.OPTIMIZATOR_NICD):
        if secret:
            s = s.replace(secret, "***")
    return s


def _err_text(e: Exception) -> str:
    return scrub(f"{type(e).__name__}: {e}")[:200]


class OptimizatorError(Exception):
    """Провайдер вернул {"error": ..., "error_id": ...} — ЧИСТЫЙ отказ (HTTP успешен,
    ответ разобран, провайдер explicitly сказал "нет"). Деньги НЕ ушли — безопасно
    показать человеку и безопасно позволить retry."""
    def __init__(self, message: str, error_id: int | None = None):
        super().__init__(f"{message} (error_id={error_id})" if error_id else message)
        self.error_id = error_id


class ZoneNotSold(OptimizatorError):
    """Провайдер не продаёт эту зону (prices вернул пустой список). ЧИСТЫЙ отказ ДО денег —
    не «неизвестный исход» (S3-08): оператору надо сказать «зона не поддерживается»."""


class OptimizatorAmbiguous(Exception):
    """Транспорт упал (timeout/5xx/соединение) ПОСЛЕ отправки денежного запроса —
    исход НЕИЗВЕСТЕН, как AmbiguousSend у backorder. НЕ давать retry вслепую."""


def _unwrap(data) -> dict:
    """[{...}] -> {...}; поднимает OptimizatorError на форму {"error":..., "error_id":...},
    OptimizatorAmbiguous на пустой/нераспознанный ответ (ни ошибки, ни данных — это НЕ
    успех с пустым результатом, у этого провайдера такого не бывает ни на одном
    подтверждённом живом вызове; это неизвестность, а не факт)."""
    row = data[0] if isinstance(data, list) and data else None
    if isinstance(row, dict) and "error" in row:
        raise OptimizatorError(row.get("error", "unknown error"), row.get("error_id"))
    if not isinstance(row, dict) or not row:
        raise OptimizatorAmbiguous(f"пустой/неожиданный ответ: {data!r}"[:200])
    return row


class OptimizatorClient(BaseClient):
    def __init__(self, quick: bool = False):
        super().__init__(settings.OPTIMIZATOR_BASE_URL)
        self.api_key = settings.OPTIMIZATOR_API_KEY
        self.nicd = settings.OPTIMIZATOR_NICD
        # quick: чтения на путях, где ждёт человек или опрос с дедлайном (баланс /queue, поллинг) —
        # один запрос с коротким таймаутом, без 3 ретраев x 30 с. Денежный register() от флага не зависит.
        self.quick = quick

    def _fetch(self, action: str, **params):
        """GET -> разобранный JSON как есть. Транспорт/JSON-сбой = OptimizatorAmbiguous (текст скрабится)."""
        p = {"a": "api", "sa": action, "api_key": self.api_key, **params}
        kw = {"retry": False, "timeout": QUICK_TIMEOUT} if self.quick else {}
        try:
            r = self.request("GET", self.base_url + "/", params=p, **kw)
            return r.json()
        except Exception as e:  # noqa: BLE001 — транспорт/JSON-сбой, тот же принцип, что в register()
            raise OptimizatorAmbiguous(_err_text(e)) from e

    def _get(self, action: str, **params) -> dict:
        return _unwrap(self._fetch(action, **params))

    def ping(self) -> bool:
        """Живость + auth — balance ничего не стоит (read-only)."""
        self._get("balance")
        return True

    def balance(self) -> float | None:
        """None — поле "balance" отсутствует в ответе (неизвестность), НЕ то же самое,
        что подтверждённый 0 ₽ (было `.get("balance") or 0`, маскировало отсутствие
        ключа под факт — см. BackorderClient.balance())."""
        b = self._get("balance").get("balance")
        return float(b) if b is not None else None

    def prices(self, zone: str = "ru") -> dict:
        """Цены зоны + явная валюта. Пустой список = провайдер зону не продаёт (живая проба:
        uk/co.uk/de/mx -> []) — `ZoneNotSold`, чистый отказ, а не Ambiguous (S3-08)."""
        data = self._fetch("prices", domain=zone)
        if isinstance(data, list) and not data:
            raise ZoneNotSold(f"optimizator не продаёт зону .{zone}")
        return {**_unwrap(data), "currency": CURRENCY}

    def check_nicd(self) -> bool:
        """True — анкета под управлением Optimizator. False — конкретно error_id=411
        (анкета не передана, живьём подтверждённый случай). Любая ДРУГАЯ ошибка —
        не гадаем её смысл, пробрасываем как есть (см. design doc)."""
        try:
            self._get("check_nicd", nicd=self.nicd)
            return True
        except OptimizatorError as e:
            if e.error_id == 411:
                return False
            raise

    def order_status(self, order_id: int) -> dict:
        return self._get("check_order", order_id=order_id)

    def check_domain(self, domain: str) -> dict:
        """Успех = домен под управлением нашей анкеты (может быть продлён). Как и все
        методы — бросает OptimizatorError/Ambiguous на отказ/сбой, нет отдельного
        None-сентинела (нет живых данных о форме ответа "домен не наш")."""
        return self._get("check_domain", domain=domain)

    def register(self, domains: list[str]) -> dict:
        """reg_domains — ДЕНЬГИ. Мимо retry BaseClient (как BackorderClient.order():
        3 ретрая = 3 попытки списания за одну команду). До 30 доменов, см. дока."""
        p = {"a": "api", "sa": "reg_domains", "api_key": self.api_key,
             "nicd": self.nicd, "domains": " ".join(domains), "enc": "utf8"}
        try:
            with httpx.Client(timeout=30.0) as client:
                r = client.get(self.base_url + "/", params=p)
                r.raise_for_status()
                res = _unwrap(r.json())
        except OptimizatorError:
            raise
        except OptimizatorAmbiguous:
            raise
        except Exception as e:  # noqa: BLE001 — транспорт/JSON-сбой ПОСЛЕ отправки денежного запроса
            raise OptimizatorAmbiguous(_err_text(e)) from e
        # Подтверждённая форма успеха — [{"order_id": N}] (док. texts/12). Любой другой непустой dict
        # ('ordered' по нему ставить нельзя, S3-10): деньги могли уйти, а номера заказа у нас нет.
        if not res.get("order_id"):
            raise OptimizatorAmbiguous(
                f"reg_domains: нет order_id в ответе, форма успеха не подтверждена: {scrub(res)!s}"[:200])
        return res
