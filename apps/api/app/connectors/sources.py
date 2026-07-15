"""Approved-source registry for controlled live connector egress (Phase 3B Batch 2).

A live fetch is **allowlist-only**: the connector may reach a source only when that
source is an explicit, immutable :class:`ApprovedSource` record here whose scheme +
host and exact feed URL match, whose ``enabled`` flag is on, and which carries both
``legal_review == APPROVED`` and ``owner_approval == APPROVED``. Arbitrary
customer-defined URLs are never accepted.

This registry ships **empty / all-disabled**: no source is legally or owner
approved yet (``docs/phase-3-plan.md`` Phase 3 entry criteria are unmet). The types
and gating exist so that enabling a source later is a small, auditable, reversible
change — never a code path that can be reached implicitly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from urllib.parse import urlsplit


class ApprovalState(StrEnum):
    """Tri-state legal/owner sign-off. Anything but ``APPROVED`` blocks activation."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class Retention(StrEnum):
    """How much of a fetched item may be persisted. Never a full article."""

    METADATA_ONLY = "metadata_only"
    EXCERPT = "excerpt"


@dataclass(frozen=True)
class ApprovedSource:
    """One immutable, explicitly-approved live source.

    Every field is non-secret. A source is inert unless ``enabled`` **and** both
    approvals are ``APPROVED``; :meth:`is_activatable` is the single gate.
    """

    source_id: str
    display_name: str
    host: str
    feed_urls: tuple[str, ...]
    scheme: str = "https"
    allowed_ports: frozenset[int] = frozenset({443})
    allowed_redirect_hosts: frozenset[str] = frozenset()
    enabled: bool = False
    environments: frozenset[str] = frozenset()
    tenants: frozenset[str] = frozenset()
    workspaces: frozenset[str] = frozenset()
    markets: frozenset[str] = frozenset()
    jurisdictions: frozenset[str] = frozenset()
    fetch_interval_seconds: float = 900.0
    burst_limit: int = 1
    daily_limit: int = 24
    max_response_bytes: int = 2 * 1024 * 1024
    retention: Retention = Retention.METADATA_ONLY
    attribution_required: bool = True
    legal_review: ApprovalState = ApprovalState.PENDING
    legal_reference: str | None = None
    owner_approval: ApprovalState = ApprovalState.PENDING
    notes: str = ""

    def is_activatable(self) -> bool:
        """True only when the source is enabled and fully approved."""
        return (
            self.enabled
            and self.legal_review is ApprovalState.APPROVED
            and self.owner_approval is ApprovalState.APPROVED
        )

    def permits_market(self, market: str | None) -> bool:
        """True when this source's jurisdiction allowlist admits ``market``.

        An empty ``jurisdictions`` set admits nothing (fail closed) — a live source
        must name the jurisdictions it is cleared for.
        """
        if not self.jurisdictions:
            return False
        if market is None:
            return False
        return any(j.lower() in market.lower() for j in self.jurisdictions)

    def owns_url(self, url: str) -> bool:
        """True when ``url`` exactly matches one of this source's approved feeds."""
        return url in self.feed_urls

    def redirect_host_allowed(self, host: str) -> bool:
        """True when ``host`` is this source's canonical host or an allowlisted one."""
        h = host.lower()
        return h == self.host.lower() or h in {r.lower() for r in self.allowed_redirect_hosts}


@dataclass(frozen=True)
class ApprovedSourceRegistry:
    """The set of approved live sources. Empty until owner + legal sign-off lands."""

    sources: tuple[ApprovedSource, ...] = field(default_factory=tuple)

    def activatable(self) -> tuple[ApprovedSource, ...]:
        """Approved, enabled sources only."""
        return tuple(s for s in self.sources if s.is_activatable())

    def get(self, source_id: str) -> ApprovedSource | None:
        for s in self.sources:
            if s.source_id == source_id:
                return s
        return None

    def match_url(self, url: str) -> ApprovedSource | None:
        """Return the activatable source that exactly owns ``url``, else ``None``.

        Only activatable (enabled + fully approved) sources can match, so a pending
        or disabled source can never be fetched even if its URL is passed in.
        """
        parts = urlsplit(url)
        for source in self.activatable():
            if source.scheme != parts.scheme:
                continue
            if source.host.lower() != (parts.hostname or "").lower():
                continue
            if source.owns_url(url):
                return source
        return None


#: The live-source allowlist. **Intentionally empty**: no source is legally or
#: owner approved (``docs/phase-3b/rss-source-policy.md`` §5). Populating this is a
#: deliberate, reviewed change gated on the Phase 3 entry criteria.
APPROVED_SOURCES: ApprovedSourceRegistry = ApprovedSourceRegistry(sources=())


def get_registry() -> ApprovedSourceRegistry:
    """Return the process-wide approved-source registry (empty by default)."""
    return APPROVED_SOURCES
