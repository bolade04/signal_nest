#!/usr/bin/env python3
"""Assertion-semantic validation for load-bearing tests (Gate 4N-I28U).

THE DEFECT THIS CLOSES. Gate 4N-I28T proved, on a git-bearing clone whose clean baseline was green
on every control, that the body of a load-bearing test could be replaced with a single
``assert True`` and NOTHING noticed: the gutted test passed, the surrounding pins passed, the site
universe pin passed, and package coherence stayed green. Seven real assertions became one trivial
one and the suite reported success.

That is not a hypothetical. One gate earlier, at 4N-I28R, two tests were found that had kept their
NAMES while losing their MEANING — ``"exact 24h maximum"`` still passed after a restamp as a 4h24m
window, and an "expired grant" fixture had drifted inside the new window. A test that still runs and
still passes while asserting nothing is the same failure wearing better clothes.

WHY THE EXPECTATION LIVES OUTSIDE THE TEST. The obvious implementation — walk the test, see what it
asserts, require that — is a self-authored oracle: it would ratify whatever the test currently says,
including ``assert True``. So the requirement comes from
``tests/fixtures/assertion-contract-registry.json``, an AUTHORED file that states, per test, which
assertion CLASSES must be present and which stable tokens they must reference. This module reads
that contract and checks the AST against it. It never derives a requirement from the AST it audits.

WHY MEMBERSHIP IN THE INVENTORY IS EARNED, NOT NAMED. A test is load-bearing here only if a real
mutation exists that the test CATCHES — each registry entry names that mutation. Nothing qualifies
because its filename contains "security", because its function name contains "critical", because it
appears in a manifest, because it is new, or because it has many assertions.

WHAT COUNTS AS MEANINGFUL. An assertion is meaningful when its truth depends on the program under
test. ``assert True``, ``assert 1``, ``assert "text"``, ``assert object()``, ``assert x == x`` and
``assert v is v`` do not: they are true whatever the code does. Those are rejected by construction,
not by pattern-matching one known expression.

FAIL CLOSED. An unparseable file, a missing test, a missing required class, a trivial required
assertion, or an assertion mechanism this module cannot resolve is a PROBLEM. An unresolved helper
is never silently trusted — that would let a load-bearing assertion hide behind a function whose
semantics nobody registered.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
REGISTRY = REPO_ROOT / "tests" / "fixtures" / "assertion-contract-registry.json"
META_CONTRACT = REPO_ROOT / "tests" / "fixtures" / "assertion-meta-contract.json"
REGISTRY_BASELINE = REPO_ROOT / "tests" / "fixtures" / "assertion-registry-baseline.json"

# The vocabulary a contract may require. Each maps to a concrete AST predicate below, so a contract
# can never require something this module cannot decide.
ASSERTION_CLASSES = (
    "EXACT_IDENTITY_EQUALITY",
    "SET_EQUALITY",
    "HASH_EQUALITY",
    "COUNT_EQUALITY",
    "MEMBERSHIP",
    "NONMEMBERSHIP",
    "EXPECTED_EXCEPTION",
    "EXPECTED_NONZERO_EXIT",
    "EXPECTED_ZERO_EXIT",
    "MUTATION_CHANGES_RESULT",
    "INERT_MUTATION_PRESERVES_RESULT",
    "BASELINE_GREEN_BEFORE_MUTATION",
    "MUTATION_CONSUMPTION_PROOF",
    "FIRST_REJECTING_CONTROL_PROOF",
    "MISSING_TO_MISSING_REJECTION",
    "CANDIDATE_OR_PACKET_BINDING_EQUALITY",
    "IDENTITY_COMPARISON",
    "INEQUALITY",
    # A non-constant expression whose truth still depends on the program under test. Weaker than an
    # equality and therefore only accepted where a contract explicitly asks for it, always with
    # must_reference tokens so it cannot be satisfied by an unrelated truthy expression.
    "MEANINGFUL_TRUTHINESS",
)

# Assertion mechanisms whose failure semantics are known. Anything else that is used AS the
# assertion of a load-bearing test is UNRESOLVED, never assumed adequate.
REGISTERED_HELPERS = {
    "pytest.raises": "raises if the expected exception is not raised",
    "pytest.fail": "fails unconditionally when reached",
    "pytest.approx": "numeric tolerance comparison, used inside a comparison",
}

# Standard-library predicates whose truth conditions are defined by Python itself. These are NOT
# "helpers whose semantics nobody declared" — the risk the unresolved-helper rule exists for is a
# PROJECT-defined function that might return True unconditionally. Flagging `str.startswith` as
# unresolved was a false positive that would have made the control unusable on real tests.
KNOWN_PREDICATE_METHODS = frozenset({
    "startswith", "endswith", "isdigit", "isalpha", "isupper", "islower", "isspace",
    "isidentifier", "isnumeric", "isalnum", "issubset", "issuperset", "isdisjoint",
    "is_file", "is_dir", "exists", "is_absolute", "is_relative_to", "samefile",
    "match", "search", "fullmatch", "__contains__",
})

TRIVIAL = "TRIVIAL_ALWAYS_TRUE"


class ContractError(ValueError):
    """Fail-closed. A contract this module cannot decide is never treated as satisfied."""


# --------------------------------------------------------------------------- AST helpers
def _is_always_true(node: ast.AST) -> bool:
    """True when the expression's truth cannot depend on the program under test."""
    if isinstance(node, ast.Constant):
        return bool(node.value)
    if isinstance(node, (ast.Tuple, ast.List)) and node.elts:
        return True                                   # a non-empty literal is always truthy
    if isinstance(node, (ast.Dict, ast.Set)) and (node.keys if isinstance(node, ast.Dict)
                                                  else node.elts):
        return True
    if isinstance(node, ast.Lambda):
        # `assert (lambda: a == b)` asserts a FUNCTION OBJECT, which is always truthy. The
        # comparison inside it never runs. Gate 4N-I28V used exactly this shape.
        return True
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and \
            node.func.id in ("object", "list", "dict", "set", "tuple") and not node.args:
        # `assert object()` is always truthy; `assert list()` is always FALSY, so only the
        # truthy constructors count as always-true.
        return node.func.id == "object"
    # GATE 4N-I28BC. Builtin conversions of a CONSTANT decide nothing either: `bool(1)`, `len((1,))`
    # and `int("1")` are computed entirely from a literal and cannot depend on the program under
    # test. `bool(1) or X` escaped the first version of this gate's fix, which handled BoolOp but
    # not this operand shape.
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and \
            node.func.id in ("bool", "len", "int", "float", "str", "abs") and \
            len(node.args) == 1 and not node.keywords:
        arg = node.args[0]
        # `-1` is UnaryOp(USub, Constant(1)), not a Constant, so `abs(-1)` slipped past the first
        # version of this check. Fold the sign before testing.
        if isinstance(arg, ast.UnaryOp) and isinstance(arg.op, (ast.USub, ast.UAdd)) and \
                isinstance(arg.operand, ast.Constant) and isinstance(arg.operand.value, (int, float)):
            value = -arg.operand.value if isinstance(arg.op, ast.USub) else arg.operand.value
            arg = ast.Constant(value=value)
        if isinstance(arg, ast.Constant):
            try:
                return bool({"bool": bool, "len": len, "int": int, "float": float,
                             "str": str, "abs": abs}[node.func.id](arg.value))
            except Exception:
                return False
        if isinstance(arg, (ast.Tuple, ast.List, ast.Set)) and node.func.id in ("bool", "len"):
            return bool(arg.elts)
        if isinstance(arg, ast.Dict) and node.func.id in ("bool", "len"):
            return bool(arg.keys)
    if isinstance(node, ast.Compare) and len(node.ops) == 1:
        left, right = node.left, node.comparators[0]
        if isinstance(node.ops[0], (ast.Eq, ast.Is, ast.LtE, ast.GtE)):
            # `x == x`, `v is v`: the same expression on both sides decides nothing.
            if ast.dump(left) == ast.dump(right):
                return True
        if isinstance(node.ops[0], ast.Eq) and isinstance(left, ast.Constant) and \
                isinstance(right, ast.Constant):
            return left.value == right.value
    # GATE 4N-I28BC, closing I28BB-RESIDUAL-01 (falsification arm f21). `assert True or X` is a
    # BoolOp, not a Constant, so every check above missed it — while Python short-circuits and
    # NEVER EVALUATES X. The neutralised assertion counted as meaningful, its required tokens were
    # still textually present inside the dead operand, and the contract layer raised no objection.
    #
    # `or` is always-true when ANY operand is; `and` only when ALL are. This is the whole class,
    # not a special case for the one literal that was used: `X or True`, `True or X`, `1 or X`,
    # `(a,) or X` and `object() or X` are all caught by recursion, and `assert X and True` is
    # correctly NOT trivial because X still decides the result.
    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.Or):
            return any(_is_always_true(v) for v in node.values)
        return all(_is_always_true(v) for v in node.values)
    # `assert not False` / `assert not 0` decide nothing either.
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not) and \
            isinstance(node.operand, ast.Constant):
        return not bool(node.operand.value)
    return False


