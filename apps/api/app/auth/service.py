"""Local auth provider: registration + password authentication.

The provider is abstracted so a hosted identity provider can replace it later, but the
local implementation is fully functional for development.
"""

from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.enums import Role
from app.core.errors import AuthError, ConflictError
from app.core.security import create_access_token, hash_password, verify_password
from app.organizations.models import Organization, OrganizationMember, User


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "org"


def register(db: Session, *, email: str, full_name: str, password: str, org_name: str) -> User:
    existing = db.scalar(select(User).where(User.email == email))
    if existing:
        raise ConflictError("An account with this email already exists.")

    user = User(email=email, full_name=full_name, hashed_password=hash_password(password))
    db.add(user)
    db.flush()

    slug = _slugify(org_name)
    if db.scalar(select(Organization).where(Organization.slug == slug)):
        slug = f"{slug}-{user.id[:6]}"
    org = Organization(name=org_name, slug=slug)
    db.add(org)
    db.flush()

    db.add(OrganizationMember(organization_id=org.id, user_id=user.id, role=Role.OWNER.value))
    db.flush()
    return user


def authenticate(db: Session, *, email: str, password: str) -> User:
    user = db.scalar(select(User).where(User.email == email))
    if not user or not verify_password(password, user.hashed_password):
        raise AuthError("Invalid email or password.")
    if not user.is_active:
        raise AuthError("This account is inactive.")
    return user


def issue_token(user: User) -> str:
    return create_access_token(subject=user.id, extra={"email": user.email})
