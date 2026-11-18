import sys
import time
import uuid
from datetime import datetime, timedelta, timezone

import httpx

GATEWAY = "http://127.0.0.1:32004"
INVENTORY = "http://127.0.0.1:32001"
SLOTS = "http://127.0.0.1:32002"

rid = uuid.uuid4().hex[:6]
sku = f"SKU-{rid}"
slot_id = f"SLOT-{rid}"
failures: list[str] = []
total = 0


def check(name: str, ok: bool, detail=None) -> None:
    global total
    total += 1
    print(f"[{'PASS' if ok else 'FAIL'}] {name}")
    if not ok:
        print(f"       got: {detail}")
        failures.append(name)


def iso(**delta) -> str:
    return (datetime.now(timezone.utc) + timedelta(**delta)).isoformat()


def main() -> int:
    with httpx.Client(timeout=10.0) as c:
        deadline = time.time() + 60
        while True:
            try:
                if c.get(f"{GATEWAY}/health").status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if time.time() > deadline:
                print("gateway never became healthy")
                return 1
            time.sleep(1)

        def stock():
            return c.get(f"{GATEWAY}/stock/{sku}").json()

        def triple():
            s = stock()
            return (s["on_hand"], s["reserved"], s["available"])

        def place(oid, qty):
            return c.post(f"{GATEWAY}/orders",
                          json={"order_id": oid, "sku": sku, "qty": qty, "slot_id": slot_id})

        def summary(oid):
            return c.get(f"{GATEWAY}/orders/{oid}/summary").json()

        a, b, big, d, e = (f"{x}-{rid}" for x in ("A", "B", "C", "D", "E"))

        r = c.put(f"{INVENTORY}/stock/{sku}", json={"on_hand": 10})
        check("seed stock", r.status_code == 200, r.text)
        r = c.post(f"{SLOTS}/slots", json={"slot_id": slot_id, "store_id": "ST-12", "kind": "pickup",
                                           "starts_at": iso(days=1), "capacity": 2})
        check("seed slot", r.status_code == 201, r.text)

        # --- happy path ---
        r = place(a, 3)
        check("order A confirmed (201)", r.status_code == 201 and r.json()["status"] == "confirmed", r.text)
        r = place(a, 3)
        check("order A replay is idempotent (200)", r.status_code == 200 and r.json()["status"] == "confirmed", r.text)
        check("stock reserved once for A", triple() == (10, 3, 7), triple())
        s = summary(a)
        check("summary complete, nothing degraded", s["degraded"] == [], s)
        check("summary shows active booking and slot usage",
              s["booking"]["status"] == "active" and (s["slot"]["booked"], s["slot"]["available"]) == (1, 1), s)

        r = c.post(f"{GATEWAY}/orders/{a}/fulfil")
        check("fulfil A", r.status_code == 200 and r.json()["status"] == "fulfilled", r.text)
        check("stock committed after fulfil", triple() == (7, 0, 7), triple())

        # --- cancel path ---
        r = place(b, 2)
        check("order B confirmed", r.status_code == 201 and r.json()["status"] == "confirmed", r.text)
        check("stock reserved for B", triple() == (7, 2, 5), triple())
        check("slot full after B", summary(b)["slot"]["available"] == 0, summary(b)["slot"])
        r = c.post(f"{GATEWAY}/orders/{b}/cancel")
        check("cancel B", r.status_code == 200 and r.json()["status"] == "cancelled", r.text)
        check("cancel released stock", triple() == (7, 0, 7), triple())
        check("cancel freed the slot", summary(a)["slot"]["available"] == 1, summary(a)["slot"])

        # --- rejections ---
        r = place(big, 99)
        check("out-of-stock order rejected",
              r.status_code == 201 and r.json()["status"] == "rejected"
              and r.json()["decision_reason"] == "insufficient stock", r.text)
        check("rejection left stock untouched", triple() == (7, 0, 7), triple())

        r = place(d, 1)
        check("order D confirmed (fills slot)", r.json()["status"] == "confirmed", r.text)
        check("stock after D", triple() == (7, 1, 6), triple())
        r = place(e, 1)
        check("order E rejected: slot is full",
              r.json()["status"] == "rejected" and "slot is full" in r.json()["decision_reason"], r.text)
        check("rejection compensated: E's reservation released", triple() == (7, 1, 6), triple())

        # --- returns ---
        ret = f"R-{rid}"
        body = {"return_id": ret, "order_id": a, "sku": sku, "qty": 3, "unit_price_cents": 1500,
                "reason": "smoke", "purchased_at": iso(days=-5)}
        r = c.post(f"{GATEWAY}/returns", json=body)
        check("return approved", r.status_code == 201 and r.json()["status"] == "approved", r.text)
        r = c.post(f"{GATEWAY}/returns/{ret}/receive", json={"condition": "resellable"})
        check("return received and restocked",
              r.status_code == 200 and r.json()["status"] == "received" and r.json()["restock_status"] == "done", r.text)
        check("restock landed in inventory", triple() == (10, 1, 9), triple())
        r = c.post(f"{GATEWAY}/returns/{ret}/receive", json={"condition": "resellable"})
        check("receive replay is idempotent, no double restock", r.status_code == 200 and triple() == (10, 1, 9), triple())
        r = c.post(f"{GATEWAY}/returns/{ret}/refund")
        check("refund = qty x price", r.status_code == 200 and r.json()["refund_cents"] == 4500, r.text)
        r = c.post(f"{GATEWAY}/returns/{ret}/refund")
        check("refund replay is idempotent", r.status_code == 200 and r.json()["status"] == "refunded", r.text)

        late = dict(body, return_id=f"R2-{rid}", purchased_at=iso(days=-120))
        r = c.post(f"{GATEWAY}/returns", json=late)
        check("return outside the 90-day window rejected",
              r.status_code == 201 and r.json()["status"] == "rejected", r.text)

        # --- platform health ---
        circuits = c.get(f"{GATEWAY}/health").json()["circuits"]
        check("all circuit breakers closed", set(circuits.values()) == {"closed"}, circuits)
        check("gateway metrics exposed", "http_requests_total" in c.get(f"{GATEWAY}/metrics").text)

    print()
    if failures:
        print(f"{len(failures)} of {total} checks FAILED")
        return 1
    print(f"ALL {total} CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())