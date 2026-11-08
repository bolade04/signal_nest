"""Project Phase 5D exit criterion 6: jurisdiction propagation -- the claims-library half.

The 5D plan gives this module to lane 5D-W1 (§5): "a brief/draft/review row snapshots the
jurisdiction, and a jurisdiction-restricted claims-library rule fires only in its
jurisdictions". **Only the claims half is here.** The snapshot half tests lane 5D-W3's capture
code in ``briefs/service.py`` and ``creative/service.py``, which does not exist yet and waits
for the operator's ruling on packet item 33 (and ideally 31); its tests come with that code. No
placeholder, skipped or expected-failure test stands in for it.

What is proven here, with the shared gate :func:`app.jurisdiction.resolver.jurisdiction_permits`
deciding every match (codes, never substrings):

* **The field.** ``ClaimIn.jurisdiction_codes`` is accepted, stored and answered; empty is the
  default and means every jurisdiction; a value outside the frozen grammar
  (:mod:`app.jurisdiction.codes`) -- lower case, ``UNRESOLVED``, ``USA``, ``US_TX``, an empty or
  over-long subdivision, a non-string, ``null`` -- is refused with 422 and nothing is written.
* **The rule fires only in its jurisdictions** (:func:`app.claims.engine.active_entries`): an
  approved entry only inside one of its codes; a blocking or required-disclaimer entry unless
  the draft's code is known to lie outside all of them.
* **Fail closed when the draft's jurisdiction is unknown** (``None``, ``UNRESOLVED`` or
  malformed; every draft today, since nothing captures one before 5D-W3): restricted entries
  that restrict apply, restricted entries that permit do not. An unreadable stored restriction
  fails closed both ways.
* **End to end through the database**: a rule set through the API and read by
  :func:`app.claims.library.load_library` fires only in its jurisdictions, and the creative
  service's own call (``active_entries(load_library(...))``, no jurisdiction) gets the
  fail-closed reading.
* **Reachable codes only (operator ruling Q-2, 2026-11-08)**: create and update refuse, with a
  422 naming the code, any code the shared resolver cannot produce (``US-PR``, ``US-ZZ``,
  ``GB-ENG``, an unknown country), and accept ``PR`` and the subdivision-list countries
  themselves (``US``: B-3). The accepted set is exactly what ``resolve_jurisdiction`` produces,
  and it follows the resolver's tables when they change (no copy). A stored value no draft can
  carry is read as unreadable and fails closed, and the pre-activation report
  (``python -m app.claims.jurisdiction_report``) lists every active entry that holds one.

API tests run on SQLite and, when ``TEST_POSTGRES_URL`` is set, on PostgreSQL (unset FAILS in
CI and skips only on a developer machine).
"""

from __future__ import annotations

import io
import itertools
import os
import string
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import update
from sqlalchemy.orm import Session, sessionmaker

from app.campaign_context.models import ClaimsLibraryEntry
from app.campaign_context.schemas import ClaimIn, ClaimPatch
from app.claims import jurisdiction_report
from app.claims.engine import (
    LibraryEntry,
    active_entries,
    applies_in_jurisdiction,
    is_approved_kind,
    is_reachable_jurisdiction_code,
    jurisdiction_restriction,
    review_against_library,
)
from app.claims.library import load_library
from app.geography.places_data import COUNTRIES
from app.jurisdiction import resolver
from app.jurisdiction.codes import UNRESOLVED
from app.jurisdiction.resolver import resolve_jurisdiction
from app.jurisdiction.subdivisions import SUBDIVISION_JURISDICTIONS
from app.tests._auth2_support import require_postgres
from app.tests._privacy_support import ALICE, API, WS_A, PrivacyEnv, privacy_world

CLAIMS = f"{API}/workspaces/{WS_A}/claims"
#: Draft jurisdictions that are not a jurisdiction code: none captured, the resolver's failure,
#: and malformed values a column could never hold (lower case, the empty string).
UNKNOWN = (None, UNRESOLVED, "us-tx", "")


@pytest.fixture(params=["sqlite", "postgresql"])
def env(request, tmp_path) -> Iterator[PrivacyEnv]:
    if request.param == "sqlite":
        with privacy_world(tmp_path / "jurisdiction-claims.db") as e:
            yield e
        return
    require_postgres()
    with privacy_world(None) as e:  # pragma: no cover - gated on PostgreSQL
        yield e


def _snapshot(env: PrivacyEnv) -> dict[str, tuple]:
    return env.snapshot(lambda table, _row: table.name != "auth_sessions")


def _stored_codes(env: PrivacyEnv, claim_id: str):
    (row,) = env.rows(ClaimsLibraryEntry, ClaimsLibraryEntry.__table__.c.id == claim_id)
    return row["jurisdiction_codes"]


def _entry(kind: str, codes, text: str = "free parking", entry_id: str = "e") -> LibraryEntry:
    return LibraryEntry(entry_id, text, kind, jurisdiction_codes=jurisdiction_restriction(codes))


