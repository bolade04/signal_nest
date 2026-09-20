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
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.db.base import utcnow
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
    #: The single execution-boundary timestamp for this attempt, captured from the
    #: worker's clock. A handler that schedules follow-on work must derive every
    #: timestamp from this immutable value rather than reading the wall clock, so a
    #: pinned-clock worker (and the fan-out it produces) stay on one consistent clock.
    now: datetime = field(default_factory=utcnow)


#: A handler returns a *safe* result summary (counts/status only — never secrets,
#: raw payloads or customer content) that is stored on the job row.
JobHandler = Callable[[HandlerContext], dict[str, Any]]

_HANDLERS: dict[str, JobHandler] = {}


@dataclass(frozen=True)
class TerminalFailureContext:
    """What a terminal-failure hook is given after a job fails for the last time.

    The hook runs inside the worker's *failure* transaction, after the job's
    terminal transition has been applied under its lease fence and before that
    transition is committed, so the hook's writes and the job's terminal state
    are one atomic unit. Like a handler, the hook must not commit or roll back.

    ``context`` is rebuilt from the durable job row (never the message body), so
    a hook can never be tricked into widening its own tenant scope.
    """

    db: Session
    context: ExecutionContext
    payload: dict[str, Any]
    job_id: str
    job_type: str
    #: The terminal status the job just reached (``failed`` or ``dead_lettered``).
    status: str
    #: The stable, secret-free classification of the final attempt's failure.
    error_code: JobErrorCode
    attempt: int
    now: datetime


#: A terminal-failure hook reconciles *domain* state that the job was driving.
#: It returns nothing: the job's own outcome is already decided by the store.
JobTerminalFailureHook = Callable[[TerminalFailureContext], None]

#: Optional, keyed exactly like :data:`_HANDLERS`. Kept as a separate map so the
#: handler registry's value type — and therefore ``resolve_handler`` and every
#: caller of it — is unchanged.
_TERMINAL_FAILURE_HOOKS: dict[str, JobTerminalFailureHook] = {}


def register_handler(job_type: str | JobType) -> Callable[[JobHandler], JobHandler]:
    """Register ``fn`` as the handler for ``job_type`` (idempotent per import)."""
    key = job_type.value if isinstance(job_type, JobType) else job_type

    def _wrap(fn: JobHandler) -> JobHandler:
        _HANDLERS[key] = fn
        return fn

    return _wrap


def register_terminal_failure_hook(
    job_type: str | JobType,
) -> Callable[[JobTerminalFailureHook], JobTerminalFailureHook]:
    """Register ``fn`` as the terminal-failure hook for ``job_type``.

    A job type needs a hook only when a *final* failure leaves domain state that
    the job itself was responsible for advancing. Most job types need none, so
    the hook is optional and its absence is not an error.
    """
    key = job_type.value if isinstance(job_type, JobType) else job_type

    def _wrap(fn: JobTerminalFailureHook) -> JobTerminalFailureHook:
        _TERMINAL_FAILURE_HOOKS[key] = fn
        return fn

    return _wrap


def resolve_terminal_failure_hook(job_type: str) -> JobTerminalFailureHook | None:
    """Return the terminal-failure hook for ``job_type``, or ``None``."""
    return _TERMINAL_FAILURE_HOOKS.get(job_type)


def register_builtin_handlers() -> tuple[str, ...]:
    """Import and register every built-in handler; return what is registered.

    Registration is an import side effect of :mod:`app.jobs.handlers`. Naming it
    here makes that dependency **explicit and orderable** for every entrypoint
    that must have it — in particular the worker process, which otherwise has no
    import path to the handler module at all and would resolve nothing.

    Idempotent: re-importing is a no-op and re-registering rebinds the same
    functions to the same keys. The import is function-local because
    :mod:`app.jobs.handlers` imports *this* module, so a module-level import
    here would be a cycle.
    """
    from app.jobs import handlers as _handlers  # noqa: F401 — registers on import

    return known_job_types()


def builtin_job_types() -> tuple[str, ...]:
    """The authoritative set of job types the platform ships handlers for.

    This is the *expectation* against which the live registry is checked at
    worker startup; :func:`known_job_types` is the *observation*.
    """
    return tuple(sorted(t.value for t in JobType))


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
    "JobTerminalFailureHook",
    "TerminalFailureContext",
    "builtin_job_types",
    "get_job_handler",
    "is_known_job_type",
    "known_job_types",
    "register_builtin_handlers",
    "register_handler",
    "register_terminal_failure_hook",
    "resolve_handler",
    "resolve_terminal_failure_hook",
]
