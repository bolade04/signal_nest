"""Connector enablement + jurisdiction policy.

A connector may run only when it is explicitly enabled for its source type. This
keeps live sources off by default (the sandbox fixture path stays authoritative)
until a product owner turns a specific connector on, satisfying the Phase 3
entry criterion "connector policy and legal feasibility confirmed".

The policy is pure and secret-free: it answers "is this connector permitted for
this request?" from configuration flags and the request's source types only.

Market restriction fails closed (P6-DATA-6): when a connector is cleared only for
some markets, a request whose market is absent or cannot be placed is refused, and
markets are compared by structured identity ("London, UK" is not "London, ON").
A cleared market must name its country ("London, UK", not "London").
"""

from __future__ import annotations

from dataclasses import dataclass

from app.geography.markets import parse_market, text_within


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
        if self.allowed_markets:
            # Location-scoped clearance: an unknown market is never inside it.
            scopes = [parse_market(m) for m in self.allowed_markets]
            if not any(text_within(market, scope) for scope in scopes):
                return False
        return True