# --------------------------------------------------------------------------- #
# 1. The ClaimIn field
# --------------------------------------------------------------------------- #
def test_a_steward_sets_reads_and_clears_jurisdiction_codes(env) -> None:
    headers = env.auth(ALICE)
    body = {"text": "Free parking", "kind": "blocked", "jurisdiction_codes": ["US-TX", "GB"]}
    created = env.client.post(CLAIMS, headers=headers, json=body)
    assert created.status_code == 201, created.text
    claim = created.json()
    assert claim["jurisdiction_codes"] == ["US-TX", "GB"]
    assert _stored_codes(env, claim["id"]) == ["US-TX", "GB"]
    listed = env.client.get(CLAIMS, headers=headers).json()
    assert next(c for c in listed if c["id"] == claim["id"])["jurisdiction_codes"] == [
        "US-TX",
        "GB",
    ]
    cleared = env.client.patch(
        f"{CLAIMS}/{claim['id']}", headers=headers, json={"jurisdiction_codes": []}
    )
    assert cleared.status_code == 200 and cleared.json()["jurisdiction_codes"] == []
    assert _stored_codes(env, claim["id"]) == []
    default = env.client.post(CLAIMS, headers=headers, json={"text": "Open late"}).json()
    assert default["jurisdiction_codes"] == [] and _stored_codes(env, default["id"]) == []


@pytest.mark.parametrize(
    "bad",
    ["us-tx", "UNRESOLVED", "USA", "US_TX", "", "US-", "US-TEXA", "U1", "G", 7, None],
)
def test_a_value_outside_the_frozen_grammar_is_refused_and_nothing_is_written(env, bad) -> None:
    headers = env.auth(ALICE)
    snapshot = _snapshot(env)
    created = env.client.post(
        CLAIMS, headers=headers, json={"text": "x", "jurisdiction_codes": ["GB", bad]}
    )
    assert created.status_code == 422, created.text
    assert created.json()["error"]["code"] == "validation_error"
    patched = env.client.patch(
        f"{CLAIMS}/a1-claim", headers=headers, json={"jurisdiction_codes": [bad]}
    )
    assert patched.status_code == 422, patched.text
    assert _snapshot(env) == snapshot


def test_the_field_itself_cannot_be_null(env) -> None:
    headers = env.auth(ALICE)
    snapshot = _snapshot(env)
    for method, url, body in (
        ("POST", CLAIMS, {"text": "x", "jurisdiction_codes": None}),
        ("PATCH", f"{CLAIMS}/a1-claim", {"jurisdiction_codes": None}),
    ):
        r = env.client.request(method, url, headers=headers, json=body)
        assert r.status_code == 422, (method, r.text)
    assert _snapshot(env) == snapshot


# --------------------------------------------------------------------------- #
# 2. The engine's library filter: the rule fires only in its jurisdictions
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("stored", [None, [], ()])
@pytest.mark.parametrize("kind", ["approved", "blocked", "required_disclaimer"])
def test_an_unrestricted_entry_applies_everywhere(stored, kind) -> None:
    entry = _entry(kind, stored)
    assert entry.jurisdiction_codes is None
    for jurisdiction in ("US-TX", "US", "GB", "NG-LA", "KE", *UNKNOWN):
        assert applies_in_jurisdiction(entry, jurisdiction), jurisdiction


@pytest.mark.parametrize(
    "codes,inside,outside",
    [
        (["GB"], ["GB", "GB-ENG"], ["KE", "US-TX"]),
        # The one containment the grammar defines (the shared gate's): a subdivision lies
        # inside its country, never the reverse, never across countries.
        (["US"], ["US", "US-TX", "US-CA"], ["CA-ON", "GB", "PR"]),
        (["US-TX"], ["US-TX"], ["US", "US-CA", "GB"]),
        (["NG-LA", "KE"], ["NG-LA", "KE"], ["NG", "NG-AB", "GB"]),
    ],
)
def test_a_restricted_approved_entry_applies_only_inside_its_codes(codes, inside, outside) -> None:
    entry = _entry("approved", codes)
    for jurisdiction in inside:
        assert applies_in_jurisdiction(entry, jurisdiction), jurisdiction
    for jurisdiction in (*outside, *UNKNOWN):
        assert not applies_in_jurisdiction(entry, jurisdiction), jurisdiction


@pytest.mark.parametrize("kind", ["blocked", "prohibited", " Blocked ", "required_disclaimer"])
@pytest.mark.parametrize(
    "codes,applies,outside",
    [
        # Applies wherever the draft's code may overlap one of the entry's (a country and its
        # subdivisions both ways), and wherever it is unknown.
        (["US-TX"], ["US-TX", "US"], ["US-CA", "GB", "KE"]),
        (["US"], ["US", "US-TX"], ["CA-ON", "GB"]),
        (["NG-LA"], ["NG-LA", "NG"], ["NG-AB", "KE"]),
    ],
)
def test_a_restricted_entry_that_restricts_applies_wherever_it_may(
    kind, codes, applies, outside
) -> None:
    entry = _entry(kind, codes)
    for jurisdiction in (*applies, *UNKNOWN):
        assert applies_in_jurisdiction(entry, jurisdiction), jurisdiction
    for jurisdiction in outside:
        assert not applies_in_jurisdiction(entry, jurisdiction), jurisdiction


@pytest.mark.parametrize("stored", ["US", ["us"], [""], [7], {"US": True}, ["GB", None]])
def test_an_unreadable_stored_restriction_fails_closed_both_ways(stored) -> None:
    """A value no API write can produce (written by hand, or by a later bug): the restriction
    names nothing readable, so nothing lies inside it and nothing can be ruled out of it."""
    assert jurisdiction_restriction(stored) is not None  # a restriction, never "everywhere"
    blocked, approved = _entry("blocked", stored), _entry("approved", stored)
    for jurisdiction in ("GB", "US", "US-TX", "KE", *UNKNOWN):
        assert applies_in_jurisdiction(blocked, jurisdiction), jurisdiction
        if stored == ["GB", None] and jurisdiction == "GB":
            continue  # the readable code still permits inside itself
        assert not applies_in_jurisdiction(approved, jurisdiction), jurisdiction


