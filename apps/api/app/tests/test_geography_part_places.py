"""GEO-P1: explicit geographic evidence in a business address is read in every position.

Geography register (2026-11-12) items G-1 (including the 2,928 recorded contradiction cases), G-4,
G-5 (D1's FU-A) and G-9 (D1's FU-K), under the operator's instruction of 2026-11-12 ("Read explicit
geographic evidence wherever it occurs in the address, including across semicolons and line breaks";
"Refuse contradictions. Return UNRESOLVED with actionable clarification where the evidence is
insufficient or ambiguous") and the C-3 ruling of 2026-11-09 (no ambiguity waiver).

The qualifier rules read a listed code only in the last three parts of an address, and a state or
country only where a qualifier goes. ``read_business_address`` now also reads every part that names
a place by itself (``BusinessAddressReading.places``), and the resolver compares each with the
location, only to find a contradiction:

* a listed code (anywhere in a part) or a suffixed or prefixed subdivision is refused when it
  contradicts the location;
* a country's or a state's name, or a two-letter code in capitals in a mixed-case address, is
  refused too, unless another part names the location's own area, when it may be a town's or a
  business's name and the answer is a clarification (``address_names_another_place``);
* a word that may be a town's name is a clarification (``address_place_unclear``), and a part that
  is only a direction is one too, settled by explicit geography as D1 settles "St NE"
  (``address_direction_ambiguous``);
* a name or word equal to the location's own city is the town.

Review W21-REV-GEO r1 (CONDITIONAL on 62677168) added, under the coordinator's rulings R-1 and R-2:
the first part "<town> <code>" read as an ambiguous reading that explicit geography settles (C-1,
and C-1b for a Nigerian code), a Nigerian code's Nigerian reading (C-2), a short code that case
does not settle asks (C-3), every Unicode line break separates parts (C-4), and the
``address_place_unclear`` remedies for a business's or building's name (C-6).

These tests do not show that arbitrary addresses are validated: they pin the measured classes.
"""

from __future__ import annotations

import itertools
from types import SimpleNamespace

import pytest

from app.geography import markets
from app.geography.markets import (
    PLACE_CODE,
    PLACE_DIRECTION,
    PLACE_FIRST_PART,
    PLACE_NAME,
    PLACE_UNCLEAR,
    exclusion_readings,
    inclusion_readings,
    observed_readings,
    parse_market,
    parse_observed,
    read_business_address,
)
from app.jurisdiction.codes import UNRESOLVED
from app.jurisdiction.resolver import (
    ADDRESS_DIRECTION_AMBIGUOUS,
    ADDRESS_NAMES_ANOTHER_PLACE,
    ADDRESS_PLACE_UNCLEAR,
    ADDRESS_SUFFIX_AMBIGUOUS,
    CLARIFICATIONS,
    resolve_jurisdiction,
)
from app.jurisdiction.subdivisions import SUBDIVISION_JURISDICTIONS


def _location(city, state, country) -> SimpleNamespace:
    return SimpleNamespace(city=city, state_province=state, country=country)


def _resolve(location, address):
    return resolve_jurisdiction(location, [SimpleNamespace(business_address=address)])


DALLAS = _location("Dallas", "Texas", "United States")
ANCHORAGE = _location("Anchorage", "Alaska", "United States")
MONTGOMERY = _location("Montgomery", "Alabama", "United States")
HARTFORD = _location("Hartford", "Connecticut", "United States")
WASHINGTON_DC = _location("Washington", "District of Columbia", "United States")
LONDON = _location("London", None, "United Kingdom")
NAIROBI = _location("Nairobi", None, "Kenya")
IKEJA = _location("Ikeja", "Lagos", "Nigeria")
ABEOKUTA = _location("Abeokuta", "Ogun", "Nigeria")
UMUAHIA = _location("Umuahia", "Abia", "Nigeria")
TORONTO = _location("Toronto", "Ontario", "Canada")
OMAHA = _location("Omaha", "Nebraska", "United States")
FREETOWN = _location("Freetown", None, "Sierra Leone")


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
# G-1: the 2,928 recorded cases and the code at any distance from the end
# --------------------------------------------------------------------------- #

#: The review's depth probe (W21-REV-5DW2FIX r3f): three codes in each of the four countries, each
#: with the next subdivision of its country as the contradicting location. The 2,928 are its rows.
CODES = {
    "US": ("AL", "TX", "CT"),
    "CA": ("ON", "BC", "QC"),
    "AU": ("NSW", "VIC", "WA"),
    "NG": ("LA", "OG", "KN"),
}
COUNTRY_NAME = {"US": "United States", "CA": "Canada", "AU": "Australia", "NG": "Nigeria"}
SEPARATORS = (", ", "; ", "\n")


def _depth_cases():
    for country, subs in CODES.items():
        listed = list(SUBDIVISION_JURISDICTIONS[country])
        for sub in subs:
            own = SUBDIVISION_JURISDICTIONS[country][sub][0].title()
            other = SUBDIVISION_JURISDICTIONS[country][
                listed[(listed.index(sub) + 1) % len(listed)]
            ]
            kinds = {
                "T": "Springfield",
                "S": "1 Main St",
                "U": "Suite 5",
                "K": COUNTRY_NAME[country],
                "D": "Downtown",
            }
            yield (
                f"{country}-{sub}",
                kinds,
                _location("Town", own, COUNTRY_NAME[country]),
                _location("Town", other[0].title(), COUNTRY_NAME[country]),
            )


