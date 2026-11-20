"""Incident drill: stop inventory-service under steady load, observe, recover, verify.

Shows, with numbers:
  * how the platform behaves while a dependency is down (errors, latency, breakers, alerts)
  * that after recovery no order is lost or double-reserved and every return is restocked once

Usage:
  python scripts/incident_drill.py --label baseline
  python scripts/incident_drill.py --control --label control     (healthy-only control run)
"""
import argparse
import json
import random
import subprocess
import sys
import threading
import time
import types
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
GATEWAY = "http://127.0.0.1:32004"
ORDERS = "http://127.0.0.1:32000"
INVENTORY = "http://127.0.0.1:32001"
SLOTS = "http://127.0.0.1:32002"
RETURNS = "http://127.0.0.1:32003"
PROM = "http://127.0.0.1:32090"

INITIAL_STOCK = 1_000_000
RETURN_STOCK = 1_000


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def iso(**delta) -> str:
    return (datetime.now(timezone.utc) + timedelta(**delta)).isoformat()


def compose(*args: str) -> float:
    started = time.monotonic()
    subprocess.run(["docker", "compose", *args], cwd=ROOT, check=True, capture_output=True)
    return time.monotonic() - started


def record(st, kind, status, started, duration, degraded=False) -> None:
    st.rows.append({"t": started - st.t0, "kind": kind, "status": status,
                    "ms": duration * 1000.0, "degraded": degraded})


# ---------------- client behaviour ----------------
def place(client, st, oid: str) -> None:
    """Idempotent client: retries the SAME order id with backoff; gives up after max_attempts."""
    sku, slot_id = st.assign[oid]
    body = {"order_id": oid, "sku": sku, "qty": 1, "slot_id": slot_id}
    for attempt in range(st.max_attempts):
        started = time.monotonic()
        status, data = 0, None
        try:
            r = client.post(f"{GATEWAY}/orders", json=body)
            status = r.status_code
            if status in (200, 201):
                data = r.json()
        except httpx.HTTPError:
            pass
        record(st, "order", status, started, time.monotonic() - started)
        if data is not None:
            st.outcomes[oid] = data.get("status", "unknown")
            if st.outcomes[oid] == "confirmed":
                with st.lock:
                    st.confirmed.append(oid)
            return
        if 400 <= status < 500:
            st.outcomes[oid] = f"http-{status}"
            return
        if attempt == st.max_attempts - 1 or st.stop.wait(min(0.5 * 2 ** attempt, 2.0)):
            break
    st.outcomes[oid] = "unresolved"


def read_summary(client, st) -> None:
    with st.lock:
        oid = random.choice(st.confirmed) if st.confirmed else None
    if oid is None:
        return
    started = time.monotonic()
    status, degraded = 0, False
    try:
        r = client.get(f"{GATEWAY}/orders/{oid}/summary")
        status = r.status_code
        if status == 200:
            degraded = bool(r.json().get("degraded"))
    except httpx.HTTPError:
        pass
    record(st, "summary", status, started, time.monotonic() - started, degraded)


def run_load(st, client, rate: float) -> None:
    """Open-loop load: arrivals keep coming at `rate`/s however slow the system gets."""
    pool = ThreadPoolExecutor(max_workers=200)
    inflight = threading.Semaphore(200)
    interval = 1.0 / rate
    next_t = time.monotonic()
    n = 0

    def order_task(oid):
        try:
            place(client, st, oid)
        finally:
            inflight.release()

    def read_task():
        try:
            read_summary(client, st)
        finally:
            inflight.release()

    while not st.stop.is_set():
        now = time.monotonic()
        if now < next_t:
            st.stop.wait(next_t - now)
            continue
        next_t += interval
        n += 1
        oid = f"D-{st.rid}-{n}"
        if not inflight.acquire(blocking=False):
            st.shed += 1  # the client itself is saturated: arrival never sent
            continue
        st.assign[oid] = (random.choice(st.skus), random.choice(st.slots))
        st.outcomes[oid] = "unresolved"
        pool.submit(order_task, oid)
        if random.random() < 0.2 and inflight.acquire(blocking=False):
            pool.submit(read_task)
    pool.shutdown(wait=True)


