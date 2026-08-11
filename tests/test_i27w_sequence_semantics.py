"""Gate 4N-I27W — `;` sequence semantics, pinned against bash in BOTH errexit states.

Gate 4N-I27V found that `failure_propagation.classify_line()` returned MASKED for `cmd; true`
whatever the effective errexit state. Bash settles it and disagrees:

    false; true    under `set -e`   -> exit 1   (errexit fires on the FIRST command)
    false; true    without `-e`     -> exit 0   (execution reaches the trailing success)

One construct, two behaviours, and the module asserted one of them unconditionally.

WHY THIS FILE EXISTS SEPARATELY FROM THE I27U TESTS. I27V's real lesson was about MEASUREMENT,
not about one branch: I27U reported "44/44" for a metric that compared the analyser against a
STORED EXPECTATION and never compared that expectation against bash. Every assertion here that
can be settled by running a shell RUNS ONE, and the two questions are kept apart.
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import failure_propagation as fp  # noqa: E402

FAILING = 'python3 -c "import sys; sys.exit(3)"'
ON = ("-euo", "pipefail")
OFF = ("-uo", "pipefail")


def step_stops_at(line: str, flags, tmp_path) -> bool:
    """Does this line's failure STOP the step? Ground truth, from bash.

    A following `echo` runs only if the step did not stop, so a non-zero status here means the
    failure reached the step — which is exactly what PROPAGATES claims.
    """
    script = tmp_path / "probe.sh"
    script.write_text(line + "\necho reached\n", encoding="utf-8")
    return subprocess.run(["bash", "--noprofile", "--norc", *flags, str(script)],
                          capture_output=True, text=True).returncode != 0


def bash_output(line: str, tmp_path) -> str:
    script = tmp_path / "tok.sh"
    script.write_text(line + "\n", encoding="utf-8")
    return subprocess.run(["bash", "--noprofile", "--norc", "-euo", "pipefail", str(script)],
                          capture_output=True, text=True).stdout.strip()


# =====================================================================================
# THE FINDING — the same construct in both states.
# =====================================================================================

def test_bash_really_does_behave_differently_in_the_two_states(tmp_path):
    """The premise. If this ever stops holding, every assertion below is meaningless."""
    assert step_stops_at(f"{FAILING}; true", ON, tmp_path) is True
    assert step_stops_at(f"{FAILING}; true", OFF, tmp_path) is False


def test_a_trailing_success_does_not_mask_under_errexit():
    """THE BLOCKER. `;` separates commands, so errexit fires before `true` is ever reached."""
    verdict = fp.classify_line(f"{FAILING}; true", pipefail=True, set_e_disabled=False)
    assert verdict["verdict"] == fp.PROPAGATES, verdict["why"]


def test_a_trailing_success_does_mask_when_errexit_is_disabled():
    """The other half. Correcting one state by breaking the other would not be a fix."""
    verdict = fp.classify_line(f"{FAILING}; true", pipefail=True, set_e_disabled=True)
    assert verdict["verdict"] == fp.MASKED, verdict["why"]


@pytest.mark.parametrize("line", [
    f"{FAILING}; true",
    f"true; {FAILING}",
    f"true; {FAILING}; true",
    f"{FAILING}; echo done",
])
def test_every_sequence_shape_propagates_under_errexit(line, tmp_path):
    """Analyser and bash checked SEPARATELY, then required to agree."""
    verdict = fp.classify_line(line, pipefail=True, set_e_disabled=False)["verdict"]
    assert verdict == fp.PROPAGATES, line
    assert step_stops_at(line, ON, tmp_path) is True, f"bash disagrees for {line!r}"


# =====================================================================================
# BASH CONTEXTS WHERE `-e` IS SUSPENDED. Refusing to model these is the point.
# =====================================================================================

@pytest.mark.parametrize("line", [
    "if false; then echo hi; fi",
    "while false; do :; done",
    "until true; do :; done",
    "for x in a b; do :; done",
    "! false; true",
    "case $x in a) :;; esac",
])
def test_compound_and_conditional_context_is_unknown(line):
    """`-e` does not exit for the command being TESTED, so a sequence rule cannot apply."""
    assert fp.classify_line(line, pipefail=True,
                            set_e_disabled=False)["verdict"] == fp.UNKNOWN


def test_bash_confirms_errexit_is_suspended_in_a_condition(tmp_path):
    assert step_stops_at("if false; then echo hi; fi", ON, tmp_path) is False
    assert step_stops_at("while false; do :; done", ON, tmp_path) is False
    assert step_stops_at("! false", ON, tmp_path) is False


@pytest.mark.parametrize("line", [f"({FAILING}; true)", f"{{ {FAILING}; true; }}"])
def test_subshell_and_group_sequences_are_unknown(line):
    """A boundary this module does not model must refuse, not guess."""
    assert fp.classify_line(line, pipefail=True,
                            set_e_disabled=False)["verdict"] == fp.UNKNOWN


# =====================================================================================
# A `;` IN DATA IS NOT A SEPARATOR.
# =====================================================================================

@pytest.mark.parametrize("line,literal", [
    ('echo "a; b"', "a; b"),
    ("echo 'x; true'", "x; true"),
    ('eval "echo one; echo two"', "one\ntwo"),
])
def test_a_semicolon_in_data_does_not_split_the_line(line, literal, tmp_path):
    """Checked by what bash PRINTS, not by whether it exits.

    A line that cannot fail proves nothing about propagation; running `echo "a; b"` and seeing
    exit 0 says only that echo works. The question these cases ask is whether the `;` SEPARATES,
    and bash answers that with its output. Using a propagation probe here produced five
    spurious disagreements before the oracle was corrected.
    """
    assert bash_output(line, tmp_path) == literal
    assert fp.classify_line(line, pipefail=True,
                            set_e_disabled=False)["verdict"] == fp.PROPAGATES


def test_a_semicolon_in_a_comment_does_not_split_the_line():
    assert fp.shell_code("python3 x.py  # ; true").strip() == "python3 x.py"
    assert fp.classify_line("python3 x.py  # ; true", pipefail=True,
                            set_e_disabled=False)["verdict"] == fp.PROPAGATES


def test_the_splitter_cuts_raw_text_at_code_positions():
    """Splitting raw text directly would cut inside a quoted string; splitting the blanked code
    would lose the original characters. Positions from code, content from raw."""
    raw = 'echo "a; b"; true'
    assert fp._split_on_code(raw, fp.shell_code(raw), ";") == ['echo "a; b"', " true"]


# =====================================================================================
# THE STATE-CONSUMPTION PROPERTY, stated as a test rather than as prose.
# =====================================================================================

def test_the_sequence_branch_consults_the_errexit_state():
    """The defect was a branch asserting an errexit-OFF conclusion from inside the region that
    only runs when errexit is ON. Same line, two states, two answers."""
    line = f"{FAILING}; true"
    on = fp.classify_line(line, pipefail=True, set_e_disabled=False)["verdict"]
    off = fp.classify_line(line, pipefail=True, set_e_disabled=True)["verdict"]
    assert on != off, "the sequence branch is ignoring the errexit state again"


def test_no_classify_line_branch_claims_masked_for_a_plain_sequence_under_errexit():
    """A structural backstop: MASKED is an errexit-OFF answer for a plain `;` sequence."""
    for trailing in ("true", ":", "echo ok", "printf x"):
        verdict = fp.classify_line(f"{FAILING}; {trailing}", pipefail=True,
                                   set_e_disabled=False)["verdict"]
        assert verdict != fp.MASKED, f"`; {trailing}` masked under errexit"


def test_the_oracle_does_not_import_the_detector_constants():
    """Same prohibition the I27U oracle carries: expectations may not be enumerated from the
    thing under test, or they can only ever confirm it."""
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    forbidden = {"_REEXIT", "ERRMODE_OFF", "ERRMODE_ON", "_HEREDOC_OPEN", "_HEREDOC_OPERATOR",
                 "_STATUS_ALTERING", "_BACKGROUND", "_EVAL", "ALWAYS_SUCCEEDS",
                 "_COMPOUND_CONTEXT", "_ERRMODE_PREFIX", "_DYNAMIC"}
    used = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    leaked = used & forbidden
    assert not leaked, f"this oracle reads detector internals {sorted(leaked)}"


def test_the_clean_workflow_is_still_accepted():
    """The repository's real workflow must still pass; a guard that cannot be green is not one."""
    result = fp.check()
    assert result["masked_lines"] == 0, result["problems"]
    assert result["unknown_lines"] == 0, result["problems"]
    assert result["clean"] is True, result["problems"]
