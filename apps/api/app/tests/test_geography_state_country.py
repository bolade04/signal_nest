"""GEO-P5: a state's name followed by a country in the same address part (geography register RI-2).

The operator's instruction of 2026-11-14: "Implement the minimum correction that reads this explicit
geographic evidence and applies the existing contradiction rules", preserving: "Clear contradictions
are refused. Unresolved ambiguity receives actionable clarification. The selected location's city or
state cannot supply missing proof that an address matches. Customer confirmation cannot override a
detected contradiction. Do not introduce a city table or waive ambiguity. Preserve the RI-1
Territory behavior and the structured-address protections."

So a part that is a state's name, with or without "State", followed by a country's name or code
("Alabama State United States", "Alabama United States", "Lagos State Nigeria", "Alabama State
US") is read as two parts, the state and then the country, as if a comma stood between them: each
such address answers exactly as its twin with the comma does. A two-letter word counts as the
country only when it is the code of the state's own country (and, for "CA", which is also
California's code, only after a name written with "State", "Province" or "Region"); any other
two-letter word is read as before, since it may be a state's code after a town of that name
("Nevada MO", "Abia AD", "Ontario CA"), a legal form or an abbreviation ("Texas Co", "Victoria SA").

Boundaries: the brief capture (``POST .../opportunities/{id}/briefs``) refuses such an address with
the existing contract before any model call or budget use, and nothing is written; no creative
generation can start from it.

These tests pin the measured classes; they do not show that arbitrary addresses are validated.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from types import SimpleNamespace

import pytest

from app.briefs import service as brief_service
from app.briefs.models import RecommendationBrief
from app.core.config import get_settings
from app.creative.models import CreativeDraft
from app.geography import markets
from app.geography.markets import PLACE_NAME, read_business_address
from app.intelligence.records import SignalIntelligenceRecord
from app.jurisdiction.business_address import (
    ACTION_CLARIFICATIONS,
    BUSINESS_ADDRESS_CONFLICT,
    BUSINESS_ADDRESS_TEXT_CONFLICT,
    TEXT_REMEDY,
    resolve_for_action,
)
from app.jurisdiction.codes import UNRESOLVED
from app.jurisdiction.resolver import (
    ADDRESS_DIRECTION_AMBIGUOUS,
    ADDRESS_NAMES_ANOTHER_PLACE,
    ADDRESS_PLACE_UNCLEAR,
    ADDRESS_ROUTE_AMBIGUOUS,
    ADDRESS_SUFFIX_AMBIGUOUS,
    ADDRESS_TERRITORY_UNCLEAR,
    CLARIFICATIONS,
    resolve_jurisdiction,
    subdivision_code_of,
)
from app.llm import service as service_module
from app.llm.models import LLMUsageEvent
from app.opportunities.models import Opportunity
from app.organizations.models import User
from app.tests._creative_support import (
    CEILINGS,
    CreativeWorld,
    creative_world,
    postgres_creative_world,
)
from app.tests.test_jurisdiction_failclosed import (
    _CountingProvider,
    _enqueue_generation,
    _full_ledger,
    _jobs_for,
    _ledger,
    _logged,
    _priced_llm,
    capture,
    evidence,
)


def _location(city, state, country) -> SimpleNamespace:
    return SimpleNamespace(city=city, state_province=state, country=country)


def _resolve(location, address):
    return resolve_jurisdiction(location, [SimpleNamespace(business_address=address)])


def _refused(location, address, evidence_end):
    result = _resolve(location, address)
    assert result.code == UNRESOLVED, (address, result)
    assert result.reason is None, (address, result)
    assert result.evidence[-1].endswith(evidence_end), (address, result.evidence)


def _asks(location, address, reason):
    result = _resolve(location, address)
    assert result.code == UNRESOLVED, (address, result)
    assert result.reason == reason, (address, result)


def _resolves(location, address, code):
    result = _resolve(location, address)
    assert result.code == code, (address, result)


def _answer(location, address) -> tuple[str, str | None, str]:
    """The outcome as the resolver words it, without the address it quotes."""
    result = _resolve(location, address)
    return result.code, result.reason, result.evidence[-1].replace(repr(address), "<address>")


ANCHORAGE = _location("Anchorage", "Alaska", "United States")
MONTGOMERY = _location("Montgomery", "Alabama", "United States")
DALLAS = _location("Dallas", "Texas", "United States")
ATLANTA = _location("Atlanta", "Georgia", "United States")
NEVADA_MO = _location("Nevada", "Missouri", "United States")
ONTARIO_CA = _location("Ontario", "California", "United States")
IKEJA = _location("Ikeja", "Lagos", "Nigeria")
UMUAHIA = _location("Umuahia", "Abia", "Nigeria")
YOLA = _location("Yola", "Adamawa", "Nigeria")
ABUJA = _location("Abuja", "FCT", "Nigeria")
TORONTO = _location("Toronto", "Ontario", "Canada")
VANCOUVER = _location("Vancouver", "British Columbia", "Canada")
DARWIN = _location("Darwin", "NT", "Australia")
SYDNEY = _location("Sydney", "NSW", "Australia")
PERTH = _location("Perth", "Western Australia", "Australia")
LONDON = _location("London", None, "United Kingdom")
NAIROBI = _location("Nairobi", None, "Kenya")
MADRID = _location("Madrid", None, "Spain")


# --------------------------------------------------------------------------- #
# The register's examples (§13, RI-2) and the design target
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "address",
    [
        "Alabama State United States, 1 Main St, United States",
        "Alabama United States, 1 Main St, United States",
    ],
)
def test_ri2_the_registers_examples_are_refused_for_anchorage(address):
    # Accepted on current main 6a3afe3a and since before D1.
    _refused(ANCHORAGE, address, "states subdivision US-AL")


def test_ri2_a_state_with_its_code_before_another_countrys_address_asks_as_its_comma_twin():
    # "Alabama State US, 1 Main St, Kenya" for Nairobi was accepted on current main. The address
    # also names the location's own country, so the existing rule (GEO-P1) reads "Alabama State"
    # and "US" as names that may be a business's: it asks, exactly as the twin with the comma has
    # always done. Without "Kenya" it is refused.
    address = "Alabama State US, 1 Main St, Kenya"
    _asks(NAIROBI, address, ADDRESS_NAMES_ANOTHER_PLACE)
    assert (
        _answer(NAIROBI, address)[:2] == _answer(NAIROBI, "Alabama State, US, 1 Main St, Kenya")[:2]
    )
    _refused(NAIROBI, "Alabama State US, 1 Main St", "states country US")


def test_ri2_the_comma_form_is_unchanged():
    _resolves(MONTGOMERY, "1 Main St, Montgomery, Alabama State, United States", "US-AL")
    _refused(
        ANCHORAGE, "1 Main St, Montgomery, Alabama State, United States", "states subdivision US-AL"
    )


# --------------------------------------------------------------------------- #
# Each form answers as its twin with a comma (the existing rules apply)
# --------------------------------------------------------------------------- #
#: (one part as written, the same part with a comma between the state and the country)
FORMS = [
    ("Alabama State United States", "Alabama State, United States"),
    ("Alabama United States", "Alabama, United States"),
    ("Alabama State US", "Alabama State, US"),
    ("Alabama US", "Alabama, US"),
    ("Alabama State USA", "Alabama State, USA"),
    ("State of Alabama United States", "State of Alabama, United States"),
    ("Alabama State Kenya", "Alabama State, Kenya"),
    ("Alabama 36104 United States", "Alabama 36104, United States"),
    ("Alabama United States 36104", "Alabama, United States 36104"),
    ("TX United States", "TX, United States"),
    ("Lagos State Nigeria", "Lagos State, Nigeria"),
    ("Lagos State NG", "Lagos State, NG"),
    ("Federal Capital Territory Nigeria", "Federal Capital Territory, Nigeria"),
    ("Ontario Canada", "Ontario, Canada"),
    ("Ontario Province CA", "Ontario Province, CA"),
    ("ON Canada", "ON, Canada"),
    ("Northern Territory Australia", "Northern Territory, Australia"),
    ("Western Australia Australia", "Western Australia, Australia"),
    ("England United Kingdom", "England, United Kingdom"),
]
#: (shape, with {p} the part and {s} the separator)
SHAPES = [
    "{p}{s}1 Main St{s}{c}",
    "1 Main St{s}{p}{s}{c}",
    "1 Main St{s}Springfield{s}{p}",
    "Suite 5{s}1 Main St{s}Springfield{s}{p}{s}{c}",
    "{p}",
]
SEPARATORS = [", ", "; ", "\n", " | "]
LOCATIONS = [MONTGOMERY, ANCHORAGE, IKEJA, UMUAHIA, TORONTO, VANCOUVER, DARWIN, SYDNEY, LONDON,
             NAIROBI]  # fmt: skip


@pytest.mark.parametrize(("part", "twin"), FORMS, ids=[f[0] for f in FORMS])
@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.parametrize("sep", SEPARATORS, ids=repr)
def test_ri2_each_form_answers_as_its_twin_with_a_comma(part, twin, shape, sep):
    for location in LOCATIONS:
        written = shape.format(p=part, s=sep, c=location.country)
        separated = shape.format(p=twin.replace(", ", sep), s=sep, c=location.country)
        assert _answer(location, written) == _answer(location, separated), (location, written)


@pytest.mark.parametrize(
    ("location", "address", "code"),
    [
        (MONTGOMERY, "Alabama State United States, 1 Main St, United States", "US-AL"),
        (MONTGOMERY, "1 Main St, Montgomery, Alabama United States", "US-AL"),
        (MONTGOMERY, "1 Main St, Montgomery, Alabama 36104 United States", "US-AL"),
        (MONTGOMERY, "1 Main St, State of Alabama USA", "US-AL"),
        (MONTGOMERY, "Alabama State US, 1 Main St, United States", "US-AL"),
        (IKEJA, "1 Allen Ave, Ikeja, Lagos State Nigeria", "NG-LA"),
        (ABUJA, "1 Main St, Abuja, Federal Capital Territory Nigeria", "NG-FC"),
        (TORONTO, "1 Main St, Toronto, Ontario Canada", "CA-ON"),
        (TORONTO, "1 Main St, Toronto, ON Canada", "CA-ON"),
        (DARWIN, "1 Main St, Darwin, Northern Territory Australia", "AU-NT"),
        (PERTH, "1 Main St, Perth, Western Australia Australia", "AU-WA"),
        (PERTH, "1 Main St, Perth, Western Australia", "AU-WA"),
        (SYDNEY, "1 Main St, Sydney, New South Wales", "AU-NSW"),
        (LONDON, "1 Main St, London, England United Kingdom", "GB"),
        (DALLAS, "1 Main St, Dallas, TX United States", "US-TX"),
    ],
)
def test_ri2_own_addresses_resolve(location, address, code):
    _resolves(location, address, code)


@pytest.mark.parametrize(
    ("location", "address", "evidence_end"),
    [
        (ANCHORAGE, "1 Main St, Montgomery, Alabama State United States", "subdivision US-AL"),
        (ANCHORAGE, "Alabama State United States; 1 Main St; United States", "subdivision US-AL"),
        (ANCHORAGE, "Alabama State United States\n1 Main St\nUnited States", "subdivision US-AL"),
        (ANCHORAGE, "1 Main St, Montgomery, Alabama United States 36104", "subdivision US-AL"),
        (ANCHORAGE, "1 Main St, State of Alabama United States", "subdivision US-AL"),
        (ANCHORAGE, "1 Main St, Dallas, TX United States", "subdivision US-TX"),
        (UMUAHIA, "1 Allen Ave, Ikeja, Lagos State Nigeria", "subdivision NG-LA"),
        (VANCOUVER, "1 Main St, Toronto, Ontario Canada", "subdivision CA-ON"),
        (VANCOUVER, "1 Main St, Toronto, ON Canada", "subdivision CA-ON"),
        (SYDNEY, "1 Main St, Darwin, Northern Territory Australia", "subdivision AU-NT"),
        (SYDNEY, "1 Main St, Perth, Western Australia Australia", "subdivision AU-WA"),
        (NAIROBI, "1 Main St, London, England United Kingdom", "country GB"),
        # A state followed by a country that contradicts it (design target point 3).
        (ANCHORAGE, "Alabama State Kenya, 1 Main St, United States", "subdivision US-AL"),
        (MONTGOMERY, "1 Main St, Montgomery, Alabama State Kenya", "country KE"),
        # "<Name> State" of a name that is no listed state: the country it writes is read.
        (LONDON, "1 Main St, Kenya State Kenya", "country KE"),
        (NAIROBI, "1 Main St, Dakota State United States", "country US"),
    ],
)
def test_ri2_contradictions_are_refused(location, address, evidence_end):
    # Each was accepted on current main 6a3afe3a.
    _refused(location, address, evidence_end)


def test_ri2_the_part_is_read_as_the_state_and_then_the_country():
    reading = read_business_address("Alabama State United States, 1 Main St, United States")
    named = [(p.kind, p.text, {(r.country, r.region) for r in p.readings}) for p in reading.places]
    assert (PLACE_NAME, "alabama state", {("US", "US-AL")}) in named
    assert sum(1 for _kind, _text, readings in named if readings == {("US", None)}) == 2


# --------------------------------------------------------------------------- #
# Two-letter words
# --------------------------------------------------------------------------- #
WASHINGTON_DC = _location("Washington", "District of Columbia", "United States")


@pytest.mark.parametrize(
    "address", ["1 Main St, Washington United States", "1 Main St, Washington USA 20001"]
)
def test_ri2_a_town_named_like_a_state_asks_as_its_comma_twin(address):
    # G-2 and G-11 (rulings P3-R-15, P3-R-16) and the DC "Let it ask": "Washington" may be the town
    # or Washington State. The part is read as two parts on both sides of the town-part pairing,
    # so the address asks, as "1 Main St, Washington, United States" does.
    twin = address.replace("Washington ", "Washington, ")
    _asks(WASHINGTON_DC, address, ADDRESS_PLACE_UNCLEAR)
    assert _answer(WASHINGTON_DC, address)[:2] == _answer(WASHINGTON_DC, twin)[:2]
    _resolves(WASHINGTON_DC, "1 Main St, Washington DC 20001, United States", "US-DC")


@pytest.mark.parametrize(
    ("location", "address", "evidence_end"),
    [
        (ANCHORAGE, "Alabama State US, 1 Main St, United States", "subdivision US-AL"),
        (ANCHORAGE, "1 Main St, Montgomery, AL US", "subdivision US-AL"),
        (ANCHORAGE, "1 Main St, Montgomery, AL USA", "subdivision US-AL"),
        (ANCHORAGE, "1 Main St, Montgomery, Alabama US", "subdivision US-AL"),
        (UMUAHIA, "1 Allen Ave, Ikeja, Lagos State NG", "subdivision NG-LA"),
        (VANCOUVER, "1 Main St, Toronto, Ontario Province CA", "subdivision CA-ON"),
    ],
)
def test_ri2_the_code_of_the_states_own_country_is_read(location, address, evidence_end):
    _refused(location, address, evidence_end)


@pytest.mark.parametrize(
    ("location", "address", "code"),
    [
        # A town and its state's code, the form the clarification messages ask for, whose code
        # is also another country's: Macao, Andorra, Saint Kitts and Nevis, Canada.
        (NEVADA_MO, "Nevada MO, Vernon County, MO, USA", "US-MO"),
        (NEVADA_MO, "1 Main St, Nevada MO", "US-MO"),
        (YOLA, "1 Main St, Abia AD, Nigeria", "NG-AD"),
        (ONTARIO_CA, "1 Main St, Ontario CA 91761", "US-CA"),
        (TORONTO, "1 Main St, Toronto, Ontario CA", "CA-ON"),
        # A business's name: a state's name and a legal form or an abbreviation.
        (DALLAS, "Texas Co, 1 Main St, Dallas, TX", "US-TX"),
        (MADRID, "Victoria SA, Calle Mayor 1, 28013 Madrid, Spain", "ES"),
        (ATLANTA, "Georgia TV, 1 Main St, Atlanta, GA", "US-GA"),
        (MONTGOMERY, "Alabama AL, 1 Main St, Montgomery", "US-AL"),
        (ANCHORAGE, "Alabama AL, 1 Main St, Anchorage, AK", "US-AK"),
    ],
)
def test_ri2_another_two_letter_word_is_read_as_before(location, address, code):
    # Each resolves on current main too; reading the word as a country would refuse or ask.
    _resolves(location, address, code)


@pytest.mark.parametrize(
    ("part", "read_as"),
    [
        ("Alabama State United States", "Alabama State, United States"),
        ("Alabama 36104 United States", "Alabama 36104, United States"),
        ("Lagos State NG", "Lagos State, NG"),
        ("Ontario Province CA", "Ontario Province, CA"),
        ("State of New Jersey USA", "State of New Jersey, USA"),
        ("Western Australia Australia", "Western Australia, Australia"),
        ("New South Wales 2000 Australia", "New South Wales 2000, Australia"),
        ("Dakota State United States", "Dakota State, United States"),
        ("Kenya State Kenya", "Kenya State, Kenya"),
        ("AL US", "AL, US"),
        ("AL United States", "AL, United States"),
        # Read as before: a code form with a hyphen or a listed code with a space, a code form of
        # another country (W21-REV-D1STREET C-12), a part that is as a whole a state's or a
        # country's name, a town and a code, a two-letter word that is not the state's country's
        # code, a territory (GEO-P4).
        ("CA NL", "CA NL"),
        ("DE BY", "DE BY"),
        ("US-AL United States", "US-AL United States"),
        ("GA-400 United States", "GA-400 United States"),
        ("Western Australia", "Western Australia"),
        ("State of Western Australia", "State of Western Australia"),
        ("State of New Jersey", "State of New Jersey"),
        ("Province of New Jersey", "Province of New Jersey"),
        ("Papua New Guinea", "Papua New Guinea"),
        ("Dallas TX United States", "Dallas TX United States"),
        ("Nevada MO", "Nevada MO"),
        ("Ontario CA", "Ontario CA"),
        ("Texas Co", "Texas Co"),
        ("Dakota State US", "Dakota State US"),
        ("Empire State AG", "Empire State AG"),
        ("Gauteng Province South Africa", "Gauteng Province South Africa"),
        ("NE 45th St", "NE 45th St"),
        ("Alabama Territory USA", "Alabama Territory USA"),
    ],
)
def test_ri2_which_parts_are_split(part, read_as):
    assert markets._with_state_country_parts(f"1 Main St; {part}") == f"1 Main St; {read_as}"


# --------------------------------------------------------------------------- #
# What stays as before: names, territories, the city field, the categories, the state fields
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "part", ["Western Australia", "South Australia", "New South Wales", "Northern Ireland",
             "New Jersey", "New Mexico"],
)  # fmt: skip
def test_ri2_a_name_that_ends_in_a_country_word_stays_whole(part):
    reading = read_business_address(f"1 Main St, Springfield, {part}")
    texts = [p.text for p in reading.places]
    assert part.lower() in texts, texts


@pytest.mark.parametrize(
    ("location", "address"),
    [
        (ANCHORAGE, "1 Main St, Montgomery, Alabama Territory USA"),
        (MONTGOMERY, "1 Main St, Montgomery, Alabama Territory USA"),
        (ANCHORAGE, "Alabama Territory United States, 1 Main St, United States"),
        (IKEJA, "1 Allen Ave, Ikeja, Lagos Territory Nigeria"),
        (DALLAS, "Lost Territory Co, 1 Main St, Dallas, TX"),
    ],
)
def test_ri1_territory_forms_still_ask(location, address):
    # GEO-P4 (#321, register RI-1 and RI-1a) is unchanged: a territory the tables do not list asks.
    _asks(location, address, ADDRESS_TERRITORY_UNCLEAR)


@pytest.mark.parametrize(
    ("location", "address"),
    [
        (ANCHORAGE, "Alabama State United States, 1 Main St, United States"),
        (MONTGOMERY, "Alabama State United States, 1 Main St, United States"),
        (NAIROBI, "Alabama State US, 1 Main St, Kenya"),
        (VANCOUVER, "1 Main St, Toronto, Ontario Canada"),
    ],
)
@pytest.mark.parametrize("city", ["Montgomery", "Toronto", "Alabama", "Zzyzx", None])
def test_ri2_the_city_field_never_settles_it(location, address, city):
    # G-11 (ruling P3-R-16): the location's city takes no part.
    neutral = _answer(location, address)
    assert _answer(_location(city, location.state_province, location.country), address) == neutral


def test_ri2_adds_no_category_and_no_message():
    assert set(CLARIFICATIONS) == {
        ADDRESS_SUFFIX_AMBIGUOUS,
        ADDRESS_NAMES_ANOTHER_PLACE,
        ADDRESS_PLACE_UNCLEAR,
        ADDRESS_DIRECTION_AMBIGUOUS,
        ADDRESS_ROUTE_AMBIGUOUS,
        ADDRESS_TERRITORY_UNCLEAR,
    }


@pytest.mark.parametrize(
    ("country", "text", "code"),
    [
        ("US", "Alabama State", "US-AL"),
        ("US", "Alabama", "US-AL"),
        ("US", "Alabama Territory", None),
        ("CA", "Ontario Province", "CA-ON"),
        ("NG", "Lagos State", "NG-LA"),
    ],
)
def test_ri2_the_state_fields_read_as_before(country, text, code):
    # The structured state and the location's own state are read by subdivision_code_of, which
    # GEO-P5 does not change (GEO-P4's rule for "<Name> Territory" stands).
    assert subdivision_code_of(country, text) == code


# --------------------------------------------------------------------------- #
# ADDR: the check before a jurisdiction-dependent action
# --------------------------------------------------------------------------- #
def _rule(location, text, **fields):
    return SimpleNamespace(
        business_address=text,
        business_address_country=location.country,
        business_address_region=location.state_province,
        business_address_locality=location.city,
        **fields,
    )


def test_addr_a_contradiction_in_the_text_is_a_step_3_refusal():
    result = resolve_for_action(
        ANCHORAGE, [_rule(ANCHORAGE, "Alabama State United States, 1 Main St, United States")]
    )
    assert (result.code, result.reason) == (UNRESOLVED, BUSINESS_ADDRESS_TEXT_CONFLICT)


def test_addr_a_confirmation_does_not_override_the_contradiction():
    confirmed = {
        "business_address_locality": "Eagle River",
        "locality_confirmed_at": "2026-11-14T00:00:00Z",
        "locality_confirmed_locality": "Eagle River",
        "locality_confirmed_city": "Anchorage",
        "locality_confirmed_jurisdiction": "US-AK",
    }
    rule = SimpleNamespace(**{**vars(_rule(ANCHORAGE, None)), **confirmed})
    assert resolve_for_action(ANCHORAGE, [rule]).code == "US-AK"  # the confirmation applies
    rule.business_address = "1 Main St, Montgomery, Alabama United States"
    result = resolve_for_action(ANCHORAGE, [rule])
    assert (result.code, result.reason) == (UNRESOLVED, BUSINESS_ADDRESS_TEXT_CONFLICT)


def test_addr_an_ask_in_the_text_comes_at_step_5_and_the_fields_remedy_it():
    text = "Alabama State US, 1 Main St, Kenya"
    result = resolve_for_action(NAIROBI, [_rule(NAIROBI, text)])
    assert (result.code, result.reason) == (UNRESOLVED, ADDRESS_NAMES_ANOTHER_PLACE)
    assert ACTION_CLARIFICATIONS[result.reason].endswith(TEXT_REMEDY)
    # Following the remedy: the structured fields are complete, so the text may be left empty.
    assert resolve_for_action(NAIROBI, [_rule(NAIROBI, None)]).code == "KE"


def test_addr_an_own_address_resolves_and_the_structured_fields_still_decide_first():
    text = "Alabama State United States, 1 Main St, United States"
    assert resolve_for_action(MONTGOMERY, [_rule(MONTGOMERY, text)]).code == "US-AL"
    wrong_state = _rule(MONTGOMERY, text)
    wrong_state.business_address_region = "Alaska"
    result = resolve_for_action(MONTGOMERY, [wrong_state])
    assert (result.code, result.reason) == (UNRESOLVED, BUSINESS_ADDRESS_CONFLICT)


# --------------------------------------------------------------------------- #
# The route and job boundaries (#313's capture, 5C generation), SQLite and PostgreSQL
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module", params=["sqlite", "postgresql"])
def ri2_world(request, tmp_path_factory) -> Iterator[CreativeWorld]:
    if request.param == "sqlite":
        db_file = tmp_path_factory.mktemp("geo-p5") / "api.db"
        with creative_world(db_file, "geop5") as world:
            yield world
        return
    with postgres_creative_world("geop5") as world:  # pragma: no cover - gated on PostgreSQL
        yield world


@pytest.fixture
def briefs_on(monkeypatch) -> None:
    """The brief route's global flag and every generation ceiling, for this test only."""
    monkeypatch.setattr(get_settings(), "recommendation_briefs_enabled", True)
    for name, value in {**CEILINGS, "llm_max_calls_per_workspace_per_day": 1000}.items():
        monkeypatch.setattr(get_settings(), name, value)


