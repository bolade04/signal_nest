#!/usr/bin/env python3
"""Semantic AWS action classification (Gate 4N-I17, Defect 4, Phases N/O).

THE DEFECT. `gen_readonly_verifier_policy.is_mutating()` decided whether an action mutates by
testing whether its verb STARTS WITH one of a hand-written prefix list. The list omitted Stop,
Run, Terminate, Start, Pass, Schedule, Invoke, Change, Enable, Upload and Restore, so:

    cloudtrail:StopLogging   -> "read"      (audit-trail shutdown)
    ecs:RunTask              -> "read"      (arbitrary workload execution)
    iam:PassRole             -> "read"      (authority delegation)
    ec2:TerminateInstances   -> "read"
    kms:ScheduleKeyDeletion  -> "read"

The generator would have emitted a "read-only verifier" policy containing any of them and printed
"actions: N (all reads)". Its seven-entry bypass allowlist was dead code — every entry returned
False without it, so the bypass had never bypassed anything and had never been exercised.

WHY A PREFIX LIST WAS ALWAYS GOING TO FAIL. AWS action names are marketing English, not a grammar.
`Get`, `List` and `Describe` are conventionally reads, but `GetFederationToken` mints credentials
and `DescribeAccountAssignmentDeletionStatus` is a read whose name contains "Deletion". No prefix
rule separates those, which is why this module classifies from DATA rather than from spelling.

TWO INDEPENDENT GROUNDS (Phase N), and they must agree:

  SOURCE 1  ACTION_METADATA below — retained authoritative categorisation, keyed by exact action
            name. Hand-curated from AWS service-authorization semantics, not from the verb.

  SOURCE 2  the repository's own security invariants — `must_not_contract.FORBIDDEN_CAPABILITIES`
            (what W0 must never hold) and the boundary policy's deny set. An action the repository
            forbids cannot simultaneously be classified READ_ONLY.

Disagreement between the two is a finding, not something to average. An action absent from
SOURCE 1 is UNKNOWN, and UNKNOWN fails verifier-policy validation — the classifier may not guess.

Usage:
    python3 scripts/action_classifier.py [--json]
Exit: 0 iff every action in every reviewed policy classifies and the two sources agree.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

# --- categories ---------------------------------------------------------------------------

READ_ONLY = "READ_ONLY"
READ_ONLY_METADATA = "READ_ONLY_METADATA"
READ_ONLY_CONFIGURATION = "READ_ONLY_CONFIGURATION"
MUTATING = "MUTATING"
AUTHORITY_BEARING = "AUTHORITY_BEARING"
EXECUTION_TRIGGERING = "EXECUTION_TRIGGERING"
DESTRUCTIVE = "DESTRUCTIVE"
# GATE 4N-I19, AWS-1. Four categories the old model could not express. `Get` covers all of
# reading a tag, reading a secret and MINTING A CREDENTIAL, and the Gate 4N-I17 escalation
# turned on exactly that conflation: sso:GetRoleCredentials returns live temporary AWS
# credentials and was classified read-only because of its first three letters.
SENSITIVE_DATA_RETURNING = "SENSITIVE_DATA_RETURNING"
CREDENTIAL_RETURNING = "CREDENTIAL_RETURNING"
TOKEN_RETURNING = "TOKEN_RETURNING"
SECRET_RETURNING = "SECRET_RETURNING"
UNKNOWN = "UNKNOWN"

CATEGORIES = (READ_ONLY, READ_ONLY_METADATA, READ_ONLY_CONFIGURATION, MUTATING,
              AUTHORITY_BEARING, EXECUTION_TRIGGERING, DESTRUCTIVE, SENSITIVE_DATA_RETURNING,
              CREDENTIAL_RETURNING, TOKEN_RETURNING, SECRET_RETURNING, UNKNOWN)

# The ONLY categories that may pass a read-only principal. Expressed as an allowlist, so a
# category added later is disqualifying until someone deliberately admits it — the opposite of
# the old denylist, where a new risk category would have been silently permitted.
PERMITTED_READ_CATEGORIES = (READ_ONLY, READ_ONLY_METADATA, READ_ONLY_CONFIGURATION)

# Categories that disqualify an action from a read-only principal. Derived from the allowlist
# so the two can never drift apart.
NON_READ = tuple(c for c in CATEGORIES if c not in PERMITTED_READ_CATEGORIES)


# --- SOURCE 1: retained authoritative metadata ---------------------------------------------
#
# Keyed by EXACT action name. An action may carry more than one non-read category — RunTask both
# mutates and triggers execution; StopLogging both mutates and destroys evidence.

ACTION_METADATA = {
    # ---- reads: identity and inspection ----
    "sts:GetCallerIdentity": (READ_ONLY,),
    "iam:GetRole": (READ_ONLY,), "iam:GetRolePolicy": (READ_ONLY,),
    "iam:GetPolicy": (READ_ONLY,), "iam:GetPolicyVersion": (READ_ONLY,),
    "iam:ListRoles": (READ_ONLY,), "iam:ListRolePolicies": (READ_ONLY,),
    "iam:ListAttachedRolePolicies": (READ_ONLY,), "iam:ListRoleTags": (READ_ONLY,),
    "iam:ListEntitiesForPolicy": (READ_ONLY,), "iam:ListPolicyVersions": (READ_ONLY,),
    "iam:SimulatePrincipalPolicy": (READ_ONLY,), "iam:SimulateCustomPolicy": (READ_ONLY,),

    # ---- reads: Identity Center. Note the three *Status reads whose names contain
    #      "Provisioning"/"Creation"/"Deletion" — precisely the shape a verb rule gets wrong.
    "sso:DescribePermissionSet": (READ_ONLY,),
    "sso:GetInlinePolicyForPermissionSet": (READ_ONLY,),
    "sso:DescribePermissionSetProvisioningStatus": (READ_ONLY,),
    "sso:DescribeAccountAssignmentCreationStatus": (READ_ONLY,),
    "sso:DescribeAccountAssignmentDeletionStatus": (READ_ONLY,),
    "sso:ListAccountAssignments": (READ_ONLY,),
    "sso:ListManagedPoliciesInPermissionSet": (READ_ONLY,),
    "sso:ListPermissionSets": (READ_ONLY,), "sso:ListInstances": (READ_ONLY,),

    # ---- reads: audit and estate ----
    "cloudtrail:LookupEvents": (READ_ONLY,), "cloudtrail:GetTrailStatus": (READ_ONLY,),
    "cloudtrail:DescribeTrails": (READ_ONLY,), "cloudtrail:GetTrail": (READ_ONLY,),
    "cloudtrail:ListTags": (READ_ONLY,), "cloudtrail:GetEventSelectors": (READ_ONLY,),

    # ---- Identity Center administration: mutating ----
    "sso:CreatePermissionSet": (MUTATING,),
    "sso:PutInlinePolicyToPermissionSet": (MUTATING, AUTHORITY_BEARING),
    "sso:CreateAccountAssignment": (MUTATING, AUTHORITY_BEARING),
    "sso:ProvisionPermissionSet": (MUTATING,),
    "sso:DeleteAccountAssignment": (MUTATING,),
    "sso:DeletePermissionSet": (MUTATING, DESTRUCTIVE),
    "sso:AttachManagedPolicyToPermissionSet": (MUTATING, AUTHORITY_BEARING),

    # ---- IAM mutation and escalation ----
    "iam:CreateRole": (MUTATING, AUTHORITY_BEARING),
    "iam:DeleteRole": (MUTATING, DESTRUCTIVE),
    "iam:PutRolePolicy": (MUTATING, AUTHORITY_BEARING),
    "iam:DeleteRolePolicy": (MUTATING, DESTRUCTIVE),
    "iam:AttachRolePolicy": (MUTATING, AUTHORITY_BEARING),
    "iam:DetachRolePolicy": (MUTATING, DESTRUCTIVE),
    "iam:UpdateAssumeRolePolicy": (MUTATING, AUTHORITY_BEARING),
    "iam:PutRolePermissionsBoundary": (MUTATING, AUTHORITY_BEARING),
    "iam:DeleteRolePermissionsBoundary": (MUTATING, DESTRUCTIVE, AUTHORITY_BEARING),
    "iam:CreatePolicy": (MUTATING, AUTHORITY_BEARING),
    "iam:CreatePolicyVersion": (MUTATING, AUTHORITY_BEARING),
    "iam:SetDefaultPolicyVersion": (MUTATING, AUTHORITY_BEARING),
    "iam:DeletePolicy": (MUTATING, DESTRUCTIVE),
    "iam:TagRole": (MUTATING,), "iam:UntagRole": (MUTATING,),
    "iam:CreateUser": (MUTATING, AUTHORITY_BEARING),
    "iam:CreateAccessKey": (MUTATING, AUTHORITY_BEARING),
    "iam:CreateLoginProfile": (MUTATING, AUTHORITY_BEARING),
    "iam:UpdateAccessKey": (MUTATING, AUTHORITY_BEARING),
    "iam:CreateServiceLinkedRole": (MUTATING, AUTHORITY_BEARING),
    "iam:UpdateRole": (MUTATING,),

    # ---- THE ONES THE PREFIX LIST GOT WRONG ----
    # PassRole grants a service the right to assume a role. It writes nothing and reads nothing;
    # it DELEGATES AUTHORITY, which no verb-based rule can see.
    "iam:PassRole": (AUTHORITY_BEARING,),
    "sts:AssumeRole": (AUTHORITY_BEARING,),
    "sts:AssumeRoleWithSAML": (AUTHORITY_BEARING,),
    "sts:AssumeRoleWithWebIdentity": (AUTHORITY_BEARING,),
    "sts:GetFederationToken": (AUTHORITY_BEARING,),
    "ecs:RunTask": (MUTATING, EXECUTION_TRIGGERING),
    "ecs:StartTask": (MUTATING, EXECUTION_TRIGGERING),
    "ecs:ExecuteCommand": (MUTATING, EXECUTION_TRIGGERING),
    "lambda:InvokeFunction": (MUTATING, EXECUTION_TRIGGERING),
    "cloudtrail:StopLogging": (MUTATING, DESTRUCTIVE),
    "cloudtrail:DeleteTrail": (MUTATING, DESTRUCTIVE),
    "kms:ScheduleKeyDeletion": (MUTATING, DESTRUCTIVE),
    "kms:DisableKey": (MUTATING, DESTRUCTIVE),
    "ec2:TerminateInstances": (MUTATING, DESTRUCTIVE),
    "s3:DeleteBucket": (MUTATING, DESTRUCTIVE),
    "s3:PutBucketPolicy": (MUTATING, AUTHORITY_BEARING),
    "rds:DeleteDBInstance": (MUTATING, DESTRUCTIVE),

    # ---- cryptographic operations. None is a "read": each either uses key material to produce
    #      plaintext/ciphertext or alters grant state. Convention cannot see this — the verbs
    #      Decrypt/Encrypt/Generate/ReEncrypt/Retire match no read or write prefix.
    "kms:Decrypt": (AUTHORITY_BEARING,),
    "kms:Encrypt": (AUTHORITY_BEARING,),
    "kms:GenerateDataKey": (AUTHORITY_BEARING,),
    "kms:GenerateDataKeyPair": (AUTHORITY_BEARING,),
    "kms:GenerateDataKeyWithoutPlaintext": (AUTHORITY_BEARING,),
    "kms:GenerateDataKeyPairWithoutPlaintext": (AUTHORITY_BEARING,),
    "kms:ReEncryptFrom": (AUTHORITY_BEARING,),
    "kms:ReEncryptTo": (AUTHORITY_BEARING,),
    "kms:RetireGrant": (MUTATING, DESTRUCTIVE, AUTHORITY_BEARING),

    # ---- remaining stragglers, each classified from its effect rather than its spelling ----
    # BatchWriteItem writes and deletes items; "Batch" matches no prefix rule.
    "dynamodb:BatchWriteItem": (MUTATING, DESTRUCTIVE),
    # FilterLogEvents is a genuine read whose verb ("Filter") matches neither list.
    "logs:FilterLogEvents": (READ_ONLY,),
    # Leaving the organization detaches the account from its guardrails — maximally destructive.
    "organizations:LeaveOrganization": (MUTATING, DESTRUCTIVE, AUTHORITY_BEARING),
}


class ClassificationError(Exception):
    """Raised when an action cannot be classified. The classifier never guesses."""


# --- SOURCE 1b: naming convention, EXPLICITLY the weaker ground ----------------------------
#
# The curated table above cannot practically enumerate all ~235 actions across six documents, most
# of which are ordinary `Describe*`/`List*` reads in the refresh closure. Convention covers those.
#
# THIS IS THE SAME KIND OF RULE THAT CAUSED THE DEFECT, so it is constrained three ways:
#   1. the curated table always wins where it has an entry;
#   2. every action classified this way is stamped provenance CONVENTION, not AUTHORITATIVE, so a
#      reader can see which classifications are weakly grounded;
#   3. SOURCE 2 holds a VETO — an action the repository forbids can never come out READ_ONLY,
#      whatever the convention says. That veto is what catches the dangerous direction: every
#      action the prefix list previously mis-called a read (StopLogging, RunTask, PassRole,
#      TerminateInstances, ScheduleKeyDeletion) is in the forbidden set.
# The residual risk is an action that is genuinely dangerous, absent from the curated table, AND
# absent from the forbidden set. That is recorded as a limitation rather than claimed away.


# --- SOURCE 1b: actions OBSERVED in the retained zero-write refresh -------------------------
#
# GATE 4N-I19, AWS-1. `gen_operator_policies.REFRESH_CLOSURE` was derived from the complete
# successful full-graph refresh of 2026-07-28T22:01:44Z: 267 CloudTrail events, 79 distinct API
# operations across 17 services, ZERO WRITES. An action observed in a run that wrote nothing is
# evidenced as non-mutating BY OBSERVATION — which is a retained fact about behaviour, not a
# claim about the verb. That is what makes this a legitimate exact source and the prefix rule
# it replaces illegitimate.
#
# The set is IMPORTED from the closure rather than copied, so it cannot drift from the evidence
# it claims to represent.


def _refresh_observed_reads() -> frozenset:
    import gen_operator_policies as _gen

    observed = set()
    for group in _gen.REFRESH_CLOSURE.values():
        if isinstance(group, (list, tuple, set)):
            observed.update(group)
    return frozenset(observed)


REFRESH_OBSERVED_READS = _refresh_observed_reads()


# --- SOURCE 1c: the remainder, reviewed one action at a time --------------------------------
#
# Everything the two evidence sources above do not cover. Each row is a deliberate judgement
# about what the API DOES, recorded so that the next reader can disagree with a specific claim
# rather than with a prefix table.

CURATED_REMAINDER = {
    # Reads that happen to carry write-shaped verbs, or that no closure observed.
    "dynamodb:BatchGetItem": (READ_ONLY,),
    "dynamodb:Query": (READ_ONLY,),
    "dynamodb:Scan": (READ_ONLY,),
    "s3:GetBucketLocation": (READ_ONLY_METADATA,),
    "s3:GetObjectVersion": (READ_ONLY,),
    "logs:StartQuery": (READ_ONLY,),          # "Start" names a query, not a mutation
    "cloudfront:GetDistribution": (READ_ONLY_CONFIGURATION,),
    "cloudfront:GetOriginAccessControl": (READ_ONLY_CONFIGURATION,),
    # INFRA-9 B-3: registration read-back by the apply identity. A read whose service has no
    # resource-level scoping for it; never observed in the zero-write refresh (no task
    # definition existed to describe), so it is curated rather than observation-classified.
    "ecs:DescribeTaskDefinition": (READ_ONLY_CONFIGURATION,),

    # Reads that return CONTENT. Not mutations, but not ordinary reads either: a read-only
    # verifier has no business reading log bodies or secret material.
    "logs:GetLogEvents": (SENSITIVE_DATA_RETURNING,),
    "secretsmanager:BatchGetSecretValue": (SECRET_RETURNING,),

    # Mutations, by service.
    "budgets:ModifyBudget": (MUTATING,),
    "ecr:CreateRepository": (MUTATING,),
    "ecr:PutImageScanningConfiguration": (MUTATING,),
    "ecr:PutImageTagMutability": (MUTATING,),
    "ecr:PutLifecyclePolicy": (MUTATING,),
    "ecr:TagResource": (MUTATING,),
    "ecs:CreateCluster": (MUTATING,),
    # INFRA-9 B-3 permissions-lane finding 3: tag-on-create authorization surface for the
    # apply identity's task-definition registration.
    "ecs:TagResource": (MUTATING,),
    "ecs:CreateTaskSet": (MUTATING, EXECUTION_TRIGGERING),
    "ecs:DeleteCluster": (MUTATING, DESTRUCTIVE),
    "ecs:DeleteService": (MUTATING, DESTRUCTIVE),
    "ecs:DeleteTaskDefinitions": (MUTATING, DESTRUCTIVE),
    "ecs:DeleteTaskSet": (MUTATING, DESTRUCTIVE),
    "ecs:DeregisterTaskDefinition": (MUTATING, DESTRUCTIVE),
    "ecs:PutClusterCapacityProviders": (MUTATING,),
    "ecs:StopTask": (MUTATING,),
    "ecs:UpdateCluster": (MUTATING,),
    "ecs:UpdateServicePrimaryTaskSet": (MUTATING, EXECUTION_TRIGGERING),
    "iam:AttachGroupPolicy": (MUTATING, AUTHORITY_BEARING),
    "iam:CreateGroup": (MUTATING, AUTHORITY_BEARING),
    "iam:PutGroupPolicy": (MUTATING, AUTHORITY_BEARING),
    "iam:DeleteOpenIDConnectProvider": (MUTATING, DESTRUCTIVE, AUTHORITY_BEARING),
    "kms:CancelKeyDeletion": (MUTATING,),
    "kms:ReplicateKey": (MUTATING,),
    "s3:PutBucketReplication": (MUTATING,),
    "s3:PutEncryptionConfiguration": (MUTATING,),
    "secretsmanager:CreateSecret": (MUTATING,),
    "secretsmanager:DeleteResourcePolicy": (MUTATING, DESTRUCTIVE),
    "secretsmanager:ReplicateSecretToRegions": (MUTATING,),
    "secretsmanager:RotateSecret": (MUTATING,),
    "secretsmanager:UpdateSecretVersionStage": (MUTATING,),
}

# ---- PHASE F: the high-risk actions this gate exists to get right -------------------------
#
# sso:GetRoleCredentials is THE Gate 4N-I17 escalation: it returns live temporary AWS
# credentials for a permission set, and the prefix rule called it a read.

CURATED_HIGH_RISK = {
    "sso:GetRoleCredentials": (CREDENTIAL_RETURNING,),
    "sso:GetRoleCredentialsWithIdentityContext": (CREDENTIAL_RETURNING,),
    "sts:AssumeRole": (CREDENTIAL_RETURNING, AUTHORITY_BEARING),
    "sts:AssumeRoleWithWebIdentity": (CREDENTIAL_RETURNING, AUTHORITY_BEARING),
    "sts:AssumeRoleWithSAML": (CREDENTIAL_RETURNING, AUTHORITY_BEARING),
    "sts:GetFederationToken": (CREDENTIAL_RETURNING, TOKEN_RETURNING, AUTHORITY_BEARING),
    "sts:GetSessionToken": (CREDENTIAL_RETURNING, TOKEN_RETURNING),
    "ecr:GetAuthorizationToken": (TOKEN_RETURNING,),
    "secretsmanager:GetSecretValue": (SECRET_RETURNING,),
    "ssm:GetParameter": (SENSITIVE_DATA_RETURNING,),
    "ssm:GetParameters": (SENSITIVE_DATA_RETURNING,),
    "kms:Decrypt": (SENSITIVE_DATA_RETURNING,),
    "iam:PassRole": (AUTHORITY_BEARING,),
    "ecs:RunTask": (MUTATING, EXECUTION_TRIGGERING),
    "cloudtrail:StopLogging": (MUTATING, DESTRUCTIVE),
    "sts:GetCallerIdentity": (READ_ONLY_METADATA,),
    "iam:GetRole": (READ_ONLY_CONFIGURATION,),
}

ACTION_METADATA.update(CURATED_REMAINDER)
ACTION_METADATA.update(CURATED_HIGH_RISK)

_READ_PREFIXES = ("Describe", "List", "Get", "Lookup", "BatchGet", "Search", "Query", "Scan",
                  "View", "Simulate", "Head", "Check", "Estimate")
_WRITE_PREFIXES = ("Create", "Delete", "Put", "Update", "Modify", "Attach", "Detach", "Set",
                   "Remove", "Add", "Tag", "Untag", "Write", "Enable", "Disable", "Start",
                   "Stop", "Run", "Terminate", "Reboot", "Restore", "Replicate", "Rotate",
                   "Provision", "Register", "Deregister", "Associate", "Disassociate",
                   "Authorize", "Revoke", "Copy", "Import", "Export", "Upload", "Invoke",
                   "Schedule", "Cancel", "Reset", "Replace", "Apply", "Accept", "Reject")


def prefix_hint(action: str) -> str | None:
    """DIAGNOSTIC ONLY (Gate 4N-I19, Phase D).

    This used to RETURN A CLASSIFICATION. 157 of 235 in-use actions were authorised by it, 79
    of them as reads, and that is precisely how sso:GetRoleCredentials — which returns live
    temporary AWS credentials — reached an Allow on Resource "*" and evaluated EXPLICIT_ALLOW.
    The module docstring already said "an action absent from SOURCE 1 is UNKNOWN, the
    classifier may not guess"; the implementation guessed anyway.

    The hint is retained because it is genuinely useful in a failure message ("this looks like
    a read but nothing says so"), and it is deliberately NOT returned as a category. Nothing in
    this module may authorise an action on the strength of its spelling.
    """
    verb = action.split(":", 1)[1] if ":" in action else action
    if verb.startswith(_READ_PREFIXES):
        return "looks-like-read"
    if verb.startswith(_WRITE_PREFIXES):
        return "looks-like-write"
    return None


def classify(action: str) -> dict:
    """Exact-source classification with a repository veto. Absence is UNKNOWN, and UNKNOWN fails.

    GATE 4N-I19, AWS-1. There is no longer any path from "the name starts with Get" to a
    permitted classification. An action is classified only if an EXACT source names it:

      CURATED_REVIEWED             hand-reviewed from AWS authorization semantics
      REFRESH_OBSERVED_ZERO_WRITE  observed in the retained full-graph refresh of
                                   2026-07-28T22:01:44Z — 267 CloudTrail events, 79 distinct
                                   operations, ZERO WRITES. Being observed in a run that wrote
                                   nothing is evidence of non-mutation, and it is evidence the
                                   repository actually retains rather than a claim about
                                   spelling.
      REPOSITORY_FORBIDDEN_INVARIANT  must_not_contract.FORBIDDEN_CAPABILITIES

    Anything else is UNKNOWN and fails closed, so a NEWLY INTRODUCED action cannot be used
    until someone classifies it deliberately.
    """
    from must_not_contract import FORBIDDEN_CAPABILITIES

    curated = ACTION_METADATA.get(action)
    if curated:
        categories, provenance = list(curated), "CURATED_REVIEWED"
    elif action in REFRESH_OBSERVED_READS:
        categories, provenance = [READ_ONLY], "REFRESH_OBSERVED_ZERO_WRITE"
    elif action in FORBIDDEN_CAPABILITIES:
        # The repository forbids it. That is an exact statement about this action, so it is a
        # classification — never a read, whatever it is called.
        categories, provenance = [MUTATING], "REPOSITORY_FORBIDDEN_INVARIANT"
    else:
        categories, provenance = [UNKNOWN], "NONE"

    forbidden = action in FORBIDDEN_CAPABILITIES
    source_2 = "FORBIDDEN_BY_REPOSITORY_INVARIANT" if forbidden else "NOT_FORBIDDEN"

    conflict = None
    if forbidden and all(c in PERMITTED_READ_CATEGORIES for c in categories):
        # Two independent grounds disagree. Surfaced, never averaged, and never resolved by
        # letting the weaker one win.
        conflict = (f"{action}: exact metadata says {categories} but the repository's must-not "
                    "contract forbids it. Two independent sources disagree.")

    if UNKNOWN in categories:
        hint = prefix_hint(action)
        conflict = conflict or (
            f"{action} has no exact classification"
            + (f" (name {hint}, which is NOT evidence)" if hint else ""))

    is_read = bool(categories) and all(c in PERMITTED_READ_CATEGORIES for c in categories)
    return {"action": action, "categories": categories,
            "provenance": provenance, "source_2": source_2,
            "vetoed_by_source_2": False,
            "prefix_hint": prefix_hint(action),
            "is_read_only": is_read and not conflict,
            "disqualifying": [c for c in categories if c in NON_READ],
            "conflict": conflict}


def is_read_only(action: str) -> bool:
    """True only for an action authoritatively classified READ_ONLY and nothing else."""
    result = classify(action)
    if result["conflict"]:
        raise ClassificationError(result["conflict"])
    return result["is_read_only"]


def reviewed_policy_actions() -> dict:
    """Every action in every reviewed policy — Phase O coverage, no exception list."""
    import gen_boundary_policy as gb
    import gen_bootstrap_operator_policy as boot
    import gen_operator_policies as gen
    import gen_readonly_verifier_policy as rv
    import gen_role_bootstrap_policy as rb

    # GATE 4N-I26A. This was a hardcoded copy of the then-active expiry. A literal here is a
    # SECOND source for a value that has exactly one authority, so a restamp left it behind and
    # this function silently classified actions from a policy generated under a superseded
    # window. Read the authoritative constant instead; there is now nothing to keep in sync.
    import expiry_authorization as _ea

    expiry = _ea.ACTIVE_EXPIRY_UTC
    documents = {
        "permanent_w0": gen.permanent_w0_policy(),
        "stage_a": gen.bootstrap_temp_policy(expiry),
        "role_bootstrap": rb.role_bootstrap_policy(expiry),
        "boundary": gb.boundary_policy(),
        "boundary_bootstrap": boot.bootstrap_operator_policy(expiry),
        "readonly_verifier": rv.readonly_verifier_policy(expiry),
    }
    out = {}
    for name, doc in documents.items():
        actions = set()
        for statement in doc.get("Statement", []):
            for key in ("Action", "NotAction"):
                value = statement.get(key)
                if value is None:
                    continue
                actions.update([value] if isinstance(value, str) else value)
        out[name] = sorted(a for a in actions if not a.endswith(":*") and a != "*")
    return out


def run() -> dict:
    per_policy = reviewed_policy_actions()
    every_action = sorted({a for actions in per_policy.values() for a in actions})
    rows = [classify(a) for a in every_action]
    unclassified = [r["action"] for r in rows if UNKNOWN in r["categories"]]
    conflicts = [r["conflict"] for r in rows if r["conflict"]]
    return {
        "policies": {k: len(v) for k, v in per_policy.items()},
        "distinct_actions": len(every_action),
        "coverage": 1.0 if not unclassified else round(
            (len(every_action) - len(unclassified)) / max(len(every_action), 1), 4),
        "unclassified": unclassified,
        "conflicts": conflicts,
        "rows": rows,
        "clean": not unclassified and not conflicts,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = run()
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=True))
    else:
        print(f"  distinct actions: {result['distinct_actions']}  "
              f"coverage: {result['coverage']}")
        for action in result["unclassified"]:
            print(f"    UNCLASSIFIED {action}", file=sys.stderr)
        for conflict in result["conflicts"]:
            print(f"    CONFLICT {conflict}", file=sys.stderr)
        print("ACTION CLASSIFIER: clean" if result["clean"] else "ACTION CLASSIFIER: findings")
    return 0 if result["clean"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
