#!/usr/bin/env python3
"""Completeness consumers for security-critical collections — Gate 4N-I26D, closing I26B-01.

WHAT WAS OPEN. Gate 4N-I26C built the half that was genuinely missing: a STRUCTURAL discoverer
that finds collection constants instead of starting from a list of known lists, plus a
meta-completeness guard that fails on an unclassified collection and on a classification for a
collection that no longer exists. It found 153 collections, 48 of them SECURITY_CRITICAL — and
none of the 48 had a completeness consumer. Knowing which lists could silently go short is not
the same as detecting when one does.

WHAT A COMPLETENESS CONSUMER IS. For a collection C with domain D, it computes BOTH:

    missing  = D - C      a domain member the collection does not cover
    unknown  = C - D      a collection member the domain does not contain
    duplicates            the same member twice under normalization

and returns non-zero on any. One direction is not enough and never was: `in_matrix - covered`
without `discovered - in_matrix` is exactly how site coverage adjudicated 15 of 203 sites and
printed "proven".

WHERE THE DOMAIN COMES FROM, AND WHY IT IS NOT ANOTHER COPY OF THE LIST. Each resolver derives
D from something that is NOT the collection:

    module_constants      the constants the module actually defines      (AST)
    function_result_keys  the keys the producing function actually emits (executed)
    emitted_policy        the actions the generated policy actually carries
    discovered_kinds      the kinds the discoverer actually emits        (executed)
    authored_contract     an independently authored requirement fixture, never re-derived

The first four are OBSERVED behaviour of the code the collection describes; the collection is a
DECLARATION about that behaviour. Comparing a declaration against the behaviour it declares is
a real oracle. Comparing it against a second hand-written copy of itself is not, and that
pattern is refused below rather than merely discouraged.
"""
from __future__ import annotations

import argparse
import ast
import importlib
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"
SPECS = REPO_ROOT / "tests" / "fixtures" / "collection-completeness-specs.json"

sys.path.insert(0, str(SCRIPTS))

RESOLVERS = ("module_constants", "function_result_keys", "emitted_policy",
             "discovered_kinds", "authored_contract")

def _framework_kinds() -> tuple:
    """The framework-kind resolvers, taken from completeness_framework's OWN canonical set — the
    single source of truth, so this module cannot drift out of coverage of the kinds evaluate()
    actually dispatches on. DISJOINT from RESOLVERS: a spec whose `resolver` is one of these is
    enforced through the certificate-backed completeness_framework.evaluate(), never the bare
    `[] == clean` set-difference path. Gate 4N-I28BH-B0a-SLICE2. (A lazy accessor, not a module-level
    constant, so no duplicated dispatch vocabulary appears as a discovered collection to drift.)"""
    import completeness_framework as framework
    return framework.FRAMEWORK_KINDS


class CompletenessError(RuntimeError):
    """Fail-closed."""


def _collection(module: str, name: str):
    mod = importlib.import_module(module[:-3] if module.endswith(".py") else module)
    if not hasattr(mod, name):
        raise CompletenessError(f"{module}::{name} no longer exists")
    value = getattr(mod, name)
    if isinstance(value, dict):
        return set(value)
    return set(value)


def _module_constants(module: str, pattern: str) -> set:
    """String constants the module DEFINES, by name pattern. The enum's real domain."""
    tree = ast.parse((SCRIPTS / module).read_text(encoding="utf-8"))
    found = set()
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            name = getattr(target, "id", "")
            if name and re.fullmatch(pattern, name) and isinstance(node.value, ast.Constant) \
                    and isinstance(node.value.value, str):
                found.add(node.value.value)
    return found


def _function_result_keys(module: str, function: str, call_args: list | None) -> set:
    """Keys the PRODUCER actually emits. Executed, not read off a declaration."""
    mod = importlib.import_module(module[:-3] if module.endswith(".py") else module)
    result = getattr(mod, function)(*(call_args or []))
    if isinstance(result, dict):
        return set(result)
    return set(result)


def _emitted_policy(module: str, function: str, call_args: list | None, effect: str,
                    sids: list | None = None) -> set:
    """Actions the GENERATED policy actually carries under the given Effect.

    When `sids` is given (Gate 4N-I28BH-B-SLICE3), only statements whose Sid is in that authored set
    are counted. This scopes a PARTITION universe to the statements the named member lists were
    authored to partition. The boundary_deny lists partition the six flat, Resource:"*" admin-Deny
    statements' 78 actions; the resource-scoped INLINE denies that later gates added (KMS/PassRole/
    RunTask/RDS/Secrets/Terraform-state, 62 actions) are still denied but were never claimed by any
    named list, so the correct partition universe is the six responsible Sids, not the whole deny set.
    The Sid list is authored in the spec (independent of the member lists) and the actions are OBSERVED
    from the emitted policy, so this is non-circular; a dishonest narrowing would drop a claimed member
    and surface as `unknown`."""
    mod = importlib.import_module(module[:-3] if module.endswith(".py") else module)
    document = getattr(mod, function)(*(call_args or []))
    actions: set = set()
    for statement in document.get("Statement", []):
        if statement.get("Effect") != effect:
            continue
        if sids is not None and statement.get("Sid") not in sids:
            continue
        value = statement.get("Action") or statement.get("NotAction") or []
        actions |= {value} if isinstance(value, str) else set(value)
    return actions


