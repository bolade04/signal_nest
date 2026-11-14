"""Project Phase 5D-W4 adversarial battery: the jurisdiction axis fails closed.

Lane 5D-W4 (W21) tests code it did not write (lanes 5D-W1 and 5D-W2) against the contract and
the operator's rulings, not against what the code happens to do:

* ``docs/project-phase-5d-multi-location-jurisdiction.md`` §2 (the fail-closed resolver, "every
  consumer treats ``UNRESOLVED`` as deny") and §4 exit criteria 4 and 5 (the connector half);
* the S1-5D rulings of 2026-11-08 (``RULINGS-S1-5D-2026-11-08.md``): **item 33** (a location's
  jurisdiction is its country, except in the United States, Canada, Australia and Nigeria, where
  it is the first-level subdivision; changing that list is a reviewed contract change) and
  **D-2** (B-3: containment as built, approved entries take the ``allowed`` limb, blocked,
  prohibited and required-disclaimer entries the overlap limb, ``PR`` is outside ``US``; B-2:
  for an unknown draft jurisdiction restricting entries apply and approved entries are not
  offered).

The inputs are deliberately the ones the existing modules do not use
(``test_jurisdiction_resolution.py``, ``test_jurisdiction_propagation.py``'s claims half,
``test_connector_policy_unresolved_market.py``): case, width and whitespace variants, look-alike
characters, the sentinel stored where a code is expected, duplicates, and lists mixing valid and
invalid codes.
"""

from __future__ import annotations

import ast
import json
import logging
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import DBAPIError

from app.audit.models import AuditLog
from app.brands.service import get_primary_brand
from app.briefs import service as brief_service
from app.briefs.models import RecommendationBrief
from app.briefs.routes import create_opportunity_brief
from app.claims.engine import (
    LibraryEntry,
    active_entries,
    applies_in_jurisdiction,
    is_approved_kind,
    jurisdiction_restriction,
    review_against_library,
)
from app.connectors.policy import ConnectorPolicy
from app.core.config import get_settings
from app.core.errors import SignalNestError
from app.creative.models import ContentReview, CreativeDraft
from app.creative.schemas import CreativeGeneratePayload
from app.geography.places_data import COUNTRIES
from app.intelligence.records import SignalIntelligenceRecord
from app.jobs.context import ExecutionContext
from app.jobs.models import Job
from app.jobs.pipeline import _coverage_rule_for
from app.jobs.service import enqueue_job
from app.jobs.status import JobType
from app.jurisdiction.business_address import (
    ACTION_CLARIFICATIONS,
    BUSINESS_ADDRESS_CONFLICT,
    BUSINESS_ADDRESS_INCOMPLETE,
    BUSINESS_ADDRESS_LOCALITY_UNCONFIRMED,
    BUSINESS_ADDRESS_TEXT_CONFLICT,
    LOCATION_UNRESOLVED,
    REFUSALS,
)
from app.jurisdiction.codes import UNRESOLVED, is_jurisdiction_code
from app.jurisdiction.resolver import (
    ADDRESS_DIRECTION_AMBIGUOUS,
    ADDRESS_NAMES_ANOTHER_PLACE,
    ADDRESS_PLACE_UNCLEAR,
    ADDRESS_ROUTE_AMBIGUOUS,
    ADDRESS_SUFFIX_AMBIGUOUS,
    ADDRESS_TERRITORY_UNCLEAR,
    CLARIFICATIONS,
    JurisdictionResolution,
    code_within,
    codes_may_overlap,
    jurisdiction_permits,
    resolve_jurisdiction,
)
from app.jurisdiction.subdivisions import SUBDIVISION_JURISDICTIONS
from app.llm import service as service_module
from app.llm.models import LLMBudgetPeriod, LLMBudgetReservation, LLMUsageEvent
from app.llm.providers_real import AnthropicProvider
from app.llm.service import LLMService
from app.locations.models import BusinessLocation, GeoCoverageRule
from app.opportunities.models import Opportunity
from app.organizations.models import User
from app.scouting_requests.models import ScoutRequest
from app.signals.models import NormalizedSignal, RawSignal, SignalCluster
from app.tests._address_support import complete_address_of
from app.tests._creative_support import (
    CEILINGS,
    CreativeWorld,
    creative_world,
    postgres_creative_world,
)
from app.tests._llm_offline import offline_settings


def _loc(city=None, state=None, country=None, **extra) -> SimpleNamespace:
    return SimpleNamespace(city=city, state_province=state, country=country, **extra)


def _rule(address) -> SimpleNamespace:
    return SimpleNamespace(business_address=address)


# --------------------------------------------------------------------------- #
# 1. The four-market matrix (exit criterion 4; item 33)
# --------------------------------------------------------------------------- #
#: Each market of the matrix in spellings a location record can hold that the resolution module
#: does not try: shouting, padding, full-width forms, dotted abbreviations, a "State" suffix.
FOUR_MARKETS = [
    ("Dallas", "TEXAS", "united states", "US-TX"),
    ("dallas", "  Texas  ", "  US  ", "US-TX"),
    ("Dallas", "Ｔｅｘａｓ", "ＵＳ", "US-TX"),  # full-width
    ("Dallas", "T.X.", "U.S.A.", "US-TX"),
    ("Dallas", "us-tx", "USA", "US-TX"),
    ("London", None, "united kingdom", "GB"),
    ("LONDON", "", " GB ", "GB"),
    ("London", "Greater London", "ＵＫ", "GB"),
    ("Lagos", "Lagos State", "Nigeria", "NG-LA"),
    ("Lagos", "LAGOS STATE", "nigeria", "NG-LA"),
    ("Lagos", "lagos   state", "NG", "NG-LA"),
    ("Lagos", "Lagos-State", "ng", "NG-LA"),
    ("Lagos", "ng-la", "NG", "NG-LA"),
    ("Nairobi", "Nairobi", "KENYA", "KE"),
    ("Nairobi", None, "ke", "KE"),
    ("Nairobi", "", "  Kenya  ", "KE"),
]


@pytest.mark.parametrize(("city", "state", "country", "expected"), FOUR_MARKETS)
def test_the_four_market_matrix_resolves_whatever_the_spelling(city, state, country, expected):
    resolution = resolve_jurisdiction(_loc(city, state, country))
    assert resolution.code == expected, resolution.evidence
    assert resolution.is_resolved and is_jurisdiction_code(resolution.code)
    # The codes whose rules apply there: the subdivision, then its country; or the country.
    assert resolution.codes == ((expected, expected[:2]) if "-" in expected else (expected,))
    # A resolved matrix code passes the shared gate, and only inside itself or its country.
    assert jurisdiction_permits(resolution) is True
    assert jurisdiction_permits(resolution, allowed=(expected[:2],)) is True
    assert jurisdiction_permits(resolution, allowed=("ZZ",)) is False


def test_the_matrix_markets_are_four_distinct_jurisdictions():
    """No two matrix markets share a jurisdiction, and no one lies inside another."""
    codes = {resolve_jurisdiction(_loc(c, s, k)).code for c, s, k, _ in FOUR_MARKETS}
    assert codes == {"US-TX", "GB", "NG-LA", "KE"}
    for code in codes:
        for other in codes - {code}:
            assert not codes_may_overlap(code, other), (code, other)


# --------------------------------------------------------------------------- #
# 2. The subdivision list is pinned (item 33: a change is a reviewed contract change)
# --------------------------------------------------------------------------- #
SUBDIVISION_LEVEL = {"US", "CA", "AU", "NG"}


def test_the_subdivision_list_is_exactly_us_ca_au_ng():
    """Any addition or removal fails here, deliberately: item 33 makes it a contract change
    that needs its own operator ruling and an independent review."""
    assert set(SUBDIVISION_JURISDICTIONS) == SUBDIVISION_LEVEL
    for country, divisions in SUBDIVISION_JURISDICTIONS.items():
        assert divisions, country  # a listed country with no subdivisions resolves nothing


@pytest.mark.parametrize("country", sorted(COUNTRIES))
def test_every_country_resolves_at_the_level_item_33_rules(country):
    """The pin, behaviourally: for every ISO country the place data knows, a location naming
    only the country resolves to the country itself, except the four subdivision-level
    countries, where the same location is UNRESOLVED (a country alone is not enough)."""
    resolution = resolve_jurisdiction(_loc("Somewhere", None, country))
    if country in SUBDIVISION_LEVEL:
        assert resolution.code == UNRESOLVED, resolution.evidence
    else:
        assert resolution.code == country, resolution.evidence


# --------------------------------------------------------------------------- #
# 3. UNRESOLVED: what the resolver must never guess
# --------------------------------------------------------------------------- #
UNRESOLVABLE = [
    # The shared resolver's own answer, unchanged by operator ruling Q-4, which amends the
    # brief capture and not the resolver: a record with no location is decided before the
    # resolver runs and gets a NULL brief (the Q-4 section at the end of this module).
    (None, "no location"),
    (_loc("Dallas", "Texas", None), "no country"),
    (_loc("Dallas", "Texas", "  "), "a blank country"),
    (_loc("Dallas", "Texas", "XX"), "an unknown two-letter country"),
    (_loc("Dallas", "Texas", "U"), "a one-letter country"),
    (_loc("Dallas", "Texas", UNRESOLVED), "the sentinel typed as the country"),
    (_loc("Dallas", "Texas", "None"), "a placeholder country"),
    (_loc("Lagos", "Lagos State", "Federal Republic of Nigeria"), "an unreadable country name"),
    # A subdivision-level country without its subdivision, or with one it cannot read.
    (_loc("Dallas", None, "US"), "US without a state"),
    (_loc("Dallas", "  ", "US"), "US with a blank state"),
    (_loc("Dallas", "Tex.", "US"), "an unreadable abbreviation"),
    (_loc("Dallas", "TX 75201", "US"), "a state with a postcode"),
    (_loc("Dallas", "Texas, Oklahoma", "US"), "two states"),
    (_loc("Dallas", "New Texas", "US"), "no such state"),
    (_loc("Dallas", "US-TX-DAL", "US"), "an over-long code"),
    (_loc("Dallas", "NG-LA", "US"), "another country's subdivision code"),
    (_loc("Toronto", None, "Canada"), "CA without a province"),
    (_loc("Los Angeles", "California", "CA"), "'CA' is Canada in a country field"),
    (_loc("Sydney", None, "AU"), "AU without a state"),
    (_loc("Perth", "Washington", "Australia"), "another country's state name"),
    (_loc("Lagos", None, "NG"), "NG without a state"),
    (_loc("Lagos", "Lagos, Nigeria", "NG"), "a state field holding a city and country"),
    (_loc("Lagos", "Ikeja", "Nigeria"), "a city typed as the state"),
    # A region typed into the country field names the country but not the subdivision.
    (_loc("Dallas", None, "Texas"), "a state in the country field, no state field"),
    # A city alone, known or unknown, even one that carries its own qualifier.
    (_loc("Dallas"), "a known city alone"),
    (_loc("Dallas, TX"), "a qualified city in the city field alone"),
    (_loc("London, UK"), "a qualified city in the city field alone"),
    (_loc("Gotham"), "an unknown city alone"),
    (_loc("Lagos", "Lagos State"), "a city and state with no country"),
]


@pytest.mark.parametrize(("location", "why"), UNRESOLVABLE, ids=[w for _, w in UNRESOLVABLE])
def test_what_cannot_be_placed_is_unresolved_and_denied(location, why):
    resolution = resolve_jurisdiction(location)
    assert resolution.code == UNRESOLVED, why
    assert resolution.codes == () and resolution.is_resolved is False
    assert resolution.evidence[-1].startswith("unresolved: ")
    assert jurisdiction_permits(resolution) is False
    assert jurisdiction_permits(resolution, allowed=("US", "GB", "NG", "KE", "CA", "AU")) is False


@pytest.mark.parametrize(
    ("location", "rules"),
    [
        # The second rule disagrees: every rule is read, not only the first.
        (_loc("Dallas", "Texas", "US"), [_rule("Austin, TX"), _rule("Tulsa, OK")]),
        (_loc("London", None, "GB"), [_rule(None), _rule("Dallas, TX")]),
        # A subdivision stated in a different spelling, and a code in an address.
        (_loc("Lagos", "Lagos State", "NG"), [_rule("Abeokuta, Ogun State, Nigeria")]),
        (_loc("Nairobi", None, "Kenya"), [_rule("Lagos, NG")]),
        # An address that states only a state of another country (unambiguous region).
        (_loc("London", "England", "United Kingdom"), [_rule("Dallas, Texas")]),
    ],
)
def test_any_conflicting_coverage_address_is_unresolved(location, rules):
    resolution = resolve_jurisdiction(location, rules)
    assert resolution.code == UNRESOLVED
    assert "business address" in resolution.evidence[-1]
    assert jurisdiction_permits(resolution) is False


@pytest.mark.parametrize(
    ("location", "captured"),
    [
        (_loc("Dallas", "Texas", "US"), "US"),  # the country of a subdivision-level location
        (_loc("Dallas", "Texas", "US"), "us-tx"),  # a case variant of the right code
        (_loc("Dallas", "Texas", "US"), "US-OK"),
        (_loc("London", None, "GB"), "GB-ENG"),  # a subdivision of a country-level location
        (_loc("Lagos", "Lagos", "NG"), "NG-OG"),
        (_loc("Nairobi", None, "KE"), UNRESOLVED),  # the sentinel captured as a code
        (_loc("Nairobi", None, "KE"), ""),
        (_loc("Nairobi", None, "KE"), " KE"),
    ],
)
def test_a_stale_captured_code_is_unresolved_never_trusted(location, captured):
    location.jurisdiction_code = captured
    resolution = resolve_jurisdiction(location)
    assert resolution.code == UNRESOLVED
    assert "captured jurisdiction" in resolution.evidence[-1]


