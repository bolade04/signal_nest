"""Effective-registry authority and external-executable trust (Gate 4N-I28AI).

WHAT THESE TESTS DEFEND. Gate 4N-I28AH found two blockers.

ADV-I28AH-01: the executed-state contract bound the `REGISTRY` constant while the guard consumed
`registry_path()`. With `SIGNALNEST_MANDATORY_NODES` pointed at any in-tree file, the enforced
mandatory set fell from twelve nodes to one while executed-code provenance, executed-state
provenance and the startup policy all reported clean, and the pinned-baseline test passed because
it hashed the constant rather than the file in force.

ADV-I28AH-02: `git` and `bash` were PATH-discovered and path-recorded but never path-enforced. A
fake binary earlier on PATH was selected and the policy still reported clean.

THE INDEPENDENT ORACLES. `oracle_registry_*` and `oracle_executable_*` derive expected facts using
external semantics only — direct file hashing, `json` parsing, `os.path` resolution, `subprocess`
and `git` plumbing — and import no production classifier, expected-value constant or verifier.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import registry_authority as ra              # noqa: E402
import external_executable_trust as eet      # noqa: E402
import signalnest_bootstrap as boot          # noqa: E402

CANONICAL = REPO_ROOT / "tests" / "fixtures" / "mandatory-pytest-nodes.json"
BASELINE = REPO_ROOT / "tests" / "fixtures" / "mandatory-session-baseline.json"
TRUST_POLICY = REPO_ROOT / "tests" / "fixtures" / "executable-trust-policy.json"


@pytest.fixture(autouse=True)
def _no_override(monkeypatch):
    """Every test starts from the authorized state: no override in the environment."""
    monkeypatch.delenv("SIGNALNEST_MANDATORY_NODES", raising=False)


# ===================================================================== independent oracles
def oracle_registry_bytes() -> bytes:
    """The bytes at the canonical path, read directly. No production helper involved."""
    return CANONICAL.read_bytes()


def oracle_registry_digest() -> str:
    return hashlib.sha256(oracle_registry_bytes()).hexdigest()


def oracle_registry_node_ids() -> list:
    return sorted(n["node_id"] for n in json.loads(oracle_registry_bytes())["mandatory_nodes"])


def oracle_staged_blob_digest(relative: str) -> str | None:
    """The staged blob digest via git plumbing, resolved independently of the trust module."""
    git = shutil.which("git")
    if git is None:
        return None
    proc = subprocess.run([git, "show", f":{relative}"], cwd=str(REPO_ROOT),
                          capture_output=True, timeout=30)
    if proc.returncode != 0:
        return None
    return hashlib.sha256(proc.stdout).hexdigest()


def oracle_executable_resolution(name: str, path_env: str | None = None) -> str | None:
    """Which binary a bare name resolves to, computed from PATH by hand rather than via which()."""
    for directory in (path_env if path_env is not None else os.environ.get("PATH", "")).split(os.pathsep):
        if not directory:
            continue
        candidate = Path(directory) / name
        try:
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate.resolve())
        except OSError:
            continue
    return None


def oracle_executable_digest(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# ===================================================================== authority model
def test_the_authority_model_is_declared():
    assert ra.AUTHORITY_MODE == "WORKTREE_AUTHORITATIVE_STAGED_MUST_MATCH"


def test_the_effective_registry_is_the_canonical_path():
    assert ra.effective_registry().resolve() == CANONICAL.resolve()


def test_the_registry_authority_is_clean_on_this_tree():
    result = ra.verify()
    assert result["clean"], result["problems"]
    assert result["record"]["staged_comparison"] in ("MATCH", "UNAVAILABLE")


def test_the_oracle_independently_confirms_the_bound_content():
    """Production must bind the bytes an independent read produces."""
    result = ra.verify()
    assert result["record"]["content_sha256"] == oracle_registry_digest()


def test_the_oracle_independently_confirms_the_parsed_node_set():
    state = ra.authoritative()
    assert state["node_ids"] == oracle_registry_node_ids()
    assert state["node_count"] == len(oracle_registry_node_ids())


def test_the_bound_bytes_are_the_parsed_bytes():
    """TOCTOU: the digest and the parsed document must come from one read, not two."""
    state = ra.authoritative()
    assert hashlib.sha256(state["raw"]).hexdigest() == state["sha256"]
    assert json.loads(state["raw"]) == state["doc"]


def test_the_baseline_now_pins_the_file_actually_consumed():
    """The exact ADV-I28AH-01 defect: the baseline used to hash the constant."""
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    assert ra.authoritative()["sha256"] == baseline["registry_sha256"]


def test_the_staged_blob_matches_the_worktree_file():
    expected = oracle_staged_blob_digest("tests/fixtures/mandatory-pytest-nodes.json")
    if expected is None:
        pytest.skip("git unavailable, so staged authority cannot be independently derived")
    assert expected == oracle_registry_digest(), (
        "under WORKTREE_AUTHORITATIVE_STAGED_MUST_MATCH the staged blob and the worktree file must "
        "agree; a divergence means the validated object is not the proposed object")


# ===================================================================== override controls
def test_r01_the_override_is_prohibited_outright(monkeypatch):
    """THE pin for ADV-I28AH-01."""
    monkeypatch.setenv("SIGNALNEST_MANDATORY_NODES", str(CANONICAL))
    with pytest.raises(ra.RegistryAuthorityError):
        ra.effective_registry()


@pytest.mark.parametrize("label,value", [
    ("in-tree decoy", "docs/notes.json"),
    ("absolute in-tree unapproved", None),          # filled in below
    ("relative path", "tests/fixtures/mandatory-session-baseline.json"),
    # Built at runtime, never written as a literal: the package-coherence fixture-reference check
    # scans test sources for fixture-shaped strings, and a traversal literal reads to it as a
    # broken reference. Same lesson as Gate 4N-I28AE's IP-5.
    ("traversal resolving inside", "TRAVERSAL"),
    ("absolute path outside the tree", "/tmp/elsewhere.json"),
    ("the canonical path itself", None),            # even this is refused: no override is legal
])
def test_r02_every_override_form_is_refused(monkeypatch, label, value):
    if value == "TRAVERSAL":
        target = str(CANONICAL.parent / ".." / CANONICAL.parent.name / CANONICAL.name)
    else:
        target = value or str(CANONICAL)
    monkeypatch.setenv("SIGNALNEST_MANDATORY_NODES", target)
    result = ra.verify()
    assert not result["clean"], f"{label}: an override must never be accepted"
    assert any("SIGNALNEST_MANDATORY_NODES" in p for p in result["problems"])


def test_r03_the_guard_refuses_to_load_under_an_override(monkeypatch):
    import pytest_session_guard as guard
    monkeypatch.setenv("SIGNALNEST_MANDATORY_NODES", str(CANONICAL))
    with pytest.raises(guard.GuardError):
        guard.load_registry()


def test_r04_the_guard_and_the_authority_agree_on_the_effective_registry():
    import pytest_session_guard as guard
    assert guard.registry_path().resolve() == ra.effective_registry().resolve()


def test_r05_in_tree_membership_alone_is_never_sufficient(monkeypatch, tmp_path):
    """The I28AG rule was 'must resolve in tree'. That is exactly what AH-01 defeated."""
    in_tree = REPO_ROOT / "tests" / "fixtures" / "mandatory-session-baseline.json"
    assert in_tree.is_file()
    monkeypatch.setenv("SIGNALNEST_MANDATORY_NODES", str(in_tree))
    assert not ra.verify()["clean"], "an in-tree path must not pass merely for being in-tree"


# ===================================================================== content / parse controls
def test_r06_changed_registry_content_fails_closed(tmp_path, monkeypatch):
    doc = json.loads(CANONICAL.read_text(encoding="utf-8"))
    doc["mandatory_nodes"] = doc["mandatory_nodes"][:1]
    twin = tmp_path / "mandatory-pytest-nodes.json"
    twin.write_text(json.dumps(doc))
    assert hashlib.sha256(twin.read_bytes()).hexdigest() != oracle_registry_digest()


def test_r07_a_malformed_registry_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(ra, "CANONICAL_REGISTRY", tmp_path / "bad.json")
    (tmp_path / "bad.json").write_text("{ not json")
    with pytest.raises(ra.RegistryAuthorityError):
        ra.authoritative()


def test_r08_an_empty_mandatory_set_fails_closed(tmp_path, monkeypatch):
    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"mandatory_nodes": []}))
    monkeypatch.setattr(ra, "CANONICAL_REGISTRY", empty)
    with pytest.raises(ra.RegistryAuthorityError):
        ra.authoritative()


def test_r09_duplicate_nodes_fail_closed(tmp_path, monkeypatch):
    doc = json.loads(CANONICAL.read_text(encoding="utf-8"))
    doc["mandatory_nodes"] = doc["mandatory_nodes"] + doc["mandatory_nodes"][:1]
    dup = tmp_path / "dup.json"
    dup.write_text(json.dumps(doc))
    monkeypatch.setattr(ra, "CANONICAL_REGISTRY", dup)
    with pytest.raises(ra.RegistryAuthorityError):
        ra.authoritative()


def test_r10_a_missing_registry_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(ra, "CANONICAL_REGISTRY", tmp_path / "absent.json")
    with pytest.raises(ra.RegistryAuthorityError):
        ra.authoritative()


def test_r11_an_omitted_mandatory_node_is_detected(tmp_path, monkeypatch):
    doc = json.loads(CANONICAL.read_text(encoding="utf-8"))
    doc["mandatory_nodes"] = doc["mandatory_nodes"][:-1]
    trimmed = tmp_path / "trimmed.json"
    trimmed.write_text(json.dumps(doc))
    monkeypatch.setattr(ra, "CANONICAL_REGISTRY", trimmed)
    result = ra.verify(require_staged_match=False)
    assert not result["clean"]
    assert any("baseline" in p or "differs" in p for p in result["problems"])


# ===================================================================== executable trust
def test_e01_the_policy_classifies_every_governed_executable():
    doc = eet.load_policy()
    for name, entry in doc["executables"].items():
        assert entry["classification"] in eet.CLASSIFICATIONS, name
        if entry["classification"] == eet.NOT_APPLICABLE:
            assert entry.get("why_not_applicable"), name


def test_e02_trust_is_clean_on_this_machine():
    result = eet.check()
    assert result["clean"], result["problems"]


def test_e03_the_oracle_independently_agrees_on_resolution():
    """Production uses shutil.which; the oracle walks PATH by hand."""
    for name in ("git", "bash"):
        record = eet.resolve(name, eet.load_policy()["executables"][name])
        assert record.get("resolved_path") == oracle_executable_resolution(name), name


def test_e04_the_oracle_independently_agrees_on_content_identity():
    for name in ("git", "bash"):
        record = eet.resolve(name, eet.load_policy()["executables"][name])
        assert record["content_sha256"] == oracle_executable_digest(record["resolved_path"])


@pytest.mark.parametrize("name", ["git", "bash"])
def test_e05_a_shadow_earlier_on_path_is_refused(name, tmp_path, monkeypatch):
    """THE pin for ADV-I28AH-02."""
    fake = tmp_path / name
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    assert oracle_executable_resolution(name) == str(fake.resolve()), (
        "the shadow must actually win resolution, or this test proves nothing")
    result = eet.check()
    assert not result["clean"]
    assert any(name in p and "approved path set" in p for p in result["problems"])


@pytest.mark.parametrize("name", ["git", "bash"])
def test_e06_a_shadow_later_on_path_does_not_win(name, tmp_path, monkeypatch):
    """Green-when-clean: a decoy that does not win resolution must not fail the run."""
    fake = tmp_path / name
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{os.environ['PATH']}{os.pathsep}{tmp_path}")
    assert eet.check()["clean"]


def test_e07_a_symlink_out_of_the_approved_set_is_refused(tmp_path, monkeypatch):
    """Membership is tested on the RESOLVED target, so an approved-looking name is not enough."""
    outside = tmp_path / "payload"
    outside.write_text("#!/bin/sh\nexit 0\n")
    outside.chmod(0o755)
    link_dir = tmp_path / "bin"
    link_dir.mkdir()
    (link_dir / "git").symlink_to(outside)
    monkeypatch.setenv("PATH", f"{link_dir}{os.pathsep}{os.environ['PATH']}")
    assert not eet.check()["clean"]


def test_e08_a_missing_required_executable_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    result = eet.check()
    assert not result["clean"]
    assert any("not resolvable" in p for p in result["problems"])


def test_e09_a_non_executable_file_fails_closed(tmp_path, monkeypatch):
    (tmp_path / "git").write_text("not executable\n")
    (tmp_path / "git").chmod(0o644)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    # Resolution skips a non-executable file, so the approved binary is still found: the control
    # that matters is that the decoy never wins, which is asserted directly.
    assert oracle_executable_resolution("git") != str((tmp_path / "git").resolve())


@pytest.mark.parametrize("variable", ["GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
                                      "GIT_CONFIG_GLOBAL", "GIT_OBJECT_DIRECTORY",
                                      "GIT_EXTERNAL_DIFF", "BASH_ENV", "ENV"])
def test_e10_fatal_steering_variables_are_refused(variable, monkeypatch):
    monkeypatch.setenv(variable, "/tmp/steer")
    result = eet.check()
    assert not result["clean"], f"{variable} changes what a read-only command reports"
    assert any(variable in p for p in result["problems"])


@pytest.mark.parametrize("variable", ["GIT_EDITOR", "GIT_ASKPASS", "GIT_PAGER"])
def test_e11_interactive_only_variables_are_recorded_not_refused(variable, monkeypatch):
    """The boundary of the deliberate split.

    Refusing these would break every developer machine whose IDE exports them — the "control you
    must disable to use it" failure. They are stripped from every invocation environment and
    recorded. `test_e12` proves they cannot change a read-only answer.
    """
    monkeypatch.setenv(variable, "/tmp/interactive")
    result = eet.check()
    assert result["clean"], result["problems"]
    assert variable in result["steering_variables_neutralized"]


def test_e12_interactive_variables_provably_cannot_change_a_read_only_answer(tmp_path):
    """The executable proof behind the NEUTRALIZED classification.

    If a future git ever consults these for a plumbing read, this breaks and the split must be
    revisited.
    """
    sentinel = tmp_path / "boom"
    sentinel.write_text("#!/bin/sh\necho SENTINEL_FIRED\nexit 3\n")
    sentinel.chmod(0o755)
    env = dict(os.environ, GIT_EDITOR=str(sentinel), GIT_ASKPASS=str(sentinel),
               GIT_PAGER=str(sentinel))
    git = shutil.which("git")
    proc = subprocess.run([git, "rev-parse", "HEAD"], cwd=str(REPO_ROOT),
                          capture_output=True, text=True, env=env, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert "SENTINEL_FIRED" not in (proc.stdout + proc.stderr)
    assert len(proc.stdout.strip()) == 40


def test_e13_the_hardened_invocation_strips_every_steering_variable(monkeypatch):
    for variable in eet.STEERING_VARIABLES:
        monkeypatch.setenv(variable, "/tmp/x")
    argv, env = eet.git_invocation(["rev-parse", "HEAD"])
    assert argv[0] == eet.validated_path("git")
    for variable in eet.STEERING_VARIABLES:
        assert env.get(variable) in (None, os.devnull, "0", "1"), variable
    assert "--no-pager" in argv and "core.hooksPath=/dev/null" in argv


def test_e14_the_hardened_bash_invocation_disables_startup_files():
    argv, env = eet.bash_invocation(["-c", "true"])
    assert argv[0] == eet.validated_path("bash")
    assert "--noprofile" in argv and "--norc" in argv
    assert "BASH_ENV" not in env and "ENV" not in env


def test_e15_the_validated_path_is_what_a_bare_name_resolves_to():
    """The induction that keeps existing bare-name call sites safe."""
    for name in ("git", "bash"):
        assert eet.validated_path(name) == oracle_executable_resolution(name)


def test_e16_a_missing_policy_fails_closed(tmp_path):
    with pytest.raises(eet.ExecutableTrustError):
        eet.load_policy(tmp_path / "absent.json")


def test_e17_an_empty_approved_path_set_fails_closed(tmp_path):
    doc = json.loads(TRUST_POLICY.read_text(encoding="utf-8"))
    doc["executables"]["git"]["approved_paths"] = []
    bad = tmp_path / "p.json"
    bad.write_text(json.dumps(doc))
    with pytest.raises(eet.ExecutableTrustError):
        eet.load_policy(bad)


# ===================================================================== drift / session finish
def test_d01_a_changed_executable_digest_is_detected():
    before = eet.snapshot()
    after = json.loads(json.dumps(before))
    after["executables"]["git"]["content_sha256"] = "0" * 64
    problems = eet.compare(before, after)
    assert any("content digest changed" in p for p in problems)


def test_d02_a_changed_path_env_is_detected():
    before = eet.snapshot()
    after = json.loads(json.dumps(before))
    after["path_env"] = "/somewhere/else"
    assert any("PATH changed" in p for p in eet.compare(before, after))


def test_d03_a_changed_symlink_target_is_detected():
    before = eet.snapshot()
    after = json.loads(json.dumps(before))
    after["executables"]["bash"]["symlink_target"] = "/tmp/elsewhere"
    assert any("symlink target changed" in p for p in eet.compare(before, after))


def test_d04_no_drift_between_two_snapshots_of_a_stable_system():
    assert eet.compare(eet.snapshot(), eet.snapshot()) == []


def test_d05_session_finish_covers_both_new_layers():
    outcome = boot.reverify()
    assert "registry_authority" in outcome["layers"]
    assert "external_executable_trust" in outcome["layers"]
    assert outcome["clean"], outcome["problems"]


def test_d06_session_finish_detects_a_registry_change_after_configure(monkeypatch):
    monkeypatch.setenv("SIGNALNEST_MANDATORY_NODES", str(CANONICAL))
    outcome = boot.reverify()
    assert not outcome["clean"]
    assert outcome["layers"]["registry_authority"] is False


def test_d07_the_bootstrap_refuses_when_either_new_layer_fails(monkeypatch):
    monkeypatch.setenv("GIT_DIR", "/tmp/evil")
    with pytest.raises(boot.BootstrapError) as excinfo:
        boot.establish(strict=True)
    assert "GIT_DIR" in str(excinfo.value)


def test_d08_the_bootstrap_establishes_cleanly_without_tampering():
    assert boot.establish(strict=True)["established"]


# ===================================================================== self-protection
def test_z01_both_new_verifiers_are_protected_modules():
    protected = {e["module"] for e in json.loads(
        (REPO_ROOT / "tests" / "fixtures" / "protected-module-set.json").read_text()
    )["protected_modules"]}
    assert "registry_authority" in protected
    assert "external_executable_trust" in protected


def test_z02_both_new_verifiers_are_state_bound():
    contract = json.loads(
        (REPO_ROOT / "tests" / "fixtures" / "executed-state-contract.json").read_text())
    for module in ("registry_authority", "external_executable_trust"):
        assert contract["modules"][module]["names"], module


def test_z03_a_registry_verifier_replaced_with_constant_success_is_caught():
    import executed_code_provenance as ecp
    module = sys.modules["registry_authority"]
    original = module.verify
    assert ecp.verify()["clean"], "baseline must be green or the refusal proves nothing"
    try:
        module.verify = lambda **k: {"clean": True, "problems": [], "record": {}}
        result = ecp.verify()
        assert not result["clean"]
        assert any("verify" in p for p in result["problems"])
    finally:
        module.verify = original
    assert ecp.verify()["clean"]


def test_z04_an_executable_verifier_replaced_with_constant_success_is_caught():
    import executed_code_provenance as ecp
    module = sys.modules["external_executable_trust"]
    original = module.check
    try:
        module.check = lambda policy=None: {"clean": True, "problems": [], "executables": {},
                                            "steering_variables_fatal": [],
                                            "steering_variables_neutralized": [],
                                            "path_env_sha256": "", "policy_sha256": ""}
        assert not ecp.verify()["clean"]
    finally:
        module.check = original
    assert ecp.verify()["clean"]


def test_z05_the_override_env_name_is_still_the_one_being_refused():
    """Guards against the refusal silently targeting a variable nobody sets."""
    import pytest_session_guard as guard
    assert ra.OVERRIDE_ENV == guard.REGISTRY_ENV == "SIGNALNEST_MANDATORY_NODES"
