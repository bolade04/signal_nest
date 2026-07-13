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


__all__ = ["execute_scout_request"]
