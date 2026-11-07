"""CloudWatch Embedded Metric Format exporter tests (P6-INF-5).

Every assertion reads the EMF document the exporter actually writes — the JSON line
CloudWatch Logs would ingest — never the exporter's internal buffers: the document
shape (namespace, dimension sets, unit, value), aggregation and flush, the bounded
array for percentiles, the cardinality guard on label values, the verbatim pass-through
of the line by both log formatters, the settings wiring and the background flusher.
"""

from __future__ import annotations

import json
import logging
import threading
import time

import pytest
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from app.core.config import Settings
from app.core.logging import EMF_LOGGER_NAME, configure_logging
from app.core.metrics import (
    HTTP_REQUEST_DURATION_MS,
    HTTP_REQUESTS_TOTAL,
    JOBS_FAILED_TOTAL,
    JOBS_QUEUE_DEPTH,
    METRIC_NAMES,
    SCOUTS_BLOCKED,
    SCOUTS_BLOCKED_OLDEST_AGE_SECONDS,
    InMemoryMetrics,
    MetricError,
    NoOpMetrics,
    configure_metrics,
    configure_metrics_from_settings,
    exporter_status,
    get_metrics,
)
from app.core.metrics_emf import (
    MAX_VALUES_PER_DOCUMENT,
    EmfMetrics,
    metric_unit,
    namespace_problem,
)
from app.core.middleware import CorrelationMiddleware

NAMESPACE = "signalnest-staging/app"
T0 = 1_760_000_000.0


def _backend(lines: list[str], **kwargs) -> EmfMetrics:
    return EmfMetrics(
        namespace=NAMESPACE,
        service="signalnest-api",
        environment="staging",
        emit=lines.append,
        clock=lambda: T0,
        **kwargs,
    )


def _docs(lines: list[str]) -> list[dict]:
    for line in lines:
        assert "\n" not in line, "an EMF document must be exactly one log line"
    return [json.loads(line) for line in lines]


@pytest.fixture(autouse=True)
def _restore_backend_and_logging():
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    configure_metrics(None)
    root.handlers = handlers
    root.level = level


# --------------------------------------------------------------------------- #
# Document shape
# --------------------------------------------------------------------------- #
def test_counter_flushes_one_emf_document_with_rollup_and_breakdown_dimensions() -> None:
    lines: list[str] = []
    m = _backend(lines)
    for _ in range(3):
        m.increment(HTTP_REQUESTS_TOTAL, outcome="server_error", status_class="5xx")
    assert lines == [], "counters are aggregated until the flush, not written per call"

    m.flush()

    [doc] = _docs(lines)
    [directive] = doc["_aws"]["CloudWatchMetrics"]
    assert doc["_aws"]["Timestamp"] == int(T0 * 1000)
    assert directive["Namespace"] == NAMESPACE
    assert directive["Dimensions"] == [
        ["service", "environment"],
        ["service", "environment", "outcome", "status_class"],
    ]
    assert directive["Metrics"] == [{"Name": "http_requests_total", "Unit": "Count"}]
    assert doc["http_requests_total"] == 3
    assert (doc["service"], doc["environment"]) == ("signalnest-api", "staging")
    assert (doc["outcome"], doc["status_class"]) == ("server_error", "5xx")


def test_a_flush_with_nothing_recorded_writes_nothing_and_a_second_flush_is_empty() -> None:
    lines: list[str] = []
    m = _backend(lines)
    m.flush()
    m.increment(HTTP_REQUESTS_TOTAL, outcome="success", status_class="2xx")
    m.flush()
    m.flush()
    assert len(lines) == 1


def test_each_label_set_is_its_own_series() -> None:
    lines: list[str] = []
    m = _backend(lines)
    m.increment(HTTP_REQUESTS_TOTAL, outcome="success", status_class="2xx")
    m.increment(HTTP_REQUESTS_TOTAL, outcome="server_error", status_class="5xx")
    m.increment(HTTP_REQUESTS_TOTAL, outcome="server_error", status_class="5xx")
    m.flush()
    by_class = {d["status_class"]: d["http_requests_total"] for d in _docs(lines)}
    assert by_class == {"2xx": 1, "5xx": 2}


