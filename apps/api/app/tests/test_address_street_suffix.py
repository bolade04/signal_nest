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
* the first part of an address with commas counts when it is ambiguous, whatever the later parts
  state ("1500 Broadway Hartford CT, USA"; "123 MAIN ST, DALLAS", FU-R1), and the address is read
  as its first part when the later parts are only a postcode or a unit ("1500 Broadway CT, Suite
  200");
* in an address with commas, a part after the first that ends in a code is read: "1500
  Broadway, Dallas TX" states US-TX, "1500 Broadway, Hartford CT" is ambiguous (CT may be a
  street type), a postcode alone is left out, and an unlisted code form ("DE-BY"), alone or after a
  town ("Munich DE-BY"), reads as before (W21-REV-D1STREET r2 C-9);
* a code that names two or more areas ("Dallas, GA": Georgia or Gabon) is never read as nothing:
  it is refused when every reading contradicts, asks for clarification when it ends an ambiguous
  street line ("1 SPRING HILL ST NE"), and agrees in the location's own area (C-8);
* line breaks and semicolons separate parts as commas do (C-10);
* an unlisted code form written with a space ("Munich DE BY", "Ikeja NG LA") is read as a group of
  both readings, the country of the code form or the code after a word, under C-8's rule
  (W21-REV-D1STREET r2 re-bind C-12);
* an ordinal street name ("123 4th St") is a street name, and a street type before a
  post-directional ("St NE" alone, "5th St NE" with no house number) is an ambiguous group that
  asks for clarification, never explicit text (re-bind 2 C-13 and C-14; lane r9 probe R17);
* a single-area direction after a street with no house number ("5th St SE": Sweden) is ambiguous
  too, in a comma-free address and in a part (re-bind 3 C-15); in a part after the first, a unit
  number ("Flat No 3") states nothing, and a route or a code after a number or a unit ("12000 FM
  1960", "Unit 5 NY 10001", "123 Main St Fl 3") is ambiguous, never explicit and never nothing
  (re-bind 3 C-16);
* any other text reads exactly as an observed market does, and only the business-address reader
  reads differently (the live Scout readers do not);
* lane 5DW2FIX's realistic-address corpus (924 rows) resolves to each row's own location, except
  "KHALIFA ST, AL-AIN", whose "ST" may be Sao Tome and which names no country, so it asks for
  clarification (FU-R1);
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
    ADDRESS_DIRECTION_AMBIGUOUS,
    ADDRESS_NAMES_ANOTHER_PLACE,
    ADDRESS_PLACE_UNCLEAR,
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
MUNICH = _location("Munich", "Bavaria", "Germany")
AL_AIN = _location("Al Ain", "Abu Dhabi", "United Arab Emirates")
MALIBU = _location("Malibu", "CA", "United States")

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
    (DALLAS, "3 Kings CT", "subdivision US-CT"),
    (DALLAS, "123 MAIN ST", "country ST"),
    (DALLAS, "123 main st", "country ST"),
    (DALLAS, "221B baker st", "country ST"),
    (DALLAS, "100 Martin Luther King Ave SE", "country SE"),
    # A part after the first that ends in a street-type abbreviation that is also a code (FU-3).
    (DALLAS, "1500 Broadway, Hartford CT", "subdivision US-CT"),
    (LONDON, "1500 Broadway, Hartford CT", "country US"),
    (DALLAS, "1500 Broadway, Hartford CT 06103", "subdivision US-CT"),
    (DALLAS, "1500 Broadway, Hartford CT, 06103", "subdivision US-CT"),
    (DALLAS, "1500 Broadway, Hartford CT, USA", "subdivision US-CT"),
    (DALLAS, "1 Main St, Warszawa PL", "country PL"),
    (DALLAS, "10 Rua X, Sao Tome ST", "country ST"),
    (DALLAS, "Suite 5, 1500 Broadway CT", "subdivision US-CT"),
    (LONDON, "Flat 2, Kings Ct", "country US"),
    # The first part of an address with commas (W21-REV-D1STREET C-6): with a country stated,
    # or when the later parts are only a postcode or a unit.
    *[
        (DALLAS, a, "subdivision US-CT")
        for a in [
            "Hartford CT, USA",
            "1500 Broadway Hartford CT, USA",
            "1500 Broadway St Hartford CT, USA",
            "1500 Broadway Hartford CT, Suite 5",
            "1500 Broadway CT, Suite 200",
            "1 Asylum Hartford CT, 06103",
            "8 Spring Hill Ct, Suite 5",
        ]
    ],
    # The first part, whatever the later parts state (FU-R1): a town alone ("..., DALLAS") neither
    # settles the first part's code reading nor rules it out.
    (DALLAS, "123 MAIN ST, DALLAS", "country ST"),
    (HARTFORD, "123 MAIN ST, DALLAS", "country ST"),
    (HARTFORD, "123 MAIN ST, HARTFORD", "country ST"),
    (DALLAS, "1500 Broadway Hartford CT, Hartford", "subdivision US-CT"),
    (LONDON, "1500 Broadway Hartford CT, Hartford", "country US"),
    (DALLAS, "Hartford CT, Hartford", "subdivision US-CT"),
    (DALLAS, "1500 Broadway Hartford CT, Suite 5, Hartford", "subdivision US-CT"),
    (DALLAS, "8 Spring Hill Ct, Dallas", "subdivision US-CT"),
    (DALLAS, "1500 Broadway PL, Warszawa", "country PL"),
    # ... also when the address is the location's own but names no country or state that rules the
    # code reading out (FU-R1's cost; the country after a comma resolves them, see below).
    (LONDON, "10 DOWNING ST, LONDON SW1A 2AA", "country ST"),
    (MALIBU, "5945 MAIN ST, MALIBU, CA 90265", "country ST"),
    (AL_AIN, "KHALIFA ST, AL-AIN", "country ST"),
    (LAGOS, "No 3 Ajose Adeogun St, Victoria Island, Lagos", "country ST"),
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
    (HARTFORD, "US-CT", "1500 Broadway, Hartford CT"),
    (WARSAW, "PL", "1 Main St, Warszawa PL"),
    (SAO_TOME, "ST", "10 Rua X, Sao Tome ST"),
    (DALLAS, "US-TX", "Suite 5, 3 Kings Ct"),
    (MUNICH, "DE", "Marienplatz 1, 80331 Munich, DE-BY"),
    # FU-R1: the first part's code reading is the location's own area.
    (HARTFORD, "US-CT", "1500 Broadway Hartford CT, Hartford"),
    (HARTFORD, "US-CT", "Hartford CT, Hartford"),
    (SAO_TOME, "ST", "123 MAIN ST, SAO TOME"),
    (WARSAW, "PL", "1 MARKET PL, WARSZAWA"),
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
    # With one comma (FU-3): a code that is not a street type is read; "CT" stays ambiguous, so
    # it agrees with Hartford and leaves Dallas unresolved (AMBIGUOUS above), never "nothing".
    (DALLAS, "1500 Broadway, Dallas TX", "US-TX", "agrees"),
    (HARTFORD, "1500 Broadway, Dallas TX", UNRESOLVED, "states subdivision US-TX"),
    (HARTFORD, "1500 Broadway, Hartford CT", "US-CT", None),
    (DALLAS, "1500 Broadway, Hartford, CT, 06103, USA", UNRESOLVED, "states subdivision US-CT"),
    (HARTFORD, "1500 Broadway, Hartford, CT, 06103, USA", "US-CT", "agrees"),
]


