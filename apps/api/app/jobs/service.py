"""Enqueue service — the seam between callers and the durable job store.

Callers (HTTP handlers, schedulers, tests) enqueue work here rather than touching
:class:`~app.jobs.store.DurableJobStore` directly. This module:

* validates the job type against the handler registry (an unknown type is
  rejected **before** enqueue, so it never lands as un-runnable work),
* bounds the serialized payload size,
* computes a deterministic ``payload_hash`` from a versioned
  :class:`~app.jobs.contracts.JobEnvelope` (so a reused idempotency key with a
  *different* payload is detected as a conflict), and
* delegates persistence + idempotency to the store.

Importing this module also imports :mod:`app.jobs.handlers`, guaranteeing every
handler is registered wherever enqueue is available.
"""

from __future__ import annotations

import json
from datetime import datetime
from logging import WARNING

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import RedisNotifyFailedError
from app.core.log_context import bound_context, new_correlation_id
from app.core.logging import get_logger, log_event
from app.core.metrics import JOBS_ENQUEUED_TOTAL, REDIS_NOTIFY_TOTAL, get_metrics
from app.core.tracing import JOB_ENQUEUE, REDIS_NOTIFY, inject_context, start_span
from app.jobs import handlers as _handlers  # noqa: F401 — registers handlers on import
from app.jobs.context import ExecutionContext
from app.jobs.contracts import CURRENT_CONTRACT_VERSION, JobEnvelope
from app.jobs.coordination import JobNotifier, NullJobNotifier, build_job_notifier
from app.jobs.models import Job
from app.jobs.registry import is_known_job_type
from app.jobs.status import ENQUEUED_STATUSES, JobErrorCode, JobExecutionError, JobStatus, JobType
from app.jobs.store import job_store

logger = get_logger("signalnest.jobs.service")

#: Process-wide wake-up notifier (best-effort). Built lazily so importing this
#: module never requires a coordination backend in local mode.
_notifier: JobNotifier | None = None


def _get_notifier() -> JobNotifier:
    global _notifier
    if _notifier is None:
        _notifier = build_job_notifier()
    return _notifier


def shutdown_notifier() -> None:
    """Close the process-wide wake-up notifier if one was built (best-effort).

    Idempotent: resets the singleton so a later enqueue rebuilds it. Any close
    failure is swallowed — shutdown must never be blocked by coordination cleanup.
    """
    global _notifier
    if _notifier is None:
        return
    try:
        _notifier.close()
    except Exception:  # pragma: no cover - best-effort cleanup
        pass
    finally:
        _notifier = None


def _notify_available(job: Job) -> None:
    """Best-effort wake-up for a freshly enqueued, immediately-due job.

    Coordination only: the job is already persisted, so any failure here is a
    warning — a worker's bounded DB poll still finds the job. Scheduled (future)
    jobs are not signalled; they become due later and are found by polling.
    """
    if JobStatus(job.status) not in ENQUEUED_STATUSES or job.status == JobStatus.SCHEDULED.value:
        return
    notifier = _get_notifier()
    if isinstance(notifier, NullJobNotifier):
        # No wake-up transport is selected (P6-PLAT-2): nothing is published, so no
        # Redis span or metric is recorded for a call that never reaches Redis.
        return
    metrics = get_metrics()
    # A child span for the wake-up so a coordination degradation is visible in the
    # trace separately from the (already-durable) enqueue, and never fails it.
    with start_span(
        REDIS_NOTIFY, kind="producer", attributes={"component": "jobs", "dependency": "redis"}
    ) as span:
        try:
            notifier.notify_job_available()
            metrics.increment(REDIS_NOTIFY_TOTAL, dependency="redis", outcome="success")
            span.set_attribute("outcome", "success")
        except RedisNotifyFailedError:
            # A notify failure is a *coordination* degradation, tracked separately from
            # an enqueue failure (the job is already durable and will be found by
            # polling).
            metrics.increment(REDIS_NOTIFY_TOTAL, dependency="redis", outcome="failure")
            span.set_attribute("outcome", "degraded")
            log_event(
                logger, "job.notify_failed", level=WARNING, component="jobs", outcome="degraded"
            )
        except Exception:  # pragma: no cover - defensive: never fail enqueue on notify
            metrics.increment(REDIS_NOTIFY_TOTAL, dependency="redis", outcome="failure")
            span.set_attribute("outcome", "degraded")
            log_event(
                logger, "job.notify_error", level=WARNING, component="jobs", outcome="degraded"
            )


def _payload_hash(job_type: str, context: ExecutionContext, payload: dict) -> str:
    """Deterministic hash over (version, type, tenant scope, payload)."""
    envelope = JobEnvelope(
        job_name=job_type,
        context=context,
        payload=payload,
    )
    return envelope.envelope_hash


