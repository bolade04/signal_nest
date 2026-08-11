#!/usr/bin/env python3
"""Executed behavioural proof for every discovered site — Gate 4N-I26D, closing I26B-10.

DISPOSITION (Gate 4N-I28S, RC-S6): **EVIDENCE_ONLY — an out-of-band operator tool.**

Gate 4N-I28Q's architect lane raised this module as a blocker, correctly: it is invoked by no
workflow step, imported by no test and by no other script, and it declared an output fixture that
did not exist in the tree — while two of its constants are graded SECURITY_CRITICAL_LIST. Read
together those facts are ambiguous, and an ambiguous 547-line module holding security-critical
scope is not reviewable. The ambiguity is resolved here rather than left to be rediscovered.

WHAT IT IS. A generator of executed behavioural evidence, run deliberately by an operator. It
rsyncs the repository into a temporary materialisation and, per site, runs the shipping guard
unmutated, mutates one load-bearing value, requires the guard to fail, and restores byte-exactly.
That is expensive by construction — it executes guards once per site — which is why it is not on
the critical path of every push.

WHAT IT IS NOT. It is **not** a control: nothing gates on its exit status, and no guard reads its
output. The matrix the enforcing guards actually consume is a DIFFERENT file,
``tests/fixtures/site-coverage-matrix.json``, read by ``scripts/site_coverage.py`` and
``scripts/site_taxonomy.py``. Nothing in the release path changes if this module never runs. It
therefore contributes zero sites to the production/control universe and zero to the CI/release
universe, and that is correct rather than an omission.

WHY IT IS NOT WIRED. Wiring it would make every push pay a full per-site mutation sweep, and the
charter in the paragraphs below — generate the evidence instead of hand-authoring it — is a
statement about how evidence should be PRODUCED, not a claim that this module is graded. The
honest state is an out-of-band tool with a declared disposition, in the same shape as
``scripts/boundary_state_mutations.py``.

THE DECLARED OUTPUT IS GONE. This module used to define a ``MATRIX_OUT`` fixture path and a
``--write-matrix`` flag that wrote it. No guard, test or script ever read that file, and it was
not in the tree — a declared output with no producer in the release path and no consumer at all,
which is precisely what made the module unreviewable. Both are removed rather than papered over:
``--json`` prints the same evidence, and an operator who wants it on disk can redirect it. A path
that nothing consumes should not be named as though something does.

If this module is ever made to gate a release, this block must be replaced, its output given a
consumer, and its sites re-derived — none of which is authorized here.


WHAT WAS OPEN. Gate 4N-I24D built executed coverage for FIFTEEN sites, hand-authored one at a
time in a matrix fixture. Gate 4N-I26B made `site_coverage.py` compute `discovered - in_matrix`
and enforce it, which turned a false "SITE COVERAGE: proven" into an honest failure — 203 sites
discovered, 15 proven, 188 with no executed evidence at all.

A hand-authored matrix cannot close that. Fifteen entries took a gate to write; a hundred and
eighty-eight would take a dozen, and the two hundred and fourth site would be missing again the
day someone added it. The matrix has to be GENERATED from the discovered set, or it is the
hand-authored-list defect one more time, wearing the costume of executed proof.

WHAT A PROOF IS HERE. Per site, in an isolated materialization of the predicted tree:

    1. run the site's shipping guard UNMUTATED and require exit 0   (baseline)
    2. mutate exactly one thing — that site's load-bearing value    (isolation)
    3. run the SAME guard and require a non-zero exit               (detection)
    4. restore byte-exactly and verify the digest                   (no residue)

Baseline-first is not ceremony: without it a guard that was already failing would credit every
mutation under it, which is `MASKED_BY_UNRELATED_FAILURE` dressed as coverage.

WHAT IS NOT A PROOF. A name in a test file, a key in a dictionary, an import, a static
reference, a broad set assertion that cannot isolate one member, or a human marking a row
covered. Each of those has been credited as coverage at some point in this chain and each was
wrong.

MUTATION STRATEGY IS PER CLASS, NOT UNIVERSAL. One blunt mutation applied to everything either
fails to change the site's meaning (proving nothing) or breaks the file (an INVALID_MUTATION
that a careless harness scores as caught). Each class below has a mutation that genuinely
alters the load-bearing behaviour of that KIND of site, and anything this module cannot classify
is UNSUPPORTED_SITE_CLASS — a finding, never a pass.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"
FIXTURES = REPO_ROOT / "tests" / "fixtures"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

CAUGHT = "CAUGHT_BY_INTENDED_CONTROL"
RELATED = "CAUGHT_BY_VALID_RELATED_CONTROL"
MASKED = "MASKED_BY_UNRELATED_FAILURE"
SURVIVED = "SURVIVED_REAL_GAP"
INVALID = "INVALID_MUTATION"
UNSUPPORTED = "UNSUPPORTED_SITE_CLASS"
PROVING = (CAUGHT, RELATED)

# Site classes this module knows how to mutate meaningfully.
REQUIRED_VALUE = "REQUIRED_VALUE"            # a key in an authored contract fixture
FUNCTION_RESULT = "FUNCTION_RESULT"          # a decisive function in a guard script
WORKFLOW_INVOCATION = "WORKFLOW_INVOCATION"  # a graded step's actual command

CLASSES = (REQUIRED_VALUE, FUNCTION_RESULT, WORKFLOW_INVOCATION, "CLI_EXIT_PROPAGATION")


class BehaviourError(RuntimeError):
    """Fail-closed."""


def _env(root: Path) -> dict:
    return {**os.environ, "SIGNALNEST_ANCHOR_TIER": "TIER_1_SYNTHETIC",
            "SIGNALNEST_CANDIDATE_MANIFEST": str(root / "tests/fixtures/candidate-manifest.json")}


def _run_guard(root: Path, script: str, timeout: int = 120) -> tuple[int, str]:
    try:
        proc = subprocess.run([sys.executable, f"scripts/{script}"], cwd=root,
                              capture_output=True, text=True, env=_env(root), timeout=timeout)
        return proc.returncode, (proc.stdout + proc.stderr)[-500:]
    except subprocess.TimeoutExpired:
        return 124, "timed out"


# --------------------------------------------------------------------------- #
# which guard enforces which site
# --------------------------------------------------------------------------- #

def _fixture_consumers() -> dict[str, str]:
    """fixture basename -> the guard script that names it. Derived, never listed."""
    out: dict[str, str] = {}
    for script in sorted(SCRIPTS.glob("*.py")):
        text = script.read_text(encoding="utf-8")
        for fixture in FIXTURES.glob("*.json"):
            if fixture.name in text and fixture.name not in out:
                if re.search(rf'"{re.escape(fixture.name)}"|/ "{re.escape(fixture.stem)}', text) \
                        or fixture.name in text:
                    out[fixture.name] = script.name
    return out


def _module_consumers() -> dict[str, list[str]]:
    """module -> guard scripts whose import closure contains it, itself first."""
    edges: dict[str, set[str]] = {}
    for script in sorted(SCRIPTS.glob("*.py")):
        try:
            tree = ast.parse(script.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names = [node.module.split(".")[0]]
            for name in names:
                if (SCRIPTS / f"{name}.py").exists():
                    edges.setdefault(f"{name}.py", set()).add(script.name)
    return {m: sorted(v) for m, v in edges.items()}


# --------------------------------------------------------------------------- #
# mutations, one per class
# --------------------------------------------------------------------------- #

def _mutate_required_value(root: Path, module: str, key: str) -> str:
    path = root / "tests" / "fixtures" / module
    doc = json.loads(path.read_text(encoding="utf-8"))
    if key not in doc:
        raise BehaviourError(f"{key} absent from {module}")
    original = doc[key]
    # A type-and-value violation, so a checker that validates either shape or content refuses.
    if isinstance(original, bool):
        doc[key] = not original
    elif isinstance(original, (int, float)):
        doc[key] = int(original) + 9999
    elif isinstance(original, str):
        doc[key] = "I26D-MUTATED-VALUE"
    elif isinstance(original, list):
        doc[key] = []
    elif isinstance(original, dict):
        doc[key] = {}
    else:
        doc[key] = None
    path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    return f"{key}: {str(original)[:40]!r} -> {str(doc[key])[:40]!r}"


def _mutate_function(root: Path, module: str, name: str) -> str:
    """Neuter the function: keep the signature, discard the body.

    A function whose result no longer depends on anything is the sharpest test of whether the
    result was load-bearing. If every guard still passes, nothing consumed it.
    """
    path = root / "scripts" / module
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    target = next((n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef) and n.name == name), None)
    if target is None:
        raise BehaviourError(f"{name} not found in {module}")
    lines = source.splitlines(keepends=True)
    start = target.body[0].lineno - 1
    end = target.body[-1].end_lineno
    indent = " " * (target.body[0].col_offset)
    replacement = f"{indent}return None  # I26D behavioural mutation\n"
    path.write_text("".join(lines[:start]) + replacement + "".join(lines[end:]), encoding="utf-8")
    return f"{module}::{name} body replaced with `return None`"


def _mutate_workflow_step(root: Path, step_id: str) -> str:
    """Echo-substitute the step's command: still 'invoked' by name, no longer executed."""
    path = root / ".github" / "workflows" / "ci.yml"
    text = path.read_text(encoding="utf-8")
    marker = f"\n        id: {step_id}\n"
    if marker not in text:
        raise BehaviourError(f"step id {step_id} not found")
    head, tail = text.split(marker, 1)
    body_end = tail.find("\n      - name:")
    body = tail if body_end == -1 else tail[:body_end]
    rest = "" if body_end == -1 else tail[body_end:]
    # GATE 4N-I26E, repairing 40 INVALID_MUTATION cases. The previous pattern anchored the
    # command to the START of a line, which is only true inside a `run: |` block scalar. For a
    # single-line step the command follows `run: `, so the substitution never applied and forty
    # workflow sites were scored INVALID_MUTATION — a defect in the GENERATOR that looked like
    # forty unprovable sites. Both forms are handled now, and a failure to change any byte is
    # still a refusal rather than a silent pass.
    mutated = re.sub(r"^(\s*run:\s*)(python3?\s+scripts/[\w\-]+\.py.*)$",
                     r"\1echo NO-OP \2", body, count=1, flags=re.M)
    if mutated == body:
        mutated = re.sub(r"^(\s*)(python3?\s+scripts/[\w\-]+\.py.*)$",
                         r"\1echo NO-OP \2", body, count=1, flags=re.M)
    if mutated == body:
        raise BehaviourError(
            f"step {step_id} runs no repository script that can be neutered; its command is "
            "not a python3 scripts/*.py invocation")
    path.write_text(head + marker + mutated + rest, encoding="utf-8")
    return f"ci.yml::{step_id} command echo-substituted"


