"""W21 ADDR: :func:`app.jurisdiction.business_address.resolve_for_action` (design v2 §4).

The operator's instruction of 2026-11-12, pinned rule by rule (U-1..U-23 of the design's test
plan; pure, no database):

* the location is checked first, then a structured contradiction (refused), then the free
  text's explicit contradiction (refused), then completeness (asks), then the free text's
  ambiguity (asks), then the town (asks; the only confirmable step);
* the mandatory fields are "the existing jurisdiction requirements" (item 33): the country,
  and the state or province in US/CA/AU/NG, from ONE table (coordinator ruling OP-3);
* empty fields never fall back to the free text ("town alone states nothing"), and a region
  with no country is never read in the location's country;
* "established mapping" for a town is equality after ``fold`` and nothing else; street,
  postcode and town are never read as codes;
* a customer confirmation answers only the town, is bound to its values, and never overrides
  a contradiction;
* the vocabulary is closed, every message fixed, and nothing customer-written reaches a message
  or an evidence line this module adds.
"""

from __future__ import annotations

import ast
import itertools
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.jurisdiction import business_address as ba
from app.jurisdiction.business_address import (
    ACTION_CLARIFICATIONS,
    ADDRESS_CLARIFICATIONS,
    BUSINESS_ADDRESS_CONFLICT,
    BUSINESS_ADDRESS_INCOMPLETE,
    BUSINESS_ADDRESS_LOCALITY_UNCONFIRMED,
    BUSINESS_ADDRESS_TEXT_CONFLICT,
    LOCATION_UNRESOLVED,
    MANDATORY_FIELDS,
    REFUSALS,
    TEXT_REMEDY,
    location_code,
    resolve_for_action,
)
from app.jurisdiction.codes import UNRESOLVED
from app.jurisdiction.resolver import (
    ADDRESS_SUFFIX_AMBIGUOUS,
    CLARIFICATIONS,
    resolve_jurisdiction,
)

APP = Path(__file__).resolve().parents[1]
SENTINEL = "Zq7Sentinel"


def loc(city=None, state=None, country=None, **extra) -> SimpleNamespace:
    return SimpleNamespace(city=city, state_province=state, country=country, **extra)


def rule(text=None, *, country=None, region=None, locality=None, street=None, postcode=None,
         **extra) -> SimpleNamespace:
    return SimpleNamespace(
        business_address=text,
        business_address_street=street,
        business_address_locality=locality,
        business_address_region=region,
        business_address_postcode=postcode,
        business_address_country=country,
        **extra,
    )


def confirmed(locality: str, city: str, code: str, **fields) -> SimpleNamespace:
    return rule(
        locality=locality,
        locality_confirmed_locality=locality,
        locality_confirmed_city=city,
        locality_confirmed_jurisdiction=code,
        locality_confirmed_at=datetime(2026, 11, 12, tzinfo=UTC),
        **fields,
    )


DALLAS = loc("Dallas", "TX", "US")
#: A complete business address for the Dallas location (its town required: the location has a city).
TX_DALLAS = {"country": "US", "region": "TX", "locality": "Dallas"}
ANCHORAGE = loc("Anchorage", "Alaska", "United States")
LONDON = loc("London", None, "United Kingdom")
LAGOS = loc("Lagos", "Lagos", "Nigeria")
NAIROBI = loc("Nairobi", None, "Kenya")
TORONTO = loc("Toronto", "Ontario", "Canada")
SYDNEY = loc("Sydney", "NSW", "Australia")
#: Each location and the structured fields that agree with it (with its own town).
OWN = {
    "US-TX": (DALLAS, {"country": "United States", "region": "Texas", "locality": "Dallas"}),
    "US-AK": (ANCHORAGE, {"country": "US", "region": "AK", "locality": "Anchorage"}),
    "GB": (LONDON, {"country": "United Kingdom", "locality": "London"}),
    "NG-LA": (LAGOS, {"country": "Nigeria", "region": "Lagos State", "locality": "Lagos"}),
    "KE": (NAIROBI, {"country": "Kenya", "locality": "Nairobi"}),
    "CA-ON": (TORONTO, {"country": "Canada", "region": "ON", "locality": "Toronto"}),
    "AU-NSW": (SYDNEY, {"country": "Australia", "region": "New South Wales", "locality": "Sydney"}),
}


