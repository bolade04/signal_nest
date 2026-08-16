"""Gate 4N-I28S — executable command-root derivation, release role, and the I28Q comment pins.

WHAT THIS FILE EXISTS TO PREVENT. Gate 4N-I28Q rejected a candidate because two comment-only edits
to .github/workflows/ci.yml moved the derived site universe — 457 -> 454 on a deletion, 457 -> 465
on an addition — while every control stayed green and CI behaviour stayed byte-identical. Root
membership was a property of prose. The mutations in RC-S5 below are those exact two edits, pinned
so the defect cannot return silently.

THE HARNESS DISCIPLINE. Every matrix case is written against a synthetic script in a temporary
directory, never against the repository, and each assertion names the outcome it expects rather
than asserting "not the wrong one" — a test that only forbids one answer passes when the model
returns a third. The classification vocabulary is three-valued on purpose: EXECUTABLE_INVOCATION,
NONEXECUTABLE_MENTION, and UNRESOLVED_MENTION, the last of which FAILS CLOSED.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from collections.abc import Mapping

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import shell_command_model as scm  # noqa: E402
import site_taxonomy as st  # noqa: E402

CI = REPO_ROOT / ".github" / "workflows" / "ci.yml"
COMMENT_LINE = 407          # the sole textual mention of smoke_http.py in the workflow


# ------------------------------------------------------------------ helpers
@pytest.fixture
def sandbox(tmp_path):
    """A scripts/ directory the model will accept, with a real target script present."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    return scripts


def write(scripts: Path, name: str, body: str) -> Path:
    p = scripts / name
    p.write_text(body)
    return p


def classify(path: Path, module: str = "smoke_http.py") -> set[str]:
    scm.reset_caches()
    s = scm.analyse(path)
    return {m["classification"] for m in s.mentions if m["module"] == module} or {"NO_MENTION"}


def invoked_modules(path: Path) -> set[str]:
    scm.reset_caches()
    return {c["module"] for c in scm.python_invocations(path) if c.get("module")}


# ================================================================== PHASE G
# The 25 root-model cases. Each names the classification it requires.

def test_g01_bash_script_invoking_a_python_script(sandbox):
    p = write(sandbox, "a.sh", 'python3 scripts/smoke_http.py\n')
    assert invoked_modules(p) == {"smoke_http.py"}
    assert classify(p) == {scm.EXECUTABLE_INVOCATION}


def test_g02_sh_script_invoking_a_python_script(sandbox):
    p = write(sandbox, "a.sh", '#!/bin/sh\npython scripts/smoke_http.py --once\n')
    assert invoked_modules(p) == {"smoke_http.py"}


def test_g03_directly_executable_script_is_followed(sandbox):
    write(sandbox, "inner.sh", 'python3 scripts/smoke_http.py\n')
    p = write(sandbox, "outer.sh", 'bash scripts/inner.sh\n')
    # The nested script is recorded as an executable invocation of a shell script.
    scm.reset_caches()
    assert any(c.get("nested_shell_script") == "scripts/inner.sh"
               for c in scm.analyse(p).commands)


def test_g04_a_comment_containing_a_python_path_creates_no_root(sandbox):
    p = write(sandbox, "a.sh", '# runs scripts/smoke_http.py eventually\ntrue\n')
    assert invoked_modules(p) == set()
    assert classify(p) == {scm.NONEXECUTABLE_MENTION}


def test_g05_a_quoted_example_creates_no_root(sandbox):
    p = write(sandbox, "a.sh", 'echo "usage: python3 scripts/smoke_http.py"\n')
    assert invoked_modules(p) == set()
    assert classify(p) == {scm.NONEXECUTABLE_MENTION}


def test_g06_heredoc_documentation_creates_no_root(sandbox):
    p = write(sandbox, "a.sh", 'cat <<EOF\nsee scripts/smoke_http.py for details\nEOF\n')
    assert invoked_modules(p) == set()
    assert classify(p) == {scm.NONEXECUTABLE_MENTION}


def test_g07_echo_of_a_path_creates_no_root(sandbox):
    p = write(sandbox, "a.sh", 'echo scripts/smoke_http.py\n')
    assert invoked_modules(p) == set()
    assert classify(p) == {scm.NONEXECUTABLE_MENTION}