# --------------------------------------------------------------------------- reachability
# GATE 4N-I28W. Gate 4N-I28V proved that an assertion which can never execute still satisfied its
# contract: wrapping a contracted test's whole body in `if False:` preserved every required token,
# class and count while executing none of it, and every control stayed green. The cause was that
# analyse_function collected assertions with a bare ast.walk, which has no notion of control flow.
#
# This is the SAME blind spot Gate 4N-I28S closed for shell (`if false; then ... fi`) and did not
# carry into the Python analysis. The model below is deliberately bounded in the same way: it folds
# only constants, and anything it cannot decide becomes UNRESOLVED and FAILS CLOSED rather than
# being assumed to execute.
REACHABLE = "REACHABLE"
UNREACHABLE = "UNREACHABLE"
CONDITIONAL = "CONDITIONAL"
UNRESOLVED = "UNRESOLVED"

_TERMINATORS = (ast.Return, ast.Raise, ast.Continue, ast.Break)


def _const_truth(node: ast.expr):
    """True/False when a test is a compile-time constant, else None.

    Only literals are folded. A name, call or comparison is NOT folded — guessing a runtime
    condition is how a reachability model starts inventing facts.
    """
    if isinstance(node, ast.Constant):
        return bool(node.value)
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return bool(node.elts)
    if isinstance(node, ast.Dict):
        return bool(node.keys)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        inner = _const_truth(node.operand)
        return None if inner is None else (not inner)
    return None