def answer(location, *rules):
    r = resolve_for_action(location, list(rules))
    return r.code if r.is_resolved else r.reason


# --------------------------------------------------------------------------- #
# U-1 / U-22: the matrix resolves as the location does; the route helper agrees
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("code", sorted(OWN))
def test_complete_agreeing_fields_resolve_to_the_locations_own_code(code: str) -> None:
    location, fields = OWN[code]
    assert resolve_jurisdiction(location).code == code
    r = resolve_for_action(location, [rule(**fields)])
    assert r.is_resolved and r.code == code
    assert r.codes == resolve_jurisdiction(location).codes
    assert r.reason is None
    assert "structured business address agrees" in r.evidence


@pytest.mark.parametrize(
    "location",
    [*(v[0] for v in OWN.values()), loc("X", None, None), loc("X", None, "Narnia"),
     loc("X", None, "US"), loc("X", "Bavaria", "US"), loc("X", "Texas", "Germany")],
)
def test_the_route_helper_reads_the_location_as_the_resolver_does(location) -> None:
    resolved = resolve_jurisdiction(location, ())
    assert location_code(location) == (resolved.code if resolved.is_resolved else None)


# --------------------------------------------------------------------------- #
# U-2 / U-3 / U-13: a canonical contradiction is refused, whatever else is filled
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("region", [None, "Texas", "Alabama", "Bavaria"])
@pytest.mark.parametrize("locality", [None, "Dallas", "Toronto"])
@pytest.mark.parametrize("text", [None, "1 Main St, Dallas, TX", "1500 Broadway Hartford CT"])
def test_another_country_is_refused(region, locality, text) -> None:
    got = answer(DALLAS, rule(text, country="Canada", region=region, locality=locality))
    assert got == BUSINESS_ADDRESS_CONFLICT


@pytest.mark.parametrize("region", ["Alabama", "AL", "US-AL", "alabama"])
@pytest.mark.parametrize("locality", [None, "Anchorage", "Montgomery"])
def test_another_state_is_refused(region, locality) -> None:
    got = answer(ANCHORAGE, rule("Montgomery, 1 Main St, United States", country="US",
                                 region=region, locality=locality))
    assert got == BUSINESS_ADDRESS_CONFLICT


def test_a_stored_confirmation_never_overrides_a_contradiction() -> None:
    # Valid when made (Ikeja within Lagos, NG-LA); the region later names Ogun State.
    stale = confirmed("Ikeja", "Lagos", "NG-LA", country="Nigeria", region="Ogun")
    assert answer(LAGOS, stale) == BUSINESS_ADDRESS_CONFLICT
    stale.business_address = "1 Allen Ave, Ikeja, Ontario, Canada"  # a text contradiction too
    stale.business_address_region = "Lagos"
    assert answer(LAGOS, stale) == BUSINESS_ADDRESS_TEXT_CONFLICT


# --------------------------------------------------------------------------- #
# U-4 / U-5: what is compared, in which readings
# --------------------------------------------------------------------------- #
def test_a_country_class_region_is_neither_required_nor_compared() -> None:
    assert answer(LONDON, rule(country="UK", locality="London")) == "GB"
    assert answer(LONDON, rule(country="England", region="Texas", locality="London")) == "GB"
    assert answer(NAIROBI, rule(country="KE", region="Bavaria", locality="Nairobi")) == "KE"


@pytest.mark.parametrize(
    ("location", "country", "region"),
    [
        (DALLAS, "US", "TX"), (DALLAS, "USA", "Texas"), (DALLAS, "United States", "US-TX"),
        (DALLAS, "united states", "texas"), (LAGOS, "NG", "LA"), (LAGOS, "Nigeria", "NG-LA"),
        (LAGOS, "Nigeria", "Lagos State"), (TORONTO, "CA", "Ontario"), (SYDNEY, "AU", "NSW"),
    ],
)
def test_codes_names_and_suffixed_names_agree(location, country, region) -> None:
    got = answer(location, rule(country=country, region=region, locality=location.city))
    assert got == resolve_jurisdiction(location).code