#: Parts after the first, read in context of the whole address (FU-3): explicit geography in a
#: later part settles what it states, and rules out ambiguous readings in another country.
LATER_PARTS = [
    (DALLAS, "123 Main St, Dallas TX", "US-TX", "agrees"),
    (DALLAS, "3 Kings Ct, US-CT", UNRESOLVED, "states subdivision US-CT"),
    (LAGOS, "Dallas, US-TX", UNRESOLVED, "states country US"),
    (SYDNEY, "1 Martin Pl, Sydney NSW 2000", "AU-NSW", "agrees"),
    (LONDON, "Flat 2, Kings Ct, London, UK", "GB", "agrees"),
    (DALLAS, "Unit 2, 123 MAIN ST, DALLAS, TX", "US-TX", "agrees"),
    (DALLAS, "123 MAIN ST, USA", "US-TX", "agrees"),
    (DALLAS, "Suite 5, 1500 Broadway CT, Dallas, TX", "US-TX", "agrees"),
]


def test_the_whole_address_is_still_read_in_context():
    """Reading later parts on their own adds evidence; it never replaces the reading of the whole
    address, so a contradiction found in context ("St. John's" places "CA" in Canada) stays."""
    address = "St. John's, US-KY, CA"
    assert parse_business_address(address) == parse_observed(address)
    resolution = _resolve(DALLAS, address)
    assert resolution.code == UNRESOLVED
    assert resolution.reason is None


@pytest.mark.parametrize(
    ("location", "address", "code", "evidence"),
    LATER_PARTS,
    ids=[f"{loc.city}:{a}" for loc, a, _, _ in LATER_PARTS],
)
def test_a_later_part_ending_in_a_code_is_read_in_context(location, address, code, evidence):
    resolution = _resolve(location, address)
    assert resolution.code == code
    assert resolution.reason is None
    prefix = "unresolved: " if code == UNRESOLVED else ""
    assert resolution.evidence[-1] == (
        f"{prefix}coverage rule 0 business address {address!r} {evidence}"
    )


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


#: The first-part rows of AMBIGUOUS above (C-6), for the location their code names.
FIRST_PART_AGREEING = [
    "Hartford CT, USA",
    "1500 Broadway Hartford CT, USA",
    "1500 Broadway St Hartford CT, USA",
    "1500 Broadway Hartford CT, Suite 5",
    "1500 Broadway CT, Suite 200",
    "1 Asylum Hartford CT, 06103",
    "8 Spring Hill Ct, Suite 5",
]


@pytest.mark.parametrize("address", FIRST_PART_AGREEING)
def test_an_ambiguous_first_part_agrees_with_its_own_location(address):
    resolution = _resolve(HARTFORD, address)
    assert resolution.code == "US-CT"
    assert resolution.reason is None


@pytest.mark.parametrize(
    ("location", "code", "address"),
    [
        (DALLAS, "US-TX", "3 Kings Ct, Suite 5"),
        (DALLAS, "US-TX", "123 Main St, Suite 5"),
        (DALLAS, "US-TX", "123 MAIN ST, USA"),
        (AL_AIN, "AE", "KHALIFA ST, AL-AIN, UAE"),
        (DALLAS, "US-TX", "1500 Broadway Hartford CT, Dallas, TX"),
        # The supported clarifications of FU-R1's rows.
        (DALLAS, "US-TX", "123 MAIN ST, DALLAS, TX"),
        (DALLAS, "US-TX", "123 MAIN ST, DALLAS, USA"),
        (DALLAS, "US-TX", "123 MAIN STREET, DALLAS"),
        (DALLAS, "US-TX", "123 Main St, Dallas"),
        (LONDON, "GB", "10 DOWNING ST, LONDON SW1A 2AA, UK"),
        (MALIBU, "US-CA", "5945 MAIN ST, MALIBU, CA 90265, USA"),
    ],
)
def test_a_clear_or_settled_first_part_still_resolves(location, code, address):
    """A clear first part states nothing; explicit geography elsewhere settles an ambiguous one
    or rules it out (C-6, FU-R1); a first part that is not a street line reads as before."""
    resolution = _resolve(location, address)
    assert resolution.code == code
    assert resolution.reason is None


