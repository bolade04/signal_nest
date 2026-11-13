"""D-N4 item 36: the closed set of claim kinds (Alembic ``1574637e56d3``).

The operator's ruling (2026-11-12): "migrate the documented prohibited alias to blocked.
Inventory other unsupported stored values and report them; do not silently delete or guess
their meaning." The packet's recommended wording, adopted: "Claim kinds are a closed set:
approved, restricted, blocked, required_disclaimer. The UI's 'Prohibited' becomes 'blocked'
(shown as 'Blocked'); stored 'prohibited' rows are migrated to 'blocked'; the API refuses any
other kind with 422; the UI can create required_disclaimer entries."

This module pins:

* **The closed set**, in one place: ``ClaimKind`` / ``CLAIM_KINDS`` (``app.campaign_context.
  schemas``), the revision's literal copy, the 5C engine's kind names, and the published
  OpenAPI enum of ``ClaimIn.kind`` and ``ClaimPatch.kind``.
* **The API**:
  - each of the four kinds is created as sent;
  - any other kind is refused with 422 on create and on update, with a fixed message naming the
    four kinds, the submitted value never echoed, and nothing written;
  - onboarding's claims input is held to the same set, never echoing the value.
* **Existing unsupported rows on update**:
  - a PATCH that leaves ``kind`` out keeps the stored value, supported or not;
  - a PATCH that sends one of the four replaces it;
  - a PATCH that sends another is refused;
  - an unsupported row is still listed.
* **The migration**, on SQLite and on PostgreSQL:
  - every stored ``prohibited`` in the review's canonical form (case, spacing, invisible
    characters, compatibility forms) becomes ``blocked``;
  - every other value outside the set is kept exactly as stored and inventoried in the log (its
    escaped, capped value, count and ids), never deleted or mapped;
  - the entries the 5C review treats as blocking are the same before and after;
  - running it again changes nothing;
  - the downgrade is a no-op.
* **The revision's frozen canonical form** equals the engine's ``canonical_kind`` on a corpus of
  spellings. The corpus includes Default_Ignorable code points that are not in category Cf, so the
  revision's Default_Ignorable pass is pinned on its own, not only through its Cf pass.
"""

from __future__ import annotations

import hashlib
import importlib.util
import itertools
import os
import subprocess
import sys
import unicodedata
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa

from app.campaign_context.models import ClaimsLibraryEntry
from app.campaign_context.schemas import CLAIM_KINDS, ClaimIn, ClaimKind
from app.claims.engine import (
    APPROVED_KIND,
    BLOCKED_KIND,
    DISCLAIMER_KIND,
    canonical_kind,
    is_blocking_kind,
)
from app.core.errors import ValidationDomainError
from app.main import app
from app.tests._privacy_support import ALICE, API, ORG_A, WS_A, PrivacyEnv, privacy_world
from app.tests.test_campaign_locations_repair import BRANDS, ORGS, WORKSPACES, Db, _database

API_DIR = Path(__file__).resolve().parents[2]
REVISION = "1574637e56d3"
BELOW = "d5f616c0ec97"
REVISION_FILE = API_DIR / "alembic" / "versions" / "20261112_1700-1574637e56d3_close_claim_kinds.py"
PREFIX = "D-N4 item 36 claim kinds"
#: The fixed 422 message for a kind outside the set (Pydantic's enum error for ClaimKind).
KIND_MESSAGE = "Input should be 'approved', 'restricted', 'blocked' or 'required_disclaimer'"
#: Values outside the set; none of them is a substring of the 422 body.
UNSUPPORTED = ["prohibited", "Prohibited", "Blocked", "APPROVED", "zz-unsupported-kind"]


