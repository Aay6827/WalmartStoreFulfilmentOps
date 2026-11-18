from pydantic import BaseModel, ConfigDict, Field


class StockIn(BaseModel):
    on_hand: int = Field(ge=0)


class StockOut(BaseModel):
    sku: str
    on_hand: int
    reserved: int
    available: int


class ReservationIn(BaseModel):
    order_id: str = Field(min_length=1, max_length=64)
    sku: str = Field(min_length=1, max_length=64)
    qty: int = Field(gt=0)


class ReservationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    order_id: str
    sku: str
    qty: int
    status: str