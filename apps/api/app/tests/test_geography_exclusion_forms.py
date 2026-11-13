"""W21 lane 5DW2FIX: exclusions written as jurisdiction codes or as Nigerian subdivisions
(defects D-5DW4-01 and D-5DW4-02).

Contract: Project Phase 5D §2 "exclusions are evaluated before any short-circuit ... and
matching uses normalized codes, not substrings"; exit criterion 5 "an excluded jurisdiction
is denied even under online_global coverage"; the item-35 ruling "Online coverage must
respect exclusions, including denying an observed market when the recorded geography cannot
rule out overlap with an excluded area".

Before the fix ``parse_market("US-TX")`` read a city "us" in Texas and "NG-LA" an unqualified
city "ng", so a code-form exclusion removed nothing in its jurisdiction; "Lagos State" read as
a city while ``parse_observed`` read the same words after a city as the region ``NG:lagos``.

This module pins:

* a code-form exclusion (ISO 3166-1 alpha-2, or ISO 3166-2 first-level) removes every
  observed market in its jurisdiction under every coverage type, in upper, lower and spaced
  forms and with a country suffix; a code the tables cannot place fails closed and is never
  read as a city;
* an observed market (evidence, a request's market, a business address) reads only a code the
  tables place: a road number or hyphenated name ("US-9", "GA-400", "AL-AIN") is no
  contradiction and widens no rule (W21-REV-5DW2FIX r1, C-1);
* a Nigerian state (or the FCT) named with its suffix removes markets in that state, and both
  parsers read it the same way, from ``app.jurisdiction.subdivisions``;
* same-name readings (W21-REV-5DW4 r1 C-1, decided from the item-35 ruling): an exclusion of a
  bare name that is also a Nigerian state ("Lagos, NG", "Niger") removes what either place
  covers; an inclusion or a clearance of it admits only what it did; "Niger" and "Niger State"
  never collapse into one another, and "Nigeria" is never a Niger place;
* the operator's exclusion semantics X-1 to X-5 (2026-11-08), which replace the blanket
  monotonicity requirement: explicit identifiers win over legacy misreadings, ambiguous names
  cover every reading a known country allows, observed markets are read in every plausible
  reading, "New York" is the city and the state, and no state is invented;
* the named inclusion and connector-clearance change ("US-TX" now admits Texas), and that it
  is bounded: it does not reach another jurisdiction, and the seeded market texts read the
  same either way;
* display and source-text paths keep the word reading.
"""

from __future__ import annotations

import itertools
from types import SimpleNamespace

import pytest

from app.connectors.policy import ConnectorPolicy
from app.connectors.sample_feeds import _MARKET_FEEDS, sample_feed_covers
from app.db.seed import CITIES
from app.geography import markets
from app.geography.engine import CoverageRule, market_in_coverage, resolve_geo
from app.geography.markets import (
    MarketIdentity,
    country_name,
    exclusion_readings,
    market_from_parts,
    market_within,
    may_overlap,
    observed_readings,
    parse_market,
    parse_observed,
    qualify_for_location,
)
from app.geography.places_data import COUNTRIES, SUBDIVISIONS
from app.jurisdiction.codes import UNRESOLVED
from app.jurisdiction.resolver import resolve_jurisdiction
from app.jurisdiction.subdivisions import SUBDIVISION_JURISDICTIONS
from app.scouting_requests.fixtures import ALL_MARKETS, fixtures_for_market

DALLAS = market_from_parts(city="Dallas", region="Texas", country="United States")
LONDON = market_from_parts(city="London", region="England", country="United Kingdom")
LAGOS = market_from_parts(city="Lagos", region="Lagos", country="Nigeria")
NAIROBI = market_from_parts(city="Nairobi", region="Nairobi", country="Kenya")
AL_AIN = market_from_parts(city="Al Ain", region=None, country="AE")
HOMES = [None, DALLAS, LONDON, LAGOS, NAIROBI]

NIGERIA = SUBDIVISION_JURISDICTIONS["NG"]


def _ident(identity: MarketIdentity | None) -> tuple | None:
    return None if identity is None else (identity.city, identity.region, identity.country)


def _admitting_rules(observed: str, excluded: tuple[str, ...], home=None) -> list[CoverageRule]:
    """Every coverage shape below that admits ``observed`` without exclusions (the online
    shapes always do), now with ``excluded``."""
    identity = parse_observed(observed)
    rules = [
        CoverageRule(coverage_type="radius", online_global=True, home_market=home),
        CoverageRule(coverage_type="online", home_market=home),
        CoverageRule(coverage_type="radius", included_markets=(observed,), home_market=home),
        CoverageRule(coverage_type="city", included_markets=(observed,), home_market=home),
        CoverageRule(coverage_type="multi_city", included_markets=(observed,), home_market=home),
    ]
    if identity is not None and identity.country:
        rules.append(
            CoverageRule(coverage_type="country", country=identity.country, home_market=home)
        )
    out = [
        CoverageRule(**{**rule.__dict__, "excluded_markets": excluded})
        for rule in rules
        if market_in_coverage(rule, observed)
    ]
    assert len(out) >= 2, (observed, home)
    return out


def _assert_removed(excluded: str, observed: str, home=None) -> None:
    for rule in _admitting_rules(observed, (excluded,), home):
        assert market_in_coverage(rule, observed) is False, (excluded, observed, rule)


def _assert_kept(excluded: str, observed: str, home=None) -> None:
    for rule in _admitting_rules(observed, (excluded,), home):
        assert market_in_coverage(rule, observed) is True, (excluded, observed, rule)


def _spellings(code: str, country: str) -> list[str]:
    """Upper, lower and spaced forms, and the forms with a country suffix."""
    prefix, part = code.split("-")
    name = country_name(country)
    return [
        code,
        code.lower(),
        f"{prefix} - {part}",
        f" {prefix.lower()} -{part.lower()} ",
        f"{prefix} {part}",
        f"{code}, {country}",
        f"{code.lower()}, {name}",
        f"{prefix} {part}, {name.upper()}",
    ]


# --------------------------------------------------------------------------- #
# D-5DW4-01: code-form exclusions
# --------------------------------------------------------------------------- #

CODE_CASES = [
    ("US-TX", "US", ["Dallas, TX", "Austin, Texas, USA", "Houston, Texas", "Texas"]),
    ("NG-LA", "NG", ["Lagos, NG", "Ikeja, Lagos, NG", "Ikeja, Lagos State, Nigeria"]),
    ("GB-ENG", "GB", ["London, England, UK", "Manchester, England", "London, UK"]),
    ("CA-ON", "CA", ["Toronto, ON", "Ottawa, Ontario, Canada"]),
    ("AU-NSW", "AU", ["Sydney, NSW", "Newcastle, New South Wales, Australia"]),
    ("US-NY", "US", ["New York, NY", "Buffalo, New York, USA"]),
]


@pytest.mark.parametrize(
    ("code", "country", "observed"), CODE_CASES, ids=[c[0] for c in CODE_CASES]
)
def test_a_code_form_exclusion_removes_its_jurisdiction_under_every_coverage_type(
    code, country, observed
):
    for spelling, market, home in itertools.product(
        _spellings(code, country), observed, [None, DALLAS, LAGOS]
    ):
        _assert_removed(spelling, market, home)


@pytest.mark.parametrize(
    ("code", "elsewhere"),
    [
        ("US-TX", ["Los Angeles, CA", "Oklahoma City, OK", "London, UK", "Lagos, NG"]),
        ("NG-LA", ["Abeokuta, Ogun, NG", "Kano, Kano State, Nigeria", "Dallas, TX"]),
        ("GB-ENG", ["Edinburgh, Scotland, UK", "Cardiff, Wales", "Dallas, TX"]),
        ("CA-ON", ["Montreal, QC", "London, UK"]),
        ("AU-NSW", ["Perth, Western Australia", "Melbourne, Victoria, Australia"]),
        ("US-NY", ["Newark, NJ", "Pittsburgh, PA"]),
    ],
)
def test_a_code_form_exclusion_keeps_what_its_jurisdiction_rules_out(code, elsewhere):
    for market in elsewhere:
        _assert_kept(code, market)


def _listed_codes() -> list[tuple[str, str, str]]:
    """(country, code, first name) for every subdivision the tables list."""
    out = []
    for country, divisions in sorted({**SUBDIVISIONS, "NG": NIGERIA}.items()):
        for code, names in sorted(divisions.items()):
            out.append((country, code, names[0]))
    return out


LISTED = _listed_codes()


@pytest.mark.parametrize(
    ("country", "code", "name"), LISTED, ids=[f"{c}-{s}" for c, s, _ in LISTED]
)
def test_every_listed_subdivision_code_removes_its_own_markets_and_no_sibling(country, code, name):
    observed = f"Somewhere, {name.title()}, {country_name(country)}"
    reading = parse_observed(observed)
    assert reading.country == country and reading.region is not None, reading
    siblings = SUBDIVISIONS.get(country) or NIGERIA
    other_code, other_names = next((c, n) for c, n in sorted(siblings.items()) if c != code)
    sibling = f"Somewhere, {other_names[0].title()}, {country_name(country)}"
    for spelling in (f"{country}-{code}", f"{country}-{code}".lower(), f"{country} - {code}"):
        assert parse_market(spelling).region == reading.region, spelling
        assert parse_market(spelling).city is None
        _assert_removed(spelling, observed)
        _assert_kept(spelling, sibling)


@pytest.mark.parametrize(
    ("excluded", "observed"),
    [
        ("KE, Kenya", "Nairobi, KE"),
        ("ke, KENYA", "Mombasa, Kenya"),
        ("GB, United Kingdom", "London, UK"),
        ("NG, Nigeria", "Lagos, NG"),
        ("US, USA", "Dallas, TX"),
        ("KE", "Nairobi, KE"),
        (" gb ", "London, UK"),
    ],
)
def test_an_alpha2_country_code_with_its_country_removes_the_country(excluded, observed):
    _assert_removed(excluded, observed)


def test_a_two_letter_city_before_another_country_stays_a_city():
    """The country-code reading is soft: "Bo, Sierra Leone" is the city Bo ("BO" is Bolivia),
    "CA, USA" is California and "GA, USA" Georgia, as before."""
    assert _ident(parse_market("Bo, Sierra Leone")) == ("bo", None, "SL")
    assert _ident(parse_market("CA, USA")) == (None, "US-CA", "US")
    assert _ident(parse_market("GA, USA")) == (None, "US-GA", "US")
    _assert_kept("Bo, Sierra Leone", "La Paz, Bolivia")


# --------------------------------------------------------------------------- #
# Codes the tables cannot place: fail closed, never a city
# --------------------------------------------------------------------------- #

