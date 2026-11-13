"""GEO-P3: place knowledge from the address and the location.

P3-a: geography register (2026-11-12) items G-2, G-7 and G-10, and review classes I-1, I-2 and I-6,
under the coordinator's rulings on the GEO-P3 plan (P3-R-1 to P3-R-5): no dataset, city table or
geocoder. P3-b: the operator's rulings of 2026-11-12 on G-3 (both forms ask), G-10 option (a) (the
bounded abbreviation and country-alias lists of ``app.geography.address_aliases``; "Korea" asks) and
other scripts (they ask). A contradiction is refused; a reading with one agreeing and one
contradicting area that nothing settles asks for clarification (RULING-D1-C3); nothing here makes an
address agree that names another area.

These tests pin the measured classes; they do not show that arbitrary addresses are validated.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.geography.address_aliases import (
    ADDRESS_ALIASES,
    ALIAS_ASKS,
    ALIAS_NAME,
    ALIAS_READ,
    SOURCES,
)
from app.geography.markets import _area_candidates, fold, read_business_address
from app.geography.places_data import COUNTRIES, SUBDIVISIONS
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
UMUAHIA = _location("Umuahia", "Abia", "Nigeria")
CALGARY = _location("Calgary", "Alberta", "Canada")
ABEOKUTA = _location("Abeokuta", "Ogun", "Nigeria")
BATON_ROUGE = _location("Baton Rouge", "Louisiana", "United States")


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
    # G-3's twin "US-AL, Montgomery, Bavaria" asks (P3-b-1, below); the city does not resolve it.
    city_field_is_a_code = _location("US-AL", "Alabama", "United States")
    _refused(city_field_is_a_code, "US-AL, 1 Main St, Anchorage, AK", "states subdivision US-AK")


@pytest.mark.parametrize(
    ("city", "state", "country", "address", "subdivision"),
    [
        ("AL", "Alaska", "United States", "1 Main St, AL, United States", "US-AL"),
        ("al", "Alaska", "United States", "1 Main St, al, United States", "US-AL"),
        ("Texas State", "Alaska", "United States", "1 Main St, Texas State, USA", "US-TX"),
        ("Ontario Province", "Alberta", "Canada", "1 Main St, Ontario Province, Canada", "CA-ON"),
        ("Lagos State", "Kano", "Nigeria", "1 Main St, Lagos State, Nigeria", "NG-LA"),
        ("Alabama", "Alaska", "United States", "1 Main St, Alabama, USA", "US-AL"),
        ("Georgia", "Alaska", "United States", "1 Main St, Georgia, USA", "US-GA"),
        ("Washington", "District of Columbia", "United States", "1 Main St, Wash., USA", "US-WA"),
    ],
)
def test_p3_r8_the_city_never_hides_a_part_that_names_an_area_alone(
    city, state, country, address, subdivision
):
    """Ruling P3-R-8: the location's city leaves out only a plain name, a part that, read alone,
    names no area. A code in any case, a name with a subdivision word, a subdivision's or a
    country's name and a listed alias (tested as written: the reader writes "Wash." as
    "Washington") each name one, so a city field equal to it hides nothing: the address is refused
    as before. With rule 3a's earlier test these resolved."""
    _refused(_location(city, state, country), address, f"states subdivision {subdivision}")


def test_p3_r8_a_plain_name_equal_to_the_city_is_still_the_town():
    """A word the reader takes for a possible town's name ("Washington", "Ogun") is still left out
    when it is the location's town (register G-2)."""
    assert _resolve(WASHINGTON_DC, "1 Main St, Washington, USA").code == "US-DC"
    assert _resolve(_location("Ogun", "Niger", "Nigeria"), "Ogun, NG-NI, Niger").code == "NG-NI"


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


MINSK = _location("Minsk", "Minsk", "Belarus")
YENAGOA = _location("Yenagoa", "Bayelsa", "Nigeria")


def test_p3_r9_an_unlisted_form_reads_only_itself():
    """Ruling P3-R-9: "DE-BY" is Germany's form, but a separate bare "BY" is also Belarus or Bayelsa
    State. The form does not settle that code, which keeps its outcome on GEO-P2: it asks when
    ambiguous and is refused when it contradicts."""
    _clarified(
        MUNICH,
        "Munich BY, DE-BY",
        ADDRESS_PLACE_UNCLEAR,
        "is ambiguous: a name or country BY or country NG",
    )
    _refused(MUNICH, "Marienplatz 1, Munich, BY, DE-BY", "states country BY or country NG")
    _refused(MUNICH, "Marienplatz 1, Munich DE-BY, BY", "states country BY")
    _refused(MUMBAI, "Andheri, Mumbai, MH, IN-MH", "states country MH")


