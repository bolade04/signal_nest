"""Executed-state provenance and startup-policy controls (Gate 4N-I28AG).

WHAT THESE TESTS DEFEND. Gate 4N-I28AF proved that binding executed CODE is necessary and not
sufficient. Exec'ing the real pinned bytes of `pytest_session_guard` — so every code object is
byte-identical — and then rebinding a single module-level global, REGISTRY, cut the guard's mandatory
requirement from twelve nodes to one. Executed-code provenance reported clean for all four protected
modules and the graded session exited 0 with eleven of twelve assurance nodes absent
(ADV-I28AF-01). Separately, the interpreter startup surface was recorded and never adjudicated, so a
staged `sitecustomize.py` was accepted while every executed module stayed byte-identical
(ADV-I28AF-02).

THE INDEPENDENT ORACLE. `oracle_*` below derive expected facts WITHOUT importing the production
classifier, the production policy constants, the production expected-state inventory, or the
production verifier. They use documented external semantics only: CPython's `compile`, the code
object attributes, `hashlib`, `ast`, documented interpreter startup behaviour, and subprocess
execution. Where the oracle and production disagree, the disagreement is preserved and diagnosed
rather than tuned away — Gate 4N-I28AE's oracle was wrong about `dis` argrepr, and the rule since
then is that the oracle is a suspect too.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import executed_state_provenance as esp   # noqa: E402
import startup_policy as sp               # noqa: E402
import signalnest_bootstrap as boot       # noqa: E402

STATE_CONTRACT = REPO_ROOT / "tests" / "fixtures" / "executed-state-contract.json"
STARTUP_POLICY = REPO_ROOT / "tests" / "fixtures" / "startup-policy.json"
PROTECTED_SET = REPO_ROOT / "tests" / "fixtures" / "protected-module-set.json"


def _protected():
    return json.loads(PROTECTED_SET.read_text(encoding="utf-8"))["protected_modules"]



def _critical(module_name: str) -> list:
    """The real authored critical callables. Passing a partial list makes REGISTRY unreachable and
    turns a genuine control into a test that fails for the wrong reason."""
    return next(e for e in _protected()
                if e["module"] == module_name).get("critical_callables") or []


def _import_all():
    for entry in _protected():
        __import__(entry["module"])


@pytest.fixture(autouse=True)
def _all_protected_modules_resident():
    """Both controls fail closed on a non-resident protected module, which is correct but would
    make every test here fail for a reason unrelated to what it probes."""
    _import_all()


# ===================================================================== the independent oracle
def oracle_reachable_names(module_path: Path, critical: list) -> set:
    """Module-level names the critical callables can read, derived from SOURCE via ast.

    Deliberately a different mechanism from production, which walks co_names on compiled code
    objects. Two implementations that share a traversal share its blind spots.
    """
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    module_level: set = set()
    functions: dict = {}

    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            module_level |= {t.id for t in targets if isinstance(t, ast.Name)}
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            module_level.add(node.name)
            functions[node.name] = node
        elif isinstance(node, ast.ClassDef):
            module_level.add(node.name)
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    functions[f"{node.name}.{sub.name}"] = sub
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            module_level |= {(a.asname or a.name).split(".")[0] for a in node.names}

    # Methods are indexed under their bare name as well, so a `self.helper()` call can be
    # followed. Without this the oracle silently under-approximates: `evaluate` reaches
    # SELECTION_OPTIONS only through `self._selection_options()`, which is an ast.Attribute on
    # `self` and not an ast.Name, so a Name-only walk never gets there. An oracle that reaches
    # less than production cannot check production.
    by_bare_name = {}
    for qual, node in functions.items():
        by_bare_name.setdefault(qual.rsplit(".", 1)[-1], node)

    reached: set = set()
    frontier = [q for q in critical if q in functions]
    # GATE 4N-I28AM. A critical callable that is defined at module level is itself part of the
    # state its own execution depends on. The oracle reaches this from ITS OWN AST — `module_level`
    # is built above from the source, and `critical` is the protected set's declaration — not by
    # consulting production. The rule is the same rule; the derivation stays independent.
    # Without it the oracle under-approximates the roots of its own walk, and an oracle that
    # reaches less than production cannot check production.
    reached |= {q for q in critical if "." not in q and q in module_level}
    seen: set = set()
    while frontier:
        qual = frontier.pop()
        if qual in seen or qual not in functions:
            continue
        seen.add(qual)
        for sub in ast.walk(functions[qual]):
            if isinstance(sub, ast.Name) and sub.id in module_level:
                reached.add(sub.id)
                if sub.id in functions:
                    frontier.append(sub.id)
                # Reaching a CLASS means any of its methods can run, so the state those methods
                # read is reachable too. Production expands classes this way and it is the safe
                # direction — MandatorySessionGuard.__init__ calls load_registry, which is how
                # GuardError and the registry become reachable from pytest_configure.
                for qual in [q for q in list(functions) if q.startswith(f"{sub.id}.")]:
                    frontier.append(qual)
            elif isinstance(sub, ast.Attribute):
                if isinstance(sub.value, ast.Name) and sub.value.id in module_level:
                    reached.add(sub.value.id)
                # self.<method>() / cls.<method>(): follow into the method body.
                if isinstance(sub.value, ast.Name) and sub.value.id in ("self", "cls") \
                        and sub.attr in by_bare_name:
                    functions.setdefault(sub.attr, by_bare_name[sub.attr])
                    frontier.append(sub.attr)
    # GATE 4N-I28AO. FUNCTION-LOCAL imports are state the critical callables read, and they are
    # reached TRANSITIVELY: a helper a critical callable calls may do the import. The oracle
    # therefore derives them from every function its own walk reached (`seen`), using its own AST,
    # not by consulting production. Same rule, independent derivation.
    for qual in sorted(seen):
        node = functions.get(qual)
        if node is None:
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.Import):
                for alias in sub.names:
                    target = (alias.name or "").split(".")[0]
                    reached.add(f"LOCALIMPORT:{target}")
                    local = alias.asname or target
                    for attr in ast.walk(node):
                        if isinstance(attr, ast.Attribute) and isinstance(attr.value, ast.Name) \
                                and attr.value.id == local:
                            reached.add(f"LOCALCALLABLE:{target}.{attr.attr}")
            elif isinstance(sub, ast.ImportFrom) and sub.module:
                target = sub.module.split(".")[0]
                reached.add(f"LOCALIMPORT:{target}")
                for alias in sub.names:
                    reached.add(f"LOCALCALLABLE:{target}.{alias.name}")
    return reached


def oracle_file_digest(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def oracle_code_bytes(module_path: Path, qualname: str) -> bytes:
    """The raw bytecode of one function, straight from a plain compile of the file."""
    def walk(code):
        for const in code.co_consts:
            if hasattr(const, "co_code"):
                if const.co_qualname == qualname:
                    return const
                found = walk(const)
                if found is not None:
                    return found
        return None
    top = compile(module_path.read_bytes(), str(module_path), "exec")
    found = walk(top)
    assert found is not None, f"{qualname} not found in a plain compile of {module_path}"
    return found.co_code


# ===================================================================== contract shape
def test_the_state_contract_is_authored_and_non_vacuous():
    doc = json.loads(STATE_CONTRACT.read_text(encoding="utf-8"))
    assert doc["_authority"].startswith("AUTHORED"), "the contract must declare its authority"
    assert doc["modules"], "an empty contract would verify vacuously"
    for name, entry in doc["modules"].items():
        assert entry["names"], f"{name}: pinning no name would verify vacuously"
        assert entry["why_load_bearing"], f"{name}: must say why this state matters"
        assert entry["name_count"] == len(entry["names"]), f"{name}: count disagrees with names"


def test_every_protected_module_is_covered_by_the_state_contract():
    """A protected module with no state pin is bound for its code and unbound for its behaviour."""
    doc = json.loads(STATE_CONTRACT.read_text(encoding="utf-8"))
    missing = sorted({e["module"] for e in _protected()} - set(doc["modules"]))
    assert not missing, f"protected modules with no executed-state pin: {missing}"


def test_the_controls_protect_themselves():
    """Both new controls must themselves be provenance-bound, or a substituted classifier
    approves anything."""
    protected = {e["module"] for e in _protected()}
    assert "executed_state_provenance" in protected
    assert "startup_policy" in protected


# ===================================================================== oracle agreement
@pytest.mark.parametrize("module_name", sorted(e["module"] for e in _protected()))
def test_the_oracle_independently_agrees_on_the_reachable_name_set(module_name):
    """Production walks co_names on code objects; the oracle walks the AST. They must agree.

    The oracle is allowed to over-approximate — an ast.Name mentioned in a nested scope that the
    compiler resolves elsewhere — so the binding assertion is that production finds nothing the
    oracle did not, i.e. production never MISSES load-bearing state the source shows is reachable.
    """
    entry = next(e for e in _protected() if e["module"] == module_name)
    module = sys.modules[module_name]
    production, problems = esp.state_identity(module, entry.get("critical_callables") or [])
    assert not problems, problems
    expected = oracle_reachable_names(REPO_ROOT / entry["relative_path"],
                                      entry.get("critical_callables") or [])
    unseen = sorted(set(production) - expected)
    assert not unseen, (
        f"{module_name}: production bound {unseen}, which an independent AST walk of the source "
        "does not consider reachable — one of the two is wrong and neither may be tuned to match")


def test_the_oracle_independently_confirms_the_registry_file_binding():
    """The token for REGISTRY must bind the CONTENT of the file, computed independently."""
    guard = sys.modules["pytest_session_guard"]
    identity, _ = esp.state_identity(guard, _critical("pytest_session_guard"))
    token = identity["REGISTRY"]
    expected = oracle_file_digest(REPO_ROOT / "tests" / "fixtures" / "mandatory-pytest-nodes.json")
    assert token.startswith("FILE:"), f"REGISTRY must be content-bound, got {token}"
    assert expected[:32] in token, (
        "the bound digest is not the sha256 of the registry the guard actually reads")


def test_the_oracle_independently_confirms_a_code_token_changes_with_bytecode():
    """A behavioural change to a bound callable must move its token; proven against raw co_code."""
    guard = sys.modules["pytest_session_guard"]
    before_token = esp.state_token(guard.load_registry, owner_file=guard.__file__)
    before_bytes = oracle_code_bytes(REPO_ROOT / "scripts" / "pytest_session_guard.py",
                                     "load_registry")

    def replacement(path=None):
        return {"mandatory_nodes": []}
    after_token = esp.state_token(replacement, owner_file=guard.__file__)
    assert before_token != after_token
    assert replacement.__code__.co_code != before_bytes, (
        "the substitute must genuinely differ in bytecode, or this proves nothing")


# ===================================================================== state-only mutations
def _both_layers_clean() -> tuple:
    import executed_code_provenance as ecp
    return ecp.verify()["clean"], esp.verify()["clean"]


def test_g00_the_baseline_is_green_before_any_mutation():
    """Falsification discipline: a refusal only means something from a proven-green baseline."""
    code_clean, state_clean = _both_layers_clean()
    assert code_clean, "executed-code provenance is not clean at baseline"
    assert state_clean, "executed-state provenance is not clean at baseline"


def test_s01_the_exact_adv_i28af_01_registry_rebinding_is_refused():
    """THE pin for ADV-I28AF-01. One global, no code change, full bypass before this gate."""
    guard = sys.modules["pytest_session_guard"]
    original = guard.REGISTRY
    try:
        guard.REGISTRY = Path("/tmp/decoy-registry.json")
        code_clean, state_clean = _both_layers_clean()
        assert code_clean, (
            "executed CODE must still verify clean — otherwise this is not a state-only mutation "
            "and it proves nothing about state binding")
        assert not state_clean, "rebinding REGISTRY must be refused by executed-state provenance"
        problems = " ".join(esp.verify()["problems"])
        assert "REGISTRY" in problems, "the refusal must name the state input that drifted"
    finally:
        guard.REGISTRY = original
    assert all(_both_layers_clean()), "restoration must return both layers to clean"


def test_s02_a_changed_keyword_default_is_refused():
    original = boot.establish.__kwdefaults__
    try:
        boot.establish.__kwdefaults__ = {"strict": False}
        code_clean, state_clean = _both_layers_clean()
        assert code_clean, "code must be unchanged for this to be a state-only mutation"
        assert not state_clean, "a changed keyword default changes behaviour and must be refused"
    finally:
        boot.establish.__kwdefaults__ = original


def test_s03_a_swapped_module_level_helper_is_refused():
    guard = sys.modules["pytest_session_guard"]
    original = guard.load_registry
    try:
        guard.load_registry = lambda path=None: {"mandatory_nodes": []}
        code_clean, state_clean = _both_layers_clean()
        assert code_clean, "code must be unchanged for this to be a state-only mutation"
        assert not state_clean, "replacing a helper a critical callable calls must be refused"
    finally:
        guard.load_registry = original


def test_s04_a_rebound_module_binding_is_refused():
    """`os` is a name the guard reads; swapping it for a decoy changes every environment lookup."""
    guard = sys.modules["pytest_session_guard"]
    original = guard.os
    try:
        guard.os = type("Decoy", (), {"environ": {}, "sep": "/"})()
        assert not esp.verify()["clean"], "a rebound module binding must be refused"
    finally:
        guard.os = original


def test_s05_changed_state_file_content_is_refused_without_touching_any_path(tmp_path):
    """TOCTOU resistance: the token binds CONTENT, so swapping the material is caught even though
    the path a critical callable reads never changed."""
    registry = REPO_ROOT / "tests" / "fixtures" / "mandatory-pytest-nodes.json"
    guard = sys.modules["pytest_session_guard"]
    identity, _ = esp.state_identity(guard, _critical("pytest_session_guard"))
    bound = identity["REGISTRY"]

    twin = tmp_path / "mandatory-pytest-nodes.json"
    doc = json.loads(registry.read_text(encoding="utf-8"))
    doc["mandatory_nodes"] = doc["mandatory_nodes"][:1]
    twin.write_text(json.dumps(doc))
    assert esp._file_token(twin).split(":")[-1] != bound.split(":")[-1], (
        "a reduced registry must produce a different content token")


def test_s06_code_only_mutation_is_refused_by_the_code_layer():
    """The complementary direction: code changes, state does not."""
    import executed_code_provenance as ecp
    guard = sys.modules["pytest_session_guard"]
    original = guard.MandatorySessionGuard.pytest_sessionfinish
    try:
        guard.MandatorySessionGuard.pytest_sessionfinish = lambda self, session, exitstatus: None
        assert not ecp.verify()["clean"], "a replaced critical method must be refused"
    finally:
        guard.MandatorySessionGuard.pytest_sessionfinish = original


def test_s07_an_inert_mutation_does_not_move_any_binding(tmp_path):
    """Stability control. If unrelated edits moved the tokens, every drift report would be noise."""
    before = esp.verify()
    (tmp_path / "irrelevant.txt").write_text("a file that no protected module reads")
    after = esp.verify()
    assert after["clean"] and before["clean"]
    assert [r["state_digest"] for r in before["results"]] == \
           [r["state_digest"] for r in after["results"]], "an inert file moved a state digest"


# ===================================================================== fail-closed
def test_f01_a_missing_state_contract_fails_closed(tmp_path):
    with pytest.raises(esp.StateProvenanceError):
        esp.load_contract(tmp_path / "absent.json")


def test_f02_an_empty_state_contract_fails_closed(tmp_path):
    empty = tmp_path / "c.json"
    empty.write_text(json.dumps({"modules": {}}))
    with pytest.raises(esp.StateProvenanceError):
        esp.load_contract(empty)


def test_f03_a_non_resident_protected_module_fails_closed():
    doc = json.loads(STATE_CONTRACT.read_text(encoding="utf-8"))
    result = esp.verify(doc, modules={})
    assert not result["clean"]
    assert any("not resident" in p for p in result["problems"])


def test_f04_uncovered_reachable_state_fails_closed():
    """The anti-omission property: a contract that drops a name cannot pass."""
    doc = json.loads(STATE_CONTRACT.read_text(encoding="utf-8"))
    doc["modules"]["pytest_session_guard"]["names"].pop("REGISTRY")
    result = esp.verify(doc)
    assert not result["clean"]
    assert any("REGISTRY" in p and "NOT covered" in p for p in result["problems"]), (
        "dropping a load-bearing name from the contract must fail, not silently narrow the pin")


def test_f05_a_pin_that_binds_nothing_fails_closed():
    doc = json.loads(STATE_CONTRACT.read_text(encoding="utf-8"))
    doc["modules"]["pytest_session_guard"]["names"]["NOT_A_REAL_NAME"] = "VALUE:1"
    result = esp.verify(doc)
    assert not result["clean"]
    assert any("no longer reach" in p for p in result["problems"])


def test_f06_an_ambiguous_state_source_fails_closed(monkeypatch):
    """SIGNALNEST_MANDATORY_NODES redirects the guard's registry without touching code or a
    module attribute — a second possible source for one authority."""
    monkeypatch.setenv("SIGNALNEST_MANDATORY_NODES", "/tmp/other-registry.json")
    result = esp.verify()
    assert not result["clean"]
    assert any("SIGNALNEST_MANDATORY_NODES" in p for p in result["problems"])


def test_f06b_an_in_tree_override_is_now_also_refused(monkeypatch):
    """RETIRED SEMANTICS, replaced at Gate 4N-I28AI rather than deleted.

    This test used to assert that an IN-TREE override was permitted and merely recorded — the
    MUST_RESOLVE_IN_TREE rule. Gate 4N-I28AH proved that rule wrong: an in-tree decoy at
    docs/notes.json cut the enforced mandatory set from twelve nodes to one while every layer
    reported clean, because being inside the repository is not authorization. The override is now
    prohibited outright, and this test pins the new behaviour so the old rule cannot quietly return.
    """
    monkeypatch.setenv("SIGNALNEST_MANDATORY_NODES",
                       str(REPO_ROOT / "tests" / "fixtures" / "mandatory-pytest-nodes.json"))
    result = esp.verify()
    assert not result["clean"], "an override must be refused whatever it points at"
    assert any("SIGNALNEST_MANDATORY_NODES" in p for p in result["problems"])


def test_f07_the_contract_cannot_bind_its_own_content():
    """The self-reference trap, made explicit instead of producing a phantom drift.

    Writing a file's own hash into it changes the hash. This chain hit that ordering defect at
    I28Q, at I28AE, and again while authoring this contract. The contract names its self-reference
    and the digest is pinned here instead."""
    doc = json.loads(STATE_CONTRACT.read_text(encoding="utf-8"))
    token = doc["modules"]["executed_state_provenance"]["names"]["STATE_CONTRACT"]
    assert token.startswith("SELFREF:"), (
        "the contract must not claim to bind its own content; that pin is unsatisfiable")


# ===================================================================== startup policy
def test_p01_the_policy_classifies_every_mechanism_with_a_valid_disposition():
    doc = sp.load_policy()
    for name, entry in doc["mechanisms"].items():
        assert entry["disposition"] in sp.DISPOSITIONS, name


def test_p02_the_startup_surface_is_adjudicated_not_merely_recorded():
    """ADV-I28AF-02: the I28AE surface was evidence with no verdict."""
    result = sp.check()
    assert "problems" in result and "clean" in result, "check() must return a verdict"
    assert result["clean"], f"the honest current surface must be policy-clean: {result['problems']}"


def test_p03_an_unclassified_mechanism_fails_closed():
    doc = sp.load_policy()
    del doc["mechanisms"]["sitecustomize"]
    result = sp.check(doc)
    assert not result["clean"]
    assert any("NO policy classification" in p for p in result["problems"])


def test_p04_a_prohibited_sitecustomize_is_refused():
    doc = sp.load_policy()
    surface = sp.observe()
    surface["sitecustomize"] = {"resident": True,
                                "origin": str(REPO_ROOT / "scripts" / "sitecustomize.py")}
    result = sp.check(doc, surface=surface)
    assert not result["clean"]
    assert any("PROHIBITED" in p and "sitecustomize" in p for p in result["problems"])


def test_p05_a_prohibited_usercustomize_is_refused():
    surface = sp.observe()
    surface["usercustomize"] = {"resident": True, "origin": str(REPO_ROOT / "usercustomize.py")}
    result = sp.check(sp.load_policy(), surface=surface)
    assert not result["clean"]
    assert any("usercustomize" in p for p in result["problems"])


def test_p06_pth_startup_execution_is_refused():
    surface = sp.observe()
    surface["pth_entries"] = ["/somewhere/evil.pth"]
    result = sp.check(sp.load_policy(), surface=surface)
    assert not result["clean"]
    assert any("pth_files" in p for p in result["problems"])


@pytest.mark.parametrize("variable", ["BASH_ENV", "ENV", "PYTEST_ADDOPTS", "PYTEST_PLUGINS",
                                      "PYTHONHOME"])
def test_p07_prohibited_environment_startup_hooks_are_refused(variable):
    surface = sp.observe()
    surface[variable] = "/tmp/injected"
    result = sp.check(sp.load_policy(), surface=surface)
    assert not result["clean"], f"{variable} must be refused when set"
    assert any(variable in p for p in result["problems"])


def test_p08_pythonpath_injection_outside_the_trust_root_is_refused(tmp_path):
    surface = sp.observe()
    surface["PYTHONPATH"] = [str(tmp_path)]
    result = sp.check(sp.load_policy(), surface=surface)
    assert not result["clean"]
    assert any("trust root" in p for p in result["problems"])


def test_p09_a_repository_pythonpath_entry_outside_the_allowed_set_is_refused():
    surface = sp.observe()
    surface["PYTHONPATH"] = [str(REPO_ROOT / "tests")]
    result = sp.check(sp.load_policy(), surface=surface)
    assert not result["clean"]
    assert any("allowed set" in p for p in result["problems"])


def test_p10_the_pinned_pythonpath_entry_is_accepted():
    """Green-when-clean: the graded command's own PYTHONPATH must not be refused."""
    surface = sp.observe()
    surface["PYTHONPATH"] = [str(REPO_ROOT / "scripts")]
    assert sp.check(sp.load_policy(), surface=surface)["clean"]


