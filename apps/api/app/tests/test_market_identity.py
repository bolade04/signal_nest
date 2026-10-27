"""P6-DATA-6: structured market identity, fail-closed coverage and connector policy.

Market matching used to be a bidirectional substring test that failed open: a bare
"London" satisfied "London, Ontario", a radius rule with no market list admitted
every market, and connector policy skipped its market check when the market was
unknown. These tests pin the replacement: markets compare as (city, region, country),
an unknown or unqualified market lies inside no location-scoped rule, and confusable
places never satisfy each other's rules — while the four fixture markets (Dallas,
London, Lagos, Nairobi) keep matching themselves and only themselves.
"""

from __future__ import annotations

import pytest

from app.connectors.policy import ConnectorPolicy
from app.geography.engine import CoverageRule, coverage_scopes, market_in_coverage, resolve_geo
from app.geography.markets import (
    MarketIdentity,
    country_name,
    fold,
    market_from_parts,
    market_within,
    may_overlap,
    normalize_country,
    parse_market,
    parse_observed,
    text_within,
)
from app.geography.places_data import COUNTRIES

# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #

#: (text, expected (city, region, country)) for an OBSERVED market.
OBSERVED = [
    ("Dallas, TX", ("dallas", "US-TX", "US")),
    ("London, UK", ("london", None, "GB")),
    ("Lagos, NG", ("lagos", None, "NG")),
    ("Nairobi, KE", ("nairobi", None, "KE")),
    ("London, Ontario", ("london", "CA-ON", "CA")),
    ("London, ON", ("london", "CA-ON", "CA")),
    ("London, England", ("london", "GB-ENG", "GB")),
    ("London, Ontario, Canada", ("london", "CA-ON", "CA")),
    ("Paris, TX", ("paris", "US-TX", "US")),
    ("Paris, France", ("paris", None, "FR")),
    ("Portland, OR", ("portland", "US-OR", "US")),
    ("Lagos, Portugal", ("lagos", None, "PT")),
    ("Birmingham, UK", ("birmingham", None, "GB")),
    ("Texas", (None, "US-TX", "US")),
    ("Texas, USA", (None, "US-TX", "US")),
    ("New York, NY", ("new york", "US-NY", "US")),
    ("Dallas TX 75201", ("dallas", "US-TX", "US")),
    ("Washington, D.C.", ("washington", "US-DC", "US")),
    ("São Paulo, Brazil", ("sao paulo", None, "BR")),
    ("St. Louis, Missouri", ("st louis", "US-MO", "US")),
    ("Windhoek, NA", ("windhoek", None, "NA")),
    # An ambiguous qualifier is read where place knowledge (KNOWN_CITIES) puts the
    # city in exactly one reading...
    ("Tbilisi, Georgia", ("tbilisi", None, "GE")),
    ("Batumi, Georgia", ("batumi", None, "GE")),
    ("Atlanta, Georgia", ("atlanta", "US-GA", "US")),
    ("Savannah, GA", ("savannah", "US-GA", "US")),
    ("Libreville, GA", ("libreville", None, "GA")),
    ("Los Angeles, CA", ("los angeles", "US-CA", "US")),
    ("Toronto, CA", ("toronto", None, "CA")),
    ("Chicago, IL", ("chicago", "US-IL", "US")),
    ("Tel Aviv, IL", ("tel aviv", None, "IL")),
    ("Perth, WA", ("perth", "AU-WA", "AU")),
    ("Seattle, WA", ("seattle", "US-WA", "US")),
    ("Portland, ME", ("portland", "US-ME", "US")),
    ("Birmingham, AL", ("birmingham", "US-AL", "US")),
    ("Atlanta GA", ("atlanta", "US-GA", "US")),
    # ...and otherwise stays unqualified: it matches nothing.
    ("Smallville, Georgia", ("smallville", None, None)),
    ("Smallville, GA", ("smallville", None, None)),
    ("Joliet, IL", ("joliet", None, None)),
    ("Smallville, WA", ("smallville", None, None)),
    ("Georgia", ("georgia", None, None)),
    ("CA", ("ca", None, None)),
    # A bare place name is never given a country it does not state.
    ("London", ("london", None, None)),
    ("London, None", ("london", None, None)),
    ("Ikeja, Lagos", ("ikeja", None, None)),
]


