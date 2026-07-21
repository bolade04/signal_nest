"""Operator-only capability-governance surface (Phase 4A-C.4).

Additive, operator-gated extensions of the ``/internal/system/*`` tier that expose
the merged capability control plane (registry → resolver → override service). This
sub-batch (4A-C.4.1) ships **only** the first, read-only slice:

* ``GET /internal/system/capabilities/registry`` — a pure projection of the closed
  capability registry (:mod:`app.capabilities.registry`): the governable capability
  set plus its frozen governance-policy metadata.

The registry read touches no database, consumes neither the resolver nor the
override service, flips no global flag, and mutates nothing — so every capability
remains dark and both the resolver-unconsumed and service-no-consumer guards stay
green unchanged. The effective-state read, override list, and set/clear write paths
are later sub-batches (4A-C.4.2–4.5).

Every route requires an authenticated operator (``require_operator``: 401 anonymous,
403 non-operator) and returns only bounded, secret-free governance metadata — never a
credential, URL, callable, ORM model, or mutable registry object.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.auth.dependencies import require_operator
from app.capabilities.registry import Capability, get_policy, iter_capabilities
from app.organizations.models import User

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
