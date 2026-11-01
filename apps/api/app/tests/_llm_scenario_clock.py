"""A scenario clock for the LLM budget tests. Not itself a test module.

The ledger (:mod:`app.llm.budget`) keys a budget period by the UTC calendar month of the
instant it is given. The meter (:mod:`app.llm.metering`) takes that instant from its own
module clock, ``datetime.now(UTC)``, when it reserves, settles, releases at rebind and
writes at flush; ``reserved_month_to_date_usd`` (and through it ``month_to_date_usd`` and
the run gate) does the same when it is given no instant. A test that sets up or asserts
at an explicit instant must run those paths at the same instant, or its period lookups
depend on the calendar date of the run.

:class:`ScenarioClock` starts at an explicit instant and advances with
``time.monotonic()``, so instants read one after another stay ordered (row
``created_at`` order, expiries) without the calendar being read. It does not cross a
month by elapsing in practice (scenarios start mid-month or name their boundary); a test
that needs a later instant moves it explicitly with :meth:`ScenarioClock.move_to`.

:meth:`ScenarioClock.installed` points exactly two module clocks at the scenario - the
``datetime`` names of :mod:`app.llm.metering` and :mod:`app.llm.budget`, through which
those modules only call ``datetime.now(UTC)`` - and restores them on exit. Nothing else
is touched: other modules' clocks (``app.db.base.utcnow`` for row timestamps, the job
store's lease clock), the database server's clock and ``time.monotonic`` keep running as
they are. Code that is handed an instant (the ledger primitives, ``JobRunner(clock=...)``)
is given one explicitly by the test.

A child process started with the ``spawn`` method does not inherit a patch made in its
parent: it rebuilds the parent's clock from :meth:`ScenarioClock.spec` (the anchor
instant and the monotonic reading taken with it) and installs it itself. The monotonic
clock is system-wide (``CLOCK_MONOTONIC`` on Linux, ``mach_absolute_time`` on macOS), so
a parent and its children read the same scenario instant. A later :meth:`move_to` in
the parent does not reach children already started.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta, tzinfo

import app.llm.budget as _budget
import app.llm.metering as _metering

#: The modules whose ``datetime.now`` is the accounting clock, and only those.
CLOCK_MODULES = (_metering, _budget)

#: What :meth:`ScenarioClock.spec` returns: (anchor instant in ISO 8601, monotonic reading).
ClockSpec = tuple[str, float]


class ScenarioClock:
    """An explicit instant that advances with the monotonic clock."""

    def __init__(self, start: datetime, *, reference: float | None = None) -> None:
        self._start = _aware_utc(start)
        self._reference = time.monotonic() if reference is None else reference

    def now(self) -> datetime:
        """The scenario's current instant (UTC)."""
        return self._start + timedelta(seconds=time.monotonic() - self._reference)

    def move_to(self, instant: datetime) -> None:
        """Continue the scenario from ``instant`` (e.g. across a month boundary)."""
        self._start = _aware_utc(instant)
        self._reference = time.monotonic()

    def spec(self) -> ClockSpec:
        """A picklable description a spawned child rebuilds this clock from."""
        return (self._start.isoformat(), self._reference)

    @classmethod
    def from_spec(cls, spec: ClockSpec) -> ScenarioClock:
        start, reference = spec
        return cls(datetime.fromisoformat(start), reference=reference)

    @contextmanager
    def installed(self) -> Iterator[ScenarioClock]:
        """Run the accounting clock (:data:`CLOCK_MODULES`) on this scenario, then restore it."""
        scenario = self

        class _ScenarioDatetime(datetime):
            @classmethod
            def now(cls, tz: tzinfo | None = None) -> datetime:  # type: ignore[override]
                instant = scenario.now()
                if tz is None:  # like datetime.now(): naive local time
                    return instant.astimezone().replace(tzinfo=None)
                return instant.astimezone(tz)

        saved = [(module, module.datetime) for module in CLOCK_MODULES]
        for module, _original in saved:
            module.datetime = _ScenarioDatetime
        try:
            yield self
        finally:
            for module, original in saved:
                module.datetime = original


def _aware_utc(instant: datetime) -> datetime:
    if instant.tzinfo is None:
        raise ValueError("a scenario instant must be timezone-aware")
    return instant.astimezone(UTC)
