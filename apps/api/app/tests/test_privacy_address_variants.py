"""P6-PRIV-1: every reachable variant of a deleted account's address is erased or flagged.

Adversarial review r3 (on 22cb462c) measured forms of a subject's own address that were neither
erased nor flagged: the operator's verification said ``verified`` with nothing to review (exit
0) while the address stayed. The rule now is that the verifier never reports verified while a
reachable variant remains. The shared matcher (``app.privacy.matching``) looks for the address
in every form a writer may have produced:

* Unicode NFC and NFD;
* full case-folding (non-ASCII letters included, not SQL ``lower()``);
* JSON ``\\uXXXX`` escapes;
* percent-encoding;
* the IDNA A-label and U-label forms of the domain.

The SQL pre-filters use only ASCII fragments that every one of those forms contains, so no
dialect's ``lower()`` decides what is read. The confirmation step classifies each occurrence
``WHOLE`` (erased) or ``AMBIGUOUS`` (left and flagged). A text that holds the address's local
part and its domain as separate tokens, but no form of the address, cannot be decided either
way, so it is flagged.

Each case below plants one bystander audit record in organization B, holding the text in
``reason`` (a text column) and in ``new_state`` (a JSON column). The verified subject then
deletes itself, and the operator's full verification and its exit status are read. R3-1 also
runs through the product's own invitation writer.
"""

from __future__ import annotations

import json
import unicodedata
from collections.abc import Iterator
from datetime import UTC, datetime
from urllib.parse import quote

import pytest
from sqlalchemy import insert, update

from app.audit.models import AuditLog
from app.organizations.models import OrganizationInvitation, User
from app.privacy import cli
from app.privacy.erasure import ERASED
from app.privacy.verification import verify_subject_erased
from app.tests._privacy_support import (
    API,
    BOB,
    CAROL,
    EMAIL,
    ORG_B,
    PASSWORD,
    PrivacyEnv,
    privacy_world,
)

JOSE = "josé@privacy.example"
IDN = "carol@exämple.com"
_L, _D = EMAIL[CAROL].split("@")


@pytest.fixture
def env(tmp_path) -> Iterator[PrivacyEnv]:
    with privacy_world(tmp_path / "variants.db") as e:
        yield e


def _subject(env: PrivacyEnv, who: str) -> tuple[str, str, dict[str, str], str]:
    """(user id, stored address, bearer header, password) of a VERIFIED subject."""
    if who == "carol":
        return CAROL, EMAIL[CAROL], env.auth(CAROL), PASSWORD
    address = {"jose": JOSE, "idn": IDN}[who]
    r = env.client.post(
        f"{API}/auth/register",
        json={"email": address, "full_name": "S", "password": "subject-pass-1",
              "organization_name": f"S Org {who}"},
    )
    assert r.status_code == 201, r.text
    uid = r.json()["user"]["id"]
    with env.engine.begin() as conn:
        conn.execute(
            update(User.__table__).where(User.id == uid)
            .values(email_verified_at=datetime.now(UTC))
        )
    return uid, r.json()["user"]["email"], {
        "Authorization": f"Bearer {r.json()['access_token']}"
    }, "subject-pass-1"


#: (case, subject, text, outcome). Review r3 cap 076's silent cases first, then forms the same
#: rule must also reach. "erased": both columns cleared, verification exit 0. "flagged": both
#: columns left, listed under content_ambiguous, exit 4.
CASES = [
    ("u7-url-encoded-at", "carol", f"Note: {_L}%40{_D}", "erased"),
    ("url-encoded-all", "jose", f"Note: {quote(JOSE, safe='')}", "erased"),
    ("j2-non-ascii-capitals", "jose", f"Note: {JOSE.upper()}", "erased"),
    ("j3-nfd", "jose", f"Note: {unicodedata.normalize('NFD', JOSE)}", "erased"),
    ("i2-idna-a-label", "idn", "Note: carol@xn--exmple-cua.com", "erased"),
    ("idna-a-label-capitals", "idn", "Note: carol@XN--EXMPLE-CUA.COM", "erased"),
    ("i3-idn-capitals", "idn", "Note: carol@EXÄMPLE.COM", "erased"),
    ("json-escape-in-text", "jose", 'Note: {"email": "JOS\\u00c9@privacy.example"}', "erased"),
    ("html-entity", "carol", f"Note: {_L}&#64;{_D}", "flagged"),
    ("at-spelled-out", "carol", f"Note: {_L} [at] {_D}", "flagged"),
]


@pytest.mark.parametrize(
    ("who", "text", "outcome"), [c[1:] for c in CASES], ids=[c[0] for c in CASES]
)
def test_every_reachable_variant_is_erased_or_flagged(
    env: PrivacyEnv, capsys, who, text, outcome
):
    uid, address, headers, password = _subject(env, who)
    with env.engine.begin() as conn:
        conn.execute(
            insert(AuditLog.__table__).values(
                id="pl-variant", organization_id=ORG_B, actor_user_id=BOB,
                action="note.created", entity_type="note", entity_id="n-variant",
                reason=text, new_state={"v": text}, context={},
            )
        )

    r = env.client.post(
        f"{API}/account/delete", headers=headers,
        json={"current_password": password, "confirmation": "DELETE"},
    )

    assert r.status_code == 200, r.text
    assert r.json()["email_keyed_data"]["status"] == "included"
    [row] = env.rows(AuditLog, AuditLog.id == "pl-variant")
    with env.session() as s:
        report = verify_subject_erased(s, user_id=uid, email=address)
    if outcome == "erased":
        assert (row["reason"], row["new_state"]) == (None, {"v": ERASED})
        assert report.verified and not report.needs_review, report.as_dict()
        expected_exit = cli.EXIT_OK
    else:
        assert (row["reason"], row["new_state"]) == (text, {"v": text})
        assert report.verified and report.needs_review, report.as_dict()
        assert {("audit_logs", "reason"), ("audit_logs", "new_state")} <= {
            (f.table, f.column) for f in report.content_ambiguous
        }
        expected_exit = cli.EXIT_REVIEW
    assert cli.main(["verify-account", "--user-id", uid, "--email", address]) == expected_exit
    capsys.readouterr()


def test_a_capitalised_non_ascii_invitation_goes_with_its_owner(env: PrivacyEnv, capsys):
    """Review r3 R3-1 through the product: BOB invites ``JOSÉ@…`` (the local part keeps its
    case). The verified owner of ``josé@…`` deletes herself. The invitation and the address in
    its audit record go, and the verification passes with nothing left to review."""
    uid, address, headers, password = _subject(env, "jose")
    invited = env.client.post(
        f"{API}/organizations/{ORG_B}/invitations", headers=env.auth(BOB),
        json={"email": JOSE.upper(), "role": "viewer"},
    )
    assert invited.status_code == 201, invited.text
    invitation_id = invited.json()["id"]
    [record] = [a for a in env.rows(AuditLog) if a["entity_id"] == invitation_id]
    assert record["new_state"]["email"] == "JOSÉ@privacy.example"

    r = env.client.post(
        f"{API}/account/delete", headers=headers,
        json={"current_password": password, "confirmation": "DELETE"},
    )

    assert r.status_code == 200, r.text
    assert env.count(OrganizationInvitation, OrganizationInvitation.id == invitation_id) == 0
    [after] = env.rows(AuditLog, AuditLog.id == record["id"])
    assert after["new_state"]["email"] == ERASED
    assert cli.main(["verify-account", "--user-id", uid, "--email", address]) == cli.EXIT_OK
    assert json.loads(capsys.readouterr().out)["content_findings"] == []