@pytest.mark.parametrize(("text", "expected"), OBSERVED)
def test_parse_observed(text, expected):
    identity = parse_observed(text)
    assert identity is not None
    assert (identity.city, identity.region, identity.country) == expected
    assert identity.label == text


@pytest.mark.parametrize(
    ("text", "known", "expected"),
    [
        # The place being compared with is knowledge too: its own city decides.
        ("Joliet, IL", ("Joliet", "Illinois", "US"), ("joliet", "US-IL", "US")),
        ("Smallville, GA", ("Smallville", "Georgia", "US"), ("smallville", "US-GA", "US")),
        ("Smallville, WA", ("Smallville", "WA", "Australia"), ("smallville", "AU-WA", "AU")),
        # ...but only its city, never just its country or region.
        ("Smallville, GA", (None, "Georgia", "US"), ("smallville", None, None)),
        ("Smallville, GA", ("Otherville", "Georgia", "US"), ("smallville", None, None)),
        # A city known in the other reading stays ambiguous: nothing is guessed.
        ("Tbilisi, Georgia", ("Tbilisi", "Georgia", "US"), ("tbilisi", None, None)),
    ],
)
def test_parse_observed_with_a_known_place(text, known, expected):
    city, region, country = known
    place = market_from_parts(city=city, region=region, country=country)
    identity = parse_observed(text, known=place)
    assert (identity.city, identity.region, identity.country) == expected


def test_known_cities_resolve_every_ambiguous_qualifier_and_never_both_ways():
    """Every reading of every ambiguous qualifier has principal cities, and no city
    is listed under two readings of the same qualifier (it would stay ambiguous)."""
    from app.geography import markets

    tokens = (
        set(markets._REGION_INDEX)
        | set(markets._COUNTRY_BY_NAME)
        | {code.lower() for code in COUNTRIES}
    )
    ambiguous = {t: markets._area_candidates(t) for t in tokens}
    ambiguous = {t: c for t, c in ambiguous.items() if len(c) > 1}
    assert "georgia" in ambiguous and "ga" in ambiguous and "ca" in ambiguous
    for token, readings in ambiguous.items():
        cities = []
        for country, region in readings:
            keys = [region] if region else [
                k for k in markets._KNOWN_CITIES if k == country or k.startswith(f"{country}-")
            ]
            listed = set().union(*(markets._KNOWN_CITIES.get(k, frozenset()) for k in keys))
            assert listed, (token, country, region)
            cities.append(listed)
        for i, first in enumerate(cities):
            for second in cities[i + 1 :]:
                assert not first & second, (token, first & second)


#: (text, location country, expected) for a market a customer CONFIGURED.
CONFIGURED = [
    ("London", "CA", ("london", None, "CA")),
    ("London", "GB", ("london", None, "GB")),
    ("Ikeja", "NG", ("ikeja", None, "NG")),
    ("Ikeja, Lagos", "NG", ("ikeja", "NG:lagos", "NG")),
    ("Victoria", "CA", ("victoria", None, "CA")),
    ("Victoria", "AU", (None, "AU-VIC", "AU")),
    ("Georgia", "US", (None, "US-GA", "US")),
    ("Georgia", "GE", (None, None, "GE")),
    ("New York", "US", ("new york", None, "US")),
    ("Nigeria", "US", (None, None, "NG")),
    ("London, Ontario", "US", ("london", "CA-ON", "CA")),
    ("London", None, ("london", None, None)),
]


