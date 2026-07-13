"""Durable job store — the local, SQLite-safe queue backend.

Responsibilities:

* **Enqueue** a job with tenant-scoped idempotency (a repeated key with the same
  payload is a no-op that returns the existing job; a repeated key with a
  *different* payload is a conflict).
* **Claim** a due job atomically with a compare-and-set ``UPDATE ... WHERE
  status = :expected`` — SQLite-safe (no ``SELECT ... FOR UPDATE SKIP LOCKED``),
  so exactly one worker can win a given row even under concurrency.
* Maintain a **lease**: the winning worker owns the job until ``lease_expires_at``;
  heartbeats extend it. A crashed worker's lease is recovered by
  :meth:`recover_expired_leases`, which returns the job to the queue (an audited
  replay) — this is what makes delivery **at-least-once**.
* Drive the lifecycle: running → success / retry-with-backoff / dead-letter /
  cancellation — always through the validated transitions in
  :mod:`app.jobs.status`.
* Append an **append-only** :class:`~app.jobs.models.JobEvent` for every change,
  carrying only safe metadata (never secrets, payloads or raw errors).

All times are timezone-aware UTC and injected by the caller (``now=...``) so the
lifecycle is deterministically testable with a fixed clock.
"""

from __future__ import annotations

import secrets
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.jobs.backoff import compute_backoff_seconds
from app.jobs.models import Job, JobEvent
from app.jobs.status import (
    ACTIVE_STATUSES,
    ENQUEUED_STATUSES,
    JobError,
    JobErrorCode,
    JobStatus,
    can_transition,
    ensure_transition,
    is_terminal,
)

logger = get_logger("signalnest.jobs.store")


def utcnow() -> datetime:
    return datetime.now(UTC)


def _new_lease_token() -> str:
    """A fresh, unguessable per-claim ownership token."""
    return secrets.token_hex(16)


class IdempotencyConflict(Exception):
    """A stored idempotency key was reused with a different payload."""

    def __init__(self, key: str) -> None:
        super().__init__(f"Idempotency key '{key}' was reused with a different payload")
        self.key = key


class JobLeaseLostError(Exception):
    """A worker tried to mutate a job it no longer owns.

    Raised when a fenced worker mutation (heartbeat / running / success / failure
    / retry / dead-letter / cancellation acknowledgement) matches **zero** rows
    because the job's lease was reclaimed by another worker (its token was
    rotated) or otherwise no longer belongs to the caller. It is a permanent
    condition for *this* attempt — the losing worker must simply stop; the new
    owner (or lease recovery) drives the job forward. It carries no raw database
    detail and is never surfaced to a customer as an infrastructure error.
    """

    def __init__(self, job_id: str) -> None:
        super().__init__(f"Lease ownership lost for job {job_id}")
        self.job_id = job_id


