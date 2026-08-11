"""Gate 4N-I28AE — the code that EXECUTED must be the code that was authorized.

THE DEFECT THIS CLOSES. Gate 4N-I28AC finding ACC-I28AC-01. A `sitecustomize.py` staged into the
repository's own `scripts/` directory is auto-imported at interpreter startup by the graded
command's own PYTHONPATH — before pytest, before `conftest.py`, before `-p pytest_session_guard`.
Pre-seeding `sys.modules["pytest_session_guard"]` there makes the guard and every verifier resolve
the same substituted module, and every disk-reading check agrees with itself. Measured: **2906
passed, 0 failed, exit 0**, four assurance-control modules absent, package coherence coherent, with
the guard source, registry, baseline and CI workflow all byte-identical to their pins.

THE INDEPENDENT ORACLE IN THIS FILE SHARES NOTHING WITH PRODUCTION. `executed_code_provenance`
enumerates through `vars(module)`, normalizes with raw `co_code` bytes plus name tuples, and emits a
qualname→sha256 map. The oracle below enumerates through an `ast` parse of the pinned source,
normalizes with the *disassembled opcode stream* from `dis.get_instructions`, and emits an
opcode-histogram identity. Different parser, different normalization, different enumeration,
different output. They are compared only after both have been derived.
"""

from __future__ import annotations

import ast
import dis
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

import executed_code_provenance as ecp  # noqa: E402
import signalnest_bootstrap as boot  # noqa: E402

PROTECTED_SET = REPO_ROOT / "tests" / "fixtures" / "protected-module-set.json"
GUARD = REPO_ROOT / "scripts" / "pytest_session_guard.py"


# ===================================================================== independent oracle
def oracle_expected_symbols(path: Path) -> set[str]:
    """Enumerate defined callables by PARSING THE SOURCE, not by reading a module namespace."""
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    names: set[str] = set()

    def walk(node, prefix=""):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                names.add(f"{prefix}{child.name}")
                walk(child, f"{prefix}{child.name}.<locals>.")
            elif isinstance(child, ast.ClassDef):
                names.add(f"{prefix}{child.name}")
                walk(child, f"{prefix}{child.name}.")
            else:
                walk(child, prefix)

    walk(tree)
    return names


def oracle_opcode_identity(code) -> str:
    """Identity from the DISASSEMBLED opcode stream — not from raw co_code bytes.

    ORACLE DEFECT FOUND AND CORRECTED AT I28AE, recorded rather than quietly fixed. The first
    version hashed `argrepr` verbatim. For a LOAD_CONST of a nested code object that repr is
    `<code object evaluate at 0x10a1b2c30, file "...", line 191>` — it embeds a MEMORY ADDRESS,
    which differs between any two compiles of identical source. The oracle therefore reported
    mismatches in evaluate, session_identity, analyse_function and validate against a production
    result that was clean, and the disagreement was the oracle's, not production's.

    The correction normalizes a nested code-object argument to its qualified name. That removes
    non-determinism WITHOUT removing signal: the nested code object is fingerprinted separately
    under its own qualname, so altered nested code is still caught — proven by
    test_the_oracle_still_catches_a_real_substitution.

    SECOND ORACLE DEFECT, FOUND AT GATE 4N-I28AS, and it is the FOURTH recurrence of the
    set-ordering class in this chain (production at I28AG, its independent sibling at I28AM, the
    canonicalisation in cache_authority at I28AR, and now here). The `argrepr` of a frozenset
    constant renders its members in ITERATION order. A module imported from a cached .pyc carries a
    frozenset unmarshalled with the insertion order of whatever process wrote the cache, while
    `oracle_disk_map` compiles the file fresh in THIS process — so two EQUAL frozensets can render
    differently. Measured across hash seeds on npm_authority.lifecycle_problems: identical at seeds
    0, 1 and 42, DIFFERENT at 7 and 99, with `co_code`, `co_names` and `co_consts` all equal. An
    intermittent, seed-dependent false mismatch is the worst possible failure for an oracle,
    because it teaches a reader to disbelieve it.

    The correction renders set and frozenset constants in sorted order. Signal is preserved: a set
    with different MEMBERS still sorts differently, so a real substitution is still caught.
    """
    def _argrepr(instruction):
        argval = instruction.argval
        if hasattr(argval, "co_code"):
            return f"<code {argval.co_qualname}>"
        if isinstance(argval, (frozenset, set)):
            return "{" + ", ".join(sorted(repr(m) for m in argval)) + "}"
        if isinstance(argval, tuple) and any(isinstance(m, (frozenset, set)) for m in argval):
            return "(" + ", ".join(
                "{" + ", ".join(sorted(repr(x) for x in m)) + "}"
                if isinstance(m, (frozenset, set)) else repr(m) for m in argval) + ")"
        return instruction.argrepr

    stream = [(i.opname, _argrepr(i)) for i in dis.get_instructions(code)]
    return hashlib.sha256(json.dumps(stream, sort_keys=False).encode()).hexdigest()