@pytest.fixture
def model(monkeypatch) -> _CountingProvider:
    spy = _CountingProvider(service_module.llm_service._provider)
    monkeypatch.setattr(service_module.llm_service, "_provider", spy)
    return spy


@pytest.fixture
def budget_checks(monkeypatch) -> list[str]:
    calls: list[str] = []
    real_check, real_meter = brief_service.ensure_run_allowed, brief_service._meter

    def check(*args, **kwargs):
        calls.append("ensure_run_allowed")
        return real_check(*args, **kwargs)

    def meter(*args, **kwargs):
        calls.append("meter")
        return real_meter(*args, **kwargs)

    monkeypatch.setattr(brief_service, "ensure_run_allowed", check)
    monkeypatch.setattr(brief_service, "_meter", meter)
    return calls


ANCHORAGE_FIELDS = {"city": "Anchorage", "state_province": "AK", "country": "US"}
MONTGOMERY_FIELDS = {"city": "Montgomery", "state_province": "AL", "country": "US"}
NAIROBI_FIELDS = {"city": "Nairobi", "state_province": None, "country": "Kenya"}
#: (case, location fields, the coverage rule's text, the capture's category). Each rule also holds
#: the location's complete, agreeing structured address (``evidence``'s default), so the text
#: decides.
ROUTE_ROWS = [
    ("register example, Anchorage", ANCHORAGE_FIELDS,
     "Alabama State United States, 1 Main St, United States", BUSINESS_ADDRESS_TEXT_CONFLICT),
    ("no State, Anchorage", ANCHORAGE_FIELDS, "1 Main St, Montgomery, Alabama United States",
     BUSINESS_ADDRESS_TEXT_CONFLICT),
    ("own state, another country", MONTGOMERY_FIELDS, "1 Main St, Montgomery, Alabama State Kenya",
     BUSINESS_ADDRESS_TEXT_CONFLICT),
    ("register example, Nairobi", NAIROBI_FIELDS, "Alabama State US, 1 Main St, Kenya",
     ADDRESS_NAMES_ANOTHER_PLACE),
]  # fmt: skip


