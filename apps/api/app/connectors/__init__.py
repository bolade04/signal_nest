"""Production scouting connectors (Phase 3B).

Phase 3B introduces the first *real* scouting connector behind the existing
``scouting_requests.connectors.Connector`` seam. This package provides the
connector-agnostic **foundation** every source must satisfy — the operational
contract mandated by ``docs/phase-3-plan.md`` Workstream A (rate limiting,
bounded retry/backoff, failure classification, source attribution, per-request
isolation and mock/sandbox support) — plus the first concrete connector
(RSS / news feeds) running in a deterministic **sandbox** by default.

Live egress (P6-DATA-1 / P6-DATA-4) is **dark by default**: ``live`` reads only feeds
named in the reviewed allow-list ``sources.APPROVED_SOURCES`` (which ships empty),
only while ``connector_rss_live_enabled`` is on and the kill switch is off, and only
through the SSRF-guarded fetcher in ``egress``. Turning it on is the operator's
first-source decision plus legal / terms-of-service sign-off (``P6-D03``, X1). The
sandbox RSS connector still reads a bundled, deterministic sample feed, and
``replay`` serves recorded feed captures from a loopback server for tests and CI.
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

__all__ = [
    "ConnectorResult",
    "ConnectorSignal",
    "FailureKind",
    "FetchFailure",
    "FetchScope",
    "SourceConnector",
]