def test_histogram_is_written_as_a_value_array_in_milliseconds() -> None:
    lines: list[str] = []
    m = _backend(lines)
    for value in (12.5, 800.0, 1500.0):
        m.observe(HTTP_REQUEST_DURATION_MS, value, outcome="success")
    m.flush()
    [doc] = _docs(lines)
    assert doc["http_request_duration_ms"] == [12.5, 800.0, 1500.0]
    assert doc["_aws"]["CloudWatchMetrics"][0]["Metrics"] == [
        {"Name": "http_request_duration_ms", "Unit": "Milliseconds"}
    ]


def test_a_full_value_array_is_written_at_once_so_memory_stays_bounded() -> None:
    lines: list[str] = []
    m = _backend(lines)
    for i in range(MAX_VALUES_PER_DOCUMENT + 5):
        m.observe(HTTP_REQUEST_DURATION_MS, float(i), outcome="success")
    [early] = _docs(lines)
    assert len(early["http_request_duration_ms"]) == MAX_VALUES_PER_DOCUMENT
    m.flush()
    docs = _docs(lines)
    assert len(docs) == 2 and len(docs[1]["http_request_duration_ms"]) == 5
    every = docs[0]["http_request_duration_ms"] + docs[1]["http_request_duration_ms"]
    assert every == [float(i) for i in range(MAX_VALUES_PER_DOCUMENT + 5)], "no value is lost"


def test_gauge_keeps_the_last_value() -> None:
    lines: list[str] = []
    m = _backend(lines)
    m.set_gauge(JOBS_QUEUE_DEPTH, 5)
    m.set_gauge(JOBS_QUEUE_DEPTH, 3)
    m.flush()
    [doc] = _docs(lines)
    assert doc["jobs_queue_depth"] == 3
    assert doc["_aws"]["CloudWatchMetrics"][0]["Dimensions"] == [["service", "environment"]]


def test_error_class_and_job_status_are_properties_never_dimensions() -> None:
    lines: list[str] = []
    m = _backend(lines)
    m.increment(JOBS_FAILED_TOTAL, job_type="scout_request.execute", error_class="validation")
    m.flush()
    [doc] = _docs(lines)
    dims = doc["_aws"]["CloudWatchMetrics"][0]["Dimensions"]
    assert dims == [["service", "environment"], ["service", "environment", "job_type"]]
    assert doc["error_class"] == "validation", "kept as a searchable property"
    assert not any("error_class" in d for d in dims)


def test_every_catalogued_metric_has_a_unit() -> None:
    for name in METRIC_NAMES:
        assert metric_unit(name) in {"Count", "Milliseconds", "Seconds"}, name
    assert metric_unit(HTTP_REQUEST_DURATION_MS) == "Milliseconds"
    # FU-267-A1: the blocked-Scout age is in seconds; the count is a count.
    assert metric_unit(SCOUTS_BLOCKED_OLDEST_AGE_SECONDS) == "Seconds"
    assert metric_unit(SCOUTS_BLOCKED) == "Count"
    assert [n for n in METRIC_NAMES if metric_unit(n) == "Seconds"] == [
        SCOUTS_BLOCKED_OLDEST_AGE_SECONDS
    ]


def test_the_blocked_scout_gauges_are_one_rollup_series_each() -> None:
    """FU-267-A1: each gauge is written once per flush with its last value, under the
    service + environment rollup only (the dimension set the alarm reads), with its unit."""
    lines: list[str] = []
    m = _backend(lines)
    m.set_gauge(SCOUTS_BLOCKED, 1)
    m.set_gauge(SCOUTS_BLOCKED, 3)  # a later sweep: the last value wins
    m.set_gauge(SCOUTS_BLOCKED_OLDEST_AGE_SECONDS, 905.5)
    m.flush()
    docs = {
        doc["_aws"]["CloudWatchMetrics"][0]["Metrics"][0]["Name"]: doc for doc in _docs(lines)
    }
    assert set(docs) == {SCOUTS_BLOCKED, SCOUTS_BLOCKED_OLDEST_AGE_SECONDS}
    for name, value, unit in (
        (SCOUTS_BLOCKED, 3.0, "Count"),
        (SCOUTS_BLOCKED_OLDEST_AGE_SECONDS, 905.5, "Seconds"),
    ):
        doc = docs[name]
        directive = doc["_aws"]["CloudWatchMetrics"][0]
        assert directive["Namespace"] == NAMESPACE
        assert directive["Dimensions"] == [["service", "environment"]]
        assert directive["Metrics"] == [{"Name": name, "Unit": unit}]
        assert doc[name] == value
        assert set(doc) == {"_aws", "service", "environment", name}