# --------------------------------------------------------------------------- #
# U-6 / U-7 / U-8 / U-9 / R-3: completeness; empty fields never fall back to the text
# --------------------------------------------------------------------------- #
TEXTS = [
    None,
    "Montgomery, 1 Main St, United States",  # town-only
    "Dallas, United States",  # the seed's form
    "1 Main St, Dallas, TX",  # own, explicit
    "1500 Broadway Hartford CT",  # ambiguous street line
    "US-1",
]


@pytest.mark.parametrize("code", sorted(OWN))
@pytest.mark.parametrize("text", TEXTS)
def test_empty_fields_never_resolve(code, text) -> None:
    location, _ = OWN[code]
    got = answer(location, rule(text))
    if text is not None and resolve_jurisdiction(location, [rule(text)]).reason is None and \
            not resolve_jurisdiction(location, [rule(text)]).is_resolved:
        assert got == BUSINESS_ADDRESS_TEXT_CONFLICT  # an explicit contradiction is refused
    else:
        assert got == BUSINESS_ADDRESS_INCOMPLETE
    assert answer(location) == BUSINESS_ADDRESS_INCOMPLETE  # no rule at all


@pytest.mark.parametrize("code", ["US-TX", "CA-ON", "AU-NSW", "NG-LA"])
def test_the_four_subdivision_countries_require_the_region(code) -> None:
    location, fields = OWN[code]
    assert answer(location, rule(**{**fields, "region": None})) == BUSINESS_ADDRESS_INCOMPLETE
    assert answer(location, rule(**{**fields, "region": "  "})) == BUSINESS_ADDRESS_INCOMPLETE


@pytest.mark.parametrize("code", ["GB", "KE"])
def test_other_countries_require_the_country_and_with_a_city_the_town(code) -> None:
    location, fields = OWN[code]
    assert answer(location, rule(country=fields["country"], locality=fields["locality"])) == code
    assert answer(location, rule(country=fields["country"])) == BUSINESS_ADDRESS_INCOMPLETE
    assert answer(location, rule(locality=fields["locality"])) == BUSINESS_ADDRESS_INCOMPLETE
    no_city = loc(None, None, location.country)
    assert answer(no_city, rule(country=fields["country"])) == code


# --------------------------------------------------------------------------- #
# The town (operator instruction of 2026-11-12, item 3): required when the location has a city
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("code", sorted(OWN))
def test_the_town_is_required_when_the_location_has_a_city(code) -> None:
    location, fields = OWN[code]
    assert location.city
    assert answer(location, rule(**fields)) == code
    for empty in (None, "", "   ", "..."):
        assert answer(location, rule(**{**fields, "locality": empty})) == (
            BUSINESS_ADDRESS_INCOMPLETE
        )


@pytest.mark.parametrize("city", [None, "", "  ", "-"])
def test_a_location_with_no_city_has_no_locality_to_compare(city) -> None:
    """The town stays optional, nothing is derived or copied, and the evidence says so."""
    location = loc(city, "TX", "US")
    bare = rule(country="US", region="TX")
    r = resolve_for_action(location, [bare])
    assert r.code == "US-TX"
    assert ba.NO_LOCALITY_TO_COMPARE in r.evidence
    assert bare.business_address_locality is None  # never filled in
    assert answer(location, rule(country="US", region="TX", locality="Irving")) == "US-TX"
    assert answer(location, rule(country="US", region="OK")) == BUSINESS_ADDRESS_CONFLICT
    assert answer(location, rule(country="US")) == BUSINESS_ADDRESS_INCOMPLETE
    assert ba.NO_LOCALITY_TO_COMPARE not in resolve_for_action(
        DALLAS, [rule(**TX_DALLAS)]
    ).evidence


def test_a_genuinely_missing_town_has_no_bypass() -> None:
    """A location with a city and an address with no locality stays incomplete: a stored
    confirmation, a free text naming the city, or a placeholder never resolves it."""
    stale = confirmed("Irving", "Dallas", "US-TX", country="US", region="TX")
    stale.business_address_locality = None
    assert answer(DALLAS, stale) == BUSINESS_ADDRESS_INCOMPLETE
    assert answer(DALLAS, rule("1 Main St, Dallas, TX", country="US", region="TX")) == (
        BUSINESS_ADDRESS_INCOMPLETE
    )
    message = ADDRESS_CLARIFICATIONS[BUSINESS_ADDRESS_INCOMPLETE]
    assert "the business town or locality" in message
    assert "named place used in the business's postal address" in message
    assert "If the location's own city is wrong, correct it on its Details tab instead." in message