def oracle_runtime_map(module) -> dict:
    """Runtime identities, enumerated by walking the module's own __dict__ tree separately."""
    origin = getattr(module, "__file__", None)
    out: dict[str, str] = {}
    stack = [(module.__dict__, "")]
    seen = set()
    while stack:
        namespace, _prefix = stack.pop()
        for value in list(namespace.values()):
            code = getattr(value, "__code__", None)
            if code is not None and getattr(code, "co_filename", None) == origin:
                out[code.co_qualname] = oracle_opcode_identity(code)
            if isinstance(value, type) and id(value) not in seen:
                seen.add(id(value))
                stack.append((dict(vars(value)), value.__name__ + "."))
            fn = getattr(value, "__func__", None)
            if fn is not None and getattr(fn, "__code__", None) is not None:
                c = fn.__code__
                if getattr(c, "co_filename", None) == origin:
                    out[c.co_qualname] = oracle_opcode_identity(c)
    return out


def oracle_disk_map(path: Path) -> dict:
    top = compile(Path(path).read_bytes(), str(path), "exec")
    out: dict[str, str] = {}

    def walk(code):
        for const in code.co_consts:
            if hasattr(const, "co_code"):
                out[const.co_qualname] = oracle_opcode_identity(const)
                walk(const)

    walk(top)
    return out


# ===================================================================== the mandatory node
def test_executed_code_matches_the_authorized_package(pytestconfig):
    """THE node. Substituted code that behaves differently has different bytecode."""
    result = ecp.verify()
    assert result["clean"], (
        "executed-code provenance disagrees with the authorized package:\n  "
        + "\n  ".join(result["problems"]))
    assert result["protected_modules"] >= 4
    resident = [r for r in result["results"] if r.get("runtime_code_digest")]
    assert resident, "no protected module was resident to verify"
    for r in resident:
        # NOT whole-map digest equality: the disk map includes class bodies a compile produces
        # and the runtime map cannot (they execute once and are not retained). Equality is
        # asserted where it means something.
        assert r["mismatched"] == [], (r["module"], r["mismatched"])
        assert r["missing_critical"] == [], (r["module"], r["missing_critical"])
        assert r["wrong_critical"] == [], (r["module"], r["wrong_critical"])
        assert r["shared_code_objects"] >= 1
        assert r["critical_callables"] >= 3, r["module"]


def test_the_independent_oracle_agrees_with_production():
    """Both sides derived first, compared second. Any disagreement is a blocker."""
    production = ecp.verify()
    oracle_findings = []
    doc = json.loads(PROTECTED_SET.read_text(encoding="utf-8"))
    for entry in doc["protected_modules"]:
        name = entry["module"]
        module = sys.modules.get(name)
        if module is None:
            continue
        path = REPO_ROOT / entry["relative_path"]
        run = oracle_runtime_map(module)
        disk = oracle_disk_map(path)
        shared = set(run) & set(disk)
        mismatched = sorted(q for q in shared if run[q] != disk[q])
        if mismatched or not shared:
            oracle_findings.append((name, mismatched or "no shared code objects"))
        # the oracle also checks the source-parsed symbol set is actually present
        expected = oracle_expected_symbols(path)
        top_level = {q.split(".")[0] for q in disk}
        assert top_level <= expected | {"<lambda>", "<listcomp>", "<setcomp>", "<dictcomp>",
                                        "<genexpr>", "<module>"}, name

    assert (production["clean"], oracle_findings == []) == (True, True), (
        f"production clean={production['clean']} problems={production['problems'][:2]}; "
        f"oracle findings={oracle_findings}")


def test_the_canonicalization_is_deterministic():
    """Measured, not assumed: pytest's hook loads these modules but does not rewrite them."""
    module = sys.modules["pytest_session_guard"]
    loader = type(module.__spec__.loader).__name__
    disk = ecp.disk_code_identity(GUARD)
    runtime = ecp.runtime_code_identity(module)
    shared = set(disk) & set(runtime)
    assert shared, "no shared code objects; the canonicalization would be vacuous"
    assert all(disk[q] == runtime[q] for q in shared), (
        f"loader {loader} produced bytecode differing from a plain compile. The canonicalization "
        "documented in scripts/executed_code_provenance.py no longer holds and this control must "
        "be reworked rather than relaxed.")
    # determinism: recomputing gives the same answer
    assert ecp.digest_of(ecp.disk_code_identity(GUARD)) == ecp.digest_of(disk)


