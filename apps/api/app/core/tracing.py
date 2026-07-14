"""Provider-neutral distributed tracing (Phase 3A.4b Batch 3).

Core code creates spans through this thin seam and **never** imports a hosted
vendor SDK. The design mirrors the Batch 2 metrics seam
(:mod:`app.core.metrics`) and keeps the same operational guarantees:

* **Off by default.** The process-wide tracer is a :class:`NoOpTracer` unless a
  real one is installed, so tracing is strictly opt-in and tests never export.
* **Bounded cardinality.** Every span name must be in :data:`SPAN_NAMES` and
  every attribute key must be in :data:`ALLOWED_SPAN_ATTRIBUTES`. A name or key
  outside those sets raises immediately — a *coding* error surfaced at dev/test
  time — so an identifier, URL, message or tenant key can never become a span
  name or a high-cardinality attribute.
* **Runtime-failure isolation.** A tracer/exporter that raises while recording or
  exporting is swallowed (counted, never re-raised), so a telemetry outage can
  never break a request, a DB transaction, job claiming, worker execution,
  readiness, or shutdown. Validation errors are the one deliberate exception:
  they are bugs, not outages, and must fail loudly.

Propagation uses the **W3C ``traceparent``** wire format so the seam is
OpenTelemetry-*compatible* without taking a hard dependency on the SDK; a real
OTLP exporter can be attached later behind :func:`configure_tracer` without
touching call sites. An in-memory exporter is provided for assertions.
"""

from __future__ import annotations

import os
import re
import secrets
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any, Protocol, runtime_checkable

from app.core.logging import trace_id_ctx
from app.core.redaction import sanitize_exception

# --------------------------------------------------------------------------- #
# Span-name catalog (low cardinality — never an id/URL/path)
# --------------------------------------------------------------------------- #
# HTTP
HTTP_REQUEST = "http.request"
# Jobs
JOB_ENQUEUE = "job.enqueue"
JOB_CLAIM = "job.claim"
JOB_EXECUTE = "job.execute"
JOB_COMPLETE = "job.complete"
JOB_FAIL = "job.fail"
JOB_RETRY = "job.retry"
JOB_RECOVER = "job.recover"
JOB_DEAD_LETTER = "job.dead_letter"
# Workers
WORKER_REGISTER = "worker.register"
WORKER_HEARTBEAT = "worker.heartbeat"
WORKER_POLL = "worker.poll"
WORKER_SHUTDOWN = "worker.shutdown"
# Dependencies
REDIS_NOTIFY = "redis.notify"
REDIS_CACHE = "redis.cache"
REDIS_LOCK = "redis.lock"
STORAGE_UPLOAD = "storage.upload"
STORAGE_SIGN_URL = "storage.sign_url"
DATABASE_TRANSACTION = "database.transaction"
READINESS_CHECK = "readiness.check"

#: The only span names any component may open. A name outside this set is a bug.
SPAN_NAMES: frozenset[str] = frozenset(
    {
        HTTP_REQUEST,
        JOB_ENQUEUE,
        JOB_CLAIM,
        JOB_EXECUTE,
        JOB_COMPLETE,
        JOB_FAIL,
        JOB_RETRY,
        JOB_RECOVER,
        JOB_DEAD_LETTER,
        WORKER_REGISTER,
        WORKER_HEARTBEAT,
        WORKER_POLL,
        WORKER_SHUTDOWN,
        REDIS_NOTIFY,
        REDIS_CACHE,
        REDIS_LOCK,
        STORAGE_UPLOAD,
        STORAGE_SIGN_URL,
        DATABASE_TRANSACTION,
        READINESS_CHECK,
    }
)

#: Span names sampled at a *reduced* rate at the root (health/readiness probes and
#: idle worker polls are high-volume, low-value). A sampled parent still forces the
#: child to record, so a real request that triggers a readiness check is unaffected.
LOW_VALUE_SPAN_NAMES: frozenset[str] = frozenset(
    {READINESS_CHECK, WORKER_POLL, WORKER_HEARTBEAT}
)

# --------------------------------------------------------------------------- #
# Attribute allow-list (bounded enums + normalized templates only)
# --------------------------------------------------------------------------- #
#: The *only* attribute keys a span may carry. Every entry is bounded to a small,
#: enumerable set of values or a normalized route template. Anything not listed —
#: ids, URLs, object keys, emails, messages, tenant/org/workspace/location,
#: business names, tokens, DSNs — is forbidden because it would explode span
#: cardinality and/or leak identifying data.
ALLOWED_SPAN_ATTRIBUTES: frozenset[str] = frozenset(
    {
        "service.name",
        "deployment.environment",
        "component",
        "operation",
        "outcome",
        "http.request.method",
        "http.response.status_code",
        "http.route",
        "job.type",
        "job.status",
        "worker.type",
        "dependency",
        "retryable",
        "recovered",
        "telemetry.enabled",
    }
)


