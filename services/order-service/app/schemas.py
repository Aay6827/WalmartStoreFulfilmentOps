from pydantic import BaseModel, ConfigDict, Field


class OrderIn(BaseModel):
    order_id: str = Field(min_length=1, max_length=64)
    sku: str = Field(min_length=1, max_length=64)
    qty: int = Field(gt=0)
    slot_id: str = Field(min_length=1, max_length=64)


class OrderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    order_id: str
    sku: str
    qty: int
    slot_id: str
    status: str
    decision_reason: str