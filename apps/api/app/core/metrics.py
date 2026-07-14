"""Provider-neutral metrics abstraction (Phase 3A.4b Batch 2).

Core code emits metrics through this thin seam and **never** imports a hosted
vendor SDK. The design keeps three guarantees that matter operationally:

* **Off by default.** The process-wide backend is a :class:`NoOpMetrics` unless a
  real one is installed, so metrics are strictly opt-in and tests never emit.
* **Bounded cardinality.** Every metric name must be in :data:`METRIC_NAMES` and
  every label key must be in :data:`ALLOWED_LABELS`. A name or label outside those
  sets raises immediately — a *coding* error surfaced at dev/test time — so a
  high-cardinality identifier (a job id, tenant id, URL, error message …) can never
  reach a time series.
* **Runtime-failure isolation.** A backend/exporter that raises while recording is
  swallowed (counted, never re-raised), so a telemetry outage can never break a
  request or a job. Validation errors are the one deliberate exception: they are
  bugs, not outages, and must fail loudly.

An in-memory backend is provided for assertions; a real exporter is deferred.
"""

from __future__ import annotations

import threading
from collections import defaultdict
from typing import Protocol, runtime_checkable

# --------------------------------------------------------------------------- #
# Cardinality policy
# --------------------------------------------------------------------------- #
#: The *only* label keys any metric may carry. Every entry is bounded to a small,
#: enumerable set of values. Anything not listed — ids, URLs, emails, messages,
#: tenant/organization/workspace/location, business names, tokens — is forbidden
#: because it would explode series cardinality and/or leak identifying data.
ALLOWED_LABELS: frozenset[str] = frozenset(
    {
        "service",
        "environment",
        "operation",
        "outcome",
        "status_class",
        "dependency",
        "worker_type",
        "job_type",
        "job_status",
        "error_class",
    }
)

# --------------------------------------------------------------------------- #
# Metric-name catalog
# --------------------------------------------------------------------------- #
# HTTP / API
HTTP_REQUESTS_TOTAL = "http_requests_total"
HTTP_REQUEST_DURATION_MS = "http_request_duration_ms"

# Durable jobs
JOBS_ENQUEUED_TOTAL = "jobs_enqueued_total"
JOBS_CLAIMED_TOTAL = "jobs_claimed_total"
JOBS_COMPLETED_TOTAL = "jobs_completed_total"
JOBS_FAILED_TOTAL = "jobs_failed_total"
JOBS_RETRIED_TOTAL = "jobs_retried_total"
JOBS_DEAD_LETTERED_TOTAL = "jobs_dead_lettered_total"
JOB_EXECUTION_DURATION_MS = "job_execution_duration_ms"
JOBS_QUEUE_DEPTH = "jobs_queue_depth"

# Workers
WORKER_POLL_TOTAL = "worker_poll_total"
WORKER_LEASE_RECOVERED_TOTAL = "worker_lease_recovered_total"
WORKER_ACTIVE = "worker_active"

# Dependencies (Redis / object store / database)
REDIS_NOTIFY_TOTAL = "redis_notify_total"
DEPENDENCY_OPERATION_TOTAL = "dependency_operation_total"
DEPENDENCY_OPERATION_DURATION_MS = "dependency_operation_duration_ms"

# Telemetry self-observation
TELEMETRY_FAILURES_TOTAL = "telemetry_failures_total"

#: Authoritative set of emittable metric names. A name outside this set is a bug.
METRIC_NAMES: frozenset[str] = frozenset(
    {
        HTTP_REQUESTS_TOTAL,
        HTTP_REQUEST_DURATION_MS,
        JOBS_ENQUEUED_TOTAL,
        JOBS_CLAIMED_TOTAL,
        JOBS_COMPLETED_TOTAL,
        JOBS_FAILED_TOTAL,
        JOBS_RETRIED_TOTAL,
        JOBS_DEAD_LETTERED_TOTAL,
        JOB_EXECUTION_DURATION_MS,
        JOBS_QUEUE_DEPTH,
        WORKER_POLL_TOTAL,
        WORKER_LEASE_RECOVERED_TOTAL,
        WORKER_ACTIVE,
        REDIS_NOTIFY_TOTAL,
        DEPENDENCY_OPERATION_TOTAL,
        DEPENDENCY_OPERATION_DURATION_MS,
        TELEMETRY_FAILURES_TOTAL,
    }
)


class MetricError(ValueError):
    """A metric name or label violates the cardinality policy (a coding bug)."""


def validate_metric(name: str, labels: dict[str, str]) -> None:
    """Reject unknown names and any label key outside :data:`ALLOWED_LABELS`.

    Raises :class:`MetricError`. This is intentionally *not* swallowed by the
    backend: a forbidden label is a programming mistake we want to fail loudly in
    tests, unlike a runtime exporter failure (which is isolated).
    """
    if name not in METRIC_NAMES:
        raise MetricError(f"unknown metric name '{name}'")
    forbidden = set(labels) - ALLOWED_LABELS
    if forbidden:
        raise MetricError(
            f"metric '{name}' uses forbidden label(s): {sorted(forbidden)}"
        )


def _label_key(labels: dict[str, str]) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((k, str(v)) for k, v in labels.items()))


# --------------------------------------------------------------------------- #
# Backend protocol + base
# --------------------------------------------------------------------------- #
@runtime_checkable
class MetricsBackend(Protocol):
    """A metrics sink. Implementations record; they never validate or raise."""

    def increment(self, name: str, *, amount: float = 1.0, **labels: str) -> None: ...
    def observe(self, name: str, value: float, **labels: str) -> None: ...
    def set_gauge(self, name: str, value: float, **labels: str) -> None: ...