@pytest.mark.parametrize(("text", "country", "expected"), CONFIGURED)
def test_parse_configured(text, country, expected):
    identity = parse_market(text, default_country=country)
    assert identity is not None
    assert (identity.city, identity.region, identity.country) == expected


@pytest.mark.parametrize("text", [None, "", "   ", ",", "None", "null, None"])
def test_nothing_usable_parses_to_none(text):
    assert parse_observed(text) is None
    assert parse_market(text, default_country="US") is None


def test_label_does_not_take_part_in_equality():
    assert parse_observed("London, UK") == parse_observed("london , united kingdom")


def test_fold_is_case_accent_and_punctuation_insensitive():
    assert fold("  SÃO-Paulo ") == "sao paulo"
    assert fold("Saint-Étienne") == fold("St. Etienne") == "st etienne"
    assert fold("Côte d’Ivoire") == "cote divoire"


def test_market_from_parts_reads_location_fields():
    assert market_from_parts(city="Dallas", region="TX", country="US") == MarketIdentity(
        "dallas", "US-TX", "US"
    )
    assert market_from_parts(city="London", region="England", country="United Kingdom") == (
        MarketIdentity("london", "GB-ENG", "GB")
    )
    assert market_from_parts(city="London", region="Ontario", country=None) == MarketIdentity(
        "london", "CA-ON", "CA"
    )
    # A country field holding a region ("England") still yields its country.
    assert market_from_parts(city="Leeds", region=None, country="England").country == "GB"
    assert market_from_parts(city="Lagos", region="Lagos State", country="Nigeria") == (
        MarketIdentity("lagos", "NG:lagos", "NG")
    )
    assert market_from_parts(city=None, region=None, country=None) is None
    assert market_from_parts(city="None", region="", country=None) is None


class TestCountryTable:
    def test_iso_table_is_complete_and_round_trips(self):
        assert len(COUNTRIES) == 249
        for code in COUNTRIES:
            assert normalize_country(code) == code
            assert normalize_country(country_name(code)) == code, code

    @pytest.mark.parametrize(
        ("text", "code"),
        [
            ("UK", "GB"),
            ("United Kingdom", "GB"),
            ("Great Britain", "GB"),
            ("U.S.A.", "US"),
            ("United States of America", "US"),
            ("Côte d'Ivoire", "CI"),
            ("Ivory Coast", "CI"),
            ("Bosnia and Herzegovina", "BA"),
            ("CA", "CA"),
            ("Georgia", "GE"),
            ("Korea", None),
            ("Narnia", None),
        ],
    )
    def test_country_names_and_aliases(self, text, code):
        assert normalize_country(text) == code


# --------------------------------------------------------------------------- #
# Confusable markets: never within each other's scope
# --------------------------------------------------------------------------- #

