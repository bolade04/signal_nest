"""Retention purge per data class (``P6-PRIV-1``) — durations are NOT decided here.

How long each class is kept is ``O-5`` / ``P6-D05``, reserved to the operator and legal
and undecided. Every duration is therefore a setting with no default
(``retention_<class>_days`` on :class:`~app.core.config.Settings`): an unset duration
means *undecided*, and the purge **refuses** that class — it deletes nothing and says so
(``policy_unset``). Production refuses to start until all of them are set; see the
privacy block of ``app/core/config.py``.

Mechanics, identical for every class:

* the **cut-off** is the start of the current UTC day on the database clock minus the
  duration, so a row is never removed earlier than the policy allows, and every sweep on
  one day computes the same cut-off;
* a **batch** selects at most ``retention_purge_batch_size`` root rows older than the
  cut-off and deletes them with their dependents through the foreign-key walk
  (:func:`app.privacy.erasure.delete_rows`) — re-running a batch deletes nothing more, so
  every step is idempotent;
* **platform classes** (no workspace: sessions, account tokens, invitations, audit
  records and approvals) are purged by the sweep itself, each batch in its own committed
  transaction, up to ``retention_purge_max_batches`` batches per class per sweep;
* **workspace classes** (signals, opportunities, job history) are purged by the
  ``retention.purge`` durable job, enqueued once per workspace per class per day (the
  idempotency key carries the cut-off), purging only inside its own tenant scope; a full
  batch enqueues its successor, up to ``retention_purge_max_batches`` links. The job
  store is tenant-scoped by design — a job row needs an organization and a workspace —
  which is why the platform classes do not use it. Workspace members can see these jobs,
  but no customer role can cancel one (the job route refuses ``retention.purge``). A sweep
  counts only the jobs it created; a link the day already has is reported by its status,
  so a cancelled or failed one shows instead of being counted as enqueued.

The sweep is started by an operator or a scheduled task (``python -m app.privacy.cli
sweep``); no scheduler for it exists in the infrastructure yet.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.approvals.models import Approval
from app.audit.models import AuditLog
from app.auth.models import AuthSession, EmailVerificationToken, PasswordResetToken
from app.core.config import Settings, get_settings
from app.core.logging import get_logger, log_event
from app.db.clock import as_utc, database_now
from app.jobs.context import ExecutionContext
from app.jobs.models import Job
from app.jobs.status import TERMINAL_STATUSES, JobErrorCode, JobExecutionError, JobType
from app.opportunities.models import Opportunity
from app.organizations.models import OrganizationInvitation, Workspace
from app.privacy.erasure import ErasureCounts, delete_rows
from app.privacy.inventory import PLATFORM_CLASSES, WORKSPACE_CLASSES, RetentionClass
from app.signals.models import RawSignal, SignalCluster

logger = get_logger("signalnest.privacy.retention")


def retention_days(settings: Settings, data_class: RetentionClass) -> int | None:
    """The configured duration for ``data_class``; ``None`` means undecided."""
    return getattr(settings, data_class.setting_name)


def cutoff_for(now: datetime, days: int) -> datetime:
    """Start of ``now``'s UTC day minus ``days`` days."""
    day = as_utc(now).replace(hour=0, minute=0, second=0, microsecond=0)
    return day - timedelta(days=days)


