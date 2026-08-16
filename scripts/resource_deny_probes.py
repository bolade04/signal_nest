#!/usr/bin/env python3
"""Resource-specific Deny probes and statement-level mutation (Gate 4N-I8, Defect 10).

THE DEFECT, reproduced and confirmed in Gate 4N-I7. Deleting
`DenyAuditLogObjectDestruction` — the ONLY protection for delivered CloudTrail log objects —
left `allow_model` reporting boundary 45/45 clean and 399/399 tests passing. The ceiling
proof used roughly ONE probe ARN per service (all five forbidden s3 capabilities were probed
at the state object), so a Deny scoped to a DIFFERENT resource could disappear without
moving any probe. A resource-scoped control is only proven at the resource it protects.

Two mechanisms, because either alone leaves a hole:

  PROBES              every protected resource is named explicitly, with an inside-scope
                      probe (must be EXPLICIT_DENY) and an outside-scope probe (must not be,
                      or the Deny is over-broad and would break a legitimate function).
  STATEMENT MUTATION  every Deny statement in every generated policy is deleted in turn, and
                      at least one probe must notice. A statement no probe defends is
                      reported as UNDEFENDED — it is not silently accepted.

Usage:
    python3 scripts/resource_deny_probes.py [--json]
Exit: 0 iff every probe holds and every Deny statement is defended.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import gen_bootstrap_operator_policy as boot  # noqa: E402
import gen_boundary_policy as gb  # noqa: E402
import gen_operator_policies as gen  # noqa: E402
import gen_role_bootstrap_policy as rb  # noqa: E402
import iam_eval  # noqa: E402
import signalnest_identity as identity  # noqa: E402
from iam_eval import Decision  # noqa: E402

# GATE 4N-I19, ADV-A: the ONE authoritative reviewed window. Gate 4N-I17's architect lane
# found ~20 independent expiry literals with nothing asserting they agreed; they now all
# resolve to the single authorized pair, so a restamp cannot leave stragglers behind.
import expiry_authorization as _ea  # noqa: E402

EXPIRY = _ea.ACTIVE_EXPIRY_UTC
IN_WINDOW = {"aws:CurrentTime": "2026-07-31T12:00:00Z"}
BOUNDARY_CTX = {**IN_WINDOW, "iam:PermissionsBoundary": identity.BOUNDARY_POLICY_ARN}

APP_BUCKET = identity.s3_bucket_arn(identity.APP_BUCKET_NAME)
OTHER_TABLE = f"arn:aws:dynamodb:{gen.REGION}:{gen.ACCOUNT}:table/some-other-table"
OTHER_ROLE = f"arn:aws:iam::{gen.ACCOUNT}:role/unrelated-role"
OTHER_POLICY = f"arn:aws:iam::{gen.ACCOUNT}:policy/unrelated-policy"


def _policies() -> dict[str, dict]:
    return {
        "boundary": gb.boundary_policy(),
        "permanent_w0": gen.permanent_w0_policy(),
        "temporary_operator": gen.bootstrap_temp_policy(EXPIRY),
        "bootstrap_operator": boot.bootstrap_operator_policy(EXPIRY),
        "role_bootstrap_operator": rb.role_bootstrap_policy(EXPIRY),
    }


def _ctx(policy_name: str) -> dict:
    return BOUNDARY_CTX if policy_name in (
        "temporary_operator", "bootstrap_operator", "role_bootstrap_operator") else {}


# Every resource-scoped protection, named at the resource it actually protects.
# `outside` is the control: a Deny that fires everywhere is over-broad and would break a
# legitimate function, which is the failure mode the boundary's PassRole fence exists for.
PROBES = [
    # --- the one that was deleted with a green suite --------------------------------
    ("boundary", "s3:DeleteObjectVersion", gb.AUDIT_OBJECTS.replace("/*", "/AWSLogs/o.json.gz"),
     APP_BUCKET + "/x", "delivered CloudTrail log objects"),
    ("boundary", "s3:DeleteObject", gb.AUDIT_OBJECTS.replace("/*", "/AWSLogs/o.json.gz"),
     APP_BUCKET + "/x", "delivered CloudTrail log objects"),
    ("boundary", "s3:PutObject", gb.AUDIT_OBJECTS.replace("/*", "/AWSLogs/o.json.gz"),
     APP_BUCKET + "/x", "planting an object in the audit bucket"),
    # --- Terraform state -------------------------------------------------------------
    ("boundary", "s3:GetObject", f"{gb.STATE_BUCKET}/signalnest-staging/root.tfstate",
     APP_BUCKET + "/x", "Terraform state object"),
    ("boundary", "s3:PutObject", f"{gb.STATE_BUCKET}/signalnest-staging/root.tfstate",
     APP_BUCKET + "/x", "Terraform state object"),
    ("boundary", "s3:DeleteBucket", gb.STATE_BUCKET, APP_BUCKET, "state bucket"),
    ("boundary", "s3:PutBucketPolicy", gb.AUDIT_BUCKET, APP_BUCKET, "audit bucket policy"),
    # --- lock table ------------------------------------------------------------------
    ("boundary", "dynamodb:PutItem", gb.LOCK_TABLE, OTHER_TABLE, "Terraform lock table"),
    ("boundary", "dynamodb:GetItem", gb.LOCK_TABLE, OTHER_TABLE, "Terraform lock table"),
    # --- CMKs ------------------------------------------------------------------------
    ("boundary", "kms:Decrypt", gb.STATE_CMK, gb.SECRETS_CMK, "state CMK"),
    ("boundary", "kms:CreateGrant", gb.SECRETS_CMK, None, "secrets CMK"),
    # --- secrets ---------------------------------------------------------------------
    ("boundary", "secretsmanager:GetSecretValue",
     f"arn:aws:secretsmanager:{gen.REGION}:{gen.ACCOUNT}:secret:other/thing",
     f"arn:aws:secretsmanager:{gen.REGION}:{gen.ACCOUNT}:secret:signalnest-staging/DATABASE_URL-AbCdEf",
     "secrets outside the approved prefix"),
    # --- protected IAM roles ----------------------------------------------------------
    ("boundary", "iam:PassRole", f"arn:aws:iam::{gen.ACCOUNT}:role/signalnest-staging-ecs-execution",
     identity.READER_EXECUTION_ROLE_ARN, "PassRole on any role but the reader execution role"),
    ("boundary", "iam:CreateRole", OTHER_ROLE, None, "role creation"),
    # --- ECS task definitions ----------------------------------------------------------
    ("boundary", "ecs:RunTask",
     f"arn:aws:ecs:{gen.REGION}:{gen.ACCOUNT}:task-definition/signalnest-staging-api:1",
     f"arn:aws:ecs:{gen.REGION}:{gen.ACCOUNT}:task-definition/signalnest-staging-revision-reader:1",
     "RunTask on anything but the reader revision"),
    # --- permanent W0 fences (INFRA-9 B-3: the apply identity's carved capabilities) ----
    # Each fence is probed at a resource it must deny (inside) and at the carved resource it
    # must NOT deny (outside), so a fence that widens or a fence that is deleted both move.
    ("permanent_w0", "s3:GetObject", APP_BUCKET + "/x", gen.ARN["state_object"],
     "state reads outside the exact state object"),
    ("permanent_w0", "s3:PutObject", f"{gb.STATE_BUCKET}/other/object",
     gen.ARN["state_object"], "state writes outside the exact state object"),
    ("permanent_w0", "dynamodb:GetItem", OTHER_TABLE, gen.ARN["lock"],
     "lock reads outside the lock table"),
    ("permanent_w0", "dynamodb:PutItem", OTHER_TABLE, gen.ARN["lock"],
     "lock items outside the lock table"),
    ("permanent_w0", "dynamodb:DeleteItem", OTHER_TABLE, gen.ARN["lock"],
     "lock release outside the lock table"),
    ("permanent_w0", "kms:Decrypt", gen.ARN["cmk_secrets"], gen.ARN["cmk_state"],
     "decrypt outside the state CMK"),
    ("permanent_w0", "kms:GenerateDataKey", gen.ARN["cmk_secrets"], None,
     "data-key generation outside the state CMK (outside probe omitted: the in-scope Allow "
     "is ViaService-conditioned, so a contextless outside probe cannot distinguish the fence "
     "from the condition; the conditioned in-scope allow is proven by the pytest suite)"),
    ("permanent_w0", "ecs:RegisterTaskDefinition",
     f"arn:aws:ecs:{gen.REGION}:{gen.ACCOUNT}:task-definition/{gen.PREFIX}-evil:*",
     gen.TASK_DEFINITION_FAMILY_ARNS[0],
     "task-definition registration outside the four composition families"),
    ("permanent_w0", "ecs:TagResource",
     f"arn:aws:ecs:{gen.REGION}:{gen.ACCOUNT}:task-definition/{gen.PREFIX}-evil:*",
     gen.TASK_DEFINITION_FAMILY_ARNS[0],
     "task-definition tag-on-create outside the four composition families"),
    # --- temporary operator fences -----------------------------------------------------
    ("temporary_operator", "s3:PutObject", f"{gb.STATE_BUCKET}/other/object",
     gen.ARN["state_object"], "state writes outside the exact state object"),
    ("temporary_operator", "dynamodb:PutItem", OTHER_TABLE, gen.ARN["lock"],
     "lock items outside the lock table"),
    ("temporary_operator", "kms:Decrypt", gen.ARN["cmk_secrets"], gen.ARN["cmk_state"],
     "decrypt outside the state CMK"),
    # GATE 4N-I9: Stage-A no longer authors roles ANYWHERE, so there is no outside-scope
    # control here — the deny is deliberately total. `outside=None` records that.
    ("temporary_operator", "iam:CreateRole", gen.READER_ROLE_ARNS[0], None,
     "role creation by Stage-A, now denied even on the reader roles"),
    ("temporary_operator", "iam:UpdateAssumeRolePolicy", gen.READER_ROLE_ARNS[0], None,
     "trust rewriting by Stage-A"),
    # iam:TagRole is not an escalation on its own, so the must-not contract does not carry
    # it and the ceiling proof cannot see it. It is still role-authoring surface that
    # belongs to the role bootstrap operator, and without this probe the statement denying
    # it could be deleted with nothing noticing — which is exactly what the UNDEFENDED
    # classification reported.
    ("temporary_operator", "iam:TagRole", gen.READER_ROLE_ARNS[0], None,
     "role tagging by Stage-A, which owns no role-authoring surface"),
    ("temporary_operator", "iam:DeleteRole", gen.READER_ROLE_ARNS[0], None,
     "role deletion by Stage-A"),
    # --- role bootstrap operator fences -------------------------------------------------
    ("role_bootstrap_operator", "iam:CreateRole", OTHER_ROLE, rb.TARGET_ROLE_ARNS[0],
     "role creation outside the three reader roles"),
    ("role_bootstrap_operator", "iam:DeleteRole", OTHER_ROLE, rb.TARGET_ROLE_ARNS[0],
     "role deletion outside the three reader roles"),
    ("role_bootstrap_operator", "iam:UpdateAssumeRolePolicy", rb.TARGET_ROLE_ARNS[0], None,
     "trust REWRITING after a role has passed read-back"),
    ("role_bootstrap_operator", "iam:PutRolePolicy", rb.TARGET_ROLE_ARNS[0], None,
     "inline policy authoring by the role bootstrap operator"),
    # --- bootstrap operator fences ------------------------------------------------------
    ("bootstrap_operator", "iam:PutRolePermissionsBoundary", OTHER_ROLE,
     identity.ALL_ROLE_ARNS[0], "boundary attachment outside the eight roles"),
    ("bootstrap_operator", "iam:CreatePolicy", OTHER_POLICY, identity.BOUNDARY_POLICY_ARN,
     "policy creation outside the boundary"),
]


def run_probes(policies: dict[str, dict] | None = None) -> list[dict]:
    policies = policies or _policies()
    rows = []
    for policy_name, action, inside, outside, what in PROBES:
        policy = policies[policy_name]
        ctx = _ctx(policy_name)
        inside_decision = iam_eval.decide(policy, action, inside, ctx).decision
        row = {"policy": policy_name, "action": action, "protects": what,
               "inside": inside, "inside_decision": inside_decision.name,
               "inside_ok": inside_decision is Decision.EXPLICIT_DENY}
        if outside is not None:
            outside_decision = iam_eval.decide(policy, action, outside, ctx).decision
            row["outside"] = outside
            row["outside_decision"] = outside_decision.name
            # Over-broad control: if the Deny also fires outside its scope, a legitimate
            # function is being removed. That is how a "safer" boundary breaks production.
            row["outside_ok"] = outside_decision is not Decision.EXPLICIT_DENY
        else:
            row["outside_ok"] = True
            row["outside"] = None
        row["ok"] = row["inside_ok"] and row["outside_ok"]
        rows.append(row)
    return rows


def statement_mutation() -> list[dict]:
    """Delete each Deny statement in turn; SOMETHING must notice.

    Two defenders, because they cover different statement shapes and either alone leaves a
    hole:

      resource probes      catch a statement scoped to a specific resource. The Gate 4N-I7
                           ceiling proof could not: it used one probe ARN per service, so
                           deleting DenyAuditLogObjectDestruction moved nothing.
      the ceiling proof    catches a FLAT deny (Resource "*"), because removing it lets the
                           corresponding must-not capability through the widening injection.

    A statement neither defender notices is UNDEFENDED and fails. It is reported by name
    rather than absorbed, because "no test noticed" is the finding, not the baseline.
    """
    import allow_model

    results = []
    for policy_name, policy in _policies().items():
        deny_sids = [s.get("Sid") or f"<statement {i}>"
                     for i, s in enumerate(policy["Statement"]) if s["Effect"] == "Deny"]
        build, ceiling_ctx, probe_fn = allow_model.TARGETS[policy_name]
        for sid in deny_sids:
            mutated = dict(policy)
            mutated["Statement"] = [
                s for i, s in enumerate(policy["Statement"])
                if (s.get("Sid") or f"<statement {i}>") != sid]

            noticed = [r for r in run_probes({**_policies(), policy_name: mutated})
                       if not r["ok"]]
            escapes = allow_model.prove_ceiling(policy_name, mutated, ceiling_ctx,
                                                probe_fn)["escapes"]
            defenders = []
            if noticed:
                defenders.append(f"resource probe: {noticed[0]['action']} on "
                                 f"{noticed[0]['protects']}")
            if escapes:
                defenders.append(f"ceiling proof: {len(escapes)} escape(s), first "
                                 f"{escapes[0]['action']}")

            # REDUNDANT vs UNDEFENDED. If deleting the statement leaves every action it
            # named STILL explicitly denied, the statement was defence in depth and nothing
            # was lost — no test should be expected to notice. If any action becomes
            # reachable and neither defender saw it, that is a real hole.
            original = next(st for i, st in enumerate(policy["Statement"])
                            if (st.get("Sid") or f"<statement {i}>") == sid)
            raw = original.get("Action", [])
            actions = raw if isinstance(raw, list) else [raw]
            reachable = []
            for action in actions:
                if "*" in action:
                    continue  # a service wildcard has no single meaningful probe resource
                resource = probe_fn(action)
                if iam_eval.decide(mutated, action, resource,
                                   _ctx(policy_name)).decision is not Decision.EXPLICIT_DENY:
                    reachable.append(action)

            results.append({
                "policy": policy_name, "deleted_statement": sid,
                "probes_that_noticed": len(noticed),
                "ceiling_escapes": len(escapes),
                "actions_made_reachable": reachable,
                "classification": ("DEFENDED" if defenders else
                                   "REDUNDANT" if not reachable else "UNDEFENDED"),
                "defended": bool(defenders) or not reachable,
                "defended_by": defenders or (
                    ["redundant: every action it named is still explicitly denied by "
                     "another statement"] if not reachable else []),
            })
    return results


def run() -> dict:
    probes = run_probes()
    mutations = statement_mutation()
    failing = [p for p in probes if not p["ok"]]
    undefended = [m for m in mutations if m["classification"] == "UNDEFENDED"]
    redundant = [m for m in mutations if m["classification"] == "REDUNDANT"]
    return {
        "probe_count": len(probes),
        "failing_probes": failing,
        "deny_statements": len(mutations),
        "undefended_statements": undefended,
        "redundant_statements": [
            {"policy": r["policy"], "statement": r["deleted_statement"],
             "note": "defence in depth: deleting it leaves every action still explicitly "
                     "denied elsewhere. Reported, not hidden."} for r in redundant],
        "clean": not failing and not undefended,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = run()
    if args.json:
        print(json.dumps({**result, "probes": run_probes(),
                          "mutations": statement_mutation()}, indent=2))
    else:
        print(f"  probes {result['probe_count']}  "
              f"Deny statements {result['deny_statements']}  "
              f"undefended {len(result['undefended_statements'])}  "
              f"redundant {len(result['redundant_statements'])}")
        for row in result["failing_probes"]:
            print(f"    PROBE FAILED {row['policy']} {row['action']} ({row['protects']}): "
                  f"inside={row['inside_decision']} outside={row.get('outside_decision')}",
                  file=sys.stderr)
        for row in result["undefended_statements"]:
            print(f"    UNDEFENDED {row['policy']}/{row['deleted_statement']} — deleting it "
                  f"moved neither a resource probe nor the ceiling proof", file=sys.stderr)
        print("RESOURCE DENY PROBES: clean" if result["clean"]
              else "RESOURCE DENY PROBES: findings")
    return 0 if result["clean"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