def _is_skip(stmt: ast.stmt) -> bool:
    """`pytest.skip(...)` as a statement ends execution of everything after it."""
    if not isinstance(stmt, ast.Expr) or not isinstance(stmt.value, ast.Call):
        return False
    f = stmt.value.func
    return (isinstance(f, ast.Attribute) and f.attr in ("skip", "xfail", "exit")
            and isinstance(f.value, ast.Name) and f.value.id == "pytest")


def _merge(a: str, b: str) -> str:
    """Combine an enclosing disposition with a local one, worst-case first."""
    order = {UNREACHABLE: 0, UNRESOLVED: 1, CONDITIONAL: 2, REACHABLE: 3}
    return a if order[a] <= order[b] else b


def assertion_reachability(fn: ast.FunctionDef) -> dict:
    """Map each ast.Assert in a function to its reachability disposition.

    Returns {id(assert_node): {"disposition": ..., "region": ..., "dominators": [...]}}.
    """
    out: dict[int, dict] = {}
    invoked: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            invoked.add(node.func.id)

    def walk(body, disposition: str, region: str, dominators: list[str]):
        live = disposition
        for stmt in body:
            if isinstance(stmt, ast.Assert):
                out[id(stmt)] = {"disposition": live, "region": region,
                                 "dominators": list(dominators)}
                continue
            if isinstance(stmt, ast.If):
                truth = _const_truth(stmt.test)
                label = ast.dump(stmt.test)[:60]
                if truth is True:
                    walk(stmt.body, live, region + "/if-const-true", dominators + ["if True"])
                    walk(stmt.orelse, UNREACHABLE, region + "/else-of-const-true",
                         dominators + ["else of if True"])
                elif truth is False:
                    walk(stmt.body, UNREACHABLE, region + "/if-const-false",
                         dominators + ["if False"])
                    walk(stmt.orelse, live, region + "/else-of-const-false",
                         dominators + ["else of if False"])
                else:
                    branch = _merge(live, CONDITIONAL)
                    walk(stmt.body, branch, region + "/if-dynamic", dominators + [label])
                    walk(stmt.orelse, branch, region + "/else-dynamic", dominators + [label])
                continue
            if isinstance(stmt, (ast.For, ast.AsyncFor)):
                truth = _const_truth(stmt.iter)
                if truth is False:
                    walk(stmt.body, UNREACHABLE, region + "/empty-loop",
                         dominators + ["for over an empty literal"])
                elif truth is True:
                    walk(stmt.body, live, region + "/finite-loop",
                         dominators + ["for over a non-empty literal"])
                else:
                    walk(stmt.body, _merge(live, CONDITIONAL), region + "/dynamic-loop",
                         dominators + ["for over a dynamic iterable"])
                walk(stmt.orelse, live, region + "/for-else", dominators)
                continue
            if isinstance(stmt, ast.While):
                truth = _const_truth(stmt.test)
                if truth is False:
                    walk(stmt.body, UNREACHABLE, region + "/while-false",
                         dominators + ["while False"])
                elif truth is True:
                    walk(stmt.body, live, region + "/while-true", dominators + ["while True"])
                else:
                    walk(stmt.body, _merge(live, CONDITIONAL), region + "/while-dynamic",
                         dominators + ["dynamic while"])
                walk(stmt.orelse, live, region + "/while-else", dominators)
                continue
            if isinstance(stmt, ast.Try):
                walk(stmt.body, live, region + "/try", dominators)
                for handler in stmt.handlers:
                    walk(handler.body, _merge(live, CONDITIONAL), region + "/except",
                         dominators + ["exception handler"])
                walk(stmt.orelse, _merge(live, CONDITIONAL), region + "/try-else", dominators)
                walk(stmt.finalbody, live, region + "/finally", dominators)
                continue
            if isinstance(stmt, (ast.With, ast.AsyncWith)):
                walk(stmt.body, live, region + "/with", dominators)
                continue
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # A nested definition executes only if something calls it by name.
                inner = live if stmt.name in invoked else UNREACHABLE
                walk(stmt.body, inner, region + f"/def:{stmt.name}",
                     dominators + ([f"nested function {stmt.name} is never called"]
                                   if inner == UNREACHABLE else
                                   [f"nested function {stmt.name} is called"]))
                continue
            if isinstance(stmt, ast.ClassDef):
                walk(stmt.body, live, region + f"/class:{stmt.name}", dominators)
                continue
            if isinstance(stmt, _TERMINATORS) or _is_skip(stmt):
                what = ("pytest.skip" if _is_skip(stmt) else type(stmt).__name__.lower())
                live = UNREACHABLE
                dominators = dominators + [f"everything after {what}"]
                continue
        return live

    walk(fn.body, REACHABLE, "body", [])
    return out


