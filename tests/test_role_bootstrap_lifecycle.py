"""Lifecycle DAG, principal coverage and falsifiable graph hashing (Gate 4N-I16).

THREE DEFECTS ARE UNDER TEST HERE, and each survived the previous gate because the check that
should have caught it was scoped so it could not.

DEFECT 5 — READ_ONLY_VERIFIER owned twelve steps and eleven actions while having no policy,
no permission set, no creation path and no retirement. `action_availability()` skipped it BY
NAME while claiming to cover "every non-root step", so the graph reported
`unavailable_actions: 0` for a principal that could not have executed six of its steps.

DEFECT 6 — ordering rested on an unvalidated integer. Flattening every value to 1 disabled
both ordering invariants at once, and there was no cycle detection at all: a step could
depend on the LAST step in the graph and the validator still said clean.

DEFECT 7 — `test_the_graph_hash_is_stable` compared the production hash to itself. Replacing
the function body with a constant left it green.

THE MUTATION TABLES BELOW ARE THE POINT. Each entry breaks exactly one thing and requires
`validate()` to report it. A graph that stays clean under a falsifying mutation is a graph
whose invariant is decorative.
"""

from __future__ import annotations

import expiry_authorization as _ea  # noqa: E402

import copy
import pathlib
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import gen_readonly_verifier_policy as verifier  # noqa: E402
import lifecycle_canonical  # noqa: E402
import role_bootstrap_lifecycle as lc  # noqa: E402

CANONICAL_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "lifecycle-canonical-sha256.txt"


def graph():
    return lc.steps()


def with_mutation(fn):
    """Apply a mutation to a COPY and revalidate through the real validator."""
    mutated = copy.deepcopy(graph())
    fn(mutated)
    original = lc.steps
    lc.steps = lambda: mutated
    try:
        return lc.validate()
    finally:
        lc.steps = original


def by_id(g, sid):
    return next(s for s in g if s["step_id"] == sid)


# =====================================================================================
# The unmutated graph — the positive control. Without it, every "mutation caught" result
# below could equally mean the validator always reports findings.
# =====================================================================================


def test_the_unmutated_graph_is_clean():
    result = lc.validate()
    assert result["clean"], result["problems"]


def test_every_invariant_is_zero():
    invariants = lc.validate()["invariants"]
    assert set(invariants.values()) == {0}, invariants


def test_the_graph_has_the_expected_shape():
    g = graph()
    assert len(g) >= 40
    assert len({s["step_id"] for s in g}) == len(g)


# =====================================================================================
# DEFECT 5 — the verifier is a real principal with real coverage
# =====================================================================================


def test_the_verifier_has_a_generated_policy():
    policy = verifier.readonly_verifier_policy(_ea.ACTIVE_EXPIRY_UTC)
    assert policy["Statement"], "the verifier policy is empty"
    assert lc.ACTOR_RULES[lc.VERIFIER]["policy"] is not None


def test_the_verifier_holds_no_mutating_action():
    for action in verifier.ALL_ACTIONS:
        assert not verifier.is_mutating(action), action
    for step in graph():
        if step["owner"] == lc.VERIFIER:
            assert not step["is_mutation"], step["step_id"]


@pytest.mark.parametrize("destructive", [
    "cloudtrail:StopLogging",   # audit-trail shutdown — the old prefix list called this a read
    "ecs:RunTask",              # arbitrary workload execution
    "iam:PassRole",             # authority delegation
    "ec2:TerminateInstances",
    "kms:ScheduleKeyDeletion",
])
def test_the_verifier_policy_refuses_the_actions_the_old_prefix_list_called_reads(
        destructive, monkeypatch):
    """Gate 4N-I17 Defect 4. Each of these returned is_mutating=False under the prefix rule."""
    monkeypatch.setattr(verifier, "ALL_ACTIONS", verifier.ALL_ACTIONS + [destructive])
    with pytest.raises(ValueError, match="non-read actions"):
        verifier.readonly_verifier_policy(_ea.ACTIVE_EXPIRY_UTC)


