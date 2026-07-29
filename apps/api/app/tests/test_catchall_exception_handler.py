"""FastAPI catch-all exception-handler containment tests (Phase 4 Gate 4F).

The catch-all must emit a fixed, low-cardinality event carrying only the
exception class — never the raw message (unredacted in the formatter's
message/event fields), never a traceback (``exc_info`` bypasses redaction
entirely) — while the HTTP envelope and request correlation are preserved and
domain errors keep routing to their own safe handler.
"""

from __future__ import annotations

import json
import logging
import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.errors import NotFoundError, register_exception_handlers
from app.core.logging import configure_logging
from app.core.middleware import CorrelationMiddleware

_SENTINEL_HOST = "db-sentinel-host.internal"
_SENTINEL_PASS = "sn-sentinel-p4ssw0rd"


@pytest.fixture(autouse=True)
def _restore_root_logging():
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers = handlers
    root.level = level


@pytest.fixture()
def client():
    get_settings.cache_clear()
    app = FastAPI()
    app.add_middleware(CorrelationMiddleware)
    register_exception_handlers(app)

    @app.get("/boom")
    def _boom():
        raise sqlite3.OperationalError(
            f"connection to server at {_SENTINEL_HOST}, port 5432 failed "
            f"(postgresql://svc:{_SENTINEL_PASS}@{_SENTINEL_HOST}:5432/appdb)"
        )

    @app.get("/domain")
    def _domain():
        raise NotFoundError("Opportunity not found")

    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    get_settings.cache_clear()


def _json_events(out: str) -> list[dict]:
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def test_unhandled_error_emits_fixed_event_only(client, capsys) -> None:
    configure_logging("INFO", log_format="json")

    resp = client.get("/boom")

    cap = capsys.readouterr()  # single capture; both streams asserted below
    # Sanitized envelope. The catch-all runs in Starlette's OUTERMOST error
    # middleware — above CorrelationMiddleware, whose context resets during
    # unwind — so request correlation was never available at this depth
    # (pre-existing residual, pinned here; domain errors below do carry it).
    assert resp.status_code == 500
    body = resp.json()["error"]
    assert body["code"] == "internal_error"
    assert body["message"] == "An unexpected error occurred"
    assert "request_id" in body

    # Positive: the fixed event actually emitted, class name only.
    events = _json_events(cap.out)
    unhandled = [e for e in events if e.get("event") == "unhandled_error"]
    assert len(unhandled) == 1
    assert unhandled[0]["error_class"] == "OperationalError"
    assert unhandled[0]["outcome"] == "failure"

    # Negative: no raw message, no traceback, no DSN fragment, either stream.
    blob = cap.out + cap.err
    assert _SENTINEL_HOST not in blob
    assert _SENTINEL_PASS not in blob
    assert "Traceback" not in blob
    assert "port 5432 failed" not in blob  # the driver prose never surfaces


def test_domain_errors_keep_their_safe_handler(client, capsys) -> None:
    configure_logging("INFO", log_format="json")

    resp = client.get("/domain")

    cap = capsys.readouterr()  # single capture
    assert resp.status_code == 404
    body = resp.json()["error"]
    assert body["code"] == "not_found"
    # Domain errors route through ExceptionMiddleware INSIDE the correlation
    # scope, so their envelope does carry the request id.
    assert body["request_id"]
    # The catch-all must NOT fire for domain errors.
    events = _json_events(cap.out)
    assert not any(e.get("event") == "unhandled_error" for e in events)
