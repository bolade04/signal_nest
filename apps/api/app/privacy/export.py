"""Data export — portability for an account and for an organization (``P6-PRIV-1``).

Read-only: an export writes nothing, not even an audit record. Both shapes carry a
``schema_version`` so a consumer can tell versions apart.

* **Account export** — the signed-in account's own data: profile, memberships, sessions,
  account-token history, invitations addressed to it, invitations it sent (without the
  invitee's address, which is someone else's data), feedback it submitted, capability
  overrides it set and the audit events it performed. No credential, token or digest.
  The invitations it accepted are bound to it by id and always listed; the others addressed
  to its email address are found by that address, so they are listed only when it is
  verified (:class:`~app.privacy.erasure.EmailScope`); ``email_keyed_data`` says which, in
  words.
* **Organization export** — every table the inventory marks exported, restricted to the
  organization (by ``organization_id``, or by its workspaces for the tables that carry
  only ``workspace_id``), minus the columns the inventory excludes (token digests and
  derived vectors), plus the members with their email and name. Each table is capped at
  :data:`ORGANIZATION_EXPORT_ROW_CAP` rows and says when it was truncated.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import Table, or_, select
from sqlalchemy.orm import Session

from app.audit.models import AuditLog
from app.auth.models import AuthSession, EmailVerificationToken, PasswordResetToken
from app.capabilities.models import WorkspaceCapabilityOverride
from app.db.clock import as_utc
from app.db.models import Base
from app.feedback.models import OpportunityFeedback
from app.organizations.models import (
    Organization,
    OrganizationInvitation,
    OrganizationMember,
    User,
    Workspace,
)
from app.privacy.erasure import EmailScope, email_scope
from app.privacy.inventory import TABLES
from app.privacy.matching import Subject, existing_addresses, prefilter, same_address

EXPORT_SCHEMA_VERSION = 1
ORGANIZATION_EXPORT_ROW_CAP = 5000


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return as_utc(value).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


def _rows(db: Session, stmt) -> list[dict[str, Any]]:
    return [{k: _jsonable(v) for k, v in row._mapping.items()} for row in db.execute(stmt)]


def _now() -> str:
    return datetime.now(UTC).isoformat()


_EMAIL_KEYED_NOTE = {
    EmailScope.INCLUDED: (
        "Your email address is verified, so every invitation addressed to it is included."
    ),
    EmailScope.EXCLUDED_UNVERIFIED: (
        "Your email address is not verified, so only the invitations you accepted are "
        "included; other invitations addressed to it are not, because nothing proves the "
        "address is yours. Verify it, then download your data again."
    ),
}


def _invitations_received(db: Session, *, user: User, scope: EmailScope) -> list[dict[str, Any]]:
    """The invitations the account accepted and, when its address is verified, every other one
    sent to that address in any form. The address is compared in Python (lower-cased, any
    normalization or domain label form): SQLite's ``lower()`` does not fold non-ASCII letters."""
    accepted = OrganizationInvitation.accepted_by_user_id == user.id
    addressed = prefilter(OrganizationInvitation.email, Subject(addresses=(user.email,)))
    rows = db.execute(
        select(
            OrganizationInvitation.id,
            OrganizationInvitation.organization_id,
            Organization.name.label("organization_name"),
            OrganizationInvitation.role,
            OrganizationInvitation.created_at,
            OrganizationInvitation.expires_at,
            OrganizationInvitation.accepted_at,
            OrganizationInvitation.revoked_at,
            OrganizationInvitation.email.label("addressed_to"),
            OrganizationInvitation.accepted_by_user_id.label("accepted_by"),
        )
        .join(Organization, Organization.id == OrganizationInvitation.organization_id)
        .where(or_(accepted, addressed) if scope is EmailScope.INCLUDED else accepted)
        .order_by(OrganizationInvitation.created_at, OrganizationInvitation.id)
    ).all()
    out = []
    existing = existing_addresses(db)
    for row in rows:
        values = {k: _jsonable(v) for k, v in row._mapping.items()}
        addressed_to, accepted_by = values.pop("addressed_to"), values.pop("accepted_by")
        if accepted_by == user.id or same_address(addressed_to, user.email, existing):
            out.append(values)
    return out


