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
        (DALLAS, "KE, Nairobi", "states country KE"),
        (DALLAS, "KE; Nairobi", "states country KE"),
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
    """In an address that mixes capitals and small letters, "NG" is a code and "Bo" a word (C-4: a
    town named like a country code stays the town). In an address written in one case the two
    cannot be told apart, so a contradicting reading asks for clarification."""
    _refused(DALLAS, "ACME, NG, Lagos", "states country NG")
    assert _resolve(FREETOWN, "Bo, Southern Province").code == "SL"
    _clarified(DALLAS, "ACME, NG, LAGOS", ADDRESS_PLACE_UNCLEAR, "a town's name or country NG")
    _clarified(DALLAS, "acme, ng, lagos", ADDRESS_PLACE_UNCLEAR, "a town's name or country NG")
    _clarified(FREETOWN, "BO, SOUTHERN PROVINCE", ADDRESS_PLACE_UNCLEAR, "country BO")
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
    # As the last part, the qualifier rules already read "SE" as Sweden, as on main: unchanged.
    _refused(WASHINGTON_DC, "1200 Pennsylvania Ave, SE", "states country SE")


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
        "country, or the name of a town. If it is the town, enter the same town as the "
        "location's city. If it is a state, province or country, write its full name (for "
        "example 'Lagos State' or 'Nigeria') after the town, separated by a comma."
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
    assert _resolve(FREETOWN, "Bo, Southern Province, Sierra Leone").code == "SL"
    assert _resolve(_location("Bo", None, "Sierra Leone"), "BO, SOUTHERN PROVINCE").code == "SL"
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
    assert read_business_address("Bo, Southern Province").places == ()
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
