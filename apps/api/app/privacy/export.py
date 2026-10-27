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
  derived vectors), plus the members with their email and name. It is complete (D-9f): an
  index call returns the exact ``row_count`` of every table with its first page and a
  ``next_cursor``; each table is read to its end with keyset pages ordered ``(created_at, id)``
  (at most :data:`ORGANIZATION_EXPORT_PAGE_CAP` rows a page). No snapshot isolation is
  claimed (:data:`ORGANIZATION_EXPORT_CONSISTENCY`). The account export is not paged: it holds
  one person's own rows, bounded by what one account can create (sessions are limited and
  the rest is that account's own invitations, feedback, overrides and audit events).
"""

from __future__ import annotations

import base64
import json
import re
import unicodedata
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import Table, and_, func, or_, select
from sqlalchemy.orm import Session

from app.audit.models import AuditLog
from app.auth.models import AuthSession, EmailVerificationToken, PasswordResetToken
from app.capabilities.models import WorkspaceCapabilityOverride
from app.core.errors import NotFoundError, ValidationDomainError
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
#: The organization export changed shape with D-9f (index + keyset pages): version 2.
ORGANIZATION_EXPORT_SCHEMA_VERSION = 2
#: The most rows one page may carry (``limit`` above it is refused) and the page size used
#: when the caller names none and for the first page inside the index.
ORGANIZATION_EXPORT_PAGE_CAP = 5000
ORGANIZATION_EXPORT_DEFAULT_PAGE = 1000
ORGANIZATION_EXPORT_CONSISTENCY = (
    "No snapshot isolation: each page is read on its own. A row created after a traversal "
    "started appears only if its (created_at, id) key sorts after the cursor in use; a row "
    "deleted meanwhile is absent from any page read after the deletion. Compare generated_at "
    "and row_count to see whether the data changed during the download."
)


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


def _exported_table(name: str) -> tuple[Table, list[Any]]:
    """The table and the columns the inventory lets out. An unknown or not-exported name is
    the same :class:`NotFoundError` whichever it is, so the route does not enumerate tables."""
    entry = TABLES.get(name)
    if entry is None or not entry.exported:
        raise NotFoundError("Table not found in this organization's export.")
    table = Base.metadata.tables[name]
    if table.c.get("id") is None or table.c.get("created_at") is None:
        raise LookupError(f"{name} has no (created_at, id) key to page by")
    columns = [c for c in table.columns if c.name not in entry.excluded_columns]
    if not {"id", "created_at"} <= {c.name for c in columns}:
        raise LookupError(f"{name} excludes a paging key column")
    return table, columns


def encode_cursor(table: str, created_at: datetime, row_id: str) -> str:
    raw = json.dumps([table, created_at.isoformat(), row_id], separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


_CURSOR_ALPHABET = re.compile(r"[A-Za-z0-9_-]+")
_CURSOR_MAX_TIMESTAMP = 40
_CURSOR_MAX_ID = 128


def _clean(text: str, limit: int) -> bool:
    """Non-empty, bounded, and free of every control, format, surrogate, private-use or
    unassigned character (Unicode category C*), NUL included."""
    return (
        0 < len(text) <= limit
        and "\x00" not in text
        and not any(unicodedata.category(ch).startswith("C") for ch in text)
    )


def decode_cursor(cursor: str, table: str) -> tuple[datetime, str]:
    """Validate a cursor for ``table``: URL-safe base64 of a JSON array of exactly three
    strings ``[table, created_at, id]``, the table named, the timestamp ISO-8601, both
    strings bounded and free of control characters. The cursor is validated, not signed:
    any well-formed cursor naming the table is accepted, and a forged position can only move
    the start within the requester's own rows (the tenant filter applies to every page)."""
    bad = ValidationDomainError("The cursor is not valid for this table.", code="invalid_cursor")
    if not _CURSOR_ALPHABET.fullmatch(cursor):
        raise bad
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        parts = json.loads(raw)
        if not (isinstance(parts, list) and len(parts) == 3):
            raise bad
        named, created, row_id = parts
        if named != table or not isinstance(created, str) or not isinstance(row_id, str):
            raise bad
        if not _clean(created, _CURSOR_MAX_TIMESTAMP) or not _clean(row_id, _CURSOR_MAX_ID):
            raise bad
        return datetime.fromisoformat(created), row_id
    except ValidationDomainError:
        raise
    except (ValueError, TypeError, UnicodeError):  # includes binascii.Error and JSON errors
        raise bad from None


