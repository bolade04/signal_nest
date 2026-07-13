"""Durable-job foundation tests (Phase 3A.3).

Three layers, none of which touch a real external service:

* **Pure contracts** — backoff determinism/bounds, lifecycle transition legality,
  and the error taxonomy's retry classification (no DB, no FastAPI).
* **Store** — enqueue/idempotency, atomic claiming (incl. a concurrent race that
  must yield a single winner), the running→success/retry/dead-letter/cancel
  lifecycle, and expired-lease recovery, all driven by an injected clock against
  a throwaway SQLite database.
* **Worker runner** — a claimed job executed end-to-end through a registered
  handler into every terminal outcome (success, retry, fail-fast, dead-letter,
  unknown-type, cooperative cancel).
"""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.base import Base
from app.jobs.backoff import compute_backoff_seconds
from app.jobs.context import ExecutionContext
from app.jobs.models import Job
from app.jobs.registry import HandlerContext, register_handler
from app.jobs.service import enqueue_job
from app.jobs.status import (
    ENQUEUED_STATUSES,
    TERMINAL_STATUSES,
    InvalidJobTransition,
    JobError,
    JobErrorCode,
    JobExecutionError,
    JobStatus,
    JobType,
    can_transition,
    ensure_transition,
    is_terminal,
)
from app.jobs.store import DurableJobStore, IdempotencyConflict
from app.jobs.worker import JobRunner

# Importing the app registers every ORM model on the shared Base so the throwaway
# schema below can be created in full.
from app.main import app  # noqa: F401

ORG = "org-test-0001"
WS = "ws-test-0001"


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture()
def session_factory(tmp_path):
    """A throwaway file-backed SQLite DB + thread-safe session factory.

    A file (not ``:memory:``) is used so the multiple connections opened by the
    concurrency test all see the same database.
    """
    engine = create_engine(
        f"sqlite:///{tmp_path/'jobs.db'}",
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
        job_type="test.ok",
        payload=payload,
        payload_hash=_hash(payload),
    )
    kwargs.update(overrides)
    job = store.enqueue(db, **kwargs)
    db.commit()
    return job


def _local_settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


# --------------------------------------------------------------------------- #
# Pure contracts
# --------------------------------------------------------------------------- #
def test_backoff_is_deterministic_and_bounded() -> None:
    a = compute_backoff_seconds(3, base_seconds=2.0, max_seconds=300.0, jitter_seed="job-x")
    b = compute_backoff_seconds(3, base_seconds=2.0, max_seconds=300.0, jitter_seed="job-x")
    assert a == b  # same seed + attempt -> identical delay
    # attempt 3 raw = 2 * 2**2 = 8; jitter only ever *reduces* within the bound.
    assert 0.0 <= a <= 8.0


def test_backoff_respects_cap_even_for_large_attempts() -> None:
    delay = compute_backoff_seconds(
        99, base_seconds=2.0, max_seconds=30.0, jitter_seed=None
    )
    assert delay == 30.0  # capped, no overflow, no jitter when seed is None


def test_terminal_states_have_no_outgoing_transitions() -> None:
    for term in TERMINAL_STATUSES:
        assert is_terminal(term)
        for target in JobStatus:
            assert not can_transition(term, target)


def test_illegal_transition_is_rejected() -> None:
    assert not can_transition(JobStatus.PENDING, JobStatus.SUCCEEDED)
    with pytest.raises(InvalidJobTransition):
        ensure_transition(JobStatus.PENDING, JobStatus.SUCCEEDED)
    # A legal one does not raise.
    ensure_transition(JobStatus.RUNNING, JobStatus.SUCCEEDED)


def test_error_retry_classification() -> None:
    assert JobError(JobErrorCode.TRANSIENT).retryable
    assert JobError(JobErrorCode.TIMEOUT).retryable
    assert not JobError(JobErrorCode.VALIDATION).retryable
    assert not JobError(JobErrorCode.UNSUPPORTED_TYPE).retryable


def test_error_summary_is_bounded_and_normalized() -> None:
    err = JobError(JobErrorCode.TRANSIENT, "  lots   of\n\nwhitespace  " + "x" * 1000)
    summary = err.safe_summary()
    assert len(summary) <= 500
    assert "\n" not in summary and "  " not in summary


