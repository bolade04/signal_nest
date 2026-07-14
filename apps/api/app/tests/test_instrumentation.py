"""Lifecycle instrumentation tests (Phase 3A.4b Batch 2).

Assert that metrics are emitted at the *authoritative* points — after the
governing transaction commits — for the HTTP path and the durable-job lifecycle,
using the in-memory backend. A no-op backend (the default) must remain the state
after each test so instrumentation never leaks between tests.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from app.core.config import Settings
from app.core.metrics import (
    HTTP_REQUEST_DURATION_MS,
    HTTP_REQUESTS_TOTAL,
    JOB_EXECUTION_DURATION_MS,
    JOBS_CLAIMED_TOTAL,
    JOBS_COMPLETED_TOTAL,
    JOBS_ENQUEUED_TOTAL,
    JOBS_FAILED_TOTAL,
    JOBS_RETRIED_TOTAL,
    WORKER_POLL_TOTAL,
    InMemoryMetrics,
    NoOpMetrics,
    configure_metrics,
    get_metrics,
)
from app.core.middleware import CorrelationMiddleware
from app.db.base import Base
from app.jobs.context import ExecutionContext
from app.jobs.registry import HandlerContext, register_handler
from app.jobs.service import enqueue_job
from app.jobs.status import JobErrorCode, JobExecutionError
from app.jobs.worker import JobRunner
from app.main import app as _real_app  # noqa: F401 — registers ORM models

ORG = "org-metrics-0001"
WS = "ws-metrics-0001"
WORKER_TYPE = "durable-jobs"  # Settings default


@pytest.fixture()
def metrics() -> InMemoryMetrics:
    backend = InMemoryMetrics()
    configure_metrics(backend)
    try:
        yield backend
    finally:
        configure_metrics(None)  # restore the no-op default


# --------------------------------------------------------------------------- #
# HTTP path
# --------------------------------------------------------------------------- #
async def _ok(request):
    return JSONResponse({"ok": True})


def test_http_request_emits_bounded_metrics(metrics: InMemoryMetrics) -> None:
    app = Starlette(routes=[Route("/ok", _ok)])
    app.add_middleware(CorrelationMiddleware)
    with TestClient(app) as client:
        client.get("/ok")
    assert metrics.counter_value(HTTP_REQUESTS_TOTAL, outcome="success", status_class="2xx") == 1
    assert metrics.observations(HTTP_REQUEST_DURATION_MS, outcome="success")


# --------------------------------------------------------------------------- #
# Durable-job lifecycle
# --------------------------------------------------------------------------- #
@pytest.fixture()
def session_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path/'metrics.db'}",
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


@register_handler("test.metrics.ok")
def _ok_handler(ctx: HandlerContext) -> dict:
    return {"ok": True}


@register_handler("test.metrics.retry")
def _retry_handler(ctx: HandlerContext) -> dict:
    raise JobExecutionError(JobErrorCode.TRANSIENT, "temporary")


@register_handler("test.metrics.fail")
def _fail_handler(ctx: HandlerContext) -> dict:
    raise JobExecutionError(JobErrorCode.VALIDATION, "bad input")


def _runner(session_factory) -> JobRunner:
    return JobRunner(settings=Settings(_env_file=None), session_factory=session_factory)


def test_enqueue_emits_counter(metrics: InMemoryMetrics, db: Session) -> None:
    enqueue_job(db, job_type="test.metrics.ok", context=_context(), payload={"n": 1})
    db.commit()
    assert metrics.counter_value(
        JOBS_ENQUEUED_TOTAL, outcome="enqueued", job_type="test.metrics.ok", job_status="pending"
    ) == 1


def test_successful_job_emits_claim_and_completion(
    metrics: InMemoryMetrics, session_factory
) -> None:
    setup = session_factory()
    enqueue_job(setup, job_type="test.metrics.ok", context=_context(), payload={"n": 2})
    setup.commit()
    setup.close()

    assert _runner(session_factory).poll_once(worker_id="w-m") is True
    assert metrics.counter_value(
        JOBS_CLAIMED_TOTAL, worker_type=WORKER_TYPE, job_type="test.metrics.ok"
    ) == 1
    assert metrics.counter_value(
        JOBS_COMPLETED_TOTAL, outcome="success", job_type="test.metrics.ok"
    ) == 1
    assert metrics.observations(
        JOB_EXECUTION_DURATION_MS, outcome="success", job_type="test.metrics.ok"
    )
    assert metrics.counter_value(WORKER_POLL_TOTAL, worker_type=WORKER_TYPE, outcome="claimed") == 1


def test_retryable_failure_emits_retry_counter(
    metrics: InMemoryMetrics, session_factory
) -> None:
    setup = session_factory()
    enqueue_job(
        setup, job_type="test.metrics.retry", context=_context(),
        payload={"n": 3}, max_attempts=5,
    )
    setup.commit()
    setup.close()

    _runner(session_factory).poll_once(worker_id="w-m")
    assert metrics.counter_value(
        JOBS_RETRIED_TOTAL, job_type="test.metrics.retry", error_class="transient"
    ) == 1


def test_non_retryable_failure_emits_failed_counter(
    metrics: InMemoryMetrics, session_factory
) -> None:
    setup = session_factory()
    enqueue_job(
        setup, job_type="test.metrics.fail", context=_context(),
        payload={"n": 4}, max_attempts=5,
    )
    setup.commit()
    setup.close()

    _runner(session_factory).poll_once(worker_id="w-m")
    assert metrics.counter_value(
        JOBS_FAILED_TOTAL, job_type="test.metrics.fail", error_class="validation"
    ) == 1


def test_idle_poll_emits_idle_counter_only(
    metrics: InMemoryMetrics, session_factory
) -> None:
    # Nothing enqueued: the poll is idle and must not claim or complete anything.
    assert _runner(session_factory).poll_once(worker_id="w-m") is False
    assert metrics.counter_value(WORKER_POLL_TOTAL, worker_type=WORKER_TYPE, outcome="idle") == 1
    assert metrics.counter_value(WORKER_POLL_TOTAL, worker_type=WORKER_TYPE, outcome="claimed") == 0


def test_default_backend_restored_after_fixture() -> None:
    # Sanity: outside the metrics fixture the process default is a no-op again.
    assert isinstance(get_metrics(), NoOpMetrics)
