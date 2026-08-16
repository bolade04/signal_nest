"""Gate 4N-I27U — the four reproduced I27T blockers, closed and pinned.

Gate 4N-I27T's six-reviewer assessment REJECTED candidate 4N-I27T-CANDIDATE-1 five PASS to one
FAIL. Every one of the adversarial lane's four blockers was in scripts/failure_propagation.py,
and all four shared one root cause:

    THE ANALYSER DID NOT CONSERVATIVELY AND CORRECTLY MODEL EFFECTIVE SHELL ERROR-MODE AND
    TERMINAL-STATUS SEMANTICS.

They are pinned SEPARATELY here even though they collapse to one cause, because they fail in
four different ways and a single test would let three of them come back unnoticed.

THE ORACLE IS BASH, NOT THE DETECTOR. Every expectation below is derived from bash's own
documented behaviour, and the cases that can be settled by running a shell DO run one. Nothing
is enumerated from _REEXIT, ERRMODE_OFF, _HEREDOC_OPERATOR or any parser branch — Gate 4N-I27Q
was sunk by a corpus drawn from the detector's own recognised-form list, which can only confirm
what the detector already knew. tests below assert that prohibition structurally.
"""
from __future__ import annotations

import ast
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import failure_propagation as fp  # noqa: E402

FAILING = 'python3 -c "import sys; sys.exit(3)"'
EUO = "bash --noprofile --norc -euo pipefail {0}"


def run_bash(script: str, flags=("-euo", "pipefail"), tmp_path=None) -> int:
    """Ground truth. Does a failure inside `script` reach the shell's exit status?"""
    target = (tmp_path or Path("/tmp")) / "i27u_probe.sh"
    target.write_text(script, encoding="utf-8")
    return subprocess.run(["bash", "--noprofile", "--norc", *flags, str(target)],
                          capture_output=True, text=True).returncode


def analyse_step(tmp_path, run_body: str, shell: str = EUO, monkeypatch=None) -> dict:
    """The REAL analyse(), over a one-step synthetic workflow whose step is graded."""
    workflow = tmp_path / "wf.yml"
    body = textwrap.indent(run_body.rstrip("\n"), " " * 10)
    workflow.write_text(
        "name: probe\non: [push]\n"
        f"defaults:\n  run:\n    shell: {shell}\n"
        "jobs:\n  j:\n    runs-on: ubuntu-latest\n    steps:\n"
        "      - name: probe\n        id: probe\n        run: |\n" + body + "\n"
        '      - name: agg\n        run: echo "probe=${{ steps.probe.outcome }}"\n',
        encoding="utf-8")
    original = fp.WORKFLOW
    try:
        fp.WORKFLOW = workflow
        result = fp.analyse()
    finally:
        fp.WORKFLOW = original
    return next(s for s in result["steps"] if s["id"] == "probe")


# =====================================================================================
# AGENDA A — the computed errexit state was derived and then thrown away.
#
# _shell_options() set options["errexit"] and the step loop then wrote
# `errexit_disabled = False`, so deleting `-e` from defaults.run.shell changed REAL bash
# behaviour (exit 3 becomes exit 0) while every line still reported "plain command under
# `set -e`" — a statement that was then false.
# =====================================================================================

def test_a_bash_really_does_stop_caring_without_dash_e(tmp_path):
    """The premise, established by bash rather than assumed."""
    script = f"{FAILING}\necho reached\n"
    assert run_bash(script, ("-euo", "pipefail"), tmp_path) == 3
    assert run_bash(script, ("-uo", "pipefail"), tmp_path) == 0


@pytest.mark.parametrize("shell,errexit", [
    ("bash --noprofile --norc -euo pipefail {0}", True),
    ("bash --noprofile --norc -uo pipefail {0}", False),
    ("bash -e {0}", True),
    ("bash {0}", False),
    ("bash -o errexit {0}", True),
    ("bash -e -u {0}", True),
])
def test_a_the_shell_contract_reads_options_as_options(shell, errexit):
    """`"-e" in shell` was a SUBSTRING test: true for `--noprofile`, and for any path with -e."""
    contract = fp.shell_contract(shell)
    assert contract["determined"] is True
    assert contract["errexit"] is errexit


