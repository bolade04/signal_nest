"""Phase 5D exit criterion 4: normalised, fail-closed jurisdiction resolution.

* The four-market matrix resolves to ``US-TX``, ``GB``, ``NG-LA`` and ``KE``, from the
  seeded locations themselves (``app.db.seed`` builds them from the fixture geocoder) and
  from the other spellings a location record can hold (documented equivalents).
* An unknown city yields ``UNRESOLVED``, and so does anything else the resolver cannot
  place without guessing.
* A gating consumer given ``UNRESOLVED`` **denies** (:func:`jurisdiction_permits`, the gate
  every consumer shares), whatever its allow and exclude lists say.
* Codes match by code, never by substring (exit criterion 5's "York" is not "New York"
  class, at the jurisdiction layer).

Authored by lane 5D-W2 (plan §5 assigns this file to 5D-W1 as a cross-authored slice; in
the W21 programme the independent review and the 5D-W4 battery provide that independence).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.db.seed import CITIES
from app.geography.geocoder import fixture_geocode
from app.geography.markets import parse_market
from app.geography.places_data import SUBDIVISIONS
from app.jurisdiction.codes import (
    JURISDICTION_CODE_MAX_LENGTH,
    UNRESOLVED,
    is_jurisdiction_code,
)
from app.jurisdiction.resolver import (
    RESOLVED_CONFIDENCE,
    JurisdictionResolution,
    code_within,
    codes_may_overlap,
    country_code_of,
    jurisdiction_permits,
    resolve_jurisdiction,
    subdivision_code_of,
)
from app.jurisdiction.subdivisions import SUBDIVISION_JURISDICTIONS
from app.locations.models import BusinessLocation, GeoCoverageRule


def _location(city=None, state=None, country=None, **extra) -> SimpleNamespace:
    return SimpleNamespace(city=city, state_province=state, country=country, **extra)


def _rule(address: str | None) -> SimpleNamespace:
    return SimpleNamespace(business_address=address)


MATRIX = {"dallas": "US-TX", "london": "GB", "lagos": "NG-LA", "nairobi": "KE"}


# --------------------------------------------------------------------------- #
# The four-market matrix
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("city", CITIES, ids=lambda c: c["key"])
def test_the_seeded_four_market_matrix_resolves(city):
    """The seeded location and its seeded coverage rule, built exactly as ``app.db.seed``
    builds them (ORM objects, not persisted: the resolver is pure)."""
    geo = fixture_geocode(city["geocode"])
    location = BusinessLocation(
        city=geo.city, state_province=geo.state_province, country=geo.country
    )
    rule = GeoCoverageRule(
        coverage_type="radius",
        business_address=f"{geo.city}, {geo.country}",
        country=geo.country,
        state=geo.state_province,
        included_markets=[city["market"]],
    )
    resolution = resolve_jurisdiction(location, [rule])
    expected = MATRIX[city["key"]]
    assert resolution.code == expected
    assert resolution.is_resolved
    assert resolution.confidence == RESOLVED_CONFIDENCE == 1.0
    assert resolution.codes == ((expected, expected[:2]) if "-" in expected else (expected,))
    assert any("agrees" in line for line in resolution.evidence)
    # The answer is storable: the exact grammar and the CF-3 column width.
    assert is_jurisdiction_code(resolution.code)
    assert len(resolution.code) <= JURISDICTION_CODE_MAX_LENGTH


def test_the_matrix_covers_exactly_the_seeded_markets():
    assert {c["key"] for c in CITIES} == set(MATRIX)


@pytest.mark.parametrize(
    ("city", "state", "country", "expected"),
    [
        # Texas / US: names, postal and ISO codes, any case.
        ("Dallas", "Texas", "United States", "US-TX"),
        ("Dallas", "TX", "US", "US-TX"),
        ("Dallas", "tx", "usa", "US-TX"),
        ("Dallas", "US-TX", "United States of America", "US-TX"),
        ("Dallas", "Texas State", "U.S.", "US-TX"),
        # London / UK: the United Kingdom resolves by country, whatever its nation.
        ("London", "England", "United Kingdom", "GB"),
        ("London", "Greater London", "UK", "GB"),
        ("London", None, "GB", "GB"),
        ("Edinburgh", "Scotland", "Great Britain", "GB"),
        ("London", None, "England", "GB"),  # a nation typed into the country field
        # Lagos / NG: the state by name, with "State", or by ISO 3166-2 code.
        ("Lagos", "Lagos", "Nigeria", "NG-LA"),
        ("Ikeja", "Lagos State", "NG", "NG-LA"),
        ("Lagos", "LA", "Nigeria", "NG-LA"),
        ("Lagos", "NG-LA", "Nigeria", "NG-LA"),
        ("Abuja", "FCT", "Nigeria", "NG-FC"),
        ("Kano", "Kano", "Nigeria", "NG-KN"),
        # Nairobi / KE: Kenya resolves by country, whatever the county.
        ("Nairobi", "Nairobi", "Kenya", "KE"),
        ("Nairobi", "Nairobi County", "KE", "KE"),
        ("Mombasa", None, "Kenya", "KE"),
        # The other subdivision-level federations the market engine knows.
        ("Toronto", "Ontario", "Canada", "CA-ON"),
        ("Sydney", "NSW", "Australia", "AU-NSW"),
        # Any other country resolves by country, even with an unknown city.
        ("Atlantis", None, "France", "FR"),
    ],
)
def test_documented_equivalents_resolve_to_the_same_code(city, state, country, expected):
    resolution = resolve_jurisdiction(_location(city, state, country))
    assert resolution.code == expected, resolution.evidence


# --------------------------------------------------------------------------- #
# UNRESOLVED: the resolver never guesses
# --------------------------------------------------------------------------- #

#: (location, why) pairs that must answer UNRESOLVED.
UNRESOLVABLE = [
    (_location("Atlantis"), "an unknown city alone"),
    (_location("Atlantis", "Atlantis"), "an unknown city and region"),
    (_location("Nairobi"), "a known city alone: a bare city names no country"),
    (_location("London", "England"), "a city and region with no country"),
    (_location("Atlantis", None, "Narnia"), "an unknown country"),
    (_location("Dallas", None, "United States"), "a United States location without a state"),
    (_location("Lagos", None, "Nigeria"), "a Nigerian location without a state"),
    (_location("Lagos", "", "Nigeria"), "an empty state"),
    (_location("Toronto", None, "Canada"), "a Canadian location without a province"),
    (_location("Perth", None, "Australia"), "an Australian location without a state"),
    (_location("York", "York", "United States"), "a state that is not one ('York')"),
    (_location("Lagos", "Lagos", "United States"), "another country's state"),
    (_location("Lagos", "Atlantis", "Nigeria"), "an unknown Nigerian state"),
    (_location("Dallas", "US-ZZ", "United States"), "a code that names no subdivision"),
    (_location("Dallas", "NG-LA", "United States"), "a code of another country"),
    (_location(None, None, "   "), "a blank country"),
    (None, "no location"),
]


@pytest.mark.parametrize(("location", "why"), UNRESOLVABLE, ids=[w for _, w in UNRESOLVABLE])
def test_what_cannot_be_placed_is_unresolved(location, why):
    resolution = resolve_jurisdiction(location)
    assert resolution.code == UNRESOLVED, why
    assert resolution.is_resolved is False
    assert resolution.codes == ()
    assert resolution.confidence == 0.0
    assert resolution.evidence and resolution.evidence[-1].startswith("unresolved: ")


@pytest.mark.parametrize(
    ("location", "address"),
    [
        # London, England with a Scout Reach rule centred on London, Ontario.
        (_location("London", "England", "United Kingdom"), "London, ON"),
        # A Texas location whose rule's address states Oklahoma.
        (_location("Dallas", "Texas", "United States"), "Tulsa, OK"),
        # A Lagos State location whose rule's address states Ogun State.
        (_location("Lagos", "Lagos", "Nigeria"), "Abeokuta, Ogun, NG"),
        (_location("Nairobi", "Nairobi", "Kenya"), "Kampala, Uganda"),
    ],
)
def test_contradicting_facts_are_unresolved(location, address):
    """A coverage rule's business address that states another country or subdivision
    than the location's own fields: the facts disagree, so nothing is resolved."""
    resolution = resolve_jurisdiction(location, [_rule(address)])
    assert resolution.code == UNRESOLVED
    assert "business address" in resolution.evidence[-1]