#: (scope text, scope's location country, observed market, within?)
CONFUSABLES = [
    # London, UK / London, Ontario
    ("London, UK", "GB", "London, UK", True),
    ("London, UK", "GB", "London, England", True),
    ("London, UK", "GB", "London, United Kingdom", True),
    ("London, UK", "GB", "London, ON", False),
    ("London, UK", "GB", "London, Ontario, Canada", False),
    ("London, Ontario", "CA", "London, UK", False),
    ("London, Ontario", "CA", "London, England", False),
    ("London, Ontario", "CA", "London, ON", True),
    ("London, ON", "CA", "London, Ontario, Canada", True),
    ("London", "CA", "London, UK", False),
    ("London", "CA", "London, ON", True),
    ("London", "GB", "London, ON", False),
    ("London", "GB", "London, England", True),
    # A bare observed "London" is within no London.
    ("London, UK", "GB", "London", False),
    ("London, Ontario", "CA", "London", False),
    ("London", "GB", "London", False),
    # Paris, France / Paris, Texas
    ("Paris, France", "FR", "Paris, TX", False),
    ("Paris, France", "FR", "Paris, FR", True),
    ("Paris, TX", "US", "Paris, France", False),
    ("Paris, TX", "US", "Paris, Texas", True),
    ("Paris", "US", "Paris, FR", False),
    # Portland, Oregon / Portland, Maine
    ("Portland, OR", "US", "Portland, ME", False),
    ("Portland, OR", "US", "Portland, Oregon", True),
    ("Portland, ME", "US", "Portland, OR", False),
    ("Portland, ME", "US", "Portland, Maine", True),
    ("Portland, Maine", "US", "Portland, ME", True),
    # Birmingham, England / Birmingham, Alabama
    ("Birmingham, UK", "GB", "Birmingham, AL", False),
    ("Birmingham, UK", "GB", "Birmingham, England", True),
    ("Birmingham, AL", "US", "Birmingham, UK", False),
    ("Birmingham, AL", "US", "Birmingham, Alabama", True),
    # A UK city is identified by city and country: an unstated region does not
    # exclude it, a different one does.
    ("Birmingham, England", "GB", "Birmingham, UK", True),
    ("Birmingham, England", "GB", "Birmingham, Scotland", False),
    # Where addresses carry the state, an unstated state is not known to match.
    ("Portland, OR", "US", "Portland, United States", False),
    ("London, Ontario", "CA", "London, Canada", False),
    ("Lagos, Lagos State, Nigeria", "NG", "Lagos, NG", True),
    # Lagos, Nigeria / Lagos, Portugal
    ("Lagos, NG", "NG", "Lagos, PT", False),
    ("Lagos, NG", "NG", "Lagos, Nigeria", True),
    ("Lagos, Portugal", "PT", "Lagos, NG", False),
    ("Lagos, Portugal", "PT", "Lagos, PT", True),
    # More same-name places.
    ("Dallas, TX", "US", "Dallas, GA", False),
    ("Springfield, IL", "US", "Springfield, MO", False),
    ("Cambridge, MA", "US", "Cambridge, UK", False),
    ("Kingston, Jamaica", "JM", "Kingston, ON", False),
    ("Perth, Western Australia", "AU", "Perth, Scotland", False),
    ("Perth, Western Australia", "AU", "Perth, WA", True),
    ("Smallville, WA", "US", "Smallville, Western Australia", False),
    ("Victoria, BC", "CA", "Victoria, Australia", False),
    ("Hyderabad, India", "IN", "Hyderabad, Pakistan", False),
    ("Valencia, Spain", "ES", "Valencia, Venezuela", False),
    ("San Jose, CA", "US", "San José, Costa Rica", False),
    ("Córdoba, Argentina", "AR", "Cordoba, Spain", False),
    ("São Paulo, Brazil", "BR", "Sao Paulo, BR", True),
    # Georgia, the US state, and Georgia, the country (adversarial review r1,
    # MAJOR): an ambiguous qualifier is never resolved toward the scope's country.
    ("Georgia", "US", "Tbilisi, Georgia", False),
    ("Georgia", "US", "Batumi, Georgia", False),
    ("Georgia", "US", "Atlanta, Georgia", True),
    ("Georgia", "US", "Savannah, GA", True),
    ("Georgia", "US", "Smallville, Georgia", False),
    ("Georgia", "US", "Smallville, GA", False),
    ("Georgia", "US", "Libreville, GA", False),
    ("Georgia", "GE", "Tbilisi, Georgia", True),
    ("Georgia", "GE", "Batumi, Georgia", True),
    ("Georgia", "GE", "Atlanta, GA", False),
    ("Georgia", "GE", "Smallville, Georgia", False),
    ("Atlanta, GA", "US", "Atlanta, Georgia", True),
    ("Atlanta, GA", "US", "Tbilisi, Georgia", False),
    # The same pattern for a 2-letter collision: "CA" is California and Canada.
    ("California", "US", "Los Angeles, CA", True),
    ("California", "US", "San Francisco, CA", True),
    ("California", "US", "Toronto, CA", False),
    ("California", "US", "Vancouver, CA", False),
    ("California", "US", "Smallville, CA", False),
    ("Canada", "CA", "Toronto, CA", True),
    ("Canada", "CA", "Los Angeles, CA", False),
    ("Canada", "CA", "Smallville, CA", False),
    # ...and for two subdivisions sharing a code: "WA".
    ("Western Australia", "AU", "Perth, WA", True),
    ("Western Australia", "AU", "Seattle, WA", False),
    ("Washington State", "US", "Seattle, WA", True),
    ("Washington State", "US", "Perth, WA", False),
    ("Washington State", "US", "Smallville, WA", False),
    # A city-level scope is itself knowledge of where its city is.
    ("Joliet, Illinois", "US", "Joliet, IL", True),
    ("Joliet, Illinois", "US", "Joliet, Israel", False),
    # Unknown markets fail closed.
    ("Dallas, TX", "US", "Atlantis", False),
    ("Dallas, TX", "US", "", False),
    ("Dallas, TX", "US", None, False),
]


