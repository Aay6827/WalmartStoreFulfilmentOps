import os
from datetime import datetime, timedelta, timezone
from typing import Annotated

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from prometheus_client import Gauge
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from shared.observability import instrument

from .db import Base, make_engine
from .models import Return
from .schemas import ReceiveIn, ReturnIn, ReturnOut

RESTOCK_PENDING = Gauge(
    "returns_restock_pending",
    "Received returns whose restock has not reached inventory-service yet",
)


def get_session(request: Request):
    with Session(request.app.state.engine) as session:
        yield session


SessionDep = Annotated[Session, Depends(get_session)]


def _refresh_gauge(session: Session) -> None:
    n = session.scalar(
        select(func.count()).select_from(Return).where(Return.restock_status == "pending")
    )
    RESTOCK_PENDING.set(n or 0)


def _try_restock(client: httpx.Client, r: Return) -> str:
    """Returns the new restock_status: done / pending (retryable) / failed (permanent)."""
    try:
        resp = client.post(
            "/restocks", json={"return_id": r.return_id, "sku": r.sku, "qty": r.qty}
        )
    except httpx.HTTPError:
        return "pending"
    if resp.status_code in (200, 201):
        return "done"
    if resp.status_code >= 500 or resp.status_code == 429:
        return "pending"
    return "failed"


def _get_or_404(session: Session, return_id: str) -> Return:
    r = session.get(Return, return_id)
    if r is None:
        raise HTTPException(404, "return not found")
    return r


def _replay(existing: Return, body: ReturnIn, response: Response) -> Return:
    if (existing.order_id, existing.sku, existing.qty) != (body.order_id, body.sku, body.qty):
        raise HTTPException(409, "idempotency conflict: return exists with different details")
    response.status_code = 200
    return existing


def create_app(
    database_url: str | None = None,
    inventory_client: httpx.Client | None = None,
    window_days: int | None = None,
) -> FastAPI:
    app = FastAPI(title="returns-service")
    engine = make_engine(database_url)
    Base.metadata.create_all(engine)
    app.state.engine = engine
    app.state.inventory = inventory_client or httpx.Client(
        base_url=os.getenv("INVENTORY_URL", "http://localhost:32001"),
        timeout=httpx.Timeout(2.0),
    )
    window = window_days or int(os.getenv("RETURN_WINDOW_DAYS", "90"))
    instrument(app, "returns-service")

    with Session(engine) as s:
        _refresh_gauge(s)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.post("/returns", response_model=ReturnOut, status_code=201)
    def create_return(body: ReturnIn, response: Response, session: SessionDep):
        existing = session.get(Return, body.return_id)
        if existing:
            return _replay(existing, body, response)

        now = datetime.now(timezone.utc)
        if body.purchased_at > now:
            raise HTTPException(422, "purchased_at is in the future")
        if now - body.purchased_at > timedelta(days=window):
            status, decision = "rejected", f"outside {window}-day return window"
        else:
            status, decision = "approved", "eligible"

        r = Return(**body.model_dump(), status=status, decision_reason=decision)
        session.add(r)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            existing = session.get(Return, body.return_id)
            if existing:
                return _replay(existing, body, response)
            raise
        session.refresh(r)
        return r

    @app.get("/returns/{return_id}", response_model=ReturnOut)
    def get_return(return_id: str, session: SessionDep):
        return _get_or_404(session, return_id)

    @app.post("/returns/{return_id}/receive", response_model=ReturnOut)
    def receive(return_id: str, body: ReceiveIn, session: SessionDep):
        r = _get_or_404(session, return_id)
        if r.status in ("received", "refunded"):
            if r.condition == body.condition:
                return r
            raise HTTPException(409, f"return was already received as {r.condition}")
        if r.status != "approved":
            raise HTTPException(409, f"cannot receive a {r.status} return")

        resellable = body.condition == "resellable"
        # Conditional flip: only one concurrent receive wins, so only one restock attempt.
        moved = session.execute(
            update(Return)
            .where(Return.return_id == return_id, Return.status == "approved")
            .values(
                status="received",
                condition=body.condition,
                restock_status="pending" if resellable else "skipped",
            )
        )
        if moved.rowcount == 0:
            # concurrent duplicate receive won the flip: replay its result
            session.rollback()
            session.refresh(r)
            if r.status in ("received", "refunded") and r.condition == body.condition:
                return r
            raise HTTPException(409, "return is no longer approved")
        session.commit()
        session.refresh(r)

        if resellable:
            # Never block the receive on inventory-service: failures stay "pending".
            r.restock_status = _try_restock(app.state.inventory, r)
            session.commit()
        _refresh_gauge(session)
        session.refresh(r)
        return r

    @app.post("/returns/restock-retry")
    def restock_retry(session: SessionDep):
        pending = list(session.scalars(select(Return).where(Return.restock_status == "pending")))
        resolved = 0
        for r in pending:
            outcome = _try_restock(app.state.inventory, r)
            if outcome != "pending":
                r.restock_status = outcome
                resolved += 1
        session.commit()
        _refresh_gauge(session)
        return {"attempted": len(pending), "resolved": resolved}

    @app.post("/returns/{return_id}/refund", response_model=ReturnOut)
    def refund(return_id: str, session: SessionDep):
        r = _get_or_404(session, return_id)
        if r.status == "refunded":
            return r
        if r.status != "received":
            raise HTTPException(409, f"cannot refund a {r.status} return")
        moved = session.execute(
            update(Return)
            .where(Return.return_id == return_id, Return.status == "received")
            .values(status="refunded", refund_cents=r.qty * r.unit_price_cents)
        )
        if moved.rowcount == 0:
            # concurrent duplicate refund won the flip: replay its result
            session.rollback()
            session.refresh(r)
            if r.status == "refunded":
                return r
            raise HTTPException(409, "return is no longer in received state")
        session.commit()
        session.refresh(r)
        return r

    return app