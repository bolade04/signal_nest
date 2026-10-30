"""A Scout's derived execution state (D-25-RECOVERY-CONTRACT-01 §4 / §9.4, PROPOSED).

A read-only presentation of committed rows and the database clock: nothing here writes, and
nothing is stored. It answers one question for the customer-safe Scout read model
(``ScoutRequestOut.execution``), for the Run-now refusal and for the operator's
``python -m app.jobs.reconcile blocked`` view: can this Scout run, and if not, why not?

For a Scout ``R`` at database time ``now``, with its unresolved admissions ``U`` (K = 1: at
most one on PostgreSQL; the ledger is never written on SQLite, so ``U`` is empty there):

1. ``needs_attention`` if some ``a`` in ``U`` is ELIGIBLE for reconciliation: its admitting
   attempt holds no valid authority and either its recorded end is ``unknown``, or it has no
   recorded end and its persisted deadline has passed (``now > deadline_at``, strict), or it
   has no recorded end and no recorded deadline (a legacy row). Whatever ``R``'s status:
   Resume and Run now cannot restore service until reconciliation.
2. else ``paused`` if ``R`` is paused;
3. else ``in_progress`` if the Scout has a live execute job not fenced out by a pause
   (whatever ``R``'s status, so a pending scheduled run is not ``idle``), or ``U`` is not
   empty (an operation may still be in its call: never ``needs_attention`` while healthy);
4. else ``stopped`` if ``R`` is ``queued``/``running`` with no live job (``run_interrupted``,
   only support can help: Run now refuses those statuses), or ``R`` is ``failed``, or the
   latest run was cancelled or failed;
5. else ``idle``.

The reason and next action are closed codes; no id, worker, token, attempt number, admission
id, payload, URL, setting or error text is exposed. ``since`` is set only where a stable
timestamp exists. Every read is scoped by the Scout's own organization and workspace.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.enums import ScoutRequestStatus
from app.jobs.admission import LIVE_JOB_STATUSES
from app.jobs.models import Job
from app.jobs.status import JobType
from app.scouting_requests.models import ScoutAdmission, ScoutRequest
from app.scouting_requests.schemas import (
    ScoutExecutionNextAction as Next,
)
from app.scouting_requests.schemas import (
    ScoutExecutionOut,
)
from app.scouting_requests.schemas import (
    ScoutExecutionReason as Reason,
)
from app.scouting_requests.schemas import (
    ScoutExecutionState as State,
)

_EXECUTE = JobType.SCOUT_REQUEST_EXECUTE.value
_PAUSED = ScoutRequestStatus.PAUSED.value
_FAILED = ScoutRequestStatus.FAILED.value
_IN_FLIGHT = (ScoutRequestStatus.QUEUED.value, ScoutRequestStatus.RUNNING.value)
_SETTLED = (
    ScoutRequestStatus.DRAFT.value,
    ScoutRequestStatus.COMPLETED.value,
    ScoutRequestStatus.FAILED.value,
)
_JOB_FAILED = ("failed", "dead_lettered")


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def db_now(db: Session) -> datetime:
    """Database time on PostgreSQL (``clock_timestamp()``); the process clock on SQLite."""
    if db.get_bind().dialect.name == "postgresql":
        return _aware(db.execute(select(func.clock_timestamp())).scalar_one())
    return datetime.now(UTC)


def attempt_holds_authority(job: Any, admission: Any, now: datetime) -> bool:
    """The admission's admitting attempt still holds valid authority at ``now``: its job is
    ``claimed``/``running`` with the same attempt and worker and a lease later than ``now``.
    The single test shared by reconciliation's refusal and the derivation, so
    ``needs_attention`` is exactly what ``resolve`` could act on."""
    if job is None or job.status not in ("claimed", "running"):
        return False
    if job.attempt_count != admission.attempt or job.worker_id != admission.worker_id:
        return False
    expires = _aware(job.lease_expires_at)
    return expires is not None and expires > now


def is_eligible(admission: Any, *, holds: bool, now: datetime) -> bool:
    """Eligible for reconciliation (contract §4.1 step 1, §9.2.1 strict)."""
    if holds:
        return False
    if admission.end_kind == "unknown":
        return True
    if admission.ended_at is not None:
        return False
    deadline = _aware(admission.deadline_at)
    if deadline is None:  # a legacy row: no recorded end and no recorded deadline
        return True
    return now > deadline


def _job_row(db: Session, job_id: str | None):
    if job_id is None:
        return None
    return db.execute(
        select(Job.status, Job.attempt_count, Job.worker_id, Job.lease_expires_at).where(
            Job.id == job_id
        )
    ).first()


def _unresolved(db: Session, request: ScoutRequest, now: datetime) -> list[tuple[Any, bool]]:
    rows = db.scalars(
        select(ScoutAdmission)
        .where(
            ScoutAdmission.scout_request_id == request.id,
            ScoutAdmission.organization_id == request.organization_id,
            ScoutAdmission.workspace_id == request.workspace_id,
            ScoutAdmission.resolved_at.is_(None),
        )
        .order_by(ScoutAdmission.admitted_at)
    ).all()
    return [(row, attempt_holds_authority(_job_row(db, row.job_id), row, now)) for row in rows]


def _jobs(db: Session, request: ScoutRequest):
    scope = (
        Job.scout_request_id == request.id,
        Job.organization_id == request.organization_id,
        Job.workspace_id == request.workspace_id,
        Job.job_type == _EXECUTE,
    )
    live = [
        job
        for job in db.execute(
            select(Job.status, Job.payload, Job.started_at, Job.created_at).where(
                *scope, Job.status.in_(LIVE_JOB_STATUSES)
            )
        ).all()
        if _fence(job.payload) == request.run_fence
    ]
    latest = db.execute(
        select(
            Job.status,
            Job.last_error_summary,
            Job.completed_at,
            Job.cancelled_at,
        )
        .where(*scope)
        .order_by(Job.created_at.desc(), Job.id.desc())
        .limit(1)
    ).first()
    return live, latest


def _fence(payload: Any) -> int:
    value = payload.get("run_fence", 0) if isinstance(payload, dict) else 0
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def derive_execution(db: Session, request: ScoutRequest) -> ScoutExecutionOut:
    """The Scout's execution state now (read-only)."""
    now = db_now(db)
    unresolved = _unresolved(db, request, now)
    for row, holds in unresolved:
        if is_eligible(row, holds=holds, now=now):
            if row.end_kind == "unknown":
                reason, since = Reason.OPERATION_OUTCOME_UNKNOWN, row.ended_at
            elif row.deadline_at is None:  # legacy: no recorded deadline
                reason, since = Reason.OPERATION_OUTCOME_UNKNOWN, row.admitted_at
            else:
                reason, since = Reason.OPERATION_OVERDUE, row.deadline_at
            return ScoutExecutionOut(
                state=State.NEEDS_ATTENTION,
                reason=reason,
                next_action=Next.CONTACT_SUPPORT,
                since=_aware(since),
            )
    if request.status == _PAUSED:
        return ScoutExecutionOut(
            state=State.PAUSED, reason=Reason.PAUSED_BY_USER, next_action=Next.RESUME, since=None
        )
    live, latest = _jobs(db, request)
    if live:
        first = min(live, key=lambda job: _aware(job.created_at))
        return ScoutExecutionOut(
            state=State.IN_PROGRESS,
            reason=Reason.RUN_ACTIVE,
            next_action=Next.WAIT,
            since=_aware(first.started_at or first.created_at),
        )
    if unresolved:
        return ScoutExecutionOut(
            state=State.IN_PROGRESS,
            reason=Reason.OPERATION_FINISHING,
            next_action=Next.WAIT,
            since=None,
        )
    ended = _aware(latest.cancelled_at or latest.completed_at) if latest is not None else None
    if request.status in _IN_FLIGHT or request.status not in (*_SETTLED, _PAUSED):
        # ``queued``/``running`` with no live job (left by an earlier Cancel, or any path the
        # Cancel recovery does not cover), or an unknown status: Run now refuses it.
        return ScoutExecutionOut(
            state=State.STOPPED,
            reason=Reason.RUN_INTERRUPTED,
            next_action=Next.CONTACT_SUPPORT,
            since=ended,
        )
    latest_status = latest.status if latest is not None else None
    if latest_status == "cancelled":
        return ScoutExecutionOut(
            state=State.STOPPED, reason=Reason.RUN_CANCELLED, next_action=Next.RUN_NOW, since=ended
        )
    if latest_status in _JOB_FAILED or request.status == _FAILED:
        reason = (
            Reason.ADMISSION_UNRESOLVED
            if latest_status in _JOB_FAILED and latest.last_error_summary == "admission_unresolved"
            else Reason.RUN_FAILED
        )
        return ScoutExecutionOut(
            state=State.STOPPED,
            reason=reason,
            next_action=Next.RUN_NOW,
            since=ended if latest_status in _JOB_FAILED else None,
        )
    return ScoutExecutionOut(
        state=State.IDLE,
        reason=Reason.NONE,
        next_action=Next.RUN_NOW,
        since=_aware(request.last_run_at),
    )