@pytest.mark.parametrize(
    ("location", "address"),
    [
        (_location("Dallas", "Texas", "United States"), "Austin, TX"),
        (_location("Dallas", "Texas", "United States"), "1 Main St, Dallas"),  # states nothing
        (_location("Lagos", "Lagos", "Nigeria"), "Ikeja, Lagos, NG"),
        (_location("London", "England", "United Kingdom"), "Manchester, UK"),
        (_location("London", "England", "United Kingdom"), None),
    ],
)
def test_agreeing_or_silent_addresses_do_not_change_the_answer(location, address):
    assert resolve_jurisdiction(location, [_rule(address)]).is_resolved


def test_a_captured_code_must_agree_with_the_fields():
    """A stored ``business_locations.jurisdiction_code`` never overrides the location's
    current fields: if they now resolve elsewhere (or nowhere), the answer is UNRESOLVED."""
    agrees = _location("Dallas", "Texas", "United States", jurisdiction_code="US-TX")
    assert resolve_jurisdiction(agrees).code == "US-TX"
    stale = _location("Dallas", "Oklahoma", "United States", jurisdiction_code="US-TX")
    assert resolve_jurisdiction(stale).code == UNRESOLVED
    orphan = _location("Dallas", None, None, jurisdiction_code="US-TX")
    assert resolve_jurisdiction(orphan).code == UNRESOLVED
    never_captured = _location("Dallas", "Texas", "US", jurisdiction_code=None)
    assert resolve_jurisdiction(never_captured).code == "US-TX"