def test_the_mandatory_set_is_one_table_derived_from_item_33() -> None:
    assert dict(MANDATORY_FIELDS) == {
        ("subdivision", True): (
            "business_address_country", "business_address_region", "business_address_locality"
        ),
        ("subdivision", False): ("business_address_country", "business_address_region"),
        ("country", True): ("business_address_country", "business_address_locality"),
        ("country", False): ("business_address_country",),
    }
    assert ba.mandatory_fields("US", "Dallas")[-1] == "business_address_locality"
    assert ba.mandatory_fields("GB", None) == ("business_address_country",)
    assert ba.mandatory_fields("GB", " .. ") == ("business_address_country",)
    assert ba.country_class("US") == "subdivision" and ba.country_class("GB") == "country"
    for country in ("US", "CA", "AU", "NG"):
        assert ba.country_class(country) == "subdivision"


@pytest.mark.parametrize("region", ["Alaska", "Alabama", "AK", "Bavaria"])
def test_a_region_with_no_country_is_never_read_in_the_locations_country(region) -> None:
    assert answer(ANCHORAGE, rule(region=region, locality="Anchorage")) == (
        BUSINESS_ADDRESS_INCOMPLETE
    )


def test_anchorage_with_region_alabama_and_no_country_asks_and_is_never_refused() -> None:
    """Review d1 C-1 (P1): step 2 reads a region only in the rule's own readable country. Read
    in the location's country, "Alabama" would contradict Alaska; it is never so read."""
    got = answer(ANCHORAGE, rule(region="Alabama"))
    assert got == BUSINESS_ADDRESS_INCOMPLETE and got != BUSINESS_ADDRESS_CONFLICT
    assert ba.structured_status(rule(region="Alabama"), "US-AK", "Anchorage") == "incomplete"
    # With the country it reads, the same region is the contradiction it is.
    assert answer(ANCHORAGE, rule(country="US", region="Alabama")) == BUSINESS_ADDRESS_CONFLICT


@pytest.mark.parametrize(
    "fields", [{"country": "Narnia", "region": "Texas", "locality": "Dallas"},
               {"country": "US", "region": "Bavaria", "locality": "Dallas"},
               {"country": "US", "region": "Lagos", "locality": "Dallas"}],
)
def test_unreadable_values_ask(fields) -> None:
    assert answer(DALLAS, rule(**fields)) == BUSINESS_ADDRESS_INCOMPLETE


@pytest.mark.parametrize("code", ["GB", "KE"])
def test_an_unreadable_country_asks_for_a_country_class_location_too(code: str) -> None:
    """With no state or province to read, an unreadable business country (a stored value that
    no longer reads, row 5c) must still ask: it never agrees (mutant MK3)."""
    location, fields = OWN[code]
    unreadable = rule(**{**fields, "country": "Narnia"})
    assert answer(location, unreadable) == BUSINESS_ADDRESS_INCOMPLETE
    assert ba.structured_status(unreadable, code, location.city) == "incomplete"


def test_the_texts_question_comes_before_the_towns() -> None:
    """Step 5 before step 6: an ambiguous text is asked about before a town that differs."""
    assert answer(DALLAS, rule("1500 Broadway Hartford CT", country="US", region="TX",
                               locality="Irving")) == ADDRESS_SUFFIX_AMBIGUOUS


def test_the_location_is_checked_first() -> None:
    for location in (loc("Dallas", None, "US"), loc("X", None, None), loc("X", None, "Narnia"),
                     None):
        assert answer(location, rule(country="Canada")) == LOCATION_UNRESOLVED
    captured = loc("Dallas", "TX", "US", jurisdiction_code="US-OK")
    assert answer(captured, rule(**TX_DALLAS)) == LOCATION_UNRESOLVED