@pytest.mark.parametrize(
    ("case", "location", "text", "category"), ROUTE_ROWS, ids=[r[0] for r in ROUTE_ROWS]
)
def test_route_an_ri2_address_refuses_the_brief_before_any_model_or_budget_use(
    ri2_world, briefs_on, model, budget_checks, caplog, case, location, text, category
):
    world = ri2_world
    ws = world.workspace(f"ri2 {case}")
    opp, record, _ = evidence(world, ws, location=location, addresses=(text,))
    before = _full_ledger(world, ws)
    with caplog.at_level(logging.INFO):
        r = capture(world, ws, opp, record)
    assert r.status_code == 422, r.text
    error = r.json()["error"]
    assert error == {
        "code": "jurisdiction_unresolved",
        "message": ACTION_CLARIFICATIONS[category],
        "details": {"reason": category},
        "request_id": error["request_id"],
    }
    assert model.requests == [] and budget_checks == []
    assert world.count(RecommendationBrief, opportunity_id=opp) == 0
    assert _ledger(world, ws) == (0, 0, 0)
    assert _full_ledger(world, ws) == before
    logged = _logged(caplog)
    for fragment in ("Alabama", "Main St", "unresolved:"):
        assert fragment not in r.text and fragment not in logged, fragment


def test_route_an_own_ri2_address_is_captured(ri2_world, briefs_on, model, budget_checks):
    """The control: the same form for the location's own state is captured, then budgeted and
    called."""
    world = ri2_world
    ws = world.workspace("ri2 own")
    text = "Alabama State United States, 1 Main St, United States"
    opp, record, _ = evidence(world, ws, location=MONTGOMERY_FIELDS, addresses=(text,))
    r = capture(world, ws, opp, record)
    assert r.status_code == 201, r.text
    assert world.row(RecommendationBrief, r.json()["id"])["jurisdiction_code"] == "US-AL"
    assert len(model.requests) == 1 and "ensure_run_allowed" in budget_checks


