"""Side-by-side comparison of two incident-drill reports.

Usage: python scripts/compare_drills.py baseline after-fix
Prints the table and writes docs/drills/comparison.md
"""
import json
import sys
from pathlib import Path

DRILLS = Path(__file__).resolve().parents[1] / "docs" / "drills"
STATES = {0: "closed", 1: "half-open", 2: "open"}


def load(label):
    return json.loads((DRILLS / f"{label}.json").read_text(encoding="utf-8"))


def get(d, *path, default=None):
    for p in path:
        if not isinstance(d, dict) or p not in d:
            return default
        d = d[p]
    return d


def phase_orders(phase):
    def f(d):
        o = get(d, "phases", phase, "orders") or {}
        if not o.get("attempts"):
            return "n/a"
        return f"{o['ok_pct']}% OK of {o['attempts']} attempts, p95 {o['p95_ms']} ms"
    return f


def summary_reads(d):
    s = get(d, "phases", "outage", "summary_reads") or {}
    if not s.get("attempts"):
        return "n/a"
    return f"{s['ok_pct']}% OK ({s.get('degraded', 0)} of them degraded, stock missing)"


def receive(d):
    r = d.get("returns_receive_during_outage") or {}
    if not r.get("attempts"):
        return "n/a"
    ok = round(r["ok_pct"] * r["attempts"] / 100)
    return f"{ok} of {r['attempts']} attempts OK"


def circuit(upstream):
    def f(d):
        v = get(d, "prometheus", "peak_circuit_state", upstream)
        return "n/a" if v is None else STATES.get(int(v), str(v))
    return f


def error_ratio(d):
    v = get(d, "prometheus", "peak_error_ratio_5m")
    return "n/a" if v is None else f"{v * 100:.1f}%"


def first_success(d):
    b = d.get("breakers") or {}
    v = b.get("first_successful_order_s_after_start_command")
    return "n/a" if v is None else f"{v} s"


def breakers_clear(d):
    b = d.get("breakers") or {}
    for key in ("no_breaker_open_s_after_inventory_healthy", "all_closed_s_after_inventory_healthy"):
        if key in b:
            return "never" if b[key] is None else f"{b[key]} s"
    return "n/a"


def replayed(d):
    return f"{d['orders_replayed_after_recovery']} of {d['orders_created']}"


def confirmed(d):
    return str(get(d, "verification", "final_order_statuses", "confirmed"))


def main() -> int:
    a_label, b_label = sys.argv[1], sys.argv[2]
    a, b = load(a_label), load(b_label)

    rows = [
        ("Healthy phase (before the outage): order creation", phase_orders("baseline")),
        ("Outage: order creation", phase_orders("outage")),
        ("Outage: GET order summary", summary_reads),
        ("Outage: returns received through the gateway", receive),
        ("Peak breaker state: inventory-service (the one that is down)", circuit("inventory-service")),
        ("Peak breaker state: order-service (healthy)", circuit("order-service")),
        ("Peak breaker state: returns-service (healthy)", circuit("returns-service")),
        ("Peak 5-minute gateway error ratio", error_ratio),
        ("Recovery: first successful order after the start command", first_success),
        ("Recovery: time until no breaker is open", breakers_clear),
        ("Recovery phase: order creation", phase_orders("recovery")),
        ("Orders replayed by the client after recovery", replayed),
        ("Orders confirmed at the end (none lost)", confirmed),
    ]
    names = sorted(set(get(a, "prometheus", "alerts_first_fired_s_after_outage", default={}))
                   | set(get(b, "prometheus", "alerts_first_fired_s_after_outage", default={})))

    def alert(name):
        def f(d):
            v = get(d, "prometheus", "alerts_first_fired_s_after_outage", name)
            return "did not fire" if v is None else f"{v} s after the outage began"
        return f

    rows += [(f"Alert {n}", alert(n)) for n in names]

    cfg = b["config"]
    lines = [
        f"# Inventory-outage drill: {a_label} vs {b_label}",
        "",
        f"Same load ({cfg['rate_per_s']:.0f} orders/s open-loop, {cfg['skus']} SKU, {cfg['slots']} slot), same fault "
        f"(inventory-service stopped for {cfg['outage_s']} s), same client (3 attempts per order, then replay after recovery).",
        "",
        f"| Metric | {a_label} | {b_label} |",
        "|---|---|---|",
    ]
    for name, fn in rows:
        lines.append(f"| {name} | {fn(a)} | {fn(b)} |")
    text = "\n".join(lines) + "\n"
    print(text)
    (DRILLS / "comparison.md").write_text(text, encoding="utf-8")
    print("written to docs/drills/comparison.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())