@pytest.mark.parametrize(("scope_text", "country", "observed", "inside"), CONFUSABLES)
def test_confusable_markets(scope_text, country, observed, inside):
    scope = parse_market(scope_text, default_country=country)
    assert text_within(observed, scope) is inside
    rule = CoverageRule(coverage_type="city", country=country, included_markets=(scope_text,))
    assert market_in_coverage(rule, observed) is inside


FIXTURE_MARKETS = ["Dallas, TX", "London, UK", "Lagos, NG", "Nairobi, KE"]
FIXTURE_LOCATIONS = {
    # The seeded locations' structured fields (fixture geocoder answers).
    "Dallas, TX": ("Dallas", "Texas", "United States"),
    "London, UK": ("London", "England", "United Kingdom"),
    "Lagos, NG": ("Lagos", "Lagos", "Nigeria"),
    "Nairobi, KE": ("Nairobi", "Nairobi", "Kenya"),
}


@pytest.mark.parametrize("scope_market", FIXTURE_MARKETS)
@pytest.mark.parametrize("observed", FIXTURE_MARKETS)
def test_fixture_markets_match_only_themselves(scope_market, observed):
    city, region, country = FIXTURE_LOCATIONS[scope_market]
    home = market_from_parts(city=city, region=region, country=country)
    # As seeded: a radius rule listing the market, with the location's country.
    seeded = CoverageRule(
        coverage_type="radius",
        country=country,
        state=region,
        included_markets=(scope_market,),
        home_market=home,
    )
    # As created in the UI: a radius rule with no market list.
    unlisted = CoverageRule(coverage_type="radius", radius_miles=25, home_market=home)
    expected = scope_market == observed
    assert market_in_coverage(seeded, observed) is expected
    assert market_in_coverage(unlisted, observed) is expected


# --------------------------------------------------------------------------- #
# Coverage rules
# --------------------------------------------------------------------------- #

DALLAS = market_from_parts(city="Dallas", region="TX", country="US")
LONDON_ON = market_from_parts(city="London", region="Ontario", country="Canada")


