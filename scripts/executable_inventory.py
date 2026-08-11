#!/usr/bin/env python3
"""Derived inventory of every external executable the assurance path can invoke (Gate 4N-I28AK).

THE DEFECT THIS CLOSES. Gate 4N-I28AJ finding ADV-I28AJ-01. Gate 4N-I28AI bound `git` and `bash`
correctly but ASSUMED the inventory was those two. It was not: empirical tracing found `tar`
invoked twice per graded suite by `commit_package_coherence.materialize()`, reached from the i23
predicted-tree coherence control, by bare name through PATH and absent from the trust policy — so a
fake `tar` placed earlier on PATH won resolution while the trust check reported clean. `python3` was
also load-bearing and merely implicit.

WHAT THIS DOES. The inventory is DERIVED from the repository's own text rather than authored, and
every derived executable must carry a policy disposition. A newly reachable executable therefore
fails closed until someone classifies it, which is the property that was missing: the previous
policy could only be as complete as the person writing it remembered to be.

TWO DELIBERATE DESIGN CHOICES, both of which exist because of how the last attempt went wrong.

  1. Static derivation is MODULE-QUALIFIED. Every invocation is recorded as
     (module, enclosing function, line, argv head). It never connects call sites by bare function
     name. My own Gate 4N-I28AJ probe did exactly that — matched `run`, `main`, `verify` and
     `materialize` across unrelated modules — and consequently reported `materialize()` as having
     no callers when the i23 coherence control calls it. Qualified records cannot make that error.

  2. Reachability is OVER-APPROXIMATED. Any executable invocation found anywhere under `scripts/`
     counts as reachable and must be dispositioned. That is the safe direction and it avoids
     call-graph inference altogether. A runtime trace then REFINES a disposition — exercised, or
     statically reachable but not exercised — but never grants permission. Evidence narrows the
     description of a thing already required to be classified; it never removes the requirement.

WHAT IS NOT CLAIMED. Static derivation sees invocations whose command word is a literal. A command
assembled entirely at runtime from computed parts is reported as UNRESOLVED and fails closed rather
than being treated as absent — the same treatment `executed_state_provenance` gives a fully computed
attribute name. Shell builtins, inert mentions in comments and docstrings, and names appearing only
in constant tables are not invocations and are excluded with that reason recorded.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
import sys
from pathlib import Path

import shell_positions

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"
POLICY = REPO_ROOT / "tests" / "fixtures" / "executable-trust-policy.json"

SUBPROCESS_CALLS = ("run", "check_output", "check_call", "call", "Popen")

# Shell builtins and keywords are not external executables. Naming them here is a claim that they
# cannot be shadowed on PATH, which is true because the shell resolves them before any path search.
SHELL_BUILTINS = frozenset({
    "cd", "echo", "test", "true", "false", "set", "export", "read", "shift", "exit", "return",
    "trap", "eval", "exec", "source", ".", "unset", "local", "printf", "pwd", "hash", "type",
    "command", "if", "then", "else", "fi", "for", "while", "do", "done", "case", "esac",
    "wait", "kill", "jobs", "umask", "times", "alias", "builtin", "let", "declare", "readonly",
    "getopts", "shopt", "ulimit", "caller", "mapfile", "continue", "break", ":", "[", "[[",
    "elif", "in", "select", "until", "function", "time", "coproc", "enable", "logout", "suspend",
})


def _is_venv_console_script(word: str) -> bool:
    """A console script inside a Python virtual-environment `bin` directory.

    GATE 4N-I28BH-E3. This is the ONLY slash-containing command word exempted from bare-name
    binding: `<something>venv/bin/<name>` (e.g. `.reader-venv/bin/pip`, `apps/api/.venv/bin/python`)
    is created by the trusted `python -m venv` and resolved literally by the shell, never via a PATH
    search, so it cannot be PATH-shadowed. The rule is STRUCTURAL — a component ending in `venv`
    immediately followed by a `bin` component — not a data allow-list of binary names, and it does
    NOT match an arbitrary relative path like `tools/evil`, which stays bound and fails closed.
    """
    parts = Path(word).parts
    return any(parts[i].endswith("venv") and i + 1 < len(parts) and parts[i + 1] == "bin"
               for i in range(len(parts) - 1))


class InventoryError(RuntimeError):
    """Fail closed. An inventory that cannot be derived is never reported as complete."""


def _declared_control_scripts() -> frozenset:
    """The control scripts the package itself declares. Discovery WITHIN them stays derived."""
    manifest = REPO_ROOT / "tests" / "fixtures" / "package-requirements.json"
    if not manifest.is_file():
        return frozenset()
    doc = json.loads(manifest.read_text(encoding="utf-8")).get("required_paths") or {}
    return frozenset(Path(p).name for group in ("control_scripts", "control_shell")
                     for p in (doc.get(group) or []))


def _load_policy() -> dict:
    if not POLICY.is_file():
        raise InventoryError(f"the executable trust policy is missing at {POLICY}")
    return json.loads(POLICY.read_text(encoding="utf-8"))


def _enclosing(tree, node):
    """The function a node sits inside, for module-qualified attribution."""
    best = None
    for candidate in ast.walk(tree):
        if isinstance(candidate, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if candidate.lineno <= node.lineno and (
                    best is None or candidate.lineno > best.lineno):
                end = getattr(candidate, "end_lineno", None)
                if end is None or node.lineno <= end:
                    best = candidate
    return best.name if best else "<module>"


WORKFLOWS = REPO_ROOT / ".github" / "workflows"


def workflow_run_blocks() -> list:
    """Every `run:` block in every workflow, with its job, step and shell.

    GATE 4N-I28AO, closing the second half of ADV-I28AN-01. The workflow was outside the scanned
    universe entirely, so the twelve `docker` invocations in its run: blocks — the graded image and
    migration steps — were never dispositioned. Those blocks ARE the assurance path:
    site_taxonomy derives the release command roots from this same file. Excluding them while
    claiming to inventory "every external executable the assurance path can invoke" was the gap.

    YAML is parsed rather than pattern-matched, so folded and literal scalars, multiline blocks and
    inline commands are all handled by the loader. A workflow that will not parse is reported, not
    skipped.
    """
    import yaml

    blocks: list = []
    if not WORKFLOWS.is_dir():
        return blocks
    for path in sorted(WORKFLOWS.glob("*.yml")) + sorted(WORKFLOWS.glob("*.yaml")):
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            blocks.append({"origin": path.name, "job": "<unparsed>", "step": "<unparsed>",
                           "shell": None, "run": "", "parse_error": str(exc)[:200]})
            continue
        workflow_default_shell = ((doc or {}).get("defaults", {}).get("run", {}).get("shell"))
        for job_name, job in (doc or {}).get("jobs", {}).items():
            for index, step in enumerate(job.get("steps") or []):
                if not isinstance(step, dict) or "run" not in step:
                    continue
                blocks.append({
                    "origin": f"{path.name}#{job_name}#{step.get('id') or index}",
                    "workflow": path.name, "job": job_name,
                    "step": step.get("name") or step.get("id") or f"[{index}]",
                    "step_id": step.get("id"),
                    # The shell actually in force: step override, then job default, then WORKFLOW
                    # default. ci.yml sets `defaults.run.shell: bash --noprofile --norc -euo
                    # pipefail {0}` at workflow level, which an independent line-oracle noticed and
                    # the first version of this walk did not read.
                    "shell": (step.get("shell")
                              or job.get("defaults", {}).get("run", {}).get("shell")
                              or workflow_default_shell),
                    "working_directory": step.get("working-directory"),
                    "run": step["run"],
                })
    return blocks


def static_inventory() -> dict:
    """Every executable invocation whose command word is a literal, module-qualified."""
    invocations: list[dict] = []
    unresolved: list[dict] = []
    foreign: list[dict] = []
    # GATE 4N-I28AV: shell sources whose parse could not be trusted. Carried out of the inventory
    # so `check()` can refuse them by name rather than silently enumerating a subset.
    incomplete_scans: list[dict] = []

    for path in sorted(SCRIPTS.glob("*.py")):
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
        except (OSError, SyntaxError) as exc:
            # A file that will not parse cannot be scanned for the executables it invokes, so it
            # is recorded as UNRESOLVED and fails closed through the normal problem path. Raising
            # here instead made the bootstrap abort with a traceback rather than a verdict, which
            # is strictly worse: a control that crashes tells you nothing about what it found.
            # WHOSE JOB IS THIS FILE? The inventory derives what the REPOSITORY's own control
            # code invokes. A file that is not part of the declared control set is foreign — an
            # attacker-dropped sitecustomize is the classic case — and adjudicating it belongs to
            # startup_policy, which PROHIBITS sitecustomize and usercustomize outright. Refusing
            # here as well would mean this control decides a question another control already
            # owns, and it would fire on the malformed attack fixtures those controls exist to
            # exercise. A foreign unparseable file is therefore RECORDED, not refused; a declared
            # control script that will not parse still fails closed, because its executables
            # genuinely cannot be derived.
            record = {"module": path.name, "function": "<module>", "line": 0,
                      "reason": f"cannot parse for inventory derivation ({exc})"}
            if path.name in _declared_control_scripts():
                unresolved.append(record)
            else:
                foreign.append(record)
            continue

        # A bare `run(...)` is only subprocess.run if this module imported it from subprocess.
        # Without this, a LOCAL helper named run() is misread as a process launch — which is
        # exactly what happened here: cloudfront_precheck.collect() defines its own run() and the
        # detector reported "cloudfront" as an executable. Over-matching by bare name is the same
        # mistake that made the Gate 4N-I28AJ probe unsound, in a different costume.
        # Map each local binding back to the subprocess function it names, so an aliased import
        # (`from subprocess import check_output as co`) is still recognised. Collecting only the
        # local names lost the alias->real mapping and silently missed every call through it — a
        # missed executable is exactly the ADV-I28AJ-01 failure mode.
        imported_from_subprocess = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "subprocess":
                for alias in node.names:
                    imported_from_subprocess[alias.asname or alias.name] = alias.name

        # Calls into the trust layer NAME their executable and return a validated ABSOLUTE path.
        # An argv built from one of these is more bound than a literal, not less, so it is recorded
        # as a trust-validated invocation and the function it appears in is exempt from the
        # unresolved-argv rule. Without this, wiring tar through the trust layer would have made
        # tar look unreachable while the raw bare-name version looked fine — the control would
        # have punished the fix.
        TRUST_HELPERS = {"git_invocation": "git", "bash_invocation": "bash",
                         "tar_invocation": "tar"}
        trust_validated_functions = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            callee = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(
                node.func, "id", None)
            executable = TRUST_HELPERS.get(callee)
            if executable is None and callee == "validated_path" and node.args and isinstance(
                    node.args[0], ast.Constant):
                executable = node.args[0].value
            if executable is not None:
                enclosing = _enclosing(tree, node)
                trust_validated_functions.add(enclosing)
                invocations.append({"module": path.name, "function": enclosing,
                                    "line": node.lineno, "call": callee,
                                    "executable": executable, "form": "trust_validated_absolute"})

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute):
                base = getattr(func.value, "id", None)
                name = func.attr if base in ("subprocess", "sp") else None
                which_call = func.attr == "which" and base in ("shutil", "sh")
            else:
                bare = getattr(func, "id", None)
                name = imported_from_subprocess.get(bare)
                which_call = imported_from_subprocess.get(bare) is None and bare == "which"

            if name in SUBPROCESS_CALLS and node.args:
                first = node.args[0]
                record = {"module": path.name, "function": _enclosing(tree, node),
                          "line": node.lineno, "call": name}
                if isinstance(first, ast.List) and first.elts:
                    head = first.elts[0]
                    # sys.executable is the BOUND interpreter form the policy requires for child
                    # Python processes. Flagging it as unresolved would penalise the correct
                    # pattern and push call sites toward a bare "python3", which is the opposite
                    # of what CURRENT_INTERPRETER_IDENTITY_BOUND asks for.
                    if isinstance(head, ast.Attribute) and head.attr == "executable" \
                            and getattr(head.value, "id", None) == "sys":
                        record.update({"executable": "python3", "form": "current_interpreter"})
                        invocations.append(record)
                    elif isinstance(head, ast.Constant) and isinstance(head.value, str):
                        record.update({"executable": head.value,
                                       "form": "absolute" if head.value.startswith("/")
                                               else "bare_name"})
                        invocations.append(record)
                    elif record["function"] in trust_validated_functions:
                        record.update({"executable": None, "form": "argv_from_trust_layer"})
                    else:
                        record["reason"] = "argv head is not a literal"
                        unresolved.append(record)
                elif isinstance(first, ast.Constant) and isinstance(first.value, str):
                    record.update({"executable": first.value.split()[0] if first.value else "",
                                   "form": "string_command"})
                    invocations.append(record)
                elif record["function"] in trust_validated_functions:
                    record.update({"executable": None, "form": "argv_from_trust_layer"})
                else:
                    record["reason"] = "argv is not a literal list"
                    unresolved.append(record)

            if which_call and node.args and isinstance(node.args[0], ast.Constant):
                invocations.append({"module": path.name, "function": _enclosing(tree, node),
                                    "line": node.lineno, "call": "shutil.which",
                                    "executable": node.args[0].value, "form": "path_lookup"})

    # Shell scripts and WORKFLOW run: blocks, via the command-position model.
    #
    # GATE 4N-I28AO, closing ADV-I28AN-01. What stood here read `stripped.split()[0]` — the first
    # token of each line — so a command after `|`, `&&`, `;`, inside `$( )`, or after a `VAR=`
    # assignment was invisible. `docker`, `seq`, `grep`, `tee`, `mktemp` and `dirname` were all
    # invoked by tracked shell and absent from the policy while this check reported clean.
    # `shell_positions` models command POSITIONS instead, and reports every construct it cannot
    # resolve so the caller can fail closed rather than skip.
    shell_functions = set()
    shell_sources = []
    for path in sorted(SCRIPTS.glob("*.sh")):
        text = path.read_text(encoding="utf-8")
        shell_functions |= shell_positions.local_functions(text)
        shell_sources.append((path.name, text, "script"))
    for block in workflow_run_blocks():
        # Functions defined inside a run: block are local to it and are not external executables.
        # Without this the inventory reported `check_file` and `probe`, which are shell functions
        # defined in ci.yml itself.
        shell_functions |= shell_positions.local_functions(block["run"])
        shell_sources.append((block["origin"], block["run"], "workflow run block"))

    for origin, text, kind in shell_sources:
        scanned = (shell_positions.scan_script(text, origin=origin) if kind == "script"
                   else shell_positions.scan(text, origin=origin))
        # GATE 4N-I28AV, closing ADV-I28AT-01. An INCOMPLETE parse may not feed the inventory.
        #
        # This is the consumer whose whole purpose is "an executable the policy does not name is an
        # executable nothing binds" — and it is the one Gate 4N-I28AU found had no independent
        # superset. It consumed whatever the parser returned and could not tell a complete scan
        # from one that skipped every word after a `case`. A clean result computed from incomplete
        # input is worse than no result, because it is indistinguishable from coverage.
        if not scanned.is_trustworthy():
            for detail in scanned.completeness_problems():
                incomplete_scans.append({
                    "module": origin, "status": scanned.status,
                    "consumed_position": scanned.consumed_position,
                    "source_length": scanned.source_length,
                    "detail": detail,
                    "reason": (f"{origin}: the shell scan is not complete, so the executables in "
                               f"this source cannot be enumerated — {detail}. Gate 4N-I28AT "
                               "finding ADV-I28AT-01: a partial parse must never be treated as "
                               "coverage.")})
            continue
        for command in scanned.commands:
            word = command.word
            if word in shell_positions.SHELL_BUILTINS or word in shell_functions \
                    or word in shell_positions.ALL_KEYWORDS:
                continue
            # GATE 4N-I28BH-E3. A command word is a PATH-shadowable BARE command only when it has no
            # slash — then the shell searches PATH and an earlier entry can stand in for it, so it is
            # bound by name and refused unless it resolves to an approved path. The over-binding this
            # closes (ADV-I28AN): a RELATIVE venv console script `.reader-venv/bin/pip` was basenamed
            # to `pip` and demanded an approved PATH pip that no call site invokes; it is resolved
            # LITERALLY by the shell (never via PATH), created by the trusted `python -m venv`, so it
            # cannot be PATH-shadowed. ONLY such venv console scripts are exempted as `explicit_path`.
            # Every OTHER slash-containing word (e.g. `tools/evil`, `./x`) stays bound by name so an
            # unclassified relative path still fails closed — the exemption is exactly the venv-bin
            # pattern, not "any path with a slash" (Gate 4N-I28BH-E3 adversarial finding, FALSE_TRUST).
            if word.startswith("/"):
                executable, form = Path(word).name, "absolute"
            elif "/" in word and _is_venv_console_script(word):
                executable, form = None, "explicit_path"
            else:
                executable, form = Path(word).name, "bare_name"
            invocations.append({"module": origin, "function": "<shell>", "line": command.line,
                                "call": "shell", "executable": executable, "path": word,
                                "construct": command.construct, "form": form})
        for problem in scanned.unresolved:
            unresolved.append({"module": origin, "function": "<shell>", "line": problem.line,
                               "call": "shell", "executable": None,
                               "reason": f"{problem.reason} ({problem.word})",
                               "construct": problem.construct})

    names = sorted({i["executable"] for i in invocations if i.get("executable")})
    return {"invocations": invocations, "unresolved": unresolved, "foreign": foreign,
            "incomplete_scans": incomplete_scans,
            "executables": names,
            "source_count": len(list(SCRIPTS.glob("*.py"))) + len(list(SCRIPTS.glob("*.sh")))}


def runtime_inventory(trace_path: Path | None = None) -> dict:
    """A recorded runtime observation, when one is available.

    Runtime evidence REFINES a disposition; it never grants permission. A missing trace is not an
    error — the static requirement stands on its own — but it is reported as absent rather than
    silently treated as "nothing was invoked".
    """
    if trace_path is None or not Path(trace_path).is_file():
        return {"available": False, "executables": [], "invocations": 0}
    seen: dict = {}
    for line in Path(trace_path).read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) >= 2:
            seen[parts[1]] = seen.get(parts[1], 0) + 1
    return {"available": True, "executables": sorted(seen), "counts": seen,
            "invocations": sum(seen.values())}


def reconcile(static: dict, runtime: dict) -> dict:
    """Disposition every name, from both directions."""
    static_names = set(static["executables"])
    runtime_names = set(runtime.get("executables") or [])
    dispositions = {}
    for name in sorted(static_names | runtime_names):
        if name in static_names and name in runtime_names:
            dispositions[name] = "STATICALLY_REACHABLE_AND_EXERCISED"
        elif name in static_names:
            dispositions[name] = "STATICALLY_REACHABLE_NOT_EXERCISED"
        else:
            dispositions[name] = "EXERCISED_BUT_STATICALLY_MISSED"
    return {"dispositions": dispositions,
            "statically_missed": sorted(runtime_names - static_names),
            "runtime_available": runtime.get("available", False)}


def check(*, trace_path: Path | None = None) -> dict:
    """Policy completeness. An unclassified reachable executable fails closed."""
    policy = _load_policy()
    governed = policy.get("executables") or {}
    try:
        static = static_inventory()
    except InventoryError as exc:
        # The failure shape must match the success shape. An early return missing a key made the
        # bootstrap raise KeyError instead of reporting a refusal — a verifier that crashes
        # reports nothing, which is the opposite of failing closed.
        return {"clean": False, "problems": [str(exc)],
                "static": {"executables": [], "invocation_count": 0, "unresolved_count": 0},
                "runtime": {"available": False, "executables": [], "invocations": 0},
                "reconciliation": {"dispositions": {}, "statically_missed": [],
                                   "runtime_available": False},
                "unreachable_policy_entries": [],
                "policy_sha256": ""}

    runtime = runtime_inventory(trace_path)
    reconciliation = reconcile(static, runtime)
    problems: list[str] = []

    # GATE 4N-I28AV, closing ADV-I28AT-01. Policy completeness must consume the parser's
    # COMPLETENESS STATUS, not only the executables it managed to enumerate. A clean result computed
    # from incomplete input is indistinguishable from coverage, which is exactly how an unclassified
    # `kubectl` sat in a graded workflow step with every layer reporting clean.
    for incomplete in static.get("incomplete_scans") or []:
        problems.append(incomplete["reason"])

    for name in static["executables"]:
        if name in SHELL_BUILTINS:
            continue
        if name not in governed:
            matching = [i for i in static["invocations"] if i.get("executable") == name]
            sites = [f"{i['module']}:{i['line']} in {i['function']}()" for i in matching][:3]
            form = matching[0]["form"] if matching else "?"
            problems.append(
                f"{name}: reachable external executable with NO policy classification "
                f"(form={form}, sites={sites}). Gate 4N-I28AJ finding ADV-I28AJ-01: an executable "
                "the policy does not name is an executable nothing binds.")

    declared_dynamic = {
        f"{d['module']}::{d['function']}" for d in (policy.get("dynamic_invocation_sites") or [])}
    # GATE 4N-I28AO. Shell unresolved sites are declared INDIVIDUALLY, by module, line and word.
    # A module-level declaration would blanket-accept a NEW dynamic command inserted into an
    # already-declared file, which is precisely what Section 11 requires to fail. The check is
    # two-way: an undeclared site fails, and a declared site that no longer exists fails too, so a
    # declaration cannot outlive the construct it describes.
    declared_shell = {f"{d['module']}::{d['line']}::{d['word']}"
                      for d in (policy.get("dynamic_shell_sites") or [])}
    observed_shell = set()
    for site in static["unresolved"]:
        if site.get("function") == "<shell>":
            word = str(site.get("reason", "")).rsplit("(", 1)[-1].rstrip(")")
            key = f"{site['module']}::{site['line']}::{word}"
            observed_shell.add(key)
            if key not in declared_shell:
                problems.append(
                    f"{site['module']}:{site['line']}: {site['reason']}. An unresolved shell "
                    "command position must be declared in dynamic_shell_sites with what bounds it; "
                    "unresolved dynamic construction fails closed rather than being treated as "
                    "absent.")
            continue
        if f"{site['module']}::{site['function']}" in declared_dynamic:
            continue
        problems.append(
            f"{site['module']}:{site['line']} in {site['function']}(): {site['reason']}, so the "
            "command word cannot be statically bounded. Unresolved dynamic construction fails "
            "closed rather than being treated as absent.")
    for stale in sorted(declared_shell - observed_shell):
        problems.append(
            f"{stale}: declared in dynamic_shell_sites but no longer present in the derivation. A "
            "declaration that outlives its construct is an unexamined exemption.")

    # A REACHABLE_NOT_EXERCISED disposition is a CLAIM about runtime, and runtime evidence can
    # refute it. `cat` was classified that way and then observed twice during the graded suite via
    # ci-smoke.sh; without this check the contradiction would have sat in the policy unnoticed,
    # which is the same shape as the assumed inventory this gate exists to replace.
    observed = set(runtime.get("executables") or [])
    for name in sorted(observed):
        entry = governed.get(name) or {}
        if entry.get("classification") == "REACHABLE_NOT_EXERCISED_IN_GRADED_PATH":
            problems.append(
                f"{name}: classified REACHABLE_NOT_EXERCISED_IN_GRADED_PATH but OBSERVED "
                f"{runtime.get('counts', {}).get(name, '?')} time(s) during the graded run. A "
                "disposition contradicted by evidence must be upgraded to a bound classification.")

    for name in reconciliation["statically_missed"]:
        if name in governed or name in SHELL_BUILTINS:
            continue
        problems.append(
            f"{name}: observed at runtime but NOT found by static derivation and not classified; "
            "the two inventories disagree without a disposition")

    unreachable = sorted(n for n in governed
                         if n not in static["executables"]
                         and governed[n].get("classification") not in ("NOT_APPLICABLE",))
    return {"clean": not problems, "problems": problems,
            "static": {"executables": static["executables"],
                       "invocation_count": len(static["invocations"]),
                       "unresolved_count": len(static["unresolved"]),
                       "foreign_unparseable": [f["module"] for f in static.get("foreign") or []]},
            "runtime": runtime, "reconciliation": reconciliation,
            "unreachable_policy_entries": unreachable,
            "policy_sha256": hashlib.sha256(POLICY.read_bytes()).hexdigest()}


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--trace", help="path to a recorded runtime invocation log")
    ap.add_argument("--static", action="store_true", help="print the static inventory only")
    args = ap.parse_args(argv)
    if args.static:
        print(json.dumps(static_inventory(), indent=1, sort_keys=True))
        return 0
    result = check(trace_path=Path(args.trace) if args.trace else None)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        for p in result["problems"]:
            print(f"    {p}")
        if result["unreachable_policy_entries"]:
            print(f"    NOTE unreachable policy entries: {result['unreachable_policy_entries']}")
    print("EXECUTABLE INVENTORY: " + ("clean" if result["clean"] else "PROBLEMS"))
    return 0 if result["clean"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
