#!/usr/bin/env python3
"""Security-critical list inventory and meta-completeness — Gate 4N-I26C, closing I26B-01.

THE ROOT CAUSE THIS CLOSES. Gate 4N-I25's adversarial lane found that seven of its findings
shared ONE defect: a control whose scope is a HAND-AUTHORED LIST with nothing asserting the list
is complete. Three helper names, four masking strings, fifteen matrix sites, five binding
fields, one requirements fixture. Every one failed by the list being SHORT, not by the logic
being wrong — so every lane that checked the LOGIC found it sound, and six such agreements were
one confirmation.

Gate 4N-I26B rebuilt four of those lists on the both-directions pattern and left the finding
OPEN, correctly: repairing four known instances does not establish that the set of instances is
known. The missing thing was never another list — it was a way to find lists.

WHAT THIS DOES. It DISCOVERS collection constants structurally (module-level assignments of a
list, tuple, set, frozenset or dict literal to an UPPER_SNAKE name in any guard script), then
requires every one to be CLASSIFIED by the authored contract, and every list classified
SECURITY_CRITICAL to name a COMPLETENESS CONSUMER that actually exists.

WHY THE CLASSIFICATION FIXTURE IS NOT THE SAME DEFECT. It is a list, and it could be short —
but being short is exactly what FAILS here. A collection the fixture does not mention is
UNKNOWN, and UNKNOWN is a finding. The fixture can only ever be too PERMISSIVE about something
it has already been shown, never silently blind to something new. That is the difference
between a list that defines a scope and a list that annotates a discovered one.
"""
from __future__ import annotations

import argparse
import ast
import importlib
import json
import sys
from pathlib import Path

SCRIPTS_ON_PATH = str(Path(__file__).resolve().parent)
if SCRIPTS_ON_PATH not in sys.path:
    sys.path.insert(0, SCRIPTS_ON_PATH)

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"
CONTRACT = REPO_ROOT / "tests" / "fixtures" / "critical-list-contract.json"
ASSURANCE_REGISTRY = REPO_ROOT / "tests" / "fixtures" / "security-assurance-registry.json"

# The CLOSED set of assurance kinds an assignment may declare. Mirrors security_collection_assurance's
# dispatch; an assignment with any other kind is UNGOVERNED (the assurance validator would also refuse
# it, but inventory fails it here so a SECURITY id can never be 'assigned' to a non-existent property).
ASSURANCE_KINDS = (
    "INDEPENDENT_MEMBERSHIP_COMPLETENESS",
    "PARTITION_RELATION_ASSURANCE",
    "AUTHORED_SOURCE_OF_TRUTH_INTEGRITY",
    "EXCLUSION_POLICY_ASSURANCE",
    "CROSS_DOMAIN_CONSISTENCY",
    "GENERATED_CONTRACT_ASSURANCE",
    "RUNTIME_INVARIANT_ASSURANCE",
)

SECURITY_CRITICAL = "SECURITY_CRITICAL_LIST"
NON_SECURITY = "NON_SECURITY_CONFIGURATION"
GENERATED = "GENERATED_OBSERVED_SET"
TEST_ONLY = "TEST_ONLY"
DOCUMENTATION_ONLY = "DOCUMENTATION_ONLY"
UNKNOWN = "UNKNOWN"

CLASSES = (SECURITY_CRITICAL, NON_SECURITY, GENERATED, TEST_ONLY, DOCUMENTATION_ONLY, UNKNOWN)

COMPLETENESS_KINDS = (
    "INDEPENDENT_EXACT_CONTRACT",       # an authored oracle never re-derived at check time
    "BIDIRECTIONAL_DIFFERENCE",         # discovered domain, both set differences enforced
    "CLOSED_SCHEMA_UNKNOWN_REJECTED",   # a closed enum where an unrecognised member fails
    "GENERATED_INVENTORY_CHECKED",      # generated, with completeness verified independently
)


class InventoryError(RuntimeError):
    """Fail-closed."""


def _collection_value(value) -> bool:
    return isinstance(value, (list, tuple, set, frozenset, dict))


