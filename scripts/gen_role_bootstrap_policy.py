#!/usr/bin/env python3
"""SignalNestRoleBootstrapOperator — the ONLY principal that may create roles (Gate 4N-I9).

WHY THIS PRINCIPAL EXISTS. `iam:CreateRole` accepts the AssumeRolePolicyDocument in the
request, and AWS provides no condition key comparing the whole submitted trust document to
an approved hash. Through Gate 4N-I8 that capability sat on the Stage-A operator, which also
held state access, ECR mutation, secret reads and the full refresh closure — so a single
compromised window could mint an approved role name with an external-account trust that
SURVIVED the window.

Splitting it out does not make trust bytes enforceable. Nothing does. What it buys is:

  BLAST RADIUS   this principal holds role creation and nothing else. No state, no secrets,
                 no ECR, no ECS, no policy lifecycle, no PassRole.
  REVIEWABILITY  it exists for one operation with one reviewed input, so the read-back
                 comparison is a short, checkable step rather than one item in a long apply.
  REVERSIBILITY  it holds iam:DeleteRole on exactly these three ARNs, so a mismatch detected
                 at read-back is reverted immediately by the same operator.

MODEL SELECTION (Gate 4N-I9 Phase B). Model B — dedicated RoleBootstrapOperator. Model D
(root-console creation) scores higher on arbitrary-trust resistance but has no read-back
automation and no rollback path without a second root session. Model C (CloudFormation with
a pinned template hash) can genuinely constrain the document via `cloudformation:TemplateUrl`,
but requires an S3-hosted template, a service role, and a stack lifecycle this design does
not otherwise need — more moving parts holding more privilege. Model B with mandatory
read-back was selected as the smallest capability that still permits automated revert.

THE HONEST STATEMENT, which must not be softened in review: the trust-byte control here is
DETECT-AND-REVERT, not PREVENT. Between CreateRole returning and the read-back completing,
an incorrect trust policy exists in the account.

Usage:
    python3 scripts/gen_role_bootstrap_policy.py --expiry ISO8601 [--hash]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import iam_eval  # noqa: E402
from must_not_contract import FORBIDDEN_CAPABILITIES  # noqa: E402
from signalnest_identity import (  # noqa: E402
    BOUNDARY_POLICY_ARN, REVISION_READER_ROLE_NAMES, iam_role_arn,
)

ROLE_BOOTSTRAP_OPERATOR_NAME = "SignalNestRoleBootstrapOperator"

# Exactly the three reader roles. Nothing else, ever.
TARGET_ROLE_ARNS = [iam_role_arn(n) for n in REVISION_READER_ROLE_NAMES]

# GATE 4N-I11 DEFECT 5. These were ONE statement carrying
# StringEquals{iam:PermissionsBoundary}. A Condition block applies to EVERY action in its
# statement, and iam:TagRole does not support that key — it supports only
# aws:RequestTag/${TagKey}, aws:TagKeys and the ResourceTag forms. A StringEquals against a
# key absent from the request context never matches, so the TagRole half of that grant was
# DEAD, and AWS requires iam:TagRole when Tags are supplied to CreateRole. The role would
# have been created untagged, failed the ListRoleTags read-back against tags_expectation,
# and been deleted by the rollback — a bootstrap loop.
#
# Worse: this repository already contained the detector (iam_eval.ACTION_CONDITION_KEYS) and
# a negative-control test that builds this EXACT shape to prove the detector fires. The
# shipped policy was simply never passed to validate_policy. Phase K makes that structural.
#
# Split. CreateRole keeps the boundary condition, where support is documented. TagRole is
# scoped by exact role ARN and constrained on the TAG axis instead, using keys it does
# support.
CREATE_ROLE_ACTIONS = ["iam:CreateRole"]
TAG_ACTIONS = ["iam:TagRole"]
CREATE_ACTIONS = CREATE_ROLE_ACTIONS + TAG_ACTIONS

# The only tag the reviewed trust manifest declares. Constraining the tag KEYS is the
# strongest control TagRole actually supports: it stops this principal inventing tags that
# some other policy might key an authorization decision on.
ALLOWED_TAG_KEYS = ["Name"]
READ_BACK_ACTIONS = ["iam:GetRole", "iam:ListRoleTags"]
# Rollback. Present ONLY because the read-back comparison is the actual trust control, and a
# control that detects without reverting is an alert, not a control.
ROLLBACK_ACTIONS = ["iam:DeleteRole"]

# iam:PutRolePolicy is DELIBERATELY ABSENT. The reader roles' inline policies are applied by
# the Stage-A operator through OpenTofu against roles that already exist, so this principal
# never needs it — and every capability it does not hold is one the trust window cannot
# abuse. iam:PutRolePermissionsBoundary is also absent: the boundary is supplied AT
# CreateRole and conditioned below, so a separate attach step would only add a second way in.
CEILING_EXCEPTIONS = frozenset(CREATE_ACTIONS + ROLLBACK_ACTIONS)

FORBIDDEN = sorted(set(FORBIDDEN_CAPABILITIES) - CEILING_EXCEPTIONS)
FORBIDDEN += ["identitystore:*", "organizations:*", "sso:*"]
FORBIDDEN = sorted(set(FORBIDDEN))


def require_valid_expiry(expiry: object) -> None:
    if expiry is None or expiry == "":
        raise ValueError("expiry is REQUIRED; there is no placeholder default")
    iam_eval.parse_iam_date(expiry, what="policy expiry")


class TagKeyDomainError(ValueError):
    """Fail-closed. A tag key no reviewed source declares never reaches a generated policy."""


def reviewed_tag_key_domain() -> set[str]:
    """The tag keys the REVIEWED TRUST MANIFEST declares, from the manifest itself.

    Deliberately NOT derived from ALLOWED_TAG_KEYS: a list compared against a copy of itself
    agrees with anything. trust_policies authors `tags_expectation` from the trust documents,
    role_bootstrap_executor sends exactly those tags to AWS, and the ListRoleTags read-back
    compares against them — so the manifest is the independent authority over which keys these
    roles may carry, and neither module imports the other.
    """
    import trust_policies

    keys: set[str] = set()
    for entry in trust_policies.trust_manifest().values():
        keys |= set(entry["tags_expectation"])
    if not keys:
        raise TagKeyDomainError(
            "the reviewed trust manifest declares NO tag keys. An empty expected domain must "
            "never be read as 'every key is approved'.")
    return keys


def require_reviewed_tag_keys(keys: object) -> None:
    """GATE 4N-I27M. ALLOWED_TAG_KEYS is an AUTHORIZATION CONDITION, not metadata.

    THE DEFECT THIS CLOSES. The list is interpolated into ForAllValues:StringEquals on
    aws:TagKeys for iam:TagRole, so it bounds which tag keys this principal may set on the
    three reader roles inside a live trust window — the module's own comment says the point is
    to stop it "inventing tags that some other policy might key an authorization decision on".
    Adding an unreviewed key moved an iam:TagRole request carrying that key from IMPLICIT_DENY
    to EXPLICIT_ALLOW under the repository's own evaluator, and NOTHING refused: all seven
    downstream consumers, the policy test suite and the graded lifecycle command exited 0. A
    control with no independent source behind it is a control that only reviews enforce.

    BOTH directions are checked. An unreviewed key widens the grant; a MISSING reviewed key
    narrows it below what the executor actually sends, which kills the tagging the read-back
    depends on. Refusal happens BEFORE any policy output exists, so an unreviewed key cannot
    reach a generated artifact at all.
    """
    if not isinstance(keys, (list, tuple)) or not all(isinstance(k, str) and k for k in keys):
        raise TagKeyDomainError(f"tag keys must be a non-empty list of strings, got {keys!r}")
    declared, reviewed = set(keys), reviewed_tag_key_domain()
    unreviewed = sorted(declared - reviewed)
    if unreviewed:
        raise TagKeyDomainError(
            f"tag key(s) {unreviewed} are NOT declared by the reviewed trust manifest "
            f"{sorted(reviewed)}. aws:TagKeys is an authorization condition: a key no "
            "reviewed source declares may not be granted to this principal.")
    missing = sorted(reviewed - declared)
    if missing:
        raise TagKeyDomainError(
            f"the reviewed trust manifest declares tag key(s) {missing} that the policy does "
            "not permit. The executor sends exactly those tags, so the grant would refuse the "
            "tagging its own read-back verifies.")


def role_bootstrap_policy(expiry: str, *, issuance: str | None = None) -> dict:
    # GATE 4N-I19, ADV-A. The window must be AUTHORIZED, not merely well-formed. Gate 4N-I17
    # showed a 2099 stamp generating cleanly with the whole suite green; this call is what
    # makes an unbounded or already-expired window fail BEFORE any policy output exists.
    import expiry_authorization

    expiry_authorization.authorize(
        issuance=issuance if issuance is not None else expiry_authorization.ACTIVE_ISSUANCE_UTC,
        expiry=expiry, purpose="role_bootstrap")

    require_valid_expiry(expiry)
    # GATE 4N-I27M. Same shape as the expiry check above and for the same reason: the tag-key
    # allow-list is validated against an INDEPENDENT reviewed source before any policy output
    # exists, so an unreviewed key can never reach a generated or hashed artifact.
    require_reviewed_tag_keys(ALLOWED_TAG_KEYS)
    expiring = {"DateLessThan": {"aws:CurrentTime": expiry}}
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                # Conditioned on iam:PermissionsBoundary, which CreateRole DOES support. This
                # guarantees the created role is bounded — it says nothing about the trust
                # document, which is why the read-back exists.
                "Sid": "CreateExactlyTheThreeReaderRolesBounded",
                "Effect": "Allow",
                "Action": CREATE_ROLE_ACTIONS,
                "Resource": TARGET_ROLE_ARNS,
                "Condition": {
                    "DateLessThan": {"aws:CurrentTime": expiry},
                    "StringEquals": {"iam:PermissionsBoundary": BOUNDARY_POLICY_ARN},
                },
            },
            {
                # NO iam:PermissionsBoundary here — TagRole does not support it and the
                # condition would render the grant dead. Constrained on the tag axis, which
                # TagRole does support, plus the same exact role ARNs.
                "Sid": "TagExactlyTheThreeReaderRolesWithApprovedKeysOnly",
                "Effect": "Allow",
                "Action": TAG_ACTIONS,
                "Resource": TARGET_ROLE_ARNS,
                "Condition": {
                    "DateLessThan": {"aws:CurrentTime": expiry},
                    "ForAllValues:StringEquals": {"aws:TagKeys": ALLOWED_TAG_KEYS},
                },
            },
            {
                # The read-back is the trust control. Without it this principal is strictly
                # worse than what it replaced.
                "Sid": "ReadBackToVerifyTheTrustDocument",
                "Effect": "Allow",
                "Action": READ_BACK_ACTIONS,
                "Resource": TARGET_ROLE_ARNS,
                "Condition": expiring,
            },
            {
                # Revert on mismatch. Scoped to the same three ARNs; it cannot delete
                # anything it did not create.
                "Sid": "RollbackOnTrustMismatch",
                "Effect": "Allow",
                "Action": ROLLBACK_ACTIONS,
                "Resource": TARGET_ROLE_ARNS,
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
                # No expiry: a ceiling that lapses stops protecting exactly when the window
                # is being abused.
                "Sid": "RoleBootstrapDenyEscalation",
                "Effect": "Deny",
                "Action": FORBIDDEN,
                "Resource": "*",
            },
            {
                "Sid": "RoleBootstrapDenyRoleAuthoringOutsideTheThreeReaderRoles",
                "Effect": "Deny",
                "Action": CREATE_ACTIONS + ROLLBACK_ACTIONS,
                "NotResource": TARGET_ROLE_ARNS,
            },
            {
                # UpdateAssumeRolePolicy would let this principal REWRITE the trust of a role
                # that already passed read-back — defeating the only control there is. It is
                # in the flat deny above too; stated separately because it is the single most
                # important thing this principal must not hold.
                "Sid": "RoleBootstrapDenyTrustRewrite",
                "Effect": "Deny",
                "Action": ["iam:UpdateAssumeRolePolicy", "iam:PutRolePolicy",
                           "iam:AttachRolePolicy", "iam:PassRole"],
                "Resource": "*",
            },
        ],
    }


def canonical(doc: dict) -> bytes:
    return json.dumps(doc, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode("utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hash", action="store_true")
    parser.add_argument("--expiry", required=True,
                        help="REQUIRED RFC 3339 UTC expiry, e.g. 2026-07-31T18:00:00Z")
    args = parser.parse_args()
    doc = role_bootstrap_policy(args.expiry)
    rendered = json.dumps(doc, indent=2, ensure_ascii=True) + "\n"
    if args.hash:
        print(f"name       {ROLE_BOOTSTRAP_OPERATOR_NAME}")
        print(f"canonical  {hashlib.sha256(canonical(doc)).hexdigest()}")
        print(f"file_byte  {hashlib.sha256(rendered.encode('utf-8')).hexdigest()}")
        print(f"statements {len(doc['Statement'])}")
        print(f"expiry     {args.expiry}")
        return 0
    sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