def test_p11_an_unpinned_conftest_is_refused():
    surface = sp.observe()
    surface["conftest_files"] = sorted(surface["conftest_files"] + ["tests/extra/conftest.py"])
    result = sp.check(sp.load_policy(), surface=surface)
    assert not result["clean"]
    assert any("not pinned" in p for p in result["problems"])


def test_p12_a_changed_conftest_is_refused():
    """An ALLOWED startup file whose content changes after binding."""
    doc = sp.load_policy()
    doc["mechanisms"]["conftest"]["files"]["tests/conftest.py"] = "0" * 64
    result = sp.check(doc)
    assert not result["clean"]
    assert any("does not match its pin" in p for p in result["problems"])


def test_p13_a_plugin_loaded_outside_the_trust_root_is_refused(tmp_path):
    surface = sp.observe()
    surface["pytest_plugin_modules"] = [{"module": "pytest_rogue",
                                         "origin": str(tmp_path / "rogue.py")}]
    result = sp.check(sp.load_policy(), surface=surface)
    assert not result["clean"]
    assert any("outside the authorized trust root" in p for p in result["problems"])


def test_p14_a_policy_entry_without_enforcement_is_refused():
    """A classification nothing adjudicates is documentation — which is what ADV-I28AF-02 was."""
    doc = sp.load_policy()
    doc["mechanisms"]["some_new_hook"] = {"disposition": sp.PROHIBITED,
                                          "why_prohibited": "invented"}
    result = sp.check(doc)
    assert not result["clean"]
    assert any("no executable check adjudicates it" in p for p in result["problems"])