def _page(
    db: Session,
    *,
    name: str,
    organization_id: str,
    workspace_ids: list[str],
    cursor: str | None,
    limit: int,
) -> tuple[list[dict[str, Any]], str | None]:
    """One keyset page of ``name`` for the organization: the ``limit`` rows after the cursor
    in ``(created_at, id)`` order, and the cursor of the next page (None after the last row).
    The database does the ordering; nothing is held in memory beyond the page."""
    table, columns = _exported_table(name)
    stmt = select(*columns).where(_organization_scope(table, organization_id, workspace_ids))
    workspace = table.c.get("workspace_id")
    if table.name != "organizations" and "organization_id" in table.c and workspace is not None:
        if not workspace.nullable:
            # Redundant for tenancy (a workspace belongs to one organization) and kept beside
            # the organization filter: it lets the database seek the (workspace_id, created_at)
            # index instead of scanning every tenant's rows on each page.
            stmt = stmt.where(workspace.in_(workspace_ids))
    if cursor is not None:
        after, after_id = decode_cursor(cursor, name)
        stmt = stmt.where(
            or_(
                table.c.created_at > after,
                and_(table.c.created_at == after, table.c.id > after_id),
            )
        )
    stmt = stmt.order_by(table.c.created_at, table.c.id).limit(limit + 1)
    fetched = [dict(row._mapping) for row in db.execute(stmt)]
    page, more = fetched[:limit], len(fetched) > limit
    next_cursor = None
    if more:
        last = page[-1]
        next_cursor = encode_cursor(name, last["created_at"], last["id"])
    return [{k: _jsonable(v) for k, v in row.items()} for row in page], next_cursor


def _workspace_ids(db: Session, organization_id: str) -> list[str]:
    return list(
        db.execute(
            select(Workspace.id).where(Workspace.organization_id == organization_id)
        ).scalars()
    )


def export_organization(db: Session, *, organization_id: str) -> dict[str, Any]:
    """The export index: members, then per exported table its exact ``row_count``, its first
    page and the ``next_cursor`` to follow with :func:`export_organization_page`. ``complete``
    is true when every table fits in that first page."""
    workspace_ids = _workspace_ids(db, organization_id)
    tables: dict[str, dict[str, Any]] = {}
    for name, entry in sorted(TABLES.items()):
        if not entry.exported:
            continue
        table, _ = _exported_table(name)
        total = db.execute(
            select(func.count()).select_from(table).where(
                _organization_scope(table, organization_id, workspace_ids)
            )
        ).scalar_one()
        rows, next_cursor = _page(
            db,
            name=name,
            organization_id=organization_id,
            workspace_ids=workspace_ids,
            cursor=None,
            limit=ORGANIZATION_EXPORT_DEFAULT_PAGE,
        )
        tables[name] = {"row_count": total, "rows": rows, "next_cursor": next_cursor}
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
        "schema_version": ORGANIZATION_EXPORT_SCHEMA_VERSION,
        "generated_at": _now(),
        "subject": {"type": "organization", "organization_id": organization_id},
        "page_size_default": ORGANIZATION_EXPORT_DEFAULT_PAGE,
        "page_size_cap": ORGANIZATION_EXPORT_PAGE_CAP,
        "complete": all(t["next_cursor"] is None for t in tables.values()),
        "consistency": ORGANIZATION_EXPORT_CONSISTENCY,
        "members": members,
        "tables": tables,
        "not_exported": sorted(n for n, e in TABLES.items() if not e.exported),
    }


def export_organization_page(
    db: Session,
    *,
    organization_id: str,
    table: str,
    cursor: str | None,
    limit: int,
) -> dict[str, Any]:
    """One page of one exported table of the organization, after ``cursor`` (None = first)."""
    rows, next_cursor = _page(
        db,
        name=table,
        organization_id=organization_id,
        workspace_ids=_workspace_ids(db, organization_id),
        cursor=cursor,
        limit=limit,
    )
    return {
        "schema_version": ORGANIZATION_EXPORT_SCHEMA_VERSION,
        "generated_at": _now(),
        "subject": {"type": "organization", "organization_id": organization_id},
        "table": table,
        "limit": limit,
        "rows": rows,
        "next_cursor": next_cursor,
    }


__all__ = [
    "EXPORT_SCHEMA_VERSION",
    "ORGANIZATION_EXPORT_CONSISTENCY",
    "ORGANIZATION_EXPORT_DEFAULT_PAGE",
    "ORGANIZATION_EXPORT_PAGE_CAP",
    "ORGANIZATION_EXPORT_SCHEMA_VERSION",
    "export_account",
    "export_organization",
    "export_organization_page",
]
