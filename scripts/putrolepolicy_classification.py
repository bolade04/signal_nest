#!/usr/bin/env python3
"""Closure-driven classification of iam:PutRolePolicy (Gate 4N-I16, Defect 3, Phase J).

THE DEFECT. `scripts/verify_closure.py` excluded iam:PutRolePolicy from every principal's
closure check with this rationale:

    "PutRolePolicy is applied by OpenTofu against roles that ALREADY exist, so the role
     bootstrap operator deliberately does not hold it; it is not required of any principal
     at create time and is excluded rather than silently passed."

The premise is false. `aws_iam_role_policy` is an INLINE POLICY resource: creating one calls
iam:PutRolePolicy regardless of whether the role pre-exists. The repository's own
provider-operation map says so one line of JSON away. With the action excluded, the closure
reported clean while no principal held an action the composition cannot apply without.

WORSE, TWO PRIMARY SOURCES CONTRADICTED EACH OTHER AND NOTHING NOTICED:
    scripts/gen_role_bootstrap_policy.py  — "applied by the Stage-A operator ... so this
                                             principal never needs it"
    scripts/gen_operator_policies.py      — "Stage-A no longer authors roles at all, so they
                                             are flatly denied"
Each generator disclaimed the action by pointing at the other. Between them, nobody held it.

THIS MODULE DERIVES THE ANSWER instead of asserting it: it reads the resource declarations
out of the repository, reads the provider operation map, and classifies into exactly one of
the four permitted outcomes. UNKNOWN is a gate failure, never an exclusion.

Usage:
    python3 scripts/putrolepolicy_classification.py [--json]
Exit: 0 iff the action is classified and the classification is satisfied by the policy set.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import iam_eval  # noqa: E402
import signalnest_identity as identity  # noqa: E402

ACTION = "iam:PutRolePolicy"

REQUIRED_TEMPORARILY = "REQUIRED_TEMPORARILY"
OBSOLETE = "OBSOLETE"
REPLACED_BY_DIFFERENT_MECHANISM = "REPLACED_BY_DIFFERENT_MECHANISM"
UNRESOLVED = "UNKNOWN"

OPERATION_MAP = REPO_ROOT / "infra" / "aws" / "provider-api-operation-map.json"
INFRA = REPO_ROOT / "infra" / "aws"

EXPIRY_PROBE = {"aws:CurrentTime": "2026-07-31T12:00:00Z",
                "aws:RequestedRegion": identity.REGION,
                "iam:PermissionsBoundary": identity.BOUNDARY_POLICY_ARN}


def declared_inline_policy_resources() -> list[dict]:
    """PRIMARY EVIDENCE 1 — what the repository actually declares.

    Not a hand-maintained list: the .tf files are read. If every aws_iam_role_policy resource
    were deleted tomorrow, this returns empty and the classification becomes OBSOLETE on its
    own, without anyone remembering to update a constant.
    """
    found = []
    for path in sorted(INFRA.rglob("*.tf")):
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r'resource\s+"aws_iam_role_policy"\s+"([A-Za-z0-9_]+)"', text):
            found.append({"module": str(path.relative_to(REPO_ROOT)), "name": match.group(1)})
    return found


def provider_requires_action() -> dict:
    """PRIMARY EVIDENCE 2 — the provider operation map, the repository's own SOURCE 2."""
    doc = json.loads(OPERATION_MAP.read_text(encoding="utf-8"))

    def find(node):
        if isinstance(node, dict):
            if "aws_iam_role_policy" in node and isinstance(node["aws_iam_role_policy"], dict):
                return node["aws_iam_role_policy"]
            for value in node.values():
                hit = find(value)
                if hit:
                    return hit
        return None

    entry = find(doc) or {}
    return {"entry": entry,
            "create_actions": entry.get("create", []),
            "requires_put_role_policy": ACTION in entry.get("create", [])}


