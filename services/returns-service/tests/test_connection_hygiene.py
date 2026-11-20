from datetime import datetime, timedelta, timezone

import httpx
from fastapi.testclient import TestClient

from app.main import create_app


def test_no_db_connection_is_held_during_the_restock_call(tmp_path):
    holder = {}
    state = {"fail": True}
    checked_out = []

    def handler(request: httpx.Request) -> httpx.Response:
        checked_out.append(holder["app"].state.engine.pool.checkedout())
        if state["fail"]:
            raise httpx.ConnectError("inventory down", request=request)
        return httpx.Response(201, json={})

    inventory = httpx.Client(base_url="http://inventory.test", transport=httpx.MockTransport(handler))
    app = create_app(f"sqlite:///{(tmp_path / 'r.db').as_posix()}", inventory_client=inventory)
    holder["app"] = app
    purchased = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
    with TestClient(app) as c:
        for rid in ("R1", "R2"):
            r = c.post("/returns", json={"return_id": rid, "order_id": "O1", "sku": "SKU-1", "qty": 1,
                                         "unit_price_cents": 1000, "reason": "x", "purchased_at": purchased})
            assert r.status_code == 201
            r = c.post(f"/returns/{rid}/receive", json={"condition": "resellable"})
            assert r.json()["restock_status"] == "pending"  # inventory is down: parked, not failed

        state["fail"] = False
        assert c.post("/returns/restock-retry").json() == {"attempted": 2, "resolved": 2}

    assert len(checked_out) == 4  # two failed attempts, then the two retries
    assert set(checked_out) == {0}, checked_out