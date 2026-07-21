"""Operator-only capability-governance surface (Phase 4A-C.4).

Additive, operator-gated extensions of the ``/internal/system/*`` tier that expose
the merged capability control plane (registry → resolver → override service). Three
read-only slices plus the first write path are shipped so far:

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
  dark.
* ``GET /internal/system/capabilities/overrides`` (4A-C.4.3) — a tenant-scoped,
  bounded, newest-first page of the stored per-workspace override rows, read through
  the merged governed override service
  (:func:`app.capabilities.service.list_capability_overrides`). This is the **first
  sanctioned production consumer of the override service**, and it consumes only its
  **read** plane: it lists persisted override *intent* but writes no row, opens no
  transaction, and emits no audit. The clear write path remains a later sub-batch
  (4A-C.4.5).
* ``PUT /internal/system/capabilities/overrides`` (4A-C.4.4) — records an operator's
  intent to enable/disable one ``(capability, workspace)`` override, delegating every
  gate to the merged governed override service
  (:func:`app.capabilities.service.set_capability_override`). This is the **first write
  path**, and it consumes only the service's **set** plane: authoritative tenant
  validation, deny-biased registry policy enforcement (an ``enabled=True`` for a
  non-``workspace_enableable`` capability such as RSS is refused with 422), bounded
  reason validation, and an idempotent, audited upsert under the service's
  ``SELECT … FOR UPDATE``/SAVEPOINT concurrency — all inside the request-scoped
  transaction, with the operator's id recorded as the actor. Recording override *intent*
  is **not activation**: it flips no global flag and wires the resolver into no live
  gate, so an enabled override is honored by the resolver alone while its bound global
  flag stays ``False`` and every capability remains dark.

The three reads are read-only: none opens a transaction of its own, writes a row, or
toggles a flag. The set write path opens no transaction of its own either — it uses the
request-scoped session so the override row and its audit row commit atomically at the
request boundary. The effective, overrides, and set routes authoritatively validate the
operator-supplied tenant scope (the workspace must exist and be owned by the supplied
organization) before touching override state, mapping a cross-tenant or absent
workspace to a non-enumerating 404 — never revealing whether the workspace exists.
Because all three global flags stay ``False``, every capability resolves disabled via
``global_configuration`` unless an honored per-workspace override is present — and even
then only the resolver honors it, with no live gate consuming the decision.

Every route requires an authenticated operator (``require_operator``: 401 anonymous,
403 non-operator) and returns only bounded, secret-free governance metadata. The
registry and effective projections carry no ``reason`` note, actor id, or timestamp;
the override-list projection intentionally surfaces the persisted row's non-secret
governance fields (the bounded operator ``reason`` note, the ``set_by_user_id``
attribution id, and the created/updated timestamps) so an operator can audit recorded
intent; the set projection returns only the bounded mutation summary (``created``,
``changed``, the resulting ``enabled``, and the surviving override id) — but never a
credential, URL, callable, or raw ORM model.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.auth.dependencies import require_operator
from app.capabilities.errors import CapabilityTenantMismatchError
from app.capabilities.registry import Capability, get_policy, iter_capabilities
from app.capabilities.resolver import (
    CapabilityResolution,
    DecisionSource,
    resolve_capability,
)
from app.capabilities.service import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    list_capability_overrides,
    set_capability_override,
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


class CapabilityOverrideOut(BaseModel):
    """Operator projection of one stored per-workspace override row.

    A secret-free view of :class:`app.capabilities.models.WorkspaceCapabilityOverride`:
    the persisted override *intent* an operator recorded — the typed capability, its
    boolean value, the bounded non-secret operator ``reason`` note, the ``set_by_user_id``
    attribution id, and the row's lifecycle timestamps. These governance fields are
    surfaced so an operator can audit *who* recorded *what* intent and *when*; the row
    carries no credential, URL, payload, token, or trace id. ``from_attributes`` lets the
    route validate the ORM row directly (the ``capability`` string coerces to the typed
    :class:`Capability`).
    """

    model_config = ConfigDict(from_attributes=True)

    id: str
    organization_id: str
    workspace_id: str
    capability: Capability
    enabled: bool
    reason: str | None
    set_by_user_id: str | None
    created_at: datetime
    updated_at: datetime


class CapabilityOverridePageOut(BaseModel):
    """A tenant-scoped, bounded, newest-first page of stored override rows."""

    items: list[CapabilityOverrideOut]
    total: int
    limit: int
    offset: int


class CapabilityOverrideSetIn(BaseModel):
    """Operator request to record one per-workspace override's intent.

    The operator supplies the tenant scope verbatim (``organization_id`` +
    ``workspace_id`` — never an implicit "current org"), the typed ``capability`` (an
    unknown value is rejected as 422 by the enum before any service call), the desired
    boolean ``enabled`` state, and an optional bounded operator ``reason`` note. The
    request carries no actor id: attribution is taken server-side from the authenticated
    operator, so an override can never be recorded under a spoofed identity. Deny-biased
    policy (e.g. RSS is disable-only), reason-length bounds, and tenant ownership are all
    enforced authoritatively by the merged service, not re-implemented here.
    """

    organization_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    capability: Capability
    enabled: bool
    reason: str | None = None


class CapabilityOverrideMutationOut(BaseModel):
    """Operator projection of one ``set`` mutation's bounded, secret-free summary.

    A secret-free view of :class:`app.capabilities.results.OverrideMutation`: the typed
    capability, the workspace scope, whether a new row was ``created`` (vs an in-place
    update or idempotent no-op), whether the stored state actually ``changed`` (so the
    caller can tell a real write from an idempotent re-PUT), the resulting ``enabled``
    value, and the surviving override id. Carries no ``reason`` note, actor id,
    timestamp, URL, credential, or raw ORM row.
    """

    capability: Capability
    workspace_id: str
    created: bool
    changed: bool
    enabled: bool | None
    override_id: str | None


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


@router.get("/capabilities/overrides", response_model=CapabilityOverridePageOut)
def internal_capability_overrides(
    organization_id: str = Query(..., min_length=1),
    workspace_id: str = Query(..., min_length=1),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    _operator: User = Depends(require_operator),
) -> CapabilityOverridePageOut:
    """Return a tenant-scoped, bounded page of a workspace's stored override rows.

    The route is a thin adapter over the merged governed override service: it delegates
    tenant validation, scoping, ordering, and clamping to
    :func:`list_capability_overrides` (which authoritatively confirms the workspace
    exists and is owned by ``organization_id``, then returns a newest-first,
    ``created_at DESC, id DESC`` page strictly scoped to that one workspace) and projects
    each row onto :class:`CapabilityOverrideOut`. It implements no query or scoping of its
    own, opens no transaction, writes no row, and emits no audit — this is a read.

    A cross-tenant or absent workspace is a non-enumerating 404. ``limit``/``offset`` are
    bounded by the typed query params (out-of-range → 422) and re-clamped inside the
    service, so the route can never over-fetch. With no real override row by default the
    page is empty; a persisted override appears here as recorded *intent* only — listing
    it activates nothing and flips no flag, so every capability stays dark.
    """
    page = list_capability_overrides(
        db,
        organization_id=organization_id,
        workspace_id=workspace_id,
        limit=limit,
        offset=offset,
    )
    return CapabilityOverridePageOut(
        items=[CapabilityOverrideOut.model_validate(row) for row in page.items],
        total=page.total,
        limit=page.limit,
        offset=page.offset,
    )


@router.put("/capabilities/overrides", response_model=CapabilityOverrideMutationOut)
def internal_capability_override_set(
    payload: CapabilityOverrideSetIn,
    db: Session = Depends(get_db),
    operator: User = Depends(require_operator),
) -> CapabilityOverrideMutationOut:
    """Record an operator's intent to enable/disable one per-workspace override.

    The route is a thin adapter over the merged governed override service: it delegates
    every gate to :func:`set_capability_override` — authoritative tenant validation
    (cross-tenant or absent workspace → non-enumerating 404), deny-biased registry policy
    (an ``enabled=True`` for a non-``workspace_enableable`` capability such as RSS →
    422 ``capability_override_not_permitted``, with a service-emitted ``.rejected`` audit
    and no row), bounded reason validation (over-length → 422), and an idempotent upsert
    under a ``SELECT … FOR UPDATE``/SAVEPOINT critical section — then projects the typed
    :class:`OverrideMutation` onto :class:`CapabilityOverrideMutationOut`. It implements
    no policy, persistence, or audit logic of its own and opens no transaction: the
    request-scoped session makes the override row and its single ``AuditLog`` commit
    atomically at the request boundary. An unknown ``capability`` value is rejected as 422
    by the typed enum before any service call.

    Attribution is server-side: ``actor_user_id`` is taken from the authenticated
    operator, never the request body, so no override is recorded anonymously or under a
    spoofed identity. Recording intent is **not activation** — the write flips no global
    flag and wires the resolver into no live gate, so an enabled override is honored by
    the resolver alone while its bound global flag stays ``False`` and every capability
    remains dark. The response's ``created``/``changed`` let the caller distinguish a real
    write from an idempotent re-PUT (which writes no new audit).
    """
    mutation = set_capability_override(
        db,
        organization_id=payload.organization_id,
        workspace_id=payload.workspace_id,
        capability=payload.capability,
        enabled=payload.enabled,
        actor_user_id=operator.id,
        reason=payload.reason,
    )
    return CapabilityOverrideMutationOut(
        capability=mutation.capability,
        workspace_id=mutation.workspace_id,
        created=mutation.created,
        changed=mutation.changed,
        enabled=mutation.enabled,
        override_id=mutation.override_id,
    )
