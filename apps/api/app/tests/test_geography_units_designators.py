"""GEO-P2: units, floors and designators in a business address.

Geography register (2026-11-12) items G-6 (D1's FU-B) and G-8 (D1's FU-H), under the operator's
instruction of 2026-11-12 ("Refuse contradictions. Return UNRESOLVED with actionable clarification
where the evidence is insufficient or ambiguous") and the C-3 ruling of 2026-11-09.

* G-6: a floor after the street ("123 Main St, Fl 3", "123 Main St Fl 3") is a floor, never
  Florida; a trailing unit does not hide a contradicting country or state before it ("1 Main St,
  Toronto, Canada, Suite 5" for Dallas is refused: GEO-P1's part places read it).
* G-8: a route designator written as the whole address ("1 SR 520", "12000 FM 1960") and a street
  with no house number written as a street ("Main St", "NE 45th St") may be a road or a street, so
  they ask for clarification instead of being refused for the business's own location; a code
  inside a route's name ("4400 US Hwy 75"), or a designator whose code is "US" ("1000 US-75"),
  names its country, so it is refused for a location elsewhere; a two-area state after an
  unlisted town keeps D1's clarification (C-8, C-16), and
  writing the state in full resolves it; a last part that is only a direction after a street line
  ("1200 Pennsylvania Ave, SE") is a direction part, not Sweden (review W21-REV-GEO r1 I-3).

These tests pin the measured classes; they do not show that arbitrary addresses are validated.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.geography.markets import read_business_address
from app.jurisdiction.codes import UNRESOLVED
from app.jurisdiction.resolver import (
    ADDRESS_DIRECTION_AMBIGUOUS,
    ADDRESS_PLACE_UNCLEAR,
    ADDRESS_ROUTE_AMBIGUOUS,
    ADDRESS_SUFFIX_AMBIGUOUS,
    CLARIFICATIONS,
    resolve_jurisdiction,
)


def _location(city, state, country) -> SimpleNamespace:
    return SimpleNamespace(city=city, state_province=state, country=country)


def _resolve(location, address):
    return resolve_jurisdiction(location, [SimpleNamespace(business_address=address)])


DALLAS = _location("Dallas", "Texas", "United States")
HOUSTON = _location("Houston", "Texas", "United States")
SEATTLE = _location("Seattle", "Washington", "United States")
OKLAHOMA_CITY = _location("Oklahoma City", "Oklahoma", "United States")
ORLANDO = _location("Orlando", "Florida", "United States")
HARTFORD = _location("Hartford", "Connecticut", "United States")
TORONTO = _location("Toronto", "Ontario", "Canada")
LONDON = _location("London", None, "United Kingdom")
PARAMARIBO = _location("Paramaribo", None, "Suriname")
SAO_TOME = _location("Sao Tome", "Agua Grande", "Sao Tome and Principe")


def _refused(location, address, evidence):
    result = _resolve(location, address)
    assert result.code == UNRESOLVED, (address, result)
    assert result.reason is None, (address, result)
    assert result.evidence[-1].endswith(evidence), (address, result.evidence)


def _clarified(location, address, reason, evidence):
    result = _resolve(location, address)
    assert result.code == UNRESOLVED, (address, result)
    assert result.reason == reason, (address, result)
    assert result.evidence[-1].endswith(evidence), (address, result.evidence)


# --------------------------------------------------------------------------- #
# G-6 (FU-B): a floor is a floor; a trailing unit hides nothing
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "address",
    [
        "123 Main St, Fl 3",
        "123 Main St Fl 3",
        "123 Main St, Floor 3",
        "123 Main St Flr 2",
        "123 Main St, Fl. 12",
        "123 MAIN ST, DALLAS, TX, FL 3",
        "ACME Corp, Suite 200, 123 Main St, Fl 3",
        "ACME Corp; Suite 200; 123 Main St; Fl 3",
        "ACME Corp\nSuite 200\n123 Main St\nFl 3",
        "ACME Corp, Suite 200, 123 Main St Fl 3",
        "ACME Corp, Suite 200, 123 Main St, Fl 3, USA",
    ],
)
def test_g6_a_floor_after_the_street_is_a_floor(address):
    """D1 refused "123 Main St, Fl 3" for Dallas as Florida and asked about "123 Main St Fl 3" in a
    later part (C-16); a floor of one to three digits after the street is a floor."""
    assert _resolve(DALLAS, address).code == "US-TX", address


@pytest.mark.parametrize(
    ("address", "evidence"),
    [
        ("1 Main St, FL 32801", "states subdivision US-FL"),
        ("1 Main St, Orlando FL 32801", "states subdivision US-FL"),
        ("123 Main St, Fl 3, Orlando, FL", "states subdivision US-FL"),
    ],
)
def test_g6_a_state_and_its_zip_code_is_not_a_floor(address, evidence):
    """A five-digit number is a ZIP code, so "FL 32801" is Florida; and a floor beside an explicit
    state leaves the state read."""
    _refused(DALLAS, address, evidence)
    assert _resolve(ORLANDO, address).code == "US-FL"


def test_g6_a_road_number_is_not_a_floor():
    """ "FL-12" (a hyphen) is a Florida road, as D1's unit rule reads it."""
    assert read_business_address("1 Main St, FL-12").stated.country == "US"
    assert _resolve(ORLANDO, "1 Main St, FL-12").code == "US-FL"