def _evaluated_compares(node: ast.expr):
    """Comparisons the assertion actually evaluates.

    Lambda bodies are NOT descended into: `assert (lambda: a == b)` asserts a function object,
    which is always truthy, and crediting the comparison inside it would let a never-called
    closure satisfy a contract. Comprehensions over a statically empty iterable are skipped for
    the same reason.
    """
    for sub in ast.walk(node):
        if isinstance(sub, ast.Lambda):
            continue
        if isinstance(sub, ast.Compare) and len(sub.ops) == 1:
            if not _inside_skipped_context(node, sub):
                yield sub


def _inside_skipped_context(root: ast.expr, target: ast.Compare) -> bool:
    """True when the comparison sits inside a lambda or a statically empty comprehension."""
    for sub in ast.walk(root):
        if isinstance(sub, ast.Lambda):
            if any(t is target for t in ast.walk(sub)):
                return True
        if isinstance(sub, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
            if all(_const_truth(g.iter) is False for g in sub.generators):
                if any(t is target for t in ast.walk(sub)):
                    return True
    return False


def _tokens(node: ast.AST) -> set[str]:
    """Stable identifiers an expression references.

    Deliberately includes attribute names, subscript keys, string constants and called function
    names, and deliberately EXCLUDES nothing — a local variable rename is tolerated by contracts
    because contracts reference stable tokens (keys, literals, attributes), not locals.
    """
    out: set[str] = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Name):
            out.add(n.id)
        elif isinstance(n, ast.Attribute):
            out.add(n.attr)
        elif isinstance(n, ast.Constant) and isinstance(n.value, str):
            out.add(n.value)
        elif isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
            out.add(str(n.value))
    return out


def _classes_of(node: ast.expr) -> set[str]:
    """The assertion classes an expression exhibits.

    EVERY comparison inside the expression counts, not just the outermost one: an assertion like
    ``assert any("x" in p for p in problems)`` carries its real semantics in a Compare nested
    inside a generator, and a top-level-only classifier would call that assertion classless and
    reject a perfectly good test.
    """
    found: set[str] = set()
    for sub in _evaluated_compares(node):
        found |= _compare_classes(sub)
    if not found and not _is_always_true(node):
        # A non-constant expression whose truth still depends on the program under test.
        found.add("MEANINGFUL_TRUTHINESS")
    return found


