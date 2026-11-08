"""Project Phase 5D-W4 adversarial battery: exclusions bind every coverage rule (exit criterion 5).

Lane 5D-W4 (W21) tests lane 5D-W2's exclusion fix (#299) against the contract and the item-35
ruling, not against what the code happens to do:

* ``docs/project-phase-5d-multi-location-jurisdiction.md`` §2: "exclusions are evaluated
  **before** any short-circuit (curing ``online_global`` bypassing exclusions), and matching uses
  normalized codes, not substrings. The existing substring paths remain for scoring display but
  are never used for gating"; §4 exit criterion 5 ("an excluded jurisdiction is denied even under
  ``online_global`` coverage"; "York" is not "New York").
* Item 35, ruled 2026-11-07 (``RULINGS-ITEMS-35-42-2026-11-07.md``): "Online coverage must
  respect exclusions, including denying an observed market when the recorded geography cannot
  rule out overlap with an excluded area." Recorded breadth: a country exclusion also removes
  every observed market that names no country (278 of 281 bare principal-city names); a region
  exclusion also removes observations in its country that name no region.

The below-relevance-floor "monitor" behaviour the same ruling records (PP-35-MONITOR) is product
polish, not a requirement, and is deliberately not pinned here.

Not in this module (reported as defects to the coordinator, with reproductions in the lane's
evidence ``defects/``): an exclusion written as an ISO 3166-2 code ("US-TX", "NG-LA") or as a
Nigerian state in words ("Lagos State") removes nothing in that subdivision. A failing test is
never committed.
"""

from __future__ import annotations

import ast
import itertools
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import update
from sqlalchemy.orm import Session

import app.connectors.policy as policy_module
import app.geography.engine as engine_module
import app.geography.markets as markets_module
import app.jurisdiction.resolver as resolver_module
from app.connectors.policy import ConnectorPolicy
from app.connectors.rss import FeedItem, item_signal
from app.geography.engine import CoverageRule, is_excluded, market_in_coverage, resolve_geo
from app.geography.markets import market_from_parts, parse_observed
from app.geography.places_data import KNOWN_CITIES
from app.jobs.pipeline import _coverage_rule_for
from app.locations.models import GeoCoverageRule
from app.scouting_requests.models import ScoutRequest
from app.tests._auth2_support import require_postgres
from app.tests._privacy_support import PrivacyEnv, privacy_world

DALLAS = market_from_parts(city="Dallas", region="Texas", country="United States")
LAGOS = market_from_parts(city="Lagos", region="Lagos", country="Nigeria")


def _online(excluded, *, home=None) -> list[CoverageRule]:
    """Both short-circuit limbs and the two together, plus a radius rule with no centre (no
    scope at all), each excluding ``excluded``."""
    excluded = tuple(excluded)
    return [
        CoverageRule(
            coverage_type="radius", online_global=True, excluded_markets=excluded, home_market=home
        ),
        CoverageRule(coverage_type="online", excluded_markets=excluded, home_market=home),
        CoverageRule(
            coverage_type="online", online_global=True, excluded_markets=excluded, home_market=home
        ),
    ]


_LIMBS = ["online_global", "coverage_type=online", "both"]


# --------------------------------------------------------------------------- #
# 1. An excluded jurisdiction is denied under online_global and online coverage
# --------------------------------------------------------------------------- #
#: (exclusion as a customer could write it, observed markets inside it, markets outside it).
#: The four-market matrix's jurisdictions in alias, case, width and padding variants.
EXCLUDED_JURISDICTIONS = [
    (
        "texas",
        ["Dallas, TX", "Austin, Texas", "Houston, Texas, USA", "Dallas, TX 75201"],
        ["Tulsa, OK", "London, UK", "Paris, France"],
    ),
    ("  TEXAS  ", ["Dallas, TX", "El Paso, TX, United States"], ["Little Rock, AR"]),
    ("ＴＸ", ["Dallas, TX"], ["Portland, OR"]),  # full-width TX
    ("Texas, US", ["Dallas, TX", "Austin, Texas, United States of America"], ["Tulsa, OK"]),
    (
        "United Kingdom",
        ["London, UK", "Manchester, England", "Edinburgh, Scotland, GB"],
        ["London, ON", "Dublin, Ireland", "Dallas, TX"],
    ),
    ("great britain", ["London, UK", "Leeds, United Kingdom"], ["Nairobi, KE"]),
    ("gb", ["London, England, UK"], ["London, Ontario, Canada"]),
    (
        "Nigeria",
        ["Lagos, NG", "Ikeja, Lagos, NG", "Abuja, Nigeria"],
        ["Niamey, Niger", "Accra, GH"],
    ),
    ("NG", ["Lagos, Nigeria"], ["Lagos, Portugal"]),
    ("Lagos, NG", ["Lagos, Nigeria", "Lagos, Lagos, NG"], ["Ikeja, Lagos, NG", "Lagos, Portugal"]),
    ("KENYA", ["Nairobi, KE", "Mombasa, Kenya"], ["Kampala, Uganda", "Louisville, Kentucky"]),
    ("ＫＥ", ["Nairobi, Kenya"], ["Dar es Salaam, Tanzania"]),  # full-width KE
]


