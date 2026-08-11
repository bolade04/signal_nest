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

# The executed mutations must be DETERMINISTIC regardless of the ambient environment the coverage
# guard runs under (a developer shell, CI's job env, or ci_harness's restricted CI-equivalent env).
# So run_site pins the two inputs its guards need to fixed, absolute, in-repo values — the synthetic
# tier and this repository's candidate manifest — exactly as CI supplies them, rather than inheriting
# an unresolved `${{ github.workspace }}` expression. GATE 4N-I28BH-E2: without the manifest pin the
# candidate_manifest.py-guarded matrix entries fail their baseline under ci_harness's restricted env.
ENV = {**os.environ, "SIGNALNEST_ANCHOR_TIER": "TIER_1_SYNTHETIC",
       "SIGNALNEST_CANDIDATE_MANIFEST": str(FIXTURES / "candidate-manifest.json")}

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


# GATE 4N-I28BH-E2. The site universe has THREE kinds, and one mutation mechanism cannot cover
# them all. Discovery (mutation_discovery) was widened at I28K to function + graded_step +
# requirement_key sites, but coverage kept a single requirement_key-only instrument (run_site
# mutates a JSON fixture key). A function or a workflow step has no fixture key to mutate, so the
# flat `discovered - in_matrix` rule reported ~830 structurally-uncoverable "gaps". check() now
# DISPATCHES OVER A CLOSED KIND SET and routes each kind to the coverage authority appropriate to
# it — the requirement_key executed matrix, the ci-invocation-contract for graded steps, and the
# governed per-module function-assurance registry (each module bound to an INDEPENDENT detector
# whose neutralization was executed-proven to drive a graded control non-zero). An unknown/
# malformed kind FAILS CLOSED; there is no default route. Discovery is NOT coverage: this module
# never certifies a site from mutation_discovery alone.
EXCLUSIONS = FIXTURES / "site-coverage-requirement-exclusions.json"
FUNCTION_ASSURANCE = FIXTURES / "site-coverage-function-assurance.json"
REVIEW_LEDGER = FIXTURES / "review-record-ledger.json"


def _canonical_digest(path: Path) -> str:
    doc = json.loads(path.read_text(encoding="utf-8"))
    return "sha256:" + hashlib.sha256(
        json.dumps(doc, separators=(",", ":"), sort_keys=True, ensure_ascii=True)
        .encode("utf-8")).hexdigest()


def _registry_governance_problems() -> list:
    """Fail closed unless each site_coverage governance registry matches its ledger-pinned digest.

    GATE 4N-I28BH-E2. The governed set is derived DIRECTLY from the registry paths this module
    consumes — the executed matrix, the requirement_key exclusion registry, and the function-
    assurance registry — so a governance list can never diverge from what is actually read. Each is
    pinned by its canonical digest in the human-reviewed review-record ledger: a content edit (a
    fabricated exclusion, a module marked ASSURED without a real detector, a matrix guard swapped for
    a weaker one) moves the digest and fails closed until the ledger is re-reviewed. The matrix is
    additionally re-EXECUTED every run, so a weakened matrix mutation is caught behaviourally too.
    """
    if not REVIEW_LEDGER.exists():
        return [f"the review-record ledger is absent: {REVIEW_LEDGER}; the governance registries "
                "would be ungoverned roots"]
    governed = json.loads(REVIEW_LEDGER.read_text(encoding="utf-8")).get("governed_files")
    if not isinstance(governed, dict):
        return ["the review-record ledger carries no governed_files digests"]
    problems = []
    for path in (MATRIX, EXCLUSIONS, FUNCTION_ASSURANCE):
        name = path.name
        if name not in governed:
            problems.append(f"root-of-trust: {name} is not pinned in the ledger governed_files — a "
                            "site_coverage governance map must not be an ungoverned root")
            continue
        current = _canonical_digest(FIXTURES / name)
        if governed[name] != current:
            problems.append(f"root-of-trust: {name} canonical digest {current} != ledger-pinned "
                            f"{governed[name]}; a change to a site_coverage governance map must be "
                            "re-reviewed in the ledger before it is trusted")
    return problems


def requirement_key_exclusions() -> dict:
    if not EXCLUSIONS.exists():
        raise CoverageError(f"the governed requirement_key exclusion registry is absent: {EXCLUSIONS}. "
                            "Absence must never be read as 'every key is coverable'.")
    doc = json.loads(EXCLUSIONS.read_text(encoding="utf-8"))
    return doc.get("exclusions") or {}


def function_assurance() -> dict:
    if not FUNCTION_ASSURANCE.exists():
        raise CoverageError(f"the governed function-assurance registry is absent: {FUNCTION_ASSURANCE}.")
    doc = json.loads(FUNCTION_ASSURANCE.read_text(encoding="utf-8"))
    return doc.get("modules") or {}


def _graded_step_authority() -> set:
    """The graded-step coverage authority — the independent CI invocation contract, NOT this module."""
    contract = json.loads((FIXTURES / "ci-invocation-contract.json").read_text(encoding="utf-8"))
    steps = contract.get("graded_steps")
    if not steps:
        raise CoverageError("the ci-invocation-contract declares no graded steps")
    return set(steps)


def _module_of(site: dict) -> str:
    """The module a function site belongs to (its taxonomy `module`, e.g. `iam_eval.py`)."""
    return site.get("module") or str(site["id"]).split("::", 1)[0]