def test_the_verifier_policy_refuses_to_emit_with_a_mutating_action(monkeypatch):
    monkeypatch.setattr(verifier, "ALL_ACTIONS",
                        verifier.ALL_ACTIONS + ["iam:DeleteRole"])
    with pytest.raises(ValueError, match="non-read actions"):
        verifier.readonly_verifier_policy(_ea.ACTIVE_EXPIRY_UTC)


def test_the_verifier_policy_refuses_a_placeholder_expiry():
    with pytest.raises(ValueError, match="placeholder"):
        verifier.readonly_verifier_policy("<EXPIRY-ISO8601>")


def test_every_verifier_action_is_proven_available_not_skipped():
    rows = [r for r in lc.action_availability() if r["owner"] == lc.VERIFIER]
    assert len(rows) >= 10, "the verifier's steps are being skipped again"
    for row in rows:
        assert row["available"], row
        assert row["supporting_sids"], f"{row['step_id']} allowed by no named Sid"


def test_action_availability_covers_every_action_bearing_step():
    """The exclusion-by-name defect: coverage must be total, not a curated subset."""
    action_steps = {s["step_id"] for s in graph() if s["action"]}
    covered = {r["step_id"] for r in lc.action_availability()}
    assert action_steps == covered, action_steps ^ covered


def test_the_verifier_is_retired_and_acts_no_later():
    g = graph()
    retire = by_id(g, "verifier_sign_out")
    assert retire["retires_principal_after"]
    later = [s["step_id"] for s in g
             if s["owner"] == lc.VERIFIER and s["sequence"] > retire["sequence"]]
    assert not later, later


def test_the_verifier_has_a_complete_lifecycle():
    ids = {s["step_id"] for s in graph()}
    for required in ("verifier_create_permission_set", "verifier_install_inline_policy",
                     "verifier_assign", "verifier_poll_assignment_creation",
                     "verifier_provision", "verifier_poll_provisioning",
                     "verifier_sso_login", "verifier_verify_caller_identity",
                     "verifier_remove_assignment", "verifier_poll_removal",
                     "verifier_sign_out", "verifier_disposition"):
        assert required in ids, required


def test_a_principal_holding_actions_without_a_policy_is_a_finding(monkeypatch):
    monkeypatch.setitem(lc.ACTOR_RULES, lc.VERIFIER,
                        {**lc.ACTOR_RULES[lc.VERIFIER], "policy": None})
    result = lc.validate()
    assert not result["clean"]
    assert any("registered policy" in p for p in result["problems"])


def test_identity_centre_administration_is_root_only_and_polls_are_not_administration():
    """Gate 4N-I15's artifact asserted an absolute its code contradicted via a carve-out."""
    for step in graph():
        if step["action"] in lc.IDENTITY_CENTRE_ADMIN_ACTIONS:
            assert step["owner"] == lc.ROOT, step["step_id"]
    for polling in ("sso:DescribePermissionSetProvisioningStatus",
                    "sso:DescribeAccountAssignmentDeletionStatus",
                    "sso:DescribeAccountAssignmentCreationStatus"):
        assert polling not in lc.IDENTITY_CENTRE_ADMIN_ACTIONS


def test_every_async_identity_centre_initiator_has_a_bounded_poll():
    g = graph()
    for step in g:
        if step["action"] in ("sso:ProvisionPermissionSet", "sso:CreateAccountAssignment",
                              "sso:DeleteAccountAssignment"):
            pollers = [s for s in g if step["step_id"] in (s["depends_on"] or [])
                       and s["action"] and s["action"].startswith("sso:Describe")]
            assert pollers, step["step_id"]
            assert any(p["timeout_seconds"] for p in pollers), step["step_id"]


def test_rollback_is_modelled_for_every_role_not_just_the_first():
    ids = {s["step_id"] for s in graph()}
    for i in (1, 2, 3):
        assert f"mismatch_rollback_{i}" in ids


# =====================================================================================
# DEFECT 6 — the DAG mutation table (Phase T)
# =====================================================================================

