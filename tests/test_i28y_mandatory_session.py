"""Gate 4N-I28Y — the mandatory pytest SESSION must actually run its assurance controls.

THE DEFECT THIS CLOSES. Gate 4N-I28X proved, from a proven-green baseline (2983 passed / 0 failed
/ 83 skipped) on the exact final I28W tree, that the whole assertion-control system could be
removed from the graded run with nothing failing. Three one-line mutations each restored the
historical dead-branch defect with a completely green suite:

  A  `collect_ignore = [...]` in tests/conftest.py         -> 2902 passed, 0 failed, 84 skipped
  B  module-level `pytestmark = pytest.mark.skip(...)`     -> 2902 passed, 0 failed, 164 skipped
  C  `--deselect <node id>` on the graded pytest command   -> zero policy problems

WHAT IS PINNED HERE. Each bypass is applied to a synthetic session that carries the REAL guard and
an authored registry, and the guard must reject it. The pins are deliberately run as real pytest
subprocesses: asking the guard's functions directly would prove only that its arithmetic works,
not that a live session with those mutations actually fails.

MANDATORY PATH. `test_the_session_guard_is_active_in_this_session` and
`test_the_mandatory_registry_matches_its_pinned_baseline` are themselves listed in
tests/fixtures/mandatory-pytest-nodes.json. The first closes the circularity — a guard that is not
loaded cannot report its own absence, so a node that FAILS when the plugin is missing is required.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import pytest_session_guard as guard  # noqa: E402
import pytest_config_contract as cfg  # noqa: E402
import ci_invocation_model as cim  # noqa: E402

REGISTRY = REPO_ROOT / "tests" / "fixtures" / "mandatory-pytest-nodes.json"
BASELINE = REPO_ROOT / "tests" / "fixtures" / "mandatory-session-baseline.json"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"


# ============================================================== the two mandatory nodes
def test_the_session_guard_is_active_in_this_session(pytestconfig):
    """THE anti-circularity node. A guard that is not loaded cannot report its own absence.

    If this fails, the session is running without the authoritative guard and NOTHING is
    checking that the assurance controls ran. Re-run through the graded invocation:

        PYTHONPATH=scripts python -m pytest tests/ -q -p signalnest_bootstrap -p pytest_session_guard
    """
    assert pytestconfig.pluginmanager.hasplugin(guard.PLUGIN_NAME), (
        "the authoritative mandatory-session guard is NOT loaded in this pytest session, so no "
        "control is verifying that the assurance tests were collected and executed. This is the "
        "state Gate 4N-I28X found exploitable. Run through the graded invocation: "
        "PYTHONPATH=scripts python -m pytest tests/ -q -p signalnest_bootstrap -p pytest_session_guard")
    # GATE 4N-I28AB: hasplugin(name) alone was the ADV-I28AA-01 mechanism. Identity is now
    # established by exact type from the module this test imported, and by the registered object
    # being the one the module built — neither of which a same-named decoy can satisfy. The full
    # provenance and genuine-hook-execution checks live in the independently pinned
    # tests/test_i28ab_guard_identity.py so this control does not stand alone.
    registered = pytestconfig.pluginmanager.get_plugin(guard.PLUGIN_NAME)
    assert type(registered) is guard.MandatorySessionGuard, (
        f"the guard name is held by {type(registered).__module__}.{type(registered).__name__}, "
        "not the genuine MandatorySessionGuard (Gate 4N-I28AA finding ADV-I28AA-01)")
    live = pytestconfig._signalnest_guard
    assert live is registered, "config._signalnest_guard is not the registered plugin object"
    assert live.hook_log.get("phase:call", 0) >= 1, (
        "the guard exposes no genuine call-phase hook evidence; an impersonator can copy "
        "attributes but cannot manufacture a hook log")
    assert live.registry_path == guard.REGISTRY, (
        f"the guard is enforcing {live.registry_path}, not the repository registry "
        f"{guard.REGISTRY}. An overridden registry may not lower the bar for the graded session.")
    assert live.nodes, "the guard resolved no mandatory nodes"


def test_the_mandatory_registry_matches_its_pinned_baseline():
    """Bounded protection against the registry being emptied, trimmed or retargeted."""
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    assert hashlib.sha256(REGISTRY.read_bytes()).hexdigest() == baseline["registry_sha256"], (
        "the mandatory-node registry changed without updating its pinned baseline. A change is "
        "legitimate, but it must be accompanied by an entry in the baseline's change_ledger — "
        "that is what makes a REDUCTION visible.")
    doc = json.loads(REGISTRY.read_text(encoding="utf-8"))
    ids = sorted(n["node_id"] for n in doc["mandatory_nodes"])
    assert len(doc["mandatory_nodes"]) == baseline["mandatory_node_count"]
    assert ids == baseline["mandatory_node_ids"], "the mandatory node set was changed"
    for n in doc["mandatory_nodes"]:
        assert n["proving_bypass_mutation"], f"{n['node_id']}: membership must be earned"
        assert n["acceptable_outcome"] == "passed"
        assert "skipped" in n["prohibited_outcomes"] and "deselected" in n["prohibited_outcomes"]
    assert baseline["change_ledger"], "the change ledger must record why the registry changed"
    assert hashlib.sha256((REPO_ROOT / "scripts" / "pytest_session_guard.py").read_bytes()
                          ).hexdigest() == baseline["guard_sha256"], (
        "scripts/pytest_session_guard.py changed without re-approving it in the baseline")


# ============================================================== synthetic-session harness
GOOD_TEST = "def test_protected():\n    assert 1 + 1 == 2\n"


def _sandbox(tmp_path: Path, *, files: dict[str, str], nodes: list[dict] | None = None,
             conftest: str = "") -> Path:
    root = tmp_path / "s"
    (root / "tests" / "fixtures").mkdir(parents=True)
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(text))
    if conftest:
        (root / "tests" / "conftest.py").write_text(textwrap.dedent(conftest))
    node_list = nodes if nodes is not None else [{
        "node_id": "tests/test_protected.py::test_protected",
        "protected_invariant": "the synthetic control runs",
        "owning_layer": "SANDBOX", "required_phase": "call", "acceptable_outcome": "passed",
        "prohibited_outcomes": ["failed", "skipped", "xfailed", "deselected"],
        "why_mandatory": "sandbox", "proving_bypass_mutation": "sandbox", "category": "SANDBOX"}]
    (root / "tests/fixtures/mandatory-pytest-nodes.json").write_text(
        json.dumps({"mandatory_nodes": node_list}))
    return root


def _sandbox_scripts(root: Path, registry: Path | None = None) -> Path:
    """Give the sandbox its own scripts/ so REPO_ROOT resolves INSIDE it.

    GATE 4N-I28AI. These sandboxes used to run the real scripts/ with SIGNALNEST_MANDATORY_NODES
    redirecting the registry. That override is retired (ADV-I28AH-01): an in-tree redirect was what
    let the enforced mandatory set fall from twelve nodes to one. A sandbox is its own tree, so its
    registry belongs at the canonical path within it.
    """
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    for rel in ("scripts/pytest_session_guard.py", "scripts/registry_authority.py",
                "scripts/executed_code_provenance.py", "scripts/executed_state_provenance.py",
                "scripts/external_executable_trust.py", "scripts/signalnest_bootstrap.py",
                "scripts/startup_policy.py", "scripts/assertion_contracts.py",
                "scripts/pytest_config_contract.py",
                "tests/fixtures/protected-module-set.json",
                "tests/fixtures/executed-state-contract.json",
                "tests/fixtures/startup-policy.json",
                "tests/fixtures/executable-trust-policy.json",
                "tests/fixtures/mandatory-session-baseline.json",
                "tests/fixtures/assertion-contract-registry.json",
                "tests/fixtures/assertion-meta-contract.json",
                "tests/fixtures/pytest-configuration-baseline.json"):
        src = REPO_ROOT / rel
        if src.is_file():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_bytes(src.read_bytes())
    # A test may drive the guard over an ALTERNATE registry; in the new model that means writing
    # it to the canonical path, not redirecting to it.
    canonical = root / "tests/fixtures/mandatory-pytest-nodes.json"
    if registry is not None:
        if Path(registry).is_file():
            canonical.write_bytes(Path(registry).read_bytes())
        else:
            # The caller asked for a registry that does not exist. Under the I28AI model that is
            # expressed by the canonical path being ABSENT, not by pointing elsewhere.
            canonical.unlink(missing_ok=True)
    base = root / "tests/fixtures/mandatory-session-baseline.json"
    if base.is_file() and canonical.is_file():
        doc = json.loads(base.read_text())
        raw = canonical.read_bytes()
        doc["registry_sha256"] = hashlib.sha256(raw).hexdigest()
        try:
            parsed = json.loads(raw)
            doc["mandatory_node_count"] = len(parsed["mandatory_nodes"])
            doc["mandatory_node_ids"] = sorted(
                n.get("node_id") for n in parsed["mandatory_nodes"])
        except (json.JSONDecodeError, KeyError, TypeError):
            pass                                   # malformed on purpose: leave the pin to fail
        base.write_text(json.dumps(doc, indent=1, sort_keys=True))
    return root / "scripts"


def _run(root: Path, extra=(), *, with_guard=True, registry: Path | None = None,
         paths=("tests/",)):
    env = dict(os.environ, PYTHONPATH=str(_sandbox_scripts(root, registry)))
    env.pop("SIGNALNEST_MANDATORY_NODES", None)
    env.pop("SIGNALNEST_SESSION_MARKER", None)
    args = [sys.executable, "-m", "pytest", *paths, "-q", "-p", "no:randomly"]
    if with_guard:
        args += ["-p", "pytest_session_guard"]
    return subprocess.run(args + list(extra), cwd=root, env=env, capture_output=True,
                          text=True, timeout=300)


def _guard_rejected(proc) -> bool:
    return proc.returncode != 0 and "MANDATORY SESSION GUARD: FAILED" in (
        proc.stdout + proc.stderr)


# ============================================================== green when clean
def test_g00_a_clean_synthetic_session_passes(tmp_path):
    """Green-when-clean. Without this the pins below would prove only that everything fails."""
    root = _sandbox(tmp_path, files={"tests/test_protected.py": GOOD_TEST})
    proc = _run(root)
    assert proc.returncode == 0, proc.stdout[-2000:]
    assert "MANDATORY SESSION GUARD: FAILED" not in proc.stdout


# ============================================================== the exact I28X bypass pins
def test_a_collect_ignore_bypass_is_caught(tmp_path):
    """I28X bypass A — the conftest form that needed no command-line change at all."""
    root = _sandbox(tmp_path, files={"tests/test_protected.py": GOOD_TEST},
                    conftest='collect_ignore = ["test_protected.py"]\n')
    proc = _run(root)
    assert _guard_rejected(proc), proc.stdout[-2000:]
    assert "NOT COLLECTED" in proc.stdout


def test_b_module_level_skip_bypass_is_caught(tmp_path):
    """I28X bypass B — collected, so the old collection pin was satisfied; never executed."""
    root = _sandbox(tmp_path, files={
        "tests/test_protected.py": "import pytest\n"
                                   "pytestmark = pytest.mark.skip(reason='x')\n" + GOOD_TEST})
    proc = _run(root)
    assert _guard_rejected(proc), proc.stdout[-2000:]
    assert "SKIPPED" in proc.stdout


def test_c_deselect_bypass_is_caught(tmp_path):
    """I28X bypass C — the graded command form."""
    root = _sandbox(tmp_path, files={"tests/test_protected.py": GOOD_TEST})
    proc = _run(root, ["--deselect", "tests/test_protected.py::test_protected"])
    assert _guard_rejected(proc), proc.stdout[-2000:]
    assert "DESELECTED" in proc.stdout or "NOT COLLECTED" in proc.stdout


# ============================================================== the rest of the selection matrix
@pytest.mark.parametrize("label,extra,files,conftest", [
    ("d01 -k excludes the node", ["-k", "not protected"], {"tests/test_protected.py": GOOD_TEST}, ""),
    ("d02 -m excludes the node", ["-m", "nonexistent_marker"],
     {"tests/test_protected.py": GOOD_TEST}, ""),
    ("d03 --ignore removes the file", ["--ignore", "tests/test_protected.py"],
     {"tests/test_protected.py": GOOD_TEST}, ""),
    ("d04 --ignore-glob removes the file", ["--ignore-glob", "*protected*"],
     {"tests/test_protected.py": GOOD_TEST}, ""),
    ("d06 collect_ignore_glob", [], {"tests/test_protected.py": GOOD_TEST},
     'collect_ignore_glob = ["*protected*"]\n'),
    ("d07 pytest_ignore_collect hook", [], {"tests/test_protected.py": GOOD_TEST},
     'def pytest_ignore_collect(collection_path, config):\n'
     '    return "protected" in str(collection_path)\n'),
    ("d08 modifyitems deselects everything", [], {"tests/test_protected.py": GOOD_TEST},
     'def pytest_collection_modifyitems(session, config, items):\n'
     '    config.hook.pytest_deselected(items=list(items))\n'
     '    items.clear()\n'),
    ("d09 runtime skip before the assertions", [],
     {"tests/test_protected.py": "import pytest\n\n\ndef test_protected():\n"
                                 "    pytest.skip('x')\n    assert 1 + 1 == 2\n"}, ""),
    ("d10 setup skip through an autouse fixture", [],
     {"tests/test_protected.py": "import pytest\n\n\n@pytest.fixture(autouse=True)\n"
                                 "def _f():\n    pytest.skip('setup')\n\n\n"
                                 "def test_protected():\n    assert 1 + 1 == 2\n"}, ""),
    ("d11 non-strict xfail", [],
     {"tests/test_protected.py": "import pytest\n"
                                 "pytestmark = pytest.mark.xfail(reason='x', strict=False)\n"
                                 "def test_protected():\n    assert False\n"}, ""),
    ("d12 strict xfail that passes", [],
     {"tests/test_protected.py": "import pytest\n"
                                 "pytestmark = pytest.mark.xfail(reason='x', strict=False)\n"
                                 + GOOD_TEST}, ""),
    ("d13 the node is renamed away", [],
     {"tests/test_protected.py": "def test_renamed_away():\n    assert True\n"}, ""),
    ("d14 collection error in the protected file", [],
     {"tests/test_protected.py": "def test_protected(:\n    pass\n"}, ""),
    ("d15 the protected file is deleted", [],
     {"tests/test_other.py": "def test_o():\n    assert True\n"}, ""),
    ("d16 the node fails", [],
     {"tests/test_protected.py": "def test_protected():\n    assert False\n"}, ""),
])
def test_selection_and_outcome_matrix(tmp_path, label, extra, files, conftest):
    root = _sandbox(tmp_path, files=files, conftest=conftest)
    proc = _run(root, extra)
    assert _guard_rejected(proc), f"{label} was NOT caught:\n{proc.stdout[-2000:]}"


# ============================================================== guard self-protection
def test_s01_missing_guard_plugin_is_caught_by_the_mandatory_node(tmp_path):
    """Without the plugin nothing observes the session — so a node must fail on its absence."""
    root = _sandbox(tmp_path, files={"tests/test_protected.py": GOOD_TEST})
    proc = _run(root, with_guard=False)
    assert proc.returncode == 0, "sanity: the synthetic session itself is green"
    assert "MANDATORY SESSION GUARD" not in proc.stdout
    # In the REAL tree the same situation is caught by a mandatory node of its own:
    src = (REPO_ROOT / "tests" / "test_i28y_mandatory_session.py").read_text(encoding="utf-8")
    assert "hasplugin(guard.PLUGIN_NAME)" in src
    reg = json.loads(REGISTRY.read_text(encoding="utf-8"))
    assert any(n["node_id"].endswith("::test_the_session_guard_is_active_in_this_session")
               for n in reg["mandatory_nodes"]), (
        "the anti-circularity node must itself be mandatory, or removing the guard is silent")


def test_s02_an_empty_registry_is_refused_not_treated_as_satisfied(tmp_path):
    root = _sandbox(tmp_path, files={"tests/test_protected.py": GOOD_TEST}, nodes=[])
    proc = _run(root)
    assert proc.returncode != 0
    assert "declares no mandatory nodes" in proc.stdout + proc.stderr


def test_s03_a_missing_registry_is_refused(tmp_path):
    root = _sandbox(tmp_path, files={"tests/test_protected.py": GOOD_TEST})
    # deliberately OUTSIDE tests/fixtures: a non-existent path that LOOKS like a fixture
    # would be read by the package-coherence fixture-reference check as a broken reference.
    proc = _run(root, registry=tmp_path / "no-such-registry.json")
    assert proc.returncode != 0
    assert "registry is missing" in proc.stdout + proc.stderr


def test_s04_a_stale_node_registry_is_caught(tmp_path):
    """A registry naming yesterday's node ids must fail, not silently match nothing."""
    root = _sandbox(tmp_path, files={"tests/test_protected.py": GOOD_TEST}, nodes=[{
        "node_id": "tests/test_protected.py::test_renamed_last_week",
        "protected_invariant": "stale", "owning_layer": "SANDBOX", "required_phase": "call",
        "acceptable_outcome": "passed", "prohibited_outcomes": ["skipped", "deselected"],
        "why_mandatory": "sandbox", "proving_bypass_mutation": "sandbox", "category": "SANDBOX"}])
    proc = _run(root)
    assert _guard_rejected(proc)
    assert "NOT COLLECTED" in proc.stdout


