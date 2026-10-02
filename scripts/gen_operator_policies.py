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
import re
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
    # The composition declares seven aws_iam_role_policy resources (6B-4C added the API
    # task role's SES send grant); creating an inline-policy
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
                # The composition declares seven aws_iam_role_policy resources. Denying the
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


# --- INFRA-9 B-3 Part-B remediation (2026-08-17): the ICPermAdmin provisioning delta -------
#
# ROOT CAUSE (retained: the Part-B evidence record and remediation design in the operator
# evidence directory): in this account Identity Center performs the reserved-role
# iam:PutRolePolicy write of ProvisionPermissionSet with the CALLER's credentials (a
# forward-access request), and ICPermAdmin does not hold that action — two independent
# terminal-FAILED provisioning denials (B-1 2026-08-13, B-3 Part-B M2 2026-08-17) name it
# exactly. This delta is the adjudicated minimum repair (OD-R1..OD-R5, 2026-08-17):
#
#   ONE statement. ONE action (iam:PutRolePolicy — nothing else was ever denied, and every
#   further IC-side need must arrive as its own denial and its own reviewed delta). ONE
#   exact resource (the current W0 reserved-role ARN — never a suffix or path wildcard,
#   never another role, never ICPermAdmin's own role). ONE condition
#   (aws:CalledViaFirst = sso.amazonaws.com — the write is eligible only when Identity
#   Center is the forwarding service; a direct operator call stays denied).
#
# The delta is a FRAGMENT, not a principal policy: it is merged operator-side into the
# operator-held ICPermAdmin permission-set document (capture -> merge -> canonicalize ->
# digest below), so it deliberately does not join policy_inventory/TARGETS, and the live
# document is never committed or printed — the merge path emits digests only.
#
# The reserved-role ARN is an AWS-assigned identifier and NEVER appears in the repository:
# it arrives at generation time from the approved operator-held inputs (the live get-role
# read), is validated fail-closed against the anchor-resolved W0 permission-set name, and
# is bound by a caller-supplied sha256 pin (the OD-R2 re-pin gate: assignment removal +
# recreation rotates the suffix, the pinned ARN goes dead, and a NEW reviewed pin is the
# only way forward — DOC-2 in the retained remediation-doc-evidence).
#
# The aws:CalledViaFirst population by IC's provisioning write is a HYPOTHESIS, recorded
# DISPUTED in iam_eval.DISPUTED_RUNTIME_CONTEXT: the AWS FAS documentation states the key
# is populated for forward-access requests, but no retained observation proves it for this
# path. The positive control is the CloudTrail iam:PutRolePolicy Allowed action-truth
# event bound to the authorized probe under the sole-grant premise (OD-R4 rev-3.1) — a
# SUCCEEDED provisioning status alone proves nothing, because IC may diff-and-skip the
# write under parity; a failure repeats today's exact posture (no widening) and any
# relaxation is a SEPARATE reviewed delta — never an in-place fallback.

ICPERMADMIN_DELTA_SID = "IcProvisionW0ReservedRoleInlinePolicyWrite"
IC_FORWARDING_SERVICE = "sso.amazonaws.com"


def _w0_permission_set_name() -> str:
    """The W0 permission-set name, anchor-resolved (never a repository literal)."""
    import anchor_loader
    entry = anchor_loader.load(anchor_loader.declared_tier()).anchor[
        "permission_sets"]["W0Operator"]
    name = entry.get("name") or entry.get("permission_set_name")
    if not name:
        raise ValueError("the anchor's W0Operator permission set declares no name")
    return name


def require_valid_w0_reserved_role_arn(arn: object) -> str:
    """Fail-closed validation of the operator-supplied W0 reserved-role ARN (OD-R2).

    Accepts EXACTLY the two documented reserved-role ARN forms (with and without the
    region path segment — the segment is absent when the identity source is hosted in
    us-east-1), for exactly the anchor-resolved W0 permission-set name, in exactly this
    account, with exactly a 16-lowercase-hex unique suffix. Everything else is refused:
    wildcards of any kind (a suffix wildcard would silently survive an assignment
    remove/recreate suffix rotation, which MUST fail closed instead), other permission
    sets (including ICPermAdmin's own reserved role — self-scope is a separately
    adjudicated trust decision, refused here), non-reserved-path roles, other accounts,
    and placeholder inputs. Error text deliberately never echoes the supplied value.
    """
    if not isinstance(arn, str) or not arn:
        raise ValueError("the reserved-role ARN is REQUIRED and must be a non-empty string")
    # Placeholder vocabulary is FUNCTION-LOCAL by design (the I28BH-E2 lesson: a closed
    # vocabulary used in one function must not become a module-level collection).
    if any(marker in arn for marker in ("<", ">", "{", "}", "$", "PLACEHOLDER")):
        raise ValueError("the reserved-role ARN input carries a placeholder marker")
    if any(ch in arn for ch in "*?["):
        raise ValueError(
            "the reserved-role ARN must be EXACT: wildcard characters are refused, because a "
            "pattern would silently cover a role nobody reviewed (suffix rotation fails "
            "closed by design)")
    pattern = (
        rf"arn:{re.escape(identity.PARTITION)}:iam::{re.escape(ACCOUNT)}:"
        rf"role/aws-reserved/sso\.amazonaws\.com/(?:{re.escape(REGION)}/)?"
        rf"AWSReservedSSO_{re.escape(_w0_permission_set_name())}_[0-9a-f]{{16}}"
    )
    if not re.fullmatch(pattern, arn):
        raise ValueError(
            "the supplied ARN is not the exact W0 reserved-role ARN this delta is "
            "adjudicated for (account, reserved path, anchor-resolved W0 permission-set "
            "name, 16-lowercase-hex suffix)")
    return arn


