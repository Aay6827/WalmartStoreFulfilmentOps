import pytest

from app.resilience import CircuitBreaker, Deadline


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def make(clock, threshold=3, reset=10.0):
    return CircuitBreaker("unit", threshold, reset, clock)


def test_opens_after_consecutive_failures():
    cb = make(Clock())
    cb.record_failure()
    cb.record_failure()
    assert cb.state == "closed" and cb.allow()
    cb.record_failure()
    assert cb.state == "open"
    assert not cb.allow()


def test_success_resets_the_failure_count():
    cb = make(Clock())
    for outcome in ("f", "f", "s", "f", "f"):
        cb.record_failure() if outcome == "f" else cb.record_success()
    assert cb.state == "closed"
    cb.record_failure()
    assert cb.state == "open"


def test_half_open_allows_a_single_probe_and_closes_on_success():
    clock = Clock()
    cb = make(clock)
    for _ in range(3):
        cb.record_failure()
    clock.advance(10)
    assert cb.allow()
    assert cb.state == "half_open"
    assert not cb.allow()  # probe already in flight
    cb.record_success()
    assert cb.state == "closed"
    assert cb.allow()


def test_failed_probe_reopens_for_a_full_cooldown():
    clock = Clock()
    cb = make(clock)
    for _ in range(3):
        cb.record_failure()
    clock.advance(10)
    assert cb.allow()
    cb.record_failure()
    assert cb.state == "open"
    clock.advance(9.9)
    assert not cb.allow()
    clock.advance(0.2)
    assert cb.allow()


def test_late_results_while_open_are_ignored():
    cb = make(Clock())
    for _ in range(3):
        cb.record_failure()
    cb.record_success()
    assert cb.state == "open"


def test_retry_after_counts_down():
    clock = Clock()
    cb = make(clock)
    assert cb.retry_after() == 0.0
    for _ in range(3):
        cb.record_failure()
    assert cb.retry_after() == pytest.approx(10.0)
    clock.advance(4)
    assert cb.retry_after() == pytest.approx(6.0)


def test_deadline_remaining():
    clock = Clock()
    d = Deadline(1.0, clock)
    assert d.remaining() == pytest.approx(1.0)
    clock.advance(0.4)
    assert d.remaining() == pytest.approx(0.6)
    clock.advance(0.7)
    assert d.remaining() < 0