def _sequences(length):
    for seq in itertools.product("TSUKD", repeat=length):
        if seq.count("K") <= 1 and seq.count("T") <= 1:
            yield seq


def test_g1_the_2928_shape_is_refused_naming_the_code_in_every_separator():
    """The 2,928: a listed code first, three or four more parts, the last "1 Main St", for a
    location in another subdivision of the same country. Each is refused with evidence naming the
    written code (not "ST"), in the comma form as in the semicolon and line-break forms (register
    §3, criteria 1 and 2); its own-location twin resolves (criterion 3)."""
    checked = 0
    for code, kinds, own, other in _depth_cases():
        for length in (3, 4):
            for seq in _sequences(length):
                if seq[-1] != "S":
                    continue
                for separator in SEPARATORS:
                    address = separator.join([code, *(kinds[k] for k in seq)])
                    result = _resolve(other, address)
                    assert result.code == UNRESOLVED and result.reason is None, (address, result)
                    assert result.evidence[-1].endswith(f"states subdivision {code}"), (
                        address,
                        result.evidence,
                    )
                    assert _resolve(own, address).code == code, address
                    checked += 1
    # 122 shapes for each of the 12 codes, in three separators.
    assert checked == 122 * 12 * 3


@pytest.mark.parametrize("separator", SEPARATORS, ids=("comma", "semicolon", "newline"))
@pytest.mark.parametrize("where", ("first", "last"))
def test_g1_a_code_is_read_at_every_distance_from_the_end(separator, where):
    """The code one to five parts from the end, first or last: refused for another subdivision,
    accepted for its own (the depth probe's shapes with a town, a street, a unit and the country).
    """
    for code, kinds, own, other in _depth_cases():
        for length in (0, 1, 2, 3, 4):
            seq = ("T", "S", "U", "K")[:length]
            parts = [kinds[k] for k in seq]
            parts = [code, *parts] if where == "first" else [*parts, code]
            address = separator.join(parts)
            result = _resolve(other, address)
            assert result.code == UNRESOLVED, (address, result)
            assert result.reason is None, (address, result)
            assert _resolve(own, address).code == code, address


@pytest.mark.parametrize(
    ("location", "address", "evidence"),
    [
        (ANCHORAGE, "US-AL, Montgomery, Bavaria, Suite 5", "states subdivision US-AL"),
        (DALLAS, "US-AL, Montgomery, 1 Main St, United States", "states subdivision US-AL"),
        (ANCHORAGE, "US-AL, Alabama, Montgomery, US", "states subdivision US-AL"),
        (ANCHORAGE, "Alabama, Montgomery", "states subdivision US-AL"),
        (ANCHORAGE, "Alabama, Montgomery, 1 Main St, United States", "states subdivision US-AL"),
        (ANCHORAGE, "Montgomery, Alabama, 1 Main St", "states subdivision US-AL"),
        (
            _location("Yola", "Adamawa", "Nigeria"),
            "Abia State, Umuahia, Nigeria",
            "subdivision NG-AB",
        ),
    ],
)
def test_g1_contradictions_the_qualifier_rules_do_not_reach_are_refused(
    location, address, evidence
):
    """The register's G-1 examples and the extended P-CN's shapes: a code four or more parts from
    the end, a code, name and town before a trailing country, a state's name with a town and no
    code, and a suffixed Nigerian state first."""
    _refused(location, address, evidence)


@pytest.mark.parametrize(
    ("location", "address", "code"),
    [
        (MONTGOMERY, "US-AL, Montgomery, Bavaria, Suite 5", "US-AL"),
        (MONTGOMERY, "US-AL, Montgomery, 1 Main St, United States", "US-AL"),
        (MONTGOMERY, "Alabama, Montgomery", "US-AL"),
        (MONTGOMERY, "Montgomery, Alabama, 1 Main St", "US-AL"),
        (UMUAHIA, "Abia State, Umuahia, Nigeria", "NG-AB"),
    ],
)
def test_g1_the_same_addresses_resolve_for_their_own_location(location, address, code):
    assert _resolve(location, address).code == code


# --------------------------------------------------------------------------- #
# G-5 (FU-A): a listed code in the first part, and a code inside a part
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "address",
    [
        "Hartford US-CT, USA",
        "US-CT Hartford",
        "US-CT Hartford, 1 Main St",
        "1500 Broadway US CT, USA",
        "Hartford US–CT, Suite 5",
        "us-ct; Hartford; 1 Main St; Suite 5",
    ],
)
def test_g5_a_listed_code_in_the_first_part_is_read(address):
    """D1 read only a first part's ambiguous readings (FU-A): "Hartford US-CT, USA" resolved for
    Dallas. A listed code written anywhere in a part is now read: refused for Dallas, accepted for
    Hartford."""
    _refused(DALLAS, address, "states subdivision US-CT")
    assert _resolve(HARTFORD, address).code == "US-CT"


