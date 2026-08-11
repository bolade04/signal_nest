#!/usr/bin/env python3
"""Executed-STATE provenance for the protected Gate 4N modules (Gate 4N-I28AG).

THE DEFECT THIS CLOSES. Gate 4N-I28AF finding ADV-I28AF-01. Gate 4N-I28AE bound the code objects
that actually execute, which closed substitution of the code itself. It did not bind what that code
READS. A critical callable's behaviour is a function of both, and the measured consequence was a
full bypass: exec the REAL pinned bytes of pytest_session_guard so every code object is
byte-identical, then rebind one module-level global — REGISTRY — to a decoy registry holding one
node instead of twelve. Executed-code provenance reported clean for all four protected modules while
the graded session ran to 2931 passed / exit 0 with eleven of twelve mandatory nodes absent.

WHY A LIST OF NAMES WOULD NOT HAVE BEEN ENOUGH. The obvious fix is an authored list of globals to
bind. That fix fails the same way the thing it replaces failed: the list is written by hand, so the
one name that matters can be left off it, and nothing notices. The required set here is DERIVED
instead — from the co_names of the critical callables, expanded transitively through the
module-level callables they reach. The authored contract pins the resulting tokens. A reachable name
the contract does not cover fails closed, and a pinned name that stops being reachable fails closed
too, so the contract cannot drift away from the code in either direction.

WHAT A TOKEN BINDS, by the kind of thing the name is bound to:
  * a module           its name and its resolved origin, so rebinding `os` to a decoy is refused
  * a callable of this module   its code identity plus __defaults__ and __kwdefaults__, because a
                       changed default changes behaviour with the code object untouched
  * a class of this module      the code identity of every method it defines
  * a path that names a real file   the repository-relative path AND the sha256 of the file's
                       CONTENT. This is the TOCTOU-resistant case the gate asks for: what is bound
                       is the material actually consumed, not a name that could be re-read later
                       from an unbound source.
  * an imported callable  its defining module and qualified name
  * anything else      its repr

ENVIRONMENT STATE. Names are not the only mutable input. Every environment variable the protected
modules consult is declared in the contract with a required disposition, because an override like
SIGNALNEST_MANDATORY_NODES redirects the guard's authority without touching a single byte of code
or any module attribute.

WHAT IS HONESTLY NOT CLAIMED. This binds the state reachable from the critical callables of the
protected set. It does not bind state reached through a name the code never mentions — for example
an attribute fetched entirely dynamically by a computed string. Such a fetch is invisible to
co_names, and the honest answer is that this control does not see it either; that is why
`getattr`-style dynamic attribute access on a protected module is reported as UNRESOLVED and fails
closed rather than being quietly treated as absent.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
STATE_CONTRACT = REPO_ROOT / "tests" / "fixtures" / "executed-state-contract.json"

# Attribute fetches that could reach module state without naming it. Presence of the call is NOT
# the signal: `getattr(report, "wasxfail", None)` reads a pytest object, not this module, and
# treating every getattr as unresolvable would refuse every honest module — the same shape of
# defect as Gate 4N-I28AE's presence-is-fatal bootstrap. What matters is the TARGET, so the check
# below is an AST analysis of the first argument, not a co_names membership test.
DYNAMIC_ACCESS = ("getattr", "setattr", "delattr", "vars")

# Dunder names carry no load-bearing state of their own: __file__ and __name__ are already bound by
# the origin check in executed_code_provenance, and __dict__ is the namespace this module walks.
IGNORED_NAMES = ("__file__", "__name__", "__dict__", "__doc__", "__spec__", "__loader__")


class StateProvenanceError(RuntimeError):
    """Fail closed. State that cannot be resolved is never reported as verified."""


def load_contract(path: Path | None = None) -> dict:
    p = path or STATE_CONTRACT
    if not p.is_file():
        raise StateProvenanceError(
            f"the executed-state contract is missing at {p}. It is the authored pin of every "
            "load-bearing value the protected modules read; without it this control would have to "
            "trust whatever the running process happens to hold, which is the defect it closes.")
    doc = json.loads(p.read_text(encoding="utf-8"))
    mods = doc.get("modules")
    if not isinstance(mods, dict) or not mods:
        raise StateProvenanceError(
            "the executed-state contract pins no module; an empty contract would verify vacuously")
    for name, entry in mods.items():
        for field in ("names", "environment", "why_load_bearing"):
            if field not in entry:
                raise StateProvenanceError(f"{name}: contract entry is missing {field!r}")
    return doc


# --------------------------------------------------------------------------- code identity
def _order_free_const(const):
    """A constant's representation with set iteration order removed.

    GATE 4N-I28AM. Gate 4N-I28AG fixed exactly this defect in executed_code_provenance and it was
    never fixed here, because the two layers keep separate implementations ON PURPOSE — a shared
    helper would let one defect blind both. The cost of that independence is that a fix has to be
    made twice, and this one was not. CPython folds a set literal used with `in` into a frozenset
    constant, and two frozensets that compare EQUAL can repr in different orders, so `repr(c)` is
    PYTHONHASHSEED-dependent. Measured on `startup_policy.check`: three seeds, three different
    tokens, each stable within its own process.

    It stayed latent because no PINNED name had such a constant. Pinning the critical callables
    themselves (see reachable_names) made `startup_policy.check` a pinned name for the first time
    and the non-determinism surfaced immediately — as a refusal on an unmodified tree, which is
    the honest way for it to surface. Written independently of the sibling implementation.
    """
    if isinstance(const, (set, frozenset)):
        kind = "set" if isinstance(const, set) else "frozenset"
        return kind + "{" + "|".join(sorted(map(repr, const))) + "}"
    if isinstance(const, tuple):
        return "(" + "|".join(_order_free_const(v) for v in const) + ")"
    return repr(const)


def _code_token(code) -> str:
    """Identity of one code object, from what it does rather than where it lives.

    Deliberately independent of executed_code_provenance: this control must still mean something
    if that one is wrong, and a shared helper would make a single defect blind both layers.
    """
    scalars = tuple(_order_free_const(c) for c in code.co_consts if not hasattr(c, "co_code"))
    payload = json.dumps({
        "code": code.co_code.hex(),
        "names": list(code.co_names),
        "varnames": list(code.co_varnames),
        "consts": list(scalars),
        "argcount": code.co_argcount,
        "kwonlyargcount": code.co_kwonlyargcount,
        "flags": code.co_flags,
    }, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


def _relative(path: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(REPO_ROOT))
    except ValueError:
        return f"<outside-repo>{Path(path).name}"


def _file_token(path: Path) -> str:
    """Bind the CONTENT, not the name. This is what makes the binding TOCTOU-resistant."""
    p = Path(path)
    if not p.is_file():
        raise StateProvenanceError(f"state file {p} named by a load-bearing value does not exist")
    return f"FILE:{_relative(p)}:{hashlib.sha256(p.read_bytes()).hexdigest()[:32]}"


def _defaults_token(obj) -> str:
    d = getattr(obj, "__defaults__", None)
    k = getattr(obj, "__kwdefaults__", None)
    return f"D{repr(d)}|K{repr(sorted(k.items())) if k else 'None'}"


# --------------------------------------------------------------------------- import provenance
# GATE 4N-I28BH-E4. What follows separates two provenance questions the earlier design conflated
# under one signal — the file's PATH.
#
#   E4-B THIRD_PARTY_LOCATION_CLASSIFICATION. local_import_tokens classified a function-local import
#   as repository-owned versus third-party purely by `str(REPO_ROOT) in str(origin)`. A virtual
#   environment created UNDER the workspace (CI builds `.reader-venv` at the repo root) is physically
#   inside the tree yet holds externally-installed packages, so PyYAML's __init__.py was bound as a
#   repository FILE and drifted from the pinned `MODULE:yaml@<outside-repo>`. Location is not
#   provenance: a package is third-party because an INSTALLED DISTRIBUTION provides it and owns the
#   bytes actually imported, established here from importlib.metadata rather than from where the file
#   happens to sit. A hand-dropped `yaml.py` with no distribution, or a module shadowing a real one
#   on sys.path, is owned by no distribution's file manifest and is refused. The token records the
#   distribution name and version, so a version change is a visible drift rather than a silent one.
#
#   E4-A CALLABLE_FINGERPRINT_PORTABILITY. local_import_tokens bound every function-local callable,
#   including stdlib and third-party ones, by raw code identity (_code_token hashes co_code). CPython
#   serialises the same source to different bytecode across interpreter patch levels, so a contract
#   pinned under one 3.12.x refused an unmodified subprocess.run under another — a state drift on code
#   nobody changed, the same class of accidental non-determinism I28AM/_canonical already excise. Our
#   OWN modules stay exact-byte bound (we author them; their bytecode is a function of our commit).
#   A callable whose source lives OUTSIDE the repository is bound instead by its version-independent
#   semantic identity — module, qualified name, provenance of its defining file, signature and
#   defaults. That still refuses a monkeypatch, a wrapper, a different module, a callable compiled
#   from a different file, or a name/qualname spoof, because each of those changes one of those
#   fields; what it deliberately no longer asserts is the patch-level bytecode of code the repository
#   does not own, which is the interpreter/toolchain layer's to pin, not this one's.


def _resolve_origin(origin) -> Path | None:
    try:
        return Path(origin).resolve() if origin else None
    except (OSError, RuntimeError, ValueError):
        return None


def _under_repo(path: Path | None) -> bool:
    """True only for a real path in the repository SOURCE tree, not an isolated env under it.

    A directory carrying a pyvenv.cfg is a virtual environment; anything at or below it is
    installed, not authored, and must earn third-party provenance from its distribution rather than
    inherit repository trust from a path prefix. The walk stops at REPO_ROOT so a pyvenv.cfg placed
    ABOVE the repository cannot flip the classification of the repository's own files.
    """
    if path is None:
        return False
    try:
        path.relative_to(REPO_ROOT)
    except ValueError:
        return False
    for parent in [path, *path.parents]:
        if parent == REPO_ROOT:
            break
        if (parent / "pyvenv.cfg").is_file():
            return False
    return True


def _under_interpreter(path: Path | None) -> bool:
    """True when the file lives under this interpreter's own installation (its stdlib)."""
    if path is None:
        return False
    for base in {sys.base_prefix, sys.prefix}:
        try:
            root = Path(base).resolve()
        except (OSError, RuntimeError):
            continue
        if path == root or root in path.parents:
            return True
    return False


