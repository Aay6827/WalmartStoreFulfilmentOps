import json
import re

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import create_app


class FakeInventory:
    """In-memory stand-in for inventory-service. stock = available units."""

    def __init__(self):
        self.mode = "ok"  # ok / down / 5xx
        self.down_after_reserve = False
        self.stock = {"SKU-1": 5}
        self.reservations = {}
        self.calls = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(path)
        if self.mode == "down":
            raise httpx.ConnectError("inventory down", request=request)
        if self.mode == "5xx":
            return httpx.Response(503)
        if path == "/reservations":
            body = json.loads(request.content)
            key = (body["order_id"], body["sku"])
            if body["sku"] not in self.stock:
                return httpx.Response(404, json={"detail": "unknown sku"})
            if key in self.reservations:
                return httpx.Response(200, json={})
            if self.stock[body["sku"]] < body["qty"]:
                return httpx.Response(409, json={"detail": "insufficient stock"})
            self.stock[body["sku"]] -= body["qty"]
            self.reservations[key] = {"qty": body["qty"], "status": "active"}
            if self.down_after_reserve:
                self.mode = "down"
            return httpx.Response(201, json={})
        m = re.fullmatch(r"/reservations/([^/]+)/([^/]+)/(release|commit)", path)
        if m:
            order_id, sku, action = m.groups()
            r = self.reservations.get((order_id, sku))
            if r is None:
                return httpx.Response(404, json={"detail": "reservation not found"})
            if action == "release":
                if r["status"] == "committed":
                    return httpx.Response(409, json={"detail": "reservation already committed"})
                if r["status"] == "active":
                    r["status"] = "released"
                    self.stock[sku] += r["qty"]
                return httpx.Response(200, json={})
            if r["status"] == "released":
                return httpx.Response(409, json={"detail": "reservation already released"})
            r["status"] = "committed"
            return httpx.Response(200, json={})
        return httpx.Response(404)


class FakeSlots:
    """In-memory stand-in for slot-service. slots = remaining capacity."""

    def __init__(self):
        self.mode = "ok"
        self.slots = {"S1": 2}
        self.bookings = {}
        self.calls = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(path)
        if self.mode == "down":
            raise httpx.ConnectError("slots down", request=request)
        if self.mode == "5xx":
            return httpx.Response(503)
        if path == "/bookings":
            body = json.loads(request.content)
            oid, sid = body["order_id"], body["slot_id"]
            if sid not in self.slots:
                return httpx.Response(404, json={"detail": "unknown slot"})
            if oid in self.bookings:
                if self.bookings[oid]["status"] != "active":
                    return httpx.Response(409, json={"detail": "booking was cancelled"})
                return httpx.Response(200, json={})
            if self.slots[sid] <= 0:
                return httpx.Response(409, json={"detail": "slot is full"})
            self.slots[sid] -= 1
            self.bookings[oid] = {"slot_id": sid, "status": "active"}
            return httpx.Response(201, json={})
        m = re.fullmatch(r"/bookings/([^/]+)/cancel", path)
        if m:
            b = self.bookings.get(m.group(1))
            if b is None:
                return httpx.Response(404, json={"detail": "booking not found"})
            if b["status"] == "active":
                b["status"] = "cancelled"
                self.slots[b["slot_id"]] += 1
            return httpx.Response(200, json={})
        return httpx.Response(404)


@pytest.fixture
def inv():
    return FakeInventory()


@pytest.fixture
def slots():
    return FakeSlots()


@pytest.fixture
def client(tmp_path, inv, slots):
    app = create_app(
        f"sqlite:///{(tmp_path / 'orders.db').as_posix()}",
        inventory_client=httpx.Client(
            base_url="http://inventory.test", transport=httpx.MockTransport(inv.handle)
        ),
        slot_client=httpx.Client(
            base_url="http://slots.test", transport=httpx.MockTransport(slots.handle)
        ),
    )
    with TestClient(app) as c:
        yield c