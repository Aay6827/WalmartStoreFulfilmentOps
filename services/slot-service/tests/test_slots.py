from datetime import datetime, timedelta, timezone


def when(hours: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


def make_slot(client, slot_id="S1", store="ST-12", kind="pickup", hours=24, capacity=2):
    return client.post(
        "/slots",
        json={"slot_id": slot_id, "store_id": store, "kind": kind,
              "starts_at": when(hours), "capacity": capacity},
    )


def book(client, order_id, slot_id="S1"):
    return client.post("/bookings", json={"order_id": order_id, "slot_id": slot_id})


def test_create_and_get_slot(client):
    r = make_slot(client, capacity=3)
    assert r.status_code == 201
    body = client.get("/slots/S1").json()
    assert body["capacity"] == 3 and body["booked"] == 0 and body["available"] == 3


def test_duplicate_slot_409(client):
    make_slot(client)
    assert make_slot(client).status_code == 409


def test_list_filters(client):
    make_slot(client, "S1", store="ST-12", kind="pickup", hours=24, capacity=1)
    make_slot(client, "S2", store="ST-12", kind="delivery", hours=26, capacity=5)
    make_slot(client, "S3", store="ST-99", kind="pickup", hours=28, capacity=5)
    book(client, "o1", "S1")  # fills S1

    assert [s["slot_id"] for s in client.get("/slots", params={"store_id": "ST-12"}).json()] == ["S1", "S2"]
    assert [s["slot_id"] for s in client.get("/slots", params={"kind": "pickup"}).json()] == ["S1", "S3"]
    avail = client.get("/slots", params={"store_id": "ST-12", "available_only": "true"}).json()
    assert [s["slot_id"] for s in avail] == ["S2"]


def test_book_reduces_availability(client):
    make_slot(client, capacity=2)
    r = book(client, "o1")
    assert r.status_code == 201 and r.json()["status"] == "active"
    assert client.get("/slots/S1").json()["available"] == 1


def test_book_is_idempotent(client):
    make_slot(client, capacity=2)
    assert book(client, "o1").status_code == 201
    assert book(client, "o1").status_code == 200
    assert client.get("/slots/S1").json()["booked"] == 1


def test_same_order_different_slot_409(client):
    make_slot(client, "S1")
    make_slot(client, "S2")
    book(client, "o1", "S1")
    assert book(client, "o1", "S2").status_code == 409
    assert client.get("/slots/S2").json()["booked"] == 0


def test_full_slot_409(client):
    make_slot(client, capacity=1)
    assert book(client, "o1").status_code == 201
    assert book(client, "o2").status_code == 409
    assert client.get("/slots/S1").json()["booked"] == 1


def test_unknown_slot_404(client):
    assert book(client, "o1", "NOPE").status_code == 404


def test_past_slot_409(client):
    make_slot(client, hours=-2)
    assert book(client, "o1").status_code == 409
    assert client.get("/slots/S1").json()["booked"] == 0


def test_cancel_restores_capacity_and_is_idempotent(client):
    make_slot(client, capacity=1)
    book(client, "o1")
    first = client.post("/bookings/o1/cancel")
    assert first.status_code == 200 and first.json()["status"] == "cancelled"
    assert client.post("/bookings/o1/cancel").status_code == 200
    assert client.get("/slots/S1").json()["available"] == 1
    assert book(client, "o2").status_code == 201  # freed capacity is usable


def test_cancelled_order_cannot_rebook_same_slot(client):
    make_slot(client)
    book(client, "o1")
    client.post("/bookings/o1/cancel")
    assert book(client, "o1").status_code == 409


def test_metrics_endpoint(client):
    client.get("/health")
    text = client.get("/metrics").text
    assert 'service="slot-service"' in text