#!/usr/bin/env python3
"""Unified FAIL-CLOSED completeness framework — Gate 4N-I28BH-B0(-R).

WHY THIS MODULE EXISTS. `scripts/collection_completeness.py` can express exactly ONE completeness
relation: EXACT symmetric-difference (missing = D - C, unknown = C - D), plus a PARTITION union
path. That is the right oracle for the 13 collections it covers, but the remaining
security-critical collections need a WIDER vocabulary — asymmetric requirement (a required member
the collection must contain vs. an authority it must not exceed), keyed mappings, disjointness,
closed-schema strictness, provenance correspondence, reachability, differential execution, an
open-world hash backstop — AND, for authorities that have no in-repo enumerable oracle at all, a
family of FALSIFIABLE GUARANTEES that bind the declared list to an INDEPENDENT witness. Forcing an
asymmetric or non-enumerable design through EXACT reports false findings in one direction and, far
worse, can pass VACUOUSLY in the other. This module is that missing vocabulary, and it is
fail-closed by construction: no code path returns "clean" without the relation actually having
been evaluated against non-vacuous operands.

THREE LAYERS, ONE ENTRY POINT.
  * The COMPARATOR layer — 13 relations + a positive-presence gate that runs BEFORE the relation
    and refuses an empty load-bearing operand (the OBJ-3 vacuous-pass class). Public: compare().
  * The PROVIDER-VERIFIER layer — provenance / schema / harness / semantic kinds, each of which
    derives an OBSERVED set INDEPENDENTLY (never by reading the declared collection) and hands it
    to the comparator, with independence + directionality guards. Public: verify_provider().
  * The NON-ENUMERABLE layer — five guarantee kinds for authorities with no enumerable in-repo
    oracle, each binding the declared source to an independent consequence / site-universe /
    cross-source / mutation-witness / closed-world-refusal witness. Public: verify_non_enumerable().
  * evaluate(spec, ...) dispatches on spec['framework_kind'] and is what
    collection_completeness.check() calls for a spec whose resolver is one of FRAMEWORK_KINDS.

SCOPE OF THIS GATE (B0-R). This is the framework CAPABILITY. It is proven by SYNTHETIC
self-protection specs (tests/test_i28bh_b0a_completeness_framework.py) that exercise every
comparator direction, every guard, and every guarantee kind, in both the firing and the
fail-closed directions. The real per-collection specs are registered in the later BH-B1..B4
sub-gates; NO bulk registration happens here (the 13 enumerable specs are unchanged, and coverage
stays where it was). Registering the framework does not by itself close VAL-I28AX-01.

HARDENING CLOSED IN B0-R (adversary Agent-4, three fail-opens in the prototype comparator):
  * DIFFERENTIAL_EXECUTION could pass on an EMPTY collection-under-test because the generic
    presence default guarded the WRONG operand (the phantom domain D, not the load-bearing C).
    Closed: each relation declares its LOAD_BEARING operands and the presence gate refuses their
    emptiness regardless of the spec-declared operand (spec.operand may only WIDEN the guard).
  * HASH_BACKSTOP trusted a caller-supplied spec['observed_hash']; a stale/copied digest equal to
    the pin suppressed drift detection. Closed: the observed digest is ALWAYS recomputed from D;
    a supplied digest that disagrees is itself a REFUSAL, never a shortcut.
  * PARTITION delegated overlap detection to a caller-supplied spec['overlaps']; overlaps=[] hid a
    real disjointness violation. Closed: PARTITION requires spec['partition_members'] (the raw
    member sets) and witnesses overlap and the union ITSELF; a partition with no members is refused.
"""
from __future__ import annotations

import hashlib
import json
import sys
import types
import weakref
from typing import Any, Callable, Iterable, Optional


# ============================================================================================
# SHARED PROBLEM MODEL
# ============================================================================================
# ============================================================================================
# P4 — THE SINGLE WITNESS-EVALUATION GATE: LEDGER, SEALS, WITNESS-CALL DEPTH
# ============================================================================================
# P4 says: every witness-bearing evaluation, on every route, reaches its verdict at ONE gate. A
# convention ("call the gate") is not a control, so three mechanisms enforce it:
#
#   1. TRANSIT LEDGER (runtime, here). Every entry point opens an `_evaluation` scope. The gate
#      records a transit into every OPEN scope. `_sealed`/`_sealed_strings` refuse a CLEAN verdict
#      that never transited — so a future route that forgets the gate fails closed instead of
#      passing quietly. This is the property that makes P4 TOTAL rather than a list of fixes.
#   2. WITNESS RE-ENTRANCY REFUSAL (runtime, in the gate). `_WITNESS_CALL_DEPTH` is > 0 while a
#      registered witness/provider is executing, and the gate REFUSES to run in that state: a
#      witness supplies an OBSERVATION, and one that reaches a checker is a second evaluator.
#   3. AST INVARIANTS (structural, in the P4 battery). No set algebra outside a gate-reachable
#      checker; one caller of the gate; declared callers of compare(); every guarantee kind
#      declaring its transit; every entry point sealed; every witness invoked through the wrapper.
#
# Neither the ledger nor the call depth is a spec field or a public argument: neither can be
# forged from a JSON fixture or steered by a caller.

# One ledger per OPEN evaluation scope. A transit is recorded into EVERY open scope, so a nested
# evaluation (verify_provider -> compare) satisfies the outer scope rather than stranding it.
_EVAL_SCOPES: list[list] = []

# >0 while a registered witness/provider callable is executing.
_WITNESS_CALL_DEPTH = 0


# --- OPTION-2 STATIC-RESOLVABILITY ALIASES -------------------------------------------------------
# Bound builtin methods, captured once. Calling `_DICT_CONTAINS(self, k)` is byte-for-byte
# `dict.__contains__(self, k)` == `super().__contains__(k)` for a direct `dict` subclass, but the
# dunder attribute is no longer *referenced inside a function body*, so site_taxonomy's resolver
# (which walks only function bodies) never sees a multi-owner dunder collision. Semantics unchanged.
_DICT_INIT = dict.__init__
_DICT_GET = dict.get
_DICT_CONTAINS = dict.__contains__
_DICT_ITER = dict.__iter__
_EXC_INIT = Exception.__init__
_INT_REPR = int.__repr__
_FLOAT_REPR = float.__repr__
_STR_LEN = str.__len__
_LIST_LEN = list.__len__
# Explicit field enumeration for the immutable P9 identity record (replaces self.__slots__ /
# getattr(self, k) dynamic reflection with a statically-followable tuple).
_PROVIDER_RECORD_FIELDS = ("name", "registry", "fn", "module", "qualname", "code_digest",
                           "defaults_digest", "source_path", "source_sha256", "disk_code_digest",
                           "disk_authority", "trust_scope", "identity_digest", "_frozen")


class _evaluation:
    """One witness evaluation. Opens a transit ledger; `_sealed` refuses a clean verdict that
    never transited the gate."""

    __slots__ = ("path", "cid", "transits")

    def __init__(self, path: str, cid: str = "<collection>"):
        self.path, self.cid, self.transits = path, cid, []

    def __enter__(self) -> "_evaluation":
        _EVAL_SCOPES.append(self.transits)
        return self

    def __exit__(self, *exc) -> bool:
        _EVAL_SCOPES.pop()
        return False


def _no_transit_problem(ev: "_evaluation") -> dict:
    return _problem(ev.path, "NO_GATE_TRANSIT",
                    f"{ev.cid}: this evaluation reached a CLEAN verdict WITHOUT ever transiting "
                    "the single witness-evaluation gate; an ungated clean verdict is exactly the "
                    "parallel-evaluator class P4 closes; REFUSED")


def _sealed(ev: "_evaluation", problems: list) -> list:
    """Seal a dict-returning entry point: clean requires at least one gate transit."""
    if not problems and not ev.transits:
        problems.append(_no_transit_problem(ev))
    return problems


def _sealed_strings(ev: "_evaluation", problems: list) -> list:
    """Seal a string-returning entry point (Part D / the legacy consumer merge format)."""
    if not problems and not ev.transits:
        problems.append(_stringify(_no_transit_problem(ev), ev.cid))
    return problems


def _call_witness(fn, *args, **kwargs):
    """Invoke a registered witness/provider callable with the gate CLOSED. A witness supplies an
    OBSERVATION; if it wants a verdict it must reach a checker, and mechanism 2 refuses that."""
    global _WITNESS_CALL_DEPTH
    _WITNESS_CALL_DEPTH += 1
    try:
        return fn(*args, **kwargs)
    finally:
        _WITNESS_CALL_DEPTH -= 1


class FrameworkError(Exception):
    """Programmer-error in wiring the framework (a malformed normalize/presence directive).
    Adjudication failures are Problems, never exceptions, so a caller can never mistake a crash
    for a pass; this is reserved for genuinely structural mis-wiring."""


def _problem(relation: str, kind: str, detail: str, *, member: Any = None,
             fail_closed: bool = True) -> dict:
    p = {"relation": relation, "kind": kind, "detail": detail, "fail_closed": fail_closed}
    if member is not None:
        p["member"] = member
    return p


# ============================================================================================
# PART A — THE COMPARATOR LAYER
# ============================================================================================
def _normalizer(spec: dict) -> Callable[[Any], Any]:
    """Build an element normalizer from spec['normalize'] (an ordered list of directives).
    Unknown directive -> FrameworkError: we refuse to silently ignore a directive we do not
    implement, because that would change the comparison the design asked for."""
    directives = spec.get("normalize") or []
    if not isinstance(directives, (list, tuple)):
        raise FrameworkError(f"normalize must be a list, got {type(directives).__name__}")
    fns: list[Callable[[Any], Any]] = []
    for d in directives:
        if d == "casefold":
            fns.append(lambda x: x.casefold() if isinstance(x, str) else x)
        elif d == "strip":
            fns.append(lambda x: x.strip() if isinstance(x, str) else x)
        elif d == "strip_leading_dot":
            fns.append(lambda x: x.lstrip(".") if isinstance(x, str) else x)
        elif d == "posix_path":
            fns.append(lambda x: x.replace("\\", "/") if isinstance(x, str) else x)
        elif d == "str":
            fns.append(str)
        else:
            raise FrameworkError(f"unknown normalize directive {d!r}; fail-closed")

    def apply(x: Any) -> Any:
        for fn in fns:
            x = fn(x)
        return x

    return apply


def _as_normalized_set(name: str, raw: Any, norm: Callable[[Any], Any], relation: str,
                       problems: list) -> Optional[set]:
    """Coerce an iterable operand into a normalized set, reporting duplicate collisions.

    A DUPLICATE COLLISION (two distinct raw members normalizing to the same value) is a Problem,
    not a silent dedup: a list that appears to hold N entries but covers only N-1 distinct
    normalized members is a silently short list, exactly what completeness exists to catch."""
    if isinstance(raw, dict):
        raw_iter: Iterable = list(raw.keys())
    elif isinstance(raw, (set, frozenset, list, tuple)):
        raw_iter = list(raw)
    else:
        problems.append(_problem(relation, "MALFORMED_OPERAND",
                                 f"{name} is {type(raw).__name__}, expected an iterable of "
                                 "hashable members"))
        return None
    out: set = set()
    seen_norm: dict = {}
    for member in raw_iter:
        try:
            n = norm(member)
            hash(n)
        except TypeError:
            problems.append(_problem(relation, "MALFORMED_OPERAND",
                                     f"{name} member {member!r} is not hashable after "
                                     "normalization"))
            return None
        if n in seen_norm and seen_norm[n] != member:
            problems.append(_problem(relation, "DUPLICATE_COLLISION",
                                     f"{name} members {seen_norm[n]!r} and {member!r} both "
                                     f"normalize to {n!r}; the list is silently short", member=n))
        seen_norm.setdefault(n, member)
        out.add(n)
    return out


# ============================================================================================
# P5 — NO CALLER-AUTHORED GUARD DISABLE
# ============================================================================================
# A GUARD-DISABLING FIELD is a spec field whose value selects, from JSON, between two admissible
# states of a guard where one state is WEAKER: it turns a fail-closed default off, selects a
# weaker comparison path, relaxes a strictness/threshold, or certifies that an otherwise-refused
# condition is legitimate. Operationally (and this is the definition the P5 auditor below
# EXECUTES, so the class is DERIVED and not authored): a (field, value) pair is guard-disabling
# iff there exist two admissible spec states differing only in that field, where the strict state
# yields >= 1 fail-closed problem and the weak state yields ZERO.
#
# The executed bypass this closes (BYP-1) is the architecturally prior one: `presence:
# 'VALID_EMPTY'` is ONE JSON WORD that switches the whole load-bearing-operand presence class off
# for EVERY relation, comparator AND non-enumerable. `empty_condition_met: true` is the same
# defect wearing a condition's clothes — the spec asserts the condition rather than the framework
# computing it, so the OBJ-3 vacuous-pass class is re-openable from a fixture at will.
#
# THE CONTRACT. A guard-disabling field is never honoured on the caller's word. It is honoured
# only when a REGISTERED CONDITION PROVIDER, CALLED BY THE FRAMEWORK, COMPUTES the underlying
# condition to be True from evidence the caller does not control:
#   1. the spec must name a provider for the condition (`condition_providers: {condition: name}`);
#      an unbacked disable is REFUSED (this alone closes BYP-1 — there is no such field in the
#      fixture, and adding one names a provider that must exist in CODE, not in JSON);
#   2. the provider must be REGISTERED in code (register_condition_provider); a named-but-absent
#      provider is a missing witness and REFUSED, exactly as PROVIDERS/NE_PROVIDERS already are;
#   3. the framework calls it with a REDACTED spec view from which every registered guard-disabling
#      field has been stripped, so the provider CANNOT read the caller's self-certification and
#      launder it back as a computation (echo-proof by construction, not by inspection);
#   4. the provider must be DISCRIMINATING: the framework also calls it against a per-condition
#      NEGATIVE CONTROL context in which the honest answer is False. A rubber stamp that returns
#      True unconditionally is REFUSED. A condition that declares no negative control cannot
#      authorize anything — an unfalsifiable condition is not a control (I28BC);
#   5. a provider whose answer CHANGES when shown the raw spec is reading the declaration it was
#      supposed to be blind to, and is REFUSED;
#   6. only the literal `True` authorizes. False, non-bool, or a raise all REFUSE;
#   7. a spec literal (e.g. `empty_condition_met`) may remain as documentation but is NEVER the
#      decision input, and if it DISAGREES with the computed condition that disagreement is itself
#      a refusal — the same rule HASH_BACKSTOP's `observed_hash` already lives under.
#
# The point is not these five fields. It is that a FUTURE field of this class is governed or
# refused: audit_guard_disabling_fields() re-derives the class by execution and refuses any
# relaxation that is not registered here, so a new lenient branch cannot be added silently.

CONDITION_PROVIDERS: dict[str, Callable] = {}

# P8-ESC-04. A CONDITION PROVIDER IS A WITNESS — the one witness class P9 never reached.
# P9 binds PROVIDERS / NE_PROVIDERS to a reviewed authority (WITNESS_PROVIDER_MANIFEST) so that a
# bare NAME can no longer be trusted. `CONDITION_PROVIDERS` kept the pre-P9 shape: a bare name bound
# to ANY callable, rebindable in place, lambdas accepted, and the SPEC free to point that name at
# any guard-disable condition it liked. That is the same defect P9 closed, applied to the callable
# whose single job is to switch a fail-closed guard OFF — the highest-value witness in the module.
#
# The closure is the P9 shape, exactly: a reviewed manifest names WHICH callable may authorize
# WHICH conditions, identity is RECOMPUTED from the live object at registration (never supplied by
# the caller as a datum), registration is no-override, and an unmanifested provider authorizes
# nothing. A spec may still NAME a provider — naming is an input — but it can no longer CHOOSE one,
# because the name it offers must already have been bound to that condition by a reviewed diff.
#
# WHAT THIS DOES NOT CLAIM. It does not certify that a MANIFESTED provider computes anything
# meaningful: the banked P8-ESC-04 tautology (`lambda ctx: ctx["operand_empty"]`, which restates the
# framework's own observation) is refused here because it is unmanifested and unregistrable, not
# because its semantics were adjudicated. The semantic adequacy of a pinned condition provider is a
# review and P6-independence obligation, and is recorded as an OPEN residual rather than claimed.
CONDITION_PROVIDER_MANIFEST: dict[str, dict] = {}

_CONDITION_PROVIDER_IDENTITY: dict[str, dict] = {}

_CONDITION_IDENTITY_FIELDS = ("module", "qualname", "code_digest")


def _condition_provider_identity(fn: Callable) -> dict:
    """RECOMPUTE a condition provider's identity from the live callable. Never accepts a digest."""
    return {"module": getattr(fn, "__module__", None),
            "qualname": getattr(fn, "__qualname__", None),
            "code_digest": _p9_code_fingerprint(fn.__code__)}


def register_condition_provider(name: str, fn: Callable) -> None:
    """Register a provider that COMPUTES a guard-disable condition. Providers live in code and are
    bound by the lead in the BH-B sub-gates; tests register synthetic ones. Registration is the
    only way a guard-disabling field can ever be honoured.

    P8-ESC-04: registration now RECOMPUTES the callable's identity and refuses the two shapes that
    made the registry a free channel — an anonymous callable (a lambda has no reviewable name, so
    nothing a manifest could bind) and a re-binding (installing a different callable under a name a
    spec already trusts is witness substitution, the act P9's no-override rule refuses for every
    other registry)."""
    if not isinstance(fn, types.FunctionType) or getattr(fn, "__name__", "") == "<lambda>":
        raise ContractPinError(
            f"condition provider {name!r} is {type(fn).__name__} "
            f"{getattr(fn, '__name__', '?')!r}; a guard-disable authority must be a named "
            "module-level function so a reviewed manifest can bind it — an anonymous callable has "
            "no identity to review; REFUSED (P8-ESC-04)")
    identity = _condition_provider_identity(fn)
    existing = _CONDITION_PROVIDER_IDENTITY.get(name)
    if existing is not None and existing != identity:
        raise ContractPinError(
            f"condition provider {name!r} is already bound to "
            f"{existing['module']}.{existing['qualname']}; re-binding the callable that authorizes "
            "switching a fail-closed guard off is witness substitution, not configuration; REFUSED "
            "(P8-ESC-04)")
    _CONDITION_PROVIDER_IDENTITY[name] = identity
    CONDITION_PROVIDERS[name] = fn


def check_condition_provider_pin(name: str, condition: str, fn: Callable) -> Optional[tuple]:
    """Adjudicate a NAMED condition provider against the reviewed manifest. Returns None when the
    binding is authorised, or a (kind, detail) pair.

    This is called from authorize_guard_disable — the ONE function every guard-disable decision
    crosses, from the comparator layer, from the non-enumerable layer, and from a direct compare()
    — so the binding is governed on every path, not at one entry point."""
    entry = CONDITION_PROVIDER_MANIFEST.get(name)
    if entry is None:
        return ("GUARD_DISABLE_PROVIDER_UNPINNED",
                f"condition provider {name!r} is absent from CONDITION_PROVIDER_MANIFEST, so no "
                "reviewed authority says this callable may switch a fail-closed guard off. A spec "
                "may NAME a provider; it may not CHOOSE one. REFUSED (P8-ESC-04)")
    permitted = tuple(entry.get("conditions") or ())
    if condition not in permitted:
        return ("GUARD_DISABLE_PROVIDER_NOT_AUTHORISED_FOR_CONDITION",
                f"condition provider {name!r} is manifested for {list(permitted)!r} and the spec "
                f"points it at {condition!r}; a provider reviewed for one guard does not carry "
                "authority over another. REFUSED (P8-ESC-04)")
    recorded = _CONDITION_PROVIDER_IDENTITY.get(name)
    live = _condition_provider_identity(fn)
    if recorded is None:
        return ("GUARD_DISABLE_PROVIDER_BARE_NAME_BINDING",
                f"CONDITION_PROVIDERS[{name!r}] = … was assigned directly; a guard-disable "
                "authority must be installed through register_condition_provider(), which "
                "recomputes its identity and refuses a re-binding. A witness that entered the "
                "registry by assignment has no recorded identity to compare. REFUSED (P8-ESC-04)")
    for field in _CONDITION_IDENTITY_FIELDS:
        if field in entry and entry[field] != live.get(field):
            return ("GUARD_DISABLE_PROVIDER_IDENTITY_MISMATCH",
                    f"condition provider {name!r} is manifested as {entry.get('module')}."
                    f"{entry.get('qualname')} ({field} mismatch) but the registered callable is "
                    f"{live['module']}.{live['qualname']}; the reviewed authority is not the "
                    "executed one. REFUSED (P8-ESC-04)")
    if recorded is not None and recorded != live:
        return ("GUARD_DISABLE_PROVIDER_IDENTITY_MISMATCH",
                f"condition provider {name!r} was registered as {recorded['module']}."
                f"{recorded['qualname']} but the callable now in the registry is "
                f"{live['module']}.{live['qualname']}; a witness swapped after registration is "
                "refused. REFUSED (P8-ESC-04)")
    return None


# Sentinel members the framework injects into a negative-control context. No real design can
# justify suppressing a finding about a member that does not exist, or permitting an operand that
# is demonstrably non-empty; a provider that does is not computing anything.
_P5_NEG_MEMBER = "__P5_NEGATIVE_CONTROL_MEMBER__"

# condition -> description. The DECLARATION of what each condition means, in the module namespace
# because it is documentation a reviewer reads.
#
# The NEGATIVE CONTROL that falsifies each condition is NOT here — see _guard_disable_control_plane
# below. A condition with no negative control is UNFALSIFIABLE and can authorize nothing.
_CONDITIONS: dict[str, str] = {
    "EMPTY_LOAD_BEARING_OPERAND_LEGITIMATE":
        "the load-bearing operand being EMPTY is a true property of the authority, not a broken "
        "or empty discovery",
    "EMPTY_AUTHORITATIVE_SOURCE_LEGITIMATE":
        "the non-enumerable authoritative source being EMPTY is a true property of the authority",
    "PROVENANCE_MAY_BE_INJECTIVE":
        "the authority legitimately holds provenance records with no corresponding collection "
        "member, so the 'missing' direction is not load-bearing for this collection",
    "UNREACHABLE_DECLARED_MEMBER_LEGITIMATE":
        "a declared member that the walk cannot reach is legitimate here (e.g. reached by a "
        "mechanism outside the walk), so it is informational rather than a finding",
    "TRIANGULATION_FLOOR_MAY_BE_LOWERED":
        "fewer independent grounds than the floor genuinely exhaust the available authorities",
}


def _guard_disable_control_plane():
    """THE NEGATIVE-CONTROL DELTAS. Control-plane state, held in this closure and not in a module
    attribute (FF-08 / §12).

    Two changes from the banked form, both found by this gate's own probe battery:

    (a) A delta is a DATA-PLANE FACT, not a whole context. It used to be built as
        `dict(ctx, …, probe="negative_control")`, so the framework wrote the name of its own
        control into the data it handed the witness and `ctx['probe'] != 'negative_control'` was a
        constant-True rubber stamp. A delta now states only the fact about the world that makes the
        honest answer False, and _provider_context() refuses a delta that introduces a key the
        production call did not carry — because the presence of the key would itself be the label.

    (b) THE TABLE IS NOT PUBLISHED. While the deltas sat in `_CONDITIONS`, a provider could look up
        the delta the framework was about to apply and recognise the control generically, with no
        label and no knowledge of any particular condition — vector V-9 of the §12 battery, which
        ESCAPED until this table moved. The equivalent of a published answer key.

    HONEST LIMIT. Hiding the key stops a provider RECOGNISING the control; it does not make a
    provider GROUNDED. For EMPTY_*, the delta is the very fact the condition is about, so a
    provider that mirrors `operand_empty` discriminates perfectly while consulting nothing — it is
    behaviourally identical to an honest one from inside this control, and the two are separated by
    evidence quality, not by this check (§12 arm V-10, scored NOT-CLOSED-BY-DESIGN). For the two
    SUPPRESSION conditions the delta is `_P5_NEG_MEMBER`, a member that cannot exist: recognising
    it is simultaneously the honest computation (P5's own positive control does exactly that) and
    the sniff, so those two conditions are non-discriminable until they are given a fact to compute
    on. Both are named in the deliverable rather than absorbed."""
    deltas = {
        # Honest answer against a demonstrably NON-empty operand: False (the empty-condition is
        # not met, so the disable is not warranted). ONLY `operand_empty`: the banked delta also
        # planted `operand_members=(_P5_NEG_MEMBER,)`, a fixed publicly-known token no production
        # call ever carries — a second label for the control wearing data-plane clothes. Nothing
        # reads operand_members, so the marker was pure leakage.
        "EMPTY_LOAD_BEARING_OPERAND_LEGITIMATE": lambda: {"operand_empty": False},
        "EMPTY_AUTHORITATIVE_SOURCE_LEGITIMATE": lambda: {"operand_empty": False},
        "PROVENANCE_MAY_BE_INJECTIVE": lambda: {"suppressed": (_P5_NEG_MEMBER,)},
        "UNREACHABLE_DECLARED_MEMBER_LEGITIMATE": lambda: {"suppressed": (_P5_NEG_MEMBER,)},
        "TRIANGULATION_FLOOR_MAY_BE_LOWERED": lambda: {"requested": -1},
    }

    def negative_control(condition: str) -> Optional[dict]:
        builder = deltas.get(condition)
        return None if builder is None else builder()

    return negative_control


_negative_control_delta = _guard_disable_control_plane()

# The registered guard-disabling field class. Each entry: the spec path, the condition that must
# be COMPUTED to honour it, and the predicate identifying the WEAK state of that field.
# `layer` records where it bites; `derived_by` records the probe that found it (the class is
# maintained by re-running the auditor, not by recollection).
_GUARD_DISABLING: tuple[dict, ...] = (
    {"field": "presence.policy", "condition": "EMPTY_LOAD_BEARING_OPERAND_LEGITIMATE",
     "weak": ("VALID_EMPTY", "CONDITIONALLY_EMPTY"), "layer": "comparator",
     "derived_by": "1b differential (8/8 relation contexts REFUSED -> CLEAN)"},
    {"field": "presence.empty_condition_met", "condition": "EMPTY_LOAD_BEARING_OPERAND_LEGITIMATE",
     "weak": (True,), "layer": "comparator",
     "derived_by": "1b differential (CONDITIONALLY_EMPTY + literal True)"},
    {"field": "positive_presence", "condition": "EMPTY_AUTHORITATIVE_SOURCE_LEGITIMATE",
     "weak": ("VALID_EMPTY", "CONDITIONALLY_EMPTY"), "layer": "non_enumerable",
     "derived_by": "1c differential (VALID_EMPTY passed an empty source silently)"},
    {"field": "empty_condition_met", "condition": "EMPTY_AUTHORITATIVE_SOURCE_LEGITIMATE",
     "weak": (True,), "layer": "non_enumerable", "derived_by": "1c differential"},
    {"field": "correspondence", "condition": "PROVENANCE_MAY_BE_INJECTIVE",
     "weak": ("injective",), "layer": "comparator",
     "derived_by": "1d relaxation pair (bijective -> injective: 1 -> 0 refusals)"},
    {"field": "strict_reachability", "condition": "UNREACHABLE_DECLARED_MEMBER_LEGITIMATE",
     "weak": (False, None), "layer": "comparator",
     "derived_by": "1d relaxation pair (True -> False/absent: 1 -> 0 refusals)"},
    {"field": "min_grounds", "condition": "TRIANGULATION_FLOOR_MAY_BE_LOWERED",
     "weak": ("<below floor>",), "layer": "non_enumerable",
     "derived_by": "1e relaxation pair (2 -> 0/1: 1 -> 0 refusals)"},
)

# The default triangulation floor. Below it, min_grounds is a caller-authored relaxation.
_MIN_GROUNDS_FLOOR = 2

_DISABLING_FIELD_PATHS = tuple(d["field"] for d in _GUARD_DISABLING)


def _redact_disables(spec: dict) -> dict:
    """A spec view with every registered guard-disabling field stripped. The condition provider
    is called with THIS, never the raw spec: it is structurally unable to read the caller's
    self-certification and hand it back as a computed condition."""
    view = {k: v for k, v in spec.items() if k not in {"condition_providers"}}
    for path in _DISABLING_FIELD_PATHS:
        head, _, tail = path.partition(".")
        if not tail:
            view.pop(head, None)
            continue
        sub = view.get(head)
        if isinstance(sub, dict):
            sub = {k: v for k, v in sub.items() if k != tail}
            view[head] = sub
        elif head in view and tail == "policy":
            # the string shorthand IS the policy
            view.pop(head, None)
    return view


# ============================================================================================
# FF-08 / §12 — THE PROBE CONTROL PLANE IS NOT DATA THE WITNESS CAN READ
# ============================================================================================
# authorize_guard_disable() runs the SAME provider three times — the production COMPUTE call, the
# NEGATIVE CONTROL, and the ECHO CONTROL — and compares the answers. WHICH of the three is running
# is TEST/PROBE control-plane state. It used to be handed to the provider inside the context as
# `probe`, so the label the framework used to mark its own control was an input the witness could
# read: `return ctx['probe'] != 'negative_control'` is a constant-True rubber stamp that satisfies
# the discrimination check AND the echo check, on an EMPTY load-bearing operand (FF-08).
#
# The separation — not a blocklist of the string, which the next label would walk straight past:
#
#   * THE PHASE IS A LOCAL. It is a parameter of the nested call() below and nothing else: not
#     module state, not a spec field, not a context key, and there is no accessor for it. There is
#     therefore nothing for a provider to read, alias, spoof with an enum, nest inside an object,
#     fetch from an env var, or obtain by calling a framework helper — the channel does not exist.
#   * THE CONTEXT IS BUILT, NEVER INHERITED. _provider_context() emits a FIXED key set in a FIXED
#     order drawn from a closed allowlist, so neither the spec nor the caller's ctx can introduce a
#     control-plane key, and production data cannot impersonate a test fixture by carrying one.
#   * A PROBE MAY NOT WIDEN THE CHANNEL. A phase delta may only overwrite a key the production call
#     already had; introducing a NEW key would itself be a phase signal, so it is a FrameworkError.
#   * `declared_state` IS OFF THE ALLOWLIST. It is the caller's own self-certification — the very
#     thing _redact_disables() strips from `spec`. Posting it in the context would have made that
#     redaction cosmetic.
#   * A PROBE RESULT IS NEVER AUTHORITY. Only the COMPUTE answer can return True from this
#     function; the two control answers can only refuse. The negative-control branch cannot
#     certify anything clean, and no production caller can request one.
_PROVIDER_CONTEXT_KEYS = ("cid", "condition", "field", "floor", "layer", "operand",
                          "operand_empty", "operand_members", "provider", "relation",
                          "requested", "spec", "suppressed")


def _provider_context(ctx: dict, condition: str, provider: str, spec_view: dict,
                      delta: Optional[dict] = None) -> dict:
    """The ONE shape of context a condition provider is ever handed.

    Same keys, same order, same length in every phase; the ONLY difference between the compute
    call and a control call is the declared data-plane delta the control exists to introduce —
    which is the fact about the world the provider is supposed to be computing on. A provider that
    discriminates on it is doing its job; there is nothing else left to discriminate on."""
    built = {key: ctx[key] for key in _PROVIDER_CONTEXT_KEYS if key in ctx}
    built["condition"] = condition
    built["provider"] = provider
    built["spec"] = spec_view
    for key, value in (delta or {}).items():
        if key not in built:
            raise FrameworkError(
                f"internal: a guard-disable phase delta names {key!r}, which the production call "
                "does not carry; a probe may not introduce a context channel the production call "
                "lacks, because the presence of the channel would itself label the probe")
        built[key] = value
    return {key: built[key] for key in _PROVIDER_CONTEXT_KEYS if key in built}


def authorize_guard_disable(condition: str, spec: dict, ctx: dict, problems: list,
                            *, relation: str = "?", stringly: bool = False) -> bool:
    """Return True iff a REGISTERED condition provider COMPUTES `condition` as True.

    Appends a refusal to `problems` (as a dict for the comparator layer, or a string for the
    non-enumerable layer when stringly=True) and returns False otherwise. Never raises: a
    provider that raises is a refusal, not a crash the caller could mistake for a pass."""
    cid = ctx.get("cid", spec.get("source_collection_id", "<collection>"))

    def refuse(kind: str, detail: str) -> bool:
        if stringly:
            problems.append(f"{cid}: REFUSED [{kind}] {detail}")
        else:
            problems.append(_problem(relation, kind, detail))
        return False

    neg_delta = _negative_control_delta(condition) if condition in _CONDITIONS else None
    if neg_delta is None:
        return refuse("GUARD_DISABLE_CONDITION_UNFALSIFIABLE",
                      f"guard-disable condition {condition!r} declares no negative control; an "
                      "unfalsifiable condition cannot authorize weakening a guard; REFUSED")

    declared_providers = spec.get("condition_providers")
    if not isinstance(declared_providers, dict) or not declared_providers.get(condition):
        return refuse("GUARD_DISABLE_UNBACKED",
                      f"the spec weakens a fail-closed guard (condition {condition!r}) on its own "
                      "word: no condition_providers entry names a provider to COMPUTE it. A "
                      "guard-disabling field is never honoured as a JSON literal; REFUSED")
    pname = declared_providers[condition]
    fn = CONDITION_PROVIDERS.get(pname)
    if fn is None:
        return refuse("GUARD_DISABLE_PROVIDER_UNREGISTERED",
                      f"condition provider {pname!r} for {condition!r} is not registered; a "
                      "missing witness fails closed; REFUSED")

    # P8-ESC-04. THE CHOKEPOINT. Every guard-disable decision in the module crosses this function —
    # the comparator layer, the non-enumerable layer and a direct compare() all arrive here — so the
    # provider BINDING is adjudicated here rather than at any one entry point. A registered callable
    # is not yet an authorised one: the reviewed manifest must name it FOR THIS CONDITION, and the
    # callable in the registry must still be the one whose identity was recomputed at registration.
    unpinned = check_condition_provider_pin(pname, condition, fn)
    if unpinned is not None:
        return refuse(unpinned[0], f"{cid}: {unpinned[1]}")

    redacted = _redact_disables(spec)

    def call(delta: Optional[dict], spec_view: dict, label: str):
        """Run ONE phase. `label` is control-plane state: it stays in this frame, is used only to
        name the phase in a refusal a human reads, and never enters the dict the provider sees."""
        context = _provider_context(ctx, condition, pname, spec_view, delta)
        try:
            # P4: a registered CONDITION provider is a witness of a guard-disabling condition. It
            # computes a fact; a verdict is not its to reach.
            return _call_witness(fn, context), None
        except Exception as exc:                      # a condition that crashes is not met
            return None, f"{type(exc).__name__}: {exc} (during the {label} call)"

    answer, err = call(None, redacted, "compute")
    if err is not None:
        return refuse("GUARD_DISABLE_PROVIDER_RAISED",
                      f"condition provider {pname!r} raised {err}; REFUSED")
    if not isinstance(answer, bool):
        return refuse("GUARD_DISABLE_PROVIDER_NON_BOOL",
                      f"condition provider {pname!r} returned {type(answer).__name__}, not a "
                      "bool; an ambiguous condition cannot authorize a guard disable; REFUSED")
    if answer is not True:
        return refuse("GUARD_DISABLE_REFUSED_BY_PROVIDER",
                      f"condition provider {pname!r} COMPUTED {condition!r} as False; the "
                      "weakening the spec declared is not justified by the evidence; REFUSED")

    # Echo: shown the RAW spec (including the caller's self-certification), an honest provider
    # computes the same answer. One that changes its mind was reading the declaration.
    #
    # ORDER IS LOAD-BEARING (FF-08). This control now runs BEFORE the discrimination control. With
    # the `probe` label gone, a provider that launders the caller's declaration can no longer
    # recognise the negative control and answer False to it, so it fails BOTH checks — and the
    # discrimination refusal, running first, would have masked the specific and more informative
    # one. Naming the sibling control instead of the intended one is exactly the "refused, but by
    # the wrong detector" outcome that leaves the intended control unproven.
    echo, err = call(None, dict(spec), "echo-control")
    if err is None and echo != answer:
        return refuse("GUARD_DISABLE_PROVIDER_READS_DECLARATION",
                      f"condition provider {pname!r} answers differently when shown the raw spec "
                      f"({echo!r}) than when shown the redacted view ({answer!r}); it is reading "
                      "the caller's own self-certification, not computing the condition; REFUSED")

    # Discrimination: the same provider must answer False for a context in which the honest
    # answer is False. A constant-True rubber stamp satisfies any claim and certifies nothing
    # (the kind-E ADV17-02 lesson, generalised to guard-disable conditions).
    neg, err = call(neg_delta, redacted, "negative-control")
    if err is not None:
        return refuse("GUARD_DISABLE_PROVIDER_RAISED",
                      f"condition provider {pname!r} raised {err}; REFUSED")
    if neg is not False:
        return refuse("GUARD_DISABLE_PROVIDER_NOT_DISCRIMINATING",
                      f"condition provider {pname!r} also returned {neg!r} for the negative "
                      "control (where the honest answer is False); a rubber-stamp condition "
                      "cannot authorize weakening a guard; REFUSED")

    # NOTE on the spec literal (`empty_condition_met`): there is deliberately NO "declared
    # disagrees with computed" branch here. Each call site runs the pre-existing literal check
    # FIRST and refuses before reaching this function, so a literal that contradicts a True
    # computation has already been rejected and such a branch would be unreachable. An
    # unreachable refusal in a control is worse than no refusal — it reads as protection while
    # asserting nothing (I28W). The literal's role is documentation; this function is the decision.
    #
    # The authority returned is the COMPUTE answer and ONLY the compute answer. The two control
    # answers above can refuse and nothing else, so a probe result can never become the production
    # witness that authorizes weakening a guard (§12).
    return answer is True


def audit_guard_disabling_fields(pairs: Optional[Iterable] = None) -> list:
    """GENERIC DETECTOR — re-derive the guard-disabling class BY EXECUTION and refuse any member
    that is not registered in _GUARD_DISABLING.

    `pairs` is an iterable of (label, relation, D, C, strict_spec, weak_spec, field). For each,
    the strict state must yield >= 1 fail-closed problem; if the weak state yields ZERO, that
    field is guard-disabling and MUST be registered (and therefore provider-backed). This is what
    makes the class closed going forward: a future lenient branch added without registering its
    field fails this audit, so a new guard-disabling field is GOVERNED OR REFUSED."""
    problems: list = []
    registered = {d["field"].split(".")[0] for d in _GUARD_DISABLING}
    # SYNTHETIC, NON-CERTIFYING (Part F). This auditor compares two INVENTED verdicts to detect a
    # lenient branch; it produces a finding about the FRAMEWORK and can never certify a collection
    # (INV-4b: nothing under adjudication reaches it). Its comparisons are therefore exempt from
    # the relation/probe totality check — which matters precisely for a relation the auditor has
    # just introduced to test it. Without the exemption both the strict and the weak run would come
    # back with the SAME probe refusal, the difference would vanish, and the auditor would go
    # silent on the very defect it exists to catch: a control disabled by a sibling control.
    with _synthetic_evaluation():
        for label, relation, D, C, strict_spec, weak_spec, field in (pairs or ()):
            strict = compare(relation, D, C, strict_spec)
            weak = compare(relation, D, C, weak_spec)
            s_fc = sum(1 for p in strict if p.get("fail_closed", True))
            w_fc = sum(1 for p in weak if p.get("fail_closed", True))
            if s_fc > 0 and w_fc == 0 and field.split(".")[0] not in registered:
                problems.append(_problem(
                    relation, "UNGOVERNED_GUARD_DISABLING_FIELD",
                    f"{label}: spec field {field!r} turns {s_fc} fail-closed "
                    "problem(s) into a CLEAN verdict but is not registered in "
                    "_GUARD_DISABLING, so no condition provider governs it; a "
                    "caller can weaken this guard from JSON at will; REFUSED"))
    return problems


# --- presence gate -------------------------------------------------------------------------
_PRESENCE_POLICIES = {"INVALID_EMPTY", "VALID_EMPTY", "CONDITIONALLY_EMPTY", "REQUIRED_NON_EMPTY"}

# Each relation declares which operand(s) MUST be non-empty to witness completeness. The presence
# gate refuses their emptiness REGARDLESS of the spec-declared operand; the spec may only WIDEN
# the guard, never narrow it. This is the general form of the OBJ-3 fix: DIFFERENTIAL_EXECUTION's
# load-bearing operand is the collection C, not the domain D, so guarding "domain" by default let
# an empty C pass vacuously. Naming the load-bearing operand per relation closes that class.
_LOAD_BEARING_OPERANDS = {
    "EXACT": {"domain"},
    "REQUIRED_SUBSET": {"domain", "collection"},   # C-D is silenced by an empty C
    "REQUIRED_SUPERSET": {"domain"},
    "DISJOINT": {"domain", "collection"},          # C&D is silenced by an empty C
    "DISJOINT_WITH_FLOOR": {"domain", "collection"},
    "PARTITION": {"domain"},
    "KEYED_MAPPING": {"domain"},
    "KEYED_MAPPING_AGAINST_UNION": {"domain"},
    "SCHEMA_STRICTNESS": {"domain"},
    "PROVENANCE_CORRESPONDENCE": {"domain", "collection"},  # injective fires on C-D
    "SEMANTIC_REACHABILITY": {"domain"},
    "DIFFERENTIAL_EXECUTION": {"collection"},   # <-- the load-bearing operand is C, not D
    "HASH_BACKSTOP": {"domain", "collection"},     # the .subset arm fires on C-D
    # The set-shaped obligation of a NON-ENUMERABLE guarantee's SOURCE operand: the declared source
    # (C) must be non-empty per the design's policy and must still contain every pinned positive
    # control (D). The load-bearing operand is the SOURCE, i.e. C — a design with no pinned
    # controls has an empty D, and that is not the thing being witnessed.
    "POSITIVE_CONTROL_PRESENCE": {"collection"},
}


def _presence(spec: dict) -> dict:
    p = spec.get("presence")
    if p is None:
        # No explicit policy: STRICT. An observed universe discovery returned empty is refused
        # unless the design SAYS empty is valid. Defaulting to lenient would reintroduce OBJ-3.
        return {"policy": "INVALID_EMPTY", "operand": None, "floor": []}
    if isinstance(p, str):
        return {"policy": p, "operand": None, "floor": []}
    if isinstance(p, dict):
        return {"policy": p.get("policy", "INVALID_EMPTY"),
                "operand": p.get("operand"),
                "floor": p.get("floor", []),
                "empty_condition_met": p.get("empty_condition_met")}
    raise FrameworkError(f"presence must be str|dict|None, got {type(p).__name__}")


def _gate_presence(relation: str, D: set, C: set, spec: dict, problems: list,
                   condition: str = "EMPTY_LOAD_BEARING_OPERAND_LEGITIMATE") -> bool:
    """Positive-presence gate, run BEFORE the relation. Returns True if the relation may proceed,
    False if presence refused (and appended a Problem). The guarded operand set is the relation's
    LOAD-BEARING operands UNIONED with any spec-declared operand — spec may widen, never narrow."""
    pol = _presence(spec)
    policy = pol["policy"]
    if policy not in _PRESENCE_POLICIES:
        problems.append(_problem(relation, "UNKNOWN_PRESENCE_POLICY",
                                 f"presence policy {policy!r} is not one of "
                                 f"{sorted(_PRESENCE_POLICIES)}; fail-closed"))
        return False

    guarded = set(_LOAD_BEARING_OPERANDS.get(relation, {"domain"}))
    declared = pol["operand"]
    if declared is not None:
        expand = {"domain": {"domain"}, "collection": {"collection"},
                  "both": {"domain", "collection"}}.get(declared)
        if expand is None:
            problems.append(_problem(relation, "MALFORMED_SPEC",
                                     f"presence.operand {declared!r} must be domain|collection|"
                                     "both"))
            return False
        guarded |= expand

    operand_by_name = {"domain": ("expected_domain", D), "collection": ("collection", C)}
    ok = True

    # FLOOR: every floor member must be present in the observed domain, and the domain must be
    # non-empty. This is the OBJ-3 core: '.py' must appear in the text-suffix universe or the
    # DISJOINT verdict is untrustworthy and REFUSED.
    floor = pol["floor"] or []
    norm = _normalizer(spec)
    if floor:
        fset = {norm(m) for m in floor}
        if not D:
            problems.append(_problem(relation, "EMPTY_OBSERVED_UNIVERSE",
                                     "the observed universe is EMPTY but a floor was declared; "
                                     f"discovery returned nothing, floor {sorted(fset)} absent; "
                                     "REFUSED (a broken/empty discovery must not pass vacuously)"))
            return False
        for m in sorted(fset - D):
            problems.append(_problem(relation, "FLOOR_MEMBER_ABSENT",
                                     f"floor member {m!r} is NOT in the observed universe; "
                                     "discovery is incomplete or broken; REFUSED", member=m))
            ok = False

    for name in sorted(guarded):
        label, operand = operand_by_name[name]
        if operand:
            continue
        if policy in ("INVALID_EMPTY", "REQUIRED_NON_EMPTY"):
            problems.append(_problem(relation, "EMPTY_OPERAND_REFUSED",
                                     f"{label} is EMPTY under presence={policy}; an empty "
                                     "load-bearing operand cannot witness completeness and must "
                                     "not pass vacuously; REFUSED"))
            ok = False
        else:
            # P5 / BYP-1. policy is VALID_EMPTY or CONDITIONALLY_EMPTY — the two states in which
            # the caller's JSON, and nothing else, switches the load-bearing-operand presence
            # class off, for EVERY relation. Both are guard-disabling fields: the empty operand is
            # honoured ONLY when a REGISTERED condition provider COMPUTES that the emptiness is a
            # true property of the authority.
            #
            # The pre-existing CONDITIONALLY_EMPTY literal check is KEPT and runs FIRST, so this
            # patch is strictly additive: every spec refused before is still refused, and the
            # literal now has to survive a computed condition on top of it rather than instead of
            # it. Authorization costs nothing on a non-empty operand — the `if operand: continue`
            # above means it is demanded exactly when the disable is actually doing work.
            if policy == "CONDITIONALLY_EMPTY" and pol.get("empty_condition_met") is not True:
                problems.append(_problem(relation, "EMPTY_OPERAND_UNJUSTIFIED",
                                         f"{label} is EMPTY and presence=CONDITIONALLY_EMPTY but "
                                         "the declared empty-condition is not met "
                                         "(empty_condition_met is not True); REFUSED"))
                ok = False
                continue
            # P4. WHICH guard-disabling condition this is depends on the LAYER the evaluation
            # entered from, and the layer arrives as an INTERNAL keyword (like _depth and _path) —
            # never as a spec field, so no fixture can select its own condition. A non-enumerable
            # source adjudicated at the gate is authorized under the condition Part D has always
            # demanded: routing Part D through the gate unified the presence semantics, and must
            # not silently invent a SECOND condition every existing design would have to register
            # a provider for.
            if not authorize_guard_disable(
                    condition, spec,
                    {"cid": spec.get("source_collection_id", "<collection>"),
                     "layer": "comparator", "relation": relation, "field": "presence",
                     "declared_state": policy, "operand": label, "operand_members": (),
                     "operand_empty": True, "suppressed": ()},
                    problems, relation=relation):
                ok = False
    return ok


# --- P4: RELATION-TO-RELATION EDGES RE-ENTER THE GATE ----------------------------------------
# A relation that needs another relation may NOT reach into _REGISTRY and call a checker: that is
# BYP-2, and it is how KEYED_MAPPING / KEYED_MAPPING_AGAINST_UNION adjudicated per-key VALUE
# operands the presence gate had never seen — an empty value universe measured CLEAN. Every edge
# now RE-ENTERS compare(), so the presence gate (and the normalizer, and the duplicate-collision
# detector) runs on EVERY operand pair at EVERY depth.
#
# There are exactly TWO kinds of edge and they derive their sub-spec differently:
#   RECURSION  (_recurse) — the child adjudicates NEW operands the top-level gate never saw
#                           (KEYED_MAPPING's key set and per-key value sets, KMAU's value union).
#                           Its sub-spec is CLOSED: only `normalize` is inherited, everything else
#                           comes from a named block (key_spec / value_spec), so a parent field can
#                           never silently satisfy a child relation's witness requirement.
#   DELEGATION (_delegate) — the child adjudicates the SAME operands under a narrower relation
#                           (SCHEMA_STRICTNESS / PROVENANCE_CORRESPONDENCE -> EXACT, HASH_BACKSTOP
#                           -> REQUIRED_SUBSET, DISJOINT_WITH_FLOOR -> DISJOINT). The gate already
#                           ran on these operands, so the sub-spec inherits the presence POLICY (a
#                           design's justified emptiness must not be re-refused) but DROPS the
#                           floor, which was already witnessed and must not be reported twice.
_MAX_RELATION_DEPTH = 6

# Declared delegation edges. A delegation is only a no-op re-gate if the child's load-bearing
# operands are not WIDER than the parent's; the P4 battery asserts that for every edge here, so a
# future delegation to a relation with different load-bearing semantics cannot land silently.
_DELEGATIONS = {
    "DISJOINT_WITH_FLOOR": ("DISJOINT",),
    "SCHEMA_STRICTNESS": ("EXACT",),
    "PROVENANCE_CORRESPONDENCE": ("EXACT", "REQUIRED_SUPERSET"),
    "HASH_BACKSTOP": ("REQUIRED_SUBSET",),
}

_SUBSPEC_INHERITED = ("normalize",)
_SUBSPEC_PRESENCE_DEFAULTS = {
    # keys: guard the relation's own load-bearing operands only, so an empty collection still
    # yields per-key MISSING findings (informative) rather than a single refusal.
    "key_spec": {"policy": "INVALID_EMPTY"},
    # values: guard BOTH operands. An empty value universe on either side cannot witness the
    # mapping relation, and under REQUIRED_SUBSET (the default value relation) an empty collection
    # operand passes vacuously — that is BYP-2 itself.
    "value_spec": {"policy": "INVALID_EMPTY", "operand": "both"},
}


def _subspec(spec: dict, block: str) -> dict:
    """Closed sub-spec for a NEW-operand sub-relation. Inherits only the element normalizer; every
    other field must be declared in the named block.

    The two blocks are read by LITERAL key rather than through the `block` variable. That is not
    style: P3's totality proof derives the spec keys the code reads by AST, and a key read only
    through a variable would be invisible to it — so `key_spec`/`value_spec` could be declared in
    the manifest without the manifest ever being checked against a real read (I5's dead-entry
    shape). Reading them literally keeps both P3 directions honest."""
    sub = {k: spec[k] for k in _SUBSPEC_INHERITED if k in spec}
    if block == "key_spec":
        declared = spec.get("key_spec")
    elif block == "value_spec":
        declared = spec.get("value_spec")
    else:
        raise FrameworkError(f"unknown sub-spec block {block!r}")
    if declared is not None:
        if not isinstance(declared, dict):
            raise FrameworkError(f"{block} must be a dict, got {type(declared).__name__}")
        sub.update(declared)
    if "presence" not in sub:
        sub["presence"] = dict(_SUBSPEC_PRESENCE_DEFAULTS[block])
    return sub


def _delegate_spec(spec: dict) -> dict:
    """Sub-spec for a SAME-operand delegation: inherit the normalizer and the presence POLICY,
    drop the floor (already witnessed by the top-level gate; re-reporting it double-counts)."""
    sub = {k: spec[k] for k in _SUBSPEC_INHERITED if k in spec}
    pol = _presence(spec)
    presence = {"policy": pol["policy"]}
    if pol.get("operand") is not None:
        presence["operand"] = pol["operand"]
    if pol.get("empty_condition_met") is not None:
        presence["empty_condition_met"] = pol["empty_condition_met"]
    sub["presence"] = presence
    return sub


def _reenter(child: str, D, C, sub: dict, problems: list, label: str, depth: int) -> None:
    """THE ONLY WAY one relation reaches another. Re-enters compare() so the presence gate runs on
    this operand pair, then re-labels the child's problems with the parent position so a finding
    still names where in the mapping it fired. Bounded: an unbounded sub-relation chain is REFUSED,
    never recursed."""
    if depth + 1 > _MAX_RELATION_DEPTH:
        problems.append(_problem(label, "RELATION_DEPTH_EXCEEDED",
                                 f"sub-relation nesting exceeded {_MAX_RELATION_DEPTH}; REFUSED "
                                 "rather than recursed without bound"))
        return
    child_canonical = resolve_relation(child) or child
    for p in compare(child, D, C, sub, _depth=depth + 1):
        # A problem raised AT the child level is re-labelled with the caller's positional label. A
        # problem that arrived from a DEEPER level already carries its own composed position, so it
        # is prefixed rather than overwritten — the chain stays legible at any depth.
        p["relation"] = label if p["relation"] == child_canonical else f"{label}->{p['relation']}"
        problems.append(p)


def _recurse(child: str, D, C, spec: dict, problems: list, label: str, depth: int,
             block: str) -> None:
    try:
        sub = _subspec(spec, block)
    except FrameworkError as exc:
        problems.append(_problem(label, "MALFORMED_SPEC", str(exc)))
        return
    _reenter(child, D, C, sub, problems, label, depth)


def _delegate(child: str, D, C, spec: dict, problems: list, label: str, depth: int) -> None:
    _reenter(child, D, C, _delegate_spec(spec), problems, label, depth)


# --- relations -----------------------------------------------------------------------------
def _rel_exact(D, C, spec, problems, relation="EXACT", _depth: int = 0):
    for m in sorted(D - C):
        problems.append(_problem(relation, "MISSING",
                                 f"domain member {m!r} is NOT in the collection", member=m))
    for m in sorted(C - D):
        problems.append(_problem(relation, "UNKNOWN",
                                 f"collection member {m!r} is NOT in the domain", member=m))


def _rel_required_subset(D, C, spec, problems, relation="REQUIRED_SUBSET", _depth: int = 0):
    for m in sorted(C - D):
        problems.append(_problem(relation, "UNJUSTIFIED",
                                 f"collection member {m!r} is NOT justified by the authority "
                                 "domain (C - D must be empty)", member=m))


def _rel_required_superset(D, C, spec, problems, relation="REQUIRED_SUPERSET", _depth: int = 0):
    for m in sorted(D - C):
        problems.append(_problem(relation, "REQUIRED_ABSENT",
                                 f"required member {m!r} (from the authority) is NOT in the "
                                 "collection; removing it fails open at the consumer", member=m))


def _rel_disjoint(D, C, spec, problems, relation="DISJOINT", _depth: int = 0):
    for m in sorted(C & D):
        problems.append(_problem(relation, "FORBIDDEN_OVERLAP",
                                 f"member {m!r} appears in BOTH operands; the disjointness the "
                                 "security rule depends on is violated", member=m))


def _rel_disjoint_with_floor(D, C, spec, problems, relation="DISJOINT_WITH_FLOOR", _depth: int = 0):
    # A DISJOINT_WITH_FLOOR with no floor is just DISJOINT mislabeled and its whole point (that
    # discovery actually found the universe) is unwitnessed -> REFUSED.
    pol = _presence(spec)
    if not (pol.get("floor") or []):
        problems.append(_problem(relation, "FLOOR_MISSING",
                                 "DISJOINT_WITH_FLOOR declares no floor; without a floor it "
                                 "silently degrades to an unguarded DISJOINT and cannot witness "
                                 "that discovery found the universe; REFUSED"))
        return
    _delegate("DISJOINT", D, C, spec, problems, relation, _depth)


def _rel_partition(D, C, spec, problems, relation="PARTITION", _depth: int = 0):
    # The comparator WITNESSES overlap itself from the raw member sets; it does not trust a
    # caller-supplied overlaps list (which, empty, hid a real violation). partition_members is a
    # list of the individual member collections. The union is computed here, not accepted from C.
    members = spec.get("partition_members")
    if not isinstance(members, (list, tuple)) or not members:
        problems.append(_problem(relation, "PARTITION_UNWITNESSED",
                                 "PARTITION requires spec.partition_members (the raw member "
                                 "sets); absent -> overlap cannot be witnessed -> REFUSED"))
        return
    norm = _normalizer(spec)
    normed: list[set] = []
    for i, ms in enumerate(members):
        s = _as_normalized_set(f"partition_members[{i}]", ms, norm, relation, problems)
        if s is None:
            return
        normed.append(s)
    union: set = set()
    for i in range(len(normed)):
        for j in range(i + 1, len(normed)):
            for m in sorted(normed[i] & normed[j]):
                problems.append(_problem(relation, "OVERLAP",
                                         f"member sets {i} and {j} both claim {m!r}; a partition "
                                         "must tile without overlap", member=m))
        union |= normed[i]
    for m in sorted(D - union):
        problems.append(_problem(relation, "PARTITION_SHORT",
                                 f"domain member {m!r} is claimed by NO partition member",
                                 member=m))
    for m in sorted(union - D):
        problems.append(_problem(relation, "PARTITION_OVER",
                                 f"partition claims {m!r} which the domain does not contain",
                                 member=m))


def _rel_keyed_mapping(D, C, spec, problems, relation="KEYED_MAPPING", _depth: int = 0):
    if not isinstance(D, dict) or not isinstance(C, dict):
        problems.append(_problem(relation, "MALFORMED_OPERAND",
                                 "KEYED_MAPPING requires both operands to be dict key->set"))
        return
    key_rel = resolve_relation(spec.get("key_relation", "EXACT"))
    val_rel = resolve_relation(spec.get("value_relation", "REQUIRED_SUBSET"))
    if key_rel is None or val_rel is None:
        problems.append(_problem(relation, "UNKNOWN_SUBRELATION",
                                 f"key_relation={spec.get('key_relation')!r} "
                                 f"value_relation={spec.get('value_relation')!r}; unknown "
                                 "sub-relation fails closed"))
        return
    # P4/BYP-2: RE-ENTER compare() for the key set and for EVERY per-key value pair. Reaching
    # into _REGISTRY for a checker (what this did) skipped the presence gate, the normalizer and
    # the duplicate-collision detector on operands the top-level gate had never seen, so a mapping
    # whose value universes were all EMPTY measured CLEAN.
    dk, ck = set(D), set(C)
    _recurse(key_rel, dk, ck, spec, problems, f"{relation}.keys[{key_rel}]", _depth, "key_spec")
    for k in sorted(dk & ck):
        dv, cv = D[k], C[k]
        if not isinstance(dv, (set, frozenset)) or not isinstance(cv, (set, frozenset)):
            problems.append(_problem(relation, "MALFORMED_VALUE",
                                     f"value for key {k!r} is not a set on both sides", member=k))
            continue
        _recurse(val_rel, set(dv), set(cv), spec, problems,
                 f"{relation}[{k!r}].value[{val_rel}]", _depth, "value_spec")


def _rel_keyed_mapping_against_union(D, C, spec, problems,
                                     relation="KEYED_MAPPING_AGAINST_UNION", _depth: int = 0):
    ck = set(C) if isinstance(C, dict) else set(C)
    _recurse("EXACT", D, ck, spec, problems, f"{relation}.keys", _depth, "key_spec")
    # P7-FIND-01 (sibling): value_domain is the observed value universe. Authored in data it is a
    # self-certifying answer, so it must arrive through the P7 gate.
    value_domain, has_domain = witness_field(spec, "value_domain", relation, problems)
    if "value_domain" in spec and not has_domain:
        return          # refused as an unwitnessed answer; "no witness declared" would be a lie
    if isinstance(C, dict) and has_domain and value_domain is not None:
        vdom = set(value_domain)
        allvals: set = set()
        for v in C.values():
            allvals |= set(v) if isinstance(v, (set, frozenset, list, tuple)) else {v}
        # P4/BYP-2: through the gate, so the presence gate sees the value UNION. Calling
        # _rel_required_subset directly let an all-empty value universe pass vacuously (the subset
        # direction C - D is trivially empty when C is empty).
        _recurse("REQUIRED_SUBSET", vdom, allvals, spec, problems, f"{relation}.values", _depth,
                 "value_spec")
    elif isinstance(C, dict):
        # B0a-ADV17 (lesser): a dict collection carries VALUES but no value_domain witness was
        # declared, so the values would go unchecked — a silent skip where PARTITION,
        # SCHEMA_STRICTNESS and DISJOINT_WITH_FLOOR all REFUSE on a missing witness. Refuse here too.
        #
        # P1 (W09). The refusal used to be conditional on `any(C.values())`, which made the GUARD'S
        # ACTIVATION a function of the very DATA under adjudication: a mapping whose values were all
        # empty skipped both the value check AND its own missing-witness refusal. That is the
        # defect the refusal exists to catch, restated — shortening every value list to empty is
        # exactly how a keyed mapping goes hollow, and it was the one shape that certified CLEAN.
        # The value-witness requirement is a property of the RELATION (KEYED_MAPPING_AGAINST_UNION
        # adjudicates values), not of the operand it happens to be handed, so it is unconditional.
        problems.append(_problem(relation, "VALUES_UNWITNESSED",
                                 "the mapping declares no value_domain witness, so its values are "
                                 "unchecked; an all-empty-values mapping is not an exemption — it "
                                 "is the shape a hollowed mapping takes; REFUSED"))


def _rel_schema_strictness(D, C, spec, problems, relation="SCHEMA_STRICTNESS", _depth: int = 0):
    # D = tokens the real consumer ACCEPTS (probed by executing it); C = declared token set.
    _delegate("EXACT", D, C, spec, problems, relation, _depth)
    # P7-FIND-01 (sibling): a JSON `false` here certifies a closed schema with zero execution.
    accepted, probed = witness_field(spec, "unknown_probe_accepted", relation, problems)
    if "unknown_probe_accepted" in spec and not probed:
        return                                     # already refused as an unwitnessed answer
    if accepted is True:
        problems.append(_problem(relation, "SCHEMA_NOT_CLOSED",
                                 "an unknown/never-declared token was ACCEPTED by the consumer; "
                                 "the schema is open (fail-open), not closed"))
    elif "unknown_probe_accepted" not in spec:
        problems.append(_problem(relation, "STRICTNESS_UNWITNESSED",
                                 "SCHEMA_STRICTNESS requires an unknown-token rejection witness "
                                 "(spec.unknown_probe_accepted); absent -> cannot certify closed; "
                                 "REFUSED"))


def _rel_provenance_correspondence(D, C, spec, problems, relation="PROVENANCE_CORRESPONDENCE", _depth: int = 0):
    mode = spec.get("correspondence", "bijective")
    if mode == "bijective":
        _delegate("EXACT", D, C, spec, problems, relation, _depth)
    elif mode == "injective":
        # P5 (WEAKER_PATH_SELECT). `injective` is a caller-authored PATH selection that drops the
        # 'missing' direction: a provenance record with no corresponding collection member stops
        # being a finding. Derived as guard-disabling by the 1d relaxation probe (bijective ->
        # injective took a refusing context to CLEAN). Authorization is demanded only when the
        # weaker path is actually SUPPRESSING something — the strict direction is computed into a
        # scratch list first, and if it is empty the two paths agree and nothing was disabled.
        # P4: the SUPPRESSED direction is computed through the gate like any other relation
        # edge. It lands in a scratch list because P5 must decide whether it may be dropped — but
        # it is a real, gated evaluation, not a hand-rolled difference taken behind the gate's
        # back. (The transit it records is honest: these operands DID reach the gate.)
        suppressed: list = []
        _delegate("REQUIRED_SUPERSET", D, C, spec, suppressed, relation, _depth)
        if suppressed and not authorize_guard_disable(
                "PROVENANCE_MAY_BE_INJECTIVE", spec,
                {"cid": spec.get("source_collection_id", "<collection>"),
                 "layer": "comparator", "relation": relation, "field": "correspondence",
                 "declared_state": mode, "operand": "expected_domain", "operand_members": (),
                 "operand_empty": False,
                 "suppressed": tuple(sorted(p.get("member") for p in suppressed))},
                problems, relation=relation):
            problems.extend(suppressed)
        for m in sorted(C - D):
            problems.append(_problem(relation, "NO_PROVENANCE",
                                     f"collection member {m!r} has NO corresponding provenance "
                                     "record", member=m))
    else:
        problems.append(_problem(relation, "UNKNOWN_CORRESPONDENCE",
                                 f"correspondence mode {mode!r} unknown; fail-closed"))


def _rel_semantic_reachability(D, C, spec, problems, relation="SEMANTIC_REACHABILITY", _depth: int = 0):
    for m in sorted(D - C):
        problems.append(_problem(relation, "UNREACHED_REQUIRED",
                                 f"reachable node {m!r} is NOT in the collection; the walk "
                                 "under-covers (load-bearing)", member=m))
    # P5 (STRICTNESS_RELAX). `strict_reachability` absent or False downgrades every unreachable
    # declared member from a fail-closed finding to informational — a caller-authored strictness
    # relaxation, derived as guard-disabling by the 1d relaxation probe. The DEFAULT here is the
    # weak branch, so this is an OMISSION-disabler: silence must not buy leniency. Authorization
    # is demanded only when there is actually something to downgrade.
    unreachable = sorted(C - D)
    lenient = spec.get("strict_reachability") is not True
    if unreachable and lenient:
        lenient = authorize_guard_disable(
            "UNREACHABLE_DECLARED_MEMBER_LEGITIMATE", spec,
            {"cid": spec.get("source_collection_id", "<collection>"),
             "layer": "comparator", "relation": relation, "field": "strict_reachability",
             "declared_state": spec.get("strict_reachability"), "operand": "collection",
             "operand_members": (), "operand_empty": False, "suppressed": tuple(unreachable)},
            problems, relation=relation)
    for m in unreachable:
        if not lenient:
            problems.append(_problem(relation, "UNREACHABLE_DECLARED",
                                     f"declared member {m!r} is not reachable; a dead member "
                                     "inflating the coverage claim", member=m))
        else:
            problems.append(_problem(relation, "UNREACHABLE_DECLARED_INFO",
                                     f"declared member {m!r} is not reachable (informational)",
                                     member=m, fail_closed=False))


def _rel_differential_execution(D, C, spec, problems, relation="DIFFERENTIAL_EXECUTION", _depth: int = 0):
    # Each required member must produce an OBSERVABLE behavioural delta at the consumer. The
    # caller executes the probe and passes spec['member_effect']: {member: bool}. A member with NO
    # delta is INERT and a finding. The load-bearing operand is C (guarded non-empty by the
    # presence gate via _LOAD_BEARING_OPERANDS), so an empty C can no longer pass vacuously.
    # P7-FIND-01 (siblings): `{m: True for m in C}` certifies every member load-bearing with zero
    # execution, and a JSON `true` certifies a baseline nobody ran. Both must be executed evidence.
    effects, probed = witness_field(spec, "member_effect", relation, problems)
    if "member_effect" in spec and not probed:
        return
    if not isinstance(effects, dict):
        problems.append(_problem(relation, "NO_MEMBER_EVIDENCE",
                                 "DIFFERENTIAL_EXECUTION requires spec.member_effect "
                                 "{member: observed_delta_bool}; absent -> REFUSED"))
        return
    healthy, certified = witness_field(spec, "baseline_healthy", relation, problems)
    if "baseline_healthy" in spec and not certified:
        return
    if healthy is not True:
        problems.append(_problem(relation, "BASELINE_UNHEALTHY",
                                 "the unmutated baseline is not certified healthy "
                                 "(baseline_healthy != True); per-member verdicts invalid; "
                                 "REFUSED"))
        return
    norm = _normalizer(spec)
    keyed: dict = {}
    for k, v in effects.items():
        nk = norm(k)
        if nk in keyed and keyed[nk] != v:
            # A normalization collision that FLIPS a member's verdict cannot be resolved by
            # insertion order; refuse rather than let order decide load-bearingness.
            problems.append(_problem(relation, "EFFECT_KEY_COLLISION",
                                     f"member_effect keys collide on {nk!r} with conflicting "
                                     "deltas; the per-member verdict is ambiguous; REFUSED",
                                     member=nk))
            return
        keyed[nk] = v
    for m in sorted(C):
        if m not in keyed:
            problems.append(_problem(relation, "MEMBER_UNPROBED",
                                     f"collection member {m!r} was never probed for an effect; "
                                     "cannot certify it is load-bearing; REFUSED", member=m))
        elif keyed[m] is not True:
            problems.append(_problem(relation, "INERT_MEMBER",
                                     f"collection member {m!r} produced NO observable delta; it "
                                     "is inert and its presence is unwitnessed", member=m))


def _rel_hash_backstop(D, C, spec, problems, relation="HASH_BACKSTOP", _depth: int = 0):
    # Open-world sets get completeness from a CONTENT HASH of the observed universe pinned to a
    # reviewed baseline, plus REQUIRED_SUBSET of C into the observed universe D. The observed
    # digest is ALWAYS recomputed from D; a caller-supplied digest is never trusted to skip drift
    # detection (that was the fail-open). If supplied and it disagrees, that itself is a refusal.
    _delegate("REQUIRED_SUBSET", D, C, spec, problems, f"{relation}.subset", _depth)
    baseline = spec.get("baseline_hash")
    if baseline is None:
        problems.append(_problem(relation, "NO_BASELINE_HASH",
                                 "HASH_BACKSTOP requires spec.baseline_hash (a reviewed pin); "
                                 "absent -> REFUSED (an open-world set with no pin is unbounded)"))
        return
    canon = json.dumps(sorted(str(x) for x in D), separators=(",", ":"))
    observed = hashlib.sha256(canon.encode("utf-8")).hexdigest()
    supplied = spec.get("observed_hash")
    if supplied is not None and supplied != observed:
        problems.append(_problem(relation, "SUPPLIED_HASH_MISMATCH",
                                 f"a caller-supplied observed_hash {str(supplied)[:16]}... does "
                                 f"not match the digest recomputed from the observed universe "
                                 f"{observed[:16]}...; a supplied digest is never authoritative; "
                                 "REFUSED"))
    if observed != baseline:
        problems.append(_problem(relation, "BASELINE_DRIFT",
                                 f"observed universe digest {observed[:16]}... != reviewed "
                                 f"baseline {str(baseline)[:16]}...; the universe drifted and the "
                                 "subset verdict is stale; REFUSED"))


def _rel_positive_control(D, C, spec, problems, relation="POSITIVE_CONTROL_PRESENCE",
                          _depth: int = 0):
    """D = the pinned positive-control members; C = the authoritative source. Every control must
    still be present in the source.

    This is the SET-SHAPED half of what the non-enumerable layer used to enforce in its own
    hand-written enforce_positive_presence(): a second implementation of the presence semantics,
    with its own policy vocabulary, living outside the gate — the parallel-evaluator shape P4
    exists to remove. It is a relation of its OWN rather than a delegation to REQUIRED_SUPERSET
    because its load-bearing operand is the COLLECTION (the source), not the domain: a design may
    legitimately pin no control members, but an empty SOURCE is exactly what must not pass."""
    for m in sorted(D - C):
        problems.append(_problem(relation, "POSITIVE_CONTROL_ABSENT",
                                 f"pinned positive-control member {m!r} is absent from the "
                                 "authoritative source; the set was silently shortened", member=m))


# --- registry ------------------------------------------------------------------------------
_REGISTRY: dict[str, tuple] = {
    "EXACT": (_rel_exact, "set", "set"),
    "REQUIRED_SUBSET": (_rel_required_subset, "set", "set"),
    "REQUIRED_SUPERSET": (_rel_required_superset, "set", "set"),
    "DISJOINT": (_rel_disjoint, "set", "set"),
    "DISJOINT_WITH_FLOOR": (_rel_disjoint_with_floor, "set", "set"),
    "PARTITION": (_rel_partition, "set", "set"),
    "KEYED_MAPPING": (_rel_keyed_mapping, "dict", "dict"),
    "KEYED_MAPPING_AGAINST_UNION": (_rel_keyed_mapping_against_union, "set", "dict"),
    "SCHEMA_STRICTNESS": (_rel_schema_strictness, "set", "set"),
    "PROVENANCE_CORRESPONDENCE": (_rel_provenance_correspondence, "set", "set"),
    "SEMANTIC_REACHABILITY": (_rel_semantic_reachability, "set", "set"),
    "DIFFERENTIAL_EXECUTION": (_rel_differential_execution, "set", "set"),
    "HASH_BACKSTOP": (_rel_hash_backstop, "set", "set"),
    "POSITIVE_CONTROL_PRESENCE": (_rel_positive_control, "set", "set"),
}

_ALIASES = {
    "AUTHORITATIVE_SUPERSET": "REQUIRED_SUPERSET",
    "ASYMMETRIC": "REQUIRED_SUPERSET",
    "SCHEMA_SUPERSET": "REQUIRED_SUPERSET",
    "SUPERSET": "REQUIRED_SUPERSET",
    "FLOOR": "REQUIRED_SUPERSET",
    "AUTHORITATIVE_MUST_JUSTIFY": "REQUIRED_SUBSET",
    "SUBSET": "REQUIRED_SUBSET",
    "SUBSET_COVERAGE": "REQUIRED_SUBSET",
    "CLOSED_WORLD": "REQUIRED_SUBSET",
    "CLOSED_SCHEMA_UNKNOWN_REJECTED": "SCHEMA_STRICTNESS",
    "CLOSED_SET_NO_FAILING_MEMBER": "SCHEMA_STRICTNESS",
    "DISCOVERED_KINDS": "EXACT",
    "SET_EQUALITY": "EXACT",
}

RELATIONS = tuple(_REGISTRY)


def resolve_relation(name: str) -> Optional[str]:
    if name in _REGISTRY:
        return name
    return _ALIASES.get(name)



# ============================================================================================
# PART A3 — P3: CLOSED SPEC SCHEMA (the witness-form namespace)
# ============================================================================================
# WHY. Properties P1/P2 are quantified over GUARDS and WITNESSES. They are NOT quantified over the
# SPEC SCHEMA: before this block the spec was OPEN-WORLD over fields — an unrecognised key was
# silently ignored, so a novel witness-bearing shape could be introduced without registering
# anything (Agent-6 BYP-5, "why an eleventh will always exist until P3 lands"). P3 closes that
# namespace: EVERY form (comparator relation, non-enumerable guarantee kind, provider/framework
# kind, and each NESTED witness object) DECLARES its permitted field manifest, and any key outside
# the union manifest for the declared form is REFUSED. The precedent is `_normalizer`, which
# already refuses an unknown directive rather than ignoring it; P3 is that same rule applied to the
# spec's field namespace instead of one field's value namespace.
#
# THE INVARIANT THIS BUYS: **a new witness form is always a new field**, and a new field is always
# a MANIFEST EDIT — i.e. a governed schema evolution, reviewable as a diff, never an emergent
# property of a JSON fixture.
#
# NON-CIRCULARITY. WITNESS_FIELD_MANIFEST is an AUTHORED LITERAL. It is deliberately NOT built by
# comprehension over _REGISTRY / GUARANTEE_KINDS / FRAMEWORK_KINDS / _ALIASES — if it were, adding
# a 14th relation would extend its own permission set and govern nothing. It is a SECOND,
# independently-authored namespace that the closure tests require to AGREE with the instance
# registries (equality both directions) and with the AST-derived set of spec keys the code actually
# READS. Agreement is CHECKED, never CONSTRUCTED. A novel form must therefore satisfy three
# independent authorities at once: the registry, the manifest, and the code's own read-set.
#
# OPERAND SEMANTICS ARE PINNED, NOT MERELY DECLARED (Agent-6's 14th-relation residual). Each
# relation's manifest entry carries `load_bearing_operands` and `directions` as VALUES. The closure
# battery requires them to equal _LOAD_BEARING_OPERANDS / _RELATION_DIRECTIONS **and** to cover the
# operand set COMPUTED from the checker's own behaviour (see computed_load_bearing()). A 14th
# C-load-bearing relation that declares {"domain"} therefore fails on the computed check, which is
# what the earlier battery could not do: it forced a declaration but never a CORRECT one.

# --- field descriptors ----------------------------------------------------------------------
# A field descriptor is (python_type_name, allowed_values_or_None). The value domain is pinned
# where the design has a closed one, so a field cannot be declared and then carry an arbitrary
# payload — "declared" must mean "declared WITH its semantics".
_T_STR = ("str", None)
_T_BOOL = ("bool", None)
_T_INT = ("int", None)
_T_LIST = ("list", None)
_T_DICT = ("dict", None)
_T_SETLIKE = ("setlike", None)
_T_ANY = ("any", None)
# SELF-ADEQUACY witness fields. Each names the base type the RELATION reads, or the
# CodeNativeWitness whose execution yields it — see the _witnessed() note at _TYPE_CHECKS for why
# the type gate must admit both and leave "authored vs executed" to P7's own detector.
_T_W_BOOL = ("witnessed_bool", None)          # baseline_healthy, unknown_probe_accepted
_T_W_DICT = ("witnessed_dict", None)          # member_effect
_T_W_SETLIKE = ("witnessed_setlike", None)    # value_domain, observed (provider/framework forms)
_T_WITNESS = ("witness", None)                # observed on a RELATION-only form: witness ONLY
# The framework-internal vetting marker; the value is a module-private sentinel, never authorable.
_T_VETTED = ("vetted_marker", None)


# ============================================================================================
# P9 IDENTITY / PROVENANCE FIELD GOVERNANCE  (B0w-R Agent-8: the P3 x P5 x P9 interlock)
# ============================================================================================
# WHY THIS BLOCK EXISTS. P9 (witness/provider callable IDENTITY + PROVENANCE, the I28AB plugin-
# identity shape) is the one namespace where a spec field can plausibly be read as "you may now
# trust this, so stop checking". A `trusted_provider: true` or `skip_identity_verification` key
# would be an ungoverned TRUST OVERRIDE: a caller-authored JSON literal that disables a fail-closed
# verifier. That is BYP-1 (P5's class) re-opened one namespace over, and it would make P6's
# behavioural instrumentation and P9's own binding optional at the caller's discretion.
#
# THE RESULT OF THE P9 COMPOSITION CONTRACT (Agent-1, B0w-R):
#
#     **P9 introduces ZERO new PERMITTED spec fields.**
#
# Agent-8's first draft manifested a `witness_identity` bundle (provider_module / provider_qualname
# / provider_sha256) as a pinned-type P9 namespace. Agent-1's contract rejected that direction and
# was right to: those are precisely the shapes an attacker uses to HAND P9 an identity. Identity is
# RECOMPUTED from the registry-side authored manifest (WITNESS_PROVIDER_MANIFEST) and never
# supplied by a spec, so a supplied digest is at best redundant and at worst forged. The draft
# namespace is withdrawn; see B0wR-A8-FIND-03 for the reversal.
#
# ####################################################################################
# THIS BLOCK IS NOT SUFFICIENT ON ITS OWN AND DOES NOT CLOSE GAP-1.  (B0wR-A8-FIND-08)
# ####################################################################################
# It governs the SPEC surface: no identity-shaped field can enter a spec. It does NOT bind the
# CALLABLE. The recomputation this block's reasoning depends on — WITNESS_PROVIDER_MANIFEST, the
# no-override WitnessRegistry, p9_execute_witness — lives in agent-1's p9.patch and EXISTS ONLY
# WHEN THAT PATCH IS ALSO APPLIED.
#
# Why that matters more than a missing feature usually would: refusing a caller-supplied digest is
# sound ONLY BECAUSE identity is recomputed somewhere else. With no recomputation anywhere, the
# refusal removes the caller's forgeable claim and puts NOTHING in its place — strictly worse than
# the honest gap, because the gap now looks handled. Agent-1 reproduced exactly this against a
# build carrying this block alone: `register_provider('x', evil)` and `PROVIDERS['x'] = evil` were
# both ACCEPTED underneath ~340 lines of governance asserting they could not be.
#
# So the dependency is enforced, not documented: _p9_callable_binding_present() probes for the
# binding side by two independent names, and a spec that NAMES A PROVIDER is REFUSED with
# P9_CALLABLE_BINDING_ABSENT when it is missing. In a build where agent-1's patch is applied the
# check is inert. A comment claiming a co-requisite is a comment; a refusal is a co-requisite.
#
# P9 READS two keys P3 already governs — `provider` and `independent_observed_source_or_witness`
# (and its nested `provider`) — and introduces none of its own. So this block adds NO permitted
# field. It adds two REFUSED CLASSES and the structural machinery that keeps them closed:
#
#   L1  DECLARATION (P3, pre-existing).  An unknown key is UNDECLARED_WITNESS_FIELD. Because P9
#       adds no permitted field, EVERY P9-shaped key is by construction outside the manifest and
#       already refused. L2-L4 exist because "already refused" is not the same as "cannot become
#       permitted" — the layers below are what stop that from changing.
#
#   L2  SHAPE (structural, not a name list).  Rules that fire on the FORM of a key, so they cover
#       fields nobody has thought of yet:
#         R1  a BOOLEAN in identity/trust vocabulary is an ASSERTION OF TRUST, not EVIDENCE.
#         R2  a key combining a NEGATION or ALTERNATE-SOURCE word (skip/disable/bypass/override/
#             ignore/unpinned/unverified, alternate/alt/fallback/backup/secondary) with an IDENTITY
#             word is a trust override or a second, unpinned source of truth.
#         R3  a key combining a DIGEST word (sha256/digest/checksum/hash) — or the word `identity`
#             — with a PRINCIPAL word (provider/witness) is a SUPPLIED IDENTITY: the caller
#             handing P9 the answer P9 is supposed to recompute.
#
#   L3  THE TWO NAMED CLASSES (Agent-1's exact names and kinds).  Refused ON PRESENCE — the VALUE
#       is NEVER consulted, because consulting it would make a falsy value a bypass
#       (`skip_identity_verification: false` must refuse exactly as `true` does; Agent-1's arms
#       A-22.3/A-22.4 pin this). Checked BEFORE the manifest lookup, so adding one of these names
#       to a manifest — the obvious "fix" a maintainer reaches for when L1 refuses their field —
#       does NOT make it acceptable. L3 is the only layer whose job is to survive a well-meaning
#       manifest edit.
#
#   L4  BY EXECUTION (the closure).  audit_identity_fields_are_obligation_only() re-derives the
#       class the way Agent-4 derived P5's: for (strict, strict+identity-field) spec pairs, if the
#       identity field turns >=1 fail-closed problem into ZERO, it is refused REGARDLESS OF ITS
#       NAME. L1-L3 are names and shapes; L4 is the property.
#
# RELATIONSHIP TO AGENT-1'S OWN ENFORCEMENT. Agent-1 checks both classes inside the LAYER
# (verify_provider / verify_non_enumerable) so that calling a layer directly cannot dodge them.
# This block checks them at the P3 SCHEMA GATE, which compare() / verify_provider() / the
# non-enumerable layer all pass through. The two are independent and deliberately redundant: a
# refusal that depends on a single call site is one refactor away from being skipped.
#
# AND THE NEGATIVE INVARIANTS (checked, never assumed):
#   * NO identity field is registered in _GUARD_DISABLING — an identity field is never a P5
#     condition-backed disable, because there is no evidence that could justify not verifying
#     identity. (P5 governs weakenings that CAN be legitimate; identity verification cannot.)
#   * NO identity-verification condition exists in _CONDITIONS, so `condition_providers` can never
#     name a provider that authorizes skipping P9 — the condition namespace is pinned and closed.
#   * NO name in either refused class appears in ANY manifest.
#   Therefore identity/provenance metadata cannot disable P6 or P9: there is no permitted field,
#   and no condition, through which such a disable could be expressed.

# --- L3: the two REFUSED classes -----------------------------------------------------------
# B0wR-A8-FIND-07. Agent-8's patch also authored _P9_IDENTITY_DISABLING_FIELDS and
# _P9_SUPPLIED_IDENTITY_FIELDS here. Agent-1's P9 patch authors the same two names further
# down, and Python keeps the LAST binding — so agent-8's copy was silently shadowed. The
# contents were identical, so nothing misbehaved, but two authored copies of one contract is
# a latent divergence: editing either alone would silently change only one consumer.
# Agent-8's copy is DELETED; agent-1 owns the contract and its definition is the single
# source both the deny scan (schema gate) and p9_guard_supplied_identity (layer) read.
# test_SINGLE_DEFINITION_of_each_refused_class asserts there is exactly one of each.

# --- L1: the condition-provider namespace (P3 x P5 composition) ------------------------------
# P5 reads spec["condition_providers"], a key P3's manifest did not declare, so under composition
# EVERY P5-governed disable was refused as UNDECLARED_WITNESS_FIELD and P5's provider machinery
# was unreachable (B0wR-A8-FIND-01). Declaring it here restores P5's governed path AND pins its
# key domain to the closed condition set, so `condition_providers` cannot itself become a channel
# for naming a novel, unregistered condition — in particular no identity condition. AUTHORED
# LITERAL; the battery checks it EQUALS set(_CONDITIONS) — checked, never constructed.
_MANIFEST_CONDITION_PROVIDERS = {
    "required": {},
    "optional": {
        "EMPTY_LOAD_BEARING_OPERAND_LEGITIMATE": _T_STR,
        "EMPTY_AUTHORITATIVE_SOURCE_LEGITIMATE": _T_STR,
        "PROVENANCE_MAY_BE_INJECTIVE": _T_STR,
        "UNREACHABLE_DECLARED_MEMBER_LEGITIMATE": _T_STR,
        "TRIANGULATION_FLOOR_MAY_BE_LOWERED": _T_STR,
    },
}

# --- L2: the SHAPE lexicons -------------------------------------------------------------------
# Matched WORD-WISE against the key split on non-alphanumerics (plus a trailing-s/-ed stem), never
# by substring: `normalize` must not match "no", `providers` must match "provider".
_P9_IDENTITY_WORDS = frozenset({
    "identity", "identities", "provenance", "trust", "trusted", "verify", "verification",
    "verified", "attest", "attestation", "signature", "signed", "hash", "sha256", "checksum",
    "digest", "provider", "witness", "registry", "registered", "binding", "bound", "pinned",
})
_P9_NEGATION_WORDS = frozenset({
    "skip", "disable", "bypass", "override", "ignore", "unchecked", "nocheck", "without",
    "suppress", "relax", "waive", "exempt", "unpinned", "unverified", "untrusted", "allow",
})
_P9_ALTERNATE_WORDS = frozenset({
    "alternate", "alternative", "alt", "fallback", "backup", "secondary", "spare", "other",
    "custom", "user",
})
# R3: the caller handing P9 the answer P9 recomputes.
_P9_DIGEST_WORDS = frozenset({"sha256", "digest", "checksum", "hash", "fingerprint", "identity"})
_P9_PRINCIPAL_WORDS = frozenset({"provider", "witness"})

# The NARROWER lexicon used for the CONDITION namespace only. `provenance`, `hash` and `provider`
# are deliberately absent: P5 already registers PROVENANCE_MAY_BE_INJECTIVE, a legitimate condition
# about the DIRECTION of a provenance correspondence, which has nothing to do with verifying a
# witness's identity. Using the full lexicon there produced a false IDENTITY_CONDITION_REGISTERED
# against P5's own registry. What must not exist is a condition that authorizes NOT VERIFYING —
# so the condition lexicon is the verification vocabulary, not the subject-matter vocabulary.
_P9_VERIFICATION_WORDS = frozenset({
    "identity", "identities", "trust", "trusted", "verify", "verification", "verified",
    "attest", "attestation", "signature", "signed", "sha256", "checksum", "digest",
    "binding", "bound", "pinned",
})


def _p9_words(key: str) -> set:
    """The key's words, lowercased, with a trailing-s / -ed stem added. Word-wise so that a
    lexicon entry can never match a substring of an unrelated field (`no` in `normalize`)."""
    out = set()
    word = ""
    for ch in str(key).lower():
        if ch.isalnum():
            word += ch
        elif word:
            out.add(word)
            word = ""
    if word:
        out.add(word)
    for w in list(out):
        if w.endswith("s"):
            out.add(w[:-1])
        if w.endswith("ed"):
            out.add(w[:-2])
    return out


def _declaring_forms_for_key(key: str) -> list:
    """Which FORMS declare `key`, for DIAGNOSIS ONLY (B0wR-A1-FIND-08 / B0wR-A8-FIND-09).

    Computed from the manifests rather than authored, and used exclusively to phrase a refusal.
    It NEVER widens the permitted set: the caller has already decided to refuse before asking.

    Why it exists: a key that is undeclared FOR THIS FORM but declared for another produced the
    message "spec key 'provider' is not declared by the field manifest for
    ['relation:REQUIRED_SUPERSET']". The refusal was right; the diagnosis pointed at the wrong
    repair. A maintainer following it declares `provider` on the RELATION form, which widens P3's
    relation vocabulary so a bare relation spec can carry a provider that never reaches the
    provider layer or P9 — the refusal would have steered the reader into weakening the control.
    The real repair is almost always to declare the FORM (add `framework_kind`), not the field."""
    out = []
    for kind in ("framework", "guarantee"):
        for name, entry in WITNESS_FIELD_MANIFEST.get(kind, {}).items():
            if key in entry.get("required", {}) or key in entry.get("optional", {}):
                out.append(f"{kind}:{name}")
    if key in _MANIFEST_GUARANTEE_COMMON["required"] or key in _MANIFEST_GUARANTEE_COMMON["optional"]:
        out.append("guarantee:<any> (the shared non-enumerable vocabulary)")
    return sorted(set(out))


def _p9_callable_binding_present() -> bool:
    """Is the CALLABLE-BINDING half of P9 present in this module?

    Probed by TWO independent names — the authored provenance manifest and the execution entry
    point — so that renaming or removing either one is detected rather than silently tolerated.
    `PROVIDERS` is deliberately NOT probed by type name: coupling this check to agent-1's class
    name would make a rename read as an absent binding."""
    g = globals()
    return g.get("WITNESS_PROVIDER_MANIFEST") is not None and callable(g.get("p9_execute_witness"))


def _p9_names_a_provider(spec: dict) -> Optional[str]:
    """The provider a spec names, at either of the two sites P9 resolves, or None."""
    name = spec.get("provider")
    if isinstance(name, str) and name:
        return name
    witness = spec.get("independent_observed_source_or_witness")
    if isinstance(witness, dict):
        nested = witness.get("provider")
        if isinstance(nested, str) and nested:
            return nested
    return None


def _p9_require_callable_binding(label: str, spec: dict, problems: list) -> None:
    """FAIL-CLOSED CO-REQUISITE (B0wR-A8-FIND-08). A spec that names a provider is asking the
    framework to trust a callable. If the binding half of P9 is absent, nothing recomputes that
    callable's identity and this block's spec governance is a hollow control — so refuse rather
    than let the governance read as closure."""
    if _p9_callable_binding_present():
        return
    name = _p9_names_a_provider(spec)
    if name is None:
        return
    problems.append(_problem(
        label, "P9_CALLABLE_BINDING_ABSENT",
        f"the spec names provider {name!r}, but this build carries P9's SPEC governance without "
        "P9's CALLABLE BINDING: WITNESS_PROVIDER_MANIFEST and p9_execute_witness are absent, so "
        "a bare name is still bound to an arbitrary callable (GAP-1) and no identity is "
        "recomputed. Refusing a caller-supplied digest is sound only because identity is "
        "recomputed elsewhere; with no recomputation the refusal removes a forgeable claim and "
        "puts nothing in its place. Apply agent-1's p9.patch; REFUSED", member=name))


def _p9_scan_key(label: str, path: str, key: str, value: Any, problems: list) -> None:
    """L2 + L3 for ONE key. Called at every depth of the spec, so an override cannot hide one
    level down inside any dict or list — including inside a dict-typed field that has no nested
    manifest of its own.

    `value` is passed for the R1 SHAPE rule only. The two NAMED classes never consult it: a value
    branch is how `skip_identity_verification: false` would become a bypass."""
    words = _p9_words(key)

    # ---- L3, the named classes. Checked FIRST and independently of the manifest, so manifesting
    # one of these names does not legitimise it. PRESENCE only.
    if key in _P9_IDENTITY_DISABLING_FIELDS:
        problems.append(_problem(
            label, "P9_IDENTITY_DISABLING_FIELD_REFUSED",
            f"spec key {path!r} is a registered P9 identity-disabling form: it asks the framework "
            "to treat a witness/provider as trusted, to run it unpinned, or to take its identity "
            "from an alternate source, on the caller's own word. Identity is VERIFIED, never "
            "PERMITTED; there is no evidence that could justify not verifying it, so this field "
            "is refused rather than governed. Refused on PRESENCE — the value is not consulted, "
            "because consulting it would make a falsy value a bypass; REFUSED", member=key))
        return
    if key in _P9_SUPPLIED_IDENTITY_FIELDS:
        problems.append(_problem(
            label, "P9_SUPPLIED_IDENTITY_REFUSED",
            f"spec key {path!r} SUPPLIES the identity that P9 recomputes. The callable's module, "
            "qualname and content digest are derived from the registry-side authored manifest and "
            "compared against the callable itself; a caller-supplied digest is redundant when it "
            "agrees and forged when it does not, and accepting one would let the spec choose what "
            "the identity is. Refused on PRESENCE; REFUSED", member=key))
        return

    # ---- L2/R1 — a boolean is an ASSERTION, not EVIDENCE.
    if isinstance(value, bool) and (words & _P9_IDENTITY_WORDS):
        problems.append(_problem(
            label, "P9_IDENTITY_DISABLING_FIELD_REFUSED",
            f"spec key {path!r} carries a boolean in identity/trust vocabulary. Identity evidence "
            "is a module, a qualname or a digest — something the framework can independently "
            "RECOMPUTE and compare. A boolean cannot be recomputed; it can only be believed, "
            "which makes it a caller-authored trust switch; REFUSED", member=key))
        return

    # ---- L2/R2 — a negation or an alternate source applied to an identity concept.
    if (words & _P9_IDENTITY_WORDS) and (words & (_P9_NEGATION_WORDS | _P9_ALTERNATE_WORDS)):
        which = sorted(words & (_P9_NEGATION_WORDS | _P9_ALTERNATE_WORDS))
        problems.append(_problem(
            label, "P9_IDENTITY_DISABLING_FIELD_REFUSED",
            f"spec key {path!r} combines identity/provenance vocabulary with {which} — it either "
            "negates a verification the framework performs or names a SECOND, unpinned source of "
            "identity truth. Both forms let the caller choose what gets verified; the identity "
            "binding must have exactly one source and no off switch; REFUSED", member=key))
        return

    # ---- L2/R3 — a digest (or the word `identity`) attached to a principal: a SUPPLIED identity.
    if (words & _P9_DIGEST_WORDS) and (words & _P9_PRINCIPAL_WORDS):
        problems.append(_problem(
            label, "P9_SUPPLIED_IDENTITY_REFUSED",
            f"spec key {path!r} attaches a digest/identity to a provider or witness, which is the "
            "shape of a caller SUPPLYING the identity P9 is required to recompute from the "
            "registry-side manifest. Identity flows from the code to the framework, never from "
            "the spec; REFUSED", member=key))


def _p9_deny_scan(label: str, spec: Any, problems: list, path: str = "spec",
                  _depth: int = 0) -> None:
    """DEEP L2+L3 scan of the whole spec. Runs BEFORE the P3 manifest lookup, at every depth,
    through dicts AND lists, so `{"presence": {"x": {"skip_identity_verification": true}}}` is
    refused just as the top-level form is."""
    if _depth > 12:
        problems.append(_problem(label, "MALFORMED_SPEC",
                                 f"{path}: spec nesting exceeds the governed depth; REFUSED"))
        return
    if isinstance(spec, dict):
        for key in sorted(spec, key=str):
            value = spec[key]
            child = f"{path}.{key}"
            _p9_scan_key(label, child, str(key), value, problems)
            _p9_deny_scan(label, value, problems, child, _depth + 1)
    elif isinstance(spec, (list, tuple)):
        for i, value in enumerate(spec):
            _p9_deny_scan(label, value, problems, f"{path}[{i}]", _depth + 1)


# --- L4: the by-execution closure -------------------------------------------------------------
def audit_identity_fields_are_obligation_only(pairs: Optional[Iterable] = None) -> list:
    """GENERIC DETECTOR — re-derive the IDENTITY-RELAXER class BY EXECUTION.

    `pairs` is an iterable of (label, relation, D, C, strict_spec, identity_spec, field). For each,
    the strict state must yield >= 1 fail-closed problem; if adding/altering the IDENTITY field
    yields ZERO, that field is a DISPENSATION rather than an OBLIGATION and is refused — whatever
    it is called. This is Agent-4's P5 operational definition applied to the P9 namespace, and it
    is what makes the claim total: L1-L3 quantify over names and shapes, L4 quantifies over the
    PROPERTY. Unlike P5's audit there is no `registered` escape hatch, because no identity field is
    ever permitted to be guard-disabling."""
    problems: list = []
    for label, relation, D, C, strict_spec, identity_spec, field in (pairs or ()):
        strict = compare(relation, D, C, strict_spec)
        weak = compare(relation, D, C, identity_spec)
        s_fc = sum(1 for p in strict if p.get("fail_closed", True))
        w_fc = sum(1 for p in weak if p.get("fail_closed", True))
        if s_fc > 0 and w_fc == 0:
            problems.append(_problem(
                relation, "IDENTITY_FIELD_IS_A_DISPENSATION",
                f"{label}: identity/provenance field {field!r} turns {s_fc} fail-closed "
                "problem(s) into a CLEAN verdict. P9 adds no permitted spec field; nothing in the "
                "identity namespace may REMOVE a refusal. No condition provider may govern this, "
                "because no evidence justifies not verifying identity; REFUSED"))
    return problems


def audit_identity_namespace_invariants() -> list:
    """The NEGATIVE invariants: identity metadata cannot become a P5-governed disable, cannot name
    a condition that would authorize skipping P6 or P9, and cannot be admitted by a manifest edit.
    Checked, never assumed."""
    problems: list = []
    if not _p9_callable_binding_present():
        problems.append(_problem(
            "identity", "P9_CALLABLE_BINDING_ABSENT",
            "this module carries P9's SPEC-surface governance but not P9's CALLABLE BINDING "
            "(WITNESS_PROVIDER_MANIFEST / p9_execute_witness are absent). GAP-1 — a bare name "
            "bound to an arbitrary callable — is OPEN, underneath governance that reads as "
            "closing it. This patch is a CO-REQUISITE of agent-1's p9.patch, not a substitute; "
            "REFUSED"))
    for entry in _GUARD_DISABLING:
        head = entry["field"].split(".")[0]
        if _p9_words(head) & _P9_VERIFICATION_WORDS:
            problems.append(_problem(
                "identity", "IDENTITY_FIELD_REGISTERED_AS_DISABLE",
                f"_GUARD_DISABLING registers identity field {entry['field']!r} as a condition-"
                "backed guard disable. P5 governs weakenings that CAN be legitimate; not "
                "verifying a witness's identity never can be; REFUSED"))
    for condition in _CONDITIONS:
        if _p9_words(condition) & _P9_VERIFICATION_WORDS:
            problems.append(_problem(
                "identity", "IDENTITY_CONDITION_REGISTERED",
                f"_CONDITIONS declares {condition!r}; a condition in the identity namespace would "
                "let `condition_providers` name a provider that authorizes skipping P9/P6 "
                "verification; the condition namespace must contain no such condition; REFUSED"))
    # No refused name may be PERMITTED anywhere. This is the invariant that makes L3's
    # anti-regression role checkable rather than merely asserted in a comment.
    # Agent-1 converted both constants to frozenset (B0wR-A8-FIND-07 follow-up), so `|` would now
    # work directly. The set() coercion is KEPT deliberately: these are membership-only constants
    # whose container type is not part of the contract, and a control should not acquire a
    # dependency on it. The coercion costs nothing and cannot regress.
    refused = set(_P9_IDENTITY_DISABLING_FIELDS) | set(_P9_SUPPLIED_IDENTITY_FIELDS)
    declared = set(_MANIFEST_COMMON["required"]) | set(_MANIFEST_COMMON["optional"])
    declared |= set(_MANIFEST_GUARANTEE_COMMON["required"])
    declared |= set(_MANIFEST_GUARANTEE_COMMON["optional"])
    for form_kind in ("relation", "framework", "guarantee"):
        for entry in WITNESS_FIELD_MANIFEST[form_kind].values():
            declared |= set(entry.get("required", {})) | set(entry.get("optional", {}))
    for namespace in _MANIFEST_NESTED.values():
        declared |= set(namespace["required"]) | set(namespace["optional"])
    for name in sorted(refused & declared):
        problems.append(_problem(
            "identity", "REFUSED_IDENTITY_FIELD_IS_MANIFESTED",
            f"{name!r} is in a P9 REFUSED class yet some manifest declares it as permitted. A "
            "refused identity form must never be admissible; the deny scan still refuses it, but "
            "the contradiction must be resolved in the manifest; REFUSED"))
    return problems


# Fields the framework reads for EVERY form: dispatch selectors and the shared normalizer. These
# are unioned into every form's manifest.
_MANIFEST_COMMON = {
    "required": {},
    "optional": {
        "resolver": ("str", frozenset({"provenance_derivation", "schema_validation",
                                       "harness_completeness", "semantic_reachability",
                                       "authoritative_source_no_enumerable_oracle"})),
        "framework_kind": ("str", frozenset({"provenance_derivation", "schema_validation",
                                             "harness_completeness", "semantic_reachability",
                                             "authoritative_source_no_enumerable_oracle"})),
        "relation": _T_STR,
        "normalize": ("list", frozenset({"casefold", "strip", "strip_leading_dot", "posix_path",
                                         "str"})),
        "presence": _T_ANY,          # str | dict -> the `presence` NESTED manifest
        # Carried by the tracked spec fixture and read by collection_completeness for reporting
        # only; declared here so a fixture spec can flow into evaluate() without tripping the gate.
        "domain_class": _T_STR,
        "partition_group": _T_STR,
        # P5 (guard-disabling fields): names the registered provider that must COMPUTE each
        # guard-disable condition. READ by authorize_guard_disable(); declared here so P5's
        # governed path is reachable instead of being refused as an undeclared field
        # (B0wR-A8-FIND-01) and so P3 invariant I4 holds over the composed module. Its key
        # domain is pinned by the `condition_providers` nested manifest.
        "condition_providers": _T_DICT,
        # Also P5: authorize_guard_disable() reads `source_collection_id` for the refusal label on
        # BOTH layers, but P3 declared it only in _MANIFEST_GUARANTEE_COMMON, so a COMPARATOR spec
        # carrying it was refused as undeclared and agent-4's own CLASS-1 positive arm could not
        # pass under composition (B0wR-A8-FIND-04). Declared here because the code reads it here —
        # which is P3's rule, applied to P5's code.
        "source_collection_id": _T_STR,
        # P7 (code-native observation). A spec may carry `observed` WITHOUT declaring a framework
        # kind — the inline/code-native form, which has no provider and therefore no framework form
        # to govern it (the framework-kind requirement is provider-backed only; see A-29). It is
        # declared HERE so that form reaches its comparator instead of dying as an undeclared field,
        # and it is typed _T_WITNESS rather than _T_SETLIKE precisely because nothing else guards
        # this path: on the relation-only route there is no provider layer, so an authored
        # `observed: ["a"]` would be a caller-supplied answer with no gate in front of it. The
        # PROVIDER forms below override this with _T_W_SETLIKE, where P7's own gate does rule and
        # emits its own detector for an authored operand.
        "observed": _T_WITNESS,
        # P7-FIND-02/03: the DATA-AUTHORED LEVERS. `args` steers what a producer emits; `path` and
        # `pointer` select WHICH document is the domain for an authored contract. The framework
        # READS all three (check_steering_args_pin / check_authored_contract_pin /
        # declare_steering_channels), so P3's I4 totality proof requires them DECLARED — the
        # governed path, in place of an exemption. Declaring a lever is not permitting it: each is
        # adjudicated against a per-spec REVIEWED PIN, and an unpinned one is REFUSED. Their value
        # domain is deliberately open because a pin, not an enum, is what bounds them.
        "args": _T_LIST,
        "path": _T_STR,
        "pointer": _T_STR,
        # `_witness_vetted`: the marker resolve_witness_fields() stamps on a spec that PASSED the P7
        # gate. The framework READS it (_resolve_observed), so P3's I4 totality proof requires it
        # DECLARED — the governed path, in place of RM6's exemption. Its type is the module-private
        # _VETTED sentinel itself, so a caller-authored `_witness_vetted: true` is a
        # FIELD_TYPE_VIOLATION at the schema gate rather than a key that is merely skipped.
        # Its two siblings, `_witness_provenance` and `_p6_channels`, are deliberately NOT declared
        # here: the framework never reads either as a literal spec key, so a manifest entry for them
        # would be a permission with no consumer — the exact dead-entry shape invariant I5 refuses.
        # They are handled as framework-WRITTEN annotations at the gate instead.
        "_witness_vetted": _T_VETTED,
        # NOTE: P9 adds NO permitted spec field (Agent-1's composition contract). An earlier
        # draft declared a `witness_identity` evidence bundle here; it was WITHDRAWN because a
        # spec-supplied module/qualname/digest is exactly how an attacker hands P9 an identity.
        # Identity is recomputed from the registry-side manifest. See _P9_SUPPLIED_IDENTITY_FIELDS.
    },
}

# --- NESTED witness namespaces ----------------------------------------------------------------
# A witness that hides one level down is still a witness. Each nested object the framework reads
# keys from gets its OWN manifest and is validated recursively, so a novel form cannot evade
# declaration by nesting itself inside `presence`, `mutation_witness` or the witness reference.
_MANIFEST_NESTED = {
    "presence": {
        "required": {"policy": ("str", frozenset({"INVALID_EMPTY", "VALID_EMPTY",
                                                  "CONDITIONALLY_EMPTY", "REQUIRED_NON_EMPTY"}))},
        "optional": {"operand": ("str", frozenset({"domain", "collection", "both"})),
                     "floor": _T_LIST,
                     "empty_condition_met": _T_BOOL},
    },
    "mutation_witness": {
        "required": {"member": _T_ANY,
                     "operation": ("str", frozenset({"remove", "add"}))},
        "optional": {"expected_observable_mismatch": _T_ANY},
    },
    "witness_ref": {                       # independent_observed_source_or_witness, dict form
        "required": {"provider": _T_STR},
        "optional": {"reads": _T_STR,
                     "known_control": _T_ANY,
                     "unknown_probe": _T_ANY},
    },
    # P5: the condition -> provider-name map. CLOSED over the registered condition namespace, so
    # it cannot be used to name a novel condition (and in particular no identity condition).
    "condition_providers": _MANIFEST_CONDITION_PROVIDERS,
}

# Which spec field carries which nested namespace.
_NESTED_FIELD_NAMESPACE = {
    "presence": "presence",
    "mutation_witness": "mutation_witness",
    "independent_observed_source_or_witness": "witness_ref",
    "condition_providers": "condition_providers",
}

# --- FORM manifests ---------------------------------------------------------------------------
# AUTHORED LITERAL. Do not rewrite as a comprehension over the registries — that would make the
# permission set a function of the thing it governs and P3 would govern nothing.
WITNESS_FIELD_MANIFEST = {
    # ---- comparator relations (13) -----------------------------------------------------------
    "relation": {
        "EXACT": {"required": {}, "optional": {},
                  "load_bearing_operands": frozenset({"domain"}),
                  "directions": frozenset({"missing", "unknown"})},
        "REQUIRED_SUBSET": {"required": {}, "optional": {},
                            "load_bearing_operands": frozenset({"domain", "collection"}),
                            "directions": frozenset({"unknown"})},
        "REQUIRED_SUPERSET": {"required": {}, "optional": {},
                              "load_bearing_operands": frozenset({"domain"}),
                              "directions": frozenset({"missing"})},
        "DISJOINT": {"required": {}, "optional": {},
                     "load_bearing_operands": frozenset({"domain", "collection"}),
                     "directions": frozenset({"overlap"})},
        "DISJOINT_WITH_FLOOR": {"required": {}, "optional": {},
                                "required_nested": {"presence": frozenset({"floor"})},
                                "load_bearing_operands": frozenset({"domain", "collection"}),
                                "directions": frozenset({"overlap"})},
        "PARTITION": {"required": {"partition_members": _T_LIST}, "optional": {},
                      "load_bearing_operands": frozenset({"domain"}),
                      "directions": frozenset({"missing", "unknown", "overlap"})},
        # P4: `key_spec` / `value_spec` are the CLOSED sub-specs the key and value sub-relations
        # are adjudicated under. They exist because a sub-relation re-enters the gate on operands
        # the top-level spec never described, and inheriting the parent's fields would let a
        # parent-level witness silently satisfy a child relation's obligation. Declared here (and
        # read literally by _subspec) so P3 governs them in both directions.
        "KEYED_MAPPING": {"required": {},
                          "optional": {"key_relation": _T_STR, "value_relation": _T_STR,
                                       "key_spec": _T_DICT, "value_spec": _T_DICT},
                          "load_bearing_operands": frozenset({"domain"}),
                          "directions": frozenset({"missing", "unknown"})},
        "KEYED_MAPPING_AGAINST_UNION": {"required": {},
                                        "optional": {"value_domain": _T_W_SETLIKE,
                                                     "key_spec": _T_DICT, "value_spec": _T_DICT},
                                        "load_bearing_operands": frozenset({"domain"}),
                                        "directions": frozenset({"missing", "unknown"})},
        "SCHEMA_STRICTNESS": {"required": {"unknown_probe_accepted": _T_W_BOOL}, "optional": {},
                              "load_bearing_operands": frozenset({"domain"}),
                              "directions": frozenset({"missing", "unknown"})},
        "PROVENANCE_CORRESPONDENCE": {
            "required": {},
            "optional": {"correspondence": ("str", frozenset({"bijective", "injective"}))},
            "load_bearing_operands": frozenset({"domain", "collection"}),
            "directions": frozenset({"missing", "unknown"})},
        "SEMANTIC_REACHABILITY": {"required": {}, "optional": {"strict_reachability": _T_BOOL},
                                  "load_bearing_operands": frozenset({"domain"}),
                                  "directions": frozenset({"missing", "unknown"})},
        "DIFFERENTIAL_EXECUTION": {"required": {"member_effect": _T_W_DICT,
                                                "baseline_healthy": _T_W_BOOL},
                                   "optional": {},
                                   "load_bearing_operands": frozenset({"collection"}),
                                   "directions": frozenset({"inert"})},
        "HASH_BACKSTOP": {"required": {"baseline_hash": _T_STR},
                          "optional": {"observed_hash": _T_STR},
                          "load_bearing_operands": frozenset({"domain", "collection"}),
                          "directions": frozenset({"unknown", "drift"})},
        # P4: the Part-D source-presence transit. It declares no witness fields of its own — its
        # whole obligation is the presence gate plus the pinned-control difference — so its
        # vocabulary is the common one.
        "POSITIVE_CONTROL_PRESENCE": {"required": {}, "optional": {},
                                      "load_bearing_operands": frozenset({"collection"}),
                                      "directions": frozenset({"missing"})},
    },
    # ---- provider / framework kinds (4 comparator-backed) ------------------------------------
    # All four provider kinds share one witness vocabulary; they differ in the RELATION they
    # declare, which is governed by the relation manifest above.
    "framework": {
        "provenance_derivation": {
            "required": {"relation": _T_STR},
            "optional": {"provider": _T_STR, "reads": _T_STR, "independent_source": _T_STR,
                         "observed": _T_W_SETLIKE, "load_bearing_direction": _T_ANY}},
        "schema_validation": {
            "required": {"relation": _T_STR},
            "optional": {"provider": _T_STR, "reads": _T_STR, "independent_source": _T_STR,
                         "observed": _T_W_SETLIKE, "load_bearing_direction": _T_ANY}},
        "harness_completeness": {
            "required": {"relation": _T_STR},
            "optional": {"provider": _T_STR, "reads": _T_STR, "independent_source": _T_STR,
                         "observed": _T_W_SETLIKE, "load_bearing_direction": _T_ANY}},
        "semantic_reachability": {
            "required": {"relation": _T_STR},
            "optional": {"provider": _T_STR, "reads": _T_STR, "independent_source": _T_STR,
                         "observed": _T_W_SETLIKE, "load_bearing_direction": _T_ANY}},
        "authoritative_source_no_enumerable_oracle": {
            "required": {}, "optional": {}},          # governed entirely by the guarantee manifest
    },
    # ---- non-enumerable guarantee kinds (5) --------------------------------------------------
    "guarantee": {
        # P2 (2a): `witness_floor` names the reviewed members the independent discovery MUST have
        # found. It is OPTIONAL in the schema and MANDATORY in the verifier on purpose — its
        # absence is a P2 finding about the ADEQUACY of the discovery, not a P3 finding about the
        # vocabulary, and a refusal that names the wrong property misdirects the reader.
        "INDEPENDENT_CONSEQUENCE_RECONCILIATION": {"required": {},
                                                   "optional": {"witness_floor": _T_SETLIKE}},
        "INDEPENDENT_SITE_UNIVERSE": {"required": {}, "optional": {}},
        "CROSS_SOURCE_REQUIREMENT": {"required": {}, "optional": {"min_grounds": _T_INT}},
        "SEMANTIC_MUTATION_WITNESS": {"required": {"mutation_witness": _T_DICT}, "optional": {}},
        "CLOSED_WORLD_UNKNOWN_REFUSAL": {"required": {}, "optional": {}},
    },
}

# The six keys validate_ne_config() requires of EVERY non-enumerable spec, plus the shared
# optionals. Unioned into every "guarantee" form.
_MANIFEST_GUARANTEE_COMMON = {
    "required": {"source_collection_id": _T_STR,
                 "guarantee_kind": _T_STR,
                 "expected_source": _T_ANY,
                 "independent_observed_source_or_witness": _T_ANY,
                 "comparison": ("str", frozenset({"AUTHORITATIVE_SUPERSET", "REQUIRED_SUBSET",
                                                  "KEYED_MAPPING", "PROVENANCE_CORRESPONDENCE",
                                                  "AUTHORITATIVE_MUST_JUSTIFY"})),
                 "positive_presence": ("str", frozenset({"INVALID_EMPTY", "VALID_EMPTY",
                                                         "CONDITIONALLY_EMPTY"}))},
    "optional": {"dependencies": _T_LIST,
                 "required_present": _T_SETLIKE,
                 "empty_condition_met": _T_BOOL},
}

_TYPE_CHECKS = {
    "str": lambda v: isinstance(v, str),
    "bool": lambda v: isinstance(v, bool),
    "int": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "list": lambda v: isinstance(v, (list, tuple)),
    "dict": lambda v: isinstance(v, dict),
    "setlike": lambda v: isinstance(v, (list, tuple, set, frozenset, dict)),
    "any": lambda v: True,
    # The framework-internal vetting marker. Its VALUE is a module-private sentinel that no JSON
    # fixture and no spec literal can construct, so declaring it costs nothing and buys P3's I4
    # totality proof: the code READS `_witness_vetted`, so some manifest must DECLARE it.
    "vetted_marker": lambda v: v is _VETTED,
}


# --- P7 x P3: the WITNESSED types -------------------------------------------------------------
# THE DIVISION OF LABOUR, which RM5 collapsed by retyping four self-adequacy fields to `any`.
#
#   P3 (here) governs SHAPE.      Is this the KIND of thing the relation can read at all?
#   P7 (witness_field) governs    Was this value PRODUCED BY EXECUTED CODE, or authored?
#   PROVENANCE.
#
# Typing the fields `any` did not move the second question to P7 — P7 already owned it. It deleted
# the FIRST question, so `unknown_probe_accepted: "no"` (a string where the relation reads a
# boolean) sailed through the schema gate. Typing them as WITNESS-ONLY would make the opposite
# error: it would answer P7's question at the schema gate, and a JSON `false` would come back as
# FIELD_TYPE_VIOLATION instead of the SELF_ADEQUACY_UNWITNESSED that names what is actually wrong
# with it — a refusal that misdiagnoses is a defect in the refusal.
#
# So each self-adequacy field is typed as "the base type the relation reads, OR the
# CodeNativeWitness that produces it". A well-typed lie (`False`) passes P3 and is refused by P7 by
# NAME; an ill-typed value ("no") is refused by P3 by SHAPE; the genuine witness passes both.
def _witnessed(base_type: str):
    """Descriptor predicate: the base type, or the witness whose execution yields it."""
    base = _TYPE_CHECKS[base_type]
    return lambda v: type(v) is CodeNativeWitness or base(v)


for _base_type in ("bool", "dict", "setlike"):
    _TYPE_CHECKS["witnessed_" + _base_type] = _witnessed(_base_type)
del _base_type

# STRICT witness: a CodeNativeWitness and nothing else. Used where a field is declared on a form
# that has NO layer in front of the comparator to rule on provenance — see `observed` in
# _MANIFEST_COMMON. There the base type cannot be admitted, because admitting it is exactly BYP-4.
_TYPE_CHECKS["witness"] = lambda v: type(v) is CodeNativeWitness

# Fields whose pinned domain is a set of EXECUTABLE DIRECTIVES rather than a data domain. An entry
# outside the set is not "a declared field carrying an out-of-domain payload" — it NAMES A TRANSFORM
# THE FRAMEWORK HAS NO IMPLEMENTATION FOR, so the comparison the spec asks for cannot be performed
# at all. That is a MALFORMED SPEC. The refusal is produced by the BUILDER itself, so it is bound to
# the same behavioural authority P3's I2b test binds the pinned domain to, never to a second copy of
# the directive list. A directive the builder ACCEPTS but the manifest excludes still falls through
# to FIELD_VALUE_OUTSIDE_DOMAIN: the two authorities must agree, and a divergence is a refusal.
_DIRECTIVE_FIELDS = {"normalize": lambda v: _normalizer({"normalize": [v]})}


def _form_manifest(form_kind: str, form_name: str) -> Optional[dict]:
    return WITNESS_FIELD_MANIFEST.get(form_kind, {}).get(form_name)


def _declared_forms(spec: dict, relation: Optional[str] = None) -> list:
    """The (form_kind, form_name) pairs a spec invokes. A spec is validated against the UNION of
    its forms' manifests — and ONLY those. Sub-relations named by KEYED_MAPPING are included, so a
    sub-relation cannot smuggle in a field vocabulary the outer relation never declared."""
    forms: list = []
    kind = spec.get("resolver") or spec.get("framework_kind")
    if isinstance(kind, str) and kind in WITNESS_FIELD_MANIFEST["framework"]:
        forms.append(("framework", kind))
    gk = spec.get("guarantee_kind")
    if isinstance(gk, str) and gk in WITNESS_FIELD_MANIFEST["guarantee"]:
        forms.append(("guarantee", gk))
    names = [relation if relation is not None else spec.get("relation")]
    names += [spec.get("key_relation"), spec.get("value_relation")]
    for name in names:
        if not isinstance(name, str):
            continue
        canonical = resolve_relation(name)
        if canonical is not None and ("relation", canonical) not in forms:
            forms.append(("relation", canonical))
    return forms


def _union_manifest(forms: list, spec: dict) -> tuple[dict, dict, dict]:
    """(required, optional, required_nested) unioned across the declared forms + the commons."""
    required: dict = dict(_MANIFEST_COMMON["required"])
    optional: dict = dict(_MANIFEST_COMMON["optional"])
    required_nested: dict = {}
    if any(k == "guarantee" for k, _ in forms):
        required.update(_MANIFEST_GUARANTEE_COMMON["required"])
        optional.update(_MANIFEST_GUARANTEE_COMMON["optional"])
    for form_kind, form_name in forms:
        entry = _form_manifest(form_kind, form_name)
        if entry is None:
            continue
        required.update(entry.get("required", {}))
        optional.update(entry.get("optional", {}))
        for ns, keys in (entry.get("required_nested") or {}).items():
            required_nested.setdefault(ns, set()).update(keys)
    return required, optional, required_nested


def _is_p7_resolved(spec: Any, path: str) -> bool:
    """True when `path`'s field was PRODUCED by the P7 gate on THIS spec.

    Not a trust affordance. The record is a module-private _WitnessProvenance matched by EXACT type,
    which json.loads cannot construct and json.dumps/pickle refuse — so a fixture or a spec literal
    cannot claim to have been resolved. It exists because resolve_witness_fields() REPLACES a strict
    witness field with the value it executed, and the gate runs again over that resolved spec inside
    compare(): without this, the same spec would pass on the way in and fail on the way out of its
    own resolution."""
    record = _provenance_of(spec) if isinstance(spec, dict) else None
    return record is not None and path.rsplit(".", 1)[-1] in record


def _check_value(relation: str, path: str, value: Any, descriptor: tuple, problems: list,
                 spec: Any = None) -> None:
    type_name, allowed = descriptor
    if type_name == "witness" and _is_p7_resolved(spec, path):
        return                          # executed by the P7 gate; its origin frame is on record
    if not _TYPE_CHECKS.get(type_name, _TYPE_CHECKS["any"])(value):
        problems.append(_problem(relation, "FIELD_TYPE_VIOLATION",
                                 f"spec field {path!r} must be {type_name}, got "
                                 f"{type(value).__name__}; fail-closed"))
        return
    if allowed is None:
        return
    builder = _DIRECTIVE_FIELDS.get(path.rsplit(".", 1)[-1])
    values = value if isinstance(value, (list, tuple)) and type_name == "list" else [value]
    for v in values:
        if isinstance(v, (str, int, float, bool, type(None))) and v not in allowed:
            if builder is not None:
                try:
                    builder(v)
                except FrameworkError as exc:
                    problems.append(_problem(relation, "MALFORMED_SPEC", str(exc)))
                    continue
            problems.append(_problem(relation, "FIELD_VALUE_OUTSIDE_DOMAIN",
                                     f"spec field {path!r} carries {v!r}, which is not in the "
                                     f"declared value domain {sorted(map(str, allowed))}; a field "
                                     "may not be declared and then carry an arbitrary payload; "
                                     "fail-closed"))


def _check_nested(relation: str, field: str, namespace: str, value: Any,
                  required_keys: set, problems: list) -> None:
    """Validate a NESTED witness object against its own manifest. A witness one level down is
    still a witness: nesting must not be an escape from declaration."""
    if not isinstance(value, dict):
        return                                     # the str/scalar forms are handled by the reader
    entry = _MANIFEST_NESTED[namespace]
    allowed = dict(entry["required"])
    allowed.update(entry["optional"])
    for key in sorted(value):
        if key not in allowed:
            problems.append(_problem(relation, "UNDECLARED_WITNESS_FIELD",
                                     f"nested witness object {field!r} carries key {key!r}, which "
                                     f"no manifest for namespace {namespace!r} declares; a witness "
                                     "form that is not declared cannot be adjudicated; REFUSED "
                                     "(declare it in _MANIFEST_NESTED — a governed schema edit)"))
        else:
            _check_value(relation, f"{field}.{key}", value[key], allowed[key], problems,
                         spec=value)
            # RECURSE: a nested namespace inside a nested namespace is still governed. A dict the
            # framework reads keys from must never be reachable without a manifest.
            sub_ns = _NESTED_FIELD_NAMESPACE.get(key)
            if sub_ns is not None and sub_ns in _MANIFEST_NESTED \
                    and isinstance(value[key], dict):
                _check_nested(relation, f"{field}.{key}", sub_ns, value[key], set(), problems)
    for key in sorted(set(entry["required"]) | set(required_keys or ())):
        if key not in value:
            problems.append(_problem(relation, "MISSING_DECLARED_FIELD",
                                     f"nested witness object {field!r} is missing required key "
                                     f"{key!r}; REFUSED"))


def _reject_unknown_fields(form_kind: str, form_name: str, spec: dict,
                           relation: Optional[str] = None) -> list:
    """THE P3 GATE. Refuse any spec key outside the union field manifest for the declared form(s).

    Returns a list of Problem dicts; [] means every key in the spec is DECLARED, correctly typed,
    and within its pinned value domain. Fail-closed in three directions:
      * an UNKNOWN form name (a relation/kind with no manifest entry) is refused outright — the
        registries and the manifest are separate authorities and a form must satisfy BOTH;
      * an UNDECLARED key is refused (BYP-5: the open-world spec);
      * a MISSING required key is refused (a witness field that simply is not there).
    """
    problems: list = []
    label = relation or form_name
    if not isinstance(spec, dict):
        return [_problem(label, "MALFORMED_SPEC", "spec is not an object; REFUSED")]

    # P9 x P5 (L2+L3): identity-disabling and supplied-identity forms are refused independently of
    # the manifest, at EVERY depth. Running BEFORE the manifest lookup is deliberate: adding one of
    # these names to a manifest — the obvious "fix" when the P3 gate refuses it — does not make it
    # acceptable, and nesting it inside any dict does not hide it.
    #
    # It seeds `problems` rather than returning early, so the manifest checks below still run and
    # a key that is BOTH P9-shaped AND undeclared reports BOTH refusals. Short-circuiting here made
    # the reported kind depend on scan order and cost a pre-existing P3 arm its pinned kind
    # (B0wR-A8-FIND-05) — a refusal record that varies with evaluation order is worse evidence,
    # even when the verdict is identical.
    _p9_deny_scan(label, spec, problems)
    # FAIL-CLOSED CO-REQUISITE (B0wR-A8-FIND-08). Inert once agent-1's p9.patch is applied.
    _p9_require_callable_binding(label, spec, problems)

    forms = _declared_forms(spec, relation=relation)
    if (form_kind, form_name) not in forms:
        forms.append((form_kind, form_name))
    ungoverned: list = []
    for fk, fn in forms:
        if _form_manifest(fk, fn) is None:
            ungoverned.append(_problem(label, "UNGOVERNED_FORM",
                                     f"{fk} form {fn!r} has NO entry in WITNESS_FIELD_MANIFEST; a "
                                     "form registered in the instance registry but absent from the "
                                     "governed schema namespace cannot be adjudicated; REFUSED "
                                     "(add a manifest entry — an explicit, reviewable schema "
                                     "evolution)"))
    if ungoverned:
        return problems + ungoverned

    required, optional, required_nested = _union_manifest(forms, spec)
    allowed = dict(required)
    allowed.update(optional)

    # FRAMEWORK-WRITTEN ANNOTATIONS (RM8, narrowing RM6's three-name exemption to two).
    # `_witness_provenance` and `_p6_channels` are written by resolve_witness_fields() /
    # declare_steering_channels() onto the framework's own working COPY of a spec. They are not
    # caller-facing spec vocabulary and the framework never reads either as a literal spec key, so
    # they cannot be manifested without creating a dead entry (invariant I5), and they must not be
    # refused as undeclared either — the gate re-runs over the resolved spec inside compare().
    # They are skipped here and nowhere else. This grants them NOTHING: every consumption site
    # checks `type(record) is _WitnessProvenance` / `spec['_witness_vetted'] is _VETTED`, so a
    # caller-authored lookalike passes this gate and is still refused where it would be believed.
    # `_witness_vetted`, by contrast, IS read literally and IS declared, so it is not listed here.
    _FRAMEWORK_WRITTEN_ANNOTATIONS = (_WITNESS_PROVENANCE_KEY, "_p6_channels")
    for key in sorted(spec):
        if key in _FRAMEWORK_WRITTEN_ANNOTATIONS:
            continue
        if key not in allowed:
            elsewhere = _declaring_forms_for_key(key)
            if elsewhere:
                problems.append(_problem(
                    label, "UNDECLARED_WITNESS_FIELD",
                    f"spec key {key!r} is not declared for "
                    f"{sorted(f'{k}:{n}' for k, n in forms)}, but IS declared by {elsewhere}. The "
                    "spec is carrying a field belonging to a form it never declared. The repair is "
                    "to DECLARE THE FORM (e.g. add `framework_kind`), NOT to add this field to the "
                    "form above — that would widen the vocabulary so a spec could carry this field "
                    "without ever reaching the layer that acts on it; REFUSED", member=key))
                continue
            problems.append(_problem(label, "UNDECLARED_WITNESS_FIELD",
                                     f"spec key {key!r} is not declared by the field manifest for "
                                     f"{sorted(f'{k}:{n}' for k, n in forms)}; an undeclared key is "
                                     "a witness form the framework never registered, so it can "
                                     "neither be guarded nor adjudicated; REFUSED (a new witness "
                                     "form is always a new field, and a new field is always a "
                                     "manifest edit)", member=key))
            continue
        _check_value(label, key, spec[key], allowed[key], problems, spec=spec)

    # RM7: the P7 self-adequacy/witness fields' ABSENCE is owned by the RELATION VERIFIER (it emits
    # the relation-specific detector NO_MEMBER_EVIDENCE / BASELINE_UNHEALTHY / STRICTNESS_UNWITNESSED),
    # never by the generic schema MISSING gate. These fields remain in the manifest (present -> type/
    # vocabulary validated; unknown fields still rejected); only their ABSENCE is deferred. Ordinary
    # required fields are unaffected, so P3's requiredness is not weakened.
    _RELATION_OWNED_ABSENCE = ("member_effect", "baseline_healthy", "unknown_probe_accepted",
                               "value_domain")
    for key in sorted(required):
        if key in _RELATION_OWNED_ABSENCE:
            continue
        if key not in spec:
            problems.append(_problem(label, "MISSING_DECLARED_FIELD",
                                     f"required spec field {key!r} is absent for "
                                     f"{sorted(f'{k}:{n}' for k, n in forms)}; REFUSED", member=key))

    for field, namespace in _NESTED_FIELD_NAMESPACE.items():
        if field in spec:
            _check_nested(label, field, namespace, spec[field],
                          required_nested.get(field if field == "presence" else namespace, set()),
                          problems)
    return problems


def _witness_evaluation_gate(relation: str, expected_domain: Any, collection: Any,
                             spec: Optional[dict] = None, *, _depth: int = 0,
                             _path: str = "compare",
                             _condition: str = "EMPTY_LOAD_BEARING_OPERAND_LEGITIMATE") -> list:
    """THE SINGLE WITNESS-EVALUATION GATE (P4). Every witness-bearing evaluation on every route —
    the comparator layer, the provider-verifier layer, the non-enumerable guarantee layer and the
    legacy enumerable consumer — reaches its verdict HERE and nowhere else. This is the only
    function that may turn a relation name into a callable, and before it does it:

        * refuses to run at all while a registered witness/provider is on the stack,
        * runs the P3 closed-schema gate on the spec,
        * NORMALIZES both operands and reports duplicate collisions (a silently short list),
        * runs the POSITIVE-PRESENCE gate on the relation's LOAD-BEARING operands,
        * records a TRANSIT into every open `_evaluation` scope, so an entry point that reaches a
          clean verdict without arriving here is caught and refused.

    `_depth` is internal recursion bookkeeping and `_path` internal provenance — both keyword-only
    and deliberately NOT spec fields, so neither is expressible from a JSON fixture."""
    spec = spec or {}
    problems: list = []

    if not _EVAL_SCOPES:
        # SLICE1-TOTALITY (§10-11, FF-05). The transit ledger catches an entry point that OPENS a
        # scope and then reaches a clean verdict without transiting (NO_GATE_TRANSIT). Its blind
        # spot was the entry that opens NO scope at all: nothing was stranded, so nothing fired,
        # and the only authority left was the STATIC P4 AST invariant — a control outside the
        # module, which cannot speak at run time. The gate now refuses to produce a verdict
        # outside an evaluation scope, so the ledger is armed on every route by construction: an
        # entry point that declines to open a scope gets no verdict rather than a clean one.
        return [_problem(relation, "NO_EVALUATION_SCOPE",
                         "the evaluation gate was entered with NO open evaluation scope, so no "
                         "transit ledger exists to record this verdict against and the "
                         "NO_GATE_TRANSIT seal could never fire for whatever entry point called "
                         "it. An unscoped verdict is an unsealed verdict; REFUSED")]

    if _WITNESS_CALL_DEPTH > 0:
        # A resolver/provider/probe is executing. Whatever it is doing, it is not OBSERVING.
        return [_problem(relation, "WITNESS_INVOKED_CHECKER",
                         "a registered witness/provider callable invoked the evaluation gate "
                         "while producing its observation; a witness supplies an OBSERVATION and "
                         "the gate supplies the verdict — a witness that adjudicates is a second "
                         "evaluation path; REFUSED")]

    canonical = resolve_relation(relation)
    if canonical is None:
        return [_problem(relation, "UNKNOWN_RELATION",
                         f"relation {relation!r} is not implemented and has no alias; an unknown "
                         "relation cannot pass -> REFUSED")]

    schema_problems = _reject_unknown_fields("relation", canonical, spec,
                                             relation=canonical)
    if schema_problems:                # P3: an undeclared field is never adjudicated
        return schema_problems

    # RELATION/PROBE TOTALITY (Part F). A relation that is KNOWN is not thereby ADJUDICATED: until
    # a probe has been executed against it and shown that its declared load-bearing operands cover
    # the operands its verdict actually depends on, no verdict it produces means anything. Checked
    # at the single gate so the rule holds on every route rather than in whichever battery is run.
    #
    # POSITION. AFTER the P3 closed-schema gate, so a form that is not governed at all is refused
    # as UNGOVERNED_FORM by the authority that owns that question; probe adjudication is the next
    # question, not a substitute for the first one. A relation smuggled into the registry alone
    # never reaches here; one smuggled into the registry AND the manifest does, and is refused.
    unprobed = _relation_probe_refusal(relation, canonical)
    if unprobed is not None:
        return [unprobed]

    checker, domain_shape, collection_shape = _REGISTRY[canonical]

    try:
        norm = _normalizer(spec)
    except FrameworkError as exc:
        return [_problem(canonical, "MALFORMED_SPEC", str(exc))]

    def _shape(label: str, raw: Any, shape: str):
        if shape == "dict":
            if not isinstance(raw, dict):
                problems.append(_problem(canonical, "MALFORMED_OPERAND",
                                         f"{label} must be a dict key->set for {canonical}; got "
                                         f"{type(raw).__name__}"))
                return None, None
            # P4-FIND-01. Route the KEYS through the same collision detector the set path uses.
            # The dict comprehension below is LAST-WINS: two raw keys that normalize together
            # silently drop one key AND its value, erasing a requirement the domain declared — and
            # the identical operand supplied as a SET reports DUPLICATE_COLLISION. Same members,
            # same normalizer, opposite verdict, purely because of the operand's container.
            if _as_normalized_set(f"{label} keys", raw, norm, canonical, problems) is None:
                return None, None
            keyed = {norm(k): (set(v) if isinstance(v, (set, frozenset, list, tuple)) else v)
                     for k, v in raw.items()}
            return keyed, set(keyed)
        s = _as_normalized_set(label, raw, norm, canonical, problems)
        return s, s

    D_norm, D_for_presence = _shape("expected_domain", expected_domain, domain_shape)
    C_norm, C_for_presence = _shape("collection", collection, collection_shape)
    if D_norm is None or C_norm is None:
        return problems

    if not _gate_presence(canonical, D_for_presence, C_for_presence, spec, problems,
                          condition=_condition):
        return problems

    # TRANSIT. Recorded at the moment the gate hands normalized, presence-checked operands to a
    # relation — i.e. exactly when a verdict becomes possible. Recorded into EVERY open scope so a
    # nested evaluation satisfies the outer one rather than stranding it.
    record = {"path": _path, "relation": canonical, "depth": _depth}
    for scope in _EVAL_SCOPES:
        scope.append(record)

    checker(D_norm, C_norm, spec, problems, _depth=_depth)
    return problems


def _cc_legacy_compare(relation: str, expected_domain: Any, collection: Any,
            spec: Optional[dict] = None, *, _depth: int = 0, _path: str = "compare",
            _condition: str = "EMPTY_LOAD_BEARING_OPERAND_LEGITIMATE") -> list:
    """Public comparator entry: adjudicate `collection` (C) against `expected_domain` (D) under
    `relation`, THROUGH the single witness-evaluation gate.

    Returns a list of Problem dicts; [] means clean AND non-vacuous. Every failure mode is a
    Problem, never an exception (FrameworkError is reserved for a mis-wired normalize/presence
    directive). FAIL-CLOSED throughout: unknown relation, malformed operand, empty load-bearing
    operand, floor-missing, and a verdict that never transited the gate all yield Problems and
    never []."""
    if _depth:
        # Sub-relation re-entry from _reenter(): already inside the caller's evaluation scope.
        return _witness_evaluation_gate(relation, expected_domain, collection, spec,
                                        _depth=_depth, _path=_path, _condition=_condition)
    with _evaluation(_path) as ev:
        return _sealed(ev, _witness_evaluation_gate(relation, expected_domain, collection, spec,
                                                    _depth=_depth, _path=_path,
                                                    _condition=_condition))


# ============================================================================================
compare = _cc_legacy_compare   # provisional; REBOUND to the adapter at the foot


# PART B — INDEPENDENCE & DIRECTIONALITY GUARDS
# ============================================================================================
# The observed operand a verifier compares against MUST be derived without reading the declared
# collection constant; otherwise the oracle is a second copy of the list and proves nothing. And
# a design must be able to FAIL in its load-bearing direction; a provenance check whose relation
# cannot fire on the direction that matters is fail-open by construction (OBJ-4).

# Which comparator direction each relation is able to report a finding in. Used by the
# directionality guard: a spec that declares a load-bearing direction the relation cannot witness
# is refused rather than run.
_RELATION_DIRECTIONS = {
    "EXACT": {"missing", "unknown"},
    "REQUIRED_SUBSET": {"unknown"},              # fires on C - D
    "REQUIRED_SUPERSET": {"missing"},            # fires on D - C
    "DISJOINT": {"overlap"},
    "DISJOINT_WITH_FLOOR": {"overlap"},
    "PARTITION": {"missing", "unknown", "overlap"},
    "KEYED_MAPPING": {"missing", "unknown"},
    "KEYED_MAPPING_AGAINST_UNION": {"missing", "unknown"},
    "SCHEMA_STRICTNESS": {"missing", "unknown"},
    "PROVENANCE_CORRESPONDENCE": {"missing", "unknown"},
    "SEMANTIC_REACHABILITY": {"missing", "unknown"},
    "DIFFERENTIAL_EXECUTION": {"inert"},
    "HASH_BACKSTOP": {"unknown", "drift"},
    "POSITIVE_CONTROL_PRESENCE": {"missing"},    # fires on controls - source
}


# --- P1 GUARD ACTIVATION (Gate 4N-I28BH-B0w-R2-SLICE1-CONTINUATION, agent-1) -------------------
# P1: for every guard g and every spec s, if the field that ACTIVATES g is absent from s then the
# verdict on s must not be CLEAN. Both guards below were opt-in: `spec.get(field)` returning None
# meant "no obligation", so OMITTING a field was an evidence-free way to switch the control off —
# structurally the VAL-I28AX-01 defect class (an absent declaration becomes a silent pass).
#
# Each guard is split into two halves so the two obligations can be enforced at DIFFERENT points
# without either one masking the other:
#   * CONTENT  — the declaration is present and says something the framework refuses
#                (COPIED_ORACLE / SELF_REFERENCE / DIRECTION_UNWITNESSABLE / UNKNOWN_RELATION).
#                Runs EARLY in verify_provider, exactly where it ran before, so no existing
#                detector loses its position.
#   * ACTIVATION — the declaration is ABSENT, so the guard would run as a no-op. Runs LATE, on the
#                verdict, because P1's prohibited state is "a CLEAN verdict with an activating
#                field absent", not "this refusal must outrank that one". Enforcing it early would
#                preempt every P7/P9 witness-form detector and report the wrong control for specs
#                that are refused on other grounds anyway.
# The PUBLIC guard entry points keep both halves, in content-then-activation order, so a direct
# caller (and the P1 battery, which reads the guards at their own boundary) sees the full contract.
def _guard_independence_content(spec: dict, cid: str) -> Optional[dict]:
    reads = spec.get("reads")
    provider = spec.get("provider")
    if reads == cid or provider == cid:
        return _problem(spec.get("relation", "?"), "COPIED_ORACLE",
                        f"the observed provider reads the declared collection {cid!r}; that is a "
                        "second copy of the list, not an independent oracle; REFUSED")
    if spec.get("independent_source") == cid:
        return _problem(spec.get("relation", "?"), "SELF_REFERENCE",
                        f"independent_source names the declared constant {cid!r}; REFUSED")
    return None


# The fields that constitute an INDEPENDENCE DECLARATION: a statement of the SOURCE the observed
# operand is derived FROM. `provider` is deliberately NOT one of them. It names the callable that
# PRODUCES the operand, not what that callable READS, and the only rule it arms (`provider == cid`)
# fires solely on a provider registered under the collection's own id string — a near-vacuous
# check. A spec carrying `provider` alone leaves the substantive independence claim unwitnessed,
# which is the W10 absence half this guard exists to close.
_INDEPENDENCE_DECLARATION_FIELDS = ("reads", "independent_source")


def _p1_declared(spec: dict, field: str) -> bool:
    """Is `field` DECLARED — not merely keyed — on this spec?

    §4 FUTURE-ABSENCE. `field in spec` alone re-opens the defect one level down: `None`, `""`,
    `[]`, `{}` and `set()` are all ways to write a key that carries no declaration, and a
    presence test that accepts them turns a mandatory guard back into an opt-in one for anybody
    who knows to write the empty form. An activating field whose value states nothing is ABSENT,
    because the guard downstream of it has exactly as much to adjudicate either way. (This is a
    SHAPE test, per P1's own quantification — it asks whether anything was declared, never
    whether what was declared is TRUE. That is P2's.)"""
    if field not in spec:
        return False
    value = spec[field]
    if value is None:
        return False
    if isinstance(value, (str, bytes, list, tuple, set, frozenset, dict)) and len(value) == 0:
        return False
    return True


def _p1_names_a_source(spec: dict, field: str) -> bool:
    """An independence declaration NAMES an authority. §4 WRONG TYPE: `reads: 17`, `reads: None`
    and `independent_source: []` are keys, not names, and a declaration that names nothing leaves
    the guard exactly as unarmed as an absent one. The P3 type gate refuses these too — but it
    refuses them because of the manifest's TYPE, and a future manifest that widened the field to
    _T_ANY would retire that refusal silently. The activation requirement must stand on its own."""
    return _p1_declared(spec, field) and isinstance(spec[field], str) and spec[field].strip() != ""


def _guard_independence_activation(spec: dict, cid: str) -> Optional[dict]:
    if any(_p1_names_a_source(spec, field) for field in _INDEPENDENCE_DECLARATION_FIELDS):
        return None
    return _problem(spec.get("relation", "?"), "INDEPENDENCE_UNDECLARED",
                    f"{cid}: the spec names no independent source for its observed operand "
                    f"(none of {list(_INDEPENDENCE_DECLARATION_FIELDS)} carries a non-empty "
                    "source name), so the "
                    "independence guard has nothing to adjudicate and runs as a no-op. A guard "
                    "that is reachable in a no-op configuration is not a control: an oracle that "
                    "is a second copy of the collection is indistinguishable from an independent "
                    "one when neither states what it reads; REFUSED")


def guard_independence(spec: dict, cid: str) -> Optional[dict]:
    """Refuse a spec whose observed provider reads the declared collection constant itself, whose
    declared independent source is an alias of the collection id, or which declares no independent
    source AT ALL (P1: the guard must not be switchable off by omission). Returns a Problem dict or
    None."""
    return (_guard_independence_content(spec, cid)
            or _guard_independence_activation(spec, cid))


def _guard_directionality_content(relation: str, spec: dict) -> Optional[dict]:
    canonical = resolve_relation(relation)
    if canonical is None:
        return _problem(relation, "UNKNOWN_RELATION", f"relation {relation!r} unknown; REFUSED")
    declared = spec.get("load_bearing_direction")
    if declared is None:
        return None
    # §4 WRONG TYPE. `load_bearing_direction` is manifested _T_ANY, so the P3 type gate does not
    # narrow it and this is the only place its SHAPE is adjudicated. A mapping was the live escape:
    # `{"missing": True}` is truthy, so it counted as declared, and `set(mapping)` silently
    # degrades to its KEYS — the declaration was read as {"missing"} and certified. A direction is
    # a name or a set of names; anything else is a form the guard cannot adjudicate, so it fails
    # closed rather than being coerced into one it can.
    if isinstance(declared, str):
        directions = {declared}
    elif isinstance(declared, (list, tuple, set, frozenset)) and all(
            isinstance(d, str) for d in declared):
        directions = set(declared)
    else:
        return _problem(canonical, "DIRECTION_MALFORMED",
                        f"load_bearing_direction is a {type(declared).__name__}; a load-bearing "
                        "direction is a name or a set of names. A form the guard cannot read is "
                        "not a declaration, and coercing it would adjudicate something the design "
                        "never said; REFUSED")
    supported = _RELATION_DIRECTIONS.get(canonical, set())
    unwitnessable = directions - supported
    if unwitnessable:
        return _problem(canonical, "DIRECTION_UNWITNESSABLE",
                        f"the design's load-bearing direction(s) {sorted(unwitnessable)} cannot "
                        f"be reported by {canonical} (it reports {sorted(supported)}); a failure "
                        "that can never be reported is fail-open; REFUSED")
    return None


def _guard_directionality_activation(relation: str, spec: dict) -> Optional[dict]:
    canonical = resolve_relation(relation)
    if canonical is None:
        return _problem(relation, "UNKNOWN_RELATION", f"relation {relation!r} unknown; REFUSED")
    if _p1_declared(spec, "load_bearing_direction"):
        return None
    supported = _RELATION_DIRECTIONS.get(canonical, set())
    return _problem(canonical, "DIRECTION_UNDECLARED",
                    "the spec declares no `load_bearing_direction` (absent, null or empty), so "
                    "the directionality guard "
                    f"cannot check whether {canonical} (which reports {sorted(supported)}) is able "
                    "to report the failure this design exists to catch. The guard would run as a "
                    "no-op and the design would be certified in a direction the relation can never "
                    "fire in; an undeclared load-bearing direction is REFUSED")


def guard_directionality(relation: str, spec: dict) -> Optional[dict]:
    """Refuse a spec whose declared load-bearing direction the relation cannot report a finding
    in — e.g. a provenance design that says 'a member with no provenance record is the failure'
    (direction 'unknown') paired with REQUIRED_SUPERSET (which only fires on 'missing') is
    fail-open — and equally refuse a spec that declares NO load-bearing direction at all (P1: the
    guard must not be switchable off by omission). Returns a Problem or None."""
    return (_guard_directionality_content(relation, spec)
            or _guard_directionality_activation(relation, spec))


def _p1_field_is_expressible(spec: dict, relation: str, field: str) -> bool:
    """Is `field` inside the P3 field universe of the forms THIS spec declares?

    P1's quantifier ranges over the guards ON A SPEC'S EVALUATION PATH, and the contract is
    explicit that "P3 is what makes P1's universal quantifier WELL-DEFINED". A guard whose
    activating field P3 would REFUSE as undeclared for this spec's form is not on that spec's path:
    demanding a field the schema gate forbids is not fail-closed, it is a contradiction between two
    properties that makes the form unusable.

    Concretely: `reads` / `independent_source` / `load_bearing_direction` are declared ONLY by the
    four FRAMEWORK forms. A relation-only spec (the P7 inline / code-native observation, which has
    no provider and no framework kind) cannot carry any of them — so for that form the comparator
    guards are NOT_APPLICABLE, and its independence obligation is discharged in code instead, by
    the CodeNativeWitness's own `reads` channel, which resolve_witness_fields() checks against the
    collection id (witness_reads_collection) before the witness is ever executed.

    Derived from the manifest, never listed by hand: adding the field to a form's vocabulary
    automatically extends the obligation to that form."""
    forms = _declared_forms(spec, relation=relation)
    required, optional, _ = _union_manifest(forms, spec)
    return field in required or field in optional


# The two directions ANY set-vs-set adjudication can produce: a member the domain has and the
# collection does not (`missing`), and one the collection has and the domain does not (`unknown`).
# `overlap`, `inert` and `drift` are the load-bearing directions of DIFFERENT comparison shapes
# (disjointness, execution inertness, hash drift), not alternative readings of a set inclusion.
_SET_DIFFERENCE_DIRECTIONS = frozenset({"missing", "unknown"})


def _p1_direction_is_adjudicable(relation: str) -> bool:
    """Can the directionality guard REFUSE anything at all for this relation?

    §3 absent-but-required vs genuinely-not-applicable, decided by computation rather than by
    listing relations. The guard's whole rule is `declared - supported != {}`. A relation that
    reports BOTH set-difference directions (EXACT, PARTITION, KEYED_MAPPING, KMAU,
    SCHEMA_STRICTNESS, PROVENANCE_CORRESPONDENCE, SEMANTIC_REACHABILITY) cannot be fail-open in
    either direction a set inclusion can produce, so no load-bearing direction a DESIGN could hold
    is unwitnessable by it and the declaration decides nothing: NOT_APPLICABLE. A relation that
    reports only one of them — REQUIRED_SUBSET (`unknown` only), REQUIRED_SUPERSET (`missing`
    only), DISJOINT/DISJOINT_WITH_FLOOR (`overlap` only), DIFFERENTIAL_EXECUTION, HASH_BACKSTOP —
    is silently blind in the other, and a design whose real failure lies there can never fire:
    that is the fail-open W11 exists for, so the declaration is MANDATORY.

    Derived from _RELATION_DIRECTIONS, so a relation added with a narrower reporting set inherits
    the obligation automatically instead of needing a second edit here."""
    canonical = resolve_relation(relation)
    supported = _RELATION_DIRECTIONS.get(canonical, set()) if canonical else set()
    return not _SET_DIFFERENCE_DIRECTIONS.issubset(supported)


def _p1_activation_problems(relation: str, spec: dict, cid: str) -> list:
    """Every ACTIVATION refusal owed by the comparator-layer guards on this spec's evaluation path.

    ONE place enumerates the guards, so adding a guard without adding its activation obligation is
    a visible omission here rather than an invisible one at a call site (the I28AM sibling-layer
    lesson: a fix that lands on one guard and not its sibling is the defect, restated).

    INDEPENDENCE IS NOT ENFORCED HERE — SEE THE RESIDUAL BELOW.
    `guard_independence` carries the full contract at its own boundary (absent declaration =>
    INDEPENDENCE_UNDECLARED), but the entry point does not invoke that half, because the landed
    provider positive controls (P7 `p_norm_ok`, P9 POS-1/POS-2, P1-POS1) are provider-backed specs
    that declare NO `reads` and are required to stay CLEAN. Enforcing absence here refuses all of
    them. That is an ACKNOWLEDGED OPEN RESIDUAL of P1, not a closure: a registry-backed provider
    that never says what it reads still reaches a verdict through this path. The contract's own
    measured instance of W10 (an inline `observed` operand with no declaration) IS closed, in code
    rather than in schema — CodeNativeWitness makes `reads` a mandatory constructor keyword and
    resolve_witness_fields() runs witness_reads_collection() before the witness executes."""
    out = []
    if (_p1_field_is_expressible(spec, relation, "load_bearing_direction")
            and _p1_direction_is_adjudicable(relation)):
        problem = _guard_directionality_activation(relation, spec)
        if problem is not None:
            out.append(problem)
    return out


# ============================================================================================
# PART B2/B3 — P7: WITNESS FORM + DATA-AUTHORED LEVERS   (Gate 4N-I28BH-B0w-R, agent-7)
# ============================================================================================
# GRAFTED onto the compose4 base (P1/P2/P3/P5/P9) at Gate 4N-I28BH-B0w-R2-RM1 stage 1. P7 was
# authored against a pre-P9 base, so only its ADDITIVE machinery crosses over; its own
# provider layer (a bare dict registry) is deliberately left behind — compose4's P9
# WitnessRegistry is strictly stronger and replacing it would be a P9 regression.
_VETTED = object()          # module-private marker; not JSON-expressible, not forgeable from data


class WitnessFormError(FrameworkError):
    """A witness supplied in a form the contract does not permit. Structural mis-wiring."""


class _WitnessProvenance:
    """Module-private, non-JSON-expressible record of WHICH witness fields were resolved by the P7
    gate and FROM WHAT CODE.

    P7-FIND-01 (siblings). The type barrier above stops a JSON `observed` reaching
    `_resolve_observed`. It does NOT stop `member_effect`, `baseline_healthy`,
    `unknown_probe_accepted` or `value_domain` reaching a comparator, because those four are read
    by the RELATION CHECKERS inside `compare()`, which is a public entry the gate does not sit in
    front of. Each is an independent instance of BYP-4: a JSON `{"m": true}` certifies every member
    load-bearing with zero execution; a JSON `true` certifies a baseline nobody ran; a JSON `false`
    certifies a closed schema nobody probed. Stamping a marker on the SPEC is not enough — the
    consumption site must be able to ask "was THIS field produced by executed code?", so the record
    is per-field and carries the producing witness's origin frame.

    `json.loads` can never construct one and `json.dumps`/`pickle` refuse it, so a fixture, a spec
    literal, or a serialised round trip cannot forge the record; the check at every consumption
    site is `type(record) is _WitnessProvenance`, never `isinstance` (I28AB: name-only trust).

    FF-10 / §13. THE TYPE IS NOT THE AUTHORITY. Exact type held against JSON, a lookalike and a
    SUBCLASS, and against nothing else: this constructor takes a caller-supplied dict, so any code
    that can import the module could mint a genuinely-typed record certifying authored fields as
    executed. Constructing one is therefore no longer an act with any meaning. An INSTANCE is an
    inert carrier; the AUTHORITY lives in a ledger held in a closure (_provenance_authority_plane,
    below the P7 gate), is issued ONLY by the gate execution that actually ran the witness, and is
    keyed by the record's OBJECT IDENTITY — so it cannot be matched by structure, copied onto
    another object, replayed through the constructor, subclassed into, spoofed by equality, or
    reached by setting an attribute. Deliberately NOT a `trusted=True` field: a field is data, and
    the whole finding is that data cannot certify execution."""

    __slots__ = ("_records", "__weakref__")

    def __init__(self, records: dict):
        object.__setattr__(self, "_records", dict(records))

    def __contains__(self, field: str) -> bool:
        return field in self._records

    def origin(self, field: str) -> Optional[dict]:
        return self._records.get(field)

    def fields(self) -> tuple:
        return tuple(sorted(self._records))

    def __reduce__(self):
        raise WitnessFormError("witness provenance is not serialisable; a record of executed "
                               "observation may not be carried in data")

    def __iter__(self):
        raise WitnessFormError("witness provenance is not iterable")

    def __repr__(self):
        return f"<_WitnessProvenance {self.fields()}>"


_WITNESS_PROVENANCE_KEY = "_witness_provenance"


def _provenance_of(spec: dict) -> Optional["_WitnessProvenance"]:
    """The provenance record, or None. EXACT type: a caller-supplied lookalike is not a record.

    Exact type is now the CHEAP half of the test. The authority half — was this object issued by an
    execution of the P7 gate, and does it still bind the value being read? — is
    _provenance_binding_refusal() below, which every consumption site runs."""
    record = spec.get(_WITNESS_PROVENANCE_KEY) if isinstance(spec, dict) else None
    return record if type(record) is _WitnessProvenance else None


def _provenance_canonical(value: Any) -> str:
    """A order-independent, type-tagged rendering of an OBSERVED value, so the binding survives
    set/dict iteration order but not a change of type or content."""
    if isinstance(value, (set, frozenset)):
        return "set{" + ",".join(sorted(_provenance_canonical(v) for v in value)) + "}"
    if isinstance(value, dict):
        return "map{" + ",".join(f"{_provenance_canonical(k)}:{_provenance_canonical(value[k])}"
                                 for k in sorted(value, key=repr)) + "}"
    if isinstance(value, (list, tuple)):
        return f"{type(value).__name__}[" + ",".join(_provenance_canonical(v) for v in value) + "]"
    return f"{type(value).__name__}({value!r})"


def _provenance_value_digest(value: Any) -> str:
    return hashlib.sha256(_provenance_canonical(value).encode("utf-8")).hexdigest()


def _provenance_producer_digest(witness: Any) -> str:
    """The identity of the CODE that produced an observation, recomputed live.

    Recomputed, never read back from a supplied field: the whole point is that the comparand must
    not be movable by the party being checked (the I28AE/P9 rule, applied to the producer behind a
    provenance record)."""
    producer = getattr(witness, "_producer", None)
    code = getattr(producer, "__code__", None)
    if code is None:
        return f"<non-code producer {type(producer).__name__}>"
    return _p9_code_fingerprint(code)


def _provenance_binding_refusal(record: "_WitnessProvenance", field: str, value: Any,
                                spec: dict) -> Optional[str]:
    """None when `record` is a LIVE authority for `field` carrying exactly `value`; otherwise the
    sentence saying which binding failed.

    Five independent bindings, each closing a different §14 forgery class:
      1. ISSUANCE   the object must be in the gate's ledger — closes constructor replay, structural
                    matching, copy/deepcopy, serialise/deserialise, subclassing, equality/hash
                    spoofing, attribute copying and monkey-patched fields, because none of those
                    produce the object the gate registered.
      2. FIELD      the ledger must hold a binding for THIS field — a record that witnessed one
                    field cannot vouch for a second one the caller added.
      3. VALUE      the value now in the spec must be the value the witness produced — closes the
                    post-validation swap, and closes copying a record onto a different observation.
      4. PRODUCER   the producing callable's code identity, RECOMPUTED now, must still be the one
                    that executed — closes a capability used after the callable was swapped.
      5. COLLECTION the record was issued for one collection; a spec that names another is not the
                    thing that was observed — closes carrying a capability to another witness."""
    authority = _witness_provenance_authority(record)
    if authority is None:
        return ("the record is a _WitnessProvenance the CALLER minted: it was never issued by the "
                "P7 gate execution that runs witnesses, so it records nothing that happened. "
                "Provenance is an authority the framework ISSUES from an executed observation, "
                "not a shape a caller can match — constructing the class is not evidence")
    binding = authority["bindings"].get(field)
    if binding is None:
        return (f"the provenance record holds no execution binding for {field!r}: the gate that "
                "issued it executed a witness for other fields, and an authority earned by one "
                "observation does not extend to a field the caller added afterwards")
    if _provenance_value_digest(value) != binding["value_digest"]:
        return (f"the value now in the spec for {field!r} is NOT the value the witness produced: "
                "it was replaced after the gate validated it, so the record certifies an "
                "observation that is no longer there")
    if _provenance_producer_digest(binding["witness"]) != binding["producer_digest"]:
        return (f"the callable that produced {field!r} has been REBOUND since it executed: the "
                "reviewed producer is not the one now behind the record, so the record attributes "
                "the observation to code that did not make it")
    declared = spec.get("source_collection_id")
    if declared is not None and declared != authority["cid"]:
        return (f"the record was issued while adjudicating {authority['cid']!r} and this spec "
                f"declares {declared!r}: an execution authority belongs to the observation that "
                "earned it and cannot be carried to another witness")
    return None


def witness_field(spec: dict, field: str, relation: str, problems: list):
    """THE single consumption path for a self-adequacy witness field. Returns (value, present).

    Contract, applied identically at every site:
      * field ABSENT      -> (None, False); the relation's own missing-witness refusal applies.
      * field PRESENT and produced by a CodeNativeWitness that passed the P7 gate -> (value, True).
      * field PRESENT by any other route -> a SELF_ADEQUACY_UNWITNESSED Problem and (None, False).

    The third case is the closure. A self-adequacy field is not an input naming which authority to
    consult; it IS the answer to the question the relation exists to ask. Authored in data it
    certifies whatever it was written to certify, and it stays CLEAN when the collection is
    shortened alongside it — the BYP-4 shape exactly. Provider-computed or refused."""
    if field not in spec:
        return None, False
    record = _provenance_of(spec)
    unbound = None if record is None else _provenance_binding_refusal(record, field, spec[field],
                                                                     spec)
    if record is None or field not in record or unbound is not None:
        problems.append(_problem(
            relation, "SELF_ADEQUACY_UNWITNESSED",
            f"spec field {field!r} carries a {type(spec[field]).__name__} that was not produced by "
            "an executed witness: it is the ANSWER this relation exists to compute, authored as "
            "data. A self-adequacy field certifies whatever it was written to certify and survives "
            "the collection being shortened alongside it. Supply it as a CodeNativeWitness the "
            "framework executes (resolve_witness_fields), or the claim is REFUSED"
            + (f" [provenance unbound: {unbound}]" if unbound is not None else "")))
        return None, False
    return spec[field], True


class CodeNativeWitness:
    """The ONLY carrier a spec may use for a code-native observation.

    Non-serialisable BY CONSTRUCTION: `json.dumps` refuses it (no encoder), `pickle` refuses it
    (`__reduce__` raises), and it is not iterable or sized, so a leak into a set operation CRASHES
    rather than degrading into a silent operand. `json.loads` can therefore never produce one:
    a tracked JSON fixture cannot carry an observation, at any nesting depth, by any encoding.

    Wraps a CALLABLE, never a literal. A literal would be an assertion, not an observation, and
    could not be re-executed — which would make P6's computational independence check unrunnable
    on exactly the specs most likely to be lying. The framework calls the producer; the caller
    never hands over a finished answer.

    `reads` is mandatory and names the authority the producer consults, so a code-native witness
    faces the SAME independence guard as a registered provider (guard_independence), and the
    origin frame is captured at construction so a witness can be attributed to reviewed code.
    """

    __slots__ = ("_producer", "reads", "label", "origin_module", "origin_file", "origin_line")

    def __init__(self, producer: Callable, *, reads: str, label: Optional[str] = None):
        if not callable(producer):
            raise WitnessFormError(
                "CodeNativeWitness wraps a CALLABLE the framework executes, not a finished value; "
                f"got {type(producer).__name__}. A literal is an assertion, not an observation, "
                "and cannot be re-executed for the computational-independence check.")
        if not isinstance(reads, str) or not reads:
            raise WitnessFormError(
                "CodeNativeWitness requires reads=<authority-id>: an undeclared witness cannot be "
                "checked for independence from the collection it certifies")
        object.__setattr__(self, "_producer", producer)
        object.__setattr__(self, "reads", reads)
        object.__setattr__(self, "label", label or getattr(producer, "__qualname__", "<producer>"))
        frame = sys._getframe(1)
        object.__setattr__(self, "origin_module", frame.f_globals.get("__name__", "<unknown>"))
        object.__setattr__(self, "origin_file", frame.f_code.co_filename)
        object.__setattr__(self, "origin_line", frame.f_lineno)

    def observe(self, spec: dict) -> Any:
        # P4: a code-native witness is still a witness — it observes, it does not adjudicate.
        return _call_witness(self._producer, spec)

    # --- the non-serialisability barrier, asserted rather than assumed ---
    def __reduce__(self):
        raise WitnessFormError("a CodeNativeWitness is not picklable; a witness may not be "
                               "serialised into data")

    def __iter__(self):
        raise WitnessFormError("a CodeNativeWitness is not iterable; it must be resolved through "
                               "resolve_witness_fields(), never consumed as an operand")

    def __len__(self):
        raise WitnessFormError("a CodeNativeWitness has no length; it must be resolved through "
                               "resolve_witness_fields()")

    def __repr__(self):
        return (f"<CodeNativeWitness {self.label} reads={self.reads!r} "
                f"from {self.origin_module}:{self.origin_line}>")


# Every spec field whose value IS, or ENCODES, an OBSERVATION — i.e. a field a caller could use to
# hand the framework a finished answer instead of letting the framework look. Each one is an
# independent instance of BYP-4, not just `observed`:
_INLINE_WITNESS_FIELDS = {
    "observed": "the independent observed operand itself (BYP-4)",
    "member_effect": "the per-member DIFFERENTIAL_EXECUTION result; {m: True for m in C} "
                     "certifies every member load-bearing with zero execution",
    "baseline_healthy": "the observation that the unmutated baseline is healthy; a JSON `true` "
                        "certifies a baseline nobody ran",
    "unknown_probe_accepted": "the observed outcome of the SCHEMA_STRICTNESS unknown-token probe; "
                              "a JSON `false` certifies a closed schema with zero execution",
    "value_domain": "the observed value universe a KEYED_MAPPING_AGAINST_UNION is checked against",
}

# Deferred by design, NOT ungoverned: `partition_members` and `observed_hash` are caller-supplied
# OPERANDS rather than observations and belong to P6 (operands derived, never accepted);
# `baseline_hash` is a reviewed PIN, governed by the pin registry. Named here so a later reader can
# see they were considered and routed, not missed.
_P6_DEFERRED_FIELDS = ("partition_members", "observed_hash")

# P6 integration slot. Agent-5 (P6/P2-computational) installs the real implementation by binding a
# module attribute of this name. It is FAIL-CLOSED while unbound: an unverified witness is refused,
# never waved through, so P7 cannot ship ahead of P6 and quietly widen the contract.
_P6_HOOK_NAME = "verify_computational_independence"


def _p6_check(witness: "CodeNativeWitness", field: str, spec: dict, collection: Any,
              cid: str) -> Optional[dict]:
    """Route a code-native witness through P6. Returns a Problem or None."""
    hook = globals().get(_P6_HOOK_NAME)
    if hook is None:
        return _problem(spec.get("relation", "?"), "WITNESS_INDEPENDENCE_UNVERIFIED",
                        f"{cid}: code-native witness {witness.label!r} for field {field!r} was not "
                        f"checked for computational independence: the P6 hook "
                        f"{_P6_HOOK_NAME!r} is not installed. An unverified witness is REFUSED; "
                        "absence of a check is never a pass")
    return hook(witness, field, spec, collection, cid)


def resolve_witness_fields(spec: dict, cid: str, collection: Any, problems: list):
    """P7 gate. Returns (resolved_spec, ok).

    REFUSES any witness-bearing field whose value is not exactly a CodeNativeWitness — which
    rejects a JSON list/bool/dict, a raw Python set, a bare lambda, a closure, a dict mapping, and
    a subclass impersonating the sentinel (exact type, per the I28AB name-only-trust lesson).
    Accepted witnesses are then run through guard_independence's rule (reads != cid) and through
    P6, and only then EXECUTED by the framework. The resolved copy carries a module-private marker
    so no downstream reader can consume a witness field that did not pass through here (P4).
    """
    relation = spec.get("relation", "?")
    resolved = dict(spec)
    resolved.pop(_WITNESS_PROVENANCE_KEY, None)   # a caller-supplied record is never carried over
    provenance: dict = {}
    # FF-10: the EXECUTION bindings behind the record. Never returned to the caller and never
    # placed in the spec — a binding the caller could see is a binding the caller could match.
    bindings: dict = {}
    ok = True
    for field, why in _INLINE_WITNESS_FIELDS.items():
        if field not in spec:
            continue
        value = spec[field]
        if type(value) is not CodeNativeWitness:
            expressible = _is_json_expressible(value)
            problems.append(_problem(
                relation, "JSON_EXPRESSIBLE_WITNESS" if expressible else "UNDECLARED_WITNESS_FORM",
                f"{cid}: spec field {field!r} carries "
                f"{'a JSON-expressible' if expressible else 'an undeclared code-native'} "
                f"{type(value).__name__} — {why}. A witness value the caller authors is a copied "
                "oracle, not an observation: it certifies whatever it was written to certify, and "
                "it stays CLEAN when the collection is shortened alongside it. Only a "
                "CodeNativeWitness (a callable the framework executes, supplied from reviewed "
                "code) is accepted; REFUSED"))
            ok = False
            continue
        if witness_reads_collection(value, cid):
            problems.append(_problem(relation, "COPIED_ORACLE",
                                     f"{cid}: the code-native witness for {field!r} declares "
                                     f"reads={value.reads!r}, the declared collection itself; a "
                                     "second copy of the list is not an oracle; REFUSED"))
            ok = False
            continue
        p6 = _p6_check(value, field, spec, collection, cid)
        if p6 is not None:
            problems.append(p6)
            ok = False
            continue
        try:
            # P6: the framework INJECTS the declared authority and runs the witness in
            # the SAME restricted namespace the independence trials ran it in.
            resolved[field] = _p6_observe(value, spec, collection, cid, field)
        except _P6ExecutionRefused as refusal:
            # A refusal by the execution-path P6 check reports the kind the DETECTOR
            # produced. Collapsing it into WITNESS_RAISED would name the wrong control:
            # 'the witness crashed' and 'the witness is not confinable' are different
            # findings, and a reader debugging the first would never look for the second.
            problems.append(refusal.problem)
        except Exception as exc:                   # a witness that crashes is not clean
            problems.append(_problem(relation, "WITNESS_RAISED",
                                     f"{cid}: code-native witness {value.label!r} for {field!r} "
                                     f"raised {type(exc).__name__}: {exc}; not clean"))
            ok = False
            continue
        # P7-FIND-01 (siblings): record WHERE the value came from, per field, so the comparator can
        # tell an executed observation from a spec literal at the point it reads it.
        provenance[field] = {"label": value.label, "reads": value.reads,
                             "origin_module": value.origin_module,
                             "origin_file": value.origin_file,
                             "origin_line": value.origin_line}
        # FF-10: bind the record to what ACTUALLY happened — the value this execution produced and
        # the code that produced it. Both are recomputed at the consumption site, so neither is a
        # comparand the party being checked can move.
        bindings[field] = {"value_digest": _provenance_value_digest(resolved[field]),
                           "producer_digest": _provenance_producer_digest(value),
                           "witness": value}
    resolved["_witness_vetted"] = _VETTED
    resolved[_WITNESS_PROVENANCE_KEY] = _mint_witness_provenance(provenance, bindings, cid)
    return resolved, ok


# ============================================================================================
# FF-10 / §13 — THE PROVENANCE AUTHORITY PLANE
# ============================================================================================
# The ledger and the code object that may write to it live in THIS closure, not in the module
# namespace, and the minting function refuses any caller whose frame is not the P7 gate's own code
# object — captured here, once, so rebinding the module attribute `resolve_witness_fields` does not
# move the authority. A caller cannot obtain an issuance: not by constructing the record class, not
# by matching its structure, not by calling the mint (its frame is wrong), not by declaring itself
# trusted (there is no such field), and not by finding the ledger (it is not a module attribute).
#
# HONEST LIMIT, stated rather than absorbed. Nothing in CPython is beyond an in-process attacker
# who is already executing arbitrary code inside the framework's own module: the ledger is
# reachable through this function's __closure__, and the two names bound below can be rebound. That
# is the same limit `_VETTED` and the P9 registries already carry, and it is a DIFFERENT act from
# the one FF-10 exercised — reaching into a verifier's closure is tampering with the verifier, and
# is what the executed-code-provenance layer governs. What is closed here is the forgery that
# needed no such act: minting a genuine record from data.
def _provenance_authority_plane(_gate_code):
    ledger: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()

    def mint(records: dict, bindings: dict, cid: str) -> "_WitnessProvenance":
        frame = sys._getframe(1)
        if frame.f_code is not _gate_code:
            raise WitnessFormError(
                "witness provenance may be issued ONLY by the P7 gate execution that ran the "
                f"witness; {frame.f_code.co_name}() at {frame.f_code.co_filename}:"
                f"{frame.f_lineno} is not that execution path. A record any caller could mint is "
                "data, and data cannot certify that code ran; REFUSED (FF-10)")
        record = _WitnessProvenance(records)
        ledger[record] = {"cid": cid, "bindings": dict(bindings)}
        return record

    def authority(record) -> Optional[dict]:
        """The issuance for THIS object, or None. Keyed by object identity: equality, hash, type
        and attribute content are all irrelevant, so no amount of shaping reaches an entry."""
        if type(record) is not _WitnessProvenance:
            return None
        try:
            return ledger.get(record)
        except TypeError:                      # an unhashable impostor is not an issuance
            return None

    return mint, authority


_mint_witness_provenance, _witness_provenance_authority = _provenance_authority_plane(
    resolve_witness_fields.__code__)


def witness_reads_collection(witness: "CodeNativeWitness", cid: str) -> bool:
    return witness.reads == cid


def _is_json_expressible(value: Any) -> bool:
    """True when the value could have come out of a tracked JSON fixture. Used for diagnosis; the
    ENFORCEMENT rule above is the stricter exact-type test, so a non-JSON smuggling form (a raw
    set, a lambda, a closure, a Python mapping) is refused too rather than merely reported."""
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return False
    return True


# --- fixture-schema hook (the boundary is NOT inside one file: A6-EQ-03) --------------------
# tests/fixtures/collection-completeness-specs.json is an unvalidated, un-mutation-covered input to
# a security control. The type barrier above stops a witness reaching the comparator; this stops it
# being WRITTEN. ALLOWLIST-PRIMARY on purpose: a denylist of known witness fields is exactly the
# "an eleventh always exists" shape — the NEXT witness form is a NEW field name. An unknown key is
# refused, and the named witness fields get a specific message on top.
_FIXTURE_TOP_LEVEL_KEYS = frozenset({"_comment", "specs"})
_FIXTURE_COMMON_KEYS = frozenset({"_note", "resolver", "domain_class", "partition_group"})
_FIXTURE_RESOLVER_KEYS = {
    "module_constants": frozenset({"module", "name_pattern"}),
    "function_result_keys": frozenset({"module", "function", "args"}),
    "emitted_policy": frozenset({"module", "function", "args", "effect"}),
    "discovered_kinds": frozenset({"module", "function", "field"}),
    "authored_contract": frozenset({"path", "pointer"}),
}


def _scan_for_witness_keys(node: Any, path: str, found: list) -> None:
    """Recursive: a witness key is refused at ANY depth, inside nested objects and inside lists,
    not only at the top level of a spec."""
    if isinstance(node, dict):
        for key, value in node.items():
            here = f"{path}.{key}" if path else key
            if key in _INLINE_WITNESS_FIELDS:
                found.append((here, key))
            _scan_for_witness_keys(value, here, found)
    elif isinstance(node, (list, tuple)):
        for index, value in enumerate(node):
            _scan_for_witness_keys(value, f"{path}[{index}]", found)


def validate_spec_fixture(doc: Any, source: str = "<specs fixture>") -> list:
    """Refuse a tracked spec fixture that carries an observation, or any unregistered field.
    Returns a list of problem STRINGS; [] means the fixture declares only inputs, never answers."""
    problems: list = []
    if not isinstance(doc, dict):
        return [f"{source}: the specs fixture is not a JSON object; fail-closed"]
    for key in sorted(set(doc) - _FIXTURE_TOP_LEVEL_KEYS):
        problems.append(f"{source}: unregistered top-level key {key!r}; the fixture schema is "
                        "CLOSED — a new key must be registered in a contract before it is read")
    specs = doc.get("specs")
    if not isinstance(specs, dict):
        return problems + [f"{source}: 'specs' is absent or not an object; fail-closed"]

    witness_hits: list = []
    _scan_for_witness_keys(specs, "specs", witness_hits)
    for where, key in witness_hits:
        problems.append(
            f"{source}: {where} declares {key!r} — {_INLINE_WITNESS_FIELDS[key]}. A tracked JSON "
            "fixture may declare INPUTS (which authority to consult) but never the ANSWER; an "
            "observation authored in data is a copied oracle and stays CLEAN when the collection "
            "is shortened alongside it. REFUSED (BYP-4)")

    for cid, spec in sorted(specs.items()):
        if not isinstance(spec, dict):
            problems.append(f"{source}: spec for {cid!r} is not an object; fail-closed")
            continue
        resolver = spec.get("resolver")
        permitted = _FIXTURE_RESOLVER_KEYS.get(resolver)
        if permitted is None:
            problems.append(f"{source}: {cid}: resolver {resolver!r} has no registered field "
                            "manifest; an unregistered resolver fails closed")
            continue
        for key in sorted(set(spec) - _FIXTURE_COMMON_KEYS - permitted):
            if key in _INLINE_WITNESS_FIELDS:
                continue                     # already reported above with the specific reason
            problems.append(f"{source}: {cid}: unregistered field {key!r} for resolver "
                            f"{resolver!r}; the spec schema is CLOSED, so a new witness form "
                            "cannot be introduced as a new field without registering a contract")
        # P7-FIND-02 / P7-FIND-03: the schema admits the FIELDS `path`/`pointer` and `args`, but a
        # field being registered says nothing about whether its VALUE is independent of the
        # collection. Both are adjudicated against a per-spec reviewed pin, at LOAD.
        # P8: the SAME adjudicator the programmatic chokepoints call, so a fixture value and an
        # injected value are governed by identical code and produce identical text.
        problems.extend(steering_pin_problems(spec, cid, source=source))
    return problems


# ============================================================================================
# PART B3 — P7-FIND-02/03/04: DATA-AUTHORED LEVERS THAT ARE NOT WITNESS *VALUES*
# ============================================================================================
# The type barrier (B2) closes the field whose VALUE is the answer. Three levers remain, and none
# of them is a witness value, which is exactly why the type barrier does not reach them:
#
#   FIND-02  `authored_contract` names WHICH FILE is the domain. The allowlist admits the field; it
#            cannot judge whether the FILE is independent of the collection it certifies.
#   FIND-03  `args` steers WHAT THE PRODUCER EMITS, i.e. it selects the observation without ever
#            authoring one.
#   FIND-04  `normalize` is a transform of BOTH operands: it redefines what "equal" means, so a
#            caller-chosen directive can make a genuinely missing member compare as present.
#
# The rule shared by all three: DATA MAY NAME AN AUTHORITY, NEVER CHOOSE THE ANSWER. Where the
# choice is unavoidable, it is moved out of the fixture into a REVIEWED PIN (FIND-02, FIND-03) or
# DERIVED from a property of the domain rather than authored per spec (FIND-04). Each pin is a
# no-override registry (I28AI precedent) and each check fails closed on absence.

class ContractPinError(FrameworkError):
    """A domain-source or steering pin that is missing, redirected, or drifted."""


# --- FIND-02: authored_contract per-spec independence pin -----------------------------------
# An authored contract is the ONE resolver whose domain is not observed behaviour — it is a
# reviewed requirement document. That makes its PROVENANCE the whole of its authority: the file
# must be the reviewed one, at the reviewed path, with the reviewed content, bound to THIS
# collection. Without the pin, `path` is free text: a spec edit silently redirects the domain to
# any JSON file in the tree, including one the collection's own producer writes, and the
# comparison becomes a collection checked against itself.
_AUTHORED_CONTRACT_ROOTS = ("tests/fixtures/",)   # authored + reviewed locations, never generated
_AUTHORED_CONTRACT_PINS: dict = {}
AUTHORED_CONTRACT_PINS = types.MappingProxyType(_AUTHORED_CONTRACT_PINS)


def register_authored_contract_pin(cid: str, *, path: str, pointer: str, sha256: str,
                                   independence: str) -> None:
    """No-override. Re-pointing a live collection's domain file is substitution, not configuration
    — the same act BYP-4 performed with a value, performed with a path."""
    if not (isinstance(sha256, str) and len(sha256) == 64):
        raise ContractPinError(f"{cid}: an authored-contract pin requires a full sha256 of the "
                               "reviewed document; a partial or absent digest cannot detect drift")
    if not independence:
        raise ContractPinError(f"{cid}: an authored-contract pin requires a written independence "
                               "rationale: why this document is not derived from the collection")
    existing = _AUTHORED_CONTRACT_PINS.get(cid)
    entry = {"path": path, "pointer": pointer, "sha256": sha256, "independence": independence}
    if existing is not None and existing != entry:
        raise ContractPinError(
            f"{cid}: an authored-contract pin already binds this collection to {existing['path']!r}; "
            "re-pinning a live domain source is REFUSED")
    _AUTHORED_CONTRACT_PINS[cid] = entry


def check_authored_contract_pin(cid: str, spec: dict, source: str = "<spec>") -> list:
    """Adjudicate an `authored_contract` spec against its pin. Returns problem STRINGS."""
    pin = _AUTHORED_CONTRACT_PINS.get(cid)
    if pin is None:
        return [f"{source}: {cid}: the spec names {spec.get('path')!r} as the domain document, "
                "but no per-spec independence pin exists. An authored contract has no "
                "observed behaviour behind it — its provenance IS its authority — so an unpinned "
                "contract path is free text that can be redirected at any JSON file in the tree, "
                "including one the collection's own producer writes. REFUSED (P7-FIND-02)"]
    problems = []
    if spec.get("path") != pin["path"]:
        problems.append(f"{source}: {cid}: the spec names domain file {spec.get('path')!r} but the "
                        f"reviewed pin binds this collection to {pin['path']!r}; redirecting a "
                        "domain source is witness substitution; REFUSED (P7-FIND-02)")
    if spec.get("pointer", "") != pin["pointer"]:
        problems.append(f"{source}: {cid}: the spec reads pointer {spec.get('pointer', '')!r} but "
                        f"the reviewed pin binds {pin['pointer']!r}; selecting a different slice of "
                        "the contract selects a different domain; REFUSED (P7-FIND-02)")
    if not any(pin["path"].startswith(root) for root in _AUTHORED_CONTRACT_ROOTS):
        problems.append(f"{source}: {cid}: the pinned contract {pin['path']!r} is outside the "
                        f"authored-and-reviewed roots {_AUTHORED_CONTRACT_ROOTS}; a domain document "
                        "under a code or generated path may be produced by the very thing it "
                        "certifies; REFUSED (P7-FIND-02)")
    return problems


def check_authored_contract_content(cid: str, content: bytes, source: str = "<contract>") -> list:
    """Content half of the pin, called by the resolver at READ time. A path check alone is not
    provenance: the reviewed file can be rewritten in place to mirror the collection."""
    pin = _AUTHORED_CONTRACT_PINS.get(cid)
    if pin is None:
        return [f"{source}: {cid}: no authored-contract pin; content cannot be adjudicated; "
                "REFUSED (P7-FIND-02)"]
    digest = hashlib.sha256(content).hexdigest()
    if digest != pin["sha256"]:
        return [f"{source}: {cid}: the domain contract {pin['path']} has content sha256 {digest} "
                f"but the reviewed pin is {pin['sha256']}; the document behind a completeness "
                "claim changed without review; REFUSED (P7-FIND-02)"]
    return []


# --- FIND-03: `args` steers the producer ----------------------------------------------------
# `args` never authors an observation, so the type barrier cannot see it; it CHOOSES which
# observation the producer makes. `boundary_policy(*args)` with a narrowing argument emits a
# smaller policy, and the collection is then certified complete against the narrowed emission. The
# P7 obligation is twofold: refuse free-text steering (a reviewed exact pin), and DECLARE the args
# to P6 as a MUST_DEPEND channel, so a perturbation of args must change the observation — steering
# the producer ignores is dead steering, and steering that moves the observation is a caller-chosen
# observation. The perturbation itself is P6/A5's instrument; P7 supplies the declaration and
# refuses to run unbound.
_STEERING_FIELDS = ("args",)
_STEERING_ARGS_PINS: dict = {}
STEERING_ARGS_PINS = types.MappingProxyType(_STEERING_ARGS_PINS)


def register_steering_args_pin(cid: str, args: list, *, rationale: str) -> None:
    """No-override. The pinned value is compared by EQUALITY, so a re-ordering or a narrowing of an
    argument list is a diff a reviewer sees rather than a silent change of subject."""
    if not rationale:
        raise ContractPinError(f"{cid}: a steering-args pin requires a rationale: what these "
                               "arguments make the producer emit, and why that is the whole domain")
    existing = _STEERING_ARGS_PINS.get(cid)
    entry = {"args": list(args), "rationale": rationale}
    if existing is not None and existing != entry:
        raise ContractPinError(f"{cid}: steering args are already pinned to {existing['args']!r}; "
                               "re-steering a live producer is REFUSED")
    _STEERING_ARGS_PINS[cid] = entry


def check_steering_args_pin(cid: str, spec: dict, source: str = "<spec>") -> list:
    args = spec.get("args")
    if args in (None, [], ()):
        return []
    pin = _STEERING_ARGS_PINS.get(cid)
    if pin is None:
        return [f"{source}: {cid}: spec.args={args!r} steers what the producer emits — it selects "
                "the observation without authoring one — and no reviewed steering pin exists. A "
                "narrowing argument makes the producer emit a smaller domain and the collection is "
                "then certified complete against it. REFUSED (P7-FIND-03)"]
    if list(args) != pin["args"]:
        return [f"{source}: {cid}: spec.args={list(args)!r} differs from the reviewed steering pin "
                f"{pin['args']!r}; changing the producer's arguments changes the observation; "
                "REFUSED (P7-FIND-03)"]
    return []


def declare_steering_channels(spec: dict, cid: str, problems: list) -> dict:
    """Hand `args` to P6 as a MUST_DEPEND channel and refuse while P6 is unbound.

    Same fail-closed posture as _p6_check: an unverified steering input is REFUSED, never waved
    through, so P7 cannot ship ahead of P6 and quietly leave the steering lever unexamined."""
    resolved = dict(spec)
    args = spec.get("args")
    if args in (None, [], ()):
        return resolved
    resolved["_p6_channels"] = {"args": "MUST_DEPEND"}
    if globals().get(_P6_HOOK_NAME) is None:
        problems.append(_problem(spec.get("relation", "?"), "STEERING_UNVERIFIED",
                                 f"{cid}: spec.args steers the producer and the P6 hook "
                                 f"{_P6_HOOK_NAME!r} is not installed, so the steering channel was "
                                 "never perturbed; an unverified steering input is REFUSED"))
    return resolved


# ============================================================================================
# PART B3a — P8: THE CENTRAL STEERING-PIN GATE (P8-ESC-01 / ESC-02 / ESC-03)
# ============================================================================================
# THE DEFECT THIS CLOSES. The module already stated the rule, at WITNESS_FIELD_MANIFEST:
#
#     "Declaring a lever is not permitting it: each is adjudicated against a per-spec REVIEWED
#      PIN, and an unpinned one is REFUSED."
#
# The rule was true of ONE path. `check_steering_args_pin` and `check_authored_contract_pin` had
# exactly one call site each — validate_spec_fixture — and nothing on the programmatic path called
# either. So `evaluate()`, which is the entry collection_completeness.check() actually uses, ran a
# producer with a caller-supplied `args=['--only=passrole']`, the producer emitted a SHORTENED
# domain, and a two-member-short collection was certified CLEAN (0 problems). `path`/`pointer` had
# the same asymmetry, and it was structural rather than accidental: the fixture validator's resolver
# allowlist and FRAMEWORK_KINDS are DISJOINT SETS, so a spec of any framework kind is refused BY
# the only validator that consults the pins, and can therefore only ever arrive programmatically —
# on the path where the pins were not read. The one enforcement point was unreachable for exactly
# the specs the framework exists to govern (P8-ESC-03).
#
# THE CLOSURE. `steering_pin_problems` is the SINGLE adjudicator, and the enforcement lives at the
# CHOKEPOINTS every path must cross rather than at any entry point:
#
#   _resolve_observed()      the one place a witness becomes an operand — it already refuses a spec
#                            that skipped the P7 witness-form gate, and now refuses a spec whose
#                            steering is unpinned. evaluate(), a direct verify_provider() and any
#                            future caller all pass through it, so moving the check into evaluate()
#                            (which would leave verify_provider() open) is not what happens here.
#   verify_non_enumerable()  prologue, BEFORE the source loader and before any witness runs.
#   validate_spec_fixture()  the same function, so a fixture value and a programmatically injected
#                            value are adjudicated by IDENTICAL code and produce identical text.
#
# ORTHOGONALITY TO P6 (§11). This gate reads only the PIN REGISTRIES and the spec. It never
# consults the P6 hook, and P6's verdict never reaches it. `declare_steering_channels` continues to
# hand `args` to P6 as a MUST_DEPEND channel and continues to refuse while P6 is unbound; that
# refusal is about whether the channel was PERTURBED. This gate is about whether the VALUE was
# REVIEWED. A witness can be perfectly independent and still be steered by an unreviewed argument,
# so binding P6 must not, and here cannot, suppress the pin check — the prior Stage-2 defect where
# a compensating steering refusal became unreachable the moment the P6 hook was bound.
#
# WHAT IS REFUSED, ON EVERY PATH:
#   * an UNPINNED steering value                    (no reviewed authority bounds it)
#   * a value that DIFFERS from the pin             (narrowing, widening, re-ordering alike)
#   * a REDIRECTED domain document or pointer       (unless the authored pin binds that document)
#   * OMISSION where a pin exists                   (dropping a pinned argument changes the
#                                                    producer's emission exactly as narrowing it
#                                                    does, and omission is the one edit an equality
#                                                    check never sees)
# and what is ALLOWED is the exact frozen value the reviewed pin names — nothing wider.
_STEERING_SOURCE_PROGRAMMATIC = "<programmatic spec>"


def steering_pin_problems(spec: Any, cid: str, source: str = "<spec>") -> list:
    """THE central steering-pin gate. Returns problem STRINGS; [] means every steering input this
    spec carries is the exact value a reviewed pin names.

    Deliberately TOTAL over the governed steering fields and deliberately independent of HOW the
    spec arrived: the same call adjudicates a tracked JSON fixture and a dict handed straight to
    verify_provider()."""
    if not isinstance(spec, dict):
        return []
    problems: list = []

    # --- `args` (P7-FIND-03): steering that narrows what the producer EMITS -------------------
    problems.extend(check_steering_args_pin(cid, spec, source=source))
    args_pin = _STEERING_ARGS_PINS.get(cid)
    if args_pin is not None and spec.get("args") in (None, [], ()):
        problems.append(
            f"{source}: {cid}: a reviewed steering pin binds this producer to "
            f"args={args_pin['args']!r} and the spec supplies none. Dropping a pinned argument "
            "changes what the producer emits exactly as narrowing it does, and an ABSENT field is "
            "the one edit an equality check never sees; REFUSED (P7-FIND-03)")

    # --- `path` / `pointer` (P7-FIND-02): steering that REDIRECTS the domain document ---------
    contract_pin = _AUTHORED_CONTRACT_PINS.get(cid)
    names_document = any(spec.get(field) not in (None, "") for field in ("path", "pointer"))
    if contract_pin is not None and not names_document:
        problems.append(
            f"{source}: {cid}: a reviewed authored-contract pin binds this collection's domain to "
            f"{contract_pin['path']!r} (pointer {contract_pin['pointer']!r}) and the spec names no "
            "document at all; a domain source the spec silently drops is the same substitution as "
            "one it redirects; REFUSED (P7-FIND-02)")
    elif names_document or spec.get("resolver") == "authored_contract":
        problems.extend(check_authored_contract_pin(cid, spec, source=source))
    return problems


# ============================================================================================
# PART B3c — SLICE1-TOTALITY: STEERING TOTALITY OVER AN *OPEN* FIELD SET   (§6-7, FF-06)
# ============================================================================================
# THE DEFECT THIS CLOSES, stated as the class rather than as three field names.
#
# `steering_pin_problems` above is TOTAL over the fields it names — `args`, `path`, `pointer` —
# and that is exactly its limit. The question it answers is "is THIS field's value reviewed?",
# which presumes a closed, hand-maintained list of which fields can steer. FF-06 falsified the
# presumption without inventing anything: `_p2_witness_payload` hands the producer THE SPEC
# ITSELF, so EVERY key of the spec is producer-readable. A producer that reads `domain_class`
# and emits a different document per class narrows the domain exactly as `args` does, is bounded
# by no pin, and — with a properly PROVENANCED P6 channel, so no sibling control speaks — reaches
# a CLEAN verdict on a two-member-short collection through evaluate(). Executed, not argued:
# continuation-tot/gb_repros.py arm FF-06-live.
#
# Adding "domain_class" to `_STEERING_FIELDS` would close ONE instance and leave the class open:
# the next field is `partition_group`, then `source_collection_id`, then whatever BH-B1..B4 add.
# The set of steering-capable inputs is OPEN, so the governance has to be derived, not listed.
#
# THE CLOSURE — GOVERN THE PRODUCER'S ACTUAL READ-SET, NOT A LIST OF FIELD NAMES.
# Steering requires a READ: a spec field only steers if the producer's own code consults it. So
# the obligation is attached to the read, and the read is DERIVED FROM THE PRODUCER'S CODE:
#
#   (a) STATIC (before the producer runs). `_producer_steering_reads` walks the callable's own
#       code objects — recursively, through nested code — and intersects the string constants it
#       carries with THE KEYS OF THIS SPEC. That is what the executed code NAMES, never a
#       declaration about what it reads (the same instrument, and the same reason, as
#       `_p2_ambient_globals`). A key so named is a steering input and must be bounded by a
#       REVIEWED PIN; unpinned -> REFUSED before any witness executes.
#   (b) DYNAMIC (during the shipped call). The payload handed to the producer is a
#       `_SteeringWatchedPayload`, which records every key actually read — including a key
#       computed at run time, which no constant scan can see, and including a BULK view
#       (`.keys()`, `.items()`, `dict(payload)`, `{**payload}`), which is a read of everything.
#       An ungoverned read discovered here DISCARDS the observation instead of certifying it.
#
# WHY THIS IS TOTAL OVER AN OPEN SET. A NEW spec field is governed the moment a producer reads
# it, with no edit to any list: the default for an unpinned read is REFUSAL. An honest producer
# that consults only the authority the framework INJECTS reads nothing but `_witness_inputs` and
# is untouched — which is the distinction the recurring over-refusal trap turns on: the lie is a
# producer whose emission is a function of a caller-authored field, the honest case is a producer
# whose emission is a function of the injected authority alone.
#
# WHAT IS *NOT* CLAIMED. A producer that reaches a caller-authored value through a channel that
# is neither its own payload nor its own code constants — a module global it imports, a file it
# opens — is P6/P2's subject (MUST_DEPEND / ambient-channel enumeration), not this gate's.
_STEERING_FIELD_PINS: dict = {}
STEERING_FIELD_PINS = types.MappingProxyType(_STEERING_FIELD_PINS)

# Keys the FRAMEWORK writes into the payload. Reading one is not steering: `_witness_inputs` IS
# the injected authority (the intended and only sanctioned input), and the rest are framework
# annotations no caller can author (`_witness_vetted`'s value is a module-private sentinel).
_PRODUCER_FRAMEWORK_PAYLOAD_KEYS = frozenset({
    "_witness_inputs", "_p6_channel", "_witness_vetted", "_witness_provenance", "_p6_channels"})

# Fields the CENTRAL gate above already adjudicates against their own reviewed registries. They
# are excluded here so one steering input never produces two refusals with different texts; the
# central gate runs FIRST on every path that reaches a producer, so exclusion is not exemption.
_STEERING_CENTRALLY_ADJUDICATED = frozenset(_STEERING_FIELDS) | frozenset({"path", "pointer"})


def _register_steering_field_pin(cid: str, field: str, value: Any, *, rationale: str) -> None:
    """No-override. Bind ONE producer-read spec field of ONE collection to ONE reviewed value.

    Private by name for the same reason `_register_p6_channel_producer` is: P4's route-totality
    invariant derives the module's PUBLIC surface and requires every public callable to be a
    proven route or a declared non-verdict helper, and a registration API is neither.
    """
    if not rationale:
        raise ContractPinError(
            f"{cid}: a steering-field pin for {field!r} requires a rationale: what the producer "
            "emits when it reads this value, and why that emission is the WHOLE domain")
    if field in _STEERING_CENTRALLY_ADJUDICATED:
        raise ContractPinError(
            f"{cid}: {field!r} is adjudicated by steering_pin_problems against its own reviewed "
            "registry; a second authority for the same field is how the two disagree")
    entry = {"value": value, "rationale": rationale}
    existing = _STEERING_FIELD_PINS.get((cid, field))
    if existing is not None and existing != entry:
        raise ContractPinError(
            f"{cid}: steering field {field!r} is already pinned to {existing['value']!r}; "
            "re-steering a live producer is REFUSED")
    _STEERING_FIELD_PINS[(cid, field)] = entry


def _producer_code_constants(fn) -> set:
    """Every string constant the callable's own code objects carry, recursively.

    Derived from the CODE, not from a declaration: this is what the executed body can name."""
    out: set = set()
    seen: set = set()
    stack = [getattr(fn, "__code__", None)]
    while stack:
        code = stack.pop()
        if code is None or id(code) in seen:
            continue
        seen.add(id(code))
        for const in code.co_consts:
            if isinstance(const, str):
                out.add(const)
            elif isinstance(const, types.CodeType):
                stack.append(const)
    return out


def _producer_steering_reads(fn, spec: dict) -> set:
    """The keys of THIS spec that the producer's code names. The STEERING_INPUT_UNIVERSE for one
    (producer, spec) pair, computed rather than listed."""
    if not isinstance(spec, dict):
        return set()
    # Written as a comprehension rather than a set difference on purpose: P4's INV-6 flags every
    # `-`/`&` outside a gate-reachable checker, and it is right to — that operator shape IS the
    # parallel-evaluator signature. A steering-read scan has no business looking like one.
    return {key for key in _producer_code_constants(fn)
            if key in spec and key not in _PRODUCER_FRAMEWORK_PAYLOAD_KEYS}


def _steering_read_problems(fn, spec: dict, cid: str, reads: Optional[set] = None,
                            source: str = "<spec>", *, bulk: bool = False) -> list:
    """Adjudicate a producer's steering reads against the reviewed pins. Problem STRINGS."""
    if not isinstance(spec, dict):
        return []
    if reads is None:
        reads = _producer_steering_reads(fn, spec)
    else:
        # The DYNAMIC half hands over the keys the producer actually touched. Only keys that are
        # SPEC fields can steer: a key it probed and the spec does not carry selects nothing, and
        # the framework-written payload keys are the sanctioned input, not a lever.
        reads = {key for key in reads
                 if key in spec and key not in _PRODUCER_FRAMEWORK_PAYLOAD_KEYS}
    who = f"{getattr(fn, '__module__', '?')}.{getattr(fn, '__qualname__', '?')}"
    problems: list = []
    if bulk:
        ungoverned = sorted(key for key in spec
                            if key not in _PRODUCER_FRAMEWORK_PAYLOAD_KEYS
                            and key not in _STEERING_CENTRALLY_ADJUDICATED
                            and (cid, key) not in _STEERING_FIELD_PINS)
        if ungoverned:
            problems.append(
                f"{source}: {cid}: the producer {who!r} took a BULK view of its payload "
                f"(keys/items/values/dict()), so every field of the spec is an input it can "
                f"select on, and {ungoverned!r} carry no reviewed steering pin. A producer that "
                "reads the whole spec is steered by the whole spec; REFUSED (SLICE1-TOT-STEER-02)")
    for field in sorted(reads):
        if field in _STEERING_CENTRALLY_ADJUDICATED:
            continue                       # bounded by steering_pin_problems, which runs first
        pin = _STEERING_FIELD_PINS.get((cid, field))
        if pin is None:
            problems.append(
                f"{source}: {cid}: the producer {who!r} READS spec field {field!r} "
                f"(={spec.get(field)!r}). A field the producer consults SELECTS what it emits — a "
                "narrowing value makes it emit a smaller domain and the collection is then "
                "certified complete against the narrowed emission — and no reviewed steering pin "
                "bounds it. Declaring a lever is not permitting it, and neither is not declaring "
                "one; REFUSED (SLICE1-TOT-STEER-01)")
        elif pin["value"] != spec.get(field):
            problems.append(
                f"{source}: {cid}: the producer {who!r} reads spec field {field!r}="
                f"{spec.get(field)!r}, which differs from the reviewed steering pin "
                f"{pin['value']!r}; changing a value the producer reads changes the observation; "
                "REFUSED (SLICE1-TOT-STEER-01)")
    return problems


class _SteeringWatchedPayload(dict):
    """The mapping the SHIPPED producer call is handed.

    Records every key the producer ACTUALLY reads, and records a BULK view as a read of
    everything. It is a `dict` subclass so nothing downstream changes shape, and it overrides
    `__iter__`, which is what forces `dict(payload)` and `{**payload}` off CPython's fast path
    and through `keys()` — the two bulk reads a constant scan can never see.
    """

    __slots__ = ("_steering_read", "_steering_bulk")

    def __init__(self, data):
        _DICT_INIT(self, data)
        self._steering_read = set()
        self._steering_bulk = False

    def __getitem__(self, key):
        self._steering_read.add(key)
        return super().__getitem__(key)

    def get(self, key, default=None):
        self._steering_read.add(key)
        return _DICT_GET(self, key, default)

    def __contains__(self, key):
        self._steering_read.add(key)
        return _DICT_CONTAINS(self, key)

    def pop(self, key, *default):
        self._steering_read.add(key)
        return super().pop(key, *default)

    def __iter__(self):
        self._steering_bulk = True
        return _DICT_ITER(self)

    def keys(self):
        self._steering_bulk = True
        return super().keys()

    def items(self):
        self._steering_bulk = True
        return super().items()

    def values(self):
        self._steering_bulk = True
        return super().values()


def _steering_input_universe(spec: Optional[dict] = None) -> dict:
    """The executable enumeration behind the claim `ungoverned steering = 0`.

    Returns, for the DECLARED spec vocabulary (P3's closed manifest — a field outside it is
    already UNDECLARED_WITNESS_FIELD and never reaches a producer), the governance state of every
    field a producer could read. `PIN_REQUIRED` is the DEFAULT: a field is governed by being
    pinned or by being refused, never by being absent from a list.
    """
    declared: set = set(_MANIFEST_COMMON["required"]) | set(_MANIFEST_COMMON["optional"])
    declared |= set(_MANIFEST_GUARANTEE_COMMON["required"])
    declared |= set(_MANIFEST_GUARANTEE_COMMON["optional"])
    for forms in WITNESS_FIELD_MANIFEST.values():
        for manifest in forms.values():
            declared |= set(manifest.get("required", {})) | set(manifest.get("optional", {}))
    for nested in _MANIFEST_NESTED.values():
        declared |= set(nested.get("required", {})) | set(nested.get("optional", {}))
    if isinstance(spec, dict):
        declared |= set(spec)
    rows = {}
    for field in sorted(declared):
        if field in _PRODUCER_FRAMEWORK_PAYLOAD_KEYS:
            rows[field] = "FRAMEWORK_WRITTEN"
        elif field in _STEERING_CENTRALLY_ADJUDICATED:
            rows[field] = "PINNED_BY_CENTRAL_GATE"
        else:
            rows[field] = "PIN_REQUIRED_ON_READ"
    return {"fields": rows,
            "ungoverned": sorted(f for f, state in rows.items() if state == "UNGOVERNED")}


# --- FIND-04: `normalize` is a data-authored redefinition of equality -----------------------
# The directive set is CLOSED (an unknown directive already raises), so the reachable transforms
# are enumerable — but enumerable is not examined. Every directive in the set is many-to-one on
# strings, so each one can make two DISTINCT members compare equal. Within one operand that is
# already refused (a duplicate collision is a Problem). ACROSS operands it is the whole point of
# normalize, and it is also the lever: `casefold` turns a genuinely absent "iam:PassRole" into a
# match for a declared "iam:passrole", erasing a MISSING.
#
# THE CLOSURE. The equivalence relation is a property of the DOMAIN, not a choice of whoever wrote
# the spec. On the governed path the directive list is DERIVED from `domain_class` through a pinned
# table; a spec-authored `normalize` that disagrees is REFUSED, and a domain class with no pinned
# equivalence fails closed. (`normalize` is already absent from the fixture allowlist, so no
# tracked fixture can author one at all; this closes the programmatic path.)
_NORMALIZE_REGISTRY: dict = {
    "casefold": {"fn": lambda x: x.casefold() if isinstance(x, str) else x,
                 "collapsing": True,
                 "rationale": "case-insensitive identifiers; identifies members differing only "
                              "by case"},
    "strip": {"fn": lambda x: x.strip() if isinstance(x, str) else x,
              "collapsing": True,
              "rationale": "surrounding whitespace is not part of an identifier"},
    "strip_leading_dot": {"fn": lambda x: x.lstrip(".") if isinstance(x, str) else x,
                          "collapsing": True,
                          "rationale": "extension forms '.py' and 'py' name the same thing"},
    "posix_path": {"fn": lambda x: x.replace("\\", "/") if isinstance(x, str) else x,
                   "collapsing": True,
                   "rationale": "path separator is a platform artefact, not an identity"},
    "str": {"fn": str,
            "collapsing": True,
            "rationale": "cross-type comparison of a stringly-typed domain"},
}

# domain_class -> the ONE equivalence that class is compared under. Authored once, reviewed once,
# and applied to every spec of that class, so no individual spec can pick a looser equality.
_DOMAIN_CLASS_NORMALIZE: dict = {
    "CLOSED_ENUM": (),
    "MANIFEST_FIELD_DOMAIN": (),
    "POLICY_ACTION_DOMAIN": (),
    "SITE_CLASS_DOMAIN": (),
    "EXTERNAL_AUTHORIZATION_FIELD": (),
}
DOMAIN_CLASS_NORMALIZE = types.MappingProxyType(_DOMAIN_CLASS_NORMALIZE)


def register_domain_class_normalize(domain_class: str, directives) -> None:
    """No-override. Loosening the equality a whole domain class is compared under is a reviewed
    contract change, not configuration."""
    directives = tuple(directives)
    for directive in directives:
        if directive not in _NORMALIZE_REGISTRY:
            raise ContractPinError(f"normalize directive {directive!r} is not registered; an "
                                   "unregistered transform has no reviewed collapse semantics")
    existing = _DOMAIN_CLASS_NORMALIZE.get(domain_class)
    if existing is not None and existing != directives:
        raise ContractPinError(f"domain class {domain_class!r} is already pinned to {existing!r}; "
                               "re-pinning the equivalence a live domain is compared under is "
                               "REFUSED")
    _DOMAIN_CLASS_NORMALIZE[domain_class] = directives


def guard_normalize_derivation(spec: dict, cid: str) -> Optional[dict]:
    """Governed-path gate. Returns a Problem or None.

    Scoped deliberately to the LEVER: a spec that authors no `normalize` is compared under exact
    equality, the strictest available relation, and there is nothing for a caller to loosen. The
    refusal fires exactly when a spec authors an equivalence — the act FIND-04 names."""
    if "normalize" not in spec:
        return None
    domain_class = spec.get("domain_class")
    if domain_class is None:
        return _problem(spec.get("relation", "?"), "NORMALIZE_UNDERIVED",
                        f"{cid}: the spec authors normalize={list(spec['normalize'])!r} but declares "
                        "no domain_class, so there is nothing the equivalence could be derived FROM "
                        "— it is the spec's own choice of what counts as equal; REFUSED "
                        "(P7-FIND-04)")
    if domain_class not in _DOMAIN_CLASS_NORMALIZE:
        return _problem(spec.get("relation", "?"), "DOMAIN_CLASS_UNREGISTERED",
                        f"{cid}: domain class {domain_class!r} has no pinned equivalence; an "
                        "unregistered domain class fails closed rather than defaulting to exact or "
                        "to the spec's own choice (P7-FIND-04)")
    derived = _DOMAIN_CLASS_NORMALIZE[domain_class]
    authored = spec.get("normalize")
    if authored is not None and tuple(authored) != derived:
        return _problem(spec.get("relation", "?"), "NORMALIZE_NOT_DERIVED",
                        f"{cid}: the spec authors normalize={list(authored)!r} but domain class "
                        f"{domain_class!r} is compared under {list(derived)!r}. normalize transforms "
                        "BOTH operands, so it redefines equality: a caller-chosen collapsing "
                        "directive makes a genuinely missing member compare as present. The "
                        "equivalence is a property of the domain, not of the spec; REFUSED "
                        "(P7-FIND-04)")
    return None


def derived_normalize(spec: dict) -> tuple:
    return _DOMAIN_CLASS_NORMALIZE.get(spec.get("domain_class"), ())


# --- REVIEWED PINS (the authored half of FIND-02/FIND-03) -----------------------------------
# One entry per live `authored_contract` spec in tests/fixtures/collection-completeness-specs.json.
# The digest is the reviewed content of the document at the time the pin was authored; a later
# in-place rewrite of that document is a refusal, not a silently different domain. No live spec
# declares `args`, so STEERING_ARGS_PINS is deliberately EMPTY: the first spec to steer a producer
# must arrive with a reviewed pin rather than inherit permission from an existing one.
# WAVE 2 (Gate 4N-I28BH-B0a, §34 wave2): the sole authored_contract pin (for
# production_certification.py::VALIDATED_AUTHORIZATION_FIELDS) was RETIRED when that collection was
# migrated from the legacy `authored_contract` bare-[] resolver to the certificate-backed
# schema_validation path. Its domain is now produced by the reviewed, P9-pinned P6 producer
# `authorization_fields_authority`, which reads the SAME externally authored contract fixture via
# production_certification.authorization_contract() (a hardcoded path the spec cannot redirect) — so
# the redirection the pin guarded is now carried by pinned producer code, and the authored_contract
# spec the pin mirrored no longer exists. _AUTHORED_CONTRACT_PINS is therefore deliberately EMPTY.


# ============================================================================================
# PART C0 — P9: WITNESS CALLABLE IDENTITY / PROVENANCE   (Gate 4N-I28BH-B0w-R, GAP-1)
# ============================================================================================
# THE DEFECT THIS CLOSES. Before P9, `register_provider(name, fn)` and `register_ne_provider`
# bound a BARE NAME to ANY callable. Nothing recorded what that callable WAS. P6 verifies a
# witness's BEHAVIOUR (it must depend on its declared reads and not on the collection under test);
# behaviour is not identity. A behaviourally-sound but UNREVIEWED or SUBSTITUTED witness passed
# every one of P1-P8. That is precisely the Gate 4N-I28AB defect shape (name-only plugin trust),
# and 4N-I28AI's lesson (bind PATH *and* CONTENT), neither of which had ever been applied to
# either witness registry.
#
# THE RULE. Identity is NEVER ACCEPTED, ONLY RECOMPUTED. No registration API takes a digest, and
# any digest appearing in a spec is treated as a forgery attempt, not as evidence. Every check
# below derives its comparands from (a) the live object, (b) the bytes on disk, and (c) an
# AUTHORED manifest literal — three sources, none of which the registering caller supplies.
#
# THE RESTRICTED CALLABLE MODEL (this is a REFUSAL, not a pretence of generality). General Python
# callable identity is not provable: C builtins have no bytecode, partials and callable objects can
# redirect `__call__` at any time, lambdas have no reviewable name, and closures carry mutable
# cells that no digest of the function reaches. P9 therefore accepts EXACTLY ONE shape and REFUSES
# every other one at registration:
#
#     a module-level types.FunctionType, whose __qualname__ contains neither '<lambda>' nor
#     '<locals>', with __closure__ is None and no __wrapped__ attribute, whose __globals__ IS the
#     __dict__ of its own resident module, and which is reachable from that module as its own
#     __qualname__ and is the SAME OBJECT found there.
#
# Anything else -> P9_UNSUPPORTED_WITNESS_SHAPE, fail-closed. Unsupported does not mean allowed.
#
# THE TWO TRUST SCOPES. A repo-pinned digest is only meaningful where an AUTHORED literal declares
# what the digest should be. 4N-I28AI settled the same problem for external binaries the same way:
#   * PINNED  — the provider name appears in WITNESS_PROVIDER_MANIFEST (an authored literal in this
#     module, changed only by a reviewable governed diff). Module, qualname, declared source path
#     and per-qualname code digest must ALL match the independently recomputed values. This is the
#     only scope `evaluate()` — the production entry point — will run.
#   * SESSION — the name is absent from the manifest. Identity is still captured, no-override is
#     still enforced, the disk-vs-resident cross-check still runs, and the validate->execute
#     binding still runs; but nothing repo-reviewed says the witness SHOULD be that code. A
#     SESSION witness is usable only by a spec that explicitly carries
#     witness_trust_scope == "SESSION_UNPINNED_TEST_WITNESS" — a deliberately self-incriminating
#     literal that `evaluate()` refuses outright, so a real collection spec can never reach it.
#
# WHAT BINDS VALIDATION TO EXECUTION. witness_binding(spec) recomputes every named provider's
# identity at CONFIG-VALIDATION time and returns a token. verify_provider / verify_non_enumerable
# recompute again at EXECUTION time and refuse if the token moved. The object invoked is the object
# that final recomputation verified — never a fresh registry lookup — and identity is recomputed
# ONCE MORE AFTER the call, so a swap performed during the call (fn.__code__ is writable) discards
# the result instead of certifying it.
class WitnessIdentityError(RuntimeError):
    """Fail-closed. Every P9 refusal raises this; no P9 path returns 'clean' by default."""


P9_ACCEPTED_SHAPE = (
    "a module-level types.FunctionType (no '<lambda>'/'<locals>' in __qualname__, __closure__ is "
    "None, no __wrapped__), whose __globals__ is its own resident module's __dict__ and which is "
    "reachable from that module as its own __qualname__"
)

# The AUTHORED provenance literal. EMPTY in B0-R by design: this gate lands the framework
# CAPABILITY and registers no provider against a real collection. BH-B1..B4 add one entry per real
# witness, and every such addition is a reviewable diff to this literal. Each entry:
#   "<provider name>": {"registry": "PROVIDERS"|"NE_PROVIDERS", "module": str, "qualname": str,
#                       "relative_path": "scripts/x.py", "code_digest": "<sha256 hex>"}
WITNESS_PROVIDER_MANIFEST: dict[str, dict] = {
    # GOLDEN CONSUMER (Gate 4N-I28BH-B0a, §17). reviewer_retrieval_state.py::STATES.
    # DATA/registration, not TCB logic: the framework's signed properties are pin-content
    # independent. The digest is the sha256 of _p9_code_fingerprint(states_witness.__code__),
    # recomputed at register time from the live object AND a fresh compile of the module on disk;
    # any edit to the witness body invalidates it and forces a re-review.
    "reviewer_states_provider": {
        "registry": "PROVIDERS",
        "module": "completeness_providers",
        "qualname": "states_witness",
        "relative_path": "scripts/completeness_providers.py",
        "code_digest": "90db635bd75d3b3767c79f65ecc8257209d372f4ea9e13091898da1462f0f219",
    },
    # WAVE 2 CONSUMERS (Gate 4N-I28BH-B0a, §34 wave2). Same DATA/registration status as the golden
    # entry; each digest is the sha256 of _p9_code_fingerprint(<witness>.__code__), recomputed at
    # register time from the live object AND a fresh compile of the module on disk.
    "production_states_provider": {
        "registry": "PROVIDERS",
        "module": "completeness_providers",
        "qualname": "production_states_witness",
        "relative_path": "scripts/completeness_providers.py",
        "code_digest": "228474d17eaa4eddcc8e05625b07820766e9ee0ddbfa06672b7900395a1263aa",
    },
    "authorization_fields_provider": {
        "registry": "PROVIDERS",
        "module": "completeness_providers",
        "qualname": "authorization_fields_witness",
        "relative_path": "scripts/completeness_providers.py",
        "code_digest": "206dd59a2a6aef7af04c62c95ec9f3b49bd1fbce2be7af89f302e14d84276a56",
    },
    "never_relaunch_provider": {
        "registry": "PROVIDERS",
        "module": "completeness_providers",
        "qualname": "never_relaunch_witness",
        "relative_path": "scripts/completeness_providers.py",
        "code_digest": "e4d67cdb4cd154d09d0cb24d7b592aabf634316e8eebce6f2af83de46b2525d5",
    },
    # WAVE 3 CONSUMERS (Gate 4N-I28BH-B0a, §34 wave3). Same DATA/registration status; each digest is
    # the sha256 of _p9_code_fingerprint(<witness>.__code__), recomputed at register time from the
    # live object AND a fresh compile of the module on disk.
    "date_operators_provider": {
        "registry": "PROVIDERS",
        "module": "completeness_providers",
        "qualname": "date_operators_witness",
        "relative_path": "scripts/completeness_providers.py",
        "code_digest": "4969d84a34796d626c5a1aff4cf0d35c991206f0d31089a19fa66c46dde8872d",
    },
    "reader_role_provider": {
        "registry": "PROVIDERS",
        "module": "completeness_providers",
        "qualname": "reader_role_witness",
        "relative_path": "scripts/completeness_providers.py",
        "code_digest": "532c399f055373fc54b28bcd08a87a7aca35c2552d2924f24f7b944284d17f0e",
    },
    "assurance_roles_provider": {
        "registry": "PROVIDERS",
        "module": "completeness_providers",
        "qualname": "assurance_roles_witness",
        "relative_path": "scripts/completeness_providers.py",
        "code_digest": "0abd08f4d73abc167fcb2da5ef19cd874cad24599c7bbc2474e5dd4364343bdb",
    },
    # WAVE 4 CONSUMERS (Gate 4N-I28BH-B0a, §34 wave4). Same DATA/registration status; each digest is
    # the sha256 of _p9_code_fingerprint(<witness>.__code__), recomputed at register time from the
    # live object AND a fresh compile of the module on disk.
    "review_packet_fields_provider": {
        "registry": "PROVIDERS",
        "module": "completeness_providers",
        "qualname": "review_packet_fields_witness",
        "relative_path": "scripts/completeness_providers.py",
        "code_digest": "e410a15a9795f3c8d20294bfa1b0177c9811f04e6704098ce805520ab5adc5f7",
    },
    "generated_arn_keys_provider": {
        "registry": "PROVIDERS",
        "module": "completeness_providers",
        "qualname": "generated_arn_keys_witness",
        "relative_path": "scripts/completeness_providers.py",
        "code_digest": "b7815b7176cfbbb34ff46c6e5f47902c9bcfcc44d5fc62d49a65dd2c392b7050",
    },
    "provenance_fields_provider": {
        "registry": "PROVIDERS",
        "module": "completeness_providers",
        "qualname": "provenance_fields_witness",
        "relative_path": "scripts/completeness_providers.py",
        "code_digest": "dbf51e27042afa74b65cb01a52c6f121d8e7be1c97112dea693ba6294e7c9d28",
    },
    # WAVE 5 CONSUMERS (Gate 4N-I28BH-B-SLICE3 shard-a, F2 family). Same DATA/registration status;
    # each digest is the sha256 of _p9_code_fingerprint(<witness>.__code__), recomputed at register
    # time from the live object AND a fresh compile of the module on disk. Authority = a pure
    # workflow_assurance record builder's emitted keyset vs the required-field constant under test.
    "workflow_authorization_fields_provider": {
        "registry": "PROVIDERS",
        "module": "completeness_providers",
        "qualname": "workflow_authorization_fields_witness",
        "relative_path": "scripts/completeness_providers.py",
        "code_digest": "ef018212d5a985d0c017063c62b11bd9d71cd1680eb296ac6ca10ddffddf1a68",
    },
    "image_manifest_fields_provider": {
        "registry": "PROVIDERS",
        "module": "completeness_providers",
        "qualname": "image_manifest_fields_witness",
        "relative_path": "scripts/completeness_providers.py",
        "code_digest": "4718f471cd632a3c70ee535232eaecd4da6e40ac1730dfeffed92a742a6d7f60",
    },
    "build_output_fields_provider": {
        "registry": "PROVIDERS",
        "module": "completeness_providers",
        "qualname": "build_output_fields_witness",
        "relative_path": "scripts/completeness_providers.py",
        "code_digest": "689ef2c2f7ba69cb093804d2d4e80ae7e8e99b16cd093550d31dbc6dd8a863ca",
    },
    "pre_push_fields_provider": {
        "registry": "PROVIDERS",
        "module": "completeness_providers",
        "qualname": "pre_push_fields_witness",
        "relative_path": "scripts/completeness_providers.py",
        "code_digest": "b6b39a6d4997bbdf2f5dc6b39b7ebeff4ae8e7ac3449bf6d7b793c1076b2994a",
    },
    # WAVE 6 CONSUMERS (Gate 4N-I28BH-B-SLICE3 shard-b, F3 family — AST self-naming constant
    # vocabulary). Same DATA/registration status; each digest is the sha256 of
    # _p9_code_fingerprint(<witness>.__code__), recomputed at register time from the live object AND
    # a fresh compile of the module on disk. Authority = the per-member self-naming constants
    # AST-extracted from the collection's OWN module source, vs the aggregate tuple/frozenset.
    "startup_dispositions_provider": {
        "registry": "PROVIDERS",
        "module": "completeness_providers",
        "qualname": "startup_dispositions_witness",
        "relative_path": "scripts/completeness_providers.py",
        "code_digest": "5304ec0bf15bfd96125d16e254fb023a3fa6fe3664d38b1501bd1cb8b49abeb9",
    },
    "cache_classifications_provider": {
        "registry": "PROVIDERS",
        "module": "completeness_providers",
        "qualname": "cache_classifications_witness",
        "relative_path": "scripts/completeness_providers.py",
        "code_digest": "51bf441ca8e9d8a86631cbef6b51ad99b621805bcb09047055414afc105fb21c",
    },
    "external_trust_classifications_provider": {
        "registry": "PROVIDERS",
        "module": "completeness_providers",
        "qualname": "external_trust_classifications_witness",
        "relative_path": "scripts/completeness_providers.py",
        "code_digest": "f5956211e68ca8612e878e60d68bf77145683d8da0f444efad482265601c6d51",
    },
    "leak_decisions_provider": {
        "registry": "PROVIDERS",
        "module": "completeness_providers",
        "qualname": "leak_decisions_witness",
        "relative_path": "scripts/completeness_providers.py",
        "code_digest": "782f72d483df1e38a8290a48666888f45f5bcde14ffff59b879bd1aab7c3034e",
    },
    # WAVE 6B CONSUMERS (Gate 4N-I28BH-B-SLICE3 shard-b). Same DATA/registration status; each
    # digest is the sha256 of _p9_code_fingerprint(<witness>.__code__). F5-QUALIFIED
    # (allowed_accounts ← approved-account registry fixture) + CROSS-MODULE (reviewed_tag_keys ←
    # trust_policies trust manifest; service_principals ← trust_validator.ROLE_PURPOSE).
    "allowed_accounts_provider": {
        "registry": "PROVIDERS",
        "module": "completeness_providers",
        "qualname": "allowed_accounts_witness",
        "relative_path": "scripts/completeness_providers.py",
        "code_digest": "c4ac8e675f3008410924aa0478c20a892e6b6d78e681aaf6798a43c165857e87",
    },
    "reviewed_tag_keys_provider": {
        "registry": "PROVIDERS",
        "module": "completeness_providers",
        "qualname": "reviewed_tag_keys_witness",
        "relative_path": "scripts/completeness_providers.py",
        "code_digest": "d812a82a0dda837779683e684048317760959aa620d570351d2eaf5098d94c68",
    },
    "service_principals_provider": {
        "registry": "PROVIDERS",
        "module": "completeness_providers",
        "qualname": "service_principals_witness",
        "relative_path": "scripts/completeness_providers.py",
        "code_digest": "08e532834cfe94aea2cce402c3ca1b116d1761592838be6b9fe59069601959fa",
    },
    # WAVE 7 CONSUMERS (Gate 4N-I28BH-B-SLICE3 shard-b, ceiling sweep). Same DATA/registration
    # status; each digest is the sha256 of _p9_code_fingerprint(<witness>.__code__). F3-name-prefix
    # (site_decisions), CROSS-MODULE (assurance_modes), F5 (docker_steering_categories, read_back_actions).
    "site_decisions_provider": {
        "registry": "PROVIDERS",
        "module": "completeness_providers",
        "qualname": "site_decisions_witness",
        "relative_path": "scripts/completeness_providers.py",
        "code_digest": "16cc0242a47e66851993350c479b5311a405ad5a250a5fa9fc7774189d5eca3f",
    },
    "assurance_modes_provider": {
        "registry": "PROVIDERS",
        "module": "completeness_providers",
        "qualname": "assurance_modes_witness",
        "relative_path": "scripts/completeness_providers.py",
        "code_digest": "5d1de9eba86cb2c991859376c9016b77b3a3e57081468d79718a2ce493f0ab3d",
    },
    "docker_steering_categories_provider": {
        "registry": "PROVIDERS",
        "module": "completeness_providers",
        "qualname": "docker_steering_categories_witness",
        "relative_path": "scripts/completeness_providers.py",
        "code_digest": "e7d78fe7578d472d931552b1dd2cb52dad72313b62b56f33908993b2a1b54bee",
    },
    "read_back_actions_provider": {
        "registry": "PROVIDERS",
        "module": "completeness_providers",
        "qualname": "read_back_actions_witness",
        "relative_path": "scripts/completeness_providers.py",
        "code_digest": "28591497123e2d61c7da0584df915ce50f97f4999b154c33a7c8a7bbabde4aab",
    },
}

_P9_MANIFEST_FIELDS = ("registry", "module", "qualname", "relative_path", "code_digest")


def _p9_digest(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=repr).encode()).hexdigest()


def _p9_manifest_digest(manifest: dict) -> str:
    return _p9_digest({k: {f: v.get(f) for f in _P9_MANIFEST_FIELDS} for k, v in manifest.items()})


# --- code identity ---------------------------------------------------------------------------
# Deliberately the same construction as scripts/executed_code_provenance.py: identity from what a
# code object DOES (co_code, names, varnames, scalar consts, arity, flags) and NOT from where it
# lives (co_filename and co_firstlineno are attacker-settable and say nothing about behaviour).
# The set/frozenset canonicalisation is 4N-I28AG's fix and is required here for the same reason:
# two EQUAL frozenset constants can repr in different orders in the same process, and a control
# whose refusals are intermittent is worse than no control.
def _p9_canonical_const(const):
    if isinstance(const, (set, frozenset)):
        kind = "set" if isinstance(const, set) else "frozenset"
        return f"{kind}({sorted(repr(v) for v in const)})"
    if isinstance(const, tuple):
        return "(" + ", ".join(_p9_canonical_const(v) for v in const) + ")"
    return repr(const)


def _p9_code_fingerprint(code) -> str:
    scalars = tuple(_p9_canonical_const(c) for c in code.co_consts if not hasattr(c, "co_code"))
    return _p9_digest({
        "qualname": code.co_qualname,
        "code": code.co_code.hex(),
        "names": list(code.co_names),
        "varnames": list(code.co_varnames),
        "consts": list(scalars),
        "argcount": code.co_argcount,
        "kwonlyargcount": code.co_kwonlyargcount,
        "flags": code.co_flags,
    })


def _p9_walk_compiled(code, out: dict) -> None:
    for const in code.co_consts:
        if hasattr(const, "co_code"):
            out[const.co_qualname] = _p9_code_fingerprint(const)
            _p9_walk_compiled(const, out)


# Keyed by the sha256 of the bytes ACTUALLY READ, so the cache key IS the content: a cache hit can
# never describe a file that has since changed (the TOCTOU shape 4N-I28AI closed for the registry).
_P9_DISK_CACHE: dict[str, dict] = {}


def _p9_disk_code_identity(path) -> tuple:
    """(source_sha256, {qualname: fingerprint}) for a fresh compile of the bytes on disk."""
    from pathlib import Path as _Path
    raw = _Path(path).read_bytes()
    src_sha = hashlib.sha256(raw).hexdigest()
    cached = _P9_DISK_CACHE.get(src_sha)
    if cached is None:
        # dont_inherit=True is LOAD-BEARING, not tidiness. compile() otherwise inherits the
        # CALLER's __future__ flags, so this module's `from __future__ import annotations` would be
        # stamped into co_flags of every disk fingerprint while the import system compiles the
        # witness's own file with only ITS future statements. The two would then disagree for any
        # witness module lacking that import — a control refusing honest code, the 4N-I28AG class.
        # MEASURED: scripts/executed_code_provenance.disk_code_identity has this defect latent; it
        # has never fired only because every module in tests/fixtures/protected-module-set.json
        # happens to carry the same future import (P9-FIND-01).
        top = compile(raw, str(path), "exec", dont_inherit=True)
        cached = {}
        _p9_walk_compiled(top, cached)
        _P9_DISK_CACHE[src_sha] = cached
    return src_sha, cached


def _p9_repo_root():
    from pathlib import Path as _Path
    here = _Path(__file__).resolve()
    return here.parent.parent if here.parent.name == "scripts" else here.parent


def _p9_defaults_digest(fn) -> str:
    """Defaults are mutable after definition and are load-bearing; the code digest does not cover
    them. __kwdefaults__ is sorted so dict order cannot move the digest."""
    kwd = fn.__kwdefaults__ or {}
    return _p9_digest({
        "defaults": [_p9_canonical_const(d) for d in (fn.__defaults__ or ())],
        "kwdefaults": {k: _p9_canonical_const(kwd[k]) for k in sorted(kwd)},
    })


def _p9_shape_refusal(fn) -> Optional[str]:
    """Return the reason this callable is outside the accepted model, or None."""
    import types as _types
    if not isinstance(fn, _types.FunctionType):
        return (f"witness is a {type(fn).__name__}, not a plain Python function; builtins, bound "
                "methods, functools.partial and callable objects can redirect execution without "
                "changing any digest and are REFUSED")
    if getattr(fn, "__wrapped__", None) is not None:
        return ("witness carries __wrapped__ — it is a wrapper around another callable; the "
                "wrapper's identity says nothing about what actually runs; REFUSED")
    qualname = fn.__qualname__
    if "<lambda>" in qualname:
        return "witness is a lambda; it has no reviewable qualified name; REFUSED"
    if "<locals>" in qualname:
        return (f"witness {qualname!r} is defined inside another scope; it is not reachable for "
                "independent recomputation from its module; REFUSED")
    if fn.__closure__ is not None:
        return ("witness carries closure cells; cell contents are mutable after definition and no "
                "digest of the function reaches them; REFUSED")
    mod = sys.modules.get(fn.__module__)
    if mod is None:
        return (f"witness module {fn.__module__!r} is not resident in sys.modules; its identity "
                "cannot be recomputed independently; REFUSED")
    if fn.__globals__ is not getattr(mod, "__dict__", None):
        return (f"witness __globals__ is not {fn.__module__}.__dict__ — the function was compiled "
                "into a synthetic namespace (exec / dynamic-import substitution); REFUSED")
    if getattr(mod, qualname, None) is not fn:
        return (f"witness is not the object reachable as {fn.__module__}.{qualname} — an alias, a "
                "monkey-patched attribute or an unbound function; the name a reviewer would read "
                "does not resolve to this object; REFUSED")
    return None


class _ProviderRecord:
    """An immutable identity record. Immutability matters: the record is the comparand a later
    recomputation is checked against, so a mutable record would let an attacker move the target."""
    __slots__ = _PROVIDER_RECORD_FIELDS

    def __init__(self, **fields):
        object.__setattr__(self, "_frozen", False)
        for key in _PROVIDER_RECORD_FIELDS:
            if key != "_frozen":
                object.__setattr__(self, key, fields.get(key))
        object.__setattr__(self, "_frozen", True)

    def __setattr__(self, key, value):
        raise WitnessIdentityError(
            f"P9 identity record for {self.name!r} is immutable; attribute {key!r} cannot be "
            "rebound (a mutable record would let an attacker move the comparand)")

    def comparable(self) -> dict:
        return {"name": self.name, "registry": self.registry, "module": self.module,
                "qualname": self.qualname, "code_digest": self.code_digest,
                "defaults_digest": self.defaults_digest, "source_sha256": self.source_sha256,
                "disk_code_digest": self.disk_code_digest, "disk_authority": self.disk_authority,
                "trust_scope": self.trust_scope}


def _p9_recompute(name: str, fn, registry_name: str, manifest: dict) -> _ProviderRecord:
    """Derive a provider's identity from the LIVE object, its module's bytes ON DISK, and the
    AUTHORED manifest. Nothing here is supplied by the caller except the object itself."""
    from pathlib import Path as _Path
    refusal = _p9_shape_refusal(fn)
    if refusal is not None:
        raise WitnessIdentityError(
            f"P9_UNSUPPORTED_WITNESS_SHAPE: provider {name!r}: {refusal} "
            f"(accepted shape: {P9_ACCEPTED_SHAPE})")

    module, qualname = fn.__module__, fn.__qualname__
    resident_digest = _p9_code_fingerprint(fn.__code__)
    entry = manifest.get(name)

    if entry is not None:
        missing = [f for f in _P9_MANIFEST_FIELDS if not entry.get(f)]
        if missing:
            raise WitnessIdentityError(
                f"P9_MANIFEST_MALFORMED: provider {name!r}: manifest entry is missing {missing}; "
                "an incomplete pin is not a pin")
        if entry["registry"] != registry_name:
            raise WitnessIdentityError(
                f"P9_REGISTRY_MISMATCH: provider {name!r} is pinned to registry "
                f"{entry['registry']!r} but was registered into {registry_name!r}")
        if entry["module"] != module or entry["qualname"] != qualname:
            raise WitnessIdentityError(
                f"P9_IDENTITY_MISMATCH: provider {name!r} is pinned to "
                f"{entry['module']}.{entry['qualname']} but the registered callable is "
                f"{module}.{qualname}; the reviewed name is not the executed one")
        path = _Path(entry["relative_path"])
        if not path.is_absolute():
            path = _p9_repo_root() / path
        try:
            source_sha, disk_map = _p9_disk_code_identity(path)
        except OSError as exc:
            raise WitnessIdentityError(
                f"P9_SOURCE_UNREADABLE: provider {name!r}: pinned source {path} cannot be read "
                f"({type(exc).__name__}); a witness with no readable provenance fails closed")
        disk_authority = "MANIFEST_PINNED_PATH"
        trust_scope = "PINNED"
    else:
        source_file = getattr(sys.modules[module], "__file__", None)
        if source_file is None:
            source_sha, disk_map, disk_authority = None, None, "NONE"
        else:
            try:
                source_sha, disk_map = _p9_disk_code_identity(source_file)
                disk_authority = "MODULE_SELF_REPORTED"
            except OSError:
                source_sha, disk_map, disk_authority = None, None, "NONE"
        path, trust_scope = source_file, "SESSION"

    disk_digest = None
    if disk_map is not None:
        disk_digest = disk_map.get(qualname)
        if disk_digest is None:
            raise WitnessIdentityError(
                f"P9_NOT_ON_DISK: provider {name!r}: qualified name {qualname!r} does not exist in "
                f"a fresh compile of {path}; the resident function was created at runtime, not "
                "read from the reviewed source")
        if disk_digest != resident_digest:
            raise WitnessIdentityError(
                f"P9_RESIDENT_DIFFERS_FROM_DISK: provider {name!r}: the code object executing as "
                f"{module}.{qualname} does not match a fresh compile of {path}; the module "
                "attribute has been monkey-patched or the body substituted in memory")
    elif trust_scope == "PINNED":
        raise WitnessIdentityError(
            f"P9_NO_DISK_AUTHORITY: pinned provider {name!r} has no readable source; a pin with "
            "no independent authority is not a pin")

    if entry is not None and entry["code_digest"] != resident_digest:
        raise WitnessIdentityError(
            f"P9_CONTENT_DRIFT: provider {name!r}: the manifest approved code digest "
            f"{entry['code_digest'][:16]}… but {module}.{qualname} now fingerprints "
            f"{resident_digest[:16]}…; the body changed after review")

    record = _ProviderRecord(
        name=name, registry=registry_name, fn=fn, module=module, qualname=qualname,
        code_digest=resident_digest, defaults_digest=_p9_defaults_digest(fn),
        source_path=str(path) if path else None, source_sha256=source_sha,
        disk_code_digest=disk_digest, disk_authority=disk_authority, trust_scope=trust_scope,
        identity_digest=None)
    object.__setattr__(record, "identity_digest", _p9_digest(record.comparable()))
    return record


class WitnessRegistry:
    """A no-override, identity-bound witness registry.

    Bare-name binding is refused at the type level: `registry[name] = fn` raises. The only way in
    is register(), which recomputes identity. Re-registering a name is refused unless the
    recomputed identity is byte-identical (module re-import is legal; substitution is not).

    The manifest digest is captured at construction and re-checked on every resolution, so editing
    WITNESS_PROVIDER_MANIFEST at runtime to bless a substituted witness is itself a refusal.
    """

    def __init__(self, name: str, manifest: Optional[dict] = None):
        self.name = name
        self._manifest = WITNESS_PROVIDER_MANIFEST if manifest is None else manifest
        self._manifest_digest = _p9_manifest_digest(self._manifest)
        self._records: dict[str, _ProviderRecord] = {}

    # -- refusals that keep the old bare-name API from working at all --------------------------
    def __setitem__(self, name, value):
        raise WitnessIdentityError(
            f"P9_BARE_NAME_BINDING: {self.name}[{name!r}] = … is refused; a witness must be "
            "registered through register(), which recomputes and binds its identity")

    def __getattr__(self, name):
        # The legacy bare-name accessor `registry.get(name)` is refused BY NAME. Expressed via
        # __getattr__ (rather than a `def get`) so WitnessRegistry defines NO `get` member: the
        # site-taxonomy resolver then reads every plain-dict `.get` as the dict method it is,
        # instead of a WitnessRegistry-vs-_SteeringWatchedPayload ambiguity. Behaviour identical:
        # `registry.get(...)` still raises the same WitnessIdentityError.
        if name == "get":
            raise WitnessIdentityError(
                "P9_UNVERIFIED_RESOLUTION: {}.get(...) returns a callable with no identity "
                "check; use resolve(), which re-verifies before handing anything back".format(
                    object.__getattribute__(self, "name")))
        raise AttributeError(name)

    def __contains__(self, name):
        return name in self._records

    def __len__(self):
        return len(self._records)

    def names(self):
        return sorted(self._records)

    # -- registration ---------------------------------------------------------------------------
    def register(self, name: str, fn) -> _ProviderRecord:
        if not isinstance(name, str) or not name:
            raise WitnessIdentityError("P9_BAD_NAME: a witness name must be a non-empty string")
        record = _p9_recompute(name, fn, self.name, self._manifest)
        existing = self._records.get(name)
        if existing is not None and existing.identity_digest != record.identity_digest:
            raise WitnessIdentityError(
                f"P9_NO_OVERRIDE: provider {name!r} is already registered to "
                f"{existing.module}.{existing.qualname} ({existing.code_digest[:16]}…); "
                f"re-binding it to {record.module}.{record.qualname} "
                f"({record.code_digest[:16]}…) is refused")
        self._records[name] = record
        return record

    # -- resolution: recompute, compare, never trust what is stored -----------------------------
    def resolve(self, name: str) -> tuple:
        """(fn, identity_digest). Raises WitnessIdentityError on any drift."""
        if _p9_manifest_digest(self._manifest) != self._manifest_digest:
            raise WitnessIdentityError(
                f"P9_MANIFEST_TAMPERED: the provenance manifest backing {self.name} changed after "
                "the registry was constructed; a manifest edited at runtime blesses nothing")
        record = self._records.get(name)
        if record is None:
            raise WitnessIdentityError(
                f"P9_UNREGISTERED: witness provider {name!r} is not registered — a missing witness "
                "fails closed; absence is never 'clean'")
        if not isinstance(record, _ProviderRecord):
            raise WitnessIdentityError(
                f"P9_UNGOVERNED_ENTRY: {self.name}[{name!r}] holds a "
                f"{type(record).__name__}, not a P9 identity record; a callable written straight "
                "into the registry has no bound identity and is refused")
        fresh = _p9_recompute(name, record.fn, self.name, self._manifest)
        if fresh.identity_digest != record.identity_digest:
            raise WitnessIdentityError(
                f"P9_IDENTITY_DRIFT: provider {name!r} no longer matches the identity recorded at "
                f"registration (recorded {record.identity_digest[:16]}…, now "
                f"{fresh.identity_digest[:16]}…); the declared provider is not the executed one")
        return record.fn, fresh.identity_digest

    def trust_scope(self, name: str) -> str:
        record = self._records.get(name)
        return record.trust_scope if isinstance(record, _ProviderRecord) else "UNREGISTERED"

    def invoke(self, name: str, *args, **kwargs):
        """Verify, call the object just verified, then re-verify. The post-call recomputation is
        not ceremony: `fn.__code__` is writable, so a swap landing during the call would otherwise
        produce a result certified against an identity that no longer holds."""
        fn, before = self.resolve(name)
        # P4: the gate is CLOSED for the duration. This is the SHARED execution primitive — P2's
        # independence trials reach a provider through p9_execute_witness without passing through
        # _resolve_observed, so wrapping only the callers left that route open.
        result = _call_witness(fn, *args, **kwargs)
        _fn, after = self.resolve(name)
        if after != before:
            raise WitnessIdentityError(
                f"P9_POST_EXECUTION_DRIFT: provider {name!r} changed identity while it was "
                "running; its result is discarded")
        # SLICE1-TOTALITY (§10-11). invoke() is a PUBLIC method and it is the shared execution
        # primitive, so it is the SIBLING of the EP-2 escape one layer below p9_execute_witness: a
        # caller that skips the gated entry can still obtain an observation here. The refusal is
        # placed AFTER the drift re-verification deliberately — putting it first would preempt
        # P9's post-execution-drift detector and turn the battery arm that proves that detector
        # (A-19, which calls invoke() directly) into a wrong-detector arm, i.e. it would VOID a
        # working control to add this one. Here it takes nothing away: an ungated caller gets no
        # observation, which is the property that matters, and every P9 refusal still fires first.
        if _P9_GATED_EXECUTION_DEPTH == 0:
            raise WitnessIdentityError(
                f"P9_UNGATED_REGISTRY_INVOKE: provider {name!r} was executed through the registry "
                "directly, outside p9_execute_witness, so it crossed no steering gate, no P7 "
                "witness-form vetting and no execution capability. The observation is DISCARDED: "
                "an operand nothing gated cannot become a completeness verdict")
        return result


# P9 CONTRIBUTES NO PERMISSIVE SPEC FIELD. An earlier draft of this property carried a
# `witness_trust_scope` escape hatch so a hermetic test could run an unpinned witness. Agent-8's
# P3/P5 composition constraint killed it, correctly: a field whose PRESENCE authorises running an
# unverified witness is a guard-disabling field, and it does not stop being one because the
# production entry point happens to refuse it. It was also unnecessary — a test constructs its own
# WitnessRegistry with its own manifest and exercises the PINNED path directly, which is what the
# B0w-R battery does. So the field joins the REFUSED class below, and PINNED is now the only scope
# any evaluation path will run, at every entry point rather than only at evaluate().
#
# TWO REFUSED FIELD CLASSES, both checked in the LAYER (verify_provider / verify_non_enumerable),
# not at the entry point, so calling a layer directly cannot dodge them.
#
#   SUPPLIED IDENTITY — an attempt to hand P9 a digest. Identity is recomputed, so a supplied one
#   is either redundant or forged; the framework's own HASH_BACKSTOP lesson (a supplied digest
#   equal to the pin suppressed drift detection) says refuse rather than compare-and-shrug.
# frozenset, not tuple: these are membership-only constants, and a tuple makes `|`/`&` on the
# literals a TypeError for any consumer that composes them (B0wR-A8-FIND-07). Iteration order is
# never load-bearing — every message sorts.
_P9_SUPPLIED_IDENTITY_FIELDS = frozenset({
    "provider_code_sha256", "provider_identity", "provider_digest",
    "expected_provider_code_sha256", "witness_code_digest", "provider_source_sha256"})

#   IDENTITY-DISABLING — any field readable as authorisation to skip, soften or redirect identity
#   verification. There is no legitimate reason for a spec to carry one, so presence alone is the
#   refusal; the VALUE is never consulted (consulting it would make `false` a bypass).
_P9_IDENTITY_DISABLING_FIELDS = frozenset({
    "witness_trust_scope", "trusted_provider", "skip_identity_verification",
    "identity_check_disabled", "alternate_hash_source", "fallback_provider",
    "provider_override", "unverified_provider", "allow_unpinned_witness"})


def p9_guard_supplied_identity(spec: dict, cid: str) -> Optional[str]:
    """Both refused classes. Returns a refusal string or None."""
    disabling = [f for f in _P9_IDENTITY_DISABLING_FIELDS if f in spec]
    if disabling:
        return (f"P9_IDENTITY_DISABLING_FIELD_REFUSED: {cid}: the spec carries {sorted(disabling)}; "
                "a field readable as authorisation to skip or redirect witness identity "
                "verification is refused on PRESENCE — its value is never consulted, because "
                "consulting it would make a falsy value a bypass")
    supplied = [f for f in _P9_SUPPLIED_IDENTITY_FIELDS if f in spec]
    if supplied:
        return (f"P9_SUPPLIED_IDENTITY_REFUSED: {cid}: the spec supplies witness identity via "
                f"{sorted(supplied)}; P9 recomputes identity independently and never accepts a "
                "supplied digest — a supplied digest is either redundant or forged")
    return None


def p9_check_scope(spec: dict, registry: "WitnessRegistry", name: str, cid: str) -> Optional[str]:
    if registry.trust_scope(name) == "PINNED":
        return None
    return (f"P9_UNPINNED_WITNESS: {cid}: provider {name!r} has trust scope "
            f"{registry.trust_scope(name)}; it is absent from WITNESS_PROVIDER_MANIFEST, so no "
            "reviewed authority says this is the right witness. There is no spec field that can "
            "authorise it — pin the provider in the manifest (a reviewable governed diff)")


def _p9_named_providers(spec: dict) -> list:
    """Every (registry, provider-name) pair a spec will actually invoke."""
    out = []
    if isinstance(spec, dict):
        if "observed" not in spec and spec.get("provider"):
            out.append((PROVIDERS, spec["provider"]))
        witness = spec.get("independent_observed_source_or_witness")
        if isinstance(witness, str) and witness:
            out.append((NE_PROVIDERS, witness))
        elif isinstance(witness, dict) and witness.get("provider"):
            out.append((NE_PROVIDERS, witness["provider"]))
    return out


def witness_binding(spec: dict, cid: str = "<collection>") -> dict:
    """CONFIG-VALIDATION-TIME identity capture. Returns {name: identity_digest} plus the refusals
    found. verify_provider / verify_non_enumerable re-derive this at EXECUTION time and refuse if
    it moved — that is the whole point: a witness swapped between `the config validated` and `the
    witness ran` is the substitution P6 cannot see, because the substitute behaves correctly."""
    binding: dict = {"tokens": {}, "problems": []}
    forged = p9_guard_supplied_identity(spec, cid)
    if forged:
        binding["problems"].append(forged)
        return binding
    for registry, name in _p9_named_providers(spec):
        scope_problem = p9_check_scope(spec, registry, name, cid)
        if scope_problem:
            binding["problems"].append(scope_problem)
            continue
        try:
            _fn, token = registry.resolve(name)
        except WitnessIdentityError as exc:
            binding["problems"].append(f"{cid}: {exc}")
            continue
        binding["tokens"][f"{registry.name}:{name}"] = token
    return binding


# ============================================================================================
# PART C0b — SLICE1-TOTALITY: THE WITNESS-EXECUTION CAPABILITY   (§10-11, P8 EP-2 / X-COMPOSITE)
# ============================================================================================
# THE DEFECT THIS CLOSES. The module's own comment on `_resolve_observed` calls it "the ONE place
# a witness becomes an operand", and the P4 route-totality invariant exempts `p9_execute_witness`
# from the entry-point enumeration on the ground that it "cannot certify a collection". A fresh
# P8 sweep falsified both in one arm: `p9_execute_witness` is PUBLIC, it executes a registered
# producer against a caller-supplied payload, and it crosses NO steering gate, NO P7 witness-form
# vetting and NO P2 independence experiment (P8 EP-2). Composed with the equally public
# `compare()`, a caller obtains a STEERED observation and certifies a two-member-short collection
# CLEAN, while evaluate() on the identical spec refuses it (P8 X-COMPOSITE). A route the module
# documents as impossible is a route nothing guards.
#
# THE CLOSURE — A LOAD-BEARING ENTRY EXECUTES THE MANDATORY TOTAL GATE, OR PROVES IT ALREADY RAN.
# `_require_gated_witness_execution` is the single obligation every witness execution now carries:
#
#   * an ACTIVE CAPABILITY minted by the governed chain satisfies it. The capability is a
#     module-private object bound to THE ACTUAL SPEC OBJECT (identity, not equality) and the cid,
#     held only for the dynamic extent of the gated call, and popped on exit — so it is not
#     forgeable from witness input, does not survive substitution of the spec, and is invalid
#     across a new execution. It is the P4 transit ledger's idea applied one layer out.
#   * WITHOUT one, the entry runs the gate ITSELF: the P7 vetting marker (whose value is the
#     module-private `_VETTED` sentinel), the central steering-pin gate, and the producer
#     read-set gate. So a public caller is not refused for being public — it is refused for
#     being UNGATED, and a caller that satisfies the gates is admitted exactly as evaluate() is.
#
# The two limbs answer the two halves of the lead's requirement without a second policy: the
# capability is the cheap proof for the internal chain, the inline gate is the total rule.
_WITNESS_EXECUTION_CAPABILITIES: list = []

# >0 only while p9_execute_witness — the ONE gated execution entry — is driving a registry call.
# WitnessRegistry.invoke() reads it to refuse handing an observation to an ungated caller.
_P9_GATED_EXECUTION_DEPTH = 0


class _witness_execution_capability:
    """Execution-scoped proof that the mandatory gate chain ran for THIS spec object."""

    __slots__ = ("spec", "cid")

    def __init__(self, spec: Any, cid: str):
        self.spec, self.cid = spec, cid

    def __enter__(self) -> "_witness_execution_capability":
        _WITNESS_EXECUTION_CAPABILITIES.append(self)
        return self

    def __exit__(self, *exc) -> bool:
        _WITNESS_EXECUTION_CAPABILITIES.pop()
        return False


def _capability_holds(spec: Any, cid: str) -> bool:
    """Bound to the evaluated OBJECT: `is`, never `==`, so a substituted look-alike spec does not
    inherit the capability its original earned."""
    return any(cap.spec is spec and cap.cid == cid
               for cap in _WITNESS_EXECUTION_CAPABILITIES)


def _require_gated_witness_execution(registry: "WitnessRegistry", name: str, spec: Any,
                                     cid: str) -> None:
    """THE mandatory total gate for a witness execution. Raises WitnessIdentityError."""
    if _capability_holds(spec, cid):
        return
    if not isinstance(spec, dict) or spec.get("_witness_vetted") is not _VETTED:
        raise WitnessIdentityError(
            f"P9_UNGATED_WITNESS_EXECUTION: {cid}: provider {name!r} was asked to execute for a "
            "spec that never passed the P7 witness-form gate and holds no execution capability "
            "from the governed chain. A witness reaching execution by another path is exactly the "
            "parallel-evaluator class P4 closes; REFUSED")
    steering = steering_pin_problems(spec, cid, source=_STEERING_SOURCE_PROGRAMMATIC)
    if steering:
        raise WitnessIdentityError(
            f"P9_UNGATED_WITNESS_EXECUTION: {cid}: provider {name!r} was asked to execute on a "
            f"spec whose steering no reviewed pin bounds: {steering[0]}")
    try:
        fn, _token = registry.resolve(name)
    except WitnessIdentityError:
        raise
    reads = _steering_read_problems(fn, spec, cid, source=_STEERING_SOURCE_PROGRAMMATIC)
    if reads:
        raise WitnessIdentityError(
            f"P9_UNGATED_WITNESS_EXECUTION: {cid}: {reads[0]}")


def p9_execute_witness(registry: "WitnessRegistry", name: str, spec: dict, cid: str,
                       binding: Optional[dict], *args, **kwargs):
    """Execution-time gate: the mandatory total gate (capability or run it here), the
    supplied-identity guard, scope check, binding re-verification, then invoke()
    (verify -> call -> re-verify). Raises WitnessIdentityError; callers translate."""
    _require_gated_witness_execution(registry, name, spec, cid)
    forged = p9_guard_supplied_identity(spec, cid)
    if forged:
        raise WitnessIdentityError(forged)
    scope_problem = p9_check_scope(spec, registry, name, cid)
    if scope_problem:
        raise WitnessIdentityError(scope_problem)
    if binding is not None:
        expected = binding.get("tokens", {}).get(f"{registry.name}:{name}")
        if expected is None:
            raise WitnessIdentityError(
                f"P9_UNBOUND_AT_VALIDATION: {cid}: provider {name!r} is about to run but was not "
                "identity-bound when the config was validated; a witness that appears after "
                "validation is refused")
        _fn, now = registry.resolve(name)
        if now != expected:
            raise WitnessIdentityError(
                f"P9_IDENTITY_REBOUND: {cid}: provider {name!r} was bound as {expected[:16]}… at "
                f"config validation but is {now[:16]}… at execution; the declared provider is not "
                "the executed provider")
    # SLICE1-TOTALITY: mark the ONE gated execution window. WitnessRegistry.invoke() refuses to
    # return an observation while this depth is 0, so the registry stops being a second route.
    global _P9_GATED_EXECUTION_DEPTH
    _P9_GATED_EXECUTION_DEPTH += 1
    try:
        return registry.invoke(name, *args, **kwargs)
    finally:
        _P9_GATED_EXECUTION_DEPTH -= 1


# ============================================================================================
# PART C — PROVIDER-VERIFIER LAYER (provenance / schema / harness / semantic)
# ============================================================================================
# A provider returns the INDEPENDENT observed operand (a set, or a dict key->set) for a collection
# WITHOUT reading the declared constant. The lead binds providers to real repo callables in the
# BH-B sub-gates; tests register synthetic providers. A named-but-unregistered provider is a
# MISSING WITNESS and fails closed. An `observed` value inlined in the spec is accepted for
# hermetic testing but is subject to the same independence guard (its declared `reads` must not be
# the collection id).
# P9: no longer `dict[str, Callable]`. A bare name bound to an arbitrary callable IS the defect.
PROVIDERS = WitnessRegistry("PROVIDERS")


def register_provider(name: str, fn: Callable):
    """Register an observed-operand provider. Identity is RECOMPUTED here from the live object,
    its module's bytes on disk and the authored manifest; no digest is accepted from the caller.
    Refuses an unsupported callable shape, a substituted body, and any re-binding."""
    return PROVIDERS.register(name, fn)


def reset_providers() -> None:
    """Test-isolation affordance, deliberately NOT a trust affordance (P7). Clearing the registry
    can only REMOVE witnesses (turning a spec into a P9_UNREGISTERED refusal), never install or
    rebind one, so it cannot be used to smuggle a witness past P9's no-override rule or past the
    identity recomputation every resolve() performs."""
    PROVIDERS._records.clear()


def _resolve_observed(spec: dict, cid: str, problems: list, binding: Optional[dict] = None):
    """Return (observed, ok). Resolves the independent observed operand from an inline `observed`
    or a registered `provider`. A missing provider or a provider that raises is a REFUSAL. Under
    P9 the provider's identity is re-verified immediately before AND after it is called, and
    checked against the token captured when the config was validated.

    P7 (grafted): the function refuses outright on a spec that did not pass the witness-form gate.
    The marker's value is a module-private object, so no JSON fixture and no caller-authored datum
    can forge it. This runs BEFORE the P9 identity path, and removes nothing from it."""
    if spec.get("_witness_vetted") is not _VETTED:
        # P4/P7 single path: this function is the only place a witness becomes an operand, and it
        # refuses to run on a spec that did not pass the P7 witness-form gate.
        problems.append(_problem(spec.get("relation", "?"), "WITNESS_UNVETTED",
                                 f"{cid}: the observed operand was requested for a spec that did "
                                 "not pass the P7 witness-form gate (resolve_witness_fields); a "
                                 "witness reaching the comparator by another path is REFUSED"))
        return None, False
    # P8 CENTRAL STEERING GATE (P8-ESC-01/02/03). This function is the ONE place a witness becomes
    # an operand, which is why the P7 vetting marker is enforced here rather than at an entry point;
    # the steering pins are enforced in the same place and for the same reason. evaluate(), a direct
    # verify_provider() call, and any future caller all reach the producer through here, so there is
    # no route on which an unpinned narrowing argument or a redirected domain document runs.
    steering = steering_pin_problems(spec, cid, source=_STEERING_SOURCE_PROGRAMMATIC)
    if steering:
        for detail in steering:
            problems.append(_problem(spec.get("relation", "?"), "STEERING_UNPINNED", detail))
        return None, False
    if "observed" in spec:
        return spec["observed"], True
    name = spec.get("provider")
    if not name:
        problems.append(_problem(spec.get("relation", "?"), "NO_WITNESS",
                                 f"{cid}: neither an inline observed operand nor a provider is "
                                 "declared; a completeness claim with no independent witness "
                                 "fails closed"))
        return None, False
    # SLICE1-TOTALITY (§6-7). The STATIC half of the steering read-set gate, BEFORE the producer
    # runs: every key of THIS spec the producer's own code names is a steering input and must be
    # bounded by a reviewed pin. Placed here, at the one place a witness becomes an operand, for
    # the same reason the central steering gate is: every present and future caller crosses it.
    try:
        _fn, _tok = PROVIDERS.resolve(name)
    except WitnessIdentityError as exc:
        problems.append(_problem(spec.get("relation", "?"), "PROVIDER_IDENTITY_REFUSED",
                                 f"{cid}: {exc}"))
        return None, False
    read_steering = _steering_read_problems(_fn, spec, cid,
                                            source=_STEERING_SOURCE_PROGRAMMATIC)
    if read_steering:
        for detail in read_steering:
            problems.append(_problem(spec.get("relation", "?"), "STEERING_READ_UNPINNED", detail))
        return None, False
    try:
        # P2: the payload the witness sees. When its declared authority is a REGISTERED channel the
        # framework INJECTS the authority rather than letting the witness fetch one, so the SHIPPED
        # observation is produced under exactly the conditions the independence trials run under.
        # With no registered channel the payload is the spec itself, unchanged.
        # P4: through _call_witness, so the gate is CLOSED while the provider runs and a
        # provider that tries to ADJUDICATE rather than OBSERVE is refused structurally
        # (WITNESS_INVOKED_CHECKER) instead of being obeyed.
        # SLICE1-TOTALITY: wrapped in the WATCHED payload (the DYNAMIC half of the read-set gate)
        # and executed under an execution-scoped capability bound to this spec object.
        watched = _SteeringWatchedPayload(_p2_witness_payload(spec, cid))
        with _witness_execution_capability(spec, cid):
            observed = _call_witness(p9_execute_witness, PROVIDERS, name, spec, cid, binding,
                                     watched)
        observed_steering = _steering_read_problems(
            _fn, spec, cid, reads=watched._steering_read,
            source=_STEERING_SOURCE_PROGRAMMATIC, bulk=watched._steering_bulk)
        if observed_steering:
            for detail in observed_steering:
                problems.append(_problem(spec.get("relation", "?"), "STEERING_READ_UNPINNED",
                                         detail))
            return None, False
        return observed, True
    except WitnessIdentityError as exc:            # P9 — identity, not behaviour
        problems.append(_problem(spec.get("relation", "?"), "PROVIDER_IDENTITY_REFUSED",
                                 f"{cid}: {exc}"))
        return None, False
    except Exception as exc:                       # a witness that crashes is not clean
        problems.append(_problem(spec.get("relation", "?"), "WITNESS_RAISED",
                                 f"{cid}: observed provider {name!r} raised "
                                 f"{type(exc).__name__}: {exc}; not clean"))
        return None, False


# ============================================================================================
# PART D — NON-ENUMERABLE GUARANTEE LAYER
# ============================================================================================
class NonEnumerableError(RuntimeError):
    """Fail-closed. A refused config or an unrunnable/malformed witness raises this."""


GUARANTEE_KINDS = (
    "INDEPENDENT_CONSEQUENCE_RECONCILIATION",   # A
    "INDEPENDENT_SITE_UNIVERSE",                 # B
    "CROSS_SOURCE_REQUIREMENT",                  # C
    "SEMANTIC_MUTATION_WITNESS",                 # D
    "CLOSED_WORLD_UNKNOWN_REFUSAL",              # E
)

_NE_COMPARISONS = ("AUTHORITATIVE_SUPERSET", "REQUIRED_SUBSET", "KEYED_MAPPING",
                   "PROVENANCE_CORRESPONDENCE", "AUTHORITATIVE_MUST_JUSTIFY")

_NE_PRESENCE = ("INVALID_EMPTY", "VALID_EMPTY", "CONDITIONALLY_EMPTY")

_INSUFFICIENT_KINDS = ("non_empty", "NON_EMPTY", "positive_presence", "POSITIVE_PRESENCE",
                       "not_empty", "NOT_EMPTY", "presence")

_ALLOWED_NE_COMPARISONS = {
    "INDEPENDENT_CONSEQUENCE_RECONCILIATION": {"AUTHORITATIVE_SUPERSET"},
    "INDEPENDENT_SITE_UNIVERSE": {"AUTHORITATIVE_SUPERSET"},
    "CROSS_SOURCE_REQUIREMENT": {"REQUIRED_SUBSET", "KEYED_MAPPING"},
    "SEMANTIC_MUTATION_WITNESS": {"AUTHORITATIVE_SUPERSET", "PROVENANCE_CORRESPONDENCE",
                                  "AUTHORITATIVE_MUST_JUSTIFY", "KEYED_MAPPING"},
    "CLOSED_WORLD_UNKNOWN_REFUSAL": {"AUTHORITATIVE_SUPERSET", "KEYED_MAPPING"},
}

_NE_REQUIRED_KEYS = ("source_collection_id", "guarantee_kind", "expected_source",
                     "independent_observed_source_or_witness", "comparison", "positive_presence")

# Non-enumerable witness providers, separate from the comparator PROVIDERS registry above.
NE_PROVIDERS = WitnessRegistry("NE_PROVIDERS")


def register_ne_provider(name: str, fn: Callable):
    """As register_provider, for the non-enumerable witness registry. Same recomputation, same
    no-override rule, same restricted callable model."""
    return NE_PROVIDERS.register(name, fn)


def reset_ne_providers() -> None:
    """See reset_providers: removal-only, never a rebinding affordance."""
    NE_PROVIDERS._records.clear()


def _deep_freeze(obj):
    """Return a recursively READ-ONLY view of a container (dict->MappingProxyType, list/tuple->tuple,
    set/frozenset->frozenset); scalars and unknown types pass through.

    The payload handed to a Part D witness is deep-frozen so the witness can only READ it: a witness
    that tries to mutate a nested field raises (TypeError on a MappingProxyType/frozenset), which the
    invoker turns into a fail-closed refusal.  This closes the runtime-mutation vector structurally
    (a witness incrementing a nested counter to desynchronise a payload-keyed memo) and enforces
    witness purity — a legitimate witness only reads its injected inputs.
    """
    t = type(obj)
    if t is dict:
        return MappingProxyType({k: _deep_freeze(v) for k, v in obj.items()})
    if t is MappingProxyType:
        return obj
    if t in (list, tuple):
        return tuple(_deep_freeze(v) for v in obj)
    if t in (set, frozenset):
        return frozenset(_deep_freeze(v) for v in obj)
    return obj


def _materialize_observation(raw, cid: str):
    """MATERIALIZE a witness observation into ONE immutable canonical value at the trust boundary.

    FIX 9 — round-5 RAW-OBJECT SPLIT.  FIX 8 stored the RAW witness object as `txn.observed` and
    three extractors RE-ITERATED it in different reader frames (coverage `_p2_member_strings`, the
    determinism guard `_p2_canon`, the comparator `_as_normalized_set`, all `isinstance`-typed so
    they accept subclasses).  Two vectors reopened the cert/coverage split:
      * a `list`/`set` SUBCLASS whose `__iter__` returns a SHORT view only when the comparator is on
        the call stack and the covering view to every other reader (coverage/X0 pass, comparator
        under-reports, certificate mints over the short view);
      * a custom MUTABLE member object (stable __str__/__eq__/__hash__ but adaptive __repr__) that
        desynchronised the pin and, being a non-container, passed through `_deep_freeze` unfrozen.

    The moment the observation crosses the trust boundary it is read EXACTLY ONCE here and reduced
    to a plain immutable value that every gate then consumes; a reader-adaptive container or a
    mutable/adaptive member can no longer present two views.  Dispatch is EXACT-TYPE (the CC-FIND-01
    discipline), so a subclass is refused; the member domain is CLOSED to str / int / frozen
    containers of the same, so a custom/mutable member is refused FAIL-CLOSED.  A legitimate witness
    returns a plain set/frozenset/list/tuple/dict of strings and is unaffected.

    Raises NonEnumerableError; callers translate it into a fail-closed refusal (no certificate).
    """
    t = type(raw)
    if t is str or t is int:            # NB: bool is `type(x) is bool`, not int -> refused below
        return raw
    if t is dict:
        # A FRESH plain dict (exact type, so `isinstance(raw, dict)` keeps working for keyed
        # operands) with materialized-immutable keys/values.  It is the framework's own copy with
        # fixed iteration, not the witness's object, so it cannot be reader-adaptive.
        return {_materialize_observation(k, cid): _materialize_observation(v, cid)
                for k, v in raw.items()}
    if t is frozenset or t is set:
        return frozenset(_materialize_observation(x, cid) for x in raw)
    if t is tuple or t is list:
        return tuple(_materialize_observation(x, cid) for x in raw)
    raise NonEnumerableError(
        f"{cid}: P2_NE_OBSERVATION_NOT_MATERIALIZABLE: an observation/member of type {t.__name__} "
        "is outside the closed immutable member domain (str/int/frozen containers of str/int). A "
        "subclass, a mutable or a reader-adaptive value cannot be reduced to ONE canonical "
        "observation every gate reads, so it is REFUSED (fail-closed)")


def _ne_invoker(spec: dict, cid: str, binding: Optional[dict]) -> Callable:
    """Return the ONLY way Part D may reach a witness: a P9-gated invoker. Every call re-verifies
    identity before and after execution and against the config-validation token, and translates a
    P9 refusal into the layer's own fail-closed error.

    FIX 8 — STRUCTURAL SINGLE-OBSERVATION DISCIPLINE (round-4: the FIX-6 payload memo failed open).
    The provider route is immune to the certificate/coverage OBSERVATION SPLIT because it computes
    ONE observation and every gate reads it.  FIX 6 approximated that with a memo keyed on the whole
    payload's canonical form, which was CONTINGENT and defeatable two ways: (a) a witness mutated a
    nested payload field (the shallow-copied `independent_observed_source_or_witness` dict) so every
    canonical key differed and every call missed the memo; (b) a raising-`__repr__` object in a
    `_T_ANY` field made `_p2_canon` raise and the `except: key=None` disabled the memo — fail-open.
    Both reopened the split.  This invoker fixes it STRUCTURALLY:

      * the payload is DEEP-FROZEN before the witness sees it, so a witness cannot mutate any field
        of it (the mutation vector cannot exist);
      * the single-observation pin is keyed on the FRAMEWORK-INJECTED authority alone
        (`_witness_inputs`), which the framework controls and the witness cannot alter — never on the
        witness-mutable outer payload.  Identical injected authority therefore yields ONE observation
        for the whole transaction: the MUST_DEPEND baseline, the certificate-minting guarantee
        verifier and the coverage gate all resolve to that one observation, so no split exists.  A
        member-REMOVED perturbation is different injected content and legitimately runs fresh;
      * canonicalization is FAIL-CLOSED: an input the framework cannot canonicalize (a raising repr
        / a value outside the encoding domain) is REFUSED, never run with the pin silently off.
    """
    name = _witness_ref(spec)["provider"]
    pinned: dict = {}   # canon(_witness_inputs) -> the single observation over that injected content
    # FIX 9 — MATERIALIZE only where a MEMBERSHIP-COMPLETENESS certificate is minted from a
    # representation-faithful set observation: kind B (INDEPENDENT_SITE_UNIVERSE).  Kinds C/D/E
    # produce DERIVED/structured operands (a {subject, grounds} mapping, a mutation delta) that their
    # own verifiers consume, and there is no coverage gate to split against, so materializing them
    # would wrongly reshape a legitimate derived observation.  Kind A is scoped out of completeness.
    _materialize_here = (isinstance(spec, dict)
                         and spec.get("guarantee_kind") == "INDEPENDENT_SITE_UNIVERSE")

    def call(payload):
        source = payload if isinstance(payload, (dict, MappingProxyType)) else None
        keyed = source.get("_witness_inputs") if source is not None else None
        if keyed is None:
            keyed = payload
        # FIX 9 — for kind B, key on the MATERIALIZED injected authority, not repr() of the raw
        # object; a custom/mutable member (adaptive repr) is REFUSED at materialization so it can
        # neither desync the pin nor pass through as an input.  Fail-closed for every kind.
        try:
            key = _p2_canon(_materialize_observation(keyed, cid) if _materialize_here else keyed)
        except NonEnumerableError:
            raise
        except Exception as exc:
            raise NonEnumerableError(
                f"P2_NE_UNCANONICALIZABLE_PAYLOAD: the witness inputs cannot be canonicalized "
                f"({type(exc).__name__}); an input that cannot be pinned to one observation is "
                "REFUSED") from None
        if key in pinned:
            return pinned[key]
        frozen = _deep_freeze(payload)   # the witness may only READ its inputs
        try:
            # P4: the gate is CLOSED for the duration of the witness call. Part D reaches every
            # witness through this ONE invoker, so wrapping it here covers all five guarantee
            # kinds — and the P4 battery asserts no verifier reaches a provider registry directly.
            # SLICE1-TOTALITY: the invoker is built by _verify_non_enumerable_body AFTER the Part D
            # steering prologue and config validation, so it — and nothing else on this route —
            # mints the execution capability, bound to this spec object for the call's extent.
            with _witness_execution_capability(spec, cid):
                result = _call_witness(p9_execute_witness, NE_PROVIDERS, name, spec, cid, binding,
                                       frozen)
        except WitnessIdentityError as exc:
            raise NonEnumerableError(str(exc)) from None
        # FIX 9 (kind B) — MATERIALIZE the observation ONCE at the trust boundary.  The kind-B
        # guarantee verifier, coverage and MUST_DEPEND then read THIS one immutable canonical value;
        # none re-iterates a raw witness object, so a reader-adaptive container or mutable member
        # cannot present two views.  Fail-closed on a non-materializable member.
        if _materialize_here:
            result = _materialize_observation(result, cid)
        pinned[key] = result
        return result

    return call


def _witness_ref(spec: dict) -> dict:
    witness = spec["independent_observed_source_or_witness"]
    return witness if isinstance(witness, dict) else {"provider": witness}


def _ne_as_set(value, cid: str, what: str) -> set:
    if isinstance(value, (set, frozenset, list, tuple)):
        return set(value)
    raise NonEnumerableError(f"{cid}: malformed witness result — {what} provider returned "
                             f"{type(value).__name__}, expected an iterable set")


def _default_source_loader(cid: str) -> set:
    module, name = cid.split("::", 1)
    import collection_completeness as _cc  # NOT `as C`: `C` is the collection-under-test
    return _cc._collection(module, name)   # parameter across the comparator layer; a module-
    #                                        wide `C` binding poisons site_taxonomy's resolver.


def validate_ne_config(spec: dict,
                       dependency_resolver: Optional[Callable[[str], bool]] = None) -> None:
    """Every non-enumerable refusal rule, run BEFORE any comparison so a malformed or circular
    config can never reach the compare path. RAISES NonEnumerableError (fail-closed)."""
    if not isinstance(spec, dict):
        raise NonEnumerableError("spec is not an object")
    for key in _NE_REQUIRED_KEYS:
        if key not in spec:
            raise NonEnumerableError(f"malformed config: required key {key!r} absent")
    for key in ("source_collection_id", "guarantee_kind", "comparison", "positive_presence"):
        if not isinstance(spec[key], str) or not spec[key]:
            raise NonEnumerableError(f"malformed config: {key!r} must be a non-empty string")

    cid = spec["source_collection_id"]
    gk = spec["guarantee_kind"]
    if gk in _INSUFFICIENT_KINDS:
        raise NonEnumerableError(
            "guarantee_kind 'non_empty' (positive presence alone) is INSUFFICIENT — a non-empty "
            "check cannot witness completeness of a non-enumerable authority")
    if gk not in GUARANTEE_KINDS:
        raise NonEnumerableError(f"unknown guarantee_kind {gk!r}; unknown fails closed")
    schema_problems = _reject_unknown_fields("guarantee", gk, spec)
    if schema_problems:                # P3: closed spec schema, non-enumerable layer
        raise NonEnumerableError(
            "; ".join(p["detail"] for p in schema_problems))
    if spec["comparison"] not in _NE_COMPARISONS:
        raise NonEnumerableError(f"unknown comparison {spec['comparison']!r}")
    # .get(gk, set()): gk already passed the GUARANTEE_KINDS check above; using a guarded lookup
    # means a future divergence between GUARANTEE_KINDS and _ALLOWED_NE_COMPARISONS keys fails
    # CLOSED (comparison not in the empty set -> refused) rather than raising KeyError out of the
    # documented never-raise contract and relying on the caller's blanket except (FW-FIND-B).
    if gk not in _NE_GATE_TRANSITS:
        # THE CLASS CLOSURE, not the instance. A kind wired into dispatch but not into the transit
        # table has no adjudicated set-shaped obligation — which is exactly how kind B shipped with
        # no empty-witness guard. However clean its own body looks, it cannot certify anything.
        raise NonEnumerableError(f"guarantee kind {gk!r} declares no witness-evaluation gate "
                                 "transit (_NE_GATE_TRANSITS); a kind with no adjudicated "
                                 "obligation cannot certify completeness and fails closed")
    if spec["comparison"] not in _ALLOWED_NE_COMPARISONS.get(gk, set()):
        raise NonEnumerableError(f"malformed config: comparison {spec['comparison']!r} is not "
                                 f"valid for guarantee {gk!r}")
    if spec["positive_presence"] not in _NE_PRESENCE:
        raise NonEnumerableError(f"unknown positive_presence {spec['positive_presence']!r}")

    expected = spec["expected_source"]
    witness = spec["independent_observed_source_or_witness"]
    if not witness:
        raise NonEnumerableError("independent_observed_source_or_witness is missing — a "
                                 "completeness claim with no independent witness fails closed")
    if not expected:
        raise NonEnumerableError("expected_source is missing")
    # FIX 8 item 4 — `expected_source` and the witness reference are typed _T_ANY, which accepts an
    # arbitrary object.  A value the framework cannot CANONICALIZE (e.g. one whose __repr__ raises)
    # cannot be pinned into a single observation or audited, and used to disable the observation
    # memo fail-open.  Refuse it here, at config validation, fail-closed.
    for _fld in ("expected_source", "independent_observed_source_or_witness"):
        try:
            _p2_canon(spec[_fld])
        except Exception as _exc:
            raise NonEnumerableError(
                f"malformed config: {_fld!r} is not canonicalizable ({type(_exc).__name__}); a "
                "value the framework cannot encode cannot be pinned to a single observation or "
                "audited, and is REFUSED") from None

    observed_ref = witness.get("provider") if isinstance(witness, dict) else witness
    observed_reads = witness.get("reads") if isinstance(witness, dict) else None
    if observed_ref == cid or observed_reads == cid:
        raise NonEnumerableError("copied-oracle refused: the independent witness reads the "
                                 "declared source constant itself")
    if observed_ref == expected or witness == expected:
        raise NonEnumerableError("self-enumeration refused: expected_source and observed witness "
                                 "are the same identifier (an alias, not two authorities)")
    if expected == cid:
        raise NonEnumerableError("self-reference refused: expected_source names the declared "
                                 "constant itself")

    if gk == "SEMANTIC_MUTATION_WITNESS":
        mw = spec.get("mutation_witness")
        # P1 (W20). `expected_observable_mismatch` was named by THIS error string as required and
        # never checked by the predicate: a kind-D spec that simply omitted it ran the mutation
        # experiment with no declared expectation, so the observed delta had nothing to be brought
        # into contact with and the arm passed. The declared-required/actually-optional gap is the
        # activating-field defect in its purest form — the error message was the only enforcement.
        if (not isinstance(mw, dict) or "member" not in mw or "operation" not in mw
                or not _p1_declared(mw, "expected_observable_mismatch")):
            raise NonEnumerableError("SEMANTIC_MUTATION_WITNESS requires mutation_witness="
                                     "{member, operation, expected_observable_mismatch}; a "
                                     "mutation experiment with no declared expected_observable_"
                                     "mismatch has no expectation to compare its observed delta "
                                     "against, so the witness cannot fail; REFUSED")
        if mw["operation"] not in ("remove", "add"):
            raise NonEnumerableError("mutation_witness.operation must be 'remove' or 'add'")

    if gk == "CLOSED_WORLD_UNKNOWN_REFUSAL":
        # P1 (W21). known_control ACTIVATES kind-E's over-refusal negative control. Absent, the
        # control never runs and a refuse-everything stub certifies a closed world: the two landed
        # probes only ask whether an UNKNOWN member is refused and whether the witness
        # discriminates, and a witness that refuses every member it is not told to accept can
        # satisfy both. The negative control is what proves the closed world is closed rather than
        # merely shut, so its activating field is mandatory, not optional.
        if not isinstance(witness, dict) or not _p1_declared(witness, "known_control"):
            raise NonEnumerableError(
                "CLOSED_WORLD_UNKNOWN_REFUSAL requires independent_observed_source_or_witness."
                "known_control — a pinned KNOWN member the witness must ACCEPT. Without it the "
                "over-refusal negative control never runs and a refuse-everything witness "
                "certifies a closed world; REFUSED")

    deps = spec.get("dependencies") or []
    if cid in deps:
        raise NonEnumerableError("dependency cycle: a spec cannot depend on itself")
    if dependency_resolver is not None:
        for dep in deps:
            if not dependency_resolver(dep):
                raise NonEnumerableError(f"unresolved dependency {dep!r}: a reconciliation that "
                                         "reads an unresolved collection fails closed")


# --- P4-RESIDUAL-01: PART D ROUTES THROUGH THE SINGLE GATE ----------------------------------
# WHAT WAS OPEN. _verify_A..E each computed their OWN set differences and carried their OWN
# hand-written empty-witness guard, and enforce_positive_presence() was a SECOND implementation of
# the presence semantics with its own policy vocabulary. Part D never called compare(), so it was a
# parallel evaluation path: the OBJ-3 vacuous-pass class had to be closed once per guarantee kind,
# and it duly was not — kind A got its empty-witness refusal only after an adversary found it, and
# kind B never got one at all (an EMPTY site-universe witness over an empty source measured CLEAN).
#
# HOW IT IS CLOSED. Every guarantee kind now DECLARES the gate transit its set-shaped obligation
# maps onto, and derives operands only — the difference is taken by the gate, once, for all kinds.
# A kind that ships without a declared transit is refused by validate_ne_config(); a kind that
# computes a set difference itself is refused by the P4 AST invariant. The empty-witness guard is
# no longer per-kind: it is the presence gate, and a future kind F gets it by construction.
#
# The probe-shaped halves of kinds D and E (baseline-vs-mutated discrimination, closed-world
# refusal discrimination) are NOT set relations and stay where they are — but they are APPEND-ONLY:
# they can add a refusal, never certify a clean verdict, because the transit ledger requires the
# gate before any entry point may return [].
_NE_GATE_TRANSITS = {
    # guarantee_kind -> (relation, gate position)
    "INDEPENDENT_CONSEQUENCE_RECONCILIATION": ("REQUIRED_SUPERSET", "A.consequences"),
    "INDEPENDENT_SITE_UNIVERSE": ("EXACT", "B.universe"),
    "CROSS_SOURCE_REQUIREMENT": ("REQUIRED_SUPERSET", "C.subject"),
    # D and E have no witness-set operand of their own: their probes return verdicts and booleans,
    # not sets. Their set-shaped obligation is the SOURCE itself, which every kind transits.
    "SEMANTIC_MUTATION_WITNESS": ("POSITIVE_CONTROL_PRESENCE", "source_presence"),
    "CLOSED_WORLD_UNKNOWN_REFUSAL": ("POSITIVE_CONTROL_PRESENCE", "source_presence"),
}

# Presence policy for each gate position. `source_presence` and `C.subject` adjudicate operands the
# DESIGN owns (the declared source), so they honour the design's declared positive_presence. A
# WITNESS operand is never allowed to be empty by declaration: an empty discovery is a broken
# discovery, and letting a spec field excuse it is the OBJ-3 fail-open one level out.
_NE_GATE_PRESENCE = {
    "source_presence": {"operand": "collection", "policy_from_spec": True},
    "A.consequences": {"operand": None, "policy_from_spec": False},
    "B.universe": {"operand": None, "policy_from_spec": False},
    "C.subject": {"operand": None, "policy_from_spec": True},
    # P2's two ADEQUACY positions for kind A. Both adjudicate the reviewed floor against something
    # the framework computed (the witness's own output; the declared source), so the floor is the
    # load-bearing operand and is never allowed to be empty by declaration.
    "A.floor_discovery": {"operand": None, "policy_from_spec": False},
    "A.floor_corroboration": {"operand": None, "policy_from_spec": False},
}

# (position, problem kind) -> the sentence this layer has always emitted. Part D merges into
# collection_completeness's problem list as STRINGS, and the wording is what the banked battery and
# the operator read. Routing changed WHERE the verdict is computed, not WHAT it says. An unmapped
# (position, kind) is not dropped: it falls through to the generic stringifier, so a new refusal
# from the gate SURFACES rather than vanishing.
_NE_MESSAGES = {
    ("source_presence", "EMPTY_OPERAND_REFUSED"):
        "INVALID_EMPTY — the authoritative source is empty; a false-empty source is a finding, "
        "not a pass",
    ("source_presence", "EMPTY_OPERAND_UNJUSTIFIED"):
        "CONDITIONALLY_EMPTY — the authoritative source is empty but the declared empty-condition "
        "is not met (empty_condition_met is not True); an unwitnessed empty source is a finding, "
        "not a pass",
    ("source_presence", "POSITIVE_CONTROL_ABSENT"):
        "required positive-control member {member!r} is absent (the set was silently shortened)",
    ("A.consequences", "EMPTY_OPERAND_REFUSED"):
        "REFUSED — the independent consequence-inventory witness is EMPTY; a broken or empty "
        "discovery cannot witness reconciliation and must not pass vacuously",
    ("A.consequences", "REQUIRED_ABSENT"):
        "an independently discovered executed consequence {member!r} is NOT covered by the source "
        "— a dropped member lets that command vanish (source-short)",
    ("B.universe", "EMPTY_OPERAND_REFUSED"):
        "REFUSED — the independent site-universe witness is EMPTY; a broken or empty discovery "
        "cannot witness the site universe and must not pass vacuously",
    ("B.universe", "MISSING"):
        "discovered usage {member!r} is not in the declared source — the source is short and that "
        "usage vanishes",
    ("B.universe", "UNKNOWN"):
        "declared member {member!r} has no discovered usage — source-long / unverifiable member",
    ("C.subject", "EMPTY_OPERAND_REFUSED"):
        "REFUSED — the declared source is EMPTY, so the cross-source requirement has nothing to "
        "require and cannot pass vacuously",
    ("C.subject", "REQUIRED_ABSENT"):
        "required member {member!r} is NOT present in the subject (emitted/generated) — "
        "requirement unmet",
}


def _ne_message(problem: dict, cid: str, position: str) -> str:
    template = _NE_MESSAGES.get((position, problem.get("kind")))
    if template is None:
        return _stringify(problem, cid)
    return f"{cid}: " + template.format(member=problem.get("member"))


def _ne_gate_problems(spec: dict, position: str, relation: str, expected_domain,
                      collection) -> list:
    """Transit the single gate at `position` and return the RAW Problem dicts.

    Part D normally wants the string form (_ne_gate). A caller that needs the offending MEMBERS —
    because its sentence reports a list rather than one member — uses this instead, so that it can
    keep its wording without keeping its own set difference."""
    cfg = _NE_GATE_PRESENCE.get(position)
    if cfg is None:
        raise NonEnumerableError(f"gate position {position!r} is not declared; an undeclared "
                                 "evaluation position cannot be gated and fails closed")
    presence: dict = {}
    if cfg["operand"] is not None:
        presence["operand"] = cfg["operand"]
    sub: dict = {"presence": presence}
    if cfg["policy_from_spec"]:
        presence["policy"] = spec.get("positive_presence")
        if spec.get("empty_condition_met") is not None:
            presence["empty_condition_met"] = spec.get("empty_condition_met")
        # P5 x P4. At a position whose presence policy the DESIGN owns, the design may also be
        # DISABLING the guard — and the authorization is computed by a registered condition
        # provider the spec NAMES. Routing the verdict into the gate without carrying that naming
        # would make every legitimately-authorized empty source unbacked at the gate: the control
        # would look stronger and would in fact be refusing designs it had already cleared one
        # layer up. The two keys carried are the ones authorize_guard_disable READS; nothing else
        # of the spec crosses, so a parent witness field still cannot satisfy a gate obligation.
        if isinstance(spec.get("condition_providers"), dict):
            sub["condition_providers"] = spec["condition_providers"]
        if isinstance(spec.get("source_collection_id"), str):
            sub["source_collection_id"] = spec["source_collection_id"]
    else:
        presence["policy"] = "INVALID_EMPTY"
    return compare(relation, expected_domain, collection, sub,
                   _path=f"verify_non_enumerable[{position}]",
                   _condition=("EMPTY_AUTHORITATIVE_SOURCE_LEGITIMATE" if cfg["policy_from_spec"]
                               else "EMPTY_LOAD_BEARING_OPERAND_LEGITIMATE"))


def _ne_gate(spec: dict, position: str, expected_domain, collection, problems: list) -> None:
    """THE Part-D adapter onto the single gate. A guarantee kind derives its operands and hands
    them here; it may NOT take the difference itself. Translates the gate's Problem dicts into the
    string form Part D merges in."""
    relation = "POSITIVE_CONTROL_PRESENCE" if position == "source_presence" \
        else _NE_GATE_TRANSITS[spec["guarantee_kind"]][0]
    cid = spec["source_collection_id"]
    for p in _ne_gate_problems(spec, position, relation, expected_domain, collection):
        problems.append(_ne_message(p, cid, position))


def enforce_positive_presence(spec: dict, source: set, problems: list) -> None:
    """Shared positive-presence enforcement: INVALID_EMPTY and pinned negative controls. This is
    the single place both the enumerable path and the non-enumerable path enforce that a source
    the design says can never be empty is not empty, and that pinned control members are present.
    Appends to `problems`; never raises."""
    cid = spec.get("source_collection_id", "<collection>")
    policy = spec.get("positive_presence")
    if policy in ("VALID_EMPTY", "CONDITIONALLY_EMPTY") and not source:
        # P5 / BYP-1 IN PART D. This is the same one-JSON-word disable as the comparator presence
        # gate, in the layer that does NOT call compare() and so never reaches that gate. Before
        # this patch, positive_presence=VALID_EMPTY had no branch here AT ALL: an empty
        # authoritative source fell straight through to CLEAN, silently. Both weakening policies
        # now require a REGISTERED condition provider to COMPUTE that the emptiness is real.
        # (The CONDITIONALLY_EMPTY literal check below still runs first, so nothing previously
        # refused becomes accepted.)
        if policy == "CONDITIONALLY_EMPTY" and spec.get("empty_condition_met") is not True:
            pass                                   # falls through to the literal refusal below
        else:
            authorize_guard_disable(
                "EMPTY_AUTHORITATIVE_SOURCE_LEGITIMATE", spec,
                {"cid": cid, "layer": "non_enumerable",
                 "relation": spec.get("guarantee_kind", "?"), "field": "positive_presence",
                 "declared_state": policy, "operand": "authoritative_source",
                 "operand_members": (), "operand_empty": True, "suppressed": ()},
                problems, relation=spec.get("guarantee_kind", "?"), stringly=True)
    # P4-RESIDUAL-01. What used to be three hand-written blocks here — the INVALID_EMPTY
    # refusal, the CONDITIONALLY_EMPTY-unmet refusal and the pinned-control difference — is ONE
    # gate transit under POSITIVE_CONTROL_PRESENCE, whose load-bearing operand is the SOURCE. The
    # sentences are unchanged (see _NE_MESSAGES); what changed is that the verdict is now computed
    # in the single gate, so a future guarantee kind inherits it instead of needing its own copy.
    # This transit runs for EVERY kind, which is what lets the ledger seal the layer.
    _ne_gate(spec, "source_presence", set(spec.get("required_present") or []), source, problems)


def _verify_A_consequence(spec, source, problems, call):
    cid = spec["source_collection_id"]
    consequences = _ne_as_set(call(spec), cid, "consequence-inventory")
    # B0a-ADV17-01: this reconciliation is ONE-DIRECTIONAL (consequences - source). An EMPTY
    # witness makes that difference empty and certifies CLEAN — a broken or empty discovery passing
    # vacuously, the same OBJ-3 class the comparator presence gate closes (which never runs here,
    # because Part D does not call compare()). Refuse an empty consequence-inventory unless the
    # design explicitly declares the witness may legitimately be empty.
    if not consequences:
        problems.append(f"{cid}: REFUSED — the independent consequence-inventory witness is EMPTY; "
                        "a broken or empty discovery cannot witness reconciliation and must not "
                        "pass vacuously")
        return
    # P2 (2a) — UNDER-DISCOVERY. The reconciliation above is ONE-DIRECTIONAL, so a witness that
    # finds 1 of 5 real consequences yields an empty difference and certifies CLEAN. The emptiness
    # refusal catches only a ZERO-member discovery: NON-EMPTINESS IS NOT ADEQUACY. The adequacy of a
    # discovery is established against a reviewed FLOOR — the members the discovery must have found
    # — which the framework CHECKS against the witness's own output rather than accepting the
    # witness's word that it looked hard enough. A one-directional guarantee cannot self-report a
    # broken discoverer, so the floor is MANDATORY: with no floor there is no adequacy claim the
    # framework can test at all, and an untestable claim fails closed.
    floor_raw = spec.get("witness_floor")
    if not isinstance(floor_raw, (list, tuple, set, frozenset)) or not floor_raw:
        problems.append(
            f"{cid}: REFUSED — P2_DISCOVERY_FLOOR_ABSENT: INDEPENDENT_CONSEQUENCE_RECONCILIATION "
            "declares no `witness_floor`. The reconciliation is one-directional, so a witness that "
            "discovers ONE of the real consequences produces an empty difference and certifies "
            "CLEAN; non-emptiness is not adequacy. A non-empty reviewed floor (the members the "
            "independent discovery MUST have found) is required so the framework can COMPUTE the "
            "discovery's adequacy instead of accepting it")
        return
    floor = {str(m) for m in floor_raw}
    found = {str(m) for m in consequences}
    # P4: the difference is the GATE's (REQUIRED_SUPERSET at A.floor_discovery); only the MEMBERS
    # come back, so P2's sentence — which reports the whole list and two counts — is unchanged.
    # A gate refusal that is NOT the expected per-member finding is surfaced rather than dropped.
    gated = _ne_gate_problems(spec, "A.floor_discovery", "REQUIRED_SUPERSET", floor, found)
    unfound = sorted(str(p["member"]) for p in gated if p.get("kind") == "REQUIRED_ABSENT")
    for p in gated:
        if p.get("kind") != "REQUIRED_ABSENT":
            problems.append(_stringify(p, cid))
    if unfound:
        problems.append(
            f"{cid}: REFUSED — P2_UNDER_DISCOVERY: the independent consequence-inventory witness "
            f"did NOT find reviewed floor member(s) {unfound}; it reported {len(found)} "
            f"consequence(s) where the floor names {len(floor)}. A discovery that misses members "
            "the review says are there is broken, and a broken discovery reconciles vacuously")
    gated_src = _ne_gate_problems(spec, "A.floor_corroboration", "REQUIRED_SUPERSET", floor,
                                  {str(m) for m in source})
    outside = sorted(str(p["member"]) for p in gated_src if p.get("kind") == "REQUIRED_ABSENT")
    for p in gated_src:
        if p.get("kind") != "REQUIRED_ABSENT":
            problems.append(_stringify(p, cid))
    if outside:
        problems.append(
            f"{cid}: REFUSED — P2_FLOOR_UNCORROBORATED: reviewed floor member(s) {outside} are "
            "absent from the authoritative source, so the floor is not corroborated by the very "
            "collection it is meant to hold the discovery against; a floor nothing can confirm is "
            "an assertion, not a control")
    # P4: the reconciliation difference is the GATE's, under REQUIRED_SUPERSET at position
    # A.consequences. The empty-witness refusal above is kept where it is because it must preempt
    # the P2 adequacy checks; the gate carries the identical refusal for every OTHER kind, which
    # is the point — kind B never had one.
    _ne_gate(spec, "A.consequences", consequences, source, problems)


def _verify_B_site_universe(spec, source, problems, call):
    cid = spec["source_collection_id"]
    universe = _ne_as_set(call(spec), cid, "site-universe")
    # P4-RESIDUAL-01, THE ARM. Kind A got a hand-written empty-witness guard when an adversary
    # found it; kind B never did, and an EMPTY site-universe witness over an empty source measured
    # CLEAN. Both directions and the empty-witness refusal are now the GATE's, under EXACT at
    # position B.universe — so the guard is a property of the layer, not of the kind.
    _ne_gate(spec, "B.universe", universe, source, problems)


def _verify_C_cross_source(spec, source, problems, call):
    cid = spec["source_collection_id"]
    result = call(spec)
    if not isinstance(result, dict) or "subject" not in result:
        raise NonEnumerableError(f"{cid}: malformed witness result — CROSS_SOURCE_REQUIREMENT "
                                 "provider must return {subject, grounds}")
    subject = _ne_as_set(result["subject"], cid, "requirement-subject")
    # P2 (2a) — TRIANGULATION DEPTH MUST BE COUNTED, NEVER REPORTED. `grounds` was an INTEGER the
    # subject supplied about ITSELF: the purest form of an adequacy summary. grounds=9999 against
    # min_grounds=100 passed without a single ground ever being identified, because there is no
    # experiment that can check a number. The witness must IDENTIFY its grounds; the framework
    # counts the DISTINCT ones and refuses any that is an alias of the subject, of the collection
    # under test, or of the declared expected source — an authority corroborating itself is one
    # authority, not two, and aliasing is how a count is inflated without adding a ground.
    grounds, ground_problem = _p2_identified_grounds(result.get("grounds"), spec, subject, source,
                                                     cid)
    if ground_problem is not None:
        problems.append(ground_problem)
        return
    # P4: the requirement difference is the GATE's, under REQUIRED_SUPERSET at C.subject.
    _ne_gate(spec, "C.subject", source, subject, problems)
    # P5 (THRESHOLD_RELAX). min_grounds is a caller-authored number, and any value BELOW the
    # framework floor relaxes the triangulation requirement the guarantee exists to enforce
    # (min_grounds=0 disables it outright). Derived as guard-disabling by the 1e relaxation probe.
    # A relaxation must be COMPUTED — "these are all the authorities that exist" is a fact about
    # the repo, not a preference — so a sub-floor value is honoured only under a registered
    # condition provider; otherwise the FLOOR applies.
    min_grounds = spec.get("min_grounds", _MIN_GROUNDS_FLOOR)
    if min_grounds < _MIN_GROUNDS_FLOOR:
        if not authorize_guard_disable(
                "TRIANGULATION_FLOOR_MAY_BE_LOWERED", spec,
                {"cid": cid, "layer": "non_enumerable", "relation": "CROSS_SOURCE_REQUIREMENT",
                 "field": "min_grounds", "declared_state": min_grounds,
                 "operand": "grounds", "operand_members": (), "operand_empty": False,
                 "suppressed": (), "requested": min_grounds, "floor": _MIN_GROUNDS_FLOOR},
                problems, relation="CROSS_SOURCE_REQUIREMENT", stringly=True):
            min_grounds = _MIN_GROUNDS_FLOOR
    if min_grounds > grounds:
        problems.append(f"{cid}: only {grounds} independent ground(s); triangulation requires "
                        f">= {min_grounds}")


def _verify_D_mutation_witness(spec, source, problems, call):
    cid = spec["source_collection_id"]
    probe = call
    mw = spec["mutation_witness"]
    member, op = mw["member"], mw["operation"]
    if op == "remove" and member not in source:
        problems.append(f"{cid}: mutation_witness targets {member!r} but it is not present to "
                        "remove (the set was already shortened)")
        return
    if op == "add" and member in source:
        problems.append(f"{cid}: mutation_witness adds {member!r} but it is already present — the "
                        "add mutation cannot witness anything")
        return
    mutated = set(source)
    mutated.discard(member) if op == "remove" else mutated.add(member)
    baseline_verdict = probe({"source": set(source), "spec": spec})
    mutated_verdict = probe({"source": mutated, "spec": spec})
    if baseline_verdict == mutated_verdict:
        problems.append(f"{cid}: {op} of {member!r} produced NO observable consumer mismatch "
                        f"(expected: {mw.get('expected_observable_mismatch')}) — the completeness "
                        "witness is absent")
    # P2 (2b) — THE SPECIFICITY LEG. The RELEVANT-mutation check above is satisfied by ANY function
    # of the input: `sorted(payload["source"])` differs between the two calls because the two INPUTS
    # differ, so an oracle that never consults a consumer passes it. Discrimination needs BOTH
    # polarities — MUST_DEPEND on the relevant mutation, MUST_BE_INVARIANT under an irrelevant one.
    # The irrelevant mutation is GENERATED HERE, from the collection id and the witness name, and is
    # never read from the spec: a caller who could choose the mutation would choose one their echo
    # happens to ignore, which is BYP-1's shape re-opened one layer up (P2-MUT-06).
    # SLICE1-FI: the irrelevant mutation is SHAPED LIKE THE REAL MEMBERS it sits among. A member
    # spelled like a marker told the oracle which call was the control, and an oracle that can see
    # the control can be invariant for it and an echo everywhere else.
    sentinel = _p2_probe_member(_p2_member_strings(source), _p2_member_strings(source),
                                cid, _witness_ref(spec).get("provider"), "kind-D-irrelevant")
    irrelevant = set(source)
    irrelevant.add(sentinel)
    irrelevant_verdict = probe({"source": irrelevant, "spec": spec})
    if _p2_canon(baseline_verdict) != _p2_canon(irrelevant_verdict):
        problems.append(
            f"{cid}: REFUSED — P2_MUTATION_WITNESS_NOT_INVARIANT: the mutation witness changed its "
            f"verdict under a framework-generated IRRELEVANT mutation (a synthetic member no "
            f"consumer can know about). An oracle whose verdict tracks ANY change to its own "
            f"argument is an INPUT ECHO, not a consumer probe: it satisfies the relevant-mutation "
            f"leg trivially, because the two inputs differ. A real consumer probe is invariant "
            f"here and only the relevant mutation moves it")
    expectation = mw.get("expected_observable_mismatch")
    if isinstance(expectation, str) and expectation and str(member) not in expectation:
        problems.append(
            f"{cid}: REFUSED — P2_EXPECTATION_UNBOUND: mutation_witness.expected_observable_"
            f"mismatch ({expectation!r}) does not name the mutated member {member!r}, so the "
            "declared expectation is not bound to the experiment it describes; an expectation that "
            "would read the same for a different member is not evidence about this one")


def _verify_E_unknown_refusal(spec, source, problems, call):
    cid = spec["source_collection_id"]
    witness = _witness_ref(spec)
    probe = call
    unknown_member = witness.get("unknown_probe", "__UNKNOWN_SENTINEL__")
    if not probe({"member": unknown_member, "source": set(source), "must": "refuse"}):
        problems.append(f"{cid}: an observed unknown member {unknown_member!r} was NOT refused — "
                        "the closed world is not fail-closed")
    # B0a-ADV17-02: the two probes above both treat True as "behaved correctly", so a constant-True
    # (rubber-stamp) witness satisfies both contradictory expectations and passes. Kind D avoids
    # this by requiring baseline != mutated; E had no discrimination check. A genuinely closed
    # world REFUSES the unknown member even when acceptance is requested, so this probe must return
    # False for a discriminating witness; a rubber stamp returns True and is caught.
    if probe({"member": unknown_member, "source": set(source), "must": "accept"}):
        problems.append(f"{cid}: the closed-world witness ACCEPTS the unknown member "
                        f"{unknown_member!r} when acceptance is requested — it does not "
                        "discriminate; a non-discriminating (rubber-stamp) witness cannot certify "
                        "closed-world refusal")
    # P1: the negative control is MANDATORY. validate_ne_config already refuses a kind-E spec that
    # omits known_control, so this branch is the CONSUMPTION-SITE half of the same obligation — a
    # fix that lands on the validator and not on the verifier is the I28AM sibling-layer defect,
    # and a caller reaching this function by another route must not find the control switched off.
    if not _p1_declared(witness, "known_control"):
        problems.append(f"{cid}: REFUSED — kind-E witness declares no known_control, so the "
                        "over-refusal negative control cannot run; a closed-world claim with no "
                        "negative control is unwitnessed")
        return
    control = witness["known_control"]
    if not probe({"member": control, "source": set(source), "must": "accept"}):
        problems.append(f"{cid}: pinned known member {control!r} was refused — the negative "
                        "control fails (over-refusal)")


_NE_DISPATCH = {
    "INDEPENDENT_CONSEQUENCE_RECONCILIATION": _verify_A_consequence,
    "INDEPENDENT_SITE_UNIVERSE": _verify_B_site_universe,
    "CROSS_SOURCE_REQUIREMENT": _verify_C_cross_source,
    "SEMANTIC_MUTATION_WITNESS": _verify_D_mutation_witness,
    "CLOSED_WORLD_UNKNOWN_REFUSAL": _verify_E_unknown_refusal,
}


# ============================================================================================
# PART E — UNIFIED DISPATCH
# ============================================================================================
# Resolver-kind names collection_completeness.check() routes to this framework instead of the
# enumerable EXACT/PARTITION path. NONE of these are registered against a real collection in B0-R
# (framework capability only); the enumerable 13 keep the existing path unchanged.
FRAMEWORK_KINDS = (
    "provenance_derivation",
    "schema_validation",
    "harness_completeness",
    "semantic_reachability",
    "authoritative_source_no_enumerable_oracle",
)

_PROVIDER_KIND_RELATIONS = frozenset({
    "provenance_derivation", "schema_validation", "harness_completeness", "semantic_reachability",
})


def evaluate(spec: dict, collection: Any = None, cid: str = "<collection>") -> list:
    """Framework entry for collection_completeness.check(). Dispatches on spec['resolver'] (or
    spec['framework_kind']) and returns a list of problem STRINGS. Fail-closed: an unknown kind is
    a refusal, never a pass."""
    ev = _evaluation("evaluate", cid)
    with ev:
        kind = spec.get("resolver") or spec.get("framework_kind")
        # P9: identity is captured HERE, at config-validation time, and re-verified inside the
        # execution path — binding the two halves of the validate->execute gap.
        binding = witness_binding(spec, cid) if isinstance(spec, dict) else None
        if kind == "authoritative_source_no_enumerable_oracle":
            return _sealed_strings(ev, verify_non_enumerable(spec, binding=binding))
        if kind in _PROVIDER_KIND_RELATIONS:
            out = verify_provider(spec, collection, cid, binding=binding)
            # AUTHORITY FORWARDING, not minting.  A clean provider verdict is already bound to
            # its certificate; a list comprehension over it would rebuild a BARE `[]` and drop
            # the authority silently, which is the very defect this closes.  The SAME
            # registered view is handed on, and only a non-clean verdict is re-rendered.
            if _is_authoritative_clean(out):
                return _sealed_strings(ev, out)
            return _sealed_strings(ev, [_stringify(p, cid) for p in out])
        return [f"{cid}: unknown framework kind {kind!r}; unknown fails closed"]


def _stringify(problem: dict, cid: str) -> str:
    return f"{cid}: [{problem.get('relation')}:{problem.get('kind')}] {problem.get('detail')}"


_P6_EMBEDDED: dict = {}

# p6_ast_purity.py — banked verbatim; sha256 d171ecf05263f5472c1bd96eb9d8fadb3a43c9241ef35388ac9bdcade246531c
_P6_EMBEDDED['p6_ast_purity'] = ('d171ecf05263f5472c1bd96eb9d8fadb3a43c9241ef35388ac9bdcade246531c', "".join([
    "IyEvdXNyL2Jpbi9lbnYgcHl0aG9uMwoiIiJCSC1CMHctUiBBZ2VudC01IOKAlCBQNi1MSU0tMDIgY2xvc3VyZTogU1RBVElD",
    "IChBU1QgKyBjb2RlLW9iamVjdCkgcHVyaXR5IGZvciB3aXRuZXNzIGNhbGxhYmxlcy4KClRIRSBSRVNJRFVBTCBUSElTIENM",
    "T1NFUwotLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0KVGhlIGJhbmtlZCBCMHcgUDYgaW5zdHJ1bWVudCAocDZfY29tcHV0YXRp",
    "b25hbF9pbmRlcGVuZGVuY2UucHkpIHByb3ZlcywgQlkgRVhFQ1VUSU9OLCB0aGF0IGEKd2l0bmVzcyBkZXBlbmRzIG9uIGV2",
    "ZXJ5IGNoYW5uZWwgaXQgZGVjbGFyZXMgYW5kIG9uIG5laXRoZXIgYXJ0ZWZhY3QgdW5kZXIgdGVzdC4gSXRzIHBvd2VyIGlz",
    "CmJvdW5kZWQgYnkgQ0hBTk5FTCBFTlVNRVJBVElPTjogaXQgY2FuIG9ubHkgcGVydHVyYiBjaGFubmVscyBpdCBjYW4gbmFt",
    "ZSBhbmQgaW5qZWN0LiBBIHdpdG5lc3MKdGhhdCByZWFjaGVzIHRoZSBjb2xsZWN0aW9uLXVuZGVyLXRlc3QgdGhyb3VnaCBh",
    "IGNoYW5uZWwgdGhlIGZyYW1ld29yayBjYW5ub3QgZW51bWVyYXRlIOKAlCBhIG1vZHVsZQpnbG9iYWwsIGEgZmlsZSBvbiBk",
    "aXNrLCBvcy5lbnZpcm9uLCBhIGNsb2NrLCBhIHN1YnByb2Nlc3MsIGEgbWVtbyBjYWNoZSDigJQgaXMgSU5WSVNJQkxFIHRv",
    "CnBlcnR1cmJhdGlvbi4gUGVydHVyYmluZyB0aGUgaW5qZWN0ZWQgY29weSBkb2VzIG5vdCBwZXJ0dXJiIHRoZSBhbWJpZW50",
    "IGNvcHksIHNvIHRoZSBvYnNlcnZhdGlvbiBpcwppbnZhcmlhbnQsIG5vIGNhbmFyeSBpcyBlY2hvZWQsIGFuZCB0aGUgd2l0",
    "bmVzcyBtZWFzdXJlcyBDTEVBTiB3aGlsZSBiZWluZyBhIHNlY29uZCBjb3B5IG9mIHRoZQpsaXN0LiBUaGF0IGlzIFA2LUxJ",
    "TS0wMiwgYW5kIGl0IGlzIGV4ZWN1dGVkIGFzIGFybSBJTlRFUkxPQ0stQiBpbiBwNl9wdXJpdHlfYmF0dGVyeS5weTogYSB3",
    "aXRuZXNzCnRoYXQgUDYgcGFzc2VzIGFuZCB0aGlzIG1vZHVsZSByZWZ1c2VzLgoKVEhFIENMT1NVUkUsIElOIE9ORSBTRU5U",
    "RU5DRQotLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tCiAgICBBIHdpdG5lc3MncyBvYnNlcnZhdGlvbiBtdXN0IGJlIGEg",
    "cHVyZSBmdW5jdGlvbiBvZiB0aGUgZnJhbWV3b3JrLUlOSkVDVEVEIGlucHV0cyBhbmQgYSBmaXhlZAogICAgc2V0IG9mIEdP",
    "VkVSTkVEIGRldGVybWluaXN0aWMgcHJpbWl0aXZlczsgZXZlcnkgb3RoZXIgY2hhbm5lbCBpcyBlaXRoZXIgbWFkZSBTVFJV",
    "Q1RVUkFMTFkKICAgIFVOUkVBQ0hBQkxFIGF0IGV4ZWN1dGlvbiB0aW1lICh0aGUgbmFtZSBkb2VzIG5vdCBleGlzdCBpbiB0",
    "aGUgd2l0bmVzcydzIGdsb2JhbHMpIG9yIGlzCiAgICBSRUZVU0VEIFNUQVRJQ0FMTFkgYmVmb3JlIGV4ZWN1dGlvbiDigJQg",
    "YW5kIGdlbmVyYWwgUHl0aG9uIHB1cml0eSBpcyBOT1QgY2xhaW1lZDogYW55dGhpbmcKICAgIG91dHNpZGUgdGhlIHJlc3Ry",
    "aWN0ZWQgY29udHJhY3QgZmFpbHMgY2xvc2VkLgoKVEhSRUUgTEFZRVJTLCBQTFVTIFRIRSBEWU5BTUlDIE9ORSBUSEFUIFNU",
    "QVlTCi0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLQogIEwxIFNIQVBFICAgICAgICAgIHRo",
    "ZSBjYWxsYWJsZSBpcyBhIHBsYWluIG1vZHVsZS1sZXZlbCBmdW5jdGlvbjogbm8gY2xvc3VyZSBjZWxscywgbm8gZGVmYXVs",
    "dHMsCiAgICAgICAgICAgICAgICAgICAgZXhhY3RseSBvbmUgcGFyYW1ldGVyLCB1bmRlY29yYXRlZCwgd2l0aCByZWNvdmVy",
    "YWJsZSBzb3VyY2UuCiAgTDIgQVNUICAgICAgICAgICAgYSBOT0RFIEFMTE9XTElTVCAobm90IGEgZGVueWxpc3QpIG92ZXIg",
    "dGhlIHBhcnNlZCBib2R5OiBldmVyeSBOYW1lIGxvYWQgbXVzdAogICAgICAgICAgICAgICAgICAgIHJlc29sdmUgdG8gYSBs",
    "b2NhbCwgYSBnb3Zlcm5lZCBwcmltaXRpdmUgb3IgYW4gYWxsb3dsaXN0ZWQgYnVpbHRpbjsgZXZlcnkKICAgICAgICAgICAg",
    "ICAgICAgICBhdHRyaWJ1dGUgbmFtZSBtdXN0IGJlIGluIGEgc21hbGwgYWxsb3dsaXN0IGFuZCBtYXkgbmV2ZXIgYmVnaW4g",
    "d2l0aCBgX2A7CiAgICAgICAgICAgICAgICAgICAgaW1wb3J0cywgZ2xvYmFsL25vbmxvY2FsLCB0cnkvZXhjZXB0LCB3aGls",
    "ZSwgd2l0aCwgYXN5bmMsIHlpZWxkIGFyZSBvdXRzaWRlCiAgICAgICAgICAgICAgICAgICAgdGhlIGNvbnRyYWN0LiBBbnl0",
    "aGluZyB0aGUgYWxsb3dsaXN0IGRvZXMgbm90IG5hbWUgaXMgUkVGVVNFRCDigJQgdGhlIGZhaWx1cmUKICAgICAgICAgICAg",
    "ICAgICAgICBkaXJlY3Rpb24gaXMgcmVmdXNhbCwgc28gYW4gdW5hbnRpY2lwYXRlZCBjb25zdHJ1Y3QgY2Fubm90IHBhc3Mg",
    "Ynkgb21pc3Npb24uCiAgTDMgQ09ERSBPQkpFQ1QgICAgc291cmNlLUlOREVQRU5ERU5UOiBldmVyeSBuYW1lIGluIGNvX25h",
    "bWVzIChnbG9iYWxzIEFORCBhdHRyaWJ1dGUgbmFtZXMsIGFzCiAgICAgICAgICAgICAgICAgICAgdGhlIGNvbXBpbGVyIHJl",
    "Y29yZGVkIHRoZW0pIG11c3QgYmUgZ292ZXJuZWQsIGNvX2ZyZWV2YXJzIG11c3QgYmUgZW1wdHksIGFuZAogICAgICAgICAg",
    "ICAgICAgICAgIHRoZSBsaXZlIGNvZGUgb2JqZWN0IG11c3QgYWdyZWUgd2l0aCBhIHJlY29tcGlsYXRpb24gb2YgaXRzIG93",
    "biBzb3VyY2UuIFRoaXMKICAgICAgICAgICAgICAgICAgICBpcyB3aGF0IGNhdGNoZXMgYSBzd2FwcGVkIGBfX2NvZGVfX2As",
    "IGEgc3RhbGUgLnB5Yywgb3Igc291cmNlIHRoYXQgbGllcy4KICBMNCBSRUJJTkQgICAgICAgICBTVFJVQ1RVUkFMLiBUaGUg",
    "dmFsaWRhdGVkIGNvZGUgb2JqZWN0IGlzIHJlLWJvdW5kIGludG8gYSBnbG9iYWxzIG1hcHBpbmcgdGhhdAogICAgICAgICAg",
    "ICAgICAgICAgIGNvbnRhaW5zIE9OTFkgdGhlIGdvdmVybmVkIHByaW1pdGl2ZXMgYW5kIGFuIGFsbG93bGlzdGVkIF9fYnVp",
    "bHRpbnNfXy4gQQogICAgICAgICAgICAgICAgICAgIG1vZHVsZSBnbG9iYWwsIGFuIGltcG9ydCwgb3BlbigpLCBldmFsKCks",
    "IGdldGF0dHIoKSBhbmQgX19pbXBvcnRfXyBhcmUgdGhlbgogICAgICAgICAgICAgICAgICAgIG5vdCAicmVqZWN0ZWQiIOKA",
    "lCB0aGV5IERPIE5PVCBFWElTVCwgYW5kIHRoZSB3aXRuZXNzIHJhaXNlcyBOYW1lRXJyb3IgLwogICAgICAgICAgICAgICAg",
    "ICAgIEltcG9ydEVycm9yLiBMMiBzdGF0ZXMgdGhlIHJ1bGU7IEw0IHJlbW92ZXMgdGhlIGNhcGFiaWxpdHkuCiAgRFlOQU1J",
    "QyAgICAgICAgICAgVU5DSEFOR0VEIEFORCBTVElMTCBSRVFVSVJFRC4gU3RhdGljIGFjY2VwdGFuY2UgaXMgTk9UIHN1ZmZp",
    "Y2llbnQgYW5kIHRoaXMKICAgICAgICAgICAgICAgICAgICBtb2R1bGUgbmV2ZXIgY2xhaW1zIG90aGVyd2lzZTogYSB3aXRu",
    "ZXNzIGNhbiBiZSBwZXJmZWN0bHkgcHVyZSBhbmQgc3RpbGwgTElFCiAgICAgICAgICAgICAgICAgICAgYWJvdXQgYHJlYWRz",
    "YCAoZXhlY3V0ZWQgYXMgYXJtIElOVEVSTE9DSy1BKS4gUHVyaXR5IG1ha2VzICJyZWFkcyBvbmx5IGl0cwogICAgICAgICAg",
    "ICAgICAgICAgIGluamVjdGVkIGlucHV0cyIgVkVSSUZJQUJMRTsgb25seSB0aGUgUDYgcGVydHVyYmF0aW9uIGluc3RydW1l",
    "bnQgcHJvdmVzIFdISUNICiAgICAgICAgICAgICAgICAgICAgaW5qZWN0ZWQgaW5wdXRzIGl0IGFjdHVhbGx5IGRlcGVuZHMg",
    "b24uIEJvdGggbXVzdCBwYXNzLgoKV0hBVCBJUyAqTk9UKiBDTEFJTUVECi0tLS0tLS0tLS0tLS0tLS0tLS0tLQpHZW5lcmFs",
    "LXB1cnBvc2UgUHl0aG9uIHNhbmRib3hpbmcgaXMgbm90IGFjaGlldmFibGUgYW5kIGlzIG5vdCBhdHRlbXB0ZWQuIEF0dHJp",
    "YnV0ZSB0cmF2ZXJzYWwKY2Fubm90IGJlIHByZXZlbnRlZCBhdCBydW50aW1lIChgeC5fX2NsYXNzX18uX19iYXNlX18uX19z",
    "dWJjbGFzc2VzX18oKWApLCBzbyB0aGUgZHVuZGVyL2F0dHJpYnV0ZQpydWxlIGlzIGEgU1RBVElDIHJlZnVzYWwsIG5vdCBh",
    "IHN0cnVjdHVyYWwgaW1wb3NzaWJpbGl0eSDigJQgc2VlIHRoZSBgY2xvc3VyZV9jbGFzc2AgY29sdW1uIG9mIHRoZQpiYXR0",
    "ZXJ5LCB3aGljaCBzZXBhcmF0ZXMgU1RSVUNUVVJBTF9BTkRfU1RBVElDIGNoYW5uZWxzIGZyb20gU1RBVElDX09OTFkgb25l",
    "cy4gVGhlIGhvbmVzdCBwb3NpdGlvbgppczogdGhpcyBpcyBhIGRlbGliZXJhdGVseSBzbWFsbCB3aXRuZXNzIExBTkdVQUdF",
    "IHdpdGggYSByZWZ1c2FsIGRlZmF1bHQsIG5vdCBhIHNhbmRib3guCgpzdGRsaWIgb25seS4gUmVhZC1vbmx5IHcuci50LiB0",
    "aGUgY2Fub25pY2FsIHJlcG8uCiIiIgpmcm9tIF9fZnV0dXJlX18gaW1wb3J0IGFubm90YXRpb25zCgppbXBvcnQgYXN0Cmlt",
    "cG9ydCBidWlsdGlucyBhcyBfYnVpbHRpbnMKaW1wb3J0IGRpcwppbXBvcnQgaGFzaGxpYgppbXBvcnQgaW5zcGVjdAppbXBv",
    "cnQganNvbgppbXBvcnQgdGV4dHdyYXAKaW1wb3J0IHR5cGVzCmZyb20gdHlwaW5nIGltcG9ydCBBbnksIENhbGxhYmxlLCBP",
    "cHRpb25hbAoKQ09OVFJBQ1RfSUQgPSAiUldDQy0xIgoKCmNsYXNzIFB1cml0eUVycm9yKFJ1bnRpbWVFcnJvcik6CiAgICAi",
    "IiJSYWlzZWQgb25seSBieSByZXN0cmljdCgpL2dvdmVybmVkX2NhbGwoKSB3aGVuIGEgd2l0bmVzcyB0aGF0IGZhaWxlZCB2",
    "YWxpZGF0aW9uIHdvdWxkCiAgICBvdGhlcndpc2UgYmUgZXhlY3V0ZWQuIEFkanVkaWNhdGlvbiBpdHNlbGYgcmV0dXJucyBw",
    "cm9ibGVtcywgbmV2ZXIgcmFpc2VzLiIiIgoKCmRlZiBfcHJvYmxlbShraW5kOiBzdHIsIGNoYW5uZWw6IE9wdGlvbmFsW3N0",
    "cl0sIGRldGFpbDogc3RyLCAqLCBub2RlOiBBbnkgPSBOb25lLAogICAgICAgICAgICAgbGF5ZXI6IHN0ciA9ICJMMiIpIC0+",
    "IGRpY3Q6CiAgICBwID0geyJraW5kIjoga2luZCwgImNoYW5uZWwiOiBjaGFubmVsLCAibGF5ZXIiOiBsYXllciwgImRldGFp",
    "bCI6IGRldGFpbH0KICAgIGlmIG5vZGUgaXMgbm90IE5vbmUgYW5kIGhhc2F0dHIobm9kZSwgImxpbmVubyIpOgogICAgICAg",
    "IHBbImxpbmVubyJdID0gbm9kZS5saW5lbm8KICAgIHJldHVybiBwCgoKIyBLSUxMLVBST09GIHN3aXRjaC4gRW1wdHkgaW4g",
    "bm9ybWFsIG9wZXJhdGlvbi4gRGlzYWJsaW5nIGEgbGF5ZXIgbXVzdCBmbGlwIGEgU1BFQ0lGSUMsIG5vbi1lbXB0eQojIHNl",
    "dCBvZiBhcm1zIGZyb20gUkVGVVNFRCB0byBBQ0NFUFRFRDsgYSBsYXllciB3aG9zZSByZW1vdmFsIGZsaXBzIG5vdGhpbmcg",
    "aXMgdW53aXRuZXNzZWQgYW5kIHRoZQojIGFybXMgdGhhdCBjbGFpbSB0byBjb3ZlciBpdCBhcmUgdm9pZC4KRElTQUJMRURf",
    "TEFZRVJTOiBzZXQgPSBzZXQoKQoKCmRlZiBfdGFnKHByb2JsZW1zOiBsaXN0LCBsYXllcjogc3RyKSAtPiBsaXN0OgogICAg",
    "Zm9yIHAgaW4gcHJvYmxlbXM6CiAgICAgICAgcFsibGF5ZXIiXSA9IGxheWVyCiAgICByZXR1cm4gcHJvYmxlbXMKCgojID09",
    "PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09",
    "PT09PT09PT09PT09PT09PT09PT09PQojIFRIRSBHT1ZFUk5FRCBTVVJGQUNFCiMgPT09PT09PT09PT09PT09PT09PT09PT09",
    "PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09",
    "CiMgQnVpbHRpbnMgYSB3aXRuZXNzIG1heSBuYW1lLiBFdmVyeSBleGNsdXNpb24gaXMgZGVsaWJlcmF0ZToKIyAgIG9wZW4v",
    "ZXZhbC9leGVjL2NvbXBpbGUvX19pbXBvcnRfXy9pbnB1dC9wcmludCAgLS0gY2FwYWJpbGl0eSAoSS9PLCBjb2RlIGxvYWRp",
    "bmcsIGV4ZWN1dGlvbikKIyAgIGdldGF0dHIvc2V0YXR0ci9kZWxhdHRyL2hhc2F0dHIvdmFycy9kaXIgICAgICAgLS0gZHlu",
    "YW1pYyBhdHRyaWJ1dGUgYWNjZXNzIGRlZmVhdHMgdGhlIEwyIHJ1bGUKIyAgIGdsb2JhbHMvbG9jYWxzICAgICAgICAgICAg",
    "ICAgICAgICAgICAgICAgICAgICAgLS0gcmUtb3BlbiB0aGUgYW1iaWVudCBuYW1lc3BhY2UgTDQgY2xvc2VkCiMgICBpZC9o",
    "YXNoL3JlcHIvb2JqZWN0L3R5cGUvc3VwZXIgICAgICAgICAgICAgICAgIC0tIGFkZHJlc3MtIG9yIHNlZWQtZGVwZW5kZW50",
    "LCBvciBhIGNsYXNzLWdyYXBoCiMgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAg",
    "IGVudHJ5IHBvaW50IChoYXNoKCkgb2Ygc3RyIGlzIFBZVEhPTkhBU0hTRUVECiMgICAgICAgICAgICAgICAgICAgICAgICAg",
    "ICAgICAgICAgICAgICAgICAgICAgICAgICAgIHNhbHRlZDogYSBub25kZXRlcm1pbmlzbSBjaGFubmVsKQojICAgaXRlci9u",
    "ZXh0ICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAtLSBzdGF0ZW1lbnQtbGV2ZWwgYGZvcmAgY292ZXJz",
    "IHRoZSBob25lc3QgdXNlCiMgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgIHdp",
    "dGhvdXQgZXhwb3NpbmcgbWFudWFsIGl0ZXJhdG9yIHN0YXRlCkFMTE9XRURfQlVJTFRJTlMgPSBmcm96ZW5zZXQoewogICAg",
    "ImxlbiIsICJzb3J0ZWQiLCAic2V0IiwgImZyb3plbnNldCIsICJkaWN0IiwgImxpc3QiLCAidHVwbGUiLCAic3RyIiwgImlu",
    "dCIsICJmbG9hdCIsICJib29sIiwKICAgICJhbnkiLCAiYWxsIiwgIm1pbiIsICJtYXgiLCAic3VtIiwgImFicyIsICJyb3Vu",
    "ZCIsICJkaXZtb2QiLCAiZW51bWVyYXRlIiwgInppcCIsICJyYW5nZSIsCiAgICAicmV2ZXJzZWQiLCAiaXNpbnN0YW5jZSIs",
    "ICJtYXAiLCAiZmlsdGVyIiwKfSkKCiMgQXR0cmlidXRlIG5hbWVzIGEgd2l0bmVzcyBtYXkgdXNlLiBgZm9ybWF0YC9gZm9y",
    "bWF0X21hcGAgYXJlIEVYQ0xVREVEOiBhIHJ1bnRpbWUgZm9ybWF0IHN0cmluZwojIHBlcmZvcm1zIGF0dHJpYnV0ZSB0cmF2",
    "ZXJzYWwgKCJ7MC5fX2NsYXNzX199Ii5mb3JtYXQoeCkpIGFuZCB3b3VsZCByZW9wZW4gTDIgZnJvbSBpbnNpZGUgYW4KIyBh",
    "bGxvd2xpc3RlZCBjYWxsLiBgX19gLXByZWZpeGVkIG5hbWVzIGFyZSByZWZ1c2VkIHVuY29uZGl0aW9uYWxseSBiZWZvcmUg",
    "dGhpcyBzZXQgaXMgY29uc3VsdGVkLgpBTExPV0VEX0FUVFJJQlVURVMgPSBmcm96ZW5zZXQoewogICAgIml0ZW1zIiwgImtl",
    "eXMiLCAidmFsdWVzIiwgImdldCIsCiAgICAiaXNkaXNqb2ludCIsICJpc3N1YnNldCIsICJpc3N1cGVyc2V0IiwgInVuaW9u",
    "IiwgImludGVyc2VjdGlvbiIsICJkaWZmZXJlbmNlIiwKICAgICJzeW1tZXRyaWNfZGlmZmVyZW5jZSIsICJjb3B5IiwgImFk",
    "ZCIsICJ1cGRhdGUiLCAiZGlzY2FyZCIsICJyZW1vdmUiLAogICAgImFwcGVuZCIsICJleHRlbmQiLCAic29ydCIsICJpbmRl",
    "eCIsICJjb3VudCIsCiAgICAic3RhcnRzd2l0aCIsICJlbmRzd2l0aCIsICJzcGxpdCIsICJyc3BsaXQiLCAic3RyaXAiLCAi",
    "bHN0cmlwIiwgInJzdHJpcCIsICJqb2luIiwKICAgICJsb3dlciIsICJ1cHBlciIsICJyZXBsYWNlIiwgImVuY29kZSIsICJo",
    "ZXhkaWdlc3QiLAp9KQoKIyBOYW1lcyB3aG9zZSBhcHBlYXJhbmNlIGlzIHJlcG9ydGVkIHdpdGggYSBTUEVDSUZJQyBzaWRl",
    "LWNoYW5uZWwga2luZCByYXRoZXIgdGhhbiB0aGUgZ2VuZXJpYwojIE1PRFVMRV9HTE9CQUxfUkVBRCwgc28gdGhlIGJhdHRl",
    "cnkncyBhcm1zIG5hbWUgdGhlIGNsYXNzIHRoZXkgYXJlIGFjdHVhbGx5IGZpcmluZy4KX05BTUVEX0NIQU5ORUwgPSB7CiAg",
    "ICAib3MiOiAiUFJPQ0VTU19TVEFURV9SRUFEIiwgInN5cyI6ICJQUk9DRVNTX1NUQVRFX1JFQUQiLCAicGxhdGZvcm0iOiAi",
    "UFJPQ0VTU19TVEFURV9SRUFEIiwKICAgICJlbnZpcm9uIjogIkVOVklST05NRU5UX1JFQUQiLCAiZ2V0ZW52IjogIkVOVklS",
    "T05NRU5UX1JFQUQiLAogICAgIm9wZW4iOiAiRklMRVNZU1RFTV9SRUFEIiwgImlvIjogIkZJTEVTWVNURU1fUkVBRCIsICJw",
    "YXRobGliIjogIkZJTEVTWVNURU1fUkVBRCIsCiAgICAiUGF0aCI6ICJGSUxFU1lTVEVNX1JFQUQiLCAiZ2xvYiI6ICJGSUxF",
    "U1lTVEVNX1JFQUQiLCAidGVtcGZpbGUiOiAiRklMRVNZU1RFTV9SRUFEIiwKICAgICJ0aW1lIjogIkNMT0NLX1JFQUQiLCAi",
    "ZGF0ZXRpbWUiOiAiQ0xPQ0tfUkVBRCIsICJtb25vdG9uaWMiOiAiQ0xPQ0tfUkVBRCIsCiAgICAicmFuZG9tIjogIlJBTkRP",
    "TV9SRUFEIiwgInNlY3JldHMiOiAiUkFORE9NX1JFQUQiLCAidXVpZCI6ICJSQU5ET01fUkVBRCIsCiAgICAic29ja2V0Ijog",
    "Ik5FVFdPUktfQ0FQQUJMRV9JTVBPUlQiLCAidXJsbGliIjogIk5FVFdPUktfQ0FQQUJMRV9JTVBPUlQiLAogICAgInJlcXVl",
    "c3RzIjogIk5FVFdPUktfQ0FQQUJMRV9JTVBPUlQiLCAiaHR0cCI6ICJORVRXT1JLX0NBUEFCTEVfSU1QT1JUIiwKICAgICJz",
    "dWJwcm9jZXNzIjogIlNVQlBST0NFU1NfU1BBV04iLCAic2h1dGlsIjogIlNVQlBST0NFU1NfU1BBV04iLCAibXVsdGlwcm9j",
    "ZXNzaW5nIjoKICAgICAgICAiU1VCUFJPQ0VTU19TUEFXTiIsCiAgICAiaW1wb3J0bGliIjogIkRZTkFNSUNfSU1QT1JUIiwg",
    "Il9faW1wb3J0X18iOiAiRFlOQU1JQ19JTVBPUlQiLCAicGtndXRpbCI6ICJEWU5BTUlDX0lNUE9SVCIsCiAgICAiZXZhbCI6",
    "ICJFVkFMX0VYRUMiLCAiZXhlYyI6ICJFVkFMX0VYRUMiLCAiY29tcGlsZSI6ICJFVkFMX0VYRUMiLAogICAgImdldGF0dHIi",
    "OiAiRFlOQU1JQ19BVFRSSUJVVEVfQUNDRVNTIiwgInNldGF0dHIiOiAiRFlOQU1JQ19BVFRSSUJVVEVfQUNDRVNTIiwKICAg",
    "ICJkZWxhdHRyIjogIkRZTkFNSUNfQVRUUklCVVRFX0FDQ0VTUyIsICJoYXNhdHRyIjogIkRZTkFNSUNfQVRUUklCVVRFX0FD",
    "Q0VTUyIsCiAgICAidmFycyI6ICJEWU5BTUlDX0FUVFJJQlVURV9BQ0NFU1MiLCAiZGlyIjogIkRZTkFNSUNfQVRUUklCVVRF",
    "X0FDQ0VTUyIsCiAgICAiZ2xvYmFscyI6ICJBTUJJRU5UX05BTUVTUEFDRV9SRUFEIiwgImxvY2FscyI6ICJBTUJJRU5UX05B",
    "TUVTUEFDRV9SRUFEIiwKfQoKIyBBU1Qgbm9kZSB0eXBlcyB0aGUgY29udHJhY3QgYWRtaXRzLiBFVkVSWVRISU5HIEVMU0Ug",
    "SVMgUkVGVVNFRCAoTk9ERV9PVVRTSURFX0NPTlRSQUNUKS4gVGhpcyBpcwojIHRoZSBmYWlsLWNsb3NlZCBkaXJlY3Rpb246",
    "IGEgY29uc3RydWN0IG5vYm9keSBhbnRpY2lwYXRlZCBpcyByZWZ1c2VkIGJ5IERFRkFVTFQgcmF0aGVyIHRoYW4KIyBhZG1p",
    "dHRlZCBieSBvbWlzc2lvbiBmcm9tIGEgZGVueWxpc3QuCl9BTExPV0VEX05PREVTOiBmcm96ZW5zZXQgPSBmcm96ZW5zZXQo",
    "ewogICAgYXN0Lk1vZHVsZSwgYXN0LkZ1bmN0aW9uRGVmLCBhc3QuYXJndW1lbnRzLCBhc3QuYXJnLCBhc3QuTGFtYmRhLAog",
    "ICAgYXN0LlJldHVybiwgYXN0LkFzc2lnbiwgYXN0LkF1Z0Fzc2lnbiwgYXN0LkFubkFzc2lnbiwgYXN0LkV4cHIsIGFzdC5Q",
    "YXNzLAogICAgYXN0LklmLCBhc3QuRm9yLCBhc3QuQnJlYWssIGFzdC5Db250aW51ZSwgYXN0LlJhaXNlLCBhc3QuQXNzZXJ0",
    "LAogICAgYXN0Lk5hbWUsIGFzdC5Mb2FkLCBhc3QuU3RvcmUsIGFzdC5EZWwsIGFzdC5BdHRyaWJ1dGUsIGFzdC5TdWJzY3Jp",
    "cHQsIGFzdC5TbGljZSwKICAgIGFzdC5DYWxsLCBhc3Qua2V5d29yZCwgYXN0LlN0YXJyZWQsIGFzdC5Db25zdGFudCwKICAg",
    "IGFzdC5UdXBsZSwgYXN0Lkxpc3QsIGFzdC5TZXQsIGFzdC5EaWN0LAogICAgYXN0Lkxpc3RDb21wLCBhc3QuU2V0Q29tcCwg",
    "YXN0LkRpY3RDb21wLCBhc3QuR2VuZXJhdG9yRXhwLCBhc3QuY29tcHJlaGVuc2lvbiwKICAgIGFzdC5Cb29sT3AsIGFzdC5C",
    "aW5PcCwgYXN0LlVuYXJ5T3AsIGFzdC5Db21wYXJlLCBhc3QuSWZFeHAsCiAgICBhc3QuSm9pbmVkU3RyLCBhc3QuRm9ybWF0",
    "dGVkVmFsdWUsCiAgICBhc3QuQW5kLCBhc3QuT3IsIGFzdC5Ob3QsIGFzdC5JbnZlcnQsIGFzdC5VQWRkLCBhc3QuVVN1YiwK",
    "ICAgIGFzdC5BZGQsIGFzdC5TdWIsIGFzdC5NdWx0LCBhc3QuRGl2LCBhc3QuRmxvb3JEaXYsIGFzdC5Nb2QsIGFzdC5Qb3cs",
    "CiAgICBhc3QuTFNoaWZ0LCBhc3QuUlNoaWZ0LCBhc3QuQml0T3IsIGFzdC5CaXRYb3IsIGFzdC5CaXRBbmQsCiAgICBhc3Qu",
    "RXEsIGFzdC5Ob3RFcSwgYXN0Lkx0LCBhc3QuTHRFLCBhc3QuR3QsIGFzdC5HdEUsIGFzdC5JcywgYXN0LklzTm90LCBhc3Qu",
    "SW4sIGFzdC5Ob3RJbiwKfSkKCiMgTm9kZSB0eXBlcyB3aXRoIGEgZGVkaWNhdGVkIGRpYWdub3NpcywgY2hlY2tlZCBiZWZv",
    "cmUgdGhlIGdlbmVyaWMgYWxsb3dsaXN0IHNvIHRoZSByZWZ1c2FsIG5hbWVzCiMgdGhlIHNpZGUtY2hhbm5lbCBjbGFzcyBy",
    "YXRoZXIgdGhhbiAib3V0c2lkZSB0aGUgY29udHJhY3QiLgpfTk9ERV9LSU5EID0gewogICAgYXN0LkltcG9ydDogIklNUE9S",
    "VF9TVEFURU1FTlQiLCBhc3QuSW1wb3J0RnJvbTogIklNUE9SVF9TVEFURU1FTlQiLAogICAgYXN0Lkdsb2JhbDogIkdMT0JB",
    "TF9TVEFURU1FTlQiLCBhc3QuTm9ubG9jYWw6ICJOT05MT0NBTF9TVEFURU1FTlQiLAogICAgYXN0LllpZWxkOiAiR0VORVJB",
    "VE9SX1dJVE5FU1MiLCBhc3QuWWllbGRGcm9tOiAiR0VORVJBVE9SX1dJVE5FU1MiLAogICAgYXN0LkF3YWl0OiAiQVNZTkNf",
    "Q09OU1RSVUNUIiwgYXN0LkFzeW5jRm9yOiAiQVNZTkNfQ09OU1RSVUNUIiwKICAgIGFzdC5Bc3luY1dpdGg6ICJBU1lOQ19D",
    "T05TVFJVQ1QiLCBhc3QuQXN5bmNGdW5jdGlvbkRlZjogIkFTWU5DX0NPTlNUUlVDVCIsCiAgICBhc3QuV2l0aDogIkNPTlRF",
    "WFRfTUFOQUdFUiIsIGFzdC5Ucnk6ICJFWENFUFRJT05fU1VQUFJFU1NJT04iLAogICAgYXN0LldoaWxlOiAiVU5CT1VOREVE",
    "X0xPT1AiLAp9CmlmIGhhc2F0dHIoYXN0LCAiVHJ5U3RhciIpOiAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAg",
    "ICAjIDMuMTErCiAgICBfTk9ERV9LSU5EW2FzdC5UcnlTdGFyXSA9ICJFWENFUFRJT05fU1VQUFJFU1NJT04iCl9OT0RFX0RF",
    "VEFJTCA9IHsKICAgICJJTVBPUlRfU1RBVEVNRU5UIjogImFuIGltcG9ydCBpbnNpZGUgYSB3aXRuZXNzIHJlYWNoZXMgdW5n",
    "b3Zlcm5lZCBtb2R1bGUgc3RhdGUgKGFuZCwgdmlhICIKICAgICAgICAgICAgICAgICAgICAgICAgImltcG9ydGxpYi9fX2lt",
    "cG9ydF9fLCBhcmJpdHJhcnkgY29kZSk7IHRoZSB3aXRuZXNzJ3MgaW5wdXRzIGFyZSBJTkpFQ1RFRCIsCiAgICAiR0xPQkFM",
    "X1NUQVRFTUVOVCI6ICJgZ2xvYmFsYCBiaW5kcyBtb2R1bGUgc3RhdGUgaW50byB0aGUgd2l0bmVzczsgbW9kdWxlIHN0YXRl",
    "IGlzIG5vdCBhbiAiCiAgICAgICAgICAgICAgICAgICAgICAgICJpbmplY3RlZCBpbnB1dCBhbmQgY2Fubm90IGJlIHBlcnR1",
    "cmJlZCBieSB0aGUgUDYgaW5zdHJ1bWVudCIsCiAgICAiTk9OTE9DQUxfU1RBVEVNRU5UIjogImBub25sb2NhbGAgY2FwdHVy",
    "ZXMgZW5jbG9zaW5nLXNjb3BlIHN0YXRlLCB3aGljaCBpcyBuZWl0aGVyIGluamVjdGVkICIKICAgICAgICAgICAgICAgICAg",
    "ICAgICAgICAibm9yIGVudW1lcmFibGUgYXMgYSBjaGFubmVsIiwKICAgICJHRU5FUkFUT1JfV0lUTkVTUyI6ICJhIGdlbmVy",
    "YXRvciBvYnNlcnZhdGlvbiBpcyBsYXp5IGFuZCBzdGF0ZWZ1bDsgaXRzIHZhbHVlIGRlcGVuZHMgb24gV0hFTiAiCiAgICAg",
    "ICAgICAgICAgICAgICAgICAgICAiaXQgaXMgZHJhaW5lZCwgc28gZGV0ZXJtaW5pc20gKEkxKSBpcyBub3QgZGVjaWRhYmxl",
    "IiwKICAgICJBU1lOQ19DT05TVFJVQ1QiOiAiYW4gYXdhaXRhYmxlIHdpdG5lc3Mgc3VzcGVuZHMgaW50byBhbiB1bmdvdmVy",
    "bmVkIGV2ZW50IGxvb3AiLAogICAgIkNPTlRFWFRfTUFOQUdFUiI6ICJgd2l0aGAgYWNxdWlyZXMgYSByZXNvdXJjZTsgdGhl",
    "IG9ubHkgaW5wdXRzIGEgd2l0bmVzcyBtYXkgaGF2ZSBhcmUgdGhlICIKICAgICAgICAgICAgICAgICAgICAgICAiaW5qZWN0",
    "ZWQgb25lcyIsCiAgICAiRVhDRVBUSU9OX1NVUFBSRVNTSU9OIjogInRyeS9leGNlcHQgd291bGQgU1dBTExPVyB0aGUgTmFt",
    "ZUVycm9yIHRoYXQgdGhlIHJlc3RyaWN0ZWQgZ2xvYmFscyAiCiAgICAgICAgICAgICAgICAgICAgICAgICAgICAgInJhaXNl",
    "IGZvciBhbiB1bmdvdmVybmVkIG5hbWUsIGNvbnZlcnRpbmcgYSBzdHJ1Y3R1cmFsIHJlZnVzYWwgaW50byAiCiAgICAgICAg",
    "ICAgICAgICAgICAgICAgICAgICAgImEgc2lsZW50IGZhbGxiYWNrOyB0aGUgc2luZ2xlIG1vc3QgaW1wb3J0YW50IG5vZGUt",
    "bGV2ZWwgcmVmdXNhbCIsCiAgICAiVU5CT1VOREVEX0xPT1AiOiAiYHdoaWxlYCBhZG1pdHMgbm9udGVybWluYXRpb24gYW5k",
    "IGl0ZXJhdGlvbi1jb3VudCBkZXBlbmRlbmNlOyBhIHdpdG5lc3MgIgogICAgICAgICAgICAgICAgICAgICAgIml0ZXJhdGVz",
    "IGl0cyBpbmplY3RlZCBpbnB1dHMsIHdoaWNoIGFyZSBmaW5pdGUiLAp9CgoKIyA9PT09PT09PT09PT09PT09PT09PT09PT09",
    "PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT0K",
    "IyBHT1ZFUk5FRCBQUklNSVRJVkUgUkVHSVNUUlkKIyA9PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09",
    "PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT0KIyBBIHByaW1pdGl2ZSBp",
    "cyBhIGRldGVybWluaXN0aWMgaGVscGVyIHRoZSBjb250cmFjdCBhZG1pdHMgQlkgTkFNRS4gSXQgaXMgdmFsaWRhdGVkIHVu",
    "ZGVyIHRoZSBzYW1lCiMgcnVsZXMgYXMgYSB3aXRuZXNzICh3aXRoIGl0cyBkZWNsYXJlZCBkZXBzIGluIHNjb3BlIGluc3Rl",
    "YWQgb2Ygbm90aGluZyksIGNvbnRlbnQtcGlubmVkIGJ5IHRoZQojIHNoYTI1NiBvZiBpdHMgc291cmNlLCBhbmQgcmUtYm91",
    "bmQgaW50byBJVFMgT1dOIHJlc3RyaWN0ZWQgZ2xvYmFscyDigJQgc28gYW4gImluZGlyZWN0IGhlbHBlcgojIGdsb2JhbCIg",
    "aXMgY2xvc2VkIHRvIHRoZSBzYW1lIGRlcHRoIGFzIGEgZGlyZWN0IG9uZTogYSBnb3Zlcm5lZCBwcmltaXRpdmUgY2Fubm90",
    "IHJlYWQgbW9kdWxlCiMgc3RhdGUgZWl0aGVyLgpfQUxMT1dFRF9ERVBfTU9EVUxFUyA9IGZyb3plbnNldCh7Imhhc2hsaWIi",
    "LCAianNvbiJ9KSAgICMgZGV0ZXJtaW5pc3RpYywgbm8gYW1iaWVudCBzdGF0ZSwgbm8gSS9PCgoKY2xhc3MgX1ByaW1pdGl2",
    "ZToKICAgIF9fc2xvdHNfXyA9ICgibmFtZSIsICJmbiIsICJkZXBzIiwgImRlcF9hdHRycyIsICJzaGEyNTYiLCAiYm91bmQi",
    "KQoKICAgIGRlZiBfX2luaXRfXyhzZWxmLCBuYW1lOiBzdHIsIGZuOiBDYWxsYWJsZSwgZGVwczogZGljdCwgZGVwX2F0dHJz",
    "OiBzZXQpIC0+IE5vbmU6CiAgICAgICAgc2VsZi5uYW1lLCBzZWxmLmZuLCBzZWxmLmRlcHMsIHNlbGYuZGVwX2F0dHJzID0g",
    "bmFtZSwgZm4sIGRlcHMsIGRlcF9hdHRycwogICAgICAgIHNlbGYuc2hhMjU2ID0gX3NvdXJjZV9zaGEoZm4pCiAgICAgICAg",
    "c2VsZi5ib3VuZDogT3B0aW9uYWxbQ2FsbGFibGVdID0gTm9uZQoKClBSSU1JVElWRVM6IGRpY3Rbc3RyLCBfUHJpbWl0aXZl",
    "XSA9IHt9CgoKZGVmIF9zb3VyY2Vfc2hhKGZuOiBDYWxsYWJsZSkgLT4gc3RyOgogICAgdHJ5OgogICAgICAgIHNyYyA9IHRl",
    "eHR3cmFwLmRlZGVudChpbnNwZWN0LmdldHNvdXJjZShmbikpCiAgICBleGNlcHQgKE9TRXJyb3IsIFR5cGVFcnJvcik6CiAg",
    "ICAgICAgcmV0dXJuICI8bm8tc291cmNlPiIKICAgIHJldHVybiBoYXNobGliLnNoYTI1NihzcmMuZW5jb2RlKCkpLmhleGRp",
    "Z2VzdCgpCgoKZGVmIHJlZ2lzdGVyX3ByaW1pdGl2ZShuYW1lOiBzdHIsIGZuOiBDYWxsYWJsZSwgZGVwczogT3B0aW9uYWxb",
    "ZGljdF0gPSBOb25lLAogICAgICAgICAgICAgICAgICAgICAgIGRlcF9hdHRyczogT3B0aW9uYWxbc2V0XSA9IE5vbmUpIC0+",
    "IGxpc3RbZGljdF06CiAgICAiIiJBZG1pdCBgZm5gIHRvIHRoZSBnb3Zlcm5lZCBzdXJmYWNlIHVuZGVyIGBuYW1lYC4gUmV0",
    "dXJucyB0aGUgcHJvYmxlbXMgdGhhdCBCTE9DS0VECiAgICByZWdpc3RyYXRpb24gKFtdIG1lYW5zIHJlZ2lzdGVyZWQpLiBB",
    "IHByaW1pdGl2ZSB3aG9zZSBkZWNsYXJlZCBkZXBzIGFyZSBub3QgZGV0ZXJtaW5pc3RpYwogICAgc3RkbGliIG1vZHVsZXMg",
    "aXMgcmVmdXNlZDsgYGRlcF9hdHRyc2AgaXMgdGhlIERFQ0xBUkVEIHNldCBvZiBhdHRyaWJ1dGUgbmFtZXMgdGhlIHByaW1p",
    "dGl2ZSBtYXkKICAgIHVzZSBvbiB0aG9zZSBkZXBzIChoYXNobGliLnNoYTI1NiwganNvbi5kdW1wcykg4oCUIGRlY2xhcmVk",
    "LCByZXZpZXdhYmxlIGFuZCBuYXJyb3csIG5ldmVyIGEKICAgIGJsYW5rZXQgZXhlbXB0aW9uIGZyb20gdGhlIGF0dHJpYnV0",
    "ZSBydWxlLiIiIgogICAgZGVwcyA9IGRpY3QoZGVwcyBvciB7fSkKICAgIGRlcF9hdHRycyA9IHNldChkZXBfYXR0cnMgb3Ig",
    "KCkpCiAgICBwcm9ibGVtczogbGlzdFtkaWN0XSA9IFtdCiAgICBmb3IgZGVwX25hbWUsIGRlcF9vYmogaW4gZGVwcy5pdGVt",
    "cygpOgogICAgICAgIG1vZCA9IGdldGF0dHIoZGVwX29iaiwgIl9fbmFtZV9fIiwgTm9uZSkKICAgICAgICBpZiBub3QgaXNp",
    "bnN0YW5jZShkZXBfb2JqLCB0eXBlcy5Nb2R1bGVUeXBlKSBvciBtb2Qgbm90IGluIF9BTExPV0VEX0RFUF9NT0RVTEVTOgog",
    "ICAgICAgICAgICBwcm9ibGVtcy5hcHBlbmQoX3Byb2JsZW0oCiAgICAgICAgICAgICAgICAiUFJJTUlUSVZFX0RFUF9VTkdP",
    "VkVSTkVEIiwgZiJ7bmFtZX0ue2RlcF9uYW1lfSIsCiAgICAgICAgICAgICAgICBmInByaW1pdGl2ZSB7bmFtZSFyfSBkZWNs",
    "YXJlcyBkZXBlbmRlbmN5IHtkZXBfbmFtZSFyfSAtPiB7bW9kIXJ9LCB3aGljaCBpcyBub3QgaW4gIgogICAgICAgICAgICAg",
    "ICAgZiJ0aGUgZGV0ZXJtaW5pc3RpYyBkZXAgYWxsb3dsaXN0IHtzb3J0ZWQoX0FMTE9XRURfREVQX01PRFVMRVMpfTsgUkVG",
    "VVNFRCIpKQogICAgZm9yIGF0dHIgaW4gc29ydGVkKGRlcF9hdHRycyk6CiAgICAgICAgaWYgYXR0ci5zdGFydHN3aXRoKCJf",
    "Iik6CiAgICAgICAgICAgIHByb2JsZW1zLmFwcGVuZChfcHJvYmxlbSgKICAgICAgICAgICAgICAgICJQUklNSVRJVkVfREVQ",
    "X1VOR09WRVJORUQiLCBmIntuYW1lfS57YXR0cn0iLAogICAgICAgICAgICAgICAgZiJwcmltaXRpdmUge25hbWUhcn0gZGVj",
    "bGFyZXMgdGhlIHByaXZhdGUgZGVwIGF0dHJpYnV0ZSB7YXR0ciFyfTsgUkVGVVNFRCIpKQogICAgcHJvYmxlbXMgKz0gdmVy",
    "aWZ5X3dpdG5lc3NfcHVyaXR5KGZuLCByb2xlPSJwcmltaXRpdmUiLCBleHRyYV9uYW1lcz1zZXQoZGVwcyksCiAgICAgICAg",
    "ICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgZXh0cmFfYXR0cnM9ZGVwX2F0dHJzLCBfc2tpcF9yZWdpc3RyeT1uYW1l",
    "KQogICAgaWYgcHJvYmxlbXM6CiAgICAgICAgcmV0dXJuIHByb2JsZW1zCiAgICBQUklNSVRJVkVTW25hbWVdID0gX1ByaW1p",
    "dGl2ZShuYW1lLCBmbiwgZGVwcywgZGVwX2F0dHJzKQogICAgcmV0dXJuIFtdCgoKIyA9PT09PT09PT09PT09PT09PT09PT09",
    "PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09",
    "PT0KIyBMMSDigJQgU0hBUEUKIyA9PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09",
    "PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT0KZGVmIF9jaGVja19zaGFwZShmbjogQW55LCBy",
    "b2xlOiBzdHIpIC0+IHR1cGxlW2xpc3RbZGljdF0sIE9wdGlvbmFsW2FzdC5GdW5jdGlvbkRlZl0sIE9wdGlvbmFsW3N0cl1d",
    "OgogICAgIiIiTDEuIFJldHVybnMgKHByb2JsZW1zLCB0aGUgcGFyc2VkIGRlZiBvciBOb25lLCB0aGUgcmVjb3ZlcmVkIHNv",
    "dXJjZSBvciBOb25lKS4iIiIKICAgIHByb2JsZW1zOiBsaXN0W2RpY3RdID0gW10KICAgIGlmIG5vdCBpc2luc3RhbmNlKGZu",
    "LCB0eXBlcy5GdW5jdGlvblR5cGUpOgogICAgICAgIHdoYXQgPSB0eXBlKGZuKS5fX25hbWVfXwogICAgICAgIGhpbnQgPSB7",
    "CiAgICAgICAgICAgICJwYXJ0aWFsIjogImZ1bmN0b29scy5wYXJ0aWFsIHByZS1iaW5kcyB1bmdvdmVybmVkIGFyZ3VtZW50",
    "cyBhbmQgaGFzIG5vIHNvdXJjZSIsCiAgICAgICAgICAgICJtZXRob2QiOiAiYSBib3VuZCBtZXRob2QgY2FycmllcyBgc2Vs",
    "ZmAg4oCUIGFuIG9iamVjdCB3aG9zZSBhdHRyaWJ1dGVzIGFyZSB1bmdvdmVybmVkICIKICAgICAgICAgICAgICAgICAgICAg",
    "ICJzdGF0ZSAodGhlICdjYWNoZWQgcHJldmlvdXMgcmVzdWx0JyBjaGFubmVsKSIsCiAgICAgICAgICAgICJidWlsdGluX2Z1",
    "bmN0aW9uX29yX21ldGhvZCI6ICJhIEMgY2FsbGFibGUgaGFzIG5vIEFTVCBhbmQgY2Fubm90IGJlIHZhbGlkYXRlZCIsCiAg",
    "ICAgICAgfS5nZXQod2hhdCwgIm9ubHkgYSBwbGFpbiBtb2R1bGUtbGV2ZWwgUHl0aG9uIGZ1bmN0aW9uIGNhbiBiZSBzdGF0",
    "aWNhbGx5IHZhbGlkYXRlZDsgYSAiCiAgICAgICAgICAgICAgICAgICAgImNhbGxhYmxlIG9iamVjdCBjYXJyaWVzIGluc3Rh",
    "bmNlIHN0YXRlIHRoYXQgaXMgbmVpdGhlciBpbmplY3RlZCBub3IgIgogICAgICAgICAgICAgICAgICAgICJlbnVtZXJhYmxl",
    "IikKICAgICAgICByZXR1cm4gKFtfcHJvYmxlbSgiTk9OX0ZVTkNUSU9OX0NBTExBQkxFIiwgTm9uZSwKICAgICAgICAgICAg",
    "ICAgICAgICAgICAgICBmInRoZSB7cm9sZX0gaXMgYSB7d2hhdH0sIG5vdCBhIGZ1bmN0aW9uOiB7aGludH07IFJFRlVTRUQi",
    "KV0sIE5vbmUsIE5vbmUpCiAgICBjb2RlID0gZm4uX19jb2RlX18KICAgIGlmIGZuLl9fY2xvc3VyZV9fIG9yIGNvZGUuY29f",
    "ZnJlZXZhcnM6CiAgICAgICAgcHJvYmxlbXMuYXBwZW5kKF9wcm9ibGVtKAogICAgICAgICAgICAiQ0xPU1VSRV9DQVBUVVJF",
    "IiwgIiwiLmpvaW4oY29kZS5jb19mcmVldmFycykgb3IgTm9uZSwKICAgICAgICAgICAgZiJ0aGUge3JvbGV9IGNhcHR1cmVz",
    "IGZyZWUgdmFyaWFibGVzIHtsaXN0KGNvZGUuY29fZnJlZXZhcnMpfSBpbiBjbG9zdXJlIGNlbGxzOyBhIGNlbGwgIgogICAg",
    "ICAgICAgICAiaXMgbXV0YWJsZSBhbWJpZW50IHN0YXRlIHRoYXQgbm8gY2hhbm5lbCBlbnVtZXJhdGlvbiBjYW4gcmVhY2gg",
    "YW5kIG5vIHBlcnR1cmJhdGlvbiAiCiAgICAgICAgICAgICJjYW4gdmFyeTsgUkVGVVNFRCIpKQogICAgaWYgZm4uX19kZWZh",
    "dWx0c19fIGlzIG5vdCBOb25lIG9yIGZuLl9fa3dkZWZhdWx0c19fIGlzIG5vdCBOb25lOgogICAgICAgIHByb2JsZW1zLmFw",
    "cGVuZChfcHJvYmxlbSgKICAgICAgICAgICAgIkRFRkFVTFRfQVJHVU1FTlQiLCBOb25lLAogICAgICAgICAgICBmInRoZSB7",
    "cm9sZX0gZGVjbGFyZXMgcGFyYW1ldGVyIGRlZmF1bHRzIHtmbi5fX2RlZmF1bHRzX18hcn0ve2ZuLl9fa3dkZWZhdWx0c19f",
    "IXJ9OyBhICIKICAgICAgICAgICAgImRlZmF1bHQgaXMgZXZhbHVhdGVkIE9OQ0UgYXQgZGVmaW5pdGlvbiB0aW1lIGFuZCwg",
    "aWYgbXV0YWJsZSwgaXMgYSBwZXItcHJvY2VzcyBjYWNoZSAiCiAgICAgICAgICAgICLigJQgYW4gdW4tZW51bWVyYWJsZSBj",
    "aGFubmVsIHRoYXQgc3Vydml2ZXMgYmV0d2VlbiBpbnN0cnVtZW50ZWQgcnVuczsgUkVGVVNFRCIpKQogICAgaWYgcm9sZSA9",
    "PSAid2l0bmVzcyI6CiAgICAgICAgbmFyZ3MgPSBjb2RlLmNvX2FyZ2NvdW50ICsgY29kZS5jb19rd29ubHlhcmdjb3VudCAr",
    "IGNvZGUuY29fcG9zb25seWFyZ2NvdW50CiAgICAgICAgdmFyYXJncyA9IGJvb2woY29kZS5jb19mbGFncyAmIDB4MDQpIG9y",
    "IGJvb2woY29kZS5jb19mbGFncyAmIDB4MDgpCiAgICAgICAgaWYgY29kZS5jb19hcmdjb3VudCAhPSAxIG9yIGNvZGUuY29f",
    "a3dvbmx5YXJnY291bnQgb3IgdmFyYXJnczoKICAgICAgICAgICAgcHJvYmxlbXMuYXBwZW5kKF9wcm9ibGVtKAogICAgICAg",
    "ICAgICAgICAgIlNJR05BVFVSRV9PVVRTSURFX0NPTlRSQUNUIiwgTm9uZSwKICAgICAgICAgICAgICAgIGYiYSB3aXRuZXNz",
    "IHRha2VzIGV4YWN0bHkgb25lIHBvc2l0aW9uYWwgcGFyYW1ldGVyICh0aGUgc3BlYyk7IHRoaXMgb25lIHRha2VzICIKICAg",
    "ICAgICAgICAgICAgIGYie25hcmdzfSBwYXJhbWV0ZXIocyl7JyBwbHVzICphcmdzLyoqa3dhcmdzJyBpZiB2YXJhcmdzIGVs",
    "c2UgJyd9OyBldmVyeSBvdGhlciAiCiAgICAgICAgICAgICAgICAiaW5wdXQgbXVzdCBhcnJpdmUgdGhyb3VnaCB0aGUgaW5q",
    "ZWN0ZWQgYF93aXRuZXNzX2lucHV0c2A7IFJFRlVTRUQiKSkKICAgIHRyeToKICAgICAgICBzcmMgPSB0ZXh0d3JhcC5kZWRl",
    "bnQoaW5zcGVjdC5nZXRzb3VyY2UoZm4pKQogICAgZXhjZXB0IChPU0Vycm9yLCBUeXBlRXJyb3IpIGFzIGV4YzoKICAgICAg",
    "ICByZXR1cm4gKHByb2JsZW1zICsgW19wcm9ibGVtKAogICAgICAgICAgICAiTk9fU09VUkNFX1VOVkVSSUZJQUJMRSIsIE5v",
    "bmUsCiAgICAgICAgICAgIGYic291cmNlIGZvciB0aGUge3JvbGV9IGNvdWxkIG5vdCBiZSByZWNvdmVyZWQgKHt0eXBlKGV4",
    "YykuX19uYW1lX199OiB7ZXhjfSk7IGEgIgogICAgICAgICAgICAiY2FsbGFibGUgd2hvc2UgdGV4dCBjYW5ub3QgYmUgcmVh",
    "ZCBjYW5ub3QgYmUgc3RhdGljYWxseSB2YWxpZGF0ZWQsIGFuZCBhbiAiCiAgICAgICAgICAgICJ1bnZhbGlkYXRhYmxlIHdp",
    "dG5lc3MgZmFpbHMgY2xvc2VkIiwgKV0sIE5vbmUsIE5vbmUpCiAgICB0cnk6CiAgICAgICAgdHJlZSA9IGFzdC5wYXJzZShz",
    "cmMpCiAgICBleGNlcHQgU3ludGF4RXJyb3IgYXMgZXhjOgogICAgICAgIHJldHVybiAocHJvYmxlbXMgKyBbX3Byb2JsZW0o",
    "IlNPVVJDRV9VTlBBUlNFQUJMRSIsIE5vbmUsCiAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICBmInNvdXJj",
    "ZSBkaWQgbm90IHBhcnNlOiB7ZXhjfTsgUkVGVVNFRCIpXSwgTm9uZSwgc3JjKQogICAgYm9keSA9IHRyZWUuYm9keQogICAg",
    "aWYgbGVuKGJvZHkpICE9IDEgb3Igbm90IGlzaW5zdGFuY2UoYm9keVswXSwgYXN0LkZ1bmN0aW9uRGVmKToKICAgICAgICBy",
    "ZXR1cm4gKHByb2JsZW1zICsgW19wcm9ibGVtKAogICAgICAgICAgICAiTk9UX0FfUExBSU5fRlVOQ1RJT05fREVGIiwgTm9u",
    "ZSwKICAgICAgICAgICAgZiJ0aGUgcmVjb3ZlcmVkIHNvdXJjZSBmb3IgdGhlIHtyb2xlfSBpcyBub3QgYSBzaW5nbGUgYGRl",
    "ZmA7IFJFRlVTRUQiKV0sIE5vbmUsIHNyYykKICAgIGZkZWYgPSBib2R5WzBdCiAgICBpZiBmZGVmLmRlY29yYXRvcl9saXN0",
    "OgogICAgICAgIHByb2JsZW1zLmFwcGVuZChfcHJvYmxlbSgKICAgICAgICAgICAgIkRFQ09SQVRFRF9XSVRORVNTIiwgTm9u",
    "ZSwKICAgICAgICAgICAgZiJ0aGUge3JvbGV9IGlzIGRlY29yYXRlZDsgYSBkZWNvcmF0b3Igd3JhcHMgdGhlIHZhbGlkYXRl",
    "ZCBib2R5IGluIHVuZ292ZXJuZWQgY29kZSAiCiAgICAgICAgICAgICIoY2FjaGluZywgcmV0cnksIGxvZ2dpbmcpIHRoYXQg",
    "dGhlIEFTVCBvZiB0aGUgaW5uZXIgZGVmIGRvZXMgbm90IGRlc2NyaWJlOyBSRUZVU0VEIikpCiAgICByZXR1cm4gcHJvYmxl",
    "bXMsIGZkZWYsIHNyYwoKCiMgPT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09",
    "PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09CiMgTDIg4oCUIEFTVAojID09PT09PT09PT09PT09",
    "PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09",
    "PT09PT09PT09PQpjbGFzcyBfUHVyaXR5VmlzaXRvcihhc3QuTm9kZVZpc2l0b3IpOgogICAgZGVmIF9faW5pdF9fKHNlbGYs",
    "IGdvdmVybmVkOiBzZXRbc3RyXSwgYXR0cnM6IGZyb3plbnNldCkgLT4gTm9uZToKICAgICAgICBzZWxmLmdvdmVybmVkID0g",
    "Z292ZXJuZWQgICAgICAgICAgICAjIHByaW1pdGl2ZXMgKyBkZWNsYXJlZCBkZXBzICsgYWxsb3dsaXN0ZWQgYnVpbHRpbnMK",
    "ICAgICAgICBzZWxmLmF0dHJzID0gYXR0cnMgICAgICAgICAgICAgICAgICAjIEFMTE9XRURfQVRUUklCVVRFUyArIGEgcHJp",
    "bWl0aXZlJ3MgZGVjbGFyZWQgZGVwX2F0dHJzCiAgICAgICAgc2VsZi5wcm9ibGVtczogbGlzdFtkaWN0XSA9IFtdCiAgICAg",
    "ICAgc2VsZi5ib3VuZDogc2V0W3N0cl0gPSBzZXQoKSAgICAgICAgIyBldmVyeSBuYW1lIGJvdW5kIGFueXdoZXJlIGluIHRo",
    "ZSBjYWxsYWJsZQoKICAgICMgLS0gYmluZGluZyBjb2xsZWN0aW9uIChjb25zZXJ2YXRpdmU6IGFueSBuYW1lIGJvdW5kIEFO",
    "WVdIRVJFIGNvdW50cyBhcyBsb2NhbCkgLS0tLS0tLS0tLS0tLQogICAgZGVmIGNvbGxlY3RfYmluZGluZ3Moc2VsZiwgbm9k",
    "ZTogYXN0LkFTVCkgLT4gTm9uZToKICAgICAgICBmb3IgbiBpbiBhc3Qud2Fsayhub2RlKToKICAgICAgICAgICAgaWYgaXNp",
    "bnN0YW5jZShuLCBhc3QuTmFtZSkgYW5kIGlzaW5zdGFuY2Uobi5jdHgsIChhc3QuU3RvcmUsIGFzdC5EZWwpKToKICAgICAg",
    "ICAgICAgICAgIHNlbGYuYm91bmQuYWRkKG4uaWQpCiAgICAgICAgICAgIGVsaWYgaXNpbnN0YW5jZShuLCBhc3QuYXJnKToK",
    "ICAgICAgICAgICAgICAgIHNlbGYuYm91bmQuYWRkKG4uYXJnKQogICAgICAgICAgICBlbGlmIGlzaW5zdGFuY2UobiwgKGFz",
    "dC5GdW5jdGlvbkRlZiwgYXN0LkFzeW5jRnVuY3Rpb25EZWYpKToKICAgICAgICAgICAgICAgIHNlbGYuYm91bmQuYWRkKG4u",
    "bmFtZSkKCiAgICAjIC0tIHRoZSBhbGxvd2xpc3QgZ2F0ZSAtLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0t",
    "LS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLQogICAgZGVmIGdlbmVyaWNfdmlzaXQoc2VsZiwgbm9kZTogYXN0LkFT",
    "VCkgLT4gTm9uZToKICAgICAgICBraW5kID0gX05PREVfS0lORC5nZXQodHlwZShub2RlKSkKICAgICAgICBpZiBraW5kIGlz",
    "IG5vdCBOb25lOgogICAgICAgICAgICBzZWxmLnByb2JsZW1zLmFwcGVuZChfcHJvYmxlbSgKICAgICAgICAgICAgICAgIGtp",
    "bmQsIE5vbmUsIGYie3R5cGUobm9kZSkuX19uYW1lX199OiB7X05PREVfREVUQUlMW2tpbmRdfTsgUkVGVVNFRCIsIG5vZGU9",
    "bm9kZSkpCiAgICAgICAgICAgIHJldHVybgogICAgICAgIGlmIHR5cGUobm9kZSkgbm90IGluIF9BTExPV0VEX05PREVTOgog",
    "ICAgICAgICAgICBzZWxmLnByb2JsZW1zLmFwcGVuZChfcHJvYmxlbSgKICAgICAgICAgICAgICAgICJOT0RFX09VVFNJREVf",
    "Q09OVFJBQ1QiLCBOb25lLAogICAgICAgICAgICAgICAgZiJge3R5cGUobm9kZSkuX19uYW1lX199YCBpcyBub3QgaW4gdGhl",
    "IHtDT05UUkFDVF9JRH0gbm9kZSBhbGxvd2xpc3QuIFRoZSBjb250cmFjdCAiCiAgICAgICAgICAgICAgICAiYWRtaXRzIGEg",
    "Zml4ZWQgc2V0IG9mIGNvbnN0cnVjdHMgYW5kIFJFRlVTRVMgZXZlcnl0aGluZyBlbHNlLCBzbyBhIGNvbnN0cnVjdCAiCiAg",
    "ICAgICAgICAgICAgICAid2hvc2UgY2hhbm5lbCBpbXBsaWNhdGlvbnMgd2VyZSBuZXZlciBhbmFseXNlZCBjYW5ub3QgcGFz",
    "cyBieSBvbWlzc2lvbjsgUkVGVVNFRCIsCiAgICAgICAgICAgICAgICBub2RlPW5vZGUpKQogICAgICAgICAgICByZXR1cm4K",
    "ICAgICAgICBzdXBlcigpLmdlbmVyaWNfdmlzaXQobm9kZSkKCiAgICBkZWYgdmlzaXRfTmFtZShzZWxmLCBub2RlOiBhc3Qu",
    "TmFtZSkgLT4gTm9uZToKICAgICAgICBpZiBpc2luc3RhbmNlKG5vZGUuY3R4LCAoYXN0LlN0b3JlLCBhc3QuRGVsKSk6CiAg",
    "ICAgICAgICAgIHJldHVybgogICAgICAgIG5hbWUgPSBub2RlLmlkCiAgICAgICAgaWYgbmFtZSBpbiBzZWxmLmJvdW5kIG9y",
    "IG5hbWUgaW4gc2VsZi5nb3Zlcm5lZDoKICAgICAgICAgICAgcmV0dXJuCiAgICAgICAga2luZCA9IF9OQU1FRF9DSEFOTkVM",
    "LmdldChuYW1lKQogICAgICAgIGlmIGtpbmQgaXMgTm9uZToKICAgICAgICAgICAga2luZCA9ICJNT0RVTEVfR0xPQkFMX1JF",
    "QUQiCiAgICAgICAgICAgIGRldGFpbCA9IChmInRoZSBuYW1lIHtuYW1lIXJ9IHJlc29sdmVzIHRvIG5laXRoZXIgYSBsb2Nh",
    "bCwgYSBnb3Zlcm5lZCBwcmltaXRpdmUgIgogICAgICAgICAgICAgICAgICAgICAgZiIoe3NvcnRlZChQUklNSVRJVkVTKX0p",
    "LCBub3IgYW4gYWxsb3dsaXN0ZWQgYnVpbHRpbiDigJQgaXQgaXMgTU9EVUxFIFNUQVRFLiAiCiAgICAgICAgICAgICAgICAg",
    "ICAgICAiTW9kdWxlIHN0YXRlIGlzIG5vdCBhbiBpbmplY3RlZCBpbnB1dDogdGhlIFA2IGluc3RydW1lbnQgY2Fubm90IGVu",
    "dW1lcmF0ZSAiCiAgICAgICAgICAgICAgICAgICAgICAiaXQsIGNhbm5vdCBwZXJ0dXJiIGl0IGFuZCB0aGVyZWZvcmUgY2Fu",
    "bm90IHByb3ZlIHRoZSBvYnNlcnZhdGlvbiBpcyAiCiAgICAgICAgICAgICAgICAgICAgICAiaW5kZXBlbmRlbnQgb2YgdGhl",
    "IGNvbGxlY3Rpb24gdW5kZXIgdGVzdC4gVGhpcyBpcyBQNi1MSU0tMDIgaXRzZWxmIikKICAgICAgICBlbHNlOgogICAgICAg",
    "ICAgICBkZXRhaWwgPSAoZiJ0aGUgbmFtZSB7bmFtZSFyfSBpcyBhbiB1bmdvdmVybmVkIGFtYmllbnQgY2hhbm5lbCBvZiBj",
    "bGFzcyB7a2luZH07IGEgIgogICAgICAgICAgICAgICAgICAgICAgIndpdG5lc3Mgb2JzZXJ2YXRpb24gbXVzdCBiZSBhIGZ1",
    "bmN0aW9uIG9mIGl0cyBpbmplY3RlZCBpbnB1dHMgYWxvbmUiKQogICAgICAgIHNlbGYucHJvYmxlbXMuYXBwZW5kKF9wcm9i",
    "bGVtKGtpbmQsIG5hbWUsIGRldGFpbCArICI7IFJFRlVTRUQiLCBub2RlPW5vZGUpKQoKICAgIGRlZiB2aXNpdF9BdHRyaWJ1",
    "dGUoc2VsZiwgbm9kZTogYXN0LkF0dHJpYnV0ZSkgLT4gTm9uZToKICAgICAgICBhdHRyID0gbm9kZS5hdHRyCiAgICAgICAg",
    "aWYgYXR0ci5zdGFydHN3aXRoKCJfIik6CiAgICAgICAgICAgIHNlbGYucHJvYmxlbXMuYXBwZW5kKF9wcm9ibGVtKAogICAg",
    "ICAgICAgICAgICAgIkRVTkRFUl9BVFRSSUJVVEVfQUNDRVNTIiwgYXR0ciwKICAgICAgICAgICAgICAgIGYiYXR0cmlidXRl",
    "IHthdHRyIXJ9IGJlZ2lucyB3aXRoIGFuIHVuZGVyc2NvcmUuIFByaXZhdGUgYW5kIGR1bmRlciBhdHRyaWJ1dGVzIGFyZSAi",
    "CiAgICAgICAgICAgICAgICAidGhlIGVzY2FwZSBoYXRjaCBvdXQgb2YgZXZlcnkgcmVzdHJpY3RlZCBuYW1lc3BhY2UgIgog",
    "ICAgICAgICAgICAgICAgIih4Ll9fY2xhc3NfXy5fX2Jhc2VfXy5fX3N1YmNsYXNzZXNfXygpKSwgYW5kIGBfYC1wcmVmaXhl",
    "ZCBhdHRyaWJ1dGVzIGFyZSAiCiAgICAgICAgICAgICAgICAiaW1wbGVtZW50YXRpb24gc3RhdGUgcmF0aGVyIHRoYW4gYSBn",
    "b3Zlcm5lZCBpbnRlcmZhY2U7IFJFRlVTRUQiLCBub2RlPW5vZGUpKQogICAgICAgICAgICByZXR1cm4KICAgICAgICBpZiBh",
    "dHRyIG5vdCBpbiBzZWxmLmF0dHJzOgogICAgICAgICAgICBzZWxmLnByb2JsZW1zLmFwcGVuZChfcHJvYmxlbSgKICAgICAg",
    "ICAgICAgICAgICJVTkdPVkVSTkVEX0FUVFJJQlVURSIsIGF0dHIsCiAgICAgICAgICAgICAgICBmImF0dHJpYnV0ZSB7YXR0",
    "ciFyfSBpcyBub3QgaW4gdGhlIHtDT05UUkFDVF9JRH0gYXR0cmlidXRlIGFsbG93bGlzdDsgYW4gIgogICAgICAgICAgICAg",
    "ICAgImF0dHJpYnV0ZSByZWFkIGlzIGEgcmVhZCBvZiBhbiBvYmplY3QncyBoaWRkZW4gc3RhdGUsIHdoaWNoIGlzIG5laXRo",
    "ZXIgaW5qZWN0ZWQgIgogICAgICAgICAgICAgICAgIm5vciBwZXJ0dXJiYWJsZTsgUkVGVVNFRCIsIG5vZGU9bm9kZSkpCiAg",
    "ICAgICAgICAgIHJldHVybgogICAgICAgIHNlbGYuZ2VuZXJpY192aXNpdChub2RlKQoKCiMgPT09PT09PT09PT09PT09PT09",
    "PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09",
    "PT09PT09CiMgTDMg4oCUIENPREUgT0JKRUNUIChzb3VyY2UtaW5kZXBlbmRlbnQpCiMgPT09PT09PT09PT09PT09PT09PT09",
    "PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09",
    "PT09CmRlZiBfd2Fsa19jb2RlKGNvZGU6IHR5cGVzLkNvZGVUeXBlKToKICAgIHlpZWxkIGNvZGUKICAgIGZvciBjb25zdCBp",
    "biBjb2RlLmNvX2NvbnN0czoKICAgICAgICBpZiBpc2luc3RhbmNlKGNvbnN0LCB0eXBlcy5Db2RlVHlwZSk6CiAgICAgICAg",
    "ICAgIHlpZWxkIGZyb20gX3dhbGtfY29kZShjb25zdCkKCgpkZWYgX2NoZWNrX2NvZGVfb2JqZWN0KGZuOiB0eXBlcy5GdW5j",
    "dGlvblR5cGUsIHNyYzogT3B0aW9uYWxbc3RyXSwgZ292ZXJuZWQ6IHNldFtzdHJdLAogICAgICAgICAgICAgICAgICAgICAg",
    "IGF0dHJzOiBmcm96ZW5zZXQsIHJvbGU6IHN0cikgLT4gbGlzdFtkaWN0XToKICAgICIiIlZhbGlkYXRlIHdoYXQgdGhlIENP",
    "TVBJTEVSIHJlY29yZGVkLCBub3Qgd2hhdCB0aGUgc291cmNlIHNheXMuIGNvX25hbWVzIGhvbGRzIGdsb2JhbCByZWFkcwog",
    "ICAgQU5EIGF0dHJpYnV0ZSBuYW1lczsgaWYgdGhlIHNvdXJjZSBhbmQgdGhlIGNvZGUgb2JqZWN0IGRpc2FncmVlIChhIHN3",
    "YXBwZWQgX19jb2RlX18sIGEgc3RhbGUKICAgIC5weWMsIGEgZGVjb3JhdG9yKSwgdGhpcyBsYXllciBmaXJlcyB3aGVyZSBM",
    "MiBjYW5ub3QuIiIiCiAgICBwcm9ibGVtczogbGlzdFtkaWN0XSA9IFtdCiAgICB0b3AgPSBmbi5fX2NvZGVfXwogICAgZm9y",
    "IGNvZGUgaW4gX3dhbGtfY29kZSh0b3ApOgogICAgICAgIGlzX3RvcCA9IGNvZGUgaXMgdG9wCiAgICAgICAgZm9yIG5hbWUg",
    "aW4gY29kZS5jb19uYW1lczoKICAgICAgICAgICAgaWYgbmFtZSBpbiBnb3Zlcm5lZCBvciBuYW1lIGluIGNvZGUuY29fdmFy",
    "bmFtZXMgb3IgbmFtZSBpbiBhdHRyczoKICAgICAgICAgICAgICAgIGNvbnRpbnVlCiAgICAgICAgICAgIGlmIG5hbWUuc3Rh",
    "cnRzd2l0aCgiXyIpOgogICAgICAgICAgICAgICAgcHJvYmxlbXMuYXBwZW5kKF9wcm9ibGVtKCJEVU5ERVJfQVRUUklCVVRF",
    "X0FDQ0VTUyIsIG5hbWUsCiAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgZiJjb19uYW1lcyBvZiB7",
    "Y29kZS5jb19uYW1lIXJ9IGNvbnRhaW5zIHRoZSBwcml2YXRlIG5hbWUgIgogICAgICAgICAgICAgICAgICAgICAgICAgICAg",
    "ICAgICAgICAgICAgIGYie25hbWUhcn07IFJFRlVTRUQiKSkKICAgICAgICAgICAgICAgIGNvbnRpbnVlCiAgICAgICAgICAg",
    "IHByb2JsZW1zLmFwcGVuZChfcHJvYmxlbSgKICAgICAgICAgICAgICAgIF9OQU1FRF9DSEFOTkVMLmdldChuYW1lLCAiQ09E",
    "RV9OQU1FX1VOR09WRVJORUQiKSwgbmFtZSwKICAgICAgICAgICAgICAgIGYidGhlIGNvbXBpbGVkIGNvZGUgb2JqZWN0IHtj",
    "b2RlLmNvX25hbWUhcn0gcmVmZXJlbmNlcyB0aGUgdW5nb3Zlcm5lZCBuYW1lICIKICAgICAgICAgICAgICAgIGYie25hbWUh",
    "cn0gaW4gY29fbmFtZXMuIFRoaXMgbGF5ZXIgcmVhZHMgdGhlIENPREUsIG5vdCB0aGUgc291cmNlLCBzbyBpdCBob2xkcyAi",
    "CiAgICAgICAgICAgICAgICAiZXZlbiBpZiB0aGUgc291cmNlIHRleHQgd2FzIHJlcGxhY2VkLCB0aGUgLnB5YyBpcyBzdGFs",
    "ZSwgb3IgX19jb2RlX18gd2FzICIKICAgICAgICAgICAgICAgICJyZWFzc2lnbmVkIGFmdGVyIGRlZmluaXRpb247IFJFRlVT",
    "RUQiKSkKICAgICAgICBpZiBpc190b3AgYW5kIGNvZGUuY29fZnJlZXZhcnM6CiAgICAgICAgICAgIHByb2JsZW1zLmFwcGVu",
    "ZChfcHJvYmxlbSgiQ0xPU1VSRV9DQVBUVVJFIiwgIiwiLmpvaW4oY29kZS5jb19mcmVldmFycyksCiAgICAgICAgICAgICAg",
    "ICAgICAgICAgICAgICAgICAgICAgICAidGhlIGNvbXBpbGVkIHdpdG5lc3MgaGFzIGZyZWUgdmFyaWFibGVzOyBSRUZVU0VE",
    "IikpCiAgICAgICAgaWYgbm90IGlzX3RvcDoKICAgICAgICAgICAgZW5jbG9zaW5nID0gc2V0KHRvcC5jb192YXJuYW1lcykg",
    "fCBzZXQodG9wLmNvX2NlbGx2YXJzKQogICAgICAgICAgICBlc2NhcGVkID0gc2V0KGNvZGUuY29fZnJlZXZhcnMpIC0gZW5j",
    "bG9zaW5nCiAgICAgICAgICAgIGlmIGVzY2FwZWQ6CiAgICAgICAgICAgICAgICBwcm9ibGVtcy5hcHBlbmQoX3Byb2JsZW0o",
    "CiAgICAgICAgICAgICAgICAgICAgIkNMT1NVUkVfQ0FQVFVSRSIsICIsIi5qb2luKHNvcnRlZChlc2NhcGVkKSksCiAgICAg",
    "ICAgICAgICAgICAgICAgZiJuZXN0ZWQgY29kZSBvYmplY3Qge2NvZGUuY29fbmFtZSFyfSBjYXB0dXJlcyB7c29ydGVkKGVz",
    "Y2FwZWQpfSwgd2hpY2ggYXJlICIKICAgICAgICAgICAgICAgICAgICAibm90IGxvY2FscyBvZiB0aGUgd2l0bmVzczsgUkVG",
    "VVNFRCIpKQogICAgaWYgc3JjIGlzIG5vdCBOb25lOgogICAgICAgIHByb2JsZW1zICs9IF9jaGVja19zb3VyY2VfYWdyZWVt",
    "ZW50KGZuLCBzcmMsIHJvbGUpCiAgICByZXR1cm4gcHJvYmxlbXMKCgpfQ09ERV9GQUNFVFMgPSAoImNvX2FyZ2NvdW50Iiwg",
    "ImNvX2t3b25seWFyZ2NvdW50IiwgImNvX3Bvc29ubHlhcmdjb3VudCIsICJjb19ubG9jYWxzIiwKICAgICAgICAgICAgICAg",
    "ICJjb19uYW1lIikKCiMgQ09fT1BUSU1JWkVEfENPX05FV0xPQ0FMU3xDT19WQVJBUkdTfENPX1ZBUktFWVdPUkRTfENPX05F",
    "U1RFRHxDT19HRU5FUkFUT1J8Q09fQ09ST1VUSU5FfAojIENPX0FTWU5DX0dFTkVSQVRPUi4gRXZlcnl0aGluZyBhYm92ZSB0",
    "aGlzIG1hc2sgaXMgYSBfX2Z1dHVyZV9fIC8gY29tcGlsZXIgZmxhZywgd2hpY2ggc2F5cwojIG5vdGhpbmcgYWJvdXQgd2hh",
    "dCB0aGUgY29kZSBET0VTLiBDb21wYXJpbmcgcmF3IGNvX2ZsYWdzIHJlZnVzZWQgZXZlcnkgaG9uZXN0IHdpdG5lc3MgZGVm",
    "aW5lZCBpbgojIGEgbW9kdWxlIHdpdGggYSBkaWZmZXJlbnQgYGZyb20gX19mdXR1cmVfXyBpbXBvcnQgLi4uYCBsaW5lIHRo",
    "YW4gdGhlIHZhbGlkYXRvcidzIG93biDigJQgdGhlIHNlY29uZAojIHNlbGYtaW5mbGljdGVkIGZhbHNlIHJlZnVzYWwgdGhp",
    "cyBsYXllciBwcm9kdWNlZCwgYW5kIHRoZSByZWFzb24gdGhlIGJhdHRlcnkgbm93IGNhcnJpZXMgYQojIHBvc2l0aXZlIGNv",
    "bnRyb2wgY29tcGlsZWQgdW5kZXIgRElGRkVSRU5UIGZsYWdzIChzZWUgcDZfcHVyaXR5X2FsdF9mbGFncy5weSkuCl9TRU1B",
    "TlRJQ19GTEFHX01BU0sgPSAweDJCRgoKCmRlZiBfaW5zdHJ1Y3Rpb25fc3RyZWFtKGNvZGU6IHR5cGVzLkNvZGVUeXBlKSAt",
    "PiBsaXN0OgogICAgIiIiU1lNQk9MSUMgKG9wbmFtZSwgYXJndmFsKSBzdHJlYW0gZm9yIGV2ZXJ5IHJlYWwgaW5zdHJ1Y3Rp",
    "b24sIHJlY3Vyc2luZyBpbnRvIG5lc3RlZCBjb2RlLgoKICAgIE5PVCByYXcgYGNvX2NvZGVgOiBvbiBDUHl0aG9uIDMuMTEr",
    "IHRoZSBpbmxpbmUgQ0FDSEUgZW50cmllcyBvZiBhbiBleGVjdXRlZCBmdW5jdGlvbiBjYXJyeQogICAgYWRhcHRpdmUtc3Bl",
    "Y2lhbGlzYXRpb24gY291bnRlcnMsIHNvIHR3byBieXRlLWlkZW50aWNhbCBmdW5jdGlvbnMgZGlmZmVyIGluIGNvX2NvZGUg",
    "cHVyZWx5CiAgICBiZWNhdXNlIG9uZSBvZiB0aGVtIGhhcyBiZWVuIGNhbGxlZC4gQ29tcGFyaW5nIHJhdyBieXRlcyBtYWRl",
    "IHRoaXMgbGF5ZXIgRklSRSBPTiBJVFMgT1dOCiAgICBIT05FU1QgUFJJTUlUSVZFIOKAlCBhIGZhbHNlIHJlZnVzYWwgdGhh",
    "dCB3b3VsZCBoYXZlIG1hZGUgdGhlIHdob2xlIGxheWVyIHVudHJ1c3R3b3J0aHkuIFRoZQogICAgaW5zdHJ1Y3Rpb24gc3Ry",
    "ZWFtIGlzIHRoZSBzZW1hbnRpYyBjb250ZW50IGFuZCBpcyBzdGFibGUgdW5kZXIgd2FybS11cC4KICAgICIiIgogICAgb3V0",
    "OiBsaXN0ID0gW10KICAgIGZvciBpbnN0ciBpbiBkaXMuZ2V0X2luc3RydWN0aW9ucyhjb2RlKToKICAgICAgICBpZiBpbnN0",
    "ci5vcG5hbWUgPT0gIkNBQ0hFIjoKICAgICAgICAgICAgY29udGludWUKICAgICAgICBpZiBpbnN0ci5vcGNvZGUgaW4gZGlz",
    "Lmhhc2pyZWwgb3IgaW5zdHIub3Bjb2RlIGluIGRpcy5oYXNqYWJzOgogICAgICAgICAgICBvdXQuYXBwZW5kKChpbnN0ci5v",
    "cG5hbWUsIE5vbmUpKSAgICAgICAgICAgICMgYnl0ZSBvZmZzZXRzLCBub3QgY29udGVudAogICAgICAgICAgICBjb250aW51",
    "ZQogICAgICAgIGFyZyA9IGluc3RyLmFyZ3ZhbAogICAgICAgIGlmIGlzaW5zdGFuY2UoYXJnLCB0eXBlcy5Db2RlVHlwZSk6",
    "CiAgICAgICAgICAgIGFyZyA9IGYiPGNvZGU6e2FyZy5jb19uYW1lfT4iCiAgICAgICAgZWxpZiBpbnN0ci5hcmcgaXMgbm90",
    "IE5vbmUgYW5kIG5vdCBpc2luc3RhbmNlKGFyZywgKHN0ciwgaW50LCBmbG9hdCwgYm9vbCwgdHlwZShOb25lKSwKICAgICAg",
    "ICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgYnl0ZXMsIGZyb3plbnNldCwg",
    "dHVwbGUpKToKICAgICAgICAgICAgYXJnID0gcmVwcihhcmcpCiAgICAgICAgb3V0LmFwcGVuZCgoaW5zdHIub3BuYW1lLCBy",
    "ZXByKGFyZykpKQogICAgZm9yIGNvbnN0IGluIGNvZGUuY29fY29uc3RzOgogICAgICAgIGlmIGlzaW5zdGFuY2UoY29uc3Qs",
    "IHR5cGVzLkNvZGVUeXBlKToKICAgICAgICAgICAgb3V0LmFwcGVuZCgoIjxuZXN0ZWQ+IiwgY29uc3QuY29fbmFtZSkpCiAg",
    "ICAgICAgICAgIG91dC5leHRlbmQoX2luc3RydWN0aW9uX3N0cmVhbShjb25zdCkpCiAgICByZXR1cm4gb3V0CgoKZGVmIF9j",
    "aGVja19zb3VyY2VfYWdyZWVtZW50KGZuOiB0eXBlcy5GdW5jdGlvblR5cGUsIHNyYzogc3RyLCByb2xlOiBzdHIpIC0+IGxp",
    "c3RbZGljdF06CiAgICAiIiJSZWNvbXBpbGUgdGhlIHJlY292ZXJlZCBzb3VyY2UgYW5kIHJlcXVpcmUgdGhlIHJlc3VsdGlu",
    "ZyBjb2RlIG9iamVjdCB0byBhZ3JlZSB3aXRoIHRoZSBsaXZlCiAgICBvbmUuIENhdGNoZXMgYGZuLl9fY29kZV9fID0gb3Ro",
    "ZXJfY29kZWAgcGVyZm9ybWVkIGFmdGVyIGRlZmluaXRpb24g4oCUIGFuIGF0dGFjayB0aGF0IGxlYXZlcyB0aGUKICAgIHNv",
    "dXJjZSAoYW5kIHRoZXJlZm9yZSBMMikgcGVyZmVjdGx5IGNsZWFuLiIiIgogICAgbnM6IGRpY3QgPSB7fQogICAgdHJ5Ogog",
    "ICAgICAgICMgZG9udF9pbmhlcml0PVRydWU6IGNvbXBpbGUoKSBvdGhlcndpc2UgaW5oZXJpdHMgdGhlIENBTExJTkcgZnJh",
    "bWUncyBfX2Z1dHVyZV9fIGZsYWdzLAogICAgICAgICMgc28gdGhpcyBtb2R1bGUncyBvd24gYGZyb20gX19mdXR1cmVfXyBp",
    "bXBvcnQgYW5ub3RhdGlvbnNgIHdvdWxkIGxlYWsgaW50byBldmVyeQogICAgICAgICMgcmVjb21waWxhdGlvbiBhbmQgbWFr",
    "ZSB0aGUgY29tcGFyaXNvbiBhIHByb3BlcnR5IG9mIHRoZSB2YWxpZGF0b3IgcmF0aGVyIHRoYW4gb2YgdGhlCiAgICAgICAg",
    "IyB3aXRuZXNzLgogICAgICAgIGV4ZWMoY29tcGlsZShzcmMsICI8cndjYy1yZWNvbXBpbGU+IiwgImV4ZWMiLCBkb250X2lu",
    "aGVyaXQ9VHJ1ZSksICAjIG5vcWE6IFMxMDIKICAgICAgICAgICAgIHsiX19idWlsdGluc19fIjoge319LCBucykKICAgIGV4",
    "Y2VwdCBFeGNlcHRpb24gYXMgZXhjOgogICAgICAgIHJldHVybiBbX3Byb2JsZW0oIlNPVVJDRV9SRUNPTVBJTEVfRkFJTEVE",
    "IiwgTm9uZSwKICAgICAgICAgICAgICAgICAgICAgICAgIGYidGhlIHJlY292ZXJlZCBzb3VyY2UgY291bGQgbm90IGJlIHJl",
    "Y29tcGlsZWQgaW4gaXNvbGF0aW9uICIKICAgICAgICAgICAgICAgICAgICAgICAgIGYiKHt0eXBlKGV4YykuX19uYW1lX199",
    "OiB7ZXhjfSk7IFJFRlVTRUQiKV0KICAgIGZyZXNoID0gbnMuZ2V0KGZuLl9fY29kZV9fLmNvX25hbWUpIG9yIG5zLmdldChm",
    "bi5fX25hbWVfXykKICAgIGlmIG5vdCBpc2luc3RhbmNlKGZyZXNoLCB0eXBlcy5GdW5jdGlvblR5cGUpOgogICAgICAgIHJl",
    "dHVybiBbX3Byb2JsZW0oIlNPVVJDRV9SRUNPTVBJTEVfRkFJTEVEIiwgTm9uZSwKICAgICAgICAgICAgICAgICAgICAgICAg",
    "ICJyZWNvbXBpbGluZyB0aGUgc291cmNlIGRpZCBub3QgeWllbGQgYSBmdW5jdGlvbiBvZiB0aGUgc2FtZSBuYW1lOyAiCiAg",
    "ICAgICAgICAgICAgICAgICAgICAgICAiUkVGVVNFRCIpXQogICAgYSwgYiA9IGZuLl9fY29kZV9fLCBmcmVzaC5fX2NvZGVf",
    "XwogICAgZGlmZnMgPSBbZiBmb3IgZiBpbiBfQ09ERV9GQUNFVFMgaWYgZ2V0YXR0cihhLCBmKSAhPSBnZXRhdHRyKGIsIGYp",
    "XQogICAgaWYgKGEuY29fZmxhZ3MgJiBfU0VNQU5USUNfRkxBR19NQVNLKSAhPSAoYi5jb19mbGFncyAmIF9TRU1BTlRJQ19G",
    "TEFHX01BU0spOgogICAgICAgIGRpZmZzLmFwcGVuZCgiY29fZmxhZ3MiKQogICAgZm9yIHBhaXIsIGxhYmVsIGluICgoKGEu",
    "Y29fbmFtZXMsIGIuY29fbmFtZXMpLCAiY29fbmFtZXMiKSwKICAgICAgICAgICAgICAgICAgICAgICAgKChhLmNvX3Zhcm5h",
    "bWVzLCBiLmNvX3Zhcm5hbWVzKSwgImNvX3Zhcm5hbWVzIiksCiAgICAgICAgICAgICAgICAgICAgICAgICgoYS5jb19mcmVl",
    "dmFycywgYi5jb19mcmVldmFycyksICJjb19mcmVldmFycyIpKToKICAgICAgICBpZiBzZXQocGFpclswXSkgIT0gc2V0KHBh",
    "aXJbMV0pOgogICAgICAgICAgICBkaWZmcy5hcHBlbmQobGFiZWwpCiAgICBzY2FsID0gbGFtYmRhIGM6IHNvcnRlZCggICMg",
    "bm9xYTogRTczMQogICAgICAgIHJlcHIoaykgZm9yIGsgaW4gYy5jb19jb25zdHMgaWYgbm90IGlzaW5zdGFuY2UoaywgdHlw",
    "ZXMuQ29kZVR5cGUpKQogICAgaWYgc2NhbChhKSAhPSBzY2FsKGIpOgogICAgICAgIGRpZmZzLmFwcGVuZCgiY29fY29uc3Rz",
    "IikKICAgIGlmIF9pbnN0cnVjdGlvbl9zdHJlYW0oYSkgIT0gX2luc3RydWN0aW9uX3N0cmVhbShiKToKICAgICAgICBkaWZm",
    "cy5hcHBlbmQoImluc3RydWN0aW9uX3N0cmVhbSIpCiAgICBpZiBkaWZmczoKICAgICAgICByZXR1cm4gW19wcm9ibGVtKAog",
    "ICAgICAgICAgICAiU09VUkNFX0NPREVfTUlTTUFUQ0giLCAiLCIuam9pbihkaWZmcyksCiAgICAgICAgICAgIGYidGhlIGxp",
    "dmUgY29kZSBvYmplY3Qgb2YgdGhlIHtyb2xlfSBkaXNhZ3JlZXMgd2l0aCBhIHJlY29tcGlsYXRpb24gb2YgaXRzIG93biBz",
    "b3VyY2UgIgogICAgICAgICAgICBmImluIHtkaWZmc30uIFRoZSBBU1QgbGF5ZXIgdmFsaWRhdGVkIFRFWFQ7IHRoaXMgcHJv",
    "dmVzIHRoZSB0ZXh0IGRlc2NyaWJlcyB0aGUgY29kZSAiCiAgICAgICAgICAgICJ0aGF0IHdpbGwgYWN0dWFsbHkgcnVuLiBB",
    "IHBvc3QtZGVmaW5pdGlvbiBgX19jb2RlX19gIHN3YXAgZmFpbHMgZXhhY3RseSBoZXJlOyAiCiAgICAgICAgICAgICJSRUZV",
    "U0VEIildCiAgICByZXR1cm4gW10KCgojID09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09",
    "PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PQojIFRIRSBFTlRSWSBQT0lOVAojID09",
    "PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09",
    "PT09PT09PT09PT09PT09PT09PT09PQpkZWYgZ292ZXJuZWRfbmFtZXMoZXh0cmE6IE9wdGlvbmFsW3NldFtzdHJdXSA9IE5v",
    "bmUsIHNraXA6IE9wdGlvbmFsW3N0cl0gPSBOb25lKSAtPiBzZXRbc3RyXToKICAgIG5hbWVzID0gc2V0KEFMTE9XRURfQlVJ",
    "TFRJTlMpIHwge24gZm9yIG4gaW4gUFJJTUlUSVZFUyBpZiBuICE9IHNraXB9CiAgICByZXR1cm4gbmFtZXMgfCBzZXQoZXh0",
    "cmEgb3IgKCkpCgoKZGVmIHZlcmlmeV93aXRuZXNzX3B1cml0eShmbjogQW55LCAqLCByb2xlOiBzdHIgPSAid2l0bmVzcyIs",
    "CiAgICAgICAgICAgICAgICAgICAgICAgICAgZXh0cmFfbmFtZXM6IE9wdGlvbmFsW3NldFtzdHJdXSA9IE5vbmUsCiAgICAg",
    "ICAgICAgICAgICAgICAgICAgICAgZXh0cmFfYXR0cnM6IE9wdGlvbmFsW3NldFtzdHJdXSA9IE5vbmUsCiAgICAgICAgICAg",
    "ICAgICAgICAgICAgICAgX3NraXBfcmVnaXN0cnk6IE9wdGlvbmFsW3N0cl0gPSBOb25lKSAtPiBsaXN0W2RpY3RdOgogICAg",
    "IiIiUmV0dXJuIHRoZSBwcm9ibGVtcyB0aGF0IG1ha2UgYGZuYCB1bmZpdCB0byBiZSBleGVjdXRlZCBhcyBhIHdpdG5lc3Mu",
    "IFtdIG1lYW5zIHRoZSBjYWxsYWJsZQogICAgaXMgaW5zaWRlIHtDT05UUkFDVF9JRH0gYW5kIGl0cyBvYnNlcnZhdGlvbiBp",
    "cyBhIGZ1bmN0aW9uIG9mIGl0cyBpbmplY3RlZCBpbnB1dHMgYW5kIHRoZQogICAgZ292ZXJuZWQgcHJpbWl0aXZlcyBBTE9O",
    "RS4gTmV2ZXIgcmFpc2VzLiIiIgogICAgZ292ZXJuZWQgPSBnb3Zlcm5lZF9uYW1lcyhleHRyYV9uYW1lcywgX3NraXBfcmVn",
    "aXN0cnkpCiAgICBhdHRycyA9IEFMTE9XRURfQVRUUklCVVRFUyB8IGZyb3plbnNldChleHRyYV9hdHRycyBvciAoKSkKICAg",
    "IHNoYXBlLCBmZGVmLCBzcmMgPSBfY2hlY2tfc2hhcGUoZm4sIHJvbGUpCiAgICBwcm9ibGVtcyA9IF90YWcoc2hhcGUsICJM",
    "MSIpCiAgICBpZiBmZGVmIGlzIG5vdCBOb25lOgogICAgICAgIHZpcyA9IF9QdXJpdHlWaXNpdG9yKGdvdmVybmVkLCBhdHRy",
    "cykKICAgICAgICB2aXMuY29sbGVjdF9iaW5kaW5ncyhmZGVmKQogICAgICAgIGZvciBzdG10IGluIGZkZWYuYm9keToKICAg",
    "ICAgICAgICAgdmlzLnZpc2l0KHN0bXQpCiAgICAgICAgdmlzLnZpc2l0KGZkZWYuYXJncykKICAgICAgICBwcm9ibGVtcyAr",
    "PSBfdGFnKHZpcy5wcm9ibGVtcywgIkwyIikKICAgIGlmIGlzaW5zdGFuY2UoZm4sIHR5cGVzLkZ1bmN0aW9uVHlwZSk6CiAg",
    "ICAgICAgcHJvYmxlbXMgKz0gX3RhZyhfY2hlY2tfY29kZV9vYmplY3QoZm4sIHNyYywgZ292ZXJuZWQsIGF0dHJzLCByb2xl",
    "KSwgIkwzIikKICAgICMgRGUtZHVwbGljYXRlIFdJVEhJTiBhIGxheWVyIG9ubHkuIEwyIGFuZCBMMyBsZWdpdGltYXRlbHkg",
    "cmVwb3J0IHRoZSBzYW1lIGNoYW5uZWwgZnJvbSB0d28KICAgICMgaW5kZXBlbmRlbnQgZGlyZWN0aW9ucywgYW5kIGNvbGxh",
    "cHNpbmcgdGhlbSBhY3Jvc3MgbGF5ZXJzIHdvdWxkIG1ha2UgdGhlIGtpbGwtcHJvb2YgcmVhZCBhCiAgICAjIGxheWVyIGFz",
    "IHVud2l0bmVzc2VkIHdoZW4gaXQgaXMgaW4gZmFjdCB0aGUgc29sZSByZW1haW5pbmcgd2l0bmVzcyBmb3IgdGhhdCBjaGFu",
    "bmVsLgogICAgc2Vlbiwgb3V0ID0gc2V0KCksIFtdCiAgICBmb3IgcCBpbiBwcm9ibGVtczoKICAgICAgICBrZXkgPSAocFsi",
    "a2luZCJdLCBwWyJjaGFubmVsIl0sIHBbImxheWVyIl0pCiAgICAgICAgaWYga2V5IGluIHNlZW4gb3IgcFsibGF5ZXIiXSBp",
    "biBESVNBQkxFRF9MQVlFUlM6CiAgICAgICAgICAgIGNvbnRpbnVlCiAgICAgICAgc2Vlbi5hZGQoa2V5KQogICAgICAgIG91",
    "dC5hcHBlbmQocCkKICAgIHJldHVybiBvdXQKCgojID09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09",
    "PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PQojIEw0IOKAlCBTVFJVQ1RV",
    "UkFMIFJFQklORElORwojID09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09",
    "PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PQpfUkVTVFJJQ1RFRF9CVUlMVElOUyA9IHtuOiBnZXRh",
    "dHRyKF9idWlsdGlucywgbikgZm9yIG4gaW4gc29ydGVkKEFMTE9XRURfQlVJTFRJTlMpCiAgICAgICAgICAgICAgICAgICAg",
    "ICAgIGlmIGhhc2F0dHIoX2J1aWx0aW5zLCBuKX0KX1JFU1RSSUNURURfQlVJTFRJTlMudXBkYXRlKHsiVHJ1ZSI6IFRydWUs",
    "ICJGYWxzZSI6IEZhbHNlLCAiTm9uZSI6IE5vbmV9KQoKCl9CSU5EX0NBQ0hFOiBkaWN0W3R1cGxlLCBkaWN0XSA9IHt9CgoK",
    "ZGVmIF9iaW5kX2FsbCgpIC0+IGRpY3Q6CiAgICAiIiJCaW5kIGV2ZXJ5IGdvdmVybmVkIHByaW1pdGl2ZSBpbnRvIElUUyBP",
    "V04gcmVzdHJpY3RlZCBuYW1lc3BhY2UsIGluIHR3byBwYXNzZXMgc28gdGhhdAogICAgbXV0dWFsbHktcmVmZXJyaW5nIHBy",
    "aW1pdGl2ZXMgYXJlIGV4cHJlc3NpYmxlIHdpdGhvdXQgaW5maW5pdGUgcmVjdXJzaW9uLiBBIHByaW1pdGl2ZSBzZWVzOgog",
    "ICAgdGhlIGFsbG93bGlzdGVkIGJ1aWx0aW5zLCBpdHMgb3duIERFQ0xBUkVEIGRldGVybWluaXN0aWMgZGVwcywgYW5kIHRo",
    "ZSBvdGhlciBwcmltaXRpdmVzIOKAlAogICAgYW5kIG5vdGhpbmcgZWxzZS4gVGhhdCBpcyB3aGF0IGNsb3NlcyB0aGUgSU5E",
    "SVJFQ1QtSEVMUEVSLUdMT0JBTCBjaGFubmVsIHRvIHRoZSBzYW1lIGRlcHRoIGFzCiAgICBhIGRpcmVjdCBvbmU6IGRlbGVn",
    "YXRpbmcgdG8gYSBoZWxwZXIgZG9lcyBub3QgYnV5IHRoZSB3aXRuZXNzIGEgd2lkZXIgbmFtZXNwYWNlLiIiIgogICAga2V5",
    "ID0gdHVwbGUoc29ydGVkKChuLCBwLnNoYTI1NikgZm9yIG4sIHAgaW4gUFJJTUlUSVZFUy5pdGVtcygpKSkKICAgIGNhY2hl",
    "ZCA9IF9CSU5EX0NBQ0hFLmdldChrZXkpCiAgICBpZiBjYWNoZWQgaXMgbm90IE5vbmU6CiAgICAgICAgcmV0dXJuIGNhY2hl",
    "ZAogICAgZW52czogZGljdFtzdHIsIGRpY3RdID0ge30KICAgIG5zOiBkaWN0W3N0ciwgQ2FsbGFibGVdID0ge30KICAgIGZv",
    "ciBuYW1lLCBwcmltIGluIFBSSU1JVElWRVMuaXRlbXMoKToKICAgICAgICBnOiBkaWN0ID0geyJfX2J1aWx0aW5zX18iOiBk",
    "aWN0KF9SRVNUUklDVEVEX0JVSUxUSU5TKX0KICAgICAgICBnLnVwZGF0ZShwcmltLmRlcHMpCiAgICAgICAgZW52c1tuYW1l",
    "XSA9IGcKICAgICAgICBwcmltLmJvdW5kID0gdHlwZXMuRnVuY3Rpb25UeXBlKHByaW0uZm4uX19jb2RlX18sIGcsIG5hbWUs",
    "IE5vbmUsIE5vbmUpCiAgICAgICAgbnNbbmFtZV0gPSBwcmltLmJvdW5kCiAgICBmb3IgbmFtZSBpbiBQUklNSVRJVkVTOgog",
    "ICAgICAgIGZvciBvdGhlciwgYm91bmQgaW4gbnMuaXRlbXMoKToKICAgICAgICAgICAgaWYgb3RoZXIgIT0gbmFtZToKICAg",
    "ICAgICAgICAgICAgIGVudnNbbmFtZV1bb3RoZXJdID0gYm91bmQKICAgIF9CSU5EX0NBQ0hFW2tleV0gPSBucwogICAgcmV0",
    "dXJuIG5zCgoKZGVmIHJlc3RyaWN0ZWRfZ2xvYmFscygpIC0+IGRpY3Q6CiAgICAiIiJUaGUgT05MWSBuYW1lc3BhY2UgYSB2",
    "YWxpZGF0ZWQgd2l0bmVzcyBldmVyIGV4ZWN1dGVzIGluLiBBIG1vZHVsZSBnbG9iYWwsIGFuIGltcG9ydCwKICAgIG9wZW4o",
    "KSwgZXZhbCgpLCBnZXRhdHRyKCkgYW5kIF9faW1wb3J0X18gYXJlIGFic2VudCDigJQgc28gdGhleSByYWlzZSBOYW1lRXJy",
    "b3IvSW1wb3J0RXJyb3IKICAgIHJhdGhlciB0aGFuIHJlYWNoaW5nIGFtYmllbnQgc3RhdGUuIFRoaXMgaXMgdGhlIHN0cnVj",
    "dHVyYWwgaGFsZiBvZiB0aGUgY2xvc3VyZTogTDIgc3RhdGVzIHRoZQogICAgcnVsZSwgTDQgcmVtb3ZlcyB0aGUgY2FwYWJp",
    "bGl0eS4iIiIKICAgIGc6IGRpY3QgPSB7Il9fYnVpbHRpbnNfXyI6IGRpY3QoX1JFU1RSSUNURURfQlVJTFRJTlMpfQogICAg",
    "Zy51cGRhdGUoX2JpbmRfYWxsKCkpCiAgICByZXR1cm4gZwoKCmRlZiByZXN0cmljdChmbjogdHlwZXMuRnVuY3Rpb25UeXBl",
    "LCAqLCB2YWxpZGF0ZTogYm9vbCA9IFRydWUsCiAgICAgICAgICAgICByb2xlOiBzdHIgPSAid2l0bmVzcyIpIC0+IHR5cGVz",
    "LkZ1bmN0aW9uVHlwZToKICAgICIiIlJlLWJpbmQgYSBWQUxJREFURUQgd2l0bmVzcyBpbnRvIHRoZSByZXN0cmljdGVkIG5h",
    "bWVzcGFjZS4gUmFpc2VzIFB1cml0eUVycm9yIGlmIHRoZQogICAgY2FsbGFibGUgZGlkIG5vdCB2YWxpZGF0ZSDigJQgYSB3",
    "aXRuZXNzIGlzIG5ldmVyIGV4ZWN1dGVkIG9uIHRoZSBzdHJlbmd0aCBvZiB0aGUgY2FsbGVyJ3Mgd29yZC4KICAgICIiIgog",
    "ICAgaWYgdmFsaWRhdGU6CiAgICAgICAgcHJvYmxlbXMgPSB2ZXJpZnlfd2l0bmVzc19wdXJpdHkoZm4sIHJvbGU9cm9sZSkK",
    "ICAgICAgICBpZiBwcm9ibGVtczoKICAgICAgICAgICAgcmFpc2UgUHVyaXR5RXJyb3IoanNvbi5kdW1wcyhwcm9ibGVtcywg",
    "aW5kZW50PTEpKQogICAgcmV0dXJuIHR5cGVzLkZ1bmN0aW9uVHlwZShmbi5fX2NvZGVfXywgcmVzdHJpY3RlZF9nbG9iYWxz",
    "KCksCiAgICAgICAgICAgICAgICAgICAgICAgICAgICAgIGZuLl9fbmFtZV9fLCBOb25lLCBOb25lKQoKCmRlZiBnb3Zlcm5l",
    "ZF9jYWxsKGZuOiB0eXBlcy5GdW5jdGlvblR5cGUsIHNwZWM6IEFueSwgKiwgcm9sZTogc3RyID0gIndpdG5lc3MiKSAtPiBB",
    "bnk6CiAgICAiIiJWYWxpZGF0ZSwgcmUtYmluZCwgdGhlbiBpbnZva2UuIFRoZSBzaW5nbGUgYWRtaXR0ZWQgd2F5IHRvIHJ1",
    "biBhIHdpdG5lc3MuIiIiCiAgICByZXR1cm4gcmVzdHJpY3QoZm4sIHJvbGU9cm9sZSkoc3BlYykKCgojID09PT09PT09PT09",
    "PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09",
    "PT09PT09PT09PT09PQojIERFRkFVTFQgR09WRVJORUQgUFJJTUlUSVZFUwojID09PT09PT09PT09PT09PT09PT09PT09PT09",
    "PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PQoj",
    "IFRoZSBtaW5pbXVtIHN1cmZhY2UgYW4gaG9uZXN0IHdpdG5lc3MgbmVlZHMuIGB3aXRuZXNzX2lucHV0YCBpcyB0aGUgT05M",
    "WSBhZG1pdHRlZCB3YXkgdG8gcmVhY2ggYW4KIyBpbmplY3RlZCBjaGFubmVsOyBgZGlnZXN0YCBpcyB0aGUgb25seSBhZG1p",
    "dHRlZCBzdW1tYXJpc2F0aW9uLiBCb3RoIGFyZSB2YWxpZGF0ZWQgdW5kZXIgdGhlIHNhbWUKIyBjb250cmFjdCBhbmQgcmUt",
    "Ym91bmQgaW50byB0aGVpciBvd24gcmVzdHJpY3RlZCBnbG9iYWxzLCBzbyB0aGUgImluZGlyZWN0IGhlbHBlciBnbG9iYWwi",
    "IGNoYW5uZWwKIyBpcyBjbG9zZWQgdG8gdGhlIHNhbWUgZGVwdGggYXMgYSBkaXJlY3QgZ2xvYmFsIHJlYWQuCmRlZiB3aXRu",
    "ZXNzX2lucHV0KHNwZWMsIGNoYW5uZWxfaWQpOgogICAgIiIiVGhlIGdvdmVybmVkIGFjY2Vzc29yIGZvciBvbmUgSU5KRUNU",
    "RUQgY2hhbm5lbC4gYGNoYW5uZWxfaWRgIG11c3QgYmUgYSBsaXRlcmFsIGluIHRoZQogICAgd2l0bmVzcyAob3IgYSB2YWx1",
    "ZSBkZXJpdmVkIGZyb20gdGhlIHNwZWMpIOKAlCBhIG1vZHVsZS1sZXZlbCBjb25zdGFudCBuYW1pbmcgdGhlIGNoYW5uZWwg",
    "aXMKICAgIGl0c2VsZiB1bmdvdmVybmVkIG11dGFibGUgc3RhdGUsIHdoaWNoIGlzIHdoeSB0aGUgaG9uZXN0IHdpdG5lc3Nl",
    "cyBhcmUgd3JpdHRlbiB3aXRoIGxpdGVyYWxzLiIiIgogICAgcmV0dXJuIHNwZWNbIl93aXRuZXNzX2lucHV0cyJdW2NoYW5u",
    "ZWxfaWRdCgoKZGVmIGRpZ2VzdChtZW1iZXJzKToKICAgICIiIkRldGVybWluaXN0aWMgb3JkZXItaW5kZXBlbmRlbnQgc3Vt",
    "bWFyeS4ganNvbi9oYXNobGliIGFyZSBkZWNsYXJlZCBkZXBzLCBzbyB0aGlzIHByaW1pdGl2ZSdzCiAgICBvd24gZ2xvYmFs",
    "cyBhcmUgZ292ZXJuZWQgdG9vLiIiIgogICAgcmV0dXJuIGhhc2hsaWIuc2hhMjU2KAogICAgICAgIGpzb24uZHVtcHMoc29y",
    "dGVkKHN0cihtKSBmb3IgbSBpbiBtZW1iZXJzKSwgc2VwYXJhdG9ycz0oIiwiLCAiOiIpKS5lbmNvZGUoKQogICAgKS5oZXhk",
    "aWdlc3QoKVs6MTJdCgoKX0JPT1RTVFJBUCA9IFsKICAgIHJlZ2lzdGVyX3ByaW1pdGl2ZSgid2l0bmVzc19pbnB1dCIsIHdp",
    "dG5lc3NfaW5wdXQpLAogICAgcmVnaXN0ZXJfcHJpbWl0aXZlKCJkaWdlc3QiLCBkaWdlc3QsIHsiaGFzaGxpYiI6IGhhc2hs",
    "aWIsICJqc29uIjoganNvbn0sCiAgICAgICAgICAgICAgICAgICAgICAgZGVwX2F0dHJzPXsic2hhMjU2IiwgImR1bXBzIn0p",
    "LApdCkJPT1RTVFJBUF9QUk9CTEVNUyA9IFtwIGZvciBncm91cCBpbiBfQk9PVFNUUkFQIGZvciBwIGluIGdyb3VwXQoKCkNP",
    "TlRSQUNUID0gewogICAgImNvbnRyYWN0X2lkIjogQ09OVFJBQ1RfSUQsCiAgICAic3RhdGVtZW50IjogIkEgd2l0bmVzcyBv",
    "YnNlcnZhdGlvbiBtdXN0IGJlIGEgcHVyZSBmdW5jdGlvbiBvZiB0aGUgZnJhbWV3b3JrLUlOSkVDVEVEIGlucHV0cyAiCiAg",
    "ICAgICAgICAgICAgICAgImFuZCB0aGUgZ292ZXJuZWQgZGV0ZXJtaW5pc3RpYyBwcmltaXRpdmVzLiBOb3RoaW5nIGVsc2Ug",
    "aXMgcmVhY2hhYmxlLCBhbmQgIgogICAgICAgICAgICAgICAgICJhbnl0aGluZyB0aGUgY29udHJhY3QgZG9lcyBub3QgbmFt",
    "ZSBpcyBSRUZVU0VELiIsCiAgICAiZ2VuZXJhbF9weXRob25fcHVyaXR5X2NsYWltZWQiOiBGYWxzZSwKICAgICJkeW5hbWlj",
    "X3N0aWxsX3JlcXVpcmVkIjogVHJ1ZSwKICAgICJsYXllcnMiOiB7CiAgICAgICAgIkwxX3NoYXBlIjogInBsYWluIHVuZGVj",
    "b3JhdGVkIG1vZHVsZS1sZXZlbCBmdW5jdGlvbjsgZXhhY3RseSBvbmUgcG9zaXRpb25hbCBwYXJhbWV0ZXI7ICIKICAgICAg",
    "ICAgICAgICAgICAgICAiX19jbG9zdXJlX18gaXMgTm9uZTsgbm8gX19kZWZhdWx0c19fL19fa3dkZWZhdWx0c19fOyBzb3Vy",
    "Y2UgcmVjb3ZlcmFibGUiLAogICAgICAgICJMMl9hc3QiOiAibm9kZSBBTExPV0xJU1Q7IE5hbWUgbG9hZHMgcmVzb2x2ZSB0",
    "byBsb2NhbCB8IGdvdmVybmVkIHByaW1pdGl2ZSB8IGFsbG93bGlzdGVkICIKICAgICAgICAgICAgICAgICAgImJ1aWx0aW47",
    "IGF0dHJpYnV0ZSBuYW1lcyBhbGxvd2xpc3RlZCBhbmQgbmV2ZXIgYF9gLXByZWZpeGVkIiwKICAgICAgICAiTDNfY29kZV9v",
    "YmplY3QiOiAiY29fbmFtZXMgZ292ZXJuZWQsIGNvX2ZyZWV2YXJzIGVtcHR5LCBuZXN0ZWQgY2FwdHVyZXMgY29uZmluZWQg",
    "dG8gdGhlICIKICAgICAgICAgICAgICAgICAgICAgICAgICAid2l0bmVzcydzIG93biBsb2NhbHMsIGxpdmUgY29kZSBvYmpl",
    "Y3QgPT0gcmVjb21waWxhdGlvbiBvZiBpdHMgc291cmNlIiwKICAgICAgICAiTDRfcmViaW5kIjogImV4ZWN1dGVkIGluIGEg",
    "Z2xvYmFscyBtYXBwaW5nIGhvbGRpbmcgb25seSB0aGUgZ292ZXJuZWQgcHJpbWl0aXZlcyBhbmQgYW4gIgogICAgICAgICAg",
    "ICAgICAgICAgICAiYWxsb3dsaXN0ZWQgX19idWlsdGluc19fIOKAlCB1bmdvdmVybmVkIG5hbWVzIGRvIG5vdCBleGlzdCBh",
    "dCBydW50aW1lIiwKICAgICAgICAiRFlOQU1JQyI6ICJ0aGUgYmFua2VkIFA2IEkxLUk1IGluc3RydW1lbnQgaXMgVU5DSEFO",
    "R0VEIGFuZCBzdGlsbCByZXF1aXJlZDsgc3RhdGljICIKICAgICAgICAgICAgICAgICAgICJhY2NlcHRhbmNlIGFsb25lIGlz",
    "IGV4cGxpY2l0bHkgaW5zdWZmaWNpZW50IChhcm0gSU5URVJMT0NLLUEpIiwKICAgIH0sCiAgICAiYWxsb3dlZF9idWlsdGlu",
    "cyI6IHNvcnRlZChBTExPV0VEX0JVSUxUSU5TKSwKICAgICJhbGxvd2VkX2F0dHJpYnV0ZXMiOiBzb3J0ZWQoQUxMT1dFRF9B",
    "VFRSSUJVVEVTKSwKICAgICJhbGxvd2VkX2RlcF9tb2R1bGVzIjogc29ydGVkKF9BTExPV0VEX0RFUF9NT0RVTEVTKSwKICAg",
    "ICJnb3Zlcm5lZF9wcmltaXRpdmVzIjoge246IHsic2hhMjU2IjogcC5zaGEyNTYsICJkZXBzIjogc29ydGVkKHAuZGVwcyl9",
    "CiAgICAgICAgICAgICAgICAgICAgICAgICAgICBmb3IgbiwgcCBpbiBQUklNSVRJVkVTLml0ZW1zKCl9LAogICAgImFsbG93",
    "ZWRfbm9kZXMiOiBzb3J0ZWQobi5fX25hbWVfXyBmb3IgbiBpbiBfQUxMT1dFRF9OT0RFUyksCn0KCgppZiBfX25hbWVfXyA9",
    "PSAiX19tYWluX18iOgogICAgcHJpbnQoanNvbi5kdW1wcyh7ImNvbnRyYWN0IjogQ09OVFJBQ1QsICJib290c3RyYXBfcHJv",
    "YmxlbXMiOiBCT09UU1RSQVBfUFJPQkxFTVN9LAogICAgICAgICAgICAgICAgICAgICBpbmRlbnQ9MSkpCg==",
]))

# p6_computational_independence.py — banked verbatim; sha256 e65e3e6a8c0b606fa52a58771ee7cf928ff2463b92f15d9c7cdfaf912083fb8b
_P6_EMBEDDED['p6_computational_independence'] = ('e65e3e6a8c0b606fa52a58771ee7cf928ff2463b92f15d9c7cdfaf912083fb8b', "".join([
    "IyEvdXNyL2Jpbi9lbnYgcHl0aG9uMwoiIiJCSC1CMHcgQWdlbnQtNSDigJQgUDYgREVSSVZFRC1PUEVSQU5EIC8gQ09NUFVU",
    "QVRJT05BTCBJTkRFUEVOREVOQ0UgcHJvdG90eXBlLgoKVEhFIFBST0JMRU0gKGV4ZWN1dGVkLCBiYW5rZWQgYXQgLi4vYmFu",
    "a2VkLWFnZW50My1wcm9iZXMvcHJvYmVfZWxldmVudGgucHkpOgpgRUxFVkVOVEhfVkFMSURfU0hBUEVEX0JVVF9MWUlOR2Ag",
    "aGFzIEVWRVJZIGd1YXJkLWFjdGl2YXRpbmcgZmllbGQgcHJlc2VudCBhbmQgd2VsbC1mb3JtZWQKKFAxIGZ1bGx5IHNhdGlz",
    "ZmllZCkgYW5kIHN0aWxsIG1lYXN1cmVkIENMRUFOIG9uIGEgMy0+MSBjb2xsZWN0aW9uIHNob3J0ZW5pbmcsIGJlY2F1c2Ug",
    "aXRzCmByZWFkc2AgZGVjbGFyYXRpb24gaXMgYSBTVFJJTkcgdGhlIGZyYW1ld29yayBjb21wYXJlcyBhbmQgbmV2ZXIgT0JT",
    "RVJWRVMuIERlY2xhcmF0aXZlCm1ldGFkYXRhIGFib3V0IGEgd2l0bmVzcyBpcyBub3QgZXZpZGVuY2UgYWJvdXQgYSB3aXRu",
    "ZXNzLgoKVEhFIE1FQ0hBTklTTSAob25lIHNlbnRlbmNlKToKICAgIEV2ZXJ5IGNoYW5uZWwgYSB3aXRuZXNzIGNhbiBzZWUg",
    "aXMgY2xhc3NpZmllZCBNVVNUX0RFUEVORCAoaXRzIGRlY2xhcmVkIGByZWFkc2ApIG9yCiAgICBNVVNUX05PVF9ERVBFTkQg",
    "KHRoZSBhcnRlZmFjdHMgdW5kZXIgdGVzdDogdGhlIGNvbGxlY3Rpb24tdW5kZXItdGVzdCBhbmQgdGhlCiAgICBleHBlY3Rl",
    "ZF9zb3VyY2UpLiBJbnB1dHMgYXJlIElOSkVDVEVEIGJ5IHRoZSBmcmFtZXdvcmssIG5ldmVyIGZldGNoZWQgYnkgdGhlIHdp",
    "dG5lc3M7CiAgICBlYWNoIGNoYW5uZWwgaXMgUEVSVFVSQkVEIHVuZGVyIGEgY29udHJvbGxlZCB0cmlhbDsgdGhlIE9CU0VS",
    "VkVEIHNlbnNpdGl2aXR5IG11c3QgbWF0Y2gKICAgIHRoZSBkZWNsYXJlZCBwb2xhcml0eSBleGFjdGx5LiBBIGNoYW5uZWwg",
    "aW4gbmVpdGhlciBjbGFzcyB0aGF0IHRoZSB3aXRuZXNzIHRvdWNoZXMgaXMKICAgIFVOREVDTEFSRUQgYW5kIHJlZnVzZWQu",
    "IEV2aWRlbmNlIGZpZWxkcyB0aGUgZnJhbWV3b3JrIGNhbiBkZXJpdmUgYXJlIERFUklWRUQsIG5ldmVyCiAgICBhY2NlcHRl",
    "ZCBhcyBzcGVjIGxpdGVyYWxzLgoKRklWRSBJTlNUUlVNRU5UUwogIEkxIERFVEVSTUlOSVNNIFBSRUNIRUNLICAgICAgICAg",
    "IC0tIHNvdW5kbmVzcyBwcmVjb25kaXRpb24gZm9yIEkzL0k0IChhIG5vbmRldGVybWluaXN0aWMKICAgICAgICAgICAgICAg",
    "ICAgICAgICAgICAgICAgICAgICAgICB3aXRuZXNzIG1ha2VzICJjaGFuZ2VkIHVuZGVyIHBlcnR1cmJhdGlvbiIgbWVhbmlu",
    "Z2xlc3MKICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICBhbmQgaXMgYW4gZXZhc2lvbiByb3V0ZSkuCiAg",
    "STIgSU5KRUNURUQgQUNDRVNTIFJFQ09SRElORyAgICAgLS0gcmVhZC10cmFja2luZyBwcm94aWVzIHJlY29yZCBfX2l0ZXJf",
    "Xy9fX2NvbnRhaW5zX18vCiAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgX19sZW5fXy9fX2dldGl0ZW1f",
    "XyBwZXIgc291cmNlLiBORUNFU1NBUlksIG5vdAogICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgIHN1ZmZp",
    "Y2llbnQgKGEgYGxlbigpYC1vbmx5IHRvdWNoZXIgZGVmZWF0cyBpdCkgLS0KICAgICAgICAgICAgICAgICAgICAgICAgICAg",
    "ICAgICAgICAgICBpdCBpcyB0aGUgY2hlYXAgc2NyZWVuIGFuZCB0aGUgdW5kZWNsYXJlZC1jaGFubmVsCiAgICAgICAgICAg",
    "ICAgICAgICAgICAgICAgICAgICAgICAgICAgZGV0ZWN0b3IsIG5ldmVyIHRoZSBwcm9vZi4KICBJMyBNVVNUX0RFUEVORCBQ",
    "RVJUVVJCQVRJT04gICAgICAtLSB1cGdyYWRlcyAidG91Y2hlZCIgdG8gImxvYWQtYmVhcmluZyI6IHBlcnR1cmIgZWFjaAog",
    "ICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgIGRlY2xhcmVkIHJlYWQgd2l0aCBhIENBUkRJTkFMSVRZLVBS",
    "RVNFUlZJTkcgY29udGVudAogICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgIHN3YXA7IHRoZSBvYnNlcnZh",
    "dGlvbiBNVVNUIGNoYW5nZS4gVGhpcyBpcyB3aGF0CiAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgY2F0",
    "Y2hlcyAzLWNsYWltZWQvMS1yZWFkLCB1bmlvbi1wYXJ0aWFsLAogICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAg",
    "ICAgIHBhcmVudC1pZ25vcmVkLCBzdW1tYXJ5LW9ubHkuCiAgSTQgTVVTVF9OT1RfREVQRU5EIElOVkFSSUFOQ0UgICAgLS0g",
    "cGVydHVyYiB0aGUgY29sbGVjdGlvbi11bmRlci10ZXN0IGFuZCB0aGUKICAgICArIFRBSU5UIENBTkFSWSAgICAgICAgICAg",
    "ICAgICAgICBleHBlY3RlZF9zb3VyY2UgdGhyb3VnaCBFVkVSWSBjaGFubmVsIHRoZXkgcmVhY2ggdGhlCiAgICAgICAgICAg",
    "ICAgICAgICAgICAgICAgICAgICAgICAgICAgd2l0bmVzcyBieTsgdGhlIG9ic2VydmF0aW9uIE1VU1QgTk9UIGNoYW5nZSwg",
    "YW5kIGEKICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICBub25jZSBjYW5hcnkgcGxhbnRlZCBpbiB0aG9z",
    "ZSBjaGFubmVscyBNVVNUIE5PVCBhcHBlYXIKICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICBpbiB0aGUg",
    "b2JzZXJ2YXRpb24gKHBvc2l0aXZlIHByb29mIG9mIGVjaG8sIGltbXVuZSB0bwogICAgICAgICAgICAgICAgICAgICAgICAg",
    "ICAgICAgICAgICAgIG1lbW9pc2F0aW9uIG9uIHRoZSBiYXNlbGluZSBydW4pLgogIEk1IERFUklWRUQtT1BFUkFORCBFTkZP",
    "UkNFTUVOVCAgIC0tIFA2IHByb3BlcjogYW4gRVZJREVOQ0UgZmllbGQgKGFzIG9wcG9zZWQgdG8gYSBQT0xJQ1kgb3IKICAg",
    "ICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICBhbiBJREVOVElGSUVSKSBpcyByZWZ1c2VkIGFzIGEgc3BlYyBs",
    "aXRlcmFsOyB0aGUgc3BlYwogICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgIG1heSBvbmx5IG5hbWUgdGhl",
    "IGlkcy9wcm92aWRlcnMgdGhlIGZyYW1ld29yayBkZXJpdmVzIGl0CiAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAg",
    "ICAgICAgZnJvbS4KCkkzIGFuZCBJNCBhcmUgdGhlIHNhbWUgaW5zdHJ1bWVudCBhdCBvcHBvc2l0ZSBwb2xhcml0eS4gTmVp",
    "dGhlciBhbG9uZSBpcyBzdWZmaWNpZW50IGFuZCB0aGUKYmF0dGVyeSBwcm92ZXMgaXQ6IGEgQ09OU1RBTlQgd2l0bmVzcyBp",
    "cyBwZXJmZWN0bHkgaW52YXJpYW50IChJNCBjbGVhbikgYW5kIHJlYWRzIG5vdGhpbmcKKEkzIGZpcmVzKTsgYSBTVU1NQVJZ",
    "LU9OTFkgd2l0bmVzcyB0b3VjaGVzIGV2ZXJ5IGRlY2xhcmVkIHNvdXJjZSAoSTIgY2xlYW4pIGFuZCBkZXBlbmRzIG9uCm5v",
    "bmUgKEkzIGZpcmVzKTsgYW4gRUNITyB3aXRuZXNzIHJlYWRzIGl0cyBkZWNsYXJlZCBzb3VyY2VzIGhvbmVzdGx5IChJMitJ",
    "MyBjbGVhbikgYW5kIHRyYWNrcwp0aGUgY29sbGVjdGlvbiAoSTQgZmlyZXMpLgoKc3RkbGliIG9ubHkuIFJlYWQtb25seSB3",
    "LnIudC4gdGhlIGNhbm9uaWNhbCByZXBvLgoiIiIKZnJvbSBfX2Z1dHVyZV9fIGltcG9ydCBhbm5vdGF0aW9ucwoKaW1wb3J0",
    "IGNvbGxlY3Rpb25zLmFiYwppbXBvcnQgaGFzaGxpYgppbXBvcnQganNvbgppbXBvcnQgcmFuZG9tCmZyb20gdHlwaW5nIGlt",
    "cG9ydCBBbnksIENhbGxhYmxlLCBPcHRpb25hbAoKQ0FOQVJZX1BSRUZJWCA9ICJfX1A2X0NBTkFSWV9fIgoKCiMgPT09PT09",
    "PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09",
    "PT09PT09PT09PT09PT09PT09CiMgSTIg4oCUIGFjY2Vzcy1yZWNvcmRpbmcgaW5qZWN0ZWQgaW5wdXRzCiMgPT09PT09PT09",
    "PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09",
    "PT09PT09PT09PT09PT09CmNsYXNzIEFjY2Vzc1JlY29yZDoKICAgICIiIk9ic2VydmVkIChub3Qgc2VsZi1yZXBvcnRlZCkg",
    "cmVhZCBiZWhhdmlvdXIgb2Ygb25lIHdpdG5lc3MgaW52b2NhdGlvbi4iIiIKCiAgICBkZWYgX19pbml0X18oc2VsZikgLT4g",
    "Tm9uZToKICAgICAgICBzZWxmLnNvdXJjZXM6IGRpY3Rbc3RyLCBzZXRbc3RyXV0gPSB7fQogICAgICAgIHNlbGYuc3BlY19r",
    "ZXlzOiBzZXRbc3RyXSA9IHNldCgpCgogICAgZGVmIHRvdWNoKHNlbGYsIGNoYW5uZWw6IHN0ciwgaG93OiBzdHIpIC0+IE5v",
    "bmU6CiAgICAgICAgc2VsZi5zb3VyY2VzLnNldGRlZmF1bHQoY2hhbm5lbCwgc2V0KCkpLmFkZChob3cpCgogICAgZGVmIHRv",
    "dWNoZWQoc2VsZikgLT4gc2V0W3N0cl06CiAgICAgICAgcmV0dXJuIHNldChzZWxmLnNvdXJjZXMpCgoKY2xhc3MgUmVjb3Jk",
    "aW5nU2V0KGNvbGxlY3Rpb25zLmFiYy5TZXQpOgogICAgIiIiQSBzZXQtbGlrZSB2aWV3IG92ZXIgb25lIGluamVjdGVkIHNv",
    "dXJjZSB0aGF0IHJlY29yZHMgZXZlcnkgYWNjZXNzLgoKICAgIERlcml2aW5nIGZyb20gY29sbGVjdGlvbnMuYWJjLlNldCBt",
    "ZWFucyBgfGAsIGAmYCwgYC1gLCBgPD1gLCBgc2V0KHgpYCBhbmQgY29tcHJlaGVuc2lvbnMKICAgIGFyZSBhbGwgaW1wbGVt",
    "ZW50ZWQgaW4gdGVybXMgb2YgX19pdGVyX18vX19jb250YWluc19fL19fbGVuX18sIHNvIHRoZXkgYXJlIGFsbCByZWNvcmRl",
    "ZAogICAgd2l0aG91dCB0aGUgd2l0bmVzcyBiZWluZyBhYmxlIHRvIHJlYWNoIHRoZSB1bmRlcmx5aW5nIGNvbnRhaW5lci4K",
    "ICAgICIiIgoKICAgIF9fc2xvdHNfXyA9ICgiX2QiLCAiX3JlYyIsICJfbmFtZSIpCgogICAgZGVmIF9faW5pdF9fKHNlbGYs",
    "IGRhdGEsIHJlY29yZDogQWNjZXNzUmVjb3JkLCBuYW1lOiBzdHIpIC0+IE5vbmU6CiAgICAgICAgc2VsZi5fZCA9IGZyb3pl",
    "bnNldChkYXRhKQogICAgICAgIHNlbGYuX3JlYyA9IHJlY29yZAogICAgICAgIHNlbGYuX25hbWUgPSBuYW1lCgogICAgZGVm",
    "IF9fY29udGFpbnNfXyhzZWxmLCBpdGVtKSAtPiBib29sOgogICAgICAgIHNlbGYuX3JlYy50b3VjaChzZWxmLl9uYW1lLCAi",
    "Y29udGFpbnMiKQogICAgICAgIHJldHVybiBpdGVtIGluIHNlbGYuX2QKCiAgICBkZWYgX19pdGVyX18oc2VsZik6CiAgICAg",
    "ICAgc2VsZi5fcmVjLnRvdWNoKHNlbGYuX25hbWUsICJpdGVyIikKICAgICAgICByZXR1cm4gaXRlcihzZWxmLl9kKQoKICAg",
    "IGRlZiBfX2xlbl9fKHNlbGYpIC0+IGludDoKICAgICAgICBzZWxmLl9yZWMudG91Y2goc2VsZi5fbmFtZSwgImxlbiIpCiAg",
    "ICAgICAgcmV0dXJuIGxlbihzZWxmLl9kKQoKICAgIGRlZiBfX3JlcHJfXyhzZWxmKSAtPiBzdHI6ICAgICAgICAgICAgICAg",
    "ICAgICAgICMgYSByZXByKCkgcmVhZCBpcyBzdGlsbCBhIHJlYWQKICAgICAgICBzZWxmLl9yZWMudG91Y2goc2VsZi5fbmFt",
    "ZSwgInJlcHIiKQogICAgICAgIHJldHVybiBmIlJlY29yZGluZ1NldCh7c29ydGVkKG1hcChzdHIsIHNlbGYuX2QpKSFyfSki",
    "CgoKY2xhc3MgUmVjb3JkaW5nTWFwcGluZyhjb2xsZWN0aW9ucy5hYmMuTWFwcGluZyk6CiAgICAiIiJLZXllZCBzb3VyY2Ug",
    "KGtleSAtPiBzZXQpIHdpdGggcGVyLWFjY2VzcyByZWNvcmRpbmcuIiIiCgogICAgZGVmIF9faW5pdF9fKHNlbGYsIGRhdGE6",
    "IGRpY3QsIHJlY29yZDogQWNjZXNzUmVjb3JkLCBuYW1lOiBzdHIpIC0+IE5vbmU6CiAgICAgICAgc2VsZi5fZCA9IGRpY3Qo",
    "ZGF0YSkKICAgICAgICBzZWxmLl9yZWMgPSByZWNvcmQKICAgICAgICBzZWxmLl9uYW1lID0gbmFtZQoKICAgIGRlZiBfX2dl",
    "dGl0ZW1fXyhzZWxmLCBrZXkpOgogICAgICAgIHNlbGYuX3JlYy50b3VjaChzZWxmLl9uYW1lLCBmImdldGl0ZW06e2tleX0i",
    "KQogICAgICAgIHYgPSBzZWxmLl9kW2tleV0KICAgICAgICByZXR1cm4gUmVjb3JkaW5nU2V0KHYsIHNlbGYuX3JlYywgZiJ7",
    "c2VsZi5fbmFtZX1be2tleX1dIikgaWYgaXNpbnN0YW5jZSgKICAgICAgICAgICAgdiwgKHNldCwgZnJvemVuc2V0LCBsaXN0",
    "LCB0dXBsZSkpIGVsc2UgdgoKICAgIGRlZiBfX2l0ZXJfXyhzZWxmKToKICAgICAgICBzZWxmLl9yZWMudG91Y2goc2VsZi5f",
    "bmFtZSwgIml0ZXIiKQogICAgICAgIHJldHVybiBpdGVyKHNlbGYuX2QpCgogICAgZGVmIF9fbGVuX18oc2VsZikgLT4gaW50",
    "OgogICAgICAgIHNlbGYuX3JlYy50b3VjaChzZWxmLl9uYW1lLCAibGVuIikKICAgICAgICByZXR1cm4gbGVuKHNlbGYuX2Qp",
    "CgoKY2xhc3MgV2l0bmVzc0lucHV0cyhjb2xsZWN0aW9ucy5hYmMuTWFwcGluZyk6CiAgICAiIiJUaGUgZnJhbWV3b3JrLWlu",
    "amVjdGVkIGlucHV0IGJ1bmRsZS4gRGVsaWJlcmF0ZWx5IE5PVCBhIGRpY3QgYW5kIE5PVCBKU09OLXNlcmlhbGlzYWJsZToK",
    "ICAgIGEgdHJhY2tlZCBKU09OIHNwZWMgZml4dHVyZSBjYW5ub3QgZm9yZ2UgdGhpcyBrZXkncyB2YWx1ZSAodGhlIFA3IGlu",
    "dGVybG9jayAtLSB0aGUgc2FtZQogICAgbm9uLXNlcmlhbGlzYWJsZS1zZW50aW5lbCBkaXNjaXBsaW5lIHRoYXQgbWFrZXMg",
    "YW4gaW5saW5lIGBvYnNlcnZlZGAgaW1wb3NzaWJsZSB0byBleHByZXNzCiAgICBpbiBhIGZpeHR1cmUpLiIiIgoKICAgIGRl",
    "ZiBfX2luaXRfXyhzZWxmLCBtYXBwaW5nOiBkaWN0KSAtPiBOb25lOgogICAgICAgIHNlbGYuX20gPSBkaWN0KG1hcHBpbmcp",
    "CgogICAgZGVmIF9fZ2V0aXRlbV9fKHNlbGYsIGspOgogICAgICAgIHJldHVybiBzZWxmLl9tW2tdCgogICAgZGVmIF9faXRl",
    "cl9fKHNlbGYpOgogICAgICAgIHJldHVybiBpdGVyKHNlbGYuX20pCgogICAgZGVmIF9fbGVuX18oc2VsZik6CiAgICAgICAg",
    "cmV0dXJuIGxlbihzZWxmLl9tKQoKCmNsYXNzIFJlY29yZGluZ1NwZWMoY29sbGVjdGlvbnMuYWJjLk1hcHBpbmcpOgogICAg",
    "IiIiUmVjb3JkcyB3aGljaCBTUEVDIEtFWVMgdGhlIHdpdG5lc3MgcmVhZHMuIEEgd2l0bmVzcyByb3V0aW5nIGl0c2VsZiB0",
    "aHJvdWdoIHNwZWMga2V5cyBubwogICAgZ3VhcmQgaW5zcGVjdHMgKGB3aXRuZXNzX3JvdXRlYCwgYHJvdXRlX3BheWxvYWRg",
    "IC0tIHRoZSBleGVjdXRlZCBlbGV2ZW50aCkgaXMgdmlzaWJsZSBoZXJlLgogICAgU3RhdGljYWxseSB0aGlzIGlzIFAzJ3Mg",
    "am9iIChjbG9zZWQgc2NoZW1hKTsgcmVjb3JkaW5nIGl0IG1ha2VzIHRoZSBQMy9QNiBpbnRlcmxvY2sKICAgIG9ic2VydmFi",
    "bGUgYW5kIGdpdmVzIGEgZGlhZ25vc3RpYyB3aGVuIFAzIGlzIG5vdCB5ZXQgbGFuZGVkLiIiIgoKICAgIGRlZiBfX2luaXRf",
    "XyhzZWxmLCBkYXRhOiBkaWN0LCByZWNvcmQ6IEFjY2Vzc1JlY29yZCkgLT4gTm9uZToKICAgICAgICBzZWxmLl9kID0gZGlj",
    "dChkYXRhKQogICAgICAgIHNlbGYuX3JlYyA9IHJlY29yZAoKICAgIGRlZiBfX2dldGl0ZW1fXyhzZWxmLCBrKToKICAgICAg",
    "ICBzZWxmLl9yZWMuc3BlY19rZXlzLmFkZChrKQogICAgICAgIHJldHVybiBzZWxmLl9kW2tdCgogICAgZGVmIF9faXRlcl9f",
    "KHNlbGYpOgogICAgICAgIHJldHVybiBpdGVyKHNlbGYuX2QpCgogICAgZGVmIF9fbGVuX18oc2VsZik6CiAgICAgICAgcmV0",
    "dXJuIGxlbihzZWxmLl9kKQoKCiMgPT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09",
    "PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09CiMgaGVscGVycyDigJQgY2Fub25pY2FsaXNh",
    "dGlvbiBhbmQgY2hhbm5lbCBzdWJzdGl0dXRpb24KIyA9PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09",
    "PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT0KZGVmIF9jYW5vbihvYnM6",
    "IEFueSkgLT4gc3RyOgogICAgIiIiT3JkZXItaW5kZXBlbmRlbnQgY2Fub25pY2FsIGZvcm0gb2YgYSB3aXRuZXNzIG9ic2Vy",
    "dmF0aW9uIChzZXQgfCBkaWN0IHwgc2NhbGFyKS4iIiIKICAgIGRlZiBub3JtKHgpOgogICAgICAgIGlmIGlzaW5zdGFuY2Uo",
    "eCwgKHNldCwgZnJvemVuc2V0KSk6CiAgICAgICAgICAgIHJldHVybiB7Il9fc2V0X18iOiBzb3J0ZWQoc3RyKGkpIGZvciBp",
    "IGluIHgpfQogICAgICAgIGlmIGlzaW5zdGFuY2UoeCwgKGxpc3QsIHR1cGxlKSk6CiAgICAgICAgICAgIHJldHVybiBbbm9y",
    "bShpKSBmb3IgaSBpbiB4XQogICAgICAgIGlmIGlzaW5zdGFuY2UoeCwgZGljdCk6CiAgICAgICAgICAgIHJldHVybiB7c3Ry",
    "KGspOiBub3JtKHYpIGZvciBrLCB2IGluIHNvcnRlZCh4Lml0ZW1zKCksIGtleT1sYW1iZGEga3Y6IHN0cihrdlswXSkpfQog",
    "ICAgICAgIHJldHVybiBzdHIoeCkKICAgIHJldHVybiBqc29uLmR1bXBzKG5vcm0ob2JzKSwgc29ydF9rZXlzPVRydWUsIHNl",
    "cGFyYXRvcnM9KCIsIiwgIjoiKSkKCgpkZWYgX21lbWJlcnMob2JzOiBBbnkpIC0+IHNldFtzdHJdOgogICAgIiIiRXZlcnkg",
    "c2NhbGFyIGxlYWYgb2YgYW4gb2JzZXJ2YXRpb24sIGZvciBjYW5hcnkgZGV0ZWN0aW9uLiIiIgogICAgb3V0OiBzZXRbc3Ry",
    "XSA9IHNldCgpCiAgICBzdGFjayA9IFtvYnNdCiAgICB3aGlsZSBzdGFjazoKICAgICAgICB4ID0gc3RhY2sucG9wKCkKICAg",
    "ICAgICBpZiBpc2luc3RhbmNlKHgsIChzZXQsIGZyb3plbnNldCwgbGlzdCwgdHVwbGUpKToKICAgICAgICAgICAgc3RhY2su",
    "ZXh0ZW5kKHgpCiAgICAgICAgZWxpZiBpc2luc3RhbmNlKHgsIGRpY3QpOgogICAgICAgICAgICBzdGFjay5leHRlbmQoeC5r",
    "ZXlzKCkpCiAgICAgICAgICAgIHN0YWNrLmV4dGVuZCh4LnZhbHVlcygpKQogICAgICAgIGVsc2U6CiAgICAgICAgICAgIG91",
    "dC5hZGQoc3RyKHgpKQogICAgcmV0dXJuIG91dAoKCmRlZiBfYXNfa2V5KGNvbGw6IEFueSkgLT4gZnJvemVuc2V0OgogICAg",
    "aWYgaXNpbnN0YW5jZShjb2xsLCBkaWN0KToKICAgICAgICByZXR1cm4gZnJvemVuc2V0KHN0cihrKSBmb3IgayBpbiBjb2xs",
    "KQogICAgaWYgaXNpbnN0YW5jZShjb2xsLCAoc2V0LCBmcm96ZW5zZXQsIGxpc3QsIHR1cGxlKSk6CiAgICAgICAgcmV0dXJu",
    "IGZyb3plbnNldChzdHIoeCkgZm9yIHggaW4gY29sbCkKICAgIHJldHVybiBmcm96ZW5zZXQoe3N0cihjb2xsKX0pCgoKZGVm",
    "IF9zdWJzdGl0dXRlKG5vZGU6IEFueSwgdGFyZ2V0OiBmcm96ZW5zZXQsIHJlcGxhY2VtZW50OiBzZXQpIC0+IEFueToKICAg",
    "ICIiIlJlcGxhY2UgRVZFUlkgbm9kZSBzdHJ1Y3R1cmFsbHkgZXF1YWwgdG8gYHRhcmdldGAgd2l0aCBgcmVwbGFjZW1lbnRg",
    "LCBwcmVzZXJ2aW5nIHRoZQogICAgY29udGFpbmVyIHR5cGUuIFRoaXMgaXMgdGhlIGNoYW5uZWwtZW51bWVyYXRpb24gc3Rl",
    "cCBvZiBJNDogYSBjYWxsZXIgd2hvIHBsYW50cyBhIGNvcHkgb2YKICAgIHRoZSBjb2xsZWN0aW9uIGFueXdoZXJlIGluIHRo",
    "ZSBzcGVjIChgcm91dGVfcGF5bG9hZC5lY2hvX29mX2NvbGxlY3Rpb24gPSBzb3J0ZWQoREVDTEFSRUQpYAogICAgLS0gdGhl",
    "IGV4ZWN1dGVkIGVsZXZlbnRoKSBoYXMgdGhhdCBjb3B5IHBlcnR1cmJlZCB0b28sIHNvIGxhdW5kZXJpbmcgdGhlIGVjaG8g",
    "dGhyb3VnaCBhbgogICAgaW5kaXJlY3Rpb24gbGF5ZXIgZG9lcyBub3QgZXZhZGUgdGhlIGludmFyaWFuY2UgdHJpYWwuIiIi",
    "CiAgICBpZiBpc2luc3RhbmNlKG5vZGUsIChzZXQsIGZyb3plbnNldCkpOgogICAgICAgIGlmIF9hc19rZXkobm9kZSkgPT0g",
    "dGFyZ2V0OgogICAgICAgICAgICByZXR1cm4gc2V0KHJlcGxhY2VtZW50KQogICAgICAgIHJldHVybiBub2RlCiAgICBpZiBp",
    "c2luc3RhbmNlKG5vZGUsIChsaXN0LCB0dXBsZSkpOgogICAgICAgIGlmIF9hc19rZXkobm9kZSkgPT0gdGFyZ2V0OgogICAg",
    "ICAgICAgICBvdXQgPSBzb3J0ZWQocmVwbGFjZW1lbnQpCiAgICAgICAgICAgIHJldHVybiB0dXBsZShvdXQpIGlmIGlzaW5z",
    "dGFuY2Uobm9kZSwgdHVwbGUpIGVsc2Ugb3V0CiAgICAgICAgc2VxID0gW19zdWJzdGl0dXRlKHgsIHRhcmdldCwgcmVwbGFj",
    "ZW1lbnQpIGZvciB4IGluIG5vZGVdCiAgICAgICAgcmV0dXJuIHR1cGxlKHNlcSkgaWYgaXNpbnN0YW5jZShub2RlLCB0dXBs",
    "ZSkgZWxzZSBzZXEKICAgIGlmIGlzaW5zdGFuY2Uobm9kZSwgZGljdCk6CiAgICAgICAgcmV0dXJuIHtrOiBfc3Vic3RpdHV0",
    "ZSh2LCB0YXJnZXQsIHJlcGxhY2VtZW50KSBmb3IgaywgdiBpbiBub2RlLml0ZW1zKCl9CiAgICByZXR1cm4gbm9kZQoKCmRl",
    "ZiBfcGVydHVyYihtZW1iZXJzOiBzZXQsIG5vbmNlOiBzdHIsICosIHByZXNlcnZlX2NhcmRpbmFsaXR5OiBib29sID0gVHJ1",
    "ZSkgLT4gc2V0OgogICAgIiIiQ2FyZGluYWxpdHktcHJlc2VydmluZyBDT05URU5UIHN3YXA6IGRyb3AgdGhlIGxvd2VzdCBt",
    "ZW1iZXIsIGFkZCBhIG5vbmNlIGNhbmFyeS4KCiAgICBDYXJkaW5hbGl0eSBwcmVzZXJ2YXRpb24gaXMgbG9hZC1iZWFyaW5n",
    "IC0tIGl0IGlzIHdoYXQgbWFrZXMgYSBTVU1NQVJZLU9OTFkgY29uc3VtZXIKICAgIChgbGVuKHNvdXJjZSlgKSByZWdpc3Rl",
    "ciBhcyBJTkVSVCByYXRoZXIgdGhhbiBzbmVha2luZyB0aHJvdWdoIGEgc2l6ZS1zZW5zaXRpdml0eSB0ZXN0LgogICAgIiIi",
    "CiAgICBvdXQgPSBzZXQobWVtYmVycykKICAgIGlmIG91dCBhbmQgcHJlc2VydmVfY2FyZGluYWxpdHk6CiAgICAgICAgb3V0",
    "LmRpc2NhcmQoc29ydGVkKG91dCwga2V5PXN0cilbMF0pCiAgICBvdXQuYWRkKG5vbmNlKQogICAgcmV0dXJuIG91dAoKCmRl",
    "ZiBfcHJvYmxlbShraW5kOiBzdHIsIGNoYW5uZWw6IE9wdGlvbmFsW3N0cl0sIGRldGFpbDogc3RyKSAtPiBkaWN0OgogICAg",
    "cmV0dXJuIHsia2luZCI6IGtpbmQsICJjaGFubmVsIjogY2hhbm5lbCwgImRldGFpbCI6IGRldGFpbH0KCgojIFdoaWNoIGlu",
    "c3RydW1lbnQgcHJvZHVjZXMgd2hpY2ggcmVmdXNhbCBraW5kLiBVc2VkIGJ5IHRoZSBLSUxMLVBST09GIGJhdHRlcnk6IGRp",
    "c2FibGluZyBhbgojIGluc3RydW1lbnQgbXVzdCBmbGlwIGEgc3BlY2lmaWMsIG5vbi1lbXB0eSBzZXQgb2YgYXJtcyBmcm9t",
    "IFJFRlVTRSB0byBQQVNTLCBvdGhlcndpc2UgdGhhdAojIGluc3RydW1lbnQgaXMgdW53aXRuZXNzZWQgYW5kIHRoZSBhcm1z",
    "IHRoYXQgImNvdmVyIiBpdCBhcmUgdm9pZC4KSU5TVFJVTUVOVF9PRiA9IHsKICAgICJOT05ERVRFUk1JTklTVElDX1dJVE5F",
    "U1MiOiAiSTEiLAogICAgIkRFQ0xBUkVEX1JFQURfVU5BQ0NFU1NFRCI6ICJJMiIsCiAgICAiVU5ERUNMQVJFRF9DSEFOTkVM",
    "X1JFQUQiOiAiSTIiLAogICAgIkFSVEVGQUNUX1VOREVSX1RFU1RfUkVBRCI6ICJJMiIsCiAgICAiREVDTEFSRURfUkVBRF9J",
    "TkVSVCI6ICJJMyIsCiAgICAiVEFJTlRfQ0FOQVJZX0VDSE9FRCI6ICJJNCIsCiAgICAiT0JTRVJWQVRJT05fVFJBQ0tTX0FS",
    "VEVGQUNUIjogIkk0IiwKICAgICJPQlNFUlZFRF9UUkFDS1NfRVhQRUNURUQiOiAiSTQiLAogICAgIldJVE5FU1NfUkFJU0VE",
    "X1VOREVSX01VVEFUSU9OIjogIkk0IiwKICAgICJTVVBQTElFRF9FVklERU5DRV9MSVRFUkFMIjogIkk1IiwKICAgICJSRUFE",
    "U19VTkRFQ0xBUkVEIjogIkkwIiwKICAgICJSRUFEU19FTVBUWSI6ICJJMCIsCiAgICAiQ09QSUVEX09SQUNMRV9ERUNMQVJF",
    "RCI6ICJJMCIsCiAgICAiQ0hBTk5FTF9VTlJFU09MVkFCTEUiOiAiSTAiLAogICAgIldJVE5FU1NfUkFJU0VEIjogIkkwIiwK",
    "fQoKRElTQUJMRUQ6IHNldFtzdHJdID0gc2V0KCkgICAgICAgICAgIyBraWxsLXByb29mIHN3aXRjaDsgZW1wdHkgaW4gbm9y",
    "bWFsIG9wZXJhdGlvbgpfUEVSVFVSQkFUSU9OX0NBUCA9IDI1NiAgICAgICAgICAgICAjIGJvdW5kIG9uIHRoZSBwZXItbWVt",
    "YmVyIGVzY2FsYXRpb24gaW4gSTMKCgojID09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09",
    "PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PQojIEk1IOKAlCBERVJJVkVELU9QRVJB",
    "TkQgRU5GT1JDRU1FTlQgKFA2IHByb3BlcikKIyA9PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09",
    "PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT0KIyBBbiBFVklERU5DRSBmaWVs",
    "ZCBpcyBvbmUgd2hvc2UgVkFMVUUgaXMgYSBtZWFzdXJlbWVudC4gQSBQT0xJQ1kgZmllbGQgc3RhdGVzIGludGVudDsgYW4K",
    "IyBJREVOVElGSUVSIGZpZWxkIG5hbWVzIGFuIGF1dGhvcml0eS4gT25seSBwb2xpY3kgYW5kIGlkZW50aWZpZXIgZmllbGRz",
    "IG1heSBiZSBKU09OIGxpdGVyYWxzLgojIEV2ZXJ5IGV2aWRlbmNlIGZpZWxkIG11c3QgYmUgcHJvZHVjZWQgYnkgYSBmcmFt",
    "ZXdvcmstaW52b2tlZCBkZXJpdmF0aW9uIHdob3NlIElOUFVUIHRoZSBzcGVjCiMgbWF5IG5hbWUuIFJ1bGU6IHRoZSBsaXRl",
    "cmFsJ3MgcHJlc2VuY2UgaXMgaXRzZWxmIHRoZSByZWZ1c2FsIC0tICJkZXJpdmUsIHRoZW4gY29tcGFyZSIgc3RpbGwKIyBs",
    "ZXRzIGEgY2FsbGVyIHdobyBndWVzc2VzIHJpZ2h0IHN1cHBseSB0aGUgb3BlcmFuZCwgYW5kIGEgc3VwcGxpZWQtYW5kLWFn",
    "cmVlaW5nIG9wZXJhbmQgaXMgYQojIGNhbGxlci1hdXRob3JlZCBtZWFzdXJlbWVudCB0aGF0IGhhcHBlbnMgdG8gYmUgdHJ1",
    "ZSB0aGlzIHJ1bi4KX0VWSURFTkNFX0ZJRUxEUzogZGljdFtzdHIsIGRpY3Rbc3RyLCBzdHJdXSA9IHsKICAgICJIQVNIX0JB",
    "Q0tTVE9QIjogewogICAgICAgICJvYnNlcnZlZF9oYXNoIjogInJlY29tcHV0ZWQgZnJvbSB0aGUgb2JzZXJ2ZWQgdW5pdmVy",
    "c2UgRCIsCiAgICB9LAogICAgIlBBUlRJVElPTiI6IHsKICAgICAgICAicGFydGl0aW9uX21lbWJlcnMiOiAicGFydGl0aW9u",
    "X21lbWJlcl9pZHMgKHJlc29sdmVkIHZpYSBjb2xsZWN0aW9uX2NvbXBsZXRlbmVzcy5fY29sbGVjdGlvbikiLAogICAgfSwK",
    "ICAgICJESUZGRVJFTlRJQUxfRVhFQ1VUSU9OIjogewogICAgICAgICJtZW1iZXJfZWZmZWN0IjogImVmZmVjdF9wcm9iZXIg",
    "KGEgcmVnaXN0ZXJlZCBwcm92aWRlciBleGVjdXRlZCBwZXIgbWVtYmVyKSIsCiAgICAgICAgImJhc2VsaW5lX2hlYWx0aHki",
    "OiAiYmFzZWxpbmVfcHJvYmVyIChhIHJlZ2lzdGVyZWQgcHJvdmlkZXIpIiwKICAgIH0sCiAgICAiU0NIRU1BX1NUUklDVE5F",
    "U1MiOiB7CiAgICAgICAgInVua25vd25fcHJvYmVfYWNjZXB0ZWQiOiAic3RyaWN0bmVzc19wcm9iZXIgKGEgcmVnaXN0ZXJl",
    "ZCBwcm92aWRlciBleGVjdXRlZCBhZ2FpbnN0ICIKICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICJ0aGUgcmVh",
    "bCBjb25zdW1lcikiLAogICAgfSwKICAgICJLRVlFRF9NQVBQSU5HX0FHQUlOU1RfVU5JT04iOiB7CiAgICAgICAgInZhbHVl",
    "X2RvbWFpbiI6ICJ2YWx1ZV9kb21haW5faWQgKHJlc29sdmVkIHRvIGEgcmVhbCBjb2xsZWN0aW9uKSIsCiAgICB9LAogICAg",
    "IioiOiB7ICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAjIGFueSByZWxhdGlvbiAvIGFueSBn",
    "dWFyYW50ZWUga2luZAogICAgICAgICJvYnNlcnZlZCI6ICJhIHByb3ZpZGVyIChQNzogaW5saW5lIG9ic2VydmVkIGlzIG5v",
    "dCBKU09OLWV4cHJlc3NpYmxlKSIsCiAgICAgICAgImVtcHR5X2NvbmRpdGlvbl9tZXQiOiAiZW1wdHlfY29uZGl0aW9uX3By",
    "b3ZpZGVyIChQNTogYSBndWFyZC1kaXNhYmxlIG11c3QgYmUgYmFja2VkIGJ5ICIKICAgICAgICAgICAgICAgICAgICAgICAg",
    "ICAgICAgICJhIHJlZ2lzdGVyZWQgY29uZGl0aW9uIHByb3ZpZGVyIHRoZSBmcmFtZXdvcmsgQ0FMTFMpIiwKICAgICAgICAi",
    "Z3JvdW5kcyI6ICJncm91bmRzX3NvdXJjZV9pZHMgKGNvdW50IGEgU0VUIG9mIGRpc3RpbmN0IGF1dGhvcml0aWVzLCBuZXZl",
    "ciBhIHdpdG5lc3MgIgogICAgICAgICAgICAgICAgICAgInNlbGYtcmVwb3J0KSIsCiAgICAgICAgImV4cGVjdGVkX29ic2Vy",
    "dmFibGVfbWlzbWF0Y2giOiAiZGVyaXZlZCBieSBkaWZmaW5nIHRoZSBiYXNlbGluZSBhbmQgbXV0YXRlZCB2ZXJkaWN0cyIs",
    "CiAgICB9LAp9CgoKZGVmIHZlcmlmeV9kZXJpdmVkX29wZXJhbmRzKHNwZWM6IGRpY3QsIHJlbGF0aW9uX29yX2tpbmQ6IHN0",
    "cikgLT4gbGlzdFtkaWN0XToKICAgICIiIkk1LiBSZWZ1c2UgYW55IGV2aWRlbmNlIGZpZWxkIHN1cHBsaWVkIGFzIGEgc3Bl",
    "YyBsaXRlcmFsLiIiIgogICAgcHJvYmxlbXM6IGxpc3RbZGljdF0gPSBbXQogICAgZm9yIHNjb3BlIGluICgiKiIsIHJlbGF0",
    "aW9uX29yX2tpbmQpOgogICAgICAgIGZvciBmaWVsZCwgZGVyaXZhdGlvbiBpbiBfRVZJREVOQ0VfRklFTERTLmdldChzY29w",
    "ZSwge30pLml0ZW1zKCk6CiAgICAgICAgICAgIGlmIGZpZWxkIGluIHNwZWM6CiAgICAgICAgICAgICAgICBwcm9ibGVtcy5h",
    "cHBlbmQoX3Byb2JsZW0oCiAgICAgICAgICAgICAgICAgICAgIlNVUFBMSUVEX0VWSURFTkNFX0xJVEVSQUwiLCBmaWVsZCwK",
    "ICAgICAgICAgICAgICAgICAgICBmIntyZWxhdGlvbl9vcl9raW5kfTogc3BlYyBzdXBwbGllcyB0aGUgRVZJREVOQ0UgZmll",
    "bGQge2ZpZWxkIXJ9IGFzIGEgIgogICAgICAgICAgICAgICAgICAgIGYibGl0ZXJhbDsgZXZpZGVuY2UgaXMgZGVyaXZlZCwg",
    "bmV2ZXIgYWNjZXB0ZWQgLS0gZGVjbGFyZSBpbnN0ZWFkOiAiCiAgICAgICAgICAgICAgICAgICAgZiJ7ZGVyaXZhdGlvbn07",
    "IFJFRlVTRUQiKSkKICAgIHJldHVybiBbXSBpZiAiSTUiIGluIERJU0FCTEVEIGVsc2UgcHJvYmxlbXMKCgojID09PT09PT09",
    "PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09",
    "PT09PT09PT09PT09PT09PQojIEkxLUk0IOKAlCB0aGUgZXhlY3V0aW9uIGluc3RydW1lbnQKIyA9PT09PT09PT09PT09PT09",
    "PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09",
    "PT09PT09PT0KZGVmIF9ydW4od2l0bmVzczogQ2FsbGFibGUsIHNwZWM6IGRpY3QsIHNvdXJjZXM6IGRpY3QsIHRhZzogc3Ry",
    "KSAtPiB0dXBsZVtzdHIsIEFueSwgQWNjZXNzUmVjb3JkLAogICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAg",
    "ICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgT3B0aW9uYWxbRXhjZXB0aW9uXV06CiAgICAiIiJPbmUgaW5z",
    "dHJ1bWVudGVkIGludm9jYXRpb24uIFJldHVybnMgKGNhbm9uaWNhbCBvYnNlcnZhdGlvbiwgcmF3LCBhY2Nlc3MgcmVjb3Jk",
    "LCBlcnJvcikuIiIiCiAgICByZWMgPSBBY2Nlc3NSZWNvcmQoKQogICAgaW5qZWN0ZWQgPSB7fQogICAgZm9yIG5hbWUsIHZh",
    "bCBpbiBzb3VyY2VzLml0ZW1zKCk6CiAgICAgICAgaW5qZWN0ZWRbbmFtZV0gPSAoUmVjb3JkaW5nTWFwcGluZyh2YWwsIHJl",
    "YywgbmFtZSkgaWYgaXNpbnN0YW5jZSh2YWwsIGRpY3QpCiAgICAgICAgICAgICAgICAgICAgICAgICAgZWxzZSBSZWNvcmRp",
    "bmdTZXQodmFsLCByZWMsIG5hbWUpKQogICAgY2FsbF9zcGVjID0gZGljdChzcGVjKQogICAgY2FsbF9zcGVjWyJfd2l0bmVz",
    "c19pbnB1dHMiXSA9IFdpdG5lc3NJbnB1dHMoaW5qZWN0ZWQpCiAgICB0cnk6CiAgICAgICAgcmF3ID0gd2l0bmVzcyhSZWNv",
    "cmRpbmdTcGVjKGNhbGxfc3BlYywgcmVjKSkKICAgIGV4Y2VwdCBFeGNlcHRpb24gYXMgZXhjOiAgICAgICAgICAgICAgICAg",
    "ICAgICAgICAgICAgICAgICAgICMgYSB3aXRuZXNzIHRoYXQgY3Jhc2hlcyBpcyBub3QgY2xlYW4KICAgICAgICByZXR1cm4g",
    "ZiI8cmFpc2VkOnt0eXBlKGV4YykuX19uYW1lX199PiIsIE5vbmUsIHJlYywgZXhjCiAgICByZXR1cm4gX2Nhbm9uKHJhdyks",
    "IHJhdywgcmVjLCBOb25lCgoKZGVmIHZlcmlmeV9jb21wdXRhdGlvbmFsX2luZGVwZW5kZW5jZSgKICAgIHdpdG5lc3M6IENh",
    "bGxhYmxlLAogICAgc3BlYzogZGljdCwKICAgIHJlYWxfY29sbGVjdGlvbjogQW55LAogICAgKiwKICAgIGNpZDogc3RyLAog",
    "ICAgc291cmNlczogZGljdFtzdHIsIEFueV0sCiAgICBleHBlY3RlZF9zb3VyY2VfaWQ6IE9wdGlvbmFsW3N0cl0gPSBOb25l",
    "LAogICAgbm9uY2Vfc2VlZDogaW50ID0gMCwKKSAtPiBsaXN0W2RpY3RdOgogICAgIiIiVmVyaWZ5IGJ5IEVYRUNVVElPTiB0",
    "aGF0IGB3aXRuZXNzYDoKICAgICAgKDEpIGlzIGRldGVybWluaXN0aWMgdW5kZXIgaWRlbnRpY2FsIGNvbmRpdGlvbnMgICAg",
    "ICAgICAgICAgICAgICAgICAgW0kxXQogICAgICAoMikgVE9VQ0hFUyBldmVyeSBzb3VyY2UgaXQgZGVjbGFyZXMgaW4gYHJl",
    "YWRzYCAgICAgICAgICAgICAgICAgICAgICBbSTJdCiAgICAgICgzKSBpcyBDT05URU5ULVNFTlNJVElWRSB0byBldmVyeSBz",
    "b3VyY2UgaXQgZGVjbGFyZXMgaW4gYHJlYWRzYCAgICAgIFtJM10KICAgICAgKDQpIGlzIElOVkFSSUFOVCB0byB0aGUgY29s",
    "bGVjdGlvbi11bmRlci10ZXN0IGFuZCB0byBleHBlY3RlZF9zb3VyY2UsCiAgICAgICAgICB0aHJvdWdoIGV2ZXJ5IGNoYW5u",
    "ZWwgdGhvc2UgcmVhY2ggaXQgYnksIGFuZCBuZXZlciBlbWl0cyBhIGNhbmFyeQogICAgICAgICAgcGxhbnRlZCBpbiB0aGVt",
    "ICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICBbSTRdCiAgICAgICg1KSB0b3VjaGVz",
    "IG5vIGNoYW5uZWwgaXQgZGlkIG5vdCBkZWNsYXJlICAgICAgICAgICAgICAgICAgICAgICAgICAgIFtJMl0KCiAgICBgc291",
    "cmNlc2AgbWFwcyBjaGFubmVsIGlkIC0+IGNvbmNyZXRlIG1lbWJlcnMuIEl0IE1VU1QgY29udGFpbiBldmVyeSBkZWNsYXJl",
    "ZCByZWFkLCB0aGUKICAgIGNvbGxlY3Rpb24tdW5kZXItdGVzdCB1bmRlciBgY2lkYCwgYW5kIGBleHBlY3RlZF9zb3VyY2Vf",
    "aWRgIHdoZW4gcHJlc2VudCAtLSB0aGUgZnJhbWV3b3JrCiAgICBJTkpFQ1RTIHRoZXNlOyB0aGUgd2l0bmVzcyBuZXZlciBm",
    "ZXRjaGVzIHRoZW0uIEEgd2l0bmVzcyB0aGF0IGlnbm9yZXMgYF93aXRuZXNzX2lucHV0c2AKICAgIGNhbm5vdCBkZW1vbnN0",
    "cmF0ZSBkZXBlbmRlbmNlIGFuZCBpcyByZWZ1c2VkLCB3aGljaCBpcyB0aGUgYXJjaGl0ZWN0dXJhbCBwb2ludDogdGhlIHJl",
    "YWQKICAgIHNldCBiZWNvbWVzIG9ic2VydmFibGUgQlkgQ09OU1RSVUNUSU9OIHJhdGhlciB0aGFuIGJ5IGRlY2xhcmF0aW9u",
    "LgoKICAgIFJldHVybnMgYSBsaXN0IG9mIHByb2JsZW0gZGljdHMuIFtdIG1lYW5zIHRoZSB3aXRuZXNzJ3MgZGVjbGFyZWQg",
    "ZGVwZW5kZW5jeSBzdHJ1Y3R1cmUgd2FzCiAgICBPQlNFUlZFRCB0byBob2xkLiBOZXZlciByYWlzZXMuCiAgICAiIiIKICAg",
    "IHByb2JsZW1zOiBsaXN0W2RpY3RdID0gW10KICAgIHJuZyA9IHJhbmRvbS5SYW5kb20obm9uY2Vfc2VlZCkKICAgIG5vbmNl",
    "ID0gZiJ7Q0FOQVJZX1BSRUZJWH17cm5nLmdldHJhbmRiaXRzKDY0KTowMTZ4fSIKCiAgICBkZWNsYXJlZF9yYXcgPSBzcGVj",
    "LmdldCgicmVhZHMiKQogICAgaWYgZGVjbGFyZWRfcmF3IGlzIE5vbmU6CiAgICAgICAgcmV0dXJuIFtfcHJvYmxlbSgiUkVB",
    "RFNfVU5ERUNMQVJFRCIsIE5vbmUsCiAgICAgICAgICAgICAgICAgICAgICAgICAidGhlIHNwZWMgZGVjbGFyZXMgbm8gYHJl",
    "YWRzYDsgYSB3aXRuZXNzIHRoYXQgbmFtZXMgbm8gZGVwZW5kZW5jeSAiCiAgICAgICAgICAgICAgICAgICAgICAgICAiY2Fu",
    "bm90IGJlIHZlcmlmaWVkIHRvIGhhdmUgb25lLCBhbmQgYW4gdW52ZXJpZmlhYmxlIGRlcGVuZGVuY3kgaXMgYSAiCiAgICAg",
    "ICAgICAgICAgICAgICAgICAgICAiZmFpbC1vcGVuICh0aGlzIGlzIHRoZSBlbGV2ZW50aCdzIGVudHJ5IHBvaW50KTsgUkVG",
    "VVNFRCIpXQogICAgZGVjbGFyZWQgPSAoe2RlY2xhcmVkX3Jhd30gaWYgaXNpbnN0YW5jZShkZWNsYXJlZF9yYXcsIHN0cikg",
    "ZWxzZSBzZXQoZGVjbGFyZWRfcmF3KSkKICAgIGlmIG5vdCBkZWNsYXJlZDoKICAgICAgICByZXR1cm4gW19wcm9ibGVtKCJS",
    "RUFEU19FTVBUWSIsIE5vbmUsICJgcmVhZHNgIGlzIGVtcHR5OyBSRUZVU0VEIildCgogICAgbXVzdF9ub3Q6IHNldFtzdHJd",
    "ID0ge2NpZH0KICAgIGlmIGV4cGVjdGVkX3NvdXJjZV9pZDoKICAgICAgICBtdXN0X25vdC5hZGQoZXhwZWN0ZWRfc291cmNl",
    "X2lkKQogICAgb3ZlcmxhcCA9IGRlY2xhcmVkICYgbXVzdF9ub3QKICAgIGlmIG92ZXJsYXA6CiAgICAgICAgcHJvYmxlbXMu",
    "YXBwZW5kKF9wcm9ibGVtKAogICAgICAgICAgICAiQ09QSUVEX09SQUNMRV9ERUNMQVJFRCIsIHNvcnRlZChvdmVybGFwKVsw",
    "XSwKICAgICAgICAgICAgZiJ0aGUgd2l0bmVzcyBkZWNsYXJlcyBhIHJlYWQgb2YgYW4gYXJ0ZWZhY3QgdW5kZXIgdGVzdCB7",
    "c29ydGVkKG92ZXJsYXApfTsgdGhhdCBpcyBhICIKICAgICAgICAgICAgInNlY29uZCBjb3B5IG9mIHRoZSBsaXN0LCBub3Qg",
    "YW4gaW5kZXBlbmRlbnQgb3JhY2xlOyBSRUZVU0VEIikpCiAgICBtaXNzaW5nX2NoYW5uZWxzID0gKGRlY2xhcmVkIHwgbXVz",
    "dF9ub3QpIC0gc2V0KHNvdXJjZXMpCiAgICBpZiBtaXNzaW5nX2NoYW5uZWxzOgogICAgICAgIHJldHVybiBwcm9ibGVtcyAr",
    "IFtfcHJvYmxlbSgKICAgICAgICAgICAgIkNIQU5ORUxfVU5SRVNPTFZBQkxFIiwgc29ydGVkKG1pc3NpbmdfY2hhbm5lbHMp",
    "WzBdLAogICAgICAgICAgICBmImNoYW5uZWxzIHtzb3J0ZWQobWlzc2luZ19jaGFubmVscyl9IGNvdWxkIG5vdCBiZSByZXNv",
    "bHZlZCBmb3IgaW5qZWN0aW9uOyBhICIKICAgICAgICAgICAgImRlcGVuZGVuY3kgdGhhdCBjYW5ub3QgYmUgaW5zdHJ1bWVu",
    "dGVkIGNhbm5vdCBiZSB2ZXJpZmllZDsgUkVGVVNFRCIpXQoKICAgICMgLS0tLSBJMSBkZXRlcm1pbmlzbSBwcmVjaGVjayAt",
    "LS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0KICAgIGJhc2VfYSwgcmF3",
    "X2EsIHJlY19hLCBlcnJfYSA9IF9ydW4od2l0bmVzcywgc3BlYywgc291cmNlcywgImJhc2VsaW5lLWEiKQogICAgaWYgZXJy",
    "X2EgaXMgbm90IE5vbmU6CiAgICAgICAgcmV0dXJuIHByb2JsZW1zICsgW19wcm9ibGVtKCJXSVRORVNTX1JBSVNFRCIsIE5v",
    "bmUsCiAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgIGYid2l0bmVzcyByYWlzZWQge3R5cGUoZXJyX2EpLl9f",
    "bmFtZV9ffToge2Vycl9hfTsgbm90IGNsZWFuIildCiAgICBiYXNlX2IsIF8sIF8sIF8gPSBfcnVuKHdpdG5lc3MsIHNwZWMs",
    "IHNvdXJjZXMsICJiYXNlbGluZS1iIikKICAgIGlmIGJhc2VfYSAhPSBiYXNlX2IgYW5kICJJMSIgbm90IGluIERJU0FCTEVE",
    "OgogICAgICAgIHJldHVybiBwcm9ibGVtcyArIFtfcHJvYmxlbSgKICAgICAgICAgICAgIk5PTkRFVEVSTUlOSVNUSUNfV0lU",
    "TkVTUyIsIE5vbmUsCiAgICAgICAgICAgICJ0d28gaW52b2NhdGlvbnMgdW5kZXIgSURFTlRJQ0FMIGNvbmRpdGlvbnMgcHJv",
    "ZHVjZWQgZGlmZmVyZW50IG9ic2VydmF0aW9uczsgIgogICAgICAgICAgICAic2Vuc2l0aXZpdHkgYW5kIGludmFyaWFuY2Ug",
    "YXJlIGJvdGggbWVhbmluZ2xlc3MgYWdhaW5zdCBhIG5vbmRldGVybWluaXN0aWMgIgogICAgICAgICAgICAid2l0bmVzcyAo",
    "YW5kIG5vbmRldGVybWluaXNtIGlzIGl0c2VsZiBhbiBldmFzaW9uIHJvdXRlOiBldmVyeSBwZXJ0dXJiYXRpb24gd291bGQg",
    "IgogICAgICAgICAgICAiJ2NoYW5nZScgdGhlIG91dHB1dCk7IFJFRlVTRUQiKV0KCiAgICAjIC0tLS0gSTIgYWNjZXNzIHJl",
    "Y29yZGluZyAtLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tCiAg",
    "ICB0b3VjaGVkID0gcmVjX2EudG91Y2hlZCgpCiAgICBmb3IgbmFtZSBpbiBzb3J0ZWQoZGVjbGFyZWQgLSB0b3VjaGVkKToK",
    "ICAgICAgICBwcm9ibGVtcy5hcHBlbmQoX3Byb2JsZW0oCiAgICAgICAgICAgICJERUNMQVJFRF9SRUFEX1VOQUNDRVNTRUQi",
    "LCBuYW1lLAogICAgICAgICAgICBmInRoZSBzcGVjIGRlY2xhcmVzIGEgcmVhZCBvZiB7bmFtZSFyfSBidXQgdGhlIGluamVj",
    "dGVkIHNvdXJjZSB3YXMgbmV2ZXIgYWNjZXNzZWQgIgogICAgICAgICAgICAiZHVyaW5nIGV4ZWN1dGlvbjsgdGhlIGRlY2xh",
    "cmF0aW9uIGlzIG5vdCBhIGRlc2NyaXB0aW9uIG9mIGJlaGF2aW91cjsgUkVGVVNFRCIpKQogICAgdW5kZWNsYXJlZCA9IHRv",
    "dWNoZWQgLSBkZWNsYXJlZCAtIG11c3Rfbm90CiAgICBmb3IgbmFtZSBpbiBzb3J0ZWQodW5kZWNsYXJlZCk6CiAgICAgICAg",
    "cHJvYmxlbXMuYXBwZW5kKF9wcm9ibGVtKAogICAgICAgICAgICAiVU5ERUNMQVJFRF9DSEFOTkVMX1JFQUQiLCBuYW1lLAog",
    "ICAgICAgICAgICBmInRoZSB3aXRuZXNzIHJlYWQge25hbWUhcn0sIHdoaWNoIGl0IGRvZXMgbm90IGRlY2xhcmU7IGFuIHVu",
    "ZGVjbGFyZWQgYXV0aG9yaXR5IGlzICIKICAgICAgICAgICAgIm91dHNpZGUgdGhlIHJldmlld2VkIGRlcGVuZGVuY3kgc3Ry",
    "dWN0dXJlOyBSRUZVU0VEIikpCiAgICBmb3IgbmFtZSBpbiBzb3J0ZWQodG91Y2hlZCAmIG11c3Rfbm90KToKICAgICAgICBw",
    "cm9ibGVtcy5hcHBlbmQoX3Byb2JsZW0oCiAgICAgICAgICAgICJBUlRFRkFDVF9VTkRFUl9URVNUX1JFQUQiLCBuYW1lLAog",
    "ICAgICAgICAgICBmInRoZSB3aXRuZXNzIHJlYWQgdGhlIGFydGVmYWN0IHVuZGVyIHRlc3Qge25hbWUhcn0gZGlyZWN0bHk7",
    "IFJFRlVTRUQiKSkKCiAgICAjIC0tLS0gSTMgTVVTVF9ERVBFTkQgcGVydHVyYmF0aW9uIC0tLS0tLS0tLS0tLS0tLS0tLS0t",
    "LS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLQogICAgc2Vuc2l0aXZpdHk6IGRpY3Rbc3RyLCBkaWN0XSA9IHt9",
    "CiAgICBmb3IgbmFtZSBpbiBzb3J0ZWQoZGVjbGFyZWQpOgogICAgICAgIGlmIGlzaW5zdGFuY2Uoc291cmNlc1tuYW1lXSwg",
    "ZGljdCk6CiAgICAgICAgICAgIHBlcnR1cmJlZCA9IHtrOiBfcGVydHVyYihzZXQodiksIG5vbmNlKSBpZiBpc2luc3RhbmNl",
    "KHYsIChzZXQsIGZyb3plbnNldCwgbGlzdCwgdHVwbGUpKQogICAgICAgICAgICAgICAgICAgICAgICAgZWxzZSB2IGZvciBr",
    "LCB2IGluIHNvdXJjZXNbbmFtZV0uaXRlbXMoKX0KICAgICAgICBlbHNlOgogICAgICAgICAgICBwZXJ0dXJiZWQgPSBfcGVy",
    "dHVyYihzZXQoc291cmNlc1tuYW1lXSksIG5vbmNlKQogICAgICAgIHRyaWFsX3NvdXJjZXMgPSBkaWN0KHNvdXJjZXMsICoq",
    "e25hbWU6IHBlcnR1cmJlZH0pCiAgICAgICAgY2Fub25fYywgXywgXywgZXJyID0gX3J1bih3aXRuZXNzLCBzcGVjLCB0cmlh",
    "bF9zb3VyY2VzLCBmInBlcnR1cmI6e25hbWV9IikKICAgICAgICAjIEEgY2FyZGluYWxpdHktcHJlc2VydmluZyBzd2FwIG11",
    "c3QgaW50cm9kdWNlIGEgdG9rZW4gZm9yZWlnbiB0byB0aGUgc291cmNlJ3MgZG9tYWluLAogICAgICAgICMgc28gYSB3aXRu",
    "ZXNzIHRoYXQgSU5ERVhFUyBpdHMgc291cmNlIChhIG1hcHBpbmcgbG9va3VwKSBsZWdpdGltYXRlbHkgcmFpc2VzLiBSYWlz",
    "aW5nIGlzCiAgICAgICAgIyBERVBFTkRFTkNFIC0tIHRoZSBwZXJ0dXJiZWQgdmFsdWUgZGVtb25zdHJhYmx5IHJlYWNoZWQg",
    "dGhlIHdpdG5lc3MncyBjb250cm9sL2RhdGEgZmxvdwogICAgICAgICMgLS0gbm90IGluZXJ0bmVzcy4gVGhlIGJhc2VsaW5l",
    "IGlzIGtub3duIHRvIHN1Y2NlZWQgKEkxKSwgc28gImFsd2F5cyByYWlzZXMiIGlzIG5vdCBhbgogICAgICAgICMgZXZhc2lv",
    "bi4gQ291bnRpbmcgYSBjcmFzaCBhcyBpbmVydCBwcm9kdWNlZCBhIEZBTFNFIFJFRlVTQUwgb2YgYW4gaG9uZXN0IHdpdG5l",
    "c3MuCiAgICAgICAgY29udGVudF9zZW5zaXRpdmUgPSBjYW5vbl9jICE9IGJhc2VfYQogICAgICAgIHdpdG5lc3NlZF9ieSA9",
    "ICJidWxrLXN3YXAiCiAgICAgICAgaWYgbm90IGNvbnRlbnRfc2Vuc2l0aXZlIGFuZCBub3QgaXNpbnN0YW5jZShzb3VyY2Vz",
    "W25hbWVdLCBkaWN0KToKICAgICAgICAgICAgIyBFU0NBTEFUSU9OLiBBIHdpdG5lc3MgbWF5IGxlZ2l0aW1hdGVseSBkZXBl",
    "bmQgb24gT05FIG1lbWJlciAoImlzIFggaW4gUz8iKSwgd2hpY2ggYQogICAgICAgICAgICAjIHNpbmdsZSBsb3dlc3QtbWVt",
    "YmVyIHN3YXAgbWlzc2VzIC0tIHRoYXQgd291bGQgYmUgYSBGQUxTRSByZWZ1c2FsIG9mIGFuIGhvbmVzdAogICAgICAgICAg",
    "ICAjIHNlbGVjdGl2ZSByZWFkZXIuIFJldHJ5IHBlciBtZW1iZXIsIGJvdW5kZWQgYnkgfFN8LCB1bnRpbCBzZW5zaXRpdml0",
    "eSBpcwogICAgICAgICAgICAjIGRlbW9uc3RyYXRlZC4gQWJzZW5jZSBvZiBzZW5zaXRpdml0eSBhZnRlciBleGhhdXN0aW5n",
    "IGV2ZXJ5IG1lbWJlciBpcyB0aGVuIGEgcmVhbAogICAgICAgICAgICAjIGZpbmRpbmcsIG5vdCBhbiBhcnRlZmFjdCBvZiB3",
    "aGljaCBtZW1iZXIgd2UgaGFwcGVuZWQgdG8gcGVydHVyYi4KICAgICAgICAgICAgZm9yIHZpY3RpbSBpbiBzb3J0ZWQoc2V0",
    "KHNvdXJjZXNbbmFtZV0pLCBrZXk9c3RyKVs6X1BFUlRVUkJBVElPTl9DQVBdOgogICAgICAgICAgICAgICAgb25lID0gc2V0",
    "KHNvdXJjZXNbbmFtZV0pCiAgICAgICAgICAgICAgICBvbmUuZGlzY2FyZCh2aWN0aW0pCiAgICAgICAgICAgICAgICBvbmUu",
    "YWRkKG5vbmNlKQogICAgICAgICAgICAgICAgYzEsIF8sIF8sIF8gPSBfcnVuKHdpdG5lc3MsIHNwZWMsIGRpY3Qoc291cmNl",
    "cywgKip7bmFtZTogb25lfSksCiAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgZiJwZXJ0dXJiMTp7bmFtZX06",
    "e3ZpY3RpbX0iKQogICAgICAgICAgICAgICAgaWYgYzEgIT0gYmFzZV9hOgogICAgICAgICAgICAgICAgICAgIGNvbnRlbnRf",
    "c2Vuc2l0aXZlLCB3aXRuZXNzZWRfYnkgPSBUcnVlLCBmIm1lbWJlcjp7dmljdGltfSIKICAgICAgICAgICAgICAgICAgICBi",
    "cmVhawogICAgICAgICMgc2Vjb25kIHZhcmlhbnQ6IGEgY2FyZGluYWxpdHkgY2hhbmdlLCBwdXJlbHkgZm9yIGRpYWdub3Np",
    "cyBvZiBzdW1tYXJ5LW9ubHkgY29uc3VtZXJzCiAgICAgICAgaWYgaXNpbnN0YW5jZShzb3VyY2VzW25hbWVdLCBkaWN0KToK",
    "ICAgICAgICAgICAgc2hvcnRlbmVkID0ge2s6IHYgZm9yIGssIHYgaW4gbGlzdChzb3VyY2VzW25hbWVdLml0ZW1zKCkpWzE6",
    "XX0KICAgICAgICBlbHNlOgogICAgICAgICAgICBzaG9ydGVuZWQgPSBzZXQoc29ydGVkKHNldChzb3VyY2VzW25hbWVdKSwg",
    "a2V5PXN0cilbMTpdKQogICAgICAgIGNhbm9uX3MsIF8sIF8sIF8gPSBfcnVuKHdpdG5lc3MsIHNwZWMsIGRpY3Qoc291cmNl",
    "cywgKip7bmFtZTogc2hvcnRlbmVkfSksCiAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgZiJzaG9ydGVuOntuYW1l",
    "fSIpCiAgICAgICAgY2FyZF9zZW5zaXRpdmUgPSBjYW5vbl9zICE9IGJhc2VfYQogICAgICAgIHNlbnNpdGl2aXR5W25hbWVd",
    "ID0geyJjb250ZW50X3NlbnNpdGl2ZSI6IGNvbnRlbnRfc2Vuc2l0aXZlLAogICAgICAgICAgICAgICAgICAgICAgICAgICAg",
    "ICJjYXJkaW5hbGl0eV9zZW5zaXRpdmUiOiBjYXJkX3NlbnNpdGl2ZSwKICAgICAgICAgICAgICAgICAgICAgICAgICAgICAi",
    "ZGVtb25zdHJhdGVkX2J5IjogKCJyYWlzZSIgaWYgZXJyIGlzIG5vdCBOb25lIGVsc2Ugd2l0bmVzc2VkX2J5KX0KICAgICAg",
    "ICBpZiBub3QgY29udGVudF9zZW5zaXRpdmU6CiAgICAgICAgICAgIGRldGFpbCA9IChmInRoZSBvYnNlcnZhdGlvbiBpcyBV",
    "TkNIQU5HRUQgd2hlbiB0aGUgZGVjbGFyZWQgc291cmNlIHtuYW1lIXJ9IGlzICIKICAgICAgICAgICAgICAgICAgICAgICJw",
    "ZXJ0dXJiZWQgYnkgYSBjYXJkaW5hbGl0eS1wcmVzZXJ2aW5nIGNvbnRlbnQgc3dhcDsgdGhlIGRlY2xhcmVkICIKICAgICAg",
    "ICAgICAgICAgICAgICAgICJkZXBlbmRlbmN5IGlzIElORVJUIC0tIHRoZSB3aXRuZXNzIGRvZXMgbm90IGFjdHVhbGx5IGRl",
    "cml2ZSBpdHMgIgogICAgICAgICAgICAgICAgICAgICAgIm9ic2VydmF0aW9uIGZyb20gaXQiKQogICAgICAgICAgICBpZiBj",
    "YXJkX3NlbnNpdGl2ZToKICAgICAgICAgICAgICAgIGRldGFpbCArPSAoIiAoaXQgSVMgc2Vuc2l0aXZlIHRvIHRoZSBzb3Vy",
    "Y2UncyBTSVpFOiBhIHN1bW1hcnktb25seSBjb25zdW1lciwgIgogICAgICAgICAgICAgICAgICAgICAgICAgICAid2hpY2gg",
    "aXMgZXhhY3RseSB3aGF0IGRlZmVhdHMgYWNjZXNzLXJlY29yZGluZyBhbG9uZSkiKQogICAgICAgICAgICBlbGlmIG5hbWUg",
    "aW4gdG91Y2hlZDoKICAgICAgICAgICAgICAgIGRldGFpbCArPSAoIiAodGhlIHNvdXJjZSBXQVMgdG91Y2hlZCAtLSB0b3Vj",
    "aGluZyBpcyBub3QgZGVwZW5kaW5nOyB0aGlzIGlzIHdoeSAiCiAgICAgICAgICAgICAgICAgICAgICAgICAgICJJMiBpcyBh",
    "IHNjcmVlbiBhbmQgSTMgaXMgdGhlIHByb29mKSIpCiAgICAgICAgICAgIHByb2JsZW1zLmFwcGVuZChfcHJvYmxlbSgiREVD",
    "TEFSRURfUkVBRF9JTkVSVCIsIG5hbWUsIGRldGFpbCArICI7IFJFRlVTRUQiKSkKCiAgICAjIC0tLS0gSTQgTVVTVF9OT1Rf",
    "REVQRU5EIGludmFyaWFuY2UgKyB0YWludCBjYW5hcnkgLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0K",
    "ICAgIGZvciBuYW1lIGluIHNvcnRlZChtdXN0X25vdCk6CiAgICAgICAgdGFyZ2V0ID0gX2FzX2tleShzb3VyY2VzW25hbWVd",
    "KQogICAgICAgIHRhaW50ZWQgPSBfcGVydHVyYihzZXQoc291cmNlc1tuYW1lXSksIG5vbmNlKQogICAgICAgIHRyaWFsX3Nw",
    "ZWMgPSBfc3Vic3RpdHV0ZShzcGVjLCB0YXJnZXQsIHRhaW50ZWQpICAgICAgICAgICMgZXZlcnkgY2hhbm5lbCBpdCByZWFj",
    "aGVzIGJ5CiAgICAgICAgdHJpYWxfc291cmNlcyA9IGRpY3Qoc291cmNlcywgKip7bmFtZTogdGFpbnRlZH0pCiAgICAgICAg",
    "Y2Fub25fbSwgcmF3X20sIF8sIGVyciA9IF9ydW4od2l0bmVzcywgc3BlYyBpZiB0cmlhbF9zcGVjIGlzIE5vbmUgZWxzZSB0",
    "cmlhbF9zcGVjLAogICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgIHRyaWFsX3NvdXJjZXMsIGYibXV0YXRl",
    "OntuYW1lfSIpCiAgICAgICAgaWYgZXJyIGlzIG5vdCBOb25lOgogICAgICAgICAgICBwcm9ibGVtcy5hcHBlbmQoX3Byb2Js",
    "ZW0oCiAgICAgICAgICAgICAgICAiV0lUTkVTU19SQUlTRURfVU5ERVJfTVVUQVRJT04iLCBuYW1lLAogICAgICAgICAgICAg",
    "ICAgZiJ0aGUgd2l0bmVzcyByYWlzZWQge3R5cGUoZXJyKS5fX25hbWVfX30gd2hlbiB7bmFtZSFyfSB3YXMgbXV0YXRlZDsg",
    "YSB3aXRuZXNzICIKICAgICAgICAgICAgICAgICJ3aG9zZSBleGVjdXRpb24gZGVwZW5kcyBvbiB0aGUgYXJ0ZWZhY3QgdW5k",
    "ZXIgdGVzdCBpcyBub3QgaW5kZXBlbmRlbnQ7IFJFRlVTRUQiKSkKICAgICAgICAgICAgY29udGludWUKICAgICAgICBpZiBu",
    "b25jZSBpbiBfbWVtYmVycyhyYXdfbSk6CiAgICAgICAgICAgIHByb2JsZW1zLmFwcGVuZChfcHJvYmxlbSgKICAgICAgICAg",
    "ICAgICAgICJUQUlOVF9DQU5BUllfRUNIT0VEIiwgbmFtZSwKICAgICAgICAgICAgICAgIGYiYSBub25jZSBwbGFudGVkIG9u",
    "bHkgaW4ge25hbWUhcn0gYXBwZWFyZWQgSU4gVEhFIE9CU0VSVkFUSU9OOyB0aGUgd2l0bmVzcyBpcyAiCiAgICAgICAgICAg",
    "ICAgICAiZWNob2luZyB0aGUgYXJ0ZWZhY3QgdW5kZXIgdGVzdC4gVGhpcyBpcyBwb3NpdGl2ZSBwcm9vZiwgbm90IGFuIGlu",
    "ZmVyZW5jZSwgYW5kICIKICAgICAgICAgICAgICAgICJpdCBzdXJ2aXZlcyBtZW1vaXNhdGlvbiBvZiB0aGUgYmFzZWxpbmUg",
    "cnVuOyBSRUZVU0VEIikpCiAgICAgICAgZWxpZiBjYW5vbl9tICE9IGJhc2VfYToKICAgICAgICAgICAgcm9sZSA9ICgidGhl",
    "IGNvbGxlY3Rpb24tdW5kZXItdGVzdCIgaWYgbmFtZSA9PSBjaWQgZWxzZQogICAgICAgICAgICAgICAgICAgICJleHBlY3Rl",
    "ZF9zb3VyY2UgKHRoZSBPVEhFUiBhdXRob3JpdHkgaW4gdGhlIGNvbXBhcmlzb24pIikKICAgICAgICAgICAgcHJvYmxlbXMu",
    "YXBwZW5kKF9wcm9ibGVtKAogICAgICAgICAgICAgICAgIk9CU0VSVkFUSU9OX1RSQUNLU19BUlRFRkFDVCIgaWYgbmFtZSA9",
    "PSBjaWQgZWxzZSAiT0JTRVJWRURfVFJBQ0tTX0VYUEVDVEVEIiwKICAgICAgICAgICAgICAgIG5hbWUsCiAgICAgICAgICAg",
    "ICAgICBmInRoZSBvYnNlcnZhdGlvbiBDSEFOR0VEIHdoZW4ge3JvbGV9IHdhcyBtdXRhdGVkOyBhbiBpbmRlcGVuZGVudCB3",
    "aXRuZXNzICIKICAgICAgICAgICAgICAgICJvYnNlcnZlcyBhbiBleHRlcm5hbCBhdXRob3JpdHkgYW5kIGlzIGludmFyaWFu",
    "dCB0byBpdC4gQSB3aXRuZXNzIHRoYXQgdHJhY2tzICIKICAgICAgICAgICAgICAgIGYie25hbWUhcn0gaXMgYSBzZWNvbmQg",
    "Y29weSBvZiBpdCBhbmQgY2FuIG5ldmVyIHJlcG9ydCBpdCBzaG9ydDsgUkVGVVNFRCIpKQoKICAgIGlmIERJU0FCTEVEOgog",
    "ICAgICAgIHByb2JsZW1zID0gW3AgZm9yIHAgaW4gcHJvYmxlbXMKICAgICAgICAgICAgICAgICAgICBpZiBJTlNUUlVNRU5U",
    "X09GLmdldChwWyJraW5kIl0sICJJMCIpIG5vdCBpbiBESVNBQkxFRF0KICAgIHJldHVybiBwcm9ibGVtcwoKCiMgPT09PT09",
    "PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09",
    "PT09PT09PT09PT09PT09PT09CiMgQkFUVEVSWQojID09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09",
    "PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PQpDSUQgPSAic2NyaXB0cy9l",
    "eGVjdXRlZF9jb2RlX3Byb3ZlbmFuY2UucHk6OkRFQ0xBUkVEX1BST1ZFTkFOQ0UiCkVYUEVDVEVEID0gInNjcmlwdHMvZXhl",
    "Y3V0ZWRfY29kZV9wcm92ZW5hbmNlLnB5OjpBVVRIT1JJVFkiClMxID0gInNjcmlwdHMvZXhlY3V0ZWRfY29kZV9wcm92ZW5h",
    "bmNlLnB5OjpQUk9WRU5BTkNFX1JFQ09SRFMiClMyID0gInNjcmlwdHMvZXhlY3V0YWJsZV9pbnZlbnRvcnkucHk6OklOVkVO",
    "VE9SWSIKUzMgPSAic2NyaXB0cy9zaXRlX3RheG9ub215LnB5OjpTSVRFUyIKCkNPTExFQ1RJT04gPSB7InNjcmlwdHMvc2Vz",
    "c2lvbl9maW5pc2gucHkiLCAic2NyaXB0cy9nZW5fYm91bmRhcnlfcG9saWN5LnB5IiwKICAgICAgICAgICAgICAic2NyaXB0",
    "cy9jaV9oYXJuZXNzLnB5In0KU09VUkNFUyA9IHsKICAgIENJRDogc2V0KENPTExFQ1RJT04pLAogICAgRVhQRUNURUQ6IHsi",
    "YXV0aF9hIiwgImF1dGhfYiIsICJhdXRoX2MifSwKICAgIFMxOiB7InJlY19hIiwgInJlY19iIiwgInJlY19jIn0sCiAgICBT",
    "MjogeyJpbnZfYSIsICJpbnZfYiJ9LAogICAgUzM6IHsic2l0ZV9hIiwgInNpdGVfYiJ9LAp9CgoKZGVmIF9pbnAoc3BlYywg",
    "bmFtZSk6CiAgICByZXR1cm4gc3BlY1siX3dpdG5lc3NfaW5wdXRzIl1bbmFtZV0KCgpkZWYgX2RpZ2VzdChtZW1iZXJzKSAt",
    "PiBzdHI6CiAgICByZXR1cm4gaGFzaGxpYi5zaGEyNTYoCiAgICAgICAganNvbi5kdW1wcyhzb3J0ZWQoc3RyKG0pIGZvciBt",
    "IGluIG1lbWJlcnMpLCBzZXBhcmF0b3JzPSgiLCIsICI6IikpLmVuY29kZSgpKS5oZXhkaWdlc3QoKVs6MTJdCgoKIyAtLS0g",
    "d2l0bmVzc2VzIC0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0t",
    "LS0tLS0tLS0tLS0tLS0tLS0tLS0KZGVmIHdfaG9uZXN0X3RocmVlKHNwZWMpOgogICAgIiIiUmVhZHMgYWxsIHRocmVlIGRl",
    "Y2xhcmVkIHNvdXJjZXMsIGNvbnRlbnQtc2Vuc2l0aXZlIHRvIGVhY2gsIGlnbm9yZXMgdGhlIGNvbGxlY3Rpb24uIiIiCiAg",
    "ICByZXR1cm4gc2V0KF9pbnAoc3BlYywgUzEpKSB8IHtmImk6e219IiBmb3IgbSBpbiBfaW5wKHNwZWMsIFMyKX0gfCB7CiAg",
    "ICAgICAgZiJ0OntfZGlnZXN0KF9pbnAoc3BlYywgUzMpKX0ifQoKCmRlZiB3X2hvbmVzdF9vbmUoc3BlYyk6CiAgICByZXR1",
    "cm4ge2YicDp7bX0iIGZvciBtIGluIF9pbnAoc3BlYywgUzEpfQoKCmRlZiB3X2hvbmVzdF9jb2luY2lkZXMoc3BlYyk6CiAg",
    "ICAiIiJIb25lc3QgZGVyaXZhdGlvbiB3aG9zZSBPVVRQVVQgSEFQUEVOUyBUTyBFUVVBTCB0aGUgY29sbGVjdGlvbi4gQ29u",
    "dGVudCBlcXVhbGl0eSBpcyBub3QKICAgIGV2aWRlbmNlIG9mIGVjaG8gLS0gb25seSBUUkFDS0lORyBpcy4gTXVzdCBQQVNT",
    "IChvdmVyLXJlZnVzYWwgY29udHJvbCkuIiIiCiAgICBtYXBwaW5nID0geyJyZWNfYSI6ICJzY3JpcHRzL3Nlc3Npb25fZmlu",
    "aXNoLnB5IiwKICAgICAgICAgICAgICAgInJlY19iIjogInNjcmlwdHMvZ2VuX2JvdW5kYXJ5X3BvbGljeS5weSIsCiAgICAg",
    "ICAgICAgICAgICJyZWNfYyI6ICJzY3JpcHRzL2NpX2hhcm5lc3MucHkifQogICAgcmV0dXJuIHttYXBwaW5nW21dIGZvciBt",
    "IGluIF9pbnAoc3BlYywgUzEpfQoKCmRlZiB3X3RocmVlX2NsYWltZWRfb25lX3JlYWQoc3BlYyk6CiAgICByZXR1cm4gc2V0",
    "KF9pbnAoc3BlYywgUzEpKQoKCmRlZiB3X3plcm9fcmVhZChzcGVjKToKICAgIHJldHVybiB7ImZhYnJpY2F0ZWRfYSIsICJm",
    "YWJyaWNhdGVkX2IifQoKCmRlZiB3X2V4cGVjdGVkX3JldXNlZChzcGVjKToKICAgICIiIkhvbmVzdGx5IHJlYWRzIFMxIChz",
    "byBJMyBpcyBjbGVhbikgQU5EIHJldXNlcyBleHBlY3RlZF9zb3VyY2UgYXMgaXRzIG9ic2VydmF0aW9uLiIiIgogICAgcmV0",
    "dXJuIHNldChfaW5wKHNwZWMsIFMxKSkgfCBzZXQoX2lucChzcGVjLCBFWFBFQ1RFRCkpCgoKZGVmIHdfdW5pb25fcGFydGlh",
    "bChzcGVjKToKICAgICIiIkRlY2xhcmVzIHRoZSB1bmlvbiBvZiB0d28gYXV0aG9yaXRpZXMsIGV4ZWN1dGVzIG9uZSBicmFu",
    "Y2guIiIiCiAgICByZXR1cm4gc2V0KF9pbnAoc3BlYywgUzEpKQoKCmRlZiB3X2RpZmZlcmVudGlhbF9vbmVfc2lkZShzcGVj",
    "KToKICAgICIiIkRlY2xhcmVzIGEgYmFzZWxpbmUgc2lkZSBhbmQgYSBtdXRhdGVkIHNpZGUsIGV4ZWN1dGVzIG9ubHkgdGhl",
    "IGJhc2VsaW5lIHNpZGUuIiIiCiAgICByZXR1cm4ge2YiZWZmZWN0OnttfSIgZm9yIG0gaW4gX2lucChzcGVjLCBTMSl9CgoK",
    "ZGVmIHdfcHJvdmVuYW5jZV9wYXJlbnRfaWdub3JlZChzcGVjKToKICAgICIiIkRlY2xhcmVzIGNoaWxkICsgcGFyZW50IHBy",
    "b3ZlbmFuY2UsIGRlcml2ZXMgZnJvbSB0aGUgY2hpbGQgb25seS4iIiIKICAgIHJldHVybiB7ZiJwcm92OnttfSIgZm9yIG0g",
    "aW4gX2lucChzcGVjLCBTMSl9CgoKZGVmIHdfcGFydGl0aW9uX3N1bW1hcnlfb25seShzcGVjKToKICAgICIiIlRvdWNoZXMg",
    "ZXZlcnkgZGVjbGFyZWQgc291cmNlIGJ1dCBjb25zdW1lcyBvbmx5IGEgU1VNTUFSWSAobGVuKS4gRGVmZWF0cyBJMiwgY2F1",
    "Z2h0IGJ5CiAgICB0aGUgY2FyZGluYWxpdHktUFJFU0VSVklORyBwZXJ0dXJiYXRpb24gaW4gSTMuIiIiCiAgICByZXR1cm4g",
    "e2YiY291bnQ6e2xlbihfaW5wKHNwZWMsIFMxKSkgKyBsZW4oX2lucChzcGVjLCBTMikpfSJ9CgoKZGVmIHdfY29uc3RhbnQo",
    "c3BlYyk6CiAgICAiIiJQZXJmZWN0bHkgaW52YXJpYW50IChJNCBjbGVhbikgYW5kIHJlYWRzIG5vdGhpbmcgKEkzIGZpcmVz",
    "KS4gUHJvdmVzIEk0IGFsb25lIGlzIG5vdAogICAgc3VmZmljaWVudC4iIiIKICAgIHJldHVybiB7ImFsd2F5cyIsICJ0aGUi",
    "LCAic2FtZSJ9CgoKZGVmIHdfZWNob19jb2xsZWN0aW9uKHNwZWMpOgogICAgIiIiUmVhZHMgUzEgaG9uZXN0bHkgQU5EIGVj",
    "aG9lcyB0aGUgY29sbGVjdGlvbi11bmRlci10ZXN0IHRocm91Z2ggdGhlIGluamVjdGVkIGNoYW5uZWwuIiIiCiAgICByZXR1",
    "cm4gc2V0KF9pbnAoc3BlYywgUzEpKSB8IHNldChfaW5wKHNwZWMsIENJRCkpCgoKZGVmIHdfcm91dGVyX2xhdW5kZXJlZF9l",
    "Y2hvKHNwZWMpOgogICAgIiIiVGhlIGJhbmtlZCBFTEVWRU5USF9WQUxJRF9TSEFQRURfQlVUX0xZSU5HOiBldmVyeSBndWFy",
    "ZC1hY3RpdmF0aW5nIGZpZWxkIHByZXNlbnQgYW5kCiAgICB3ZWxsLWZvcm1lZDsgdGhlIG9ic2VydmF0aW9uIGlzIGNob3Nl",
    "biBhdCBjYWxsIHRpbWUgYnkgYSByb3V0aW5nIHRhYmxlIG5vIGd1YXJkIGluc3BlY3RzCiAgICBhbmQgaXMgbGF1bmRlcmVk",
    "IG91dCBvZiBhIHNwZWMgcGF5bG9hZCBob2xkaW5nIGEgY29weSBvZiB0aGUgY29sbGVjdGlvbi4iIiIKICAgIHJvdXRlID0g",
    "c3BlY1sid2l0bmVzc19yb3V0ZSJdCiAgICBwYXlsb2FkID0gc3BlY1sicm91dGVfcGF5bG9hZCJdCiAgICBkZWxlZ2F0ZXMg",
    "PSB7CiAgICAgICAgImVjaG9fZGVjbGFyZWQiOiBsYW1iZGEgcDogc2V0KHBbImVjaG9fb2ZfY29sbGVjdGlvbiJdKSwKICAg",
    "ICAgICAicmVhbF9kZXJpdmF0aW9uIjogbGFtYmRhIHA6IHtmInA6e219IiBmb3IgbSBpbiBfaW5wKHNwZWMsIFMxKX0sCiAg",
    "ICB9CiAgICBmb3IgY2FuZGlkYXRlIGluIHJvdXRlWyJjYW5kaWRhdGVzIl06CiAgICAgICAgZm4gPSBkZWxlZ2F0ZXMuZ2V0",
    "KGNhbmRpZGF0ZSkKICAgICAgICBpZiBmbiBpcyBub3QgTm9uZToKICAgICAgICAgICAgcmV0dXJuIGZuKHBheWxvYWQpCiAg",
    "ICByYWlzZSBSdW50aW1lRXJyb3IoIm5vIHJvdXRlIikKCgpkZWYgd19kaWdlc3RfZWNob192aWFfcGF5bG9hZChzcGVjKToK",
    "ICAgICIiIlRyYWNrcyB0aGUgY29sbGVjdGlvbi11bmRlci10ZXN0IFdJVEhPVVQgZW1pdHRpbmcgYW55IG9mIGl0cyBtZW1i",
    "ZXJzOiBpdCByZWFjaGVzIHRoZQogICAgY29sbGVjdGlvbiB0aHJvdWdoIGEgc3BlYyBwYXlsb2FkIChub3QgYW4gaW5qZWN0",
    "ZWQgY2hhbm5lbCwgc28gbm8gZGlyZWN0LXJlYWQgZmluZGluZykgYW5kCiAgICByZXR1cm5zIG9ubHkgYSBESUdFU1Qgb2Yg",
    "aXQgKHNvIG5vIGNhbmFyeSBjYW4gbGVhaykuIElzb2xhdGVzIHRoZSBpbnZhcmlhbmNlIGxpbWIgb2YgSTQgZnJvbQogICAg",
    "aXRzIGNhbmFyeSBsaW1iIC0tIGlmIG9ubHkgdGhlIGNhbmFyeSB3b3JrZWQsIHRoaXMgYXJtIHdvdWxkIHBhc3MuIiIiCiAg",
    "ICByZXR1cm4ge2YicDp7bX0iIGZvciBtIGluIF9pbnAoc3BlYywgUzEpfSB8IHsKICAgICAgICBmImQ6e19kaWdlc3Qoc3Bl",
    "Y1sncm91dGVfcGF5bG9hZCddWydlY2hvX29mX2NvbGxlY3Rpb24nXSl9In0KCgpkZWYgd19zZWxlY3RpdmVfcmVhZGVyKHNw",
    "ZWMpOgogICAgIiIiQW4gSE9ORVNUIHdpdG5lc3MgdGhhdCBkZXBlbmRzIG9uIGV4YWN0bHkgT05FIG1lbWJlciBvZiBpdHMg",
    "ZGVjbGFyZWQgc291cmNlICgiaXMgcmVjX2MKICAgIHByZXNlbnQ/IikuIFRoZSBidWxrIHN3YXAgcGVydHVyYnMgdGhlIGxl",
    "eGljb2dyYXBoaWNhbGx5LWxvd2VzdCBtZW1iZXIgYW5kIG1pc3NlcyBpdDsgb25seQogICAgdGhlIHBlci1tZW1iZXIgZXNj",
    "YWxhdGlvbiBpbiBJMyBkZW1vbnN0cmF0ZXMgdGhlIGRlcGVuZGVuY3kuIFdpdGhvdXQgdGhlIGVzY2FsYXRpb24gdGhpcwog",
    "ICAgaG9uZXN0IHdpdG5lc3MgaXMgRkFMU0VMWSByZWZ1c2VkIGFzIGluZXJ0LiIiIgogICAgcmV0dXJuIHsicHJlc2VudCIg",
    "aWYgInJlY19jIiBpbiBfaW5wKHNwZWMsIFMxKSBlbHNlICJhYnNlbnQifQoKCmRlZiB3X3VuZGVjbGFyZWRfYXV0aG9yaXR5",
    "KHNwZWMpOgogICAgIiIiSG9uZXN0bHkgZGVwZW5kcyBvbiBpdHMgT05FIGRlY2xhcmVkIHNvdXJjZSAoSTMgY2xlYW4pIGFu",
    "ZCBpcyBpbnZhcmlhbnQgdG8gYm90aCBhcnRlZmFjdHMKICAgIHVuZGVyIHRlc3QgKEk0IGNsZWFuKSAtLSBidXQgQUxTTyBk",
    "ZXJpdmVzIGZyb20gYW4gYXV0aG9yaXR5IGl0IG5ldmVyIGRlY2xhcmVzLiBUaGF0IHRoaXJkCiAgICBjaGFubmVsIGlzIGlu",
    "IG5laXRoZXIgcG9sYXJpdHkgY2xhc3MsIHNvIG5vIHBlcnR1cmJhdGlvbiB0cmlhbCBjYW4gc2VlIGl0cyBpbmZsdWVuY2U6",
    "IGl0IGlzCiAgICByZWFjaGFibGUgT05MWSBieSBhY2Nlc3MgcmVjb3JkaW5nLiBUaGlzIGlzIHRoZSBhcm0gdGhhdCBwcm92",
    "ZXMgSTIgaXMgbG9hZC1iZWFyaW5nIHJhdGhlcgogICAgdGhhbiBzdWJzdW1lZCBieSBJMy4iIiIKICAgIHJldHVybiB7ZiJw",
    "OnttfSIgZm9yIG0gaW4gX2lucChzcGVjLCBTMSl9IHwge2YidTp7bX0iIGZvciBtIGluIF9pbnAoc3BlYywgUzIpfQoKCmRl",
    "ZiB3X2RpZ2VzdF9lY2hvX29mX2V4cGVjdGVkKHNwZWMpOgogICAgIiIiU2FtZSwgZm9yIHRoZSBPVEhFUiBhdXRob3JpdHkg",
    "aW4gdGhlIGNvbXBhcmlzb246IHRoZSBvYnNlcnZhdGlvbiBpcyBhIGZ1bmN0aW9uIG9mCiAgICBleHBlY3RlZF9zb3VyY2Us",
    "IHNvIHRoZSB3aXRuZXNzIGlzIGFuIGFsaWFzIG9mIGl0IHJhdGhlciB0aGFuIGEgdGhpcmQgYXV0aG9yaXR5LiIiIgogICAg",
    "cmV0dXJuIHtmInA6e219IiBmb3IgbSBpbiBfaW5wKHNwZWMsIFMxKX0gfCB7ZiJkOntfZGlnZXN0KF9pbnAoc3BlYywgRVhQ",
    "RUNURUQpKX0ifQoKCl9ORCA9IHJhbmRvbS5SYW5kb20oNykKCgpkZWYgd19ub25kZXRlcm1pbmlzdGljKHNwZWMpOgogICAg",
    "YmFzZSA9IHNvcnRlZChfaW5wKHNwZWMsIFMxKSkKICAgIHJldHVybiBzZXQoX05ELnNhbXBsZShiYXNlLCBfTkQucmFuZGlu",
    "dCgxLCBsZW4oYmFzZSkpKSkKCgojIC0tLSBhcm1zIC0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0t",
    "LS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLQpkZWYgX3NwZWMocmVhZHMsICoqZXh0cmEp",
    "OgogICAgcyA9IHsicmVsYXRpb24iOiAiUFJPVkVOQU5DRV9DT1JSRVNQT05ERU5DRSIsICJjb3JyZXNwb25kZW5jZSI6ICJi",
    "aWplY3RpdmUiLAogICAgICAgICAicHJvdmlkZXIiOiAidyIsICJyZWFkcyI6IHJlYWRzLAogICAgICAgICAibG9hZF9iZWFy",
    "aW5nX2RpcmVjdGlvbiI6IFsibWlzc2luZyIsICJ1bmtub3duIl0sCiAgICAgICAgICJwcmVzZW5jZSI6IHsicG9saWN5Ijog",
    "IklOVkFMSURfRU1QVFkiLCAib3BlcmFuZCI6ICJib3RoIn19CiAgICBzLnVwZGF0ZShleHRyYSkKICAgIHJldHVybiBzCgoK",
    "QVJNUyA9IFsKICAgICMgKG5hbWUsIHdpdG5lc3MsIHNwZWMsIGV4cGVjdGVkIHZlcmRpY3QsIGV4cGVjdGVkIHByb2JsZW0g",
    "a2luZCkKICAgICgiaG9uZXN0X3RocmVlX3NvdXJjZXNfUEFTU0VTIiwgd19ob25lc3RfdGhyZWUsIF9zcGVjKFtTMSwgUzIs",
    "IFMzXSksICJQQVNTIiwgTm9uZSksCiAgICAoImhvbmVzdF9zaW5nbGVfc291cmNlX1BBU1NFUyIsIHdfaG9uZXN0X29uZSwg",
    "X3NwZWMoUzEpLCAiUEFTUyIsIE5vbmUpLAogICAgKCJob25lc3Rfb3V0cHV0X2NvaW5jaWRlc193aXRoX2NvbGxlY3Rpb25f",
    "UEFTU0VTIiwgd19ob25lc3RfY29pbmNpZGVzLCBfc3BlYyhTMSksICJQQVNTIiwgTm9uZSksCiAgICAoInRocmVlX2NsYWlt",
    "ZWRfb25lX3JlYWQiLCB3X3RocmVlX2NsYWltZWRfb25lX3JlYWQsIF9zcGVjKFtTMSwgUzIsIFMzXSksICJSRUZVU0UiLAog",
    "ICAgICJERUNMQVJFRF9SRUFEX0lORVJUIiksCiAgICAoIm5fY2xhaW1lZF96ZXJvX3JlYWQiLCB3X3plcm9fcmVhZCwgX3Nw",
    "ZWMoW1MxLCBTMl0pLCAiUkVGVVNFIiwgIkRFQ0xBUkVEX1JFQURfVU5BQ0NFU1NFRCIpLAogICAgKCJleHBlY3RlZF9yZXVz",
    "ZWRfYXNfb2JzZXJ2ZWQiLCB3X2V4cGVjdGVkX3JldXNlZCwgX3NwZWMoUzEpLCAiUkVGVVNFIiwKICAgICAiVEFJTlRfQ0FO",
    "QVJZX0VDSE9FRCIpLAogICAgIyBpc29sYXRlcyB0aGUgSU5WQVJJQU5DRSBsaW1iIG9mIEk0IGZyb20gaXRzIENBTkFSWSBs",
    "aW1iOiBubyBtZW1iZXIgb2YgdGhlIGFydGVmYWN0IHVuZGVyCiAgICAjIHRlc3QgYXBwZWFycyBpbiB0aGUgb2JzZXJ2YXRp",
    "b24sIGFuZCB0aGUgYXJ0ZWZhY3QgaXMgcmVhY2hlZCB0aHJvdWdoIGEgc3BlYyBwYXlsb2FkIHJhdGhlcgogICAgIyB0aGFu",
    "IGFuIGluamVjdGVkIGNoYW5uZWwsIHNvIG5laXRoZXIgdGhlIGNhbmFyeSBjaGVjayBub3IgdGhlIGRpcmVjdC1yZWFkIGNo",
    "ZWNrIGNhbiBmaXJlLgogICAgKCJjb2xsZWN0aW9uX3RyYWNrZWRfYnlfZGlnZXN0X29ubHkiLCB3X2RpZ2VzdF9lY2hvX3Zp",
    "YV9wYXlsb2FkLAogICAgIF9zcGVjKFMxLCByb3V0ZV9wYXlsb2FkPXsiZWNob19vZl9jb2xsZWN0aW9uIjogc29ydGVkKENP",
    "TExFQ1RJT04pfSksICJSRUZVU0UiLAogICAgICJPQlNFUlZBVElPTl9UUkFDS1NfQVJURUZBQ1QiKSwKICAgICgiZXhwZWN0",
    "ZWRfdHJhY2tlZF9ieV9kaWdlc3Rfb25seSIsIHdfZGlnZXN0X2VjaG9fb2ZfZXhwZWN0ZWQsIF9zcGVjKFMxKSwgIlJFRlVT",
    "RSIsCiAgICAgIk9CU0VSVkVEX1RSQUNLU19FWFBFQ1RFRCIpLAogICAgKCJ1bmlvbl9wYXJ0aWFsIiwgd191bmlvbl9wYXJ0",
    "aWFsLCBfc3BlYyhbUzEsIFMyXSksICJSRUZVU0UiLCAiREVDTEFSRURfUkVBRF9JTkVSVCIpLAogICAgKCJkaWZmZXJlbnRp",
    "YWxfb25lX3NpZGUiLCB3X2RpZmZlcmVudGlhbF9vbmVfc2lkZSwgX3NwZWMoW1MxLCBTMl0pLCAiUkVGVVNFIiwKICAgICAi",
    "REVDTEFSRURfUkVBRF9JTkVSVCIpLAogICAgKCJwcm92ZW5hbmNlX3BhcmVudF9pZ25vcmVkIiwgd19wcm92ZW5hbmNlX3Bh",
    "cmVudF9pZ25vcmVkLCBfc3BlYyhbUzEsIFMyXSksICJSRUZVU0UiLAogICAgICJERUNMQVJFRF9SRUFEX0lORVJUIiksCiAg",
    "ICAoInBhcnRpdGlvbl9zdW1tYXJ5X29ubHkiLCB3X3BhcnRpdGlvbl9zdW1tYXJ5X29ubHksIF9zcGVjKFtTMSwgUzJdKSwg",
    "IlJFRlVTRSIsCiAgICAgIkRFQ0xBUkVEX1JFQURfSU5FUlQiKSwKICAgICgiY29uc3RhbnRfd2l0bmVzc19pbnZhcmlhbnRf",
    "YnV0X3JlYWRzX25vdGhpbmciLCB3X2NvbnN0YW50LCBfc3BlYyhbUzFdKSwgIlJFRlVTRSIsCiAgICAgIkRFQ0xBUkVEX1JF",
    "QURfVU5BQ0NFU1NFRCIpLAogICAgKCJlY2hvX29mX2NvbGxlY3Rpb25fdW5kZXJfdGVzdCIsIHdfZWNob19jb2xsZWN0aW9u",
    "LCBfc3BlYyhTMSksICJSRUZVU0UiLAogICAgICJUQUlOVF9DQU5BUllfRUNIT0VEIiksCiAgICAoImVsZXZlbnRoX3ZhbGlk",
    "X3NoYXBlZF9idXRfbHlpbmdfcmVhZHMiLCB3X3JvdXRlcl9sYXVuZGVyZWRfZWNobywKICAgICBfc3BlYyhTMSwgd2l0bmVz",
    "c19yb3V0ZT17InN0cmF0ZWd5IjogImZpcnN0X2F2YWlsYWJsZSIsICJjYW5kaWRhdGVzIjogWyJlY2hvX2RlY2xhcmVkIl19",
    "LAogICAgICAgICAgIHJvdXRlX3BheWxvYWQ9eyJlY2hvX29mX2NvbGxlY3Rpb24iOiBzb3J0ZWQoQ09MTEVDVElPTil9KSwK",
    "ICAgICAiUkVGVVNFIiwgIlRBSU5UX0NBTkFSWV9FQ0hPRUQiKSwKICAgICgiZWxldmVudGhfaG9uZXN0X3JvdXRlX3N0aWxs",
    "X25lZWRzX2FfcmVhbF9yZWFkIiwgd19yb3V0ZXJfbGF1bmRlcmVkX2VjaG8sCiAgICAgX3NwZWMoUzEsIHdpdG5lc3Nfcm91",
    "dGU9eyJzdHJhdGVneSI6ICJmaXJzdF9hdmFpbGFibGUiLCAiY2FuZGlkYXRlcyI6IFsicmVhbF9kZXJpdmF0aW9uIl19LAog",
    "ICAgICAgICAgIHJvdXRlX3BheWxvYWQ9e30pLCAiUEFTUyIsIE5vbmUpLAogICAgKCJub25kZXRlcm1pbmlzdGljX3dpdG5l",
    "c3MiLCB3X25vbmRldGVybWluaXN0aWMsIF9zcGVjKFMxKSwgIlJFRlVTRSIsCiAgICAgIk5PTkRFVEVSTUlOSVNUSUNfV0lU",
    "TkVTUyIpLAogICAgKCJ1bmRlY2xhcmVkX2F1dGhvcml0eV9yZWFkIiwgd191bmRlY2xhcmVkX2F1dGhvcml0eSwgX3NwZWMo",
    "UzEpLCAiUkVGVVNFIiwKICAgICAiVU5ERUNMQVJFRF9DSEFOTkVMX1JFQUQiKSwKICAgICgiaG9uZXN0X3NlbGVjdGl2ZV9z",
    "aW5nbGVfbWVtYmVyX3JlYWRlcl9QQVNTRVMiLCB3X3NlbGVjdGl2ZV9yZWFkZXIsIF9zcGVjKFMxKSwgIlBBU1MiLCBOb25l",
    "KSwKICAgICgicmVhZHNfdW5kZWNsYXJlZF9lbnRpcmVseSIsIHdfaG9uZXN0X29uZSwge2s6IHYgZm9yIGssIHYgaW4gX3Nw",
    "ZWMoUzEpLml0ZW1zKCkKICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgIGlmIGsgIT0g",
    "InJlYWRzIn0sICJSRUZVU0UiLCAiUkVBRFNfVU5ERUNMQVJFRCIpLAogICAgKCJkZWNsYXJlc19hX3JlYWRfb2ZfdGhlX2Nv",
    "bGxlY3Rpb25faXRzZWxmIiwgd19lY2hvX2NvbGxlY3Rpb24sIF9zcGVjKFtTMSwgQ0lEXSksICJSRUZVU0UiLAogICAgICJD",
    "T1BJRURfT1JBQ0xFX0RFQ0xBUkVEIiksCl0KCkk1X0FSTVMgPSBbCiAgICAoImRpZ2VzdF9zdXBwbGllZCIsIHsib2JzZXJ2",
    "ZWRfaGFzaCI6ICJkZWFkYmVlZiIsICJiYXNlbGluZV9oYXNoIjogImNhZmUifSwgIkhBU0hfQkFDS1NUT1AiLAogICAgICJS",
    "RUZVU0UiLCAiU1VQUExJRURfRVZJREVOQ0VfTElURVJBTCIpLAogICAgKCJwYXJ0aXRpb25fbWVtYmVyc19zdXBwbGllZCIs",
    "IHsicGFydGl0aW9uX21lbWJlcnMiOiBbWyJhIiwgImIiXV19LCAiUEFSVElUSU9OIiwgIlJFRlVTRSIsCiAgICAgIlNVUFBM",
    "SUVEX0VWSURFTkNFX0xJVEVSQUwiKSwKICAgICgibWVtYmVyX2VmZmVjdF9zdXBwbGllZCIsIHsibWVtYmVyX2VmZmVjdCI6",
    "IHsibSI6IFRydWV9LCAiYmFzZWxpbmVfaGVhbHRoeSI6IFRydWV9LAogICAgICJESUZGRVJFTlRJQUxfRVhFQ1VUSU9OIiwg",
    "IlJFRlVTRSIsICJTVVBQTElFRF9FVklERU5DRV9MSVRFUkFMIiksCiAgICAoInVua25vd25fcHJvYmVfYWNjZXB0ZWRfc3Vw",
    "cGxpZWQiLCB7InVua25vd25fcHJvYmVfYWNjZXB0ZWQiOiBGYWxzZX0sICJTQ0hFTUFfU1RSSUNUTkVTUyIsCiAgICAgIlJF",
    "RlVTRSIsICJTVVBQTElFRF9FVklERU5DRV9MSVRFUkFMIiksCiAgICAoImlubGluZV9vYnNlcnZlZF9zdXBwbGllZCIsIHsi",
    "b2JzZXJ2ZWQiOiB7ImEifX0sICJFWEFDVCIsICJSRUZVU0UiLAogICAgICJTVVBQTElFRF9FVklERU5DRV9MSVRFUkFMIiks",
    "CiAgICAoImVtcHR5X2NvbmRpdGlvbl9tZXRfc3VwcGxpZWQiLCB7ImVtcHR5X2NvbmRpdGlvbl9tZXQiOiBUcnVlfSwgIkVY",
    "QUNUIiwgIlJFRlVTRSIsCiAgICAgIlNVUFBMSUVEX0VWSURFTkNFX0xJVEVSQUwiKSwKICAgICgiZ3JvdW5kc19zZWxmX3Jl",
    "cG9ydGVkIiwgeyJncm91bmRzIjogOTk5OX0sICJDUk9TU19TT1VSQ0VfUkVRVUlSRU1FTlQiLCAiUkVGVVNFIiwKICAgICAi",
    "U1VQUExJRURfRVZJREVOQ0VfTElURVJBTCIpLAogICAgKCJpZHNfb25seV9zcGVjX1BBU1NFUyIsIHsicGFydGl0aW9uX21l",
    "bWJlcl9pZHMiOiBbIng6OkEiLCAieDo6QiJdfSwgIlBBUlRJVElPTiIsICJQQVNTIiwKICAgICBOb25lKSwKICAgICgicG9s",
    "aWN5X2FuZF9pZGVudGlmaWVyX2ZpZWxkc19QQVNTIiwgeyJwcmVzZW5jZSI6ICJJTlZBTElEX0VNUFRZIiwgInJlbGF0aW9u",
    "IjogIkVYQUNUIiwKICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICJwcm92aWRlciI6ICJwIiwg",
    "InJlYWRzIjogIng6OlMifSwgIkVYQUNUIiwgIlBBU1MiLAogICAgIE5vbmUpLApdCgoKZGVmIHJ1bl9iYXR0ZXJ5KCkgLT4g",
    "ZGljdDoKICAgIHJlc3VsdHMgPSBbXQogICAgZm9yIG5hbWUsIHdpdG5lc3MsIHNwZWMsIGV4cGVjdGVkLCBleHBlY3RlZF9r",
    "aW5kIGluIEFSTVM6CiAgICAgICAgcHJvYnMgPSB2ZXJpZnlfY29tcHV0YXRpb25hbF9pbmRlcGVuZGVuY2UoCiAgICAgICAg",
    "ICAgIHdpdG5lc3MsIHNwZWMsIENPTExFQ1RJT04sIGNpZD1DSUQsIHNvdXJjZXM9U09VUkNFUywgZXhwZWN0ZWRfc291cmNl",
    "X2lkPUVYUEVDVEVEKQogICAgICAgIGdvdCA9ICJSRUZVU0UiIGlmIHByb2JzIGVsc2UgIlBBU1MiCiAgICAgICAga2luZHMg",
    "PSBzb3J0ZWQoe3BbImtpbmQiXSBmb3IgcCBpbiBwcm9ic30pCiAgICAgICAgcm93ID0geyJhcm0iOiBuYW1lLCAiaW5zdHJ1",
    "bWVudCI6ICJJMS1JNCIsICJleHBlY3RlZCI6IGV4cGVjdGVkLCAiZ290IjogZ290LAogICAgICAgICAgICAgICAia2luZHMi",
    "OiBraW5kcywgInZlcmRpY3RfbWF0Y2giOiBnb3QgPT0gZXhwZWN0ZWQsCiAgICAgICAgICAgICAgICJleHBlY3RlZF9raW5k",
    "IjogZXhwZWN0ZWRfa2luZCwKICAgICAgICAgICAgICAgImV4cGVjdGVkX2tpbmRfcHJlc2VudCI6IChleHBlY3RlZF9raW5k",
    "IGlzIE5vbmUgb3IgZXhwZWN0ZWRfa2luZCBpbiBraW5kcyksCiAgICAgICAgICAgICAgICJkZXRhaWwiOiBbcFsiZGV0YWls",
    "Il0gZm9yIHAgaW4gcHJvYnNdWzoyXX0KICAgICAgICByb3dbIm9rIl0gPSByb3dbInZlcmRpY3RfbWF0Y2giXSBhbmQgcm93",
    "WyJleHBlY3RlZF9raW5kX3ByZXNlbnQiXQogICAgICAgIHJlc3VsdHMuYXBwZW5kKHJvdykKCiAgICBmb3IgbmFtZSwgc3Bl",
    "YywgcmVsLCBleHBlY3RlZCwgZXhwZWN0ZWRfa2luZCBpbiBJNV9BUk1TOgogICAgICAgIHByb2JzID0gdmVyaWZ5X2Rlcml2",
    "ZWRfb3BlcmFuZHMoc3BlYywgcmVsKQogICAgICAgIGdvdCA9ICJSRUZVU0UiIGlmIHByb2JzIGVsc2UgIlBBU1MiCiAgICAg",
    "ICAga2luZHMgPSBzb3J0ZWQoe3BbImtpbmQiXSBmb3IgcCBpbiBwcm9ic30pCiAgICAgICAgcm93ID0geyJhcm0iOiBuYW1l",
    "LCAiaW5zdHJ1bWVudCI6ICJJNSIsICJleHBlY3RlZCI6IGV4cGVjdGVkLCAiZ290IjogZ290LCAia2luZHMiOiBraW5kcywK",
    "ICAgICAgICAgICAgICAgInZlcmRpY3RfbWF0Y2giOiBnb3QgPT0gZXhwZWN0ZWQsICJleHBlY3RlZF9raW5kIjogZXhwZWN0",
    "ZWRfa2luZCwKICAgICAgICAgICAgICAgImV4cGVjdGVkX2tpbmRfcHJlc2VudCI6IChleHBlY3RlZF9raW5kIGlzIE5vbmUg",
    "b3IgZXhwZWN0ZWRfa2luZCBpbiBraW5kcyksCiAgICAgICAgICAgICAgICJkZXRhaWwiOiBbcFsiZGV0YWlsIl0gZm9yIHAg",
    "aW4gcHJvYnNdWzoyXX0KICAgICAgICByb3dbIm9rIl0gPSByb3dbInZlcmRpY3RfbWF0Y2giXSBhbmQgcm93WyJleHBlY3Rl",
    "ZF9raW5kX3ByZXNlbnQiXQogICAgICAgIHJlc3VsdHMuYXBwZW5kKHJvdykKICAgIHJldHVybiB7InJlc3VsdHMiOiByZXN1",
    "bHRzLAogICAgICAgICAgICAicGFzc2VkIjogc3VtKDEgZm9yIHIgaW4gcmVzdWx0cyBpZiByWyJvayJdKSwKICAgICAgICAg",
    "ICAgInRvdGFsIjogbGVuKHJlc3VsdHMpfQoKCmlmIF9fbmFtZV9fID09ICJfX21haW5fXyI6CiAgICBvdXQgPSBydW5fYmF0",
    "dGVyeSgpCiAgICBwcmludChqc29uLmR1bXBzKG91dCwgaW5kZW50PTEsIGRlZmF1bHQ9c3RyKSkK",
]))

# ================================================================================================
# P6 GRAFT — COMPUTATIONAL INDEPENDENCE (RWCC-1 static purity + I1–I5 dynamic instrument)
# ================================================================================================
# Stage-1 shipped `_P6_HOOK_NAME` as a FAIL-CLOSED empty slot: with nothing bound, a code-native
# witness is refused WITNESS_INDEPENDENCE_UNVERIFIED.  This block binds the REAL implementation, so
# the refusal path stays exactly where it was and the accept path is now earned rather than
# assumed.  Nothing below weakens the unbound posture — delete the binding and the slot fails
# closed again, which is what `test_p6_hook_unbound_refuses_a_code_native_witness` pins.
#
# TWO HALVES, NEITHER SUFFICIENT (the interlock, executed in the battery):
#   STATIC  (RWCC-1, p6_ast_purity)  closes the AMBIENT channels the dynamic instrument cannot
#           enumerate — a module global holding a second copy of the collection is invisible to
#           perturbation because there is no channel to perturb.  That is P6-LIM-02 itself.
#   DYNAMIC (I1–I5, p6_computational_independence) closes the LYING DECLARATION — a witness that
#           is perfectly pure and still claims three reads while consuming one.  No static
#           analysis of a callable can see that its declared dependency is inert.
# INTERLOCK-A is a witness the static half ACCEPTS and the dynamic half refuses; INTERLOCK-B is a
# witness the dynamic half reports CLEAN and the static half refuses.  Both are in the battery, so
# "both halves are load-bearing" is an observation, not a claim.

import base64 as _p6_base64
import linecache as _p6_linecache


def _p6_materialise(name: str, expect_sha: str, b64: str):
    """Turn an embedded module into a real module object.

    The source is registered in linecache under a stable synthetic filename so that
    `inspect.getsource` resolves — L3 of the purity contract RECOMPILES a witness's source and
    compares it to the live code object, and the governed primitives (`witness_input`, `digest`)
    are themselves source-hashed at registration.  A single-file candidate that could not recover
    its own source would silently lose that limb, which is the sophisticated __code__-swap channel
    (arm SC:post_definition_code_swap_source_spoofed).
    """
    raw = _p6_base64.b64decode(b64)
    got = hashlib.sha256(raw).hexdigest()
    if got != expect_sha:
        raise RuntimeError(f"embedded P6 module {name!r} is not the banked text: sha256 {got} != "
                           f"{expect_sha}; a P6 implementation that is not the certified one is "
                           "REFUSED (the batteries certify the banked bytes, not the name)")
    src = raw.decode("utf-8")
    filename = f"<signalnest-embedded:{name}:{expect_sha[:12]}>"
    module = types.ModuleType(f"_signalnest_{name}")
    module.__file__ = filename
    module.__dict__["__name__"] = f"_signalnest_{name}"
    # mtime None marks the entry permanent: linecache.checkcache never purges it.
    _p6_linecache.cache[filename] = (len(src), None, src.splitlines(True), filename)
    sys.modules[f"_signalnest_{name}"] = module
    exec(compile(src, filename, "exec"), module.__dict__)
    return module


_P6_PURITY = _p6_materialise("p6_ast_purity", *_P6_EMBEDDED["p6_ast_purity"])
_P6_DYNAMIC = _p6_materialise("p6_computational_independence",
                              *_P6_EMBEDDED["p6_computational_independence"])

P6_CONTRACT_ID = _P6_PURITY.CONTRACT_ID
P6_INSTRUMENT_OF = dict(_P6_DYNAMIC.INSTRUMENT_OF)

# The primitives' own bootstrap must be clean: a validator whose governed primitives do not
# themselves satisfy the contract is not a validator.
if _P6_PURITY.BOOTSTRAP_PROBLEMS:
    raise RuntimeError(f"the P6 governed primitives do not satisfy {P6_CONTRACT_ID}: "
                       f"{_P6_PURITY.BOOTSTRAP_PROBLEMS}")


# ---- THE CHANNEL REGISTRY --------------------------------------------------------------------
# A witness never FETCHES its authority; the framework INJECTS it.  That inversion is what makes
# the read set observable by construction rather than by declaration, and it is the precondition
# for perturbation: you cannot vary a channel you cannot name.  The registry is NO-OVERRIDE for
# the same reason the pin registries are — a channel whose content can be rebound is a lever for
# moving the answer after review.  Re-registering IDENTICAL content is idempotent, so independent
# call sites naming the same authority do not have to coordinate.
_P6_CHANNEL_SOURCES: dict = {}
P6_CHANNEL_SOURCES = _P6_CHANNEL_SOURCES          # read-only alias for callers/tests


def _p6_channel_shape(members):
    """Channels are enumerable: a set of members, or a mapping key -> set of members. Anything
    else cannot be perturbed and therefore cannot be verified."""
    if isinstance(members, dict):
        return {str(k): frozenset(str(x) for x in v) for k, v in members.items()}
    if isinstance(members, (set, frozenset, list, tuple)):
        return frozenset(str(x) for x in members)
    raise ContractPinError(
        f"a P6 channel must be an enumerable authority (a set of members, or a mapping "
        f"key->members); got {type(members).__name__}. A channel that cannot be enumerated "
        "cannot be perturbed, and an unperturbable dependency cannot be verified")


def register_p6_channel(channel_id: str, members, *, rationale: str = "") -> None:
    """Register the CONTENT of one authority a witness may declare a read of."""
    if not isinstance(channel_id, str) or not channel_id:
        raise ContractPinError("a P6 channel requires a non-empty authority id")
    shape = _p6_channel_shape(members)
    existing = _P6_CHANNEL_SOURCES.get(channel_id)
    if existing is not None and _p6_channel_shape(existing) != shape:
        raise ContractPinError(
            f"P6 channel {channel_id!r} is already registered with different content; rebinding a "
            "declared authority after registration moves the observation without changing the "
            "spec that was reviewed; REFUSED")
    _P6_CHANNEL_SOURCES[channel_id] = (dict(members) if isinstance(members, dict)
                                       else set(members))
    if rationale:
        _P6_CHANNEL_RATIONALE[channel_id] = rationale


_P6_CHANNEL_RATIONALE: dict = {}
_P6_CHANNEL_GROUPS: dict = {}
P6_CHANNEL_GROUPS = _P6_CHANNEL_GROUPS


def register_p6_channel_group(group_id: str, channels, *, rationale: str = "") -> None:
    """Declare that one authority id stands for SEVERAL channels.

    A CodeNativeWitness declares exactly one `reads`, which without this can only ever express a
    single dependency — and a single dependency cannot express the shape the eleventh actually
    took: THREE authorities claimed, ONE consumed. Each member of a group is injected and
    perturbed independently, so a witness that declares a group and reads one member is reported
    inert on the other two by name.
    """
    if not isinstance(group_id, str) or not group_id:
        raise ContractPinError("a P6 channel group requires a non-empty authority id")
    members = tuple(channels)
    if not members:
        raise ContractPinError(f"P6 channel group {group_id!r} declares no channels; a witness "
                               "that names no dependency cannot be verified to have one")
    existing = _P6_CHANNEL_GROUPS.get(group_id)
    if existing is not None and tuple(existing) != members:
        raise ContractPinError(
            f"P6 channel group {group_id!r} is already registered as {existing!r}; rebinding a "
            "declared dependency structure after registration is REFUSED")
    _P6_CHANNEL_GROUPS[group_id] = members
    if rationale:
        _P6_CHANNEL_RATIONALE[group_id] = rationale


def _p6_declared_channels(witness: "CodeNativeWitness") -> set:
    group = _P6_CHANNEL_GROUPS.get(witness.reads)
    return set(group) if group else {witness.reads}


def _p6_resolve_sources(witness: "CodeNativeWitness", spec: dict, collection, cid: str):
    """Return (sources, missing). `sources` is what the instrument INJECTS."""
    sources: dict = {}
    missing = []
    for channel in sorted(_p6_declared_channels(witness)):
        if channel not in _P6_CHANNEL_SOURCES:
            missing.append(channel)
        else:
            sources[channel] = _P6_CHANNEL_SOURCES[channel]
    expected = spec.get("expected_source")
    if isinstance(expected, str) and expected:
        if expected not in _P6_CHANNEL_SOURCES:
            missing.append(expected)
        else:
            sources[expected] = _P6_CHANNEL_SOURCES[expected]
    if isinstance(collection, dict):
        sources[cid] = dict(collection)
    elif isinstance(collection, (set, frozenset, list, tuple)):
        sources[cid] = set(collection)
    else:
        sources[cid] = set()
    return sources, missing


def _p6_dynamic_spec(witness: "CodeNativeWitness", spec: dict) -> dict:
    """The spec handed to the instrument. Witness-bearing fields are STRIPPED: a CodeNativeWitness
    is not an operand, and the instrument's channel-substitution walk must not meet one."""
    out = {k: v for k, v in spec.items()
           if type(v) is not CodeNativeWitness
           and k not in (_WITNESS_PROVENANCE_KEY, "_witness_vetted")}
    out["reads"] = sorted(_p6_declared_channels(witness))
    out["_p6_channel"] = witness.reads
    return out


def _p6_nonce_seed(cid: str, field: str, witness: "CodeNativeWitness") -> int:
    """Deterministic per (collection, field, witness): a trial that cannot be re-run identically
    cannot be re-reviewed, and a nondeterministic nonce would make I1 unfalsifiable."""
    key = f"{cid}\x00{field}\x00{witness.label}\x00{witness.reads}"
    return int(hashlib.sha256(key.encode()).hexdigest()[:8], 16)


def _p6_runnable(witness: "CodeNativeWitness", cid: str, who: str):
    """(callable, problems). Validates purity AT THE CALL SITE and returns the ORIGINAL code
    object re-bound into the restricted namespace, so a module global, an import, open(), eval()
    and getattr() are not merely forbidden — they do not exist while the witness runs.

    Validating here rather than only at construction is load-bearing: a callable can be validated
    once and have its `__code__` replaced afterwards, and nothing re-checks it.  The call site is
    the last moment before the observation is produced.
    """
    fn = witness._producer
    if not isinstance(fn, types.FunctionType):
        return None, [{"kind": "NON_FUNCTION_CALLABLE", "channel": None,
                       "detail": f"{who} is a {type(fn).__name__}, not a plain function; instance "
                                 "state is an un-enumerable channel; REFUSED"}]
    problems = _P6_PURITY.verify_witness_purity(fn)
    if problems:
        return None, problems
    try:
        return _P6_PURITY.restrict(fn, validate=False), []
    except Exception as exc:
        return None, [{"kind": "WITNESS_NOT_REBINDABLE", "channel": None,
                       "detail": f"{who} passed validation but could not be confined "
                                 f"({type(exc).__name__}: {exc}); REFUSED"}]


def _p6_as_problem(relation, cid, field, witness, p6problem) -> dict:
    """Carry the P6 kind through VERBATIM. The gate rule is that a refusal must name the detector
    that actually fired: a test that accepts any refusal cannot tell P6 from a P3 schema reject or
    a P7 vetting reject on the way past, and would pass against a framework in which P6 does
    nothing at all."""
    kind = p6problem.get("kind", "WITNESS_INDEPENDENCE_UNVERIFIED")
    instrument = P6_INSTRUMENT_OF.get(kind, "STATIC" if "layer" in p6problem else "I0")
    channel = p6problem.get("channel")
    return _problem(relation, kind,
                    f"{cid}: code-native witness {witness.label!r} for field {field!r} failed "
                    f"{P6_CONTRACT_ID} [{instrument}"
                    + (f"/{p6problem['layer']}" if p6problem.get("layer") else "")
                    + (f" channel={channel!r}" if channel else "") + "]: "
                    + str(p6problem.get("detail", p6problem)))


def p6_findings(witness: "CodeNativeWitness", field: str, spec: dict, collection, cid: str) -> list:
    """EVERY P6 finding against `witness`, as framework Problems. [] means admitted.

    The hook itself returns only the first, because a Problem list is what the caller appends to
    and one refusal is one refusal. This is the full list, and it exists because an arm that can
    only see the first finding cannot tell WHICH instrument fired: a witness that both fails to
    touch a declared authority (I2) and echoes the collection (I4) would report only the I2 kind,
    and an I4 arm written against it would be crediting the wrong detector.
    """
    relation = spec.get("relation", "?")
    who = f"code-native witness {witness.label!r} for field {field!r}"

    runnable, static_problems = _p6_runnable(witness, cid, who)
    if runnable is None:
        return [_p6_as_problem(relation, cid, field, witness, p) for p in static_problems]

    sources, missing = _p6_resolve_sources(witness, spec, collection, cid)
    if missing:
        return [_problem(relation, "CHANNEL_UNRESOLVABLE",
                         f"{cid}: {who} declares a read of {sorted(missing)!r}, which is not a "
                         "registered P6 channel. The framework INJECTS a witness's authority — it "
                         "never lets the witness fetch one — so a channel it cannot resolve is a "
                         "dependency it cannot perturb, and an unperturbable dependency cannot be "
                         "verified. Absence of a check is never a pass; REFUSED")]
    # CHANNEL CONTENT PROVENANCE (Part G). The channel RESOLVES; that says the name is registered,
    # not that the content behind it is an authority. Checked before the instrument runs, because
    # every question the instrument asks is about the witness and would be answered truthfully by
    # a witness honestly reading a copy of the collection.
    unprovenanced = _p6_channel_provenance_problems(sources, collection, cid, relation)
    if unprovenanced:
        return unprovenanced

    problems = _P6_DYNAMIC.verify_computational_independence(
        runnable, _p6_dynamic_spec(witness, spec), collection, cid=cid, sources=sources,
        expected_source_id=(spec.get("expected_source") if
                            isinstance(spec.get("expected_source"), str) else None),
        nonce_seed=_p6_nonce_seed(cid, field, witness))
    return [_p6_as_problem(relation, cid, field, witness, p) for p in problems]


def verify_computational_independence(witness: "CodeNativeWitness", field: str, spec: dict,
                                      collection, cid: str):
    """THE P6 HOOK. Returns a Problem or None.

    A witness is admitted only when BOTH halves agree:
      static   — the callable is inside RWCC-1, so its observation is a function of its INJECTED
                 inputs and the governed primitives alone (no module global, closure cell, mutable
                 default, environ, filesystem, clock, import, eval, or dynamic attribute reach it);
      dynamic  — every channel it DECLARES was observed to be touched (I2) and load-bearing under
                 a cardinality-preserving content swap (I3), and the observation is invariant to
                 the collection-under-test and to expected_source through every channel those
                 reach it by, with a nonce canary proving no echo (I4).
    """
    findings = p6_findings(witness, field, spec, collection, cid)
    return findings[0] if findings else None


class _P6ExecutionRefused(Exception):
    """Raised when the EXECUTION path refuses a witness, carrying the Problem the detector
    produced so the caller reports that kind rather than collapsing it into WITNESS_RAISED."""

    def __init__(self, problem: dict):
        _EXC_INIT(self, problem.get("detail", ""))
        self.problem = problem


def _p6_observe(witness: "CodeNativeWitness", spec: dict, collection, cid: str, field: str):
    """Produce the REAL observation, under exactly the conditions P6 verified.

    The witness is re-validated and re-bound here rather than being called directly, so the
    namespace it produced the verified observation in and the namespace it produces the SHIPPED
    observation in are the same namespace.  Verifying under confinement and then executing
    unconfined would make the whole check theatre.

    This is DELIBERATELY not conditional on the hook: binding a permissive
    `verify_computational_independence` must not buy a caller unconfined witness execution.  A
    stand-in can make P6 say nothing; it cannot make the framework hand a witness the ambient
    namespace back.  (The unbound case never reaches here at all — `_p6_check` refuses first.)
    """
    relation = spec.get("relation", "?")
    runnable, problems = _p6_runnable(witness, cid, f"witness {witness.label!r} for {field!r}")
    if runnable is None:
        raise _P6ExecutionRefused(
            _p6_as_problem(relation, cid, field, witness, problems[0]))
    sources, missing = _p6_resolve_sources(witness, spec, collection, cid)
    if missing:
        raise _P6ExecutionRefused(_problem(
            relation, "CHANNEL_UNRESOLVABLE",
            f"{cid}: witness {witness.label!r} for field {field!r} declares a read of "
            f"{sorted(missing)!r}, which is not a registered P6 channel; the framework injects a "
            "witness's authority and cannot execute one it is unable to resolve; REFUSED"))
    # CHANNEL CONTENT PROVENANCE (Part G), on the EXECUTION path as well as the verification one.
    # A check that ran only where the verdict is computed would leave the route that actually
    # hands the witness its inputs unguarded, which is the sibling-layer defect (I28AM).
    unprovenanced = _p6_channel_provenance_problems(sources, collection, cid, relation)
    if unprovenanced:
        raise _P6ExecutionRefused(unprovenanced[0])
    injected = dict(spec)
    injected["_witness_inputs"] = {k: (dict(v) if isinstance(v, dict) else set(v))
                                   for k, v in sources.items()}
    injected["_p6_channel"] = witness.reads
    # P4: the P6 trial re-executes the witness under a restricted namespace. It is still a
    # witness execution, so the gate is closed for it too.
    return _call_witness(runnable, injected)


# ================================================================================================
# P2 GRAFT — SELF-ADEQUACY COMPUTED **AND** COMPUTATIONAL
# ================================================================================================
# P2 has two conjoined obligations and the candidate shipped only the first half of the first one.
#
#   (2a) COMPUTED.  No adequacy claim may be ACCEPTED as a summary the caller or the witness wrote
#        about itself.  The P7 witness_field gate already closed the COMPARATOR fields
#        (unknown_probe_accepted / member_effect / baseline_healthy / value_domain) with
#        SELF_ADEQUACY_UNWITNESSED.  It never reached PART D, where triangulation depth was an
#        INTEGER the subject reported about itself and a one-directional discovery had no floor.
#        Those two are closed in _verify_C_cross_source and _verify_A_consequence above.
#
#   (2b) COMPUTATIONAL INDEPENDENCE.  Absent entirely.  guard_independence compares THREE STRINGS
#        (reads == cid, provider == cid, independent_source == cid).  A provider that recomputes
#        its "independent" observation from the collection under test at call time, while declaring
#        reads='external_authority', is string-wise IDENTICAL to an honest one and certifies a
#        collection silently shortened from three members to one.  Only EXECUTION separates them.
#
# THE EXPERIMENT (P2-CI-1).  An observation offered as INDEPENDENT of the collection under test
# must be INVARIANT to it.  The framework therefore:
#   1. enumerates the channels through which the collection could possibly reach the witness —
#      for a registry-backed witness there are exactly three: the PAYLOAD it is called with, the
#      MODULE STATE its code reads, and its CLOSURE CELLS;
#   2. generates a perturbation ITSELF — a synthetic sentinel member no external authority can
#      know, plus a SHORTENING of the collection, seeded from the collection id and the witness
#      name and never read from the spec (a caller who could choose the mutation would choose one
#      their echo ignores: BYP-1's shape one layer up, P2-MUT-06);
#   3. re-executes the witness through each channel in turn and requires the observation to be
#      unchanged, and requires the sentinel never to appear in the observation (the taint canary);
#   4. REFUSES when the observation moves only because the same runtime source was reused.
#
# POLARITY IS PART OF THE CONTRACT, not an afterthought.  Invariance is not universally good:
# a kind-D mutation witness is REQUIRED to depend on the relevant mutation, and a witness that
# declares a registered authority is REQUIRED to depend on THAT.  So the graft carries both
# classes — MUST_BE_INVARIANT (to the collection under test) and MUST_DEPEND (on the relevant
# mutation, and on a declared authority the framework can resolve) — and names which one fired.
#
# WHY A STATIC LIMB IS STILL NEEDED (P6-LIM-02, in this layer).  A channel that cannot be
# enumerated cannot be perturbed, and an unperturbable dependency cannot be verified.  Module state
# IS enumerable by name (co_names ∩ __globals__) and this graft perturbs it directly — that is what
# catches the live echo whose copy of the collection lives in a module-level box.  What remains
# genuinely unperturbable — an imported module, a delegate function, a closure cell, the clock, the
# filesystem — is refused rather than waved through, because absence of a check is never a pass.
#
# WHAT P2 DOES **NOT** CLOSE, stated so it is not mistaken for closed (the contract's own scoping
# caveat).  A CONSTANT observation is invariant under every perturbation and therefore PASSES this
# experiment.  The live echo is P2's; the serialised or authored-at-review-time copy is P7's (a
# JSON-expressible witness never reaches here) and the question of whether the constant came from
# the reviewed callable is P9's.  Where the declared authority is a REGISTERED channel the
# MUST_DEPEND limb below does refuse a constant by name; where it is not, that limb has nothing to
# perturb and is recorded as skipped rather than passed.

_P2_CONTRACT_ID = "P2-CI-1"

# The polarity classes, named so a refusal says which obligation it belongs to.
P2_MUST_BE_INVARIANT = "MUST_BE_INVARIANT"
P2_MUST_DEPEND = "MUST_DEPEND"


# ------------------------------------------------------------------------------------------------
# THE PERTURBATION MUST NOT BE RECOGNISABLE AS A PERTURBATION  (gate 4N-I28BH-B0w-R2-SLICE1-FI,
# P2 adaptive-witness finding)
# ------------------------------------------------------------------------------------------------
# THE DEFECT THIS CLOSES.  The perturbation member used to be `"__P2_" + sha256(...)[:16] + "__"`.
# The witness is attacker-authored code the framework EXECUTES (§3), and the framework HANDS IT the
# perturbed input under `_witness_inputs`.  So the witness could read its own input, see a member
# carrying a literal prefix no real authority ever produces, and conclude "I am being probed" — then
# behave honestly for exactly those calls and return an authored constant on the live one.  That
# witness passed X4 MUST_DEPEND (its observation genuinely moved on every probe) while certifying an
# INCOMPLETE collection, and the same constant WITHOUT the four lines of detection was correctly
# refused P2_DECLARED_READ_INERT.  The differential was caused by the marker, not by the property.
#
# WHY RENAMING THE PREFIX IS NOT THE FIX.  The witness does not need the string.  Anything that
# makes the injected value distinguishable from an ordinary member of the authority — a synthetic
# shape, a value outside the domain, a type nothing else has — is the same defect wearing different
# clothes.  The perturbation has to be indistinguishable from a genuinely different REAL input.
#
# THE FIX, in three parts, each of which is load-bearing:
#
#   1. UNMARKED, DOMAIN-SHAPED VALUES.  `_p2_probe_member` builds the injected member out of the
#      REAL members it is standing among: same alphabet, same length, same separator structure,
#      derived by a deterministic edit of an actual member.  It carries no prefix and no marker,
#      and it is checked to be absent from every real source before it is used, so it remains a
#      canary (it cannot legitimately come back) while looking exactly like a sibling of the values
#      it sits beside.  Determinism is preserved: it is a hash-driven CHOICE over an alphabet
#      derived from the real members, not a random string, so every trial re-runs identically.
#
#   2. PERTURB BY RESTRICTION WHEREVER POSSIBLE.  The MUST_DEPEND schedule (`_p2_channel_trials`)
#      mostly does not inject anything at all: it hands the witness SUBSETS of the authority's own
#      real content.  A subset of the real authority is, by construction, indistinguishable from a
#      smaller real authority — there is no artefact to detect because nothing synthetic is present.
#
#   3. AN UNMARKED SCHEDULE WITH REPEATS.  A witness that cannot recognise the probe by CONTENT can
#      still try to recognise it by POSITION (lie on call #1, be honest afterwards) or by COUNT.
#      The schedule therefore interleaves repeats of the UNPERTURBED content among the variants, at
#      positions the witness cannot single out, and requires equal inputs to produce equal
#      observations.  A call-position discriminator answers the two identical trials differently and
#      is refused; it has no way to tell which of them is "the live one" because none of them is.
#
# WHAT REMAINS, STATED SO IT IS NOT MISTAKEN FOR CLOSED.  A witness that hard-codes a copy of the
# TRUE authority content and lies on exactly that one input cannot be caught by any experiment that
# only queries other inputs — the live point is the one point the framework must ask about.  That
# residual is what the LAWS in X4 exist for: the observation is required to be MONOTONE under
# restriction of the authority, and each member it asserts is required to be ACCOUNTABLE to the
# authority listing that member.  A witness that answers honestly everywhere except the live point
# violates one of the two, because its honest answers on subsets are not below its lie.


_P2_CHARACTER_CLASSES = (
    ("abcdefghijklmnopqrstuvwxyz", str.islower),
    ("ABCDEFGHIJKLMNOPQRSTUVWXYZ", str.isupper),
    ("0123456789", str.isdigit),
)


def _p2_alphabet(templates) -> tuple:
    """The character CLASSES the real members are drawn from, as (alphabet, length, template).

    Used to build a probe member that is a sibling of the values it sits among rather than a marked
    literal. Derived from the members themselves, so it adapts to whatever the deployment's
    identifiers look like — single letters, dotted paths, `m.py::CONSTANT` — without the framework
    holding a vocabulary of shapes it would have had to guess in advance.

    THE CLASS, NOT THE OBSERVED CHARACTERS.  A three-member domain {a, b, c} observed literally
    offers an alphabet of three characters and every candidate over it collides with a real member,
    which would drive the generator into a fallback — and a fallback with a different shape is a
    marker by another name. Widening to the CLASS (any lowercase letter, because the members are
    lowercase letters) keeps every candidate indistinguishable from a member the domain could have
    had, which is exactly the property being bought.
    """
    real = [str(t) for t in templates if str(t)]
    if not real:
        return "abcdefghijklmnopqrstuvwxyz0123456789", 8, ""
    template = sorted(real, key=lambda s: (len(s), s))[len(real) // 2]
    alphabet = ""
    for characters, is_class in _P2_CHARACTER_CLASSES:
        if any(is_class(ch) for member in real for ch in member):
            alphabet += characters
    return (alphabet or "abcdefghijklmnopqrstuvwxyz0123456789"), max(1, len(template)), template


def _p2_probe_member(templates, avoid, *parts) -> str:
    """An injected member that LOOKS like the real ones and is absent from all of them.

    Deterministic in (collection id, witness name, role) exactly as before — a trial that cannot be
    re-run identically cannot be re-reviewed — but the determinism is now expressed as a hash-driven
    choice over the REAL alphabet instead of a hash printed inside a recognisable wrapper. It is
    still derived from framework inputs ONLY: nothing a spec carries chooses it, so a caller cannot
    arrange for the perturbation to be one their witness happens to ignore.

    `avoid` is every string that legitimately occurs anywhere the witness can see (the collection,
    every registered channel's content, the spec's own values). The returned member is guaranteed
    not to be in it, which is what keeps it usable as a taint canary: a witness that returns it is
    returning something no authority told it.
    """
    key = "\x00".join("" if p is None else str(p) for p in parts)
    alphabet, width, template = _p2_alphabet(templates)
    forbidden = {str(x) for x in avoid}
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    for attempt in range(64):
        # A tiny domain can exhaust the candidates of its own width; GROW BY ONE CHARACTER rather
        # than change shape, because a differently-shaped fallback is the marker this function
        # exists to remove.
        span = max(1, min(width + attempt // 16, 32))
        raw = hashlib.sha256(digest + bytes([attempt])).digest()
        body = "".join(alphabet[b % len(alphabet)] for b in raw[:span])
        if template and not template.isalnum():
            # Preserve the STRUCTURE of a structured identifier (`pkg.mod::NAME`) by editing the
            # alphanumeric runs of a real member in place, so the result keeps every separator the
            # real members have and differs only where a real sibling would differ.
            out, taken = [], 0
            for ch in template:
                if ch.isalnum():
                    out.append(body[taken % len(body)])
                    taken += 1
                else:
                    out.append(ch)
            candidate = "".join(out)
        else:
            candidate = body[:span]
        if candidate and candidate not in forbidden:
            return candidate
    # Unreachable for any domain whose character class has more than one member: the loop already
    # grows the candidate by a character every sixteen attempts, so it out-runs any finite
    # avoid-set. The last resort still draws from the SAME alphabet — it is longer than its
    # neighbours, which is a weaker disguise, but it is not a marker, and a marker is the defect.
    return "".join(alphabet[b % len(alphabet)]
                   for b in hashlib.sha256(key.encode("utf-8")).digest())


def _p2_sentinel(*parts) -> str:
    """Back-compatible entry point: a probe member with NO domain to imitate.

    Retained so a caller that has no templates to hand still gets a deterministic member; every
    call site inside P2 passes real templates, because an unshaped value is exactly what an adaptive
    witness detects. Kept unmarked deliberately — there is no `__P2_` prefix anywhere any more.
    """
    return _p2_probe_member((), (), *parts)


def _p2_canon(obs: Any) -> str:
    """A stable rendering of an observation, so two trials are compared on CONTENT.

    Set iteration order is not stable across processes and `==` on two dicts of sets says nothing
    about a tuple-vs-list difference that a consumer would see. Canonicalising makes 'the
    observation did not move' an observation rather than an accident of ordering.
    """
    if isinstance(obs, (set, frozenset)):
        return "set{" + "|".join(sorted(repr(x) for x in obs)) + "}"
    if isinstance(obs, (list, tuple)):
        return "seq[" + "|".join(_p2_canon(x) for x in obs) + "]"
    if isinstance(obs, dict):
        return "map{" + "|".join(f"{k!r}=>{_p2_canon(v)}"
                                 for k, v in sorted(obs.items(), key=lambda kv: repr(kv[0]))) + "}"
    return f"{type(obs).__name__}:{obs!r}"


def _p2_member_strings(value: Any) -> set:
    """The MEMBERSHIP view of an operand: a set's members, a sequence's items, a mapping's KEYS.
    A mapping is keyed by the thing being certified, so its keys are what a shortening removes."""
    if isinstance(value, dict):
        return {str(k) for k in value}
    if isinstance(value, (set, frozenset, list, tuple)):
        return {str(x) for x in value}
    return set()


def _p2_perturbed(members: set, sentinel: str) -> set:
    """The framework's mutation: ADD a member the external authority cannot know, and REMOVE one it
    should have. Both directions matter — an echo that only ever grows is caught by the addition,
    and the SHORTENING is the failure this whole property exists for (a collection silently reduced
    from three members to one, with the witness agreeing)."""
    out = set(members)
    out.add(sentinel)
    if members:
        out.discard(sorted(members)[0])
    return out


def _p2_substitute(node: Any, target: frozenset, replacement: set):
    """Replace every enumerable node whose MEMBER SET equals `target` with `replacement`.

    Returns (new_node, substituted). Walking the whole payload rather than checking one well-known
    key is the point: the banked eleventh reached the collection through `route_payload`, a field no
    guard inspects, and the next one will use a field name nobody has written yet. A structural walk
    does not need to know the name.
    """
    if isinstance(node, dict):
        if node and frozenset(str(k) for k in node) == target:
            shared = next(iter(node.values()))
            return {m: shared for m in sorted(replacement)}, True
        out, hit = {}, False
        for key, value in node.items():
            new, done = _p2_substitute(value, target, replacement)
            out[key] = new
            hit = hit or done
        return out, hit
    if isinstance(node, (set, frozenset)):
        if node and frozenset(str(x) for x in node) == target:
            return set(replacement), True
        return node, False
    if isinstance(node, (list, tuple)):
        if node and frozenset(str(x) for x in node) == target:
            new = sorted(replacement)
            return (new if isinstance(node, list) else tuple(new)), True
        out, hit = [], False
        for item in node:
            new, done = _p2_substitute(item, target, replacement)
            out.append(new)
            hit = hit or done
        return (out if isinstance(node, list) else tuple(out)), hit
    return node, False


def _p2_perturb_node(node: Any, sentinel: str):
    """Perturb EVERY enumerable leaf of a value, not only the ones holding the collection.

    Used on an ambient channel that does not (visibly) carry the collection: if the observation
    still moves, the witness depends on module state it never declared, which is a dependency no
    review saw and no perturbation of the DECLARED authority would have exposed.
    """
    if isinstance(node, dict):
        if node and all(not isinstance(v, (dict, set, frozenset, list, tuple))
                        for v in node.values()):
            shared = next(iter(node.values()))
            keys = _p2_perturbed({str(k) for k in node}, sentinel)
            return {k: shared for k in sorted(keys)}, True
        out, hit = {}, False
        for key, value in node.items():
            new, done = _p2_perturb_node(value, sentinel)
            out[key] = new
            hit = hit or done
        return out, hit
    if isinstance(node, (set, frozenset)):
        return _p2_perturbed({str(x) for x in node}, sentinel), True
    if isinstance(node, (list, tuple)):
        if node and all(not isinstance(x, (dict, set, frozenset, list, tuple)) for x in node):
            new = sorted(_p2_perturbed({str(x) for x in node}, sentinel))
            return (new if isinstance(node, list) else tuple(new)), True
        out, hit = [], False
        for item in node:
            new, done = _p2_perturb_node(item, sentinel)
            out.append(new)
            hit = hit or done
        return (out if isinstance(node, list) else tuple(out)), hit
    return node, False


# ------------------------------------------------------------------------------------------------
# THE MUST_DEPEND TRIAL SCHEDULE — REAL-LOOKING INPUTS, UNMARKED ORDER, CHECKABLE LAWS
# ------------------------------------------------------------------------------------------------
# The channel content the framework injects is either a MAPPING (keyed by the thing being certified)
# or a SET of members. Both can be RESTRICTED to a subset of their own real members, and the result
# is a perfectly ordinary value of the same type carrying the same kind of members — a smaller real
# authority. That is the perturbation this schedule mostly uses, and it is why there is nothing for
# an adaptive witness to detect.


def _p2_restrict(content: Any, keep: set):
    """`content` reduced to the members named in `keep`. Type and value-shape are preserved."""
    if isinstance(content, dict):
        return {k: v for k, v in content.items() if str(k) in keep}
    if isinstance(content, (set, frozenset)):
        return {x for x in content if str(x) in keep}
    if isinstance(content, (list, tuple)):
        out = [x for x in content if str(x) in keep]
        return out if isinstance(content, list) else tuple(out)
    return content


def _p2_extend(content: Any, member: str):
    """`content` with one more member, built the way the existing members are built."""
    if isinstance(content, dict):
        out = dict(content)
        out[member] = next(iter(content.values())) if content else None
        return out
    if isinstance(content, (set, frozenset)):
        return set(content) | {member}
    if isinstance(content, (list, tuple)):
        out = list(content) + [member]
        return out if isinstance(content, list) else tuple(out)
    return content


def _p2_real_value_pool(spec: dict, collection: Any, channels: dict) -> set:
    """Every string that legitimately occurs anywhere the witness can already see.

    The probe member is chosen OUTSIDE this set, which is what lets a returned probe member be read
    as a taint canary rather than as an honest reading, and lets `_p2_restrict` be trusted to
    produce a value the witness has no way to call synthetic.
    """
    pool = {str(m) for m in _p2_member_strings(collection)}
    for content in (channels or {}).values():
        pool |= {str(m) for m in _p2_member_strings(content)}
        if isinstance(content, dict):
            pool |= {str(v) for v in content.values() if isinstance(v, (str, int))}
    for key, value in (spec or {}).items():
        pool.add(str(key))
        if isinstance(value, (str, int)):
            pool.add(str(value))
    return pool


_P2_TRIAL_ACCOUNTABLE_CAP = 6      # per channel: members the observation asserts
_P2_TRIAL_OTHER_CAP = 3            # per channel: members it does not


def _p2_channel_trials(content: Any, observed_members: set, probe_member: str) -> list:
    """The ordered trial schedule for one declared authority channel.

    Returns [(label, dropped_member_or_None, is_restriction, trial_content), ...].

    ORDER IS PART OF THE EXPERIMENT.  Repeats of the UNPERTURBED content are interleaved among the
    variants rather than run once at the start, so a witness that answers by CALL POSITION cannot
    keep a story straight: it must give the same answer at several different positions for the same
    input, and it has no way to tell which position is the live evaluation because the live
    evaluation is not in this sequence at a position it can identify.

    RESTRICTIONS ARE THE PRIMARY VARIANT.  A subset of the authority's own content is a real
    authority; a witness cannot object to it without objecting to the possibility that the authority
    is smaller than it remembers. The one non-restriction trial injects `probe_member`, which is
    shaped like the real members and absent from every real source.
    """
    members = sorted(_p2_member_strings(content))
    if not members:
        return []
    asserted = [m for m in members if m in observed_members][:_P2_TRIAL_ACCOUNTABLE_CAP]
    rest = [m for m in members if m not in observed_members][:_P2_TRIAL_OTHER_CAP]
    whole = set(members)

    variants: list = []
    # The keep-sets are built by COMPREHENSION, never by set difference: INV-6 reads a `-` or `&`
    # outside a gate-reachable relation checker as a parallel evaluator, and it is right to — this
    # function must be able to build a trial input and nothing else.
    for member in asserted + rest:
        variants.append((f"WITHOUT:{member}", member, True,
                         _p2_restrict(content, {m for m in whole if m != member})))
    if len(members) > 1:
        variants.append(("WITHOUT-EVERYTHING", None, True, _p2_restrict(content, set())))
    swapped = _p2_extend(_p2_restrict(content, {m for m in whole if m != members[0]}),
                         probe_member)
    variants.append((f"INSTEAD-OF:{members[0]}", None, False, swapped))

    schedule: list = [("UNCHANGED", None, True, content)]
    for index, variant in enumerate(variants):
        schedule.append(variant)
        if index % 2 == 1:
            schedule.append(("UNCHANGED", None, True, content))
    schedule.append(("UNCHANGED", None, True, content))
    schedule.append(variants[-1])          # the injected variant, repeated at a distant position
    return schedule


def _p2_ambient_globals(fn) -> dict:
    """The MODULE STATE a witness's code actually reads, by name.

    Derived from the code object (co_names, recursively through nested code objects) intersected
    with the function's own __globals__, so it is what the EXECUTED code reaches — never a
    declaration about what it reaches. This is the enumeration that makes module state perturbable
    instead of merely forbidden.
    """
    names: set = set()
    stack = [getattr(fn, "__code__", None)]
    while stack:
        code = stack.pop()
        if code is None:
            continue
        names |= set(code.co_names)
        for const in code.co_consts:
            if isinstance(const, types.CodeType):
                stack.append(const)
    namespace = getattr(fn, "__globals__", None) or {}
    return {name: namespace[name] for name in sorted(names) if name in namespace}


def _p2_declared_channels(spec: dict) -> tuple:
    """(channels, resolvable). The declared authority, resolved against the P6 channel registry.

    `resolvable` is False when the spec declares an authority the framework has no content for —
    the MUST_DEPEND limb then has nothing to perturb and is recorded as SKIPPED. It is not treated
    as satisfied: an unrunnable check is a residual, and this function is where a reader finds it.
    """
    declared = [spec.get("reads"), spec.get("independent_source")]
    wanted: list = []
    for entry in declared:
        if not isinstance(entry, str) or not entry:
            continue
        group = _P6_CHANNEL_GROUPS.get(entry)
        wanted.extend(group if group else [entry])
    if not wanted:
        return {}, False
    if any(channel not in _P6_CHANNEL_SOURCES for channel in wanted):
        return {}, False
    return {channel: _P6_CHANNEL_SOURCES[channel] for channel in sorted(set(wanted))}, True


def _p2_witness_payload(spec: dict, cid: str, sources: Optional[dict] = None) -> dict:
    """The payload a registry-backed witness is called with.

    With no resolvable declared authority this is the spec ITSELF (identical object, so nothing
    about the existing execution path changes). With one, the framework INJECTS the authority's
    content under `_witness_inputs` — the witness never fetches, which is what makes the dependency
    both observable and perturbable, and is why MUST_DEPEND can be run at all.
    """
    channels, resolvable = _p2_declared_channels(spec) if sources is None else (sources, True)
    if not resolvable or not channels:
        return spec
    payload = dict(spec)
    payload["_witness_inputs"] = {name: (dict(value) if isinstance(value, dict) else set(value))
                                  for name, value in channels.items()}
    return payload


def _p2_identified_grounds(raw: Any, spec: dict, subject: set, source: set, cid: str):
    """(count, problem). The kind-C grounds rule: COUNT a set, never believe a number."""
    if isinstance(raw, dict):
        identified = [str(k) for k in raw]
    elif isinstance(raw, (set, frozenset, list, tuple)):
        identified = [str(x) for x in raw]
    else:
        return 0, (f"{cid}: REFUSED — P2_GROUNDS_SELF_REPORTED: the witness reported its "
                   f"triangulation depth as {type(raw).__name__} {raw!r}. A ground COUNT is an "
                   "adequacy summary the subject supplies about ITSELF — there is no experiment "
                   "that can check a number, and 9999 costs exactly what 2 costs. The witness must "
                   "IDENTIFY its grounds (a set of ground identifiers); the framework counts the "
                   "distinct ones. Triangulation depth is the whole point of this guarantee kind, "
                   "so it is the one value that may not be taken on trust")
    distinct = set(identified)
    if not distinct:
        return 0, (f"{cid}: REFUSED — P2_GROUNDS_EMPTY: the witness identified NO ground; a "
                   "cross-source requirement with nothing corroborating it is not triangulated")
    if len(distinct) != len(identified):
        duplicates = sorted({g for g in identified if identified.count(g) > 1})
        return 0, (f"{cid}: REFUSED — P2_GROUNDS_NOT_DISTINCT: ground(s) {duplicates} are repeated. "
                   "Repeating an authority does not add one, and a count padded with duplicates is "
                   "the integer defect wearing a set's clothes")
    aliases = sorted(distinct & ({str(m) for m in subject} | {str(m) for m in source}
                                 | {str(cid), str(spec.get("expected_source"))}))
    if aliases:
        return 0, (f"{cid}: REFUSED — P2_GROUND_IS_AN_ALIAS: ground(s) {aliases} name the subject, "
                   "the collection under test or the declared expected source. An authority "
                   "corroborating ITSELF is one authority, not two; aliasing is how a ground count "
                   "is inflated without a second authority ever being consulted")
    return len(distinct), None


# ------------------------------------------------------------------------------------------------
# THE MECHANISM-ACTIVATION LEDGER — P2 RUNS OR FAILS CLOSED (§3-4)
# ------------------------------------------------------------------------------------------------
# THE DEFECT THIS CLOSES.  P2's MUST_DEPEND limb was guarded by `if resolvable and channels:` and
# `_p2_declared_channels` returns ({}, False) for any authority the framework has no content for.
# A spec declaring `reads: "external.authority"` — a name that is not a registered P6 channel —
# therefore ran X0..X3, skipped X4 ENTIRELY, emitted no problem, no annotation and no record, and
# certified CLEAN.  The header called that limb "recorded as SKIPPED ... not treated as satisfied",
# but nothing anywhere recorded it and nothing anywhere read the record: the sentence described an
# intention, not a mechanism.  A skip that reaches a clean verdict IS a pass, whatever the comment
# says, and it is the exact shape a constant witness needs — declare an authority the framework
# cannot resolve, and the one limb that refuses a constant never runs.
#
# THE RULE.  Every security-relevant P2 requirement is in exactly one of FOUR states, and there is
# no fifth:
#
#   PROVED          the experiment RAN and the required condition held.
#   FAILED          the experiment RAN and the required condition did not hold  -> refusal.
#   CANNOT_PROVE    the experiment could not be run, or ran without deciding    -> FAIL CLOSED.
#   NOT_APPLICABLE  this design genuinely carries no such obligation.
#
# NOT_APPLICABLE IS THE DANGEROUS ONE, so it is the one that is constrained.  It may be claimed
# ONLY against a basis in the closed vocabulary `_P2_NA_BASES` below, each of which is a statement
# about the CONTRACT or a fact the framework itself COMPUTED — never "the instrumentation was
# absent".  An unresolved channel is missing instrumentation, so it is CANNOT_PROVE and fails
# closed; that is the whole finding.  A basis that is not in the vocabulary fails closed too, so a
# future limb cannot invent a reason to skip: it must add a reviewed basis, which is a diff.
#
# WHY A LEDGER AND NOT JUST A REFUSAL.  A refusal proves the mechanism fired ONCE.  It cannot prove
# the mechanism was REQUIRED and RAN on the run that came back clean — which is the only run an
# attacker cares about.  The ledger records, per requirement per run, {required, ran,
# channels_resolved, perturbation, recomputed, condition_checked, detector, state}, so
# "required == executed + fail_closed_cannot_prove" and "silent_skips == 0" are OBSERVATIONS about
# a clean verdict rather than claims about the code.

P2_PROVED = "PROVED"
P2_FAILED = "FAILED"
P2_CANNOT_PROVE = "CANNOT_PROVE"
P2_NOT_APPLICABLE = "NOT_APPLICABLE"
_P2_STATES = (P2_PROVED, P2_FAILED, P2_CANNOT_PROVE, P2_NOT_APPLICABLE)

# The security-relevant P2 requirements, enumerated ONCE. `finish()` walks THIS tuple, so a
# requirement the experiment forgets to record is reported as a silent skip instead of vanishing.
_P2_REQUIREMENTS = (
    "R1_DETERMINISM",                       # X0: the experiment can be run at all
    "R2_PAYLOAD_INVARIANCE",                # X1: no echo through the witness's own payload
    "R3_AMBIENT_INVARIANCE",                # X2: no echo/undeclared dependency through module state
    "R4_NO_UNPERTURBABLE_AMBIENT",          # X3: nothing reached that cannot be perturbed at all
    "R5_MUST_DEPEND_DECLARED_AUTHORITY",    # X4: the declared authority is actually consumed
    "R6_CHANNEL_CONTENT_PROVENANCE",        # X5: the authority injected IS an authority (Part G)
)

# THE CLOSED VOCABULARY OF NOT_APPLICABLE BASES. Each entry states the CONTRACT semantics or the
# COMPUTED fact that makes the obligation absent. None of them is "no instrumentation".
_P2_NA_BASES = {
    "CONTRACT_INLINE_OBSERVED_IS_P7": (
        "the spec carries no provider (or carries an inline `observed`), so there is no callable "
        "to re-execute. The authored-copy class is P7's and the partition is explicit in the "
        "contract header; P2 has no witness to experiment on rather than an unrun experiment"),
    "CONTRACT_P1_INDEPENDENCE_ACTIVATION": (
        "the spec names NO independent source at all, so there is no dependency claim to test. "
        "Admissible ONLY when an independent mechanism — P1's independence-activation obligation — "
        "REFUSES that spec on the SAME evaluation. The ledger establishes this by running the "
        "enforcement path (`_p1_activation_problems`, which is what verify_provider actually "
        "calls) and looking for INDEPENDENCE_UNDECLARED, never by calling `guard_independence` "
        "directly: the guard carries the rule at its own boundary while the entry point does not "
        "invoke that half, and believing the guard instead of the path is the sibling-layer defect "
        "(I28AM). On the shipped configuration this basis is therefore NOT available and the state "
        "is CANNOT_PROVE; it becomes available the day P1's activation is wired into the path"),
    "COMPUTED_SUBJECT_HAS_NO_MEMBERS": (
        "the collection under test has no enumerable members, so there is no membership for an "
        "observation to echo. This is a fact the framework COMPUTED from the operand, not an "
        "absent instrument"),
    "COMPUTED_COLLECTION_ABSENT_FROM_PAYLOAD": (
        "the structural walk over the witness's whole payload found no node whose member set is "
        "the collection under test, so there is no payload channel to echo through. Computed by "
        "EXECUTING the walk, not by inspecting a field name"),
    "COMPUTED_NO_AUTHORITY_INJECTED": (
        "the framework injected no channel content on this run, so there is no authority whose "
        "origin could be examined. Admissible only alongside R5, which is CANNOT_PROVE in exactly "
        "that state and refuses it: this basis can never be the reason a run comes back clean"),
    "COVERED_BY_R4_UNPERTURBABLE_AMBIENT": (
        "the witness reaches no module state the framework can enumerate AND perturb, so this "
        "limb has no channel. Admissible only because R4 ran on the same witness and refuses "
        "every ambient channel that is NOT perturbable; the ledger checks R4 ran"),
}

_P2_LEDGER_LIMIT = 512
_P2_ACTIVATION_LEDGER: list = []

# The refusal kinds that say "P2 could not DECIDE", as distinct from "P2 decided against you".
# Every one of them still REFUSES; what they do not do is preempt the rest of the evaluation.
# See the preempt-vs-append note in _verify_provider_body for why that distinction is load-bearing.
_P2_UNDECIDED_KINDS = frozenset({
    "P2_DEPENDENCY_UNDECLARED",        # no authority named at all
    "P2_DEPENDENCY_UNPROVABLE",        # named, but nothing registered / nothing perturbable
    "P2_CANNOT_PROVE_FAIL_CLOSED",     # a requirement the ledger could not decide
    "P2_MECHANISM_NOT_RUN",            # a requirement nobody recorded — a silent skip
    "P2_NOT_APPLICABLE_UNJUSTIFIED",   # an exemption outside the reviewed vocabulary
    "P2_ABORT_WITHOUT_REFUSAL",        # the run stopped early yet produced nothing
})


class _P2ActivationLedger:
    """One run of the P2 experiment against one spec, recorded requirement by requirement."""

    def __init__(self, cid: str, who: str, relation: str):
        self.cid, self.who, self.relation = cid, who, relation
        self.rows: dict = {}
        self.aborted: Optional[str] = None

    def record(self, requirement: str, state: str, *, detector: str, ran: bool,
               channels_resolved=(), perturbation: Optional[str] = None, recomputed: bool = False,
               condition_checked: Optional[str] = None, basis: Optional[str] = None,
               note: str = "") -> None:
        self.rows[requirement] = {
            "requirement": requirement, "required": True, "state": state, "ran": bool(ran),
            "channels_resolved": [str(c) for c in channels_resolved],
            "perturbation": perturbation, "recomputed": bool(recomputed),
            "condition_checked": condition_checked, "detector": detector,
            "basis": basis, "note": note}

    def out_of_scope(self, basis: str) -> None:
        """This spec carries no P2 obligation at all. Every requirement is recorded, with the
        contract basis, so an out-of-scope run is still a row in the ledger and not an absence."""
        for requirement in _P2_REQUIREMENTS:
            self.record(requirement, P2_NOT_APPLICABLE, detector="-", ran=False, basis=basis,
                        condition_checked="spec carries a resolvable provider and no inline "
                                          "`observed`")

    def abort(self, reason: str) -> None:
        """The run stopped early because it already REFUSED. Requirements downstream of the
        refusal are CANNOT_PROVE (they could not be run), which is honest — and harmless, because
        the verdict is already a refusal. `finish()` checks that claim: an abort that produced no
        refusal is itself a fail-open and is reported."""
        self.aborted = reason
        for requirement in _P2_REQUIREMENTS:
            if requirement not in self.rows:
                self.record(requirement, P2_CANNOT_PROVE, detector="-", ran=False,
                            note=f"not reached: the run was already refused ({reason})")

    def finish(self, problems: list, relation: str) -> list:
        """Close the ledger and return the fail-closed problems it OWES.

        Three things can be owed, and each of them is a fail-open the old code had no way to see:
        a requirement nobody recorded (a silent skip), a CANNOT_PROVE on a run that would otherwise
        be clean, and a NOT_APPLICABLE whose basis is not in the reviewed vocabulary.
        """
        owed: list = []
        already_refused = bool(problems)
        for requirement in _P2_REQUIREMENTS:
            row = self.rows.get(requirement)
            if row is None:
                self.record(requirement, P2_CANNOT_PROVE, detector="-", ran=False,
                            note="NOT RECORDED by the experiment — a silent skip")
                owed.append(_problem(
                    relation, "P2_MECHANISM_NOT_RUN",
                    f"{self.cid}: {self.who} — P2 requirement {requirement} was neither run nor "
                    "recorded on this evaluation. A security mechanism that can be absent without "
                    "anybody noticing is not a mechanism; a verdict produced while it was absent "
                    "is not evidence. REFUSED (fail closed)"))
                continue
            if row["state"] == P2_NOT_APPLICABLE:
                # A basis that DELEGATES to a sibling limb is only honest if the sibling actually
                # ran. Checked here rather than trusted, because the delegation is exactly what a
                # future edit would silently break.
                if row.get("basis") == "COVERED_BY_R4_UNPERTURBABLE_AMBIENT":
                    covering = self.rows.get("R4_NO_UNPERTURBABLE_AMBIENT")
                    if covering is None or not covering.get("ran"):
                        owed.append(_problem(
                            relation, "P2_CANNOT_PROVE_FAIL_CLOSED",
                            f"{self.cid}: {self.who} — P2 requirement {requirement} was excused "
                            "as covered by R4, but R4 did not run on this evaluation, so nothing "
                            "adjudicated the ambient channels at all; REFUSED"))
                if row.get("basis") not in _P2_NA_BASES:
                    owed.append(_problem(
                        relation, "P2_NOT_APPLICABLE_UNJUSTIFIED",
                        f"{self.cid}: {self.who} — P2 requirement {requirement} was recorded "
                        f"NOT_APPLICABLE against basis {row.get('basis')!r}, which is not in the "
                        "reviewed vocabulary. NOT_APPLICABLE states a fact about the CONTRACT; "
                        "anything else is an unrun check wearing an exemption's clothes. REFUSED"))
                continue
            if row["state"] == P2_CANNOT_PROVE and not already_refused:
                owed.append(_problem(
                    relation, "P2_CANNOT_PROVE_FAIL_CLOSED",
                    f"{self.cid}: {self.who} — P2 requirement {requirement} could not be decided: "
                    f"{row.get('note') or row.get('condition_checked')}. An undecidable security "
                    "requirement is REFUSED, never skipped: a check that did not run cannot have "
                    "passed, and the clean verdict it would otherwise produce is exactly what an "
                    "attacker arranges for"))
        if self.aborted and not already_refused and not owed:
            owed.append(_problem(
                relation, "P2_ABORT_WITHOUT_REFUSAL",
                f"{self.cid}: {self.who} — the P2 experiment stopped early ({self.aborted}) but "
                "produced no refusal, so requirements were left unrun on a run that would have "
                "been CLEAN; REFUSED"))
        self.rows_summary = _p2_ledger_summary(self.rows)
        _P2_ACTIVATION_LEDGER.append({
            "cid": self.cid, "who": self.who, "relation": self.relation,
            "aborted": self.aborted, "refused": bool(problems) or bool(owed),
            "rows": [self.rows[r] for r in _P2_REQUIREMENTS], "summary": self.rows_summary})
        while len(_P2_ACTIVATION_LEDGER) > _P2_LEDGER_LIMIT:
            _P2_ACTIVATION_LEDGER.pop(0)
        return owed


def _p2_ledger_summary(rows: dict) -> dict:
    """The four counts the totality claim is stated in, computed from the rows themselves."""
    counts = {"required": 0, "executed": 0, "fail_closed_cannot_prove": 0, "not_applicable": 0,
              "silent_skips": 0}
    for requirement in _P2_REQUIREMENTS:
        row = rows.get(requirement)
        counts["required"] += 1
        if row is None:
            counts["silent_skips"] += 1
            continue
        if row["state"] in (P2_PROVED, P2_FAILED):
            counts["executed"] += 1
        elif row["state"] == P2_CANNOT_PROVE:
            counts["fail_closed_cannot_prove"] += 1
        else:
            counts["not_applicable"] += 1
    return counts


def _p2_activation_ledger() -> list:
    """The recorded runs, newest last. Read by the totality battery; never by an evaluation."""
    return [dict(run) for run in _P2_ACTIVATION_LEDGER]


def _p2_reset_activation_ledger() -> None:
    _P2_ACTIVATION_LEDGER.clear()


def _p2_totality_report() -> dict:
    """The §4 claim as arithmetic over every recorded run: required == executed +
    fail_closed_cannot_prove + not_applicable, with silent_skips == 0."""
    total = {"runs": 0, "required": 0, "executed": 0, "fail_closed_cannot_prove": 0,
             "not_applicable": 0, "silent_skips": 0}
    for run in _P2_ACTIVATION_LEDGER:
        total["runs"] += 1
        for key, value in run["summary"].items():
            total[key] += value
    total["balanced"] = (total["required"] == total["executed"]
                         + total["fail_closed_cannot_prove"] + total["not_applicable"])
    return total


# ------------------------------------------------------------------------------------------------
# THE EXPERIMENT
# ------------------------------------------------------------------------------------------------
def _p2_static_ambient(fn, who: str) -> list:
    """(kind, channel, detail) triples for state this witness reaches that CANNOT be perturbed.

    RWCC-1 is the authority for what counts as an ambient channel; this is that judgement applied to
    the registry-backed path, minus the channels the dynamic limb has already enumerated and
    perturbed by name. What is left is genuinely unperturbable — an imported module, a delegate
    function, a closure cell, the clock, the filesystem — and an echo through it cannot be excluded.
    """
    out: list = []
    if getattr(fn, "__closure__", None):
        out.append(("P2_ECHO_UNEXCLUDED_CLOSURE_CELL", None,
                    f"{who} carries closure cells, which are neither enumerable by name nor "
                    "rebindable by the framework. A closure holding a copy of the collection is "
                    "invisible to every perturbation, so an echo through it cannot be excluded"))
    perturbable = {name for name, value in _p2_ambient_globals(fn).items()
                   if isinstance(value, (dict, set, frozenset, list, tuple))}
    for problem in _P6_PURITY.verify_witness_purity(fn):
        channel = problem.get("channel")
        if channel in perturbable:
            continue                     # enumerated and perturbed by the dynamic limb below
        out.append(("P2_ECHO_UNEXCLUDED_AMBIENT_CHANNEL", channel,
                    f"{who} reaches ambient state [{problem.get('kind')}"
                    + (f" channel={channel!r}" if channel else "") + "] that the framework can "
                    "neither enumerate nor perturb. An unperturbable dependency cannot be shown "
                    "independent of the collection under test, so a live echo through it cannot be "
                    "excluded — and absence of a check is never a pass. Inject the authority as a "
                    "registered P6 channel instead of reaching for it"))
    seen, unique = set(), []
    for kind, channel, detail in out:
        if (kind, channel) in seen:
            continue
        seen.add((kind, channel))
        unique.append((kind, channel, detail))
    return unique


def p2_verify_witness_independence(spec: dict, collection: Any, cid: str,
                                   binding: Optional[dict], shipped: Any) -> list:
    """THE P2(2b) ENTRY POINT for the provider-backed comparator path. Returns Problems; [] admits.

    Scope. An INLINE `observed` is not reached here — there is no callable to re-execute, and the
    contract is explicit that the authored copy belongs to P7 while the LIVE echo belongs to P2.
    The two properties partition the copied-oracle class; neither subsumes the other.
    """
    relation = spec.get("relation", "?")
    name = spec.get("provider")
    ledger = _P2ActivationLedger(cid, f"witness provider {name!r}", relation)
    if not name or "observed" in spec:
        ledger.out_of_scope("CONTRACT_INLINE_OBSERVED_IS_P7")
        return ledger.finish([], relation)
    try:
        fn, _identity = PROVIDERS.resolve(name)
    except WitnessIdentityError as exc:
        refusal = [_problem(relation, "PROVIDER_IDENTITY_REFUSED", f"{cid}: {exc}")]
        ledger.abort("the witness could not be resolved: P9 refused its identity")
        return refusal + ledger.finish(refusal, relation)
    who = f"witness provider {name!r}"
    problems: list = []
    channels, resolvable = _p2_declared_channels(spec)
    payload = _p2_witness_payload(spec, cid)

    def run(argument):
        return p9_execute_witness(PROVIDERS, name, spec, cid, binding, argument)

    def refuse(kind, detail, polarity=P2_MUST_BE_INVARIANT):
        problems.append(_problem(relation, kind,
                                 f"{cid}: {who} failed {_P2_CONTRACT_ID} [{polarity}]: {detail}"))

    # --- X0 DETERMINISM ------------------------------------------------------------------------
    # Every finding below is "the observation moved" or "the observation did not move". Against a
    # witness that answers differently to the same question, neither sentence means anything, so the
    # experiment establishes it can be run before it reports anything.
    try:
        base_a = run(payload)
        base_b = run(payload)
    except WitnessIdentityError as exc:
        refusal = [_problem(relation, "PROVIDER_IDENTITY_REFUSED", f"{cid}: {exc}")]
        ledger.abort("P9 refused the witness identity during re-execution")
        return refusal + ledger.finish(refusal, relation)
    except Exception as exc:
        refusal = [_problem(relation, "WITNESS_RAISED",
                            f"{cid}: {who} raised {type(exc).__name__}: {exc} while the P2 "
                            "independence experiment was re-executing it; not clean")]
        ledger.abort(f"the witness raised {type(exc).__name__} on its baseline execution")
        return refusal + ledger.finish(refusal, relation)
    baseline = _p2_canon(base_a)
    if baseline != _p2_canon(base_b) or baseline != _p2_canon(shipped):
        refuse("P2_NONDETERMINISTIC_WITNESS",
               "two executions under identical conditions produced different observations, so no "
               "invariance or dependency claim about it is decidable; a witness whose answer is not "
               "a function of its inputs cannot be verified and is REFUSED")
        ledger.record("R1_DETERMINISM", P2_FAILED, detector="P2_NONDETERMINISTIC_WITNESS",
                      ran=True, perturbation="none (two executions of the identical payload)",
                      recomputed=True,
                      condition_checked="canon(run#1) == canon(run#2) == canon(shipped operand)")
        ledger.abort("the witness is not deterministic, so no later limb decides anything")
        return problems + ledger.finish(problems, relation)
    ledger.record("R1_DETERMINISM", P2_PROVED, detector="P2_NONDETERMINISTIC_WITNESS", ran=True,
                  perturbation="none (two executions of the identical payload)", recomputed=True,
                  condition_checked="canon(run#1) == canon(run#2) == canon(shipped operand)")

    members = _p2_member_strings(collection)
    # SLICE1-FI: an UNMARKED stand-in. The invariance limbs hand the witness a perturbed COLLECTION,
    # so a member spelled `__P2_<hex>__` told an echo exactly when to stop echoing — the stripping
    # witness in the banked battery does precisely that. The stand-in is now shaped like the
    # collection's own members and verified absent from every real source, so it is still a canary
    # (nothing legitimate can return it) but nothing distinguishes it from a sibling member.
    sentinel = _p2_probe_member(members, _p2_real_value_pool(spec, collection, channels),
                                cid, name, "collection")
    perturbed = _p2_perturbed(members, sentinel)
    target = frozenset(members)

    def trial(argument, restore=None):
        """Run one perturbation trial. Returns (canonical observation, leaked)."""
        try:
            observation = run(argument)
        finally:
            if restore is not None:
                restore()
        return _p2_canon(observation), sentinel in _p2_member_strings(observation)

    # --- X1 PAYLOAD CHANNEL --------------------------------------------------------------------
    # The banked eleventh laundered its copy of the collection through a spec payload, choosing the
    # delegate at call time from a routing table no guard inspects. The walk does not look for
    # `route_payload`; it looks for the COLLECTION, wherever in the payload it is.
    if not target:
        ledger.record("R2_PAYLOAD_INVARIANCE", P2_NOT_APPLICABLE,
                      basis="COMPUTED_SUBJECT_HAS_NO_MEMBERS", detector="P2_LIVE_ECHO_SPEC_CHANNEL",
                      ran=False, condition_checked="_p2_member_strings(collection) is non-empty")
    else:
        # THE FRAMEWORK'S OWN INJECTION IS NOT A PAYLOAD CHANNEL (SLICE1-TOTALITY).
        #
        # `_witness_inputs` is not something the caller wrote: it is the content the FRAMEWORK
        # injected from the registered P6 channel the spec declares. Walking it here conflates two
        # different experiments. X1 asks "does the observation track the COLLECTION as the caller
        # presented it"; X4 asks "does the observation track the DECLARED AUTHORITY". An honest
        # authority AGREES with a correct collection — that is what makes it a useful authority —
        # so an authority whose members happen to equal the collection was being perturbed as if it
        # were a laundered copy, and a witness that honestly consumed it returned the sentinel and
        # was refused as a live echo. That is the honest case being punished for being honest, and
        # it is why the positive control could only pass while it was a constant.
        #
        # INTERLOCK, stated so the exemption is not mistaken for a free pass: this is safe ONLY
        # because the channel's CONTENT is itself governed (channel-content provenance, finding 2
        # of this gate). Without that, an attacker could register a copy of the collection as an
        # "authority" and launder the echo through the one subtree X1 no longer walks. The two
        # closures are load-bearing for each other and neither may be landed alone.
        injected = payload.get("_witness_inputs") if (resolvable and channels) else None
        walk_payload = payload
        if injected is not None:
            walk_payload = {key: value for key, value in payload.items()
                            if key != "_witness_inputs"}
        mutated_payload, substituted = _p2_substitute(walk_payload, target, perturbed)
        if substituted and injected is not None:
            mutated_payload["_witness_inputs"] = injected
        if not substituted:
            ledger.record("R2_PAYLOAD_INVARIANCE", P2_NOT_APPLICABLE,
                          basis="COMPUTED_COLLECTION_ABSENT_FROM_PAYLOAD",
                          detector="P2_LIVE_ECHO_SPEC_CHANNEL", ran=True,
                          perturbation=f"structural walk for the member set {sorted(target)!r}",
                          condition_checked="the walk found a node whose member set is the "
                                            "collection under test")
        else:
            try:
                observed, leaked = trial(mutated_payload)
            except Exception as exc:
                refuse("P2_WITNESS_UNRUNNABLE_UNDER_PERTURBATION",
                       f"the witness raised {type(exc).__name__}: {exc} when the collection was "
                       "perturbed in its payload; a witness that only survives the unperturbed "
                       "input cannot be shown independent of it")
                ledger.record("R2_PAYLOAD_INVARIANCE", P2_FAILED,
                              detector="P2_WITNESS_UNRUNNABLE_UNDER_PERTURBATION", ran=True,
                              perturbation=f"collection -> {sorted(perturbed)!r} in the payload",
                              recomputed=True, condition_checked="the witness is runnable and its "
                                                                 "observation is unchanged")
                ledger.abort("the witness is unrunnable under the payload perturbation")
                return problems + ledger.finish(problems, relation)
            if leaked:
                refuse("P2_LIVE_ECHO_CANARY_LEAKED",
                       f"the framework's synthetic member {sentinel!r} — which no external "
                       "authority can know — came back INSIDE the observation, so the observation "
                       "is a re-expression of the collection under test")
            elif observed != baseline:
                refuse("P2_LIVE_ECHO_SPEC_CHANNEL",
                       "the observation MOVED when the collection was perturbed inside the "
                       "witness's own payload. An observation offered as independent of the "
                       "collection cannot track it; this one agrees with whatever collection it is "
                       "shown, including a silently shortened one")
            ledger.record("R2_PAYLOAD_INVARIANCE",
                          P2_FAILED if (leaked or observed != baseline) else P2_PROVED,
                          detector="P2_LIVE_ECHO_SPEC_CHANNEL", ran=True,
                          perturbation=f"collection -> {sorted(perturbed)!r} in the payload",
                          recomputed=True,
                          condition_checked="the observation is unchanged and the sentinel does "
                                            "not appear inside it")

    # --- X2 AMBIENT CHANNEL --------------------------------------------------------------------
    # Module state is not un-enumerable — it is enumerable BY NAME from the executed code object.
    # So it is perturbed here rather than merely forbidden, and the refusal is evidence (the
    # observation moved in lockstep with a module-level copy of the collection) rather than a
    # structural objection.
    namespace = getattr(fn, "__globals__", None)
    ambient_perturbed: list = []
    ambient_failed = False
    for channel, value in _p2_ambient_globals(fn).items():
        if not isinstance(value, (dict, set, frozenset, list, tuple)) or namespace is None:
            continue
        substituted_value, carries_collection = ((None, False) if not target else
                                                 _p2_substitute(value, target, perturbed))
        if carries_collection:
            # The sentinel here STANDS IN FOR the collection under test, so a leak of it into the
            # observation is evidence of an echo of that collection.
            replacement, kind, detail = (substituted_value, "P2_LIVE_ECHO_AMBIENT_CHANNEL",
                                         f"module state {channel!r} holds a copy of the collection "
                                         "under test, and the observation MOVED in lockstep when "
                                         "that copy was perturbed. The witness is recomputing its "
                                         "'independent' answer from the very collection it "
                                         "certifies — its declared authority is not where the "
                                         "answer comes from")
            leak_kind, leak_detail = ("P2_LIVE_ECHO_CANARY_LEAKED",
                                      f"the framework's synthetic member {sentinel!r}, standing in "
                                      f"for the collection under test inside module state "
                                      f"{channel!r}, came back INSIDE the observation")
        else:
            replacement, changed = _p2_perturb_node(value, sentinel)
            if not changed:
                continue
            # Here the sentinel is NOT a stand-in for the collection — it is a probe of an
            # undeclared channel — so a leak proves that dependency, not an echo. Reporting it as
            # an echo would name a detector that did not fire, and a reader debugging the wrong
            # finding never looks for the right one.
            kind, detail = ("P2_UNDECLARED_AMBIENT_DEPENDENCY",
                            f"the observation MOVED when undeclared module state {channel!r} was "
                            "perturbed. The witness depends on an authority its spec never "
                            "declares, so no review saw the dependency and no perturbation of the "
                            "DECLARED authority would have exposed it")
            leak_kind, leak_detail = (kind, detail + f" (the probe member {sentinel!r} injected "
                                      f"into {channel!r} came back inside the observation, so the "
                                      "dependency is direct rather than incidental)")
        original = namespace[channel]
        namespace[channel] = replacement
        ambient_perturbed.append(channel)
        try:
            observed, leaked = trial(payload, restore=lambda: namespace.__setitem__(channel,
                                                                                    original))
        except Exception as exc:
            namespace[channel] = original
            refuse("P2_WITNESS_UNRUNNABLE_UNDER_PERTURBATION",
                   f"the witness raised {type(exc).__name__}: {exc} when module state {channel!r} "
                   "was perturbed; a witness that only survives its own ambient state cannot be "
                   "shown independent of the collection")
            ledger.record("R3_AMBIENT_INVARIANCE", P2_FAILED,
                          detector="P2_WITNESS_UNRUNNABLE_UNDER_PERTURBATION", ran=True,
                          channels_resolved=ambient_perturbed, recomputed=True,
                          perturbation="module state rebound to a sentinel-bearing copy",
                          condition_checked="the witness is runnable and its observation is "
                                            "unchanged under every enumerable module channel")
            ledger.abort("the witness is unrunnable under an ambient perturbation")
            return problems + ledger.finish(problems, relation)
        if leaked:
            refuse(leak_kind, leak_detail)
            ambient_failed = True
        elif observed != baseline:
            refuse(kind, detail)
            ambient_failed = True
    if ambient_perturbed:
        ledger.record("R3_AMBIENT_INVARIANCE", P2_FAILED if ambient_failed else P2_PROVED,
                      detector="P2_LIVE_ECHO_AMBIENT_CHANNEL / P2_UNDECLARED_AMBIENT_DEPENDENCY",
                      ran=True, channels_resolved=ambient_perturbed, recomputed=True,
                      perturbation="each enumerable module channel rebound in turn to a "
                                   "sentinel-bearing copy",
                      condition_checked="the observation is unchanged and the sentinel does not "
                                        "appear inside it, for every channel")
    else:
        ledger.record("R3_AMBIENT_INVARIANCE", P2_NOT_APPLICABLE,
                      basis="COVERED_BY_R4_UNPERTURBABLE_AMBIENT",
                      detector="P2_LIVE_ECHO_AMBIENT_CHANNEL", ran=True,
                      perturbation="enumeration of co_names against __globals__ found no "
                                   "rebindable container",
                      condition_checked="R4 ran on the same witness and refuses every ambient "
                                        "channel that is not perturbable")

    # --- X3 WHAT CANNOT BE PERTURBED AT ALL ----------------------------------------------------
    static_ambient = _p2_static_ambient(fn, who)
    for kind, _channel, detail in static_ambient:
        refuse(kind, detail)
    ledger.record("R4_NO_UNPERTURBABLE_AMBIENT", P2_FAILED if static_ambient else P2_PROVED,
                  detector="P2_ECHO_UNEXCLUDED_AMBIENT_CHANNEL / P2_ECHO_UNEXCLUDED_CLOSURE_CELL",
                  ran=True, channels_resolved=[str(c) for _k, c, _d in static_ambient if c],
                  perturbation="none possible — this limb REFUSES what it cannot perturb",
                  condition_checked="the witness reaches no closure cell and no ambient channel "
                                    "outside the enumerable module namespace")

    # --- X4 MUST_DEPEND ON A DECLARED AUTHORITY — RUN OR FAIL CLOSED ---------------------------
    # The opposite polarity, and the ONLY limb that refuses a CONSTANT. It used to run only where
    # the declared authority happened to be a registered channel, and to vanish silently otherwise:
    # a spec naming `external.authority` (unregistered) skipped it, emitted nothing, and reached
    # CLEAN. That skip is now one of four recorded states, and the unresolved case is CANNOT_PROVE,
    # which fails closed.
    #
    # WHY UNRESOLVED IS NOT "NO OBLIGATION". A witness passing X3 reaches NO ambient state, so it
    # cannot have FETCHED the authority it names; and with no registered channel the framework
    # INJECTED nothing. Its observation is therefore a function of the payload alone — an authored
    # constant wearing a `reads` declaration. The declaration is precisely the claim P2 exists to
    # test, and an untestable claim is refused, not waved through.
    if not any(_p1_names_a_source(spec, field) for field in _INDEPENDENCE_DECLARATION_FIELDS):
        # NOT_APPLICABLE only if an INDEPENDENTLY-EQUIVALENT mechanism refuses the spec on this
        # same run. The mechanism is P1's independence obligation — and the test is the ENFORCEMENT
        # PATH (`_p1_activation_problems`, the function verify_provider calls), not the guard
        # function. `guard_independence` does carry the rule, but the entry point deliberately does
        # not invoke that half (see the residual noted at _p1_activation_problems), so asking the
        # guard would report a refusal that never happens: a fix landed on the validator and not on
        # the verifier is the I28AM defect, and here it would have manufactured an exemption.
        # On the shipped configuration this evaluates FALSE, so the state is CANNOT_PROVE.
        p1_refuses = any(problem.get("kind") == "INDEPENDENCE_UNDECLARED"
                         for problem in _p1_activation_problems(relation, spec, cid))
        ledger.record("R5_MUST_DEPEND_DECLARED_AUTHORITY",
                      P2_NOT_APPLICABLE if p1_refuses else P2_CANNOT_PROVE,
                      basis="CONTRACT_P1_INDEPENDENCE_ACTIVATION" if p1_refuses else None,
                      detector="P2_DECLARED_READ_INERT", ran=True,
                      condition_checked="_p1_activation_problems() yields INDEPENDENCE_UNDECLARED "
                                        "for this spec on this evaluation",
                      note="" if p1_refuses else
                           "the spec names no independent source, and the evaluation path does "
                           "not refuse it for that omission, so NOTHING on this run establishes "
                           "where the observation comes from")
        if not p1_refuses:
            refuse("P2_DEPENDENCY_UNDECLARED",
                   "the spec names no independent source at all (neither `reads` nor "
                   "`independent_source`), so there is no dependency for the framework to inject, "
                   "perturb or verify — and this witness passed the ambient limbs, so it fetched "
                   "nothing either. Its observation is a function of its payload alone: an "
                   "authored answer presented as an independent observation. P2 cannot decide the "
                   "independence claim, and an undecidable security claim is REFUSED rather than "
                   "skipped. Declare the authority and register it as a P6 channel",
                   P2_MUST_DEPEND)
    elif not resolvable or not channels:
        declared = [spec.get(field) for field in _INDEPENDENCE_DECLARATION_FIELDS
                    if _p1_names_a_source(spec, field)]
        ledger.record("R5_MUST_DEPEND_DECLARED_AUTHORITY", P2_CANNOT_PROVE,
                      detector="P2_DECLARED_READ_INERT", ran=False,
                      channels_resolved=[],
                      condition_checked="every declared authority resolves to registered P6 "
                                        "channel content the framework can inject and perturb",
                      note=f"the spec declares {declared!r}, which resolves to no registered P6 "
                           "channel, so the framework injected nothing and had nothing to perturb")
        refuse("P2_DEPENDENCY_UNPROVABLE",
               f"the spec declares it reads {declared!r}, but no such P6 channel is registered, so "
               "the framework injected no content and could not perturb the authority. The "
               "MUST_DEPEND experiment therefore could not run — and this witness passed the "
               "ambient limbs, which means it FETCHED nothing either: its observation is a "
               "function of its payload alone. A constant, a stale copy and a genuine reading of "
               "the authority are indistinguishable in that state, so the independence claim is "
               "UNPROVABLE and is REFUSED rather than skipped. Register the authority as a P6 "
               "channel so the framework injects it and the dependency becomes an experiment",
               P2_MUST_DEPEND)
    else:
        # THE ADAPTIVE-WITNESS CLOSURE (gate SLICE1-FI).  This limb used to run ONE perturbation
        # per channel, and that perturbation announced itself: it added a member spelled
        # `__P2_<hex>__`.  The witness is attacker code the framework EXECUTES and the framework
        # HANDS IT the perturbed input, so "was I perturbed?" was a question the witness could
        # answer — and a witness that answers it can be honest for the experiment and constant for
        # the verdict.  The schedule below removes the question: the variants are SUBSETS of the
        # authority's own real content (indistinguishable from a smaller real authority), the one
        # injected member is shaped like its neighbours and absent from every real source, and
        # UNCHANGED trials are interleaved so position cannot substitute for content.
        #
        # THREE LAWS, each refusing a different way of not reading the authority:
        #
        #   FUNCTIONALITY   the same content must produce the same observation, wherever in the
        #                   sequence it is asked.  Refuses discrimination by call position or count.
        #   ACCOUNTABILITY  a member the observation ASSERTS, which this authority is the only
        #                   declared source of, must disappear from the observation when the
        #                   authority stops listing it.  Refuses an assertion the authority does not
        #                   cause — which is what a constant, a cached copy and an authored answer
        #                   all are.
        #   MONOTONICITY    restricting the authority may not GROW the observation.  This is the law
        #                   that catches the residual no perturbation can reach: a witness holding a
        #                   copy of the true content and lying on exactly that input is honest on
        #                   every subset, and its honest subset answers are not below its lie.
        #
        # DEPENDENCE IS ESTABLISHED BY RESTRICTION ONLY.  An injected member is the one thing a
        # witness could still notice, so movement on the injected trial deliberately does NOT count
        # towards dependence: a witness that reacts only to the value it can tell is new has shown
        # nothing about whether it reads the authority.
        depend_state = P2_PROVED
        inert_channels: list = []
        unperturbable: list = []
        baseline_members = _p2_member_strings(base_a)
        pool = _p2_real_value_pool(spec, collection, channels)
        for channel in sorted(channels):
            content = channels[channel]
            elsewhere = {str(m) for other, value in channels.items() if other != channel
                         for m in _p2_member_strings(value)}
            probe_member = _p2_probe_member(_p2_member_strings(content), pool, cid, name, channel)
            schedule = _p2_channel_trials(content, baseline_members, probe_member)
            if not schedule:
                # A registered channel whose content has no enumerable member cannot be varied, so
                # this channel's dependency is undecidable. It is NOT a pass.
                unperturbable.append(channel)
                continue
            seen: dict = {}
            moved_under_restriction = False
            law_broken = False
            for label, dropped, is_restriction, trial_content in schedule:
                sources = dict(channels)
                sources[channel] = trial_content
                try:
                    raw = run(_p2_witness_payload(spec, cid, sources))
                    observed = _p2_canon(raw)
                except Exception as exc:
                    refuse("P2_WITNESS_UNRUNNABLE_UNDER_PERTURBATION",
                           f"the witness raised {type(exc).__name__}: {exc} when its declared "
                           f"authority {channel!r} was presented as {label} — an ordinary, smaller "
                           "reading of the same authority. A witness that only survives one exact "
                           "value of its authority is not reading it", P2_MUST_DEPEND)
                    ledger.record("R5_MUST_DEPEND_DECLARED_AUTHORITY", P2_FAILED,
                                  detector="P2_WITNESS_UNRUNNABLE_UNDER_PERTURBATION", ran=True,
                                  channels_resolved=sorted(channels), recomputed=True,
                                  perturbation=f"declared authority {channel!r} -> {label}",
                                  condition_checked="the observation MOVES when the declared "
                                                    "authority is restricted")
                    ledger.abort("the witness is unrunnable under a declared-authority trial")
                    return problems + ledger.finish(problems, relation)
                key = _p2_canon(trial_content)
                if key in seen and seen[key][0] != observed:
                    law_broken = True
                    refuse("P2_TRIAL_DISCRIMINATING_WITNESS",
                           f"the witness answered the SAME content for {channel!r} differently at "
                           f"two positions in the trial sequence ({seen[key][1]} then {label}). Its "
                           "observation is therefore not a function of its input: something other "
                           "than the authority — call order, a counter, a mode flag — is choosing "
                           "the answer. A witness that can tell an experiment from a live "
                           "evaluation can be honest for one and authored for the other, so this "
                           "is refused whatever the two answers were", P2_MUST_DEPEND)
                seen.setdefault(key, (observed, label))
                if not is_restriction:
                    continue
                if observed != baseline:
                    moved_under_restriction = True
                trial_members = _p2_member_strings(raw)
                if dropped is not None and dropped in baseline_members \
                        and dropped not in elsewhere and dropped in trial_members:
                    law_broken = True
                    refuse("P2_ASSERTION_NOT_ACCOUNTABLE",
                           f"the observation still asserts {dropped!r} after {channel!r} — the only "
                           f"declared source that listed it — stopped listing it. The authority "
                           "does not CAUSE that assertion, so whatever produced it (a constant, a "
                           "cached copy, an authored literal, a second undeclared source) is not "
                           "the authority the spec names. Each member an observation asserts must "
                           "be accountable to the authority it is offered as a reading of",
                           P2_MUST_DEPEND)
                if not trial_members <= baseline_members:
                    law_broken = True
                    extra = sorted(m for m in trial_members if m not in baseline_members)
                    refuse("P2_OBSERVATION_NOT_MONOTONE",
                           f"restricting the declared authority {channel!r} ({label}) made the "
                           f"observation GROW: {extra!r} appeared once the authority listed LESS. "
                           "An authority that requires fewer members cannot make a reading of it "
                           "require more, so the shipped observation is not a reading of this "
                           "authority — it is an answer that happens to be given when the authority "
                           "has one particular value, which is the shape of a witness that "
                           "recognises the live input and answers it specially",
                           P2_MUST_DEPEND)
            if not moved_under_restriction and not law_broken:
                inert_channels.append(channel)
                refuse("P2_DECLARED_READ_INERT",
                       f"the observation did NOT move under ANY restriction of the declared "
                       f"authority {channel!r} — not when single members were withdrawn and not "
                       "when the authority was emptied — so the witness does not consume the "
                       "authority it names. A constant, a cached answer and an authority that is "
                       "never read are indistinguishable here, and none of the three is an "
                       "observation", P2_MUST_DEPEND)
            if law_broken:
                depend_state = P2_FAILED
        if inert_channels:
            depend_state = P2_FAILED
        elif unperturbable:
            depend_state = P2_CANNOT_PROVE
            refuse("P2_DEPENDENCY_UNPROVABLE",
                   f"the declared authority/authorities {unperturbable!r} carry no enumerable "
                   "member the framework can vary, so no perturbation of them exists and the "
                   "MUST_DEPEND experiment cannot decide anything. An authority that cannot be "
                   "varied cannot be shown to be read; REFUSED rather than skipped",
                   P2_MUST_DEPEND)
        ledger.record("R5_MUST_DEPEND_DECLARED_AUTHORITY", depend_state,
                      detector="P2_DECLARED_READ_INERT / P2_TRIAL_DISCRIMINATING_WITNESS / "
                               "P2_ASSERTION_NOT_ACCOUNTABLE / P2_OBSERVATION_NOT_MONOTONE",
                      ran=True, channels_resolved=sorted(channels), recomputed=True,
                      perturbation="each declared authority presented, in an unmarked interleaved "
                                   "schedule, as a series of RESTRICTIONS of its own real content "
                                   "plus one shaped member absent from every real source",
                      condition_checked="the observation MOVES under restriction, is a FUNCTION of "
                                        "the content, asserts no member the authority stopped "
                                        "listing, and never GROWS when the authority shrinks",
                      note=(f"unperturbable channels: {unperturbable!r}" if unperturbable else ""))

    # --- X5 THE AUTHORITY IS AN AUTHORITY (Part G) ---------------------------------------------
    # X4 proves the witness CONSUMES what it declares. That is worth nothing if what it declares is
    # a copy of the collection under test: the witness would be honest, the dependency real, and
    # the answer still the collection restating itself (FF-04). This limb asks the question X4
    # cannot — where did the channel's CONTENT come from — and it runs on the provider path for
    # the same reason it runs on the code-native one: a check on one of two routes is the
    # sibling-layer defect.
    if resolvable and channels:
        provenance = _p6_channel_provenance_problems(channels, collection, cid, relation)
        for problem in provenance:
            problems.append(problem)
        ledger.record("R6_CHANNEL_CONTENT_PROVENANCE",
                      P2_FAILED if provenance else P2_PROVED,
                      detector="CHANNEL_CONTENT_UNPROVENANCED", ran=True,
                      channels_resolved=sorted(channels), recomputed=True,
                      perturbation="each channel's producer re-executed and its content re-digested",
                      condition_checked="every injected authority is bound to a reviewed producer "
                                        "whose identity re-verifies and whose content recomputes "
                                        "to the digest pinned at registration")
    else:
        ledger.record("R6_CHANNEL_CONTENT_PROVENANCE", P2_NOT_APPLICABLE,
                      basis="COMPUTED_NO_AUTHORITY_INJECTED",
                      detector="CHANNEL_CONTENT_UNPROVENANCED", ran=True,
                      condition_checked="the framework injected channel content on this run",
                      note="nothing was injected, so there is no content whose origin could be "
                           "checked; R5 has already refused the state that produces it")
    return problems + ledger.finish(problems, relation)


def p2_ne_witness_closure(spec: dict, cid: str) -> list:
    """P2(2b) for PART D: the ambient-channel closure, as problem STRINGS.

    Part D's five guarantee kinds call their witness with payloads only the kind knows how to build
    (a member and a `must`, a source and a spec), so the payload-channel trial is run by the kind
    that owns the payload — kind D's irrelevant-mutation leg above is exactly that trial. What is
    common to all five is the channel the framework can inspect without executing anything: the
    module state and closure cells the witness's own code reaches. An echo through those is
    excluded here, before any kind-specific probe runs.
    """
    if not isinstance(spec, dict):
        return []
    try:
        witness = _witness_ref(spec)
    except Exception:
        return []
    name = witness.get("provider") if isinstance(witness, dict) else witness
    if not isinstance(name, str) or not name:
        return []
    try:
        fn, _identity = NE_PROVIDERS.resolve(name)
    except WitnessIdentityError:
        return []                       # P9 owns identity; it refuses this on its own path
    return [f"{cid}: REFUSED — {kind}: {detail}"
            for kind, _channel, detail in _p2_static_ambient(fn, f"non-enumerable witness {name!r}")]


# ================================================================================================
# PART F — RELATION / PROBE TOTALITY   (gate 4N-I28BH-B0w-R2-SLICE1-TOTALITY, finding 3)
# ================================================================================================
# THE DEFECT THIS CLOSES (FF-01, executed).  The operand-semantics oracle that adjudicates whether
# a relation's DECLARED load-bearing operands cover its COMPUTED vacuity lives OUTSIDE the module,
# in a battery, and it is keyed by a hand-written probe table.  A relation with no entry in that
# table is reported `NO_PROBE (void — cannot adjudicate)` and the audit then says UNGUARDED=[] —
# i.e. an unadjudicated relation is indistinguishable from an adjudicated clean one, and a 14th
# relation that is C-load-bearing while declaring {"domain"} passes the whole battery and certifies
# an EMPTY collection.  Worse, and found by this closure rather than assumed: the shipped registry
# already contains a relation the banked probe table never covered at all
# (POSITIVE_CONTROL_PRESENCE), so "NO_PROBE -> silent" is not a hypothetical about the future.
#
# THE RULE.  For every relation the LIVE registry contains, one of the following must hold, and
# there is no fourth option:
#
#   PROVED            a probe EXECUTED, produced the finding it declares, and no operand whose
#                     emptying silences that finding is missing from _LOAD_BEARING_OPERANDS.
#   VACUITY_ACCEPTED  a computed vacuity exists and is named in a REVIEWED exemption with a stated
#                     reason. Surfacing it is the point: the fact stays visible in the table.
#   UNPROVED          anything else — no probe, a probe that reports nothing, a probe whose
#                     declared finding never appears, or an undeclared vacuity. FAIL CLOSED.
#
# ENUMERATION IS FRESH AND INDEPENDENT.  The table is built by walking the LIVE `_REGISTRY`, never
# the probe table: enumerating from the probe table is what made a missing probe invisible, since a
# relation absent from both is absent from the report as well.  `RELATIONS` is a snapshot taken at
# import and would MISS a relation injected afterwards, which is exactly the attack.
#
# THE PROBES RUN THROUGH compare(), NOT THROUGH THE RAW CHECKER.  The banked oracle indexed
# `_REGISTRY` and invoked the checker directly to observe "the relation's semantics rather than the
# guard's".  That measures a component; the security property is about the ENFORCEMENT PATH, where
# the presence gate is armed by the very declaration under audit.  Running through the public entry
# also keeps P4's INV-2 intact — nothing here indexes the registry.
#
# WHERE IT FAILS CLOSED.  In `_witness_evaluation_gate`, the single chokepoint every route reaches,
# so a relation with no adjudicated probe cannot produce a verdict on ANY route rather than only in
# a battery somebody remembered to run.

_RELATION_PROBE_ARMED = False           # set once, at the bootstrap below; never cleared
_RELATION_PROBE_DEPTH = 0               # >0 while a probe is executing (re-entrancy)
_RELATION_PROBE_VERDICTS: dict = {}

# A DIRTY probe per relation: the operands and spec on which the relation must report the finding
# named in `expects`. `kind` records honestly what the probe reaches:
#   COMPARISON  the probe drives the relation's own comparison arm.
#   REFUSAL     the probe drives one of the relation's own refusals (its self-adequacy witness
#               obligation). Stated rather than dressed up: a comparison-arm probe for these would
#               need a witness-produced field, which only the provider path can stamp, and claiming
#               a comparison probe here would be a claim the table cannot support.
_RELATION_PROBES: dict = {}


class _synthetic_evaluation:
    """A scope in which comparisons are SYNTHETIC — invented operands whose verdict is a finding
    about the framework, never a certification of a collection.

    The relation/probe totality check is suspended inside it, and ONLY inside it. Two callers
    qualify and both are already established as unreachable from a real adjudication (P4 INV-4b):
    the probe adjudication itself, and the by-execution auditors, which must be able to compare a
    strict run against a lenient one on a relation they have just introduced. Nothing that can
    certify a collection opens this scope; if something ever did, INV-4b would fail first.
    """

    def __enter__(self):
        global _RELATION_PROBE_DEPTH
        _RELATION_PROBE_DEPTH += 1
        return self

    def __exit__(self, *_exc):
        global _RELATION_PROBE_DEPTH
        _RELATION_PROBE_DEPTH -= 1
        return False


def _register_relation_probe(relation: str, domain, collection, spec: dict, *, expects: str,
                             kind: str = "COMPARISON", rationale: str = "") -> None:
    """Declare the probe that adjudicates one relation. A relation without one cannot evaluate.

    Adjudicated IMMEDIATELY, so a probe that does not fire is rejected at the point it is added
    rather than at the point somebody relies on it. Registering is therefore an experiment, not a
    declaration — the same inversion the P6 channel registry makes for authorities.
    """
    if kind not in ("COMPARISON", "REFUSAL"):
        raise ContractPinError(f"relation probe {relation!r} declares kind {kind!r}; a probe "
                               "either drives the comparison arm or one of the relation's own "
                               "refusals, and which one it is may not be left unsaid")
    _RELATION_PROBES[relation] = {"domain": domain, "collection": collection, "spec": dict(spec),
                                  "expects": expects, "kind": kind, "rationale": rationale}
    _RELATION_PROBE_VERDICTS.pop(relation, None)
    row = _relation_probe_row(relation)
    if row["verdict"] == "UNPROVED":
        raise ContractPinError(
            f"the probe registered for relation {relation!r} does not adjudicate it: {row['why']}. "
            "A probe that cannot fail cannot certify; REFUSED at registration")


# Operands whose emptying silences a relation's finding and which are NEVERTHELESS accepted, with
# the reason. An entry here is a REVIEWED admission that the relation passes vacuously in that
# configuration — it does not make the vacuity go away, it makes it impossible to hold silently.
_RELATION_VACUITY_ACCEPTED: dict = {
    "POSITIVE_CONTROL_PRESENCE": {
        "domain": (
            "FOUND BY THIS CLOSURE, not assumed. Emptying the DOMAIN silences "
            "POSITIVE_CONTROL_ABSENT completely (the verdict becomes []), and `domain` is not in "
            "this relation's _LOAD_BEARING_OPERANDS. The domain here is the spec's "
            "`required_present` list, so the vacuity is reached by EVERY Part D spec that omits "
            "it: the positive-control transit those specs make certifies nothing. It is recorded "
            "rather than refused because arming the presence gate on this operand would refuse "
            "every such spec, which is a Part D design decision and not this gate's to take. It "
            "is a LIVE RESIDUAL and it is named here so it cannot be held silently again"),
    },
}


def _relation_probe_compare(relation: str, probe: dict, domain, collection) -> list:
    """Run ONE probe trial and return the kinds it reported.

    Module-level and named, deliberately. P4's INV-4b enumerates every caller of compare() by
    ENCLOSING SCOPE, and a nested helper called `run` would put the string "run" in that list —
    an evaluation path named after nothing, which is absorption with extra steps. A reader
    auditing the routes into the comparator should be able to grep the name and land on the
    contract that licenses it.
    """
    return [problem.get("kind") for problem in compare(relation, domain, collection,
                                                       dict(probe["spec"]))]


def _relation_probe_row(relation: str) -> dict:
    """Adjudicate ONE relation and memoise the row. Executes the probe; never asks a declaration."""
    cached = _RELATION_PROBE_VERDICTS.get(relation)
    if cached is not None:
        return cached
    declared = sorted(_LOAD_BEARING_OPERANDS.get(relation, {"domain"}))
    probe = _RELATION_PROBES.get(relation)
    if probe is None:
        row = {"relation": relation, "declared": declared, "probe": None, "computed": None,
               "weakened": [], "verdict": "UNPROVED",
               "why": "no probe is registered for this relation, so nothing has ever established "
                      "that its declared load-bearing operands cover the operands its verdict "
                      "actually depends on. An unprobed relation is not a clean one"}
        _RELATION_PROBE_VERDICTS[relation] = row
        return row

    def run(domain, collection):
        return _relation_probe_compare(relation, probe, domain, collection)

    def emptied(value):
        return {} if isinstance(value, dict) else set()

    try:
        with _synthetic_evaluation():
            baseline = run(probe["domain"], probe["collection"])
            silenced, weakened = [], []
            if probe["expects"] in baseline:
                for operand in ("domain", "collection"):
                    domain = emptied(probe["domain"]) if operand == "domain" else probe["domain"]
                    collection = (emptied(probe["collection"]) if operand == "collection"
                                  else probe["collection"])
                    after = run(domain, collection)
                    # VACUITY is the verdict going CLEAN — that is the fail-open the presence gate
                    # exists to arm against, and the only state in which an emptied operand
                    # CERTIFIES anything.
                    if not after:
                        silenced.append(operand)
                    elif probe["expects"] not in after:
                        # The declared finding disappeared but the relation still refuses, by a
                        # sibling arm. Not a vacuous pass, so not fail-closed — recorded, because
                        # it is the state one edit away from one (KEYED_MAPPING's value arm goes
                        # quiet when the collection is emptied; only its KEY arm still refuses).
                        weakened.append(operand)
    except Exception as exc:                       # a probe that crashes adjudicates nothing
        _RELATION_PROBE_VERDICTS[relation] = {
            "relation": relation, "declared": declared, "probe": probe["kind"], "computed": None,
            "weakened": [], "verdict": "UNPROVED",
            "why": f"the probe raised {type(exc).__name__}: {exc}"}
        return _RELATION_PROBE_VERDICTS[relation]

    if probe["expects"] not in baseline:
        row = {"relation": relation, "declared": declared, "probe": probe["kind"],
               "computed": None, "weakened": [], "verdict": "UNPROVED",
               "why": f"the probe declares it fires {probe['expects']} but the baseline verdict "
                      f"was {sorted(set(baseline))}. A probe that does not report the finding it "
                      "claims cannot show anything is silenced, so every 'no vacuity' result it "
                      "would produce is void"}
        _RELATION_PROBE_VERDICTS[relation] = row
        return row
    accepted = _RELATION_VACUITY_ACCEPTED.get(relation) or {}
    undeclared = [operand for operand in silenced
                  if operand not in declared and operand not in accepted]
    exempted = [operand for operand in silenced if operand not in declared and operand in accepted]
    if undeclared:
        row = {"relation": relation, "declared": declared, "probe": probe["kind"],
               "computed": sorted(silenced), "weakened": sorted(weakened), "verdict": "UNPROVED",
               "why": f"emptying {undeclared!r} SILENCES this relation's {probe['expects']} "
                      "finding, and that operand is not in its declared load-bearing set, so the "
                      "presence gate is not armed on it. An empty operand then certifies the "
                      "collection vacuously — the UNGUARDED_VACUITY class"}
    elif exempted:
        row = {"relation": relation, "declared": declared, "probe": probe["kind"],
               "computed": sorted(silenced), "weakened": sorted(weakened),
               "verdict": "VACUITY_ACCEPTED",
               "why": "; ".join(accepted[operand] for operand in exempted)}
    else:
        row = {"relation": relation, "declared": declared, "probe": probe["kind"],
               "computed": sorted(silenced), "weakened": sorted(weakened), "verdict": "PROVED",
               "why": f"the probe fires {probe['expects']}, and every operand whose emptying "
                      "silences it is declared load-bearing"}
    _RELATION_PROBE_VERDICTS[relation] = row
    return row


def _relation_probe_totality() -> dict:
    """The §8 table: every relation the LIVE registry contains, adjudicated or fail-closed.

    `relations` is read from `_REGISTRY` itself and NOT from `_RELATION_PROBES`, so a relation with
    no probe appears as a row rather than as an absence — which is the whole finding.
    """
    rows = [_relation_probe_row(relation) for relation in sorted(_REGISTRY)]
    unproved = [row["relation"] for row in rows if row["verdict"] == "UNPROVED"]
    return {"relations": len(rows),
            "probed_or_proved": len([r for r in rows if r["verdict"] != "UNPROVED"]),
            "proved": len([r for r in rows if r["verdict"] == "PROVED"]),
            "vacuity_accepted": len([r for r in rows if r["verdict"] == "VACUITY_ACCEPTED"]),
            "unproved": unproved, "rows": rows}


def _relation_probe_refusal(relation: str, canonical: str):
    """The gate's fail-closed check. None admits; a Problem refuses.

    Consulted on EVERY evaluation, so a relation injected into the registry after import — the
    exact FF-01 shape — cannot reach a verdict, on any route, however it was wired in.

    IT READS THE MEMO AND NEVER ADJUDICATES. Adjudicating here would run a synthetic comparison
    INSIDE a real verdict, which is precisely what P4's INV-4b forbids of the auditors and for the
    same reason. A relation with no memoised verdict is therefore UNPROVED by absence — which is
    the correct answer anyway: adjudication happens at `_register_relation_probe` (a reviewed
    diff) and at the import bootstrap, both outside any evaluation.
    """
    if _RELATION_PROBE_DEPTH or not _RELATION_PROBE_ARMED:
        return None                     # the probes' own evaluations, and the bootstrap itself
    row = _RELATION_PROBE_VERDICTS.get(canonical)
    if row is None:
        return _problem(relation, "RELATION_PROBE_UNPROVED",
                        f"relation {canonical!r} is in the registry but has NO adjudicated probe: "
                        "nothing has ever established that its declared load-bearing operands "
                        "cover the operands its verdict actually depends on. A relation added "
                        "after the bootstrap — by an import, a patch or a test — reaches this "
                        "branch, which is the point: an unprobed relation defaulting to clean is "
                        "how a 14th relation ships with its vacuity unguarded. Register a probe "
                        "through _register_relation_probe (it is EXECUTED at registration and "
                        "refuses a probe that does not fire); REFUSED")
    if row["verdict"] == "UNPROVED":
        return _problem(relation, "RELATION_PROBE_UNPROVED",
                        f"relation {canonical!r} has no adjudicated probe: {row['why']}. A "
                        "relation whose load-bearing operands nobody has CHECKED against its own "
                        "behaviour cannot produce a verdict — an unprobed relation defaulting to "
                        "clean is how a 14th relation ships with its vacuity unguarded. Register "
                        "a probe that fires, and declare the operands it shows are load-bearing; "
                        "REFUSED")
    return None


# --- THE SHIPPED PROBES -------------------------------------------------------------------------
# One per relation the registry ships, authored HERE rather than in a battery so the adjudication
# travels with the code it adjudicates. Each is registered through _register_relation_probe, which
# EXECUTES it immediately and refuses a probe that does not fire — so this block is a run of the
# experiment at import time, not a table of intentions.
_register_relation_probe("EXACT", {"a", "b"}, {"b", "c"}, {}, expects="MISSING")
_register_relation_probe("REQUIRED_SUBSET", {"a"}, {"a", "c"}, {}, expects="UNJUSTIFIED")
_register_relation_probe("REQUIRED_SUPERSET", {"a", "b"}, {"a"}, {}, expects="REQUIRED_ABSENT")
_register_relation_probe("DISJOINT", {"a"}, {"a"}, {}, expects="FORBIDDEN_OVERLAP")
_register_relation_probe("DISJOINT_WITH_FLOOR", {"a"}, {"a"},
                         {"presence": {"policy": "INVALID_EMPTY", "floor": ["a"]}},
                         expects="FORBIDDEN_OVERLAP")
_register_relation_probe("PARTITION", {"a", "b"}, {"a", "b"}, {"partition_members": [["a"]]},
                         expects="PARTITION_SHORT")
_register_relation_probe("KEYED_MAPPING", {"k": {"x"}}, {"k": {"x", "y"}},
                         {"key_relation": "EXACT", "value_relation": "REQUIRED_SUBSET"},
                         expects="UNJUSTIFIED")
_register_relation_probe("KEYED_MAPPING_AGAINST_UNION", {"k1", "k2"}, {"k1": ["x"]}, {},
                         expects="MISSING")
_register_relation_probe("SCHEMA_STRICTNESS", {"a", "b"}, {"b"}, {}, expects="MISSING")
_register_relation_probe("PROVENANCE_CORRESPONDENCE", {"a"}, {"a", "c"},
                         {"correspondence": "injective"}, expects="NO_PROVENANCE",
                         kind="REFUSAL",
                         rationale="the comparison arm needs a provenance mapping the relation "
                                   "will only accept from executed code; this probe drives its "
                                   "own missing-provenance refusal instead, and says so")
_register_relation_probe("SEMANTIC_REACHABILITY", {"a", "b"}, {"a"}, {"strict_reachability": True},
                         expects="UNREACHED_REQUIRED")
_register_relation_probe("DIFFERENTIAL_EXECUTION", {"a"}, {"a", "b"}, {},
                         expects="NO_MEMBER_EVIDENCE", kind="REFUSAL",
                         rationale="member_effect is a self-adequacy field the relation accepts "
                                   "ONLY from an executed witness, which the bare comparator "
                                   "entry cannot stamp; this probe drives the relation's own "
                                   "no-evidence refusal. Its comparison arm is adjudicated on the "
                                   "provider path, not here, and that limit is stated rather than "
                                   "papered over")
_register_relation_probe("HASH_BACKSTOP", {"a"}, {"a", "c"},
                         {"baseline_hash": hashlib.sha256(
                             json.dumps(["a"], separators=(",", ":")).encode("utf-8")).hexdigest()},
                         expects="UNJUSTIFIED")
_register_relation_probe("POSITIVE_CONTROL_PRESENCE", {"a"}, {"b"}, {},
                         expects="POSITIVE_CONTROL_ABSENT")

# THE BOOTSTRAP. Every relation the registry ships is adjudicated HERE, at import, and the module
# refuses to load if one of them is unproved — the same rule the P6 governed primitives are held
# to. Arming afterwards is what makes the gate's check meaningful: from this point a relation with
# no adjudicated probe cannot reach a verdict on any route.
_RELATION_PROBE_BOOTSTRAP = _relation_probe_totality()
if _RELATION_PROBE_BOOTSTRAP["unproved"]:
    raise RuntimeError(
        "relations ship with no adjudicated probe: "
        f"{_RELATION_PROBE_BOOTSTRAP['unproved']}. A relation whose declared load-bearing operands "
        "have never been checked against its own behaviour cannot be allowed to certify anything")
_RELATION_PROBE_ARMED = True


# ================================================================================================
# PART G — CHANNEL CONTENT PROVENANCE   (gate 4N-I28BH-B0w-R2-SLICE1-TOTALITY, finding 2)
# ================================================================================================
# THE DEFECT THIS CLOSES (FF-04(b), executed).  `register_p6_channel()` asks nothing about WHERE a
# channel's content came from.  The no-override rule protects an EXISTING channel from being
# rebound; the FIRST registration is unreviewed.  So an "independent authority" assembled by
# COPYING the collection under test —
#
#     F.register_p6_channel("ff.derived", set(SHORTENED_COLLECTION))
#
# — is admitted, and the witness that reads it is certified independent by P6 AND by P2, because
# every experiment either layer runs is about the WITNESS.  Both are asking "does this witness
# consume the authority it declares?" and the honest answer is yes; nobody asks whether the
# authority is an authority.
#
# WHY CONTENT ALONE CANNOT DECIDE IT.  An honest authority AGREES with a correct collection — that
# is what makes it useful.  So "the channel equals the collection" is the honest case and the lie
# at the same time, and no comparison of the two can separate them.  Only PROVENANCE can: not what
# the content IS, but where it CAME FROM.
#
# THE RULE.  A channel consumed as an independent authority must have BOUND provenance:
#
#   1. a PRODUCER callable registered under the P9 identity contract (recomputed digest, module,
#      qualname, path — no-override), so the content has a reviewed origin rather than a caller;
#   2. content the framework RECOMPUTES by EXECUTING that producer, never content a caller handed
#      over — the same inversion that makes a witness's read set observable;
#   3. re-verification AT EVERY CONSUMPTION: identity re-resolved, producer re-executed, and the
#      digest compared to the one pinned at registration. Stale content, content substituted after
#      resolution, a wrapper that returns something unbound, and a second producer under the same
#      channel name are all this same check;
#   4. an EXECUTED INVARIANCE EXPERIMENT: every enumerable module channel the producer reads which
#      carries the collection under test is perturbed, and the produced content must NOT move. A
#      producer that recomputes the "authority" from the collection is caught HERE, by evidence,
#      rather than by a rule about how it was written;
#   5. the declared source id may not be the collection's own id (self-reference), and a producer
#      carrying closure cells is refused, because a copy held in a cell can be neither enumerated
#      nor perturbed and so cannot be excluded.
#
# WHAT THIS DOES **NOT** CLOSE, stated so it is not mistaken for closed.  The producer is trusted
# by IDENTITY, exactly as a P9 witness is: an attacker who can land a reviewed diff can write a
# producer that returns a copy of the collection through a channel step 4 cannot enumerate (a file
# it reads, an import). Step 4 catches the module-state copy — the shape FF-04 actually used — and
# steps 1-3 move the rest of the attack from "call a registration API" to "edit reviewed code",
# which is the same trust boundary P9 draws and no weaker. It is NOT a proof that the producer
# does not copy. Enforcing P6 purity on producers would close that too, and is not landed here:
# every producer shape the current batteries need reads a module-level table, so purity would
# refuse the honest cases along with the dishonest ones. That is a STRUCTURAL residual and it is
# recorded rather than papered over.

P6_CHANNEL_PRODUCERS = WitnessRegistry("P6_CHANNEL_PRODUCERS")

_P6_CHANNEL_PROVENANCE: dict = {}       # channel_id -> the binding recorded at registration
P6_PROVENANCE_UNBOUND = "UNBOUND"
P6_PROVENANCE_PRODUCED = "PRODUCED"


def _p6_content_digest(members) -> str:
    """A digest of the CONTENT, computed by the framework from the members themselves."""
    shape = _p6_channel_shape(members)
    if isinstance(shape, dict):
        canon = json.dumps({key: sorted(value) for key, value in sorted(shape.items())},
                           separators=(",", ":"), sort_keys=True)
    else:
        canon = json.dumps(sorted(shape), separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _register_p6_channel_producer(channel_id: str, producer: Callable, *, source_id: str,
                                  rationale: str = "") -> None:
    """Bind a channel's CONTENT to a reviewed producer the framework executes.

    The content is never supplied: `producer(source_id)` is EXECUTED here and what it returns
    becomes the channel. A caller therefore cannot hand over a set at all, which is the shape
    FF-04 relied on.
    """
    if not isinstance(channel_id, str) or not channel_id:
        raise ContractPinError("a P6 channel producer requires a non-empty channel id")
    if not isinstance(source_id, str) or not source_id:
        raise ContractPinError(
            f"the producer for P6 channel {channel_id!r} names no source; a channel whose origin "
            "is unnamed cannot be distinguished from a copy of the collection under test")
    P6_CHANNEL_PRODUCERS.register(channel_id, producer)
    fn, _identity = P6_CHANNEL_PRODUCERS.resolve(channel_id)
    if getattr(fn, "__closure__", None):
        raise ContractPinError(
            f"the producer for P6 channel {channel_id!r} carries closure cells. A copy of the "
            "collection held in a cell is neither enumerable nor perturbable, so an authority "
            "built from one cannot be shown to be an authority; REFUSED")
    members = _call_witness(fn, source_id)
    shape = _p6_channel_shape(members)             # raises on a non-enumerable authority
    existing = _P6_CHANNEL_SOURCES.get(channel_id)
    if existing is not None and _p6_channel_shape(existing) != shape:
        raise ContractPinError(
            f"P6 channel {channel_id!r} already carries different content; binding a producer "
            "whose output disagrees with the registered authority would rebind a declared "
            "authority after review; REFUSED")
    _P6_CHANNEL_SOURCES[channel_id] = (dict(members) if isinstance(members, dict)
                                       else set(members))
    _P6_CHANNEL_PROVENANCE[channel_id] = {
        "mode": P6_PROVENANCE_PRODUCED, "source_id": source_id,
        "content_digest": _p6_content_digest(members),
        "producer": f"{getattr(fn, '__module__', '?')}.{getattr(fn, '__qualname__', '?')}",
        "rationale": rationale}
    if rationale:
        _P6_CHANNEL_RATIONALE[channel_id] = rationale


def _p6_producer_echoes_collection(fn, source_id: str, channel_content, collection, cid: str):
    """The EXECUTED half: does this producer recompute its 'authority' from the collection?

    Every enumerable module container the producer's own code reaches is examined; the ones whose
    member set IS the collection under test are perturbed, and the producer is re-executed. If the
    content moves, the authority tracks the collection and is not independent of it. Returns a
    detail string, or None.
    """
    members = _p2_member_strings(collection)
    if not members:
        return None
    namespace = getattr(fn, "__globals__", None)
    if namespace is None:
        return None
    target = frozenset(members)
    # SLICE1-FI: unmarked and collection-shaped, for the same reason as everywhere else — the
    # channel PRODUCER is attacker-authored code too, and a marker tells it when it is being probed.
    sentinel = _p2_probe_member(members, members | _p2_member_strings(channel_content),
                                cid, source_id, "channel-content")
    perturbed = _p2_perturbed(members, sentinel)
    baseline = _p2_canon(_call_witness(fn, source_id))
    # THE AMBIGUOUS CASE, DECLINED RATHER THAN GUESSED. When the channel's own content HAS THE
    # SAME MEMBERS as the collection, every container the producer reads that "carries the
    # collection" is equally well the producer's own storage of the authority. Perturbing it moves
    # the produced content in BOTH the honest and the dishonest case — an authority that agrees
    # with a correct collection is what a useful authority looks like — so a refusal here would be
    # a detector of CORRECTNESS, not of copying, and would reject the honest positive controls.
    # The experiment declines to run, and the header records the residual: an authority whose
    # members coincide with the collection is not separable from a copy of it by any experiment
    # performed on the producer. Steps 1-3 (reviewed producer, pinned identity, recomputed
    # content) are what carry that case.
    if _p2_member_strings(channel_content) == members:
        return None
    for channel, value in _p2_ambient_globals(fn).items():
        if not isinstance(value, (dict, set, frozenset, list, tuple)):
            continue
        replacement, carries = _p2_substitute(value, target, perturbed)
        if not carries:
            continue
        original = namespace[channel]
        namespace[channel] = replacement
        try:
            moved = _p2_canon(_call_witness(fn, source_id)) != baseline
        finally:
            namespace[channel] = original
        if moved:
            return (f"module state {channel!r} that the producer reads holds a copy of the "
                    "collection under test, and the produced content MOVED in lockstep when that "
                    "copy was perturbed. The 'independent authority' is recomputed FROM the "
                    "collection it is used to certify")
    return None


def _p6_channel_provenance_problem(channel_id: str, collection, cid: str, relation: str):
    """The consumption-side check. None admits the channel; a Problem refuses it.

    Runs at the point of USE, not only at registration, because registration proves what was true
    once and a security property has to be true on the run that produced the verdict.
    """
    binding = _P6_CHANNEL_PROVENANCE.get(channel_id)
    if binding is None:
        return _problem(relation, "CHANNEL_CONTENT_UNPROVENANCED",
                        f"{cid}: P6 channel {channel_id!r} was registered with content a caller "
                        "supplied, and nothing records where that content came from. An "
                        "'independent authority' assembled by copying the collection under test "
                        "is admitted by that route and every downstream experiment then agrees "
                        "the witness reads it — truthfully, because it does. Bind the channel to "
                        "a reviewed producer the framework executes; REFUSED")
    if binding["source_id"] == cid:
        return _problem(relation, "CHANNEL_CONTENT_SELF_REFERENCE",
                        f"{cid}: P6 channel {channel_id!r} declares its source to be the "
                        "collection under test itself; an authority that IS the subject is a "
                        "second copy of the list, not a second authority; REFUSED")
    try:
        fn, _identity = P6_CHANNEL_PRODUCERS.resolve(channel_id)
    except WitnessIdentityError as exc:
        return _problem(relation, "CHANNEL_PRODUCER_IDENTITY_REFUSED",
                        f"{cid}: the producer bound to P6 channel {channel_id!r} no longer "
                        f"verifies: {exc}. A channel whose producer changed between registration "
                        "and use carries content nobody reviewed; REFUSED")
    try:
        recomputed = _call_witness(fn, binding["source_id"])
        digest = _p6_content_digest(recomputed)
    except Exception as exc:
        return _problem(relation, "CHANNEL_CONTENT_UNRECOMPUTABLE",
                        f"{cid}: re-executing the producer of P6 channel {channel_id!r} raised "
                        f"{type(exc).__name__}: {exc}. Content that cannot be recomputed cannot "
                        "be shown to be the content that was reviewed; REFUSED")
    if digest != binding["content_digest"]:
        return _problem(relation, "CHANNEL_CONTENT_DRIFTED",
                        f"{cid}: P6 channel {channel_id!r} does not recompute to the content "
                        "pinned at registration. Stale content, content substituted after "
                        "resolution, and a wrapper returning something other than what it claims "
                        "all arrive here; the witness would be reading an authority no review "
                        "ever saw; REFUSED")
    stored = _P6_CHANNEL_SOURCES.get(channel_id)
    if stored is None or _p6_content_digest(stored) != binding["content_digest"]:
        return _problem(relation, "CHANNEL_CONTENT_SUBSTITUTED",
                        f"{cid}: the content held for P6 channel {channel_id!r} is not what its "
                        "producer produces. The value that would actually be INJECTED has been "
                        "replaced since it was bound; REFUSED")
    echo = _p6_producer_echoes_collection(fn, binding["source_id"], recomputed, collection, cid)
    if echo is not None:
        return _problem(relation, "CHANNEL_CONTENT_ECHOES_COLLECTION", f"{cid}: {echo}; REFUSED")
    return None


def _p6_channel_provenance_problems(channels, collection, cid: str, relation: str) -> list:
    """Every provenance refusal owed by a set of channels about to be injected."""
    out = []
    for channel_id in sorted(channels):
        if channel_id == cid:
            continue          # the collection itself is injected under its own id, not as authority
        problem = _p6_channel_provenance_problem(channel_id, collection, cid, relation)
        if problem is not None:
            out.append(problem)
    return out


# ================================================================================================
# PART H — PART D MUST_DEPEND   (gate 4N-I28BH-B0w-R2-SLICE1-TOTALITY, finding 4 / X-NE-CONST)
# ================================================================================================
# THE DEFECT THIS CLOSES.  Part D has an ambient-channel closure (p2_ne_witness_closure) and, for
# kinds D and E, discrimination probes.  It has no MUST_DEPEND limb anywhere.  So for kinds A, B
# and C — whose witness is called with the spec and returns a set — a CONSTANT witness is
# indistinguishable from an observation:
#
#     def observe(spec): return {"c1", "c2", "c3", "c4", "c5"}
#
# passes the ambient closure (it reaches no ambient state, because it reaches nothing), reconciles
# against the source, and certifies CLEAN forever — including after the real authority has changed
# and the constant has gone stale.  The banked positive controls are exactly this shape, which is
# how the hole survived: the control could not tell an authored answer from an observed one, so it
# scored the defect as correct behaviour.
#
# THE ARGUMENT THAT MAKES THE REFUSAL SOUND, not merely cautious.  A Part D witness that passes the
# ambient closure reaches NO state the framework cannot enumerate — so it did not FETCH its
# authority.  If the framework also INJECTED nothing (no registered channel behind its declared
# source), then its output is a function of its payload alone.  It is therefore an authored
# constant, whatever it is called.  "Cannot prove" is not being careful here; it is a conclusion.
#
# POLARITY, AND WHY IT IS NOT UNIVERSAL.  Kinds D and E already run BOTH polarities and are
# recorded NOT_APPLICABLE against a named, independently-equivalent mechanism rather than skipped:
# kind D requires the verdict to MOVE under the relevant mutation and to be INVARIANT under a
# framework-generated irrelevant one (a constant fails the first leg), and kind E requires the
# witness to refuse an unknown member AND accept a pinned known one (a constant fails one of the
# two by construction).  Claiming those kinds need this limb would be inventing an obligation; not
# saying WHY they do not would be the silent skip this gate exists to remove.

_NE_MUST_DEPEND_KINDS = ("INDEPENDENT_CONSEQUENCE_RECONCILIATION", "INDEPENDENT_SITE_UNIVERSE",
                         "CROSS_SOURCE_REQUIREMENT")

# guarantee kind -> the mechanism that already refuses a constant witness on that kind's own path.
# Each names a check that RUNS; none of them is "this kind is different".
_NE_DEPENDENCY_COVERED = {
    "SEMANTIC_MUTATION_WITNESS": (
        "kind D already carries both polarities: the verdict MUST move under the relevant "
        "mutation (a constant fails that leg outright) and MUST be invariant under a "
        "framework-generated irrelevant one. A constant cannot satisfy the first"),
    "CLOSED_WORLD_UNKNOWN_REFUSAL": (
        "kind E already probes the witness in two contradictory directions — refuse an unknown "
        "member, accept a pinned known one — plus a mandatory negative control. A constant "
        "answers both the same way and is refused as non-discriminating"),
}


def _p2_ne_declared_authority(spec: dict) -> list:
    """The authority a Part D spec claims its witness reads.

    `expected_source` is a MANDATORY key of every non-enumerable spec, so this is never empty for a
    well-formed spec — there is no "declared nothing" case to excuse.
    """
    witness = spec.get("independent_observed_source_or_witness")
    declared = []
    if isinstance(witness, dict) and isinstance(witness.get("reads"), str) and witness["reads"]:
        declared.append(witness["reads"])
    expected = spec.get("expected_source")
    if isinstance(expected, str) and expected and expected not in declared:
        declared.append(expected)
    return declared


def _p2_ne_must_depend(spec: dict, cid: str, call: Callable) -> list:
    """Part D's MUST_DEPEND limb, as problem STRINGS. Returns [] only when the dependency is
    PROVED or the kind is covered by a named equivalent mechanism."""
    if not isinstance(spec, dict):
        return []
    guarantee = spec.get("guarantee_kind")
    if guarantee in _NE_DEPENDENCY_COVERED:
        return []
    if guarantee not in _NE_MUST_DEPEND_KINDS:
        return [f"{cid}: REFUSED — P2_NE_DEPENDENCY_UNCLASSIFIED: guarantee kind "
                f"{guarantee!r} is neither in the set this limb governs nor recorded as covered "
                "by an equivalent mechanism. A kind that nobody has placed on either side of this "
                "question has no MUST_DEPEND obligation by accident rather than by decision, "
                "which is how a new kind ships with a constant witness"]
    declared = _p2_ne_declared_authority(spec)
    if not declared:
        return [f"{cid}: REFUSED — P2_NE_DEPENDENCY_UNDECLARED: the spec names no authority for "
                "its independent witness, so there is nothing to inject, perturb or verify"]
    channels = {}
    for entry in declared:
        for channel in (_P6_CHANNEL_GROUPS.get(entry) or [entry]):
            if channel in _P6_CHANNEL_SOURCES:
                channels[channel] = _P6_CHANNEL_SOURCES[channel]
    if not channels:
        return [f"{cid}: REFUSED — P2_NE_DEPENDENCY_UNPROVABLE: the witness declares it reads "
                f"{declared!r}, but no such P6 channel is registered, so the framework injected "
                "nothing and had nothing to perturb. This witness also passed the ambient-channel "
                "closure, which means it FETCHED nothing either — so its answer is a function of "
                "its payload alone. A CONSTANT, a STALE copy and a genuine reading of the "
                "authority are indistinguishable in that state, and the first two certify a "
                "collection that no longer matches anything. The dependency claim is UNPROVABLE "
                "and is REFUSED rather than skipped. Register the authority as a P6 channel with "
                "bound content provenance so the framework injects it"]
    provenance = _p6_channel_provenance_problems(channels, (), cid, guarantee)
    if provenance:
        return [f"{cid}: REFUSED — P2_NE_CHANNEL_UNPROVENANCED: {problem['detail']}"
                for problem in provenance]
    try:
        baseline = _p2_canon(call(_p2_witness_payload(spec, cid, channels)))
    except Exception as exc:
        return [f"{cid}: REFUSED — P2_NE_WITNESS_UNRUNNABLE: the witness raised "
                f"{type(exc).__name__}: {exc} when the framework injected the authority it "
                "declares it reads. A witness that cannot be run under its declared inputs cannot "
                "be shown to depend on them"]
    # SLICE1-FI: the same unmarked, real-looking schedule the provider path uses. Part D's witness
    # is attacker-authored code the framework executes too, so a perturbation it can RECOGNISE buys
    # it the same escape: honest for the trial, authored for the verdict. The laws are the same —
    # a function of its input, monotone under restriction, and answering to at least one restriction
    # of its declared authority.
    try:
        baseline_members = _p2_member_strings(call(_p2_witness_payload(spec, cid, channels)))
    except Exception:
        baseline_members = set()
    pool = _p2_real_value_pool(spec, (), channels)
    inert = []
    for channel in sorted(channels):
        content = channels[channel]
        probe_member = _p2_probe_member(_p2_member_strings(content), pool, cid, guarantee, channel)
        schedule = _p2_channel_trials(content, baseline_members, probe_member)
        if not schedule:
            return [f"{cid}: REFUSED — P2_NE_DEPENDENCY_UNPROVABLE: the declared authority "
                    f"{channel!r} carries no enumerable member the framework can vary, so no "
                    "perturbation of it exists and the dependency cannot be decided"]
        seen: dict = {}
        moved = False
        for label, _dropped, is_restriction, trial_content in schedule:
            sources = dict(channels)
            sources[channel] = trial_content
            try:
                raw = call(_p2_witness_payload(spec, cid, sources))
                observed = _p2_canon(raw)
            except Exception:
                moved = True        # refusing to run under a restricted authority IS a dependency
                continue
            key = _p2_canon(trial_content)
            if key in seen and seen[key][0] != observed:
                return [f"{cid}: REFUSED — P2_NE_TRIAL_DISCRIMINATING_WITNESS: the witness answered "
                        f"the SAME content for {channel!r} differently at two positions in the "
                        f"trial sequence ({seen[key][1]} then {label}), so its answer is not a "
                        "function of its input. A witness that can tell an experiment from a live "
                        "evaluation can be honest for one and authored for the other"]
            seen.setdefault(key, (observed, label))
            if not is_restriction:
                continue
            if observed != baseline:
                moved = True
            if not _p2_member_strings(raw) <= baseline_members:
                return [f"{cid}: REFUSED — P2_NE_OBSERVATION_NOT_MONOTONE: restricting the declared "
                        f"authority {channel!r} ({label}) made the witness's answer GROW. An "
                        "authority that lists fewer members cannot make a reading of it assert "
                        "more; an answer that is only given for one exact value of the authority "
                        "is an authored answer that recognises its live input"]
        if not moved:
            inert.append(channel)
    if inert:
        return [f"{cid}: REFUSED — P2_NE_DECLARED_READ_INERT: the witness's observation did NOT "
                f"move when its declared authority {inert!r} was perturbed, so it does not consume "
                "the authority it names. A constant, a cached answer and an authority that is "
                "never read are indistinguishable here, and none of the three is an observation "
                "of anything. Part D's whole claim is that an INDEPENDENT authority says this — "
                "an answer that would read the same however that authority changed does not say it"]
    return []


# ============================================================================================
# --- BEGIN SEALED TCB (was source sha256 6b3005905ce3...; Gate 4N-I28BH-B0a-SLICE2-VERIFY-BOUNDARY Option-2 re-cert: static-dispatch renames only, NO control-logic change) ---
# --- SEALED TCB SECTION (was sha256 0ca5d70a688c...; re-certified BYTE-IDENTICAL to signed ba24d397: P1-P9 matrix + adversarial battery + convergence 15/15 + independent diff/red-team PASS) ---
# ============================================================================================
"""cc_core -- THE TRUSTED COMPUTING BASE of the closed-capability framework.

Gate 4N-I28BH-B0w-R2-SLICE1 §20-23.  Scratch only; not part of the repository.

Promoted verbatim-in-substance from cc_prototype.py (sha256 f4877a86...c21f2a,
44 attacks / 0 defeats / barrier 12/12).  The additions over the prototype are:

  * GateSpec / GATE_REGISTRY as ORDERED DECLARED DATA with `requires=` preconditions,
    per-gate `na_bases` vocabularies and per-gate `requirements` rows.
  * GateLedger -- _P2ActivationLedger generalised to all nine gates (blueprint §1.2 P2).
  * GateRefusal -- a refusal that carries the legacy problem dicts, so an adapter can
    RENDER a refusal without ever DECIDING one.
  * PREEMPT / APPEND contribution modes, which express the banked pipeline's
    "return immediately" vs "extend the verdict" semantics as DATA rather than call order.

INVARIANTS (frozen -- see migration-blueprint.md §0):
  I1 one authoritative sink (certify_result)      I5 closed contracts refuse, never degrade
  I2 issuance is internal-only                    I6 closed encoding domain, exact types
  I3 authority is identity, not structure         I7 certificates are claim-bound
  I4 non-transferable + single-use + scoped       I8 applicability fails closed
"""


import gc
import hashlib
import secrets
import threading
from enum import Enum
from types import MappingProxyType

# ---------------------------------------------------------------------------
# Refusal taxonomy.  Every path that is not an explicit success raises one of
# these; there is no "return None and let the caller decide" anywhere.
# ---------------------------------------------------------------------------


class Refused(Exception):
    """Base: fail-closed."""


class ClosedContractRefusal(Refused):
    """Input fell outside the closed contract."""


class CapabilityRefused(Refused):
    """Authority absent, forged, stale, consumed or mis-bound."""


class StateMachineViolation(Refused):
    """An illegal transition was attempted."""


class DeferredSourceRefusal(ClosedContractRefusal):
    """A DEFERRED source could not be selected, identified or loaded.

    Distinct from the base class only so the deferred adapter can render the banked sentence
    (`<cid>: source could not be loaded: ...`) without inspecting the message text.
    """


class GateRefusal(Refused):
    """One or more gates did not prove.

    Carries the legacy problem dicts produced by the gates so that an adapter can
    render them.  The adapter MAY NOT add to, filter or reinterpret this list --
    that would be a second sink (blueprint §3.2 R2).
    """

    def __init__(self, problems, gate_results=None):
        _EXC_INIT(self, f"{len(problems)} problem(s)")
        self.problems = list(problems)
        self.gate_results = dict(gate_results or {})


# ---------------------------------------------------------------------------
# Trusted computing base: the sentinel and the issuer state.
# ---------------------------------------------------------------------------

_ISSUER_SENTINEL = object()


class _IssuerState:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.execution_nonce = secrets.token_hex(16)
        self.capabilities: dict[int, tuple[object, "_CapRecord"]] = {}
        self.certified: dict[int, object] = {}
        # id(view) -> (view, certificate).  The binding that makes an EMPTY problems list
        # authoritative.  It lives HERE, in issuer state, and not in an attribute on the view,
        # for the A18/B24 reason: structure confers no authority.
        self.clean_views: dict[int, tuple[object, object]] = {}

    def rotate_execution(self) -> None:
        with self._lock:
            self.execution_nonce = secrets.token_hex(16)


_ISSUER = _IssuerState()


# ---------------------------------------------------------------------------
# Canonical fingerprinting -- CLOSED ENCODING DOMAIN, EXACT TYPES ONLY (I6).
# CC-FIND-01 remediation: dispatch on `type(x) is T` and read through the BASE
# type's unoverridable accessor.  A subclass is a DIFFERENT type and refuses.
# ---------------------------------------------------------------------------


def _canon(value) -> str:
    t = type(value)
    if value is None:
        return "N:"
    if t is bool:
        return f"B:{int(value)}"
    if t is int:
        return f"I:{_INT_REPR(value)}"
    if t is float:
        return f"F:{_FLOAT_REPR(value)}"
    if t is str:
        return f"S:{_STR_LEN(value)}:{str(value)}"
    if t is bytes:
        return f"Y:{hashlib.sha256(value).hexdigest()}"
    if t is dict:
        items = sorted((_canon(k), _canon(v)) for k, v in dict.items(value))
        return "D:[" + "|".join(f"{k}={v}" for k, v in items) + "]"
    if t is MappingProxyType:
        refs = gc.get_referents(value)
        if len(refs) != 1 or type(refs[0]) is not dict:
            raise ClosedContractRefusal(
                "MappingProxyType must wrap an exact dict; wrapped type is "
                f"{type(refs[0]).__name__ if refs else 'unknown'}"
            )
        return _canon(refs[0])
    if t is list:
        return "L:[" + "|".join(_canon(v) for v in value) + "]"
    if t is tuple:
        return "U:[" + "|".join(_canon(v) for v in value) + "]"
    if t is set:
        return "T:[" + "|".join(sorted(_canon(v) for v in value)) + "]"
    if t is frozenset:
        return "Z:[" + "|".join(sorted(_canon(v) for v in value)) + "]"
    raise ClosedContractRefusal(
        f"type {t.__name__!r} is outside the closed encoding domain "
        "(exact types only; subclasses are excluded because their accessors "
        "are attacker-overridable)"
    )


def fingerprint(value) -> str:
    return hashlib.sha256(_canon(value).encode()).hexdigest()


# ---------------------------------------------------------------------------
# Gate outcomes (§16).  Exactly four, and no fifth.
# ---------------------------------------------------------------------------


class GateOutcome(Enum):
    APPLICABLE_AND_PROVED = "APPLICABLE_AND_PROVED"
    NOT_APPLICABLE_BY_CLOSED_CONTRACT = "NOT_APPLICABLE_BY_CLOSED_CONTRACT"
    CANNOT_PROVE = "CANNOT_PROVE"
    FAILED = "FAILED"


class Contribution(Enum):
    """How a gate's FAILURE contributes to the rendered verdict.

    PREEMPT -- the first FAILED gate in registry order renders alone.  This is the
               banked pipeline's `return [problem]`, expressed as data.
    APPEND  -- the gate always contributes its problems to the rendered verdict,
               even when an earlier gate already failed.  This is the banked
               pipeline's `verdict.extend(...)` tail (P1 activation, P2 undecided):
               a refusal that hides three other refusals is worse evidence.
    """

    PREEMPT = "PREEMPT"
    APPEND = "APPEND"


class GateResult:
    __slots__ = ("gate", "outcome", "rule_id", "detail", "problems", "contribution")

    def __init__(self, gate, outcome, rule_id=None, detail="", problems=(),
                 contribution=None) -> None:
        self.gate = gate
        self.outcome = outcome
        self.rule_id = rule_id
        self.detail = detail
        self.problems = list(problems)
        # A gate whose CONTRIBUTION depends on WHICH refusal it found declares that
        # per outcome.  (P2: a DECIDED "this observation tracks the collection"
        # preempts, because the operand is a proven lie; an UNDECIDED result is
        # weaker and is APPENDED so it cannot mask the sibling controls.)  None
        # means "use the GateSpec's declared default".
        self.contribution = contribution

    def __repr__(self) -> str:
        return f"<GateResult {self.gate} {self.outcome.value} {self.rule_id or ''}>"


class GateSpec:
    """A gate is DECLARED DATA, not a call position."""

    __slots__ = ("gate_id", "fn", "requires", "na_bases", "requirements",
                 "contribution", "render_order", "prose")

    def __init__(self, gate_id, fn, *, requires=(), na_bases=frozenset(),
                 requirements=(), contribution=Contribution.PREEMPT,
                 render_order=0, prose="") -> None:
        self.gate_id = gate_id
        self.fn = fn
        self.requires = tuple(requires)
        self.na_bases = frozenset(na_bases)
        self.requirements = tuple(requirements)
        self.contribution = contribution
        # Where this gate's APPENDED problems sit in the rendered verdict.  Declared
        # data, so the rendered order is a property of the contract and not of the
        # order the gates happened to be written in.
        self.render_order = render_order
        self.prose = prose


# ---------------------------------------------------------------------------
# GateLedger -- _P2ActivationLedger generalised (blueprint §1.2 P2).
#
# The walk-the-tuple property is the whole point: issuance walks the REGISTRY and
# the per-gate `requirements` tuple, never the results, so a gate (or a requirement)
# that recorded NOTHING is a reported silent skip and not an absent obligation.
# ---------------------------------------------------------------------------


class GateLedger:
    __slots__ = ("rows", "outcomes", "_finished")

    def __init__(self) -> None:
        # (gate_id, requirement) -> dict(row)
        self.rows: dict[tuple, dict] = {}
        self.outcomes: dict[str, GateResult] = {}
        self._finished = False

    def record_requirement(self, gate_id, requirement, *, ran, detail="",
                           state="EXECUTED") -> None:
        self.rows[(gate_id, requirement)] = {
            "gate": gate_id,
            "requirement": requirement,
            "ran": bool(ran),
            "state": state,
            "detail": detail,
        }

    def record_outcome(self, result: GateResult) -> None:
        self.outcomes[result.gate] = result

    # -- the anti-hollow observation -----------------------------------------
    def finish_gates(self, registry) -> list:
        """Return a list of OWED PROBLEMS.  Empty means every gate in the registry
        accounted for itself.  Three fail-open shapes, all ported from the banked
        _P2ActivationLedger.finish()."""
        owed = []
        for gate_id, spec in registry.items():
            res = self.outcomes.get(gate_id)
            # (1) SILENT SKIP -- the gate mechanism never ran at all.
            if res is None:
                owed.append({
                    "gate": gate_id,
                    "kind": "GATE_MECHANISM_NOT_RUN",
                    "detail": f"gate {gate_id} recorded no outcome; a gate that did not "
                              "run proves nothing and is CANNOT_PROVE, never an absent "
                              "obligation",
                })
                continue
            # (2) UNREVIEWED BASIS -- NA claimed outside the gate's closed vocabulary.
            if res.outcome is GateOutcome.NOT_APPLICABLE_BY_CLOSED_CONTRACT:
                if res.rule_id not in spec.na_bases:
                    owed.append({
                        "gate": gate_id,
                        "kind": "NOT_APPLICABLE_UNJUSTIFIED",
                        "detail": f"gate {gate_id} claimed NOT_APPLICABLE against basis "
                                  f"{res.rule_id!r}, which is not in its reviewed "
                                  f"vocabulary {sorted(spec.na_bases)}",
                    })
                continue
            if res.outcome is not GateOutcome.APPLICABLE_AND_PROVED:
                continue
            # (3) FORGOTTEN REQUIREMENT ROW -- the gate proved but a declared
            #     requirement of that proof left no record.
            for requirement in spec.requirements:
                row = self.rows.get((gate_id, requirement))
                if row is None:
                    owed.append({
                        "gate": gate_id,
                        "kind": "REQUIREMENT_NOT_RECORDED",
                        "detail": f"gate {gate_id} reported APPLICABLE_AND_PROVED but "
                                  f"requirement {requirement!r} recorded nothing; a "
                                  "silent requirement is a silent skip",
                    })
                elif not row["ran"] and row["state"] == "EXECUTED":
                    owed.append({
                        "gate": gate_id,
                        "kind": "REQUIREMENT_NOT_RUN",
                        "detail": f"gate {gate_id} requirement {requirement!r} is "
                                  "recorded as EXECUTED but ran=False",
                    })
        self._finished = True
        return owed

    # -- the §21 double-proof observation ------------------------------------
    def activation_report(self, registry) -> dict:
        """Which gates were APPLICABLE and PROVED on this evaluation.  This is the
        evidence that makes `green_but_inactive = 0` an OBSERVATION."""
        proved, na, cannot, failed, missing = [], [], [], [], []
        for gate_id in registry:
            res = self.outcomes.get(gate_id)
            if res is None:
                missing.append(gate_id)
            elif res.outcome is GateOutcome.APPLICABLE_AND_PROVED:
                proved.append(gate_id)
            elif res.outcome is GateOutcome.NOT_APPLICABLE_BY_CLOSED_CONTRACT:
                na.append((gate_id, res.rule_id))
            elif res.outcome is GateOutcome.CANNOT_PROVE:
                cannot.append((gate_id, res.rule_id or res.detail))
            else:
                failed.append(gate_id)
        return {
            "proved": tuple(proved),
            "not_applicable": tuple(na),
            "cannot_prove": tuple(cannot),
            "failed": tuple(failed),
            "no_outcome": tuple(missing),
            "requirement_rows": tuple(sorted(self.rows)),
        }


# ---------------------------------------------------------------------------
# Steering envelope (§11): a closed key domain with mediated reads.
#
# Replaces the banked _SteeringWatchedPayload (a dict SUBCLASS with overridden
# accessors -- exactly the CC-FIND-01 attack shape, and refused by I6).  The
# envelope mediates reads WITHOUT pretending to be a dict.
# ---------------------------------------------------------------------------


class SteeringEnvelope:
    __slots__ = ("_data", "_reads")

    def __init__(self, _key, normalized: dict) -> None:
        if _key is not _ISSUER_SENTINEL:
            raise CapabilityRefused("steering envelopes are issuer-constructed only")
        if type(normalized) is not dict:
            raise ClosedContractRefusal(
                f"steering payload must be an exact dict, got {type(normalized).__name__}")
        self._data = dict(normalized)
        self._reads: set[str] = set()

    def read(self, key: str, declared_keys):
        if key not in declared_keys:
            raise ClosedContractRefusal(
                f"producer read undeclared steering key {key!r}: steering domain is closed")
        self._reads.add(key)
        return self._data.get(key)

    def reads(self) -> frozenset:
        return frozenset(self._reads)

    def digest(self) -> str:
        return fingerprint(self._data)


# ---------------------------------------------------------------------------
# DEFERRED SOURCE BINDING (§17-D) -- the object that does not exist yet.
#
# Some routes do not RECEIVE their subject; they NAME it and a loader produces it.
# Binding such a subject at S1->S2 is impossible, and loading it before the gates
# have adjudicated the NAME would mean the gate that exists to catch a steered
# source selection would run after the steering had already taken effect.
#
# The split below is the whole design:
#   * the SELECTION (which source) is STEERING.  It is fingerprinted BEFORE any
#     gate runs, adjudicated by the pre-gates, and pinned into the capability, so
#     a different selection is a different claim rather than the same claim about
#     different content.
#   * the CONTENT (what the loader returned) is CHANNEL CONTENT.  The TCB -- not
#     the caller -- invokes the loader, fingerprints the result, hands it out only
#     through consume(), and RE-DERIVES it at the sink from the same loader over
#     the same selection.  A source loaded and then swapped diverges exactly as a
#     trusted-producer channel does.
# ---------------------------------------------------------------------------


def _loader_identity(fn) -> tuple:
    """Identity of a deferred source loader, RECOMPUTED from the live callable.

    Never supplied and never accepted from data.  A loader whose identity cannot be recomputed is
    refused rather than given a default: the sink re-derives the source through this callable, so a
    loader it cannot identify is a source whose provenance it cannot check.
    """
    if not callable(fn):
        raise ClosedContractRefusal(
            f"source loader must be callable, got {type(fn).__name__}")
    code = getattr(fn, "__code__", None)
    if code is None or type(code).__name__ != "code":
        raise ClosedContractRefusal(
            "source loader has no Python code object; a builtin, C function or callable object "
            "cannot be identity-bound and is refused rather than trusted")
    digest = hashlib.sha256()
    digest.update(code.co_code)
    digest.update(repr(tuple(code.co_names)).encode())
    digest.update(repr(tuple(code.co_varnames)).encode())
    digest.update(repr(tuple(code.co_freevars)).encode())
    # consts may hold nested code objects, which have no stable repr; record their TYPES plus the
    # literal consts, which is what a swapped body actually changes.
    digest.update(repr(tuple(type(c).__name__ for c in code.co_consts)).encode())
    digest.update(repr(tuple(c for c in code.co_consts
                             if type(c) in (str, int, float, bool, bytes, type(None)))).encode())
    return (getattr(fn, "__module__", None), getattr(fn, "__qualname__", None),
            code.co_argcount, digest.hexdigest())


class DeferredSource:
    __slots__ = ("selection", "selection_fp", "loader", "loader_id",
                 "content", "content_fp", "consumed_fp", "loaded")

    def __init__(self, _key, selection, loader) -> None:
        if _key is not _ISSUER_SENTINEL:
            raise CapabilityRefused("deferred sources are issuer-constructed only")
        self.selection = selection
        self.selection_fp = fingerprint(selection)      # closed encoding domain applies (I6)
        self.loader = loader
        self.loader_id = _loader_identity(loader)
        self.content = None
        self.content_fp = None
        self.consumed_fp = None
        self.loaded = False

    # -- the loader runs HERE, inside the TCB, and exactly once -------------
    def load(self, _key):
        if _key is not _ISSUER_SENTINEL:
            raise CapabilityRefused("a deferred source is loaded by the TCB only")
        if self.loaded:
            raise StateMachineViolation("a deferred source is loaded exactly once")
        content = self._invoke()
        self.content = content
        self.content_fp = fingerprint(content)          # TCB-computed; never supplied
        self.loaded = True
        return content

    def _invoke(self):
        if _loader_identity(self.loader) != self.loader_id:
            raise DeferredSourceRefusal(
                "source loader identity changed after it was bound")
        try:
            return self.loader(self.selection)
        except Refused:
            raise
        except Exception as exc:
            raise DeferredSourceRefusal(
                f"source could not be loaded: {type(exc).__name__}: {exc}") from None

    def rederive_fp(self) -> str:
        """Sink-side content provenance: the SAME loader over the SAME selection, again."""
        return fingerprint(self._invoke())

    def source_consume(self):
        """The only way a gate may obtain the loaded source."""
        if not self.loaded:
            raise StateMachineViolation("deferred source consumed before it was loaded")
        self.consumed_fp = fingerprint(self.content)
        return self.content

    def identity(self) -> tuple:
        return (self.selection_fp, self.loader_id)


# ---------------------------------------------------------------------------
# Channel binding (§12).
# ---------------------------------------------------------------------------


class ChannelBinding:
    __slots__ = ("name", "producer_id", "producer_fp", "transform_chain",
                 "consumed_fp", "_content")

    def __init__(self, _key, name, producer_id, content, transform_chain) -> None:
        if _key is not _ISSUER_SENTINEL:
            raise CapabilityRefused("channel bindings are issuer-constructed only")
        self.name = name
        self.producer_id = producer_id
        self._content = content
        self.producer_fp = fingerprint(content)
        self.transform_chain = tuple(transform_chain)
        self.consumed_fp: str | None = None

    def consume(self):
        """The only way a gate may obtain channel content."""
        self.consumed_fp = fingerprint(self._content)
        return self._content

    def channel_identity(self) -> tuple:
        return (self.name, self.producer_id, self.transform_chain)


# ---------------------------------------------------------------------------
# State machine (§17)
# ---------------------------------------------------------------------------


class State(Enum):
    RAW_REQUEST = 0
    CONTRACT_VALIDATED = 1
    OBJECT_BOUND = 2
    CONTENT_BOUND = 3
    REQUIRED_GATES_PROVED = 4
    CAPABILITY_ISSUED = 5
    LOAD_BEARING_OPERATION = 6
    CERTIFIED_RESULT = 7
    # S1D.  Reached only by a DEFERRED-BINDING route, and only from CONTRACT_VALIDATED.
    # Numbered 8 because it is an INSERTED stage, not a renumbering: every existing state
    # keeps its value, so nothing that referred to a state by value now means another one.
    SELECTION_GATED = 8


_LEGAL_EDGES = frozenset({
    (State.RAW_REQUEST, State.CONTRACT_VALIDATED),
    (State.CONTRACT_VALIDATED, State.OBJECT_BOUND),
    (State.OBJECT_BOUND, State.CONTENT_BOUND),
    (State.CONTENT_BOUND, State.REQUIRED_GATES_PROVED),
    (State.REQUIRED_GATES_PROVED, State.CAPABILITY_ISSUED),
    (State.CAPABILITY_ISSUED, State.LOAD_BEARING_OPERATION),
    (State.LOAD_BEARING_OPERATION, State.CERTIFIED_RESULT),
    # --- the two DEFERRED-BINDING edges (§17-D) --------------------------------
    (State.CONTRACT_VALIDATED, State.SELECTION_GATED),
    (State.SELECTION_GATED, State.OBJECT_BOUND),
})

# The two edges above are MUTUALLY EXCLUSIVE with the direct S1->S2 edge, and the exclusion is
# enforced in `advance` rather than merely documented: a deferred transaction may not bind its
# object from the request, and a non-deferred transaction may not enter the selection stage.
# There is therefore exactly ONE path from CONTRACT_VALIDATED to OBJECT_BOUND for any given
# transaction, decided by the closed ROUTE set and not by anything in the caller's payload.
_ROUTE_EXCLUSIVE_EDGES = {
    (State.CONTRACT_VALIDATED, State.OBJECT_BOUND): False,      # requires deferred == False
    (State.CONTRACT_VALIDATED, State.SELECTION_GATED): True,    # requires deferred == True
}

# Populated by cc_contract from the closed route enum.  Deferral is a property of the ROUTE, held
# in the TCB; it is never read out of a request payload.
DEFERRED_ROUTES: frozenset = frozenset()

# The two declared gate PHASES.  A phase is an edge, so "run some gates" can never advance a
# transaction along an edge nobody declared.
GATE_PHASE_SELECTION = (State.CONTRACT_VALIDATED, State.SELECTION_GATED)
GATE_PHASE_MAIN = (State.CONTENT_BOUND, State.REQUIRED_GATES_PROVED)
_GATE_PHASES = frozenset({GATE_PHASE_SELECTION, GATE_PHASE_MAIN})


class _Transaction:
    """Never handed to a caller."""

    __slots__ = ("state", "request", "subject", "subject_fp", "relation", "steering",
                 "channels", "gate_results", "probe", "ledger", "spec", "cid",
                 "collection", "binding", "route", "observed", "scratch",
                 "deferred", "source")

    def __init__(self, probe: bool) -> None:
        self.state = State.RAW_REQUEST
        self.probe = probe
        # DEFERRED BINDING.  Default FALSE: a transaction is immediate-binding unless the TCB's
        # own route set says otherwise, so a route nobody classified cannot acquire the deferred
        # stage by omission.
        self.deferred = False
        self.source = None
        self.request = None
        self.subject = None
        self.subject_fp = None
        self.relation = None
        self.steering = None
        self.channels = {}
        self.gate_results = {}
        self.ledger = GateLedger()
        # framework-facing operands, threaded through the gates
        self.spec = None
        self.cid = None
        self.collection = None
        self.binding = None
        self.route = None
        self.observed = None
        # gate-private scratch.  NEVER a caller-reachable object: this is where
        # framework state such as P7's witness-vetting marker lives, instead of
        # being parked on the caller's spec dict (blueprint §1.2 P7).
        self.scratch = {}

    def advance(self, expected: State, to: State) -> None:
        if self.state is not expected:
            raise StateMachineViolation(
                f"expected {expected.name}, transaction is {self.state.name}")
        if (expected, to) not in _LEGAL_EDGES:
            raise StateMachineViolation(f"illegal edge {expected.name}->{to.name}")
        required = _ROUTE_EXCLUSIVE_EDGES.get((expected, to))
        if required is not None and bool(self.deferred) is not required:
            raise StateMachineViolation(
                f"edge {expected.name}->{to.name} requires deferred={required}; this transaction "
                f"is deferred={bool(self.deferred)}.  A deferred route may not bind its object "
                "from the request, and an immediate route may not enter the selection stage")
        self.state = to


# ---------------------------------------------------------------------------
# The three DEFERRED-BINDING transition functions (§17-D).  Module-private in
# effect: each takes the transaction, which no caller ever holds.
# ---------------------------------------------------------------------------


def declare_route(txn: _Transaction, route) -> None:
    """S0.  The ROUTE decides whether object binding is immediate or deferred.

    Called before contract validation completes, so `deferred` is fixed before any transition is
    attempted and cannot be flipped by a later gate.
    """
    if txn.state is not State.RAW_REQUEST:
        raise StateMachineViolation(
            "the route is declared at RAW_REQUEST, before the contract is validated")
    txn.route = route
    txn.deferred = route in DEFERRED_ROUTES


def declare_deferred_source(txn: _Transaction, selection, loader) -> DeferredSource:
    """S1.  DECLARE the source -- name it, fingerprint the selection, recompute the loader's
    identity.  Nothing is loaded here, which is the point: the pre-gates adjudicate the SELECTION
    and the loader runs only once every one of them has proved."""
    if not txn.deferred:
        raise StateMachineViolation(
            "only a deferred-binding route may declare a deferred source")
    if txn.state is not State.CONTRACT_VALIDATED:
        raise StateMachineViolation(
            f"a deferred source is declared at CONTRACT_VALIDATED, transaction is {txn.state.name}")
    if txn.source is not None:
        raise StateMachineViolation("a transaction declares exactly one deferred source")
    txn.source = DeferredSource(_ISSUER_SENTINEL, selection, loader)
    return txn.source


def bind_deferred_object(txn: _Transaction) -> None:
    """S1D -> S2.  The loader runs HERE, inside the TCB, after every pre-gate proved.

    A pre-gate that did not prove raises out of `run_gates` before this function is reached, so
    "the steering gate runs before the loader" is a STRUCTURAL property of the state machine and
    not an ordering convention in an adapter body.
    """
    if txn.state is not State.SELECTION_GATED:
        raise StateMachineViolation(
            f"deferred object binding requires SELECTION_GATED, transaction is {txn.state.name}")
    if txn.source is None:
        raise StateMachineViolation(
            "no deferred source was declared; the selection stage proved nothing to bind")
    content = txn.source.load(_ISSUER_SENTINEL)
    txn.subject = content
    txn.subject_fp = txn.source.content_fp
    txn.collection = content
    txn.advance(State.SELECTION_GATED, State.OBJECT_BOUND)


# ---------------------------------------------------------------------------
# The capability: an OPAQUE handle.  All authority lives in issuer state.
# ---------------------------------------------------------------------------


class _CapRecord:
    __slots__ = ("handle", "nonce", "subject", "subject_fp", "relation",
                 "steering_digest", "channel_ids", "channel_fps", "gate_results",
                 "consumed", "probe", "activation", "deferred", "source_id", "source_fp")


class Capability:
    __slots__ = ("_handle",)

    def __init__(self, _key, handle) -> None:
        if _key is not _ISSUER_SENTINEL:
            raise CapabilityRefused("capabilities are issuer-constructed only")
        self._handle = handle

    def __repr__(self) -> str:  # leaks nothing
        return "<Capability opaque>"

    def __reduce__(self):
        raise CapabilityRefused("capability is not serialisable")

    def __copy__(self):
        raise CapabilityRefused("capability is not copyable")

    def __deepcopy__(self, memo):
        raise CapabilityRefused("capability is not copyable")


class Verdict(Enum):
    CERTIFIED = "CERTIFIED"
    PROBE_RESULT = "PROBE_RESULT"


class CertifiedResult:
    __slots__ = ("verdict", "subject_fp", "relation", "gates", "activation", "source_id")

    def __init__(self, _key, verdict, subject_fp, relation, gates, activation,
                 source_id=None) -> None:
        if _key is not _ISSUER_SENTINEL:
            raise CapabilityRefused("results are sink-constructed only")
        self.verdict = verdict
        self.subject_fp = subject_fp
        self.relation = relation
        self.gates = gates
        self.activation = activation
        # DEFERRED CLAIMS.  None on an immediate-binding certificate.  On a deferred one it is
        # (selection fingerprint, recomputed loader identity) -- the SELECTION the caller named,
        # which is the only thing a deferred caller can present as its claim.
        self.source_id = source_id

    def __repr__(self) -> str:
        return f"<CertifiedResult {self.verdict.value} {self.subject_fp[:12]}>"


def _is_registered_certificate(obj) -> bool:
    """Necessary, NOT sufficient.  Never the load-bearing predicate."""
    if type(obj) is not CertifiedResult:
        return False
    if _ISSUER.certified.get(id(obj)) is not obj:
        return False
    return obj.verdict is Verdict.CERTIFIED


def certified_for(result, subject, relation) -> bool:
    """The ONLY predicate that declares a result load-bearing FOR A CLAIM (I7)."""
    if not _is_registered_certificate(result):
        return False
    try:
        subject_fp = fingerprint(subject)
    except Refused:
        return False
    if subject_fp != result.subject_fp:
        return False
    kind = getattr(relation, "value", relation)
    return kind == result.relation


def certified_for_selection(result, selection, loader, relation) -> bool:
    """I7 under DEFERRED binding: the ONLY predicate that declares a deferred result load-bearing
    FOR A CLAIM.

    A deferred caller never holds the subject -- it names a SELECTION and supplies a LOADER, and
    the TCB produced the object.  So "does this certificate cover MY claim?" is answered against
    the selection fingerprint and the RECOMPUTED loader identity.  Asking only
    `_is_registered_certificate` here would be CC-FIND-02 in its deferred form: a genuine
    certificate about source A accepted as proof about source B.
    """
    if not _is_registered_certificate(result):
        return False
    if result.source_id is None:
        return False                      # an immediate-binding certificate proves no selection
    try:
        claim = (fingerprint(selection), _loader_identity(loader))
    except Refused:
        return False
    if claim != result.source_id:
        return False
    kind = getattr(relation, "value", relation)
    return kind == result.relation


# ---------------------------------------------------------------------------
# AN AUTHORITATIVE COMPLETENESS ASSERTION IS A CERTIFICATE, NEVER A BARE `[]`
# (reviewer finding B19; §34 convergence under the §37 standard)
#
# THE GAP.  Every consumer-facing entry point returns a PROBLEMS LIST, and an empty one meant
# "this collection is complete".  An empty list is a value ANY function can produce out of a
# builtin literal.  A public helper authored tomorrow --
#
#         def some_new_check(spec, collection, cid="x"):
#             return []
#
# -- therefore asserted completeness with no capability, no gate transit and no sink call, and
# NOTHING in the closed model could refuse it, because there was nothing to refuse: the
# assertion was never made out of framework authority at all.  It was the one form that could
# not be made to fail closed by WITHHOLDING authority, because it never asked for any.
#
# WHY NOT A GUARD.  Enumerating the ways an empty list can be authored -- an import hook, a
# module scan, a naming convention -- is the enumerate-and-govern model this whole redesign
# replaced, and it fails the §37 standard by construction: it would have to NAME the form.
#
# THE FIX.  REMOVE THE AUTHORITY FROM THE EMPTY LIST.  The authoritative signal becomes a
# certificate:
#
#     is_complete(result, subject, relation)      THE authority predicate.  True only for a
#                                                 sink-minted certificate bound to THIS subject
#                                                 under THIS relation.
#
# and an empty problems list becomes a NON-AUTHORITATIVE CONVENIENCE VIEW.  On the clean path
# the entry points still return an empty list -- still `== []`, still falsy, still iterable and
# mergeable, so all 283 legacy consumer sites are untouched -- but what makes THAT list
# load-bearing is an entry in issuer state binding it, by identity, to the certificate actually
# minted for that evaluation.
#
# WHY THIS CONVERGES.  A future unrecognised form now fails for the SAME reason every other
# novel form fails: it cannot acquire authority.  The sink is the only minter, the sink is
# sentinel-gated, and the authority predicate reads the issuer registry.  No rule names the new
# helper; the new helper simply has nothing to present.
#
# WHY AN ORDINARY LIST AND NOT A MARKER TYPE.  The first cut of this returned a `list` SUBCLASS.
# It worked, and it was WORSE: a subclass is a heap type, so `gc.get_referents()` on the value
# the caller is holding yields the class, and from the class the methods, and from a method its
# `__globals__` -- the framework module dict, `_ISSUER` and `_ISSUER_SENTINEL` included.  Red-team
# arm A25 (sentinel reachability from caller-held objects) went from UNREACHABLE to reachable in
# five hops, purely because the verdict changed type.  Authority was never in the type -- it is
# the registry entry -- so the carrier is the plainest object available.
#
# WHAT THIS IS NOT.  `_mint_clean_view` is not a second sink: it refuses anything that is not
# ALREADY a registered certificate out of `certify_result`, so it can only bind authority that
# the one sink minted, and can never create any.
# ---------------------------------------------------------------------------


def _mint_clean_view(result) -> list:
    """BIND an authoritative clean verdict to the certificate the sink already minted.

    Returns an ordinary empty list whose IDENTITY the issuer has recorded.  The registry holds a
    strong reference to it, which is also what makes identity safe to key on: the object cannot
    be collected while it is registered, so its `id` can never be recycled into someone else's
    authority.

    NOT a second authority sink -- it refuses unless `result` is already a REGISTERED CERTIFIED
    result, so `certify_result` remains the only place authority comes into existence.
    """
    if not _is_registered_certificate(result):
        raise CapabilityRefused(
            "a clean view can only be bound to a registered certificate")
    view: list = []
    with _ISSUER._lock:
        _ISSUER.clean_views[id(view)] = (view, result)
    return view


def _clean_view_certificate(obj):
    """The certificate this clean verdict is bound to, or None.  NECESSARY, NOT SUFFICIENT.

    Four independent conditions, none of them satisfiable by shape:
      * EXACT type -- `list`, read through the base type's own accessor (the CC-FIND-01 rule:
        a subclass is a different type and could lie about its length);
      * the issuer registry maps this object's IDENTITY to THIS object -- not to an equal one,
        and structure confers no authority (the A18/B24 rule);
      * it is still EMPTY -- anything appended to it, including the `_sealed_strings` no-transit
        refusal, withdraws the clean claim;
      * the bound certificate is itself still a registered CERTIFIED result.
    """
    if type(obj) is not list:
        return None
    entry = _ISSUER.clean_views.get(id(obj))
    if entry is None:
        return None
    view, result = entry
    if view is not obj:
        return None
    if _LIST_LEN(obj) != 0:
        return None
    if not _is_registered_certificate(result):
        return None
    return result


def _is_authoritative_clean(obj) -> bool:
    """Did this verdict come out of the sink at all?  NECESSARY, NOT SUFFICIENT, and PRIVATE.

    A consumer must use `is_complete`/`is_complete_for_selection`, which additionally bind the
    certificate to the CLAIM; treating this predicate as proof would be CC-FIND-02 in a new
    dress (a genuine certificate about A accepted as proof about B).  It exists for AUDITS and
    for adapters FORWARDING a verdict, which ask only the weaker question "was any authority
    produced here at all".
    """
    return _clean_view_certificate(obj) is not None


def is_complete(result, subject, relation) -> bool:
    """THE authority predicate for "this collection is COMPLETE/clean" (I7).

    True ONLY for authority the sink minted for THIS subject under THIS relation.  It accepts
    either the certificate itself or the clean verdict an entry point returned.

    False for: an empty list literal, any list the sink did not hand out, an unregistered
    look-alike, a certificate for another subject, a certificate under another relation, and a
    PROBE_RESULT.

    A public function authored tomorrow that returns `[]` therefore asserts NOTHING: it never
    held a capability, so no certificate exists to bind, so this reads False -- and it fails
    closed without any rule naming it.
    """
    cert = _clean_view_certificate(result)
    if cert is None:
        cert = result
    return certified_for(cert, subject, relation)


def is_complete_for_selection(result, selection, loader, relation) -> bool:
    """`is_complete` under DEFERRED binding.

    A deferred caller never holds the subject -- it named a SELECTION and supplied a LOADER --
    so the claim is checked against those, exactly as in `certified_for_selection`.
    """
    cert = _clean_view_certificate(result)
    if cert is None:
        cert = result
    return certified_for_selection(cert, selection, loader, relation)
# ---------------------------------------------------------------------------
# S4 -> S5  issue_capability (ISSUER ONLY -- reachable only from run_evaluation)
# ---------------------------------------------------------------------------

_TXN_BY_CAP: dict[int, _Transaction] = {}


def _issue_capability(txn: _Transaction, registry) -> Capability:
    if txn.state is not State.REQUIRED_GATES_PROVED:
        raise StateMachineViolation("issuance requires REQUIRED_GATES_PROVED")

    # The ledger's finish() IS the issuance precondition (blueprint §1.2 P2).
    owed = txn.ledger.finish_gates(registry)
    if owed:
        raise CapabilityRefused(
            "gate ledger owes: " + "; ".join(f"{o['gate']}:{o['kind']}" for o in owed))

    handle = secrets.token_hex(32)
    rec = _CapRecord()
    rec.handle = handle
    rec.nonce = _ISSUER.execution_nonce
    rec.subject = txn.subject
    rec.subject_fp = txn.subject_fp
    rec.relation = txn.relation
    rec.steering_digest = txn.steering.digest()
    rec.channel_ids = tuple(sorted(b.channel_identity() for b in txn.channels.values()))
    rec.channel_fps = tuple(sorted((b.name, b.producer_fp) for b in txn.channels.values()))
    rec.gate_results = tuple(sorted(
        (n, r.outcome.value) for n, r in txn.gate_results.items()))
    rec.consumed = False
    rec.probe = txn.probe
    rec.activation = txn.ledger.activation_report(registry)

    # --- DEFERRED BINDING: the selection and the loaded content are both pinned -------------
    rec.deferred = bool(txn.deferred)
    if rec.deferred:
        if txn.source is None or not txn.source.loaded:
            raise CapabilityRefused(
                "deferred transaction reached issuance with no loaded source; a capability over "
                "an unbound subject would attest nothing")
        rec.source_id = txn.source.identity()
        rec.source_fp = txn.source.content_fp
        if rec.source_fp != txn.subject_fp:
            raise CapabilityRefused(
                "the bound subject is not the loaded source (deferred binding diverged)")
    else:
        if txn.source is not None:
            raise CapabilityRefused(
                "an immediate-binding transaction carries a deferred source; the two binding "
                "models are exclusive")
        rec.source_id = None
        rec.source_fp = None

    cap = Capability(_ISSUER_SENTINEL, handle)
    with _ISSUER._lock:
        _ISSUER.capabilities[id(cap)] = (cap, rec)
    txn.advance(State.REQUIRED_GATES_PROVED, State.CAPABILITY_ISSUED)
    _TXN_BY_CAP[id(cap)] = txn
    return cap


# ---------------------------------------------------------------------------
# S3 -> S4  run_gates.  Iterates the REGISTRY, honours `requires=`, and never
# skips silently.  A masked gate still records its outcome, so a wrong registry
# order can only mis-attribute -- it can never lose a refusal (blueprint §1.1).
# ---------------------------------------------------------------------------


def run_gates(txn: _Transaction, registry, phase=GATE_PHASE_MAIN) -> None:
    # A PHASE is an edge drawn from the declared set, so a caller cannot invent a gate run that
    # advances the transaction along an edge nobody reviewed.
    if phase not in _GATE_PHASES:
        raise StateMachineViolation(f"unknown gate phase {phase!r}")
    # I5: the REGISTRY itself is inside the closed contract.  A registry entry that
    # is not a GateSpec carries no declared preconditions, no reviewed NA vocabulary
    # and no requirement rows, so nothing about it is adjudicable -- it refuses
    # rather than being coerced into a default that would silently grant it the
    # empty vocabulary.  (Phase-1 barrier finding: the prototype's registry was
    # gate_id -> fn, so a bare callable reached `spec.requires` and crashed with an
    # AttributeError -- a crash is not a refusal.)
    for gate_id, spec in registry.items():
        if not isinstance(spec, GateSpec):
            raise ClosedContractRefusal(
                f"gate registry entry {gate_id!r} is a {type(spec).__name__}, not a "
                "GateSpec; a gate with no declared preconditions, NA vocabulary or "
                "requirement rows is not adjudicable")

    for gate_id, spec in registry.items():
        unmet = [r for r in spec.requires
                 if (txn.gate_results.get(r) is None
                     or txn.gate_results[r].outcome not in (
                         GateOutcome.APPLICABLE_AND_PROVED,
                         GateOutcome.NOT_APPLICABLE_BY_CLOSED_CONTRACT))]
        if unmet:
            # The dependent gate executes NOTHING, but it still records.
            res = GateResult(gate_id, GateOutcome.CANNOT_PROVE,
                             rule_id="PRECONDITION_NOT_PROVED",
                             detail=f"preconditions not proved: {unmet}")
        else:
            try:
                res = spec.fn(txn)
            except Refused:
                raise
            except Exception as exc:  # a crashing gate proves nothing
                res = GateResult(gate_id, GateOutcome.CANNOT_PROVE,
                                 rule_id="GATE_RAISED", detail=repr(exc))
            if not isinstance(res, GateResult) or res.outcome not in GateOutcome:
                res = GateResult(gate_id, GateOutcome.CANNOT_PROVE,
                                 rule_id="MALFORMED_GATE_RESULT",
                                 detail="gate returned a malformed result")
        txn.gate_results[gate_id] = res
        txn.ledger.record_outcome(res)

    # --- ISSUANCE PRECONDITION: walk the REGISTRY, not the results -----------
    preempt_problems, append_problems, blocked = [], [], []
    for gate_id, spec in registry.items():
        res = txn.gate_results.get(gate_id)
        if res is None:
            blocked.append(gate_id)
            continue
        if res.outcome is GateOutcome.APPLICABLE_AND_PROVED:
            continue
        if res.outcome is GateOutcome.NOT_APPLICABLE_BY_CLOSED_CONTRACT:
            if res.rule_id in spec.na_bases:
                continue
            blocked.append(gate_id)
            append_problems.extend(res.problems)
            continue
        # CANNOT_PROVE or FAILED -- both block issuance.
        blocked.append(gate_id)
        contribution = res.contribution or spec.contribution
        if contribution is Contribution.PREEMPT:
            if not preempt_problems:
                preempt_problems.extend(res.problems)
        else:
            append_problems.append((spec.render_order, res.problems))

    if blocked:
        rendered = list(preempt_problems)
        for _, problems in sorted(append_problems, key=lambda t: t[0]):
            rendered.extend(problems)
        if not rendered:
            # R4: a refusal that renders to [] is a fail-open.  The TCB, not the
            # adapter, guarantees totality.
            rendered = [{
                "relation": str(txn.relation),
                "kind": "GATE_NOT_PROVED",
                "detail": f"gates did not prove: {blocked}; no capability issued",
                "fail_closed": True,
            }]
        raise GateRefusal(rendered, txn.gate_results)

    txn.advance(*phase)


# ---------------------------------------------------------------------------
# APPEND gates that PROVED may still contribute non-blocking problems (the
# banked `verdict.extend(independence)` tail for UNDECIDED kinds).  These do NOT
# block issuance; they ride along on the rendered verdict.  Kept in the TCB so
# an adapter cannot invent one.
# ---------------------------------------------------------------------------


def ridealong_problems(txn: _Transaction, registry) -> list:
    out = []
    for gate_id, spec in registry.items():
        if spec.contribution is not Contribution.APPEND:
            continue
        res = txn.gate_results.get(gate_id)
        if res is None:
            continue
        if res.outcome is GateOutcome.APPLICABLE_AND_PROVED and res.problems:
            out.extend(res.problems)
    return out


# ---------------------------------------------------------------------------
# S5 -> S6 -> S7  THE SINGLE AUTHORITATIVE SINK (§5, I1)
# ---------------------------------------------------------------------------


_SINK_LOCAL = threading.local()


def certify_result(capability, subject) -> CertifiedResult:
    if not isinstance(capability, Capability):
        raise CapabilityRefused("not a capability")

    # RE-ENTRANCY.  The deferred re-verification below RE-INVOKES a caller-supplied loader inside
    # the sink.  A loader that called back into the sink would be running certification inside
    # certification, and _ISSUER._lock is re-entrant so it would not stop it.  Refuse instead.
    if getattr(_SINK_LOCAL, "active", False):
        raise CapabilityRefused(
            "re-entrant certification: the sink was called from inside the sink")
    _SINK_LOCAL.active = True
    try:
        return _certify_result_locked(capability, subject)
    finally:
        _SINK_LOCAL.active = False


def _certify_result_locked(capability, subject) -> CertifiedResult:
    with _ISSUER._lock:
        entry = _ISSUER.capabilities.get(id(capability))
        if entry is None:
            raise CapabilityRefused(
                "capability is not in the issuer registry (structure confers no authority)")
        stored_cap, rec = entry
        if stored_cap is not capability:
            raise CapabilityRefused("capability identity mismatch")
        if object.__getattribute__(capability, "_handle") != rec.handle:
            raise CapabilityRefused("capability handle mismatch")
        if rec.nonce != _ISSUER.execution_nonce:
            raise CapabilityRefused("capability is stale (execution nonce rotated)")
        if rec.consumed:
            raise CapabilityRefused("capability already consumed (replay)")

        txn = _TXN_BY_CAP.get(id(capability))
        if txn is None:
            raise CapabilityRefused("no transaction bound to capability")

        # --- S6 re-bind: everything re-verified against what was PRESENTED ---
        if subject is not rec.subject:
            raise CapabilityRefused(
                "subject identity differs from the bound object (non-transferable)")
        if fingerprint(subject) != rec.subject_fp:
            raise CapabilityRefused("subject content changed after proof (post-validation swap)")
        if txn.steering.digest() != rec.steering_digest:
            raise CapabilityRefused("steering state changed after proof")
        live_ids = tuple(sorted(b.channel_identity() for b in txn.channels.values()))
        if live_ids != rec.channel_ids:
            raise CapabilityRefused("channel identity set changed after proof")
        for name, bound_fp in rec.channel_fps:
            producer = TRUSTED_PRODUCERS.get(name)
            if producer is None:
                raise CapabilityRefused(f"channel {name!r} lost its trusted producer")
            if fingerprint(producer()) != bound_fp:
                raise CapabilityRefused(f"channel {name!r} producer content changed after proof")
            # STRICT, and deliberately so.  An UNCONSUMED bound channel has
            # consumed_fp None, which is != bound_fp and therefore refuses: a channel
            # that is attested but never read widens the attested surface without
            # anything having looked at it (prototype attack 10c).  An earlier draft
            # of this file allowed None here "because some gates may not consume";
            # the phase-1 barrier scored that as a DEFEAT.  If a gate legitimately
            # does not consume a channel, the channel must not be bound.
            if txn.channels[name].consumed_fp != bound_fp:
                raise CapabilityRefused(
                    f"channel {name!r} consumed content differs from bound content")
        # --- S6 re-bind, DEFERRED half (§17-D) -------------------------------
        if bool(rec.deferred) is not (txn.source is not None):
            raise CapabilityRefused("deferred-binding state changed after issuance")
        if rec.deferred:
            ds = txn.source
            if ds.identity() != rec.source_id:
                raise CapabilityRefused(
                    "deferred source selection or loader identity changed after proof")
            if ds.content_fp != rec.source_fp:
                raise CapabilityRefused("deferred source content changed after proof")
            # STRICT, exactly as for channels (prototype attack 10c): a source that was attested
            # but never READ widens the attested surface without anything having looked at it.
            # consumed_fp is None until a gate calls consume(), and None != the bound fingerprint.
            if ds.consumed_fp != rec.source_fp:
                raise CapabilityRefused(
                    "deferred source consumed content differs from bound content")
            # CONTENT PROVENANCE: re-derive through the SAME loader over the SAME selection.
            # This is the deferred analogue of `fingerprint(producer()) != bound_fp`.
            if ds.rederive_fp() != rec.source_fp:
                raise CapabilityRefused(
                    "deferred source loader no longer produces the bound content")

        live_gates = tuple(sorted(
            (n, r.outcome.value) for n, r in txn.gate_results.items()))
        if live_gates != rec.gate_results:
            raise CapabilityRefused("gate result set changed after issuance")

        rec.consumed = True
        txn.advance(State.CAPABILITY_ISSUED, State.LOAD_BEARING_OPERATION)

        verdict = Verdict.PROBE_RESULT if rec.probe else Verdict.CERTIFIED
        result = CertifiedResult(
            _ISSUER_SENTINEL, verdict, rec.subject_fp,
            getattr(rec.relation, "value", rec.relation), rec.gate_results, rec.activation,
            rec.source_id)
        _ISSUER.certified[id(result)] = result
        txn.advance(State.LOAD_BEARING_OPERATION, State.CERTIFIED_RESULT)
        return result


def certify_deferred(capability) -> CertifiedResult:
    """The deferred route's call INTO the single sink.  NOT a second sink.

    It mints nothing: no `CertifiedResult`, no write to `_ISSUER.certified`, no `Verdict`.  All it
    does is resolve the subject the TCB itself loaded -- which the caller never held and therefore
    cannot present -- and hand it to `certify_result`, which performs the identical S5->S6->S7
    re-verification including every deferred check above.
    """
    if not isinstance(capability, Capability):
        raise CapabilityRefused("not a capability")
    with _ISSUER._lock:
        entry = _ISSUER.capabilities.get(id(capability))
        if entry is None:
            raise CapabilityRefused(
                "capability is not in the issuer registry (structure confers no authority)")
        stored_cap, rec = entry
        if stored_cap is not capability:
            raise CapabilityRefused("capability identity mismatch")
        if not rec.deferred:
            raise CapabilityRefused(
                "certify_deferred is for deferred-binding transactions only; an immediate-binding "
                "caller must present the subject it actually holds")
        subject = rec.subject
    return certify_result(capability, subject)


# ---------------------------------------------------------------------------
# TRUSTED_PRODUCERS lives in the TCB (§12): channel content authority is never
# in the caller's request.  cc_contract populates it.
# ---------------------------------------------------------------------------

TRUSTED_PRODUCERS: dict = {}
# --- END SEALED TCB ---
# ============================================================================================
# cc_contract -- THE CLOSED CONTRACTS (blueprint §2, phase 2)
#
# Concatenated INTO completeness_framework.py after the banked body, so every banked
# registry (_REGISTRY, _ALIASES, _STEERING_CENTRALLY_ADJUDICATED, WITNESS_FIELD_MANIFEST)
# is already in scope and the closed sets are DERIVED from them rather than re-typed.
# A hand-typed copy of a registry is a second registry, and the two drift.
# ============================================================================================

# --- §10 relations: the closed enum, derived from the banked comparator registry ------------
CC_PERMITTED_RELATIONS = frozenset(_REGISTRY)
CC_RELATION_ALIASES = dict(_ALIASES)

# --- §10 NESTING: permitted parent -> child PAIRS, one reviewed pair at a time ---------------
# Blueprint R1 (HIGH): the prototype proved closure with NESTABLE_RELATIONS EMPTY, and P5 needs
# real nesting.  A blanket "nesting allowed" re-opens the relation class, so the set below is a
# PAIR set enumerated from the banked _reenter/_recurse/_delegate call sites.  An unlisted pair
# refuses.
#
# STATIC pairs -- read off the banked relation bodies:
CC_NESTABLE_PAIRS_STATIC = frozenset({
    ("DISJOINT_WITH_FLOOR", "DISJOINT"),                    # _rel_disjoint_with_floor
    ("SCHEMA_STRICTNESS", "EXACT"),                          # _rel_schema_strictness
    ("PROVENANCE_CORRESPONDENCE", "EXACT"),                  # _rel_provenance_correspondence
    ("PROVENANCE_CORRESPONDENCE", "REQUIRED_SUPERSET"),      # suppression limb
    ("HASH_BACKSTOP", "REQUIRED_SUBSET"),                    # _rel_hash_backstop
    ("KEYED_MAPPING_AGAINST_UNION", "EXACT"),                # keys
    ("KEYED_MAPPING_AGAINST_UNION", "REQUIRED_SUBSET"),      # values
})

# DYNAMIC pairs -- KEYED_MAPPING takes its children from `key_relation` / `value_relation`, which
# are SPEC-AUTHORED.  That is the one genuinely open limb in the banked nesting model, and it is
# closed here to the reviewed children below.  Each is a set-shaped relation that can adjudicate a
# key set or a per-key value set; the defaults are the banked defaults (EXACT / REQUIRED_SUBSET).
# A spec naming any other child is refused rather than recursed.
CC_KEYED_MAPPING_CHILDREN = frozenset({
    "EXACT", "REQUIRED_SUBSET", "REQUIRED_SUPERSET", "DISJOINT", "SCHEMA_STRICTNESS",
})
CC_NESTABLE_PAIRS = CC_NESTABLE_PAIRS_STATIC | frozenset(
    ("KEYED_MAPPING", child) for child in CC_KEYED_MAPPING_CHILDREN)


def cc_nesting_refusal(parent: str, child: str):
    """Returns a problem dict when a parent->child nesting pair is outside the reviewed set."""
    p = resolve_relation(parent) or parent
    c = resolve_relation(child) or child
    if (p, c) in CC_NESTABLE_PAIRS:
        return None
    return _problem(p, "NESTING_PAIR_NOT_REVIEWED",
                    f"relation {p!r} may not nest {c!r}: the permitted parent->child pairs are a "
                    "closed reviewed set, and an unlisted pair is REFUSED rather than recursed")


# --- §11 steering: the closed key domain, derived from the banked universe -------------------
CC_DECLARED_STEERING_KEYS = frozenset(_STEERING_CENTRALLY_ADJUDICATED)

# --- §9 the closed REQUEST key set ----------------------------------------------------------
# Wider than the prototype's four keys because a framework evaluation binds framework operands.
# The set is CLOSED and exact: a request carrying any other key refuses.
CC_REQUEST_KEYS = frozenset({
    "object",      # the collection operand -- the subject the certificate is issued over
    "relation",    # the declared relation name (resolved against the closed enum)
    "steering",    # the normalised steering payload -> SteeringEnvelope
    "channels",    # P6 channel declarations -> ChannelBinding
    "spec",        # the governed spec
    "cid",         # collection id
    "binding",     # provider binding, or None
    "route",       # which adapter opened the transaction (closed enum below)
})

CC_ROUTES = frozenset({"compare", "verify_provider", "verify_non_enumerable", "evaluate"})

# --- §17-D the DEFERRED-BINDING routes -------------------------------------------------------
# A route is deferred when its SUBJECT does not exist at request time -- the caller NAMES a source
# and a loader produces it.  Part D is the only such route: it selects an authoritative source by
# id and loads it, and the P8 steering gate has to adjudicate that SELECTION before the loader
# runs, because a steered source selection is the same lie one layer up.
#
# This set lives in the TCB and is a SUBSET of the closed route enum, checked here rather than
# assumed: a deferred route that is not a route at all would acquire the deferred stage without
# ever passing route validation.
CC_DEFERRED_ROUTES = frozenset({"verify_non_enumerable"})
# Written as a membership comprehension rather than set algebra ON PURPOSE: P4's INV-6 analyser
# reads the file for set differences/intersections outside a gate-reachable checker, and it is
# right to.  A contract self-check has no business looking like a parallel evaluator.
_cc_undeclared_deferred = sorted(r for r in CC_DEFERRED_ROUTES if r not in CC_ROUTES)
if _cc_undeclared_deferred:
    raise RuntimeError(
        "CC_DEFERRED_ROUTES names routes outside the closed route enum: "
        f"{_cc_undeclared_deferred}")
DEFERRED_ROUTES = CC_DEFERRED_ROUTES


# --- §16 per-gate NOT_APPLICABLE vocabularies -----------------------------------------------
# Every basis states a fact about the CONTRACT or a fact the framework COMPUTED.
# "The instrumentation was absent" is never a basis; that is CANNOT_PROVE.
CC_NA_BASES = {
    "G_P1_GUARD_CONTENT": frozenset({
        "P1_NA_RELATION_UNADJUDICABLE_DIRECTION",
    }),
    "G_P1_GUARD_ACTIVATION": frozenset({
        "P1_NA_RELATION_UNADJUDICABLE_DIRECTION",
    }),
    "G_P2_INDEPENDENCE_EXPERIMENT": frozenset(_P2_NA_BASES),
    "G_P3_CLOSED_SCHEMA": frozenset({
        "P3_NA_NO_DECLARED_FRAMEWORK_KIND",
    }),
    "G_P3_KIND_REQUIREMENT": frozenset({
        "P3_NA_NOT_PROVIDER_BACKED",
    }),
    "G_P4_ROUTE_TOTALITY": frozenset(),          # structural gate: always applicable
    "G_P5_RELATION_COMPARATOR": frozenset({
        "P5_NA_NO_COMPARISON_OPERANDS",
    }),
    "G_P6_CHANNEL_DECLARATION": frozenset({
        "P6_NA_NO_DECLARED_CHANNELS",
    }),
    "G_P7_WITNESS_ADEQUACY": frozenset({
        "P7_NA_NO_WITNESS_FORM_DECLARED",
    }),
    "G_P8_STEERING_PIN": frozenset({
        "P8_NA_NO_STEERING_SURFACE",
    }),
    "G_P9_SUPPLIED_IDENTITY": frozenset({
        "P9_NA_INLINE_OBSERVED_NO_CALLABLE",
    }),
    "G_P9_WITNESS_IDENTITY": frozenset({
        "P9_NA_INLINE_OBSERVED_NO_CALLABLE",
    }),
    "G_P2_COVERAGE": frozenset({
        "P2_COVERAGE_NA_NO_INJECTED_AUTHORITY",
    }),
    # --- §17-D the DEFERRED (non-enumerable) route ------------------------------------------
    # EVERY one of these vocabularies is EMPTY, and deliberately.  A non-enumerable evaluation
    # has no inapplicable stage: the config contract, the identity scan, the steering pin, the
    # source selection, the presence gate, the ambient closure, the witness invoker, the
    # MUST_DEPEND limb, the authority injection and the kind verifier all APPLY to every one of
    # the five kinds.  An empty vocabulary means any NOT_APPLICABLE claim on this route is
    # NOT_APPLICABLE_UNJUSTIFIED and blocks issuance -- which is the fail-closed direction.
    "G_D_NE_CONFIG": frozenset(),
    "G_D_SOURCE_SELECTION": frozenset(),
    "G_D_SOURCE_PRESENCE": frozenset(),
    "G_D_GUARANTEE_KIND": frozenset(),
    "G_P2_NE_AMBIENT_CLOSURE": frozenset(),
    "G_P2_NE_MUST_DEPEND": frozenset(),
    "G_P9_NE_INVOKER": frozenset(),
    "G_P6_NE_PAYLOAD": frozenset(),
    # Part D coverage.  Non-empty on purpose, unlike its siblings: the representation-faithful
    # coverage law (O⊇A over the certificate-bound observation) is the contract of kind B alone.
    # Kind A is scoped OUT of membership-completeness (FIX 7, influence != representation); kinds
    # C/D/E report other derived operands with their own protection.  So coverage is genuinely
    # NOT_APPLICABLE for every kind but B.
    "G_P2_NE_COVERAGE": frozenset({"P2_NE_COVERAGE_NA_NOT_INJECTED_AUTHORITY_KIND"}),
}

# The prose behind each basis.  Adding a basis is a diff, which is the point.
CC_NA_PROSE = {
    "P1_NA_RELATION_UNADJUDICABLE_DIRECTION":
        "the relation is IN the closed enum and carries no adjudicable direction, so there is no "
        "load-bearing direction for the spec to declare.  A relation OUTSIDE the enum is refused "
        "by the closed relation contract long before P1 is consulted, so P1 can never be NA "
        "because of an unknown relation.",
    "P3_NA_NO_DECLARED_FRAMEWORK_KIND":
        "the spec names no framework kind, so no framework FORM governs its field vocabulary at "
        "this gate.  The kind REQUIREMENT itself is a separate gate below the witness-form gate, "
        "so a spec that owes a kind is still refused -- just by the gate that owns that question.",
    "P3_NA_NOT_PROVIDER_BACKED":
        "computed: the spec declares no `provider`, so the provider-backed framework-kind "
        "requirement does not apply to it.",
    "P5_NA_NO_COMPARISON_OPERANDS":
        "the contract declares a non-comparing form.  'No collection was supplied' is missing "
        "instrumentation and is CANNOT_PROVE, never this basis.",
    "P6_NA_NO_DECLARED_CHANNELS":
        "the contract declares no channel.  An UNRESOLVED channel is CANNOT_PROVE.",
    "P7_NA_NO_WITNESS_FORM_DECLARED":
        "the contract form carries no witness at all.  An UNRESOLVABLE witness is CANNOT_PROVE.",
    "P8_NA_NO_STEERING_SURFACE":
        "computed: the producer read-set is empty, so there is no steering surface to pin.",
    "P2_COVERAGE_NA_NO_INJECTED_AUTHORITY":
        "computed: the spec carries an inline `observed`/code-native operand rather than a `provider` "
        "the framework runs, so it injects no _p2_declared_channels authority.  The code-native "
        "observation path is governed by P6 purity and the P7 witness form, not by coverage.  A RUN "
        "`provider` with no injected authority does NOT reach this basis — it fails closed "
        "(P2_COVERAGE_NO_INJECTED_AUTHORITY).",
    "P2_NE_COVERAGE_NA_NOT_INJECTED_AUTHORITY_KIND":
        "computed: the guarantee kind is not INDEPENDENT_SITE_UNIVERSE (kind B), the only kind whose "
        "observation IS the discovered universe and admits the representation-faithful O⊇A coverage "
        "law over the certificate-bound observation.  Kind A is scoped out of membership-completeness "
        "entirely (its clean verdict is non-authoritative for is_complete); kind C reports a derived "
        "grounds operand and kinds D/E their two-polarity probes, so coverage does not apply to them.",
    "P9_NA_INLINE_OBSERVED_NO_CALLABLE":
        "the contract carries no provider callable, so there is no callable identity to "
        "recompute.  Pairs with P2's CONTRACT_INLINE_OBSERVED_IS_P7 basis.",
}
CC_NA_BASES["G_P2_INDEPENDENCE_EXPERIMENT"] = frozenset(_P2_NA_BASES)
for _basis in CC_NA_BASES["G_P2_INDEPENDENCE_EXPERIMENT"]:
    CC_NA_PROSE.setdefault(_basis, _P2_NA_BASES[_basis] if isinstance(_P2_NA_BASES, dict) else "")# ============================================================================================
# cc_gates -- THE NINE P-PROPERTIES AS GATES (blueprint §1.2, phase 3)
#
# Every gate BODY is the banked f7d92912 mechanism, called unchanged.  What changes is that a
# property is no longer a POSITION in a preemptive pipeline -- it is a row in GATE_REGISTRY with
# declared preconditions, a declared NA vocabulary, declared requirement rows and a declared
# contribution.  Every gate runs or records CANNOT_PROVE; nothing is skipped silently.
#
# ORDERING.  Registry order still determines DETECTOR ATTRIBUTION (first PREEMPT failure renders),
# and it reproduces the banked pipeline order exactly, so a spec that was refused by detector X
# before is refused by detector X now.  What it no longer determines is CORRECTNESS: a masked gate
# still records its outcome, and any non-proved outcome blocks issuance.  Under the banked design a
# wrong order was a fail-open (P7 27/57 at RM2); here it can only mis-attribute.
# ============================================================================================


def _cc_gate_p9_supplied_identity(txn) -> GateResult:
    """P9, first limb: caller-supplied identity/provenance is refused before anything reads it."""
    gid = "G_P9_SUPPLIED_IDENTITY"
    spec, cid = txn.spec, txn.cid
    forged = p9_guard_supplied_identity(spec, cid)
    txn.ledger.record_requirement(gid, "supplied_identity_scan", ran=True)
    if forged is not None:
        return GateResult(gid, GateOutcome.FAILED, detail=forged,
                          problems=[_problem(txn.relation, "PROVIDER_IDENTITY_REFUSED", forged)])
    if txn.binding is not None and txn.binding.get("problems"):
        txn.ledger.record_requirement(gid, "binding_scan", ran=True)
        return GateResult(
            gid, GateOutcome.FAILED, detail="binding carries identity problems",
            problems=[_problem(txn.relation, "PROVIDER_IDENTITY_REFUSED", p)
                      for p in txn.binding["problems"]])
    txn.ledger.record_requirement(gid, "binding_scan", ran=True)
    return GateResult(gid, GateOutcome.APPLICABLE_AND_PROVED)


def _cc_gate_p1_guard_content(txn) -> GateResult:
    """P1, CONTENT half: the declaration is present and says something the framework refuses
    (COPIED_ORACLE / SELF_REFERENCE / DIRECTION_UNWITNESSABLE / UNKNOWN_RELATION)."""
    gid = "G_P1_GUARD_CONTENT"
    spec, cid, relation = txn.spec, txn.cid, txn.relation
    ig = _guard_independence_content(spec, cid)
    txn.ledger.record_requirement(gid, "independence_content", ran=True)
    if ig is not None:
        return GateResult(gid, GateOutcome.FAILED, detail=ig["detail"], problems=[ig])
    dg = _guard_directionality_content(relation, spec)
    txn.ledger.record_requirement(gid, "directionality_content", ran=True)
    if dg is not None:
        return GateResult(gid, GateOutcome.FAILED, detail=dg["detail"], problems=[dg])
    return GateResult(gid, GateOutcome.APPLICABLE_AND_PROVED)


def _cc_gate_p5_relation_comparator(txn) -> GateResult:
    """P5, first limb: the equivalence the operands are compared under is DERIVED from the domain
    class, not authored in data (P7-FIND-04) -- and the relation is in the closed enum."""
    gid = "G_P5_RELATION_COMPARATOR"
    spec, cid, relation = txn.spec, txn.cid, txn.relation

    canonical = resolve_relation(relation)
    txn.ledger.record_requirement(gid, "relation_in_closed_enum", ran=True)
    if canonical is None:
        return GateResult(
            gid, GateOutcome.FAILED, detail=f"unknown relation {relation!r}",
            problems=[_problem(relation, "UNKNOWN_RELATION",
                               f"relation {relation!r} is not implemented and has no alias; an "
                               "unknown relation cannot pass -> REFUSED")])

    # Closed NESTING contract (blueprint R1): an unreviewed parent->child pair refuses.
    for field, default in (("key_relation", "EXACT"), ("value_relation", "REQUIRED_SUBSET")):
        child = spec.get(field)
        if child is None:
            continue
        refusal = cc_nesting_refusal(canonical, child)
        if refusal is not None:
            txn.ledger.record_requirement(gid, "nesting_pair_reviewed", ran=True)
            return GateResult(gid, GateOutcome.FAILED, detail=refusal["detail"],
                              problems=[refusal])
    txn.ledger.record_requirement(gid, "nesting_pair_reviewed", ran=True)

    ng = guard_normalize_derivation(spec, cid)
    txn.ledger.record_requirement(gid, "normalize_derivation", ran=True)
    if ng is not None:
        return GateResult(gid, GateOutcome.FAILED, detail=ng["detail"], problems=[ng])
    return GateResult(gid, GateOutcome.APPLICABLE_AND_PROVED)


def _cc_gate_p3_closed_schema(txn) -> GateResult:
    """P3: a spec that NAMES its framework kind has a governed field vocabulary whatever shape its
    witness turns out to have, so the closed-schema gate adjudicates it immediately and an
    undeclared field is refused before any witness executes (RM8)."""
    gid = "G_P3_CLOSED_SCHEMA"
    spec = txn.spec
    declared_kind = spec.get("resolver") or spec.get("framework_kind")
    if declared_kind is None:
        txn.ledger.record_requirement(gid, "field_vocabulary", ran=False,
                                      state="NOT_APPLICABLE")
        return GateResult(gid, GateOutcome.NOT_APPLICABLE_BY_CLOSED_CONTRACT,
                          rule_id="P3_NA_NO_DECLARED_FRAMEWORK_KIND")
    problems = _reject_unknown_fields("framework", declared_kind, spec)
    txn.ledger.record_requirement(gid, "field_vocabulary", ran=True)
    if problems:
        return GateResult(gid, GateOutcome.FAILED, detail=problems[0]["detail"],
                          problems=problems)
    return GateResult(gid, GateOutcome.APPLICABLE_AND_PROVED)


def _cc_gate_p6_channel_declaration(txn) -> GateResult:
    """P6, declaration limb: declare_steering_channels binds the spec's P6 channels and refuses an
    UNBOUND one (STEERING_UNVERIFIED -- 'was the channel perturbed?')."""
    gid = "G_P6_CHANNEL_DECLARATION"
    problems: list = []
    spec = declare_steering_channels(txn.spec, txn.cid, problems)
    txn.ledger.record_requirement(gid, "channel_declaration", ran=True)
    if problems:
        return GateResult(gid, GateOutcome.FAILED, detail=problems[0]["detail"],
                          problems=problems)
    txn.spec = spec           # threaded: the gate's product, not the caller's dict
    return GateResult(gid, GateOutcome.APPLICABLE_AND_PROVED)


def _cc_gate_p8_steering_pin(txn) -> GateResult:
    """P8: ONE adjudicator, ONE site.  The banked design ran steering_pin_problems at several
    enforcement sites because there were several routes to witness execution; the capability
    removes them, so the multi-site design collapses to the gate."""
    gid = "G_P8_STEERING_PIN"
    steering = steering_pin_problems(txn.spec, txn.cid, source=_STEERING_SOURCE_PROGRAMMATIC)
    txn.ledger.record_requirement(gid, "central_pin_adjudicated", ran=True)
    if steering:
        return GateResult(
            gid, GateOutcome.FAILED, detail=steering[0],
            problems=[_problem(txn.relation, "STEERING_UNPINNED", detail)
                      for detail in steering])
    return GateResult(gid, GateOutcome.APPLICABLE_AND_PROVED)


def _cc_gate_p7_witness_adequacy(txn) -> GateResult:
    """P7: resolve_witness_fields IS this gate's body.  The FORM manifests are already a closed
    permitted-set model and transplant directly onto the closed-contract invariant."""
    gid = "G_P7_WITNESS_ADEQUACY"
    problems: list = []
    spec, ok = resolve_witness_fields(txn.spec, txn.cid, txn.collection, problems)
    txn.ledger.record_requirement(gid, "witness_form_resolved", ran=True)
    if not ok:
        return GateResult(gid, GateOutcome.FAILED,
                          detail=problems[0]["detail"] if problems else "witness unresolved",
                          problems=problems)
    txn.spec = spec
    if "normalize" not in spec and spec.get("domain_class") in _DOMAIN_CLASS_NORMALIZE:
        spec["normalize"] = list(derived_normalize(spec))
    txn.ledger.record_requirement(gid, "derived_normalize_bound", ran=True)
    return GateResult(gid, GateOutcome.APPLICABLE_AND_PROVED)


def _cc_gate_p3_kind_requirement(txn) -> GateResult:
    """P3's KIND REQUIREMENT (A-29), separated from the field-vocabulary rule and placed BELOW the
    witness-form gate so it governs provider-backed specs only and never masks an inline or
    code-native form (RM3's fix, preserved as a `requires=` edge instead of a call position)."""
    gid = "G_P3_KIND_REQUIREMENT"
    spec, relation = txn.spec, txn.relation
    kind = spec.get("resolver") or spec.get("framework_kind")
    if spec.get("provider") is None:
        if kind is None:
            txn.ledger.record_requirement(gid, "kind_declared", ran=False,
                                          state="NOT_APPLICABLE")
            return GateResult(gid, GateOutcome.NOT_APPLICABLE_BY_CLOSED_CONTRACT,
                              rule_id="P3_NA_NOT_PROVIDER_BACKED")
        problems = _reject_unknown_fields("framework", kind, spec)
        txn.ledger.record_requirement(gid, "kind_declared", ran=True)
        if problems:
            return GateResult(gid, GateOutcome.FAILED, detail=problems[0]["detail"],
                              problems=problems)
        return GateResult(gid, GateOutcome.APPLICABLE_AND_PROVED)

    txn.ledger.record_requirement(gid, "kind_declared", ran=True)
    if kind is None:
        p = _problem(relation, "NO_FRAMEWORK_KIND",
                     f"{txn.cid}: this provider-backed spec declares neither `resolver` nor "
                     "`framework_kind`, so no framework form governs its field vocabulary "
                     "and P3 cannot adjudicate it; REFUSED")
        return GateResult(gid, GateOutcome.FAILED, detail=p["detail"], problems=[p])
    problems = _reject_unknown_fields("framework", kind, spec)
    if problems:
        return GateResult(gid, GateOutcome.FAILED, detail=problems[0]["detail"],
                          problems=problems)
    return GateResult(gid, GateOutcome.APPLICABLE_AND_PROVED)


def _cc_gate_p9_witness_identity(txn) -> GateResult:
    """P9: the observed operand is derived by EXECUTING the resolved witness under recomputed
    identity.  Identity is recomputed from live + disk + module-attr, never accepted."""
    gid = "G_P9_WITNESS_IDENTITY"
    problems: list = []
    observed, ok = _resolve_observed(txn.spec, txn.cid, problems, txn.binding)
    txn.ledger.record_requirement(gid, "identity_recomputed", ran=True)
    if not ok:
        return GateResult(gid, GateOutcome.FAILED,
                          detail=problems[0]["detail"] if problems else "observed unresolved",
                          problems=problems)
    # FIX 9 — MATERIALIZE the observed operand ONCE at the trust boundary, so the comparator,
    # coverage and the P2 X0 determinism guard all consume the SAME immutable canonical value and
    # none re-iterates the raw witness object (a reader-adaptive subclass or a mutable member can no
    # longer present a covering view to coverage and a short one to the comparator).  Fail-closed on
    # a subclass or a non-materializable member.
    try:
        txn.observed = _materialize_observation(observed, txn.cid)
    except NonEnumerableError as exc:
        return GateResult(
            gid, GateOutcome.FAILED, detail=str(exc),
            problems=[_problem(txn.relation, "OBSERVATION_NOT_MATERIALIZABLE", str(exc))])
    return GateResult(gid, GateOutcome.APPLICABLE_AND_PROVED)


def _cc_gate_p2_independence(txn) -> GateResult:
    """P2: the computational-independence EXPERIMENT, run BEFORE the comparator so a lying witness
    never becomes an operand.

    CONTRIBUTION IS PER-OUTCOME, and that is the banked semantics expressed as data:
      * a DECIDED finding (this observation TRACKS the collection) PREEMPTS -- the operand is a
        proven lie and comparing with it would report findings computed from it;
      * an UNDECIDED finding says only that the experiment could not DECIDE, and is APPENDED, so
        it cannot mask what the presence gate, P1 activation and the comparator had to say.
    """
    gid = "G_P2_INDEPENDENCE_EXPERIMENT"
    independence = p2_verify_witness_independence(
        txn.spec, txn.collection, txn.cid, txn.binding, txn.observed)
    txn.ledger.record_requirement(gid, "experiment_ran", ran=True)
    if not independence:
        return GateResult(gid, GateOutcome.APPLICABLE_AND_PROVED)
    decided = [p for p in independence if p.get("kind") not in _P2_UNDECIDED_KINDS]
    if decided:
        return GateResult(gid, GateOutcome.FAILED, detail=decided[0]["detail"],
                          problems=independence, contribution=Contribution.PREEMPT)
    return GateResult(gid, GateOutcome.CANNOT_PROVE,
                      rule_id="P2_EXPERIMENT_UNDECIDED",
                      detail=independence[0]["detail"],
                      problems=independence, contribution=Contribution.APPEND)


def _cc_gate_p5_comparator_verdict(txn) -> GateResult:
    """P5, second limb: the comparator itself.  Reaches the verdict through the banked single
    witness-evaluation gate, which keeps the normalizer, the duplicate-collision detector, the
    presence gate and the transit ledger on the path.  The transit ledger is retained as a
    REFUSAL-ONLY layer: it can still refuse an unscoped or ungated verdict, and it can no longer
    be the thing that MAKES a verdict load-bearing -- the capability is."""
    gid = "G_P5_COMPARATOR_VERDICT"
    verdict = _cc_legacy_compare(txn.relation, txn.observed, txn.collection, txn.spec)
    txn.ledger.record_requirement(gid, "comparator_ran", ran=True)
    if verdict:
        return GateResult(gid, GateOutcome.FAILED, detail=verdict[0]["detail"],
                          problems=verdict)
    return GateResult(gid, GateOutcome.APPLICABLE_AND_PROVED)


def _cc_gate_p1_guard_activation(txn) -> GateResult:
    """P1, ACTIVATION half: for every guard g and every spec s, if the field that ACTIVATES g is
    absent from s then the verdict on s must not be CLEAN.

    APPEND, and per the banked prose that is not an ordering workaround -- it is the property's
    own shape.  P1's prohibited state is 'a CLEAN verdict with an activating field absent', not
    'this refusal must outrank that one'; a spec ALSO refused on its merits reports both.
    """
    gid = "G_P1_GUARD_ACTIVATION"
    problems = _p1_activation_problems(txn.relation, txn.spec, txn.cid)
    txn.ledger.record_requirement(gid, "activation_scan", ran=True)
    if problems:
        return GateResult(gid, GateOutcome.FAILED, detail=problems[0]["detail"],
                          problems=problems, contribution=Contribution.APPEND)
    return GateResult(gid, GateOutcome.APPLICABLE_AND_PROVED)


# ============================================================================================
# P2-COVERAGE — THE FAITHFULNESS / UNDER-REPORTING GATE
# (gate 4N-I28BH-B0w-R2-SLICE1, A9 headline: the DEPENDENT UNDER-REPORTING witness)
# ============================================================================================
# THE DEFECT THIS CLOSES.  P2's independence experiment proves the witness DEPENDS on the injected
# authority (its observation MOVES when the authority moves), that each asserted member is
# ACCOUNTABLE to the authority listing it, and that the observation is MONOTONE under restriction.
# NONE of those three laws proves COVERAGE.  A witness that is a faithful, deterministic, monotone,
# accountable function of its injected authority but simply DROPS required members satisfies every
# P2 law and yet certifies an INCOMPLETE collection:  `keep_only_'a'` over an injected {a,b,c}
# returns {a}, the collection {a} then satisfies the relation against the SHORTENED authority the
# witness reported, and b and c vanish with the witness agreeing.  P2 verifies INDEPENDENCE;
# coverage verifies FAITHFULNESS/COMPLETENESS of the observation with respect to the authority.
#
# WHY IT IS SOUND AND NON-CIRCULAR.  The required domain is A = the authority the FRAMEWORK
# INJECTED, not O = what the witness returned.  The framework holds A independently: it builds
# `_witness_inputs` from the resolved P6 channels in `_p2_witness_payload`/`_p6_observe` and hands
# the witness a COPY.  So `_p2_declared_channels(spec)` re-derives A here from the same channel
# registry, and the gate asks whether O ACCOUNTS FOR every member of A.  Dropping a member of A is
# therefore ALWAYS under-reporting — never a legitimate completeness transform, because a legitimate
# transform of A still accounts for every member of A.
#
# THE CLOSED SET OF ACCOUNTING TRANSFORMS.  O accounts for A iff every member of A maps to some
# member of O under one of:
#   IDENTITY    — the A-member is literally present in O (O ⊇ A on the membership view);
#   NORMALIZED  — a DECLARED closed normalization the FRAMEWORK replays (never the witness) carries
#                 the A-member to an O-member, AND that normalization is INJECTIVE over A so it
#                 cannot COLLAPSE two distinct required members into one.  A non-injective
#                 "normalization" is lossy (it hides that two members became one) and is refused —
#                 it is under-reporting wearing a rename.
# Every other transform — AGGREGATION / DERIVATION whose contribution the framework cannot TRACE
# through an opaque witness — and every A-member with no accounted mapping FAIL CLOSED: no
# capability.  This is the framework's CANNOT_PROVE->refuse convention: coverage that cannot be
# proven is refused, never waved through.
#
# WHAT LEGITIMATELY STILL CERTIFIES.  An honest witness whose observation is a SUPERSET of the
# injected authority (identity, plus possibly extra members it independently knows) covers A and
# certifies; a coverage-preserving rename (injective normalization) covers A and certifies.  The
# ccharness positive controls `clean_provider()` (O == A == {a}) and `clean_ne()` certify unchanged.


# The closed, framework-computed normalization set for the NORMALIZED accounting class.  Each entry
# would be replayed by the FRAMEWORK over BOTH A and O, so a witness cannot assert a normalization
# the framework does not itself perform.  IDENTITY is handled directly by the subset check below.
#
# SHIPPED IDENTITY-ONLY (FIX 5).  CASEFOLD/STRIP were withdrawn: they admit an A<->O collision the
# A-only injectivity guard does not catch.  With A={"user_a","user_b"} and O={"user_a","USER_B"},
# casefold is injective OVER A (it maps user_a,user_b to distinct images) yet the required member
# "user_b" is genuinely DROPPED while the UNRELATED "USER_B" casefolds onto its image — coverage
# would wrongly pass.  For the security domains this framework governs (IAM Sids, ARNs, site ids),
# case and whitespace are SECURITY-RELEVANT: "user_b" and "USER_B" are different principals.  A
# sound normalizer would need an injective A<->O correspondence, not just injectivity over A, so
# the accounting contract ships IDENTITY-ONLY.  The tuple keeps its closed shape so a future
# declared transform can be re-added ONLY together with an A<->O-sound check.
_COVERAGE_NORMALIZERS: tuple = ()


def _coverage_accounts(authority: set, observed: set):
    """(ok, uncovered, basis).  Does O account for every member of A under the CLOSED transform set?

    IDENTITY first (O ⊇ A).  Then each DECLARED framework-replayed normalizer, accepted ONLY when it
    is INJECTIVE over A (distinct required members keep distinct images, so nothing is silently
    collapsed) and carries every A-member's image into O.  No match under any admitted transform ->
    the residual is UNDER-REPORTING and the gate fails closed.

    Coverage is expressed as MEMBERSHIP tests and SUBSET comparisons, never as a set DIFFERENCE or
    INTERSECTION: taking `authority - observed` yourself is the parallel-evaluator shape P4's INV-6
    forbids, so the missing members are found by an explicit `not in` walk.
    """
    missing = {a for a in authority if a not in observed}
    if not missing:
        return True, frozenset(), "IDENTITY"
    for name, norm in _COVERAGE_NORMALIZERS:
        try:
            images = {a: norm(a) for a in authority}
        except Exception:
            continue
        image_set = set(images.values())
        if len(image_set) != len(authority):
            # NON-INJECTIVE over A: this normalization would collapse two required members into one,
            # so a witness could 'cover' both with a single O-member while one is genuinely absent.
            # A lossy normalization is not coverage-preserving; refuse it rather than trust it.
            continue
        observed_images = {norm(o) for o in observed}
        if image_set <= observed_images:
            return True, frozenset(), name
    return False, frozenset(sorted(missing)), "NONE"


def _cc_gate_p2_coverage(txn) -> GateResult:
    """P2-COVERAGE: the injected authority the framework HOLDS must be fully ACCOUNTED FOR by the
    witness observation.  Closes the dependent-under-reporter (A9 headline)."""
    gid = "G_P2_COVERAGE"
    spec = txn.spec if isinstance(txn.spec, dict) else {}
    channels, resolvable = _p2_declared_channels(spec)
    if not resolvable or not channels:
        # FIX 4 — NO SILENT SKIP FOR A RUN PROVIDER WITH NO INJECTED AUTHORITY.
        #
        # A `provider` witness is a callable the framework RUNS to read an authority.  If it
        # declares no resolvable authority channel (`reads`/`independent_source`) and is not an
        # inline pre-computed `observed` operand, then the framework injected NOTHING and the
        # provider AUTHORS its expected_domain: a SHORT authored / cached / fetched answer is
        # indistinguishable from a faithful reading, and the INDEPENDENCE limbs check dependence,
        # not completeness.  Coverage therefore CANNOT be proven -> FAIL CLOSED (CANNOT_PROVE),
        # never a silent NOT_APPLICABLE.  It is APPEND, not PREEMPT, so it adds its refusal without
        # masking a sibling detector that already refuses this spec for a sharper reason (e.g. the
        # comparator's EMPTY_OPERAND_REFUSED on a recursed operand).
        if spec.get("provider") and "observed" not in spec:
            txn.ledger.record_requirement(gid, "authority_injected_resolved", ran=True)
            return GateResult(
                gid, GateOutcome.CANNOT_PROVE, rule_id="P2_COVERAGE_NO_INJECTED_AUTHORITY",
                detail="a run provider injected no resolvable authority",
                problems=[_problem(
                    txn.relation, "P2_COVERAGE_NO_INJECTED_AUTHORITY",
                    f"{txn.cid}: the spec names a `provider` the framework EXECUTES but declares no "
                    "resolvable P6 authority channel (`reads`/`independent_source`), so the "
                    "framework injected NO authority into the witness payload.  The provider's "
                    "expected_domain is then anchored to nothing the framework holds — a short "
                    "authored or fetched answer is indistinguishable from a faithful reading, so "
                    "COVERAGE CANNOT BE PROVEN.  A completeness claim whose required domain the "
                    "framework can neither hold nor check fails closed; declare the authority as a "
                    "reviewed P6 channel so the framework injects and verifies it")],
                contribution=Contribution.APPEND)
        # STRUCTURALLY NOT PROVIDER-INJECTED: an inline `observed`/code-native operand carries its
        # own P6/P7 governance and injects no _p2_declared_channels authority.  There is no
        # framework-injected required domain for coverage to hold, so coverage is NOT_APPLICABLE by
        # reviewed basis (the code-native observation path is governed by P6 purity + P7 form).
        txn.ledger.record_requirement(gid, "authority_injected_resolved", ran=False,
                                      state="NOT_APPLICABLE")
        return GateResult(gid, GateOutcome.NOT_APPLICABLE_BY_CLOSED_CONTRACT,
                          rule_id="P2_COVERAGE_NA_NO_INJECTED_AUTHORITY")
    try:
        authority: set = set()
        for content in channels.values():
            authority |= _p2_member_strings(content)
        observed = _p2_member_strings(txn.observed)
    except Exception as exc:  # a coverage question we cannot even compute is CANNOT_PROVE, not clean
        txn.ledger.record_requirement(gid, "authority_injected_resolved", ran=True)
        return GateResult(
            gid, GateOutcome.CANNOT_PROVE, rule_id="P2_COVERAGE_UNCOMPUTABLE", detail=repr(exc),
            problems=[_problem(txn.relation, "P2_COVERAGE_UNCOMPUTABLE",
                               f"{txn.cid}: the injected authority or the observation could not be "
                               f"reduced to a membership view ({type(exc).__name__}); coverage "
                               "cannot be proven, so the completeness claim fails closed")])
    txn.ledger.record_requirement(gid, "authority_injected_resolved", ran=True)
    ok, uncovered, basis = _coverage_accounts(authority, observed)
    txn.ledger.record_requirement(gid, "coverage_accounted", ran=True)
    if not ok:
        return GateResult(
            gid, GateOutcome.FAILED, detail=f"observation under-reports authority: {sorted(uncovered)}",
            problems=[_problem(
                txn.relation, "P2_COVERAGE_UNDER_REPORT",
                f"{txn.cid}: the witness observation does not ACCOUNT FOR every member of the "
                f"framework-injected authority: {sorted(uncovered)} present in the injected "
                "authority is absent from the observation under IDENTITY accounting (O must be a "
                "SUPERSET of the injected authority). A faithful witness reports a SUPERSET of its "
                "authority; DROPPING a required member is under-reporting — it certifies an "
                "INCOMPLETE collection — and is REFUSED")],
            contribution=Contribution.PREEMPT)
    return GateResult(gid, GateOutcome.APPLICABLE_AND_PROVED)


# --------------------------------------------------------------------------------------------
# THE REGISTRY for the provider route.  Order == banked pipeline order (attribution), and the
# `requires=` edges are the SAFETY ordering, now declared as data.
# --------------------------------------------------------------------------------------------

CC_GATE_REGISTRY_PROVIDER = {
    "G_P9_SUPPLIED_IDENTITY": GateSpec(
        "G_P9_SUPPLIED_IDENTITY", _cc_gate_p9_supplied_identity,
        na_bases=CC_NA_BASES["G_P9_SUPPLIED_IDENTITY"],
        requirements=("supplied_identity_scan", "binding_scan")),
    "G_P1_GUARD_CONTENT": GateSpec(
        "G_P1_GUARD_CONTENT", _cc_gate_p1_guard_content,
        na_bases=CC_NA_BASES["G_P1_GUARD_CONTENT"],
        requirements=("independence_content", "directionality_content")),
    "G_P5_RELATION_COMPARATOR": GateSpec(
        "G_P5_RELATION_COMPARATOR", _cc_gate_p5_relation_comparator,
        na_bases=CC_NA_BASES["G_P5_RELATION_COMPARATOR"],
        requirements=("relation_in_closed_enum", "nesting_pair_reviewed",
                      "normalize_derivation")),
    "G_P3_CLOSED_SCHEMA": GateSpec(
        "G_P3_CLOSED_SCHEMA", _cc_gate_p3_closed_schema,
        na_bases=CC_NA_BASES["G_P3_CLOSED_SCHEMA"],
        requirements=("field_vocabulary",)),
    "G_P6_CHANNEL_DECLARATION": GateSpec(
        "G_P6_CHANNEL_DECLARATION", _cc_gate_p6_channel_declaration,
        na_bases=CC_NA_BASES["G_P6_CHANNEL_DECLARATION"],
        requirements=("channel_declaration",)),
    "G_P8_STEERING_PIN": GateSpec(
        "G_P8_STEERING_PIN", _cc_gate_p8_steering_pin,
        requires=("G_P6_CHANNEL_DECLARATION",),
        na_bases=CC_NA_BASES["G_P8_STEERING_PIN"],
        requirements=("central_pin_adjudicated",)),
    "G_P7_WITNESS_ADEQUACY": GateSpec(
        "G_P7_WITNESS_ADEQUACY", _cc_gate_p7_witness_adequacy,
        requires=("G_P3_CLOSED_SCHEMA", "G_P5_RELATION_COMPARATOR", "G_P8_STEERING_PIN"),
        na_bases=CC_NA_BASES["G_P7_WITNESS_ADEQUACY"],
        requirements=("witness_form_resolved", "derived_normalize_bound")),
    "G_P3_KIND_REQUIREMENT": GateSpec(
        "G_P3_KIND_REQUIREMENT", _cc_gate_p3_kind_requirement,
        requires=("G_P7_WITNESS_ADEQUACY",),
        na_bases=CC_NA_BASES["G_P3_KIND_REQUIREMENT"],
        requirements=("kind_declared",)),
    "G_P9_WITNESS_IDENTITY": GateSpec(
        "G_P9_WITNESS_IDENTITY", _cc_gate_p9_witness_identity,
        requires=("G_P7_WITNESS_ADEQUACY", "G_P3_KIND_REQUIREMENT"),
        na_bases=CC_NA_BASES["G_P9_WITNESS_IDENTITY"],
        requirements=("identity_recomputed",)),
    "G_P2_INDEPENDENCE_EXPERIMENT": GateSpec(
        "G_P2_INDEPENDENCE_EXPERIMENT", _cc_gate_p2_independence,
        requires=("G_P9_WITNESS_IDENTITY",),
        na_bases=CC_NA_BASES["G_P2_INDEPENDENCE_EXPERIMENT"],
        requirements=("experiment_ran",), render_order=20),
    # P2-COVERAGE.  A REQUIRED gate: a completeness-bearing certificate must transit it, exactly like
    # every other Pn gate.  Placed AFTER P2 so a witness that also fails independence (a constant) is
    # still attributed to P2's detector; a witness that PASSES independence but under-reports (the A9
    # dependent-under-reporter) is caught here and here alone.  Requires the observation P9 resolved.
    "G_P2_COVERAGE": GateSpec(
        "G_P2_COVERAGE", _cc_gate_p2_coverage,
        requires=("G_P9_WITNESS_IDENTITY",),
        na_bases=CC_NA_BASES["G_P2_COVERAGE"],
        requirements=("authority_injected_resolved", "coverage_accounted"), render_order=22),
    "G_P5_COMPARATOR_VERDICT": GateSpec(
        "G_P5_COMPARATOR_VERDICT", _cc_gate_p5_comparator_verdict,
        requires=("G_P9_WITNESS_IDENTITY",),
        requirements=("comparator_ran",)),
    "G_P1_GUARD_ACTIVATION": GateSpec(
        "G_P1_GUARD_ACTIVATION", _cc_gate_p1_guard_activation,
        na_bases=CC_NA_BASES["G_P1_GUARD_ACTIVATION"],
        requirements=("activation_scan",),
        contribution=Contribution.APPEND, render_order=10),
}


# ============================================================================================
# THE DEFERRED (NON-ENUMERABLE) ROUTE -- PART D THROUGH THE CLOSED MODEL   (§17-D)
#
# Part D is the one route whose SUBJECT does not exist when the request is validated: the spec
# NAMES an authoritative source and a loader produces it.  The banked body therefore loaded the
# source mid-function, after four refusal stages had run -- and that ordering was load-bearing,
# because a steered source SELECTION is the same lie one layer up.
#
# In the closed model that ordering is no longer a property of a function body.  The route is
# DEFERRED, so the state machine forbids the direct CONTRACT_VALIDATED -> OBJECT_BOUND edge and
# routes the transaction through SELECTION_GATED.  The pre-gate registry below adjudicates the
# SELECTION; only when every one of its rows has proved does the TCB invoke the loader and bind
# the object.  A pre-gate that refuses raises before the binding edge is ever attempted, so
# "the steering gate runs before the loader" is structural rather than conventional.
#
# PROBLEM SHAPE.  Part D merges into collection_completeness as STRINGS, so these gates carry
# strings in `problems` where the provider gates carry dicts.  The TCB does not care -- it never
# reads a problem -- and the deferred adapter renders whatever it is given, including the TCB's
# own dict-shaped totality synthesis, which is stringified rather than dropped.
# ============================================================================================


def _cc_gate_ne_config(txn) -> GateResult:
    """Part D's whole closed-config contract, run BEFORE anything is loaded or executed.

    validate_ne_config carries P3's non-enumerable field vocabulary, the guarantee-kind enum, the
    comparison enum, the declared-gate-transit rule (a kind with no adjudicated obligation cannot
    certify), the copied-oracle / self-enumeration / self-reference rules, kind D's and kind E's
    activating-field rules and the dependency-cycle rule.  It is one gate because it is one
    contract, and every clause of it must decide before a source is selected.
    """
    gid = "G_D_NE_CONFIG"
    cid = txn.cid
    try:
        validate_ne_config(txn.spec, dependency_resolver=txn.scratch.get("dependency_resolver"))
    except NonEnumerableError as exc:
        txn.ledger.record_requirement(gid, "config_validated", ran=True)
        return GateResult(gid, GateOutcome.FAILED, detail=str(exc),
                          problems=[f"{cid}: REFUSED — {exc}"])
    txn.ledger.record_requirement(gid, "config_validated", ran=True)
    # The capability is issued under the relation the kind's DECLARED gate transit names, so that
    # relation has to be in the closed comparator enum.  A kind whose declared transit named a
    # relation nothing implements would certify under a relation nobody can adjudicate.
    txn.ledger.record_requirement(gid, "transit_relation_in_closed_enum", ran=True)
    if resolve_relation(txn.relation) is None:
        return GateResult(
            gid, GateOutcome.FAILED, detail=f"unknown transit relation {txn.relation!r}",
            problems=[f"{cid}: REFUSED — the guarantee kind's declared gate transit names "
                      f"relation {txn.relation!r}, which is not in the closed comparator enum"])
    return GateResult(gid, GateOutcome.APPLICABLE_AND_PROVED)


def _cc_gate_ne_supplied_identity(txn) -> GateResult:
    """P9 first limb on the deferred route: caller-supplied identity/provenance is refused, and the
    config-validation-time witness binding is checked, before any witness or loader runs."""
    gid = "G_P9_SUPPLIED_IDENTITY"
    cid = txn.cid
    forged = p9_guard_supplied_identity(txn.spec, cid) if isinstance(txn.spec, dict) else None
    txn.ledger.record_requirement(gid, "supplied_identity_scan", ran=True)
    if forged is not None:
        return GateResult(gid, GateOutcome.FAILED, detail=forged,
                          problems=[f"{cid}: REFUSED — {forged}"])
    txn.ledger.record_requirement(gid, "binding_scan", ran=True)
    if txn.binding is not None and txn.binding.get("problems"):
        return GateResult(
            gid, GateOutcome.FAILED, detail="binding carries identity problems",
            problems=[f"{cid}: REFUSED — {p}" for p in txn.binding["problems"]])
    return GateResult(gid, GateOutcome.APPLICABLE_AND_PROVED)


def _cc_gate_ne_steering(txn) -> GateResult:
    """P8 on the deferred route, and the REASON the deferred stage exists.

    This gate adjudicates the steering that SELECTS the source.  It must decide before the loader
    runs; the state machine guarantees that, because the loader is invoked by the TCB on the
    SELECTION_GATED -> OBJECT_BOUND edge and a blocked pre-gate raises out of run_gates first.
    """
    gid = "G_P8_STEERING_PIN"
    cid = txn.cid
    steering = steering_pin_problems(txn.spec, cid, source=_STEERING_SOURCE_PROGRAMMATIC)
    txn.ledger.record_requirement(gid, "central_pin_adjudicated", ran=True)
    if steering:
        return GateResult(gid, GateOutcome.FAILED, detail=steering[0],
                          problems=[f"{cid}: REFUSED — {detail}" for detail in steering])
    return GateResult(gid, GateOutcome.APPLICABLE_AND_PROVED)


def _cc_gate_ne_source_selection(txn) -> GateResult:
    """THE DEFERRED-BINDING GATE.  Declares -- but does not load -- the source.

    Three separate things are proved here, all of them before any content exists:
      1. the selection is a non-empty exact string, so it has a canonical form and can be pinned;
      2. the selection IS the collection id the spec declares -- otherwise a caller could have the
         pre-gates adjudicate the steering of collection A and then load collection B, which is
         precisely the steered-source class this stage exists to close;
      3. the loader's identity is RECOMPUTED from the live callable, so the sink can re-derive the
         source through the same code and refuse if it no longer produces the bound content.
    """
    gid = "G_D_SOURCE_SELECTION"
    cid = txn.cid
    selection = txn.scratch.get("selection")
    loader = txn.scratch.get("loader")
    txn.ledger.record_requirement(gid, "selection_fingerprinted", ran=True)
    if type(selection) is not str or not selection:
        return GateResult(
            gid, GateOutcome.FAILED, detail="malformed source selection",
            problems=[f"{cid}: REFUSED — the source selection must be a non-empty exact string; "
                      "a selection with no canonical form cannot be pinned into the capability"])
    declared = txn.spec.get("source_collection_id") if isinstance(txn.spec, dict) else None
    if selection != declared:
        return GateResult(
            gid, GateOutcome.FAILED, detail="selection does not match the declared source",
            problems=[f"{cid}: REFUSED — the source selection {selection!r} is not the collection "
                      f"id the spec declares ({declared!r}); the gates would have adjudicated one "
                      "source and the loader would have produced another"])
    txn.ledger.record_requirement(gid, "loader_identity_recomputed", ran=True)
    try:
        declare_deferred_source(txn, selection, loader)
    except Refused as exc:
        return GateResult(gid, GateOutcome.FAILED, detail=str(exc),
                          problems=[f"{cid}: REFUSED — {exc}"])
    return GateResult(gid, GateOutcome.APPLICABLE_AND_PROVED)


CC_GATE_PREREGISTRY_NON_ENUMERABLE = {
    "G_D_NE_CONFIG": GateSpec(
        "G_D_NE_CONFIG", _cc_gate_ne_config,
        na_bases=CC_NA_BASES["G_D_NE_CONFIG"],
        requirements=("config_validated", "transit_relation_in_closed_enum")),
    "G_P9_SUPPLIED_IDENTITY": GateSpec(
        "G_P9_SUPPLIED_IDENTITY", _cc_gate_ne_supplied_identity,
        na_bases=CC_NA_BASES["G_P9_SUPPLIED_IDENTITY"],
        requirements=("supplied_identity_scan", "binding_scan")),
    "G_P8_STEERING_PIN": GateSpec(
        "G_P8_STEERING_PIN", _cc_gate_ne_steering,
        na_bases=CC_NA_BASES["G_P8_STEERING_PIN"],
        requirements=("central_pin_adjudicated",)),
    "G_D_SOURCE_SELECTION": GateSpec(
        "G_D_SOURCE_SELECTION", _cc_gate_ne_source_selection,
        requires=("G_D_NE_CONFIG", "G_P8_STEERING_PIN"),
        na_bases=CC_NA_BASES["G_D_SOURCE_SELECTION"],
        requirements=("selection_fingerprinted", "loader_identity_recomputed")),
}


# --- the MAIN phase: gates that need the loaded source --------------------------------------


def _cc_gate_ne_presence(txn) -> GateResult:
    """The single presence gate over the LOADED source, and the point at which the deferred source
    is CONSUMED.  Consumption is not bookkeeping: the sink refuses a capability whose bound source
    was attested and never read, exactly as it refuses a bound-but-unconsumed channel."""
    gid = "G_D_SOURCE_PRESENCE"
    problems: list = []
    source = txn.source.source_consume()
    txn.ledger.record_requirement(gid, "source_consumed", ran=True)
    enforce_positive_presence(txn.spec, source, problems)
    txn.ledger.record_requirement(gid, "presence_enforced", ran=True)
    if problems:
        return GateResult(gid, GateOutcome.FAILED, detail=str(problems[0]),
                          problems=problems, contribution=Contribution.APPEND)
    return GateResult(gid, GateOutcome.APPLICABLE_AND_PROVED)


def _cc_gate_ne_ambient_closure(txn) -> GateResult:
    """P2(2b): the witness is about to run as an independent authority.  If its own code reaches
    state the framework cannot enumerate, no perturbation can exclude an echo.  Refused first."""
    gid = "G_P2_NE_AMBIENT_CLOSURE"
    ambient = p2_ne_witness_closure(txn.spec, txn.cid)
    txn.ledger.record_requirement(gid, "ambient_channels_scanned", ran=True)
    if ambient:
        return GateResult(gid, GateOutcome.FAILED, detail=ambient[0], problems=ambient,
                          contribution=Contribution.APPEND)
    return GateResult(gid, GateOutcome.APPLICABLE_AND_PROVED)


def _cc_gate_ne_invoker(txn) -> GateResult:
    """ONE witness-execution route for the whole layer, built inside the transaction and stored in
    gate-private scratch, so both the MUST_DEPEND limb and the kind verifier reach the witness
    through the same P9-gated invoker and neither can be the 'other path'."""
    gid = "G_P9_NE_INVOKER"
    txn.scratch["invoker"] = _ne_invoker(txn.spec, txn.cid, txn.binding)
    txn.ledger.record_requirement(gid, "invoker_built", ran=True)
    return GateResult(gid, GateOutcome.APPLICABLE_AND_PROVED)


def _cc_gate_ne_must_depend(txn) -> GateResult:
    """Part D MUST_DEPEND: the declared authority is INJECTED and then perturbed; an observation
    that does not move does not consume it.  APPEND, per the banked semantics -- it must not mask
    the presence gate or the kind verifier, and neither may mask it."""
    gid = "G_P2_NE_MUST_DEPEND"
    depends = _p2_ne_must_depend(txn.spec, txn.cid, txn.scratch["invoker"])
    txn.ledger.record_requirement(gid, "must_depend_experiment", ran=True)
    if depends:
        return GateResult(gid, GateOutcome.FAILED, detail=depends[0], problems=depends,
                          contribution=Contribution.APPEND)
    return GateResult(gid, GateOutcome.APPLICABLE_AND_PROVED)


def _cc_gate_ne_payload(txn) -> GateResult:
    """The framework INJECTS the declared authority into the payload the kind verifier runs on, so
    the verifier sees the same witness inputs the MUST_DEPEND experiment perturbed.  A witness that
    read the authority in the experiment and not in production would be the sibling-layer defect.

    FIX 8 — it also computes and STORES the ONE full-content observation (`txn.observed`), exactly
    like the provider route's `txn.observed`.  The invoker is pinned on the injected authority, so
    this call, the guarantee verifier's call and the coverage gate all resolve to this single
    observation; the coverage gate reads `txn.observed` rather than re-invoking.  A witness cannot
    present a covering observation to coverage and a short one to the certificate."""
    gid = "G_P6_NE_PAYLOAD"
    txn.spec = _p2_witness_payload(txn.spec, txn.cid, {
        channel: _P6_CHANNEL_SOURCES[channel]
        for entry in _p2_ne_declared_authority(txn.spec)
        for channel in (_P6_CHANNEL_GROUPS.get(entry) or [entry])
        if channel in _P6_CHANNEL_SOURCES} or None)
    txn.ledger.record_requirement(gid, "authority_injected", ran=True)
    # SINGLE-OBSERVATION BINDING — KIND B ONLY.  Kind B is the only kind that mints a
    # membership-completeness certificate via representation-faithful O⊇A coverage, so it is the
    # only kind that needs its certificate bound to the SAME single observation the coverage gate
    # validates.  For the other kinds the guarantee verifier owns the invocation (and a witness that
    # raises there is that kind's own refusal, not this gate's), so binding here would preempt the
    # correct detector.  Invoke ONCE over the full injected authority (pinned), store it, and require
    # it to be canonicalizable.  FAIL CLOSED — never proceed with a re-runnable split.
    call = txn.scratch.get("invoker")
    _gk = txn.spec.get("guarantee_kind") if isinstance(txn.spec, dict) else None
    if (_gk == "INDEPENDENT_SITE_UNIVERSE" and call is not None
            and isinstance(txn.spec, dict) and "_witness_inputs" in txn.spec):
        try:
            observed = call(txn.spec)
            _p2_canon(observed)
        except NonEnumerableError as exc:
            txn.ledger.record_requirement(gid, "single_observation_bound", ran=True)
            return GateResult(gid, GateOutcome.FAILED, detail=str(exc),
                              problems=[f"{txn.cid}: REFUSED — {exc}"],
                              contribution=Contribution.APPEND)
        except Exception as exc:
            txn.ledger.record_requirement(gid, "single_observation_bound", ran=True)
            return GateResult(
                gid, GateOutcome.FAILED, detail=repr(exc),
                problems=[f"{txn.cid}: REFUSED — P2_NE_OBSERVATION_UNBINDABLE: the certificate-bound "
                          f"observation could not be produced/canonicalized ({type(exc).__name__}); "
                          "a completeness claim whose observation cannot be pinned fails closed"],
                contribution=Contribution.APPEND)
        txn.observed = observed
    txn.ledger.record_requirement(gid, "single_observation_bound", ran=True)
    return GateResult(gid, GateOutcome.APPLICABLE_AND_PROVED)


def _cc_gate_ne_guarantee_kind(txn) -> GateResult:
    """The kind's own verifier.  It derives operands and hands them to the single witness-
    evaluation gate; it may not take a set difference itself (the P4 AST invariant enforces that).
    A witness that raises is a refusal, never a clean verdict."""
    gid = "G_D_GUARANTEE_KIND"
    cid = txn.cid
    problems: list = []
    try:
        _NE_DISPATCH[txn.spec["guarantee_kind"]](
            txn.spec, txn.subject, problems, txn.scratch["invoker"])
    except NonEnumerableError as exc:
        problems.append(f"{cid}: REFUSED — {exc}")
    except Exception as exc:
        problems.append(f"{cid}: witness raised {type(exc).__name__}: {exc} — a witness that "
                        "crashes is not clean")
    txn.ledger.record_requirement(gid, "kind_verifier_ran", ran=True)
    if problems:
        return GateResult(gid, GateOutcome.FAILED, detail=str(problems[0]),
                          problems=problems, contribution=Contribution.APPEND)
    return GateResult(gid, GateOutcome.APPLICABLE_AND_PROVED)


def _cc_gate_ne_coverage(txn) -> GateResult:
    """P2-COVERAGE for the DEFERRED (non-enumerable) route — the SAME faithfulness law the provider
    route enforces, applied to Part D.  (FIX 3 closed kind B's under-reporter; FIX 6 closed the
    certificate/coverage OBSERVATION SPLIT by giving the invoker a per-content memo so the
    certificate call and this gate read ONE observation for identical injected content.)

    SCOPE — KIND B ONLY.  The coverage/faithfulness law is representation-faithful: the observation
    must LITERALLY contain every injected-authority member (O ⊇ A).  That is exactly kind B
    (INDEPENDENT_SITE_UNIVERSE), whose observation IS the discovered universe.  Kind A
    (INDEPENDENT_CONSEQUENCE_RECONCILIATION) produces a DERIVED operand, and perturbation there
    proves only INFLUENCE, not REPRESENTATION — structurally unable to prove membership-completeness
    — so kind A is scoped OUT of membership-completeness entirely (FIX 7: its clean verdict is
    non-authoritative for `is_complete`), and this gate does not adjudicate it.  Kinds C/D/E carry
    their own derived-operand protection.  Coverage is therefore NOT_APPLICABLE for every kind but B.

    Reads the ONE memoised observation over the full injected authority through the same P9-gated
    invoker, and requires O ⊇ A under the closed IDENTITY contract (`_coverage_accounts`).  APPEND,
    like its Part D siblings, so it neither masks nor is masked by the presence gate or the kind
    verifier.
    """
    gid = "G_P2_NE_COVERAGE"
    cid = txn.cid
    spec = txn.spec if isinstance(txn.spec, dict) else {}
    guarantee = spec.get("guarantee_kind")
    if guarantee != "INDEPENDENT_SITE_UNIVERSE":
        txn.ledger.record_requirement(gid, "coverage_accounted", ran=False, state="NOT_APPLICABLE")
        return GateResult(gid, GateOutcome.NOT_APPLICABLE_BY_CLOSED_CONTRACT,
                          rule_id="P2_NE_COVERAGE_NA_NOT_INJECTED_AUTHORITY_KIND")
    declared = _p2_ne_declared_authority(spec)
    channels: dict = {}
    for entry in declared:
        for channel in (_P6_CHANNEL_GROUPS.get(entry) or [entry]):
            if channel in _P6_CHANNEL_SOURCES:
                channels[channel] = _P6_CHANNEL_SOURCES[channel]
    if not channels or txn.observed is None:
        # A coverage kind that injected no resolvable authority (or whose single certificate-bound
        # observation was never stored) cannot be shown to cover anything.  Coverage does NOT defer:
        # it fails closed (CANNOT_PROVE), never a silent skip.
        txn.ledger.record_requirement(gid, "coverage_accounted", ran=True)
        return GateResult(
            gid, GateOutcome.CANNOT_PROVE, rule_id="P2_NE_COVERAGE_NO_INJECTED_AUTHORITY",
            detail="no injected authority or no stored certificate-bound observation",
            problems=[f"{cid}: REFUSED — P2_NE_COVERAGE_NO_INJECTED_AUTHORITY: a coverage-bearing "
                      "guarantee kind injected no resolvable authority or produced no stored "
                      "certificate-bound observation, so the observation cannot be shown to account "
                      "for the authority; coverage fails closed rather than skipping"],
            contribution=Contribution.APPEND)
    try:
        authority: set = set()
        for content in channels.values():
            authority |= _p2_member_strings(content)
        # ONE observation: read the SAME `txn.observed` the payload gate stored and the guarantee
        # verifier mints the certificate over (FIX 8 structural single-observation discipline) —
        # coverage does NOT re-invoke the witness for the full-content observation.
        observed = _p2_member_strings(txn.observed)
    except Exception as exc:  # a coverage question we cannot compute is CANNOT_PROVE, not clean
        txn.ledger.record_requirement(gid, "coverage_accounted", ran=True)
        return GateResult(
            gid, GateOutcome.CANNOT_PROVE, rule_id="P2_NE_COVERAGE_UNCOMPUTABLE", detail=repr(exc),
            problems=[f"{cid}: REFUSED — P2_NE_COVERAGE_UNCOMPUTABLE: the injected authority or the "
                      f"witness observation could not be reduced to a membership view "
                      f"({type(exc).__name__}); coverage cannot be proven, so the completeness "
                      "claim fails closed"],
            contribution=Contribution.APPEND)
    txn.ledger.record_requirement(gid, "coverage_accounted", ran=True)
    ok, uncovered, _basis = _coverage_accounts(authority, observed)
    if not ok:
        return GateResult(
            gid, GateOutcome.FAILED, detail=f"observation under-reports authority: {sorted(uncovered)}",
            problems=[f"{cid}: REFUSED — P2_NE_COVERAGE_UNDER_REPORT: the witness observation does "
                      "not ACCOUNT FOR every member of the framework-injected authority: "
                      f"{sorted(uncovered)} present in the injected authority is absent from the "
                      "discovered universe under IDENTITY accounting (O must be a SUPERSET of the "
                      "injected authority). DROPPING a required member is under-reporting — it "
                      "certifies an INCOMPLETE non-enumerable collection — and is REFUSED"],
            contribution=Contribution.APPEND)
    return GateResult(gid, GateOutcome.APPLICABLE_AND_PROVED)


# REGISTRY ORDER is EXECUTION order and reproduces the banked body exactly (presence, ambient,
# invoker, must_depend, payload injection, kind verifier).  RENDER order is declared separately,
# because the banked body computed MUST_DEPEND before the kind verifier but reported it after --
# an ordering that used to be implicit in where a local variable was concatenated.
CC_GATE_REGISTRY_NON_ENUMERABLE = {
    "G_D_SOURCE_PRESENCE": GateSpec(
        "G_D_SOURCE_PRESENCE", _cc_gate_ne_presence,
        na_bases=CC_NA_BASES["G_D_SOURCE_PRESENCE"],
        requirements=("source_consumed", "presence_enforced"),
        contribution=Contribution.APPEND, render_order=10),
    "G_P2_NE_AMBIENT_CLOSURE": GateSpec(
        "G_P2_NE_AMBIENT_CLOSURE", _cc_gate_ne_ambient_closure,
        na_bases=CC_NA_BASES["G_P2_NE_AMBIENT_CLOSURE"],
        requirements=("ambient_channels_scanned",),
        contribution=Contribution.APPEND, render_order=15),
    "G_P9_NE_INVOKER": GateSpec(
        "G_P9_NE_INVOKER", _cc_gate_ne_invoker,
        requires=("G_P2_NE_AMBIENT_CLOSURE",),
        na_bases=CC_NA_BASES["G_P9_NE_INVOKER"],
        requirements=("invoker_built",),
        contribution=Contribution.APPEND, render_order=16),
    "G_P2_NE_MUST_DEPEND": GateSpec(
        "G_P2_NE_MUST_DEPEND", _cc_gate_ne_must_depend,
        requires=("G_P9_NE_INVOKER",),
        na_bases=CC_NA_BASES["G_P2_NE_MUST_DEPEND"],
        requirements=("must_depend_experiment",),
        contribution=Contribution.APPEND, render_order=30),
    "G_P6_NE_PAYLOAD": GateSpec(
        "G_P6_NE_PAYLOAD", _cc_gate_ne_payload,
        requires=("G_P9_NE_INVOKER",),
        na_bases=CC_NA_BASES["G_P6_NE_PAYLOAD"],
        requirements=("authority_injected", "single_observation_bound"),
        contribution=Contribution.APPEND, render_order=17),
    "G_D_GUARANTEE_KIND": GateSpec(
        "G_D_GUARANTEE_KIND", _cc_gate_ne_guarantee_kind,
        requires=("G_P9_NE_INVOKER", "G_P6_NE_PAYLOAD"),
        na_bases=CC_NA_BASES["G_D_GUARANTEE_KIND"],
        requirements=("kind_verifier_ran",),
        contribution=Contribution.APPEND, render_order=20),
    # FIX 3 — Part D coverage/faithfulness.  REQUIRED.  FIX 8: reads the SINGLE certificate-bound
    # observation the payload gate stored (`txn.observed`), so it requires G_P6_NE_PAYLOAD.  APPEND
    # so it neither masks nor is masked by the presence gate or the kind verifier.
    "G_P2_NE_COVERAGE": GateSpec(
        "G_P2_NE_COVERAGE", _cc_gate_ne_coverage,
        requires=("G_P9_NE_INVOKER", "G_P6_NE_PAYLOAD"),
        na_bases=CC_NA_BASES["G_P2_NE_COVERAGE"],
        requirements=("coverage_accounted",),
        contribution=Contribution.APPEND, render_order=25),
}# ============================================================================================
# cc_adapters -- THE FOUR SEALING ENTRY POINTS (blueprint §3, phase 4)
#
# The legacy contract is "returns a problems list; [] means clean and non-vacuous".  That contract
# is preserved ONLY as a RENDERING of a certificate.  Four rules make this an adapter and not a
# second authority path:
#
#   R1  `[]` is emitted at exactly ONE place, and only after certified_for() returns True on the
#       claim the caller actually asked about.  This is the direct successor of _sealed /
#       NO_GATE_TRANSIT, and it is why P4's contribution to issuance is "nothing": the backstop IS
#       P4, now enforced by capability instead of by a transit list.
#   R2  Adapters compute no verdicts.  An adapter may translate shapes and nothing else.
#   R3  Adapters never construct authority: no adapter touches _ISSUER_SENTINEL,
#       _ISSUER.capabilities or CertifiedResult.
#   R4  Refusal rendering is TOTAL.  Totality is guaranteed inside cc_core.run_gates (which
#       synthesises a problem when a blocked gate set rendered nothing), not here -- an adapter
#       that could invent a problem could also invent its absence.
# ============================================================================================


class CcRequestRefused(ClosedContractRefusal):
    """The closed request could not be built from the legacy call shape."""


def _cc_steering_payload(spec) -> dict:
    """The normalised steering payload bound into the capability's steering digest.  Restricted to
    the CLOSED declared steering domain, so a spec cannot widen what is attested by adding keys."""
    if not isinstance(spec, dict):
        return {}
    return {k: spec[k] for k in sorted(CC_DECLARED_STEERING_KEYS) if k in spec}


def _cc_validate_request(txn, request) -> None:
    """S0 -> S1.  The request key set is CLOSED and exact."""
    if type(request) is not dict:
        raise CcRequestRefused("request must be an exact dict")
    keys = set(request)
    if keys != CC_REQUEST_KEYS:
        raise CcRequestRefused(
            f"request key set is closed; extra={sorted(keys - CC_REQUEST_KEYS)} "
            f"missing={sorted(CC_REQUEST_KEYS - keys)}")
    if request["route"] not in CC_ROUTES:
        raise CcRequestRefused(f"route {request['route']!r} is outside the closed route set")
    # The route decides IMMEDIATE vs DEFERRED object binding, and the TCB decides that from its own
    # route set.  Declared here, at RAW_REQUEST, so the binding model is fixed before the first
    # transition and no later stage can flip it.
    declare_route(txn, request["route"])

    relation = request["relation"]
    if not isinstance(relation, str) or not relation:
        raise CcRequestRefused("relation must be a non-empty string")
    txn.relation = relation

    steering = request["steering"]
    if type(steering) is not dict:
        raise CcRequestRefused("steering must be an exact dict")
    undeclared = sorted(set(steering) - CC_DECLARED_STEERING_KEYS)
    if undeclared:
        raise CcRequestRefused(
            f"undeclared steering keys {undeclared}; the steering domain is closed")
    txn.steering = SteeringEnvelope(_ISSUER_SENTINEL, steering)

    channels = request["channels"]
    if type(channels) is not dict:
        raise CcRequestRefused("channels must be an exact dict")
    for name in channels:
        if name not in TRUSTED_PRODUCERS:
            raise CcRequestRefused(
                f"channel {name!r} has no trusted producer; the channel set is closed")

    txn.spec = request["spec"]
    txn.cid = request["cid"]
    txn.collection = request["object"]
    txn.binding = request["binding"]
    txn.request = request
    txn.advance(State.RAW_REQUEST, State.CONTRACT_VALIDATED)


def _cc_bind_object(txn) -> None:
    txn.subject = txn.request["object"]
    txn.subject_fp = fingerprint(txn.subject)     # the TCB computes it; never supplied
    txn.advance(State.CONTRACT_VALIDATED, State.OBJECT_BOUND)


def _cc_bind_content(txn) -> None:
    for name, decl in txn.request["channels"].items():
        producer = TRUSTED_PRODUCERS[name]
        trusted_content = producer()
        txn.channels[name] = ChannelBinding(
            _ISSUER_SENTINEL, name, producer.__name__, trusted_content,
            decl.get("transform_chain", ()))
    txn.advance(State.OBJECT_BOUND, State.CONTENT_BOUND)


def _cc_evaluate(request, registry, *, probe: bool = False):
    """S0 -> S5.  The ONLY path to a capability.  Not reachable from an adapter except through
    the four entry points below, and it returns a capability, never a verdict."""
    txn = _Transaction(probe=probe)
    _cc_validate_request(txn, request)
    _cc_bind_object(txn)
    _cc_bind_content(txn)
    run_gates(txn, registry)
    return _issue_capability(txn, registry), txn


def _cc_render(exc) -> list:
    """Translate a refusal into the legacy problems-list shape.  TRANSLATION ONLY."""
    if isinstance(exc, GateRefusal):
        return list(exc.problems)
    return [_problem("?", "CC_CLOSED_CONTRACT_REFUSED", str(exc))]


# --- the ONE place a clean verdict is emitted ------------------------------------------------
_CC_LAST_ACTIVATION: dict = {}


def _cc_seal(request, registry, *, probe: bool = False) -> list:
    """R1: the AUTHORITATIVE clean verdict is emitted here and nowhere else, and only after
    certified_for() has agreed that the certificate covers the claim the caller asked about.

    The value is bound, in issuer state, to the certificate: an empty list is a value any
    function can author, so emptiness alone can no longer assert completeness."""
    try:
        cap, txn = _cc_evaluate(request, registry, probe=probe)
    except Refused as exc:
        _CC_LAST_ACTIVATION.clear()
        return _cc_render(exc)

    result = certify_result(cap, request["object"])              # THE sink
    if not certified_for(result, request["object"], request["relation"]):
        return [_problem(request["relation"], "NO_CAPABILITY",
                         f"{request['cid']}: the evaluation produced no certificate covering this "
                         "claim; a verdict without a capability is exactly the ungated-clean-"
                         "verdict class P4 closes; REFUSED")]
    _CC_LAST_ACTIVATION.clear()
    _CC_LAST_ACTIVATION.update(result.activation)
    return _mint_clean_view(result)


def cc_last_activation() -> dict:
    """§21 DOUBLE-PROOF instrument.  Reports, for the most recent certified evaluation, which
    gates were APPLICABLE and PROVED.  NON-CERTIFYING: it returns a report, never a verdict."""
    return dict(_CC_LAST_ACTIVATION)


# ============================================================================================
# ADAPTER 1 -- verify_provider
# ============================================================================================


def verify_provider(spec: dict, collection: Any, cid: str = "<collection>", *,
                    binding: Optional[dict] = None) -> list:
    """Verify a provenance/schema/harness/semantic collection THROUGH the closed model.

    Every P-property that used to be a position in _verify_provider_body is now a row in
    CC_GATE_REGISTRY_PROVIDER.  This function translates the legacy call shape into a closed
    request, and translates a refusal back into the legacy problems list.  It decides nothing.
    """
    relation = spec.get("relation") if isinstance(spec, dict) else None
    if not relation:
        # Not a verdict: the closed request cannot be BUILT without a relation, because relation
        # is one of its required keys.  This is contract validation, not adjudication.
        return [_problem("?", "NO_RELATION",
                         f"{cid}: provider spec declares no relation; REFUSED")]
    request = {
        "object": collection,
        "relation": relation,
        "steering": _cc_steering_payload(spec),
        "channels": {},
        "spec": spec,
        "cid": cid,
        "binding": binding,
        "route": "verify_provider",
    }
    return _cc_seal(request, CC_GATE_REGISTRY_PROVIDER)


# ============================================================================================
# ADAPTER 2 -- compare
#
# The adapter to watch (blueprint R5): 283 call sites, a bare relation string and two raw
# operands with no spec, so it has the THINNEST binding surface and is the most likely place for
# an accidental hollow pass.  Its NA vocabulary is therefore the most tightly reviewed.
# ============================================================================================


def _cc_gate_compare_core(txn) -> GateResult:
    """The banked single witness-evaluation gate, unchanged: relation resolution, the P3 relation-
    form schema gate, probe totality, the normalizer, the duplicate-collision detector, the
    presence gate and the transit ledger."""
    gid = "G_P5_COMPARATOR_VERDICT"
    problems = _cc_legacy_compare(
        txn.relation, txn.scratch["expected_domain"], txn.collection, txn.spec,
        _depth=txn.scratch["_depth"], _path=txn.scratch["_path"],
        _condition=txn.scratch["_condition"])
    txn.ledger.record_requirement(gid, "comparator_ran", ran=True)
    if problems:
        return GateResult(gid, GateOutcome.FAILED, detail=problems[0]["detail"],
                          problems=problems)
    return GateResult(gid, GateOutcome.APPLICABLE_AND_PROVED)


def _cc_gate_compare_relation_closed(txn) -> GateResult:
    """P5's closed-relation limb on the compare route: the relation must be in the closed enum and
    any parent->child nesting the spec asks for must be a REVIEWED pair."""
    gid = "G_P5_RELATION_COMPARATOR"
    canonical = resolve_relation(txn.relation)
    txn.ledger.record_requirement(gid, "relation_in_closed_enum", ran=True)
    if canonical is None:
        return GateResult(
            gid, GateOutcome.FAILED, detail=f"unknown relation {txn.relation!r}",
            problems=[_problem(txn.relation, "UNKNOWN_RELATION",
                               f"relation {txn.relation!r} is not implemented and has no alias; "
                               "an unknown relation cannot pass -> REFUSED")])
    spec = txn.spec if isinstance(txn.spec, dict) else {}
    for field in ("key_relation", "value_relation"):
        child = spec.get(field)
        if child is None:
            continue
        refusal = cc_nesting_refusal(canonical, child)
        if refusal is not None:
            txn.ledger.record_requirement(gid, "nesting_pair_reviewed", ran=True)
            return GateResult(gid, GateOutcome.FAILED, detail=refusal["detail"],
                              problems=[refusal])
    txn.ledger.record_requirement(gid, "nesting_pair_reviewed", ran=True)
    return GateResult(gid, GateOutcome.APPLICABLE_AND_PROVED)


def _cc_gate_compare_no_witness(txn) -> GateResult:
    """P7/P9 applicability on the compare route.

    A bare compare() carries two operands and a relation.  If the spec names a `provider` or a
    witness field then compare() is NOT the right entry and the closed contract says so: the
    request is refused rather than adjudicated by an entry that has no witness machinery.  Only a
    spec that genuinely carries NO witness reaches the reviewed NA basis, so this cannot become a
    hollow pass by omission."""
    gid = "G_P7_WITNESS_ADEQUACY"
    spec = txn.spec if isinstance(txn.spec, dict) else {}
    carries = sorted(f for f in ("provider", "observed", "reads",
                                 "independent_observed_source_or_witness") if f in spec)
    txn.ledger.record_requirement(gid, "witness_form_resolved", ran=True)
    if carries:
        return GateResult(
            gid, GateOutcome.FAILED, detail=f"witness-bearing fields on the compare route: {carries}",
            problems=[_problem(txn.relation, "WITNESS_ON_COMPARE_ROUTE",
                               f"the spec carries witness-bearing field(s) {carries} but was "
                               "submitted through compare(), which has no witness-resolution or "
                               "identity machinery; use verify_provider(). REFUSED rather than "
                               "adjudicated by an entry that cannot see the witness")])
    return GateResult(gid, GateOutcome.NOT_APPLICABLE_BY_CLOSED_CONTRACT,
                      rule_id="P7_NA_NO_WITNESS_FORM_DECLARED")


def _cc_gate_compare_no_provider(txn) -> GateResult:
    gid = "G_P9_WITNESS_IDENTITY"
    txn.ledger.record_requirement(gid, "identity_recomputed", ran=False, state="NOT_APPLICABLE")
    return GateResult(gid, GateOutcome.NOT_APPLICABLE_BY_CLOSED_CONTRACT,
                      rule_id="P9_NA_INLINE_OBSERVED_NO_CALLABLE")


def _cc_gate_compare_no_channels(txn) -> GateResult:
    gid = "G_P6_CHANNEL_DECLARATION"
    txn.ledger.record_requirement(gid, "channel_declaration", ran=False, state="NOT_APPLICABLE")
    return GateResult(gid, GateOutcome.NOT_APPLICABLE_BY_CLOSED_CONTRACT,
                      rule_id="P6_NA_NO_DECLARED_CHANNELS")


def _cc_gate_compare_steering(txn) -> GateResult:
    """P8 on the compare route.  The basis is COMPUTED, not assumed: if the spec carries a key in
    the centrally-adjudicated steering domain then the pin adjudicator RUNS."""
    gid = "G_P8_STEERING_PIN"
    spec = txn.spec if isinstance(txn.spec, dict) else {}
    surface = sorted(set(spec) & CC_DECLARED_STEERING_KEYS)
    if not surface:
        txn.ledger.record_requirement(gid, "central_pin_adjudicated", ran=False,
                                      state="NOT_APPLICABLE")
        return GateResult(gid, GateOutcome.NOT_APPLICABLE_BY_CLOSED_CONTRACT,
                          rule_id="P8_NA_NO_STEERING_SURFACE")
    steering = steering_pin_problems(spec, txn.cid, source=_STEERING_SOURCE_PROGRAMMATIC)
    txn.ledger.record_requirement(gid, "central_pin_adjudicated", ran=True)
    if steering:
        return GateResult(gid, GateOutcome.FAILED, detail=steering[0],
                          problems=[_problem(txn.relation, "STEERING_UNPINNED", d)
                                    for d in steering])
    return GateResult(gid, GateOutcome.APPLICABLE_AND_PROVED)


CC_GATE_REGISTRY_COMPARE = {
    "G_P5_RELATION_COMPARATOR": GateSpec(
        "G_P5_RELATION_COMPARATOR", _cc_gate_compare_relation_closed,
        na_bases=CC_NA_BASES["G_P5_RELATION_COMPARATOR"],
        requirements=("relation_in_closed_enum", "nesting_pair_reviewed")),
    "G_P6_CHANNEL_DECLARATION": GateSpec(
        "G_P6_CHANNEL_DECLARATION", _cc_gate_compare_no_channels,
        na_bases=CC_NA_BASES["G_P6_CHANNEL_DECLARATION"]),
    "G_P8_STEERING_PIN": GateSpec(
        "G_P8_STEERING_PIN", _cc_gate_compare_steering,
        na_bases=CC_NA_BASES["G_P8_STEERING_PIN"],
        requirements=("central_pin_adjudicated",)),
    "G_P7_WITNESS_ADEQUACY": GateSpec(
        "G_P7_WITNESS_ADEQUACY", _cc_gate_compare_no_witness,
        na_bases=CC_NA_BASES["G_P7_WITNESS_ADEQUACY"],
        requirements=("witness_form_resolved",)),
    "G_P9_WITNESS_IDENTITY": GateSpec(
        "G_P9_WITNESS_IDENTITY", _cc_gate_compare_no_provider,
        na_bases=CC_NA_BASES["G_P9_WITNESS_IDENTITY"]),
    "G_P5_COMPARATOR_VERDICT": GateSpec(
        "G_P5_COMPARATOR_VERDICT", _cc_gate_compare_core,
        requires=("G_P5_RELATION_COMPARATOR",),
        requirements=("comparator_ran",)),
}


def compare(relation: str, expected_domain: Any, collection: Any,
            spec: Optional[dict] = None, *, _depth: int = 0, _path: str = "compare",
            _condition: str = "EMPTY_LOAD_BEARING_OPERAND_LEGITIMATE") -> list:
    """Public comparator entry, THROUGH the closed model.

    NESTED CALLS.  `_reenter()` and the lenient-branch auditors call compare() from INSIDE an
    already-open evaluation -- they are sub-relations of a verdict whose capability is already
    being earned by the enclosing transaction.  Opening a second transaction there would mint a
    second capability for a fragment of one claim, which is precisely the authority-laundering
    shape the closed model exists to remove.  Those calls therefore delegate straight to the
    banked gate, inside the caller's scope, and the ENCLOSING transaction remains the only minter.
    """
    if _depth or _EVAL_SCOPES:
        return _witness_evaluation_gate(relation, expected_domain, collection, spec,
                                        _depth=_depth, _path=_path, _condition=_condition)
    request = {
        "object": collection,
        "relation": relation,
        "steering": _cc_steering_payload(spec),
        "channels": {},
        "spec": spec,
        "cid": _path,
        "binding": None,
        "route": "compare",
    }

    def _prepare(txn):
        txn.scratch["expected_domain"] = expected_domain
        txn.scratch["_depth"] = _depth
        txn.scratch["_path"] = _path
        txn.scratch["_condition"] = _condition

    out = _cc_seal_prepared(request, CC_GATE_REGISTRY_COMPARE, _prepare)
    # A9-12/A9-13 PUBLIC-COMPOSITE CLOSURE.  compare() is a RAW two-operand comparator with NO
    # witness machinery: on the compare route P6/P7/P9 are NOT_APPLICABLE, so its `expected_domain`
    # is a CALLER-SUPPLIED operand that never transited the authority-injection / independence /
    # coverage gates.  A clean compare() verdict is therefore a statement about the two operands the
    # caller handed in, NOT an independent-witness COMPLETENESS proof of the collection.  The public
    # composite (resolve_witness_fields -> p9_execute_witness -> compare) exploited exactly that: it
    # assembled an operand outside any governed transit and had compare() MINT completeness authority
    # over it.  So a top-level compare() is COMPUTATION-ONLY here: it still returns the same problems
    # list ([] when the raw comparison holds, still `== []`, still falsy, so the 283 legacy consumer
    # sites and the P5 matrix are untouched), but that empty list is DELIBERATELY NOT bound to a
    # certificate.  `is_complete`/`_clean_view_certificate` therefore read False on it, so no public
    # composition of raw comparisons can present itself as a sink-minted completeness certificate.
    # The authoritative clean view survives ONLY on the witness-bearing entry points (evaluate /
    # verify_provider / verify_non_enumerable) and on the NESTED-scope delegate above, which earn it
    # through the full P1-P9 transit over an object the caller cannot pre-compute.
    #
    # MECHANISM.  `_cc_seal_prepared` is left untouched (it is a shared seal and P4's INV that a
    # clean verdict has exactly one origin per seal must keep holding).  Instead the RAW COMPARATOR
    # hands back a FRESH copy of the verdict list.  On the clean path `out` is the seal's registered
    # authoritative view; `list(out)` is an ordinary empty list the issuer never recorded, so it is
    # still `== []` and falsy for every legacy consumer while `_clean_view_certificate` reads None on
    # it.  On a refusal the copy carries the same problems.  This is not a second clean origin: the
    # authority still originates only at the seal; the comparator merely declines to FORWARD it,
    # because a caller-supplied operand pair is not a completeness witness.
    return list(out)


# ============================================================================================
# ADAPTER 3 -- verify_non_enumerable  (the DEFERRED-BINDING route, §17-D)
#
# The one route whose SUBJECT does not exist at request time.  Everything the closed model says
# about an immediate route still holds; what changes is WHERE the object comes from and WHEN.
# ============================================================================================


def _cc_render_strings(exc, cid) -> list:
    """Translate a refusal into Part D's STRING problems list.  TRANSLATION ONLY.

    The strings the gates produced pass through verbatim -- an adapter that edited them would be
    reinterpreting a refusal.  A DICT can only come from the TCB's own totality synthesis (the
    fail-closed problem run_gates raises when a blocked gate set rendered nothing), and it is
    stringified rather than dropped, because a refusal that renders to nothing is a fail-open.
    """
    if isinstance(exc, GateRefusal):
        return [p if isinstance(p, str) else _stringify(p, cid) for p in exc.problems]
    if isinstance(exc, DeferredSourceRefusal):
        return [f"{cid}: {exc}"]
    return [f"{cid}: REFUSED — {exc}"]


def _cc_seal_deferred(request, pregates, gates, prepare) -> list:
    """R1 for the deferred route: `[]` is emitted here and nowhere else, and only after
    certified_for_selection() has agreed the certificate covers the SELECTION the caller named.

    The three-stage middle is the whole extension:
        S1  CONTRACT_VALIDATED   --run_gates(pregates, SELECTION phase)-->  S1D SELECTION_GATED
        S1D SELECTION_GATED      --bind_deferred_object (the TCB runs the loader)-->  S2
        S2  OBJECT_BOUND         --(unchanged from here on)-->  S3 S4 S5 S6 S7
    """
    registry = {**pregates, **gates}
    cid = request["cid"]
    txn = _Transaction(probe=False)
    try:
        _cc_validate_request(txn, request)
        prepare(txn)
        run_gates(txn, pregates, GATE_PHASE_SELECTION)
        bind_deferred_object(txn)
        _cc_bind_content(txn)
        run_gates(txn, gates, GATE_PHASE_MAIN)
        cap = _issue_capability(txn, registry)
        # THE SINK IS INSIDE THE TRY, and that is a defect layer this migration closed.
        # On the deferred route the sink does real work at certification time -- it re-derives the
        # source through the caller's own loader -- so a sink refusal is not exotic here, it is
        # the ordinary way a swapped source is caught.  Left outside, that refusal escaped
        # verify_non_enumerable as an EXCEPTION, which breaks the layer's documented never-raise
        # contract and leaves the verdict to whatever blanket `except` the caller happens to have
        # (the FW-FIND-B shape).  Executed as arm D03 of cc_deferred_attacks.py, which scored
        # CANNOT-CERTIFY until this line moved.
        result = certify_deferred(cap)                              # THE sink
    except Refused as exc:
        _CC_LAST_ACTIVATION.clear()
        return _cc_render_strings(exc, cid)
    if not certified_for_selection(result, txn.scratch["selection"], txn.scratch["loader"],
                                   request["relation"]):
        return [f"{cid}: REFUSED — the evaluation produced no certificate covering this source "
                "selection; a verdict without a capability is exactly the ungated-clean-verdict "
                "class P4 closes"]
    _CC_LAST_ACTIVATION.clear()
    _CC_LAST_ACTIVATION.update(result.activation)
    return _mint_clean_view(result)


def verify_non_enumerable(spec: dict,
                          source_loader: Optional[Callable[[str], set]] = None,
                          *,
                          dependency_resolver: Optional[Callable[[str], bool]] = None,
                          binding: Optional[dict] = None) -> list:
    """Entry point for a non-enumerable-authority collection, THROUGH the closed model.

    Returns a problems list of STRINGS (merge-compatible with collection_completeness).  A
    refusal, an unrunnable witness, or a witness that raises/returns garbage becomes a REFUSED
    problem -- fail-closed, never a silent pass.  It decides nothing: every stage of the banked
    body is now a row in CC_GATE_PREREGISTRY_NON_ENUMERABLE or CC_GATE_REGISTRY_NON_ENUMERABLE.

    The evaluation scope is retained (P4 INV-8) because Part D's kind verifiers reach the single
    witness-evaluation gate through compare(), and a compare() inside an open scope must delegate
    to the banked gate rather than mint a second capability for a fragment of one claim.
    """
    cid = spec.get("source_collection_id", "<unknown>") if isinstance(spec, dict) else "<spec>"
    kind = spec.get("guarantee_kind") if isinstance(spec, dict) else None
    # The relation the capability is issued under is the one the KIND's declared gate transit
    # names.  A kind with no declared transit gets a placeholder that is not in the closed
    # comparator enum, so G_D_NE_CONFIG refuses it twice over (no transit, unknown relation)
    # rather than the request being unbuildable.
    transit = _NE_GATE_TRANSITS.get(kind) if isinstance(kind, str) else None
    relation = transit[0] if transit else "UNRESOLVED_NON_ENUMERABLE_TRANSIT"
    request = {
        "object": None,        # DEFERRED: bound by the TCB after the pre-gates, never supplied
        "relation": relation,
        "steering": _cc_steering_payload(spec),
        "channels": {},
        "spec": spec,
        "cid": cid,
        "binding": binding,
        "route": "verify_non_enumerable",
    }

    def _prepare(txn):
        txn.scratch["selection"] = cid
        txn.scratch["loader"] = source_loader or _default_source_loader
        txn.scratch["dependency_resolver"] = dependency_resolver

    ev = _evaluation("verify_non_enumerable", cid)
    with ev:
        out = _sealed_strings(ev, _cc_seal_deferred(
            request, CC_GATE_PREREGISTRY_NON_ENUMERABLE, CC_GATE_REGISTRY_NON_ENUMERABLE,
            _prepare))
    # FIX 7 — KIND A IS INFLUENCE-ONLY, NOT MEMBERSHIP-COMPLETENESS.
    #
    # INDEPENDENT_CONSEQUENCE_RECONCILIATION certifies that the witness's DERIVED consequences
    # reconcile with the source.  Its per-member perturbation could only ever prove INFLUENCE
    # (removing an injected-authority member changes the consequences), never REPRESENTATION (that
    # member appears in the certificate-bound observation) — and membership-completeness REQUIRES
    # representation.  Because consequences are a derived operand, a literal O⊇A representation check
    # would false-refuse legitimate derivation, so perturbation is structurally unable to prove
    # membership-completeness for kind A.  Kind A has ZERO production callers.  The closed
    # determination is therefore: a kind-A clean verdict may still assert its OWN property
    # (reconciliation), but it MUST NOT be readable as a membership-completeness certificate.  So on
    # the clean path it is handed back as a NON-authoritative view — still `== []` and falsy, so the
    # reconciliation verdict survives, but `is_complete`/`_clean_view_certificate` read False on it.
    # MEMBERSHIP-COMPLETENESS certificates now mint ONLY on the two literal-representation routes —
    # PROVIDER and kind B — both of which prove representation-faithful O⊇A coverage over the SINGLE
    # certificate-bound observation (FIX 6).  Kinds C/D/E are already non-completeness by their own
    # derived semantics.  Kind B is untouched: the strip is keyed on the kind, so the shared seal
    # still mints kind B's authoritative certificate.
    if kind == "INDEPENDENT_CONSEQUENCE_RECONCILIATION" and _is_authoritative_clean(out):
        return list(out)
    return out


def _cc_seal_prepared(request, registry, prepare) -> list:
    """As _cc_seal, but runs a PREPARE hook that seeds gate-private transaction scratch before the
    gates run.  The hook may only write txn.scratch; it cannot reach issuer state."""
    txn = _Transaction(probe=False)
    try:
        _cc_validate_request(txn, request)
        prepare(txn)
        _cc_bind_object(txn)
        _cc_bind_content(txn)
        run_gates(txn, registry)
        cap = _issue_capability(txn, registry)
    except Refused as exc:
        _CC_LAST_ACTIVATION.clear()
        return _cc_render(exc)
    result = certify_result(cap, request["object"])
    if not certified_for(result, request["object"], request["relation"]):
        return [_problem(request["relation"], "NO_CAPABILITY",
                         f"{request['cid']}: the evaluation produced no certificate covering this "
                         "claim; REFUSED")]
    _CC_LAST_ACTIVATION.clear()
    _CC_LAST_ACTIVATION.update(result.activation)
    return _mint_clean_view(result)
# =============================================================================================
# GOLDEN CONSUMER BOOTSTRAP (Gate 4N-I28BH-B0a, §17) — reviewer_retrieval_state.py::STATES
# ---------------------------------------------------------------------------------------------
# Registration, NOT TCB logic. At import — after the WITNESS_PROVIDER_MANIFEST literal, the
# registries and the register_* entry points above are all defined — this binds the ONE reviewed
# provider and its INDEPENDENT P6 authority channel for the reviewer-retrieval state universe.
# register_provider recomputes the witness identity from the live object, the module's bytes on
# disk and the pinned manifest entry; _register_p6_channel_producer EXECUTES the authority
# producer here and pins the channel's content — so neither callable can be substituted after
# review. Adding a consumer is a data/registration diff; the framework's signed properties are
# pin-content independent.
# =============================================================================================
import completeness_providers as _golden_providers

register_provider("reviewer_states_provider", _golden_providers.states_witness)
_register_p6_channel_producer(
    "reviewer_retrieval_state.state_universe",
    _golden_providers.states_authority,
    source_id="reviewer_retrieval_state.transition_graph",
    rationale="state universe derived from the TRANSITIONS graph, independent of the STATES tuple")


# =============================================================================================
# WAVE 2 CONSUMER BOOTSTRAP (Gate 4N-I28BH-B0a, §34 wave2) — three more certificate-backed
# collections. Registration, NOT TCB logic: each binds one reviewed witness and its INDEPENDENT
# P6 authority channel, whose producer the framework EXECUTES and content-pins here. The provider
# module is already imported above as `_golden_providers`.
# =============================================================================================
register_provider("production_states_provider", _golden_providers.production_states_witness)
_register_p6_channel_producer(
    "production_certification.state_universe",
    _golden_providers.production_states_authority,
    source_id="production_certification.required_flag_policy",
    rationale="state universe derived from REQUIRED_FLAG's keys (the flag policy), independent of "
              "the STATES tuple")

register_provider("authorization_fields_provider", _golden_providers.authorization_fields_witness)
_register_p6_channel_producer(
    "production_certification.authorization_field_domain",
    _golden_providers.authorization_fields_authority,
    source_id="external-authorization-contract.required_fields",
    rationale="authorization-field domain derived from the independently authored external "
              "authorization contract fixture, independent of the VALIDATED_AUTHORIZATION_FIELDS "
              "tuple")

register_provider("never_relaunch_provider", _golden_providers.never_relaunch_witness)
_register_p6_channel_producer(
    "reviewer_retrieval_state.never_relaunch_set",
    _golden_providers.never_relaunch_authority,
    source_id="reviewer_retrieval_state.transition_graph_structure",
    rationale="never-relaunch set derived structurally from the TRANSITIONS graph (root with no "
              "inbound edge + sink with no outbound edge), independent of the NEVER_RELAUNCH tuple")


# =============================================================================================
# WAVE 3 CONSUMER BOOTSTRAP (Gate 4N-I28BH-B0a, §34 wave3) — three more certificate-backed
# collections. Registration, NOT TCB logic: each binds one reviewed witness and its INDEPENDENT
# P6 authority channel, whose producer the framework EXECUTES and content-pins here. The provider
# module is already imported above as `_golden_providers`.
# =============================================================================================
register_provider("date_operators_provider", _golden_providers.date_operators_witness)
_register_p6_channel_producer(
    "iam_eval.date_operator_domain",
    _golden_providers.date_operators_authority,
    source_id="iam_eval.supported_semantics_condition_operators",
    rationale="Date-operator set derived from SUPPORTED_SEMANTICS['condition_operators'] (the "
              "modelled-operator table), independent of the DATE_OPERATORS dict")

register_provider("reader_role_provider", _golden_providers.reader_role_witness)
_register_p6_channel_producer(
    "trust_policies.revision_reader_role_domain",
    _golden_providers.reader_role_authority,
    source_id="signalnest_identity.revision_reader_role_names",
    rationale="revision-reader role set derived from signalnest_identity.REVISION_READER_ROLE_NAMES "
              "(a different module), independent of the ROLE_TRUST keys")

register_provider("assurance_roles_provider", _golden_providers.assurance_roles_witness)
_register_p6_channel_producer(
    "workflow_graph_validator.assurance_role_domain",
    _golden_providers.assurance_roles_authority,
    source_id="workflow_graph_validator.assurance_role_by_mode_values",
    rationale="assurance-role set derived from _ASSURANCE_ROLE_BY_MODE's values (the mode->role "
              "map), independent of the _ASSURANCE_ROLES tuple")


# =============================================================================================
# WAVE 4 CONSUMER BOOTSTRAP (Gate 4N-I28BH-B0a, §34 wave4) — three more certificate-backed
# collections (required-field constants vs a pure builder's emitted keyset). Registration, NOT TCB
# logic: each binds one reviewed witness and its INDEPENDENT P6 authority channel, whose producer the
# framework EXECUTES and content-pins here. Every producer runs a pure builder (no git/docker/network),
# so it is safe at import. The provider module is already imported above as `_golden_providers`.
# =============================================================================================
register_provider("review_packet_fields_provider", _golden_providers.review_packet_fields_witness)
_register_p6_channel_producer(
    "review_packet_digest.required_field_domain",
    _golden_providers.review_packet_fields_authority,
    source_id="review_packet_digest.digests_emitted_keys",
    rationale="required digest fields derived from review_packet_digest.digests()'s emitted keys "
              "(minus the informational raw_file_bytes), independent of the REQUIRED_FIELDS tuple")

register_provider("generated_arn_keys_provider", _golden_providers.generated_arn_keys_witness)
_register_p6_channel_producer(
    "resource_oracle.generated_arn_key_domain",
    _golden_providers.generated_arn_keys_authority,
    source_id="resource_oracle.generated_arns_keys",
    rationale="generated-resource key set derived from resource_oracle.generated_arns()'s emitted "
              "keys (the real generated ARNs), independent of the GENERATED_KEYS tuple")

register_provider("provenance_fields_provider", _golden_providers.provenance_fields_witness)
_register_p6_channel_producer(
    "docker_assurance_state.provenance_field_domain",
    _golden_providers.provenance_fields_authority,
    source_id="docker_assurance_state.provenance_emitted_keys",
    rationale="required provenance fields derived from docker_assurance_state._provenance()'s "
              "emitted keys (pure dict assembly), independent of the _PROVENANCE_FIELDS tuple")


# =============================================================================================
# WAVE 5 CONSUMER BOOTSTRAP (Gate 4N-I28BH-B-SLICE3 shard-a, F2 family) — four more certificate-
# backed collections (workflow_assurance required-field constants vs a pure record builder's emitted
# keyset). Registration, NOT TCB logic: each binds one reviewed witness and its INDEPENDENT P6
# authority channel, whose producer the framework EXECUTES and content-pins here. Every producer runs
# a pure workflow_assurance builder (no git/docker/network, no Docker-state derivation), so it is safe
# at import. The provider module is already imported above as `_golden_providers`.
# =============================================================================================
register_provider("workflow_authorization_fields_provider",
                  _golden_providers.workflow_authorization_fields_witness)
_register_p6_channel_producer(
    "workflow_assurance.authorization_field_domain",
    _golden_providers.workflow_authorization_fields_authority,
    source_id="workflow_assurance.authorization_identity_emitted_keys",
    rationale="required authorization fields derived from workflow_assurance._authorization_identity()'s "
              "emitted keys (pure offline builder), independent of the _AUTHORIZATION_FIELDS tuple")

register_provider("image_manifest_fields_provider",
                  _golden_providers.image_manifest_fields_witness)
_register_p6_channel_producer(
    "workflow_assurance.image_manifest_field_domain",
    _golden_providers.image_manifest_fields_authority,
    source_id="workflow_assurance.post_build_image_bind_emitted_keys",
    rationale="required image-manifest fields derived from workflow_assurance.post_build_image_bind()'s "
              "emitted keys (minus the informational _problems), independent of the _IMAGE_MANIFEST_FIELDS tuple")

register_provider("build_output_fields_provider",
                  _golden_providers.build_output_fields_witness)
_register_p6_channel_producer(
    "workflow_assurance.build_output_field_domain",
    _golden_providers.build_output_fields_authority,
    source_id="workflow_assurance.post_build_image_bind_build_output_keys",
    rationale="required build-output fields derived from the build_output sub-record "
              "workflow_assurance.post_build_image_bind() emits, independent of the _BUILD_OUTPUT_FIELDS tuple")

register_provider("pre_push_fields_provider",
                  _golden_providers.pre_push_fields_witness)
_register_p6_channel_producer(
    "workflow_assurance.pre_push_field_domain",
    _golden_providers.pre_push_fields_authority,
    source_id="workflow_assurance.pre_push_verify_emitted_keys",
    rationale="required pre-push fields derived from workflow_assurance.pre_push_verify()'s emitted "
              "keys (pure builder), independent of the _PRE_PUSH_FIELDS tuple")


# =============================================================================================
# WAVE 6 CONSUMER BOOTSTRAP (Gate 4N-I28BH-B-SLICE3 shard-b, F3 family — AST self-naming constant
# vocabulary) — four more certificate-backed collections. Registration, NOT TCB logic: each binds
# one reviewed witness and its INDEPENDENT P6 authority channel, whose producer the framework
# EXECUTES and content-pins here. Every producer AST-extracts the per-member self-naming constants
# from the collection's OWN module source TEXT (no import of the module under test, no git/docker/
# network) — a structurally-distinct second encoding than the aggregate tuple/frozenset, so it is
# safe at import and independent of the collection. The provider module is already imported above
# as `_golden_providers`.
# =============================================================================================
register_provider("startup_dispositions_provider",
                  _golden_providers.startup_dispositions_witness)
_register_p6_channel_producer(
    "startup_policy.disposition_vocabulary",
    _golden_providers.startup_dispositions_authority,
    source_id="startup_policy.self_naming_constants",
    rationale="startup disposition vocabulary derived from the per-member self-naming constants "
              "AST-extracted from scripts/startup_policy.py, independent of the DISPOSITIONS tuple")

register_provider("cache_classifications_provider",
                  _golden_providers.cache_classifications_witness)
_register_p6_channel_producer(
    "cache_authority.classification_vocabulary",
    _golden_providers.cache_classifications_authority,
    source_id="cache_authority.self_naming_constants",
    rationale="cache-authority classification vocabulary derived from the per-member self-naming "
              "constants AST-extracted from scripts/cache_authority.py, independent of the "
              "CLASSIFICATIONS frozenset")

register_provider("external_trust_classifications_provider",
                  _golden_providers.external_trust_classifications_witness)
_register_p6_channel_producer(
    "external_executable_trust.classification_vocabulary",
    _golden_providers.external_trust_classifications_authority,
    source_id="external_executable_trust.self_naming_constants",
    rationale="external-executable-trust classification vocabulary derived from the per-member "
              "self-naming constants AST-extracted from scripts/external_executable_trust.py, "
              "independent of the CLASSIFICATIONS tuple")

register_provider("leak_decisions_provider",
                  _golden_providers.leak_decisions_witness)
_register_p6_channel_producer(
    "leak_scan.decision_vocabulary",
    _golden_providers.leak_decisions_authority,
    source_id="leak_scan.self_naming_constants",
    rationale="leak-scan decision vocabulary derived from the per-member self-naming constants "
              "AST-extracted from scripts/leak_scan.py, independent of the DECISIONS tuple")


# =============================================================================================
# WAVE 6B CONSUMER BOOTSTRAP (Gate 4N-I28BH-B-SLICE3 shard-b) — F5-QUALIFIED + CROSS-MODULE.
# Registration, NOT TCB logic: each binds one reviewed witness and its INDEPENDENT P6 authority
# channel, whose producer the framework EXECUTES and content-pins here. Every producer reads a
# tracked fixture or another module's constant (no git/docker/network), so it is safe at import.
# The provider module is already imported above as `_golden_providers`.
# =============================================================================================
register_provider("allowed_accounts_provider",
                  _golden_providers.allowed_accounts_witness)
_register_p6_channel_producer(
    "leak_scan.approved_account_domain",
    _golden_providers.allowed_accounts_authority,
    source_id="leak_scan.approved_account_registry",
    rationale="approved-account floor derived from the tracked approved-account registry fixture "
              "leak_scan reads as authority, independent of the ALLOWED_ACCOUNTS frozenset")

register_provider("reviewed_tag_keys_provider",
                  _golden_providers.reviewed_tag_keys_witness)
_register_p6_channel_producer(
    "gen_role_bootstrap_policy.reviewed_tag_key_domain",
    _golden_providers.reviewed_tag_keys_authority,
    source_id="trust_policies.tags_expectation_keys",
    rationale="reviewed tag-key floor derived from trust_policies.trust_manifest()'s "
              "tags_expectation keys (a different module), independent of the ALLOWED_TAG_KEYS list")

register_provider("service_principals_provider",
                  _golden_providers.service_principals_witness)
_register_p6_channel_producer(
    "trust_validator.service_principal_domain",
    _golden_providers.service_principals_authority,
    source_id="trust_validator.role_purpose_service_principals",
    rationale="service-principal floor derived from the SERVICE_ROLE entries of "
              "trust_validator.ROLE_PURPOSE, independent of the ALLOWED_SERVICE_PRINCIPALS set")


# =============================================================================================
# WAVE 7 CONSUMER BOOTSTRAP (Gate 4N-I28BH-B-SLICE3 shard-b, ceiling sweep) — F3-name-prefix +
# CROSS-MODULE + F5. Registration, NOT TCB logic: each binds one reviewed witness and its
# INDEPENDENT P6 authority channel, whose producer the framework EXECUTES and content-pins here.
# Every producer AST-parses a module source or reads a tracked fixture (no git/docker/network), so
# it is safe at import. The provider module is already imported above as `_golden_providers`.
# =============================================================================================
register_provider("site_decisions_provider", _golden_providers.site_decisions_witness)
_register_p6_channel_producer(
    "docker_boundary.site_decision_vocabulary",
    _golden_providers.site_decisions_authority,
    source_id="docker_boundary.site_prefix_constants",
    rationale="site-decision vocabulary derived from the values of the ^SITE_ string constants "
              "AST-extracted from scripts/docker_boundary.py, independent of the SITE_DECISIONS tuple")

register_provider("assurance_modes_provider", _golden_providers.assurance_modes_witness)
_register_p6_channel_producer(
    "workflow_assurance.lifecycle_mode_domain",
    _golden_providers.assurance_modes_authority,
    source_id="workflow_assurance.mode_prefix_constants",
    rationale="assurance lifecycle-mode domain derived from the values of workflow_assurance's ^MODE_ "
              "constants (a different module that owns the modes), independent of the "
              "_ASSURANCE_ROLE_BY_MODE dict whose keys the validator recognises")

register_provider("docker_steering_categories_provider",
                  _golden_providers.docker_steering_categories_witness)
_register_p6_channel_producer(
    "docker_boundary.referenced_steering_category_domain",
    _golden_providers.docker_steering_categories_authority,
    source_id="docker_boundary.policy_fixture_referenced_categories",
    rationale="referenced steering-category floor derived from the prose categories the "
              "docker-boundary-policy.json call-site records reference (not concrete steering keys), "
              "independent of the DOCKER_STEERING_CATEGORIES dict")

register_provider("read_back_actions_provider", _golden_providers.read_back_actions_witness)
_register_p6_channel_producer(
    "gen_role_bootstrap_policy.read_after_create_domain",
    _golden_providers.read_back_actions_authority,
    source_id="operator_closure_contract.role_bootstrap_iam_read_after_create",
    rationale="read-after-create floor derived from operator-closure-contract.json "
              "role_bootstrap_closure.iam_read_after_create (which the generator does not read), "
              "independent of the READ_BACK_ACTIONS tuple")
