"""P6-PRIV-1: every reachable variant of a deleted account's address is erased or flagged.

Adversarial review r3 (on 22cb462c) measured forms of a subject's own address that were neither
erased nor flagged: the operator's verification said ``verified`` with nothing to review (exit
0) while the address stayed. The rule now is that the verifier never reports verified while a
reachable variant remains. The shared matcher (``app.privacy.matching``) looks for the address
in every form a writer may have produced:

* Unicode NFC and NFD;
* any letter case, non-ASCII letters included (Python lower-casing, not SQL ``lower()``);
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

**A shared upper case decides nothing** (reviews r5, R5-1). Some upper-case spellings belong to
two accounts: ``ΣΣ`` is the upper case of ``σσ`` and of ``σς``, ``IA`` of ``ia`` and ``ıa``,
``STRASSE`` of ``strasse`` and ``straße``, and ``JOSÉ`` of ``josé`` and ``joſé``. Such an
occurrence is flagged and never exported or erased, for either twin. **Registration's own domain
mapping counts** (R5-2): ``privacy。example``, fullwidth and mathematical letters are the same
domain to registration, so they are found too. A fullwidth ``＠`` is an explicit separator
(R5-3).

**No folding may join two distinct addresses** (review r4, R4-1). Registration treats
``straße@…`` and ``strasse@…`` as two accounts, and likewise ``bob@weiß…`` and ``bob@weiss…``,
``ſam``/``sam``, ``oﬃce``/``office`` and ``σς``/``σσ``. Full Unicode case-folding made each pair
equal, so a verified account at one form exported, deleted and rewrote another organization's
invitation and audit record for the other. Each pair now runs through the API in both directions,
and nothing of the other organization is reached.
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
    # JOSÉ is also the upper case of joſé, another registrable account: flagged (R5-1).
    ("j2-non-ascii-capitals", "jose", f"Note: {JOSE.upper()}", "flagged"),
    ("j3-nfd", "jose", f"Note: {unicodedata.normalize('NFD', JOSE)}", "erased"),
    ("i2-idna-a-label", "idn", "Note: carol@xn--exmple-cua.com", "erased"),
    ("idna-a-label-capitals", "idn", "Note: carol@XN--EXMPLE-CUA.COM", "erased"),
    ("i3-idn-capitals", "idn", "Note: carol@EXÄMPLE.COM", "erased"),
    ("json-escape-in-text", "jose", 'Note: {"email": "jos\\u00e9@privacy.example"}', "erased"),
    ("json-escape-capitals-in-text", "jose", 'Note: {"email": "JOS\\u00c9@privacy.example"}',
     "flagged"),
    # Review r5 R5-2: domain spellings registration maps to the same domain (UTS46).
    ("ideographic-full-stop", "carol", f"Note: {_L}@privacy\u3002example", "erased"),
    ("fullwidth-full-stop", "carol", f"Note: {_L}@privacy\uff0eexample", "erased"),
    ("halfwidth-ideographic-stop", "carol", f"Note: {_L}@privacy\uff61example", "erased"),
    ("fullwidth-label", "carol", f"Note: {_L}@\uff50\uff52\uff49\uff56\uff41\uff43\uff59.example",
     "erased"),
    ("mathematical-bold-letter", "carol", f"Note: {_L}@\U0001d429rivacy.example", "erased"),
    # Review r5 R5-3: a fullwidth or small commercial at is an explicit separator.
    ("fullwidth-at", "carol", f"Note: {_L}\uff20{_D}", "flagged"),
    ("small-commercial-at", "carol", f"Note: {_L}\ufe6b{_D}", "flagged"),
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


def test_a_capitalised_non_ascii_invitation_is_flagged_not_taken(env: PrivacyEnv, capsys):
    """Review r3 R3-1 through the product: BOB invites ``JOSÉ@…`` (the local part keeps its
    case). It is no longer silent (review r3), and since review r5 it is not taken either:
    ``JOSÉ`` is also the upper case of ``joſé``, another registrable account. So the verified
    owner of ``josé@…`` neither deletes it nor erases its audit value, and the verification
    flags both for review."""
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
    assert env.count(OrganizationInvitation, OrganizationInvitation.id == invitation_id) == 1
    assert env.rows(AuditLog, AuditLog.id == record["id"]) == [record]
    assert cli.main(["verify-account", "--user-id", uid, "--email", address]) == cli.EXIT_REVIEW
    printed = json.loads(capsys.readouterr().out)
    assert printed["content_findings"] == []
    assert {("organization_invitations", "email"), ("audit_logs", "new_state")} <= {
        (f["table"], f["column"]) for f in printed["content_ambiguous"]
    }


# --------------------------------------------------------------------------- #
# No folding joins two distinct addresses (review r4 R4-1)
# --------------------------------------------------------------------------- #
#: Pairs that registration keeps apart as two accounts, but that full case-folding made equal.
COLLISIONS = [
    ("strasse@privacy.example", "straße@privacy.example"),
    ("bob@weiss.example", "bob@weiß.example"),
    ("sam@privacy.example", "ſam@privacy.example"),
    ("office@privacy.example", "oﬃce@privacy.example"),
    ("σσ@privacy.example", "σς@privacy.example"),
]
_DIRECTIONS = [(a, b) for a, b in COLLISIONS] + [(b, a) for a, b in COLLISIONS]


@pytest.mark.parametrize(
    ("holder", "other"), _DIRECTIONS, ids=[f"{h}-holds-{o}-invited" for h, o in _DIRECTIONS]
)
def test_a_verified_account_never_reaches_a_distinct_twin_address(env: PrivacyEnv, holder, other):
    """The holder is verified at its own address; BOB invites the twin address into B through the
    product. The holder's export must not list that invitation, and its deletion must neither
    delete it nor rewrite B's audit record about it."""
    r = env.client.post(
        f"{API}/auth/register",
        json={"email": holder, "full_name": "H", "password": "holder-pass-1",
              "organization_name": "H Org"},
    )
    assert r.status_code == 201, r.text
    uid, headers = r.json()["user"]["id"], {"Authorization": f"Bearer {r.json()['access_token']}"}
    with env.engine.begin() as conn:
        conn.execute(
            update(User.__table__).where(User.id == uid)
            .values(email_verified_at=datetime.now(UTC))
        )
    invited = env.client.post(
        f"{API}/organizations/{ORG_B}/invitations", headers=env.auth(BOB),
        json={"email": other, "role": "marketer"},
    )
    assert invited.status_code == 201, invited.text
    invitation_id = invited.json()["id"]
    [record] = [a for a in env.rows(AuditLog) if a["entity_id"] == invitation_id]

    export = env.client.get(f"{API}/account/export", headers=headers).json()
    deleted = env.client.post(
        f"{API}/account/delete", headers=headers,
        json={"current_password": "holder-pass-1", "confirmation": "DELETE"},
    )

    assert [i["id"] for i in export["invitations_received"]] == []
    assert deleted.status_code == 200, deleted.text
    assert "organization_invitations" not in deleted.json()["counts"]["deleted"]
    assert env.count(OrganizationInvitation, OrganizationInvitation.id == invitation_id) == 1
    assert env.rows(AuditLog, AuditLog.id == record["id"]) == [record]


