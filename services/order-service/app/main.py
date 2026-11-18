import os
from typing import Annotated

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from prometheus_client import Counter, Gauge
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from shared.observability import instrument

from .db import Base, make_engine
from .models import Order
from .schemas import OrderIn, OrderOut

COMPENSATING = Gauge(
    "orders_compensating", "Orders whose downstream undo has not completed yet"
)
OUTCOMES = Counter("orders_outcome_total", "Order outcomes", ["outcome"])


class Transient(Exception):
    """A dependency failed in a way that is safe to retry."""


def get_session(request: Request):
    with Session(request.app.state.engine) as session:
        yield session


SessionDep = Annotated[Session, Depends(get_session)]


def _post(client: httpx.Client, path: str, payload: dict | None = None) -> httpx.Response:
    try:
        resp = client.post(path, json=payload)
    except httpx.HTTPError as exc:
        raise Transient(str(exc)) from exc
    if resp.status_code >= 500 or resp.status_code == 429:
        raise Transient(f"{path} -> {resp.status_code}")
    return resp


def _detail(resp: httpx.Response) -> str:
    try:
        return str(resp.json().get("detail", ""))
    except Exception:
        return ""


def _set_status(session: Session, order_id: str, from_statuses: tuple, **values) -> bool:
    """Conditional transition: returns True only for the caller that actually moved the order."""
    result = session.execute(
        update(Order)
        .where(Order.order_id == order_id, Order.status.in_(from_statuses))
        .values(**values)
    )
    session.commit()
    return result.rowcount == 1


def _refresh_gauge(session: Session) -> None:
    n = session.scalar(select(func.count()).select_from(Order).where(Order.status == "compensating"))
    COMPENSATING.set(n or 0)


def _compensate(app: FastAPI, session: Session, order_id: str, sku: str, to_status: str) -> bool:
    """Undo downstream effects. Both calls are idempotent and 404 means nothing to undo."""
    try:
        released = _post(app.state.inventory, f"/reservations/{order_id}/{sku}/release")
        unbooked = _post(app.state.slots, f"/bookings/{order_id}/cancel")
    except Transient:
        return False
    if released.status_code not in (200, 404) or unbooked.status_code not in (200, 404):
        return False
    if _set_status(session, order_id, ("compensating",), status=to_status):
        OUTCOMES.labels(to_status).inc()
    return True


def _reject(app: FastAPI, session: Session, order_id: str, sku: str, reason: str, undo: bool) -> None:
    if not undo:  # nothing was reserved yet
        if _set_status(session, order_id, ("pending",), status="rejected", decision_reason=reason):
            OUTCOMES.labels("rejected").inc()
        return
    if _set_status(
        session, order_id, ("pending",),
        status="compensating", compensate_to="rejected", decision_reason=reason,
    ):
        _compensate(app, session, order_id, sku, "rejected")
    _refresh_gauge(session)


def _run_saga(app: FastAPI, session: Session, order: Order) -> None:
    oid, sku, qty, slot_id = order.order_id, order.sku, order.qty, order.slot_id
    try:
        reserved = _post(app.state.inventory, "/reservations", {"order_id": oid, "sku": sku, "qty": qty})
        if reserved.status_code not in (200, 201):
            reason = {404: "unknown sku", 409: "insufficient stock"}.get(
                reserved.status_code, f"inventory rejected request: {_detail(reserved)}"
            )
            _reject(app, session, oid, sku, reason, undo=False)
            return
        booked = _post(app.state.slots, "/bookings", {"order_id": oid, "slot_id": slot_id})
    except Transient:
        OUTCOMES.labels("transient_failure").inc()
        raise HTTPException(503, "dependency unavailable; order stays pending, retry with the same order_id")

    if booked.status_code in (200, 201):
        if _set_status(session, oid, ("pending",), status="confirmed", decision_reason="confirmed"):
            OUTCOMES.labels("confirmed").inc()
        return
    reason = "unknown slot" if booked.status_code == 404 else f"slot unavailable: {_detail(booked)}"
    _reject(app, session, oid, sku, reason, undo=True)


def _same(order: Order, body: OrderIn) -> bool:
    return (order.sku, order.qty, order.slot_id) == (body.sku, body.qty, body.slot_id)


def _get_or_404(session: Session, order_id: str) -> Order:
    order = session.get(Order, order_id)
    if order is None:
        raise HTTPException(404, "order not found")
    return order