def test_p3_r9_the_form_still_agrees_or_contradicts_by_itself():
    """The form alone still names its country, and an explicit country still settles the bare code
    as before. A form that contradicts is refused first, before review I-6's town and Nigerian code
    can ask ("Springfield BY": Belarus or Bayelsa State)."""
    assert _resolve(MUNICH, "Munich, DE-BY").code == "DE"
    assert _resolve(MUNICH, "Munich BY, Germany").code == "DE"
    _refused(MINSK, "Munich BY, DE-BY", "states country DE")
    _refused(YENAGOA, "1 Main St, Springfield BY, DE-BY", "states country DE")
    _clarified(
        YENAGOA,
        "1 Main St, Springfield BY",
        ADDRESS_PLACE_UNCLEAR,
        "is ambiguous: country BY or subdivision NG-BY",
    )


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


# --------------------------------------------------------------------------- #
# 6. A later-part or one-part town and Nigerian code (review I-6)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("location", "address", "evidence"),
    [
        (UMUAHIA, "1 Main St, Springfield AB", "is ambiguous: country CA or subdivision NG-AB"),
        (UMUAHIA, "Springfield AB", "is ambiguous: country CA or subdivision NG-AB"),
        (
            IKEJA,
            "1 Main St, Ikeja LA",
            "is ambiguous: country LA or country US or subdivision NG-LA",
        ),
        (IKEJA, "Baton Rouge LA", "is ambiguous: country US or subdivision NG-LA"),
        (ABEOKUTA, "Munich DE OG", "is ambiguous: country DE or subdivision NG-OG"),
    ],
)
def test_p3_a_town_and_shared_nigerian_code_asks(location, address, evidence):
    """ "AB" is Alberta or Abia State and "LA" Laos, Louisiana or Lagos State: for a location in
    the Nigerian state only that reading agrees, and nothing settles it, so the address asks. It
    was refused. "Baton Rouge LA" for Lagos asks too (P3-R-2): Baton Rouge is in no table."""
    _clarified(location, address, ADDRESS_PLACE_UNCLEAR, evidence)


def test_p3_nigeria_written_or_the_other_reading_settles_it():
    assert _resolve(UMUAHIA, "1 Main St, Springfield AB, Nigeria").code == "NG-AB"
    assert _resolve(CALGARY, "1 Main St, Calgary AB").code == "CA-AB"
    assert _resolve(BATON_ROUGE, "Baton Rouge LA").code == "US-LA"


def test_p3_a_town_and_nigerian_only_code_is_that_state():
    """ "OG" names only Ogun State: it agrees for an Ogun location and is refused elsewhere; it
    stated nothing before."""
    assert _resolve(ABEOKUTA, "1 Main St, Springfield OG").code == "NG-OG"
    _refused(MINNA, "1 Main St, Springfield OG", "states subdivision NG-OG")
    _refused(DALLAS, "1 Main St, Springfield AB", "states country CA")


# --------------------------------------------------------------------------- #
# P3-b-1. G-3: a listed code with words that name no listed area (operator ruling)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("location", "address"),
    [
        (MONTGOMERY, "US-AL, Downtown, Montgomery"),
        (_location("Town", "Alabama", "United States"), "US-AL, Downtown, Montgomery"),
        (MONTGOMERY, "US-AL, Montgomery, Bavaria"),
    ],
)
def test_p3b_the_own_code_with_unplaceable_words_asks(location, address):
    """The operator amended G-3's criterion: the own form and its twin both ask, since without
    other countries' subdivision names "Bavaria" cannot be told from "Downtown"."""
    _clarified(
        location,
        address,
        ADDRESS_PLACE_UNCLEAR,
        "is ambiguous: subdivision US-AL, but the rest of the address cannot be read with it",
    )


def test_p3b_the_remedy_resolves_and_another_area_is_still_refused():
    assert _resolve(MONTGOMERY, "Downtown, Montgomery, Alabama").code == "US-AL"
    _refused(
        DALLAS,
        "US-TX, Canada",
        "states subdivision US-TX, but the rest of the address cannot be read with it",
    )
    _refused(
        ANCHORAGE,
        "US-AL, Downtown, Montgomery",
        "states subdivision US-AL, but the rest of the address cannot be read with it",
    )


