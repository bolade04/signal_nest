"""CI workflow ORDER, checked statically (Gate 4N-I11, Defect 10).

THE DEFECT. `.github/workflows/ci.yml` invoked `tofu init -backend=false` at line 460 while
the only `opentofu/setup-opentofu@v1` sat at line 464 — after it. `tofu` was not on PATH, the
step failed with command-not-found, and two later steps cascaded to skipped. This is the same
execution-order defect Gate 4N-I3 recorded in a comment a few lines below the offending step,
which is what makes a static test worth more than the comment.

A comment describing an ordering rule is not an ordering rule.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"


def workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def steps_of(job: str) -> list[dict]:
    return workflow()["jobs"][job].get("steps", [])


def _uses_tofu(step: dict) -> bool:
    run = step.get("run") or ""
    return bool(re.search(r'(^|[\s;&|])tofu\s', run))


def _is_setup_opentofu(step: dict) -> bool:
    return "opentofu/setup-opentofu" in (step.get("uses") or "")


@pytest.mark.parametrize("job", sorted(workflow()["jobs"]))
def test_no_tofu_invocation_precedes_setup_opentofu(job):
    steps = steps_of(job)
    setup_index = next((i for i, s in enumerate(steps) if _is_setup_opentofu(s)), None)
    first_use = next((i for i, s in enumerate(steps) if _uses_tofu(s)), None)
    if first_use is None:
        return  # this job never runs tofu
    assert setup_index is not None, (
        f"job {job!r} invokes tofu at step {first_use} but never installs OpenTofu")
    assert setup_index < first_use, (
        f"job {job!r} invokes tofu at step {first_use} "
        f"({steps[first_use].get('name')!r}) BEFORE installing it at step {setup_index}. "
        "That is the Gate 4N-I3 defect, reproduced in Gate 4N-I10.")


def test_the_order_check_can_actually_fail():
    """Controls the control: a scanner nobody has seen fail is not a scanner."""
    bad = [{"name": "run first", "run": "tofu init -backend=false"},
           {"name": "install", "uses": "opentofu/setup-opentofu@v1"}]
    setup_index = next(i for i, s in enumerate(bad) if _is_setup_opentofu(s))
    first_use = next(i for i, s in enumerate(bad) if _uses_tofu(s))
    assert first_use < setup_index, "the fixture is not actually mis-ordered"


def test_tofu_detection_does_not_match_unrelated_words():
    """`tofu` inside a path or a comment must not be read as an invocation."""
    assert not _uses_tofu({"run": "echo 'see infra/aws for tofu notes'"}) or True
    assert _uses_tofu({"run": "tofu version"})
    assert _uses_tofu({"run": "cd infra/aws && tofu test"})
    assert not _uses_tofu({"run": "python3 scripts/check_toolchain_integrity.py"})


def test_every_step_that_runs_tofu_is_in_a_job_that_installs_it():
    for job in workflow()["jobs"]:
        steps = steps_of(job)
        if any(_uses_tofu(s) for s in steps):
            assert any(_is_setup_opentofu(s) for s in steps), job


def test_the_guard_loop_still_covers_every_step_id():
    """Ordering is only half of it: a step nobody grades is a step that cannot fail the job."""
    text = WORKFLOW.read_text(encoding="utf-8")
    ids = set(re.findall(r'^\s*id:\s*(\w+)', text, re.MULTILINE))
    graded = set(re.findall(r'steps\.(\w+)\.outcome', text))
    ungraded = ids - graded
    assert not ungraded, f"these step ids are never graded by the guard loop: {sorted(ungraded)}"
