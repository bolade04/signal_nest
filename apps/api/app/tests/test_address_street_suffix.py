"""D1: a street suffix in a business address is not country or subdivision evidence.

A coverage rule's ``business_address`` is what
:func:`app.jurisdiction.resolver.resolve_jurisdiction` compares with the location's own fields
(``_address_check``). It used to read the address with the market parser, which splits a final
2-3 letter word off a comma-free text as a code ("Dallas TX"). A street-only address therefore
named a place it does not name: "123 Main St" was Sao Tome (ST), "1 Market Pl" Poland (PL) and
"3 Kings Ct" Connecticut (US-CT), so a Dallas, TX location became UNRESOLVED.

The resolver now reads the address with
:func:`app.geography.markets.parse_business_address`: a comma-free street line (a house number,
street words and a street-type abbreviation, optionally a post-directional) states no place for
certain. Under the operator's ruling of 2026-11-09 on W21-REV-D1STREET r1 C-3, ambiguous address
text is never read as stating nothing: a street line whose last words may also be a town and a
state or country code is "ambiguous", and if that code contradicts the location the answer is
UNRESOLVED with the reason ``address_suffix_ambiguous`` and a fixed clarification.
These tests prove:

* the defect cases resolve to the location's own code, and the address states nothing;
* clear street-only addresses with the usual suffixes of six countries (US, GB, CA, AU, NG, KE)
  do too;
* ambiguous street lines ("1500 Broadway Hartford CT", "No 3 Ajose Adeogun St", "1500 Broadway
  CT", "123 MAIN ST") are UNRESOLVED with the clarification when their code contradicts, resolve
  when it agrees, and their clarified forms resolve or are refused as their text says;
* a house number in second place counts only after a unit label ("No 3 ...", "Level 3 ...");
* explicit geography still counts: a comma-separated component ("123 Main St, Dallas, TX",
  "10 Rua X, ST"), a code ("US-CT"), a name or alias, a bare "PL", and a comma-free text that is
  not a street line ("Hartford CT", "123 Main St Hartford CT", "1500 Broadway USA");
* contradicting addresses are still refused (UNRESOLVED), including a subdivision code after a
  street ("1500 Broadway US-CT") and a number after a street name ("Marszalkowska 1 Warszawa PL");
* any text that is not a street line reads exactly as an observed market does, and only the
  business-address reader reads a street line differently (the live Scout readers do not);
* lane 5DW2FIX's realistic-address corpus (924 rows) resolves to each row's own location;
* a stored rule, selected by ``location_id`` as ``jobs/pipeline.py`` selects a run's rule,
  resolves the same on SQLite and PostgreSQL, and another tenant's rule never enters.

The live scout path (``parse_observed``, ``parse_market`` and the engine) is not changed.
"""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import Base
from app.db.seed import CITIES
from app.geography.geocoder import fixture_geocode
from app.geography.markets import parse_business_address, parse_market, parse_observed
from app.jurisdiction.codes import UNRESOLVED
from app.jurisdiction.resolver import (
    ADDRESS_SUFFIX_AMBIGUOUS,
    CLARIFICATIONS,
    jurisdiction_permits,
    resolve_jurisdiction,
)
from app.locations.models import BusinessLocation, GeoCoverageRule
from app.tests._auth2_support import pg_database, require_postgres


def _location(city, state, country) -> SimpleNamespace:
    return SimpleNamespace(city=city, state_province=state, country=country)


def _resolve(location, address):
    return resolve_jurisdiction(location, [SimpleNamespace(business_address=address)])


def _states_nothing(resolution) -> bool:
    """The address added no evidence line: it neither agreed nor contradicted."""
    return not any("business address" in line for line in resolution.evidence)


DALLAS = _location("Dallas", "TX", "US")
HARTFORD = _location("Hartford", "CT", "US")
LONDON = _location("London", "England", "United Kingdom")
TORONTO = _location("Toronto", "ON", "Canada")
SYDNEY = _location("Sydney", "NSW", "Australia")
LAGOS = _location("Lagos", "Lagos", "Nigeria")
GARKI = _location("Garki", "FCT", "Nigeria")
NAIROBI = _location("Nairobi", "Nairobi", "Kenya")
WARSAW = _location("Warsaw", "Masovia", "Poland")
SAO_TOME = _location("Sao Tome", "Agua Grande", "Sao Tome and Principe")

