"""Structured logging with request/job correlation and secret redaction.

One structured event per log record. In production a stable JSON object is emitted
to stdout; in development a human-readable line may be used instead. Both formats
draw the same standard fields and both run every ``extra_fields`` value through the
central redactor (:mod:`app.core.redaction`) so a careless field can never leak a
secret. Formatting is defensive: if building the structured record fails for any
reason, a minimal safe line is emitted rather than raising into the application.
"""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from app.core.redaction import REDACTION_ERROR, redact

# --- Correlation context ----------------------------------------------------
# Defined here (the lowest-level telemetry module) so the formatter can read them
# without importing higher-level code. ``app.core.log_context`` builds ergonomic
# helpers on top of these.
request_id_ctx: ContextVar[str | None] = ContextVar("request_id", default=None)
trace_id_ctx: ContextVar[str | None] = ContextVar("trace_id", default=None)
job_correlation_id_ctx: ContextVar[str | None] = ContextVar(
    "job_correlation_id", default=None
)
component_ctx: ContextVar[str | None] = ContextVar("component", default=None)
worker_type_ctx: ContextVar[str | None] = ContextVar("worker_type", default=None)
operation_ctx: ContextVar[str | None] = ContextVar("operation", default=None)

#: Reserved standard fields — an ``extra_fields`` key colliding with one of these
#: is namespaced (``fld_<key>``) so it can never overwrite a correlation field.
_RESERVED_FIELDS = frozenset(
    {
        "timestamp",
        "severity",
        "service",
        "environment",
        "logger",
        "message",
        "event",
        "request_id",
        "trace_id",
        "job_correlation_id",
        "component",
        "worker_type",
        "operation",
        "exc_info",
        "telemetry_error",
    }
)


def _iso_timestamp(created: float) -> str:
    return datetime.fromtimestamp(created, tz=UTC).isoformat()


def _base_payload(record: logging.LogRecord, *, service: str, environment: str) -> dict:
    payload: dict[str, Any] = {
        "timestamp": _iso_timestamp(record.created),
        "severity": record.levelname,
        "service": service,
        "environment": environment,
        "logger": record.name,
        "message": record.getMessage(),
        # The message doubles as the event name by convention; an explicit
        # ``event`` in extra_fields overrides it below.
        "event": record.getMessage(),
    }
    for key, ctx in (
        ("request_id", request_id_ctx),
        ("trace_id", trace_id_ctx),
        ("job_correlation_id", job_correlation_id_ctx),
        ("component", component_ctx),
        ("worker_type", worker_type_ctx),
        ("operation", operation_ctx),
    ):
        value = ctx.get()
        if value is not None:
            payload[key] = value
    return payload


def _merge_extra_fields(payload: dict, record: logging.LogRecord) -> None:
    raw = getattr(record, "extra_fields", None)
    if not raw:
        return
    redacted = redact(raw)
    if not isinstance(redacted, dict):  # pragma: no cover - redact preserves dicts
        payload["fields"] = redacted
        return
    for key, value in redacted.items():
        # ``event`` may intentionally override the message-derived event name.
        if key == "event":
            payload["event"] = value
        elif key in _RESERVED_FIELDS:
            payload[f"fld_{key}"] = value
        else:
            payload[key] = value


class JsonFormatter(logging.Formatter):
    """Emit one redacted JSON object per record; never raise on a bad record."""

    def __init__(self, *, service: str = "signalnest", environment: str = "development") -> None:
        super().__init__()
        self._service = service
        self._environment = environment

    def format(self, record: logging.LogRecord) -> str:
        try:
            payload = _base_payload(
                record, service=self._service, environment=self._environment
            )
            _merge_extra_fields(payload, record)
            if record.exc_info:
                payload["exc_info"] = self.formatException(record.exc_info)
            return json.dumps(payload, default=str)
        except Exception:
            return self._fallback(record)

    def _fallback(self, record: logging.LogRecord) -> str:
        # Minimal, guaranteed-safe line. Do not run user extras through here.
        try:
            safe = {
                "timestamp": _iso_timestamp(record.created),
                "severity": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
                "telemetry_error": "log_format_failed",
            }
            return json.dumps(safe, default=str)
        except Exception:  # pragma: no cover - last-resort
            return f'{{"telemetry_error":"{REDACTION_ERROR}"}}'


class ConsoleFormatter(logging.Formatter):
    """Human-readable single line for local development. Still redacts extras."""

    def __init__(self, *, service: str = "signalnest", environment: str = "development") -> None:
        super().__init__()
        self._service = service
        self._environment = environment

    def format(self, record: logging.LogRecord) -> str:
        try:
            payload = _base_payload(
                record, service=self._service, environment=self._environment
            )
            _merge_extra_fields(payload, record)
            head = f"{payload['timestamp']} {record.levelname:<8} {record.name} {payload['event']}"
            extras = {
                k: v
                for k, v in payload.items()
                if k not in {"timestamp", "severity", "logger", "message", "event",
                             "service", "environment"}
            }
            tail = " ".join(f"{k}={v}" for k, v in extras.items())
            line = f"{head} {tail}".rstrip()
            if record.exc_info:
                line = f"{line}\n{self.formatException(record.exc_info)}"
            return line
        except Exception:
            return f"{record.levelname} {record.name} {record.getMessage()} [telemetry_error]"


def _select_formatter(log_format: str, *, service: str, environment: str) -> logging.Formatter:
    if log_format == "console":
        return ConsoleFormatter(service=service, environment=environment)
    return JsonFormatter(service=service, environment=environment)


def configure_logging(
    level: str = "INFO",
    *,
    log_format: str | None = None,
    service: str | None = None,
    environment: str | None = None,
) -> None:
    """Install a single stdout handler using the configured formatter.

    ``log_format`` (``json`` | ``console``), ``service`` and ``environment`` default
    to the resolved application settings so callers can invoke this with just a
    level. JSON is the production default; ``console`` is a development convenience.
    """
    from app.core.config import get_settings

    settings = get_settings()
    fmt = log_format or settings.effective_log_format
    svc = service or settings.service_name
    env = environment or settings.environment

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_select_formatter(fmt, service=svc, environment=env))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def log_event(
    logger: logging.Logger,
    event: str,
    *,
    level: int = logging.INFO,
    outcome: str | None = None,
    duration_ms: float | None = None,
    **fields: Any,
) -> None:
    """Emit one structured event. ``fields`` are redacted by the formatter.

    Convenience wrapper so call sites pass flat keyword fields instead of building
    the ``extra={"extra_fields": {...}}`` envelope by hand.
    """
    extra_fields: dict[str, Any] = dict(fields)
    if outcome is not None:
        extra_fields["outcome"] = outcome
    if duration_ms is not None:
        extra_fields["duration_ms"] = duration_ms
    logger.log(level, event, extra={"extra_fields": extra_fields})


__all__ = [
    "JsonFormatter",
    "ConsoleFormatter",
    "configure_logging",
    "get_logger",
    "log_event",
    "request_id_ctx",
    "trace_id_ctx",
    "job_correlation_id_ctx",
    "component_ctx",
    "worker_type_ctx",
    "operation_ctx",
]
