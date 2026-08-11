"""Eight-role permissions-boundary compatibility (Gate 4N-I6).

A permissions boundary caps effective permission to
`identity policy INTERSECT boundary`, subject to explicit Deny. A boundary that removes
a function a role legitimately performs fails at RUNTIME, not at apply — so it must be
proven compatible before it is ever created.

The intended-permission table below is transcribed from each role's own inline policy in
the repository, and a test asserts that transcription still matches the source. The
load-bearing case is `iam:PassRole`: the revision-reader runner genuinely needs it, so a
blanket deny — which would have looked like good security — would have broken its only
job.

No AWS access, no network.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import gen_boundary_policy as gb  # noqa: E402
import iam_eval  # noqa: E402
import signalnest_identity  # noqa: E402
from iam_eval import Decision  # noqa: E402

BOUNDARY = gb.boundary_policy()
A, R, P = gb.ACCOUNT, gb.REGION, gb.PREFIX

SECRET = f"arn:aws:secretsmanager:{R}:{A}:secret:{P}/DATABASE_URL-AbCdEf"
APP_BUCKET = signalnest_identity.s3_bucket_arn(signalnest_identity.APP_BUCKET_NAME)
READER_REPO = f"arn:aws:ecr:{R}:{A}:repository/{P}/revision-reader"
APP_REPO = f"arn:aws:ecr:{R}:{A}:repository/{P}/api"
LOG_GROUP = f"arn:aws:logs:{R}:{A}:log-group:/ecs/{P}-api"
CLUSTER = f"arn:aws:ecs:{R}:{A}:cluster/{P}-cluster"

# role -> (action, resource) pairs the role's OWN inline policy legitimately allows.
INTENDED = {
    "ecs-execution": [
        ("ecr:GetAuthorizationToken", "*"),
        ("ecr:BatchGetImage", APP_REPO),
        ("ecr:BatchCheckLayerAvailability", APP_REPO),
        ("ecr:GetDownloadUrlForLayer", APP_REPO),
        ("logs:CreateLogStream", LOG_GROUP),
        ("logs:PutLogEvents", LOG_GROUP),
        ("secretsmanager:GetSecretValue", SECRET),
        ("kms:Decrypt", gb.SECRETS_CMK),
    ],
    "api-task": [("s3:ListBucket", APP_BUCKET), ("s3:GetObject", f"{APP_BUCKET}/x"),
                 ("s3:PutObject", f"{APP_BUCKET}/x"), ("s3:DeleteObject", f"{APP_BUCKET}/x")],
    "worker-task": [("s3:ListBucket", APP_BUCKET), ("s3:GetObject", f"{APP_BUCKET}/x"),
                    ("s3:PutObject", f"{APP_BUCKET}/x"), ("s3:DeleteObject", f"{APP_BUCKET}/x")],
    "migration-task": [],  # inline policy is deliberately empty
    "ci-publisher": [
        ("ecr:GetAuthorizationToken", "*"),
        ("ecr:PutImage", APP_REPO), ("ecr:InitiateLayerUpload", APP_REPO),
        ("ecr:UploadLayerPart", APP_REPO), ("ecr:CompleteLayerUpload", APP_REPO),
        ("ecr:BatchCheckLayerAvailability", APP_REPO), ("ecr:BatchGetImage", APP_REPO),
        ("ecr:DescribeImages", APP_REPO),
    ],
    "revision-reader-execution": [
        ("ecr:GetAuthorizationToken", "*"),
        ("ecr:BatchGetImage", READER_REPO), ("ecr:GetDownloadUrlForLayer", READER_REPO),
        ("logs:CreateLogStream", f"arn:aws:logs:{R}:{A}:log-group:/ecs/{P}-revision-reader"),
        ("logs:PutLogEvents", f"arn:aws:logs:{R}:{A}:log-group:/ecs/{P}-revision-reader"),
        ("secretsmanager:GetSecretValue", SECRET),
        ("kms:Decrypt", gb.SECRETS_CMK),
    ],
    "revision-reader-publisher": [
        ("ecr:GetAuthorizationToken", "*"),
        ("ecr:PutImage", READER_REPO), ("ecr:InitiateLayerUpload", READER_REPO),
        ("ecr:UploadLayerPart", READER_REPO), ("ecr:CompleteLayerUpload", READER_REPO),
    ],
    "revision-reader-runner": [
        ("ecs:RunTask", f"arn:aws:ecs:{R}:{A}:task-definition/{P}-revision-reader:1"),
        ("ecs:DescribeTasks", f"arn:aws:ecs:{R}:{A}:task/{P}-cluster/abc"),
        ("logs:GetLogEvents", f"arn:aws:logs:{R}:{A}:log-group:/ecs/{P}-revision-reader:*"),
        # THE load-bearing case: the runner passes exactly one role.
        ("iam:PassRole", gb.READER_EXECUTION_ROLE),
    ],
}

# Capabilities the boundary exists to remove, with the role that must NOT gain them.
INTENTIONALLY_REMOVED = [
    ("iam:CreateRole", f"arn:aws:iam::{A}:role/anything"),
    ("iam:PutRolePolicy", f"arn:aws:iam::{A}:role/anything"),
    ("iam:AttachRolePolicy", f"arn:aws:iam::{A}:role/anything"),
    ("iam:PutRolePermissionsBoundary", f"arn:aws:iam::{A}:role/anything"),
    ("sso:PutInlinePolicyToPermissionSet", "*"),
    ("organizations:LeaveOrganization", "*"),
    ("cloudtrail:StopLogging", f"arn:aws:cloudtrail:{R}:{A}:trail/{P}-audit"),
    ("cloudtrail:DeleteTrail", f"arn:aws:cloudtrail:{R}:{A}:trail/{P}-audit"),
    ("s3:GetObject", f"{gb.STATE_BUCKET}/{P}/root.tfstate"),
    ("s3:PutObject", f"{gb.STATE_BUCKET}/{P}/root.tfstate"),
    ("dynamodb:PutItem", gb.LOCK_TABLE),
    ("secretsmanager:GetSecretValue", f"arn:aws:secretsmanager:{R}:{A}:secret:other/thing"),
    ("secretsmanager:PutSecretValue", SECRET),
    ("kms:Decrypt", gb.STATE_CMK),
    ("kms:CreateGrant", gb.SECRETS_CMK),
    ("kms:ScheduleKeyDeletion", gb.SECRETS_CMK),
    ("ecs:RegisterTaskDefinition", "*"),
    ("ecs:CreateService", "*"),
    ("ecs:ExecuteCommand", "*"),
    ("iam:PassRole", f"arn:aws:iam::{A}:role/{P}-ecs-execution"),
    ("sts:AssumeRole", "arn:aws:iam::999988887777:role/outside"),
    ("rds:ModifyDBInstance", f"arn:aws:rds:{R}:{A}:db:{P}-postgres"),
]


@pytest.mark.parametrize("role,pairs", sorted(INTENDED.items()))
def test_boundary_retains_every_intended_role_function(role, pairs):
    """A boundary that silently removes a role's real job is not ready."""
    broken = [
        f"{action} on {resource}"
        for action, resource in pairs
        if iam_eval.decide(BOUNDARY, action, resource, {}).decision is not Decision.EXPLICIT_ALLOW
    ]
    assert not broken, f"boundary would break {role}: {broken}"