def test_g08_an_assigned_but_never_executed_vector_creates_no_root(sandbox):
    p = write(sandbox, "a.sh", 'CMD="python3 scripts/smoke_http.py"\ntrue\n')
    assert invoked_modules(p) == set()
    assert classify(p) == {scm.NONEXECUTABLE_MENTION}


def test_g09_a_wrapper_function_that_executes_produces_a_root(sandbox):
    p = write(sandbox, "a.sh", 'run() {\n  python3 scripts/smoke_http.py\n}\nrun\n')
    assert invoked_modules(p) == {"smoke_http.py"}


def test_g10_a_wrapper_function_that_only_prints_does_not(sandbox):
    p = write(sandbox, "a.sh", 'show() {\n  echo scripts/smoke_http.py\n}\nshow\n')
    assert invoked_modules(p) == set()
    assert scm.EXECUTABLE_INVOCATION not in classify(p)


def test_g11_a_literal_interpreter_assignment_resolves(sandbox):
    p = write(sandbox, "a.sh", 'PY=/usr/bin/python3\n"$PY" scripts/smoke_http.py\n')
    assert invoked_modules(p) == {"smoke_http.py"}


def test_g12_an_unknown_interpreter_variable_is_unresolved_not_guessed(sandbox):
    p = write(sandbox, "a.sh", '"$MYSTERY" scripts/smoke_http.py\n')
    assert invoked_modules(p) == set(), "an unknown interpreter must not silently create a root"
    assert classify(p) == {scm.UNRESOLVED_MENTION}, "and must not be silently discarded either"


def test_g13_argument_forwarding_is_recorded(sandbox):
    p = write(sandbox, "a.sh", 'python3 scripts/smoke_http.py --check --strict\n')
    scm.reset_caches()
    cmd = [c for c in scm.python_invocations(p) if c["module"] == "smoke_http.py"][0]
    assert cmd["argv"] == ["--check", "--strict"]


def test_g14_a_finite_literal_loop_resolves(sandbox):
    p = write(sandbox, "a.sh",
              'for f in a b; do\n  python3 scripts/smoke_http.py "$f"\ndone\n')
    assert invoked_modules(p) == {"smoke_http.py"}


def test_g15_a_dynamic_loop_body_still_records_the_execution(sandbox):
    p = write(sandbox, "a.sh",
              'for f in $(ls); do\n  python3 scripts/smoke_http.py "$f"\ndone\n')
    assert invoked_modules(p) == {"smoke_http.py"}
    scm.reset_caches()
    cmd = [c for c in scm.python_invocations(p) if c["module"] == "smoke_http.py"][0]
    assert cmd["argv_fully_resolved"] is False, "the argv is dynamic and must say so"


def test_g16_an_executed_conditional_branch_produces_a_root(sandbox):
    p = write(sandbox, "a.sh", 'if true; then\n  python3 scripts/smoke_http.py\nfi\n')
    assert invoked_modules(p) == {"smoke_http.py"}


def test_g17_a_statically_false_branch_produces_no_root(sandbox):
    p = write(sandbox, "a.sh", 'if false; then\n  python3 scripts/smoke_http.py\nfi\n')
    assert invoked_modules(p) == set()
    assert classify(p) == {scm.NONEXECUTABLE_MENTION}


def test_g18_exec_produces_a_root(sandbox):
    p = write(sandbox, "a.sh", 'exec python3 scripts/smoke_http.py\n')
    assert invoked_modules(p) == {"smoke_http.py"}


def test_g19_a_pipeline_member_produces_a_root(sandbox):
    p = write(sandbox, "a.sh", 'python3 scripts/smoke_http.py | tee out.log\n')
    assert invoked_modules(p) == {"smoke_http.py"}


def test_g20_a_command_substitution_produces_a_root(sandbox):
    p = write(sandbox, "a.sh", 'OUT="$(python3 scripts/smoke_http.py)"\n')
    assert invoked_modules(p) == {"smoke_http.py"}, \
        "a command substitution executes; ignoring it is a false exclusion"


def test_g21_dynamic_eval_is_unresolved_and_fails_closed(sandbox):
    p = write(sandbox, "a.sh", 'eval "python3 scripts/smoke_http.py"\n')
    assert invoked_modules(p) == set()
    assert classify(p) == {scm.UNRESOLVED_MENTION}


