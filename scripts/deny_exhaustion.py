#!/usr/bin/env python3
"""Final-defender exhaustion: a FALSIFIABLE Deny mutation harness (Gate 4N-I11, Defect 3).

WHY THE OLD ONE WAS DISCARDED RATHER THAN PATCHED. `deny_triangulation.per_action_mutations`
counted a mutation as "genuinely surviving" only when the number of denying policies was
`<= 1`. The observed minimum is 4. The filter was unreachable, so `clean` was a constant
True and `test_no_mandatory_mutation_survives` could not fail. The Gate 4N-I10 adversarial
lane gutted EVERY Deny statement from permanent_w0 and still received `score 1432/1432,
clean True`. A score that cannot go down is not a measurement.

THE REPLACEMENT ASKS A DIFFERENT QUESTION. Not "did some policy still deny it?" — with five
principals each carrying a ceiling, something almost always does. Instead:

    Remove the defenders ONE AT A TIME until none is left.
    The removal of the FINAL defender must flip the decision.

If it does not, the capability was never actually defended by any of them — the decision was
coming from somewhere else, or from nothing. That is the only formulation where the result
can be wrong, which is the only formulation worth running.

Three properties, each of which the old harness lacked:
  - no constant verdict: `clean` is computed from observed flips
  - no threshold on defender COUNT: exhaustion is by construction, not by comparison
  - a capability earns credit ONLY when final-defender removal fails

The harness is itself mutated by tests/test_deny_exhaustion.py: forcing `clean`, skipping
capabilities, ignoring the final-defender check and returning success on empty input must
each make its own test suite fail.

Usage:
    python3 scripts/deny_exhaustion.py [--json]
Exit: 0 iff every mandatory capability is exhaustible and its final defender is decisive.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import deny_triangulation as dt  # probe resources and principal contexts only
import iam_eval  # noqa: E402
from iam_eval import Decision  # noqa: E402


def _strip_statement(doc: dict, sid: str) -> dict:
    return {**doc, "Statement": [s for i, s in enumerate(doc["Statement"])
                                 if (s.get("Sid") or f"<statement {i}>") != sid]}


def defenders_for(action: str, resource: str, policies: dict) -> list[tuple[str, str]]:
    """(policy, sid) for every statement that currently produces EXPLICIT_DENY."""
    out = []
    for name, doc in policies.items():
        result = iam_eval.decide(doc, action, resource, dt._ctx(name))
        if result.decision is Decision.EXPLICIT_DENY:
            out.extend((name, sid) for sid in result.matching_deny_sids)
    return out


def exhaust(action: str, resource: str, policies: dict) -> dict:
    """Remove defenders one at a time; the LAST removal must flip the decision."""
    working = {name: copy.deepcopy(doc) for name, doc in policies.items()}
    defenders = defenders_for(action, resource, working)
    if not defenders:
        return {"action": action, "resource": resource, "initial_defenders": 0,
                "removal_sequence": [], "final_removal_flipped": False,
                "verdict": "UNDEFENDED",
                "why": "no statement denies this capability at its protected resource"}

    sequence = []
    remaining = list(defenders)
    while remaining:
        name, sid = remaining.pop(0)
        working[name] = _strip_statement(working[name], sid)
        still = defenders_for(action, resource, working)
        # A removal can expose defenders that were shadowed; re-enumerate rather than trust
        # the initial list, or the "final" defender would be whichever was listed last.
        for entry in still:
            if entry not in remaining:
                remaining.append(entry)
        any_denies = bool(still)
        sequence.append({"removed": f"{name}/{sid}", "defenders_left": len(still),
                         "still_denied": any_denies})
        if not still:
            break

    final_decisions = {name: iam_eval.decide(doc, action, resource, dt._ctx(name)).decision.name
                       for name, doc in working.items()}
    flipped = all(d != Decision.EXPLICIT_DENY.name for d in final_decisions.values())
    return {
        "action": action, "resource": resource,
        "initial_defenders": len(defenders),
        "defender_sids": [f"{n}/{s}" for n, s in defenders],
        "removal_sequence": sequence,
        "decisions_after_exhaustion": final_decisions,
        "final_removal_flipped": flipped,
        "verdict": "EXHAUSTIBLE_AND_DECISIVE" if flipped else "NOT_DECISIVE",
        "why": ("removing the final defender flipped the decision away from EXPLICIT_DENY"
                if flipped else
                "every defender was removed and the decision is STILL EXPLICIT_DENY — the "
                "defenders were not what was producing it"),
    }


def run(policies: dict | None = None, capabilities: list[str] | None = None) -> dict:
    policies = policies if policies is not None else dt.policies()
    rows_in = dt.triangulate()["rows"]
    if capabilities is not None:
        rows_in = [r for r in rows_in if r["action"] in capabilities]

    results = [exhaust(r["action"], r["probe_resource"], policies) for r in rows_in]
    failing = [r for r in results if r["verdict"] != "EXHAUSTIBLE_AND_DECISIVE"]

    # A run over nothing is not a pass. The old harness could report a perfect score on a
    # silently shrunken denominator.
    if not results:
        return {"capabilities": 0, "results": [], "failing": [],
                "clean": False,
                "why": "no capabilities were evaluated; an empty run is not a pass"}

    return {
        "capabilities": len(results),
        "total_defenders": sum(r["initial_defenders"] for r in results),
        "results": results,
        "failing": failing,
        "clean": not failing,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    report = run()
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=True))
    else:
        print(f"  capabilities {report['capabilities']}  "
              f"defenders {report.get('total_defenders', 0)}  "
              f"not-decisive {len(report['failing'])}")
        for row in report["failing"]:
            print(f"    {row['verdict']} {row['action']} at {row['resource']}: {row['why']}",
                  file=sys.stderr)
        print("DENY EXHAUSTION: clean" if report["clean"] else "DENY EXHAUSTION: findings")
    return 0 if report["clean"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