@pytest.mark.parametrize(
    ("location", "address", "code"),
    [
        (MONTGOMERY, "US-AL, Montgomery, State of Texas", "US-AL"),
        (MONTGOMERY, "US-AL, Montgomery, Texas State", "US-AL"),
        (UMUAHIA, "Adamawa State, NG-AB, Umuahia", "NG-AB"),
        (MONTGOMERY, "US-AL, Montgomery, Ont.", "US-AL"),
        (DALLAS, "US-TX, Dallas, Korea", "US-TX"),
    ],
)
def test_p3b_another_area_named_by_a_prefix_a_suffix_or_an_alias_is_refused(
    location, address, code
):
    """G-3 asks only when no other part names an area: a prefixed or suffixed subdivision and a
    listed alias (G-10) name one, so the own code with another area is refused, not asked."""
    _refused(
        location,
        address,
        f"states subdivision {code}, but the rest of the address cannot be read with it",
    )


# --------------------------------------------------------------------------- #
# P3-b-3. Other scripts ask (register G-10; operator ruling)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "address",
    ["1 Main St, Dallas, Техас", "1 Main St, Dallas, テキサス州", "1 Main St, Dallas, США"],
)
def test_p3b_a_part_in_another_script_asks(address):
    """The tables hold Latin names, so a state or country in another script cannot be read: the
    address asks, for its own location too (disclosed), and is never accepted elsewhere."""
    for location in (DALLAS, ANCHORAGE):
        _clarified(
            location,
            address,
            ADDRESS_PLACE_UNCLEAR,
            "is ambiguous: letters outside the Latin script",
        )


def test_p3b_accented_latin_is_read():
    assert _resolve(IKEJA, "1 Main St, Ikeja, Nigéria").code == "NG-LA"


# --------------------------------------------------------------------------- #
# P3-b-2. G-10 option (a): the bounded abbreviation and country-alias lists
# --------------------------------------------------------------------------- #

TORONTO = _location("Toronto", "Ontario", "Canada")
VANCOUVER = _location("Vancouver", "British Columbia", "Canada")
BRISBANE = _location("Brisbane", "Queensland", "Australia")
PERTH = _location("Perth", "Western Australia", "Australia")
SEOUL = _location("Seoul", None, "South Korea")
PYONGYANG = _location("Pyongyang", None, "North Korea")
TBILISI = _location("Tbilisi", None, "Georgia")


def _code_area(code: str) -> tuple[str, str | None]:
    return (code[:2], code) if "-" in code else (code, None)


def test_p3b_every_alias_has_a_source_a_valid_code_and_its_handling():
    """Each entry records its source, its ISO 3166 code and how it is read; the handling it records
    is the one the tables give it, so the list cannot drift from the reader."""
    seen = set()
    for alias in ADDRESS_ALIASES:
        assert alias.source in SOURCES, alias
        assert alias.written not in seen, alias
        seen.add(alias.written)
        for code in alias.codes:
            country, region = _code_area(code)
            assert country in COUNTRIES, alias
            assert region is None or region[3:] in SUBDIVISIONS[country], alias
        letters = fold(alias.written.replace(". ", "."))
        known = _area_candidates(letters)
        if alias.handling == ALIAS_NAME:
            assert len(alias.codes) == 1 and not known, alias
            assert _code_area(alias.codes[0]) in _area_candidates(fold(alias.name)), alias
        elif alias.handling == ALIAS_READ:
            assert len(alias.codes) == 1 and _code_area(alias.codes[0]) in known, alias
        else:
            assert alias.handling == ALIAS_ASKS and len(alias.codes) >= 2 and not known, alias
    for source in SOURCES.values():
        assert source.url.startswith("https://") and len(source.snapshot_sha256) == 64