def test_a_a_cluster_ending_in_o_consumes_the_next_word():
    """`-euo pipefail` is `-e -u -o pipefail`. Missing this dropped pipefail on the real
    workflow, which would have silently re-judged every pipeline in it."""
    contract = fp.shell_contract(EUO)
    assert contract == {"shell": EUO, "determined": True,
                        "errexit": True, "pipefail": True, "nounset": True}


def test_a_an_unmodelled_shell_is_not_guessed_at():
    contract = fp.shell_contract("pwsh {0}")
    assert contract["determined"] is False
    assert "errexit" not in contract


def test_a_removing_dash_e_changes_the_classification(tmp_path):
    """THE BLOCKER. Same step, two shells: the verdict must follow the shell, not a constant."""
    with_e = analyse_step(tmp_path, FAILING, shell=EUO)
    without_e = analyse_step(tmp_path, FAILING,
                             shell="bash --noprofile --norc -uo pipefail {0}")
    assert with_e["lines"][0]["verdict"] == fp.PROPAGATES
    assert without_e["lines"][0]["verdict"] == fp.MASKED
    assert without_e["masked"], "a shell without -e must produce a finding on a graded step"


def test_a_an_undeterminable_shell_fails_closed(tmp_path):
    step = analyse_step(tmp_path, FAILING, shell="pwsh {0}")
    assert step["unknown"], "an unmodelled shell must fail closed, not default to safe"


def test_a_the_step_level_shell_wins_over_the_workflow_default():
    """GitHub resolves step, then job, then workflow — and the winner's options do not merge."""
    job = {"defaults": {"run": {"shell": "bash -e {0}"}}}
    assert fp.effective_shell(EUO, job, {"shell": "bash {0}"})["errexit"] is False
    assert fp.effective_shell(EUO, job, {})["errexit"] is True
    assert fp.effective_shell(EUO, {}, {})["errexit"] is True


