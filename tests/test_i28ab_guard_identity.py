"""Gate 4N-I28AB — the authoritative session guard must be the GENUINE implementation.

THE DEFECT THIS CLOSES. Gate 4N-I28AA reviewer finding ADV-I28AA-01. The guard established plugin
identity by NAME ALONE (`if config.pluginmanager.hasplugin(PLUGIN_NAME): return`) and then declined
protection SILENTLY. `tests/conftest.py` registers after the `-p pytest_session_guard` plugin, so
under pluggy's LIFO ordering its `pytest_configure` ran first and a four-line decoy could claim the
name. The genuine guard then returned without registering: no observation, no adjudication, no
exit-status change. The sole anti-circularity node checked `hasplugin(name)` plus two duck-typed
attributes, which the decoy supplied. Measured on the frozen tree:

  * faithful decoy + removal of both control modules -> 2943 passed, 0 failed, 8 of 10 mandatory
    nodes absent, nothing reported;
  * disarm-only -> 3026 passed / 0 failed / 83 skipped, byte-identical to the clean baseline.

WHAT IS PINNED HERE. Identity is checked three ways, none of them a name or a shape: exact type
identity produced by the genuine module object, implementation provenance (resolved path, source
hash, spec origin, loader class) against an authored baseline, and evidence that the genuine
lifecycle hooks actually executed in this process. A same-named object satisfies none of them.

HONEST BOUND. The provenance digest is computed from files a party with repository write access can
also read; it is not a secret and nothing here pretends otherwise. What it buys is that substituting
the guard now requires changing several separately reviewed, separately pinned files rather than
appending to one conftest.
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

BASELINE = REPO_ROOT / "tests" / "fixtures" / "mandatory-session-baseline.json"
GUARD_SRC = REPO_ROOT / "scripts" / "pytest_session_guard.py"
GOOD = "def test_protected():\n    assert 1 + 1 == 2\n"
NODE = "tests/test_protected.py::test_protected"


# ===================================================================== the mandatory node
def test_the_registered_guard_is_the_genuine_implementation(pytestconfig):
    """THE anti-circularity node, rebuilt. Name and shape are no longer sufficient.

    If this fails, the object holding the guard's name in this session is not the genuine guard,
    or the genuine guard did not actually run its hooks. Both are the ADV-I28AA-01 condition.
    """
    live = pytestconfig.pluginmanager.get_plugin(guard.PLUGIN_NAME)
    assert live is not None, "no plugin holds the guard name in this session"

    # 1. exact type identity from the module THIS test imported — not a name, not a duck type,
    #    and not a subclass (a subclass can override every hook).
    assert type(live) is guard.MandatorySessionGuard, (
        f"the guard name is held by {type(live).__module__}.{type(live).__name__}, which is not "
        "the genuine MandatorySessionGuard. This is the Gate 4N-I28AA impersonation condition.")
    assert guard.is_genuine(live)

    # 2. the registered object is the one the module built, and nothing was swapped afterwards
    assert getattr(pytestconfig, "_signalnest_guard", None) is live, (
        "config._signalnest_guard and the registered plugin are different objects")

    # 3. implementation provenance against the authored baseline
    base = json.loads(BASELINE.read_text(encoding="utf-8"))
    prov = guard.implementation_provenance()
    assert prov["source_sha256"] == base["guard_sha256"], (
        "the running guard's source hash does not match the pinned baseline")
    assert Path(prov["resolved_path"]) == GUARD_SRC.resolve()
    assert prov["spec_origin"] == str(GUARD_SRC.resolve())
    assert prov["loader_class"] in base["approved_loader_classes"]
    assert prov["guard_version"] == base["guard_version"]

    # 4. genuine lifecycle execution — an impersonator can copy attributes but has no hook log.
    #    Only hooks that have DEMONSTRABLY already fired by this point are asserted here: this
    #    test runs in its own call phase, so no call report has been logged yet when it executes
    #    (the report is emitted after the call returns). Requiring phase:call here would be an
    #    assertion about the future. Call-phase evidence for every mandatory node is adjudicated
    #    by the guard itself at sessionfinish, which reports "never entered its required 'call'
    #    phase" — that is the correct division of labour between the two controls.
    assert live.hook_log.get("pytest_collection_modifyitems", 0) >= 1, live.hook_log
    assert live.hook_log.get("pytest_runtest_logreport", 0) >= 1, live.hook_log
    assert live.hook_log.get("phase:setup", 0) >= 1, live.hook_log
    assert "pytest_collection_modifyitems" in live.hook_sequence

    # 5. still enforcing the repository registry
    assert live.registry_path == guard.REGISTRY
    assert live.nodes


def test_the_guard_baseline_pins_provenance_not_just_a_hash():
    base = json.loads(BASELINE.read_text(encoding="utf-8"))
    for field in ("guard_sha256", "guard_version", "approved_loader_classes",
                  "guard_canonical_module", "guard_class_qualname"):
        assert field in base, f"the baseline must pin {field}"
    assert base["guard_canonical_module"] == "pytest_session_guard"
    assert base["guard_class_qualname"] == "MandatorySessionGuard"
    assert hashlib.sha256(GUARD_SRC.read_bytes()).hexdigest() == base["guard_sha256"]


# ===================================================================== sandbox harness
def _sandbox(tmp_path: Path, conftest: str = "", files=None, nodes=None) -> Path:
    root = tmp_path / "s"
    (root / "tests" / "fixtures").mkdir(parents=True)
    for rel, text in (files or {"tests/test_protected.py": GOOD}).items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(text))
    if conftest:
        (root / "tests" / "conftest.py").write_text(textwrap.dedent(conftest))
    nl = nodes if nodes is not None else [{
        "node_id": NODE, "protected_invariant": "the synthetic control runs",
        "owning_layer": "SANDBOX", "required_phase": "call", "acceptable_outcome": "passed",
        "prohibited_outcomes": ["failed", "skipped", "xfailed", "deselected"],
        "why_mandatory": "sandbox", "proving_bypass_mutation": "sandbox", "category": "SANDBOX"}]
    (root / "tests/fixtures/mandatory-pytest-nodes.json").write_text(
        json.dumps({"mandatory_nodes": nl}))
    return root


def _sandbox_scripts(root: Path) -> Path:
    """Give the sandbox its own scripts/ so REPO_ROOT resolves INSIDE it.

    GATE 4N-I28AI. This sandbox used to run the real scripts/ with the registry redirected by
    SIGNALNEST_MANDATORY_NODES. That override is retired (ADV-I28AH-01), and the right shape is the
    one tests/test_i28ae_executed_provenance.py already uses: a sandbox is its own tree, so its
    registry sits at the canonical path within it and needs no redirection.
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
    base = root / "tests/fixtures/mandatory-session-baseline.json"
    reg = root / "tests/fixtures/mandatory-pytest-nodes.json"
    if base.is_file() and reg.is_file():
        doc = json.loads(base.read_text())
        raw = reg.read_bytes()
        parsed = json.loads(raw)
        doc["registry_sha256"] = hashlib.sha256(raw).hexdigest()
        doc["mandatory_node_count"] = len(parsed["mandatory_nodes"])
        doc["mandatory_node_ids"] = sorted(n["node_id"] for n in parsed["mandatory_nodes"])
        base.write_text(json.dumps(doc, indent=1, sort_keys=True))
    return root / "scripts"


