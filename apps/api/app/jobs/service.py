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
from app.jobs.coordination import JobNotifier, build_job_notifier
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


def _notify_available(job: Job) -> None:
    """Best-effort wake-up for a freshly enqueued, immediately-due job.

    Coordination only: the job is already persisted, so any failure here is a
    warning — a worker's bounded DB poll still finds the job. Scheduled (future)
    jobs are not signalled; they become due later and are found by polling.
    """
    if JobStatus(job.status) not in ENQUEUED_STATUSES or job.status == JobStatus.SCHEDULED.value:
        return
    metrics = get_metrics()
    # A child span for the wake-up so a coordination degradation is visible in the
    # trace separately from the (already-durable) enqueue, and never fails it.
    with start_span(
        REDIS_NOTIFY, kind="producer", attributes={"component": "jobs", "dependency": "redis"}
    ) as span:
        try:
            _get_notifier().notify_job_available()
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
    now: datetime | None = None,
) -> Job:
    """Enqueue a durable ``scout_request.execute`` job for one scout request."""
    context = ExecutionContext.for_scout_request(
        organization_id=organization_id,
        workspace_id=workspace_id,
        location_id=location_id,
        campaign_id=campaign_id,
        request_id=request_id,
        trace_id=trace_id,
    )
    return enqueue_job(
        db,
        job_type=JobType.SCOUT_REQUEST_EXECUTE,
        context=context,
        payload={"scout_request_id": scout_request_id},
        location_id=location_id,
        scout_request_id=scout_request_id,
        idempotency_key=idempotency_key,
        now=now,
    )


__all__ = ["enqueue_job", "enqueue_scout_request"]