def test_the_protected_set_is_authored_and_bounded():
    doc = json.loads(PROTECTED_SET.read_text(encoding="utf-8"))
    mods = doc["protected_modules"]
    assert len(mods) == len({m["module"] for m in mods}), "duplicate protected module"
    for m in mods:
        assert (REPO_ROOT / m["relative_path"]).is_file(), m["module"]
        assert m["why_protected"] and m["proving_substitution"]
    assert any(m["module"] == "signalnest_bootstrap" for m in mods), (
        "the bootstrap must be protected by the same rule it applies, or it is the one module a "
        "substitution could still reach")
    assert "HONEST LIMIT" in " ".join(doc["_trust_boundary"])


# ===================================================================== adversarial harness
def _sandbox(tmp_path: Path, sitecustomize: str = "", extra: dict | None = None) -> Path:
    root = tmp_path / "s"
    (root / "scripts").mkdir(parents=True)
    (root / "tests" / "fixtures").mkdir(parents=True)
    (root / ".github" / "workflows").mkdir(parents=True)
    for rel in ("scripts/pytest_session_guard.py", "scripts/executed_code_provenance.py",
                "scripts/signalnest_bootstrap.py", "scripts/assertion_contracts.py",
                "scripts/pytest_config_contract.py",
                # Gate 4N-I28AG: the protected set grew, and a sandbox missing a protected module
                # fails closed on absence rather than on the attack the test is probing.
                "scripts/executed_state_provenance.py", "scripts/startup_policy.py",
                "scripts/registry_authority.py", "scripts/external_executable_trust.py",
                "scripts/executable_inventory.py",
                # Gate 4N-I28AO: the inventory now consumes the command-position model, and
                # site_taxonomy is a protected module whose local import the state contract binds.
                # A sandbox missing either fails on absence rather than on the attack under test.
                "scripts/shell_positions.py", "scripts/site_taxonomy.py",
                # Gate 4N-I28AR: cache_authority is the seventh bootstrap layer and site_taxonomy
                # imports it to freeze every value entering the cache. It fails CLOSED when its
                # policy is absent, so a sandbox without both would refuse on absence rather than
                # on the attack under test — measuring nothing.
                "scripts/cache_authority.py",
                "tests/fixtures/cache-authority-policy.json",
                # Gate 4N-I28AS: npm_authority is the eighth bootstrap layer and external
                # executable trust delegates npm to it. It fails CLOSED without its policy.
                "scripts/npm_authority.py",
                "tests/fixtures/npm-authority-policy.json",
                # Gate 4N-I28AT: docker_boundary is the ninth bootstrap layer and fails CLOSED
                # without its policy.
                "scripts/docker_boundary.py",
                "tests/fixtures/docker-boundary-policy.json",
                # Gate 4N-I28BF-B1: the bootstrap binds the authoritative Docker assurance state, a
                # protected module that imports expiry_authorization. A sandbox missing either fails
                # on ABSENCE rather than on the attack under test.
                "scripts/docker_assurance_state.py",
                "scripts/expiry_authorization.py",
                # Gate 4N-I28BG-B1: the reusable workflow-assurance verifier and the static graph
                # validator are protected modules. A sandbox missing either fails on ABSENCE rather
                # than on the attack under test. workflow_assurance imports docker_assurance_state
                # and expiry_authorization (already above); workflow_graph_validator imports only
                # PyYAML and the stdlib.
                "scripts/workflow_assurance.py",
                "scripts/workflow_graph_validator.py",
                # Gate 4N-I28AV: the parser completeness fixtures.
                "tests/fixtures/shell-case-grammar-contract.json",
                "tests/fixtures/shell-grammar-corpus.json",
                # Gate 4N-I28BB: the bootstrap imports the INDEPENDENT exec-transfer oracle to
                # reconcile command-position transfers against the production parser. A sandbox
                # without it raises ModuleNotFoundError inside establish(), so the session fails on
                # ABSENCE rather than on the attack under test — measuring nothing.
                "scripts/exec_transfer_oracle.py",
                # Gate 4N-I28BE: the per-site Docker enforcement consumer lives in docker_boundary.

                "scripts/failure_propagation.py", "scripts/shell_command_model.py",
                "scripts/ci_invocation_model.py", "scripts/site_behavior.py",
                "tests/fixtures/executed-state-contract.json",
                "tests/fixtures/startup-policy.json",
                "tests/fixtures/protected-module-set.json",
                # Gate 4N-I28AI: registry_authority pins the registry against this baseline and
                # refuses when it is absent, so the sandbox must carry one to re-pin.
                "tests/fixtures/mandatory-session-baseline.json",
                "tests/fixtures/executable-trust-policy.json",
                "tests/fixtures/mandatory-pytest-nodes.json",
                "tests/fixtures/assertion-contract-registry.json",
                "tests/fixtures/assertion-meta-contract.json",
                "tests/fixtures/pytest-configuration-baseline.json",
                # Gate 4N-I28AO: site_taxonomy reads the workflow and the dispatch fixture, and
                # executable_inventory derives run: blocks from it. Both are pinned state now, so a
                # sandbox without them fails on absence rather than on the attack under test.
                ".github/workflows/ci.yml",
                "tests/fixtures/framework-dispatch-observed.json"):
        src = REPO_ROOT / rel
        if src.is_file():
            (root / rel).write_bytes(src.read_bytes())
    (root / "tests" / "test_probe.py").write_text("def test_probe():\n    assert True\n")
    # Gate 4N-I28AO: the trust policy declares dynamic shell sites by file and line. This sandbox
    # has no shell scripts and no workflow, so every declaration is stale HERE and the two-way
    # check refuses. A declaration describes the tree it governs.
    # Gate 4N-I28AT: the Docker boundary policy describes the tree it governs. This sandbox
    # carries ci.yml but not reader-publish.yml or staging-publish.yml, so classifications naming
    # call sites in those workflows are STALE here and the two-way check correctly refuses. Filter
    # to the sandbox's own tree — the same principle the executable-trust rewrite below uses, and
    # the same reason: a sandbox must refuse on the attack under test, not on absence.
    _docker_policy = root / "tests" / "fixtures" / "docker-boundary-policy.json"
    if _docker_policy.is_file():
        _doc = json.loads(_docker_policy.read_text())
        _doc["call_sites"] = [c for c in _doc["call_sites"]
                              if (root / c["source"]).is_file()]
        _docker_policy.write_text(json.dumps(_doc, indent=1, sort_keys=True))

    _policy = root / "tests" / "fixtures" / "executable-trust-policy.json"
    if _policy.is_file():
        _doc = json.loads(_policy.read_text())
        # Keep the declarations whose construct EXISTS here — the sandbox has ci.yml but no shell
        # scripts — so the two-way check still fails on an undeclared or stale site in this tree.
        _doc["dynamic_shell_sites"] = [d for d in _doc.get("dynamic_shell_sites", [])
                                       if str(d.get("module", "")).startswith("ci.yml")]
        _doc["executables"] = {k: v for k, v in _doc["executables"].items()
                               if v.get("classification") != "UNREACHABLE_FROM_GRADED_ROOTS"}
        _policy.write_text(json.dumps(_doc, indent=1, sort_keys=True))
        # The contract binds state FILES by CONTENT, and the policy just rewritten is one of them.
        # Re-derive every FILE token against this sandbox's own copies; a file the sandbox does not
        # contain keeps its pin, so a genuinely missing state file still fails closed.
        _contract = root / "tests" / "fixtures" / "executed-state-contract.json"
        if _contract.is_file():
            _cdoc = json.loads(_contract.read_text())
            for _entry in _cdoc.get("modules", {}).values():
                for _k, _tok in list(_entry.get("names", {}).items()):
                    if not isinstance(_tok, str) or not _tok.startswith("FILE:"):
                        continue
                    _, _rel, _old = _tok.split(":", 2)
                    _local = root / _rel
                    if _local.is_file():
                        _entry["names"][_k] = (
                            f"FILE:{_rel}:"
                            f"{hashlib.sha256(_local.read_bytes()).hexdigest()[:32]}")
            _contract.write_text(json.dumps(_cdoc, indent=1, sort_keys=True))
    # deliberately OUTSIDE tests/fixtures: a sandbox-only path that LOOKS like a repository
    # fixture is read by the package-coherence fixture-reference check as a broken reference.
    (root / "tests" / "fixtures" / "mandatory-pytest-nodes.json").write_text(json.dumps({
        "mandatory_nodes": [{"node_id": "tests/test_probe.py::test_probe",
                             "protected_invariant": "the sandbox control runs",
                             "owning_layer": "SANDBOX", "required_phase": "call",
                             "acceptable_outcome": "passed",
                             "prohibited_outcomes": ["skipped", "deselected"],
                             "why_mandatory": "sandbox", "proving_bypass_mutation": "sandbox",
                             "category": "SANDBOX"}]}))
    if sitecustomize:
        (root / "scripts" / "sitecustomize.py").write_text(textwrap.dedent(sitecustomize))
    for rel, text in (extra or {}).items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(text))
    # GATE 4N-I28AG. The startup policy describes the tree it governs. The copy taken from the
    # real repository pins that repository's conftest set, which this sandbox does not have, so
    # the policy must be re-pinned for the sandbox's own tree. Re-pinning here rather than
    # loosening the production check keeps "an allowed startup file is bound by content" true in
    # both trees — an unfaithful sandbox must not become a reason to weaken the real control.
    policy_file = root / "tests" / "fixtures" / "startup-policy.json"
    if policy_file.is_file():
        policy = json.loads(policy_file.read_text())
        sandbox_conftests = {}
        for rel in ("conftest.py", "tests/conftest.py"):
            candidate = root / rel
            if candidate.is_file():
                sandbox_conftests[rel] = hashlib.sha256(candidate.read_bytes()).hexdigest()
        policy["mechanisms"]["conftest"]["files"] = sandbox_conftests
        policy["mechanisms"]["conftest"]["multiplicity"] = len(sandbox_conftests)
        policy_file.write_text(json.dumps(policy, indent=1, sort_keys=True))

    # The executed-state contract binds state FILES by content, and the policy just rewritten is
    # one of them. A contract copied from the real repository therefore describes hashes this tree
    # does not have. Re-derive every FILE token against the sandbox's own copies — same principle
    # as the policy: a contract describes the tree it governs. Any file the sandbox does not
    # contain is left pinned as-is so a genuinely missing state file still fails closed.
    # GATE 4N-I28AI: registry_authority pins the registry against mandatory-session-baseline.json.
    # The sandbox writes its own registry, so it must re-pin its own baseline — same principle as
    # the policy and contract below: a contract describes the tree it governs.
    reg = root / "tests" / "fixtures" / "mandatory-pytest-nodes.json"
    base = root / "tests" / "fixtures" / "mandatory-session-baseline.json"
    if reg.is_file() and base.is_file():
        doc = json.loads(base.read_text())
        raw = reg.read_bytes()
        parsed = json.loads(raw)
        doc["registry_sha256"] = hashlib.sha256(raw).hexdigest()
        doc["mandatory_node_count"] = len(parsed["mandatory_nodes"])
        doc["mandatory_node_ids"] = sorted(n["node_id"] for n in parsed["mandatory_nodes"])
        base.write_text(json.dumps(doc, indent=1, sort_keys=True))

    # GATE 4N-I28AM: an UNREACHABLE_FROM_GRADED_ROOTS entry re-derives reachability from
    # site_taxonomy, which this sandbox does not carry. Its precondition therefore cannot be
    # checked here and correctly refuses — for a reason unrelated to the attack under test. The
    # sandbox has none of the developer scripts that name npm, so dropping the entry is the same
    # "a policy describes the tree it governs" rule already applied to conftest above.
    trust_file = root / "tests" / "fixtures" / "executable-trust-policy.json"
    if trust_file.is_file():
        trust = json.loads(trust_file.read_text())
        trust["executables"] = {
            name: entry for name, entry in trust["executables"].items()
            if entry.get("classification") != "UNREACHABLE_FROM_GRADED_ROOTS"}
        trust_file.write_text(json.dumps(trust, indent=1, sort_keys=True))

    contract_file = root / "tests" / "fixtures" / "executed-state-contract.json"
    if contract_file.is_file():
        contract = json.loads(contract_file.read_text())
        for entry in contract.get("modules", {}).values():
            for name, token in list(entry.get("names", {}).items()):
                if not token.startswith("FILE:"):
                    continue
                _, rel, _digest = token.split(":", 2)
                local = root / rel
                if local.is_file():
                    entry["names"][name] = (
                        f"FILE:{rel}:{hashlib.sha256(local.read_bytes()).hexdigest()[:32]}")
        contract_file.write_text(json.dumps(contract, indent=1, sort_keys=True))
    return root


