"""Local auth provider: registration + password authentication.

The provider is abstracted so a hosted identity provider can replace it later, but the
local implementation is fully functional for development.
"""

from __future__ import annotations

import re
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.auth.account_tokens import _lock_user, _revoke_open
from app.auth.models import AuthSession, PasswordResetToken
from app.core.config import get_settings
from app.core.enums import Role
from app.core.errors import AuthError, ConflictError, ValidationDomainError
from app.core.security import create_access_token, hash_password, verify_password
from app.db.clock import database_now
from app.organizations import invitations as invitation_service
from app.organizations.models import Organization, OrganizationMember, User


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "org"


def create_user(db: Session, *, email: str, full_name: str, password: str) -> User:
    """Create and flush a user account, and nothing else.

    Shared by :func:`register` and :func:`register_invited`. No organization and no
    membership are created here; each caller decides which organization the user joins.
    """
    existing = db.scalar(select(User).where(User.email == email))
    if existing:
        raise ConflictError("An account with this email already exists.")

    user = User(email=email, full_name=full_name, hashed_password=hash_password(password))
    db.add(user)
    db.flush()
    return user


def register(db: Session, *, email: str, full_name: str, password: str, org_name: str) -> User:
    user = create_user(db, email=email, full_name=full_name, password=password)

    slug = _slugify(org_name)
    if db.scalar(select(Organization).where(Organization.slug == slug)):
        slug = f"{slug}-{user.id[:6]}"
    org = Organization(name=org_name, slug=slug)
    db.add(org)
    db.flush()

    db.add(OrganizationMember(organization_id=org.id, user_id=user.id, role=Role.OWNER.value))
    db.flush()
    return user


def register_invited(db: Session, *, token: str, full_name: str, password: str) -> User:
    """Create a user who joins the invitation's existing organization.

    The account's email is the invitation's, never the caller's, and no organization is
    created. The invitation service claims the invitation, calls back here to create the
    user, then adds the membership with the invitation's role; it also maps an email
    that is already registered to ``invitation_account_exists``.
    """

    def _create(email: str) -> User:
        return create_user(db, email=email, full_name=full_name, password=password)

    user, _membership = invitation_service.accept_invitation_new_user(
        db, token=token, create_user=_create
    )
    return user


def authenticate(db: Session, *, email: str, password: str) -> User:
    user = db.scalar(select(User).where(User.email == email))
    if not user or not verify_password(password, user.hashed_password):
        raise AuthError("Invalid email or password.")
    if not user.is_active:
        raise AuthError("This account is inactive.")
    return user


def create_session(db: Session, user: User) -> AuthSession:
    """Open a new sign-in session for ``user`` (P6-AUTH-4); flushed, not committed.

    Called only at the three fresh-credential boundaries -- login, register and
    invitation register -- and never from a bearer-authenticated request, so holding a
    token can never open a session. ``created_at`` is the DATABASE clock and
    ``expires_at`` is fixed here, ``session_absolute_lifetime_minutes`` later; nothing
    ever moves it. The caller commits before it mints a token for the session.
    """
    now = database_now(db)
    lifetime = timedelta(minutes=get_settings().session_absolute_lifetime_minutes)
    session = AuthSession(user_id=user.id, created_at=now, expires_at=now + lifetime)
    db.add(session)
    db.flush()
    return session


def issue_token(user: User, session: AuthSession) -> str:
    """Issue an access token for ``user`` inside ``session``.

    The only access-token issuance path. Every claim comes from server state: the
    session's id (``sid``), the account's credential epoch and the session's fixed
    absolute expiry, which caps the token's ``exp``. ``get_current_user`` refuses a token
    whose session is revoked or expired, or whose ``auth_epoch`` differs from the
    account's, so a sign-out ends the session's tokens and a password reset ends every
    token issued before it.
    """
    if session.user_id != user.id:
        raise ValueError("A token can only be issued inside the user's own session.")
    return create_access_token(
        subject=user.id,
        session_id=session.id,
        auth_epoch=user.auth_epoch,
        expires_at_cap=session.expires_at,
        email=user.email,
    )


def _unchanged_since_verified(locked: User | None, *, epoch: int, hashed_password: str) -> bool:
    """Whether the locked row is still the credential state a password change verified.

    Plain comparisons, no bcrypt (it runs under the user-row lock): the account is active
    and both its ``auth_epoch`` and its stored hash STRING are the ones read when the
    request authenticated. Any committed change or reset since then moves both. Factored
    out so the PostgreSQL race tests can prove, by removing it, that it is load-bearing.
    """
    return (
        locked is not None
        and locked.is_active
        and locked.auth_epoch == epoch
        and locked.hashed_password == hashed_password
    )


def change_password(db: Session, *, user: User, current_password: str, new_password: str) -> None:
    """Replace the signed-in ``user``'s password (P6-UI-017); flushed, not committed.

    ``user`` is the account the bearer dependency has just authenticated, so the values
    it was loaded with are the state the caller proved: its ``auth_epoch`` equals the
    token's epoch, and its ``hashed_password`` is the hash the current password is
    checked against. Both are captured before anything else, because the lock below
    re-reads the same instance.

    The order is fixed, and every bcrypt call runs BEFORE the lock, so the user row is
    never held across bcrypt:

    1. the current password must verify against the captured hash, else 422
       ``current_password_incorrect``. Never 401: a wrong entry is a field error, and
       the caller's session stays valid;
    2. only then, a new password equivalent to the current one (bcrypt semantics: the
       first 72 bytes count) is 422 ``password_unchanged``. Checked after step 1 so it
       can never serve as an oracle for guessing the current password;
    3. the new password is hashed;
    4. the user row is locked (``SELECT … FOR UPDATE``, as a password reset locks it)
       and, with no bcrypt, the verified state is re-checked: the account is active and
       its epoch and stored hash are still the captured ones. Any difference means
       another credential change (a change or a reset) committed after this request
       authenticated, so the caller's token is already dead: 401 "Invalid or expired
       token." and nothing is written;
    5. one ``UPDATE`` replaces the hash and increments ``auth_epoch`` as a SQL
       expression, so every token issued before it -- the caller's included -- stops
       authenticating. ``email_verified_at`` is untouched: a password proves nothing
       about the mailbox;
    6. every open password-reset token of the account is revoked, so a link mailed
       earlier cannot take the account back. Email-verification tokens are untouched.

    No session is created, extended or revoked (Model A): the epoch alone ends every
    session's tokens and leaves their rows inert, exactly as a password reset does. Time
    is the database clock. The route commits before it answers.
    """
    user_id, epoch, current_hash = user.id, user.auth_epoch, user.hashed_password
    if not verify_password(current_password, current_hash):
        raise ValidationDomainError(
            "The current password is incorrect.", code="current_password_incorrect"
        )
    if verify_password(new_password, current_hash):
        raise ValidationDomainError(
            "Choose a password different from your current one.", code="password_unchanged"
        )
    new_hash = hash_password(new_password)
    now = database_now(db)

    locked = _lock_user(db, User.id == user_id)
    if not _unchanged_since_verified(locked, epoch=epoch, hashed_password=current_hash):
        raise AuthError("Invalid or expired token.")
    db.execute(
        update(User)
        .where(User.id == user_id)
        .values(hashed_password=new_hash, auth_epoch=User.auth_epoch + 1, updated_at=now),
        execution_options={"synchronize_session": False},
    )
    _revoke_open(db, PasswordResetToken, user_id=user_id, now=now)
    db.expire(locked, ["hashed_password", "auth_epoch", "updated_at"])
    db.flush()
