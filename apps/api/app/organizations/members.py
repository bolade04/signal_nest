"""Organization membership management: list members, change a role, remove a member.

Conventions mirror the house service style (``capabilities/service.py``): explicit
``db: Session`` first positional, keyword-only domain args, caller-owns-transaction
(the service flushes but never commits — routes commit before returning success),
and ``SignalNestError`` subclasses rather than ``HTTPException``.

Every mutation first takes a ``SELECT … FOR UPDATE`` lock on the organization row
and only then re-reads the actor's membership, so the authority a decision rests on
is the one current under the lock rather than the one resolved when the request
began. The lock also serializes concurrent mutations in one organization, which is
what makes the last-owner count trustworthy on PostgreSQL. On SQLite ``FOR UPDATE``
compiles to a no-op (the single-writer engine serializes writers), so the owner
count is re-checked after the write as a portable backstop.

Rules, applied in this order (Phase-5B design authority):

* the actor must still be a member, with role OWNER or ADMIN;
* nobody changes their own role or removes themselves;
* the target must be a member of this organization;
* only an OWNER may change or remove an OWNER, or grant OWNER;
* nobody assigns a role ranked above their own, or acts on a member ranked above
  them (the privilege ceiling);
* the last OWNER can be neither demoted nor removed.

Removal deletes the membership row only; the user account is never touched.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit.service import record_audit
from app.auth.dependencies import _ROLE_RANK, OrganizationContext
from app.core.enums import Role
from app.core.errors import ConflictError, NotFoundError, PermissionDeniedError
from app.organizations.models import Organization, OrganizationMember, User

#: The roles that administer an organization's membership and invitations.
ORGANIZATION_ADMIN_ROLES: frozenset[Role] = frozenset({Role.OWNER, Role.ADMIN})


@dataclass(frozen=True, slots=True)
class MemberRow:
    """One organization member: identity plus the membership's role and join time."""

    user_id: str
    email: str
    full_name: str
    role: Role
    created_at: datetime


def role_rank(role: Role | str) -> int:
    """Privilege rank of ``role`` from the single role hierarchy (higher = more)."""
    return _ROLE_RANK[Role(role)]


