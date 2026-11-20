# Inventory-outage drill: baseline vs after-fix

Same load (20 orders/s open-loop, 1 SKU, 1 slot), same fault (inventory-service stopped for 120 s), same client (3 attempts per order, then replay after recovery).

| Metric | baseline | after-fix |
|---|---|---|
| Healthy phase (before the outage): order creation | 92.4% OK of 660 attempts, p95 1359 ms | 82.5% OK of 727 attempts, p95 2859 ms |
| Outage: order creation | 0.0% OK of 7505 attempts, p95 16 ms | 0.0% OK of 7549 attempts, p95 2422 ms |
| Outage: GET order summary | 0.2% OK (1 of them degraded, stock missing) | 11.7% OK (60 of them degraded, stock missing) |
| Outage: returns received through the gateway | 0 of 60 attempts OK | 20 of 21 attempts OK |
| Peak breaker state: inventory-service (the one that is down) | open | open |
| Peak breaker state: order-service (healthy) | open | open |
| Peak breaker state: returns-service (healthy) | open | closed |
| Peak 5-minute gateway error ratio | 90.6% | 91.5% |
| Recovery: first successful order after the start command | 12.1 s | 13.5 s |
| Recovery: time until no breaker is open | never | 7.7 s |
| Recovery phase: order creation | 89.0% OK of 3855 attempts, p95 531 ms | 65.8% OK of 5185 attempts, p95 3531 ms |
| Orders replayed by the client after recovery | 2641 of 4042 | 3041 of 4014 |
| Orders confirmed at the end (none lost) | 4042 | 4014 |
| Alert CircuitOpen | 43.0 s after the outage began | did not fire |
| Alert GatewayAvailabilityFastBurn | 68.0 s after the outage began | 72.7 s after the outage began |
| Alert ReturnsRestockBacklog | 133.0 s after the outage began | 132.7 s after the outage began |
| Alert ServiceDown | 38.0 s after the outage began | 32.7 s after the outage began |
