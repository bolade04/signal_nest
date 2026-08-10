"""Gate 4N-I27Z — compound context, ERR traps, and quoted command words.

Gate 4N-I27Y rejected candidate 4N-I27Y-CANDIDATE-1 two PASS to four FAIL. Three lanes —
architecture, security and adversarial — independently found the same defect, and the coordinator
reproduced it: `_in_compound_context()` had exactly ONE call site, inside the `";" in code`
branch, so the entire compound model was unreachable for any line without a semicolon. Bash
settles what that cost:

    ! false                 -> exit 0    analyser said PROPAGATES
    if CMD / then / fi      -> exit 0    analyser said PROPAGATES
    if CMD | cat; then ...  -> exit 0    analyser said PROPAGATES

The adversarial lane added two more, each a fail-open in the mandatory release control:
`trap 'exit 0' ERR` was entirely unmodelled, and `shell_code()` — the Gate 4N-I27U repair that
stopped DATA acting as SYNTAX — turned out to be one-way, so `"set" +e` disabled errexit while
the module reported nothing.

THE ORACLE IS BASH. Every expectation below that a shell can settle is settled by running one.
Nothing is enumerated from the module's own patterns; a test that reads the detector's constants
can only confirm what the detector already knows, which is the failure that rejected I27Q.
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

FAILING = 'python3 -c "import sys; sys.exit(7)"'
ON = ("-euo", "pipefail")


def step_stops_at(body: str, tmp_path, flags=ON) -> bool:
    """Ground truth: does a failure inside `body` stop the step? A following echo runs only if
    it did not."""
    script = tmp_path / "probe.sh"
    script.write_text(body.rstrip("\n") + "\necho reached\n", encoding="utf-8")
    return subprocess.run(["bash", "--noprofile", "--norc", *flags, str(script)],
                          capture_output=True, text=True).returncode != 0


def verdict(line: str, *, disabled: bool = False) -> str:
    return fp.classify_line(line, pipefail=True, set_e_disabled=disabled)["verdict"]


def analyse_step(tmp_path, run_body: str,
                 shell: str = "bash --noprofile --norc -euo pipefail {0}") -> dict:
    workflow = tmp_path / "wf.yml"
    workflow.write_text(
        "name: probe\non: [push]\n"
        f"defaults:\n  run:\n    shell: {shell}\n"
        "jobs:\n  j:\n    runs-on: ubuntu-latest\n    steps:\n"
        "      - name: probe\n        id: probe\n        run: |\n"
        + textwrap.indent(run_body.rstrip("\n"), " " * 10) + "\n"
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
# AGENDA A — compound context, checked independently of any separator.
# =====================================================================================

@pytest.mark.parametrize("body", [
    "! false",
    f"if {FAILING}\nthen echo ok\nfi",
    f"while {FAILING}\ndo break\ndone",
    f"if {FAILING} | cat; then echo ok; fi",
])
def test_bash_absorbs_the_failure_in_these_constructs(body, tmp_path):
    """The premise. Every claim below is meaningless if this stops holding."""
    assert step_stops_at(body, tmp_path) is False


@pytest.mark.parametrize("line", [
    "! false",
    "! false | cat",
    f"if {FAILING}",
    f"while {FAILING}",
    "until true",
    "for x in a b",
    "case $x in",
    "then", "else", "elif true", "fi", "do", "done", "esac",
    f"if {FAILING} | cat; then echo ok; fi",
])
def test_every_compound_form_is_unknown_with_or_without_a_semicolon(line):
    """THE BLOCKER. None of these carries a `;`, and that used to be the only way in."""
    assert verdict(line) == fp.UNKNOWN, line


def test_the_compound_check_is_not_reachable_from_only_one_branch():
    """Structural backstop for the root cause: the helper must have more than a single call
    site buried inside the sequence branch."""
    source = (REPO_ROOT / "scripts" / "failure_propagation.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    classify = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == "classify_line")
    called = {getattr(c.func, "id", getattr(c.func, "attr", ""))
              for c in ast.walk(classify) if isinstance(c, ast.Call)}
    assert "in_compound_context" in called, (
        "classify_line no longer asks the compound question directly; if it is reachable only "
        "from a separator branch the Gate 4N-I27Y defect is back")


@pytest.mark.parametrize("line", [
    "python3 x.py; echo done",
    "echo done",
    'echo "if you fail, retry"',
    'printf "%s" "while true"',
    "python3 x.py  # then fi done",
])
def test_a_keyword_that_is_not_in_command_position_is_inert(line):
    """The Gate 4N-I27W property, preserved: `done` as an ARGUMENT is not syntax."""
    assert verdict(line) != fp.UNKNOWN, line


def test_a_graded_step_with_a_bare_compound_opener_fails_closed(tmp_path):
    step = analyse_step(tmp_path, f"if {FAILING}\nthen echo ok\nfi")
    assert step["unknown"], "a compound opener must produce a finding on a graded step"


# =====================================================================================
# AGENDA B — ERR/EXIT traps.
# =====================================================================================

def test_bash_confirms_an_exit_zero_err_trap_absorbs_failure(tmp_path):
    assert step_stops_at(f"trap 'exit 0' ERR\n{FAILING}", tmp_path) is False
    assert step_stops_at(FAILING, tmp_path) is True          # discriminating control


# GATE 4N-I28B, FINDING I28A-01. What stood here was a parametrised list asserting five forms
# were ABSORBS against fp.trap_effect and nothing else — in a file whose docstring says the oracle
# is bash. Three of the five were simply wrong, and the test encoded the error instead of catching
# it. The replacement below states each case's BASH RESULT as data and checks two things
# separately: that bash really does what the case claims, and that the analyser agrees with bash.
#
#     (trap line, does bash end the script successfully?, why)
TRAP_CASES = [
    ("trap 'exit 0' ERR",  True,  "the trap exits, with status 0"),
    ("trap 'exit 0' EXIT", True,  "an EXIT trap that exits zero replaces the failing status"),
    ("trap 'exit 1' ERR",  False, "the trap exits, with a failing status"),
    ("trap 'exit 7' ERR",  False, "likewise"),
    ("trap 'exit' ERR",    False, "bare `exit` reuses the CURRENT status, which is the failure"),
    ("trap ':' ERR",       False, "the body returns 0; errexit still terminates the shell"),
    ("trap 'true' ERR",    False, "returning 0 is not the same as EXITING with 0"),
    ("trap 'true' EXIT",   False, "an EXIT trap that does not exit cannot change the status"),
    ("( trap 'exit 0' ERR )", False, "a subshell trap dies with the subshell"),
    ("{ trap 'exit 0' ERR; }", True, "a brace group runs in the CURRENT shell, so it binds"),
]


@pytest.mark.parametrize("line,ends_successfully,why", TRAP_CASES)
def test_bash_confirms_each_trap_case(line, ends_successfully, why, tmp_path):
    """AXIS ONE: the stated expectation against a real shell. If this fails, the table is wrong."""
    assert step_stops_at(f"{line}\n{FAILING}", tmp_path) is not ends_successfully, why


@pytest.mark.parametrize("line,ends_successfully,why", TRAP_CASES)
def test_the_analyser_agrees_with_bash_on_each_trap_case(line, ends_successfully, why):
    """AXIS TWO: the analyser against that same expectation. Kept separate on purpose — collapsing
    the two axes is how Gate 4N-I27V's defect stayed hidden for two gates."""
    effect = fp.trap_effect(line)
    if ends_successfully:
        assert effect in (fp.TRAP_ABSORBS, fp.TRAP_UNPROVEN), (
            f"bash ends successfully here ({why}); calling this {effect} is fail-OPEN")
    else:
        assert effect != fp.TRAP_ABSORBS, (
            f"bash exits non-zero here ({why}); ABSORBS claims a masking that does not happen")