@pytest.mark.parametrize(
    ("address", "conflict"),
    [
        ("Hartford CT, Suite 5", "states subdivision US-CT"),
        ("US-CT, Suite 200", "states subdivision US-CT"),
        ("Warsaw PL, Apt 4", "states country PL"),
    ],
)
def test_an_address_whose_later_parts_are_only_units_reads_as_its_first_part(address, conflict):
    """C-6(b): when every later part is only a unit or a postcode, the address is read as its first
    part alone, as a comma-free address, so an explicit code there is still read, not lost behind
    the unit ("Hartford CT, Suite 5" reads as "Hartford CT")."""
    resolution = _resolve(DALLAS, address)
    assert resolution.code == UNRESOLVED
    assert resolution.reason is None
    assert resolution.evidence[-1] == (
        f"unresolved: coverage rule 0 business address {address!r} {conflict}"
    )


def test_only_an_ambiguous_suffix_carries_a_reason():
    """Clear lines, explicit contradictions and unresolvable fields carry no reason; D1's reason
    category has exactly one fixed message. GEO-P1 adds three categories for parts read wherever
    they stand (their messages are pinned in ``test_geography_part_places.py``)."""
    assert set(CLARIFICATIONS) == {
        ADDRESS_SUFFIX_AMBIGUOUS,
        ADDRESS_NAMES_ANOTHER_PLACE,
        ADDRESS_PLACE_UNCLEAR,
        ADDRESS_DIRECTION_AMBIGUOUS,
    }
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
    # A later part ending in an explicit subdivision code is explicit, not ambiguous (C-7).
    (DALLAS, "1500 Broadway, Hartford US-CT", "states subdivision US-CT"),
    (DALLAS, "1500 Broadway, Hartford US CT", "states subdivision US-CT"),
    # A unit label that is also a code is not a unit: "FL-12" is a Florida road (C-6).
    (DALLAS, "Suite 5, FL-12", "states subdivision US-FL"),
    # A semicolon separates parts as a comma does, so a lone "CT" is explicit (C-10).
    (DALLAS, "1500 Broadway; CT", "states subdivision US-CT"),
    # Explicit geography after an ambiguous first part is still read (FU-R1).
    (DALLAS, "123 MAIN ST, HARTFORD, CT", "states subdivision US-CT"),
    (DALLAS, "1500 Broadway Hartford CT, Hartford, CT", "states subdivision US-CT"),
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


# --------------------------------------------------------------------------- #
# A code of two or more areas (W21-REV-D1STREET r2 C-8), an unlisted code form after a town
# (C-9), and line breaks and semicolons as separators (C-10). The reviewer's proposed tests
# (r2/proposed/test_rev_r2_f6_proposed.py), with exact evidence, plus the lane's r7 rows.
# --------------------------------------------------------------------------- #

OMAHA = _location("Omaha", "NE", "US")
NIAMEY = _location("Niamey", "Niamey", "Niger")
ATLANTA = _location("Atlanta", "GA", "US")
LIBREVILLE = _location("Libreville", "Estuaire", "Gabon")
CUMMING = _location("Cumming", "GA", "US")
TIRANA = _location("Tirana", "Tirana", "Albania")
MUMBAI = _location("Mumbai", "MH", "India")

#: (a) An ambiguous street line whose post-directional NE may be Nebraska or Niger.
NE_LINES = [
    "1 SPRING HILL ST NE",
    "123 MAIN ST NE",
    "1 Spring Hill St NE",
    "123 MAIN ST NE, Dallas",
]


@pytest.mark.parametrize(
    ("location", "conflict"),
    [(DALLAS, "country NE or subdivision US-NE"), (LONDON, "country NE or country US")],
    ids=["Dallas", "London"],
)
@pytest.mark.parametrize("address", NE_LINES)
def test_an_ambiguous_street_line_ending_in_ne_asks_for_clarification(location, conflict, address):
    resolution = _resolve(location, address)
    assert resolution.code == UNRESOLVED
    assert resolution.reason == ADDRESS_SUFFIX_AMBIGUOUS
    assert resolution.evidence[-1] == (
        f"unresolved: coverage rule 0 business address {address!r} is ambiguous: "
        f"a street type or {conflict}"
    )


@pytest.mark.parametrize(("location", "code"), [(OMAHA, "US-NE"), (NIAMEY, "NE")])
@pytest.mark.parametrize("address", NE_LINES)
def test_an_ambiguous_ne_line_agrees_where_one_reading_is_the_location(location, code, address):
    resolution = _resolve(location, address)
    assert resolution.code == code
    assert resolution.reason is None


def test_a_clear_street_line_ending_in_ne_states_nothing():
    resolution = _resolve(DALLAS, "123 Main St NE")
    assert resolution.code == "US-TX"
    assert _states_nothing(resolution)


#: (b) Explicit text whose code names two areas, every reading contradicting the location.
TWO_AREA_CONTRADICTING = [
    (LONDON, "Dallas, GA", "country GA or country US"),
    (DALLAS, "Dallas, GA", "country GA or subdivision US-GA"),
    (LONDON, "Dallas GA", "country GA or country US"),
    (DALLAS, "GA", "country GA or subdivision US-GA"),
    (DALLAS, "5945 Main St, Malibu, CA 90265", "country CA or subdivision US-CA"),
    (LONDON, "5945 Main St, Malibu, CA 90265", "country CA or country US"),
    (DALLAS, "100 Main St, Kennebunk, ME 04043", "country ME or subdivision US-ME"),
    (DALLAS, "1500 Broadway, Dallas GA, USA", "country GA or subdivision US-GA"),
    (DALLAS, "1500 Broadway NE", "country NE or subdivision US-NE"),
    # A stated country cannot excuse explicit text ("Dallas GA" with "Canada").
    (TORONTO, "1500 Broadway, Dallas GA, Canada", "country GA or country US"),
    # A listed subdivision code keeps only the reading its prefix names: "US-CA" is never Canada,
    # "US-AL" never Albania, "US-GA" never Gabon.
    (TORONTO, "1500 Broadway, Malibu US-CA", "country US"),
    (TORONTO, "1500 Broadway US-CA", "country US"),
    (TIRANA, "1 Main St, Montgomery, US-AL", "country US"),
    (LIBREVILLE, "Dallas, US-GA", "country US"),
    (DALLAS, "Dallas, US-GA", "subdivision US-GA"),
]