def _run(root: Path, *, bootstrap=True, env_extra=None, paths=("tests/",), extra_args=()):
    # GATE 4N-I28AI: the override is retired (ADV-I28AH-01). A sandbox is its own tree, so its
    # registry lives at the canonical path INSIDE that tree and needs no redirection.
    env = dict(os.environ, PYTHONPATH=str(root / "scripts"))
    env.pop("SIGNALNEST_MANDATORY_NODES", None)
    env.pop("SIGNALNEST_BOOTSTRAP_ATTESTATION", None)
    env.update(env_extra or {})
    args = [sys.executable, "-m", "pytest", *paths, "-q", "-p", "no:randomly"]
    if bootstrap:
        args += ["-p", "signalnest_bootstrap"]
    args += ["-p", "pytest_session_guard", *extra_args]
    return subprocess.run(args, cwd=root, env=env, capture_output=True, text=True, timeout=300)


PRESEED = '''
    import hashlib, pathlib, sys, types
    GENUINE = pathlib.Path(__file__).resolve().parent / "pytest_session_guard.py"
    if "pytest_session_guard" not in sys.modules:
        src = GENUINE.read_text()
        target = "            if exitstatus == 0:\\n                session.exitstatus = 3\\n"
        assert target in src, "MUTATION NOT APPLIED"
        src = src.replace(target, "")
        mod = types.ModuleType("pytest_session_guard")
        mod.__file__ = str(GENUINE)
        mod.__spec__ = types.SimpleNamespace(
            origin=str(GENUINE), loader=type("SourceFileLoader", (), {})())
        exec(compile(src, str(GENUINE), "exec"), mod.__dict__)
        sys.modules["pytest_session_guard"] = mod
'''


