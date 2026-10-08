"""Списки чистоты доменов (UT1 blacklists, blocklistproject): «домен числится в gambling/adult/…».

Заполняет services/domain_lists.py раз в сутки, читает волна risk скоринга. Хранится только то, что
может совпасть с кандидатами (регистрируемые имена из зон белого списка) — иначе adult-список UT1
(миллионы хостов) раздул бы таблицу впустую.
"""
from datetime import datetime
from sqlalchemy import DateTime, Index, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column
from app.db import Base


class DomainList(Base):
    __tablename__ = "domain_list"
    __table_args__ = (UniqueConstraint("domain", "category", "source", name="uq_domain_list"),
                      Index("ix_domain_list_src_cat", "source", "category"))

    id: Mapped[int] = mapped_column(primary_key=True)
    domain: Mapped[str] = mapped_column(String(255), index=True)       # нижний регистр, без www.
    category: Mapped[str] = mapped_column(String(32))                  # adult|gambling|phishing|malware|drugs
    source: Mapped[str] = mapped_column(String(16))                    # ut1|blp
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