def test_job_a_refused_ri2_capture_admits_no_spending_where_a_resolved_one_does(ri2_world):
    """On the priced path (the real provider adapter over a mock transport) the refused capture
    sends nothing and writes no usage row, reservation or budget period; the own address's capture
    reserves before its call on PostgreSQL, where the shared ledger lives."""
    world = ri2_world
    ws = world.workspace("ri2 priced")
    llm, settings, sent = _priced_llm()
    text = "Alabama State United States, 1 Main St, United States"
    refused_opp, refused_record, _ = evidence(
        world, ws, location=ANCHORAGE_FIELDS, addresses=(text,)
    )
    own_opp, own_record, _ = evidence(world, ws, location=MONTGOMERY_FIELDS, addresses=(text,))

    def run(opportunity_id: str, record_id: str):
        with world.session() as s:
            return brief_service.create_brief(
                s,
                opportunity=s.get(Opportunity, opportunity_id),
                intelligence_record=s.get(SignalIntelligenceRecord, record_id),
                campaign_id=None,
                author=s.get(User, world.users["marketer"]),
                llm=llm,
                settings=settings,
            ).id

    with pytest.raises(brief_service.BriefJurisdictionUnresolvedError):
        run(refused_opp, refused_record)
    assert sent == [] and _ledger(world, ws) == (0, 0, 0)
    assert world.count(RecommendationBrief, opportunity_id=refused_opp) == 0
    brief_id = run(own_opp, own_record)
    assert len(sent) == 1
    assert world.row(RecommendationBrief, brief_id)["jurisdiction_code"] == "US-AL"
    usage, reservations, _periods = _ledger(world, ws)
    assert usage == 1
    if world.engine.dialect.name == "postgresql":
        assert reservations == 1  # the reservation path is live, so the refusal's 0 counts


