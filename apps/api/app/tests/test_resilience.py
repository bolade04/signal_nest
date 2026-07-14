"""Resilience & failure-injection suite (Phase 3A.4b Batch 5B).

Deterministic, seam- and fake-based proof that the production data plane degrades
*safely* under failure. No real network, no brittle sleeps, no live infrastructure —
failures are injected through the existing seams (the enqueue notifier, the metrics
backend, an injected clock, a bounded pool).

This module deliberately does **not** re-test scenarios already covered elsewhere;
it references them so the coverage map is honest:

* Dependency-driver outage sanitization (Redis/S3 raising typed, secret-free errors)
  — ``test_production_adapters.py``.
* Startup schema gate refusing an uninitialized/behind schema (the migration-failure
  guard) and bounded/forced worker drain + telemetry-flush isolation on shutdown
  — ``test_deployment_lifecycle.py``.
* Invalid-configuration rejection at ``Settings`` construction
  — ``test_production_adapters.py``.
* Readiness probes degrading without a live dependency, and probe error-detail
  redaction — ``test_readiness_probes.py``.
* Expired-lease single-winner recovery and stale-worker fencing at the store layer
  — ``test_durable_jobs.py``.

What is genuinely new here is the *end-to-end* behavior through the worker poll loop
and the enqueue service under injected failure.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import TimeoutError as SATimeoutError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import QueuePool

from app.core.config import Settings
from app.core.errors import RedisNotifyFailedError
from app.core.metrics import (
    REDIS_NOTIFY_TOTAL,
    InMemoryMetrics,
    configure_metrics,
    telemetry_failure_count,
)
from app.db.base import Base
from app.jobs.context import ExecutionContext
from app.jobs.models import Job
from app.jobs.registry import HandlerContext, register_handler
from app.jobs.service import enqueue_job
from app.jobs.status import JobErrorCode, JobExecutionError, JobStatus, JobType
from app.jobs.store import DurableJobStore
from app.jobs.worker import JobRunner

# Importing the app registers every ORM model and every job handler.
from app.main import app  # noqa: F401

ORG = "org-resilience-01"
WS = "ws-resilience-01"


# --------------------------------------------------------------------------- #
# Fixtures & helpers (mirror the durable-job suite's throwaway-SQLite pattern)
# --------------------------------------------------------------------------- #
@pytest.fixture()
def session_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path/'resilience.db'}",
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


@pytest.fixture()
def store() -> DurableJobStore:
    return DurableJobStore()


def _hash(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _enqueue(store: DurableJobStore, db: Session, **overrides) -> Job:
    payload = overrides.pop("payload", {"scout_request_id": "sr-1"})
    kwargs = dict(
        organization_id=ORG,
        workspace_id=WS,
        job_type="test.resilience.ok",
        payload=payload,
        payload_hash=_hash(payload),
    )
    kwargs.update(overrides)
    job = store.enqueue(db, **kwargs)
    db.commit()
    return job


def _local_settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


def _fixed_clock(now: datetime) -> Callable[[], datetime]:
    return lambda: now


def _ctx() -> ExecutionContext:
    return ExecutionContext.for_scout_request(organization_id=ORG, workspace_id=WS)


@register_handler("test.resilience.ok")
def _ok_handler(ctx: HandlerContext) -> dict:
    return {"ran": True, "sr": ctx.payload.get("scout_request_id")}


@register_handler("test.resilience.poison")
def _poison_handler(ctx: HandlerContext) -> dict:
    # A job that fails on every attempt: it must be retried within policy and then
    # dead-lettered — never looped forever.
    raise JobExecutionError(JobErrorCode.TRANSIENT, "poison: always fails")


# --------------------------------------------------------------------------- #
# 1. Redis (advisory) outage never fails an enqueue
# --------------------------------------------------------------------------- #
def test_enqueue_survives_redis_notify_outage(db, monkeypatch) -> None:
    """The wake-up notifier is advisory: if it is down, enqueue still persists.

    Redis is not authoritative — a notify failure is a coordination degradation
    (counted), while the job is durably enqueued and later found by the worker's
    bounded database poll.
    """
    from app.jobs import service as service_mod

    class _BrokenNotifier:
        def notify_job_available(self) -> None:
            raise RedisNotifyFailedError("redis://secret-host:6379 refused")

        def wait_for_job(self, timeout: float) -> bool:
            return False

        def close(self) -> None:
            return None

    monkeypatch.setattr(service_mod, "_notifier", _BrokenNotifier())
    backend = InMemoryMetrics()
    configure_metrics(backend)
    try:
        job = enqueue_job(
            db,
            job_type=JobType.SCOUT_REQUEST_EXECUTE,
            context=_ctx(),
            payload={"scout_request_id": "sr-x"},
            scout_request_id="sr-x",
        )
        db.commit()
    finally:
        configure_metrics(None)

    # Durably enqueued despite the notify outage ...
    assert job.status == JobStatus.PENDING.value
    # ... and the coordination degradation was isolated and counted, not raised.
    assert backend.counter_value(
        REDIS_NOTIFY_TOTAL, dependency="redis", outcome="failure"
    ) == 1


# --------------------------------------------------------------------------- #
# 2. Worker termination mid-job — recovered and completed through the poll loop
# --------------------------------------------------------------------------- #
def test_abandoned_in_flight_job_is_recovered_and_completed(store, session_factory) -> None:
    """A worker that dies mid-execution loses nothing: the lease expires and a
    fresh worker's poll recovers, re-claims, and completes the job exactly once."""
    t0 = datetime(2026, 7, 12, tzinfo=UTC)

    # Worker A claims and starts, then "crashes" (never completes).
    setup = session_factory()
    job = _enqueue(store, setup, now=t0, max_attempts=5)
    job_id = job.id
    a = store.claim_one(setup, worker_id="A", lease_seconds=30, now=t0)
    store.mark_running(setup, a, worker_id=a.worker_id, lease_token=a.lease_token, now=t0)
    setup.commit()
    setup.close()

    # A fresh worker polls after A's lease has expired.
    later = t0 + timedelta(hours=1)
    runner = JobRunner(
        settings=_local_settings(), session_factory=session_factory, clock=_fixed_clock(later)
    )
    assert runner.poll_once(worker_id="B") is True

    check = session_factory()
    try:
        final = check.get(Job, job_id)
        assert final.status == JobStatus.SUCCEEDED.value
        assert final.attempt_count == 2  # A's start burned one; B ran the second
        assert final.worker_id is None and final.lease_token is None
        successes = [
            e for e in store.list_events(check, job_id=job_id) if e.event_type == "succeeded"
        ]
        assert len(successes) == 1  # exactly one terminal success across the recovery
    finally:
        check.close()