class TraceError(ValueError):
    """A span name or attribute violates the cardinality policy (a coding bug)."""


def validate_span(name: str, attributes: dict[str, Any]) -> None:
    """Reject unknown span names and any attribute key outside the allow-list.

    Raises :class:`TraceError`. Like :func:`app.core.metrics.validate_metric`, this
    is intentionally *not* swallowed by the tracer: a forbidden attribute is a
    programming mistake we want to fail loudly in tests, unlike a runtime export
    failure (which is isolated).
    """
    if name not in SPAN_NAMES:
        raise TraceError(f"unknown span name '{name}'")
    forbidden = set(attributes) - ALLOWED_SPAN_ATTRIBUTES
    if forbidden:
        raise TraceError(f"span '{name}' uses forbidden attribute(s): {sorted(forbidden)}")


# --------------------------------------------------------------------------- #
# W3C trace-context (traceparent) propagation
# --------------------------------------------------------------------------- #
#: Strict W3C ``traceparent``: ``00-<32 hex trace-id>-<16 hex span-id>-<2 hex flags>``.
_TRACEPARENT_RE = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")
_ALL_ZERO_TRACE = "0" * 32
_ALL_ZERO_SPAN = "0" * 16
#: ``sampled`` flag bit in the trace-flags byte.
_FLAG_SAMPLED = 0x01


@dataclass(frozen=True)
class SpanContext:
    """The identity carried across a trace boundary (header or persisted job field)."""

    trace_id: str  # 32 hex
    span_id: str  # 16 hex
    sampled: bool
    remote: bool = False


def new_trace_id() -> str:
    return secrets.token_hex(16)


def new_span_id() -> str:
    return secrets.token_hex(8)


def format_traceparent(ctx: SpanContext) -> str:
    """Serialize a :class:`SpanContext` to a W3C ``traceparent`` string."""
    flags = _FLAG_SAMPLED if ctx.sampled else 0
    return f"00-{ctx.trace_id}-{ctx.span_id}-{flags:02x}"


def parse_traceparent(header: str | None) -> SpanContext | None:
    """Parse a W3C ``traceparent`` header, or ``None`` if missing/malformed.

    Strict: only version ``00``, exact field lengths, and neither the trace id nor
    the span id may be all-zero. Anything oversized, newline-bearing or otherwise
    off-format is rejected so a client can never inject an arbitrary context.
    """
    if not header:
        return None
    candidate = header.strip()
    match = _TRACEPARENT_RE.match(candidate)
    if match is None:
        return None
    trace_id, span_id, flags = match.group(1), match.group(2), match.group(3)
    if trace_id == _ALL_ZERO_TRACE or span_id == _ALL_ZERO_SPAN:
        return None
    sampled = bool(int(flags, 16) & _FLAG_SAMPLED)
    return SpanContext(trace_id=trace_id, span_id=span_id, sampled=sampled, remote=True)


# --------------------------------------------------------------------------- #
# Sampling (deterministic, parent-based)
# --------------------------------------------------------------------------- #
def _trace_id_is_sampled(trace_id: str, ratio: float) -> bool:
    """Deterministic ratio decision from the trace id (OTel-style, RNG-free).

    Uses the low 63 bits of the trace id so the same trace always samples the same
    way — which makes the in-memory tests deterministic without injecting an RNG.
    """
    if ratio >= 1.0:
        return True
    if ratio <= 0.0:
        return False
    try:
        value = int(trace_id[-16:], 16) & ((1 << 63) - 1)
    except ValueError:  # pragma: no cover - trace_id is always hex here
        return False
    threshold = int(ratio * (1 << 63))
    return value < threshold


def _decide_sampled(
    name: str, parent: SpanContext | None, *, ratio: float, trace_id: str
) -> bool:
    """Parent-based sampler: honor a parent's decision, else head-sample by ratio.

    Low-value root spans (readiness/idle polls) are sampled at a reduced rate
    (``ratio``/10) so a healthy, mostly-idle system does not flood the exporter.
    """
    if parent is not None:
        return parent.sampled
    effective = ratio / 10.0 if name in LOW_VALUE_SPAN_NAMES else ratio
    return _trace_id_is_sampled(trace_id, effective)


