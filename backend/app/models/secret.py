"""Переопределения ключей/адресов сервисов, заданные оператором в панели («Ключи и сервисы»).

Читает config.Settings: значение из этой таблицы побеждает .env (без рестарта, во всех
процессах — backend и worker смотрят в одну БД). Белый список ключей — services/api_keys.py;
сюда не попадает ничего, чего нет в нём.
"""
from datetime import datetime

from sqlalchemy import DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class SecretOverride(Base):
    __tablename__ = "secret_override"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    # ponytail: значение лежит в БД как есть, без шифрования — панель в LAN под Basic-auth, а
    # ключ шифрования всё равно жил бы рядом (в том же .env). В UI значение целиком не отдаётся.
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