def test_p15_not_applicable_must_name_a_proof_test(tmp_path):
    doc = json.loads(STARTUP_POLICY.read_text(encoding="utf-8"))
    doc["mechanisms"]["PYTHONSTARTUP"].pop("proof_test")
    bad = tmp_path / "p.json"
    bad.write_text(json.dumps(doc))
    with pytest.raises(sp.StartupPolicyError):
        sp.load_policy(bad)


def test_p16_a_missing_policy_fails_closed(tmp_path):
    with pytest.raises(sp.StartupPolicyError):
        sp.load_policy(tmp_path / "absent.json")


def test_p17_a_symlinked_startup_path_is_resolved_not_trusted_by_name(tmp_path):
    """A symlink whose NAME looks in-tree but which resolves outside it must not pass."""
    outside = tmp_path / "outside.py"
    outside.write_text("# not in the repository\n")
    link = tmp_path / "looks-like-scripts"
    link.symlink_to(outside)
    assert not sp.in_trust_root(link), (
        "trust must be decided on the resolved target, never on the path as written")


def test_p18_the_trust_root_accepts_the_repository_and_the_interpreter():
    assert sp.in_trust_root(REPO_ROOT / "scripts" / "pytest_session_guard.py")
    assert sp.in_trust_root(Path(json.__file__))
    assert not sp.in_trust_root(Path("/etc/passwd"))