def test_retired_entries_stay_ignored_whatever_their_jurisdiction() -> None:
    retired = LibraryEntry(
        "r", "free parking", "blocked", retired_at=datetime.now(UTC), jurisdiction_codes=("GB",)
    )
    for jurisdiction in ("GB", "KE", *UNKNOWN):
        assert active_entries([retired], jurisdiction=jurisdiction) == []


def test_the_review_fires_a_restricted_rule_only_in_its_jurisdictions() -> None:
    entries = [
        _entry("blocked", ["US-TX"], "free parking", "b-tx"),
        _entry("required_disclaimer", ["GB"], "Terms apply.", "d-gb"),
        _entry("blocked", None, "guaranteed cure", "b-all"),
    ]
    text = "Free parking all week."
    texas = review_against_library(text, None, entries, jurisdiction="US-TX")
    assert texas.is_blocked and texas.blocked_claim_ids == ["b-tx"]
    assert texas.disclaimers_resolved and not texas.passed
    britain = review_against_library(text, None, entries, jurisdiction="GB")
    assert not britain.is_blocked and britain.missing_disclaimers == ["Terms apply."]
    assert review_against_library(f"{text} Terms apply.", None, entries, jurisdiction="GB").passed
    kenya = review_against_library(text, None, entries, jurisdiction="KE")
    assert kenya.passed and kenya.required_disclaimers == []
    # An unrestricted rule still fires everywhere.
    assert review_against_library("A guaranteed cure.", None, entries, jurisdiction="KE").is_blocked
    for unknown in UNKNOWN:
        review = review_against_library(text, None, entries, jurisdiction=unknown)
        assert review.is_blocked and review.missing_disclaimers == ["Terms apply."], unknown
    # Omitting the jurisdiction is a draft with none captured: the same fail-closed reading.
    omitted = review_against_library(text, None, entries)
    assert omitted.is_blocked and omitted.missing_disclaimers == ["Terms apply."]


# --------------------------------------------------------------------------- #
# 3. End to end through the database
# --------------------------------------------------------------------------- #
def _library_world(env: PrivacyEnv) -> dict[str, str]:
    headers = env.auth(ALICE)
    ids = {}
    for key, body in {
        "blocked_tx": {"text": "free parking", "kind": "blocked", "jurisdiction_codes": ["US-TX"]},
        "approved_gb": {"text": "Award-winning", "kind": "approved", "jurisdiction_codes": ["GB"]},
        "disclaimer_ke": {
            "text": "Terms apply.",
            "kind": "required_disclaimer",
            "jurisdiction_codes": ["KE"],
        },
    }.items():
        r = env.client.post(CLAIMS, headers=headers, json=body)
        assert r.status_code == 201, r.text
        ids[key] = r.json()["id"]
    return ids


def test_a_rule_set_through_the_api_fires_only_in_its_jurisdictions(env) -> None:
    ids = _library_world(env)
    with Session(bind=env.witness) as s:
        library = load_library(s, WS_A)
    by_id = {e.id: e for e in library}
    assert by_id[ids["blocked_tx"]].jurisdiction_codes == ("US-TX",)
    assert by_id["a1-claim"].jurisdiction_codes is None  # written before the field existed
    text = "Free parking. Award-winning."
    assert review_against_library(text, None, library, jurisdiction="US-TX").blocked_claim_ids == [
        ids["blocked_tx"]
    ]
    assert not review_against_library(text, None, library, jurisdiction="GB").is_blocked
    kenya = review_against_library(text, None, library, jurisdiction="KE")
    assert not kenya.is_blocked and kenya.missing_disclaimers == ["Terms apply."]

    def approved(jurisdiction):
        return sorted(
            e.id
            for e in active_entries(library, jurisdiction=jurisdiction)
            if is_approved_kind(e.kind)
        )

    assert approved("GB") == sorted([ids["approved_gb"], "a1-claim"])
    assert approved("US-TX") == ["a1-claim"]


def test_callers_that_capture_no_jurisdiction_get_the_fail_closed_reading(env) -> None:
    """The creative service reads ``active_entries(load_library(db, workspace_id))`` with no
    jurisdiction (nothing captures one before 5D-W3): every restricted entry that restricts
    applies, no restricted entry that permits is offered; a hand-written unreadable value too."""
    ids = _library_world(env)
    with env.witness.begin() as conn:
        conn.execute(
            update(ClaimsLibraryEntry.__table__)
            .where(ClaimsLibraryEntry.__table__.c.id == "a1-claim")
            .values(jurisdiction_codes="GB")  # not a list: unreadable
        )
    with Session(bind=env.witness) as s:
        applied = {e.id for e in active_entries(load_library(s, WS_A))}
    assert applied == {ids["blocked_tx"], ids["disclaimer_ke"]}


# --------------------------------------------------------------------------- #
# 4. Reachable codes only (operator ruling Q-2, 2026-11-08)
# --------------------------------------------------------------------------- #
#: Codes the shared resolver can produce: countries it resolves to (PR among them), the
#: subdivision-list countries as the containers of their subdivision drafts (B-3), and
#: subdivisions in the resolver's tables.
ACCEPTED = [
    "PR", "GB", "KE", "AQ", "US", "CA", "AU", "NG",
    "US-TX", "US-DC", "CA-QC", "CA-NT", "AU-NSW", "AU-WA", "NG-LA", "NG-FC",
]  # fmt: skip
#: Well-formed codes the resolver cannot produce, each with the reason its 422 gives.
REFUSED = {
    "US-PR": "PR is not a state or province of US (PR has its own country code, so use PR)",
    "US-VI": "VI is not a state or province of US (VI has its own country code, so use VI)",
    "US-ZZ": "ZZ is not a state or province of US",
    "CA-TX": "TX is not a state or province of CA",
    "NG-XX": "XX is not a state or province of NG",
    "GB-ENG": "GB applies as a whole country, so use GB",
    "KE-30": "KE applies as a whole country, so use KE",
    "XX": "XX is not an ISO 3166-1 country code",
    "UK": "UK is not an ISO 3166-1 country code",
    "EU": "EU is not an ISO 3166-1 country code",
    "TX": "TX is not an ISO 3166-1 country code",
    "ZZ-TX": "ZZ is not an ISO 3166-1 country code",
}


