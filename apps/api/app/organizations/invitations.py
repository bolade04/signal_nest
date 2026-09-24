"""Organization invitations: create, list, revoke, preview and accept.

An OWNER or ADMIN invites an email address into their organization with a non-OWNER
role. The invitation carries a one-time token that is shared manually (there is no
email transport): :func:`create_invitation` returns the raw token exactly once and
only its SHA-256 hex digest is stored, so a stored row can never be turned back into
a working link. The token is never logged, audited or placed in an error, and
neither is its digest.

Acceptance is one-time and transactional. An existing user accepts while signed in
with the invited email address; a new user registers through the invitation, which
creates the account and the membership but never an organization. Either way the
invitation is claimed with a conditional ``UPDATE`` whose ``WHERE`` re-states the
pending predicate, and the claim only counts when exactly one row changed, so a
replayed, revoked or expired token can never be claimed twice. The inviter's
authority is re-checked at acceptance: an inviter who has since left, been demoted
below ADMIN, or can no longer assign the invited role cannot confer access.

Time is the database's clock, never the application host's or the browser's: the
expiry is computed from it at creation, and every expiry decision compares against
it. :func:`database_now` reads it once per operation; that one reading is bound
into every SQL predicate the operation issues and used for the derived state, so
the decision and the write always agree. The state is never stored: revoked, else
accepted, else expired (``expires_at`` at or before now), else pending.

Conventions mirror the house service style (``capabilities/service.py``): explicit
``db: Session`` first positional, keyword-only domain args, caller-owns-transaction
(the service flushes but never commits — routes commit before returning success),
constraint races mapped to stable 409 codes, and ``SignalNestError`` subclasses
rather than ``HTTPException``. A ``begin_nested`` savepoint is only ever opened after
the transaction's first write: under pysqlite an outermost savepoint commits on
release, which would break caller-owns-transaction.
"""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit.service import record_audit
from app.auth.dependencies import OrganizationContext
from app.core.config import get_settings
from app.core.enums import Role
from app.core.errors import ConflictError, NotFoundError, PermissionDeniedError
from app.organizations.members import (
    ORGANIZATION_ADMIN_ROLES,
    as_utc,
    load_membership,
    lock_organization,
    role_rank,
)
from app.organizations.models import (
    Organization,
    OrganizationInvitation,
    OrganizationMember,
    User,
)

#: Roles an invitation may carry. OWNER is granted only by an existing OWNER through
#: a role change, never by invitation.
INVITABLE_ROLES: frozenset[Role] = frozenset(Role) - {Role.OWNER}

#: Upper bound on a presented token. Issued tokens are 43 characters; anything far
#: longer cannot be one and is rejected before it is hashed.
MAX_TOKEN_LENGTH = 128


class InvitationState(StrEnum):
    """Derived lifecycle state of an invitation. Never persisted."""

    PENDING = "pending"
    ACCEPTED = "accepted"
    REVOKED = "revoked"
    EXPIRED = "expired"


@dataclass(frozen=True, slots=True)
class InvitationPreview:
    """What a token holder may see before accepting. Carries no token material."""

    organization_id: str
    organization_name: str
    email: str
    role: Role
    expires_at: datetime