def require_pinned_w0_reserved_role_arn(arn: str, pin_sha256: object) -> str:
    """The OD-R2 re-pin gate: the ARN must match a separately supplied sha256 pin.

    The pin is operator-held (like the anchor hash), never committed: committing it would
    put a brute-forceable digest of a protected identifier into history. A suffix rotation
    changes the ARN, the pin no longer matches, and this refuses until a consciously
    re-adjudicated pin is supplied.
    """
    arn = require_valid_w0_reserved_role_arn(arn)
    if not isinstance(pin_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", pin_sha256):
        raise ValueError("the reserved-role ARN pin is REQUIRED: 64 lowercase hex characters")
    if hashlib.sha256(arn.encode("utf-8")).hexdigest() != pin_sha256:
        raise ValueError(
            "the reserved-role ARN does not match the supplied pin — if the W0 assignment "
            "was removed and recreated the suffix has rotated, and that requires a "
            "separately reviewed re-pin, not a retry")
    return arn


def icpermadmin_provisioning_delta(reserved_role_arn: str) -> dict:
    """The single adjudicated delta statement, as a standalone reviewable document."""
    arn = require_valid_w0_reserved_role_arn(reserved_role_arn)
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": ICPERMADMIN_DELTA_SID,
                "Effect": "Allow",
                "Action": ["iam:PutRolePolicy"],
                "Resource": arn,
                "Condition": {"StringEquals": {"aws:CalledViaFirst": IC_FORWARDING_SERVICE}},
            }
        ],
    }


def merge_icpermadmin_delta(captured: object, reserved_role_arn: str) -> dict:
    """Append the delta statement to the operator-captured ICPermAdmin document.

    The captured document is OPERATOR-HELD live policy content: only its SHAPE is
    validated here (it is not modelled by iam_eval and must never be committed or
    printed), its statements are preserved byte-for-byte in order, and the delta is
    appended LAST. A document already carrying the delta Sid is refused — re-running the
    merge starts from a fresh capture, never from a previous merge output.
    """
    if not isinstance(captured, dict):
        raise ValueError("the captured document must be a JSON object")
    if captured.get("Version") != "2012-10-17":
        raise ValueError("the captured document does not declare policy Version 2012-10-17")
    statements = captured.get("Statement")
    if not isinstance(statements, list) or not statements:
        raise ValueError("the captured document carries no Statement list")
    for index, stmt in enumerate(statements):
        if not isinstance(stmt, dict) or stmt.get("Effect") not in ("Allow", "Deny"):
            raise ValueError(f"captured statement {index} is malformed (missing/invalid Effect)")
        if stmt.get("Sid") == ICPERMADMIN_DELTA_SID:
            raise ValueError(
                "the captured document already carries the delta Sid — merge from a FRESH "
                "capture, never from a previous merge output")
    delta_statement = icpermadmin_provisioning_delta(reserved_role_arn)["Statement"][0]
    return {**captured, "Statement": [*statements, delta_statement]}



# --- P6-W0-TRANSITION (2026-10-02): the SEPARATE, EXPIRING window-transition principal ----------
#
# Operator selections D1 = A, D3, D4, D5 (sealed preparation set P6-W0-TRANSITION-PERMS-prep,
# manifest sha256 def6cfd7…): W0 is restored to the reviewed baseline (permanent_w0_policy above,
# unchanged byte-for-byte) and the staging-window OPEN/CLOSE transitions run under a NEW permission
# set that holds exactly the window closure and nothing else. Three documents are emitted because
# the complete closure does not fit the IAM Identity Center permission-set inline quota (10,240
# non-whitespace characters, == the IAM role aggregate inline limit; verified against the IAM and
# Identity Center quota pages in the sealed DOC-VERIFICATION):
#
#   window_transition_inline_policy        the permission set's INLINE policy: state backend +
#                                          window writes + RDS stop/start (D4) + the ONE bounded
#                                          IAM write (D5) + the NotResource fences — every Allow
#                                          expiring (D3)
#   window_transition_read_closure_policy  CUSTOMER MANAGED policy 1: the full refresh read closure
#                                          (REFRESH_CLOSURE, identical action sets to W0) plus the two
#                                          reads the window needs beyond it — every Allow expiring
#   window_transition_deny_ceiling_policy  CUSTOMER MANAGED policy 2: the flat DenyDangerous ceiling
#                                          (PERMANENT_DENY ∪ FORBIDDEN_CAPABILITIES minus the seven
#                                          scoped capabilities re-denied by the fences) — NOT expiring
#   window_transition_effective_policy     the three concatenated: what the reserved role EFFECTIVELY
#                                          evaluates. This is what the allow-model ceiling proof, the
#                                          deny probes, the action classifier and the deny-mutation
#                                          hook consume; it is never provisioned as one document.
#
# Scoping follows the sealed derivation: every write is bound to the exact window resources by the
# module-deterministic names (Name tag or resource name derived from name_prefix) and, where AWS
# offers them, by request/resource condition keys verified in the Service Authorization Reference;
# the ONE Route 53 write is confined to the ONE record name/type/actions by the three record
# condition keys; the ONE IAM write is bound to the api-task role AND conditioned on the reviewed
# permissions boundary; RDS stop/start name the ONE instance ARN and rds:CreateDBSnapshot stays in
# the ceiling, so the runbook's optional dated-snapshot flag is NOT available to this principal.
#
# Two operator-held inputs reach these documents at generation time and are NEVER committed:
#   api_fqdn   the tfvars API hostname (route53 normalized record name: lowercase, no trailing dot)
#   expiry     the window's expiry (RFC 3339 UTC), authorized against the issuance by
#              expiry_authorization.authorize(purpose="window_transition") — ≤ 24 h, ≥ 15 min.
# The runbook's D3 rule "no plan or apply may START within three hours of expiry" is a scheduling
# rule the policy cannot express; it is a constant here so the window form and tests pin ONE value.