# --------------------------------------------------------------------------- #
# 3. Poison job — retried within policy then dead-lettered, never looped forever
# --------------------------------------------------------------------------- #
def test_poison_job_is_dead_lettered_within_budget(store, session_factory) -> None:
    max_attempts = 3
    now = datetime(2026, 7, 12, tzinfo=UTC)

    setup = session_factory()
    job = _enqueue(
        store, setup, job_type="test.resilience.poison", now=now, max_attempts=max_attempts
    )
    job_id = job.id
    setup.close()

    executed = 0
    status = None
    # A generous upper bound on poll cycles: the point is it terminates *well*
    # before this, proving no infinite loop.
    for _ in range(max_attempts + 3):
        runner = JobRunner(
            settings=_local_settings(),
            session_factory=session_factory,
            clock=_fixed_clock(now),
        )
        if runner.poll_once(worker_id="w"):
            executed += 1
        # Advance past the retry backoff (bounded well under an hour) so the
        # RETRY_WAIT job becomes due for the next attempt.
        now = now + timedelta(hours=1)
        check = session_factory()
        status = check.get(Job, job_id).status
        check.close()
        if status == JobStatus.DEAD_LETTERED.value:
            break

    assert status == JobStatus.DEAD_LETTERED.value
    assert executed == max_attempts  # exactly the budget, never an unbounded loop

    check = session_factory()
    try:
        final = check.get(Job, job_id)
        assert final.attempt_count == max_attempts
        dead = [
            e for e in store.list_events(check, job_id=job_id) if e.event_type == "dead_lettered"
        ]
        assert len(dead) == 1
    finally:
        check.close()


