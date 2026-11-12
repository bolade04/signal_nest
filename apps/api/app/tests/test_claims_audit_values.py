"""D-N4 item 40 (operator ruling 2026-11-12): claims-library audit rows record the previous and new
claim text and kind, under the ``audit_logs`` access, export, erasure, retention and verification
controls, and the text is never classified as containing no personal data.

The ruling, verbatim: "record the previous and new claim text and kind under the applicable
access, retention and erasure controls. Free-form claim text must not automatically be
classified as containing no personal data." It supersedes the packet's "(no personal data)"
(review W21-REV-R4 F-2: after an in-place edit nobody could rebuild which text a draft was
reviewed against).

Built on the P6-PRIV-1 world (``_privacy_support``: organization A with ALICE the owner, DAVE an
admin and CAROL a viewer whose address is verified; B the bystander), through the real routes,
on SQLite and on PostgreSQL (``TEST_POSTGRES_URL``; unset FAILS in CI and skips elsewhere).

* **The shape.** A create records ``new_state = {jurisdiction_codes, kind, text}``; every
  update records the values before (``previous_state``) and after (``new_state``), whichever
  fields changed; a retirement records ``previous_state``. ``context`` keeps the field names; no
  other field's value is recorded. The chain rebuilds the entry's history.
* **Jurisdiction codes (coordinator addition A-R-3, beyond the ruling's minimum).** Since 5D-W1
  an entry's ``jurisdiction_codes`` decide where it applies, so they are recorded beside kind and
  text, as stored: a codes-only edit records the codes before and after, a retirement records
  the codes that stop applying, and a legacy entry's NULL codes are recorded as NULL.
* **Access.** The audit read returns the text to OWNER, ADMIN and COMPLIANCE_REVIEWER only; a
  marketer, reviewer or viewer is refused (and can still read the live library).
* **Export.** The organization export carries the values; the steward's account export never
  does.
* **Erasure.** A claim text naming CAROL's verified address, edited out of the live entry by a
  steward, stays in the audit history until CAROL's account deletion erases it there
  (``"[erased]"``), after which the independent verifier finds nothing (before it, it finds the
  audit rows). Invitee-email erasure does the same for an invitee's address. Organization
  deletion clears both states and keeps the rows; B is untouched.
* **Retention.** The ``audit_logs`` purge removes old claims audit rows and keeps newer ones.
* **Classification.** The inventory and its document state that this text may contain personal
  data.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import insert, update

from app.audit.models import AuditLog
from app.campaign_context.models import ClaimsLibraryEntry
from app.claims.library import CLAIM_AUDIT_VALUES, claim_audit_values
from app.organizations.models import OrganizationMember, User
from app.privacy import retention
from app.privacy.erasure import ERASED, erase_invitee_email
from app.privacy.inventory import TABLES, Category, RetentionClass
from app.privacy.verification import verify_organization_erased, verify_subject_erased
from app.tests._auth2_support import require_postgres
from app.tests._privacy_support import (
    ALICE,
    API,
    BOB,
    CAROL,
    DAVE,
    EMAIL,
    INVITEE,
    NAME,
    ORG_A,
    ORG_B,
    PASSWORD,
    PASSWORD_HASH,
    WORKSPACES,
    WS_A,
    WS_B,
    PrivacyEnv,
    privacy_world,
)

DOC = Path(__file__).resolve().parents[4] / "docs" / "operations" / "data-inventory.md"
ACTIONS = (
    "claims_library_entry.created",
    "claims_library_entry.updated",
    "claims_library_entry.retired",
)
#: Extra members of organization A for the access checks (the world has none of these roles).
MARKETER, REVIEWER, COMPLIANCE = "prv-mkt", "prv-rev", "prv-cmp"
#: A claim text naming CAROL by her verified address (a testimonial a steward later edits).
NAMING_CAROL = f"Recommended by our customer {EMAIL[CAROL]}"
NAMING_INVITEE = f"Ask {INVITEE} for the tasting notes"


@pytest.fixture(params=["sqlite", "postgres"])
def env(request, tmp_path) -> Iterator[PrivacyEnv]:
    if request.param == "postgres":
        require_postgres()
        db_file = None
    else:
        db_file = tmp_path / "claims-audit.db"
    with privacy_world(db_file) as e:
        with e.engine.begin() as conn:
            for user_id, role in ((MARKETER, "marketer"), (REVIEWER, "reviewer"),
                                  (COMPLIANCE, "compliance_reviewer")):
                conn.execute(
                    insert(User.__table__).values(
                        id=user_id, email=f"{user_id}@privacy.example", full_name=user_id,
                        hashed_password=PASSWORD_HASH, is_active=True, auth_epoch=0,
                        is_operator=False,
                    )
                )
                conn.execute(
                    insert(OrganizationMember.__table__).values(
                        id=f"m-{user_id}", organization_id=ORG_A, user_id=user_id, role=role
                    )
                )
        yield e


def _claims(env: PrivacyEnv, ws: str = WS_A) -> str:
    return f"{API}/workspaces/{ws}/claims"


def _create(env: PrivacyEnv, user: str, body: dict, ws: str = WS_A) -> str:
    r = env.client.post(_claims(env, ws), headers=env.auth(user), json=body)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _patch(env: PrivacyEnv, user: str, claim_id: str, body: dict) -> None:
    r = env.client.patch(f"{_claims(env)}/{claim_id}", headers=env.auth(user), json=body)
    assert r.status_code == 200, r.text


def _retire(env: PrivacyEnv, user: str, claim_id: str) -> None:
    r = env.client.delete(f"{_claims(env)}/{claim_id}", headers=env.auth(user))
    assert r.status_code == 204, r.text


def _audits(env: PrivacyEnv, claim_id: str) -> list[dict]:
    rows = env.rows(AuditLog, AuditLog.entity_id == claim_id, AuditLog.action.in_(ACTIONS))
    return sorted(rows, key=lambda a: (ACTIONS.index(a["action"]), a["created_at"]))


def _states(env: PrivacyEnv, claim_id: str) -> list[tuple]:
    return [(a["action"], a["previous_state"], a["new_state"]) for a in _audits(env, claim_id)]


#: The history's jurisdiction restriction (A-R-3): recorded beside kind and text.
CODES = ["US-TX"]


def _history(env: PrivacyEnv) -> str:
    """A claim created naming CAROL, edited (text, then kind, then category only), then edited
    again to remove her address — the live entry no longer names her; its audit rows do."""
    claim_id = _create(env, ALICE, {"text": NAMING_CAROL, "kind": "approved",
                                     "category": "health", "jurisdiction_codes": CODES})
    _patch(env, DAVE, claim_id, {"kind": "restricted"})
    _patch(env, ALICE, claim_id, {"category": "testimonial"})
    _patch(env, DAVE, claim_id, {"text": "Recommended by our customers"})
    return claim_id


# --------------------------------------------------------------------------- #
# The shape
# --------------------------------------------------------------------------- #
def test_create_update_and_retire_record_the_previous_and_new_text_and_kind(env):
    claim_id = _history(env)
    _retire(env, ALICE, claim_id)

    named = {"jurisdiction_codes": CODES, "kind": "approved", "text": NAMING_CAROL}
    restricted = {"jurisdiction_codes": CODES, "kind": "restricted", "text": NAMING_CAROL}
    edited = {"jurisdiction_codes": CODES, "kind": "restricted",
              "text": "Recommended by our customers"}
    assert _states(env, claim_id) == [
        ("claims_library_entry.created", None, named),
        ("claims_library_entry.updated", named, restricted),
        ("claims_library_entry.updated", restricted, restricted),  # category only: both kept
        ("claims_library_entry.updated", restricted, edited),
        ("claims_library_entry.retired", edited, None),
    ]
    contexts = [a["context"] for a in _audits(env, claim_id)]
    assert contexts[1:] == [
        {"fields": ["kind"]}, {"fields": ["category"]}, {"fields": ["text"]},
        {"fields": ["retired_at"]},
    ]
    # Only codes, kind and text: the category's values are never recorded.
    states = json.dumps([[a["previous_state"], a["new_state"]] for a in _audits(env, claim_id)])
    assert "health" not in states and "testimonial" not in states


def test_the_audit_chain_rebuilds_the_entrys_history(env):
    """Each update's previous values are the values the preceding row left: the library a draft
    was reviewed against can be rebuilt after an edit (F-2)."""
    claim_id = _history(env)
    rows = _audits(env, claim_id)
    for before, after in zip(rows, rows[1:], strict=False):
        assert after["previous_state"] == before["new_state"]
    with env.session() as s:
        live = claim_audit_values(s.get(ClaimsLibraryEntry, claim_id))
    assert rows[-1]["new_state"] == live
    assert tuple(sorted(live)) == CLAIM_AUDIT_VALUES == ("jurisdiction_codes", "kind", "text")


def test_an_empty_update_writes_no_audit_record(env):
    claim_id = _create(env, ALICE, {"text": "Locally roasted"})
    _patch(env, ALICE, claim_id, {})
    assert [a["action"] for a in _audits(env, claim_id)] == ["claims_library_entry.created"]


def test_jurisdiction_codes_are_recorded_on_create_update_and_retire(env):
    """A-R-3: a codes-only edit records the codes before and after it (so the entry's
    applicability can be rebuilt), and a retirement records the codes that stop applying."""
    claim_id = _create(env, ALICE, {"text": "Free parking all weekend", "kind": "blocked",
                                     "jurisdiction_codes": ["US-TX"]})
    _patch(env, DAVE, claim_id, {"jurisdiction_codes": ["US-TX", "GB"]})
    _patch(env, ALICE, claim_id, {"jurisdiction_codes": []})  # every jurisdiction
    _retire(env, DAVE, claim_id)

    def state(codes):
        return {"jurisdiction_codes": codes, "kind": "blocked",
                "text": "Free parking all weekend"}

    assert _states(env, claim_id) == [
        ("claims_library_entry.created", None, state(["US-TX"])),
        ("claims_library_entry.updated", state(["US-TX"]), state(["US-TX", "GB"])),
        ("claims_library_entry.updated", state(["US-TX", "GB"]), state([])),
        ("claims_library_entry.retired", state([]), None),
    ]
    assert [a["context"] for a in _audits(env, claim_id)][1:3] == [
        {"fields": ["jurisdiction_codes"]}, {"fields": ["jurisdiction_codes"]},
    ]


def test_a_legacy_entrys_null_codes_are_recorded_as_stored(env):
    """An entry written before the column existed has NULL codes (every jurisdiction): the
    audit records NULL, not a guessed list."""
    legacy = "a1-claim"  # seeded directly by the P6-PRIV-1 world, codes NULL
    with env.session() as s:
        assert s.get(ClaimsLibraryEntry, legacy).jurisdiction_codes is None
    _patch(env, DAVE, legacy, {"kind": "restricted"})
    assert _states(env, legacy) == [
        ("claims_library_entry.updated",
         {"jurisdiction_codes": None, "kind": "approved", "text": "Claim"},
         {"jurisdiction_codes": None, "kind": "restricted", "text": "Claim"}),
    ]


# --------------------------------------------------------------------------- #
# Access
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("reader", [ALICE, DAVE, COMPLIANCE])
def test_the_audit_read_shows_the_values_to_owner_admin_and_compliance_reviewer(env, reader):
    claim_id = _history(env)
    r = env.client.get(
        f"{API}/workspaces/{WS_A}/audit-logs", headers=env.auth(reader),
        params={"entity_id": claim_id},
    )
    assert r.status_code == 200, r.text
    rows = {(a["action"], json.dumps(a["previous_state"], sort_keys=True),
             json.dumps(a["new_state"], sort_keys=True)) for a in r.json()}
    assert ("claims_library_entry.created", "null", json.dumps(
        {"jurisdiction_codes": CODES, "kind": "approved", "text": NAMING_CAROL}, sort_keys=True,
    )) in rows


@pytest.mark.parametrize("reader", [MARKETER, REVIEWER, CAROL])
def test_the_audit_read_refuses_every_other_role_who_still_reads_the_live_library(env, reader):
    claim_id = _history(env)
    r = env.client.get(
        f"{API}/workspaces/{WS_A}/audit-logs", headers=env.auth(reader),
        params={"entity_id": claim_id},
    )
    assert r.status_code == 403, r.text
    assert NAMING_CAROL not in r.text
    listed = env.client.get(_claims(env), headers=env.auth(reader))
    assert listed.status_code == 200, listed.text
    assert claim_id in {c["id"] for c in listed.json()}


# --------------------------------------------------------------------------- #
# Export
# --------------------------------------------------------------------------- #
def test_the_organization_export_carries_the_values_and_the_account_export_never(env):
    claim_id = _history(env)

    org = env.client.get(f"{API}/organizations/{ORG_A}/export/audit_logs",
                         headers=env.auth(ALICE), params={"limit": 5000})
    assert org.status_code == 200, org.text
    exported = [row for row in org.json()["rows"] if row["entity_id"] == claim_id]
    assert len(exported) == 4
    assert any(
        (row["new_state"] or {}).get("text") == NAMING_CAROL for row in exported
    ), exported

    for steward in (ALICE, DAVE):
        mine = env.client.get(f"{API}/account/export", headers=env.auth(steward))
        assert mine.status_code == 200, mine.text
        performed = [e for e in mine.json()["audit_events_performed"]
                     if e["entity_id"] == claim_id]
        assert performed  # the steward's own events are listed ...
        assert set(performed[0]) == {
            "id", "organization_id", "workspace_id", "action", "entity_type", "entity_id",
            "created_at",
        }
        assert NAMING_CAROL not in mine.text  # ... never the claim text they carry


# --------------------------------------------------------------------------- #
# Erasure and verification
# --------------------------------------------------------------------------- #
def test_account_deletion_erases_a_claim_text_naming_the_account_from_the_history(env):
    claim_id = _history(env)
    with env.session() as s:
        before = verify_subject_erased(s, user_id=CAROL, email=EMAIL[CAROL])
    # The live entry no longer names her; only the audit history does, and the verifier sees it.
    assert ("audit_logs", "new_state") in {(f.table, f.column) for f in before.content_findings}
    assert "claims_library" not in {f.table for f in before.content_findings}

    r = env.client.post(
        f"{API}/account/delete", headers=env.auth(CAROL),
        json={"current_password": PASSWORD, "confirmation": "DELETE"},
    )
    assert r.status_code == 200, r.text

    erased_named = {"jurisdiction_codes": CODES, "kind": "approved", "text": ERASED}
    erased_restricted = {"jurisdiction_codes": CODES, "kind": "restricted", "text": ERASED}
    edited = {"jurisdiction_codes": CODES, "kind": "restricted",
              "text": "Recommended by our customers"}
    assert _states(env, claim_id) == [
        ("claims_library_entry.created", None, erased_named),
        ("claims_library_entry.updated", erased_named, erased_restricted),
        ("claims_library_entry.updated", erased_restricted, erased_restricted),
        ("claims_library_entry.updated", erased_restricted, edited),
    ]
    with env.session() as s:
        after = verify_subject_erased(s, user_id=CAROL, email=EMAIL[CAROL])
    assert after.verified, after.as_dict()


def test_invitee_email_erasure_erases_a_claim_text_naming_the_address(env):
    claim_id = _create(env, ALICE, {"text": NAMING_INVITEE})
    with env.session() as s:
        erase_invitee_email(s, email=INVITEE)
        s.commit()
    assert _states(env, claim_id) == [
        ("claims_library_entry.created", None,
         {"jurisdiction_codes": [], "kind": "approved", "text": ERASED}),
    ]


def test_organization_deletion_clears_the_values_keeps_the_rows_and_spares_b(env):
    claim_id = _history(env)
    b_claim = _create(env, BOB, {"text": "Bravo's own claim", "kind": "blocked"}, ws=WS_B)
    b_before = _audits(env, b_claim)

    r = env.client.post(
        f"{API}/organizations/{ORG_A}/delete", headers=env.auth(ALICE),
        json={"current_password": PASSWORD, "confirm_organization_name": NAME[ORG_A]},
    )
    assert r.status_code == 200, r.text

    rows = env.rows(AuditLog, AuditLog.action.in_(ACTIONS), AuditLog.organization_id == ORG_A)
    assert len(rows) >= 4  # retained (O-6) ...
    for row in rows:  # ... with every value cleared
        assert (row["previous_state"], row["new_state"], row["entity_id"]) == (None, None, None)
    assert env.count(AuditLog, AuditLog.entity_id == claim_id) == 0
    assert _audits(env, b_claim) == b_before
    assert b_before[0]["new_state"] == {
        "jurisdiction_codes": [], "kind": "blocked", "text": "Bravo's own claim"
    }
    assert b_before[0]["organization_id"] == ORG_B
    with env.session() as s:
        report = verify_organization_erased(
            s, organization_id=ORG_A, workspace_ids=WORKSPACES[ORG_A]
        )
    assert report.verified, report.as_dict()


# --------------------------------------------------------------------------- #
# Retention
# --------------------------------------------------------------------------- #
def test_the_audit_logs_class_purges_old_claims_audit_rows_only(env):
    old_claim = _create(env, ALICE, {"text": "An old claim"})
    new_claim = _create(env, ALICE, {"text": "A new claim"})
    now = datetime.now(UTC)
    table = AuditLog.__table__
    with env.engine.begin() as conn:
        conn.execute(
            update(table)
            .where(table.c.entity_id == old_claim)
            .values(created_at=now - timedelta(days=400))
        )
    with env.session() as s:
        result = retention.purge_batch(
            s, RetentionClass.AUDIT_LOGS, now - timedelta(days=30), batch_size=500
        )
        s.commit()
    assert result.counts.deleted["audit_logs"] >= 1
    assert _audits(env, old_claim) == []
    assert [a["new_state"]["text"] for a in _audits(env, new_claim)] == ["A new claim"]
    # The live entries are tenant content, not in this class: both stay.
    assert env.count(ClaimsLibraryEntry, ClaimsLibraryEntry.id.in_([old_claim, new_claim])) == 2


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #
def test_the_inventory_classifies_the_claim_text_as_possibly_personal():
    entry = TABLES["audit_logs"]
    assert entry.category is Category.COMPLIANCE and entry.category is not Category.SYSTEM
    assert entry.exported is True and entry.excluded_columns == ()
    assert entry.retention_class is RetentionClass.AUDIT_LOGS
    text = DOC.read_text(encoding="utf-8")
    assert "#### Claims-library audit text (D-N4 item 40)" in text
    section = text.split("#### Claims-library audit text (D-N4 item 40)", 1)[1].split("\n## ", 1)[0]
    assert "**may contain personal data**" in section
    assert "never treated as containing none" in section
    assert "`jurisdiction_codes`" in section and "A-R-3" in section  # codes: not personal
    for control in ("**Access.**", "**Export.**", "**Erasure.**", "**Retention.**",
                    "**Verification.**", "**Limit:**"):
        assert control in section, control
    [row] = [
        line for line in text.splitlines()
        if line.startswith("| `audit_logs` | `actor_user_id`")  # the table-inventory row
    ]
    assert "may contain personal data" in row and "claims-library audits" in row
