from datetime import datetime, timedelta, timezone


def ret(client, return_id="R1", days_ago=5, qty=2, price=1500, sku="SKU-1", order_id="O1"):
    purchased = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return client.post(
        "/returns",
        json={"return_id": return_id, "order_id": order_id, "sku": sku, "qty": qty,
              "unit_price_cents": price, "reason": "changed mind",
              "purchased_at": purchased.isoformat()},
    )


def receive(client, return_id="R1", condition="resellable"):
    return client.post(f"/returns/{return_id}/receive", json={"condition": condition})


def test_eligible_return_is_approved(client):
    r = ret(client)
    assert r.status_code == 201
    assert r.json()["status"] == "approved"
    assert r.json()["decision_reason"] == "eligible"


def test_return_outside_window_is_rejected(client):
    r = ret(client, days_ago=120)
    assert r.status_code == 201
    assert r.json()["status"] == "rejected"
    assert "90-day" in r.json()["decision_reason"]


def test_future_purchase_date_422(client):
    assert ret(client, days_ago=-3).status_code == 422


def test_create_is_idempotent_and_detects_conflict(client):
    assert ret(client).status_code == 201
    assert ret(client).status_code == 200
    assert ret(client, qty=5).status_code == 409


def test_receive_resellable_restocks_inventory(client, inventory):
    ret(client, qty=2)
    r = receive(client)
    assert r.status_code == 200
    assert r.json()["status"] == "received"
    assert r.json()["restock_status"] == "done"
    assert inventory["calls"] == [{"return_id": "R1", "sku": "SKU-1", "qty": 2}]


def test_receive_damaged_skips_restock(client, inventory):
    ret(client)
    r = receive(client, condition="damaged")
    assert r.json()["restock_status"] == "skipped"
    assert inventory["calls"] == []


def test_cannot_receive_rejected_return(client):
    ret(client, days_ago=120)
    assert receive(client).status_code == 409


def test_receive_is_idempotent(client, inventory):
    ret(client)
    receive(client)
    assert receive(client).status_code == 200
    assert len(inventory["calls"]) == 1
    assert receive(client, condition="damaged").status_code == 409


def test_inventory_down_leaves_restock_pending_then_retry_drains(client, inventory):
    ret(client)
    inventory["mode"] = "down"
    r = receive(client)
    assert r.status_code == 200
    assert r.json()["status"] == "received"
    assert r.json()["restock_status"] == "pending"
    assert "returns_restock_pending 1.0" in client.get("/metrics").text

    inventory["mode"] = "ok"
    out = client.post("/returns/restock-retry").json()
    assert out == {"attempted": 1, "resolved": 1}
    assert client.get("/returns/R1").json()["restock_status"] == "done"
    assert "returns_restock_pending 0.0" in client.get("/metrics").text


def test_inventory_5xx_leaves_restock_pending(client, inventory):
    ret(client)
    inventory["mode"] = "5xx"
    assert receive(client).json()["restock_status"] == "pending"


def test_unknown_sku_is_permanent_failure_and_not_retried(client, inventory):
    ret(client)
    inventory["mode"] = "unknown"
    assert receive(client).json()["restock_status"] == "failed"
    calls_before = len(inventory["calls"])
    assert client.post("/returns/restock-retry").json() == {"attempted": 0, "resolved": 0}
    assert len(inventory["calls"]) == calls_before


def test_refund_after_receive_and_idempotent(client):
    ret(client, qty=3, price=1500)
    receive(client)
    r = client.post("/returns/R1/refund")
    assert r.status_code == 200
    assert r.json()["status"] == "refunded"
    assert r.json()["refund_cents"] == 4500
    assert client.post("/returns/R1/refund").status_code == 200


def test_refund_before_receive_409(client):
    ret(client)
    assert client.post("/returns/R1/refund").status_code == 409


def test_unknown_return_404(client):
    assert client.get("/returns/NOPE").status_code == 404
    assert receive(client, "NOPE").status_code == 404
    assert client.post("/returns/NOPE/refund").status_code == 404


def test_metrics_endpoint(client):
    client.get("/health")
    assert 'service="returns-service"' in client.get("/metrics").text