class _SafeBackend:
    """Mixin that validates first, then isolates runtime recording failures.

    Validation errors propagate (bugs). Any other exception from the concrete
    ``_record_*`` hook is swallowed and counted in an internal failure tally, so a
    telemetry outage never breaks the caller.
    """

    def __init__(self) -> None:
        self._telemetry_failures = 0

    @property
    def telemetry_failures(self) -> int:
        return self._telemetry_failures

    def increment(self, name: str, *, amount: float = 1.0, **labels: str) -> None:
        validate_metric(name, labels)
        try:
            self._record_increment(name, amount, labels)
        except Exception:
            self._telemetry_failures += 1

    def observe(self, name: str, value: float, **labels: str) -> None:
        validate_metric(name, labels)
        try:
            self._record_observe(name, value, labels)
        except Exception:
            self._telemetry_failures += 1

    def set_gauge(self, name: str, value: float, **labels: str) -> None:
        validate_metric(name, labels)
        try:
            self._record_set_gauge(name, value, labels)
        except Exception:
            self._telemetry_failures += 1

    # Concrete backends override these three.
    def _record_increment(self, name: str, amount: float, labels: dict[str, str]) -> None:
        raise NotImplementedError

    def _record_observe(self, name: str, value: float, labels: dict[str, str]) -> None:
        raise NotImplementedError

    def _record_set_gauge(self, name: str, value: float, labels: dict[str, str]) -> None:
        raise NotImplementedError


class NoOpMetrics(_SafeBackend):
    """Default backend: validates labels (so bugs still surface) but records nothing."""

    def _record_increment(self, name: str, amount: float, labels: dict[str, str]) -> None:
        return None

    def _record_observe(self, name: str, value: float, labels: dict[str, str]) -> None:
        return None

    def _record_set_gauge(self, name: str, value: float, labels: dict[str, str]) -> None:
        return None


class InMemoryMetrics(_SafeBackend):
    """Test backend: keeps counters, histograms and gauges for assertions."""

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self._counters: dict[tuple, float] = defaultdict(float)
        self._histograms: dict[tuple, list[float]] = defaultdict(list)
        self._gauges: dict[tuple, float] = {}

    def _record_increment(self, name: str, amount: float, labels: dict[str, str]) -> None:
        with self._lock:
            self._counters[(name, _label_key(labels))] += amount

    def _record_observe(self, name: str, value: float, labels: dict[str, str]) -> None:
        with self._lock:
            self._histograms[(name, _label_key(labels))].append(value)

    def _record_set_gauge(self, name: str, value: float, labels: dict[str, str]) -> None:
        with self._lock:
            self._gauges[(name, _label_key(labels))] = value

    # -- assertion helpers --------------------------------------------------- #
    def counter_value(self, name: str, **labels: str) -> float:
        with self._lock:
            return self._counters.get((name, _label_key(labels)), 0.0)

    def observations(self, name: str, **labels: str) -> list[float]:
        with self._lock:
            return list(self._histograms.get((name, _label_key(labels)), []))

    def gauge_value(self, name: str, **labels: str) -> float | None:
        with self._lock:
            return self._gauges.get((name, _label_key(labels)))


# --------------------------------------------------------------------------- #
# Process-wide accessor
# --------------------------------------------------------------------------- #
_backend: MetricsBackend = NoOpMetrics()


def get_metrics() -> MetricsBackend:
    """Return the installed process-wide metrics backend (no-op by default)."""
    return _backend


def configure_metrics(backend: MetricsBackend | None) -> None:
    """Install a metrics backend (``None`` resets to a fresh :class:`NoOpMetrics`)."""
    global _backend
    _backend = backend if backend is not None else NoOpMetrics()


def telemetry_failure_count() -> int:
    """Isolated-and-counted runtime recording failures on the current backend."""
    return int(getattr(_backend, "telemetry_failures", 0))


def exporter_status(*, metrics_enabled: bool) -> str:
    """Operator-safe backend health: ``disabled`` / ``healthy`` / ``degraded``.

    ``disabled`` when metrics are off or the no-op backend is installed; otherwise
    ``degraded`` if any recording failure has been swallowed, else ``healthy``.
    Carries no identifiers, endpoints or credentials.
    """
    if not metrics_enabled or isinstance(_backend, NoOpMetrics):
        return "disabled"
    return "degraded" if telemetry_failure_count() > 0 else "healthy"


__all__ = [
    "ALLOWED_LABELS",
    "METRIC_NAMES",
    "MetricError",
    "MetricsBackend",
    "NoOpMetrics",
    "InMemoryMetrics",
    "get_metrics",
    "configure_metrics",
    "validate_metric",
    "telemetry_failure_count",
    "exporter_status",
    # names
    "HTTP_REQUESTS_TOTAL",
    "HTTP_REQUEST_DURATION_MS",
    "JOBS_ENQUEUED_TOTAL",
    "JOBS_CLAIMED_TOTAL",
    "JOBS_COMPLETED_TOTAL",
    "JOBS_FAILED_TOTAL",
    "JOBS_RETRIED_TOTAL",
    "JOBS_DEAD_LETTERED_TOTAL",
    "JOB_EXECUTION_DURATION_MS",
    "JOBS_QUEUE_DEPTH",
    "WORKER_POLL_TOTAL",
    "WORKER_LEASE_RECOVERED_TOTAL",
    "WORKER_ACTIVE",
    "REDIS_NOTIFY_TOTAL",
    "DEPENDENCY_OPERATION_TOTAL",
    "DEPENDENCY_OPERATION_DURATION_MS",
    "TELEMETRY_FAILURES_TOTAL",
]