UNPLACEABLE = [
    ("US-ZZ", ["Dallas, TX", "Chicago, IL", "Texas", "Springfield, US", "Springfield"]),
    ("us-zz", ["Dallas, TX"]),
    ("NG-ZZ", ["Lagos, NG", "Ikeja, Lagos State, Nigeria", "Kano, Kano, NG"]),
    ("GB-LND", ["London, UK", "London, England", "Edinburgh, Scotland, UK"]),
    ("CA-PEI", ["Toronto, ON", "Charlottetown, Prince Edward Island, Canada"]),
    ("US-ZZ, USA", ["Dallas, TX"]),
    # A country whose subdivisions are not listed: its upper-case code is still a code.
    ("DE-BY", ["Munich, Germany", "Berlin, DE"]),
    ("KE-30", ["Nairobi, KE", "Mombasa, Kenya"]),
]


@pytest.mark.parametrize(("code", "observed"), UNPLACEABLE, ids=[c for c, _ in UNPLACEABLE])
def test_an_unplaceable_code_removes_every_market_its_country_cannot_rule_out(code, observed):
    reading = parse_market(code, default_country="NG")
    assert reading.city is None, reading
    assert reading.country == code.strip()[:2].upper()
    for market, home in itertools.product(observed, HOMES):
        _assert_removed(code, market, home)
    for market in ("Paris, France", "Kampala, Uganda", "Tokyo, Japan"):
        _assert_kept(code, market)


def test_an_unplaceable_code_is_never_read_as_a_city():
    """Before the fix the exclusion "US-ZZ" was a city "us zz" (in the location's country),
    which removed no Texas market. A rule's text, with or without a location country, is never
    read as one now. An observed unplaceable code is read as words, as at the base (it names
    no region): an address's road number is no contradiction (W21-REV-5DW2FIX r1, C-1)."""
    for text, home in itertools.product(("US-ZZ", "us-zz", "NG-ZZ", "GB-LND", "DE-BY"), HOMES):
        country = home.country if home else None
        assert parse_market(text, default_country=country).city is None, (text, home)
        assert parse_observed(text).region is None, text


def test_an_unplaceable_code_rules_out_nothing_in_its_country_and_contains_nothing():
    zz = parse_market("US-ZZ")
    assert may_overlap(parse_observed("Dallas, TX"), zz) is True
    assert may_overlap(parse_observed("Springfield, US-ZZ"), parse_market("Texas")) is True
    assert may_overlap(parse_observed("London, UK"), zz) is False
    # As a scope it contains nothing, not even a market that states the same code.
    assert market_within(parse_observed("Springfield, US-ZZ"), zz) is False
    rule = CoverageRule(coverage_type="radius", included_markets=("US-ZZ",), home_market=DALLAS)
    for market in ("Dallas, TX", "Springfield, US-ZZ", "Texas", "US-ZZ"):
        assert market_in_coverage(rule, market) is False, market
    assert not ConnectorPolicy(
        source_type="rss_news", enabled=True, allowed_markets=frozenset({"US-ZZ"})
    ).permits(source_types=(), market="Dallas, TX")


CONTRADICTED = [
    ("US-TX, Canada", None, "US", ["Dallas, TX", "Houston, Texas", "Chicago, IL", "Texas"]),
    ("NG-LA, Kenya", None, "NG", ["Lagos, NG", "Kano, Kano, NG", "Abuja, Nigeria"]),
    ("Dallas, US-TX, Canada", "dallas", "US", ["Dallas, TX", "Dallas, GA"]),
]


@pytest.mark.parametrize(("text", "city", "country", "removed"), CONTRADICTED)
def test_a_code_its_own_text_contradicts_cannot_be_placed(text, city, country, removed):
    """A listed code with another country's suffix is read as its own country with a region
    that cannot be placed: it removes every market of that country it cannot rule out,
    admits nothing, clears nothing, and contradicts every location's jurisdiction."""
    code = text.split(", ")[-2]
    assert _ident(parse_market(text)) == (city, f"{code}?", country)
    assert _ident(parse_observed(text)) == _ident(parse_market(text))
    for market in removed:
        _assert_removed(text, market)
    rule = CoverageRule(coverage_type="radius", included_markets=(text,), home_market=DALLAS)
    for market in ("Dallas, TX", "Toronto, ON", "Lagos, NG", "Houston, TX"):
        assert market_in_coverage(rule, market) is False
    policy = ConnectorPolicy(source_type="rss_news", enabled=True, allowed_markets={text})
    assert not policy.permits(source_types=(), market="Dallas, TX")
    # Not even the same text: a region the tables cannot place contains nothing and lies in
    # no scope, so the code's own country never admits the self-contradicting observation
    # (the word reading still places it in the suffix's country, as at the base).
    assert market_within(parse_observed(text), parse_market(text)) is False
    assert not policy.permits(source_types=(), market=text)
    own = CoverageRule(coverage_type="country", country=country)
    assert market_in_coverage(own, text) is False, (country, text)


@pytest.mark.parametrize("address", ["US-TX, Canada", "NG-LA, Kenya", "Dallas, US-TX, Canada"])
def test_the_jurisdiction_resolver_still_refuses_a_contradicted_address(address):
    """The resolver reads a coverage rule's business address with ``parse_observed``: an
    address that names another country stays a contradiction for every location (the
    contradicted code is no location's subdivision), as the 5D-W2 contract requires."""
    for city, state, country in (
        ("Dallas", "Texas", "US"),
        ("Toronto", "Ontario", "Canada"),
        ("Lagos", "Lagos", "Nigeria"),
        ("Nairobi", "Nairobi", "Kenya"),
    ):
        location = SimpleNamespace(city=city, state_province=state, country=country)
        rule = SimpleNamespace(business_address=address)
        assert resolve_jurisdiction(location, [rule]).code == UNRESOLVED, (city, address)


def test_a_code_in_a_business_address_is_read_as_its_jurisdiction():
    """The same reading makes the resolver see codes and Nigerian states in an address: one
    that names another subdivision now contradicts the location (deny), one that names the
    location's own still agrees."""
    lagos = SimpleNamespace(city="Lagos", state_province="Lagos", country="Nigeria")
    nairobi = SimpleNamespace(city="Nairobi", state_province="Nairobi", country="Kenya")
    for address, location, code in (
        ("Ikeja, NG-LA", lagos, "NG-LA"),
        ("Lagos State, Nigeria", lagos, "NG-LA"),
        ("Ikeja, Ogun State", lagos, UNRESOLVED),
        ("NG-KE", nairobi, UNRESOLVED),
        ("Kebbi State", nairobi, UNRESOLVED),
    ):
        rule = SimpleNamespace(business_address=address)
        assert resolve_jurisdiction(location, [rule]).code == code, address


# --------------------------------------------------------------------------- #
# An observed market reads only a placeable code (W21-REV-5DW2FIX r1, C-1)
# --------------------------------------------------------------------------- #

US = "United States"
#: (location city, state, country), a realistic business address, the location's own code.
#: Road numbers and hyphenated names that look like codes; each resolved so at e3571ac8.
REALISTIC_ADDRESSES = [
    # The review's eight.
    (("Freehold", "New Jersey", US), "US-9, Freehold, NJ 07728", "US-NJ"),
    (("Freehold", "New Jersey", US), "US-9", "US-NJ"),
    (("Islamorada", "Florida", US), "Mile Marker 82, US-1, Islamorada, FL 33036", "US-FL"),
    (("Cumming", "Georgia", US), "GA-400", "US-GA"),
    (("Cumming", "Georgia", US), "Suite 100, GA-400", "US-GA"),
    (("Malibu", "California", US), "CA-1, Malibu, CA 90265", "US-CA"),
    (("Malibu", "California", US), "CA-1", "US-CA"),
    (("Bloomington", "Indiana", US), "IN-37", "US-IN"),
    (("Al Ain", "Abu Dhabi", "United Arab Emirates"), "KHALIFA STREET, AL-AIN", "AE"),
    (("Ca Mau", "Ca Mau", "Vietnam"), "Ca-Mau, Vietnam", "VN"),
    # US and state highway numbers whose prefix is also an ISO country code.
    (("Ventura", "California", US), "US-101, Ventura, CA 93001", "US-CA"),
    (("Baltimore", "Maryland", US), "Mile Marker 5, US-1, Baltimore, MD", "US-MD"),
    (("Dover", "Delaware", US), "US-9, Dover, DE 19901", "US-DE"),
    (("Annapolis", "Maryland", US), "MD-2, Annapolis, MD 21401", "US-MD"),
    (("Payson", "Arizona", US), "AZ-87, Payson, AZ 85541", "US-AZ"),
    (("Aspen", "Colorado", US), "CO-82, Aspen, CO 81611", "US-CO"),
    (("Kennebunk", "Maine", US), "ME-1, Kennebunk, ME 04043", "US-ME"),
    (("Rehoboth Beach", "Delaware", US), "DE-1, Rehoboth Beach, DE 19971", "US-DE"),
    (("Easton", "Pennsylvania", US), "PA-611, Easton, PA 18042", "US-PA"),
    (("Tuscaloosa", "Alabama", US), "AL-69, Tuscaloosa, AL 35401", "US-AL"),
    (("Lincoln", "Nebraska", US), "NE-2, Lincoln, NE 68502", "US-NE"),
    (("Grand Isle", "Louisiana", US), "LA-1, Grand Isle, LA 70358", "US-LA"),
    (("Duluth", "Minnesota", US), "MN-61, Duluth, MN 55802", "US-MN"),
    (("Gatlinburg", "Tennessee", US), "TN-73", "US-TN"),
    (("Myrtle Beach", "South Carolina", US), "SC-17, Myrtle Beach, SC 29577", "US-SC"),
    (("Leesburg", "Virginia", US), "VA-7", "US-VA"),
    (("Sonoma", "California", US), "Suite 5, CA-12", "US-CA"),
    # Arabic names with the article written "AL-".
    (("AlUla", "Medina", "Saudi Arabia"), "AL-ULA", "SA"),
    (("AlUla", "Medina", "Saudi Arabia"), "Old Town, AL-ULA, Saudi Arabia", "SA"),
    (("Kut", "Wasit", "Iraq"), "AL-KUT", "IQ"),
    (("Al Bab", "Aleppo", "Syria"), "AL-BAB, Syria", "SY"),
    (("Al Ain", "Abu Dhabi", "United Arab Emirates"), "AL-AIN", "AE"),
    # Vietnamese hyphenated names.
    (("Ca Mau", "Ca Mau", "Vietnam"), "CA-MAU", "VN"),
    (("Ba Ria", "Ba Ria-Vung Tau", "Vietnam"), "Ba-Ria, Vietnam", "VN"),
    (("Ba Ria", "Ba Ria-Vung Tau", "Vietnam"), "BA-RIA", "VN"),
    (("Ca Na", "Ninh Thuan", "Vietnam"), "Ca-Na, Ninh Thuan, Vietnam", "VN"),
]


