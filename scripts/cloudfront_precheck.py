#!/usr/bin/env python3
"""Fail-closed CloudFront pre-execution inventory (Gate 4N-I5).

Restores a mandatory precheck inherited from Gate 4N-H4 and dropped in 4N-I3/I4. Provider
source proves `cloudfront:GetDistributionConfig` is NOT required for the refresh path, so
this check deliberately does NOT use it — but "not required for refresh" is a different
claim from "the edge surface is what we think it is", and only the second one licenses a
stamp or a plan.

Three permission classes are kept distinct, because conflating them is what let the check
disappear:

  REFRESH_READS    called by the provider on every plan; already in the operator closure
  INVENTORY_READS  called ONCE by this precheck before stamping; a superset of the above
  MUTATIONS        never granted to either operator; listed only so their absence is explicit

Runs read-only against AWS and is fail-closed: any unexpected finding exits non-zero.
Unit tests never call AWS — they exercise `evaluate()` against recorded fixtures.

Usage:
    python3 scripts/cloudfront_precheck.py --profile <name> [--json]
    python3 scripts/cloudfront_precheck.py --fixture path.json   # offline evaluation
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

REFRESH_READS = ["cloudfront:GetDistribution", "cloudfront:GetOriginAccessControl",
                 "cloudfront:ListTagsForResource"]
INVENTORY_READS = REFRESH_READS + ["cloudfront:ListDistributions", "cloudfront:ListOriginAccessControls"]
MUTATIONS_NEVER_GRANTED = ["cloudfront:CreateDistribution", "cloudfront:UpdateDistribution",
                           "cloudfront:DeleteDistribution", "cloudfront:CreateOriginAccessControl",
                           "cloudfront:UpdateOriginAccessControl", "cloudfront:DeleteOriginAccessControl"]
DELIBERATELY_NOT_USED = ["cloudfront:GetDistributionConfig"]

# Repository evidence: modules/edge declares exactly one distribution and one OAC.
EXPECTED_DISTRIBUTIONS = 1
EXPECTED_OACS = 1


def evaluate(inventory: dict, expected: dict) -> dict:
    """Compare an inventory against repository expectations. No AWS access."""
    findings: list[str] = []

    dists = inventory.get("distributions", [])
    oacs = inventory.get("origin_access_controls", [])

    if len(dists) != EXPECTED_DISTRIBUTIONS:
        findings.append(
            f"expected exactly {EXPECTED_DISTRIBUTIONS} staging distribution, found {len(dists)} "
            "— an unexpected second distribution must be explained before stamping"
        )
    if len(oacs) != EXPECTED_OACS:
        findings.append(
            f"expected exactly {EXPECTED_OACS} origin access control, found {len(oacs)} "
            "— an unmanaged OAC must be explained before stamping"
        )

    if dists:
        dist = dists[0]
        if expected.get("distribution_id") and dist.get("Id") != expected["distribution_id"]:
            findings.append(f"distribution identity mismatch: {dist.get('Id')} != {expected['distribution_id']}")
        if dist.get("Status") != "Deployed":
            findings.append(f"distribution status is {dist.get('Status')!r}, expected 'Deployed'")
        if dist.get("Enabled") is not True:
            findings.append(f"distribution Enabled is {dist.get('Enabled')!r}, expected True")
        aliases = (dist.get("Aliases") or {}).get("Items") or []
        if expected.get("aliases") is not None and sorted(aliases) != sorted(expected["aliases"]):
            findings.append(f"aliases {sorted(aliases)} != expected {sorted(expected['aliases'])}")
        origins = [o.get("Id") for o in ((dist.get("Origins") or {}).get("Items") or [])]
        if not origins:
            findings.append("distribution reports no origins")

    if oacs:
        oac = oacs[0]
        if expected.get("oac_id") and oac.get("Id") != expected["oac_id"]:
            findings.append(f"origin access control identity mismatch: {oac.get('Id')} != {expected['oac_id']}")

    return {
        "permission_classes": {
            "refresh_reads": REFRESH_READS,
            "inventory_reads": INVENTORY_READS,
            "mutations_never_granted": MUTATIONS_NEVER_GRANTED,
            "deliberately_not_used": DELIBERATELY_NOT_USED,
        },
        "expected": expected,
        "observed": {"distributions": len(dists), "origin_access_controls": len(oacs)},
        "findings": findings,
        "clean": not findings,
    }


def collect(profile: str) -> dict:
    """Read-only AWS inventory. Never called by unit tests."""
    def run(args: list[str]) -> dict:
        proc = subprocess.run(["aws", *args, "--profile", profile, "--output", "json"],
                              capture_output=True, text=True, timeout=90)
        if proc.returncode != 0:
            return {"_error": proc.stderr.strip()[:400]}
        return json.loads(proc.stdout or "{}")

    dl = run(["cloudfront", "list-distributions"])
    ol = run(["cloudfront", "list-origin-access-controls"])
    return {
        "distributions": ((dl.get("DistributionList") or {}).get("Items") or []) if "_error" not in dl else [],
        "origin_access_controls": ((ol.get("OriginAccessControlList") or {}).get("Items") or []) if "_error" not in ol else [],
        "errors": {k: v.get("_error") for k, v in (("list-distributions", dl), ("list-origin-access-controls", ol)) if "_error" in v},
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile")
    parser.add_argument("--fixture", help="evaluate a recorded inventory offline")
    # GATE 4N-I18, SEC-1: the expected edge identifiers are AWS-assigned and were contained
    # with the rest of the live inventory. They now arrive through the tier-resolved loader;
    # --expected remains for an explicit external override.
    parser.add_argument("--expected", default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    # GATE 4N-I18, SEC-1. The edge identifiers used to sit in a repository file
    # (infra/aws/cloudfront-expected.json) that Gate 4N-I17's security lane flagged as live
    # first-disclosure material. They now come from the tier-resolved inventory: synthetic
    # under Tier 1, the real values under Tier 2 through the protected channel. An explicit
    # --expected path still overrides, for an operator supplying evidence out of band.
    if args.expected:
        expected_path = Path(args.expected)
        expected = json.loads(expected_path.read_text(encoding="utf-8")) if expected_path.exists() else {}
    else:
        import protected_inventory
        expected = protected_inventory.load().data.get("cloudfront", {})

    if args.fixture:
        inventory = json.loads(Path(args.fixture).read_text(encoding="utf-8"))
    elif args.profile:
        inventory = collect(args.profile)
        if inventory.get("errors"):
            print(f"FAIL: inventory reads denied or failed: {inventory['errors']}", file=sys.stderr)
            return 1
    else:
        print("FAIL: supply --profile (live) or --fixture (offline)", file=sys.stderr)
        return 1

    result = evaluate(inventory, expected)
    print(json.dumps(result, indent=2) if args.json else
          ("CLOUDFRONT PRECHECK: clean" if result["clean"] else
           "CLOUDFRONT PRECHECK: " + "; ".join(result["findings"])))
    return 0 if result["clean"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
