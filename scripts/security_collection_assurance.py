#!/usr/bin/env python3
"""SECURITY-COLLECTION ASSURANCE — the sibling graded validator (Gate 4N-I28BH-B-ARCH, Design 1 §4).

WHY THIS EXISTS. `critical_list_inventory` discovers every module-level SECURITY-critical collection
constant and (as of this gate) requires each to carry an ASSURANCE ASSIGNMENT in
`security-assurance-registry.json` — a closed `assurance_kind` naming the property that CAN be proven
for that collection and the control that proves it. This module RUNS the controls for the kinds it
owns and reports fail-closed. Membership/partition kinds are already enforced by
`collection_completeness` (certificate-backed); this validator confirms those assignments are truly
wired and OWNS the five kinds that `collection_completeness` cannot express:

  AUTHORED_SOURCE_OF_TRUTH_INTEGRITY  reviewed-integrity digest (review_pin_control)   — the ~71 bulk
  EXCLUSION_POLICY_ASSURANCE          negative-space: reviewed exclusion + justification / ceiling
  CROSS_DOMAIN_CONSISTENCY            the collection agrees with an INDEPENDENT owner set
  GENERATED_CONTRACT_ASSURANCE        the collection == its generator re-run on pinned inputs
  RUNTIME_INVARIANT_ASSURANCE         a closed executed predicate holds over the live object

CLOSED DISPATCH, NO ALWAYS-PASS. `_HANDLERS` is a closed dict `{assurance_kind: module-level fn}`.
An unknown kind FAILS. A missing/invalid config FAILS. The registry carries DATA (kind + refs),
never a callable — a JSON fixture cannot inject a passing function. Every handler's only ACCEPT path
is a positive proof; every other path is a fail-closed refusal.

HONEST SCOPE. For the authored vocabularies this certifies REVIEWED INTEGRITY (no drift from the last
independent review), not completeness against external truth — no such oracle exists for a
hand-authored list, which is exactly why it is authored. See `review_pin_control` for the full
argument. This removes the SECURITY_UNGOVERNED gap without manufacturing a fake oracle.

STATIC RESOLVABILITY. Module-level functions, direct calls, a literal handler dict — no class
dispatch, no getattr routing of behaviour, no runtime-constructed table. `site_taxonomy`
unresolved_calls stays 0 on the release-reachable path.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import sys
import types
from pathlib import Path

import review_pin_control as rpc

# The closed set of statuses a review-record may carry (root-of-trust self-invariant).
_LEDGER_STATUSES = ("ACTIVE", "SUPERSEDED", "REVOKED")

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "tests" / "fixtures"
REGISTRY = FIXTURES / "security-assurance-registry.json"
PIN_REGISTRY = FIXTURES / "review-pin-registry.json"
REVIEW_LEDGER = FIXTURES / "review-record-ledger.json"
CONSUMERS_CONTRACT = FIXTURES / "critical-list-contract.json"
READONLY_CEILING = FIXTURES / "readonly-verifier-ceiling.json"

# Every authored map that DECIDES governance must itself be pinned in the review-record ledger's
# governed_files (BH-C F3/F7): the assurance registry, the review-pin registry, the classification
# contract (what is SECURITY), and the independent read-only ceiling (what the *_READS grants may not
# exceed). A change to any of these moves its canonical digest and fails root-of-trust until the
# ledger is re-reviewed.
_GOVERNED_FILES = {
    "security-assurance-registry.json": REGISTRY,
    "review-pin-registry.json": PIN_REGISTRY,
    "critical-list-contract.json": CONSUMERS_CONTRACT,
    "readonly-verifier-ceiling.json": READONLY_CEILING,
}

# The CLOSED assurance-kind enum. Unknown kind -> fail-closed (no default branch).
MEMBERSHIP = "INDEPENDENT_MEMBERSHIP_COMPLETENESS"
PARTITION = "PARTITION_RELATION_ASSURANCE"
AUTHORED = "AUTHORED_SOURCE_OF_TRUTH_INTEGRITY"
EXCLUSION = "EXCLUSION_POLICY_ASSURANCE"
CROSS_DOMAIN = "CROSS_DOMAIN_CONSISTENCY"
GENERATED = "GENERATED_CONTRACT_ASSURANCE"
RUNTIME_INVARIANT = "RUNTIME_INVARIANT_ASSURANCE"

# The runtime-invariant predicates are a closed dict of module-level functions defined below
# (`_RUNTIME_PREDICATES`). A RUNTIME_INVARIANT entry selects one by name; the registry cannot name
# an arbitrary callable.


class AssuranceError(RuntimeError):
    """Fail-closed."""


# --------------------------------------------------------------------------------------------- #
# SHARED LOADERS — resolve a set reference to a concrete Python set, import-safe.
# --------------------------------------------------------------------------------------------- #
def _load_symbol(ref: str):
    """Resolve `module.py::NAME` (optionally `::NAME()` to call a zero-arg producer) to its value."""
    module_file, name = ref.split("::", 1)
    call = name.endswith("()")
    if call:
        name = name[:-2]
    module_name = module_file[:-3] if module_file.endswith(".py") else module_file
    module = importlib.import_module(module_name)
    if not hasattr(module, name):
        raise LookupError(f"{ref}: no such attribute")
    obj = getattr(module, name)
    return obj() if call else obj


def _load_fixture_path(ref: str):
    """Resolve `fixture.json#/a/b` to the value at that JSON pointer under tests/fixtures."""
    file_part, _, pointer = ref.partition("#")
    doc = json.loads((FIXTURES / file_part).read_text(encoding="utf-8"))
    node = doc
    for token in [t for t in pointer.split("/") if t]:
        node = node[token]
    return node


def _resolve_set(ref: str) -> set:
    """Resolve a set reference (symbol or fixture pointer) to a Python set of members."""
    value = _load_fixture_path(ref) if "#" in ref else _load_symbol(ref)
    if isinstance(value, dict):
        return set(value.keys())
    return set(value)


def _live_members(collection_id: str) -> set:
    """The live collection's members as a set (keys for a dict)."""
    value = rpc.load_collection(collection_id)
    if isinstance(value, dict):
        return set(value.keys())
    return set(value)


