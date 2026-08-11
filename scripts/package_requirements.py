#!/usr/bin/env python3
"""Independent package-completeness oracle — Gate 4N-I24C, finding I24C-05.

THE DEFECT THIS CLOSES (adversarial X4, the finding that made I23's mandatory answer YES).

`commit_package_coherence` answered "is the package complete?" like this:

    EXPECTED : repo_scripts = {f"scripts/{p.name}" for p in (REPO_ROOT/"scripts").glob("*.py")}
               repo_tests   = {f"tests/{p.name}"   for p in (REPO_ROOT/"tests").glob("test_*.py")}
    OBSERVED : present      = tree_paths(tree_hash)

Both sides descend from the WORKING TREE, which is also the object the predicted tree is
built from. Delete a control from both and expectation and observation move together. The
adversarial lane executed it: removing `tests/test_final_agenda_remediation.py` from the
worktree AND the index left coherence reporting "coherent", every guard at 0, 1961 passed,
and nothing failed. That is the Gate 4N-I22 blocker-2 defect class living inside blocker 1's
own remediation.

The repository already had an authored inventory for provenance rows and one for writable
roles. It had NONE for the package itself. This module supplies it.

SCOPING, ADOPTED FROM THE ADVERSARIAL LANE VERBATIM. `commit_package_coherence` requiring
referenced paths to EXIST is that module doing its stated job — presence in the commit. It is
NOT an invocation oracle, NOT a provenance oracle, and NOT a completeness oracle, and it must
not be widened into any of them. This module is separate and answers exactly one question:
"does the predicted commit contain everything the package is REQUIRED to contain?"

INDEPENDENCE, STATED HONESTLY. The requirement fixture is AUTHORED and thereafter FROZEN. Its
initial enumeration was seeded by inspection, exactly as tests/fixtures/expected-writable-
roles.json was seeded by reading the composition. What makes it an oracle is that it is NEVER
RE-DERIVED AT CHECK TIME: this module reads the fixture and the tree and requires the tree to
satisfy the fixture. Deleting a control therefore moves only the OBSERVED side. Changing the
fixture is a reviewable diff and must be treated as one.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS = REPO_ROOT / "tests" / "fixtures" / "package-requirements.json"

# Never permitted in a commit, regardless of what the requirement lists.
PROHIBITED_PREFIXES = ("infra/aws/live-resource-inventory.json",
                       "infra/aws/cloudfront-expected.json",
                       ".claude/agents/")
PROHIBITED_SUBSTRINGS = (".signalnest/", "FROZEN-CANDIDATE.json")


class PackageRequirementError(RuntimeError):
    """Fail-closed."""


def requirements() -> dict:
    if not REQUIREMENTS.exists():
        raise PackageRequirementError(
            f"the authored package requirement is absent: {REQUIREMENTS}. Absence must never "
            "be read as 'nothing is required'.")
    doc = json.loads(REQUIREMENTS.read_text(encoding="utf-8"))
    groups = doc.get("required_paths")
    if not isinstance(groups, dict) or not groups:
        raise PackageRequirementError("the requirement declares no required paths")
    for name, paths in groups.items():
        if not isinstance(paths, list) or not paths:
            raise PackageRequirementError(f"required group {name!r} is empty")
    return doc


def tree_paths(tree_hash: str) -> set[str]:
    out = subprocess.run(["git", "ls-tree", "-r", "--name-only", tree_hash],
                         cwd=REPO_ROOT, capture_output=True, text=True)
    if out.returncode != 0:
        raise PackageRequirementError(f"cannot list tree {tree_hash}: {out.stderr.strip()}")
    return {line for line in out.stdout.split("\n") if line}


def check(tree_hash: str | None = None) -> dict:
    if tree_hash is None:
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        import tracked_state
        tree_hash = tracked_state.predicted_commit_tree()["predicted_tree_hash"]

    doc = requirements()
    present = tree_paths(tree_hash)
    problems: list[str] = []
    by_group = {}

    for group, paths in sorted(doc["required_paths"].items()):
        missing = sorted(p for p in paths if p not in present)
        by_group[group] = {"required": len(paths), "missing": missing}
        for m in missing:
            problems.append(f"{group}: REQUIRED path {m!r} is absent from the commit")

    for p in sorted(present):
        if p.startswith(PROHIBITED_PREFIXES) or any(s in p for s in PROHIBITED_SUBSTRINGS):
            problems.append(f"PROHIBITED path {p!r} is in the commit")

    pins = check_remediation_pins(present)
    problems.extend(pins["problems"])

    return {
        "requirement_source": str(REQUIREMENTS),
        "requirement_kind": "INDEPENDENTLY_AUTHORED_PACKAGE_CONTRACT",
        "tree": tree_hash,
        "paths_in_tree": len(present),
        "required_total": sum(len(v) for v in doc["required_paths"].values()),
        "by_group": by_group,
        "remediation_pins": pins,
        "problems": problems,
        "complete": not problems,
    }


PIN_REGISTRY = REPO_ROOT / "tests" / "fixtures" / "remediation-pin-registry.json"


def remediation_pins() -> list[dict]:
    """The independently authored remediation-pin registry.

    Deliberately does NOT enumerate the tests directory: a registry generated from the tree
    agrees with the tree by construction, which is the defect this exists to prevent.
    """
    if not PIN_REGISTRY.exists():
        raise PackageRequirementError(
            f"the remediation-pin registry is absent: {PIN_REGISTRY}. Absence must never be "
            "read as 'every remediation is still pinned'.")
    doc = json.loads(PIN_REGISTRY.read_text(encoding="utf-8"))
    entries = doc.get("remediations")
    if not isinstance(entries, list) or not entries:
        raise PackageRequirementError("the remediation-pin registry declares no remediations")
    return entries


def check_remediation_pins(present: set) -> dict:
    """GATE 4N-I27R. Every remediation family must still have its regression pin IN the commit.

    THE DEFECT THIS CLOSES. `package-requirements.json` never listed
    tests/test_i27o_blocker_remediations.py, and the check above only asks whether required
    paths are present — never whether a pin that ought to be required is missing from the
    requirement list itself. Gate 4N-I27Q's architect lane deleted that one file and every
    control stayed green, taking the sole regression pin for four High remediations with it.

    BOTH DIRECTIONS. Registry-minus-tree catches a deleted pin; tree-minus-registry catches a
    remediation test that exists but that no registry entry claims, which is how a pin silently
    stops being load-bearing. A registry entry naming a path that does not exist is itself a
    finding, so the registry cannot be satisfied by pointing at nothing.
    """
    problems: list[str] = []
    families, pin_paths = [], set()
    seen = set()
    for entry in remediation_pins():
        family = entry.get("family")
        test_path = entry.get("test_path")
        sources = entry.get("source_paths") or []
        if not family or not test_path:
            problems.append(f"malformed remediation-pin entry: {entry!r}")
            continue
        if family in seen:
            problems.append(f"remediation family {family!r} is registered twice")
        seen.add(family)
        if not entry.get("canary") or not entry.get("expected_control"):
            problems.append(f"{family}: no canary or expected control is stated")
        if test_path not in present:
            problems.append(
                f"{family}: regression pin {test_path!r} is ABSENT from the commit. The "
                "remediation is unpinned, so its defect could return undetected.")
        if not (REPO_ROOT / test_path).exists():
            problems.append(f"{family}: registry names a test path that does not exist: "
                            f"{test_path!r}")
        for src in sources:
            if src not in present:
                problems.append(f"{family}: remediated source {src!r} is absent from the commit")
        families.append(family)
        pin_paths.add(test_path)

    # tree-minus-registry: a remediation-shaped test nobody claims.
    unclaimed = sorted(p for p in present
                       if p.startswith("tests/test_") and _looks_like_a_remediation_pin(p)
                       and p not in pin_paths)
    for p in unclaimed:
        problems.append(f"remediation-shaped test {p!r} is in the commit but no registry entry "
                        "claims it, so nothing states which defect it pins")

    return {"registry": str(PIN_REGISTRY), "families": families,
            "pin_paths": sorted(pin_paths), "unclaimed": unclaimed,
            "bidirectional": True, "problems": problems}


def _looks_like_a_remediation_pin(path: str) -> bool:
    """A test whose name marks it as pinning a specific gate's remediation."""
    name = path.rsplit("/", 1)[-1]
    return bool(re.match(r"^test_(i\d+[a-z]*_|tier\d+_|tag_key_)", name))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tree")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    result = check(args.tree)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"  tree {result['tree']}  {result['paths_in_tree']} paths; "
              f"{result['required_total']} required")
        for g, v in sorted(result["by_group"].items()):
            print(f"    {g:22s} {v['required']:3d} required, {len(v['missing'])} missing")
        for p in result["problems"]:
            print(f"    {p}", file=sys.stderr)
        print("PACKAGE REQUIREMENTS:", "complete" if result["complete"] else "INCOMPLETE")
    return 0 if result["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