#: The defect as measured on main e3571ac8 (lane F1LOC, D1): each was UNRESOLVED for Dallas.
DEFECT = ["123 Main St", "1 Main St.", "221B Baker St", "1 Market Pl", "3 Kings Ct"]


# --------------------------------------------------------------------------- #
# The defect
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("address", DEFECT)
def test_a_street_only_address_resolves_to_the_location(address):
    resolution = _resolve(DALLAS, address)
    assert resolution.code == "US-TX"
    assert resolution.is_resolved
    assert _states_nothing(resolution), resolution.evidence


@pytest.mark.parametrize("address", DEFECT)
def test_a_street_line_states_no_place(address):
    stated = parse_business_address(address)
    assert stated is not None
    assert (stated.country, stated.region) == (None, None)


#: Clear street-only addresses with the usual suffixes of the six countries, each for a location
#: there: one street-name word, the suffix written as a street type. Covers the colliding
#: abbreviations (St, Pl, Ct, Cl, Gr), a label before the house number ("No", "Plot", "Flat"),
#: a unit after "#", a pre-directional and post-directionals.
STREET_LINES = [
    *[
        (DALLAS, "US-TX", a)
        for a in [
            *DEFECT,
            "3 Kings Ct #5",
            "No 3 Kings Ct",
            "100 N Main St",
            "1200 Pennsylvania Ave SE",
            "100 Main St NE",
            "500 Elm Blvd",
            "9 Oak Ln",
            "12 Harbor Dr",
            "7 High Rd",
        ]
    ],
    *[
        (LONDON, "GB", a)
        for a in [
            "10 Downing St",
            "221B Baker St",
            "Flat 2 10 Downing St",
            "12 Rose Cl",
            "1 Pine Gr",
            "4 Abbey Rd",
            "8 Kings Cres",
            "2 Chester Sq",
            "3 Royal Ter",
            "5 Kew Gdns",
        ]
    ],
    *[
        (TORONTO, "CA-ON", a)
        for a in ["1 Front St", "100 King St W", "25 Bay St", "10 Maple Cres", "40 Bloor St E"]
    ],
    *[
        (SYDNEY, "AU-NSW", a)
        for a in [
            "1 Martin Pl",
            "12 Rose Cl",
            "5 Ocean Pde",
            "3 Hill Tce",
            "9 Kings Cct",
            "1 Pine Gr",
            "2 Bay Esp",
            "15 Banksia Ct",
            "1 Macquarie St",
        ]
    ],
    *[
        (LAGOS, "NG-LA", a)
        for a in [
            "12 Awolowo Rd",
            "5 Mambilla Cl",
            "2 Glover Ct",
            "7 Broad St",
        ]
    ],
    *[(GARKI, "NG-FC", a) for a in ["No 5 Aso Dr", "3 Lake Chad Cres", "Plot 10 Mambilla St"]],
    *[
        (NAIROBI, "KE", a)
        for a in ["14 Riverside Dr", "1 Kenyatta Ave", "7 Muthaiga Cl", "10 Kimathi St"]
    ],
    # A house number after a unit label (W21-REV-D1STREET r1, C-2).
    (SYDNEY, "AU-NSW", "Level 3 1 Martin Pl"),
    (DALLAS, "US-TX", "Suite 5 123 Main St"),
    (LONDON, "GB", "Unit 5 12 Rose Cl"),
    (DALLAS, "US-TX", "Lot 7 3 Kings Ct"),
]


@pytest.mark.parametrize(
    ("location", "code", "address"), STREET_LINES, ids=[a for _, _, a in STREET_LINES]
)
def test_street_only_addresses_in_six_countries_resolve_to_their_location(location, code, address):
    resolution = _resolve(location, address)
    assert resolution.code == code
    assert _states_nothing(resolution), resolution.evidence
    stated = parse_business_address(address)
    assert (stated.country, stated.region) == (None, None)


# --------------------------------------------------------------------------- #
# Explicit geography still counts
# --------------------------------------------------------------------------- #

