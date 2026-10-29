"""P6-PRIV-1 x D-LLM-1 option B: ``llm_budget_periods`` and ``llm_budget_reservations``.

Ruled by the coordinator on 2026-10-27T19:27Z: both tables are OPERATIONAL, tenant-scoped
in the organization export, parent-lifetime, deleted with the workspace / organization.
What each test pins:

* the inventory entries, and that the human inventory lists **every column** of both tables;
* the organization export holds exactly the organization's rows with every column, never
  another organization's; the account export carries none (no user key);
* organization deletion (through the route) removes the organization's rows and leaves the
  others byte-identical;
* **late callbacks after the parent was deleted** - a settlement, a release, the expiry
  sweep, the terminal sweep, a fresh reservation and a same-session record, arriving after
  the organization (route) or a workspace (the #246 erasure walk) was erased - change
  nothing: the whole database is byte-identical before and after, no period row, no
  reservation row, no usage row is recreated, and the late settlement is counted
  (``llm_settlements_orphaned_total``);
* a settlement that is in flight at the instant its tenant is erased (the erasure commits
  between the settlement's read and its update) records nothing and raises nothing.

Runs on SQLite (foreign keys on, as in production) and, with ``TEST_POSTGRES_URL``, on
PostgreSQL (the production dialect, where ``INSERT ... ON CONFLICT`` is what could resurrect
a period row).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import event

from app.core.metrics import LLM_SETTLEMENTS_ORPHANED_TOTAL, InMemoryMetrics, configure_metrics
from app.llm import budget as ledger
from app.llm.models import LLMBudgetPeriod, LLMBudgetReservation, LLMUsageEvent
from app.organizations.models import Workspace
from app.privacy.erasure import delete_rows
from app.privacy.inventory import TABLES, Category, TableEntry
from app.tests._auth2_support import require_postgres
from app.tests._privacy_support import (
    ALICE,
    API,
    BOB,
    CAROL,
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

PERIODS, RESERVATIONS = "llm_budget_periods", "llm_budget_reservations"
DOC = Path(__file__).resolve().parents[4] / "docs" / "operations" / "data-inventory.md"
PERIOD_COLUMNS = frozenset(LLMBudgetPeriod.__table__.columns.keys())
RESERVATION_COLUMNS = frozenset(LLMBudgetReservation.__table__.columns.keys())
ACCOUNT_EXPORT = f"{API}/account/export"
MONTH = datetime.now(UTC).date().replace(day=1)
W = Decimal("0.021261")
ACTUAL = Decimal("0.010500")

#: (workspace, organization, tag) for every seeded ledger scope.
SCOPES = (
    (WS_A, ORG_A, "a1"),
    (WS_A2, ORG_A, "a2"),
    (WS_B, ORG_B, "b1"),
    (WS_C, ORG_C, "c1"),
)
A_PERIODS = {"a1-period", "a2-period"}
A_RESERVATIONS = {"a1-res-reserved", "a1-res-settled", "a2-res-reserved", "a2-res-settled"}
OTHER_PERIODS = {"b1-period", "c1-period"}
OTHER_RESERVATIONS = {"b1-res-reserved", "b1-res-settled", "c1-res-reserved", "c1-res-settled"}


@pytest.fixture(params=["sqlite", "postgres"])
def env(request, tmp_path) -> Iterator[PrivacyEnv]:
    if request.param == "postgres":
        require_postgres()
        world = privacy_world(None)
    else:
        world = privacy_world(tmp_path / "ledger.db")
    with world as e:
        with e.session() as s:
            for ws, org, tag in SCOPES:
                s.add(
                    LLMBudgetPeriod(
                        id=f"{tag}-period",
                        organization_id=org,
                        workspace_id=ws,
                        period_start=MONTH,
                        reserved_usd=2 * W,
                        settled_usd=W + ACTUAL,
                        bound_violations=0,
                        created_at=e.seeded_at,
                    )
                )
                s.flush()
                for state, settled, key in (("reserved", None, "r"), ("settled", ACTUAL, "s")):
                    s.add(
                        LLMBudgetReservation(
                            id=f"{tag}-res-{state}",
                            organization_id=org,
                            workspace_id=ws,
                            period_start=MONTH,
                            job_id=None,
                            attempt_key=f"{tag}:1:{key}",
                            reserved_usd=W,
                            settled_usd=settled,
                            state=state,
                            # a2's in-flight row is overdue (the sweep would expire it if its
                            # scope still existed); every other scope's is not.
                            expires_at=datetime.now(UTC)
                            + (timedelta(hours=-1) if tag == "a2" else timedelta(days=1)),
                            settled_at=e.seeded_at if settled is not None else None,
                            usage_recorded_at=e.seeded_at if settled is not None else None,
                            created_at=e.seeded_at,
                        )
                    )
            s.commit()
        yield e


def _by_id(env: PrivacyEnv, model) -> dict[str, dict]:
    return {r["id"]: r for r in env.rows(model)}


def _org_export(org: str) -> str:
    return f"{API}/organizations/{org}/export"


def _org_delete(org: str) -> str:
    return f"{API}/organizations/{org}/delete"


def _org_body(org: str) -> dict[str, str]:
    return {"current_password": PASSWORD, "confirm_organization_name": NAME[org]}


# --------------------------------------------------------------------------- #
# The ruling, as recorded: inventory entries and every column in the document
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("table", [PERIODS, RESERVATIONS])
def test_the_inventory_entries_record_the_ruling(table):
    assert TABLES[table] == TableEntry(
        table, Category.OPERATIONAL, exported=True, excluded_columns=(), retention_class=None
    )


@pytest.mark.parametrize(
    ("table", "columns"), [(PERIODS, PERIOD_COLUMNS), (RESERVATIONS, RESERVATION_COLUMNS)]
)
def test_the_document_lists_every_column_of_the_table(table, columns):
    text = DOC.read_text(encoding="utf-8")
    row = next(line for line in text.splitlines() if line.startswith(f"| `{table}` |"))
    assert "tenant-linked usage metadata" in row
    for column in sorted(columns - {"id", "created_at", "updated_at"}):
        assert f"`{column}`" in row, (table, column)
    assert "timestamps" in row  # created_at / updated_at
    for forbidden in ("non-personal", "nonpersonal", "anonymous", "anonymised data"):
        assert forbidden not in row.lower(), forbidden


# --------------------------------------------------------------------------- #
# Organization export and the account export
# --------------------------------------------------------------------------- #
def test_the_organization_export_holds_exactly_its_rows_with_every_column(env: PrivacyEnv):
    stored = {
        PERIODS: _by_id(env, LLMBudgetPeriod),
        RESERVATIONS: _by_id(env, LLMBudgetReservation),
    }

    r = env.client.get(_org_export(ORG_A), headers=env.auth(ALICE))

    assert r.status_code == 200, r.text
    body = r.json()
    for table, columns, ids in (
        (PERIODS, PERIOD_COLUMNS, A_PERIODS),
        (RESERVATIONS, RESERVATION_COLUMNS, A_RESERVATIONS),
    ):
        assert table in body["tables"] and table not in body["not_exported"]
        exported = body["tables"][table]
        assert (exported["row_count"], exported["next_cursor"]) == (len(ids), None)
        assert {row["id"] for row in exported["rows"]} == ids
        for row in exported["rows"]:
            assert set(row) == columns, (table, sorted(set(row) ^ columns))
            assert row["organization_id"] == ORG_A
            kept = stored[table][row["id"]]
            for column in columns - {
                "created_at",
                "updated_at",
                "expires_at",
                "settled_at",
                "usage_recorded_at",
                "reserved_usd",
                "settled_usd",
            }:
                assert row[column] == kept[column] or str(row[column]) == str(kept[column]), column
    text = json.dumps(body)
    assert not any(i in text for i in OTHER_PERIODS | OTHER_RESERVATIONS)


def test_another_organizations_owner_never_sees_the_rows(env: PrivacyEnv):
    own = env.client.get(_org_export(ORG_B), headers=env.auth(BOB))
    assert own.status_code == 200, own.text
    assert {r["id"] for r in own.json()["tables"][PERIODS]["rows"]} == {"b1-period"}
    foreign = env.client.get(_org_export(ORG_A), headers=env.auth(BOB))
    assert foreign.status_code == 403
    assert not any(i in foreign.text for i in A_PERIODS | A_RESERVATIONS)


def test_the_account_export_carries_no_ledger_row(env: PrivacyEnv):
    r = env.client.get(ACCOUNT_EXPORT, headers=env.auth(CAROL))
    assert r.status_code == 200, r.text
    text = json.dumps(r.json())
    assert PERIODS not in text and RESERVATIONS not in text and "attempt_key" not in text
    assert not any(i in text for i in A_PERIODS | A_RESERVATIONS)


# --------------------------------------------------------------------------- #
# Parent lifetime: deletion with the organization
# --------------------------------------------------------------------------- #
def test_deleting_the_organization_removes_its_rows_and_leaves_the_others_identical(
    env: PrivacyEnv,
):
    periods, reservations = _by_id(env, LLMBudgetPeriod), _by_id(env, LLMBudgetReservation)

    r = env.client.post(_org_delete(ORG_A), headers=env.auth(ALICE), json=_org_body(ORG_A))

    assert r.status_code == 200, r.text
    receipt = r.json()
    assert receipt["counts"]["deleted"][PERIODS] == len(A_PERIODS)
    assert receipt["counts"]["deleted"][RESERVATIONS] == len(A_RESERVATIONS)
    assert receipt["verification"]["references_cleared"] is True
    assert _by_id(env, LLMBudgetPeriod) == {k: periods[k] for k in OTHER_PERIODS}
    assert _by_id(env, LLMBudgetReservation) == {k: reservations[k] for k in OTHER_RESERVATIONS}


# --------------------------------------------------------------------------- #
# Late callbacks after the parent was deleted
# --------------------------------------------------------------------------- #
def _late_callbacks(env: PrivacyEnv, workspace: str, organization: str, *, tag: str) -> None:
    """Every late path, against a scope that no longer exists."""
    now = datetime.now(UTC)
    usage = {
        "scout_request_id": None,
        "task": "classify_signal",
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
        "input_tokens": 1000,
        "output_tokens": 500,
        "cost_usd": float(ACTUAL),
        "outcome": "ok",
    }
    with env.session() as db:
        # a settlement, for the reservation that was still in flight, and for a settled one
        assert not ledger.settle(
            db, attempt_key=f"{tag}:1:r", settled_usd=ACTUAL, now=now, usage=usage
        )
        assert not ledger.settle(
            db, attempt_key=f"{tag}:1:s", settled_usd=ACTUAL, now=now, usage=usage
        )
        # recovery of every kind
        assert ledger.release_for_job(db, job_id="gone-job", now=now, outcome="terminal") == 0
        assert ledger.release_expired(db, now=now) == 0
        assert ledger.release_terminal(db, now=now) == 0
        assert ledger.sweep(db, now=now) == {"expired": 0, "terminal": 0}
        # the writers that insert
        assert not ledger.reserve(
            db,
            workspace_id=workspace,
            organization_id=organization,
            job_id=None,
            attempt_key=f"{tag}:2:0",
            worst_usd=W,
            budget_usd=Decimal("5"),
            expires_at=now + timedelta(hours=1),
            now=now,
        )
        assert not ledger.record_settled(
            db,
            workspace_id=workspace,
            organization_id=organization,
            job_id=None,
            attempt_key=f"{tag}:3:0",
            settled_usd=ACTUAL,
            now=now,
            usage=usage,
        )
        db.commit()


@pytest.mark.parametrize("parent", ["organization", "workspace"])
def test_late_callbacks_after_the_parent_was_deleted_recreate_nothing(env: PrivacyEnv, parent):
    # Erase the parent: the organization through the route, or one workspace through the
    # #246 erasure walk (the same primitive the organization walk uses).
    if parent == "organization":
        r = env.client.post(_org_delete(ORG_A), headers=env.auth(ALICE), json=_org_body(ORG_A))
        assert r.status_code == 200, r.text
        gone = ((WS_A, ORG_A, "a1"), (WS_A2, ORG_A, "a2"))
    else:
        with env.session() as db:
            delete_rows(db, Workspace.__table__, [WS_A2])
            db.commit()
        gone = ((WS_A2, ORG_A, "a2"),)
    assert env.count(LLMBudgetPeriod, LLMBudgetPeriod.workspace_id == gone[0][0]) == 0
    before = env.snapshot()
    metrics = InMemoryMetrics()
    configure_metrics(metrics)
    try:
        for workspace, organization, tag in gone:
            _late_callbacks(env, workspace, organization, tag=tag)
    finally:
        configure_metrics(None)
    assert env.snapshot() == before  # the whole database: nothing recreated, nothing changed
    for workspace, _org, _tag in gone:
        assert env.count(LLMBudgetPeriod, LLMBudgetPeriod.workspace_id == workspace) == 0
        assert env.count(LLMBudgetReservation, LLMBudgetReservation.workspace_id == workspace) == 0
        assert env.count(LLMUsageEvent, LLMUsageEvent.workspace_id == workspace) == 0
    assert metrics.counter_value(LLM_SETTLEMENTS_ORPHANED_TOTAL, outcome="settle") == 2 * len(gone)
    assert metrics.counter_value(LLM_SETTLEMENTS_ORPHANED_TOTAL, outcome="reserve") == len(gone)
    assert metrics.counter_value(LLM_SETTLEMENTS_ORPHANED_TOTAL, outcome="record_settled") == len(
        gone
    )


def test_a_settlement_in_flight_when_its_tenant_is_erased_records_nothing(env: PrivacyEnv):
    """The erasure commits between the settlement's read of the reservation and its lock /
    update: the update matches nothing, the re-read finds no row, and nothing is written or
    raised."""
    usage = {
        "scout_request_id": None,
        "task": "classify_signal",
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
        "input_tokens": 1,
        "output_tokens": 1,
        "cost_usd": float(ACTUAL),
        "outcome": "ok",
    }
    fired: list[int] = []

    @event.listens_for(env.engine, "before_cursor_execute")
    def erase_first(conn, cursor, statement, parameters, context, executemany):
        # Before the settlement's period lock (PostgreSQL) or its claim update (SQLite has
        # no row locks): the erasure cannot be issued while this thread holds the lock.
        text = statement.lstrip().upper()
        if (
            text.startswith("UPDATE LLM_BUDGET_RESERVATIONS") or "FOR UPDATE" in text
        ) and not fired:
            fired.append(1)
            with env.witness.begin() as other:  # a separate connection commits the erasure
                from sqlalchemy.orm import Session

                session = Session(bind=other)
                delete_rows(session, Workspace.__table__, [WS_A2])
                session.flush()

    metrics = InMemoryMetrics()
    configure_metrics(metrics)
    try:
        with env.session() as db:
            assert not ledger.settle(
                db, attempt_key="a2:1:r", settled_usd=ACTUAL, now=datetime.now(UTC), usage=usage
            )
            db.commit()
    finally:
        configure_metrics(None)
        event.remove(env.engine, "before_cursor_execute", erase_first)
    assert fired == [1]
    assert env.count(LLMBudgetReservation, LLMBudgetReservation.workspace_id == WS_A2) == 0
    assert env.count(LLMUsageEvent, LLMUsageEvent.workspace_id == WS_A2) == 0
    assert env.count(LLMBudgetPeriod, LLMBudgetPeriod.workspace_id == WS_A2) == 0
    assert metrics.counter_value(LLM_SETTLEMENTS_ORPHANED_TOTAL, outcome="settle") == 1
    # the other scopes are untouched
    assert {r for r in _by_id(env, LLMBudgetPeriod)} == (A_PERIODS - {"a2-period"}) | OTHER_PERIODS
