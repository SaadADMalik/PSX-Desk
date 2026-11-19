"""Circuit breaker per model.

After N consecutive failures a model is skipped ("open") for a cooldown, so we
stop paying timeouts against something that is down. After the cooldown one
trial request is let through ("half-open"); success closes it, failure re-opens.

State is in-process. With one gateway replica that is enough; with several, each
replica learns independently, which is acceptable because the failure signal is
fast to rediscover. Move it to Redis if that ever stops being true.
"""

import time
from dataclasses import dataclass, field
from enum import StrEnum


class BreakerState(StrEnum):
    closed = "closed"
    open = "open"
    half_open = "half_open"


@dataclass
class _Entry:
    failures: int = 0
    opened_at: float = 0.0
    state: BreakerState = BreakerState.closed


@dataclass
class CircuitBreaker:
    failure_threshold: int = 3
    cooldown_s: float = 60.0
    clock: callable = time.monotonic
    _models: dict[str, _Entry] = field(default_factory=dict)

    def _e(self, model: str) -> _Entry:
        return self._models.setdefault(model, _Entry())

    def allow(self, model: str) -> bool:
        e = self._e(model)
        if e.state is BreakerState.open:
            if self.clock() - e.opened_at >= self.cooldown_s:
                e.state = BreakerState.half_open
                return True
            return False
        return True

    def success(self, model: str) -> None:
        e = self._e(model)
        e.failures, e.state = 0, BreakerState.closed

    def failure(self, model: str) -> None:
        e = self._e(model)
        e.failures += 1
        if e.state is BreakerState.half_open or e.failures >= self.failure_threshold:
            e.state, e.opened_at = BreakerState.open, self.clock()

    def state(self, model: str) -> BreakerState:
        return self._e(model).state