# --------------------------------------------------------------------------- #

CLI_EXIT_PROPAGATION = "CLI_EXIT_PROPAGATION"

# Verdicts specific to CLI proofs. Each names a way a proof can be WRONG rather than merely
# negative, so none of them can be mistaken for evidence.
WRAPPER_MUTATED = "WRAPPER_MUTATED"
NO_SAFE_DOWNSTREAM_TARGET = "NO_SAFE_DOWNSTREAM_TARGET"

# The wrapper is the thing under test in a CLI_EXIT_PROPAGATION proof, so it is exactly what a
# mutation may never touch. Gate 4N-I27 found the previous strategy neutering `main()` itself:
# `raise SystemExit(main())` with main returning None exits 0, so the mutation destroyed the
# very exit code the proof reads and 35 sites were scored "unenforced" on a measurement that
# could not have produced any other answer.
WRAPPER_PATTERNS = (r"if\s+__name__\s*==", r"raise\s+SystemExit", r"sys\.exit\s*\(")

# Callee names that are decisive: main() calls one of these to do the actual work.
DECISIVE_CALLEES = ("check", "run", "verify", "analyse", "analyze", "build", "coverage",
                    "model", "reconcile", "report", "authorize", "classify", "load",
                    "requirements", "contract", "discover_sites", "scan_repository",
                    "covered_sites", "active_pair", "declared", "matrix")