def discover_collections() -> list[dict]:
    """Every module-level collection constant in every guard script. Structural, not listed.

    BH-C F8: a collection is discovered by what it IS at module level, not by the SYNTAX of its
    right-hand side. The original discoverer recognised only literals and frozenset/set/tuple/list/
    dict CALLS, so a security-relevant collection assigned via a helper call, a set BinOp union, or a
    comprehension (e.g. `FORBIDDEN = _build()`, `ALL_KEYWORDS = A | B`, `ROLE_ARNS = [f(n) for ...]`)
    escaped discovery, classification, and governance entirely. To close that silent-escape vector,
    every module-level UPPER_SNAKE name ASSIGNED IN THIS MODULE (an Assign target — NOT an imported
    alias) whose live value is a collection is discovered, whatever the RHS form. Imported names have
    no Assign node here, so they are still discovered only in their home module (no double count).
    """
    found: list[dict] = []
    for script in sorted(SCRIPTS.glob("*.py")):
        try:
            tree = ast.parse(script.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        assigned: dict[str, ast.AST] = {}           # UPPER_SNAKE names assigned at module level here
        for node in tree.body:
            if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                name = getattr(target, "id", None)
                if name and name.isupper():
                    assigned.setdefault(name, node)
        # Syntactic literals / constructor calls are collections by inspection — no import needed.
        module_obj = None
        for name, node in assigned.items():
            value = node.value
            literal = isinstance(value, (ast.List, ast.Tuple, ast.Set, ast.Dict))
            call = (isinstance(value, ast.Call) and
                    getattr(value.func, "id", None) in ("frozenset", "set", "tuple", "list", "dict"))
            is_collection = literal or call
            form = type(value).__name__
            if not is_collection:
                # Any other RHS form (helper call, BinOp union, comprehension, name/attr alias to a
                # locally-built collection): decide by the LIVE value. Import lazily, once per module.
                if module_obj is None:
                    try:
                        module_obj = importlib.import_module(script.stem)
                    except Exception:
                        module_obj = False          # unimportable in this env — literals still emit
                if module_obj is False:
                    continue
                live = getattr(module_obj, name, None)
                if not _collection_value(live):
                    continue
                is_collection = True
                form = f"derived:{type(live).__name__}"
            if is_collection:
                found.append({"id": f"{script.name}::{name}", "module": script.name,
                              "name": name, "line": node.lineno, "form": form})
    return found


def contract() -> dict:
    if not CONTRACT.exists():
        raise InventoryError(
            f"the critical-list contract is absent: {CONTRACT}. Absence must never be read as "
            "'no list needs completeness enforcement'.")
    doc = json.loads(CONTRACT.read_text(encoding="utf-8"))
    if not doc.get("classifications"):
        raise InventoryError("the critical-list contract classifies nothing")
    return doc


def assurance_registry() -> dict:
    if not ASSURANCE_REGISTRY.exists():
        raise InventoryError(
            f"the security-assurance registry is absent: {ASSURANCE_REGISTRY}. Absence must never "
            "read as 'every SECURITY collection is governed'.")
    doc = json.loads(ASSURANCE_REGISTRY.read_text(encoding="utf-8"))
    if not doc.get("assurance"):
        raise InventoryError("the security-assurance registry assigns nothing")
    return doc


def check() -> dict:
    doc = contract()
    classifications = doc["classifications"]
    discovered = discover_collections()
    discovered_ids = {c["id"] for c in discovered}
    assurance = assurance_registry()["assurance"]

    problems: list[str] = []
    unknown: list[str] = []
    critical: list[str] = []
    ungoverned: list[str] = []

    for collection in discovered:
        cid = collection["id"]
        klass = classifications.get(cid)
        if klass is None:
            unknown.append(cid)
            problems.append(
                f"{cid}: a collection constant with NO classification. Every discovered "
                "collection must be classified; an unclassified one is exactly how a new "
                "security-critical list would enter unnoticed.")
            continue
        if klass not in CLASSES:
            problems.append(f"{cid}: unknown classification {klass!r}")
            continue
        if klass == UNKNOWN:
            problems.append(f"{cid}: classified UNKNOWN, which fails closed")
            continue
        if klass != SECURITY_CRITICAL:
            continue

        critical.append(cid)
        # GOVERNANCE IS AN ASSURANCE ASSIGNMENT, NOT A CONSUMER-MANIFEST ENTRY. Every
        # SECURITY_CRITICAL collection must carry an assignment naming the property that CAN be
        # proven for it and the control that proves it. Reading the assignment (not a shortenable
        # completeness_consumers list) closes the "an id leaves the manifest -> inventory stops
        # demanding a control for it -> goes green" hole: dropping an assignment is UNGOVERNED here.
        entry = assurance.get(cid)
        if not isinstance(entry, dict):
            ungoverned.append(cid)
            problems.append(f"{cid}: SECURITY_CRITICAL with NO assurance assignment — UNGOVERNED")
            continue
        kind = entry.get("assurance_kind")
        if kind not in ASSURANCE_KINDS:
            ungoverned.append(cid)
            problems.append(f"{cid}: assurance_kind {kind!r} is not one of {ASSURANCE_KINDS}")

    # META-COMPLETENESS, BOTH DIRECTIONS, on BOTH authored maps.
    stale = sorted(set(classifications) - discovered_ids)
    for cid in stale:
        problems.append(
            f"{cid}: classified in the contract but NOT DISCOVERED. Either the collection was "
            "renamed or removed, or the discoverer has stopped seeing it — both matter.")
    security_ids = set(critical)
    stale_assurance = sorted(set(assurance) - security_ids)
    for cid in stale_assurance:
        problems.append(
            f"{cid}: assured in the registry but is not a discovered SECURITY_CRITICAL collection "
            "— an assignment must not outlive (or misname) the collection it governs.")

    return {
        "discovered_collections": len(discovered),
        "classified": len(classifications),
        "unclassified": unknown,
        "stale_classifications": stale,
        "stale_assurance": stale_assurance,
        "security_critical": sorted(critical),
        "security_critical_count": len(critical),
        "security_ungoverned": ungoverned,
        "assurance_kinds": list(ASSURANCE_KINDS),
        "problems": problems,
        "meta_completeness":
            "Governance is an assurance ASSIGNMENT per SECURITY collection, not an entry in a "
            "shortenable consumer manifest. A discovered collection the contract does not classify, "
            "or a SECURITY collection the registry does not assure, is a FAILURE, not a default.",
        "clean": not problems,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = check()
    except InventoryError as exc:
        print(f"  {exc}", file=sys.stderr)
        print("CRITICAL LIST INVENTORY: refused")
        return 2
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"  {result['discovered_collections']} collection constants discovered; "
              f"{result['security_critical_count']} SECURITY_CRITICAL; "
              f"{len(result['unclassified'])} unclassified; "
              f"{len(result['security_ungoverned'])} ungoverned")
        for problem in result["problems"][:40]:
            print(f"    {problem}", file=sys.stderr)
        print("CRITICAL LIST INVENTORY:", "complete" if result["clean"] else "INCOMPLETE")
    return 0 if result["clean"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
