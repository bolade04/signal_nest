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
* **platform classes** (sessions, account tokens, invitations, audit records, approvals
  and exports, none of which has a workspace; and in-app notifications, which do but are
  kept out of the job path by operator ruling D-N4 item 30 — never enqueued, no ``JobType``)
  are purged by the sweep itself, each batch in its own committed transaction, up to
  ``retention_purge_max_batches`` batches per class per sweep;
* **workspace classes** (signals, opportunities, job history) are purged by the
  ``retention.purge`` durable job, enqueued once per workspace per class per day (the
  idempotency key carries the cut-off), purging only inside its own tenant scope; a full
  batch enqueues its successor, up to ``retention_purge_max_batches`` links. The job
  store is tenant-scoped by design — a job row needs an organization and a workspace —
  which is why the platform classes do not use it. Workspace members can see these jobs,
  but no customer role can cancel one (the job route refuses ``retention.purge``). A sweep
  counts only the jobs it created; a link the day already has is reported by its status,
  so a cancelled or failed one shows instead of being counted as enqueued.

**Media (Phase 7A M0-B; media plan v3 §2.6, MD-6; CF-7A-0 contract §5).** ``media_uploads``
purges ``media_assets`` and ``media_objects`` **with their stored objects**: every object version,
every delete marker and every in-progress multipart upload of the root's keys
(:mod:`app.privacy.object_erasure`). Both media classes are platform classes (the sweep purges
them; no job does). Their rules:

* **Roots.** An asset is a root once its ``created_at`` is past the cut-off, whatever its state;
  an upload record (a ``media_objects`` row that is not ``stored``) likewise. A ``stored`` object
  is never a root of its own: it goes with its asset, so a purge never strips a live asset of its
  bytes. ``media_provenance`` and ``media_rights`` are ``CASCADE`` dependents and go with their
  object and asset; ``media_records`` has no root in M0, so a ``media_records`` duration never
  removes the provenance of an existing object or the rights record of an existing asset.
* **Objects first, rows second.** For each root the object erasure runs first; only roots whose
  object erasure is complete (no failed call, nothing left in a fresh listing) lose their rows.
  Every other root is **kept**, its rows being the handle the next sweep retries from, and the
  sweep reports the class incomplete (``complete: false``, ``kept``, ``objects``) and stops that
  class for this run. A store that cannot list versions (the adapter before M0-A0) erases nothing
  and keeps every root. Never is a row deleted over an object that may remain.