def _revision():
    spec = importlib.util.spec_from_file_location("claim_kinds_revision", REVISION_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------- #
# One closed set
# --------------------------------------------------------------------------- #
def test_the_closed_set_is_one_set_everywhere() -> None:
    assert CLAIM_KINDS == ("approved", "restricted", "blocked", "required_disclaimer")
    assert _revision().CLAIM_KINDS == CLAIM_KINDS
    assert {APPROVED_KIND, BLOCKED_KIND, DISCLAIMER_KIND} <= set(CLAIM_KINDS)
    assert ClaimIn.model_fields["kind"].annotation is ClaimKind
    assert tuple(ClaimKind) == CLAIM_KINDS
    schemas = app.openapi()["components"]["schemas"]
    published = schemas["ClaimKind"]
    assert published == {"type": "string", "enum": list(CLAIM_KINDS), "title": "ClaimKind"}
    for name in ("ClaimIn", "ClaimPatch"):  # both publish the closed set as that component
        assert schemas[name]["properties"]["kind"]["$ref"] == "#/components/schemas/ClaimKind"


#: Default_Ignorable code points that are NOT in category Cf. The revision's canonical form
#: drops every Cf code point in a second pass, so a corpus whose invisible characters are all Cf
#: (U+200B, U+00AD, U+2060 below) cannot tell whether its Default_Ignorable pass runs at all.
#: These can, and each one is named here so the pin cannot fall back to Cf characters unnoticed.
NON_CF_IGNORABLES = {
    "️": "VARIATION SELECTOR-16",
    "͏": "COMBINING GRAPHEME JOINER",
    "ㅤ": "HANGUL FILLER",
    "᠋": "MONGOLIAN FREE VARIATION SELECTOR ONE",
    "︀": "VARIATION SELECTOR-1",
}
#: Spellings carrying them, with the kind the engine's canonical_kind reads.
NON_CF_SPELLINGS = {
    "prohibited️": "prohibited",
    "pro͏hibited": "prohibited",
    "ㅤprohibited": "prohibited",
    "prohibited᠋": "prohibited",
    "blocked︀": "blocked",
}

SPELLINGS = [
    *NON_CF_SPELLINGS,
    "prohibited",
    "Prohibited",
    " PROHIBITED ",
    "pro​hibited",
    "ｐrohibited",
    "prohibited。",
    "blocked",
    "Blocked",
    " blocked",
    "blocked​",
    "ｂlocked",
    "required_disclaimer",
    " Required_Disclaimer ",
    "approved",
    "restricted",
    "",
    "blocked claim",
    "un­blocked",
    "⁠approved⁠",
    "x" * 200,
]


@pytest.mark.parametrize("kind", SPELLINGS)
def test_the_revisions_frozen_canonical_form_is_the_engines(kind) -> None:
    assert _revision().canonical(kind) == canonical_kind(kind)


@pytest.mark.parametrize("spelling", NON_CF_SPELLINGS)
def test_a_non_cf_default_ignorable_is_removed_like_the_engine_removes_it(spelling) -> None:
    """Each added code point is a named non-Cf Default_Ignorable, and both the engine and the
    revision read the spelling as its kind (so the revision's Default_Ignorable pass is needed)."""
    added = [ch for ch in spelling if not ch.isascii()]
    assert len(added) == 1 and added[0] in NON_CF_IGNORABLES, ascii(spelling)
    assert unicodedata.category(added[0]) != "Cf", ascii(spelling)
    assert unicodedata.name(added[0]) == NON_CF_IGNORABLES[added[0]]
    assert canonical_kind(spelling) == NON_CF_SPELLINGS[spelling]
    assert _revision().canonical(spelling) == NON_CF_SPELLINGS[spelling]


def test_every_non_cf_ignorable_is_carried_by_a_spelling() -> None:
    carried = {ch for spelling in NON_CF_SPELLINGS for ch in spelling if not ch.isascii()}
    assert carried == set(NON_CF_IGNORABLES)
    assert {"️", "͏", "ㅤ"} <= carried  # ruling DN4M-36-R-2's minimum


# --------------------------------------------------------------------------- #
# The API: 422 for any other kind, on create and update, never echoing it
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def env(tmp_path_factory) -> Iterator[PrivacyEnv]:
    with privacy_world(tmp_path_factory.mktemp("kinds") / "kinds.db") as world:
        yield world


def _claims(env: PrivacyEnv) -> str:
    return f"{API}/workspaces/{WS_A}/claims"


def _refused_kind(r, value: str) -> None:
    assert r.status_code == 422, r.text
    body = r.json()["error"]
    assert body["code"] == "validation_error"
    assert [(d["loc"][-1], d["msg"]) for d in body["details"]] == [("kind", KIND_MESSAGE)]
    assert value not in r.text  # never echoed


@pytest.mark.parametrize("kind", CLAIM_KINDS)
def test_each_of_the_four_kinds_is_created_as_sent(env, kind) -> None:
    r = env.client.post(
        _claims(env), headers=env.auth(ALICE), json={"text": f"Claim {kind}", "kind": kind}
    )
    assert r.status_code == 201, r.text
    assert r.json()["kind"] == kind
    assert env.rows(ClaimsLibraryEntry, ClaimsLibraryEntry.id == r.json()["id"])[0]["kind"] == kind


@pytest.mark.parametrize("value", UNSUPPORTED)
def test_any_other_kind_is_refused_on_create_with_a_fixed_422(env, value) -> None:
    before = env.count(ClaimsLibraryEntry)
    r = env.client.post(
        _claims(env), headers=env.auth(ALICE), json={"text": "A claim", "kind": value}
    )
    _refused_kind(r, value)
    assert env.count(ClaimsLibraryEntry) == before


_serial = itertools.count(1)


def _stored(env: PrivacyEnv, kind: str) -> str:
    """An entry written before the set was closed, with ``kind`` as stored (Core insert)."""
    entry_id = f"ck-{next(_serial)}"
    with env.engine.begin() as conn:
        conn.execute(
            sa.insert(ClaimsLibraryEntry.__table__).values(
                id=entry_id,
                organization_id=ORG_A,
                workspace_id=WS_A,
                brand_id="a1-brand",
                text="Stored claim",
                kind=kind,
                risk_level="low",
            )
        )
    return entry_id


@pytest.mark.parametrize("value", UNSUPPORTED)
def test_any_other_kind_is_refused_on_update_and_nothing_changes(env, value) -> None:
    entry_id = _stored(env, "approved")
    before = env.rows(ClaimsLibraryEntry, ClaimsLibraryEntry.id == entry_id)
    r = env.client.patch(
        f"{_claims(env)}/{entry_id}", headers=env.auth(ALICE), json={"kind": value, "text": "X"}
    )
    _refused_kind(r, value)
    assert env.rows(ClaimsLibraryEntry, ClaimsLibraryEntry.id == entry_id) == before


@pytest.mark.parametrize("stored", ["prohibited", "Blocked", "zz-unsupported-kind", ""])
def test_an_existing_unsupported_row_on_update(env, stored) -> None:
    """A PATCH that leaves ``kind`` out keeps the stored value; one that sends one of the four
    replaces it; one that sends another is refused. The row is listed throughout."""
    entry_id = _stored(env, stored)
    headers = env.auth(ALICE)
    listed = env.client.get(_claims(env), headers=headers, params={"limit": 500}).json()
    items = listed["items"] if isinstance(listed, dict) else listed
    assert {i["id"]: i["kind"] for i in items}[entry_id] == stored

    kept = env.client.patch(f"{_claims(env)}/{entry_id}", headers=headers, json={"notes": "n"})
    assert kept.status_code == 200, kept.text
    assert kept.json()["kind"] == stored and kept.json()["notes"] == "n"

    refused = env.client.patch(
        f"{_claims(env)}/{entry_id}", headers=headers, json={"kind": "zz-other"}
    )
    _refused_kind(refused, "zz-other")
    assert env.rows(ClaimsLibraryEntry, ClaimsLibraryEntry.id == entry_id)[0]["kind"] == stored

    fixed = env.client.patch(
        f"{_claims(env)}/{entry_id}", headers=headers, json={"kind": "blocked"}
    )
    assert fixed.status_code == 200 and fixed.json()["kind"] == "blocked", fixed.text


@pytest.mark.parametrize("value", UNSUPPORTED)
def test_onboarding_claims_are_held_to_the_same_set_and_never_echo(value) -> None:
    from app.brands.service import _validated_claims

    with pytest.raises(ValidationDomainError) as refused:
        _validated_claims([{"text": "Fine", "kind": "approved"}, {"text": "x", "kind": value}])
    assert refused.value.message == "Invalid claim: claims[1].kind: enum"
    assert value not in refused.value.message


# --------------------------------------------------------------------------- #
# The migration, on SQLite and PostgreSQL
# --------------------------------------------------------------------------- #
#: Stored kinds before the revision, by entry id, and what each must be after it.
PERSON_KIND = "kind with a name Jane Doe"
STORED = {
    "k-approved": ("approved", "approved"),
    "k-restricted": ("restricted", "restricted"),
    "k-blocked": ("blocked", "blocked"),
    "k-disclaimer": ("required_disclaimer", "required_disclaimer"),
    "k-prohibited": ("prohibited", "blocked"),
    "k-prohibited-case": ("Prohibited", "blocked"),
    "k-prohibited-space": ("  PROHIBITED ", "blocked"),
    "k-prohibited-zwsp": ("pro​hibited", "blocked"),
    "k-prohibited-wide": ("ｐrohibited", "blocked"),
    "k-prohibited-retired": ("prohibited", "blocked"),
    "k-blocked-case": ("Blocked", "Blocked"),
    "k-approved-space": (" approved", " approved"),
    "k-empty": ("", ""),
    "k-other": ("zz-unsupported-kind", "zz-unsupported-kind"),
    "k-other-2": ("zz-unsupported-kind", "zz-unsupported-kind"),
    "k-long": ("y" * 40, "y" * 40),
    # A free-text kind naming a person: its text must never reach the migration's log (36-R-1).
    "k-person": (PERSON_KIND, PERSON_KIND),
}
RETIRED_AT = datetime(2026, 1, 1, tzinfo=UTC)
CLAIMS = sa.table(
    "claims_library",
    sa.column("id"),
    sa.column("organization_id"),
    sa.column("workspace_id"),
    sa.column("brand_id"),
    sa.column("text"),
    sa.column("kind"),
    sa.column("risk_level"),
    sa.column("retired_at", sa.DateTime(timezone=True)),
)


@pytest.fixture(params=["sqlite", "postgresql"])
def db(request, tmp_path) -> Iterator[Db]:
    with _database(request.param, tmp_path) as database:
        yield database


def _world_at_below(db: Db) -> None:
    db.ok("upgrade", BELOW)
    db.execute(ORGS.insert(), [{"id": "org-k", "name": "org-k", "slug": "org-k"}])
    db.execute(
        WORKSPACES.insert(),
        [
            {
                "id": "ws-k",
                "organization_id": "org-k",
                "name": "ws-k",
                "slug": "ws-k",
                "onboarding_completed": True,
            }
        ],
    )
    db.execute(
        BRANDS.insert(),
        [{"id": "brand-k", "organization_id": "org-k", "workspace_id": "ws-k", "name": "B"}],
    )
    db.execute(
        CLAIMS.insert(),
        [
            {
                "id": entry_id,
                "organization_id": "org-k",
                "workspace_id": "ws-k",
                "brand_id": "brand-k",
                "text": f"text {entry_id}",
                "kind": stored,
                "risk_level": "low",
                "retired_at": RETIRED_AT if entry_id.endswith("retired") else None,
            }
            for entry_id, (stored, _) in STORED.items()
        ],
    )


def _kinds(db: Db) -> dict[str, str]:
    with db.engine.connect() as conn:
        return dict(conn.execute(sa.select(CLAIMS.c.id, CLAIMS.c.kind)).all())


def test_the_migration_maps_the_alias_and_inventories_the_rest(db: Db) -> None:
    _world_at_below(db)
    blocking_before = {i for i, k in _kinds(db).items() if is_blocking_kind(k)}

    out = db.ok("upgrade", "head")

    assert db.version() == [REVISION]
    assert _kinds(db) == {entry_id: after for entry_id, (_, after) in STORED.items()}
    assert {i for i, k in _kinds(db).items() if is_blocking_kind(k)} == blocking_before
    migrated = sorted(i for i, (s, a) in STORED.items() if s != a)
    assert (
        f"{PREFIX}: 'prohibited' -> 'blocked': {len(migrated)} entr(y/ies) "
        f"ids=[{', '.join(migrated)}]" in out
    ), out
    for value, ids in UNSUPPORTED_STORED.items():
        line = (
            f"{PREFIX}: unsupported kind kept as stored: sha256:{_digest(value)}: {len(ids)} "
            f"entr(y/ies) ids=[{', '.join(ids)}]"
        )
        assert line in out, (line, out)
    assert (
        f"{PREFIX}: {len(STORED)} entr(y/ies) read, {len(migrated)} migrated to 'blocked', "
        f"7 with an unsupported kind kept as stored (6 distinct value(s))" in out
    ), out
    db.ok("check")


#: The unsupported values the corpus keeps, with their entry ids.
UNSUPPORTED_STORED = {
    "": ["k-empty"],
    " approved": ["k-approved-space"],
    "Blocked": ["k-blocked-case"],
    "zz-unsupported-kind": ["k-other", "k-other-2"],
    "y" * 40: ["k-long"],
    PERSON_KIND: ["k-person"],
}


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def test_the_migration_log_carries_no_stored_value_text(db: Db) -> None:
    """36-R-1: ids, counts and digests only; no stored value's text, escaped or not."""
    _world_at_below(db)
    out = db.ok("upgrade", "head")
    assert f"sha256:{_digest(PERSON_KIND)}" in out, out
    for value in ("Jane Doe", "kind with a name", "zz-unsupported-kind", "y" * 40, "Blocked"):
        assert value not in out, value
    assert "' approved'" not in out and "''" not in out


def test_the_read_only_inventory_prints_the_values_beside_the_logs_digests(db: Db) -> None:
    """The operator's read-only script: each unsupported value escaped beside the digest the
    revision logged, in a READ ONLY transaction on PostgreSQL, writing nothing."""
    from sqlalchemy.orm import Session

    from app.claims.kind_inventory import report_lines, unsupported_kinds

    _world_at_below(db)
    db.ok("upgrade", "head")
    before = _kinds(db)
    with Session(db.engine) as session:
        inventory = unsupported_kinds(session)
        if db.pg:
            assert session.execute(sa.text("SHOW transaction_read_only")).scalar_one() == "on"
            with pytest.raises(sa.exc.DBAPIError):  # a write in it is refused
                session.execute(sa.text("UPDATE claims_library SET kind = 'blocked'"))
        session.rollback()
    assert inventory == UNSUPPORTED_STORED  # every unsupported value, nothing else
    assert list(inventory) == sorted(UNSUPPORTED_STORED, key=_digest)
    lines = report_lines(inventory)
    for value, ids in UNSUPPORTED_STORED.items():
        line = f"sha256:{_digest(value)} {value!r}: {len(ids)} entr(y/ies) ids=[{', '.join(ids)}]"
        assert line in lines, (line, lines)
    assert lines[-1] == "7 entr(y/ies) with an unsupported kind (6 distinct value(s))"
    assert _kinds(db) == before

    env = {**os.environ, "DATABASE_URL": db.url}
    env.pop("TEST_POSTGRES_URL", None)
    cli = subprocess.run(
        [sys.executable, "-m", "app.claims.kind_inventory"],
        cwd=API_DIR,
        env=env,
        capture_output=True,
        text=True,
    )
    assert cli.returncode == 0, cli.stdout + cli.stderr
    assert cli.stdout.splitlines() == lines
    assert _kinds(db) == before


def test_running_it_again_changes_nothing_and_the_downgrade_is_a_no_op(db: Db) -> None:
    _world_at_below(db)
    db.ok("upgrade", "head")
    after = _kinds(db)

    db.ok("downgrade", "-1")
    assert db.version() == [BELOW]
    assert _kinds(db) == after  # nothing restored: the alias's spelling is not recorded
    again = db.ok("upgrade", "head")
    assert _kinds(db) == after
    assert f"{PREFIX}: {len(STORED)} entr(y/ies) read, 0 migrated to 'blocked'" in again, again


def test_an_empty_library_is_read_and_nothing_is_reported(db: Db) -> None:
    out = db.ok("upgrade", "head")
    assert db.version() == [REVISION]
    assert PREFIX not in out, out


def test_a_long_unsupported_value_is_capped_in_the_inventory() -> None:
    from app.claims.kind_inventory import shown

    capped = shown("z" * 500)
    assert capped.endswith("... (500 characters)") and len(capped) < 120
    assert shown("a\nb") == "'a\\nb'"  # a control character cannot break the report's line


def test_the_revision_and_the_inventory_use_one_digest() -> None:
    from app.claims.kind_inventory import digest

    for value in ("", "Blocked", PERSON_KIND, "\u00e9"):
        assert _revision().digest(value) == digest(value) == _digest(value)