def test_a_the_initial_state_is_derived_and_not_hardcoded():
    """Structural backstop for the behavioural test above.

    It is NOT enough to forbid `errexit_disabled = False` outright: restoring the mode after a
    `set -e` is a legitimate constant assignment, and banning it would force the fix to be
    written badly. What must hold is that at least one assignment DERIVES the state from
    something — the blocker was that every assignment was a literal.
    """
    source = (REPO_ROOT / "scripts" / "failure_propagation.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    analyse = next(n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef) and n.name == "analyse")
    assignments = [node.value for node in ast.walk(analyse)
                   if isinstance(node, ast.Assign) and len(node.targets) == 1
                   and getattr(node.targets[0], "id", "") == "errexit_disabled"]
    assert assignments, "analyse() no longer tracks errexit at all"
    assert any(not isinstance(value, ast.Constant) for value in assignments), (
        "every assignment to errexit_disabled is a literal, so the shell's real state is "
        "being discarded exactly as it was before Gate 4N-I27U")


# =====================================================================================
# AGENDA B — quoted prose satisfied the re-raise test.
#
# `|| echo "no false positives were suppressed"` classified PROPAGATES because \bfalse\b
# matched inside the MESSAGE. Bash disagrees: that line exits 0.
# =====================================================================================

def test_b_bash_says_the_prose_line_still_masks(tmp_path):
    assert run_bash('false || echo "no false positives were suppressed"\necho reached\n',
                    tmp_path=tmp_path) == 0


@pytest.mark.parametrize("line", [
    'pytest -q tests || echo "no false positives were suppressed"',
    'pytest -q tests || echo "soft gate; exit 1 was not reached"',
    "pytest -q tests || echo 'returns 1 only in theory'",
])
def test_b_quoted_prose_cannot_re_raise(line):
    assert fp.classify_line(line, pipefail=True, set_e_disabled=False)["verdict"] == fp.MASKED


@pytest.mark.parametrize("line,expected", [
    ("python3 x.py || true", fp.MASKED),
    ("python3 x.py || echo warn", fp.MASKED),
    ("python3 x.py || exit 1", fp.PROPAGATES),
    ("python3 x.py || false", fp.PROPAGATES),
])
def test_b_a_genuine_re_raise_still_propagates(line, expected):
    """The repair must not make the module blind to real re-raises."""
    assert fp.classify_line(line, pipefail=True, set_e_disabled=False)["verdict"] == expected


def test_b_a_comment_is_not_a_command():
    assert fp.shell_code("python3 x.py  # || exit 1").strip() == "python3 x.py"


def test_b_quoting_does_not_change_which_command_runs():
    """shell_code BLANKS quoted spans rather than deleting them, so `ec"h"o` cannot collapse
    into a keyword nobody wrote."""
    assert len(fp.shell_code('echo "abc"')) == len('echo "abc"')


# =====================================================================================
# AGENDA C — alternative error-mode-disabling forms evaded the detector.
#
# `builtin set +e` and `eval "set +e"` both genuinely disable errexit. ERRMODE_OFF was
# anchored on the literal token `set`, so neither was seen.
# =====================================================================================

@pytest.mark.parametrize("line", ["builtin set +e", 'eval "set +e"'])
def test_c_bash_confirms_these_really_disable_errexit(line, tmp_path):
    assert run_bash(f"{line}\nfalse\necho reached\n", tmp_path=tmp_path) == 0


@pytest.mark.parametrize("line", [
    "set +e", "set +o errexit", "set +ex", "set +xe",
    "builtin set +e", "builtin set +o errexit", "command set +e",
    'eval "set +e"', "eval 'set +o errexit'",
])
def test_c_every_effective_disabling_form_is_recognised(line):
    assert fp.errmode_effect(line) == fp.ERRMODE_DISABLED


@pytest.mark.parametrize("line", ["set -e", "set -o errexit", "builtin set -e"])
def test_c_restoring_forms_are_recognised(line):
    assert fp.errmode_effect(line) == fp.ERRMODE_RESTORED


@pytest.mark.parametrize("line", [
    'echo "remember to set +e later"',
    "# set +e is deliberately not used here",
    'printf "%s\\n" "set +o errexit"',
])
def test_c_prose_and_comments_never_change_the_mode(line):
    assert fp.errmode_effect(line) == fp.ERRMODE_NONE


@pytest.mark.parametrize("line", ['eval "$mode"', "eval $(cat mode.txt)", "eval `cat m`"])
def test_c_a_dynamic_eval_is_unproven_not_safe(line):
    """The conservative half. An eval whose text is not knowable here must not be assumed
    harmless — that assumption is exactly the fail-open shape this chain keeps finding."""
    assert fp.errmode_effect(line) == fp.ERRMODE_UNPROVEN


def test_c_a_dynamic_eval_fails_closed_in_a_graded_step(tmp_path):
    step = analyse_step(tmp_path, f'eval "$mode"\n{FAILING}')
    assert step["unknown"], "a dynamic eval must leave the step unproven, not passing"


def test_c_state_is_carried_across_the_command_sequence(tmp_path):
    step = analyse_step(tmp_path, f"builtin set +e\n{FAILING}\nset -e\n{FAILING}")
    verdicts = [line["verdict"] for line in step["lines"]]
    assert fp.MASKED in verdicts, "the disabled state must reach the following command"
    assert verdicts[-1] == fp.PROPAGATES, "a restore must actually restore"


# =====================================================================================
# AGENDA D — a quoted `<<` opened a heredoc and blinded the rest of the step.
#
# `echo "shifting << SWALLOW"` set a terminator no line ever matched, so every later line was
# skipped UNCLASSIFIED — including a following `set +e`. This was a direct consequence of the
# Gate 4N-I27R repair, which moved heredoc substitution BEFORE quote-stripping so that a real
# `<<'PY'` would still be seen. BOTH properties must hold; neither may be traded for the other.
# =====================================================================================

@pytest.mark.parametrize("line,tag", [
    ("cat <<EOF", "EOF"),
    ("cat <<-EOF", "EOF"),
    ("python3 - <<'PY'", "PY"),
    ('python3 - "$work" <<"PY"', "PY"),
    ("grep x <<'A_B1'", "A_B1"),
])
def test_d_valid_heredocs_are_still_recognised(line, tag):
    """The regression half. Losing these turns every inline-Python body back into 'shell'."""
    opener = fp.heredoc_opener(line)
    assert opener is not None and opener["tag"] == tag


@pytest.mark.parametrize("line", [
    'echo "shifting << SWALLOW"',
    "echo 'note << TAG'",
    "# see the note about << TAG",
    "python3 x.py  # heredoc << NOPE",
    'echo "EOF"',
    'cat <<< "a here-string has no body"',
])
def test_d_no_false_opener(line):
    assert fp.heredoc_opener(line) is None


def test_d_a_here_string_is_not_a_here_document():
    """`<<<` matched at its SECOND character until a look-behind was added, so a here-string
    swallowed the remainder of the step. Found by the corpus, not by reading the pattern."""
    assert fp.heredoc_opener('cat <<< "x"') is None
    assert fp.heredoc_opener("cat <<EOF")["tag"] == "EOF"


def test_d_a_false_opener_cannot_hide_a_following_set_plus_e(tmp_path):
    """THE BLOCKER, end to end."""
    step = analyse_step(tmp_path,
                        f'echo "shifting << SWALLOW"\nset +e\n{FAILING}')
    assert step["masked"], "the set +e after the fake opener must still be seen"


def test_d_bash_agrees_the_quoted_marker_is_inert(tmp_path):
    assert run_bash('echo "shifting << SWALLOW"\n(exit 7) || true\necho reached\n',
                    tmp_path=tmp_path) == 0


def test_d_a_heredoc_body_is_data(tmp_path):
    """A `set +e` inside a body must NOT disable anything: bash never executes it."""
    step = analyse_step(tmp_path, f"cat <<'EOF'\nset +e\nEOF\n{FAILING}")
    assert step["lines"][-1]["verdict"] == fp.PROPAGATES
    assert run_bash(f"cat <<'EOF'\nset +e\nEOF\n{FAILING}\necho reached\n",
                    tmp_path=tmp_path) == 3


def test_d_analysis_resumes_after_the_terminator(tmp_path):
    step = analyse_step(tmp_path, f"cat <<'EOF'\nhello\nEOF\nset +e\n{FAILING}")
    assert step["lines"][-1]["verdict"] == fp.MASKED, (
        "a real set +e AFTER the heredoc must be seen; skipping past the terminator is the "
        "same blinding in a different place")


def test_d_an_unterminated_heredoc_fails_closed(tmp_path):
    step = analyse_step(tmp_path, f"cat <<'EOF'\nhello\n{FAILING}")
    assert step["unknown"], "silence about unclassified lines is the defect, not the fix"
    assert any("unterminated" in line["line"] for line in step["unknown"])


def test_d_a_tab_indented_terminator_closes_a_dash_heredoc(tmp_path):
    step = analyse_step(tmp_path, "cat <<-EOF\n\tbody\n\tEOF\nset +e\n" + FAILING)
    assert step["lines"][-1]["verdict"] == fp.MASKED
    assert not any("unterminated" in line["line"] for line in step["lines"])


# =====================================================================================
# CORPUS INDEPENDENCE — the property that makes every test above worth anything.
# =====================================================================================

def test_the_oracle_does_not_import_the_detector_constants():
    """This module may import failure_propagation for its API and its verdict NAMES, but no
    expectation may be enumerated from a detector pattern. Reading `_REEXIT` or `ERRMODE_OFF`
    to build cases is the self-authored-oracle defect that sank Gate 4N-I27Q."""
    source = Path(__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    forbidden = {"_REEXIT", "ERRMODE_OFF", "ERRMODE_ON", "_HEREDOC_OPEN", "_HEREDOC_OPERATOR",
                 "_STATUS_ALTERING", "_BACKGROUND", "_QUOTED", "_EVAL", "ALWAYS_SUCCEEDS",
                 "_ERRMODE_PREFIX", "_DYNAMIC"}
    used = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    leaked = used & forbidden
    assert not leaked, (
        f"this oracle reads detector internals {sorted(leaked)}; its cases would then be "
        "enumerated from the thing under test and could only confirm it")


def test_the_clean_workflow_is_still_accepted():
    """A guard that cannot be green is not a guard. The repository's real workflow must pass."""
    result = fp.check()
    assert result["masked_lines"] == 0, result["problems"]
    assert result["unknown_lines"] == 0, result["problems"]
    assert result["clean"] is True, result["problems"]
    assert result["graded_steps"] == 46, "the graded set changed size unexpectedly"  # +1: Gate 4N-I28BH-B security_collection_assurance step  # INFRA-9-B3: +1 (root_wiring)