def _refused(p) -> bool:
    return p.returncode != 0


# ===================================================================== the exact ACC-I28AC-01 pin
def test_acc_i28ac_01_sys_modules_preseeding_is_refused(tmp_path):
    """THE pin. The exact successful I28AC exploit, now failing closed."""
    root = _sandbox(tmp_path, sitecustomize=PRESEED)
    p = _run(root)
    assert _refused(p), p.stdout[-2500:]
    blob = p.stdout + p.stderr
    assert "ALREADY resident in sys.modules" in blob or "BootstrapError" in blob, blob[-2500:]


def test_acc_i28ac_01_reproduces_without_the_bootstrap(tmp_path):
    """Detector-is-alive: the same exploit WITHOUT the bootstrap plugin still disarms the guard.

    This is the pre-remediation path preserved as a live control. If this ever starts failing,
    the pin above proves nothing and must be re-derived.
    """
    root = _sandbox(tmp_path, sitecustomize=PRESEED)
    p = _run(root, bootstrap=False)
    assert p.returncode == 0, (
        "the pre-remediation path no longer reproduces the exploit; the pin's baseline is gone\n"
        + p.stdout[-2000:])


def test_clean_positive_control_still_runs(tmp_path):
    """Green when clean — without this every refusal above proves only that everything fails."""
    root = _sandbox(tmp_path)
    p = _run(root)
    assert p.returncode == 0, p.stdout[-2500:]


