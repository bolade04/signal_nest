"""P6-PRIV-1: organization deletion by its OWNER (SQLite, request level).

    POST /api/v1/organizations/{id}/delete
        exact OWNER, {current_password, confirm_organization_name}  -> 200

* **Deletion.** ALICE deletes A: every row of A and of both its workspaces is gone from every
  tenant table, with its members and invitations; A's audit records are kept (``O-6``) with
  every identifying column cleared; B and C are byte-identical; the members' ACCOUNTS
  survive (CAROL and DAVE keep their user rows and can still sign in), and so do memberships
  elsewhere. The receipt's reference check is clear and lists the retained anonymised audit
  records, and the operator's full verification agrees. Before, the same verifier finds A
  (positive control).
* **Authority.** Exactly OWNER: ADMIN and VIEWER members get the role denial, a non-member the
  membership denial, anonymous 401. The service re-checks the role under the organization
  lock (defence in depth, called directly).
* **Re-authentication and confirmation.** A wrong password is 422
  ``current_password_incorrect``; a name that differs in any way (case included) is 422
  ``organization_name_mismatch``; a malformed body is 422 ``validation_error``.
* **Running jobs.** A claimed or running job of the organization refuses the deletion with 409
  ``organization_jobs_active``.
* **What the request checks.** The verifier's reference check and the retained-audit check run
  in the request. The scan of every text column does not; that is the operator's command.

Every refusal writes nothing.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import update

from app.audit.models import AuditLog
from app.core.errors import PermissionDeniedError
from app.jobs.models import Job
from app.organizations.models import Organization, OrganizationMember, User
from app.privacy import erasure, verification
from app.privacy.verification import verify_organization_erased
from app.tests._privacy_support import (
    ALICE,
    API,
    BOB,
    CAROL,
    DAVE,
    EMAIL,
    NAME,
    ORG_A,
    ORG_B,
    ORG_C,
    PASSWORD,
    WORKSPACES,
    PrivacyEnv,
    belongs_to,
    privacy_world,
)

DELETE_A = f"{API}/organizations/{ORG_A}/delete"
BODY = {"current_password": PASSWORD, "confirm_organization_name": NAME[ORG_A]}


@pytest.fixture
def env(tmp_path) -> Iterator[PrivacyEnv]:
    with privacy_world(tmp_path / "organization.db") as e:
        yield e


def _error(r) -> tuple[int, str, str]:
    error = r.json()["error"]
    return r.status_code, error["code"], error["message"]


def test_owner_deletes_the_organization_and_only_it(env: PrivacyEnv):
    headers = env.auth(ALICE)
    with env.session() as s:
        before = verify_organization_erased(
            s, organization_id=ORG_A, workspace_ids=WORKSPACES[ORG_A]
        )
    assert not before.verified and before.reference_findings  # positive control
    a_audit = [r["id"] for r in env.rows(AuditLog, AuditLog.organization_id == ORG_A)]
    assert len(a_audit) == 6
    untouched = env.snapshot(lambda t, row: not belongs_to(ORG_A)(t, row)
                             and t.name not in {"users", "auth_sessions"})

    r = env.client.post(DELETE_A, headers=headers, json=BODY)

    assert r.status_code == 200, r.text
    receipt = r.json()
    assert receipt["organization_id"] == ORG_A
    assert receipt["workspace_ids"] == list(WORKSPACES[ORG_A])
    assert receipt["verification"]["references_cleared"] is True
    assert receipt["verification"]["retained_anonymised"] == [
        {"table": "audit_logs", "column": "organization_id", "rows": 6}
    ]
    assert receipt["counts"]["anonymised"] == {"audit_logs": 6}
    assert receipt["counts"]["deleted"]["organizations"] == 1
    assert receipt["counts"]["deleted"]["workspaces"] == 2
    assert receipt["counts"]["deleted"]["organization_members"] == 3
    # Nothing of A remains outside its anonymised audit records.
    remaining = env.snapshot(belongs_to(ORG_A))
    assert {name for name, rows in remaining.items() if rows} == {"audit_logs"}
    for row in env.rows(AuditLog, AuditLog.organization_id == ORG_A):
        assert row["id"] in a_audit
        assert (row["actor_user_id"], row["entity_id"], row["reason"]) == (None, None, None)
        assert (row["previous_state"], row["new_state"], row["context"]) == (None, None, {})
        assert row["action"]  # what happened, and when, is kept
    # B, C, every other row: byte-identical. Members' accounts survive.
    assert env.snapshot(lambda t, row: not belongs_to(ORG_A)(t, row)
                        and t.name not in {"users", "auth_sessions"}) == untouched
    for user_id in (ALICE, BOB, CAROL, DAVE):
        assert env.count(User, User.id == user_id) == 1
    assert env.count(OrganizationMember, OrganizationMember.user_id == ALICE) == 1  # C
    r = env.client.post(f"{API}/auth/login", json={"email": EMAIL[CAROL], "password": PASSWORD})
    assert r.status_code == 200 and r.json()["memberships"] == []
    with env.session() as s:
        again = verify_organization_erased(
            s, organization_id=ORG_A, workspace_ids=WORKSPACES[ORG_A]
        )
    assert again.verified, again.as_dict()


def test_a_member_left_without_organizations_can_still_delete_their_account(env: PrivacyEnv):
    assert env.client.post(DELETE_A, headers=env.auth(ALICE), json=BODY).status_code == 200
    headers = env.auth(CAROL)
    preview = env.client.get(f"{API}/account/deletion-preview", headers=headers).json()
    assert (preview["can_delete"], preview["organizations"]) == (True, [])
    r = env.client.post(
        f"{API}/account/delete", headers=headers,
        json={"current_password": PASSWORD, "confirmation": "DELETE"},
    )
    assert r.status_code == 200 and r.json()["verification"]["references_cleared"] is True


@pytest.mark.parametrize(
    ("user_id", "expected"),
    [
        (DAVE, (403, "permission_denied", "Role 'admin' is not permitted for this action.")),
        (CAROL, (403, "permission_denied", "Role 'viewer' is not permitted for this action.")),
        (BOB, (403, "permission_denied", "You are not a member of this organization.")),
    ],
)
def test_only_the_owner_may_delete(env: PrivacyEnv, user_id, expected):
    headers = env.auth(user_id)
    before = env.snapshot()
    assert _error(env.client.post(DELETE_A, headers=headers, json=BODY)) == expected
    assert env.snapshot() == before


def test_anonymous_is_401(env: PrivacyEnv):
    assert env.client.post(DELETE_A, json=BODY).status_code == 401


def test_the_service_rechecks_the_role_under_the_lock(env: PrivacyEnv):
    with env.session() as s:
        dave = s.get(User, DAVE)
        with pytest.raises(PermissionDeniedError):
            erasure.delete_organization(
                s, user=dave, organization_id=ORG_A, current_password=PASSWORD,
                confirm_organization_name=NAME[ORG_A],
            )
        s.rollback()
    assert env.count(Organization, Organization.id == ORG_A) == 1


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({**BODY, "current_password": "wrong-password-0"}, "current_password_incorrect"),
        ({**BODY, "confirm_organization_name": NAME[ORG_A].upper()}, "organization_name_mismatch"),
        ({**BODY, "confirm_organization_name": NAME[ORG_B]}, "organization_name_mismatch"),
        ({"current_password": PASSWORD}, "validation_error"),
        ({**BODY, "organization_id": ORG_B}, "validation_error"),
    ],
)
def test_reauthentication_and_confirmation_refusals(env: PrivacyEnv, body, code):
    headers = env.auth(ALICE)
    before = env.snapshot()
    r = env.client.post(DELETE_A, headers=headers, json=body)
    assert (r.status_code, r.json()["error"]["code"]) == (422, code)
    assert env.snapshot() == before


@pytest.mark.parametrize("status", ["claimed", "running", "cancel_requested"])
def test_an_active_job_refuses_the_deletion(env: PrivacyEnv, status):
    with env.engine.begin() as conn:
        conn.execute(update(Job.__table__).where(Job.id == "a2-job").values(status=status))
    headers = env.auth(ALICE)
    before = env.snapshot()
    r = env.client.post(DELETE_A, headers=headers, json=BODY)
    assert _error(r)[:2] == (409, "organization_jobs_active")
    assert env.snapshot() == before


def test_deleting_another_organization_by_its_own_owner_leaves_a_untouched(env: PrivacyEnv):
    """C is ALICE's too: deleting it removes only C."""
    a_rows = env.snapshot(belongs_to(ORG_A))
    r = env.client.post(
        f"{API}/organizations/{ORG_C}/delete", headers=env.auth(ALICE),
        json={"current_password": PASSWORD, "confirm_organization_name": NAME[ORG_C]},
    )
    assert r.status_code == 200 and r.json()["verification"]["references_cleared"] is True
    assert env.snapshot(belongs_to(ORG_A)) == a_rows


def test_the_request_runs_no_content_scan(env: PrivacyEnv, monkeypatch):
    scans: list[object] = []
    real = verification._content_findings

    def counted(*args, **kwargs):
        scans.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(verification, "_content_findings", counted)
    r = env.client.post(DELETE_A, headers=env.auth(ALICE), json=BODY)
    assert r.status_code == 200, r.text
    assert scans == []
    assert r.json()["verification"]["content_scan"] == "operator"
