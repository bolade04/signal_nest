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

API tests run on SQLite and, when ``TEST_POSTGRES_URL`` is set, on PostgreSQL (unset FAILS in
CI and skips only on a developer machine).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import update
from sqlalchemy.orm import Session

from app.campaign_context.models import ClaimsLibraryEntry
from app.claims.engine import (
    LibraryEntry,
    active_entries,
    applies_in_jurisdiction,
    is_approved_kind,
    jurisdiction_restriction,
    review_against_library,
)
from app.claims.library import load_library
from app.jurisdiction.codes import UNRESOLVED
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
