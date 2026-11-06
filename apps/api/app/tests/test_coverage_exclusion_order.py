"""Phase 5D exit criterion 5 (engine half): exclusions are evaluated before any
short-circuit in ``market_in_coverage``, on both limbs the CF-3 reviews found
(``online_global`` and ``coverage_type == "online"``), and normalised matching has no
substring false positives ("York" is not "New York").

The last section characterises the behaviour change exactly against the CF-3 function
(``app.tests._legacy_coverage``): over a grid of rules and markets, the only decisions that
change are online-rule decisions on a market one of the rule's exclusions covers, and each
changes from admit to deny. The pipeline-level bound (exit criterion 8(b)) is
``test_jurisdiction_scoring_snapshot``.
"""

from __future__ import annotations

import itertools

import pytest

from app.geography.engine import (
    CoverageRule,
    coverage_scopes,
    is_excluded,
    market_in_coverage,
    resolve_geo,
)
from app.geography.markets import market_from_parts
from app.tests._legacy_coverage import legacy_market_in_coverage

DALLAS = market_from_parts(city="Dallas", region="Texas", country="United States")
LONDON = market_from_parts(city="London", region="England", country="United Kingdom")
LAGOS = market_from_parts(city="Lagos", region="Lagos", country="Nigeria")
NAIROBI = market_from_parts(city="Nairobi", region="Nairobi", country="Kenya")

FOUR_MARKETS = ["Dallas, TX", "London, UK", "Lagos, NG", "Nairobi, KE"]


def _online_rules(excluded: tuple[str, ...], home=None) -> list[CoverageRule]:
    """Both short-circuit limbs, and the two together."""
    return [
        CoverageRule(
            coverage_type="radius", online_global=True, excluded_markets=excluded, home_market=home
        ),
        CoverageRule(coverage_type="online", excluded_markets=excluded, home_market=home),
        CoverageRule(
            coverage_type="online", online_global=True, excluded_markets=excluded, home_market=home
        ),
    ]


_LIMB_IDS = ["online_global", "coverage_type=online", "both"]


# --------------------------------------------------------------------------- #
# An excluded jurisdiction is denied even under online coverage
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("limb", range(3), ids=_LIMB_IDS)
@pytest.mark.parametrize("excluded", FOUR_MARKETS)
def test_an_excluded_market_is_denied_under_online_coverage(limb, excluded):
    rule = _online_rules((excluded,))[limb]
    assert market_in_coverage(rule, excluded) is False
    # Every other seeded market is still admitted: the exclusion removes only its market.
    for market in FOUR_MARKETS:
        if market != excluded:
            assert market_in_coverage(rule, market) is True, market
    # The CF-3 function admitted it: this is the bypass being cured.
    assert legacy_market_in_coverage(rule, excluded) is True


@pytest.mark.parametrize("limb", range(3), ids=_LIMB_IDS)
def test_an_excluded_country_or_state_is_denied_under_online_coverage(limb):
    rule = _online_rules(("Texas", "Kenya"))[limb]
    for market in ("Dallas, TX", "Austin, Texas", "Nairobi, KE", "Mombasa, Kenya"):
        assert market_in_coverage(rule, market) is False, market
    for market in ("London, UK", "Lagos, NG", "Portland, OR", "Kampala, Uganda"):
        assert market_in_coverage(rule, market) is True, market


@pytest.mark.parametrize("limb", range(3), ids=_LIMB_IDS)
def test_a_bare_exclusion_is_read_in_the_location_country(limb):
    """As on every other rule: a bare excluded name is qualified by the location's country."""
    rule = _online_rules(("Ikeja",), home=LAGOS)[limb]
    assert market_in_coverage(rule, "Ikeja, NG") is False
    assert market_in_coverage(rule, "Lagos, NG") is True


@pytest.mark.parametrize("limb", range(3), ids=_LIMB_IDS)
def test_an_exclusion_wins_over_a_market_it_cannot_rule_out(limb):
    """Fail closed, as exclusions already were on every other rule: an unqualified or
    unreadable market cannot be shown to lie outside the exclusion, so it is denied."""
    rule = _online_rules(("London, UK",))[limb]
    for market in ("London", None, "", "   ", "None"):
        assert market_in_coverage(rule, market) is False, market
    # A market a component separates from the exclusion is still admitted.
    for market in ("London, ON", "London, Ontario, Canada", "Paris", "anywhere"):
        assert market_in_coverage(rule, market) is True, market


@pytest.mark.parametrize("limb", range(3), ids=_LIMB_IDS)
def test_a_country_or_region_exclusion_removes_what_it_cannot_rule_out(limb):
    """The breadth is deliberate (W21-REV-5DW2 r1, C1): a country exclusion removes every
    observed market that states no country, and a region exclusion every observation in its
    country that states no region. A stated component that differs still admits."""
    uk = _online_rules(("UK",))[limb]
    assert market_in_coverage(uk, "Montgomery") is False
    assert market_in_coverage(uk, "Montgomery, US") is True
    texas = _online_rules(("Texas",))[limb]
    assert market_in_coverage(texas, "Little Rock, US") is False
    assert market_in_coverage(texas, "Little Rock") is False
    assert market_in_coverage(texas, "Little Rock, AR") is True
    assert market_in_coverage(texas, "London, UK") is True