def as_utc(value: datetime) -> datetime:
    """Return ``value`` as an aware UTC datetime.

    SQLite hands back naive datetimes; every value this package writes is UTC, so a
    naive value is UTC by construction.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _organization_lock_select(organization_id: str):
    """The ``SELECT … FOR UPDATE`` that serializes mutations in one organization.

    Factored out so a test can compile it against the PostgreSQL dialect and prove
    it emits ``FOR UPDATE`` without a live database.
    """
    return select(Organization.id).where(Organization.id == organization_id).with_for_update()


def lock_organization(db: Session, organization_id: str) -> None:
    """Lock the organization row for the rest of the caller's transaction.

    Raises :class:`NotFoundError` when the organization no longer exists.
    """
    if db.execute(_organization_lock_select(organization_id)).scalar_one_or_none() is None:
        raise NotFoundError("Organization not found.")


def load_membership(
    db: Session, *, organization_id: str, user_id: str
) -> OrganizationMember | None:
    """Read one membership fresh from the database, bypassing the identity map.

    ``populate_existing`` overwrites any instance already loaded in the session (for
    example by the request's authorization dependency), so the role read here is the
    one committed now, not the one seen when the request began.
    """
    return db.execute(
        select(OrganizationMember)
        .where(
            OrganizationMember.organization_id == organization_id,
            OrganizationMember.user_id == user_id,
        )
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()


def _owner_count(db: Session, organization_id: str) -> int:
    return (
        db.scalar(
            select(func.count())
            .select_from(OrganizationMember)
            .where(
                OrganizationMember.organization_id == organization_id,
                OrganizationMember.role == Role.OWNER.value,
            )
        )
        or 0
    )


def _lock_and_authorize_actor(db: Session, ctx: OrganizationContext) -> Role:
    """Lock the organization, then re-read the actor's authority under the lock."""
    organization_id = ctx.organization.id
    lock_organization(db, organization_id)
    actor = load_membership(db, organization_id=organization_id, user_id=ctx.user.id)
    if actor is None:
        raise PermissionDeniedError("You are not a member of this organization.")
    actor_role = Role(actor.role)
    if actor_role not in ORGANIZATION_ADMIN_ROLES:
        raise PermissionDeniedError(f"Role '{actor_role.value}' is not permitted for this action.")
    return actor_role


def _load_target(db: Session, *, organization_id: str, target_user_id: str) -> OrganizationMember:
    target = load_membership(db, organization_id=organization_id, user_id=target_user_id)
    if target is None:
        raise NotFoundError("Member not found.", code="member_not_found")
    return target


def _require_owner_quorum(db: Session, organization_id: str) -> None:
    """Refuse to leave the organization without an OWNER (counted under the lock)."""
    if _owner_count(db, organization_id) < 2:
        raise ConflictError(
            "The organization's last owner cannot be demoted or removed.",
            code="organization_last_owner",
        )


def _require_an_owner_remains(db: Session, organization_id: str) -> None:
    """Post-write backstop: at least one OWNER must remain after the change."""
    db.flush()
    if _owner_count(db, organization_id) < 1:
        raise ConflictError(
            "The organization's last owner cannot be demoted or removed.",
            code="organization_last_owner",
        )


def _member_row(db: Session, membership: OrganizationMember) -> MemberRow:
    user = db.get(User, membership.user_id)
    return MemberRow(
        user_id=membership.user_id,
        email=user.email,
        full_name=user.full_name,
        role=Role(membership.role),
        created_at=as_utc(membership.created_at),
    )


def list_members(db: Session, *, organization_id: str) -> list[MemberRow]:
    """Return every member of the organization, oldest membership first.

    Ordered by membership ``created_at`` then ``user_id`` so the order is stable
    under equal timestamps. Read-only.
    """
    rows = db.execute(
        select(
            OrganizationMember.user_id,
            User.email,
            User.full_name,
            OrganizationMember.role,
            OrganizationMember.created_at,
        )
        .join(User, User.id == OrganizationMember.user_id)
        .where(OrganizationMember.organization_id == organization_id)
        .order_by(OrganizationMember.created_at.asc(), OrganizationMember.user_id.asc())
    ).all()
    return [
        MemberRow(
            user_id=user_id,
            email=email,
            full_name=full_name,
            role=Role(role),
            created_at=as_utc(created_at),
        )
        for user_id, email, full_name, role, created_at in rows
    ]


def change_member_role(
    db: Session,
    *,
    ctx: OrganizationContext,
    target_user_id: str,
    new_role: Role,
) -> MemberRow:
    """Change a member's organization role under the lock and the role rules.

    Changing a member to the role they already hold is an authorized no-op: every
    rule is still applied, nothing is written and no audit entry is recorded.
    Flushes but never commits.
    """
    new_role = Role(new_role)
    organization_id = ctx.organization.id
    actor_role = _lock_and_authorize_actor(db, ctx)

    if target_user_id == ctx.user.id:
        raise PermissionDeniedError(
            "You cannot change your own role.", code="member_self_change_forbidden"
        )
    target = _load_target(db, organization_id=organization_id, target_user_id=target_user_id)
    current_role = Role(target.role)

    if Role.OWNER in (current_role, new_role) and actor_role is not Role.OWNER:
        raise PermissionDeniedError(
            "Only an owner can grant, change or remove the owner role.",
            code="member_owner_required",
        )
    ceiling = role_rank(actor_role)
    if role_rank(new_role) > ceiling or role_rank(current_role) > ceiling:
        raise PermissionDeniedError(
            "You cannot manage a member or assign a role ranked above your own.",
            code="member_role_ceiling",
        )
    if current_role is Role.OWNER and new_role is not Role.OWNER:
        _require_owner_quorum(db, organization_id)

    if current_role is new_role:
        return _member_row(db, target)

    target.role = new_role.value
    if current_role is Role.OWNER:
        _require_an_owner_remains(db, organization_id)
    db.flush()
    record_audit(
        db,
        organization_id=organization_id,
        actor_user_id=ctx.user.id,
        action="organization_member.role_changed",
        entity_type="organization_member",
        entity_id=target_user_id,
        previous_state={"role": current_role.value},
        new_state={"role": new_role.value},
    )
    return _member_row(db, target)


def remove_member(db: Session, *, ctx: OrganizationContext, target_user_id: str) -> None:
    """Remove a member from the organization under the lock and the role rules.

    Deletes the membership row only; the user account and the user's other
    memberships are untouched. Flushes but never commits.
    """
    organization_id = ctx.organization.id
    actor_role = _lock_and_authorize_actor(db, ctx)

    if target_user_id == ctx.user.id:
        raise PermissionDeniedError(
            "You cannot remove yourself from the organization.",
            code="member_self_removal_forbidden",
        )
    target = _load_target(db, organization_id=organization_id, target_user_id=target_user_id)
    current_role = Role(target.role)

    if current_role is Role.OWNER and actor_role is not Role.OWNER:
        raise PermissionDeniedError(
            "Only an owner can grant, change or remove the owner role.",
            code="member_owner_required",
        )
    if role_rank(current_role) > role_rank(actor_role):
        raise PermissionDeniedError(
            "You cannot manage a member or assign a role ranked above your own.",
            code="member_role_ceiling",
        )
    if current_role is Role.OWNER:
        _require_owner_quorum(db, organization_id)

    db.delete(target)
    if current_role is Role.OWNER:
        _require_an_owner_remains(db, organization_id)
    db.flush()
    record_audit(
        db,
        organization_id=organization_id,
        actor_user_id=ctx.user.id,
        action="organization_member.removed",
        entity_type="organization_member",
        entity_id=target_user_id,
        previous_state={"role": current_role.value},
    )


__all__ = [
    "ORGANIZATION_ADMIN_ROLES",
    "MemberRow",
    "as_utc",
    "change_member_role",
    "list_members",
    "load_membership",
    "lock_organization",
    "remove_member",
    "role_rank",
]