@pytest.mark.parametrize("line,scope", [
    ("trap 'exit 0' ERR", fp.TRAP_SCOPE_PARENT),
    ("{ trap 'exit 0' ERR; }", fp.TRAP_SCOPE_PARENT),
    ("( trap 'exit 0' ERR )", fp.TRAP_SCOPE_SUBSHELL),
    ("{ ( trap 'exit 0' ERR ); }", fp.TRAP_SCOPE_SUBSHELL),
    ('echo "( trap x ERR )"', fp.TRAP_SCOPE_PARENT),
])
def test_trap_scope_distinguishes_a_subshell_from_a_group(line, scope):
    """`( … )` is a child shell; `{ …; }` is not. A parenthesis inside quotes opens neither."""
    assert fp.trap_scope(line) == scope


def test_a_subshell_trap_does_not_clear_the_parents(tmp_path):
    """Bash: an absorbing parent trap survives a replacement attempted inside a subshell."""
    assert step_stops_at(f"trap 'exit 0' ERR\n( trap 'exit 1' ERR )\n{FAILING}", tmp_path) is False
    assert step_stops_at(f"trap 'exit 0' ERR\ntrap 'exit 1' ERR\n{FAILING}", tmp_path) is True


@pytest.mark.parametrize("line", ['trap "$T" ERR', "trap 'cleanup && exit 3' ERR",
                                  "trap 'echo bye; exit 0' ERR"])
