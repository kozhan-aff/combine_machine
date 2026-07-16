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
import httpx

from app.config import settings
from app.integrations.base import BaseClient


class OptimizatorError(Exception):
    """Провайдер вернул {"error": ..., "error_id": ...} — ЧИСТЫЙ отказ (HTTP успешен,
    ответ разобран, провайдер explicitly сказал "нет"). Деньги НЕ ушли — безопасно
    показать человеку и безопасно позволить retry."""
    def __init__(self, message: str, error_id: int | None = None):
        super().__init__(f"{message} (error_id={error_id})" if error_id else message)
        self.error_id = error_id


class OptimizatorAmbiguous(Exception):
    """Транспорт упал (timeout/5xx/соединение) ПОСЛЕ отправки денежного запроса —
    исход НЕИЗВЕСТЕН, как AmbiguousSend у backorder. НЕ давать retry вслепую."""


def _unwrap(data) -> dict:
    """[{...}] -> {...}; поднимает OptimizatorError на форму {"error":..., "error_id":...}."""
    row = data[0] if isinstance(data, list) and data else {}
    if isinstance(row, dict) and "error" in row:
        raise OptimizatorError(row.get("error", "unknown error"), row.get("error_id"))
    return row if isinstance(row, dict) else {}


class OptimizatorClient(BaseClient):
    def __init__(self):
        super().__init__("http://optimizator.ru")
        self.api_key = settings.OPTIMIZATOR_API_KEY
        self.nicd = settings.OPTIMIZATOR_NICD

    def _get(self, action: str, **params) -> dict:
        p = {"a": "api", "sa": action, "api_key": self.api_key, **params}
        r = self.request("GET", self.base_url + "/", params=p)
        return _unwrap(r.json())

    def ping(self) -> bool:
        """Живость + auth — balance ничего не стоит (read-only)."""
        self._get("balance")
        return True

    def balance(self) -> float:
        return float(self._get("balance").get("balance") or 0)

    def prices(self, zone: str = "ru") -> dict:
        return self._get("prices", domain=zone)

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
                return _unwrap(r.json())
        except OptimizatorError:
            raise
        except Exception as e:  # noqa: BLE001 — транспорт/JSON-сбой ПОСЛЕ отправки денежного запроса
            raise OptimizatorAmbiguous(f"{type(e).__name__}: {e}") from e
