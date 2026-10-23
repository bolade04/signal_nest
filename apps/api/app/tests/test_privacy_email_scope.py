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
* **The same person, unverified.** She is erased by id, which includes the invitation she
  ACCEPTED (bound to her by ``accepted_by_user_id``) and her address in the audit records about
  it. The invitations and audit values found only by her address are left, and the preview,
  export and receipt say why.
* **An account that joined by invitation** (review r2 cap 023). Invited registration leaves
  the account unverified. Its export still lists the invitation it accepted, and its deletion
  leaves nothing of its address in the inviting organization. The operator's full verification,
  with the address, agrees.
* **The gate is read under the lock** (review r2 cap 025, mutant F1). An address verified after
  the request loaded the account, but before the deletion locked it, counts as verified.
* **Neighbouring addresses** (``xcarol_x@…`` and ``carol_x@….org``). CAROL's deletion leaves the
  audit values about them. An account holding a containing address does not make the operator's
  full verification of her erasure read "not verified".
* **Every context of review r2** (caps 019, 022, 065), planted in another organization or
  written by the product's own invitation writer. A ``WHOLE`` mention of her address is erased.
  An ``AMBIGUOUS`` one is left: other people's addresses, and her own after ``email=``,
  ``users/``, ``#`` or ``{`` or before ``.Word``, which registration's grammar cannot tell apart.
  The operator's verification reports what was left under ``content_ambiguous``, and it never
  says verified while a whole mention remains (her address as a JSON key).