def _wrapper_bytes(source: str) -> str:
    """The TERMINAL entry block a CLI_EXIT_PROPAGATION proof must leave byte-identical.

    SCOPED, not grepped. A first version matched the wrapper patterns anywhere in the file and
    refused a valid proof for verify_closure.py, because that module's `verify()` body happens
    to contain a `sys.exit(` of its own — replacing the body removed a line the check had
    mistaken for the wrapper. Fail-closed, so nothing bad was credited, but a false refusal is
    still a wrong answer. The wrapper is the `if __name__ == "__main__":` block and what follows
    it; an exit expression inside a downstream callee is not the wrapper and is precisely the
    kind of thing a downstream mutation is allowed to replace.
    """
    lines = source.splitlines()
    for index, line in enumerate(lines):
        if re.match(r'\s*if\s+__name__\s*==', line):
            return "\n".join(lines[index:])
    return ""


def _main_body_bytes(source: str) -> str:
    tree = ast.parse(source)
    target = next((n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef) and n.name == "main"), None)
    if target is None:
        return ""
    lines = source.splitlines()
    return "\n".join(lines[target.lineno - 1:target.end_lineno])


def downstream_target(module: str) -> str | None:
    """The decisive function `main()` calls. Discovered, not listed per module.

    The refusal must be induced BELOW the entry point — that is the whole distinction between
    CLI_EXIT_PROPAGATION and FUNCTION_RESULT. A target is only valid if main() actually calls
    it on the default path, so it is read out of main()'s own body.
    """
    source = (SCRIPTS / module).read_text(encoding="utf-8")
    tree = ast.parse(source)
    main_fn = next((n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == "main"), None)
    if main_fn is None:
        return None
    module_fns = {n.name for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name != "main"}
    called = []
    for node in ast.walk(main_fn):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in module_fns:
                called.append(node.func.id)
    if not called:
        return None
    for preferred in DECISIVE_CALLEES:
        for name in called:
            if name == preferred or name.endswith(f"_{preferred}"):
                return name
    return called[0]