# --------------------------------------------------------------------------- #
# The in-request work stays bounded (review r4 R4-2)
# --------------------------------------------------------------------------- #
IVAN = "иван@пример.рф"


def test_an_address_without_ascii_still_selects_only_its_own_rows(env: PrivacyEnv):
    """``иван@пример.рф`` has no ASCII run in its local part. The in-request pre-filter must not
    select every row (it did: an empty term is TRUE), yet must still select the row that names
    it, in the casings a person types, in a text column and in a JSON column."""
    from sqlalchemy import func, or_, select

    from app.privacy.matching import Subject, prefilter

    with env.engine.begin() as conn:
        for n in range(20):
            conn.execute(
                insert(AuditLog.__table__).values(
                    id=f"pl-noise-{n}", organization_id=ORG_B, action="note.created",
                    entity_type="note", entity_id=f"n-{n}",
                    reason=f"Wrote to someone{n}@other.example",
                    new_state={"email": f"someone{n}@other.example"}, context={},
                )
            )
        for n, cased in enumerate((IVAN, IVAN.upper(), IVAN.capitalize())):
            conn.execute(
                insert(AuditLog.__table__).values(
                    id=f"pl-ivan-{n}", organization_id=ORG_B, action="note.created",
                    entity_type="note", entity_id=f"i-{n}", reason=f"Wrote to {cased}",
                    new_state={"email": cased}, context={},
                )
            )
    subject = Subject(addresses=(IVAN,))
    columns = (AuditLog.reason, AuditLog.new_state)
    with env.witness.connect() as conn:
        selected = conn.execute(
            select(AuditLog.id).where(or_(*(prefilter(c, subject) for c in columns)))
        ).scalars().all()
    assert sorted(selected) == ["pl-ivan-0", "pl-ivan-1", "pl-ivan-2"]
    # The operator's verification is off the request path: it reads every row for such an address.
    with env.witness.connect() as conn:
        everything = conn.execute(
            select(func.count()).select_from(AuditLog.__table__)
            .where(or_(*(prefilter(c, subject, exhaustive=True) for c in columns)))
        ).scalar_one()
    assert everything == env.count(AuditLog)