@pytest.mark.parametrize("limb", range(3), ids=_LIMBS)
@pytest.mark.parametrize(
    ("excluded", "inside", "outside"),
    EXCLUDED_JURISDICTIONS,
    ids=[repr(e[0]) for e in EXCLUDED_JURISDICTIONS],
)
def test_an_excluded_jurisdiction_is_denied_under_online_coverage(limb, excluded, inside, outside):
    rule = _online([excluded])[limb]
    for market in inside:
        assert market_in_coverage(rule, market) is False, market
    for market in outside:
        assert market_in_coverage(rule, market) is True, market


@pytest.mark.parametrize("limb", range(3), ids=_LIMBS)
def test_one_matching_exclusion_among_many_is_enough(limb):
    """The exclusion that matches is last, after unreadable and non-matching ones, and a
    duplicate of it: every exclusion is read, none short-circuits the others."""
    rule = _online(["", "None", "Paris, France", "Tulsa, OK", "Texas", "Texas"])[limb]
    assert market_in_coverage(rule, "Dallas, TX") is False
    assert market_in_coverage(rule, "Paris, France") is False
    assert market_in_coverage(rule, "Nairobi, KE") is True


@pytest.mark.parametrize("limb", range(3), ids=_LIMBS)
def test_the_exclusion_holds_through_resolve_geo(limb):
    """The scout pipeline's own decision (``resolve_geo``): evidence that pools on an excluded
    market is outside the scout area, whatever its weight."""
    rule = _online(["United Kingdom"])[limb]
    heavy = [
        ("platform_geotag", "London, UK"),
        ("business_address", "London, United Kingdom"),
        ("local_source", "london, uk"),
    ]
    resolved = resolve_geo(rule, heavy)
    assert resolved.inside_scout_area is False
    assert resolve_geo(rule, [("local_source", "Nairobi, KE")]).inside_scout_area is True


@pytest.fixture(params=["sqlite", "postgresql"])
def env(request, tmp_path) -> Iterator[PrivacyEnv]:
    if request.param == "sqlite":
        with privacy_world(tmp_path / "exclusions.db") as e:
            yield e
        return
    require_postgres()
    with privacy_world(None) as e:  # pragma: no cover - gated on PostgreSQL
        yield e


@pytest.mark.parametrize(
    "shape",
    [{"online_global": True}, {"coverage_type": "online"}],
    ids=["online_global", "coverage_type=online"],
)
def test_a_stored_online_rule_keeps_its_exclusions_into_the_run(env, shape):
    """End to end from the database: a Scout's location's stored ``geo_coverage_rules`` row,
    online and with exclusions, becomes the run's coverage rule (``_coverage_rule_for``), and
    the excluded market is outside the scout area."""
    with env.witness.begin() as conn:
        conn.execute(
            update(GeoCoverageRule.__table__)
            .where(GeoCoverageRule.__table__.c.id == "a1-geo")
            .values(excluded_markets=["London, UK", "Texas"], **shape)
        )
    with Session(bind=env.engine) as s:
        request = s.get(ScoutRequest, "a1-scout")
        assert request is not None and request.location_id == "a1-loc"
        coverage, _market, _source_market = _coverage_rule_for(s, request)
    assert coverage.excluded_markets == ("London, UK", "Texas")
    for market in ("London, UK", "Dallas, TX", "Austin, Texas, US"):
        assert resolve_geo(coverage, [("local_source", market)]).inside_scout_area is False
    assert resolve_geo(coverage, [("local_source", "Nairobi, KE")]).inside_scout_area is True


# --------------------------------------------------------------------------- #
# 2. Exclusions are evaluated before any short-circuit
# --------------------------------------------------------------------------- #
_SHAPES = [
    {"coverage_type": "online"},
    {"coverage_type": "online", "online_global": True},
    {"coverage_type": "radius", "online_global": True},
    {"coverage_type": "city", "online_global": True, "included_markets": ("Dallas, TX",)},
    {"coverage_type": "radius"},
    {
        "coverage_type": "radius",
        "radius_miles": 50,
        "center_latitude": 32.7,
        "center_longitude": -96.8,
        "included_markets": ("Dallas, TX",),
    },
    {"coverage_type": "city", "included_markets": ("London, UK", "Dallas, TX")},
    {"coverage_type": "state", "state": "Texas", "country": "US"},
    {"coverage_type": "country", "country": "Kenya"},
    {"coverage_type": "multi_city", "included_markets": ("Lagos, NG", "Nairobi, KE")},
]
_EXCLUSIONS = [
    ("Dallas, TX",),
    ("Texas",),
    ("UK",),
    ("Kenya",),
    ("Lagos, NG",),
    ("York",),
    ("London",),
    ("", "Nairobi, KE"),
]
_HOMES = [None, DALLAS, LAGOS]
_MARKETS = [
    "Dallas, TX",
    "Austin, TX",
    "London, UK",
    "London, ON",
    "Lagos, NG",
    "Nairobi, KE",
    "New York, NY",
    "York, UK",
    "Dallas",
    "London",
    "anywhere",
    "",
    None,
]