def _owning_distribution(import_name: str, origin: Path | None):
    """(distribution_name, version) iff an installed distribution PROVIDES import_name AND OWNS origin.

    Independent evidence (Gate 4N-I28BH-E4 §13): the answer comes from installed-distribution
    metadata, not from the file's location and not from any self-claim by the module. packages_
    distributions() maps the top-level import to the distributions that declare it, and the file
    actually imported must appear in that distribution's own file manifest — so a module shadowing a
    real one on sys.path, or a bare file with no distribution behind it, is not owned and returns
    None. The version travels in the token, so a drift from the pinned dependency is visible here.
    """
    if not import_name or origin is None:
        return None
    try:
        import importlib.metadata as md
    except Exception:                                               # noqa: BLE001
        return None
    try:
        mapping = md.packages_distributions()
    except Exception:                                               # noqa: BLE001
        return None
    for dist_name in dict.fromkeys(mapping.get(import_name, []) or []):
        try:
            dist = md.distribution(dist_name)
            owned = {
                _resolve_origin(dist.locate_file(f))
                for f in (dist.files or [])
            }
        except Exception:                                          # noqa: BLE001
            continue
        if origin in owned:
            return dist_name, dist.version
    return None


def _localimport_token(import_name: str, imported) -> str:
    """Provenance of a function-local import, keyed on WHO PROVIDES it, not on WHERE it sits."""
    origin = getattr(imported, "__file__", None)
    resolved = _resolve_origin(origin)
    dist = _owning_distribution(import_name, resolved)
    if dist is not None:
        return f"DIST:{import_name}@{dist[0]}=={dist[1]}"
    if not origin:
        return f"MODULE:{import_name}@<builtin>"
    if resolved is not None and _under_repo(resolved):
        digest = hashlib.sha256(resolved.read_bytes()).hexdigest()[:32]
        return f"FILE:{_relative(resolved)}:{digest}"
    # Not distribution-owned and not repository source: the interpreter's own stdlib, or a file
    # outside the tree that no distribution claims. Bound by name and basename as before — this is
    # the branch stdlib imports (ast, subprocess, ...) have always taken, unchanged.
    return f"MODULE:{import_name}@<outside-repo>{Path(origin).name}"


