"""订单持久化模型（PostgreSQL）。"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from src.models.session import Base


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    order_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    email: Mapped[str] = mapped_column(String(255), default="", index=True)
    status: Mapped[str] = mapped_column(String(64), default="Unknown")
    destination_country: Mapped[str] = mapped_column(String(8), default="")
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    total: Mapped[float] = mapped_column(Float, default=0.0)
    tracking_number: Mapped[str] = mapped_column(String(128), default="")
    carrier: Mapped[str] = mapped_column(String(64), default="")
    created_at_biz: Mapped[str] = mapped_column(String(64), default="")
    items: Mapped[list] = mapped_column(JSON().with_variant(JSONB(), "postgresql"), default=list)
    source: Mapped[str] = mapped_column(String(32), default="csv")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