@pytest.mark.parametrize(
    ("location", "address", "code"), REALISTIC_ADDRESSES, ids=[a for _, a, _ in REALISTIC_ADDRESSES]
)
def test_a_road_number_or_hyphenated_name_in_an_address_is_no_contradiction(
    location, address, code
):
    city, state, country = location
    resolution = resolve_jurisdiction(
        SimpleNamespace(city=city, state_province=state, country=country),
        [SimpleNamespace(business_address=address)],
    )
    assert resolution.code == code, resolution.evidence


def test_genuine_address_contradictions_still_refuse_and_agreements_still_agree():
    lagos = SimpleNamespace(city="Lagos", state_province="Lagos", country="Nigeria")
    dallas = SimpleNamespace(city="Dallas", state_province="Texas", country=US)
    for address in ("US-TX, Canada", "NG-LA, Kenya", "Dallas, US-TX, Canada", "Ikeja, Ogun State"):
        rule = SimpleNamespace(business_address=address)
        assert resolve_jurisdiction(lagos, [rule]).code == UNRESOLVED, address
    for location, address, code in (
        (lagos, "Ikeja, NG-LA", "NG-LA"),
        (lagos, "Lagos State, Nigeria", "NG-LA"),
        (dallas, "Dallas, US-TX", "US-TX"),
        (dallas, "4400 US-9, Dallas, TX 75201", "US-TX"),
    ):
        rule = SimpleNamespace(business_address=address)
        assert resolve_jurisdiction(location, [rule]).code == code, address


def test_an_observed_market_reads_only_a_placeable_code():
    for text in ("US-9", "GA-400", "IN-37", "CA-1", "AL-AIN", "Ca-Mau", "DE-BY", "US-ZZ"):
        assert parse_observed(text) == parse_observed(text, jurisdiction_forms=False), text
    assert _ident(parse_observed("US-TX")) == (None, "US-TX", "US")
    assert _ident(parse_observed("ng-la")) == (None, "NG:lagos", "NG")
    # A rule's text keeps the fail-closed reading of the same spelling.
    assert _ident(parse_market("US-9")) == (None, "US-9", "US")
    assert _ident(parse_market("IN-37")) == (None, "IN-37", "IN")


@pytest.mark.parametrize(
    ("country", "markets_outside"),
    [
        ("India", ["IN-37", "Bloomington, IN-37"]),
        ("Canada", ["CA-12", "Suite 5, CA-12", "CA-1, Malibu, CA 90265"]),
        ("Albania", ["AL-69", "AL-ULA", "KHALIFA ST, AL-AIN"]),
        ("Gabon", ["GA-400"]),
        ("United States", ["US-AZ, Australia"]),
    ],
)
def test_a_road_number_widens_no_country_rule(country, markets_outside):
    rule = CoverageRule(coverage_type="country", country=country)
    for market in markets_outside:
        assert market_in_coverage(rule, market) is False, (country, market)


def test_a_road_number_keeps_its_clearance_and_its_fixture_market():
    for allowed, market in (
        ("DE, USA", "US-9, Dover, DE 19901"),
        ("Kentucky, US", "US-9, Louisville, KY 40202"),
        ("Maryland, US", "Mile Marker 5, US-1, Baltimore, MD"),
        ("Florida, US", "Mile Marker 82, US-1, Islamorada, FL 33036"),
    ):
        policy = ConnectorPolicy(source_type="rss_news", enabled=True, allowed_markets={allowed})
        assert policy.permits(source_types=(), market=market), (allowed, market)
    for market in ("US-9, Dallas, TX 75201", "Mile Marker 5, US-1, Dallas, TX"):
        signals = fixtures_for_market(market)
        assert signals and {s.market for s in signals} == {"Dallas, TX"}, market
        assert sample_feed_covers(market), market


@pytest.mark.parametrize("excluded", ["Ogun State", "NG-OG", "Kano State", "FCT, Nigeria"])
def test_another_nigerian_state_excluded_removes_a_lagos_market_that_names_no_state(excluded):
    """Recorded breadth (W21-REV-5DW2FIX r1, decision 5; item 35): "Lagos, NG" names no state,
    so an exclusion of another Nigerian state cannot rule it out. A market that names Lagos
    State is kept."""
    for rule in (
        CoverageRule(coverage_type="online", excluded_markets=(excluded,), home_market=LAGOS),
        CoverageRule(
            coverage_type="radius",
            included_markets=("Lagos, NG",),
            excluded_markets=(excluded,),
            home_market=LAGOS,
        ),
    ):
        assert market_in_coverage(rule, "Lagos, NG") is False, (excluded, rule)
        assert market_in_coverage(rule, "Lagos, Lagos State, Nigeria") is True, (excluded, rule)


def test_a_hyphenated_place_name_is_not_a_code():
    """Only an upper-case code of a country without listed subdivisions reads as a code, and
    not when the text names another country: "Al-Ain" and "Al-Ain, UAE" are the city."""
    assert _ident(parse_market("Al-Ain")) == ("al ain", None, None)
    assert _ident(parse_market("Al-Ain, UAE")) == ("al ain", None, "AE")
    assert _ident(parse_market("AL-AIN, UAE")) == ("al ain", None, "AE")
    assert _ident(parse_market("La-Paz")) == ("la paz", None, None)


# --------------------------------------------------------------------------- #
# D-5DW4-02: Nigerian states named in words
# --------------------------------------------------------------------------- #


def _state_words(names: tuple[str, ...]) -> str:
    title = names[0].title()
    return title if names[0].endswith(" territory") else f"{title} State"


NG_STATES = sorted(NIGERIA.items())


@pytest.mark.parametrize(("code", "names"), NG_STATES, ids=[c for c, _ in NG_STATES])
def test_a_nigerian_state_in_words_removes_markets_in_that_state(code, names):
    words = _state_words(names)
    other_code, other_names = next((c, n) for c, n in NG_STATES if c != code)
    inside = [
        f"Somewhere, {names[0].title()}, NG",
        f"Somewhere, {words}, Nigeria",
        f"Somewhere, {names[0].title()} State, NG",
    ]
    outside = [f"Somewhere, {other_names[0].title()}, NG", "Dallas, TX", "Nairobi, KE"]
    for spelling in (words, f"{words}, Nigeria", f"{words}, NG", words.lower(), words.upper()):
        for market, home in itertools.product(inside, [None, LAGOS, DALLAS]):
            _assert_removed(spelling, market, home)
        for market in outside:
            _assert_kept(spelling, market)


@pytest.mark.parametrize(
    ("excluded", "observed", "home"),
    [
        # The 5DW4 reproduction's cases.
        ("Lagos State", "Ikeja, Lagos, NG", LAGOS),
        ("Lagos State", "Lagos, NG", LAGOS),
        ("Lagos State, Nigeria", "Ikeja, Lagos State, Nigeria", None),
        ("Lagos State, NG", "Lagos, Lagos State, NG", None),
        ("Ogun State, Nigeria", "Abeokuta, Ogun State, Nigeria", None),
        # Every spelling of the FCT is one subdivision.
        ("Federal Capital Territory", "Garki, Abuja, Nigeria", None),
        ("Federal Capital Territory, Nigeria", "Garki, FCT, Nigeria", None),
        ("NG-FC", "Garki, Federal Capital Territory, Nigeria", None),
        ("NG-FC", "Garki, Abuja, Nigeria", None),
        ("NG-NA", "Lafia, Nassarawa, Nigeria", None),
        ("NG-LA", "Ikeja, LA, Nigeria", None),
    ],
)
def test_the_reproduced_cases_and_the_aliases(excluded, observed, home):
    _assert_removed(excluded, observed, home)


@pytest.mark.parametrize(("code", "names"), NG_STATES, ids=[c for c, _ in NG_STATES])
def test_both_parsers_read_a_nigerian_subdivision_the_same_way(code, names):
    """Symmetric: the words, the code and the region after a city all give one key."""
    words = _state_words(names)
    key = parse_market(f"NG-{code}").region
    assert key == f"NG:{names[0]}"
    for text in (words, f"{words}, Nigeria", f"{words}, NG", f"NG-{code}", f"ng {code.lower()}"):
        assert _ident(parse_market(text)) == (None, key, "NG"), text
        assert _ident(parse_observed(text)) == _ident(parse_market(text)), text
    for text in (f"Town, {words}, Nigeria", f"Town, {names[0]}, NG", f"Town, {words}"):
        assert _ident(parse_observed(text)) == ("town", key, "NG"), text
        assert _ident(parse_market(text)) == ("town", key, "NG"), text
    for alias in names[1:]:
        assert parse_observed(f"Town, {alias}, Nigeria").region == key, alias


def test_nigerian_subdivisions_are_read_from_the_jurisdiction_module_only():
    """One source: the reading is derived from ``SUBDIVISION_JURISDICTIONS`` (no second copy),
    and Nigeria is still absent from the market table, so a bare "Lagos" is the city."""
    assert "NG" not in SUBDIVISIONS
    assert set(markets._JURISDICTION_REGION_NAMES) == {"NG"}
    assert set(markets._JURISDICTION_REGION_NAMES["NG"].values()) == {
        names[0] for names in NIGERIA.values()
    }
    assert set(markets._JURISDICTION_REGION_TOKENS["NG"]) >= {c.lower() for c in NIGERIA}
    assert _ident(parse_market("Lagos, NG")) == ("lagos", None, "NG")
    assert _ident(parse_market("Lagos, Nigeria")) == ("lagos", None, "NG")
    assert _ident(parse_market("Lagos", default_country="NG")) == ("lagos", None, "NG")
    assert _ident(parse_market("Kano, Nigeria")) == ("kano", None, "NG")
    assert _ident(parse_market("Abuja, Nigeria")) == ("abuja", None, "NG")
    assert _ident(parse_market("Niger")) == (None, None, "NE")
    # A state's name before another country is a name in that country, as before.
    assert _ident(parse_market("Delta State, US")) == ("delta state", None, "US")


# --------------------------------------------------------------------------- #
# Same-name readings: a bare name that is also a Nigerian state (REV-5DW4 C-1, item 35)
# --------------------------------------------------------------------------- #