import datetime as _datetime

WINDOW_PRINCIPAL_PURPOSE = "window_transition"
WINDOW_NO_START_BEFORE_EXPIRY = _datetime.timedelta(hours=3)
WINDOW_SESSION_DURATION = "PT12H"  # set at CreatePermissionSet; the reserved role is created with 12 h

# IAM quotas the emitted documents are measured against (characters, whitespace excluded — the
# canonical rendering has none, so len(canonical(doc)) IS the IAM-counted size).
IAM_ROLE_INLINE_POLICY_MAX_CHARS = 10240
IAM_MANAGED_POLICY_MAX_CHARS = 6144

# route53:ChangeResourceRecordSetsNormalizedRecordNames rules (Route 53 developer guide): lowercase,
# no trailing dot, labels of a-z 0-9 - _ joined by dots (other characters need octal escapes, which
# this design refuses rather than encodes — an api_fqdn is a plain hostname by the root's own
# variables.tf validation).
_API_FQDN_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$")

# The window write surface, grouped by the module resource it serves. Resource scopes and
# conditions are applied in window_transition_inline_policy; this collection is the ACTION truth
# the contract (window_transition_closure) is compared against.
WINDOW_TRANSITION_WRITES = {
    # module.network: EIP + NAT gateway + private default route (OPEN create / CLOSE destroy)
    "network_create": ["ec2:AllocateAddress", "ec2:CreateNatGateway", "ec2:CreateRoute", "ec2:CreateTags"],
    "network_destroy": ["ec2:DeleteNatGateway", "ec2:DeleteRoute", "ec2:DisassociateAddress", "ec2:ReleaseAddress"],
    # module.alb: load balancer + target group + HTTPS listener (+ provider attribute calls after create)
    "alb_load_balancer": ["elasticloadbalancing:CreateLoadBalancer", "elasticloadbalancing:DeleteLoadBalancer",
                          "elasticloadbalancing:ModifyLoadBalancerAttributes"],
    "alb_target_group": ["elasticloadbalancing:CreateTargetGroup", "elasticloadbalancing:DeleteTargetGroup",
                         "elasticloadbalancing:ModifyTargetGroupAttributes"],
    "alb_listener": ["elasticloadbalancing:CreateListener", "elasticloadbalancing:DeleteListener",
                     "elasticloadbalancing:ModifyListenerAttributes"],
    "alb_tag_on_create": ["elasticloadbalancing:AddTags"],
    # module.data_cache: the replication group only (subnet/parameter groups and the SG persist)
    "cache_create": ["elasticache:CreateReplicationGroup"],
    "cache_destroy_and_tag": ["elasticache:AddTagsToResource", "elasticache:DeleteReplicationGroup"],
    # module.alb: the windowed API alias record (hosted-zone scope) + change polling (change scope)
    "dns_record": ["route53:ChangeResourceRecordSets"],
    "dns_change_poll": ["route53:GetChange"],
    # first-apply carry-overs that ride the next apply of this root (not window resources)
    "budget_update": ["budgets:ModifyBudget"],
    "api_role_policy_write": ["iam:PutRolePolicy"],
    # D4: RDS stop/start inside the window principal; outside OpenTofu (runbook §5)
    "rds_stop_start": ["rds:StartDBInstance", "rds:StopDBInstance"],
}

# Reads the window needs beyond REFRESH_CLOSURE: the provider's ALB-destroy ENI clean-up LOOKS for
# lingering interfaces (the Detach/Delete writes stay ungranted — their denial is an expected,
# non-fatal WARN, runbook §3), and the reader task definition is gated by the READER flags, not by
# deploy_workload, so the read W0 already holds at "*" is kept rather than dropped on an assumption.
WINDOW_READ_ADDITIONS = {
    "alb_eni_cleanup_read": ["ec2:DescribeNetworkInterfaces"],
    "task_definition_describe": list(W0_APPLY_CLOSURE["task_definition_describe"]),
}

# The forbidden capabilities the window principal holds SCOPED: subtracted from its flat ceiling
# and re-denied everywhere else by NotResource fences (the W0 / temporary-operator idiom).
# ecs:RegisterTaskDefinition is NOT here — the window principal registers nothing (the workload
# stage belongs to the separately designed cutover principal), so it stays flatly denied.
WINDOW_SCOPED_CAPABILITIES = frozenset({
    "s3:GetObject",        # state_backend_closure.read, exact state object
    "s3:PutObject",        # state_backend_closure.write_apply_only, exact state object
    "dynamodb:GetItem",    # lock inspect, exact lock table
    "dynamodb:PutItem",    # lock acquire, exact lock table
    "dynamodb:DeleteItem",  # lock release, exact lock table
    "kms:Decrypt",         # state CMK only, ViaService-conditioned
    "iam:PutRolePolicy",   # D5: the #194 api_ses_send create — ONE role, boundary-conditioned, fenced
})