# --------------------------------------------------------------------------- #
# Root selection per class
# --------------------------------------------------------------------------- #
#: (model, predicate builder) pairs per class. A predicate takes the cut-off. Workspace
#: classes are additionally restricted to one workspace by the caller.
_ROOTS: dict[RetentionClass, tuple[tuple[Any, Callable[[datetime], Any]], ...]] = {
    RetentionClass.AUTH_SESSIONS: ((AuthSession, lambda c: AuthSession.expires_at < c),),
    RetentionClass.ACCOUNT_TOKENS: (
        (PasswordResetToken, lambda c: PasswordResetToken.expires_at < c),
        (EmailVerificationToken, lambda c: EmailVerificationToken.expires_at < c),
    ),
    RetentionClass.INVITATIONS: (
        (
            OrganizationInvitation,
            lambda c: or_(
                OrganizationInvitation.accepted_at < c,
                OrganizationInvitation.revoked_at < c,
                OrganizationInvitation.expires_at < c,
            ),
        ),
    ),
    RetentionClass.SIGNAL_EVIDENCE: (
        (RawSignal, lambda c: RawSignal.created_at < c),
        (SignalCluster, lambda c: SignalCluster.created_at < c),
    ),
    RetentionClass.OPPORTUNITIES: ((Opportunity, lambda c: Opportunity.created_at < c),),
    RetentionClass.JOB_HISTORY: (
        (
            Job,
            lambda c: (Job.updated_at < c) & Job.status.in_([s.value for s in TERMINAL_STATUSES]),
        ),
    ),
    # Approvals are compliance records of the same class (W21 CF-1; O-6 rider).
    RetentionClass.AUDIT_LOGS: (
        (AuditLog, lambda c: AuditLog.created_at < c),
        (Approval, lambda c: Approval.created_at < c),
    ),
}


def _candidates(
    db: Session,
    data_class: RetentionClass,
    cutoff: datetime,
    *,
    limit: int,
    workspace_id: str | None,
) -> list[tuple[Any, list[str]]]:
    """Up to ``limit`` root ids per model of ``data_class`` that are past the cut-off."""
    out = []
    for model, predicate in _ROOTS[data_class]:
        stmt = select(model.id).where(predicate(cutoff))
        if workspace_id is not None:
            stmt = stmt.where(model.workspace_id == workspace_id)
        ids = db.execute(stmt.order_by(model.id).limit(limit)).scalars().all()
        out.append((model, list(ids)))
    return out


@dataclass
class BatchResult:
    counts: ErasureCounts
    #: True when no model of the class returned a full batch: nothing is left to purge.
    complete: bool


def purge_batch(
    db: Session,
    data_class: RetentionClass,
    cutoff: datetime,
    *,
    batch_size: int,
    workspace_id: str | None = None,
) -> BatchResult:
    """Delete one bounded batch of ``data_class`` rows older than ``cutoff``. Flushes only."""
    counts = ErasureCounts()
    complete = True
    for model, ids in _candidates(
        db, data_class, cutoff, limit=batch_size, workspace_id=workspace_id
    ):
        if ids:
            counts.merge(delete_rows(db, model.__table__, ids))
        if len(ids) >= batch_size:
            complete = False
    db.flush()
    return BatchResult(counts, complete)


def count_candidates(
    db: Session, data_class: RetentionClass, cutoff: datetime, *, workspace_id: str | None = None
) -> int:
    """How many root rows a purge would select (dry run). Read-only."""
    total = 0
    for model, predicate in _ROOTS[data_class]:
        stmt = select(func.count()).select_from(model).where(predicate(cutoff))
        if workspace_id is not None:
            stmt = stmt.where(model.workspace_id == workspace_id)
        total += db.execute(stmt).scalar_one()
    return total


# --------------------------------------------------------------------------- #
# The durable job (workspace classes)
# --------------------------------------------------------------------------- #
def purge_idempotency_key(data_class: RetentionClass, cutoff: datetime, sequence: int) -> str:
    return f"retention:{data_class.value}:{cutoff.date().isoformat()}:{sequence}"