# ===================================================================== NOT_APPLICABLE proofs
def test_pythonstartup_provably_does_not_execute_for_the_graded_invocation(tmp_path):
    """The executable proof behind PYTHONSTARTUP's NOT_APPLICABLE disposition.

    Refusing a variable that provably cannot fire would break every operator shell that sets one
    for the REPL — the "control you must disable to use it" failure. So the disposition is excused
    by measurement, and this test is the measurement. If a future CPython ever honours
    PYTHONSTARTUP non-interactively, this breaks and the disposition must be revisited.
    """
    probe = tmp_path / "startup.py"
    probe.write_text("raise SystemExit('PYTHONSTARTUP EXECUTED')\n")
    env = dict(os.environ, PYTHONSTARTUP=str(probe))
    proc = subprocess.run([sys.executable, "-c", "print('MAIN RAN')"],
                          capture_output=True, text=True, env=env)
    assert proc.returncode == 0, proc.stderr
    assert "MAIN RAN" in proc.stdout
    assert "PYTHONSTARTUP EXECUTED" not in (proc.stdout + proc.stderr), (
        "PYTHONSTARTUP executed non-interactively; its NOT_APPLICABLE disposition is now wrong")


def test_meta_path_variant_is_still_displaced_by_pytest():
    """Proof behind import_hooks_meta_path = NOT_APPLICABLE.

    Gate 4N-I28AC measured that pytest inserts its AssertionRewritingHook ahead of a finder
    installed at interpreter startup. Re-measured here rather than inherited.
    """
    assert sys.meta_path, "no meta_path finders at all would be its own anomaly"
    front = type(sys.meta_path[0]).__name__
    assert front == "AssertionRewritingHook", (
        f"pytest no longer occupies meta_path[0] (found {front}); the NOT_APPLICABLE disposition "
        "for import hooks rests on that displacement and must be revisited")


