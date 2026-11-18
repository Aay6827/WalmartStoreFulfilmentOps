from datetime import datetime, timezone
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from shared.observability import instrument

from .db import Base, make_engine
from .models import Booking, Slot
from .schemas import BookingIn, BookingOut, SlotIn, SlotOut


def get_session(request: Request):
    with Session(request.app.state.engine) as session:
        yield session


SessionDep = Annotated[Session, Depends(get_session)]


def _aware(dt: datetime) -> datetime:
    # SQLite returns naive datetimes; everything we store is UTC.
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _slot_out(s: Slot) -> SlotOut:
    return SlotOut(
        slot_id=s.slot_id, store_id=s.store_id, kind=s.kind,
        starts_at=_aware(s.starts_at), capacity=s.capacity,
        booked=s.booked, available=s.capacity - s.booked,
    )


def _find_booking(session: Session, order_id: str) -> Booking | None:
    return session.scalar(select(Booking).where(Booking.order_id == order_id))


def _replay(existing: Booking, body: BookingIn, response: Response) -> Booking:
    if existing.slot_id != body.slot_id:
        raise HTTPException(409, "order already has a booking for a different slot")
    if existing.status != "active":
        raise HTTPException(409, "booking for this order was cancelled")
    response.status_code = 200
    return existing


def create_app(database_url: str | None = None) -> FastAPI:
    app = FastAPI(title="slot-service")
    engine = make_engine(database_url)
    Base.metadata.create_all(engine)
    app.state.engine = engine
    instrument(app, "slot-service")

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.post("/slots", response_model=SlotOut, status_code=201)
    def create_slot(body: SlotIn, session: SessionDep):
        if session.get(Slot, body.slot_id):
            raise HTTPException(409, "slot already exists")
        slot = Slot(**body.model_dump(), booked=0)
        session.add(slot)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            raise HTTPException(409, "slot already exists")
        session.refresh(slot)
        return _slot_out(slot)

    @app.get("/slots", response_model=list[SlotOut])
    def list_slots(
        session: SessionDep,
        store_id: str | None = None,
        kind: Literal["pickup", "delivery"] | None = None,
        available_only: bool = False,
    ):
        q = select(Slot).order_by(Slot.starts_at)
        if store_id:
            q = q.where(Slot.store_id == store_id)
        if kind:
            q = q.where(Slot.kind == kind)
        if available_only:
            q = q.where(Slot.booked < Slot.capacity)
        return [_slot_out(s) for s in session.scalars(q)]

    @app.get("/slots/{slot_id}", response_model=SlotOut)
    def get_slot(slot_id: str, session: SessionDep):
        slot = session.get(Slot, slot_id)
        if slot is None:
            raise HTTPException(404, "unknown slot")
        return _slot_out(slot)

    @app.post("/bookings", response_model=BookingOut, status_code=201)
    def book(body: BookingIn, response: Response, session: SessionDep):
        existing = _find_booking(session, body.order_id)
        if existing:
            return _replay(existing, body, response)

        slot = session.get(Slot, body.slot_id)
        if slot is None:
            raise HTTPException(404, "unknown slot")
        if _aware(slot.starts_at) <= datetime.now(timezone.utc):
            raise HTTPException(409, "slot has already started")

        # Atomic check-and-increment: two orders cannot take the last place.
        result = session.execute(
            update(Slot)
            .where(Slot.slot_id == body.slot_id, Slot.booked < Slot.capacity)
            .values(booked=Slot.booked + 1)
        )
        if result.rowcount == 0:
            session.rollback()
            raise HTTPException(409, "slot is full")

        booking = Booking(order_id=body.order_id, slot_id=body.slot_id)
        session.add(booking)
        try:
            session.commit()
        except IntegrityError:
            # Concurrent duplicate for the same order: the increment rolled back with us.
            session.rollback()
            existing = _find_booking(session, body.order_id)
            if existing:
                return _replay(existing, body, response)
            raise
        session.refresh(booking)
        return booking

    @app.get("/bookings/{order_id}", response_model=BookingOut)
    def get_booking(order_id: str, session: SessionDep):
        b = _find_booking(session, order_id)
        if b is None:
            raise HTTPException(404, "booking not found")
        return b

    @app.post("/bookings/{order_id}/cancel", response_model=BookingOut)
    def cancel(order_id: str, session: SessionDep):
        b = _find_booking(session, order_id)
        if b is None:
            raise HTTPException(404, "booking not found")
        if b.status == "cancelled":
            return b
        # Conditional flip: only one concurrent cancel gets to free the capacity.
        moved = session.execute(
            update(Booking)
            .where(Booking.id == b.id, Booking.status == "active")
            .values(status="cancelled")
        )
        if moved.rowcount == 0:
            session.rollback()
            session.refresh(b)
            return b
        session.execute(
            update(Slot).where(Slot.slot_id == b.slot_id).values(booked=Slot.booked - 1)
        )
        session.commit()
        session.refresh(b)
        return b

    return app