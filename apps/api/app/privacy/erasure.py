"""Erasure: account deletion, organization deletion, invitee-email erasure (``P6-PRIV-1``).

Every function here flushes and never commits: the route (or CLI) owns the transaction,
so a deletion is all-or-nothing.

**The foreign-key walk.** :func:`delete_rows` removes rows together with everything that
depends on them by following the foreign keys declared on ``Base.metadata``, children
first: a ``CASCADE`` key is followed (its rows are deleted the same way, recursively), a
``SET NULL`` key is cleared with an explicit ``UPDATE``. Nothing relies on the
database's own ``ON DELETE`` actions firing (SQLite enforces them only with
``PRAGMA foreign_keys=ON``), and any other delete rule raises instead of being skipped.
Rows are processed in chunks of :data:`CHUNK` ids, so no statement carries an unbounded
``IN`` list.

**Audit records.** ``audit_logs`` has no foreign key to its organization (by design: the
Phase-5 resolution ``O-6`` keeps compliance records when their scope is deleted, subject
to the future ``O-5`` retention policy). They are therefore *anonymised*, not deleted, by
both deletions: account deletion clears every value that names the account; organization
deletion clears every identifying column of the organization's records. The time-based
purge of the ``audit_logs`` retention class removes them. The Project Phase 5 compliance
records follow the same rule (W21 CF-1): ``approvals`` (FK-less scope, like ``audit_logs``)
survive organization deletion anonymised, and the actor snapshots on ``approvals`` and
``recommendation_briefs`` are cleared when the account they name is deleted, while the rows
are kept. A value names a subject only where
the shared matcher (:mod:`app.privacy.matching`, also the verifier's) finds its id or address
``WHOLE``; an occurrence that some registrable address could contain is left, because it may be
another person's.

**Data found only by an email address** (invitations addressed to it, audit values naming
it) is the account's own only once the account has proven the address
(``users.email_verified_at``; :class:`EmailScope`). Registration does not prove it, account
identity is exact while those matches ignore letter case, so an unproven address may be
another person's: for an unverified account those limbs do nothing. **Invitations the account
accepted** are bound to it by id (``accepted_by_user_id``; acceptance required the exact
address), so they are its own whether or not the address is verified: they are deleted with
it, and the address is cleared from the audit records about exactly those invitations.

Locks: account deletion locks the user row, then each of its organizations in id order
(``SELECT … FOR UPDATE``; no-ops on SQLite, whose single writer serializes instead);
organization deletion locks the organization and re-reads the actor's role under the
lock, as every membership mutation does (:mod:`app.organizations.members`).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from functools import lru_cache
from typing import Any, NamedTuple

from sqlalchemy import Column, Table, delete, func, null, or_, select, update
from sqlalchemy.orm import Session

from app.approvals.models import Approval
from app.audit.models import AuditLog
from app.auth.account_tokens import _lock_user
from app.briefs.models import RecommendationBrief
from app.core.enums import Role
from app.core.errors import AuthError, ConflictError, PermissionDeniedError, ValidationDomainError
from app.core.security import verify_password
from app.creative.models import CreativeDraft
from app.db.models import Base
from app.jobs.models import Job
from app.jobs.status import ACTIVE_STATUSES
from app.organizations.members import load_membership, lock_organization
from app.organizations.models import (
    Organization,
    OrganizationInvitation,
    OrganizationMember,
    User,
    Workspace,
)
from app.privacy.matching import (
    Mention,
    Subject,
    existing_addresses,
    mention,
    prefilter,
    same_address,
)

#: Ids per statement. Well under SQLite's bound-parameter limit.
CHUNK = 500
#: What an erased value in an anonymised audit record becomes.
ERASED = "[erased]"


class EmailScope(StrEnum):
    """Whether data found only by the account's email address is treated as the account's."""

    #: ``email_verified_at`` is set: the address is the account's own, so invitations
    #: addressed to it and audit values naming it are exported and erased with it.
    INCLUDED = "included"
    #: The address was never verified: nothing proves it is the account's (it may be a
    #: letter-case variant of another account's address, or an address invited somewhere),
    #: so every limb keyed on it is skipped. Only data keyed on the account's id is
    #: exported or erased, which includes the invitations it accepted.
    EXCLUDED_UNVERIFIED = "excluded_unverified"


def email_scope(user: User) -> EmailScope:
    """The :class:`EmailScope` of ``user`` as loaded (read it under the lock to act on it)."""
    if user.email_verified_at is None:
        return EmailScope.EXCLUDED_UNVERIFIED
    return EmailScope.INCLUDED


# --------------------------------------------------------------------------- #
# The foreign-key walk
# --------------------------------------------------------------------------- #
@dataclass
class ErasureCounts:
    """Rows deleted per table, values cleared per ``table.column``, records anonymised."""

    deleted: Counter = field(default_factory=Counter)
    nulled: Counter = field(default_factory=Counter)
    anonymised: Counter = field(default_factory=Counter)

    def merge(self, other: ErasureCounts) -> None:
        self.deleted.update(other.deleted)
        self.nulled.update(other.nulled)
        self.anonymised.update(other.anonymised)

    def as_dict(self) -> dict[str, dict[str, int]]:
        return {
            "deleted": dict(sorted((k, v) for k, v in self.deleted.items() if v)),
            "nulled": dict(sorted((k, v) for k, v in self.nulled.items() if v)),
            "anonymised": dict(sorted((k, v) for k, v in self.anonymised.items() if v)),
        }


class UnsupportedDeleteRule(RuntimeError):
    """A foreign key whose delete rule the walk cannot honour (neither CASCADE nor SET NULL)."""


@lru_cache(maxsize=1)
def _references() -> dict[str, tuple[tuple[Column, str], ...]]:
    """For each table, the (child column, delete rule) pairs of the keys that point at it."""
    out: dict[str, list[tuple[Column, str]]] = {}
    for table in Base.metadata.sorted_tables:
        for fk in table.foreign_keys:
            rule = (fk.ondelete or "NO ACTION").upper()
            out.setdefault(fk.column.table.name, []).append((fk.parent, rule))
    return {name: tuple(refs) for name, refs in out.items()}


def _chunks(ids: Sequence[str]) -> Iterable[list[str]]:
    for start in range(0, len(ids), CHUNK):
        yield list(ids[start : start + CHUNK])


def delete_rows(db: Session, table: Table, ids: Sequence[str]) -> ErasureCounts:
    """Delete ``table`` rows by id with every dependent row, children first.

    Returns what was removed or cleared. Flushes nothing through the ORM: every statement
    is a Core ``DELETE``/``UPDATE`` on the session's connection, so the caller's
    transaction covers all of it.
    """
    counts = ErasureCounts()
    for chunk in _chunks(list(dict.fromkeys(ids))):
        for child_column, rule in _references().get(table.name, ()):
            child = child_column.table
            if rule == "CASCADE":
                child_ids = db.execute(
                    select(child.c.id).where(child_column.in_(chunk))
                ).scalars().all()
                if child_ids:
                    counts.merge(delete_rows(db, child, child_ids))
            elif rule == "SET NULL":
                result = db.execute(
                    update(child)
                    .where(child_column.in_(chunk))
                    .values({child_column.name: None})
                    .execution_options(synchronize_session=False)
                )
                counts.nulled[f"{child.name}.{child_column.name}"] += result.rowcount or 0
            else:
                raise UnsupportedDeleteRule(f"{child.name}.{child_column.name} ON DELETE {rule}")
        result = db.execute(
            delete(table).where(table.c.id.in_(chunk)).execution_options(synchronize_session=False)
        )
        counts.deleted[table.name] += result.rowcount or 0
    return counts


# --------------------------------------------------------------------------- #
# Audit anonymisation
# --------------------------------------------------------------------------- #
def _names(text: str, subject: Subject) -> bool:
    return mention(text, subject) is Mention.WHOLE


def _scrub(value: Any, subject: Subject) -> Any:
    """``value`` with every string naming the subject replaced by :data:`ERASED`.

    Object keys are left as they are; the verifier reports one that names the subject.
    """
    if isinstance(value, str):
        return ERASED if _names(value, subject) else value
    if isinstance(value, dict):
        return {k: _scrub(v, subject) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v, subject) for v in value]
    return value


#: The audit ``entity_type`` of records about an invitation.
_INVITATION_ENTITY = "organization_invitation"


class AcceptedInvitation(NamedTuple):
    """An invitation an account accepted: where it lives, and the address it was sent to."""

    organization_id: str
    email: str


def anonymise_audit_for_subject(
    db: Session,
    *,
    user_id: str | None,
    email: str | None,
    invitations: Mapping[str, AcceptedInvitation] | None = None,
) -> int:
    """Clear every audit value that names an account (by id) or a person (by email).

    Candidate rows are found with a case-insensitive text match over the identifying
    columns; each is then scrubbed exactly in Python (an id column equal to the account's
    id becomes NULL, a free-text ``reason`` that names the subject becomes NULL, and any
    JSON string that names the id or the email becomes ``"[erased]"``), where *names*
    means a ``WHOLE`` mention to the shared matcher (:mod:`app.privacy.matching`), so a
    record about a longer address that merely contains this one is untouched.
    ``email=None`` erases by id only (an account whose address is not verified).
    ``invitations`` maps the ids of the invitations the account accepted to where they live and
    their address: in the records about exactly those invitations, in the invitation's own
    organization, that address is cleared too, whatever ``email`` is. Returns the number of
    records changed, each counted once.
    """
    invitations = dict(invitations or {})
    base = Subject(
        ids=(user_id,) if user_id else (),
        addresses=(email,) if email else (),
        existing=existing_addresses(db),
    )
    if not base and not invitations:
        return 0
    columns = (AuditLog.reason, AuditLog.previous_state, AuditLog.new_state, AuditLog.context)
    clauses = [prefilter(c, base) for c in columns] if base else []
    if user_id:
        clauses += [AuditLog.actor_user_id == user_id, AuditLog.entity_id == user_id]
    clauses += [
        (AuditLog.entity_type == _INVITATION_ENTITY)
        & (AuditLog.entity_id == invitation_id)
        & (AuditLog.organization_id == invitation.organization_id)
        for invitation_id, invitation in sorted(invitations.items())
    ]
    rows = db.execute(
        select(
            AuditLog.id,
            AuditLog.organization_id,
            AuditLog.actor_user_id,
            AuditLog.entity_type,
            AuditLog.entity_id,
            AuditLog.reason,
            AuditLog.previous_state,
            AuditLog.new_state,
            AuditLog.context,
        ).where(or_(*clauses))
    ).all()
    changed = 0
    for row in rows:
        subject = base
        invitation = invitations.get(row.entity_id)
        if (
            invitation is not None
            and row.entity_type == _INVITATION_ENTITY
            and invitation.organization_id == row.organization_id
        ):
            subject = Subject(
                ids=base.ids,
                addresses=(*base.addresses, invitation.email),
                existing=base.existing,
            )
        values: dict[str, Any] = {}
        if user_id and row.actor_user_id == user_id:
            values["actor_user_id"] = None
        if user_id and row.entity_id == user_id:
            values["entity_id"] = None
        if row.reason and _names(row.reason, subject):
            values["reason"] = None
        for name in ("previous_state", "new_state", "context"):
            current = getattr(row, name)
            scrubbed = _scrub(current, subject)
            if scrubbed != current:
                values[name] = scrubbed
        if values:
            db.execute(
                update(AuditLog.__table__)
                .where(AuditLog.__table__.c.id == row.id)
                .values(values)
                .execution_options(synchronize_session=False)
            )
            changed += 1
    return changed


def anonymise_organization_audit(db: Session, *, organization_id: str) -> int:
    """Clear every identifying column of the organization's audit records; keep the rows.

    Kept: ``organization_id``, ``workspace_id`` (both opaque ids of deleted scopes),
    ``action``, ``entity_type`` and the timestamps. Cleared: the actor, the entity id, the
    free-text reason and both state snapshots (SQL NULL), and the context (``{}``).
    """
    table = AuditLog.__table__
    result = db.execute(
        update(table)
        .where(table.c.organization_id == organization_id)
        .values(
            actor_user_id=None,
            entity_id=None,
            reason=None,
            previous_state=null(),
            new_state=null(),
            context={},
        )
        .execution_options(synchronize_session=False)
    )
    return result.rowcount or 0


def anonymise_phase5_records_for_subject(
    db: Session, *, user_id: str | None, email: str | None
) -> Counter:
    """Clear every value of a Phase 5 compliance record that names the account; keep the rows.

    Operator decision ``O-6`` as this module already applies it to ``audit_logs``: the record
    survives, the values naming the subject go. ``recommendation_briefs.created_by_snapshot``,
    ``creative_drafts.created_by_snapshot`` (CF-2) and ``approvals.decided_by_snapshot``
    become :data:`ERASED` when the row was made by
    ``user_id`` or the snapshot names the subject; an ``approvals.note`` naming the subject
    becomes NULL. The ``created_by`` / ``decided_by`` foreign keys are cleared afterwards by
    the foreign-key walk (``SET NULL``). Run it before the user row goes. Returns the rows
    changed per table.
    """
    changed: Counter = Counter()
    subject = Subject(
        ids=(user_id,) if user_id else (),
        addresses=(email,) if email else (),
        existing=existing_addresses(db),
    )
    if not subject:
        return changed
    for authored in (RecommendationBrief.__table__, CreativeDraft.__table__):
        clauses = [prefilter(authored.c.created_by_snapshot, subject)]
        if user_id:
            clauses.append(authored.c.created_by == user_id)
        for row in db.execute(
            select(authored.c.id, authored.c.created_by, authored.c.created_by_snapshot).where(
                or_(*clauses)
            )
        ).all():
            mine = bool(user_id) and row.created_by == user_id
            if row.created_by_snapshot != ERASED and (
                mine or _names(row.created_by_snapshot, subject)
            ):
                db.execute(
                    update(authored)
                    .where(authored.c.id == row.id)
                    .values(created_by_snapshot=ERASED)
                    .execution_options(synchronize_session=False)
                )
                changed[authored.name] += 1
    approvals = Approval.__table__
    clauses = [prefilter(c, subject) for c in (approvals.c.decided_by_snapshot, approvals.c.note)]
    if user_id:
        clauses.append(approvals.c.decided_by == user_id)
    for row in db.execute(
        select(
            approvals.c.id,
            approvals.c.decided_by,
            approvals.c.decided_by_snapshot,
            approvals.c.note,
        ).where(or_(*clauses))
    ).all():
        values: dict[str, Any] = {}
        mine = bool(user_id) and row.decided_by == user_id
        if row.decided_by_snapshot != ERASED and (
            mine or _names(row.decided_by_snapshot, subject)
        ):
            values["decided_by_snapshot"] = ERASED
        if row.note and _names(row.note, subject):
            values["note"] = None
        if values:
            db.execute(
                update(approvals)
                .where(approvals.c.id == row.id)
                .values(values)
                .execution_options(synchronize_session=False)
            )
            changed["approvals"] += 1
    return changed


def anonymise_organization_approvals(db: Session, *, organization_id: str) -> int:
    """Clear every identifying column of the organization's approvals; keep the rows (O-6).

    Kept: ``organization_id``, ``workspace_id`` (opaque ids of deleted scopes), the subject
    (type, id, version), the decision, its sequence, reason code and decided role, the
    simulation flag and ``created_at``. Cleared: ``decided_by`` (NULL), ``decided_by_snapshot``
    (:data:`ERASED`) and the free-text ``note`` (NULL). ``brief_id`` and ``draft_id`` are
    cleared by the foreign-key walk when the organization's briefs and drafts are deleted.
    """
    table = Approval.__table__
    result = db.execute(
        update(table)
        .where(table.c.organization_id == organization_id)
        .values(decided_by=None, decided_by_snapshot=ERASED, note=None)
        .execution_options(synchronize_session=False)
    )
    return result.rowcount or 0


# --------------------------------------------------------------------------- #
# Organization deletion
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class OrganizationErasure:
    organization_id: str
    workspace_ids: tuple[str, ...]
    counts: ErasureCounts


def _require_no_active_jobs(db: Session, organization_id: str) -> None:
    active = db.scalar(
        select(func.count())
        .select_from(Job)
        .where(
            Job.organization_id == organization_id,
            Job.status.in_([s.value for s in ACTIVE_STATUSES]),
        )
    )
    if active:
        raise ConflictError(
            "A scouting job of this organization is running. Try again when it has finished.",
            code="organization_jobs_active",
        )


def erase_organization(db: Session, *, organization_id: str) -> OrganizationErasure:
    """Delete one organization and everything that belongs to it; anonymise its audit.

    The caller has locked the organization and checked authority. Refuses (409) while a
    job of the organization is claimed or running, so no worker writes into a scope that
    is being deleted.
    """
    _require_no_active_jobs(db, organization_id)
    workspace_ids = tuple(
        db.execute(
            select(Workspace.id)
            .where(Workspace.organization_id == organization_id)
            .order_by(Workspace.id)
        ).scalars()
    )
    counts = ErasureCounts()
    counts.anonymised["audit_logs"] += anonymise_organization_audit(
        db, organization_id=organization_id
    )
    counts.anonymised["approvals"] += anonymise_organization_approvals(
        db, organization_id=organization_id
    )
    counts.merge(delete_rows(db, Organization.__table__, [organization_id]))
    db.flush()
    return OrganizationErasure(organization_id, workspace_ids, counts)


def _verify_current_password(user: User, current_password: str) -> tuple[int, str]:
    """Re-authenticate ``user``; returns the credential state the check was made against."""
    epoch, current_hash = user.auth_epoch, user.hashed_password
    if not verify_password(current_password, current_hash):
        raise ValidationDomainError(
            "The current password is incorrect.", code="current_password_incorrect"
        )
    return epoch, current_hash


def _lock_verified_user(db: Session, user_id: str, *, epoch: int, current_hash: str) -> User:
    """Lock the user row and require the credential state the password was checked against.

    A credential change committed after the request authenticated has already ended the
    caller's token, so it is the dependency's 401 and nothing is written.
    """
    locked = _lock_user(db, User.id == user_id)
    if (
        locked is None
        or not locked.is_active
        or locked.auth_epoch != epoch
        or locked.hashed_password != current_hash
    ):
        raise AuthError("Invalid or expired token.")
    return locked


def delete_organization(
    db: Session,
    *,
    user: User,
    organization_id: str,
    current_password: str,
    confirm_organization_name: str,
) -> OrganizationErasure:
    """Delete an organization on its OWNER's request (``POST /organizations/{id}/delete``).

    Order: the password is verified (bcrypt, before any lock); the user row, then the
    organization row, are locked; the actor's role is re-read under the lock and must be
    exactly OWNER; the typed name must equal the organization's name exactly; then the
    organization is erased. Flushes, never commits.
    """
    epoch, current_hash = _verify_current_password(user, current_password)
    _lock_verified_user(db, user.id, epoch=epoch, current_hash=current_hash)
    lock_organization(db, organization_id)
    actor = load_membership(db, organization_id=organization_id, user_id=user.id)
    if actor is None:
        raise PermissionDeniedError("You are not a member of this organization.")
    if Role(actor.role) is not Role.OWNER:
        raise PermissionDeniedError(
            f"Role '{Role(actor.role).value}' is not permitted for this action."
        )
    organization = db.execute(
        select(Organization).where(Organization.id == organization_id)
    ).scalar_one()
    if confirm_organization_name != organization.name:
        raise ValidationDomainError(
            "Type the organization's name exactly to confirm.",
            code="organization_name_mismatch",
        )
    return erase_organization(db, organization_id=organization_id)


# --------------------------------------------------------------------------- #
# Account deletion
# --------------------------------------------------------------------------- #
class MembershipOutcome(StrEnum):
    #: The account's membership row is deleted; the organization stays.
    MEMBERSHIP_REMOVED = "membership_removed"
    #: The account is the organization's only member, so the organization is deleted
    #: with it (left alone it would have no member who could ever delete it).
    ORGANIZATION_DELETED = "organization_deleted"
    #: The account is the only OWNER and other members remain: deletion is refused until
    #: ownership is transferred or the organization is deleted.
    BLOCKED_SOLE_OWNER = "blocked_sole_owner"


@dataclass(frozen=True)
class MembershipImpact:
    organization_id: str
    organization_name: str
    role: str
    member_count: int
    outcome: MembershipOutcome


def plan_account_deletion(db: Session, *, user_id: str) -> list[MembershipImpact]:
    """What deleting the account would do to each of its organizations. Read-only."""
    memberships = db.execute(
        select(OrganizationMember.organization_id, OrganizationMember.role, Organization.name)
        .join(Organization, Organization.id == OrganizationMember.organization_id)
        .where(OrganizationMember.user_id == user_id)
        .order_by(OrganizationMember.organization_id)
        .execution_options(populate_existing=True)
    ).all()
    impacts: list[MembershipImpact] = []
    for organization_id, role, name in memberships:
        roles = db.execute(
            select(OrganizationMember.role).where(
                OrganizationMember.organization_id == organization_id
            )
        ).scalars().all()
        owners = sum(1 for r in roles if r == Role.OWNER.value)
        if len(roles) == 1:
            outcome = MembershipOutcome.ORGANIZATION_DELETED
        elif role == Role.OWNER.value and owners == 1:
            outcome = MembershipOutcome.BLOCKED_SOLE_OWNER
        else:
            outcome = MembershipOutcome.MEMBERSHIP_REMOVED
        impacts.append(MembershipImpact(organization_id, name, role, len(roles), outcome))
    return impacts


@dataclass(frozen=True)
class AccountErasure:
    user_id: str
    organizations: tuple[MembershipImpact, ...]
    deleted_organizations: tuple[OrganizationErasure, ...]
    counts: ErasureCounts
    #: Whether the limbs keyed on the account's email address ran (read under the lock).
    email_scope: EmailScope


def _invitations_addressed_to(db: Session, email: str) -> list[str]:
    """Invitations sent to ``email`` in any form (letter case, NFC/NFD, U-/A-label domain),
    decided in Python: SQLite's ``lower()`` does not fold non-ASCII letters."""
    rows = db.execute(
        select(OrganizationInvitation.id, OrganizationInvitation.email).where(
            prefilter(OrganizationInvitation.email, Subject(addresses=(email,)))
        )
    ).all()
    existing = existing_addresses(db)
    return [row.id for row in rows if same_address(row.email, email, existing)]