def test_s05_a_registry_entry_missing_required_fields_is_refused(tmp_path):
    root = _sandbox(tmp_path, files={"tests/test_protected.py": GOOD_TEST},
                    nodes=[{"node_id": "tests/test_protected.py::test_protected"}])
    proc = _run(root)
    assert proc.returncode != 0
    assert "missing the required field" in proc.stdout + proc.stderr


def test_s06_duplicate_mandatory_node_ids_are_refused(tmp_path):
    entry = {"node_id": "tests/test_protected.py::test_protected",
             "protected_invariant": "x", "owning_layer": "SANDBOX", "required_phase": "call",
             "acceptable_outcome": "passed", "prohibited_outcomes": ["skipped"],
             "why_mandatory": "s", "proving_bypass_mutation": "s", "category": "SANDBOX"}
    root = _sandbox(tmp_path, files={"tests/test_protected.py": GOOD_TEST},
                    nodes=[entry, dict(entry)])
    proc = _run(root)
    assert proc.returncode != 0
    assert "duplicate mandatory node id" in proc.stdout + proc.stderr


def test_s07_the_guard_does_not_derive_its_requirement_from_the_collection(tmp_path):
    """A derived registry would ratify a session that runs nothing. Prove it is authored."""
    src = (REPO_ROOT / "scripts" / "pytest_session_guard.py").read_text(encoding="utf-8")
    assert "mandatory_nodes" in src
    # a session that collects NOTHING relevant must still fail against the authored registry
    root = _sandbox(tmp_path, files={"tests/test_other.py": "def test_o():\n    assert True\n"})
    proc = _run(root)
    assert _guard_rejected(proc), "an authored requirement must survive an empty collection"


