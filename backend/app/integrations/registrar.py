"""Шов регистратора для международных зон: протокол Registrar, заглушка «не настроен», выбор клиента.

Реализация — integrations/namesilo.py (NameSiloClient). M2 (acquisition) зовёт price/balance/register/bid,
M3 (provisioning) — set_nameservers после создания зоны Cloudflare. Денежный гейт живёт в acquisition
и один на все каналы.
"""
from typing import NamedTuple, Protocol, runtime_checkable

from app.config import settings


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
    def price(self, domain: str, auction: bool = False) -> Money:
        """Свежая котировка в ЯВНОЙ валюте. auction=True — лот аукциона: котировка = текущая ставка."""
        ...
    def renew_price(self, domain: str) -> Money:
        """Цена продления на год: победитель аукциона платит ставку + год продления."""
        ...
    def bid(self, domain: str, max_bid: float) -> dict:
        """Ставка на аукционе (ДЕНЬГИ, за тем же гейтом confirmed_by_human; max_bid — потолок человека)."""
        ...
    def reconcile(self, domain: str, since, balance_before: float | None) -> tuple[str, str]:
        """Сверка неизвестного исхода: ("registered"|"not_registered"|"unknown", пояснение). Только чтение."""
        ...
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

    check_available = price = renew_price = bid = reconcile = register = set_nameservers = balance = _no


def get_registrar() -> Registrar:
    """Текущий регистратор: NameSilo, если задан NAMESILO_API_KEY (в т.ч. из панели), иначе заглушка.
    Денежный гейт от клиента не зависит — он в acquisition и общий для всех каналов."""
    if settings.NAMESILO_API_KEY:
        from app.integrations.namesilo import NameSiloClient
        return NameSiloClient()
    return NotConfiguredRegistrar()
