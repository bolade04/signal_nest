#!/usr/bin/env python3
"""Decisive ReadOnlyVerifier ceiling check (Gate 4N-I19, AWS-1, Phases H/I).

THE DEFECT THIS CLOSES. `gen_readonly_verifier_policy` built its only Deny as
`NotAction: ALL_ACTIONS`, where `ALL_ACTIONS` was exactly the union of its own Allow sets. A
Deny whose exemption list is computed from the Allow list cannot constrain that Allow list:
anything added to the Allow is automatically removed from the Deny's NotAction, so the policy
stays internally consistent while becoming strictly more permissive. The only thing left
standing between the generator and an escalation was `is_read_only()`, which guessed from verb
prefixes. Gate 4N-I17's AWS-permissions lane proved the consequence by executing it: adding
`sso:GetRoleCredentials` — which returns live temporary AWS credentials — was accepted, placed
on `Resource "*"`, and evaluated EXPLICIT_ALLOW.

THE MODEL. The expected value is an INDEPENDENTLY AUTHORED contract,
`tests/fixtures/readonly-verifier-ceiling.json`, written from what the permission set is FOR.
The observed value is the generated policy. Neither is computed from the other:

  * the ceiling is not generated from the Allow list, the Deny list, the classifier output or
    any candidate artifact — it is authored, tracked, and changing it is a reviewable diff;
  * the generated Deny may remain, but it is DEFENCE IN DEPTH ONLY. It is never the decisive
    check, because a self-derived exemption list is not evidence about anything.

WHY BOTH AN ACTION ALLOWLIST AND A CATEGORY RULE. Either alone can be defeated. A pure
category rule inherits any misclassification; a pure action allowlist says nothing about an
action whose meaning changes. Requiring both means an escalation has to defeat an authored
list AND an exact classification, which have no common ancestor.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

CEILING_PATH = REPO_ROOT / "tests" / "fixtures" / "readonly-verifier-ceiling.json"


class CeilingViolation(Exception):
    """Fail-closed. A violation is never downgraded to a warning."""


def ceiling() -> dict:
    return json.loads(CEILING_PATH.read_text(encoding="utf-8"))


def _statement_actions(statement: dict) -> list[str]:
    value = statement.get("Action")
    if value is None:
        return []
    return [value] if isinstance(value, str) else list(value)


def _statement_resources(statement: dict) -> list[str]:
    value = statement.get("Resource")
    if value is None:
        return []
    return [value] if isinstance(value, str) else list(value)


def check(policy: dict, *, contract: dict | None = None) -> dict:
    # GATE 4N-I24D. `unknown_action_behaviour` was authored and consumed by NOTHING: the
    # ceiling could declare FAIL while an unknown action was silently permitted. It now
    # decides the treatment of an action absent from the authored allowlist.
    _c = ceiling()
    # GATE 4N-I24D. `expiry_required_on_every_allow` was authored and consumed by NOTHING, so
    # flipping it to false merely relaxed a check that happened to pass anyway — the field
    # could not change any outcome. Only the safe value is permitted, and when it is declared
    # the requirement is ENFORCED against the emitted document rather than assumed.
    _expiry_required = _c.get("expiry_required_on_every_allow")
    if _expiry_required is not True:
        raise CeilingViolation(
            f"the authored ceiling declares expiry_required_on_every_allow="
            f"{_expiry_required!r}; only true is permitted. A temporary-access ceiling that "
            "does not require an expiry on every Allow is not a ceiling.")
    _behaviour = str(_c.get("unknown_action_behaviour", "")).upper()
    if _behaviour != "FAIL":
        raise CeilingViolation(
            f"the authored ceiling declares unknown_action_behaviour={_behaviour!r}; only "
            "'FAIL' is permitted. An allowlist that admits the unknown is not a ceiling.")
    """Compare a generated verifier policy against the independently authored ceiling."""
    import action_classifier

    contract = contract or ceiling()
    permitted = set(contract["permitted_actions"])
    permitted_categories = set(contract["permitted_categories"])
    forbidden_categories = set(contract["forbidden_categories"])
    explicitly_forbidden = contract["explicitly_forbidden_actions"]
    may_star = set(contract["resource_rules"]["actions_that_may_use_star"])
    forbid_service_wildcards = contract["resource_rules"]["wildcard_service_actions_forbidden"]
    require_expiry = contract["expiry_required_on_every_allow"]

    findings: list[str] = []
    allowed_actions: list[str] = []

    for statement in policy.get("Statement", []):
        if statement.get("Effect") != "Allow":
            continue
        actions = _statement_actions(statement)
        resources = _statement_resources(statement)
        sid = statement.get("Sid", "<no Sid>")

        if require_expiry:
            date = (statement.get("Condition") or {}).get("DateLessThan", {})
            if "aws:CurrentTime" not in date:
                findings.append(f"{sid}: an Allow carries no expiry condition")

        for action in actions:
            allowed_actions.append(action)

            if action == "*" or action.endswith(":*"):
                if forbid_service_wildcards:
                    findings.append(
                        f"{sid}: wildcard action {action!r} — a service wildcard admits every "
                        "future action that service gains")
                continue

            if action in explicitly_forbidden:
                findings.append(
                    f"{sid}: {action} is EXPLICITLY FORBIDDEN by the authored ceiling "
                    f"({explicitly_forbidden[action]})")
                continue

            if action not in permitted:
                findings.append(
                    f"{sid}: {action} is not in the authored ceiling's permitted set. The "
                    "ceiling is an allowlist; absence is refusal.")

            result = action_classifier.classify(action)
            disallowed = [c for c in result["categories"] if c in forbidden_categories]
            if disallowed:
                findings.append(
                    f"{sid}: {action} classifies {sorted(disallowed)}, which the ceiling forbids")
            elif not set(result["categories"]) <= permitted_categories:
                findings.append(
                    f"{sid}: {action} classifies {result['categories']}, which the ceiling does "
                    "not admit")

            if "*" in resources and action not in may_star:
                findings.append(
                    f"{sid}: {action} is granted on Resource '*' but the ceiling does not permit "
                    "a wildcard resource for it")

    missing = sorted(permitted - set(allowed_actions))
    return {
        "allowed_actions": sorted(set(allowed_actions)),
        "permitted_actions": sorted(permitted),
        "actions_in_policy_not_in_ceiling": sorted(set(allowed_actions) - permitted),
        "ceiling_actions_not_in_policy": missing,
        "findings": findings,
        "clean": not findings,
        "decisive_source": "tests/fixtures/readonly-verifier-ceiling.json (independently authored)",
        "generated_deny_is_defence_in_depth_only": True,
    }


def require_within_ceiling(policy: dict) -> None:
    """Raise unless the policy is inside the authored ceiling. Used at generation time."""
    result = check(policy)
    if not result["clean"]:
        raise CeilingViolation(
            "the generated ReadOnlyVerifier policy exceeds the independently authored ceiling:\n  "
            + "\n  ".join(result["findings"]))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--expiry", default=None,
                        help="expiry for the generated policy under test")
    parser.add_argument("--issuance", default=None)
    args = parser.parse_args()

    import expiry_authorization
    import gen_readonly_verifier_policy as rv

    pair = expiry_authorization.active_pair()
    policy = rv.readonly_verifier_policy(args.expiry or pair["expiry_utc"],
                                         issuance=args.issuance or pair["issuance_utc"])
    result = check(policy)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=True))
    else:
        for finding in result["findings"]:
            print(f"  {finding}", file=sys.stderr)
        print(f"  allowed {len(result['allowed_actions'])} actions; ceiling permits "
              f"{len(result['permitted_actions'])}")
        print("VERIFIER CEILING: clean" if result["clean"] else "VERIFIER CEILING: findings")
    return 0 if result["clean"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