def _compare_classes(node: ast.Compare) -> set[str]:
    found: set[str] = set()
    op = node.ops[0]
    left, right = node.left, node.comparators[0]
    both = _tokens(left) | _tokens(right)
    dumps = f"{ast.dump(left)}{ast.dump(right)}"

    if isinstance(op, ast.Eq):
        found.add("EXACT_IDENTITY_EQUALITY")
        if "Set(" in dumps or any(
                isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and
                n.func.id in ("set", "frozenset") for n in (left, right)):
            found.add("SET_EQUALITY")
        if any(t.lower().find(x) >= 0 for t in both for x in ("hash", "sha", "digest")):
            found.add("HASH_EQUALITY")
        if any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "len"
               for n in (left, right)) or any("count" in t.lower() for t in both):
            found.add("COUNT_EQUALITY")
        if any(t in ("returncode", "exit", "exit_code", "returncode") for t in both) and \
                any(isinstance(n, ast.Constant) and n.value == 0 for n in (left, right)):
            found.add("EXPECTED_ZERO_EXIT")
        if any(t.lower().find(x) >= 0 for t in both
               for x in ("candidate", "packet", "manifest", "tree")):
            found.add("CANDIDATE_OR_PACKET_BINDING_EQUALITY")
        found.add("INERT_MUTATION_PRESERVES_RESULT")
        found.add("BASELINE_GREEN_BEFORE_MUTATION")
        found.add("MUTATION_CONSUMPTION_PROOF")
        found.add("FIRST_REJECTING_CONTROL_PROOF")
        found.add("MISSING_TO_MISSING_REJECTION")
    if isinstance(op, ast.NotEq):
        found.add("INEQUALITY")
        found.add("MUTATION_CHANGES_RESULT")
        if any(t in ("returncode", "exit", "exit_code") for t in both):
            found.add("EXPECTED_NONZERO_EXIT")
    if isinstance(op, ast.In):
        found.add("MEMBERSHIP")
    if isinstance(op, ast.NotIn):
        found.add("NONMEMBERSHIP")
    if isinstance(op, (ast.Is, ast.IsNot)):
        found.add("IDENTITY_COMPARISON")
    return found


def _call_name(call: ast.Call) -> str:
    f = call.func
    if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
        return f"{f.value.id}.{f.attr}"
    if isinstance(f, ast.Attribute):
        return f.attr
    if isinstance(f, ast.Name):
        return f.id
    return "<dynamic>"


def _helper_calls(node: ast.AST) -> set[str]:
    """Dotted names of every call inside an expression. Used for context, not for adequacy."""
    return {_call_name(n) for n in ast.walk(node) if isinstance(n, ast.Call)}


def _mechanism_call(test: ast.expr) -> str | None:
    """The call that IS the assertion, if the assertion is nothing but a call.

    Only this position can hide failure semantics. An earlier version treated EVERY call inside an
    assertion as an assertion helper, which flagged ordinary methods such as ``str.startswith``
    appearing inside a comprehension — a false positive that would have made the control unusable.
    """
    node = test
    while isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        node = node.operand
    if isinstance(node, ast.Call):
        return _call_name(node)
    return None


BUILTINS = {"len", "set", "sorted", "any", "all", "list", "dict", "str", "int", "bool", "tuple",
            "frozenset", "isinstance", "getattr", "hasattr", "type", "repr", "abs", "min", "max",
            "sum", "range", "enumerate", "zip", "open", "print", "float", "round", "next", "iter"}


def analyse_function(fn: ast.FunctionDef) -> dict:
    """Every assertion mechanism the function uses, with its classes and triviality."""
    asserts = []
    reach = assertion_reachability(fn)
    for node in ast.walk(fn):
        if isinstance(node, ast.Assert):
            r = reach.get(id(node), {"disposition": UNRESOLVED, "region": "?", "dominators": []})
            asserts.append({
                "kind": "assert",
                "reachability": r["disposition"],
                "region": r["region"],
                "dominators": r["dominators"],
                "trivial": _is_always_true(node.test),
                "classes": sorted(_classes_of(node.test)),
                "tokens": sorted(_tokens(node.test)),
                "dump": ast.dump(node.test),
                "helpers": sorted(_helper_calls(node.test) - BUILTINS),
                "mechanism_call": _mechanism_call(node.test),
            })
    raises, fails = [], []
    for node in ast.walk(fn):
        if isinstance(node, ast.With):
            for item in node.items:
                c = item.context_expr
                if isinstance(c, ast.Call):
                    name = _helper_calls(c)
                    if "pytest.raises" in name:
                        raises.append({"kind": "pytest.raises",
                                       "body_statements": len(node.body),
                                       "body_empty": not node.body or all(
                                           isinstance(s, ast.Pass) for s in node.body)})
        if isinstance(node, ast.Call) and "pytest.fail" in _helper_calls(node):
            fails.append({"kind": "pytest.fail"})
    return {"asserts": asserts, "raises": raises, "fails": fails,
            "reachable_meaningful_assert_count": sum(
                1 for a in asserts if not a["trivial"] and a["reachability"] == REACHABLE),
            "conditional_meaningful_assert_count": sum(
                1 for a in asserts if not a["trivial"] and a["reachability"] == CONDITIONAL),
            "unreachable_assert_count": sum(
                1 for a in asserts if a["reachability"] == UNREACHABLE),
            "unresolved_reachability_count": sum(
                1 for a in asserts if a["reachability"] == UNRESOLVED),
            "meaningful_assert_count": sum(1 for a in asserts if not a["trivial"]),
            "trivial_assert_count": sum(1 for a in asserts if a["trivial"]),
            "mechanism_count": len(asserts) + len(raises) + len(fails)}