def check() -> dict:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import mutation_discovery

    # The CLOSED site-kind dispatch domain and the closed set of accepted function-detector kinds
    # are local to this dispatch (used only here), so they are implementation vocabularies rather
    # than module-level security lists — no unknown/default route exists for either.
    known_kinds = ("requirement_key", "graded_step", "function")
    closed_detector_kinds = ("CERTIFICATE_BACKED", "GRADED_PYTEST", "E2_NEW_TEST")

    discovered = list(mutation_discovery.discover_sites())
    problems: list[str] = list(_registry_governance_problems())
    by_kind: dict[str, list[dict]] = {k: [] for k in known_kinds}
    for s in discovered:
        kind = s.get("kind")
        if not isinstance(kind, str) or kind not in known_kinds:
            problems.append(f"{s.get('id')!r}: unknown/malformed site kind {kind!r}; site_coverage "
                            "dispatches over a CLOSED kind set and fails closed — no default route")
            continue
        by_kind[kind].append(s)

    # ---- requirement_key -> executed matrix UNION governed exclusion ----
    rk_ids = {s["id"] for s in by_kind["requirement_key"]}
    matrix_ids = set(matrix()["sites"])
    excluded = set(requirement_key_exclusions())
    executed = covered_sites()
    covered = executed["covered"]
    survived = [r for r in executed["results"].values() if r["result"] == SURVIVED]
    invalid = [r for r in executed["results"].values() if r["result"] == INVALID]
    for c in sorted(matrix_ids & excluded):
        problems.append(f"{c}: a requirement_key cannot be BOTH executed-matrix-covered and governed-excluded")
    for u in sorted(rk_ids - matrix_ids - excluded):
        problems.append(f"{u}: requirement_key is neither in the executed matrix nor governed-excluded")
    for stale in sorted((matrix_ids | excluded) - rk_ids):
        problems.append(f"{stale}: named in a requirement_key registry but no longer discovered (stale)")
    for sid in sorted(matrix_ids - covered):
        problems.append(f"{sid}: matrix entry produced no EXECUTED catch")
    for r in invalid:
        problems.append(f"{r['site']}: {r['why']}")

    # ---- graded_step -> independent CI invocation contract ----
    gs_names = {s.get("name") or str(s["id"]).split("::", 1)[1] for s in by_kind["graded_step"]}
    authority = _graded_step_authority()
    for miss in sorted(gs_names - authority):
        problems.append(f"ci.yml::{miss}: graded step absent from the ci-invocation-contract authority")
    for stale in sorted(authority - gs_names):
        problems.append(f"ci.yml::{stale}: in the graded-step authority but no longer a discovered graded step (stale)")

    # ---- function -> governed per-module assurance (independent detector) ----
    assurance = function_assurance()
    fn_modules = {_module_of(s) for s in by_kind["function"]}
    for mod in sorted(fn_modules):
        entry = assurance.get(mod)
        if entry is None:
            problems.append(f"{mod}: function sites discovered but the module has NO detector in the "
                            "function-assurance registry (residual — fail closed)")
        elif entry.get("status") != "ASSURED" or entry.get("detector_kind") not in closed_detector_kinds:
            problems.append(f"{mod}: function-assurance entry is not a closed ASSURED detector "
                            f"(status={entry.get('status')!r}, kind={entry.get('detector_kind')!r})")
    for stale in sorted(set(assurance) - fn_modules):
        problems.append(f"{stale}: in the function-assurance registry but contributes no discovered function site (stale)")

    return {
        "discovered_total": len(discovered),
        "by_kind": {k: len(v) for k, v in by_kind.items()},
        "requirement_key": {"discovered": len(rk_ids), "matrix": len(matrix_ids),
                            "excluded": len(excluded), "executed_covered": len(covered),
                            "survived_real_gap": len(survived), "invalid": len(invalid)},
        "graded_step": {"discovered": len(gs_names), "authority": len(authority)},
        "function": {"discovered_modules": len(fn_modules), "assured_modules": len(assurance),
                     "sites": len(by_kind["function"])},
        "results": executed["results"], "problems": problems,
        "coverage_definition": "kind-aware: requirement_key by EXECUTED mutation of the shipping guard; "
                               "graded_step by the independent CI invocation contract; function by a "
                               "governed per-module independent detector proven load-bearing; unknown "
                               "kind fails closed. Names, strings and set membership never count.",
        "clean": not problems}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    result = check()
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        rk, gsr, fn = result["requirement_key"], result["graded_step"], result["function"]
        print(f"  requirement_key: {rk['discovered']} discovered = {rk['matrix']} matrix "
              f"(EXECUTED-covered {rk['executed_covered']}, survived {rk['survived_real_gap']}, "
              f"invalid {rk['invalid']}) + {rk['excluded']} governed-excluded")
        print(f"  graded_step: {gsr['discovered']} discovered / {gsr['authority']} contract authority")
        print(f"  function: {fn['sites']} sites across {fn['discovered_modules']} modules; "
              f"{fn['assured_modules']} assured")
        for p in result["problems"]:
            print(f"    {p}", file=sys.stderr)
        print("SITE COVERAGE:", "proven" if result["clean"] else "GAPS")
    return 0 if result["clean"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