# --------------------------------------------------------------------------- #
# Cardinality and failure isolation
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "value",
    ["a value with spaces", "/api/v1/opportunities/123", "x" * 65, "", "line\nbreak"],
)
def test_a_free_text_label_value_is_dropped_and_counted_not_written(value: str) -> None:
    lines: list[str] = []
    m = _backend(lines)
    m.increment(HTTP_REQUESTS_TOTAL, outcome=value, status_class="5xx")  # must not raise
    m.flush()
    assert lines == []
    assert m.telemetry_failures == 1


def test_a_non_finite_value_is_dropped_and_counted() -> None:
    lines: list[str] = []
    m = _backend(lines)
    m.observe(HTTP_REQUEST_DURATION_MS, float("nan"), outcome="success")
    m.flush()
    assert lines == [] and m.telemetry_failures == 1


def test_a_forbidden_label_key_still_fails_loudly() -> None:
    m = _backend([])
    with pytest.raises(MetricError):
        m.increment(HTTP_REQUESTS_TOTAL, organization_id="org-1")


def test_a_failing_writer_is_isolated_and_counted() -> None:
    def broken(_line: str) -> None:
        raise OSError("stdout closed")

    m = EmfMetrics(namespace=NAMESPACE, service="signalnest-api", environment="staging",
                   emit=broken)
    m.increment(HTTP_REQUESTS_TOTAL, outcome="success", status_class="2xx")
    m.flush()  # must not raise
    assert m.telemetry_failures == 1


@pytest.mark.parametrize("namespace", ["AWS/ApplicationELB", "", "has space", "x" * 256])
def test_an_invalid_namespace_is_refused(namespace: str) -> None:
    assert namespace_problem(namespace) is not None
    with pytest.raises(ValueError):
        EmfMetrics(namespace=namespace, service="signalnest-api", environment="staging")


# --------------------------------------------------------------------------- #
# The line reaches stdout verbatim through the single root handler
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("log_format", ["json", "console"])
def test_the_default_writer_emits_the_bare_emf_document_through_logging(
    log_format: str, capsys
) -> None:
    configure_logging("WARNING", log_format=log_format)  # root level must not hide metrics
    m = EmfMetrics(namespace=NAMESPACE, service="signalnest-api", environment="staging",
                   clock=lambda: T0)
    m.increment(HTTP_REQUESTS_TOTAL, outcome="success", status_class="2xx")
    m.flush()
    out = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert len(out) == 1
    doc = json.loads(out[0])
    assert set(doc) == {"_aws", "service", "environment", "outcome", "status_class",
                        "http_requests_total"}, "no log envelope may wrap the EMF document"


def test_an_ordinary_record_on_another_logger_cannot_inject_a_raw_line(capsys) -> None:
    configure_logging("INFO", log_format="json")
    logging.getLogger("signalnest.other").info("x", extra={"emf_line": '{"forged":1}'})
    [line] = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    assert json.loads(line).get("forged") is None, "only the EMF logger's records pass through"
    assert EMF_LOGGER_NAME != "signalnest.other"


# --------------------------------------------------------------------------- #
# Settings wiring and the background flusher
# --------------------------------------------------------------------------- #
def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


def test_settings_default_to_no_exporter() -> None:
    s = _settings()
    assert (s.metrics_enabled, s.metrics_exporter) == (False, "none")


@pytest.mark.parametrize(
    "overrides",
    [
        {"metrics_namespace": "AWS/ECS"},
        {"metrics_namespace": "bad namespace"},
        {"metrics_flush_interval_seconds": 0},
        {"metrics_flush_interval_seconds": 301},
    ],
)
def test_invalid_exporter_settings_are_refused_at_construction(overrides: dict) -> None:
    with pytest.raises(ValidationError):
        _settings(**overrides)