# --------------------------------------------------------------------------- #
# 4. The shared gate denies anything that is not a code, on both limbs
# --------------------------------------------------------------------------- #
#: Values that are not a jurisdiction code. Strings first (the sentinel, malformed shapes, case,
#: padding, full-width and Cyrillic look-alikes, an en dash, a trailing newline, a NUL), then
#: non-strings.
NOT_CODES = [
    None,
    UNRESOLVED,
    "unresolved",
    "US-",
    "us-tx",
    "Us-TX",
    "U",
    "",
    " ",
    " US-TX",
    "US-TX ",
    "US-TX\n",
    "\tGB",
    "ＵＳ-ＴＸ",  # full-width US-TX
    "US-ТХ",  # Cyrillic TE and HA
    "US–TX",  # en dash
    "GB\x00",
    "US--X",
    "US-TEXA",
    "USA",
    0,
    1,
    True,
    3.5,
    b"US",
    ["US"],
    ("US",),
    {"US"},
]
_NOT_CODE_IDS = [repr(v) for v in NOT_CODES]


@pytest.mark.parametrize("bad", NOT_CODES, ids=_NOT_CODE_IDS)
def test_the_gate_denies_a_subject_that_is_not_a_code(bad):
    assert jurisdiction_permits(bad) is False
    assert jurisdiction_permits(bad, allowed=("US", "US-TX", "GB")) is False
    assert jurisdiction_permits(bad, excluded=()) is False
    assert jurisdiction_permits(bad, excluded=("ZZ",)) is False
    assert jurisdiction_permits(bad, allowed=(bad,)) is False  # never matches even itself


@pytest.mark.parametrize("bad", NOT_CODES, ids=_NOT_CODE_IDS)
def test_an_allowed_entry_that_is_not_a_code_clears_nothing(bad):
    assert jurisdiction_permits("US-TX", allowed=(bad,)) is False
    # A readable entry beside it still clears inside itself, and only there.
    assert jurisdiction_permits("US-TX", allowed=(bad, "US")) is True
    assert jurisdiction_permits("GB", allowed=(bad, "US")) is False
    assert code_within("US-TX", bad) is False and code_within(bad, "US") is False


@pytest.mark.parametrize("bad", NOT_CODES, ids=_NOT_CODE_IDS)
def test_an_excluded_entry_that_is_not_a_code_cannot_be_ruled_out(bad):
    assert jurisdiction_permits("US-TX", excluded=(bad,)) is False
    assert jurisdiction_permits("KE", allowed=("KE",), excluded=("GB", bad)) is False
    assert codes_may_overlap("KE", bad) is True


def test_a_bare_string_passed_as_a_list_never_widens():
    """A caller passing ``"US"`` where a list is expected iterates its letters: neither ``"U"``
    nor ``"S"`` is a code, so nothing is cleared and everything is excluded."""
    assert jurisdiction_permits("US-TX", allowed="US") is False
    assert jurisdiction_permits("GB", excluded="KE") is False


def test_duplicates_change_nothing():
    assert jurisdiction_permits("US-TX", allowed=("US", "US", "US")) is True
    assert jurisdiction_permits("GB", allowed=("US-TX", "US-TX")) is False
    assert jurisdiction_permits("US-TX", excluded=("GB", "GB")) is True
    assert jurisdiction_permits("US-TX", excluded=("US", "US")) is False


def test_a_resolution_object_never_passes_on_a_forged_code():
    for forged in ("us-tx", " US-TX", UNRESOLVED, ""):
        resolution = JurisdictionResolution(
            code=forged, codes=("US-TX", "US"), confidence=1.0, evidence=()
        )
        assert jurisdiction_permits(resolution, allowed=("US",)) is False


# --------------------------------------------------------------------------- #
# 5. The connector policy (exit criterion 5, connector half)
# --------------------------------------------------------------------------- #
#: Clearances, each a non-empty ``allowed_markets``, in spellings the connector-half module
#: does not use; the last two name nothing readable at all.
CLEARANCES = [
    ("Dallas, TX", "London, UK", "Lagos, NG", "Nairobi, KE"),
    ("  london, united kingdom  ",),
    ("Ｌｏｎｄｏｎ, ＵＫ",),  # full-width London, UK
    ("TEXAS",),
    ("United States",),
    ("Lagos, Lagos, Nigeria",),
    ("", "None"),
    ("London",),  # unqualified: no country, so it clears nothing
]


@pytest.mark.parametrize("allowed", CLEARANCES, ids=[repr(c) for c in CLEARANCES])
@pytest.mark.parametrize("source_types", [(), ("rss_news",), ("rss_news", "reddit")])
def test_a_market_restricted_policy_denies_an_unresolved_market(allowed, source_types):
    policy = ConnectorPolicy("rss_news", enabled=True, allowed_markets=frozenset(allowed))
    for market in (None, "", "   ", "\t", UNRESOLVED, "unknown", ",", "London,", "Gotham"):
        assert policy.permits(source_types=source_types, market=market) is False, market


@pytest.mark.parametrize(
    ("allowed", "served"),
    [
        (("  london, united kingdom  ",), "London, UK"),
        (("Ｌｏｎｄｏｎ, ＵＫ",), "London, United Kingdom"),
        (("TEXAS",), "Dallas, TX"),
        (("United States",), "Dallas, TX"),
        (("Lagos, Lagos, Nigeria",), "Lagos, Lagos, NG"),
    ],
)
def test_the_denial_is_the_market_not_the_spelling(allowed, served):
    """Each clearance above does serve a resolved market inside it: the denials of the
    unresolved markets are the cure, not a clearance that can never match."""
    policy = ConnectorPolicy("rss_news", enabled=True, allowed_markets=frozenset(allowed))
    assert policy.permits(source_types=("rss_news",), market=served) is True


def test_an_unreadable_clearance_serves_nothing():
    """A non-empty clearance that names no readable market is still a restriction."""
    for allowed in (("", "None"), ("London",), ("Gotham",)):
        policy = ConnectorPolicy("rss_news", enabled=True, allowed_markets=frozenset(allowed))
        for market in ("London, UK", "Dallas, TX", "Nairobi, KE", None):
            assert policy.permits(source_types=(), market=market) is False, (allowed, market)


# --------------------------------------------------------------------------- #
# 6. The claims gate as ruled (D-2: B-3 and B-2), with adversarial inputs
# --------------------------------------------------------------------------- #
APPROVED_KINDS = ["approved", "APPROVED", " Approved ", "ａｐｐｒｏｖｅｄ"]
RESTRICTING_KINDS = [
    "blocked",
    "BLOCKED",
    "ｂｌｏｃｋｅｄ",  # full-width blocked
    "Prohibited",
    " prohibited ",
    "required_disclaimer",
    "REQUIRED_DISCLAIMER",
    "Required_Disclaimer ",
]


def _entry(kind: str, codes, *, text: str = "free parking", entry_id: str = "e") -> LibraryEntry:
    return LibraryEntry(entry_id, text, kind, jurisdiction_codes=jurisdiction_restriction(codes))


def _offered(entries, jurisdiction) -> list[str]:
    """What generation offers the model as approved: the creative service's own expression
    (``[e.text for e in active if is_approved_kind(e.kind)]``)."""
    return [
        e.id for e in active_entries(entries, jurisdiction=jurisdiction) if is_approved_kind(e.kind)
    ]


@pytest.mark.parametrize("kind", APPROVED_KINDS)
def test_b3_an_approved_country_entry_is_offered_for_its_subdivisions(kind):
    """B-3: "A country-level approved entry may apply to a subdivision-level draft, such as US
    to US-TX." Whatever the kind's spelling, the approved limb is ``code_within``."""
    us = _entry(kind, ["US"])
    for draft in ("US-TX", "US-CA", "US-NY", "US-DC", "US"):
        assert applies_in_jurisdiction(us, draft), draft
    us_tx = _entry(kind, ["US-TX"])
    assert applies_in_jurisdiction(us_tx, "US-TX")
    for draft in ("US", "US-CA", "GB", "PR", "US-T", "US-TXX"):
        assert not applies_in_jurisdiction(us_tx, draft), draft


@pytest.mark.parametrize("kind", RESTRICTING_KINDS)
def test_b3_restricting_entries_apply_wherever_the_jurisdictions_may_overlap(kind):
    """B-3: "Blocked and disclaimer entries apply wherever the jurisdictions may overlap."
    A US entry applies to US-TX; a US-TX entry applies to a US draft and not to GB."""
    us = _entry(kind, ["US"])
    for draft in ("US", "US-TX", "US-CA"):
        assert applies_in_jurisdiction(us, draft), draft
    for draft in ("PR", "GB", "CA-ON", "MX"):
        assert not applies_in_jurisdiction(us, draft), draft
    us_tx = _entry(kind, ["US-TX"])
    for draft in ("US-TX", "US"):
        assert applies_in_jurisdiction(us_tx, draft), draft
    for draft in ("GB", "US-CA", "PR", "US-T"):
        assert not applies_in_jurisdiction(us_tx, draft), draft


@pytest.mark.parametrize("kind", [*APPROVED_KINDS, *RESTRICTING_KINDS])
def test_b3_pr_is_outside_us_both_ways(kind):
    """Recorded as part of "as built": a territory with its own ISO 3166-1 code is a separate
    jurisdiction from its parent country, for every kind and in both directions."""
    assert not applies_in_jurisdiction(_entry(kind, ["US"]), "PR")
    assert not applies_in_jurisdiction(_entry(kind, ["PR"]), "US")
    assert not applies_in_jurisdiction(_entry(kind, ["PR"]), "US-TX")
    assert applies_in_jurisdiction(_entry(kind, ["PR"]), "PR")


@pytest.mark.parametrize("kind", [*APPROVED_KINDS, *RESTRICTING_KINDS])
def test_an_empty_restriction_means_everywhere(kind):
    for stored in (None, [], ()):
        entry = _entry(kind, stored)
        for draft in ("US-TX", "GB", "NG-LA", "KE", "PR", None, UNRESOLVED, " US"):
            assert applies_in_jurisdiction(entry, draft), (stored, draft)


#: Draft jurisdictions that are unknown or not a code (a snapshot could hold some of these:
#: the column's CHECK is a shape bound that admits full-width capitals, for instance).
UNKNOWN_DRAFTS = [
    None,
    UNRESOLVED,
    " US-TX",
    "US-TX ",
    "US-TX\n",
    "ＵＳ-ＴＸ",
    "ＵＳ",
    "US-ТХ",
    "Us",
    0,
    ["US"],
]


@pytest.mark.parametrize("draft", UNKNOWN_DRAFTS, ids=[repr(d) for d in UNKNOWN_DRAFTS])
def test_b2_an_unknown_draft_gets_every_restriction_and_no_restricted_approval(draft):
    """B-2: "When a draft's jurisdiction is unknown, restricting entries apply and approved
    entries are not offered." An unrestricted approved entry is still offered (it is not a
    jurisdiction-restricted one)."""
    entries = [
        _entry("approved", ["US"], entry_id="ok-us"),
        _entry("approved", ["US", "GB", "NG", "KE", "CA", "AU"], entry_id="ok-many"),
        _entry("approved", None, entry_id="ok-everywhere"),
        _entry("blocked", ["KE"], entry_id="block-ke"),
        _entry("prohibited", ["US-TX"], entry_id="prohibit-tx"),
        _entry("required_disclaimer", ["GB"], text="Terms apply.", entry_id="disclaim-gb"),
    ]
    applied = {e.id for e in active_entries(entries, jurisdiction=draft)}
    assert applied == {"ok-everywhere", "block-ke", "prohibit-tx", "disclaim-gb"}
    assert _offered(entries, draft) == ["ok-everywhere"]
    review = review_against_library("Free parking.", None, entries, jurisdiction=draft)
    assert review.is_blocked and review.missing_disclaimers == ["Terms apply."]


def test_the_sentinel_stored_as_an_entry_code_fails_closed_both_ways():
    """``UNRESOLVED`` written into ``jurisdiction_codes`` by hand (the API refuses it; the JSON
    column has no CHECK): it names nothing, so a restricting entry applies everywhere and an
    approved one is offered nowhere, not even to a draft whose code is the sentinel."""
    blocked, approved = _entry("blocked", [UNRESOLVED]), _entry("approved", [UNRESOLVED])
    for draft in ("US-TX", "GB", "KE", None, UNRESOLVED):
        assert applies_in_jurisdiction(blocked, draft), draft
        assert not applies_in_jurisdiction(approved, draft), draft


@pytest.mark.parametrize(
    "codes",
    [
        ["GB", UNRESOLVED],
        ["GB", "us"],
        ["GB", " "],
        ["GB", "ＵＳ"],
        ["GB", "US-ТХ"],
        ["GB", ""],
        ["GB", "GB "],
    ],
    ids=repr,
)
def test_a_mixed_restriction_reads_its_valid_codes_and_fails_closed_on_the_rest(codes):
    """Mixed valid and invalid entry codes: the valid one still clears its own jurisdiction for
    an approved entry; the invalid one cannot be ruled out, so a restricting entry applies
    everywhere."""
    approved, blocked = _entry("approved", codes), _entry("blocked", codes)
    assert applies_in_jurisdiction(approved, "GB")
    assert applies_in_jurisdiction(approved, "GB-ENG")
    for draft in ("US", "US-TX", "KE", "NG-LA"):
        assert not applies_in_jurisdiction(approved, draft), draft
    for draft in ("GB", "US-TX", "KE", "NG-LA", None):
        assert applies_in_jurisdiction(blocked, draft), draft


@pytest.mark.parametrize(
    "codes",
    [
        [" "],
        ["\t"],
        ["ＵＳ"],
        ["US-ТХ"],
        ["us-tx"],
        [" US-TX"],
        ["US-TX\n"],
        [UNRESOLVED, ""],
    ],
    ids=repr,
)
def test_a_wholly_unreadable_restriction_fails_closed_both_ways(codes):
    approved, blocked = _entry("approved", codes), _entry("blocked", codes)
    assert jurisdiction_restriction(codes) is not None  # a restriction, never "everywhere"
    for draft in ("US-TX", "US", "GB", "KE", "NG-LA", None, UNRESOLVED):
        assert not applies_in_jurisdiction(approved, draft), draft
        assert applies_in_jurisdiction(blocked, draft), draft


