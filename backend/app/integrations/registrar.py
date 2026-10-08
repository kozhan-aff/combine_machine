"""Registrar nameserver management (.ru: reg.ru / nic.ru). Transport only.

Needed by M3 provisioning step 2: after creating a Cloudflare zone, point the
domain's NS to Cloudflare's nameservers, then wait for propagation.

reg.ru API v2: https://api.reg.ru/api/regru2/  (username/password or signature+SSL cert)
  method for NS update: domain/update_nss (set ns0/ns1 to Cloudflare's)
nic.ru has its own API. Some resellers expose NS changes too.
"""
from typing import NamedTuple, Protocol, runtime_checkable

from app.config import settings
from app.integrations.base import BaseClient


# ---- Шов под международный выкуп (S3-01/F8-13) ----------------------------------------------
# Выбор провайдера (NameSilo / Dynadot / …) — решение оператора, не кода: здесь только ИНТЕРФЕЙС и
# заглушка «не настроен». Денежный гейт (confirm/execute, TTL, баланс, maybe_sent) живёт в
# services/acquisition и ОДИН для всех каналов — реализация регистратора его не дублирует и не обходит.

class Money(NamedTuple):
    amount: float
    currency: str            # «USD», «RUB» … — сумма без валюты на денежном экране недопустима


class RegistrarError(Exception):
    """ЧИСТЫЙ отказ регистратора (нет средств, домен занят, зона не продаётся): деньги не ушли."""


class RegistrarAmbiguous(Exception):
    """Исход НЕИЗВЕСТЕН (таймаут/5xx/неразобранный ответ ПОСЛЕ отправки платного запроса):
    деньги могли уйти. Как AmbiguousSend у backorder — слепой повтор запрещён."""


class RegistrarNotConfigured(RegistrarError):
    """Канал не настроен: реальный провайдер ещё не выбран/не подключён."""


@runtime_checkable
class Registrar(Protocol):
    """Контракт канала выкупа по API регистратора. Все методы транспортные, без БД и без гейта."""
    name: str
    configured: bool

    def check_available(self, domain: str) -> bool: ...
    def price(self, domain: str) -> Money: ...
    def register(self, domain: str, period: int = 1) -> dict:
        """ИДЕМПОТЕНТНО: домен уже наш -> успех без второго списания. Платный вызов — без ретрая
        транспорта; неизвестный исход -> RegistrarAmbiguous. Успех = dict с подтверждённой формой."""
        ...
    def set_nameservers(self, domain: str, ns: list[str]) -> dict: ...
    def balance(self) -> Money | None: ...


class NotConfiguredRegistrar:
    """Заглушка канала «registrar»: любая операция — RegistrarNotConfigured с понятной причиной."""
    name = "registrar"
    configured = False

    def _no(self, *_a, **_k):
        raise RegistrarNotConfigured(
            "международный регистратор не настроен: провайдер выбирает оператор "
            "(пока домен покупается руками — «купил руками» на экране Домены)")

    check_available = price = register = set_nameservers = balance = _no


def get_registrar() -> Registrar:
    """Текущий реализованный регистратор. Пока провайдер не выбран — заглушка. Подключение
    реального клиента = вернуть его здесь (и ничего больше не менять в acquisition)."""
    return NotConfiguredRegistrar()


class RegistrarClient(BaseClient):
    def __init__(self):
        super().__init__("https://api.reg.ru/api/regru2")
        self.username = settings.REGRU_USERNAME
        self.password = settings.REGRU_PASSWORD

    def set_nameservers(self, domain: str, nameservers: list[str]) -> dict:
        """Point domain NS to Cloudflare's assigned nameservers. TODO."""
        raise NotImplementedError

    def ping(self) -> bool:
        raise NotImplementedError