def test_a_casing_the_bounded_request_misses_is_still_found_by_the_verifier(env: PrivacyEnv):
    """The recorded limit of the bounded in-request filter: a mixed casing of a non-ASCII-only
    address (``иваН@…``) in a text column is not selected on SQLite, so the deletion leaves it. The
    operator's verification reads every row for such an address and reports it as a finding
    (exit 1), never as verified. (``иВАН`` would be flagged instead: ``В`` is also the upper case
    of ``ᲀ``, review r5.)"""
    r = env.client.post(
        f"{API}/auth/register",
        json={"email": IVAN, "full_name": "I", "password": "ivan-pass-123",
              "organization_name": "I Org"},
    )
    assert r.status_code == 201, r.text
    uid, headers = r.json()["user"]["id"], {"Authorization": f"Bearer {r.json()['access_token']}"}
    odd = "иваН@пример.рф"
    with env.engine.begin() as conn:
        conn.execute(
            update(User.__table__).where(User.id == uid)
            .values(email_verified_at=datetime.now(UTC))
        )
        conn.execute(
            insert(AuditLog.__table__).values(
                id="pl-odd", organization_id=ORG_B, action="note.created", entity_type="note",
                entity_id="n-odd", reason=f"Wrote to {odd}", new_state={}, context={},
            )
        )

    assert env.client.post(
        f"{API}/account/delete", headers=headers,
        json={"current_password": "ivan-pass-123", "confirmation": "DELETE"},
    ).status_code == 200

    [row] = env.rows(AuditLog, AuditLog.id == "pl-odd")
    assert env.engine.dialect.name == "sqlite"  # this module runs on SQLite
    assert row["reason"] == f"Wrote to {odd}"  # left by the bounded request
    with env.session() as s:
        report = verify_subject_erased(s, user_id=uid, email=IVAN)
    assert not report.verified  # found by the operator's exhaustive scan, never passed
    assert ("audit_logs", "reason") in {(f.table, f.column) for f in report.content_findings}


# --------------------------------------------------------------------------- #
# A shared upper case decides nothing (reviews r5, R5-1)
# --------------------------------------------------------------------------- #
#: (holder, the upper-case spelling BOB invites). Each spelling is the upper case of the holder's
#: address AND of a distinct registrable twin (σσ/σς, ia/ıa, strasse/straße).
SHARED_UPPER = [
    ("σσ@privacy.example", "ΣΣ@privacy.example"),
    ("σς@privacy.example", "ΣΣ@privacy.example"),
    ("ia@privacy.example", "IA@privacy.example"),
    ("ıa@privacy.example", "IA@privacy.example"),
    ("straße@privacy.example", "STRASSE@privacy.example"),
    ("strasse@privacy.example", "STRASSE@privacy.example"),
]