def erase_invitations_addressed_to(db: Session, *, email: str) -> ErasureCounts:
    """Delete every invitation addressed to ``email`` (in any form), in any state."""
    ids = _invitations_addressed_to(db, email)
    return delete_rows(db, OrganizationInvitation.__table__, ids) if ids else ErasureCounts()


def invitations_accepted_by(db: Session, *, user_id: str) -> dict[str, AcceptedInvitation]:
    """The invitations ``user_id`` accepted, by id. Read it before the user row goes:
    ``accepted_by_user_id`` is ``ON DELETE SET NULL``, so the deletion destroys the link."""
    rows = db.execute(
        select(
            OrganizationInvitation.id,
            OrganizationInvitation.organization_id,
            OrganizationInvitation.email,
        ).where(OrganizationInvitation.accepted_by_user_id == user_id)
    ).all()
    return {row.id: AcceptedInvitation(row.organization_id, row.email) for row in rows}


def delete_account(
    db: Session, *, user: User, current_password: str
) -> AccountErasure:
    """Delete the signed-in account on its own request (``POST /account/delete``).

    Order: the password is verified (bcrypt, before any lock); the user row is locked and
    its credential state re-checked; every organization the account belongs to is locked
    in id order and the plan is re-made under those locks. Then, all in the caller's
    transaction: refuse if the account is the sole OWNER of an organization with other
    members; erase every organization of which it is the only member; delete the
    invitations it accepted and clear their address from the audit records about them;
    anonymise audit records that name it; delete the user row and everything that depends on
    it (memberships, sessions, account tokens), clearing the attribution columns that point
    at it. Only when its email address is verified (:class:`EmailScope`, read from the row
    under the lock) are the other invitations addressed to that address deleted and the
    other audit values naming it cleared too. Flushes, never commits.
    """
    epoch, current_hash = _verify_current_password(user, current_password)
    user_id = user.id
    locked = _lock_verified_user(db, user_id, epoch=epoch, current_hash=current_hash)
    scope = email_scope(locked)
    email = locked.email if scope is EmailScope.INCLUDED else None
    organization_ids = db.execute(
        select(OrganizationMember.organization_id)
        .where(OrganizationMember.user_id == user_id)
        .order_by(OrganizationMember.organization_id)
    ).scalars().all()
    for organization_id in organization_ids:
        lock_organization(db, organization_id)

    impacts = plan_account_deletion(db, user_id=user_id)
    if any(i.outcome is MembershipOutcome.BLOCKED_SOLE_OWNER for i in impacts):
        raise ConflictError(
            "You are the only owner of an organization that has other members. Make another "
            "member an owner, or delete the organization, before deleting your account.",
            code="account_deletion_sole_owner",
        )

    counts = ErasureCounts()
    deleted: list[OrganizationErasure] = []
    for impact in impacts:
        if impact.outcome is MembershipOutcome.ORGANIZATION_DELETED:
            erased = erase_organization(db, organization_id=impact.organization_id)
            counts.merge(erased.counts)
            deleted.append(erased)
    accepted = invitations_accepted_by(db, user_id=user_id)
    invitation_ids = {*accepted, *(_invitations_addressed_to(db, email) if email else ())}
    if invitation_ids:
        counts.merge(delete_rows(db, OrganizationInvitation.__table__, sorted(invitation_ids)))
    counts.anonymised["audit_logs"] += anonymise_audit_for_subject(
        db, user_id=user_id, email=email, invitations=accepted
    )
    counts.anonymised.update(anonymise_phase5_records_for_subject(db, user_id=user_id, email=email))
    counts.merge(delete_rows(db, User.__table__, [user_id]))
    db.flush()
    db.expunge_all()
    return AccountErasure(user_id, tuple(impacts), tuple(deleted), counts, scope)


