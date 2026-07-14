"""Request + durable-job correlation tests (Phase 3A.4b Batch 2).

Two layers, neither touching a real external service:

* **HTTP correlation** — the middleware accepts only a strict inbound request id,
  generates a fresh one otherwise, echoes it in the response header, exposes it to
  the handler via request-local context, and leaves **no** context behind after the
  request (so ids can never bleed between requests).
* **Job correlation** — enqueue mints an opaque ``correlation_id`` (distinct from the
  row id / lease token / worker id), persists it, and the worker restores it into the
  logging context for the duration of execution and clears it afterwards.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from app.core.config import Settings
from app.core.log_context import (
    bound_context,
    clear_context,
    current_context,
    new_correlation_id,
    normalize_request_id,
)
from app.core.middleware import CorrelationMiddleware
from app.db.base import Base
from app.jobs.context import ExecutionContext
from app.jobs.models import Job
from app.jobs.registry import HandlerContext, register_handler
from app.jobs.service import enqueue_job
from app.jobs.worker import JobRunner

# Importing the app registers every ORM model on the shared Base.
from app.main import app as _real_app  # noqa: F401

ORG = "org-corr-0001"
WS = "ws-corr-0001"

_REQUEST_ID_LEN = 32  # uuid4().hex


# --------------------------------------------------------------------------- #
# HTTP correlation middleware
# --------------------------------------------------------------------------- #
async def _echo(request):  # returns the request id the handler observed
    ctx = current_context()
    return JSONResponse({"request_id": ctx.get("request_id"), "trace_id": ctx.get("trace_id")})


async def _boom(request):
    # A handled error status: flows back through the middleware as a real response
    # (unlike an uncaught crash, which Starlette's outermost error middleware owns).
    raise HTTPException(status_code=503, detail="unavailable")


@pytest.fixture()
def client() -> TestClient:
    routes = [Route("/echo", _echo), Route("/boom", _boom)]
    app = Starlette(routes=routes)
    app.add_middleware(CorrelationMiddleware)
    # raise_server_exceptions=False so a 500 flows through as a real response.
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def test_missing_request_id_is_generated_and_returned(client) -> None:
    resp = client.get("/echo")
    rid = resp.headers.get("x-request-id")
    assert rid is not None
    assert normalize_request_id(rid) == rid  # the generated id is itself valid
    assert resp.json()["request_id"] == rid  # handler saw the same id


def test_valid_inbound_request_id_is_preserved(client) -> None:
    incoming = "abcdef01-2345-6789-abcd-ef0123456789"
    resp = client.get("/echo", headers={"x-request-id": incoming})
    assert resp.headers["x-request-id"] == incoming
    assert resp.json()["request_id"] == incoming


def test_invalid_characters_are_rejected_and_replaced(client) -> None:
    resp = client.get("/echo", headers={"x-request-id": "bad id\nwith spaces"})
    rid = resp.headers["x-request-id"]
    assert rid != "bad id\nwith spaces"
    assert normalize_request_id(rid) == rid
    assert "\n" not in rid and " " not in rid


def test_oversized_request_id_is_rejected_and_replaced(client) -> None:
    oversized = "a" * 200
    resp = client.get("/echo", headers={"x-request-id": oversized})
    rid = resp.headers["x-request-id"]
    assert rid != oversized
    assert len(rid) <= 64


def test_trace_id_defaults_to_request_id_when_absent(client) -> None:
    resp = client.get("/echo")
    body = resp.json()
    assert body["trace_id"] == body["request_id"]


def test_error_response_still_carries_request_id(client) -> None:
    resp = client.get("/boom")
    assert resp.status_code == 503
    assert normalize_request_id(resp.headers.get("x-request-id")) is not None


def test_sequential_requests_get_distinct_ids_no_contamination(client) -> None:
    first = client.get("/echo").headers["x-request-id"]
    second = client.get("/echo").headers["x-request-id"]
    assert first != second


def test_handler_sees_only_its_own_inbound_id(client) -> None:
    a = client.get("/echo", headers={"x-request-id": "11111111-1111-1111-1111-111111111111"})
    b = client.get("/echo", headers={"x-request-id": "22222222-2222-2222-2222-222222222222"})
    assert a.json()["request_id"] == "11111111-1111-1111-1111-111111111111"
    assert b.json()["request_id"] == "22222222-2222-2222-2222-222222222222"


# --------------------------------------------------------------------------- #
# Context helpers
# --------------------------------------------------------------------------- #
def test_bound_context_restores_prior_values() -> None:
    clear_context()
    with bound_context(request_id="outer"):
        assert current_context()["request_id"] == "outer"
        with bound_context(request_id="inner"):
            assert current_context()["request_id"] == "inner"
        assert current_context()["request_id"] == "outer"
    assert "request_id" not in current_context()


def test_bound_context_restores_even_on_exception() -> None:
    clear_context()
    with pytest.raises(ValueError):
        with bound_context(job_correlation_id="job-x"):
            raise ValueError("boom")
    assert "job_correlation_id" not in current_context()


def test_bound_context_ignores_unknown_and_none_fields() -> None:
    clear_context()
    with bound_context(request_id="rid", not_a_field="x", trace_id=None):
        ctx = current_context()
        assert ctx["request_id"] == "rid"
        assert "not_a_field" not in ctx
        assert "trace_id" not in ctx


def test_normalize_request_id_accepts_and_rejects() -> None:
    assert normalize_request_id("deadbeef") == "deadbeef"
    assert normalize_request_id("  deadbeef  ") == "deadbeef"  # trimmed
    assert normalize_request_id(None) is None
    assert normalize_request_id("") is None
    assert normalize_request_id("short") is None  # < 8 chars
    assert normalize_request_id("a" * 65) is None  # > 64 chars
    assert normalize_request_id("has spaces here") is None
    assert normalize_request_id("semi;colon;inject") is None


# --------------------------------------------------------------------------- #
# Durable-job correlation
# --------------------------------------------------------------------------- #
@pytest.fixture()
def session_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path/'corr.db'}",
        connect_args={"check_same_thread": False},
        future=True,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)
    yield factory
    engine.dispose()


@pytest.fixture()
def db(session_factory) -> Session:
    s = session_factory()
    try:
        yield s
    finally:
        s.close()


def _context() -> ExecutionContext:
    return ExecutionContext.for_scout_request(organization_id=ORG, workspace_id=WS)


_seen_correlation: dict[str, str | None] = {}


@register_handler("test.corr.capture")
def _capture_handler(ctx: HandlerContext) -> dict:
    # Record what the worker restored into the logging context while running.
    _seen_correlation["value"] = current_context().get("job_correlation_id")
    return {"ok": True}


def test_enqueue_mints_persisted_correlation_id(db) -> None:
    job = enqueue_job(
        db, job_type="test.corr.capture", context=_context(), payload={"n": 1}
    )
    db.commit()
    assert job.correlation_id is not None
    assert len(job.correlation_id) == _REQUEST_ID_LEN
    # Distinct from the row id and (unclaimed) lease/worker fields.
    assert job.correlation_id != job.id
    assert job.correlation_id != job.lease_token
    assert job.correlation_id != job.worker_id


def test_correlation_id_survives_reload(session_factory) -> None:
    setup = session_factory()
    job = enqueue_job(
        setup, job_type="test.corr.capture", context=_context(), payload={"n": 2}
    )
    setup.commit()
    job_id, corr = job.id, job.correlation_id
    setup.close()

    check = session_factory()
    try:
        reloaded = check.get(Job, job_id)
        assert reloaded.correlation_id == corr
    finally:
        check.close()


def test_worker_restores_correlation_into_logging_context(session_factory) -> None:
    _seen_correlation.clear()
    setup = session_factory()
    job = enqueue_job(
        setup, job_type="test.corr.capture", context=_context(), payload={"n": 3}
    )
    setup.commit()
    corr = job.correlation_id
    setup.close()

    runner = JobRunner(settings=Settings(_env_file=None), session_factory=session_factory)
    ran = runner.poll_once(worker_id="w-corr")
    assert ran is True
    # The handler observed the job's correlation id while executing.
    assert _seen_correlation["value"] == corr


def test_worker_clears_correlation_after_job(session_factory) -> None:
    clear_context()
    setup = session_factory()
    enqueue_job(setup, job_type="test.corr.capture", context=_context(), payload={"n": 4})
    setup.commit()
    setup.close()

    runner = JobRunner(settings=Settings(_env_file=None), session_factory=session_factory)
    runner.poll_once(worker_id="w-corr")
    # No job correlation leaks into the polling thread's context after the job.
    assert "job_correlation_id" not in current_context()


def test_two_jobs_do_not_share_correlation(session_factory) -> None:
    setup = session_factory()
    a = enqueue_job(setup, job_type="test.corr.capture", context=_context(), payload={"n": 5})
    b = enqueue_job(setup, job_type="test.corr.capture", context=_context(), payload={"n": 6})
    setup.commit()
    setup.close()
    assert a.correlation_id != b.correlation_id


def test_new_correlation_id_is_opaque_and_unique() -> None:
    ids = {new_correlation_id() for _ in range(100)}
    assert len(ids) == 100
    assert all(len(i) == _REQUEST_ID_LEN for i in ids)
