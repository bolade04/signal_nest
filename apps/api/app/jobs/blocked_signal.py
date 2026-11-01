"""The operator signal for blocked Scouts (FU-267-A1, PROPOSED; not a customer notification).

A Scout is blocked when it is in ``needs_attention``: an external operation it admitted has no
known end and is eligible for reconciliation (``app.scouting_requests.execution``, rule 1). It
stays blocked until an operator reconciles it (``python -m app.jobs.reconcile``); nothing in
SignalNest clears it on its own. This module turns that state into two gauges an operator's
monitoring can alarm on:

* ``scouts_blocked`` - how many Scouts are blocked, across every tenant;
* ``scouts_blocked_oldest_age_seconds`` - how long the longest-blocked one has been (0: none).

**Where it runs.** :func:`record_blocked_scouts` is called by the worker's registry heartbeat
(``Worker._start_registry_heartbeat``, every ``WORKER_HEARTBEAT_SECONDS``), the loop that
already sweeps stale workers: it needs no request and no customer traffic. Every worker sets the
same fleet-wide value, so a monitor reads the ``Maximum`` across workers.

**Cost.** One database clock reading and one statement per interval
(:func:`app.scouting_requests.execution.eligible_admissions`: the unresolved admissions through
the partial index ``uq_scout_admission_unresolved``, their Scout and job joined), preceded on
PostgreSQL by two ``set_config`` calls, and none at all while metrics are off (the no-op backend
is installed).

**Bounded wait (review r1 MINOR-2).** The read shares the registry heartbeat's loop, so a read
waiting on a lock (a migration's DDL on ``scout_admissions``, ``scout_requests`` or ``jobs``)
would delay the next beat. On PostgreSQL the read's own transaction therefore starts by
setting ``lock_timeout`` and ``statement_timeout`` with ``set_config(name, value, true)``, the
function form of ``SET LOCAL`` (transaction-scoped; the values are bound parameters, never
SQL text). :func:`read_timeouts_ms` sizes them: at most 2 s and 5 s, and at most a quarter of
the stale margin ``WORKER_STALE_AFTER_SECONDS - WORKER_HEARTBEAT_SECONDS`` (the time a late beat
has before peers flag the worker stale; 60 - 10 s by default, so 2 s and 5 s) for any margin of
at least 0.4 s. Below 0.4 s a 100 ms floor applies (:data:`MIN_TIMEOUT_MS`), so the bound can
then reach or exceed the margin. A read that hits either bound fails like any other: it is
rolled back, logged by class name (``worker.blocked_scouts_sweep_failed``), and sets no gauge for
that interval. The settings end with the transaction, so nothing leaks to the pooled connection.
The bound covers the wait inside the database; obtaining a connection from the pool is shared
with the heartbeat's own and is not bounded here. On SQLite nothing is set (the functions are
PostgreSQL's, and no read runs there).

**Same predicate.** The count is exactly the Scouts the ``reconcile blocked`` view lists and the
customer read shows as ``needs_attention``; nothing here re-states the rule.

**SQLite.** The admission ledger is kept on PostgreSQL only, so on SQLite nothing bounds
concurrent operations and nothing can be blocked. The gauges are then ABSENT (never set), not
zero: a zero would claim a bound that does not exist there.
"""

from __future__ import annotations

from collections.abc import Callable
from logging import WARNING

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.logging import get_logger, log_event
from app.core.metrics import (
    SCOUTS_BLOCKED,
    SCOUTS_BLOCKED_OLDEST_AGE_SECONDS,
    MetricsBackend,
    NoOpMetrics,
    get_metrics,
)
from app.scouting_requests.execution import BlockedScouts, blocked_scouts

logger = get_logger("signalnest.jobs.blocked_signal")

