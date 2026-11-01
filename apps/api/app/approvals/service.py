"""Approval decisions on recommendation briefs (Project Phase 5B, W21 lane P5B).

The single write path for :class:`~app.approvals.models.Approval` rows, plus the reads
every consumer of a decision must use. Conventions follow ``feedback/service.py`` and
``capabilities/service.py``: ``db`` first, keyword-only domain arguments, the caller owns
the transaction (this module flushes, never commits), domain errors only.

**What a decision binds to.** One decision is made on one exact, immutable subject version:
``subject_type = recommendation_brief``, ``subject_id`` = the brief row's id and
``subject_version`` = that row's ``brief_version``. A brief row never changes (append-only),
so the request must name the version the decider read; a different number is refused
(409, :data:`STALE_VERSION`) rather than silently re-targeted.

**Who may decide.** Exactly the approver role (``APPROVER_ROLES = {REVIEWER}``, operator
decision O-2; the route gates with ``require_approver`` and this service re-checks), and
never the author of the brief version (separation of duties): the author is read from
``created_by`` and, because that foreign key is ``SET NULL``, also from the id that leads
``created_by_snapshot`` (:mod:`app.core.actor_snapshot`).

**The latest decision controls.** Decisions are append-only; a change of mind is a new
row. The controlling decision of a subject version is the row with the highest
``decision_seq`` (:func:`controlling_decision`); consumers gate on that row, never on "an
approved row exists". ``revoked`` withdraws an approval, so it is accepted only while the
controlling decision is ``approved`` (409 :data:`NOTHING_TO_REVOKE` otherwise).
``approved`` and ``rejected`` may follow any state.

**``decision_seq`` allocation (binding review rule W21-REV-CF1-DATA).** The subject's brief
row is locked with ``SELECT … FOR UPDATE`` (:func:`subject_lock_select`, the
``capabilities/service.py`` ``_workspace_lock_select`` pattern, scoped to the subject
rather than the workspace) **before** ``max(decision_seq)`` is read, and the lock is held
until the caller's transaction ends. Under PostgreSQL's default READ COMMITTED isolation
each statement reads the latest committed state, so a second decider blocks on the lock
and then reads the first decider's committed row: the sequence cannot race. The unique key
``uq_approval_decision_seq`` stays the portable backstop: an insert that clashes on it
writes nothing and is refused with 409 :data:`CONCURRENT_DECISION` (the request's whole
transaction rolls back; the client re-reads and decides again). That backstop is what keeps
a REPEATABLE READ session (whose snapshot predates the lock, so the lock alone is not
sufficient there) and SQLite (where ``FOR UPDATE`` compiles to nothing) from ever storing a
duplicate. Both are proven in ``test_approvals_postgres.py`` and
``test_approvals_service.py``.

*Why a 409 and not an in-transaction retry:* a retry needs a SAVEPOINT, and with the
``pysqlite`` driver's legacy transaction handling a SAVEPOINT issued before the request's
first INSERT opens the transaction itself, so its RELEASE commits the decision before its
audit row exists (observed in ``test_approvals_api.py``, which keeps that property under
test). Under the lock and READ COMMITTED a clash cannot occur on PostgreSQL, so the retry
would buy nothing there.

**Audit.** Every decision writes one ``approval.<decision>`` audit row in the same
transaction (the capability-override precedent). The audited entity is the brief whose
approval state changes: ``previous_state`` is the formerly controlling decision (or
``None``), ``new_state`` the new one, with the approval id. Neither carries the free-text
note or any identity value beyond the actor column, which erasure already clears.

**Append-only.** Nothing here updates or deletes an approval, a brief or an audit row,
through the ORM or otherwise; the only writes are inserts.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Select, and_, exists, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased

from app.approvals.models import Approval
from app.approvals.policy import APPROVER_ROLES
from app.approvals.reasons import ApprovalReason, reason_allowed
from app.audit.actions import APPROVAL_DECISION_ACTIONS
from app.audit.service import record_audit
from app.auth.dependencies import TenantContext
from app.briefs.models import RecommendationBrief
from app.core.actor_snapshot import actor_snapshot
from app.core.enums import ApprovalDecision, ApprovalSubjectType, Role
from app.core.errors import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationDomainError,
)
from app.core.logging import get_logger, log_event

logger = get_logger("signalnest.approvals")

#: The subject type every 5B decision carries.
BRIEF_SUBJECT = ApprovalSubjectType.RECOMMENDATION_BRIEF.value

#: The audit ``entity_type`` of an approval decision: the brief whose state changes.
AUDIT_ENTITY_TYPE = "recommendation_brief"

#: Pagination bounds (mirroring the feedback history).
DEFAULT_LIMIT = 20
MAX_LIMIT = 100

# Refusal messages (static; none names another tenant's data).
BRIEF_NOT_FOUND = "Brief not found in this workspace."
AUTHOR_CANNOT_DECIDE = "The author of a brief cannot decide on it."
STALE_VERSION = "The brief version you decided on is not this brief's version."
NOTHING_TO_REVOKE = "Only an approved brief can be revoked."
CONCURRENT_DECISION = "Another decision on this brief was recorded at the same time. Try again."
REASON_MISMATCH = "reason_code does not match the decision"


@dataclass(frozen=True)
class ApprovalPage:
    """A bounded page of decisions, newest ``decision_seq`` first."""

    items: tuple[Approval, ...]
    total: int
    limit: int
    offset: int


@dataclass(frozen=True)
class BriefQueuePage:
    """A bounded page of briefs awaiting a decision, oldest first."""

    items: tuple[RecommendationBrief, ...]
    total: int
    limit: int
    offset: int


def _clamp(limit: int, offset: int) -> tuple[int, int]:
    return max(1, min(MAX_LIMIT, int(limit))), max(0, int(offset))


# --------------------------------------------------------------------------- #
# Scope
# --------------------------------------------------------------------------- #
def get_scoped_brief(
    db: Session, *, workspace_id: str, opportunity_id: str, brief_id: str
) -> RecommendationBrief:
    """The brief, only when it belongs to this workspace AND this opportunity; else 404.

    One answer for "does not exist", "another workspace's" and "another opportunity's", so a
    caller can never learn that a brief id exists elsewhere (the uniform non-enumerating
    404 of the feedback router and ``capabilities/service.py``).
    """
    brief = db.get(RecommendationBrief, brief_id)
    if (
        brief is None
        or brief.workspace_id != workspace_id
        or brief.opportunity_id != opportunity_id
    ):
        raise NotFoundError(BRIEF_NOT_FOUND)
    return brief


def is_brief_author(brief: RecommendationBrief, user_id: str) -> bool:
    """Whether ``user_id`` authored ``brief`` (by the live FK or by the snapshot's id).

    The snapshot is ``"<user id> <full name> <<email>>"``; its leading token is the author's
    id even after ``created_by`` was nulled. An erased snapshot (``"[erased]"``) names
    nobody, and an erased account cannot sign in to decide.
    """
    if brief.created_by is not None and brief.created_by == user_id:
        return True
    leading = (brief.created_by_snapshot or "").split(" ", 1)[0]
    return bool(leading) and leading == user_id


# --------------------------------------------------------------------------- #
# The latest-decision rule
# --------------------------------------------------------------------------- #
def _subject_filter(subject_type: str, subject_id: str, subject_version: int):
    return and_(
        Approval.subject_type == subject_type,
        Approval.subject_id == subject_id,
        Approval.subject_version == subject_version,
    )


def controlling_decision(
    db: Session, *, subject_type: str, subject_id: str, subject_version: int
) -> Approval | None:
    """The controlling decision of one exact subject version: the highest ``decision_seq``.

    ``None`` when no decision was ever recorded. Every consumer of an approval (5E export
    above all) must gate on this row, never on "an approved row exists".
    """
    return db.scalar(
        select(Approval)
        .where(_subject_filter(subject_type, subject_id, subject_version))
        .order_by(Approval.decision_seq.desc())
        .limit(1)
    )


def is_approved(db: Session, *, brief: RecommendationBrief) -> bool:
    """Whether the brief's exact version is currently approved (latest decision rule)."""
    latest = controlling_decision(
        db, subject_type=BRIEF_SUBJECT, subject_id=brief.id, subject_version=brief.brief_version
    )
    return latest is not None and latest.decision == ApprovalDecision.APPROVED.value


# --------------------------------------------------------------------------- #
# decision_seq: lock, read, insert (a clash is a 409)
# --------------------------------------------------------------------------- #
def subject_lock_select(brief_id: str) -> Select:
    """``SELECT … FOR UPDATE`` on the subject's brief row (the per-subject serializer).

    Factored out so a test can compile it against the PostgreSQL dialect and prove it
    emits ``FOR UPDATE`` (the ``_workspace_lock_select`` convention). Selects the id column,
    not the entity, so the identity map is untouched.
    """
    return (
        select(RecommendationBrief.id).where(RecommendationBrief.id == brief_id).with_for_update()
    )


def _lock_subject(db: Session, brief_id: str) -> None:
    """Hold the subject's row lock until the caller's transaction ends.

    Taken before ``max(decision_seq)`` is read. On SQLite ``FOR UPDATE`` compiles to a plain
    read (the single-writer engine serializes writers); correctness there rests on the
    unique key, whose clash is a 409.
    """
    if db.execute(subject_lock_select(brief_id)).first() is None:
        # The brief vanished between the scoped read and the lock (deleted with its
        # workspace or opportunity): the same non-enumerating answer.
        raise NotFoundError(BRIEF_NOT_FOUND)


def _next_decision_seq(db: Session, *, subject_id: str, subject_version: int) -> int:
    """``max(decision_seq) + 1`` for the subject version (1 for the first decision)."""
    current = db.scalar(
        select(func.max(Approval.decision_seq)).where(
            _subject_filter(BRIEF_SUBJECT, subject_id, subject_version)
        )
    )
    return int(current or 0) + 1


def _is_sequence_clash(exc: IntegrityError) -> bool:
    """Whether the integrity failure is the ``decision_seq`` unique key (and nothing else).

    PostgreSQL names the constraint; SQLite names the table's columns. Any other integrity
    failure (a CHECK, a foreign key) is a defect, not a race, and is re-raised.
    """
    text = str(getattr(exc, "orig", exc))
    return "uq_approval_decision_seq" in text or (
        "UNIQUE constraint failed: approvals.subject_type" in text
    )


def _insert_decision(db: Session, *, values: dict) -> Approval:
    """Insert one decision with the next ``decision_seq``, read under the subject lock.

    A clash on ``uq_approval_decision_seq`` (only possible where the lock does not
    serialize: REPEATABLE READ, SQLite, or a writer that skipped the lock) writes nothing and
    is refused with 409; the caller's transaction is rolled back by the request scope. Any
    other integrity failure is re-raised.
    """
    seq = _next_decision_seq(
        db, subject_id=values["subject_id"], subject_version=values["subject_version"]
    )
    approval = Approval(**values, decision_seq=seq)
    db.add(approval)
    try:
        db.flush()
    except IntegrityError as exc:
        if not _is_sequence_clash(exc):
            raise
        log_event(
            logger,
            "approval_decision_sequence_clash",
            outcome="refused",
            workspace_id=values["workspace_id"],
        )
        raise ConflictError(CONCURRENT_DECISION, code="approval_concurrent_decision") from exc
    return approval


# --------------------------------------------------------------------------- #
# The write path
# --------------------------------------------------------------------------- #
def record_decision(
    db: Session,
    *,
    ctx: TenantContext,
    brief: RecommendationBrief,
    decision: ApprovalDecision,
    subject_version: int,
    reason_code: ApprovalReason | None = None,
    note: str | None = None,
) -> Approval:
    """Append one decision on ``brief``'s exact version, with its audit row; flush only.

    Order of checks (each refusal writes nothing): the approver role (403), the brief's
    tenant scope (404), separation of duties (403), the exact version (409), reason
    polarity (422); then the subject lock, the transition rule against the controlling
    decision read under that lock (409), the sequenced insert and the audit row.
    """
    decision = ApprovalDecision(decision)
    role = Role(ctx.role)
    # Defence in depth behind the route's exact-set gate (O-1/O-2): rank plays no part.
    if role not in APPROVER_ROLES:
        raise PermissionDeniedError(f"Role '{role.value}' is not permitted for this action.")
    if brief.workspace_id != ctx.workspace.id or brief.organization_id != ctx.organization.id:
        raise NotFoundError(BRIEF_NOT_FOUND)
    if is_brief_author(brief, ctx.user.id):
        raise PermissionDeniedError(AUTHOR_CANNOT_DECIDE, code="approval_author_conflict")
    if subject_version != brief.brief_version:
        raise ConflictError(STALE_VERSION, code="approval_stale_version")
    reason = None if reason_code is None else ApprovalReason(reason_code)
    if not reason_allowed(decision, reason):
        raise ValidationDomainError(REASON_MISMATCH)

    _lock_subject(db, brief.id)
    latest = controlling_decision(
        db, subject_type=BRIEF_SUBJECT, subject_id=brief.id, subject_version=brief.brief_version
    )
    if decision is ApprovalDecision.REVOKED and (
        latest is None or latest.decision != ApprovalDecision.APPROVED.value
    ):
        raise ConflictError(NOTHING_TO_REVOKE, code="approval_nothing_to_revoke")

    approval = _insert_decision(
        db,
        values={
            "organization_id": ctx.organization.id,
            "workspace_id": ctx.workspace.id,
            "subject_type": BRIEF_SUBJECT,
            "subject_id": brief.id,
            "subject_version": brief.brief_version,
            "brief_id": brief.id,
            "decision": decision.value,
            "reason_code": None if reason is None else reason.value,
            "note": note,
            "decided_by": ctx.user.id,
            "decided_by_snapshot": actor_snapshot(
                user_id=ctx.user.id, full_name=ctx.user.full_name, email=ctx.user.email
            ),
            "decided_role": role.value,
            # A decision on simulated (mock-model) content is itself simulated.
            "is_simulated": bool(brief.is_simulated),
        },
    )
    record_audit(
        db,
        organization_id=ctx.organization.id,
        workspace_id=ctx.workspace.id,
        actor_user_id=ctx.user.id,
        action=APPROVAL_DECISION_ACTIONS[decision],
        entity_type=AUDIT_ENTITY_TYPE,
        entity_id=brief.id,
        previous_state=None
        if latest is None
        else {"decision": latest.decision, "decision_seq": latest.decision_seq},
        new_state={
            "approval_id": approval.id,
            "decision": approval.decision,
            "decision_seq": approval.decision_seq,
            "subject_type": approval.subject_type,
            "subject_version": approval.subject_version,
            "reason_code": approval.reason_code,
        },
    )
    log_event(
        logger,
        "approval_decision_recorded",
        outcome="success",
        workspace_id=ctx.workspace.id,
        decision=approval.decision,
        decision_seq=approval.decision_seq,
    )
    return approval


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #
def list_decisions(
    db: Session,
    *,
    brief: RecommendationBrief,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> ApprovalPage:
    """The brief version's decisions, newest ``decision_seq`` first (the first item, when
    present, is the controlling decision). Read-only."""
    limit, offset = _clamp(limit, offset)
    base = select(Approval).where(
        Approval.workspace_id == brief.workspace_id,
        _subject_filter(BRIEF_SUBJECT, brief.id, brief.brief_version),
    )
    total = int(db.scalar(select(func.count()).select_from(base.subquery())) or 0)
    rows = tuple(
        db.execute(
            base.order_by(Approval.decision_seq.desc(), Approval.id.desc())
            .limit(limit)
            .offset(offset)
        ).scalars()
    )
    return ApprovalPage(items=rows, total=total, limit=limit, offset=offset)


def _awaiting_decision_select(workspace_id: str) -> Select:
    """Briefs of the workspace that await a decision.

    A brief awaits a decision when (a) it is the latest version of its opportunity's brief
    (a superseded version is decided through its successor) and (b) its controlling
    decision is absent or ``revoked`` (an approval was withdrawn). Approved and rejected
    versions are not in the queue.
    """
    newer = aliased(RecommendationBrief)
    superseded = exists().where(
        newer.workspace_id == RecommendationBrief.workspace_id,
        newer.opportunity_id == RecommendationBrief.opportunity_id,
        newer.brief_version > RecommendationBrief.brief_version,
    )
    latest_decision = (
        select(Approval.decision)
        .where(
            Approval.workspace_id == RecommendationBrief.workspace_id,
            Approval.subject_type == BRIEF_SUBJECT,
            Approval.subject_id == RecommendationBrief.id,
            Approval.subject_version == RecommendationBrief.brief_version,
        )
        .order_by(Approval.decision_seq.desc())
        .limit(1)
        .correlate(RecommendationBrief)
        .scalar_subquery()
    )
    return select(RecommendationBrief).where(
        RecommendationBrief.workspace_id == workspace_id,
        ~superseded,
        or_(latest_decision.is_(None), latest_decision == ApprovalDecision.REVOKED.value),
    )


def list_awaiting_decision(
    db: Session, *, workspace_id: str, limit: int = DEFAULT_LIMIT, offset: int = 0
) -> BriefQueuePage:
    """The workspace's approval queue, oldest first (a work queue). Read-only."""
    limit, offset = _clamp(limit, offset)
    base = _awaiting_decision_select(workspace_id)
    total = int(db.scalar(select(func.count()).select_from(base.subquery())) or 0)
    rows = tuple(
        db.execute(
            base.order_by(RecommendationBrief.created_at.asc(), RecommendationBrief.id.asc())
            .limit(limit)
            .offset(offset)
        ).scalars()
    )
    return BriefQueuePage(items=rows, total=total, limit=limit, offset=offset)


__all__ = [
    "AUDIT_ENTITY_TYPE",
    "AUTHOR_CANNOT_DECIDE",
    "BRIEF_NOT_FOUND",
    "BRIEF_SUBJECT",
    "CONCURRENT_DECISION",
    "NOTHING_TO_REVOKE",
    "STALE_VERSION",
    "ApprovalPage",
    "BriefQueuePage",
    "controlling_decision",
    "get_scoped_brief",
    "is_approved",
    "is_brief_author",
    "list_awaiting_decision",
    "list_decisions",
    "record_decision",
    "subject_lock_select",
]
