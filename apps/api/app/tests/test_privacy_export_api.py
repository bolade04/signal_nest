"""P6-PRIV-1: data export — portability for an account and an organization (SQLite).

    GET /api/v1/account/export                    Bearer         -> 200
    GET /api/v1/organizations/{id}/export         exact OWNER    -> 200

* **Account.** CAROL's export holds her profile, her membership, both invitations addressed to
  her (one in another letter case, in another organization), the feedback she submitted and
  the audit event she performed; no password hash, no token digest, and no other account's
  email address.
* **Organization.** ALICE's export of A holds every table the inventory marks exported, every
  row scoped to A or one of its workspaces, its three members with email and name; no row of
  B or C, no token digest, no derived vector; a per-table cap reports truncation.
* **Authority.** Organization export is exactly OWNER: ADMIN, VIEWER and a non-member are
  refused; anonymous is 401 for both.
* **Read-only.** Neither export writes anything (the whole database is compared).
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest

from app.privacy import export as export_module
from app.privacy.inventory import TABLES
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
    WORKSPACES,
    PrivacyEnv,
    privacy_world,
)

ACCOUNT = f"{API}/account/export"
ORG_EXPORT = f"{API}/organizations/{ORG_A}/export"
DIGESTS = [c * 64 for c in "12345"]


@pytest.fixture
def env(tmp_path) -> Iterator[PrivacyEnv]:
    with privacy_world(tmp_path / "export.db") as e:
        yield e


def test_account_export_holds_the_accounts_own_data_only(env: PrivacyEnv):
    headers = env.auth(CAROL)
    before = env.snapshot()

    r = env.client.get(ACCOUNT, headers=headers)

    assert r.status_code == 200, r.text
    assert env.snapshot() == before
    body = r.json()
    assert body["schema_version"] == 1
    assert body["subject"] == {"type": "account", "user_id": CAROL, "organization_id": None}
    assert body["profile"]["email"] == EMAIL[CAROL]
    assert [m["organization_id"] for m in body["memberships"]] == [ORG_A]
    assert {i["id"] for i in body["invitations_received"]} == {"prv-inv-carol", "prv-inv-b-carol"}
    assert body["invitations_sent"] == []
    assert [f["id"] for f in body["feedback_submitted"]] == ["a1-feedback"]
    assert [o["id"] for o in body["capability_overrides_set"]] == ["a1-override"]
    assert [a["id"] for a in body["audit_events_performed"]] == ["prv-audit-2"]
    assert len(body["sessions"]) == 1
    assert len(body["password_reset_requests"]) == 1
    assert body["email_verification_requests"][0]["email_snapshot"] == EMAIL[CAROL]
    text = json.dumps(body)
    assert "hashed_password" not in text and "token_hash" not in text
    for digest in DIGESTS:
        assert digest not in text
    for other in (ALICE, BOB, DAVE):
        assert EMAIL[other] not in text


def test_account_export_lists_invitations_sent_without_the_invitee_address(env: PrivacyEnv):
    body = env.client.get(ACCOUNT, headers=env.auth(ALICE)).json()
    assert {i["id"] for i in body["invitations_sent"]} == {"prv-inv-carol", "prv-inv-invitee"}
    assert all("email" not in i for i in body["invitations_sent"])


def test_organization_export_is_complete_and_scoped(env: PrivacyEnv):
    headers = env.auth(ALICE)
    before = env.snapshot()

    r = env.client.get(ORG_EXPORT, headers=headers)

    assert r.status_code == 200, r.text
    assert env.snapshot() == before
    body = r.json()
    assert body["subject"]["organization_id"] == ORG_A
    assert set(body["tables"]) == {n for n, e in TABLES.items() if e.exported}
    assert set(body["not_exported"]) == {n for n, e in TABLES.items() if not e.exported}
    workspaces = set(WORKSPACES[ORG_A])
    for name, table in body["tables"].items():
        assert table["truncated"] is False
        assert table["row_count"] == len(table["rows"])
        for row in table["rows"]:
            if name == "organizations":
                assert row["id"] == ORG_A
            elif "organization_id" in row:
                assert row["organization_id"] == ORG_A, (name, row)
            else:
                assert row["workspace_id"] in workspaces, (name, row)
            for column in TABLES[name].excluded_columns:
                assert column not in row
    assert body["tables"]["raw_signals"]["row_count"] == 2
    assert body["tables"]["audit_logs"]["row_count"] == 6
    assert {m["user_id"] for m in body["members"]} == {ALICE, CAROL, DAVE}
    assert {m["email"] for m in body["members"]} == {EMAIL[ALICE], EMAIL[CAROL], EMAIL[DAVE]}
    text = json.dumps(body)
    for foreign in (ORG_B, ORG_C, *WORKSPACES[ORG_B], *WORKSPACES[ORG_C], EMAIL[BOB]):
        assert foreign not in text
    for digest in DIGESTS:
        assert digest not in text
    assert "hashed_password" not in text


def test_organization_export_reports_truncation(env: PrivacyEnv, monkeypatch):
    monkeypatch.setattr(export_module, "ORGANIZATION_EXPORT_ROW_CAP", 1)
    body = env.client.get(ORG_EXPORT, headers=env.auth(ALICE)).json()
    brands = body["tables"]["brands"]
    assert (brands["row_count"], len(brands["rows"]), brands["truncated"]) == (1, 1, True)
    assert body["row_cap_per_table"] == 1


@pytest.mark.parametrize(
    ("user_id", "message"),
    [
        (DAVE, "Role 'admin' is not permitted for this action."),
        (CAROL, "Role 'viewer' is not permitted for this action."),
        (BOB, "You are not a member of this organization."),
    ],
)
def test_organization_export_is_owner_only(env: PrivacyEnv, user_id, message):
    r = env.client.get(ORG_EXPORT, headers=env.auth(user_id))
    assert (r.status_code, r.json()["error"]["message"]) == (403, message)


def test_anonymous_is_401(env: PrivacyEnv):
    assert env.client.get(ACCOUNT).status_code == 401
    assert env.client.get(ORG_EXPORT).status_code == 401
