#!/usr/bin/env python3
"""Boundary rollout owner graph, with ownership COMPUTED rather than asserted (Gate 4N-I7).

Gate 4N-I6 published `ownerless_operations: 0` as a hand-maintained field in a static JSON
file, next to a principal — the boundary bootstrap executor — whose policy did not
exist anywhere. The invariant was therefore unfalsifiable: nothing could have made it
report a non-zero value.

Here every operation names an action, a resource and a principal, and this module
EVALUATES that action against that principal's actual policy document with the shared
evaluator. `ownerless_operations` is the count of operations whose evaluation is not
EXPLICIT_ALLOW. Deleting a grant from the bootstrap policy makes the number move.

Two principals are evaluated:
  SignalNestBoundaryBootstrapOp        scripts/gen_bootstrap_operator_policy.py
  SignalNestStagingReadOnly            live-captured baseline, replayed from the pinned
                                       capability list below (read-only, live-probed in
                                       Gate 4N-I6)

`root_console` operations are NOT evaluated — they are permission-set administration that
no principal in this design holds, which is the honest statement rather than a gap.

Usage:
    python3 scripts/gen_boundary_rollout.py [--check]
Exit: 0 all invariants hold; 1 otherwise.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import gen_bootstrap_operator_policy as boot  # noqa: E402
import gen_boundary_policy as gb  # noqa: E402
import iam_eval  # noqa: E402
from iam_eval import Decision  # noqa: E402
from signalnest_identity import (  # noqa: E402
    ALL_ROLE_ARNS, BOUNDARY_POLICY_ARN, BOOTSTRAP_OPERATOR_NAME,
    MODULE_IAM_ROLE_NAMES, iam_role_arn,
)

BOOTSTRAP = BOOTSTRAP_OPERATOR_NAME
READONLY = "SignalNestStagingReadOnly"
ROOT = "root_console"

# The five roles that ALREADY EXIST. The three reader roles are created already-bounded by
# the Gate 4N operator, so they are never read back here.
EXISTING_ROLE_ARNS = [iam_role_arn(n) for n in MODULE_IAM_ROLE_NAMES]

# Gate 4N-I6 live-probed these three under SignalNestStagingReadOnly. iam:ListEntitiesForPolicy
# is deliberately ABSENT: the live probe returned AccessDenied, which is exactly the Gate
# 4N-I5 defect that moved operation 12 to the executor.
READONLY_CAPABILITIES = {
    "Version": "2012-10-17",
    "Statement": [{
        "Sid": "LiveProbedReadOnlySubset",
        "Effect": "Allow",
        "Action": ["iam:GetPolicy", "iam:GetRole", "iam:ListRoles"],
        "Resource": "*",
    }],
}

# GATE 4N-I8 DEFECT 11. This froze the clock at 2000-01-01 against an UNSTAMPED policy, so
# the rollout proof never exercised a real expiry. The policy is now stamped and the clock
# sits inside the window.
# GATE 4N-I19, ADV-A: the ONE authoritative reviewed window. Gate 4N-I17's architect lane
# found ~20 independent expiry literals with nothing asserting they agreed; they now all
# resolve to the single authorized pair, so a restamp cannot leave stragglers behind.
import expiry_authorization as _ea  # noqa: E402

ROLLOUT_EXPIRY = _ea.ACTIVE_EXPIRY_UTC
WINDOW_CONTEXT = {"aws:CurrentTime": "2026-07-31T12:00:00Z"}


def operations() -> list[dict]:
    boundary_canonical = hashlib.sha256(gb.canonical(gb.boundary_policy())).hexdigest()
    return [
        {"n": 1, "op": "provision the temporary executor", "principal": ROOT,
         "actions": ["sso:CreatePermissionSet", "sso:PutInlinePolicyToPermissionSet",
                     "sso:ProvisionPermissionSet", "sso:CreateAccountAssignment"],
         "note": "the executor deliberately lacks sso:*, so it cannot provision itself. "
                 "ICPermAdmin cannot either: its live policy grants PutInlinePolicy + "
                 "Provision only, not Create/DeletePermissionSet or account assignments."},
        {"n": 2, "op": "create boundary policy", "principal": BOOTSTRAP,
         "actions": ["iam:CreatePolicy"], "resources": [BOUNDARY_POLICY_ARN]},
        {"n": 3, "op": "retrieve policy", "principal": BOOTSTRAP,
         "actions": ["iam:GetPolicy"], "resources": [BOUNDARY_POLICY_ARN]},
        {"n": 4, "op": "retrieve default version", "principal": BOOTSTRAP,
         "actions": ["iam:GetPolicyVersion"], "resources": [BOUNDARY_POLICY_ARN]},
        {"n": 5, "op": "verify canonical hash equals the reviewed value",
         "principal": BOOTSTRAP, "actions": [], "resources": [],
         "expected_canonical": boundary_canonical,
         "note": "offline comparison; no API call, so no owner is required"},
        # OPERATING MODEL 1: the executor creates the boundary ONCE and can never revise it.
        # If the created bytes do not match the reviewed hash, the run STOPS and the
        # correction is a new gate — it is not something this principal can paper over.
        {"n": 6, "op": "if the hash does not match, STOP; correction requires a new gate",
         "principal": ROOT, "actions": ["iam:CreatePolicyVersion", "iam:SetDefaultPolicyVersion"],
         "note": "root-console only. The executor deliberately holds no policy-version "
                 "capability, so it cannot rewrite the ceiling it is installing."},
        {"n": 7, "op": "confirm the default version is the reviewed one", "principal": BOOTSTRAP,
         "actions": ["iam:ListPolicyVersions"], "resources": [BOUNDARY_POLICY_ARN]},
        {"n": 8, "op": "inspect references", "principal": BOOTSTRAP,
         "actions": ["iam:ListEntitiesForPolicy"], "resources": [BOUNDARY_POLICY_ARN]},
        {"n": 9, "op": "apply boundary to the five EXISTING roles", "principal": BOOTSTRAP,
         "actions": ["iam:PutRolePermissionsBoundary"], "resources": EXISTING_ROLE_ARNS,
         "context": {"iam:PermissionsBoundary": BOUNDARY_POLICY_ARN},
         "note": "the three reader roles do not exist yet and are created already-bounded "
                 "by the Gate 4N operator — they are NOT touched here"},
        {"n": 10, "op": "read back each role's boundary", "principal": BOOTSTRAP,
         "actions": ["iam:GetRole"], "resources": EXISTING_ROLE_ARNS},
        {"n": 11, "op": "read back effective policy surface", "principal": BOOTSTRAP,
         "actions": ["iam:GetRolePolicy", "iam:ListAttachedRolePolicies",
                     "iam:ListRolePolicies"],
         "resources": EXISTING_ROLE_ARNS},
        {"n": 12, "op": "FINAL reference check BEFORE retirement", "principal": BOOTSTRAP,
         "actions": ["iam:ListEntitiesForPolicy"], "resources": [BOUNDARY_POLICY_ARN],
         "note": "before retirement and assigned to a principal that actually holds it"},
        {"n": 13, "op": "rollback if any check failed", "principal": BOOTSTRAP,
         "actions": ["iam:DeleteRolePermissionsBoundary"], "resources": EXISTING_ROLE_ARNS,
         "context": {"iam:PermissionsBoundary": BOUNDARY_POLICY_ARN},
         "note": "strictly before retirement"},
        {"n": 14, "op": "rollback the policy itself if created in error",
         "principal": ROOT,
         # Root-console under Operating Model 1. Policy DELETION is not a capability the
         # temporary executor holds, so an executor that misbehaves cannot remove the
         # evidence of what it created.
         "actions": ["iam:ListPolicyVersions", "iam:DeletePolicyVersion", "iam:DeletePolicy"],
         "resources": [BOUNDARY_POLICY_ARN]},
        {"n": 15, "op": "retire the temporary executor", "principal": ROOT,
         "actions": ["sso:DeleteAccountAssignment", "sso:DeletePermissionSet"]},
        {"n": 16, "op": "post-retirement residual check", "principal": READONLY,
         "actions": ["iam:GetPolicy", "iam:GetRole", "iam:ListRoles"],
         "resources": [BOUNDARY_POLICY_ARN] + list(ALL_ROLE_ARNS),
         "note": "iam:ListEntitiesForPolicy is NOT used here — live-probed AccessDenied"},
    ]


POLICIES = {
    BOOTSTRAP: lambda: boot.bootstrap_operator_policy(ROLLOUT_EXPIRY),
    READONLY: lambda: READONLY_CAPABILITIES,
}


def evaluate() -> dict:
    rows, ownerless, retired_before_use, rollback_without_owner = [], [], [], []
    retirement_step = max(o["n"] for o in operations() if o["principal"] == ROOT)

    for op in operations():
        principal = op["principal"]
        for action in op.get("actions", []):
            if principal == ROOT:
                rows.append({"n": op["n"], "action": action, "principal": principal,
                             "decision": "ROOT_CONSOLE_NOT_EVALUATED"})
                continue
            policy = POLICIES[principal]()
            for resource in op.get("resources") or ["*"]:
                ctx = dict(WINDOW_CONTEXT)
                ctx.update(op.get("context", {}))
                result = iam_eval.decide(policy, action, resource, ctx)
                row = {"n": op["n"], "action": action, "resource": resource,
                       "principal": principal, "decision": result.decision.name}
                rows.append(row)
                if result.decision is not Decision.EXPLICIT_ALLOW:
                    ownerless.append(row)
                # An operation performed by the temporary executor AFTER its retirement
                # step would be unexecutable regardless of policy.
                if principal == BOOTSTRAP and op["n"] > retirement_step:
                    retired_before_use.append(row)
                if "rollback" in op["op"] and result.decision is not Decision.EXPLICIT_ALLOW:
                    rollback_without_owner.append(row)

    return {
        "boundary_policy_arn": BOUNDARY_POLICY_ARN,
        "principals_evaluated": [BOOTSTRAP, READONLY],
        "rows": rows,
        "invariants": {
            "ownerless_operations": len(ownerless),
            "retired_before_use_operations": len(retired_before_use),
            "rollback_without_owner_operations": len(rollback_without_owner),
        },
        "ownerless_detail": ownerless,
        "root_dependency_stated_honestly":
            "operations 1 and 15 require permission-set administration that the executor "
            "deliberately lacks and ICPermAdmin does not hold. They are root-console only.",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    result = evaluate()
    graph = {"operations": operations(), **result}
    if args.check:
        inv = result["invariants"]
        for key, value in inv.items():
            print(f"  {key:42s} {value}")
        for row in result["ownerless_detail"]:
            print(f"    OWNERLESS op {row['n']} {row['action']} -> {row['decision']}",
                  file=sys.stderr)
        ok = all(v == 0 for v in inv.values())
        print("ROLLOUT OWNERSHIP: clean" if ok else "ROLLOUT OWNERSHIP: ownerless operations")
        return 0 if ok else 1
    print(json.dumps(graph, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