# ===================================================================== adversarial matrix
def test_the_oracle_still_catches_a_real_substitution():
    """Green-when-clean is not enough: prove the CORRECTED oracle still detects altered code."""
    import types
    src = GUARD.read_text().replace(
        "            if exitstatus == 0:\n                session.exitstatus = 3\n", "", 1)
    mod = types.ModuleType("pytest_session_guard")
    mod.__file__ = str(GUARD)
    exec(compile(src, str(GUARD), "exec"), mod.__dict__)
    run = oracle_runtime_map(mod)
    disk = oracle_disk_map(GUARD)
    shared = set(run) & set(disk)
    mismatched = [q for q in shared if run[q] != disk[q]]
    assert mismatched, "the corrected oracle no longer detects a real substitution"
    assert any("sessionfinish" in q for q in mismatched), mismatched


@pytest.mark.parametrize("label,sitecustomize,extra", [
    ("a01 preseed with matching name and plausible __file__", PRESEED, None),
    ("a02 preseed a byte-identical copy but patch a method afterwards", '''
        import pathlib, sys, importlib.util
        GENUINE = pathlib.Path(__file__).resolve().parent / "pytest_session_guard.py"
        spec = importlib.util.spec_from_file_location("pytest_session_guard", str(GENUINE))
        mod = importlib.util.module_from_spec(spec)
        sys.modules["pytest_session_guard"] = mod
        spec.loader.exec_module(mod)
        mod.MandatorySessionGuard.pytest_sessionfinish = lambda self, session, exitstatus: None
     ''', None),
    ("a04 cwd shadow module", "", {"pytest_session_guard.py": "raise SystemExit('shadow')\n"}),
])
def test_adversarial_matrix(tmp_path, label, sitecustomize, extra):
    root = _sandbox(tmp_path, sitecustomize=sitecustomize, extra=extra)
    p = _run(root)
    assert _refused(p), f"{label} was NOT refused:\n{p.stdout[-2500:]}"


