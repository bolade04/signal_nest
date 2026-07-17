"""Read-only run-history service for a scouting request (SB-A).

Projects the durable ``Job`` rows that belong to one scout request into a bounded,
customer-safe :class:`~app.scouting_requests.schemas.RunHistoryOut`. It mirrors the
intelligence read pattern:

* the route has already authorized the request within the workspace (``_get_scoped``)
  and resolved the tenant (``TenantContext``); this service only *reads*, filters by
  the tenant + request scope, maps and bounds — it never writes, never enqueues, never
  touches the durable-job state machine, and never changes job-execution behaviour;
* **customer-safe projection** — every exposed field already rides the redacted
  ``JobOut`` boundary. Payloads, payload hashes, idempotency/lease tokens, worker/host
  ids, correlation/trace context and the free-form ``last_error_summary`` are read
  server-side at most (for the trigger derivation) and **never** returned; the coarse
  ``last_error_code`` enum is the only error signal surfaced;
* **honest-unknown over guessing** — ``trigger`` and ``is_simulated`` are only asserted
  when the row actually carries evidence for them, otherwise they degrade to
  ``unknown``/``null`` rather than being inferred from scheduling columns or config.

No migration and no new table: the history is a query over the existing ``jobs`` rows.
"""

from __future__ import annotations

import time

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.logging import get_logger, log_event
from app.jobs.models import Job
from app.jobs.status import JobType
from app.scouting_requests.schemas import (
    RunHistoryOut,
    RunItem,
    RunStats,
    TriggerType,
)

logger = get_logger("signalnest.scouting.run_history")

#: Pagination contract (also enforced at the route boundary via ``Query`` bounds; the
#: service re-clamps so a direct call can never over-fetch).
DEFAULT_LIMIT = 20
MAX_LIMIT = 100

_STAT_KEYS = ("scanned", "noise_filtered", "signals_analyzed", "opportunities")


def _clamp_limit(limit: int) -> int:
    return max(1, min(MAX_LIMIT, int(limit)))


def _clamp_offset(offset: int) -> int:
    return max(0, int(offset))


def _derive_trigger(job: Job) -> TriggerType:
    """Classify how the run was enqueued without ever exposing the payload.

    An explicit, server-written ``trigger`` marker on the payload wins (forward
    compatibility for a future scheduled path). Absent a marker, only the manual run
    path exists today for ``scout_request.execute`` jobs, so such a job is ``manual``;
    anything else is ``unknown``. We never infer ``scheduled`` from ``scheduled_for``
    or any other scheduling column.
    """
    payload = job.payload if isinstance(job.payload, dict) else {}
    marker = payload.get("trigger")
    if marker == TriggerType.SCHEDULED.value:
        return TriggerType.SCHEDULED
    if marker == TriggerType.MANUAL.value:
        return TriggerType.MANUAL
    if job.job_type == JobType.SCOUT_REQUEST_EXECUTE.value:
        return TriggerType.MANUAL
    return TriggerType.UNKNOWN


def _derive_simulated(job: Job) -> bool | None:
    """Report simulation status only when the row explicitly records it.

    Real runs carry a stats-only ``result_summary`` with no such flag, so they map to
    ``None`` ("unknown") — never a fabricated ``True``/``False``. A future live run
    must therefore never be mislabelled as simulated.
    """
    summary = job.result_summary if isinstance(job.result_summary, dict) else {}
    value = summary.get("is_simulated")
    return value if isinstance(value, bool) else None


def _map_stats(job: Job) -> RunStats | None:
    """Map the durable job's aggregate ``result_summary`` counts, or ``None`` when a
    run has not produced a (complete) summary yet. Counts are floored at 0."""
    summary = job.result_summary
    if not isinstance(summary, dict) or not all(k in summary for k in _STAT_KEYS):
        return None
    try:
        return RunStats(
            scanned=max(0, int(summary["scanned"])),
            noise_filtered=max(0, int(summary["noise_filtered"])),
            signals_analyzed=max(0, int(summary["signals_analyzed"])),
            opportunities=max(0, int(summary["opportunities"])),
        )
    except (TypeError, ValueError):
        return None


def _map_run(job: Job) -> RunItem:
    return RunItem(
        id=job.id,
        status=job.status,
        trigger=_derive_trigger(job),
        is_simulated=_derive_simulated(job),
        attempt_count=job.attempt_count,
        max_attempts=job.max_attempts,
        last_error_code=job.last_error_code,
        scheduled_for=job.scheduled_for,
        started_at=job.started_at,
        completed_at=job.completed_at,
        cancel_requested_at=job.cancel_requested_at,
        cancelled_at=job.cancelled_at,
        created_at=job.created_at,
        updated_at=job.updated_at,
        stats=_map_stats(job),
    )


def get_run_history(
    db: Session,
    *,
    organization_id: str,
    workspace_id: str,
    scout_request_id: str,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> RunHistoryOut:
    """Return the tenant + request-scoped run history, newest first.

    The caller must already have authorized the request within the workspace. The
    query is tenant-scoped (organization + workspace) *and* request-scoped, so a run
    can never leak across tenants, workspaces or requests. Ordering is
    ``created_at DESC, id DESC`` for a deterministic, stable page under equal
    timestamps. One bounded count + one bounded item query (no per-row fan-out).
    """
    started = time.perf_counter()
    limit = _clamp_limit(limit)
    offset = _clamp_offset(offset)

    base = select(Job).where(
        Job.organization_id == organization_id,
        Job.workspace_id == workspace_id,
        Job.scout_request_id == scout_request_id,
    )
    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    rows = list(
        db.execute(
            base.order_by(Job.created_at.desc(), Job.id.desc()).limit(limit).offset(offset)
        ).scalars()
    )
    items = [_map_run(job) for job in rows]

    log_event(
        logger,
        "scout_run_history_read",
        outcome="success",
        duration_ms=(time.perf_counter() - started) * 1000,
        workspace_id=workspace_id,
        scout_request_id=scout_request_id,
        returned=len(items),
        total=total,
    )
    return RunHistoryOut(items=items, total=total, limit=limit, offset=offset)