def test_a_trap_this_module_cannot_resolve_is_unproven(line):
    """A body that replaces the status with something unmodelled must not be called safe."""
    assert fp.trap_effect(line) == fp.TRAP_UNPROVEN


@pytest.mark.parametrize("line", ["trap 'exit 1' ERR", "trap 'exit' ERR", "trap ':' ERR",
                                  "( trap 'exit 0' ERR )"])
def test_a_statically_known_nonabsorbing_trap_says_so(line):
    """Refusing to call these ABSORBS is the fix; naming them NONABSORBING is the point of it."""
    assert fp.trap_effect(line) == fp.TRAP_NONABSORBING


def test_trap_removal_is_recognised():
    assert fp.trap_effect("trap - ERR") == fp.TRAP_REMOVED


@pytest.mark.parametrize("line", ['echo "we set a trap ERR here"', "trap 'cleanup' USR1",
                                  'printf "%s" "trap exit 0 ERR"'])
def test_prose_and_unrelated_signals_do_not_install_a_trap(line):
    assert fp.trap_effect(line) == fp.TRAP_NONE


def test_an_absorbing_trap_masks_the_rest_of_the_step(tmp_path):
    step = analyse_step(tmp_path, f"trap 'exit 0' ERR\n{FAILING}")
    assert step["masked"], "the guard after an absorbing trap must be reported masked"


def test_a_dynamic_trap_body_fails_closed_in_a_graded_step(tmp_path):
    step = analyse_step(tmp_path, f'T="exit 0"\ntrap "$T" ERR\n{FAILING}')
    assert step["unknown"] or step["masked"], "an unprovable trap must produce a finding"


def test_removing_the_trap_restores_ordinary_reasoning(tmp_path):
    step = analyse_step(tmp_path, f"trap 'exit 0' ERR\ntrap - ERR\n{FAILING}")
    assert step["lines"][-1]["verdict"] == fp.PROPAGATES
    assert step_stops_at(f"trap 'exit 0' ERR\ntrap - ERR\n{FAILING}", tmp_path) is True


# =====================================================================================
# AGENDA C — a quoted command word is still that command.
# =====================================================================================

@pytest.mark.parametrize("form", ['"set" +e', "'set' +e", "\\set +e", "s'e't +e",
                                  'builtin "set" +e', '"builtin" set +e'])