def _expanded_members(collection_id: str, expansion: str) -> set:
    """Members at the granularity a justification map addresses.

    "keys" (default) — dict keys / set members. "dict_items" — for a mapping whose values are
    dicts, the "outer::inner" pairs (e.g. an exemption table operator->{action}); for a mapping
    whose values are lists, "outer::v1,v2" over the sorted value list. This lets a per-carve-out
    justification map (finer than the top-level key) cover exactly what is excluded.
    """
    value = rpc.load_collection(collection_id)
    if expansion == "keys":
        return set(value.keys()) if isinstance(value, dict) else set(value)
    if expansion == "dict_items":
        if not isinstance(value, dict):
            raise TypeError(f"{collection_id}: dict_items expansion needs a mapping")
        members = set()
        for outer, inner in value.items():
            if isinstance(inner, dict):
                members.update(f"{outer}::{k}" for k in inner)
            elif isinstance(inner, (list, tuple, set, frozenset)):
                members.add(f"{outer}::" + ",".join(sorted(str(x) for x in inner)))
            else:
                members.add(f"{outer}::{inner}")
        return members
    raise ValueError(f"{collection_id}: unknown member_expansion {expansion!r}")


# --------------------------------------------------------------------------------------------- #
# HANDLERS — each returns {"verdict": ACCEPT|REFUSED_*, "detail": ...}. Fail-closed everywhere.
# --------------------------------------------------------------------------------------------- #
def _h_delegated(collection_id: str, entry: dict, ctx: dict) -> dict:
    """Membership/partition: enforcement lives in collection_completeness. Confirm the assignment is
    truthfully WIRED to an existing completeness consumer (the meta-completeness that closes the
    'item left the manifest -> goes green' hole). The set-difference itself is run there."""
    consumer_ref = entry.get("consumer_ref")
    if not consumer_ref:
        return {"verdict": "REFUSED_UNWIRED",
                "detail": f"{collection_id}: {entry['assurance_kind']} names no consumer_ref"}
    # BH-C F1: a row must delegate to ITS OWN consumer, never an arbitrary existing one. Without this
    # a collection could be relabelled membership/partition and "wired" to a DIFFERENT collection's
    # consumer (identity confusion) whose set-difference says nothing about this collection — and, for
    # an authored vocabulary with no self-consumer, that is exactly the AUTHORED->delegated downgrade.
    if consumer_ref != collection_id:
        return {"verdict": "REFUSED_MISWIRED_CONSUMER",
                "detail": f"{collection_id}: consumer_ref {consumer_ref!r} is not this collection's "
                          "own consumer; a membership/partition row must delegate to itself"}
    consumers = ctx["consumers"]
    if consumer_ref not in consumers:
        return {"verdict": "REFUSED_UNWIRED",
                "detail": f"{collection_id}: consumer_ref {consumer_ref!r} absent from "
                          "completeness_consumers — the membership/partition control is not wired"}
    # BH-C F2: for a PARTITION row whose universe is not fully independent of the member lists (the
    # boundary_deny universe is emitted from those very lists), a review-pin drop-backstop makes a
    # silently dropped member RED even though the tautological union==universe check would pass.
    if collection_id in ctx["pins"].get("pins", {}):
        pin_result = _h_review_pin(collection_id, entry, ctx)
        if pin_result["verdict"] != rpc.ACCEPT:
            return pin_result
    return {"verdict": rpc.ACCEPT,
            "detail": f"{collection_id}: wired to completeness consumer {consumer_ref}"}


