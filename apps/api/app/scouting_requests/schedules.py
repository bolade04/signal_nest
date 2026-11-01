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
* **Failure survival** (:func:`recover_after_tick_failure`, P6-PLAT-6) — a tick that
  fails for the last time (``failed`` / ``dead_lettered``) no longer ends the
  recurrence silently: the worker's terminal-failure hook re-seeds the next
  occurrence and records a structured failure event, and the failure stays visible
  in the derived schedule state (:func:`derive_schedule_state`,
  :func:`last_tick_failure`).

Everything here is dark by default. Whether scheduling is available is decided per
workspace by the capability resolver (:func:`resolve_scheduling_capability`, P6-CAP-1)
— the same decision the HTTP gate enforces and the customer reflection reports — so
with the shipped ``scout_scheduling_enabled=False`` and no workspace override no tick
is ever enqueued and no scheduled scouting run can occur. The service never touches
the manual-run, cancellation or lease semantics of the durable-job system; it only
*enqueues* work through the existing service seam.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit.actions import AuditAction
from app.audit.service import record_audit
from app.capabilities.registry import Capability
from app.capabilities.resolver import CapabilityResolution, resolve_capability
from app.core.config import get_settings
from app.core.enums import ScheduleInterval, ScheduleState, ScoutRequestStatus
from app.core.errors import ConflictError, ValidationDomainError
from app.core.logging import get_logger, log_event
from app.db.base import utcnow
from app.jobs.context import ExecutionContext
from app.jobs.models import Job
from app.jobs.service import enqueue_job, enqueue_scout_request
from app.jobs.status import ACTIVE_STATUSES, ENQUEUED_STATUSES, JobStatus, JobType
from app.organizations.models import Workspace
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
# Tick outcome while the schedule's scout request is paused (D-25): no run is fanned
# out, the chain still reseeds, so a resumed request is scouted at the next boundary.
REQUEST_PAUSED_OUTCOME = "request_paused"
# Tick outcome while the Scout has an unresolved admission (execution controls, PROPOSED).
ADMISSION_UNRESOLVED_OUTCOME = "admission_unresolved"


def _has_unresolved_admission(db: Session, scout_request_id: str) -> bool:
    from app.scouting_requests.models import ScoutAdmission

    return (
        db.scalar(
            select(ScoutAdmission.id)
            .where(
                ScoutAdmission.scout_request_id == scout_request_id,
                ScoutAdmission.resolved_at.is_(None),
            )
            .limit(1)
        )
        is not None
    )

#: Statuses that mean a scout request already has execution work in flight, so a
#: scheduled run must coalesce (skip) rather than pile a second run on top.
_INFLIGHT_STATUSES = frozenset(
    s.value for s in (ACTIVE_STATUSES | ENQUEUED_STATUSES)
)

#: Terminal statuses that mean a tick will never run again *and* it failed. A
#: cancelled tick is an operator's deliberate stop, not a failure, so it is absent.
_FAILED_TICK_STATUSES = frozenset(
    {JobStatus.FAILED.value, JobStatus.DEAD_LETTERED.value}
)

#: Terminal statuses that carry a ``completed_at``: the ticks that actually finished.
_FINISHED_TICK_STATUSES = _FAILED_TICK_STATUSES | {JobStatus.SUCCEEDED.value}


def resolve_scheduling_capability(
    db: Session, *, organization_id: str, workspace_id: str
) -> CapabilityResolution:
    """The one place scheduling availability is decided for a workspace (P6-CAP-1).

    Every scheduling enforcement point calls this — the HTTP mutation gate, the
    customer reflection, the tick's own feature gate and the tick seeding on create /
    resume / failure recovery — so a workspace override and the safety ceiling apply
    to all of them identically, and what the UI shows is the decision the backend
    enforces. The tenant identity is always server-derived: the authenticated
    tenant context for a route, the durable schedule row for the worker.

    A dependency failure is logged and re-raised, never converted into a decision: a
    coerced ``False`` would be indistinguishable from a genuine governance denial and
    would turn an outage into an apparent policy.
    """
    try:
        return resolve_capability(
            session=db,
            settings=get_settings(),
            capability=Capability.SCOUT_SCHEDULING,
            organization_id=organization_id,
            workspace_id=workspace_id,
        )
    except Exception:
        log_event(
            logger,
            "scout_scheduling_gate_failed",
            level=logging.ERROR,
            outcome="error",
            capability=Capability.SCOUT_SCHEDULING.value,
            organization_id=organization_id,
            workspace_id=workspace_id,
        )
        raise


