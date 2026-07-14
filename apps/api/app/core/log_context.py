"""Ergonomic, context-safe helpers over the logging correlation context vars.

The underlying :class:`~contextvars.ContextVar`s live in :mod:`app.core.logging`.
This module adds:

* :func:`bound_context` — a context manager that sets one or more correlation
  fields and **restores the previous values on exit**, so nested operations (an
  HTTP request that enqueues a job, a worker that runs one job then the next) never
  leak context into their surroundings, even when the body raises.
* strict request-id acceptance / generation, shared by the HTTP middleware.
* correlation-id generation for durable jobs.

ContextVars are copied per asyncio task and are thread-local for the worker's
threads, so these helpers are safe under both the async request stack and the
thread-based worker without any locking.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from app.core.logging import (
    component_ctx,
    job_correlation_id_ctx,
    operation_ctx,
    request_id_ctx,
    trace_id_ctx,
    worker_type_ctx,
)

_FIELD_CTX: dict[str, ContextVar[str | None]] = {
    "request_id": request_id_ctx,
    "trace_id": trace_id_ctx,
    "job_correlation_id": job_correlation_id_ctx,
    "component": component_ctx,
    "worker_type": worker_type_ctx,
    "operation": operation_ctx,
}

#: Accepted inbound request-id shape: hex/UUID characters only, bounded length.
#: Anything else is rejected and a fresh id is generated (no log injection, no
#: unbounded-cardinality ids from clients).
_REQUEST_ID_RE = re.compile(r"^[0-9a-fA-F-]{8,64}$")


@contextmanager
def bound_context(**fields: str | None) -> Iterator[None]:
    """Set correlation ``fields`` for the block, restoring prior values on exit.

    Unknown field names are ignored. ``None`` values are skipped (the current value
    is left untouched). Restoration runs in a ``finally`` so an exception in the body
    never leaves context behind.
    """
    tokens = []
    try:
        for name, value in fields.items():
            ctx = _FIELD_CTX.get(name)
            if ctx is None or value is None:
                continue
            tokens.append((ctx, ctx.set(value)))
        yield
    finally:
        for ctx, token in reversed(tokens):
            ctx.reset(token)


def clear_context() -> None:
    """Reset every correlation field to ``None`` (belt-and-braces cleanup)."""
    for ctx in _FIELD_CTX.values():
        ctx.set(None)


def current_context() -> dict[str, Any]:
    """Snapshot the currently-bound correlation fields (for diagnostics/tests)."""
    return {name: ctx.get() for name, ctx in _FIELD_CTX.items() if ctx.get() is not None}


def normalize_request_id(candidate: str | None) -> str | None:
    """Return a valid, normalized request id or ``None`` if the candidate is unsafe.

    Accepts only bounded hex/UUID strings; rejects empty, oversized, or
    special-character input so a client can never inject an arbitrary id.
    """
    if not candidate:
        return None
    candidate = candidate.strip()
    if not _REQUEST_ID_RE.match(candidate):
        return None
    return candidate


def new_request_id() -> str:
    """Generate a fresh opaque request id (32 hex chars)."""
    return uuid.uuid4().hex


def new_correlation_id() -> str:
    """Generate a fresh opaque durable-job correlation id.

    Distinct from the database job id, lease token, worker id and tenant ids; safe
    for logs and support correlation, never used as a metric label.
    """
    return uuid.uuid4().hex


__all__ = [
    "bound_context",
    "clear_context",
    "current_context",
    "normalize_request_id",
    "new_request_id",
    "new_correlation_id",
]
