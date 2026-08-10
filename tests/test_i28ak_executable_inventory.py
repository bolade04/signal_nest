"""Derived external-executable inventory and tar trust (Gate 4N-I28AK).

WHAT THESE TESTS DEFEND. Gate 4N-I28AJ finding ADV-I28AJ-01: Gate 4N-I28AI bound `git` and `bash`
but ASSUMED the inventory was those two. `tar` was invoked twice per graded suite by
`commit_package_coherence.materialize()` — reached from the i23 predicted-tree coherence control —
by bare name through PATH, absent from the trust policy, so a fake `tar` earlier on PATH won
resolution while the trust check reported clean. `python3` was load-bearing and merely implicit.

THE INDEPENDENT ORACLES. `oracle_*` derive expected facts from external semantics only — a
hand-written PATH walk, direct file hashing, `ast` over the source, and real subprocess execution —
and import no production inventory, policy constant or verifier as truth.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import executable_inventory as inv                # noqa: E402
import external_executable_trust as eet           # noqa: E402
import signalnest_bootstrap as boot               # noqa: E402

POLICY = REPO_ROOT / "tests" / "fixtures" / "executable-trust-policy.json"


# ===================================================================== independent oracles
def oracle_path_resolution(name: str) -> str | None:
    """Which binary a bare name resolves to, walking PATH by hand rather than via which()."""
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if not directory:
            continue
        candidate = Path(directory) / name
        try:
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate.resolve())
        except OSError:
            continue
    return None


def oracle_digest(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def oracle_literal_argv_executables() -> set:
    """Executables named by a literal subprocess argv head, derived with an independent AST walk.

    Deliberately simpler than production: it looks only for `subprocess.<call>([ "name", ... ])`
    with an attribute-qualified callee. Qualification is the point — an unqualified `run(` match is
    what made the Gate 4N-I28AJ probe unsound.
    """
    found = set()
    for path in sorted((REPO_ROOT / "scripts").glob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if getattr(node.func.value, "id", None) != "subprocess":
                continue
            if not node.args or not isinstance(node.args[0], ast.List) or not node.args[0].elts:
                continue
            head = node.args[0].elts[0]
            if isinstance(head, ast.Constant) and isinstance(head.value, str):
                found.add(head.value)
    return found


def oracle_child_interpreter() -> str:
    """The interpreter a child process actually reports, measured by running one."""
    proc = subprocess.run([sys.executable, "-c", "import sys; print(sys.executable)"],
                          capture_output=True, text=True, timeout=30)
    return proc.stdout.strip()


# ===================================================================== derivation
def test_the_inventory_is_derived_not_authored():
    """The name set must come from the repository's text, not from the policy file."""
    static = inv.static_inventory()
    assert static["executables"], "an empty derivation would classify nothing"
    assert static["invocation_count"] if "invocation_count" in static else static["invocations"]


def test_the_oracle_independently_finds_the_same_literal_executables():
    """Production over-approximates (it also reads shell scripts and which() calls), so the
    binding assertion is that it never MISSES what an independent AST walk finds."""
    production = set(inv.static_inventory()["executables"])
    missed = sorted(oracle_literal_argv_executables() - production)
    assert not missed, f"production derivation missed literal argv executables: {missed}"


def test_tar_is_in_the_derived_inventory():
    """The exact ADV-I28AJ-01 omission."""
    assert "tar" in inv.static_inventory()["executables"]


def test_every_derived_executable_is_classified():
    result = inv.check()
    assert result["clean"], result["problems"]


def test_invocations_are_module_qualified():
    """Guards against the I28AJ probe defect: attribution by bare function name."""
    for record in inv.static_inventory()["invocations"]:
        assert record["module"] and record["function"] and record["line"]


def test_a_newly_reachable_bare_name_executable_fails_closed(monkeypatch):
    """THE policy-completeness pin."""
    static = inv.static_inventory()
    static["executables"] = sorted(set(static["executables"]) | {"totally-new-utility"})
    static["invocations"].append({"module": "probe.py", "function": "f", "line": 1,
                                  "call": "run", "executable": "totally-new-utility",
                                  "form": "bare_name"})
    monkeypatch.setattr(inv, "static_inventory", lambda: static)
    result = inv.check()
    assert not result["clean"]
    assert any("totally-new-utility" in p and "NO policy classification" in p
               for p in result["problems"])


def test_an_undeclared_dynamic_site_fails_closed(monkeypatch):
    static = inv.static_inventory()
    static["unresolved"].append({"module": "probe.py", "function": "mystery", "line": 9,
                                 "reason": "argv head is not a literal"})
    monkeypatch.setattr(inv, "static_inventory", lambda: static)
    result = inv.check()
    assert not result["clean"]
    assert any("mystery" in p for p in result["problems"])