def test_job_execution_error_carries_code_only() -> None:
    exc = JobExecutionError(JobErrorCode.VALIDATION, "bad input")
    assert exc.error.code is JobErrorCode.VALIDATION
    assert not exc.error.retryable


# --------------------------------------------------------------------------- #
# Store lifecycle
# --------------------------------------------------------------------------- #
def test_enqueue_creates_pending_job_with_audit_event(store, db) -> None:
    job = _enqueue(store, db)
    assert job.status == JobStatus.PENDING.value
    assert job.attempt_count == 0
    events = store.list_events(db, job_id=job.id)
    assert [e.event_type for e in events] == ["enqueued"]


def test_future_scheduled_job_is_scheduled_not_pending(store, db) -> None:
    now = datetime(2026, 7, 12, tzinfo=UTC)
    job = _enqueue(store, db, scheduled_for=now + timedelta(hours=1), now=now)
    assert job.status == JobStatus.SCHEDULED.value
    assert JobStatus.SCHEDULED in ENQUEUED_STATUSES


def test_idempotent_repeat_returns_same_job(store, db) -> None:
    first = _enqueue(store, db, idempotency_key="k-1")
    second = _enqueue(store, db, idempotency_key="k-1")
    assert first.id == second.id


def test_idempotency_conflict_on_different_payload(store, db) -> None:
    _enqueue(store, db, idempotency_key="k-2", payload={"scout_request_id": "a"})
    with pytest.raises(IdempotencyConflict):
        _enqueue(store, db, idempotency_key="k-2", payload={"scout_request_id": "b"})


def test_claim_marks_job_and_audits(store, db) -> None:
    job = _enqueue(store, db)
    claimed = store.claim_one(db, worker_id="w1", lease_seconds=30)
    assert claimed is not None and claimed.id == job.id
    # Read DB truth on a fresh instance (a real worker claims on its own session).
    db.expire_all()
    persisted = store.get_job(db, workspace_id=WS, job_id=job.id)
    assert persisted.status == JobStatus.CLAIMED.value
    assert persisted.worker_id == "w1" and persisted.lease_expires_at is not None
    types = [e.event_type for e in store.list_events(db, job_id=job.id)]
    assert types == ["enqueued", "claimed"]


def test_claim_prefers_higher_priority(store, db) -> None:
    _enqueue(store, db, priority=0)
    high = _enqueue(store, db, priority=10)
    claimed = store.claim_one(db, worker_id="w1", lease_seconds=30)
    assert claimed.id == high.id


def test_complete_records_success_and_result(store, db) -> None:
    job = _enqueue(store, db)
    store.claim_one(db, worker_id="w1", lease_seconds=30)
    store.mark_running(db, job)
    assert job.attempt_count == 1
    store.complete(db, job, result_summary={"opportunities": 3})
    db.commit()
    assert job.status == JobStatus.SUCCEEDED.value
    assert job.result_summary == {"opportunities": 3}
    assert job.lease_expires_at is None


def test_non_retryable_failure_fails_fast(store, db) -> None:
    job = _enqueue(store, db, max_attempts=5)
    store.claim_one(db, worker_id="w1", lease_seconds=30)
    store.mark_running(db, job)
    store.fail(db, job, error=JobError(JobErrorCode.VALIDATION, "bad"),
               base_seconds=2, max_seconds=30)
    db.commit()
    assert job.status == JobStatus.FAILED.value
    assert job.attempt_count == 1  # no further attempts burned


def test_retryable_failure_schedules_backoff(store, db) -> None:
    now = datetime(2026, 7, 12, tzinfo=UTC)
    job = _enqueue(store, db, max_attempts=5, now=now)
    store.claim_one(db, worker_id="w1", lease_seconds=30, now=now)
    store.mark_running(db, job, now=now)
    store.fail(db, job, error=JobError(JobErrorCode.TRANSIENT), base_seconds=2,
               max_seconds=300, jitter_seed=job.id, now=now)
    db.commit()
    assert job.status == JobStatus.RETRY_WAIT.value
    assert job.available_at > now  # waits out the backoff
    assert JobStatus.RETRY_WAIT in ENQUEUED_STATUSES  # eligible to reclaim


