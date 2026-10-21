"""Request and response models for the privacy routes (``P6-PRIV-1``)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class AccountDeletionRequest(BaseModel):
    """Delete the signed-in account. Re-authenticated: the current password is required.

    The account is the caller's own and is never taken from the body. Unknown fields are
    rejected.
    """

    model_config = ConfigDict(extra="forbid")

    current_password: str = Field(min_length=1, max_length=128)
    #: The word ``DELETE``, typed by the person.
    confirmation: Literal["DELETE"]


class OrganizationDeletionRequest(BaseModel):
    """Delete an organization. Re-authenticated, and its name typed exactly to confirm."""

    model_config = ConfigDict(extra="forbid")

    current_password: str = Field(min_length=1, max_length=128)
    confirm_organization_name: str = Field(min_length=1, max_length=200)


MembershipOutcomeValue = Literal["membership_removed", "organization_deleted", "blocked_sole_owner"]


class MembershipImpactOut(BaseModel):
    """What deleting the account does to one of its organizations."""

    model_config = ConfigDict(from_attributes=True)

    organization_id: str
    organization_name: str
    role: str
    member_count: int
    outcome: MembershipOutcomeValue


class AccountDeletionPreviewOut(BaseModel):
    """Read-only: whether the account can be deleted now, and what that would do."""

    can_delete: bool
    organizations: list[MembershipImpactOut]


class ErasureFindingOut(BaseModel):
    table: str
    column: str
    rows: int


class ErasureVerificationOut(BaseModel):
    """The independent verification report (``app.privacy.verification``).

    ``verified`` is true only when no row references the subject and no text or JSON
    value contains its identifiers. It is computed from the live schema, not from audit
    records and not from the deletion's own bookkeeping.
    """

    verified: bool
    subject_type: str
    subject_id: str | None
    email_sha256: str | None
    tables_checked: int
    reference_columns_checked: int
    content_columns_checked: int
    reference_findings: list[ErasureFindingOut]
    content_findings: list[ErasureFindingOut]
    retained_anonymised: list[ErasureFindingOut]
    generated_at: str


class ErasureCountsOut(BaseModel):
    #: Rows deleted, per table.
    deleted: dict[str, int]
    #: Values set to NULL, per ``table.column``.
    nulled: dict[str, int]
    #: Records anonymised in place, per table.
    anonymised: dict[str, int]


class DeletedOrganizationOut(BaseModel):
    organization_id: str
    workspace_ids: list[str]
    verification: ErasureVerificationOut


class AccountDeletionOut(BaseModel):
    """The deletion receipt. The account no longer exists when this is returned."""

    user_id: str
    organizations: list[MembershipImpactOut]
    deleted_organizations: list[DeletedOrganizationOut]
    counts: ErasureCountsOut
    verification: ErasureVerificationOut


class OrganizationDeletionOut(BaseModel):
    """The deletion receipt. Keep ``workspace_ids``: verification after the fact needs them."""

    organization_id: str
    workspace_ids: list[str]
    counts: ErasureCountsOut
    verification: ErasureVerificationOut


class ExportSubjectOut(BaseModel):
    type: Literal["account", "organization"]
    user_id: str | None = None
    organization_id: str | None = None


class AccountExportOut(BaseModel):
    """Everything stored about the signed-in account (schema version 1)."""

    schema_version: int
    generated_at: str
    subject: ExportSubjectOut
    profile: dict[str, Any]
    memberships: list[dict[str, Any]]
    sessions: list[dict[str, Any]]
    password_reset_requests: list[dict[str, Any]]
    email_verification_requests: list[dict[str, Any]]
    invitations_received: list[dict[str, Any]]
    invitations_sent: list[dict[str, Any]]
    feedback_submitted: list[dict[str, Any]]
    capability_overrides_set: list[dict[str, Any]]
    audit_events_performed: list[dict[str, Any]]


class ExportedTableOut(BaseModel):
    rows: list[dict[str, Any]]
    row_count: int
    truncated: bool


class OrganizationExportOut(BaseModel):
    """Every exported table of the organization (schema version 1)."""

    schema_version: int
    generated_at: str
    subject: ExportSubjectOut
    row_cap_per_table: int
    members: list[dict[str, Any]]
    tables: dict[str, ExportedTableOut]
    not_exported: list[str]
