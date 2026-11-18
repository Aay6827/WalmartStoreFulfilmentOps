import json
import re
import types

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import create_app


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeService:
    """Base fake upstream. mode: ok / down / timeout / 5xx / 503."""

    def __init__(self, clock=None):
        self.clock = clock
        self.mode = "ok"
        self.advance = 0.0  # seconds the fake clock moves during each call
        self.calls = []
        self.timeouts = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, request.url.path))
        self.timeouts.append(request.extensions["timeout"]["read"])
        if self.clock is not None and self.advance:
            self.clock.advance(self.advance)
        if self.mode == "down":
            raise httpx.ConnectError("down", request=request)
        if self.mode == "timeout":
            raise httpx.ReadTimeout("slow", request=request)
        if self.mode == "5xx":
            return httpx.Response(500, json={"detail": "boom"})
        if self.mode == "503":
            return httpx.Response(
                503, json={"detail": "dependency unavailable; order stays pending, retry with the same order_id"}
            )
        return self.respond(request)

    def respond(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "not found"})


class FakeOrders(FakeService):
    def __init__(self, clock=None):
        super().__init__(clock)
        self.orders = {
            "O1": {"order_id": "O1", "sku": "SKU-1", "qty": 1, "slot_id": "S1",
                   "status": "confirmed", "decision_reason": "confirmed"},
            "O2": {"order_id": "O2", "sku": "SKU-1", "qty": 1, "slot_id": "S1",
                   "status": "rejected", "decision_reason": "slot is full"},
        }

    def respond(self, request):
        path, method = request.url.path, request.method
        if method == "POST" and path == "/orders":
            body = json.loads(request.content)
            if body.get("qty", 0) <= 0:
                return httpx.Response(422, json={"detail": [{"msg": "qty must be > 0"}]})
            order = {**body, "status": "confirmed", "decision_reason": "confirmed"}
            self.orders[body["order_id"]] = order
            return httpx.Response(201, json=order)
        m = re.fullmatch(r"/orders/([^/]+)", path)
        if method == "GET" and m:
            order = self.orders.get(m.group(1))
            if order is None:
                return httpx.Response(404, json={"detail": "order not found"})
            return httpx.Response(200, json=order)
        m = re.fullmatch(r"/orders/([^/]+)/(cancel|fulfil)", path)
        if method == "POST" and m:
            order = self.orders.get(m.group(1))
            if order is None:
                return httpx.Response(404, json={"detail": "order not found"})
            order["status"] = "cancelled" if m.group(2) == "cancel" else "fulfilled"
            return httpx.Response(200, json=order)
        return super().respond(request)


class FakeSlots(FakeService):
    SLOT = {"slot_id": "S1", "store_id": "ST-12", "kind": "pickup",
            "starts_at": "2026-12-01T10:00:00+00:00", "capacity": 2, "booked": 1, "available": 1}

    def __init__(self, clock=None):
        super().__init__(clock)
        self.last_query = {}

    def respond(self, request):
        path = request.url.path
        if request.method == "GET" and path == "/slots":
            self.last_query = dict(request.url.params)
            return httpx.Response(200, json=[self.SLOT])
        if request.method == "GET" and path == "/slots/S1":
            return httpx.Response(200, json=self.SLOT)
        if request.method == "GET" and path == "/bookings/O1":
            return httpx.Response(200, json={"order_id": "O1", "slot_id": "S1", "status": "active"})
        return super().respond(request)


class FakeInventory(FakeService):
    def respond(self, request):
        if request.method == "GET" and request.url.path == "/stock/SKU-1":
            return httpx.Response(200, json={"sku": "SKU-1", "on_hand": 5, "reserved": 1, "available": 4})
        return super().respond(request)


class FakeReturns(FakeService):
    def respond(self, request):
        path, method = request.url.path, request.method
        if method == "POST" and path == "/returns":
            return httpx.Response(201, json={**json.loads(request.content), "status": "approved"})
        m = re.fullmatch(r"/returns/([^/]+)", path)
        if method == "GET" and m:
            if m.group(1) != "R1":
                return httpx.Response(404, json={"detail": "return not found"})
            return httpx.Response(200, json={"return_id": "R1", "status": "approved"})
        m = re.fullmatch(r"/returns/([^/]+)/receive", path)
        if method == "POST" and m:
            body = json.loads(request.content)
            return httpx.Response(200, json={"return_id": m.group(1), "status": "received",
                                             "condition": body["condition"]})
        m = re.fullmatch(r"/returns/([^/]+)/refund", path)
        if method == "POST" and m:
            return httpx.Response(200, json={"return_id": m.group(1), "status": "refunded",
                                             "refund_cents": 3000})
        return super().respond(request)


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def svc(clock):
    return types.SimpleNamespace(
        order=FakeOrders(clock), slots=FakeSlots(clock),
        inventory=FakeInventory(clock), returns=FakeReturns(clock),
    )


@pytest.fixture
def client(svc, clock):
    def mk(fake, base):
        return httpx.Client(base_url=base, transport=httpx.MockTransport(fake.handle))

    clients = {
        "order-service": mk(svc.order, "http://order.test"),
        "slot-service": mk(svc.slots, "http://slots.test"),
        "inventory-service": mk(svc.inventory, "http://inventory.test"),
        "returns-service": mk(svc.returns, "http://returns.test"),
    }
    app = create_app(clients=clients, deadline_seconds=1.0, failure_threshold=3,
                     reset_timeout=10.0, clock=clock)
    with TestClient(app) as c:
        yield c