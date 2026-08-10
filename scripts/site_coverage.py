#!/usr/bin/env python3
"""Executed site coverage — Gate 4N-I24D, closing I24C-12.

WHY THIS IS SEPARATE FROM DISCOVERY.

`scripts/mutation_discovery.py` answers "what are the load-bearing sites?" by reading AST call
graphs, the workflow graded-step graph and authored contract key sets. It must never also
answer "are they covered?", because a module that decides both questions can satisfy itself.
That is the shared-ancestor defect this chain has removed from the reconciler (I23 blocker 2)
and from package completeness (I23 X4), and it would be the same defect here.

WHY NAME MATCHING IS NOT COVERAGE. An earlier draft of this gate credited a site when its name
appeared anywhere in a test file. Under that rule the fourteen requirement keys went from
"untested" to "covered" the moment a dictionary listing their names was added — no behaviour
was proven, and a no-op assertion carrying the key name would have scored identically. Names,
strings, imports, documentation, dead code and test collection are all explicitly worthless
here.

WHAT COUNTS. A site is covered only when an EXECUTED mutation of its load-bearing value made
the SHIPPING guard exit non-zero, attributably. This module runs those mutations itself, in
place, restoring each fixture byte-exactly and verifying the restored digest, and reports the
result. Coverage is therefore an executed fact, not a claim about the shape of the test suite.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "tests" / "fixtures"
MATRIX = FIXTURES / "site-coverage-matrix.json"

ENV = {**os.environ, "SIGNALNEST_ANCHOR_TIER": "TIER_1_SYNTHETIC"}

CAUGHT = "CAUGHT_BY_INTENDED_CONTROL"
RELATED = "CAUGHT_BY_VALID_RELATED_CONTROL"
MASKED = "MASKED_BY_UNRELATED_FAILURE"
SURVIVED = "SURVIVED_REAL_GAP"
INVALID = "INVALID_MUTATION"
COVERING = (CAUGHT, RELATED)


class CoverageError(RuntimeError):
    """Fail-closed."""


def matrix() -> dict:
    if not MATRIX.exists():
        raise CoverageError(
            f"the authored site-coverage matrix is absent: {MATRIX}. Absence must never be "
            "read as 'every site is covered'.")
    doc = json.loads(MATRIX.read_text(encoding="utf-8"))
    if not doc.get("sites"):
        raise CoverageError("the coverage matrix declares no sites")
    return doc


def _guard(script: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, f"scripts/{script}"], cwd=REPO_ROOT,
                          capture_output=True, text=True, env=ENV)


def _set_key(doc: dict, key: str, value):
    if value == "__DELETE__":
        doc.pop(key, None)
    else:
        doc[key] = value


def run_site(site_id: str, spec: dict) -> dict:
    """Execute one mutation against the real fixture and the real guard, then restore."""
    fixture = FIXTURES / spec["fixture"]
    guard = spec["guard"]
    original = fixture.read_bytes()
    digest = hashlib.sha256(original).hexdigest()

    baseline = _guard(guard)
    if baseline.returncode != 0:
        return {"site": site_id, "result": INVALID,
                "why": f"{guard} already fails before mutation; a result here would be masked",
                "baseline_exit": baseline.returncode}
    try:
        doc = json.loads(original)
        _set_key(doc, spec["key"], spec["mutate_to"])
        fixture.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        proc = _guard(guard)
        output = proc.stdout + proc.stderr
    finally:
        fixture.write_bytes(original)
        if hashlib.sha256(fixture.read_bytes()).hexdigest() != digest:
            raise CoverageError(f"{fixture} was not restored byte-exactly")

    if proc.returncode == 0:
        return {"site": site_id, "result": SURVIVED, "guard": guard, "exit": 0}
    token = spec.get("attributable_token")
    if token and token not in output:
        return {"site": site_id, "result": RELATED, "guard": guard, "exit": proc.returncode,
                "why": f"guard refused but did not name {token!r}"}
    return {"site": site_id, "result": CAUGHT, "guard": guard, "exit": proc.returncode}


def covered_sites() -> dict:
    """Sites proven covered BY EXECUTION. Never by name, membership or assertion text."""
    doc = matrix()
    results = {sid: run_site(sid, spec) for sid, spec in sorted(doc["sites"].items())}
    covered = {sid for sid, r in results.items() if r["result"] in COVERING}
    return {"results": results, "covered": covered}


def check() -> dict:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import mutation_discovery

    discovered = {s["id"]: s for s in mutation_discovery.discover_sites()}
    executed = covered_sites()
    covered = executed["covered"]

    # GATE 4N-I26B, closing the ARCH-H1 half of I26B-05.
    #
    # The previous version computed `in_matrix - discovered` and `in_matrix - covered` and
    # NEVER `discovered - in_matrix`, so the matrix defined its own denominator: it adjudicated
    # fifteen sites, every one passed, and the module printed "SITE COVERAGE: proven" while a
    # hundred and seventy discovered sites were never considered. A set difference computed in
    # one direction only cannot see what is missing — that is arithmetic, not policy.
    #
    # Both directions are computed here and BOTH are enforced. Identifiers are compared under
    # the single canonical vocabulary in tests/fixtures/site-identifier-contract.json; the
    # matrix and any parallel enumeration previously used forms that shared ZERO identifiers,
    # so no comparison between them was even possible.
    in_matrix = set(matrix()["sites"])
    discovered_ids = set(discovered)
    unknown = sorted(in_matrix - discovered_ids)
    missing_from_matrix = sorted(discovered_ids - in_matrix)
    uncovered = sorted(in_matrix - covered)
    matrix_ids = list(matrix()["sites"])
    duplicates = sorted({i for i in matrix_ids if matrix_ids.count(i) > 1})
    survived = [r for r in executed["results"].values() if r["result"] == SURVIVED]
    invalid = [r for r in executed["results"].values() if r["result"] == INVALID]

    problems = []
    for s in uncovered:
        problems.append(f"{s}: no EXECUTED mutation proved it load-bearing")
    for s in unknown:
        problems.append(f"{s}: the matrix names a site discovery does not know")
    for s in missing_from_matrix:
        problems.append(f"{s}: DISCOVERED as load-bearing but absent from the matrix, so no "
                        "executed mutation has ever proved it")
    for s in duplicates:
        problems.append(f"{s}: duplicate canonical id in the matrix")
    for r in invalid:
        problems.append(f"{r['site']}: {r['why']}")

    return {"discovered_total": len(discovered), "matrix_sites": len(in_matrix),
            "missing_from_matrix": missing_from_matrix,
            "missing_from_matrix_count": len(missing_from_matrix),
            "unknown_in_matrix": unknown, "duplicates": duplicates,
            "executed_covered": len(covered), "survived_real_gap": len(survived),
            "invalid": len(invalid), "unknown": len(unknown),
            "results": executed["results"], "problems": problems,
            "coverage_definition": "an EXECUTED mutation made the shipping guard exit "
                                   "non-zero; names, strings and set membership never count",
            "clean": not problems}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    result = check()
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"  matrix sites {result['matrix_sites']}; EXECUTED-covered "
              f"{result['executed_covered']}; survived {result['survived_real_gap']}; "
              f"invalid {result['invalid']}")
        for sid, r in sorted(result["results"].items()):
            print(f"    {r['result']:32s} {sid}")
        for p in result["problems"]:
            print(f"    {p}", file=sys.stderr)
        print("SITE COVERAGE:", "proven" if result["clean"] else "GAPS")
    return 0 if result["clean"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