@pytest.mark.parametrize(
    ("location", "address", "conflict"),
    TWO_AREA_CONTRADICTING,
    ids=[f"{loc.city}:{a}" for loc, a, _ in TWO_AREA_CONTRADICTING],
)
def test_a_code_of_two_areas_that_contradicts_in_every_reading_is_refused(
    location, address, conflict
):
    resolution = _resolve(location, address)
    assert resolution.code == UNRESOLVED
    assert resolution.reason is None
    assert resolution.evidence[-1] == (
        f"unresolved: coverage rule 0 business address {address!r} states {conflict}"
    )


TWO_AREA_AGREEING = [
    (ATLANTA, "US-GA", "Dallas, GA"),
    (LIBREVILLE, "GA", "Dallas, GA"),
    (MALIBU, "US-CA", "5945 Main St, Malibu, CA 90265"),
    (CUMMING, "US-GA", "100 Main St, Cumming, GA 30040"),
    (OMAHA, "US-NE", "1500 Broadway NE"),
    (DALLAS, "US-TX", "Dallas, TX"),
    (MALIBU, "US-CA", "1500 Broadway, Malibu US-CA"),
]


@pytest.mark.parametrize(
    ("location", "code", "address"),
    TWO_AREA_AGREEING,
    ids=[f"{loc.city}:{a}" for loc, _, a in TWO_AREA_AGREEING],
)
def test_a_code_of_two_areas_agrees_where_one_reading_is_the_location(location, code, address):
    resolution = _resolve(location, address)
    assert resolution.code == code
    assert resolution.reason is None


def test_a_stated_country_rules_out_an_ambiguous_lines_readings_elsewhere():
    """An ambiguous street line keeps its street reading, so a stated country rules out its code
    readings in another country ("123 MAIN ST NE, Toronto, Canada"); explicit text has no such
    reading (see "1500 Broadway, Dallas GA, Canada" above)."""
    assert _resolve(TORONTO, "123 MAIN ST NE, Toronto, Canada").code == "CA-ON"
    resolution = _resolve(DALLAS, "123 MAIN ST NE, USA")
    assert resolution.reason == ADDRESS_SUFFIX_AMBIGUOUS
    assert resolution.evidence[-1].endswith("is ambiguous: a street type or subdivision US-NE")


#: (C-9) An ISO 3166-2 form whose subdivision is not listed, alone or after a town and before any
#: postcode, states nothing, as on main and at 816fc941; its letters after the hyphen are no code.
UNLISTED_CODE_FORMS = [
    (MUNICH, "DE", "Marienplatz 1, Munich DE-BY"),
    (_location("Berlin", "Berlin", "Germany"), "DE", "Unter den Linden 1, Berlin DE-BE"),
    (MUMBAI, "IN", "Shop 5, Andheri IN-MH"),
    (_location("Bogota", "Bogota", "Colombia"), "CO", "Calle 1, Bogota CO-DC"),
    (_location("Barcelona", "Catalonia", "Spain"), "ES", "Carrer Gran 1, Barcelona ES-CT"),
    (_location("Milan", "Lombardy", "Italy"), "IT", "Via Roma 1, Milano IT-MI"),
    (_location("Chennai", "Tamil Nadu", "India"), "IN", "1 Anna Salai, Chennai IN-TN"),
    (LAGOS, "NG-LA", "NG-LA"),
    (LAGOS, "NG-LA", "Plot 1, Lekki NG-LA"),
    (MUMBAI, "IN", "Shop 5, Andheri IN-MH 400053"),
    (MUNICH, "DE", "Marienplatz 1, Munich DE-BY 80331"),
    # With no comma before it, or bare and written with a space, as before.
    (LAGOS, "NG-LA", "Lekki NG-LA"),
    (LAGOS, "NG-LA", "NG LA"),
    (MUNICH, "DE", "Marienplatz 1, 80331 Munich, DE BY"),
    # With a typographic dash, or another separator that folding reads as a space (lane r8).
    (MUNICH, "DE", "Marienplatz 1, Munich DE\u2013BY"),
    (MUNICH, "DE", "Marienplatz 1, Munich DE \u2013 BY"),
    (LAGOS, "NG-LA", "Plot 1, Lekki NG\u2013LA"),
    (LAGOS, "NG-LA", "NG\u2013LA"),
    (MUNICH, "DE", "Marienplatz 1, 80331 Munich, DE_BY"),
]


@pytest.mark.parametrize(
    ("location", "code", "address"),
    UNLISTED_CODE_FORMS,
    ids=[a for _, _, a in UNLISTED_CODE_FORMS],
)
def test_an_unlisted_code_form_after_a_town_does_not_refuse_its_own_location(
    location, code, address
):
    resolution = _resolve(location, address)
    assert resolution.code == code, resolution.evidence
    # It states nothing here, as on main; lane 5DW2FIX (816fc941) reads a listed jurisdiction form
    # such as "NG-LA" and adds an "agrees" line. Neither refuses nor asks for clarification.
    assert resolution.reason is None


