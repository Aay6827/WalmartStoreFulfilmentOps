from datetime import datetime, timezone

from sqlalchemy import CheckConstraint, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Order(Base):
    __tablename__ = "orders"
    order_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    sku: Mapped[str] = mapped_column(String(64), nullable=False)
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
    slot_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # pending, confirmed, fulfilling, fulfilled, compensating, rejected, cancelled
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    decision_reason: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    compensate_to: Mapped[str | None] = mapped_column(String(16), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    __table_args__ = (CheckConstraint("qty > 0", name="ck_qty_pos"),)