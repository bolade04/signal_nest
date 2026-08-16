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
    ("kms:Decrypt", gen.ARN["cmk_secrets"], "DenyDecryptOutsideTheStateCmk"),
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
            assert allowed(permanent, action, arn, PERM_CTX), f"{action} on {arn}"
    for action in stage_b["describe_star"]:
        assert allowed(permanent, action, "*", PERM_CTX), action
    assert "iam:PassRole" not in json.dumps(
        [s for s in permanent["Statement"] if s["Effect"] == "Allow"]), (
        "the B-3 delta must add NO PassRole surface")


def test_state_cmk_use_is_dead_without_the_backend_via_service(permanent):
    """The ViaService condition is load-bearing: a DIRECT kms call by the operator's own
    credentials (context lacking or naming another service) must never reach an Allow."""
    for ctx, want in ((PERM_CTX, iam_eval.Decision.MISSING_CONTEXT),
                      (dict(PERM_CTX, **{"kms:ViaService": f"lambda.{gen.REGION}.amazonaws.com"}),
                       iam_eval.Decision.EXPLICIT_DENY)):
        got = iam_eval.decide(permanent, "kms:GenerateDataKey", gen.ARN["cmk_state"], ctx).decision
        assert got is not iam_eval.Decision.EXPLICIT_ALLOW, ctx
    # kms:Decrypt additionally stays EXPLICITLY denied off the state CMK whatever the context
    iam_eval.require_explicit_deny(permanent, "kms:Decrypt", gen.ARN["cmk_secrets"],
                                   W0_CMK_CTX_S3, sid="DenyDecryptOutsideTheStateCmk")


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
    # both ARN forms per family, so an authorization-shape difference cannot break mid-apply
    assert len(gen.TASK_DEFINITION_FAMILY_ARNS) == 8


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
