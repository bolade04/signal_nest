"""The demo fixture library and the sandbox sample feeds match a request's market by
structured identity (P6-DATA-6), never by bidirectional substring."""

from __future__ import annotations

import pytest

from app.connectors.sample_feeds import sample_feed_covers
from app.scouting_requests.fixtures import fixtures_for_market


@pytest.mark.parametrize(
    "market",
    ["Dallas, TX", "dallas, tx", "Dallas, TX, US", "Dallas, Texas, USA", "London, England, UK"],
)
def test_a_placeable_market_inside_a_demo_market_is_covered(market):
    assert fixtures_for_market(market)
    assert sample_feed_covers(market)


@pytest.mark.parametrize(
    "market",
    [
        "a", "ke", ", ", "Dallas", "London", "London, ON", "London, ON, CA", "Dallas, GA",
        "Berlin, DE",
    ],
)
def test_substrings_and_other_places_are_not_covered(market):
    assert fixtures_for_market(market) == []
    assert not sample_feed_covers(market)


def test_no_market_still_means_the_global_library():
    assert len(fixtures_for_market(None)) > len(fixtures_for_market("Dallas, TX"))