class DurableJobStore:
    """DB-backed durable queue. Stateless; every method takes an active session."""

    # --- Enqueue ------------------------------------------------------------
    def enqueue(
        self,
        db: Session,
        *,
        organization_id: str,
        workspace_id: str,
        job_type: str,
        payload: dict[str, Any],
        payload_hash: str,
        contract_version: str = "1",
        location_id: str | None = None,
        scout_request_id: str | None = None,
        idempotency_key: str | None = None,
        max_attempts: int = 5,
        priority: int = 0,
        scheduled_for: datetime | None = None,
        now: datetime | None = None,
    ) -> Job:
        """Persist a new job (or return the existing one for a repeated key)."""
        now = now or utcnow()

        if idempotency_key is not None:
            existing = db.scalar(
                select(Job).where(
                    Job.organization_id == organization_id,
                    Job.workspace_id == workspace_id,
                    Job.idempotency_key == idempotency_key,
                )
            )
            if existing is not None:
                if existing.payload_hash != payload_hash:
                    raise IdempotencyConflict(idempotency_key)
                return existing

        future = scheduled_for is not None and scheduled_for > now
        status = JobStatus.SCHEDULED if future else JobStatus.PENDING
        available_at = scheduled_for if future else now

        job = Job(
            organization_id=organization_id,
            workspace_id=workspace_id,
            location_id=location_id,
            scout_request_id=scout_request_id,
            job_type=job_type,
            contract_version=contract_version,
            payload=payload,
            payload_hash=payload_hash,
            idempotency_key=idempotency_key,
            status=status.value,
            priority=priority,
            attempt_count=0,
            max_attempts=max_attempts,
            available_at=available_at,
            scheduled_for=scheduled_for,
        )
        db.add(job)
        try:
            db.flush()
        except IntegrityError:
            # Lost an idempotency race with a concurrent enqueue: roll back this
            # insert and return the row the winner created.
            db.rollback()
            if idempotency_key is None:
                raise
            winner = db.scalar(
                select(Job).where(
                    Job.organization_id == organization_id,
                    Job.workspace_id == workspace_id,
                    Job.idempotency_key == idempotency_key,
                )
            )
            if winner is None:  # pragma: no cover - defensive
                raise
            if winner.payload_hash != payload_hash:
                raise IdempotencyConflict(idempotency_key) from None
            return winner

        self._record_event(db, job, event_type="enqueued", new_status=status)
        return job

    # --- Claiming (atomic compare-and-set) ----------------------------------
    def claim_one(
        self,
        db: Session,
        *,
        worker_id: str,
        lease_seconds: float,
        now: datetime | None = None,
        max_scan: int = 20,
    ) -> Job | None:
        """Atomically claim the highest-priority due job for ``worker_id``.

        Returns the claimed job, or ``None`` if nothing is due. Concurrency-safe:
        the claiming ``UPDATE`` is guarded by ``status = <observed>`` so only one
        worker transitions a given row; a lost race simply scans the next
        candidate (bounded by ``max_scan``).
        """
        now = now or utcnow()
        lease_expires = now + timedelta(seconds=lease_seconds)

        candidates = db.execute(
            select(Job.id, Job.status)
            .where(
                Job.status.in_(
                    [s.value for s in ENQUEUED_STATUSES]
                ),
                Job.available_at <= now,
            )
            .order_by(Job.priority.desc(), Job.available_at.asc(), Job.created_at.asc())
            .limit(max_scan)
        ).all()

        for job_id, observed_status in candidates:
            # A fresh token on every claim rotates ownership: any previous owner's
            # captured token no longer matches, so its late writes are fenced out.
            lease_token = _new_lease_token()
            result = db.execute(
                update(Job)
                .where(Job.id == job_id, Job.status == observed_status)
                .values(
                    status=JobStatus.CLAIMED.value,
                    worker_id=worker_id,
                    claimed_at=now,
                    lease_expires_at=lease_expires,
                    heartbeat_at=now,
                    lease_token=lease_token,
                )
            )
            if result.rowcount == 1:
                db.flush()
                job = db.get(Job, job_id)
                assert job is not None
                # A core UPDATE does not refresh an already-identity-mapped row, so
                # reload it: the caller relies on the persisted worker_id/lease_token
                # to fence its subsequent mutations.
                db.refresh(job)
                self._record_event(
                    db,
                    job,
                    event_type="claimed",
                    previous_status=JobStatus(observed_status),
                    new_status=JobStatus.CLAIMED,
                    worker_id=worker_id,
                )
                db.commit()
                return job
            # Lost the race for this row; try the next candidate.
            db.rollback()

        return None

    # --- Lease fencing ------------------------------------------------------
    def _fenced_update(
        self,
        db: Session,
        job: Job,
        *,
        worker_id: str,
        lease_token: str,
        allowed_source: Iterable[JobStatus],
        values: dict[str, Any],
    ) -> None:
        """Apply ``values`` only if the caller still owns the job's active lease.

        The ownership predicate lives **inside** the UPDATE (never a read-then-write
        in Python), so the check and the mutation are one atomic step. A worker
        matches only when the row still carries its ``worker_id`` *and* the exact
        ``lease_token`` it captured at claim time and the status is still one of
        ``allowed_source``. If another worker reclaimed the job (rotating the
        token) or it already moved on, zero rows match and we raise
        :class:`JobLeaseLostError` — no audit event is written for a lost owner.
        """
        result = db.execute(
            update(Job)
            .where(
                Job.id == job.id,
                Job.worker_id == worker_id,
                Job.lease_token == lease_token,
                Job.status.in_([s.value for s in allowed_source]),
            )
            .values(**values)
        )
        if result.rowcount != 1:
            raise JobLeaseLostError(job.id)
        # Reflect the committed row back onto the in-memory instance so the
        # subsequent audit event records the true post-mutation state.
        db.refresh(job)

    # --- Execution lifecycle ------------------------------------------------
    def mark_running(
        self,
        db: Session,
        job: Job,
        *,
        worker_id: str,
        lease_token: str,
        now: datetime | None = None,
    ) -> Job:
        """Begin an execution attempt (claimed -> running); counts the attempt.

        Fenced by lease ownership: only the worker that still holds this claim's
        token may start the attempt.
        """
        now = now or utcnow()
        previous = JobStatus(job.status)
        ensure_transition(previous, JobStatus.RUNNING)
        self._fenced_update(
            db,
            job,
            worker_id=worker_id,
            lease_token=lease_token,
            allowed_source=(JobStatus.CLAIMED, JobStatus.CANCEL_REQUESTED),
            values={
                "status": JobStatus.RUNNING.value,
                "attempt_count": Job.attempt_count + 1,
                "started_at": func.coalesce(Job.started_at, now),
                "heartbeat_at": now,
            },
        )
        self._record_event(
            db,
            job,
            event_type="started",
            previous_status=previous,
            new_status=JobStatus.RUNNING,
            worker_id=worker_id,
        )
        return job

    def heartbeat(
        self,
        db: Session,
        job: Job,
        *,
        worker_id: str,
        lease_token: str,
        lease_seconds: float,
        now: datetime | None = None,
    ) -> Job:
        """Extend the lease of a job the caller still owns (not audited: noisy).

        Fenced: a stale worker (whose lease was reclaimed) cannot renew, and the
        restriction to :data:`ACTIVE_STATUSES` means a heartbeat can never revive a
        terminal or retry-waiting job.
        """
        now = now or utcnow()
        self._fenced_update(
            db,
            job,
            worker_id=worker_id,
            lease_token=lease_token,
            allowed_source=ACTIVE_STATUSES,
            values={
                "heartbeat_at": now,
                "lease_expires_at": now + timedelta(seconds=lease_seconds),
            },
        )
        return job

    def complete(
        self,
        db: Session,
        job: Job,
        *,
        worker_id: str,
        lease_token: str,
        result_summary: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> Job:
        """Record a successful attempt (terminal SUCCEEDED). Fenced by ownership."""
        now = now or utcnow()
        ensure_transition(JobStatus(job.status), JobStatus.SUCCEEDED)
        self._fenced_update(
            db,
            job,
            worker_id=worker_id,
            lease_token=lease_token,
            allowed_source=(JobStatus.RUNNING, JobStatus.CANCEL_REQUESTED),
            values={
                "status": JobStatus.SUCCEEDED.value,
                "completed_at": now,
                "result_summary": result_summary or {},
                "lease_expires_at": None,
                "worker_id": None,
                "lease_token": None,
            },
        )
        self._record_event(
            db,
            job,
            event_type="succeeded",
            new_status=JobStatus.SUCCEEDED,
            worker_id=worker_id,
        )
        return job

    def fail(
        self,
        db: Session,
        job: Job,
        *,
        worker_id: str,
        lease_token: str,
        error: JobError,
        base_seconds: float,
        max_seconds: float,
        jitter_seed: str | None = None,
        now: datetime | None = None,
    ) -> Job:
        """Record a failed attempt and route it: retry / fail / dead-letter.

        Fenced by ownership, so a stale worker can neither burn an attempt nor
        overwrite the new owner's outcome.

        * non-retryable error  -> FAILED (fail fast, no more attempts)
        * retryable, attempts remain -> RETRY_WAIT (backoff, re-enqueued)
        * retryable, attempts exhausted -> DEAD_LETTERED
        """
        now = now or utcnow()
        error_code = error.code.value
        error_summary = error.safe_summary()
        source = (JobStatus.RUNNING, JobStatus.CANCEL_REQUESTED)

        attempts_remain = job.attempt_count < job.max_attempts

        if not error.retryable:
            ensure_transition(JobStatus(job.status), JobStatus.FAILED)
            self._fenced_update(
                db, job, worker_id=worker_id, lease_token=lease_token,
                allowed_source=source,
                values={
                    "status": JobStatus.FAILED.value,
                    "completed_at": now,
                    "last_error_code": error_code,
                    "last_error_summary": error_summary,
                    "worker_id": None,
                    "lease_token": None,
                    "lease_expires_at": None,
                },
            )
            self._record_event(
                db, job, event_type="failed", new_status=JobStatus.FAILED,
                error_code=error.code,
            )
            return job

        if attempts_remain:
            delay = compute_backoff_seconds(
                job.attempt_count,
                base_seconds=base_seconds,
                max_seconds=max_seconds,
                jitter_seed=jitter_seed,
            )
            ensure_transition(JobStatus(job.status), JobStatus.RETRY_WAIT)
            self._fenced_update(
                db, job, worker_id=worker_id, lease_token=lease_token,
                allowed_source=source,
                values={
                    "status": JobStatus.RETRY_WAIT.value,
                    "available_at": now + timedelta(seconds=delay),
                    "last_error_code": error_code,
                    "last_error_summary": error_summary,
                    "worker_id": None,
                    "lease_token": None,
                    "lease_expires_at": None,
                },
            )
            self._record_event(
                db,
                job,
                event_type="retry_scheduled",
                new_status=JobStatus.RETRY_WAIT,
                error_code=error.code,
                metadata={"delay_seconds": round(delay, 3), "attempt": job.attempt_count},
            )
            return job

        ensure_transition(JobStatus(job.status), JobStatus.DEAD_LETTERED)
        self._fenced_update(
            db, job, worker_id=worker_id, lease_token=lease_token,
            allowed_source=source,
            values={
                "status": JobStatus.DEAD_LETTERED.value,
                "completed_at": now,
                "last_error_code": error_code,
                "last_error_summary": error_summary,
                "worker_id": None,
                "lease_token": None,
                "lease_expires_at": None,
            },
        )
        self._record_event(
            db, job, event_type="dead_lettered", new_status=JobStatus.DEAD_LETTERED,
            error_code=error.code,
        )
        return job

    # --- Cancellation -------------------------------------------------------
    def request_cancel(self, db: Session, job: Job, *, now: datetime | None = None) -> Job:
        """Request cancellation.

        A not-yet-running job is cancelled immediately; a running/claimed job is
        marked ``cancel_requested`` and stopped cooperatively by the worker. A
        terminal job is left untouched.
        """
        now = now or utcnow()
        current = JobStatus(job.status)
        if is_terminal(current):
            return job
        # Idempotent: a running job whose cancellation was already requested stays
        # cancel_requested. Re-requesting must not attempt an illegal
        # cancel_requested -> cancel_requested transition (which would surface as a
        # 500), so the endpoint can be safely retried/double-clicked.
        if current == JobStatus.CANCEL_REQUESTED:
            return job
        job.cancel_requested_at = now

        if current in ENQUEUED_STATUSES:
            # Not yet owned by a worker: cancel immediately and clear any residual
            # ownership fields so no stale token can ever match this row again.
            self._transition(job, JobStatus.CANCELLED)
            job.cancelled_at = now
            job.worker_id = None
            job.lease_token = None
            job.lease_expires_at = None
            db.flush()
            self._record_event(
                db, job, event_type="cancelled", previous_status=current,
                new_status=JobStatus.CANCELLED,
            )
            return job

        # A worker owns it: record the request but leave worker_id/lease_token
        # intact so only that owner may acknowledge the cancellation.
        self._transition(job, JobStatus.CANCEL_REQUESTED)
        db.flush()
        self._record_event(
            db, job, event_type="cancel_requested", previous_status=current,
            new_status=JobStatus.CANCEL_REQUESTED,
        )
        return job

    def finish_cancel(
        self,
        db: Session,
        job: Job,
        *,
        worker_id: str,
        lease_token: str,
        now: datetime | None = None,
    ) -> Job:
        """A worker observed the cancel request and stopped (-> CANCELLED).

        Fenced by ownership: only the worker that still holds the claim may
        convert it to CANCELLED, so a stale worker cannot cancel a job another
        worker has since reclaimed.
        """
        now = now or utcnow()
        ensure_transition(JobStatus(job.status), JobStatus.CANCELLED)
        self._fenced_update(
            db,
            job,
            worker_id=worker_id,
            lease_token=lease_token,
            allowed_source=ACTIVE_STATUSES,
            values={
                "status": JobStatus.CANCELLED.value,
                "cancelled_at": now,
                "worker_id": None,
                "lease_token": None,
                "lease_expires_at": None,
            },
        )
        self._record_event(
            db, job, event_type="cancelled", new_status=JobStatus.CANCELLED,
        )
        return job

    # --- Recovery -----------------------------------------------------------
    def recover_expired_leases(
        self, db: Session, *, now: datetime | None = None, limit: int = 100
    ) -> int:
        """Return abandoned (expired-lease) jobs to the queue — an audited replay.

        This is the mechanism behind at-least-once delivery: a worker that
        crashed mid-attempt loses its lease and the job becomes eligible again
        (or dead-letters if it has exhausted its attempts).
        """
        now = now or utcnow()
        stale = db.execute(
            select(Job)
            .where(
                Job.status.in_([JobStatus.CLAIMED.value, JobStatus.RUNNING.value]),
                Job.lease_expires_at.is_not(None),
                Job.lease_expires_at < now,
            )
            .limit(limit)
        ).scalars().all()

        recovered = 0
        for job in stale:
            previous = JobStatus(job.status)
            # Clear ownership *including the token*: this is what fences out the
            # crashed/slow previous owner — its captured token no longer matches.
            job.worker_id = None
            job.lease_token = None
            job.lease_expires_at = None
            if job.attempt_count < job.max_attempts:
                job.available_at = now
                self._transition(job, JobStatus.PENDING)
                self._record_event(
                    db, job, event_type="lease_recovered", previous_status=previous,
                    new_status=JobStatus.PENDING,
                    metadata={"reason": "lease_expired"},
                )
            else:
                job.completed_at = now
                job.last_error_code = JobErrorCode.TIMEOUT.value
                job.last_error_summary = "lease expired and attempts exhausted"
                self._transition(job, JobStatus.DEAD_LETTERED)
                self._record_event(
                    db, job, event_type="dead_lettered", previous_status=previous,
                    new_status=JobStatus.DEAD_LETTERED, error_code=JobErrorCode.TIMEOUT,
                    metadata={"reason": "lease_expired"},
                )
            recovered += 1
        db.flush()
        return recovered

    # --- Reads --------------------------------------------------------------
    def get_job(self, db: Session, *, workspace_id: str, job_id: str) -> Job | None:
        job = db.get(Job, job_id)
        if job is None or job.workspace_id != workspace_id:
            return None
        return job

    def list_jobs(
        self,
        db: Session,
        *,
        organization_id: str,
        workspace_id: str,
        location_id: str | None = None,
        scout_request_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Job], int]:
        """Paginated, tenant-scoped listing. Returns ``(rows, total)``."""
        base = select(Job).where(
            Job.organization_id == organization_id,
            Job.workspace_id == workspace_id,
        )
        if location_id:
            base = base.where(Job.location_id == location_id)
        if scout_request_id:
            base = base.where(Job.scout_request_id == scout_request_id)
        if status:
            base = base.where(Job.status == status)

        total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
        rows = list(
            db.execute(
                base.order_by(Job.created_at.desc()).limit(limit).offset(offset)
            ).scalars()
        )
        return rows, total

    def list_events(self, db: Session, *, job_id: str, limit: int = 200) -> list[JobEvent]:
        return list(
            db.execute(
                select(JobEvent)
                .where(JobEvent.job_id == job_id)
                .order_by(JobEvent.created_at.asc())
                .limit(limit)
            ).scalars()
        )

    # --- Internals ----------------------------------------------------------
    def _transition(self, job: Job, target: JobStatus) -> None:
        ensure_transition(JobStatus(job.status), target)
        job.status = target.value

    def _record_event(
        self,
        db: Session,
        job: Job,
        *,
        event_type: str,
        previous_status: JobStatus | None = None,
        new_status: JobStatus | None = None,
        worker_id: str | None = None,
        error_code: JobErrorCode | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> JobEvent:
        event = JobEvent(
            job_id=job.id,
            organization_id=job.organization_id,
            workspace_id=job.workspace_id,
            location_id=job.location_id,
            event_type=event_type,
            previous_status=previous_status.value if previous_status else None,
            new_status=new_status.value if new_status else None,
            attempt=job.attempt_count,
            worker_id=worker_id,
            error_code=error_code.value if error_code else None,
            event_metadata=metadata or {},
        )
        db.add(event)
        db.flush()
        return event


class JobStore(Protocol):  # pragma: no cover - typing surface only
    """Interface a durable queue backend implements.

    Only :class:`DurableJobStore` (SQLite/PostgreSQL via SQLAlchemy) is provided
    in this slice. Redis/PostgreSQL-native adapters may implement the same
    surface later without changing callers.
    """

    def enqueue(self, db: Session, **kwargs: Any) -> Job: ...
    def claim_one(self, db: Session, **kwargs: Any) -> Job | None: ...
    def mark_running(self, db: Session, job: Job, **kwargs: Any) -> Job: ...
    def heartbeat(self, db: Session, job: Job, **kwargs: Any) -> Job: ...
    def complete(self, db: Session, job: Job, **kwargs: Any) -> Job: ...
    def fail(self, db: Session, job: Job, **kwargs: Any) -> Job: ...
    def request_cancel(self, db: Session, job: Job, **kwargs: Any) -> Job: ...
    def recover_expired_leases(self, db: Session, **kwargs: Any) -> int: ...


#: Process-wide default store. Stateless, so a single instance is safe to share.
job_store = DurableJobStore()


def can_cancel(job: Job) -> bool:
    """True if the job is in a state where cancellation is meaningful."""
    return not is_terminal(JobStatus(job.status))


def is_enqueued(job: Job) -> bool:
    return JobStatus(job.status) in ENQUEUED_STATUSES


__all__ = [
    "DurableJobStore",
    "IdempotencyConflict",
    "JobStore",
    "can_cancel",
    "can_transition",
    "is_enqueued",
    "job_store",
    "utcnow",
]
