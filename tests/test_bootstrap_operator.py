"""SignalNestBoundaryBootstrapOperator materialization and negative tests (Gate 4N-I7, Defect 5).

THE DEFECT. The Gate 4N-I6 rollout assigned 12 of its 15 operations to this principal while
its exact policy bytes existed nowhere — not in AWS, not in the repository, not as a
generator output, not as a reviewed artifact. `ownerless_operations: 0` sat in a static
JSON file next to a principal nobody could inspect, and nothing could have made that number
move. It is Defect 3 of the previous gate reproduced for a MORE privileged principal.

The policy now exists as a generator, its ownership claim is computed by evaluating each
rollout operation against it, and the tests below are mostly NEGATIVE: what this principal
must not be able to do, and what must break if the grant is widened.

No AWS access, no network. Nothing here creates the permission set — that is a root-console
operation, stated as such in the rollout graph.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import gen_bootstrap_operator_policy as boot  # noqa: E402
import gen_boundary_rollout as rollout  # noqa: E402
import expiry_authorization as _ea  # noqa: E402
import iam_eval  # noqa: E402
import must_not_contract  # noqa: E402
import signalnest_identity as identity  # noqa: E402
from iam_eval import Decision  # noqa: E402

POLICY = boot.bootstrap_operator_policy(_ea.ACTIVE_EXPIRY_UTC)
IN_WINDOW = {"aws:CurrentTime": "2026-07-31T12:00:00Z"}
AFTER_WINDOW = {"aws:CurrentTime": "2026-09-01T00:00:00Z"}
BOUNDARY_CTX = {**IN_WINDOW, "iam:PermissionsBoundary": identity.BOUNDARY_POLICY_ARN}

OUTSIDE_ROLE = f"arn:aws:iam::{identity.ACCOUNT}:role/some-unrelated-role"


def test_the_policy_is_structurally_valid():
    assert iam_eval.validate_policy(POLICY, kind="identity") == []


def test_it_can_do_its_job():
    """Positive control. A principal that cannot perform the rollout is not safer."""
    assert iam_eval.decide(POLICY, "iam:CreatePolicy", identity.BOUNDARY_POLICY_ARN,
                           IN_WINDOW).decision is Decision.EXPLICIT_ALLOW
    for role in identity.ALL_ROLE_ARNS:
        assert iam_eval.decide(POLICY, "iam:PutRolePermissionsBoundary", role,
                               BOUNDARY_CTX).decision is Decision.EXPLICIT_ALLOW


def test_every_rollout_operation_it_owns_is_actually_authorized():
    """The invariant that was previously a hand-maintained field in a JSON file."""
    result = rollout.evaluate()
    assert result["invariants"]["ownerless_operations"] == 0, result["ownerless_detail"]
    assert result["invariants"]["rollback_without_owner_operations"] == 0
    assert result["invariants"]["retired_before_use_operations"] == 0


def test_the_ownership_invariant_can_actually_fail(monkeypatch):
    """Controls the control. Gate 4N-I6's version could not report a non-zero value."""
    stripped = {
        **POLICY,
        "Statement": [s for s in boot.bootstrap_operator_policy(_ea.ACTIVE_EXPIRY_UTC)["Statement"]
                      if s.get("Sid") != "BoundaryPolicyLifecycle"],
    }
    monkeypatch.setitem(rollout.POLICIES, rollout.BOOTSTRAP, lambda: stripped)
    result = rollout.evaluate()
    assert result["invariants"]["ownerless_operations"] > 0, (
        "removing the entire policy-lifecycle grant left the rollout claiming full ownership")


# --- negative: the capabilities it must never hold -------------------------------------


NEVER = [
    # Creating or authoring roles is what the boundary exists to constrain. A principal
    # that installs the boundary must not also be able to sidestep it.
    "iam:CreateRole", "iam:PutRolePolicy", "iam:DeleteRolePolicy", "iam:AttachRolePolicy",
    "iam:DetachRolePolicy", "iam:UpdateAssumeRolePolicy", "iam:DeleteRole",
    "iam:PassRole", "iam:CreateUser", "iam:CreateAccessKey",
    # It cannot provision or retire itself; that dependency is stated, not smuggled.
    "sso:CreatePermissionSet", "sso:PutInlinePolicyToPermissionSet",
    "sso:ProvisionPermissionSet", "sso:DeletePermissionSet",
    "organizations:LeaveOrganization", "identitystore:CreateUser",
    # No data, no secrets, no state, no evidence.
    "s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:PutBucketPolicy",
    "dynamodb:GetItem", "dynamodb:PutItem",
    "secretsmanager:GetSecretValue", "secretsmanager:PutSecretValue",
    "kms:Decrypt", "kms:ScheduleKeyDeletion",
    "cloudtrail:StopLogging", "cloudtrail:DeleteTrail",
    "ecs:RegisterTaskDefinition", "ecs:RunTask", "ecs:CreateService",
    "rds:ModifyDBInstance", "rds:DeleteDBInstance",
    "sts:AssumeRole",
]


