"""Durable job handlers.

Each handler performs one job type. Handlers are thin: they validate their
payload, honour cancellation, delegate to existing domain logic, and return a
**safe** result summary (counts and status only — never secrets, raw payloads or
customer content). They must not commit or roll back; the worker owns the
transaction boundary so the job's bookkeeping and the handler's writes are one
atomic unit.

Importing this module registers every handler on the
:mod:`app.jobs.registry` registry.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.enums import ScoutRequestStatus
from app.core.logging import get_logger, log_event
from app.infra.vector import SignalVolumeAboveBoundError
from app.jobs.context import ExecutionContext
from app.jobs.pipeline import _run, metered_run, refused_run_stats
from app.jobs.registry import (
    HandlerContext,
    TerminalFailureContext,
    register_handler,
    register_terminal_failure_hook,
)
from app.jobs.status import JobErrorCode, JobExecutionError, JobType
from app.llm.metering import LLMMeter
from app.scouting_requests.models import ScoutRequest

logger = get_logger("signalnest.jobs.handlers")


def _record_refused_run(
    scout_request_id: str, context: ExecutionContext, exc: SignalVolumeAboveBoundError
) -> Callable[[Session], None]:
    """An after-rollback writer that settles a run refused at the in-process bound.

    The attempt is rolled back (the refusal happens before any signal is written),
    so the outcome is recorded in the worker's after-rollback transaction: the
    request becomes ``failed`` with ``stats`` carrying the stable reason code and
    counts (P6-PLAT-1 B). Tenant guard as in :func:`settle_failed_scout_request`:
    the context is the job row's, so a mis-enqueued id never settles another
    tenant's request.
    """

    def write(db: Session) -> None:
        request = db.get(ScoutRequest, scout_request_id)
        if request is None:
            return
        if (
            request.organization_id != context.organization_id
            or request.workspace_id != context.workspace_id
        ):
            return
        request.status = ScoutRequestStatus.FAILED.value
        request.stats = refused_run_stats(scanned=exc.scanned, bound=exc.bound)
        db.flush()
        log_event(
            logger,
            "scout_request.refused_at_bound",
            component="jobs",
            outcome="refused",
            reason=exc.reason,
            scanned=exc.scanned,
            bound=exc.bound,
        )

    return write


@register_handler(JobType.SCOUT_REQUEST_EXECUTE)
def execute_scout_request(ctx: HandlerContext) -> dict[str, Any]:
    """Run the scouting pipeline for one scout request.

    The tenant/location isolation guard inside :func:`app.jobs.pipeline._run`
    re-verifies that the loaded request matches the job's declared execution
    context, so a mis-enqueued id can never blend one market's data into
    another's.

    Model calls are paid for when they are made (P6-LLM-4). The run's usage rows
    are written in the worker's transaction, so a successful attempt commits them
    with its results. A failed attempt is rolled back and may be retried, so the
    same rows are also handed to the worker as an after-rollback write. Every
    attempt's spend then counts against the workspace's monthly budget, the next
    attempt's included.
    """
    scout_request_id = ctx.payload.get("scout_request_id")
    if not isinstance(scout_request_id, str) or not scout_request_id:
        raise JobExecutionError(
            JobErrorCode.VALIDATION,
            "scout_request.execute payload requires a string 'scout_request_id'",
        )

    # Honour cancellation requested before the attempt starts. The pipeline runs
    # as a single unit, so this is the safe interruption point.
    if ctx.is_cancelled():
        raise JobExecutionError(JobErrorCode.CANCELLED, "cancelled before execution")

    # The run is this job: the call cap counts its earlier attempts' recorded rows.
    meter = LLMMeter(get_settings(), job_id=ctx.job_id)
    ctx.after_rollback.append(meter.flush)
    with metered_run(meter):
        try:
            result = _run(ctx.db, scout_request_id, ctx.context)
        except SignalVolumeAboveBoundError as exc:
            # P6-PLAT-1 (strategy B): the run would index more signals than the
            # per-run in-process bound. Non-retryable - the condition is a property
            # of the request's source volume, not of this attempt - and recorded as
            # the run's visible outcome after the rollback (nothing was written).
            ctx.after_rollback.append(_record_refused_run(scout_request_id, ctx.context, exc))
            raise JobExecutionError(JobErrorCode.NON_RETRYABLE, exc.reason) from exc

    # ``_run`` returns only safe aggregate counts (scanned / noise_filtered /
    # signals_analyzed / opportunities), which is exactly the shape we persist.
    return dict(result)


@register_handler(JobType.SCOUT_SCHEDULE_TICK)
def execute_schedule_tick(ctx: HandlerContext) -> dict[str, Any]:
    """Advance one recurring schedule (SB-B).

    A tick never runs the scouting pipeline itself: it enqueues at most one
    ``scout_request.execute`` run and its own successor tick. All recurrence,
    dark-deploy, overlap-coalescing and missed-run-skip decisions live in
    :func:`app.scouting_requests.schedules.run_schedule_tick`; this handler only
    validates the payload and adapts the cancellation probe. It returns a safe
    summary (outcome + booleans + next occurrence) — never any payload or secret.
    """
    # Deferred import breaks the cycle: the enqueue service imports this module to
    # register handlers, and the schedule service imports the enqueue service.
    from app.scouting_requests.schedules import run_schedule_tick

    schedule_id = ctx.payload.get("schedule_id")
    occurrence_raw = ctx.payload.get("occurrence_at")
    if not isinstance(schedule_id, str) or not schedule_id:
        raise JobExecutionError(
            JobErrorCode.VALIDATION,
            "scout_schedule.tick payload requires a string 'schedule_id'",
        )
    if not isinstance(occurrence_raw, str) or not occurrence_raw:
        raise JobExecutionError(
            JobErrorCode.VALIDATION,
            "scout_schedule.tick payload requires an ISO 'occurrence_at'",
        )
    try:
        occurrence_at = datetime.fromisoformat(occurrence_raw)
    except ValueError as exc:
        raise JobExecutionError(
            JobErrorCode.VALIDATION,
            "scout_schedule.tick 'occurrence_at' is not a valid ISO datetime",
        ) from exc

    return run_schedule_tick(
        ctx.db,
        schedule_id=schedule_id,
        occurrence_at=occurrence_at,
        now=ctx.now,
        is_cancelled=ctx.is_cancelled,
    )


@register_terminal_failure_hook(JobType.SCOUT_SCHEDULE_TICK)
def recover_failed_schedule_tick(ctx: TerminalFailureContext) -> None:
    """Keep a recurring schedule alive after one of its ticks failed for good (P6-PLAT-6).

    A tick is the only thing that enqueues the next tick, so a tick that reaches
    ``failed`` or ``dead_lettered`` would otherwise end the schedule silently and
    forever. This hook hands the failure to
    :func:`app.scouting_requests.schedules.recover_after_tick_failure`, which re-seeds
    the next occurrence (skipping the failed one, never back-filling) under the same
    capability and enabled checks a healthy tick applies, and logs the loss.

    The tenant scope is the job row's (``ctx.context``), never the payload's, so a
    tampered payload naming another tenant's schedule is refused downstream. A
    payload this hook cannot parse is logged and left alone: there is no schedule it
    could safely identify. The worker owns the transaction: this writes and flushes,
    never commits.
    """
    from app.scouting_requests.schedules import recover_after_tick_failure

    schedule_id = ctx.payload.get("schedule_id")
    occurrence_raw = ctx.payload.get("occurrence_at")
    occurrence_at: datetime | None = None
    if isinstance(occurrence_raw, str) and occurrence_raw:
        try:
            occurrence_at = datetime.fromisoformat(occurrence_raw)
        except ValueError:
            occurrence_at = None
    if not isinstance(schedule_id, str) or not schedule_id or occurrence_at is None:
        log_event(
            logger,
            "scout_schedule_tick_failed",
            level=logging.ERROR,
            component="jobs",
            outcome="unrecoverable_payload",
            chained=False,
            job_status=ctx.status,
            error_class=ctx.error_code.value,
        )
        return

    recover_after_tick_failure(
        ctx.db,
        schedule_id=schedule_id,
        occurrence_at=occurrence_at,
        organization_id=ctx.context.organization_id,
        workspace_id=ctx.context.workspace_id,
        job_status=ctx.status,
        error_code=ctx.error_code.value,
        now=ctx.now,
    )


#: The in-flight states a scout request occupies only while a run is expected to
#: make progress. A final job failure means no run is coming, so a request left in
#: one of these is stranded: ``POST .../run`` rejects it with "already queued or
#: running", making the scout permanently un-runnable from the UI.
_IN_FLIGHT_SCOUT_STATUSES = frozenset(
    {ScoutRequestStatus.QUEUED.value, ScoutRequestStatus.RUNNING.value}
)


@register_terminal_failure_hook(JobType.SCOUT_REQUEST_EXECUTE)
def settle_failed_scout_request(ctx: TerminalFailureContext) -> None:
    """Settle a scout request whose execution job failed for the last time.

    Runs only on a *final* failure (``failed`` / ``dead_lettered``), never on a
    retryable attempt, and only from in-flight states — so a request a human has
    since paused, or one already settled, is left exactly as it is. ``failed`` is
    an existing settled state that ``POST .../run`` accepts, which is what makes
    the scout runnable again.

    The worker owns the transaction: this writes and flushes, never commits.
    """
    scout_request_id = ctx.payload.get("scout_request_id")
    if not isinstance(scout_request_id, str) or not scout_request_id:
        return

    request = ctx.db.get(ScoutRequest, scout_request_id)
    if request is None:
        return
    # Tenant guard, mirroring the pipeline's: the context is rebuilt from the
    # durable job row, so a mis-enqueued id can never settle another org's request.
    if (
        request.organization_id != ctx.context.organization_id
        or request.workspace_id != ctx.context.workspace_id
    ):
        return
    if request.status not in _IN_FLIGHT_SCOUT_STATUSES:
        return

    request.status = ScoutRequestStatus.FAILED.value
    ctx.db.flush()
    log_event(
        logger,
        "scout_request.settled_after_job_failure",
        component="jobs",
        outcome="failed",
        job_type=ctx.job_type,
        job_status=ctx.status,
        error_class=ctx.error_code.value,
    )


__all__ = [
    "execute_schedule_tick",
    "execute_scout_request",
    "recover_failed_schedule_tick",
    "settle_failed_scout_request",
]
