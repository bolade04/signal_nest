"""No-skip source-deletion verification (Gate 4N-I12, Defect 3).

THE DEFECT. The Gate 4N-I10 deletion tests were `if`-guarded:

    if action in deny_requirements.source1_actions():
        ...assert...

For a capability grounded in only ONE source — which was 37 of 69 at the time — the guard was
false and the body never ran. A vacuous pass, on exactly the rows that most needed testing.
The adversarial lane deleted 28 requirements with a byte-identical green suite.

A missing source is the finding. It is not a reason to skip the check.

Every mandatory capability is tested against EVERY source axis with no conditional. The
completeness assertion pins `pairs == capabilities x axes`, so a shrinking matrix fails
instead of quietly testing less. The harness self-tests at the bottom re-introduce each way
the old harness could go quiet and require this file to fail.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import deny_requirements as dr  # noqa: E402

SOURCE_AXES = ("SOURCE_1_EXTERNAL", "SOURCE_2_INVARIANT", "SOURCE_3_AWS_SAFETY")
MANDATORY = sorted(dr.required_denies())
PAIRS = [(a, axis) for a in MANDATORY for axis in SOURCE_AXES]


def test_every_mandatory_capability_has_at_least_two_grounds():
    """Phase C. A single-grounded requirement is a DEFECT, not a warning."""
    single = dr.single_grounded()
    assert not single, [f"{e['action']} grounded only by {e['grounds']}" for e in single]


@pytest.mark.parametrize("action,axis", PAIRS, ids=[f"{a}|{x.split('_')[1]}"
                                                    for a, x in PAIRS])
def test_removing_one_source_axis_leaves_the_requirement_standing(action, axis):
    """NO CONDITIONAL. Every capability is checked against every axis.

    If `action` is not in `axis`, that is fine and the assertion below still runs: what must
    be true either way is that the OTHER axes still ground it. The old harness skipped this
    case, which is the one where a single deletion silently removed a requirement.
    """
    grounds = dr.grounds_for(action)
    remaining = [g for g in grounds if g != axis]
    assert remaining, (
        f"{action} is grounded ONLY by {axis}: deleting that one source removes the "
        "requirement entirely, with nothing left to notice")


def test_the_matrix_is_complete_and_cannot_silently_shrink():
    expected = len(MANDATORY) * len(SOURCE_AXES)
    assert len(PAIRS) == expected, f"{len(PAIRS)} pairs for {len(MANDATORY)} capabilities"
    assert len(MANDATORY) >= 90, len(MANDATORY)
    assert len(SOURCE_AXES) == 3


def test_no_capability_is_absent_from_the_matrix():
    covered = {a for a, _ in PAIRS}
    assert covered == set(MANDATORY), set(MANDATORY) - covered


# --- PHASE K: the harness must not be able to go quiet -------------------------------------

# Structural properties a deletion test must not have. Detected by AST, not by text: a
# text scan flags the very dictionary that declares the rule, and flagging your own rule
# statement is how the previous three scanners in this gate chain broke.
FORBIDDEN_CALLS = {
    ("pytest", "skip"): "a skipped deletion case is indistinguishable from a passing one",
    ("unittest", "skip"): "same, via the other API",
}
FORBIDDEN_DECORATORS = {
    "xfail": "an expected failure on a MANDATORY deletion case hides a real gap",
}

DELETION_TEST_FILES = ("test_source_deletion.py", "test_deny_triangulation.py")


def _test_functions(path: Path):
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
            yield node


@pytest.mark.parametrize("filename", DELETION_TEST_FILES)
def test_no_deletion_test_can_skip_or_swallow(filename):
    """AST-based. A construct that lets a case go quiet is the defect, not the text of it."""
    import ast

    path = REPO_ROOT / "tests" / filename
    offenders = []
    for fn in _test_functions(path):
        for node in ast.walk(fn):
            if isinstance(node, ast.Call):
                target = node.func
                if (isinstance(target, ast.Attribute)
                        and isinstance(target.value, ast.Name)
                        and (target.value.id, target.attr) in FORBIDDEN_CALLS):
                    offenders.append(
                        f"{filename}:{node.lineno}: {target.value.id}.{target.attr}() — "
                        f"{FORBIDDEN_CALLS[(target.value.id, target.attr)]}")
            if isinstance(node, ast.ExceptHandler):
                names = ast.dump(node.type or ast.Constant(None))
                if "AssertionError" in names:
                    offenders.append(
                        f"{filename}:{node.lineno}: catches AssertionError — swallowing the "
                        "assertion is the loudest possible way to go quiet")
        for decorator in fn.decorator_list:
            if any(name in ast.dump(decorator) for name in FORBIDDEN_DECORATORS):
                offenders.append(f"{filename}:{fn.lineno}: {fn.name} is xfail-marked")
    assert not offenders, "\n".join(offenders)


@pytest.mark.parametrize("filename", DELETION_TEST_FILES)
def test_no_deletion_test_guards_its_assertions_on_source_membership(filename):
    """THE Gate 4N-I10 guard: `if action in source1_actions():` around the assertion.

    Detected structurally — an `If` whose test is a membership check and whose body contains
    the only assertion in the function.
    """
    import ast

    path = REPO_ROOT / "tests" / filename
    offenders = []
    for fn in _test_functions(path):
        asserts_total = [n for n in ast.walk(fn) if isinstance(n, ast.Assert)]
        if not asserts_total:
            continue
        for node in ast.walk(fn):
            if not isinstance(node, ast.If) or not isinstance(node.test, ast.Compare):
                continue
            if not any(isinstance(op, ast.In) for op in node.test.ops):
                continue
            guarded = [n for b in node.body for n in ast.walk(b)
                       if isinstance(n, ast.Assert)]
            if guarded and len(guarded) == len(asserts_total):
                offenders.append(
                    f"{filename}:{node.lineno}: {fn.name} guards ALL of its assertions on a "
                    "membership test — a capability absent from that source is never checked")
    assert not offenders, "\n".join(offenders)


def test_the_parametrization_is_not_empty():
    """An empty parametrize reports zero failures and looks identical to success."""
    assert PAIRS, "the deletion matrix is empty"
    assert len(PAIRS) > 250, len(PAIRS)


def test_the_guard_detector_can_actually_fail(tmp_path):
    """Controls the control: build the exact Gate 4N-I10 shape and require detection."""
    import ast

    probe = tmp_path / "test_probe.py"
    probe.write_text(
        "def test_x(action):\n"
        "    if action in source1():\n"
        "        assert action in source2()\n", encoding="utf-8")
    tree = ast.parse(probe.read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef))
    asserts_total = [n for n in ast.walk(fn) if isinstance(n, ast.Assert)]
    guarded = []
    for node in ast.walk(fn):
        if isinstance(node, ast.If) and isinstance(node.test, ast.Compare) \
                and any(isinstance(op, ast.In) for op in node.test.ops):
            guarded = [n for b in node.body for n in ast.walk(b) if isinstance(n, ast.Assert)]
    assert guarded and len(guarded) == len(asserts_total), (
        "the detector did not recognise a fully-guarded assertion")


def test_grounds_for_is_not_itself_conditional():
    """If grounds_for() could return early, every assertion above weakens silently."""
    source = (REPO_ROOT / "scripts" / "deny_requirements.py").read_text(encoding="utf-8")
    match = re.search(r"def grounds_for\(.*?\n(.*?)\n\n", source, re.DOTALL)
    assert match, "grounds_for is missing"
    body = match.group(1)
    assert "return" in body and "if " not in body.split("return")[0], (
        "grounds_for gained a conditional before its return")
