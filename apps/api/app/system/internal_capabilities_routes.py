"""Operator-only capability-governance surface (Phase 4A-C.4).

Additive, operator-gated extensions of the ``/internal/system/*`` tier that expose
the merged capability control plane (registry → resolver → override service). Two
read-only slices are shipped so far:

* ``GET /internal/system/capabilities/registry`` (4A-C.4.1) — a pure projection of
  the closed capability registry (:mod:`app.capabilities.registry`): the governable
  capability set plus its frozen governance-policy metadata. Touches no database and
  consumes neither the resolver nor the override service.
* ``GET /internal/system/capabilities/effective`` (4A-C.4.2) — the effective state of
  each ``(capability, workspace)`` pair, computed through the merged deny-biased
  resolver (:func:`app.capabilities.resolver.resolve_capability`). This is the
  **first sanctioned production consumer of the resolver**: the operator surface
  reads override *intent* and the deciding rule, but it is **not a live gate** — it
  gates no customer request and flips no global flag, so every capability remains
  dark. The override list and set/clear write paths are later sub-batches
  (4A-C.4.3–4.5) and the override service stays unconsumed here.

The effective read is read-only: it opens no transaction of its own, writes no row,
and toggles no flag. It authoritatively validates the operator-supplied tenant scope
(the workspace must exist and be owned by the supplied organization) before resolving,
mapping a cross-tenant or absent workspace to a non-enumerating 404 — never revealing
whether the workspace exists. Because all three global flags stay ``False`` and no
real override row exists, every capability resolves disabled via
``global_configuration``.

Every route requires an authenticated operator (``require_operator``: 401 anonymous,
403 non-operator) and returns only bounded, secret-free governance metadata — never a
credential, URL, callable, ORM model, override ``reason`` note, actor id, or timestamp.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth.dependencies import require_operator
from app.capabilities.errors import CapabilityTenantMismatchError
from app.capabilities.registry import Capability, get_policy, iter_capabilities
from app.capabilities.resolver import (
    CapabilityResolution,
    DecisionSource,
    resolve_capability,
)
from app.core.config import get_settings
from app.core.errors import NotFoundError
from app.db.session import get_db
from app.organizations.models import User, Workspace

router = APIRouter(prefix="/internal/system", tags=["internal"])


# --------------------------------------------------------------------------- #
# Schemas (operator-safe; bounded enums/booleans + non-secret metadata only)
# --------------------------------------------------------------------------- #
class CapabilityRegistryItemOut(BaseModel):
    """Operator projection of one capability's frozen governance policy.

    A secret-free view of :class:`app.capabilities.registry.CapabilityPolicy` — the
    typed capability, its human-safe label, the bound global-flag attribute name
    (non-secret, internal; included for operator explainability), whether an override
    may enable/disable it per workspace, and the documentation-only future-activation
    phase. Carries no credential, URL, callable, or mutable registry object.
    """

    capability: Capability
    label: str
    global_flag_attr: str
    workspace_enableable: bool
    workspace_disableable: bool
    future_activation_phase: str


class CapabilityRegistryOut(BaseModel):
    """The full closed capability registry in canonical enumeration order."""

    items: list[CapabilityRegistryItemOut]


class CapabilityEffectiveOut(BaseModel):
    """Operator projection of one resolved ``(capability, workspace)`` pair.

    A secret-free view of :class:`app.capabilities.resolver.CapabilityResolution`: the
    effective boolean a future gate would consume, the precedence rule that decided it,
    the bound global-flag value, and whether an honored per-workspace override was
    present (and its boolean). Reporting ``effective_enabled`` and ``global_flag``
    separately lets the operator distinguish persisted override *intent* from global
    *activation*. Carries no override ``reason`` note, actor id, timestamp, URL, or
    credential.
    """

    capability: Capability
    workspace_id: str
    effective_enabled: bool
    decided_by: DecisionSource
    global_flag: bool
    has_override: bool
    override_value: bool | None


class CapabilityEffectiveListOut(BaseModel):
    """Effective state for a workspace, one item per resolved capability."""

    items: list[CapabilityEffectiveOut]


# --------------------------------------------------------------------------- #
# Tenant scope (read-only; authoritative, non-enumerating)
# --------------------------------------------------------------------------- #
def _validate_effective_scope(db: Session, *, organization_id: str, workspace_id: str) -> None:
    """Confirm ``workspace_id`` exists and is owned by ``organization_id``.

    Authoritative, read-only tenant validation performed before any resolution so the
    operator surface can never read effective state across a tenant boundary. Mirrors
    the override service's :func:`_validate_tenant` semantics without importing it (the
    service stays unconsumed until 4A-C.4.3): an absent workspace and a cross-tenant
    workspace both raise a 404 that shares the generic ``not_found`` code, so a caller
    cannot distinguish "exists but not yours" from "does not exist" (non-enumeration).
    """
    workspace = db.get(Workspace, workspace_id)
    if workspace is None:
        raise NotFoundError("Workspace not found.")
    if workspace.organization_id != organization_id:
        raise CapabilityTenantMismatchError("Workspace not found.")


def _project(resolution: CapabilityResolution) -> CapabilityEffectiveOut:
    """Project a resolver :class:`CapabilityResolution` onto the operator-safe schema."""
    return CapabilityEffectiveOut(
        capability=resolution.capability,
        workspace_id=resolution.workspace_id,
        effective_enabled=resolution.effective_enabled,
        decided_by=resolution.decided_by,
        global_flag=resolution.global_flag,
        has_override=resolution.has_override,
        override_value=resolution.override_value,
    )


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@router.get("/capabilities/registry", response_model=CapabilityRegistryOut)
def internal_capability_registry(
    _operator: User = Depends(require_operator),
) -> CapabilityRegistryOut:
    """Return the closed, governable capability set with its governance metadata.

    A pure projection of :func:`iter_capabilities` + :func:`get_policy` in canonical
    declaration order. Read-only and stateless: it queries no database, consumes
    neither the resolver nor the override service, and enables/disables/mutates
    nothing — it only describes which capabilities are governable and how.
    """
    items = [
        CapabilityRegistryItemOut(
            capability=policy.capability,
            label=policy.label,
            global_flag_attr=policy.global_flag_attr,
            workspace_enableable=policy.workspace_enableable,
            workspace_disableable=policy.workspace_disableable,
            future_activation_phase=policy.future_activation_phase,
        )
        for policy in (get_policy(capability) for capability in iter_capabilities())
    ]
    return CapabilityRegistryOut(items=items)


@router.get("/capabilities/effective", response_model=CapabilityEffectiveListOut)
def internal_capability_effective(
    organization_id: str = Query(..., min_length=1),
    workspace_id: str = Query(..., min_length=1),
    capability: Capability | None = Query(default=None),
    db: Session = Depends(get_db),
    _operator: User = Depends(require_operator),
) -> CapabilityEffectiveListOut:
    """Return the effective state of each capability for one workspace.

    The route is a thin adapter over the merged deny-biased resolver: it validates the
    operator-supplied tenant scope, then delegates every precedence decision to
    :func:`resolve_capability` (safety ceiling → honored workspace override → global
    configuration → secure default) and projects each :class:`CapabilityResolution`.
    It implements no precedence of its own, opens no transaction, writes no row, and
    flips no flag.

    ``capability`` is an optional filter: absent, the response covers every capability
    in :func:`iter_capabilities` (canonical order); supplied, it narrows to that one.
    An unknown ``capability`` value is rejected as 422 by the typed enum before any
    resolution. A cross-tenant or absent workspace is a non-enumerating 404.

    With shipped defaults (all three global flags ``False``) and no real override row,
    every capability resolves disabled via ``global_configuration`` — the surface is
    dark. A persisted enable on a ``workspace_enableable`` capability would show
    ``has_override=True``/``decided_by=workspace_override``/``effective_enabled=True``
    while ``global_flag`` stays ``False``: persisted intent the resolver alone honors,
    with no live gate consuming it, so nothing is globally activated.
    """
    _validate_effective_scope(db, organization_id=organization_id, workspace_id=workspace_id)
    settings = get_settings()
    capabilities = [capability] if capability is not None else list(iter_capabilities())
    items = [
        _project(
            resolve_capability(
                session=db,
                settings=settings,
                capability=cap,
                organization_id=organization_id,
                workspace_id=workspace_id,
            )
        )
        for cap in capabilities
    ]
    return CapabilityEffectiveListOut(items=items)