def _refusal_messages(response) -> list[str]:
    return [d["msg"] for d in response.json()["error"]["details"]]


def test_q2_codes_the_resolver_can_produce_are_accepted_on_create_and_update(env) -> None:
    headers = env.auth(ALICE)
    created = env.client.post(
        CLAIMS,
        headers=headers,
        json={"text": "x", "kind": "blocked", "jurisdiction_codes": ACCEPTED},
    )
    assert created.status_code == 201, created.text
    assert _stored_codes(env, created.json()["id"]) == ACCEPTED
    patched = env.client.patch(
        f"{CLAIMS}/a1-claim", headers=headers, json={"jurisdiction_codes": ACCEPTED}
    )
    assert patched.status_code == 200, patched.text
    assert _stored_codes(env, "a1-claim") == ACCEPTED


@pytest.mark.parametrize("code", sorted(REFUSED))
def test_q2_a_code_the_resolver_cannot_produce_is_refused_on_create_and_update(env, code) -> None:
    """A clear, specific 422 that names the code (and nothing internal); nothing is written,
    by the create or by the update."""
    headers = env.auth(ALICE)
    snapshot = _snapshot(env)
    expected = f"Value error, {code} is not a jurisdiction a claim can apply in: {REFUSED[code]}"
    for method, url in (("POST", CLAIMS), ("PATCH", f"{CLAIMS}/a1-claim")):
        body = {"text": "x", "kind": "blocked", "jurisdiction_codes": ["GB", code]}
        r = env.client.request(method, url, headers=headers, json=body)
        assert r.status_code == 422, (method, r.text)
        assert r.json()["error"]["code"] == "validation_error"
        assert _refusal_messages(r) == [expected], (method, r.text)
    assert _snapshot(env) == snapshot


def test_q2_pr_is_accepted_and_us_pr_refused(env) -> None:
    """Puerto Rico is its own ISO 3166-1 code, PR, and a jurisdiction of its own (B-3 as built);
    US-PR is not in the US table, so the resolver can never produce it."""
    headers = env.auth(ALICE)
    created = env.client.post(
        CLAIMS,
        headers=headers,
        json={"text": "free parking", "kind": "blocked", "jurisdiction_codes": ["PR"]},
    )
    assert created.status_code == 201, created.text
    claim_id = created.json()["id"]
    refused = env.client.post(
        CLAIMS,
        headers=headers,
        json={"text": "free parking", "kind": "blocked", "jurisdiction_codes": ["US-PR"]},
    )
    assert refused.status_code == 422 and "so use PR" in _refusal_messages(refused)[0]
    edited = env.client.patch(
        f"{CLAIMS}/{claim_id}", headers=headers, json={"jurisdiction_codes": ["US-PR"]}
    )
    assert edited.status_code == 422, edited.text
    assert _stored_codes(env, claim_id) == ["PR"]  # the edit wrote nothing
    with Session(bind=env.witness) as s:
        library = load_library(s, WS_A)
    for draft, blocked in (("PR", True), ("US-TX", False), ("US", False), ("GB", False)):
        review = review_against_library("Free parking.", None, library, jurisdiction=draft)
        assert (claim_id in review.blocked_claim_ids) is blocked, draft


def test_q2_create_update_and_onboarding_share_one_rule() -> None:
    """``ClaimPatch`` keeps ``ClaimIn``'s validators, and onboarding validates its claims as
    ``ClaimIn`` (``app.brands.service.run_onboarding``): one rule for every writer."""
    for model in (ClaimIn, ClaimPatch):
        assert model.model_validate({"text": "x", "jurisdiction_codes": ["PR", "US"]})
        with pytest.raises(ValueError, match="US-PR is not a jurisdiction a claim can apply in"):
            model.model_validate({"text": "x", "jurisdiction_codes": ["US-PR"]})


@dataclass(frozen=True)
class _Location:
    city: str | None
    state_province: str | None
    country: str | None


def _produced_by_the_resolver() -> set[str]:
    """Every code ``resolve_jurisdiction`` answers in a resolution's ``codes``: each country the
    tables know, through each subdivision where the country resolves by subdivision."""
    produced: set[str] = set()
    for country in COUNTRIES:
        divisions = SUBDIVISION_JURISDICTIONS.get(country)
        for part in sorted(divisions) if divisions else [None]:
            state = f"{country}-{part}" if part else None
            resolution = resolve_jurisdiction(_Location(None, state, country))
            assert resolution.is_resolved, (country, part, resolution.evidence)
            produced.update(resolution.codes)
    return produced


