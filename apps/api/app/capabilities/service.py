"""Governed workspace capability override service — read + set + clear planes.

This module is the single, transaction-participating access path for
:class:`~app.capabilities.models.WorkspaceCapabilityOverride` rows. Phase 4A-C.3.2
shipped the read plane (:func:`get_capability_override`,
:func:`list_capability_overrides`); Phase 4A-C.3.3 added the **set plane**
(:func:`set_capability_override`): authoritative tenant validation, deny-biased
registry-derived policy enforcement, bounded reason validation, an idempotent
insert-or-update upsert with a ``begin_nested`` SAVEPOINT race backstop, and audit
emission that shares the caller's transaction (plan §8.9, §8.11, §8.17, §8.19–§8.21).
Phase 4A-C.3.4 adds the **clear plane** (:func:`clear_capability_override`): tenant
validation, an idempotent delete-or-no-op, and a ``.cleared`` audit — clearing is
policy-free and reason-free, and after a clear no override remains (plan §8.10).

Deferred to a later, separately-approved sub-batch (plan §8.32): the
``SELECT … FOR UPDATE`` workspace lock plus the PostgreSQL-gated concurrency test
(4A-C.3.5). This batch adds no route, no resolver wiring, no flag flip, and no
migration.

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
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit.service import record_audit
from app.capabilities.errors import (
    CapabilityOverrideNotPermittedError,
    CapabilityTenantMismatchError,
)
from app.capabilities.models import WorkspaceCapabilityOverride
from app.capabilities.registry import Capability, get_policy
from app.capabilities.results import OverrideMutation, OverridePage
from app.core.errors import NotFoundError, ValidationDomainError
from app.core.logging import get_logger, log_event
from app.organizations.models import Workspace

logger = get_logger("signalnest.capabilities")

#: Pagination contract, mirroring ``scouting_requests/run_history.py``. Re-clamped
#: in the service so a direct call can never over-fetch or use a negative offset.
DEFAULT_LIMIT = 20
MAX_LIMIT = 100

#: Maximum length of the optional operator ``reason`` note (plan §8.17). A
#: defensive, non-secret cap so the note stays a short justification, not a payload.
MAX_REASON_LEN = 500


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


# --------------------------------------------------------------------------- #
# Set plane (4A-C.3.3)
# --------------------------------------------------------------------------- #
def _normalize_reason(reason: str | None) -> str | None:
    """Strip the optional operator note; blank → ``None``; over-length → reject.

    Whitespace-only notes collapse to ``None`` (no note). A note longer than
    :data:`MAX_REASON_LEN` raises :class:`ValidationDomainError` before any row is
    written (plan §8.17). The note is a non-secret justification, never logged.
    """
    if reason is None:
        return None
    stripped = reason.strip()
    if not stripped:
        return None
    if len(stripped) > MAX_REASON_LEN:
        raise ValidationDomainError("Override reason exceeds the maximum length.")
    return stripped


def _state(capability: Capability, enabled: bool, override_id: str | None) -> dict:
    """Bounded, secret-free audit state dict — only ``{capability, enabled, id}``."""
    return {
        "capability": capability.value,
        "enabled": enabled,
        "override_id": override_id,
    }


def _load_override_row(
    db: Session, *, workspace_id: str, capability: Capability
) -> WorkspaceCapabilityOverride | None:
    """Load the single ``(workspace_id, capability)`` override row, or ``None``.

    The unique constraint guarantees at most one. Kept as a small internal helper
    (distinct from :func:`get_capability_override`, which re-validates tenancy) so
    the set path reads without re-loading the workspace and so the SAVEPOINT
    race-backstop re-read is expressed once.
    """
    return db.scalar(
        select(WorkspaceCapabilityOverride).where(
            WorkspaceCapabilityOverride.workspace_id == workspace_id,
            WorkspaceCapabilityOverride.capability == capability.value,
        )
    )


def _apply_update(
    db: Session,
    row: WorkspaceCapabilityOverride,
    *,
    organization_id: str,
    workspace_id: str,
    capability: Capability,
    enabled: bool,
    actor_user_id: str,
    reason: str | None,
) -> OverrideMutation:
    """Update an existing override in place, or return an idempotent no-op.

    If the stored ``(enabled, reason)`` already match the request, nothing is
    mutated and no audit entry is written (``changed=False``, plan §8.9/D3).
    Otherwise the row's ``enabled``/``reason``/``set_by_user_id`` are updated, the
    change is flushed, and a ``.updated`` audit entry carrying the bounded
    ``previous_state``/``new_state`` is emitted within the caller's transaction.
    """
    if row.enabled == enabled and row.reason == reason:
        log_event(
            logger,
            "workspace_capability_override_set",
            outcome="success",
            workspace_id=workspace_id,
            capability=capability.value,
            enabled=enabled,
            created=False,
            changed=False,
        )
        return OverrideMutation(
            capability=capability,
            workspace_id=workspace_id,
            created=False,
            changed=False,
            enabled=row.enabled,
            override_id=row.id,
        )

    previous = _state(capability, row.enabled, row.id)
    row.enabled = enabled
    row.reason = reason
    row.set_by_user_id = actor_user_id
    db.flush()
    record_audit(
        db,
        organization_id=organization_id,
        workspace_id=workspace_id,
        actor_user_id=actor_user_id,
        action="workspace_capability_override.updated",
        entity_type="workspace_capability_override",
        entity_id=row.id,
        reason=reason,
        previous_state=previous,
        new_state=_state(capability, enabled, row.id),
    )
    log_event(
        logger,
        "workspace_capability_override_set",
        outcome="success",
        workspace_id=workspace_id,
        capability=capability.value,
        enabled=enabled,
        created=False,
        changed=True,
    )
    return OverrideMutation(
        capability=capability,
        workspace_id=workspace_id,
        created=False,
        changed=True,
        enabled=enabled,
        override_id=row.id,
    )


def set_capability_override(
    db: Session,
    *,
    organization_id: str,
    workspace_id: str,
    capability: Capability,
    enabled: bool,
    actor_user_id: str,
    reason: str | None = None,
) -> OverrideMutation:
    """Record an operator's intent to enable/disable ``capability`` for a workspace.

    Ordered, fail-closed gates (plan §8.9): authoritative tenant validation
    (§8.12), deny-biased registry policy enforcement (§8.11 — an ``enabled=True``
    for a non-``workspace_enableable`` capability such as RSS is rejected before any
    row or mutation-audit is written; the attempt is recorded as ``.rejected``),
    bounded reason validation (§8.17), then an idempotent insert-or-update upsert
    with a ``begin_nested`` SAVEPOINT race backstop (§8.21). Mutation and audit
    share the caller's transaction; the service flushes but never commits (§8.20,
    §8.23). ``actor_user_id`` is required so no override is written anonymously
    (§8.16); the service does not load the user row or check operator rights (a
    route concern). Returns a typed :class:`OverrideMutation`.
    """
    _validate_tenant(db, organization_id=organization_id, workspace_id=workspace_id)

    policy = get_policy(capability)
    if (enabled and not policy.workspace_enableable) or (
        not enabled and not policy.workspace_disableable
    ):
        record_audit(
            db,
            organization_id=organization_id,
            workspace_id=workspace_id,
            actor_user_id=actor_user_id,
            action="workspace_capability_override.rejected",
            entity_type="workspace_capability_override",
            entity_id=None,
            new_state=_state(capability, enabled, None),
        )
        log_event(
            logger,
            "workspace_capability_override_set",
            outcome="rejected",
            workspace_id=workspace_id,
            capability=capability.value,
            enabled=enabled,
        )
        raise CapabilityOverrideNotPermittedError(
            "This capability cannot be overridden to the requested state per policy."
        )

    reason = _normalize_reason(reason)

    existing = _load_override_row(db, workspace_id=workspace_id, capability=capability)
    if existing is not None:
        return _apply_update(
            db,
            existing,
            organization_id=organization_id,
            workspace_id=workspace_id,
            capability=capability,
            enabled=enabled,
            actor_user_id=actor_user_id,
            reason=reason,
        )

    row = WorkspaceCapabilityOverride(
        organization_id=organization_id,
        workspace_id=workspace_id,
        capability=capability.value,
        enabled=enabled,
        set_by_user_id=actor_user_id,
        reason=reason,
    )
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError:
        # A concurrent insert won the unique-constraint race. Roll back only the
        # savepoint (done by the context manager), re-read the now-present row, and
        # update it in place — never a duplicate row, never a lost update (§8.21).
        existing = _load_override_row(db, workspace_id=workspace_id, capability=capability)
        if existing is None:  # pragma: no cover - constraint fired but row absent
            raise
        return _apply_update(
            db,
            existing,
            organization_id=organization_id,
            workspace_id=workspace_id,
            capability=capability,
            enabled=enabled,
            actor_user_id=actor_user_id,
            reason=reason,
        )

    record_audit(
        db,
        organization_id=organization_id,
        workspace_id=workspace_id,
        actor_user_id=actor_user_id,
        action="workspace_capability_override.created",
        entity_type="workspace_capability_override",
        entity_id=row.id,
        reason=reason,
        new_state=_state(capability, enabled, row.id),
    )
    log_event(
        logger,
        "workspace_capability_override_set",
        outcome="success",
        workspace_id=workspace_id,
        capability=capability.value,
        enabled=enabled,
        created=True,
        changed=True,
    )
    return OverrideMutation(
        capability=capability,
        workspace_id=workspace_id,
        created=True,
        changed=True,
        enabled=enabled,
        override_id=row.id,
    )


# --------------------------------------------------------------------------- #
# Clear plane (4A-C.3.4)
# --------------------------------------------------------------------------- #
def clear_capability_override(
    db: Session,
    *,
    organization_id: str,
    workspace_id: str,
    capability: Capability,
    actor_user_id: str,
) -> OverrideMutation:
    """Remove any override for ``capability`` in a workspace (plan §8.10).

    Ordered gates: authoritative tenant validation (§8.12), then one indexed lookup
    of the ``(workspace_id, capability)`` row. Clearing is deny-biased and carries
    **no** policy gate — removing an override can only relax toward the secure
    default, so it is always permitted — and takes **no** ``reason`` (§8.7). When no
    override exists the call is an idempotent success that writes nothing and emits
    **no** audit (clearing an absent override is a benign no-op; absence already
    resolves disabled). When one exists, its prior state is captured, the row is
    deleted and flushed, and a ``.cleared`` audit sharing the caller's transaction
    records the removal (§8.19–§8.20). The service flushes but never commits (§8.23);
    ``actor_user_id`` is required so no clear is anonymous (§8.16). Returns a typed
    :class:`OverrideMutation` with ``enabled``/``override_id`` set to ``None`` (no
    override remains). The ``SELECT … FOR UPDATE`` workspace lock is deferred to
    4A-C.3.5 (§8.22, §8.32).
    """
    _validate_tenant(db, organization_id=organization_id, workspace_id=workspace_id)

    existing = _load_override_row(db, workspace_id=workspace_id, capability=capability)
    if existing is None:
        log_event(
            logger,
            "workspace_capability_override_clear",
            outcome="success",
            workspace_id=workspace_id,
            capability=capability.value,
            created=False,
            changed=False,
        )
        return OverrideMutation(
            capability=capability,
            workspace_id=workspace_id,
            created=False,
            changed=False,
            enabled=None,
            override_id=None,
        )

    previous = _state(capability, existing.enabled, existing.id)
    db.delete(existing)
    db.flush()
    record_audit(
        db,
        organization_id=organization_id,
        workspace_id=workspace_id,
        actor_user_id=actor_user_id,
        action="workspace_capability_override.cleared",
        entity_type="workspace_capability_override",
        entity_id=previous["override_id"],
        previous_state=previous,
    )
    log_event(
        logger,
        "workspace_capability_override_clear",
        outcome="success",
        workspace_id=workspace_id,
        capability=capability.value,
        created=False,
        changed=True,
    )
    return OverrideMutation(
        capability=capability,
        workspace_id=workspace_id,
        created=False,
        changed=True,
        enabled=None,
        override_id=None,
    )


__all__ = [
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "MAX_REASON_LEN",
    "clear_capability_override",
    "get_capability_override",
    "list_capability_overrides",
    "set_capability_override",
]
