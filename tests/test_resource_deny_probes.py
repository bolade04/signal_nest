"""Resource-specific Deny probes and statement-level mutation (Gate 4N-I8, Defect 10).

THE DEFECT, confirmed by reproduction in Gate 4N-I7. Deleting `DenyAuditLogObjectDestruction`
— the only protection for delivered CloudTrail log objects — left the ceiling proof at
boundary 45/45 clean and 399/399 tests passing. The proof used roughly one probe ARN per
service, so a Deny scoped to a different resource vanished without moving anything.

A resource-scoped control is only proven at the resource it protects. These tests require
every protected resource to be named, and every Deny statement to be defended by something.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import gen_boundary_policy as gb  # noqa: E402
import resource_deny_probes as probes  # noqa: E402

REPORT = probes.run()
PROBE_ROWS = probes.run_probes()


@pytest.mark.parametrize("row", PROBE_ROWS,
                         ids=[f"{r['policy']}:{r['action']}:{r['protects'][:28]}"
                              for r in PROBE_ROWS])
def test_each_protected_resource_is_denied_inside_scope_and_free_outside(row):
    assert row["inside_ok"], (
        f"{row['action']} is {row['inside_decision']} on {row['protects']} "
        f"({row['inside']}) — the protection is absent")
    assert row["outside_ok"], (
        f"{row['action']} is also denied OUTSIDE its scope ({row.get('outside')}), so the "
        "control is over-broad and would remove a legitimate function")


def test_every_deny_statement_is_defended_by_something():
    """A statement no test notices is a statement that can be deleted silently."""
    undefended = REPORT["undefended_statements"]
    assert not undefended, (
        "these Deny statements can be deleted with a green suite: "
        + ", ".join(f"{r['policy']}/{r['deleted_statement']}" for r in undefended))


def test_the_probe_set_is_large_enough_to_be_meaningful():
    assert REPORT["probe_count"] >= 20, REPORT["probe_count"]
    assert REPORT["deny_statements"] >= 20, REPORT["deny_statements"]


def test_deleting_the_audit_log_deny_is_caught():
    """THE regression. This exact deletion left Gate 4N-I7 entirely green."""
    policy = gb.boundary_policy()
    without = {**policy, "Statement": [s for s in policy["Statement"]
                                       if s.get("Sid") != "DenyAuditLogObjectDestruction"]}
    assert len(without["Statement"]) == len(policy["Statement"]) - 1, "statement not found"
    failing = [r for r in probes.run_probes({**probes._policies(), "boundary": without})
               if not r["ok"]]
    assert failing, "deleting DenyAuditLogObjectDestruction moved no probe"
    assert any("audit" in r["protects"].lower() or "CloudTrail" in r["protects"]
               for r in failing), [r["protects"] for r in failing]


@pytest.mark.parametrize("sid", [
    "DenyTerraformStateAccess", "DenyProtectedBucketAndLockAdministration",
    "DenyAuditLogObjectDestruction", "DenyPassRoleExceptReaderExecutionRole",
    "DenyRunTaskExceptTheReaderRevision",
])
def test_deleting_any_resource_scoped_boundary_deny_is_caught(sid):
    policy = gb.boundary_policy()
    without = {**policy, "Statement": [s for s in policy["Statement"] if s.get("Sid") != sid]}
    assert len(without["Statement"]) == len(policy["Statement"]) - 1, f"{sid} not found"
    failing = [r for r in probes.run_probes({**probes._policies(), "boundary": without})
               if not r["ok"]]
    assert failing, f"deleting {sid} moved no probe"


def test_the_probes_are_clean_against_the_current_policies():
    assert not REPORT["failing_probes"], REPORT["failing_probes"]
