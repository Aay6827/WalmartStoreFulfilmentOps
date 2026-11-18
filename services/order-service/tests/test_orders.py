def order(client, order_id="O1", sku="SKU-1", qty=1, slot_id="S1"):
    return client.post(
        "/orders", json={"order_id": order_id, "sku": sku, "qty": qty, "slot_id": slot_id}
    )


def test_happy_path_confirms_order(client, inv, slots):
    r = order(client)
    assert r.status_code == 201
    assert r.json()["status"] == "confirmed"
    assert inv.stock["SKU-1"] == 4
    assert slots.slots["S1"] == 1


def test_create_is_idempotent(client, inv, slots):
    assert order(client).status_code == 201
    again = order(client)
    assert again.status_code == 200
    assert again.json()["status"] == "confirmed"
    assert inv.stock["SKU-1"] == 4
    assert slots.slots["S1"] == 1


def test_idempotency_conflict_409(client):
    order(client, qty=1)
    assert order(client, qty=2).status_code == 409


def test_insufficient_stock_rejects_without_touching_slots(client, inv, slots):
    r = order(client, qty=9)
    assert r.status_code == 201
    assert r.json()["status"] == "rejected"
    assert r.json()["decision_reason"] == "insufficient stock"
    assert slots.calls == []
    assert [p for p in inv.calls if "release" in p] == []


def test_unknown_sku_rejected(client):
    r = order(client, sku="NOPE")
    assert r.json()["status"] == "rejected"
    assert r.json()["decision_reason"] == "unknown sku"


def test_slot_full_rejects_and_releases_inventory(client, inv, slots):
    slots.slots["S1"] = 0
    r = order(client)
    assert r.status_code == 201
    assert r.json()["status"] == "rejected"
    assert "slot is full" in r.json()["decision_reason"]
    assert inv.stock["SKU-1"] == 5
    assert inv.reservations[("O1", "SKU-1")]["status"] == "released"


def test_unknown_slot_rejects_and_releases_inventory(client, inv):
    r = order(client, slot_id="NOPE")
    assert r.json()["status"] == "rejected"
    assert r.json()["decision_reason"] == "unknown slot"
    assert inv.stock["SKU-1"] == 5


def test_failed_compensation_stays_pending_then_retry_drains(client, inv, slots):
    slots.slots["S1"] = 0
    inv.down_after_reserve = True  # inventory dies right after reserving, before the release
    r = order(client)
    assert r.status_code == 202
    assert r.json()["status"] == "compensating"
    assert "orders_compensating 1.0" in client.get("/metrics").text

    inv.mode = "ok"
    out = client.post("/orders/compensation-retry").json()
    assert out == {"attempted": 1, "resolved": 1}
    assert client.get("/orders/O1").json()["status"] == "rejected"
    assert inv.stock["SKU-1"] == 5
    assert "orders_compensating 0.0" in client.get("/metrics").text


def test_inventory_down_keeps_order_pending_and_replay_completes(client, inv):
    inv.mode = "down"
    assert order(client).status_code == 503
    assert client.get("/orders/O1").json()["status"] == "pending"

    inv.mode = "ok"
    r = order(client)
    assert r.status_code == 200
    assert r.json()["status"] == "confirmed"
    assert inv.stock["SKU-1"] == 4


def test_slot_service_down_after_reserve_replay_completes_without_double_reserving(client, inv, slots):
    slots.mode = "5xx"
    assert order(client).status_code == 503
    assert inv.stock["SKU-1"] == 4  # reservation is held while the order is pending

    slots.mode = "ok"
    r = order(client)
    assert r.status_code == 200
    assert r.json()["status"] == "confirmed"
    assert inv.stock["SKU-1"] == 4
    assert slots.slots["S1"] == 1


def test_cancel_confirmed_releases_everything_and_is_idempotent(client, inv, slots):
    order(client)
    r = client.post("/orders/O1/cancel")
    assert r.status_code == 200
    assert r.json()["status"] == "cancelled"
    assert inv.stock["SKU-1"] == 5
    assert slots.slots["S1"] == 2
    assert client.post("/orders/O1/cancel").json()["status"] == "cancelled"
    assert inv.stock["SKU-1"] == 5


def test_cancel_with_downstream_down_is_accepted_then_retry_completes(client, inv, slots):
    order(client)
    inv.mode = "down"
    r = client.post("/orders/O1/cancel")
    assert r.status_code == 202
    assert r.json()["status"] == "compensating"

    inv.mode = "ok"
    assert client.post("/orders/compensation-retry").json() == {"attempted": 1, "resolved": 1}
    assert client.get("/orders/O1").json()["status"] == "cancelled"
    assert inv.stock["SKU-1"] == 5
    assert slots.slots["S1"] == 2


def test_cancel_unknown_404(client):
    assert client.post("/orders/NOPE/cancel").status_code == 404


def test_fulfil_commits_inventory_and_is_idempotent(client, inv):
    order(client)
    r = client.post("/orders/O1/fulfil")
    assert r.status_code == 200
    assert r.json()["status"] == "fulfilled"
    assert inv.reservations[("O1", "SKU-1")]["status"] == "committed"
    assert client.post("/orders/O1/fulfil").status_code == 200


def test_fulfil_retry_when_inventory_down_blocks_cancel(client, inv):
    order(client)
    inv.mode = "down"
    assert client.post("/orders/O1/fulfil").status_code == 503
    assert client.get("/orders/O1").json()["status"] == "fulfilling"
    assert client.post("/orders/O1/cancel").status_code == 409

    inv.mode = "ok"
    r = client.post("/orders/O1/fulfil")
    assert r.status_code == 200
    assert r.json()["status"] == "fulfilled"
    assert inv.reservations[("O1", "SKU-1")]["status"] == "committed"


def test_fulfil_rejected_order_409(client):
    order(client, qty=9)
    assert client.post("/orders/O1/fulfil").status_code == 409


def test_cancel_after_fulfil_409(client):
    order(client)
    client.post("/orders/O1/fulfil")
    assert client.post("/orders/O1/cancel").status_code == 409


def test_get_order_and_404(client):
    order(client)
    body = client.get("/orders/O1").json()
    assert body["status"] == "confirmed" and body["qty"] == 1
    assert client.get("/orders/NOPE").status_code == 404


def test_metrics_endpoint(client):
    order(client)
    text = client.get("/metrics").text
    assert 'service="order-service"' in text
    assert "orders_outcome_total" in text