AGREEING = [
    (DALLAS, "US-TX", "123 Main St, Dallas, TX", ("US", "US-TX")),
    (DALLAS, "US-TX", "123 Main St, Dallas, TX 75201, US", ("US", "US-TX")),
    (DALLAS, "US-TX", "Dallas, United States", ("US", None)),
    (DALLAS, "US-TX", "US-TX", ("US", "US-TX")),
    (DALLAS, "US-TX", "123 Main St USA", ("US", None)),
    (DALLAS, "US-TX", "1500 Broadway USA", ("US", None)),
    (HARTFORD, "US-CT", "US-CT", ("US", "US-CT")),
    (HARTFORD, "US-CT", "Hartford, CT", ("US", "US-CT")),
    (HARTFORD, "US-CT", "Hartford CT", ("US", "US-CT")),
    (HARTFORD, "US-CT", "Hartford CT 06103", ("US", "US-CT")),
    (HARTFORD, "US-CT", "New Haven CT", ("US", "US-CT")),
    (HARTFORD, "US-CT", "123 Main St Hartford CT", ("US", "US-CT")),
    (HARTFORD, "US-CT", "3 Kings Ct, Hartford, CT", ("US", "US-CT")),
    (SAO_TOME, "ST", "10 Rua X, ST", ("ST", None)),
    (SAO_TOME, "ST", "10 Rua X, Sao Tome and Principe", ("ST", None)),
    (SAO_TOME, "ST", "ST", ("ST", None)),
    (WARSAW, "PL", "PL", ("PL", None)),
    (WARSAW, "PL", "Warsaw, PL", ("PL", None)),
    (LONDON, "GB", "10 Downing St UK", ("GB", None)),
    (LONDON, "GB", "10 Downing St, London SW1A 2AA, UK", ("GB", "GB:london")),
    (HARTFORD, "US-CT", "1500 Broadway US-CT", ("US", "US-CT")),
]


@pytest.mark.parametrize(
    ("location", "code", "address", "stated"), AGREEING, ids=[a for _, _, a, _ in AGREEING]
)
def test_explicit_geography_in_an_address_still_counts(location, code, address, stated):
    """A comma component, a code, a name or alias, a bare code and a comma-free text that is
    not a street line are read as before, and agree with a location they name."""
    resolution = _resolve(location, address)
    assert resolution.code == code
    assert resolution.evidence[-1] == f"coverage rule 0 business address {address!r} agrees"
    found = parse_business_address(address)
    assert (found.country, found.region) == stated


# --------------------------------------------------------------------------- #
# Ambiguous street lines (operator ruling of 2026-11-09 on W21-REV-D1STREET r1 C-3)
# --------------------------------------------------------------------------- #

#: Street lines whose last words may also be a town and a code, and whose code contradicts the
#: location: two or more street-name words, or a suffix not written as a street type (upper
#: case in a mixed-case line, or a line in one case), or a post-directional after two names.
AMBIGUOUS = [
    (DALLAS, "1 St Paul St", "country ST"),
    (DALLAS, "5 St Marks Pl", "country PL"),
    (DALLAS, "8 Spring Hill Ct", "subdivision US-CT"),
    (LAGOS, "No 3 Ajose Adeogun St", "country ST"),
    (LAGOS, "Plot 1 Adeola Odeku St", "country ST"),
    (DALLAS, "1500 Broadway Hartford CT", "subdivision US-CT"),
    (LONDON, "1500 Broadway Hartford CT", "country US"),
    (LAGOS, "1500 Broadway Hartford CT", "country US"),
    (DALLAS, "1500 N Broadway Hartford CT", "subdivision US-CT"),
    (DALLAS, "1500 Broadway CT", "subdivision US-CT"),
    (DALLAS, "1500 Broadway PL", "country PL"),
    (DALLAS, "06103 Hartford CT", "subdivision US-CT"),
    (DALLAS, "00-001 Warszawa PL", "country PL"),
    (DALLAS, "1234 Providencia Santiago CL", "country CL"),
    (DALLAS, "10 Ermou Athens GR", "country GR"),
    (DALLAS, "1 Rua X Sao Tome ST", "country ST"),
    (DALLAS, "1500 Broadway\nHartford CT", "subdivision US-CT"),
    (DALLAS, "1500 Broadway; CT", "subdivision US-CT"),
    (DALLAS, "3 Kings CT", "subdivision US-CT"),
    (DALLAS, "123 MAIN ST", "country ST"),
    (DALLAS, "123 main st", "country ST"),
    (DALLAS, "221B baker st", "country ST"),
    (DALLAS, "100 Martin Luther King Ave SE", "country SE"),
]


