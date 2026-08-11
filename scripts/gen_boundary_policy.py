#!/usr/bin/env python3
"""Deterministic generator for the SignalNest role permissions boundary (Gate 4N-I6).

THE DEFECT THIS CLOSES. Gates 4N-I3 through I5 referenced
`arn:aws:iam::<account>:policy/signalnest-staging-role-boundary` in a rollout contract,
in a Stage-A precondition and in a temporary-operator condition — while the DOCUMENT
existed nowhere: not in AWS, not in the repository, not in any artifact. Nothing could be
created from reviewed bytes, hashed, versioned, or rolled back.

SHAPE: a CEILING, not a grant. `Allow *` on `*` followed by targeted Denies. A permissions
boundary never adds permission — effective = identity policy INTERSECT boundary — so the
broad Allow is not a grant; it is what stops the boundary from silently removing a
capability nobody remembered to enumerate. That is the failure mode the gate warns about:
"do not create an unusably broad Deny that breaks intended execution-role or
publisher-role permissions."

Each Deny below is scoped so that no repository-managed role loses a function it
legitimately has. The load-bearing example is `iam:PassRole`: the revision-reader RUNNER
genuinely needs it (Sid PassOnlyReaderExecutionRole), so the boundary denies PassRole
everywhere EXCEPT the exact reader execution role. A blanket PassRole deny would have
broken the runner's only job — and would have looked like good security while doing it.

Usage:
    python3 scripts/gen_boundary_policy.py [--hash]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Gate 4N-I7: identity comes from ONE authoritative source. This module must never
# reconstruct the boundary name or ARN itself — that duplication is Defect 1.
from signalnest_identity import (  # noqa: E402
    ACCOUNT, REGION, PREFIX, BOUNDARY_POLICY_NAME, BOUNDARY_POLICY_ARN,
    BOUNDARY_VERSION_ID, READER_EXECUTION_ROLE_ARN,
    # Gate 4N-I10 Defect 5: these were rebuilt HERE as independent f-strings and every test
    # then probed them from this module. Imported now; reconstructing them is a static-audit
    # failure.
    AUDIT_BUCKET_ARN, AUDIT_OBJECTS_ARN, LOCK_TABLE_ARN, READER_TASK_DEFINITION_ARNS,
    SECRETS_CMK_ARN,
    SECRETS_PREFIX_ARN, STATE_BUCKET_ARN, STATE_CMK_ARN, STATE_OBJECTS_ARN, TRAIL_ARN,
)

POLICY_NAME = BOUNDARY_POLICY_NAME
POLICY_ARN = BOUNDARY_POLICY_ARN
VERSION_ID = BOUNDARY_VERSION_ID

READER_EXECUTION_ROLE = READER_EXECUTION_ROLE_ARN
# ECS_EXECUTION_ROLE and APP_TASK_ROLES were removed in Gate 4N-I10: the Gate 4N-I7
# architect lane found them dead (never referenced by boundary_policy()) AND duplicate
# constructions of values signalnest_identity already owns.

# Imported from the authoritative critical-resource layer. Aliases only.
SECRETS_PREFIX = SECRETS_PREFIX_ARN
SECRETS_CMK = SECRETS_CMK_ARN
STATE_CMK = STATE_CMK_ARN
STATE_BUCKET = STATE_BUCKET_ARN
STATE_OBJECTS = STATE_OBJECTS_ARN
AUDIT_BUCKET = AUDIT_BUCKET_ARN
AUDIT_OBJECTS = AUDIT_OBJECTS_ARN
LOCK_TABLE = LOCK_TABLE_ARN
TRAIL = TRAIL_ARN

# ECS control-plane actions denied outright. RunTask, DescribeTasks and the read set are
# EXCLUDED because the revision-reader runner legitimately holds them (Sids
# RunExactReaderRevisionOnly, DescribeTasksInStagingClusterOnly).
ECS_DENIED = [
    "ecs:CreateCluster", "ecs:CreateService", "ecs:DeleteCluster", "ecs:DeleteService",
    "ecs:DeregisterTaskDefinition", "ecs:DeleteTaskDefinitions", "ecs:ExecuteCommand",
    "ecs:PutClusterCapacityProviders", "ecs:RegisterTaskDefinition", "ecs:StartTask",
    "ecs:StopTask", "ecs:CreateTaskSet", "ecs:DeleteTaskSet", "ecs:UpdateServicePrimaryTaskSet",
    "ecs:UpdateCluster", "ecs:UpdateService",
]

IAM_ADMIN_DENIED = [
    "iam:AddClientIDToOpenIDConnectProvider", "iam:AttachGroupPolicy", "iam:AttachRolePolicy",
    "iam:AttachUserPolicy", "iam:CreateAccessKey", "iam:CreateGroup", "iam:CreateInstanceProfile",
    "iam:CreateLoginProfile", "iam:CreateOpenIDConnectProvider", "iam:CreatePolicy",
    "iam:CreatePolicyVersion", "iam:CreateRole", "iam:CreateUser", "iam:DeleteOpenIDConnectProvider",
    "iam:DeletePolicy", "iam:DeleteRole", "iam:DeleteRolePermissionsBoundary", "iam:DeleteRolePolicy",
    "iam:DetachRolePolicy", "iam:PutGroupPolicy", "iam:PutRolePermissionsBoundary",
    "iam:PutRolePolicy", "iam:PutUserPolicy", "iam:SetDefaultPolicyVersion", "iam:TagRole",
    "iam:UntagRole", "iam:UpdateAssumeRolePolicy", "iam:UpdateOpenIDConnectProviderThumbprint",
    "iam:UpdateRole",
    # Added in Gate 4N-I8 when the triangulated requirement exposed them. A service-linked
    # role has an AWS-defined trust and policy, so it is narrower than CreateRole — but it is
    # still a principal this design never intends a bounded role to mint. DeletePolicyVersion
    # is half of the boundary-rewrite path the 4N-I7 architect lane found.
    "iam:CreateServiceLinkedRole", "iam:DeletePolicyVersion",
    # Gate 4N-I11: an instance profile is a role-to-EC2 binding, i.e. another way to make a
    # role assumable by a compute principal.
    "iam:AddRoleToInstanceProfile", "iam:PutUserPolicy",
]

# Evidence destruction that does not go through CloudTrail. The Gate 4N-I7 security lane
# found this class entirely absent: the boundary is Allow * minus targeted Denies, so any
# role whose identity policy carried logs:DeleteLogGroup could erase its own application and
# audit log groups. No repository-managed role needs these — the execution roles need
# CreateLogStream and PutLogEvents only — so the deny is flat.
LOGS_DENIED = [
    "logs:DeleteLogGroup", "logs:DeleteLogStream", "logs:DeleteRetentionPolicy",
    "logs:PutRetentionPolicy",
]

# Gate 4N-I11 Defect 16, found by the expanded requirement set. Each of these stops or
# redirects audit delivery without deleting a single object: turning off access logging,
# repointing notifications, or handing ownership of delivered objects to another account.
# s3:PutBucketVersioning and PutLifecycleConfiguration are already in the protected-bucket
# statement; these three were not enumerated anywhere.
S3_DELIVERY_DENIED = [
    "s3:PutBucketLogging", "s3:PutBucketNotification", "s3:PutBucketOwnershipControls",
]

# ACLs reach the same outcome as a bucket policy through a different API. Denying
# s3:PutBucketPolicy on the protected buckets while leaving ACLs open was a complete bypass.
# No repository-managed role uses object or bucket ACLs, so this is flat too.
S3_ACL_DENIED = ["s3:PutBucketAcl", "s3:PutObjectAcl"]

# aws-portal:* is a RETIRED prefix (AWS migrated billing authorization off it in 2023),
# so it is a dead Deny on its own. The live successors are enumerated alongside it.
ACCOUNT_ADMIN_DENIED = [
    "account:*", "aws-portal:*", "billing:*", "budgets:ModifyBudget", "ce:*",
    "consolidatedbilling:*", "cur:*", "freetier:*", "identitystore:*", "invoicing:*",
    "organizations:*", "payments:*", "purchase-orders:*", "sso:*", "sso-directory:*", "tax:*",
]

CLOUDTRAIL_DENIED = [
    "cloudtrail:DeleteTrail", "cloudtrail:PutEventSelectors", "cloudtrail:PutInsightSelectors",
    "cloudtrail:StopLogging", "cloudtrail:UpdateTrail",
]


def boundary_policy() -> dict:
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                # Not a grant. A boundary intersects with the identity policy, so this is
                # the ceiling; every restriction below carves out of it.
                "Sid": "CeilingAllowsWhatIsNotDeniedBelow",
                "Effect": "Allow",
                "Action": "*",
                "Resource": "*",
            },
            {
                "Sid": "DenyIdentityAndAccountAdministration",
                "Effect": "Deny",
                "Action": sorted(IAM_ADMIN_DENIED + ACCOUNT_ADMIN_DENIED),
                "Resource": "*",
            },
            {
                # The reader RUNNER legitimately passes exactly one role. Denying PassRole
                # outright would break its only function, so the deny excludes that role
                # and nothing else.
                "Sid": "DenyPassRoleExceptReaderExecutionRole",
                "Effect": "Deny",
                "Action": "iam:PassRole",
                "NotResource": READER_EXECUTION_ROLE,
            },
            {
                "Sid": "DenyLogGroupDestructionAndRetentionTampering",
                "Effect": "Deny",
                "Action": sorted(LOGS_DENIED),
                "Resource": "*",
            },
            {
                "Sid": "DenyAuditDeliveryTampering",
                "Effect": "Deny",
                "Action": sorted(S3_DELIVERY_DENIED),
                "Resource": "*",
            },
            {
                "Sid": "DenyObjectAndBucketAclChanges",
                "Effect": "Deny",
                "Action": sorted(S3_ACL_DENIED),
                "Resource": "*",
            },
            {
                "Sid": "DenyAuditTrailShutdown",
                "Effect": "Deny",
                "Action": sorted(CLOUDTRAIL_DENIED),
                "Resource": "*",
            },
            {
                "Sid": "DenyTerraformStateAccess",
                "Effect": "Deny",
                "Action": ["s3:DeleteObject", "s3:DeleteObjectVersion", "s3:GetObject",
                           "s3:GetObjectVersion", "s3:PutObject"],
                "Resource": [STATE_BUCKET, STATE_OBJECTS],
            },
            {
                # Covers the STATE bucket AND the AUDIT bucket. A bucket-policy or
                # lifecycle write on the audit bucket halts CloudTrail delivery without
                # ever calling StopLogging — the S3-side bypass the operator generator
                # already warns about, which the Gate 4N-I6 boundary left open.
                "Sid": "DenyProtectedBucketAndLockAdministration",
                "Effect": "Deny",
                "Action": [
                    # The READ actions matter too. No repository-managed role has any
                    # business reading the lock table, and the Gate 4N-I7 Allow-axis proof
                    # found dynamodb:GetItem reaching it — the write actions were denied
                    # and the reads simply had not been considered.
                    "dynamodb:BatchGetItem", "dynamodb:GetItem", "dynamodb:Query",
                    "dynamodb:Scan", "dynamodb:BatchWriteItem",
                    "dynamodb:DeleteItem", "dynamodb:DeleteTable", "dynamodb:PutItem",
                    "dynamodb:UpdateItem", "s3:DeleteBucket", "s3:DeleteBucketPolicy",
                    "s3:PutBucketPolicy", "s3:PutBucketPublicAccessBlock",
                    "s3:PutBucketVersioning", "s3:PutEncryptionConfiguration",
                    "s3:PutLifecycleConfiguration", "s3:PutBucketReplication",
                ],
                "Resource": [STATE_BUCKET, STATE_OBJECTS, LOCK_TABLE,
                             AUDIT_BUCKET, AUDIT_OBJECTS],
            },
            {
                "Sid": "DenyAuditLogObjectDestruction",
                "Effect": "Deny",
                "Action": ["s3:DeleteObject", "s3:DeleteObjectVersion", "s3:PutObject"],
                "Resource": [AUDIT_BUCKET, AUDIT_OBJECTS],
            },
            {
                # Execution roles legitimately read the four staging containers, so the
                # deny is a fence: everything EXCEPT the approved prefix.
                "Sid": "DenySecretsOutsideApprovedContainers",
                "Effect": "Deny",
                "Action": ["secretsmanager:BatchGetSecretValue", "secretsmanager:GetSecretValue"],
                "NotResource": SECRETS_PREFIX,
            },
            {
                "Sid": "DenySecretMutationEverywhere",
                "Effect": "Deny",
                "Action": [
                    "secretsmanager:CreateSecret", "secretsmanager:DeleteResourcePolicy",
                    "secretsmanager:DeleteSecret", "secretsmanager:PutResourcePolicy",
                    "secretsmanager:PutSecretValue", "secretsmanager:ReplicateSecretToRegions",
                    "secretsmanager:RestoreSecret", "secretsmanager:RotateSecret",
                    "secretsmanager:UpdateSecret", "secretsmanager:UpdateSecretVersionStage",
                ],
                "Resource": "*",
            },
            {
                # Task roles decrypt via Secrets Manager on the secrets CMK only.
                "Sid": "DenyKmsUseOutsideSecretsCmk",
                "Effect": "Deny",
                "Action": ["kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey",
                           "kms:GenerateDataKeyPair", "kms:GenerateDataKeyPairWithoutPlaintext",
                           "kms:GenerateDataKeyWithoutPlaintext", "kms:ReEncryptFrom",
                           "kms:ReEncryptTo"],
                "NotResource": SECRETS_CMK,
            },
            {
                "Sid": "DenyKmsAdministration",
                "Effect": "Deny",
                "Action": [
                    "kms:CancelKeyDeletion", "kms:CreateGrant", "kms:DisableKey",
                    "kms:PutKeyPolicy", "kms:ReplicateKey", "kms:RetireGrant",
                    "kms:RevokeGrant", "kms:ScheduleKeyDeletion",
                ],
                "Resource": "*",
            },
            {
                # ecs:RunTask is NOT in the flat deny below: the revision-reader runner's
                # entire job is to run one task-definition family. A blanket deny would
                # break it, and a bare omission left the boundary permitting RunTask on
                # ANY task definition — the single escape in the Gate 4N-I7 Allow-axis
                # proof. The NotResource fence keeps the runner working and closes the rest.
                "Sid": "DenyRunTaskExceptTheReaderRevision",
                "Effect": "Deny",
                "Action": ["ecs:RunTask"],
                "NotResource": [READER_TASK_DEFINITION_ARNS],
            },
            {
                "Sid": "DenyEcsControlPlaneMutation",
                "Effect": "Deny",
                "Action": sorted(ECS_DENIED),
                "Resource": "*",
            },
            {
                # FLAT deny, no carve-out. A permissions boundary constrains the role's
                # IDENTITY policy, never the TRUST policy that lets ECS or GitHub OIDC
                # assume it — so denying AssumeRole outright breaks nothing, while the
                # Gate 4N-I6 `signalnest-staging-*` carve-out left the whole staging role
                # family chainable and was WEAKER than the standing read-only baseline.
                "Sid": "DenyRoleChainingAndFederation",
                "Effect": "Deny",
                "Action": ["sts:AssumeRole", "sts:AssumeRoleWithSAML",
                           "sts:AssumeRoleWithWebIdentity", "sts:GetFederationToken"],
                "Resource": "*",
            },
            {
                "Sid": "DenyRdsDestruction",
                "Effect": "Deny",
                "Action": ["rds:CreateDBSnapshot", "rds:DeleteDBInstance",
                           "rds:DeleteDBSnapshot", "rds:ModifyDBInstance",
                           "rds:ModifyDBSnapshotAttribute",
                           "rds:RestoreDBInstanceFromDBSnapshot"],
                "Resource": "*",
            },
        ],
    }


def canonical(doc: dict) -> bytes:
    return json.dumps(doc, sort_keys=True, separators=(",", ":")).encode("utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hash", action="store_true")
    args = parser.parse_args()
    doc = boundary_policy()
    rendered = json.dumps(doc, indent=2) + "\n"
    if args.hash:
        print(f"name       {POLICY_NAME}")
        print(f"arn        {POLICY_ARN}")
        print(f"version    {VERSION_ID}")
        print(f"canonical  {hashlib.sha256(canonical(doc)).hexdigest()}")
        print(f"file_byte  {hashlib.sha256(rendered.encode('utf-8')).hexdigest()}")
        print(f"statements {len(doc['Statement'])}")
        return 0
    sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