def _callable_provenance(value) -> str:
    """A version-independent classification of where an imported callable's code comes from."""
    code = getattr(value, "__code__", None)
    if code is None:
        return "BUILTIN"
    cf = _resolve_origin(code.co_filename)
    if cf is None or not cf.is_file():
        # A co_filename that names no real file (e.g. '<string>', a compiled-in name) cannot be
        # attributed to the stdlib or a distribution, so it is never treated as trusted-external.
        return "SYNTHETIC"
    if _under_repo(cf):
        return f"REPO:{_relative(cf)}"
    top = str(getattr(value, "__module__", "") or "").split(".")[0]
    dist = _owning_distribution(top, cf) if top else None
    if dist is not None:
        return f"DIST:{dist[0]}=={dist[1]}"
    if _under_interpreter(cf):
        return "STDLIB"
    return f"OUTSIDE:{cf.name}"


def _extern_callable_token(value) -> str:
    """Version-independent identity for a callable the repository does not author.

    Binds the semantic surface that is stable across interpreter patch levels and that an attacker
    must change to substitute behaviour: defining module, qualified name, the provenance of the
    defining file, the call signature, and the defaults. It does NOT bind co_code, which is the
    field that legitimately differs between CPython builds of identical source.
    """
    import inspect

    mod = getattr(value, "__module__", "?")
    qn = getattr(value, "__qualname__", getattr(value, "__name__", "?"))
    prov = _callable_provenance(value)
    try:
        sig = str(inspect.signature(value))
    except (ValueError, TypeError):
        sig = "<no-signature>"
    return f"EXTERNCALLABLE:{mod}.{qn}@{prov}|sig={sig}|{_defaults_token(value)}"


