#!/usr/bin/env python3
"""Production/control sites, derived from INVOCATION and CONSEQUENCE — never from a name.

THE DEFECT THIS CLOSES — Gate 4N-I28J, finding I28J-01.

`scripts/mutation_discovery.py` used to build the production/control half of the site universe
like this:

    DECISIVE_SUFFIXES = ("check", "verify", "reconcile", "run", "main", "report", ...)
    ...
    if not any(node.name.endswith(s) for s in DECISIVE_SUFFIXES):
        continue

Membership was a property of the SPELLING of a function's name, inside a set of files whose only
qualification was being mentioned literally in `ci.yml`. Gate 4N-I28J executed four mutations
against that rule and every one of them landed:

    rename a real, still-invoked control to a neutral name   127 -> 126   a control DISAPPEARS
    append a never-called `never_called_check`               127 -> 128   dead code ENTERS
    add a live, transitively-invoked enforcing helper        127 -> 127   a real control OMITTED
    delete the word "check" from DECISIVE_SUFFIXES           127 -> 116   11 controls LEAVE

Nothing about any guard's behaviour changed in the second or fourth case. Worse, the match was a
raw string suffix rather than a word: renaming `is_scannable` to `path_is_in_scan_domain` ADDED a
site, because `"domain".endswith("main")`.

The practical consequence was that `scripts/leak_scan.py` — the leak scanner — was represented in
the universe by `main` ALONE. `scan_text`, which IS the protected-token detector, `is_scannable`,
`scan_decision`, `candidate_files`, `scan_repository` and `scan_accounting` were all outside it,
so no assurance claim computed over the site universe covered any of them. Ten further modules of
enforcement logic reached only through imports — `iam_eval.py` and the policy generators among
them — were invisible for a second reason: the file filter listed only scripts named literally in
the workflow.

Gate 4N-I28I already replaced name-shape reasoning in the CONTROL/COLLECTION contract with
call-graph derivation (`scripts/enforcement_path.py`). It did not convert the SITE universe. That
asymmetry is what this module removes.

WHAT A SITE IS HERE. A production/control site is a point at which the release pipeline makes an
independent decision that can change what is examined, refused or reported:

    reachable   the function is reached, directly or transitively, from a function the workflow
                actually invokes — resolved across module boundaries, including imports
    consequent  the function can affect an outcome: it raises, exits, asserts, returns or yields
                a value, or mutates state its caller reads

Both are properties of the code, not of its spelling. A function nothing calls is not a site
however decisive its name reads; a function on the enforcement path is a site however neutral its
name reads. Renaming one, or moving it to another file, changes its recorded location and leaves
its membership and its enforcement fingerprint alone.

WHAT IS DELIBERATELY NOT HERE. There is no suffix list, no keyword list, and no per-module
allowlist to lengthen. Lengthening such a list was the defect, not the fix. The only authored
inputs are `TERMINAL_CALLS` and `MUTATING_METHODS`, which name PYTHON's own vocabulary for
"stops the process" and "changes a container" — properties of the language, not of this codebase.

FAIL CLOSED. A call this module cannot resolve to a definition, a guard with no derivable entry
point, and a duplicate canonical identity are all reported as problems and make `check()` fail.
An unresolved edge means the closure may be missing sites, and a taxonomy that silently reports a
smaller world is the failure mode this whole gate chain exists to prevent.
"""
from __future__ import annotations

from collections.abc import Mapping

import argparse
import ast
import json
import re
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"
TESTS = REPO_ROOT / "tests"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

#: Calls that end the process. Python's vocabulary for a terminal refusal, not this repo's.
TERMINAL_CALLS = frozenset({"exit", "_exit", "SystemExit"})

#: Methods that change a container in place. A function with no return can still decide something
#: by appending to a problems list its caller reads; treating that as "no consequence" is how a
#: real accumulating check gets collapsed away.
MUTATING_METHODS = frozenset({"append", "add", "update", "extend", "insert", "setdefault",
                              "pop", "remove", "discard", "write", "write_text", "write_bytes"})

#: Primary categories. A site has exactly one; secondary roles are recorded separately.
PRIMARY_CATEGORIES = ("PRODUCTION_CONTROL_SITE", "CI_RELEASE_CONTROL_SITE",
                      "COLLAPSED_PRESENTATION_HELPER", "UNREACHABLE_NOT_A_SITE")

#: Attribute names that need no resolution on a locally-constructed instance: they are inherited
#: object protocol rather than enforcement this module could follow.
_DUNDER_AND_STDLIB_SAFE = frozenset({"__class__", "__dict__", "__doc__"})

#: Framework base classes that dispatch to overrides by protocol rather than by an explicit call.
#: Python's own visitor protocol, not this repository's vocabulary.
FRAMEWORK_VISITOR_BASES = frozenset({"ast.NodeVisitor", "ast.NodeTransformer",
                                     "NodeVisitor", "NodeTransformer"})

#: Methods those dispatchers invoke by protocol.
FRAMEWORK_DISPATCH_PREFIXES = ("visit_",)
FRAMEWORK_DISPATCH_NAMES = frozenset({"generic_visit", "visit"})

#: Overrides OBSERVED dispatched by a real graded command. A protocol dispatcher can reach any
#: override for the node types it encounters, and the node-type set of an arbitrary visited tree
#: is not statically bounded — so candidacy is decided statically and EXERCISE is decided by
#: execution, recorded here and re-derived by tests/test_i28o_dynamic_edges.py. An override with
#: no observation is not a site; it is FRAMEWORK_DISPATCH_UNEXERCISED, which is a category.
_OBSERVED_DISPATCH = TESTS / "fixtures" / "framework-dispatch-observed.json"

DECIDES = "DECIDES"
COMPUTES = "COMPUTES"
PRESENTS = "PRESENTS"


class TaxonomyError(RuntimeError):
    """Fail-closed."""


# --------------------------------------------------------------------------- #
# module parsing
# --------------------------------------------------------------------------- #

_INDEX: dict[str, dict] = {}
_DERIVED: dict[str, object] = {}


def reset_caches() -> None:
    """Drop every parse and derivation. A test that rewrites a script must call this."""
    _INDEX.clear()
    _DERIVED.clear()
    # The shell model and the aggregator model cache their own parses. A mutation test that
    # rewrites ci.yml or a .sh file must drop those too, or the second probe silently answers
    # from the first probe's tree — the exact way a mutation harness reports a false green.
    try:
        import shell_command_model

        shell_command_model.reset_caches()
    except Exception:                                      # pragma: no cover - import-time only
        pass
    try:
        import failure_propagation

        if hasattr(failure_propagation, "reset_caches"):
            failure_propagation.reset_caches()
    except Exception:                                      # pragma: no cover
        pass


def _cached(key: str, produce):
    """Memoise a derivation, storing a value nobody can mutate afterwards.

    Gate 4N-I28AP finding ADV-I28AP-03 exploited this exact function. `_DERIVED` held live mutable
    structures, so emptying the cached lists took release roots 41 -> 0 and production sites 492 -> 0
    while executed-code provenance, executed-state provenance, startup policy, executable trust and
    session-finish reverification ALL reported clean — the sharpest shape mutating a list nested
    inside the dict without ever rebinding `_DERIVED`, which is why the `VOLATILE_CACHE:<type>` token
    could never have caught it: the poisoned dict is still a dict.

    Freezing here rather than at each of the six call sites is deliberate: this is the single
    population point for the cache, so a derivation added later is covered without anyone remembering
    to opt in. The authoritative recomputation in `cache_authority.verify()` remains the check on
    CONTENT; this makes the value itself unwritable.
    """
    if key not in _DERIVED:
        _store(key, produce())
    return _DERIVED[key]


def _store(key: str, value):
    """The ONLY way a derived value enters `_DERIVED`, and it enters frozen.

    `_cached` is not the single population point, contrary to what the Gate 4N-I28AR reconnaissance
    recorded: `resolved_roots`, `command_selections` and `production_unresolved` are assigned
    directly. `resolved_roots` is precisely the key ADV-I28AP-03 empties to take release roots
    41 -> 0, so freezing only inside `_cached` would have left the most load-bearing value in the
    cache mutable. Routing every write through one function is what makes the coverage checkable by
    reading rather than by remembering.
    """
    import cache_authority

    _DERIVED[key] = cache_authority.deep_freeze(value)
    return _DERIVED[key]


def _scripts_modules() -> set[str]:
    return {p.name for p in SCRIPTS.glob("*.py")}