# --------------------------------------------------------------------------- #
# U-10 / U-11 / U-12 / U-16: the town; "established mapping" is fold equality only
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("city", "town"),
    [("St. Louis", "Saint Louis"), ("Sao Paulo", "São Paulo"), ("Ado-Ekiti", "ADO EKITI"),
     ("Dallas", "  dallas "), ("Fort Worth", "Ft. Worth")],
)
def test_fold_equal_towns_agree(city, town) -> None:
    location = loc(city, "TX", "US")
    assert answer(location, rule(country="US", region="TX", locality=town)) == "US-TX"


@pytest.mark.parametrize(
    "town", ["Dallas TX", "Dallas, TX", "NYC", "Irving", "Hartford CT", "US-TX", "Texas"]
)
def test_any_other_town_asks_and_is_never_read_as_a_code(town) -> None:
    assert answer(DALLAS, rule(country="US", region="TX", locality=town)) == (
        BUSINESS_ADDRESS_LOCALITY_UNCONFIRMED
    )


def test_a_town_is_compared_only_when_both_sides_are_present() -> None:
    no_city = loc(None, "TX", "US")
    assert answer(no_city, rule(country="US", region="TX", locality="Irving")) == "US-TX"
    # With a city, a missing town is not "nothing to compare": it is incomplete (item 3).
    assert answer(DALLAS, rule(country="US", region="TX", locality=None)) == (
        BUSINESS_ADDRESS_INCOMPLETE
    )
    assert answer(loc("...", "TX", "US"), rule(country="US", region="TX", locality="Irving")) == (
        "US-TX"
    )


# --------------------------------------------------------------------------- #
# U-14: the confirmation answers the town only, bound to its values
# --------------------------------------------------------------------------- #
def test_a_confirmation_answers_the_town_and_is_labelled_customer_confirmed() -> None:
    r = resolve_for_action(LAGOS, [confirmed("Ikeja", "LAGOS", "NG-LA", country="NG",
                                             region="Lagos")])
    assert r.code == "NG-LA"
    assert "town or locality confirmed by the customer within the location's area" in r.evidence
    assert not any("verif" in line.lower() for line in r.evidence)


def test_the_binding_lapses_when_the_town_the_city_or_the_code_changes() -> None:
    made = {"country": "NG", "region": "Lagos"}
    town_changed = confirmed("Ikeja", "Lagos", "NG-LA", **made)
    town_changed.business_address_locality = "Lekki"
    assert answer(LAGOS, town_changed) == BUSINESS_ADDRESS_LOCALITY_UNCONFIRMED
    city_changed = confirmed("Ikeja", "Lagos", "NG-LA", **made)
    assert answer(loc("Badagry", "Lagos", "Nigeria"), city_changed) == (
        BUSINESS_ADDRESS_LOCALITY_UNCONFIRMED
    )
    # The location and the business both move to Ogun, keeping their names: the code differs.
    code_changed = confirmed("Ikeja", "Lagos", "NG-LA", country="NG", region="Ogun")
    assert answer(loc("Lagos", "Ogun", "Nigeria"), code_changed) == (
        BUSINESS_ADDRESS_LOCALITY_UNCONFIRMED
    )
    withdrawn = confirmed("Ikeja", "Lagos", "NG-LA", **made)
    withdrawn.locality_confirmed_at = None
    assert answer(LAGOS, withdrawn) == BUSINESS_ADDRESS_LOCALITY_UNCONFIRMED


def test_a_confirmation_does_not_answer_anything_but_the_town() -> None:
    incomplete = confirmed("Ikeja", "Lagos", "NG-LA", country="NG")
    assert answer(LAGOS, incomplete) == BUSINESS_ADDRESS_INCOMPLETE
    ambiguous = confirmed("Ikeja", "Lagos", "NG-LA", country="NG", region="Lagos")
    ambiguous.business_address = "1500 Broadway Hartford CT"
    assert answer(loc("Lagos", "Lagos", "Nigeria"), ambiguous) in CLARIFICATIONS
    assert answer(None, confirmed("Ikeja", "Lagos", "NG-LA")) == LOCATION_UNRESOLVED


# --------------------------------------------------------------------------- #
# U-15 / R-8: street and postcode are never read
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("street", "postcode"),
    [("1500 Broadway Hartford CT", None), ("1 Spring Hill St NE", "CT 06103"),
     ("ACME SA, Calle Mayor 1", "28013"), ("Suite 5, US-AL", "AL"), (SENTINEL, SENTINEL)],
)
def test_street_and_postcode_are_never_read(street, postcode) -> None:
    got = answer(DALLAS, rule(**TX_DALLAS, street=street,
                              postcode=postcode))
    assert got == "US-TX"