def export_account(db: Session, *, user: User) -> dict[str, Any]:
    """Everything stored about the signed-in account, in machine-readable form."""
    uid = user.id
    scope = email_scope(user)
    return {
        "schema_version": EXPORT_SCHEMA_VERSION,
        "generated_at": _now(),
        "subject": {"type": "account", "user_id": uid},
        "email_keyed_data": {"status": scope.value, "note": _EMAIL_KEYED_NOTE[scope]},
        "profile": {
            "id": uid,
            "email": user.email,
            "full_name": user.full_name,
            "email_verified_at": _jsonable(user.email_verified_at),
            "is_operator": user.is_operator,
            "created_at": _jsonable(user.created_at),
            "updated_at": _jsonable(user.updated_at),
        },
        "memberships": _rows(
            db,
            select(
                OrganizationMember.organization_id,
                Organization.name.label("organization_name"),
                OrganizationMember.role,
                OrganizationMember.created_at.label("joined_at"),
            )
            .join(Organization, Organization.id == OrganizationMember.organization_id)
            .where(OrganizationMember.user_id == uid)
            .order_by(OrganizationMember.created_at, OrganizationMember.organization_id),
        ),
        "sessions": _rows(
            db,
            select(
                AuthSession.id, AuthSession.created_at, AuthSession.expires_at,
                AuthSession.revoked_at,
            )
            .where(AuthSession.user_id == uid)
            .order_by(AuthSession.created_at, AuthSession.id),
        ),
        "password_reset_requests": _rows(
            db,
            select(
                PasswordResetToken.created_at, PasswordResetToken.expires_at,
                PasswordResetToken.used_at, PasswordResetToken.revoked_at,
            )
            .where(PasswordResetToken.user_id == uid)
            .order_by(PasswordResetToken.created_at),
        ),
        "email_verification_requests": _rows(
            db,
            select(
                EmailVerificationToken.email_snapshot, EmailVerificationToken.created_at,
                EmailVerificationToken.expires_at, EmailVerificationToken.used_at,
                EmailVerificationToken.revoked_at,
            )
            .where(EmailVerificationToken.user_id == uid)
            .order_by(EmailVerificationToken.created_at),
        ),
        "invitations_received": _invitations_received(db, user=user, scope=scope),
        "invitations_sent": _rows(
            db,
            select(
                OrganizationInvitation.id,
                OrganizationInvitation.organization_id,
                OrganizationInvitation.role,
                OrganizationInvitation.created_at,
                OrganizationInvitation.expires_at,
                OrganizationInvitation.accepted_at,
                OrganizationInvitation.revoked_at,
            )
            .where(OrganizationInvitation.invited_by_user_id == uid)
            .order_by(OrganizationInvitation.created_at, OrganizationInvitation.id),
        ),
        "feedback_submitted": _rows(
            db,
            select(
                OpportunityFeedback.id,
                OpportunityFeedback.organization_id,
                OpportunityFeedback.workspace_id,
                OpportunityFeedback.opportunity_id,
                OpportunityFeedback.is_useful,
                OpportunityFeedback.reason_code,
                OpportunityFeedback.created_at,
            )
            .where(OpportunityFeedback.submitted_by_user_id == uid)
            .order_by(OpportunityFeedback.created_at, OpportunityFeedback.id),
        ),
        "capability_overrides_set": _rows(
            db,
            select(
                WorkspaceCapabilityOverride.id,
                WorkspaceCapabilityOverride.organization_id,
                WorkspaceCapabilityOverride.workspace_id,
                WorkspaceCapabilityOverride.capability,
                WorkspaceCapabilityOverride.enabled,
                WorkspaceCapabilityOverride.reason,
                WorkspaceCapabilityOverride.created_at,
            )
            .where(WorkspaceCapabilityOverride.set_by_user_id == uid)
            .order_by(WorkspaceCapabilityOverride.created_at, WorkspaceCapabilityOverride.id),
        ),
        "audit_events_performed": _rows(
            db,
            select(
                AuditLog.id,
                AuditLog.organization_id,
                AuditLog.workspace_id,
                AuditLog.action,
                AuditLog.entity_type,
                AuditLog.entity_id,
                AuditLog.created_at,
            )
            .where(AuditLog.actor_user_id == uid)
            .order_by(AuditLog.created_at, AuditLog.id),
        ),
    }


def _organization_scope(table: Table, organization_id: str, workspace_ids: list[str]):
    if table.name == "organizations":
        return table.c.id == organization_id
    if "organization_id" in table.c:
        return table.c.organization_id == organization_id
    if "workspace_id" in table.c:
        return table.c.workspace_id.in_(workspace_ids)
    raise LookupError(f"{table.name} has no organization or workspace column")


def export_organization(db: Session, *, organization_id: str) -> dict[str, Any]:
    """Every exported table of the organization, capped per table, plus its members."""
    workspace_ids = list(
        db.execute(
            select(Workspace.id).where(Workspace.organization_id == organization_id)
        ).scalars()
    )
    tables: dict[str, dict[str, Any]] = {}
    for name, entry in sorted(TABLES.items()):
        if not entry.exported:
            continue
        table = Base.metadata.tables[name]
        columns = [c for c in table.columns if c.name not in entry.excluded_columns]
        order = [c for c in (table.c.get("created_at"), table.c.id) if c is not None]
        stmt = (
            select(*columns)
            .where(_organization_scope(table, organization_id, workspace_ids))
            .order_by(*order)
            .limit(ORGANIZATION_EXPORT_ROW_CAP + 1)
        )
        rows = _rows(db, stmt)
        truncated = len(rows) > ORGANIZATION_EXPORT_ROW_CAP
        tables[name] = {
            "rows": rows[:ORGANIZATION_EXPORT_ROW_CAP],
            "row_count": min(len(rows), ORGANIZATION_EXPORT_ROW_CAP),
            "truncated": truncated,
        }
    members = _rows(
        db,
        select(
            OrganizationMember.user_id,
            User.email,
            User.full_name,
            OrganizationMember.role,
            OrganizationMember.created_at.label("joined_at"),
        )
        .join(User, User.id == OrganizationMember.user_id)
        .where(OrganizationMember.organization_id == organization_id)
        .order_by(OrganizationMember.created_at, OrganizationMember.user_id),
    )
    return {
        "schema_version": EXPORT_SCHEMA_VERSION,
        "generated_at": _now(),
        "subject": {"type": "organization", "organization_id": organization_id},
        "row_cap_per_table": ORGANIZATION_EXPORT_ROW_CAP,
        "members": members,
        "tables": tables,
        "not_exported": sorted(n for n, e in TABLES.items() if not e.exported),
    }


__all__ = [
    "EXPORT_SCHEMA_VERSION",
    "ORGANIZATION_EXPORT_ROW_CAP",
    "export_account",
    "export_organization",
]