@pytest.mark.parametrize(
    ("location", "address", "conflict"), AMBIGUOUS, ids=[repr(a) for _, a, _ in AMBIGUOUS]
)
def test_an_ambiguous_street_line_that_may_contradict_is_unresolved_with_a_clarification(
    location, address, conflict
):
    resolution = _resolve(location, address)
    assert resolution.code == UNRESOLVED
    assert resolution.is_resolved is False
    assert resolution.reason == ADDRESS_SUFFIX_AMBIGUOUS
    assert resolution.evidence[-1] == (
        f"unresolved: coverage rule 0 business address {address!r} is ambiguous: "
        f"a street type or {conflict}"
    )
    assert jurisdiction_permits(resolution) is False
    # The clarification is fixed: no address text, no evidence.
    message = CLARIFICATIONS[resolution.reason]
    assert address not in message and conflict not in message


#: The same kind of line, for a location its code agrees with, or with no code reading at all.
AMBIGUOUS_CONSISTENT = [
    (HARTFORD, "US-CT", "1500 Broadway Hartford CT"),
    (HARTFORD, "US-CT", "3 Kings CT"),
    (HARTFORD, "US-CT", "8 Spring Hill Ct"),
    (SAO_TOME, "ST", "No 3 Ajose Adeogun St"),
    (SAO_TOME, "ST", "123 MAIN ST"),
    (WARSAW, "PL", "00-001 Warszawa PL"),
    (WARSAW, "PL", "5 St Marks Pl"),
    (GARKI, "NG-FC", "3 Lake Chad Cres"),
    (TORONTO, "CA-ON", "100 KING ST W"),
]


@pytest.mark.parametrize(
    ("location", "code", "address"),
    AMBIGUOUS_CONSISTENT,
    ids=[f"{c}:{a}" for _, c, a in AMBIGUOUS_CONSISTENT],
)
def test_an_ambiguous_street_line_that_cannot_contradict_resolves(location, code, address):
    resolution = _resolve(location, address)
    assert resolution.code == code
    assert resolution.reason is None
    assert _states_nothing(resolution), resolution.evidence


#: The supported clarifications: the street type or direction in full (the line then states
#: nothing), or the town and state or country after a comma (then explicit geography).
CLARIFIED = [
    (DALLAS, "1500 Broadway Hartford Court", "US-TX", None),
    (DALLAS, "1500 Broadway Court", "US-TX", None),
    (DALLAS, "8 Spring Hill Court", "US-TX", None),
    (DALLAS, "1 St Paul Street", "US-TX", None),
    (DALLAS, "123 MAIN STREET", "US-TX", None),
    (DALLAS, "100 Martin Luther King Ave Southeast", "US-TX", None),
    (LAGOS, "No 3 Ajose Adeogun Street", "NG-LA", None),
    (LAGOS, "No 3 Ajose Adeogun St, Lagos, Nigeria", "NG-LA", "agrees"),
    (HARTFORD, "1500 Broadway, Hartford, CT", "US-CT", "agrees"),
    (DALLAS, "1500 Broadway, Hartford, CT", UNRESOLVED, "states subdivision US-CT"),
    (DALLAS, "1500 Broadway, CT", UNRESOLVED, "states subdivision US-CT"),
]


@pytest.mark.parametrize(
    ("location", "address", "code", "evidence"),
    CLARIFIED,
    ids=[f"{loc.city}:{a}" for loc, a, _, _ in CLARIFIED],
)
def test_a_clarified_address_resolves_or_is_refused_as_its_text_says(
    location, address, code, evidence
):
    resolution = _resolve(location, address)
    assert resolution.code == code
    assert resolution.reason is None
    if evidence is None:
        assert _states_nothing(resolution), resolution.evidence
    else:
        prefix = "unresolved: " if code == UNRESOLVED else ""
        assert resolution.evidence[-1] == (
            f"{prefix}coverage rule 0 business address {address!r} {evidence}"
        )