def _find(tree: ast.AST, name: str):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def load_registry(path: Path | None = None) -> dict:
    p = path or REGISTRY
    if not p.is_file():
        raise ContractError(
            f"the assertion contract registry is missing at {p}. It is the INDEPENDENT source of "
            "what each load-bearing test must assert; without it this control would have to read "
            "the requirement out of the tests it audits, which is the self-authored oracle it "
            "exists to prevent.")
    return json.loads(p.read_text(encoding="utf-8"))


def validate(registry: dict | None = None, *, root: Path | None = None) -> dict:
    """Check every contracted test against its declared assertion requirements."""
    reg = registry if registry is not None else load_registry()
    base = root or REPO_ROOT
    problems: list[str] = []
    results = []
    contracts = reg.get("contracts", [])
    seen_ids = set()

    for c in contracts:
        cid = c["contract_id"]
        if cid in seen_ids:
            problems.append(f"{cid}: duplicate contract id")
        seen_ids.add(cid)
        path = base / c["file"]
        entry = {"contract_id": cid, "file": c["file"], "test": c["test"],
                 "protected_invariant": c["protected_invariant"], "satisfied": False}
        if not path.is_file():
            problems.append(f"{cid}: {c['file']} is not in the tree")
            results.append(entry)
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            # A file that will not parse is a PROBLEM, never a skip: skipping unparseable files
            # is how a validator is silenced by breaking the thing it validates.
            problems.append(f"{cid}: {c['file']} does not parse ({exc}); refusing to skip it")
            results.append(entry)
            continue
        fn = _find(tree, c["test"])
        if fn is None:
            problems.append(f"{cid}: {c['file']}::{c['test']} no longer exists")
            results.append(entry)
            continue

        found = analyse_function(fn)
        entry["analysis"] = {k: found[k] for k in
                             ("meaningful_assert_count", "trivial_assert_count",
                              "mechanism_count")}

        minimum = c.get("minimum_meaningful_assertions", 1)
        # Guarded conditional assertions count toward the minimum: a loop body whose non-vacuity
        # is proven by a REACHABLE guard really does check what it claims. Unguarded conditionals
        # and unreachable assertions never count.
        guarded = (found["reachable_meaningful_assert_count"] >= 1 and
                   any(r.get("allow_conditional") for r in c["required_assertions"]))
        countable = found["reachable_meaningful_assert_count"] + (
            found["conditional_meaningful_assert_count"] if guarded else 0)
        if countable + len(found["raises"]) + len(found["fails"]) < minimum:
            problems.append(
                f"{cid}: {c['file']}::{c['test']} has "
                f"{countable} countable meaningful assertion(s) "
                f"({found['reachable_meaningful_assert_count']} reachable) plus "
                f"{len(found['raises'])} pytest.raises and {len(found['fails'])} pytest.fail, "
                f"below the contracted minimum of {minimum}. Protected invariant: "
                f"{c['protected_invariant']}.")

        reachable = [a for a in found["asserts"] if a["reachability"] == REACHABLE]
        if reachable and all(a["trivial"] for a in reachable) and \
                not found["raises"] and not found["fails"]:
            problems.append(
                f"{cid}: every assertion in {c['file']}::{c['test']} is always true regardless of "
                f"the program under test, so the test can no longer fail. Protected invariant: "
                f"{c['protected_invariant']}.")

        # unresolved helper semantics never pass silently
        unresolved = set()
        for a in found["asserts"]:
            # Unresolved only when the assertion IS a call whose failure semantics nobody declared.
            # A call appearing inside a comparison is an argument, not the mechanism.
            helper = a.get("mechanism_call")
            bare = helper.rsplit(".", 1)[-1] if helper else ""
            if helper and helper not in BUILTINS and helper not in REGISTERED_HELPERS and \
                    bare not in KNOWN_PREDICATE_METHODS and \
                    helper not in c.get("registered_helpers", []):
                unresolved.add(helper)
        if unresolved:
            problems.append(
                f"{cid}: the only assertion mechanism in {c['file']}::{c['test']} is the "
                f"unregistered helper(s) {sorted(unresolved)}, whose failure semantics are not "
                "declared. An unresolved helper is not accepted for a load-bearing test.")
        entry["unresolved_helpers"] = sorted(unresolved)

        # required classes, each satisfied by a NON-TRIVIAL assertion that references the
        # contracted stable tokens
        satisfied, missing = [], []
        for req in c["required_assertions"]:
            klass = req["class"]
            if klass not in ASSERTION_CLASSES:
                raise ContractError(f"{cid}: unknown assertion class {klass!r}")
            need = set(req.get("must_reference", []))
            ok = False
            for a in found["asserts"]:
                if a["trivial"] or klass not in a["classes"]:
                    continue
                # GATE 4N-I28W: an assertion that cannot execute cannot satisfy a contract.
                # CONDITIONAL is accepted only when the contract explicitly allows it, so a
                # dynamic branch never silently stands in for a guaranteed check.
                if a["reachability"] == UNREACHABLE:
                    continue
                if a["reachability"] == CONDITIONAL:
                    # A conditionally reachable assertion satisfies a contract only when the
                    # contract says so AND the test carries a REACHABLE guard proving the
                    # conditional region is not vacuous. Without that proof a loop over an empty
                    # collection would "satisfy" an invariant while checking nothing.
                    if not req.get("allow_conditional"):
                        continue
                    if found["reachable_meaningful_assert_count"] < 1:
                        continue
                if a["reachability"] == UNRESOLVED:
                    continue
                if need <= set(a["tokens"]):
                    ok = True
                    break
            if not ok and klass == "EXPECTED_EXCEPTION":
                ok = any(not r["body_empty"] for r in found["raises"])
            (satisfied if ok else missing).append(klass)
        for a in found["asserts"]:
            if a["reachability"] == UNREACHABLE and not a["trivial"] and a["classes"]:
                problems.append(
                    f"{cid}: {c['file']}::{c['test']} contains a meaningful assertion that can "
                    f"never execute (region {a['region']}; {'; '.join(a['dominators'])}). An "
                    "assertion that cannot run cannot protect an invariant.")
            if a["reachability"] == UNRESOLVED:
                problems.append(
                    f"{cid}: {c['file']}::{c['test']} contains an assertion whose reachability "
                    f"could not be determined (region {a['region']}). Unresolved reachability "
                    "fails closed.")
        entry["reachability"] = {
            "reachable_meaningful": found["reachable_meaningful_assert_count"],
            "unreachable": found["unreachable_assert_count"],
            "unresolved": found["unresolved_reachability_count"]}
        entry["required_classes"] = [r["class"] for r in c["required_assertions"]]
        entry["satisfied_classes"] = satisfied
        entry["missing_classes"] = missing
        for klass in missing:
            need = next(r.get("must_reference", []) for r in c["required_assertions"]
                        if r["class"] == klass)
            problems.append(
                f"{cid}: {c['file']}::{c['test']} no longer contains a meaningful {klass} "
                f"assertion referencing {need}. Protected invariant: {c['protected_invariant']}. "
                f"Proving mutation: {c['proving_mutation']}.")
        entry["satisfied"] = not missing
        results.append(entry)

    return {
        "registry": str((REGISTRY.relative_to(REPO_ROOT)) if REGISTRY.is_relative_to(REPO_ROOT)
                        else REGISTRY),
        "contracts": len(contracts),
        "duplicate_contract_ids": len(contracts) - len(seen_ids),
        "results": results,
        "problems": problems,
        "clean": not problems,
        "derivation": "requirements come from the authored contract registry; this module never "
                      "derives a requirement from the AST it audits",
    }


