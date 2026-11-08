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
* a Nigerian state (or the FCT) named with its suffix removes markets in that state, and both
  parsers read it the same way, from ``app.jurisdiction.subdivisions``;
* same-name readings (W21-REV-5DW4 r1 C-1, decided from the item-35 ruling): an exclusion of a
  bare name that is also a Nigerian state ("Lagos, NG", "Niger") removes what either place
  covers; an inclusion or a clearance of it admits only what it did; "Niger" and "Niger State"
  never collapse into one another, and "Nigeria" is never a Niger place;
* monotonicity: the word reading (``jurisdiction_forms=False``, the parser as it was) still
  applies, so no exclusion removes less than it did;
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
from app.connectors.sample_feeds import _MARKET_FEEDS
from app.db.seed import CITIES
from app.geography import markets
from app.geography.engine import CoverageRule, is_excluded, market_in_coverage, resolve_geo
from app.geography.markets import (
    MarketIdentity,
    country_name,
    exclusion_readings,
    market_from_parts,
    market_within,
    may_overlap,
    parse_market,
    parse_observed,
    qualify_for_location,
)
from app.geography.places_data import COUNTRIES, KNOWN_CITIES, SUBDIVISIONS
from app.jurisdiction.codes import UNRESOLVED
from app.jurisdiction.resolver import resolve_jurisdiction
from app.jurisdiction.subdivisions import SUBDIVISION_JURISDICTIONS
from app.scouting_requests.fixtures import ALL_MARKETS

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
    """Before the fix "US-ZZ" was a city "us zz" (in the location's country), which removed no
    Texas market. Neither parser, with or without a location country, reads it as one now."""
    for text, home in itertools.product(("US-ZZ", "us-zz", "NG-ZZ", "GB-LND", "DE-BY"), HOMES):
        country = home.country if home else None
        assert parse_market(text, default_country=country).city is None, (text, home)
        assert parse_observed(text).city is None, text


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


def test_niger_and_niger_state_never_collapse_and_nigeria_is_never_a_niger_place():
    for country in (None, "NG", "NE", "US"):
        assert _ident(parse_market("Niger", default_country=country)) == (None, None, "NE")
        assert _ident(parse_market("Niger State", default_country=country)) == (
            None,
            "NG:niger",
            "NG",
        )
        assert _ident(parse_market("Nigeria", default_country=country)) == (None, None, "NG")
        assert [_ident(r) for r in exclusion_readings("Nigeria", default_country=country)] == [
            (None, None, "NG")
        ]
        assert [_ident(r) for r in exclusion_readings("Niger State", default_country=country)] == [
            (None, "NG:niger", "NG")
        ]
    assert _ident(parse_observed("Niger")) == (None, None, "NE")
    assert _ident(parse_observed("Niger State")) == (None, "NG:niger", "NG")
    assert _ident(parse_observed("Nigeria")) == (None, None, "NG")
    # The exclusion reading of "Niger" keeps the country and adds the state.
    assert [_ident(r) for r in exclusion_readings("Niger")] == [
        (None, None, "NE"),
        (None, "NG:niger", "NG"),
    ]
    # Niger State never reaches the country Niger, and Nigeria never reaches either Niger.
    for excluded in ("Niger State, NG", "NG-NI", "Nigeria", "NG"):
        for market in ("Niamey, Niger", "Niger", "Zinder, NE"):
            _assert_kept(excluded, market)
    for market in ("Niamey, Niger", "Zinder, NE"):
        _assert_kept("Niger State", market)
    for rule in (
        CoverageRule(coverage_type="online", excluded_markets=("Niger State",), home_market=LAGOS),
        CoverageRule(
            coverage_type="radius",
            online_global=True,
            excluded_markets=("Niger State",),
            home_market=LAGOS,
        ),
    ):
        assert market_in_coverage(rule, "Niger") is True
    # Monotonic carry-over (requirement 3; an operator decision is requested): with no
    # location country, e3571ac8 read "Niger State" as an unqualified city "niger state",
    # which removed every market that names no city, the bare "Niger" among them. It still
    # does; the jurisdiction-form reading alone keeps it.
    rule = CoverageRule(coverage_type="online", excluded_markets=("Niger State",))
    assert market_in_coverage(rule, "Niger") is False
    assert may_overlap(parse_observed("Niger"), parse_market("Niger State")) is False
    # "NE" (Niger or Nebraska) removes both, and no Niger State market that names its city.
    _assert_removed("NE", "Niamey, Niger")
    _assert_kept("NE", "Minna, Niger State, Nigeria")


