"""Цена выкупа бэкордер-доменов: базовый тариф (публичный JSON) → acquire_price.

Живую аукционную per-domain цену бесплатный фид не отдаёт (спек §L) — храним базовую,
ОТДЕЛЬНО ПО ЗОНЕ (S2, аудит 2026-07-18): backorder держит разные сетки тарифов для
.RU и .РФ, один тариф на все домены давал .рф-доменам чужую (RU) цену. Кэш тарифа на
процесс, чтобы discovery проставлял цену при вставке без лишних запросов; кнопка
«Обновить цены» перечитывает и обновляет всех backorder-доменов, каждого — по его
зоне.
"""
_TARIFF: dict = {}          # кэш на процесс, по зоне: {".RU": price, ".РФ": price}


def cached_backorder_price(zone: str = ".RU") -> float | None:
    return _TARIFF.get(zone)


def refresh_backorder_prices() -> int:
    """Перечитать тарифы (по зоне домена), проставить acquire_price/price_checked_at
    всем backorder-доменам. Возвращает число обновлённых. Дёшево (тарифная сетка обеих
    зон приходит одним публичным JSON-запросом, см. BackorderClient.tariffs), денег
    не тратит."""
    from datetime import datetime, timezone
    from sqlalchemy import select
    from app.db import SessionLocal
    from app.models.domain import Domain
    from app.integrations.backorder import BackorderClient, zone_of

    client = BackorderClient()
    now = datetime.now(timezone.utc)
    n = 0
    with SessionLocal() as db:
        for d in db.execute(select(Domain).where(Domain.source == "backorder")).scalars():
            zone = zone_of(d.domain) or ".RU"
            if zone not in _TARIFF:
                _TARIFF[zone] = client.get_tariffs(zone).get("price")
            price = _TARIFF[zone]
            if price is None:
                continue
            d.acquire_price = price
            d.price_checked_at = now
            n += 1
        db.commit()
    return n