def test_no_rule_of_any_shape_admits_a_market_its_exclusions_remove():
    """The invariant behind "before any short-circuit": over every coverage shape (online,
    online_global, radius, city, state, country), home and market, a market an exclusion
    removes is never inside the rule."""
    checked = removed = 0
    for shape, excluded, home in itertools.product(_SHAPES, _EXCLUSIONS, _HOMES):
        rule = CoverageRule(**shape, excluded_markets=excluded, home_market=home)
        for market in _MARKETS:
            checked += 1
            if is_excluded(rule, market):
                removed += 1
                assert market_in_coverage(rule, market) is False, (shape, excluded, home, market)
    assert removed > 100 and checked > removed


@pytest.mark.parametrize("limb", range(3), ids=_LIMBS)
def test_an_unreadable_market_is_removed_by_any_exclusion_even_online(limb):
    """A market that cannot be read at all cannot be ruled out of any exclusion."""
    rule = _online(["Paris, France"])[limb]
    for market in (None, "", "   ", "None", "null", ",", " , "):
        assert parse_observed(market) is None, market
        assert market_in_coverage(rule, market) is False, market
    # A readable market a known component separates from the exclusion is still admitted.
    for market in ("London", "Paris, TX", "Lyon, France"):
        assert market_in_coverage(rule, market) is True, market


# --------------------------------------------------------------------------- #
# 3. "York" is not "New York", in both directions, on every gating path
# --------------------------------------------------------------------------- #
YORK_PAIRS = [
    ("York", "New York"),
    ("YORK", "NEW YORK"),
    ("ｙｏｒｋ", "New York"),  # full-width york
    ("York, UK", "New York, NY"),
    ("York, England, UK", "New York, New York, USA"),
    ("York", "New York, NY"),
]


@pytest.mark.parametrize(("short", "long"), YORK_PAIRS)
@pytest.mark.parametrize("limb", range(3), ids=_LIMBS)
def test_york_does_not_exclude_new_york_nor_the_reverse(limb, short, long):
    assert market_in_coverage(_online([short])[limb], long) is True
    assert market_in_coverage(_online([long])[limb], short) is True


def test_york_is_not_new_york_on_inclusion_and_connector_clearance():
    for short, long in (("York, UK", "New York, NY"), ("York, PA, US", "New York, NY")):
        for included, observed in ((short, long), (long, short)):
            rule = CoverageRule(coverage_type="city", included_markets=(included,))
            assert market_in_coverage(rule, observed) is False, (included, observed)
            policy = ConnectorPolicy("rss_news", True, frozenset({included}))
            assert policy.permits(source_types=(), market=observed) is False
        # The control: each is inside its own scope.
        assert (
            market_in_coverage(CoverageRule(coverage_type="city", included_markets=(long,)), long)
            is True
        )


# --------------------------------------------------------------------------- #
# 4. The item-35 breadth, as ruled
# --------------------------------------------------------------------------- #
PRINCIPAL_CITIES = sorted({name for names in KNOWN_CITIES.values() for name in names})
#: The bare principal-city names an observation reads with no country at all.
COUNTRYLESS = [
    n
    for n in PRINCIPAL_CITIES
    if (parsed := parse_observed(n)) is not None and parsed.country is None
]


def test_the_recorded_breadth_is_what_the_ruling_disclosed():
    """Item 35 records the breadth "278 of 281 bare principal-city names". A change to the place
    data that moves it is a change to a disclosed breadth: re-measure and re-disclose."""
    assert (len(COUNTRYLESS), len(PRINCIPAL_CITIES)) == (278, 281)


@pytest.mark.parametrize("excluded", ["UK", "Kenya", "Nigeria", "United States", "fr"])
@pytest.mark.parametrize("limb", range(3), ids=_LIMBS)
def test_a_country_exclusion_removes_every_bare_principal_city(limb, excluded):
    """Item 35 as ruled: an observation that names no country cannot be ruled out of the
    excluded country, so it is denied, wherever the city actually is."""
    rule = _online([excluded])[limb]
    admitted = [name for name in COUNTRYLESS if market_in_coverage(rule, name)]
    assert admitted == []
    # A stated, differing country still admits: the breadth is "cannot rule out", no wider.
    assert market_in_coverage(rule, "Tokyo, Japan") is True