HOMONYM_EXCLUSIONS = [
    # (exclusion, home, removed: the state cannot be ruled out, kept: ruled out)
    (
        "Lagos, NG",
        None,
        ["Ikeja, Lagos, NG", "Ikeja, Lagos State, Nigeria", "Lagos, Nigeria", "Lagos, Lagos, NG"],
        ["Lagos, Portugal", "Kano, Kano, NG", "Abeokuta, Ogun State, Nigeria", "Dallas, TX"],
    ),
    ("Lagos", None, ["Ikeja, Lagos State, Nigeria", "Lagos, NG"], ["Kano, Kano, NG"]),
    ("Lagos", LAGOS, ["Ikeja, Lagos, NG", "Lagos, NG"], ["Kano, Kano, NG"]),
    ("Kano, Nigeria", None, ["Fagge, Kano State, Nigeria", "Kano, NG"], ["Ikeja, Lagos, NG"]),
    (
        "Abuja, Nigeria",
        None,
        ["Garki, FCT, Nigeria", "Garki, Federal Capital Territory, Nigeria"],
        ["Ikeja, Lagos, NG"],
    ),
    # "Niger" is the country Niger and Niger State: both are removed, and so is every Nigerian
    # market that states no state (as a "Texas" exclusion removes "Little Rock, US").
    (
        "Niger",
        None,
        ["Niamey, Niger", "Niger State, NG", "Minna, Niger State, Nigeria", "Nigeria", "Lagos, NG"],
        ["Kano, Kano, NG", "Ikeja, Lagos State, Nigeria", "Accra, Ghana", "Dallas, TX"],
    ),
    (
        "Minna, Niger",
        None,
        ["Minna, Niger State, Nigeria", "Minna, NE"],
        ["Niamey, Niger", "Kano, Kano, NG"],
    ),
    # The last qualifier, read in a US location's country, is also the Nigerian state.
    ("Ikeja, Lagos", DALLAS, ["Ikeja, Lagos, NG", "Ikeja, Lagos State, Nigeria"], ["Dallas, TX"]),
]


@pytest.mark.parametrize(
    ("excluded", "home", "removed", "kept"),
    HOMONYM_EXCLUSIONS,
    ids=[f"{e}|{h.label if h else '-'}" for e, h, _, _ in HOMONYM_EXCLUSIONS],
)
def test_an_ambiguous_exclusion_removes_what_either_place_covers(excluded, home, removed, kept):
    for market in removed:
        _assert_removed(excluded, market, home)
    for market in kept:
        _assert_kept(excluded, market, home)


def test_a_name_before_another_country_or_a_state_code_is_not_a_nigerian_state():
    """ "Lagos, Portugal" names Portugal's Lagos; "KE" is Kenya, never Kebbi (codes are not read
    as homonyms); a suffixed state is unambiguous and has one reading."""
    _assert_kept("Lagos, Portugal", "Ikeja, Lagos State, Nigeria")
    _assert_kept("Lagos, Portugal", "Lagos, NG")
    _assert_kept("KE", "Birnin Kebbi, Kebbi State, Nigeria")
    for text in ("Lagos, Portugal", "KE", "Nigeria", "Lagos State", "Lagos, Lagos, NG"):
        assert exclusion_readings(text) == [parse_market(text)], text
    for text in ("KE", "LA", "ON", "KE, Kenya"):
        assert all(r.country != "NG" for r in exclusion_readings(text)), text


def test_an_ambiguous_inclusion_still_admits_only_what_it_did():
    """No widening: "CA" alone is still read once (unplaced, or in the location's country)."""
    bare = CoverageRule(coverage_type="radius", included_markets=("CA",))
    for market in ("Toronto, ON", "Los Angeles, CA"):
        assert market_in_coverage(bare, market) is False
    us = CoverageRule(coverage_type="radius", included_markets=("CA",), home_market=DALLAS)
    assert market_in_coverage(us, "Los Angeles, CA") is True
    assert market_in_coverage(us, "Toronto, ON") is False
    policy = ConnectorPolicy(source_type="rss_news", enabled=True, allowed_markets={"CA"})
    assert not policy.permits(source_types=(), market="Toronto, ON")


@pytest.mark.parametrize(
    ("included", "admitted", "refused"),
    [
        ("Lagos, NG", ["Lagos, NG", "Lagos, Nigeria"], ["Ikeja, Lagos, NG", "Lagos State"]),
        ("Niger", ["Niamey, Niger"], ["Nigeria", "Niger State, NG", "Minna, Niger State, NG"]),
        ("Kano, Nigeria", ["Kano, NG"], ["Fagge, Kano State, Nigeria"]),
        ("Niger State", ["Minna, Niger State, Nigeria"], ["Niamey, Niger", "Niger", "Nigeria"]),
    ],
)
def test_an_ambiguous_inclusion_or_clearance_admits_only_what_it_did(included, admitted, refused):
    """No widening from ambiguity: an inclusion and a connector clearance read a bare name once,
    as before (the suffixed state is the named jurisdiction-form change)."""
    rule = CoverageRule(coverage_type="radius", included_markets=(included,))
    policy = ConnectorPolicy(source_type="rss_news", enabled=True, allowed_markets={included})
    for market in admitted:
        assert market_in_coverage(rule, market) is True, (included, market)
        assert policy.permits(source_types=(), market=market), (included, market)
    for market in refused:
        assert market_in_coverage(rule, market) is False, (included, market)
        assert not policy.permits(source_types=(), market=market), (included, market)


# --------------------------------------------------------------------------- #
# Operator rulings X-1 to X-5 (2026-11-08): exclusion semantics. These replace the
# blanket monotonicity requirement (the narrowing is measured and disclosed in the lane's
# evidence).
# --------------------------------------------------------------------------- #

NIGER_HOME = market_from_parts(city="Niamey", region=None, country="Niger")
TEXAS_ELSEWHERE = ["Dallas, TX", "Austin, Texas, USA"]


def _online_only(excluded: str, observed: str, home=None) -> bool:
    """The online decision for one exclusion (no inclusion can interfere)."""
    return market_in_coverage(
        CoverageRule(coverage_type="online", excluded_markets=(excluded,), home_market=home),
        observed,
    )


@pytest.mark.parametrize(
    "excluded", ["NG-LA", "ng - la", "NG LA", "Lagos State", "Lagos State, Nigeria", "NG-LA, NG"]
)
def test_x1_an_explicit_identifier_is_that_jurisdiction_only(excluded):
    """X-1: "explicit, valid geographic identifiers take precedence over erroneous legacy
    interpretations. NG-LA identifies Lagos State, Nigeria. An observation positively
    identified as Kenya cannot overlap it." """
    assert [_ident(r) for r in exclusion_readings(excluded)] == [(None, "NG:lagos", "NG")]
    for observed in ("Kenya", "Nairobi, KE", "Texas", "Dallas, TX", "London, UK", "Ghana"):
        _assert_kept(excluded, observed)
    for observed in ("Abeokuta, Ogun State, Nigeria", "Kano, Kano, NG", "NG-OG", "Niger State"):
        _assert_kept(excluded, observed)
    for observed in ("Ikeja, Lagos, NG", "Ikeja, Lagos State, Nigeria", "NG-LA", "Lagos State"):
        _assert_removed(excluded, observed)


@pytest.mark.parametrize("excluded", ["NG-LA", "Lagos State", "US-TX", "US-ZZ"])
def test_x1_missing_geography_is_still_matched_conservatively(excluded):
    """X-1: "Retain conservative matching when geography is genuinely missing or ambiguous":
    a market that names no country, or no subdivision of a country that has them, cannot be
    ruled out and is removed (item 35)."""
    country = parse_market(excluded).country
    for observed in ("London", "Springfield", "anywhere", None, "", country_name(country)):
        assert _online_only(excluded, observed) is False, (excluded, observed)


def test_x1_codes_and_identified_observations_never_overlap_by_a_word_misreading():
    """X-1 for the other explicit forms: none keeps the old city misreading ("us zz", "ca",
    "ng"), so none removes a market positively identified elsewhere."""
    for excluded in ("US-ZZ", "GB-LND", "NG-ZZ", "CA", "NE", "GA", "Georgia", "KE-30"):
        for observed in ("Kenya", "Nairobi, KE", "Lima, Peru"):
            if parse_observed(observed).country not in {
                r.country for r in exclusion_readings(excluded)
            }:
                assert _online_only(excluded, observed) is True, (excluded, observed)
    # An observation identified by an explicit form is that place only.
    for excluded, observed in (("Texas", "NG-LA"), ("Kenya", "NG-LA"), ("Kenya", "Lagos State")):
        assert _online_only(excluded, observed) is True, (excluded, observed)


def test_x1_a_code_that_may_be_a_place_name_keeps_both_readings():
    """Not an explicit valid identifier: an upper-case code of a country whose subdivisions
    are not listed ("AL-AIN") may be the place Al Ain, so its word reading is kept."""
    assert _online_only("AL-AIN", "Al Ain, AE", AL_AIN) is False
    assert _online_only("AL-AIN", "Tirana, Albania", AL_AIN) is False
    assert _online_only("AL-AIN", "Kenya", AL_AIN) is True
    assert _online_only("AL-AIN", "Dubai, AE", AL_AIN) is True


def test_x1_an_unplaceable_code_keeps_a_word_reading_that_names_an_area():
    """W21-REV-5DW2FIX r2 C-1: an unplaceable code is not an explicit valid identifier, so its
    geography stays ambiguous (X-1, conservative). A US location's state-route text ("GA-400",
    "CA-1", "AL-69") is also the state its words name there; never a city misreading."""
    for excluded, market in (
        ("GA-400", "Atlanta, GA"),
        ("CA-1", "Los Angeles, CA"),
        ("AL-69", "Birmingham, Alabama"),
        ("AL-69", "Tuscaloosa, AL"),
        ("PA-611", "Philadelphia, PA"),
    ):
        assert _online_only(excluded, market, DALLAS) is False, (excluded, market)
    # The code's own country is still excluded, and nothing else.
    assert _online_only("GA-400", "Libreville, GA", DALLAS) is False
    assert _online_only("GA-400", "Seattle, WA", DALLAS) is True
    # With no location the words name no area, so only the code's country applies.
    assert _online_only("GA-400", "Atlanta, GA") is True
    assert _online_only("GA-400", "Libreville, GA") is False
    # "US-ZZ" names no area in words: still never a market identified as Kenya.
    assert _online_only("US-ZZ", "Kenya", DALLAS) is True


def test_x1_a_contradicted_code_is_its_country_and_the_country_its_text_names():
    for observed in ("Dallas, TX", "Houston, Texas", "Toronto, ON", "Canada"):
        assert _online_only("US-TX, Canada", observed) is False, observed
    for observed in ("London, UK", "Lagos, NG", "Kenya"):
        assert _online_only("US-TX, Canada", observed) is True, observed


@pytest.mark.parametrize("home", [LONDON, LAGOS, NAIROBI])
def test_x1_a_named_subdivision_is_that_subdivision_for_any_location(home):
    """An excluded "Texas" for a location outside the US was a local place (a city "texas" in the
    location's country); the named subdivision now wins and the local reading stays."""
    for observed in TEXAS_ELSEWHERE:
        assert _online_only("Texas", observed, home) is False, (home, observed)
    assert _online_only("Texas", "Little Rock, AR", home) is True


NIGER_COUNTRY_ONLY = ["Republic of Niger", "Niger Republic", "republic of niger", "NE, Niger"]
NIGER_STATE_ONLY = ["Niger State", "Niger State, Nigeria", "Niger State, NG", "NG-NI", "ng-ni"]
NIGER_COUNTRY_MARKETS = ["Niamey, Niger", "Zinder, NE", "Niger Republic"]
NIGER_STATE_MARKETS = ["Minna, Niger State, Nigeria", "Minna, Niger, NG", "Niger State, NG"]