class TestCoverage:
    def test_radius_rule_without_markets_is_no_longer_permissive(self):
        rule = CoverageRule(coverage_type="radius", radius_miles=25, home_market=DALLAS)
        assert market_in_coverage(rule, "Dallas, TX") is True
        assert market_in_coverage(rule, "Austin, TX") is False
        assert market_in_coverage(rule, "London, UK") is False

    def test_rule_with_no_place_at_all_admits_nothing(self):
        for rule in (
            CoverageRule(),
            CoverageRule(coverage_type="radius", radius_miles=25),
            CoverageRule(coverage_type="city", included_markets=("London",)),
            CoverageRule(coverage_type="city", included_markets=("Atlantis",)),
            CoverageRule(coverage_type="radius", home_market=MarketIdentity(city="london")),
        ):
            assert coverage_scopes(rule) == []
            for market in FIXTURE_MARKETS + ["London", "anywhere"]:
                assert market_in_coverage(rule, market) is False, (rule, market)

    def test_online_rules_admit_everything(self):
        for rule in (
            CoverageRule(coverage_type="online"),
            CoverageRule(coverage_type="radius", online_global=True),
        ):
            assert market_in_coverage(rule, "anywhere") is True
            assert market_in_coverage(rule, None) is True

    def test_unknown_observed_market_is_outside(self):
        rule = CoverageRule(coverage_type="radius", home_market=DALLAS)
        for market in (None, "", "   ", "None", "Dallas", "Atlantis"):
            assert market_in_coverage(rule, market) is False, market

    def test_state_rule_covers_its_state_only(self):
        rule = CoverageRule(coverage_type="state", home_market=DALLAS)
        assert market_in_coverage(rule, "Austin, TX") is True
        assert market_in_coverage(rule, "Paris, Texas") is True
        assert market_in_coverage(rule, "Portland, OR") is False
        assert market_in_coverage(rule, "Paris, France") is False
        explicit = CoverageRule(coverage_type="state", state="Ontario", country="Canada")
        assert market_in_coverage(explicit, "London, ON") is True
        assert market_in_coverage(explicit, "London, UK") is False

    def test_a_named_place_that_cannot_be_read_never_falls_back_to_the_location(self):
        rule = CoverageRule(coverage_type="city", included_markets=("???",), home_market=DALLAS)
        assert market_in_coverage(rule, "Dallas, TX") is False

    def test_country_rule_covers_its_country_only(self):
        rule = CoverageRule(coverage_type="country", home_market=LONDON_ON)
        assert market_in_coverage(rule, "Toronto, ON") is True
        assert market_in_coverage(rule, "London, UK") is False
        explicit = CoverageRule(coverage_type="country", country="Nigeria")
        assert market_in_coverage(explicit, "Lagos, NG") is True
        assert market_in_coverage(explicit, "Lagos, PT") is False
        garbage = CoverageRule(coverage_type="country", country="Narnia", home_market=DALLAS)
        assert market_in_coverage(garbage, "Austin, TX") is False

    def test_state_and_country_fields_no_longer_widen_a_city_rule(self):
        # Before: "texas" was a haystack, so any "..., Texas" market matched.
        rule = CoverageRule(
            coverage_type="city",
            country="United States",
            state="Texas",
            included_markets=("Dallas, TX",),
        )
        assert market_in_coverage(rule, "Dallas, Texas") is True
        assert market_in_coverage(rule, "Austin, Texas") is False

    def test_exclusions_win_and_fail_closed(self):
        rule = CoverageRule(
            coverage_type="state",
            country="US",
            included_markets=("Texas",),
            excluded_markets=("Paris, TX",),
        )
        assert market_in_coverage(rule, "Dallas, TX") is True
        assert market_in_coverage(rule, "Paris, TX") is False
        bare = CoverageRule(
            coverage_type="city",
            country="US",
            included_markets=("Dallas, TX",),
            excluded_markets=("Dallas",),
        )
        # "Dallas" (in the rule's country) cannot be ruled out for Dallas, TX.
        assert market_in_coverage(bare, "Dallas, TX") is False

    def test_may_overlap(self):
        texas = parse_market("Texas")
        assert may_overlap(parse_observed("Dallas, TX"), texas) is True
        assert may_overlap(parse_observed("London, UK"), texas) is False
        assert may_overlap(parse_observed("London"), parse_market("London, Ontario")) is True

    def test_market_within_requires_a_qualified_scope(self):
        assert market_within(parse_observed("London, UK"), MarketIdentity(city="london")) is False
        # Two unqualified identities with the same city still do not match: an
        # unqualified scope admits nothing (pins the ``is_qualified`` guard).
        bare = parse_observed("London")
        assert bare == MarketIdentity(city="london")
        assert market_within(bare, MarketIdentity(city="london")) is False
        assert market_within(MarketIdentity(), MarketIdentity()) is False
        assert market_within(None, parse_market("London, UK")) is False
        assert market_within(parse_observed("London, UK"), None) is False


