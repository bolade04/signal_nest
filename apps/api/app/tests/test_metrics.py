"""Provider-neutral metrics abstraction tests (Phase 3A.4b Batch 2).

Covers the three guarantees the abstraction exists to provide: bounded metric
names, a strict label allow-list that rejects high-cardinality/identifying keys,
a no-op default that still validates, and runtime-failure isolation that keeps a
broken exporter from ever breaking the caller.
"""

from __future__ import annotations

import pytest

from app.core.metrics import (
    ALLOWED_LABELS,
    JOB_EXECUTION_DURATION_MS,
    JOBS_ENQUEUED_TOTAL,
    JOBS_QUEUE_DEPTH,
    METRIC_NAMES,
    InMemoryMetrics,
    MetricError,
    NoOpMetrics,
    _SafeBackend,
    configure_metrics,
    exporter_status,
    get_metrics,
    telemetry_failure_count,
    validate_metric,
)

# Keys that must never be accepted — ids, tenancy, free text, connection strings.
FORBIDDEN_LABELS = [
    "job_id",
    "organization_id",
    "workspace_id",
    "location_id",
    "user_id",
    "worker_id",
    "correlation_id",
    "url",
    "path",
    "email",
    "error_message",
    "business_name",
    "tenant",
]


# --------------------------------------------------------------------------- #
# Catalog + label policy
# --------------------------------------------------------------------------- #
def test_allowed_labels_are_all_bounded_dimensions() -> None:
    # Guardrail: the allow-list stays small and free of identifier-shaped keys.
    assert "job_id" not in ALLOWED_LABELS
    assert "organization_id" not in ALLOWED_LABELS
    assert ALLOWED_LABELS  # non-empty


def test_unknown_metric_name_is_rejected() -> None:
    with pytest.raises(MetricError):
        validate_metric("not_a_real_metric", {})


def test_known_metric_name_with_allowed_labels_ok() -> None:
    validate_metric(JOBS_ENQUEUED_TOTAL, {"outcome": "enqueued", "job_type": "scout"})


@pytest.mark.parametrize("label", FORBIDDEN_LABELS)
def test_forbidden_label_is_rejected(label: str) -> None:
    with pytest.raises(MetricError):
        validate_metric(JOBS_ENQUEUED_TOTAL, {label: "anything"})


def test_every_allowed_label_passes_validation() -> None:
    for label in ALLOWED_LABELS:
        validate_metric(JOBS_ENQUEUED_TOTAL, {label: "x"})


# --------------------------------------------------------------------------- #
# No-op default
# --------------------------------------------------------------------------- #
def test_default_backend_is_noop() -> None:
    configure_metrics(None)
    assert isinstance(get_metrics(), NoOpMetrics)


def test_noop_records_nothing_but_still_validates() -> None:
    m = NoOpMetrics()
    m.increment(JOBS_ENQUEUED_TOTAL, outcome="enqueued")  # accepted, no store
    with pytest.raises(MetricError):
        m.increment(JOBS_ENQUEUED_TOTAL, job_id="leak")  # still rejects forbidden


# --------------------------------------------------------------------------- #
# In-memory backend
# --------------------------------------------------------------------------- #
def test_in_memory_counter_accumulates_per_label_set() -> None:
    m = InMemoryMetrics()
    m.increment(JOBS_ENQUEUED_TOTAL, outcome="enqueued")
    m.increment(JOBS_ENQUEUED_TOTAL, outcome="enqueued")
    m.increment(JOBS_ENQUEUED_TOTAL, outcome="duplicate")
    assert m.counter_value(JOBS_ENQUEUED_TOTAL, outcome="enqueued") == 2
    assert m.counter_value(JOBS_ENQUEUED_TOTAL, outcome="duplicate") == 1
    assert m.counter_value(JOBS_ENQUEUED_TOTAL, outcome="missing") == 0


def test_in_memory_histogram_collects_observations() -> None:
    m = InMemoryMetrics()
    m.observe(JOB_EXECUTION_DURATION_MS, 12.0, job_type="scout", outcome="success")
    m.observe(JOB_EXECUTION_DURATION_MS, 8.0, job_type="scout", outcome="success")
    obs = m.observations(JOB_EXECUTION_DURATION_MS, job_type="scout", outcome="success")
    assert obs == [12.0, 8.0]


def test_in_memory_gauge_holds_last_value() -> None:
    m = InMemoryMetrics()
    m.set_gauge(JOBS_QUEUE_DEPTH, 5, job_status="pending")
    m.set_gauge(JOBS_QUEUE_DEPTH, 3, job_status="pending")
    assert m.gauge_value(JOBS_QUEUE_DEPTH, job_status="pending") == 3
    assert m.gauge_value(JOBS_QUEUE_DEPTH, job_status="running") is None


# --------------------------------------------------------------------------- #
# Runtime-failure isolation
# --------------------------------------------------------------------------- #
class _ExplodingMetrics(_SafeBackend):
    def _record_increment(self, name, amount, labels):  # noqa: ANN001
        raise RuntimeError("exporter down")

    def _record_observe(self, name, value, labels):  # noqa: ANN001
        raise RuntimeError("exporter down")

    def _record_set_gauge(self, name, value, labels):  # noqa: ANN001
        raise RuntimeError("exporter down")


def test_backend_runtime_failure_is_isolated_and_counted() -> None:
    m = _ExplodingMetrics()
    # A recording failure must never propagate to the caller.
    m.increment(JOBS_ENQUEUED_TOTAL, outcome="enqueued")
    m.observe(JOB_EXECUTION_DURATION_MS, 1.0, outcome="success")
    m.set_gauge(JOBS_QUEUE_DEPTH, 1)
    assert m.telemetry_failures == 3


def test_validation_error_is_not_swallowed_even_on_failing_backend() -> None:
    m = _ExplodingMetrics()
    # Forbidden label is a bug: it must raise before any recording is attempted.
    with pytest.raises(MetricError):
        m.increment(JOBS_ENQUEUED_TOTAL, job_id="leak")
    assert m.telemetry_failures == 0


# --------------------------------------------------------------------------- #
# Catalog integrity
# --------------------------------------------------------------------------- #
def test_catalog_contains_expected_families() -> None:
    for name in (JOBS_ENQUEUED_TOTAL, JOB_EXECUTION_DURATION_MS, JOBS_QUEUE_DEPTH):
        assert name in METRIC_NAMES


# --------------------------------------------------------------------------- #
# Operator-safe status helpers
# --------------------------------------------------------------------------- #
def test_exporter_status_disabled_when_metrics_off_or_noop() -> None:
    configure_metrics(None)  # NoOp default
    assert exporter_status(metrics_enabled=False) == "disabled"
    assert exporter_status(metrics_enabled=True) == "disabled"  # still no-op backend


def test_exporter_status_healthy_then_degraded() -> None:
    m = InMemoryMetrics()
    configure_metrics(m)
    try:
        assert exporter_status(metrics_enabled=True) == "healthy"
        assert telemetry_failure_count() == 0
    finally:
        configure_metrics(None)


def test_exporter_status_degraded_after_swallowed_failure() -> None:
    m = _ExplodingMetrics()
    configure_metrics(m)
    try:
        m.increment(JOBS_ENQUEUED_TOTAL, outcome="enqueued")  # swallowed, counted
        assert telemetry_failure_count() == 1
        assert exporter_status(metrics_enabled=True) == "degraded"
    finally:
        configure_metrics(None)
