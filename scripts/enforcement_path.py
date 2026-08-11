#!/usr/bin/env python3
"""Which controls are load-bearing, derived from INVOCATION rather than from their names.

THE DEFECT THIS CLOSES — Gate 4N-I28G finding ADV-03, canonical root cause RC-3.

`tests/fixtures/critical-list-contract.json` states its rule in one sentence:

    SECURITY_CRITICAL when a module-level constant in a guard script names a SCOPE the guard
    enforces (allowed / required / expected / denied / permitted / covered).

That is a rule about the NAME. Applied to `scripts/leak_scan.py` it produced two inversions at once:

    SKIP_DIRS            graded NON_SECURITY_CONFIGURATION, reason "a fixed configuration value
                         or literal set, not an enforced scope" — while `candidate_files()` uses
                         it to decide which files are scanned AT ALL, before anything else runs
    SCAN_SUFFIXES        graded SECURITY_CRITICAL_LIST, reason "name denotes a SCOPE the guard
                         enforces" — while nothing in the decision path references it; its own
                         comment says it is NO LONGER THE INCLUSION RULE
    EXCLUDED_PATH_PARTS  absent from the contract entirely, while `is_scannable()` enforces it

Because `SKIP_DIRS` was graded non-security, nobody pinned it — and Gate 4N-I28G showed a single
line added to it makes 80 files vanish from the scan with a planted identifier inside them and
zero skip-report entries. RC-3 is the architectural parent of that failure: the misclassification
is why the gap existed to be found.

WHAT THIS MODULE DOES. It answers "is this constant on a path that decides what a guard examines
or refuses?" by walking the call graph from the guard's own entry points. A constant reachable
from `main()`/`check()` through the functions that gate examination is LOAD-BEARING regardless of
what it is called; a constant nothing calls is not load-bearing however enforcing its name sounds.

WHAT THIS MODULE DOES NOT DO. It is not a general reachability prover. It resolves direct calls to
module-level functions by name — enough for these single-file guards, and deliberately not more.
An unresolvable call makes the analysis INCOMPLETE and is reported, never silently dropped.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"

#: Entry points a guard is invoked through. Reachability starts here.
ENTRY_POINTS = ("main", "check")

#: Guards whose enforcement path decides WHAT IS EXAMINED. A constant reachable from one of these
#: gates coverage itself, which is the class of defect RC-2 and RC-3 are about.
SCAN_DOMAIN_GATES = {
    # `scan_decision` is where Gate 4N-I28I RC-2 consolidated the filter chain, so it is a gate
    # by construction: it is the single function that decides whether a discovered path is
    # examined. Omitting it here would have reproduced the very defect this module exists to
    # catch — the derivation would have reported SKIP_DIRS as no longer load-bearing simply
    # because the code moved.
    "leak_scan.py": ("candidate_files", "scan_decision", "is_scannable", "scan_text",
                     "scan_repository", "scan_accounting"),
}


def _module_functions(tree: ast.Module) -> dict[str, ast.FunctionDef]:
    return {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}


def _called_names(fn: ast.FunctionDef) -> set[str]:
    """Direct calls by bare name. `ls.foo()` is an attribute call and is not resolved here."""
    return {c.func.id for c in ast.walk(fn)
            if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}


def _unresolvable_calls(fn: ast.FunctionDef, known: set[str]) -> set[str]:
    """Attribute calls that COULD be a guard function this analysis failed to follow.

    `path.read_text()` and `', '.join(...)` are stdlib method calls on objects — they cannot
    reach another function in this module, so counting them as unresolved would make the
    completeness signal meaningless noise. What matters is an attribute call whose final name
    matches a function defined here (`ls.scan_text()`, `self.check()`), because that IS a hop the
    name-based walk missed. Reported, so the analysis never claims a completeness it lacks.
    """
    suspicious = set()
    for call in ast.walk(fn):
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute):
            if call.func.attr in known:
                suspicious.add(ast.unparse(call.func))
    return suspicious


def reachable_functions(module: str) -> dict:
    """Every function reachable from the guard's entry points, plus what could not be resolved."""
    path = SCRIPTS / module
    tree = ast.parse(path.read_text(encoding="utf-8"))
    functions = _module_functions(tree)
    seen: set[str] = set()
    frontier = [e for e in ENTRY_POINTS if e in functions]
    unresolved: set[str] = set()
    while frontier:
        name = frontier.pop()
        if name in seen or name not in functions:
            continue
        seen.add(name)
        called = _called_names(functions[name])
        unresolved |= _unresolvable_calls(functions[name], set(functions))
        frontier.extend(c for c in called if c in functions and c not in seen)
    return {"module": module, "entry_points": [e for e in ENTRY_POINTS if e in functions],
            "reachable": sorted(seen), "defined": sorted(functions),
            "unreachable": sorted(set(functions) - seen),
            "unresolved_calls": sorted(unresolved),
            "analysis_complete": not unresolved}