@pytest.mark.parametrize(
    ("location", "address", "code"),
    [
        (MONTGOMERY, "1 Main St, Montgomery, Ala.", "US-AL"),
        (MONTGOMERY, "1 Main St, Montgomery, ALA. 36104, USA", "US-AL"),
        (TORONTO, "1 Main St, Toronto, Ont.", "CA-ON"),
        (TORONTO, "1 Main St, Toronto, Ont. M5V 2T6", "CA-ON"),
        (BRISBANE, "1 Main St, Brisbane, Qld", "AU-QLD"),
        (SEATTLE, "1 Main St, Seattle, Wash.", "US-WA"),
        (
            _location("New York", "New York", "United States"),
            "1 Main St, New York, N. Y. 10001",
            "US-NY",
        ),
        (MUNICH, "Marienplatz 1, 80331 Munich, Deutschland", "DE"),
        (IKEJA, "1 Allen Ave, Ikeja, Federal Republic of Nigeria", "NG-LA"),
        (
            _location("Sydney", "New South Wales", "Australia"),
            "1 George St, Sydney, Commonwealth of Australia",
            "AU-NSW",
        ),
        (
            _location("Mexico City", None, "Mexico"),
            "Av. Reforma 1, Mexico City, Estados Unidos Mexicanos",
            "MX",
        ),
    ],
)
def test_p3b_a_listed_alias_agrees_with_its_own_location(location, address, code):
    result = _resolve(location, address)
    assert result.code == code, (address, result.evidence)
    assert result.evidence[-1].endswith("agrees"), result.evidence


@pytest.mark.parametrize(
    ("location", "address", "evidence"),
    [
        (ANCHORAGE, "1 Main St, Montgomery, Ala.", "states subdivision US-AL"),
        (ANCHORAGE, "1 Main St, Montgomery, Ala., USA", "states subdivision US-AL"),
        (VANCOUVER, "1 Main St, Toronto, Ont.", "states subdivision CA-ON"),
        (PERTH, "1 Main St, Brisbane, Qld", "states subdivision AU-QLD"),
        (DALLAS, "1 Main St, Dallas, Deutschland", "states country DE"),
        (DALLAS, "1 Main St, Ikeja, Federal Republic of Nigeria", "states country NG"),
        (DALLAS, "1 Main St, Seoul, Korea", "states country KR or country KP"),
        (DALLAS, "US-TX, Dallas, Ont.", "but the rest of the address cannot be read with it"),
        (TBILISI, "1 Main St, Tbilisi, Ga.", "states country GA or country US"),
    ],
)
def test_p3b_a_listed_alias_that_contradicts_is_refused(location, address, evidence):
    _refused(location, address, evidence)


@pytest.mark.parametrize("location", [SEOUL, PYONGYANG])
def test_p3b_korea_asks_and_is_never_settled_by_the_location(location):
    """Its source lists "Korea" as South Korea or North Korea: neither the location nor place
    knowledge chooses; the address itself may ("Republic of Korea" written)."""
    _clarified(
        location,
        "1 Main St, Seoul, Korea",
        ADDRESS_PLACE_UNCLEAR,
        "is ambiguous: country KR or country KP",
    )
    reading = read_business_address("1 Main St, Seoul, Korea")
    assert [[r.country for r in g] for g in reading.alias_groups] == [["KR", "KP"]]
    assert reading.stated is None or reading.stated.country is None


def test_p3b_korea_with_its_country_written_resolves_and_still_refuses_elsewhere():
    assert _resolve(SEOUL, "1 Main St, Seoul, Korea, Republic of Korea").code == "KR"
    _refused(PYONGYANG, "1 Main St, Seoul, Korea, Republic of Korea", "states country KR")


@pytest.mark.parametrize(
    ("location", "address"),
    [
        # Without the period an abbreviation is a word or a name, and is not read (as before).
        (ANCHORAGE, "1 Main St, Montgomery, Ala"),
        (DALLAS, "ACME Car Wash, 1 Main St, Dallas, TX"),
        (DALLAS, "Miss. Jane Doe, 1 Main St, Dallas, TX"),
        (_location("Man", None, "Ivory Coast"), "1 Rue du Commerce, Man, Ivory Coast"),
    ],
)
def test_p3b_only_a_whole_part_with_its_period_is_an_alias(location, address):
    assert _resolve(location, address).code != UNRESOLVED, address


def test_p3b_an_alias_whose_letters_were_read_reads_as_before():
    """By its source "Ga." is Georgia, but its letters are already the code GA (Gabon) or US-GA,
    read as the C-8 group they were; "La." keeps N-1's Nigerian reading."""
    assert _resolve(ATLANTA, "1 Main St, Atlanta, Ga.").code == "US-GA"
    assert _resolve(BATON_ROUGE, "1 Main St, Baton Rouge, La.").code == "US-LA"
    _clarified(
        IKEJA,
        "1 Main St, Ikeja, La.",
        ADDRESS_PLACE_UNCLEAR,
        "is ambiguous: country LA or country US or subdivision NG-LA",
    )
    assert read_business_address("1 Main St, New York, N. Y.") == read_business_address(
        "1 Main St, New York, N.Y."
    )
