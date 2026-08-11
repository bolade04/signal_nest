"""Deny-shadow regression: no suite may stay green when a Deny is removed (Gate 4N-I7).

THE DEFECT. Gate 4N-I6 proved that removing `iam:PassRole` from the permanent policy left
tests/test_operator_policies.py at 57/57 passed — the file that most obviously claims to
cover it. The explicit-Deny suite caught it, so the mutation matrix reported "caught" and
the shadow went unnoticed. A capability is only genuinely defended if EVERY suite that
claims to cover it fails when the control is removed; a suite that stays green is a suite
whose assertions were decorative, and it will keep passing through the next regression too.

Each case below removes one action from every Deny statement in the generated policies —
via the test-only hook in tests/conftest.py, never a switch in the generators — and runs
each covering suite in a subprocess. Every one of them must fail.

The four capabilities are the ones whose loss is unrecoverable or invisible:

  iam:PassRole            re-opens the ECS path closed in Gate 4N-H4
  cloudtrail:StopLogging  destroys the evidence that would show the rest
  s3:PutObject on state   lets a principal rewrite infrastructure reality
  iam:CreateRole          mints a principal with an arbitrary trust document, which is
                          the transitive escape identified in Gate 4N-H4 (BR-2)

Subprocess, not monkeypatch: the covering suites bind their policy constants at import
time, so the mutation has to be in place before collection.
"""

from __future__ import annotations

import expiry_authorization as _ea  # noqa: E402

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TESTS = REPO_ROOT / "tests"

# capability -> the suites that CLAIM to cover it and must therefore fail without it
COVERING_SUITES = {
    "iam:PassRole": [
        "test_explicit_deny.py",
        "test_operator_policies.py",
        "test_boundary_compatibility.py",
    ],
    "cloudtrail:StopLogging": [
        "test_explicit_deny.py",
        "test_operator_policies.py",
        "test_boundary_compatibility.py",
    ],
    "s3:PutObject": [
        "test_explicit_deny.py",
        "test_operator_policies.py",
        "test_boundary_compatibility.py",
    ],
    "iam:CreateRole": [
        "test_explicit_deny.py",
        "test_operator_policies.py",
        "test_boundary_compatibility.py",
    ],
}

CASES = [(action, suite) for action, suites in COVERING_SUITES.items() for suite in suites]


def _run(suite: str, env_extra: dict[str, str]) -> subprocess.CompletedProcess:
    env = {**os.environ, **env_extra, "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.run(
        [sys.executable, "-m", "pytest", str(TESTS / suite), "-q", "-p", "no:cacheprovider"],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=300,
    )


@pytest.mark.parametrize("action,suite", CASES, ids=[f"{a}->{s}" for a, s in CASES])
def test_removing_a_deny_fails_every_suite_that_covers_it(action, suite):
    result = _run(suite, {"SIGNALNEST_DENY_MUTATION": action})
    assert result.returncode != 0, (
        f"{suite} stayed GREEN with {action} removed from every Deny.\n"
        f"Its assertions on {action} are decorative — they accept implicit denial, or they "
        f"never probe it at all.\n{result.stdout[-3000:]}"
    )


@pytest.mark.parametrize("suite", sorted({s for _, s in CASES}))
def test_each_covering_suite_is_green_without_the_mutation(suite):
    """Controls the control: a suite that is red anyway would pass the test above vacuously."""
    result = _run(suite, {})
    assert result.returncode == 0, (
        f"{suite} fails WITHOUT any mutation, so its shadow result proves nothing.\n"
        f"{result.stdout[-3000:]}")


def test_the_mutation_hook_actually_removes_the_action():
    """Controls the harness: a no-op mutation would make every case above vacuous."""
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import gen_boundary_policy as gb

    from conftest import _strip_action_from_denies

    doc = gb.boundary_policy()

    def deny_actions(d):
        out = set()
        for s in d["Statement"]:
            if s.get("Effect") != "Deny":
                continue
            raw = s.get("Action", [])
            out.update(raw if isinstance(raw, list) else [raw])
        return out

    before = deny_actions(doc)
    assert "iam:CreateRole" in before, "the boundary must deny iam:CreateRole to begin with"
    after = deny_actions(_strip_action_from_denies(doc, "iam:CreateRole"))
    assert "iam:CreateRole" not in after
    assert before - after == {"iam:CreateRole"}, "the hook must remove exactly one action"


