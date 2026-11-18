from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import DateTime, Integer, String, UniqueConstraint, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base
from .models import Stock


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Restock(Base):
    __tablename__ = "restocks"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    return_id: Mapped[str] = mapped_column(String(64), nullable=False)
    sku: Mapped[str] = mapped_column(String(64), nullable=False)
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    __table_args__ = (UniqueConstraint("return_id", "sku", name="uq_return_sku"),)


class RestockIn(BaseModel):
    return_id: str = Field(min_length=1, max_length=64)
    sku: str = Field(min_length=1, max_length=64)
    qty: int = Field(gt=0)


class RestockOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    return_id: str
    sku: str
    qty: int


def register(app: FastAPI, SessionDep) -> None:
    def _find(session, return_id: str, sku: str):
        return session.scalar(
            select(Restock).where(Restock.return_id == return_id, Restock.sku == sku)
        )

    def _replay(existing: Restock, body: RestockIn, response: Response) -> Restock:
        if existing.qty != body.qty:
            raise HTTPException(409, "idempotency conflict: restock exists with a different qty")
        response.status_code = 200
        return existing

    @app.post("/restocks", response_model=RestockOut, status_code=201)
    def restock(body: RestockIn, response: Response, session: SessionDep):
        existing = _find(session, body.return_id, body.sku)
        if existing:
            return _replay(existing, body, response)
        if session.get(Stock, body.sku) is None:
            raise HTTPException(404, "unknown sku")

        # The unique (return_id, sku) row is inserted first: if a concurrent duplicate
        # wins the race, our flush fails before we ever touch on_hand, so stock is
        # never incremented twice for the same return.
        row = Restock(return_id=body.return_id, sku=body.sku, qty=body.qty)
        session.add(row)
        try:
            session.flush()
        except IntegrityError:
            session.rollback()
            existing = _find(session, body.return_id, body.sku)
            if existing:
                return _replay(existing, body, response)
            raise
        session.execute(
            update(Stock).where(Stock.sku == body.sku).values(on_hand=Stock.on_hand + body.qty)
        )
        session.commit()
        session.refresh(row)
        return row