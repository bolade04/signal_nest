"""A small, pure token-bucket rate limiter for connector egress.

Every connector must rate-limit its calls to a source (``docs/phase-3-plan.md``
Workstream A). This limiter is deterministic and clock-injectable so it is fully
unit-testable without real time: it never sleeps, it only answers "may I make a
call now?" and reports how long to wait otherwise. The caller (the retry wrapper
in sandbox tests, a real scheduler in live mode) decides how to honour the wait.
"""

from __future__ import annotations

from collections.abc import Callable


class TokenBucket:
    """Refilling token bucket.

    ``capacity`` tokens, refilled at ``refill_per_second``. :meth:`try_acquire`
    consumes one token if available and returns ``True``; otherwise it returns
    ``False`` without consuming. Time comes from an injected monotonic clock so
    tests advance it explicitly.
    """

    def __init__(
        self,
        *,
        capacity: int,
        refill_per_second: float,
        clock: Callable[[], float],
    ) -> None:
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        if refill_per_second <= 0:
            raise ValueError("refill_per_second must be > 0")
        self._capacity = capacity
        self._refill = refill_per_second
        self._clock = clock
        self._tokens = float(capacity)
        self._updated = clock()

    def _replenish(self) -> None:
        now = self._clock()
        elapsed = now - self._updated
        if elapsed > 0:
            self._tokens = min(self._capacity, self._tokens + elapsed * self._refill)
            self._updated = now

    def try_acquire(self) -> bool:
        self._replenish()
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return True
        return False

    def retry_after_seconds(self) -> float:
        """Seconds until at least one token is available (0 if one is ready)."""
        self._replenish()
        if self._tokens >= 1.0:
            return 0.0
        return (1.0 - self._tokens) / self._refill
