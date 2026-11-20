from prometheus_client import generate_latest

from app.resilience import CircuitBreaker


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def gauge(name: str) -> float:
    prefix = f'gateway_circuit_state{{upstream="{name}"}}'
    line = next(l for l in generate_latest().decode().splitlines() if l.startswith(prefix))
    return float(line.split()[-1])


def test_idle_open_breaker_is_reported_half_open_once_the_cooldown_has_elapsed():
    clock = Clock()
    cb = CircuitBreaker("idle-upstream", 3, 10.0, clock)
    for _ in range(3):
        cb.record_failure()
    assert cb.state == "open" and gauge("idle-upstream") == 2.0

    clock.advance(9.9)
    assert cb.state == "open"
    clock.advance(0.2)  # cool-down over, but no traffic has arrived to move the breaker
    assert cb.state == "half_open" and gauge("idle-upstream") == 1.0

    assert cb.allow()  # the next request really is admitted as the probe
    cb.record_success()
    assert cb.state == "closed" and gauge("idle-upstream") == 0.0