* **Both durations, or nothing.** While **any** media duration is unset, both media classes report
  ``policy_unset`` and nothing of them runs (AE-M0-5: "With any ``retention_media_*_days`` unset
  … the purge refuses to run").

The sweep is started by an operator or a scheduled task (``python -m app.privacy.cli
sweep``); no scheduler for it exists in the infrastructure yet.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from logging import ERROR
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.approvals.models import Approval
from app.audit.models import AuditLog
from app.auth.models import AuthSession, EmailVerificationToken, PasswordResetToken
from app.core.config import Settings, get_settings
from app.core.enums import MediaObjectState
from app.core.logging import get_logger, log_event
from app.db.clock import as_utc, database_now
from app.exports.models import Export
from app.jobs.context import ExecutionContext
from app.jobs.models import Job
from app.jobs.status import TERMINAL_STATUSES, JobErrorCode, JobExecutionError, JobType
from app.media.models import MEDIA_ASSET_AREA, MEDIA_KEY_ROOT, MediaAsset, MediaObject
from app.notifications.models import Notification
from app.opportunities.models import Opportunity
from app.organizations.models import OrganizationInvitation, Workspace
from app.privacy.erasure import ErasureCounts, delete_rows
from app.privacy.inventory import PLATFORM_CLASSES, WORKSPACE_CLASSES, RetentionClass
from app.privacy.object_erasure import (
    InvalidObjectScope,
    ObjectErasureReport,
    ObjectScope,
    erase_scopes,
    require_segment,
    resolve_object_store,
    supports_object_erasure,
    tenant_scope,
    unavailable_report,
)
from app.privacy.writer import mark_privacy_writer
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
    # Approvals (W21 CF-1) and exports (CF-3) are compliance records of the same class (O-6
    # rider: not indefinite by default).
    RetentionClass.AUDIT_LOGS: (
        (AuditLog, lambda c: AuditLog.created_at < c),
        (Approval, lambda c: Approval.created_at < c),
        (Export, lambda c: Export.created_at < c),
    ),
    # In-app notifications (Project Phase 5E; operator ruling D-N4 item 30): by age, once the
    # duration is set. Not a workspace class, so the sweep alone purges it (see above).
    RetentionClass.NOTIFICATIONS: ((Notification, lambda c: Notification.created_at < c),),
    # Phase 7A media (M0-B; contract §5): assets and upload records by age, with their stored
    # objects (every version, delete marker and in-progress upload; ``_purge_media_uploads``). A
    # ``stored`` object is never a root: it goes with its asset (CASCADE), as do the asset's
    # provenance and rights records.
    RetentionClass.MEDIA_UPLOADS: (
        (MediaAsset, lambda c: MediaAsset.created_at < c),
        (
            MediaObject,
            lambda c: (MediaObject.created_at < c)
            & (MediaObject.state != MediaObjectState.STORED.value),
        ),
    ),
    # No root in M0: every provenance and rights record cascades with an existing object or
    # asset, and a ``media_records`` purge must never select one (ruling CF7A0-3, DATA C2).
    # Records that do not cascade get roots here with CF-7A-1/2.
    RetentionClass.MEDIA_RECORDS: (),
}

#: The media classes (M0-B): none of them runs while any of their durations is unset.
MEDIA_CLASSES: frozenset[RetentionClass] = frozenset(
    {RetentionClass.MEDIA_UPLOADS, RetentionClass.MEDIA_RECORDS}
)


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
    #: True when no model of the class returned a full batch and no root was kept: nothing
    #: is left to purge.
    complete: bool
    #: Media (M0-B): what this batch's object erasure removed, merged over its roots; None for
    #: every class without stored objects.
    objects: ObjectErasureReport | None = None
    #: Media (M0-B): root rows kept, per table, because their objects could not all be erased
    #: (or no store could erase them). The next run retries them.
    kept: dict[str, int] = field(default_factory=dict)


def purge_batch(
    db: Session,
    data_class: RetentionClass,
    cutoff: datetime,
    *,
    batch_size: int,
    workspace_id: str | None = None,
    object_store: object | None = None,
) -> BatchResult:
    """Delete one bounded batch of ``data_class`` rows older than ``cutoff``. Flushes only.

    Marks the transaction as a sanctioned privacy-writer transaction first
    (:func:`app.privacy.writer.mark_privacy_writer`): on PostgreSQL the append-only backstop
    refuses the walk's deletes of guarded rows otherwise (D-N4 item 37).

    ``media_uploads`` erases each root's stored objects first and deletes only the rows whose
    objects are gone (``_purge_media_uploads``); ``object_store`` is the store to erase from
    (``None``: the application's, :func:`app.privacy.object_erasure.resolve_object_store`). It is
    ignored for every other class."""
    mark_privacy_writer(db)
    candidates = _candidates(db, data_class, cutoff, limit=batch_size, workspace_id=workspace_id)
    full = any(len(ids) >= batch_size for _model, ids in candidates)
    if data_class is RetentionClass.MEDIA_UPLOADS:
        result = _purge_media_uploads(db, candidates, object_store=object_store)
        result.complete = result.complete and not full
        db.flush()
        return result
    counts = ErasureCounts()
    for model, ids in candidates:
        if ids:
            counts.merge(delete_rows(db, model.__table__, ids))
    db.flush()
    return BatchResult(counts, not full)


