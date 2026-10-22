"""P6-PRIV-1: self-serve account deletion and its preview (SQLite, request level).

    GET  /api/v1/account/deletion-preview   Bearer                                  -> 200
    POST /api/v1/account/delete             Bearer, {current_password, confirmation} -> 200

The world is ``_privacy_support``'s: every table populated for three organizations.

* **A member leaves.** CAROL (VIEWER of A, invited to B) is deleted: her user row, membership,
  sessions and account tokens are gone; both invitations addressed to her (one in another
  letter case, in another organization) are gone, because her address is verified; feedback
  and the capability override she authored survive with the attribution cleared; every audit
  record that named her by id or email is anonymised and every other audit record is
  byte-identical; nothing of A's tenant content, of B, of C, or of any other account changes.
  The receipt's reference check is clear and the operator's full verification (references and
  content) agrees; before the deletion the same verifier finds her (the positive control).
* **The sole owner.** ALICE is A's only OWNER while A has other members: refused 409
  ``account_deletion_sole_owner`` and nothing changes; the preview says so in advance. Once
  another member is an OWNER, deleting ALICE removes her A membership (A survives) and deletes
  C, where she was the only member, entirely.
* **Refusals.** A wrong password is 422 ``current_password_incorrect``; a missing or wrong
  confirmation, or an unknown field, is 422 ``validation_error``; anonymous is 401. None of
  them writes anything.
* **After.** The token used is refused (401): the account no longer exists. Signing in again
  fails.
* **What the request checks.** After committing, the request runs the verifier's reference
  check on the live schema: every column that can point at the account. A reference left in a
  table the deletion code does not know is reported in the receipt. The scan of every text
  column is not run in the request; that is the operator's command.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import text, update

from app.audit.models import AuditLog
from app.auth.models import AuthSession, EmailVerificationToken, PasswordResetToken
from app.capabilities.models import WorkspaceCapabilityOverride
from app.feedback.models import OpportunityFeedback
from app.organizations.models import (
    Organization,
    OrganizationInvitation,
    OrganizationMember,
    User,
)
from app.privacy import verification
from app.privacy.erasure import ERASED
from app.privacy.verification import verify_organization_erased, verify_subject_erased
from app.tests._privacy_support import (
    ALICE,
    API,
    BOB,
    CAROL,
    DAVE,
    EMAIL,
    ORG_A,
    ORG_B,
    ORG_C,
    PASSWORD,
    WORKSPACES,
    PrivacyEnv,
    belongs_to,
    privacy_world,
)

DELETE = f"{API}/account/delete"
PREVIEW = f"{API}/account/deletion-preview"
BODY = {"current_password": PASSWORD, "confirmation": "DELETE"}


@pytest.fixture
def env(tmp_path) -> Iterator[PrivacyEnv]:
    with privacy_world(tmp_path / "account.db") as e:
        yield e


def _error(r) -> tuple[int, str]:
    return r.status_code, r.json()["error"]["code"]


def _mentions(env: PrivacyEnv, needle: str) -> list[str]:
    """Ids of audit records whose identifying values still contain ``needle``."""
    hits = []
    for row in env.rows(AuditLog):
        blob = repr(
            (row["actor_user_id"], row["entity_id"], row["reason"], row["previous_state"],
             row["new_state"], row["context"])
        ).lower()
        if needle.lower() in blob:
            hits.append(row["id"])
    return hits


#: The tables an account deletion may write; every other table must not move at all.
_ACCOUNT_TABLES = frozenset({
    "users", "organization_members", "auth_sessions", "password_reset_tokens",
    "email_verification_tokens", "organization_invitations", "audit_logs",
    "opportunity_feedback", "workspace_capability_overrides",
})


def _untouched_by_account_deletion(table, row) -> bool:
    return table.name not in _ACCOUNT_TABLES


# --------------------------------------------------------------------------- #
# A member deletes their account
# --------------------------------------------------------------------------- #
def test_preview_for_a_plain_member(env: PrivacyEnv):
    r = env.client.get(PREVIEW, headers=env.auth(CAROL))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.pop("email_keyed_data")["status"] == "included"  # her address is verified
    assert body == {
        "can_delete": True,
        "organizations": [
            {
                "organization_id": ORG_A,
                "organization_name": "Acme Privacy",
                "role": "viewer",
                "member_count": 3,
                "outcome": "membership_removed",
            }
        ],
    }


def test_member_deletion_erases_the_account_and_nothing_else(env: PrivacyEnv):
    headers = env.auth(CAROL)
    with env.session() as s:
        before = verify_subject_erased(s, user_id=CAROL, email=EMAIL[CAROL])
    assert not before.verified  # positive control: the verifier sees her before
    assert {f.table for f in before.reference_findings} >= {
        "users", "organization_members", "auth_sessions", "password_reset_tokens",
        "email_verification_tokens", "opportunity_feedback", "workspace_capability_overrides",
        "audit_logs", "organization_invitations",
    }
    audit_before = {r["id"]: r for r in env.rows(AuditLog)}
    carol_audit = set(_mentions(env, CAROL)) | set(_mentions(env, EMAIL[CAROL]))
    assert carol_audit == {"prv-audit-1", "prv-audit-2", "prv-audit-3", "prv-audit-4",
                           "prv-audit-7"}
    others = env.snapshot(_untouched_by_account_deletion)

    r = env.client.post(DELETE, headers=headers, json=BODY)

    assert r.status_code == 200, r.text
    receipt = r.json()
    assert receipt["user_id"] == CAROL
    assert receipt["deleted_organizations"] == []
    assert receipt["verification"]["references_cleared"] is True
    assert receipt["verification"]["subject_id"] == CAROL
    assert receipt["email_keyed_data"]["status"] == "included"
    assert receipt["counts"]["deleted"] == {
        "auth_sessions": 1,
        "email_verification_tokens": 1,
        "organization_invitations": 2,
        "organization_members": 1,
        "password_reset_tokens": 1,
        "users": 1,
    }
    assert receipt["counts"]["anonymised"] == {"audit_logs": 5}
    # The account and everything that was only hers.
    assert env.count(User, User.id == CAROL) == 0
    assert env.count(OrganizationMember, OrganizationMember.user_id == CAROL) == 0
    for model in (AuthSession, PasswordResetToken, EmailVerificationToken):
        assert env.count(model, model.user_id == CAROL) == 0
    assert {r["id"] for r in env.rows(OrganizationInvitation)} == {"prv-inv-invitee"}
    # Attribution cleared, the records kept.
    assert env.rows(OpportunityFeedback, OpportunityFeedback.id == "a1-feedback")[0][
        "submitted_by_user_id"
    ] is None
    assert env.rows(
        WorkspaceCapabilityOverride, WorkspaceCapabilityOverride.id == "a1-override"
    )[0]["set_by_user_id"] is None
    # Audit: hers anonymised (and kept), every other record byte-identical.
    audit_after = {r["id"]: r for r in env.rows(AuditLog)}
    assert set(audit_after) == set(audit_before)
    for audit_id, row in audit_after.items():
        if audit_id not in carol_audit:
            assert row == audit_before[audit_id], audit_id
    assert _mentions(env, CAROL) == [] and _mentions(env, EMAIL[CAROL]) == []
    accepted = audit_after["prv-audit-2"]
    assert accepted["actor_user_id"] is None
    assert accepted["new_state"] == {
        "email": ERASED, "role": "viewer", "accepted_by_user_id": ERASED
    }
    assert audit_after["prv-audit-3"]["entity_id"] is None
    assert audit_after["prv-audit-4"]["reason"] is None
    assert audit_after["prv-audit-4"]["actor_user_id"] == DAVE
    # Nothing else in the database moved.
    assert env.snapshot(_untouched_by_account_deletion) == others
    for user_id in (ALICE, BOB, DAVE):
        assert env.count(User, User.id == user_id) == 1
    # An independent re-run, on a fresh session, agrees.
    with env.session() as s:
        again = verify_subject_erased(s, user_id=CAROL, email=EMAIL[CAROL])
    assert again.verified, again.as_dict()


def test_the_token_stops_working_and_sign_in_fails(env: PrivacyEnv):
    headers = env.auth(CAROL)
    assert env.client.post(DELETE, headers=headers, json=BODY).status_code == 200
    assert env.client.get(f"{API}/auth/me", headers=headers).status_code == 401
    r = env.client.post(
        f"{API}/auth/login", json={"email": EMAIL[CAROL], "password": PASSWORD}
    )
    assert r.status_code == 401


# --------------------------------------------------------------------------- #
# The sole owner
# --------------------------------------------------------------------------- #
def test_sole_owner_of_a_shared_organization_is_refused(env: PrivacyEnv):
    headers = env.auth(ALICE)
    preview = env.client.get(PREVIEW, headers=headers).json()
    assert preview["can_delete"] is False
    assert {(o["organization_id"], o["outcome"]) for o in preview["organizations"]} == {
        (ORG_A, "blocked_sole_owner"),
        (ORG_C, "organization_deleted"),
    }
    before = env.snapshot()

    r = env.client.post(DELETE, headers=headers, json=BODY)

    assert _error(r) == (409, "account_deletion_sole_owner")
    assert env.snapshot() == before


def test_owner_deletion_after_handing_over_deletes_her_sole_member_organization(
    env: PrivacyEnv,
):
    with env.engine.begin() as conn:
        conn.execute(
            update(OrganizationMember.__table__)
            .where(OrganizationMember.user_id == DAVE)
            .values(role="owner")
        )
    a_tenant = env.snapshot(
        lambda t, row: belongs_to(ORG_A)(t, row)
        and t.name not in {"organization_members", "audit_logs", "organization_invitations",
                           "opportunity_feedback", "workspace_capability_overrides"}
    )
    b_rows = env.snapshot(belongs_to(ORG_B))
    with env.session() as s:
        assert not verify_organization_erased(
            s, organization_id=ORG_C, workspace_ids=WORKSPACES[ORG_C]
        ).verified  # positive control

    r = env.client.post(DELETE, headers=env.auth(ALICE), json=BODY)

    assert r.status_code == 200, r.text
    receipt = r.json()
    assert receipt["verification"]["references_cleared"] is True
    [deleted] = receipt["deleted_organizations"]
    assert deleted["organization_id"] == ORG_C
    assert deleted["workspace_ids"] == list(WORKSPACES[ORG_C])
    assert deleted["verification"]["references_cleared"] is True
    assert deleted["verification"]["retained_anonymised"] == [
        {"table": "audit_logs", "column": "organization_id", "rows": 1}
    ]
    assert env.count(Organization, Organization.id == ORG_C) == 0
    assert env.count(Organization, Organization.id == ORG_A) == 1
    assert env.snapshot(belongs_to(ORG_B)) == b_rows
    assert (
        env.snapshot(
            lambda t, row: belongs_to(ORG_A)(t, row)
            and t.name not in {"organization_members", "audit_logs",
                               "organization_invitations", "opportunity_feedback",
                               "workspace_capability_overrides"}
        )
        == a_tenant
    )
    assert {r["user_id"] for r in env.rows(OrganizationMember,
                                           OrganizationMember.organization_id == ORG_A)} == {
        DAVE, CAROL
    }


# --------------------------------------------------------------------------- #
# Refusals
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"current_password": "wrong-password-0", "confirmation": "DELETE"},
         (422, "current_password_incorrect")),
        ({"current_password": PASSWORD}, (422, "validation_error")),
        ({"current_password": PASSWORD, "confirmation": "delete"}, (422, "validation_error")),
        ({**BODY, "user_id": BOB}, (422, "validation_error")),
        ({"confirmation": "DELETE"}, (422, "validation_error")),
    ],
)
def test_refusals_write_nothing(env: PrivacyEnv, body, expected):
    headers = env.auth(CAROL)
    before = env.snapshot()
    assert _error(env.client.post(DELETE, headers=headers, json=body)) == expected
    assert env.snapshot() == before


def test_anonymous_is_401(env: PrivacyEnv):
    assert env.client.post(DELETE, json=BODY).status_code == 401
    assert env.client.get(PREVIEW).status_code == 401


# --------------------------------------------------------------------------- #
# What the request checks
# --------------------------------------------------------------------------- #
def test_the_request_runs_no_content_scan(env: PrivacyEnv, monkeypatch):
    """The scan of every text column (359 at this head) stays out of the request path."""
    scans: list[object] = []
    real = verification._content_findings

    def counted(*args, **kwargs):
        scans.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(verification, "_content_findings", counted)
    r = env.client.post(DELETE, headers=env.auth(CAROL), json=BODY)
    assert r.status_code == 200, r.text
    assert scans == []
    check = r.json()["verification"]
    assert check["content_scan"] == "operator"
    assert check["references_cleared"] is True
    assert check["reference_columns_checked"] > 0


def test_the_receipt_reports_a_reference_left_behind(env: PrivacyEnv):
    """Positive control: the in-request check reflects the live schema, so a table the deletion
    code does not know, still pointing at the account, is a finding in the receipt."""
    with env.engine.begin() as conn:
        conn.execute(
            text("CREATE TABLE side_notes (id VARCHAR PRIMARY KEY, author_user_id VARCHAR)")
        )
        conn.execute(text("INSERT INTO side_notes VALUES ('n1', :u)"), {"u": CAROL})

    r = env.client.post(DELETE, headers=env.auth(CAROL), json=BODY)

    assert r.status_code == 200, r.text
    check = r.json()["verification"]
    assert check["references_cleared"] is False
    assert check["reference_findings"] == [
        {"table": "side_notes", "column": "author_user_id", "rows": 1}
    ]