def _scheduling_enabled_for(db: Session, schedule: ScoutSchedule) -> bool:
    """Whether scheduling is effectively enabled for the schedule's own workspace."""
    return resolve_scheduling_capability(
        db,
        organization_id=schedule.organization_id,
        workspace_id=schedule.workspace_id,
    ).effective_enabled


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


def _count_enabled(
    db: Session, workspace_id: str, *, exclude_schedule_id: str | None = None
) -> int:
    """Count the workspace's *enabled* schedules, optionally excluding one row.

    ``exclude_schedule_id`` lets :func:`resume_schedule` avoid counting the row it is
    about to (re)enable against itself — an already-enabled-but-inert schedule must
    never consume a slot twice when it is activated.
    """
    stmt = (
        select(func.count())
        .select_from(ScoutSchedule)
        .where(
            ScoutSchedule.workspace_id == workspace_id,
            ScoutSchedule.enabled.is_(True),
        )
    )
    if exclude_schedule_id is not None:
        stmt = stmt.where(ScoutSchedule.id != exclude_schedule_id)
    return int(db.scalar(stmt) or 0)


def _workspace_lock_select(workspace_id: str):
    """The ``SELECT ... FOR UPDATE`` used to lock a workspace row for the cap check.

    Factored out so a test can compile it against the PostgreSQL dialect and prove it
    emits ``FOR UPDATE`` without a live database.
    """
    return select(Workspace.id).where(Workspace.id == workspace_id).with_for_update()


def _lock_workspace_for_cap(db: Session, workspace_id: str) -> None:
    """Serialize the four-active-schedule cap check for one workspace.

    The cap is a check-then-act invariant: two concurrent create/resume calls could
    each read "three enabled" and both proceed, leaving five. We take a row lock on
    the *stable* workspace row before counting, so the count + enable is one atomic
    critical section held until the surrounding request transaction commits. On
    PostgreSQL this is ``SELECT ... FOR UPDATE``: a second enable for the same
    workspace blocks until the first commits, then re-reads the true count. On SQLite
    ``FOR UPDATE`` is a no-op, but the engine already serializes writers on the single
    database file (``busy_timeout``), so the invariant still holds. The lock is scoped
    to one workspace row, so it never contends across tenants.
    """
    db.execute(_workspace_lock_select(workspace_id))


def _has_pending_tick(db: Session, *, schedule: ScoutSchedule) -> bool:
    """True when a live ``scout_schedule.tick`` job already exists for this schedule.

    A schedule owns exactly one request, so a tick in an enqueued/active status for
    that request is *this* schedule's chain. Used to (a) tell an actually-running
    schedule apart from an enabled-but-inert one (:func:`derive_schedule_state`), and
    (b) keep activation idempotent — never seed a second tick when the chain is
    already live.
    """
    found = db.scalar(
        select(Job.id)
        .where(
            Job.organization_id == schedule.organization_id,
            Job.workspace_id == schedule.workspace_id,
            Job.scout_request_id == schedule.scout_request_id,
            Job.job_type == JobType.SCOUT_SCHEDULE_TICK.value,
            Job.status.in_(_INFLIGHT_STATUSES),
        )
        .limit(1)
    )
    return found is not None


@dataclass(frozen=True, slots=True)
class TickFailure:
    """The most recently finished tick of a schedule, when that tick failed.

    Carries only a timestamp and the stable, secret-free error classification the job
    store already recorded — never a payload, summary text or job id.
    """

    failed_at: datetime
    job_status: str
    error_code: str | None


def last_tick_failure(db: Session, schedule: ScoutSchedule) -> TickFailure | None:
    """Return the schedule's last *finished* tick if it failed, else ``None``.

    "Finished" means the tick reached ``succeeded``, ``failed`` or ``dead_lettered``;
    the newest of those by ``completed_at`` decides. A failure is therefore reported
    until a later tick of the same schedule succeeds — the moment recurrence is
    healthy again the signal clears on its own, with nothing persisted to reset.

    Scoped to *this* schedule through the tick idempotency key
    (``schedule-tick:{schedule_id}:…``), not merely the request, so a schedule that
    was deleted and re-created for the same request never inherits its
    predecessor's failures. Tenant columns are matched too.
    """
    row = db.execute(
        select(Job.status, Job.completed_at, Job.last_error_code)
        .where(
            Job.organization_id == schedule.organization_id,
            Job.workspace_id == schedule.workspace_id,
            Job.scout_request_id == schedule.scout_request_id,
            Job.job_type == JobType.SCOUT_SCHEDULE_TICK.value,
            Job.idempotency_key.startswith(f"schedule-tick:{schedule.id}:", autoescape=True),
            Job.status.in_(_FINISHED_TICK_STATUSES),
            Job.completed_at.is_not(None),
        )
        .order_by(Job.completed_at.desc(), Job.created_at.desc())
        .limit(1)
    ).first()
    if row is None or row.status not in _FAILED_TICK_STATUSES:
        return None
    return TickFailure(
        failed_at=row.completed_at,
        job_status=row.status,
        error_code=row.last_error_code,
    )