def _mutate_cli_downstream(root: Path, module: str, callee: str) -> str:
    """Make the DOWNSTREAM callee refuse. `main()` and the terminal wrapper stay untouched."""
    path = root / "scripts" / module
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    target = next((n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef) and n.name == callee), None)
    if target is None:
        raise BehaviourError(f"downstream target {callee} not found in {module}")
    lines = source.splitlines(keepends=True)
    start = target.body[0].lineno - 1
    end = target.body[-1].end_lineno
    indent = " " * target.body[0].col_offset
    refusal = (f'{indent}raise RuntimeError("I27A downstream refusal in {callee}")\n')
    path.write_text("".join(lines[:start]) + refusal + "".join(lines[end:]), encoding="utf-8")
    return f"{module}::{callee} raises; {module}::main and the terminal wrapper untouched"


def prove_cli(root: Path, site: dict, snapshot: dict) -> dict:
    """CLI_EXIT_PROPAGATION: does a downstream refusal reach the PROCESS exit status?"""
    module = site["module"]
    row = {"site": site["id"], "site_class": CLI_EXIT_PROPAGATION, "module": module,
           "name": site["name"], "guard": module}
    source_before = (root / "scripts" / module).read_text(encoding="utf-8")
    wrapper_before = _wrapper_bytes(source_before)
    main_before = _main_body_bytes(source_before)

    callee = downstream_target(module)
    row["downstream_target"] = callee
    if callee is None:
        row.update({"result": NO_SAFE_DOWNSTREAM_TARGET,
                    "why": "main() calls no module-level function on the default path, so there "
                           "is nothing below the entry point to make refuse"})
        return row

    base_exit, base_out = _run_guard(root, module)
    row["baseline_exit"] = base_exit
    if base_exit != 0:
        row.update({"result": MASKED,
                    "why": f"{module} already exits {base_exit} before mutation"})
        return row

    try:
        row["mutation"] = _mutate_cli_downstream(root, module, callee)
    except (BehaviourError, SyntaxError) as exc:
        _restore(root, snapshot)
        row.update({"result": INVALID, "why": str(exc)[:160]})
        return row

    # WRAPPER INTEGRITY. If main() or the terminal SystemExit moved, the proof is void — that
    # is the I27 defect, and it must be impossible to score rather than merely discouraged.
    source_after = (root / "scripts" / module).read_text(encoding="utf-8")
    if _wrapper_bytes(source_after) != wrapper_before or \
            _main_body_bytes(source_after) != main_before:
        _restore(root, snapshot)
        row.update({"result": WRAPPER_MUTATED,
                    "why": "the mutation altered main() or the terminal exit expression; a "
                           "CLI_EXIT_PROPAGATION proof may never touch what it is testing"})
        return row
    row["wrapper_unchanged"] = True

    mutated_exit, mutated_out = _run_guard(root, module)
    row["mutated_exit"] = mutated_exit
    row["observed"] = "process exit of the real CLI, not an in-process main() return"
    _restore(root, snapshot)

    if mutated_exit == 0:
        row.update({"result": SURVIVED,
                    "why": "the downstream refusal did NOT reach the process exit — main() "
                           "swallowed it. This is a real enforcement gap."})
    else:
        named = callee in mutated_out or "I27A downstream refusal" in mutated_out
        row["result"] = CAUGHT if named else RELATED
        row["attributable"] = named
    return row


