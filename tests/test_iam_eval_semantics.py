"""Semantic tests for the IAM evaluator (Gate 4N-I5).

Three Gate 4N-I4 defects are pinned here:

  * `ForAllValues` was treated as fail-closed on an absent key. AWS makes it VACUOUSLY
    TRUE — so a `ForAllValues`-conditioned Deny silently stopped applying.
  * The fail-closed guarantee was ORDER-DEPENDENT: `effect()` returned on the first
    matching Deny, so an unmodelled element in a later statement never raised.
  * `iam:DeleteRolePolicy` was mapped to an empty condition-key set, though AWS
    supports `iam:PermissionsBoundary` on it.

No AWS access, no network.
"""

from __future__ import annotations

import itertools
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import expiry_authorization as _ea  # noqa: E402
import iam_eval  # noqa: E402


def policy(*statements) -> dict:
    return {"Version": "2012-10-17", "Statement": list(statements)}


# --- Phase I: ForAllValues / ForAnyValue -------------------------------------------
#
# The table below is the contract. AWS semantics:
#   ForAllValues -> true when there are NO values to check (vacuous truth)
#   ForAnyValue  -> false when there are NO values

FORALL_DENY = policy(
    {"Effect": "Allow", "Action": "iam:TagRole", "Resource": "*"},
    {"Sid": "DenyUnlessApprovedKeys", "Effect": "Deny", "Action": "iam:TagRole", "Resource": "*",
     "Condition": {"ForAllValues:StringEquals": {"aws:TagKeys": ["Approved"]}}},
)


@pytest.mark.parametrize("ctx,expected,why", [
    ({}, "Deny", "absent key -> ForAllValues is vacuously true -> the Deny applies"),
    ({"aws:TagKeys": None}, "Deny", "null key is treated as absent -> vacuously true"),
    ({"aws:TagKeys": []}, "Deny", "empty list -> nothing violates the constraint -> true"),
    ({"aws:TagKeys": ["Approved"]}, "Deny", "every value satisfies -> true"),
    ({"aws:TagKeys": ["Approved", "Other"]}, "Allow", "one value violates -> false -> Deny does not apply"),
    ({"aws:TagKeys": "Approved"}, "Deny", "malformed scalar where a list is expected is coerced to one value"),
])
def test_forallvalues_semantics(ctx, expected, why):
    assert iam_eval.effect(FORALL_DENY, "iam:TagRole", "*", ctx) == expected, why


FORANY_ALLOW = policy(
    {"Sid": "AllowIfAnyApproved", "Effect": "Allow", "Action": "s3:GetObject", "Resource": "*",
     "Condition": {"ForAnyValue:StringEquals": {"k": ["v"]}}},
)


@pytest.mark.parametrize("ctx,expected,why", [
    ({}, "ImplicitDeny", "absent key -> ForAnyValue is false -> statement does not match"),
    ({"k": None}, "ImplicitDeny", "null is treated as absent -> false"),
    ({"k": []}, "ImplicitDeny", "empty list -> no value satisfies -> false"),
    ({"k": ["v"]}, "Allow", "one value satisfies -> true"),
    ({"k": ["x", "v"]}, "Allow", "at least one satisfies -> true"),
    ({"k": ["x"]}, "ImplicitDeny", "no value satisfies -> false"),
    ({"k": "v"}, "Allow", "scalar coerced to a single-element list"),
])
def test_foranyvalue_semantics(ctx, expected, why):
    assert iam_eval.effect(FORANY_ALLOW, "s3:GetObject", "*", ctx) == expected, why


def test_null_can_require_presence_alongside_forallvalues():
    """The documented way to stop ForAllValues being vacuously satisfied."""
    p = policy({"Effect": "Deny", "Action": "iam:TagRole", "Resource": "*",
                "Condition": {"Null": {"aws:TagKeys": "false"},
                              "ForAllValues:StringEquals": {"aws:TagKeys": ["Approved"]}}})
    assert iam_eval.effect(p, "iam:TagRole", "*", {}) == "ImplicitDeny", (
        "Null false requires the key to be PRESENT, so the vacuous case no longer denies"
    )
    assert iam_eval.effect(p, "iam:TagRole", "*", {"aws:TagKeys": ["Approved"]}) == "Deny"


def test_single_valued_operators_still_fail_closed_on_absence():
    p = policy({"Effect": "Allow", "Action": "iam:CreateRole", "Resource": "*",
                "Condition": {"StringEquals": {"iam:PermissionsBoundary": "arn:aws:iam::1:policy/b"}}})
    assert iam_eval.effect(p, "iam:CreateRole", "*", {}) == "ImplicitDeny"