# --------------------------------------------------------------------------- #
# 4. Worker-fleet loss — the queue accumulates durably, nothing errors
# --------------------------------------------------------------------------- #
def test_queue_accumulates_durably_with_no_workers(store, session_factory) -> None:
    """With no worker ever running, enqueued work sits safely PENDING. API liveness
    and readiness never depend on the worker fleet (``require_worker_fleet`` is
    False by default), so fleet loss degrades throughput, not availability."""
    assert _local_settings().require_worker_fleet is False

    setup = session_factory()
    job = _enqueue(store, setup)
    setup.commit()
    setup.close()

    check = session_factory()
    try:
        persisted = check.get(Job, job.id)
        assert persisted.status == JobStatus.PENDING.value
        assert persisted.worker_id is None
    finally:
        check.close()


# --------------------------------------------------------------------------- #
# 5. Connection-pool pressure — bounded timeout, never an unbounded hang
# --------------------------------------------------------------------------- #
def test_production_engine_pool_timeout_is_bounded() -> None:
    """The PostgreSQL engine is built with a bounded checkout timeout, so pool
    exhaustion surfaces as a timeout rather than an infinite wait."""
    from app.db.session import build_engine

    s = _local_settings(database_url="postgresql+psycopg://u:p@h:5432/db")
    engine = build_engine(s)
    try:
        assert engine.pool._timeout == s.db_pool_timeout_seconds
        assert engine.pool._timeout > 0
    finally:
        engine.dispose()


def test_exhausted_connection_pool_raises_bounded_timeout(tmp_path) -> None:
    """A saturated pool raises a bounded ``TimeoutError`` promptly instead of
    hanging: one slot, no overflow, a short timeout."""
    engine = create_engine(
        f"sqlite:///{tmp_path/'pool.db'}",
        connect_args={"check_same_thread": False},
        poolclass=QueuePool,
        pool_size=1,
        max_overflow=0,
        pool_timeout=0.5,
        future=True,
    )
    try:
        held = engine.connect()  # exhaust the single slot and keep it checked out
        try:
            started = time.monotonic()
            with pytest.raises(SATimeoutError):
                engine.connect()  # bounded wait, then raise — never an unbounded hang
            assert time.monotonic() - started < 5.0
        finally:
            held.close()
    finally:
        engine.dispose()


# --------------------------------------------------------------------------- #
# 6. Telemetry failure — job execution completes, the failure is isolated
# --------------------------------------------------------------------------- #
class _FailingMetrics(InMemoryMetrics):
    """A metrics backend whose recording always raises (an exporter outage)."""

    def _record_increment(self, name, amount, labels):  # noqa: ANN001
        raise RuntimeError("metrics exporter down")

    def _record_observe(self, name, value, labels):  # noqa: ANN001
        raise RuntimeError("metrics exporter down")


def test_job_execution_completes_despite_metrics_backend_failure(store, session_factory) -> None:
    setup = session_factory()
    job = _enqueue(store, setup)
    job_id = job.id
    setup.close()

    configure_metrics(_FailingMetrics())
    try:
        JobRunner(settings=_local_settings(), session_factory=session_factory).poll_once(
            worker_id="w"
        )
        failures = telemetry_failure_count()
    finally:
        configure_metrics(None)

    check = session_factory()
    try:
        final = check.get(Job, job_id)
        # Telemetry failing closed to no-op never breaks execution ...
        assert final.status == JobStatus.SUCCEEDED.value
        # ... and every swallowed recording failure was isolated and counted.
        assert failures > 0
    finally:
        check.close()