# A module-level MEMOISATION CACHE is reachable state whose CONTENT is a function of already-pinned
# code and already-pinned files. Pinning its content would pin a value that legitimately differs
# between "before any derivation ran" and "after", so the token cannot bind content.
#
# GATE 4N-I28AR REDEFINES WHAT THIS EXCLUSION MEANS. Finding ADV-I28AP-03: the token used to bind
# the cache's TYPE, and poisoning preserves the type — the poisoned dict is still a dict — so
# emptying `_DERIVED` took release roots 41 -> 0 and production sites 492 -> 0 with every layer
# clean. The old reading was effectively "mutable, therefore excused".
#
# VOLATILE_CACHE now means: mutable, NON-AUTHORITATIVE, and independently recomputed or
# identity-verified. An exclusion must EARN itself — `cache_authority` must classify the cache, and
# the token records that CLASSIFICATION rather than the type, so silently reclassifying a cache as
# something more trusted is a contract drift rather than an invisible edit. A declared cache that
# nothing classifies is refused outright: that is the precise hole this finding came through.
#
# Still declared per module rather than inferred, because "looks like a cache" is exactly the kind
# of guess this chain has been burned by.
VOLATILE_CACHES = {
    "site_taxonomy": ("_DERIVED", "_INDEX"),
    # GATE 4N-I28AR: the cache-authority layer's own identity pins. Content cannot be pinned — the
    # dict is empty in a fresh interpreter and populated the moment anything calls pin() — and a
    # layer that exempted its own mutable state from the rule it enforces on every other module
    # would be the same defect one level up.
    "cache_authority": ("_PINS",),
    # GATE 4N-I28BF-B1: the governed Docker assurance-state cache. Its CONTENT cannot be pinned — it
    # is empty in a fresh interpreter and populated the moment establish_state() stores the validated
    # baseline — so it is exempt from VALUE pinning here and instead classified and content-bound by
    # cache_authority (AUTHORITATIVE_CONTENT_BOUND_CACHE), which the _volatile_token check requires.
    "docker_assurance_state": ("_STATE_CACHE",),
}