@pytest.mark.parametrize(
    ("address", "conflict"),
    [
        ("1 King St, Toronto CA-ON", "country CA"),
        ("1 Martin Pl, Sydney AU-NSW", "country AU"),
        ("1500 Broadway, Hartford US-CT", "subdivision US-CT"),
    ],
)
def test_a_listed_code_form_after_a_town_is_still_explicit(address, conflict):
    resolution = _resolve(DALLAS, address)
    assert resolution.code == UNRESOLVED
    assert resolution.reason is None
    assert resolution.evidence[-1].endswith(f"{address!r} states {conflict}")


#: (C-10) A line break or a semicolon after an ambiguous first line separates the parts as a
#: comma does, so FU-R1 holds whatever the separator.
SEPARATED = [
    (HARTFORD, "123 MAIN ST\nDALLAS", "country ST"),
    (LONDON, "123 MAIN ST\nDALLAS", "country ST"),
    (HARTFORD, "123 MAIN ST\r\nDALLAS 75201", "country ST"),
    (HARTFORD, "123 MAIN ST; DALLAS", "country ST"),
    (DALLAS, "1500 Broadway Hartford CT\nDallas", "subdivision US-CT"),
    # A slash separates parts too (lane r7).
    (HARTFORD, "123 MAIN ST / DALLAS", "country ST"),
    (HARTFORD, "123 MAIN ST/DALLAS", "country ST"),
]


@pytest.mark.parametrize(
    ("location", "address", "conflict"),
    SEPARATED,
    ids=[f"{loc.city}:{a!r}" for loc, a, _ in SEPARATED],
)
def test_an_ambiguous_first_line_before_a_line_break_asks_for_clarification(
    location, address, conflict
):
    resolution = _resolve(location, address)
    assert resolution.code == UNRESOLVED
    assert resolution.reason == ADDRESS_SUFFIX_AMBIGUOUS
    assert resolution.evidence[-1] == (
        f"unresolved: coverage rule 0 business address {address!r} is ambiguous: "
        f"a street type or {conflict}"
    )


@pytest.mark.parametrize(
    ("location", "code", "address"),
    [
        (DALLAS, "US-TX", "123 MAIN ST\nDALLAS, TX"),
        (SAO_TOME, "ST", "123 MAIN ST\nSAO TOME"),
        (HARTFORD, "US-CT", "1500 Broadway Hartford CT\nHartford"),
        (DALLAS, "US-TX", "123 Main St\nDallas"),
        (DALLAS, "US-TX", "123 Main St\nDallas, TX 75201"),
        (DALLAS, "US-TX", "123 MAIN ST / DALLAS, TX"),
        # A unit written "5/12" (unit 5, number 12) reads as before.
        (SYDNEY, "AU-NSW", "5/12 Main St"),
        (DALLAS, "US-TX", "Unit 5 / 12 Main St"),
    ],
)
def test_a_line_break_settles_or_agrees_as_a_comma_does(location, code, address):
    resolution = _resolve(location, address)
    assert resolution.code == code
    assert resolution.reason is None


# --------------------------------------------------------------------------- #
# An unlisted code form written with a space (W21-REV-D1STREET r2 re-bind C-12): the reviewer's
# proposed tests (r2b/proposed/test_rev_r2b_c12_proposed.py), with exact evidence, plus the lane's
# r8 rows. "XX YY" is the country XX of a code form, or the code YY after a word; both readings
# form a group, so the business's own address agrees and every-reading contradictions are refused.
# --------------------------------------------------------------------------- #

BOGOTA = _location("Bogota", "Bogota", "Colombia")
MILAN = _location("Milan", "Lombardy", "Italy")
HOUSTON = _location("Houston", "TX", "US")
ALULA = _location("AlUla", "Medina", "Saudi Arabia")
KRAKOW = _location("Krakow", "Malopolskie", "Poland")

SPACED_OWN = [
    (MUNICH, "DE", "Marienplatz 1, Munich DE BY"),
    (MUNICH, "DE", "Marienplatz 1, Munich DE BY 80331"),
    (MUNICH, "DE", "Marienplatz 1, Munich De By"),
    (MUMBAI, "IN", "Shop 5, Andheri IN MH"),
    (BOGOTA, "CO", "Calle 1, Bogota CO DC"),
    (MILAN, "IT", "Via Roma 1, Milano IT MI"),
    (LAGOS, "NG-LA", "Ikeja NG LA"),
    (LAGOS, "NG-LA", "1 Main Rd, Ikeja NG LA"),
    (LAGOS, "NG-LA", "Plot 1, Lekki NG LA"),
    (LAGOS, "NG-LA", "NG LA"),
    (HOUSTON, "US-TX", "1500 BROADWAY, HARRIS CO TX"),
    (ATLANTA, "US-GA", "1500 BROADWAY, FULTON CO GA"),
    (ALULA, "SA", "Old Town, AL ULA"),
    # The lane's r8 rows: dots dropped as folding drops them, and a bare form whose country code
    # is also a street-type abbreviation ("PL" alone with a code is no street).
    (LAGOS, "NG-LA", "N.G. LA"),
    (KRAKOW, "PL", "PL MA"),
]


@pytest.mark.parametrize(
    ("location", "code", "address"), SPACED_OWN, ids=[a for _, _, a in SPACED_OWN]
)
def test_a_spaced_code_form_does_not_refuse_its_own_location(location, code, address):
    resolution = _resolve(location, address)
    assert resolution.code == code, resolution.evidence
    assert resolution.reason is None


SPACED_CONTRADICTING = [
    (DALLAS, "Marienplatz 1, Munich DE BY", "country DE or country BY"),
    (DALLAS, "Ikeja NG LA", "country NG or country LA or subdivision US-LA"),
    (HARTFORD, "1500 Broadway, Harris Co TX", "country CO or subdivision US-TX"),
    (HARTFORD, "1500 BROADWAY, HARRIS CO TX", "country CO or subdivision US-TX"),
    (LONDON, "1500 BROADWAY, FULTON CO GA", "country CO or country GA or country US"),
]


