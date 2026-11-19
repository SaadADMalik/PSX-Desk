from llm_gateway.breaker import BreakerState, CircuitBreaker


def test_opens_after_threshold_then_half_opens_after_cooldown(clock):
    b = CircuitBreaker(failure_threshold=2, cooldown_s=30, clock=clock)
    b.failure("m")
    assert b.allow("m")
    b.failure("m")
    assert b.state("m") is BreakerState.open and not b.allow("m")
    clock.now += 31
    assert b.allow("m") and b.state("m") is BreakerState.half_open


def test_half_open_failure_reopens_and_success_closes(clock):
    b = CircuitBreaker(failure_threshold=1, cooldown_s=10, clock=clock)
    b.failure("m")
    clock.now += 11
    b.allow("m")
    b.failure("m")
    assert b.state("m") is BreakerState.open
    clock.now += 11
    b.allow("m")
    b.success("m")
    assert b.state("m") is BreakerState.closed