def state_token(value, *, owner_file: str) -> str:
    """The bound identity of one load-bearing value."""
    import types

    if isinstance(value, types.ModuleType):
        origin = getattr(value, "__file__", None)
        return f"MODULE:{value.__name__}@{_relative(Path(origin)) if origin else '<builtin>'}"

    if isinstance(value, type):
        methods = {}
        for name, attr in sorted(vars(value).items()):
            code = getattr(attr, "__code__", None) or getattr(
                getattr(attr, "__func__", None), "__code__", None)
            if code is not None:
                methods[name] = _code_token(code) + "|" + _defaults_token(attr)
        if value.__module__ != Path(owner_file).stem and not methods:
            return f"EXTERN_CLASS:{value.__module__}.{value.__qualname__}"
        # The qualified name is part of the token on purpose: two distinct exception classes with
        # no methods otherwise hash identically, and swapping one for the other silently changes
        # which `except` clauses match.
        return f"CLASS:{value.__qualname__}:" + hashlib.sha256(
            json.dumps(methods, sort_keys=True).encode()).hexdigest()[:32]

    code = getattr(value, "__code__", None)
    if code is not None:
        if code.co_filename != owner_file:
            return f"EXTERN:{getattr(value, '__module__', '?')}.{getattr(value, '__qualname__', '?')}"
        return f"CALLABLE:{_code_token(code)}|{_defaults_token(value)}"

    if callable(value):                       # builtins, C functions, partials, descriptors
        return (f"EXTERN:{getattr(value, '__module__', '?')}."
                f"{getattr(value, '__qualname__', getattr(value, '__name__', '?'))}")

    if isinstance(value, Path) or (isinstance(value, str) and os.sep in str(value)):
        candidate = Path(value)
        # A contract cannot bind its own content: writing the hash changes the hash. This is the
        # self-reference ordering trap this chain has now hit three times (Gate 4N-I28Q, I28AE,
        # and here). Rather than pretend, the self-reference is named as such and its digest is
        # pinned OUTSIDE this file, by the I28AG test module.
        try:
            if candidate.resolve() == STATE_CONTRACT.resolve():
                return f"SELFREF:{_relative(candidate)}"
        except (OSError, RuntimeError):
            pass
        if candidate.is_file():
            return _file_token(candidate)
        if candidate.is_dir():
            # A directory has no content to bind; what matters is WHICH directory, and a rebound
            # REPO_ROOT pointing somewhere else changes every path derived from it.
            return f"DIR:{_relative(candidate)}"
        return f"PATH:{_relative(candidate)}:<absent>"

    return f"VALUE:{_canonical(value)}"


def _canonical(value) -> str:
    """A repr that does not depend on this process's hash seed.

    set and frozenset iterate in hash order, and PYTHONHASHSEED randomises string hashing per
    process, so repr() of a set of strings differs between two runs of identical code. Pinning
    that would produce a control that fails at random — which reads as a real state drift and
    trains the next reader to re-pin without looking. Sorting makes the token depend on the
    membership, which is the thing that is actually load-bearing.
    """
    if isinstance(value, (set, frozenset)):
        kind = "set" if isinstance(value, set) else "frozenset"
        return f"{kind}({sorted(repr(v) for v in value)})"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{k!r}: {_canonical(v)}"
                               for k, v in sorted(value.items(), key=lambda kv: repr(kv[0]))) + "}"
    if isinstance(value, (list, tuple)):
        joined = ", ".join(_canonical(v) for v in value)
        return f"[{joined}]" if isinstance(value, list) else f"({joined})"
    return repr(value)


# --------------------------------------------------------------------------- reachability
def _code_for(module, qualname: str):
    obj = module
    for part in qualname.split("."):
        obj = vars(obj).get(part) if isinstance(obj, type) else getattr(obj, part, None)
        if obj is None:
            return None
    return getattr(obj, "__code__", None) or getattr(
        getattr(obj, "__func__", None), "__code__", None)


def local_imports(module, critical_callables) -> tuple[dict, list]:
    """Modules imported INSIDE the reachable code, with the attributes read from each.

    GATE 4N-I28AO, closing ADV-I28AN-02. The name walk collects module-level globals, so a module
    imported inside a function was pinned nowhere. `external_executable_trust`'s npm precondition
    does exactly that — `import site_taxonomy` inside `_graded_reachability_problems` — and
    replacing `site_taxonomy.release_roots` with `lambda: []` disarmed the precondition while
    executed-code, executed-state and the trust layer all reported clean.

    Derived from bytecode, never authored: IMPORT_NAME gives the module, and the LOAD_ATTR that
    follows on the imported binding gives the attribute actually read. A module that cannot be
    resolved is a PROBLEM, not an omission.
    """
    import dis
    import importlib

    found: dict = {}
    problems: list[str] = []
    seen: set = set()
    frontier = []
    for qual in critical_callables:
        code = _code_for(module, qual)
        if code is not None:
            frontier.append(code)
    while frontier:
        code = frontier.pop()
        if id(code) in seen:
            continue
        seen.add(id(code))
        instructions = list(dis.get_instructions(code))
        for index, ins in enumerate(instructions):
            if ins.opname == "IMPORT_NAME":
                target = str(ins.argval).split(".")[0]
                attrs = found.setdefault(target, set())
                # the attributes read from this import, taken from the instructions that follow
                for follow in instructions[index + 1:]:
                    if follow.opname in ("LOAD_ATTR", "IMPORT_FROM"):
                        attrs.add(str(follow.argval))
                    if follow.opname == "IMPORT_NAME":
                        break
            if ins.opname == "LOAD_CONST" and hasattr(ins.argval, "co_code"):
                frontier.append(ins.argval)
        for const in code.co_consts:
            if hasattr(const, "co_code"):
                frontier.append(const)
        for name in code.co_names:
            value = getattr(module, name, None)
            inner = getattr(value, "__code__", None)
            if inner is not None and inner.co_filename == getattr(module, "__file__", None):
                frontier.append(inner)
    return found, problems


