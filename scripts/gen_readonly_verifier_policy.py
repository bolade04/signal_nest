#!/usr/bin/env python3
"""SignalNestRoleBootstrapROVerify — policy generator (Gate 4N-I16, Defect 5).

THE DEFECT. The lifecycle graph assigned twelve steps and eleven distinct AWS actions to a
principal called READ_ONLY_VERIFIER. That principal existed in exactly two files — the graph
and its test. There was no permission set, no policy, no trust document, no creation path,
and no retirement. The action-availability proof, whose docstring said "for every non-root
step", skipped it by name at scripts/role_bootstrap_lifecycle.py:217, so the graph reported
`unavailable_actions: 0` while six of the verifier's steps could not have executed at all —
including the inline-policy hash verification, BOTH provisioning polls, the residual-access
check and the CloudTrail evidence capture.

A design whose verification steps are assigned to a principal that does not exist is not a
design that can be executed.

WHAT THIS PRINCIPAL MAY DO. Reads only. Every action here answers a question the lifecycle
must answer to be safe: did provisioning reach a terminal state, does the inline policy match
the reviewed hash, is the assignment really gone, did the roles come out as expected, and is
there a CloudTrail record of every mutation. It holds NO mutating action, and the generator
refuses to emit a policy if one appears.

TWO SCOPING LIMITS, STATED RATHER THAN PAPERED OVER:

  1. THE ACTION PREFIX IS `sso:`, NOT `sso-admin:`. The API namespace is sso-admin but the
     IAM action prefix for IAM Identity Center is `sso:` (AWS Service Authorization
     Reference). A policy written with the API namespace would grant nothing at all while
     looking correct — the same class of defect as a wrong ARN shape.

  2. THE IDENTITY CENTER RESOURCES CANNOT BE SCOPED YET. Identity Center authorizes on
     `arn:aws:sso:::permissionSet/ssoins-<id>/ps-<id>` plus the instance ARN. No gate in this
     chain has been authorized to make the `sso-admin:ListInstances` call that would recover
     those identifiers, and the provenance record for the instance is explicitly UNKNOWN. So
     those statements carry Resource "*" with a region condition, and that is recorded as an
     open residual rather than presented as scoped. Inventing a plausible instance ARN would
     be worse than admitting the gap: it would produce a policy that denies at the worst
     moment.

Usage:
    python3 scripts/gen_readonly_verifier_policy.py \
        --issuance <ACTIVE_ISSUANCE_UTC> --expiry <ACTIVE_EXPIRY_UTC> [--json]

    Both halves come from the single reviewed pair in scripts/expiry_authorization.py. Gate
    4N-I26A corrected this line: it previously showed `--expiry` alone, carrying a stamp two
    windows out of date, so the documented invocation taught the exact defect Gate 4N-I19
    closed — an expiry travelling without the issuance that bounds it. `--issuance` defaults to
    the authoritative constant when omitted, but the documented form passes both, because a
    reader copying this line should see the pair travel together.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import signalnest_identity as identity  # noqa: E402

# IAM Identity Center caps permission-set names at 32 characters (CreatePermissionSet
# rejects longer names server-side — proven live by the B-1 executor rename, 2026-08-12).
# The original 39-character "…ReadOnlyVerifier" spelling could never have been created, and
# its reserved role AWSReservedSSO_<name>_<16-hex> would have exceeded IAM's 64-character
# role-name cap as well. This is the single source for the verifier's name; the lifecycle
# graph and every consumer import it rather than restating it.
PERMISSION_SET_NAME = "SignalNestRoleBootstrapROVerify"

# Every action this principal holds, and the lifecycle question each one answers.
IDENTITY_CENTRE_READS = {
    "sso:DescribePermissionSet": "did the permission set come out as reviewed",
    "sso:GetInlinePolicyForPermissionSet": "does the inline policy match the reviewed hash",
    "sso:DescribePermissionSetProvisioningStatus": "did provisioning reach a terminal state",
    "sso:DescribeAccountAssignmentCreationStatus": "did the assignment creation terminate",
    "sso:DescribeAccountAssignmentDeletionStatus": "did the removal terminate",
    "sso:ListAccountAssignments": "is the assignment really gone",
    "sso:ListManagedPoliciesInPermissionSet": "were managed policies attached behind our back",
}
IAM_READS = {
    "iam:GetRole": "does the role exist with the reviewed trust and boundary",
    "iam:ListRolePolicies": "which inline policies does the role carry",
    "iam:GetRolePolicy": "does the inline policy match what was reviewed",
    "iam:ListAttachedRolePolicies": "were managed policies attached to the role",
    "iam:ListRoleTags": "do the tags match the reviewed set",
}
IAM_STAR_READS = {
    "iam:ListRoles": "is the role inventory exactly the expected set, with no extras",
}
AUDIT_READS = {
    "cloudtrail:LookupEvents": "is every mutating step recorded in the audit trail",
}
CALLER_READS = {
    "sts:GetCallerIdentity": "am I the principal I believe I am",
}

ALL_ACTIONS = sorted({*IDENTITY_CENTRE_READS, *IAM_READS, *IAM_STAR_READS,
                      *AUDIT_READS, *CALLER_READS})

# GATE 4N-I17 DEFECT 4. The prefix list and its dead allowlist are GONE.
#
# What was here classified an action as mutating if its verb STARTED WITH one of a hand-written
# list. The list omitted Stop, Run, Pass, Terminate, Schedule and Invoke, so cloudtrail:StopLogging,
# ecs:RunTask and iam:PassRole all came back "read" and this generator would have emitted them
# while printing "all reads". The seven-entry MUTATION_ALLOWLIST that was supposed to protect the
# classification was dead code: every entry returned False without it, so it had never bypassed
# anything and had never been exercised.
#
# Classification now comes from scripts/action_classifier.py, which decides from curated action
# metadata plus the repository's own forbidden-capability invariant, and which refuses to guess.
import action_classifier  # noqa: E402


def is_mutating(action: str) -> bool:
    """True unless the action is authoritatively READ_ONLY. Fails closed on the unknown."""
    try:
        return not action_classifier.is_read_only(action)
    except action_classifier.ClassificationError:
        return True


# The roles this verifier inspects: exactly the eight the composition manages, plus the
# materialized Identity Center role for the bootstrap operator.
TARGET_ROLE_ARNS = [identity.iam_role_arn(n) for n in identity.ALL_ROLE_NAMES]
RESERVED_SSO_ROLE_GLOB = (
    f"arn:{identity.PARTITION}:iam::{identity.ACCOUNT}:role/aws-reserved/"
    f"sso.amazonaws.com/{identity.REGION}/AWSReservedSSO_*")

REGION_CONDITION = {"StringEquals": {"aws:RequestedRegion": identity.REGION}}


def readonly_verifier_policy(expiry: str, *, issuance: str | None = None) -> dict:
    """Reads expire. There is no Deny with a date condition — denies are permanent."""
    # GATE 4N-I19, ADV-A. The window must be AUTHORIZED, not merely well-formed. Gate 4N-I17
    # showed a 2099 stamp generating cleanly with the whole suite green; this call is what
    # makes an unbounded or already-expired window fail BEFORE any policy output exists.
    import expiry_authorization

    expiry_authorization.authorize(
        issuance=issuance if issuance is not None else expiry_authorization.ACTIVE_ISSUANCE_UTC,
        expiry=expiry, purpose="readonly_verifier")

    if not expiry or "<" in expiry:
        raise ValueError(f"refusing to stamp a placeholder expiry: {expiry!r}")

    def expiring(extra: dict | None = None) -> dict:
        condition = {"DateLessThan": {"aws:CurrentTime": expiry}}
        for key, value in (extra or {}).items():
            condition.setdefault(key, {}).update(value)
        return condition

    offenders = {}
    for action in ALL_ACTIONS:
        result = action_classifier.classify(action)
        if not result["is_read_only"]:
            offenders[action] = result["categories"]
    if offenders:
        raise ValueError(
            "the read-only verifier was given non-read actions: "
            + ", ".join(f"{a} {cats}" for a, cats in sorted(offenders.items())))

    document = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "VerifierCallerIdentity",
                "Effect": "Allow",
                "Action": sorted(CALLER_READS),
                "Resource": "*",
                "Condition": expiring(),
            },
            {
                # RESIDUAL, stated: Resource "*" because the Identity Center instance and
                # permission-set ARNs are not recoverable without an unauthorized live call.
                "Sid": "VerifierIdentityCentreReadsUnscopedResidual",
                "Effect": "Allow",
                "Action": sorted(IDENTITY_CENTRE_READS),
                "Resource": "*",
                "Condition": expiring(REGION_CONDITION),
            },
            {
                "Sid": "VerifierRoleReadsExact",
                "Effect": "Allow",
                "Action": sorted(IAM_READS),
                "Resource": TARGET_ROLE_ARNS + [RESERVED_SSO_ROLE_GLOB],
                "Condition": expiring(),
            },
            {
                # iam:ListRoles does not accept a resource scope: the question "is the
                # inventory exactly the expected set" is account-wide by construction.
                "Sid": "VerifierRoleInventoryStar",
                "Effect": "Allow",
                "Action": sorted(IAM_STAR_READS),
                "Resource": "*",
                "Condition": expiring(),
            },
            {
                # cloudtrail:LookupEvents is likewise not resource-scopable.
                "Sid": "VerifierAuditLookupStar",
                "Effect": "Allow",
                "Action": sorted(AUDIT_READS),
                "Resource": "*",
                "Condition": expiring(REGION_CONDITION),
            },
            {
                # PERMANENT. No date condition: a Deny that expires is not a Deny.
                #
                # GATE 4N-I19, AWS-1: THIS DENY IS DEFENCE IN DEPTH ONLY AND IS NOT THE
                # DECISIVE CONTROL. Its NotAction is computed from ALL_ACTIONS — the union of
                # this policy's own Allow sets — so anything added to the Allow is
                # automatically exempted here. Gate 4N-I17 executed that escalation: adding
                # sso:GetRoleCredentials produced an internally consistent policy that
                # evaluated EXPLICIT_ALLOW. The decisive check is the independently authored
                # ceiling enforced below, which shares no ancestor with this list.
                "Sid": "VerifierDenyEveryMutation",
                "Effect": "Deny",
                "NotAction": ALL_ACTIONS,
                "Resource": "*",
            },
        ],
    }

    # THE DECISIVE CHECK. Compared against tests/fixtures/readonly-verifier-ceiling.json, which
    # is authored from what this permission set is FOR and is not derived from the Allow list,
    # the Deny list, the classifier output or any candidate artifact. Widening the Allow and
    # the generated Deny together still fails here.
    import verifier_ceiling

    verifier_ceiling.require_within_ceiling(document)
    return document


def canonical(policy: dict) -> str:
    return json.dumps(policy, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def policy_hash(policy: dict) -> str:
    return hashlib.sha256(canonical(policy).encode()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expiry", required=True,
                        help="ISO-8601 UTC expiry; there is deliberately NO default")
    # GATE 4N-I19, ADV-A: issuance travels WITH the expiry. A window is authorized as a pair,
    # never as a lone endpoint, so the CLI cannot express an unbounded one.
    parser.add_argument("--issuance", default=None,
                        help="ISO-8601 UTC issuance; defaults to the reviewed active pair")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    policy = readonly_verifier_policy(args.expiry, issuance=args.issuance)
    if args.json:
        print(json.dumps(policy, indent=2, ensure_ascii=True))
    else:
        print(f"  permission set : {PERMISSION_SET_NAME}")
        print(f"  actions        : {len(ALL_ACTIONS)} (all reads)")
        print(f"  canonical hash : {policy_hash(policy)}")
        print("READONLY VERIFIER POLICY: generated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