def test_site_flags_can_only_remove_startup_mechanisms():
    """Proof behind interpreter_site_flags = NOT_APPLICABLE: -S/-I disable site loading."""
    proc = subprocess.run([sys.executable, "-S", "-c",
                           "import sys; print('sitecustomize' in sys.modules)"],
                          capture_output=True, text=True)
    assert proc.stdout.strip() == "False", (
        "-S must prevent sitecustomize from loading; if it does not, site flags can ADD a "
        "mechanism and the disposition is wrong")


# ===================================================================== bootstrap integration
def test_b01_the_bootstrap_runs_both_new_layers():
    attestation = boot.establish(strict=True)
    assert attestation["state_provenance"]["clean"], attestation["state_provenance"]["problems"]
    assert attestation["startup_policy"]["clean"], attestation["startup_policy"]["problems"]
    assert attestation["established"]


def test_b02_the_bootstrap_refuses_when_state_provenance_fails():
    guard = sys.modules["pytest_session_guard"]
    original = guard.REGISTRY
    try:
        guard.REGISTRY = Path("/tmp/decoy-registry.json")
        with pytest.raises(boot.BootstrapError) as excinfo:
            boot.establish(strict=True)
        assert "REGISTRY" in str(excinfo.value)
    finally:
        guard.REGISTRY = original
    assert boot.establish(strict=True)["established"], "restoration must re-establish"