# --------------------------------------------------------------------------- #
# Tokens and the database clock
# --------------------------------------------------------------------------- #
def hash_invitation_token(raw: str) -> str:
    """SHA-256 hex digest (64 lowercase hex characters) of a raw token."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def generate_invitation_token() -> tuple[str, str]:
    """Mint a token: ``(raw, digest)``. Only the digest is ever stored."""
    raw = secrets.token_urlsafe(32)
    return raw, hash_invitation_token(raw)


def database_now(db: Session) -> datetime:
    """The database's current time, as an aware UTC datetime.

    PostgreSQL's ``now()`` is the transaction's start time, so every reading in one
    transaction agrees. SQLite's ``CURRENT_TIMESTAMP`` has whole-second resolution,
    so the millisecond form of ``'now'`` is read instead; SQLite returns it as naive
    UTC text.
    """
    if db.get_bind().dialect.name == "sqlite":
        text_value = db.scalar(select(func.strftime("%Y-%m-%d %H:%M:%f", "now")))
        return as_utc(datetime.fromisoformat(text_value))
    return as_utc(db.scalar(select(func.now())))


def invitation_state(invitation: OrganizationInvitation, *, now: datetime) -> InvitationState:
    """Derive the state of ``invitation`` at the database time ``now``."""
    if invitation.revoked_at is not None:
        return InvitationState.REVOKED
    if invitation.accepted_at is not None:
        return InvitationState.ACCEPTED
    if as_utc(invitation.expires_at) <= now:
        return InvitationState.EXPIRED
    return InvitationState.PENDING


# --------------------------------------------------------------------------- #
# Internal helpers
# --------------------------------------------------------------------------- #
_STATE_CONFLICTS = {
    InvitationState.ACCEPTED: ("This invitation has already been used.", "invitation_already_used"),
    InvitationState.REVOKED: ("This invitation has been revoked.", "invitation_revoked"),
    InvitationState.EXPIRED: ("This invitation has expired.", "invitation_expired"),
}


def _pending_predicate(now: datetime) -> tuple:
    return (
        OrganizationInvitation.accepted_at.is_(None),
        OrganizationInvitation.revoked_at.is_(None),
        OrganizationInvitation.expires_at > now,
    )


def _raise_unless_pending(invitation: OrganizationInvitation, *, now: datetime) -> None:
    state = invitation_state(invitation, now=now)
    if state is not InvitationState.PENDING:
        message, code = _STATE_CONFLICTS[state]
        raise ConflictError(message, code=code)


def _invalid_token() -> NotFoundError:
    return NotFoundError("This invitation link is invalid.", code="invitation_invalid")


def _not_found() -> NotFoundError:
    return NotFoundError("Invitation not found.", code="invitation_not_found")


def _pending_exists() -> ConflictError:
    return ConflictError(
        "A pending invitation already exists for this email address.",
        code="invitation_pending_exists",
    )


def _account_exists() -> ConflictError:
    return ConflictError(
        "An account with this email address already exists. Sign in to accept the invitation.",
        code="invitation_account_exists",
    )


def _reload(db: Session, invitation_id: str) -> OrganizationInvitation | None:
    """Read one invitation fresh from the database, bypassing the identity map."""
    return db.execute(
        select(OrganizationInvitation)
        .where(OrganizationInvitation.id == invitation_id)
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()


def _open_invitation(
    db: Session, *, organization_id: str, email: str
) -> OrganizationInvitation | None:
    """The single not-accepted, not-revoked row for (organization, email), if any."""
    return db.execute(
        select(OrganizationInvitation)
        .where(
            OrganizationInvitation.organization_id == organization_id,
            OrganizationInvitation.email == email,
            OrganizationInvitation.accepted_at.is_(None),
            OrganizationInvitation.revoked_at.is_(None),
        )
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()


def _transition_pending(
    db: Session,
    invitation: OrganizationInvitation,
    *,
    now: datetime,
    values: dict,
    missing: Callable[[], NotFoundError],
) -> OrganizationInvitation:
    """Apply ``values`` only while the invitation is still pending at ``now``.

    The pending predicate lives inside the ``UPDATE``'s ``WHERE`` — never a
    read-then-write in Python — so the check and the mutation are one atomic
    compare-and-set. When the row did not change, it is re-read and the state that
    beat us is reported with its 409 code. ``synchronize_session=False`` because
    the instance is re-read with ``populate_existing`` straight after.
    """
    result = db.execute(
        update(OrganizationInvitation)
        .where(OrganizationInvitation.id == invitation.id, *_pending_predicate(now))
        .values(updated_at=now, **values),
        execution_options={"synchronize_session": False},
    )
    fresh = _reload(db, invitation.id)
    if fresh is None:
        raise missing()
    if result.rowcount != 1:
        _raise_unless_pending(fresh, now=now)
        raise ConflictError("The invitation changed while it was being processed.")
    return fresh


def _is_unique_violation(exc: IntegrityError) -> bool:
    """Whether the driver reports a unique violation, without querying the database.

    After a failed flush the transaction cannot be queried (PostgreSQL has aborted
    it), so the driver's own classification is read instead: psycopg's SQLSTATE,
    or sqlite3's extended error name. An unclassifiable error counts as a unique
    violation, the one integrity failure this insert can realistically meet.
    """
    code = getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)
    if code is not None:
        return code == "23505"
    name = getattr(exc.orig, "sqlite_errorname", None)
    if name is not None:
        return name == "SQLITE_CONSTRAINT_UNIQUE"
    return True


def _audit_state(invitation: OrganizationInvitation) -> dict:
    """Bounded, secret-free audit state: never the token or its digest."""
    return {
        "email": invitation.email,
        "role": Role(invitation.role).value,
        "expires_at": as_utc(invitation.expires_at).isoformat(),
    }


def _require_administrator(ctx: OrganizationContext) -> None:
    """Defence in depth behind the routes' exact OWNER/ADMIN gate."""
    role = Role(ctx.role)
    if role not in ORGANIZATION_ADMIN_ROLES:
        raise PermissionDeniedError(f"Role '{role.value}' is not permitted for this action.")


