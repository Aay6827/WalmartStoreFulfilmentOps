import threading
import time

import httpx
from prometheus_client import Counter, Gauge

CIRCUIT_STATE = Gauge(
    "gateway_circuit_state",
    "Circuit breaker state per upstream: 0 closed, 1 half-open, 2 open",
    ["upstream"],
)
UPSTREAM_ERRORS = Counter(
    "gateway_upstream_errors_total", "Failed upstream calls", ["upstream", "kind"]
)
CIRCUIT_REJECTIONS = Counter(
    "gateway_circuit_rejections_total",
    "Calls rejected without being sent because the circuit was open",
    ["upstream"],
)

CLOSED, HALF_OPEN, OPEN = "closed", "half_open", "open"
_STATE_VALUE = {CLOSED: 0, HALF_OPEN: 1, OPEN: 2}

# Statuses that mean "the upstream is sick". 503 is deliberately absent: order-service
# returns 503 on purpose when one of ITS dependencies is down, and that must neither
# trip its own breaker nor be hidden from the client.
FAILURE_STATUSES = frozenset({500, 502, 504})


class UpstreamError(Exception):
    def __init__(self, status: int, detail: str, kind: str, retry_after: float | None = None):
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.kind = kind
        self.retry_after = retry_after


class Deadline:
    """A per-request time budget shared by every downstream call of that request."""

    def __init__(self, seconds: float, clock=time.monotonic):
        self._clock = clock
        self._end = clock() + seconds

    def remaining(self) -> float:
        return self._end - self._clock()


class CircuitBreaker:
    def __init__(self, name: str, failure_threshold: int = 5, reset_timeout: float = 10.0,
                 clock=time.monotonic):
        self.name = name
        self._threshold = failure_threshold
        self._reset_timeout = reset_timeout
        self._clock = clock
        self._lock = threading.Lock()
        self._state = CLOSED
        self._failures = 0
        self._opened_at = 0.0
        self._probing = False
        # Evaluated at scrape time, so the gauge also reflects a cool-down that expired on an
        # upstream that receives no traffic (nothing else would ever move it out of "open").
        CIRCUIT_STATE.labels(name).set_function(lambda: _STATE_VALUE[self.effective_state()])

    def effective_state(self) -> str:
        """What the next caller would observe. An open breaker whose cool-down has elapsed is
        half-open: the next request is admitted as the probe."""
        with self._lock:
            if self._state == OPEN and self._clock() - self._opened_at >= self._reset_timeout:
                return HALF_OPEN
            return self._state

    @property
    def state(self) -> str:
        return self.effective_state()

    def _trip(self) -> None:
        self._opened_at = self._clock()
        self._failures = 0
        self._probing = False
        self._state = OPEN

    def retry_after(self) -> float:
        with self._lock:
            if self._state != OPEN:
                return 0.0
            return max(0.0, self._reset_timeout - (self._clock() - self._opened_at))

    def allow(self) -> bool:
        with self._lock:
            if self._state == CLOSED:
                return True
            if self._state == OPEN:
                if self._clock() - self._opened_at < self._reset_timeout:
                    return False
                self._state = HALF_OPEN
                self._probing = True
                return True
            if self._probing:  # half-open: exactly one probe at a time
                return False
            self._probing = True
            return True

    def record_success(self) -> None:
        with self._lock:
            if self._state == OPEN:  # late result from a call started before the trip
                return
            self._failures = 0
            self._probing = False
            if self._state == HALF_OPEN:
                self._state = CLOSED

    def record_failure(self) -> None:
        with self._lock:
            if self._state == HALF_OPEN:
                self._trip()
            elif self._state == CLOSED:
                self._failures += 1
                if self._failures >= self._threshold:
                    self._trip()


class Upstream:
    def __init__(self, name: str, client: httpx.Client, breaker: CircuitBreaker):
        self.name = name
        self.client = client
        self.breaker = breaker

    def _fail(self, status: int, detail: str, kind: str) -> UpstreamError:
        self.breaker.record_failure()
        UPSTREAM_ERRORS.labels(self.name, kind).inc()
        return UpstreamError(status, detail, kind)

    def call(self, method: str, path: str, deadline: Deadline, *, json=None, params=None) -> httpx.Response:
        # Deadline is checked BEFORE allow(): allow() may hand out the half-open probe,
        # and a probe that is never sent would wedge the breaker.
        remaining = deadline.remaining()
        if remaining <= 0:
            UPSTREAM_ERRORS.labels(self.name, "deadline").inc()
            raise UpstreamError(504, "request deadline exceeded", "deadline")
        if not self.breaker.allow():
            CIRCUIT_REJECTIONS.labels(self.name).inc()
            raise UpstreamError(
                503, f"{self.name} circuit open", "circuit_open",
                retry_after=self.breaker.retry_after(),
            )
        try:
            resp = self.client.request(method, path, json=json, params=params, timeout=remaining)
        except httpx.TimeoutException as exc:
            raise self._fail(504, f"{self.name} timed out", "timeout") from exc
        except httpx.HTTPError as exc:
            raise self._fail(502, f"{self.name} unreachable", "unreachable") from exc
        except Exception as exc:
            raise self._fail(502, f"{self.name} call failed", "error") from exc
        if resp.status_code in FAILURE_STATUSES:
            raise self._fail(502, f"{self.name} returned {resp.status_code}", "bad_status")
        self.breaker.record_success()
        return resp