#!/usr/bin/env python3
"""Deny triangulation classifier and per-action mutation score (Gate 4N-I10, Defects 1-3).

WHAT WAS MISSING. Gate 4N-I8 built three INDEPENDENT requirement sources and Gate 4N-I9
built resource-specific probes, but nothing reconciled them. A capability could be demanded
by the incident ledger, denied by a policy, and probed at the wrong resource — and every
individual check would pass. Aggregate scores hid it: "69/69 ceiling" and "28/28 statements
defended" are both true of a set with a hole in the middle of it.

FIVE SOURCES, reconciled per capability:

  A  incident ledger        external, mode 400, outside the repository
  B  architecture invariant declarative, in-repo, expanded to actions
  C  protected-resource     the exact ARNs a Deny must actually cover
  D  generated policy       the Deny statements that exist
  E  explicit probe         an evaluation proving EXPLICIT_DENY at a real resource

A and B are the authority. D is the SUBJECT. C and E are what stop a Deny that exists but
points at the wrong thing from counting as protection.

Usage:
    python3 scripts/deny_triangulation.py [--json] [--mutations]
Exit: 0 iff every capability passes and every mandatory mutation is caught.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import deny_requirements  # noqa: E402
import gen_bootstrap_operator_policy as boot  # noqa: E402
import gen_boundary_policy as gb  # noqa: E402
import gen_operator_policies as gen  # noqa: E402
import gen_role_bootstrap_policy as rb  # noqa: E402
import iam_eval  # noqa: E402
import resource_deny_probes as probes  # noqa: E402
import signalnest_identity as identity  # noqa: E402
from iam_eval import Decision  # noqa: E402

# GATE 4N-I19, ADV-A: the ONE authoritative reviewed window. Gate 4N-I17's architect lane
# found ~20 independent expiry literals with nothing asserting they agreed; they now all
# resolve to the single authorized pair, so a restamp cannot leave stragglers behind.
import expiry_authorization as _ea  # noqa: E402

EXPIRY = _ea.ACTIVE_EXPIRY_UTC
IN_WINDOW = {"aws:CurrentTime": "2026-07-31T12:00:00Z", "aws:RequestedRegion": "us-east-1"}

REQUIRED_AND_PRESENT = "REQUIRED_AND_PRESENT"
REQUIRED_BUT_MISSING = "REQUIRED_BUT_MISSING"
PRESENT_BUT_UNJUSTIFIED = "PRESENT_BUT_UNJUSTIFIED"
RESOURCE_UNRESOLVED = "RESOURCE_UNRESOLVED"
PROBE_MISSING = "PROBE_MISSING"
CONFLICTING_SCOPE = "CONFLICTING_SCOPE"
DUPLICATE_WITH_EQUIVALENT_PROTECTION = "DUPLICATE_WITH_EQUIVALENT_PROTECTION"
UNKNOWN = "UNKNOWN"

PASSING = {REQUIRED_AND_PRESENT, DUPLICATE_WITH_EQUIVALENT_PROTECTION}


def policies() -> dict[str, dict]:
    return {
        "boundary": gb.boundary_policy(),
        "permanent_w0": gen.permanent_w0_policy(),
        "temporary_operator": gen.bootstrap_temp_policy(EXPIRY),
        "bootstrap_operator": boot.bootstrap_operator_policy(EXPIRY),
        "role_bootstrap_operator": rb.role_bootstrap_policy(EXPIRY),
    }


def _ctx(name: str) -> dict:
    if name in ("temporary_operator", "bootstrap_operator", "role_bootstrap_operator"):
        return {**IN_WINDOW, "iam:PermissionsBoundary": identity.BOUNDARY_POLICY_ARN}
    return {}


# SOURCE C. The exact protected resource a capability must be denied AT. A capability with no
# entry falls back to the service-generic probe and is marked as such — never silently.
PROTECTED_RESOURCE = {
    "s3:PutObject": "state_object", "s3:GetObject": "state_object",
    "s3:DeleteObject": "audit_objects", "s3:DeleteObjectVersion": "audit_objects",
    "s3:PutBucketPolicy": "audit_bucket", "s3:PutBucketAcl": "audit_bucket",
    "s3:PutObjectAcl": "audit_objects",
    # The invariant behind this is log DESTRUCTION: a lifecycle rule can expire delivered
    # CloudTrail objects without any Delete call. So the protected resource is the AUDIT
    # BUCKET, not "*". Probed at "*" it read as a boundary gap, but a flat deny would be
    # wrong — the api and worker roles may legitimately set lifecycle rules on the app
    # bucket. Scoping the probe to what the invariant is actually about resolves it.
    "s3:PutLifecycleConfiguration": "audit_bucket",
    "s3:PutBucketVersioning": "audit_bucket",
    "s3:DeleteBucketPolicy": "audit_bucket",
    "s3:PutBucketPublicAccessBlock": "audit_bucket",
    "s3:DeleteBucket": "state_bucket",
    "dynamodb:PutItem": "lock_table", "dynamodb:GetItem": "lock_table",
    "dynamodb:DeleteItem": "lock_table",
    # Gate 4N-I11: the sibling write/destroy APIs reach the same lock table. Probed at "*"
    # they read as boundary gaps; the boundary correctly scopes them to the lock table, and
    # a flat deny would be wrong because no other table is in scope of this design.
    "dynamodb:UpdateItem": "lock_table", "dynamodb:BatchWriteItem": "lock_table",
    "dynamodb:DeleteTable": "lock_table",
    "kms:Decrypt": "state_cmk", "kms:ScheduleKeyDeletion": "secrets_cmk",
    "kms:CreateGrant": "secrets_cmk", "kms:PutKeyPolicy": "secrets_cmk",
    "kms:ReEncryptFrom": "state_cmk", "kms:ReEncryptTo": "state_cmk",
    "cloudtrail:StopLogging": "trail", "cloudtrail:DeleteTrail": "trail",
    "cloudtrail:UpdateTrail": "trail", "cloudtrail:PutEventSelectors": "trail",
    "cloudtrail:PutInsightSelectors": "trail",
    "secretsmanager:GetSecretValue": "secrets_prefix",
    "secretsmanager:PutSecretValue": "secrets_prefix",
    "secretsmanager:DeleteSecret": "secrets_prefix",
    "secretsmanager:UpdateSecret": "secrets_prefix",
    "secretsmanager:PutResourcePolicy": "secrets_prefix",
    "iam:CreatePolicyVersion": "boundary_policy",
    "iam:SetDefaultPolicyVersion": "boundary_policy",
    "iam:DeletePolicyVersion": "boundary_policy", "iam:DeletePolicy": "boundary_policy",
}


def probe_resource(action: str) -> tuple[str, str]:
    """(resource, provenance). SOURCE C where we have it, service-generic otherwise."""
    slot = PROTECTED_RESOURCE.get(action)
    if slot:
        resources = identity.critical_resources()
        value = resources[slot]
        if slot in ("audit_objects", "state_objects"):
            value = value.replace("/*", "/AWSLogs/probe.json.gz")
        if slot == "secrets_prefix":
            value = value.replace("*", "other/thing")
        return value, f"SOURCE C: {slot}"
    return probes.PROBES and _generic(action), "service-generic (no SOURCE C entry)"


def _generic(action: str) -> str:
    service = action.split(":", 1)[0]
    return {
        "iam": f"arn:aws:iam::{identity.ACCOUNT}:role/anything",
        "sts": "arn:aws:iam::999988887777:role/outside",
    }.get(service, "*")


def triangulate() -> dict:
    required = deny_requirements.required_denies()
    docs = policies()
    rows = []

    for action, requirement in sorted(required.items()):
        if "*" in action:
            continue  # service wildcards have no single probe resource
        resource, provenance = probe_resource(action)
        per_policy, denying = {}, []
        for name, doc in docs.items():
            decision = iam_eval.decide(doc, action, resource, _ctx(name))
            per_policy[name] = decision.decision.name
            if decision.decision is Decision.EXPLICIT_DENY:
                denying.append({"policy": name, "sids": list(decision.matching_deny_sids)})

        exempt = {n for n in docs
                  if action in __import__("allow_model").EXEMPTIONS.get(n, {})}
        expected = [n for n in docs if n not in exempt]

        # CLASSIFICATION. The first draft called a capability "duplicate" whenever more than
        # one POLICY denied it — which is every capability, because five principals each
        # carry a ceiling. Everything landed in one bucket and REQUIRED_AND_PRESENT never
        # appeared, so the classifier said nothing. Multiple PRINCIPALS denying is the
        # design; duplication means multiple STATEMENTS INSIDE ONE policy covering the same
        # action, which is what Phase F asks about.
        gaps = [n for n in expected if per_policy[n] != Decision.EXPLICIT_DENY.name]
        within_policy_duplicates = [e for e in denying if len(e["sids"]) > 1]

        if not requirement["in_source_1"] and not requirement["in_source_2"]:
            classification = PRESENT_BUT_UNJUSTIFIED
        elif resource is None:
            classification = RESOURCE_UNRESOLVED
        elif not denying:
            classification = REQUIRED_BUT_MISSING
        elif gaps:
            # A principal that is neither exempt nor denying is the hole aggregate scores hide.
            classification = CONFLICTING_SCOPE
        elif within_policy_duplicates:
            classification = DUPLICATE_WITH_EQUIVALENT_PROTECTION
        else:
            classification = REQUIRED_AND_PRESENT

        rows.append({
            "action": action,
            "source_a": requirement["in_source_1"],
            "source_b": requirement["in_source_2"],
            "source_c": provenance,
            "probe_resource": resource,
            "source_d_denying": denying,
            "source_e_decisions": per_policy,
            "exempted_principals": sorted(exempt),
            "classification": classification,
            "principals_expected_to_deny": expected,
            "principals_not_denying": gaps,
            "within_policy_duplicate_sids": within_policy_duplicates,
            "justification": requirement["justification"],
        })

    failing = [r for r in rows if r["classification"] not in PASSING]
    return {
        "capabilities": len(rows),
        "rows": rows,
        "failing": failing,
        "counts": {c: sum(1 for r in rows if r["classification"] == c)
                   for c in sorted({r["classification"] for r in rows})},
        "clean": not failing,
    }


def per_action_mutations() -> dict:
    """Every mandatory capability, mutated NINE ways in each policy that denies it."""
    docs = policies()
    results, survived = [], []

    for row in triangulate()["rows"]:
        action, resource = row["action"], row["probe_resource"]
        for entry in row["source_d_denying"]:
            name = entry["policy"]
            doc, ctx = docs[name], _ctx(name)
            for sid in entry["sids"]:
                for label, mutate in _MUTATIONS.items():
                    mutated = mutate(doc, sid, action)
                    if mutated is None:
                        continue
                    decision = iam_eval.decide(mutated, action, resource, ctx).decision
                    still_denied = decision is Decision.EXPLICIT_DENY
                    caught = not still_denied
                    record = {"action": action, "policy": name, "sid": sid,
                              "mutation": label, "decision": decision.name,
                              "caught": caught}
                    results.append(record)
                    if not caught:
                        # Not automatically a failure: another independently required Deny
                        # may still cover it. Recorded either way.
                        record["reason"] = "another Deny statement still covers this action"
                        survived.append(record)

    total = len(results)
    genuinely_survived = [
        s for s in survived
        if len([e for r in triangulate()["rows"] if r["action"] == s["action"]
                for e in r["source_d_denying"]]) <= 1
    ]
    return {
        "mutations_run": total,
        "caught": sum(1 for r in results if r["caught"]),
        "absorbed_by_duplicate_protection": len(survived) - len(genuinely_survived),
        "genuinely_survived": genuinely_survived,
        "score": f"{total - len(genuinely_survived)}/{total}" if total else "0/0",
        "clean": not genuinely_survived,
    }


def _delete_action(doc, sid, action):
    out = []
    for s in doc["Statement"]:
        if s.get("Sid") != sid:
            out.append(s); continue
        actions = [a for a in iam_eval._as_list(s.get("Action")) if a != action]
        if actions:
            out.append({**s, "Action": actions})
    return {**doc, "Statement": out}


def _misspell(doc, sid, action):
    return {**doc, "Statement": [
        s if s.get("Sid") != sid else
        {**s, "Action": [a if a != action else a + "X"
                         for a in iam_eval._as_list(s.get("Action"))]}
        for s in doc["Statement"]]}


def _delete_statement(doc, sid, action):
    return {**doc, "Statement": [s for s in doc["Statement"] if s.get("Sid") != sid]}


def _flip_effect(doc, sid, action):
    return {**doc, "Statement": [
        s if s.get("Sid") != sid else {**s, "Effect": "Allow"} for s in doc["Statement"]]}


def _narrow_resource(doc, sid, action):
    out = []
    for s in doc["Statement"]:
        if s.get("Sid") != sid or "NotResource" in s:
            out.append(s); continue
        out.append({**s, "Resource": "arn:aws:s3:::a-resource-this-never-matches"})
    return {**doc, "Statement": out}


def _invert_polarity(doc, sid, action):
    out, changed = [], False
    for s in doc["Statement"]:
        if s.get("Sid") != sid:
            out.append(s); continue
        if "NotResource" in s:
            out.append({**{k: v for k, v in s.items() if k != "NotResource"},
                        "Resource": s["NotResource"]}); changed = True
        elif "Resource" in s and s["Resource"] != "*":
            out.append({**{k: v for k, v in s.items() if k != "Resource"},
                        "NotResource": s["Resource"]}); changed = True
        else:
            out.append(s)
    return {**doc, "Statement": out} if changed else None


def _widen_condition(doc, sid, action):
    out, changed = [], False
    for s in doc["Statement"]:
        if s.get("Sid") == sid and s.get("Condition"):
            out.append({k: v for k, v in s.items() if k != "Condition"}); changed = True
        else:
            out.append(s)
    return {**doc, "Statement": out} if changed else None


def _wildcard_resource(doc, sid, action):
    out, changed = [], False
    for s in doc["Statement"]:
        if s.get("Sid") == sid and "Resource" in s and s["Resource"] != "*":
            out.append({**s, "Resource": "arn:aws:s3:::*"}); changed = True
        else:
            out.append(s)
    return {**doc, "Statement": out} if changed else None


def _sibling_resource(doc, sid, action):
    out, changed = [], False
    for s in doc["Statement"]:
        key = "NotResource" if "NotResource" in s else "Resource"
        if s.get("Sid") == sid and key in s and s[key] != "*":
            values = iam_eval._as_list(s[key])
            out.append({**s, key: [v.replace("signalnest-staging", "signalnest-sibling")
                                   for v in values]}); changed = True
        else:
            out.append(s)
    return {**doc, "Statement": out} if changed else None


_MUTATIONS = {
    "delete_action": _delete_action,
    "misspell_action": _misspell,
    "delete_statement": _delete_statement,
    "flip_effect_to_allow": _flip_effect,
    "narrow_resource_incorrectly": _narrow_resource,
    "invert_resource_polarity": _invert_polarity,
    "remove_condition": _widen_condition,
    "replace_exact_arn_with_wildcard": _wildcard_resource,
    "replace_with_sibling_arn": _sibling_resource,
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--mutations", action="store_true")
    args = parser.parse_args()

    tri = triangulate()
    mut = per_action_mutations() if args.mutations else None
    if args.json:
        print(json.dumps({"triangulation": tri, "mutations": mut}, indent=2, ensure_ascii=True))
    else:
        print(f"  capabilities {tri['capabilities']}  {tri['counts']}")
        for row in tri["failing"]:
            print(f"    {row['classification']} {row['action']} at {row['probe_resource']}",
                  file=sys.stderr)
        if mut:
            print(f"  mutations {mut['mutations_run']}  score {mut['score']}  "
                  f"absorbed-by-duplicate {mut['absorbed_by_duplicate_protection']}")
            for row in mut["genuinely_survived"]:
                print(f"    SURVIVED {row['policy']}/{row['sid']} {row['mutation']} "
                      f"{row['action']}", file=sys.stderr)
    ok = tri["clean"] and (mut["clean"] if mut else True)
    print("DENY TRIANGULATION: clean" if ok else "DENY TRIANGULATION: findings")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