@pytest.mark.parametrize(
    ("holder", "invited"), SHARED_UPPER, ids=[f"{h}-{i}" for h, i in SHARED_UPPER]
)
def test_a_shared_upper_case_is_flagged_and_never_taken(env: PrivacyEnv, capsys, holder, invited):
    """Review r5 cap 170, both twins: neither exports or deletes B's invitation to the shared
    upper-case spelling, neither erases B's audit value, and both verifications flag it."""
    r = env.client.post(
        f"{API}/auth/register",
        json={"email": holder, "full_name": "H", "password": "holder-pass-1",
              "organization_name": "H Org"},
    )
    assert r.status_code == 201, r.text
    uid, headers = r.json()["user"]["id"], {"Authorization": f"Bearer {r.json()['access_token']}"}
    stored = r.json()["user"]["email"]
    with env.engine.begin() as conn:
        conn.execute(
            update(User.__table__).where(User.id == uid)
            .values(email_verified_at=datetime.now(UTC))
        )
    sent = env.client.post(
        f"{API}/organizations/{ORG_B}/invitations", headers=env.auth(BOB),
        json={"email": invited, "role": "viewer"},
    )
    assert sent.status_code == 201, sent.text
    invitation_id = sent.json()["id"]
    [record] = [a for a in env.rows(AuditLog) if a["entity_id"] == invitation_id]

    export = env.client.get(f"{API}/account/export", headers=headers).json()
    deleted = env.client.post(
        f"{API}/account/delete", headers=headers,
        json={"current_password": "holder-pass-1", "confirmation": "DELETE"},
    )

    assert [i["id"] for i in export["invitations_received"]] == []
    assert deleted.status_code == 200, deleted.text
    assert env.count(OrganizationInvitation, OrganizationInvitation.id == invitation_id) == 1
    assert env.rows(AuditLog, AuditLog.id == record["id"]) == [record]
    assert cli.main(["verify-account", "--user-id", uid, "--email", stored]) == cli.EXIT_REVIEW
    printed = json.loads(capsys.readouterr().out)
    assert {("organization_invitations", "email"), ("audit_logs", "new_state")} <= {
        (f["table"], f["column"]) for f in printed["content_ambiguous"]
    }


def test_a_percent_encoded_copy_without_ascii_is_erased_in_the_request(env: PrivacyEnv, capsys):
    """Review r5 R5-4 (mutant P4): the bounded in-request needles of an address without an ASCII
    local run include its percent-encoded form, so such a copy is erased by the deletion itself."""
    r = env.client.post(
        f"{API}/auth/register",
        json={"email": IVAN, "full_name": "I", "password": "ivan-pass-123",
              "organization_name": "I Org"},
    )
    uid, headers = r.json()["user"]["id"], {"Authorization": f"Bearer {r.json()['access_token']}"}
    encoded = quote(IVAN, safe="")
    with env.engine.begin() as conn:
        conn.execute(
            update(User.__table__).where(User.id == uid)
            .values(email_verified_at=datetime.now(UTC))
        )
        conn.execute(
            insert(AuditLog.__table__).values(
                id="pl-pct", organization_id=ORG_B, action="note.created", entity_type="note",
                entity_id="n-pct", reason=f"Contact {encoded}", new_state={"contact": encoded},
                context={},
            )
        )

    assert env.client.post(
        f"{API}/account/delete", headers=headers,
        json={"current_password": "ivan-pass-123", "confirmation": "DELETE"},
    ).status_code == 200

    [row] = env.rows(AuditLog, AuditLog.id == "pl-pct")
    assert (row["reason"], row["new_state"]) == (None, {"contact": ERASED})
    assert cli.main(["verify-account", "--user-id", uid, "--email", IVAN]) == cli.EXIT_OK
    capsys.readouterr()