def test_only_an_ambiguous_suffix_carries_a_reason():
    """Clear lines, explicit contradictions and unresolvable fields carry no reason; the one
    reason category has exactly one fixed message."""
    assert set(CLARIFICATIONS) == {ADDRESS_SUFFIX_AMBIGUOUS}
    assert CLARIFICATIONS[ADDRESS_SUFFIX_AMBIGUOUS] == (
        "We couldn't tell whether the end of the business address is a street type or a "
        "state or country. Write the street type or direction in full (for example "
        "'Street', 'Court' or 'Southeast'), or add the city and state or country after a "
        "comma (for example '1500 Broadway, Hartford, CT')."
    )
    assert _resolve(DALLAS, "123 Main St").reason is None
    assert _resolve(DALLAS, "Toronto, Ontario, Canada").reason is None
    assert _resolve(DALLAS, "1500 Broadway US-CT").reason is None
    assert resolve_jurisdiction(_location("Dallas", None, "US")).reason is None
    assert resolve_jurisdiction(None).reason is None


def test_sao_tome_written_alone_reads_as_on_main():
    """A component "Sao Tome" alone is not in the place tables (only "Sao Tome and Principe"),
    on main as here: it states nothing, and a Sao Tome location still resolves ST."""
    address = "10 Rua X, São Tomé"
    assert parse_business_address(address) == parse_observed(address)
    resolution = _resolve(SAO_TOME, address)
    assert resolution.code == "ST"
    assert _states_nothing(resolution)


CONTRADICTING = [
    (DALLAS, "Toronto, Ontario, Canada", "states country CA"),
    (DALLAS, "Sacramento, California, US", "states subdivision US-CA"),
    (DALLAS, "London, United Kingdom", "states country GB"),
    (DALLAS, "PL", "states country PL"),
    (DALLAS, "US-CT", "states subdivision US-CT"),
    (DALLAS, "Hartford CT", "states subdivision US-CT"),
    (DALLAS, "Hartford CT 06103", "states subdivision US-CT"),
    (DALLAS, "New Haven CT", "states subdivision US-CT"),
    (DALLAS, "123 Main St Hartford CT", "states subdivision US-CT"),
    (DALLAS, "123 Main Street Hartford CT 06103", "states subdivision US-CT"),
    (DALLAS, "PO Box 12 Hartford CT", "states subdivision US-CT"),
    (DALLAS, "3 Kings Ct, CT", "states subdivision US-CT"),
    (DALLAS, "123 Main St, ST", "states country ST"),
    (DALLAS, "10 Rua X, ST", "states country ST"),
    (DALLAS, "10 Rua X, Sao Tome and Principe", "states country ST"),
    (DALLAS, "1 Market Pl, PL", "states country PL"),
    (DALLAS, "Warsaw PL", "states country PL"),
    (DALLAS, "10 Downing St UK", "states country GB"),
    (LONDON, "1500 Broadway USA", "states country US"),
    (LONDON, "123 Main St, Dallas, TX", "states country US"),
    (LAGOS, "12 Awolowo Rd, Abeokuta, Ogun State, Nigeria", "states subdivision NG-OG"),
    (NAIROBI, "1 Kenyatta Ave, Kampala, Uganda", "states country UG"),
    # A subdivision code after a typeless street is explicit (W21-REV-D1STREET r1, C-1).
    (DALLAS, "1500 Broadway US-CT", "states subdivision US-CT"),
    (DALLAS, "1500 Broadway US CT", "states subdivision US-CT"),
    (DALLAS, "1500 Broadway Hartford US-CT", "states subdivision US-CT"),
    # A number after a first word that is not a unit label is not a house number (C-2).
    (DALLAS, "Box 12 Hartford CT", "states subdivision US-CT"),
    (DALLAS, "Marszalkowska 1 Warszawa PL", "states country PL"),
    (DALLAS, "Ermou 10 Athens GR", "states country GR"),
    (DALLAS, "Providencia 1234 Santiago CL", "states country CL"),
    (LONDON, "Marszalkowska 1 Warszawa PL", "states country PL"),
    # Numbers with no street name are not a street line (C-4).
    (DALLAS, "12 14 PL", "states country PL"),
]