# --- Phase J: order independence and validate-before-evaluate -----------------------


ORDER_POLICY_STATEMENTS = [
    {"Sid": "A", "Effect": "Allow", "Action": "s3:GetObject", "Resource": "arn:aws:s3:::b/*"},
    {"Sid": "B", "Effect": "Deny", "Action": "s3:GetObject", "Resource": "arn:aws:s3:::b/secret"},
    {"Sid": "C", "Effect": "Allow", "Action": "s3:GetObject", "Resource": "arn:aws:s3:::b/secret"},
    {"Sid": "D", "Effect": "Allow", "Action": "s3:ListBucket", "Resource": "arn:aws:s3:::b"},
]


@pytest.mark.parametrize("perm", list(itertools.permutations(range(4))))
def test_result_is_invariant_under_statement_permutation(perm):
    p = policy(*[ORDER_POLICY_STATEMENTS[i] for i in perm])
    assert iam_eval.effect(p, "s3:GetObject", "arn:aws:s3:::b/secret", {}) == "Deny"
    assert iam_eval.effect(p, "s3:GetObject", "arn:aws:s3:::b/other", {}) == "Allow"


def test_unmodelled_element_raises_regardless_of_position():
    """The Gate 4N-I4 defect: a matching Deny first meant later statements never raised."""
    deny = {"Effect": "Deny", "Action": "s3:GetObject", "Resource": "*"}
    bad = {"Effect": "Allow", "Action": "s3:GetObject", "Resource": "*", "Principal": {"AWS": "*"}}
    for statements in ([deny, bad], [bad, deny]):
        with pytest.raises(iam_eval.UnsupportedPolicyFeature):
            iam_eval.effect(policy(*statements), "s3:GetObject", "*", {})


def test_unsupported_condition_raises_even_when_another_statement_would_decide():
    deny = {"Effect": "Deny", "Action": "s3:GetObject", "Resource": "*"}
    bad = {"Effect": "Allow", "Action": "s3:GetObject", "Resource": "*",
           "Condition": {"IpAddress": {"aws:SourceIp": "1.2.3.4"}}}
    for statements in ([deny, bad], [bad, deny]):
        with pytest.raises(iam_eval.UnsupportedPolicyFeature):
            iam_eval.effect(policy(*statements), "s3:GetObject", "*", {})


def test_condition_map_key_order_does_not_change_the_result():
    a = {"Effect": "Allow", "Action": "x:y", "Resource": "*",
         "Condition": {"StringEquals": {"k1": "v1"}, "DateLessThan": {"aws:CurrentTime": "2030-01-01T00:00:00Z"}}}
    b = {"Effect": "Allow", "Action": "x:y", "Resource": "*",
         "Condition": {"DateLessThan": {"aws:CurrentTime": "2030-01-01T00:00:00Z"}, "StringEquals": {"k1": "v1"}}}
    ctx = {"k1": "v1", "aws:CurrentTime": "2026-01-01T00:00:00Z"}
    assert iam_eval.effect(policy(a), "x:y", "*", ctx) == iam_eval.effect(policy(b), "x:y", "*", ctx) == "Allow"


def test_malformed_condition_block_fails_closed():
    with pytest.raises(iam_eval.UnsupportedPolicyFeature):
        iam_eval.effect(policy({"Effect": "Allow", "Action": "x:y", "Resource": "*",
                                "Condition": {"StringEquals": ["not", "a", "map"]}}), "x:y", "*", {})


def test_unknown_effect_raises():
    with pytest.raises(iam_eval.UnsupportedPolicyFeature):
        iam_eval.effect(policy({"Effect": "Maybe", "Action": "x:y", "Resource": "*"}), "x:y", "*", {})


# --- Phase K: action-condition support matrix ---------------------------------------
#
# Values verified against the AWS Service Authorization Reference during Gate 4N-I5.

EXPECTED_MATRIX = {
    "iam:CreateRole": {"aws:RequestTag/${TagKey}", "aws:TagKeys", "iam:PermissionsBoundary"},
    "iam:TagRole": {"aws:RequestTag/${TagKey}", "aws:TagKeys"},
    "iam:UntagRole": {"aws:TagKeys"},
    "iam:PutRolePolicy": {"iam:PermissionsBoundary"},
    "iam:DeleteRolePolicy": {"iam:PermissionsBoundary"},
    "iam:PutRolePermissionsBoundary": {"iam:PermissionsBoundary"},
    # INFRA-9 B-3 (adversarial-lane round-3 delta 2): the delta's own additions must not
    # rest on the live table asserting itself. Verified against the AWS Service
    # Authorization Reference (KMS actions accept kms:ViaService), retained with the B-3
    # evidence (b3-part-a-live-readback/ecs-action-truth-evidence.md sources).
    "kms:Decrypt": {"kms:ViaService"},
    "kms:GenerateDataKey": {"kms:ViaService"},
    # GATE 4N-I11 DEFECT 14. iam:DeleteRolePermissionsBoundary was asserted here as
    # SUPPORTING the key. Whether AWS POPULATES it for the Delete action is disputed and
    # unproven, and the evaluator asserting the optimistic reading meant the dead-grant
    # detector was calibrated to the reading the policy generator refuses to rely on. It has
    # moved to iam_eval.DISPUTED_RUNTIME_CONTEXT and is no longer claimed as fact.
}