def monitor(st, stop_mon: threading.Event) -> None:
    while not stop_mon.is_set():
        t = time.monotonic() - st.t0
        try:
            circuits = httpx.get(f"{GATEWAY}/health", timeout=3.0).json()["circuits"]
        except Exception:
            circuits = None
        st.timeline.append((t, circuits))
        stop_mon.wait(1.0)


def receive_returns(client, st) -> None:
    def one(return_id):
        for _ in range(3):
            started = time.monotonic()
            status = 0
            try:
                status = client.post(f"{GATEWAY}/returns/{return_id}/receive",
                                     json={"condition": "resellable"}).status_code
            except httpx.HTTPError:
                pass
            record(st, "receive", status, started, time.monotonic() - started)
            if status == 200:
                return
            time.sleep(1.0)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(one, st.return_ids))


# ---------------- analysis ----------------
def pct(sorted_vals, p):
    return sorted_vals[min(len(sorted_vals) - 1, int(round(p * (len(sorted_vals) - 1))))]


def stats(rows, kind, lo, hi) -> dict:
    sel = [r for r in rows if r["kind"] == kind and lo <= r["t"] < hi]
    if not sel:
        return {"attempts": 0}
    ok = [r for r in sel if r["status"] in (200, 201)]
    lat = sorted(r["ms"] for r in sel)
    codes = Counter("timeout/transport" if r["status"] == 0 else str(r["status"]) for r in sel)
    out = {"attempts": len(sel), "ok_pct": round(100.0 * len(ok) / len(sel), 1),
           "p50_ms": round(pct(lat, 0.5)), "p95_ms": round(pct(lat, 0.95)), "max_ms": round(lat[-1]),
           "statuses": dict(sorted(codes.items()))}
    if kind == "summary":
        out["degraded"] = sum(1 for r in sel if r["degraded"])
    return out


def first_state(timeline, upstream, state, after):
    for t, circuits in timeline:
        if t >= after and circuits and circuits.get(upstream) == state:
            return t
    return None


def first_none_open(timeline, after):
    for t, circuits in timeline:
        if t >= after and circuits and "open" not in circuits.values():
            return t
    return None


def prom_instant(client, expr):
    return client.get(f"{PROM}/api/v1/query", params={"query": expr}).json()["data"]["result"]


