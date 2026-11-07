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

The operator signal for blocked Scouts (FU-267-A1, :func:`blocked_scouts`) is the fleet-wide
aggregate of rule 1 over :func:`eligible_admissions`, the rows the ``reconcile blocked`` view
lists: the same predicate, never a second one.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
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


#: The admitting job's columns :func:`attempt_holds_authority` reads, outer-joined to the
#: admission so a listing reads every row's job in the same statement (never one per row).
_JOB_COLUMNS = (
    Job.status.label("job_status"),
    Job.attempt_count.label("job_attempt"),
    Job.worker_id.label("job_worker"),
    Job.lease_expires_at.label("job_lease"),
)


def _joined_job(row: Any) -> SimpleNamespace | None:
    """The admitting job from a row selected with :data:`_JOB_COLUMNS` (``None``: no job)."""
    if row.job_status is None:
        return None
    return SimpleNamespace(
        status=row.job_status,
        attempt_count=row.job_attempt,
        worker_id=row.job_worker,
        lease_expires_at=row.job_lease,
    )


def _attention(admission: Any) -> tuple[Reason, datetime | None]:
    """Why an ELIGIBLE admission puts its Scout in ``needs_attention``, and since when: an
    unknown end since it was recorded; a legacy row (no recorded deadline) since it was
    admitted; otherwise overdue since its persisted deadline."""
    if admission.end_kind == "unknown":
        return Reason.OPERATION_OUTCOME_UNKNOWN, admission.ended_at
    if admission.deadline_at is None:  # legacy: no recorded deadline
        return Reason.OPERATION_OUTCOME_UNKNOWN, admission.admitted_at
    return Reason.OPERATION_OVERDUE, admission.deadline_at


@dataclass
class _ScoutRows:
    """The committed rows the classification of one Scout reads."""

    unresolved: list[tuple[Any, bool]] = field(default_factory=list)
    live: list[Any] = field(default_factory=list)
    latest: Any = None


def _fence(payload: Any) -> int:
    value = payload.get("run_fence", 0) if isinstance(payload, dict) else 0
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _scopes(requests: list[ScoutRequest]) -> dict[tuple[str, str], list[ScoutRequest]]:
    groups: dict[tuple[str, str], list[ScoutRequest]] = {}
    for request in requests:
        groups.setdefault((request.organization_id, request.workspace_id), []).append(request)
    return groups


def _unresolved_rows(
    db: Session, organization_id: str, workspace_id: str, ids: list[str], now: datetime
) -> dict[str, list[tuple[Any, bool]]]:
    """Unresolved admissions of the Scouts, oldest first, each with whether its attempt holds
    valid authority: ONE statement (the admitting job joined, not read per row)."""
    out: dict[str, list[tuple[Any, bool]]] = {}
    rows = db.execute(
        select(ScoutAdmission, *_JOB_COLUMNS)
        .outerjoin(Job, Job.id == ScoutAdmission.job_id)
        .where(
            ScoutAdmission.scout_request_id.in_(ids),
            ScoutAdmission.organization_id == organization_id,
            ScoutAdmission.workspace_id == workspace_id,
            ScoutAdmission.resolved_at.is_(None),
        )
        .order_by(ScoutAdmission.admitted_at, ScoutAdmission.id)
    ).all()
    for row in rows:
        admission = row[0]
        out.setdefault(admission.scout_request_id, []).append(
            (admission, attempt_holds_authority(_joined_job(row), admission, now))
        )
    return out