def test_b03_reverification_exists_and_re_runs_every_layer():
    """ADV-I28AF-03: both modules claimed a session-finish re-run that did not exist."""
    outcome = boot.reverify()
    # Gate 4N-I28AI added the registry-authority and external-executable-trust layers, so this
    # asserts CONTAINMENT of the three original layers rather than an exact set that a later
    # gate would have to weaken to extend.
    assert {"executed_code", "executed_state", "startup_policy"} <= set(outcome["layers"])
    assert {"registry_authority", "external_executable_trust"} <= set(outcome["layers"])
    assert outcome["clean"], outcome["problems"]


def test_b04_reverification_catches_a_mutation_applied_after_configure():
    """The window ADV-I28AF-03 named. State verified at configure and mutated later must be
    caught before the session is reported satisfactory."""
    guard = sys.modules["pytest_session_guard"]
    original = guard.REGISTRY
    try:
        guard.REGISTRY = Path("/tmp/decoy-registry.json")
        outcome = boot.reverify()
        assert not outcome["clean"]
        assert outcome["layers"]["executed_state"] is False
    finally:
        guard.REGISTRY = original


def test_b05_the_documented_reverification_actually_exists():
    """The docstrings used to assert a mitigation that was absent. Bind claim to code."""
    source = (REPO_ROOT / "scripts" / "signalnest_bootstrap.py").read_text(encoding="utf-8")
    assert "def pytest_sessionfinish" in source, (
        "the residual-limitations text claims a session-finish re-run; it must exist")
    assert "def reverify" in source