def test_bash_confirms_every_spelling_disables_errexit(form, tmp_path):
    """Quoting changes how a word is PARSED, not which word it IS."""
    script = tmp_path / "q.sh"
    script.write_text(f"{form}\nfalse\necho SURVIVED\n", encoding="utf-8")
    done = subprocess.run(["bash", "--noprofile", "--norc", *ON, str(script)],
                          capture_output=True, text=True)
    assert done.returncode == 0 and "SURVIVED" in done.stdout, form


@pytest.mark.parametrize("form", ['"set" +e', "'set' +e", "\\set +e", "s'e't +e",
                                  'builtin "set" +e', '"builtin" set +e', "set +e",
                                  "set +o errexit", "set +ex"])
def test_every_quoted_spelling_is_recognised_as_disabling(form):
    assert fp.errmode_effect(form) == fp.ERRMODE_DISABLED, form


@pytest.mark.parametrize("form", ['"set" -e', "'set' -e", "set -e", "builtin set -e"])
def test_quoted_restore_forms_are_recognised(form):
    assert fp.errmode_effect(form) == fp.ERRMODE_RESTORED, form


@pytest.mark.parametrize("line", ['echo "remember set +e later"', 'echo "set -e"',
                                  'printf "%s" "set +e"', "# set +e in a comment"])
def test_quoted_prose_remains_inert(line):
    """The Gate 4N-I27U property. Both halves must hold at once, or the fix is a trade."""
    assert fp.errmode_effect(line) == fp.ERRMODE_NONE, line


def test_bash_confirms_prose_does_not_disable_errexit(tmp_path):
    assert step_stops_at(f'echo "remember set +e later"\n{FAILING}', tmp_path) is True


def test_the_lexer_reports_command_position_and_quoting():
    words = fp.shell_words('echo "a b" ; "set" +e')
    assert [w["word"] for w in words] == ["echo", "a b", "set", "+e"]
    assert [w["command_position"] for w in words] == [True, False, True, False]
    assert words[1]["quoted"] is True and words[2]["quoted"] is True


def test_a_dynamic_word_is_marked_dynamic():
    assert any(w["dynamic"] for w in fp.shell_words('eval "$mode"'))


# =====================================================================================
# The properties that make all of the above worth anything.
# =====================================================================================

def test_the_oracle_does_not_import_the_detector_constants():
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    forbidden = {"_REEXIT", "ERRMODE_OFF", "ERRMODE_ON", "_HEREDOC_OPEN", "_HEREDOC_OPERATOR",
                 "_STATUS_ALTERING", "_BACKGROUND", "_EVAL", "ALWAYS_SUCCEEDS",
                 "_COMPOUND_KEYWORDS", "_ERRMODE_PREFIX", "_DYNAMIC", "_TRAP_SIGNALS",
                 "_TRAP_EXIT_ZERO", "_WORD_SEPARATORS", "_COMMAND_SEPARATOR",
                 # GATE 4N-I28B: the guard has to learn every new internal, or it protects only
                 # the ones that existed when it was written. A Gate 4N-I28B falsification —
                 # rewrite an expectation as `fp._TRAP_EXPLICIT_SUCCESS.match(...)` — went
                 # UNCAUGHT because these four names were not yet listed.
                 "_TRAP_EXPLICIT_SUCCESS", "_TRAP_EXPLICIT_FAILURE", "_TRAP_STATUS_PRESERVING",
                 "_TRAP_PREFIXES", "_TRAP_WORD"}
    used = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    leaked = used & forbidden
    assert not leaked, f"this oracle reads detector internals {sorted(leaked)}"


def test_the_clean_workflow_is_still_accepted():
    result = fp.check()
    assert result["masked_lines"] == 0, result["problems"]
    assert result["unknown_lines"] == 0, result["problems"]
    assert result["clean"] is True, result["problems"]
    assert result["graded_steps"] == 45  # +1: Gate 4N-I28BH-B security_collection_assurance step