@pytest.mark.parametrize(
    ("location", "address", "conflict"),
    SPACED_CONTRADICTING,
    ids=[f"{loc.city}:{a}" for loc, a, _ in SPACED_CONTRADICTING],
)
def test_a_spaced_code_form_that_contradicts_in_every_reading_is_refused(
    location, address, conflict
):
    resolution = _resolve(location, address)
    assert resolution.code == UNRESOLVED
    assert resolution.reason is None
    assert resolution.evidence[-1] == (
        f"unresolved: coverage rule 0 business address {address!r} states {conflict}"
    )


@pytest.mark.parametrize(
    ("location", "address"),
    [(DALLAS, "NG LA"), (LONDON, "NG LA"), (DALLAS, "N.G. LA")],
    ids=["Dallas:NG LA", "London:NG LA", "Dallas:N.G. LA"],
)
def test_a_bare_spaced_code_form_that_contradicts_is_refused(location, address):
    """Asserted by outcome only: here the group's readings all contradict ("states country NG or
    country LA or ..."); lane 5DW2FIX (816fc941) reads a bare "NG LA" as the jurisdiction form NG-LA
    and refuses it as "states country NG"."""
    resolution = _resolve(location, address)
    assert resolution.code == UNRESOLVED
    assert resolution.reason is None


@pytest.mark.parametrize("address", ["1500 Broadway, US CA", "1 Main St, Los Angeles, US CA"])
def test_a_listed_code_written_with_a_space_stays_explicit(address):
    resolution = _resolve(TORONTO, address)
    assert resolution.code == UNRESOLVED
    assert resolution.evidence[-1].endswith("states country US"), resolution.evidence


# --------------------------------------------------------------------------- #
# An ordinal street name and a street type before a post-directional (W21-REV-D1STREET r2 re-bind 2
# C-13 and C-14): the reviewer's proposed tests (r2c/proposed/test_rev_r2c_c13_c14_proposed.py),
# with exact evidence, plus the lane's r9 rows (broad probe R17). "4th" is a street name, so "123
# 4th St" is a clear street line. "St NE" alone, and "5th St NE" without a house number (R17), may
# be a street type and a direction: the code readings form an ambiguous group, settled by explicit
# geography and never refused as explicit text (the two "ST NE" rows moved here from
# SPACED_CONTRADICTING).
# --------------------------------------------------------------------------- #

WASHINGTON = _location("Washington", "DC", "US")
NEW_YORK = _location("New York", "NY", "US")

ORDINAL_OR_DIRECTION_OWN = [
    (DALLAS, "US-TX", "123 4th St"),
    (DALLAS, "US-TX", "100 1st Pl"),
    (DALLAS, "US-TX", "22 3rd Ct"),
    (DALLAS, "US-TX", "500 2nd St"),
    (DALLAS, "US-TX", "Suite 5, 123 4th St"),
    (DALLAS, "US-TX", "123 4th St NE"),
    (NEW_YORK, "US-NY", "1 W 34th St"),
    (WASHINGTON, "US-DC", "123 4th St NE"),
    (WASHINGTON, "US-DC", "1 1st St SE"),
    (WASHINGTON, "US-DC", "Suite 200, 123 4th St NE, Washington, DC 20002"),
    (WASHINGTON, "US-DC", "Suite 200, 123 4th St NE"),
    (WASHINGTON, "US-DC", "Suite 5, 1 1st St SE, Washington, DC 20003"),
    (WASHINGTON, "US-DC", "1 Main St, St NE, Washington, DC"),
    (OMAHA, "US-NE", "ST NE"),
    (SAO_TOME, "ST", "ST NE"),
    # The lane's R17 rows: a street name before "St NE" with no house number.
    (WASHINGTON, "US-DC", "5th St NE, Washington, DC"),
    (WASHINGTON, "US-DC", "5th St NE, Washington, DC 20002"),
    (OMAHA, "US-NE", "5th St NE"),
    (NIAMEY, "NE", "5th St NE"),
]


@pytest.mark.parametrize(
    ("location", "code", "address"),
    ORDINAL_OR_DIRECTION_OWN,
    ids=[f"{loc.city}:{a}" for loc, _, a in ORDINAL_OR_DIRECTION_OWN],
)
def test_an_ordinal_street_or_a_street_and_direction_resolves_its_own_location(
    location, code, address
):
    resolution = _resolve(location, address)
    assert resolution.code == code, resolution.evidence
    assert resolution.reason is None


ORDINAL_OR_DIRECTION_AMBIGUOUS = [
    (DALLAS, "123 4TH ST", "country ST"),
    (WASHINGTON, "123 4TH ST NE", "country NE or subdivision US-NE"),
    (DALLAS, "ST NE", "country ST or country NE or subdivision US-NE"),
    (DALLAS, "1 Main Rd, ST NE", "country ST or country NE or subdivision US-NE"),
    (DALLAS, "1 Main St, St NE", "country ST or country NE or subdivision US-NE"),
    (WASHINGTON, "1 Main St, St NE", "country ST or country NE or subdivision US-NE"),
    # The lane's R17 rows: main accepts "5th St NE" for Washington, DC, and C-8 refused it as
    # explicit text; it asks for clarification as "St NE" alone does.
    (WASHINGTON, "5th St NE", "country NE or subdivision US-NE"),
    (WASHINGTON, "5TH ST NE", "country NE or subdivision US-NE"),
    (WASHINGTON, "5th st ne", "country NE or subdivision US-NE"),
    (WASHINGTON, "Massachusetts Ave NE", "country NE or subdivision US-NE"),
    (WASHINGTON, "5th St NE 20002", "country NE or subdivision US-NE"),
    (WASHINGTON, "Suite 5, 5th St NE", "country NE or subdivision US-NE"),
    (WASHINGTON, "5th St NE, Suite 5", "country NE or subdivision US-NE"),
    (DALLAS, "5th St NE", "country NE or subdivision US-NE"),
    (LONDON, "5th St NE", "country NE or country US"),
]


