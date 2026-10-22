"""CloudWatch Embedded Metric Format exporter (P6-INF-5).

The process's metrics are written as CloudWatch Embedded Metric Format (EMF) documents —
one JSON object per line — to the stdout stream the structured logs already use. On ECS
the ``awslogs`` driver ships that stream to the workload's CloudWatch Logs group, and
CloudWatch Logs extracts the declared metrics from each EMF document as it ingests it.
The exporter therefore needs no agent, no endpoint, no credentials, no SDK and no new
infrastructure: nothing here imports a vendor library or opens a connection.

Aggregation keeps log volume and cost bounded. Counters are summed and gauges keep their
last value per series; both are written once per flush interval. Histogram observations
are written as value arrays of at most :data:`MAX_VALUES_PER_DOCUMENT` (the EMF limit),
so CloudWatch can still compute percentiles such as the p95 latency that
``docs/operations/alerts.md`` specifies. A series that fills an array is written at
once, so memory stays bounded however busy the process is.

Cardinality stays bounded twice over. :mod:`app.core.metrics` already rejects any label
key outside ``ALLOWED_LABELS`` (which is what keeps tenant, user, request and job ids out);
here every label VALUE must also be a short token (:data:`_LABEL_VALUE_RE`), so free text,
a path or URL, or an oversized value that reached a label by mistake is dropped (and
counted) instead of minting a new, separately billed CloudWatch metric. Each document
declares two dimension sets: ``service`` + ``environment`` (the rollup that ratio and
percentile alarms read) and that pair plus the series' breakdown labels. ``error_class``
and ``job_status`` are written as plain properties — searchable in Logs Insights, never
a metric dimension.
"""

from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
from collections import defaultdict
from collections.abc import Callable

from app.core.logging import EMF_LINE_ATTRIBUTE, EMF_LOGGER_NAME
from app.core.metrics import _label_key, _SafeBackend

#: The EMF specification's limit on values per metric in one document.
MAX_VALUES_PER_DOCUMENT = 100

#: Dimensions on every document: one per process, set once at construction.
BASE_DIMENSIONS: tuple[str, ...] = ("service", "environment")

#: Labels written as properties rather than dimensions (cost: each dimension value is a
#: separately billed CloudWatch metric, and these are for diagnosis, not alarming).
PROPERTY_LABELS: frozenset[str] = frozenset({"error_class", "job_status"})

#: A label value must look like an enumerable token — never free text or an identifier.
_LABEL_VALUE_RE = re.compile(r"[A-Za-z0-9_.:-]{1,64}")

#: CloudWatch namespace rules: ASCII letters, digits and ``. - _ / # :``, 1-255 chars,
#: and never the reserved ``AWS/`` prefix.
_NAMESPACE_RE = re.compile(r"[A-Za-z0-9._/#:-]{1,255}")


def namespace_problem(namespace: str) -> str | None:
    """Why ``namespace`` cannot be a custom CloudWatch namespace, or ``None``."""
    if not _NAMESPACE_RE.fullmatch(namespace):
        return "must be 1-255 characters of letters, digits and . - _ / # :"
    if namespace.startswith("AWS/"):
        return "must not start with the reserved AWS/ prefix"
    return None


def metric_unit(name: str) -> str:
    """The CloudWatch unit declared for a catalogued metric."""
    if name.endswith("_duration_ms"):
        return "Milliseconds"
    return "Count"


def _emit_through_logging(line: str) -> None:
    logging.getLogger(EMF_LOGGER_NAME).info("metrics.emf", extra={EMF_LINE_ATTRIBUTE: line})


_SeriesKey = tuple[str, tuple[tuple[str, str], ...]]