@pytest.mark.parametrize(
    "address",
    ["US-9, Dallas, TX", "1 Main St, GA-400, Dallas, TX", "AL-AIN, 1 Main St, Dallas, TX"],
)
def test_g5_road_numbers_and_hyphenated_names_are_not_codes(address):
    """Only a code the tables place is read (W21-REV-5DW2FIX r1 C-1): "US-9", "GA-400" and "AL-AIN"
    state nothing, wherever they stand."""
    assert _resolve(DALLAS, address).code == "US-TX"


def test_g5_a_listed_nigerian_code_after_a_town_is_read():
    """A Nigerian code after a town ("Lekki NG-LA") stated nothing in a business address (D1's C-9
    read NG-LA as unlisted). NG-LA is a listed jurisdiction code, so it is now that place: Lagos
    agrees, Dallas is refused."""
    assert _resolve(IKEJA, "1 Allen Ave, Lekki NG-LA").code == "NG-LA"
    assert _resolve(IKEJA, "1 Allen Ave, Lekki NG–LA").code == "NG-LA"
    _refused(DALLAS, "1 Allen Ave, Lekki NG-LA", "states country NG")


def test_g5_a_listed_nigerian_code_written_with_a_space_after_a_word_is_read():
    """D1's C-12 keeps a listed code written with a space explicit ("1 Main St, Los Angeles, US
    CA"); since #312 Nigeria's codes are listed, so "1500 Broadway NG AB" is Abia State, not only
    the group "Nigeria or Alberta", which agreed with any Nigerian location. A group whose every
    reading contradicts keeps C-12's evidence."""
    yola = _location("Yola", "Adamawa", "Nigeria")
    _refused(yola, "1500 Broadway NG AB, Nigeria", "states subdivision NG-AB")
    assert _resolve(UMUAHIA, "1500 Broadway NG AB, Nigeria").code == "NG-AB"
    new_orleans = _location("New Orleans", "Louisiana", "United States")
    _refused(new_orleans, "Ikeja NG LA", "states country NG")
    _refused(DALLAS, "Ikeja NG LA", "states country NG or country LA or subdivision US-LA")
    assert _resolve(IKEJA, "Ikeja NG LA").code == "NG-LA"


# --------------------------------------------------------------------------- #
# G-9 (FU-K): a code or name alone in a middle part; a state or country before the town
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("location", "address", "evidence"),
    [
        (DALLAS, "ACME, NG, Lagos", "states country NG"),
        (DALLAS, "ACME, Nigeria, Lagos", "states country NG"),
        (DALLAS, "ACME, Suite 5, NG 100001, Lagos", "states country NG"),
        (LONDON, "ACME, US, Dallas", "states country US"),
        (IKEJA, "Ogun State, Abeokuta", "states subdivision NG-OG"),
        # "KE" is Kenya or Kebbi State (W21-REV-GEO r1 C-2): every reading contradicts Dallas.
        (DALLAS, "KE, Nairobi", "states country KE or country NG"),
        (DALLAS, "KE; Nairobi", "states country KE or country NG"),
        (DALLAS, "Kenya\nNairobi\n1 Moi Ave", "states country KE"),
        (DALLAS, "ACME, State of Alabama, Montgomery", "states subdivision US-AL"),
    ],
)
def test_g9_a_code_or_name_before_the_town_is_refused(location, address, evidence):
    _refused(location, address, evidence)


@pytest.mark.parametrize(
    ("location", "address", "code"),
    [
        (IKEJA, "ACME, NG, Lagos", "NG-LA"),
        (IKEJA, "ACME, Nigeria, Lagos", "NG-LA"),
        (DALLAS, "ACME, US, Dallas", "US-TX"),
        (ABEOKUTA, "Ogun State, Abeokuta", "NG-OG"),
        (NAIROBI, "KE, Nairobi", "KE"),
        (MONTGOMERY, "ACME, State of Alabama, Montgomery", "US-AL"),
    ],
)
def test_g9_the_same_addresses_resolve_for_their_own_location(location, address, code):
    assert _resolve(location, address).code == code


# --------------------------------------------------------------------------- #
# G-4: a leading part naming another area beside the location's own
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("location", "address", "reason", "evidence"),
    [
        (DALLAS, "Oklahoma, US-TX, Texas, USA", ADDRESS_NAMES_ANOTHER_PLACE, "subdivision US-OK"),
        (UMUAHIA, "Kenya, NG-AB, Abia State, Nigeria", ADDRESS_NAMES_ANOTHER_PLACE, "country KE"),
        (UMUAHIA, "Adamawa, NG-AB, Abia State, Nigeria", ADDRESS_PLACE_UNCLEAR, "NG-AD"),
        (UMUAHIA, "Adamawa, NG-AB, Umuahia", ADDRESS_PLACE_UNCLEAR, "subdivision NG-AD"),
        (DALLAS, "Alaska, US-TX", ADDRESS_NAMES_ANOTHER_PLACE, "also names subdivision US-AK"),
        (MONTGOMERY, "Alabama, Alaska, US-AL", ADDRESS_NAMES_ANOTHER_PLACE, "subdivision US-AK"),
        (MONTGOMERY, "Georgia, US-AL", ADDRESS_NAMES_ANOTHER_PLACE, "or subdivision US-GA"),
    ],
)
def test_g4_another_area_beside_the_locations_own_asks_for_clarification(
    location, address, reason, evidence
):
    """r3d's twin argument: "Oklahoma, US-TX, Texas, USA" has the shape of "Washington, US-DC,
    District of Columbia, United States", whose first part is the town. Without place knowledge the
    first part may be a town or another area, so it is never accepted and never refused: it asks."""
    _clarified(location, address, reason, evidence)