#: The read's ceilings on PostgreSQL (milliseconds): a lock wait, and any one statement.
LOCK_TIMEOUT_MS = 2_000
STATEMENT_TIMEOUT_MS = 5_000
#: The floor. It must be positive: PostgreSQL reads 0 as "no timeout", which would unbound the
#: read exactly when the margin is smallest. 100 ms is about the shortest bound a healthy read
#: of this size can meet; a smaller one would fail every sweep while bounding nothing that
#: matters (a margin under 0.4 s is far below any working heartbeat configuration). Below a
#: 0.4 s margin the floor exceeds a quarter of it, and at 0.1 s or less it reaches the margin.
MIN_TIMEOUT_MS = 100


def read_timeouts_ms(*, heartbeat_seconds: float, stale_after_seconds: float) -> tuple[int, int]:
    """``(lock_timeout, statement_timeout)`` in milliseconds for one sweep's read: at most
    :data:`LOCK_TIMEOUT_MS` / :data:`STATEMENT_TIMEOUT_MS`; the statement bound is a quarter of
    the margin ``stale_after - heartbeat`` (rounded down) but never below :data:`MIN_TIMEOUT_MS`,
    so it is at most a quarter of the margin for any margin of at least 0.4 s; the lock bound
    never exceeds the statement bound."""
    margin_ms = round(max(0.0, stale_after_seconds - heartbeat_seconds) * 1000.0)
    statement = max(MIN_TIMEOUT_MS, min(STATEMENT_TIMEOUT_MS, margin_ms // 4))
    return min(LOCK_TIMEOUT_MS, statement), statement


def set_blocked_gauges(metrics: MetricsBackend, summary: BlockedScouts) -> None:
    """Set both gauges from one summary (no label: only the backend's service/environment)."""
    metrics.set_gauge(SCOUTS_BLOCKED, float(summary.count))
    metrics.set_gauge(SCOUTS_BLOCKED_OLDEST_AGE_SECONDS, summary.oldest_age_seconds or 0.0)


def record_blocked_scouts(
    session_factory: Callable[[], Session],
    *,
    timeouts_ms: tuple[int, int] = (LOCK_TIMEOUT_MS, STATEMENT_TIMEOUT_MS),
) -> BlockedScouts | None:
    """One sweep: read the blocked Scouts in a short read-only transaction of its own and set
    the gauges. Returns the summary, or ``None`` when nothing was measured (metrics off, the
    ledger not kept, or the read failed or hit its bound). Never raises a database error: a
    failed read is logged (class name only) and retried on the next interval.

    ``timeouts_ms`` is ``(lock_timeout, statement_timeout)``, applied with
    ``set_config(..., true)`` (transaction-scoped, like ``SET LOCAL``) at the start of the
    read's transaction, on PostgreSQL only (the worker passes :func:`read_timeouts_ms` of its
    own settings)."""
    metrics = get_metrics()
    if isinstance(metrics, NoOpMetrics):
        return None  # metrics are off: nothing would be recorded, so nothing is read
    lock_ms, statement_ms = (int(value) for value in timeouts_ms)
    db = session_factory()
    try:
        if db.get_bind().dialect.name == "postgresql":
            # Bound the wait before the first read. set_config(name, value, is_local=true) is
            # SET LOCAL's function form: it lasts until this transaction ends.
            db.execute(select(func.set_config("lock_timeout", f"{lock_ms}ms", True)))
            db.execute(select(func.set_config("statement_timeout", f"{statement_ms}ms", True)))
        summary = blocked_scouts(db)
        db.rollback()  # read-only: end the transaction at once
    except Exception as exc:  # noqa: BLE001 - observation must never break the heartbeat
        try:
            db.rollback()
        except Exception:  # noqa: BLE001 - the connection may be gone; close() still runs
            pass
        log_event(
            logger,
            "worker.blocked_scouts_sweep_failed",
            level=WARNING,
            component="jobs",
            outcome="degraded",
            error_class=type(exc).__name__,
        )
        return None
    finally:
        db.close()
    if summary is not None:
        set_blocked_gauges(metrics, summary)
    return summary


__all__ = [
    "LOCK_TIMEOUT_MS",
    "STATEMENT_TIMEOUT_MS",
    "read_timeouts_ms",
    "record_blocked_scouts",
    "set_blocked_gauges",
]
