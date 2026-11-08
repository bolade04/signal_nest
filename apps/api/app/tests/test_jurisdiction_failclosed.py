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

import json
import logging
import uuid
from collections.abc import Iterator
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import func, select, update

from app.brands.service import get_primary_brand
from app.briefs import service as brief_service
from app.briefs.models import RecommendationBrief
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
from app.creative.models import CreativeDraft
from app.geography.places_data import COUNTRIES
from app.intelligence.records import SignalIntelligenceRecord
from app.jobs.models import Job
from app.jurisdiction.codes import UNRESOLVED, is_jurisdiction_code
from app.jurisdiction.resolver import (
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
#   spent and no brief is stored", and an absent location is refused too.
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
    addresses: tuple[str, ...] = (),
    location_ws: str | None = None,
    location_org: str | None = None,
) -> tuple[str, str, str | None]:
    """One opportunity and its intelligence record in ``ws``, bound to a new location (in
    ``location_ws``, default ``ws``) with a coverage rule per address; ``location=None`` binds
    none. Returns ``(opportunity id, record id, location id)``."""
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
            for address in addresses:
                s.add(
                    GeoCoverageRule(
                        location_id=location_id, business_address=address, **home_scoped
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


def _logged(caplog) -> str:
    return "\n".join(json.dumps(r.__dict__, default=str) for r in caplog.records)


#: (case, location fields or None, coverage addresses, where the location lives). Every
#: location text is distinctive, so its absence from the answer and the logs is checkable.
UNRESOLVED_CAPTURES = [
    ("no location", None, (), None),
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
    # A location row in this workspace that names another organization: not the
    # opportunity's location, so treated as no location.
    (
        "a location row of this workspace naming another organization",
        {"city": "Quillbrook", "state_province": "TX", "country": "US"},
        (),
        "row_of_org_b",
    ),
]
_LOCATION_TEXT = ("Quillbrook", "Quillshire", "Quillandia", "Tulsa", "Austin")


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
    location_ws = getattr(world, where) if where in ("ws2", "ws_b") else None
    location_org = world.org_b if where == "row_of_org_b" else None
    opp, record, _location_id = evidence(
        world,
        ws,
        location=location,
        addresses=addresses,
        location_ws=location_ws,
        location_org=location_org,
    )
    with caplog.at_level(logging.INFO):
        r = capture(world, ws, opp, record)
    assert r.status_code == 422, (case, r.text)
    error = r.json()["error"]
    assert error["code"] == "jurisdiction_unresolved"
    # The wording is not pinned (it may be reworded); that it is one fixed answer is, below.
    assert isinstance(error["message"], str) and error["message"].strip()
    # Nothing was guessed, called, budgeted or stored.
    assert model.requests == [] and budget_checks == []
    assert world.count(RecommendationBrief, opportunity_id=opp) == 0
    assert _ledger(world, ws) == (0, 0, 0)
    # No location text, and no resolver evidence, in the answer or the logs.
    logged = _logged(caplog)
    for text in _LOCATION_TEXT:
        assert text not in r.text and text not in logged, text
    assert "unresolved:" not in r.text and "unresolved:" not in logged
    refusals = [
        rec for rec in caplog.records if rec.getMessage() == "recommendation_brief_not_generated"
    ]
    assert refusals and refusals[-1].extra_fields["outcome"] == "refused"


def test_every_unresolved_capture_gets_one_fixed_answer(capture_world, phase5_on) -> None:
    """Whatever made the location unresolvable, the answer is the same: it cannot reveal which
    fact was missing or wrong (no resolver evidence)."""
    world = capture_world
    ws = world.workspace("refuse fixed answer")
    answers = set()
    for _case, location, addresses, _where in UNRESOLVED_CAPTURES[:6]:
        opp, record, _ = evidence(world, ws, location=location, addresses=addresses)
        r = capture(world, ws, opp, record)
        assert r.status_code == 422, r.text
        error = r.json()["error"]
        answers.add((error["code"], error["message"]))
    assert len(answers) == 1


def test_a_deleted_location_refuses_the_brief_as_no_location(
    capture_world, phase5_on, model, budget_checks
) -> None:
    """A record whose location was deleted after it was written (``location_id`` is SET NULL
    by the foreign key): no location, so no jurisdiction, so no brief."""
    world = capture_world
    ws = world.workspace("refuse deleted location")
    fields, address, _code = MATRIX_LOCATIONS["dallas"]
    opp, record, location_id = evidence(world, ws, location=fields, addresses=(address,))
    with world.session() as s:
        s.delete(s.get(BusinessLocation, location_id))
    assert world.row(SignalIntelligenceRecord, record)["location_id"] is None
    r = capture(world, ws, opp, record)
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "jurisdiction_unresolved"
    assert model.requests == [] and budget_checks == []
    assert world.count(RecommendationBrief, opportunity_id=opp) == 0
    assert _ledger(world, ws) == (0, 0, 0)


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
    PostgreSQL, where the shared ledger lives; the refused one reserves nothing on either
    engine, sends nothing and stores nothing."""
    world = capture_world
    ws = world.workspace("priced capture")
    llm, settings, sent = _priced_llm()
    refused_opp, refused_record, _ = evidence(world, ws, location=None)
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


@pytest.mark.parametrize("stored", NOT_CAPTURED, ids=repr)
def test_item_31_the_job_refuses_a_brief_nulled_between_enqueue_and_run(
    capture_world, phase5_on, model, stored
):
    world = capture_world
    ws = world.workspace("item 31 job")
    brief_id = world.seed_brief(ws, jurisdiction_code="US-TX")
    r = world.generate(brief_id, ws=ws)
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]
    table = RecommendationBrief.__table__
    with world.witness.begin() as conn:
        conn.execute(update(table).where(table.c.id == brief_id).values(jurisdiction_code=stored))
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
    legacy = world.seed_brief(ws, jurisdiction_code="US-TX")
    refused = world.generate(legacy, ws=ws).json()["job_id"]
    table = RecommendationBrief.__table__
    with world.witness.begin() as conn:
        conn.execute(update(table).where(table.c.id == legacy).values(jurisdiction_code=None))
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