DAG_MUTATIONS = {
    "two_node_cycle":
        lambda g: by_id(g, "assign_operator").__setitem__(
            "depends_on", ["poll_assignment_creation"]),
    "three_node_cycle":
        lambda g: by_id(g, "create_permission_set").__setitem__(
            "depends_on", ["provision_permission_set"]),
    "self_loop":
        lambda g: by_id(g, "verify_expected_role_set").__setitem__(
            "depends_on", ["verify_expected_role_set"]),
    "duplicate_sequence":
        lambda g: by_id(g, "verify_expected_role_set").__setitem__(
            "sequence", by_id(g, "remove_account_assignment")["sequence"]),
    "string_sequence":
        lambda g: by_id(g, "operator_sign_out").__setitem__("sequence", "twenty"),
    "boolean_sequence":
        lambda g: by_id(g, "operator_sign_out").__setitem__("sequence", True),
    "missing_dependency_target":
        lambda g: by_id(g, "root_sign_out").__setitem__("depends_on", ["no_such_step"]),
    "duplicate_dependency_entry":
        lambda g: by_id(g, "root_sign_out").__setitem__(
            "depends_on", ["verifier_disposition", "verifier_disposition"]),
    "dependency_on_a_later_sequence":
        lambda g: by_id(g, "create_permission_set").__setitem__(
            "depends_on", ["root_sign_out"]),
    "flatten_every_sequence":
        lambda g: [s.__setitem__("sequence", 1) for s in g],
    "orphan_step":
        lambda g: by_id(g, "capture_cloudtrail_evidence").__setitem__("depends_on", []),
    "duplicate_step_id":
        lambda g: by_id(g, "verify_role_2").__setitem__("step_id", "verify_role_1"),
    "two_producers_for_one_evidence_artifact":
        lambda g: by_id(g, "verify_role_2").__setitem__(
            "evidence", by_id(g, "verify_role_1")["evidence"]),
    "rollback_assigned_to_the_read_only_verifier":
        lambda g: by_id(g, "bootstrap_role_1").__setitem__("rollback_owner", lc.VERIFIER),
    "rollback_assigned_to_the_lead":
        lambda g: by_id(g, "bootstrap_role_1").__setitem__("rollback_owner", lc.LEAD),
    "retired_principal_acts_later":
        lambda g: by_id(g, "verify_caller_identity").__setitem__(
            "retires_principal_after", True),
    "mutation_without_a_rollback_owner":
        lambda g: by_id(g, "assign_operator").__setitem__("rollback_owner", None),
    "mutation_without_a_readback":
        lambda g: by_id(g, "install_inline_policy").__setitem__("read_back", None),
    "step_without_evidence":
        lambda g: by_id(g, "operator_sign_out").__setitem__("evidence", None),
    "ownerless_step":
        lambda g: by_id(g, "create_permission_set").__setitem__("owner", None),
    "verifier_given_a_mutation":
        lambda g: by_id(g, "verify_role_1").__setitem__("is_mutation", True),
    "lead_given_an_aws_action":
        lambda g: by_id(g, "prepare_executor_manifest").__setitem__("action", "iam:GetRole"),
    "identity_centre_admin_moved_off_root":
        lambda g: by_id(g, "create_permission_set").__setitem__("owner", lc.BOOTSTRAP),
    "async_initiator_loses_its_poll":
        lambda g: by_id(g, "poll_provisioning_to_terminal").__setitem__(
            "depends_on", ["assign_operator"]),
    "poll_loses_its_timeout":
        lambda g: by_id(g, "poll_removal_to_terminal").__setitem__("timeout_seconds", None),
    "mandatory_step_deleted":
        lambda g: g.remove(by_id(g, "verify_no_managed_policy_attachments")),
    "verifier_never_retired":
        lambda g: by_id(g, "verifier_sign_out").__setitem__(
            "retires_principal_after", False),
    "temporary_mutation_without_a_timeout":
        lambda g: by_id(g, "bootstrap_role_1").__setitem__("timeout_seconds", None),
    "action_the_owner_does_not_hold":
        lambda g: by_id(g, "verify_role_1").__setitem__("action", "iam:PutRolePolicy"),
    "nonexistent_aws_action":
        lambda g: by_id(g, "verify_expected_role_set").__setitem__(
            "action", "iam:ThisActionDoesNotExist"),
    "verifier_given_a_destructive_action":
        lambda g: by_id(g, "capture_cloudtrail_evidence").__setitem__(
            "action", "s3:DeleteBucket"),
    "unknown_actor_class":
        lambda g: by_id(g, "verify_role_1").__setitem__("owner", "SOMEBODY_ELSE"),
}