def classify() -> dict:
    resources = declared_inline_policy_resources()
    provider = provider_requires_action()

    if not resources:
        return {
            "action": ACTION, "classification": OBSOLETE,
            "why": "the repository declares no aws_iam_role_policy resource, so no apply "
                   "path invokes the operation",
            "evidence": {"declared_resources": resources, "provider": provider},
        }

    if not provider["requires_put_role_policy"]:
        return {
            "action": ACTION, "classification": UNRESOLVED,
            "why": (f"the repository declares {len(resources)} aws_iam_role_policy resources "
                    "but the provider operation map does not map their creation to "
                    f"{ACTION}. Two primary sources disagree; classification cannot be "
                    "resolved from evidence and the gate must fail rather than guess."),
            "evidence": {"declared_resources": resources, "provider": provider},
        }

    # Both primary sources agree: the resources exist and creating them calls the action.
    return {
        "action": ACTION,
        "classification": REQUIRED_TEMPORARILY,
        "why": (f"{len(resources)} aws_iam_role_policy resources are declared in the "
                "composition, and the provider operation map maps their creation to "
                f"{ACTION}. The operation is invoked by an ordinary apply, so it is required "
                "— it was never obsolete and nothing replaced it."),
        "owner": "STAGE_A_TEMP_OPERATOR",
        "owner_rationale": (
            "OpenTofu issues the call during the Stage-A apply, so the Stage-A operator is "
            "the principal that makes it. The Gate 4N-I9 reason for stripping role authoring "
            "from Stage-A does NOT extend to this action: that reason was that CreateRole "
            "accepts an AssumeRolePolicyDocument which AWS has no condition key over, so an "
            "approved role NAME could still be created with attacker-chosen trust that "
            "outlives the window. PutRolePolicy accepts no trust document and creates no "
            "principal; it writes an inline policy to a role that already exists."),
        "containment": {
            "resource_scope": "exact role ARNs, not a prefix wildcard",
            "policy_name_scope": (
                "NOT ACHIEVABLE, stated rather than claimed. The resource for "
                "iam:PutRolePolicy is the ROLE ARN; the inline-policy NAME is not an ARN "
                "component and AWS publishes no condition key for it. So this grant can "
                "write ANY inline policy name onto one of the enumerated roles. The "
                "containment that does hold is the boundary: whatever name is used, the "
                "resulting permissions are still identity AND boundary."),
            "condition": "iam:PermissionsBoundary must equal the reviewed boundary ARN",
            "expiry": "DateLessThan on the grant, as with every other temporary Allow",
            "why_bounded": (
                "the target role's effective permissions are identity AND boundary, so an "
                "inline policy written by this grant cannot exceed the reviewed ceiling. "
                "Gate 4N-I16 Defect 1 makes that composition guaranteed rather than hoped "
                "for: a Stage-A bootstrap is now rejected at plan time unless the boundary "
                "state is BOUNDARY_ENFORCED, so there is no configuration in which this "
                "grant applies to an unbounded role."),
        },
        "read_back": "iam:GetRolePolicy on the exact role and policy name",
        "retirement": "the grant expires with the Stage-A window; it is not in permanent W0",
        "evidence": {"declared_resources": resources, "provider": provider},
    }


def check_policies(result: dict) -> dict:
    """The classification must be SATISFIED by the shipped policy set, not merely stated."""
    import gen_operator_policies as gen
    import gen_role_bootstrap_policy as rb

    expiry = __import__("expiry_authorization").ACTIVE_EXPIRY_UTC
    temp = gen.bootstrap_temp_policy(expiry)
    perm = gen.permanent_w0_policy()
    boot = rb.role_bootstrap_policy(expiry)

    role_arn = identity.iam_role_arn(identity.REVISION_READER_ROLE_NAMES[0])
    findings = []

    temp_decision = iam_eval.decide(temp, ACTION, role_arn, EXPIRY_PROBE)
    perm_decision = iam_eval.decide(perm, ACTION, role_arn,
                                    {"aws:RequestedRegion": identity.REGION})
    boot_decision = iam_eval.decide(boot, ACTION, role_arn, EXPIRY_PROBE)

    if result["classification"] == REQUIRED_TEMPORARILY:
        if temp_decision.decision is not iam_eval.Decision.EXPLICIT_ALLOW:
            findings.append(
                f"classified {REQUIRED_TEMPORARILY} to {result['owner']} but the Stage-A "
                f"policy returns {temp_decision.decision.name} for {ACTION} on {role_arn}")
        if perm_decision.decision is iam_eval.Decision.EXPLICIT_ALLOW:
            findings.append(
                "the PERMANENT operator holds a temporary-only action; it must expire")
    elif result["classification"] == UNRESOLVED:
        findings.append("classification is UNKNOWN — the gate fails rather than excluding "
                        "the action to make the closure green")

    return {
        "stage_a": temp_decision.decision.name,
        "stage_a_supporting_sids": list(temp_decision.matching_allow_sids),
        "permanent_w0": perm_decision.decision.name,
        "role_bootstrap": boot_decision.decision.name,
        "findings": findings,
    }


def run() -> dict:
    result = classify()
    satisfaction = check_policies(result)
    return {**result, "policy_satisfaction": satisfaction,
            "clean": not satisfaction["findings"]}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = run()
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=True))
    else:
        print(f"  {ACTION}: {result['classification']}")
        print(f"    declared aws_iam_role_policy resources: "
              f"{len(result['evidence']['declared_resources'])}")
        sat = result["policy_satisfaction"]
        print(f"    stage_a={sat['stage_a']}  permanent_w0={sat['permanent_w0']}  "
              f"role_bootstrap={sat['role_bootstrap']}")
        for finding in sat["findings"]:
            print(f"    {finding}", file=sys.stderr)
        print("PUTROLEPOLICY CLASSIFICATION: clean" if result["clean"]
              else "PUTROLEPOLICY CLASSIFICATION: findings")
    return 0 if result["clean"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
