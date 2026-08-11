#!/usr/bin/env python3
"""Executed-code provenance for the protected Gate 4N validation modules (Gate 4N-I28AE).

THE DEFECT THIS CLOSES. Gate 4N-I28AC finding ACC-I28AC-01. A `sitecustomize.py` staged into the
repository's own `scripts/` directory is auto-imported by the graded command's own PYTHONPATH at
interpreter startup — before pytest, before conftest, before `-p pytest_session_guard`. Pre-seeding
`sys.modules["pytest_session_guard"]` there makes the guard and every verifier resolve the SAME
substituted module. The measured exploit left the suite at 2906 passed / 0 failed with four
assurance-control modules absent, while `inspect.getsource`, the provenance hash and package
coherence all reported the legitimate staged file, because every one of them reads the file on DISK
rather than the code that ran.

WHAT THIS BINDS INSTEAD. An identity derived from the code objects ACTUALLY RESIDENT in the running
module — `co_code`, names, varnames, argument count and flags, keyed by qualified name — compared
against the same identity computed from a fresh compile of the pinned bytes. Substituted code that
behaves differently HAS different bytecode, so the two disagree and the session fails closed.
Substituted code that is byte-identical is not an attack.

WHY THE COMPARISON IS DETERMINISTIC. pytest loads `-p` plugins through its AssertionRewritingHook,
so one might expect rewritten bytecode. Measured on this tree: the hook is the loader but it does
NOT rewrite these modules — all 9 shared code objects in `pytest_session_guard` are byte-identical
to a plain `compile()`. The canonicalization is therefore "plain compile of the pinned bytes", and
`test_i28ae_executed_provenance.py::test_the_canonicalization_is_deterministic` re-measures that
claim rather than assuming it. If a future pytest DOES rewrite these modules the equality breaks
loudly, which is the correct direction to fail.

WHAT IS HONESTLY NOT CLAIMED. This does not defend against a party who can rewrite every
independently pinned source at once. It also does not, by itself, stop code that patches a function
object AFTER verification: `signalnest_bootstrap.pytest_sessionfinish` re-runs this check at
session finish for that reason, added at Gate 4N-I28AG. Until then this docstring claimed the
re-run already existed when it did not — Gate 4N-I28AF finding ADV-I28AF-03 — and the claim is
recorded here rather than quietly deleted.

THE LIMIT THAT MATTERS MOST, named because Gate 4N-I28AF found it the hard way. Code identity is
NOT sufficient. A module whose code is byte-identical to the pinned file can still behave
differently, because behaviour depends on what the code READS: module globals, keyword defaults,
the module-level helpers it calls, and the contents of the files those values name. Rebinding one
global defeated this module entirely while every check here reported clean. That gap is closed by
`executed_state_provenance.py`, not by anything in this file, and this file should never be read as
covering it.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PROTECTED_SET = REPO_ROOT / "tests" / "fixtures" / "protected-module-set.json"


class ProvenanceError(RuntimeError):
    """Fail closed. Provenance that cannot be established is never treated as established."""


def load_protected_set(path: Path | None = None) -> dict:
    p = path or PROTECTED_SET
    if not p.is_file():
        raise ProvenanceError(
            f"the protected-module set is missing at {p}. It is the authored statement of which "
            "modules must be provenance-bound; without it this control would have to trust "
            "whatever is resident, which is the defect it exists to close.")
    doc = json.loads(p.read_text(encoding="utf-8"))
    mods = doc.get("protected_modules")
    if not isinstance(mods, list) or not mods:
        raise ProvenanceError("the protected-module set declares no modules; an empty protected "
                              "set would pass vacuously")
    for m in mods:
        for field in ("module", "relative_path", "why_protected"):
            if field not in m:
                raise ProvenanceError(f"{m.get('module', '<unnamed>')}: missing {field!r}")
    return doc


def _canonical_const(const):
    """A representation of a constant that does not depend on set iteration order.

    GATE 4N-I28AG, fixing a latent defect introduced here at Gate 4N-I28AE. CPython folds a set
    literal used with `in` into a frozenset constant, and two frozensets that compare EQUAL can
    still repr in different orders — measured on `startup_policy.check`, where the disk compile and
    the resident code object produced different orderings of the same nine strings in the same
    process. The old fingerprint hashed repr() directly, so any protected code object containing a
    set constant would intermittently be reported as "executing code that differs from the pinned
    bytes": a false refusal, in a control whose whole value is that its refusals mean something.
    It never fired before only because none of the four original protected modules had such a
    constant in a critical callable.
    """
    if isinstance(const, (set, frozenset)):
        kind = "set" if isinstance(const, set) else "frozenset"
        return f"{kind}({sorted(repr(v) for v in const)})"
    if isinstance(const, tuple):
        return "(" + ", ".join(_canonical_const(v) for v in const) + ")"
    return repr(const)


def _code_fingerprint(code) -> str:
    """A stable identity for one code object, from what it DOES rather than where it lives.

    co_filename and co_firstlineno are deliberately excluded: they are attacker-settable and say
    nothing about behaviour. co_consts is reduced to its scalar shape because it may contain
    nested code objects, which are fingerprinted separately under their own qualified names.
    """
    scalars = tuple(_canonical_const(c) for c in code.co_consts if not hasattr(c, "co_code"))
    payload = json.dumps({
        "qualname": code.co_qualname,
        "code": code.co_code.hex(),
        "names": list(code.co_names),
        "varnames": list(code.co_varnames),
        "consts": list(scalars),
        "argcount": code.co_argcount,
        "kwonlyargcount": code.co_kwonlyargcount,
        "flags": code.co_flags,
    }, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def _walk_compiled(code, out: dict) -> None:
    for const in code.co_consts:
        if hasattr(const, "co_code"):
            out[const.co_qualname] = _code_fingerprint(const)
            _walk_compiled(const, out)


def disk_code_identity(path: Path) -> dict:
    """Identity of the code a fresh compile of the pinned bytes WOULD produce."""
    src = Path(path).read_bytes()
    top = compile(src, str(path), "exec")
    out: dict[str, str] = {}
    _walk_compiled(top, out)
    return out


def _reachable_code_objects(module) -> dict:
    """Identity of the code objects actually RESIDENT in the running module.

    Enumerated from the module namespace, then descended into classes, so methods are covered.
    Only code compiled from this module's own file is considered; imported callables belong to
    their own module's protected entry, if any.
    """
    origin = getattr(module, "__file__", None)
    out: dict[str, str] = {}

    def consider(obj):
        code = getattr(obj, "__code__", None)
        if code is not None and getattr(code, "co_filename", None) == origin:
            out[code.co_qualname] = _code_fingerprint(code)
            _walk_compiled(code, out)

    for value in vars(module).values():
        consider(value)
        if isinstance(value, type):
            for attr in vars(value).values():
                consider(attr)
                consider(getattr(attr, "__func__", None))
    return out


def runtime_code_identity(module) -> dict:
    return _reachable_code_objects(module)


def digest_of(identity: dict) -> str:
    """A digest over one identity map.

    NOTE ON ASYMMETRY, stated because it would otherwise look like a bug. The disk map is every
    code object a compile produces, including class bodies and module-level comprehensions. The
    runtime map is every code object REACHABLE from the module namespace, which excludes class
    bodies (they execute once and are not retained). The two digests are therefore recorded as
    evidence and are NOT required to be equal — equality is required where it is meaningful: on
    the shared code objects, and on the authored critical callables, which must additionally be
    PRESENT. Requiring whole-map equality would fail on every honest module and teach the next
    reader to relax the check.
    """
    return hashlib.sha256(
        json.dumps(identity, sort_keys=True).encode()).hexdigest()


def verify(protected: dict | None = None, *, modules=None) -> dict:
    """Compare executed code against the pinned bytes for every protected module."""
    doc = protected if protected is not None else load_protected_set()
    if not (doc.get("protected_modules") or []):
        raise ProvenanceError("the protected-module set is empty; an empty protected set would "
                              "verify vacuously")
    mods = modules if modules is not None else sys.modules
    problems: list[str] = []
    results = []

    for entry in doc["protected_modules"]:
        name, rel = entry["module"], entry["relative_path"]
        path = REPO_ROOT / rel
        record = {"module": name, "relative_path": rel, "resident": name in mods}
        if not path.is_file():
            problems.append(f"{name}: pinned path {rel} is not in the tree")
            results.append(record)
            continue
        record["disk_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        module = mods.get(name)
        if module is None:
            if entry.get("required_resident", True):
                problems.append(
                    f"{name}: protected module is not resident, so no executed-code identity "
                    "exists to verify. Provenance that cannot be established fails closed.")
            results.append(record)
            continue

        origin = getattr(module, "__file__", None)
        record["runtime_origin"] = origin
        if origin is None or Path(origin).resolve() != path.resolve():
            problems.append(
                f"{name}: resident module reports origin {origin!r}, which is not the pinned "
                f"path {path}. An unexpected origin is refused.")
            results.append(record)
            continue

        try:
            disk = disk_code_identity(path)
            runtime = runtime_code_identity(module)
        except (SyntaxError, OSError) as exc:
            problems.append(f"{name}: runtime provenance could not be established ({exc})")
            results.append(record)
            continue

        record["disk_code_digest"] = digest_of(disk)
        record["runtime_code_digest"] = digest_of(runtime)
        record["code_objects_disk"] = len(disk)
        record["code_objects_runtime"] = len(runtime)

        shared = set(disk) & set(runtime)
        mismatched = sorted(q for q in shared if disk[q] != runtime[q])
        missing = sorted(set(runtime) - set(disk))
        # AUTHORED CRITICAL CALLABLES. Comparing only the intersection would miss a monkey-patch:
        # replacing MandatorySessionGuard.pytest_sessionfinish with a lambda defined elsewhere
        # removes it from the runtime map entirely, so it is simply absent from the intersection
        # and nothing disagrees. Each protected entry therefore names the callables that MUST be
        # present and MUST match, and absence is a problem in its own right.
        critical = list(entry.get("critical_callables") or [])
        missing_critical = sorted(q for q in critical if q not in runtime)
        wrong_critical = sorted(q for q in critical
                                if q in runtime and q in disk and runtime[q] != disk[q])
        record["shared_code_objects"] = len(shared)
        record["mismatched"] = mismatched
        record["runtime_only"] = missing
        record["critical_callables"] = len(critical)
        record["missing_critical"] = missing_critical
        record["wrong_critical"] = wrong_critical
        if missing_critical:
            problems.append(
                f"{name}: critical callable(s) {missing_critical} are NOT present in the executing "
                "module. A replaced or monkey-patched implementation is absent from the runtime "
                "code map rather than different from it, so absence is refused too.")
        if wrong_critical:
            problems.append(
                f"{name}: critical callable(s) {wrong_critical} execute code that differs from "
                "the pinned bytes")

        if not shared:
            problems.append(
                f"{name}: no code object could be matched between the pinned bytes and the "
                "resident module, so provenance is unresolvable. Fails closed.")
        if mismatched:
            problems.append(
                f"{name}: {len(mismatched)} code object(s) EXECUTING differ from the pinned "
                f"bytes on disk: {mismatched[:4]}. This is the Gate 4N-I28AC condition — the "
                "staged file is not the code that ran.")
        if missing:
            problems.append(
                f"{name}: resident module defines code the pinned bytes do not: {missing[:4]}")
        results.append(record)

    return {"protected_modules": len(doc["protected_modules"]),
            "results": results, "problems": problems, "clean": not problems,
            "protected_set_sha256": hashlib.sha256(
                (PROTECTED_SET.read_bytes() if PROTECTED_SET.is_file() else b"")).hexdigest()}


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    try:
        for entry in load_protected_set()["protected_modules"]:
            __import__(entry["module"])
        result = verify()
    except ProvenanceError as exc:
        print(f"  {exc}", file=sys.stderr)
        print("EXECUTED CODE PROVENANCE: refused")
        return 2
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"  {result['protected_modules']} protected module(s); "
              f"problems {len(result['problems'])}")
        for p in result["problems"]:
            print(f"    {p}")
    print("EXECUTED CODE PROVENANCE: " + ("clean" if result["clean"] else "PROBLEMS"))
    return 0 if result["clean"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
