"""P6-PRIV-1: data found only by an email address is the account's once the address is proven.

Self-registration is open and does not prove the address (sign-in does not require a verified
one), and account identity is exact while invitation and audit matching ignore letter case. So
an account can be registered under an address it does not control: a letter-case variant of
another account's address, or an address invited somewhere that has no account. Such an
account must not export the invitations addressed to that address, and deleting itself must not
delete them or rewrite other organizations' audit records.

The rule: every limb of export and erasure keyed on the address (invitations addressed to it,
audit values naming it) runs only when ``users.email_verified_at`` is set. An unverified
account is exported and erased by its id alone. The export, the deletion preview and the
receipt say so (``email_keyed_data.status == "excluded_unverified"``). The world is
``_privacy_support``'s; only CAROL is verified.

* **A case variant of a verified address** (``CAROL_X@…``) and **an invited address with no
  account** (``INVITEE``), each registered and so unverified. The export lists no invitation and
  names neither organization. Deleting the account leaves every row that existed before it
  byte-identical. Only its own organization's anonymised registration record remains.
* **A suffix of another address** (``x@…``), unverified and then verified. Neither deletion
  rewrites the audit records naming ``carol_x@…``, because addresses match only whole.
* **The verified owner of the address** (CAROL). Her export lists both invitations, and her
  deletion erases them and anonymises every audit value naming her.
* **The same person, unverified.** She is erased by id only: her membership, sessions, tokens
  and the audit values naming her id. The invitations and audit values found only by her address
  are left, and the preview, export and receipt say why.
* **Neighbouring addresses** (``xcarol_x@…`` and ``carol_x@….org``). CAROL's deletion leaves the
  audit values about them. An account holding a containing address does not make the operator's
  full verification of her erasure read "not verified".
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import insert, update

from app.audit.models import AuditLog
from app.auth.models import AuthSession
from app.organizations.models import OrganizationInvitation, OrganizationMember, User
from app.privacy.erasure import ERASED
from app.privacy.verification import verify_subject_erased
from app.tests._privacy_support import (
    API,
    BOB,
    CAROL,
    EMAIL,
    INVITEE,
    NAME,
    ORG_A,
    ORG_B,
    PASSWORD,
    PASSWORD_HASH,
    PrivacyEnv,
    privacy_world,
)

EXPORT = f"{API}/account/export"
DELETE = f"{API}/account/delete"
PREVIEW = f"{API}/account/deletion-preview"
OTHER_PASSWORD = "mallory-pass-1"
_LOCAL, _DOMAIN = EMAIL[CAROL].split("@")
#: CAROL's address with the local part in capitals: a different account to the identity rule.
CAROL_VARIANT = f"{_LOCAL.upper()}@{_DOMAIN}"


@pytest.fixture
def env(tmp_path) -> Iterator[PrivacyEnv]:
    with privacy_world(tmp_path / "email-scope.db") as e:
        yield e


def _register(env: PrivacyEnv, email: str) -> tuple[dict[str, str], str]:
    """Open self-registration; returns the bearer header and the account's own organization."""
    r = env.client.post(
        f"{API}/auth/register",
        json={"email": email, "full_name": "Mallory", "password": OTHER_PASSWORD,
              "organization_name": "Mallory Org"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["user"]["email_verified"] is False
    [membership] = body["memberships"]
    return {"Authorization": f"Bearer {body['access_token']}"}, membership["organization_id"]


def _delete(env: PrivacyEnv, headers: dict[str, str], password: str = OTHER_PASSWORD):
    return env.client.post(
        DELETE, headers=headers, json={"current_password": password, "confirmation": "DELETE"}
    )


def _world(env: PrivacyEnv) -> tuple[dict[str, tuple], list[dict]]:
    """Every row of every table, with the audit records apart (as dicts, in id order)."""
    snapshot = env.snapshot()
    snapshot.pop("audit_logs")
    return snapshot, env.rows(AuditLog)


def _assert_only_its_own_rows_went(env: PrivacyEnv, before, *, own_organization: str) -> None:
    """Every row that existed before the registrant is byte-identical and nothing else remains,
    except its own organization's audit record, which deleting that organization anonymised."""
    tables_before, audit_before = before
    tables_after, audit_after = _world(env)
    assert tables_after == tables_before
    assert [a for a in audit_after if a["organization_id"] != own_organization] == audit_before
    [own] = [a for a in audit_after if a["organization_id"] == own_organization]
    assert (own["action"], own["actor_user_id"], own["entity_id"]) == ("auth.register", None, None)


# --------------------------------------------------------------------------- #
# An address the account never proved
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "address", [CAROL_VARIANT, INVITEE], ids=["case-variant-of-an-account", "invited-address"]
)
def test_an_unproven_address_exports_no_invitation_of_another_organization(
    env: PrivacyEnv, address: str
):
    headers, _own = _register(env, address)

    r = env.client.get(EXPORT, headers=headers)

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["invitations_received"] == []
    text = json.dumps(body)
    for foreign in (ORG_A, ORG_B, NAME[ORG_A], NAME[ORG_B]):
        assert foreign not in text, foreign
    assert body["email_keyed_data"]["status"] == "excluded_unverified"
    assert "not verified" in body["email_keyed_data"]["note"]


@pytest.mark.parametrize(
    "address", [CAROL_VARIANT, INVITEE], ids=["case-variant-of-an-account", "invited-address"]
)
def test_an_unproven_address_deletes_and_rewrites_nothing_of_another_organization(
    env: PrivacyEnv, address: str
):
    before = _world(env)
    headers, own = _register(env, address)
    preview = env.client.get(PREVIEW, headers=headers).json()

    r = _delete(env, headers)

    assert r.status_code == 200, r.text
    _assert_only_its_own_rows_went(env, before, own_organization=own)
    receipt = r.json()
    assert "organization_invitations" not in receipt["counts"]["deleted"]
    assert receipt["counts"]["anonymised"] == {"audit_logs": 1}  # its own registration record
    assert receipt["email_keyed_data"]["status"] == "excluded_unverified"
    assert preview["email_keyed_data"]["status"] == "excluded_unverified"


def test_the_receipt_carries_no_finding_about_another_account(env: PrivacyEnv):
    """The case variant's receipt used to report the real CAROL's ``users.email`` and
    ``email_verification_tokens.email_snapshot`` (review r1, cap 027)."""
    headers, _own = _register(env, CAROL_VARIANT)

    receipt = _delete(env, headers).json()

    check = receipt["verification"]
    assert check.get("content_findings", []) == []
    assert "content_findings" not in check
    assert check["references_cleared"] is True
    assert check["reference_findings"] == []
    assert env.count(User, User.id == CAROL) == 1


@pytest.mark.parametrize("verified", [False, True], ids=["unverified", "verified"])
def test_a_suffix_of_another_address_rewrites_no_audit_record(env: PrivacyEnv, verified: bool):
    """``x@privacy.example`` is held by no one and ends ``carol_x@privacy.example``."""
    before = _world(env)
    headers, own = _register(env, "x@privacy.example")
    if verified:
        with env.engine.begin() as conn:
            conn.execute(
                update(User.__table__)
                .where(User.email == "x@privacy.example")
                .values(email_verified_at=datetime.now(UTC))
            )

    r = _delete(env, headers)

    assert r.status_code == 200, r.text
    _assert_only_its_own_rows_went(env, before, own_organization=own)
    expected = "included" if verified else "excluded_unverified"
    assert r.json()["email_keyed_data"]["status"] == expected


# --------------------------------------------------------------------------- #
# The owner of the address
# --------------------------------------------------------------------------- #
def test_the_verified_owner_of_the_address_still_exports_and_erases_it(env: PrivacyEnv):
    headers = env.auth(CAROL)
    export = env.client.get(EXPORT, headers=headers).json()
    assert {i["id"] for i in export["invitations_received"]} == {"prv-inv-carol", "prv-inv-b-carol"}
    assert export["email_keyed_data"]["status"] == "included"
    preview = env.client.get(PREVIEW, headers=headers).json()
    assert preview["email_keyed_data"]["status"] == "included"

    r = _delete(env, headers, PASSWORD)

    assert r.status_code == 200, r.text
    receipt = r.json()
    assert receipt["email_keyed_data"]["status"] == "included"
    assert receipt["counts"]["deleted"]["organization_invitations"] == 2
    assert receipt["counts"]["anonymised"] == {"audit_logs": 5}
    assert {i["id"] for i in env.rows(OrganizationInvitation)} == {"prv-inv-invitee"}


def test_an_unverified_account_is_erased_by_id_only(env: PrivacyEnv):
    with env.engine.begin() as conn:
        conn.execute(
            update(User.__table__).where(User.id == CAROL).values(email_verified_at=None)
        )
    headers = env.auth(CAROL)
    preview = env.client.get(PREVIEW, headers=headers).json()
    export = env.client.get(EXPORT, headers=headers).json()
    audit_before = {a["id"]: a for a in env.rows(AuditLog)}

    r = _delete(env, headers, PASSWORD)

    assert r.status_code == 200, r.text
    # Found only by her address: left in place, because nothing proved the address is hers.
    invitations = {i["id"]: i for i in env.rows(OrganizationInvitation)}
    assert set(invitations) == {"prv-inv-carol", "prv-inv-invitee", "prv-inv-b-carol"}
    assert invitations["prv-inv-carol"]["accepted_by_user_id"] is None  # her id: cleared
    audit = {a["id"]: a for a in env.rows(AuditLog)}
    for audit_id in ("prv-audit-1", "prv-audit-4", "prv-audit-7"):  # her address only
        assert audit[audit_id] == audit_before[audit_id], audit_id
    # Keyed on her id: erased as before.
    assert env.count(User, User.id == CAROL) == 0
    assert env.count(OrganizationMember, OrganizationMember.user_id == CAROL) == 0
    assert env.count(AuthSession, AuthSession.user_id == CAROL) == 0
    accepted = audit["prv-audit-2"]
    assert accepted["actor_user_id"] is None
    assert accepted["new_state"] == {
        "email": EMAIL[CAROL], "role": "viewer", "accepted_by_user_id": ERASED
    }
    assert audit["prv-audit-3"]["entity_id"] is None
    receipt = r.json()
    assert receipt["counts"]["anonymised"] == {"audit_logs": 2}
    assert "organization_invitations" not in receipt["counts"]["deleted"]
    assert receipt["verification"]["references_cleared"] is True
    for said in (preview, export, receipt):
        assert said["email_keyed_data"]["status"] == "excluded_unverified"
    assert export["invitations_received"] == []


# --------------------------------------------------------------------------- #
# Neighbouring addresses
# --------------------------------------------------------------------------- #
def test_a_neighbours_address_is_not_erased_with_hers(env: PrivacyEnv):
    """Review r1 cap 028: an audit value about ``xcarol_x@…`` was erased by CAROL's deletion."""
    neighbours = (f"x{EMAIL[CAROL]}", f"{EMAIL[CAROL]}.org")
    with env.engine.begin() as conn:
        for n, other in enumerate(neighbours):
            conn.execute(
                insert(AuditLog.__table__).values(
                    id=f"planted-other-{n}", organization_id=ORG_B, actor_user_id=BOB,
                    action="organization_invitation.created",
                    entity_type="organization_invitation", entity_id=f"inv-other-{n}",
                    reason=f"Invited {other} at their request",
                    new_state={"email": other, "role": "viewer"}, context={},
                )
            )
        # Her own address, whole: at the end of a sentence and in capitals inside a note.
        conn.execute(
            insert(AuditLog.__table__).values(
                id="planted-hers", organization_id=ORG_B, actor_user_id=BOB,
                action="opportunity.reviewed", entity_type="opportunity", entity_id="b1-opp",
                reason=f"Spoke to {EMAIL[CAROL]}.",
                new_state={"note": f"cc {EMAIL[CAROL].upper()}"}, context={},
            )
        )
    others_before = [a for a in env.rows(AuditLog) if a["id"].startswith("planted-other-")]

    r = _delete(env, env.auth(CAROL), PASSWORD)

    assert r.status_code == 200, r.text
    assert [a for a in env.rows(AuditLog) if a["id"].startswith("planted-other-")] == others_before
    [hers] = env.rows(AuditLog, AuditLog.id == "planted-hers")
    assert (hers["reason"], hers["new_state"]) == (None, {"note": ERASED})


def test_an_account_holding_a_containing_address_does_not_fail_her_verification(
    env: PrivacyEnv,
):
    """Review r1 cap 025: ``users.email`` of ``xcarol_x@…`` made her erasure "not verified"."""
    with env.session() as s:
        s.add(User(id="prv-other", email=f"x{EMAIL[CAROL]}", full_name="Other",
                   hashed_password=PASSWORD_HASH))
        s.commit()

    assert _delete(env, env.auth(CAROL), PASSWORD).status_code == 200

    with env.session() as s:
        report = verify_subject_erased(s, user_id=CAROL, email=EMAIL[CAROL])
    assert report.verified, report.as_dict()
    assert env.count(User, User.id == "prv-other") == 1
