import httpx
from fastapi.testclient import TestClient

from app.main import create_app


def test_no_db_connection_is_held_across_remote_calls(tmp_path):
    holder = {}
    checked_out = []

    def handler(request: httpx.Request) -> httpx.Response:
        checked_out.append(holder["app"].state.engine.pool.checkedout())
        created = request.method == "POST" and request.url.path in ("/reservations", "/bookings")
        return httpx.Response(201 if created else 200, json={})

    def client_for(base: str) -> httpx.Client:
        return httpx.Client(base_url=base, transport=httpx.MockTransport(handler))

    app = create_app(
        f"sqlite:///{(tmp_path / 'o.db').as_posix()}",
        inventory_client=client_for("http://inventory.test"),
        slot_client=client_for("http://slots.test"),
    )
    holder["app"] = app
    with TestClient(app) as c:
        body = {"order_id": "O1", "sku": "SKU-1", "qty": 1, "slot_id": "S1"}
        assert c.post("/orders", json=body).json()["status"] == "confirmed"
        assert c.post("/orders/O1/fulfil").json()["status"] == "fulfilled"
        assert c.post("/orders", json={**body, "order_id": "O2"}).json()["status"] == "confirmed"
        assert c.post("/orders/O2/cancel").json()["status"] == "cancelled"

    assert len(checked_out) == 7  # saga 2 + commit 1 + saga 2 + release and unbook 2
    assert set(checked_out) == {0}, checked_out