@pytest.mark.parametrize(
    ("location", "address", "conflict"), CONTRADICTING, ids=[a for _, a, _ in CONTRADICTING]
)
def test_contradicting_addresses_are_still_refused(location, address, conflict):
    resolution = _resolve(location, address)
    assert resolution.code == UNRESOLVED
    assert resolution.is_resolved is False
    assert resolution.evidence[-1] == (
        f"unresolved: coverage rule 0 business address {address!r} {conflict}"
    )


#: The live Scout readers' reading of street lines, as measured at main e3571ac8.
LIVE_READINGS = {
    "123 Main St": ("123 main", None, "ST"),
    "1 Market Pl": ("1 market", None, "PL"),
    "3 Kings Ct": ("3 kings", "US-CT", "US"),
    "1200 Pennsylvania Ave SE": ("1200 pennsylvania ave", None, "SE"),
}


@pytest.mark.parametrize("address", sorted(LIVE_READINGS))
def test_only_the_business_address_reader_reads_a_street_line_differently(address):
    """D1 changes only the business-address reader; any change to the live Scout reader is a
    separate, measured decision. ``parse_observed`` and ``parse_market`` (with and without a
    location country) still read these street lines as at main."""
    stated = parse_business_address(address)
    assert (stated.country, stated.region) == (None, None)
    for live in (
        parse_observed(address),
        parse_market(address),
        parse_market(address, default_country="US"),
    ):
        assert (live.city, live.region, live.country) == LIVE_READINGS[address]


@pytest.mark.parametrize(
    "address",
    sorted(
        {a for _, _, a, _ in AGREEING} | {a for _, a, _ in CONTRADICTING} | {"10 Rua X, São Tomé"}
    ),
)
def test_a_text_that_is_not_a_street_line_reads_as_an_observed_market(address):
    """Only a comma-free street line reads differently from ``parse_observed``."""
    assert parse_business_address(address) == parse_observed(address)


# --------------------------------------------------------------------------- #
# Realistic addresses: lane 5DW2FIX's corpus (P6-W21-5DW2FIX-01/proof/corpus.py,
# realistic_addresses(), sha256 3432e4b5...45fd), ported as data; plus the seed's own.
# --------------------------------------------------------------------------- #

_US_PLACES = [
    ("Dallas", "TX", "Texas", "75201"), ("Freehold", "NJ", "New Jersey", "07728"),
    ("Islamorada", "FL", "Florida", "33036"), ("Cumming", "GA", "Georgia", "30040"),
    ("Malibu", "CA", "California", "90265"), ("Bloomington", "IN", "Indiana", "47401"),
    ("Tuscaloosa", "AL", "Alabama", "35401"), ("Little Rock", "AR", "Arkansas", "72201"),
    ("Payson", "AZ", "Arizona", "85541"), ("Aspen", "CO", "Colorado", "81611"),
    ("Dover", "DE", "Delaware", "19901"), ("Boise", "ID", "Idaho", "83702"),
    ("Peoria", "IL", "Illinois", "61602"), ("Louisville", "KY", "Kentucky", "40202"),
    ("Grand Isle", "LA", "Louisiana", "70358"), ("Worcester", "MA", "Massachusetts", "01608"),
    ("Annapolis", "MD", "Maryland", "21401"), ("Kennebunk", "ME", "Maine", "04043"),
    ("Duluth", "MN", "Minnesota", "55802"), ("Branson", "MO", "Missouri", "65616"),
    ("Biloxi", "MS", "Mississippi", "39530"), ("Bozeman", "MT", "Montana", "59715"),
    ("Asheville", "NC", "North Carolina", "28801"), ("Lincoln", "NE", "Nebraska", "68502"),
    ("Easton", "PA", "Pennsylvania", "18042"), ("Myrtle Beach", "SC", "South Carolina", "29577"),
    ("Rapid City", "SD", "South Dakota", "57701"), ("Gatlinburg", "TN", "Tennessee", "37738"),
    ("Leesburg", "VA", "Virginia", "20175"), ("Portland", "OR", "Oregon", "97204"),
    ("Seattle", "WA", "Washington", "98101"), ("Albany", "NY", "New York", "12207"),
]  # fmt: skip
_OTHER_PLACES = [
    (("London", "England", "United Kingdom"), ["10 Downing St, London SW1A 2AA, UK"]),
    (("Toronto", "Ontario", "Canada"), ["100 King St W, Toronto, ON M5X 1A9, Canada", "ON-401"]),
    (("Sydney", "New South Wales", "Australia"), ["1 Martin Pl, Sydney NSW 2000, Australia"]),
    (("Lekki", "Lagos", "Nigeria"), ["Plot 5, Admiralty Way, Lekki, Lagos State, Nigeria",
                                     "No. 3, Admiralty Way, Lekki, Lagos, NG"]),
    (("Garki", "FCT", "Nigeria"), ["Plot 1, Garki, Abuja FCT, Nigeria", "Garki, FCT, Nigeria"]),
    (("Nairobi", "Nairobi", "Kenya"), ["Kenyatta Ave, Nairobi, Kenya"]),
    (("Al Ain", "Abu Dhabi", "United Arab Emirates"),
     ["KHALIFA ST, AL-AIN", "AL-AIN", "Al-Ain, UAE", "Khalifa St, Al-Ain, Abu Dhabi, UAE"]),
    (("AlUla", "Medina", "Saudi Arabia"), ["AL-ULA", "Old Town, AL-ULA, Saudi Arabia"]),
    (("Kut", "Wasit", "Iraq"), ["AL-KUT", "Main St, AL-KUT, Iraq"]),
    (("Al Bab", "Aleppo", "Syria"), ["AL-BAB, Syria"]),
    (("Ca Mau", "Ca Mau", "Vietnam"), ["Ca-Mau, Vietnam", "CA-MAU", "12 Ly Thuong Kiet, Ca-Mau"]),
    (("Ba Ria", "Ba Ria-Vung Tau", "Vietnam"), ["Ba-Ria, Vietnam", "BA-RIA"]),
    (("Munich", "Bavaria", "Germany"), ["Marienplatz 1, 80331 Munich, DE-BY"]),
]  # fmt: skip


