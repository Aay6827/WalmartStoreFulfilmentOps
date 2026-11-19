"""Generates dashboards/fulfilment-overview.json (dashboard as code).

Run: python infra/grafana/gen_dashboard.py
"""
import json
from pathlib import Path

DS = {"type": "prometheus", "uid": "prometheus"}
panels: list[dict] = []

STAT = {
    "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
    "colorMode": "background",
    "graphMode": "none",
    "textMode": "value",
}
SERIES = {"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi"}}
LINE = {"lineWidth": 2, "fillOpacity": 10}


def thresholds(*steps):
    return {"mode": "absolute", "steps": [{"color": c, "value": v} for c, v in steps]}


def add(kind, title, x, y, w, h, queries, unit=None, description=None, defaults=None,
        options=None, target_extra=None):
    targets = []
    for i, (expr, legend) in enumerate(queries):
        t = {"refId": chr(65 + i), "datasource": DS, "expr": expr, "legendFormat": legend}
        if target_extra:
            t.update(target_extra)
        targets.append(t)
    field_defaults = dict(defaults or {})
    if unit:
        field_defaults["unit"] = unit
    panel = {
        "id": len(panels) + 1,
        "type": kind,
        "title": title,
        "datasource": DS,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": targets,
        "fieldConfig": {"defaults": field_defaults, "overrides": []},
        "options": options or {},
    }
    if description:
        panel["description"] = description
    panels.append(panel)


GW = 'service="api-gateway",path!="/health"'

# Row 1: SLO stats
add("stat", "Availability SLI (5m)", 0, 0, 6, 4,
    [("1 - gateway:error_ratio:rate5m", "availability")], unit="percentunit",
    description="Share of gateway requests that did not fail with a 5xx. SLO: 99.5%.",
    defaults={"decimals": 3, "thresholds": thresholds(("red", None), ("green", 0.995))}, options=STAT)
add("stat", "Error-budget burn rate (1h)", 6, 0, 6, 4,
    [("gateway:error_ratio:rate1h / 0.005", "burn rate")], unit="none",
    description="1.0 = spending the 30-day error budget exactly at SLO pace. Page at 14.4, ticket at 6.",
    defaults={"decimals": 1, "thresholds": thresholds(("green", None), ("orange", 6), ("red", 14.4))},
    options=STAT)
add("stat", "Latency SLI: requests under 500 ms (5m)", 12, 0, 6, 4,
    [("1 - gateway:slow_ratio:rate5m", "fast requests")], unit="percentunit",
    description="Share of gateway requests answered in under 500 ms. SLO: 95%.",
    defaults={"decimals": 2, "thresholds": thresholds(("red", None), ("green", 0.95))}, options=STAT)
add("stat", "Firing alerts", 18, 0, 6, 4,
    [('count(ALERTS{alertstate="firing"}) or vector(0)', "firing")], unit="none",
    defaults={"thresholds": thresholds(("green", None), ("red", 1))}, options=STAT)

# Row 2: traffic and latency
add("timeseries", "Gateway requests by status", 0, 4, 12, 8,
    [(f"sum by (status) (rate(http_requests_total{{{GW}}}[1m]))", "{{status}}")],
    unit="reqps", defaults={"custom": LINE}, options=SERIES)
add("timeseries", "Gateway latency (p50 / p95 / p99)", 12, 4, 12, 8,
    [(f"histogram_quantile({q}, sum by (le) (rate(http_request_duration_seconds_bucket{{{GW}}}[1m])))", f"p{int(q * 100)}")
     for q in (0.5, 0.95, 0.99)],
    unit="s", defaults={"custom": LINE}, options=SERIES)

# Row 3: resilience
add("timeseries", "Circuit breaker state per upstream", 0, 12, 8, 8,
    [("gateway_circuit_state", "{{upstream}}")],
    description="0 closed, 1 half-open (single probe), 2 open (failing fast).",
    defaults={"min": 0, "max": 2, "custom": {"lineWidth": 2, "lineInterpolation": "stepAfter"},
              "mappings": [{"type": "value", "options": {
                  "0": {"text": "closed"}, "1": {"text": "half-open"}, "2": {"text": "open"}}}]},
    options=SERIES)
add("timeseries", "Failed upstream calls by kind", 8, 12, 8, 8,
    [("sum by (upstream, kind) (rate(gateway_upstream_errors_total[1m]))", "{{upstream}} / {{kind}}")],
    unit="ops", defaults={"custom": LINE}, options=SERIES)
add("timeseries", "Order outcomes", 16, 12, 8, 8,
    [("sum by (outcome) (rate(orders_outcome_total[1m]))", "{{outcome}}")],
    unit="ops", defaults={"custom": LINE}, options=SERIES)

# Row 4: stuck work and per-service traffic
add("timeseries", "Work stuck behind a failed dependency", 0, 20, 12, 8,
    [("max(orders_compensating)", "orders awaiting undo"),
     ("max(returns_restock_pending)", "returns awaiting restock")],
    description="Both should sit at zero. Non-zero means a dependency was down and a retry is needed.",
    unit="none", defaults={"custom": LINE}, options=SERIES)
add("timeseries", "Requests per service", 12, 20, 12, 8,
    [('sum by (service) (rate(http_requests_total{path!="/health"}[1m]))', "{{service}}")],
    unit="reqps", defaults={"custom": LINE}, options=SERIES)

# Row 5: alerts table
add("table", "Firing alerts", 0, 28, 24, 7,
    [('ALERTS{alertstate="firing"}', "")],
    options={"showHeader": True}, target_extra={"instant": True, "format": "table"})

dashboard = {
    "uid": "fulfilment-overview",
    "title": "Fulfilment overview",
    "tags": ["walmart-store-fulfilment"],
    "timezone": "browser",
    "schemaVersion": 39,
    "version": 1,
    "refresh": "5s",
    "time": {"from": "now-15m", "to": "now"},
    "templating": {"list": []},
    "annotations": {"list": []},
    "panels": panels,
}

out = Path(__file__).parent / "dashboards" / "fulfilment-overview.json"
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps(dashboard, indent=2) + "\n", encoding="utf-8")
print(f"wrote {out} ({len(panels)} panels)")