* **The operator's erase-email** is not blocked by an unverified account holding the address
  in another letter case (review r2 R2-6). An account with a verified address still blocks it.
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
from app.privacy import cli, erasure
from app.privacy.erasure import ERASED, EmailScope
from app.privacy.matching import Mention, Subject, mention
from app.privacy.verification import verify_subject_erased
from app.tests._privacy_support import (
    ALICE,
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


def test_an_unverified_account_is_erased_by_id_and_by_what_it_accepted(env: PrivacyEnv):
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
    # Bound to her by id: the invitation she accepted, and her address in its two records.
    invitations = {i["id"] for i in env.rows(OrganizationInvitation)}
    assert invitations == {"prv-inv-invitee", "prv-inv-b-carol"}
    audit = {a["id"]: a for a in env.rows(AuditLog)}
    assert audit["prv-audit-1"]["new_state"] == {"email": ERASED, "role": "viewer"}
    assert audit["prv-audit-2"]["actor_user_id"] is None
    assert audit["prv-audit-2"]["new_state"] == {
        "email": ERASED, "role": "viewer", "accepted_by_user_id": ERASED
    }
    # Found only by her address: left in place, because nothing proved the address is hers.
    for audit_id in ("prv-audit-4", "prv-audit-7"):
        assert audit[audit_id] == audit_before[audit_id], audit_id
    # Keyed on her id: erased as before.
    assert env.count(User, User.id == CAROL) == 0
    assert env.count(OrganizationMember, OrganizationMember.user_id == CAROL) == 0
    assert env.count(AuthSession, AuthSession.user_id == CAROL) == 0
    assert audit["prv-audit-3"]["entity_id"] is None
    receipt = r.json()
    assert receipt["counts"]["anonymised"] == {"audit_logs": 3}  # each record counted once
    assert receipt["counts"]["deleted"]["organization_invitations"] == 1
    assert receipt["verification"]["references_cleared"] is True
    for said in (preview, export, receipt):
        assert said["email_keyed_data"]["status"] == "excluded_unverified"
    assert [i["id"] for i in export["invitations_received"]] == ["prv-inv-carol"]


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


# --------------------------------------------------------------------------- #
# Every context of review r2, in both directions
# --------------------------------------------------------------------------- #
_LOCAL, _DOMAIN_ = EMAIL[CAROL].split("@")
#: (case, text, whose, erased). Caps 019/022: "other" is another person's address, "hers" is
#: her own in some context. ``erased`` follows the matcher: WHOLE is erased, AMBIGUOUS is left.
_CONTEXTS = [
    ("o1", f"x{EMAIL[CAROL]}", "other", False),
    ("o2", f"o'{EMAIL[CAROL]}", "other", False),
    ("o3", f"í{EMAIL[CAROL]}", "other", False),
    ("o4", f"{EMAIL[CAROL]}é.com", "other", False),
    ("o5", f"{EMAIL[CAROL]}.org", "other", False),
    ("o6", f"{_LOCAL}+tag@{_DOMAIN_}", "other", False),
    ("u1", f"email={EMAIL[CAROL]}", "hers", False),
    ("u2", f"users/{EMAIL[CAROL]}", "hers", False),
    ("u3", f"{{{EMAIL[CAROL]}}}", "hers", False),
    ("u4", f"{EMAIL[CAROL]}.Thanks", "hers", False),
    ("u5", f"mailto:{EMAIL[CAROL]}", "hers", True),
    ("u6", f'"Carol" <{EMAIL[CAROL]}>', "hers", True),
    ("u7", f"{_LOCAL}%40{_DOMAIN_}", "hers", True),  # percent-decoded: whole (review r3)
    ("u9", EMAIL[CAROL].upper(), "hers", True),
    ("u10", f"#{EMAIL[CAROL]}", "hers", False),
]
_HERS = Subject(ids=(CAROL,), addresses=(EMAIL[CAROL],))


def _plant_notes(env: PrivacyEnv, cases) -> None:
    with env.engine.begin() as conn:
        for case, text, _whose, _erased in cases:
            conn.execute(
                insert(AuditLog.__table__).values(
                    id=f"pl-{case}", organization_id=ORG_B, actor_user_id=BOB,
                    action="note.created", entity_type="note", entity_id=f"n-{case}",
                    reason=f"Note: {text}", new_state={"v": text}, context={},
                )
            )


def test_every_context_is_erased_exactly_when_it_names_her_whole(env: PrivacyEnv):
    _plant_notes(env, _CONTEXTS)
    before = {a["id"]: a for a in env.rows(AuditLog) if a["id"].startswith("pl-")}

    r = _delete(env, env.auth(CAROL), PASSWORD)

    assert r.status_code == 200, r.text
    after = {a["id"]: a for a in env.rows(AuditLog) if a["id"].startswith("pl-")}
    for case, _text, _whose, erased in _CONTEXTS:
        row = after[f"pl-{case}"]
        if erased:
            assert (row["reason"], row["new_state"]) == (None, {"v": ERASED}), case
        else:
            assert row == before[f"pl-{case}"], case
    # What was left is reported, row by row, for an operator: never passed over in silence.
    left = [c for c in _CONTEXTS if not c[3] and mention(c[1], _HERS) is Mention.AMBIGUOUS]
    assert len(left) == 10  # o1-o5 and u1-u4, u10; o6 (another address) is no occurrence
    with env.session() as s:
        report = verify_subject_erased(s, user_id=CAROL, email=EMAIL[CAROL])
    assert report.verified and report.needs_review, report.as_dict()
    assert {(f.table, f.column, f.rows) for f in report.content_ambiguous} == {
        ("audit_logs", "reason", len(left)), ("audit_logs", "new_state", len(left))
    }


def test_the_verifier_never_says_verified_while_a_whole_mention_remains(env: PrivacyEnv):
    """Review r2 cap 019 u8: her address as a JSON object key, which erasure does not rewrite."""
    with env.engine.begin() as conn:
        conn.execute(
            insert(AuditLog.__table__).values(
                id="pl-u8", organization_id=ORG_B, actor_user_id=BOB, action="note.created",
                entity_type="note", entity_id="n-u8", new_state={EMAIL[CAROL]: "viewer"},
                context={},
            )
        )
    assert _delete(env, env.auth(CAROL), PASSWORD).status_code == 200
    with env.session() as s:
        report = verify_subject_erased(s, user_id=CAROL, email=EMAIL[CAROL])
    assert not report.verified
    assert [(f.table, f.column) for f in report.content_findings] == [("audit_logs", "new_state")]


@pytest.mark.parametrize(
    "other",
    [f"o'{EMAIL[CAROL]}", f"í{EMAIL[CAROL]}", f"x{EMAIL[CAROL]}", f"{EMAIL[CAROL]}é.com"],
    ids=["apostrophe", "non-ascii-before", "longer-local-part", "non-ascii-domain"],
)
def test_a_neighbour_invited_through_the_product_is_left_alone(env: PrivacyEnv, other):
    """Review r2 cap 065: the invitation service writes its own audit record about ``other``;
    CAROL's verified deletion must leave that record and the invitation untouched."""
    r = env.client.post(
        f"{API}/organizations/{ORG_B}/invitations", headers=env.auth(BOB),
        json={"email": other, "role": "viewer"},
    )
    assert r.status_code == 201, r.text
    invitation_id = r.json()["id"]
    [record] = [a for a in env.rows(AuditLog) if a["entity_id"] == invitation_id]
    assert record["new_state"]["email"] == other
    invitation = env.rows(OrganizationInvitation, OrganizationInvitation.id == invitation_id)

    assert _delete(env, env.auth(CAROL), PASSWORD).status_code == 200

    assert env.rows(AuditLog, AuditLog.id == record["id"]) == [record]
    assert invitation == env.rows(
        OrganizationInvitation, OrganizationInvitation.id == invitation_id
    )


# --------------------------------------------------------------------------- #
# An account that joined by invitation (unverified by default)
# --------------------------------------------------------------------------- #
NEWBIE = "newbie@privacy.example"
NEWBIE_PASSWORD = "newbie-pass-1"


def test_an_invited_account_takes_its_invitation_with_it(env: PrivacyEnv, capsys):
    """Review r2 cap 023: invited registration leaves the account unverified; its invitation and
    the records about it are its own by id, so export lists them and deletion clears them."""
    invited = env.client.post(
        f"{API}/organizations/{ORG_A}/invitations", headers=env.auth(ALICE),
        json={"email": NEWBIE, "role": "viewer"},
    )
    assert invited.status_code == 201, invited.text
    invitation_id = invited.json()["id"]
    joined = env.client.post(
        f"{API}/auth/invitations/register",
        json={"token": invited.json()["token"], "full_name": "New Bie",
              "password": NEWBIE_PASSWORD},
    )
    assert joined.status_code == 201, joined.text
    assert joined.json()["user"]["email_verified"] is False  # auth semantics unchanged
    uid = joined.json()["user"]["id"]
    headers = {"Authorization": f"Bearer {joined.json()['access_token']}"}
    naming = [a["id"] for a in env.rows(AuditLog) if NEWBIE in json.dumps(a["new_state"])]
    assert len(naming) == 2  # organization_invitation.created and .accepted

    export = env.client.get(EXPORT, headers=headers).json()
    r = _delete(env, headers, NEWBIE_PASSWORD)

    assert [i["id"] for i in export["invitations_received"]] == [invitation_id]
    assert export["email_keyed_data"]["status"] == "excluded_unverified"
    assert r.status_code == 200, r.text
    assert r.json()["counts"]["deleted"]["organization_invitations"] == 1
    assert r.json()["counts"]["anonymised"] == {"audit_logs": 2}
    # Nothing of the address is left in the organization it joined.
    assert env.count(OrganizationInvitation, OrganizationInvitation.email == NEWBIE) == 0
    assert not [a for a in env.rows(AuditLog) if NEWBIE in json.dumps(a, default=str)]
    owner_export = env.client.get(f"{API}/organizations/{ORG_A}/export", headers=env.auth(ALICE))
    assert owner_export.status_code == 200 and NEWBIE not in owner_export.text
    # The operator's full verification, address included, agrees.
    assert cli.main(["verify-account", "--user-id", uid, "--email", NEWBIE]) == cli.EXIT_OK
    assert json.loads(capsys.readouterr().out)["verified"] is True


# --------------------------------------------------------------------------- #
# The gate is read under the lock (mutant F1)
# --------------------------------------------------------------------------- #
def test_an_address_verified_before_the_lock_counts_as_verified(env: PrivacyEnv):
    """Review r2 cap 025, deterministically: the request loaded the account unverified; a
    verification committed before the deletion locked the row; the deletion acts on the row."""
    headers, _own = _register(env, INVITEE)
    uid = env.client.get(f"{API}/auth/me", headers=headers).json()["user"]["id"]
    with env.session() as s:
        stale = s.get(User, uid)
        assert erasure.email_scope(stale) is EmailScope.EXCLUDED_UNVERIFIED
        with env.engine.begin() as conn:  # a confirm that commits in between
            conn.execute(
                update(User.__table__).where(User.id == uid)
                .values(email_verified_at=datetime.now(UTC))
            )
        result = erasure.delete_account(s, user=stale, current_password=OTHER_PASSWORD)
        s.commit()
    assert result.email_scope is EmailScope.INCLUDED
    assert result.counts.deleted["organization_invitations"] == 1
    assert env.count(OrganizationInvitation, OrganizationInvitation.email == INVITEE) == 0


# --------------------------------------------------------------------------- #
# The operator's erase-email
# --------------------------------------------------------------------------- #
def test_an_unverified_account_does_not_block_erase_email(env: PrivacyEnv, capsys):
    """Review r2 cap 026: a registrant holding INVITEE in other letter case used to block it."""
    _register(env, f"{INVITEE.split('@')[0].upper()}@{INVITEE.split('@')[1]}")

    # It runs; its own verification then reports, honestly, the one copy it does not touch:
    # the registrant's account, which holds the address in other letter case.
    assert cli.main(["erase-email", "--email", INVITEE, "--confirm"]) == cli.EXIT_FINDINGS

    printed = json.loads(capsys.readouterr().out)
    assert printed["counts"]["deleted"] == {"organization_invitations": 1}
    assert printed["verification"]["content_findings"] == [
        {"table": "users", "column": "email", "rows": 1}
    ]
    assert env.count(OrganizationInvitation, OrganizationInvitation.email == INVITEE) == 0
    # An account whose address is verified still blocks it: that is its own deletion.
    assert cli.main(["erase-email", "--email", EMAIL[CAROL].upper(), "--confirm"]) == (
        cli.EXIT_REFUSED
    )


def test_the_deletion_event_carries_the_email_scope(env: PrivacyEnv, caplog):
    """Review r2 R2-5: an operator reading the log knows which verification run applies."""
    headers, _own = _register(env, INVITEE)
    with caplog.at_level("INFO", logger="app.privacy.routes"):
        assert _delete(env, headers).status_code == 200
    [event] = [r for r in caplog.records if r.getMessage() == "privacy.account.deleted"]
    assert event.extra_fields["email_scope"] == "excluded_unverified"


# --------------------------------------------------------------------------- #
# Behaviour pinned so the docs can state it (review r3 R3-3, R3-4, R3-5)
# --------------------------------------------------------------------------- #
def test_the_accepted_invitation_clause_stays_in_its_organization(env: PrivacyEnv):
    """R3-4: a record in ANOTHER organization that names the same invitation id is not the
    invitation's, so the unverified owner's deletion leaves it untouched."""
    with env.engine.begin() as conn:
        conn.execute(
            update(User.__table__).where(User.id == CAROL).values(email_verified_at=None)
        )
        conn.execute(
            insert(AuditLog.__table__).values(
                id="pl-same-entity-b", organization_id=ORG_B, actor_user_id=BOB,
                action="organization_invitation.created",
                entity_type="organization_invitation", entity_id="prv-inv-carol",
                new_state={"email": EMAIL[CAROL], "role": "viewer"}, context={},
            )
        )
    [planted] = env.rows(AuditLog, AuditLog.id == "pl-same-entity-b")

    assert _delete(env, env.auth(CAROL), PASSWORD).status_code == 200

    assert env.rows(AuditLog, AuditLog.id == "pl-same-entity-b") == [planted]
    [created] = env.rows(AuditLog, AuditLog.id == "prv-audit-1")  # A's own record: cleared
    assert created["new_state"]["email"] == ERASED


def test_a_value_is_erased_whole_when_it_names_her_whole(env: PrivacyEnv):
    """R3-3: erasure clears whole values. A string naming her WHOLE goes entirely, even if it
    also holds another person's address; separate list items are separate values."""
    mixed = f"cc {EMAIL[CAROL]} and x{EMAIL[CAROL]}"
    with env.engine.begin() as conn:
        conn.execute(
            insert(AuditLog.__table__).values(
                id="pl-mixed", organization_id=ORG_B, actor_user_id=BOB, action="note.created",
                entity_type="note", entity_id="n-mixed", reason=mixed,
                new_state={"v": mixed, "to": [EMAIL[CAROL], f"x{EMAIL[CAROL]}"]}, context={},
            )
        )

    assert _delete(env, env.auth(CAROL), PASSWORD).status_code == 200

    [row] = env.rows(AuditLog, AuditLog.id == "pl-mixed")
    assert row["reason"] is None
    assert row["new_state"] == {"v": ERASED, "to": [ERASED, f"x{EMAIL[CAROL]}"]}


def test_erase_email_takes_an_active_invited_accounts_accepted_invitation(env: PrivacyEnv):
    """R3-5: the operator's erase-email for an address held by an ACTIVE, unverified invited
    account deletes that account's accepted invitation and clears the address from its records,
    while the account and its membership stay."""
    invited = env.client.post(
        f"{API}/organizations/{ORG_A}/invitations", headers=env.auth(ALICE),
        json={"email": NEWBIE, "role": "viewer"},
    )
    joined = env.client.post(
        f"{API}/auth/invitations/register",
        json={"token": invited.json()["token"], "full_name": "New Bie",
              "password": NEWBIE_PASSWORD},
    )
    uid = joined.json()["user"]["id"]

    assert cli.main(["erase-email", "--email", NEWBIE, "--confirm"]) == cli.EXIT_FINDINGS

    assert env.count(OrganizationInvitation, OrganizationInvitation.id == invited.json()["id"]) == 0
    assert env.count(User, User.id == uid) == 1
    assert env.count(
        OrganizationMember,
        (OrganizationMember.user_id == uid) & (OrganizationMember.organization_id == ORG_A),
    ) == 1