def test_job_no_generation_starts_from_an_ri2_location(ri2_world, briefs_on, model):
    """After the refused capture there is no brief, so nothing can be generated from the location.
    A brief written before capture on the same opportunity (``jurisdiction_code`` NULL) is refused
    by the generate route and, enqueued directly, by the job itself: no model call, usage row,
    reservation, draft or other row. Generation never re-reads the location (operator ruling Q-1),
    so the refusal at capture is the one that matters."""
    world = ri2_world
    ws = world.workspace("ri2 generation")
    text = "Alabama State United States, 1 Main St, United States"
    opp, record, _ = evidence(world, ws, location=ANCHORAGE_FIELDS, addresses=(text,))
    assert capture(world, ws, opp, record).status_code == 422
    assert world.count(RecommendationBrief, opportunity_id=opp) == 0 and _jobs_for(world, ws) == 0
    legacy = world.seed_brief(ws, opportunity_id=opp, jurisdiction_code=None)
    r = world.generate(legacy, ws=ws)
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "brief_jurisdiction_missing"
    assert _jobs_for(world, ws) == 0
    job_id = _enqueue_generation(world, legacy)
    before = _full_ledger(world, ws)
    assert world.drain() == 1
    job = world.job(job_id)
    assert (job["status"], job["last_error_code"], job["last_error_summary"]) == (
        "failed",
        "validation",
        "brief_jurisdiction_missing",
    )
    assert model.requests == []
    assert world.count(LLMUsageEvent, job_id=job_id) == 0
    assert world.count(CreativeDraft, brief_id=legacy) == 0
    assert _ledger(world, ws) == (0, 0, 0)
    assert _full_ledger(world, ws) == before