def test_the_disputed_pairing_is_recorded_as_unknown_not_as_fact():
    """The evaluator must not encode an unproven AWS runtime behaviour as established."""
    pairing = ("iam:DeleteRolePermissionsBoundary", "iam:PermissionsBoundary")
    assert pairing in iam_eval.DISPUTED_RUNTIME_CONTEXT, (
        "the disputed pairing is not recorded as disputed")
    assert "iam:PermissionsBoundary" not in iam_eval.ACTION_CONDITION_KEYS.get(
        pairing[0], set()), "the optimistic reading is still asserted as fact"
    why = iam_eval.DISPUTED_RUNTIME_CONTEXT[pairing]
    assert "Unproven" in why or "unproven" in why


def test_relying_on_the_disputed_pairing_is_reported():
    policy = {"Version": "2012-10-17", "Statement": [{
        "Sid": "Risky", "Effect": "Allow",
        "Action": "iam:DeleteRolePermissionsBoundary", "Resource": "*",
        "Condition": {"StringEquals": {"iam:PermissionsBoundary": "arn:aws:iam::1:policy/b"}}}]}
    problems = iam_eval.disputed_pairings(policy)
    assert problems and "DISPUTED" in problems[0]


def test_the_shipped_policies_do_not_depend_on_a_disputed_pairing():
    """Critical execution must not rest on an UNKNOWN."""
    import policy_inventory

    for key, entry in sorted(policy_inventory.discover().items()):
        if "document" not in entry:
            continue
        assert iam_eval.disputed_pairings(entry["document"]) == [], key


@pytest.mark.parametrize("action,keys", sorted(EXPECTED_MATRIX.items()))
def test_action_condition_matrix_matches_aws(action, keys):
    assert iam_eval.ACTION_CONDITION_KEYS.get(action) == keys


def test_delete_role_policy_supports_the_boundary_key():
    """Gate 4N-I4 mapped this to the empty set, so a correct statement read as dead."""
    assert "iam:PermissionsBoundary" in iam_eval.ACTION_CONDITION_KEYS["iam:DeleteRolePolicy"]
    ok = policy({"Sid": "S", "Effect": "Allow", "Action": "iam:DeleteRolePolicy", "Resource": "*",
                 "Condition": {"StringEquals": {"iam:PermissionsBoundary": "arn:aws:iam::1:policy/b"}}})
    assert iam_eval.validate_policy(ok) == [], "a boundary-conditioned DeleteRolePolicy is valid"


def test_tagrole_with_the_boundary_key_is_still_rejected():
    bad = policy({"Sid": "S", "Effect": "Allow", "Action": "iam:TagRole", "Resource": "*",
                  "Condition": {"StringEquals": {"iam:PermissionsBoundary": "arn:aws:iam::1:policy/b"}}})
    assert any("does not support condition key" in p for p in iam_eval.validate_policy(bad))


def test_empty_condition_is_rejected_where_the_design_requires_a_boundary():
    """Role creation MUST carry the boundary condition.

    GATE 4N-I9: this moved from the Stage-A operator to the dedicated role bootstrap
    operator. The Stage-A operator now holds NO role authoring at all — iam:CreateRole
    accepts the AssumeRolePolicyDocument and AWS has no condition key over it, so scoping by
    role name and boundary constrained what the role could DO while leaving who may ASSUME
    it entirely to the caller.
    """
    import gen_operator_policies as gen
    import gen_role_bootstrap_policy as rb

    policy = rb.role_bootstrap_policy(_ea.ACTIVE_EXPIRY_UTC)
    create = [s for s in policy["Statement"]
              if s.get("Effect") == "Allow"
              and "iam:CreateRole" in iam_eval._as_list(s.get("Action"))]
    assert create, "the role bootstrap operator must be able to create the reader roles"
    for stmt in create:
        assert stmt.get("Condition", {}).get("StringEquals", {}).get(
            "iam:PermissionsBoundary") == gen.ARN["boundary"], (
            "an unconditioned CreateRole would let any boundary — or none — be attached")

    fences = [s for s in policy["Statement"]
              if s.get("Effect") == "Deny"
              and "iam:CreateRole" in iam_eval._as_list(s.get("Action"))
              and "NotResource" in s]
    assert fences, "role creation must also be fenced to the three reader roles"
    for stmt in fences:
        assert "Condition" not in stmt, (
            "the fence must be an unconditional NotResource deny, so it cannot be "
            "sidestepped by omitting request context")