@pytest.mark.parametrize(
    "overrides",
    [{}, {"metrics_enabled": True}, {"metrics_exporter": "emf"}],
)
def test_without_an_enabled_exporter_the_installed_backend_is_kept(overrides: dict) -> None:
    installed = InMemoryMetrics()
    configure_metrics(installed)
    assert configure_metrics_from_settings(_settings(**overrides)) is installed
    assert get_metrics() is installed


def test_enabled_emf_exporter_is_installed_started_and_reported_healthy() -> None:
    s = _settings(metrics_enabled=True, metrics_exporter="emf",
                  metrics_namespace=NAMESPACE, metrics_flush_interval_seconds=30)
    backend = configure_metrics_from_settings(s)
    assert isinstance(backend, EmfMetrics) and get_metrics() is backend
    assert backend._namespace == NAMESPACE and backend._interval == 30
    assert backend._thread is not None and backend._thread.is_alive()
    assert exporter_status(metrics_enabled=True) == "healthy"

    configure_metrics(None)  # replacing it closes it: the flusher thread stops
    backend._thread.join(timeout=5)
    assert not backend._thread.is_alive()
    assert isinstance(get_metrics(), NoOpMetrics)


def test_an_exporter_that_cannot_be_built_falls_back_to_the_noop_backend(monkeypatch) -> None:
    import app.core.metrics_emf as emf

    def explode(**_kwargs):
        raise RuntimeError("cannot build")

    monkeypatch.setattr(emf, "EmfMetrics", explode)
    configure_metrics(InMemoryMetrics())
    s = _settings(metrics_enabled=True, metrics_exporter="emf")
    assert isinstance(configure_metrics_from_settings(s), NoOpMetrics)


def test_the_background_flusher_writes_without_any_further_call() -> None:
    lines: list[str] = []
    written = threading.Event()

    def emit(line: str) -> None:
        lines.append(line)
        written.set()

    m = EmfMetrics(namespace=NAMESPACE, service="signalnest-worker", environment="staging",
                   flush_interval_seconds=0.05, emit=emit)
    m.start()
    m.start()  # idempotent: still one thread
    m.increment(JOBS_FAILED_TOTAL, job_type="scout_request.execute", error_class="timeout")
    assert written.wait(timeout=5), "the flusher thread never wrote the series"
    m.close()
    assert not m._thread.is_alive()
    [doc] = _docs(lines)
    assert doc["jobs_failed_total"] == 1 and doc["service"] == "signalnest-worker"


def test_close_writes_what_is_left_and_is_idempotent() -> None:
    lines: list[str] = []
    m = _backend(lines, flush_interval_seconds=3600)
    m.start()
    m.increment(HTTP_REQUESTS_TOTAL, outcome="success", status_class="2xx")
    started = time.monotonic()
    m.close()
    m.close()
    assert time.monotonic() - started < 5, "close is bounded"
    assert len(lines) == 1


# --------------------------------------------------------------------------- #
# HTTP: an unhandled exception is counted as the 500 it becomes
# --------------------------------------------------------------------------- #
async def _boom(request):
    raise RuntimeError("handler bug")


def test_an_unhandled_exception_is_counted_as_a_5xx_request() -> None:
    metrics = InMemoryMetrics()
    configure_metrics(metrics)
    app = Starlette(routes=[Route("/boom", _boom)])
    app.add_middleware(CorrelationMiddleware)
    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.get("/boom").status_code == 500
    counted = metrics.counter_value(HTTP_REQUESTS_TOTAL, outcome="server_error", status_class="5xx")
    assert counted == 1
    assert len(metrics.observations(HTTP_REQUEST_DURATION_MS, outcome="server_error")) == 1


def test_the_unhandled_exception_still_propagates_to_the_server_error_handler() -> None:
    app = Starlette(routes=[Route("/boom", _boom)])
    app.add_middleware(CorrelationMiddleware)
    with TestClient(app) as client, pytest.raises(RuntimeError, match="handler bug"):
        client.get("/boom")
