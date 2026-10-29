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


class EmailKeyedDataOut(BaseModel):
    """Whether data found only by the account's email address counts as the account's.

    ``included``: the address is verified, so invitations addressed to it (and, for a
    deletion, audit values naming it) are exported or erased with the account.
    ``excluded_unverified``: the address was never verified, so nothing proves it is the
    account's; that data is neither exported nor erased, because it may be another
    person's. The invitations the account accepted are bound to it by id and are exported
    and erased in both cases. ``note`` says the same in words.
    """

    status: Literal["included", "excluded_unverified"]
    note: str


class AccountDeletionPreviewOut(BaseModel):
    """Read-only: whether the account can be deleted now, and what that would do."""

    can_delete: bool
    organizations: list[MembershipImpactOut]
    email_keyed_data: EmailKeyedDataOut


class ErasureFindingOut(BaseModel):
    table: str
    column: str
    rows: int


class ErasureVerificationOut(BaseModel):
    """The check a deletion request runs on the committed state (``app.privacy.verification``).

    It is the verifier's **reference check**, computed from the live schema (not from audit
    records and not from the deletion's own bookkeeping): every column that can point at
    the deleted subject is counted for the subject's own ids. ``references_cleared`` is true
    when none does and, for an organization, every retained audit record is anonymised.
    Findings name only rows that point at the subject itself.

    The scan of every text and JSON column for the subject's identifiers is not run in the
    request: it reads every text column of every table. An operator runs it with
    ``python -m app.privacy.cli verify-account`` / ``verify-organization`` (the E10 path).
    """

    references_cleared: bool
    subject_type: str
    subject_id: str | None
    tables_checked: int
    reference_columns_checked: int
    reference_findings: list[ErasureFindingOut]
    retained_anonymised: list[ErasureFindingOut]
    #: Where the content scan runs: the operator's command, never the request.
    content_scan: Literal["operator"]
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
    email_keyed_data: EmailKeyedDataOut


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
    email_keyed_data: EmailKeyedDataOut
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
    """One table in the export index: its exact row count, the first page and the cursor of
    the next one (None when the first page holds every row)."""

    row_count: int
    rows: list[dict[str, Any]]
    next_cursor: str | None


class OrganizationExportOut(BaseModel):
    """The organization export index (schema version 2): per table the exact ``row_count``,
    the first page and ``next_cursor``; follow the cursors with the table-page route."""

    schema_version: int
    generated_at: str
    subject: ExportSubjectOut
    page_size_default: int
    page_size_cap: int
    complete: bool
    consistency: str
    members: list[dict[str, Any]]
    tables: dict[str, ExportedTableOut]
    not_exported: list[str]


class OrganizationExportPageOut(BaseModel):
    """One keyset page of one exported table, after the cursor it was asked for."""

    schema_version: int
    generated_at: str
    subject: ExportSubjectOut
    table: str
    limit: int
    rows: list[dict[str, Any]]
    next_cursor: str | None