def test_q2_the_accepted_codes_are_exactly_those_the_resolver_produces() -> None:
    """The oracle is the resolver itself, run over every country and subdivision it knows. The
    universe holds every two-letter code and every known country with every subdivision part
    of any table, and a few that no table has."""
    produced = _produced_by_the_resolver()
    parts = {p for divisions in SUBDIVISION_JURISDICTIONS.values() for p in divisions}
    parts |= {"PR", "VI", "ZZ", "ENG", "30", "X"}
    letters = string.ascii_uppercase
    universe = {a + b for a, b in itertools.product(letters, repeat=2)}
    universe |= {f"{country}-{part}" for country in COUNTRIES for part in parts}
    assert produced <= universe
    assert {code for code in universe if is_reachable_jurisdiction_code(code)} == produced
    assert "PR" in produced and "US" in produced and "US-PR" not in produced
    assert len(produced) == len(COUNTRIES) + sum(map(len, SUBDIVISION_JURISDICTIONS.values()))


def test_q2_the_rule_follows_the_resolvers_tables_not_a_copy(monkeypatch) -> None:
    """Change what the resolver reads, and the rule changes with it: a country added to the
    subdivision list (GB, with England) makes GB-ENG producible and accepted; a state removed
    from the US table makes US-TX unproducible and refused. A copy of the tables would not
    follow. (``_token_index`` is rebuilt as the resolver builds it at import.)"""
    us_without_texas = {k: v for k, v in SUBDIVISION_JURISDICTIONS["US"].items() if k != "TX"}
    monkeypatch.setitem(SUBDIVISION_JURISDICTIONS, "GB", {"ENG": ("england",)})
    monkeypatch.setitem(SUBDIVISION_JURISDICTIONS, "US", us_without_texas)
    monkeypatch.setattr(resolver, "_TOKENS", resolver._token_index())
    assert resolve_jurisdiction(_Location("London", "England", "GB")).code == "GB-ENG"
    assert not resolve_jurisdiction(_Location("Dallas", "TX", "US")).is_resolved
    assert is_reachable_jurisdiction_code("GB-ENG")
    assert not is_reachable_jurisdiction_code("US-TX")
    assert ClaimIn.model_validate({"text": "x", "jurisdiction_codes": ["GB-ENG"]})
    with pytest.raises(ValueError, match="TX is not a state or province of US"):
        ClaimIn.model_validate({"text": "x", "jurisdiction_codes": ["US-TX"]})
    produced = _produced_by_the_resolver()
    assert "GB-ENG" in produced and "US-TX" not in produced


#: Stored values no draft can carry: well-formed codes the resolver cannot produce.
UNREACHABLE_STORED = [["US-PR"], ["US-ZZ"], ["GB-ENG"], ["XX"], ["US-PR", "GB-ENG"]]
DRAFTS = ("PR", "US", "US-TX", "GB", "KE", "NG-LA", *UNKNOWN)


@pytest.mark.parametrize("stored", UNREACHABLE_STORED, ids=repr)
def test_q2_a_stored_code_no_draft_can_carry_fails_closed_like_an_unreadable_one(stored) -> None:
    """Written before the rule or by hand: read exactly as an unreadable value (``""``, the same
    path as a non-string), so a restricting entry applies to every draft (a blocked US-PR no
    longer applies nowhere) and an approved one is offered nowhere."""
    assert jurisdiction_restriction(stored) == ("",) * len(stored)
    for kind in ("blocked", "prohibited", "required_disclaimer"):
        entry = _entry(kind, stored)
        for draft in DRAFTS:
            assert applies_in_jurisdiction(entry, draft), (kind, draft)
    approved = _entry("approved", stored)
    for draft in DRAFTS:
        assert not applies_in_jurisdiction(approved, draft), draft


def test_q2_a_mixed_stored_restriction_keeps_its_reachable_codes() -> None:
    stored = ["GB", "US-PR"]
    assert jurisdiction_restriction(stored) == ("GB", "")
    approved, blocked = _entry("approved", stored), _entry("blocked", stored)
    assert applies_in_jurisdiction(approved, "GB")
    for draft in ("PR", "US-TX", "KE", *UNKNOWN):
        assert not applies_in_jurisdiction(approved, draft), draft
    for draft in DRAFTS:
        assert applies_in_jurisdiction(blocked, draft), draft
    # Constructed without the reader, the engine still reads it the same way.
    raw = LibraryEntry("raw", "free parking", "blocked", jurisdiction_codes=("US-PR",))
    assert all(applies_in_jurisdiction(raw, draft) for draft in DRAFTS)


#: Entries written straight to the database (before the rule, or by hand), with what the
#: report says about each: (kind, stored codes, retired, listed as (reads_as, unreachable, kept)).
LEGACY = {
    "q2-blocked-us-pr": ("blocked", ["US-PR"], False, ("applies-to-every-draft", ["US-PR"], [])),
    "q2-disclaimer-gb-eng": (
        "required_disclaimer", ["GB-ENG"], False, ("applies-to-every-draft", ["GB-ENG"], []),
    ),
    "q2-approved-mixed": (
        "approved", ["GB", "US-ZZ"], False, ("offered-only-inside-kept-codes", ["US-ZZ"], ["GB"]),
    ),
    "q2-approved-xx": ("approved", ["XX"], False, ("offered-nowhere", ["XX"], [])),
    "q2-restricted-us-pr": ("restricted", ["US-PR"], False, ("no-effect", ["US-PR"], [])),
    "q2-unreadable": ("Blocked", "GB", False, ("applies-to-every-draft", ["GB"], [])),
    "q2-retired-us-pr": ("blocked", ["US-PR"], True, None),
    "q2-reachable-pr": ("blocked", ["PR"], False, None),
}  # fmt: skip


