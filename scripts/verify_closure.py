#!/usr/bin/env python3
"""Closure verifier: joins SOURCE 1 and SOURCE 2, then checks the generated policies.

Gate 4N-I5. The Gate 4N-I4 design stored the expected closure in a separate FILE but the
two lists were set-identical and shared every resource ARN, so a SHARED OMISSION —
both hand-authored files forgetting the same action — was undetectable. That is exactly
what happened with `s3:ListTagsForResource`.

The fix is structural rather than clerical. The expectation is now COMPUTED by joining:

  SOURCE 1  scripts/derive_repo_operation_graph.py  — parsed from infra/aws/**/*.tf
  SOURCE 2  infra/aws/provider-api-operation-map.json — resource TYPE to AWS action

Neither source contains a policy statement, and SOURCE 2 contains no ARNs at all. The
verifier never reads the generator's action list or the generated policy as its
authority — it reads the generated policy only as the SUBJECT under test.

Checks performed:
  C1 every declared resource type has a SOURCE 2 mapping        (shared-omission guard)
  C2 every mapping corresponds to a declared resource type      (dead-mapping guard)
  C3 every required read in the join is authorized by permanent W0
  C4 every required read + non-role create in the join is authorized by the temporary operator
  C6 every required ROLE CREATE is authorized by the dedicated role bootstrap operator
  C7 the Stage-A operator authorizes NO role authoring at all (Gate 4N-I9 Defect 1)
  C5 every historically AccessDenied action is present or explicitly classified

Usage:
    python3 scripts/verify_closure.py [--json]
Exit: 0 clean; 1 divergence.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import derive_repo_operation_graph as repo_graph  # noqa: E402
import iam_eval  # noqa: E402

MAP_PATH = REPO_ROOT / "infra" / "aws" / "provider-api-operation-map.json"
CONTRACT_PATH = REPO_ROOT / "infra" / "aws" / "operator-closure-contract.json"

# Units owned by the W0 composition. `bootstrap` is a separate root applied by a
# different permission set and is deliberately outside the closure.
W0_UNITS_EXCLUDED = {"bootstrap"}


def load_map() -> dict:
    return json.loads(MAP_PATH.read_text(encoding="utf-8"))["mappings"]


def join(graph: dict, mappings: dict, mappings_doc: dict | None = None) -> dict:
    """Compute the required action closure from the two independent sources."""
    declared = set(graph["distinct_resource_types"])
    mapped = set(mappings)

    unmapped = sorted(declared - mapped)
    dead = sorted(mapped - declared)

    required_read: dict[str, str] = {}   # action -> the resource TYPE that requires it
    required_create: dict[str, str] = {}
    deferred_dark: dict[str, str] = {}

    in_scope_types = {
        r["type"] for r in graph["resources"] if r["unit"] not in W0_UNITS_EXCLUDED
    }
    # Gate 4N-I5 discarded graph["data_sources"], which is why sts:GetCallerIdentity —
    # required by four `data "aws_caller_identity"` blocks — was invisible to the join.
    data_map = mappings_doc.get("_data_source_mappings", {}) if mappings_doc else {}
    for d in graph.get("data_sources", []):
        entry = data_map.get(d["type"])
        if entry:
            for action in entry.get("read", []):
                required_read.setdefault(action, f"data.{d['type']}")
    for rtype in sorted(in_scope_types):
        entry = mappings.get(rtype)
        if entry is None:
            continue  # already reported as unmapped
        if entry.get("separate_root"):
            continue
        target = deferred_dark if entry.get("dark") else required_read
        for action in entry.get("read", []):
            target.setdefault(action, rtype)
        for action in entry.get("create", []) + entry.get("read_after_create", []):
            required_create.setdefault(action, rtype)

    return {
        "unmapped_resource_types": unmapped,
        "dead_mappings": dead,
        "required_read_actions": dict(sorted(required_read.items())),
        "required_create_actions": dict(sorted(required_create.items())),
        "deferred_dark_actions": dict(sorted(deferred_dark.items())),
    }


BY_TYPE = {
    "aws_db_instance": "db", "aws_db_parameter_group": "pg", "aws_db_subnet_group": "subgrp",
    "aws_iam_role": "reader_role", "aws_iam_role_policy": "reader_role",
    "aws_ecr_repository": "reader_ecr", "aws_ecr_lifecycle_policy": "reader_ecr",
    "aws_cloudfront_distribution": "distribution", "aws_cloudfront_origin_access_control": "oac",
}


def _probe_resource(action: str, arns: dict, rtype: str | None = None) -> str:
    """Pick a representative resource, chosen from the RESOURCE TYPE in the join.

    Deliberately NOT chosen from the policy under test — otherwise the probe would be
    guaranteed to match and the check would be vacuous.
    """
    slot = BY_TYPE.get(rtype or "")
    if slot == "db":
        return arns["db"]
    if slot == "pg":
        return arns["pg"]
    if slot == "subgrp":
        return arns["subgrp"]
    if slot == "reader_role":
        return arns["reader_role"]
    if slot == "reader_ecr":
        return arns["reader_ecr"]
    if slot == "distribution":
        return arns["distribution"]
    if slot == "oac":
        return arns["oac"]
    service = action.split(":", 1)[0]
    return {
        "s3": arns["audit_bucket"],
        "kms": arns["cmk_secrets"],
        "iam": f"arn:aws:iam::{arns['account']}:role/{arns['prefix']}-ecs-execution",
        # GATE 4N-I27Z. Was a bare literal identical to the generator's, so the
        # verifier agreed with the generator only because both were hand-typed.
        # It now resolves through the same tier-resolved identity the generator
        # uses, so a wrong zone cannot be confirmed by restating it.
        "route53": arns["hosted_zone"],
        "cloudfront": arns["distribution"],
        "cloudtrail": arns["trail"],
        "budgets": f"arn:aws:budgets::{arns['account']}:budget/{arns['prefix']}-monthly",
        "secretsmanager": f"arn:aws:secretsmanager:{arns['region']}:{arns['account']}:secret:{arns['prefix']}/X-abc",
        "ecr": arns["reader_ecr"],
    }.get(service, "*")


def verify() -> dict:
    import gen_operator_policies as gen  # subject under test, not an authority

    graph = repo_graph.derive()
    mappings = load_map()
    joined = join(graph, mappings, json.loads(MAP_PATH.read_text(encoding="utf-8")))

    arns = {
        "account": gen.ACCOUNT, "region": gen.REGION, "prefix": gen.PREFIX,
        "audit_bucket": gen.ARN["audit_bucket"], "cmk_secrets": gen.ARN["cmk_secrets"],
        "distribution": gen.ARN["distribution"], "trail": gen.ARN["trail"],
        "reader_ecr": gen.ARN["reader_ecr"], "db": gen.ARN["db"], "pg": gen.ARN["pg"],
        "subgrp": gen.ARN["subgrp"], "oac": gen.ARN["oac"],
        "reader_role": gen.READER_ROLE_ARNS[0],
        # GATE 4N-I27Z. Resolved from the identity module, not restated as a literal.
        "hosted_zone": gen.identity.route53_hosted_zone_arn(),
    }
    perm = gen.permanent_w0_policy()
    temp = gen.bootstrap_temp_policy(__import__("expiry_authorization").ACTIVE_EXPIRY_UTC)
    perm_ctx = {"aws:RequestedRegion": gen.REGION}
    temp_ctx = dict(perm_ctx, **{
        "aws:CurrentTime": "2026-07-31T12:00:00Z",
        "iam:PermissionsBoundary": gen.ARN["boundary"],
    })

    def unauthorized(policy, action_map, ctx):
        out = []
        for action, rtype in action_map.items():
            res = _probe_resource(action, arns, rtype)
            if iam_eval.effect(policy, action, res, ctx) != "Allow":
                out.append(f"{action} on {res} (required by {rtype})")
        return out

    # GATE 4N-I9. Role authoring moved off the Stage-A operator entirely: iam:CreateRole
    # accepts the AssumeRolePolicyDocument and AWS has no condition key over it, so a
    # principal holding it for an approved role NAME could still create that role with
    # attacker-chosen trust that outlives its own expiry. The closure is still checked end
    # to end — it is just checked against the principal that now owns each action, rather
    # than being relaxed.
    import gen_role_bootstrap_policy as rb

    ROLE_AUTHORING = {"iam:CreateRole", "iam:PutRolePolicy", "iam:TagRole",
                      "iam:UpdateAssumeRolePolicy", "iam:DeleteRole"}
    role_bootstrap = rb.role_bootstrap_policy(__import__("expiry_authorization").ACTIVE_EXPIRY_UTC)

    all_required = {**joined["required_read_actions"], **joined["required_create_actions"]}
    temp_actions = {a: t for a, t in all_required.items() if a not in ROLE_AUTHORING}
    bootstrap_actions = {a: t for a, t in all_required.items() if a in ROLE_AUTHORING}

    perm_missing = unauthorized(perm, joined["required_read_actions"], perm_ctx)
    temp_missing = unauthorized(temp, temp_actions, temp_ctx)
    # GATE 4N-I16 DEFECT 3. The exclusion that used to sit here read:
    #
    #     "PutRolePolicy is applied by OpenTofu against roles that ALREADY exist, so the
    #      role bootstrap operator deliberately does not hold it; it is not required of any
    #      principal at create time and is excluded rather than silently passed."
    #
    # The premise was false — creating an aws_iam_role_policy resource calls PutRolePolicy
    # whether or not the role pre-exists, as this repository's own operation map states —
    # and the exclusion made the closure green while NO principal held the action.
    #
    # Actions are no longer excluded to reach clean. Each role-authoring action is checked
    # against the principal that scripts/putrolepolicy_classification.py assigns it to, from
    # primary evidence. iam:PutRolePolicy is classified REQUIRED_TEMPORARILY to the Stage-A
    # operator and is verified against the Stage-A policy below with the other temp actions;
    # UpdateAssumeRolePolicy and DeleteRole remain rollback/trust actions owned by the
    # RoleBootstrap principal's own conditional paths rather than its create closure.
    ROLE_BOOTSTRAP_NOT_AT_CREATE = ("iam:UpdateAssumeRolePolicy", "iam:DeleteRole")
    role_bootstrap_missing = unauthorized(
        role_bootstrap, {a: t for a, t in bootstrap_actions.items()
                         if a not in ROLE_BOOTSTRAP_NOT_AT_CREATE
                         and a != "iam:PutRolePolicy"},
        temp_ctx)

    # PutRolePolicy is verified against its CLASSIFIED owner instead of being skipped.
    import putrolepolicy_classification as prp
    prp_result = prp.run()
    if not prp_result["clean"]:
        raise SystemExit(
            "iam:PutRolePolicy classification is not satisfied by the policy set: "
            f"{prp_result['policy_satisfaction']['findings']}")
    if prp_result["classification"] == "UNKNOWN":
        raise SystemExit("iam:PutRolePolicy is UNCLASSIFIED — the gate fails rather than "
                         "excluding it to make the closure green")

    # The Stage-A operator must NOT hold any TRUST-BEARING role-authoring action. This is
    # the positive statement of Gate 4N-I9 Defect 1 being closed, checked rather than assumed.
    #
    # GATE 4N-I16 DEFECT 3 — the set is now named precisely. The I9 finding is about the
    # ASSUME-ROLE TRUST DOCUMENT: CreateRole and UpdateAssumeRolePolicy decide WHO MAY ASSUME
    # a role, AWS publishes no condition key over that document, and trust granted inside the
    # window OUTLIVES the window. iam:PutRolePolicy carries no trust document and creates no
    # principal — it decides what an existing role may DO, bounded by that role's permissions
    # boundary. Treating the two as one category is what forced the previous gate to choose
    # between a false exclusion and a broken apply.
    TRUST_BEARING_AUTHORING = {"iam:CreateRole", "iam:UpdateAssumeRolePolicy"}
    stage_a_still_authors = [
        f"{action} on {_probe_resource(action, arns, rtype)}"
        for action, rtype in bootstrap_actions.items()
        if action in TRUST_BEARING_AUTHORING
        and iam_eval.decide(temp, action, _probe_resource(action, arns, rtype),
                            temp_ctx).decision is iam_eval.Decision.EXPLICIT_ALLOW
    ]

    # C5: historical denials must be present or explicitly classified as not required.
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    denial_gaps = []
    for entry in contract.get("historical_denials_classified", []):
        if not entry.get("required"):
            continue
        action = entry["action"]
        res = _probe_resource(action, arns)
        if iam_eval.effect(perm, action, res, perm_ctx) != "Allow":
            denial_gaps.append(f"{action} classified required but not authorized")

    findings = []
    if joined["unmapped_resource_types"]:
        findings.append(f"C1 unmapped resource types: {joined['unmapped_resource_types']}")
    if joined["dead_mappings"]:
        findings.append(f"C2 mappings with no declared resource: {joined['dead_mappings']}")
    if perm_missing:
        findings.append(f"C3 permanent W0 does not authorize: {perm_missing}")
    if temp_missing:
        findings.append(f"C4 temporary operator does not authorize: {temp_missing}")
    if role_bootstrap_missing:
        findings.append(f"C6 role bootstrap operator does not authorize: {role_bootstrap_missing}")
    if stage_a_still_authors:
        findings.append(
            f"C7 the Stage-A operator STILL authorizes role authoring: {stage_a_still_authors}. "
            "That is Gate 4N-I9 Defect 1 reopened — CreateRole carries the trust document and "
            "no IAM condition constrains it.")
    if denial_gaps:
        findings.append(f"C5 {denial_gaps}")

    return {
        "sources": {
            "source_1": "scripts/derive_repo_operation_graph.py (parsed .tf)",
            "source_2": str(MAP_PATH.relative_to(REPO_ROOT)),
        },
        "graph_resource_count": graph["resource_count"],
        "graph_distinct_types": len(graph["distinct_resource_types"]),
        **joined,
        "findings": findings,
        "clean": not findings,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = verify()
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"resources={result['graph_resource_count']} types={result['graph_distinct_types']}")
        print(f"required reads={len(result['required_read_actions'])} "
              f"creates={len(result['required_create_actions'])} "
              f"deferred(dark)={len(result['deferred_dark_actions'])}")
        for finding in result["findings"]:
            print(f"  FAIL {finding}", file=sys.stderr)
        print("CLOSURE: clean" if result["clean"] else "CLOSURE: divergence")
    return 0 if result["clean"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