SYS_META_PATH_VARIANT = '''
    import importlib.abc, importlib.util, pathlib, sys
    GENUINE = pathlib.Path(__file__).resolve().parent / "pytest_session_guard.py"
    class SourceFileLoader:
        def create_module(self, spec): return None
        def exec_module(self, module):
            module.__file__ = str(GENUINE)
            src = GENUINE.read_text().replace(
                "            if exitstatus == 0:\n                session.exitstatus = 3\n", "")
            exec(compile(src, str(GENUINE), "exec"), module.__dict__)
    class Finder(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path=None, target=None):
            if name != "pytest_session_guard": return None
            return importlib.util.spec_from_loader(name, SourceFileLoader(), origin=str(GENUINE))
    sys.meta_path.insert(0, Finder())
'''


def test_a03_sys_meta_path_variant_is_displaced_not_refused(tmp_path):
    """HISTORICAL DISPOSITION, preserved deliberately and NOT relabelled as successful.

    Gate 4N-I28AC measured this variant as UNSUCCESSFUL: pytest inserts its own
    AssertionRewritingHook into sys.meta_path AFTER sitecustomize runs, which displaces an
    attacker finder installed at startup. The genuine module is therefore imported and the genuine
    code executes.

    The correct assertion is NOT "the session is refused" — there is nothing to refuse when
    nothing was substituted, and asserting a refusal here would have quietly converted a
    non-exploit into a claimed catch. What is asserted is what was measured: the loader in force
    is pytest's, the executing code is the pinned code, and the session legitimately passes.
    """
    root = _sandbox(tmp_path, sitecustomize=SYS_META_PATH_VARIANT,
                    extra={"tests/conftest.py": '''
        def pytest_configure(config):
            import dis, io, pytest_session_guard as g
            buf = io.StringIO()
            dis.dis(g.MandatorySessionGuard.pytest_sessionfinish, file=buf)
            print("A03 loader=%s exitstatus_refs=%d" % (
                type(g.__spec__.loader).__name__, buf.getvalue().count("exitstatus")), flush=True)
    '''})
    p = _run(root, extra_args=("-s",))
    assert "A03 loader=AssertionRewritingHook" in p.stdout, p.stdout[-1500:]
    assert "exitstatus_refs=2" in p.stdout, (
        "the attacker's loader was NOT displaced; this variant would then be exploitable and the "
        "I28AC historical disposition would need re-deriving rather than citing")
    assert p.returncode == 0, "genuine code ran, so the session must pass"


def test_a05_unexpected_origin_is_refused():
    """A resident protected module whose origin is not the pinned path fails closed."""
    import types
    fake = types.ModuleType("pytest_session_guard")
    fake.__file__ = "/tmp/not-the-pinned-path.py"
    r = ecp.verify(modules={"pytest_session_guard": fake})
    assert not r["clean"]
    assert any("not the pinned path" in p for p in r["problems"])


def test_a06_unresolvable_provenance_is_refused():
    import types
    fake = types.ModuleType("pytest_session_guard")
    fake.__file__ = str(GUARD)          # right origin, but no code objects at all
    r = ecp.verify(modules={"pytest_session_guard": fake})
    assert not r["clean"]
    assert any("unresolvable" in p or "no code object" in p for p in r["problems"])


def test_a07_altered_runtime_unchanged_disk_is_refused(tmp_path):
    """The ACC-I28AC-01 shape in isolation: disk untouched, executed code different."""
    import types
    src = GUARD.read_text().replace(
        "            if exitstatus == 0:\n                session.exitstatus = 3\n", "", 1)
    mod = types.ModuleType("pytest_session_guard")
    mod.__file__ = str(GUARD)
    exec(compile(src, str(GUARD), "exec"), mod.__dict__)
    r = ecp.verify(modules={"pytest_session_guard": mod})
    assert not r["clean"]
    assert any("differ from the pinned bytes" in p for p in r["problems"]), r["problems"]


def test_a08_unchanged_runtime_changed_disk_is_refused(tmp_path):
    """The mirror image: what executed is genuine, but the staged file has moved."""
    scratch = tmp_path / "scripts"
    scratch.mkdir(parents=True)
    copy = scratch / "pytest_session_guard.py"
    copy.write_text(GUARD.read_text() + "\n\ndef _added_after_the_fact():\n    return 1\n")
    disk = ecp.disk_code_identity(copy)
    runtime = ecp.runtime_code_identity(sys.modules["pytest_session_guard"])
    assert set(disk) - set(runtime), "the changed disk source must expose code the runtime lacks"