def module_index(module: str) -> dict:
    """Functions, import bindings, module-level aliases and entry points of one script.

    Entry points are DERIVED: the functions called from the module's own ``__main__`` guard, which
    is what running ``python scripts/<module>`` actually executes. They are not a list of accepted
    names, so a guard that renames its entry point keeps working and keeps its sites.
    """
    if module in _INDEX:
        return _INDEX[module]
    path = SCRIPTS / module
    if not path.is_file():
        raise TaxonomyError(f"{module} is not a script in {SCRIPTS}")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    known = _scripts_modules()

    # LEXICAL INDEXING. Gate 4N-I28O, closing I28N-01: a class defined inside a function used to
    # have its methods flattened onto the enclosing function (`_prune_dispatch.visit_If`), which
    # lost the class and left the real identity — `_prune_dispatch.Pruner.visit_If` — in no
    # category at all. Identities are now the lexical path, so two local classes with the same
    # name in different owners stay distinct.
    functions: dict[str, ast.AST] = {}
    classes: dict[str, ast.ClassDef] = {}
    class_bases: dict[str, list[str]] = {}

    def _index(node, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = f"{prefix}{child.name}"
                functions[name] = child
                _index(child, f"{name}.")
            elif isinstance(child, ast.ClassDef):
                name = f"{prefix}{child.name}"
                classes[name] = child
                class_bases[name] = [ast.unparse(b) for b in child.bases]
                _index(child, f"{name}.")
            elif isinstance(child, (ast.If, ast.Try, ast.With, ast.For, ast.While)):
                _index(child, prefix)          # statically resolvable conditional blocks

    _index(tree, "")

    # A loader returns an INSTANCE. `loaded = load_anchor()` then `loaded.redacted()` is how most
    # of these guards reach their own class members, so without this the I28M correction would
    # swing straight into a false exclusion — which the first run of it did.
    returns_instance_of: dict[str, str] = {}
    for fname, fnode in functions.items():
        produced = set()
        for r in ast.walk(fnode):
            if isinstance(r, ast.Return) and isinstance(r.value, ast.Call) \
                    and isinstance(r.value.func, ast.Name):
                if r.value.func.id in classes:
                    produced.add(r.value.func.id)
        if len(produced) == 1:
            returns_instance_of[fname] = produced.pop()

    # A module-level `NAME = SomeLocalClass(...)` makes `NAME.method()` an edge into that class.
    # `leak_scan.py` builds its credential detector exactly this way — `_CREDENTIAL =
    # _CredentialRule(...)` — so without this the actual credential matcher is unreachable and
    # therefore invisible, while every stdlib construction (`re.compile(...)`) correctly binds
    # nothing.
    instance_bindings: dict[str, str] = {}
    deferred_instance_bindings: list[ast.Assign] = []
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        value = node.value
        bound = None
        if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) \
                and value.func.id in classes:
            bound = value.func.id                      # NAME = SomeClass(...)
        elif isinstance(value, ast.Name) and value.id in classes:
            bound = value.id                           # NAME = SomeClass
        elif isinstance(value, ast.Call) and isinstance(value.func, ast.Name) \
                and value.func.id in returns_instance_of:
            bound = returns_instance_of[value.func.id]  # NAME = loader()
        if bound:
            for target in node.targets:
                if isinstance(target, ast.Name):
                    instance_bindings[target.id] = bound
        elif isinstance(value, ast.Call):
            deferred_instance_bindings.append(node)     # resolved after every module is indexed

    # `MUTATORS = {"flip": _flip_effect}` — every function named inside a module-level
    # collection is reachable from any function that reads that collection.
    constant_function_refs: dict[str, list[str]] = {}
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        names = [getattr(t, "id", None) for t in targets]
        referenced = sorted({n.id for n in ast.walk(node) if isinstance(n, ast.Name)
                             and n.id in functions}
                            | {f"{n.value.id}.{n.attr}" for n in ast.walk(node)
                               if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
                               and f"{n.value.id}.{n.attr}" in functions})
        for name in names:
            if name and referenced:
                constant_function_refs[name] = referenced

    defined_names = set(functions) | set(classes) | {
        getattr(t, "id", None)
        for node in tree.body if isinstance(node, (ast.Assign, ast.AnnAssign))
        for t in (node.targets if isinstance(node, ast.Assign) else [node.target])}
    defined_names.discard(None)

    module_bindings: dict[str, str] = {}      # local name -> other script module
    symbol_bindings: dict[str, tuple[str, str]] = {}   # local name -> (module, symbol)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if f"{alias.name}.py" in known:
                    module_bindings[alias.asname or alias.name] = f"{alias.name}.py"
        elif isinstance(node, ast.ImportFrom):
            if node.module and f"{node.module}.py" in known:
                for alias in node.names:
                    symbol_bindings[alias.asname or alias.name] = (f"{node.module}.py",
                                                                   alias.name)

    # A module-level `NAME = some_function` is an alias, not a second control. Resolving it keeps
    # one canonical identity where a naive walk would either miss the call or count it twice.
    aliases: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Name) \
                and node.value.id in functions:
            for target in node.targets:
                if isinstance(target, ast.Name):
                    aliases[target.id] = node.value.id

    entry_points = set()
    for node in tree.body:
        if isinstance(node, ast.If) and "__name__" in ast.dump(node.test):
            for call in ast.walk(node):
                if isinstance(call, ast.Call) and isinstance(call.func, ast.Name):
                    name = aliases.get(call.func.id, call.func.id)
                    if name in functions:
                        entry_points.add(name)

    constants = set()
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                name = getattr(target, "id", None)
                if name and name.isupper():
                    constants.add(name)

    _INDEX[module] = {"module": module, "functions": functions, "aliases": aliases,
                      "classes": classes, "class_bases": class_bases,
                      "defined_names": defined_names,
                      "instance_bindings": instance_bindings,
                      "deferred_instance_bindings": deferred_instance_bindings,
                      "constant_function_refs": constant_function_refs,
                      "returns_instance_of": returns_instance_of,
                      "module_bindings": module_bindings, "symbol_bindings": symbol_bindings,
                      "entry_points": sorted(entry_points), "constants": constants}
    return _INDEX[module]


def observed_dispatch() -> set[str]:
    """Framework-dispatched overrides an execution trace has actually seen run."""
    if not _OBSERVED_DISPATCH.is_file():
        return set()
    try:
        return set(json.loads(_OBSERVED_DISPATCH.read_text(encoding="utf-8"))["observed"])
    except (json.JSONDecodeError, KeyError, UnicodeDecodeError):
        return set()


def is_framework_visitor(module: str, class_name: str) -> bool:
    """Does this class inherit a protocol dispatcher, directly or through a local base?"""
    index = module_index(module)
    seen, frontier = set(), [class_name]
    while frontier:
        name = frontier.pop()
        if name in seen or name not in index["class_bases"]:
            continue
        seen.add(name)
        for base in index["class_bases"][name]:
            if base in FRAMEWORK_VISITOR_BASES:
                return True
            if base in index["classes"]:
                frontier.append(base)
            else:
                for local in index["classes"]:
                    if local.rsplit(".", 1)[-1] == base.rsplit(".", 1)[-1]:
                        frontier.append(local)
    return False


def dispatch_candidates(module: str, class_name: str) -> list[str]:
    """The members a protocol dispatcher could invoke on this class, inherited ones included."""
    index = module_index(module)
    members, seen, frontier = [], set(), [class_name]
    while frontier:
        name = frontier.pop(0)
        if name in seen:
            continue
        seen.add(name)
        members.extend(_class_members(module, name))
        for base in index["class_bases"].get(name, ()):
            if base in index["classes"]:
                frontier.append(base)
            else:
                frontier.extend(c for c in index["classes"]
                                if c.rsplit(".", 1)[-1] == base.rsplit(".", 1)[-1])
    out = []
    for member in members:
        leaf = member.rsplit(".", 1)[-1]
        if leaf.startswith(FRAMEWORK_DISPATCH_PREFIXES) or leaf in FRAMEWORK_DISPATCH_NAMES:
            out.append(member)
    return sorted(out)


def _class_members(module: str, class_name: str) -> list[str]:
    prefix = f"{class_name}."
    return sorted(f for f in module_index(module)["functions"]
                  if f.startswith(prefix) and "." not in f[len(prefix):])


def _deferred_module_instances(module: str) -> dict[str, str]:
    """Module-level `NAME = other_module.loader()` bindings, resolved after indexing.

    They cannot be resolved while the module is being indexed, because resolving them requires
    the OTHER module's index. `signalnest_identity._INV = _inventory()` reaches
    `protected_inventory.LoadedInventory.dig` through exactly this shape.
    """
    index = module_index(module)
    out: dict[str, str] = {}
    for node in index["deferred_instance_bindings"]:
        cls = _instance_class(node.value, module, index)
        if not cls:
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                out[target.id] = cls
    return out