def test_s08_self_reported_execution_is_not_accepted(tmp_path):
    """A test claiming it ran proves nothing; the guard uses the session's own reports."""
    root = _sandbox(tmp_path, files={
        "tests/test_protected.py": "def test_protected():\n"
                                   "    print('I definitely ran and passed')\n"
                                   "    import pytest\n    pytest.skip('but actually not')\n"})
    proc = _run(root)
    assert _guard_rejected(proc)
    assert "SKIPPED" in proc.stdout


def test_s09_the_guard_is_not_inside_a_protected_assertion_module():
    """Placement is the whole point: I28X's pin died with the file it protected."""
    assert (REPO_ROOT / "scripts" / "pytest_session_guard.py").is_file()
    for protected in ("tests/test_i28u_assertion_self_protection.py",
                      "tests/test_i28w_assertion_reachability.py"):
        text = (REPO_ROOT / protected).read_text(encoding="utf-8")
        assert "def pytest_sessionfinish" not in text, (
            f"{protected}: the session guard must not live inside a file it protects")


def test_s10_forcing_guard_success_or_swallowing_its_failure_is_visible():
    """MV-style propagation: the guard must fail the session, not just print."""
    src = (REPO_ROOT / "scripts" / "pytest_session_guard.py").read_text(encoding="utf-8")
    assert "session.exitstatus" in src, (
        "the guard must change the session exit status; printing a warning is not blocking")
    assert 'if not result["clean"]' in src


