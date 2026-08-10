"""Shell command positions and function-local import binding (Gate 4N-I28AO).

WHAT THESE DEFEND. Gate 4N-I28AN found two blockers.

ADV-I28AN-01: the shell deriver read `stripped.split()[0]` — the first token of each line — so a
command after an assignment, inside `$( )`, after `|`, `&&`, `||` or `;`, or in any compound
construct was invisible. `docker`, `seq`, `grep`, `tee`, `mktemp` and `dirname` were invoked by
tracked shell and absent from the trust policy while both checks reported clean. `.github/workflows/
ci.yml` was outside the scanned universe entirely, and its run: blocks invoke `docker` twelve times.

ADV-I28AN-02: `external_executable_trust`'s npm precondition reads `site_taxonomy.release_roots()`
through an import INSIDE the function. The name walk collects module-level globals, so nothing
pinned it; replacing `release_roots` with `lambda: []` disarmed the precondition with executed-code,
executed-state and the trust layer all clean.

THE ORACLE RULE. The corpus below states an EXPECTED executable set per case, written from shell
semantics rather than from what the parser happens to return. A case with no expectation is not
evidence.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import executable_inventory as inv                # noqa: E402
import executed_code_provenance as ecp            # noqa: E402
import executed_state_provenance as esp           # noqa: E402
import external_executable_trust as eet           # noqa: E402
import shell_positions as sp                      # noqa: E402
import signalnest_bootstrap as boot               # noqa: E402
import site_taxonomy as st                        # noqa: E402

POLICY = REPO_ROOT / "tests" / "fixtures" / "executable-trust-policy.json"
PROTECTED_SET = REPO_ROOT / "tests" / "fixtures" / "protected-module-set.json"
CONTRACT = REPO_ROOT / "tests" / "fixtures" / "executed-state-contract.json"

# ===================================================================== the adversarial corpus
# (label, shell source, expected NON-BUILTIN executables)
CORPUS = [
    ("simple",                      "docker run --rm x",                        {"docker"}),
    ("substitution in assignment",  'uid="$(docker run --rm x)"',               {"docker"}),
    ("VAR=value cmd",               "VAR=value docker run x",                   {"docker"}),
    ("two assignments",             "A=1 B=2 docker run x",                     {"docker"}),
    ("command wrapper",             "command docker run x",                     {"docker"}),
    ("env wrapper",                 "env VAR=value docker run x",               {"env", "docker"}),
    ("sudo wrapper",                "sudo docker run x",                        {"sudo", "docker"}),
    ("pipeline",                    "docker run x | grep pattern",              {"docker", "grep"}),
    ("pipeline to xargs",           "seq 1 10 | xargs echo",                    {"seq", "xargs"}),
    ("AND list",                    "true && docker run x",                     {"docker"}),
    ("OR list",                     "false || docker run x",                    {"docker"}),
    ("semicolon list",              "cd /tmp ; docker run x",                   {"docker"}),
    ("subshell",                    "(docker run x)",                           {"docker"}),
    ("group",                       "{ docker run x; }",                        {"docker"}),
    ("if condition",                "if docker inspect y; then :; fi",          {"docker"}),
    ("while condition",             "while docker ps; do :; done",              {"docker"}),
    ("for body",                    "for x in a b; do docker inspect $x; done", {"docker"}),
    ("case branch",                 "case $v in a) docker run x ;; esac",       {"docker"}),
    ("output capture",              "output=$(docker run x)",                   {"docker"}),
    ("process substitution in",     "diff <(docker run x) f",                   {"diff", "docker"}),
    ("process substitution out",    "cmd > >(tee file)",                        {"cmd", "tee"}),
    ("trap body",                   "trap 'docker rm c' EXIT",                  {"docker"}),
    ("function body",               "f() { docker run x; }",                    {"docker"}),
    ("heredoc opener",              "python3 - <<EOF\nprint(1)\nEOF",           {"python3"}),
    ("heredoc body is inert",       "cat <<EOF\ndocker run x\nEOF",             {"cat"}),
    ("quoted string is inert",      'echo "docker run x"',                      set()),
    ("comment is inert",            "# docker run x",                           set()),
    ("echo argument is inert",      "echo docker run",                          set()),
    ("printf argument is inert",    "printf '%s' 'docker run'",                 set()),
    ("assignment text is inert",    "cmd='docker run x'",                       set()),
    ("eval fails closed",           'eval "$cmd"',                              set()),
    ("bash -c nested",              "bash -c 'docker run x'",                   {"bash", "docker"}),
    ("sh -c nested",                "sh -c 'docker run x'",                     {"sh", "docker"}),
    ("dirname in a path",           '"$(dirname "$0")/helper.sh"',              {"dirname"}),
    ("mktemp in assignment",        'D="$(mktemp -d)"',                         {"mktemp"}),
    ("grep",                        "grep -q ok file",                          {"grep"}),
    ("tee after a redirection",     "false 2>&1 | tee log",                     {"tee"}),
    ("seq in a for list",           "for _ in $(seq 1 60); do :; done",         {"seq"}),
    ("dirname nested twice",        'R="$(cd "$(dirname "$0")" && pwd)"',       {"dirname"}),
    ("find",                        "find /app -name '*.env' -print",           {"find"}),
    ("arithmetic is not a command", "failures=$((failures + 1))",               set()),
]


@pytest.mark.parametrize("label,source,expected", CORPUS, ids=[c[0] for c in CORPUS])
def test_s01_command_positions_match_the_expected_set(label, source, expected):
    result = sp.scan_script(source, origin=label)
    actual = result.executables(sp.local_functions(source))
    assert actual == {e for e in expected if e not in sp.SHELL_BUILTINS}, (
        f"{label}: expected {sorted(expected)}, derived {sorted(actual)} from: {source!r}")


def test_s02_the_first_token_rule_would_have_missed_these():
    """The regression this gate exists to prevent, stated as a property rather than as prose."""
    missed = ['uid="$(docker run x)"', "false 2>&1 | tee log", "true && grep -q x f",
              'D="$(mktemp -d)"', "cd /tmp ; seq 1 3"]
    for source in missed:
        first_token = source.strip().split()[0].strip("(){};")
        derived = sp.scan(source).executables()
        assert derived, f"{source!r} must yield a command"
        assert derived != {first_token}, (
            f"{source!r}: the derivation must see past the first token {first_token!r}")


def test_s03_unsupported_constructs_fail_closed_rather_than_being_skipped():
    for source, fragment in [('eval "$cmd"', "computed operand"),
                             ('"${CMD[@]}" x', "variable expansion"),
                             ("$tool --run", "variable expansion")]:
        result = sp.scan(source)
        assert result.unresolved, f"{source!r} must be reported, not skipped"
        assert any(fragment in c.reason for c in result.unresolved), (
            f"{source!r}: {[c.reason for c in result.unresolved]}")


def test_s04_the_grammar_claim_is_enumerable_and_bounded():
    supported, unsupported = sp.supported_forms(), sp.unsupported_forms()
    assert len(supported) >= 20 and len(unsupported) >= 5
    assert not set(supported) & set(unsupported), "a form cannot be both supported and not"
    assert "eval with a computed string" in unsupported, (
        "the model must not claim to resolve eval")


# ===================================================================== workflow run blocks
def test_w01_workflow_run_blocks_are_in_scope():
    blocks = inv.workflow_run_blocks()
    assert blocks, "the workflow must be adjudicated, not excluded"
    assert any(b["workflow"] == "ci.yml" for b in blocks)
    for b in blocks:
        assert "run" in b and "job" in b and "step" in b


def test_w02_docker_is_discovered_from_the_workflow_and_from_the_script():
    static = inv.static_inventory()
    sites = {i["module"] for i in static["invocations"] if i.get("executable") == "docker"}
    assert any(s.startswith("ci.yml") for s in sites), f"workflow sites missing: {sorted(sites)}"
    assert "docker-security-check.sh" in sites, (
        "the shell script Gate 4N-I28AN named must be covered too")


def test_w03_every_derived_executable_carries_a_policy_disposition():
    static = inv.static_inventory()
    policy = json.loads(POLICY.read_text(encoding="utf-8"))["executables"]
    missing = sorted(set(static["executables"]) - set(policy))
    assert not missing, f"derived but unclassified: {missing}"


def test_w04_the_six_executables_i28an_named_are_all_bound():
    policy = json.loads(POLICY.read_text(encoding="utf-8"))["executables"]
    for name in ("docker", "seq", "grep", "tee", "mktemp", "dirname"):
        assert name in policy, f"{name} is still absent from the policy"
        assert policy[name]["classification"] == "APPROVED_PATH_SET_AND_CONTENT_BOUND"
        assert policy[name].get("bound_before_execution") is True


@pytest.mark.parametrize("name", ["seq", "grep", "tee", "mktemp", "dirname"])
def test_w05_a_shadow_of_each_newly_bound_executable_is_refused(name, tmp_path):
    """Each shim alone in its own directory: two shims in one directory refuse for the wrong one."""
    assert eet.check()["clean"], "baseline must be green or the refusal proves nothing"
    shim = tmp_path / f"shim-{name}"
    shim.mkdir()
    (shim / name).write_text("#!/bin/sh\nexit 0\n")
    (shim / name).chmod(0o755)
    env = dict(os.environ, PATH=f"{shim}{os.pathsep}{os.environ['PATH']}",
               PYTHONPATH=str(REPO_ROOT / "scripts"))
    proc = subprocess.run(
        [sys.executable, "-c",
         "import json,sys;sys.path.insert(0,'scripts');import external_executable_trust as e;"
         "r=e.check();print(json.dumps({'clean':r['clean'],'problems':r['problems'][:1]}))"],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, proc.stderr[-400:]
    result = json.loads(proc.stdout)
    assert not result["clean"], f"a fake {name} earlier on PATH was not refused"
    assert name in result["problems"][0]


def test_w06_docker_is_bound_whenever_it_resolves():
    entry = json.loads(POLICY.read_text(encoding="utf-8"))["executables"]["docker"]
    assert entry["approved_paths"], "docker must name an approved path set"
    assert entry.get("required_present") is False, (
        "docker is absent on some development hosts and present on the runner; the entry must say "
        "so explicitly rather than making the suite refuse where docker is simply not installed")
    resolved = shutil.which("docker")
    if resolved is None:
        assert eet.check()["clean"], "absence must not refuse when required_present is False"


def test_w07_an_undeclared_shell_site_and_a_stale_declaration_both_fail():
    """Two-way, measured against a real derivation rather than asserted from the fixture."""
    static = inv.static_inventory()
    observed = {f"{s['module']}::{s['line']}::"
                f"{str(s.get('reason', '')).rsplit('(', 1)[-1].rstrip(')')}"
                for s in static["unresolved"] if s.get("function") == "<shell>"}
    declared = {f"{d['module']}::{d['line']}::{d['word']}"
                for d in json.loads(POLICY.read_text(encoding="utf-8"))["dynamic_shell_sites"]}
    assert observed, "the derivation must report the constructs it cannot resolve"
    assert observed == declared, (
        f"undeclared: {sorted(observed - declared)[:3]}; stale: {sorted(declared - observed)[:3]}")
    assert inv.check()["clean"], "with every site declared the check must pass"


def test_w08_every_declared_shell_site_states_what_bounds_it():
    doc = json.loads(POLICY.read_text(encoding="utf-8"))
    sites = doc.get("dynamic_shell_sites") or []
    assert sites, "unresolved command positions must be declared, not silently accepted"
    undeclared = [s for s in sites if str(s.get("why_bounded", "")).startswith("UNDECLARED")]
    assert not undeclared, f"{len(undeclared)} sites carry no adjudication"


# ===================================================================== function-local imports
def test_l01_the_npm_precondition_dependency_is_derived_not_authored():
    imports, problems = esp.local_imports(eet, ["check"])
    assert not problems, problems
    assert "site_taxonomy" in imports, (
        "the precondition imports site_taxonomy inside the function; the walk must follow it")
    assert "release_roots" in imports["site_taxonomy"]


def test_l02_the_local_import_is_pinned_by_file_content_and_by_callable_identity():
    tokens, problems = esp.local_import_tokens(eet, ["check"])
    assert not problems, problems
    assert tokens["LOCALIMPORT:site_taxonomy"].startswith("FILE:scripts/site_taxonomy.py:")
    assert tokens["LOCALCALLABLE:site_taxonomy.release_roots"].startswith("CALLABLE:")


def test_l03_the_contract_carries_those_tokens():
    names = json.loads(CONTRACT.read_text(encoding="utf-8"))["modules"][
        "external_executable_trust"]["names"]
    assert "LOCALIMPORT:site_taxonomy" in names
    assert "LOCALCALLABLE:site_taxonomy.release_roots" in names


def test_l04_an_unresolvable_local_import_fails_closed():
    module = types.ModuleType("probe_module")
    module.__file__ = str(REPO_ROOT / "scripts" / "probe_module.py")
    source = "def f():\n    import a_module_that_does_not_exist\n    return 1\n"
    namespace = {}
    exec(compile(source, module.__file__, "exec"), namespace)
    module.f = namespace["f"]
    _tokens, problems = esp.local_import_tokens(module, ["f"])
    assert problems and "cannot be resolved" in problems[0], problems


# ===================================================================== site_taxonomy protection
def test_p01_site_taxonomy_is_a_protected_module():
    modules = {e["module"] for e in
               json.loads(PROTECTED_SET.read_text(encoding="utf-8"))["protected_modules"]}
    assert "site_taxonomy" in modules


def test_p02_release_roots_is_a_critical_callable():
    entry = next(e for e in json.loads(PROTECTED_SET.read_text(encoding="utf-8"))["protected_modules"]
                 if e["module"] == "site_taxonomy")
    assert "release_roots" in entry["critical_callables"]


def _layers():
    entries = boot._protected_entries()
    return {"code": ecp.verify({"protected_modules": entries})["clean"],
            "state": esp.verify()["clean"]}


def test_p03_stubbing_release_roots_is_detected():
    """THE ADV-I28AN-02 EXPLOIT, now refused. Baseline green first."""
    assert all(_layers().values()), "baseline must be green or the refusal proves nothing"
    original = st.release_roots
    try:
        st.release_roots = lambda: []
        after = _layers()
        assert not all(after.values()), "release_roots -> [] was not detected"
        assert any("release_roots" in p for p in esp.verify()["problems"]), (
            "the refusal must NAME release_roots")
    finally:
        st.release_roots = original
    assert all(_layers().values()), "the tree must be restored"


def test_p04_replacing_the_site_taxonomy_module_is_detected():
    assert all(_layers().values()), "baseline must be green or the refusal proves nothing"
    real = sys.modules["site_taxonomy"]
    try:
        decoy = types.ModuleType("site_taxonomy")
        decoy.__file__ = real.__file__
        decoy.release_roots = lambda: []
        decoy.production_control_function_sites = lambda: []
        decoy.ci_release_control_sites = lambda: []
        sys.modules["site_taxonomy"] = decoy
        assert not all(_layers().values()), "sys.modules substitution was not detected"
    finally:
        sys.modules["site_taxonomy"] = real
    assert all(_layers().values()), "the tree must be restored"


def test_p05_the_memoisation_caches_are_declared_volatile_and_start_empty():
    """A cache is derived state; its CONTENT cannot be pinned, so emptiness at import is the pin."""
    assert esp.VOLATILE_CACHES["site_taxonomy"] == ("_DERIVED", "_INDEX")
    proc = subprocess.run(
        [sys.executable, "-c",
         "import sys;sys.path.insert(0,'scripts');import site_taxonomy as s;"
         "print(len(s._DERIVED), len(s._INDEX))"],
        cwd=REPO_ROOT, env=dict(os.environ, PYTHONPATH=str(REPO_ROOT / "scripts")),
        capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr[-300:]
    assert proc.stdout.split() == ["0", "0"], (
        "a memoisation cache pre-populated at import would carry attacker-chosen derivations that "
        "the VOLATILE_CACHE token cannot see; it must be empty in a fresh interpreter")


def test_p06_the_precondition_still_refuses_a_genuine_reachability_change():
    """The mechanism is live, not merely present."""
    doc = eet.load_policy()
    root = sorted({r["module"] for r in st.release_roots()})[0]
    doc["executables"]["probe-tool"] = {"classification": eet.UNREACHABLE_FROM_GRADED_ROOTS,
                                        "call_site_modules": [root]}
    original = inv.static_inventory
    try:
        inv.static_inventory = lambda: {
            "invocations": [{"module": root, "function": "f", "line": 1, "call": "run",
                             "executable": "probe-tool", "form": "bare_name"}],
            "unresolved": [], "foreign": [], "executables": ["probe-tool"], "source_count": 1}
        result = eet.check(doc)
        assert not result["clean"]
        assert any("release command root" in p for p in result["problems"])
    finally:
        inv.static_inventory = original
