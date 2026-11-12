"""GEO-P3 (P3-a): place knowledge from the address and the location, without data.

Geography register (2026-11-12) items G-2, G-7 and G-10, and review classes I-1, I-2 and I-6, under
the coordinator's rulings on the GEO-P3 plan (P3-R-1 to P3-R-5): no dataset, city table, geocoder
or alias list. A contradiction is refused; a code with one agreeing and one contradicting reading
that nothing settles asks for clarification (RULING-D1-C3); nothing here makes an address agree
that names another area.

These tests pin the measured classes; they do not show that arbitrary addresses are validated.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.geography.markets import read_business_address
from app.jurisdiction.codes import UNRESOLVED
from app.jurisdiction.resolver import (
    ADDRESS_NAMES_ANOTHER_PLACE,
    ADDRESS_PLACE_UNCLEAR,
    resolve_jurisdiction,
)


def _location(city, state, country) -> SimpleNamespace:
    return SimpleNamespace(city=city, state_province=state, country=country)


def _resolve(location, address):
    return resolve_jurisdiction(location, [SimpleNamespace(business_address=address)])


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


ANCHORAGE = _location("Anchorage", "Alaska", "United States")
MONTGOMERY = _location("Montgomery", "Alabama", "United States")
LEKKI = _location("Lekki", "Lagos", "Nigeria")
DALLAS = _location("Dallas", "Texas", "United States")
IKEJA = _location("Ikeja", "Lagos", "Nigeria")
NEW_ORLEANS = _location("New Orleans", "Louisiana", "United States")
VIENTIANE = _location("Vientiane", None, "Laos")
ASABA = _location("Asaba", "Delta", "Nigeria")
WASHINGTON_DC = _location("Washington", "District of Columbia", "United States")
SEATTLE = _location("Seattle", "Washington", "United States")
MINNA = _location("Minna", "Niger", "Nigeria")
NIAMEY = _location("Niamey", None, "Niger")
MUNICH = _location("Munich", "Bavaria", "Germany")
MUMBAI = _location("Mumbai", "Maharashtra", "India")
OSLO = _location("Oslo", None, "Norway")
ATLANTA = _location("Atlanta", "Georgia", "United States")


# --------------------------------------------------------------------------- #
# 1. Other separators (register G-10, review I-2)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "address",
    [
        "Alabama | Montgomery | 1 Main St | Suite 5",
        "Montgomery | AL | 1 Main Street",
        "Alabama\tMontgomery\t1 Main St\tSuite 5",
        "Alabama • Montgomery • 1 Main St",
        "Alabama · Montgomery · 1 Main St",
        "Alabama，Montgomery，1 Main St",
        "Alabama、Montgomery、1 Main St",
        "Alabama؛ Montgomery؛ 1 Main St",
        "Alabama - Montgomery - 1 Main St",
        "Alabama – Montgomery – 1 Main St",
    ],
)
def test_p3_other_separators_separate_parts(address):
    """A state named after a pipe, a tab, a bullet, a middle dot, a full-width or ideographic comma,
    the Arabic semicolon or a spaced dash is read; it was one part that named nothing."""
    assert _resolve(ANCHORAGE, address).code == UNRESOLVED
    assert _resolve(ANCHORAGE, address).reason is None
    assert _resolve(MONTGOMERY, address).code == "US-AL"


def test_p3_a_town_and_code_first_behind_a_pipe_asks():
    _clarified(
        ANCHORAGE,
        "Montgomery AL | 1 Main St | Suite 5",
        ADDRESS_PLACE_UNCLEAR,
        "is ambiguous: a name or subdivision US-AL",
    )


def test_p3_an_own_address_merged_across_a_pipe_is_no_longer_sao_tome():
    assert _resolve(MONTGOMERY, "ACME|Alabama|Springfield|1 Main St").code == "US-AL"


def test_p3_a_code_form_written_with_a_spaced_dash_stays_a_code():
    """ "NG - LA" is the code form NG-LA, not two parts; "AL - 1" is not a code form."""
    assert _resolve(LEKKI, "Plot 1, Lekki NG - LA").code == "NG-LA"
    _refused(DALLAS, "Plot 1, Lekki NG - LA", "states country NG")
    reading = read_business_address("Montgomery AL - 1 Main St")
    assert [p.text for p in reading.places][:1] == ["montgomery al"]


# --------------------------------------------------------------------------- #
# 2. A Nigerian code as the last part (register G-10 N-1)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("address", ["1 Main St, Ikeja, LA", "LA", "1 Main St, Ikeja, LA 100001"])
def test_p3_a_last_part_code_that_is_also_a_nigerian_state_asks(address):
    """ "LA" is Laos, Louisiana or Lagos State. For a Lagos location only the Nigerian reading
    agrees, and nothing settles it: the address asks, where it was refused as Laos or Louisiana."""
    _clarified(
        IKEJA,
        address,
        ADDRESS_PLACE_UNCLEAR,
        "is ambiguous: country LA or country US or subdivision NG-LA",
    )


def test_p3_nigeria_written_settles_a_nigerian_reading():
    assert _resolve(IKEJA, "1 Main St, Ikeja, LA, Nigeria").code == "NG-LA"


def test_p3_the_nigerian_reading_never_hides_a_contradiction_or_another_reading():
    _refused(DALLAS, "1 Main St, Ikeja, LA", "states country LA or subdivision US-LA or country NG")
    assert _resolve(NEW_ORLEANS, "1 Main St, New Orleans, LA").code == "US-LA"
    assert _resolve(VIENTIANE, "1 Main St, Ikeja, LA").code == "LA"


@pytest.mark.parametrize(
    ("address", "evidence"),
    [
        ("1500 Broadway US-LA", "states country US"),
        ("LA-1", "states country LA or country US"),
        ("State of CR, 1 Main St, Springfield, DE", "states country CR or subdivision NG-CR"),
    ],
)
def test_p3_a_code_form_or_an_explicit_code_elsewhere_is_still_refused(address, evidence):
    location = ASABA if address.endswith("DE") else IKEJA
    _refused(location, address, evidence)


def test_p3_a_building_name_before_a_nigerian_code_asks():
    """P3-R-2: "Kwara Plaza, …, LA" for Lagos asks (refused on main and GEO-P2, accepted pre-D1)."""
    _clarified(
        IKEJA,
        "Kwara Plaza, 1 Main St, Springfield, LA",
        ADDRESS_PLACE_UNCLEAR,
        "is ambiguous: country LA or country US or subdivision NG-LA",
    )


# --------------------------------------------------------------------------- #
# 3. The location's city (register G-2)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "address",
    [
        "US-DC, Washington",
        "US-DC, Washington, District of Columbia",
        "District of Columbia, Washington",
    ],
)
def test_p3_the_locations_city_is_the_town_not_washington_state(address):
    """ "Washington" is also Washington State. For a location whose city is Washington it is the
    town: the address resolves, where it was refused."""
    assert _resolve(WASHINGTON_DC, address).code == "US-DC"


def test_p3_the_city_never_hides_a_contradiction():
    _refused(
        SEATTLE,
        "US-DC, Washington, District of Columbia",
        "states subdivision US-DC, but the rest of the address cannot be read with it",
    )
    _refused(
        DALLAS,
        "US-TX, Ohio, Texas",
        "states subdivision US-TX, but the rest of the address cannot be read with it",
    )
    _refused(
        MONTGOMERY,
        "US-AL, Montgomery, Bavaria",
        "states subdivision US-AL, but the rest of the address cannot be read with it",
    )
    city_field_is_a_code = _location("US-AL", "Alabama", "United States")
    _refused(city_field_is_a_code, "US-AL, 1 Main St, Anchorage, AK", "states subdivision US-AK")


# --------------------------------------------------------------------------- #
# 3b. "Niger": the country, or Niger State (register G-2; ruling P3-R-6)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "address", ["Minna, Niger", "1 Allen Ave, Minna, Niger", "Minna, 1 Allen Ave, Niger"]
)
def test_p3_niger_for_a_niger_state_location_asks(address):
    """ "Niger" is the country and Niger State (X-5). For a Niger State location only Niger State
    agrees, and nothing settles it: the address asks, where it was refused as the country."""
    _clarified(
        MINNA, address, ADDRESS_PLACE_UNCLEAR, "is ambiguous: country NE or subdivision NG-NI"
    )


@pytest.mark.parametrize("address", ["Minna, Niger State", "Minna, Niger, Nigeria"])
def test_p3_niger_state_or_nigeria_written_settles_it(address):
    assert _resolve(MINNA, address).code == "NG-NI"


def test_p3_niger_never_hides_a_contradiction():
    _refused(MINNA, "1 Main St, Springfield, AB, Niger", "states country NE")
    _refused(DALLAS, "Minna, Niger", "states country NE")
    assert _resolve(NIAMEY, "Niamey, Niger").code == "NE"


# --------------------------------------------------------------------------- #
# 4. Unlisted ISO 3166-2 forms and country-prefixed postcodes (register G-7)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("location", "address", "code"),
    [
        (MUNICH, "DE-BY", "DE"),
        (MUNICH, "Munich DE-BY", "DE"),
        (MUMBAI, "IN-MH", "IN"),
        (MUMBAI, "Andheri IN-MH 400053", "IN"),
        (MUNICH, "Marienplatz 1, Munich DE-BY", "DE"),
        (MUNICH, "Marienplatz 1, 80331 Munich, DE-BY", "DE"),
        (OSLO, "ACME, Flat 3, NO-0150 Oslo", "NO"),
    ],
)
def test_p3_an_unlisted_code_form_names_its_country(location, address, code):
    """ "DE-BY" was read as Belarus ("BY") and refused for Munich; it is Germany's form, and the
    address agrees (D1's test held "Marienplatz 1, 80331 Munich, DE-BY" as stating nothing)."""
    resolution = _resolve(location, address)
    assert resolution.code == code
    assert resolution.evidence[-1].endswith("agrees"), resolution.evidence


@pytest.mark.parametrize(
    ("address", "evidence"),
    [
        ("DE-BY", "states country DE"),
        ("Marienplatz 1, Munich DE-BY", "states country DE"),
        ("DE-BY, Bavaria", "states country DE"),
        ("ACME, Flat 3, NO-0150 Oslo", "states country NO"),
    ],
)
def test_p3_an_unlisted_code_form_elsewhere_is_refused(address, evidence):
    _refused(DALLAS, address, evidence)


def test_p3_roads_and_hyphenated_names_are_not_code_forms():
    """ "AL-AIN" (Al Ain) and "GA-400" (a road) keep their readings."""
    _clarified(
        DALLAS,
        "KHALIFA ST, AL-AIN",
        "address_suffix_ambiguous",
        "is ambiguous: a street type or country ST",
    )
    assert _resolve(DALLAS, "4400 GA-400").reason == "address_route_ambiguous"


# --------------------------------------------------------------------------- #
# 5. The region slot before a two-reading name (review I-1)
# --------------------------------------------------------------------------- #


def test_p3_a_code_before_georgia_is_read_in_the_listed_country():
    """ "Georgia" is the country GE or US-GA, so "AL" before it is US-AL: the address names
    another state beside Georgia, which may be the town, so it asks; it was accepted."""
    _clarified(
        ATLANTA,
        "Springfield, AL, Georgia, 1 Main St",
        ADDRESS_NAMES_ANOTHER_PLACE,
        "also names subdivision US-AL",
    )
    assert _resolve(ATLANTA, "Springfield, GA, Georgia, 1 Main St").code == "US-GA"
    _refused(DALLAS, "Mumbai, MH, India", "states country IN")
