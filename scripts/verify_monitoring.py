import os
import sys
import time

import httpx

PROM = "http://127.0.0.1:32090"
GRAFANA = "http://127.0.0.1:32030"
GATEWAY = "http://127.0.0.1:32004"
GRAFANA_AUTH = ("admin", os.getenv("GRAFANA_PASSWORD", "admin"))

EXPECTED_ALERTS = {
    "GatewayAvailabilityFastBurn", "GatewayAvailabilitySlowBurn",
    "GatewayLatencyFastBurn", "GatewayLatencySlowBurn",
    "ServiceDown", "CircuitOpen", "OrdersStuckCompensating", "ReturnsRestockBacklog",
}
EXPECTED_RECORDED = {
    f"gateway:{kind}:rate{w}" for kind in ("error_ratio", "slow_ratio") for w in ("5m", "30m", "1h", "6h")
}

failures: list[str] = []
total = 0


def check(name: str, ok: bool, detail=None) -> None:
    global total
    total += 1
    print(f"[{'PASS' if ok else 'FAIL'}] {name}")
    if not ok:
        print(f"       got: {detail}")
        failures.append(name)


def wait_for(fn, timeout: float = 90.0, interval: float = 2.0):
    """Poll fn() -> (ok, detail) until ok or timeout. Returns the last (ok, detail)."""
    deadline = time.time() + timeout
    last = (False, "never ran")
    while time.time() < deadline:
        try:
            last = fn()
            if last[0]:
                return last
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            last = (False, repr(exc))
        time.sleep(interval)
    return last


def main() -> int:
    with httpx.Client(timeout=10.0) as c:
        def targets():
            data = c.get(f"{PROM}/api/v1/targets").json()["data"]["activeTargets"]
            mine = [t for t in data if t["labels"].get("job") == "fulfilment"]
            return (len(mine) == 5 and all(t["health"] == "up" for t in mine),
                    [(t["scrapeUrl"], t["health"]) for t in mine])
        ok, detail = wait_for(targets)
        check("Prometheus scrapes all 5 services (all up)", ok, detail)

        def rules():
            groups = c.get(f"{PROM}/api/v1/rules").json()["data"]["groups"]
            allr = [r for g in groups for r in g["rules"]]
            alerts = {r["name"] for r in allr if r["type"] == "alerting"}
            recorded = {r["name"] for r in allr if r["type"] == "recording"}
            healthy = all(r.get("health") == "ok" for r in allr)
            return (EXPECTED_ALERTS <= alerts and EXPECTED_RECORDED <= recorded and healthy,
                    (sorted(EXPECTED_ALERTS - alerts), sorted(EXPECTED_RECORDED - recorded), healthy))
        ok, detail = wait_for(rules)
        check("all alert and recording rules loaded and evaluating without error", ok, detail)

        # Healthy traffic through the gateway, none of it a 5xx.
        for i in range(60):
            c.get(f"{GATEWAY}/orders/NOPE-{i}")
            c.get(f"{GATEWAY}/slots", params={"store_id": "NO-SUCH-STORE"})

        def query(expr: str):
            res = c.get(f"{PROM}/api/v1/query", params={"query": expr}).json()["data"]["result"]
            return float(res[0]["value"][1]) if res else None

        def error_ratio_zero():
            v = query("gateway:error_ratio:rate5m")
            return (v is not None and v == v and v < 1e-9, v)
        ok, detail = wait_for(error_ratio_zero)
        check("availability SLI computes: error ratio is 0 on healthy traffic", ok, detail)

        def slow_ratio_present():
            v = query("gateway:slow_ratio:rate5m")
            return (v is not None and v == v and v < 0.5, v)
        ok, detail = wait_for(slow_ratio_present)
        check("latency SLI computes: most requests are under 500 ms", ok, detail)

        def custom_metrics():
            names = ("gateway_circuit_state", "orders_compensating", "returns_restock_pending",
                     "orders_outcome_total")
            missing = [n for n in names if query(n) is None]
            return (not missing, missing)
        ok, detail = wait_for(custom_metrics)
        check("custom metrics are scraped (breaker state, stuck orders, restock backlog, outcomes)", ok, detail)

        fired = c.get(f"{PROM}/api/v1/alerts").json()["data"]["alerts"]
        firing = [a["labels"]["alertname"] for a in fired if a["state"] == "firing"]
        check("no alert is firing on a healthy stack", not firing, firing)

        # Grafana
        def grafana_health():
            return (c.get(f"{GRAFANA}/api/health").json().get("database") == "ok", None)
        ok, detail = wait_for(grafana_health)
        check("Grafana is up", ok, detail)

        def dashboard_found():
            res = c.get(f"{GRAFANA}/api/search", params={"query": "Fulfilment overview"}, auth=GRAFANA_AUTH).json()
            return (any(d.get("uid") == "fulfilment-overview" for d in res), res)
        ok, detail = wait_for(dashboard_found)
        check("dashboard 'Fulfilment overview' is provisioned", ok, detail)

        r = c.get(f"{GRAFANA}/api/dashboards/uid/fulfilment-overview", auth=GRAFANA_AUTH)
        panels = r.json().get("dashboard", {}).get("panels", []) if r.status_code == 200 else []
        check("dashboard has all 12 panels", len(panels) == 12, len(panels))

        r = c.get(f"{GRAFANA}/api/datasources/proxy/uid/prometheus/api/v1/query", params={"query": "up"},
                  auth=GRAFANA_AUTH)
        ok = r.status_code == 200 and bool(r.json().get("data", {}).get("result"))
        check("Grafana can query Prometheus through its datasource", ok, r.text[:200])

    print()
    if failures:
        print(f"{len(failures)} of {total} checks FAILED")
        return 1
    print(f"ALL {total} CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())