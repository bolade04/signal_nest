"""Worker-fleet registry service.

Stateless helper (every method takes an active session) that maintains the
``worker_registrations`` table: registration on startup, lifecycle transitions,
bounded heartbeats, stale detection and operator-facing reads.

Design guarantees:

* **Registration is idempotent / self-replacing.** Registering an already-known
  ``worker_id`` overwrites that row and resets it to ``starting`` — a process
  restart re-initializes its own registration rather than accumulating stale rows
  or colliding on the unique ``worker_id``.
* **Heartbeats are cheap.** A heartbeat only updates ``last_heartbeat_at`` (and,
  when reported, the coarse ``ready``/``busy``/``draining`` status). It writes no
  audit rows and persists no error detail.
* **Stale is derived, never owned.** A worker is stale when it is not stopped and
  its heartbeat age exceeds the threshold. Marking it stale changes only its
  registry status; it never affects job ownership, which is governed solely by
  job lease expiry in :class:`~app.jobs.store.DurableJobStore`.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.jobs.store import utcnow
from app.jobs.worker_models import WorkerRegistration
from app.jobs.worker_status import (
    TERMINAL_WORKER_STATUSES,
    WorkerStatus,
    ensure_worker_transition,
)


def _new_generation_token() -> str:
    """A fresh opaque per-registration fencing token (never a credential)."""
    return secrets.token_hex(16)


class WorkerRegistry:
    """DB-backed worker-fleet registry. Stateless; pass an active session."""

    # --- Registration -------------------------------------------------------
    def register(
        self,
        db: Session,
        *,
        worker_id: str,
        worker_type: str,
        concurrency: int,
        supported_job_types: list[str],
        queue_backend: str,
        application_version: str,
        build_revision: str | None = None,
        host_fingerprint: str | None = None,
        now: datetime | None = None,
    ) -> WorkerRegistration:
        """Register (or self-replace) this worker, initializing it to STARTING.

        A fresh ``generation_token`` is minted on every registration. The caller
        (the worker process) captures it and presents it on every heartbeat and
        status transition; because a restart rotates the token, any *older* process
        still holding a stale token is fenced out and can no longer mutate the row
        that replaced it.
        """
        now = now or utcnow()
        row = db.scalar(
            select(WorkerRegistration).where(WorkerRegistration.worker_id == worker_id)
        )
        if row is None:
            row = WorkerRegistration(worker_id=worker_id)
            db.add(row)
        # A fresh registration re-initializes the lifecycle regardless of the prior
        # status (a restart of the same worker id), so this is a direct reset, not
        # a validated transition.
        row.worker_type = worker_type
        row.status = WorkerStatus.STARTING.value
        row.started_at = now
        row.last_heartbeat_at = now
        row.stopped_at = None
        row.concurrency = concurrency
        row.supported_job_types = list(supported_job_types)
        row.queue_backend = queue_backend
        row.application_version = application_version
        row.build_revision = build_revision
        row.host_fingerprint = host_fingerprint
        # Rotate the fencing token so any prior generation is locked out.
        row.generation_token = _new_generation_token()
        db.flush()
        return row

    # --- Lifecycle transitions ---------------------------------------------
    def _get(self, db: Session, worker_id: str) -> WorkerRegistration | None:
        return db.scalar(
            select(WorkerRegistration).where(WorkerRegistration.worker_id == worker_id)
        )

    @staticmethod
    def _fenced_out(row: WorkerRegistration, generation_token: str | None) -> bool:
        """True if a supplied generation token does not match the row's current one.

        When ``generation_token`` is ``None`` the caller opts out of fencing (used
        by operator-driven sweeps that are not tied to a single worker generation).
        When it is supplied, a mismatch means an *older* process is trying to mutate
        a registration that a newer one has already replaced — so the mutation is
        refused. The row's token only ever changes at ``register`` (serialized by the
        unique ``worker_id``), so comparing the caller's fixed token to the row's
        current value is a sound fence.
        """
        return generation_token is not None and row.generation_token != generation_token

    def _transition(
        self,
        db: Session,
        worker_id: str,
        target: WorkerStatus,
        *,
        now: datetime | None = None,
        generation_token: str | None = None,
    ) -> WorkerRegistration | None:
        row = self._get(db, worker_id)
        if row is None:
            return None
        if self._fenced_out(row, generation_token):
            return None
        current = WorkerStatus(row.status)
        if current == target:
            return row
        ensure_worker_transition(current, target)
        row.status = target.value
        if target in TERMINAL_WORKER_STATUSES:
            row.stopped_at = now or utcnow()
        db.flush()
        return row

    def mark_ready(
        self,
        db: Session,
        worker_id: str,
        *,
        now: datetime | None = None,
        generation_token: str | None = None,
    ) -> WorkerRegistration | None:
        return self._transition(
            db, worker_id, WorkerStatus.READY, now=now, generation_token=generation_token
        )

    def mark_busy(
        self,
        db: Session,
        worker_id: str,
        *,
        now: datetime | None = None,
        generation_token: str | None = None,
    ) -> WorkerRegistration | None:
        return self._transition(
            db, worker_id, WorkerStatus.BUSY, now=now, generation_token=generation_token
        )

    def mark_draining(
        self,
        db: Session,
        worker_id: str,
        *,
        now: datetime | None = None,
        generation_token: str | None = None,
    ) -> WorkerRegistration | None:
        return self._transition(
            db, worker_id, WorkerStatus.DRAINING, now=now, generation_token=generation_token
        )

    def mark_stopped(
        self,
        db: Session,
        worker_id: str,
        *,
        now: datetime | None = None,
        generation_token: str | None = None,
    ) -> WorkerRegistration | None:
        return self._transition(
            db, worker_id, WorkerStatus.STOPPED, now=now, generation_token=generation_token
        )

    def mark_failed(
        self,
        db: Session,
        worker_id: str,
        *,
        now: datetime | None = None,
        generation_token: str | None = None,
    ) -> WorkerRegistration | None:
        return self._transition(
            db, worker_id, WorkerStatus.FAILED, now=now, generation_token=generation_token
        )

    # --- Heartbeat ----------------------------------------------------------
    def heartbeat(
        self,
        db: Session,
        worker_id: str,
        *,
        status: WorkerStatus | None = None,
        now: datetime | None = None,
        generation_token: str | None = None,
    ) -> WorkerRegistration | None:
        """Record liveness. Optionally set the coarse ready/busy/draining status.

        A stale worker that heartbeats again recovers to the reported status (or
        ``ready`` by default). No audit row is written. When a ``generation_token``
        is supplied it fences the heartbeat: a stale process whose token no longer
        matches the row cannot keep a replaced registration alive.
        """
        now = now or utcnow()
        row = self._get(db, worker_id)
        if row is None:
            return None
        if self._fenced_out(row, generation_token):
            return None
        current = WorkerStatus(row.status)
        row.last_heartbeat_at = now
        target = status
        if current == WorkerStatus.STALE and target is None:
            target = WorkerStatus.READY
        if target is not None and target != current:
            ensure_worker_transition(current, target)
            row.status = target.value
        db.flush()
        return row

    # --- Stale detection ----------------------------------------------------
    def find_stale(
        self,
        db: Session,
        *,
        stale_after_seconds: float,
        now: datetime | None = None,
    ) -> list[WorkerRegistration]:
        """Return non-stopped workers whose heartbeat is older than the threshold.

        ``stale`` and ``failed`` are already flagged, so they are excluded to keep
        the sweep idempotent.
        """
        now = now or utcnow()
        cutoff = now - timedelta(seconds=stale_after_seconds)
        excluded = {
            WorkerStatus.STOPPED.value,
            WorkerStatus.STALE.value,
            WorkerStatus.FAILED.value,
        }
        rows = db.execute(
            select(WorkerRegistration).where(
                WorkerRegistration.status.notin_(excluded),
                WorkerRegistration.last_heartbeat_at.is_not(None),
                WorkerRegistration.last_heartbeat_at < cutoff,
            )
        ).scalars().all()
        return list(rows)

    def sweep_stale(
        self,
        db: Session,
        *,
        stale_after_seconds: float,
        now: datetime | None = None,
    ) -> int:
        """Flag stale workers as STALE. Returns the number newly flagged."""
        now = now or utcnow()
        stale = self.find_stale(db, stale_after_seconds=stale_after_seconds, now=now)
        for row in stale:
            ensure_worker_transition(WorkerStatus(row.status), WorkerStatus.STALE)
            row.status = WorkerStatus.STALE.value
        db.flush()
        return len(stale)

    # --- Reads --------------------------------------------------------------
    def get(self, db: Session, worker_id: str) -> WorkerRegistration | None:
        return self._get(db, worker_id)

    def list_workers(
        self, db: Session, *, limit: int = 100
    ) -> list[WorkerRegistration]:
        return list(
            db.execute(
                select(WorkerRegistration)
                .order_by(WorkerRegistration.last_heartbeat_at.desc())
                .limit(limit)
            ).scalars()
        )

    def status_counts(self, db: Session) -> dict[str, int]:
        rows = db.execute(
            select(WorkerRegistration.status, func.count()).group_by(WorkerRegistration.status)
        ).all()
        return {str(k): int(v) for k, v in rows}

    def _fresh_active_conditions(
        self, *, stale_after_seconds: float, now: datetime
    ) -> tuple:
        """The single authoritative predicate for a *currently live* worker.

        A worker is active only when it is in an active status (``ready``/``busy``)
        *and* its heartbeat is fresh (within the stale threshold). The freshness
        cutoff is the exact complement of :meth:`find_stale` (which flags
        ``last_heartbeat_at < cutoff``), so a ready/busy worker is either counted
        active here or counted overdue by stale detection — never both, never
        neither. This is why readiness stays accurate even when the whole fleet is
        dead and no sweep has run: liveness is derived from heartbeat age, not from
        a status another process must have swept.
        """
        cutoff = now - timedelta(seconds=stale_after_seconds)
        active = {WorkerStatus.READY.value, WorkerStatus.BUSY.value}
        return (
            WorkerRegistration.status.in_(active),
            WorkerRegistration.last_heartbeat_at.is_not(None),
            WorkerRegistration.last_heartbeat_at >= cutoff,
        )

    def active_count(
        self, db: Session, *, stale_after_seconds: float, now: datetime | None = None
    ) -> int:
        """Workers currently ready/busy *and* fresh (a live, working fleet).

        Requires a fresh heartbeat within ``stale_after_seconds``, so an overdue
        worker that has not yet been swept to ``stale`` is not mistaken for a live
        one. This keeps ``require_worker_fleet`` readiness accurate for a dead
        fleet, where nothing is left to run the sweep.
        """
        now = now or utcnow()
        return int(
            db.scalar(
                select(func.count()).where(
                    *self._fresh_active_conditions(
                        stale_after_seconds=stale_after_seconds, now=now
                    )
                )
            )
            or 0
        )

    def stale_count(
        self, db: Session, *, stale_after_seconds: float, now: datetime | None = None
    ) -> int:
        """Workers flagged stale, plus any whose heartbeat is already past due.

        Counts both already-flagged ``stale`` rows and not-yet-swept rows whose
        heartbeat age exceeds the threshold, so operators see the true stale
        picture without depending on the sweep cadence.
        """
        now = now or utcnow()
        flagged = int(
            db.scalar(
                select(func.count()).where(
                    WorkerRegistration.status == WorkerStatus.STALE.value
                )
            )
            or 0
        )
        overdue = len(self.find_stale(db, stale_after_seconds=stale_after_seconds, now=now))
        return flagged + overdue


#: Process-wide default registry. Stateless, so a single instance is safe.
worker_registry = WorkerRegistry()

__all__ = ["WorkerRegistry", "worker_registry"]