def _window_names() -> dict:
    """Module-deterministic names the window scopes bind to (pinned to the .tf text by tests)."""
    return {
        "nat_eip_name": f"{PREFIX}-nat-eip",
        "nat_name": f"{PREFIX}-nat",
        "private_rt_name": f"{PREFIX}-private-rt",
        "public_subnet_glob": f"{PREFIX}-public-*",
        "vpc_name": f"{PREFIX}-vpc",
        "alb_name": f"{PREFIX}-alb",
        "api_tg_name": f"{PREFIX}-api-tg",
        "redis_group": f"{PREFIX}-redis",
        "redis_params": f"{PREFIX}-redis-params",
        "redis_subnets": f"{PREFIX}-redis-subnets",
        "redis_member_glob": f"{PREFIX}-redis-00*",
        "budget_name": f"{PREFIX}-monthly",
        "api_task_role_arn": identity.iam_role_arn(f"{PREFIX}-api-task"),
    }


def window_resource_arns() -> dict:
    """The exact resource scopes of the window principal, as ARN strings/patterns."""
    n = _window_names()
    ec2 = lambda rt: f"arn:{identity.PARTITION}:ec2:{REGION}:{ACCOUNT}:{rt}"  # noqa: E731
    elb = lambda path: f"arn:{identity.PARTITION}:elasticloadbalancing:{REGION}:{ACCOUNT}:{path}"  # noqa: E731
    cache = lambda rt, name: f"arn:{identity.PARTITION}:elasticache:{REGION}:{ACCOUNT}:{rt}:{name}"  # noqa: E731
    return {
        "elastic_ip": ec2("elastic-ip/*"), "natgateway": ec2("natgateway/*"), "route_table": ec2("route-table/*"),
        "subnet": ec2("subnet/*"), "vpc": ec2("vpc/*"), "network_interface": ec2("network-interface/*"),
        "load_balancer": elb(f"loadbalancer/app/{n['alb_name']}/*"),
        "target_group": elb(f"targetgroup/{n['api_tg_name']}/*"),
        "listener": elb(f"listener/app/{n['alb_name']}/*/*"),
        "replication_group": cache("replicationgroup", n["redis_group"]),
        "parameter_group": cache("parametergroup", n["redis_params"]),
        "subnet_group": cache("subnetgroup", n["redis_subnets"]),
        "member_clusters": cache("cluster", n["redis_member_glob"]),
        "hosted_zone": identity.route53_hosted_zone_arn(),
        "change": f"arn:{identity.PARTITION}:route53:::change/*",
        "budget": f"arn:{identity.PARTITION}:budgets::{ACCOUNT}:budget/{n['budget_name']}",
        "api_task_role": n["api_task_role_arn"],
        "db": ARN["db"],
    }


def require_valid_api_fqdn(api_fqdn: object) -> str:
    """Fail-closed validation of the operator-held API hostname (the route53 normalized name)."""
    if not isinstance(api_fqdn, str) or not api_fqdn:
        raise ValueError("api_fqdn is REQUIRED and must be a non-empty string; there is no default")
    if any(marker in api_fqdn for marker in ("<", ">", "{", "}", "$", "PLACEHOLDER")):
        raise ValueError("api_fqdn carries a placeholder marker")
    if api_fqdn != api_fqdn.lower() or api_fqdn.endswith("."):
        raise ValueError("api_fqdn must be the route53 NORMALIZED record name: lowercase, no trailing dot")
    if len(api_fqdn) > 253 or not _API_FQDN_RE.fullmatch(api_fqdn):
        raise ValueError("api_fqdn must be a valid multi-label hostname (labels 1-63 of a-z 0-9 -, no empty "
                         "labels, no scheme/port/path; characters outside that set are refused, not escaped)")
    return api_fqdn


def _window_authorize(expiry: str, issuance: str | None) -> None:
    """The window must be AUTHORIZED, not merely well-formed (Gate 4N-I19 rule, same as bootstrap-temp)."""
    import expiry_authorization

    expiry_authorization.authorize(
        issuance=issuance if issuance is not None else expiry_authorization.ACTIVE_ISSUANCE_UTC,
        expiry=expiry, purpose=WINDOW_PRINCIPAL_PURPOSE)
    require_valid_expiry(expiry)


def latest_window_start(expiry: str) -> str:
    """D3: the last instant a plan or apply may START under this expiry (expiry − 3 h)."""
    parsed = iam_eval.parse_iam_date(expiry, what="window expiry")
    return (parsed - WINDOW_NO_START_BEFORE_EXPIRY).strftime("%Y-%m-%dT%H:%M:%SZ")


def _expiring(expiry: str, cond: dict | None = None) -> dict:
    merged = {"DateLessThan": {"aws:CurrentTime": expiry}}
    for op, kv in (cond or {}).items():
        merged.setdefault(op, {}).update(kv)
    return merged


