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


def get_connector(
    *,
    source_types: list[str] | None = None,
    market: str | None = None,
) -> Connector:
    """Resolve the connector for a request scope.

    Returns a live connector when one is enabled and its policy permits the
    scope; otherwise the sandbox :class:`FixtureConnector`. Called with no
    arguments (the legacy signature) it always returns the fixture connector.
    """
    from app.connectors.registry import resolve_connector

    live = resolve_connector(
        source_types=tuple(source_types or ()),
        market=market,
    )
    return live or FixtureConnector()