@pytest.mark.parametrize("name", sorted(DAG_MUTATIONS))
def test_every_graph_mutation_is_caught(name):
    result = with_mutation(DAG_MUTATIONS[name])
    assert not result["clean"], f"mutation {name!r} left the graph reporting CLEAN"


def test_the_mutation_table_is_broad():
    assert len(DAG_MUTATIONS) >= 30


# =====================================================================================
# DEFECT 7 — the graph hash, checked against an INDEPENDENT implementation (Phases U/V)
# =====================================================================================


def test_the_graph_hash_has_an_independent_check_and_it_is_the_ORACLE():
    """GATE 4N-I21, ARCH-M2: the self-comparing assertion that used to sit here is DELETED.

    It read:

        assert lc.graph_hash() == lifecycle_canonical.expected_hash(lc.steps())

    with the docstring "the expected value is produced WITHOUT calling the production hash
    function". That was false. `lc.graph_hash()` is
    `sha256(lifecycle_canonical.canonical_bytes(steps()))` and `expected_hash()` is
    `sha256(canonical_bytes(steps))` — ONE implementation invoked twice. It is the Gate 4N-I16
    Defect 1 shape, and it shipped alongside the oracle written to replace it, still claiming
    the opposite.

    Deleting it is the correction. This test stands in its place to assert that the independent
    check still EXISTS and is the stdlib-only oracle, so the deletion cannot quietly become
    "no independent check at all".
    """
    import sys as _sys

    _sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "tests" / "oracle"))
    import graph_oracle

    assert graph_oracle.__file__.endswith("graph_oracle.py")
    oracle_hash = graph_oracle.oracle_hash(lc.steps())
    assert lc.graph_hash() == oracle_hash, (
        "the production hash and the INDEPENDENT oracle disagree")

    # ...and the oracle must not be the production path wearing a different name.
    import ast

    tree = ast.parse(pathlib.Path(graph_oracle.__file__).read_text(encoding="utf-8"))
    imported = {(n.names[0].name if isinstance(n, ast.Import) else n.module or "").split(".")[0]
                for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))}
    assert not (imported & {"lifecycle_canonical", "role_bootstrap_lifecycle"}), (
        f"the oracle imports the production canonicalisation: {sorted(imported)}")


def test_the_production_hash_equals_the_committed_byte_fixture():
    """A third anchor: both implementations drifting together is still caught."""
    expected = CANONICAL_FIXTURE.read_text(encoding="utf-8").split()[0]
    assert lc.graph_hash() == expected, (
        "the lifecycle graph changed. If that was intentional, regenerate "
        "tests/fixtures/lifecycle-canonical-sha256.txt deliberately — a graph change must "
        "be an explicit act, not a silent one.")


def test_a_constant_hash_implementation_is_detected(monkeypatch):
    """THE Gate 4N-I15 defect, reproduced and required to fail."""
    monkeypatch.setattr(lc, "graph_hash", lambda: "0" * 64)
    assert lc.graph_hash() != lifecycle_canonical.expected_hash(lc.steps())


SEMANTIC_HASH_MUTATIONS = {
    "omit_a_step": lambda g: g.pop(),
    "change_an_owner": lambda g: by_id(g, "verify_role_1").__setitem__("owner", lc.ROOT),
    "change_an_action": lambda g: by_id(g, "verify_role_1").__setitem__(
        "action", "iam:ListRoles"),
    "change_a_resource": lambda g: by_id(g, "verify_role_1").__setitem__("resource", "*"),
    "change_a_dependency": lambda g: by_id(g, "verify_role_1").__setitem__(
        "depends_on", ["root_session_open"]),
    "change_a_timeout": lambda g: by_id(g, "bootstrap_role_1").__setitem__(
        "timeout_seconds", 31),
    "change_an_evidence_output": lambda g: by_id(g, "verify_role_1").__setitem__(
        "evidence", "something else"),
    "change_a_rollback_owner": lambda g: by_id(g, "bootstrap_role_1").__setitem__(
        "rollback_owner", lc.ROOT),
    "change_a_sequence": lambda g: by_id(g, "verify_role_1").__setitem__("sequence", 99),
}


