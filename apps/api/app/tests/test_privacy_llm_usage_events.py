"""P6-PRIV-1 x P6-LLM-4: ``llm_usage_events`` under the operator decisions of 2026-10-27
(D-9a/D-9b/D-9c, packet D9-DECISIONS-01), through the real export and deletion routes.

The table is tenant-linked usage metadata (organization and workspace ids, the request and
job ids of the call, task, provider, model, token counts, cost, outcome, timestamps; no prompt
text, no content, no user key). The ruling, and what each test pins:

* **D-9a/b — organization export.** The OWNER's export of A holds the table with exactly A's
  rows and every recorded column (the column set equals the model's), none of B's or C's.
  An OWNER of B sees B's rows only; A's never reach another organization's export.
* **D-9b — no attribution to an account.** The account export of a member of A carries no
  usage row and no usage column: the table has no user key, so there is no supported
  relationship between an account and the organization's usage.
* **D-9c — parent lifetime.** Deleting A (its OWNER, through the route) removes A's rows and
  leaves B's and C's byte-identical (ids and values). Deleting a member's account leaves
  every row untouched, including the rows whose ``scout_request_id``/``job_id`` point at the
  request and job of the workspace that member's activity produced: neither deleted nor set
  null. When an account deletion removes a sole-member organization, that organization's rows
  go with it and the others stay byte-identical.
* **The inventory entry** is pinned to the ruling, and the human inventory words the table as
  tenant-linked usage metadata, never as non-personal or anonymous.

The world is ``_privacy_support``'s, plus one usage row per seeded workspace and one row of A
written off the worker path (no request, no job).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import update

from app.llm.models import LLMUsageEvent
from app.organizations.models import OrganizationMember
from app.privacy.inventory import TABLES, Category, TableEntry
from app.tests._privacy_support import (
    ALICE,
    API,
    BOB,
    CAROL,
    DAVE,
    NAME,
    ORG_A,
    ORG_B,
    ORG_C,
    PASSWORD,
    WS_A,
    WS_A2,
    WS_B,
    WS_C,
    PrivacyEnv,
    privacy_world,
)

TABLE = "llm_usage_events"
DOC = Path(__file__).resolve().parents[4] / "docs" / "operations" / "data-inventory.md"
COLUMNS = frozenset(LLMUsageEvent.__table__.columns.keys())

ACCOUNT_EXPORT = f"{API}/account/export"
ACCOUNT_DELETE = f"{API}/account/delete"
ACCOUNT_BODY = {"current_password": PASSWORD, "confirmation": "DELETE"}


def _org_export(org: str) -> str:
    return f"{API}/organizations/{org}/export"


def _org_delete(org: str) -> str:
    return f"{API}/organizations/{org}/delete"


def _org_body(org: str) -> dict[str, str]:
    return {"current_password": PASSWORD, "confirm_organization_name": NAME[org]}


#: (id, organization, workspace, scout request, job, task, outcome, cost)
_ROWS = (
    # a1 is the workspace content CAROL's activity produced (actor=CAROL in the seed).
    ("a1-usage-1", ORG_A, WS_A, "a1-scout", "a1-job", "signal.classify", "ok", 0.0031),
    ("a1-usage-2", ORG_A, WS_A, None, None, "opportunity.explain", "RateLimited", 0.0),
    ("a2-usage-1", ORG_A, WS_A2, "a2-scout", "a2-job", "signal.classify", "ok", 0.0017),
    ("b1-usage-1", ORG_B, WS_B, "b1-scout", "b1-job", "signal.classify", "ok", 0.0042),
    ("c1-usage-1", ORG_C, WS_C, "c1-scout", "c1-job", "signal.classify", "ok", 0.0008),
)
A_IDS = {"a1-usage-1", "a1-usage-2", "a2-usage-1"}
B_IDS = {"b1-usage-1"}
C_IDS = {"c1-usage-1"}
ALL_IDS = A_IDS | B_IDS | C_IDS


@pytest.fixture
def env(tmp_path) -> Iterator[PrivacyEnv]:
    with privacy_world(tmp_path / "usage.db") as e:
        with e.session() as s:
            for row_id, org, ws, scout, job, task, outcome, cost in _ROWS:
                s.add(
                    LLMUsageEvent(
                        id=row_id, organization_id=org, workspace_id=ws,
                        scout_request_id=scout, job_id=job, task=task, provider="mock",
                        model="mock-model", input_tokens=120, output_tokens=40, cost_usd=cost,
                        outcome=outcome, created_at=e.seeded_at,
                    )
                )
            s.commit()
        assert {r["id"] for r in e.rows(LLMUsageEvent)} == ALL_IDS
        yield e


def _rows_by_id(env: PrivacyEnv) -> dict[str, dict]:
    return {r["id"]: r for r in env.rows(LLMUsageEvent)}


def _usage_ids_in(text: str) -> set[str]:
    return {row_id for row_id in ALL_IDS if row_id in text}


# --------------------------------------------------------------------------- #
# The ruling, as recorded
# --------------------------------------------------------------------------- #
def test_the_inventory_entry_records_the_ruling():
    assert TABLES[TABLE] == TableEntry(
        TABLE, Category.OPERATIONAL, exported=True, excluded_columns=(), retention_class=None
    )


def test_the_document_words_the_table_as_tenant_linked_usage_metadata():
    text = DOC.read_text(encoding="utf-8")
    row = next(line for line in text.splitlines() if line.startswith(f"| `{TABLE}` |"))
    assert "tenant-linked usage metadata" in row
    assert "kept for the life of its workspace for usage accounting and reconciliation" in row
    for column in sorted(COLUMNS - {"id", "created_at", "updated_at"}):
        assert f"`{column}`" in row, column
    for forbidden in ("non-personal", "nonpersonal", "anonymous", "anonymised data"):
        assert forbidden not in row.lower(), forbidden


# --------------------------------------------------------------------------- #
# D-9a/b: the organization export
# --------------------------------------------------------------------------- #
def test_organization_export_holds_exactly_the_organizations_rows_with_every_column(
    env: PrivacyEnv,
):
    stored = _rows_by_id(env)

    r = env.client.get(_org_export(ORG_A), headers=env.auth(ALICE))

    assert r.status_code == 200, r.text
    body = r.json()
    assert TABLE in body["tables"] and TABLE not in body["not_exported"]
    table = body["tables"][TABLE]
    assert (table["row_count"], table["truncated"]) == (len(A_IDS), False)
    assert {row["id"] for row in table["rows"]} == A_IDS
    for row in table["rows"]:
        assert set(row) == COLUMNS, sorted(set(row) ^ COLUMNS)
        assert row["organization_id"] == ORG_A
        kept = stored[row["id"]]
        for column in COLUMNS - {"created_at", "updated_at"}:
            assert row[column] == kept[column], (row["id"], column)
        assert row["created_at"] and row["updated_at"]
    # The off-worker row (no request, no job) is exported as recorded, nulls included.
    off_worker = next(row for row in table["rows"] if row["id"] == "a1-usage-2")
    assert (off_worker["scout_request_id"], off_worker["job_id"]) == (None, None)
    assert _usage_ids_in(json.dumps(body)) == A_IDS


def test_another_organizations_owner_never_sees_the_rows(env: PrivacyEnv):
    """Cross-organization: BOB's export of B holds B's row only; A's export is refused to him."""
    headers = env.auth(BOB)

    own = env.client.get(_org_export(ORG_B), headers=headers)

    assert own.status_code == 200, own.text
    assert {row["id"] for row in own.json()["tables"][TABLE]["rows"]} == B_IDS
    assert _usage_ids_in(json.dumps(own.json())) == B_IDS
    foreign = env.client.get(_org_export(ORG_A), headers=headers)
    assert foreign.status_code == 403, foreign.text
    assert _usage_ids_in(foreign.text) == set()