def load_meta(path: Path | None = None) -> dict:
    p = path or META_CONTRACT
    if not p.is_file():
        raise ContractError(
            f"the assertion META contract is missing at {p}. It is the SECOND trust layer: it "
            "states what the control itself must do and which tests must run. Without it the "
            "control could remove its own protection in a single edit.")
    return json.loads(p.read_text(encoding="utf-8"))


def helper_contract(name: str, meta: dict | None = None) -> dict | None:
    """The independently specified argument roles and failure semantics of a registered helper."""
    m = meta if meta is not None else load_meta()
    return m.get("registered_helper_contracts", {}).get(name)


def check_helper_use(call: ast.Call, name: str, meta: dict | None = None) -> list[str]:
    """Validate one registered-helper call site against its authored contract.

    Registering a helper by NAME alone was the I28V limitation: it accepted any call shape. The
    contract states arity, argument roles and permitted keywords, so a swapped or omitted argument
    is a problem rather than an assumption.
    """
    contract = helper_contract(name, meta)
    if contract is None:
        return [f"{name}: used as an assertion mechanism with no authored helper contract"]
    out = []
    if len(call.args) < contract.get("arity_min", 0):
        out.append(f"{name}: called with {len(call.args)} positional argument(s), fewer than the "
                   f"contracted minimum {contract['arity_min']} "
                   f"(roles: {contract.get('argument_roles')})")
    roles = contract.get("argument_roles") or []
    if roles and len(call.args) > len(roles) + 1:
        out.append(f"{name}: called with {len(call.args)} positional arguments but only "
                   f"{len(roles)} role(s) are contracted: {roles}")
    permitted = set(contract.get("permitted_keywords") or [])
    for kw in call.keywords:
        if kw.arg and kw.arg not in permitted:
            out.append(f"{name}: keyword {kw.arg!r} is not in the contracted set "
                       f"{sorted(permitted)}")
    return out