def enqueue_job(
    db: Session,
    *,
    job_type: str | JobType,
    context: ExecutionContext,
    payload: dict,
    location_id: str | None = None,
    scout_request_id: str | None = None,
    idempotency_key: str | None = None,
    max_attempts: int | None = None,
    priority: int = 0,
    scheduled_for: datetime | None = None,
    now: datetime | None = None,
) -> Job:
    """Validate and persist a durable job. Returns the (possibly existing) row.

    Raises :class:`~app.jobs.status.JobExecutionError` for an unknown job type
    (``UNSUPPORTED_TYPE``) or an oversized payload (``PAYLOAD_TOO_LARGE``) — both
    permanent conditions rejected before anything is enqueued.
    """
    settings = get_settings()
    type_str = job_type.value if isinstance(job_type, JobType) else job_type

    if not is_known_job_type(type_str):
        raise JobExecutionError(
            JobErrorCode.UNSUPPORTED_TYPE,
            f"No handler registered for job type '{type_str}'",
        )

    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    size = len(encoded.encode("utf-8"))
    if size > settings.job_max_payload_bytes:
        raise JobExecutionError(
            JobErrorCode.PAYLOAD_TOO_LARGE,
            f"payload is {size} bytes; limit is {settings.job_max_payload_bytes}",
        )

    correlation_id = new_correlation_id()
    # Open the enqueue span first so its context is the parent persisted on the row;
    # the worker restores it to link execution back to this enqueue. Only a recording
    # span yields a traceparent to persist — with tracing disabled the column stays
    # NULL and the worker simply starts a fresh root span.
    with start_span(
        JOB_ENQUEUE, kind="producer", attributes={"component": "jobs", "job.type": type_str}
    ) as span:
        trace_context = inject_context(span) if span.recording else None
        job = job_store.enqueue(
            db,
            organization_id=context.organization_id,
            workspace_id=context.workspace_id,
            job_type=type_str,
            payload=payload,
            payload_hash=_payload_hash(type_str, context, payload),
            contract_version=CURRENT_CONTRACT_VERSION,
            location_id=location_id if location_id is not None else context.location_id,
            scout_request_id=scout_request_id,
            idempotency_key=idempotency_key,
            max_attempts=(
                max_attempts if max_attempts is not None else settings.job_default_max_attempts
            ),
            priority=priority,
            scheduled_for=scheduled_for,
            correlation_id=correlation_id,
            trace_context=trace_context,
            now=now,
        )
        span.set_attribute("job.status", job.status)
        # Bind the (possibly pre-existing, for an idempotent hit) correlation id so the
        # enqueue event is followable without logging the raw job/tenant identifiers.
        with bound_context(job_correlation_id=job.correlation_id or correlation_id):
            log_event(
                logger,
                "job.enqueued",
                component="jobs",
                outcome="enqueued",
                job_type=type_str,
                job_status=job.status,
            )
            # The caller owns the surrounding transaction; the row is persisted here, so
            # this is the single choke point through which every enqueue passes.
            get_metrics().increment(
                JOBS_ENQUEUED_TOTAL, outcome="enqueued", job_type=type_str, job_status=job.status
            )
            _notify_available(job)
    return job


def enqueue_scout_request(
    db: Session,
    *,
    organization_id: str,
    workspace_id: str,
    scout_request_id: str,
    location_id: str | None = None,
    campaign_id: str | None = None,
    request_id: str | None = None,
    trace_id: str | None = None,
    idempotency_key: str | None = None,
    trigger: str | None = None,
    run_fence: int | None = None,
    prior_status: str | None = None,
    now: datetime | None = None,
) -> Job:
    """Enqueue a durable ``scout_request.execute`` job for one scout request.

    ``trigger`` is an optional, server-written provenance marker recorded on the
    payload (e.g. ``"scheduled"`` for a recurrence-driven run); the manual-run path
    passes none. The marker is read only by the customer-safe run-history projection
    to label how a run was enqueued; it never alters execution, idempotency or
    isolation.

    The payload hash is computed here, from the payload as enqueued, and is what the
    enqueue's idempotency comparison uses. It is not recomputed later: a scheduled job's
    first claim adds ``prior_status`` to the stored payload (below) while the hash, as
    shown in the operator view too, stays that of the payload as enqueued.

    ``run_fence`` (D-25 cooperative pause, PROPOSED; ``app.jobs.admission``) is the
    request's fence at enqueue time, recorded on the payload. The worker claims the run
    only while the request still has that fence, so a pause after this enqueue fences the
    job even once the request is resumed. A payload without it (legacy jobs, the demo
    seed) is fence 0.

    ``prior_status`` (D-25 recovery contract §9.5.5, PROPOSED) is the settled status the
    Scout had before this run was requested (Run now passes the status it replaced with
    ``queued``, so a manual run's payload now carries it). A Cancel returns the Scout to
    it. A job without one (a scheduled run) records it at its first claim.
    """
    context = ExecutionContext.for_scout_request(
        organization_id=organization_id,
        workspace_id=workspace_id,
        location_id=location_id,
        campaign_id=campaign_id,
        request_id=request_id,
        trace_id=trace_id,
    )
    payload: dict = {"scout_request_id": scout_request_id}
    if trigger is not None:
        payload["trigger"] = trigger
    if run_fence is not None:
        payload["run_fence"] = run_fence
    if prior_status is not None:
        payload["prior_status"] = prior_status
    return enqueue_job(
        db,
        job_type=JobType.SCOUT_REQUEST_EXECUTE,
        context=context,
        payload=payload,
        location_id=location_id,
        scout_request_id=scout_request_id,
        idempotency_key=idempotency_key,
        now=now,
    )


__all__ = ["enqueue_job", "enqueue_scout_request"]