# --------------------------------------------------------------------------- #
# The gate: UNRESOLVED denies
# --------------------------------------------------------------------------- #

_EVERYTHING = ("US", "GB", "NG", "KE", "US-TX", "NG-LA")


@pytest.mark.parametrize(("location", "why"), UNRESOLVABLE, ids=[w for _, w in UNRESOLVABLE])
def test_a_gating_consumer_given_unresolved_denies(location, why):
    resolution = resolve_jurisdiction(location)
    assert jurisdiction_permits(resolution) is False
    assert jurisdiction_permits(resolution, allowed=_EVERYTHING) is False
    assert jurisdiction_permits(resolution, excluded=()) is False
    assert jurisdiction_permits(resolution.code) is False


@pytest.mark.parametrize(
    "subject",
    [UNRESOLVED, None, "", "us-tx", "USA", "US_TX", "US-", "U1X", 42, "UNRESOLVED "],
)
def test_the_gate_denies_anything_that_is_not_a_code(subject):
    """A stored snapshot that is NULL (never captured), the sentinel, or garbage: deny."""
    assert jurisdiction_permits(subject) is False
    assert jurisdiction_permits(subject, allowed=_EVERYTHING) is False


def test_a_forged_resolution_carrying_the_sentinel_still_denies():
    forged = JurisdictionResolution(code=UNRESOLVED, codes=("US-TX",), confidence=1.0, evidence=())
    assert jurisdiction_permits(forged) is False
    garbage = JurisdictionResolution(code="texas", codes=(), confidence=1.0, evidence=())
    assert garbage.is_resolved is False
    assert jurisdiction_permits(garbage) is False


@pytest.mark.parametrize(
    ("code", "allowed", "excluded", "permitted"),
    [
        ("US-TX", (), (), True),
        ("US-TX", ("US",), (), True),
        ("US-TX", ("US-TX",), (), True),
        ("US-TX", ("US-OK", "GB"), (), False),
        ("US", ("US-TX",), (), False),  # a country is not inside one of its states
        ("US-TX", (), ("US",), False),
        ("US-TX", (), ("US-CA",), True),
        ("US", (), ("US-CA",), False),  # a whole country cannot be ruled out of a state
        ("GB", (), ("GB-ENG",), False),
        ("KE", (), ("NG", "US-TX"), True),
        # An exclusion beats an allowance (exclusions are read first).
        ("US-TX", ("US",), ("US-TX",), False),
        ("NG-LA", ("NG-LA",), ("NG",), False),
        # An exclusion that is not a code cannot be ruled out: deny.
        ("KE", (), ("Kenya",), False),
        ("KE", (), (UNRESOLVED,), False),
        # An allowance that is not a code clears nothing.
        ("KE", ("Kenya",), (), False),
        ("KE", ("ke",), (), False),
        ("NG-LA", ("NG",), ("NG-OG",), True),
    ],
)
def test_the_gate_for_resolved_codes(code, allowed, excluded, permitted):
    assert jurisdiction_permits(code, allowed=allowed, excluded=excluded) is permitted