# ===================================================================== self-protection
def test_z01_a_verifier_replaced_with_constant_success_is_visible():
    """Forced-success on the state verifier must be detectable, or the control is decorative."""
    import executed_code_provenance as ecp
    module = sys.modules["executed_state_provenance"]
    original = module.verify
    assert ecp.verify()["clean"], "baseline must be green or the refusal proves nothing"
    try:
        module.verify = lambda *a, **k: {"clean": True, "problems": [], "results": [],
                                         "modules": 0, "contract_sha256": ""}
        result = ecp.verify()
        assert not result["clean"], (
            "replacing the state verifier with constant success must be refused; `verify` is an "
            "authored critical callable of a protected module, so its absence from the runtime "
            "code map is itself the signal")
        assert any("verify" in p for p in result["problems"])
    finally:
        module.verify = original
    assert ecp.verify()["clean"], "restoration must return both layers to clean"


def test_z02_the_state_contract_and_policy_are_themselves_bound_state():
    """Both authored files must be load-bearing state of the modules that read them."""
    doc = json.loads(STATE_CONTRACT.read_text(encoding="utf-8"))
    policy_token = doc["modules"]["startup_policy"]["names"]["POLICY"]
    assert policy_token.startswith("FILE:"), (
        f"the startup policy must be content-bound, got {policy_token}")
    assert oracle_file_digest(STARTUP_POLICY)[:32] in policy_token