def test_g4_the_locations_own_city_is_the_town():
    """The location's city is structured input the resolver already holds. A name equal to it is the
    town, so the DC forms resolve for a Washington location (the I-2 test's own row) and a business
    in a town named like a state resolves."""
    assert _resolve(
        WASHINGTON_DC, "Washington, US-DC, District of Columbia, United States"
    ).code == ("US-DC")
    assert _resolve(WASHINGTON_DC, "Washington, DC 20001").code == "US-DC"
    assert _resolve(_location("Oklahoma", "Texas", "US"), "Oklahoma, US-TX, Texas, USA").code == (
        "US-TX"
    )
    lebanon = _location("Lebanon", "Pennsylvania", "United States")
    assert _resolve(lebanon, "Lebanon, 1 Main St, PA 17042").code == "US-PA"
    nevada = _location("Nevada", "Missouri", "United States")
    assert _resolve(nevada, "Nevada, Vernon County, MO, USA").code == "US-MO"


def test_g4_a_code_is_never_the_town():
    """A listed code is compared whatever the location's city says."""
    location = _location("US-AL", "Alaska", "United States")
    _refused(location, "US-AL; Montgomery; 1 Main St; Suite 5", "states subdivision US-AL")


# --------------------------------------------------------------------------- #
# Text that may or may not be a place: clarification, never acceptance
# --------------------------------------------------------------------------- #


def test_a_two_letter_code_follows_the_case_it_is_written_in():
    """In an address that mixes capitals and small letters, "NG" written in capitals is a code. A
    code written otherwise ("Bo", "Tx") may be a word or a code, and so may any code in an address
    written in one case: a contradicting reading asks for clarification (W21-REV-GEO r1 C-3; the
    qualifier rules read "1 Main St, Dallas, Tx" as Texas). "Bo" for a location in Bo is the town
    (r3b C-4's row, through the location's city)."""
    _refused(DALLAS, "ACME, NG, Lagos", "states country NG")
    bo_or_borno = "a town's name or country BO or country NG"
    _clarified(FREETOWN, "Bo, Southern Province", ADDRESS_PLACE_UNCLEAR, bo_or_borno)
    assert _resolve(_location("Bo", None, "Sierra Leone"), "Bo, Southern Province").code == "SL"
    _clarified(DALLAS, "ACME, NG, LAGOS", ADDRESS_PLACE_UNCLEAR, "a town's name or country NG")
    _clarified(DALLAS, "acme, ng, lagos", ADDRESS_PLACE_UNCLEAR, "a town's name or country NG")
    _clarified(FREETOWN, "BO, SOUTHERN PROVINCE", ADDRESS_PLACE_UNCLEAR, bo_or_borno)
    assert _resolve(IKEJA, "ACME, NG, LAGOS").code == "NG-LA"


def test_a_nigerian_states_bare_name_and_a_city_first_name_ask_for_clarification():
    """A bare Nigerian state's name is also a town's (X-5), and "Washington" a city and a state
    (X-4): as the only geography they ask, they are never accepted for a location they
    contradict."""
    _clarified(DALLAS, "Lagos", ADDRESS_PLACE_UNCLEAR, "a town's name or country NG")
    _clarified(DALLAS, "Washington", ADDRESS_PLACE_UNCLEAR, "subdivision US-WA")
    _clarified(
        _location("Georgetown", "District of Columbia", "United States"),
        "Washington, DC",
        ADDRESS_PLACE_UNCLEAR,
        "a town's name or subdivision US-WA",
    )
    assert _resolve(IKEJA, "Lagos").code == "NG-LA"
    assert (
        _resolve(_location("Seattle", "Washington", "United States"), "Washington").code == "US-WA"
    )


def test_a_part_that_is_only_a_direction_is_read_as_d1_reads_st_ne():
    """A part "NE" may be a street's direction or a code (Nebraska, Niger): explicit geography
    settles it (C-14, C-15), otherwise it asks for clarification."""
    _clarified(
        DALLAS, "ACME, NE, Omaha", ADDRESS_DIRECTION_AMBIGUOUS, "country NE or subdivision US-NE"
    )
    assert _resolve(OMAHA, "ACME, NE, Omaha").code == "US-NE"
    dc_address = "1200 Pennsylvania Ave, SE, Washington, DC 20003"
    assert _resolve(WASHINGTON_DC, dc_address).code == "US-DC"
    _clarified(
        WASHINGTON_DC,
        "1200 Pennsylvania Ave, SE, Suite 5",
        ADDRESS_DIRECTION_AMBIGUOUS,
        "country SE",
    )
    # As the last part after a street line it is a direction part too (GEO-P2, review I-3); on
    # main and at GEO-P1 the qualifier rules read it as Sweden and refused it.
    _clarified(
        WASHINGTON_DC, "1200 Pennsylvania Ave, SE", ADDRESS_DIRECTION_AMBIGUOUS, "country SE"
    )