def needs_attention(db: Session, request: ScoutRequest) -> bool:
    """Some unresolved admission of the Scout is eligible for reconciliation (Run now is
    refused: contract §4.2, §9.5.6)."""
    now = db_now(db)
    return any(
        is_eligible(row, holds=holds, now=now) for row, holds in _unresolved(db, request, now)
    )


def eligible_admissions(
    db: Session,
    *,
    now: datetime,
    organization_id: str | None = None,
    workspace_id: str | None = None,
) -> Iterator[tuple[Any, str, str | None, tuple[str, int] | None]]:
    """Every unresolved admission eligible for reconciliation, oldest first, with its Scout's
    status, its job's status and the Scout's current owner ``(job_id, attempt)``. Read-only
    (the operator's ``reconcile blocked`` view)."""
    stmt = (
        select(
            ScoutAdmission,
            ScoutRequest.status,
            ScoutRequest.owner_job_id,
            ScoutRequest.owner_attempt,
        )
        .join(ScoutRequest, ScoutRequest.id == ScoutAdmission.scout_request_id)
        .where(ScoutAdmission.resolved_at.is_(None))
        .order_by(ScoutAdmission.admitted_at)
    )
    if organization_id is not None:
        stmt = stmt.where(ScoutAdmission.organization_id == organization_id)
    if workspace_id is not None:
        stmt = stmt.where(ScoutAdmission.workspace_id == workspace_id)
    for row, request_status, owner_job, owner_attempt in db.execute(stmt).all():
        job = _job_row(db, row.job_id)
        if is_eligible(row, holds=attempt_holds_authority(job, row, now), now=now):
            owner = (owner_job, owner_attempt) if owner_job else None
            yield row, request_status, (job.status if job is not None else None), owner


__all__ = [
    "attempt_holds_authority",
    "db_now",
    "derive_execution",
    "eligible_admissions",
    "is_eligible",
    "needs_attention",
]
