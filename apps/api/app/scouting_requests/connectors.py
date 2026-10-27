"""Signal connectors.

A single ``Connector`` interface is what live connectors (Phase 3B onward)
implement. The default ``FixtureConnector`` reads from the simulated fixture
library, filtered by the request's market + keywords + enabled source types, so
results stay isolated per location.

Since Phase 3B, :func:`get_connector` consults the connector registry
(``app.connectors``): when a live connector is enabled *and* its policy permits
the request scope, that connector is used; otherwise this falls back to the
sandbox ``FixtureConnector``, so default behaviour is unchanged.
"""

from __future__ import annotations

from typing import Protocol

from app.connectors.fetch_limits import FetchOutcome, SourceFetchReport
from app.scouting_requests.fixtures import FixtureSignal, fixtures_for_market


class Connector(Protocol):
    name: str

    def fetch(
        self, *, market: str | None, keywords: list[str], source_types: list[str]
    ) -> list[FixtureSignal]:
        ...


class FixtureConnector:
    name = "fixture"
    is_simulated = True

    def fetch(
        self, *, market: str | None, keywords: list[str], source_types: list[str]
    ) -> list[FixtureSignal]:
        candidates = fixtures_for_market(market)
        allowed = set(source_types) if source_types else None
        kw = [k.lower() for k in keywords]

        results: list[FixtureSignal] = []
        for signal in candidates:
            if allowed is not None and signal.source_type not in allowed:
                continue
            if kw:
                haystack = (signal.content + " " + " ".join(signal.topics)).lower()
                if not any(k in haystack for k in kw):
                    # Keep spam/noise fixtures so the noise filter is exercised.
                    if "spam" not in signal.topics:
                        continue
            results.append(signal)
        return results

    def fetch_reported(
        self, *, market: str | None, keywords: list[str], source_types: list[str]
    ) -> FetchOutcome:
        results = self.fetch(market=market, keywords=keywords, source_types=source_types)
        # The demo library declares no fetch limit: nothing it holds is ever clipped.
        report = SourceFetchReport(
            source_key=self.name,
            max_items=None,
            fetched=len(results),
            eligible_seen=len(results),
            clipped_by_fetch_limit=False,
        )
        return FetchOutcome(signals=results, reports=[report])


def fetch_with_reports(
    connector,
    *,
    market: str | None,
    keywords: list[str],
    source_types: list[str],
) -> FetchOutcome:
    """The connector's signals plus its per-source fetch-limit reports (#268).

    A connector without ``fetch_reported`` (a test double, a future adapter) is reported
    as one source with no declared fetch limit, so the run's stats still say so rather
    than staying silent.
    """
    reported = getattr(connector, "fetch_reported", None)
    if callable(reported):
        return reported(market=market, keywords=keywords, source_types=source_types)
    signals = connector.fetch(market=market, keywords=keywords, source_types=source_types)
    report = SourceFetchReport(
        source_key=getattr(connector, "name", "unknown"),
        max_items=None,
        fetched=len(signals),
        eligible_seen=len(signals),
        clipped_by_fetch_limit=False,
    )
    return FetchOutcome(signals=signals, reports=[report])


def _rss_enabled_for(db, *, organization_id: str, workspace_id: str) -> bool:
    """P6-CAP-1: the one RSS enforcement decision — the capability resolver's.

    Safety ceiling → workspace override → global flag → secure default, so a workspace
    *disable* override (or the ceiling) turns RSS off for that workspace. RSS is not
    ``workspace_enableable``, so an enable override resolves to the secure default and
    never turns it on. The operator console's effective view resolves the same pair
    with the same function. A resolver failure propagates (the run fails and is
    retried); it is never coerced into a decision.
    """
    from app.capabilities.registry import Capability
    from app.capabilities.resolver import resolve_capability
    from app.core.config import get_settings

    return resolve_capability(
        session=db,
        settings=get_settings(),
        capability=Capability.CONNECTOR_RSS,
        organization_id=organization_id,
        workspace_id=workspace_id,
    ).effective_enabled


def get_connector(
    *,
    source_types: list[str] | None = None,
    market: str | None = None,
    db=None,
    organization_id: str | None = None,
    workspace_id: str | None = None,
) -> Connector:
    """Resolve the connector for a request scope.

    Returns a live connector when one is enabled and its policy permits the
    scope; otherwise the sandbox :class:`FixtureConnector`. Called with no
    arguments (the legacy signature) it always returns the fixture connector.

    "Enabled" is :func:`_rss_enabled_for` for the request's workspace (P6-CAP-1).
    Without a session and a full tenant scope there is no workspace to decide for,
    so RSS counts as disabled — deny-biased, never the raw global flag.
    """
    from app.connectors.registry import resolve_connector

    rss_enabled = (
        db is not None
        and organization_id is not None
        and workspace_id is not None
        and _rss_enabled_for(
            db, organization_id=organization_id, workspace_id=workspace_id
        )
    )
    live = resolve_connector(
        source_types=tuple(source_types or ()),
        market=market,
        rss_enabled=rss_enabled,
    )
    return live or FixtureConnector()