def _load(db: Session, requests: list[ScoutRequest], now: datetime) -> dict[str, _ScoutRows]:
    """The rows every Scout's classification reads, for a whole page of Scouts in a bounded
    number of statements (W16 customer repairs §C, PROPOSED): the unresolved admissions with
    their jobs, the live execute jobs, and the latest execute job per Scout - three statements
    per tenant scope (a list page has one), whatever the number of Scouts. Every read is scoped
    by the Scouts' own organization and workspace."""
    out: dict[str, _ScoutRows] = {r.id: _ScoutRows() for r in requests}
    for (organization_id, workspace_id), group in _scopes(requests).items():
        ids = [r.id for r in group]
        fences = {r.id: r.run_fence for r in group}
        for request_id, rows in _unresolved_rows(
            db, organization_id, workspace_id, ids, now
        ).items():
            out[request_id].unresolved = rows
        scope = (
            Job.scout_request_id.in_(ids),
            Job.organization_id == organization_id,
            Job.workspace_id == workspace_id,
            Job.job_type == _EXECUTE,
        )
        for job in db.execute(
            select(
                Job.scout_request_id, Job.status, Job.payload, Job.started_at, Job.created_at
            ).where(*scope, Job.status.in_(LIVE_JOB_STATUSES))
        ).all():
            if _fence(job.payload) == fences[job.scout_request_id]:
                out[job.scout_request_id].live.append(job)
        ranked = (
            select(
                Job.scout_request_id,
                Job.status,
                Job.last_error_summary,
                Job.completed_at,
                Job.cancelled_at,
                func.row_number()
                .over(
                    partition_by=Job.scout_request_id,
                    order_by=(Job.created_at.desc(), Job.id.desc()),
                )
                .label("rank"),
            )
            .where(*scope)
            .subquery()
        )
        for latest in db.execute(select(ranked).where(ranked.c.rank == 1)).all():
            out[latest.scout_request_id].latest = latest
    return out


def _classify(request: ScoutRequest, rows: _ScoutRows, now: datetime) -> ScoutExecutionOut:
    """The Scout's execution state from its rows (pure: no database access)."""
    unresolved, live, latest = rows.unresolved, rows.live, rows.latest
    for row, holds in unresolved:
        if is_eligible(row, holds=holds, now=now):
            reason, since = _attention(row)
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


def derive_executions(
    db: Session, requests: list[ScoutRequest]
) -> dict[str, ScoutExecutionOut]:
    """The execution state of every Scout in ``requests`` (read-only), from one database clock
    reading and the rows of :func:`_load`: the same classification for the list and the detail."""
    if not requests:
        return {}
    now = db_now(db)
    rows = _load(db, requests, now)
    return {r.id: _classify(r, rows[r.id], now) for r in requests}


def derive_execution(db: Session, request: ScoutRequest) -> ScoutExecutionOut:
    """The Scout's execution state now (read-only)."""
    return derive_executions(db, [request])[request.id]


def live_run_exists(db: Session, request: ScoutRequest, *, fence: int) -> bool:
    """A live execute job of the Scout that a pause has not fenced out (its payload fence equals
    ``fence``): the "in progress" an edit is refused for (W16 §B, PROPOSED). The same rule as the
    derivation's ``run_active``."""
    for (payload,) in db.execute(
        select(Job.payload).where(
            Job.scout_request_id == request.id,
            Job.organization_id == request.organization_id,
            Job.workspace_id == request.workspace_id,
            Job.job_type == _EXECUTE,
            Job.status.in_(LIVE_JOB_STATUSES),
        )
    ).all():
        if _fence(payload) == fence:
            return True
    return False


def needs_attention(db: Session, request: ScoutRequest) -> bool:
    """Some unresolved admission of the Scout is eligible for reconciliation (Run now is
    refused: contract §4.2, §9.5.6)."""
    now = db_now(db)
    rows = _unresolved_rows(db, request.organization_id, request.workspace_id, [request.id], now)
    return any(is_eligible(row, holds=holds, now=now) for row, holds in rows.get(request.id, ()))