def test_exhausted_retryable_failure_dead_letters(store, db) -> None:
    job = _enqueue(store, db, max_attempts=1)
    store.claim_one(db, worker_id="w1", lease_seconds=30)
    store.mark_running(db, job)  # attempt_count -> 1 == max_attempts
    store.fail(db, job, error=JobError(JobErrorCode.TRANSIENT), base_seconds=2,
               max_seconds=30)
    db.commit()
    assert job.status == JobStatus.DEAD_LETTERED.value


def test_cancel_pending_job_is_immediate(store, db) -> None:
    job = _enqueue(store, db)
    store.request_cancel(db, job)
    db.commit()
    assert job.status == JobStatus.CANCELLED.value
    assert job.cancelled_at is not None


def test_cancel_running_job_is_cooperative(store, db) -> None:
    job = _enqueue(store, db)
    store.claim_one(db, worker_id="w1", lease_seconds=30)
    store.mark_running(db, job)
    store.request_cancel(db, job)
    db.commit()
    assert job.status == JobStatus.CANCEL_REQUESTED.value
    assert job.cancel_requested_at is not None


def test_cancel_running_job_is_idempotent(store, db) -> None:
    # A second cancel on an already-cancel_requested running job must be a no-op,
    # never an illegal cancel_requested -> cancel_requested transition (which would
    # surface to the customer as a 500). The endpoint is safe to retry/double-click.
    job = _enqueue(store, db)
    store.claim_one(db, worker_id="w1", lease_seconds=30)
    store.mark_running(db, job)
    store.request_cancel(db, job)
    db.commit()
    assert job.status == JobStatus.CANCEL_REQUESTED.value
    first_requested_at = job.cancel_requested_at

    store.request_cancel(db, job)  # must not raise
    db.commit()
    assert job.status == JobStatus.CANCEL_REQUESTED.value
    assert job.cancel_requested_at == first_requested_at


def test_expired_lease_is_recovered_to_pending(store, db) -> None:
    past = datetime(2026, 7, 12, tzinfo=UTC)
    job = _enqueue(store, db, now=past)
    store.claim_one(db, worker_id="w1", lease_seconds=30, now=past)
    store.mark_running(db, job, now=past)
    # Lease expired well before "now".
    recovered = store.recover_expired_leases(db, now=past + timedelta(hours=1))
    db.commit()
    assert recovered == 1
    db.refresh(job)
    assert job.status == JobStatus.PENDING.value
    assert job.worker_id is None