def local_import_tokens(module, critical_callables) -> tuple[dict, list]:
    """A pinnable token per function-local import, and per attribute read from it."""
    import importlib

    imports, problems = local_imports(module, critical_callables)
    tokens: dict = {}
    for target in sorted(imports):
        try:
            imported = importlib.import_module(target)
        except Exception as exc:                                    # noqa: BLE001
            problems.append(f"local import {target}: cannot be resolved ({exc}); an import the "
                            "verifier cannot follow is refused rather than assumed benign")
            continue
        tokens[f"LOCALIMPORT:{target}"] = _localimport_token(target, imported)
        for attr in sorted(imports[target]):
            value = getattr(imported, attr, None)
            if value is None or not callable(value):
                continue
            code = getattr(value, "__code__", None)
            if code is None:
                continue
            key = f"LOCALCALLABLE:{target}.{attr}"
            cf = _resolve_origin(code.co_filename)
            if cf is not None and _under_repo(cf):
                # Our own code, imported function-locally: bound to its exact bytes, because we
                # author it and its bytecode is a function of the committed source, not the build.
                tokens[key] = f"CALLABLE:{_code_token(code)}|{_defaults_token(value)}"
            else:
                # stdlib / third-party: version-independent semantic + provenance identity.
                tokens[key] = _extern_callable_token(value)
    return tokens, problems


def reachable_names(module, critical_callables) -> tuple[set, list]:
    """Every module-level name the critical callables can read, plus unresolved-access problems.

    Derived, never authored. Expanded to a fixpoint through module-level callables and classes
    defined in this module, so a global read by a helper that a critical callable calls is
    reachable too — which is exactly how load_registry reaches REGISTRY.
    """
    problems: list[str] = []
    seen_code: set = set()
    names: set = set()
    frontier = []

    for qual in critical_callables:
        code = _code_for(module, qual)
        if code is None:
            problems.append(f"critical callable {qual} is not present, so its reachable state "
                            "cannot be derived")
            continue
        # GATE 4N-I28AM. A critical callable is itself load-bearing state, and until now the roots
        # of this walk were the one thing it never pinned. Names were collected only from co_names
        # — the globals some code REFERENCES — so a callable invoked only from OUTSIDE its own
        # module got no token at all. `pytest_sessionfinish` is called by pluggy and
        # `executed_code_provenance.verify` is called by the bootstrap; neither is named anywhere
        # inside its own module, and replacing either body on disk passed all six layers clean.
        # `reverify` was caught only because `pytest_sessionfinish` happens to call it by name.
        # Detection must not depend on that accident.
        if "." not in qual and hasattr(module, qual):
            names.add(qual)
        frontier.append((qual, code))

    while frontier:
        qual, code = frontier.pop()
        if id(code) in seen_code:
            continue
        seen_code.add(id(code))
        for name in code.co_names:
            if name in IGNORED_NAMES or not hasattr(module, name):
                continue
            names.add(name)
            value = getattr(module, name)
            inner = getattr(value, "__code__", None)
            if inner is not None and inner.co_filename == getattr(module, "__file__", None):
                frontier.append((name, inner))
            elif isinstance(value, type):
                for mname, attr in vars(value).items():
                    mcode = getattr(attr, "__code__", None) or getattr(
                        getattr(attr, "__func__", None), "__code__", None)
                    if mcode is not None and mcode.co_filename == getattr(module, "__file__", None):
                        frontier.append((f"{name}.{mname}", mcode))
        for const in code.co_consts:
            if hasattr(const, "co_code"):
                frontier.append((qual, const))

    problems.extend(unbounded_access(module))
    return names, problems