# --------------------------------------------------------------------------- #
# D-9b: no attribution to an individual account
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("user_id", [CAROL, ALICE, DAVE])
def test_account_export_carries_no_usage_rows(env: PrivacyEnv, user_id):
    r = env.client.get(ACCOUNT_EXPORT, headers=env.auth(user_id))

    assert r.status_code == 200, r.text
    text = json.dumps(r.json())
    assert TABLE not in text and "llm_usage" not in text
    assert _usage_ids_in(text) == set()
    for column in ("cost_usd", "input_tokens", "output_tokens", "provider", "model"):
        assert f'"{column}"' not in text, column


# --------------------------------------------------------------------------- #
# D-9c: parent lifetime
# --------------------------------------------------------------------------- #
def test_deleting_the_organization_removes_its_rows_and_leaves_the_others_byte_identical(
    env: PrivacyEnv,
):
    before = _rows_by_id(env)
    others_before = {k: v for k, v in before.items() if k not in A_IDS}

    r = env.client.post(_org_delete(ORG_A), headers=env.auth(ALICE), json=_org_body(ORG_A))

    assert r.status_code == 200, r.text
    receipt = r.json()
    assert receipt["counts"]["deleted"][TABLE] == len(A_IDS)
    assert receipt["verification"]["references_cleared"] is True
    after = _rows_by_id(env)
    assert set(after) == B_IDS | C_IDS
    assert after == others_before  # every column of every surviving row, unchanged
    assert env.count(LLMUsageEvent, LLMUsageEvent.organization_id == ORG_A) == 0
    assert env.count(LLMUsageEvent, LLMUsageEvent.workspace_id.in_((WS_A, WS_A2))) == 0