@pytest.mark.parametrize("limb", range(3), ids=_LIMBS)
def test_a_region_exclusion_removes_its_countrys_regionless_observations(limb):
    rule = _online(["Texas"])[limb]
    for market in ("Springfield, US", "Houston, USA", "Dallas, United States", "Dallas"):
        assert market_in_coverage(rule, market) is False, market
    # Another stated region of the same country, or another country, rules the overlap out.
    for market in ("Springfield, IL", "Springfield, IL, US", "Toronto, Canada", "Lagos, NG"):
        assert market_in_coverage(rule, market) is True, market


# --------------------------------------------------------------------------- #
# 5. Substring paths are never used for gating
# --------------------------------------------------------------------------- #
#: Each pair's names contain one another as text but are different places, which structured
#: identity tells apart. A substring test would equate each pair.
SUBSTRING_TRAPS = [
    ("Jersey", "New Jersey"),
    ("Mexico", "New Mexico"),
    ("Wales", "New South Wales"),
    ("Guinea", "Papua New Guinea"),
    ("Guinea", "Equatorial Guinea"),
    ("Niger", "Nigeria"),
    ("Niger", "Niger State, NG"),
    ("Sudan", "South Sudan"),
    ("Virginia", "West Virginia"),
    ("Kansas", "Arkansas"),
    ("Dominica", "Dominican Republic"),
    ("Ireland", "Northern Ireland"),
    ("India", "Indiana"),
    ("Kenya", "Kentucky"),
    ("Oman", "Romania"),
    ("Mali", "Somalia"),
    ("Castle, UK", "Newcastle, UK"),
    ("Paris, TX", "Paris, France"),
    ("London, ON", "London, UK"),
    ("Lagos, Portugal", "Lagos, NG"),
]


@pytest.mark.parametrize(("a", "b"), SUBSTRING_TRAPS)
def test_no_gating_path_equates_a_name_with_one_containing_it(a, b):
    for first, second in ((a, b), (b, a)):
        for rule in _online([first]):  # exclusion: the other place is still admitted
            assert market_in_coverage(rule, second) is True, ("excluded", first, second)
        inclusion = CoverageRule(coverage_type="city", included_markets=(first,))
        assert market_in_coverage(inclusion, second) is False, ("included", first, second)
        policy = ConnectorPolicy("rss_news", True, frozenset({first}))
        assert policy.permits(source_types=(), market=second) is False, ("cleared", first, second)


_GATING_MODULES = [engine_module, markets_module, policy_module, resolver_module]


def _case_folded_membership(tree: ast.AST) -> list[int]:
    """Line numbers of ``x in y`` / ``x not in y`` searched inside a ``.lower()``,
    ``.casefold()`` or ``.upper()`` call: the shape of the pre-P6-DATA-6 substring match
    (``m.lower() in market.lower()``). A folded key looked up in a table (``code.upper() in
    COUNTRIES``) is membership, not a substring search, and is not flagged."""

    def folded(node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"lower", "casefold", "upper"}
        )

    lines = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare) and any(
            isinstance(op, ast.In | ast.NotIn) for op in node.ops
        ):
            if any(folded(c) for c in node.comparators):
                lines.append(node.lineno)
    return lines


@pytest.mark.parametrize("module", _GATING_MODULES, ids=lambda m: m.__name__)
def test_the_gating_modules_hold_no_case_folded_substring_match(module):
    source = Path(module.__file__).read_text(encoding="utf-8")
    assert _case_folded_membership(ast.parse(source)) == []


def test_the_retained_substring_path_is_display_evidence_only():
    """The RSS connector's text-mention evidence is the substring path §2 retains "for scoring
    display": it adds weight to the request's own market, never another market, so it cannot
    move a gating decision. "York" mentioned in an item for a New York request is evidence for
    New York only, and a York exclusion still admits it."""
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    item = FeedItem(
        title="York traffic",
        description="Downtown York closes a road",
        link="x",
        guid="g",
        published=now,
        author=None,
    )
    signal = item_signal(
        item, market="York, UK", retrieved_at=now, now=now, is_simulated=True, attribution={}
    )
    assert ("text_mention", "York, UK") in signal.geo_evidence
    assert {market for _kind, market in signal.geo_evidence} == {"York, UK"}
    excluding_new_york = _online(["New York, NY"])[0]
    with_mention = resolve_geo(excluding_new_york, signal.geo_evidence)
    without = resolve_geo(excluding_new_york, [("local_source", "York, UK")])
    assert with_mention.inside_scout_area is without.inside_scout_area is True
    assert with_mention.confidence > without.confidence  # display weight only