def test_a_floor_or_unit_label_before_its_number_is_left_to_the_unit_rules():
    """ "FL 3" and "NO 5" are a floor and a unit (GEO-P2), never Florida or Norway here."""
    assert _resolve(DALLAS, "ACME, 123 MAIN ST, FL 3, DALLAS, TX").code == "US-TX"
    assert _resolve(DALLAS, "ACME, 123 Main St, Fl 3, Dallas, TX").code == "US-TX"
    assert _resolve(DALLAS, "ACME, NO 5, 123 Main St, Dallas, TX").code == "US-TX"


@pytest.mark.parametrize(
    "address",
    [
        "Shop No-5, MG Road, Mumbai, MH, India",
        "Mumbai, MH 400001, India",
        "MG Road, Mumbai; MH; India",
        "MG Road, Mumbai, MH, 400001, India",
    ],
)
def test_a_region_code_before_an_unlisted_country_is_left_unread(address):
    """A code "MH" before "India" is the region slot of "town, region, country", and India's
    subdivisions are not listed, so the tables cannot read it (the register's G-7); it is not read
    as the Marshall Islands' code. The lane's realistic row "Shop No-5, MG Road, Mumbai, MH, India"
    resolves, as on main."""
    assert _resolve(_location("Mumbai", "Maharashtra", "India"), address).code == "IN"


@pytest.mark.parametrize(
    ("location", "address", "code"),
    [
        (IKEJA, "Ikeja, LA, Nigeria", "NG-LA"),
        (IKEJA, "1 Allen Ave, Ikeja, LA, 100001, Nigeria", "NG-LA"),
        (IKEJA, "LA, NG", "NG-LA"),
        (_location("Kano", "Kano", "Nigeria"), "Kano, KN, Nigeria", "NG-KN"),
        (DALLAS, "Dallas, TX, USA", "US-TX"),
        (_location("Los Angeles", "California", "United States"), "CA, USA", "US-CA"),
    ],
)
def test_a_code_before_a_listed_country_is_that_countrys_subdivision(location, address, code):
    """In the region slot of "town, region, country" the qualifier rules read a code as the
    country's subdivision ("LA" before "Nigeria" is Lagos State, never Laos or Louisiana); a part
    place reads it the same way."""
    assert _resolve(location, address).code == code


def test_a_three_letter_state_name_is_a_name_not_a_code():
    """ "Edo", "Imo" and "Oyo" are Nigerian states' bare names (also towns), not codes: before
    "Nigeria" they are not read as the region slot's code, and they ask for clarification for a
    location in another state, as any bare Nigerian name does."""
    ebonyi = _location("Abakaliki", "Ebonyi", "Nigeria")
    for address in ("Edo, Nigeria, NG-EB", "Imo, Nigeria, NG-EB", "Oyo, Nigeria, NG-EB"):
        result = _resolve(ebonyi, address)
        assert (result.code, result.reason) == (UNRESOLVED, ADDRESS_PLACE_UNCLEAR), address
    # Written in capitals in a mixed-case address it is still a name, never a code.
    _clarified(ebonyi, "ACME, EDO, Nigeria, 1 Main St", ADDRESS_PLACE_UNCLEAR, "NG-ED")
    assert _resolve(_location("Benin City", "Edo", "Nigeria"), "Edo, Nigeria").code == "NG-ED"


def test_another_subdivisions_code_before_a_listed_country_is_refused():
    _refused(IKEJA, "Ikeja, KE, Nigeria", "states subdivision NG-KE")
    _clarified(IKEJA, "KE, NG", ADDRESS_PLACE_UNCLEAR, "a town's name or subdivision NG-KE")


def test_a_part_place_never_settles_an_ambiguous_reading_or_adds_agreement():
    """A part place only finds contradictions. "123 MAIN ST" stays ambiguous (D1) beside an agreeing
    code four parts from the end, and an accepted address carries no new "agrees" evidence."""
    address = "US-AL; 123 MAIN ST; MONTGOMERY; Suite 5"
    result = _resolve(MONTGOMERY, address)
    assert (result.code, result.reason) == (UNRESOLVED, ADDRESS_SUFFIX_AMBIGUOUS)
    _refused(ANCHORAGE, address, "states subdivision US-AL")
    accepted = _resolve(MONTGOMERY, "US-AL; Montgomery; 1 Main St; 1 Main St")
    assert accepted.code == "US-AL"
    assert not any("agrees" in line for line in accepted.evidence)


# --------------------------------------------------------------------------- #
# Review W21-REV-GEO r1: conditions C-1 to C-4 and C-8
# --------------------------------------------------------------------------- #

PERTH = _location("Perth", "Western Australia", "Australia")
MADRID = _location("Madrid", None, "Spain")
STOCKHOLM = _location("Stockholm", None, "Sweden")
MINNA = _location("Minna", "Niger", "Nigeria")