def prom_findings(client, wall_start, wall_outage, wall_end) -> dict:
    dur = int(wall_end - wall_start) + 10
    out = {}
    try:
        res = prom_instant(client, f"max_over_time(gateway:error_ratio:rate5m[{dur}s])")
        out["peak_error_ratio_5m"] = round(float(res[0]["value"][1]), 3) if res else None
        res = prom_instant(client, f"max_over_time(gateway:slow_ratio:rate5m[{dur}s])")
        out["peak_slow_ratio_5m"] = round(float(res[0]["value"][1]), 3) if res else None
        res = prom_instant(client, f"max by (upstream) (max_over_time(gateway_circuit_state[{dur}s]))")
        out["peak_circuit_state"] = {r["metric"]["upstream"]: float(r["value"][1]) for r in res}
        res = prom_instant(client, f"max_over_time(returns_restock_pending[{dur}s])")
        out["peak_returns_restock_pending"] = float(res[0]["value"][1]) if res else None
        res = prom_instant(client, f"max_over_time(orders_compensating[{dur}s])")
        out["peak_orders_compensating"] = float(res[0]["value"][1]) if res else None
        r = client.get(f"{PROM}/api/v1/query_range",
                       params={"query": 'ALERTS{alertstate="firing"}', "start": wall_start,
                               "end": wall_end, "step": 5}).json()
        first = {}
        for s in r["data"]["result"]:
            name = s["metric"]["alertname"]
            ts = [float(v[0]) for v in s["values"] if v[1] == "1"]
            if ts:
                first[name] = min(first.get(name, 1e18), min(ts))
        out["alerts_first_fired_s_after_outage"] = {n: round(t - wall_outage, 1) for n, t in sorted(first.items())}
    except Exception as exc:
        out["error"] = repr(exc)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default="baseline")
    ap.add_argument("--rate", type=float, default=20.0, help="order arrivals per second (open loop)")
    ap.add_argument("--baseline-seconds", type=int, default=30)
    ap.add_argument("--outage-seconds", type=int, default=120)
    ap.add_argument("--recovery-seconds", type=int, default=45)
    ap.add_argument("--operator-delay", type=int, default=20,
                    help="seconds after recovery before the operator runs the retry endpoints")
    ap.add_argument("--returns", type=int, default=20)
    ap.add_argument("--skus", type=int, default=1, help="spread orders over this many SKUs")
    ap.add_argument("--slots", type=int, default=1, help="spread orders over this many delivery slots")
    ap.add_argument("--control", action="store_true", help="healthy-only run: no outage, no returns")
    args = ap.parse_args()
    if args.control:
        args.returns = 0
    args.recovery_seconds = max(args.recovery_seconds, args.operator_delay + 15)

    st = types.SimpleNamespace(
        rid=uuid.uuid4().hex[:6], t0=time.monotonic(), rows=[], outcomes={}, assign={}, confirmed=[],
        lock=threading.Lock(), stop=threading.Event(), max_attempts=3, shed=0, timeline=[],
        return_ids=[], skus=[], slots=[])
    st.skus = [f"DRILL-{st.rid}-{i}" for i in range(args.skus)]
    st.slots = [f"DRILLSLOT-{st.rid}-{i}" for i in range(args.slots)]
    st.ret_sku = f"DRILLRET-{st.rid}"
    failures: list[str] = []
    total = 0

    def check(name, ok, detail=None):
        nonlocal total
        total += 1
        print(f"[{'PASS' if ok else 'FAIL'}] {name}", flush=True)
        if not ok:
            print(f"       got: {detail}", flush=True)
            failures.append(name)

    client = httpx.Client(timeout=10.0, limits=httpx.Limits(max_connections=300, max_keepalive_connections=50))

    # ---- preflight and seed (direct to the services) ----
    health = client.get(f"{GATEWAY}/health").json()
    if "open" in health["circuits"].values():
        print("preflight failed: a circuit breaker is open:", health["circuits"])
        return 2
    for sku in st.skus:
        assert client.put(f"{INVENTORY}/stock/{sku}", json={"on_hand": INITIAL_STOCK}).status_code == 200
    for slot in st.slots:
        assert client.post(f"{SLOTS}/slots", json={"slot_id": slot, "store_id": "ST-12", "kind": "pickup",
                                                   "starts_at": iso(days=1), "capacity": INITIAL_STOCK}).status_code == 201
    if args.returns:
        assert client.put(f"{INVENTORY}/stock/{st.ret_sku}", json={"on_hand": RETURN_STOCK}).status_code == 200
        for i in range(args.returns):
            rid_ = f"DR-{st.rid}-{i}"
            r = client.post(f"{GATEWAY}/returns", json={
                "return_id": rid_, "order_id": f"DRO-{st.rid}-{i}", "sku": st.ret_sku, "qty": 1,
                "unit_price_cents": 1000, "reason": "drill", "purchased_at": iso(days=-5)})
            assert r.status_code == 201 and r.json()["status"] == "approved", r.text
            st.return_ids.append(rid_)
    log(f"seeded: {len(st.skus)} sku(s), {len(st.slots)} slot(s), {len(st.return_ids)} returns"
        + ("  [CONTROL RUN: no outage]" if args.control else ""))

    # ---- run ----
    st.t0 = time.monotonic()
    wall_start = time.time()
    stop_mon = threading.Event()
    threading.Thread(target=monitor, args=(st, stop_mon), daemon=True).start()
    load = threading.Thread(target=run_load, args=(st, client, args.rate))
    load.start()

    log(f"baseline traffic for {args.baseline_seconds}s at {args.rate:.0f} orders/s")
    time.sleep(args.baseline_seconds)

    t_out = t_healthy = t_start_issued = t_operator = None
    comp = rest = None
    wall_outage = wall_start
    if not args.control:
        log("OUTAGE: stopping inventory-service")
        stop_took = compose("stop", "inventory-service")
        t_out = time.monotonic() - st.t0
        wall_outage = time.time()
        log(f"inventory-service is down (stop took {stop_took:.1f}s); receiving {len(st.return_ids)} returns")
        time.sleep(5)
        receive_returns(client, st)
        remaining = args.outage_seconds - (time.monotonic() - st.t0 - t_out)
        if remaining > 0:
            time.sleep(remaining)

        log("RECOVERY: starting inventory-service")
        t_start_issued = time.monotonic() - st.t0
        compose("start", "inventory-service")
        deadline = time.time() + 120
        while time.time() < deadline:
            try:
                if httpx.get(f"{INVENTORY}/health", timeout=2.0).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        t_healthy = time.monotonic() - st.t0
        log(f"inventory-service healthy {t_healthy - t_start_issued:.1f}s after start")

        time.sleep(args.operator_delay)
        log("OPERATOR: running the retry endpoints")
        t_operator = time.monotonic() - st.t0
        comp = client.post(f"{ORDERS}/orders/compensation-retry").json()
        rest = client.post(f"{RETURNS}/returns/restock-retry").json()
        log(f"compensation-retry={comp} restock-retry={rest}")
        time.sleep(max(0, args.recovery_seconds - (time.monotonic() - st.t0 - t_healthy)))

    log("stopping load and draining in-flight requests")
    st.stop.set()
    load.join(timeout=90)
    stop_mon.set()
    t_load_end = time.monotonic() - st.t0
    wall_end = time.time()

    # ---- drain: client-side replay of anything unresolved, then verify against the databases ----
    st.stop.clear()
    st.max_attempts = 6
    unresolved = [o for o, s in st.outcomes.items() if s not in ("confirmed", "rejected")]
    log(f"replaying {len(unresolved)} unresolved orders (client retry after recovery)")
    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(lambda o: place(client, st, o), unresolved))

    not_received = [r_ for r_ in st.return_ids
                    if client.get(f"{RETURNS}/returns/{r_}").json().get("status") != "received"]
    for rid_ in not_received:
        client.post(f"{RETURNS}/returns/{rid_}/receive", json={"condition": "resellable"})
    if not_received:
        client.post(f"{RETURNS}/returns/restock-retry")

    all_oids = list(st.outcomes)

    def final_status(oid):
        try:
            r = client.get(f"{ORDERS}/orders/{oid}")
            return r.json()["status"] if r.status_code == 200 else f"http-{r.status_code}"
        except Exception:
            return "error"

    with ThreadPoolExecutor(max_workers=16) as pool:
        finals = Counter(pool.map(final_status, all_oids))
    reserved_total = on_hand_total = 0
    for sku in st.skus:
        d = client.get(f"{INVENTORY}/stock/{sku}").json()
        reserved_total += d["reserved"]
        on_hand_total += d["on_hand"]
    booked_total = sum(client.get(f"{SLOTS}/slots/{s}").json()["booked"] for s in st.slots)
    ret_states = Counter()
    for rid_ in st.return_ids:
        d = client.get(f"{RETURNS}/returns/{rid_}").json()
        ret_states[(d["status"], d["restock_status"])] += 1
    ret_stock = client.get(f"{INVENTORY}/stock/{st.ret_sku}").json() if st.return_ids else None

    # ---- findings ----
    rows = st.rows
    if args.control:
        phases = {"baseline": (0, 1e9)}
    else:
        phases = {"baseline": (0, t_out), "outage": (t_out, t_healthy), "recovery": (t_healthy, 1e9)}
    report = {
        "label": args.label,
        "config": {"control": args.control, "rate_per_s": args.rate, "baseline_s": args.baseline_seconds,
                   "outage_s": None if args.control else args.outage_seconds,
                   "recovery_s": args.recovery_seconds, "operator_delay_s": args.operator_delay,
                   "client_max_attempts": 3, "returns": len(st.return_ids),
                   "skus": len(st.skus), "slots": len(st.slots)},
        "events_s": {"outage_start": t_out and round(t_out, 1), "inventory_healthy": t_healthy and round(t_healthy, 1),
                     "operator_retry": t_operator and round(t_operator, 1), "load_end": round(t_load_end, 1)},
        "orders_created": len(all_oids), "orders_shed_by_client": st.shed,
        "orders_replayed_after_recovery": len(unresolved),
        "phases": {name: {"orders": stats(rows, "order", lo, hi), "summary_reads": stats(rows, "summary", lo, hi)}
                   for name, (lo, hi) in phases.items()},
        "returns_receive_during_outage": stats(rows, "receive", 0, 1e9),
        "admin_retry": {"compensation": comp, "restock": rest},
    }
    if not args.control:
        open_inv = first_state(st.timeline, "inventory-service", "open", t_out)
        open_ord = first_state(st.timeline, "order-service", "open", t_out)
        open_ret = first_state(st.timeline, "returns-service", "open", t_out)
        none_open = first_none_open(st.timeline, t_healthy)
        first_ok = next((r["t"] for r in rows if r["kind"] == "order" and r["status"] in (200, 201)
                         and r["t"] >= t_start_issued), None)

        def rel(x, base):
            return None if x is None else round(x - base, 1)

        report["breakers"] = {
            "inventory_open_s_after_outage": rel(open_inv, t_out),
            "order_service_open_s_after_outage": rel(open_ord, t_out),
            "returns_service_open_s_after_outage": rel(open_ret, t_out),
            "no_breaker_open_s_after_inventory_healthy": rel(none_open, t_healthy),
            "first_successful_order_s_after_start_command": rel(first_ok, t_start_issued),
        }
    report["prometheus"] = prom_findings(client, wall_start, wall_outage, wall_end)
    report["verification"] = {
        "final_order_statuses": dict(finals), "reserved_total": reserved_total, "on_hand_total": on_hand_total,
        "slot_booked_total": booked_total,
        "returns": {f"{s}/{r}": n for (s, r), n in ret_states.items()}, "returns_stock": ret_stock,
    }

    print("\n==================== FINDINGS ====================")
    for name in phases:
        print(f"{name:9s} POST /orders  {report['phases'][name]['orders']}")
        print(f"{'':9s} GET summary   {report['phases'][name]['summary_reads']}")
    print(f"returns receive during outage: {report['returns_receive_during_outage']}")
    print(f"breakers:   {report.get('breakers')}")
    print(f"prometheus: {report['prometheus']}")
    print(f"orders created={len(all_oids)} shed_by_client={st.shed} replayed_after_recovery={len(unresolved)}")

    print("\n==================== INVARIANTS ====================")
    confirmed = finals.get("confirmed", 0)
    check("every order reached a final state (none pending, compensating or missing)",
          set(finals) <= {"confirmed", "rejected"}, dict(finals))
    check("no order lost: every created order is confirmed", confirmed == len(all_oids),
          (confirmed, len(all_oids)))
    check("inventory reserved == confirmed orders (no double reservation, none leaked)",
          reserved_total == confirmed, (reserved_total, confirmed))
    check("inventory on_hand unchanged", on_hand_total == INITIAL_STOCK * len(st.skus), on_hand_total)
    check("slot booked == confirmed orders", booked_total == confirmed, (booked_total, confirmed))
    if st.return_ids:
        check("all returns received and restocked",
              dict(ret_states) == {("received", "done"): len(st.return_ids)}, dict(ret_states))
        check("returns restocked exactly once (on_hand = stock + returns)",
              ret_stock["on_hand"] == RETURN_STOCK + len(st.return_ids), ret_stock)

    out = ROOT / "docs" / "drills"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{args.label}.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"\nreport written to docs/drills/{args.label}.json")
    print(f"ALL {total} INVARIANTS PASSED" if not failures else f"{len(failures)} of {total} INVARIANTS FAILED")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())