def test_every_mutated_action_is_actually_denied_somewhere_first():
    """A capability that was never denied cannot produce a meaningful shadow result."""
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import gen_boundary_policy as gb
    import gen_operator_policies as gen

    denied = set()
    for doc in (gb.boundary_policy(), gen.permanent_w0_policy(), gen.bootstrap_temp_policy(_ea.ACTIVE_EXPIRY_UTC)):
        for statement in doc["Statement"]:
            if statement.get("Effect") != "Deny":
                continue
            raw = statement.get("Action", [])
            denied.update(a.lower() for a in (raw if isinstance(raw, list) else [raw]))

    missing = [a for a in COVERING_SUITES if a.lower() not in denied]
    assert not missing, f"these actions are not denied anywhere, so the shadow test is vacuous: {missing}"


# --- Phase D: the vacuous-assertion patterns must not reappear ------------------------
#
# Converting the 20 legacy assertions in tests/test_operator_policies.py fixes today's
# suite. It does nothing about the next file someone writes. These checks are static: they
# read the test sources and fail on the PATTERN, not on a behaviour, so a reintroduced
# `not allowed(...)` is caught at the moment it is written rather than the next time a
# control is removed.

PROHIBITED_PATTERNS = {
    "not allowed(": (
        "accepts IMPLICIT_DENY, so it passes whether or not the Deny exists. Use "
        "iam_eval.require_explicit_deny(...) for a safety control, or assert the exact "
        "Decision for an absence check."),
    "iam_eval.effect(": (
        "the legacy string API collapses implicit and explicit denial into one value, so "
        "no assertion built on it can tell a present control from an absent one. Use "
        "iam_eval.decide(...) and compare a Decision."),
}

# NOT prohibited: `is not Decision.EXPLICIT_ALLOW`.
#
# The first draft of this check banned that too, and it was wrong. When the claim is that a
# capability IS PRESENT — the boundary must not break a role's job, a required action must
# survive the ceiling — negating EXPLICIT_ALLOW is the STRICTEST available form: it rejects
# implicit denial, explicit denial, missing context and an invalid policy alike. Banning it
# would have pushed those tests toward weaker predicates. The defect was never the negation;
# it was using a predicate that treats "no statement matched" as a safety control.

# Files allowed to contain a pattern, with the reason. Every entry is a commitment, and
# test_every_pattern_exception_is_still_needed below deletes itself when it stops being one.
PATTERN_EXCEPTIONS = {
    ("tests/test_iam_eval_semantics.py", "iam_eval.effect("):
        "this file tests the legacy effect() function itself, comparing against its exact "
        "return strings including the ImplicitDeny/Deny distinction the API does preserve; "
        "it is the one place the function is the subject rather than the tool",
}


@pytest.mark.parametrize("pattern", sorted(PROHIBITED_PATTERNS))
def test_no_test_file_reintroduces_a_vacuous_assertion(pattern):
    offenders = []
    for path in sorted(TESTS.glob("test_*.py")):
        rel = f"tests/{path.name}"
        if (rel, pattern) in PATTERN_EXCEPTIONS:
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#") or "PROHIBITED_PATTERNS" in line:
                continue
            if pattern in line and stripped.startswith(("assert", "return", "if")):
                offenders.append(f"{rel}:{lineno}: {stripped}")
    assert not offenders, (
        f"vacuous assertion pattern {pattern!r} reintroduced — {PROHIBITED_PATTERNS[pattern]}\n"
        + "\n".join(offenders))


def test_the_vacuous_helper_is_gone_from_the_legacy_suite():
    """`denied()` returned `effect(...) != "Allow"` and was the source of all 20."""
    source = (TESTS / "test_operator_policies.py").read_text(encoding="utf-8")
    assert "def denied(" not in source, (
        "the vacuous helper is back; every call site it serves accepts implicit denial")


def test_every_pattern_exception_is_still_needed():
    """A stale exception silently re-permits the pattern everywhere it was scoped to."""
    for (rel, pattern), reason in PATTERN_EXCEPTIONS.items():
        assert (REPO_ROOT / rel).exists(), f"stale exception for {rel}"
        assert pattern in PROHIBITED_PATTERNS, f"exception for unknown pattern {pattern}"
        assert len(reason.split()) >= 6, f"exception for {rel} is not justified"
        assert pattern in (REPO_ROOT / rel).read_text(encoding="utf-8"), (
            f"the exception for {rel}/{pattern} is no longer needed — remove it")
