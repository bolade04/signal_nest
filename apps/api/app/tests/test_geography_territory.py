"""GEO-P4: "<Name> Territory" in the business address (geography register RI-1).

The operator's instruction of 2026-11-13: "Preserve the geography rules: no selected-location
inference, no ambiguity waiver, and actionable clarification where the address cannot be resolved.
Do not assume a historical territory has the boundaries of a present-day state."

So a part that names a territory the tables do not list ("Alabama Territory", "Dakota Territory",
"Territory of Hawaii", "Lagos Territory") is read as no area: it is never accepted, never refused as
the state whose name it holds, and asks with one fixed message, for every location and whatever the
location's city. A refusal of another part stands. The present-day names the tables list ("Northern
Territory", "Yukon Territory", "Northwest Territories", "Australian Capital Territory", "Federal
Capital Territory") are read as before, and so is "Territory" alone. No name, list or dataset is
added.

The operator's answer of 2026-11-13 ("Same rule, in GEO-P4") extends the rule to the state fields:
the location's own state or province and the structured business-address state or province read an
unlisted "<Name> Territory" as nothing, so the location is unresolved and the structured state is
unreadable.

These tests pin the measured classes; they do not show that arbitrary addresses are validated.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.geography.markets import read_business_address
from app.jurisdiction.business_address import (
    ACTION_CLARIFICATIONS,
    BUSINESS_ADDRESS_INCOMPLETE,
    LOCATION_UNRESOLVED,
    TEXT_REMEDY,
    location_code,
    resolve_for_action,
    structured_status,
)
from app.jurisdiction.codes import UNRESOLVED
from app.jurisdiction.resolver import (
    ADDRESS_PLACE_UNCLEAR,
    ADDRESS_TERRITORY_UNCLEAR,
    CLARIFICATIONS,
    resolve_jurisdiction,
    subdivision_code_of,
)
from app.locations.schemas import UNKNOWN_REGION, GeoCoverageBase, unreadable_address_fields

TERRITORY_ASKS = "is ambiguous: a territory that is not a listed state, province, region or country"


def _location(city, state, country) -> SimpleNamespace:
    return SimpleNamespace(city=city, state_province=state, country=country)


def _resolve(location, address):
    return resolve_jurisdiction(location, [SimpleNamespace(business_address=address)])


def _asks(location, address):
    result = _resolve(location, address)
    assert result.code == UNRESOLVED, (address, result)
    assert result.reason == ADDRESS_TERRITORY_UNCLEAR, (address, result)
    assert result.evidence[-1].endswith(TERRITORY_ASKS), (address, result.evidence)


def _refused(location, address, evidence):
    result = _resolve(location, address)
    assert result.code == UNRESOLVED, (address, result)
    assert result.reason is None, (address, result)
    assert result.evidence[-1].endswith(evidence), (address, result.evidence)


def _resolves(location, address, code):
    result = _resolve(location, address)
    assert result.code == code, (address, result)


ANCHORAGE = _location("Anchorage", "Alaska", "United States")
MONTGOMERY = _location("Montgomery", "Alabama", "United States")
BISMARCK = _location("Bismarck", "North Dakota", "United States")
TULSA = _location("Tulsa", "Oklahoma", "United States")
HONOLULU = _location("Honolulu", "Hawaii", "United States")
SEATTLE = _location("Seattle", "Washington", "United States")
DALLAS = _location("Dallas", "Texas", "United States")
NEW_ORLEANS = _location("New Orleans", "Louisiana", "United States")
IKEJA = _location("Ikeja", "Lagos", "Nigeria")
ABUJA = _location("Abuja", "FCT", "Nigeria")
DARWIN = _location("Darwin", "NT", "Australia")
SYDNEY = _location("Sydney", "NSW", "Australia")
CANBERRA = _location("Canberra", "ACT", "Australia")
WHITEHORSE = _location("Whitehorse", "YT", "Canada")
YELLOWKNIFE = _location("Yellowknife", "NT", "Canada")
VANCOUVER = _location("Vancouver", "BC", "Canada")
LONDON = _location("London", None, "United Kingdom")
NAIROBI = _location("Nairobi", None, "Kenya")


# --------------------------------------------------------------------------- #
# RI-1: "<Name> Territory" asks, for the namesake location and for any other
# --------------------------------------------------------------------------- #
RI1_FORMS = [
    "Alabama Territory",
    "Alabama Territories",
    "Territory of Alabama",
    "Territories of Alabama",
]
SHAPES = [
    "{f}{s}1 Main St{s}United States",
    "1 Main St{s}{f}{s}United States",
    "1 Main St{s}United States{s}{f}",
    "1 Main St{s}Montgomery{s}{f}",
    "1 Main St{s}{f}",
]


@pytest.mark.parametrize("form", RI1_FORMS)
@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.parametrize("sep", [", ", "; ", "\n"])
@pytest.mark.parametrize("location", [MONTGOMERY, ANCHORAGE], ids=["namesake", "elsewhere"])
def test_ri1_a_territory_with_a_listed_name_asks_wherever_it_stands(form, shape, sep, location):
    _asks(location, shape.format(f=form, s=sep))


def test_ri1_the_examples_of_the_register():
    # Accepted on every tree since pre-D1; "Alabama State" stays refused for Anchorage.
    _asks(ANCHORAGE, "Alabama Territory, 1 Main St, United States")
    _asks(ANCHORAGE, "1 Main St, Montgomery, Alabama Territory")
    _refused(ANCHORAGE, "1 Main St, Montgomery, Alabama State", "states subdivision US-AL")


def test_ri1_the_region_slot_no_longer_reads_the_territory_as_the_state():
    # "town, region, country": the reader read "Alabama Territory" there as US-AL (refused for
    # Anchorage).
    _asks(ANCHORAGE, "1 Main St, Montgomery, Alabama Territory, USA")
    _asks(MONTGOMERY, "1 Main St, Montgomery, Alabama Territory, USA")
    _asks(DALLAS, "1 Main St, Dallas, Texas Territory, USA")


@pytest.mark.parametrize(
    ("location", "address"),
    [
        (BISMARCK, "1 Main St, Bismarck, Dakota Territory"),
        (TULSA, "1 Main St, Tulsa, Indian Territory"),
        (HONOLULU, "1 Main St, Honolulu, Territory of Hawaii"),
        (HONOLULU, "1 Main St, Honolulu, The Territory of Hawaii 96813"),
        (SEATTLE, "1 Main St, Seattle, Washington Territory"),
        (DALLAS, "1 Main St, Seattle, Washington Territory"),
        (NEW_ORLEANS, "1 Main St, New Orleans, Orleans Territory"),
        (ANCHORAGE, "Territory of Alaska\n1 Main St\nAnchorage"),
        (YELLOWKNIFE, "1 Main St, Yellowknife, North-West Territories"),
        (NAIROBI, "1 Main St, Nairobi, Kenya Territory"),
        (LONDON, "1 Main St, London, Kenya Territory"),
    ],
)
def test_ri1_a_historical_territory_asks_for_every_location(location, address):
    _asks(location, address)


@pytest.mark.parametrize("location", [IKEJA, ABUJA])
def test_ri1_a_nigerian_states_name_with_territory_is_no_longer_that_state(location):
    # Read as NG-LA before: accepted for a Lagos location, refused for Abuja.
    _asks(location, "1 Allen Ave, Ikeja, Lagos Territory")
    _asks(location, "1 Allen Ave, Ikeja, Lagos Territory, Nigeria")
    _asks(location, "Lagos Territory")


def test_ri1_the_suffixed_state_is_read_as_before():
    _resolves(IKEJA, "1 Allen Ave, Ikeja, Lagos State", "NG-LA")
    _refused(ABUJA, "1 Allen Ave, Ikeja, Lagos State", "states subdivision NG-LA")


def test_ri1_the_reading_holds_the_part_and_reads_its_name_as_no_area():
    reading = read_business_address("1 Allen Ave, Ikeja, Lagos Territory")
    assert reading.territories == ("Lagos Territory",)
    assert reading.places == ()
    assert read_business_address("1 Main St, Darwin, Northern Territory").territories == ()
    assert read_business_address("1 Main St, Territory, Australia").territories == ()


# --------------------------------------------------------------------------- #
# The names the tables list are read as before; so is "Territory" alone
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("location", "address", "code"),
    [
        (DARWIN, "1 Main St, Darwin, Northern Territory", "AU-NT"),
        (DARWIN, "1 Main St, Darwin, Northern Territory 0800", "AU-NT"),
        (DARWIN, "1 Main St, Darwin Northern Territory 0800", "AU-NT"),
        (CANBERRA, "1 Main St, Canberra, Australian Capital Territory", "AU-ACT"),
        (WHITEHORSE, "1 Main St, Whitehorse, Yukon Territory", "CA-YT"),
        (YELLOWKNIFE, "1 Main St, Yellowknife, Northwest Territories", "CA-NT"),
        (YELLOWKNIFE, "1 Main St, Yellowknife, State of Northwest Territories", "CA-NT"),
        (ABUJA, "1 Main St, Abuja, Federal Capital Territory", "NG-FC"),
        (ABUJA, "1 Main St, Abuja, Federal Capital Territory, Nigeria", "NG-FC"),
        (SYDNEY, "1 Main St, Territory, Australia", "AU-NSW"),
        (SYDNEY, "1 Main St, Sydney, Territory", "AU-NSW"),
        (DALLAS, "1 Territory Rd, Dallas, TX", "US-TX"),
        (DALLAS, "Lost Territory Brewing, 1 Main St, Dallas, TX", "US-TX"),
    ],
)
def test_listed_names_and_the_word_alone_resolve_as_before(location, address, code):
    _resolves(location, address, code)


@pytest.mark.parametrize(
    ("location", "address", "evidence"),
    [
        (SYDNEY, "1 Main St, Darwin, Northern Territory", "states subdivision AU-NT"),
        (VANCOUVER, "1 Main St, Whitehorse, Yukon Territory", "states subdivision CA-YT"),
        (LONDON, "1 Main St, London, British Indian Ocean Territory", "states country IO"),
    ],
)
def test_listed_names_still_refuse_another_location(location, address, evidence):
    _refused(location, address, evidence)


# --------------------------------------------------------------------------- #
# A refusal of another part stands; a code in the part is read
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("address", "evidence"),
    [
        ("US-TX, Alabama Territory", "states subdivision US-TX"),
        ("1 Main St, Alabama Territory, Canada", "states country CA"),
        ("1 Main St, Dallas, TX, Oklahoma Territory", "states subdivision US-TX"),
        (
            "Oklahoma Territory; US-TX; Dallas; 1 Main St",
            "states subdivision US-TX, but the rest of the address cannot be read with it",
        ),
        ("US-AL Territory, 1 Main St", "states subdivision US-AL"),
    ],
)
def test_another_parts_contradiction_is_still_refused(address, evidence):
    _refused(ANCHORAGE, address, evidence)


def test_a_code_in_the_part_is_read_and_the_part_still_asks():
    _asks(MONTGOMERY, "US-AL Territory, 1 Main St")


# --------------------------------------------------------------------------- #
# The location's city plays no part (G-11, ruling P3-R-16)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("city", ["Montgomery", "Alabama Territory", "Alabama", "Territory", None])
@pytest.mark.parametrize("state", ["Alabama", "Alaska"])
def test_the_city_field_never_settles_a_territory(city, state):
    location = _location(city, state, "United States")
    for address in (
        "1 Main St, Montgomery, Alabama Territory",
        "Alabama Territory, 1 Main St, USA",
    ):
        _asks(location, address)


# --------------------------------------------------------------------------- #
# The message: fixed text, before any other ask, and its remedy resolves
# --------------------------------------------------------------------------- #
def test_the_territory_clarification_is_fixed_text():
    assert CLARIFICATIONS[ADDRESS_TERRITORY_UNCLEAR] == (
        "We couldn't match a territory named in the business address to a present-day state, "
        "province, region or country: a historical territory's borders are not a present-day "
        "state's. Write the present-day state, province, region or country instead, by its full "
        "name or its code (for example 'North Dakota' or 'ND', or 'Lagos State'), or leave that "
        "part out of the address."
    )
    for address in ("1 Main St, Montgomery, Alabama Territory", "Lost Territory, 1 Main St"):
        for part in address.split(", "):
            assert part not in CLARIFICATIONS[ADDRESS_TERRITORY_UNCLEAR]


def test_the_territory_ask_comes_before_another_ask():
    # Without the territory part the address asks as G-3 does (address_place_unclear); with it, the
    # territory's ask comes first.
    assert _resolve(MONTGOMERY, "US-AL, Downtown, Montgomery").reason == ADDRESS_PLACE_UNCLEAR
    _asks(MONTGOMERY, "US-AL, Downtown, Montgomery, Alabama Territory")


@pytest.mark.parametrize(
    ("location", "address", "remedies", "code"),
    [
        (
            MONTGOMERY,
            "1 Main St, Montgomery, Alabama Territory",
            [
                "1 Main St, Montgomery, Alabama",
                "1 Main St, Montgomery, AL",
                "1 Main St, Montgomery",
            ],
            "US-AL",
        ),
        (
            BISMARCK,
            "1 Main St, Bismarck, Dakota Territory",
            ["1 Main St, Bismarck, North Dakota", "1 Main St, Bismarck, ND", "1 Main St, Bismarck"],
            "US-ND",
        ),
        (
            HONOLULU,
            "Territory of Hawaii, 1 Main St, Honolulu",
            ["Hawaii, 1 Main St, Honolulu", "HI, 1 Main St, Honolulu", "1 Main St, Honolulu"],
            "US-HI",
        ),
        (
            IKEJA,
            "1 Allen Ave, Ikeja, Lagos Territory",
            ["1 Allen Ave, Ikeja, Lagos State", "1 Allen Ave, Ikeja"],
            "NG-LA",
        ),
        (
            NAIROBI,
            "1 Main St, Nairobi, Kenya Territory",
            ["1 Main St, Nairobi, Kenya", "1 Main St, Nairobi"],
            "KE",
        ),
    ],
)
def test_the_remedy_resolves_when_followed_literally(location, address, remedies, code):
    _asks(location, address)
    for remedy in remedies:
        _resolves(location, remedy, code)


# --------------------------------------------------------------------------- #
# ADDR's resolve_for_action: such text asks at step 5; the structured fields remedy it
# --------------------------------------------------------------------------- #
def _rule(text, **fields) -> SimpleNamespace:
    return SimpleNamespace(
        business_address=text,
        business_address_street=fields.get("street"),
        business_address_locality=fields.get("locality"),
        business_address_region=fields.get("region"),
        business_address_postcode=fields.get("postcode"),
        business_address_country=fields.get("country"),
    )


MONTGOMERY_FIELDS = {"country": "United States", "region": "Alabama", "locality": "Montgomery"}


def test_addr_a_territory_in_the_text_asks_at_step_5_with_the_text_remedy():
    result = resolve_for_action(
        MONTGOMERY, [_rule("1 Main St, Montgomery, Alabama Territory", **MONTGOMERY_FIELDS)]
    )
    assert result.code == UNRESOLVED
    assert result.reason == ADDRESS_TERRITORY_UNCLEAR
    assert ACTION_CLARIFICATIONS[ADDRESS_TERRITORY_UNCLEAR] == (
        CLARIFICATIONS[ADDRESS_TERRITORY_UNCLEAR] + TEXT_REMEDY
    )


@pytest.mark.parametrize(
    "text", [None, "", "1 Main St, Montgomery, Alabama", "1 Main St, Montgomery"]
)
def test_addr_the_structured_fields_or_the_message_remedy_it(text):
    result = resolve_for_action(MONTGOMERY, [_rule(text, **MONTGOMERY_FIELDS)])
    assert result.code == "US-AL", result


def test_addr_another_parts_contradiction_is_still_a_step_3_refusal():
    anchorage = {"country": "United States", "region": "Alaska", "locality": "Anchorage"}
    result = resolve_for_action(
        ANCHORAGE, [_rule("1 Main St, Dallas, TX, Oklahoma Territory", **anchorage)]
    )
    assert result.reason == "business_address_text_conflict"


# --------------------------------------------------------------------------- #
# The state fields (the operator's answer of 2026-11-13, "Same rule, in GEO-P4"): the location's
# own state or province (item 33) and the structured business-address state or province read an
# unlisted "<Name> Territory" as nothing, and fail closed
# --------------------------------------------------------------------------- #
UNLISTED_STATES = [
    ("US", "Alabama Territory"),
    ("US", "Washington Territory"),
    ("US", "AL Territory"),
    ("US", "Territory of Hawaii"),
    ("CA", "Nunavut Territory"),
    ("CA", "NT Territory"),
    ("AU", "NT Territory"),
    ("NG", "Lagos Territory"),
    ("NG", "Abuja Territory"),
    ("NG", "FCT Territory"),
]
LISTED_STATES = [
    ("CA", "Yukon Territory", "CA-YT"),
    ("CA", "Yukon", "CA-YT"),
    ("CA", "Northwest Territories", "CA-NT"),
    ("AU", "Northern Territory", "AU-NT"),
    ("AU", "Australian Capital Territory", "AU-ACT"),
    ("NG", "Federal Capital Territory", "NG-FC"),
    ("NG", "FCT", "NG-FC"),
    ("NG", "Lagos State", "NG-LA"),
    ("US", "Texas State", "US-TX"),
    ("US", "US-AL", "US-AL"),
]


@pytest.mark.parametrize(("country", "state"), UNLISTED_STATES)
def test_state_reader_reads_an_unlisted_territory_as_nothing(country, state):
    assert subdivision_code_of(country, state) is None


@pytest.mark.parametrize(("country", "state", "code"), LISTED_STATES)
def test_state_reader_reads_listed_names_as_before(country, state, code):
    assert subdivision_code_of(country, state) == code


@pytest.mark.parametrize("city", ["Seattle", "Washington Territory", "Washington", None])
def test_a_locations_own_state_written_as_an_unlisted_territory_is_unresolved(city):
    location = _location(city, "Washington Territory", "United States")
    result = resolve_jurisdiction(location)
    assert result.code == UNRESOLVED
    assert result.evidence[-1] == (
        "unresolved: 'Washington Territory' is not a state or province of US"
    )
    assert location_code(location) is None
    assert resolve_for_action(location, [_rule(None, **MONTGOMERY_FIELDS)]).reason == (
        LOCATION_UNRESOLVED
    )


@pytest.mark.parametrize(
    ("city", "state", "country", "code"),
    [
        ("Whitehorse", "Yukon Territory", "Canada", "CA-YT"),
        ("Yellowknife", "Northwest Territories", "Canada", "CA-NT"),
        ("Darwin", "Northern Territory", "Australia", "AU-NT"),
        ("Canberra", "Australian Capital Territory", "Australia", "AU-ACT"),
        ("Abuja", "Federal Capital Territory", "Nigeria", "NG-FC"),
    ],
)
def test_a_locations_own_listed_territory_still_resolves(city, state, country, code):
    assert resolve_jurisdiction(_location(city, state, country)).code == code
    assert resolve_jurisdiction(_location(state, state, country)).code == code


@pytest.mark.parametrize("city", ["Montgomery", "Alabama Territory", None])
def test_a_structured_state_written_as_an_unlisted_territory_is_unreadable(city):
    location = _location(city, "Alabama", "United States")
    fields = {**MONTGOMERY_FIELDS, "region": "Alabama Territory"}
    if city is None:
        fields.pop("locality")
    rule = _rule(None, **fields)
    # Accepted for Montgomery before (read as US-AL); now unreadable, so incomplete (step 4).
    assert structured_status(rule, "US-AL", city) == "incomplete"
    assert resolve_for_action(location, [rule]).reason == BUSINESS_ADDRESS_INCOMPLETE
    # Never a contradiction on that basis: for Anchorage it is incomplete too, not a conflict.
    assert structured_status(rule, "US-AK", city) == "incomplete"
    # The coverage PUT refuses to store it, with the fixed message of an unreadable state.
    assert unreadable_address_fields("United States", "Alabama Territory") == [
        ("business_address_region", UNKNOWN_REGION)
    ]
    with pytest.raises(ValidationError):
        GeoCoverageBase(
            business_address_country="United States", business_address_region=("Alabama Territory")
        )


def test_a_structured_listed_territory_still_reads():
    darwin = _location("Darwin", "NT", "Australia")
    fields = {"country": "Australia", "region": "Northern Territory", "locality": "Darwin"}
    assert resolve_for_action(darwin, [_rule(None, **fields)]).code == "AU-NT"
    assert unreadable_address_fields("Australia", "Northern Territory") == []