def _discovered_kinds(module: str, function: str, field: str) -> set:
    mod = importlib.import_module(module[:-3] if module.endswith(".py") else module)
    return {row[field] for row in getattr(mod, function)()}


def _authored_contract(path: str, pointer: str) -> set:
    doc = json.loads((REPO_ROOT / path).read_text(encoding="utf-8"))
    for part in pointer.split("."):
        if part:
            doc = doc[part]
    return set(doc)


def resolve_domain(spec: dict) -> set:
    kind = spec["resolver"]
    if kind == "module_constants":
        return _module_constants(spec["module"], spec["name_pattern"])
    if kind == "function_result_keys":
        return _function_result_keys(spec["module"], spec["function"], spec.get("args"))
    if kind == "emitted_policy":
        return _emitted_policy(spec["module"], spec["function"], spec.get("args"),
                               spec.get("effect", "Deny"), spec.get("partition_universe_sids"))
    if kind == "discovered_kinds":
        return _discovered_kinds(spec["module"], spec["function"], spec["field"])
    if kind == "authored_contract":
        return _authored_contract(spec["path"], spec.get("pointer", ""))
    raise CompletenessError(f"unknown resolver {kind!r}; unknown fails closed")


def specs() -> dict:
    if not SPECS.exists():
        raise CompletenessError(
            f"the completeness specs are absent: {SPECS}. Absence must never be read as "
            "'no collection needs a completeness consumer'.")
    return json.loads(SPECS.read_text(encoding="utf-8"))["specs"]


def _framework_check(cid: str, spec: dict, module: str, name: str, row: dict,
                     problems: list) -> None:
    """Enforce a FRAMEWORK-KIND completeness spec through the certificate-backed framework.

    Authority is obtained from exactly ONE governed entry point — completeness_framework.evaluate() —
    and the CLEAN decision from exactly ONE predicate — is_complete(). No public helper
    (resolve_witness_fields / p9_execute_witness / a top-level compare) is called or composed here, so
    a caller can never assemble authority outside a governed evaluate() transit (gate §7).

    A CLEAN verdict is authoritative ONLY when the framework's issuer bound a certificate to it for
    THIS collection under THIS relation. A bare []/{}/None — or any empty list the sink never handed
    out — is NON-authoritative and fails closed (UNCERTIFIED). `row`/`problems` mutated in place.
    """
    import completeness_framework as framework

    try:
        observed = _collection(module, name)
    except Exception as exc:            # a collection that will not load is a finding, not a pass
        problems.append(f"{cid}: collection could not be loaded: {type(exc).__name__}: {exc}")
        row["result"] = "UNRESOLVED"
        return

    relation = spec.get("relation")
    verdict = framework.evaluate(spec, observed, cid)          # THE governed entry point
    row["domain_class"] = spec.get("domain_class")

    if verdict:                          # a non-empty verdict is a findings/refusal list: INCOMPLETE
        for problem in verdict:
            problems.append(f"{cid}: {problem}")
        row["result"] = "INCOMPLETE"
        return

    # An EMPTY verdict is load-bearing ONLY if the sink minted a certificate for THIS claim.
    # is_complete() reads the issuer registry; a bare [] reads False and is refused below. The
    # non-enumerable kind binds to a SELECTION, so it is checked with is_complete_for_selection.
    if relation is not None:
        if spec["resolver"] == "authoritative_source_no_enumerable_oracle":
            authoritative = framework.is_complete_for_selection(
                verdict, cid, framework._default_source_loader, relation)
        else:
            authoritative = framework.is_complete(verdict, observed, relation)
        if authoritative:
            row["result"] = "COMPLETE"
            return

    # Empty but NOT certificate-backed. This is exactly the `[] == clean` defect the framework
    # redesign removed authority from; here it fails closed rather than being read as proof.
    problems.append(f"{cid}: framework returned an empty verdict with NO authoritative "
                    "completeness certificate for this collection under relation "
                    f"{relation!r}; a bare [] is not proof of completeness — REFUSED")
    row["result"] = "UNCERTIFIED"