@pytest.mark.parametrize(
    ("address", "evidence"),
    [
        ("1 Main St, Toronto, Canada, Suite 5", "states country CA"),
        ("1500 Broadway, Hartford, CT, Suite 5", "states subdivision US-CT"),
        ("123 Main St, Toronto, Ontario, Unit 5", "states country CA"),
        ("1 Main St, Lagos, Nigeria, Floor 3", "states country NG"),
    ],
)
def test_g6_a_trailing_unit_hides_no_contradiction(address, evidence):
    _refused(DALLAS, address, evidence)


def test_g6_the_same_addresses_resolve_for_their_own_location():
    assert _resolve(TORONTO, "1 Main St, Toronto, Canada, Suite 5").code == "CA-ON"
    assert _resolve(HARTFORD, "1500 Broadway, Hartford, CT, Suite 5").code == "US-CT"


# --------------------------------------------------------------------------- #
# G-8 (FU-H): designators and numberless streets as the whole address
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("location", "address", "reading"),
    [
        (SEATTLE, "1 SR 520", "country SR"),
        (SEATTLE, "1 sr 520", "country SR"),
        (HOUSTON, "12000 FM 1960", "country FM"),
        (DALLAS, "100 CR 45", "country CR"),
        (HOUSTON, "12000 FM 1960, Suite 5", "country FM"),
    ],
)
def test_g8_a_route_designator_as_the_whole_address_asks(location, address, reading):
    """D1 refused these for the business's own location ("1 SR 520" for Seattle: SR is Suriname); a
    route may be a road, as C-16 reads one in a later part, so the address asks."""
    _clarified(location, address, ADDRESS_ROUTE_AMBIGUOUS, f"is ambiguous: a route or {reading}")


def test_g8_a_route_designator_agrees_where_its_code_is_the_location():
    assert _resolve(PARAMARIBO, "1 SR 520").code == "SR"


@pytest.mark.parametrize("address", ["1000 US-75", "1000 us-75", "1000 US 75", "1000 us 75"])
def test_g8_a_whole_address_us_route_names_its_country(address):
    """A designator whose code is "US" is a United States route, and "US" is the United States:
    both readings name one country, so it is read as "4400 US Hwy 75" is, refused for London and
    resolving for Dallas. "1 SR 520" still asks (a road, or Suriname)."""
    _refused(LONDON, address, "states country US")
    assert _resolve(DALLAS, address).code == "US-TX"
    _clarified(LONDON, "1 SR 520", ADDRESS_ROUTE_AMBIGUOUS, "is ambiguous: a route or country SR")


@pytest.mark.parametrize(
    "address", ["1 SR 520, Redmond, Washington", "1 State Route 520", "1 SR 520, Seattle, WA 98101"]
)
def test_g8_the_route_clarification_remedies_resolve(address):
    assert _resolve(SEATTLE, address).code == "US-WA", address