def _h_review_pin(collection_id: str, entry: dict, ctx: dict) -> dict:
    pin = ctx["pins"].get("pins", {}).get(collection_id)
    live_value, load_error = rpc._load_for_pin(collection_id, pin)
    return rpc.verify_pin(collection_id, pin, live_value, ctx["ledger"], load_error)


def _h_exclusion(collection_id: str, entry: dict, ctx: dict) -> dict:
    """Negative-space assurance. Three sound sub-controls, selected by data (never a callback)."""
    subtype = entry.get("subtype")
    if subtype == "D2":                                     # ceiling-bounded: subset/disjoint
        owner = entry.get("ceiling_owner")
        relation = entry.get("ceiling_relation")
        if not owner or relation not in ("SUBSET_OF_CEILING", "DISJOINT_FROM_MUSTNOT"):
            return {"verdict": "REFUSED_MALFORMED", "detail": f"{collection_id}: D2 config invalid"}
        try:
            live = _live_members(collection_id)
            ceiling = _resolve_set(owner)
        except Exception as exc:
            return {"verdict": rpc.REFUSED_UNLOADABLE, "detail": f"{collection_id}: {exc}"}
        if relation == "SUBSET_OF_CEILING":
            extra = live - ceiling
            if extra:
                return {"verdict": "REFUSED_EXCEEDS_CEILING",
                        "detail": f"{collection_id}: members outside the independent ceiling "
                                  f"{owner}: {sorted(extra)}"}
        else:
            # A member inside the must-not set is a violation UNLESS it is a reviewed carve-out in
            # the module's own exceptions collection — which is ITSELF a D1 review-pinned collection,
            # so the carve-out cannot silently grow. overlap-minus-reviewed-exceptions must be empty.
            exceptions = set()
            exceptions_ref = entry.get("exceptions_ref")
            if exceptions_ref:
                try:
                    exceptions = _resolve_set(exceptions_ref)
                except Exception as exc:
                    return {"verdict": rpc.REFUSED_UNLOADABLE,
                            "detail": f"{collection_id}: exceptions {exceptions_ref}: {exc}"}
            overlap = (live & ceiling) - exceptions
            if overlap:
                return {"verdict": "REFUSED_INTERSECTS_MUSTNOT",
                        "detail": f"{collection_id}: members inside the must-not set {owner} with "
                                  f"no reviewed carve-out: {sorted(overlap)}"}
        return {"verdict": rpc.ACCEPT,
                "detail": f"{collection_id}: exclusion {relation} {owner} holds"}
    if subtype == "D3":                                     # deny/must-not list: drift-pin + rationale
        # The enforced control is reviewed-integrity of the deny list: it cannot silently change
        # between suite runs. The direct-consequence positive control (why a short list is a hole)
        # is the reviewed rationale carried in `positive_control` and exercised by the existing
        # suite tests for the enforcer; the pin is the drift gate that keeps that rationale valid.
        if not entry.get("positive_control"):
            return {"verdict": "REFUSED_NO_POSITIVE_CONTROL",
                    "detail": f"{collection_id}: D3 carries no reviewed positive-control rationale"}
        return _h_review_pin(collection_id, entry, ctx)
    # D1 (default): reviewed exclusion set + a justification for EVERY current member.
    result = _h_review_pin(collection_id, entry, ctx)
    if result["verdict"] != rpc.ACCEPT:
        return result
    justifications = entry.get("justifications")
    if not isinstance(justifications, dict):
        return {"verdict": "REFUSED_NO_JUSTIFICATIONS",
                "detail": f"{collection_id}: D1 exclusion carries no justification map"}
    try:
        live = _expanded_members(collection_id, entry.get("member_expansion", "keys"))
    except Exception as exc:
        return {"verdict": rpc.REFUSED_UNLOADABLE, "detail": f"{collection_id}: {exc}"}
    unjustified = sorted(m for m in live if str(m) not in justifications)
    if unjustified:
        return {"verdict": "REFUSED_UNJUSTIFIED_MEMBER",
                "detail": f"{collection_id}: excluded members without a reviewed justification: "
                          f"{unjustified}"}
    return {"verdict": rpc.ACCEPT,
            "detail": f"{collection_id}: exclusion pinned + every member justified"}