def enqueue_purge(
    db: Session,
    *,
    organization_id: str,
    workspace_id: str,
    data_class: RetentionClass,
    cutoff: datetime,
    sequence: int = 0,
    now: datetime | None = None,
) -> Job:
    """Enqueue one ``retention.purge`` link for one workspace (idempotent per key)."""
    from app.jobs.service import enqueue_job

    if data_class not in WORKSPACE_CLASSES:
        raise ValueError(f"{data_class.value} is not a workspace retention class")
    return enqueue_job(
        db,
        job_type=JobType.RETENTION_PURGE,
        context=ExecutionContext(organization_id=organization_id, workspace_id=workspace_id),
        payload={
            "data_class": data_class.value,
            "cutoff": cutoff.isoformat(),
            "sequence": sequence,
        },
        idempotency_key=purge_idempotency_key(data_class, cutoff, sequence),
        now=now,
    )


def run_purge_job(
    db: Session,
    *,
    context: ExecutionContext,
    payload: dict[str, Any],
    now: datetime,
    is_cancelled: Callable[[], bool] = lambda: False,
) -> dict[str, Any]:
    """Execute one ``retention.purge`` link: one bounded batch inside the job's workspace.

    Refuses (non-retryable ``policy``) when the class's duration is no longer set, or when
    the cut-off it was enqueued with is later than the current policy allows (the duration
    was lengthened since): a stale job must never delete what the policy now keeps.
    Returns a safe summary: class, counts per table, completeness, link number.
    """
    raw_class = payload.get("data_class")
    raw_cutoff = payload.get("cutoff")
    sequence = payload.get("sequence")
    try:
        data_class = RetentionClass(raw_class)
    except ValueError as exc:
        raise JobExecutionError(JobErrorCode.VALIDATION, "unknown retention class") from exc
    if data_class not in WORKSPACE_CLASSES:
        raise JobExecutionError(JobErrorCode.VALIDATION, "not a workspace retention class")
    if type(sequence) is not int or sequence < 0:
        raise JobExecutionError(JobErrorCode.VALIDATION, "sequence must be a non-negative int")
    if not isinstance(raw_cutoff, str):
        raise JobExecutionError(JobErrorCode.VALIDATION, "cutoff must be an ISO datetime")
    try:
        cutoff = as_utc(datetime.fromisoformat(raw_cutoff))
    except ValueError as exc:
        raise JobExecutionError(JobErrorCode.VALIDATION, "cutoff must be an ISO datetime") from exc

    settings = get_settings()
    days = retention_days(settings, data_class)
    if days is None:
        raise JobExecutionError(
            JobErrorCode.POLICY, f"retention duration for {data_class.value} is not set"
        )
    if cutoff > cutoff_for(now, days):
        raise JobExecutionError(
            JobErrorCode.POLICY, f"cutoff is later than the {data_class.value} policy allows"
        )
    if is_cancelled():
        raise JobExecutionError(JobErrorCode.CANCELLED, "cancelled before execution")

    batch = purge_batch(
        db,
        data_class,
        cutoff,
        batch_size=settings.retention_purge_batch_size,
        workspace_id=context.workspace_id,
    )
    chained = False
    if not batch.complete and sequence + 1 < settings.retention_purge_max_batches:
        enqueue_purge(
            db,
            organization_id=context.organization_id,
            workspace_id=context.workspace_id,
            data_class=data_class,
            cutoff=cutoff,
            sequence=sequence + 1,
            now=now,
        )
        chained = True
    log_event(
        logger,
        "privacy.retention.purged",
        outcome="success",
        data_class=data_class.value,
        rows_deleted=sum(batch.counts.deleted.values()),
        sequence=sequence,
        complete=batch.complete,
    )
    return {
        "data_class": data_class.value,
        "sequence": sequence,
        "complete": batch.complete,
        "successor_enqueued": chained,
        **batch.counts.as_dict(),
    }


# --------------------------------------------------------------------------- #
# The sweep (entry point for an operator or a scheduled task)
# --------------------------------------------------------------------------- #
@dataclass
class ClassOutcome:
    data_class: str
    #: purged | enqueued | dry_run | policy_unset
    status: str
    retention_days: int | None = None
    cutoff: str | None = None
    batches: int = 0
    complete: bool | None = None
    #: Jobs this sweep created (workspace classes).
    jobs_enqueued: int = 0
    #: The day's jobs that already existed, by status; they are not enqueued again.
    jobs_existing: dict[str, int] = field(default_factory=dict)
    candidates: int | None = None
    deleted: dict[str, int] = field(default_factory=dict)


