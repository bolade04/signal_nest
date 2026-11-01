"""A Scout run's customer-facing outcome (W16 customer repairs §D, PROPOSED; a proposed Q1
presentation, not a Q1 ruling).

Derived read-only from what a ``scout_request.execute`` job already records: its ``status``, its
``result_summary.outcome`` (and, for a failure to admit an external operation, its
``result_summary.admission_wait``; W21 FU-Q1-2, PROPOSED), its ``last_error_code`` and an
allow-list of ``last_error_summary`` codes. The summary itself is never exposed; only the closed
codes below are. Job lifecycle states keep their meaning: a ``succeeded`` job whose run was
stopped or skipped is still ``succeeded``, but its outcome is never ``completed``.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from app.jobs.admission import ADMISSION_WAIT_UNTIL_DEADLINE
from app.jobs.status import JobErrorCode, JobStatus, JobType


class RunOutcome(StrEnum):
    """How a finished Scout run ended (null while it is in flight, and for other job types)."""

    COMPLETED = "completed"
    STOPPED_PAUSED = "stopped_paused"
    SKIPPED_PAUSED = "skipped_paused"
    #: The run never started for a reason other than a pause (another run owned the Scout).
    SKIPPED = "skipped"
    STOPPED = "stopped"
    CANCELLED = "cancelled"
    FAILED = "failed"


class RunOutcomeReason(StrEnum):
    """Why, in a closed customer-safe code (null where the outcome says it all)."""

    PAUSED_DURING_RUN = "paused_during_run"
    ANOTHER_RUN_ACTIVE = "another_run_active"
    ANOTHER_RUN_TOOK_OVER = "another_run_took_over"
    WORKER_LOST = "worker_lost"
    SCOUT_CHANGED = "scout_changed"
    SCOUT_REMOVED = "scout_removed"
    #: The run waited for an earlier operation, and that operation's deadline passed while it did
    #: (the job recorded ``admission_wait: until_deadline``).
    EARLIER_OPERATION_TIMED_OUT = "earlier_operation_timed_out"
    #: An earlier operation had no confirmed outcome, so the run could not go on (W21 FU-Q1-2,
    #: PROPOSED). Says nothing about a wait: it is every ``admission_unresolved`` failure that did
    #: not record a wait that ended at the deadline - an immediate refusal (nothing waited), a wait
    #: that ended with the earlier operation's unknown end, and a failure recorded before the wait
    #: was recorded.
    EARLIER_OPERATION_UNRESOLVED = "earlier_operation_unresolved"
    TOO_MANY_SIGNALS = "too_many_signals"
    RETRIES_EXHAUSTED = "retries_exhausted"
    WORKER_STOPPED_RESPONDING = "worker_stopped_responding"
    INTERNAL = "internal"


_O, _R = RunOutcome, RunOutcomeReason

#: ``result_summary.outcome`` of a ``succeeded`` job -> (outcome, reason). A missing outcome is a
#: run recorded before outcomes existed: it always ran to completion.
_SUCCEEDED: dict[str | None, tuple[RunOutcome, RunOutcomeReason | None]] = {
    None: (_O.COMPLETED, None),
    "completed": (_O.COMPLETED, None),
    # Legacy path only (no admission: replay seed, direct calls): the findings were kept.
    "paused_during_run": (_O.COMPLETED, _R.PAUSED_DURING_RUN),
    "stopped_request_paused": (_O.STOPPED_PAUSED, None),
    "skipped_request_paused": (_O.SKIPPED_PAUSED, None),
    "skipped_owner_active": (_O.SKIPPED, _R.ANOTHER_RUN_ACTIVE),
    "stopped_owner_superseded": (_O.STOPPED, _R.ANOTHER_RUN_TOOK_OVER),
    "stopped_lease_lost": (_O.STOPPED, _R.WORKER_LOST),
    "status_changed_during_run": (_O.STOPPED, _R.SCOUT_CHANGED),
    "request_gone": (_O.STOPPED, _R.SCOUT_REMOVED),
}

#: The ``last_error_summary`` of a run whose external operation could not be admitted (wait
#: expiry, D-25: accepted as a failed run). Its reason depends on the recorded wait, below.
_ADMISSION_UNRESOLVED = "admission_unresolved"

#: ``result_summary.admission_wait`` (``app.jobs.admission.ADMISSION_WAIT_*``, recorded by the
#: scout handler on that failure; W21 FU-Q1-2, PROPOSED) that alone means "waited until the
#: deadline". Anything else - ``none`` (nothing waited), ``until_unknown_end``, or no record - is
#: never worded as a wait that timed out.
_WAITED_UNTIL_DEADLINE = ADMISSION_WAIT_UNTIL_DEADLINE

#: Allow-listed ``last_error_summary`` codes of a final failure -> reason.
_FAILURE_SUMMARIES: dict[str, RunOutcomeReason] = {
    _ADMISSION_UNRESOLVED: _R.EARLIER_OPERATION_UNRESOLVED,
    "signal_volume_above_bound": _R.TOO_MANY_SIGNALS,
    "claim_identity_incomplete": _R.INTERNAL,
    "after_rollback_write_failed": _R.INTERNAL,
}

#: Error codes that can only mean a defect or a malformed job, never the customer's doing.
_INTERNAL_CODES = frozenset(
    code.value
    for code in (
        JobErrorCode.VALIDATION,
        JobErrorCode.AUTHORIZATION,
        JobErrorCode.TENANT_ISOLATION,
        JobErrorCode.UNSUPPORTED_VERSION,
        JobErrorCode.UNSUPPORTED_TYPE,
        JobErrorCode.IDEMPOTENCY_CONFLICT,
        JobErrorCode.PAYLOAD_TOO_LARGE,
        JobErrorCode.POLICY,
    )
)


_NON_RETRYABLE = JobErrorCode.NON_RETRYABLE.value


def run_outcome(
    *,
    job_type: str | None,
    status: str | None,
    result_summary: Any,
    last_error_code: str | None,
    last_error_summary: str | None,
) -> tuple[RunOutcome | None, RunOutcomeReason | None]:
    """The run's ``(outcome, reason)``; ``(None, None)`` in flight or for another job type."""
    if job_type != JobType.SCOUT_REQUEST_EXECUTE.value:
        return None, None
    if status == JobStatus.CANCELLED.value:
        return _O.CANCELLED, None
    if status == JobStatus.SUCCEEDED.value:
        summary = result_summary if isinstance(result_summary, dict) else {}
        recorded = summary.get("outcome")
        if recorded is not None and not isinstance(recorded, str):
            recorded = str(recorded)
        # An outcome this table does not know is never claimed as completed.
        return _SUCCEEDED.get(recorded, (_O.STOPPED, _R.INTERNAL))
    if status in (JobStatus.FAILED.value, JobStatus.DEAD_LETTERED.value):
        if last_error_summary == _ADMISSION_UNRESOLVED:
            summary = result_summary if isinstance(result_summary, dict) else {}
            if summary.get("admission_wait") == _WAITED_UNTIL_DEADLINE:
                return _O.FAILED, _R.EARLIER_OPERATION_TIMED_OUT
        if last_error_summary in _FAILURE_SUMMARIES:
            return _O.FAILED, _FAILURE_SUMMARIES[last_error_summary]
        if status == JobStatus.DEAD_LETTERED.value:
            if last_error_code == JobErrorCode.TIMEOUT.value:
                return _O.FAILED, _R.WORKER_STOPPED_RESPONDING
            if last_error_code == JobErrorCode.TRANSIENT.value:
                return _O.FAILED, _R.RETRIES_EXHAUSTED
        if last_error_code in _INTERNAL_CODES or last_error_code == _NON_RETRYABLE:
            return _O.FAILED, _R.INTERNAL
        return _O.FAILED, None
    return None, None


def outcome_of(job: Any) -> tuple[RunOutcome | None, RunOutcomeReason | None]:
    """:func:`run_outcome` of a ``Job`` row."""
    return run_outcome(
        job_type=getattr(job, "job_type", None),
        status=getattr(job, "status", None),
        result_summary=getattr(job, "result_summary", None),
        last_error_code=getattr(job, "last_error_code", None),
        last_error_summary=getattr(job, "last_error_summary", None),
    )


__all__ = ["RunOutcome", "RunOutcomeReason", "outcome_of", "run_outcome"]