def test_account_deletion_of_a_member_leaves_every_row_untouched(env: PrivacyEnv):
    """CAROL (VIEWER of A) deletes her account: the rows survive, and ``a1-usage-1`` keeps its
    ``scout_request_id``/``job_id`` (the request and job her workspace activity produced are
    the workspace's, not hers): neither deleted nor set null."""
    before = _rows_by_id(env)
    assert (before["a1-usage-1"]["scout_request_id"], before["a1-usage-1"]["job_id"]) == (
        "a1-scout", "a1-job",
    )

    r = env.client.post(ACCOUNT_DELETE, headers=env.auth(CAROL), json=ACCOUNT_BODY)

    assert r.status_code == 200, r.text
    receipt = r.json()
    assert receipt["verification"]["references_cleared"] is True
    assert TABLE not in receipt["counts"]["deleted"]
    assert not any(key.startswith(f"{TABLE}.") for key in receipt["counts"]["nulled"])
    after = _rows_by_id(env)
    assert after == before
    assert (after["a1-usage-1"]["scout_request_id"], after["a1-usage-1"]["job_id"]) == (
        "a1-scout", "a1-job",
    )


def test_account_deletion_that_removes_a_sole_member_organization_takes_its_rows_only(
    env: PrivacyEnv,
):
    """ALICE hands A over, then deletes her account: C (her sole-member organization) goes,
    and C's usage row with it; A's and B's rows are byte-identical."""
    with env.engine.begin() as conn:
        conn.execute(
            update(OrganizationMember.__table__)
            .where(OrganizationMember.user_id == DAVE)
            .values(role="owner")
        )
    before = _rows_by_id(env)
    kept_before = {k: v for k, v in before.items() if k not in C_IDS}

    r = env.client.post(ACCOUNT_DELETE, headers=env.auth(ALICE), json=ACCOUNT_BODY)

    assert r.status_code == 200, r.text
    receipt = r.json()
    [deleted] = receipt["deleted_organizations"]
    assert deleted["organization_id"] == ORG_C
    assert receipt["counts"]["deleted"][TABLE] == len(C_IDS)  # merged from C's erasure
    after = _rows_by_id(env)
    assert set(after) == A_IDS | B_IDS
    assert after == kept_before