def _h_cross_domain(collection_id: str, entry: dict, ctx: dict) -> dict:
    owner = entry.get("owner")
    relation = entry.get("relation")
    if not owner or relation not in ("EQUAL", "SUBSET_OF_OWNER", "SUPERSET_OF_OWNER"):
        return {"verdict": "REFUSED_MALFORMED", "detail": f"{collection_id}: cross-domain config invalid"}
    try:
        live = _live_members(collection_id)
        owner_set = _resolve_set(owner)
    except Exception as exc:
        return {"verdict": rpc.REFUSED_UNLOADABLE, "detail": f"{collection_id}: {exc}"}
    if relation == "EQUAL" and live != owner_set:
        return {"verdict": "REFUSED_CROSS_DOMAIN_DRIFT",
                "detail": f"{collection_id}: != independent owner {owner}; "
                          f"only-here={sorted(live-owner_set)} only-owner={sorted(owner_set-live)}"}
    if relation == "SUBSET_OF_OWNER" and (live - owner_set):
        return {"verdict": "REFUSED_CROSS_DOMAIN_DRIFT",
                "detail": f"{collection_id}: has members the owner {owner} lacks: "
                          f"{sorted(live-owner_set)}"}
    if relation == "SUPERSET_OF_OWNER" and (owner_set - live):
        return {"verdict": "REFUSED_CROSS_DOMAIN_DRIFT",
                "detail": f"{collection_id}: missing owner {owner} members: "
                          f"{sorted(owner_set-live)}"}
    # SUBSET_OF_OWNER proves nothing is ADDED beyond the owner, but is blind to a member being
    # DROPPED (a curated subset shrinking toward empty stays a subset). For a security list whose
    # documented failure mode IS member-removal that is not enough, so a SUBSET row must carry a
    # review-pin drift backstop: any membership change — including a drop — REDs until an independent
    # review re-approves it. A SUBSET row without a pin fails closed rather than silently accepting.
    if relation == "SUBSET_OF_OWNER":
        if collection_id not in ctx["pins"].get("pins", {}):
            return {"verdict": "REFUSED_NO_DROP_BACKSTOP",
                    "detail": f"{collection_id}: a SUBSET_OF_OWNER cross-domain row must carry a "
                              "review-pin so member-removal cannot pass unseen; none is registered"}
        pin_result = _h_review_pin(collection_id, entry, ctx)
        if pin_result["verdict"] != rpc.ACCEPT:
            return pin_result
    return {"verdict": rpc.ACCEPT,
            "detail": f"{collection_id}: {relation} independent owner {owner} holds"}


def _policy_allow_actions(policy: dict) -> set:
    """The set of Allow-effect Action strings emitted by an IAM policy document."""
    actions = set()
    for statement in policy.get("Statement", []):
        if statement.get("Effect") == "Allow":
            action = statement.get("Action", [])
            actions.update(action if isinstance(action, list) else [action])
    return actions


