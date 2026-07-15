"""Connector enablement + jurisdiction policy.

A connector may run only when it is explicitly enabled for its source type. This
keeps live sources off by default (the sandbox fixture path stays authoritative)
until a product owner turns a specific connector on, satisfying the Phase 3
entry criterion "connector policy and legal feasibility confirmed".

The policy is pure and secret-free: it answers "is this connector permitted for
this request?" from configuration flags and the request's source types only.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ConnectorPolicy:
    """Immutable per-connector permission derived from settings."""

    source_type: str
    #: Whether the live connector is switched on. Off ⇒ the sandbox/fixture path
    #: is used; a live source never runs implicitly.
    enabled: bool = False
    #: Market names this connector is cleared to serve. Empty ⇒ no jurisdiction
    #: restriction beyond the request's own market scoping.
    allowed_markets: frozenset[str] = frozenset()

    def permits(self, *, source_types: tuple[str, ...], market: str | None) -> bool:
        """True when this connector may serve the given request scope."""
        if not self.enabled:
            return False
        if source_types and self.source_type not in source_types:
            return False
        if self.allowed_markets and market is not None:
            if not any(m.lower() in market.lower() for m in self.allowed_markets):
                return False
        return True
