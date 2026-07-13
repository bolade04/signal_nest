"""Bounded exponential backoff for job retries.

Pure and deterministic: given the same inputs (including an explicit jitter
seed) it always returns the same delay, so tests can assert exact schedules
while production still gets spread-out retries.

The delay for attempt *n* (1-based, the attempt that just failed) is::

    raw   = base * 2 ** (n - 1)
    delay = min(cap, raw)

Optional *jitter* subtracts up to ``jitter_fraction`` of the delay using a
seeded PRNG, which de-synchronizes a thundering herd of jobs that failed at the
same instant without ever exceeding the computed delay.
"""

from __future__ import annotations

import random

#: Default share of the delay that jitter may remove (full-jitter is 1.0; we use
#: a partial jitter so retries never collapse to ~0s under load).
DEFAULT_JITTER_FRACTION = 0.25


def compute_backoff_seconds(
    attempt: int,
    *,
    base_seconds: float,
    max_seconds: float,
    jitter_seed: str | None = None,
    jitter_fraction: float = DEFAULT_JITTER_FRACTION,
) -> float:
    """Return the retry delay in seconds for a just-failed ``attempt``.

    ``attempt`` is clamped to at least 1. The result is always in
    ``[0, max_seconds]`` and is bounded even for large attempt counts (the
    exponent is capped so ``2 ** n`` cannot overflow).
    """
    n = max(1, int(attempt))
    # Cap the exponent so the intermediate never explodes for pathological
    # attempt counts; 63 doublings already dwarfs any sane max_seconds.
    exponent = min(n - 1, 63)
    raw = base_seconds * float(2**exponent)
    delay = min(max_seconds, raw)
    if delay <= 0:
        return 0.0

    if jitter_seed is not None and jitter_fraction > 0:
        rng = random.Random(f"{jitter_seed}:{n}")
        reduction = delay * jitter_fraction * rng.random()
        delay = max(0.0, delay - reduction)
    return delay
