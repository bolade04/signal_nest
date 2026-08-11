#!/usr/bin/env python3
"""REVIEW-PINNED INTEGRITY control — Gate 4N-I28BH-B-ARCHITECTURAL-ADJUDICATION (Design 2, §6-9).

THE PROPERTY THIS CERTIFIES. For the authored SOURCE-OF-TRUTH vocabularies (hand-written
allow/deny/keyword lists that have NO in-repo enumerable oracle to derive a domain from), the only
honest completeness property is REVIEWED INTEGRITY: the collection's current canonical content is
byte-identical to the content an INDEPENDENT security review approved. This module certifies exactly
that and nothing more. It does NOT claim the list is complete against external truth (there is no
oracle for "every command that consumes data as an argument"); it claims the list has not DRIFTED
away from its last independent review. A legitimate change is EXPECTED to make this RED until a new
review updates the pin — that is the control working, not failing (see §9 CHANGE-CONTROL below).

THE THREE INDEPENDENT SURFACES (why regress terminates).
  1. The COLLECTION under test          — the live authored vocabulary in scripts/.
  2. The REVIEW-PIN REGISTRY (fixture)  — {collection_id -> reviewed_digest + review_record_id}.
                                          Lives OUTSIDE the collection; the collection cannot
                                          self-update it. Governed as a SECURITY collection itself.
  3. The REVIEW-RECORD LEDGER (fixture) — {review_record_id -> status ACTIVE|SUPERSEDED}. The
                                          independently-controlled authority that says which review
                                          approvals are currently in force. A digest bump is only
                                          honoured if it cites an ACTIVE ledger record, so an owner
                                          cannot self-service a new pin by reusing an old approval.

FAIL-CLOSED BY CONSTRUCTION. Every refusal path returns a REFUSED verdict; the only ACCEPT path is
"identity matches AND digest matches AND the citing review record is ACTIVE". A malformed pin, a
missing pin, a copied pin, a stale (superseded) pin, or a pin that tries to DERIVE its expected
value at runtime all fail closed. There is no spec-supplied callback and no always-pass branch.

TWO DIGEST SOURCES, both import-independent in result.
  * "live"        (default) — import the module and digest the live object. Strictest: catches drift
                              in the final value however it was built.
  * "ast_literal"           — parse the module source, evaluate the module-level assignment's literal
                              RHS with ast.literal_eval (NO import, NO code execution) and digest that.
                              For a pure authored literal this yields the SAME digest as "live"; it is
                              used only for collections whose module cannot be imported in every
                              grading environment (e.g. a standalone script importing an optional
                              third-party dependency). It pins the exact reviewed authored literal.

STATIC RESOLVABILITY. Module-level functions and direct calls only; no class dispatch, no getattr
routing, no dynamically selected members. This keeps the module resolvable by site_taxonomy
(unresolved must stay 0) on a release-reachable path.
"""
from __future__ import annotations

import ast
import hashlib
import importlib
import json
import types
from pathlib import Path

# Verdict tags. ACCEPT is the ONLY non-refusal; every other tag is a fail-closed refusal reason.
ACCEPT = "ACCEPT"
REFUSED_MISSING_PIN = "REFUSED_MISSING_PIN"
REFUSED_MISBOUND_IDENTITY = "REFUSED_MISBOUND_IDENTITY"          # pin.collection_id != subject
REFUSED_MALFORMED_PIN = "REFUSED_MALFORMED_PIN"                  # invalid config
REFUSED_SELF_DERIVING_PIN = "REFUSED_SELF_DERIVING_PIN"         # pin tries to derive at runtime
REFUSED_UNKNOWN_REVIEW = "REFUSED_UNKNOWN_REVIEW"               # cited review not in ledger
REFUSED_STALE_REVIEW = "REFUSED_STALE_REVIEW"                   # cited review SUPERSEDED/revoked
REFUSED_DIGEST_DRIFT = "REFUSED_DIGEST_DRIFT"                   # current content != reviewed
REFUSED_UNLOADABLE = "REFUSED_UNLOADABLE"                       # collection could not be loaded

# Fields a pin may NEVER carry. Their presence means the record is attempting to compute its own
# "expected" value at check time (self-fulfilling), which defeats the entire control.
_FORBIDDEN_PIN_FIELDS = ("derive_from", "import", "expected_expr", "recompute", "eval")