@pytest.mark.parametrize(
    ("location", "address", "evidence", "own", "code"),
    [
        (ANCHORAGE, "Dallas TX, 1 Main St", "a name or subdivision US-TX", DALLAS, "US-TX"),
        (
            ANCHORAGE,
            "Montgomery AL; 1 Main St; Suite 5; Floor 2",
            "subdivision US-AL",
            MONTGOMERY,
            "US-AL",
        ),
        (
            TORONTO,
            "Vancouver BC, 1 Main St, Suite 5, Floor 2",
            "subdivision CA-BC",
            _location("Vancouver", "British Columbia", "Canada"),
            "CA-BC",
        ),
        (
            PERTH,
            "Melbourne VIC 3000, 1 Main St, Level 3, Suite 5",
            "subdivision AU-VIC",
            _location("Melbourne", "Victoria", "Australia"),
            "AU-VIC",
        ),
        (HARTFORD, "Dallas TX, USA", "a name or subdivision US-TX", DALLAS, "US-TX"),
        (ANCHORAGE, "Dallas TX\nSpringfield\n1 Main St", "subdivision US-TX", DALLAS, "US-TX"),
    ],
)
def test_c1_a_first_part_town_and_code_asks_for_clarification(
    location, address, evidence, own, code
):
    """C-1 (GEO-P1's G-1 and G-5): D1 reads "Dallas TX" as explicit in a later part but never in
    the first, which may be a business's name and its legal form. The first part's reading is now
    ambiguous: it asks when every reading contradicts the location, and its own location
    resolves."""
    _clarified(location, address, ADDRESS_PLACE_UNCLEAR, evidence)
    assert _resolve(own, address).code == code


def test_c1_a_first_part_town_and_code_is_compared_whatever_the_city():
    """Like a code, the first part's reading is compared even when the location's city field holds
    the same text."""
    city = _location("Dallas TX", "Alaska", "United States")
    _clarified(city, "Dallas TX, 1 Main St", ADDRESS_PLACE_UNCLEAR, "a name or subdivision US-TX")


def test_c1_explicit_geography_settles_a_first_part_town_and_code():
    """Ruling R-2 (the settled form): an explicit subdivision of the location's country settles the
    first part's reading, and an explicit country rules out its readings in other countries. It is
    never refused and never agrees. "Dallas TX, 1 Main St, Anchorage, AK" resolves for Anchorage, as
    on main: a settled ambiguity (the first part may be a business's name), not evidence read."""
    assert _resolve(ANCHORAGE, "Dallas TX, 1 Main St, Anchorage, AK").code == "US-AK"
    assert _resolve(MADRID, "ACME SA, Calle Mayor 1, 28013 Madrid, Spain").code == "ES"
    _clarified(MADRID, "ACME SA, Calle Mayor 1, 28013 Madrid", ADDRESS_PLACE_UNCLEAR, "country AU")
    # D1's R19/R20 own rows: "AB" is a Swedish legal form, Alberta and Abia State; the country
    # written resolves them.
    address = "ACME AB, 5th St SE, 111 51 Stockholm"
    _clarified(STOCKHOLM, address, ADDRESS_PLACE_UNCLEAR, "country CA or country NG")
    assert _resolve(STOCKHOLM, f"{address}, Sweden").code == "SE"
    # The location's own country written rules out readings elsewhere, not another state's.
    us = "Dallas TX, 1 Main St, United States"
    _clarified(ANCHORAGE, us, ADDRESS_PLACE_UNCLEAR, "a name or subdivision US-TX")


def test_c9_a_suffixed_or_prefixed_first_part_stays_explicit():
    """W21-REV-GEO r2 C-9: the first part's town-and-code reading comes after the suffixed and
    prefixed readings, so a prefixed code in the first part is explicit there as elsewhere, and a
    prefixed Nigerian code has its Nigerian reading (as C-2 gives the bare code one)."""
    _refused(DALLAS, "State of WA, 1 Main St, Dallas, TX", "states subdivision US-WA or country AU")
    _refused(DALLAS, "Province of ON, 1 Main St, Dallas, TX", "states country CA or country NG")
    jos = _location("Jos", "Plateau", "Nigeria")
    assert _resolve(jos, "State of PL, 1 Main St, Nigeria").code == "NG-PL"


def test_c1_a_first_part_designator_keeps_d1s_reading():
    """D1's C-16 keeps a first-part designator's reading ("1 SR 520, Redmond"): it is not read as a
    town and a code."""
    assert read_business_address("1 SR 520, Redmond").places == ()
    redmond = _location("Redmond", "Washington", "United States")
    assert _resolve(redmond, "1 SR 520, Redmond").code == "US-WA"


def test_c1b_a_first_part_town_and_nigerian_code_asks_for_clarification():
    """C-1b (ruling R-2): the first part's reading takes C-2's Nigerian table for a trailing
    two-letter code that names nothing else ("Springfield AN"; 192 such rows were refused before
    D1). Settled the same way: never refused, never agreeing."""
    bauchi = _location("Bauchi", "Bauchi", "Nigeria")
    awka = _location("Awka", "Anambra", "Nigeria")
    for separator in (", ", "; ", "\n", " / "):
        address = separator.join(["Springfield AN", "1 Main St"])
        _clarified(bauchi, address, ADDRESS_PLACE_UNCLEAR, "a name or subdivision NG-AN")
        assert _resolve(awka, address).code == "NG-AN"
    assert _resolve(bauchi, "Springfield AN, 1 Main St, Bauchi State").code == "NG-BA"
    # A code alone stays a short part (C-2: refused), and a code that names something else keeps
    # D1's reading ("12 Allen Pl" is a street line; PL is Poland and Plateau State).
    _refused(MINNA, "OG, Abeokuta, 1 Main St", "states subdivision NG-OG")
    assert _resolve(IKEJA, "12 Allen Pl, Ikeja").code == "NG-LA"
    # A spaced code form keeps C-12's two readings: Germany's code form, or OG after a word.
    munich = _location("Munich", None, "Germany")
    assert _resolve(munich, "Munich DE OG, 1 Main St").code == "DE"
    assert _resolve(ABEOKUTA, "Munich DE OG, 1 Main St").code == "NG-OG"
    _clarified(DALLAS, "Munich DE OG, 1 Main St", ADDRESS_PLACE_UNCLEAR, "country DE or country NG")
    # Its cost: "Oy" is a Finnish legal form and Oyo State's code. The country written resolves it.
    _clarified(MADRID, "ACME Oy, Calle Mayor 1, 28013 Madrid", ADDRESS_PLACE_UNCLEAR, "country NG")
    assert _resolve(MADRID, "ACME Oy, Calle Mayor 1, 28013 Madrid, Spain").code == "ES"