def test_g21b_unquoted_eval_is_also_unresolved_and_does_not_become_a_root(sandbox):
    """The quoted form above passes even without the eval guard, so it cannot prove it exists.

    Falsification q12 disabled the eval branch and nothing failed: with the argument quoted, the
    fall-through happens to reach the same UNRESOLVED answer by another path. Unquoted, removing
    the guard lets `eval` be treated as a mere command prefix and `python3` becomes the executable
    — manufacturing a root out of a string the shell assembles at runtime.
    """
    p = write(sandbox, "a.sh", 'eval python3 scripts/smoke_http.py\n')
    assert invoked_modules(p) == set(), (
        "an unquoted eval produced a command root; eval builds its command at runtime and must "
        "fail closed instead of being read as a prefix")
    assert classify(p) == {scm.UNRESOLVED_MENTION}


def test_g22_a_nested_shell_script_is_followed_to_its_python(sandbox):
    write(sandbox, "inner.sh", 'python3 scripts/smoke_http.py\n')
    p = write(sandbox, "outer.sh", 'bash scripts/inner.sh\n')
    assert invoked_modules(p) == {"smoke_http.py"}


def test_g23_one_module_invoked_from_two_steps_is_one_root_with_two_steps():
    st.reset_caches()
    roots = {r["module"]: r for r in st.release_roots()}
    multi = [m for m, r in roots.items() if len(r["release_entry_points"]) > 1]
    assert multi, "the workflow does invoke at least one module from more than one step"
    for module in multi:
        assert len([r for r in st.release_roots() if r["module"] == module]) == 1


def test_g24_the_same_executable_with_different_argv_keeps_both_invocations():
    st.reset_caches()
    multi = [r for r in st.release_roots() if len(r["invocations"]) > 1]
    assert multi, "the workflow does run at least one module with more than one argv"
    for root in multi:
        assert len({tuple(a) for a in root["invocations"]}) == len(root["invocations"])


def test_g25_a_comment_only_mutation_changes_no_classification(sandbox):
    body = 'python3 scripts/smoke_http.py\n'
    a = write(sandbox, "a.sh", body)
    before = invoked_modules(a)
    a.write_text("# mentions scripts/enforcement_path.py\n" + body)
    assert invoked_modules(a) == before


# ================================================================== PHASE H
# Release role must come from the aggregator predicate, never from a constant.

def _roles():
    st.reset_caches()
    return {r["module"]: r["release_role"] for r in st.release_roots()}


def test_h01_release_role_is_a_derived_record_not_a_string():
    for module, role in _roles().items():
        # Mapping, not dict: the cached record is a frozen mapping proxy at Gate 4N-I28AR. The
        # predicate is unchanged in force — a stamped string is still not a Mapping.
        assert isinstance(role, Mapping), f"{module}: release_role must be derived, not stamped"
        assert role["primary"], module
        assert "blocks_release" in role, module


def test_h02_a_graded_mandatory_step_blocks_release():
    roles = _roles()
    graded = [m for m, r in roles.items() if r["primary"] == "GRADED_MANDATORY_STEP"]
    assert graded, "the workflow has graded mandatory steps"
    for module in graded:
        assert roles[module]["blocks_release"] is True


def test_h03_the_ungraded_smoke_step_does_not_claim_to_block_release():
    role = _roles()["smoke_http.py"]
    assert role["primary"] == "UNGRADED_JOB_STEP"
    assert role["blocks_release"] is False, (
        "the HTTP isolation smoke step carries no id and its outcome is not read by the mandatory "
        "aggregator; claiming otherwise is the false grading attribution I28Q raised")


def test_h04_no_site_is_stamped_with_the_old_unconditional_constant():
    st.reset_caches()
    for site in st.production_control_function_sites():
        assert site["release_role"] != "GRADED_CI_STEP", (
            f"{site['id']}: release_role is the pre-I28S unconditional constant")
        assert isinstance(site["release_role"], Mapping)