def _realistic_addresses() -> list[tuple[tuple[str, str, str], str]]:
    out = []
    for city, code, state, zipcode in _US_PLACES:
        for state_field in (state, code):
            location = (city, state_field, "United States")
            for address in (
                f"100 Main St, {city}, {code} {zipcode}",
                f"100 Main St, {city}, {code} {zipcode}, USA",
                f"100 Main St, {city}, {state}",
                f"{city} {code} {zipcode}",
                f"US-9, {city}, {code} {zipcode}",
                f"4400 US-9, {city}, {code} {zipcode}",
                f"Mile Marker 5, US-1, {city}, {code} {zipcode}",
                "US-1",
                f"{code}-12",
                f"Suite 5, {code}-12",
                f"{code}-400, {city}, {code} {zipcode}",
                f"21150 {code}-1, {city}, {code} {zipcode}",
                f"{code.lower()}-37, {city}, {state}",
                f"PO Box 12, {city}, {code} {zipcode}",
            ):
                out.append((location, address))
    for location, addresses in _OTHER_PLACES:
        out += [(location, address) for address in addresses]
    for city in CITIES:
        geo = fixture_geocode(city["geocode"])
        location = (geo.city, geo.state_province, geo.country)
        out.append((location, f"{geo.city}, {geo.country}"))
    return out


REALISTIC = _realistic_addresses()


def test_the_realistic_corpus_has_the_5dw2fix_size():
    assert len(REALISTIC) == 924
    assert len(set(REALISTIC)) == len(REALISTIC)


def test_the_5dw2fix_realistic_addresses_resolve_to_their_location():
    """Every row's address belongs to its location, so each resolves to the location's own
    code; and none is a street line, so each reads exactly as an observed market does."""
    wrong = []
    for (city, state, country), address in REALISTIC:
        location = _location(city, state, country)
        own = resolve_jurisdiction(location).code
        got = _resolve(location, address).code
        if own == UNRESOLVED or got != own:
            wrong.append((location, address, own, got))
        if parse_business_address(address) != parse_observed(address):
            wrong.append((location, address, "reads differently"))
    assert wrong == []


# --------------------------------------------------------------------------- #
# A stored rule, on SQLite and PostgreSQL
# --------------------------------------------------------------------------- #