def test_the_runner_keeps_passrole_for_exactly_one_role():
    """The load-bearing exception. A blanket PassRole deny would break the runner."""
    assert iam_eval.decide(BOUNDARY, "iam:PassRole", gb.READER_EXECUTION_ROLE, {}).decision is Decision.EXPLICIT_ALLOW
    for other in (f"arn:aws:iam::{A}:role/{P}-ecs-execution", f"arn:aws:iam::{A}:role/anything"):
        iam_eval.require_explicit_deny(BOUNDARY, "iam:PassRole", other, {},
                                       sid="DenyPassRoleExceptReaderExecutionRole")


@pytest.mark.parametrize("action,resource", INTENTIONALLY_REMOVED,
                         ids=[f"{a}" for a, _ in INTENTIONALLY_REMOVED])
def test_boundary_explicitly_denies_the_capabilities_it_exists_to_remove(action, resource):
    iam_eval.require_explicit_deny(BOUNDARY, action, resource, {})


def test_execution_roles_keep_secrets_and_kms_within_the_approved_scope():
    """Fences, not blanket denies — the deny excludes the approved prefix and CMK."""
    assert iam_eval.decide(BOUNDARY, "secretsmanager:GetSecretValue", SECRET, {}).decision is Decision.EXPLICIT_ALLOW
    assert iam_eval.decide(BOUNDARY, "kms:Decrypt", gb.SECRETS_CMK, {}).decision is Decision.EXPLICIT_ALLOW
    iam_eval.require_explicit_deny(BOUNDARY, "kms:Decrypt", gb.STATE_CMK, {})


def test_the_ceiling_is_not_a_grant():
    """The Allow * exists so the boundary does not silently drop unenumerated needs.

    It grants nothing on its own: a boundary only ever intersects with the identity
    policy. This test pins the intent so the statement is not mistaken for a grant.
    """
    ceiling = [s for s in BOUNDARY["Statement"] if s["Effect"] == "Allow"]
    assert len(ceiling) == 1 and ceiling[0]["Action"] == "*" and ceiling[0]["Resource"] == "*"
    assert len([s for s in BOUNDARY["Statement"] if s["Effect"] == "Deny"]) >= 10


def test_boundary_is_structurally_valid():
    assert iam_eval.validate_policy(BOUNDARY, kind="boundary") == []


def test_the_ceiling_idiom_is_still_rejected_in_an_identity_policy():
    """kind-awareness must not become a blanket exemption."""
    problems = iam_eval.validate_policy(BOUNDARY, kind="identity")
    assert any("bare wildcard Action in an Allow" in p for p in problems)


def test_intended_table_still_matches_the_repository():
    """Guards against the table drifting away from the roles it claims to describe."""
    reader = (REPO_ROOT / "infra/aws/modules/revision_reader/iam.tf").read_text(encoding="utf-8")
    assert "PassOnlyReaderExecutionRole" in reader, "the runner's PassRole Sid must still exist"
    assert "RunExactReaderRevisionOnly" in reader
    iam_src = (REPO_ROOT / "infra/aws/modules/iam/main.tf").read_text(encoding="utf-8")
    for sid in ("EcrPullApplicationImages", "SecretsReadReferencedContainers",
                "AppBucketObjects", "EcrPushToTwoRepos"):
        assert sid in iam_src, f"{sid} missing — the intended-permission table is stale"
    declared = len(re.findall(r'^resource "aws_iam_role" ', iam_src + reader, re.MULTILINE))
    assert declared == 8, f"expected 8 roles, found {declared}"
    assert len(INTENDED) == 8