def test_duplicate_entry_codes_and_duplicate_entries():
    duplicated = _entry("approved", ["US-TX", "US-TX"], entry_id="dup")
    assert applies_in_jurisdiction(duplicated, "US-TX")
    assert not applies_in_jurisdiction(duplicated, "US")
    twins = [
        _entry("approved", ["US"], text="Award-winning", entry_id="a"),
        _entry("approved", ["US"], text="Award-winning", entry_id="b"),
        _entry("required_disclaimer", ["US-TX"], text="Terms apply.", entry_id="d1"),
        _entry("required_disclaimer", ["US-TX", "US-TX"], text="Terms apply.", entry_id="d2"),
    ]
    assert _offered(twins, "US-TX") == ["a", "b"]
    review = review_against_library("Award-winning.", None, twins, jurisdiction="US-TX")
    assert review.required_disclaimers == ["Terms apply."]  # carried once
    assert review_against_library("Award-winning.", None, twins, jurisdiction="GB").passed


def test_a_jurisdiction_match_is_necessary_never_sufficient():
    """B-3: geographic eligibility is decided "alongside the entry's other approval and safety
    requirements". A retired entry stays inert wherever it is restricted to, and a kind that is
    none of approved / blocked / prohibited / required_disclaimer is never offered."""
    from datetime import UTC, datetime

    retired = LibraryEntry(
        "r", "Award-winning", "approved", retired_at=datetime.now(UTC), jurisdiction_codes=("US",)
    )
    typo = _entry("aproved", ["US"], entry_id="typo")
    for draft in ("US", "US-TX"):
        assert active_entries([retired], jurisdiction=draft) == []
        assert _offered([typo], draft) == []


# =========================================================================== #
# Phase 2 (lane 5D-W4): the capture refusal and the item-31 refusal, against lane 5D-W3's
# ``briefs/service.py`` and ``creative/service.py``.
#
# * The S1-5D capture rule: "For new snapshot capture, an unresolved location must produce an
#   explicit refusal rather than a guessed jurisdiction or permissive fallback." The rulings
#   record's interaction note 2: the refusal "comes before the model call, so no AI budget is
#   spent and no brief is stored". Operator ruling Q-4 (2026-11-08) supersedes its sentence
#   that an absent location is refused too: a record with no location now gets a brief with
#   jurisdiction NULL (the Q-4 section at the end of this module). Every existing location
#   reference that does not resolve, or is not the opportunity's workspace's and
#   organization's, is still refused (``UNRESOLVED_CAPTURES``).
# * Item 31: "Generation and export are refused for any brief or draft whose jurisdiction_code
#   is NULL. Nothing is inferred or defaulted." Enforced at the generate request and in the job
#   (rulings record, "Where it is enforced"). Export is 5E-W1's and is not built.
#
# A real HTTP world (``app.tests._creative_support``) on SQLite and, when
# ``TEST_POSTGRES_URL`` is set, on PostgreSQL migrated to head: the 5A brief route and the 5C
# generate route and worker run for real; only the model is the offline mock, counted here.
# =========================================================================== #
#: The four-market matrix as location records, each with a coverage rule whose business
#: address agrees, and the code each captures (item 33).
MATRIX_LOCATIONS = {
    "dallas": ({"city": "Dallas", "state_province": "TX", "country": "US"}, "Dallas, TX", "US-TX"),
    "london": (
        {"city": "London", "state_province": None, "country": "United Kingdom"},
        "London, UK",
        "GB",
    ),
    "lagos": (
        {"city": "Lagos", "state_province": "Lagos State", "country": "Nigeria"},
        "Ikeja, Lagos, NG",
        "NG-LA",
    ),
    "nairobi": (
        {"city": "Nairobi", "state_province": None, "country": "Kenya"},
        "Nairobi, KE",
        "KE",
    ),
}


@pytest.fixture(scope="module", params=["sqlite", "postgresql"])
def capture_world(request, tmp_path_factory) -> Iterator[CreativeWorld]:
    if request.param == "sqlite":
        db_file = tmp_path_factory.mktemp("w4-capture") / "api.db"
        with creative_world(db_file, "w4cap") as world:
            yield world
        return
    with postgres_creative_world("w4cap") as world:  # pragma: no cover - gated on PostgreSQL
        yield world


@pytest.fixture
def phase5_on(monkeypatch) -> None:
    """The brief route's global flag and every generation ceiling, for this test only (the
    creative capability is a per-workspace override, :meth:`CreativeWorld.workspace`)."""
    monkeypatch.setattr(get_settings(), "recommendation_briefs_enabled", True)
    for name, value in {**CEILINGS, "llm_max_calls_per_workspace_per_day": 1000}.items():
        monkeypatch.setattr(get_settings(), name, value)


class _CountingProvider:
    """Wraps the process's offline provider: records every request the model is sent."""

    def __init__(self, real) -> None:
        self.real, self.requests = real, []
        self.name, self.model = real.name, real.model

    def generate(self, request, prompt_name, prompt_version):
        self.requests.append(request)
        return self.real.generate(request, prompt_name, prompt_version)


@pytest.fixture
def model(monkeypatch) -> _CountingProvider:
    spy = _CountingProvider(service_module.llm_service._provider)
    monkeypatch.setattr(service_module.llm_service, "_provider", spy)
    return spy


@pytest.fixture
def budget_checks(monkeypatch) -> list[str]:
    """Every budget check and meter the brief service starts (the first budget use)."""
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


def evidence(
    world: CreativeWorld,
    ws: str,
    *,
    location: dict | None,
    addresses: tuple[str | None, ...] = (),
    location_ws: str | None = None,
    location_org: str | None = None,
    structured: dict | bool = True,
) -> tuple[str, str, str | None]:
    """One opportunity and its intelligence record in ``ws``, bound to a new location (in
    ``location_ws``, default ``ws``) with a coverage rule per address; ``location=None`` binds
    none. Returns ``(opportunity id, record id, location id)``.

    Since #313 the capture checks the structured business address (W21 ADDR item 6), so every
    rule written here holds, by default, the complete structured address of the location's own
    fields (``app.tests._address_support``); with no address, one rule holds it alone.
    ``structured`` may instead give the fields (a dict, merged over nothing) or ``False`` for
    none, the state of every rule written before W21 ADDR."""
    tag = uuid.uuid4().hex[:12]
    with world.session() as s:
        brand = get_primary_brand(s, ws)
        scoped = {"organization_id": world.org_of(ws), "workspace_id": ws}
        location_id = None
        if location is not None:
            home = location_ws or ws
            home_scoped = {
                "organization_id": location_org or world.org_of(home),
                "workspace_id": home,
            }
            location_id = f"lo{tag}"
            s.add(
                BusinessLocation(
                    id=location_id,
                    brand_id=get_primary_brand(s, home).id,
                    name="Shop",
                    **location,
                    **home_scoped,
                )
            )
            s.flush()
            if structured is True:
                fields = complete_address_of(location)
            else:
                fields = dict(structured or {})
            for address in addresses or ((None,) if fields else ()):
                s.add(
                    GeoCoverageRule(
                        location_id=location_id,
                        business_address=address,
                        **fields,
                        **home_scoped,
                    )
                )
            s.flush()
        s.add(ScoutRequest(id=f"sr{tag}", name="Scout", brand_id=brand.id, **scoped))
        s.flush()
        req = {**scoped, "scout_request_id": f"sr{tag}"}
        s.add(RawSignal(id=f"rs{tag}", source_type="rss", content="parking", **req))
        s.add(
            SignalCluster(
                id=f"cl{tag}", label="parking", workspace_id=ws, scout_request_id=f"sr{tag}"
            )
        )
        s.flush()
        s.add(
            NormalizedSignal(
                id=f"ns{tag}",
                raw_signal_id=f"rs{tag}",
                cluster_id=f"cl{tag}",
                source_type="rss",
                excerpt="parking",
                content_hash="0" * 64,
                **req,
            )
        )
        s.flush()
        s.add(
            Opportunity(
                id=f"op{tag}",
                brand_id=brand.id,
                title="Parking",
                scout_request_id=f"sr{tag}",
                cluster_id=f"cl{tag}",
                classification="x",
                decision="act_now",
                **scoped,
            )
        )
        s.flush()
        s.add(
            SignalIntelligenceRecord(
                id=f"in{tag}",
                normalized_signal_id=f"ns{tag}",
                opportunity_id=f"op{tag}",
                analysis_version="1",
                scoring_version="1",
                fingerprint="f" * 64,
                enricher="test",
                accepted=True,
                classification="x",
                location_id=location_id,
                **req,
            )
        )
    return f"op{tag}", f"in{tag}", location_id


def capture(world: CreativeWorld, ws: str, opportunity_id: str, record_id: str):
    """``POST .../opportunities/{id}/briefs`` as the marketer (the 5A route)."""
    return world.send(
        "POST",
        f"/workspaces/{ws}/opportunities/{opportunity_id}/briefs",
        "marketer",
        {"intelligence_record_id": record_id},
    )


def _ledger(world: CreativeWorld, ws: str) -> tuple[int, int, int]:
    return (
        world.count(LLMUsageEvent, workspace_id=ws),
        world.count(LLMBudgetReservation, workspace_id=ws),
        world.count(LLMBudgetPeriod, workspace_id=ws),
    )


def _full_ledger(world: CreativeWorld, ws: str) -> dict[str, int]:
    """Every row a capture or a generation could write in ``ws``, including ALL of the
    workspace's audit rows (review W21-REV-5DW3 r4 §4's ledger)."""
    return {
        "briefs": world.count(RecommendationBrief, workspace_id=ws),
        "drafts": world.count(CreativeDraft, workspace_id=ws),
        "reviews": world.count(ContentReview, workspace_id=ws),
        "jobs": world.count(Job, workspace_id=ws),
        "usage events": world.count(LLMUsageEvent, workspace_id=ws),
        "reservations": world.count(LLMBudgetReservation, workspace_id=ws),
        "budget periods": world.count(LLMBudgetPeriod, workspace_id=ws),
        "audit rows": world.count(AuditLog, workspace_id=ws),
    }


def _logged(caplog) -> str:
    return "\n".join(json.dumps(r.__dict__, default=str) for r in caplog.records)


#: (case, location fields, coverage addresses, where the location lives): every row is an
#: EXISTING location reference (Q-4: an absent location is not refused; it is covered in the
#: Q-4 section). Every location text is distinctive, so its absence from the answer and the
#: logs is checkable.
UNRESOLVED_CAPTURES = [
    (
        "a US location without a state",
        {"city": "Quillbrook", "state_province": None, "country": "US"},
        (),
        None,
    ),
    (
        "a US location with an unknown state",
        {"city": "Quillbrook", "state_province": "Quillshire", "country": "US"},
        (),
        None,
    ),
    ("a city only", {"city": "Quillbrook", "state_province": None, "country": None}, (), None),
    (
        "an unknown country",
        {"city": "Quillbrook", "state_province": None, "country": "Quillandia"},
        (),
        None,
    ),
    (
        "a conflicting coverage address",
        {"city": "Quillbrook", "state_province": "TX", "country": "US"},
        ("Tulsa, OK",),
        None,
    ),
    (
        "a location of another workspace of the organization",
        {"city": "Quillbrook", "state_province": "TX", "country": "US"},
        (),
        "ws2",
    ),
    (
        "a location of another organization",
        {"city": "Quillbrook", "state_province": "TX", "country": "US"},
        (),
        "ws_b",
    ),
    # Every coverage rule is read: the first agrees, the second states another state.
    (
        "a second coverage rule that conflicts",
        {"city": "Quillbrook", "state_province": "TX", "country": "US"},
        ("Austin, TX", "Tulsa, OK"),
        None,
    ),
    # A location row in this workspace that names another organization: an existing location
    # reference outside the opportunity's organization, so refused, never treated as absent
    # (Q-4, implementation reading: that would turn a tenant-boundary fault into a NULL brief).
    (
        "a location row of this workspace naming another organization",
        {"city": "Quillbrook", "state_province": "TX", "country": "US"},
        (),
        "row_of_org_b",
    ),
]
_LOCATION_TEXT = ("Quillbrook", "Quillshire", "Quillandia", "Tulsa", "Austin")


def _existing_location_evidence(
    world: CreativeWorld, ws: str, location: dict, addresses: tuple[str, ...], where: str | None
) -> tuple[str, str]:
    """``evidence`` for one ``UNRESOLVED_CAPTURES`` row: the record is bound to an existing
    location, in ``ws`` or where the row says (another workspace or organization, or a row of
    this workspace naming another organization)."""
    opp, record, location_id = evidence(
        world,
        ws,
        location=location,
        addresses=addresses,
        location_ws=getattr(world, where) if where in ("ws2", "ws_b") else None,
        location_org=world.org_b if where == "row_of_org_b" else None,
    )
    assert location_id is not None
    return opp, record