def _purge_media_uploads(
    db: Session, candidates: list[tuple[Any, list[str]]], *, object_store: object | None
) -> BatchResult:
    """One ``media_uploads`` batch: erase each root's objects, then delete the rows of the roots
    whose objects are gone. Never deletes a row while an object of its root may remain."""
    ids = {model.__tablename__: list(found) for model, found in candidates}
    asset_ids = ids.get(MediaAsset.__tablename__, [])
    object_ids = ids.get(MediaObject.__tablename__, [])
    if not asset_ids and not object_ids:
        return BatchResult(ErasureCounts(), True, ObjectErasureReport(), {})

    # Each root's scope in the store, inside its own tenant's media prefix.
    assets = db.execute(
        select(MediaAsset.id, MediaAsset.organization_id, MediaAsset.workspace_id).where(
            MediaAsset.id.in_(asset_ids)
        )
    ).all()
    asset_keys: dict[str, list[str]] = {a: [] for a in asset_ids}
    for asset_id, storage_key in db.execute(
        select(MediaObject.asset_id, MediaObject.storage_key).where(
            MediaObject.asset_id.in_(asset_ids)
        )
    ).all():
        asset_keys[asset_id].append(storage_key)
    # An upload record whose asset is also a root goes with that asset (no overlapping scopes).
    taken = set(asset_ids)
    object_rows = db.execute(
        select(
            MediaObject.id,
            MediaObject.asset_id,
            MediaObject.organization_id,
            MediaObject.workspace_id,
            MediaObject.storage_key,
        ).where(MediaObject.id.in_(object_ids))
    ).all()
    objects = [row for row in object_rows if row.asset_id not in taken]
    with_asset = [row.asset_id for row in object_rows if row.asset_id in taken]

    scopes: dict[str, ObjectScope] = {}
    refused: set[str] = set()
    for row in assets:
        name = f"media_assets:{row.id}"
        try:
            tenant = _tenant_prefix(row.organization_id, row.workspace_id)
            area = f"{tenant}/{MEDIA_ASSET_AREA}/{require_segment(row.id, label='asset id')}"
            scopes[name] = tenant_scope(tenant, prefixes=(area,), keys=asset_keys[row.id])
        except InvalidObjectScope:
            refused.add(name)
    for row in objects:
        name = f"media_objects:{row.id}"
        try:
            tenant = _tenant_prefix(row.organization_id, row.workspace_id)
            scopes[name] = tenant_scope(tenant, keys=(row.storage_key,))
        except InvalidObjectScope:
            refused.add(name)

    store = resolve_object_store() if object_store is None else object_store
    if supports_object_erasure(store):
        reports = erase_scopes(store, scopes)
    else:
        reports = {name: unavailable_report() for name in scopes}
    for name in refused:
        reports[name] = ObjectErasureReport(refused_keys=1).settle()

    total = ObjectErasureReport()
    done: dict[str, list[str]] = {MediaAsset.__tablename__: [], MediaObject.__tablename__: []}
    kept: Counter = Counter()
    for name, report in sorted(reports.items()):
        table, _, root_id = name.partition(":")
        total.merge(report)
        if report.complete:
            done[table].append(root_id)
        else:
            kept[table] += 1
    # The upload records that went with an asset root are kept with it.
    kept_assets = set(asset_ids) - set(done[MediaAsset.__tablename__])
    kept[MediaObject.__tablename__] += sum(1 for a in with_asset if a in kept_assets)

    counts = ErasureCounts()
    if done[MediaAsset.__tablename__]:
        counts.merge(delete_rows(db, MediaAsset.__table__, done[MediaAsset.__tablename__]))
    if done[MediaObject.__tablename__]:
        counts.merge(delete_rows(db, MediaObject.__table__, done[MediaObject.__tablename__]))
    kept_now = {table: n for table, n in sorted(kept.items()) if n}
    if kept_now:
        log_event(
            logger,
            "privacy.retention.objects_incomplete",
            level=ERROR,
            outcome="failure",
            data_class=RetentionClass.MEDIA_UPLOADS.value,
            kept=kept_now,
            **{k: v for k, v in total.as_dict().items() if k != "store"},
        )
    return BatchResult(counts, not kept_now, total, kept_now)


