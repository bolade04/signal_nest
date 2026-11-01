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
the partial index ``uq_scout_admission_unresolved``, their Scout and job joined), and none at
all while metrics are off (the no-op backend is installed).

**Same predicate.** The count is exactly the Scouts the ``reconcile blocked`` view lists and the
customer read shows as ``needs_attention``; nothing here re-states the rule.

**SQLite.** The admission ledger is kept on PostgreSQL only, so on SQLite nothing bounds
concurrent operations and nothing can be blocked. The gauges are then ABSENT (never set), not
zero: a zero would claim a bound that does not exist there.
"""

from __future__ import annotations

from collections.abc import Callable
from logging import WARNING

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


def set_blocked_gauges(metrics: MetricsBackend, summary: BlockedScouts) -> None:
    """Set both gauges from one summary (no label: only the backend's service/environment)."""
    metrics.set_gauge(SCOUTS_BLOCKED, float(summary.count))
    metrics.set_gauge(SCOUTS_BLOCKED_OLDEST_AGE_SECONDS, summary.oldest_age_seconds or 0.0)


def record_blocked_scouts(session_factory: Callable[[], Session]) -> BlockedScouts | None:
    """One sweep: read the blocked Scouts in a short read-only transaction of its own and set
    the gauges. Returns the summary, or ``None`` when nothing was measured (metrics off, the
    ledger not kept, or the read failed). Never raises a database error: a failed read is
    logged (class name only) and retried on the next interval."""
    metrics = get_metrics()
    if isinstance(metrics, NoOpMetrics):
        return None  # metrics are off: nothing would be recorded, so nothing is read
    db = session_factory()
    try:
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


__all__ = ["record_blocked_scouts", "set_blocked_gauges"]