AMBIGUOUS_NAMES = [
    # (exclusion, removed: every place the name may be, kept)
    ("CA", ["Toronto, ON", "Los Angeles, CA", "Vancouver, BC"], ["Dallas, TX", "London, UK"]),
    ("NE", ["Niamey, Niger", "Omaha, NE"], ["Dallas, TX", "Kano, Kano, NG"]),
    ("GA", ["Libreville, GA", "Atlanta, GA"], ["Dallas, TX", "Tbilisi, Georgia"]),
    ("Georgia", ["Tbilisi, Georgia", "Atlanta, Georgia"], ["Dallas, TX", "Libreville, GA"]),
    ("WA", ["Perth, WA", "Seattle, WA"], ["Dallas, TX", "Darwin, NT"]),
    ("IN", ["Mumbai, India", "Indianapolis, IN"], ["Dallas, TX"]),
    ("NT", ["Darwin, NT", "Yellowknife, NT"], ["Perth, WA"]),
]


@pytest.mark.parametrize(("excluded", "removed", "kept"), AMBIGUOUS_NAMES)
def test_an_ambiguous_alpha2_code_or_name_excludes_every_place_it_may_be(excluded, removed, kept):
    """Requirement 1 for the alpha-2 codes a US, Canadian or Australian subdivision shares, and
    item 35 for every name with several readings: "CA" was an unqualified city "ca" that
    removed neither Canada nor California. Every reading is now removed."""
    for market, home in itertools.product(removed, [None, DALLAS, LAGOS]):
        _assert_removed(excluded, market, home)
    for market in kept:
        _assert_kept(excluded, market)


def test_a_city_first_name_stays_the_city_in_an_exclusion():
    """``CITY_FIRST_NAMES`` reads "New York", "Washington" and "Quebec" as the city (the
    narrower scope; the state is "New York State" or "NY"). Not changed here: an operator
    decision is requested on whether an exclusion should also read the subdivision."""
    for name in ("New York", "Washington", "Quebec"):
        assert exclusion_readings(name) == [parse_market(name)], name
    _assert_kept("New York", "Buffalo, New York, USA")
    _assert_kept("New York", "York")


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
# Monotonic: the word reading still applies, so nothing is removed that was kept
# --------------------------------------------------------------------------- #

#: Exclusion texts: the reproductions', this module's, and names from the place tables.
_TEXTS = sorted(
    {c for c, _, _ in CODE_CASES}
    | {c for c, _ in UNPLACEABLE}
    | {"Lagos State", "Lagos State, Nigeria", "Ogun State, Nigeria", "NG-FC", "AL-AIN"}
    | {"Al-Ain", "KE, Kenya", "US-TX, Canada", "Texas", "GB", "KE", "London, UK", "Ikeja"}
    | {f"{country}-{code}" for country, code, _ in LISTED[::7]}
    | {_state_words(names) for _, names in NG_STATES[::5]}
)
#: Observed markets: the four fixture markets, principal cities, and markets that state no
#: city (the word reading of an unqualified exclusion such as "ng" removed those).
_OBSERVED = sorted(
    set(ALL_MARKETS)
    | {f"{city}, {key}" for key, cities in sorted(KNOWN_CITIES.items())[::6] for city in cities[:2]}
    | {"Kenya", "Texas", "Nigeria", "Lagos State", "Al Ain, AE", "Springfield", "US-TX"}
)


def _word_excluded(text: str, observed: str, home) -> bool:
    country = home.country if home else None
    excluded = parse_market(text, default_country=country, jurisdiction_forms=False)
    reading = parse_observed(observed, jurisdiction_forms=False)
    return excluded is not None and may_overlap(reading, excluded)


def test_no_exclusion_removes_less_than_its_word_reading():
    """``is_excluded`` is a superset of the word reading (``jurisdiction_forms=False``), which
    is the parser before the fix (proved against e3571ac8 in the lane's evidence)."""
    word_only = 0
    for text, observed, home in itertools.product(_TEXTS, _OBSERVED, HOMES):
        rule = CoverageRule(coverage_type="online", excluded_markets=(text,), home_market=home)
        if _word_excluded(text, observed, home):
            assert is_excluded(rule, observed), (text, observed, home)
            forms = parse_market(text, default_country=home.country if home else None)
            if not may_overlap(parse_observed(observed), forms):
                word_only += 1
    assert word_only > 0, "the corpus never exercises the word reading on its own"


def test_the_word_reading_keeps_what_an_unqualified_misreading_removed():
    """Fail closed until an operator rules otherwise: "NG-LA" was read as the unqualified city
    "ng", which removed every observation that states no city. It still does."""
    for market in ("Kenya", "Texas", "Springfield"):
        rule = CoverageRule(coverage_type="online", excluded_markets=("NG-LA",))
        assert market_in_coverage(rule, market) is False, market
    # "AL-AIN" as a code is Albania; as words it is the city Al Ain: both are removed.
    _assert_removed("AL-AIN", "Al Ain, AE", AL_AIN)
    _assert_removed("AL-AIN", "Tirana, Albania", AL_AIN)


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