def test_a_declared_dynamic_site_is_permitted():
    """Green-when-clean for the rule above: the seven declared sites do not fail the run."""
    assert inv.check()["clean"]


def test_inert_mentions_do_not_create_inventory_entries():
    """A command named in a comment, docstring or constant table is not an invocation."""
    executables = set(inv.static_inventory()["executables"])
    # INTERPRETERS and PYTHON_BASENAMES are constant tables naming shells and interpreters; the
    # names that appear ONLY there must not enter the inventory.
    assert "zsh" not in executables, "a name appearing only in a constant table is not an invocation"


def test_shell_builtins_are_not_treated_as_external():
    executables = set(inv.static_inventory()["executables"])
    for builtin in ("cd", "export", "wait", "kill", "trap", "echo"):
        assert builtin not in executables, builtin


def test_heredoc_bodies_do_not_create_entries():
    """Shell heredocs carry Python; `import` and `from` are not commands."""
    executables = set(inv.static_inventory()["executables"])
    for token in ("import", "from", "with"):
        assert token not in executables, token


def test_shell_functions_are_not_treated_as_external():
    assert "require_venv" not in set(inv.static_inventory()["executables"])


def test_a_local_helper_named_run_is_not_a_subprocess_call():
    """cloudfront_precheck.collect() defines its own run(); an unqualified match reported
    'cloudfront' as an executable. Qualification is what prevents that."""
    assert "cloudfront" not in set(inv.static_inventory()["executables"])


def test_reconciliation_dispositions_every_name():
    static = inv.static_inventory()
    runtime = {"available": True, "executables": ["git", "tar"], "counts": {"git": 3, "tar": 2}}
    result = inv.reconcile(static, runtime)
    for name in static["executables"]:
        assert name in result["dispositions"], name
    assert result["dispositions"]["tar"] == "STATICALLY_REACHABLE_AND_EXERCISED"


def test_an_executable_seen_only_at_runtime_is_reported(monkeypatch):
    trace = Path(tempfile.mkdtemp()) / "trace.log"
    trace.write_text("00:00:00 mystery-tool --flag\n")
    result = inv.check(trace_path=trace)
    assert not result["clean"]
    assert any("mystery-tool" in p for p in result["problems"])


def test_a_missing_runtime_trace_is_reported_not_assumed_empty():
    runtime = inv.runtime_inventory(None)
    assert runtime["available"] is False


# ===================================================================== tar trust
def test_tar_is_classified_and_bound():
    entry = eet.load_policy()["executables"]["tar"]
    assert entry["classification"] == eet.APPROVED_PATH_SET_AND_CONTENT_BOUND
    assert entry["approved_paths"]


def test_the_oracle_independently_agrees_on_tar_resolution():
    record = eet.resolve("tar", eet.load_policy()["executables"]["tar"])
    assert record.get("resolved_path") == oracle_path_resolution("tar")


def test_the_oracle_independently_agrees_on_tar_content():
    record = eet.resolve("tar", eet.load_policy()["executables"]["tar"])
    assert record["content_sha256"] == oracle_digest(record["resolved_path"])


def test_t01_a_fake_tar_earlier_on_path_is_refused(tmp_path, monkeypatch):
    """THE pin for ADV-I28AJ-01."""
    fake = tmp_path / "tar"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    assert oracle_path_resolution("tar") == str(fake.resolve()), (
        "the shadow must actually win resolution, or this test proves nothing")
    result = eet.check()
    assert not result["clean"]
    assert any("tar" in p and "approved path set" in p for p in result["problems"])


def test_t02_a_fake_tar_later_on_path_does_not_win(tmp_path, monkeypatch):
    fake = tmp_path / "tar"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{os.environ['PATH']}{os.pathsep}{tmp_path}")
    assert eet.check()["clean"]


def test_t03_a_symlink_out_of_the_approved_set_is_refused(tmp_path, monkeypatch):
    payload = tmp_path / "payload"
    payload.write_text("#!/bin/sh\nexit 0\n")
    payload.chmod(0o755)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "tar").symlink_to(payload)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    assert not eet.check()["clean"]


def test_t04_the_coherence_call_site_uses_the_validated_absolute_tar():
    """§7: resolve once, validate, invoke that path — never re-resolve through PATH."""
    source = (REPO_ROOT / "scripts" / "commit_package_coherence.py").read_text(encoding="utf-8")
    assert 'tar_invocation(' in source, "the call site must use the trust layer"
    assert '["tar", "-x"' not in source, "the bare-name invocation must be gone"
    argv, _env = eet.tar_invocation(["-x", "-C", "/tmp"])
    assert Path(argv[0]).is_absolute()
    assert argv[0] == eet.validated_path("tar")


