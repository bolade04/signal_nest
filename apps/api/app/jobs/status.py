"""Durable job status model and error taxonomy.

This module is the single source of truth for:

* the job lifecycle (:class:`JobStatus`) and the **explicit** set of allowed
  transitions between states — invalid transitions are rejected, and terminal
  jobs never silently revert (a controlled replay must go through an audited
  path in the store, not a bare status write);
* the constrained set of job *types* the platform knows how to execute
  (:class:`JobType`); and
* the error taxonomy (:class:`JobErrorCode`) that decides whether a failed
  attempt may be retried.

Delivery semantics are **at-least-once with idempotency controls**, never
exactly-once: a job may run more than once (e.g. a worker dies after doing work
but before recording success), so handlers must be safe to re-run.

Nothing here touches the database, the network, or secrets; it is pure and
unit-testable in isolation.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class JobStatus(StrEnum):
    """Lifecycle states for a durable job.

    ``scheduled`` is the initial state of a future-dated job; ``pending`` is a
    job that is due and eligible to be claimed. ``retry_wait`` is a job that
    failed a retryable attempt and is waiting out its backoff before becoming
    eligible again.
    """

    PENDING = "pending"
    SCHEDULED = "scheduled"
    CLAIMED = "claimed"
    RUNNING = "running"
    RETRY_WAIT = "retry_wait"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DEAD_LETTERED = "dead_lettered"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"


#: Terminal states. A job in a terminal state is done; it must never move to a
#: non-terminal state except through an explicit, audited replay in the store.
TERMINAL_STATUSES: frozenset[JobStatus] = frozenset(
    {
        JobStatus.SUCCEEDED,
        JobStatus.FAILED,
        JobStatus.DEAD_LETTERED,
        JobStatus.CANCELLED,
    }
)

#: States in which a job is not yet finished and not yet claimed for execution.
ENQUEUED_STATUSES: frozenset[JobStatus] = frozenset(
    {JobStatus.PENDING, JobStatus.SCHEDULED, JobStatus.RETRY_WAIT}
)

#: States in which a worker currently holds (or is meant to hold) a lease.
ACTIVE_STATUSES: frozenset[JobStatus] = frozenset(
    {JobStatus.CLAIMED, JobStatus.RUNNING, JobStatus.CANCEL_REQUESTED}
)


# Explicit adjacency map. Only these transitions are permitted; anything else is
# rejected by :func:`ensure_transition`. Lease-recovery moves an abandoned
# CLAIMED/RUNNING job back to PENDING/RETRY_WAIT (an audited replay).
_ALLOWED: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.SCHEDULED: frozenset(
        {JobStatus.PENDING, JobStatus.CLAIMED, JobStatus.CANCEL_REQUESTED, JobStatus.CANCELLED}
    ),
    JobStatus.PENDING: frozenset(
        {JobStatus.CLAIMED, JobStatus.CANCEL_REQUESTED, JobStatus.CANCELLED}
    ),
    JobStatus.RETRY_WAIT: frozenset(
        {JobStatus.PENDING, JobStatus.CLAIMED, JobStatus.CANCEL_REQUESTED, JobStatus.CANCELLED}
    ),
    JobStatus.CLAIMED: frozenset(
        {
            JobStatus.RUNNING,
            JobStatus.RETRY_WAIT,
            JobStatus.FAILED,
            JobStatus.DEAD_LETTERED,
            JobStatus.CANCEL_REQUESTED,
            JobStatus.CANCELLED,
            JobStatus.PENDING,  # lease recovery
        }
    ),
    JobStatus.RUNNING: frozenset(
        {
            JobStatus.SUCCEEDED,
            JobStatus.FAILED,
            JobStatus.RETRY_WAIT,
            JobStatus.DEAD_LETTERED,
            JobStatus.CANCEL_REQUESTED,
            JobStatus.CANCELLED,
            JobStatus.PENDING,  # lease recovery
        }
    ),
    JobStatus.CANCEL_REQUESTED: frozenset(
        {
            JobStatus.CANCELLED,
            JobStatus.RUNNING,  # observed mid-flight; finishes then cancels
            JobStatus.SUCCEEDED,  # completed before the cancel was observed
            JobStatus.FAILED,
            JobStatus.DEAD_LETTERED,
            JobStatus.RETRY_WAIT,
        }
    ),
    JobStatus.SUCCEEDED: frozenset(),
    JobStatus.FAILED: frozenset(),
    JobStatus.DEAD_LETTERED: frozenset(),
    JobStatus.CANCELLED: frozenset(),
}


class InvalidJobTransition(Exception):
    """Raised when a status change is not permitted by the lifecycle."""

    def __init__(self, current: JobStatus, target: JobStatus) -> None:
        super().__init__(f"Illegal job transition {current.value} -> {target.value}")
        self.current = current
        self.target = target


def is_terminal(status: JobStatus) -> bool:
    return status in TERMINAL_STATUSES


def can_transition(current: JobStatus, target: JobStatus) -> bool:
    return target in _ALLOWED.get(current, frozenset())


def ensure_transition(current: JobStatus, target: JobStatus) -> None:
    """Validate a transition, raising :class:`InvalidJobTransition` if illegal."""
    if not can_transition(current, target):
        raise InvalidJobTransition(current, target)


class JobType(StrEnum):
    """The constrained set of job types the platform can execute.

    Enqueueing or executing any other type is rejected (a stable, non-retryable
    error), so an unknown/renamed type can never silently no-op.
    """

    SCOUT_REQUEST_EXECUTE = "scout_request.execute"
    #: Recurring schedule tick (SB-B). Self-chaining: each tick may enqueue one
    #: scout-request execution and always enqueues its own successor tick while the
    #: schedule stays enabled. Never runs the scouting pipeline itself.
    SCOUT_SCHEDULE_TICK = "scout_schedule.tick"


class JobErrorCode(StrEnum):
    """Stable, secret-free error codes that drive the retry decision."""

    # Retryable: a later attempt might succeed.
    TRANSIENT = "transient"
    TIMEOUT = "timeout"

    # Non-retryable: retrying cannot help (bad input, policy, or a permanent
    # condition). These fail fast or dead-letter rather than burning attempts.
    VALIDATION = "validation"
    AUTHORIZATION = "authorization"
    TENANT_ISOLATION = "tenant_isolation"
    UNSUPPORTED_VERSION = "unsupported_version"
    UNSUPPORTED_TYPE = "unsupported_type"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    PAYLOAD_TOO_LARGE = "payload_too_large"
    POLICY = "policy"
    CANCELLED = "cancelled"
    NON_RETRYABLE = "non_retryable"


#: The only error codes for which another attempt is worthwhile.
RETRYABLE_ERROR_CODES: frozenset[JobErrorCode] = frozenset(
    {JobErrorCode.TRANSIENT, JobErrorCode.TIMEOUT}
)

#: Upper bound on the stored error summary, so a verbose message can never bloat
#: the row or smuggle large customer content into audit records.
MAX_ERROR_SUMMARY_LEN = 500


def is_retryable(code: JobErrorCode) -> bool:
    return code in RETRYABLE_ERROR_CODES


@dataclass(frozen=True)
class JobError:
    """A classified job failure.

    ``summary`` is a short, safe, human-readable description. It must never carry
    secrets, credentials, raw payloads, full customer content, or raw stack
    traces — the store persists it verbatim and it may reach operator surfaces.
    """

    code: JobErrorCode
    summary: str = ""

    @property
    def retryable(self) -> bool:
        return is_retryable(self.code)

    def safe_summary(self) -> str:
        text = " ".join((self.summary or self.code.value).split())
        return text[:MAX_ERROR_SUMMARY_LEN]


class JobExecutionError(Exception):
    """Raised by a handler to fail an attempt with an explicit classification.

    Any other exception escaping a handler is treated as an unclassified
    :data:`JobErrorCode.TRANSIENT` failure (retryable) — the conservative default
    — with only the exception *class name* recorded, never its message.
    """

    def __init__(self, code: JobErrorCode, summary: str = "") -> None:
        self.error = JobError(code=code, summary=summary)
        super().__init__(f"{code.value}: {self.error.safe_summary()}")