def test_s11_no_historical_verdict_is_consulted():
    src = (REPO_ROOT / "scripts" / "pytest_session_guard.py").read_text(encoding="utf-8")
    for forbidden in (".signalnest/generated", "adversarial-review", "4n-i28q",
                      "historical_verdict", "reviewer-review", "verdict"):
        assert forbidden not in src, f"the guard must not consult {forbidden!r}"


# ============================================================== command + configuration contracts
def test_the_graded_command_requires_the_guard_and_rejects_selection_flags():
    spec = cim.contract()["graded_steps"]["policy_tests"]
    assert spec.get("required_options"), "the graded command must require the guard plugin"
    assert any("pytest_session_guard" in " ".join(opt) for opt in spec["required_options"])
    forbidden = spec.get("forbidden_options") or []
    for flag in ("--deselect", "-k", "-m", "--ignore", "--ignore-glob", "--collect-only",
                 "--confcutdir", "--pyargs"):
        assert flag in forbidden, f"{flag} must be forbidden on the graded pytest command"


@pytest.mark.parametrize("mutation,why", [
    (" --deselect tests/test_i28w_assertion_reachability.py::test_the_meta_contract_holds",
     "I28X bypass C"),
    (" -k 'not meta_contract'", "keyword exclusion"),
    (" -m 'not slow'", "marker exclusion"),
    (" --ignore tests/test_i28w_assertion_reachability.py", "ignore"),
    (" --collect-only", "collection only, nothing executes"),
])
def test_selection_flags_on_the_graded_command_are_rejected(mutation, why):
    text = WORKFLOW.read_text(encoding="utf-8")
    original = "python -m pytest tests/ -q -p signalnest_bootstrap -p pytest_session_guard"
    assert original in text, "the graded invocation moved; this pin must be updated with it"
    mutated = text.replace(original, original + mutation, 1)
    assert mutated != text
    problems = [p for p in cim.check(mutated)["problems"] if "policy_tests" in p]
    assert problems, f"{why} was not rejected by the graded command contract"