def constant_references(module: str) -> dict[str, list[str]]:
    """Which functions reference each module-level UPPER_CASE collection constant."""
    path = SCRIPTS / module
    tree = ast.parse(path.read_text(encoding="utf-8"))
    constants = set()
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                name = getattr(t, "id", None)
                if name and name.isupper():
                    constants.add(name)
    refs: dict[str, list[str]] = {c: [] for c in constants}
    for fn in _module_functions(tree).values():
        used = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
        for c in constants & used:
            refs[c].append(fn.name)
    return {c: sorted(v) for c, v in sorted(refs.items())}


def enforcement_inventory(module: str) -> dict:
    """The load-bearing record for one guard, derived from invocation and effect.

    `load_bearing` means: referenced by a function that is REACHABLE from an entry point AND
    that gates what the guard examines or refuses. `dead` means referenced by nothing at all —
    such a constant cannot be security-critical no matter how its name reads.
    """
    reach = reachable_functions(module)
    refs = constant_references(module)
    gates = set(SCAN_DOMAIN_GATES.get(module, ()))
    records = {}
    for const, callers in refs.items():
        reachable_callers = [c for c in callers if c in reach["reachable"]]
        gating_callers = [c for c in reachable_callers if c in gates]
        records[f"{module}::{const}"] = {
            "control_identity": f"{module}::{const}",
            "implementation_path": f"scripts/{module}",
            "symbol": const,
            "callers": callers,
            "reachable_callers": reachable_callers,
            "gating_callers": gating_callers,
            "invocation": ("reachable from " + ", ".join(reach["entry_points"])
                           if reachable_callers else "not reachable from any entry point"),
            "dead": not callers,
            "load_bearing": bool(gating_callers) if gates else bool(reachable_callers),
            "failure_consequence": ("narrows or widens what the guard examines, silently"
                                    if gating_callers else
                                    "no effect on what the guard examines"),
        }
    return {"module": module, "reachability": reach, "controls": records,
            "load_bearing": sorted(k for k, v in records.items() if v["load_bearing"]),
            "dead": sorted(k for k, v in records.items() if v["dead"])}


def check() -> dict:
    inventories = {m: enforcement_inventory(m) for m in SCAN_DOMAIN_GATES}
    incomplete = [m for m, i in inventories.items() if not i["reachability"]["analysis_complete"]]
    return {"inventories": inventories, "modules": sorted(inventories),
            "analysis_incomplete_for": incomplete,
            "clean": not incomplete,
            "derivation": "call graph from entry points; names are never consulted"}


def main() -> int:
    result = check()
    print(json.dumps(result, indent=1))
    for module, inv in result["inventories"].items():
        print(f"{module}: load-bearing {inv['load_bearing']}")
        print(f"{module}: dead {inv['dead']}")
    return 0 if result["clean"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
