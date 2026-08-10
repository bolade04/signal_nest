#!/usr/bin/env python3
"""Deterministic generator for the SignalNestBoundaryBootstrapOperator policy (Gate 4N-I7).

THE DEFECT THIS CLOSES. The Gate 4N-I6 rollout assigned 12 of 15 operations to this
principal while its exact policy bytes existed nowhere — not in AWS, not in the
repository, not as a generator output, not as a reviewed artifact. That is Defect 3 from
the previous gate recreated for a MORE privileged principal, and it made the rollout's
`ownerless_operations: 0` claim unverifiable.

SCOPE. Boundary policy lifecycle plus attachment to exactly the eight repository-managed
roles, plus the read-back needed to prove the result. Nothing else. In particular it holds
no role creation or deletion, no PutRolePolicy, no PassRole, no secret, state, ECS, RDS,
S3 or CloudTrail access, and no permission-set administration — provisioning and retiring
the permission set itself are root-console operations, stated as such in the rollout graph
rather than smuggled in here.

Identity comes from scripts/signalnest_identity.py; this module never rebuilds an ARN.

Usage:
    python3 scripts/gen_bootstrap_operator_policy.py [--hash] [--expiry ISO8601]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import iam_eval  # noqa: E402
from must_not_contract import FORBIDDEN_CAPABILITIES  # noqa: E402
from signalnest_identity import (  # noqa: E402
    ALL_ROLE_ARNS, BOUNDARY_POLICY_ARN, BOOTSTRAP_OPERATOR_NAME,
)

# GATE 4N-I8 DEFECT 3. There is deliberately NO placeholder constant any more. Both
# generators defaulted --expiry to "<EXPIRY-ISO8601>", so the reviewed artifacts were hashed
# with a literal placeholder: AWS would reject them as an invalid Date, the evaluator's
# string comparison made every clock satisfy them, validate_policy said nothing, and the
# tests exercised freshly-stamped documents rather than the bytes on disk. A missing expiry
# is now a generation FAILURE.

# OPERATING MODEL 1 — CREATE-ONCE BOUNDARY (Gate 4N-I8 Phase N).
#
# The Gate 4N-I7 architect lane found that this principal could rewrite the reviewed boundary
# to Allow * and set it default: it held CreatePolicyVersion + SetDefaultPolicyVersion +
# DeletePolicy on the boundary ARN, and none of those three was in the must-not contract. A
# principal that can rewrite the security ceiling defeats the entire design, so the fix is
# not a tighter condition — AWS has no condition key over policy-document bytes — but
# REMOVING the capability.
#
# This principal may now CREATE the boundary policy once, and READ it. It cannot version it,
# cannot change the default version, and cannot delete it. Changing the boundary document
# after creation is therefore a separate, separately-reviewed gate, and deleting it is a
# root-console operation. That is the smallest ability to rewrite the ceiling among the
# models considered, which is the stated selection criterion.
POLICY_LIFECYCLE_ACTIONS = [
    "iam:CreatePolicy",
    "iam:GetPolicy",
    "iam:GetPolicyVersion",
    "iam:ListEntitiesForPolicy",
    "iam:ListPolicyVersions",
]

# Deliberately NOT granted, and denied by the ceiling below:
#   iam:CreatePolicyVersion      would let it author replacement boundary bytes
#   iam:SetDefaultPolicyVersion  would let it activate them
#   iam:DeletePolicyVersion      would let it remove the reviewed version
#   iam:DeletePolicy             would let it remove the boundary outright
RETAINED_BY_ROOT = [
    "iam:CreatePolicyVersion", "iam:SetDefaultPolicyVersion",
    "iam:DeletePolicyVersion", "iam:DeletePolicy",
]

# GATE 4N-I10 DEFECT 7 — THE DISPUTED CONDITION IS OFF THE ROLLBACK PATH.
#
# These two actions used to share ONE statement carrying
# StringEquals{iam:PermissionsBoundary}. Whether AWS populates that key for
# DeleteRolePermissionsBoundary is disputed: the Gate 4N-I7 security lane read the service
# authorization reference as NOT populating it; I read it as populated with the CURRENTLY
# attached boundary, per the permissions-boundary delegation pattern. Neither reading was
# proven, and settling it needs a policy-simulator call this gate chain has never been
# authorized to make.
#
# The danger is asymmetric and points the wrong way. A StringEquals against a key ABSENT from
# the request context evaluates FALSE, so if the pessimistic reading is right the grant is
# DEAD AT RUNTIME — and it is dead precisely when rollback is being attempted, i.e. when
# something has already gone wrong. An uncertain condition on a rollback path is worse than
# no condition at all.
#
# So the statements are SPLIT:
#   PutRolePermissionsBoundary  KEEPS the condition. Support there is undisputed, and the
#                               condition is what stops a permissive boundary being attached.
#   DeleteRolePermissionsBoundary  gets its own statement with NO iam:PermissionsBoundary
#                               condition. Removal carries no boundary ARN in the request for
#                               a condition to constrain, so the condition bought nothing on
#                               this action even under the optimistic reading. It is scoped by
#                               exact Resource to the eight roles and fenced by NotResource.
#
# WHY THIS IS SAFE IF THE KEY IS NEVER POPULATED: the removal grant has no dependency on it.
# WHY IT IS SAFE IF THE KEY IS POPULATED: removal is still confined to the eight role ARNs,
# and the principal expires.
BOUNDARY_ATTACH_ACTIONS = ["iam:PutRolePermissionsBoundary"]
BOUNDARY_REMOVE_ACTIONS = ["iam:DeleteRolePermissionsBoundary"]
BOUNDARY_ATTACHMENT_ACTIONS = BOUNDARY_ATTACH_ACTIONS + BOUNDARY_REMOVE_ACTIONS

ROLE_READ_ACTIONS = [
    "iam:GetRole",
    "iam:GetRolePolicy",
    "iam:ListAttachedRolePolicies",
    "iam:ListRolePolicies",
]

# The internal ceiling, DERIVED from the must-not contract rather than hand-listed.
#
# The hand-written first draft of this list omitted eight capabilities that the contract
# forbids — among them kms:PutKeyPolicy, kms:CreateGrant, secretsmanager:DeleteSecret and
# ecs:ExecuteCommand — and the Allow-axis proof scored this principal 31/39. Deriving the
# list closes that class of omission permanently: a capability added to the contract is
# denied here automatically, with no edit to this file.
#
# The two exceptions are the principal's actual job. Both stay grantable, but only through
# the conditioned statement below, and scripts/allow_model.py requires them to remain
# denied on every role outside the eight.
# The exceptions are this principal's job, and each is resource-scoped by the Allow above
# plus a NotResource fence below. iam:CreatePolicy is scoped to the boundary policy ARN
# alone; scripts/allow_model.py requires it to remain denied on every other policy.
CEILING_EXCEPTIONS = frozenset(set(BOUNDARY_ATTACHMENT_ACTIONS) | {"iam:CreatePolicy"})

FORBIDDEN = sorted(set(FORBIDDEN_CAPABILITIES) - CEILING_EXCEPTIONS)

# Service-wide denies for the administrative surfaces this principal must never touch.
# The contract names individual sso:* actions; these close the rest of those services.
FORBIDDEN += ["identitystore:*", "organizations:*", "sso:*"]
FORBIDDEN = sorted(set(FORBIDDEN))


def bootstrap_operator_policy(expiry: str, *, issuance: str | None = None) -> dict:
    """`expiry` is REQUIRED and must be a real RFC 3339 UTC instant."""
    # GATE 4N-I19, ADV-A. The window must be AUTHORIZED, not merely well-formed. Gate 4N-I17
    # showed a 2099 stamp generating cleanly with the whole suite green; this call is what
    # makes an unbounded or already-expired window fail BEFORE any policy output exists.
    import expiry_authorization

    expiry_authorization.authorize(
        issuance=issuance if issuance is not None else expiry_authorization.ACTIVE_ISSUANCE_UTC,
        expiry=expiry, purpose="boundary_bootstrap")

    require_valid_expiry(expiry)
    expiring = {"DateLessThan": {"aws:CurrentTime": expiry}}
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "BoundaryPolicyLifecycle",
                "Effect": "Allow",
                "Action": POLICY_LIFECYCLE_ACTIONS,
                "Resource": BOUNDARY_POLICY_ARN,
                "Condition": expiring,
            },
            {
                # Conditioned on the boundary ARN so this principal can attach ONLY the
                # reviewed boundary — not some other policy — as a permissions boundary.
                # Both actions support the iam:PermissionsBoundary condition key.
                "Sid": "BoundaryAttachmentToExactRoles",
                "Effect": "Allow",
                "Action": BOUNDARY_ATTACH_ACTIONS,
                "Resource": list(ALL_ROLE_ARNS),
                "Condition": {
                    "DateLessThan": {"aws:CurrentTime": expiry},
                    "StringEquals": {"iam:PermissionsBoundary": BOUNDARY_POLICY_ARN},
                },
            },
            {
                # NO iam:PermissionsBoundary condition. See the note above: its runtime
                # population on this action is UNPROVEN, and an unpopulated key would make
                # this grant evaluate FALSE exactly when rollback is needed.
                "Sid": "BoundaryRemovalForRollbackWithoutDisputedCondition",
                "Effect": "Allow",
                "Action": BOUNDARY_REMOVE_ACTIONS,
                "Resource": list(ALL_ROLE_ARNS),
                "Condition": {"DateLessThan": {"aws:CurrentTime": expiry}},
            },
            {
                "Sid": "RoleReadBackForEffectivePermissionCheck",
                "Effect": "Allow",
                "Action": ROLE_READ_ACTIONS,
                "Resource": list(ALL_ROLE_ARNS),
                "Condition": expiring,
            },
            {
                "Sid": "CallerIdentity",
                "Effect": "Allow",
                "Action": "sts:GetCallerIdentity",
                "Resource": "*",
                "Condition": expiring,
            },
            {
                # No expiry: a ceiling that lapses stops protecting exactly when the
                # window is abused.
                "Sid": "BootstrapDenyEscalation",
                "Effect": "Deny",
                "Action": FORBIDDEN,
                "Resource": "*",
            },
            {
                # The two boundary-attachment actions are this principal's whole purpose,
                # so they cannot go in the flat deny above. Without this fence they were
                # available on EVERY role in the account — the Allow statement scoped them,
                # but only by implicit denial, which any later-attached policy can lift.
                # The Allow-axis proof reported exactly that as an escape.
                "Sid": "BootstrapDenyBoundaryAdministrationOutsideTheEightRoles",
                "Effect": "Deny",
                "Action": BOUNDARY_ATTACHMENT_ACTIONS,
                "NotResource": list(ALL_ROLE_ARNS),
            },
            {
                # iam:CreatePolicy is granted for the boundary ARN alone. Without this fence
                # it would reach every policy name in the account by implicit denial only,
                # which a later-attached policy can lift — the same shape as the boundary
                # attachment escape above.
                "Sid": "BootstrapDenyPolicyCreationOutsideTheBoundary",
                "Effect": "Deny",
                "Action": "iam:CreatePolicy",
                "NotResource": BOUNDARY_POLICY_ARN,
            },
        ],
    }


def require_valid_expiry(expiry: object) -> None:
    """Reject a missing, placeholder or malformed expiry at GENERATION time.

    Catching it here means a placeholder can never reach a hashed artifact in the first
    place, which is strictly better than detecting it downstream.
    """
    if expiry is None or expiry == "":
        raise ValueError("expiry is REQUIRED; there is no placeholder default")
    iam_eval.parse_iam_date(expiry, what="policy expiry")


def canonical(doc: dict) -> bytes:
    return json.dumps(doc, sort_keys=True, separators=(",", ":")).encode("utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hash", action="store_true")
    parser.add_argument("--expiry", required=True,
                        help="REQUIRED RFC 3339 UTC expiry, e.g. 2026-07-31T18:00:00Z")
    args = parser.parse_args()
    doc = bootstrap_operator_policy(args.expiry)
    rendered = json.dumps(doc, indent=2) + "\n"
    if args.hash:
        allows = [s for s in doc["Statement"] if s["Effect"] == "Allow"]
        actions = {a for s in doc["Statement"] for a in
                   (s["Action"] if isinstance(s["Action"], list) else [s["Action"]])}
        print(f"name       {BOOTSTRAP_OPERATOR_NAME}")
        print(f"canonical  {hashlib.sha256(canonical(doc)).hexdigest()}")
        print(f"file_byte  {hashlib.sha256(rendered.encode('utf-8')).hexdigest()}")
        print(f"statements {len(doc['Statement'])}  allow_stmts {len(allows)}")
        print(f"actions    {len(actions)}")
        return 0
    sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