def test_x2_a_bare_niger_covers_both():
    """X-2: "an ambiguous bare 'Niger' may cover both country Niger and Niger State." """
    for home in (None, DALLAS, LONDON):
        for observed in NIGER_COUNTRY_MARKETS + NIGER_STATE_MARKETS:
            assert _online_only("Niger", observed, home) is False, (home, observed)
        for observed in ("Kano, Kano, NG", "Ikeja, Lagos State, Nigeria", "Accra, Ghana"):
            assert _online_only("Niger", observed, home) is True, (home, observed)


@pytest.mark.parametrize("excluded", NIGER_COUNTRY_ONLY)
def test_x2_the_country_niger_alone(excluded):
    """X-2: "Preserve unambiguous ways to specify country Niger and Nigerian Niger State
    separately." """
    assert {_ident(r) for r in exclusion_readings(excluded)} == {(None, None, "NE")}
    for observed in NIGER_COUNTRY_MARKETS:
        assert _online_only(excluded, observed) is False, observed
    for observed in NIGER_STATE_MARKETS + ["Nigeria", "Lagos, NG", "Kano, Kano, NG"]:
        assert _online_only(excluded, observed) is True, observed


@pytest.mark.parametrize("excluded", NIGER_STATE_ONLY)
def test_x2_niger_state_alone(excluded):
    assert {_ident(r) for r in exclusion_readings(excluded)} == {(None, "NG:niger", "NG")}
    for observed in NIGER_STATE_MARKETS:
        assert _online_only(excluded, observed) is False, observed
    for observed in NIGER_COUNTRY_MARKETS + ["Kano, Kano, NG", "Ikeja, Lagos State, Nigeria"]:
        assert _online_only(excluded, observed) is True, observed


def test_x2_a_known_country_constrains_the_readings():
    """X-2: "A known country must constrain the possible readings": place knowledge
    (KNOWN_CITIES) and the location's country."""
    # "Niamey, Niger": Niamey is a principal city of the country Niger only.
    assert [_ident(r) for r in exclusion_readings("Niamey, Niger")] == [("niamey", None, "NE")]
    assert _online_only("Niamey, Niger", "Minna, Niger State, Nigeria") is True
    assert _online_only("Niamey, Niger", "Niamey, NE") is False
    # "Minna, Niger" for a Nigerian location cannot be the country Niger ...
    assert [_ident(r) for r in exclusion_readings("Minna, Niger", default_country="NG")] == [
        ("minna", "NG:niger", "NG")
    ]
    assert _online_only("Minna, Niger", "Minna, Niger State, Nigeria", LAGOS) is False
    assert _online_only("Minna, Niger", "Minna, NE", LAGOS) is True
    # ... and with no known country it is both.
    assert _online_only("Minna, Niger", "Minna, NE") is False
    assert _online_only("Minna, Niger", "Minna, Niger State, Nigeria") is False
    # A bare "Niger" for a location in Niger is the country only. For a Nigerian location it
    # is both (W21-REV-5DW2FIX r2 C-2: the location's country narrows a bare name only to
    # readings it already had there, and the country Niger was one; X-2 "may cover both").
    assert _online_only("Niger", "Niamey, Niger", NIGER_HOME) is False
    assert _online_only("Niger", "Minna, Niger State, Nigeria", NIGER_HOME) is True
    for market in ("Minna, Niger State, Nigeria", "Niamey, Niger", "Zinder, NE", "Maradi, Niger"):
        assert _online_only("Niger", market, LAGOS) is False, market
    assert {_ident(r) for r in exclusion_readings("Niger", default_country="NG")} == {
        (None, None, "NE"),
        (None, "NG:niger", "NG"),
    }
    # A narrowing that reproduces the location's own reading stays: "CA" for a US location
    # is California, "Georgia" the US state.
    assert [_ident(r) for r in exclusion_readings("CA", default_country="US")] == [
        (None, "US-CA", "US")
    ]
    assert [_ident(r) for r in exclusion_readings("Georgia", default_country="US")] == [
        (None, "US-GA", "US")
    ]
    # "Nigeria" is never read as a Niger place.
    assert [_ident(r) for r in exclusion_readings("Nigeria")] == [(None, None, "NG")]
    assert [_ident(r) for r in observed_readings("Nigeria")] == [(None, None, "NG")]


def test_x2_the_location_adds_only_an_area_reading_never_a_local_city():
    """W21-REV-5DW2FIX r2 C-3: the location's own reading is kept only when it names an area
    (C-2's guard). "WA" for a Canadian location, or "NT" for a US one, is read there as a
    local place name, which is a city reading: it must not join the exclusion's readings."""
    canada = market_from_parts(city="Toronto", region="Ontario", country="Canada")
    assert all(r.city is None for r in exclusion_readings("WA", default_country="CA"))
    for observed in ("BC", "Canada"):
        assert _online_only("WA", observed, canada) is True, observed
    assert _online_only("NT", "Texas", DALLAS) is True


def test_x2_ne_is_niger_and_nebraska_narrowed_by_the_location():
    """ "NE" is the country Niger and the US state Nebraska (never the old city "ne")."""
    assert {_ident(r) for r in exclusion_readings("NE")} == {
        (None, None, "NE"),
        (None, "US-NE", "US"),
    }
    for observed in ("Niamey, Niger", "Omaha, NE"):
        assert _online_only("NE", observed) is False, observed
    for observed in ("Kenya", "Nigeria", "Lagos, NG", "Minna, Niger State, Nigeria"):
        assert _online_only("NE", observed) is True, observed
    assert _online_only("NE", "Niamey, Niger", DALLAS) is True  # a US location: Nebraska
    assert _online_only("NE", "Omaha, NE", DALLAS) is False
    assert _online_only("NE", "Omaha, NE", NIGER_HOME) is True  # a Niger location: Niger
    assert _online_only("NE", "Niamey, Niger", NIGER_HOME) is False


def test_x3_every_plausible_observed_reading():
    """X-3: "evaluate every plausible reading of an observed market, consistently with its
    recorded country, recognised codes and qualifiers." """
    # "Minna, Niger" may be in Niger State; "Niamey, Niger" is the country (place knowledge).
    for excluded in NIGER_STATE_ONLY:
        assert _online_only(excluded, "Minna, Niger") is False, excluded
        assert _online_only(excluded, "Niger") is False, excluded
        assert _online_only(excluded, "Niamey, Niger") is True, excluded
    # "York, PA" is York in Pennsylvania or in Panama: never in New York, never in Kenya.
    assert {_ident(r) for r in observed_readings("York, PA")} == {
        ("york", "US-PA", "US"),
        ("york", None, "PA"),
    }
    for excluded, removed in (("Pennsylvania", False), ("Panama", False), ("New York", True)):
        assert _online_only(excluded, "York, PA") is removed, excluded
    assert _online_only("Kenya", "York, PA") is True
    # "CA" observed is Canada or California.
    assert _online_only("Ontario", "CA") is False
    assert _online_only("California", "CA") is False
    assert _online_only("Kenya", "CA") is True


def test_x3_inclusions_and_clearance_read_an_observed_market_once():
    rule = CoverageRule(coverage_type="radius", included_markets=("Niger State",))
    policy = ConnectorPolicy(source_type="rss_news", enabled=True, allowed_markets={"Niger State"})
    for market in ("Minna, Niger", "Niger", "CA"):
        assert market_in_coverage(rule, market) is False, market
        assert not policy.permits(source_types=(), market=market), market


@pytest.mark.parametrize("excluded", ["New York", "new york", "NEW YORK"])
def test_x4_new_york_covers_the_city_and_the_state(excluded):
    """X-4: "ambiguous city/state exclusions such as 'New York' cover both readings." """
    for home in (None, DALLAS):
        for observed in ("New York, NY", "Buffalo, New York, USA", "Albany, NY", "New York City"):
            assert _online_only(excluded, observed, home) is False, (home, observed)
        for observed in ("Newark, NJ", "London, UK", "Pittsburgh, PA", "York, PA"):
            assert _online_only(excluded, observed, home) is True, (home, observed)


def test_x4_washington_and_quebec_cover_the_city_and_the_state_or_province():
    for observed in ("Seattle, WA", "Spokane, Washington, USA", "Washington, DC"):
        assert _online_only("Washington", observed) is False, observed
    assert _online_only("Washington", "Portland, OR") is True
    for observed in ("Montreal, QC", "Quebec City, QC", "Quebec, QC"):
        assert _online_only("Quebec", observed) is False, observed
    assert _online_only("Quebec", "Toronto, ON") is True


def test_x4_explicit_qualifiers_narrow_the_interpretation():
    """X-4: "Explicit qualifiers narrow the interpretation." """
    for city_only in ("New York City", "NYC", "New York, NY", "New York, New York, USA"):
        assert _online_only(city_only, "New York, NY") is False, city_only
        for observed in ("Buffalo, New York, USA", "Albany, NY"):
            assert _online_only(city_only, observed) is True, (city_only, observed)
    for state in ("New York State", "NY", "US-NY"):
        for observed in ("New York, NY", "Buffalo, New York, USA", "Albany, NY"):
            assert _online_only(state, observed) is False, (state, observed)
        assert _online_only(state, "Newark, NJ") is True
    assert _online_only("Quebec City", "Quebec City, QC") is False
    assert _online_only("Quebec City", "Montreal, QC") is True
    # As an observation, a city name with no country stays a place anywhere (item 35).
    for observed in ("New York City", "Quebec City"):
        assert _online_only("Kenya", observed) is False, observed


@pytest.mark.parametrize("excluded", ["New York", "New York City", "New York, NY", "NY"])
def test_x4_word_boundaries_and_york_gb(excluded):
    """X-4: "Preserve word boundaries and distinguish unrelated places such as York, GB." """
    for observed in ("York, GB", "York, UK", "York, England, UK", "York, Ontario, Canada"):
        assert _online_only(excluded, observed) is True, (excluded, observed)
    for york in ("York", "York, UK", "York, PA"):
        assert _online_only(york, "New York, NY") is True, york


def test_x4_an_unqualified_york_cannot_be_ruled_out_of_new_york_state():
    """The outcome implemented for a bare "York" (ambiguous geography, X-1's conservative
    rule): it names no country, so New York State cannot be ruled out and it is removed by an
    exclusion that covers the state; it is kept by one that names only the city."""
    assert _online_only("New York", "York") is False
    assert _online_only("New York State", "York") is False
    assert _online_only("New York City", "York") is True
    assert _online_only("New York, NY", "York") is True
    # The same rule the other way (X-3): an observed bare "New York" may be the state, and an
    # excluded York that names no country cannot be ruled out of it. A York that states its
    # country, or a location outside the US, rules it out.
    assert _online_only("York", "New York") is False
    assert _online_only("York", "New York", LONDON) is True
    for york in ("York, UK", "York, GB", "York, England, UK"):
        assert _online_only(york, "New York") is True, york
    assert _online_only("York", "New York, NY") is True