# --------------------------------------------------------------------------- #
# Span
# --------------------------------------------------------------------------- #
#: Span status values (bounded).
STATUS_UNSET = "unset"
STATUS_OK = "ok"
STATUS_ERROR = "error"


@dataclass
class Span:
    """A single unit of traced work. Carries only bounded, allow-listed attributes."""

    name: str
    context: SpanContext
    parent: SpanContext | None
    recording: bool
    kind: str = "internal"
    attributes: dict[str, Any] = field(default_factory=dict)
    status: str = STATUS_UNSET
    error_class: str | None = None
    _start: float = field(default_factory=perf_counter)
    duration_ms: float | None = None

    @property
    def trace_id(self) -> str:
        return self.context.trace_id

    @property
    def span_id(self) -> str:
        return self.context.span_id

    @property
    def sampled(self) -> bool:
        return self.context.sampled

    def set_attribute(self, key: str, value: Any) -> None:
        """Set one allow-listed attribute (validated; forbidden keys raise)."""
        validate_span(self.name, {key: value})
        if self.recording:
            self.attributes[key] = value

    def set_status(self, status: str) -> None:
        if status in (STATUS_UNSET, STATUS_OK, STATUS_ERROR):
            self.status = status

    def record_exception(self, exc: BaseException) -> None:
        """Record only the sanitized exception *class* + set error status.

        The message is never attached (it may carry a secret or customer content);
        the redaction layer's :func:`sanitize_exception` gives the safe class name.
        """
        self.error_class = sanitize_exception(exc).get("error_class")
        self.status = STATUS_ERROR

    def to_export(self) -> dict[str, Any]:
        """A serializable, secret-free view of the finished span (for exporters/tests)."""
        return {
            "name": self.name,
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent.span_id if self.parent else None,
            "kind": self.kind,
            "status": self.status,
            "error_class": self.error_class,
            "attributes": dict(self.attributes),
            "duration_ms": self.duration_ms,
        }


#: The currently-active span (per asyncio task / per worker thread).
_current_span_ctx: ContextVar[Span | None] = ContextVar("current_span", default=None)


def current_span() -> Span | None:
    return _current_span_ctx.get()