def _legacy_world(env: PrivacyEnv) -> None:
    (seed,) = env.rows(ClaimsLibraryEntry, ClaimsLibraryEntry.__table__.c.id == "a1-claim")
    with Session(bind=env.engine) as s:
        for entry_id, (kind, codes, retired, _listed) in LEGACY.items():
            s.add(
                ClaimsLibraryEntry(
                    id=entry_id,
                    organization_id=seed["organization_id"],
                    workspace_id=seed["workspace_id"],
                    brand_id=seed["brand_id"],
                    text="free parking" if kind != "required_disclaimer" else "Terms apply.",
                    kind=kind,
                    jurisdiction_codes=codes,
                    retired_at=datetime.now(UTC) if retired else None,
                )
            )
        s.commit()


def test_q2_an_entry_written_before_the_rule_fails_closed_and_is_reported(env) -> None:
    _legacy_world(env)
    with Session(bind=env.witness) as s:
        library = load_library(s, WS_A)
    for draft in ("PR", "GB", "KE", "US-TX", None):
        review = review_against_library("Free parking.", None, library, jurisdiction=draft)
        assert {"q2-blocked-us-pr", "q2-unreadable"} <= set(review.blocked_claim_ids), draft
        assert "q2-retired-us-pr" not in review.blocked_claim_ids
        assert ("q2-reachable-pr" in review.blocked_claim_ids) is (draft in ("PR", None)), draft
        assert review.missing_disclaimers == ["Terms apply."], draft

    def offered(draft):
        return {
            e.id for e in active_entries(library, jurisdiction=draft) if is_approved_kind(e.kind)
        }

    assert offered("GB") == {"a1-claim", "q2-approved-mixed"}
    for draft in ("PR", "KE", "US-TX", None):
        assert offered(draft) == {"a1-claim"}, draft

    with Session(bind=env.witness) as s:
        listed, retired = jurisdiction_report.unreachable_jurisdiction_entries(s)
    assert retired == 1
    expected = {entry_id: info[3] for entry_id, info in LEGACY.items() if info[3] is not None}
    assert {e.entry_id: (e.effect, list(e.unreachable), list(e.kept)) for e in listed} == expected
    assert {e.workspace_id for e in listed} == {WS_A}
    out = io.StringIO()
    factory = sessionmaker(bind=env.witness, future=True)
    assert jurisdiction_report.main(factory, out) == 1
    lines = out.getvalue().splitlines()
    assert lines[0].startswith(f"claims-library jurisdiction report (Q-2): {len(expected)} active")
    assert lines[-1] == "retired entries left out: 1"
    assert (
        f'workspace={WS_A} entry=q2-blocked-us-pr kind="blocked" reads_as=applies-to-every-draft '
        'unreachable=["US-PR"] kept=[]' in lines
    )
    assert "free parking" not in out.getvalue() and "Terms apply" not in out.getvalue()


def test_q2_the_report_command_runs_read_only_against_a_database(env) -> None:
    """The documented command, ``python -m app.claims.jurisdiction_report``, against this
    database: exit 0 and an empty report before, exit 1 and the entries after, nothing written."""
    url = env.witness.url.render_as_string(hide_password=False)
    api_dir = Path(__file__).resolve().parents[2]

    def run() -> subprocess.CompletedProcess:
        environ = {**os.environ, "DATABASE_URL": url}
        return subprocess.run(
            [sys.executable, "-m", "app.claims.jurisdiction_report"],
            cwd=api_dir, env=environ, capture_output=True, text=True,
        )  # fmt: skip

    empty = run()
    assert empty.returncode == 0, empty.stdout + empty.stderr
    assert empty.stdout.splitlines()[0].startswith("claims-library jurisdiction report (Q-2): 0")
    _legacy_world(env)
    snapshot = _snapshot(env)
    found = run()
    assert found.returncode == 1, found.stdout + found.stderr
    assert "entry=q2-blocked-us-pr" in found.stdout and "entry=q2-retired-us-pr" not in found.stdout
    assert _snapshot(env) == snapshot


# =========================================================================== #
# The snapshot half (exit criterion 6, first clause), lane 5D-W4 phase 2.
#
# Disclosed deviation: the plan gives this module to 5D-W1 (5d:189). W1's lane closed with the
# claims half above; the snapshot half tests lane 5D-W3's capture and propagation code
# (``briefs/service.py``, ``creative/service.py``), and 5D-W3 authors no tests (5d:191), so lane
# 5D-W4 writes it, independently of W3, as this appended section. W1's rows above are unchanged
# (the module docstring's note that the snapshot half "waits" predates this section).
#
# What is pinned, from the S1-5D rulings record (2026-11-08) and 5d §2's propagation chain
# location -> brief -> creative -> review:
#
# * a brief captured through the 5A route stores its location's code, for each matrix city;
#   its draft and that draft's review carry the same code;
# * **Q-1 (the recorded default, not a ruling):** "A creative draft's jurisdiction_code is its
#   brief's captured jurisdiction_code; the location is not resolved again at generation." If
#   the operator rules Q-1's alternative, ``test_q1_default_*`` below is the test to change;
# * the claims engine is given the draft's code at the three sites (B-3 rows), and a legacy
#   draft with no code gets B-2's fail-closed reading at the approval re-check.
#
# A real HTTP world on SQLite and, when ``TEST_POSTGRES_URL`` is set, on PostgreSQL migrated to
# head (``app.tests._creative_support``); offline mock model only.
# =========================================================================== #
from sqlalchemy import select  # noqa: E402