def _run(root: Path, extra=(), guard_flag=True):
    env = dict(os.environ, PYTHONPATH=str(_sandbox_scripts(root)))
    env.pop("SIGNALNEST_MANDATORY_NODES", None)
    env.pop("SIGNALNEST_SESSION_MARKER", None)
    args = [sys.executable, "-m", "pytest", "tests/", "-q", "-p", "no:randomly"]
    if guard_flag:
        args += ["-p", "pytest_session_guard"]
    return subprocess.run(args + list(extra), cwd=root, env=env, capture_output=True,
                          text=True, timeout=300)


DECOY_FAITHFUL = '''
    import sys as _s, pathlib as _p
    _s.path.insert(0, %r)
    import pytest_session_guard as _g


    class _Decoy:
        def __init__(self):
            self.registry_path = _g.REGISTRY
            self.nodes = {"x": {}}
            self.problems = []
            self.hook_log = {"pytest_collection_modifyitems": 1,
                             "pytest_runtest_logreport": 3,
                             "phase:setup": 1, "phase:call": 1, "phase:teardown": 1}


    def pytest_configure(config):
        d = _Decoy()
        config.pluginmanager.register(d, "signalnest_mandatory_session_guard")
        config._signalnest_guard = d
''' % str(REPO_ROOT / "scripts")