def create_app(
    database_url: str | None = None,
    inventory_client: httpx.Client | None = None,
    slot_client: httpx.Client | None = None,
) -> FastAPI:
    app = FastAPI(title="order-service")
    engine = make_engine(database_url)
    Base.metadata.create_all(engine)
    app.state.engine = engine
    app.state.inventory = inventory_client or httpx.Client(
        base_url=os.getenv("INVENTORY_URL", "http://localhost:32001"), timeout=httpx.Timeout(2.0)
    )
    app.state.slots = slot_client or httpx.Client(
        base_url=os.getenv("SLOT_URL", "http://localhost:32002"), timeout=httpx.Timeout(2.0)
    )
    instrument(app, "order-service")

    with Session(engine) as s:
        _refresh_gauge(s)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.post("/orders", response_model=OrderOut, status_code=201)
    def create_order(body: OrderIn, response: Response, session: SessionDep):
        order = session.get(Order, body.order_id)
        if order is None:
            order = Order(
                order_id=body.order_id, sku=body.sku, qty=body.qty,
                slot_id=body.slot_id, status="pending", decision_reason="",
            )
            session.add(order)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                order = session.get(Order, body.order_id)
                if order is None:
                    raise
                response.status_code = 200
        else:
            response.status_code = 200
        if not _same(order, body):
            raise HTTPException(409, "idempotency conflict: order exists with different details")

        if order.status == "pending":
            _run_saga(app, session, order)
        elif order.status == "compensating":
            _compensate(app, session, order.order_id, order.sku, order.compensate_to or "cancelled")
            _refresh_gauge(session)
        session.refresh(order)
        if order.status == "compensating":
            response.status_code = 202
        return order

    @app.get("/orders/{order_id}", response_model=OrderOut)
    def get_order(order_id: str, session: SessionDep):
        return _get_or_404(session, order_id)

    @app.post("/orders/{order_id}/cancel", response_model=OrderOut)
    def cancel(order_id: str, response: Response, session: SessionDep):
        order = _get_or_404(session, order_id)
        if order.status in ("cancelled", "rejected"):
            return order
        if order.status in ("fulfilling", "fulfilled"):
            raise HTTPException(409, f"cannot cancel a {order.status} order")
        if order.status in ("pending", "confirmed"):
            _set_status(
                session, order_id, ("pending", "confirmed"),
                status="compensating", compensate_to="cancelled",
            )
            session.refresh(order)
            if order.status not in ("compensating", "cancelled"):
                raise HTTPException(409, f"order moved to {order.status} concurrently")
        if order.status == "compensating":
            _compensate(app, session, order_id, order.sku, order.compensate_to or "cancelled")
            _refresh_gauge(session)
            session.refresh(order)
            if order.status == "compensating":
                response.status_code = 202  # accepted, undo still in progress
        return order

    @app.post("/orders/compensation-retry")
    def compensation_retry(session: SessionDep):
        rows = [
            (o.order_id, o.sku, o.compensate_to or "cancelled")
            for o in session.scalars(select(Order).where(Order.status == "compensating"))
        ]
        resolved = sum(1 for oid, sku, to in rows if _compensate(app, session, oid, sku, to))
        _refresh_gauge(session)
        return {"attempted": len(rows), "resolved": resolved}

    @app.post("/orders/{order_id}/fulfil", response_model=OrderOut)
    def fulfil(order_id: str, session: SessionDep):
        order = _get_or_404(session, order_id)
        if order.status == "fulfilled":
            return order
        if order.status == "confirmed":
            # Claim the order so a concurrent cancel is refused; losing this race is harmless
            # because the inventory commit below is idempotent.
            _set_status(session, order_id, ("confirmed",), status="fulfilling")
            session.refresh(order)
        if order.status != "fulfilling":
            raise HTTPException(409, f"cannot fulfil a {order.status} order")
        try:
            resp = _post(app.state.inventory, f"/reservations/{order_id}/{order.sku}/commit")
        except Transient:
            raise HTTPException(503, "inventory unavailable; order stays fulfilling, retry")
        if resp.status_code != 200:
            raise HTTPException(409, f"inventory refused commit: {_detail(resp)}")
        if _set_status(session, order_id, ("fulfilling",), status="fulfilled"):
            OUTCOMES.labels("fulfilled").inc()
        session.refresh(order)
        return order

    return app