def _email_is_member(db: Session, *, organization_id: str, email: str) -> bool:
    return (
        db.scalar(
            select(OrganizationMember.id)
            .join(User, User.id == OrganizationMember.user_id)
            .where(OrganizationMember.organization_id == organization_id, User.email == email)
            .limit(1)
        )
        is not None
    )


def _account_exists_for(db: Session, email: str) -> bool:
    return db.scalar(select(User.id).where(User.email == email).limit(1)) is not None


def _resolve_token(db: Session, token: str, *, now: datetime) -> OrganizationInvitation:
    """Find the pending invitation for a presented raw token.

    Unknown token → 404 ``invitation_invalid``; a known token that is not pending →
    the 409 code of its state (revoked, else already used, else expired).
    """
    if not isinstance(token, str) or not token or len(token) > MAX_TOKEN_LENGTH:
        raise _invalid_token()
    invitation = db.execute(
        select(OrganizationInvitation)
        .where(OrganizationInvitation.token_hash == hash_invitation_token(token))
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if invitation is None:
        raise _invalid_token()
    _raise_unless_pending(invitation, now=now)
    return invitation


def _require_inviter_authorized(db: Session, invitation: OrganizationInvitation) -> None:
    """Re-check, at acceptance, that the inviter could still issue this invitation.

    The inviter must still exist and be active, still be a member of the
    invitation's organization with role OWNER or ADMIN, and still be able to assign
    the invited role (never OWNER; never above their own rank). Read fresh, after
    the caller has locked the organization row.
    """
    role = Role(invitation.role)
    inviter_id = invitation.invited_by_user_id
    authorized = False
    if inviter_id is not None and role is not Role.OWNER:
        inviter = db.get(User, inviter_id, populate_existing=True)
        membership = load_membership(
            db, organization_id=invitation.organization_id, user_id=inviter_id
        )
        if inviter is not None and inviter.is_active and membership is not None:
            inviter_role = Role(membership.role)
            authorized = inviter_role in ORGANIZATION_ADMIN_ROLES and (
                role_rank(role) <= role_rank(inviter_role)
            )
    if not authorized:
        raise ConflictError(
            "The person who sent this invitation can no longer grant this access.",
            code="invitation_inviter_not_authorized",
        )


def _add_membership(
    db: Session, invitation: OrganizationInvitation, *, user_id: str
) -> OrganizationMember:
    membership = OrganizationMember(
        organization_id=invitation.organization_id,
        user_id=user_id,
        role=Role(invitation.role).value,
    )
    try:
        with db.begin_nested():
            db.add(membership)
            db.flush()
    except IntegrityError:
        # ``from None``: the driver error carries bound parameters and is not
        # chained into anything a caller could log.
        if (
            load_membership(db, organization_id=invitation.organization_id, user_id=user_id)
            is not None
        ):
            raise ConflictError(
                "You are already a member of this organization.",
                code="invitation_already_member",
            ) from None
        raise ConflictError("The invitation could not be accepted.") from None
    return membership


def _record_accepted(db: Session, invitation: OrganizationInvitation, *, user_id: str) -> None:
    record_audit(
        db,
        organization_id=invitation.organization_id,
        actor_user_id=user_id,
        action="organization_invitation.accepted",
        entity_type="organization_invitation",
        entity_id=invitation.id,
        new_state={
            "email": invitation.email,
            "role": Role(invitation.role).value,
            "accepted_by_user_id": user_id,
        },
    )


# --------------------------------------------------------------------------- #
# Administration: create, list, revoke
# --------------------------------------------------------------------------- #
def create_invitation(
    db: Session,
    *,
    ctx: OrganizationContext,
    email: str,
    role: Role,
) -> tuple[OrganizationInvitation, str]:
    """Invite ``email`` into ``ctx.organization`` with ``role``.

    Returns ``(invitation, raw_token)``; the raw token is returned here and nowhere
    else and cannot be recovered afterwards. An expired open invitation for the same
    address is revoked (and audited) and replaced; a still-pending one is a 409.
    Flushes but never commits.
    """
    role = Role(role)
    organization_id = ctx.organization.id
    _require_administrator(ctx)
    if role is Role.OWNER:
        raise PermissionDeniedError(
            "The owner role cannot be granted by invitation.",
            code="invitation_owner_forbidden",
        )
    if role_rank(role) > role_rank(ctx.role):
        raise PermissionDeniedError(
            "You cannot invite a member with a role ranked above your own.",
            code="invitation_role_forbidden",
        )
    if _email_is_member(db, organization_id=organization_id, email=email):
        raise ConflictError(
            "This email address already belongs to a member of the organization.",
            code="invitation_already_member",
        )

    now = database_now(db)
    existing = _open_invitation(db, organization_id=organization_id, email=email)
    if existing is not None:
        if invitation_state(existing, now=now) is InvitationState.PENDING:
            raise _pending_exists()
        superseded = db.execute(
            update(OrganizationInvitation)
            .where(
                OrganizationInvitation.id == existing.id,
                OrganizationInvitation.accepted_at.is_(None),
                OrganizationInvitation.revoked_at.is_(None),
                OrganizationInvitation.expires_at <= now,
            )
            .values(revoked_at=now, updated_at=now),
            execution_options={"synchronize_session": False},
        )
        if superseded.rowcount == 1:
            record_audit(
                db,
                organization_id=organization_id,
                actor_user_id=ctx.user.id,
                action="organization_invitation.revoked",
                entity_type="organization_invitation",
                entity_id=existing.id,
                reason="Expired invitation superseded by a new invitation.",
                previous_state=_audit_state(existing),
            )

    raw_token, token_hash = generate_invitation_token()
    invitation = OrganizationInvitation(
        organization_id=organization_id,
        email=email,
        role=role.value,
        token_hash=token_hash,
        invited_by_user_id=ctx.user.id,
        expires_at=now + timedelta(hours=get_settings().invitation_expire_hours),
    )
    # No savepoint here: when this insert is the transaction's first write, pysqlite
    # commits a released outermost SAVEPOINT at once, and a later failure would leave
    # a committed invitation whose token was never delivered. A failed flush instead
    # fails the whole transaction, which the caller rolls back; nothing earlier in
    # this call needs preserving.
    db.add(invitation)
    try:
        db.flush()
    except IntegrityError as exc:
        # ``from None``: the driver error carries the bound parameters, the token
        # digest among them.
        if _is_unique_violation(exc):
            # A concurrent create won the partial-unique-index race.
            raise _pending_exists() from None
        raise ConflictError("The invitation could not be created.") from None

    record_audit(
        db,
        organization_id=organization_id,
        actor_user_id=ctx.user.id,
        action="organization_invitation.created",
        entity_type="organization_invitation",
        entity_id=invitation.id,
        new_state=_audit_state(invitation),
    )
    return invitation, raw_token


def list_pending_invitations(db: Session, *, organization_id: str) -> list[OrganizationInvitation]:
    """Return the organization's pending invitations, newest first. Read-only.

    Accepted, revoked and expired invitations are excluded. Ordered
    ``created_at DESC, id DESC`` so the order is stable under equal timestamps.
    """
    now = database_now(db)
    return list(
        db.execute(
            select(OrganizationInvitation)
            .where(
                OrganizationInvitation.organization_id == organization_id,
                *_pending_predicate(now),
            )
            .order_by(OrganizationInvitation.created_at.desc(), OrganizationInvitation.id.desc())
        ).scalars()
    )


def revoke_invitation(
    db: Session, *, ctx: OrganizationContext, invitation_id: str
) -> OrganizationInvitation:
    """Revoke a pending invitation of ``ctx.organization``. Flushes but never commits.

    An invitation of another organization is reported exactly like an unknown one
    (404 ``invitation_not_found``), so ids cannot be probed across tenants.
    """
    _require_administrator(ctx)
    invitation = _reload(db, invitation_id)
    if invitation is None or invitation.organization_id != ctx.organization.id:
        raise _not_found()
    now = database_now(db)
    _raise_unless_pending(invitation, now=now)
    invitation = _transition_pending(
        db, invitation, now=now, values={"revoked_at": now}, missing=_not_found
    )
    record_audit(
        db,
        organization_id=invitation.organization_id,
        actor_user_id=ctx.user.id,
        action="organization_invitation.revoked",
        entity_type="organization_invitation",
        entity_id=invitation.id,
        previous_state=_audit_state(invitation),
    )
    return invitation


# --------------------------------------------------------------------------- #
# Token holders: preview and accept
# --------------------------------------------------------------------------- #
def preview_invitation(db: Session, *, token: str) -> InvitationPreview:
    """Describe the invitation behind ``token`` without consuming it. Read-only."""
    now = database_now(db)
    invitation = _resolve_token(db, token, now=now)
    organization = db.get(Organization, invitation.organization_id)
    if organization is None:
        raise _invalid_token()
    return InvitationPreview(
        organization_id=organization.id,
        organization_name=organization.name,
        email=invitation.email,
        role=Role(invitation.role),
        expires_at=as_utc(invitation.expires_at),
    )


def accept_invitation_existing_user(db: Session, *, user: User, token: str) -> OrganizationMember:
    """Accept an invitation as the signed-in ``user``; returns the new membership.

    The user's email must equal the invited email exactly. Flushes but never
    commits; the claim, the membership and the audit entry share the caller's
    transaction.
    """
    now = database_now(db)
    invitation = _resolve_token(db, token, now=now)
    if user.email != invitation.email:
        raise PermissionDeniedError("This invitation was issued to a different email address.")
    lock_organization(db, invitation.organization_id)
    if load_membership(db, organization_id=invitation.organization_id, user_id=user.id) is not None:
        raise ConflictError(
            "You are already a member of this organization.",
            code="invitation_already_member",
        )
    _require_inviter_authorized(db, invitation)

    invitation = _transition_pending(
        db,
        invitation,
        now=now,
        values={"accepted_at": now, "accepted_by_user_id": user.id},
        missing=_invalid_token,
    )
    membership = _add_membership(db, invitation, user_id=user.id)
    _record_accepted(db, invitation, user_id=user.id)
    return membership


def accept_invitation_new_user(
    db: Session,
    *,
    token: str,
    create_user: Callable[[str], User],
) -> tuple[User, OrganizationMember]:
    """Register a new account through an invitation; returns ``(user, membership)``.

    ``create_user`` receives the invited email and must build and flush the
    :class:`User` (the auth service supplies it, so this package never imports the
    auth service). No organization is created. The invitation is claimed first,
    then the user is created and recorded as the acceptor on the claimed row, then
    the membership is added — all in the caller's transaction. Flushes but never
    commits.
    """
    now = database_now(db)
    invitation = _resolve_token(db, token, now=now)
    lock_organization(db, invitation.organization_id)
    if _account_exists_for(db, invitation.email):
        raise _account_exists()
    _require_inviter_authorized(db, invitation)

    invitation = _transition_pending(
        db, invitation, now=now, values={"accepted_at": now}, missing=_invalid_token
    )
    try:
        with db.begin_nested():
            user = create_user(invitation.email)
            db.flush()
    except (IntegrityError, ConflictError) as exc:
        # A concurrent registration took the address after the check above.
        if _account_exists_for(db, invitation.email):
            raise _account_exists() from None
        if isinstance(exc, ConflictError):
            raise
        raise ConflictError("The account could not be created.") from None
    if user.id is None or user.email != invitation.email:
        raise RuntimeError("create_user must create the account for the invited email.")

    db.execute(
        update(OrganizationInvitation)
        .where(OrganizationInvitation.id == invitation.id)
        .values(accepted_by_user_id=user.id, updated_at=now),
        execution_options={"synchronize_session": False},
    )
    db.refresh(invitation)
    membership = _add_membership(db, invitation, user_id=user.id)
    _record_accepted(db, invitation, user_id=user.id)
    return user, membership


__all__ = [
    "INVITABLE_ROLES",
    "MAX_TOKEN_LENGTH",
    "InvitationPreview",
    "InvitationState",
    "accept_invitation_existing_user",
    "accept_invitation_new_user",
    "create_invitation",
    "database_now",
    "generate_invitation_token",
    "hash_invitation_token",
    "invitation_state",
    "list_pending_invitations",
    "preview_invitation",
    "revoke_invitation",
]
