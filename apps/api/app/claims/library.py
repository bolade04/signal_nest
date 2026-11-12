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
  the request model's field *names* (:data:`CLAIM_CREATE_FIELDS`) in ``context``, and the new
  entry's ``text``, ``kind`` and ``jurisdiction_codes`` in ``new_state``
  (:func:`claim_audit_values`).
* :func:`claim_audit_values` — what every claims-library audit record carries of the entry
  (operator ruling D-N4 item 40, 2026-11-12: "record the previous and new claim text and kind
  under the applicable access, retention and erasure controls. Free-form claim text must not
  automatically be classified as containing no personal data."): its ``text`` and ``kind``,
  and its ``jurisdiction_codes`` (:data:`CLAIM_AUDIT_VALUES`). The codes are a coordinator
  addition beyond the ruling's minimum (DN4-A ruling A-R-3): since 5D-W1 they decide where an
  entry applies, so without them the trail could not rebuild an entry's applicability after an
  edit; they are jurisdiction codes, not personal data. A create records them as ``new_state``,
  an update as
  ``previous_state`` and ``new_state`` (both on every update, changed or not), a retirement as
  ``previous_state``, so the library a draft was reviewed against can be rebuilt from the audit
  trail after an edit (review W21-REV-R4 F-2). The text is free-form tenant text that may name
  a person: it lives in ``audit_logs`` under that table's controls (the audit read's exact
  roles, the organization export, account, invitee-email and organization erasure, the
  verifier and the ``audit_logs`` retention class), as ``docs/operations/data-inventory.md``
  states, and is never classified as containing no personal data.
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
from app.claims.engine import LibraryEntry, jurisdiction_restriction
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

#: The entry values every claims-library audit record carries (operator ruling D-N4 item 40):
#: the claim's free-form ``text`` (which may contain personal data) and its ``kind``; and its
#: ``jurisdiction_codes`` (codes only; coordinator addition A-R-3, beyond the ruling's minimum).
CLAIM_AUDIT_VALUES: tuple[str, ...] = ("jurisdiction_codes", "kind", "text")


def is_claims_steward(role: Role | str) -> bool:
    """Whether ``role`` may write the claims library (exact membership)."""
    try:
        return Role(role) in CLAIMS_STEWARD_ROLES
    except ValueError:
        return False


def claim_audit_values(entry: ClaimsLibraryEntry) -> dict[str, object]:
    """The entry's :data:`CLAIM_AUDIT_VALUES` as an audit state, exactly as stored (D-N4 item
    40; A-R-3): ``{"jurisdiction_codes": …, "kind": …, "text": …}``. ``jurisdiction_codes`` is
    the stored list (an empty list or ``None`` both mean every jurisdiction), copied so the
    state never shares the entry's list."""
    values: dict[str, object] = {}
    for name in CLAIM_AUDIT_VALUES:
        value = getattr(entry, name)
        values[name] = list(value) if isinstance(value, list) else value
    return values


def audit_claim_created(
    db: Session,
    *,
    organization_id: str,
    workspace_id: str,
    actor_user_id: str,
    entry_id: str,
) -> None:
    """Write ``claims_library_entry.created`` for one new entry, as ``/claims`` does: the
    field names in ``context`` and the entry's text and kind in ``new_state``.

    The entry is the one the caller has just added and flushed in ``db``, so it is read from
    the session (no query)."""
    entry = db.get(ClaimsLibraryEntry, entry_id)
    if entry is None:  # a programming error: the caller audits an entry it never wrote
        raise LookupError(f"claims library entry {entry_id!r} is not in this session")
    record_audit(
        db,
        organization_id=organization_id,
        workspace_id=workspace_id,
        actor_user_id=actor_user_id,
        action=AuditAction.CLAIMS_LIBRARY_ENTRY_CREATED,
        entity_type=CLAIM_ENTITY_TYPE,
        entity_id=entry_id,
        new_state=claim_audit_values(entry),
        context={"fields": list(CLAIM_CREATE_FIELDS)},
    )


def load_library(db: Session, workspace_id: str) -> list[LibraryEntry]:
    """The workspace's active (not retired) library entries, oldest first, each with its
    jurisdiction restriction (Project Phase 5D; :func:`app.claims.engine.jurisdiction_restriction`).

    Not truncated: a review that silently read only part of the library could miss a
    blocked claim (fail open)."""
    rows = db.execute(
        select(
            ClaimsLibraryEntry.id,
            ClaimsLibraryEntry.text,
            ClaimsLibraryEntry.kind,
            ClaimsLibraryEntry.retired_at,
            ClaimsLibraryEntry.jurisdiction_codes,
        )
        .where(
            ClaimsLibraryEntry.workspace_id == workspace_id,
            ClaimsLibraryEntry.retired_at.is_(None),
        )
        .order_by(ClaimsLibraryEntry.created_at, ClaimsLibraryEntry.id)
    ).all()
    return [
        LibraryEntry(
            id=r.id,
            text=r.text or "",
            kind=r.kind,
            retired_at=r.retired_at,
            jurisdiction_codes=jurisdiction_restriction(r.jurisdiction_codes),
        )
        for r in rows
    ]


__all__ = [
    "CLAIMS_STEWARD_ROLES",
    "CLAIM_AUDIT_VALUES",
    "CLAIM_CREATE_FIELDS",
    "CLAIM_ENTITY_TYPE",
    "audit_claim_created",
    "claim_audit_values",
    "is_claims_steward",
    "load_library",
]
