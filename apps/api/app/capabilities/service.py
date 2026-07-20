"""Governed workspace capability override service — read plane (Phase 4A-C.3.2).

This module is the single, transaction-participating access path for
:class:`~app.capabilities.models.WorkspaceCapabilityOverride` rows. Phase 4A-C.3.2
ships **only the read plane**: authoritative tenant validation plus the two
read-back accessors (:func:`get_capability_override`,
:func:`list_capability_overrides`). The write plane (``set``/``clear``), policy
enforcement, idempotent upsert, concurrency locking, and audit emission are later,
separately-approved sub-batches (4A-C.3.3+), so nothing here mutates a row, emits
an audit entry, or touches the caller's transaction boundary (plan §8.8, §8.32).

Conventions mirror the established house style (``feedback/service.py``,
``scouting_requests/run_history.py``): explicit ``db: Session`` first positional,
keyword-only domain args, caller-owns-transaction, raise ``SignalNestError``
subclasses (never ``HTTPException``), and return ORM rows / typed result models.

Tenant validation here is deliberately *authoritative* — stronger than the
resolver's in-memory org match: every operation loads the concrete
:class:`~app.organizations.models.Workspace` and confirms ownership before reading
an override, so a caller can never read across a tenant boundary. A cross-tenant
workspace is reported as a 404-mapped, non-enumerating
:class:`~app.capabilities.errors.CapabilityTenantMismatchError` so it is
indistinguishable from a genuinely absent workspace (plan §8.12, §8.27).

This batch ships with **no live consumer**: no route imports it, the resolver stays
byte-for-byte unchanged and unconsumed, no global flag flips, and no real override
row exists. Every capability remains dark.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.capabilities.errors import CapabilityTenantMismatchError
from app.capabilities.models import WorkspaceCapabilityOverride
from app.capabilities.registry import Capability
from app.capabilities.results import OverridePage
from app.core.errors import NotFoundError
from app.organizations.models import Workspace

#: Pagination contract, mirroring ``scouting_requests/run_history.py``. Re-clamped
#: in the service so a direct call can never over-fetch or use a negative offset.
DEFAULT_LIMIT = 20
MAX_LIMIT = 100


def _clamp_limit(limit: int) -> int:
    return max(1, min(MAX_LIMIT, int(limit)))


def _clamp_offset(offset: int) -> int:
    return max(0, int(offset))


def _validate_tenant(db: Session, *, organization_id: str, workspace_id: str) -> None:
    """Authoritatively confirm ``workspace_id`` exists and is owned by the org.

    Loads the concrete :class:`Workspace` and checks ownership in code (workspaces
    carry no ``(id, organization_id)`` composite key, so no composite FK is
    available). Raises before any override is read:

    * ``workspace is None`` → :class:`NotFoundError` (the workspace does not exist).
    * ``workspace.organization_id != organization_id`` →
      :class:`CapabilityTenantMismatchError`, which is 404-mapped and shares the
      generic ``not_found`` code so a caller cannot distinguish "exists but not
      yours" from "does not exist" (non-enumeration, plan §8.12, §8.27).

    Read-only: it neither mutates nor flushes.
    """
    workspace = db.get(Workspace, workspace_id)
    if workspace is None:
        raise NotFoundError("Workspace not found.")
    if workspace.organization_id != organization_id:
        raise CapabilityTenantMismatchError("Workspace not found.")


def get_capability_override(
    db: Session,
    *,
    organization_id: str,
    workspace_id: str,
    capability: Capability,
) -> WorkspaceCapabilityOverride | None:
    """Return the tenant-validated override row for a scope, or ``None`` (plan §8.8).

    Performs authoritative tenant validation (§8.12), then one indexed
    ``session.scalar(select(...))`` on the unique ``(workspace_id, capability)`` key
    (the unique constraint guarantees at most one row). Returns the ORM row or
    ``None`` when no override exists. Read-only: it never flushes and never raises
    for a missing override (only for a missing/cross-tenant workspace).
    """
    _validate_tenant(db, organization_id=organization_id, workspace_id=workspace_id)
    return db.scalar(
        select(WorkspaceCapabilityOverride).where(
            WorkspaceCapabilityOverride.workspace_id == workspace_id,
            WorkspaceCapabilityOverride.capability == capability.value,
        )
    )


def list_capability_overrides(
    db: Session,
    *,
    organization_id: str,
    workspace_id: str,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> OverridePage:
    """Return a tenant-scoped, clamped, ordered page of a workspace's overrides.

    Performs authoritative tenant validation (§8.12), then one bounded ``count(*)``
    plus one bounded page query ordered ``created_at DESC, id DESC`` for a
    deterministic, stable page under equal timestamps (mirrors ``run_history.py``).
    Scoped strictly to the one workspace, so it can never return another
    workspace's rows. Read-only: no flush, no mutation, no audit.
    """
    _validate_tenant(db, organization_id=organization_id, workspace_id=workspace_id)
    limit = _clamp_limit(limit)
    offset = _clamp_offset(offset)

    base = select(WorkspaceCapabilityOverride).where(
        WorkspaceCapabilityOverride.workspace_id == workspace_id,
    )
    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    rows = tuple(
        db.execute(
            base.order_by(
                WorkspaceCapabilityOverride.created_at.desc(),
                WorkspaceCapabilityOverride.id.desc(),
            )
            .limit(limit)
            .offset(offset)
        ).scalars()
    )
    return OverridePage(items=rows, total=total, limit=limit, offset=offset)


__all__ = [
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "get_capability_override",
    "list_capability_overrides",
]
