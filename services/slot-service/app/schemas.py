from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class SlotIn(BaseModel):
    slot_id: str = Field(min_length=1, max_length=64)
    store_id: str = Field(min_length=1, max_length=32)
    kind: Literal["pickup", "delivery"]
    starts_at: datetime
    capacity: int = Field(gt=0)

    @field_validator("starts_at")
    @classmethod
    def _to_utc(cls, v: datetime) -> datetime:
        # Treat naive timestamps as UTC; normalise everything to UTC.
        if v.tzinfo is None:
            return v.replace(tzinfo=timezone.utc)
        return v.astimezone(timezone.utc)


class SlotOut(BaseModel):
    slot_id: str
    store_id: str
    kind: str
    starts_at: datetime
    capacity: int
    booked: int
    available: int


class BookingIn(BaseModel):
    order_id: str = Field(min_length=1, max_length=64)
    slot_id: str = Field(min_length=1, max_length=64)


class BookingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    order_id: str
    slot_id: str
    status: str