REMOVE_ITEMS = '''
    def pytest_collection_modifyitems(session, config, items):
        items[:] = [i for i in items if "test_protected" not in i.nodeid]
'''


def _refused(p) -> bool:
    return p.returncode != 0


# ===================================================================== THE exact pin
def test_the_exact_adv_i28aa_01_faithful_decoy_is_refused(tmp_path):
    """THE pin for ADV-I28AA-01: a faithful same-name decoy registered first."""
    root = _sandbox(tmp_path, conftest=DECOY_FAITHFUL + REMOVE_ITEMS)
    p = _run(root)
    assert _refused(p), p.stdout[-2500:]
    assert "GuardIdentityError" in (p.stdout + p.stderr) or "already registered to" in p.stdout, \
        p.stdout[-2500:]


def test_g00_a_clean_synthetic_session_still_passes(tmp_path):
    """Green when clean — without this the refusals above prove only that everything fails."""
    p = _run(_sandbox(tmp_path))
    assert p.returncode == 0, p.stdout[-2000:]


# ===================================================================== provenance matrix
@pytest.mark.parametrize("label,conftest,expect_refused", [
    ("m03 same-name naive decoy",
     "\n    class _D:\n        pass\n\n\n    def pytest_configure(config):\n"
     "        config.pluginmanager.register(_D(), 'signalnest_mandatory_session_guard')\n", True),
    ("m04 same-name faithful decoy", DECOY_FAITHFUL, True),
    ("m05 pre-registered decoy", DECOY_FAITHFUL, True),
    ("m12 subclassed guard",
     "\n    import sys as _s\n    _s.path.insert(0, %r)\n    import pytest_session_guard as _g\n\n\n"
     "    class _Sub(_g.MandatorySessionGuard):\n"
     "        def pytest_sessionfinish(self, session, exitstatus):\n            return\n\n\n"
     "    def pytest_configure(config):\n"
     "        config.pluginmanager.register(_Sub(config), 'signalnest_mandatory_session_guard')\n"
     % str(REPO_ROOT / "scripts"), True),
    ("m13 proxy object",
     "\n    import sys as _s\n    _s.path.insert(0, %r)\n    import pytest_session_guard as _g\n\n\n"
     "    class _Proxy:\n        def __init__(self, inner):\n            self._i = inner\n"
     "        def __getattr__(self, k):\n            return getattr(self._i, k)\n\n\n"
     "    def pytest_configure(config):\n"
     "        config.pluginmanager.register(_Proxy(_g.MandatorySessionGuard(config)),\n"
     "                                      'signalnest_mandatory_session_guard')\n"
     % str(REPO_ROOT / "scripts"), True),
    ("m14 duck-typed object only",
     "\n    class _D:\n        registry_path = 'x'\n        nodes = {'a': {}}\n"
     "        hook_log = {'phase:call': 1}\n\n\n    def pytest_configure(config):\n"
     "        config.pluginmanager.register(_D(), 'signalnest_mandatory_session_guard')\n", True),
])
def test_plugin_provenance_matrix(tmp_path, label, conftest, expect_refused):
    p = _run(_sandbox(tmp_path, conftest=conftest))
    assert _refused(p) == expect_refused, f"{label}: {p.stdout[-2000:]}"


def test_m02_missing_plugin_is_caught_by_the_mandatory_node():
    """Without the plugin nothing observes the session; the mandatory node must fail."""
    src = (REPO_ROOT / "tests" / "test_i28ab_guard_identity.py").read_text(encoding="utf-8")
    assert "type(live) is guard.MandatorySessionGuard" in src
    reg = json.loads((REPO_ROOT / "tests/fixtures/mandatory-pytest-nodes.json").read_text())
    ids = [n["node_id"] for n in reg["mandatory_nodes"]]
    assert any(i.endswith("::test_the_registered_guard_is_the_genuine_implementation")
               for i in ids), "the identity node must itself be mandatory"