from app.brands.service import get_primary_brand  # noqa: E402
from app.briefs.models import RecommendationBrief  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.creative.models import ContentReview, CreativeDraft  # noqa: E402
from app.creative.service import draft_approval_refusal  # noqa: E402
from app.jurisdiction.resolver import resolve_jurisdiction  # noqa: E402
from app.llm import service as llm_service_module  # noqa: E402
from app.locations.models import BusinessLocation, GeoCoverageRule  # noqa: E402
from app.tests._creative_support import (  # noqa: E402
    CEILINGS,
    CreativeWorld,
    creative_world,
    postgres_creative_world,
)
from app.tests.test_jurisdiction_failclosed import (  # noqa: E402
    MATRIX_LOCATIONS,
    _CountingProvider,
    capture,
    evidence,
)


@pytest.fixture(scope="module", params=["sqlite", "postgresql"])
def snapshot_world(request, tmp_path_factory) -> Iterator[CreativeWorld]:
    if request.param == "sqlite":
        db_file = tmp_path_factory.mktemp("w4-snapshot") / "api.db"
        with creative_world(db_file, "w4snap") as world:
            yield world
        return
    with postgres_creative_world("w4snap") as world:  # pragma: no cover - gated on PostgreSQL
        yield world


@pytest.fixture
def generation_on(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "recommendation_briefs_enabled", True)
    for name, value in {**CEILINGS, "llm_max_calls_per_workspace_per_day": 1000}.items():
        monkeypatch.setattr(get_settings(), name, value)


@pytest.fixture
def sent(monkeypatch) -> _CountingProvider:
    spy = _CountingProvider(llm_service_module.llm_service._provider)
    monkeypatch.setattr(llm_service_module.llm_service, "_provider", spy)
    return spy


def _restricted(world: CreativeWorld, ws: str, text: str, kind: str, codes: list[str]) -> str:
    """A claims-library entry restricted to ``codes`` (written as the API stores it)."""
    with world.session() as s:
        entry = ClaimsLibraryEntry(
            organization_id=world.org_of(ws),
            workspace_id=ws,
            brand_id=get_primary_brand(s, ws).id,
            text=text,
            kind=kind,
            jurisdiction_codes=list(codes),
        )
        s.add(entry)
        s.flush()
        return entry.id


def _drafts(world: CreativeWorld, brief_id: str) -> list[dict]:
    return sorted(world.rows(CreativeDraft, brief_id=brief_id), key=lambda d: d["draft_version"])


def _review(world: CreativeWorld, draft_id: str) -> dict:
    (row,) = world.rows(ContentReview, draft_id=draft_id)
    return row


def _generate(world: CreativeWorld, ws: str, brief_id: str) -> dict:
    """Generate through the route and the worker; the newest draft of the brief."""
    job = world.generated(brief_id, ws=ws)
    assert job["status"] == "succeeded", (job["last_error_code"], job["last_error_summary"])
    return _drafts(world, brief_id)[-1]


def _refusal(world: CreativeWorld, draft_id: str) -> str | None:
    with world.session() as s:
        return draft_approval_refusal(s, s.get(CreativeDraft, draft_id))


# --------------------------------------------------------------------------- #
# 4. The snapshot: location -> brief -> draft -> review, for each matrix city
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("city", sorted(MATRIX_LOCATIONS))
def test_each_matrix_city_is_snapshotted_on_the_brief_the_draft_and_the_review(
    snapshot_world, generation_on, city
) -> None:
    world = snapshot_world
    fields, address, code = MATRIX_LOCATIONS[city]
    ws = world.workspace(f"snapshot {city}")
    opp, record, _ = evidence(world, ws, location=fields, addresses=(address,))
    created = capture(world, ws, opp, record)
    assert created.status_code == 201, created.text
    brief_id = created.json()["id"]
    assert world.row(RecommendationBrief, brief_id)["jurisdiction_code"] == code
    world.decide(brief_id, "approved")
    draft = _generate(world, ws, brief_id)
    assert draft["jurisdiction_code"] == code
    assert _review(world, draft["id"])["jurisdiction_code"] == code


def test_q1_default_a_draft_keeps_its_briefs_code_after_the_location_moves(
    snapshot_world, generation_on
) -> None:
    """Q-1, the recorded default (rulings record, "Remaining policy choice"): the draft carries
    its brief's captured code and the location is not resolved again at generation, so a
    location edited after capture changes neither the brief nor any later draft. A ruling for
    Q-1's alternative (re-resolve, refuse on UNRESOLVED or a different code) flips this test."""
    world = snapshot_world
    fields, address, code = MATRIX_LOCATIONS["dallas"]
    ws = world.workspace("q1 default")
    opp, record, location_id = evidence(world, ws, location=fields, addresses=(address,))
    brief_id = capture(world, ws, opp, record).json()["id"]
    world.decide(brief_id, "approved")
    locations, rules = BusinessLocation.__table__, GeoCoverageRule.__table__
    # Each move is consistent (the location and its coverage address move together), so a
    # fresh resolution would give another code (US-OK, GB) or none at all; the draft keeps US-TX.
    for moved, address, fresh in (
        ({"city": "Tulsa", "state_province": "OK", "country": "US"}, "Tulsa, OK", "US-OK"),
        (
            {"city": "London", "state_province": None, "country": "United Kingdom"},
            "London, UK",
            "GB",
        ),
        ({"city": "Dallas", "state_province": None, "country": "US"}, "Dallas, US", UNRESOLVED),
    ):
        with world.witness.begin() as conn:
            conn.execute(update(locations).where(locations.c.id == location_id).values(**moved))
            conn.execute(
                update(rules)
                .where(rules.c.location_id == location_id)
                .values(business_address=address)
            )
        with world.session() as s:  # the move is real: the location now resolves elsewhere
            location = s.get(BusinessLocation, location_id)
            moved_rules = s.scalars(
                select(GeoCoverageRule).where(GeoCoverageRule.location_id == location_id)
            ).all()
            assert resolve_jurisdiction(location, moved_rules).code == fresh
        draft = _generate(world, ws, brief_id)
        assert draft["jurisdiction_code"] == code, moved
        assert _review(world, draft["id"])["jurisdiction_code"] == code, moved
    assert world.row(RecommendationBrief, brief_id)["jurisdiction_code"] == code  # written once
    assert [d["draft_version"] for d in _drafts(world, brief_id)] == [1, 2, 3]