# --------------------------------------------------------------------------- #
# U-17 / U-18: the free text, a consistency check only
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("complete", [True, False])
def test_an_explicit_contradiction_in_the_text_is_refused(complete) -> None:
    fields = {"country": "US", "region": "TX"} if complete else {}
    assert answer(DALLAS, rule("1 Main St, Toronto, Canada", **fields)) == (
        BUSINESS_ADDRESS_TEXT_CONFLICT
    )
    assert answer(DALLAS, rule("1 Main St, Sacramento, CA", **fields)) == (
        BUSINESS_ADDRESS_TEXT_CONFLICT
    )


def test_an_ambiguous_text_asks_with_the_readers_category_once_the_fields_are_complete() -> None:
    text = "1500 Broadway Hartford CT"
    assert resolve_jurisdiction(DALLAS, [rule(text)]).reason == ADDRESS_SUFFIX_AMBIGUOUS
    assert answer(DALLAS, rule(text)) == BUSINESS_ADDRESS_INCOMPLETE
    assert answer(DALLAS, rule(text, **TX_DALLAS)) == ADDRESS_SUFFIX_AMBIGUOUS
    assert ACTION_CLARIFICATIONS[ADDRESS_SUFFIX_AMBIGUOUS] == (
        CLARIFICATIONS[ADDRESS_SUFFIX_AMBIGUOUS] + TEXT_REMEDY
    )
    # The remedy the sentence names resolves: the text left empty.
    assert answer(DALLAS, rule(None, **TX_DALLAS)) == "US-TX"


def test_an_agreeing_text_is_recorded_and_changes_nothing() -> None:
    r = resolve_for_action(DALLAS, [rule("1 Main St, Dallas, TX", **TX_DALLAS)])
    assert r.code == "US-TX"
    assert "business address text states nothing against it" in r.evidence


# --------------------------------------------------------------------------- #
# U-19: several rules (not creatable through the API)
# --------------------------------------------------------------------------- #
def test_a_known_contradiction_in_a_later_rule_is_never_hidden_by_an_earlier_ambiguity() -> None:
    """Review d1 C-2 (P2): each rule's text is read on its own, so rule 0's suffix ambiguity
    cannot hide rule 1's explicit contradiction; the refusal (step 3) comes first."""
    complete = {"country": "US", "region": "TX"}
    assert answer(DALLAS, rule("1500 Broadway Hartford CT", **complete),
                  rule("1 Main St, Toronto, Canada", **complete)) == BUSINESS_ADDRESS_TEXT_CONFLICT
    assert answer(DALLAS, rule("1 Main St, Toronto, Canada", **complete),
                  rule("1500 Broadway Hartford CT", **complete)) == BUSINESS_ADDRESS_TEXT_CONFLICT


def test_each_step_runs_over_every_rule_before_the_next() -> None:
    """Review d1 C-2 (P3): step-major. Rule 0 is empty (step 4) and rule 1 contradicts (step
    2): the contradiction decides, whatever the rules' order."""
    assert answer(DALLAS, rule(), rule(country="Canada", region="Ontario")) == (
        BUSINESS_ADDRESS_CONFLICT
    )
    assert answer(DALLAS, rule(country="Canada", region="Ontario"), rule()) == (
        BUSINESS_ADDRESS_CONFLICT
    )
    # Within one step, the first failing rule in the caller's order decides.
    assert answer(DALLAS, rule("1500 Broadway Hartford CT", **TX_DALLAS),
                  rule("1 Spring Hill St NE", **TX_DALLAS)) == (
        resolve_jurisdiction(DALLAS, [rule("1500 Broadway Hartford CT")]).reason
    )