def classify(site: dict) -> str:
    # GATE 4N-I27A. A terminal CLI entry point is NOT a FUNCTION_RESULT site. Its return value
    # IS the process exit status, so mutating it manufactures the very observation the proof
    # reads. Routed to CLI_EXIT_PROPAGATION, which mutates BELOW the entry point instead.
    if site["kind"] == "function" and site["name"] == "main":
        return CLI_EXIT_PROPAGATION
    return {"requirement_key": REQUIRED_VALUE,
            "function": FUNCTION_RESULT,
            "graded_step": WORKFLOW_INVOCATION}.get(site["kind"], "UNKNOWN")


NO_SAFE_STRATEGY = "NO_SAFE_MUTATION_STRATEGY_UNDER_CURRENT_SCOPE"
GENUINE_GAP = "GENUINE_ENFORCEMENT_GAP"


def guard_for(site: dict, fixture_map: dict, module_map: dict) -> str | None:
    klass = classify(site)
    if klass == REQUIRED_VALUE:
        return fixture_map.get(site["module"])
    if klass == WORKFLOW_INVOCATION:
        return "ci_invocation_model.py"
    if klass == FUNCTION_RESULT:
        module = site["module"]
        if (SCRIPTS / module).read_text(encoding="utf-8").find("__main__") != -1:
            return module
        consumers = module_map.get(module) or []
        return consumers[0] if consumers else None
    return None


def prove(root: Path, site: dict, guard: str, snapshot: dict) -> dict:
    """One site, one mutation, baseline-first, restored byte-exactly."""
    klass = classify(site)
    if klass == CLI_EXIT_PROPAGATION:
        return prove_cli(root, site, snapshot)
    row = {"site": site["id"], "site_class": klass, "module": site["module"],
           "name": site["name"], "guard": guard}
    if klass == "UNKNOWN":
        row.update({"result": UNSUPPORTED,
                    "why": "site class is not in the strategy registry; unknown fails closed"})
        return row
    if guard is None:
        # GATE 4N-I26E. "UNSUPPORTED" conflated two different things: a class this module cannot
        # mutate, and a site NO DEFAULT-PATH GUARD READS. The second is not a generator defect —
        # it is an enforcement gap, and calling it unsupported hid that. Named precisely now.
        row.update({"result": GENUINE_GAP,
                    "why": "a mutation is constructible, but NO default-path shipping guard "
                           "reads this source, so nothing would refuse the mutation. That is an "
                           "enforcement gap, not an unsupported site class."})
        return row

    base_exit, base_out = _run_guard(root, guard)
    row["baseline_exit"] = base_exit
    if base_exit != 0:
        row.update({"result": MASKED,
                    "why": f"{guard} already fails before mutation ({base_out[-120:]})"})
        return row

    try:
        if klass == REQUIRED_VALUE:
            row["mutation"] = _mutate_required_value(root, site["module"], site["name"])
        elif klass == FUNCTION_RESULT:
            row["mutation"] = _mutate_function(root, site["module"], site["name"])
        else:
            row["mutation"] = _mutate_workflow_step(root, site["name"])
    except (BehaviourError, SyntaxError, json.JSONDecodeError) as exc:
        _restore(root, snapshot)
        # GATE 4N-I26E. A graded step that runs no repository script — `tofu test`, a pip
        # install — has no script invocation to neuter, and that is a property of the STEP, not
        # a defect in the mutation. Calling it INVALID_MUTATION implied the generator had failed
        # when it had correctly determined there is nothing of this kind to mutate. Named as a
        # justified strategy limit instead; it is still NOT proof and is never credited.
        if "runs no repository script" in str(exc):
            row.update({"result": NO_SAFE_STRATEGY, "why": str(exc)[:200]})
        else:
            row.update({"result": INVALID, "why": str(exc)[:160]})
        return row

    # APPLICABILITY: a mutation that changed no byte proves nothing, and scoring it would be
    # the "no-op counted as caught" defect. Checked by hash, not by trusting the mutator.
    after = {k: v for k, v in _snapshot(root).items() if snapshot.get(k) != v}
    if not after:
        _restore(root, snapshot)
        row.update({"result": INVALID,
                    "why": "the mutation changed no byte; nothing was actually altered"})
        return row
    row["files_changed_by_mutation"] = [Path(k).name for k in after]

    mutated_exit, mutated_out = _run_guard(root, guard)
    row["mutated_exit"] = mutated_exit
    row["first_failure"] = guard if mutated_exit else None
    _restore(root, snapshot)

    if mutated_exit == 0:
        row["result"] = SURVIVED
        row["why"] = "the shipping guard still passed; nothing consumes this site"
    else:
        named = site["name"] in mutated_out
        row["result"] = CAUGHT if named else RELATED
        row["attributable"] = named
    return row


