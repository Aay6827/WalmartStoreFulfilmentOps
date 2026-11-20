# Timeout budget

Every hop must give up BEFORE the hop above it. Otherwise the caller times out first, reports a
failure the callee never got to report, and the circuit breaker blames a healthy service.

| Hop | Limit | Why |
|---|---|---|
| client -> api-gateway | 2.5 s deadline (`GATEWAY_DEADLINE_SECONDS`) | total budget for one request; each upstream call gets only what is left of it |
| api-gateway -> any service | the remaining budget | never longer than the deadline |
| order-service -> inventory / slot-service | 0.5 s per call (`DOWNSTREAM_TIMEOUT_SECONDS`) | worst case is 4 sequential calls (reserve, book, release, unbook) = 2.0 s < 2.5 s |
| returns-service -> inventory-service | 0.5 s | a received return answers within 0.5 s even when the restock cannot be sent |

Rules

1. Downstream timeout x number of sequential calls < upstream deadline.
2. Never hold a database connection across a remote call (regression-tested in
   `test_connection_hygiene.py` for order-service and returns-service).
3. A dependency being down must surface as the service's own deliberate 503 or a `pending` state,
   never as a gateway timeout. The gateway does not count that 503 against the service's breaker.
4. An open breaker whose cool-down has elapsed is reported as half-open, so an idle upstream does
   not look open forever.