def test_the_stage_a_operator_holds_no_trust_bearing_role_authoring_capability():
    """The positive statement of Gate 4N-I9 Defect 1 being closed.

    GATE 4N-I16 DEFECT 3 NARROWED THIS SET, deliberately and with the reasoning stated.
    The Gate 4N-I9 finding is about the ASSUME-ROLE TRUST DOCUMENT: CreateRole and
    UpdateAssumeRolePolicy decide WHO MAY ASSUME a role, AWS publishes no condition key over
    that document, and trust granted inside the window OUTLIVES the window. That reasoning
    does not reach iam:PutRolePolicy, which carries no trust document and creates no
    principal — it writes an inline policy to a role that already exists, bounded by that
    role's permissions boundary.

    Treating the two as one category is what forced Gate 4N-I15 to choose between a false
    closure exclusion and an apply that fails after the ECR resources exist. It chose the
    exclusion, and the closure went green while no principal held an action the composition
    cannot apply without.

    PutRolePolicy is NOT simply released here. It is now:
      * classified REQUIRED_TEMPORARILY from primary evidence
        (scripts/putrolepolicy_classification.py);
      * granted only on the enumerated role ARNs the composition declares;
      * conditioned on iam:PermissionsBoundary equalling the reviewed ceiling;
      * EXPLICITLY denied everywhere else (see the companion test below);
      * registered as a scoped EXEMPTION in the allow-model, which independently proves the
        out-of-scope denial rather than accepting implicit denial.
    """
    import gen_operator_policies as gen

    temp = gen.bootstrap_temp_policy(_ea.ACTIVE_EXPIRY_UTC)
    ctx = {"aws:CurrentTime": "2026-07-31T12:00:00Z",
           "aws:RequestedRegion": "us-east-1",
           "iam:PermissionsBoundary": gen.ARN["boundary"]}
    for action in ("iam:CreateRole", "iam:TagRole",
                   "iam:UpdateAssumeRolePolicy", "iam:DeleteRole"):
        iam_eval.require_explicit_deny(temp, action, gen.READER_ROLE_ARNS[0], ctx)


def test_the_stage_a_inline_policy_grant_is_fenced_to_the_declared_roles():
    """The containment that replaces the blanket deny. Anything outside the enumerated
    roles must be EXPLICITLY denied — implicit denial is the absence of a grant, not a
    control, and it evaporates as soon as any other statement widens the action."""
    import gen_operator_policies as gen

    temp = gen.bootstrap_temp_policy(_ea.ACTIVE_EXPIRY_UTC)
    ctx = {"aws:CurrentTime": "2026-07-31T12:00:00Z",
           "aws:RequestedRegion": "us-east-1",
           "iam:PermissionsBoundary": gen.ARN["boundary"]}
    for outside in (
        f"arn:aws:iam::{gen.ACCOUNT}:role/{gen.PREFIX}-not-a-declared-role",
        f"arn:aws:iam::{gen.ACCOUNT}:role/some-unrelated-role",
        f"arn:aws:iam::{gen.ACCOUNT}:role/aws-reserved/sso.amazonaws.com/"
        f"{gen.REGION}/AWSReservedSSO_AdministratorAccess_abc123",
    ):
        iam_eval.require_explicit_deny(temp, "iam:PutRolePolicy", outside, ctx)


def test_the_stage_a_inline_policy_grant_requires_the_reviewed_boundary():
    """Without the boundary condition key present and correct, no write."""
    import gen_operator_policies as gen

    temp = gen.bootstrap_temp_policy(_ea.ACTIVE_EXPIRY_UTC)
    base = {"aws:CurrentTime": "2026-07-31T12:00:00Z", "aws:RequestedRegion": "us-east-1"}
    target = gen.INLINE_POLICY_ROLE_ARNS[0]
    assert iam_eval.decide(temp, "iam:PutRolePolicy", target, base).decision         is not iam_eval.Decision.EXPLICIT_ALLOW
    wrong = dict(base, **{"iam:PermissionsBoundary":
                          f"arn:aws:iam::{gen.ACCOUNT}:policy/some-other-boundary"})
    assert iam_eval.decide(temp, "iam:PutRolePolicy", target, wrong).decision         is not iam_eval.Decision.EXPLICIT_ALLOW
