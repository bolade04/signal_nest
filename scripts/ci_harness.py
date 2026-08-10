#!/usr/bin/env python3
"""True CI-equivalent harness — Gate 4N-I26B, closing the second half of I26B-04.

THE DEFECT THIS CLOSES. Every prior gate validated the graded steps by exporting
SIGNALNEST_ANCHOR_TIER (and whatever else was needed) before running each guard. That harness
reported "34/34 guards pass" while CI was deterministically red, because it measured a state the
runner never has. A harness more permissive than the runner cannot detect an ordering defect —
it can only confirm that the commands work when the environment is already correct, which is
the one thing that was never in doubt.

WHAT THIS HARNESS DOES DIFFERENTLY. Each graded step runs with EXACTLY the environment
scripts/ci_env_dataflow.py says is available AT THAT POINT in the workflow — job `env:`, that
step's `env:`, and only those `$GITHUB_ENV` writes made by EARLIER steps. Nothing is
pre-exported. The developer shell is not inherited: no AWS variables, no authorization
variables, no ambient tier.

Bodies run under `bash -e` because that is the shell GitHub Actions uses, and because the
question this harness exists to answer is not "was the command invoked?" but "does its failure
fail the step?"

WHAT IT DOES NOT DO. It does not run steps that would mutate the machine or reach the network —
`tofu init`, `pip install`, `docker`, `gh`, or an `uses:` action. Those are classified
NOT_EXECUTABLE_LOCALLY by a STRUCTURAL rule (every command in the body must be a repository
script invocation or a shell builtin), never by a hand-written list of step ids, and they are
reported rather than silently skipped. A harness that quietly drops a third of the workflow and
still claims CI equivalence is the reporting defect this chain has spent six gates removing.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

EXECUTED = "EXECUTED"
NOT_EXECUTABLE = "NOT_EXECUTABLE_LOCALLY"

# Commands that mutate the machine or reach the network. A body containing any of these is not
# run here. This IS a list, and it is guarded in the only direction that matters: anything NOT
# recognised as a safe repository invocation is refused, so an unknown command cannot be
# executed by omission from this list.
MUTATING = ("tofu", "terraform", "pip", "docker", "gh ", "npm", "pnpm", "curl", "wget", "aws ")

# Shell constructs that are safe to run locally alongside a script invocation.
SAFE_BUILTIN = re.compile(
    r"^\s*(#|test\s|\[\s|echo\s|set\s|true$|:$|fi$|then|else|if\s|for\s|done$|do$|"
    r"cd\s|mkdir\s|rm\s|cat\s|python3?\s+-c|export\s|local\s|\}|\{|\)|fail=|\w+=)")

SAFE_INVOCATION = re.compile(r"python3?\s+(-m\s+\w+|scripts/[\w\-]+\.py)")


def _bodies() -> list[dict]:
    """Graded steps with their run bodies, in workflow order."""
    import yaml

    doc = yaml.safe_load((REPO_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    out = []
    for job_name, job in (doc.get("jobs") or {}).items():
        for index, step in enumerate(job.get("steps") or []):
            if step.get("id"):
                out.append({"job": job_name, "order": index, "id": step["id"],
                            "run": step.get("run") or "", "uses": step.get("uses"),
                            "working_directory": step.get("working-directory")})
    return out


def classify(step: dict) -> str:
    """EXECUTED or NOT_EXECUTABLE_LOCALLY, decided from the body's structure."""
    if step.get("uses") or not step["run"].strip():
        return NOT_EXECUTABLE
    if step.get("working_directory"):
        return NOT_EXECUTABLE
    for raw in step["run"].splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if any(token in line for token in MUTATING):
            return NOT_EXECUTABLE
        if SAFE_INVOCATION.search(line) or SAFE_BUILTIN.match(line):
            continue
        return NOT_EXECUTABLE
    return EXECUTED


def available_env() -> dict[str, dict]:
    """Per-step availability, from the structural dataflow model. Never from this process."""
    import ci_env_dataflow

    return {s["id"]: s for s in ci_env_dataflow.model()["steps"]}


def _expand(value: str) -> str:
    """Substitute the runner expressions this workflow actually uses.

    Only `github.workspace` appears in a graded step's env, and on a runner it is the checkout
    root. Leaving it unexpanded made a step fail here for a reason the runner would never hit —
    a harness artefact reported as a repository defect, which is its own kind of false result.
    An expression this function does not know is left ALONE and will surface as a failure rather
    than being silently blanked.
    """
    return value.replace("${{ github.workspace }}", str(REPO_ROOT))