@pytest.mark.parametrize(
    ("location", "address", "reading"),
    [
        (DALLAS, "Main St", "country ST"),
        (LONDON, "High St", "country ST"),
        (SEATTLE, "NE 45th St", "country ST"),
        (OKLAHOMA_CITY, "NE 23rd St", "country ST"),
        (DALLAS, "Hartford Ct", "subdivision US-CT"),
    ],
)
def test_g8_a_street_without_a_house_number_asks(location, address, reading):
    """A street name and a street type written as a street ("St", not "ST") with no house number
    may also be a town and a code; D1 refused the business's own "Main St". It asks, with D1's
    reason."""
    _clarified(location, address, ADDRESS_SUFFIX_AMBIGUOUS, f"a street type or {reading}")


def test_g8_a_town_and_a_code_written_as_one_stays_explicit():
    """ "Hartford CT" (the code in capitals) stays a town and a state, as on main."""
    _refused(DALLAS, "Hartford CT", "states subdivision US-CT")
    assert _resolve(SAO_TOME, "Main St").code == "ST"
    assert _resolve(DALLAS, "Main Street").code == "US-TX"
    assert _resolve(DALLAS, "Main St, Dallas, TX").code == "US-TX"


@pytest.mark.parametrize(
    ("address", "reading"),
    [("MAIN ST", "country ST"), ("main st", "country ST"), ("HARTFORD CT", "subdivision US-CT")],
)
def test_g8_in_one_case_a_street_without_a_house_number_asks(address, reading):
    """In an address written in one case, case cannot tell a street type from a code ("MAIN ST",
    "HARTFORD CT"), as D1 reads "123 MAIN ST": it asks, never accepted. A listed code ("US-CT")
    stays explicit."""
    _clarified(DALLAS, address, ADDRESS_SUFFIX_AMBIGUOUS, f"a street type or {reading}")
    _refused(DALLAS, "US-CT", "states subdivision US-CT")
    _refused(DALLAS, "US CT", "states subdivision US-CT")


def test_g6_a_floor_part_is_a_unit_so_the_first_part_reads_alone():
    """A floor after a whole-address form is a unit (D1's C-6(b)), so the form keeps its own
    reading: "Main St, Fl 3" asks as "Main St" does, and "1 SR 520, Fl 3" as "1 SR 520" does."""
    _clarified(DALLAS, "Main St, Fl 3", ADDRESS_SUFFIX_AMBIGUOUS, "a street type or country ST")
    _clarified(SEATTLE, "1 SR 520, Fl 3", ADDRESS_ROUTE_AMBIGUOUS, "a route or country SR")


def test_g6_a_state_and_its_zip_code_before_a_trailing_unit_is_read():
    """ "FL 33101" is Florida and its ZIP code, not a floor: it is read before a trailing unit."""
    _refused(HARTFORD, "1 Main St, Miami, FL 33101, Suite 200", "states subdivision US-FL")
    assert _resolve(
        _location("Miami", "Florida", "US"), "1 Main St, Miami, FL 33101, Suite 200"
    ).code == ("US-FL")


def test_g8_a_two_area_state_after_an_unlisted_town_asks_and_resolves_in_full():
    """D1's C-8 and C-16: "WA" (Washington or Western Australia) cannot settle the route's reading,
    so the address asks, with D1's reason and evidence; written in full, the state settles it."""
    address = "ACME, 1 SR 520, Redmond, WA 98052"
    _clarified(SEATTLE, address, ADDRESS_SUFFIX_AMBIGUOUS, "a street type or country SR")
    assert _resolve(SEATTLE, "ACME, 1 SR 520, Redmond, Washington 98052").code == "US-WA"


@pytest.mark.parametrize(
    "address",
    [
        "ACME Corp, Suite 200, 4400 US Hwy 75",
        "ACME Corp, Suite 200, 4400 U.S. Hwy 75",
        "ACME Corp; 4400 US Route 75",
        "4400 US Highway 75, Suite 5",
    ],
)
def test_g8_a_country_code_inside_a_route_name_is_read(address):
    """ "US Hwy 75" is a road of the United States: refused for London, resolves for Dallas."""
    _refused(LONDON, address, "states country US")
    assert _resolve(DALLAS, address).code == "US-TX"