@pytest.mark.parametrize("action", NEVER)
def test_the_bootstrap_operator_is_explicitly_denied(action):
    """EXPLICIT, not implicit: an absent grant can be supplied by another attachment."""
    iam_eval.require_explicit_deny(POLICY, action, "*", IN_WINDOW,
                                   sid="BootstrapDenyEscalation")


def test_boundary_administration_is_fenced_to_the_eight_roles():
    """The Allow scopes it; only the fence makes that scoping explicit."""
    for action in boot.BOUNDARY_ATTACHMENT_ACTIONS:
        assert iam_eval.decide(POLICY, action, identity.ALL_ROLE_ARNS[0],
                               BOUNDARY_CTX).decision is Decision.EXPLICIT_ALLOW
        iam_eval.require_explicit_deny(
            POLICY, action, OUTSIDE_ROLE, BOUNDARY_CTX,
            sid="BootstrapDenyBoundaryAdministrationOutsideTheEightRoles")


def test_it_cannot_attach_a_policy_that_is_not_the_reviewed_boundary():
    """ATTACH only. Without the condition it could install a permissive 'boundary'.

    GATE 4N-I10 DEFECT 7 split attach from remove. Removal deliberately carries NO
    iam:PermissionsBoundary condition — whether AWS populates that key for
    DeleteRolePermissionsBoundary is disputed and unproven, and a StringEquals against an
    ABSENT key evaluates FALSE, which would make the rollback grant dead at runtime exactly
    when rollback is being attempted. Asserting the condition on removal here would have
    pinned the dangerous design in place.
    """
    wrong = {**IN_WINDOW,
             "iam:PermissionsBoundary": f"arn:aws:iam::{identity.ACCOUNT}:policy/something-else"}
    for action in boot.BOUNDARY_ATTACH_ACTIONS:
        assert iam_eval.decide(POLICY, action, identity.ALL_ROLE_ARNS[0], wrong).decision \
            is not Decision.EXPLICIT_ALLOW


def test_rollback_works_without_the_disputed_condition_key():
    """The whole point of the Phase O split: rollback must not depend on an unproven key."""
    pessimistic = {"aws:CurrentTime": "2026-07-31T12:00:00Z"}  # key never populated
    for role in identity.ALL_ROLE_ARNS:
        assert iam_eval.decide(POLICY, "iam:DeleteRolePermissionsBoundary", role,
                               pessimistic).decision is Decision.EXPLICIT_ALLOW, role
    iam_eval.require_explicit_deny(POLICY, "iam:DeleteRolePermissionsBoundary",
                                   OUTSIDE_ROLE, pessimistic)


def test_attachment_still_requires_the_undisputed_condition():
    """Splitting must not have weakened the half that AWS definitely supports."""
    pessimistic = {"aws:CurrentTime": "2026-07-31T12:00:00Z"}
    assert iam_eval.decide(POLICY, "iam:PutRolePermissionsBoundary",
                           identity.ALL_ROLE_ARNS[0], pessimistic).decision \
        is not Decision.EXPLICIT_ALLOW


def test_it_cannot_touch_any_policy_other_than_the_boundary():
    other = f"arn:aws:iam::{identity.ACCOUNT}:policy/some-other-policy"
    for action in boot.POLICY_LIFECYCLE_ACTIONS:
        assert iam_eval.decide(POLICY, action, other, IN_WINDOW).decision \
            is not Decision.EXPLICIT_ALLOW


# --- the expiry ------------------------------------------------------------------------


def test_every_grant_expires():
    """A temporary principal whose grant does not lapse is a permanent principal."""
    for statement in POLICY["Statement"]:
        if statement["Effect"] != "Allow":
            continue
        assert statement.get("Condition", {}).get("DateLessThan", {}).get("aws:CurrentTime"), (
            f"{statement['Sid']} has no expiry")