def _h_generated(collection_id: str, entry: dict, ctx: dict) -> dict:
    generator = entry.get("generator")
    mode = entry.get("mode", "EQUAL")
    if not generator:
        return {"verdict": "REFUSED_MALFORMED", "detail": f"{collection_id}: generated config invalid"}
    try:
        produced = _load_symbol(generator if generator.endswith("()") else generator + "()")
        live_value = rpc.load_collection(collection_id)
    except Exception as exc:
        return {"verdict": rpc.REFUSED_UNLOADABLE, "detail": f"{collection_id}: {exc}"}
    if mode in ("FLATTEN_EQUALS_POLICY_ALLOW", "FLATTEN_UNION_EQUALS_POLICY_ALLOW"):
        # The collection is a grouped closure whose flattened action union must equal the Allow
        # actions of the policy the generator emits from it — i.e. the closure faithfully drives the
        # generated artifact, with nothing added or dropped in generation.
        #
        # INFRA-9 B-3: FLATTEN_UNION_EQUALS_POLICY_ALLOW extends this to a policy emitted from
        # SEVERAL closures (permanent W0 = REFRESH_CLOSURE ∪ W0_APPLY_CLOSURE). The union members
        # are named in `union_with` as DATA; each must itself be a mapping AND carry its own
        # review-pin drop-backstop — the tautology argument below applies to every contributing
        # closure, not just the primary. A UNION row with no union_with is malformed, never a
        # silent downgrade to the single-closure equality.
        if not isinstance(live_value, dict):
            return {"verdict": "REFUSED_MALFORMED", "detail": f"{collection_id}: expected a mapping"}
        union_refs = []
        if mode == "FLATTEN_UNION_EQUALS_POLICY_ALLOW":
            union_refs = entry.get("union_with")
            if not isinstance(union_refs, list) or not union_refs:
                return {"verdict": "REFUSED_MALFORMED",
                        "detail": f"{collection_id}: FLATTEN_UNION names no union_with collections"}
            # Adversarial-lane finding 6: a self-referencing or duplicated member is
            # meaningless under set union and would previously ACCEPT silently — an
            # undocumented acceptance is how a malformed registry edit slips review.
            if collection_id in union_refs or len(set(union_refs)) != len(union_refs):
                return {"verdict": "REFUSED_MALFORMED",
                        "detail": f"{collection_id}: union_with must not repeat members or "
                                  "name the primary collection"}
        flat = set()
        for members in live_value.values():
            flat.update(members)
        for ref in union_refs:
            try:
                other = rpc.load_collection(ref)
            except Exception as exc:
                return {"verdict": rpc.REFUSED_UNLOADABLE,
                        "detail": f"{collection_id}: union_with {ref}: {exc}"}
            if not isinstance(other, dict):
                return {"verdict": "REFUSED_MALFORMED",
                        "detail": f"{collection_id}: union_with {ref} is not a mapping"}
            for members in other.values():
                flat.update(members)
        allow = _policy_allow_actions(produced)
        if flat != allow:
            return {"verdict": "REFUSED_GENERATOR_MISMATCH",
                    "detail": f"{collection_id}: flatten(closure) != {generator} Allow actions; "
                              f"only-closure={sorted(flat - allow)} only-policy={sorted(allow - flat)}"}
        # BH-C F4: the generator emits its Allow set FROM this closure, so flatten==Allow is
        # TAUTOLOGICAL — it proves faithful generation, never that the closure CONTENT is reviewed.
        # Injecting an escalation action into the closure passes the equality while the emitted policy
        # genuinely grants it. A review-pin drop-backstop is therefore mandatory: any content change
        # to the closure (add or drop) REDs until an independent review re-approves it. For a UNION
        # row the same holds for EVERY contributing closure.
        for pinned_id in [collection_id, *union_refs]:
            if pinned_id not in ctx["pins"].get("pins", {}):
                return {"verdict": "REFUSED_NO_DROP_BACKSTOP",
                        "detail": f"{pinned_id}: a self-referential FLATTEN generated row must carry a "
                                  "review-pin so closure content changes cannot pass unseen; none registered"}
            pin_result = _h_review_pin(pinned_id, entry, ctx)
            if pin_result["verdict"] != rpc.ACCEPT:
                return pin_result
        return {"verdict": rpc.ACCEPT,
                "detail": f"{collection_id}: flatten(closure"
                          + (f" ∪ {len(union_refs)} union" if union_refs else "")
                          + f") == {generator} Allow actions ({len(flat)}) + pin(s)"}
    if rpc.canonical_digest(collection_id, produced, isinstance(produced, (list, tuple))) != \
            rpc.canonical_digest(collection_id, live_value, isinstance(live_value, (list, tuple))):
        return {"verdict": "REFUSED_GENERATOR_MISMATCH",
                "detail": f"{collection_id}: stored collection != {generator} re-run"}
    return {"verdict": rpc.ACCEPT,
            "detail": f"{collection_id}: equals generator {generator} on pinned inputs"}


def _runtime_resolvers_dispatch_liveness(value) -> tuple:
    """RESOLVERS <-> resolve_domain() dispatch, both directions, + helper liveness. Recomputed from
    the live source AST + module namespace, independent of the RESOLVERS literal."""
    import ast as _ast
    import inspect as _inspect
    cc = importlib.import_module("collection_completeness")
    tree = _ast.parse(_inspect.getsource(cc.resolve_domain))
    dispatched = set()
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Compare):
            operands = [node.left, *node.comparators]
            names = [o for o in operands if isinstance(o, _ast.Name)]
            consts = [o.value for o in operands if isinstance(o, _ast.Constant)
                      and isinstance(o.value, str)]
            if names and any(n.id in ("kind", "resolver", "resolver_kind") for n in names):
                dispatched.update(consts)
    registered = set(value)
    only_reg = registered - dispatched
    only_disp = dispatched - registered
    dangling = [k for k in registered if not hasattr(cc, "_" + k)]
    if only_reg or only_disp or dangling:
        return False, (f"registered-not-dispatched={sorted(only_reg)} "
                       f"dispatched-not-registered={sorted(only_disp)} "
                       f"no-live-helper={sorted(dangling)}")
    return True, f"RESOLVERS<->dispatch closed ({len(registered)}); every kind has a live helper"


