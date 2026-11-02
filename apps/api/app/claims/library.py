"""The workspace claims library as a protected safety control (Project Phase 5C).

The library (``claims_library``, :class:`~app.campaign_context.models.ClaimsLibraryEntry`) is
load-bearing for the 5C output-side review: its ``blocked`` entries block a generated draft
and its ``required_disclaimer`` entries must appear in it. ``kind`` is free text; the review
reads it in canonical form (:func:`app.claims.engine.canonical_kind`), so "Blocked" and the
campaign-context UI's "prohibited" block too, and an unrecognised kind does nothing. This
module holds the pieces every writer and reader of the library shares, so there is one
authority for each:

* :data:`CLAIMS_STEWARD_ROLES` — the exact roles that may write the library (W21 CF-2:
  ``OWNER``, ``ADMIN``, ``COMPLIANCE_REVIEWER``; no rank inheritance). The ``/claims`` routes
  (:mod:`app.campaign_context.routes`) and the onboarding claims input
  (:func:`app.brands.service.run_onboarding`, coordinator ruling R1) both gate on it.
* :func:`audit_claim_created` — the ``claims_library_entry.created`` audit record, in the
  shape the ``/claims`` create route writes: entity ``claims_library_entry``, the entry id,
  and the request model's field *names* (:data:`CLAIM_CREATE_FIELDS`), never their values.
* :func:`load_library` — the workspace's entries as the engine reads them
  (:class:`app.claims.engine.LibraryEntry`). Retired entries are not loaded, and the engine
  ignores any that reach it (:func:`app.claims.engine.active_entries`): two independent
  guards, each tested on its own.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.actions import AuditAction
from app.audit.service import record_audit
from app.campaign_context.models import ClaimsLibraryEntry
from app.campaign_context.schemas import ClaimIn
from app.claims.engine import LibraryEntry
from app.core.enums import Role

#: The claims library's stewards: exact roles, no rank inheritance (W21 CF-2).
CLAIMS_STEWARD_ROLES: frozenset[Role] = frozenset(
    {Role.OWNER, Role.ADMIN, Role.COMPLIANCE_REVIEWER}
)

#: The audit ``entity_type`` of a claims-library mutation.
CLAIM_ENTITY_TYPE = "claims_library_entry"

#: The field names a claims-library create records: the ``/claims`` create body's fields
#: (``ClaimIn.model_dump()`` always carries every one), sorted.
CLAIM_CREATE_FIELDS: tuple[str, ...] = tuple(sorted(ClaimIn.model_fields))


def is_claims_steward(role: Role | str) -> bool:
    """Whether ``role`` may write the claims library (exact membership)."""
    try:
        return Role(role) in CLAIMS_STEWARD_ROLES
    except ValueError:
        return False


def audit_claim_created(
    db: Session,
    *,
    organization_id: str,
    workspace_id: str,
    actor_user_id: str,
    entry_id: str,
) -> None:
    """Write ``claims_library_entry.created`` for one new entry, as ``/claims`` does."""
    record_audit(
        db,
        organization_id=organization_id,
        workspace_id=workspace_id,
        actor_user_id=actor_user_id,
        action=AuditAction.CLAIMS_LIBRARY_ENTRY_CREATED,
        entity_type=CLAIM_ENTITY_TYPE,
        entity_id=entry_id,
        context={"fields": list(CLAIM_CREATE_FIELDS)},
    )


def load_library(db: Session, workspace_id: str) -> list[LibraryEntry]:
    """The workspace's active (not retired) library entries, oldest first.

    Not truncated: a review that silently read only part of the library could miss a
    blocked claim (fail open)."""
    rows = db.execute(
        select(
            ClaimsLibraryEntry.id,
            ClaimsLibraryEntry.text,
            ClaimsLibraryEntry.kind,
            ClaimsLibraryEntry.retired_at,
        )
        .where(
            ClaimsLibraryEntry.workspace_id == workspace_id,
            ClaimsLibraryEntry.retired_at.is_(None),
        )
        .order_by(ClaimsLibraryEntry.created_at, ClaimsLibraryEntry.id)
    ).all()
    return [
        LibraryEntry(id=r.id, text=r.text or "", kind=r.kind, retired_at=r.retired_at)
        for r in rows
    ]


__all__ = [
    "CLAIMS_STEWARD_ROLES",
    "CLAIM_CREATE_FIELDS",
    "CLAIM_ENTITY_TYPE",
    "audit_claim_created",
    "is_claims_steward",
    "load_library",
]
