from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from shared.observability import instrument

from .db import Base, make_engine
from .models import Reservation, Stock
from .restocks import register as register_restocks
from .schemas import ReservationIn, ReservationOut, StockIn, StockOut


def get_session(request: Request):
    with Session(request.app.state.engine) as session:
        yield session


SessionDep = Annotated[Session, Depends(get_session)]


def _stock_out(s: Stock) -> StockOut:
    return StockOut(sku=s.sku, on_hand=s.on_hand, reserved=s.reserved, available=s.on_hand - s.reserved)


def _find(session: Session, order_id: str, sku: str) -> Reservation | None:
    return session.scalar(
        select(Reservation).where(Reservation.order_id == order_id, Reservation.sku == sku)
    )


def _replay(existing: Reservation, body: ReservationIn, response: Response) -> Reservation:
    if existing.qty != body.qty:
        raise HTTPException(409, "idempotency conflict: reservation exists with a different qty")
    response.status_code = 200
    return existing


def _transition(session: Session, r: Reservation, new_status: str, stock_values: dict) -> Reservation:
    # Conditional status flip makes concurrent release/commit calls safe: only one wins.
    moved = session.execute(
        update(Reservation)
        .where(Reservation.id == r.id, Reservation.status == "active")
        .values(status=new_status)
    )
    if moved.rowcount == 0:
        session.rollback()
        raise HTTPException(409, "reservation is no longer active")
    session.execute(update(Stock).where(Stock.sku == r.sku).values(**stock_values))
    session.commit()
    session.refresh(r)
    return r


def create_app(database_url: str | None = None) -> FastAPI:
    app = FastAPI(title="inventory-service")
    engine = make_engine(database_url)
    Base.metadata.create_all(engine)
    app.state.engine = engine
    instrument(app, "inventory-service")

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.put("/stock/{sku}", response_model=StockOut)
    def put_stock(sku: str, body: StockIn, session: SessionDep):
        stock = session.get(Stock, sku)
        if stock is None:
            stock = Stock(sku=sku, on_hand=body.on_hand, reserved=0)
            session.add(stock)
        else:
            stock.on_hand = body.on_hand
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            raise HTTPException(409, "on_hand cannot drop below reserved quantity")
        session.refresh(stock)
        return _stock_out(stock)

    @app.get("/stock/{sku}", response_model=StockOut)
    def get_stock(sku: str, session: SessionDep):
        stock = session.get(Stock, sku)
        if stock is None:
            raise HTTPException(404, "unknown sku")
        return _stock_out(stock)

    @app.post("/reservations", response_model=ReservationOut, status_code=201)
    def reserve(body: ReservationIn, response: Response, session: SessionDep):
        existing = _find(session, body.order_id, body.sku)
        if existing:
            return _replay(existing, body, response)

        # Atomic check-and-increment: no read-modify-write race, works on SQLite and Postgres.
        result = session.execute(
            update(Stock)
            .where(Stock.sku == body.sku, Stock.on_hand - Stock.reserved >= body.qty)
            .values(reserved=Stock.reserved + body.qty)
        )
        if result.rowcount == 0:
            session.rollback()
            if session.get(Stock, body.sku) is None:
                raise HTTPException(404, "unknown sku")
            raise HTTPException(409, "insufficient stock")

        reservation = Reservation(order_id=body.order_id, sku=body.sku, qty=body.qty)
        session.add(reservation)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            existing = _find(session, body.order_id, body.sku)
            if existing:
                return _replay(existing, body, response)
            raise
        session.refresh(reservation)
        return reservation

    @app.post("/reservations/{order_id}/{sku}/release", response_model=ReservationOut)
    def release(order_id: str, sku: str, session: SessionDep):
        r = _find(session, order_id, sku)
        if r is None:
            raise HTTPException(404, "reservation not found")
        if r.status == "released":
            return r
        if r.status == "committed":
            raise HTTPException(409, "reservation already committed")
        return _transition(session, r, "released", {"reserved": Stock.reserved - r.qty})

    @app.post("/reservations/{order_id}/{sku}/commit", response_model=ReservationOut)
    def commit(order_id: str, sku: str, session: SessionDep):
        r = _find(session, order_id, sku)
        if r is None:
            raise HTTPException(404, "reservation not found")
        if r.status == "committed":
            return r
        if r.status == "released":
            raise HTTPException(409, "reservation already released")
        return _transition(
            session, r, "committed",
            {"on_hand": Stock.on_hand - r.qty, "reserved": Stock.reserved - r.qty},
        )

    register_restocks(app, SessionDep)
    return app