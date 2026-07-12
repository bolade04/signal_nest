"""Signal connectors.

A single ``Connector`` interface is what future live connectors (Reddit, reviews,
Google Trends, Meta Ad Library, ...) will implement. The default ``FixtureConnector``
reads from the simulated fixture library, filtered by the request's market + keywords +
enabled source types, so results stay isolated per location.
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


def get_connector() -> Connector:
    return FixtureConnector()