def test_z03_the_protected_set_is_content_bound_by_the_bootstrap():
    doc = json.loads(STATE_CONTRACT.read_text(encoding="utf-8"))
    token = doc["modules"]["signalnest_bootstrap"]["names"]["PROTECTED_SET"]
    assert token.startswith("FILE:")
    assert oracle_file_digest(PROTECTED_SET)[:32] in token


def test_z04_dynamic_attribute_access_on_a_protected_module_is_reported(tmp_path):
    """Honest-limit control: state reached by a computed name is not silently treated as absent."""
    module_file = tmp_path / "m.py"
    module_file.write_text("SECRET = 1\n\n\ndef critical(name):\n    return getattr(SECRET, name)\n")
    import types
    module = types.ModuleType("m")
    module.__file__ = str(module_file)
    exec(compile(module_file.read_bytes(), str(module_file), "exec"), module.__dict__)
    problems = esp.unbounded_access(module)
    assert any("dynamically" in p for p in problems), (
        "a getattr on a module-level object must be reported as unbounded, not ignored")


def test_z05_ordinary_getattr_on_a_local_object_is_not_flagged():
    """Green-when-clean for z04. Flagging every getattr would refuse every honest module — the
    Gate 4N-I28AE presence-is-fatal defect in a new costume."""
    for name in ("pytest_session_guard", "signalnest_bootstrap", "startup_policy"):
        assert not esp.unbounded_access(sys.modules[name]), (
            f"{name}: honest getattr calls on local objects must not be reported as unbounded")


def test_z06_set_valued_state_is_canonicalised_across_processes():
    """Set iteration order depends on the hash seed; an uncanonicalised token would drift at
    random and train the next reader to re-pin without looking."""
    script = textwrap.dedent(f"""
        import sys, json
        sys.path.insert(0, {str(REPO_ROOT / 'scripts')!r})
        import executed_state_provenance as esp
        print(esp.state_token({{'b', 'a', 'c'}}, owner_file='x'))
    """)
    seen = set()
    for seed in ("0", "1", "424242"):
        proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                              env=dict(os.environ, PYTHONHASHSEED=seed))
        assert proc.returncode == 0, proc.stderr
        seen.add(proc.stdout.strip())
    assert len(seen) == 1, f"set token is hash-seed dependent: {seen}"