class TestResolveGeo:
    def test_same_place_in_different_words_is_pooled(self):
        rule = CoverageRule(
            coverage_type="radius",
            home_market=market_from_parts(
                city="London", region="England", country="United Kingdom"
            ),
        )
        res = resolve_geo(
            rule,
            signals=[
                ("hashtag", "London, Ontario"),
                ("text_mention", "London, UK"),
                ("hashtag", "London, United Kingdom"),
            ],
        )
        # 0.55 + 0.35 for London, UK beats 0.35 for London, Ontario.
        assert res.resolved_market == "London, UK"
        assert res.evidence == ["text_mention: London, UK", "hashtag: London, United Kingdom"]
        assert res.confidence == round(1 - (1 - 0.55) * (1 - 0.35), 2)
        assert res.inside_scout_area is True

    def test_strongest_foreign_market_is_outside(self):
        rule = CoverageRule(coverage_type="radius", home_market=LONDON_ON)
        res = resolve_geo(rule, signals=[("local_source", "London, UK")])
        assert res.resolved_market == "London, UK"
        assert res.inside_scout_area is False


# --------------------------------------------------------------------------- #
# Connector policy
# --------------------------------------------------------------------------- #


class TestConnectorPolicy:
    def _policy(self, *markets: str) -> ConnectorPolicy:
        return ConnectorPolicy(
            source_type="rss_news", enabled=True, allowed_markets=frozenset(markets)
        )

    @pytest.mark.parametrize(
        ("allowed", "market", "permitted"),
        [
            (("London, UK",), "London, UK", True),
            (("London, UK",), "London, England", True),
            (("London, UK",), "London, Ontario", False),
            (("London, UK",), "London, ON", False),
            (("London, Ontario, Canada",), "London, UK", False),
            (("Paris, France",), "Paris, TX", False),
            (("Portland, OR",), "Portland, ME", False),
            (("Birmingham, UK",), "Birmingham, AL", False),
            (("Lagos, NG",), "Lagos, PT", False),
            (("Lagos, NG",), "Lagos, NG", True),
            (("Los Angeles, CA, US",), "Los Angeles, CA", True),
            (("Dallas, TX", "Nairobi, KE"), "Nairobi, KE", True),
            # Fail closed: an unknown or unqualified request market is never inside.
            (("London, UK",), None, False),
            (("London, UK",), "", False),
            (("London, UK",), "London", False),
            (("London, UK",), "London, None", False),
            # An allowed market that names no country clears nothing, not even itself.
            (("London",), "London, UK", False),
            (("London",), "London", False),
            # Ambiguous request markets are not resolved toward the cleared country.
            (("Georgia, US",), "Tbilisi, Georgia", False),
            (("Georgia, US",), "Atlanta, Georgia", True),
            (("Georgia, US",), "Smallville, GA", False),
        ],
    )
    def test_market_clearance(self, allowed, market, permitted):
        assert self._policy(*allowed).permits(source_types=("rss_news",), market=market) is (
            permitted
        )

    def test_unrestricted_policy_still_serves_any_market(self):
        policy = ConnectorPolicy(source_type="rss_news", enabled=True)
        assert policy.permits(source_types=("rss_news",), market=None) is True
        assert policy.permits(source_types=("rss_news",), market="London") is True

    def test_disabled_policy_never_serves(self):
        policy = ConnectorPolicy(
            source_type="rss_news", enabled=False, allowed_markets=frozenset({"London, UK"})
        )
        assert policy.permits(source_types=("rss_news",), market="London, UK") is False