# The closed set of digest sources a pin may declare. Anything else is malformed config.
_DIGEST_SOURCES = ("live", "ast_literal")

# A well-formed reviewed digest is exactly this shape. Anything else is invalid config.
_DIGEST_PREFIX = "sha256:"
_DIGEST_HEXLEN = 64

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS = _REPO_ROOT / "scripts"


# --------------------------------------------------------------------------------------------- #
# CANONICALIZATION — structure-aware, ordering-explicit.
# --------------------------------------------------------------------------------------------- #
def _code_fingerprint(code) -> str:
    """A deterministic fingerprint of a code object's BEHAVIOUR — never its identity/address.

    Binds the bytecode, argument shape, names and constants (nested code objects — closures/inner
    lambdas — recursively by their own code bytes). A change to the function body moves the
    fingerprint; re-running the same source in a fresh process does not. This is the review-pin
    analogue of the P9 code-digest posture, so a collection carrying authored PREDICATES (e.g. a
    rule table with policy lambdas) is content-pinned by what the predicate DOES, not where it lives.
    """
    consts = []
    for const in code.co_consts:
        if isinstance(const, types.CodeType):
            # Recurse into a nested code object's OWN fingerprint (BH-C F5c): a closure/inner lambda
            # that differs only in an inner constant must move the digest, so bind the full nested
            # fingerprint, not just its co_code bytes + names.
            consts.append(["code", _code_fingerprint(const)])
        else:
            consts.append(["const", repr(const)])
    shape = [code.co_argcount, code.co_kwonlyargcount, code.co_posonlyargcount,
             list(code.co_varnames), list(code.co_names), list(code.co_freevars),
             list(code.co_cellvars), consts, code.co_code.hex()]
    return hashlib.sha256(
        json.dumps(shape, separators=(",", ":"), ensure_ascii=True).encode("utf-8")).hexdigest()


def _canonical_member(value):
    """A JSON-safe, TYPE-TAGGED canonical form of one member.

    Type tags keep {"a"} distinct from ["a"] and a tuple distinct from a list, so a "rename" that
    swaps a member's container type still moves the digest. Sets/frozensets are order-normalised
    (unordered by nature); lists/tuples keep their order (ordering may be security-relevant, e.g. a
    precedence vector) and the top-level `ordered` flag decides whether the OUTER collection sorts.
    A member that is a callable is bound by its code fingerprint (behaviour, not address).
    """
    if isinstance(value, (types.FunctionType, types.LambdaType, types.MethodType)) \
            and getattr(value, "__code__", None) is not None:
        return ["callable", value.__qualname__, _code_fingerprint(value.__code__)]
    if isinstance(value, type):
        # A CLASS/type member (BH-C F5b) — bind its qualified identity rather than falling through to
        # str(). Honest limit: a review pin over a collection of classes binds by (module, qualname);
        # a same-named impostor with spoofed dunders in a source edit would still collide (the P9
        # name+provenance boundary), so class-collections are pinned for identity, not deep structure.
        return ["type", getattr(value, "__module__", "?"), getattr(value, "__qualname__", "?")]
    if isinstance(value, dict):
        pairs = [[_canonical_member(k), _canonical_member(v)] for k, v in value.items()]
        pairs.sort(key=_stable_key)
        return ["dict", pairs]
    if isinstance(value, frozenset):
        items = [_canonical_member(v) for v in value]
        items.sort(key=_stable_key)
        return ["frozenset", items]
    if isinstance(value, set):
        items = [_canonical_member(v) for v in value]
        items.sort(key=_stable_key)
        return ["set", items]
    if isinstance(value, tuple):
        return ["tuple", [_canonical_member(v) for v in value]]
    if isinstance(value, list):
        return ["list", [_canonical_member(v) for v in value]]
    if isinstance(value, bool):
        return ["bool", value]
    if isinstance(value, int):
        return ["int", value]
    if isinstance(value, float):
        return ["float", repr(value)]
    if value is None:
        return ["null", None]
    return ["str", str(value)]


def _stable_key(canonical_form) -> str:
    """A total, stable sort key for canonical forms: their compact JSON text."""
    return json.dumps(canonical_form, separators=(",", ":"), ensure_ascii=True)


