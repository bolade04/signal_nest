"""Session-finish protection, pre-execution executable trust, valid meta_path (Gate 4N-I28AM).

WHAT THESE TESTS DEFEND. Gate 4N-I28AL found three blockers.

ADV-I28AL-01: the historical I28AE `SYS_META_PATH_VARIANT` fixture generates Python that does not
compile, so its sitecustomize never executed, its finder was never installed, and `test_a03`
asserted properties of a session in which the attack did not exist.

ADV-I28AL-02: `REACHABLE_NOT_EXERCISED_IN_GRADED_PATH` was enforced only by a runtime-contradiction
check that never ran in a graded session — the bootstrap calls `check()` with no trace, so the
observed set was always empty and the six executables were neither path- nor content-bound.

ADV-I28AL-03: `reverify` and `pytest_sessionfinish` were in neither the critical-callables list nor
the executed-state contract, so replacing the final verifier with constant success left every
provenance layer clean.

THE ORACLES. `oracle_*` derive facts from external semantics only — filesystem sentinels, a hand
PATH walk, direct hashing, real subprocess execution — and import no production classifier or
expected-value constant as truth.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import executable_inventory as inv                # noqa: E402
import external_executable_trust as eet           # noqa: E402
import executed_code_provenance as ecp            # noqa: E402
import executed_state_provenance as esp           # noqa: E402
import signalnest_bootstrap as boot               # noqa: E402

FIXTURE = REPO_ROOT / "tests" / "fixtures" / "valid-meta-path-fixture.py"
HISTORICAL_DISPOSITION = REPO_ROOT / "tests" / "fixtures" / "historical-fixture-disposition.json"
PROTECTED_SET = REPO_ROOT / "tests" / "fixtures" / "protected-module-set.json"
TRUST_POLICY = REPO_ROOT / "tests" / "fixtures" / "executable-trust-policy.json"

STAGES = ("SITECUSTOMIZE_EXECUTED", "FINDER_INSTALLED", "FINDER_CONSULTED",
          "LOADER_SELECTED", "MODULE_EXECUTED")


# ===================================================================== independent oracles
def oracle_sentinel_stages(path: Path) -> list:
    """What actually happened, read from the filesystem. No production code involved."""
    if not path.is_file():
        return []
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def oracle_path_resolution(name: str) -> str | None:
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


def _attack_sandbox(tmp_path: Path) -> tuple:
    """A tree carrying the protected modules plus the VALID attacker sitecustomize."""
    root = tmp_path / "s"
    (root / "scripts").mkdir(parents=True)
    (root / "tests" / "fixtures").mkdir(parents=True)
    (root / ".github" / "workflows").mkdir(parents=True)
    # Gate 4N-I28BF-A: the docker_per_site layer reconciles the AUTHORED universe against the
    # DERIVED one, and the derivation reads every workflow. A sandbox carrying only ci.yml is
    # missing 32 authored sites, so establish() fails on ABSENCE before the attack under test can
    # be detected — the sandbox would measure the wrong thing.
    for workflow in ("ci.yml", "reader-publish.yml", "staging-publish.yml", "reader-run.yml"):
        source = REPO_ROOT / ".github" / "workflows" / workflow
        if source.is_file():
            (root / ".github" / "workflows" / workflow).write_bytes(source.read_bytes())
    for entry in json.loads(PROTECTED_SET.read_text(encoding="utf-8"))["protected_modules"]:
        src = REPO_ROOT / entry["relative_path"]
        (root / entry["relative_path"]).write_bytes(src.read_bytes())
    # executed_code_provenance is imported by the bootstrap; copy it explicitly so a sandbox that
    # predates its protection still runs. Belt and braces: if it is in the protected set the loop
    # above already wrote it, and rewriting identical bytes is harmless.
    (root / "scripts" / "executed_code_provenance.py").write_bytes(
        (REPO_ROOT / "scripts" / "executed_code_provenance.py").read_bytes())
    # Gate 4N-I28AO: the inventory imports the command-position model, and the state contract binds
    # site_taxonomy through a function-local import, so both must exist here or the sandbox refuses
    # on absence rather than on the attack under test.
    # Gate 4N-I28BB: the bootstrap imports the independent exec-transfer oracle; without it the
    # sandbox raises ModuleNotFoundError inside establish() and fails on ABSENCE, not the attack.
    for support in ("shell_positions.py", "site_taxonomy.py", "failure_propagation.py",
                    "shell_command_model.py", "ci_invocation_model.py", "site_behavior.py",
                    "exec_transfer_oracle.py",
                    # Gate 4N-I28BF-B1: docker_assurance_state (a protected module) imports
                    # expiry_authorization, which is not itself protected, so the PROTECTED_SET loop
                    # does not copy it; without it establish() fails on ABSENCE, not the attack.
                    "expiry_authorization.py"):
        src = REPO_ROOT / "scripts" / support
        if src.is_file():
            (root / "scripts" / support).write_bytes(src.read_bytes())
    for name in ("protected-module-set.json", "executed-state-contract.json",
                 "startup-policy.json", "executable-trust-policy.json",
                 "mandatory-pytest-nodes.json", "mandatory-session-baseline.json",
                 "assertion-contract-registry.json", "assertion-meta-contract.json",
                 "pytest-configuration-baseline.json",
                 # Gate 4N-I28AR: cache_authority arrives via the protected-module loop above, and
                 # it fails CLOSED without its policy. A sandbox lacking this would refuse on
                 # absence rather than on the attack under test, measuring nothing.
                 "cache-authority-policy.json",
                 # Gate 4N-I28AS: npm_authority arrives via the protected-module loop; it fails
                 # CLOSED without its policy.
                 "npm-authority-policy.json",
                 # Gate 4N-I28AT: docker_boundary arrives via the protected-module loop and fails
                 # CLOSED without its policy.
                 "docker-boundary-policy.json",
                 # Gate 4N-I28AV parser completeness fixtures.
                 "shell-case-grammar-contract.json",
                 "shell-grammar-corpus.json",
                 "framework-dispatch-observed.json"):
        src = REPO_ROOT / "tests" / "fixtures" / name
        if src.is_file():
            (root / "tests" / "fixtures" / name).write_bytes(src.read_bytes())
    # A policy describes the tree it governs. This sandbox has no bootstrap.sh/dev.sh/gen-types.sh,
    # so npm is genuinely not reachable here and its UNREACHABLE_FROM_GRADED_ROOTS precondition —
    # which needs site_taxonomy to re-derive reachability — has nothing to check. Dropping the
    # entry is the same principle the I28AE sandbox uses for conftest pins; keeping it would make
    # the sandbox refuse for a reason unrelated to the attack under test.
    # Gate 4N-I28BF-A, same principle as the npm entry below: a policy describes the tree it
    # governs. The docker_per_site layer reconciles authored sites against DERIVED ones, and this
    # sandbox's workflows are copies whose steps this synthetic tree does not fully reproduce, so
    # authored sites it cannot derive would make the sandbox refuse for a reason unrelated to the
    # attack under test. Restrict the authored universe to what this tree can actually derive.
    docker_policy = root / "tests" / "fixtures" / "docker-boundary-policy.json"
    if docker_policy.is_file():
        import subprocess as _sp
        derived = _sp.run([sys.executable, "-c",
                           "import sys,json;sys.path.insert(0,'scripts');import docker_boundary as d;"
                           "print(json.dumps(sorted(s['id'] for s in d.derive_call_sites()['sites'])))"],
                          cwd=root, capture_output=True, text=True)
        try:
            derivable = set(json.loads(derived.stdout.strip().splitlines()[-1]))
        except Exception:
            derivable = None
        if derivable is not None:
            doc = json.loads(docker_policy.read_text())
            doc["call_sites"] = [s for s in doc["call_sites"] if s.get("id") in derivable]
            docker_policy.write_text(json.dumps(doc, indent=1) + "\n")

    policy_file = root / "tests" / "fixtures" / "executable-trust-policy.json"
    if policy_file.is_file():
        policy = json.loads(policy_file.read_text())
        policy["executables"] = {
            name: entry for name, entry in policy["executables"].items()
            if entry.get("classification") != "UNREACHABLE_FROM_GRADED_ROOTS"}
        # Gate 4N-I28AO: dynamic_shell_sites names lines in shell scripts and workflow run: blocks
        # that this sandbox does not contain. The two-way check correctly reports every one as a
        # STALE declaration — a declaration that outlives its construct — so the sandbox must carry
        # a declaration set describing ITS tree, which is empty. Same principle as the policy
        # rewrite above: a declaration describes the tree it governs.
        policy["dynamic_shell_sites"] = [d for d in policy.get("dynamic_shell_sites", [])
                                         if str(d.get("module", "")).startswith("ci.yml")]
        policy_file.write_text(json.dumps(policy, indent=1, sort_keys=True))
    (root / "scripts" / "sitecustomize.py").write_bytes(FIXTURE.read_bytes())
    (root / "tests" / "test_probe.py").write_text("def test_probe():\n    assert True\n")
    # The executed-state contract binds state FILES by content, and the policy just rewritten is
    # one of them. Re-derive every FILE token against this sandbox's own copies — a contract
    # describes the tree it governs. A file the sandbox does not contain keeps its pin, so a
    # genuinely missing state file still fails closed.
    contract_file = root / "tests" / "fixtures" / "executed-state-contract.json"
    if contract_file.is_file():
        contract = json.loads(contract_file.read_text())
        for entry in contract.get("modules", {}).values():
            for key, token in list(entry.get("names", {}).items()):
                if not token.startswith("FILE:"):
                    continue
                _, rel, _digest = token.split(":", 2)
                local = root / rel
                if local.is_file():
                    entry["names"][key] = (
                        f"FILE:{rel}:{hashlib.sha256(local.read_bytes()).hexdigest()[:32]}")
        contract_file.write_text(json.dumps(contract, indent=1, sort_keys=True))
    sentinel = tmp_path / "sentinel.txt"
    return root, sentinel


def _run_attack(root: Path, sentinel: Path, *, with_bootstrap: bool) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONPATH=str(root / "scripts"),
               SIGNALNEST_METAPATH_SENTINEL=str(sentinel))
    env.pop("SIGNALNEST_MANDATORY_NODES", None)
    argv = [sys.executable, "-m", "pytest", "tests/", "-q", "-p", "no:randomly"]
    if with_bootstrap:
        argv += ["-p", "signalnest_bootstrap"]
    return subprocess.run(argv, cwd=root, env=env, capture_output=True, text=True, timeout=300)


# ===================================================================== ADV-I28AL-01
def test_m01_the_valid_fixture_compiles():
    """The historical one does not; that is the whole defect."""
    import ast
    ast.parse(FIXTURE.read_text(encoding="utf-8"))


def test_m02_the_historical_fixture_still_does_not_compile():
    """Pinned so the demotion cannot be quietly reversed, and the bytes stay untouched."""
    import ast
    import importlib.util as u
    spec = u.spec_from_file_location("i28ae", REPO_ROOT / "tests"
                                     / "test_i28ae_executed_provenance.py")
    module = u.module_from_spec(spec)
    spec.loader.exec_module(module)
    with pytest.raises(SyntaxError):
        ast.parse(textwrap.dedent(module.SYS_META_PATH_VARIANT))


def test_m03_the_attack_actually_executes_under_plain_python(tmp_path):
    """THE positive control. Every stage must fire, or a later refusal proves nothing."""
    root, sentinel = _attack_sandbox(tmp_path)
    env = dict(os.environ, PYTHONPATH=str(root / "scripts"),
               SIGNALNEST_METAPATH_SENTINEL=str(sentinel))
    proc = subprocess.run(
        [sys.executable, "-c",
         "import pytest_session_guard as g, sys;"
         "print('LOADER', type(getattr(g.__spec__, 'loader', None)).__name__);"
         "print('EXITSTATUS_REFS', open(g.__file__).read().count('session.exitstatus = 3'))"],
        cwd=root, env=env, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    stages = oracle_sentinel_stages(sentinel)
    for stage in STAGES:
        assert stage in stages, f"{stage} did not fire; the attack did not reach that point: {stages}"
    assert "LOADER SubstitutingFinder" in proc.stdout, proc.stdout
    assert "MODULE_EXECUTED" in stages


def test_m04_the_protected_session_refuses_the_valid_attack(tmp_path):
    """And it must refuse for a NAMED reason, with the attack proven to have started."""
    root, sentinel = _attack_sandbox(tmp_path)
    proc = _run_attack(root, sentinel, with_bootstrap=True)
    assert proc.returncode != 0, proc.stdout[-2000:]
    combined = proc.stdout + proc.stderr
    assert "sitecustomize" in combined and "PROHIBITED" in combined, combined[-1500:]
    stages = oracle_sentinel_stages(sentinel)
    assert "SITECUSTOMIZE_EXECUTED" in stages, (
        "the fixture never ran, so this refusal is not evidence about the attack — exactly the "
        "failure mode ADV-I28AL-01 describes")
    assert "FINDER_INSTALLED" in stages


def test_m05_a_refusal_without_sentinels_is_not_accepted_as_a_catch(tmp_path):
    """The anti-self-deception control: prove the test can tell the two apart."""
    root, sentinel = _attack_sandbox(tmp_path)
    (root / "scripts" / "sitecustomize.py").write_text("    this is not valid python\n")
    proc = _run_attack(root, sentinel, with_bootstrap=True)
    assert proc.returncode != 0
    assert oracle_sentinel_stages(sentinel) == [], (
        "a non-compiling fixture must leave NO sentinel; if this ever fires, the sentinel is "
        "measuring something other than execution")


def test_m06_the_historical_fixture_is_demoted_in_a_declaration():
    doc = json.loads(HISTORICAL_DISPOSITION.read_text(encoding="utf-8"))
    entry = doc["dispositions"]["SYS_META_PATH_VARIANT"]
    assert entry["syntactically_valid"] is False
    assert entry["accepted_as_current_proof"] is False
    assert entry["superseded_by"].endswith("valid-meta-path-fixture.py")
    assert entry["historical_bytes_modified"] is False


# ===================================================================== ADV-I28AL-02
def test_e01_the_retired_classification_is_refused(tmp_path):
    doc = json.loads(TRUST_POLICY.read_text(encoding="utf-8"))
    doc["executables"]["rsync"]["classification"] = eet.REACHABLE_NOT_EXERCISED_IN_GRADED_PATH
    result = eet.check(doc)
    assert not result["clean"]
    assert any("retired" in p for p in result["problems"])


def test_e02_no_executable_still_carries_the_retired_classification():
    doc = eet.load_policy()["executables"]
    offenders = [n for n, v in doc.items()
                 if v["classification"] == eet.REACHABLE_NOT_EXERCISED_IN_GRADED_PATH]
    assert not offenders, offenders


@pytest.mark.parametrize("name", ["aws", "env", "find", "rsync", "sleep"])
def test_e03_formerly_unexercised_executables_are_now_bound_before_execution(name):
    """Trust is decided at check() time, which the bootstrap runs at pytest_configure."""
    record = eet.check()["executables"].get(name)
    assert record is not None, f"{name} is not resolved by the trust layer"
    assert record.get("content_sha256"), f"{name} has no content identity"
    assert record["content_sha256"] == oracle_digest(record["resolved_path"])


@pytest.mark.parametrize("name", ["aws", "env", "find", "rsync", "sleep"])
def test_e04_a_shadow_of_each_is_refused(name, tmp_path, monkeypatch):
    fake = tmp_path / name
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    assert oracle_path_resolution(name) == str(fake.resolve()), (
        "the shadow must actually win resolution, or this proves nothing")
    result = eet.check()
    assert not result["clean"]
    assert any(name in p and "approved path set" in p for p in result["problems"])


def test_e05_npm_is_bound_because_a_graded_workflow_job_invokes_it():
    """RE-ENCODED at Gate 4N-I28AO, and the reason matters more than the assertion.

    This test used to assert `npm` carried UNREACHABLE_FROM_GRADED_ROOTS. Gate 4N-I28AO's
    command-position derivation reads `ci.yml` run: blocks for the first time and found npm invoked
    by eight steps of the `frontend-quality` job — lint, type-check, test and build — whose outcome
    blocks release. The unreachability claim was therefore FALSE, and the precondition Gate
    4N-I28AN found unprotected refused it as soon as the derivation could see those sites.

    The precondition worked. The classification did not. npm is now bound.

    RE-ENCODED AGAIN at Gate 4N-I28AS. APPROVED_PATH_SET_AND_CONTENT_BOUND was itself too weak,
    because the entry reached it through `approved_path_prefixes`, and a prefix match SKIPPED the
    membership test entirely — Gate 4N-I28AP finding ADV-I28AP-02. npm's identity is not a file
    digest; it is a chain (wrapper -> symlink -> CLI JavaScript -> Node -> package root ->
    package.json -> installation family), so it is now TOOLCHAIN_IDENTITY_DELEGATED to
    `npm_authority`. The assertion below tracks that move rather than being relaxed: the delegate
    is named, and it must adjudicate npm cleanly on this tree.
    """
    entry = eet.load_policy()["executables"]["npm"]
    assert entry["classification"] == "TOOLCHAIN_IDENTITY_DELEGATED"
    assert entry["delegated_to"] == "npm_authority"
    assert not entry.get("approved_path_prefixes"), (
        "the prefix allowance is retired; an entry still carrying one would read as authorization")
    assert entry["bound_before_execution"] is True
    assert "ci.yml" in entry["call_site_modules"], (
        "the declaration must name the workflow, which is where the graded invocations live")
    assert eet.check()["clean"], "npm must be bound cleanly on this tree"


def test_e06_no_executable_claims_unreachability_it_cannot_support():
    """The retired classification must have no users, and the precondition must still work."""
    policy = eet.load_policy()["executables"]
    holders = [n for n, e in policy.items()
               if e.get("classification") == eet.UNREACHABLE_FROM_GRADED_ROOTS]
    assert holders == [], (
        f"{holders} claim unreachability; Gate 4N-I28AO showed that claim must be re-derived from "
        "a derivation that can see workflow run: blocks, not asserted")
    # The mechanism itself is still live: give it a synthetic holder and it must refuse.
    doc = eet.load_policy()
    import site_taxonomy
    a_graded_root = sorted({r["module"] for r in site_taxonomy.release_roots()})[0]
    doc["executables"]["probe-tool"] = {
        "classification": eet.UNREACHABLE_FROM_GRADED_ROOTS,
        "call_site_modules": [a_graded_root]}
    original = inv.static_inventory
    try:
        inv.static_inventory = lambda: {
            "invocations": [{"module": a_graded_root, "function": "f", "line": 1,
                             "call": "run", "executable": "probe-tool", "form": "bare_name"}],
            "unresolved": [], "foreign": [], "executables": ["probe-tool"], "source_count": 1}
        result = eet.check(doc)
        assert not result["clean"]
        assert any("release command root" in p for p in result["problems"])
    finally:
        inv.static_inventory = original


def test_e07_an_undeclared_call_site_breaks_the_precondition():
    """Unchanged in substance, retargeted at a synthetic holder now that npm is bound."""
    doc = eet.load_policy()
    doc["executables"]["probe-tool"] = {
        "classification": eet.UNREACHABLE_FROM_GRADED_ROOTS,
        "call_site_modules": ["a-module-that-does-not-invoke-it.sh"]}
    original = inv.static_inventory
    try:
        inv.static_inventory = lambda: {
            "invocations": [{"module": "somewhere-else.sh", "function": "f", "line": 1,
                             "call": "shell", "executable": "probe-tool", "form": "bare_name"}],
            "unresolved": [], "foreign": [], "executables": ["probe-tool"], "source_count": 1}
        result = eet.check(doc)
        assert not result["clean"]
        assert any("not declared in call_site_modules" in p for p in result["problems"])
    finally:
        inv.static_inventory = original


def test_e08_trust_is_decided_before_graded_work_not_after():
    """The heart of ADV-I28AL-02: the decision must not depend on a runtime trace."""
    result = eet.check()
    assert result["clean"]
    bound = set(result["executables"])
    assert {"aws", "env", "find", "rsync", "sleep", "tar", "cat", "git", "bash"} <= bound
    # And it holds with no runtime observation whatsoever, which is the graded-session condition.
    assert inv.check()["runtime"]["available"] is False


# ===================================================================== ADV-I28AL-03
def test_c01_reverify_and_sessionfinish_are_critical_callables():
    entry = next(e for e in json.loads(PROTECTED_SET.read_text(encoding="utf-8"))
                 ["protected_modules"] if e["module"] == "signalnest_bootstrap")
    assert "reverify" in entry["critical_callables"]
    assert "pytest_sessionfinish" in entry["critical_callables"]


def test_c02_replacing_reverify_is_detected():
    module = sys.modules["signalnest_bootstrap"]
    original = module.reverify
    assert ecp.verify()["clean"], "baseline must be green or the refusal proves nothing"
    try:
        module.reverify = lambda config=None: {"clean": True, "problems": [], "layers": {}}
        result = ecp.verify()
        assert not result["clean"]
        assert any("reverify" in p for p in result["problems"])
    finally:
        module.reverify = original
    assert ecp.verify()["clean"]


def test_c03_replacing_pytest_sessionfinish_is_detected():
    module = sys.modules["signalnest_bootstrap"]
    original = module.pytest_sessionfinish
    try:
        module.pytest_sessionfinish = lambda session, exitstatus: None
        result = ecp.verify()
        assert not result["clean"]
        assert any("pytest_sessionfinish" in p for p in result["problems"])
    finally:
        module.pytest_sessionfinish = original
    assert ecp.verify()["clean"]


def test_c04_the_final_verifier_runs_every_layer():
    outcome = boot.reverify()
    for layer in ("executed_code", "executed_state", "startup_policy", "registry_authority",
                  "external_executable_trust", "executable_inventory"):
        assert layer in outcome["layers"], layer
    assert outcome["clean"], outcome["problems"]


def test_c05_a_post_configure_mutation_is_detected_at_session_finish(monkeypatch):
    monkeypatch.setenv("SIGNALNEST_MANDATORY_NODES", "/tmp/decoy.json")
    outcome = boot.reverify()
    assert not outcome["clean"]
    assert outcome["layers"]["registry_authority"] is False


def test_c06_the_hook_is_registered_and_named_correctly():
    """Protecting an alias while execution calls a different binding would be worthless."""
    source = (REPO_ROOT / "scripts" / "signalnest_bootstrap.py").read_text(encoding="utf-8")
    assert "def pytest_sessionfinish(" in source
    assert callable(boot.pytest_sessionfinish)
    assert boot.pytest_sessionfinish.__module__ == "signalnest_bootstrap"


def test_c07_the_state_contract_covers_both_callables():
    contract = json.loads((REPO_ROOT / "tests" / "fixtures"
                           / "executed-state-contract.json").read_text(encoding="utf-8"))
    names = contract["modules"]["signalnest_bootstrap"]["names"]
    assert "reverify" in names or "pytest_sessionfinish" in names, (
        "at least one must be reachable state; both are critical callables")


# ===================================================================== self-protection
def test_z01_the_sentinel_itself_cannot_be_neutralised_silently(tmp_path):
    """If the sentinel path is unwritable the fixture raises, rather than reporting no stages."""
    root, _ = _attack_sandbox(tmp_path)
    bad = tmp_path / "no-such-dir" / "sentinel.txt"
    env = dict(os.environ, PYTHONPATH=str(root / "scripts"),
               SIGNALNEST_METAPATH_SENTINEL=str(bad))
    proc = subprocess.run([sys.executable, "-c", "import sys"], cwd=root, env=env,
                          capture_output=True, text=True, timeout=60)
    assert not bad.exists()
    assert proc.returncode != 0 or "Error" in proc.stderr or proc.stderr, (
        "an unwritable sentinel must surface, not silently produce an empty stage list")


def test_z02_the_inventory_still_fails_on_an_unclassified_executable():
    static = inv.static_inventory()
    mutated = {k: (json.loads(json.dumps(v)) if k != "foreign" else []) for k, v in static.items()}
    mutated["executables"] = sorted(set(mutated["executables"]) | {"brand-new-tool"})
    mutated["invocations"].append({"module": "p.py", "function": "f", "line": 1, "call": "run",
                                   "executable": "brand-new-tool", "form": "bare_name"})
    original = inv.static_inventory
    try:
        inv.static_inventory = lambda: mutated
        assert not inv.check()["clean"]
    finally:
        inv.static_inventory = original


def test_z03_runtime_observation_remains_a_contradiction_detector(tmp_path):
    trace = tmp_path / "trace.log"
    trace.write_text("00:00:00 ghost-tool --x\n")
    result = inv.check(trace_path=trace)
    assert not result["clean"]
    assert any("ghost-tool" in p for p in result["problems"])


# ============================================ the roots of the walk (self-finding I28AM-01)
#
# Registering `reverify` and `pytest_sessionfinish` as critical callables closed the mechanism
# ADV-I28AL-03 named and left a second one open: the executed-state contract derived names by
# walking co_names FROM the critical callables and never pinned the callables themselves. A
# callable referenced nowhere inside its own module — a pluggy hook, a public entry point — got no
# token, so replacing its body on disk passed all six layers clean. `reverify` was caught only
# because `pytest_sessionfinish` happens to call it by name. These tests exist so that accident can
# never again be mistaken for a control.

STATE_CONTRACT = REPO_ROOT / "tests" / "fixtures" / "executed-state-contract.json"


def _contract() -> dict:
    return json.loads(STATE_CONTRACT.read_text(encoding="utf-8"))


def test_r01_every_critical_callable_is_itself_pinned():
    """The roots of the reachability walk are state too."""
    doc = _contract()
    unpinned = []
    for module_name, entry in doc["modules"].items():
        for qual in entry.get("critical_callables") or []:
            if "." in qual:
                continue
            token = entry["names"].get(qual)
            if token is None:
                unpinned.append(f"{module_name}.{qual}")
            elif not token.startswith(("CALLABLE:", "CLASS:")):
                unpinned.append(f"{module_name}.{qual} -> {token[:24]}")
    assert not unpinned, (
        "critical callables with no pinned identity of their own: " + repr(unpinned))


def test_r02_the_externally_called_entry_points_are_covered():
    """Named explicitly because these are the ones nothing inside their module references."""
    doc = _contract()
    for module_name, qual in [("signalnest_bootstrap", "pytest_sessionfinish"),
                              ("signalnest_bootstrap", "pytest_configure"),
                              ("executed_code_provenance", "verify"),
                              ("executed_state_provenance", "verify"),
                              ("startup_policy", "check"),
                              ("registry_authority", "verify"),
                              ("external_executable_trust", "check"),
                              ("executable_inventory", "check"),
                              ("pytest_session_guard", "pytest_configure")]:
        token = doc["modules"][module_name]["names"].get(qual)
        assert token and token.startswith("CALLABLE:"), (
            f"{module_name}.{qual} is called only from outside its module and must be pinned "
            "explicitly; nothing inside the module names it, so the co_names walk cannot reach it")


@pytest.mark.parametrize("module_name,qual", [
    ("signalnest_bootstrap", "pytest_sessionfinish"),
    ("executed_code_provenance", "verify"),
    ("startup_policy", "check"),
])
def test_r03_replacing_an_externally_called_entry_point_is_detected(module_name, qual):
    """Mutate the live code object and require executed-STATE provenance to refuse.

    Baseline green first: a refusal from an already-failing layer proves nothing.
    """
    assert esp.verify()["clean"], "baseline must be green or the refusal proves nothing"
    module = sys.modules[module_name]
    original = getattr(module, qual)

    def impostor(*args, **kwargs):
        return {"clean": True, "problems": [], "results": [], "protected_modules": 0,
                "protected_set_sha256": "0" * 64}

    try:
        setattr(module, qual, impostor)
        result = esp.verify()
        assert not result["clean"], f"replacing {module_name}.{qual} was not detected"
        assert any(qual in p for p in result["problems"]), (
            f"the refusal must NAME {qual}: {result['problems'][:2]}")
    finally:
        setattr(module, qual, original)
    assert esp.verify()["clean"], "the tree must be restored after the mutation"


def test_r04_the_state_token_does_not_depend_on_hash_seed():
    """Set constants fold to frozensets whose repr order is seed-dependent.

    Gate 4N-I28AG fixed this in executed_code_provenance. The two layers keep separate
    implementations on purpose, and the fix was never made in executed_state_provenance; pinning
    `startup_policy.check` for the first time surfaced it as a refusal on an unmodified tree.
    """
    digests = set()
    for seed in ("0", "7", "424242"):
        proc = subprocess.run(
            [sys.executable, "-c",
             "import hashlib,json,sys;sys.path.insert(0,'scripts');"
             "import executed_state_provenance as esp;"
             "import startup_policy as sp;"
             "print(esp.state_identity(sp,['check'])[0]['check'])"],
            cwd=REPO_ROOT, env=dict(os.environ, PYTHONHASHSEED=seed),
            capture_output=True, text=True, timeout=120)
        assert proc.returncode == 0, proc.stderr[-600:]
        digests.add(proc.stdout.strip())
    assert len(digests) == 1, (
        f"the token for startup_policy.check varies with PYTHONHASHSEED: {sorted(digests)}")


def test_r05_a_set_constant_is_canonicalised_order_free():
    """Green-when-clean control for the canonicaliser itself."""
    a = frozenset({"alpha", "beta", "gamma"})
    b = frozenset({"gamma", "alpha", "beta"})
    assert a == b
    assert esp._order_free_const(a) == esp._order_free_const(b)
    assert esp._order_free_const(frozenset({"x"})) != esp._order_free_const(frozenset({"y"})), (
        "canonicalisation must not erase the difference between different sets")
    assert esp._order_free_const((1, frozenset({"p", "q"}))) == \
        esp._order_free_const((1, frozenset({"q", "p"}))), "tuples must recurse"