def _runtime_discoverers_emit_liveness(value) -> tuple:
    """DISCOVERERS == kinds discover_sites() actually emits, both directions. Recomputed by
    executing the live discoverer, independent of the DISCOVERERS literal."""
    md = importlib.import_module("mutation_discovery")
    emitted = {row.get("kind") for row in md.discover_sites()}
    registered = set(value)
    if registered != emitted:
        return False, (f"registered-not-emitted={sorted(registered - emitted)} "
                       f"emitted-not-registered={sorted(emitted - registered)}")
    return True, f"DISCOVERERS<->emitted kinds closed ({sorted(emitted)})"


def _runtime_entry_points_reachable(value) -> tuple:
    """The enforcement-path reachability analysis over the live guard call graph is clean. Recomputed
    by enforcement_path.check() from the code's call graph, independent of the ENTRY_POINTS literal."""
    ep = importlib.import_module("enforcement_path")
    report = ep.check()
    if not report.get("clean"):
        return False, f"enforcement-path analysis not clean: {report.get('analysis_incomplete_for')}"
    return True, "enforcement-path reachability closure complete and clean"


def _runtime_scan_gates_live(value) -> tuple:
    """Every named scan gate resolves to a LIVE callable in its module. Recomputed from the live
    module namespace, independent of the SCAN_DOMAIN_GATES literal. (Silent gate-dropping is caught
    by the review-pin backstop this collection also carries.)"""
    if not isinstance(value, dict):
        return False, "SCAN_DOMAIN_GATES must map module -> gate names"
    dead = []
    for module_file, gates in value.items():
        module_name = module_file[:-3] if module_file.endswith(".py") else module_file
        module = importlib.import_module(module_name)
        for gate in gates:
            if not callable(getattr(module, gate, None)):
                dead.append(f"{module_file}::{gate}")
    return (not dead), f"dead gates: {dead}" if dead else "every scan gate resolves to a live callable"


def _runtime_disputed_context_decides(value) -> tuple:
    """Every disputed (action, context_key) pair evaluates to DENY over the shipped policy, and no
    Allow statement conditions a disputed action on the disputed key. Recomputed via the live IAM
    evaluator over the live generated policy, independent of the DISPUTED_RUNTIME_CONTEXT literal."""
    iam_eval = importlib.import_module("iam_eval")
    go = importlib.import_module("gen_operator_policies")
    policy = go.permanent_w0_policy()
    silent_allows = []
    for pair in value:
        action = pair[0] if isinstance(pair, (tuple, list)) else pair
        decision = iam_eval.decide(policy, action, "*", {}).decision
        if "DENY" not in getattr(decision, "value", str(decision)).upper():
            silent_allows.append(f"{action}->{decision}")
    residual = iam_eval.disputed_pairings(policy)
    if silent_allows or residual:
        return False, f"non-DENY disputed actions={silent_allows}; disputed_pairings={residual}"
    return True, "every disputed action DENIED; no Allow conditions on a disputed key"


def _runtime_scan_decisions_schema(value) -> tuple:
    """A runtime scan-decision buffer (empty at rest, populated during a scan) has no static content
    to pin; its security property is a SCHEMA invariant that holds in every state: each recorded
    value is (decision, reason) with the decision drawn from the INDEPENDENT closed leak_scan.DECISIONS
    enum and a non-empty reason. Recomputed from that enum, not from the buffer. Completeness (every
    scanned file recorded) is enforced separately by leak_scan.scan_repository's own reconciliation."""
    if not isinstance(value, dict):
        return False, "SCAN_DECISIONS must be a mapping"
    decisions = set(importlib.import_module("leak_scan").DECISIONS)
    bad = []
    for key, record in value.items():
        if not (isinstance(record, tuple) and len(record) == 2):
            bad.append(f"{key}: not a (decision, reason) pair")
        elif record[0] not in decisions:
            bad.append(f"{key}: decision {record[0]!r} not in the DECISIONS enum")
        elif not record[1]:
            bad.append(f"{key}: empty reason")
    return (not bad), f"schema violations: {bad[:5]}" if bad else \
        f"every recorded decision is in the DECISIONS enum with a reason ({len(value)} recorded)"