# --------------------------------------------------------------------------- #
# Tracer protocol + safe base
# --------------------------------------------------------------------------- #
@runtime_checkable
class Tracer(Protocol):
    """A span sink. Implementations export finished spans; they never validate."""

    def start_span(
        self,
        name: str,
        *,
        kind: str = "internal",
        parent: SpanContext | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> Any: ...

    def shutdown(self, timeout_seconds: float) -> bool: ...


class _SafeTracer:
    """Base tracer: validates first, then isolates runtime export failures.

    Validation errors propagate (bugs). Any other exception from the concrete
    ``_export`` hook is swallowed and counted, so a telemetry outage never breaks
    the caller. The span-start path itself is defensive too: if building/sampling a
    span ever fails at runtime, a non-recording span is returned rather than raising.
    """

    def __init__(self, *, sample_ratio: float = 1.0) -> None:
        self._sample_ratio = sample_ratio
        self._export_failures = 0
        self._last_failure_category: str | None = None
        self._lock = threading.Lock()

    # -- operator-safe counters --------------------------------------------
    @property
    def export_failures(self) -> int:
        return self._export_failures

    @property
    def last_failure_category(self) -> str | None:
        return self._last_failure_category

    @property
    def sample_ratio(self) -> float:
        return self._sample_ratio

    def _note_failure(self, category: str) -> None:
        with self._lock:
            self._export_failures += 1
            self._last_failure_category = category

    # -- span lifecycle -----------------------------------------------------
    @contextmanager
    def start_span(
        self,
        name: str,
        *,
        kind: str = "internal",
        parent: SpanContext | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> Iterator[Span]:
        attributes = attributes or {}
        # Validation is deliberate and loud: a bad name/attribute is a bug.
        validate_span(name, attributes)
        span = self._build_span(name, kind=kind, parent=parent, attributes=attributes)

        token = _current_span_ctx.set(span)
        # Only a *recording* span drives the log-correlation trace id, so tracing
        # being disabled never changes existing correlation behavior.
        trace_token = trace_id_ctx.set(span.trace_id) if span.recording else None
        try:
            yield span
        except BaseException as exc:
            span.record_exception(exc)
            raise
        finally:
            span.duration_ms = round((perf_counter() - span._start) * 1000, 3)
            if span.status is STATUS_UNSET:
                span.status = STATUS_OK
            if trace_token is not None:
                trace_id_ctx.reset(trace_token)
            _current_span_ctx.reset(token)
            if span.recording:
                try:
                    self._export(span)
                except Exception:
                    self._note_failure("export")

    def _build_span(
        self,
        name: str,
        *,
        kind: str,
        parent: SpanContext | None,
        attributes: dict[str, Any],
    ) -> Span:
        try:
            trace_id = parent.trace_id if parent is not None else new_trace_id()
            sampled = _decide_sampled(
                name, parent, ratio=self._sample_ratio, trace_id=trace_id
            )
            ctx = SpanContext(trace_id=trace_id, span_id=new_span_id(), sampled=sampled)
            recording = sampled and self._records()
            return Span(
                name=name,
                context=ctx,
                parent=parent,
                recording=recording,
                kind=kind,
                attributes=dict(attributes) if recording else {},
            )
        except Exception:  # pragma: no cover - defensive: never fail the caller
            self._note_failure("build")
            unsampled = SpanContext(
                trace_id=_ALL_ZERO_TRACE, span_id=_ALL_ZERO_SPAN, sampled=False
            )
            return Span(name=name, context=unsampled, parent=parent, recording=False)

    def _records(self) -> bool:
        """Whether this tracer keeps/exports sampled spans (no-op returns False)."""
        return True

    def _export(self, span: Span) -> None:
        raise NotImplementedError

    def shutdown(self, timeout_seconds: float) -> bool:
        """Flush within a bounded budget. Never raises; returns success best-effort."""
        try:
            return self._flush(timeout_seconds)
        except Exception:
            self._note_failure("flush")
            return False

    def _flush(self, timeout_seconds: float) -> bool:
        return True


class NoOpTracer(_SafeTracer):
    """Default tracer: validates (so bugs still surface) but records/exports nothing."""

    def _records(self) -> bool:
        return False

    def _export(self, span: Span) -> None:  # pragma: no cover - never called
        return None


class InMemoryTracer(_SafeTracer):
    """Test tracer: keeps finished, sampled spans for assertions."""

    def __init__(self, *, sample_ratio: float = 1.0) -> None:
        super().__init__(sample_ratio=sample_ratio)
        self._spans: list[Span] = []

    def _export(self, span: Span) -> None:
        with self._lock:
            self._spans.append(span)

    # -- assertion helpers --------------------------------------------------
    @property
    def spans(self) -> list[Span]:
        with self._lock:
            return list(self._spans)

    def finished(self, name: str | None = None) -> list[Span]:
        return [s for s in self.spans if name is None or s.name == name]

    def reset(self) -> None:
        with self._lock:
            self._spans.clear()


# --------------------------------------------------------------------------- #
# Process-wide accessor
# --------------------------------------------------------------------------- #
_tracer: _SafeTracer = NoOpTracer()


def get_tracer() -> _SafeTracer:
    """Return the installed process-wide tracer (no-op by default)."""
    return _tracer


def configure_tracer(tracer: _SafeTracer | None) -> None:
    """Install a tracer (``None`` resets to a fresh :class:`NoOpTracer`)."""
    global _tracer
    _tracer = tracer if tracer is not None else NoOpTracer()


@contextmanager
def start_span(
    name: str,
    *,
    kind: str = "internal",
    parent: SpanContext | None = None,
    attributes: dict[str, Any] | None = None,
) -> Iterator[Span]:
    """Open a span on the process tracer. If no explicit parent is given, the
    current in-context span (if any) is used, so nested calls form a tree."""
    if parent is None:
        active = current_span()
        if active is not None:
            parent = active.context
    with _tracer.start_span(name, kind=kind, parent=parent, attributes=attributes) as span:
        yield span


def inject_context(span: Span | None = None) -> str | None:
    """Serialize the active (or given) span to a ``traceparent`` for propagation."""
    span = span or current_span()
    if span is None:
        return None
    return format_traceparent(span.context)


def extract_context(traceparent: str | None) -> SpanContext | None:
    """Parse an inbound/persisted ``traceparent`` into a remote parent context."""
    return parse_traceparent(traceparent)


# --------------------------------------------------------------------------- #
# Configuration bootstrap
# --------------------------------------------------------------------------- #
def configure_tracing_from_settings(settings: Any) -> _SafeTracer:
    """Install the tracer implied by settings; return it (also the process tracer).

    Fails closed: with tracing disabled (the default), or on any construction
    error, a :class:`NoOpTracer` is installed so the application path is never
    affected. The optional OTLP exporter is import-guarded — if the OpenTelemetry
    packages are absent, tracing degrades to the in-memory/no-op tracer rather than
    raising at startup.
    """
    ratio = getattr(settings, "tracing_sample_ratio", 1.0)
    if not getattr(settings, "tracing_enabled", False):
        configure_tracer(NoOpTracer())
        return get_tracer()

    exporter = getattr(settings, "tracing_exporter", "none")
    try:
        if exporter == "memory":
            configure_tracer(InMemoryTracer(sample_ratio=ratio))
        elif exporter == "otlp":
            configure_tracer(_build_otlp_tracer(settings, ratio))
        else:
            configure_tracer(NoOpTracer())
    except Exception:
        # A collector that is unreachable or missing SDK packages must not stop the
        # process from starting; degrade to no-op.
        configure_tracer(NoOpTracer())
    return get_tracer()


def _build_otlp_tracer(settings: Any, ratio: float) -> _SafeTracer:  # pragma: no cover - optional
    """Construct an OTLP-backed tracer *iff* OpenTelemetry packages are installed.

    Import-guarded so the core never hard-depends on the SDK. When the packages are
    absent this raises, and :func:`configure_tracing_from_settings` degrades to a
    no-op tracer. The concrete SDK wiring is intentionally minimal here; the seam's
    value is that call sites never change when it is added.
    """
    __import__("opentelemetry")  # raises ImportError if the SDK is not installed
    # Deferred: real OTLP span-processor wiring. Until it lands, an enabled OTLP
    # exporter behaves as an in-memory tracer (spans are still created + bounded),
    # which keeps the seam exercised without a hosted dependency.
    return InMemoryTracer(sample_ratio=ratio)


def tracing_exporter_status(*, tracing_enabled: bool) -> str:
    """Operator-safe tracer health: ``disabled`` / ``healthy`` / ``degraded``.

    ``disabled`` when tracing is off or the no-op tracer is installed; otherwise
    ``degraded`` if any export/flush failure has been swallowed, else ``healthy``.
    Carries no endpoint, credential, trace id or span id.
    """
    if not tracing_enabled or isinstance(_tracer, NoOpTracer):
        return "disabled"
    return "degraded" if _tracer.export_failures > 0 else "healthy"


def trace_export_failure_count() -> int:
    """Isolated-and-counted export/flush failures on the current tracer."""
    return int(getattr(_tracer, "export_failures", 0))


def last_export_failure_category() -> str | None:
    """Bounded category of the most recent swallowed failure (or ``None``)."""
    return getattr(_tracer, "last_failure_category", None)


def sampling_ratio() -> float:
    return float(getattr(_tracer, "sample_ratio", 0.0))


def _shutdown_flush(timeout_seconds: float) -> bool:
    """Flush the process tracer within a bounded budget (never raises)."""
    return _tracer.shutdown(timeout_seconds)


# Allow an operator to force the no-op tracer regardless of other settings (a safe
# kill-switch that never depends on secrets).
if os.environ.get("TRACING_FORCE_DISABLED") == "1":  # pragma: no cover - ops override
    configure_tracer(NoOpTracer())


__all__ = [
    "ALLOWED_SPAN_ATTRIBUTES",
    "SPAN_NAMES",
    "LOW_VALUE_SPAN_NAMES",
    "TraceError",
    "Tracer",
    "NoOpTracer",
    "InMemoryTracer",
    "Span",
    "SpanContext",
    "STATUS_OK",
    "STATUS_ERROR",
    "STATUS_UNSET",
    "validate_span",
    "get_tracer",
    "configure_tracer",
    "configure_tracing_from_settings",
    "start_span",
    "current_span",
    "inject_context",
    "extract_context",
    "format_traceparent",
    "parse_traceparent",
    "new_trace_id",
    "new_span_id",
    "tracing_exporter_status",
    "trace_export_failure_count",
    "last_export_failure_category",
    "sampling_ratio",
    # span names
    "HTTP_REQUEST",
    "JOB_ENQUEUE",
    "JOB_CLAIM",
    "JOB_EXECUTE",
    "JOB_COMPLETE",
    "JOB_FAIL",
    "JOB_RETRY",
    "JOB_RECOVER",
    "JOB_DEAD_LETTER",
    "WORKER_REGISTER",
    "WORKER_HEARTBEAT",
    "WORKER_POLL",
    "WORKER_SHUTDOWN",
    "REDIS_NOTIFY",
    "REDIS_CACHE",
    "REDIS_LOCK",
    "STORAGE_UPLOAD",
    "STORAGE_SIGN_URL",
    "DATABASE_TRANSACTION",
    "READINESS_CHECK",
]