def unbounded_access(module) -> list:
    """Dynamic reads that could reach this module's own namespace without naming the attribute.

    Analysed from the module's source, because only the AST shows what the first argument IS.
    A `getattr` whose target is a parameter or an attribute chain reads some other object and is
    irrelevant here; a `getattr` whose target is a module-level name of THIS module, or a bare
    `globals()` used for anything but a dunder lookup, could read load-bearing state that co_names
    never records — and that is the one case this control cannot bound, so it says so.
    """
    import ast

    origin = getattr(module, "__file__", None)
    if not origin or not Path(origin).is_file():
        return [f"{getattr(module, '__name__', '?')}: no readable source, so dynamic access "
                "cannot be ruled out"]
    try:
        tree = ast.parse(Path(origin).read_text(encoding="utf-8"))
    except SyntaxError as exc:
        return [f"{getattr(module, '__name__', '?')}: source does not parse ({exc})"]

    module_names = {n for n in dir(module) if not n.startswith("__")}
    problems = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        fn = node.func.id
        if fn in DYNAMIC_ACCESS and node.args:
            target = node.args[0]
            if isinstance(target, ast.Name) and target.id in module_names:
                problems.append(
                    f"line {node.lineno}: {fn}({target.id}, …) reads a module-level object "
                    "dynamically, so its reachable state cannot be statically bounded")
        if fn == "globals":
            parent_ok = False
            for outer in ast.walk(tree):
                for attr in ("value", "func"):
                    inner = getattr(outer, attr, None)
                    if inner is node and isinstance(outer, ast.Attribute) and outer.attr == "get":
                        parent_ok = True
            if not parent_ok:
                problems.append(
                    f"line {node.lineno}: globals() exposes this module's namespace in a form this "
                    "control cannot bound")
    return problems


def _volatile_token(module, name, problems) -> str:
    """The token for a cache excluded from content pinning — and the exclusion must be earned.

    Gate 4N-I28AR. Binding the TYPE let ADV-I28AP-03 through, because every poisoning shape kept the
    type intact. This binds the AUTHORITY CLASSIFICATION instead, which means two things a type
    could not mean: an unclassified cache is refused rather than excused, and reclassifying a cache
    to something more trusted shows up here as a drift rather than passing silently.
    """
    qualified = f"{getattr(module, '__name__', '?')}.{name}"
    try:
        import cache_authority

        classification = cache_authority.load_policy()["caches"][qualified]["classification"]
    except KeyError:
        problems.append(
            f"{name}: declared a VOLATILE_CACHE but `cache_authority` does not classify "
            f"{qualified}. Since Gate 4N-I28AR an exclusion from content pinning must be earned by "
            "an explicit authority classification; 'mutable' is not by itself a reason to trust "
            "something, which is exactly how ADV-I28AP-03 reached a live trust decision.")
        return "VOLATILE_CACHE:UNCLASSIFIED"
    except Exception as exc:                                     # noqa: BLE001
        problems.append(f"{name}: the cache-authority policy could not be read ({exc}), so this "
                        "exclusion cannot be justified and fails closed")
        return "VOLATILE_CACHE:UNAVAILABLE"
    return f"VOLATILE_CACHE:{classification}"


def state_identity(module, critical_callables) -> tuple[dict, list]:
    names, problems = reachable_names(module, critical_callables)
    owner = getattr(module, "__file__", "")
    identity = {}
    volatile = VOLATILE_CACHES.get(getattr(module, "__name__", ""), ())
    for name in sorted(names):
        if name in volatile:
            identity[name] = _volatile_token(module, name, problems)
            continue
        try:
            identity[name] = state_token(getattr(module, name), owner_file=owner)
        except StateProvenanceError as exc:
            problems.append(f"{name}: {exc}")
    # GATE 4N-I28AO: what a critical callable imports INSIDE itself is state it reads.
    local_tokens, local_problems = local_import_tokens(module, critical_callables)
    identity.update(local_tokens)
    problems.extend(local_problems)
    return identity, problems