def _runtime_every_value_nonempty(value) -> tuple:
    """For a runtime record whose contract is 'every entry carries a justification' (e.g. a skip
    buffer that must never skip WITHOUT a reason), the state-independent invariant is that every
    recorded value is non-empty. Holds vacuously when the buffer is empty at rest and is enforced the
    moment an entry is added — a reasonless skip fails closed. No static content to pin (it is a
    runtime accumulator), so this schema invariant replaces a content pin."""
    if not isinstance(value, dict):
        return False, "expected a mapping of entry -> reason"
    missing = [k for k, v in value.items() if not v]
    return (not missing), f"entries with no reason: {missing[:5]}" if missing else \
        f"every recorded entry carries a non-empty reason ({len(value)} recorded)"


_RUNTIME_PREDICATES = {
    "RESOLVERS_DISPATCH_LIVENESS": _runtime_resolvers_dispatch_liveness,
    "DISCOVERERS_EMIT_LIVENESS": _runtime_discoverers_emit_liveness,
    "ENTRY_POINTS_LIVE_CALLABLE": _runtime_entry_points_reachable,
    "SCAN_GATES_LIVE_CALLABLE": _runtime_scan_gates_live,
    "DISPUTED_CONTEXT_DECIDES": _runtime_disputed_context_decides,
    "SCAN_DECISION_VALUES_IN_DOMAIN": _runtime_scan_decisions_schema,
    "MAPPING_VALUES_ALL_NONEMPTY": _runtime_every_value_nonempty,
}


def _h_runtime(collection_id: str, entry: dict, ctx: dict) -> dict:
    invariant = entry.get("invariant")
    predicate = _RUNTIME_PREDICATES.get(invariant)
    if predicate is None:
        return {"verdict": "REFUSED_UNKNOWN_INVARIANT",
                "detail": f"{collection_id}: runtime invariant {invariant!r} is not one of "
                          f"{tuple(_RUNTIME_PREDICATES)}"}
    try:
        value = rpc.load_collection(collection_id)
    except Exception as exc:
        return {"verdict": rpc.REFUSED_UNLOADABLE, "detail": f"{collection_id}: {exc}"}
    ok, why = predicate(value)
    if not ok:
        return {"verdict": "REFUSED_INVARIANT_VIOLATED", "detail": f"{collection_id}: {why}"}
    # A liveness-only invariant catches a dead member but not a silently DROPPED one; where the entry
    # also carries a review pin, enforce it as a scope-narrowing backstop (both must hold).
    if collection_id in ctx["pins"].get("pins", {}):
        pin_result = _h_review_pin(collection_id, entry, ctx)
        if pin_result["verdict"] != rpc.ACCEPT:
            return pin_result
    return {"verdict": rpc.ACCEPT, "detail": f"{collection_id}: runtime invariant {invariant} holds"}


# The CLOSED dispatch. A kind not in this dict fails closed in `assess` (no default handler).
_HANDLERS = {
    MEMBERSHIP: _h_delegated,
    PARTITION: _h_delegated,
    AUTHORED: _h_review_pin,
    EXCLUSION: _h_exclusion,
    CROSS_DOMAIN: _h_cross_domain,
    GENERATED: _h_generated,
    RUNTIME_INVARIANT: _h_runtime,
}


# --------------------------------------------------------------------------------------------- #
# TOP-LEVEL ASSESSMENT.
# --------------------------------------------------------------------------------------------- #
def _load_json(path: Path, what: str) -> dict:
    if not path.exists():
        raise AssuranceError(f"{what} is absent: {path}. Absence must never read as 'governed'.")
    return json.loads(path.read_text(encoding="utf-8"))


def _canonical_file_digest(path: Path) -> str:
    """A canonical-JSON sha256 of a fixture (whitespace/key-order independent)."""
    doc = json.loads(path.read_text(encoding="utf-8"))
    return "sha256:" + hashlib.sha256(
        json.dumps(doc, separators=(",", ":"), sort_keys=True, ensure_ascii=True)
        .encode("utf-8")).hexdigest()