@pytest.mark.parametrize(
    ("location", "address", "conflict"),
    ORDINAL_OR_DIRECTION_AMBIGUOUS,
    ids=[f"{loc.city}:{a}" for loc, a, _ in ORDINAL_OR_DIRECTION_AMBIGUOUS],
)
def test_an_ambiguous_ordinal_line_or_street_and_direction_asks_for_clarification(
    location, address, conflict
):
    resolution = _resolve(location, address)
    assert resolution.code == UNRESOLVED
    assert resolution.reason == ADDRESS_SUFFIX_AMBIGUOUS
    assert resolution.evidence[-1] == (
        f"unresolved: coverage rule 0 business address {address!r} is ambiguous: "
        f"a street type or {conflict}"
    )


@pytest.mark.parametrize(
    ("location", "address", "conflict"),
    [
        (DALLAS, "123 4th St Hartford CT", "subdivision US-CT"),
        (DALLAS, "12 14 PL", "country PL"),
        (DALLAS, "PL MA", "country PL or country MA or subdivision US-MA"),
        # R17: explicit geography after "5th St NE" is still read.
        (LONDON, "5th St NE, Washington, DC", "country US"),
        (WASHINGTON, "5th St NE, Dallas, TX", "subdivision US-TX"),
        (DALLAS, "5th St NE, Omaha, NE", "subdivision US-NE"),
    ],
    ids=lambda value: value if isinstance(value, str) else value.city,
)
def test_explicit_text_after_an_ordinal_street_is_still_refused(location, address, conflict):
    resolution = _resolve(location, address)
    assert resolution.code == UNRESOLVED
    assert resolution.reason is None
    assert resolution.evidence[-1] == (
        f"unresolved: coverage rule 0 business address {address!r} states {conflict}"
    )


# --------------------------------------------------------------------------- #
# A single-area direction and a designator in a part (W21-REV-D1STREET r2 re-bind 3 C-15 and
# C-16): the reviewer's proposed tests (r2d/proposed/test_rev_r2d_c15_c16_proposed.py), with
# exact evidence. "5th St SE" may be "5th Street Southeast", so its single-area code reading
# (Sweden) is ambiguous, as "5th St NE" is. In a part after the first, a unit number ("Flat No
# 3") states nothing, and a route, or a state and its ZIP code after a unit ("12000 FM 1960",
# "Unit 5 NY 10001", "123 Main St Fl 3"), is ambiguous: explicit geography settles it, and it is
# never read as nothing. The first part keeps its reading ("1 SR 520, Redmond, WA 98052").
# --------------------------------------------------------------------------- #

SEATTLE = _location("Seattle", "WA", "US")
MIAMI = _location("Miami", "FL", "US")
STOCKHOLM = _location("Stockholm", "Stockholm", "Sweden")
MAHARASHTRA_MUMBAI = _location("Mumbai", "Maharashtra", "India")

DIRECTION_OR_DESIGNATOR_OWN = [
    # C-15
    (WASHINGTON, "US-DC", "Suite 5, 5th St SE, Washington, DC 20003"),
    (WASHINGTON, "US-DC", "Suite 5, Massachusetts Ave SE, Washington, DC"),
    (WASHINGTON, "US-DC", "5th St SE, Washington, DC"),
    (STOCKHOLM, "SE", "5th St SE"),
    (STOCKHOLM, "SE", "Suite 5, 5th St SE"),
    # C-16
    (HOUSTON, "US-TX", "ACME Inc, 12000 FM 1960, Houston, TX 77070"),
    (SEATTLE, "US-WA", "ACME, 1 SR 520, Seattle, WA 98052"),
    (DALLAS, "US-TX", "ACME, 100 CR 45, Dallas, TX"),
    (DALLAS, "US-TX", "ACME, 123 Main St Fl 3, Dallas, TX"),
    (MIAMI, "US-FL", "ACME, 123 Main St Fl 3"),
    (MAHARASHTRA_MUMBAI, "IN", "Sea View Apts, Flat No 3, Bandra, Mumbai"),
    (MAHARASHTRA_MUMBAI, "IN", "ACME Ltd, Shop No 5, MG Road, Mumbai"),
    (MAHARASHTRA_MUMBAI, "IN", "Unit 4, Plot No 12, MIDC, Andheri East, Mumbai 400093"),
    (LAGOS, "NG-LA", "ACME Ltd, Plot No 12, Admiralty Way, Lekki"),
    (LAGOS, "NG-LA", "Block A, Flat No 3, Victoria Island, Lagos"),
    # C-16 is for later parts: a designator in the first part keeps its reading, as on main.
    (SEATTLE, "US-WA", "1 SR 520, Redmond, WA 98052"),
    (SEATTLE, "US-WA", "1 SR 520, Redmond"),
    (HOUSTON, "US-TX", "12000 FM 1960, Houston"),
    (DALLAS, "US-TX", "123 Main St Fl 3, Dallas"),
    (DALLAS, "US-TX", "100 CR 45, Dallas"),
]


@pytest.mark.parametrize(
    ("location", "code", "address"),
    DIRECTION_OR_DESIGNATOR_OWN,
    ids=[f"{loc.city}:{a}" for loc, _, a in DIRECTION_OR_DESIGNATOR_OWN],
)
def test_a_direction_or_designator_in_a_later_part_resolves_its_own_location(
    location, code, address
):
    resolution = _resolve(location, address)
    assert resolution.code == code, resolution.evidence
    assert resolution.reason is None


