"""Local auth provider: registration + password authentication.

The provider is abstracted so a hosted identity provider can replace it later, but the
local implementation is fully functional for development.
"""

from __future__ import annotations

import re
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.models import AuthSession
from app.core.config import get_settings
from app.core.enums import Role
from app.core.errors import AuthError, ConflictError
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