# --------------------------------------------------------------------------- verification
def verify(contract: dict | None = None, *, modules=None) -> dict:
    doc = contract if contract is not None else load_contract()
    mods = modules if modules is not None else sys.modules
    problems: list[str] = []
    results = []

    for name, entry in sorted(doc["modules"].items()):
        module = mods.get(name)
        record = {"module": name, "resident": module is not None}
        if module is None:
            problems.append(
                f"{name}: protected module is not resident, so no executed state exists to verify")
            results.append(record)
            continue

        identity, derive_problems = state_identity(module, entry.get("critical_callables") or [])
        problems.extend(f"{name}: {p}" for p in derive_problems)

        pinned = entry["names"]
        reachable, covered = set(identity), set(pinned)
        uncovered = sorted(reachable - covered)
        stale = sorted(covered - reachable)
        drifted = sorted(k for k in reachable & covered if identity[k] != pinned[k])

        record.update({"reachable": len(reachable), "pinned": len(covered),
                       "uncovered": uncovered, "stale": stale, "drifted": drifted,
                       "state_digest": hashlib.sha256(
                           json.dumps(identity, sort_keys=True).encode()).hexdigest()})

        if uncovered:
            problems.append(
                f"{name}: load-bearing state {uncovered} is reachable from a critical callable but "
                "is NOT covered by the executed-state contract. New load-bearing state may not "
                "enter without being pinned.")
        if stale:
            problems.append(
                f"{name}: contract pins {stale}, which the critical callables can no longer reach. "
                "A pin that binds nothing is refused rather than ignored.")
        for key in drifted:
            problems.append(
                f"{name}.{key}: executed state does NOT match the pinned contract "
                f"(pinned {pinned[key][:44]}…, executing {identity[key][:44]}…). This is the Gate "
                "4N-I28AF condition (ADV-I28AF-01): the code is unchanged and its state is not.")

        for var, requirement in sorted((entry.get("environment") or {}).items()):
            actual = os.environ.get(var)
            record.setdefault("environment", {})[var] = "<set>" if actual is not None else None
            if requirement == "MUST_BE_UNSET" and actual is not None:
                problems.append(
                    f"{name}: environment variable {var} is set, which redirects load-bearing state "
                    "without changing any code or module attribute. It must be unset in a graded "
                    "run.")
            if requirement == "MUST_RESOLVE_IN_TREE" and actual is not None:
                # A redirect to somewhere OUTSIDE the tree the module itself came from is the
                # attack: it substitutes authority the repository never contained. A redirect that
                # stays inside that tree is how the self-protection sandboxes drive the guard over
                # synthetic sessions, and refusing it would delete those controls rather than
                # strengthen anything.
                #
                # HONEST RESIDUAL, stated because this is a deliberate narrowing: inside the real
                # repository this permits an override naming a repository file. That file is not
                # unguarded — it must be tracked (the fixture-tracking control), coherent (package
                # coherence), and for the mandatory registry specifically its bytes are pinned by
                # tests/fixtures/mandatory-session-baseline.json. The narrowing trades an absolute
                # rule for one that does not disable the sandboxes, and names what now carries the
                # weight instead.
                try:
                    resolved = Path(actual).resolve()
                    inside = resolved == REPO_ROOT or REPO_ROOT in resolved.parents
                except (OSError, RuntimeError):
                    inside = False
                record.setdefault("environment", {})[var] = (
                    "<in-tree>" if inside else "<out-of-tree>")
                if not inside:
                    problems.append(
                        f"{name}: environment variable {var} redirects load-bearing state to "
                        f"{actual!r}, outside the tree this module was loaded from. An override "
                        "naming state the repository does not contain is refused.")
            if requirement == "MUST_BE_SET" and actual is None:
                problems.append(f"{name}: required environment variable {var} is not set")
        results.append(record)

    return {"modules": len(doc["modules"]), "results": results, "problems": problems,
            "clean": not problems,
            "contract_sha256": hashlib.sha256(
                STATE_CONTRACT.read_bytes() if STATE_CONTRACT.is_file() else b"").hexdigest()}


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Verify executed-state provenance.")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--emit", action="store_true",
                    help="print the derived identity, for authoring the contract")
    args = ap.parse_args(argv)

    protected = json.loads(
        (REPO_ROOT / "tests" / "fixtures" / "protected-module-set.json").read_text())
    for entry in protected["protected_modules"]:
        __import__(entry["module"])

    if args.emit:
        out = {}
        for entry in protected["protected_modules"]:
            module = sys.modules[entry["module"]]
            identity, problems = state_identity(module, entry.get("critical_callables") or [])
            out[entry["module"]] = {"names": identity, "problems": problems}
        print(json.dumps(out, indent=1, sort_keys=True))
        return 0

    try:
        result = verify()
    except StateProvenanceError as exc:
        print(f"  {exc}", file=sys.stderr)
        print("EXECUTED STATE PROVENANCE: refused")
        return 2
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"  {result['modules']} module(s); problems {len(result['problems'])}")
        for p in result["problems"]:
            print(f"    {p}")
    print("EXECUTED STATE PROVENANCE: " + ("clean" if result["clean"] else "PROBLEMS"))
    return 0 if result["clean"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
