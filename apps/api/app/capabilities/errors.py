"""Typed domain errors for the governed capability override service (Phase 4A-C.3).

These errors are the capabilities-local extension of the shared
:mod:`app.core.errors` taxonomy. They subclass :class:`SignalNestError` (via
``ValidationDomainError`` / ``NotFoundError``) so a future route batch (4A-C.4)
renders them through the standard error envelope with no new HTTP status codes and
no ``HTTPException`` in the service layer (plan §8.15).

This batch (4A-C.3.1) ships **types only**: the errors are defined and unit-tested,
but no service raises them yet. The read/set/clear service that raises them is a
later, separately-approved sub-batch (4A-C.3.2+).
"""

from __future__ import annotations

from app.core.errors import NotFoundError, ValidationDomainError


class CapabilityOverrideNotPermittedError(ValidationDomainError):
    """An override contradicts the registry's governance policy (plan §8.11, §8.15).

    Raised when a ``set`` would record an intent the registry forbids — most
    importantly an ``enabled=True`` override for a capability whose policy is
    ``workspace_enableable=False`` (the RSS connector). Maps to 422 with the stable,
    secret-free code ``capability_override_not_permitted`` so a caller can tell a
    policy denial apart from a generic validation error. No row is ever written.
    """

    code = "capability_override_not_permitted"


class CapabilityTenantMismatchError(NotFoundError):
    """A workspace exists but is not owned by the supplied organization (§8.12, §8.27).

    Deliberately inherits ``NotFoundError``'s ``not_found`` code and 404 status —
    it does **not** define its own code — so at the error envelope it is
    indistinguishable from a genuinely absent workspace. This prevents a caller
    from enumerating cross-tenant workspace existence ("exists but not yours" vs
    "does not exist"). The distinct *type* exists only for internal clarity,
    logging, and testing; the *envelope* leaks nothing.
    """


__all__ = [
    "CapabilityOverrideNotPermittedError",
    "CapabilityTenantMismatchError",
]