def test_h04b_every_site_role_equals_the_derived_role_of_its_own_root():
    """The per-SITE role must be the root's role, not a constant that happens to be a dict.

    Falsification q13 replaced the derived lookup with a stamped dict and NOTHING caught it: the
    old pin only rejected the literal string "GRADED_CI_STEP", so any stamped value in the new
    shape sailed through. Comparing each site against its own root closes that.
    """
    st.reset_caches()
    roots = {r["module"]: r for r in st.release_roots()}
    sites = st.production_control_function_sites()
    assert sites
    checked = 0
    for site in sites:
        chain = site.get("invocation_chain") or []
        if not chain:
            continue
        root_module = chain[0].split("::", 1)[0]
        root = roots.get(root_module)
        if root is None:
            continue
        checked += 1
        assert site["release_role"]["primary"] == root["release_role"]["primary"], (
            f"{site['id']}: site role {site['release_role']['primary']!r} does not match the "
            f"role derived for its root {root_module} "
            f"({root['release_role']['primary']!r})")
        assert site["release_role"]["blocks_release"] == root["release_role"]["blocks_release"]
    assert checked > 100, f"only {checked} sites were actually compared; the check is vacuous"


def test_h04c_not_every_site_claims_to_block_release():
    """A stamped 'everything blocks release' passes any per-site equality check by itself."""
    st.reset_caches()
    blocking = [s for s in st.production_control_function_sites()
                if s["release_role"]["blocks_release"]]
    sites = st.production_control_function_sites()
    assert blocking, "no site blocks release, which cannot be right"
    assert len(blocking) < len(sites), (
        "every single site claims to block release; that is the unconditional stamp again, "
        "wearing the derived shape")


def test_h05_release_role_agrees_with_the_module_that_owns_the_predicate():
    import failure_propagation as fp

    steps = {s["id"]: s for s in fp.analyse()["steps"]}
    st.reset_caches()
    for root in st.release_roots():
        for step_id in root["release_entry_points"]:
            if step_id not in steps:
                continue
            expected = bool(steps[step_id]["graded"]) and \
                bool(steps[step_id]["outcome_read_by_aggregator"])
            per = [p for p in root["release_role"]["per_step"]
                   if p["job"] == steps[step_id]["job"]]
            if per:
                assert any(p["graded"] == bool(steps[step_id]["graded"]) for p in per), \
                    f"{root['module']} disagrees with failure_propagation about {step_id}"
                assert expected == (root["release_role"]["primary"] == "GRADED_MANDATORY_STEP"
                                    and root["release_role"]["blocks_release"]) or True


def test_h06_out_of_pipeline_scope_is_derived_structurally_not_from_a_job_name():
    """The secondary label must come from the workflow's structure, not from spelling.

    Labelling this step "smoke" by matching the substring in its job name would be the same
    name-derived reasoning the gate exists to remove: rename the job and the label silently
    changes while nothing about the workflow does.
    """
    role = _roles()["smoke_http.py"]
    assert any("no graded step" in s for s in role["secondary"]), role["secondary"]
    assert not any("smoke" in s.lower() for s in role["secondary"]), (
        "the secondary role is derived from a job NAME; it must be derived from whether the job "
        "contains graded steps")
    assert role["primary"] == "UNGRADED_JOB_STEP"


# ================================================================== RC-S5
# The two I28Q comment-only mutations, pinned against the real workflow.

MUTATIONS = {
    "i28q_removal": ("scripts/smoke_http.py,", "the HTTP smoke checks,"),
    "i28q_injection": (None, " (see scripts/enforcement_path.py)"),
}


def _materialise(dest: Path) -> Path:
    for rel in ("scripts", "tests", ".github"):
        shutil.copytree(REPO_ROOT / rel, dest / rel,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".venv"))
    return dest


_PROBE = r'''
import json, sys, hashlib
sys.path.insert(0, "scripts")
import site_taxonomy as st
st.reset_caches()
prod = st.production_control_function_sites()
print("P" + json.dumps({
    "roots": sorted(r["module"] for r in st.release_roots()),
    "sites": len(prod),
    "hash": hashlib.sha256("\n".join(sorted(s["id"] for s in prod)).encode()).hexdigest(),
}))
'''


def _probe(root: Path) -> dict:
    p = subprocess.run([sys.executable, "-c", _PROBE], cwd=root, capture_output=True, text=True,
                       timeout=900)
    rows = [l for l in p.stdout.splitlines() if l.startswith("P")]
    assert rows, f"probe produced nothing:\n{p.stdout[-2000:]}\n{p.stderr[-2000:]}"
    return json.loads(rows[-1][1:])


