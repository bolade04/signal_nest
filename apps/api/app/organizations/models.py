"""Tenancy root models: users, organizations, memberships, invitations, workspaces."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    false,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import Role
from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class User(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(320), unique=True, index=True, nullable=False)
    full_name: Mapped[str] = mapped_column(String(200), nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    #: Platform-operator flag. Server-controlled only (never set from client
    #: input): gates access to detailed infrastructure introspection. Defaults to
    #: False so ordinary customers can never see runtime topology; the demo seed
    #: sets it True only in local/test environments.
    is_operator: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )

    memberships: Mapped[list[OrganizationMember]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class Organization(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "organizations"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(120), unique=True, index=True, nullable=False)

    members: Mapped[list[OrganizationMember]] = relationship(
        back_populates="organization", cascade="all, delete-orphan"
    )
    workspaces: Mapped[list[Workspace]] = relationship(
        back_populates="organization", cascade="all, delete-orphan"
    )


class OrganizationMember(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "organization_members"
    __table_args__ = (UniqueConstraint("organization_id", "user_id", name="uq_org_user"),)

    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True, nullable=False
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    role: Mapped[Role] = mapped_column(String(40), nullable=False)

    organization: Mapped[Organization] = relationship(back_populates="members")
    user: Mapped[User] = relationship(back_populates="memberships")


#: Predicate selecting the invitations still open for acceptance, shared by the
#: partial unique index on both dialects.
_PENDING_INVITATION_PREDICATE = "accepted_at IS NULL AND revoked_at IS NULL"


def _role_in_vocabulary_sql() -> str:
    """Render the closed role vocabulary as a portable ``IN`` guard.

    Derived from :class:`Role` so the storable set can never drift from the role
    vocabulary. Deterministic (sorted) so the migration renders identically on
    every regeneration.
    """
    values = ", ".join(f"'{value}'" for value in sorted(role.value for role in Role))
    return f"role IN ({values})"


class OrganizationInvitation(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A one-time invitation for an email address to join an organization.

    Only the SHA-256 hex digest of the invitation token is stored; the raw token is
    returned once, at creation, and is never persisted. There is no status column:
    the state is derived — ``revoked_at`` set means revoked, else ``accepted_at`` set
    means accepted, else ``expires_at`` at or before the database clock means
    expired, else pending.
    """

    __tablename__ = "organization_invitations"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_organization_invitations_token_hash"),
        CheckConstraint(_role_in_vocabulary_sql(), name="ck_organization_invitations_role"),
        # At most one open invitation per (organization, email). Accepted and
        # revoked rows fall outside the predicate, so history is kept.
        Index(
            "uq_organization_invitations_pending",
            "organization_id",
            "email",
            unique=True,
            postgresql_where=text(_PENDING_INVITATION_PREDICATE),
            sqlite_where=text(_PENDING_INVITATION_PREDICATE),
        ),
    )

    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True, nullable=False
    )
    #: The invited address exactly as validated at creation (exact-match semantics).
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    role: Mapped[Role] = mapped_column(String(40), nullable=False)
    #: SHA-256 hex digest of the invitation token. Never logged, audited or returned.
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # SET NULL so deleting a user forgets who invited without destroying the row;
    # an invitation whose inviter is gone can no longer be accepted.
    invited_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    accepted_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Workspace(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A brand workspace within an organization (tenant sub-scope)."""

    __tablename__ = "workspaces"
    __table_args__ = (UniqueConstraint("organization_id", "slug", name="uq_ws_org_slug"),)

    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(120), nullable=False)
    onboarding_completed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    organization: Mapped[Organization] = relationship(back_populates="workspaces")
