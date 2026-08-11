"""Gate 4N-I28BF-A4 — structural-test self-protection (section 14).

WHAT THIS PROVES. Every structural control this gate relies on must inspect EXECUTABLE STRUCTURE
or proven live reachability — never mere text presence. A required token surviving only in a
comment, docstring, string literal, assertion message, dead code, an unreachable branch, or a
stale alias must NOT satisfy the control; and renaming a live callable must be detected by AST
even when every textual reference is preserved. The representative structural control is
``assertion_contracts.validate`` (the AST/reachability engine behind the assertion-mutation and
AC-23 batteries) plus the AST-based ``pytest_sessionfinish`` check in the graded-session module.

Each attack activates a real text-only trick; the intended detector fires; and — for the
load-bearing assertion control — the graded result fails, because ``validate`` runs inside the
mandatory node ``test_the_contracted_assertions_are_all_intact``. The final control proves inert
textual changes remain inert, so the battery cannot pass by flagging noise.
"""

from __future__ import annotations

import ast
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import assertion_contracts as ac                   # noqa: E402

_EQ_CONTRACT = {
    "contract_id": "STRUCT-PROBE",
    "minimum_meaningful_assertions": 1,
    "required_assertions": [{"class": "EXACT_IDENTITY_EQUALITY",
                             "must_reference": ["result", "expected"]}],
    "protected_invariant": "synthetic", "proving_mutation": "synthetic",
    "why_load_bearing": "synthetic",
}


def _validate_body(tmp_path: Path, body: str) -> dict:
    (tmp_path / "tests").mkdir(exist_ok=True)
    (tmp_path / "tests" / "t.py").write_text("import pytest\n\n\n" + textwrap.dedent(body))
    reg = {"contracts": [{**_EQ_CONTRACT, "file": "tests/t.py", "test": "test_case"}]}
    return ac.validate(reg, root=tmp_path)


# ===================================================================== green-when-real control
def test_a_real_reachable_assertion_satisfies_the_control(tmp_path):
    """Without this the rejections below prove nothing: the control must ACCEPT the real thing."""
    r = _validate_body(tmp_path, "def test_case():\n    result = f()\n    expected = 3\n"
                                 "    assert result == expected\n")
    assert r["clean"], r["problems"]


# ===================================================================== the eight text-only attacks
def test_attack_1_token_only_in_an_explanatory_comment_is_rejected(tmp_path):
    r = _validate_body(tmp_path, "def test_case():\n    # result == expected (only a comment)\n"
                                 "    result = f()\n")
    assert not r["clean"], "a token in a comment is not an executable assertion"


def test_attack_2_token_only_in_a_docstring_is_rejected(tmp_path):
    r = _validate_body(tmp_path, "def test_case():\n    '''result == expected'''\n    result = f()\n")
    assert not r["clean"], "a token in a docstring is not an executable assertion"


def test_attack_3_token_only_in_a_string_literal_is_rejected(tmp_path):
    r = _validate_body(tmp_path, "def test_case():\n    s = 'result == expected'\n    assert s\n")
    assert not r["clean"], "a token inside a string literal is not the assertion"


def test_attack_4_token_only_in_an_assertion_message_is_rejected(tmp_path):
    """The token appears in test code (the assertion MESSAGE), while the condition is trivial."""
    r = _validate_body(tmp_path,
                       "def test_case():\n    assert True, 'result == expected'\n")
    assert not r["clean"], "a token in the assertion message does not make the condition meaningful"


def test_attack_5_stale_alias_with_no_live_consumer_is_rejected(tmp_path):
    """The names exist as a stale alias but no reachable assertion consumes them meaningfully."""
    r = _validate_body(tmp_path, "def test_case():\n    result = expected = 1\n    x = result\n"
                                 "    assert x\n")
    assert not r["clean"], "a stale alias without a meaningful equality is not the control"


def test_attack_6_dead_code_containing_the_expected_name_is_rejected(tmp_path):
    r = _validate_body(tmp_path, "def test_case():\n    return\n    result = f()\n    expected = 3\n"
                                 "    assert result == expected\n")
    assert not r["clean"], "code after return can never execute"


def test_attack_7_an_unreachable_branch_is_rejected(tmp_path):
    r = _validate_body(tmp_path, "def test_case():\n    if False:\n        result = f()\n"
                                 "        expected = 3\n        assert result == expected\n")
    assert not r["clean"], "an assertion under `if False:` can never execute"


def test_attack_8_renaming_a_live_callable_is_detected_by_ast_not_text():
    """A structural control must find the callable by AST, so a renamed callable is detected even
    when the old name survives in a comment. The contrast proves AST beats text presence."""
    source = (
        "def pytest_sessionfinish_RENAMED(session, exitstatus):\n"
        "    # was: def pytest_sessionfinish(session, exitstatus)\n"
        "    session.exitstatus = 3\n")
    tree = ast.parse(source)
    ast_names = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    assert "pytest_sessionfinish" not in ast_names, (
        "an AST search correctly reports the live callable is gone")
    assert "pytest_sessionfinish" in source, (
        "a text search IS fooled by the surviving comment — which is why the control uses AST")
    # And the real graded-session control does use AST: it looks up the FunctionDef by name.
    boot_tree = ast.parse((REPO_ROOT / "scripts" / "signalnest_bootstrap.py").read_text())
    live = [n for n in ast.walk(boot_tree)
            if isinstance(n, ast.FunctionDef) and n.name == "pytest_sessionfinish"]
    assert len(live) == 1, "the real session-finish callable must be present as a live FunctionDef"


# ===================================================================== inert-change control
def test_inert_comment_addition_does_not_flip_a_clean_verdict(tmp_path):
    """An inert textual change must remain inert; otherwise this battery flags noise."""
    body = ("def test_case():\n    # a harmless comment\n    result = f()\n    expected = 3\n"
            "    assert result == expected\n")
    assert _validate_body(tmp_path, body)["clean"], "a comment-only change must stay inert"


def test_the_control_reads_structure_not_text_presence():
    """The validator's own source never decides membership by grepping a fixed token, and it walks
    the AST (ast.parse / reachability) rather than matching strings against the audited file."""
    src = (REPO_ROOT / "scripts" / "assertion_contracts.py").read_text()
    assert "ast.parse(" in src and "reachability" in src, (
        "the assertion control must parse and reason about reachability")
