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

from datetime import datetime
from typing import Any

from app.core.logging import get_logger
from app.jobs.pipeline import _run
from app.jobs.registry import HandlerContext, register_handler
from app.jobs.status import JobErrorCode, JobExecutionError, JobType

logger = get_logger("signalnest.jobs.handlers")


@register_handler(JobType.SCOUT_REQUEST_EXECUTE)
def execute_scout_request(ctx: HandlerContext) -> dict[str, Any]:
    """Run the scouting pipeline for one scout request.

    The tenant/location isolation guard inside :func:`app.jobs.pipeline._run`
    re-verifies that the loaded request matches the job's declared execution
    context, so a mis-enqueued id can never blend one market's data into
    another's.
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

    result = _run(ctx.db, scout_request_id, ctx.context)

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
        is_cancelled=ctx.is_cancelled,
    )


__all__ = ["execute_scout_request", "execute_schedule_tick"]
