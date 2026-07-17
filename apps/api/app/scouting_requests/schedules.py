"""Scouting schedule service — recurrence math + lifecycle (SB-B).

This module owns the *backend* recurrence foundation for a scouting request:

* **Pure recurrence math** (:func:`interval_delta`, :func:`next_future_occurrence`)
  — fixed UTC intervals only. ``daily`` is 24h, ``weekly`` is 7 days; the minimum
  interval is 24h so a schedule can never fan out more than once per day. There is
  no clock-of-day, timezone or DST handling, and **missed occurrences are skipped,
  never back-filled** — the next occurrence is always the first interval boundary
  strictly in the future.
* **Lifecycle** (:func:`create_schedule` / :func:`pause_schedule` /
  :func:`resume_schedule` / :func:`delete_schedule`) — enforcing the product limits
  (one schedule per request, four *enabled* schedules per workspace), writing the
  audit trail, and — only while the feature is live — seeding the self-chaining
  ``scout_schedule.tick`` job.
* **Fan-out plumbing** (:func:`enqueue_schedule_tick`, :func:`has_active_execution`)
  reused by the tick handler.

Everything here is dark by default: unless ``scout_scheduling_enabled`` is on, no
tick is ever enqueued, so no scheduled scouting run can occur. The service never
touches the manual-run, cancellation or lease semantics of the durable-job system;
it only *enqueues* work through the existing service seam.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit.service import record_audit
from app.core.config import get_settings
from app.core.enums import ScheduleInterval
from app.core.errors import ValidationDomainError
from app.core.logging import get_logger, log_event
from app.db.base import utcnow
from app.jobs.context import ExecutionContext
from app.jobs.models import Job
from app.jobs.service import enqueue_job, enqueue_scout_request
from app.jobs.status import ACTIVE_STATUSES, ENQUEUED_STATUSES, JobType
from app.scouting_requests.models import ScoutRequest, ScoutSchedule

logger = get_logger("signalnest.scouting.schedules")

#: At most this many *enabled* schedules may exist in one workspace.
MAX_ACTIVE_SCHEDULES_PER_WORKSPACE = 4

#: Fixed recurrence intervals. The 24h floor is intrinsic: the shortest cadence is
#: daily, so no schedule can enqueue more than once per day.
_INTERVAL_DELTAS: dict[ScheduleInterval, timedelta] = {
    ScheduleInterval.DAILY: timedelta(hours=24),
    ScheduleInterval.WEEKLY: timedelta(days=7),
}

#: Provenance marker written onto a scheduled run's payload so the customer-safe
#: run-history projection can honestly label it ``scheduled`` (SB-A contract).
SCHEDULED_TRIGGER = "scheduled"

#: Statuses that mean a scout request already has execution work in flight, so a
#: scheduled run must coalesce (skip) rather than pile a second run on top.
_INFLIGHT_STATUSES = frozenset(
    s.value for s in (ACTIVE_STATUSES | ENQUEUED_STATUSES)
)


def interval_delta(interval: ScheduleInterval) -> timedelta:
    """Return the fixed :class:`~datetime.timedelta` for a recurrence cadence."""
    return _INTERVAL_DELTAS[interval]


def next_future_occurrence(
    base: datetime, interval: ScheduleInterval, now: datetime
) -> datetime:
    """First interval boundary after ``base`` that is strictly in the future.

    Steps forward from ``base`` in whole intervals until past ``now``. Any missed
    boundaries (the process was down, the schedule paused, etc.) are *skipped*, not
    replayed — there is never a catch-up/backfill run.
    """
    delta = interval_delta(interval)
    occurrence = base + delta
    while occurrence <= now:
        occurrence += delta
    return occurrence


def _coerce_interval(interval: ScheduleInterval | str) -> ScheduleInterval:
    try:
        return ScheduleInterval(interval)
    except ValueError as exc:
        raise ValidationDomainError(
            "Unsupported schedule interval; use 'daily' or 'weekly'."
        ) from exc


def _count_enabled(db: Session, workspace_id: str) -> int:
    return int(
        db.scalar(
            select(func.count())
            .select_from(ScoutSchedule)
            .where(
                ScoutSchedule.workspace_id == workspace_id,
                ScoutSchedule.enabled.is_(True),
            )
        )
        or 0
    )


def has_active_execution(
    db: Session,
    *,
    organization_id: str,
    workspace_id: str,
    scout_request_id: str,
) -> bool:
    """True when the request already has a scout-execute job in flight.

    Used to coalesce a scheduled run: never enqueue a second execution while one is
    pending/scheduled/claimed/running/cancel-requested for the same request. Scoped
    to the tenant + request so it can never observe another tenant's jobs.
    """
    found = db.scalar(
        select(Job.id)
        .where(
            Job.organization_id == organization_id,
            Job.workspace_id == workspace_id,
            Job.scout_request_id == scout_request_id,
            Job.job_type == JobType.SCOUT_REQUEST_EXECUTE.value,
            Job.status.in_(_INFLIGHT_STATUSES),
        )
        .limit(1)
    )
    return found is not None


def enqueue_schedule_tick(
    db: Session,
    *,
    schedule: ScoutSchedule,
    occurrence_at: datetime,
    now: datetime | None = None,
) -> Job:
    """Enqueue the self-chaining ``scout_schedule.tick`` job for one occurrence.

    Idempotent per (schedule, occurrence): a retried handler re-enqueuing the same
    successor tick collapses onto the existing row via the tenant idempotency key,
    so an at-least-once handler can never spawn a tick storm. The tick is
    ``scheduled_for`` its occurrence instant, so the worker only claims it once due.
    """
    context = ExecutionContext.for_scout_request(
        organization_id=schedule.organization_id,
        workspace_id=schedule.workspace_id,
        location_id=schedule.location_id,
        request_id=schedule.scout_request_id,
    )
    return enqueue_job(
        db,
        job_type=JobType.SCOUT_SCHEDULE_TICK,
        context=context,
        payload={
            "schedule_id": schedule.id,
            "occurrence_at": occurrence_at.isoformat(),
        },
        location_id=schedule.location_id,
        scout_request_id=schedule.scout_request_id,
        idempotency_key=f"schedule-tick:{schedule.id}:{occurrence_at.isoformat()}",
        scheduled_for=occurrence_at,
        now=now,
    )


def run_schedule_tick(
    db: Session,
    *,
    schedule_id: str,
    occurrence_at: datetime,
    now: datetime | None = None,
    is_cancelled: Callable[[], bool] | None = None,
) -> dict:
    """Process one schedule tick and (re)seed the chain. Returns a safe summary.

    The fan-out sequence, in order:

    1. **Load** the schedule. A missing row (hard-deleted) stops the chain — no run,
       no successor tick.
    2. **Feature gate.** While ``scout_scheduling_enabled`` is off the tick is a
       self-terminating no-op: no run, no successor. This is the dark-deploy
       guarantee — zero recurring work until the flag is turned on.
    3. **Enabled gate.** A paused schedule likewise stops the chain.
    4. **Cancellation.** An operator-cancelled tick stops the chain.
    5. **Overlap coalescing.** If the request already has an execution in flight the
       scheduled run is *skipped* (not queued twice), but the schedule continues.
    6. **Fan-out.** Otherwise enqueue exactly one ``scout_request.execute`` run,
       idempotent on ``schedule:{id}:{occurrence}`` and labelled ``scheduled``.
    7. **Self-chain.** Compute the next future occurrence (missed boundaries
       skipped, never back-filled), enqueue the successor tick, and advance
       ``last_tick_at`` / ``next_run_at``.

    The function never commits or rolls back — the worker owns the transaction.
    """
    now = now or utcnow()
    schedule = db.get(ScoutSchedule, schedule_id)
    if schedule is None:
        return {"outcome": "schedule_missing", "run_enqueued": False, "chained": False}
    if not get_settings().scout_scheduling_enabled:
        return {"outcome": "feature_disabled", "run_enqueued": False, "chained": False}
    if not schedule.enabled:
        return {"outcome": "disabled", "run_enqueued": False, "chained": False}
    if is_cancelled is not None and is_cancelled():
        return {"outcome": "cancelled", "run_enqueued": False, "chained": False}

    coalesced = has_active_execution(
        db,
        organization_id=schedule.organization_id,
        workspace_id=schedule.workspace_id,
        scout_request_id=schedule.scout_request_id,
    )
    run_enqueued = False
    if not coalesced:
        enqueue_scout_request(
            db,
            organization_id=schedule.organization_id,
            workspace_id=schedule.workspace_id,
            scout_request_id=schedule.scout_request_id,
            location_id=schedule.location_id,
            request_id=schedule.scout_request_id,
            idempotency_key=f"schedule:{schedule.id}:{occurrence_at.isoformat()}",
            trigger=SCHEDULED_TRIGGER,
            now=now,
        )
        run_enqueued = True

    next_occurrence = next_future_occurrence(
        occurrence_at, schedule.interval_enum(), now
    )
    enqueue_schedule_tick(
        db, schedule=schedule, occurrence_at=next_occurrence, now=now
    )
    schedule.last_tick_at = now
    schedule.next_run_at = next_occurrence
    db.add(schedule)
    db.flush()

    log_event(
        logger,
        "scout_schedule_tick",
        outcome="success",
        workspace_id=schedule.workspace_id,
        scout_request_id=schedule.scout_request_id,
        run_enqueued=run_enqueued,
        coalesced=coalesced,
    )
    return {
        "outcome": "fanned_out" if run_enqueued else "coalesced",
        "run_enqueued": run_enqueued,
        "coalesced": coalesced,
        "chained": True,
        "next_run_at": next_occurrence.isoformat(),
    }


def create_schedule(
    db: Session,
    *,
    request: ScoutRequest,
    interval: ScheduleInterval | str,
    actor_user_id: str | None = None,
    enabled: bool = True,
    now: datetime | None = None,
) -> ScoutSchedule:
    """Create the single schedule for a scout request.

    Enforces the two product limits (one schedule per request; four enabled per
    workspace), writes the ``scout_schedule.created`` audit record, and — only when
    ``enabled`` and the feature flag is live — seeds the first tick one interval out.
    ``next_run_at`` is ``enabled_at + interval`` (pure interval-from-enable).
    """
    now = now or utcnow()
    interval_enum = _coerce_interval(interval)

    existing = db.scalar(
        select(ScoutSchedule).where(
            ScoutSchedule.workspace_id == request.workspace_id,
            ScoutSchedule.scout_request_id == request.id,
        )
    )
    if existing is not None:
        raise ValidationDomainError("This request already has a schedule.")

    if enabled and _count_enabled(db, request.workspace_id) >= MAX_ACTIVE_SCHEDULES_PER_WORKSPACE:
        raise ValidationDomainError(
            "This workspace already has the maximum number of active schedules."
        )

    next_run_at = now + interval_delta(interval_enum) if enabled else None
    schedule = ScoutSchedule(
        organization_id=request.organization_id,
        workspace_id=request.workspace_id,
        location_id=request.location_id,
        scout_request_id=request.id,
        interval=interval_enum.value,
        enabled=enabled,
        next_run_at=next_run_at,
    )
    db.add(schedule)
    db.flush()

    if enabled and next_run_at is not None and get_settings().scout_scheduling_enabled:
        enqueue_schedule_tick(db, schedule=schedule, occurrence_at=next_run_at, now=now)

    record_audit(
        db,
        organization_id=schedule.organization_id,
        workspace_id=schedule.workspace_id,
        actor_user_id=actor_user_id,
        action="scout_schedule.created",
        entity_type="scout_schedule",
        entity_id=schedule.id,
    )
    log_event(
        logger,
        "scout_schedule_created",
        outcome="success",
        workspace_id=schedule.workspace_id,
        scout_request_id=schedule.scout_request_id,
        interval=schedule.interval,
        enabled=schedule.enabled,
    )
    return schedule


def pause_schedule(
    db: Session,
    *,
    schedule: ScoutSchedule,
    actor_user_id: str | None = None,
) -> ScoutSchedule:
    """Pause a schedule: the row is retained but inert.

    ``enabled`` flips to False and ``next_run_at`` is cleared, so any tick already
    in flight becomes a self-terminating no-op. No new work is enqueued.
    """
    schedule.enabled = False
    schedule.next_run_at = None
    db.add(schedule)
    db.flush()
    record_audit(
        db,
        organization_id=schedule.organization_id,
        workspace_id=schedule.workspace_id,
        actor_user_id=actor_user_id,
        action="scout_schedule.paused",
        entity_type="scout_schedule",
        entity_id=schedule.id,
    )
    log_event(
        logger,
        "scout_schedule_paused",
        outcome="success",
        workspace_id=schedule.workspace_id,
        scout_request_id=schedule.scout_request_id,
    )
    return schedule


def resume_schedule(
    db: Session,
    *,
    schedule: ScoutSchedule,
    actor_user_id: str | None = None,
    now: datetime | None = None,
) -> ScoutSchedule:
    """Resume a paused schedule, recomputing the next occurrence from *now*.

    Recurrence restarts from the resume moment (``now + interval``) — the pause gap
    is never back-filled. Re-enabling is subject to the per-workspace active limit.
    Seeds a fresh tick only while the feature flag is live.
    """
    now = now or utcnow()
    if _count_enabled(db, schedule.workspace_id) >= MAX_ACTIVE_SCHEDULES_PER_WORKSPACE:
        raise ValidationDomainError(
            "This workspace already has the maximum number of active schedules."
        )
    schedule.enabled = True
    schedule.next_run_at = now + interval_delta(schedule.interval_enum())
    db.add(schedule)
    db.flush()

    if get_settings().scout_scheduling_enabled:
        enqueue_schedule_tick(
            db, schedule=schedule, occurrence_at=schedule.next_run_at, now=now
        )

    record_audit(
        db,
        organization_id=schedule.organization_id,
        workspace_id=schedule.workspace_id,
        actor_user_id=actor_user_id,
        action="scout_schedule.resumed",
        entity_type="scout_schedule",
        entity_id=schedule.id,
    )
    log_event(
        logger,
        "scout_schedule_resumed",
        outcome="success",
        workspace_id=schedule.workspace_id,
        scout_request_id=schedule.scout_request_id,
    )
    return schedule


def delete_schedule(
    db: Session,
    *,
    schedule: ScoutSchedule,
    actor_user_id: str | None = None,
) -> None:
    """Hard-delete a schedule (no soft-delete column exists).

    Any tick still in flight self-terminates when it fails to load the row. The
    audit record is written before the delete so the deletion is itself auditable.
    """
    workspace_id = schedule.workspace_id
    scout_request_id = schedule.scout_request_id
    record_audit(
        db,
        organization_id=schedule.organization_id,
        workspace_id=workspace_id,
        actor_user_id=actor_user_id,
        action="scout_schedule.deleted",
        entity_type="scout_schedule",
        entity_id=schedule.id,
    )
    db.delete(schedule)
    db.flush()
    log_event(
        logger,
        "scout_schedule_deleted",
        outcome="success",
        workspace_id=workspace_id,
        scout_request_id=scout_request_id,
    )


__all__ = [
    "MAX_ACTIVE_SCHEDULES_PER_WORKSPACE",
    "SCHEDULED_TRIGGER",
    "create_schedule",
    "delete_schedule",
    "enqueue_schedule_tick",
    "has_active_execution",
    "interval_delta",
    "next_future_occurrence",
    "pause_schedule",
    "resume_schedule",
    "run_schedule_tick",
]