def test_removing_the_guard_plugin_from_the_graded_command_is_rejected():
    text = WORKFLOW.read_text(encoding="utf-8")
    mutated = text.replace(" -p pytest_session_guard", "", 1)
    assert mutated != text
    problems = [p for p in cim.check(mutated)["problems"] if "policy_tests" in p]
    assert problems, "removing the session guard from the graded command must be a policy failure"


def test_the_pytest_configuration_contract_is_clean_and_catches_collection_removers(tmp_path):
    result = cfg.check()
    assert result["clean"], "\n  ".join(result["problems"])
    baseline = json.loads((REPO_ROOT / "tests" / "fixtures"
                           / "pytest-configuration-baseline.json").read_text(encoding="utf-8"))
    approved = dict(baseline["approved_sources"])
    # an unapproved change to tests/conftest.py must be caught by the hash
    approved["tests/conftest.py"] = "0" * 64
    dirty = cfg.check(baseline={"approved_sources": approved})
    assert not dirty["clean"]
    assert any("content changed" in p for p in dirty["problems"])


def test_d05_a_narrowed_positional_path_that_omits_the_file_is_caught(tmp_path):
    """Running a SUBSET of the tree is the quietest bypass of all: nothing is skipped, nothing
    is deselected, every collected test passes, and the control was simply never asked to run."""
    root = _sandbox(tmp_path, files={
        "tests/test_protected.py": GOOD_TEST,
        "tests/test_other.py": "def test_o():\n    assert True\n"})
    proc = _run(root, paths=("tests/test_other.py",))
    assert _guard_rejected(proc), proc.stdout[-2000:]
    assert "NOT COLLECTED" in proc.stdout