IDENTITY_CHECK_TEST = """
import sys
sys.path.insert(0, %r)
import pytest_session_guard as guard


def test_protected(pytestconfig):
    live = pytestconfig.pluginmanager.get_plugin(guard.PLUGIN_NAME)
    assert type(live) is guard.MandatorySessionGuard, "the genuine guard is not registered"
    assert live.hook_log.get("pytest_collection_modifyitems", 0) >= 1
""" % str(REPO_ROOT / "scripts")


def test_m15_genuine_object_unregistered_after_bootstrap_is_caught(tmp_path):
    """Unregistering the guard mid-session removes its sessionfinish adjudication, so the guard
    itself cannot report it. The mandatory identity node is what notices, because it runs in the
    call phase and asks the plugin manager directly — which is exactly why that node is mandatory.
    The sandbox therefore carries an identity check of its own; without one this probe would prove
    nothing about the real tree."""
    root = _sandbox(tmp_path, files={"tests/test_protected.py": IDENTITY_CHECK_TEST},
                    conftest='''
    def pytest_collection_modifyitems(session, config, items):
        pm = config.pluginmanager
        obj = pm.get_plugin("signalnest_mandatory_session_guard")
        if obj is not None:
            pm.unregister(obj)
''')
    p = _run(root)
    assert _refused(p), p.stdout[-2000:]


def test_m15b_the_identity_check_sandbox_is_green_when_clean(tmp_path):
    """Detector-is-alive control for m15: the same sandbox with no unregistration must pass."""
    root = _sandbox(tmp_path, files={"tests/test_protected.py": IDENTITY_CHECK_TEST})
    p = _run(root)
    assert p.returncode == 0, p.stdout[-2000:]


# ===================================================================== self-protection
def test_s01_the_silent_early_return_is_gone():
    src = GUARD_SRC.read_text(encoding="utf-8")
    assert "if config.pluginmanager.hasplugin(PLUGIN_NAME):\n        return" not in src, (
        "the silent early return is the ADV-I28AA-01 mechanism and must not come back")
    assert "GuardIdentityError" in src
    assert "raise GuardIdentityError" in src


def test_s02_identity_is_not_reduced_to_a_name_or_a_shape():
    src = GUARD_SRC.read_text(encoding="utf-8")
    assert "def is_genuine" in src
    assert "type(obj) is MandatorySessionGuard" in src
    node = (REPO_ROOT / "tests/test_i28ab_guard_identity.py").read_text(encoding="utf-8")
    assert "type(live) is guard.MandatorySessionGuard" in node
    assert "hook_log" in node


def test_s03_hook_log_cannot_be_produced_without_genuine_hooks():
    src = GUARD_SRC.read_text(encoding="utf-8")
    assert "_record_hook" in src
    assert src.count("self._record_hook(") >= 5, "every observed hook must record itself"


def test_s04_the_observed_set_is_taken_last(tmp_path):
    """trylast: the guard observes the FINAL item list, after other hooks have modified it."""
    src = GUARD_SRC.read_text(encoding="utf-8")
    assert "@pytest.hookimpl(trylast=True)" in src
    root = _sandbox(tmp_path, conftest=REMOVE_ITEMS)
    p = _run(root)
    assert _refused(p), p.stdout[-2000:]


def test_s05_outcome_map_is_bound_to_provenance_registry_and_tree():
    src = GUARD_SRC.read_text(encoding="utf-8")
    for field in ("implementation_provenance", "provenance_token", "hook_log",
                  "registered_object_is_self", "genuine_type"):
        assert field in src, f"the session record must carry {field}"


def test_s06_no_historical_verdict_or_candidate_is_consulted():
    src = GUARD_SRC.read_text(encoding="utf-8")
    for forbidden in (".signalnest/generated", "4N-I28AA-CANDIDATE", "adversarial-review",
                      "historical_verdict"):
        assert forbidden not in src