@pytest.mark.parametrize("limb", range(3), ids=_LIMB_IDS)
def test_online_coverage_without_exclusions_is_unchanged(limb):
    rule = _online_rules(())[limb]
    for market in [*FOUR_MARKETS, "London", "anywhere", None, ""]:
        assert market_in_coverage(rule, market) is True, market


@pytest.mark.parametrize("limb", range(3), ids=_LIMB_IDS)
def test_unreadable_exclusion_text_excludes_nothing(limb):
    """An exclusion with no usable text names no place (as before the fix)."""
    rule = _online_rules(("", "   ", "None", "null"))[limb]
    for market in [*FOUR_MARKETS, None]:
        assert market_in_coverage(rule, market) is True, market


@pytest.mark.parametrize("limb", range(3), ids=_LIMB_IDS)
def test_resolve_geo_reports_an_excluded_market_outside_the_scout_area(limb):
    rule = _online_rules(("London, UK",), home=LONDON)[limb]
    res = resolve_geo(rule, signals=[("local_source", "London, UK"), ("hashtag", "London, UK")])
    assert res.resolved_market == "London, UK"
    assert res.inside_scout_area is False
    other = resolve_geo(rule, signals=[("local_source", "Nairobi, KE")])
    assert other.inside_scout_area is True


# --------------------------------------------------------------------------- #
# "York" is not "New York": no substring false positives
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("limb", range(3), ids=_LIMB_IDS)
@pytest.mark.parametrize(
    ("excluded", "admitted"),
    [
        ("York", "New York, NY"),
        ("York, UK", "New York, NY"),
        ("York, PA", "New York, NY"),
        ("New York, NY", "York, PA"),
        ("New York, NY", "York, UK"),
        ("Lagos, Portugal", "Lagos, NG"),
        ("London, Ontario", "London, UK"),
        ("Dallas, GA", "Dallas, TX"),
        ("Paris, TX", "Paris, France"),
        ("Jersey", "New Jersey"),
    ],
)
def test_no_substring_false_positive_under_online_coverage(limb, excluded, admitted):
    rule = _online_rules((excluded,), home=DALLAS)[limb]
    assert market_in_coverage(rule, admitted) is True


@pytest.mark.parametrize(
    ("included", "market"),
    [
        ("York, PA", "New York, NY"),
        ("York, UK", "New York, NY"),
        ("New York, NY", "York, PA"),
        ("Lagos, NG", "Lagos, Portugal"),
        ("London, UK", "New London, CT"),
    ],
)
def test_no_substring_false_positive_on_inclusion(included, market):
    rule = CoverageRule(coverage_type="city", included_markets=(included,), home_market=DALLAS)
    assert market_in_coverage(rule, market) is False


def test_york_is_not_new_york_on_a_radius_rule_with_exclusions():
    rule = CoverageRule(
        coverage_type="state",
        included_markets=("New York",),
        country="US",
        excluded_markets=("York",),
    )
    assert coverage_scopes(rule)
    assert market_in_coverage(rule, "New York, NY") is True


# --------------------------------------------------------------------------- #
# The behaviour change, characterised exactly
# --------------------------------------------------------------------------- #

_EXCLUSION_SETS: list[tuple[str, ...]] = [
    (),
    ("London, UK",),
    ("Dallas, TX", "Nairobi, KE"),
    ("Texas",),
    ("York",),
    ("London",),
    ("Ikeja",),
    ("",),
]
_SHAPES = [
    {"coverage_type": "online"},
    {"coverage_type": "online", "online_global": True},
    {"coverage_type": "radius", "online_global": True},
    {"coverage_type": "radius"},
    {"coverage_type": "radius", "radius_miles": 25, "included_markets": ("Dallas, TX",)},
    {"coverage_type": "city", "included_markets": ("London, UK", "Lagos")},
    {"coverage_type": "state", "state": "Texas", "country": "US"},
    {"coverage_type": "country", "country": "Kenya"},
    {"coverage_type": "multi_city", "included_markets": ("Nairobi, KE", "Dallas")},
]
_HOMES = [None, DALLAS, LONDON, LAGOS, NAIROBI]
_MARKETS = [
    *FOUR_MARKETS,
    "Austin, TX",
    "London, ON",
    "Lagos, Portugal",
    "New York, NY",
    "York, UK",
    "Ikeja, NG",
    "London",
    "Dallas",
    "anywhere",
    "",
    "None",
    None,
]


def _grid():
    for shape, excluded, home in itertools.product(_SHAPES, _EXCLUSION_SETS, _HOMES):
        yield CoverageRule(**shape, excluded_markets=excluded, home_market=home)


def test_only_online_decisions_on_excluded_markets_change_and_only_to_deny():
    changed = 0
    for rule in _grid():
        online = rule.online_global or rule.coverage_type == "online"
        for market in _MARKETS:
            before = legacy_market_in_coverage(rule, market)
            after = market_in_coverage(rule, market)
            if online and is_excluded(rule, market):
                assert (before, after) == (True, False), (rule, market)
                changed += 1
            else:
                assert after is before, (rule, market)
    assert changed > 0


def test_is_excluded_is_the_same_test_every_rule_already_applied():
    """Non-online rules read their exclusions exactly as before: ``is_excluded`` is the
    CF-3 loop, now shared by both limbs."""
    for rule in _grid():
        if rule.online_global or rule.coverage_type == "online":
            continue
        for market in _MARKETS:
            if is_excluded(rule, market):
                assert legacy_market_in_coverage(rule, market) is False, (rule, market)