DIRECTION_OR_DESIGNATOR_AMBIGUOUS = [
    # C-15
    (WASHINGTON, "5th St SE", "country SE"),
    (WASHINGTON, "5TH ST SE", "country SE"),
    (WASHINGTON, "5th St SE 20003", "country SE"),
    (WASHINGTON, "Suite 5, 5th St SE", "country SE"),
    (WASHINGTON, "5th St SE, Suite 5", "country SE"),
    (WASHINGTON, "ACME, 5th St SE", "country SE"),
    (DALLAS, "Gate 2 1 Main St SE", "country SE"),
    (DALLAS, "Suite 5, 5th St SE, Stockholm", "country SE"),
    # C-16: a route, or a state and its postcode after a unit, with nothing explicit to settle it
    (HOUSTON, "Suite 5, 12000 FM 1960", "country FM"),
    (SEATTLE, "Suite 5, 1 SR 520", "country SR"),
    (DALLAS, "Suite 5, 100 CR 45", "country CR"),
    (DALLAS, "1 Main St, Unit 5 NY 10001", "subdivision US-NY"),
    (DALLAS, "ACME, Apt 5 CT 06103", "subdivision US-CT"),
    (DALLAS, "ACME, 123 Main St FL 32801", "subdivision US-FL"),
    (DALLAS, "ACME, Suite 5 CA 90265", "country CA or subdivision US-CA"),
    (TORONTO, "ACME, Suite 5 TX 75201", "country US"),
]


@pytest.mark.parametrize(
    ("location", "address", "reading"),
    DIRECTION_OR_DESIGNATOR_AMBIGUOUS,
    ids=[f"{loc.city}:{a}" for loc, a, _ in DIRECTION_OR_DESIGNATOR_AMBIGUOUS],
)
def test_an_ambiguous_direction_or_designator_asks_for_clarification(location, address, reading):
    resolution = _resolve(location, address)
    assert resolution.code == UNRESOLVED
    assert resolution.reason == ADDRESS_SUFFIX_AMBIGUOUS
    assert resolution.evidence[-1] == (
        f"unresolved: coverage rule 0 business address {address!r} is ambiguous: "
        f"a street type or {reading}"
    )


DIRECTION_OR_DESIGNATOR_REFUSED = [
    (DALLAS, "1 Main St, Orlando FL 32801", "subdivision US-FL"),
    (DALLAS, "1 Main St, Miami FL 33101", "subdivision US-FL"),
    (DALLAS, "ACME, Floor 3 US-CT 06103", "subdivision US-CT"),
    (DALLAS, "ACME, Flat No 3, Oslo, Norway", "country NO"),
    (DALLAS, "ACME, Plot No 12, Lagos NG 100001", "country NG"),
    (DALLAS, "ACME, 1 SR 520, Paramaribo, SR", "country SR"),
    (DALLAS, "Level 3, 1 Martin Pl, Sydney NSW 2000", "country AU"),
    (DALLAS, "1 Main St, Sector 5 SE", "country SE"),
    (HOUSTON, "Suite 5, 12000 FM 1960, Pohnpei, Micronesia", "country FM"),
    (WASHINGTON, "5th St SE, Stockholm, Sweden", "country SE"),
    (WASHINGTON, "Drottninggatan 5, Stockholm SE", "country SE"),
]


@pytest.mark.parametrize(
    ("location", "address", "conflict"),
    DIRECTION_OR_DESIGNATOR_REFUSED,
    ids=[f"{loc.city}:{a}" for loc, a, _ in DIRECTION_OR_DESIGNATOR_REFUSED],
)
def test_explicit_geography_beside_a_direction_or_designator_is_still_refused(
    location, address, conflict
):
    resolution = _resolve(location, address)
    assert resolution.code == UNRESOLVED
    assert resolution.reason is None
    assert resolution.evidence[-1] == (
        f"unresolved: coverage rule 0 business address {address!r} states {conflict}"
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
    """None of these is a street line or has a postcode alone in a later part, so the whole
    address reads exactly as ``parse_observed`` reads it."""
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

#: The realistic row that asks for clarification since FU-R1: an upper-case street name whose "ST"
#: may be Sao Tome, with no country or state after it to rule that reading out.
REALISTIC_CLARIFY = {"KHALIFA ST, AL-AIN"}


def test_the_realistic_corpus_has_the_5dw2fix_size():
    assert len(REALISTIC) == 924
    assert len(set(REALISTIC)) == len(REALISTIC)


def test_the_5dw2fix_realistic_addresses_resolve_to_their_location():
    """Every row's address belongs to its location, so each resolves to the location's own
    code, except the one row of :data:`REALISTIC_CLARIFY`, which asks for clarification (FU-R1);
    and none is a street line or has a postcode alone in a later part, so the whole address reads
    exactly as an observed market does (a later part such as "Sydney NSW" is also read on its own,
    and agrees)."""
    wrong = []
    for (city, state, country), address in REALISTIC:
        location = _location(city, state, country)
        own = resolve_jurisdiction(location).code
        resolution = _resolve(location, address)
        expected = (
            (UNRESOLVED, ADDRESS_SUFFIX_AMBIGUOUS) if address in REALISTIC_CLARIFY else (own, None)
        )
        if own == UNRESOLVED or (resolution.code, resolution.reason) != expected:
            wrong.append((location, address, own, resolution.code, resolution.reason))
        if parse_business_address(address) != parse_observed(address):
            wrong.append((location, address, "reads differently"))
    assert wrong == []
    assert [address for _, address in REALISTIC if address in REALISTIC_CLARIFY] == [
        "KHALIFA ST, AL-AIN"
    ]


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