def validate_meta(meta: dict | None = None, *, root: Path | None = None) -> dict:
    """LAYER 2. Check the control's own required behaviours and its mandatory-test inventory."""
    m = meta if meta is not None else load_meta()
    base = root or REPO_ROOT
    problems: list[str] = []
    behaviours = m.get("required_validator_behaviours", [])
    module_src = (base / "scripts" / "assertion_contracts.py")
    if not module_src.is_file():
        problems.append("scripts/assertion_contracts.py is absent from the tree; the assertion "
                        "control does not exist")
        src_text = ""
    else:
        src_text = module_src.read_text(encoding="utf-8")
    for b in behaviours:
        symbol = b.get("symbol")
        if symbol and f"def {symbol}" not in src_text and symbol not in src_text:
            problems.append(f"{b['behaviour_id']}: the control no longer defines {symbol!r}, so "
                            f"the required behaviour '{b['requirement']}' cannot hold")
    mandatory = m.get("mandatory_tests", [])
    for entry in mandatory:
        rel, _, name = entry["node_id"].partition("::")
        path = base / rel
        if not path.is_file():
            problems.append(f"mandatory test file {rel} is absent: {entry['why']}")
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            problems.append(f"mandatory test file {rel} does not parse ({exc})")
            continue
        if _find(tree, name) is None:
            problems.append(f"mandatory test {entry['node_id']} is missing or renamed: "
                            f"{entry['why']}")
    # Helper implementation provenance. A third_party helper has no in-tree implementation to
    # weaken; an in_tree helper must be pinned by hash, or "the implementation was weakened" is
    # undetectable rather than impossible.
    for name, hc in (m.get("registered_helper_contracts") or {}).items():
        source = hc.get("implementation_source")
        if source not in ("third_party", "in_tree"):
            problems.append(f"helper {name}: implementation_source must be declared as "
                            "'third_party' or 'in_tree'")
            continue
        module = name.split(".")[0]
        candidates = [q for q in ((base / "scripts").rglob(f"{module}.py") if
                                  (base / "scripts").is_dir() else [])]
        if source == "third_party" and candidates:
            problems.append(f"helper {name}: declared third_party but an in-tree implementation "
                            f"exists at {candidates[0]}; it must be pinned instead")
        if source == "in_tree":
            pinned = hc.get("implementation_sha256")
            if not pinned:
                problems.append(f"helper {name}: declared in_tree without an "
                                "implementation_sha256; a weakened implementation would be "
                                "undetectable")
            elif candidates:
                actual = hashlib.sha256(candidates[0].read_bytes()).hexdigest()
                if actual != pinned:
                    problems.append(f"helper {name}: implementation changed "
                                    f"({actual[:16]} != pinned {pinned[:16]})")
    return {"behaviours": len(behaviours), "mandatory_tests": len(mandatory),
            "helper_contracts": len(m.get("registered_helper_contracts") or {}),
            "problems": problems, "clean": not problems,
            "trust_boundary": m.get("_trust_boundary")}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    try:
        result = validate()
        meta_result = validate_meta()
        result["meta"] = meta_result
        result["problems"] = result["problems"] + [f"META {p}" for p in meta_result["problems"]]
        result["clean"] = not result["problems"]
    except ContractError as exc:
        print(f"  {exc}", file=sys.stderr)
        print("ASSERTION CONTRACTS: refused")
        return 2
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"  {result['contracts']} contracted tests; problems {len(result['problems'])}")
        for p in result["problems"]:
            print(f"    {p}")
    print("ASSERTION CONTRACTS: " + ("clean" if result["clean"] else "PROBLEMS"))
    return 0 if result["clean"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
