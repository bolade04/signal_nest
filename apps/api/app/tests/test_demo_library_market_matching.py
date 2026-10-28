"""The demo fixture library and the sandbox sample feeds match a request's market by
structured identity (P6-DATA-6), never by bidirectional substring."""

from __future__ import annotations

import pytest

from app.connectors.sample_feeds import sample_feed_covers
from app.scouting_requests.fixtures import fixtures_for_market

#: Demo market key per city, for the qualified-spelling cases below.
FIXTURE_MARKETS = {
    "Dallas": "Dallas, TX",
    "London": "London, UK",
    "Lagos": "Lagos, NG",
    "Nairobi": "Nairobi, KE",
}


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


# --------------------------------------------------------------------------- #
# Same-name and ambiguous cities (operator question, closeout-03 §3)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "market",
    [
        # Same city name, another region or country: never the demo market.
        "London, Ontario, Canada", "London, Canada", "London, OH", "London, KY, US",
        "Dallas, OR", "Dallas, TX, Mexico",
        "Lagos, PT", "Lagos, Portugal", "Lagos, Algarve, Portugal",
        "Nairobi, TZ",
        # A bare city name says nothing about which city it is: fail closed.
        "Lagos", "Nairobi", "DFW",
        # A country or region alone is not a city.
        "tx", "Texas, US", "Greater London, UK", "Lagos State, NG",
        # Two markets in one string are not one market.
        "Dallas, TX; London, UK",
        # The scope's region is required where addresses carry one (US): country-only
        # qualification is not enough to pick a Dallas.
        "Dallas, US",
    ],
)
def test_same_name_and_ambiguous_cities_are_not_covered(market):
    assert fixtures_for_market(market) == []
    assert not sample_feed_covers(market)


@pytest.mark.parametrize(
    ("market", "expected_city"),
    [
        ("Dallas, Texas", "Dallas"),
        ("Dallas TX", "Dallas"),
        ("Dallas, TX 75201", "Dallas"),
        ("London, GB", "London"),
        ("London, England", "London"),
        ("Lagos, Nigeria", "Lagos"),
        ("Nairobi, Kenya", "Nairobi"),
        ("Nairobi, Nairobi County, KE", "Nairobi"),
    ],
)
def test_qualified_spellings_of_a_demo_market_are_covered(market, expected_city):
    signals = fixtures_for_market(market)
    assert signals
    assert {s.market for s in signals} == {FIXTURE_MARKETS[expected_city]}
    assert sample_feed_covers(market)


def test_the_substring_rule_is_gone():
    """The old rule matched any market that was a substring of a key or contained one."""
    for text in ("a", ", ", "Dallas", "Dallas, TX, Mexico", "Greater London, UK"):
        assert fixtures_for_market(text) == [], text