def sweep(
    session_factory: Callable[[], Session],
    *,
    classes: tuple[RetentionClass, ...] | None = None,
    dry_run: bool = False,
    settings: Settings | None = None,
) -> list[ClassOutcome]:
    """Run the retention purge for every (or the named) class. Commits per batch.

    A class whose duration is unset is reported ``policy_unset`` and left untouched.
    ``dry_run`` only counts the rows each class would select (and the workspaces a job
    would be enqueued for); it deletes and enqueues nothing.
    """
    settings = settings or get_settings()
    outcomes: list[ClassOutcome] = []
    for data_class in classes or tuple(RetentionClass):
        days = retention_days(settings, data_class)
        if days is None:
            outcomes.append(ClassOutcome(data_class.value, "policy_unset"))
            continue
        with session_factory() as db:
            cutoff = cutoff_for(database_now(db), days)
        outcome = ClassOutcome(data_class.value, "dry_run", days, cutoff.isoformat())
        if data_class in PLATFORM_CLASSES:
            _sweep_platform(session_factory, data_class, cutoff, outcome, settings, dry_run)
        else:
            _sweep_workspaces(session_factory, data_class, cutoff, outcome, dry_run)
        outcomes.append(outcome)
    return outcomes


def _sweep_platform(session_factory, data_class, cutoff, outcome, settings, dry_run) -> None:
    if dry_run:
        with session_factory() as db:
            outcome.candidates = count_candidates(db, data_class, cutoff)
        return
    totals = ErasureCounts()
    complete = False
    for _ in range(settings.retention_purge_max_batches):
        with session_factory() as db:
            batch = purge_batch(
                db, data_class, cutoff, batch_size=settings.retention_purge_batch_size
            )
            db.commit()
        outcome.batches += 1
        totals.merge(batch.counts)
        if batch.complete:
            complete = True
            break
    outcome.status = "purged"
    outcome.complete = complete
    outcome.deleted = totals.as_dict()["deleted"]
    log_event(
        logger,
        "privacy.retention.purged",
        outcome="success",
        data_class=data_class.value,
        rows_deleted=sum(totals.deleted.values()),
        batches=outcome.batches,
        complete=complete,
    )


def _sweep_workspaces(session_factory, data_class, cutoff, outcome, dry_run) -> None:
    with session_factory() as db:
        workspaces = db.execute(
            select(Workspace.id, Workspace.organization_id).order_by(Workspace.id)
        ).all()
        if dry_run:
            outcome.candidates = count_candidates(db, data_class, cutoff)
            return
        now = database_now(db)
        key = purge_idempotency_key(data_class, cutoff, 0)
        for workspace_id, organization_id in workspaces:
            existing = db.scalar(
                select(Job.status).where(
                    Job.organization_id == organization_id,
                    Job.workspace_id == workspace_id,
                    Job.idempotency_key == key,
                )
            )
            if existing is not None:
                outcome.jobs_existing[existing] = outcome.jobs_existing.get(existing, 0) + 1
                continue
            enqueue_purge(
                db,
                organization_id=organization_id,
                workspace_id=workspace_id,
                data_class=data_class,
                cutoff=cutoff,
                now=now,
            )
            outcome.jobs_enqueued += 1
        db.commit()
    outcome.status = "enqueued"


__all__ = [
    "BatchResult",
    "ClassOutcome",
    "count_candidates",
    "cutoff_for",
    "enqueue_purge",
    "purge_batch",
    "purge_idempotency_key",
    "retention_days",
    "run_purge_job",
    "sweep",
]
