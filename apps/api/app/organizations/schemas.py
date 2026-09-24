from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.core.enums import Role

#: The roles an invitation may carry: every role except OWNER, which only an existing
#: OWNER grants, through a role change. A literal, so OWNER is never offered.
InvitationRole = Literal["admin", "marketer", "reviewer", "compliance_reviewer", "viewer"]


class OrganizationOut(BaseModel):
    id: str
    name: str
    slug: str

    class Config:
        from_attributes = True


class WorkspaceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class WorkspaceOut(BaseModel):
    id: str
    organization_id: str
    name: str
    slug: str
    onboarding_completed: bool
    created_at: datetime

    class Config:
        from_attributes = True


class InvitationCreate(BaseModel):
    """Invite an email address into the organization in the path with a non-OWNER role.

    The organization and the inviter come from the authorized request, never the body.
    Unknown fields are rejected.
    """

    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    role: InvitationRole


class InvitationOut(BaseModel):
    """An invitation as its organization's administrators see it. Never carries the token."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    email: str
    role: InvitationRole
    expires_at: datetime
    created_at: datetime
    invited_by_user_id: str | None


class InvitationCreatedOut(InvitationOut):
    token: str = Field(
        description=(
            "The one-time invitation token. It is returned only in this response: the "
            "server keeps only its hash, so it can never be shown again or recovered. "
            "Share it with the invitee; accepting the invitation spends it."
        )
    )


class OrganizationMemberOut(BaseModel):
    """One member of an organization: identity and organization-wide role."""

    model_config = ConfigDict(from_attributes=True)

    user_id: str
    email: str
    full_name: str
    role: Role
    created_at: datetime


class MemberRoleUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Role