def test_x5_no_state_is_invented_for_a_market_that_names_none():
    """X-5: "record the disclosed Lagos, NG consequence under item 35. Do not invent a missing
    state." An observed "Lagos, NG" names no state (and so a Nigerian state exclusion removes
    it: test_another_nigerian_state_excluded_removes_a_lagos_market_that_names_no_state)."""
    for observed in ("Lagos, NG", "Lagos", "Kano, Nigeria", "Abuja, Nigeria"):
        assert all(r.region is None for r in observed_readings(observed)), observed
    assert _online_only("Ikeja", "Lagos, NG", LAGOS) is True
    assert _online_only("Ogun State", "Lagos, NG", LAGOS) is False


# --------------------------------------------------------------------------- #
# The named inclusion and clearance change, and its bound
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("included", "inside", "outside"),
    [
        (
            "US-TX",
            ["Dallas, TX", "Austin, Texas", "Texas"],
            ["Los Angeles, CA", "Oklahoma City, OK"],
        ),
        ("us tx, USA", ["Dallas, TX"], ["Los Angeles, CA", "Sacramento, California"]),
        ("NG-LA", ["Ikeja, Lagos, NG", "Ikeja, Lagos State, Nigeria"], ["Abeokuta, Ogun, NG"]),
        ("Lagos State, Nigeria", ["Ikeja, Lagos, NG"], ["Kano, Kano, NG", "Lagos, Portugal"]),
        ("GB-ENG", ["London, England"], ["Edinburgh, Scotland", "London, ON"]),
        ("KE, Kenya", ["Nairobi, KE"], ["Kampala, Uganda"]),
    ],
)
def test_a_code_or_state_inclusion_now_admits_its_jurisdiction_and_nothing_else(
    included, inside, outside
):
    for coverage_type in ("radius", "city", "multi_city"):
        rule = CoverageRule(coverage_type=coverage_type, included_markets=(included,))
        for market in inside:
            assert market_in_coverage(rule, market) is True, (included, market)
        for market in [*outside, "London, UK", "Lagos", "Dallas"]:
            assert market_in_coverage(rule, market) is False, (included, market)


def test_connector_clearance_by_code_admits_its_jurisdiction_only():
    policy = ConnectorPolicy(
        source_type="rss_news", enabled=True, allowed_markets=frozenset({"US-TX", "NG-LA"})
    )
    for market in ("Dallas, TX", "Austin, Texas, USA", "Ikeja, Lagos, NG"):
        assert policy.permits(source_types=(), market=market), market
    for market in ("Los Angeles, CA", "Abeokuta, Ogun, NG", "London, UK", None, "Dallas"):
        assert not policy.permits(source_types=(), market=market), market


def test_the_word_reading_still_admits_what_it_admitted():
    """An inclusion never admits less: "AL-AIN" (the code reading is unplaceable) still admits
    the city Al Ain of a UAE location, as its word reading did."""
    rule = CoverageRule(coverage_type="radius", included_markets=("AL-AIN",), home_market=AL_AIN)
    assert market_in_coverage(rule, "Al Ain, AE") is True
    assert market_in_coverage(rule, "Tirana, Albania") is False


def test_the_seeded_and_fixture_market_texts_read_the_same_either_way():
    """The bound on the seeded four-market world: no seeded market, location field or fixture
    key changes reading, so no seeded scoring input can move (the scoring snapshot in the
    lane's evidence shows it end to end)."""
    texts = {c["market"] for c in CITIES} | set(ALL_MARKETS) | set(_MARKET_FEEDS)
    assert len(texts) == 4
    for text in texts:
        for country in (None, "US", "GB", "NG", "KE"):
            assert parse_market(text, default_country=country) == parse_market(
                text, default_country=country, jurisdiction_forms=False
            ), (text, country)
        assert parse_observed(text) == parse_observed(text, jurisdiction_forms=False), text


# --------------------------------------------------------------------------- #
# Display and source-text paths stay as they were
# --------------------------------------------------------------------------- #


def test_the_source_text_helper_keeps_the_word_reading():
    assert qualify_for_location("Lagos State", country="NG") == "Lagos State, NG"
    assert qualify_for_location("Lagos, Lagos", country="NG") == "Lagos, Lagos, NG"
    assert qualify_for_location("NG-LA", country="NG") == "NG-LA"
    assert qualify_for_location("US-TX", country="US") == "US-TX"


def test_evidence_pooling_keeps_the_word_reading():
    """``resolve_geo`` pools wordings of one place to pick the reported market; it is not a
    coverage decision and pools exactly as before: "US-TX" and "Texas" stay apart."""
    rule = CoverageRule(coverage_type="online")
    result = resolve_geo(rule, [("local_source", "US-TX"), ("text_mention", "Texas")])
    assert (result.resolved_market, result.confidence) == ("US-TX", 0.65)
    assert result.inside_scout_area is True


def test_labels_are_the_text_as_written():
    for text in ("US-TX", " ng - la ", "Lagos State, Nigeria", "US-TX, Canada"):
        assert parse_market(text).label == text.strip()
        assert parse_observed(text).label == text.strip()


def test_country_table_is_unchanged_by_the_code_reading():
    """A two-letter code alone is still the country (it worked before the fix)."""
    for code in ("GB", "KE", "NG", "US", "gb"):
        assert _ident(parse_market(code)) == (None, None, code.upper())
    assert len(COUNTRIES) == 249


# --------------------------------------------------------------------------- #
# FU-E (W21-REV-5DW2FIX r3): a listed code written before the town in a business address
# --------------------------------------------------------------------------- #

HARTFORD_LOCATION = SimpleNamespace(
    city="Hartford", state_province="Connecticut", country="United States"
)
DALLAS_LOCATION = SimpleNamespace(city="Dallas", state_province="Texas", country="United States")
TORONTO_LOCATION = SimpleNamespace(city="Toronto", state_province="Ontario", country="Canada")
LONDON_LOCATION = SimpleNamespace(city="London", state_province="England", country="United Kingdom")

#: The review's eight code-first forms (r3 §4.2), written for a Hartford, Connecticut location.
CODE_FIRST_FORMS = [
    "US-CT, Hartford",
    "US-CT, Hartford, United States",
    "US-CT, Hartford, US",
    "US-CT, Hartford 06103",
    "1 Main St, US-CT, Hartford",
    "1 Main St, US-CT, Hartford, United States",
    "us-ct, hartford",
    "US CT, Hartford",
]
#: What the resolver says of a listed code the rest of the address leaves unplaceable (r3 I-1).
UNPLACED = "states subdivision {}, but the rest of the address cannot be read with it"


def _location(city: str, state: str, country: str) -> SimpleNamespace:
    return SimpleNamespace(city=city, state_province=state, country=country)


def _resolve(location, address: str):
    return resolve_jurisdiction(location, [SimpleNamespace(business_address=address)])


@pytest.mark.parametrize("address", CODE_FIRST_FORMS)
def test_fue_a_listed_code_before_the_town_is_the_locations_own_subdivision(address):
    """FU-E: "US-CT, Hartford" is Hartford in Connecticut. A town contradicts no code, so a
    business's own address written code first agrees with its location; it was refused as
    "states subdivision US-CT?" (the D1 review's R9 row and the r3 review's 672 of 704)."""
    result = _resolve(HARTFORD_LOCATION, address)
    assert result.code == "US-CT", address
    assert result.evidence[-1].endswith("agrees"), result.evidence


@pytest.mark.parametrize(
    ("location", "address", "code"),
    [
        (TORONTO_LOCATION, "CA-ON, Toronto", "CA-ON"),
        (TORONTO_LOCATION, "100 King St W, CA-ON, Toronto, Canada", "CA-ON"),
        (_location("Sydney", "New South Wales", "Australia"), "AU-NSW, Sydney 2000", "AU-NSW"),
        (_location("Ikeja", "Lagos", "Nigeria"), "NG-LA, Ikeja", "NG-LA"),
        # A town named as its own state ("Kano" after NG-KN, "Lagos" after NG-LA) is the town.
        (_location("Kano", "Kano", "Nigeria"), "NG-KN, Kano", "NG-KN"),
        (_location("Lagos", "Lagos", "Nigeria"), "NG-LA, Lagos", "NG-LA"),
    ],
)
def test_fue_holds_for_every_country_with_listed_subdivisions(location, address, code):
    assert _resolve(location, address).code == code, address


@pytest.mark.parametrize("address", CODE_FIRST_FORMS)
def test_fue_the_same_forms_for_a_location_elsewhere_are_refused(address):
    """The reading names Connecticut, so a location elsewhere is contradicted, in plain words
    (the current release accepted these: it read no code there)."""
    dallas = _resolve(DALLAS_LOCATION, address)
    assert dallas.code == UNRESOLVED
    assert dallas.evidence[-1].endswith("states subdivision US-CT"), dallas.evidence
    toronto = _resolve(TORONTO_LOCATION, address)
    assert toronto.code == UNRESOLVED
    assert toronto.evidence[-1].endswith("states country US"), toronto.evidence


def test_fue_a_country_after_the_town_must_agree_with_the_code():
    """An address that contradicts itself ("US-CT, Hartford, Canada") is refused for Hartford
    too, in words."""
    result = _resolve(HARTFORD_LOCATION, "US-CT, Hartford, Canada")
    assert result.code == UNRESOLVED
    assert result.evidence[-1].endswith(UNPLACED.format("US-CT")), result.evidence


def test_fue_uk_nations_and_comma_free_forms_read_as_the_review_measured():
    """UK nations resolve by country, so a nation code before the town agrees with any UK
    location and contradicts one abroad, as before. A code and a town with no comma between
    them ("US-CT Hartford") agrees with Hartford. The qualifier rules do not read it as a code,
    as on the current release and D1, but GEO-P1 reads a listed code wherever it is written, so
    it contradicts Dallas (this was a characterisation, not a ruling)."""
    assert _resolve(LONDON_LOCATION, "GB-ENG, London").code == "GB"
    assert _resolve(LONDON_LOCATION, "GB-SCT, Edinburgh").code == "GB"
    toronto = _resolve(TORONTO_LOCATION, "GB-ENG, London")
    assert toronto.code == UNRESOLVED
    assert toronto.evidence[-1].endswith("states country GB")
    assert _resolve(HARTFORD_LOCATION, "US-CT Hartford").code == "US-CT"
    dallas = _resolve(DALLAS_LOCATION, "US-CT Hartford")
    assert dallas.code == UNRESOLVED
    assert dallas.evidence[-1].endswith("states subdivision US-CT"), dallas.evidence


