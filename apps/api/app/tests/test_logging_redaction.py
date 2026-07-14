"""Structured-logging and secret-redaction tests (Phase 3A.4b Batch 2).

Proves that representative secrets never survive serialization, that ordinary
non-secret fields pass through intact, that the JSON formatter emits the standard
field set, and that formatting/redaction failures degrade safely instead of
raising into the application.
"""

from __future__ import annotations

import json
import logging

import pytest

from app.core.logging import ConsoleFormatter, JsonFormatter, log_event
from app.core.redaction import (
    REDACTED,
    redact,
    redact_text,
    sanitize_exception,
)


def _record(msg: str = "event.test", **extra_fields) -> logging.LogRecord:
    record = logging.LogRecord(
        name="signalnest.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=(),
        exc_info=None,
    )
    if extra_fields:
        record.extra_fields = extra_fields
    return record


# --- Redaction: secrets never survive --------------------------------------
@pytest.mark.parametrize(
    "key",
    [
        "password",
        "api_key",
        "apiKey",
        "authorization",
        "Authorization",
        "access_token",
        "refresh_token",
        "client_secret",
        "lease_token",
        "generation_token",
        "database_url",
        "redis_url",
        "cookie",
        "private_key",
        "jwt",
    ],
)
def test_sensitive_keys_are_redacted(key: str) -> None:
    out = redact({key: "super-secret-value"})
    assert out[key] == REDACTED
    assert "super-secret-value" not in json.dumps(out)


def test_nested_secrets_are_redacted() -> None:
    payload = {
        "outer": {"inner": {"api_key": "sk-live-123"}},
        "list": [{"password": "hunter2"}, {"ok": "keep"}],
    }
    serialized = json.dumps(redact(payload))
    assert "sk-live-123" not in serialized
    assert "hunter2" not in serialized
    assert "keep" in serialized


def test_url_credentials_are_stripped() -> None:
    out = redact_text("postgresql://user:s3cret@db.internal:5432/app")
    assert "s3cret" not in out
    assert "user" not in out
    assert REDACTED in out
    assert "db.internal" in out


def test_sensitive_query_parameters_are_stripped() -> None:
    url = "https://bucket.s3.amazonaws.com/o?X-Amz-Signature=abcdef&X-Amz-Credential=AKIA/x"
    out = redact_text(url)
    assert "abcdef" not in out
    assert "AKIA" not in out
    assert "bucket.s3.amazonaws.com" in out


def test_bytes_are_never_emitted_raw() -> None:
    out = redact({"blob": b"\x00\x01secret"})
    assert out["blob"].startswith("[bytes:")
    assert "secret" not in json.dumps(out)


def test_cycles_do_not_recurse_forever() -> None:
    d: dict = {"self": None}
    d["self"] = d
    out = redact(d)  # must return, not raise/hang
    assert isinstance(out, dict)


def test_oversized_string_is_truncated() -> None:
    out = redact_text("x" * 10_000)
    assert out.endswith("...[truncated]")
    assert len(out) < 10_000


def test_redaction_never_raises_on_pathological_object() -> None:
    class Boom:
        def __str__(self) -> str:
            raise RuntimeError("nope")

    # Must not raise.
    out = redact({"weird": Boom()})
    assert isinstance(out, dict)


# --- Non-secret fields remain intact ---------------------------------------
def test_ordinary_fields_pass_through() -> None:
    payload = {"job_type": "scout", "outcome": "succeeded", "duration_ms": 12.5, "count": 3}
    out = redact(payload)
    assert out == payload


def test_sanitize_exception_keeps_class_redacts_message() -> None:
    exc = ValueError("connect failed for redis://user:pw@host:6379/0")
    info = sanitize_exception(exc)
    assert info["error_class"] == "ValueError"
    assert "pw" not in info["error_message"]


# --- JSON formatter: standard fields + redaction ---------------------------
def test_json_formatter_emits_standard_fields() -> None:
    fmt = JsonFormatter(service="signalnest-api", environment="production")
    payload = json.loads(fmt.format(_record("job.enqueued", outcome="ok", job_type="scout")))
    assert payload["service"] == "signalnest-api"
    assert payload["environment"] == "production"
    assert payload["severity"] == "INFO"
    assert payload["event"] == "job.enqueued"
    assert payload["outcome"] == "ok"
    assert payload["job_type"] == "scout"
    assert "timestamp" in payload


def test_json_formatter_redacts_extra_fields() -> None:
    fmt = JsonFormatter()
    line = fmt.format(_record("dep.connect", redis_url="redis://u:pw@host:6379/0"))
    assert "pw" not in line
    payload = json.loads(line)
    assert REDACTED in json.dumps(payload["redis_url"])


def test_json_formatter_does_not_leak_lease_or_generation_token() -> None:
    fmt = JsonFormatter()
    line = fmt.format(
        _record("job.claimed", lease_token="lt-abc123", generation_token="gt-xyz789")
    )
    assert "lt-abc123" not in line
    assert "gt-xyz789" not in line


def test_json_formatter_survives_unserializable_extra() -> None:
    class Weird:
        def __repr__(self) -> str:
            raise RuntimeError("boom")

    fmt = JsonFormatter()
    # Must produce a line, never raise.
    line = fmt.format(_record("weird.event", thing=Weird()))
    assert isinstance(line, str) and line


def test_console_formatter_is_human_readable_and_redacts() -> None:
    fmt = ConsoleFormatter()
    line = fmt.format(_record("worker.start", secret_key="dev-insecure", worker_type="durable"))
    assert "worker.start" in line
    assert "worker_type=durable" in line
    assert "dev-insecure" not in line


def test_log_event_helper_redacts(caplog) -> None:
    logger = logging.getLogger("signalnest.test.helper")
    # Attach the JSON formatter to a capture handler.
    with caplog.at_level(logging.INFO, logger="signalnest.test.helper"):
        log_event(logger, "dep.degraded", outcome="degraded", api_key="sk-secret")
    record = caplog.records[-1]
    fmt = JsonFormatter()
    assert "sk-secret" not in fmt.format(record)