def test_c2_a_bare_nigerian_code_has_its_nigerian_reading():
    """C-2: "LA" is Laos, Louisiana or Lagos State; "OG" names only Ogun State. Its own location
    resolves (main accepted these; 62677168 refused them) and another state is refused, in a
    middle part and in the region slot before "Nigeria"."""
    assert _resolve(IKEJA, "ACME, LA, Ikeja, 1 Main St").code == "NG-LA"
    lagos = _location("Lagos", "Lagos", "Nigeria")
    assert _resolve(lagos, "ACME, LA, Springfield, 1 Main St").code == "NG-LA"
    _refused(MINNA, "Abeokuta, OG, Nigeria, 1 Main St", "states subdivision NG-OG")
    _refused(MINNA, "ACME, OG, Abeokuta, 1 Main St", "states subdivision NG-OG")
    assert _resolve(ABEOKUTA, "ACME, OG, Abeokuta, 1 Main St").code == "NG-OG"
    _refused(DALLAS, "ACME, LA, Ikeja, 1 Main St", "country LA or subdivision US-LA or country NG")


def test_c3_a_code_that_case_does_not_settle_asks_for_clarification():
    """C-3: the qualifier rules read "1 Main St, Dallas, Tx" as Texas; before the street the same
    text was a word. It now asks; the location in Bo keeps r3b C-4's row."""
    _clarified(ANCHORAGE, "Dallas, Tx, 1 Main St", ADDRESS_PLACE_UNCLEAR, "subdivision US-TX")
    _refused(ANCHORAGE, "1 Main St, Dallas, Tx", "states subdivision US-TX")
    _clarified(PERTH, "Brisbane, Qld, 1 Main St, Level 3", ADDRESS_PLACE_UNCLEAR, "AU-QLD")
    _clarified(DALLAS, "ACME, Ke, Nairobi", ADDRESS_PLACE_UNCLEAR, "country KE or country NG")
    assert _resolve(_location("Bo", None, "Sierra Leone"), "Bo, Southern Province").code == "SL"
    assert _resolve(DALLAS, "Dallas, Tx, 1 Main St").code == "US-TX"


@pytest.mark.parametrize("separator", ["\u2028", "\u2029", "\x0b", "\x0c", "\x85", "\r\n"])
def test_c4_every_unicode_line_break_separates_parts(separator):
    """C-4: the instruction reads "across ... line breaks"; :meth:`str.splitlines` knows each."""
    address = separator.join(["Alabama", "Springfield", "1 Main St", "Suite 5"])
    _refused(ANCHORAGE, address, "states subdivision US-AL")
    assert _resolve(MONTGOMERY, address).code == "US-AL"


def test_c8_the_reviewers_surviving_mutants_have_a_test_row():
    """C-8: one row each for the review's V3 (an unclear word is not the own area), V4 (the city's
    first comma part) and V5 (a typographic dash before the town)."""
    _refused(IKEJA, "Oklahoma, 1 Main St, Lagos", "states country US")
    dc = _location("Washington, D.C.", "District of Columbia", "United States")
    assert _resolve(dc, "Washington, 1 Main St, DC 20001").code == "US-DC"
    _refused(DALLAS, "US\u2013CT Hartford, 1 Main St", "states subdivision US-CT")


# --------------------------------------------------------------------------- #
# The clarifications: fixed text, and each remedy resolves
# --------------------------------------------------------------------------- #


def test_the_geo_p1_clarifications_are_fixed_text():
    assert CLARIFICATIONS[ADDRESS_NAMES_ANOTHER_PLACE] == (
        "The business address names a state, province or country that is not this "
        "location's, as well as the location's own. If that part of the address is the "
        "town, enter the same town as the location's city. If it is the name of the "
        "business or a building, leave it out of the address. Otherwise correct or remove it."
    )
    assert CLARIFICATIONS[ADDRESS_PLACE_UNCLEAR] == (
        "We couldn't tell whether a word in the business address is a state, province or "
        "country, or the name of a town. If it is the town, write the state, province or "
        "region code right after it without a comma (for example 'Washington DC' or "
        "'Victoria BC'), or leave the town out. If it is a state, province or country, write "
        "its full name (for example 'Lagos State' or 'Nigeria') after the town, separated by a "
        "comma. If it is the name of the business or a building, leave it out of the address. "
        "Otherwise correct or remove it."
    )
    assert CLARIFICATIONS[ADDRESS_DIRECTION_AMBIGUOUS] == (
        "We couldn't tell whether a part of the business address that is only a direction "
        "(such as 'NE' or 'SE') is the street's direction or a state or country. Write the "
        "direction in full after the street name (for example '1200 Pennsylvania Avenue "
        "Southeast'), or add the city and state or country after a comma."
    )
    for address in ("Jordan, 1 Main St, Dallas, TX", "Adamawa, NG-AB, Umuahia", "ACME, NE, Omaha"):
        for message in CLARIFICATIONS.values():
            assert address not in message
            assert all(part.strip() not in message for part in address.split(",")[:1])


