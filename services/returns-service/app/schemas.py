from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ReturnIn(BaseModel):
    return_id: str = Field(min_length=1, max_length=64)
    order_id: str = Field(min_length=1, max_length=64)
    sku: str = Field(min_length=1, max_length=64)
    qty: int = Field(gt=0)
    unit_price_cents: int = Field(ge=0)
    reason: str = Field(min_length=1, max_length=200)
    purchased_at: datetime

    @field_validator("purchased_at")
    @classmethod
    def _to_utc(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            return v.replace(tzinfo=timezone.utc)
        return v.astimezone(timezone.utc)


class ReceiveIn(BaseModel):
    condition: Literal["resellable", "damaged"]


class ReturnOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    return_id: str
    order_id: str
    sku: str
    qty: int
    unit_price_cents: int
    reason: str
    status: str
    decision_reason: str
    condition: str | None
    restock_status: str
    refund_cents: int | None