from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.organizations.invitations import MAX_TOKEN_LENGTH
from app.organizations.schemas import InvitationRole


class RegisterRequest(BaseModel):
    email: EmailStr
    full_name: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=8, max_length=128)
    organization_name: str = Field(min_length=1, max_length=200)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class UserOut(BaseModel):
    id: str
    email: EmailStr
    full_name: str
    #: Server-controlled platform-operator flag. Read-only to the client; used
    #: only to decide whether to request detailed runtime introspection.
    is_operator: bool = False
    #: ``email_verified_at is not None`` for the stored address. Status only: it is a
    #: soft gate (FD-4) and restricts no feature; the timestamp itself is not exposed.
    email_verified: bool = False


class MembershipOut(BaseModel):
    organization_id: str
    organization_name: str
    role: str


class SessionOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut
    memberships: list[MembershipOut]


class InvitationTokenRequest(BaseModel):
    """An invitation token, carried in the body and never in a URL.

    Only the token: the organization, role and email are the invitation's own and are
    never taken from the caller. Unknown fields are rejected.
    """

    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=1, max_length=MAX_TOKEN_LENGTH)


class InvitationRegisterRequest(BaseModel):
    """Create an account through an invitation.

    The account's email is the invitation's; the caller supplies only the token and the
    account's name and password, bounded as in :class:`RegisterRequest`. No organization
    is created. Unknown fields are rejected.
    """

    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=1, max_length=MAX_TOKEN_LENGTH)
    full_name: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=8, max_length=128)


class PasswordResetRequest(BaseModel):
    """Ask for a password-reset email.

    The answer is the same for every address, known or not, so it reveals nothing about
    which accounts exist. Unknown fields are rejected.
    """

    model_config = ConfigDict(extra="forbid")

    email: EmailStr


class PasswordResetConfirmRequest(BaseModel):
    """Set a new password with a reset token carried in the body, never in a URL.

    The new password is bounded as in :class:`RegisterRequest`. Unknown fields are
    rejected.
    """

    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=1, max_length=MAX_TOKEN_LENGTH)
    new_password: str = Field(min_length=8, max_length=128)


class EmailVerificationRequest(BaseModel):
    """Ask for a verification email for the signed-in account's stored address.

    Deliberately empty: the address is the account's own and is never taken from the
    caller. Unknown fields are rejected.
    """

    model_config = ConfigDict(extra="forbid")


class EmailVerificationConfirmRequest(BaseModel):
    """A verification token, carried in the body and never in a URL.

    Only the token: the account is the signed-in caller's, and the token must be that
    account's. Unknown fields are rejected.
    """

    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=1, max_length=MAX_TOKEN_LENGTH)


class InvitationPreviewOut(BaseModel):
    """What an invitation offers, shown before accepting it. Reading it spends nothing."""

    model_config = ConfigDict(from_attributes=True)

    organization_id: str
    organization_name: str
    email: str
    role: InvitationRole
    expires_at: datetime