def test_every_rule_must_pass_each_step_refusals_first() -> None:
    complete = rule(**TX_DALLAS)
    assert answer(DALLAS, complete, rule()) == BUSINESS_ADDRESS_INCOMPLETE
    assert answer(DALLAS, rule(), rule(country="CA")) == BUSINESS_ADDRESS_CONFLICT
    assert answer(DALLAS, complete, rule("1 Main St, Toronto, Canada", country="US",
                                         region="TX")) == BUSINESS_ADDRESS_TEXT_CONFLICT
    assert answer(DALLAS, complete, rule(country="US", region="TX", locality="Irving")) == (
        BUSINESS_ADDRESS_LOCALITY_UNCONFIRMED
    )
    assert answer(DALLAS, complete, complete) == "US-TX"


# --------------------------------------------------------------------------- #
# U-20: the vocabulary is closed; messages fixed; no customer text anywhere
# --------------------------------------------------------------------------- #
def test_the_vocabulary_is_closed_disjoint_and_fixed() -> None:
    assert set(ADDRESS_CLARIFICATIONS) == {
        LOCATION_UNRESOLVED,
        BUSINESS_ADDRESS_INCOMPLETE,
        BUSINESS_ADDRESS_CONFLICT,
        BUSINESS_ADDRESS_TEXT_CONFLICT,
        BUSINESS_ADDRESS_LOCALITY_UNCONFIRMED,
    }
    assert not set(ADDRESS_CLARIFICATIONS) & set(CLARIFICATIONS)
    assert set(ACTION_CLARIFICATIONS) == set(ADDRESS_CLARIFICATIONS) | set(CLARIFICATIONS)
    assert REFUSALS == {BUSINESS_ADDRESS_CONFLICT, BUSINESS_ADDRESS_TEXT_CONFLICT}
    for message in ACTION_CLARIFICATIONS.values():
        assert "{" not in message and "%" not in message and message.endswith(".")
        assert "verified" not in message
    for category in CLARIFICATIONS:
        assert ACTION_CLARIFICATIONS[category].endswith(TEXT_REMEDY)


def test_the_text_refusal_is_worded_as_a_reading() -> None:
    """Review d1 C-7: the reader also refuses own addresses through its own defects (552 own rows
    of the extended P-CN: US-DC read as US-WA, NG-NI as the country Niger), so the message says
    what was read, not what the text is."""
    assert ADDRESS_CLARIFICATIONS[BUSINESS_ADDRESS_TEXT_CONFLICT] == (
        "We read the Business address text as naming a different country, or state or province, "
        "from this location's. Correct that text on the location's Service area tab, or leave it "
        "empty and use the business address fields. This cannot be confirmed instead of "
        "corrected."
    )


def test_the_web_copy_mirrors_these_categories_and_messages() -> None:
    """The web's static copy (``apps/web/src/lib/jurisdictionClarifications.ts``), which the brief
    panel shows for ``error.details.reason``, holds exactly these five lines and the remedy."""
    mirror = (APP.parents[1] / "web/src/lib/jurisdictionClarifications.ts").read_text()
    for category, message in ADDRESS_CLARIFICATIONS.items():
        assert f'  {category}: "{message}",\n' in mirror, category
    assert mirror.count('",\n') == len(ADDRESS_CLARIFICATIONS)
    assert f'export const TEXT_REMEDY = "{TEXT_REMEDY}";' in mirror


def test_the_design_messages_verbatim() -> None:
    assert ADDRESS_CLARIFICATIONS[BUSINESS_ADDRESS_INCOMPLETE] == (
        "This location's business address is incomplete. Open the location and, on its Service "
        "area tab, enter the business country; for a business in the United States, Canada, "
        "Australia or Nigeria, the business state or province; and, when the location has a "
        "city, the business town or locality: the town, locality or named place used in the "
        "business's postal address. If the location's own city is wrong, correct it on its "
        "Details tab instead. Then try again."
    )
    assert ADDRESS_CLARIFICATIONS[BUSINESS_ADDRESS_CONFLICT].endswith(
        "This cannot be confirmed instead of corrected."
    )
    assert ADDRESS_CLARIFICATIONS[BUSINESS_ADDRESS_LOCALITY_UNCONFIRMED].startswith(
        "The business town or locality is not this location's city."
    )
    assert TEXT_REMEDY == (
        " The business address fields are complete, so you can instead leave the Business "
        "address text empty."
    )


VALUES = [None, "", SENTINEL, "US", "Texas", "Canada", "Alaska", "Dallas", "Lagos"]