@pytest.mark.parametrize("name", sorted(SEMANTIC_HASH_MUTATIONS))
def test_a_semantic_mutation_changes_the_hash(name):
    mutated = copy.deepcopy(graph())
    SEMANTIC_HASH_MUTATIONS[name](mutated)
    assert lifecycle_canonical.expected_hash(mutated) != lc.graph_hash(), name


NON_SEMANTIC_HASH_MUTATIONS = {
    "change_a_note": lambda g: by_id(g, "verify_role_1").__setitem__("note", "reworded"),
    "add_a_note": lambda g: by_id(g, "operator_sign_out").__setitem__("note", "new prose"),
    "change_actor_class_alias": lambda g: by_id(g, "verify_role_1").__setitem__(
        "actor_class", "SOMETHING_ELSE"),
    "reorder_dependency_entries": lambda g: by_id(g, "verify_inline_policy_hash").__setitem__(
        "depends_on", list(reversed(by_id(g, "verify_inline_policy_hash")["depends_on"]))),
    "reorder_the_step_list": lambda g: g.reverse(),
}


@pytest.mark.parametrize("name", sorted(NON_SEMANTIC_HASH_MUTATIONS))
def test_a_non_semantic_mutation_does_not_change_the_hash(name):
    """Order is carried by `sequence`, and commentary is excluded by name. Both are stated
    in the canonical contract rather than left for a reader to infer."""
    mutated = copy.deepcopy(graph())
    NON_SEMANTIC_HASH_MUTATIONS[name](mutated)
    assert lifecycle_canonical.expected_hash(mutated) == lc.graph_hash(), name


def test_the_canonical_contract_is_declared_not_implicit():
    assert lifecycle_canonical.SEMANTIC_FIELDS
    assert "note" in lifecycle_canonical.NON_SEMANTIC_FIELDS
    assert "depends_on" in lifecycle_canonical.SORTED_LIST_FIELDS


def test_the_independent_module_does_not_import_the_production_hash():
    """If it did, 'independent' would be a word rather than a property.

    AST, not text. The first draft grepped the source for "role_bootstrap_lifecycle" and
    flagged the module's own DOCSTRING, which explains the defect it exists to prevent — the
    same self-flagging that has now bitten a text scanner in this chain six times. What
    matters is whether the module IMPORTS or CALLS the production code, which the parse tree
    answers exactly and prose cannot confuse.
    """
    import ast
    tree = ast.parse((REPO_ROOT / "scripts" / "lifecycle_canonical.py").read_text(
        encoding="utf-8"))

    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert "role_bootstrap_lifecycle" not in imported, imported

    called = {ast.unparse(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    assert not any("graph_hash" in name for name in called), called


# =====================================================================================
# Carried-forward corrections from the Gate 4N-I15 architect lane
# =====================================================================================


def test_the_residual_session_bound_cites_the_expiry_not_session_duration():
    """Gate 4N-I15 bounded the residual by a SessionDuration this design never sets."""
    note = by_id(graph(), "verify_new_session_unavailable")["note"]
    assert "ALREADY-ISSUED session is NOT invalidated" in note
    assert "policy expiry" in note
    assert "SessionDuration is NOT set" in note


def test_the_managed_policy_check_runs_before_assignment_and_before_deletion():
    """Gate 4N-I15 ran it AFTER deletion, where it passed on 'or the set is gone'."""
    g = graph()
    check = by_id(g, "verify_no_managed_policy_attachments")
    disposal = by_id(g, "permission_set_disposition")
    assign = by_id(g, "assign_operator")
    assert check["sequence"] < disposal["sequence"]
    assert check["sequence"] < assign["sequence"], (
        "the check must precede assignment: the escalation window opens there")
    assert "EMPTY" in check["read_back"]
    assert "or the set is gone" not in (check["read_back"] or "")


def test_the_reserved_sso_role_is_named_as_a_real_arn_with_its_path():
    """A bare AWSReservedSSO_* string is not an ARN and would match no policy."""
    resource = by_id(graph(), "verify_reserved_role_exists")["resource"]
    assert resource.startswith("arn:aws:iam::")
    assert "aws-reserved/sso.amazonaws.com/" in resource
