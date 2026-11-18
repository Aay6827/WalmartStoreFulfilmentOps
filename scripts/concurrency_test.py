import sys
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import httpx

INVENTORY = "http://127.0.0.1:32001"
SLOTS = "http://127.0.0.1:32002"
RETURNS = "http://127.0.0.1:32003"

rid = uuid.uuid4().hex[:6]
failures: list[str] = []
total = 0
client = httpx.Client(timeout=30.0, limits=httpx.Limits(max_connections=100))


def check(name: str, ok: bool, detail=None) -> None:
    global total
    total += 1
    print(f"[{'PASS' if ok else 'FAIL'}] {name}")
    if not ok:
        print(f"       got: {detail}")
        failures.append(name)


def iso(**delta) -> str:
    return (datetime.now(timezone.utc) + timedelta(**delta)).isoformat()


def blast(fn, n: int, workers: int = 30) -> list:
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(fn, range(n)))


def put_stock(sku: str, on_hand: int) -> None:
    r = client.put(f"{INVENTORY}/stock/{sku}", json={"on_hand": on_hand})
    assert r.status_code == 200, r.text


def get_stock(sku: str) -> dict:
    return client.get(f"{INVENTORY}/stock/{sku}").json()


def make_slot(slot_id: str, capacity: int) -> None:
    r = client.post(f"{SLOTS}/slots", json={"slot_id": slot_id, "store_id": "ST-12", "kind": "pickup",
                                            "starts_at": iso(days=1), "capacity": capacity})
    assert r.status_code == 201, r.text


def get_slot(slot_id: str) -> dict:
    return client.get(f"{SLOTS}/slots/{slot_id}").json()


def reserve(sku: str, oid: str, qty: int = 1) -> int:
    return client.post(f"{INVENTORY}/reservations", json={"order_id": oid, "sku": sku, "qty": qty}).status_code


def book(slot_id: str, oid: str) -> int:
    return client.post(f"{SLOTS}/bookings", json={"order_id": oid, "slot_id": slot_id}).status_code


def main() -> int:
    # 1. Oversell: 60 distinct orders fight for 20 units.
    sku = f"CC-{rid}-1"
    put_stock(sku, 20)
    counts = dict(Counter(blast(lambda i: reserve(sku, f"o{i}-{rid}"), 60)))
    check("60 concurrent reservations over 20 units: exactly 20 win, 40 refused",
          counts == {201: 20, 409: 40}, counts)
    s = get_stock(sku)
    check("never oversold: on_hand 20, reserved 20, available 0",
          (s["on_hand"], s["reserved"], s["available"]) == (20, 20, 0), s)

    # 2. The same reservation sent 20 times at once.
    sku = f"CC-{rid}-2"
    put_stock(sku, 5)
    counts = dict(Counter(blast(lambda i: reserve(sku, f"same-{rid}"), 20)))
    check("20 identical concurrent reservations: one created, 19 replays", counts == {201: 1, 200: 19}, counts)
    check("stock reserved exactly once", get_stock(sku)["reserved"] == 1, get_stock(sku))

    # 3. Slot capacity.
    slot = f"CCSLOT-{rid}-1"
    make_slot(slot, 5)
    counts = dict(Counter(blast(lambda i: book(slot, f"b{i}-{rid}"), 30)))
    check("30 concurrent bookings into 5 places: exactly 5 win, 25 refused", counts == {201: 5, 409: 25}, counts)
    sl = get_slot(slot)
    check("slot never overbooked", (sl["booked"], sl["available"]) == (5, 0), sl)

    # 4. The same booking sent 15 times at once.
    slot = f"CCSLOT-{rid}-2"
    make_slot(slot, 5)
    counts = dict(Counter(blast(lambda i: book(slot, f"samebook-{rid}"), 15)))
    check("15 identical concurrent bookings: one created, 14 replays", counts == {201: 1, 200: 14}, counts)
    check("slot booked exactly once", get_slot(slot)["booked"] == 1, get_slot(slot))

    # 5. Release racing commit on one reservation: exactly one transition wins.
    sku = f"CC-{rid}-5"
    oid = f"race-{rid}"
    put_stock(sku, 10)
    check("reserve 4 for the race", reserve(sku, oid, 4) == 201)

    def transition(i: int):
        action = "release" if i % 2 == 0 else "commit"
        code = client.post(f"{INVENTORY}/reservations/{oid}/{sku}/{action}").status_code
        return action, code

    results = blast(transition, 20)
    s = get_stock(sku)
    winner = "release" if s["on_hand"] == 10 else "commit" if s["on_hand"] == 6 else None
    check("exactly one outcome applied and nothing left reserved",
          winner is not None and s["reserved"] == 0, s)
    check("winner's duplicates all replay 200, the opposite action is refused 409",
          winner is not None
          and all(code == (200 if action == winner else 409) for action, code in results), Counter(results))

    # 6. Concurrent duplicate receives and refunds, through the real inventory-service.
    sku = f"CC-{rid}-6"
    put_stock(sku, 10)
    ret = f"RET-{rid}"
    r = client.post(f"{RETURNS}/returns", json={"return_id": ret, "order_id": f"O-{rid}", "sku": sku, "qty": 3,
                                                "unit_price_cents": 1500, "reason": "race",
                                                "purchased_at": iso(days=-5)})
    check("return created", r.status_code == 201, r.text)
    counts = dict(Counter(blast(
        lambda i: client.post(f"{RETURNS}/returns/{ret}/receive", json={"condition": "resellable"}).status_code, 15)))
    check("15 concurrent receives all succeed", counts == {200: 15}, counts)
    check("inventory restocked exactly once (10 + 3)", get_stock(sku)["on_hand"] == 13, get_stock(sku))
    got = client.get(f"{RETURNS}/returns/{ret}").json()
    check("restock marked done", got["status"] == "received" and got["restock_status"] == "done", got)
    counts = dict(Counter(blast(lambda i: client.post(f"{RETURNS}/returns/{ret}/refund").status_code, 15)))
    check("15 concurrent refunds all succeed", counts == {200: 15}, counts)
    got = client.get(f"{RETURNS}/returns/{ret}").json()
    check("refunded once for 4500", got["status"] == "refunded" and got["refund_cents"] == 4500, got)

    print()
    if failures:
        print(f"{len(failures)} of {total} checks FAILED")
        return 1
    print(f"ALL {total} CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())