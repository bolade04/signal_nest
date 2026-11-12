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

**The role is re-read under locks held until the decision commits (operator ruling D-N4 item
34, 2026-11-12: "re-read the approver's membership role under a FOR SHARE lock, for brief and
draft decisions").** The role the request resolved at its start (``ctx.role``) is checked
first, as before, but it is not what a decision rests on: before the subject's row lock,
:func:`_reread_approver_role` reads the decider's membership row again ``FOR SHARE``, and the
decision is refused (403, nothing written) unless that row still exists and its role is an
approver role. ``decided_role`` is the role read there. Every lock below is held until the
decision's transaction ends, so a demotion or removal of the decider waits for the decision to
commit, and a decision that comes second reads the committed change and is refused (proven on
PostgreSQL in ``test_approvals_role_reread.py``).

**Lock order (PostgreSQL) and why no cycle can form** (review W21-REV-DN4A r1, C-1: the r1 order
``subject → user → membership`` deadlocked with organization deletion, which deletes the
memberships before it reaches the subject). A decision takes, in this order:

======  ==================================  ================  ==================================
step    row                                 mode              why
======  ==================================  ================  ==================================
1       the decider's ``users`` row         ``KEY SHARE``     the decision's own foreign keys
                                                              (``approvals.decided_by``,
                                                              ``audit_logs.actor_user_id``) take
                                                              it anyway; first, so a path that
                                                              locks the user ``FOR UPDATE``
                                                              meets the decision before anything
2       the decision's ``organizations``    ``KEY SHARE``     every writer of a membership, and
        row                                                   organization deletion, locks this
                                                              row ``FOR UPDATE`` first
3       the decider's                       ``SHARE``         the ruled re-read; held to commit
        ``organization_members`` row
4       the subject (brief or draft)        ``UPDATE``        the ``decision_seq`` serializer
5       inserts: ``approvals``,             FK ``KEY SHARE``  only on rows 1 and 4, already held
        ``audit_logs``
======  ==================================  ================  ==================================

Every other path that locks one of those rows, and where it first meets the decision:

======================================  =====================================  =================
path (its order)                        first lock it shares with a decision   cycle?
======================================  =====================================  =================
organization deletion                   the organization (``FOR UPDATE`` vs    no
(owner's user → organization →          step 2), before it deletes any
members → workspaces → … → subject)     membership or subject
account deletion of the decider         the user (``FOR UPDATE`` vs step 1),   no
(user → its organizations → …)          the decision's first lock
a sole member deleting their own        the user (step 1); then the            no (the cycle
account, which erases the               organization (step 2)                  through the
organization (user → organization →                                            ``decided_by`` FK
… → subject)                                                                   at ``5f4131d8``
                                                                               is closed)
account deletion of another member      the organization (step 2), before it   no
(that user → organizations → its        touches the subject
``created_by`` on the subject)
role change or removal                  the organization (step 2)              no
(organization → membership)
invitation create, revoke, accept       the organization (step 2)              no
(organization → invitation, new
membership)
credential flows (user ``FOR UPDATE``   the user (step 1); they lock nothing   no
→ tokens, sessions)                     else a decision holds
creative generation (organization       the subject (step 4): a brief          no (a decision
``KEY SHARE`` → advisory → workspace →  ``FOR UPDATE`` at version allocation,  holds no advisory
brief ``FOR UPDATE``; a revision's      or a parent draft's FK ``KEY SHARE``   or workspace lock;
parent draft FK)                                                               step 2 is
                                                                               compatible)
retention purge of opportunities        the subject (step 4), deleted with     no (the purge
(… → brief → draft)                     its opportunity                        locks no user,
                                                                               organization or
                                                                               membership)
another decision                        the same order                         no
======================================  =====================================  =================

So each path either takes, as its first contested lock, a row the decision takes earlier in the
same order (the user, or the organization), or holds nothing the decision waits for. Two
transactions therefore never each hold a row the other waits on. The organization is taken
after the user because account deletion locks the user, then its organizations: the reverse
order would cycle with it. The membership ``FOR SHARE`` is still taken and held to commit, as
ruled; with step 2 before it, a membership writer that takes the organization first never
reaches it during a decision, and one that did not (none exists) would wait on it. On SQLite
every lock compiles to a plain read; the re-read still reads the persisted role, and the single
writer serializes.

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
duplicate. REPEATABLE READ is proven on PostgreSQL in ``test_approvals_postgres.py``
(``test_repeatable_read_is_not_sufficient_and_the_backstop_refuses_a_duplicate``); the clash
answer on SQLite in ``test_approvals_api.py``
(``TestSequence::test_a_sequence_clash_is_a_409_and_writes_nothing``).

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

**Creative drafts (Project Phase 5C, W21 lane P5C) — a disclosed plan deviation.** The 5C plan
assigns this file to 5C-W1 with a "sole change: blocked-review precondition" (5c:173). The 5B
service decided on briefs only, so that precondition had no write path to guard; by
coordinator ruling (W21 hand-back r1, item 1) this module also gains
:func:`record_draft_decision`, the brief decision's twin for ``subject_type =
creative_draft`` (``draft_id`` set, CF-2's ``ck_approval_draft_subject``): the same order of
checks, the exact ``draft_version`` (stale 409), the revoke rule, ``decision_seq`` read under
the draft row's lock, the audit row in the same transaction, inserts only. The precondition is
:func:`app.creative.service.draft_approval_refusal`: an ``approved`` decision is refused with
409 ``approval_review_blocked`` while it gives a reason (it re-reads the library's blocked
claims and required disclaimers as they are *now*); rejecting or revoking is never blocked
by the review. :func:`list_draft_decisions` reads a draft version's decisions. Drafts are not in
the approval queue (an I-5C follow-up). The only edit to an existing function is
``_next_decision_seq``, which now takes the subject type (``_insert_decision`` passes it).
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
from app.creative.models import CreativeDraft
from app.creative.service import draft_approval_refusal
from app.organizations.models import Organization, OrganizationMember, User

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
#: Subject-neutral since I-5C (W21-REV-P5C r2 O-r2-1): the clash answer is shared by brief and
#: draft decisions.
CONCURRENT_DECISION = "Another decision was recorded at the same time. Try again."
REASON_MISMATCH = "reason_code does not match the decision"
#: The decider's membership is gone when the role is re-read under the lock (the answer
#: ``app.auth.dependencies`` gives a non-member).
NOT_A_MEMBER = "You are not a member of this organization."


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
    """``SELECT id, brief_version … FOR UPDATE`` on the subject's brief row (the per-subject
    serializer).

    Factored out so a test can compile it against the PostgreSQL dialect and prove it
    emits ``FOR UPDATE`` (the ``_workspace_lock_select`` convention). Selects columns, not
    the entity, so the identity map is untouched; the version is read under the lock.
    """
    return (
        select(RecommendationBrief.id, RecommendationBrief.brief_version)
        .where(RecommendationBrief.id == brief_id)
        .with_for_update()
    )


def _lock_subject(db: Session, brief_id: str) -> int:
    """Hold the subject's row lock until the caller's transaction ends; return its version.

    Taken before ``max(decision_seq)`` is read. On SQLite ``FOR UPDATE`` compiles to a plain
    read (the single-writer engine serializes writers); correctness there rests on the
    unique key, whose clash is a 409.
    """
    row = db.execute(subject_lock_select(brief_id)).first()
    if row is None:
        # The brief vanished between the scoped read and the lock (deleted with its
        # workspace or opportunity): the same non-enumerating answer.
        raise NotFoundError(BRIEF_NOT_FOUND)
    return int(row.brief_version)


# --------------------------------------------------------------------------- #
# The approver's role, re-read under locks held to commit (D-N4 item 34)
# --------------------------------------------------------------------------- #
def approver_user_lock_select(user_id: str) -> Select:
    """``SELECT id FROM users … FOR KEY SHARE``: the decider's user row, the decision's first
    lock (step 1 of "Lock order" in the module docstring).

    Factored out so a test can compile it against the PostgreSQL dialect."""
    return select(User.id).where(User.id == user_id).with_for_update(read=True, key_share=True)


def approver_organization_lock_select(organization_id: str) -> Select:
    """``SELECT id FROM organizations … FOR KEY SHARE``: the decision's organization, step 2
    of "Lock order": every membership writer and organization deletion lock this row ``FOR
    UPDATE`` first, so they wait for the decision (or it for them) before either holds a
    membership or the subject.

    Factored out so a test can compile it against the PostgreSQL dialect."""
    return (
        select(Organization.id)
        .where(Organization.id == organization_id)
        .with_for_update(read=True, key_share=True)
    )


def approver_membership_select(organization_id: str, user_id: str) -> Select:
    """``SELECT role FROM organization_members … FOR SHARE``: the decider's membership row
    (step 3), held until the decision's transaction ends, so a demotion or removal waits for it.

    Selects the column, not the entity, so the identity map (and ``ctx``) is untouched and the
    role is the one committed now. Factored out so a test can compile it against the
    PostgreSQL dialect."""
    return (
        select(OrganizationMember.role)
        .where(
            OrganizationMember.organization_id == organization_id,
            OrganizationMember.user_id == user_id,
        )
        .with_for_update(read=True)
    )


def _reread_approver_role(db: Session, ctx: TenantContext) -> Role:
    """The decider's role as it stands now: steps 1 to 3 of "Lock order" (user ``KEY SHARE``,
    organization ``KEY SHARE``, membership ``SHARE``), taken before the subject's lock and held
    to commit. 403 unless the membership still exists and its role is an approver role
    (nothing written). A deleted organization or user leaves no membership: 403 too."""
    db.execute(approver_user_lock_select(ctx.user.id))
    db.execute(approver_organization_lock_select(ctx.organization.id))
    stored = db.execute(approver_membership_select(ctx.organization.id, ctx.user.id)).scalar()
    if stored is None:
        log_event(
            logger, "approval_refused_role_changed", outcome="refused",
            workspace_id=ctx.workspace.id, reason="not_a_member",
        )
        raise PermissionDeniedError(NOT_A_MEMBER)
    role = Role(stored)
    if role not in APPROVER_ROLES:
        log_event(
            logger, "approval_refused_role_changed", outcome="refused",
            workspace_id=ctx.workspace.id, reason="role_not_permitted",
        )
        raise PermissionDeniedError(f"Role '{role.value}' is not permitted for this action.")
    return role


def _next_decision_seq(
    db: Session, *, subject_id: str, subject_version: int, subject_type: str = BRIEF_SUBJECT
) -> int:
    """``max(decision_seq) + 1`` for the subject version (1 for the first decision)."""
    current = db.scalar(
        select(func.max(Approval.decision_seq)).where(
            _subject_filter(subject_type, subject_id, subject_version)
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
        db,
        subject_id=values["subject_id"],
        subject_version=values["subject_version"],
        subject_type=values["subject_type"],
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

    Order of checks (each refusal writes nothing): the approver role as the request resolved
    it (403), the brief's tenant scope (404), separation of duties (403), reason polarity
    (422); then the approver role re-read ``FOR SHARE`` from the decider's membership, under
    locks 1 to 3 of "Lock order" (403, D-N4 item 34); then, under the subject lock: the row
    still exists (404), the exact version (409), the transition rule against the controlling
    decision (409); then the sequenced insert and the audit row, whose ``decided_role`` is the
    re-read role.
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
    reason = None if reason_code is None else ApprovalReason(reason_code)
    if not reason_allowed(decision, reason):
        raise ValidationDomainError(REASON_MISMATCH)

    # The decider's role as it stands now, under locks 1 to 3 of "Lock order" (D-N4 item 34;
    # before the subject, review W21-REV-DN4A r1 C-1). Then everything that decides the
    # sequence is read under the subject's row lock, in this transaction (review rule
    # W21-REV-CF1-DATA): the row's existence (404 if it is gone), the exact version, the
    # controlling decision, then max(decision_seq).
    role = _reread_approver_role(db, ctx)
    version = _lock_subject(db, brief.id)
    if subject_version != version:
        raise ConflictError(STALE_VERSION, code="approval_stale_version")
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


# --------------------------------------------------------------------------- #
# Creative drafts (Project Phase 5C)
# --------------------------------------------------------------------------- #
#: The subject type of a decision on one generated draft version (CF-2).
DRAFT_SUBJECT = ApprovalSubjectType.CREATIVE_DRAFT.value
#: The audit ``entity_type`` of a decision on a draft: the draft whose state changes.
DRAFT_AUDIT_ENTITY_TYPE = "creative_draft"

DRAFT_NOT_FOUND = "Draft not found in this workspace."
AUTHOR_CANNOT_DECIDE_DRAFT = "The author of a draft cannot decide on it."
STALE_DRAFT_VERSION = "The draft version you decided on is not this draft's version."
NOTHING_TO_REVOKE_DRAFT = "Only an approved draft can be revoked."
REVIEW_BLOCKS_APPROVAL = "approval_review_blocked"
#: Why an approval is refused, per ``draft_approval_refusal`` reason (no draft text, no
#: claim text: a static sentence per reason).
REVIEW_REFUSALS = {
    "review_missing": "This draft has no content review yet, so it cannot be approved.",
    "review_blocked": (
        "This draft's content review blocked it, so it cannot be approved. Generate a "
        "revised version."
    ),
    "review_not_passed": (
        "A required disclaimer is missing from this draft, so it cannot be approved."
    ),
    "blocked_claim": (
        "This draft contains a claim the claims library now blocks, so it cannot be "
        "approved."
    ),
    "disclaimer_missing": (
        "This draft lacks a disclaimer the claims library now requires, so it cannot be "
        "approved. Generate a revised version."
    ),
}


def is_draft_author(draft: CreativeDraft, user_id: str) -> bool:
    """Whether ``user_id`` generated ``draft`` (the live FK, or the snapshot's leading id
    once the FK is nulled) — exact id, as :func:`is_brief_author`."""
    if draft.created_by is not None and draft.created_by == user_id:
        return True
    leading = (draft.created_by_snapshot or "").split(" ", 1)[0]
    return bool(leading) and leading == user_id


def draft_lock_select(draft_id: str) -> Select:
    """``SELECT id, draft_version … FOR UPDATE`` on the draft row: the per-subject
    serializer for a draft's ``decision_seq`` (the brief's ``subject_lock_select``)."""
    return (
        select(CreativeDraft.id, CreativeDraft.draft_version)
        .where(CreativeDraft.id == draft_id)
        .with_for_update()
    )


def _lock_draft(db: Session, draft_id: str) -> int:
    row = db.execute(draft_lock_select(draft_id)).first()
    if row is None:
        raise NotFoundError(DRAFT_NOT_FOUND)
    return int(row.draft_version)


def record_draft_decision(
    db: Session,
    *,
    ctx: TenantContext,
    draft: CreativeDraft,
    decision: ApprovalDecision,
    subject_version: int,
    reason_code: ApprovalReason | None = None,
    note: str | None = None,
) -> Approval:
    """Append one decision on ``draft``'s exact version, with its audit row; flush only.

    The brief decision's checks in the same order (approver role 403, tenant scope 404,
    author 403, reason polarity 422; the approver role re-read ``FOR SHARE`` under locks 1 to
    3 of "Lock order" 403 (D-N4 item 34); under the draft row's lock: existence 404, exact
    version 409, the revoke rule 409),
    then the 5C precondition: an ``approved`` decision is refused (409
    ``approval_review_blocked``, nothing written) while
    :func:`app.creative.service.draft_approval_refusal` gives a reason.
    """
    decision = ApprovalDecision(decision)
    role = Role(ctx.role)
    if role not in APPROVER_ROLES:
        raise PermissionDeniedError(f"Role '{role.value}' is not permitted for this action.")
    if draft.workspace_id != ctx.workspace.id or draft.organization_id != ctx.organization.id:
        raise NotFoundError(DRAFT_NOT_FOUND)
    if is_draft_author(draft, ctx.user.id):
        raise PermissionDeniedError(AUTHOR_CANNOT_DECIDE_DRAFT, code="approval_author_conflict")
    reason = None if reason_code is None else ApprovalReason(reason_code)
    if not reason_allowed(decision, reason):
        raise ValidationDomainError(REASON_MISMATCH)

    role = _reread_approver_role(db, ctx)  # D-N4 item 34: locks 1 to 3, as for a brief
    version = _lock_draft(db, draft.id)
    if subject_version != version:
        raise ConflictError(STALE_DRAFT_VERSION, code="approval_stale_version")
    latest = controlling_decision(
        db, subject_type=DRAFT_SUBJECT, subject_id=draft.id, subject_version=draft.draft_version
    )
    if decision is ApprovalDecision.REVOKED and (
        latest is None or latest.decision != ApprovalDecision.APPROVED.value
    ):
        raise ConflictError(NOTHING_TO_REVOKE_DRAFT, code="approval_nothing_to_revoke")
    if decision is ApprovalDecision.APPROVED:
        refusal = draft_approval_refusal(db, draft)
        if refusal is not None:
            log_event(
                logger,
                "approval_refused_by_review",
                outcome="refused",
                workspace_id=ctx.workspace.id,
                reason=refusal,
            )
            raise ConflictError(REVIEW_REFUSALS[refusal], code=REVIEW_BLOCKS_APPROVAL)

    approval = _insert_decision(
        db,
        values={
            "organization_id": ctx.organization.id,
            "workspace_id": ctx.workspace.id,
            "subject_type": DRAFT_SUBJECT,
            "subject_id": draft.id,
            "subject_version": draft.draft_version,
            "draft_id": draft.id,
            "decision": decision.value,
            "reason_code": None if reason is None else reason.value,
            "note": note,
            "decided_by": ctx.user.id,
            "decided_by_snapshot": actor_snapshot(
                user_id=ctx.user.id, full_name=ctx.user.full_name, email=ctx.user.email
            ),
            "decided_role": role.value,
            "is_simulated": bool(draft.is_simulated),
        },
    )
    record_audit(
        db,
        organization_id=ctx.organization.id,
        workspace_id=ctx.workspace.id,
        actor_user_id=ctx.user.id,
        action=APPROVAL_DECISION_ACTIONS[decision],
        entity_type=DRAFT_AUDIT_ENTITY_TYPE,
        entity_id=draft.id,
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


def list_draft_decisions(
    db: Session, *, draft: CreativeDraft, limit: int = DEFAULT_LIMIT, offset: int = 0
) -> ApprovalPage:
    """The draft version's decisions, newest ``decision_seq`` first. Read-only."""
    limit, offset = _clamp(limit, offset)
    base = select(Approval).where(
        Approval.workspace_id == draft.workspace_id,
        _subject_filter(DRAFT_SUBJECT, draft.id, draft.draft_version),
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


__all__ = [
    "AUTHOR_CANNOT_DECIDE_DRAFT",
    "DRAFT_AUDIT_ENTITY_TYPE",
    "DRAFT_NOT_FOUND",
    "DRAFT_SUBJECT",
    "NOTHING_TO_REVOKE_DRAFT",
    "REVIEW_BLOCKS_APPROVAL",
    "REVIEW_REFUSALS",
    "STALE_DRAFT_VERSION",
    "AUDIT_ENTITY_TYPE",
    "AUTHOR_CANNOT_DECIDE",
    "BRIEF_NOT_FOUND",
    "BRIEF_SUBJECT",
    "CONCURRENT_DECISION",
    "NOTHING_TO_REVOKE",
    "STALE_VERSION",
    "NOT_A_MEMBER",
    "ApprovalPage",
    "BriefQueuePage",
    "approver_membership_select",
    "approver_organization_lock_select",
    "approver_user_lock_select",
    "controlling_decision",
    "draft_lock_select",
    "is_draft_author",
    "list_draft_decisions",
    "record_draft_decision",
    "get_scoped_brief",
    "is_approved",
    "is_brief_author",
    "list_awaiting_decision",
    "list_decisions",
    "record_decision",
    "subject_lock_select",
]