def _tenant_prefix(organization_id: str, workspace_id: str) -> str:
    """``{organization_id}/{workspace_id}/media``: every key of the tenant's media is under it."""
    return "/".join(
        (
            require_segment(organization_id, label="organization id"),
            require_segment(workspace_id, label="workspace id"),
            MEDIA_KEY_ROOT,
        )
    )


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
    #: Media (M0-B): the object erasure's counts and status for the class (None for a class
    #: without stored objects, or before anything ran).
    objects: dict[str, Any] | None = None
    #: Media (M0-B): root rows kept per table because their objects could not all be erased;
    #: the next sweep retries them, and ``complete`` is false.
    kept: dict[str, int] = field(default_factory=dict)


def _policy_unset(settings: Settings, data_class: RetentionClass) -> bool:
    """Undecided: the class's own duration is unset, or, for a media class, any media class's."""
    if retention_days(settings, data_class) is None:
        return True
    return data_class in MEDIA_CLASSES and any(
        retention_days(settings, c) is None for c in MEDIA_CLASSES
    )


def sweep(
    session_factory: Callable[[], Session],
    *,
    classes: tuple[RetentionClass, ...] | None = None,
    dry_run: bool = False,
    settings: Settings | None = None,
    object_store: object | None = None,
) -> list[ClassOutcome]:
    """Run the retention purge for every (or the named) class. Commits per batch.

    A class whose duration is unset is reported ``policy_unset`` and left untouched; a media
    class is also ``policy_unset`` while any other media duration is unset (AE-M0-5).
    ``dry_run`` only counts the rows each class would select (and the workspaces a job
    would be enqueued for); it deletes and enqueues nothing, and lists no store.
    ``object_store`` is the store the media purge erases from (``None``: the application's).
    """
    settings = settings or get_settings()
    outcomes: list[ClassOutcome] = []
    for data_class in classes or tuple(RetentionClass):
        days = retention_days(settings, data_class)
        if _policy_unset(settings, data_class):
            outcomes.append(ClassOutcome(data_class.value, "policy_unset"))
            continue
        with session_factory() as db:
            cutoff = cutoff_for(database_now(db), days)
        outcome = ClassOutcome(data_class.value, "dry_run", days, cutoff.isoformat())
        if data_class in PLATFORM_CLASSES:
            _sweep_platform(
                session_factory, data_class, cutoff, outcome, settings, dry_run, object_store
            )
        else:
            _sweep_workspaces(session_factory, data_class, cutoff, outcome, dry_run)
        outcomes.append(outcome)
    return outcomes


def _sweep_platform(
    session_factory, data_class, cutoff, outcome, settings, dry_run, object_store=None
) -> None:
    if dry_run:
        with session_factory() as db:
            outcome.candidates = count_candidates(db, data_class, cutoff)
        return
    totals = ErasureCounts()
    objects: ObjectErasureReport | None = None
    kept: Counter = Counter()
    complete = False
    for _ in range(settings.retention_purge_max_batches):
        with session_factory() as db:
            batch = purge_batch(
                db,
                data_class,
                cutoff,
                batch_size=settings.retention_purge_batch_size,
                object_store=object_store,
            )
            db.commit()
        outcome.batches += 1
        totals.merge(batch.counts)
        if batch.objects is not None:
            objects = objects or ObjectErasureReport()
            objects.merge(batch.objects)
        kept.update(batch.kept)
        if batch.kept:
            # Roots kept for a retry: report the class incomplete and stop it for this run, so
            # a failing store is not asked again for the same roots batch after batch.
            break
        if batch.complete:
            complete = True
            break
    outcome.status = "purged"
    outcome.complete = complete
    outcome.deleted = totals.as_dict()["deleted"]
    if objects is not None:
        outcome.objects = objects.as_dict()
    outcome.kept = {table: n for table, n in sorted(kept.items()) if n}
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
    "MEDIA_CLASSES",
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
