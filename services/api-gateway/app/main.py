import math
import os
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Annotated
from urllib.parse import quote

import httpx
from fastapi import Body, FastAPI, Request
from fastapi.responses import JSONResponse, Response

from shared.observability import instrument

from .resilience import CircuitBreaker, Deadline, Upstream, UpstreamError

ORDERS, INVENTORY, SLOTS, RETURNS = "order-service", "inventory-service", "slot-service", "returns-service"


def _q(value: str) -> str:
    # Path parameters are encoded so an id can never rewrite the upstream path.
    return quote(value, safe="")


def _relay(resp: httpx.Response):
    try:
        return JSONResponse(resp.json(), status_code=resp.status_code)
    except ValueError:
        return Response(resp.content, status_code=resp.status_code,
                        media_type=resp.headers.get("content-type"))


def create_app(
    clients: dict[str, httpx.Client] | None = None,
    deadline_seconds: float | None = None,
    failure_threshold: int | None = None,
    reset_timeout: float | None = None,
    clock=None,
) -> FastAPI:
    app = FastAPI(title="api-gateway")
    clock = clock or time.monotonic
    deadline_s = deadline_seconds if deadline_seconds is not None else float(
        os.getenv("GATEWAY_DEADLINE_SECONDS", "2.0"))
    threshold = failure_threshold if failure_threshold is not None else int(
        os.getenv("GATEWAY_FAILURE_THRESHOLD", "5"))
    reset = reset_timeout if reset_timeout is not None else float(
        os.getenv("GATEWAY_RESET_SECONDS", "10"))

    urls = {
        ORDERS: os.getenv("ORDER_URL", "http://localhost:32000"),
        INVENTORY: os.getenv("INVENTORY_URL", "http://localhost:32001"),
        SLOTS: os.getenv("SLOT_URL", "http://localhost:32002"),
        RETURNS: os.getenv("RETURNS_URL", "http://localhost:32003"),
    }
    clients = clients or {}
    upstreams = {
        name: Upstream(
            name,
            clients[name] if name in clients else httpx.Client(base_url=url),
            CircuitBreaker(name, threshold, reset, clock),
        )
        for name, url in urls.items()
    }
    app.state.upstreams = upstreams
    instrument(app, "api-gateway")

    @app.exception_handler(UpstreamError)
    async def _upstream_error(request: Request, exc: UpstreamError):
        headers = None
        if exc.retry_after is not None:
            headers = {"Retry-After": str(max(1, math.ceil(exc.retry_after)))}
        return JSONResponse({"detail": exc.detail}, status_code=exc.status, headers=headers)

    def new_deadline() -> Deadline:
        return Deadline(deadline_s, clock)

    def forward(name: str, method: str, path: str, json=None, params=None):
        return _relay(upstreams[name].call(method, path, new_deadline(), json=json, params=params))

    @app.get("/health")
    def health():
        return {"status": "ok", "circuits": {n: u.breaker.state for n, u in upstreams.items()}}

    # ---- orders ----
    @app.post("/orders")
    def create_order(payload: Annotated[dict, Body()]):
        return forward(ORDERS, "POST", "/orders", json=payload)

    @app.get("/orders/{order_id}")
    def get_order(order_id: str):
        return forward(ORDERS, "GET", f"/orders/{_q(order_id)}")

    @app.post("/orders/{order_id}/cancel")
    def cancel_order(order_id: str):
        return forward(ORDERS, "POST", f"/orders/{_q(order_id)}/cancel")

    @app.post("/orders/{order_id}/fulfil")
    def fulfil_order(order_id: str):
        return forward(ORDERS, "POST", f"/orders/{_q(order_id)}/fulfil")

    @app.get("/orders/{order_id}/summary")
    def order_summary(order_id: str):
        deadline = new_deadline()
        order_resp = upstreams[ORDERS].call("GET", f"/orders/{_q(order_id)}", deadline)
        if order_resp.status_code != 200:
            return _relay(order_resp)
        order = order_resp.json()

        parts = {
            "booking": (SLOTS, f"/bookings/{_q(order_id)}"),
            "slot": (SLOTS, f"/slots/{_q(str(order.get('slot_id', '')))}"),
            "stock": (INVENTORY, f"/stock/{_q(str(order.get('sku', '')))}"),
        }

        def fetch(item):
            name, (service, path) = item
            try:
                resp = upstreams[service].call("GET", path, deadline)
            except UpstreamError:
                return name, None, True  # degraded: the order is still worth returning
            if resp.status_code == 200:
                return name, resp.json(), False
            if resp.status_code == 404:
                return name, None, False  # e.g. a rejected order has no booking
            return name, None, True

        with ThreadPoolExecutor(max_workers=len(parts)) as pool:
            results = list(pool.map(fetch, parts.items()))
        body = {"order": order}
        body.update({name: data for name, data, _ in results})
        body["degraded"] = [name for name, _, bad in results if bad]
        return body

    # ---- returns ----
    @app.post("/returns")
    def create_return(payload: Annotated[dict, Body()]):
        return forward(RETURNS, "POST", "/returns", json=payload)

    @app.get("/returns/{return_id}")
    def get_return(return_id: str):
        return forward(RETURNS, "GET", f"/returns/{_q(return_id)}")

    @app.post("/returns/{return_id}/receive")
    def receive_return(return_id: str, payload: Annotated[dict, Body()]):
        return forward(RETURNS, "POST", f"/returns/{_q(return_id)}/receive", json=payload)

    @app.post("/returns/{return_id}/refund")
    def refund_return(return_id: str):
        return forward(RETURNS, "POST", f"/returns/{_q(return_id)}/refund")

    # ---- slots and stock (read-only views) ----
    @app.get("/slots")
    def list_slots(request: Request):
        return forward(SLOTS, "GET", "/slots", params=request.query_params.multi_items())

    @app.get("/stock/{sku}")
    def get_stock(sku: str):
        return forward(INVENTORY, "GET", f"/stock/{_q(sku)}")

    return app