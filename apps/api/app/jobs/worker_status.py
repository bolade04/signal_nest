"""Worker-fleet lifecycle states and their explicit transition map.

A worker registration moves through a small, explicit state machine. Only the
transitions in :data:`_ALLOWED` are permitted; anything else raises
:class:`InvalidWorkerTransition`. This is intentionally separate from the *job*
lifecycle in :mod:`app.jobs.status`: a worker's health has no bearing on a job's
correctness, and job lease recovery is driven purely by lease expiry, never by a
worker's registry status.

Pure and dependency-free, so the transition rules are unit-testable in isolation.
"""

from __future__ import annotations

from enum import StrEnum


class WorkerStatus(StrEnum):
    """Lifecycle states for a worker registration.

    ``starting`` — registered but still running startup checks.
    ``ready`` — validated and idle, eligible to claim work.
    ``busy`` — currently executing at least one job.
    ``draining`` — finishing in-flight work during graceful shutdown.
    ``stopped`` — shut down cleanly (terminal).
    ``stale`` — no heartbeat within the stale threshold (may recover).
    ``failed`` — startup or a fatal loop error stopped the worker abnormally.
    """

    STARTING = "starting"
    READY = "ready"
    BUSY = "busy"
    DRAINING = "draining"
    STOPPED = "stopped"
    STALE = "stale"
    FAILED = "failed"


#: A stopped worker is done; nothing revives that registration (a restart writes a
#: fresh row). Every other state can still change.
TERMINAL_WORKER_STATUSES: frozenset[WorkerStatus] = frozenset({WorkerStatus.STOPPED})


_ALLOWED: dict[WorkerStatus, frozenset[WorkerStatus]] = {
    WorkerStatus.STARTING: frozenset(
        {WorkerStatus.READY, WorkerStatus.FAILED, WorkerStatus.STOPPED}
    ),
    WorkerStatus.READY: frozenset(
        {
            WorkerStatus.BUSY,
            WorkerStatus.DRAINING,
            WorkerStatus.STALE,
            WorkerStatus.STOPPED,
            WorkerStatus.FAILED,
        }
    ),
    WorkerStatus.BUSY: frozenset(
        {
            WorkerStatus.READY,
            WorkerStatus.DRAINING,
            WorkerStatus.STALE,
            WorkerStatus.STOPPED,
            WorkerStatus.FAILED,
        }
    ),
    WorkerStatus.DRAINING: frozenset(
        {WorkerStatus.STOPPED, WorkerStatus.STALE, WorkerStatus.FAILED}
    ),
    # A stale worker that heartbeats again recovers to ready/busy.
    WorkerStatus.STALE: frozenset(
        {WorkerStatus.READY, WorkerStatus.BUSY, WorkerStatus.STOPPED, WorkerStatus.FAILED}
    ),
    WorkerStatus.FAILED: frozenset({WorkerStatus.STOPPED}),
    WorkerStatus.STOPPED: frozenset(),
}


class InvalidWorkerTransition(Exception):
    """Raised when a worker status change is not permitted by the lifecycle."""

    def __init__(self, current: WorkerStatus, target: WorkerStatus) -> None:
        super().__init__(f"Illegal worker transition {current.value} -> {target.value}")
        self.current = current
        self.target = target


def is_worker_terminal(status: WorkerStatus) -> bool:
    return status in TERMINAL_WORKER_STATUSES


def can_worker_transition(current: WorkerStatus, target: WorkerStatus) -> bool:
    return target in _ALLOWED.get(current, frozenset())


def ensure_worker_transition(current: WorkerStatus, target: WorkerStatus) -> None:
    if not can_worker_transition(current, target):
        raise InvalidWorkerTransition(current, target)