def _resolve_member(module: str, class_name: str, attr: str) -> str | None:
    """One member of a class, following local base classes.

    GATE 4N-I28M, closing I28L-01. Constructing a class used to admit EVERY method defined on it,
    so `iam_eval.py::Evaluation.allowed` — a property referenced nowhere in the repository —
    became a production/control site on the strength of its class being built somewhere. A member
    now enters only when something actually reaches it: an explicit attribute access, a dispatch
    or callback reference, a bounded `getattr`, or construction reaching `__init__`.

    Inheritance is followed explicitly rather than assumed: a method invoked on a subclass and
    defined on its base is still that base's member, and an inherited method nothing invokes is
    still nothing.
    """
    index = module_index(module)
    seen = set()
    frontier = [class_name]
    while frontier:
        name = frontier.pop(0)
        if name in seen:
            continue
        seen.add(name)
        if f"{name}.{attr}" in index["functions"]:
            return f"{name}.{attr}"
        frontier.extend(b for b in index["class_bases"].get(name, ())
                        if b in index["classes"])
    return None


def _lexical_class(name: str, module: str, index: dict, owner: str = "") -> str | None:
    """Resolve a class NAME to its lexical identity, preferring the current scope.

    Lexical indexing made `Pruner` inside `_prune_dispatch` into `_prune_dispatch.Pruner`, so a
    bare-name lookup stopped finding it — and with it, `p = Pruner()` stopped being an instance.
    Gate 4N-I28O's own falsification found that: a visitor driven through a variable would have
    been missed.
    """
    if name in index["classes"]:
        return name
    scope = owner
    while scope:
        if f"{scope}.{name}" in index["classes"]:
            return f"{scope}.{name}"
        scope = scope.rpartition(".")[0]
    matches = [c for c in index["classes"] if c.rsplit(".", 1)[-1] == name]
    return matches[0] if len(matches) == 1 else None


def _instance_class(value: ast.AST, module: str, index: dict, owner: str = "") -> str | None:
    """The `module::Class` an expression evaluates to, when that is decidable.

    Constructors, loader functions that return a constructor call, and the same across a module
    boundary — `protected_inventory.load()` returns a `LoadedInventory`, and `signalnest_identity`
    reaches `LoadedInventory.dig` only through it.
    """
    if not isinstance(value, ast.Call):
        return None
    func = value.func
    if isinstance(func, ast.Name):
        lexical = _lexical_class(func.id, module, index, owner)
        if lexical:
            return f"{module}::{lexical}"
        if func.id in index["returns_instance_of"]:
            return f"{module}::{index['returns_instance_of'][func.id]}"
        if func.id in index["symbol_bindings"]:
            tm, ts = index["symbol_bindings"][func.id]
            ti = module_index(tm)
            if ts in ti["classes"]:
                return f"{tm}::{ts}"
            if ts in ti["returns_instance_of"]:
                return f"{tm}::{ti['returns_instance_of'][ts]}"
    elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) \
            and func.value.id in index["module_bindings"]:
        tm = index["module_bindings"][func.value.id]
        ti = module_index(tm)
        if func.attr in ti["classes"]:
            return f"{tm}::{func.attr}"
        if func.attr in ti["returns_instance_of"]:
            return f"{tm}::{ti['returns_instance_of'][func.attr]}"
    return None


def _local_instances(fn: ast.AST, module: str, index: dict,
                     owner: str = "") -> dict[str, str]:
    """Local names bound to an instance, as `module::Class`.

    Without this, correcting I28L-01 would swing into a false EXCLUSION: `report = Report()`
    followed by `report.add(...)` is how several guards use their own classes, and the first run
    of the correction did exactly that. Tuple unpacking is handled because
    `left, right = parse(a), parse(b)` is how `arn_model.compare` reaches `Arn.differs_from`.
    """
    out: dict[str, str] = {}
    for node in ast.walk(fn):
        if not isinstance(node, ast.Assign):
            continue
        pairs = []
        if isinstance(node.value, ast.Tuple):
            for target in node.targets:
                if isinstance(target, ast.Tuple) and len(target.elts) == len(node.value.elts):
                    pairs.extend(zip(target.elts, node.value.elts))
        else:
            pairs.extend((target, node.value) for target in node.targets)
        for target, value in pairs:
            cls = _instance_class(value, module, index, owner)
            if cls and isinstance(target, ast.Name):
                out[target.id] = cls
    return out


# --------------------------------------------------------------------------- #
# what the workflow actually runs
# --------------------------------------------------------------------------- #

def _steps() -> list[dict]:
    """Every workflow step with an id, its condition and the raw text of its run block."""
    lines = WORKFLOW.read_text(encoding="utf-8").splitlines()
    steps, current = [], None
    for line in lines:
        m = re.match(r"^(\s+)id:\s*([A-Za-z0-9_-]+)\s*$", line)
        if m:
            current = {"id": m.group(2), "indent": len(m.group(1)), "condition": "",
                       "body": []}
            steps.append(current)
            continue
        if current is None:
            continue
        if re.match(r"^\s+-\s+name:", line):
            current = None
            continue
        c = re.match(r"^\s+if:\s*(.+?)\s*$", line)
        if c:
            current["condition"] = c.group(1)
        current["body"].append(line)
    for step in steps:
        step["body"] = "\n".join(step["body"])
    return steps


def _literal_commands(text: str) -> list[list[str]]:
    """`python scripts/X.py a b` written directly in a shell block."""
    folded = re.sub(r"\\\n\s*", " ", text)
    out = []
    for m in re.finditer(r"python3?\s+scripts/([A-Za-z0-9_.-]+\.py)([^|&;#\n]*)", folded):
        script = m.group(1)
        if not (SCRIPTS / script).is_file():
            continue
        argv = [a for a in m.group(2).split() if a and "${{" not in a and not a.startswith("$")]
        out.append([script, *argv])
    return out


def _heredoc_bodies(text: str) -> list[str]:
    """Python heredocs inside a run block. Bounded: `python - <<'TAG' ... TAG`."""
    bodies = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        m = re.search(r"python3?\s+[^|&;#]*<<\s*'?([A-Za-z_][A-Za-z0-9_]*)'?", lines[i])
        if not m:
            i += 1
            continue
        tag = m.group(1)
        body = []
        i += 1
        while i < len(lines) and lines[i].strip() != tag:
            body.append(lines[i])
            i += 1
        bodies.append(textwrap.dedent("\n".join(body)))
        i += 1
    return bodies