def test_every_reason_is_a_key_and_no_sentinel_reaches_a_message_or_an_added_line() -> None:
    location = loc(f"Dallas {SENTINEL}", "TX", "US")
    base = set(resolve_jurisdiction(location).evidence)
    seen: set[str] = set()
    for text, country, region, locality in itertools.product(
        [None, f"1 Main St {SENTINEL}", f"{SENTINEL}, Toronto, Canada"], VALUES, VALUES, VALUES
    ):
        r = resolve_for_action(
            location, [rule(text, country=country, region=region, locality=locality,
                            street=SENTINEL, postcode=SENTINEL)]
        )
        if not r.is_resolved:
            assert r.reason in ACTION_CLARIFICATIONS
            seen.add(r.reason)
            assert SENTINEL not in ACTION_CLARIFICATIONS[r.reason]
        for line in set(r.evidence) - base:
            assert SENTINEL not in line, line
    assert {BUSINESS_ADDRESS_INCOMPLETE, BUSINESS_ADDRESS_CONFLICT,
            BUSINESS_ADDRESS_TEXT_CONFLICT, BUSINESS_ADDRESS_LOCALITY_UNCONFIRMED} <= seen


# --------------------------------------------------------------------------- #
# U-23: every category's remedy, applied, resolves
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("before", "after", "category"),
    [
        ((loc("Dallas", None, "US"), rule(**TX_DALLAS)),
         (DALLAS, rule(**TX_DALLAS)), LOCATION_UNRESOLVED),
        ((DALLAS, rule(country="US")), (DALLAS, rule(**TX_DALLAS)),
         BUSINESS_ADDRESS_INCOMPLETE),
        ((DALLAS, rule(country="US", region="OK")), (DALLAS, rule(**TX_DALLAS)),
         BUSINESS_ADDRESS_CONFLICT),
        ((DALLAS, rule("Toronto, Canada", **TX_DALLAS)),
         (DALLAS, rule(None, **TX_DALLAS)), BUSINESS_ADDRESS_TEXT_CONFLICT),
        ((DALLAS, rule("1500 Broadway Hartford CT", **TX_DALLAS)),
         (DALLAS, rule("1500 Broadway, Dallas, TX", **TX_DALLAS)),
         ADDRESS_SUFFIX_AMBIGUOUS),
        ((DALLAS, rule(country="US", region="TX", locality="Irving")),
         (DALLAS, confirmed("Irving", "Dallas", "US-TX", country="US", region="TX")),
         BUSINESS_ADDRESS_LOCALITY_UNCONFIRMED),
        ((DALLAS, rule(country="US", region="TX", locality="Irving")),
         (DALLAS, rule(**TX_DALLAS)),
         BUSINESS_ADDRESS_LOCALITY_UNCONFIRMED),
    ],
)
def test_each_remedy_resolves(before, after, category) -> None:
    assert answer(*before) == category
    assert answer(*after) == "US-TX"


def test_the_unresolved_answer_is_never_a_stored_code() -> None:
    r = resolve_for_action(DALLAS, [rule()])
    assert r.code == UNRESOLVED and r.codes == () and r.confidence == 0.0


# --------------------------------------------------------------------------- #
# G0: the product callers of the resolver and of the action check
# --------------------------------------------------------------------------- #
def product_callers(name: str) -> set[str]:
    """The product modules (outside tests) that call ``name``, by plain name or attribute."""
    callers = set()
    for path in APP.rglob("*.py"):
        if "tests" in path.parts:
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                called = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                if called == name:
                    callers.add(path.relative_to(APP).as_posix())
    return callers


def test_only_the_brief_capture_calls_the_action_check_and_only_it_calls_the_resolver() -> None:
    """Register gate G0 attaches to ``resolve_jurisdiction``'s first caller outside tests: #313's
    brief capture (W21 ADDR item 9, review r1 BC-4; COORDINATOR-RULINGS-ADDR-r1: the guard names
    :func:`resolve_for_action` as the only function ``briefs/service.py`` may call). So the
    capture calls the action check and never the resolver, and the resolver is called by the
    action check's module alone. A new caller of either fails here until it is reviewed."""
    assert product_callers("resolve_for_action") == {"briefs/service.py"}
    assert product_callers("resolve_jurisdiction") == {"jurisdiction/business_address.py"}
