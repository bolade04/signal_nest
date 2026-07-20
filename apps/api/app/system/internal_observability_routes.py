"""Operator-only read observability surface (Phase 4A-B).

Additive, **read-only** extensions of the operator ``/internal/system/*`` tier
(the existing operator disclosure tier established in
``app.system.internal_routes``). They compose the durable-job, worker-fleet and
schedule state already present in the platform into coarse, secret-free operator
views:

* ``GET /internal/system/overview`` — one operational snapshot (job status
  counts + total, stuck count, dead-letter count, worker-fleet health, schedule
  state counts).
* ``GET /internal/system/jobs/list`` — bounded, filtered, cross-tenant job page.
* ``GET /internal/system/jobs/stuck`` — live stuck-job summary (§8.10, via
  :mod:`app.jobs.stuck`).
* ``GET /internal/system/jobs/dead-letter`` — dead-letter count + recent page.
* ``GET /internal/system/jobs/{job_id}`` — single-job operator detail.
* ``GET /internal/system/jobs/{job_id}/events`` — a job's sanitized event timeline.
* ``GET /internal/system/schedules`` — schedule visibility with derived state.

Every route requires an authenticated operator (``require_operator``) and returns
only the already-established secret-free operator fields — never a raw payload,
URL, credential, lease token, correlation/trace id, or worker identity beyond the
safe ids the operator job schema already exposes. Nothing here mutates state or
enables any capability; per-workspace capability overrides are a later batch.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth.dependencies import require_operator
from app.core.config import get_settings
from app.core.enums import ScheduleInterval, ScheduleState
from app.core.errors import NotFoundError
from app.db.session import get_db
from app.jobs.models import Job
from app.jobs.schemas import (
    DeadLetterJobsOut,
    JobEventOut,
    JobOperatorOut,
    JobPageOut,
    StuckJobsOut,
)
from app.jobs.status import JobStatus, JobType
from app.jobs.store import job_store, utcnow
from app.jobs.stuck import count_stuck, list_stuck
from app.jobs.worker_registry import worker_registry
from app.organizations.models import User
from app.scouting_requests.models import ScoutSchedule
from app.scouting_requests.schedules import derive_schedule_state

router = APIRouter(prefix="/internal/system", tags=["internal"])


# --------------------------------------------------------------------------- #
# Schemas (operator-safe; bounded enums, counts and safe ids only)
# --------------------------------------------------------------------------- #
class ScheduleOperatorOut(BaseModel):
    """Operator view of one scouting schedule with its derived lifecycle state.

    ``state`` is computed live from the row plus the live tick chain
    (:func:`derive_schedule_state`) so it can never drift from reality. Carries
    only safe scope ids and lifecycle timestamps — never any secret.
    """

    id: str
    organization_id: str
    workspace_id: str
    location_id: str | None
    scout_request_id: str
    interval: ScheduleInterval
    enabled: bool
    state: ScheduleState
    next_run_at: datetime | None
    last_tick_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ScheduleFleetOut(BaseModel):
    """A bounded, cross-tenant operator page of schedules."""

    total: int
    limit: int
    offset: int
    items: list[ScheduleOperatorOut]


class JobsOverviewOut(BaseModel):
    total: int
    status_counts: dict[str, int]
    stuck_count: int
    dead_letter_count: int


class WorkersOverviewOut(BaseModel):
    status_counts: dict[str, int]
    active_count: int
    stale_count: int


class SchedulesOverviewOut(BaseModel):
    total: int
    state_counts: dict[str, int]


class OperationalOverviewOut(BaseModel):
    """One coarse operator snapshot of queue, fleet and schedule health."""

    stale_after_seconds: float
    as_of: datetime
    jobs: JobsOverviewOut
    workers: WorkersOverviewOut
    schedules: SchedulesOverviewOut


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _dead_letter_count(db: Session) -> int:
    return int(
        db.scalar(
            select(func.count())
            .select_from(Job)
            .where(Job.status == JobStatus.DEAD_LETTERED.value)
        )
        or 0
    )


def _schedule_state_counts(db: Session) -> tuple[int, dict[str, int]]:
    """Return ``(total, state_counts)`` across all schedules (cross-tenant).

    ``paused`` is a pure column count; the enabled rows (capped per workspace by
    the product rule) are classified with the same authoritative
    :func:`derive_schedule_state` used everywhere else, so operator counts can
    never disagree with the customer-facing schedule state.
    """
    paused = int(
        db.scalar(
            select(func.count())
            .select_from(ScoutSchedule)
            .where(ScoutSchedule.enabled.is_(False))
        )
        or 0
    )
    enabled_rows = list(
        db.execute(
            select(ScoutSchedule).where(ScoutSchedule.enabled.is_(True))
        ).scalars()
    )
    active = sum(
        1 for s in enabled_rows if derive_schedule_state(db, s) is ScheduleState.ACTIVE
    )
    activation_required = len(enabled_rows) - active
    counts = {
        ScheduleState.PAUSED.value: paused,
        ScheduleState.ACTIVE.value: active,
        ScheduleState.ACTIVATION_REQUIRED.value: activation_required,
    }
    return paused + len(enabled_rows), counts


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@router.get("/overview", response_model=OperationalOverviewOut)
def internal_overview(
    db: Session = Depends(get_db),
    _operator: User = Depends(require_operator),
) -> OperationalOverviewOut:
    """A single coarse operator snapshot composed from existing diagnostics.

    Read-only and cross-tenant (operator-gated). Stuck is derived live against the
    request clock and the configured worker stale threshold, so the number is
    accurate regardless of the recovery-sweep cadence.
    """
    settings = get_settings()
    now = utcnow()
    stale_after = settings.worker_stale_after_seconds

    job_counts = {
        str(k): int(v)
        for k, v in db.execute(
            select(Job.status, func.count()).group_by(Job.status)
        ).all()
    }
    schedule_total, state_counts = _schedule_state_counts(db)

    return OperationalOverviewOut(
        stale_after_seconds=stale_after,
        as_of=now,
        jobs=JobsOverviewOut(
            total=sum(job_counts.values()),
            status_counts=job_counts,
            stuck_count=count_stuck(db, now=now, stale_after_seconds=stale_after),
            dead_letter_count=_dead_letter_count(db),
        ),
        workers=WorkersOverviewOut(
            status_counts=worker_registry.status_counts(db),
            active_count=worker_registry.active_count(
                db, stale_after_seconds=stale_after, now=now
            ),
            stale_count=worker_registry.stale_count(
                db, stale_after_seconds=stale_after, now=now
            ),
        ),
        schedules=SchedulesOverviewOut(total=schedule_total, state_counts=state_counts),
    )


@router.get("/jobs/list", response_model=JobPageOut)
def internal_jobs_list(
    organization_id: str | None = Query(default=None),
    workspace_id: str | None = Query(default=None),
    location_id: str | None = Query(default=None),
    scout_request_id: str | None = Query(default=None),
    status: JobStatus | None = Query(default=None),
    job_type: JobType | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    _operator: User = Depends(require_operator),
) -> JobPageOut:
    """Bounded, filtered, cross-tenant job listing (operator diagnostics).

    Unlike the customer ``/workspaces/{id}/jobs`` listing this is intentionally
    cross-tenant — hence the operator gate — but it still returns only the
    secret-free :class:`JobOperatorOut` view (no raw payload/lease token). Filters
    narrow by tenant scope, status and job type; results are newest-first.
    """
    base = select(Job)
    if organization_id:
        base = base.where(Job.organization_id == organization_id)
    if workspace_id:
        base = base.where(Job.workspace_id == workspace_id)
    if location_id:
        base = base.where(Job.location_id == location_id)
    if scout_request_id:
        base = base.where(Job.scout_request_id == scout_request_id)
    if status is not None:
        base = base.where(Job.status == status.value)
    if job_type is not None:
        base = base.where(Job.job_type == job_type.value)

    total = int(db.scalar(select(func.count()).select_from(base.subquery())) or 0)
    rows = list(
        db.execute(
            base.order_by(Job.created_at.desc(), Job.id.desc())
            .limit(limit)
            .offset(offset)
        ).scalars()
    )
    return JobPageOut(
        items=[JobOperatorOut.model_validate(j) for j in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/jobs/stuck", response_model=StuckJobsOut)
def internal_jobs_stuck(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    _operator: User = Depends(require_operator),
) -> StuckJobsOut:
    """Live stuck-job summary per Phase 4A plan §8.10.

    A job is stuck when it is still claimed/running yet its lease has expired or
    its heartbeat is older than the configured worker stale threshold. Computed
    against the request clock; read-only (no requeue/recovery is triggered here).
    """
    now = utcnow()
    stale_after = get_settings().worker_stale_after_seconds
    rows, total = list_stuck(
        db, now=now, stale_after_seconds=stale_after, limit=limit, offset=offset
    )
    return StuckJobsOut(
        stuck_count=total,
        stale_after_seconds=stale_after,
        as_of=now,
        limit=limit,
        offset=offset,
        items=[JobOperatorOut.model_validate(j) for j in rows],
    )


@router.get("/jobs/dead-letter", response_model=DeadLetterJobsOut)
def internal_jobs_dead_letter(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    _operator: User = Depends(require_operator),
) -> DeadLetterJobsOut:
    """Dead-letter visibility: total count + a bounded recent page. Read-only.

    Surfaces jobs that exhausted their attempts (or expired their lease with no
    attempts left). No requeue/retry action is offered — that is a later,
    separately-approved operability action; this is visibility only.
    """
    base = select(Job).where(Job.status == JobStatus.DEAD_LETTERED.value)
    total = _dead_letter_count(db)
    rows = list(
        db.execute(
            base.order_by(Job.completed_at.desc(), Job.created_at.desc(), Job.id.desc())
            .limit(limit)
            .offset(offset)
        ).scalars()
    )
    return DeadLetterJobsOut(
        total=total,
        limit=limit,
        offset=offset,
        items=[JobOperatorOut.model_validate(j) for j in rows],
    )


@router.get("/jobs/{job_id}/events", response_model=list[JobEventOut])
def internal_job_events(
    job_id: str,
    limit: int = Query(default=200, ge=1, le=500),
    db: Session = Depends(get_db),
    _operator: User = Depends(require_operator),
) -> list[JobEventOut]:
    """A single job's sanitized lifecycle-event timeline (oldest-first).

    Uses the same safe :class:`JobEventOut` projection the customer sees — event
    type, status transition, attempt, error code and bounded safe metadata — never
    a raw payload, worker id or secret. 404 when the job id is unknown.
    """
    if db.get(Job, job_id) is None:
        raise NotFoundError("Job not found.")
    events = job_store.list_events(db, job_id=job_id, limit=limit)
    return [JobEventOut.model_validate(e) for e in events]


@router.get("/jobs/{job_id}", response_model=JobOperatorOut)
def internal_job_detail(
    job_id: str,
    db: Session = Depends(get_db),
    _operator: User = Depends(require_operator),
) -> JobOperatorOut:
    """Single-job operator detail (cross-tenant). 404 when unknown.

    Returns the secret-free operator view (safe worker/lease diagnostics, never a
    raw payload). Cross-tenant lookup is deliberate and gated by ``require_operator``.
    """
    job = db.get(Job, job_id)
    if job is None:
        raise NotFoundError("Job not found.")
    return JobOperatorOut.model_validate(job)


@router.get("/schedules", response_model=ScheduleFleetOut)
def internal_schedules(
    workspace_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    _operator: User = Depends(require_operator),
) -> ScheduleFleetOut:
    """Schedule visibility with live-derived lifecycle state (cross-tenant).

    Operator-gated and read-only. Each row's ``state`` is derived from the live
    tick chain via :func:`derive_schedule_state`, so it reflects the real
    paused / active / activation-required lifecycle rather than a persisted guess.
    """
    base = select(ScoutSchedule)
    if workspace_id:
        base = base.where(ScoutSchedule.workspace_id == workspace_id)

    total = int(db.scalar(select(func.count()).select_from(base.subquery())) or 0)
    rows = list(
        db.execute(
            base.order_by(ScoutSchedule.created_at.desc(), ScoutSchedule.id.desc())
            .limit(limit)
            .offset(offset)
        ).scalars()
    )
    items = [
        ScheduleOperatorOut(
            id=s.id,
            organization_id=s.organization_id,
            workspace_id=s.workspace_id,
            location_id=s.location_id,
            scout_request_id=s.scout_request_id,
            interval=s.interval_enum(),
            enabled=s.enabled,
            state=derive_schedule_state(db, s),
            next_run_at=s.next_run_at,
            last_tick_at=s.last_tick_at,
            created_at=s.created_at,
            updated_at=s.updated_at,
        )
        for s in rows
    ]
    return ScheduleFleetOut(total=total, limit=limit, offset=offset, items=items)