def _snapshot(root: Path) -> dict:
    """Byte snapshot of everything a mutation may touch."""
    files = list((root / "tests" / "fixtures").glob("*.json")) + \
        list((root / "scripts").glob("*.py")) + [root / ".github" / "workflows" / "ci.yml"]
    return {str(p): p.read_bytes() for p in files if p.exists()}


def _restore(root: Path, snapshot: dict) -> None:
    for path, data in snapshot.items():
        p = Path(path)
        if not p.exists() or p.read_bytes() != data:
            p.write_bytes(data)


def build(root: Path | None = None, limit: int | None = None) -> dict:
    root = root or REPO_ROOT
    sys.path.insert(0, str(root / "scripts"))
    import mutation_discovery

    sites = mutation_discovery.discover_sites()
    if limit:
        sites = sites[:limit]
    fixture_map, module_map = _fixture_consumers(), _module_consumers()
    snapshot = _snapshot(root)

    results = []
    for site in sites:
        results.append(prove(root, site, guard_for(site, fixture_map, module_map), snapshot))
    _restore(root, snapshot)

    proven = [r for r in results if r["result"] in PROVING]
    return {
        "generated_from": "scripts/mutation_discovery.py :: discover_sites()",
        "sites": len(results), "proven": len(proven),
        "unproven": len(results) - len(proven),
        "by_result": {k: sum(1 for r in results if r["result"] == k)
                      for k in (CAUGHT, RELATED, MASKED, SURVIVED, INVALID, UNSUPPORTED,
                                GENUINE_GAP, NO_SAFE_STRATEGY, WRAPPER_MUTATED,
                                NO_SAFE_DOWNSTREAM_TARGET)},
        "results": results,
        "proof_definition": "baseline guard exit 0, one isolated mutation, mutated guard exit "
                            "non-zero, byte-exact restore. Names, dictionaries, imports and "
                            "static references never count.",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--limit", type=int)
    args = parser.parse_args(argv)

    work = Path(tempfile.mkdtemp(prefix="i26d-behaviour-"))
    try:
        subprocess.run(["rsync", "-a", "--exclude", "node_modules", "--exclude", ".next",
                        "--exclude", "dist", "--exclude", ".terraform",
                        f"{REPO_ROOT}/", f"{work}/"], check=True, capture_output=True)
        result = build(work, limit=args.limit)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"  {result['sites']} sites; proven {result['proven']}; "
              f"unproven {result['unproven']}")
        for key, count in result["by_result"].items():
            print(f"    {key:32s} {count}")
    return 0 if result["unproven"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