def test_fue_only_the_business_address_reads_the_town():
    """The town reading is the business-address reader's alone: exclusion, inclusion and
    observed readings of the same text keep the unplaceable code (X-1 to X-5 unchanged)."""
    for text in ("US-CT, Hartford", "1 Main St, US-CT, Hartford", "US-CT, Hartford, USA"):
        for reading in (
            *exclusion_readings(text),
            *observed_readings(text),
            *markets.inclusion_readings(text),
            parse_market(text),
            parse_observed(text),
        ):
            assert (reading.region, reading.country) == ("US-CT?", "US"), (text, reading)
        stated = markets.read_business_address(text).stated
        assert (stated.city, stated.region, stated.country) == ("hartford", "US-CT", "US")


@pytest.mark.parametrize(
    ("address", "note"),
    [
        ("US-TX, Canada", "r3 I-1: a code and another country"),
        ("Dallas, US-TX, Canada", "a town, a code and another country"),
        ("US-TX, Texas, Canada", "a code, its own state's name and another country"),
        ("US-TX, Ohio, USA", "a code and another state's name"),
    ],
)
def test_fue_leaves_a_code_the_rest_of_the_address_cannot_be_read_with(address, note):
    """Still refused for Dallas, now with words instead of "US-TX?": what follows the code is
    another country or another state, not a town or the code's own state with its country."""
    result = _resolve(DALLAS_LOCATION, address)
    assert result.code == UNRESOLVED, note
    assert result.evidence[-1].endswith(UNPLACED.format("US-TX")), (note, result.evidence)


def test_fue_another_nigerian_state_after_the_code_is_not_a_town():
    """A Nigerian state's name after the code ("NG-OG, Lagos") is that state, not a town in
    Ogun: the code stays unplaceable and the address is refused for an Ogun location, while
    "NG-OG, Abeokuta" agrees."""
    abeokuta = _location("Abeokuta", "Ogun", "Nigeria")
    result = _resolve(abeokuta, "NG-OG, Lagos")
    assert result.code == UNRESOLVED
    assert result.evidence[-1].endswith(UNPLACED.format("NG-OG")), result.evidence
    assert _resolve(abeokuta, "NG-OG, Abeokuta").code == "NG-OG"


def test_fue_a_town_named_like_a_country_code_is_not_read_as_one():
    """W21-REV-5DW2FIX r3b C-4: only a listed subdivision code is read before the town. A town
    whose name is also a country code ("Bo" is Bolivia's, "Ba" Bosnia's) is not read as that code.
    GEO-P3 (ruling P3-R-16): the location's city no longer settles that the name is the town, so
    these own addresses ask; with the town left out they resolve by country."""
    bo = _location("Bo", "Southern Province", "Sierra Leone")
    ba = _location("Ba", "Western Division", "Fiji")
    assert _resolve(bo, "Bo, Southern Province").reason == "address_place_unclear"
    assert _resolve(ba, "Ba, Western Division").reason == "address_place_unclear"
    assert _resolve(bo, "Southern Province").code == "SL"
    assert _resolve(ba, "Western Division").code == "FJ"


# --------------------------------------------------------------------------- #
# r1 I-2 (folded in after W21-REV-5DW2FIX r3b): a code before its own state's name
# --------------------------------------------------------------------------- #

SYDNEY_LOCATION = _location("Sydney", "New South Wales", "Australia")
I2_OWN = [
    (DALLAS_LOCATION, "US-TX, Texas, USA", "US-TX"),
    (DALLAS_LOCATION, "US-TX, Texas, United States", "US-TX"),
    (DALLAS_LOCATION, "us-tx, texas, us", "US-TX"),
    (DALLAS_LOCATION, "1 Main St, US-TX, Texas, USA", "US-TX"),
    (HARTFORD_LOCATION, "US-CT, Connecticut, United States", "US-CT"),
    (TORONTO_LOCATION, "CA-ON, Ontario, Canada", "CA-ON"),
    (SYDNEY_LOCATION, "AU-NSW, New South Wales, Australia", "AU-NSW"),
    (_location("Ikeja", "Lagos", "Nigeria"), "NG-LA, Lagos, Nigeria", "NG-LA"),
]


@pytest.mark.parametrize(("location", "address", "code"), I2_OWN)
def test_i2_a_code_before_its_own_state_and_country_is_that_state(location, address, code):
    """r1 I-2: "US-TX, Texas, USA" is the business's own address in Texas. It was refused as an
    unplaceable "US-TX?" (74 of the review's 84 own rows at 4896bd69); it now agrees."""
    result = _resolve(location, address)
    assert result.code == code, address
    assert result.evidence[-1].endswith("agrees"), result.evidence


def test_i2_the_same_forms_for_a_location_elsewhere_are_refused():
    hartford = _resolve(HARTFORD_LOCATION, "US-TX, Texas, USA")
    assert hartford.code == UNRESOLVED
    assert hartford.evidence[-1].endswith("states subdivision US-TX"), hartford.evidence
    toronto = _resolve(TORONTO_LOCATION, "US-TX, Texas, USA")
    assert toronto.code == UNRESOLVED
    assert toronto.evidence[-1].endswith("states country US"), toronto.evidence
    dallas = _resolve(DALLAS_LOCATION, "CA-ON, Ontario, Canada")
    assert dallas.code == UNRESOLVED
    assert dallas.evidence[-1].endswith("states country CA"), dallas.evidence


def test_i2_only_the_business_address_reads_the_state_after_the_code():
    """Exclusion, inclusion and observed readings of the same text keep the unplaceable code."""
    text = "US-TX, Texas, USA"
    for reading in (
        *exclusion_readings(text),
        *observed_readings(text),
        *markets.inclusion_readings(text),
        parse_market(text),
        parse_observed(text),
    ):
        assert (reading.region, reading.country) == ("US-TX?", "US"), reading
    stated = markets.read_business_address(text).stated
    assert (stated.city, stated.region, stated.country) == (None, "US-TX", "US")


def test_i2_a_town_before_the_code_is_the_town():
    """The I-2 reading takes the code, its own state's name and the country at the end; a town
    before them is a town, even one named like another state. GEO-P3 (ruling P3-R-16): the
    location's city no longer settles that "Washington" is the town, so these ask; written with the
    town's code ("Washington DC") they resolve."""
    dc = _location("Washington", "District of Columbia", "United States")
    for address in (
        "Washington, US-DC, District of Columbia, United States",
        "1 Main St, Washington, US-DC, District of Columbia, USA",
    ):
        assert _resolve(dc, address).reason == "address_place_unclear", address
        remedy = address.replace("Washington,", "Washington DC,")
        assert _resolve(dc, remedy).code == "US-DC", remedy


# --------------------------------------------------------------------------- #
# C-5 (W21-REV-5DW2FIX r3c): a Nigerian code before its own state's suffixed or bare name
# --------------------------------------------------------------------------- #

ABUJA_LOCATION = _location("Abuja", "Federal Capital Territory", "Nigeria")
MINNA_LOCATION = _location("Minna", "Niger", "Nigeria")
IKEJA_LOCATION = _location("Ikeja", "Lagos", "Nigeria")
KANO_LOCATION = _location("Kano", "Kano", "Nigeria")
#: (own location, code, suffixed name, bare name): the review's four forms are built from them.
C5_STATES = [
    (IKEJA_LOCATION, "NG-LA", "Lagos State", "Lagos"),
    (KANO_LOCATION, "NG-KN", "Kano State", "Kano"),
    (ABUJA_LOCATION, "NG-FC", "Federal Capital Territory", "FCT"),
    (MINNA_LOCATION, "NG-NI", "Niger State", "Niger"),
]


def _ng_forms(code: str, suffixed: str, bare: str) -> list[str]:
    return [
        f"{code}, {suffixed}, Nigeria",
        f"{code}, {suffixed}, NG",
        f"1 Allen Ave, {code}, {suffixed}, Nigeria",
        f"{code}, {bare}, Nigeria",
    ]


@pytest.mark.parametrize(("location", "code", "suffixed", "bare"), C5_STATES)
def test_c5_a_nigerian_code_before_its_own_states_name_is_that_state(
    location, code, suffixed, bare
):
    """C-5: "NG-LA, Lagos State, Nigeria", "..., NG", with a street before, and "NG-LA, Lagos,
    Nigeria" are the business's own address in Lagos State. They were refused as an unplaceable
    "NG-LA?" (111 own rows of the review's 37 states x 3 suffixed forms, and "NG-NI, Niger,
    Nigeria"); each now agrees. This inverts the pins of r3b's two remaining rows."""
    for address in _ng_forms(code, suffixed, bare):
        result = _resolve(location, address)
        assert result.code == code, address
        assert result.evidence[-1].endswith("agrees"), result.evidence


@pytest.mark.parametrize(("location", "code", "suffixed", "bare"), C5_STATES)
def test_c5_the_same_forms_for_another_states_location_are_refused(location, code, suffixed, bare):
    other = KANO_LOCATION if code != "NG-KN" else IKEJA_LOCATION
    for address in _ng_forms(code, suffixed, bare):
        result = _resolve(other, address)
        assert result.code == UNRESOLVED, address
        assert result.evidence[-1].endswith(f"states subdivision {code}"), result.evidence


def test_c5_niger_after_ng_ni_is_the_state_and_never_the_country_niger():
    """The name after NG-NI ("Niger") is Niger State, Nigeria: own Minna resolves; Lagos and a
    location in the country Niger are refused."""
    assert _resolve(MINNA_LOCATION, "NG-NI, Niger, Nigeria").code == "NG-NI"
    lagos = _resolve(IKEJA_LOCATION, "NG-NI, Niger, Nigeria")
    assert lagos.evidence[-1].endswith("states subdivision NG-NI"), lagos.evidence
    niamey = _resolve(_location("Niamey", "Niamey", "Niger"), "NG-NI, Niger, Nigeria")
    assert niamey.code == UNRESOLVED
    assert niamey.evidence[-1].endswith("states country NG"), niamey.evidence


def test_c5_another_states_name_after_a_nigerian_code_is_still_refused():
    for address in (
        "NG-LA, Ogun State, Nigeria",
        "NG-LA, Kano, Nigeria",
        "NG-LA, Lagos State, Kenya",
    ):
        result = _resolve(IKEJA_LOCATION, address)
        assert result.code == UNRESOLVED, address
        assert result.evidence[-1].endswith(UNPLACED.format("NG-LA")), result.evidence


def test_c5_a_suffixed_state_before_its_bare_name_keeps_the_word_reading():
    """Only a listed code is read before the state's name (the strict-code guard of the I-2
    reading): a suffixed state then its bare name is read as words, as ``parse_observed`` does."""
    for text in (
        "Lagos State, Lagos, Nigeria",
        "Niger State, Niger, Nigeria",
        "Kano State, Kano, NG",
    ):
        stated = markets.read_business_address(text).stated
        assert _ident(stated) == _ident(parse_observed(text)), text