class EmfMetrics(_SafeBackend):
    """Aggregate in process and write CloudWatch EMF documents to the log stream."""

    def __init__(
        self,
        *,
        namespace: str,
        service: str,
        environment: str,
        flush_interval_seconds: float = 60.0,
        emit: Callable[[str], None] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        super().__init__()
        problem = namespace_problem(namespace)
        if problem is not None:
            raise ValueError(f"metrics namespace {problem}")
        if flush_interval_seconds <= 0:
            raise ValueError("flush_interval_seconds must be greater than 0")
        self._base = {"service": service, "environment": environment}
        for key, value in self._base.items():
            if not _LABEL_VALUE_RE.fullmatch(value):
                raise ValueError(f"{key} must be a short token to be a metric dimension")
        self._namespace = namespace
        self._interval = flush_interval_seconds
        self._emit = emit or _emit_through_logging
        self._clock = clock
        self._lock = threading.Lock()
        self._counters: dict[_SeriesKey, float] = defaultdict(float)
        self._histograms: dict[_SeriesKey, list[float]] = defaultdict(list)
        self._gauges: dict[_SeriesKey, float] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # Logging is the one writer on stdout; an explicit level keeps metric lines
        # independent of the root level an operator chooses for diagnostics.
        logging.getLogger(EMF_LOGGER_NAME).setLevel(logging.INFO)

    # -- recording (called through _SafeBackend: failures are counted, not raised) --
    def _record_increment(self, name: str, amount: float, labels: dict[str, str]) -> None:
        key = self._series(name, labels, amount)
        with self._lock:
            self._counters[key] += amount

    def _record_observe(self, name: str, value: float, labels: dict[str, str]) -> None:
        key = self._series(name, labels, value)
        full: list[float] | None = None
        with self._lock:
            values = self._histograms[key]
            values.append(float(value))
            if len(values) >= MAX_VALUES_PER_DOCUMENT:
                full = self._histograms.pop(key)
        if full is not None:
            self._emit(self._document(key, full, self._timestamp_ms()))

    def _record_set_gauge(self, name: str, value: float, labels: dict[str, str]) -> None:
        key = self._series(name, labels, value)
        with self._lock:
            self._gauges[key] = float(value)

    def _series(self, name: str, labels: dict[str, str], value: float) -> _SeriesKey:
        if not math.isfinite(value):
            raise ValueError(f"metric '{name}' value is not finite")
        for key, label_value in labels.items():
            if not _LABEL_VALUE_RE.fullmatch(str(label_value)):
                raise ValueError(f"metric '{name}' label '{key}' is not a short token")
        return (name, _label_key(labels))

    # -- output --------------------------------------------------------------------
    def _timestamp_ms(self) -> int:
        return int(self._clock() * 1000)

    def _document(self, key: _SeriesKey, value: float | list[float], timestamp_ms: int) -> str:
        name, label_items = key
        labels = dict(label_items)
        dimensions = {k: v for k, v in labels.items() if k not in PROPERTY_LABELS}
        properties = {k: v for k, v in labels.items() if k in PROPERTY_LABELS}
        dimension_values = {**self._base, **dimensions}
        breakdown = sorted(k for k in dimension_values if k not in BASE_DIMENSIONS)
        dimension_sets = [list(BASE_DIMENSIONS)]
        if breakdown:
            dimension_sets.append([*BASE_DIMENSIONS, *breakdown])
        document = {
            "_aws": {
                "Timestamp": timestamp_ms,
                "CloudWatchMetrics": [
                    {
                        "Namespace": self._namespace,
                        "Dimensions": dimension_sets,
                        "Metrics": [{"Name": name, "Unit": metric_unit(name)}],
                    }
                ],
            },
            **dimension_values,
            **properties,
            name: value,
        }
        return json.dumps(document, separators=(",", ":"), allow_nan=False)

    def flush(self) -> None:
        """Write every aggregated series now. Never raises; failures are counted."""
        with self._lock:
            counters, self._counters = self._counters, defaultdict(float)
            histograms, self._histograms = self._histograms, defaultdict(list)
            gauges, self._gauges = self._gauges, {}
        timestamp_ms = self._timestamp_ms()
        pending: list[tuple[_SeriesKey, float | list[float]]] = [
            *counters.items(),
            *gauges.items(),
            *((key, values) for key, values in histograms.items() if values),
        ]
        for key, value in pending:
            try:
                self._emit(self._document(key, value, timestamp_ms))
            except Exception:
                self._telemetry_failures += 1

    # -- background flusher -------------------------------------------------------
    def start(self) -> None:
        """Start the daemon thread that flushes every interval (idempotent)."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="metrics-emf-flush", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            self.flush()

    def close(self, timeout_seconds: float = 2.0) -> None:
        """Stop the flusher and write what is left. Bounded and idempotent; never raises."""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=timeout_seconds)
        self.flush()


__all__ = [
    "BASE_DIMENSIONS",
    "EmfMetrics",
    "MAX_VALUES_PER_DOCUMENT",
    "PROPERTY_LABELS",
    "metric_unit",
    "namespace_problem",
]
