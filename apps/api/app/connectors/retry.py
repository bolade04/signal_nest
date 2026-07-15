"""Bounded retry with classified backoff for connector fetches.

Wraps a single source fetch so a transient fault (timeout, network blip, rate
limit, upstream 5xx) is retried a bounded number of times with exponential
backoff, while a permanent fault (parse error, unsafe content, not configured)
fails immediately. Backoff is *computed*, not slept, so the policy is fully
unit-testable; an optional ``sleep`` hook lets a live caller actually wait.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

from app.connectors.base import FailureKind, FetchFailure

T = TypeVar("T")


class ConnectorFetchError(Exception):
    """Raised by a fetch attempt, carrying a classified :class:`FetchFailure`."""

    def __init__(self, kind: FailureKind, detail: str) -> None:
        super().__init__(detail)
        self.failure = FetchFailure(kind=kind, detail=detail)


@dataclass(frozen=True)
class RetryPolicy:
    """Bounded exponential backoff. ``max_attempts`` includes the first try."""

    max_attempts: int = 3
    base_delay_seconds: float = 0.5
    max_delay_seconds: float = 8.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if self.base_delay_seconds <= 0:
            raise ValueError("base_delay_seconds must be > 0")
        if self.max_delay_seconds < self.base_delay_seconds:
            raise ValueError("max_delay_seconds must be >= base_delay_seconds")

    def delay_for(self, attempt: int) -> float:
        """Backoff before ``attempt`` (1-based); attempt 1 never waits."""
        if attempt <= 1:
            return 0.0
        raw = self.base_delay_seconds * (2 ** (attempt - 2))
        return min(raw, self.max_delay_seconds)


def run_with_retry(
    fetch: Callable[[], T],
    *,
    policy: RetryPolicy,
    sleep: Callable[[float], None] | None = None,
) -> T:
    """Call ``fetch`` with bounded, classified retries.

    Retries only :class:`ConnectorFetchError` whose kind is retryable; a
    non-retryable kind (or exhausted attempts) re-raises the last error so the
    caller can record the classified failure. ``sleep`` is invoked with the
    computed backoff between attempts when provided (no-op by default).
    """
    last: ConnectorFetchError | None = None
    for attempt in range(1, policy.max_attempts + 1):
        try:
            return fetch()
        except ConnectorFetchError as exc:
            last = exc
            if not exc.failure.retryable or attempt == policy.max_attempts:
                raise
            if sleep is not None:
                sleep(policy.delay_for(attempt + 1))
    assert last is not None  # unreachable: loop either returns or raises
    raise last