def eligible_admissions(
    db: Session,
    *,
    now: datetime,
    organization_id: str | None = None,
    workspace_id: str | None = None,
) -> Iterator[tuple[Any, str, str | None, tuple[str, int] | None]]:
    """Every unresolved admission eligible for reconciliation, oldest first, with its Scout's
    status, its job's status and the Scout's current owner ``(job_id, attempt)``. Read-only
    (the operator's ``reconcile blocked`` view, and the blocked-Scout signal).

    ONE statement, whatever the number of rows: the unresolved admissions (read through the
    partial index ``uq_scout_admission_unresolved``, ``WHERE resolved_at IS NULL``) with their
    Scout and their admitting job joined; eligibility is then decided per row by the shared
    :func:`is_eligible` and :func:`attempt_holds_authority`. K = 1 bounds the rows by the
    number of Scouts with an operation outstanding (in flight or blocked)."""
    stmt = (
        select(
            ScoutAdmission,
            ScoutRequest.status.label("request_status"),
            ScoutRequest.owner_job_id,
            ScoutRequest.owner_attempt,
            *_JOB_COLUMNS,
        )
        .join(ScoutRequest, ScoutRequest.id == ScoutAdmission.scout_request_id)
        .outerjoin(Job, Job.id == ScoutAdmission.job_id)
        .where(ScoutAdmission.resolved_at.is_(None))
        .order_by(ScoutAdmission.admitted_at, ScoutAdmission.id)
    )
    if organization_id is not None:
        stmt = stmt.where(ScoutAdmission.organization_id == organization_id)
    if workspace_id is not None:
        stmt = stmt.where(ScoutAdmission.workspace_id == workspace_id)
    for result in db.execute(stmt).all():
        row, job = result[0], _joined_job(result)
        if is_eligible(row, holds=attempt_holds_authority(job, row, now), now=now):
            owner = (result.owner_job_id, result.owner_attempt) if result.owner_job_id else None
            yield row, result.request_status, (job.status if job is not None else None), owner


@dataclass(frozen=True)
class BlockedScouts:
    """The Scouts in ``needs_attention`` across every tenant, at database time ``as_of``: how
    many, and since when the longest-blocked one has been. Counts and instants only - no id,
    tenancy, name or customer content."""

    count: int
    oldest_since: datetime | None
    as_of: datetime

    @property
    def oldest_age_seconds(self) -> float | None:
        """How long the longest-blocked Scout has been blocked (``None`` when none is)."""
        if self.oldest_since is None:
            return None
        return max(0.0, (self.as_of - self.oldest_since).total_seconds())


def ledger_kept(db: Session) -> bool:
    """The K = 1 admission ledger is kept on this database (PostgreSQL only: on SQLite a run
    never writes it, so nothing bounds concurrent operations and nothing can be blocked)."""
    return db.get_bind().dialect.name == "postgresql"


def blocked_scouts(db: Session) -> BlockedScouts | None:
    """The fleet-wide blocked-Scout signal (FU-267-A1, PROPOSED): the Scouts the ``reconcile
    blocked`` view lists (rule 1, ``needs_attention``), aggregated. Read-only; one database
    clock reading and the one statement of :func:`eligible_admissions`.

    ``None`` - the signal is ABSENT, never zero - where the ledger is not kept (SQLite): a zero
    there would claim a bound that does not exist. A Scout's ``since`` is that of its oldest
    eligible admission, exactly as its own derived state reports it."""
    if not ledger_kept(db):
        return None
    now = db_now(db)
    since: dict[str, datetime] = {}
    for row, _status, _job_status, _owner in eligible_admissions(db, now=now):
        if row.scout_request_id not in since:
            since[row.scout_request_id] = _aware(_attention(row)[1] or row.admitted_at)
    return BlockedScouts(
        count=len(since), oldest_since=min(since.values(), default=None), as_of=now
    )


__all__ = [
    "BlockedScouts",
    "attempt_holds_authority",
    "blocked_scouts",
    "db_now",
    "derive_execution",
    "derive_executions",
    "eligible_admissions",
    "is_eligible",
    "ledger_kept",
    "live_run_exists",
    "needs_attention",
]