def _subprocess_commands(source: str) -> list[list[str]]:
    """Script invocations a bounded Python heredoc performs.

    GATE 4N-I28O, closing I28N-02. The graded `certification_gate` step runs
    `subprocess.run([sys.executable, "scripts/production_certification.py", *args])` inside a
    wrapper and calls that wrapper with `verify`, `eligibility` and `certify`. A model that reads
    only literal `python scripts/X.py` commands sees `state` alone and prunes three subcommands
    that CI really executes.

    This resolves exactly what can be resolved statically: a literal script path in a list, a
    starred parameter filled from literal call arguments, and finite literal argument vectors.
    Anything else is reported UNRESOLVED rather than guessed or ignored.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return [["UNRESOLVED_HEREDOC"]]

    def script_of(call: ast.Call) -> tuple[str | None, str | None]:
        """(script, starred-parameter-name) for a subprocess call on a scripts/ path."""
        if not (isinstance(call.func, ast.Attribute)
                and call.func.attr in ("run", "check_call", "check_output")):
            return None, None
        if not call.args or not isinstance(call.args[0], ast.List):
            return None, None
        script, starred = None, None
        for element in call.args[0].elts:
            if isinstance(element, ast.Constant) and isinstance(element.value, str) \
                    and element.value.startswith("scripts/") and element.value.endswith(".py"):
                script = Path(element.value).name
            elif isinstance(element, ast.Starred) and isinstance(element.value, ast.Name):
                starred = element.value.id
        return script, starred

    out: list[list[str]] = []
    wrappers: dict[str, tuple[str, bool]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for call in ast.walk(node):
            if not isinstance(call, ast.Call):
                continue
            script, starred = script_of(call)
            if not script:
                continue
            vararg = node.args.vararg.arg if node.args.vararg else None
            wrappers[node.name] = (script, bool(starred and starred == vararg))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        script, starred = script_of(node)
        if script and starred:
            # This IS the wrapper body — its arguments arrive from the call sites below, so
            # recording it here would invent a bare invocation that nothing performs.
            continue
        if script:                                  # a direct call, arguments inline
            argv = [e.value for e in node.args[0].elts
                    if isinstance(e, ast.Constant) and isinstance(e.value, str)
                    and not e.value.startswith("scripts/")]
            out.append([script, *argv])
        elif isinstance(node.func, ast.Name) and node.func.id in wrappers:
            script, forwards = wrappers[node.func.id]
            if not forwards:
                out.append([script, "UNRESOLVED_ARGUMENTS"])
                continue
            argv, resolved = [], True
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    argv.append(arg.value)
                else:
                    resolved = False
            out.append([script, *argv] if resolved or argv else [script, "UNRESOLVED_ARGUMENTS"])
    return out


def _aggregator_steps() -> list[dict]:
    """Every workflow step, from the module that owns the grading predicate.

    GATE 4N-I28S. ``_steps()`` above only yields steps carrying an ``id:``, which is why the
    "HTTP isolation smoke test" step — no id, body ``bash scripts/ci-smoke.sh`` — was invisible to
    both real derivation paths and had to be rescued by a regex over comment text. Grading is not
    re-derived here: ``failure_propagation`` already owns that predicate
    (``bool(step_id) and f"steps.{sid}.outcome" in the aggregator``), and a second definition of
    "graded" living in this module is exactly the divergence Gate 4N-I28Q's architect lane found.
    """
    def produce():
        import failure_propagation

        return failure_propagation.analyse()["steps"]

    # Cached: analyse() re-parses the whole workflow, and this is consulted once per step and once
    # per root. Without the cache the derivation went from seconds to minutes.
    return _cached("aggregator_steps", produce)


def _jobs_without_graded_steps() -> set:
    """Jobs in which the mandatory aggregator reads no step outcome at all.

    A STRUCTURAL derivation of "this work is outside the graded pipeline". The obvious shortcut
    was to look for "smoke" in the job name — which is precisely the name-derived reasoning this
    gate exists to remove, and it would silently mislabel a renamed job. Counting graded steps per
    job says the same thing from the workflow's structure.
    """
    def produce():
        graded_by_job: dict = {}
        for step in _aggregator_steps():
            job = step.get("job") or ""
            graded_by_job[job] = graded_by_job.get(job, 0) + (1 if step.get("graded") else 0)
        return {job for job, count in graded_by_job.items() if count == 0}

    return _cached("jobs_without_graded_steps", produce)


def _release_role(step: dict) -> dict:
    """The role a root inherits from the step that runs it, derived and never stamped.

    GATE 4N-I28S, RC-S4. This module used to write ``release_role = "GRADED_CI_STEP"``
    unconditionally onto every site, including sites under a step its own sibling module
    classifies as ungraded. The primary role now comes from the aggregator predicate, and a
    secondary role records the consequence that survives when the aggregator does not read the
    step at all — a step outside the mandatory aggregator still fails its job, and calling that
    "not release relevant" would be as wrong as calling it graded.
    """
    graded = bool(step.get("graded"))
    aggregated = bool(step.get("outcome_read_by_aggregator"))
    advisory = bool(step.get("continue_on_error"))
    secondary = []
    if advisory:
        primary = "ADVISORY_STEP"
        secondary.append("failure is explicitly tolerated by continue-on-error")
    elif graded and aggregated:
        primary = "GRADED_MANDATORY_STEP"
        secondary.append("its outcome is read by the mandatory aggregator, so failure blocks "
                         "release")
    elif aggregated:
        primary = "AGGREGATED_UNGRADED_STEP"
    else:
        primary = "UNGRADED_JOB_STEP"
        secondary.append("no step id and no aggregator outcome read, so failure is not tracked by "
                         "the mandatory aggregator; it still fails its own job")
    job = step.get("job") or ""
    if job and job in _jobs_without_graded_steps():
        secondary.append("its job contains no graded step at all, so nothing in it is read by the "
                         "mandatory aggregator")
    return {"primary": primary, "secondary": secondary,
            "graded": graded, "outcome_read_by_aggregator": aggregated,
            "continue_on_error": advisory,
            "blocks_release": (graded and aggregated and not advisory),
            "job": step.get("job"), "step_has_id": bool(step.get("has_id"))}


_SHELL_CALL = re.compile(
    r"(?:^|\s)(?:bash|sh|zsh|ksh|dash)\s+(?:-[A-Za-z]+\s+)*(scripts/[A-Za-z0-9_.-]+\.sh)\b"
    r"|(?:^|\s)(\./scripts/[A-Za-z0-9_.-]+\.sh)\b")


def _shell_indirection(step: dict) -> list[dict]:
    """Commands a step reaches by running a repository shell script.

    RC-S1. The workflow step names a shell script; the shell script names the executable. Both
    halves are syntax, so both can be read without running anything.
    """
    import shell_command_model as scm

    out = []
    for line in step.get("lines", []):
        raw = line.get("line") or ""
        stripped = raw.strip()
        if stripped.startswith("#"):
            continue
        for m in _SHELL_CALL.finditer(raw):
            rel = (m.group(1) or m.group(2) or "").lstrip("./")
            target = REPO_ROOT / rel
            if not target.is_file():
                continue
            for inv in scm.python_invocations(target):
                if not inv.get("module"):
                    continue
                out.append({
                    "module": inv["module"],
                    "argv": inv.get("argv", []),
                    "argv_fully_resolved": inv.get("argv_fully_resolved", False),
                    "shell_command": stripped,
                    "shell_script": rel,
                    "shell_line": inv["line"],
                    "shell_text": inv["text"],
                    "interpreter": inv.get("interpreter"),
                    "via": inv.get("via", []),
                    "propagation": line.get("verdict"),
                })
    return out


def workflow_script_mentions() -> list[dict]:
    """Every ``scripts/*.py`` occurrence in the workflow, classified by SYNTAX.

    RC-S2 / RC-S3. The whole-file regex that used to create roots survives here as a DISCOVERY
    HINT and nothing else: it finds candidate occurrences, and each one is then classified as
    executable, inert, or unresolved. It can no longer decide root membership, argv, category or
    release role — which is the whole of the Gate 4N-I28Q defect.
    """
    import shell_command_model as scm

    text = WORKFLOW.read_text(encoding="utf-8")
    lines = text.splitlines()
    executed = {r["module"] for r in _resolved_roots()}
    out = []
    for number, line in enumerate(lines, start=1):
        code, comment = scm.strip_comment(line)
        for m in re.finditer(r"scripts/([A-Za-z0-9_.-]+\.py)", comment or ""):
            out.append({"file": ".github/workflows/ci.yml", "line": number,
                        "text": line.strip(), "syntax_context": "comment",
                        "module": m.group(1),
                        "classification": scm.NONEXECUTABLE_MENTION,
                        "parser_evidence": "the occurrence lies after a `#` comment introducer "
                                           "that is not inside a quoted string",
                        "resolved_root": None, "unresolved_reason": None})
        for m in re.finditer(r"scripts/([A-Za-z0-9_.-]+\.py)", code):
            module = m.group(1)
            if module in executed:
                out.append({"file": ".github/workflows/ci.yml", "line": number,
                            "text": line.strip(), "syntax_context": "command",
                            "module": module,
                            "classification": scm.EXECUTABLE_INVOCATION,
                            "parser_evidence": "a command derivation resolved this module to an "
                                               "executed root",
                            "resolved_root": module, "unresolved_reason": None})
            elif re.search(r"(?:^|\s)(?:echo|printf|cat|#)\s", line):
                out.append({"file": ".github/workflows/ci.yml", "line": number,
                            "text": line.strip(), "syntax_context": "data_consumer_argument",
                            "module": module,
                            "classification": scm.NONEXECUTABLE_MENTION,
                            "parser_evidence": "argument of a command that consumes its arguments "
                                               "as data",
                            "resolved_root": None, "unresolved_reason": None})
            else:
                out.append({"file": ".github/workflows/ci.yml", "line": number,
                            "text": line.strip(), "syntax_context": "unclassified_code",
                            "module": module,
                            "classification": scm.UNRESOLVED_MENTION,
                            "parser_evidence": "the occurrence is in workflow code but no command "
                                               "derivation resolved it to an execution",
                            "resolved_root": None,
                            "unresolved_reason": "could be proven neither executable nor inert"})
    return out


def _resolved_roots() -> list[dict]:
    """The root set, derived only from executable semantics. Cached through _DERIVED."""
    if "resolved_roots" in _DERIVED:
        return _DERIVED["resolved_roots"]

    roots: dict[str, dict] = {}

    def record(script: str, argv: list[str], step_id: str, condition: str, *,
               role: dict, chain: dict):
        entry = roots.setdefault(script, {"steps": set(), "invocations": [], "conditions": set(),
                                          "roles": [], "chains": []})
        entry["steps"].add(step_id)
        entry["conditions"].add(condition or "")
        if argv not in entry["invocations"]:
            entry["invocations"].append(argv)
        if role not in entry["roles"]:
            entry["roles"].append(role)
        entry["chains"].append(chain)

    # 1. commands written directly in an id-carrying step, and bounded python heredocs.
    id_steps = {s["id"]: s for s in _steps()}
    agg = {s["id"]: s for s in _aggregator_steps()}
    by_suffix = {}
    for sid, step in agg.items():
        by_suffix.setdefault(sid.split(":")[-1], step)

    for step in _steps():
        role_step = agg.get(step["id"]) or {"id": step["id"], "graded": True,
                                            "outcome_read_by_aggregator": True,
                                            "has_id": True, "job": None}
        role = _release_role(role_step)
        for command in _literal_commands(step["body"]):
            record(command[0], command[1:], step["id"], step["condition"], role=role,
                   chain={"resolution": "DIRECT_COMMAND", "workflow": str(WORKFLOW.name),
                          "job": role.get("job"), "step": step["id"], "step_has_id": True,
                          "module": command[0], "argv": command[1:]})
        for body in _heredoc_bodies(step["body"]):
            for command in _subprocess_commands(body):
                if (SCRIPTS / command[0]).is_file():
                    record(command[0], command[1:], step["id"], step["condition"], role=role,
                           chain={"resolution": "PYTHON_HEREDOC_SUBPROCESS",
                                  "workflow": str(WORKFLOW.name), "job": role.get("job"),
                                  "step": step["id"], "step_has_id": True,
                                  "module": command[0], "argv": command[1:]})

    # 2. commands reached through a repository shell script, from ANY step, id or not.
    for step in _aggregator_steps():
        role = _release_role(step)
        for inv in _shell_indirection(step):
            record(inv["module"], inv["argv"], step["id"], "", role=role,
                   chain={"resolution": "SHELL_INDIRECTION",
                          "workflow": str(WORKFLOW.name), "job": step.get("job"),
                          "step": step["id"], "step_has_id": bool(step.get("has_id")),
                          "shell_command": inv["shell_command"],
                          "shell_script": inv["shell_script"],
                          "shell_source_line": inv["shell_line"],
                          "shell_source_text": inv["shell_text"],
                          "interpreter": inv["interpreter"],
                          "module": inv["module"], "argv": inv["argv"],
                          "argv_fully_resolved": inv["argv_fully_resolved"],
                          "invocation_chain": [*inv["via"], inv["module"]],
                          "propagation": inv["propagation"]})

    out = []
    for mod, v in sorted(roots.items()):
        primaries = [r["primary"] for r in v["roles"]]
        blocks = any(r["blocks_release"] for r in v["roles"])
        primary = ("GRADED_MANDATORY_STEP" if "GRADED_MANDATORY_STEP" in primaries
                   else primaries[0] if primaries else "UNGRADED_JOB_STEP")
        out.append({"module": mod, "release_entry_points": sorted(v["steps"]),
                    "invocations": v["invocations"] or [[]],
                    "conditions": sorted(v["conditions"]),
                    "release_role": {"primary": primary,
                                     "secondary": sorted({s for r in v["roles"]
                                                          for s in r["secondary"]}),
                                     "blocks_release": blocks,
                                     "per_step": v["roles"]},
                    "resolution": sorted({c["resolution"] for c in v["chains"]}),
                    "chains": v["chains"]})
    return _store("resolved_roots", out)


def release_roots() -> list[dict]:
    """Every ``scripts/*.py`` command the workflow actually EXECUTES, with step, argv and role.

    A root is a command, not a script, and membership is a property of execution, never of
    spelling. Three derivations produce one: a command written directly in a step, a bounded
    Python heredoc running one through ``subprocess``, and — Gate 4N-I28S — a step running a
    repository shell script that runs one.

    WHAT WAS REMOVED, and why it could not simply be removed. This function used to end by
    promoting every ``scripts/<name>.py`` substring anywhere in ci.yml, comments included, to a
    root with a synthetic ``UNSTEPPED`` step id and an empty argv. Gate 4N-I28Q proved that made
    the site universe a function of prose: deleting a comment mention dropped 457 sites to 454 and
    adding one raised it to 465, with every control green and CI byte-identical. Deleting that
    fallback ALONE was prohibited and would have been worse — ``smoke_http.py`` reached the
    universe through no other path. It is gone because the real path it was standing in for,
    ``bash scripts/ci-smoke.sh`` -> ``ci-smoke.sh:63``, is now derived. Textual occurrences are
    still examined, but only by ``workflow_script_mentions``, which classifies them and can create
    no root at all; anything it cannot prove inert becomes an UNRESOLVED_MENTION problem in
    ``check()`` and fails closed.
    """
    return _resolved_roots()


def _loop_literals(fn: ast.AST, variable: str) -> list[str]:
    """String literals a finite `for variable in (...)` loop binds inside this function."""
    out = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.For) or not isinstance(node.target, ast.Name):
            continue
        if node.target.id != variable:
            continue
        if isinstance(node.iter, (ast.Tuple, ast.List, ast.Set)):
            out.extend(e.value for e in node.iter.elts
                       if isinstance(e, ast.Constant) and isinstance(e.value, str))
    return out


def command_dispatch(module: str, entry: str) -> dict | None:
    """How this entry point selects a handler, if it does.

    Returns the dispatch variable, the declared subcommand literals and the statically known
    default. `None` means the entry point has no subcommand dispatch and every branch is shared.
    """
    fn = module_index(module)["functions"].get(entry)
    if fn is None:
        return None
    dest = None
    declared: list[str] = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "add_subparsers":
                for kw in node.keywords:
                    if kw.arg == "dest" and isinstance(kw.value, ast.Constant):
                        dest = kw.value.value
            elif node.func.attr == "add_parser" and node.args:
                if isinstance(node.args[0], ast.Constant):
                    declared.append(node.args[0].value)
                elif isinstance(node.args[0], ast.Name):
                    # `for name in ("a", "b"): sub.add_parser(name)` — a finite loop over a
                    # literal vector is statically bounded, so it is resolved rather than
                    # abandoned.
                    declared.extend(_loop_literals(fn, node.args[0].id))
    if dest is None:
        return None
    variable, default = None, None
    for node in ast.walk(fn):
        if not isinstance(node, ast.Assign) or not isinstance(node.targets[0], ast.Name):
            continue
        value = node.value
        if isinstance(value, ast.BoolOp) and isinstance(value.op, ast.Or):
            left, *rest = value.values
            if isinstance(left, ast.Attribute) and left.attr == dest:
                variable = node.targets[0].id
                if rest and isinstance(rest[0], ast.Constant):
                    default = rest[0].value
        elif isinstance(value, ast.Attribute) and value.attr == dest:
            variable = node.targets[0].id
    return {"dest": dest, "declared": sorted(set(declared)), "variable": variable,
            "default": default}


def selected_command(dispatch: dict, argv: list[str]) -> tuple[str | None, str]:
    """Which handler this argument vector selects. Fails closed when it cannot be decided."""
    positionals = [a for a in argv if not a.startswith("-")]
    chosen = next((a for a in positionals if a in dispatch["declared"]), None)
    if chosen:
        return chosen, "explicit positional"
    if not positionals and dispatch["default"] is not None:
        return dispatch["default"], "statically declared default"
    if not positionals and dispatch["default"] is None:
        return None, "no positional and no statically decidable default"
    return None, f"positional {positionals!r} matches no declared subcommand"


def _prune_dispatch(fn: ast.AST, variable: str, selected: str) -> ast.AST:
    """A copy of the entry point with the branches of OTHER subcommands removed.

    Code before and after dispatch is untouched, so shared setup and shared teardown stay
    reachable; only the handler branches that this invocation cannot take are dropped.
    """
    import copy

    pruned = copy.deepcopy(fn)

    def other_branch(test) -> bool:
        return (isinstance(test, ast.Compare) and isinstance(test.left, ast.Name)
                and test.left.id == variable and len(test.ops) == 1
                and isinstance(test.ops[0], ast.Eq) and len(test.comparators) == 1
                and isinstance(test.comparators[0], ast.Constant)
                and test.comparators[0].value != selected)

    class Pruner(ast.NodeTransformer):
        def visit_If(self, node):
            self.generic_visit(node)
            if other_branch(node.test):
                return node.orelse or ast.Pass()
            return node

    return ast.fix_missing_locations(Pruner().visit(pruned))
def graded_steps() -> list[str]:
    """Every graded workflow step id. Wiring sites; invocation-derived, never name-derived."""
    text = WORKFLOW.read_text(encoding="utf-8")
    return [m.group(1) for m in re.finditer(r"^\s+id:\s*([A-Za-z0-9_-]+)\s*$", text, re.M)]


def test_roots() -> list[dict]:
    """Script functions a graded TEST calls directly.

    A module consumed only by the suite is not production control flow, but a failure in it still
    stops the release, because the pytest step is graded. Gate 4N-I28J left the disposition of
    `scripts/enforcement_path.py` open for exactly this reason; it is answered here by deriving
    the category rather than by arguing about the module's name or location.
    """
    known = _scripts_modules()
    roots: dict[tuple[str, str], set[str]] = {}
    for test in sorted(TESTS.glob("test_*.py")):
        try:
            tree = ast.parse(test.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        bindings: dict[str, str] = {}
        direct: dict[str, tuple[str, str]] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if f"{alias.name}.py" in known:
                        bindings[alias.asname or alias.name] = f"{alias.name}.py"
            elif isinstance(node, ast.ImportFrom):
                if node.module and f"{node.module}.py" in known:
                    for alias in node.names:
                        direct[alias.asname or alias.name] = (f"{node.module}.py", alias.name)
        for call in ast.walk(tree):
            if not isinstance(call, ast.Call):
                continue
            target = None
            if isinstance(call.func, ast.Attribute) and isinstance(call.func.value, ast.Name) \
                    and call.func.value.id in bindings:
                target = (bindings[call.func.value.id], call.func.attr)
            elif isinstance(call.func, ast.Name) and call.func.id in direct:
                target = direct[call.func.id]
            if target and target[1] in module_index(target[0])["functions"]:
                roots.setdefault(target, set()).add(test.name)
    return [{"module": m, "symbol": f, "called_by_tests": sorted(t)}
            for (m, f), t in sorted(roots.items())]


# --------------------------------------------------------------------------- #
# enforcement evidence
# --------------------------------------------------------------------------- #

def _own_nodes(fn: ast.AST) -> list[ast.AST]:
    """Every node in a function's body EXCLUDING nested function bodies.

    A nested definition is its own decision point; attributing its branches to the enclosing
    function would credit an outer wrapper with a consequence it does not have.
    """
    out: list[ast.AST] = []

    def walk(node, top=False):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)) \
                    and not top:
                continue
            out.append(child)
            walk(child)

    walk(fn, True)
    return out


def decision_evidence(fn: ast.AST) -> dict:
    """What this function can independently do to an outcome. Structure only; never the name."""
    nodes = _own_nodes(fn)
    raises = sum(isinstance(n, ast.Raise) for n in nodes)
    exits = sum(
        isinstance(n, ast.Call) and (
            (isinstance(n.func, ast.Attribute) and n.func.attr in TERMINAL_CALLS)
            or (isinstance(n.func, ast.Name) and n.func.id in TERMINAL_CALLS))
        for n in nodes)
    asserts = sum(isinstance(n, ast.Assert) for n in nodes)
    conditionals = (
        sum(isinstance(n, (ast.If, ast.IfExp, ast.Try, ast.While, ast.Match)) for n in nodes)
        + sum(isinstance(n, ast.comprehension) and bool(n.ifs) for n in nodes)
        + sum(isinstance(n, ast.BoolOp) for n in nodes))
    returns = sum(isinstance(n, (ast.Return, ast.Yield, ast.YieldFrom)) for n in nodes)
    mutations = (
        sum(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr in MUTATING_METHODS for n in nodes)
        + sum(isinstance(n, ast.Assign)
              and any(isinstance(t, (ast.Subscript, ast.Attribute)) for t in n.targets)
              for n in nodes)
        + sum(isinstance(n, (ast.Global, ast.Nonlocal)) for n in nodes))

    decides = bool(raises or exits or asserts or conditionals)
    consequent = bool(raises or exits or asserts or returns or mutations)
    role = DECIDES if decides else (COMPUTES if consequent else PRESENTS)

    behaviours = []
    if raises:
        behaviours.append("RAISES")
    if exits:
        behaviours.append("EXITS")
    if asserts:
        behaviours.append("ASSERTS")
    if returns:
        behaviours.append("RETURNS_DECISION")
    if mutations:
        behaviours.append("MUTATES_STATE_ITS_CALLER_READS")
    return {"raises": raises, "exits": exits, "asserts": asserts, "conditionals": conditionals,
            "returns": returns, "mutations": mutations, "role": role,
            "terminal_failure_behaviour": behaviours or ["NONE"]}


def _referenced_constants(fn: ast.AST, constants: set[str]) -> list[str]:
    return sorted({n.id for n in ast.walk(fn) if isinstance(n, ast.Name) and n.id in constants})


# --------------------------------------------------------------------------- #
# the closure
# --------------------------------------------------------------------------- #

def _resolve_calls(module: str, fn: ast.AST,
                   owner: str = "") -> tuple[set[tuple[str, str]], list[str]]:
    """Callees of one function, resolved across module boundaries. Unresolved edges are named.

    Three edge forms beyond the obvious call are followed, because each of them is how real
    enforcement in this repository is actually reached:

    * a CLOSURE defined inside `owner` is called by its bare name and is indexed as
      `owner.closure`;
    * a function REFERENCED without being called — `handlers = [_flip_effect, _misspell]` — runs
      later through the table it was put in, so a reference is an edge;
    * a module-level constant holding such a table makes every function named in it reachable
      from any function that reads the constant. `scripts/deny_triangulation.py` dispatches all
      nine of its mutation operators this way, and a call-only walk reports every one of them
      dead.
    """
    index = module_index(module)
    resolved: set[tuple[str, str]] = set()
    unresolved: list[str] = []
    instances = _local_instances(fn, module, index, owner)
    for _name, _node in _deferred_module_instances(module).items():
        instances.setdefault(_name, _node)
    owner_class = owner.split(".")[0] if owner.split(".")[0] in index["classes"] else None

    for node in ast.walk(fn):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            local = index["aliases"].get(node.id, node.id)
            if owner and f"{owner}.{local}" in index["functions"]:
                resolved.add((module, f"{owner}.{local}"))
            elif local in index["functions"]:
                resolved.add((module, local))
            for referenced in index["constant_function_refs"].get(local, ()):
                resolved.add((module, referenced))

    # ATTRIBUTE ACCESS, whether or not it is a call. GATE 4N-I28M: a property is READ, never
    # called, so a call-only walk would drop every invoked property the moment construction
    # stopped admitting members wholesale.
    for node in ast.walk(fn):
        if not (isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load)):
            continue
        receiver = node.value
        cls = None
        if isinstance(receiver, ast.Name):
            if receiver.id in instances:
                cls = instances[receiver.id]
            elif receiver.id in index["instance_bindings"]:
                cls = f"{module}::{index['instance_bindings'][receiver.id]}"
            elif receiver.id in index["classes"]:
                cls = f"{module}::{receiver.id}"
            elif receiver.id == "self" and owner_class:
                cls = f"{module}::{owner_class}"
        if cls is None:
            # An unknown receiver — a parameter, a call result, a comprehension variable. If the
            # attribute names a member of EXACTLY ONE class in this module, the target set is
            # bounded and the edge resolves; if it names members of several, the selection is
            # ambiguous and fails closed. `left.differs_from(right)` resolves this way.
            owners = [c for c in index["classes"] if f"{c}.{node.attr}" in index["functions"]]
            if len(owners) == 1:
                resolved.add((module, f"{owners[0]}.{node.attr}"))
            elif len(owners) > 1:
                unresolved.append(
                    f"{module}::{ast.unparse(node)} could select {node.attr} on any of "
                    f"{sorted(owners)}; the receiver is not resolvable, so it fails closed")
            continue
        cls_module, cls_name = cls.split("::", 1)
        member = _resolve_member(cls_module, cls_name, node.attr)
        if member:
            resolved.add((cls_module, member))
        elif node.attr not in _DUNDER_AND_STDLIB_SAFE and node.attr not in index["defined_names"]:
            # A data attribute is not a member this analysis must follow; a NAME that resolves to
            # nothing on a class the module defines is an edge it failed to follow.
            if node.attr.startswith("__"):
                unresolved.append(f"{module}::{ast.unparse(node)} names no member of {cls}")

    # getattr(obj, "literal") is an attribute access spelled differently. getattr with anything
    # else is reachability this module cannot resolve, and it FAILS CLOSED rather than quietly
    # admitting every member or quietly admitting none.
    # FRAMEWORK DISPATCH. A visitor that is DRIVEN — something calls .visit()/.generic_visit()
    # on it — can have its protocol overrides invoked without any explicit edge naming them.
    # Candidacy is static; exercise is an observed fact. An unexercised candidate is recorded as
    # a category, never dropped in silence.
    for node in ast.walk(fn):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr not in FRAMEWORK_DISPATCH_NAMES:
            continue
        receiver = node.func.value
        driven = None
        if isinstance(receiver, ast.Call) and isinstance(receiver.func, ast.Name):
            driven = _lexical_class(receiver.func.id, module, index, owner)
        elif isinstance(receiver, ast.Name):
            bound = (instances.get(receiver.id)
                     or (f"{module}::{index['instance_bindings'][receiver.id]}"
                         if receiver.id in index["instance_bindings"] else None))
            if bound and bound.split("::", 1)[0] == module:
                driven = bound.split("::", 1)[1]
        if driven is None or not is_framework_visitor(module, driven):
            continue
        exercised = observed_dispatch()
        for member in dispatch_candidates(module, driven):
            if f"{module}::{member}" in exercised:
                resolved.add((module, member))

    for call in ast.walk(fn):
        if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                and call.func.id == "getattr" and call.args):
            continue
        receiver = call.args[0]
        cls = None
        if isinstance(receiver, ast.Name):
            cls = (instances.get(receiver.id)
                   or (f"{module}::{index['instance_bindings'][receiver.id]}"
                       if receiver.id in index["instance_bindings"] else None)
                   or (f"{module}::{receiver.id}" if receiver.id in index["classes"] else None)
                   or (f"{module}::{owner_class}" if receiver.id == "self" and owner_class
                       else None))
        if cls is None:
            continue
        cls_module, cls_name = cls.split("::", 1)
        name_arg = call.args[1] if len(call.args) > 1 else None
        if isinstance(name_arg, ast.Constant) and isinstance(name_arg.value, str):
            member = _resolve_member(cls_module, cls_name, name_arg.value)
            if member:
                resolved.add((cls_module, member))
        else:
            unresolved.append(
                f"{module}::{ast.unparse(call)} selects a member of {cls} dynamically; the "
                "target set is not bounded, so reachability is UNRESOLVED and fails closed")

    for call in ast.walk(fn):
        if not isinstance(call, ast.Call):
            continue
        func = call.func
        if isinstance(func, ast.Name):
            name = index["aliases"].get(func.id, func.id)
            if owner and f"{owner}.{name}" in index["functions"]:
                resolved.add((module, f"{owner}.{name}"))
                continue
            if name in index["functions"]:
                resolved.add((module, name))
            elif name in index["classes"]:
                # CONSTRUCTION reaches __init__ and nothing else. Every other member needs its
                # own evidence — I28L-01.
                init = _resolve_member(module, name, "__init__")
                if init:
                    resolved.add((module, init))
            elif name in index["symbol_bindings"]:
                target_module, target_symbol = index["symbol_bindings"][name]
                resolved.update(_resolve_symbol(target_module, target_symbol, unresolved,
                                                f"{module}::{name}"))
        elif isinstance(func, ast.Attribute):
            receiver = func.value
            if isinstance(receiver, ast.Name) and receiver.id in index["module_bindings"]:
                target_module = index["module_bindings"][receiver.id]
                resolved.update(_resolve_symbol(target_module, func.attr, unresolved,
                                                f"{module}::{ast.unparse(func)}"))
            elif isinstance(receiver, ast.Name) and receiver.id in index["instance_bindings"]:
                member = f"{index['instance_bindings'][receiver.id]}.{func.attr}"
                if member in index["functions"]:
                    resolved.add((module, member))
                elif func.attr not in _DUNDER_AND_STDLIB_SAFE:
                    unresolved.append(
                        f"{module}::{ast.unparse(func)} is a call on a local instance of "
                        f"{index['instance_bindings'][receiver.id]}, which defines no {func.attr}")
    return resolved, unresolved


def _resolve_symbol(module: str, symbol: str, unresolved: list[str],
                    origin: str) -> set[tuple[str, str]]:
    """One cross-module edge.

    A function is followed. A class pulls in its methods, because a class on the enforcement path
    enforces through them. Anything else defined in the target module — an exception type with no
    body, a constant, a dataclass field — is a real reference with nothing to follow. A name that
    resolves to NOTHING is unresolved and fails the analysis closed.
    """
    index = module_index(module)
    if symbol in index["functions"]:
        return {(module, symbol)}
    if symbol in index["classes"]:
        init = _resolve_member(module, symbol, "__init__")
        return {(module, init)} if init else set()
    if symbol in index["defined_names"]:
        return set()
    unresolved.append(f"{origin} -> {module}::{symbol} resolves to no definition")
    return set()


def enforcement_closure(roots: list[tuple[str, str]], overrides: dict | None = None) -> dict:
    """Every (module, function) reachable from the given roots, with its evidence."""
    seen: dict[tuple[str, str], dict] = {}
    callers: dict[tuple[str, str], set[tuple[str, str]]] = {}
    chain: dict[tuple[str, str], list[str]] = {}
    unresolved: list[str] = []
    frontier: list[tuple[tuple[str, str], list[str]]] = [(r, [f"{r[0]}::{r[1]}"]) for r in roots]
    while frontier:
        (module, symbol), path = frontier.pop(0)
        index = module_index(module)
        symbol = index["aliases"].get(symbol, symbol)
        key = (module, symbol)
        if key in seen or symbol not in index["functions"]:
            continue
        node = (overrides or {}).get(key) or index["functions"][symbol]
        evidence = decision_evidence(node)
        seen[key] = {
            "canonical_site_id": f"{module}::{symbol}",
            "implementation_path": f"scripts/{module}",
            "symbol": symbol,
            "module": module,
            "protected_invariant": _referenced_constants(node, index["constants"]) or
                                   ["control flow only — no module-level collection referenced"],
            **evidence,
        }
        chain[key] = path
        callees, bad = _resolve_calls(module, node, owner=symbol)
        unresolved.extend(bad)
        for callee in sorted(callees):
            callers.setdefault(callee, set()).add(key)
            if callee not in seen:
                frontier.append((callee, path + [f"{callee[0]}::{callee[1]}"]))
    for key, record in seen.items():
        record["direct_callers"] = sorted(f"{m}::{s}" for m, s in callers.get(key, set()))
        record["invocation_chain"] = chain[key]
        record["enforcement_fingerprint"] = _fingerprint(record)
    return {"sites": seen, "unresolved_calls": sorted(set(unresolved)),
            "analysis_complete": not unresolved}


def _fingerprint(record: dict) -> str:
    """An identity for the CONTROL, independent of what it is called and where it lives.

    Two functions with the same protected invariant, decision shape and terminal behaviour are the
    same control however they are spelled or filed. This is what makes a rename or a move visible
    as a relocation rather than as a control disappearing and an unrelated one appearing.
    """
    import hashlib
    payload = json.dumps({"protected_invariant": record["protected_invariant"],
                          "role": record["role"],
                          "terminal_failure_behaviour": record["terminal_failure_behaviour"],
                          "conditionals": record["conditionals"],
                          "returns": record["returns"]}, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# the universes
# --------------------------------------------------------------------------- #

def _mutation_pins() -> set[str]:
    matrix = TESTS / "fixtures" / "site-coverage-matrix.json"
    if not matrix.is_file():
        return set()
    try:
        return set(json.loads(matrix.read_text(encoding="utf-8")).get("sites", {}))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return set()


def production_control_function_sites() -> list[dict]:
    """Function sites: reachable from a workflow invocation AND able to change an outcome."""
    return _cached("production", _production_control_function_sites)


def _production_control_function_sites() -> list[dict]:
    """The union over every real command the workflow runs.

    Each invocation is its own root: a script the workflow runs twice with different subcommands
    reaches both handlers, while a subcommand nothing invokes reaches nothing. Gate 4N-I28O made
    this a union because `certification_gate` invokes four of one script's subcommands and the
    single-selection model could only represent one.
    """
    entry_steps: dict[tuple[str, str], list[str]] = {}
    unresolved_commands: list[str] = []
    selections: list[dict] = []
    merged: dict[tuple[str, str], dict] = {}
    merged_unresolved: list[str] = []

    entry_roles: dict[tuple[str, str], dict] = {}
    for root in release_roots():
        module, steps = root["module"], root["release_entry_points"]
        for entry in module_index(module)["entry_points"]:
            entry_steps[(module, entry)] = steps
            entry_roles[(module, entry)] = root["release_role"]
            dispatch = command_dispatch(module, entry)
            for argv in root["invocations"]:
                overrides: dict[tuple[str, str], ast.AST] = {}
                if dispatch and dispatch["variable"]:
                    command, why = selected_command(dispatch, argv)
                    selections.append({"module": module, "entry": entry, "argv": argv,
                                       "selected": command, "why": why,
                                       "steps": steps, "condition": root.get("conditions")})
                    if command is None:
                        unresolved_commands.append(
                            f"{module}::{entry} invoked as {argv!r}: {why}. Command selection is "
                            "UNRESOLVED, so reachability fails closed rather than admitting every "
                            "branch.")
                        continue
                    overrides[(module, entry)] = _prune_dispatch(
                        module_index(module)["functions"][entry], dispatch["variable"], command)
                closure = enforcement_closure([(module, entry)], overrides)
                merged_unresolved.extend(closure["unresolved_calls"])
                for key, record in closure["sites"].items():
                    merged.setdefault(key, record)

    unresolved = sorted(set(merged_unresolved) | set(unresolved_commands))
    _store("command_selections", selections)
    _store("production_unresolved", unresolved)
    pins = _mutation_pins()

    sites = []
    for (module, symbol), record in sorted(merged.items()):
        if record["role"] == PRESENTS:
            continue
        root = record["invocation_chain"][0]
        root_module, root_symbol = root.split("::", 1)
        sites.append({
            **record,
            "kind": "function",
            "name": symbol,
            "id": record["canonical_site_id"],
            "layer": ("entry" if (module, symbol) in entry_steps else
                      "decision" if record["role"] == DECIDES else "computation"),
            "release_entry_point": entry_steps.get((root_module, root_symbol), []),
            # GATE 4N-I28S, RC-S4: derived from the step that actually runs this root, never
            # stamped. The previous unconditional "GRADED_CI_STEP" asserted a grading claim that
            # failure_propagation would contradict for any step the aggregator does not read.
            "release_role": entry_roles.get(
                (root_module, root_symbol),
                {"primary": "UNRESOLVED_ROLE", "secondary": [], "blocks_release": False}),
            "security_or_release_consequence": _consequence(record),
            "independent_mutation_pin": record["canonical_site_id"] in pins,
            "execution_evidence": {"static_invocation_chain": record["invocation_chain"]},
            "primary_category": "PRODUCTION_CONTROL_SITE",
        })
    return sites


def _consequence(record: dict) -> str:
    if "EXITS" in record["terminal_failure_behaviour"]:
        return "can end the guard process, so it can pass or fail the release step directly"
    if "RAISES" in record["terminal_failure_behaviour"]:
        return "can refuse by raising, which its caller turns into a non-zero guard exit"
    if record["role"] == DECIDES:
        return "decides a branch its caller acts on; widening it widens what the guard accepts"
    return "computes a value a decision consumes; corrupting it corrupts that decision silently"


def collapsed_presentation_helpers() -> list[dict]:
    return _cached("collapsed", _collapsed_presentation_helpers)


def _collapsed_presentation_helpers() -> list[dict]:
    """Reachable helpers with no consequence at all — they can only print or format.

    Recorded rather than discarded, so that the exclusion is auditable and so that a helper
    acquiring a consequence later shows up as a new site instead of staying invisible.
    """
    roots = [(r["module"], e) for r in release_roots()
             for e in module_index(r["module"])["entry_points"]]
    closure = enforcement_closure(roots)
    return [{"id": rec["canonical_site_id"], "reason": "no raise, exit, assert, return or "
             "state mutation — cannot change any outcome"}
            for rec in closure["sites"].values() if rec["role"] == PRESENTS]


def ci_release_control_sites() -> list[dict]:
    return _cached("ci_release", _ci_release_control_sites)


def _ci_release_control_sites() -> list[dict]:
    """Sites in scripts consumed only by the suite. A graded test failure still stops a release.

    They are a SEPARATE primary category: they do not run in the guard pipeline, so counting them
    as production control would overstate the production surface — and dropping them entirely
    would repeat I28J's mistake in the other direction, because the pytest step is graded.
    """
    # Roots are filtered by SITE, never by module. `leak_scan.py::scan_accounting` lives in a
    # production module but is reached only from the graded suite; excluding its whole module here
    # would drop it from both universes — the same disappearance I28J found, arrived at from the
    # opposite direction.
    roots = [(r["module"], r["symbol"]) for r in test_roots()]
    if not roots:
        return []
    closure = enforcement_closure(roots)
    production = {s["id"] for s in production_control_function_sites()}
    out = []
    for record in sorted(closure["sites"].values(), key=lambda r: r["canonical_site_id"]):
        if record["role"] == PRESENTS or record["canonical_site_id"] in production:
            continue
        out.append({**record, "kind": "function", "name": record["symbol"],
                    "id": record["canonical_site_id"], "layer": "ci_release",
                    "release_role": "GRADED_TEST_STEP",
                    "security_or_release_consequence": _consequence(record),
                    "primary_category": "CI_RELEASE_CONTROL_SITE"})
    return out


def unreachable_functions() -> list[str]:
    """Defined in a workflow-invoked script but reached by nothing. Never a site."""
    reachable = {(s["module"], s["symbol"]) for s in production_control_function_sites()}
    reachable |= {(s["module"], s["symbol"]) for s in ci_release_control_sites()}
    reachable |= {(h["id"].split("::")[0], h["id"].split("::")[1])
                  for h in collapsed_presentation_helpers()}
    out = []
    for root in release_roots():
        index = module_index(root["module"])
        for symbol in sorted(index["functions"]):
            if (root["module"], symbol) not in reachable:
                out.append(f"{root['module']}::{symbol}")
    return sorted(out)


def check() -> dict:
    """Fail closed: unresolved edges, entry-pointless guards and duplicate identities all fail."""
    roots = release_roots()
    entryless = [r["module"] for r in roots if not module_index(r["module"])["entry_points"]]
    sites = production_control_function_sites()
    closure = {"unresolved_calls": _DERIVED.get("production_unresolved", [])}
    ids = [s["id"] for s in sites]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})

    problems = []
    for edge in closure["unresolved_calls"]:
        problems.append(f"unresolved call {edge}: reachability is not established, so the site "
                        "universe may be understated. Fail closed rather than report a smaller "
                        "world.")
    for module in entryless:
        problems.append(f"{module}: the workflow runs it but no entry point could be derived "
                        "from its __main__ guard, so nothing downstream of it is discoverable")
    for dup in duplicates:
        problems.append(f"{dup}: duplicate canonical site identity")
    for site in sites:
        if not site["invocation_chain"] or not site["release_entry_point"]:
            problems.append(f"{site['id']}: no resolvable release invocation")
        role = site.get("release_role")
        # Mapping rather than dict: at Gate 4N-I28AR every value entering _DERIVED enters FROZEN,
        # so a derived role arrives as a mapping proxy. The predicate is unchanged in what it
        # rejects — the pre-I28S stamped string is still not a Mapping — and narrowing it to `dict`
        # would refuse every honest site, which is how a control ends up switched off.
        if not isinstance(role, Mapping) or role.get("primary") in (None, "UNRESOLVED_ROLE"):
            problems.append(f"{site['id']}: release role is not derived from the aggregator "
                            "predicate")

    # GATE 4N-I28S, RC-S2. A textual occurrence that could be proven neither executable nor inert
    # FAILS CLOSED. Silently dropping it would hide a real control; silently promoting it is the
    # I28Q defect. Neither is available.
    mentions = workflow_script_mentions()
    for mention in mentions:
        if mention["classification"] == "UNRESOLVED_MENTION":
            problems.append(
                f"{mention['file']}:{mention['line']}: `scripts/{mention['module']}` is named in "
                "workflow code but no command derivation resolves it to an execution, and it "
                "cannot be shown inert. UNRESOLVED_MENTION fails closed: prove it executable by "
                "modelling the invocation, or prove it inert.")
    for shell in sorted({c["shell_script"] for r in _resolved_roots() for c in r["chains"]
                         if c.get("shell_script")}):
        import shell_command_model as scm

        for mention in scm.unresolved_mentions(REPO_ROOT / shell):
            problems.append(
                f"{shell}:{mention['line']}: `scripts/{mention['module']}` could be proven "
                f"neither executable nor inert ({mention['evidence']}). Fail closed.")

    return {"production_control_function_sites": len(sites),
            "graded_steps": len(graded_steps()),
            "ci_release_control_sites": len(ci_release_control_sites()),
            "collapsed_presentation_helpers": len(collapsed_presentation_helpers()),
            "unreachable_excluded": len(unreachable_functions()),
            "unresolved_calls": closure["unresolved_calls"],
            "command_roots": len(_resolved_roots()),
            "workflow_script_mentions": {
                "total": len(mentions),
                "executable": sum(1 for m in mentions
                                  if m["classification"] == "EXECUTABLE_INVOCATION"),
                "nonexecutable": sum(1 for m in mentions
                                     if m["classification"] == "NONEXECUTABLE_MENTION"),
                "unresolved": sum(1 for m in mentions
                                  if m["classification"] == "UNRESOLVED_MENTION")},
            "duplicates": duplicates, "problems": problems,
            "derivation": "reachability from workflow-invoked entry points, plus consequence. A "
                          "root is derived from EXECUTABLE SEMANTICS only: a command in a step, a "
                          "bounded Python heredoc, or a bounded shell script the step runs. No "
                          "name, suffix, keyword or per-module allowlist participates, and no "
                          "comment, documentation string or other inert text can create a root; "
                          "an occurrence that can be proven neither executable nor inert fails "
                          "closed.",
            "clean": not problems}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--sites", action="store_true", help="print the canonical site identities")
    args = ap.parse_args(argv)
    result = check()
    if args.json:
        print(json.dumps(result, indent=2))
    elif args.sites:
        for site in production_control_function_sites():
            print(site["id"])
    else:
        print(f"  production/control function sites {result['production_control_function_sites']}"
              f"; graded steps {result['graded_steps']}; CI/release sites "
              f"{result['ci_release_control_sites']}; collapsed "
              f"{result['collapsed_presentation_helpers']}; unreachable excluded "
              f"{result['unreachable_excluded']}")
        for problem in result["problems"]:
            print(f"    {problem}")
        print("SITE TAXONOMY:", "derived" if result["clean"] else "UNRESOLVED")
    return 0 if result["clean"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