def check() -> dict:
    import critical_list_inventory as inventory

    contract = inventory.contract()
    critical = {cid for cid, klass in contract["classifications"].items()
                if klass == inventory.SECURITY_CRITICAL}
    declared = specs()

    problems: list[str] = []
    rows: list[dict] = []

    # META-COMPLETENESS OF THE CONSUMERS THEMSELVES, both directions.
    for cid in sorted(critical - set(declared)):
        problems.append(f"{cid}: SECURITY_CRITICAL with NO completeness consumer")
    for cid in sorted(set(declared) - critical):
        problems.append(f"{cid}: a consumer is declared for a collection that is not "
                        "SECURITY_CRITICAL (or no longer exists)")

    # PARTITIONS. Seven of the boundary deny lists are each a SUBSET of what the emitted policy
    # denies — no single one can equal the domain, and comparing each against the whole reports
    # false "missing" sets. What is actually true, and what the boundary depends on, is that their
    # UNION is exactly the emitted deny set OF THE SIX FLAT ADMIN-DENY STATEMENTS the lists partition
    # (78 actions): nothing in that sub-domain denied that no list claims, and nothing claimed that
    # the policy does not deny. Checked at the union, both directions. GATE 4N-I28BH-B-SLICE3: the
    # universe is scoped by each spec's authored `partition_universe_sids` (the six responsible Sids)
    # rather than the whole deny set, because later gates (I8-I11+) added resource-scoped INLINE denies
    # (KMS/PassRole/RunTask/RDS/Secrets/Terraform-state, 62 actions) that ARE denied but were never
    # claimed by any named list — the earlier comment's "the whole emitted deny set" became stale then.
    groups: dict[str, list[str]] = {}
    for cid, spec in declared.items():
        if spec.get("partition_group"):
            groups.setdefault(spec["partition_group"], []).append(cid)
    partitioned = {cid for members in groups.values() for cid in members}

    for group, members in sorted(groups.items()):
        spec = declared[members[0]]
        try:
            domain = resolve_domain(spec)
            union: set = set()
            overlaps = []
            for cid in sorted(members):
                module, name = cid.split("::", 1)
                part = _collection(module, name)
                for other in union & part:
                    overlaps.append(f"{other!r} appears in more than one member of {group}")
                union |= part
        except Exception as exc:
            problems.append(f"{group}: partition domain could not be resolved: {exc}")
            continue
        missing = sorted(domain - union)
        unknown = sorted(union - domain)
        for member in missing:
            problems.append(f"{group}: the emitted policy denies {member!r} but NO member list "
                            "claims it — the partition is short")
        for member in unknown:
            problems.append(f"{group}: member list claims {member!r} which the emitted policy "
                            "does not deny")
        problems.extend(overlaps)
        rows.append({"collection": f"PARTITION::{group}", "members": sorted(members),
                     "resolver": spec["resolver"], "domain_class": spec.get("domain_class"),
                     "domain_size": len(domain), "collection_size": len(union),
                     "missing": missing, "unknown": unknown, "duplicates": overlaps,
                     "result": "COMPLETE" if not (missing or unknown or overlaps)
                               else "INCOMPLETE"})

    for cid in sorted((critical & set(declared)) - partitioned):
        spec = declared[cid]
        module, name = cid.split("::", 1)
        row = {"collection": cid, "resolver": spec["resolver"],
               "domain_class": spec.get("domain_class")}
        # FRAMEWORK-KIND specs are enforced through the certificate-backed framework, never the
        # bare-[] set-difference path below. Must sit BEFORE the RESOLVERS refusal.
        if spec["resolver"] in _framework_kinds():
            _framework_check(cid, spec, module, name, row, problems)
            rows.append(row)
            continue
        if spec["resolver"] not in RESOLVERS:
            problems.append(f"{cid}: unknown resolver {spec['resolver']!r}")
            row["result"] = "UNKNOWN_RESOLVER"
            rows.append(row)
            continue
        try:
            observed = _collection(module, name)
            domain = resolve_domain(spec)
        except Exception as exc:                       # a resolver that cannot run is a finding
            problems.append(f"{cid}: domain could not be resolved: {type(exc).__name__}: {exc}")
            row["result"] = "UNRESOLVED"
            rows.append(row)
            continue

        missing = sorted(domain - observed)
        unknown = sorted(observed - domain)
        row.update({"domain_size": len(domain), "collection_size": len(observed),
                    "missing": missing, "unknown": unknown})
        for member in missing:
            problems.append(f"{cid}: domain member {member!r} is NOT in the collection")
        for member in unknown:
            problems.append(f"{cid}: collection member {member!r} is NOT in the domain")
        row["result"] = "COMPLETE" if not missing and not unknown else "INCOMPLETE"
        rows.append(row)

    return {"security_critical": len(critical), "with_consumer": len(critical & set(declared)),
            "without_consumer": sorted(critical - set(declared)),
            "orphan_consumers": sorted(set(declared) - critical),
            "rows": rows, "problems": problems,
            "both_directions": "missing = domain - collection; unknown = collection - domain",
            "clean": not problems}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = check()
    except CompletenessError as exc:
        print(f"  {exc}", file=sys.stderr)
        print("COLLECTION COMPLETENESS: refused")
        return 2
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"  {result['security_critical']} security-critical collections; "
              f"{result['with_consumer']} with a completeness consumer; "
              f"{len(result['without_consumer'])} without")
        for problem in result["problems"][:30]:
            print(f"    {problem}", file=sys.stderr)
        print("COLLECTION COMPLETENESS:", "complete" if result["clean"] else "INCOMPLETE")
    return 0 if result["clean"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
