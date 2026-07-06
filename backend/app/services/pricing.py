"""Цена выкупа бэкордер-доменов: базовый тариф (публичный JSON) → acquire_price.

Живую аукционную per-domain цену бесплатный фид не отдаёт (спек §L) — храним базовую.
Кэш тарифа на процесс, чтобы discovery проставлял цену при вставке без лишних запросов;
кнопка «Обновить цены» перечитывает и обновляет всех backorder-доменов.
"""
_TARIFF: dict = {"price": None}          # кэш на процесс; refresh перезаписывает


def cached_backorder_price() -> float | None:
    return _TARIFF.get("price")


def refresh_backorder_prices() -> int:
    """Перечитать тариф, проставить acquire_price/price_checked_at всем backorder-доменам.
    Возвращает число обновлённых. Дёшево (один публичный JSON), денег не тратит."""
    from datetime import datetime, timezone
    from sqlalchemy import select
    from app.db import SessionLocal
    from app.models.domain import Domain
    from app.integrations.backorder import BackorderClient

    price = BackorderClient().get_tariffs().get("price")
    _TARIFF["price"] = price
    if price is None:
        return 0
    now = datetime.now(timezone.utc)
    n = 0
    with SessionLocal() as db:
        for d in db.execute(select(Domain).where(Domain.source == "backorder")).scalars():
            d.acquire_price = price
            d.price_checked_at = now
            n += 1
        db.commit()
    return n