def _window_read_statements(expiry: str) -> list[dict]:
    c = REFRESH_CLOSURE
    e = lambda cond=None: _expiring(expiry, cond)  # noqa: E731
    return [
        {"Sid": "WinEstateReadRegional", "Effect": "Allow", "Action": c["star_regional"], "Resource": "*", "Condition": e(REGION_COND)},
        {"Sid": "WinRdsDescribeInstancesStar", "Effect": "Allow", "Action": c["rds_star"], "Resource": "*", "Condition": e(REGION_COND)},
        {"Sid": "WinRdsReadExact", "Effect": "Allow", "Action": c["rds_exact"], "Resource": [ARN["db"], ARN["pg"], ARN["subgrp"]], "Condition": e()},
        {"Sid": "WinBucketReadWorkload", "Effect": "Allow", "Action": c["s3_bucket"], "Resource": WORKLOAD_BUCKETS, "Condition": e()},
        {"Sid": "WinKmsReadExact", "Effect": "Allow", "Action": c["kms_exact"], "Resource": [ARN["cmk_state"], ARN["cmk_secrets"]], "Condition": e()},
        {"Sid": "WinSecretsMetadataRead", "Effect": "Allow", "Action": c["secrets"], "Resource": f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:{PREFIX}/*", "Condition": e()},
        {"Sid": "WinIamRoleRead", "Effect": "Allow", "Action": c["iam_read"], "Resource": f"arn:aws:iam::{ACCOUNT}:role/{PREFIX}-*", "Condition": e()},
        {"Sid": "WinRoute53Read", "Effect": "Allow", "Action": c["route53"], "Resource": identity.route53_hosted_zone_arn(), "Condition": e()},
        {"Sid": "WinCloudFrontRead", "Effect": "Allow", "Action": c["cloudfront_read"], "Resource": [ARN["distribution"], ARN["oac"]], "Condition": e()},
        {"Sid": "WinAuditTrailReadExact", "Effect": "Allow", "Action": c["cloudtrail_read_exact"], "Resource": ARN["trail"], "Condition": e()},
        {"Sid": "WinAuditTrailListStar", "Effect": "Allow", "Action": c["cloudtrail_read_star"], "Resource": "*", "Condition": e(REGION_COND)},
        {"Sid": "WinBudgetsRead", "Effect": "Allow", "Action": c["budgets"], "Resource": f"arn:aws:budgets::{ACCOUNT}:budget/*", "Condition": e()},
        {"Sid": "WinAlbEniCleanupRead", "Effect": "Allow", "Action": WINDOW_READ_ADDITIONS["alb_eni_cleanup_read"], "Resource": "*", "Condition": e(REGION_COND)},
        {"Sid": "WinTaskDefinitionDescribeStar", "Effect": "Allow", "Action": WINDOW_READ_ADDITIONS["task_definition_describe"], "Resource": "*", "Condition": e(REGION_COND)},
    ]


def _window_inline_statements(expiry: str, api_fqdn: str) -> list[dict]:
    w = W0_APPLY_CLOSURE
    x = WINDOW_TRANSITION_WRITES
    r = window_resource_arns()
    n = _window_names()
    e = lambda cond=None: _expiring(expiry, cond)  # noqa: E731
    return [
        # --- state backend: identical scopes to the reviewed W0 apply surface ----------------
        {"Sid": "WinStateBucketRead", "Effect": "Allow", "Action": w["state_bucket_read"], "Resource": ARN["state_bucket"], "Condition": e()},
        {"Sid": "WinStateObjectReadWrite", "Effect": "Allow", "Action": w["state_object_rw"], "Resource": ARN["state_object"], "Condition": e()},
        {"Sid": "WinStateLock", "Effect": "Allow", "Action": w["state_lock"], "Resource": ARN["lock"], "Condition": e()},
        {"Sid": "WinStateCmkUseViaBackendServices", "Effect": "Allow", "Action": w["state_cmk_use"], "Resource": ARN["cmk_state"],
         "Condition": e({"StringEquals": {"kms:ViaService": [f"dynamodb.{REGION}.amazonaws.com", f"s3.{REGION}.amazonaws.com"]}})},
        # --- network: EIP, NAT gateway, private default route --------------------------------
        {"Sid": "WinEipAllocate", "Effect": "Allow", "Action": ["ec2:AllocateAddress"], "Resource": r["elastic_ip"],
         "Condition": e({"StringEquals": {"aws:RequestTag/Name": n["nat_eip_name"]}})},
        {"Sid": "WinNatCreateGateway", "Effect": "Allow", "Action": ["ec2:CreateNatGateway"], "Resource": r["natgateway"],
         "Condition": e({"StringEquals": {"aws:RequestTag/Name": n["nat_name"]}})},
        # CreateNatGateway also authorizes against the subnet, elastic-ip and vpc resources when present
        # (Service Authorization Reference): all three legs are covered so none falls to implicit deny.
        {"Sid": "WinNatCreateInputs", "Effect": "Allow", "Action": ["ec2:CreateNatGateway"], "Resource": [r["subnet"], r["elastic_ip"], r["vpc"]],
         "Condition": e({"StringLike": {"ec2:ResourceTag/Name": [n["public_subnet_glob"], n["nat_eip_name"], n["vpc_name"]]}})},
        {"Sid": "WinEc2TagOnCreate", "Effect": "Allow", "Action": ["ec2:CreateTags"], "Resource": [r["elastic_ip"], r["natgateway"]],
         "Condition": e({"StringEquals": {"ec2:CreateAction": ["AllocateAddress", "CreateNatGateway"]}})},
        {"Sid": "WinPrivateDefaultRoute", "Effect": "Allow", "Action": ["ec2:CreateRoute", "ec2:DeleteRoute"], "Resource": r["route_table"],
         "Condition": e({"StringEquals": {"ec2:ResourceTag/Name": n["private_rt_name"]}})},
        {"Sid": "WinNatDelete", "Effect": "Allow", "Action": ["ec2:DeleteNatGateway"], "Resource": r["natgateway"],
         "Condition": e({"StringEquals": {"ec2:ResourceTag/Name": n["nat_name"]}})},
        {"Sid": "WinEipRelease", "Effect": "Allow", "Action": ["ec2:ReleaseAddress"], "Resource": r["elastic_ip"],
         "Condition": e({"StringEquals": {"ec2:ResourceTag/Name": n["nat_eip_name"]}})},
        # DisassociateAddress authorizes against elastic-ip AND network-interface (both evaluated when present);
        # the NAT ENI carries no Name tag, so the interface leg is region-conditioned only — the address leg
        # keeps the Name-tag condition, so the effective grant is "disassociate the NAT EIP", never the
        # ALB-service-managed second address.
        {"Sid": "WinEipDisassociate", "Effect": "Allow", "Action": ["ec2:DisassociateAddress"], "Resource": r["elastic_ip"],
         "Condition": e({"StringEquals": {"ec2:ResourceTag/Name": n["nat_eip_name"]}})},
        {"Sid": "WinEipDisassociateInterfaceLeg", "Effect": "Allow", "Action": ["ec2:DisassociateAddress"], "Resource": r["network_interface"],
         "Condition": e(REGION_COND)},
        # --- alb: load balancer, target group, HTTPS listener --------------------------------
        {"Sid": "WinAlbLoadBalancer", "Effect": "Allow", "Action": x["alb_load_balancer"], "Resource": r["load_balancer"], "Condition": e()},
        {"Sid": "WinAlbTargetGroup", "Effect": "Allow", "Action": x["alb_target_group"], "Resource": r["target_group"], "Condition": e()},
        # CreateListener authorizes against the LOAD BALANCER resource (no listener ARN exists yet);
        # Delete/ModifyListenerAttributes authorize against listener/app.
        {"Sid": "WinAlbListenerCreate", "Effect": "Allow", "Action": ["elasticloadbalancing:CreateListener"], "Resource": r["load_balancer"], "Condition": e()},
        {"Sid": "WinAlbListener", "Effect": "Allow", "Action": ["elasticloadbalancing:DeleteListener", "elasticloadbalancing:ModifyListenerAttributes"],
         "Resource": r["listener"], "Condition": e()},
        {"Sid": "WinAlbTagOnCreate", "Effect": "Allow", "Action": x["alb_tag_on_create"], "Resource": [r["load_balancer"], r["target_group"], r["listener"]],
         "Condition": e({"StringEquals": {"elasticloadbalancing:CreateAction": ["CreateLoadBalancer", "CreateTargetGroup", "CreateListener"]}})},
        # --- data_cache: the replication group -----------------------------------------------
        # The encryption keys are REQUEST parameters of CreateReplicationGroup; a Bool on an absent key never
        # matches, so they condition the CREATE statement only. Delete + AddTagsToResource are unconditioned
        # on the same exact ARN (sealed review finding, permissions F1 / adversarial F2).
        {"Sid": "WinRedisReplicationGroupCreate", "Effect": "Allow", "Action": x["cache_create"], "Resource": r["replication_group"],
         "Condition": e({"Bool": {"elasticache:AtRestEncryptionEnabled": "true", "elasticache:TransitEncryptionEnabled": "true"}})},
        {"Sid": "WinRedisReplicationGroupDeleteTag", "Effect": "Allow", "Action": x["cache_destroy_and_tag"], "Resource": r["replication_group"], "Condition": e()},
        {"Sid": "WinRedisCreateInputs", "Effect": "Allow", "Action": x["cache_create"], "Resource": [r["parameter_group"], r["subnet_group"]], "Condition": e()},
        # Defensive (disclosed): the SAR lists `cluster` among the evaluable resources and the member cluster is
        # named <group>-001 by the service; whether it is evaluated is NOT STATED.
        {"Sid": "WinRedisMemberClusters", "Effect": "Allow", "Action": x["cache_create"] + x["cache_destroy_and_tag"], "Resource": r["member_clusters"], "Condition": e()},
        # --- Route 53: the API alias record only; hosted-zone scope and change scope are DISTINCT ----
        {"Sid": "WinApiAliasRecord", "Effect": "Allow", "Action": x["dns_record"], "Resource": r["hosted_zone"],
         "Condition": e({"ForAllValues:StringEquals": {
             "route53:ChangeResourceRecordSetsNormalizedRecordNames": [api_fqdn],
             "route53:ChangeResourceRecordSetsRecordTypes": ["A"],
             "route53:ChangeResourceRecordSetsActions": ["CREATE", "UPSERT", "DELETE"]}})},
        {"Sid": "WinRoute53ChangePoll", "Effect": "Allow", "Action": x["dns_change_poll"], "Resource": r["change"], "Condition": e()},
        # --- first-apply carry-overs -----------------------------------------------------------
        {"Sid": "WinFirstApplyBudgetBound", "Effect": "Allow", "Action": x["budget_update"], "Resource": r["budget"], "Condition": e()},
        {"Sid": "WinFirstApplyApiSesSendInlinePolicyBounded", "Effect": "Allow", "Action": x["api_role_policy_write"], "Resource": r["api_task_role"],
         "Condition": e({"StringEquals": {"iam:PermissionsBoundary": ARN["boundary"]}})},
        # --- D4: RDS stop/start on the ONE instance (no snapshot: rds:CreateDBSnapshot stays in the ceiling) ---
        {"Sid": "WinRdsStopStart", "Effect": "Allow", "Action": x["rds_stop_start"], "Resource": r["db"], "Condition": e()},
        # --- fences: the carved capabilities re-denied everywhere else (never expiring) ------------
        {"Sid": "WinDenyStateObjectAccessOutsideTheStateObject", "Effect": "Deny", "Action": w["state_object_rw"], "NotResource": ARN["state_object"]},
        {"Sid": "WinDenyLockItemsOutsideTheLockTable", "Effect": "Deny", "Action": w["state_lock"], "NotResource": ARN["lock"]},
        {"Sid": "WinDenyStateCmkUseOutsideTheStateCmk", "Effect": "Deny", "Action": w["state_cmk_use"], "NotResource": ARN["cmk_state"]},
        {"Sid": "WinDenyInlinePolicyOutsideTheApiTaskRole", "Effect": "Deny", "Action": x["api_role_policy_write"], "NotResource": r["api_task_role"]},
        {"Sid": "WinDenyRecordChangesOutsideTheConsumedZone", "Effect": "Deny", "Action": x["dns_record"], "NotResource": r["hosted_zone"]},
    ]


def _window_ceiling_statement() -> dict:
    return {"Sid": "WinDenyDangerous", "Effect": "Deny",
            "Action": sorted((set(PERMANENT_DENY) | set(FORBIDDEN_CAPABILITIES)) - WINDOW_SCOPED_CAPABILITIES),
            "Resource": "*"}


def require_window_policy_quotas(inline: dict, read_closure: dict, deny_ceiling: dict) -> dict:
    """Measure the FILLED documents against the quotas; refuse to emit an unprovisionable one."""
    sizes = {"inline": len(canonical(inline)), "read_closure": len(canonical(read_closure)),
             "deny_ceiling": len(canonical(deny_ceiling))}
    limits = {"inline": IAM_ROLE_INLINE_POLICY_MAX_CHARS, "read_closure": IAM_MANAGED_POLICY_MAX_CHARS,
              "deny_ceiling": IAM_MANAGED_POLICY_MAX_CHARS}
    over = {k: (sizes[k], limits[k]) for k in sizes if sizes[k] > limits[k]}
    if over:
        raise ValueError(f"window-transition document(s) exceed the IAM quota and cannot be provisioned: {over}")
    return {"sizes": sizes, "limits": limits}


def _require_managed_quota(doc: dict, what: str) -> dict:
    size = len(canonical(doc))
    if size > IAM_MANAGED_POLICY_MAX_CHARS:
        raise ValueError(f"the window-transition {what} document ({size} chars) exceeds the customer managed "
                         f"policy quota ({IAM_MANAGED_POLICY_MAX_CHARS}) and cannot be provisioned")
    return doc


def window_transition_read_closure_policy(expiry: str, *, issuance: str | None = None) -> dict:
    """CUSTOMER MANAGED policy 1 — the refresh read closure + the two window reads, expiring."""
    _window_authorize(expiry, issuance)
    return _require_managed_quota({"Version": "2012-10-17", "Statement": _window_read_statements(expiry)}, "read-closure")


def window_transition_deny_ceiling_policy() -> dict:
    """CUSTOMER MANAGED policy 2 — the flat ceiling. Never expires (a Deny that lapses stops protecting)."""
    return _require_managed_quota({"Version": "2012-10-17", "Statement": [_window_ceiling_statement()]}, "deny-ceiling")


def window_transition_inline_policy(expiry: str, api_fqdn: str, *, issuance: str | None = None) -> dict:
    """The permission set's INLINE policy — state backend, window writes, RDS, the one IAM write, fences."""
    _window_authorize(expiry, issuance)
    api_fqdn = require_valid_api_fqdn(api_fqdn)
    doc = {"Version": "2012-10-17", "Statement": _window_inline_statements(expiry, api_fqdn)}
    require_window_policy_quotas(doc, window_transition_read_closure_policy(expiry, issuance=issuance),
                                 window_transition_deny_ceiling_policy())
    return doc


def window_transition_effective_policy(expiry: str, api_fqdn: str, *, issuance: str | None = None) -> dict:
    """The three documents concatenated — what the reserved role EFFECTIVELY evaluates (analysis only)."""
    inline = window_transition_inline_policy(expiry, api_fqdn, issuance=issuance)
    reads = window_transition_read_closure_policy(expiry, issuance=issuance)
    ceiling = window_transition_deny_ceiling_policy()
    return {"Version": "2012-10-17", "Statement": inline["Statement"] + reads["Statement"] + ceiling["Statement"]}


def require_valid_expiry(expiry: object) -> None:
    """Reject a missing, placeholder or malformed expiry at GENERATION time."""
    if expiry is None or expiry == "":
        raise ValueError("expiry is REQUIRED; there is no placeholder default")
    iam_eval.parse_iam_date(expiry, what="policy expiry")


def canonical(doc: dict) -> bytes:
    return json.dumps(doc, sort_keys=True, separators=(",", ":")).encode("utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--emit",
                        choices=["permanent-w0", "bootstrap-temp", "icpermadmin-provisioning-delta",
                                 "window-transition-inline", "window-transition-read-closure",
                                 "window-transition-deny-ceiling", "window-transition-effective"],
                        default="permanent-w0")
    # GATE 4N-I10 DEFECT 4. This CLI still defaulted to "<EXPIRY-ISO8601>". Gate 4N-I8
    # removed the placeholder from the FUNCTION signature and I reported the defect closed —
    # but the command-line path, which is what actually writes reviewed artifacts to disk,
    # kept it. Every reviewed artifact produced through this entry point would have carried
    # the placeholder. Required now, with no default.
    parser.add_argument("--expiry", default=None,
                        help="RFC 3339 UTC expiry; REQUIRED with --emit bootstrap-temp")
    parser.add_argument("--hash", action="store_true", help="print canonical + file-byte hashes only")
    # P6-W0-TRANSITION: the window principal's operator-held inputs. --api-fqdn is REQUIRED with the
    # inline and effective emits; --issuance is the window's issuance instant (the per-window re-stamp
    # under D3 — ≤ 24 h before --expiry, enforced by expiry_authorization). Neither has a default.
    parser.add_argument("--api-fqdn", default=None,
                        help="the tfvars api_fqdn (lowercase, no trailing dot); REQUIRED with "
                             "--emit window-transition-inline|window-transition-effective")
    parser.add_argument("--issuance", default=None,
                        help="RFC 3339 UTC issuance of the window expiry (window-transition emits only); "
                             "defaults to the reviewed ACTIVE_ISSUANCE_UTC when omitted")
    # INFRA-9 B-3 Part-B remediation: the ICPermAdmin delta takes the exact W0 reserved-role
    # ARN from the approved operator-held inputs at generation time (the bootstrap-temp
    # --expiry precedent: operator-supplied, validated, refused-if-placeholder) plus the
    # OD-R2 sha256 pin. The merge path combines the delta with operator-captured
    # permission-set bytes and NEVER prints policy content — digests only.
    parser.add_argument("--reserved-role-arn", default=None,
                        help="exact W0 reserved-role ARN (operator-held input); REQUIRED "
                             "with --emit icpermadmin-provisioning-delta")
    parser.add_argument("--reserved-role-arn-pin", default=None,
                        help="sha256 of the reserved-role ARN (operator-held pin); REQUIRED "
                             "with --emit icpermadmin-provisioning-delta")
    parser.add_argument("--merge-captured", default=None,
                        help="path to operator-captured ICPermAdmin permission-set JSON; "
                             "only with --emit icpermadmin-provisioning-delta")
    parser.add_argument("--merge-output", default=None,
                        help="path to write the merged document (created 0600, never "
                             "overwritten); required with --merge-captured")

    args = parser.parse_args()

    if args.emit != "icpermadmin-provisioning-delta":
        for flag, value in (("--reserved-role-arn", args.reserved_role_arn),
                            ("--reserved-role-arn-pin", args.reserved_role_arn_pin),
                            ("--merge-captured", args.merge_captured),
                            ("--merge-output", args.merge_output)):
            if value is not None:
                parser.error(f"{flag} is only meaningful with --emit icpermadmin-provisioning-delta")

    window_emits = ("window-transition-inline", "window-transition-read-closure",
                    "window-transition-deny-ceiling", "window-transition-effective")
    if args.emit not in window_emits:
        for flag, value in (("--api-fqdn", args.api_fqdn), ("--issuance", args.issuance)):
            if value is not None:
                parser.error(f"{flag} is only meaningful with the window-transition emits")

    if args.emit == "bootstrap-temp":
        if args.expiry is None:
            parser.error("--expiry is REQUIRED with --emit bootstrap-temp; there is no default")
        doc = bootstrap_temp_policy(args.expiry)
    elif args.emit == "window-transition-deny-ceiling":
        if args.expiry is not None:
            parser.error("--expiry is meaningless for the deny ceiling; a Deny that lapses stops protecting")
        doc = window_transition_deny_ceiling_policy()
    elif args.emit in window_emits:
        if args.expiry is None:
            parser.error(f"--expiry is REQUIRED with --emit {args.emit}; there is no default")
        try:
            if args.emit == "window-transition-read-closure":
                if args.api_fqdn is not None:
                    parser.error("--api-fqdn is not an input of the read-closure document")
                doc = window_transition_read_closure_policy(args.expiry, issuance=args.issuance)
            else:
                if args.api_fqdn is None:
                    parser.error(f"--api-fqdn is REQUIRED with --emit {args.emit}; there is no default")
                build = (window_transition_inline_policy if args.emit == "window-transition-inline"
                         else window_transition_effective_policy)
                doc = build(args.expiry, args.api_fqdn, issuance=args.issuance)
        except ValueError as exc:
            parser.error(str(exc))
    elif args.emit == "icpermadmin-provisioning-delta":
        if args.expiry is not None:
            parser.error("--expiry is meaningless for the ICPermAdmin delta; the grant is "
                         "permanent and reviewed as such")
        if args.reserved_role_arn is None or args.reserved_role_arn_pin is None:
            parser.error("--reserved-role-arn and --reserved-role-arn-pin are BOTH required "
                         "with --emit icpermadmin-provisioning-delta; there are no defaults")
        try:
            arn = require_pinned_w0_reserved_role_arn(
                args.reserved_role_arn, args.reserved_role_arn_pin)
        except ValueError as exc:
            parser.error(str(exc))
        if (args.merge_captured is None) != (args.merge_output is None):
            parser.error("--merge-captured and --merge-output must be supplied together")
        if args.merge_captured is not None:
            return _merge_cli(arn, args.merge_captured, args.merge_output)
        doc = icpermadmin_provisioning_delta(arn)
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


def _merge_cli(arn: str, captured_path: str, output_path: str) -> int:
    """Capture -> merge -> canonicalize -> digest. Prints DIGESTS AND COUNTS ONLY.

    The captured and merged documents are operator-held live policy content: neither is
    ever printed, and the output file is created 0600 and never overwritten (an existing
    output must be removed deliberately, not clobbered)."""
    import os

    captured_bytes = Path(captured_path).read_bytes()
    captured = json.loads(captured_bytes)
    merged = merge_icpermadmin_delta(captured, arn)
    delta_statement = icpermadmin_provisioning_delta(arn)["Statement"][0]
    rendered = json.dumps(merged, indent=2, ensure_ascii=True) + "\n"
    fd = os.open(output_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(rendered)
    print(f"captured_file_byte  {hashlib.sha256(captured_bytes).hexdigest()}")
    print(f"captured_canonical  {hashlib.sha256(canonical(captured)).hexdigest()}")
    print(f"delta_canonical     {hashlib.sha256(canonical(delta_statement)).hexdigest()}")
    print(f"merged_canonical    {hashlib.sha256(canonical(merged)).hexdigest()}")
    print(f"merged_file_byte    {hashlib.sha256(rendered.encode('utf-8')).hexdigest()}")
    print(f"statements          {len(captured['Statement'])} -> {len(merged['Statement'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