# --------------------------------------------------------------------------- #
# 5. The claims engine is given the draft's code at all three sites
# --------------------------------------------------------------------------- #
def test_generation_offers_the_entries_of_the_drafts_jurisdiction(
    snapshot_world, generation_on, sent
) -> None:
    """B-3 at the generation context: an approved US entry is offered for a US-TX draft, an
    approved US-TX entry is not offered for a GB draft; a US disclaimer is required in US-TX."""
    world = snapshot_world
    ws = world.workspace("claims generation")
    _restricted(world, ws, "Family run since 1998", "approved", ["US"])
    _restricted(world, ws, "Texas owned and operated", "approved", ["US-TX"])
    _restricted(world, ws, "Proudly British", "approved", ["GB"])
    _restricted(world, ws, "Open every day", "approved", [])
    _restricted(world, ws, "Terms apply in the US.", "required_disclaimer", ["US"])
    offered = {}
    for code in ("US-TX", "GB"):
        draft = _generate(world, ws, world.seed_brief(ws, jurisdiction_code=code))
        assert draft["jurisdiction_code"] == code
        inputs = sent.requests[-1].inputs
        offered[code] = (list(inputs["approved_claims"]), list(inputs["required_disclaimers"]))
    assert offered["US-TX"] == (
        ["Family run since 1998", "Texas owned and operated", "Open every day"],
        ["Terms apply in the US."],
    )
    assert offered["GB"] == (["Proudly British", "Open every day"], [])


def test_the_generation_review_reads_the_library_in_the_drafts_jurisdiction(
    snapshot_world, generation_on
) -> None:
    """B-3 at review: a blocked GB entry does not block a US-TX draft and does block a GB one;
    a blocked US entry blocks a US-TX draft (a country entry applies to its subdivisions)."""
    world = snapshot_world
    gb_only = world.workspace("claims review gb")
    _restricted(world, gb_only, "weekend parking", "blocked", ["GB"])
    us_tx = _generate(world, gb_only, world.seed_brief(gb_only, jurisdiction_code="US-TX"))
    gb = _generate(world, gb_only, world.seed_brief(gb_only, jurisdiction_code="GB"))
    assert (_review(world, us_tx["id"])["is_blocked"], _review(world, us_tx["id"])["passed"]) == (
        False,
        True,
    )
    assert _review(world, gb["id"])["is_blocked"] is True
    us_only = world.workspace("claims review us")
    _restricted(world, us_only, "weekend parking", "blocked", ["US"])
    texan = _generate(world, us_only, world.seed_brief(us_only, jurisdiction_code="US-TX"))
    assert _review(world, texan["id"])["is_blocked"] is True
    for draft in (us_tx, gb, texan):
        assert _review(world, draft["id"])["jurisdiction_code"] == draft["jurisdiction_code"]


def test_the_approval_recheck_reads_the_library_in_the_drafts_jurisdiction(
    snapshot_world, generation_on
) -> None:
    """B-3 at the approval re-check, then B-2 for a legacy draft whose code is NULL: restricting
    entries apply to it wherever they are restricted to (rulings record, D-2 / B-2: "Preserve
    this handling for legacy unknown-jurisdiction records")."""
    world = snapshot_world
    ws = world.workspace("claims recheck")
    texan = _generate(world, ws, world.seed_brief(ws, jurisdiction_code="US-TX"))
    british = _generate(world, ws, world.seed_brief(ws, jurisdiction_code="GB"))
    legacy = _generate(world, ws, world.seed_brief(ws, jurisdiction_code="US-TX"))
    assert {_refusal(world, d["id"]) for d in (texan, british, legacy)} == {None}
    table = CreativeDraft.__table__
    with world.witness.begin() as conn:  # a draft written before 5D-W3: no code
        conn.execute(update(table).where(table.c.id == legacy["id"]).values(jurisdiction_code=None))
    _restricted(world, ws, "weekend parking", "blocked", ["GB"])
    assert _refusal(world, texan["id"]) is None
    assert _refusal(world, british["id"]) == "blocked_claim"
    assert _refusal(world, legacy["id"]) == "blocked_claim"  # B-2
    disclaimers = world.workspace("claims recheck disclaimer")
    texan = _generate(world, disclaimers, world.seed_brief(disclaimers, jurisdiction_code="US-TX"))
    legacy = _generate(world, disclaimers, world.seed_brief(disclaimers, jurisdiction_code="US-TX"))
    with world.witness.begin() as conn:
        conn.execute(update(table).where(table.c.id == legacy["id"]).values(jurisdiction_code=None))
    _restricted(world, disclaimers, "Licensed in Kenya.", "required_disclaimer", ["KE"])
    assert _refusal(world, texan["id"]) is None
    assert _refusal(world, legacy["id"]) == "disclaimer_missing"  # B-2
    _restricted(world, disclaimers, "weekend parking", "blocked", ["US"])
    assert _refusal(world, texan["id"]) == "blocked_claim"  # B-3: US applies to US-TX