def test_no_grant_survives_the_window():
    for action, resource in (
        ("iam:CreatePolicy", identity.BOUNDARY_POLICY_ARN),
        ("iam:PutRolePermissionsBoundary", identity.ALL_ROLE_ARNS[0]),
        ("iam:GetRole", identity.ALL_ROLE_ARNS[0]),
        ("sts:GetCallerIdentity", "*"),
    ):
        assert iam_eval.decide(POLICY, action, resource,
                               {**AFTER_WINDOW,
                                "iam:PermissionsBoundary": identity.BOUNDARY_POLICY_ARN}
                               ).decision is not Decision.EXPLICIT_ALLOW


def test_the_ceiling_does_NOT_expire():
    """A ceiling that lapses stops protecting exactly when the window is being abused."""
    ceiling = next(s for s in POLICY["Statement"] if s["Sid"] == "BootstrapDenyEscalation")
    assert "Condition" not in ceiling
    iam_eval.require_explicit_deny(POLICY, "iam:CreateRole", "*", AFTER_WINDOW)


def test_the_ceiling_is_derived_from_the_contract_not_hand_listed():
    """The hand-written list omitted eight capabilities and scored 31/39."""
    ceiling = set(next(s for s in POLICY["Statement"]
                       if s["Sid"] == "BootstrapDenyEscalation")["Action"])
    missing = set(must_not_contract.FORBIDDEN_CAPABILITIES) - boot.CEILING_EXCEPTIONS - ceiling
    assert not missing, f"the ceiling does not cover the contract: {sorted(missing)}"


def test_it_cannot_revise_the_boundary_it_installs():
    """Operating Model 1. The 4N-I7 architect lane found this principal could rewrite the
    reviewed boundary to Allow * and set it default. The capability is now REMOVED, not
    conditioned — AWS has no condition key over policy-document bytes."""
    for action in boot.RETAINED_BY_ROOT:
        assert action not in boot.POLICY_LIFECYCLE_ACTIONS, f"{action} is still granted"
        iam_eval.require_explicit_deny(POLICY, action, identity.BOUNDARY_POLICY_ARN,
                                       IN_WINDOW, sid="BootstrapDenyEscalation")


def test_policy_creation_is_fenced_to_the_boundary_arn():
    other = f"arn:aws:iam::{identity.ACCOUNT}:policy/anything-else"
    assert iam_eval.decide(POLICY, "iam:CreatePolicy", identity.BOUNDARY_POLICY_ARN,
                           IN_WINDOW).decision is Decision.EXPLICIT_ALLOW
    iam_eval.require_explicit_deny(POLICY, "iam:CreatePolicy", other, IN_WINDOW,
                                   sid="BootstrapDenyPolicyCreationOutsideTheBoundary")


def test_a_capability_added_to_the_contract_lands_in_the_ceiling_automatically(monkeypatch):
    """The property that makes derivation worth having."""
    monkeypatch.setitem(must_not_contract.FORBIDDEN_CAPABILITIES,
                        "lambda:CreateFunction", "run arbitrary code as any passed role")
    import importlib

    reloaded = importlib.reload(boot)
    try:
        assert "lambda:CreateFunction" in reloaded.FORBIDDEN
        iam_eval.require_explicit_deny(reloaded.bootstrap_operator_policy(_ea.ACTIVE_EXPIRY_UTC),
                                       "lambda:CreateFunction", "*", IN_WINDOW)
    finally:
        monkeypatch.undo()
        importlib.reload(boot)


def test_the_policy_hashes_are_stable():
    """Two calls must produce identical bytes, or no reviewed hash means anything."""
    first = boot.canonical(boot.bootstrap_operator_policy(_ea.ACTIVE_EXPIRY_UTC))
    second = boot.canonical(boot.bootstrap_operator_policy(_ea.ACTIVE_EXPIRY_UTC))
    assert first == second


def test_the_expiry_is_the_only_thing_the_argument_changes():
    """Byte-reversal control: a stamp must differ from its template ONLY in the expiry."""
    import json

    template = json.loads(boot.canonical(boot.bootstrap_operator_policy(_ea.ACTIVE_EXPIRY_UTC)).decode())
    stamped = json.loads(boot.canonical(
        boot.bootstrap_operator_policy(_ea.ACTIVE_EXPIRY_UTC)).decode())

    def strip(doc):
        return [{k: v for k, v in s.items() if k != "Condition"} for s in doc["Statement"]]

    assert strip(template) == strip(stamped)
    times = {c["DateLessThan"]["aws:CurrentTime"]
             for s in stamped["Statement"] if (c := s.get("Condition"))
             if "DateLessThan" in c}
    assert times == {_ea.ACTIVE_EXPIRY_UTC}