def test_a09_protected_set_omission_is_caught():
    doc = json.loads(PROTECTED_SET.read_text(encoding="utf-8"))
    trimmed = {"protected_modules": [m for m in doc["protected_modules"]
                                     if m["module"] != "pytest_session_guard"]}
    r = ecp.verify(protected=trimmed)
    assert r["protected_modules"] < len(doc["protected_modules"])
    assert not any(x["module"] == "pytest_session_guard" for x in r["results"]), (
        "an omitted module is silently unprotected, which is why the set is pinned by hash")


def test_a10_protected_set_injection_is_caught():
    doc = json.loads(PROTECTED_SET.read_text(encoding="utf-8"))
    injected = {"protected_modules": doc["protected_modules"] + [
        {"module": "not_a_real_module", "relative_path": "scripts/not_a_real_module.py",
         "why_protected": "injected", "proving_substitution": "injected"}]}
    r = ecp.verify(protected=injected)
    assert not r["clean"]
    assert any("not in the tree" in p for p in r["problems"])


def test_a11_empty_protected_set_is_refused():
    with pytest.raises(ecp.ProvenanceError):
        ecp.load_protected_set(Path("/dev/null"))
    with pytest.raises(ecp.ProvenanceError):
        ecp.verify(protected={"protected_modules": []})


def test_a12_bootstrap_order_every_resident_protected_module_is_verified(tmp_path):
    """Bootstrap order, asserted against what actually happens rather than what I first assumed.

    pytest imports `-p` plugins BEFORE pytest_configure fires, so `pytest_session_guard` and
    `signalnest_bootstrap` are legitimately resident by the time the bootstrap runs. An earlier
    version of this test asserted `preseeded == []` and failed on every honest session. Treating
    presence as the signal would have forced the control to be disabled to be usable. The real
    requirement is that EVERY resident protected module has its executed code verified against the
    pinned bytes before the session is allowed to proceed.
    """
    root = _sandbox(tmp_path)
    marker = tmp_path / "attestation.json"
    p = _run(root, env_extra={"SIGNALNEST_BOOTSTRAP_ATTESTATION": str(marker)})
    assert p.returncode == 0, p.stdout[-2000:]
    att = json.loads(marker.read_text())
    assert att["established"] is True
    assert att["provenance"]["clean"] is True, att["provenance"]["problems"]
    verified = {r["module"] for r in att["provenance"]["results"] if r.get("runtime_code_digest")}
    assert set(att["preseeded_protected_modules"]) <= verified, (
        "a protected module was resident before the bootstrap ran and was NOT provenance-verified")
    assert att["bootstrap_source_sha256"] and att["protected_set_sha256"]


def test_a13_the_startup_surface_is_recorded_as_evidence(tmp_path):
    surface = boot.startup_surface()
    for field in ("sitecustomize_resident", "usercustomize_resident", "pythonpath", "cwd",
                  "cwd_on_sys_path", "meta_path", "path_hooks", "no_site", "isolated"):
        assert field in surface, field


def test_a14_provenance_manifest_tamper_is_caught():
    """The protected set is pinned; a tampered copy does not silently become authoritative."""
    doc = json.loads(PROTECTED_SET.read_text(encoding="utf-8"))
    tampered = json.loads(json.dumps(doc))
    # By NAME, never by position: Gate 4N-I28AG re-sorted the protected set, and an index-addressed
    # tamper silently became a self-pointing no-op that verified clean.
    victim = next(e for e in tampered["protected_modules"]
                  if e["module"] == "pytest_session_guard")
    victim["relative_path"] = "scripts/assertion_contracts.py"
    r = ecp.verify(protected=tampered)
    assert not r["clean"], "pointing a protected entry at a different file must not verify clean"


def test_a15_bootstrap_refuses_when_the_protected_set_is_missing(tmp_path):
    original = boot.PROTECTED_SET
    try:
        boot.PROTECTED_SET = tmp_path / "absent.json"
        with pytest.raises(boot.BootstrapError):
            boot.establish(strict=True)
    finally:
        boot.PROTECTED_SET = original


def test_a16_meta_path_historical_disposition_is_preserved():
    """I28AC measured the meta_path variant as DISPLACED, not successful. Do not relabel it."""
    src = (REPO_ROOT / "scripts" / "signalnest_bootstrap.py").read_text(encoding="utf-8")
    assert "sitecustomize" in src and "sys.modules" in src
    doc = json.loads(PROTECTED_SET.read_text(encoding="utf-8"))
    assert any("sitecustomize" in m.get("proving_substitution", "")
               for m in doc["protected_modules"]), (
        "the proving substitution must name the mechanism that actually succeeded")