def test_each_remedy_resolves():
    # address_names_another_place: the town is the location's city, or the business name leaves.
    _clarified(DALLAS, "Jordan, 1 Main St, Dallas, TX", ADDRESS_NAMES_ANOTHER_PLACE, "country JO")
    assert _resolve(DALLAS, "1 Main St, Dallas, TX").code == "US-TX"
    oklahoma = _location("Oklahoma", "Texas", "United States")
    assert _resolve(oklahoma, "Oklahoma, US-TX, Texas, USA").code == "US-TX"
    # address_place_unclear: the town is the location's city; or the state written in full, which is
    # then explicit (refused when it contradicts, accepted when it agrees).
    assert (
        _resolve(_location("Adamawa", "Abia", "Nigeria"), "Adamawa, NG-AB, Umuahia").code == "NG-AB"
    )
    explicit = _resolve(UMUAHIA, "Adamawa State, NG-AB, Umuahia")
    assert (explicit.code, explicit.reason) == (UNRESOLVED, None)
    _refused(UMUAHIA, "Adamawa State, Umuahia, 1 Main St, Suite 5", "states subdivision NG-AD")
    assert _resolve(_location("Bo", None, "Sierra Leone"), "BO, SOUTHERN PROVINCE").code == "SL"
    # W21-REV-GEO r1 C-6: a business's or a building's name is left out, or the word is corrected
    # or removed; for a first part with a legal form, the country written resolves too (C-1).
    madrid = _location("Madrid", None, "Spain")
    _clarified(madrid, "ACME SA, Calle Mayor 1, 28013 Madrid", ADDRESS_PLACE_UNCLEAR, "country AU")
    assert _resolve(madrid, "Calle Mayor 1, 28013 Madrid").code == "ES"
    assert _resolve(madrid, "ACME SA, Calle Mayor 1, 28013 Madrid, Spain").code == "ES"
    _clarified(FREETOWN, "Bo, Southern Province, Sierra Leone", ADDRESS_PLACE_UNCLEAR, "NG")
    assert _resolve(FREETOWN, "Southern Province, Sierra Leone").code == "SL"
    _clarified(HARTFORD, "8 Spring Hill Ct, Lagos", ADDRESS_PLACE_UNCLEAR, "country NG")
    assert _resolve(HARTFORD, "8 Spring Hill Ct").code == "US-CT"
    # address_direction_ambiguous: the direction in full, or the city and state after a comma.
    assert _resolve(WASHINGTON_DC, "1200 Pennsylvania Avenue Southeast").code == "US-DC"
    assert _resolve(WASHINGTON_DC, "1200 Pennsylvania Ave, SE, Washington, DC").code == "US-DC"


# --------------------------------------------------------------------------- #
# Scope: the business-address reader only
# --------------------------------------------------------------------------- #


def test_part_places_are_read_by_kind():
    places = read_business_address(
        "US-AL; Montgomery; Nigeria; ACME, NG, Lagos; NE; Washington"
    ).places
    kinds = [(place.kind, place.text) for place in places]
    assert kinds == [
        (PLACE_CODE, "us al"),
        (PLACE_NAME, "nigeria"),
        (PLACE_NAME, "ng"),
        (PLACE_UNCLEAR, "lagos"),
        (PLACE_DIRECTION, "ne"),
        (PLACE_UNCLEAR, "washington"),
    ]
    bo = read_business_address("Bo, Southern Province").places
    assert [(place.kind, place.text) for place in bo] == [(PLACE_UNCLEAR, "bo")]
    assert {(r.country, r.region) for r in bo[0].readings} == {("BO", None), ("NG", "NG:borno")}
    first = read_business_address("Dallas TX, 1 Main St").places
    assert [(place.kind, place.text) for place in first] == [(PLACE_FIRST_PART, "dallas tx")]
    assert read_business_address(None).places == ()


@pytest.mark.parametrize(
    "text", ["US-AL; Montgomery; 1 Main St; 1 Main St", "ACME, NG, Lagos", "Ogun State, Abeokuta"]
)
def test_only_the_business_address_reader_reads_part_places(text):
    """Exclusion, inclusion and observed readings (X-1 to X-5) and the market parsers keep their
    readings: ``places`` exists only on the business-address reading."""
    for reader in (exclusion_readings, observed_readings, inclusion_readings):
        assert all(not hasattr(reading, "places") for reading in reader(text))
    assert not hasattr(parse_market(text), "places")
    assert not hasattr(parse_observed(text), "places")
    assert markets.parse_business_address(text) == read_business_address(text).stated
