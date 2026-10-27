"""Signal connectors and per-run source selection.

A single ``Connector`` interface is what live connectors (Phase 3B onward)
implement. The default ``FixtureConnector`` reads from the simulated fixture
library, filtered by the request's market + keywords + enabled source types, so
results stay isolated per location.

:func:`fetch_signals` is what a scout run calls. It picks the run's sources in a
fixed order and always says why it holds what it holds (P6-DATA-2):

1. approved **live** feeds for the market (``app.connectors.live`` — dark by
   default) → ``covered/live_sources``, or ``unavailable/sources_unavailable`` when
   every live source failed;
2. the **sandbox** RSS feed, when ``connector_rss_enabled`` and its policy admit the
   request and the sandbox has a feed for the market → ``covered/sandbox_feed``;
3. the simulated **demo library** for the four demo markets, unless
   ``connector_fixture_fallback_enabled`` is off → ``covered/fixture_library``;
4. otherwise ``not_covered/no_source_for_market`` with no signals — an explicit,
   user-visible outcome instead of a silent empty result.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from app.connectors.base import FetchScope
from app.connectors.coverage import Coverage, CoverageReason
from app.core.config import Settings, get_settings
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
    """Resolve the sandbox connector for a request scope.

    Returns the sandbox RSS connector when it is enabled and its policy permits the
    scope; otherwise the :class:`FixtureConnector`. Called with no arguments (the
    legacy signature) it always returns the fixture connector. Live feeds are chosen
    by :func:`fetch_signals`, never here.

    "Enabled" is :func:`_rss_enabled_for` for the request's workspace (P6-CAP-1).
    Without a session and a full tenant scope there is no workspace to decide for,
    so RSS counts as disabled - deny-biased, never the raw global flag.
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


@dataclass
class SignalBatch:
    """The signals for one scout run and the coverage outcome that explains them."""

    signals: list = field(default_factory=list)
    coverage: Coverage = field(
        default_factory=lambda: Coverage(CoverageReason.NO_SOURCE_FOR_MARKET, is_simulated=False)
    )


def fetch_signals(
    *,
    market: str | None,
    keywords: list[str],
    source_types: list[str],
    rss_enabled: bool,
    takedowns: frozenset[str] = frozenset(),
    settings: Settings | None = None,
) -> SignalBatch:
    """Select the run's sources (see the module doc) and collect their signals."""
    from app.connectors.live import live_connector_for
    from app.connectors.registry import resolve_connector
    from app.connectors.sample_feeds import sample_feed_covers

    settings = settings or get_settings()
    types = tuple(source_types or ())
    scope = FetchScope(market=market, keywords=tuple(keywords or ()), source_types=types)

    live = live_connector_for(
        market=market, source_types=types, takedowns=takedowns, settings=settings
    )
    if live is not None:
        result = live.collect(scope)
        total, failed = len(live.targets), len(live.failed_sources)
        reason = (
            CoverageReason.SOURCES_UNAVAILABLE
            if failed == total
            else CoverageReason.LIVE_SOURCES
        )
        return SignalBatch(
            signals=result.signals,
            coverage=Coverage(
                reason,
                is_simulated=any(s.is_simulated for s in result.signals),
                sources=total,
                sources_failed=failed,
            ),
        )

    sandbox = resolve_connector(
        source_types=types, market=market, rss_enabled=rss_enabled, settings=settings
    )
    if sandbox is not None and sample_feed_covers(market):
        return SignalBatch(
            signals=sandbox.collect(scope).signals,
            coverage=Coverage(CoverageReason.SANDBOX_FEED, is_simulated=True),
        )

    if settings.connector_fixture_fallback_enabled and fixtures_for_market(market):
        return SignalBatch(
            signals=FixtureConnector().fetch(
                market=market, keywords=list(keywords or ()), source_types=list(types)
            ),
            coverage=Coverage(CoverageReason.FIXTURE_LIBRARY, is_simulated=True),
        )

    return SignalBatch()
