"""Gate 4N-I28I, root cause RC-5 — a corpus enumerated from the SHELL, not from the module.

Gate 4N-I28G finding ARCH-03. The Gate 4N-I28C trap matrix ran 18 cases and the Gate 4N-I28E
replacement oracle 6 rows, both reporting two independent axes at 18/18 and 6/6 with zero
disagreements. Those numbers were sound *for the cases present*. Every single case placed `trap`
at word position 0, or behind `(` or `{` — precisely and only the prefixes `trap_effect()` handled.
No case in either corpus, and no test in the repository, exercised `; trap` or `; set`.

So the defect was never in the measurement. It was in the DOMAIN: an input shape the implementation
could not see was never generated, and no number of independent axes over that corpus could have
surfaced it. The zero-disagreement figure that supported a 39/39 readiness assessment was withdrawn
append-only at Gate 4N-I28H.

This corpus is built the other way round: from the shell's own composition grammar — every
separator class crossed with every install position and both scopes — and then executed. The seven
frozen Gate 4N-I28G disagreements are carried verbatim so they can never quietly leave the domain.
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import failure_propagation as fp  # noqa: E402

FAILING = 'python3 -c "import sys; sys.exit(7)"'

# ---------------------------------------------------------------------------------------------
# THE DOMAIN, enumerated from the shell. Separator classes x install position x scope.
# Nothing below is read from a production regex, constant, or branch name.
# ---------------------------------------------------------------------------------------------

SEPARATORS = [
    ("first", "{control}"),                       # control command first — the only old case
    ("after ;", "echo lead; {control}"),          # sequence
    ("after ; spaced", "echo lead ; {control}"),  # sequence, spaced
    ("after newline", "echo lead\n{control}"),    # newline boundary
    ("after &&", "echo lead && {control}"),       # AND list
    ("after ||", "false || {control}"),           # OR list
    ("in brace group", "{{ {control}; }}"),       # current shell
    ("in subshell", "( {control} )"),             # child shell
]

CONTROLS = [
    ("trap install absorbing", "trap 'exit 0' ERR", "ABSORBING"),
    ("trap install non-absorbing", "trap 'exit 1' ERR", "NOT_ABSORBING"),
    ("trap removal", "trap - ERR", "NOT_ABSORBING"),
    ("set disable", "set +e", "ERREXIT_OFF"),
    ("set restore", "set -e", "ERREXIT_ON"),
    ("quoted set disable", '"set" +e', "ERREXIT_OFF"),
    ("dynamic trap", 'trap "$T" ERR', "UNPROVEN"),
]

# The seven forms Gate 4N-I28G proved disagreed with bash. Carried verbatim from the frozen
# evidence so the domain can never silently lose them again.
FROZEN_I28G_DISAGREEMENTS = [
    "echo x; trap 'exit 0' ERR",
    "echo x; set +e",
    "cd .; set +o errexit",
    "true; set +e",
    "cd . ; trap 'exit 0' ERR",
    "true; trap 'exit 0' ERR",
    "trap 'exit 0' ERR; false",
]


def bash_ends_successfully(body: str) -> bool:
    """Ground truth. The guard is MANDATORY: without a failing command, exit 0 proves nothing."""
    assert FAILING in body, "a probe with no failing command cannot demonstrate absorption"
    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as handle:
        handle.write("T='exit 0'\n" + body.rstrip("\n") + "\necho REACHED\n")
        script = handle.name
    try:
        return subprocess.run(["bash", "--noprofile", "--norc", "-euo", "pipefail", script],
                              capture_output=True, text=True).returncode == 0
    finally:
        os.unlink(script)


def _cases():
    for sep_name, template in SEPARATORS:
        for ctrl_name, control, effect in CONTROLS:
            line = template.format(control=control)
            yield (f"{ctrl_name} / {sep_name}", line, effect,
                   "SUBSHELL" if sep_name == "in subshell" else "PARENT")


CASES = list(_cases())


# =====================================================================================
# AXIS ONE — independent expectation versus bash.
# =====================================================================================

@pytest.mark.parametrize("label,line,effect,scope", CASES)
def test_bash_settles_each_composed_form(label, line, effect, scope):
    """Whatever bash does IS the expectation; this records it and proves the probe is sound."""
    body = f"{line}\n{FAILING}"
    absorbs = bash_ends_successfully(body)
    if scope == "SUBSHELL":
        assert absorbs is False, f"{label}: a child shell must not change the parent"
    elif effect in ("ABSORBING", "ERREXIT_OFF", "UNPROVEN"):
        assert absorbs is True, f"{label}: bash should have absorbed the later failure"
    else:
        assert absorbs is False, f"{label}: bash should have propagated the failure"


# =====================================================================================
# AXIS TWO — the analyser versus that same expectation. Kept separate on purpose.
# =====================================================================================

def analyse_step(body: str) -> dict:
    """Run the real step analyser, which is what sees a multi-line body.

    `classify_line()` judges ONE line by contract, so a control separated by a NEWLINE is a
    separate line and is only observable here. Feeding a two-line string to classify_line would
    be testing it against an input it never receives.
    """
    workflow = Path(tempfile.mkdtemp()) / "wf.yml"
    workflow.write_text(
        "name: p\non: [push]\n"
        "defaults:\n  run:\n    shell: bash --noprofile --norc -euo pipefail {0}\n"
        "jobs:\n  j:\n    runs-on: ubuntu-latest\n    steps:\n"
        "      - name: p\n        id: p\n        run: |\n"
        + textwrap.indent(body.rstrip("\n"), " " * 10) + "\n"
        '      - name: agg\n        run: echo "p=${{ steps.p.outcome }}"\n', encoding="utf-8")
    original = fp.WORKFLOW
    try:
        fp.WORKFLOW = workflow
        return next(s for s in fp.analyse()["steps"] if s["id"] == "p")
    finally:
        fp.WORKFLOW = original


@pytest.mark.parametrize("label,line,effect,scope", CASES)
def test_the_analyser_never_asserts_propagation_where_bash_absorbs(label, line, effect, scope):
    """THE RC-1 PROPERTY. A fail-open here is the defect three lanes found."""
    body = f"{line}\n{FAILING}"
    absorbs = bash_ends_successfully(body)
    if not absorbs:
        return
    guard = analyse_step(body)["lines"][-1]["verdict"]
    assert guard in (fp.MASKED, fp.UNKNOWN), (
        f"{label}: bash absorbs the later failure but the guard after it is reported "
        f"{guard} — fail-open")


@pytest.mark.parametrize("line", FROZEN_I28G_DISAGREEMENTS)
def test_every_frozen_i28g_disagreement_is_closed(line):
    """The seven forms that disagreed with bash at Gate 4N-I28G, carried verbatim."""
    body = f"{line}\n{FAILING}" if FAILING not in line else line
    if FAILING not in body:
        body = f"{line}\n{FAILING}"
    absorbs = bash_ends_successfully(body)
    trap = fp.trap_effect(line)
    errmode = fp.errmode_effect(line)
    assert absorbs, f"{line}: the frozen evidence recorded bash exiting 0 here"
    assert trap in (fp.TRAP_ABSORBS, fp.TRAP_UNPROVEN) or errmode in (
        fp.ERRMODE_DISABLED, fp.ERRMODE_UNPROVEN), (
        f"{line}: still invisible to the analyser — trap={trap} errmode={errmode}")


def test_a_subshell_control_does_not_change_the_parent():
    assert fp.errmode_effect("echo x; ( set +e )") == fp.ERRMODE_NONE
    assert fp.trap_effect("( trap 'exit 0' ERR )") == fp.TRAP_NONABSORBING


def test_quoted_prose_containing_a_composed_control_stays_inert():
    """Widening recognition must not start reading strings as syntax."""
    assert fp.trap_effect("""echo "a; trap 'exit 0' ERR" """) == fp.TRAP_NONE
    assert fp.errmode_effect('echo "lead; set +e"') == fp.ERRMODE_NONE


# =====================================================================================
# RC-5 SELF-PROTECTION — the corpus must be able to fail.
# =====================================================================================

FORBIDDEN_PRODUCTION_NAMES = {
    "_TRAP_EXPLICIT_SUCCESS", "_TRAP_EXPLICIT_FAILURE", "_TRAP_STATUS_PRESERVING",
    "_TRAP_SIGNALS", "_TRAP_PREFIXES", "_TRAP_WORD", "_COMPOUND_KEYWORDS",
    "ERRMODE_OFF", "ERRMODE_ON", "_ERRMODE_WRAPPERS", "_WORD_SEPARATORS", "_HOSTED_ZONE",
}

DOMAIN_TABLES = {"SEPARATORS", "CONTROLS", "FROZEN_I28G_DISAGREEMENTS"}


def _module_ast():
    return ast.parse(Path(__file__).read_text(encoding="utf-8"))


def test_no_case_is_derived_from_a_production_constant():
    used = {n.attr for n in ast.walk(_module_ast()) if isinstance(n, ast.Attribute)}
    leaked = used & FORBIDDEN_PRODUCTION_NAMES
    assert not leaked, f"the corpus reads production internals {sorted(leaked)}"


def test_the_domain_tables_are_stated_data():
    seen = set()
    for node in _module_ast().body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") in DOMAIN_TABLES:
            seen.add(node.targets[0].id)
            for attr in ast.walk(node.value):
                assert not isinstance(attr, ast.Attribute), (
                    f"{node.targets[0].id} is built from an attribute read, not stated data")
    assert seen == DOMAIN_TABLES, f"domain tables missing: {sorted(DOMAIN_TABLES - seen)}"


def test_the_domain_contains_composed_forms_not_just_first_position():
    """THE ARCH-03 DEFECT, as a positive assertion. The old corpora would fail this."""
    positions = {name for name, _template in SEPARATORS}
    assert "first" in positions
    composed = positions - {"first"}
    assert len(composed) >= 5, f"only {sorted(composed)} composed positions — too narrow a domain"
    for required in ("after ;", "after newline", "after &&", "in brace group", "in subshell"):
        assert required in positions, f"the domain omits {required!r}"


def test_both_control_families_are_composed():
    """A domain that composed only `trap` would have missed the `set` half of the same defect."""
    families = {name.split()[0] for name, _c, _e in CONTROLS}
    assert {"trap", "set", "quoted", "dynamic"} <= families | {"quoted", "dynamic"}
    assert any(c.startswith("trap") for _n, c, _e in CONTROLS)
    assert any(c.startswith("set") or c.startswith('"set"') for _n, c, _e in CONTROLS)


def test_every_frozen_disagreement_is_still_in_the_domain():
    assert len(FROZEN_I28G_DISAGREEMENTS) == 7


def test_the_bash_probe_requires_a_failing_guard():
    """A probe with no failing command proves nothing — the adversarial lane's own first oracle
    had exactly that defect and was discarded rather than reported."""
    with pytest.raises(AssertionError):
        bash_ends_successfully("echo no failing command here")
