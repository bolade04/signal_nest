#!/usr/bin/env python3
"""Independent mutation-site discovery — Gate 4N-I24C, findings I24C-12 and I24C-13.

THE DEFECT THIS CLOSES.

Gate 4N-I23 shipped `source-level-falsification-results.json` claiming total 20 / caught 20 /
survived 0. The adversarial lane then falsified three of its rows by execution: L11 and L12
directly (the echoed CI bodies), and the L5 closure narrative ("the guard now exits 1 naming
the coupling" — it exited 0). Coverage had been DEFINED as "all hand-authored mutations
passed", so the sweep measured the imagination of the person who wrote the controls rather
than the strength of the controls. The mutation sites were chosen by their author.

So this module does not take a list. It DISCOVERS load-bearing sites from independent
structure — the AST of every guard script, the graded-step graph of the workflow, and the
key sets of the authored contracts — and then reports which of those sites any test actually
references. A site nothing references is UNTESTED, and untested load-bearing sites are a
finding rather than a footnote.

I24C-13, ISOLATION. The I23 sweep's own method note claimed "a scratch copy cannot reproduce
index/HEAD state (established I20B)". The adversarial lane rebutted that by doing it: a
byte-clone including `.git` reproduces index and HEAD exactly, and it verified REPO_ROOT
resolved to the clone. In-place mutation of a shared checkout during a concurrent multi-lane
review corrupted two suite runs and one coherence run at I23. `isolated_clone()` below is the
default, so falsification no longer needs the live tree.
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"
TESTS = REPO_ROOT / "tests"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

# The layers a load-bearing claim must be mutated at (Phase T of the authorization).
# GATE 4N-I26B, closing part of I26B-01/I26B-11.
#
# This used to be a hand-authored tuple of TWELVE layer names. The discoverers emit layers
# COMPUTED from what they find, and several declared names were never emitted by anything —
# they were credited by matching a set-wide TEST NAME, so a layer with no discoverer and no
# site could still be reported covered. A declared layer nothing discovers inflates the
# denominator and proves nothing.
#
# There is no declared layer list any more. What IS asserted is that each of the three
# DISCOVERERS yields sites: a discoverer that has stopped working is the failure worth
# catching, and unlike a name list it cannot drift from what the code does.
DISCOVERERS = ("function", "graded_step", "requirement_key")

# GATE 4N-I28K, closing I28J-01. `DECISIVE_SUFFIXES` LIVED HERE AND IS GONE.
#
# It was a tuple of eleven word endings, and a function was a load-bearing site when its NAME
# ended in one of them inside a file whose only qualification was appearing literally in ci.yml.
# Gate 4N-I28J executed the consequences: renaming a real control removed it (127 -> 126), a
# never-called `never_called_check` entered on its name alone (127 -> 128), a genuinely enforcing
# helper with a neutral name was never counted, and deleting the single word "check" took eleven
# controls out of the universe (127 -> 116) without changing one line of enforcement. The match
# was a raw string suffix rather than a word, so `path_is_in_scan_domain` qualified via "main".
#
# The fix is NOT a longer list — a longer list is the same defect with more entries. The function
# discoverer now delegates to scripts/site_taxonomy.py, which derives membership from invocation
# and consequence. There is no name-shaped rule left here to lengthen.


def isolated_clone(dest: Path | None = None) -> Path:
    """A byte-clone INCLUDING .git, so index and HEAD are reproduced exactly.

    Gate 4N-I24C, finding I24C-13. This exists so falsification never has to mutate the
    shared checkout again.
    """
    dest = Path(tempfile.mkdtemp(prefix="i24c-clone-")) if dest is None else dest
    subprocess.run(["rsync", "-a", "--exclude", "__pycache__", "--exclude", ".pytest_cache",
                    f"{REPO_ROOT}/", f"{dest}/"], check=True, capture_output=True)
    if not (dest / ".git").exists():
        raise RuntimeError("clone lacks .git; index and HEAD would not be reproduced")
    return dest


# --------------------------------------------------------------------------- #
# discovery
# --------------------------------------------------------------------------- #

def discover_sites() -> list[dict]:
    """Load-bearing mutation sites, derived from structure and not from a hand-written list."""
    sites: list[dict] = []

    # 1. Enforcement path: every function reachable from a workflow invocation that can change
    #    an outcome. GATE 4N-I28K — derived by scripts/site_taxonomy.py from invocation and
    #    consequence. This used to be an AST walk matching name suffixes; see the note above
    #    DISCOVERERS for what that cost.
    sys.path.insert(0, str(SCRIPTS))
    import site_taxonomy

    sites.extend(site_taxonomy.production_control_function_sites())

    # 2. Workflow: every graded step is a wiring site.
    for step in site_taxonomy.graded_steps():
        sites.append({"kind": "graded_step", "module": "ci.yml", "name": step,
                      "layer": "workflow", "id": f"ci.yml::{step}"})

    # 3. Authored contracts: every top-level requirement key is a requirement site.
    for fixture in sorted((REPO_ROOT / "tests" / "fixtures").glob("*.json")):
        try:
            doc = json.loads(fixture.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(doc, dict):
            continue
        for key in doc:
            if key.startswith("_"):
                continue
            sites.append({"kind": "requirement_key", "module": fixture.name, "name": key,
                          "layer": "requirement", "id": f"{fixture.name}::{key}"})
    return sites


# Tests that assert over a WHOLE SET rather than naming each member. A site covered by one
# of these is genuinely tested even though its name never appears in a test file; counting
# only name references under-reports coverage and would push toward writing 33 near-identical
# tests instead of one set assertion.
SET_COVERING_TESTS = {
    "workflow": ("test_every_graded_step_is_covered_by_the_contract",
                 "test_the_workflow_satisfies_the_authored_invocation_contract"),
}


# GATE 4N-I26B, closing I26B-11. `tested_sites()` LIVED HERE AND IS GONE.
#
# It credited a site when its NAME appeared anywhere in a test file. Gate 4N-I25's adversarial
# addendum showed what that means in practice: a newly authored key consumed by NO guard, named
# only in a test dictionary, was DISCOVERED and CREDITED on arrival — scored as covered before
# anyone wrote a line of enforcement for it. The module carried a REFERENCE_ONLY_WARNING saying
# the number was not coverage, in prose, next to the number a reader sees.
#
# A warning is not a control. Discovery now answers ONE question — what are the load-bearing
# sites? — and does not report coverage at all. Whether a site is covered is decided by
# scripts/site_coverage.py, by EXECUTED mutation, and by nothing else.


def coverage() -> dict:
    """What are the load-bearing sites? THIS MODULE DOES NOT DECIDE COVERAGE.

    It reports the discovered set and its shape. `scripts/site_coverage.py` decides, by
    executing a mutation at each site and requiring the shipping guard to refuse. Two modules,
    because one that answered both questions could satisfy itself — the shared-ancestor defect
    already removed from the reconciler and from package completeness.
    """
    sites = discover_sites()
    by_layer: dict[str, dict] = {}
    for s in sites:
        by_layer.setdefault(s["layer"], {"total": 0})["total"] += 1

    problems = []
    kinds = {s["kind"] for s in sites}
    for discoverer in DISCOVERERS:
        if discoverer not in kinds:
            problems.append(
                f"the {discoverer!r} discoverer yielded NO sites. A discoverer that finds "
                "nothing reports a clean, empty world; that is the failure to catch, and it is "
                "checkable in a way a declared layer name never was.")

    ids = [s["id"] for s in sites]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    for dup in duplicates:
        problems.append(f"{dup}: duplicate site id — two sites sharing an identifier are "
                        "indistinguishable to every consumer")

    return {"discovered": len(sites), "site_ids": sorted(ids),
            "by_layer": by_layer, "discoverers": list(DISCOVERERS),
            "kinds_emitted": sorted({s["kind"] for s in sites}),
            "duplicates": duplicates, "problems": problems,
            "COVERAGE_IS_NOT_DECIDED_HERE":
                "This module reports DISCOVERY only. It no longer counts name references, "
                "because crediting a name let a site consumed by nothing be scored covered on "
                "arrival. Executed coverage: scripts/site_coverage.py.",
            "clean": not problems}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--fail-on-untested-layer", action="store_true",
                    help="retained for compatibility; failing on problems is now the "
                         "DEFAULT and this flag changes nothing")
    args = ap.parse_args(argv)
    result = coverage()
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"  discovered {result['discovered']} load-bearing sites "
              "(DISCOVERY ONLY — coverage is decided by scripts/site_coverage.py)")
        for layer, row in sorted(result["by_layer"].items()):
            print(f"    {layer:16s} {row['total']:4d} discovered")
        for p in result["problems"]:
            print(f"    {p}")
        print("MUTATION DISCOVERY:", "ok" if not result["problems"] else "gaps")
    # GATE 4N-I26C, closing the ARCH-M1 half of I26B-05. The exit code used to depend on an
    # OPT-IN flag: problems were computed, printed, and then discarded unless the caller
    # remembered to ask for them. A guard whose default is to report and exit 0 is graded in CI
    # for nothing. The default now FAILS on any problem; the flag is retained only as an
    # explicit no-op alias so an existing invocation keeps working and means what it says.
    return 1 if result["problems"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
