"""Contract tests for the generated operator permission-set policies (Gate 4N-I4).

Three independent sources meet here, and that separation is the point:

  1. scripts/gen_operator_policies.py   produces the policies
  2. infra/aws/operator-closure-contract.json  states what must be authorized
  3. scripts/iam_eval.py                evaluates, including Condition

Gate 4N-I3's suite failed because (1) and (2) were the same Python dict — a defect moved
the policy and its expectation together, so the test could not fail — and because its
evaluator ignored `Condition` entirely, letting an impossible permissions-boundary ARN
pass. Both are fixed and pinned by explicit negative tests below.

No AWS access, no network, no tofu.
"""

from __future__ import annotations

import copy
import datetime as _datetime
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import gen_operator_policies as gen  # noqa: E402
import gen_role_bootstrap_policy as rb  # noqa: E402
import expiry_authorization as _ea  # noqa: E402
import iam_eval  # noqa: E402
import signalnest_identity as _identity  # noqa: E402

CONTRACT_PATH = REPO_ROOT / "infra" / "aws" / "operator-closure-contract.json"

# Request contexts. The permanent policy's only condition is the region; the temporary
# policy adds an expiry and the boundary. Supplying these explicitly is what makes the
# condition tests meaningful — an evaluator that ignored them would pass either way.
PERM_CTX = {"aws:RequestedRegion": gen.REGION}
TEMP_EXPIRY = _ea.ACTIVE_EXPIRY_UTC
TEMP_CTX = {
    "aws:RequestedRegion": gen.REGION,
    "aws:CurrentTime": "2026-07-31T12:00:00Z",
    "iam:PermissionsBoundary": gen.ARN["boundary"],
}


@pytest.fixture(scope="module")
def contract() -> dict:
    return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def permanent() -> dict:
    return gen.permanent_w0_policy()


@pytest.fixture(scope="module")
def temporary() -> dict:
    return gen.bootstrap_temp_policy(TEMP_EXPIRY)


def allowed(policy, action, resource, ctx):
    """Positive check only: is this capability actually GRANTED?

    Built on decide() rather than the legacy effect() string API, so it cannot be
    accidentally inverted into a safety assertion — `not allowed(...)` was the source of
    all 20 vacuous assertions this file carried before Gate 4N-I7. For a safety Deny use
    iam_eval.require_explicit_deny(...); for an absence check assert the exact Decision.
    """
    return iam_eval.decide(policy, action, resource, ctx).decision \
        is iam_eval.Decision.EXPLICIT_ALLOW


# --- the evaluator itself must be trustworthy ---------------------------------------


def test_evaluator_is_condition_aware():
    """The Gate 4N-I3 evaluator ignored Condition. This pins that it no longer does."""
    policy = {
        "Version": "2012-10-17",
        "Statement": [{
            "Sid": "X", "Effect": "Allow", "Action": "iam:CreateRole", "Resource": "*",
            "Condition": {"StringEquals": {"iam:PermissionsBoundary": "arn:aws:iam::1:policy/right"}},
        }],
    }
    assert allowed(policy, "iam:CreateRole", "*", {"iam:PermissionsBoundary": "arn:aws:iam::1:policy/right"})
    assert iam_eval.decide(policy, "iam:CreateRole", "*",
                           {"iam:PermissionsBoundary": "arn:aws:iam::1:policy/WRONG"}
                           ).decision is iam_eval.Decision.IMPLICIT_DENY
    assert iam_eval.decide(policy, "iam:CreateRole", "*", {}).decision \
        is iam_eval.Decision.MISSING_CONTEXT, "absent context key must fail closed"


def test_evaluator_fails_closed_on_unmodelled_features():
    for stmt in (
        {"Effect": "Allow", "Action": "s3:GetObject", "Resource": "*", "Condition": {"IpAddress": {"aws:SourceIp": "1.2.3.4"}}},
        {"Effect": "Allow", "Action": "s3:GetObject", "Resource": "*", "Condition": {"StringEqualsIfExists": {"aws:PrincipalAccount": "1"}}},
        {"Effect": "Allow", "Action": "s3:GetObject", "Resource": "*", "Principal": {"AWS": "*"}},
    ):
        # decide() reports the unmodelled feature as a DECISION rather than raising, and
        # UNSUPPORTED_SEMANTICS is fail-closed in exactly the same way: it is not
        # EXPLICIT_ALLOW, so nothing can be authorised on the strength of a construct the
        # evaluator does not understand.
        result = iam_eval.decide({"Version": "2012-10-17", "Statement": [stmt]},
                                 "s3:GetObject", "*", {})
        assert result.decision is iam_eval.Decision.UNSUPPORTED_SEMANTICS, stmt
        assert result.decision is not iam_eval.Decision.EXPLICIT_ALLOW


def test_evaluator_honours_deny_precedence_and_notresource():
    policy = {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": "s3:GetObject", "Resource": "*"},
        {"Effect": "Deny", "Action": "s3:GetObject", "NotResource": "arn:aws:s3:::keep/*"},
    ]}
    assert allowed(policy, "s3:GetObject", "arn:aws:s3:::keep/x", {})
    # EXPLICIT_DENY, not implicit: a NotResource Deny matches everything OUTSIDE the listed
    # resources, so it is an active denial. Getting this distinction wrong in either
    # direction is exactly what the Gate 4N-I7 assertion audit was about.
    iam_eval.require_explicit_deny(policy, "s3:GetObject", "arn:aws:s3:::other/x", {})


def test_evaluator_rejects_expiring_deny_and_dead_condition_keys():
    problems = iam_eval.validate_policy({"Version": "2012-10-17", "Statement": [
        {"Sid": "DeadKey", "Effect": "Allow", "Action": "iam:TagRole", "Resource": "*",
         "Condition": {"StringEquals": {"iam:PermissionsBoundary": "arn:aws:iam::1:policy/b"}}},
        {"Sid": "ExpiringDeny", "Effect": "Deny", "Action": "iam:PassRole", "Resource": "*",
         "Condition": {"DateLessThan": {"aws:CurrentTime": "2026-01-01T00:00:00Z"}}},
    ]})
    assert any("does not support condition key" in p for p in problems)
    assert any("Deny carries a date condition" in p for p in problems)


def test_generated_policies_are_structurally_valid(permanent, temporary):
    assert iam_eval.validate_policy(permanent) == []
    assert iam_eval.validate_policy(temporary) == []


# --- exact names, verified against module source ------------------------------------


def _module_text(rel: str) -> str:
    return (REPO_ROOT / "infra" / "aws" / rel).read_text(encoding="utf-8")


@pytest.mark.parametrize("key,expr", [
    ("reader_publisher", '"${var.name_prefix}-revision-reader-publisher"'),
    ("reader_execution", '"${var.name_prefix}-revision-reader-execution"'),
    ("reader_runner", '"${var.name_prefix}-revision-reader-runner"'),
])
def test_reader_role_names_match_repository(key, expr):
    assert expr in _module_text("modules/revision_reader/iam.tf")
    assert gen.NAMES[key].startswith(f"{gen.PREFIX}-revision-reader-")


def test_reader_ecr_repository_uses_a_slash():
    assert 'repo_name = "${var.name_prefix}/revision-reader"' in _module_text("modules/revision_reader/main.tf")
    assert gen.NAMES["reader_ecr_repo"] == f"{gen.PREFIX}/revision-reader"