def test_concurrent_claim_yields_single_winner(store, session_factory) -> None:
    setup = session_factory()
    _enqueue(store, setup)
    setup.close()

    winners: list[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(4)

    def worker(name: str) -> None:
        s = session_factory()
        try:
            barrier.wait()
            claimed = store.claim_one(s, worker_id=name, lease_seconds=30)
            if claimed is not None:
                with lock:
                    winners.append(name)
        finally:
            s.close()

    threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(winners) == 1, f"exactly one worker must claim the job, got {winners}"


def test_list_jobs_is_tenant_scoped(store, db) -> None:
    mine = _enqueue(store, db)
    _enqueue(store, db, organization_id="org-other", workspace_id="ws-other")
    rows, total = store.list_jobs(db, organization_id=ORG, workspace_id=WS)
    assert total == 1 and [r.id for r in rows] == [mine.id]
    # Cross-tenant read returns nothing.
    assert store.get_job(db, workspace_id="ws-other", job_id=mine.id) is None


# --------------------------------------------------------------------------- #
# Worker runner (end-to-end lifecycle through a handler)
# --------------------------------------------------------------------------- #
@register_handler("test.runner.ok")
def _ok_handler(ctx: HandlerContext) -> dict:
    return {"ran": True, "sr": ctx.payload.get("scout_request_id")}


@register_handler("test.runner.retry")
def _retry_handler(ctx: HandlerContext) -> dict:
    raise JobExecutionError(JobErrorCode.TRANSIENT, "temporary blip")


@register_handler("test.runner.badinput")
def _bad_handler(ctx: HandlerContext) -> dict:
    raise JobExecutionError(JobErrorCode.VALIDATION, "bad input")


@register_handler("test.runner.boom")
def _boom_handler(ctx: HandlerContext) -> dict:
    raise RuntimeError("secret-bearing message that must not be stored")


@register_handler("test.runner.cancel")
def _cancel_handler(ctx: HandlerContext) -> dict:
    raise JobExecutionError(JobErrorCode.CANCELLED, "cancelled mid-flight")


def _runner(session_factory) -> JobRunner:
    return JobRunner(settings=_local_settings(), session_factory=session_factory)


def _run_type(store, session_factory, job_type: str, **overrides) -> Job:
    setup = session_factory()
    job = _enqueue(store, setup, job_type=job_type, **overrides)
    setup.close()
    _runner(session_factory).poll_once(worker_id="w-runner")
    check = session_factory()
    try:
        return check.get(Job, job.id)
    finally:
        check.close()


def test_runner_success(store, session_factory) -> None:
    job = _run_type(store, session_factory, "test.runner.ok")
    assert job.status == JobStatus.SUCCEEDED.value
    assert job.result_summary == {"ran": True, "sr": "sr-1"}
    assert job.attempt_count == 1


def test_runner_retryable_failure_reschedules(store, session_factory) -> None:
    job = _run_type(store, session_factory, "test.runner.retry", max_attempts=5)
    assert job.status == JobStatus.RETRY_WAIT.value
    assert job.last_error_code == JobErrorCode.TRANSIENT.value


def test_runner_non_retryable_failure_fails_fast(store, session_factory) -> None:
    job = _run_type(store, session_factory, "test.runner.badinput", max_attempts=5)
    assert job.status == JobStatus.FAILED.value
    assert job.last_error_code == JobErrorCode.VALIDATION.value
    assert job.attempt_count == 1


def test_runner_unclassified_exception_hides_message(store, session_factory) -> None:
    job = _run_type(store, session_factory, "test.runner.boom", max_attempts=5)
    # Unclassified escapes are conservatively retryable...
    assert job.status == JobStatus.RETRY_WAIT.value
    assert job.last_error_code == JobErrorCode.TRANSIENT.value
    # ...and only the exception CLASS name is stored, never its message.
    assert job.last_error_summary == "RuntimeError"
    assert "secret" not in (job.last_error_summary or "")


def test_runner_unknown_type_fails_without_retry(store, session_factory) -> None:
    job = _run_type(store, session_factory, "test.runner.unregistered", max_attempts=5)
    assert job.status == JobStatus.FAILED.value
    assert job.last_error_code == JobErrorCode.UNSUPPORTED_TYPE.value


def test_runner_cooperative_cancel(store, session_factory) -> None:
    job = _run_type(store, session_factory, "test.runner.cancel")
    assert job.status == JobStatus.CANCELLED.value
    assert job.cancelled_at is not None


# --------------------------------------------------------------------------- #
# Enqueue service guards (reject before anything lands in the queue)
# --------------------------------------------------------------------------- #
def _ctx() -> ExecutionContext:
    return ExecutionContext.for_scout_request(organization_id=ORG, workspace_id=WS)


def test_enqueue_rejects_unknown_type(db) -> None:
    with pytest.raises(JobExecutionError) as exc:
        enqueue_job(db, job_type="not.a.real.type", context=_ctx(), payload={})
    assert exc.value.error.code is JobErrorCode.UNSUPPORTED_TYPE


def test_enqueue_rejects_oversized_payload(db) -> None:
    huge = {"blob": "x" * 200_000}  # far above the 64 KiB default bound
    with pytest.raises(JobExecutionError) as exc:
        enqueue_job(
            db,
            job_type=JobType.SCOUT_REQUEST_EXECUTE,
            context=_ctx(),
            payload=huge,
        )
    assert exc.value.error.code is JobErrorCode.PAYLOAD_TOO_LARGE


def test_enqueue_scout_request_persists_scoped_job(store, db) -> None:
    job = enqueue_job(
        db,
        job_type=JobType.SCOUT_REQUEST_EXECUTE,
        context=_ctx(),
        payload={"scout_request_id": "sr-9"},
        scout_request_id="sr-9",
    )
    db.commit()
    assert job.status == JobStatus.PENDING.value
    assert job.organization_id == ORG and job.workspace_id == WS
    assert job.scout_request_id == "sr-9"
