#!/usr/bin/env python3
"""Authoritative policy inventory by DISCOVERY (Gate 4N-I11, Defect 6).

THE DEFECT. Gate 4N-I10 shipped a RoleBootstrapOperator policy that its own validator
rejects — `iam:TagRole` conditioned on `iam:PermissionsBoundary`, a key TagRole does not
support, so the grant was dead. The detector existed. A negative-control test built that
exact shape to prove the detector fires. The policy was simply never passed to
`validate_policy`, because the call sites were a HAND-MAINTAINED LIST and the newest artifact
was not on it. 933 tests were green.

A hand-maintained list of things to check cannot protect the thing you just added, because
adding it is the moment you forget. So the inventory is DISCOVERED: every module under
scripts/ matching the generator contract is enumerated, its policy-producing callables are
found by introspection, and anything produced but not enumerated is an ORPHAN and fails.

Usage:
    python3 scripts/policy_inventory.py [--json]
Exit: 0 iff every discovered policy validates.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import inspect
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import iam_eval  # noqa: E402

# A real instant, so temporary policies render with a valid expiry during discovery.
# GATE 4N-I19, ADV-A: the ONE authoritative reviewed window. Gate 4N-I17's architect lane
# found ~20 independent expiry literals with nothing asserting they agreed; they now all
# resolve to the single authorized pair, so a restamp cannot leave stragglers behind.
import expiry_authorization as _ea  # noqa: E402

DISCOVERY_EXPIRY = _ea.ACTIVE_EXPIRY_UTC

# Modules that produce IAM policy documents. DISCOVERED by filename convention, not listed:
# a new gen_*polic*.py is picked up with no edit here.
#
# The first draft used "gen_*policy*.py" and silently missed gen_operator_policies.py —
# "policies" does not contain "policy". That is the orphan defect in miniature, found by this
# module on its first run: a discovery rule narrower than reality is a hand-maintained list
# wearing a glob. The stem "polic" covers both spellings, and
# test_policy_inventory.py::test_every_generator_module_is_discovered pins the count.
GENERATOR_GLOB = "gen_*polic*.py"

# Policies whose Allow ceiling is the boundary idiom rather than an identity grant.
BOUNDARY_KINDS = {"boundary_policy"}

# Callables that build a document for a DIFFERENT principal's inspection rather than being a
# policy in their own right would go here. Empty today; present so an exclusion must be
# written down rather than achieved by a narrower glob.
NOT_A_POLICY: set[str] = set()


def generator_modules() -> list[str]:
    return sorted(p.stem for p in SCRIPTS.glob(GENERATOR_GLOB))


def discover() -> dict[str, dict]:
    """Every policy document any generator module can produce."""
    found: dict[str, dict] = {}
    for name in generator_modules():
        module = importlib.import_module(name)
        for attr, fn in sorted(vars(module).items()):
            if not callable(fn) or attr.startswith("_"):
                continue
            if inspect.getmodule(fn) is not module:
                continue  # imported, not defined here
            if not attr.endswith(("_policy", "_policies")) or attr in NOT_A_POLICY:
                continue
            signature = inspect.signature(fn)
            required = [p for p in signature.parameters.values()
                        if p.default is inspect.Parameter.empty
                        and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
            try:
                doc = fn(DISCOVERY_EXPIRY) if required else fn()
            except Exception as exc:  # noqa: BLE001
                found[f"{name}.{attr}"] = {"module": name, "callable": attr,
                                           "error": f"{type(exc).__name__}: {exc}"}
                continue
            if not isinstance(doc, dict) or "Statement" not in doc:
                continue
            found[f"{name}.{attr}"] = {
                "module": name, "callable": attr, "document": doc,
                "temporary": bool(required),
                "kind": "boundary" if attr in BOUNDARY_KINDS else "identity",
            }
    return found


def validate_all() -> dict:
    rows = []
    for key, entry in sorted(discover().items()):
        if "error" in entry:
            rows.append({"policy": key, "result": "GENERATION_FAILED",
                         "problems": [entry["error"]]})
            continue
        doc = entry["document"]
        problems = list(iam_eval.validate_policy(doc, kind=entry["kind"]))

        # Temporary policies must carry a real expiry on every Allow.
        if entry["temporary"]:
            for statement in doc["Statement"]:
                if statement["Effect"] != "Allow":
                    continue
                value = statement.get("Condition", {}).get(
                    "DateLessThan", {}).get("aws:CurrentTime")
                if not value:
                    problems.append(f"{statement.get('Sid')}: temporary policy Allow has no expiry")
                else:
                    try:
                        iam_eval.parse_iam_date(value, what="expiry")
                    except iam_eval.UnsupportedPolicyFeature as exc:
                        problems.append(f"{statement.get('Sid')}: {exc}")

        rows.append({
            "policy": key, "kind": entry["kind"], "temporary": entry["temporary"],
            "statements": len(doc["Statement"]),
            "canonical_sha256": hashlib.sha256(json.dumps(
                doc, sort_keys=True, separators=(",", ":"),
                ensure_ascii=True).encode()).hexdigest(),
            "result": "VALID" if not problems else "INVALID",
            "problems": problems,
        })
    invalid = [r for r in rows if r["result"] != "VALID"]
    return {"discovered": len(rows), "modules": generator_modules(),
            "rows": rows, "invalid": invalid, "clean": not invalid}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = validate_all()
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=True))
    else:
        for row in result["rows"]:
            print(f"  {row['result']:16s} {row['policy']}")
        for row in result["invalid"]:
            for problem in row["problems"]:
                print(f"    {row['policy']}: {problem}", file=sys.stderr)
        print(f"  discovered {result['discovered']} from {len(result['modules'])} modules")
        print("POLICY VALIDATION: clean" if result["clean"] else "POLICY VALIDATION: findings")
    return 0 if result["clean"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
