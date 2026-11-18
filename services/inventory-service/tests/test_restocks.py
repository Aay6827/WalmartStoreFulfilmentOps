def seed(client, sku="SKU-1", qty=10):
    assert client.put(f"/stock/{sku}", json={"on_hand": qty}).status_code == 200


def restock(client, return_id="R1", sku="SKU-1", qty=2):
    return client.post("/restocks", json={"return_id": return_id, "sku": sku, "qty": qty})


def test_restock_increases_on_hand(client):
    seed(client, qty=10)
    r = restock(client, qty=3)
    assert r.status_code == 201
    assert client.get("/stock/SKU-1").json()["on_hand"] == 13


def test_restock_is_idempotent(client):
    seed(client, qty=10)
    assert restock(client, qty=3).status_code == 201
    assert restock(client, qty=3).status_code == 200
    assert client.get("/stock/SKU-1").json()["on_hand"] == 13


def test_restock_idempotency_conflict(client):
    seed(client, qty=10)
    restock(client, qty=3)
    assert restock(client, qty=5).status_code == 409
    assert client.get("/stock/SKU-1").json()["on_hand"] == 13


def test_restock_unknown_sku_404(client):
    assert restock(client, sku="NOPE").status_code == 404
    assert client.get("/stock/NOPE").status_code == 404