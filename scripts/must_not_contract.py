#!/usr/bin/env python3
"""The MUST-NOT contract — DERIVED, not defined here (Gate 4N-I8, Defect 2).

WHAT CHANGED AND WHY. Through Gate 4N-I7 this file DEFINED the forbidden set by hand, and
the tests measured the policies against that same hand-written list. The adversarial lane
showed what that buys: delete one line and the policy and the expectation shrink together —
suite green, `allow_model` reporting "clean at 44/44", two principals silently downgraded
from EXPLICIT_DENY to IMPLICIT_DENY. Across 135 Deny actions, ~80 were individually
removable. A contract that lives inside the thing it constrains is decoration.

The authoritative set now comes from `scripts/deny_requirements.py`, which triangulates:

  SOURCE 1  the INCIDENT LEDGER at ~/.signalnest/anchor/, OUTSIDE the repository, mode 400,
            each entry bound to the prior gate and retained artifact that established it
  SOURCE 2  architecture invariants, expanded to actions

Deleting a line from this file can no longer remove a requirement: SOURCE 1 is unreachable
from the repository and keeps demanding it. Deriving also caught 24 capabilities the
hand-written list had missed, including every one the Gate 4N-I7 security lane named
(logs:Delete*, s3:PutBucketAcl/PutObjectAcl, kms:ReEncrypt*, secretsmanager:UpdateSecret and
PutResourcePolicy, sts:GetFederationToken, rds:ModifyDBSnapshotAttribute,
iam:CreateServiceLinkedRole) and the boundary-rewrite triad the architect lane found.

This module imports no policy generator. That direction of dependency is the whole point,
and a test enforces it.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from deny_requirements import required_denies  # noqa: E402

CONSEQUENCE_NOTES = {
    # BR-2, Gate 4N-H4: CreateRole takes an arbitrary trust document and needs no boundary
    # unless one is required by condition, so a principal holding it can mint a successor
    # with permissions it does not itself have. This is the transitive escape that made
    # "the ECS path is closed" true directly and false transitively.
    "iam:CreateRole": "mint a successor principal with an arbitrary trust document",
    "iam:PutRolePolicy": "write an arbitrary identity policy onto an existing role",
    "iam:AttachRolePolicy": "attach AdministratorAccess to a role",
    "iam:UpdateAssumeRolePolicy": "redirect an existing role's trust to an outside principal",
    "iam:DeleteRolePermissionsBoundary": "remove the very control this gate installs",
    "iam:PutRolePermissionsBoundary": "replace the boundary with a permissive one",
    "iam:CreateUser": "create a long-lived principal outside Identity Center",
    "iam:CreateAccessKey": "mint long-lived credentials that outlive every session control",
    "iam:CreateOpenIDConnectProvider": "trust an attacker-controlled identity provider",
    "iam:PassRole": "hand a more-privileged role to a service that will act as it",
    "iam:DeleteRole": "destroy a role other controls depend on",
    "iam:DeleteRolePolicy": "strip a role's inline policy, including its own restrictions",
    "iam:DetachRolePolicy": "detach a managed policy that was carrying a control",

    # Permission-set administration is how every prior gate's temporary grant was created.
    "sso:PutInlinePolicyToPermissionSet": "rewrite any permission set's policy",
    "sso:ProvisionPermissionSet": "materialise a rewritten permission set",
    "sso:CreateAccountAssignment": "assign a permission set to a new principal",
    "organizations:LeaveOrganization": "detach the account from organisational control",

    # Evidence destruction. Everything else in this file is unprovable without these.
    "cloudtrail:StopLogging": "stop recording the actions that would show the rest",
    "cloudtrail:DeleteTrail": "destroy the audit trail and everything it would have shown",
    "cloudtrail:UpdateTrail": "redirect the trail to a bucket nobody reads",
    "cloudtrail:PutEventSelectors": "silence the event classes that matter",
    "s3:DeleteObjectVersion": "erase delivered log objects under versioning",

    # Infrastructure reality. Rewriting state makes the repository stop describing AWS.
    "s3:GetObject": "read Terraform state, which contains resource identifiers and any "
                    "sensitive attribute the providers recorded",
    "s3:PutObject": "rewrite Terraform state, or plant an object in the audit bucket",
    "s3:DeleteObject": "delete state or delivered audit logs",
    "s3:PutBucketPolicy": "grant the world access to the state or audit bucket",
    "dynamodb:GetItem": "read the state lock table, revealing who is mid-apply and when",
    "dynamodb:PutItem": "forge or steal the state lock",
    "dynamodb:DeleteItem": "release another operator's lock mid-apply",

    # Secret material and the keys that protect it.
    "secretsmanager:GetSecretValue": "read the database credential and every other secret "
                                     "the workload holds",
    "secretsmanager:PutSecretValue": "overwrite a credential the workload will then use",
    "secretsmanager:DeleteSecret": "destroy a credential the workload depends on",
    "kms:Decrypt": "decrypt state or secret material protected by the CMK",
    "kms:ScheduleKeyDeletion": "destroy the key that protects state and secrets",
    "kms:PutKeyPolicy": "grant an outside principal use of the CMK",
    "kms:CreateGrant": "delegate CMK use without changing the key policy",

    # Workload execution. Gate 4N-H4 closed this path; it must not reopen.
    "ecs:RegisterTaskDefinition": "define a task that runs an attacker image as a role",
    "ecs:CreateService": "launch that task definition continuously as a service",
    "ecs:RunTask": "launch an attacker task definition a single time",
    "ecs:UpdateService": "swap a running service to an attacker revision",
    "ecs:ExecuteCommand": "obtain a shell inside a running task",

    # Data destruction.
    "rds:DeleteDBInstance": "destroy the database and the data in it",
    "rds:ModifyDBInstance": "make the database publicly reachable, or reset its password",
    "rds:RestoreDBInstanceFromDBSnapshot": "materialise a copy of production data",

    # Chaining out of the account entirely.
    "sts:AssumeRole": "chain into a role in another account",
}





def _build() -> dict[str, str]:
    """action -> consequence, for the AUTHORITATIVE set.

    The keys come from the triangulated requirement. The values prefer the hand-written
    consequence note where one exists, because those were reviewed; anything without a note
    falls back to the justification the requirement source itself carries. A key with no
    note is NOT dropped — dropping it is how the set would silently shrink again.
    """
    out: dict[str, str] = {}
    for action, row in required_denies().items():
        note = CONSEQUENCE_NOTES.get(action)
        out[action] = note if note else "; ".join(row["justification"])
    return out


FORBIDDEN_CAPABILITIES = _build()


def forbidden_actions() -> tuple[str, ...]:
    return tuple(sorted(FORBIDDEN_CAPABILITIES))


def consequence(action: str) -> str:
    return FORBIDDEN_CAPABILITIES[action]


def unnoted_actions() -> tuple[str, ...]:
    """Required capabilities with no reviewed consequence note. Visible, never hidden."""
    return tuple(sorted(a for a in FORBIDDEN_CAPABILITIES if a not in CONSEQUENCE_NOTES))