def canonical_digest(collection_id: str, value, ordered: bool) -> str:
    """The reviewed-integrity digest of a live collection.

    Binds the COLLECTION IDENTITY into the hash input so a digest computed for collection A can
    never validate collection B even if their contents coincide (defeats the copied-pin / substitute
    attacks at the cryptographic level, on top of the explicit identity check in verify_pin).
    """
    if isinstance(value, dict):
        body = _canonical_member(value)
    else:
        members = [_canonical_member(v) for v in value]
        if not ordered:
            members.sort(key=_stable_key)
        # BH-C F5a: tag the OUTER container's concrete type so frozenset / set / tuple / list can
        # never collide at the top level. Losing the type erased the immutability guarantee a reviewer
        # signed off on (a frozenset silently becoming a mutable set had the same digest). The
        # ordered/unordered marker is retained because it also decides whether members were sorted.
        body = [type(value).__name__, "ordered" if ordered else "unordered", members]
    payload = collection_id + "\n" + json.dumps(body, separators=(",", ":"), ensure_ascii=True)
    return _DIGEST_PREFIX + hashlib.sha256(payload.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------------------------- #
# LIVE COLLECTION LOADER — imports the real module and reads the attribute. Never touches the pin.
# --------------------------------------------------------------------------------------------- #
def load_collection(collection_id: str):
    """Resolve `module.py::NAME` to the live object. Raises on anything that will not load."""
    module_file, name = collection_id.split("::", 1)
    module_name = module_file[:-3] if module_file.endswith(".py") else module_file
    module = importlib.import_module(module_name)
    if not hasattr(module, name):
        raise LookupError(f"{collection_id}: no such attribute")
    return getattr(module, name)


def load_collection_literal(collection_id: str):
    """Resolve `module.py::NAME` to its authored literal value WITHOUT importing the module.

    Parses the module source and evaluates the module-level assignment's RHS with
    ``ast.literal_eval`` (no code execution). Raises if the target is not a module-level assignment
    of a literal (list/tuple/set/dict/frozenset-of-literals) — an import-fragile collection that is
    not a pure literal cannot use this source and must be repaired or imported.
    """
    module_file, name = collection_id.split("::", 1)
    path = _SCRIPTS / module_file
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if not any(getattr(t, "id", None) == name for t in targets):
            continue
        rhs = node.value
        if rhs is None:
            break
        # frozenset({...}) / set([...]) style constructor wrapping a literal argument.
        if isinstance(rhs, ast.Call) and getattr(rhs.func, "id", None) in (
                "frozenset", "set", "tuple", "list"):
            if len(rhs.args) == 1:
                inner = ast.literal_eval(rhs.args[0])
                ctor = {"frozenset": frozenset, "set": set, "tuple": tuple, "list": list}
                return ctor[rhs.func.id](inner)
            if not rhs.args:
                ctor = {"frozenset": frozenset, "set": set, "tuple": tuple, "list": list}
                return ctor[rhs.func.id]()
        return ast.literal_eval(rhs)
    raise LookupError(f"{collection_id}: no module-level literal assignment to evaluate")


# --------------------------------------------------------------------------------------------- #
# PIN VALIDATION — the fail-closed core.
# --------------------------------------------------------------------------------------------- #
def _well_formed_digest(text) -> bool:
    if not isinstance(text, str) or not text.startswith(_DIGEST_PREFIX):
        return False
    hexpart = text[len(_DIGEST_PREFIX):]
    if len(hexpart) != _DIGEST_HEXLEN:
        return False
    return all(c in "0123456789abcdef" for c in hexpart)


def _review_status(ledger: dict, review_record_id) -> str:
    """The status of the cited review record, or UNKNOWN if the ledger does not carry it."""
    records = ledger.get("review_records", {})
    record = records.get(review_record_id)
    if not isinstance(record, dict):
        return "UNKNOWN"
    return record.get("status", "UNKNOWN")


def verify_pin(collection_id: str, pin, live_value, ledger: dict, load_error: str = "") -> dict:
    """Verify one collection against its review pin. Returns {verdict, detail}.

    ACCEPT requires ALL of: pin present; pin.collection_id == subject; well-formed digest; no
    self-deriving field; declared source in the closed set; cited review record ACTIVE in the
    ledger; current digest == reviewed digest. Every other outcome is a fail-closed REFUSED_*.
    """
    if load_error:
        return {"verdict": REFUSED_UNLOADABLE, "detail": load_error}
    if pin is None:
        return {"verdict": REFUSED_MISSING_PIN,
                "detail": f"{collection_id}: no review pin — an authored SECURITY vocabulary "
                          "without an independent review pin fails closed"}
    if not isinstance(pin, dict):
        return {"verdict": REFUSED_MALFORMED_PIN, "detail": f"{collection_id}: pin is not an object"}

    # SELF-DERIVING record: any field that would compute the expected value at runtime is forbidden.
    for field in _FORBIDDEN_PIN_FIELDS:
        if field in pin:
            return {"verdict": REFUSED_SELF_DERIVING_PIN,
                    "detail": f"{collection_id}: pin carries forbidden self-deriving field "
                              f"{field!r}; a review pin is inert reviewed DATA, never a runtime "
                              "recomputation of the collection it governs"}

    # IDENTITY: the pin must name the collection it governs. A pin copied from another collection
    # (even with a matching digest string) is refused here before any content comparison.
    if pin.get("collection_id") != collection_id:
        return {"verdict": REFUSED_MISBOUND_IDENTITY,
                "detail": f"{collection_id}: pin is bound to {pin.get('collection_id')!r}, not to "
                          "this collection"}

    source = pin.get("source", "live")
    if source not in _DIGEST_SOURCES:
        return {"verdict": REFUSED_MALFORMED_PIN,
                "detail": f"{collection_id}: digest source {source!r} is not one of "
                          f"{_DIGEST_SOURCES}"}

    reviewed_digest = pin.get("reviewed_digest")
    if not _well_formed_digest(reviewed_digest):
        return {"verdict": REFUSED_MALFORMED_PIN,
                "detail": f"{collection_id}: reviewed_digest {reviewed_digest!r} is not a "
                          "well-formed sha256 pin"}

    review_record_id = pin.get("review_record_id")
    status = _review_status(ledger, review_record_id)
    if status == "UNKNOWN":
        return {"verdict": REFUSED_UNKNOWN_REVIEW,
                "detail": f"{collection_id}: cites review record {review_record_id!r} which the "
                          "independent review ledger does not carry — a self-minted approval"}
    if status != "ACTIVE":
        return {"verdict": REFUSED_STALE_REVIEW,
                "detail": f"{collection_id}: cited review record {review_record_id!r} is "
                          f"{status} — a superseded/revoked approval no longer pins anything"}

    ordered = bool(pin.get("ordered", False))
    current_digest = canonical_digest(collection_id, live_value, ordered)
    if current_digest != reviewed_digest:
        return {"verdict": REFUSED_DIGEST_DRIFT,
                "detail": f"{collection_id}: current content digest {current_digest} != reviewed "
                          f"digest {reviewed_digest}; the collection changed since its last "
                          "independent review — RED until a new review updates the pin"}

    return {"verdict": ACCEPT,
            "detail": f"{collection_id}: current content matches review {review_record_id}"}


def _load_for_pin(collection_id: str, pin):
    """Resolve the live value for a pin according to its declared source. Returns (value, error)."""
    source = pin.get("source", "live") if isinstance(pin, dict) else "live"
    try:
        if source == "ast_literal":
            return load_collection_literal(collection_id), ""
        return load_collection(collection_id), ""
    except Exception as exc:                            # a collection that will not load is a finding
        return None, f"{collection_id}: {type(exc).__name__}: {exc}"


# --------------------------------------------------------------------------------------------- #
# BATCH CHECK — over an assurance-registry subset that selects the review_pin control.
# --------------------------------------------------------------------------------------------- #
def check(registry: dict, pins: dict, ledger: dict) -> dict:
    """Verify every collection the assurance registry routes to the review_pin control.

    `registry` maps collection_id -> {"control": "review_pin", ...}. A registry entry naming an
    unknown control is NOT silently skipped — it is a refusal (fail-closed dispatch).
    """
    rows = []
    problems = []
    for collection_id in sorted(registry):
        entry = registry[collection_id]
        control = entry.get("control")
        if control != "review_pin":
            problems.append(f"{collection_id}: control {control!r} is not review_pin — refused")
            rows.append({"collection": collection_id, "verdict": "REFUSED_UNKNOWN_CONTROL"})
            continue
        pin = pins.get("pins", {}).get(collection_id)
        live_value, load_error = _load_for_pin(collection_id, pin)
        result = verify_pin(collection_id, pin, live_value, ledger, load_error)
        rows.append({"collection": collection_id, **result})
        if result["verdict"] != ACCEPT:
            problems.append(result["detail"])
    return {"rows": rows, "problems": problems, "clean": not problems}
