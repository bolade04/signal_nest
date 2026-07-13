"""Typed handler registry for durable jobs.

A handler is the code that actually performs a job of a given
:class:`~app.jobs.status.JobType`. Handlers are registered by their constrained
job-type string and looked up by the worker at execution time.

A handler receives a :class:`HandlerContext` carrying everything it needs and
nothing it does not:

* ``db`` — the worker's active session (the handler must not commit/rollback; the
  worker owns the transaction boundary so job bookkeeping and the handler's writes
  succeed or fail together),
* ``context`` — the validated tenant/location :class:`ExecutionContext` rebuilt
  from the durable job row (never trusted from the message body),
* ``payload`` — the validated job payload,
* execution metadata (``job_id``, ``attempt``, ``worker_id``), and
* ``is_cancelled`` — a cheap cooperative-cancellation check the handler may poll.

The registry is intentionally separate from the legacy in-process
``app.infra.queue`` registry: that one runs work synchronously inside the request;
this one names durable, worker-executed units of work.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.jobs.context import ExecutionContext
from app.jobs.status import JobErrorCode, JobExecutionError, JobType


@dataclass(frozen=True)
class HandlerContext:
    """Everything a handler is given to execute one attempt of one job."""

    db: Session
    context: ExecutionContext
    payload: dict[str, Any]
    job_id: str
    attempt: int
    worker_id: str | None = None
    #: Cooperative cancellation probe. Returns ``True`` once the operator has
    #: requested cancellation; a long-running handler should poll it and raise
    #: :class:`~app.jobs.status.JobExecutionError` with ``JobErrorCode.CANCELLED``.
    is_cancelled: Callable[[], bool] = field(default=lambda: False)


#: A handler returns a *safe* result summary (counts/status only — never secrets,
#: raw payloads or customer content) that is stored on the job row.
JobHandler = Callable[[HandlerContext], dict[str, Any]]

_HANDLERS: dict[str, JobHandler] = {}


def register_handler(job_type: str | JobType) -> Callable[[JobHandler], JobHandler]:
    """Register ``fn`` as the handler for ``job_type`` (idempotent per import)."""
    key = job_type.value if isinstance(job_type, JobType) else job_type

    def _wrap(fn: JobHandler) -> JobHandler:
        _HANDLERS[key] = fn
        return fn

    return _wrap


def get_job_handler(job_type: str) -> JobHandler | None:
    """Return the handler for ``job_type`` or ``None`` if none is registered."""
    return _HANDLERS.get(job_type)


def is_known_job_type(job_type: str) -> bool:
    return job_type in _HANDLERS


def known_job_types() -> tuple[str, ...]:
    return tuple(sorted(_HANDLERS))


def resolve_handler(job_type: str) -> JobHandler:
    """Return the handler for ``job_type`` or raise a **non-retryable** error.

    An unknown job type is a permanent misconfiguration, not a transient fault,
    so retrying it would only burn attempts. The worker converts this into a
    dead-lettered/failed job with a stable ``UNSUPPORTED_TYPE`` error code.
    """
    handler = _HANDLERS.get(job_type)
    if handler is None:
        raise JobExecutionError(
            JobErrorCode.UNSUPPORTED_TYPE,
            f"No handler registered for job type '{job_type}'",
        )
    return handler


__all__ = [
    "HandlerContext",
    "JobHandler",
    "get_job_handler",
    "is_known_job_type",
    "known_job_types",
    "register_handler",
    "resolve_handler",
]