def _root_of_trust(ledger: dict) -> list:
    """The regress TERMINATES here. Every AUTHORED MAP that decides governance — the assurance
    registry (which kind proves each collection), the review-pin registry (against which digest), the
    critical-list classification contract (what is SECURITY at all), and every independent CEILING an
    exclusion control measures against — is pinned by its canonical digest inside the review-record
    LEDGER, the small file a reviewer reads end-to-end. A tampered map moves its canonical digest and
    fails HERE until the ledger is re-reviewed. BH-C F3/F7 added the classification contract + the
    read-only ceiling to this set: before, the ceiling and the classification map were ungoverned, so
    a down-classification or a ceiling widening passed. HONEST BOUND: the ledger itself is the terminal
    human-reviewed anchor (and is additionally pinned in executed-state — a second independent
    validator); an all-fixtures editor past human review still bypasses, which is the standard
    root-of-trust posture, not machine-eliminable in-repo. Returns problems (empty == clean)."""
    problems = []
    governed = ledger.get("governed_files")
    if not isinstance(governed, dict):
        return ["the review-record ledger carries no governed_files digests — the governance maps "
                "would be ungoverned roots"]
    for name, path in _GOVERNED_FILES.items():
        if name not in governed:
            problems.append(f"root-of-trust: {name} is not pinned in the ledger governed_files — a "
                            "governance map must not be an ungoverned root")
            continue
        current = _canonical_file_digest(path)
        if governed[name] != current:
            problems.append(f"root-of-trust: {name} canonical digest {current} != ledger-pinned "
                            f"{governed[name]}; a change to a governance map must be re-reviewed in "
                            "the ledger before it is trusted")
    for record_id, record in ledger.get("review_records", {}).items():
        status = record.get("status") if isinstance(record, dict) else None
        if status not in _LEDGER_STATUSES:
            problems.append(f"root-of-trust: review record {record_id!r} has status {status!r} not "
                            f"in {_LEDGER_STATUSES}")
    return problems


def assess() -> dict:
    registry = _load_json(REGISTRY, "the assurance registry")
    pins = _load_json(PIN_REGISTRY, "the review-pin registry")
    ledger = _load_json(REVIEW_LEDGER, "the review-record ledger")
    consumers = _load_json(CONSUMERS_CONTRACT, "the critical-list contract").get(
        "completeness_consumers", {})
    ctx = {"pins": pins, "ledger": ledger, "consumers": consumers}

    assurance = registry.get("assurance")
    if not isinstance(assurance, dict) or not assurance:
        raise AssuranceError("the assurance registry assigns nothing")

    rows = []
    problems = list(_root_of_trust(ledger))
    for collection_id in sorted(assurance):
        entry = assurance[collection_id]
        if not isinstance(entry, dict):
            problems.append(f"{collection_id}: registry entry is not an object")
            rows.append({"collection": collection_id, "verdict": "REFUSED_MALFORMED_ENTRY"})
            continue
        kind = entry.get("assurance_kind")
        # BH-C P6: a non-string kind (e.g. a JSON list/dict) is unhashable — guard it to a CLEAN
        # refusal rather than letting _HANDLERS.get() raise TypeError (still fail-closed, but a
        # controlled REFUSED beats an uncaught crash).
        handler = _HANDLERS.get(kind) if isinstance(kind, str) else None
        if handler is None:                                 # unknown/malformed kind -> fail closed
            problems.append(f"{collection_id}: unknown assurance_kind {kind!r}")
            rows.append({"collection": collection_id, "verdict": "REFUSED_UNKNOWN_KIND"})
            continue
        result = handler(collection_id, entry, ctx)
        rows.append({"collection": collection_id, "kind": kind, **result})
        if result["verdict"] != rpc.ACCEPT:
            problems.append(result["detail"])

    return {
        "assigned": len(assurance),
        "accepted": sum(1 for r in rows if r.get("verdict") == rpc.ACCEPT),
        "rows": rows,
        "problems": problems,
        "by_kind": _count_by_kind(assurance),
        "clean": not problems,
    }


def _count_by_kind(assurance: dict) -> dict:
    counts = {}
    for entry in assurance.values():
        if isinstance(entry, dict):
            kind = entry.get("assurance_kind")
            key = kind if isinstance(kind, str) else "<malformed>"   # BH-C P6: never hash a non-str
            counts[key] = counts.get(key, 0) + 1
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = assess()
    except AssuranceError as exc:
        print(f"  {exc}", file=sys.stderr)
        print("SECURITY-COLLECTION ASSURANCE: refused")
        return 2
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"  {result['assigned']} assigned; {result['accepted']} accepted; "
              f"{len(result['problems'])} problem(s); by kind: {result['by_kind']}")
        for problem in result["problems"][:40]:
            print(f"    {problem}", file=sys.stderr)
        print("SECURITY-COLLECTION ASSURANCE:", "clean" if result["clean"] else "REFUSED")
    return 0 if result["clean"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
