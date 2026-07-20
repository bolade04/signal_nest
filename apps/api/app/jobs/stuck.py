"""Centralized, deterministic stuck-job classification (Phase 4A-B).

A durable job is *stuck* when a worker is still holding it for execution but has
evidently stopped making progress: the lease deadline has passed, or its
heartbeat has gone stale. This is derived **live** against an injected clock —
never persisted — mirroring the worker-registry "stale is derived from heartbeat
age, not owned" principle (``worker_registry.py``) and the lease-recovery
predicate in :mod:`app.jobs.store` (``_recovery_candidates_select``).

The classification is a single source of truth: one pure predicate
(:func:`is_job_stuck`) and the matching SQL conditions (:func:`_stuck_conditions`)
that stay in lockstep, so the operator read and any test agree exactly on what
"stuck" means, independent of any recovery-sweep cadence.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.jobs.models import Job
from app.jobs.status import JobStatus

#: A job can only be stuck while a worker is actively holding it for execution.
#: This is the claimed/running class — deliberately excluding ``CANCEL_REQUESTED``
#: (a cooperative stop already in progress), every enqueued status (no worker
#: holds it yet) and every terminal status (it is already done). It matches the
#: lease-recovery candidate set in :meth:`DurableJobStore._recovery_candidates_select`.
STUCK_CANDIDATE_STATUSES: frozenset[JobStatus] = frozenset(
    {JobStatus.CLAIMED, JobStatus.RUNNING}
)


def _as_aware(value: datetime) -> datetime:
    """Treat a naive timestamp (SQLite returns these) as UTC for comparison."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def is_job_stuck(job: Job, *, now: datetime, stale_after_seconds: float) -> bool:
    """True iff ``job`` meets the stuck predicate at ``now`` (Phase 4A plan §8.10).

    All must hold:

    * its status is a non-terminal, in-flight execution status
      (:data:`STUCK_CANDIDATE_STATUSES` — claimed/running), **and**
    * it holds a lease whose deadline has passed (``lease_expires_at < now``)
      **or** its ``heartbeat_at`` is older than ``stale_after_seconds``.

    A cancel-requested / cancelled / terminal job is never stuck (excluded by the
    candidate status set). The evaluation is pure and clock-injected, so it is
    accurate regardless of whether a recovery sweep has run.
    """
    if JobStatus(job.status) not in STUCK_CANDIDATE_STATUSES:
        return False
    cutoff = now - timedelta(seconds=stale_after_seconds)
    lease_expired = (
        job.lease_expires_at is not None and _as_aware(job.lease_expires_at) < now
    )
    heartbeat_stale = (
        job.heartbeat_at is not None and _as_aware(job.heartbeat_at) < cutoff
    )
    return lease_expired or heartbeat_stale


def _stuck_conditions(*, now: datetime, stale_after_seconds: float) -> tuple:
    """The SQL counterpart of :func:`is_job_stuck`, kept in lockstep with it."""
    cutoff = now - timedelta(seconds=stale_after_seconds)
    return (
        Job.status.in_([s.value for s in STUCK_CANDIDATE_STATUSES]),
        or_(
            and_(Job.lease_expires_at.is_not(None), Job.lease_expires_at < now),
            and_(Job.heartbeat_at.is_not(None), Job.heartbeat_at < cutoff),
        ),
    )


def count_stuck(db: Session, *, now: datetime, stale_after_seconds: float) -> int:
    """Live count of stuck jobs at ``now`` (cross-tenant; operator use only)."""
    return int(
        db.scalar(
            select(func.count())
            .select_from(Job)
            .where(*_stuck_conditions(now=now, stale_after_seconds=stale_after_seconds))
        )
        or 0
    )


def list_stuck(
    db: Session,
    *,
    now: datetime,
    stale_after_seconds: float,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[Job], int]:
    """Return ``(rows, total)`` of stuck jobs, oldest-first, bounded.

    Cross-tenant by design (operator-gated at the route). Ordering is stable and
    dialect-portable (``created_at`` then ``id``), so pagination is deterministic.
    """
    conds = _stuck_conditions(now=now, stale_after_seconds=stale_after_seconds)
    total = int(db.scalar(select(func.count()).select_from(Job).where(*conds)) or 0)
    rows = list(
        db.execute(
            select(Job)
            .where(*conds)
            .order_by(Job.created_at.asc(), Job.id.asc())
            .limit(limit)
            .offset(offset)
        ).scalars()
    )
    return rows, total


__all__ = [
    "STUCK_CANDIDATE_STATUSES",
    "count_stuck",
    "is_job_stuck",
    "list_stuck",
]