def derive_schedule_state(db: Session, schedule: ScoutSchedule) -> ScheduleState:
    """Classify a schedule's lifecycle state honestly from observed evidence.

    * ``paused`` — the row is disabled and drives no work.
    * ``active`` — enabled *and* a live tick chain exists, so runs are really being
      fanned out.
    * ``stalled`` — enabled, no live tick chain, and the last tick that finished
      *failed* (P6-PLAT-6). The failure hook normally re-seeds the next occurrence,
      so a schedule only lands here when it could not (the capability was off for
      the workspace at the time, or the re-seed itself failed). It is distinct from
      ``activation_required`` so a broken chain is never mistaken for one that was
      simply never started; resume/activate restarts it.
    * ``activation_required`` — enabled but no live tick exists yet. This is the
      dark-deploy / restart-safe state: a schedule created while scheduling was
      unavailable for its workspace is intentionally *not* auto-seeded; it stays
      inert until an explicit resume/activate action starts the chain.

    The state is derived, never persisted, so it can never disagree with the actual
    job state.
    """
    if not schedule.enabled:
        return ScheduleState.PAUSED
    if _has_pending_tick(db, schedule=schedule):
        return ScheduleState.ACTIVE
    if last_tick_failure(db, schedule) is not None:
        return ScheduleState.STALLED
    return ScheduleState.ACTIVATION_REQUIRED


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
    2. **Capability gate.** While scheduling resolves disabled for the schedule's
       workspace (:func:`resolve_scheduling_capability` — the shipped default) the
       tick is a self-terminating no-op: no run, no successor. This is the
       dark-deploy guarantee — zero recurring work until the capability is enabled
       for that workspace.
    3. **Enabled gate.** A paused schedule likewise stops the chain.
    4. **Cancellation.** An operator-cancelled tick stops the chain.
    5. **Request pause (D-25).** While the scout request itself is ``paused`` no run is
       fanned out (``request_paused``), but the schedule stays enabled and the chain
       continues exactly like a coalesced tick: resuming the request lets scheduled
       scouting pick up at the next boundary without the customer touching the
       schedule. The request pause is honoured here, not by pausing the schedule row.
    6. **Overlap coalescing.** If the request already has an execution in flight the
       scheduled run is *skipped* (not queued twice), but the schedule continues.
    7. **Fan-out.** Otherwise enqueue exactly one ``scout_request.execute`` run,
       idempotent on ``schedule:{id}:{occurrence}`` and labelled ``scheduled``.
    8. **Self-chain.** Compute the next future occurrence (missed boundaries
       skipped, never back-filled), enqueue the successor tick, and advance
       ``last_tick_at`` / ``next_run_at``.

    The function never commits or rolls back — the worker owns the transaction.
    """
    now = now or utcnow()
    schedule = db.get(ScoutSchedule, schedule_id)
    if schedule is None:
        return {"outcome": "schedule_missing", "run_enqueued": False, "chained": False}
    if not _scheduling_enabled_for(db, schedule):
        return {"outcome": "feature_disabled", "run_enqueued": False, "chained": False}
    if not schedule.enabled:
        return {"outcome": "disabled", "run_enqueued": False, "chained": False}
    if is_cancelled is not None and is_cancelled():
        return {"outcome": "cancelled", "run_enqueued": False, "chained": False}

    request = db.get(ScoutRequest, schedule.scout_request_id)
    request_paused = request is not None and request.status == ScoutRequestStatus.PAUSED.value
    coalesced = not request_paused and has_active_execution(
        db,
        organization_id=schedule.organization_id,
        workspace_id=schedule.workspace_id,
        scout_request_id=schedule.scout_request_id,
    )
    # Execution controls (PROPOSED): with no execution in flight, a Scout that still has an
    # unresolved admission (K = 1: an orphaned or unknown-outcome one) fans out no run (it
    # could admit nothing); the chain continues, nothing is caught up. An in-flight run's own
    # admission is reported as the overlap it is (``coalesced``).
    admission_unresolved = (
        not request_paused
        and not coalesced
        and request is not None
        and _has_unresolved_admission(db, request.id)
    )
    run_enqueued = False
    if not request_paused and not coalesced and not admission_unresolved:
        enqueue_scout_request(
            db,
            organization_id=schedule.organization_id,
            workspace_id=schedule.workspace_id,
            scout_request_id=schedule.scout_request_id,
            location_id=schedule.location_id,
            request_id=schedule.scout_request_id,
            idempotency_key=f"schedule:{schedule.id}:{occurrence_at.isoformat()}",
            trigger=SCHEDULED_TRIGGER,
            # D-25 (PROPOSED): the run carries the fence it was fanned out under, so a
            # pause after this tick fences it even if the request is resumed first.
            run_fence=request.run_fence if request is not None else None,
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
        request_paused=request_paused,
    )
    if run_enqueued:
        outcome = "fanned_out"
    elif request_paused:
        outcome = REQUEST_PAUSED_OUTCOME
    elif admission_unresolved:
        outcome = ADMISSION_UNRESOLVED_OUTCOME
    else:
        outcome = "coalesced"
    return {
        "outcome": outcome,
        "run_enqueued": run_enqueued,
        "coalesced": coalesced,
        "chained": True,
        "next_run_at": next_occurrence.isoformat(),
    }


def recover_after_tick_failure(
    db: Session,
    *,
    schedule_id: str,
    occurrence_at: datetime,
    organization_id: str,
    workspace_id: str,
    job_status: str,
    error_code: str,
    now: datetime | None = None,
) -> dict:
    """Keep a recurring schedule alive after one of its ticks failed for good (P6-PLAT-6).

    Called from the ``scout_schedule.tick`` terminal-failure hook once a tick reached
    ``failed`` or ``dead_lettered`` — on either dead-letter path, a handler that
    exhausted its retries or a lease that expired with no attempts left. Without
    this, the chain is self-propagating with no external cron: the failed tick
    never enqueued its successor, so the schedule would stop forever without a
    trace.

    The failed occurrence itself is **skipped, never back-filled** (the same rule as
    every other missed boundary): the successor is the first interval boundary after
    ``occurrence_at`` that is strictly in the future, enqueued with the ordinary
    per-occurrence idempotency key, so a repeated hook can never fork the chain. The
    successor is seeded only when the schedule still exists in the job's own tenant
    scope, is enabled, has no live tick already, and scheduling resolves enabled for
    its workspace — exactly the conditions under which a healthy tick would have
    chained. ``last_tick_at`` is not advanced: no occurrence fired.

    Every call emits ``scout_schedule_tick_failed`` at ERROR (a scheduled occurrence
    was lost) with ``chained`` saying whether recurrence survived; the job-level
    ``jobs_failed_total`` / ``jobs_dead_lettered_total`` counters carry the matching
    ``job_type`` for metric alarms. The function writes and flushes, never commits —
    the worker owns the transaction.
    """
    now = now or utcnow()
    schedule = db.get(ScoutSchedule, schedule_id)
    chained = False
    next_occurrence: datetime | None = None
    if (
        schedule is None
        or schedule.organization_id != organization_id
        or schedule.workspace_id != workspace_id
    ):
        # Missing, or a row the job's durable tenant scope does not own: never touch it.
        outcome = "schedule_missing"
        schedule = None
    elif not schedule.enabled:
        outcome = "disabled"
    elif _has_pending_tick(db, schedule=schedule):
        # A live chain already exists (e.g. re-activated meanwhile); never fork it.
        outcome = "chain_live"
        chained = True
    elif not _scheduling_enabled_for(db, schedule):
        outcome = "feature_disabled"
    else:
        next_occurrence = next_future_occurrence(
            occurrence_at, schedule.interval_enum(), now
        )
        enqueue_schedule_tick(
            db, schedule=schedule, occurrence_at=next_occurrence, now=now
        )
        schedule.next_run_at = next_occurrence
        db.add(schedule)
        db.flush()
        outcome = "reseeded"
        chained = True

    log_event(
        logger,
        "scout_schedule_tick_failed",
        level=logging.ERROR,
        outcome=outcome,
        chained=chained,
        job_status=job_status,
        error_class=error_code,
        workspace_id=schedule.workspace_id if schedule is not None else workspace_id,
        scout_request_id=schedule.scout_request_id if schedule is not None else None,
    )
    return {
        "outcome": outcome,
        "chained": chained,
        "next_run_at": next_occurrence.isoformat() if next_occurrence else None,
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
    ``enabled`` and scheduling resolves enabled for the request's workspace — seeds the
    first tick one interval out.
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
        raise ConflictError("This request already has a schedule.")

    if enabled:
        # Lock the workspace row *before* counting so the cap check + insert is one
        # atomic critical section — two concurrent enabled creates can never both
        # squeeze past a stale "three enabled" read.
        _lock_workspace_for_cap(db, request.workspace_id)
        if _count_enabled(db, request.workspace_id) >= MAX_ACTIVE_SCHEDULES_PER_WORKSPACE:
            raise ConflictError(
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

    if enabled and next_run_at is not None and _scheduling_enabled_for(db, schedule):
        enqueue_schedule_tick(db, schedule=schedule, occurrence_at=next_run_at, now=now)

    record_audit(
        db,
        organization_id=schedule.organization_id,
        workspace_id=schedule.workspace_id,
        actor_user_id=actor_user_id,
        action=AuditAction.SCOUT_SCHEDULE_CREATED,
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
    in flight becomes a self-terminating no-op. No new work is enqueued. Pausing an
    already-paused schedule is an idempotent no-op — it neither mutates the row again
    nor writes a duplicate audit record.
    """
    if not schedule.enabled:
        return schedule
    schedule.enabled = False
    schedule.next_run_at = None
    db.add(schedule)
    db.flush()
    record_audit(
        db,
        organization_id=schedule.organization_id,
        workspace_id=schedule.workspace_id,
        actor_user_id=actor_user_id,
        action=AuditAction.SCOUT_SCHEDULE_PAUSED,
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
    """Resume (or activate) a schedule, recomputing the next occurrence from *now*.

    This is also the explicit *activation* path for a schedule that was created while
    the capability was dark: such a row is ``enabled`` yet has no live tick chain, and
    is never auto-started when the capability turns on — the customer must
    resume/activate it.

    Behaviour by state:

    * **already active** (enabled *and* a live tick exists) → an idempotent no-op: the
      row is returned unchanged, so resume never counts the schedule against itself,
      never resets ``next_run_at`` and never seeds a duplicate tick.
    * **paused or activation-required** → (re)enable it. Recurrence restarts from the
      resume moment (``now + interval``); the pause gap is never back-filled. The
      per-workspace active cap is re-checked under a workspace row lock, excluding this
      schedule from the count so it can never self-count. A fresh tick is seeded only
      while scheduling resolves enabled for the workspace *and* no tick chain already
      exists. This is also the explicit restart for a ``stalled`` schedule.
    """
    now = now or utcnow()

    # Already-active guard: leave a running schedule exactly as it is.
    if schedule.enabled and _has_pending_tick(db, schedule=schedule):
        return schedule

    # Lock the workspace and re-check the cap atomically, excluding this row so an
    # already-enabled-but-inert schedule never counts itself toward the limit.
    _lock_workspace_for_cap(db, schedule.workspace_id)
    if (
        _count_enabled(db, schedule.workspace_id, exclude_schedule_id=schedule.id)
        >= MAX_ACTIVE_SCHEDULES_PER_WORKSPACE
    ):
        raise ConflictError(
            "This workspace already has the maximum number of active schedules."
        )
    schedule.enabled = True
    schedule.next_run_at = now + interval_delta(schedule.interval_enum())
    db.add(schedule)
    db.flush()

    if _scheduling_enabled_for(db, schedule) and not _has_pending_tick(db, schedule=schedule):
        enqueue_schedule_tick(
            db, schedule=schedule, occurrence_at=schedule.next_run_at, now=now
        )

    record_audit(
        db,
        organization_id=schedule.organization_id,
        workspace_id=schedule.workspace_id,
        actor_user_id=actor_user_id,
        action=AuditAction.SCOUT_SCHEDULE_RESUMED,
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
        action=AuditAction.SCOUT_SCHEDULE_DELETED,
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
    "TickFailure",
    "create_schedule",
    "delete_schedule",
    "derive_schedule_state",
    "enqueue_schedule_tick",
    "has_active_execution",
    "interval_delta",
    "last_tick_failure",
    "next_future_occurrence",
    "pause_schedule",
    "recover_after_tick_failure",
    "resolve_scheduling_capability",
    "resume_schedule",
    "run_schedule_tick",
]