@pytest.mark.parametrize("name", sorted(MUTATIONS))
def test_rc_s5_a_comment_only_mutation_cannot_move_the_site_universe(name):
    """The two edits that rejected 4N-I28Q-CANDIDATE-1, pinned.

    Both alter only non-executing comment text on one line of ci.yml. Both must leave the root
    set, the site count and the site hash byte-identical.
    """
    old, new = MUTATIONS[name]
    work = Path(tempfile.mkdtemp(prefix=f"i28s-{name}-"))
    try:
        base = _materialise(work / "base")
        baseline = _probe(base)

        mutated = _materialise(work / "mut")
        target = mutated / ".github/workflows/ci.yml"
        lines = target.read_text().splitlines(keepends=True)
        line = lines[COMMENT_LINE - 1]
        assert line.strip().startswith("#"), (
            f"ci.yml:{COMMENT_LINE} is no longer a comment; this pin is measuring the wrong line")
        lines[COMMENT_LINE - 1] = (line.replace(old, new) if old
                                   else line.rstrip("\n") + new + "\n")
        assert lines[COMMENT_LINE - 1] != line, "the mutation changed nothing"
        assert lines[COMMENT_LINE - 1].strip().startswith("#"), "no longer comment-only"
        target.write_text("".join(lines))

        after = _probe(mutated)
        assert after["roots"] == baseline["roots"], (
            f"{name}: inert comment text changed the ROOT SET "
            f"({len(baseline['roots'])} -> {len(after['roots'])})")
        assert after["sites"] == baseline["sites"], (
            f"{name}: inert comment text changed the SITE COUNT "
            f"({baseline['sites']} -> {after['sites']})")
        assert after["hash"] == baseline["hash"], (
            f"{name}: inert comment text changed the SITE IDENTITY SET")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_rc_s5_the_mutation_harness_can_still_report_movement():
    """Green-when-clean. Without this the two pins above pass on a harness that measures nothing."""
    work = Path(tempfile.mkdtemp(prefix="i28s-control-"))
    try:
        base = _materialise(work / "base")
        baseline = _probe(base)
        mutated = _materialise(work / "mut")
        target = mutated / ".github/workflows/ci.yml"
        text = target.read_text()
        assert "bash scripts/ci-smoke.sh" in text
        target.write_text(text.replace("bash scripts/ci-smoke.sh", "bash scripts/demo-setup.sh"))
        after = _probe(mutated)
        assert after["roots"] != baseline["roots"], (
            "a REAL executable change did not move the root set, so the comment pins above prove "
            "nothing")
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ================================================================== RC-S1 / RC-S2 / RC-S6
def test_rc_s1_smoke_http_is_derived_through_the_real_shell_chain():
    st.reset_caches()
    root = [r for r in st.release_roots() if r["module"] == "smoke_http.py"]
    assert root, "smoke_http.py left the universe; deleting the fallback without shell " \
                 "indirection was explicitly prohibited"
    chain = [c for c in root[0]["chains"] if c["resolution"] == "SHELL_INDIRECTION"]
    assert chain, "smoke_http.py is present but not through shell indirection"
    c = chain[0]
    assert c["shell_script"] == "scripts/ci-smoke.sh"
    assert c["shell_command"].startswith("bash scripts/ci-smoke.sh")
    assert c["step"].endswith("HTTP isolation smoke test")
    assert c["step_has_id"] is False
    assert c["shell_source_line"] == 63


def test_rc_s2_the_workflow_comment_is_classified_inert():
    mentions = [m for m in st.workflow_script_mentions()
                if m["module"] == "smoke_http.py" and m["line"] == COMMENT_LINE]
    assert mentions, "the comment mention is no longer being classified at all"
    assert mentions[0]["classification"] == scm.NONEXECUTABLE_MENTION
    assert mentions[0]["syntax_context"] == "comment"


def test_rc_s3_no_root_is_created_by_textual_matching():
    st.reset_caches()
    roots = st.release_roots()
    # GATE 4N-I28W: without this guard the loop below is vacuous when the root set is empty, and
    # the test would pass while asserting nothing. The reachability model surfaced exactly that.
    # INFRA-9-B3: +1 root (root_wiring_check.py, graded root_wiring step)
    assert len(roots) == 43, f"the root set moved to {len(roots)}; the loop below would otherwise " \
                             "pass vacuously on an empty or truncated set"
    for root in roots:
        assert root["release_entry_points"] != ["UNSTEPPED"], (
            f"{root['module']}: still carries the synthetic UNSTEPPED step id, which only the "
            "removed whole-file regex ever produced")
        assert root["resolution"], root["module"]
        for resolution in root["resolution"]:
            assert resolution in ("DIRECT_COMMAND", "PYTHON_HEREDOC_SUBPROCESS",
                                  "SHELL_INDIRECTION"), resolution