#: Two tenants, each a Dallas, TX location with one stored rule.
TENANTS = {
    "a": {"org": "org-d1-a", "ws": "ws-d1-a", "address": "123 Main St"},
    "b": {"org": "org-d1-b", "ws": "ws-d1-b", "address": "Toronto, Ontario, Canada"},
}


def _seed(s: Session) -> None:
    from app.brands.models import Brand
    from app.organizations.models import Organization, Workspace

    for key, t in TENANTS.items():
        s.add(Organization(id=t["org"], name=t["org"], slug=t["org"]))
        s.flush()
        s.add(Workspace(id=t["ws"], organization_id=t["org"], name=t["ws"], slug=t["ws"]))
        s.flush()
        s.add(Brand(id=f"br-{key}", organization_id=t["org"], workspace_id=t["ws"], name=key))
        s.flush()
        s.add(
            BusinessLocation(
                id=f"loc-{key}",
                organization_id=t["org"],
                workspace_id=t["ws"],
                brand_id=f"br-{key}",
                name=f"Dallas {key}",
                address=t["address"],
                city="Dallas",
                state_province="TX",
                country="US",
            )
        )
        s.flush()
        s.add(
            GeoCoverageRule(
                id=f"geo-{key}",
                organization_id=t["org"],
                workspace_id=t["ws"],
                location_id=f"loc-{key}",
                coverage_type="radius",
                business_address=t["address"],
                country="United States",
                state="Texas",
            )
        )
        s.flush()


@pytest.fixture(params=["sqlite", "postgresql"])
def make(request, tmp_path) -> Iterator[sessionmaker]:
    """One database per test: SQLite with foreign keys on, or PostgreSQL when
    ``TEST_POSTGRES_URL`` is set (``require_postgres``: unset fails in CI, skips elsewhere)."""
    if request.param == "sqlite":
        engine = create_engine(f"sqlite:///{tmp_path / 'd1.db'}", future=True)

        @event.listens_for(engine, "connect")
        def _fk(dbapi_conn, _):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

        yield from _world(engine)
        return
    require_postgres()
    with pg_database("sn_d1st") as url:  # pragma: no cover - gated on PostgreSQL
        yield from _world(create_engine(url, future=True))


def _world(engine) -> Iterator[sessionmaker]:
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with maker() as s:
        _seed(s)
        s.commit()
    try:
        yield maker
    finally:
        engine.dispose()


def _stored_resolution(maker: sessionmaker, key: str):
    """The location and its rules, selected by ``GeoCoverageRule.location_id`` alone, as
    ``jobs/pipeline.py`` selects a run's rule; then every selected rule's tenant is asserted to be
    the location's own."""
    t = TENANTS[key]
    with maker() as s:
        location = s.get(BusinessLocation, f"loc-{key}")
        assert (location.organization_id, location.workspace_id) == (t["org"], t["ws"])
        rules = s.scalars(
            select(GeoCoverageRule).where(GeoCoverageRule.location_id == location.id)
        ).all()
        assert [r.id for r in rules] == [f"geo-{key}"]
        assert all((r.organization_id, r.workspace_id) == (t["org"], t["ws"]) for r in rules)
        return resolve_jurisdiction(location, rules), [r.business_address for r in rules]


def test_a_stored_street_only_address_resolves_on_both_engines(make):
    resolution, stored = _stored_resolution(make, "a")
    assert stored == ["123 Main St"]
    assert resolution.code == "US-TX"
    assert _states_nothing(resolution)
    # The other tenant's own contradicting address is still refused, and never enters "a".
    other, _ = _stored_resolution(make, "b")
    assert other.code == UNRESOLVED
    assert other.evidence[-1].endswith("states country CA")


def test_a_stored_explicit_component_is_still_read_on_both_engines(make):
    with make() as s:
        s.get(GeoCoverageRule, "geo-a").business_address = "123 Main St, ST"
        s.commit()
    resolution, stored = _stored_resolution(make, "a")
    assert stored == ["123 Main St, ST"]
    assert resolution.code == UNRESOLVED
    assert resolution.evidence[-1].endswith("states country ST")
    with make() as s:
        s.get(GeoCoverageRule, "geo-a").business_address = "123 Main St, Dallas, TX"
        s.commit()
    resolution, _ = _stored_resolution(make, "a")
    assert resolution.code == "US-TX"
    assert resolution.evidence[-1].endswith("agrees")
