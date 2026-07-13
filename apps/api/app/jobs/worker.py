"""Durable job worker.

Run with ``python -m app.jobs.worker`` (or ``npm run worker``). The worker is a
**separate process**; it is never auto-started inside the FastAPI app.

Lifecycle per the spec:

* derive a unique worker id and validate configuration + schema at startup,
* poll the durable store on a bounded interval (never a busy-spin),
* recover expired leases (an at-least-once safety net for crashed workers),
* atomically claim a due job, execute its handler, heartbeat to hold the lease,
  then drive it to a terminal state (succeeded / retry / failed / dead-lettered /
  cancelled),
* on ``SIGINT``/``SIGTERM`` stop claiming, let in-flight work finish within a
  bounded grace period, then exit — anything still running loses its lease and is
  safely recovered by the next worker.

Concurrency is a bounded number of identical worker threads, each with its own
session; the store's compare-and-set claim guarantees no two ever run the same
job.
"""

from __future__ import annotations

import os
import secrets
import signal
import socket
import threading
from collections.abc import Callable
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.logging import configure_logging, get_logger
from app.db.session import SessionLocal
from app.jobs.context import ExecutionContext
from app.jobs.models import Job
from app.jobs.registry import HandlerContext, resolve_handler
from app.jobs.status import (
    JobError,
    JobErrorCode,
    JobExecutionError,
    JobStatus,
)
from app.jobs.store import DurableJobStore, job_store, utcnow

logger = get_logger("signalnest.jobs.worker")

SessionFactory = Callable[[], Session]
Clock = Callable[[], datetime]


def derive_worker_id(configured: str | None = None) -> str:
    """A stable-if-configured, otherwise unique worker identity."""
    if configured:
        return configured
    return f"{socket.gethostname()}-{os.getpid()}-{secrets.token_hex(3)}"


def _context_from_job(job: Job) -> ExecutionContext:
    """Rebuild the tenant/location context from the durable row, never the message.

    Deriving the scope from the persisted job columns (not from the transported
    payload) means a tampered or stale message body cannot widen a job's scope.
    """
    return ExecutionContext.for_scout_request(
        organization_id=job.organization_id,
        workspace_id=job.workspace_id,
        location_id=job.location_id,
    )