@pytest.mark.parametrize(
    ("case", "location", "addresses", "where"),
    UNRESOLVED_CAPTURES,
    ids=[c[0] for c in UNRESOLVED_CAPTURES],
)
def test_an_unresolved_location_refuses_the_brief_before_any_model_or_budget_use(
    capture_world, phase5_on, model, budget_checks, caplog, case, location, addresses, where
):
    world = capture_world
    ws = world.workspace(f"refuse {case[:30]}")
    opp, record = _existing_location_evidence(world, ws, location, addresses, where)
    before = _full_ledger(world, ws)
    with caplog.at_level(logging.INFO):
        r = capture(world, ws, opp, record)
    assert r.status_code == 422, (case, r.text)
    error = r.json()["error"]
    assert error["code"] == "jurisdiction_unresolved"
    # The wording is not pinned (it may be reworded); that it is one fixed answer is, below.
    assert isinstance(error["message"], str) and error["message"].strip()
    # Nothing was guessed, called, budgeted, audited or stored: not one row of any kind, and
    # not one audit row of the workspace.
    assert model.requests == [] and budget_checks == []
    assert world.count(RecommendationBrief, opportunity_id=opp) == 0
    assert _ledger(world, ws) == (0, 0, 0)
    assert world.count(AuditLog, workspace_id=ws, entity_type="recommendation_brief") == 0
    assert _full_ledger(world, ws) == before
    # No location text, and no resolver evidence, in the answer or the logs.
    logged = _logged(caplog)
    for text in _LOCATION_TEXT:
        assert text not in r.text and text not in logged, text
    assert "unresolved:" not in r.text and "unresolved:" not in logged
    refusals = [
        rec for rec in caplog.records if rec.getMessage() == "recommendation_brief_not_generated"
    ]
    assert refusals and refusals[-1].extra_fields["outcome"] == "refused"


#: The category of the capture's check (``resolve_for_action``) each ``UNRESOLVED_CAPTURES``
#: row is refused with; ``None`` is the tenant refusal, which names none. Each row's rules hold
#: the location's complete structured address (``evidence``'s default), so a coverage text that
#: names another state is the text refusal, and a location's own missing or unknown fact is
#: refused before its address is read.
UNRESOLVED_CAPTURE_CATEGORIES = {
    "a US location without a state": LOCATION_UNRESOLVED,
    "a US location with an unknown state": LOCATION_UNRESOLVED,
    "a city only": LOCATION_UNRESOLVED,
    "an unknown country": LOCATION_UNRESOLVED,
    "a conflicting coverage address": BUSINESS_ADDRESS_TEXT_CONFLICT,
    "a location of another workspace of the organization": None,
    "a location of another organization": None,
    "a second coverage rule that conflicts": BUSINESS_ADDRESS_TEXT_CONFLICT,
    "a location row of this workspace naming another organization": None,
}


def test_every_unresolved_capture_gets_its_categorys_one_fixed_answer(
    capture_world, phase5_on
) -> None:
    """The final clarification contract (W21 ADDR items 3 and 7; checklist I-10, INFO-6): the
    answer to a capture whose location fails the check names the check's category as
    ``error.details.reason``, with that category's one fixed message, and nothing else about
    what was missing or wrong (no resolver evidence, no text). A location outside the
    opportunity's workspace or organization gets the one generic answer, with no ``details``,
    exactly as before. Formerly "one fixed answer" for every cause; now one per category."""
    world = capture_world
    ws = world.workspace("refuse fixed answer")
    assert set(UNRESOLVED_CAPTURE_CATEGORIES) == {row[0] for row in UNRESOLVED_CAPTURES}
    answers: dict[str | None, set] = {}
    for case, location, addresses, where in UNRESOLVED_CAPTURES:
        opp, record = _existing_location_evidence(world, ws, location, addresses, where)
        r = capture(world, ws, opp, record)
        assert r.status_code == 422, (case, r.text)
        error = r.json()["error"]
        category = UNRESOLVED_CAPTURE_CATEGORIES[case]
        if category is None:
            assert set(error) == {"code", "message", "request_id"}, case
            assert error["message"] == brief_service.JURISDICTION_UNRESOLVED_MESSAGE, case
        else:
            assert set(error) == {"code", "message", "request_id", "details"}, case
            assert error["details"] == {"reason": category}, case
            assert error["message"] == ACTION_CLARIFICATIONS[category], case
        assert error["code"] == "jurisdiction_unresolved", case
        answers.setdefault(category, set()).add(
            (error["code"], error["message"], json.dumps(error.get("details")))
        )
    assert {category: len(seen) for category, seen in answers.items()} == {
        LOCATION_UNRESOLVED: 1,
        BUSINESS_ADDRESS_TEXT_CONFLICT: 1,
        None: 1,
    }


def test_a_deleted_location_gets_a_null_brief_that_generates_nothing(
    capture_world, phase5_on, model, budget_checks
) -> None:
    """A record whose location was deleted after it was written (``location_id`` is SET NULL
    by the foreign key) has no location (Q-4, implementation reading): its brief is created,
    budgeted and called as usual, with jurisdiction NULL, and item 31 refuses drafts from it.
    Before Q-4 this brief was refused as "no location"."""
    world = capture_world
    ws = world.workspace("deleted location")
    fields, address, _code = MATRIX_LOCATIONS["dallas"]
    opp, record, location_id = evidence(world, ws, location=fields, addresses=(address,))
    with world.session() as s:
        s.delete(s.get(BusinessLocation, location_id))
    assert world.row(SignalIntelligenceRecord, record)["location_id"] is None
    r = capture(world, ws, opp, record)
    assert r.status_code == 201, r.text
    brief_id = r.json()["id"]
    assert world.row(RecommendationBrief, brief_id)["jurisdiction_code"] is None
    assert len(model.requests) == 1 and "ensure_run_allowed" in budget_checks
    assert _ledger(world, ws)[0] == 1
    world.decide(brief_id, "approved")
    refused = world.generate(brief_id, ws=ws)
    assert refused.status_code == 422, refused.text
    assert refused.json()["error"]["code"] == "brief_jurisdiction_missing"
    world.drain()
    assert len(model.requests) == 1
    assert world.count(CreativeDraft, brief_id=brief_id) == 0


def test_a_resolved_location_is_captured_and_only_then_budgeted_and_called(
    capture_world, phase5_on, model, budget_checks
):
    """The control for the refusals above: the same world, a location that resolves."""
    world = capture_world
    ws = world.workspace("capture control")
    fields, address, code = MATRIX_LOCATIONS["dallas"]
    opp, record, _ = evidence(world, ws, location=fields, addresses=(address,))
    r = capture(world, ws, opp, record)
    assert r.status_code == 201, r.text
    assert world.row(RecommendationBrief, r.json()["id"])["jurisdiction_code"] == code
    assert len(model.requests) == 1 and "ensure_run_allowed" in budget_checks
    assert _ledger(world, ws)[0] == 1  # one usage row: the call was made and recorded


#: The real Anthropic adapter over a mock transport, priced: the path that reserves budget.
_KEY, _MODEL = "fixture-key", "claude-sonnet-4-6"
_EXPLAINED = {
    "why_it_matters": "Commuters report long morning waits.",
    "who_cares": "commuters",
    "ai_inference": "Inference: speed decides.",
    "recommended_action": "Publish a brand-safe post about faster service.",
    "risk_note": "",
    "suggested_angles": ["Speed you can count on"],
}