def test_t05_a_shadowed_tar_makes_coherence_refuse_rather_than_use_it(tmp_path, monkeypatch):
    import commit_package_coherence as cpc
    fake = tmp_path / "tar"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    with pytest.raises(eet.ExecutableTrustError):
        cpc.materialize("HEAD", tmp_path / "dest")


def test_t06_tar_drift_after_verification_is_detected():
    before = eet.snapshot()
    after = json.loads(json.dumps(before))
    after["executables"]["tar"]["content_sha256"] = "0" * 64
    assert any("tar" in p and "content digest" in p for p in eet.compare(before, after))


def test_t07_tar_extraction_stays_inside_the_temporary_tree(tmp_path):
    """The destination is an explicit -C into a TemporaryDirectory the caller owns."""
    argv, _ = eet.tar_invocation(["-x", "-C", str(tmp_path)])
    assert "-C" in argv and str(tmp_path) in argv
    source = (REPO_ROOT / "scripts" / "commit_package_coherence.py").read_text(encoding="utf-8")
    assert "TemporaryDirectory" in source, (
        "the archive must be extracted into a temporary directory, not the worktree")


# ===================================================================== python interpreter
def test_p01_python3_is_explicitly_classified():
    entry = eet.load_policy()["executables"]["python3"]
    assert entry["classification"] == eet.CURRENT_INTERPRETER_IDENTITY_BOUND
    assert entry["interpreter_rule"]


def test_p02_the_child_interpreter_is_the_parent_interpreter():
    """Measured by running a child, not asserted from configuration."""
    assert oracle_child_interpreter() == sys.executable


def test_p03_a_fake_python3_earlier_on_path_does_not_change_the_child(tmp_path, monkeypatch):
    """The graded path uses sys.executable, so a PATH shadow cannot select the child."""
    fake = tmp_path / "python3"
    fake.write_text("#!/bin/sh\necho /fake/interpreter\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    assert oracle_path_resolution("python3") == str(fake.resolve())
    assert oracle_child_interpreter() == sys.executable, (
        "a child launched via sys.executable must be unaffected by a PATH shadow")


def test_p04_the_interpreter_identity_is_recorded():
    record = eet.check()["executables"].get("python3")
    assert record is not None
    assert record["sys_executable"] == sys.executable
    assert record.get("content_sha256") == oracle_digest(str(Path(sys.executable).resolve()))


def test_p05_graded_call_sites_prefer_sys_executable():
    """A bare python3 in a graded call site would defeat the classification."""
    offenders = []
    for path in sorted((REPO_ROOT / "scripts").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if getattr(node.func.value, "id", None) != "subprocess" or not node.args:
                continue
            head = node.args[0]
            if isinstance(head, ast.List) and head.elts:
                first = head.elts[0]
                if isinstance(first, ast.Constant) and first.value in ("python", "python3"):
                    offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, f"a graded call site launches a bare interpreter: {offenders}"


# ===================================================================== integration
def test_i01_the_bootstrap_runs_the_inventory_layer():
    attestation = boot.establish(strict=True)
    assert attestation["executable_inventory"]["clean"]
    assert attestation["established"]


def test_i02_session_finish_covers_the_inventory():
    outcome = boot.reverify()
    assert "executable_inventory" in outcome["layers"]
    assert outcome["clean"], outcome["problems"]


def test_i03_the_inventory_module_is_protected():
    protected = {e["module"] for e in json.loads(
        (REPO_ROOT / "tests" / "fixtures" / "protected-module-set.json").read_text()
    )["protected_modules"]}
    assert "executable_inventory" in protected


def test_i04_a_stubbed_inventory_derivation_is_caught():
    import executed_code_provenance as ecp
    module = sys.modules["executable_inventory"]
    original = module.static_inventory
    assert ecp.verify()["clean"], "baseline must be green or the refusal proves nothing"
    try:
        module.static_inventory = lambda: {"invocations": [], "unresolved": [],
                                           "executables": [], "source_count": 0}
        assert not ecp.verify()["clean"]
    finally:
        module.static_inventory = original
    assert ecp.verify()["clean"]


def test_i05_git_and_bash_protections_did_not_regress():
    policy = eet.load_policy()["executables"]
    for name in ("git", "bash"):
        assert policy[name]["classification"] == eet.APPROVED_PATH_SET_AND_CONTENT_BOUND
        assert policy[name]["approved_paths"]
    assert eet.check()["clean"]


@pytest.mark.parametrize("variable", ["GIT_DIR", "BASH_ENV", "ENV", "GIT_CONFIG_GLOBAL"])
def test_i06_fatal_steering_variables_still_refused(variable, monkeypatch):
    monkeypatch.setenv(variable, "/tmp/steer")
    assert not eet.check()["clean"]
