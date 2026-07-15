"""Production scouting connectors (Phase 3B).

Phase 3B introduces the first *real* scouting connector behind the existing
``scouting_requests.connectors.Connector`` seam. This package provides the
connector-agnostic **foundation** every source must satisfy — the operational
contract mandated by ``docs/phase-3-plan.md`` Workstream A (rate limiting,
bounded retry/backoff, failure classification, source attribution, per-request
isolation and mock/sandbox support) — plus the first concrete connector
(RSS / news feeds) running in a deterministic **sandbox** by default.

Nothing here performs live network egress. The RSS connector reads a bundled,
deterministic sample feed so the whole path (fetch → parse → normalize →
attribute) is exercised offline. Wiring a live feed provider is a follow-up
batch gated on explicit product-owner approval and connector legal feasibility
sign-off (see ``docs/phase-3-plan.md`` Phase 3 entry criteria).
"""

from __future__ import annotations

from app.connectors.base import (
    ConnectorResult,
    ConnectorSignal,
    FailureKind,
    FetchFailure,
    FetchScope,
    SourceConnector,
)
from app.connectors.live import LiveEgressDecision, decide_live_egress, live_egress_available
from app.connectors.sources import (
    ApprovalState,
    ApprovedSource,
    ApprovedSourceRegistry,
    Retention,
    get_registry,
)

__all__ = [
    "ConnectorResult",
    "ConnectorSignal",
    "FailureKind",
    "FetchFailure",
    "FetchScope",
    "SourceConnector",
    # Batch 2 safety foundation (no live egress by default)
    "ApprovalState",
    "ApprovedSource",
    "ApprovedSourceRegistry",
    "Retention",
    "get_registry",
    "LiveEgressDecision",
    "decide_live_egress",
    "live_egress_available",
]
