"""Durable permissions-boundary input (Gate 4N-I8, Defect 8).

THE DEFECT, found by the Gate 4N-I7 adversarial lane and covered by nothing at the time.
`permissions_boundary` is a MANAGED attribute on all eight roles;
`role_permissions_boundary_arn` defaulted to null; no tfvars file set it; and none of the
16 rollout operations persisted it. The sequence that follows is:

  1. the bootstrap operator attaches the boundary out-of-band;
  2. a later OpenTofu execution supplies null;
  3. OpenTofu plans REMOVAL;
  4. the next apply strips the boundary from all five existing roles.

The hardening was therefore non-durable, and nothing said so.

WHAT PROVES WHAT — stated plainly, because these two levels are not equally strong:

  EXECUTED   infra/aws/modules/iam/boundary_durability.tftest.hcl runs `tofu test` offline
             with a mocked provider. It proves the attribute reaches all five roles, that a
             null input really does produce unbounded roles (the removal state), and that a
             malformed ARN fails before any resource is planned.
  STRUCTURAL this file. It parses variables.tf and asserts the validation blocks exist with
             the right conditions, and it re-implements the guard's logic to show the
             intended truth table. It does NOT execute OpenTofu. The root composition cannot
             be planned offline without a synthetic fixture for every module, and this gate
             forbids a backend plan — so the root-level cross-variable validation is proven
             structurally, which is weaker, and is labelled as such rather than reported as
             an execution result.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
VARIABLES = REPO_ROOT / "infra/aws/variables.tf"
IAM_MAIN = REPO_ROOT / "infra/aws/modules/iam/main.tf"
READER_IAM = REPO_ROOT / "infra/aws/modules/revision_reader/iam.tf"
MODULE_TFTEST = REPO_ROOT / "infra/aws/modules/iam/boundary_durability.tftest.hcl"


def variable_block(name: str) -> str:
    text = VARIABLES.read_text(encoding="utf-8")
    match = re.search(r'variable "%s" \{(.*?)\n\}\n' % name, text, re.DOTALL)
    assert match, f"variable {name!r} is not declared"
    return match.group(1)


def test_the_boundary_mode_variable_has_NO_default():
    """GATE 4N-I14 DEFECT 3, and a reversal of what this test used to assert.

    It previously REQUIRED `default = "disabled"`, reasoning that the default should match
    the deployed unbounded state. That reasoning holds only until the boundary is attached;
    afterwards the same default silently plans REMOVAL from every role, and three review
    lanes flagged it. A security boundary that disappears through OMISSION is not a boundary.
    There is no default now: an execution that does not state its mode fails.
    """
    block = variable_block("role_boundary_mode")
    # Match the ATTRIBUTE (`default = ...`), not the word: the description deliberately says
    # "There is no default", and a substring check flagged its own explanation.
    assert not re.search(r"^\s*default\s*=", block, re.MULTILINE), (
        "role_boundary_mode has a default again — omission would silently choose it")
    assert 'contains(["disabled", "required"], var.role_boundary_mode)' in block


def test_the_mode_is_consumed_by_the_resource_graph_not_only_by_validation():
    """DEFECT 2. The mode used to appear only inside variable validation, so it changed no
    plan. Every role must now derive its boundary FROM the mode."""
    for module in ("infra/aws/modules/iam/main.tf",
                   "infra/aws/modules/revision_reader/iam.tf"):
        text = (REPO_ROOT / module).read_text(encoding="utf-8")
        assert "local.effective_permissions_boundary" in text, module
        assert "var.role_boundary_mode == \"required\"" in text, module
        assert "permissions_boundary = var.role_permissions_boundary_arn" not in text, (
            f"{module} still consumes the ARN directly, bypassing the mode")


def test_every_role_reads_the_derived_value_not_the_raw_arn():
    iam = (REPO_ROOT / "infra/aws/modules/iam/main.tf").read_text(encoding="utf-8")
    reader = (REPO_ROOT / "infra/aws/modules/revision_reader/iam.tf").read_text(encoding="utf-8")
    derived = (iam + reader).count("permissions_boundary = local.effective_permissions_boundary")
    assert derived == 8, f"{derived} of 8 roles read the derived value"


def test_required_mode_with_a_null_arn_fails_at_plan_time():
    """Structural: the precondition exists in both modules."""
    for module in ("infra/aws/modules/iam/main.tf",
                   "infra/aws/modules/revision_reader/iam.tf"):
        text = (REPO_ROOT / module).read_text(encoding="utf-8")
        assert "boundary_mode_precondition" in text, module
        assert "plans REMOVAL of the boundary from every deployed role" in text, module


def test_enforced_mode_requires_a_non_null_boundary_arn():
    """THE durability guard. This is the pairing that was missing entirely."""
    block = variable_block("role_permissions_boundary_arn")
    assert 'var.role_boundary_mode != "required" || var.role_permissions_boundary_arn != null' \
        in block, "nothing prevents required mode with a null ARN — the removal state"


def test_the_arn_must_name_the_reviewed_boundary_policy():
    """A syntactically valid ARN naming a different policy attaches the wrong ceiling."""
    block = variable_block("role_permissions_boundary_arn")
    assert "signalnest-staging-role-boundary" in block, (
        "any policy ARN would satisfy the shape check; the reviewed NAME must be pinned")


def test_the_arn_shape_is_still_validated():
    block = variable_block("role_permissions_boundary_arn")
    assert "arn:aws:iam::[0-9]{12}:policy/" in block


@pytest.mark.parametrize("mode,arn,ok", [
    ("required", None, False),
    ("required", "arn:aws:iam::111122223333:policy/signalnest-staging-role-boundary", True),
    ("required", "arn:aws:iam::111122223333:policy/some-other-policy", False),
    ("required", "not-an-arn", False),
    ("disabled", None, True),
    ("disabled", "arn:aws:iam::111122223333:policy/signalnest-staging-role-boundary", True),
])
def test_the_guard_truth_table(mode, arn, ok):
    """Re-implements the HCL conditions so the intended behaviour is readable and pinned.

    This mirrors the validation; it does not execute it. The executed proof is the
    module-level tftest.
    """
    shape_ok = arn is None or bool(re.match(r"^arn:aws:iam::[0-9]{12}:policy/", arn))
    name_ok = arn is None or bool(re.search(r"policy/signalnest-staging-role-boundary$", arn))
    pairing_ok = mode != "required" or arn is not None
    assert (shape_ok and name_ok and pairing_ok) is ok


def test_every_role_still_sets_the_attribute():
    """A role that stopped setting it would be permanently unbounded, silently."""
    iam = IAM_MAIN.read_text(encoding="utf-8")
    reader = READER_IAM.read_text(encoding="utf-8")
    roles = len(re.findall(r'^resource "aws_iam_role" ', iam + reader, re.MULTILINE))
    # Gate 4N-I14: roles read the DERIVED value now, not the raw variable.
    attrs = len(re.findall(r'permissions_boundary\s*=\s*local\.effective_permissions_boundary',
                           iam + reader))
    assert roles == 8, f"expected 8 roles, found {roles}"
    assert attrs == roles, f"{roles} roles but only {attrs} set permissions_boundary"


def test_the_executed_module_level_proof_exists_and_covers_the_removal_state():
    """Guards the stronger half: if that file is deleted, only structure would remain."""
    assert MODULE_TFTEST.exists(), "the executed durability proof is missing"
    text = MODULE_TFTEST.read_text(encoding="utf-8")
    assert "mock_provider" in text, "the proof must be offline"
    assert "required_mode_with_a_null_arn_fails_before_any_resource_is_planned" in text
    assert "expect_failures" in text, "the negative case must actually expect a failure"
    for role in ("execution", "api_task", "worker_task", "migration_task", "ci_publisher"):
        assert f"aws_iam_role.{role}" in text, f"{role} is not asserted"


def test_the_structural_limitation_is_documented_not_hidden():
    """An unstated limitation reads as a stronger claim than it is."""
    text = Path(__file__).read_text(encoding="utf-8")
    assert "STRUCTURAL" in text and "does NOT execute OpenTofu" in text