class JobRunner:
    """Executes the lifecycle of a single claimed job. Thread/loop agnostic."""

    def __init__(
        self,
        *,
        settings: Settings,
        session_factory: SessionFactory = SessionLocal,
        store: DurableJobStore = job_store,
        clock: Clock = utcnow,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._store = store
        self._clock = clock

    # -- cooperative cancellation ------------------------------------------
    def _cancel_checker(self, job_id: str) -> Callable[[], bool]:
        def _check() -> bool:
            probe = self._session_factory()
            try:
                requested = probe.scalar(
                    select(Job.cancel_requested_at).where(Job.id == job_id)
                )
                return requested is not None
            finally:
                probe.close()

        return _check

    # -- heartbeat ----------------------------------------------------------
    def _start_heartbeat(self, job_id: str) -> tuple[threading.Event, threading.Thread]:
        stop = threading.Event()
        interval = self._settings.worker_heartbeat_seconds
        lease = self._settings.worker_lease_seconds

        def _beat() -> None:
            # ``wait`` returns True when signalled to stop, so a healthy run ticks
            # every ``interval`` and stops promptly when the job finishes.
            while not stop.wait(interval):
                hs = self._session_factory()
                try:
                    job = hs.get(Job, job_id)
                    if job is not None and job.status == JobStatus.RUNNING.value:
                        self._store.heartbeat(hs, job, lease_seconds=lease)
                        hs.commit()
                except Exception:  # pragma: no cover - best-effort lease renewal
                    hs.rollback()
                finally:
                    hs.close()

        thread = threading.Thread(target=_beat, name=f"hb-{job_id[:8]}", daemon=True)
        thread.start()
        return stop, thread

    # -- one job ------------------------------------------------------------
    def run_claimed(self, db: Session, job: Job) -> str:
        """Execute an already-claimed job to a terminal state; return that state."""
        # Persist RUNNING (and count the attempt) before doing any work, so a crash
        # mid-handler leaves an accurate, recoverable record.
        self._store.mark_running(db, job, now=self._clock())
        db.commit()

        job_id = job.id
        stop_hb, hb_thread = self._start_heartbeat(job_id)
        try:
            handler = resolve_handler(job.job_type)
            ctx = HandlerContext(
                db=db,
                context=_context_from_job(job),
                payload=dict(job.payload or {}),
                job_id=job_id,
                attempt=job.attempt_count,
                worker_id=job.worker_id,
                is_cancelled=self._cancel_checker(job_id),
            )
            result = handler(ctx)
            self._store.complete(db, job, result_summary=result, now=self._clock())
            db.commit()
            return JobStatus.SUCCEEDED.value
        except JobExecutionError as exc:
            db.rollback()
            if exc.error.code == JobErrorCode.CANCELLED:
                self._store.finish_cancel(db, job, now=self._clock())
                db.commit()
                return JobStatus.CANCELLED.value
            return self._fail(db, job, exc.error)
        except Exception as exc:  # unclassified escape -> conservative retryable
            db.rollback()
            # Record only the exception *class name*, never its message (which may
            # carry secrets or customer content).
            return self._fail(db, job, JobError(JobErrorCode.TRANSIENT, type(exc).__name__))
        finally:
            stop_hb.set()
            hb_thread.join(timeout=1.0)

    def _fail(self, db: Session, job: Job, error: JobError) -> str:
        self._store.fail(
            db,
            job,
            error=error,
            base_seconds=self._settings.job_retry_base_seconds,
            max_seconds=self._settings.job_retry_max_seconds,
            jitter_seed=job.id,
            now=self._clock(),
        )
        db.commit()
        return job.status

    # -- claim + run one ----------------------------------------------------
    def poll_once(self, *, worker_id: str) -> bool:
        """Recover leases, claim one due job and run it. Returns True if it ran."""
        db = self._session_factory()
        try:
            self._store.recover_expired_leases(db, now=self._clock())
            db.commit()
            job = self._store.claim_one(
                db,
                worker_id=worker_id,
                lease_seconds=self._settings.worker_lease_seconds,
                now=self._clock(),
                max_scan=max(self._settings.job_claim_batch_size, 1) * 20,
            )
            if job is None:
                return False
            self.run_claimed(db, job)
            return True
        finally:
            db.close()


class Worker:
    """Bounded multi-threaded worker with graceful signal-driven shutdown."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        session_factory: SessionFactory = SessionLocal,
        store: DurableJobStore = job_store,
        clock: Clock = utcnow,
    ) -> None:
        self._settings = settings or get_settings()
        self.worker_id = derive_worker_id(self._settings.worker_id)
        self._runner = JobRunner(
            settings=self._settings,
            session_factory=session_factory,
            store=store,
            clock=clock,
        )
        self._session_factory = session_factory
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    # -- startup validation -------------------------------------------------
    def validate(self) -> None:
        """Confirm configuration and that the durable schema is present."""
        if self._settings.job_queue_backend != "local":  # pragma: no cover - future
            raise RuntimeError(
                f"Unsupported job_queue_backend={self._settings.job_queue_backend!r}; "
                "only 'local' is implemented in this build."
            )
        db = self._session_factory()
        try:
            db.execute(select(Job.id).limit(1)).first()
        except Exception as exc:  # pragma: no cover - surfaced as a clear message
            raise RuntimeError(
                "Durable job schema is not initialized. Run migrations first "
                "(npm run migrate)."
            ) from exc
        finally:
            db.close()

    # -- loop ---------------------------------------------------------------
    def _loop(self) -> None:
        poll = self._settings.worker_poll_interval_seconds
        while not self._stop.is_set():
            try:
                did_work = self._runner.poll_once(worker_id=self.worker_id)
            except Exception:  # pragma: no cover - never let one job kill the loop
                logger.exception("worker.poll_error")
                did_work = False
            if not did_work:
                # Sleep between empty polls; wakes immediately on shutdown.
                self._stop.wait(poll)

    # -- signals ------------------------------------------------------------
    def _install_signal_handlers(self) -> None:
        def _handle(signum, _frame):  # noqa: ANN001
            logger.info("worker.signal", extra={"extra_fields": {"signal": signum}})
            self._stop.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, _handle)
            except ValueError:  # pragma: no cover - not on main thread (tests)
                pass

    def request_stop(self) -> None:
        self._stop.set()

    def start(self) -> None:
        for i in range(self._settings.worker_concurrency):
            t = threading.Thread(target=self._loop, name=f"worker-{i}", daemon=True)
            t.start()
            self._threads.append(t)

    def join(self, grace: float | None = None) -> None:
        grace = self._settings.worker_shutdown_grace_seconds if grace is None else grace
        for t in self._threads:
            t.join(timeout=grace)

    def run(self) -> None:
        """Blocking entrypoint: validate, spawn slots, wait, drain."""
        self.validate()
        self._install_signal_handlers()
        logger.info(
            "worker.start",
            extra={
                "extra_fields": {
                    "worker_id": self.worker_id,
                    "concurrency": self._settings.worker_concurrency,
                    "backend": self._settings.job_queue_backend,
                    "lease_seconds": self._settings.worker_lease_seconds,
                }
            },
        )
        self.start()
        try:
            while not self._stop.is_set():
                self._stop.wait(0.5)
        except KeyboardInterrupt:  # pragma: no cover - redundant with SIGINT handler
            self._stop.set()
        logger.info(
            "worker.draining",
            extra={"extra_fields": {"grace_seconds": self._settings.worker_shutdown_grace_seconds}},
        )
        self.join()
        logger.info("worker.stopped", extra={"extra_fields": {"worker_id": self.worker_id}})


def main() -> None:
    settings = get_settings()
    configure_logging("DEBUG" if settings.debug else "INFO")
    Worker(settings=settings).run()


if __name__ == "__main__":  # pragma: no cover - process entrypoint
    main()