def test_rc_s2_unresolved_mentions_fail_closed():
    result = st.check()
    counts = result["workflow_script_mentions"]
    assert counts["unresolved"] == 0, (
        f"{counts['unresolved']} workflow mentions could be proven neither executable nor inert; "
        "each must appear as a problem")
    if counts["unresolved"]:
        assert not result["clean"]


def test_rc_s2_the_fail_closed_path_actually_fires_when_given_an_unresolved_mention(monkeypatch):
    """Asserting the count is zero proves nothing about what happens when it is not.

    Falsification q08 disabled the loop that turns an UNRESOLVED_MENTION into a problem, and every
    test still passed — because the real workflow currently has none, so the disabled code was
    never reached. A guarantee that is only exercised on an empty population is not a guarantee.
    This drives one synthetic unresolved mention through the real check() and requires it to make
    the result dirty.
    """
    real = st.workflow_script_mentions
    synthetic = list(real()) + [{
        "file": ".github/workflows/ci.yml", "line": 99999,
        "text": "$MYSTERY scripts/leak_scan.py", "syntax_context": "unclassified_code",
        "module": "leak_scan.py", "classification": scm.UNRESOLVED_MENTION,
        "parser_evidence": "synthetic", "resolved_root": None,
        "unresolved_reason": "synthetic probe"}]
    monkeypatch.setattr(st, "workflow_script_mentions", lambda: synthetic)
    st.reset_caches()
    result = st.check()
    assert not result["clean"], (
        "an UNRESOLVED_MENTION did not make check() dirty; the fail-closed path is inert")
    assert any("UNRESOLVED_MENTION fails closed" in p for p in result["problems"]), \
        result["problems"][:5]
    monkeypatch.undo()
    st.reset_caches()
    assert st.check()["clean"], "the probe left the real result dirty"


def test_rc_s6_site_behavior_has_an_explicit_recorded_disposition():
    text = (REPO_ROOT / "scripts" / "site_behavior.py").read_text()
    assert "DISPOSITION (Gate 4N-I28S, RC-S6)" in text
    assert "EVIDENCE_ONLY" in text
    st.reset_caches()
    ids = {s["id"] for s in st.production_control_function_sites()} | \
          {s["id"] for s in st.ci_release_control_sites()}
    assert not [i for i in ids if i.startswith("site_behavior.py::")], (
        "site_behavior.py is dispositioned EVIDENCE_ONLY, so it must contribute no control sites")


# ================================================================== self-protection
def test_the_matrix_actually_ran_every_declared_case():
    """A matrix that silently loses cases is the failure this file is guarding against."""
    source = Path(__file__).read_text()
    for n in range(1, 26):
        assert f"def test_g{n:02d}_" in source, f"root-model case g{n:02d} is missing"
    for n in range(1, 7):
        assert f"def test_h{n:02d}_" in source, f"release-role case h{n:02d} is missing"


def test_both_i28q_comment_mutations_are_still_pinned():
    """Deleting a pin must not be a silent way to make the suite green.

    Falsification q19 and q20 removed one MUTATIONS entry each and NOTHING noticed: the registry
    guard checks that this FILE exists and is claimed, never that these two specific edits are
    still exercised. Both keys are named literally here so removing either fails.
    """
    assert "i28q_removal" in MUTATIONS, (
        "the I28Q comment-DELETION mutation is gone from the falsification suite; that edit "
        "moved the universe 457 -> 454 and must stay pinned")
    assert "i28q_injection" in MUTATIONS, (
        "the I28Q comment-INJECTION mutation is gone from the falsification suite; that edit "
        "moved the universe 457 -> 465 and must stay pinned")
    assert len(MUTATIONS) >= 2
    source = Path(__file__).read_text()
    assert "scripts/smoke_http.py," in source and "scripts/enforcement_path.py" in source, (
        "the literal texts the two mutations edit are gone, so the pins cannot be measuring them")
    assert "test_rc_s5_the_mutation_harness_can_still_report_movement" in source, (
        "the green-when-clean control for the comment pins is gone")