def test_g8_a_route_name_follows_case_and_skips_street_types():
    """In an address written in one case the code may be part of a name, so it asks; "St Rt 9" is a
    State Route, never Sao Tome."""
    _clarified(LONDON, "ACME CORP, 4400 US HWY 75", ADDRESS_PLACE_UNCLEAR, "country US")
    assert _resolve(LONDON, "ACME Ltd, 1 St Rt 9").code == "GB"
    assert _resolve(LONDON, "ACME Ltd, 1 ST RT 9").code == "GB"


# --------------------------------------------------------------------------- #
# The clarification
# --------------------------------------------------------------------------- #


def test_the_route_clarification_is_fixed_text():
    assert CLARIFICATIONS[ADDRESS_ROUTE_AMBIGUOUS] == (
        "We couldn't tell whether a route or unit number in the business address (such as "
        "'SR 520', 'FM 1960' or 'Unit 5 NY 10001') is part of the street or a state or "
        "country. After it, write the town and the state or province in full, separated by "
        "commas (for example '1 SR 520, Redmond, Washington'), or write the road's name in "
        "full (for example '1 State Route 520')."
    )
    assert "SR 520" not in CLARIFICATIONS[ADDRESS_SUFFIX_AMBIGUOUS]


def test_only_a_whole_address_designator_takes_the_route_reason():
    """A designator in a later part keeps D1's reason and evidence (C-16)."""
    result = _resolve(HOUSTON, "Suite 5, 12000 FM 1960")
    assert result.reason == ADDRESS_SUFFIX_AMBIGUOUS
    assert read_business_address("Suite 5, 12000 FM 1960").routes == ()
    assert [r.country for r in read_business_address("12000 FM 1960").routes] == ["FM"]


# --------------------------------------------------------------------------- #
# G-8, review W21-REV-GEO r1 I-3: a trailing direction after a street line
# --------------------------------------------------------------------------- #

WASHINGTON_DC = _location("Washington", "District of Columbia", "United States")


@pytest.mark.parametrize(
    "address",
    [
        "1200 Pennsylvania Ave, SE",
        "1200 Pennsylvania Ave; SE",
        "1200 Pennsylvania Ave\nSE",
        "1200 Pennsylvania Ave, SE, 20003",
        "1200 Pennsylvania Ave, Suite 5, SE",
        "1200 PENNSYLVANIA AVE, SE",
    ],
)
def test_i3_a_trailing_direction_after_a_street_line_is_a_direction(address):
    """I-3: the qualifier rules read a last part "SE" as Sweden, so "1200 Pennsylvania Ave, SE" was
    refused for a Washington, DC location ("states country SE"), on main and at GEO-P1. After a
    street line it may be the street's direction, as "Ave SE" is (C-15): it is read as GEO-P1 reads
    a part that is only a direction. Explicit geography settles it; otherwise it asks."""
    _clarified(WASHINGTON_DC, address, ADDRESS_DIRECTION_AMBIGUOUS, "a direction or country SE")
    stockholm = _location("Stockholm", None, "Sweden")
    assert _resolve(stockholm, address).code == "SE"


def test_i3_explicit_geography_settles_a_trailing_direction_and_a_town_does_not_hide_it():
    assert _resolve(WASHINGTON_DC, "1200 Pennsylvania Ave, SE, Washington, DC").code == "US-DC"
    assert _resolve(WASHINGTON_DC, "1200 Pennsylvania Avenue Southeast").code == "US-DC"
    # Before a named part it stays where the qualifier rules read it, as on main: before the country
    # it is the region slot ("NE" is Nebraska).
    _refused(DALLAS, "1 Main St, NE, USA", "states subdivision US-NE")
    # After a town, "SE" is the qualifier's code, as on main.
    _refused(WASHINGTON_DC, "1200 Pennsylvania Ave, Washington, SE", "states country SE")
    _refused(WASHINGTON_DC, "Washington, SE", "states country SE")
