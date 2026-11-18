import pytest

VALID = {"order_id": "O9", "sku": "SKU-1", "qty": 2, "slot_id": "S1"}


def trip_order_breaker(client, svc):
    svc.order.mode = "down"
    for _ in range(3):
        assert client.get("/orders/O1").status_code == 502


# ---------- proxying ----------
def test_create_order_is_proxied(client, svc):
    r = client.post("/orders", json=VALID)
    assert r.status_code == 201
    assert r.json()["order_id"] == "O9" and r.json()["status"] == "confirmed"
    assert ("POST", "/orders") in svc.order.calls


def test_get_order_and_404_passthrough(client):
    assert client.get("/orders/O1").json()["status"] == "confirmed"
    r = client.get("/orders/NOPE")
    assert r.status_code == 404 and r.json()["detail"] == "order not found"


def test_validation_error_passes_through_and_does_not_trip_breaker(client, svc):
    for _ in range(5):
        r = client.post("/orders", json={**VALID, "qty": 0})
        assert r.status_code == 422
    assert len(svc.order.calls) == 5  # every call reached order-service


def test_cancel_and_fulfil_are_proxied(client):
    assert client.post("/orders/O1/cancel").json()["status"] == "cancelled"
    assert client.post("/orders/O1/fulfil").json()["status"] == "fulfilled"
    assert client.post("/orders/NOPE/cancel").status_code == 404


def test_returns_routes(client):
    body = {"return_id": "R1", "order_id": "O1", "sku": "SKU-1", "qty": 1}
    assert client.post("/returns", json=body).status_code == 201
    assert client.get("/returns/R1").json()["status"] == "approved"
    r = client.post("/returns/R1/receive", json={"condition": "resellable"})
    assert r.status_code == 200 and r.json()["condition"] == "resellable"
    assert client.post("/returns/R1/refund").json()["refund_cents"] == 3000
    assert client.get("/returns/NOPE").status_code == 404


def test_slots_and_stock_routes(client, svc):
    r = client.get("/slots", params={"store_id": "ST-12", "kind": "pickup"})
    assert r.status_code == 200 and r.json()[0]["slot_id"] == "S1"
    assert svc.slots.last_query == {"store_id": "ST-12", "kind": "pickup"}
    assert client.get("/stock/SKU-1").json()["available"] == 4
    assert client.get("/stock/NOPE").status_code == 404


# ---------- failure mapping and deadline ----------
def test_upstream_timeout_returns_504(client, svc):
    svc.order.mode = "timeout"
    r = client.get("/orders/O1")
    assert r.status_code == 504 and "timed out" in r.json()["detail"]


def test_upstream_unreachable_returns_502(client, svc):
    svc.order.mode = "down"
    r = client.get("/orders/O1")
    assert r.status_code == 502 and "unreachable" in r.json()["detail"]


def test_upstream_5xx_returns_502(client, svc):
    svc.order.mode = "5xx"
    r = client.get("/orders/O1")
    assert r.status_code == 502 and "returned 500" in r.json()["detail"]


def test_remaining_deadline_is_sent_as_the_call_timeout(client, svc):
    client.get("/orders/O1")
    assert svc.order.timeouts == [pytest.approx(1.0)]


# ---------- circuit breaker behaviour ----------
def test_circuit_opens_fails_fast_then_recovers(client, svc, clock):
    trip_order_breaker(client, svc)
    assert len(svc.order.calls) == 3

    r = client.get("/orders/O1")
    assert r.status_code == 503 and "circuit open" in r.json()["detail"]
    assert int(r.headers["Retry-After"]) >= 1
    assert len(svc.order.calls) == 3  # failed fast: nothing was sent

    clock.advance(5)
    assert client.get("/orders/O1").status_code == 503  # still cooling down

    clock.advance(6)
    svc.order.mode = "ok"
    assert client.get("/orders/O1").status_code == 200  # the probe
    assert client.get("/orders/O1").status_code == 200  # closed again
    assert len(svc.order.calls) == 5


def test_failed_probe_reopens_the_circuit(client, svc, clock):
    trip_order_breaker(client, svc)
    clock.advance(11)
    assert client.get("/orders/O1").status_code == 502  # probe hits the still-dead service
    assert len(svc.order.calls) == 4
    assert client.get("/orders/O1").status_code == 503  # open again, nothing sent
    assert len(svc.order.calls) == 4


def test_circuits_are_per_upstream(client, svc):
    trip_order_breaker(client, svc)
    assert client.get("/stock/SKU-1").status_code == 200


def test_deliberate_503_is_passed_through_and_not_counted(client, svc):
    svc.order.mode = "503"
    for _ in range(6):
        r = client.post("/orders", json=VALID)
        assert r.status_code == 503
        assert "order stays pending" in r.json()["detail"]
    assert len(svc.order.calls) == 6  # the breaker never opened
    svc.order.mode = "ok"
    assert client.post("/orders", json=VALID).status_code == 201
    assert client.get("/health").json()["circuits"]["order-service"] == "closed"


def test_health_reports_circuit_states(client, svc):
    circuits = client.get("/health").json()["circuits"]
    assert set(circuits.values()) == {"closed"} and len(circuits) == 4
    trip_order_breaker(client, svc)
    assert client.get("/health").json()["circuits"]["order-service"] == "open"


# ---------- aggregated summary ----------
def test_summary_happy_path(client):
    r = client.get("/orders/O1/summary")
    assert r.status_code == 200
    body = r.json()
    assert body["order"]["status"] == "confirmed"
    assert body["booking"]["status"] == "active"
    assert body["slot"]["available"] == 1
    assert body["stock"]["available"] == 4
    assert body["degraded"] == []


def test_summary_is_degraded_when_slot_service_is_down(client, svc):
    svc.slots.mode = "down"
    r = client.get("/orders/O1/summary")
    assert r.status_code == 200
    body = r.json()
    assert body["order"]["order_id"] == "O1"
    assert body["booking"] is None and body["slot"] is None
    assert body["stock"]["available"] == 4
    assert body["degraded"] == ["booking", "slot"]


def test_summary_404_enrichment_is_not_degraded(client):
    body = client.get("/orders/O2/summary").json()  # rejected order: no booking exists
    assert body["order"]["status"] == "rejected"
    assert body["booking"] is None
    assert body["slot"]["slot_id"] == "S1"
    assert body["degraded"] == []


def test_summary_unknown_order_404(client):
    assert client.get("/orders/NOPE/summary").status_code == 404


def test_summary_requires_order_service(client, svc):
    svc.order.mode = "down"
    assert client.get("/orders/O1/summary").status_code == 502


def test_summary_degrades_when_the_deadline_is_spent_and_does_not_trip_breakers(client, svc):
    svc.order.advance = 1.5  # the order call alone burns the whole 1.0 s budget
    r = client.get("/orders/O1/summary")
    assert r.status_code == 200
    assert r.json()["degraded"] == ["booking", "slot", "stock"]
    assert svc.slots.calls == [] and svc.inventory.calls == []
    circuits = client.get("/health").json()["circuits"]
    assert set(circuits.values()) == {"closed"}  # a spent budget is not an upstream failure
    svc.order.advance = 0
    assert client.get("/stock/SKU-1").status_code == 200


# ---------- metrics ----------
def test_metrics_expose_circuit_state(client, svc):
    trip_order_breaker(client, svc)
    client.get("/orders/O1")  # rejected by the open circuit
    text = client.get("/metrics").text
    assert 'gateway_circuit_state{upstream="order-service"} 2.0' in text
    assert 'gateway_circuit_rejections_total{upstream="order-service"}' in text
    assert 'service="api-gateway"' in text