def test_every_iam_role_sets_a_permissions_boundary():
    result = subprocess.run([sys.executable, str(REPO_ROOT / "scripts" / "check-iam-role-boundaries.py")],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


# --- permanent W0: forbidden capabilities -------------------------------------------


# INFRA-9 B-3 (2026-08-16): permanent W0 is the APPLY IDENTITY. Four capability groups are
# carved out of the flat DenyDangerous ceiling and re-denied by NotResource fences, so the
# carved rows below assert the FENCE Sid at an out-of-scope resource. The in-scope allows
# are asserted positively further down.
@pytest.mark.parametrize("action,resource,sid", [
    ("s3:GetObject", gen.ARN["audit_bucket"] + "/AWSLogs/x", "DenyStateObjectAccessOutsideTheStateObject"),
    ("s3:PutObject", gen.ARN["audit_bucket"] + "/AWSLogs/x", "DenyStateObjectAccessOutsideTheStateObject"),
    ("s3:GetObjectVersion", gen.ARN["state_object"], "DenyDangerous"),
    ("dynamodb:PutItem", f"arn:aws:dynamodb:{gen.REGION}:{gen.ACCOUNT}:table/other", "DenyLockItemsOutsideTheLockTable"),
    ("dynamodb:DeleteItem", f"arn:aws:dynamodb:{gen.REGION}:{gen.ACCOUNT}:table/other", "DenyLockItemsOutsideTheLockTable"),
    ("kms:Decrypt", gen.ARN["cmk_secrets"], "DenyStateCmkUseOutsideTheStateCmk"),
    ("iam:CreateRole", gen.READER_ROLE_ARNS[0], "DenyDangerous"),
    ("iam:PutRolePolicy", gen.READER_ROLE_ARNS[0], "DenyDangerous"),
    ("iam:TagRole", gen.READER_ROLE_ARNS[0], "DenyDangerous"),
    ("iam:PutRolePermissionsBoundary", gen.READER_ROLE_ARNS[0], "DenyDangerous"),
    ("iam:PassRole", "*", "DenyDangerous"),
    ("cloudtrail:StopLogging", gen.ARN["trail"], "DenyDangerous"),
    ("cloudtrail:DeleteTrail", gen.ARN["trail"], "DenyDangerous"),
    ("s3:PutBucketPolicy", gen.ARN["audit_bucket"], "DenyDangerous"),
    ("s3:PutBucketPolicy", gen.ARN["state_bucket"], "DenyDangerous"),
    ("rds:ModifyDBInstance", gen.ARN["db"], "DenyDangerous"),
    ("secretsmanager:GetSecretValue", "*", "DenyDangerous"),
    ("kms:ScheduleKeyDeletion", gen.ARN["cmk_secrets"], "DenyDangerous"),
    ("kms:CreateGrant", gen.ARN["cmk_secrets"], "DenyDangerous"),
    # the capability set the entire 4N-H saga was about. RegisterTaskDefinition is now
    # FENCED (the B-3 carve-out); RunTask/CreateService/friends stay flatly denied.
    ("ecs:RegisterTaskDefinition", "*", "DenyTaskDefinitionRegistrationOutsideTheFamilies"),
    ("ecs:RegisterTaskDefinition",
     f"arn:aws:ecs:{gen.REGION}:{gen.ACCOUNT}:task-definition/{gen.PREFIX}-evil",
     "DenyTaskDefinitionRegistrationOutsideTheFamilies"),
    ("ecs:CreateService", "*", "DenyDangerous"),
    ("ecs:RunTask", "*", "DenyDangerous"),
])
def test_permanent_w0_denies_dangerous_actions(permanent, action, resource, sid):
    """EXPLICIT Deny, not merely 'not allowed'.

    Gate 4N-I6 left this as `not allowed(...)`, which IMPLICIT_DENY satisfies — removing
    iam:PassRole from the Deny left this file at 57/57 passed. It now requires the real
    control to be present.
    """
    iam_eval.require_explicit_deny(permanent, action, resource, PERM_CTX, sid=sid)


def test_permanent_w0_deny_is_unconditional(permanent):
    """INFRA-9 B-3 re-authoring (the ownership sweep's HAZARD 2): every Deny is still
    UNCONDITIONAL, and every Deny is either the flat Resource-"*" ceiling or one of the four
    NotResource fences whose carve is asserted statement-by-statement in
    tests/test_explicit_deny.py::test_every_fence_is_unconditional_and_mirrors_its_allow_scope."""
    for stmt in [s for s in permanent["Statement"] if s["Effect"] == "Deny"]:
        assert "Condition" not in stmt, stmt.get("Sid")
        assert stmt.get("Resource") == "*" or "NotResource" in stmt, stmt.get("Sid")


# --- permanent W0: the B-3 apply surface (positive assertions) ----------------------


W0_CMK_CTX_S3 = dict(PERM_CTX, **{"kms:ViaService": f"s3.{gen.REGION}.amazonaws.com"})
W0_CMK_CTX_DDB = dict(PERM_CTX, **{"kms:ViaService": f"dynamodb.{gen.REGION}.amazonaws.com"})


def test_permanent_w0_has_the_state_backend_closure(permanent, contract):
    """INFRA-9 B-3: W0 is the apply identity, so the SAME contract section the temporary
    operator is measured against now measures W0 — kms actions under the ViaService context
    the statement requires (S3/DynamoDB calling KMS on the operator's behalf)."""
    sb = contract["state_backend_closure"]
    for action in sb["read"] + sb["write_apply_only"]:
        resource = gen.ARN["state_object"] if "Object" in action else gen.ARN["state_bucket"]
        assert allowed(permanent, action, resource, PERM_CTX), action
    for action in sb["lock"]:
        assert allowed(permanent, action, gen.ARN["lock"], PERM_CTX), action
    for action in sb["cmk"]:
        assert allowed(permanent, action, gen.ARN["cmk_state"], W0_CMK_CTX_S3), action
        assert allowed(permanent, action, gen.ARN["cmk_state"], W0_CMK_CTX_DDB), action


def test_permanent_w0_covers_the_stage_b_task_definition_closure(permanent, contract):
    stage_b = contract["stage_b_task_definition_closure"]
    for action in stage_b["register"]:
        for arn in gen.TASK_DEFINITION_FAMILY_ARNS:
            # probe a CONCRETE revision — the pattern matching itself by string identity
            # would not exercise the match (architect-lane finding 10)
            concrete = arn[:-1] + "1"
            assert allowed(permanent, action, concrete, PERM_CTX), f"{action} on {concrete}"
            assert allowed(permanent, action, arn, PERM_CTX), f"{action} on {arn}"
    for action in stage_b["describe_star"]:
        assert allowed(permanent, action, "*", PERM_CTX), action
    assert "iam:PassRole" not in json.dumps(
        [s for s in permanent["Statement"] if s["Effect"] == "Allow"]), (
        "the B-3 delta must add NO PassRole surface")


def test_state_cmk_use_is_dead_without_the_backend_via_service(permanent):
    """The ViaService condition is load-bearing: a DIRECT kms call by the operator's own
    credentials (context lacking or naming another service) must never reach an Allow.

    EXACT decisions, consumed (permissions-lane finding 5): an absent key is MISSING_CONTEXT;
    a wrong-service value fails the condition and, the fence not matching at the state CMK,
    lands on IMPLICIT_DENY — an EXPLICIT deny off the state CMK is asserted separately."""
    for ctx, want in ((PERM_CTX, iam_eval.Decision.MISSING_CONTEXT),
                      (dict(PERM_CTX, **{"kms:ViaService": f"lambda.{gen.REGION}.amazonaws.com"}),
                       iam_eval.Decision.IMPLICIT_DENY)):
        got = iam_eval.decide(permanent, "kms:GenerateDataKey", gen.ARN["cmk_state"], ctx).decision
        assert got is want, (ctx, got)
    # off the state CMK, BOTH cmk-use actions stay EXPLICITLY denied whatever the context
    for action in ("kms:Decrypt", "kms:GenerateDataKey"):
        iam_eval.require_explicit_deny(permanent, action, gen.ARN["cmk_secrets"],
                                       W0_CMK_CTX_S3, sid="DenyStateCmkUseOutsideTheStateCmk")


def test_the_scoped_capability_set_is_exactly_the_forbidden_subset_of_the_apply_closure():
    """PROVENANCE_CORRESPONDENCE, implemented (architect-lane finding 3): the carve-out set
    must equal flatten(W0_APPLY_CLOSURE) ∩ the flat-ceiling union — a member with no paired
    Allow, or an apply-closure forbidden action left un-carved, both fail here."""
    from must_not_contract import FORBIDDEN_CAPABILITIES
    flat_union = set(gen.PERMANENT_DENY) | set(FORBIDDEN_CAPABILITIES)
    apply_actions = {a for group in gen.W0_APPLY_CLOSURE.values() for a in group}
    assert gen.W0_SCOPED_CAPABILITIES == apply_actions & flat_union


def test_task_definition_families_match_the_composition():
    """The three workload families are pinned to the module source (the reader family is
    imported from signalnest_identity, exactly as the reader role names are)."""
    ecs_module = _module_text("modules/ecs/main.tf")
    for family_expr in ('"${var.name_prefix}-api"', '"${var.name_prefix}-worker"',
                       '"${var.name_prefix}-migration"'):
        assert f"family                   = {family_expr}" in ecs_module, family_expr
    assert 'family    = "${var.name_prefix}-revision-reader"' in _module_text(
        "modules/revision_reader/main.tf")
    families = {arn.rsplit("/", 1)[-1].split(":")[0] for arn in gen.TASK_DEFINITION_FAMILY_ARNS}
    assert families == {f"{gen.PREFIX}-api", f"{gen.PREFIX}-worker",
                        f"{gen.PREFIX}-migration", f"{gen.PREFIX}-revision-reader"}
    # the family:* form ONLY — the Service Reference ARNFormats entry is revision-bearing and
    # this account's CloudTrail names the authorization resource in exactly that form
    # (retained: b3-part-a-live-readback/ecs-action-truth-evidence.md); a bare-family entry
    # never matches and must not reappear.
    assert len(gen.TASK_DEFINITION_FAMILY_ARNS) == 4
    assert all(arn.endswith(":*") for arn in gen.TASK_DEFINITION_FAMILY_ARNS)


# --- permanent W0 vs the INDEPENDENT contract ---------------------------------------


def test_permanent_w0_authorizes_every_action_in_the_independent_contract(permanent, contract):
    """Expectations come from operator-closure-contract.json, NOT from the generator."""
    missing = [
        f"{action} on {resource}"
        for action, resource in _contract_probes(contract)
        if iam_eval.decide(permanent, action, resource, PERM_CTX).decision
        is not iam_eval.Decision.EXPLICIT_ALLOW
    ]
    assert not missing, "closure actions not authorized: " + ", ".join(missing)


def _contract_probes(contract: dict):
    c = contract["refresh_closure"]
    for a in c["resource_star_regional"] + c["rds_star"] + c["cloudtrail_read_star"]:
        yield a, "*"
    for a in c["rds_exact"]:
        yield a, gen.ARN["pg"]
    for a in c["s3_bucket"]:
        yield a, gen.ARN["audit_bucket"]
    for a in c["kms_exact"]:
        yield a, gen.ARN["cmk_secrets"]
    for a in c["secrets"]:
        yield a, f"arn:aws:secretsmanager:{gen.REGION}:{gen.ACCOUNT}:secret:{gen.PREFIX}/DATABASE_URL-AbCdEf"
    for a in c["iam_read"]:
        yield a, f"arn:aws:iam::{gen.ACCOUNT}:role/{gen.PREFIX}-ecs-execution"
    for a in c["route53"]:
        # GATE 4N-I27Z. This used to repeat the generator's own hosted-zone literal, so the
        # test confirmed only that two hand-typed strings matched. It now probes the ARN the
        # generator actually resolves; the zone's SHAPE and PROVENANCE are asserted separately
        # in test_the_hosted_zone_id_is_tier_resolved_and_not_a_repository_literal below.
        yield a, _identity.route53_hosted_zone_arn()
    for a in c["cloudfront_read"]:
        yield a, gen.ARN["distribution"]
    for a in c["cloudtrail_read_exact"]:
        yield a, gen.ARN["trail"]
    for a in c["budgets"]:
        yield a, f"arn:aws:budgets::{gen.ACCOUNT}:budget/{gen.PREFIX}-monthly"


def test_s3_list_tags_for_resource_is_granted(permanent, temporary, contract):
    """Denied four times in the very run Gate 4N-I3 derived from, and omitted there."""
    entry = next(d for d in contract["historical_denials_classified"]
                 if d["action"] == "s3:ListTagsForResource")
    assert entry["required"] and entry["classification"] == "OPTIONAL_SOFT_FAIL"
    assert allowed(permanent, "s3:ListTagsForResource", gen.ARN["audit_bucket"], PERM_CTX)
    assert allowed(temporary, "s3:ListTagsForResource", gen.ARN["audit_bucket"], TEMP_CTX)


def test_every_historical_denial_is_classified(contract):
    valid = {"REQUIRED_MISSING", "OPTIONAL_SOFT_FAIL", "OBSOLETE_PATH", "UNRELATED", "UNKNOWN"}
    entries = contract["historical_denials_classified"]
    assert entries
    for entry in entries:
        assert entry["classification"] in valid
        assert entry.get("evidence")


# --- temporary operator --------------------------------------------------------------


def test_temporary_operator_is_standalone_over_the_contract(temporary, contract):
    missing = [f"{a} on {r}" for a, r in _contract_probes(contract)
               if iam_eval.decide(temporary, a, r, TEMP_CTX).decision
               is not iam_eval.Decision.EXPLICIT_ALLOW]
    assert not missing, "standalone reads missing: " + ", ".join(missing)
    assert allowed(temporary, "sts:GetCallerIdentity", "*", TEMP_CTX), "backend init calls this first"


def test_temporary_operator_covers_the_whole_stage_a_closure(temporary, contract):
    stage = contract["stage_a_create_closure"]
    for action in stage["ecr_create"] + stage["ecr_read_after_create"]:
        assert allowed(temporary, action, gen.ARN["reader_ecr"], TEMP_CTX), action
    # GATE 4N-I9: stage_a_create_closure no longer has an iam_create group. Role authoring
    # moved to role_bootstrap_closure and to a separate principal, because iam:CreateRole
    # accepts the trust document and AWS cannot condition it.
    assert "iam_create" not in stage, (
        "stage_a_create_closure must not reclaim role creation; that is Defect 1")
    for action in stage["iam_read_after_create"]:
        for arn in gen.READER_ROLE_ARNS:
            assert allowed(temporary, action, arn, TEMP_CTX), f"{action} on {arn}"


def test_temporary_operator_has_the_state_backend_closure(temporary, contract):
    sb = contract["state_backend_closure"]
    for action in sb["read"] + sb["write_apply_only"]:
        resource = gen.ARN["state_object"] if "Object" in action else gen.ARN["state_bucket"]
        assert allowed(temporary, action, resource, TEMP_CTX), action
    for action in sb["lock"]:
        assert allowed(temporary, action, gen.ARN["lock"], TEMP_CTX), action
    for action in sb["cmk"]:
        assert allowed(temporary, action, gen.ARN["cmk_state"], TEMP_CTX), action


def test_temporary_operator_has_no_audit_bucket_or_trail_mutation(temporary, contract):
    """Removed in Gate 4N-I4: live evidence shows the audit surface is already converged.

    These are ABSENCE assertions, not safety Denies: the actions are simply not granted.
    IMPLICIT_DENY is the correct and expected outcome, so it is asserted by name rather
    than through a boolean helper that would hide the distinction.
    """
    assert "audit_bucket_mutation" in contract["explicitly_not_required"]

    # Each action is asserted at its EXACT decision. Accepting "anything that is not an
    # Allow" is what made the Gate 4N-I6 assertions vacuous, so the distinction between a
    # capability that is merely absent and one the ceiling actively removes is spelled out
    # rather than collapsed.
    #
    # s3:PutBucketPolicy moved from absent to explicitly denied in Gate 4N-I7, when the
    # ceiling began deriving from scripts/must_not_contract.py — the contract forbids it
    # because it reaches the state and audit buckets.
    expected = {
        ("s3:PutBucketPolicy", gen.ARN["audit_bucket"]): iam_eval.Decision.EXPLICIT_DENY,
        # Gate 4N-I11: moved from absent to EXPLICITLY denied when the expanded requirement
        # set added the audit-delivery tampering class. Recorded at its new exact decision
        # rather than loosened to "not allowed".
        ("s3:PutBucketPublicAccessBlock", gen.ARN["audit_bucket"]): iam_eval.Decision.EXPLICIT_DENY,
        ("cloudtrail:CreateTrail", gen.ARN["trail"]): iam_eval.Decision.IMPLICIT_DENY,
        ("cloudtrail:StartLogging", gen.ARN["trail"]): iam_eval.Decision.IMPLICIT_DENY,
    }
    for (action, resource), want in expected.items():
        got = iam_eval.decide(temporary, action, resource, TEMP_CTX).decision
        assert got is want, f"{action} on {resource}: expected {want.name}, got {got.name}"


def test_temporary_operator_cannot_administer_the_boundary(temporary):
    """Boundary creation and attachment belong to a SEPARATE executor."""
    for action in ("iam:CreatePolicy", "iam:PutRolePermissionsBoundary"):
        iam_eval.require_explicit_deny(temporary, action, "*", TEMP_CTX, sid="TempDenyEscalation")


def test_temporary_operator_cannot_exceed_its_purpose(temporary):
    for action in ("iam:PassRole", "iam:DeleteRole", "ecs:RegisterTaskDefinition",
                   "secretsmanager:GetSecretValue", "cloudtrail:StopLogging", "sts:AssumeRole"):
        iam_eval.require_explicit_deny(temporary, action, "*", TEMP_CTX)


def test_temporary_allows_expire_and_denies_do_not(temporary):
    for stmt in temporary["Statement"]:
        has_expiry = "DateLessThan" in (stmt.get("Condition") or {})
        assert has_expiry == (stmt["Effect"] == "Allow"), stmt["Sid"]


def test_expired_credentials_authorize_nothing(temporary):
    expired = dict(TEMP_CTX, **{"aws:CurrentTime": "2026-09-01T00:00:00Z"})
    assert iam_eval.decide(temporary, "iam:CreateRole", gen.READER_ROLE_ARNS[0], expired).decision \
        is not iam_eval.Decision.EXPLICIT_ALLOW
    assert iam_eval.decide(temporary, "s3:PutObject", gen.ARN["state_object"], expired).decision \
        is not iam_eval.Decision.EXPLICIT_ALLOW


# --- Phase E: condition negative tests ----------------------------------------------


def _mutate(policy: dict, sid: str, fn) -> dict:
    out = copy.deepcopy(policy)
    for stmt in out["Statement"]:
        if stmt.get("Sid") == sid:
            fn(stmt)
    return out


def test_wrong_permissions_boundary_arn_is_rejected():
    """THE Gate 4N-I2 defect. The I3 evaluator returned success on this.

    Moved to the role bootstrap operator in Gate 4N-I9 with the CreateRole capability.
    """
    temporary = rb.role_bootstrap_policy(_ea.ACTIVE_EXPIRY_UTC)
    broken = _mutate(temporary, "CreateExactlyTheThreeReaderRolesBounded",
                     lambda s: s["Condition"]["StringEquals"].__setitem__(
                         "iam:PermissionsBoundary", "arn:aws:iam::111122223333:policy/NEVER-EXISTS"))
    assert iam_eval.decide(broken, "iam:CreateRole", rb.TARGET_ROLE_ARNS[0], TEMP_CTX).decision \
        is not iam_eval.Decision.EXPLICIT_ALLOW


def test_missing_permissions_boundary_condition_is_detected():
    """Moved to the role bootstrap operator in Gate 4N-I9 along with the capability."""
    import gen_role_bootstrap_policy as rb

    policy = rb.role_bootstrap_policy(_ea.ACTIVE_EXPIRY_UTC)
    broken = _mutate(policy, "CreateExactlyTheThreeReaderRolesBounded",
                     lambda s: s.pop("Condition", None))
    assert allowed(broken, "iam:CreateRole", rb.TARGET_ROLE_ARNS[0], {}), (
        "without the condition the grant becomes unconditional — which is the risk"
    )
    assert "iam:PermissionsBoundary" in json.dumps(policy), "the real policy must carry it"


def test_wrong_condition_operator_is_rejected():
    import gen_role_bootstrap_policy as rb

    temporary = rb.role_bootstrap_policy(_ea.ACTIVE_EXPIRY_UTC)
    broken = _mutate(temporary, "CreateExactlyTheThreeReaderRolesBounded",
                     lambda s: s.__setitem__("Condition", {
                         "DateLessThan": s["Condition"]["DateLessThan"],
                         "StringNotEquals": {"iam:PermissionsBoundary": gen.ARN["boundary"]}}))
    assert iam_eval.decide(broken, "iam:CreateRole", rb.TARGET_ROLE_ARNS[0], TEMP_CTX).decision \
        is not iam_eval.Decision.EXPLICIT_ALLOW


def test_unsupported_condition_key_on_tagrole_is_reported():
    """iam:TagRole moved to the role bootstrap operator with the rest of role authoring.

    GATE 4N-I11: retargeted to the TagRole statement. Through Gate 4N-I10 this test mutated
    `CreateExactlyTheThreeReaderRolesBounded`, which then held BOTH CreateRole and TagRole
    under one boundary condition — so the "broken" mutant it constructed was byte-identical
    to the shipped statement, and the suite proved the detector worked while shipping the
    thing it detects. The statements are now split, so the mutant is genuinely a mutant.
    """
    import gen_role_bootstrap_policy as rb

    temporary = rb.role_bootstrap_policy(_ea.ACTIVE_EXPIRY_UTC)
    broken = _mutate(temporary, "TagExactlyTheThreeReaderRolesWithApprovedKeysOnly",
                     lambda s: s.__setitem__("Condition", dict(
                         s.get("Condition", {}),
                         **{"StringEquals": {"iam:PermissionsBoundary": gen.ARN["boundary"]}})))
    problems = iam_eval.validate_policy(broken)
    assert any("iam:TagRole does not support condition key" in p for p in problems)


def test_wrong_region_condition_is_rejected(permanent):
    assert iam_eval.decide(permanent, "ec2:DescribeVpcs", "*",
                           {"aws:RequestedRegion": "eu-west-3"}).decision \
        is not iam_eval.Decision.EXPLICIT_ALLOW


def test_wrong_expiry_key_is_rejected(temporary):
    broken = _mutate(temporary, "TempStateObject",
                     lambda s: s.__setitem__("Condition", {"DateLessThan": {"aws:TokenIssueTime": TEMP_EXPIRY}}))
    assert iam_eval.decide(broken, "s3:PutObject", gen.ARN["state_object"], TEMP_CTX).decision \
        is not iam_eval.Decision.EXPLICIT_ALLOW


def test_notresource_widening_is_detected():
    tight = {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": "s3:GetObject", "Resource": "arn:aws:s3:::b/exact"},
        {"Effect": "Deny", "Action": "s3:GetObject", "NotResource": "arn:aws:s3:::b/exact"}]}
    widened = {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": "s3:GetObject", "Resource": "arn:aws:s3:::b/*"},
        {"Effect": "Deny", "Action": "s3:GetObject", "NotResource": "arn:aws:s3:::b/*"}]}
    assert not iam_eval.is_allowed(tight, "s3:GetObject", "arn:aws:s3:::b/other", {})
    assert iam_eval.is_allowed(widened, "s3:GetObject", "arn:aws:s3:::b/other", {})


def test_explicit_deny_conflict_is_detected(permanent):
    """Adding an Allow for a denied action must NOT make it allowed."""
    broken = copy.deepcopy(permanent)
    broken["Statement"].insert(0, {"Sid": "Sneak", "Effect": "Allow",
                                   "Action": "iam:CreateRole", "Resource": "*"})
    assert iam_eval.decide(broken, "iam:CreateRole", gen.READER_ROLE_ARNS[0], PERM_CTX).decision \
        is not iam_eval.Decision.EXPLICIT_ALLOW


# --- Phase N: closure independence proofs -------------------------------------------


def test_contract_is_not_produced_by_the_generator():
    source = (REPO_ROOT / "scripts" / "gen_operator_policies.py").read_text(encoding="utf-8")
    # A prose mention in the module docstring is fine and in fact desirable; what must
    # never exist is real file ACCESS, which would couple the two sources again.
    code = "\n".join(
        line for line in source.splitlines()
        if "operator-closure-contract" not in line or "#" in line
    )
    for forbidden in ("open(", "read_text", "json.load", "Path("):
        for line in source.splitlines():
            if "operator-closure-contract" in line and forbidden in line:
                pytest.fail(f"generator accesses the contract file: {line.strip()}")
    assert "import json" in source  # it serialises policies, but never reads the contract
    assert CONTRACT_PATH.exists()
    assert code


def test_changing_the_generator_while_the_contract_stands_fails(contract):
    """Drop a required read from the generator: the contract test must catch it."""
    reduced = copy.deepcopy(gen.permanent_w0_policy())
    for stmt in reduced["Statement"]:
        if stmt["Sid"] == "EstateReadRegional":
            stmt["Action"] = [a for a in stmt["Action"] if a != "ec2:DescribeVpcAttribute"]
    missing = [a for a, r in _contract_probes(contract) if not allowed(reduced, a, r, PERM_CTX)]
    assert "ec2:DescribeVpcAttribute" in missing


def test_changing_the_contract_while_the_generator_stands_fails(contract, permanent):
    """Add a requirement the policy does not satisfy: it must surface as missing."""
    widened = copy.deepcopy(contract)
    widened["refresh_closure"]["resource_star_regional"].append("ec2:DescribeFlowLogs")
    missing = [a for a, r in _contract_probes(widened) if not allowed(permanent, a, r, PERM_CTX)]
    assert "ec2:DescribeFlowLogs" in missing


def test_removing_a_condition_from_the_generated_policy_is_detected(temporary):
    broken = _mutate(temporary, "TempStateObject", lambda s: s.pop("Condition", None))
    expired = dict(TEMP_CTX, **{"aws:CurrentTime": "2026-09-01T00:00:00Z"})
    assert allowed(broken, "s3:PutObject", gen.ARN["state_object"], expired), (
        "with the expiry removed the grant survives expiry — the defect this detects"
    )
    assert iam_eval.decide(temporary, "s3:PutObject", gen.ARN["state_object"], expired).decision \
        is not iam_eval.Decision.EXPLICIT_ALLOW


def test_generation_is_deterministic():
    assert gen.canonical(gen.permanent_w0_policy()) == gen.canonical(gen.permanent_w0_policy())
    # Two REAL expiries, not "X" and "Y": Gate 4N-I8 makes a malformed expiry a generation
    # failure, so the old placeholders would now raise before the comparison happened.
    # GATE 4N-I28R: derived from the reviewed issuance, not hand-written. A restamp used to
    # leave this literal behind the new issuance, making `early` an already-expired window and
    # turning a determinism test into a generation refusal.
    early = (_datetime.datetime.strptime(_ea.ACTIVE_ISSUANCE_UTC, "%Y-%m-%dT%H:%M:%SZ")
             + _datetime.timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M:%SZ")
    late = _ea.ACTIVE_EXPIRY_UTC
    assert gen.canonical(gen.bootstrap_temp_policy(early)) == \
        gen.canonical(gen.bootstrap_temp_policy(early)), "same expiry must produce same bytes"
    assert gen.canonical(gen.bootstrap_temp_policy(early)) != \
        gen.canonical(gen.bootstrap_temp_policy(late)), "the expiry must reach the bytes"


def test_a_placeholder_or_malformed_expiry_cannot_reach_a_generated_artifact():
    """Gate 4N-I8 Defect 3, closed at the earliest possible point.

    Both reviewed Gate 4N-I7 artifacts were hashed carrying the literal `<EXPIRY-ISO8601>`.
    Rejecting it at GENERATION means it can never reach a hashed artifact at all, which is
    strictly stronger than detecting it downstream.
    """
    for bad in (None, "", "<EXPIRY-ISO8601>", "not-a-date", "2026-01-01T00:00:00", 12345):
        with pytest.raises((ValueError, iam_eval.UnsupportedPolicyFeature)):
            gen.bootstrap_temp_policy(bad)


def test_permanent_and_temporary_must_be_separate_principals(permanent, temporary):
    # INFRA-9 B-3: the state write is now legitimately held by BOTH principals (W0 became
    # the apply identity), so the separation is demonstrated on inline-policy authoring —
    # the temporary operator holds it boundary-conditioned on the declared roles, and
    # permanent W0 denies it flatly.
    for action, resource in (("iam:PutRolePolicy", gen.INLINE_POLICY_ROLE_ARNS[0]),):
        assert allowed(temporary, action, resource, TEMP_CTX)
        iam_eval.require_explicit_deny(permanent, action, resource, PERM_CTX)


# =====================================================================================
# GATE 4N-I27Z, AGENDA D. Hosted-zone provenance.
#
# Gate 4N-I27Y's aws-permissions lane found the Route53 hosted-zone id hardcoded in the
# generator, the verifier AND this test. Three consumers restating one hand-typed literal agree
# only with each other — self-attestation, not provenance — and no containment control could see
# it: leak_scan matches 12-digit accounts, 32+ hex runs, UUIDs and AKIA keys, none of which a
# `Z`+20-alphanumeric zone id resembles, and the ARN carries no account segment.
#
# These tests assert SHAPE and PROVENANCE. They deliberately do NOT contain the identifier.
# =====================================================================================

def test_the_hosted_zone_id_is_tier_resolved_and_not_a_repository_literal():
    """The id must come from the inventory, exactly as the CloudFront ids do."""
    import protected_inventory

    resolved = _identity.ROUTE53_HOSTED_ZONE_ID
    inventory = protected_inventory.load()
    supplied, present = inventory.dig("route53.hosted_zone_id")
    assert present, "the resolved inventory carries no route53.hosted_zone_id"
    assert resolved == supplied, "the identity module is not reading the inventory value"


def test_the_hosted_zone_id_has_the_shape_aws_assigns():
    """Shape, not value: a test that repeated the value would be the defect again."""
    assert re.fullmatch(r"Z[A-Z0-9]{20}", _identity.ROUTE53_HOSTED_ZONE_ID), (
        "a hosted-zone id is `Z` followed by 20 uppercase alphanumerics")


def test_the_generator_and_verifier_resolve_the_zone_rather_than_restating_it():
    """No source file may carry a bare hosted-zone ARN literal.

    The point is not that today's value is wrong — it is that three consumers agreeing on a
    hand-typed string cannot distinguish a right value from a wrong one.
    """
    literal = re.compile(r"hostedzone/Z[A-Z0-9]{20}")
    offenders = []
    for path in [REPO_ROOT / "scripts" / "gen_operator_policies.py",
                 REPO_ROOT / "scripts" / "verify_closure.py",
                 Path(__file__)]:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if literal.search(line):
                offenders.append(f"{path.name}:{number}")
    assert not offenders, (
        "a hosted-zone ARN is hardcoded again at " + ", ".join(offenders) +
        "; resolve it through signalnest_identity.route53_hosted_zone_arn() instead")


def test_the_synthetic_zone_is_marked_synthetic():
    """The Tier-1 value must announce that it is invented, so it cannot be mistaken for real."""
    fixture = json.loads(
        (REPO_ROOT / "tests" / "fixtures" / "synthetic-inventory.json").read_text("utf-8"))
    assert "route53" in fixture, "the synthetic inventory has no route53 block"
    assert "_synthetic" in fixture["route53"], "the synthetic zone carries no synthetic marker"
    assert fixture["route53"]["hosted_zone_id"].startswith("ZSYNTH")


# =====================================================================================
# INFRA-9 B-3 Part-B remediation (2026-08-17): the ICPermAdmin provisioning delta.
#
# One statement, one action, one exact reserved-role resource, one condition — the
# OD-R1..OD-R5 adjudication. The tests below are the STRUCTURAL enforcement the
# adjudication substitutes for live negative probes (OD-R5: an identical-byte
# PutRolePolicy still alters attribution and is mutation-class, so nothing here ever
# touches AWS): every prohibited expansion — condition removal, aws:ViaAWSService
# substitution, action expansion, resource/suffix wildcarding, self-scope, other-role
# scope, a dropped permanent-assignment invariant — either fails generation, fails
# validate_policy, or is demonstrated as the exact risk the shipped shape refuses.
# =====================================================================================

_DELTA_SUFFIX = "0123456789abcdef"


def _w0_reserved_role_arn(suffix: str = _DELTA_SUFFIX, *, region_segment: bool = True,
                          ps_name: str | None = None, account: str | None = None) -> str:
    name = ps_name if ps_name is not None else gen._w0_permission_set_name()
    segment = f"{gen.REGION}/" if region_segment else ""
    return (f"arn:aws:iam::{account or gen.ACCOUNT}:role/aws-reserved/sso.amazonaws.com/"
            f"{segment}AWSReservedSSO_{name}_{suffix}")


_FAS_CTX = {"aws:CalledViaFirst": "sso.amazonaws.com"}


@pytest.fixture(scope="module")
def delta() -> dict:
    return gen.icpermadmin_provisioning_delta(_w0_reserved_role_arn())


def test_icpermadmin_delta_is_exactly_the_adjudicated_statement(delta):
    """Shape pin: one statement, one action, one exact resource, one condition, stable Sid."""
    assert delta["Version"] == "2012-10-17"
    assert len(delta["Statement"]) == 1
    stmt = delta["Statement"][0]
    assert stmt["Sid"] == gen.ICPERMADMIN_DELTA_SID == "IcProvisionW0ReservedRoleInlinePolicyWrite"
    assert stmt["Effect"] == "Allow"
    assert stmt["Action"] == ["iam:PutRolePolicy"], "OD-R1: single-action minimality"
    assert isinstance(stmt["Resource"], str), "OD-R2: one exact resource, never a list or pattern"
    assert stmt["Resource"] == _w0_reserved_role_arn()
    assert stmt["Condition"] == {
        "StringEquals": {"aws:CalledViaFirst": "sso.amazonaws.com"}}, (
        "OD-R3: exactly the CalledViaFirst condition — no ViaAWSService, no alternative "
        "service principal, no unconditioned form")
    assert iam_eval.validate_policy(delta) == []


def test_icpermadmin_delta_exact_fas_context_is_the_only_eligible_context(delta):
    """OD-R3 positive direction plus every near-miss, each at its EXACT decision."""
    arn = _w0_reserved_role_arn()
    assert allowed(delta, "iam:PutRolePolicy", arn, _FAS_CTX)
    near_misses = {
        # a different forwarding service is a value mismatch, not missing context
        ("wrong service", "cloudformation.amazonaws.com"): iam_eval.Decision.IMPLICIT_DENY,
        ("padded value", " sso.amazonaws.com"): iam_eval.Decision.IMPLICIT_DENY,
        ("uppercase value", "SSO.AMAZONAWS.COM"): iam_eval.Decision.IMPLICIT_DENY,
    }
    for (label, value), want in near_misses.items():
        got = iam_eval.decide(delta, "iam:PutRolePolicy", arn,
                              {"aws:CalledViaFirst": value}).decision
        assert got is want, (label, got)
    # aws:ViaAWSService alone — a FAS request the CalledVia key did not accompany — must
    # NOT satisfy the statement: the key the condition names is absent, so it fails closed.
    got = iam_eval.decide(delta, "iam:PutRolePolicy", arn,
                          {"aws:ViaAWSService": "true"}).decision
    assert got is iam_eval.Decision.MISSING_CONTEXT, got


def test_icpermadmin_delta_direct_call_context_remains_denied(delta):
    """OD-R3/OD-R5: a direct operator call carries no CalledVia key and fails closed."""
    arn = _w0_reserved_role_arn()
    assert iam_eval.decide(delta, "iam:PutRolePolicy", arn, {}).decision \
        is iam_eval.Decision.MISSING_CONTEXT
    # and the delta grants NOTHING on any other role, whatever the context
    for other in (gen.READER_ROLE_ARNS[0],
                  f"arn:aws:iam::{gen.ACCOUNT}:role/{gen.PREFIX}-ecs-execution",
                  _w0_reserved_role_arn(suffix="fedcba9876543210")):
        assert iam_eval.decide(delta, "iam:PutRolePolicy", other, _FAS_CTX).decision \
            is iam_eval.Decision.IMPLICIT_DENY, other


def test_condition_removal_would_make_the_grant_unconditional(delta):
    """The risk the shape pin refuses: without the condition, a direct call is allowed."""
    broken = _mutate(delta, gen.ICPERMADMIN_DELTA_SID, lambda s: s.pop("Condition", None))
    assert allowed(broken, "iam:PutRolePolicy", _w0_reserved_role_arn(), {}), (
        "with the condition removed the grant becomes a standing direct-write capability — "
        "which is exactly what OD-R3 exists to prevent")
    assert "Condition" in delta["Statement"][0], "the shipped delta must carry it"


def test_via_aws_service_substitution_is_flagged_as_a_dead_grant(delta):
    """OD-R3: aws:ViaAWSService is not implemented, and a substituted mutant must not
    validate clean — the dead-grant detector (ACTION_CONDITION_KEYS) refuses it, so the
    substitution requires the separately reviewed delta the adjudication demands."""
    broken = _mutate(delta, gen.ICPERMADMIN_DELTA_SID,
                     lambda s: s.__setitem__("Condition",
                                             {"Bool": {"aws:ViaAWSService": "true"}}))
    problems = iam_eval.validate_policy(broken)
    assert any("does not support condition key aws:ViaAWSService" in p for p in problems), problems


def test_action_expansion_grants_nothing_in_the_shipped_delta(delta):
    """The mutant demonstrates the risk; the shipped shape refuses it (OD-R1)."""
    arn = _w0_reserved_role_arn()
    broken = _mutate(delta, gen.ICPERMADMIN_DELTA_SID,
                     lambda s: s.__setitem__("Action",
                                             ["iam:PutRolePolicy", "iam:DeleteRolePolicy"]))
    assert allowed(broken, "iam:DeleteRolePolicy", arn, _FAS_CTX), (
        "the expanded mutant grants the extra action — the risk")
    assert iam_eval.decide(delta, "iam:DeleteRolePolicy", arn, _FAS_CTX).decision \
        is iam_eval.Decision.IMPLICIT_DENY, "the shipped delta grants ONLY iam:PutRolePolicy"


@pytest.mark.parametrize("bad_arn", [
    "*",
    _w0_reserved_role_arn(suffix="*"),
    _w0_reserved_role_arn(suffix="0123456789abcde?"),
    f"arn:aws:iam::111122223333:role/aws-reserved/sso.amazonaws.com/*",
    _w0_reserved_role_arn() + "*",
])
def test_resource_and_suffix_wildcarding_is_refused(bad_arn):
    """OD-R2: a pattern would silently cover a role nobody reviewed; suffix rotation must
    fail closed instead of being survived."""
    with pytest.raises(ValueError):
        gen.require_valid_w0_reserved_role_arn(bad_arn)


@pytest.mark.parametrize("bad_suffix", [
    "0123456789abcde",      # 15 hex
    "0123456789abcdef0",    # 17 hex
    "0123456789ABCDEF",     # uppercase
    "0123456789abcdez",     # non-hex
    "",                     # missing
])
def test_suffix_shape_is_enforced(bad_suffix):
    with pytest.raises(ValueError):
        gen.require_valid_w0_reserved_role_arn(_w0_reserved_role_arn(suffix=bad_suffix))


def test_self_scope_and_other_permission_sets_are_refused():
    """OD-R2: ICPermAdmin must never gain the write on its OWN reserved role (self-scope
    plus PutInlinePolicyToPermissionSet would be full self-modification), nor on any other
    permission set's role."""
    import anchor_loader
    sets = anchor_loader.load(anchor_loader.declared_tier()).anchor["permission_sets"]
    for key in ("ICPermAdmin", "ReadOnly"):
        other = sets[key].get("name") or sets[key].get("permission_set_name")
        with pytest.raises(ValueError):
            gen.require_valid_w0_reserved_role_arn(_w0_reserved_role_arn(ps_name=other))


@pytest.mark.parametrize("bad_arn", [
    # a non-reserved-path role in this account
    lambda: gen.READER_ROLE_ARNS[0],
    # the right name pattern in the WRONG account
    lambda: _w0_reserved_role_arn(account="999988887777"),
    # the wrong partition
    lambda: _w0_reserved_role_arn().replace("arn:aws:", "arn:aws-cn:", 1),
    # a wrong region path segment
    lambda: _w0_reserved_role_arn(region_segment=False).replace(
        "sso.amazonaws.com/", "sso.amazonaws.com/eu-west-3/", 1),
    # placeholders and non-strings
    lambda: "<RESERVED-ROLE-ARN>",
    lambda: "",
    lambda: None,
    lambda: 42,
])
def test_other_role_scopes_and_placeholders_are_refused(bad_arn):
    with pytest.raises(ValueError):
        gen.require_valid_w0_reserved_role_arn(bad_arn())


def test_both_documented_reserved_path_forms_are_accepted():
    """DOC-2: the region path segment is absent when the identity source is in us-east-1.
    Both exact forms are valid; the operator-held live read decides which is real."""
    for form in (_w0_reserved_role_arn(region_segment=True),
                 _w0_reserved_role_arn(region_segment=False)):
        doc = gen.icpermadmin_provisioning_delta(form)
        assert doc["Statement"][0]["Resource"] == form


def test_icpermadmin_delta_generation_is_deterministic_and_the_arn_reaches_the_bytes():
    a = _w0_reserved_role_arn()
    b = _w0_reserved_role_arn(suffix="fedcba9876543210")
    assert gen.canonical(gen.icpermadmin_provisioning_delta(a)) == \
        gen.canonical(gen.icpermadmin_provisioning_delta(a))
    assert gen.canonical(gen.icpermadmin_provisioning_delta(a)) != \
        gen.canonical(gen.icpermadmin_provisioning_delta(b))


def test_the_reserved_role_arn_pin_gate_fails_closed():
    """OD-R2 re-pin gate: a rotated suffix no longer matches the operator-held pin, and a
    missing or malformed pin is a refusal, never a default."""
    import hashlib as _hashlib
    arn = _w0_reserved_role_arn()
    pin = _hashlib.sha256(arn.encode("utf-8")).hexdigest()
    assert gen.require_pinned_w0_reserved_role_arn(arn, pin) == arn
    rotated = _w0_reserved_role_arn(suffix="fedcba9876543210")
    with pytest.raises(ValueError):
        gen.require_pinned_w0_reserved_role_arn(rotated, pin)
    for bad_pin in (None, "", "abc", pin.upper(), 42):
        with pytest.raises(ValueError):
            gen.require_pinned_w0_reserved_role_arn(arn, bad_pin)


def test_merge_appends_exactly_the_delta_and_preserves_the_captured_statements():
    captured = {"Version": "2012-10-17", "Statement": [
        {"Sid": "ExistingA", "Effect": "Allow",
         "Action": ["sso:DescribePermissionSet"], "Resource": "*"},
        {"Effect": "Deny", "Action": ["iam:CreateUser"], "Resource": "*"},
    ]}
    before = gen.canonical(captured)
    merged = gen.merge_icpermadmin_delta(copy.deepcopy(captured), _w0_reserved_role_arn())
    assert len(merged["Statement"]) == len(captured["Statement"]) + 1
    assert merged["Statement"][-1] == gen.icpermadmin_provisioning_delta(
        _w0_reserved_role_arn())["Statement"][0]
    assert gen.canonical({**merged, "Statement": merged["Statement"][:-1]}) == before, (
        "the captured statements must be preserved byte-for-byte, in order")


def test_merge_refuses_a_document_already_carrying_the_delta_sid():
    captured = {"Version": "2012-10-17", "Statement": [
        {"Sid": "ExistingA", "Effect": "Allow",
         "Action": ["sso:DescribePermissionSet"], "Resource": "*"}]}
    merged = gen.merge_icpermadmin_delta(captured, _w0_reserved_role_arn())
    with pytest.raises(ValueError):
        gen.merge_icpermadmin_delta(merged, _w0_reserved_role_arn())


@pytest.mark.parametrize("bad_captured", [
    "not a dict",
    {"Version": "2008-10-17", "Statement": [{"Effect": "Allow"}]},
    {"Version": "2012-10-17", "Statement": []},
    {"Version": "2012-10-17"},
    {"Version": "2012-10-17", "Statement": [{"Sid": "NoEffect"}]},
    {"Version": "2012-10-17", "Statement": ["not a statement"]},
])
def test_merge_refuses_a_malformed_captured_document(bad_captured):
    with pytest.raises(ValueError):
        gen.merge_icpermadmin_delta(bad_captured, _w0_reserved_role_arn())


def test_the_delta_reliance_on_called_via_first_is_recorded_disputed():
    """OD-R3 honesty: SUPPORT is documented (the key is in ACTION_CONDITION_KEYS, so the
    dead-grant detector does not refuse the reviewed shape), but POPULATION by Identity
    Center's provisioning write is unproven and must be REPORTED. What settles it is the
    live CloudTrail iam:PutRolePolicy ALLOWED action-truth event (OD-R4 rev-3.1) — NOT a
    terminal ProvisionPermissionSet SUCCEEDED status alone, which can diff-and-skip the
    downstream write under parity; the simulator is explicitly insufficient."""
    pairing = ("iam:PutRolePolicy", "aws:CalledViaFirst")
    assert pairing in iam_eval.DISPUTED_RUNTIME_CONTEXT
    assert "Unproven" in iam_eval.DISPUTED_RUNTIME_CONTEXT[pairing]
    assert "aws:CalledViaFirst" in iam_eval.ACTION_CONDITION_KEYS["iam:PutRolePolicy"]
    doc = gen.icpermadmin_provisioning_delta(_w0_reserved_role_arn())
    reported = iam_eval.disputed_pairings(doc)
    assert reported and "aws:CalledViaFirst" in reported[0], (
        "the delta's reliance on the disputed key must be visible, never silent")


def test_the_permanent_w0_assignment_invariant_is_pinned(contract):
    """OD-R2: dropping the invariant text is a contract regression, not a cleanup."""
    section = contract["ic_provisioning_closure"]
    invariant = section["_permanent_assignment_invariant"]
    for required_phrase in ("at least one", "suffix", "re-pin", "fails closed"):
        assert required_phrase in invariant, required_phrase
    assert section["provision_role_write"] == ["iam:PutRolePolicy"]


# --------------------------------------------------------------------------------------- #
# OD-R4 rev-3.1 CLASS-LEVEL anti-reintroduction guard. The adversarial lane proved an
# exact-string absence check inert (a reworded reintroduction stayed green), so the guard
# below is structural: it scans EVERY sentence of EVERY governed prose surface — the whole
# ic_provisioning_closure contract section, every DISPUTED_RUNTIME_CONTEXT entry, every
# ACTIVE review-record scope, and the generator/evaluator commentary — for the CLASS of
# unsafe claims, and it pins the machine-checkable standard the prose must agree with.
# The structured fields are the authority; the sentence scans keep the prose from ever
# contradicting them, wherever and however the superseded inference is reworded.
# --------------------------------------------------------------------------------------- #
_ODR31_SUCCESS_TOKENS = frozenset(
    {"succeeded", "succeeds", "successful", "success"})
_ODR31_PROOF_TOKENS = frozenset(
    {"prove", "proves", "proven", "proof", "sufficient", "suffices", "settles", "settle",
     "confirms", "confirm", "establishes", "establish", "demonstrates", "demonstrate",
     "satisfies", "satisfy", "guarantees", "guarantee", "verifies", "verify", "evidences"})
_ODR31_NEGATION_TOKENS = frozenset(
    {"not", "no", "never", "nothing", "none", "insufficient", "cannot", "without",
     "indeterminate", "unproven"})
_ODR31_OPTIONAL_MARKERS = (
    "optional", "recommended", "preferabl", "best effort", "best-effort",
    "where practical", "when practical", "if available", "when available",
    "where possible", "if possible", "advisory", "not required", "need not",
    "if desired", "unless")
_ODR31_COMMENTARY_SOURCES = ("scripts/iam_eval.py", "scripts/gen_operator_policies.py")


def _odr31_normalize(text: str) -> str:
    """Lowercase; underscores to spaces (so PROVISIONAL_CONDITION_DEAD reads as words);
    every other non-word character to a space. Hyphens survive (sole-grant stays one
    token). Rewording that only changes case, punctuation or identifier style therefore
    cannot dodge the scans."""
    return re.sub(r"[^a-z0-9\s-]", " ", text.lower().replace("_", " "))


def _odr31_sentences(text: str) -> list:
    # A '.' or ';' ends a sentence only before whitespace/end-of-text, so dotted
    # identifiers (userIdentity.sessionContext, test_operator_policies.py,
    # sso.amazonaws.com) never fracture the sentence they sit in.
    return [_odr31_normalize(part)
            for part in re.split(r"[.;](?=\s|$)", text) if part.strip()]


def _odr31_prose_surfaces(contract) -> dict:
    """Every governed prose surface of the OD-R4 rev-3.1 correction, so a reintroduction
    RELOCATED to another contract field, a disputed-context entry, or a review-record
    scope fails exactly like one in _condition_hypothesis."""
    surfaces = {}

    def walk(prefix, node):
        if isinstance(node, str):
            surfaces[prefix] = node
        elif isinstance(node, dict):
            for key, value in node.items():
                walk(f"{prefix}.{key}", value)
        elif isinstance(node, (list, tuple)):
            for index, value in enumerate(node):
                walk(f"{prefix}[{index}]", value)

    walk("ic_provisioning_closure", contract["ic_provisioning_closure"])
    for pairing, why in iam_eval.DISPUTED_RUNTIME_CONTEXT.items():
        surfaces[f"DISPUTED_RUNTIME_CONTEXT[{pairing}]"] = why
    ledger = json.loads(
        (REPO_ROOT / "tests" / "fixtures" / "review-record-ledger.json")
        .read_text(encoding="utf-8"))
    for record_id, record in ledger["review_records"].items():
        if record.get("status") == "ACTIVE":
            surfaces[f"review-record-ledger:{record_id}"] = record.get("scope", "")
    return surfaces


def _odr31_commentary_texts() -> dict:
    """The generator/evaluator source files, comments included, so the superseded framing
    cannot survive (or return) as commentary either."""
    return {relative: (REPO_ROOT / relative).read_text(encoding="utf-8")
            for relative in _ODR31_COMMENTARY_SOURCES}


def test_no_governed_surface_claims_succeeded_proves_the_condition(contract):
    """OD-R4 rev-3.1, class-level: a terminal ProvisionPermissionSet SUCCEEDED status does
    NOT prove aws:CalledViaFirst was present or matched — Identity Center may diff-and-skip
    the downstream iam:PutRolePolicy write under parity, so a SUCCEEDED status without an
    exercised write proves nothing. ANY sentence on ANY governed surface that pairs a
    provisioning-success term with a proof/sufficiency term and carries no negating or
    limiting term is the superseded inference — original bytes, reworded, or relocated —
    and fails here."""
    offending = []
    for name, text in _odr31_prose_surfaces(contract).items():
        for sentence in _odr31_sentences(text):
            words = frozenset(sentence.split())
            if (words & _ODR31_SUCCESS_TOKENS and words & _ODR31_PROOF_TOKENS
                    and not words & _ODR31_NEGATION_TOKENS):
                offending.append((name, sentence.strip()))
    assert not offending, (
        "superseded 'SUCCEEDED proves the condition' inference reintroduced: "
        f"{offending}")
    # The prose correction itself must stay present, not merely the claim absent.
    hyp = contract["ic_provisioning_closure"]["_condition_hypothesis"].lower()
    assert "succeeded" in hyp and "does not by itself prove" in hyp


def test_condition_hypothesis_pins_the_corrected_structured_standard(contract):
    """The machine-checkable form of the correction. SUCCEEDED-without-write is
    INDETERMINATE_WRITE_NOT_EXERCISED, never CONDITION_LIVE; observations never upgrade;
    disagreement is CONFLICTING_OBSERVATIONS; documentation stays insufficient."""
    section = contract["ic_provisioning_closure"]
    std = section["_condition_liveness_evidence_standard"]
    assert std["succeeded_alone_proves_condition"] is False
    assert "cloudtrail" in std["condition_live_requires"].lower()
    assert "allowed" in std["condition_live_requires"].lower()
    assert std["lookup_events_reveals_calledviafirst"] is False
    assert std["invokedby_is_condition_key_proof"] is False
    assert std["succeeded_without_write_event"] == "INDETERMINATE_WRITE_NOT_EXERCISED"
    assert std["single_controlled_denial"] == "PROVISIONAL_CONDITION_DEAD"
    assert std["single_denial_may_authorize_fallback"] is False
    assert std["confirmation_requires_separate_authorization_and_window"] is True
    assert std["later_observations_non_upgrading"] is True
    assert std["disagreement_class"] == "CONFLICTING_OBSERVATIONS"
    assert std["disagreement_can_yield_condition_live"] is False
    assert std["retry_or_same_window_disambiguation_exists"] is False
    assert std["documentation_status"] == "DOCUMENTATION_INSUFFICIENT"
    # The prose must carry the load-bearing corrected terms too (belt and braces).
    hyp = section["_condition_hypothesis"]
    for phrase in ("INDETERMINATE_WRITE_NOT_EXERCISED", "PROVISIONAL_CONDITION_DEAD",
                   "CONFLICTING_OBSERVATIONS", "action-truth", "invokedBy",
                   "lookup-events", "DOCUMENTATION_INSUFFICIENT"):
        assert phrase in hyp, phrase
    # The grant itself is untouched by this correction.
    assert section["provision_role_write"] == ["iam:PutRolePolicy"]


def test_positive_control_is_the_cloudtrail_allowed_event_not_the_run(contract):
    """The positive control is the CloudTrail iam:PutRolePolicy Allowed action-truth
    event — NEVER the provisioning run or its terminal status. Structured pin plus a
    class-level scan: any sentence anywhere on the governed surfaces (commentary
    included) that frames a positive control/artifact without CloudTrail, or frames it
    around the run/status without a negating term, restores the superseded framing and
    fails here."""
    std = contract["ic_provisioning_closure"]["_condition_liveness_evidence_standard"]
    assert std.get("positive_control_artifact") == \
        "cloudtrail_iam_putrolepolicy_allowed_action_truth_event"
    assert std.get("positive_control_is_provisioning_run") is False
    surfaces = dict(_odr31_prose_surfaces(contract))
    surfaces.update(_odr31_commentary_texts())
    offending = []
    for name, text in surfaces.items():
        for sentence in _odr31_sentences(text):
            if "positive control" not in sentence and "positive artifact" not in sentence:
                continue
            words = frozenset(sentence.split())
            if "cloudtrail" not in words:
                offending.append((name, "positive control without CloudTrail",
                                  sentence.strip()))
            elif (words & {"run", "runs", "provisioning", "succeeded", "status"}
                    and not words & _ODR31_NEGATION_TOKENS):
                offending.append((name, "run/status-framed positive control with no "
                                        "negating term", sentence.strip()))
    assert not offending, f"superseded positive-control framing: {offending}"


def test_condition_live_requires_the_bound_allowed_event_and_sole_grant_premise(contract):
    """CONDITION_LIVE takes a CloudTrail Allowed event BOUND to the probe submission,
    caller/session context, exact hashed target role and event time, under the REQUIRED
    sole-grant premise — an unbound Allowed event proves nothing (any broader grant could
    have authorized it). Deleting the premise, detaching it from CONDITION_LIVE, or
    weakening it to optional fails here."""
    std = contract["ic_provisioning_closure"]["_condition_liveness_evidence_standard"]
    assert std.get("condition_live_requires_cloudtrail_allowed_event") is True
    assert std.get("condition_live_requires_sole_grant_premise") is True
    assert set(std.get("allowed_event_required_bindings") or []) == {
        "probe_submission", "caller_session_context", "exact_hashed_target_role",
        "event_time", "sole_grant_premise"}, (
        "the Allowed event's mandatory bindings changed — an event accepted without "
        "target/session/request binding is not attributable evidence")
    requirement = std["condition_live_requires"].lower()
    for needed in ("cloudtrail", "allowed", "bound", "sole-grant premise"):
        assert needed in requirement, needed
    # The prose sentence DEFINING CONDITION_LIVE must itself bind event and premise.
    hyp = contract["ic_provisioning_closure"]["_condition_hypothesis"]
    defining = [s for s in _odr31_sentences(hyp)
                if "condition live" in s and "requires" in s]
    assert defining, "the hypothesis no longer defines what CONDITION_LIVE requires"
    assert any("cloudtrail" in s and "allowed" in s and "bound" in s and "sole-grant" in s
               for s in defining), (
        "the CONDITION_LIVE definition lost the bound Allowed event or the sole-grant "
        f"premise: {defining}")
    # The premise is REQUIRED on every surface — never optional, advisory or best-effort.
    surfaces = dict(_odr31_prose_surfaces(contract))
    surfaces.update(_odr31_commentary_texts())
    weakened = [
        (name, sentence.strip())
        for name, text in surfaces.items()
        for sentence in _odr31_sentences(text)
        if ("sole-grant" in sentence or "sole grant" in sentence)
        and any(marker in sentence for marker in _ODR31_OPTIONAL_MARKERS)]
    assert not weakened, f"sole-grant premise weakened from required: {weakened}"


def test_condition_dead_requires_denial_source_discrimination(contract):
    """PROVISIONAL_CONDITION_DEAD may be assigned ONLY to a denial that survives BOTH
    explicit-deny discrimination and sole-grant discrimination. A denial attributable to
    an SCP, a permissions boundary, a session policy, a stale authorization plane, a
    wrong target, a wrong caller, or another grant is INDETERMINATE — never
    condition-dead. Dropping any discrimination, or accepting a bare AccessDenied as
    condition-dead, fails here."""
    section = contract["ic_provisioning_closure"]
    std = section["_condition_liveness_evidence_standard"]
    required = {
        "cloudtrail_access_denied_action_truth_event",
        "explicit_deny_discrimination",
        "scp_attribution_excluded",
        "permissions_boundary_attribution_excluded",
        "session_policy_attribution_excluded",
        "sole_grant_discrimination",
        "stale_authorization_plane_excluded",
        "wrong_target_excluded",
        "wrong_caller_excluded",
        "other_grant_attribution_excluded"}
    actual = set(std.get("provisional_condition_dead_requires") or [])
    assert required <= actual, (
        f"condition-dead lost mandatory discriminations: {sorted(required - actual)}")
    assert std.get("explicit_deny_attributable_denial_class") == "INDETERMINATE"
    assert std.get("alternate_cause_attributable_denial_class") == "INDETERMINATE"
    prose = _odr31_normalize(section["_condition_hypothesis"] + " "
                             + section["_fallback_posture"])
    for cause in ("accessdenied", "scp", "permissions boundary", "session policy",
                  "stale", "wrong target", "wrong caller", "another grant"):
        assert cause in prose, f"denial-source discrimination lost from prose: {cause}"
    assert "indeterminate" in prose and "never" in prose
    # No prose sentence may hand out condition-dead unconditionally.
    unconditional = [
        sentence.strip()
        for field in ("_condition_hypothesis", "_fallback_posture")
        for sentence in _odr31_sentences(section[field])
        if "provisional condition dead" in sentence
        and not frozenset(sentence.split()) & {"only", "never", "not", "discrimination",
                                               "discriminations", "discriminated"}]
    assert not unconditional, f"unconditional condition-dead assignment: {unconditional}"


def test_condition_liveness_standard_forbids_a_single_denial_widening(contract):
    """A single controlled denial (PROVISIONAL_CONDITION_DEAD) may never, by itself,
    authorize aws:ViaAWSService, condition removal, resource widening, or another IAM
    action — it takes a separate authorization, a separate window, and a non-upgrading
    confirmation. Pins that no path treats one denial as licence to widen."""
    section = contract["ic_provisioning_closure"]
    std = section["_condition_liveness_evidence_standard"]
    assert std["single_denial_may_authorize_fallback"] is False
    assert std["confirmation_requires_separate_authorization_and_window"] is True
    assert std["retry_or_same_window_disambiguation_exists"] is False
    # And the standing fallback posture still forbids in-place relaxation.
    assert "separate reviewed delta" in section["_condition_hypothesis"].lower()
    # Class-level: every ViaAWSService mention must refuse, sever or separately review it.
    permissive = [
        (field, sentence.strip())
        for field in ("_condition_hypothesis", "_fallback_posture", "_excluded")
        for sentence in _odr31_sentences(section[field])
        if "viaawsservice" in sentence
        and not frozenset(sentence.split()) & {"not", "never", "separate", "separately",
                                               "excluded", "refused"}]
    assert not permissive, f"ViaAWSService mentioned permissively: {permissive}"


def test_the_ic_provisioning_closure_never_joins_the_operator_requirement_sets():
    """The delta's principal is ICPermAdmin. If its section leaked into the W0/temporary
    requirement joins, the loss proof would demand a capability those principals must
    never hold."""
    import allow_model
    assert "iam:PutRolePolicy" not in allow_model.w0_required_actions()
    assert "iam:PutRolePolicy" not in allow_model.temporary_required_actions()
    assert "iam:PutRolePolicy" not in allow_model.required_actions()


def test_the_delta_stays_out_of_the_principal_policy_inventory():
    """The delta is a FRAGMENT merged into an operator-held document, deliberately outside
    policy_inventory (which would call it with a discovery expiry and report a generation
    failure) and outside allow_model.TARGETS (a fragment has no deny ceiling to prove)."""
    import allow_model
    import policy_inventory
    assert not any("icpermadmin" in key.lower() for key in policy_inventory.discover()), (
        "the fragment builder must not satisfy the *_policy discovery convention")
    assert "icpermadmin" not in json.dumps(sorted(allow_model.TARGETS)).lower()


def test_the_delta_cli_pin_gate_and_merge_print_digests_only(tmp_path):
    """The CLI path is what an operator actually runs: a wrong pin refuses before any
    output exists, and the merge prints digests and counts ONLY — never the ARN, never
    policy content — and writes the merged file 0600 without overwriting."""
    import hashlib as _hashlib
    import os as _os
    arn = _w0_reserved_role_arn()
    pin = _hashlib.sha256(arn.encode("utf-8")).hexdigest()
    captured_path = tmp_path / "captured.json"
    captured_path.write_text(json.dumps({"Version": "2012-10-17", "Statement": [
        {"Sid": "ExistingA", "Effect": "Allow",
         "Action": ["sso:DescribePermissionSet"], "Resource": "*"}]}), encoding="utf-8")
    output_path = tmp_path / "merged.json"
    cli = [sys.executable, str(REPO_ROOT / "scripts" / "gen_operator_policies.py"),
           "--emit", "icpermadmin-provisioning-delta", "--reserved-role-arn", arn]

    wrong = subprocess.run([*cli, "--reserved-role-arn-pin", "0" * 64],
                           capture_output=True, text=True)
    assert wrong.returncode != 0 and not output_path.exists()

    merge = subprocess.run([*cli, "--reserved-role-arn-pin", pin,
                            "--merge-captured", str(captured_path),
                            "--merge-output", str(output_path)],
                           capture_output=True, text=True)
    assert merge.returncode == 0, merge.stderr
    assert "arn:" not in merge.stdout and _DELTA_SUFFIX not in merge.stdout, (
        "the merge path must never print the reserved-role ARN or policy content")
    assert "merged_canonical" in merge.stdout and "statements          1 -> 2" in merge.stdout
    assert _os.stat(output_path).st_mode & 0o777 == 0o600
    merged = json.loads(output_path.read_text(encoding="utf-8"))
    assert merged["Statement"][-1]["Sid"] == gen.ICPERMADMIN_DELTA_SID

    again = subprocess.run([*cli, "--reserved-role-arn-pin", pin,
                            "--merge-captured", str(captured_path),
                            "--merge-output", str(output_path)],
                           capture_output=True, text=True)
    assert again.returncode != 0, "an existing output must never be overwritten"
