#!/usr/bin/env python3
"""Structural CI environment dataflow — Gate 4N-I26B, closing I26B-04 (I25's VAL-CI).

THE DEFECT THIS CLOSES. Gate 4N-I25's validator lane found the workflow deterministically red:
the `closure` step ran BEFORE `anchor_tier`, and `anchor_tier` is what writes
SIGNALNEST_ANCHOR_TIER to `$GITHUB_ENV`. A `$GITHUB_ENV` write affects only SUBSEQUENT steps, so
`closure` executed with the variable unset and `verify_closure.py` died on an uncaught
module-level AnchorError.

WHY NOBODY CAUGHT IT LOCALLY, WHICH IS THE WORSE HALF. The guard harness pre-exported
SIGNALNEST_ANCHOR_TIER before every step, so it measured a state the runner will never be in.
"34/34 guards pass" was true only under an environment more permissive than CI. A harness that
cannot reproduce the ordering cannot detect an ordering defect, and three consecutive gates
reported green from it.

WHAT THIS MODULE DOES. It reads the workflow as a DATAFLOW GRAPH rather than a list of commands:

  producers   job-level `env:`, step-level `env:`, and `>> "$GITHUB_ENV"` writes
  timing      job/step env is available AT the step; a $GITHUB_ENV write is available only
              from the NEXT step onward — the rule the defect turned on
  consumers   derived by walking the AST of every script a step invokes, transitively through
              local imports, for os.environ / os.getenv reads

Availability is then decided per (step, variable). UNKNOWN is a FINDING, never a pass: if this
module cannot tell whether a value is available, it reports that rather than assuming.
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
SCRIPTS = REPO_ROOT / "scripts"

# A $GITHUB_ENV write is visible to LATER steps only. This constant exists so the rule is
# stated once and named, rather than implied by an index comparison somewhere.
GITHUB_ENV_APPLIES_FROM_NEXT_STEP = True


class DataflowError(RuntimeError):
    """Fail-closed."""


def _load_workflow() -> dict:
    try:
        import yaml
    except ModuleNotFoundError as exc:                       # pragma: no cover - env dependent
        raise DataflowError(
            "PyYAML is required to model the workflow structurally. Refusing to fall back to a "
            "regex parse: a regex that mis-reads a block scalar is exactly how Gate 4N-I23's F5 "
            "guard became vacuous.") from exc
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _environ_aliases(tree: ast.AST) -> set[str]:
    """Names this module ever binds to os.environ.

    WHY THIS EXISTS. The first version of this module matched `os.environ.get("X")` syntactically
    and reported the workflow CLEAN on the exact defect it was written to catch — because
    anchor_loader.py reads the tier as `env.get("SIGNALNEST_ANCHOR_TIER")` after
    `env = os.environ if env is None else env`. Matching a fixed set of spellings is the
    hand-authored-list defect wearing a different hat, and it produced a green result on a
    deterministic CI failure. Aliases are resolved instead of enumerated.
    """
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        value = node.value
        if value is None or not any(_is_environ(sub) for sub in ast.walk(value)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            if isinstance(target, ast.Name):
                aliases.add(target.id)
    return aliases


def _env_reads(path: Path, _seen: set[str] | None = None) -> set[str]:
    """Environment variables a script reads, following local imports transitively.

    Consumption is a property of the CODE, not of a list someone maintained alongside it. A new
    environment read in a helper three imports deep becomes a consumer here with no edit.
    """
    seen = _seen if _seen is not None else set()
    if path.name in seen or not path.exists():
        return set()
    seen.add(path.name)
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return set()

    aliases = _environ_aliases(tree) | {"environ"}
    found: set[str] = set()

    def _reads_environ(node) -> bool:
        """True when `node` denotes os.environ or a name bound to it."""
        if _is_environ(node):
            return True
        return isinstance(node, ast.Name) and node.id in aliases

    for node in ast.walk(tree):
        # os.environ["X"] and alias["X"]
        if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant) \
                and isinstance(node.slice.value, str) and _reads_environ(node.value):
            found.add(node.slice.value)
        if isinstance(node, ast.Call):
            func = node.func
            name = getattr(func, "attr", None) or getattr(func, "id", None)
            if name in ("getenv", "get") and node.args and isinstance(node.args[0], ast.Constant) \
                    and isinstance(node.args[0].value, str):
                if name == "getenv" or _reads_environ(getattr(func, "value", None)):
                    found.add(node.args[0].value)
        # transitive imports, including the dynamic __import__("name") form the repository uses
        if isinstance(node, ast.Import):
            for alias in node.names:
                found |= _env_reads(SCRIPTS / f"{alias.name}.py", seen)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found |= _env_reads(SCRIPTS / f"{node.module}.py", seen)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id == "__import__" and node.args \
                and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
            found |= _env_reads(SCRIPTS / f"{node.args[0].value}.py", seen)
    return found


def _is_environ(node) -> bool:
    return isinstance(node, ast.Attribute) and node.attr == "environ"


def _scripts_invoked(run: str) -> list[str]:
    """Script paths a run body actually invokes. Comments and heredoc data are not invocations."""
    out = []
    for raw in run.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        for match in re.finditer(r"(scripts/[A-Za-z0-9_\-]+\.py)", line):
            out.append(match.group(1))
    return out


def _github_env_writes(run: str) -> set[str]:
    """Variables this step writes to $GITHUB_ENV. Only real writes; a commented line is not one."""
    found = set()
    for raw in run.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "GITHUB_ENV" not in line:
            continue
        for match in re.finditer(r"([A-Z_][A-Z0-9_]*)=", line):
            found.add(match.group(1))
    return found


def _same_step_exports(run: str) -> set[str]:
    found = set()
    for raw in run.splitlines():
        line = raw.strip()
        if line.startswith("#"):
            continue
        match = re.match(r"export\s+([A-Z_][A-Z0-9_]*)=", line)
        if match:
            found.add(match.group(1))
    return found


def model() -> dict:
    """The ordered dataflow model. Every graded step, every producer, every consumer."""
    doc = _load_workflow()
    steps: list[dict] = []
    for job_name, job in (doc.get("jobs") or {}).items():
        job_env = set((job.get("env") or {}).keys())
        # Available from a $GITHUB_ENV write in an EARLIER step of this job.
        carried: set[str] = set()
        for index, step in enumerate(job.get("steps") or []):
            step_id = step.get("id")
            run = step.get("run") or ""
            step_env = set((step.get("env") or {}).keys())
            invoked = _scripts_invoked(run)
            consumed: set[str] = set()
            for rel in invoked:
                consumed |= _env_reads(REPO_ROOT / rel)
            available = job_env | step_env | carried | _same_step_exports(run)
            writes = _github_env_writes(run)
            if step_id:
                steps.append({
                    "job": job_name, "order": index, "id": step_id,
                    "invokes": sorted(set(invoked)),
                    "job_env": sorted(job_env), "step_env": sorted(step_env),
                    "github_env_writes": sorted(writes),
                    "same_step_exports": sorted(_same_step_exports(run)),
                    "available_here": sorted(available),
                    "consumes": sorted(consumed),
                    "missing": sorted(consumed - available),
                    "continue_on_error": bool(step.get("continue-on-error")),
                })
            # THE RULE THE DEFECT TURNED ON: a $GITHUB_ENV write lands on the NEXT step.
            if GITHUB_ENV_APPLIES_FROM_NEXT_STEP:
                carried |= writes
    return {"steps": steps}


def check() -> dict:
    graded = model()["steps"]
    problems: list[str] = []
    unknown: list[str] = []

    for step in graded:
        for var in step["missing"]:
            producer = next((s for s in graded
                             if var in s["github_env_writes"] or var in s["step_env"]), None)
            if producer is None:
                unknown.append(
                    f"{step['id']}: consumes {var} and NO step in the workflow produces it. "
                    "Unknown availability fails closed.")
            else:
                problems.append(
                    f"{step['id']} (order {step['order']}) consumes {var}, which is produced by "
                    f"{producer['id']} (order {producer['order']}). A $GITHUB_ENV write reaches "
                    "only LATER steps, so the value is UNSET here and the step runs in an "
                    "environment the local harness never reproduced.")

    return {
        "graded_steps": len(graded),
        "steps": graded,
        "problems": problems + unknown,
        "unknown_availability": unknown,
        "rule": "job/step env is available AT the step; a $GITHUB_ENV write is available only "
                "from the NEXT step onward",
        "clean": not problems and not unknown,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = check()
    except DataflowError as exc:
        print(f"  {exc}", file=sys.stderr)
        print("CI ENV DATAFLOW: refused")
        return 2
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"  {result['graded_steps']} graded steps modelled")
        for problem in result["problems"]:
            print(f"    {problem}", file=sys.stderr)
        print("CI ENV DATAFLOW:", "clean" if result["clean"] else "ORDERING DEFECT")
    return 0 if result["clean"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