def _workflow_env_values() -> dict[str, dict[str, str]]:
    """The VALUES the workflow declares, per step id: job env overlaid with step env."""
    import yaml

    doc = yaml.safe_load((REPO_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    values: dict[str, dict[str, str]] = {}
    for job in (doc.get("jobs") or {}).values():
        job_env = {k: _expand(str(v)) for k, v in (job.get("env") or {}).items()}
        carried: dict[str, str] = {}
        for step in job.get("steps") or []:
            merged = {**job_env, **carried,
                      **{k: _expand(str(v))
                         for k, v in (step.get("env") or {}).items()}}
            if step.get("id"):
                values[step["id"]] = merged
            for line in (step.get("run") or "").splitlines():
                stripped = line.strip()
                if stripped.startswith("#") or "GITHUB_ENV" not in stripped:
                    continue
                match = re.search(r'"?([A-Z_][A-Z0-9_]*)=([^"\s]*)"?\s*>>', stripped)
                if match:
                    carried[match.group(1)] = match.group(2)
    return values


def run_step(step: dict, env_values: dict[str, str]) -> dict:
    """Execute one body under `bash -e` with ONLY the values available at that point."""
    env = {
        # The minimum a shell needs. Deliberately NOT os.environ: inheriting the developer
        # shell is how the previous harness acquired the tier it was supposed to be testing for.
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin:/opt/homebrew/bin",
        "HOME": tempfile.mkdtemp(prefix="ci-harness-home-"),
        "LANG": "C",
        **env_values,
    }
    try:
        proc = subprocess.run(["bash", "-e", "-c", step["run"]], cwd=REPO_ROOT, env=env,
                              capture_output=True, text=True, timeout=600)
        return {"exit": proc.returncode,
                "tail": (proc.stdout + proc.stderr)[-400:]}
    except subprocess.TimeoutExpired:
        return {"exit": 124, "tail": "timed out"}
    finally:
        shutil.rmtree(env["HOME"], ignore_errors=True)


def check(execute: bool = True) -> dict:
    steps = _bodies()
    model = available_env()
    values = _workflow_env_values()

    graded_ids = {s["id"] for s in steps}
    modelled_ids = set(model)

    results = []
    for step in steps:
        classification = classify(step)
        row = {"id": step["id"], "order": step["order"], "classification": classification,
               "available_env": sorted(values.get(step["id"], {})),
               "missing_env": model.get(step["id"], {}).get("missing", [])}
        if classification == EXECUTED and execute:
            row.update(run_step(step, values.get(step["id"], {})))
        results.append(row)

    problems = []
    # BIDIRECTIONAL completeness. A harness that models a subset of the graded steps and reports
    # a pass rate over that subset is claiming coverage it does not have.
    for missing in sorted(graded_ids - modelled_ids):
        problems.append(f"{missing}: graded in the workflow but ABSENT from the dataflow model")
    for extra in sorted(modelled_ids - graded_ids):
        problems.append(f"{extra}: modelled but not a graded workflow step")
    for row in results:
        if row["missing_env"]:
            problems.append(f"{row['id']}: consumes {row['missing_env']} unavailable at its point")
        if row.get("exit") not in (None, 0):
            problems.append(f"{row['id']}: exited {row['exit']} under bash -e with the real "
                            f"environment for its position — {row.get('tail','')[-160:]}")

    executed = [r for r in results if r["classification"] == EXECUTED]
    return {
        "graded_steps": len(graded_ids), "modelled_steps": len(modelled_ids),
        "bidirectional_equality": graded_ids == modelled_ids,
        "executed": len(executed), "not_executable_locally": len(results) - len(executed),
        "not_executable_ids": [r["id"] for r in results if r["classification"] != EXECUTED],
        "results": results, "problems": problems,
        "environment_policy": "no value is pre-exported; each step receives exactly what the "
                              "workflow makes available at its position, and os.environ is not "
                              "inherited",
        "coverage_claim": f"{len(executed)} of {len(graded_ids)} graded steps executed; the "
                          f"remainder are classified NOT_EXECUTABLE_LOCALLY by a structural rule "
                          f"and are reported, not skipped silently",
        "clean": not problems,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--model-only", action="store_true",
                        help="check ordering and completeness without executing bodies")
    args = parser.parse_args(argv)
    result = check(execute=not args.model_only)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"  graded {result['graded_steps']}; modelled {result['modelled_steps']}; "
              f"executed {result['executed']}; not executable locally "
              f"{result['not_executable_locally']}")
        for problem in result["problems"]:
            print(f"    {problem}", file=sys.stderr)
        print("CI HARNESS:", "clean" if result["clean"] else "DEFECT")
    return 0 if result["clean"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