# --------------------------------------------------------------------------- #
# P-CN (the coordinator's convergence probe): every other part agrees with the code
# --------------------------------------------------------------------------- #

MONTGOMERY_LOCATION = _location("Montgomery", "Alabama", "United States")
UMUAHIA_LOCATION = _location("Umuahia", "Abia", "Nigeria")
PCN_OWN = [
    (MONTGOMERY_LOCATION, "US-AL, Alabama, Montgomery", "US-AL"),
    (MONTGOMERY_LOCATION, "1 Main St, US-AL, Alabama, Montgomery 36104", "US-AL"),
    (MONTGOMERY_LOCATION, "US-AL, Montgomery, State of Alabama", "US-AL"),
    (MONTGOMERY_LOCATION, "us al, state of alabama, montgomery", "US-AL"),
    (TORONTO_LOCATION, "CA-ON, Toronto, Province of Ontario", "CA-ON"),
    (UMUAHIA_LOCATION, "NG-AB, Abia State, Umuahia", "NG-AB"),
    (UMUAHIA_LOCATION, "Abia State, NG-AB, Nigeria", "NG-AB"),
    (UMUAHIA_LOCATION, "Umuahia, Abia State, NG-AB, NG", "NG-AB"),
    (UMUAHIA_LOCATION, "NG-AB, Umuahia, Abia", "NG-AB"),
]


@pytest.mark.parametrize(("location", "address", "code"), PCN_OWN)
def test_pcn_a_code_that_every_other_part_agrees_with_is_that_place(location, address, code):
    """P-CN: an address whose listed code the qualifier rules left unplaceable is the code's
    subdivision when every other part agrees with it (its own state's name, "State of ...",
    "Province of ...", an agreeing country, or a town or street) and one part names its area."""
    result = _resolve(location, address)
    assert result.code == code, address
    assert result.evidence[-1].endswith("agrees"), result.evidence


@pytest.mark.parametrize(
    ("location", "address", "code"),
    [
        (DALLAS_LOCATION, "US-AL, Alabama, Montgomery", "US-AL"),
        (DALLAS_LOCATION, "US-AL, Montgomery, State of Alabama", "US-AL"),
        (KANO_LOCATION, "Abia State, NG-AB, Nigeria", "NG-AB"),
    ],
)
def test_pcn_the_same_forms_for_another_location_are_refused(location, address, code):
    result = _resolve(location, address)
    assert result.code == UNRESOLVED, address
    assert result.evidence[-1].endswith(f"states subdivision {code}"), result.evidence


@pytest.mark.parametrize(
    ("address", "evidence"),
    [
        ("US-AL, Texas, Montgomery", UNPLACED.format("US-AL")),
        ("US-AL, Montgomery, State of Texas", UNPLACED.format("US-AL")),
        # GEO-P3 (G-3, amended by the operator): "US-AL, Montgomery, Bavaria" names no other listed
        # area, so it now asks, as its own twin does (test_geography_place_knowledge).
        ("US-AL, Alabama, Montgomery, Canada", "states country CA"),
    ],
)
def test_pcn_another_area_or_no_confirmation_keeps_the_code_unplaceable(address, evidence):
    """Any part naming another area keeps the address refused, and so does an address where no
    part but the code names the area ("Bavaria" is no area the tables know, so it could be a town:
    nothing confirms the code)."""
    result = _resolve(MONTGOMERY_LOCATION, address)
    assert result.code == UNRESOLVED, address
    assert result.evidence[-1].endswith(evidence), result.evidence


def test_pcn_only_the_business_address_reads_the_agreement():
    for text in ("US-AL, Alabama, Montgomery", "Abia State, NG-AB, Nigeria"):
        for reading in (*exclusion_readings(text), *observed_readings(text), parse_market(text)):
            assert (reading.region or "").endswith("?"), (text, reading)


# --------------------------------------------------------------------------- #
# C-6 and C-7 (W21-REV-5DW2FIX r3d)
# --------------------------------------------------------------------------- #

C6_OWN = [
    (MONTGOMERY_LOCATION, "US-AL, 1 Main St, Montgomery", "US-AL"),
    (MONTGOMERY_LOCATION, "US-AL, Montgomery, 1 Main St", "US-AL"),
    (MONTGOMERY_LOCATION, "us-al, 1 main st, montgomery", "US-AL"),
    (MONTGOMERY_LOCATION, "US AL, MONTGOMERY, 1 MAIN ST", "US-AL"),
    (TORONTO_LOCATION, "CA-ON, 100 King St W, Toronto", "CA-ON"),
    (IKEJA_LOCATION, "NG-LA, 1 Allen Ave, Ikeja", "NG-LA"),
    (SYDNEY_LOCATION, "AU-NSW, Sydney, 1 Martin Pl", "AU-NSW"),
]


@pytest.mark.parametrize(("location", "address", "code"), C6_OWN)
def test_c6_a_code_then_a_street_and_a_town_is_that_place(location, address, code):
    """C-6: "US-AL, 1 Main St, Montgomery" and "US-AL, Montgomery, 1 Main St" are the business's
    own address, as "US-AL, Montgomery" is (FU-E): a street carries a number, so the one word
    without a digit is the town. They were refused (218 own rows of the review's probe)."""
    result = _resolve(location, address)
    assert result.code == code, address
    assert result.evidence[-1].endswith("agrees"), result.evidence


@pytest.mark.parametrize(
    "address", ["US-AL, 1 Main St, Montgomery", "US-AL, Montgomery, 1 Main St"]
)
def test_c6_the_same_forms_for_a_location_elsewhere_are_refused(address):
    dallas = _resolve(DALLAS_LOCATION, address)
    assert dallas.code == UNRESOLVED
    assert dallas.evidence[-1].endswith("states subdivision US-AL"), dallas.evidence
    toronto = _resolve(TORONTO_LOCATION, address)
    assert toronto.code == UNRESOLVED
    assert toronto.evidence[-1].endswith("states country US"), toronto.evidence


@pytest.mark.parametrize(
    ("location", "address", "code"),
    [
        (IKEJA_LOCATION, "NG-LA, NG-OG, Lagos State", "NG-OG"),
        (UMUAHIA_LOCATION, "NG-AB, NG-AD, Abia", "NG-AD"),
        (UMUAHIA_LOCATION, "NG-AB, NG-AD, Abia State", "NG-AD"),
        (UMUAHIA_LOCATION, "Abia, NG-AB, NG-AD", "NG-AB"),
        (DALLAS_LOCATION, "US-TX, US-OK, Texas", "US-OK"),
        (DALLAS_LOCATION, "1 Main St, US-TX, US-OK, Texas", "US-OK"),
    ],
)
def test_c7_a_second_code_for_another_subdivision_keeps_the_address_refused(
    location, address, code
):
    """C-7: the agreement reading passes over only the code it reads. A second listed code for
    another subdivision is another area, so the address stays refused for the first code's own
    location, in words (the review's mutant RA-E accepted 1,270 such contradictions)."""
    result = _resolve(location, address)
    assert result.code == UNRESOLVED, address
    assert result.evidence[-1].endswith(UNPLACED.format(code)), result.evidence


VANCOUVER_LOCATION = _location("Vancouver", "British Columbia", "Canada")
MELBOURNE_LOCATION = _location("Melbourne", "Victoria", "Australia")
ABEOKUTA_LOCATION = _location("Abeokuta", "Ogun", "Nigeria")
C8_OWN = [
    (MONTGOMERY_LOCATION, "US-AL, Montgomery, Suite 5", "US-AL"),
    (MONTGOMERY_LOCATION, "US-AL, Suite 5, Montgomery", "US-AL"),
    (MONTGOMERY_LOCATION, "1 Main St, US-AL, Montgomery, Suite 5", "US-AL"),
    (MONTGOMERY_LOCATION, "us-al, montgomery, suite 5", "US-AL"),
    (MONTGOMERY_LOCATION, "US-AL, Montgomery, Apt 4B", "US-AL"),
    (TORONTO_LOCATION, "CA-ON, Toronto, Unit 3", "CA-ON"),
    (TORONTO_LOCATION, "CA-ON, Unit 3, Toronto", "CA-ON"),
    (SYDNEY_LOCATION, "AU-NSW, Sydney, Level 3", "AU-NSW"),
]


@pytest.mark.parametrize(("location", "address", "code"), C8_OWN)
def test_c8_a_unit_beside_the_town_is_that_place(location, address, code):
    """C-8: a unit ("Suite 5", "Unit 3") is not a word beside the town. Its number is lost in the
    folded part ("suite"), so the C-6 count reads the part as written. These were refused for
    their own location (384 own rows of the review's completeness probe, r3e)."""
    result = _resolve(location, address)
    assert result.code == code, address
    assert result.evidence[-1].endswith("agrees"), result.evidence


@pytest.mark.parametrize(
    ("location", "address", "stated"),
    [
        (DALLAS_LOCATION, "US-AL, Montgomery, Suite 5", "subdivision US-AL"),
        (DALLAS_LOCATION, "US-AL, Suite 5, Montgomery", "subdivision US-AL"),
        (DALLAS_LOCATION, "1 Main St, US-AL, Montgomery, Suite 5", "subdivision US-AL"),
        (VANCOUVER_LOCATION, "CA-ON, Toronto, Unit 3", "subdivision CA-ON"),
        (DALLAS_LOCATION, "CA-ON, Unit 3, Toronto", "country CA"),
        (MELBOURNE_LOCATION, "AU-NSW, Sydney, Level 3", "subdivision AU-NSW"),
    ],
)
def test_c8_the_same_addresses_for_a_location_elsewhere_are_refused(location, address, stated):
    result = _resolve(location, address)
    assert result.code == UNRESOLVED, address
    assert result.evidence[-1].endswith(f"states {stated}"), result.evidence


@pytest.mark.parametrize(
    ("location", "address", "code"),
    [
        (IKEJA_LOCATION, "NG-LA, No 3 Ajose Adeogun St, Ikeja", "NG-LA"),
        (IKEJA_LOCATION, "NG-LA, Ikeja, Plot 1 Adeola Odeku St", "NG-LA"),
        (SYDNEY_LOCATION, "AU-NSW, Level 3 1 Martin Pl, Sydney", "AU-NSW"),
        (ABEOKUTA_LOCATION, "NG-LA, No 3 Ajose Adeogun St, Ikeja", UNRESOLVED),
        (MELBOURNE_LOCATION, "AU-NSW, Level 3 1 Martin Pl, Sydney", UNRESOLVED),
    ],
)
def test_c8_a_street_line_after_a_unit_label_is_a_street(location, address, code):
    """A street line written after a unit label ("No 3 Ajose Adeogun St", "Level 3 1 Martin Pl")
    carries its number inside, not first; it is a street, not a word (the review's mutant RC-5)."""
    result = _resolve(location, address)
    assert result.code == code, address
