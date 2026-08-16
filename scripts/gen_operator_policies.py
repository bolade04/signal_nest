#!/usr/bin/env python3
"""Deterministic generator for the SignalNest operator permission-set policies.

Gate 4N-I3. Two policies are generated:

  permanent-w0     the standing SignalNestStagingW0Operator inline policy. Read-mostly
                   diagnostics PLUS — since INFRA-9 B-3 (2026-08-16) — the exact-scoped,
                   fenced apply surface: the S3/DynamoDB/KMS state-backend closure on the
                   exact backend resources and ecs:RegisterTaskDefinition on the four
                   composition task-definition families. Every carved capability is
                   re-denied everywhere else by a NotResource fence (the same idiom the
                   temporary operator uses), so the universal Resource-"*" probes still
                   resolve EXPLICIT_DENY.

  bootstrap-temp   a SEPARATE, EXPIRING permission set that performs planning and the
                   remaining bootstrap mutations. It is standalone: it carries its own
                   full refresh-read closure and inherits nothing from W0.

Why a generator rather than hand-written JSON: Gate 4N-I2 shipped four wrong resource
names and a wrong action list because the documents were written by hand. Here every
name comes from NAMES below, which is checked against the repository by
tests/test_operator_policies.py, and the read closure comes from REFRESH_CLOSURE, which
was derived from a real successful full-graph refresh rather than from intuition.

The EXPECTED closure lives in infra/aws/operator-closure-contract.json, a SEPARATE source
this module never reads. tests/ compares the generated policy against that contract, so a
defect here cannot silently move the expectation with it.

Output is canonical JSON (sort_keys, compact separators, ensure_ascii=True) so the same
inputs always produce byte-identical output and a stamped hash is reproducible.

Usage:
    python3 scripts/gen_operator_policies.py [--emit permanent-w0|bootstrap-temp]
                                             [--hash] [--expiry ISO8601]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Gate 4N-I7: account/region/prefix and the boundary ARN come from ONE authoritative
# source (scripts/signalnest_identity.py). This module must not reconstruct them.
import iam_eval  # noqa: E402
import signalnest_identity as identity  # noqa: E402
from must_not_contract import FORBIDDEN_CAPABILITIES  # noqa: E402
from signalnest_identity import (  # noqa: E402
    ACCOUNT, REGION, PREFIX, BOUNDARY_POLICY_ARN, BOUNDARY_POLICY_NAME,
    READER_ECR_REPOSITORY_PATH, READER_TASK_DEFINITION_FAMILY, REVISION_READER_ROLE_NAMES,
    APP_BUCKET_NAME, AUDIT_BUCKET_NAME, LOCK_TABLE_NAME, SECRETS_CMK_KEY_ID,
    STATE_BUCKET_NAME, STATE_CMK_KEY_ID, STATE_OBJECT_KEY, TRAIL_NAME,
    SPA_BUCKET_NAME, ALB_LOGS_BUCKET_NAME, CLOUDFRONT_DISTRIBUTION_ID, CLOUDFRONT_OAC_ID,
)

_READER = {n.rsplit("-", 1)[-1]: n for n in REVISION_READER_ROLE_NAMES}

# --- exact names -------------------------------------------------------------------
# Every value is derived from a repository expression; see the Gate 4N-I3 name manifest.
# The reader roles carry a `revision-` segment and the reader ECR repository uses a
# SLASH — both were wrong in Gate 4N-I2 and are the reason this table exists.
NAMES = {
    # Imported from the authoritative layer (Gate 4N-I10 Defect 5). Rebuilding these here
    # is what let the Gate 4N-I2 "revision-" segment error and the reader ECR slash/hyphen
    # error happen in the first place.
    "reader_publisher": _READER["publisher"],
    "reader_execution": _READER["execution"],
    "reader_runner": _READER["runner"],
    "reader_ecr_repo": READER_ECR_REPOSITORY_PATH,
    "reader_log_group": f"/ecs/{READER_TASK_DEFINITION_FAMILY}",
    # Imported, never rebuilt: an f-string here would resurrect the duplicate
    # construction that Defect 1 exists to eliminate.
    "boundary_policy": BOUNDARY_POLICY_NAME,
    "trail": TRAIL_NAME,
    "lock_table": LOCK_TABLE_NAME,
}

# Physical names carrying provider-generated suffixes or caller-supplied values. These
# are NOT derivable from the repository and were read live; re-verify before stamping.
LIVE_NAMES = {
    "bucket_state": STATE_BUCKET_NAME,
    "bucket_audit": AUDIT_BUCKET_NAME,
    # GATE 4N-I18, SEC-1: imported, never reconstructed. These carried live provider-generated
    # suffixes as literals until the containment moved them behind the tier-resolved inventory.
    "bucket_spa": SPA_BUCKET_NAME,
    "bucket_alb_logs": ALB_LOGS_BUCKET_NAME,
    "bucket_app": APP_BUCKET_NAME,
    "state_key": STATE_OBJECT_KEY,
    "cmk_state": STATE_CMK_KEY_ID,
    "cmk_secrets": SECRETS_CMK_KEY_ID,
    # GATE 4N-I18, SEC-1: AWS-assigned CloudFront ids, tier-resolved like every other
    # live identifier. They were literals until the containment.
    "distribution": CLOUDFRONT_DISTRIBUTION_ID,
    "oac": CLOUDFRONT_OAC_ID,
}

ARN = {
    "boundary": BOUNDARY_POLICY_ARN,  # authoritative — never rebuilt here
    "trail": f"arn:aws:cloudtrail:{REGION}:{ACCOUNT}:trail/{NAMES['trail']}",
    "lock": f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/{NAMES['lock_table']}",
    "state_bucket": f"arn:aws:s3:::{LIVE_NAMES['bucket_state']}",
    "state_object": f"arn:aws:s3:::{LIVE_NAMES['bucket_state']}/{LIVE_NAMES['state_key']}",
    "audit_bucket": f"arn:aws:s3:::{LIVE_NAMES['bucket_audit']}",
    "cmk_state": f"arn:aws:kms:{REGION}:{ACCOUNT}:key/{LIVE_NAMES['cmk_state']}",
    "cmk_secrets": f"arn:aws:kms:{REGION}:{ACCOUNT}:key/{LIVE_NAMES['cmk_secrets']}",
    "db": f"arn:aws:rds:{REGION}:{ACCOUNT}:db:{PREFIX}-postgres",
    "pg": f"arn:aws:rds:{REGION}:{ACCOUNT}:pg:{PREFIX}-pg-params",
    "subgrp": f"arn:aws:rds:{REGION}:{ACCOUNT}:subgrp:{PREFIX}-pg",
    "reader_ecr": f"arn:aws:ecr:{REGION}:{ACCOUNT}:repository/{NAMES['reader_ecr_repo']}",
    "reader_log_group": f"arn:aws:logs:{REGION}:{ACCOUNT}:log-group:{NAMES['reader_log_group']}",
    "distribution": f"arn:aws:cloudfront::{ACCOUNT}:distribution/{LIVE_NAMES['distribution']}",
    "oac": f"arn:aws:cloudfront::{ACCOUNT}:origin-access-control/{LIVE_NAMES['oac']}",
}

WORKLOAD_BUCKETS = [
    f"arn:aws:s3:::{LIVE_NAMES[k]}"
    for k in ("bucket_audit", "bucket_spa", "bucket_alb_logs", "bucket_app")
]

READER_ROLE_ARNS = [
    f"arn:aws:iam::{ACCOUNT}:role/{NAMES[k]}"
    for k in ("reader_publisher", "reader_execution", "reader_runner")
]

# GATE 4N-I17 DEFECT 6. Derived from the ACTUAL Terraform declarations, not from the full role
# inventory.
#
# Gate 4N-I16 built this from identity.ALL_ROLE_NAMES — all EIGHT repository-managed roles — while
# the composition declares inline policies for SEVEN. `migration-task` was therefore writable, and
# its own module comment says "The migration role is deliberately absent (empty role, no policy)."
# A parser that reads the .tf declarations already existed and was never joined to this scope.
#
# The set is now resolved by walking `resource "aws_iam_role_policy"` blocks and following the role
# each one binds — including the `for_each = local.s3_workload_roles` form, which is exactly where
# migration-task is excluded and where a naive parser would miss the exclusion.
import terraform_role_inventory as _tf_roles  # noqa: E402

INLINE_POLICY_ROLE_ARNS = _tf_roles.role_arns(_tf_roles.writable_roles())

REGION_COND = {"StringEquals": {"aws:RequestedRegion": REGION}}

# --- refresh read closure ----------------------------------------------------------
# Derived from the complete successful full-graph refresh of 2026-07-28T22:01:44Z:
# 267 CloudTrail events, 79 distinct API operations across 17 services, zero writes.
# These are IAM ACTION names, which differ from CloudTrail eventNames in several places
# (GetBucketEncryption -> s3:GetEncryptionConfiguration, DescribeBudget -> budgets:ViewBudget).
REFRESH_CLOSURE = {
    # Regional, no resource-level support in aggregate -> Resource "*" with a region condition.
    "star_regional": sorted(
        [
            "ec2:DescribeAddresses",
            "ec2:DescribeAddressesAttribute",
            "ec2:DescribeInternetGateways",
            "ec2:DescribeNatGateways",
            "ec2:DescribeNetworkAcls",
            "ec2:DescribeRouteTables",
            "ec2:DescribeSecurityGroupRules",
            "ec2:DescribeSecurityGroups",
            "ec2:DescribeSubnets",
            "ec2:DescribeVpcAttribute",
            "ec2:DescribeVpcs",
            "elasticloadbalancing:DescribeCapacityReservation",
            "elasticloadbalancing:DescribeListenerAttributes",
            "elasticloadbalancing:DescribeListeners",
            "elasticloadbalancing:DescribeLoadBalancerAttributes",
            "elasticloadbalancing:DescribeLoadBalancers",
            "elasticloadbalancing:DescribeTags",
            "elasticloadbalancing:DescribeTargetGroupAttributes",
            "elasticloadbalancing:DescribeTargetGroups",
            "elasticache:DescribeCacheClusters",
            "elasticache:DescribeCacheParameterGroups",
            "elasticache:DescribeCacheParameters",
            "elasticache:DescribeCacheSubnetGroups",
            "elasticache:DescribeReplicationGroups",
            "elasticache:ListTagsForResource",
            "logs:DescribeLogGroups",
            "logs:DescribeMetricFilters",
            "logs:ListTagsForResource",
            "cloudwatch:DescribeAlarms",
            "cloudwatch:GetDashboard",
            "cloudwatch:ListTagsForResource",
            "ecs:DescribeClusters",
            "ecs:ListTagsForResource",
            "ecr:DescribeRepositories",
            "ecr:GetLifecyclePolicy",
            "ecr:ListTagsForResource",
            "kms:ListAliases",
            "sts:GetCallerIdentity",
        ]
    ),
    # RDS reads. Every one supports an exact ARN, BUT the provider calls
    # DescribeDBInstances with no identifier, which authorizes against db:* — so that
    # single action must stay at Resource "*" or it is denied. Split accordingly.
    "rds_star": ["rds:DescribeDBInstances"],
    "rds_exact": sorted(
        [
            "rds:DescribeDBParameterGroups",
            "rds:DescribeDBParameters",
            "rds:DescribeDBSubnetGroups",
            "rds:ListTagsForResource",
        ]
    ),
    # S3 bucket reads. The wildcard s3:GetBucket* was decomposed from observed
    # AUTHORIZED calls, which includes calls returning benign not-found errors —
    # filtering those out as "failures" is exactly how Gate 4N-I2 lost six actions.
    "s3_bucket": sorted(
        [
            "s3:GetAccelerateConfiguration",
            "s3:GetBucketAcl",
            "s3:GetBucketCORS",
            "s3:GetBucketLogging",
            "s3:GetBucketObjectLockConfiguration",
            "s3:GetBucketOwnershipControls",
            "s3:GetBucketPolicy",
            "s3:GetBucketPublicAccessBlock",
            "s3:GetBucketRequestPayment",
            "s3:GetBucketTagging",
            "s3:GetBucketVersioning",
            "s3:GetBucketWebsite",
            "s3:GetEncryptionConfiguration",
            "s3:GetLifecycleConfiguration",
            "s3:GetReplicationConfiguration",
            "s3:ListBucket",
            "s3:ListTagsForResource",
        ]
    ),
    "kms_exact": sorted(
        [
            "kms:DescribeKey",
            "kms:GetKeyPolicy",
            "kms:GetKeyRotationStatus",
            "kms:ListResourceTags",
        ]
    ),
    "secrets": sorted(["secretsmanager:DescribeSecret", "secretsmanager:GetResourcePolicy"]),
    "iam_read": sorted(
        [
            "iam:GetRole",
            "iam:GetRolePolicy",
            "iam:ListAttachedRolePolicies",
            "iam:ListRolePolicies",
        ]
    ),
    "route53": sorted(["route53:GetHostedZone", "route53:ListResourceRecordSets"]),
    "cloudfront_read": sorted(
        ["cloudfront:GetDistribution", "cloudfront:GetOriginAccessControl", "cloudfront:ListTagsForResource"]
    ),
    "cloudtrail_read_exact": sorted(["cloudtrail:GetTrailStatus", "cloudtrail:ListTags"]),
    "cloudtrail_read_star": ["cloudtrail:DescribeTrails"],
    # budgets:ViewBudget authorizes DescribeBudget, DescribeNotificationsForBudget and
    # DescribeSubscribersForNotification. Gate 4N-I2 dropped it and broke module.cost.
    "budgets": sorted(["budgets:ViewBudget", "budgets:ListTagsForResource"]),
}

# Actions permanent W0 must never effectively hold, regardless of any future Allow.
# Capabilities the temporary operator legitimately needs on SPECIFIC resources. They are
# excluded from the flat ceiling and re-denied by NotResource fences. iam:PassRole is NOT
# among them: stage_a_create_closure requires only CreateRole, PutRolePolicy and TagRole —
# the reader RUNNER needs PassRole at runtime, which is a different principal entirely.
TEMP_SCOPED_CAPABILITIES = frozenset({
    "s3:GetObject",        # state_backend_closure.read
    "s3:PutObject",        # state_backend_closure.write_apply_only
    "dynamodb:GetItem",    # state lock inspect
    "dynamodb:PutItem",    # state lock acquire
    "dynamodb:DeleteItem",  # state lock release
    "kms:Decrypt",         # the state CMK, to read the encrypted state object
    # iam:CreateRole was removed from this set in Gate 4N-I9: CreateRole accepts an
    # AssumeRolePolicyDocument that AWS has NO condition key over, so an approved role NAME
    # could still be created with attacker-chosen trust outliving the window. It stays
    # flatly denied.
    #
    # GATE 4N-I16 DEFECT 3. iam:PutRolePolicy is NOT in that category and is fenced back in.
    # The composition declares six aws_iam_role_policy resources; creating an inline-policy
    # resource calls PutRolePolicy whether or not the role pre-exists, so an ordinary Stage-A
    # apply cannot complete without it. Gate 4N-I15 hid that by EXCLUDING the action from the
    # closure check on the false premise that it applies only to pre-existing roles — while
    # this file and gen_role_bootstrap_policy.py each disclaimed it by pointing at the other.
    #
    # Why fencing it is safe, and why that safety is now guaranteed rather than hoped for:
    # PutRolePolicy accepts no trust document and creates no principal. It supports the
    # iam:PermissionsBoundary condition key, so the grant below fires only when the target
    # role carries the reviewed ceiling — and the target's effective permissions are
    # identity AND boundary. Gate 4N-I16 Defect 1 additionally rejects any Stage-A bootstrap
    # at plan time unless the boundary state is BOUNDARY_ENFORCED, so no configuration
    # reaches this grant with an unbounded role.
    "iam:PutRolePolicy",
})

# --- INFRA-9 B-3 (2026-08-16): the permanent apply identity -------------------------------
#
# The Stage-A barrier established that the APPLY identity is W0 itself, not another expiring
# operator, so W0 carries the state-backend closure and the Stage-A/B task-definition
# registration — exact-scoped and fenced. Two collections, deliberately SEPARATE from
# REFRESH_CLOSURE: action_classifier.REFRESH_OBSERVED_READS flattens REFRESH_CLOSURE into
# zero-write observation evidence, so a write action inserted there would classify READ_ONLY
# on false provenance and trip the forbidden-conflict detector (HAZARD 1 of the B-3 ownership
# sweep). The union of both closures is what security_collection_assurance now compares
# against the emitted policy (FLATTEN_UNION_EQUALS_POLICY_ALLOW).
#
# The action content is the ADJUDICATED minimal set from the Part-A capability adjudication
# (OpenTofu 1.12.5 vs the operator backend config, use_lockfile NOT set): the live 2026-07-27
# policy's extras — dynamodb:UpdateItem, dynamodb:DescribeTable, kms:Encrypt — are NOT
# carried. kms:DescribeKey is already granted by KmsReadExact and is not repeated here.
W0_APPLY_CLOSURE = {
    "state_bucket_read": ["s3:GetBucketLocation", "s3:ListBucket"],
    "state_object_rw": ["s3:GetObject", "s3:PutObject"],
    "state_lock": ["dynamodb:DeleteItem", "dynamodb:GetItem", "dynamodb:PutItem"],
    # ViaService-conditioned in the statement: S3 (BucketKeyEnabled) and DynamoDB call KMS on
    # the operator's behalf; W0 itself never calls KMS directly for backend work, so a direct
    # out-of-band Decrypt of the state blob stays dead even with the fence deleted.
    "state_cmk_use": ["kms:Decrypt", "kms:GenerateDataKey"],
    # ecs:TagResource travels WITH registration: the composition registers every task
    # definition carrying tags, and ECS tag-on-create performs an additional ecs:TagResource
    # authorization (Service Reference: TagResource covers the task-definition resource;
    # RegisterTaskDefinition carries aws:RequestTag/aws:TagKeys). Six-lane permissions-lane
    # finding; evidence retained in the operator evidence directory
    # (b3-part-a-live-readback/ecs-action-truth-evidence.md).
    "task_definition_register": ["ecs:RegisterTaskDefinition", "ecs:TagResource"],
    # AWS supports NO resource scoping on DescribeTaskDefinition (Service Reference,
    # Part-A adjudication) -> Resource "*" with the region condition.
    "task_definition_describe": ["ecs:DescribeTaskDefinition"],
}

# The forbidden capabilities W0 now holds SCOPED. Subtracted from the flat DenyDangerous
# union and re-denied by NotResource fences, exactly as TEMP_SCOPED_CAPABILITIES is for the
# temporary operator. iam:PassRole is deliberately NOT here: whether RegisterTaskDefinition
# performs a PassRole authorization check is recorded DISPUTED (the contract's
# _no_passrole_note and the retained evidence file carry both sides); the B-3 delta adds no
# PassRole surface either way — the fail-closed direction — and W0's flat PassRole deny is
# preserved. Resolution is a mandatory Part-B pre-flight gate, not an assumption here.
W0_SCOPED_CAPABILITIES = frozenset({
    "s3:GetObject",              # state_backend_closure.read, exact state object
    "s3:PutObject",              # state_backend_closure.write_apply_only, exact state object
    "dynamodb:GetItem",          # state lock inspect, exact lock table
    "dynamodb:PutItem",          # state lock acquire, exact lock table
    "dynamodb:DeleteItem",       # state lock release, exact lock table
    "kms:Decrypt",               # state CMK only, ViaService-conditioned
    "ecs:RegisterTaskDefinition",  # the four composition families only
    # ecs:TagResource and kms:GenerateDataKey are NOT here: neither is in the
    # PERMANENT_DENY/FORBIDDEN union, so there is nothing to subtract — but both are still
    # FENCED below so every apply-surface action is re-denied off-scope uniformly
    # (six-lane permissions-lane finding 4).
})

# The `family:*` ARN form ONLY. The Service Reference's task-definition ARNFormats entry is
# REVISION-BEARING (task-definition/${Family}:${Revision}), and this account's own CloudTrail
# shows the RegisterTaskDefinition authorization resource in exactly that form (the
# 2026-07-28T01:38:31Z AccessDenied names task-definition/<family>:*). A bare-family entry
# never matches the documented format and would be dead weight in both the Allow and the
# fence. Evidence retained: b3-part-a-live-readback/ecs-action-truth-evidence.md.
# Families come from the composition declarations — the reader family is IMPORTED (never
# rebuilt; the Gate 4N-I2 lesson); api/worker/migration are constructed from PREFIX here and
# pinned to the module source by tests/test_operator_policies.py (the asymmetry vs a NAMES
# import is acknowledged; the .tf-text pin is the drift control).
TASK_DEFINITION_FAMILY_ARNS = [
    f"arn:aws:ecs:{REGION}:{ACCOUNT}:task-definition/{family}:*"
    for family in sorted((f"{PREFIX}-api", f"{PREFIX}-migration",
                          f"{PREFIX}-worker", READER_TASK_DEFINITION_FAMILY))
]

PERMANENT_DENY = sorted(
    [
        # role minting and the escalation set
        "iam:AddClientIDToOpenIDConnectProvider",
        "iam:AttachGroupPolicy",
        "iam:AttachRolePolicy",
        "iam:AttachUserPolicy",
        "iam:CreateAccessKey",
        "iam:CreateGroup",
        "iam:CreateInstanceProfile",
        "iam:CreateLoginProfile",
        "iam:CreateOpenIDConnectProvider",
        "iam:CreatePolicy",
        "iam:CreatePolicyVersion",
        "iam:CreateRole",
        "iam:CreateServiceLinkedRole",
        "iam:CreateUser",
        "iam:DeleteOpenIDConnectProvider",
        "iam:DeleteRole",
        "iam:DeleteRolePermissionsBoundary",
        "iam:DeleteRolePolicy",
        "iam:DetachRolePolicy",
        "iam:PassRole",
        "iam:PutGroupPolicy",
        "iam:PutRolePermissionsBoundary",
        "iam:PutRolePolicy",
        "iam:PutUserPolicy",
        "iam:SetDefaultPolicyVersion",
        "iam:TagRole",
        "iam:UntagRole",
        "iam:UpdateAssumeRolePolicy",
        "iam:UpdateOpenIDConnectProviderThumbprint",
        # audit-trail integrity
        "cloudtrail:DeleteTrail",
        "cloudtrail:PutEventSelectors",
        "cloudtrail:PutInsightSelectors",
        "cloudtrail:StopLogging",
        "cloudtrail:UpdateTrail",
        # state and bucket integrity
        "s3:DeleteBucket",
        "s3:DeleteBucketPolicy",
        "s3:DeleteObject",
        "s3:DeleteObjectVersion",
        "s3:GetObject",
        "s3:GetObjectVersion",
        "s3:PutBucketPolicy",
        "s3:PutBucketReplication",
        "s3:PutBucketPublicAccessBlock",
        "s3:PutBucketVersioning",
        "s3:PutEncryptionConfiguration",
        "s3:PutLifecycleConfiguration",
        "s3:PutObject",
        # data planes and secret material
        "dynamodb:DeleteItem",
        "dynamodb:GetItem",
        "dynamodb:PutItem",
        "dynamodb:Query",
        "dynamodb:Scan",
        "dynamodb:UpdateItem",
        "kms:CreateGrant",
        "kms:DisableKey",
        "kms:PutKeyPolicy",
        "kms:ScheduleKeyDeletion",
        "rds-data:*",
        "rds:DeleteDBInstance",
        "rds:ModifyDBInstance",
        "secretsmanager:GetSecretValue",
        "secretsmanager:PutResourcePolicy",
        "secretsmanager:PutSecretValue",
        "secretsmanager:UpdateSecret",
        # account and identity administration
        "ecs:CreateService",
        "ecs:ExecuteCommand",
        "ecs:RegisterTaskDefinition",
        "ecs:RunTask",
        "ecs:StartTask",
        "ecs:UpdateService",
        "identitystore:*",
        "logs:FilterLogEvents",
        "logs:GetLogEvents",
        "logs:StartQuery",
        "organizations:*",
        "sso:*",
        "sts:AssumeRole",
    ]
)


def permanent_w0_policy() -> dict:
    """Read-mostly diagnostics PLUS the exact-scoped, fenced apply surface.

    The Phase F decision ("no state access, no mutation of any kind") was superseded by the
    INFRA-9 B-3 apply-identity adjudication (2026-08-16): the Stage-A barrier established
    that the apply identity is W0 itself, so W0 carries the state-backend closure and
    task-definition registration — on exactly the backend resources and composition
    families, with every carved capability re-denied everywhere else by a NotResource
    fence. The flat DenyDangerous ceiling stays unconditional and global over everything
    NOT deliberately carved.
    """
    c = REFRESH_CLOSURE
    w = W0_APPLY_CLOSURE
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "EstateReadRegional",
                "Effect": "Allow",
                "Action": c["star_regional"],
                "Resource": "*",
                "Condition": REGION_COND,
            },
            {"Sid": "RdsDescribeInstancesStar", "Effect": "Allow", "Action": c["rds_star"], "Resource": "*", "Condition": REGION_COND},
            {"Sid": "RdsReadExact", "Effect": "Allow", "Action": c["rds_exact"], "Resource": [ARN["db"], ARN["pg"], ARN["subgrp"]]},
            {"Sid": "BucketReadWorkload", "Effect": "Allow", "Action": c["s3_bucket"], "Resource": WORKLOAD_BUCKETS},
            {"Sid": "KmsReadExact", "Effect": "Allow", "Action": c["kms_exact"], "Resource": [ARN["cmk_state"], ARN["cmk_secrets"]]},
            {"Sid": "SecretsMetadataRead", "Effect": "Allow", "Action": c["secrets"], "Resource": f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:{PREFIX}/*"},
            {"Sid": "IamRoleRead", "Effect": "Allow", "Action": c["iam_read"], "Resource": f"arn:aws:iam::{ACCOUNT}:role/{PREFIX}-*"},
            {"Sid": "Route53Read", "Effect": "Allow", "Action": c["route53"], "Resource": identity.route53_hosted_zone_arn()},
            # CloudFront is GLOBAL: its ARNs carry no region, so a region condition here
            # would be vacuous. Scope by exact ARN instead.
            {"Sid": "CloudFrontRead", "Effect": "Allow", "Action": c["cloudfront_read"], "Resource": [ARN["distribution"], ARN["oac"]]},
            {"Sid": "AuditTrailReadExact", "Effect": "Allow", "Action": c["cloudtrail_read_exact"], "Resource": ARN["trail"]},
            {"Sid": "AuditTrailListStar", "Effect": "Allow", "Action": c["cloudtrail_read_star"], "Resource": "*", "Condition": REGION_COND},
            {"Sid": "BudgetsRead", "Effect": "Allow", "Action": c["budgets"], "Resource": f"arn:aws:budgets::{ACCOUNT}:budget/*"},
            # --- INFRA-9 B-3: the apply surface. Exact backend resources; no expiry — this
            # is the PERMANENT apply identity, reviewed as such. -----------------------------
            {"Sid": "StateBucketRead", "Effect": "Allow", "Action": w["state_bucket_read"], "Resource": ARN["state_bucket"]},
            {"Sid": "StateObjectReadWrite", "Effect": "Allow", "Action": w["state_object_rw"], "Resource": ARN["state_object"]},
            {"Sid": "StateLock", "Effect": "Allow", "Action": w["state_lock"], "Resource": ARN["lock"]},
            # ViaService: only S3 (BucketKeyEnabled) and DynamoDB may use the state CMK on
            # W0's behalf. A direct kms:Decrypt of the state blob by the operator's own
            # credentials never matches this statement.
            {"Sid": "StateCmkUseViaBackendServices", "Effect": "Allow", "Action": w["state_cmk_use"], "Resource": ARN["cmk_state"],
             "Condition": {"StringEquals": {"kms:ViaService": [
                 f"dynamodb.{REGION}.amazonaws.com", f"s3.{REGION}.amazonaws.com"]}}},
            {"Sid": "TaskDefinitionFamiliesRegister", "Effect": "Allow", "Action": w["task_definition_register"], "Resource": TASK_DEFINITION_FAMILY_ARNS},
            {"Sid": "TaskDefinitionDescribeStar", "Effect": "Allow", "Action": w["task_definition_describe"], "Resource": "*", "Condition": REGION_COND},
            # Union with the must-not contract. The hand-maintained PERMANENT_DENY list
            # scored 37/39 on the Allow-axis proof: rds:RestoreDBInstanceFromDBSnapshot and
            # secretsmanager:DeleteSecret were absent.
            #
            # INFRA-9 B-3: W0 now has scoped exemptions — W0_SCOPED_CAPABILITIES is
            # subtracted from the flat ceiling and re-denied by the NotResource fences
            # below, exactly as TempDenyEscalation does with TEMP_SCOPED_CAPABILITIES.
            # Everything else remains denied flatly, unconditionally, at Resource "*".
            {"Sid": "DenyDangerous", "Effect": "Deny",
             "Action": sorted((set(PERMANENT_DENY) | set(FORBIDDEN_CAPABILITIES))
                              - W0_SCOPED_CAPABILITIES),
             "Resource": "*"},
            # --- NotResource fences for the carved capabilities. A flat Deny would kill the
            # capability; a bare Allow would leave it implicit-denied elsewhere, which another
            # attached policy could lift. The fence is the idiom that does neither, and it is
            # what keeps the universal Resource-"*" invariant probes at EXPLICIT_DENY. -------
            {"Sid": "DenyStateObjectAccessOutsideTheStateObject", "Effect": "Deny",
             "Action": w["state_object_rw"], "NotResource": ARN["state_object"]},
            {"Sid": "DenyLockItemsOutsideTheLockTable", "Effect": "Deny",
             "Action": w["state_lock"], "NotResource": ARN["lock"]},
            # kms:Decrypt reaches the SECRETS CMK too unless fenced, and that CMK protects
            # the database credential this principal must never read. kms:GenerateDataKey is
            # fenced with it (permissions-lane finding 4): it is not forbidden, but the
            # apply surface's "re-denied everywhere else" property is kept uniform.
            {"Sid": "DenyStateCmkUseOutsideTheStateCmk", "Effect": "Deny",
             "Action": w["state_cmk_use"], "NotResource": ARN["cmk_state"]},
            {"Sid": "DenyTaskDefinitionRegistrationOutsideTheFamilies", "Effect": "Deny",
             "Action": w["task_definition_register"], "NotResource": TASK_DEFINITION_FAMILY_ARNS},
        ],
    }


def bootstrap_temp_policy(expiry: str, *, issuance: str | None = None) -> dict:
    """`expiry` is REQUIRED. See Gate 4N-I8 Defect 3: the placeholder default is gone."""
    # GATE 4N-I19, ADV-A. The window must be AUTHORIZED, not merely well-formed. Gate 4N-I17
    # showed a 2099 stamp generating cleanly with the whole suite green; this call is what
    # makes an unbounded or already-expired window fail BEFORE any policy output exists.
    import expiry_authorization

    expiry_authorization.authorize(
        issuance=issuance if issuance is not None else expiry_authorization.ACTIVE_ISSUANCE_UTC,
        expiry=expiry, purpose="stage_a_operator")

    require_valid_expiry(expiry)
    """Standalone expiring operator: the full read closure PLUS the bootstrap mutations.

    This is a SEPARATE permission set, never an attachment to W0 — permanent explicit
    Denies cannot be overridden by an attached Allow on the same principal.
    """
    c = REFRESH_CLOSURE
    exp = {"DateLessThan": {"aws:CurrentTime": expiry}}

    def expiring(cond: dict | None = None) -> dict:
        merged = dict(exp)
        if cond:
            merged.update(cond)
        return merged

    return {
        "Version": "2012-10-17",
        "Statement": [
            # --- the full refresh closure, standalone (inherits nothing from W0) ---
            {"Sid": "TempEstateReadRegional", "Effect": "Allow", "Action": c["star_regional"], "Resource": "*", "Condition": expiring(REGION_COND)},
            {"Sid": "TempRdsDescribeInstancesStar", "Effect": "Allow", "Action": c["rds_star"], "Resource": "*", "Condition": expiring(REGION_COND)},
            {"Sid": "TempRdsReadExact", "Effect": "Allow", "Action": c["rds_exact"], "Resource": [ARN["db"], ARN["pg"], ARN["subgrp"]], "Condition": exp},
            {"Sid": "TempBucketRead", "Effect": "Allow", "Action": c["s3_bucket"], "Resource": WORKLOAD_BUCKETS + [ARN["state_bucket"]], "Condition": exp},
            {"Sid": "TempKmsRead", "Effect": "Allow", "Action": c["kms_exact"], "Resource": [ARN["cmk_state"], ARN["cmk_secrets"]], "Condition": exp},
            {"Sid": "TempSecretsMetadataRead", "Effect": "Allow", "Action": c["secrets"], "Resource": f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:{PREFIX}/*", "Condition": exp},
            # Read-back after CreateRole. iam:ListRoleTags is included here but NOT in the
            # permanent policy: provider v6.55.0 internal/service/iam/role.go defines
            # roleTags() -> ListRoleTags, and a create-then-read of a tagged role can take
            # that path. It was never observed during refresh of the ALREADY-EXISTING roles,
            # which is why permanent W0 omits it — absence there is evidence; absence on a
            # path no reader role has ever exercised is not.
            {"Sid": "TempIamRoleRead", "Effect": "Allow", "Action": sorted(c["iam_read"] + ["iam:ListRoleTags"]), "Resource": f"arn:aws:iam::{ACCOUNT}:role/{PREFIX}-*", "Condition": exp},
            {"Sid": "TempRoute53Read", "Effect": "Allow", "Action": c["route53"], "Resource": identity.route53_hosted_zone_arn(), "Condition": exp},
            {"Sid": "TempCloudFrontRead", "Effect": "Allow", "Action": c["cloudfront_read"], "Resource": [ARN["distribution"], ARN["oac"]], "Condition": exp},
            {"Sid": "TempAuditTrailReadExact", "Effect": "Allow", "Action": c["cloudtrail_read_exact"], "Resource": ARN["trail"], "Condition": exp},
            {"Sid": "TempAuditTrailListStar", "Effect": "Allow", "Action": c["cloudtrail_read_star"], "Resource": "*", "Condition": expiring(REGION_COND)},
            {"Sid": "TempBudgetsRead", "Effect": "Allow", "Action": c["budgets"], "Resource": f"arn:aws:budgets::{ACCOUNT}:budget/*", "Condition": exp},
            # --- backend: state read, state write, lock ---
            {"Sid": "TempStateBucketRead", "Effect": "Allow", "Action": ["s3:ListBucket", "s3:GetBucketLocation"], "Resource": ARN["state_bucket"], "Condition": exp},
            {"Sid": "TempStateObject", "Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject"], "Resource": ARN["state_object"], "Condition": exp},
            {"Sid": "TempStateLock", "Effect": "Allow", "Action": ["dynamodb:DeleteItem", "dynamodb:GetItem", "dynamodb:PutItem"], "Resource": ARN["lock"], "Condition": exp},
            {"Sid": "TempStateCmkUse", "Effect": "Allow", "Action": ["kms:Decrypt", "kms:DescribeKey", "kms:GenerateDataKey"], "Resource": ARN["cmk_state"], "Condition": exp},
            # --- reader role creation: exact names, boundary REQUIRED ---------------
            # iam:CreateRole and iam:PutRolePolicy DO support the iam:PermissionsBoundary
            # condition key; iam:TagRole DOES NOT, so it lives in its own statement. In
            # Gate 4N-I2 all three shared one conditioned statement, which meant TagRole
            # could never match and role creation would have failed.


            # --- inline policies for the composition's roles (Gate 4N-I16 Defect 3) --
            #
            # EXACT role ARNs, not the `signalnest-staging-*` prefix used by the READ grant
            # above: a write grant is scoped to the roles the composition actually declares
            # inline policies for. The iam:PermissionsBoundary condition means this cannot
            # write a policy into a role that is not carrying the reviewed ceiling.
            # GATE 4N-I17 DEFECT 3. Two corrections to what Gate 4N-I16 shipped here.
            #
            # (a) iam:DeleteRolePolicy is GONE. It was Allowed here AND denied unconditionally by
            #     TempDenyEscalation, so the grant was dead on every one of its own resources —
            #     an Allow that evaluates EXPLICIT_DENY. The classification is OBSOLETE, not
            #     required-and-broken: provider-api-operation-map.json maps aws_iam_role_policy to
            #     {read: GetRolePolicy, create: PutRolePolicy} and has NO delete axis at all, so no
            #     declared operation in this composition invokes it. The correct repair is to stop
            #     granting it, not to carve it out of the deny.
            #
            # (b) iam:GetRolePolicy is GONE from this statement. It is a READ, and reads do not
            #     populate iam:PermissionsBoundary, so conditioning it on that key produced a
            #     statement that could never match. It is already granted unconditionally by
            #     TempIamRoleRead below, which is where a read belongs.
            #
            # What remains is one write action, on exactly the roles the .tf files declare inline
            # policies for, gated on the reviewed boundary, and expiring.
            {"Sid": "TempInlineRolePolicyBounded", "Effect": "Allow", "Action": ["iam:PutRolePolicy"], "Resource": INLINE_POLICY_ROLE_ARNS, "Condition": expiring({"StringEquals": {"iam:PermissionsBoundary": ARN["boundary"]}})},
            # --- reader ECR repository: note the SLASH in the repository path --------
            {"Sid": "TempReaderEcr", "Effect": "Allow", "Action": ["ecr:CreateRepository", "ecr:PutLifecyclePolicy", "ecr:TagResource", "ecr:PutImageScanningConfiguration", "ecr:PutImageTagMutability", "ecr:DescribeRepositories", "ecr:GetLifecyclePolicy", "ecr:ListTagsForResource"], "Resource": ARN["reader_ecr"], "Condition": exp},
            # NOTE: NO audit-bucket or CloudTrail grant. Live evidence shows the trail
            # has been logging since 2026-07-27 and the audit bucket policy and PAB are
            # already converged, so nothing needs converging. Gate 4N-I3 granted them
            # anyway, covering only 2 of 6 module-owned observability resources and
            # opening a path to halt log delivery without calling cloudtrail:StopLogging.
            # A future observability rebuild needs its OWN operator covering all six.
            # --- internal ceiling: the temporary operator cannot exceed its purpose --
            # --- internal ceiling ----------------------------------------------------
            #
            # DERIVED from scripts/must_not_contract.py, not hand-listed. The hand-written
            # list this replaces covered 25 actions and the Allow-axis proof scored this
            # principal 20/39: kms:ScheduleKeyDeletion, kms:PutKeyPolicy, kms:CreateGrant,
            # secretsmanager:PutSecretValue, secretsmanager:DeleteSecret, s3:DeleteObject,
            # s3:DeleteObjectVersion, s3:PutBucketPolicy, ecs:ExecuteCommand,
            # ecs:UpdateService, iam:CreateAccessKey, iam:DeleteRolePermissionsBoundary and
            # rds:RestoreDBInstanceFromDBSnapshot were all missing. Deriving the list means
            # a capability added to the contract is denied here with no edit to this file.
            #
            # The four capabilities this principal genuinely needs are excluded here and
            # re-denied below by NotResource fences, so they survive on exactly the
            # resources the closure contract justifies and nowhere else.
            {
                "Sid": "TempDenyEscalation",
                "Effect": "Deny",
                "Action": sorted(
                    (set(FORBIDDEN_CAPABILITIES) | {
                        "cloudtrail:PutInsightSelectors",
                        "iam:CreatePolicy",
                        "iam:CreatePolicyVersion",
                        "iam:SetDefaultPolicyVersion",
                        "secretsmanager:GetSecretValue",
                        "identitystore:*", "organizations:*", "sso:*",
                    }) - TEMP_SCOPED_CAPABILITIES
                ),
                "Resource": "*",
            },
            # --- NotResource fences for the four scoped capabilities -----------------
            #
            # A flat Deny would win over the Allow and destroy the capability; a bare Allow
            # leaves it available everywhere by implicit denial only, which another
            # attached policy can lift. The fence is the idiom that does neither.
            {
                "Sid": "TempDenyStateObjectAccessOutsideTheStateObject",
                "Effect": "Deny",
                "Action": ["s3:GetObject", "s3:PutObject"],
                "NotResource": ARN["state_object"],
            },
            {
                "Sid": "TempDenyLockItemsOutsideTheLockTable",
                "Effect": "Deny",
                "Action": ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:DeleteItem"],
                "NotResource": ARN["lock"],
            },
            {
                # kms:Decrypt reaches the SECRETS CMK too unless fenced, and that CMK
                # protects the database credential this principal must never read.
                "Sid": "TempDenyDecryptOutsideTheStateCmk",
                "Effect": "Deny",
                "Action": "kms:Decrypt",
                "NotResource": ARN["cmk_state"],
            },
            {
                # GATE 4N-I9 DEFECT 1. This was a NotResource FENCE allowing role authoring
                # on the three reader roles. It is now a FLAT deny.
                #
                # iam:CreateRole accepts the AssumeRolePolicyDocument in the request, and AWS
                # has NO condition key comparing the whole submitted trust document to an
                # approved hash. So exact role-name scoping, iam:PermissionsBoundary
                # conditioning and policy-name scoping — all of which this statement had —
                # constrained what the role could DO while leaving WHO MAY ASSUME IT entirely
                # to the caller. A role created with an external-account or wildcard trust
                # SURVIVES this operator's expiry.
                #
                # The capability moved to a separate, minimal RoleBootstrapOperator whose
                # safety rests on exact reviewed trust files plus mandatory post-create
                # read-back — detect-and-revert, because AWS offers no prevent here.
                # GATE 4N-I16 DEFECT 3. iam:PutRolePolicy is removed from this Deny and
                # granted above under TempInlineRolePolicyBounded. The reasoning recorded
                # immediately above is specifically about the TRUST DOCUMENT: CreateRole and
                # UpdateAssumeRolePolicy decide WHO MAY ASSUME a role, AWS has no condition
                # key over that document, and a role created with external-account trust
                # SURVIVES this operator's expiry. PutRolePolicy decides what a role may DO,
                # not who may assume it; it creates no principal, accepts no trust document,
                # and it supports iam:PermissionsBoundary, so the grant above cannot even
                # reach a role that is not carrying the reviewed ceiling.
                #
                # The composition declares six aws_iam_role_policy resources. Denying the
                # action here while the closure verifier EXCLUDED it (Gate 4N-I15) meant an
                # ordinary Stage-A apply would have failed with AccessDenied after the ECR
                # resources already existed — the exact partial apply the Stage-A guards are
                # written to prevent.
                "Sid": "TempDenyAllRoleAuthoring",
                "Effect": "Deny",
                "Action": ["iam:CreateRole", "iam:TagRole",
                           "iam:UpdateAssumeRolePolicy", "iam:DeleteRole"],
                "Resource": "*",
            },
            {
                # GATE 4N-I16 DEFECT 3 — the FENCE for the inline-policy grant.
                #
                # Found by the allow-model exemption proof, which requires an exemption to be
                # EXPLICITLY denied out of scope rather than merely unmatched. Without this
                # statement the enumerated-ARN Allow left every other role at IMPLICIT_DENY,
                # including `AWSReservedSSO_AdministratorAccess_*`. Implicit denial is not
                # containment: it is the absence of a grant, and it disappears the moment any
                # other statement grants the action more broadly. This gate chain has already
                # been burned once by treating implicit denial as a control.
                #
                # NotResource FENCES: it confines the action to the enumerated roles. It does
                # not grant anything.
                "Sid": "TempDenyInlinePolicyOutsideDeclaredRoles",
                "Effect": "Deny",
                "Action": ["iam:PutRolePolicy"],
                "NotResource": INLINE_POLICY_ROLE_ARNS,
            },
        ],
    }


def require_valid_expiry(expiry: object) -> None:
    """Reject a missing, placeholder or malformed expiry at GENERATION time."""
    if expiry is None or expiry == "":
        raise ValueError("expiry is REQUIRED; there is no placeholder default")
    iam_eval.parse_iam_date(expiry, what="policy expiry")


def canonical(doc: dict) -> bytes:
    return json.dumps(doc, sort_keys=True, separators=(",", ":")).encode("utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--emit", choices=["permanent-w0", "bootstrap-temp"], default="permanent-w0")
    # GATE 4N-I10 DEFECT 4. This CLI still defaulted to "<EXPIRY-ISO8601>". Gate 4N-I8
    # removed the placeholder from the FUNCTION signature and I reported the defect closed —
    # but the command-line path, which is what actually writes reviewed artifacts to disk,
    # kept it. Every reviewed artifact produced through this entry point would have carried
    # the placeholder. Required now, with no default.
    parser.add_argument("--expiry", default=None,
                        help="RFC 3339 UTC expiry; REQUIRED with --emit bootstrap-temp")
    parser.add_argument("--hash", action="store_true", help="print canonical + file-byte hashes only")
    args = parser.parse_args()

    if args.emit == "bootstrap-temp":
        if args.expiry is None:
            parser.error("--expiry is REQUIRED with --emit bootstrap-temp; there is no default")
        doc = bootstrap_temp_policy(args.expiry)
    else:
        if args.expiry is not None:
            parser.error("--expiry is meaningless for the PERMANENT policy; it does not expire")
        doc = permanent_w0_policy()
    rendered = json.dumps(doc, indent=2, ensure_ascii=True) + "\n"

    if args.hash:
        print(f"canonical  {hashlib.sha256(canonical(doc)).hexdigest()}")
        print(f"file_byte  {hashlib.sha256(rendered.encode('utf-8')).hexdigest()}")
        print(f"statements {len(doc['Statement'])}")
        return 0

    sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