# --------------------------------------------------------------------------- #
# Invitee-email erasure (operator; a person with no account)
# --------------------------------------------------------------------------- #
def erase_invitee_email(db: Session, *, email: str) -> ErasureCounts:
    """Erase a person by address, on out-of-band proof: their invitations and mentions.

    Refused when an account with a **verified** address holds it (in any form): that is the
    account holder's own deletion, which also covers this. An unverified account holding the
    address does not block it, because nothing proves the address is that account's (a
    registrant can claim any address); its accepted invitations go too, while it stays a
    member. Flushes, never commits.
    """
    holders = db.execute(
        select(User.email).where(
            prefilter(User.email, Subject(addresses=(email,))),
            User.email_verified_at.is_not(None),
        )
    ).scalars().all()
    existing = existing_addresses(db)
    if any(same_address(holder, email, existing) for holder in holders):
        raise ConflictError(
            "An account with this verified email address exists; delete the account instead.",
            code="erasure_account_exists",
        )
    counts = erase_invitations_addressed_to(db, email=email)
    counts.anonymised["audit_logs"] += anonymise_audit_for_subject(db, user_id=None, email=email)
    counts.anonymised.update(anonymise_phase5_records_for_subject(db, user_id=None, email=email))
    db.flush()
    return counts


__all__ = [
    "CHUNK",
    "ERASED",
    "AcceptedInvitation",
    "AccountErasure",
    "EmailScope",
    "ErasureCounts",
    "MembershipImpact",
    "MembershipOutcome",
    "OrganizationErasure",
    "UnsupportedDeleteRule",
    "anonymise_audit_for_subject",
    "anonymise_organization_approvals",
    "anonymise_organization_audit",
    "anonymise_phase5_records_for_subject",
    "delete_account",
    "delete_organization",
    "delete_rows",
    "email_scope",
    "erase_invitations_addressed_to",
    "erase_invitee_email",
    "erase_organization",
    "invitations_accepted_by",
    "plan_account_deletion",
]
