def seed(client, sku="SKU-1", qty=10):
    r = client.put(f"/stock/{sku}", json={"on_hand": qty})
    assert r.status_code == 200
    return r.json()


def reserve(client, order_id, sku="SKU-1", qty=1):
    return client.post("/reservations", json={"order_id": order_id, "sku": sku, "qty": qty})


def test_put_and_get_stock(client):
    seed(client, qty=10)
    body = client.get("/stock/SKU-1").json()
    assert body == {"sku": "SKU-1", "on_hand": 10, "reserved": 0, "available": 10}


def test_get_unknown_stock_404(client):
    assert client.get("/stock/NOPE").status_code == 404


def test_reserve_reduces_available(client):
    seed(client, qty=10)
    r = reserve(client, "o1", qty=3)
    assert r.status_code == 201
    assert r.json()["status"] == "active"
    assert client.get("/stock/SKU-1").json()["available"] == 7


def test_reserve_is_idempotent(client):
    seed(client, qty=10)
    assert reserve(client, "o1", qty=3).status_code == 201
    again = reserve(client, "o1", qty=3)
    assert again.status_code == 200
    assert client.get("/stock/SKU-1").json()["reserved"] == 3


def test_reserve_idempotency_conflict(client):
    seed(client, qty=10)
    reserve(client, "o1", qty=3)
    assert reserve(client, "o1", qty=5).status_code == 409
    assert client.get("/stock/SKU-1").json()["reserved"] == 3


def test_reserve_insufficient_stock(client):
    seed(client, qty=2)
    assert reserve(client, "o1", qty=3).status_code == 409
    assert client.get("/stock/SKU-1").json()["reserved"] == 0


def test_reserve_unknown_sku(client):
    assert reserve(client, "o1", sku="NOPE").status_code == 404


def test_reserve_validation(client):
    seed(client)
    assert reserve(client, "o1", qty=0).status_code == 422


def test_release_restores_availability_and_is_idempotent(client):
    seed(client, qty=10)
    reserve(client, "o1", qty=4)
    first = client.post("/reservations/o1/SKU-1/release")
    assert first.status_code == 200 and first.json()["status"] == "released"
    second = client.post("/reservations/o1/SKU-1/release")
    assert second.status_code == 200
    assert client.get("/stock/SKU-1").json()["available"] == 10


def test_commit_reduces_on_hand(client):
    seed(client, qty=10)
    reserve(client, "o1", qty=4)
    r = client.post("/reservations/o1/SKU-1/commit")
    assert r.status_code == 200 and r.json()["status"] == "committed"
    assert client.get("/stock/SKU-1").json() == {"sku": "SKU-1", "on_hand": 6, "reserved": 0, "available": 6}
    assert client.post("/reservations/o1/SKU-1/release").status_code == 409


def test_cannot_lower_on_hand_below_reserved(client):
    seed(client, qty=10)
    reserve(client, "o1", qty=8)
    assert client.put("/stock/SKU-1", json={"on_hand": 5}).status_code == 409
    assert client.get("/stock/SKU-1").json()["on_hand"] == 10


def test_metrics_endpoint(client):
    client.get("/health")
    text = client.get("/metrics").text
    assert "http_requests_total" in text
    assert 'service="inventory-service"' in text