def _priced_llm() -> tuple[LLMService, object, list]:
    settings = offline_settings(llm_provider="anthropic", llm_api_key=_KEY, llm_model=_MODEL)
    sent: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request.url.path)
        return httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": json.dumps(_EXPLAINED)}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1000, "output_tokens": 500},
            },
        )

    provider = AnthropicProvider(
        _KEY, _MODEL, 5, client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    return LLMService(provider, sleep=lambda _s: None, settings=settings), settings, sent


def test_a_refused_capture_reserves_no_budget_where_a_resolved_one_does(capture_world):
    """On the priced path the resolved capture reserves (and settles) before its call on
    PostgreSQL, where the shared ledger lives; the refused one (an existing location without
    its state) reserves nothing on either engine, sends nothing and stores nothing; an absent
    location (Q-4) is budgeted, reserved and called exactly as the resolved one."""
    world = capture_world
    ws = world.workspace("priced capture")
    llm, settings, sent = _priced_llm()
    refused_opp, refused_record, _ = evidence(
        world, ws, location={"city": "Dallas", "state_province": None, "country": "US"}
    )
    absent_opp, absent_record, _ = evidence(world, ws, location=None)
    fields, address, code = MATRIX_LOCATIONS["dallas"]
    opp, record, _ = evidence(world, ws, location=fields, addresses=(address,))

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
    brief_id = run(opp, record)
    assert len(sent) == 1
    assert world.row(RecommendationBrief, brief_id)["jurisdiction_code"] == code
    usage, reservations, _periods = _ledger(world, ws)
    assert usage == 1
    if world.engine.dialect.name == "postgresql":
        assert reservations == 1  # the reservation path is live, so the refusal's 0 counts
    absent_id = run(absent_opp, absent_record)
    assert len(sent) == 2
    assert world.row(RecommendationBrief, absent_id)["jurisdiction_code"] is None
    usage, reservations, _periods = _ledger(world, ws)
    assert usage == 2
    if world.engine.dialect.name == "postgresql":
        assert reservations == 2


# --------------------------------------------------------------------------- #
# Item 31: a brief with no captured jurisdiction generates nothing
# --------------------------------------------------------------------------- #
def _jobs_for(world: CreativeWorld, ws: str) -> int:
    return world.count(Job, workspace_id=ws)


#: A brief written before capture (NULL), and stored values the column's shape CHECK admits
#: but that are not jurisdiction codes (a full-width ``US``, ``U1``): none is inferred from.
NOT_CAPTURED = [None, "\uff35\uff33", "U1"]


@pytest.mark.parametrize("stored", NOT_CAPTURED, ids=repr)
def test_item_31_the_generate_route_refuses_and_enqueues_nothing(
    capture_world, phase5_on, model, stored
):
    world = capture_world
    ws = world.workspace("item 31 route")
    brief_id = world.seed_brief(ws, jurisdiction_code=stored)
    assert world.row(RecommendationBrief, brief_id)["jurisdiction_code"] == stored
    r = world.generate(brief_id, ws=ws)
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "brief_jurisdiction_missing"
    assert _jobs_for(world, ws) == 0
    world.drain()
    assert model.requests == []
    assert world.count(CreativeDraft, brief_id=brief_id) == 0
    # The control: the same workspace, a brief that carries a code, is accepted and run here
    # (so no job of this test is left for a later one's worker).
    control = world.seed_brief(ws, jurisdiction_code="US-TX")
    ok = world.generate(control, ws=ws)
    assert ok.status_code == 202, ok.text
    world.drain()
    assert world.job(ok.json()["job_id"])["status"] == "succeeded"
    assert world.count(CreativeDraft, brief_id=control) == 1


def test_item_31_is_checked_before_approval_at_the_generate_route(
    capture_world, phase5_on, model
) -> None:
    """An unapproved legacy brief is refused for its missing jurisdiction, not for approval:
    approving it cannot cure it (a new version can)."""
    world = capture_world
    ws = world.workspace("item 31 gate order")
    legacy = world.seed_brief(ws, approved=False, jurisdiction_code=None)
    r = world.generate(legacy, ws=ws)
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "brief_jurisdiction_missing"
    # The control: an unapproved brief that has a code meets the approval gate instead.
    pending = world.generate(world.seed_brief(ws, approved=False, jurisdiction_code="US-TX"), ws=ws)
    assert pending.status_code == 422, pending.text
    assert pending.json()["error"]["code"] == "brief_not_approved"
    assert _jobs_for(world, ws) == 0 and model.requests == []


#: The SQLSTATE of D-N4 item 37's PostgreSQL append-only backstop (#318, ``d0a8bce8de6a``).
APPEND_ONLY_SQLSTATE = "SNA01"


def backstop_refuses_update(world: CreativeWorld, model, row_id: str, **values) -> None:
    """On PostgreSQL, D-N4 item 37's backstop refuses an UPDATE of a guarded row, whoever issues
    it (here the test's own witness connection, outside any privacy-writer transaction), and the
    row is unchanged. The test never takes the privacy-writer mark, which is for the privacy
    writers only (37 ruling)."""
    assert world.witness.dialect.name == "postgresql"
    table = model.__table__
    before = world.row(model, row_id)
    with pytest.raises(DBAPIError) as refused:
        with world.witness.begin() as conn:
            conn.execute(update(table).where(table.c.id == row_id).values(**values))
    assert getattr(refused.value.orig, "sqlstate", None) == APPEND_ONLY_SQLSTATE, refused.value
    assert world.row(model, row_id) == before


def _job_for_a_brief_without_a_code(world: CreativeWorld, ws: str, stored) -> tuple[str, str]:
    """A queued ``creative.generate`` job whose brief holds ``stored`` (not a code) when the job
    runs: ``(brief id, job id)``. Item 31 must refuse it in the job itself.

    * **SQLite** (no backstop): the brief is coded when the generate route checks it, and is
      changed afterwards through the witness, the window between enqueue and run.
    * **PostgreSQL**: since D-N4 item 37 (#318) the append-only backstop refuses that change
      (asserted), so the window does not exist there. The job's own check is exercised on a
      brief inserted with ``stored``, as legacy rows are written (an INSERT is never refused),
      its job enqueued exactly as the route builds one but without the route's check."""
    if world.witness.dialect.name != "postgresql":
        brief_id = world.seed_brief(ws, jurisdiction_code="US-TX")
        r = world.generate(brief_id, ws=ws)
        assert r.status_code == 202, r.text
        table = RecommendationBrief.__table__
        with world.witness.begin() as conn:
            conn.execute(
                update(table).where(table.c.id == brief_id).values(jurisdiction_code=stored)
            )
        return brief_id, r.json()["job_id"]
    coded = world.seed_brief(ws, jurisdiction_code="US-TX")
    backstop_refuses_update(world, RecommendationBrief, coded, jurisdiction_code=stored)
    legacy = world.seed_brief(ws, jurisdiction_code=stored)
    assert world.row(RecommendationBrief, legacy)["jurisdiction_code"] == stored
    return legacy, _enqueue_generation(world, legacy)


@pytest.mark.parametrize("stored", NOT_CAPTURED, ids=repr)
def test_item_31_the_job_refuses_a_brief_nulled_between_enqueue_and_run(
    capture_world, phase5_on, model, stored
):
    world = capture_world
    ws = world.workspace("item 31 job")
    brief_id, job_id = _job_for_a_brief_without_a_code(world, ws, stored)
    world.drain()
    job = world.job(job_id)
    assert (job["status"], job["last_error_code"], job["last_error_summary"]) == (
        "failed",
        "validation",
        "brief_jurisdiction_missing",
    )
    assert model.requests == []
    assert world.count(CreativeDraft, brief_id=brief_id) == 0
    assert world.count(LLMUsageEvent, job_id=job_id) == 0
    with world.witness.connect() as conn:
        assert (
            conn.execute(
                select(func.count())
                .select_from(CreativeDraft.__table__)
                .where(CreativeDraft.__table__.c.workspace_id == ws)
            ).scalar_one()
            == 0
        )


def test_item_31_the_job_refusal_is_terminal_and_releases_the_generation_locks(
    capture_world, phase5_on, model
) -> None:
    """The refused job fails once and for all (VALIDATION, never retried), and the generation
    locks it took (organization, global, workspace on PostgreSQL) end with its transaction: a
    second job in the same workspace runs right after it."""
    world = capture_world
    ws = world.workspace("item 31 terminal")
    legacy, refused = _job_for_a_brief_without_a_code(world, ws, None)
    world.drain()
    job = world.job(refused)
    assert (job["status"], job["last_error_code"], job["attempt_count"]) == (
        "failed",
        "validation",
        1,
    )
    other = world.seed_brief(ws, jurisdiction_code="US-TX")
    ran = world.generate(other, ws=ws).json()["job_id"]
    world.drain()
    assert world.job(ran)["status"] == "succeeded"
    assert world.count(CreativeDraft, brief_id=other) == 1
    assert len(model.requests) == 1  # the second job's call only
    again = world.job(refused)  # still terminal: no retry was scheduled or run
    assert (again["status"], again["attempt_count"]) == ("failed", 1)
    assert world.count(CreativeDraft, brief_id=legacy) == 0


# =========================================================================== #
# Phase Q-4 (lane 5D-W4): absent versus unresolved locations.
#
# Operator ruling Q-4 (2026-11-08; rulings record, "Rulings of the sixth instruction"):
# "Allow an opportunity with no location to receive a recommendation brief with jurisdiction
# NULL. ... Item 31 remains enforced: no creative draft may be generated or exported from that
# brief. An opportunity with an existing but unresolved location must still be refused before
# generation. Do not infer a jurisdiction or backfill existing records. ... add regression
# coverage distinguishing absent locations from unresolved locations."
#
# The coordinator's implementation reading (same section): "Absent means exactly
# intelligence_record.location_id IS NULL. That includes a record whose location was deleted";
# "Every non-NULL location_id is an existing location reference. If it does not load inside the
# opportunity's workspace and organization, or the resolver answers UNRESOLVED for it, brief
# creation is refused before the model call"; "Nothing is inferred: no workspace default, no
# country from the scout, no back-fill of existing briefs."
#
# Absent (here) against existing-but-unresolved (``UNRESOLVED_CAPTURES`` and its refusal test
# above, which keep every existing-location row), on SQLite and PostgreSQL (``capture_world``).
# Export is 5E-W1's and is not built.
# =========================================================================== #
#: The two ways a record has no location (the implementation reading of Q-4).
ABSENT = ("never bound", "deleted")
#: Text of a location the capture must never read, quote or log for an absent record.
_ABSENT_TEXT = ("Wrenmoor", "Wrenshire", "Wrenfield")
#: Fragments of the resolver's evidence (``app.jurisdiction.resolver``): never answered or logged.
_RESOLVER_EVIDENCE = (
    "unresolved:",
    "resolves by country",
    "business address",
    "captured jurisdiction",
)


def _absent_record(world: CreativeWorld, ws: str, how: str) -> tuple[str, str]:
    """An opportunity whose intelligence record has no location: never bound, or bound to a
    resolvable location (US-TX) that is then deleted (the reference is SET NULL)."""
    if how == "never bound":
        opp, record, _ = evidence(world, ws, location=None)
        return opp, record
    opp, record, location_id = evidence(
        world,
        ws,
        location={"city": "Wrenmoor", "state_province": "TX", "country": "US"},
        addresses=("Wrenfield, TX",),
    )
    with world.session() as s:
        location = s.get(BusinessLocation, location_id)
        rules = s.scalars(
            select(GeoCoverageRule).where(GeoCoverageRule.location_id == location_id)
        ).all()
        assert resolve_jurisdiction(location, rules).code == "US-TX"  # it would have resolved
        s.delete(location)
    assert world.row(SignalIntelligenceRecord, record)["location_id"] is None
    return opp, record


def _brief_audits(world: CreativeWorld, ws: str) -> int:
    return world.count(AuditLog, workspace_id=ws, entity_type="recommendation_brief")


def _reviews(world: CreativeWorld, ws: str) -> int:
    return world.count(ContentReview, workspace_id=ws)


@pytest.mark.parametrize("how", ABSENT)
def test_q4_an_absent_location_gets_a_brief_with_jurisdiction_null(
    capture_world, phase5_on, model, budget_checks, caplog, how
) -> None:
    """Q-4: the brief is created normally (the budget gate, one model call, its usage event,
    its audit row), and its ``jurisdiction_code`` is NULL: nothing stands in for the location."""
    world = capture_world
    ws = world.workspace(f"q4 absent {how}")
    opp, record = _absent_record(world, ws, how)
    with caplog.at_level(logging.INFO):
        r = capture(world, ws, opp, record)
    assert r.status_code == 201, r.text
    body = r.json()
    # No response model carries the code (no contract change).
    assert "jurisdiction_code" not in body
    row = world.row(RecommendationBrief, body["id"])
    assert row["jurisdiction_code"] is None and row["location_id"] is None
    # The normal path, as for a located opportunity: budget checked and metered, one call, one
    # usage event that the brief references, one audit row.
    assert len(model.requests) == 1
    assert {"ensure_run_allowed", "meter"} <= set(budget_checks)
    (usage,) = world.rows(LLMUsageEvent, workspace_id=ws)
    assert row["llm_usage_event_id"] == usage["id"]
    assert _brief_audits(world, ws) == 1
    # Created, not refused; and nothing of a location or the resolver is answered or logged.
    events = [rec.getMessage() for rec in caplog.records]
    assert "recommendation_brief_created" in events
    assert "recommendation_brief_not_generated" not in events
    logged = _logged(caplog)
    for text in (*_ABSENT_TEXT, *_RESOLVER_EVIDENCE):
        assert text not in r.text and text not in logged, text


def test_q4_absent_and_unresolved_are_told_apart_in_one_workspace(
    capture_world, phase5_on, model
) -> None:
    """The distinction itself, side by side: absent is captured as NULL; an existing location
    that does not resolve is refused (no brief, no call); a resolvable one stores its code."""
    world = capture_world
    ws = world.workspace("q4 side by side")
    absent_opp, absent_record, _ = evidence(world, ws, location=None)
    no_state_opp, no_state_record, _ = evidence(
        world, ws, location={"city": "Dallas", "state_province": None, "country": "US"}
    )
    fields, address, code = MATRIX_LOCATIONS["dallas"]
    dallas_opp, dallas_record, _ = evidence(world, ws, location=fields, addresses=(address,))

    refused = capture(world, ws, no_state_opp, no_state_record)
    assert refused.status_code == 422, refused.text
    assert refused.json()["error"]["code"] == "jurisdiction_unresolved"
    assert model.requests == []
    absent = capture(world, ws, absent_opp, absent_record)
    dallas = capture(world, ws, dallas_opp, dallas_record)
    assert (absent.status_code, dallas.status_code) == (201, 201), (absent.text, dallas.text)
    assert world.row(RecommendationBrief, absent.json()["id"])["jurisdiction_code"] is None
    assert world.row(RecommendationBrief, dallas.json()["id"])["jurisdiction_code"] == code
    assert world.count(RecommendationBrief, opportunity_id=no_state_opp) == 0
    assert len(model.requests) == 2 and world.count(LLMUsageEvent, workspace_id=ws) == 2


#: Where a jurisdiction could be (wrongly) inferred from for a record with no location.
INFERENCE_SOURCES = (
    "the workspace's only location",
    "the scout's and the opportunity's location",
    "market text naming a place",
    "a workspace-level coverage address",
    "all of them",
)


@pytest.mark.parametrize("source", INFERENCE_SOURCES)
def test_q4_an_absent_location_is_never_inferred(capture_world, phase5_on, model, source):
    """Q-4: "Do not infer a jurisdiction." The workspace's one location resolves (US-TX), the
    scout and the opportunity may point at it, the market text may name it, and a
    workspace-level coverage rule may state its address: the record has no location, so the
    brief's code is NULL all the same (review r4 RQ2, "every other source naming a place")."""
    world = capture_world
    ws = world.workspace(f"q4 no inference {source[:24]}")
    fields, address, code = MATRIX_LOCATIONS["dallas"]
    located_opp, located_record, only = evidence(world, ws, location=fields, addresses=(address,))
    assert world.count(BusinessLocation, workspace_id=ws) == 1
    opp, record, _ = evidence(world, ws, location=None)
    scouts, opportunities = ScoutRequest.__table__, Opportunity.__table__
    scout_id = world.row(Opportunity, opp)["scout_request_id"]
    with world.witness.begin() as conn:
        if source in ("the scout's and the opportunity's location", "all of them"):
            conn.execute(update(scouts).where(scouts.c.id == scout_id).values(location_id=only))
            conn.execute(
                update(opportunities).where(opportunities.c.id == opp).values(location_id=only)
            )
        if source in ("market text naming a place", "all of them"):
            conn.execute(
                update(scouts).where(scouts.c.id == scout_id).values(resolved_market="Dallas, TX")
            )
            conn.execute(
                update(opportunities)
                .where(opportunities.c.id == opp)
                .values(resolved_market="Dallas, TX")
            )
    if source in ("a workspace-level coverage address", "all of them"):
        with world.session() as s:
            s.add(
                GeoCoverageRule(
                    organization_id=world.org_of(ws),
                    workspace_id=ws,
                    location_id=None,
                    business_address="500 Main St, Dallas, TX",
                )
            )
    assert world.row(SignalIntelligenceRecord, record)["location_id"] is None
    r = capture(world, ws, opp, record)
    assert r.status_code == 201, r.text
    assert world.row(RecommendationBrief, r.json()["id"])["jurisdiction_code"] is None
    # The control: the same location, bound to the record, is read and resolves.
    bound = capture(world, ws, located_opp, located_record)
    assert bound.status_code == 201, bound.text
    assert world.row(RecommendationBrief, bound.json()["id"])["jurisdiction_code"] == code


def test_q4_an_unresolved_location_never_falls_back_to_another(
    capture_world, phase5_on, model, budget_checks
) -> None:
    """The refusal side of "no inference": a record bound to an existing location that does
    not resolve is refused even when the workspace has another location that resolves."""
    world = capture_world
    ws = world.workspace("q4 no fallback")
    fields, address, _code = MATRIX_LOCATIONS["dallas"]
    evidence(world, ws, location=fields, addresses=(address,))
    opp, record, _ = evidence(
        world, ws, location={"city": "Dallas", "state_province": None, "country": "US"}
    )
    r = capture(world, ws, opp, record)
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "jurisdiction_unresolved"
    assert model.requests == [] and budget_checks == []
    assert world.count(RecommendationBrief, opportunity_id=opp) == 0
    assert _brief_audits(world, ws) == 0


def test_q4_creating_a_brief_back_fills_nothing(capture_world, phase5_on, model) -> None:
    """Q-4: "Do not ... backfill existing records." Creating new briefs, with and without a
    location, leaves every existing brief exactly as it was: a NULL brief of an opportunity
    that has a location stays NULL, and a coded brief of an opportunity with none keeps its
    code (every column is compared)."""
    world = capture_world
    ws = world.workspace("q4 no back-fill")
    fields, address, code = MATRIX_LOCATIONS["dallas"]
    located_opp, located_record, _ = evidence(world, ws, location=fields, addresses=(address,))
    absent_opp, absent_record, _ = evidence(world, ws, location=None)
    existing = [
        world.seed_brief(ws, opportunity_id=located_opp, jurisdiction_code=None),
        world.seed_brief(ws, opportunity_id=absent_opp, jurisdiction_code="GB"),
        world.seed_brief(ws, jurisdiction_code=None),  # another opportunity
        world.seed_brief(ws, jurisdiction_code="US-TX"),  # another opportunity
    ]
    before = [world.row(RecommendationBrief, b) for b in existing]

    located = capture(world, ws, located_opp, located_record)
    absent = capture(world, ws, absent_opp, absent_record)
    assert (located.status_code, absent.status_code) == (201, 201), (located.text, absent.text)
    assert located.json()["brief_version"] == absent.json()["brief_version"] == 2
    assert world.row(RecommendationBrief, located.json()["id"])["jurisdiction_code"] == code
    assert world.row(RecommendationBrief, absent.json()["id"])["jurisdiction_code"] is None
    assert [world.row(RecommendationBrief, b) for b in existing] == before


def test_q4_a_second_version_without_a_location_is_null_and_leaves_the_first(
    capture_world, phase5_on, model
) -> None:
    """A new version for an opportunity with no location is NULL again, and the first
    version's row is unchanged (no back-fill by a later capture either)."""
    world = capture_world
    ws = world.workspace("q4 second version")
    opp, record, _ = evidence(world, ws, location=None)
    first = capture(world, ws, opp, record)
    assert first.status_code == 201, first.text
    v1 = world.row(RecommendationBrief, first.json()["id"])
    second = capture(world, ws, opp, record)
    assert second.status_code == 201, second.text
    v2 = world.row(RecommendationBrief, second.json()["id"])
    assert (v1["brief_version"], v2["brief_version"]) == (1, 2)
    assert v2["parent_brief_id"] == v1["id"]
    assert v1["jurisdiction_code"] is None and v2["jurisdiction_code"] is None
    assert world.row(RecommendationBrief, v1["id"]) == v1


def test_q4_an_absent_capture_is_booked_exactly_as_a_resolved_one(
    capture_world, phase5_on, model
) -> None:
    """Q-4 gives the absent location "a recommendation brief": the same brief, budget, usage
    and audit rows as a resolved capture writes (each the first capture of its workspace),
    only its code is NULL."""
    world = capture_world
    deltas, codes = {}, {}
    for kind in ("absent", "resolved"):
        ws = world.workspace(f"q4 booked {kind}")
        if kind == "absent":
            opp, record, _ = evidence(world, ws, location=None)
        else:
            fields, address, _code = MATRIX_LOCATIONS["dallas"]
            opp, record, _ = evidence(world, ws, location=fields, addresses=(address,))
        before = _full_ledger(world, ws)
        r = capture(world, ws, opp, record)
        assert r.status_code == 201, (kind, r.text)
        after = _full_ledger(world, ws)
        deltas[kind] = {key: after[key] - before[key] for key in before}
        codes[kind] = world.row(RecommendationBrief, r.json()["id"])["jurisdiction_code"]
    assert deltas["absent"] == deltas["resolved"]
    assert deltas["absent"]["briefs"] == 1 and deltas["absent"]["usage events"] == 1
    assert codes == {"absent": None, "resolved": "US-TX"}
    assert len(model.requests) == 2


def _point_record_at_a_missing_location(world: CreativeWorld, record_id: str, ghost: str):
    """Make a record reference a location row that does not exist, bypassing the foreign key
    as only a fault or a manual repair could (``session_replication_role`` needs the
    superuser role CI's PostgreSQL service gives its user).

    As a non-superuser the PostgreSQL case fails loudly with ``InsufficientPrivilege``; it
    never skips. Should CI's test role ever lose superuser, the least-privilege alternative
    needs no change here: ``GRANT SET ON PARAMETER session_replication_role TO <role>``
    (PostgreSQL 15 and later), measured to pass for a ``NOSUPERUSER`` role (W21-REV-5DW3 r4
    part (2), P2-5 and F-8)."""
    table = SignalIntelligenceRecord.__table__
    stmt = update(table).where(table.c.id == record_id).values(location_id=ghost)
    if world.witness.dialect.name == "postgresql":
        with world.witness.begin() as conn:
            conn.exec_driver_sql("SET LOCAL session_replication_role = replica")
            conn.execute(stmt)
        return
    with world.witness.connect() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        conn.execute(stmt)
        conn.commit()
        conn.exec_driver_sql("PRAGMA foreign_keys=ON")


def test_q4_a_dangling_location_reference_is_refused_never_absent(
    capture_world, phase5_on, model, budget_checks, caplog
) -> None:
    """Q-4, implementation reading: "Every non-NULL location_id is an existing location
    reference." One whose row is gone is a fault, not "no location": refused before any
    budget or model use, with nothing written (review r4 RQ1)."""
    world = capture_world
    ws = world.workspace("q4 dangling reference")
    opp, record, _ = evidence(world, ws, location=None)
    ghost = f"gone{uuid.uuid4().hex[:28]}"
    _point_record_at_a_missing_location(world, record, ghost)
    try:
        assert world.row(SignalIntelligenceRecord, record)["location_id"] == ghost
        before = _full_ledger(world, ws)
        with caplog.at_level(logging.INFO):
            r = capture(world, ws, opp, record)
        assert r.status_code == 422, r.text
        assert r.json()["error"]["code"] == "jurisdiction_unresolved"
        assert model.requests == [] and budget_checks == []
        assert _full_ledger(world, ws) == before
        assert ghost not in r.text and ghost not in _logged(caplog)
    finally:
        # Referential integrity again for the rest of the module's world.
        table = SignalIntelligenceRecord.__table__
        with world.witness.begin() as conn:
            conn.execute(update(table).where(table.c.id == record).values(location_id=None))


# --------------------------------------------------------------------------- #
# Item 31 for the absent-location brief
# --------------------------------------------------------------------------- #
def _absent_brief(world: CreativeWorld, ws: str, how: str = "never bound") -> str:
    opp, record = _absent_record(world, ws, how)
    r = capture(world, ws, opp, record)
    assert r.status_code == 201, r.text
    brief_id = r.json()["id"]
    assert world.row(RecommendationBrief, brief_id)["jurisdiction_code"] is None
    return brief_id


@pytest.mark.parametrize("how", ABSENT)
def test_q4_item_31_the_route_refuses_drafts_from_an_absent_location_brief(
    capture_world, phase5_on, model, how
) -> None:
    """Q-4: "no creative draft may be generated ... from that brief". Refused at the generate
    route before and after approval (approval cannot cure it): no job, no model call, no draft,
    no review; and the brief's code is still NULL (nothing is resolved at generation)."""
    world = capture_world
    ws = world.workspace(f"q4 item 31 route {how}")
    brief_id = _absent_brief(world, ws, how)
    calls = len(model.requests)  # the brief's own call
    for approved in (False, True):
        if approved:
            world.decide(brief_id, "approved")
        r = world.generate(brief_id, ws=ws)
        assert r.status_code == 422, (approved, r.text)
        assert r.json()["error"]["code"] == "brief_jurisdiction_missing", approved
    assert _jobs_for(world, ws) == 0
    world.drain()
    assert len(model.requests) == calls
    assert world.count(CreativeDraft, brief_id=brief_id) == 0 and _reviews(world, ws) == 0
    assert world.row(RecommendationBrief, brief_id)["jurisdiction_code"] is None


def test_q4_item_31_one_answer_for_both_kinds_of_null_brief(
    capture_world, phase5_on, model
) -> None:
    """Both kinds of NULL brief, a legacy one (written before capture, its opportunity has a
    location) and a Q-4 one (no location), get the same refusal, and its text is true for
    both: it does not say the brief predates recorded jurisdictions, and invites no retry."""
    world = capture_world
    ws = world.workspace("q4 item 31 one answer")
    q4 = _absent_brief(world, ws)
    world.decide(q4, "approved")
    fields, address, _code = MATRIX_LOCATIONS["dallas"]
    located_opp, _record, _ = evidence(world, ws, location=fields, addresses=(address,))
    legacy = world.seed_brief(ws, opportunity_id=located_opp, jurisdiction_code=None)
    answers = {}
    for kind, brief_id in (("q4", q4), ("legacy", legacy)):
        r = world.generate(brief_id, ws=ws)
        assert r.status_code == 422, (kind, r.text)
        error = r.json()["error"]
        answers[kind] = (error["code"], error["message"])
    assert answers["q4"] == answers["legacy"]
    code, message = answers["q4"]
    assert code == "brief_jurisdiction_missing"
    assert "before jurisdictions were recorded" not in message
    assert "try again" not in message.lower()
    assert _jobs_for(world, ws) == 0


def _enqueue_generation(world: CreativeWorld, brief_id: str) -> str:
    """A ``creative.generate`` job for ``brief_id`` exactly as the generate route builds one,
    but without the route's checks: the job must refuse on its own (defence in depth)."""
    with world.session() as s:
        brief = s.get(RecommendationBrief, brief_id)
        job = enqueue_job(
            s,
            job_type=JobType.CREATIVE_GENERATE,
            context=ExecutionContext(
                organization_id=brief.organization_id,
                workspace_id=brief.workspace_id,
                location_id=brief.location_id,
                campaign_id=brief.campaign_id,
            ),
            payload=CreativeGeneratePayload(
                brief_id=brief.id, channel="social_post", requested_by=world.users["marketer"]
            ).model_dump(mode="json"),
            location_id=brief.location_id,
            idempotency_key=f"w4-q4:{brief.id}",
        )
        return job.id


@pytest.mark.parametrize("how", ABSENT)
def test_q4_item_31_the_job_refuses_an_absent_location_brief_terminally(
    capture_world, phase5_on, model, how
) -> None:
    """The job re-checks item 31 for the absent-location brief: it fails once and for all
    (VALIDATION; a second drain runs nothing) and writes nothing (the full ledger), with no
    model call; the generation locks are released (on PostgreSQL no advisory lock and no
    session idle in a transaction remain, and on both engines a coded brief in the same
    workspace generates next)."""
    world = capture_world
    ws = world.workspace(f"q4 item 31 job {how}")
    brief_id = _absent_brief(world, ws, how)
    world.decide(brief_id, "approved")
    calls = len(model.requests)
    job_id = _enqueue_generation(world, brief_id)
    before = _full_ledger(world, ws)
    assert world.drain() == 1
    job = world.job(job_id)
    assert (
        job["status"],
        job["last_error_code"],
        job["last_error_summary"],
        job["attempt_count"],
    ) == ("failed", "validation", "brief_jurisdiction_missing", 1)
    assert world.drain() == 0  # terminal: nothing is scheduled again
    assert len(model.requests) == calls
    assert world.count(LLMUsageEvent, job_id=job_id) == 0
    assert world.count(CreativeDraft, brief_id=brief_id) == 0 and _reviews(world, ws) == 0
    assert _full_ledger(world, ws) == before
    if world.engine.dialect.name == "postgresql":
        with world.witness.connect() as conn:
            held = conn.exec_driver_sql(
                "SELECT count(*) FROM pg_locks l JOIN pg_database d ON d.oid = l.database "
                "WHERE l.locktype = 'advisory' AND d.datname = current_database()"
            ).scalar_one()
            idle = conn.exec_driver_sql(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                "AND state IN ('idle in transaction', 'idle in transaction (aborted)')"
            ).scalar_one()
        assert (held, idle) == (0, 0)
    other = world.seed_brief(ws, jurisdiction_code="US-TX")
    ran = world.generate(other, ws=ws)
    assert ran.status_code == 202, ran.text
    world.drain()
    assert world.job(ran.json()["job_id"])["status"] == "succeeded"
    assert len(model.requests) == calls + 1  # the second job's call only
    again = world.job(job_id)
    assert (again["status"], again["attempt_count"]) == ("failed", 1)
    assert world.row(RecommendationBrief, brief_id)["jurisdiction_code"] is None


# =========================================================================== #
# The final structured-address and clarification contract (#313, integration v3).
#
# The instruction of 2026-11-13: "wire in the final structured-address and clarification
# contracts". The capture calls ``resolve_for_action`` (W21 ADDR, #316) instead of the reader,
# and its 422 names the check's category (#316's published body, "The change #313's capture
# needs", items 1-9; review W21-REV-ADDR r1 BC-1..BC-4; checklist I-10, INFO-6). Rows T-1 to
# T-11 of INTEGRATION-PLAN-v3 §2.5 (T-8, the split G0 guard, is in
# ``test_business_address_action``), on SQLite and, with ``TEST_POSTGRES_URL``, PostgreSQL.
# =========================================================================== #
#: The API's ``app`` package (product modules outside ``tests``).
APP_DIR = Path(__file__).resolve().parents[1]
DALLAS_FIELDS = MATRIX_LOCATIONS["dallas"][0]
#: Dallas's complete structured business address: its own country, state and city.
DALLAS_COMPLETE = complete_address_of(DALLAS_FIELDS)

#: One free text per category of the free-text reader (``resolver.CLARIFICATIONS``), each asked
#: for the Dallas, TX location with its complete structured address (the check's step 5). A
#: category added to ``resolver.CLARIFICATIONS`` needs one text here, the adding lane's own;
#: ``test_the_reader_samples_cover_the_readers_vocabulary`` fails until it is given. RI-1's
#: (GEO-P4, #321) is GEO-P4's own row for its Dallas location, from the region-slot test of
#: ``test_geography_territory``.
READER_SAMPLES = {
    ADDRESS_SUFFIX_AMBIGUOUS: "1500 Broadway Hartford CT",
    ADDRESS_NAMES_ANOTHER_PLACE: "Victoria, 1 Main St, Dallas, TX",
    ADDRESS_PLACE_UNCLEAR: "Washington, 1 Main St, Dallas",
    ADDRESS_DIRECTION_AMBIGUOUS: "1200 Main St, SE",
    ADDRESS_ROUTE_AMBIGUOUS: "1 FM 1960",
    ADDRESS_TERRITORY_UNCLEAR: "1 Main St, Dallas, Texas Territory, USA",
}

#: (category, location fields, coverage addresses, structured fields for ``evidence``): every
#: category of the check's closed vocabulary, the structured-address ones from the fields and
#: the location, the reader's from free text.
CATEGORY_CAPTURES = [
    (LOCATION_UNRESOLVED, {"city": "Quillbrook", "state_province": None, "country": "US"}, (),
     True),
    (BUSINESS_ADDRESS_INCOMPLETE, DALLAS_FIELDS, ("Dallas, TX",), False),
    (BUSINESS_ADDRESS_CONFLICT, DALLAS_FIELDS, (),
     {**DALLAS_COMPLETE, "business_address_region": "OK"}),
    (BUSINESS_ADDRESS_TEXT_CONFLICT, DALLAS_FIELDS, ("Tulsa, OK",), True),
    (BUSINESS_ADDRESS_LOCALITY_UNCONFIRMED, DALLAS_FIELDS, (),
     {**DALLAS_COMPLETE, "business_address_locality": "Irving"}),
    *((category, DALLAS_FIELDS, (text,), True) for category, text in READER_SAMPLES.items()),
]  # fmt: skip
#: Customer text of those rows, none of which may reach the answer or the logs.
_CUSTOMER_TEXT = ("Quillbrook", "Dallas", "Tulsa", "Irving", *READER_SAMPLES.values())


def _refusal_logs(caplog) -> list:
    return [r for r in caplog.records if r.getMessage() == "recommendation_brief_not_generated"]


def test_the_reader_samples_cover_the_readers_vocabulary() -> None:
    """The T-1 rows reach every category of the closed vocabulary (a tripwire for RI-1)."""
    assert set(READER_SAMPLES) == set(CLARIFICATIONS)
    assert {row[0] for row in CATEGORY_CAPTURES} == set(ACTION_CLARIFICATIONS)
    assert len(CATEGORY_CAPTURES) == len(ACTION_CLARIFICATIONS)


@pytest.mark.parametrize(
    ("category", "location", "addresses", "structured"),
    CATEGORY_CAPTURES,
    ids=[row[0] for row in CATEGORY_CAPTURES],
)
def test_t1_every_category_reaches_the_422_with_its_reason_before_any_model_or_budget_use(
    capture_world, phase5_on, model, budget_checks, caplog, category, location, addresses,
    structured,
):
    """T-1, T-3, T-10 (ADDR items 2, 3 and 7; BC-3): each category of the check reaches the
    422 as ``details.reason`` with exactly its fixed message, before any model call or budget
    use and with no row of any kind; the answer holds nothing else (no customer text, no
    evidence), and the refusal's log line names the category and nothing more."""
    world = capture_world
    ws = world.workspace(f"t1 {category[:34]}")
    opp, record, _ = evidence(
        world, ws, location=location, addresses=addresses, structured=structured
    )
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
    assert set(r.json()) == {"error"}
    assert model.requests == [] and budget_checks == []
    assert world.count(RecommendationBrief, opportunity_id=opp) == 0
    assert _full_ledger(world, ws) == before
    (logged,) = _refusal_logs(caplog)
    assert logged.extra_fields == {
        "outcome": "refused",
        "workspace_id": ws,
        "opportunity_id": opp,
        "reason": category,
    }
    everything = _logged(caplog)
    for text in _CUSTOMER_TEXT:
        assert text not in everything, text
    assert "unresolved:" not in r.text and "unresolved:" not in everything


@pytest.mark.parametrize(
    "reason", [None, "address_from_a_later_release", "1 Main St, Quillbrook"], ids=repr
)
def test_a_category_outside_the_vocabulary_is_never_named(
    capture_world, phase5_on, model, caplog, reason
) -> None:
    """RV-A13, redefined for the clarification contract (INTEGRATION-PLAN-v3 §2.5): the answer
    never names a reason outside the closed vocabulary, nor any text the check carries. An
    UNRESOLVED check with no category or another one (``resolve_for_action`` returns neither)
    gets the generic answer with no ``details``, and the log its fixed reason."""
    world = capture_world
    ws = world.workspace("outside the vocabulary")
    opp, record, _ = evidence(world, ws, location=DALLAS_FIELDS, addresses=("Dallas, TX",))
    forged = JurisdictionResolution(
        code=UNRESOLVED, codes=(), confidence=0.0,
        evidence=("unresolved: Quillbrook evidence",), reason=reason,
    )  # fmt: skip
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(brief_service, "resolve_for_action", lambda location, rules=(): forged)
        with caplog.at_level(logging.INFO):
            r = capture(world, ws, opp, record)
    assert r.status_code == 422, r.text
    error = r.json()["error"]
    assert set(error) == {"code", "message", "request_id"}
    assert error["message"] == brief_service.JURISDICTION_UNRESOLVED_MESSAGE
    (logged,) = _refusal_logs(caplog)
    assert logged.extra_fields["reason"] == brief_service.UNRESOLVED_OUTSIDE_VOCABULARY
    assert "Quillbrook" not in r.text and "Quillbrook" not in _logged(caplog)
    assert model.requests == [] and world.count(RecommendationBrief, opportunity_id=opp) == 0


#: (case, location fields, coverage addresses, structured fields, where the location lives):
#: locations outside the opportunity's workspace or organization whose own data would give a
#: category, or resolve, if they were the workspace's own.
FOREIGN_CAPTURES = [
    ("another workspace's location that would be location_unresolved",
     {"city": "Quillbrook", "state_province": None, "country": "US"}, (), True, "ws2"),
    ("another organization's location that would be a text refusal",
     DALLAS_FIELDS, ("Tulsa, OK",), True, "ws_b"),
    ("another organization's location that would ask", DALLAS_FIELDS, ("1 FM 1960",), True,
     "ws_b"),
    ("another workspace's location that would resolve", DALLAS_FIELDS, ("Dallas, TX",), True,
     "ws2"),
    ("a row of this workspace naming another organization, that would resolve",
     DALLAS_FIELDS, ("Dallas, TX",), True, "row_of_org_b"),
]  # fmt: skip


def test_t2_the_tenant_refusal_keeps_its_generic_answer_and_names_no_category(
    capture_world, phase5_on, model, budget_checks, caplog
) -> None:
    """T-2 (ADDR item 3: "The fixed JURISDICTION_UNRESOLVED_MESSAGE stays for the tenant
    refusal"): a location outside the opportunity's workspace or organization is refused with
    the one generic answer and no ``details``, whatever its own data would give, so the
    per-category answer reveals nothing about another tenant's location (R-5D B-8). The
    tenant check comes first: the check never reads the foreign location."""
    world = capture_world
    ws = world.workspace("t2 tenant")
    checked: list = []
    real = brief_service.resolve_for_action

    def spy(location, rules=()):
        checked.append(location.id)
        return real(location, rules)

    answers = set()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(brief_service, "resolve_for_action", spy)
        for case, location, addresses, structured, where in FOREIGN_CAPTURES:
            opp, record, location_id = evidence(
                world, ws, location=location, addresses=addresses, structured=structured,
                location_ws=getattr(world, where) if where in ("ws2", "ws_b") else None,
                location_org=world.org_b if where == "row_of_org_b" else None,
            )  # fmt: skip
            caplog.clear()
            with caplog.at_level(logging.INFO):
                r = capture(world, ws, opp, record)
            assert r.status_code == 422, (case, r.text)
            error = r.json()["error"]
            assert set(error) == {"code", "message", "request_id"}, case
            answers.add((error["code"], error["message"]))
            (logged,) = _refusal_logs(caplog)
            assert logged.extra_fields["reason"] == brief_service.OUTSIDE_WORKSPACE, case
            assert world.count(RecommendationBrief, opportunity_id=opp) == 0
    assert answers == {("jurisdiction_unresolved", brief_service.JURISDICTION_UNRESOLVED_MESSAGE)}
    assert checked == [] and model.requests == [] and budget_checks == []


#: (case, fields of a coverage rule row that another tenant holds for this tenant's location):
#: each would refuse or ask if the capture read it.
FOREIGN_RULES = [
    ("a text naming another state", {"business_address": "Tulsa, OK"}),
    ("a text that asks", {"business_address": "1 FM 1960"}),
    (
        "a structured address of another state",
        complete_address_of({"city": "Tulsa", "state_province": "OK", "country": "US"}),
    ),
]


@pytest.mark.parametrize(("case", "fields"), FOREIGN_RULES, ids=[c for c, _ in FOREIGN_RULES])
@pytest.mark.parametrize("where", ["ws2", "ws_b"])
def test_t2_a_rule_row_of_another_tenant_never_decides_the_capture(
    capture_world, phase5_on, model, case, fields, where
) -> None:
    """R-5D-B r1 F-1 (beside T-2): the capture reads the location's coverage rules of the
    opportunity's workspace only. ``geo_coverage_rules`` does not tie a rule's workspace to its
    location's, so a row of another workspace (``ws2``) or organization (``ws_b``) can name this
    tenant's location; it never decides, or is named in, this tenant's answer."""
    world = capture_world
    ws = world.workspace(f"t2 rules {where} {case[:12]}")
    opp, record, location_id = evidence(
        world, ws, location=DALLAS_FIELDS, addresses=("Dallas, TX",)
    )
    home = getattr(world, where)
    with world.session() as s:
        s.add(
            GeoCoverageRule(
                location_id=location_id,
                organization_id=world.org_of(home),
                workspace_id=home,
                **fields,
            )
        )
    r = capture(world, ws, opp, record)
    assert r.status_code == 201, (case, where, r.text)
    assert world.row(RecommendationBrief, r.json()["id"])["jurisdiction_code"] == "US-TX"


def _scout_rule_of(world: CreativeWorld, ws: str, location_id: str):
    """The coverage rule a Scout search of ``location_id`` is run with (``jobs.pipeline``)."""
    with world.session() as s:
        location = s.get(BusinessLocation, location_id)
        request = ScoutRequest(
            id=f"sr{uuid.uuid4().hex[:12]}", organization_id=location.organization_id,
            workspace_id=ws, brand_id=location.brand_id, location_id=location_id,
            name="Scout", status="queued", source_types=[], keywords=["parking"],
        )  # fmt: skip
        s.add(request)
        s.flush()
        rule = _coverage_rule_for(s, request)
        s.rollback()
    return rule


def test_t4_empty_fields_or_a_missing_town_refuse_the_capture_but_not_the_scout(
    capture_world, phase5_on, model
) -> None:
    """T-4 (ADDR item 7; the operator: "Existing Scout searches must remain usable; the new
    completeness requirement applies to jurisdiction-dependent actions"): empty structured
    fields, or every field but the town of a location that has a city, are refused with
    ``business_address_incomplete``, and the coverage rule a Scout search of that same
    location runs with is the one a complete address gives. The Scout run itself, with these
    fields, is pinned by ADDR's ``test_structured_address_scout`` (S-2)."""
    world = capture_world
    ws = world.workspace("t4 incomplete")
    no_town = {k: v for k, v in DALLAS_COMPLETE.items() if k != "business_address_locality"}
    scout_rules = []
    for structured in (False, no_town, DALLAS_COMPLETE):
        opp, record, location_id = evidence(
            world, ws, location=DALLAS_FIELDS, addresses=("Dallas, TX",), structured=structured
        )
        r = capture(world, ws, opp, record)
        if structured is DALLAS_COMPLETE:
            assert r.status_code == 201, r.text
        else:
            assert r.status_code == 422, r.text
            assert r.json()["error"]["details"] == {"reason": BUSINESS_ADDRESS_INCOMPLETE}
        scout_rules.append(_scout_rule_of(world, ws, location_id))
    assert len({repr(rule) for rule in scout_rules}) == 1
    assert scout_rules[0] is not None


def _location_rule(world: CreativeWorld, location_id: str) -> str:
    (rule,) = world.rows(GeoCoverageRule, location_id=location_id)
    return rule["id"]


def _confirm(world: CreativeWorld, ws: str, location_id: str, confirmed: bool = True):
    return world.send(
        "PUT",
        f"/workspaces/{ws}/locations/{location_id}/geo-coverage/locality-confirmation",
        "marketer",
        {"confirmed": confirmed},
    )


def test_t5_a_confirmed_town_resolves_and_the_confirmation_lapses_on_any_change(
    capture_world, phase5_on, model
) -> None:
    """T-5 (ADDR item 7): a town that differs from the location's city asks
    (``business_address_locality_unconfirmed``) until the customer confirms it through the
    Locations API; then the capture stores the location's code. The confirmation binds the town,
    the city and the code: a change to any of them makes the capture ask again."""
    world = capture_world
    ws = world.workspace("t5 confirmed town")
    irving = {**DALLAS_COMPLETE, "business_address_locality": "Irving"}
    opp, record, location_id = evidence(
        world, ws, location=DALLAS_FIELDS, addresses=(), structured=irving
    )
    unconfirmed = capture(world, ws, opp, record)
    assert unconfirmed.status_code == 422, unconfirmed.text
    assert unconfirmed.json()["error"]["details"] == {
        "reason": BUSINESS_ADDRESS_LOCALITY_UNCONFIRMED
    }
    assert _confirm(world, ws, location_id).status_code == 200
    resolved = capture(world, ws, opp, record)
    assert resolved.status_code == 201, resolved.text
    assert world.row(RecommendationBrief, resolved.json()["id"])["jurisdiction_code"] == "US-TX"
    rule_id = _location_rule(world, location_id)
    rules, locations = GeoCoverageRule.__table__, BusinessLocation.__table__
    for table, row_id, change, restore in (
        (rules, rule_id, {"business_address_locality": "Plano"},
         {"business_address_locality": "Irving"}),
        (locations, location_id, {"city": "Fort Worth"}, {"city": "Dallas"}),
        # The code: the location and its address move to Oklahoma together (they agree).
        (None, None, {"state_province": "OK", "business_address_region": "OK"},
         {"state_province": "TX", "business_address_region": "TX"}),
    ):  # fmt: skip
        with world.witness.begin() as conn:
            if table is None:
                conn.execute(update(locations).where(locations.c.id == location_id)
                             .values(state_province=change["state_province"]))
                conn.execute(update(rules).where(rules.c.id == rule_id)
                             .values(business_address_region=change["business_address_region"]))
            else:
                conn.execute(update(table).where(table.c.id == row_id).values(**change))
        lapsed = capture(world, ws, opp, record)
        assert lapsed.status_code == 422, (change, lapsed.text)
        assert lapsed.json()["error"]["details"] == {
            "reason": BUSINESS_ADDRESS_LOCALITY_UNCONFIRMED
        }, change
        with world.witness.begin() as conn:
            if table is None:
                conn.execute(update(locations).where(locations.c.id == location_id)
                             .values(state_province=restore["state_province"]))
                conn.execute(update(rules).where(rules.c.id == rule_id)
                             .values(business_address_region=restore["business_address_region"]))
            else:
                conn.execute(update(table).where(table.c.id == row_id).values(**restore))
        again = capture(world, ws, opp, record)  # restored: the confirmation binds again
        assert again.status_code == 201, (change, again.text)
    assert world.count(RecommendationBrief, opportunity_id=opp) == 4


def test_t6_a_contradiction_is_refused_even_with_a_stored_confirmation(
    capture_world, phase5_on, model
) -> None:
    """T-6 (ADDR item 7): a confirmation answers only the town. A structured contradiction or a
    free text that names another place is refused with its own category even while a
    confirmation is stored, and the refusal says it cannot be confirmed away."""
    world = capture_world
    ws = world.workspace("t6 contradiction")
    irving = {**DALLAS_COMPLETE, "business_address_locality": "Irving"}
    rules = GeoCoverageRule.__table__
    for column, value, category in (
        ("business_address_region", "OK", BUSINESS_ADDRESS_CONFLICT),
        ("business_address", "Tulsa, OK", BUSINESS_ADDRESS_TEXT_CONFLICT),
    ):
        opp, record, location_id = evidence(
            world, ws, location=DALLAS_FIELDS, addresses=(), structured=irving
        )
        assert _confirm(world, ws, location_id).status_code == 200
        assert capture(world, ws, opp, record).status_code == 201  # the confirmation applies
        rule_id = _location_rule(world, location_id)
        with world.witness.begin() as conn:
            conn.execute(update(rules).where(rules.c.id == rule_id).values({column: value}))
        assert world.row(GeoCoverageRule, rule_id)["locality_confirmed_at"] is not None
        r = capture(world, ws, opp, record)
        assert r.status_code == 422, r.text
        assert r.json()["error"]["details"] == {"reason": category}
        assert category in REFUSALS
        assert r.json()["error"]["message"].endswith(
            "This cannot be confirmed instead of corrected."
        )


#: T-7 / S-10: W21-REV-5D-INTEG r1's rows (§2.2: the 13 the battery pins; §2.3: the register's
#: examples), its probe ``capture_rows_at_head.py`` (``4e602070…``), each location's rules
#: holding its complete structured address, and the capture's answer at #313's integrated head:
#: a code (stored) or the check's category (refused or asked). Lane evidence raw/070.
_US_TX, _NG_LA = ("Dallas", "TX", "US"), ("Lagos", "Lagos State", "NG")
R1_ROWS = [
    # §2.2, the 13 pinned rows
    ("matrix dallas", ("Dallas", "TX", "US"), ("Dallas, TX",), "US-TX"),
    ("matrix london", ("London", None, "United Kingdom"), ("London, UK",), "GB"),
    ("matrix lagos", ("Lagos", "Lagos State", "Nigeria"), ("Ikeja, Lagos, NG",), "NG-LA"),
    ("matrix nairobi", ("Nairobi", None, "Kenya"), ("Nairobi, KE",), "KE"),
    ("conflict Tulsa, OK", ("Quillbrook", "TX", "US"), ("Tulsa, OK",),
     BUSINESS_ADDRESS_TEXT_CONFLICT),
    ("second rule conflicts", ("Quillbrook", "TX", "US"), ("Austin, TX", "Tulsa, OK"),
     BUSINESS_ADDRESS_TEXT_CONFLICT),
    ("Ogun for Lagos", _NG_LA, ("Abeokuta, Ogun State, Nigeria",),
     BUSINESS_ADDRESS_TEXT_CONFLICT),
    ("Lagos, NG for Nairobi", ("Nairobi", None, "Kenya"), ("Lagos, NG",),
     BUSINESS_ADDRESS_TEXT_CONFLICT),
    ("Dallas, Texas for London", ("London", "England", "United Kingdom"), ("Dallas, Texas",),
     BUSINESS_ADDRESS_TEXT_CONFLICT),
    ("None then Dallas, TX for London", ("London", None, "GB"), (None, "Dallas, TX"),
     BUSINESS_ADDRESS_TEXT_CONFLICT),
    ("Q-1 move: Tulsa", ("Tulsa", "OK", "US"), ("Tulsa, OK",), "US-OK"),
    ("Q-1 move: Dallas, US no state", ("Dallas", None, "US"), ("Dallas, US",),
     LOCATION_UNRESOLVED),
    # The workspace-level rule is not the location's: with no rule of its own the location's
    # address is incomplete (ADDR step 4: "no rule at all").
    ("workspace-level address (not read by capture)", _US_TX, "workspace-level",
     BUSINESS_ADDRESS_INCOMPLETE),
    # §2.3, the register's examples (G-1..G-10), and D1
    ("D1: street only", _US_TX, ("123 Main St",), "US-TX"),
    ("G-1 depth 4 (2,928 shape)", _US_TX, ("US-AL; Montgomery; 1 Main St; 1 Main St",),
     BUSINESS_ADDRESS_TEXT_CONFLICT),
    ("G-1 commas", _US_TX, ("US-AL, Montgomery, Bavaria, Suite 5",),
     BUSINESS_ADDRESS_TEXT_CONFLICT),
    ("G-4 leading other area", _US_TX, ("Oklahoma, US-TX, Texas, USA",),
     ADDRESS_NAMES_ANOTHER_PLACE),
    ("G-6 trailing unit hides country", _US_TX, ("1 Main St, Toronto, Canada, Suite 5",),
     BUSINESS_ADDRESS_TEXT_CONFLICT),
    ("G-6 floor read as FL (own)", _US_TX, ("123 Main St, Fl 3",), "US-TX"),
    ("G-7 country-prefixed postcode", _US_TX, ("ACME, Flat 3, NO-0150 Oslo",),
     BUSINESS_ADDRESS_TEXT_CONFLICT),
    ("G-9 code in a middle part", _US_TX, ("ACME, NG, Lagos",), BUSINESS_ADDRESS_TEXT_CONFLICT),
    ("G-9 KE before town", _US_TX, ("KE, Nairobi",), BUSINESS_ADDRESS_TEXT_CONFLICT),
    ("G-2 own DC row", ("Washington", "DC", "US"), ("US-DC, Washington, District of Columbia",),
     ADDRESS_PLACE_UNCLEAR),
    ("G-3 own two-word row", ("Montgomery", "AL", "US"), ("US-AL, Downtown, Montgomery",),
     ADDRESS_PLACE_UNCLEAR),
    ("G-8 own route designator", ("Seattle", "WA", "US"), ("1 SR 520",), ADDRESS_ROUTE_AMBIGUOUS),
    ("G-8 route contradiction (London loc)", ("London", None, "GB"),
     ("ACME Corp, Suite 200, 4400 US Hwy 75",), BUSINESS_ADDRESS_TEXT_CONFLICT),
    ("G-9 state before town (Lagos loc)", _NG_LA, ("Ogun State, Abeokuta",),
     BUSINESS_ADDRESS_TEXT_CONFLICT),
]  # fmt: skip


def test_t7_r1s_rows_with_complete_fields_through_the_capture(
    capture_world, phase5_on, model
) -> None:
    """T-7 / S-10 (ADDR item 8; checklist I-10's re-check list): the 13 rows the battery pins
    and r1 §2.3's register examples, captured through the route with complete structured
    fields. Every contradicting form is refused or asks; every own form resolves or asks; the
    13 pinned rows keep their pinned answer (a code, or a refusal now naming its category)."""
    world = capture_world
    ws = world.workspace("t7 r1 rows")
    assert len(R1_ROWS) == 27
    outcomes = {}
    for case, (city, state, country), addresses, _expected in R1_ROWS:
        fields = {"city": city, "state_province": state, "country": country}
        if addresses == "workspace-level":
            opp, record, _ = evidence(world, ws, location=fields, structured=False)
            with world.session() as s:
                s.add(GeoCoverageRule(
                    organization_id=world.org_of(ws), workspace_id=ws, location_id=None,
                    business_address="500 Main St, Dallas, TX", **complete_address_of(fields),
                ))  # fmt: skip
        else:
            opp, record, _ = evidence(world, ws, location=fields, addresses=addresses)
        r = capture(world, ws, opp, record)
        if r.status_code == 201:
            outcomes[case] = world.row(RecommendationBrief, r.json()["id"])["jurisdiction_code"]
        else:
            assert r.status_code == 422, (case, r.text)
            outcomes[case] = r.json()["error"]["details"]["reason"]
    assert outcomes == {case: expected for case, _l, _a, expected in R1_ROWS}


def _ordered_rules(world: CreativeWorld, location_id: str, ws: str, rules: list[dict]) -> None:
    """Coverage rules of the location in an explicit (created_at, id) order."""
    with world.session() as s:
        for index, fields in enumerate(rules):
            s.add(GeoCoverageRule(
                id=f"r{index}-{uuid.uuid4().hex[:8]}", organization_id=world.org_of(ws),
                workspace_id=ws, location_id=location_id,
                created_at=datetime(2026, 1, 1, 0, 0, index, tzinfo=UTC), **fields,
            ))  # fmt: skip


def test_t9_with_several_rules_the_first_failing_step_then_the_first_failing_rule_decides(
    capture_world, phase5_on, model
) -> None:
    """T-9 (ADDR design v3 §4.1; review d1 C-2): each step runs over every rule, in the
    capture's (created_at, id) order, before the next: a later rule's earlier step decides over
    an earlier rule's later step (2 over 5 and 6, 3 over 4, 4 over 5, 5 over 6), and within one
    step the earlier rule decides."""
    world = capture_world
    ws = world.workspace("t9 several rules")
    town = {**DALLAS_COMPLETE, "business_address_locality": "Irving"}
    conflict = {**DALLAS_COMPLETE, "business_address_region": "OK"}
    for rules, category in (
        # step 6 (rule 0), step 5 (rule 1), step 2 (rule 2): the structured conflict decides
        ([town, {**DALLAS_COMPLETE, "business_address": "1 FM 1960"}, conflict],
         BUSINESS_ADDRESS_CONFLICT),
        # two of step 5: the first rule's category decides
        ([{**DALLAS_COMPLETE, "business_address": "1200 Main St, SE"},
          {**DALLAS_COMPLETE, "business_address": "1 FM 1960"}], ADDRESS_DIRECTION_AMBIGUOUS),
        ([{**DALLAS_COMPLETE, "business_address": "1 FM 1960"},
          {**DALLAS_COMPLETE, "business_address": "1200 Main St, SE"}], ADDRESS_ROUTE_AMBIGUOUS),
        # step 5 (rule 0) against step 4 (rule 1): completeness decides
        ([{**DALLAS_COMPLETE, "business_address": "1 FM 1960"}, {}],
         BUSINESS_ADDRESS_INCOMPLETE),
        # step 4 (rule 0: empty fields) against step 3 (rule 1: a text naming another state):
        # the text refusal decides (review r2 INFO-1)
        ([{}, {**DALLAS_COMPLETE, "business_address": "Tulsa, OK"}],
         BUSINESS_ADDRESS_TEXT_CONFLICT),
        # step 6 (rule 0: an unconfirmed town) against step 5 (rule 1: an ambiguous text): the
        # text's question decides, so a town is never asked about before the text (INFO-1)
        ([town, {**DALLAS_COMPLETE, "business_address": "1 FM 1960"}],
         ADDRESS_ROUTE_AMBIGUOUS),
    ):  # fmt: skip
        opp, record, location_id = evidence(
            world, ws, location=DALLAS_FIELDS, structured=False
        )
        _ordered_rules(world, location_id, ws, rules)
        r = capture(world, ws, opp, record)
        assert r.status_code == 422, r.text
        assert r.json()["error"]["details"] == {"reason": category}, rules


def test_t11_q4_no_location_is_a_null_brief_with_no_check(
    capture_world, phase5_on, model
) -> None:
    """T-11 (Q-4, unchanged): a record with no location gets a brief with jurisdiction NULL,
    decided from the reference itself: the capture's check is never called."""
    world = capture_world
    ws = world.workspace("t11 q4")
    checked: list = []

    def spy(*args, **kwargs):
        checked.append(args)
        raise AssertionError("the check must not run for a record with no location")

    opp, record, _ = evidence(world, ws, location=None)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(brief_service, "resolve_for_action", spy)
        r = capture(world, ws, opp, record)
    assert r.status_code == 201, r.text
    assert world.row(RecommendationBrief, r.json()["id"])["jurisdiction_code"] is None
    assert checked == []


def test_only_the_capture_refusal_carries_error_details() -> None:
    """N-6: ``SignalNestError`` gained ``details`` for the capture's category only. No error
    class sets it, and the one product raise that passes it is the capture's refusal."""
    import app.main  # noqa: F401 - every router and service module loaded

    def subclasses(cls):
        for sub in cls.__subclasses__():
            yield sub
            yield from subclasses(sub)

    classes = [SignalNestError, *subclasses(SignalNestError)]
    assert len(classes) > 20
    assert {c.__qualname__ for c in classes if c.details is not None} == set()
    passing = set()
    for path in APP_DIR.rglob("*.py"):
        if "tests" in path.parts:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call) and any(k.arg == "details" for k in node.keywords):
                passing.add(path.relative_to(APP_DIR).as_posix())
    assert passing == {"briefs/service.py"}
    # Nor does any product statement store it afterwards (``err.details = ...`` or
    # ``setattr(err, "details", ...)``): only SignalNestError.__init__ does
    # (review W21-R5D-A r1, F-2).
    # The same check is review W21-R5D-B r1's F-3: a 404 given another workspace's id by
    # attribute. A Store context covers plain, augmented and annotated assignments alike.
    stored = set()
    for path in APP_DIR.rglob("*.py"):
        if "tests" in path.parts:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Attribute)
                and node.attr == "details"
                and isinstance(node.ctx, ast.Store)
            ) or (
                isinstance(node, ast.Call)
                and getattr(node.func, "id", None) == "setattr"
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value == "details"
            ):
                stored.add(path.relative_to(APP_DIR).as_posix())
    assert stored == {"core/errors.py"}
    assert SignalNestError("x").details is None
    assert SignalNestError("x", details={"reason": "r"}).details == {"reason": "r"}


def test_the_route_documents_every_category_of_the_vocabulary() -> None:
    """The contract names the closed vocabulary (INTEGRATION-PLAN-v3 §2.6): every key of
    ``ACTION_CLARIFICATIONS`` appears in the brief route's published description, so a category
    added to the reader (RI-1) fails here until the route names it and the contract is
    regenerated."""
    description = create_opportunity_brief.__doc__ or ""
    published = json.loads((APP_DIR.parent / "openapi.json").read_text())["paths"][
        "/api/v1/workspaces/{workspace_id}/opportunities/{opportunity_id}/briefs"
    ]["post"]["description"]
    for category in ACTION_CLARIFICATIONS:
        assert f"``{category}``" in description, category
        assert f"``{category}``" in published, category
    assert "no ``error.details``" in description