def test_the_gate_reads_a_resolution_and_its_code_alike():
    resolution = resolve_jurisdiction(_location("Lagos", "Lagos", "Nigeria"))
    for allowed in ((), ("NG",), ("NG-LA",), ("GB",)):
        assert jurisdiction_permits(resolution, allowed=allowed) is jurisdiction_permits(
            "NG-LA", allowed=allowed
        )


# --------------------------------------------------------------------------- #
# Codes match by code, never by substring
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("code", "scope", "within"),
    [
        ("US-NY", "US-NY", True),
        ("US-NY", "US", True),
        ("GB-ENG", "GB", True),
        ("US-NY", "US-N", False),  # a prefix is not containment
        ("NG-LA", "NG-L", False),
        ("US-TX", "US-T", False),
        ("GB", "GB-ENG", False),
        ("US", "US-TX", False),
        ("US", "USA", False),
        ("NG", "N", False),
        ("CA-ON", "CA-O", False),
        ("US-TX", "UNRESOLVED", False),
        (UNRESOLVED, UNRESOLVED, False),
        (None, "US", False),
        ("US", None, False),
    ],
)
def test_code_containment_is_never_a_substring_test(code, scope, within):
    assert code_within(code, scope) is within


def test_overlap_is_containment_either_way_and_fails_closed_on_non_codes():
    assert codes_may_overlap("US-TX", "US") and codes_may_overlap("US", "US-TX")
    assert not codes_may_overlap("US-NY", "US-N")
    assert not codes_may_overlap("US-TX", "US-CA")
    assert not codes_may_overlap("GB", "KE")
    for garbage in (None, "", UNRESOLVED, "york", "US-"):
        assert codes_may_overlap("US-NY", garbage)


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("New York", "US-NY"),
        ("NY", "US-NY"),
        ("York", None),
        ("New", None),
        ("Jersey", None),
        ("New Jersey", "US-NJ"),
        ("Virginia", "US-VA"),
        ("West Virginia", "US-WV"),
        ("Carolina", None),
    ],
)
def test_subdivision_names_match_whole_names_only(state, expected):
    """ "York" is not "New York", and "Virginia" is not "West Virginia"."""
    assert subdivision_code_of("US", state) == expected


# --------------------------------------------------------------------------- #
# The subdivision table
# --------------------------------------------------------------------------- #


def test_every_subdivision_code_is_a_storable_jurisdiction_code():
    for country, divisions in SUBDIVISION_JURISDICTIONS.items():
        assert is_jurisdiction_code(country)
        for part in divisions:
            code = f"{country}-{part}"
            assert is_jurisdiction_code(code), code
            assert len(code) <= JURISDICTION_CODE_MAX_LENGTH


def test_the_subdivision_level_countries_are_the_documented_set():
    assert set(SUBDIVISION_JURISDICTIONS) == {"US", "CA", "AU", "NG"}
    assert len(SUBDIVISION_JURISDICTIONS["NG"]) == 37  # 36 states and the FCT
    assert len(SUBDIVISION_JURISDICTIONS["US"]) == 51  # 50 states and DC


def test_every_subdivision_reads_back_from_its_own_names_and_code():
    for country, divisions in SUBDIVISION_JURISDICTIONS.items():
        for part, names in divisions.items():
            expected = f"{country}-{part}"
            assert subdivision_code_of(country, expected) == expected
            assert subdivision_code_of(country, part) == expected
            for name in names:
                assert subdivision_code_of(country, name) == expected, (country, name)


def test_nigeria_stays_out_of_the_market_engine_table():
    """Adding Nigeria's states to ``places_data.SUBDIVISIONS`` would make "Lagos" read as
    the state in every market match. The jurisdiction table is separate."""
    assert "NG" not in SUBDIVISIONS
    lagos = parse_market("Lagos", default_country="NG")
    assert lagos is not None and lagos.city == "lagos" and lagos.region is None


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("United States", "US"),
        ("us", "US"),
        ("United Kingdom", "GB"),
        ("England", "GB"),
        ("Nigeria", "NG"),
        ("Kenya", "KE"),
        ("Narnia", None),
        ("", None),
        (None, None),
    ],
)
def test_country_reading(text, code):
    assert country_code_of(text) == code
