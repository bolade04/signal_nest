"""Gate 4N-I28AS — npm and Node toolchain identity, closing Gate 4N-I28AP finding ADV-I28AP-02.

THE DEFECT. npm was authorized by a path PREFIX. A prefix match set `approved = []`, skipping the
approved-path membership test entirely, so ANY file beneath `~/.nvm/versions/node/` was accepted and
the ATTACKER'S digest was recorded as the bound content. Reproduced on this host before the fix:
six attacker layouts accepted, executable trust clean, bootstrap established, session-finish
reverification clean, and the attacker npm then EXECUTED. The identical fake OUTSIDE the prefix was
refused — which is what proves the prefix allowance was the entire basis of acceptance.

WHAT THESE CONTROLS PROTECT.

  1. The prefix mechanism is GONE from the code, not merely unused, and a policy still carrying one
     is refused.
  2. The whole chain is resolved and bound: wrapper, symlink chain, CLI JavaScript, Node
     interpreter, package root, package.json, installation family.
  3. Installation families carry explicit authority models; an unknown layout fails closed.
  4. NVM provenance rests on the installation TREE, the Node<->npm RELATIONSHIP and the version
     manager's OWN METADATA — never on being in the right directory.
  5. Configuration able to substitute the tool is adjudicated; lifecycle boundaries are explicit.
  6. Everything is reverified at session finish, where a late substitution is only ever visible as
     a difference from what was validated.

EVERY control that writes attacker state does so inside a probe directory removed in a `finally`.
The genuine Node installation on this host is never modified — a control that breaks the developer's
toolchain is not a control, it is an outage.
"""

from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import external_executable_trust as eet                          # noqa: E402
import npm_authority as na                                       # noqa: E402

POLICY = REPO_ROOT / "tests" / "fixtures" / "npm-authority-policy.json"
TRUST_POLICY = REPO_ROOT / "tests" / "fixtures" / "executable-trust-policy.json"
PROTECTED_SET = REPO_ROOT / "tests" / "fixtures" / "protected-module-set.json"

NVM = Path(os.environ.get("NVM_DIR", str(Path.home() / ".nvm")))
VERSIONS = NVM / "versions" / "node"
GENUINE = VERSIONS / "v20.20.2"

_HAVE_NVM = GENUINE.is_dir() and (GENUINE / "bin" / "npm").exists()
requires_nvm = pytest.mark.skipif(
    not _HAVE_NVM,
    reason="this host has no NVM installation at $NVM_DIR/versions/node/v20.20.2, so the "
           "provenance controls have nothing genuine to distinguish an attacker layout FROM")


@pytest.fixture
def probe():
    """An attacker-controlled version directory, always removed."""
    root = VERSIONS / "v0.0.0-i28as-control"
    semver = VERSIONS / "v99.99.99"
    shutil.rmtree(root, ignore_errors=True)
    shutil.rmtree(semver, ignore_errors=True)
    try:
        yield {"attacker_named": root, "semver_named": semver}
    finally:
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(semver, ignore_errors=True)
        assert not root.exists() and not semver.exists(), "a control left attacker state behind"


def _write(path: Path, body: str, mode=0o755):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(mode)


def _full_attacker_installation(root: Path, *, header: str | None, copy_node: bool):
    """A layout that satisfies every INTERNAL consistency check an attacker can control."""
    (root / "lib/node_modules/npm/bin").mkdir(parents=True, exist_ok=True)
    (root / "bin").mkdir(parents=True, exist_ok=True)
    _write(root / "lib/node_modules/npm/bin/npm-cli.js",
           "#!/usr/bin/env node\nconsole.log('attacker');\n")
    (root / "lib/node_modules/npm/package.json").write_text(
        json.dumps({"name": "npm", "version": "10.8.2", "bin": {"npm": "bin/npm-cli.js"}}))
    link = root / "bin/npm"
    if not link.exists():
        link.symlink_to("../lib/node_modules/npm/bin/npm-cli.js")
    if copy_node and (GENUINE / "bin/node").exists():
        shutil.copy2(GENUINE / "bin/node", root / "bin/node")
    if header:
        hdr = root / "include/node/node_version.h"
        hdr.parent.mkdir(parents=True, exist_ok=True)
        hdr.write_text(header)


def _verify_with(path_first: Path):
    return na.verify(path_env=f"{path_first}:{os.environ['PATH']}")


# ------------------------------------------------------------------ 1. the prefix rule is GONE
def test_n01_the_prefix_mechanism_is_absent_from_the_code():
    """Not unused — ABSENT. npm was its only user, so deletion makes the defect unreachable.

    The two facts are asserted as ONE equality keyed by stable names. An earlier version asserted
    them separately against local variables, and the assertion-contract analyser was right to
    reject that: `len(reads) == 1` carries no token a contract can pin, so weakening it to
    `len(reads) >= 0` would have been invisible. The meaning now lives in the assertion itself.
    """
    source = (REPO_ROOT / "scripts" / "external_executable_trust.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    # Reads of the retired key. Counting READS rather than the word matters: the refusal message
    # legitimately names the mechanism, and a substring search cannot tell an explanation from an
    # action.
    reads = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr == "get" and n.args
             and isinstance(n.args[0], ast.Constant)
             and n.args[0].value == "approved_path_prefixes"]

    # Assignments emptying the approved path set. Checked on the AST for the same reason: the
    # comment above the removal QUOTES `approved = []` to explain the defect.
    emptied = [n for n in ast.walk(tree)
               if isinstance(n, ast.Assign)
               and any(isinstance(t, ast.Name) and t.id == "approved" for t in n.targets)
               and isinstance(n.value, ast.List) and not n.value.elts]

    summary = {"approved_path_prefixes_reads": len(reads),
               "approved_emptied_assignments": len(emptied)}
    assert summary == {"approved_path_prefixes_reads": 1,
                       "approved_emptied_assignments": 0}, (
        f"{summary}: exactly one READ is expected — the refusal that keeps the retired mechanism "
        f"out of the POLICY — and ZERO assignments emptying the approved path set. That single "
        f"assignment is the whole of ADV-I28AP-02; reads at {[n.lineno for n in reads]}, empties "
        f"at {[n.lineno for n in emptied]}.")


def test_n02_no_policy_entry_carries_a_path_prefix():
    policy = json.loads(TRUST_POLICY.read_text(encoding="utf-8"))
    carriers = [n for n, e in policy["executables"].items() if e.get("approved_path_prefixes")]
    assert carriers == [], f"retired prefix allowance still declared for {carriers}"


def test_n03_a_policy_that_reintroduces_a_prefix_is_refused():
    """Both directions: the code must object even if a fixture smuggles one back."""
    policy = json.loads(TRUST_POLICY.read_text(encoding="utf-8"))
    policy["executables"]["npm"]["approved_path_prefixes"] = ["/tmp/"]
    result = eet.check(policy=policy)
    assert not result["clean"]
    assert any("approved_path_prefixes is retired" in p for p in result["problems"]), \
        result["problems"][:3]


def test_n04_npm_is_delegated_to_the_toolchain_authority():
    entry = json.loads(TRUST_POLICY.read_text(encoding="utf-8"))["executables"]["npm"]
    assert entry["classification"] == "TOOLCHAIN_IDENTITY_DELEGATED"
    assert entry["delegated_to"] == "npm_authority"
    assert entry["bound_before_execution"] is True


def test_n05_node_is_classified_because_the_chain_resolves_it():
    """The executable inventory REFUSED the session until this existed — the I28AJ control working.

    Recorded because it is evidence rather than decoration: resolving the interpreter made `node` a
    statically reachable executable, and the inventory would not let the session start while it was
    unnamed.
    """
    entry = json.loads(TRUST_POLICY.read_text(encoding="utf-8"))["executables"].get("node")
    assert entry is not None, "node is part of the chain that executes graded npm steps"
    assert entry["classification"] == "TOOLCHAIN_IDENTITY_DELEGATED"


# ------------------------------------------------------------------ 2. chain resolution
def test_n06_the_whole_chain_resolves_on_this_host():
    chain = na.resolve_chain()
    if chain.get("path_selected_npm") is None:
        pytest.skip("npm is not installed on this host, so there is no chain to resolve")
    assert chain["problems"] == [], chain["problems"]
    for field in ("canonical_npm", "object_type", "npm_sha256", "mode",
                  "npm_package_root", "npm_package_json_sha256", "npm_declared_version"):
        assert chain.get(field), f"{field} is not bound"


def test_n07_a_wrapper_disagreeing_with_its_package_is_refused(probe):
    """The package says which JavaScript is npm. If the wrapper resolves elsewhere, neither can
    vouch for the other."""
    root = probe["semver_named"]
    _full_attacker_installation(root, header=None, copy_node=False)
    (root / "lib/node_modules/npm/package.json").write_text(
        json.dumps({"name": "npm", "version": "10.8.2", "bin": {"npm": "bin/other-cli.js"}}))
    _write(root / "lib/node_modules/npm/bin/other-cli.js", "#!/usr/bin/env node\n")
    result = _verify_with(root / "bin")
    assert not result["clean"]
    assert any("disagree about which JavaScript runs" in p for p in result["problems"]), \
        result["problems"][:3]


def test_n08_a_broken_symlink_is_refused_not_followed(probe):
    root = probe["semver_named"]
    (root / "bin").mkdir(parents=True)
    (root / "bin/npm").symlink_to("../lib/node_modules/npm/bin/npm-cli.js")   # target absent
    result = _verify_with(root / "bin")
    assert not result["clean"]
    assert any("BROKEN symlink" in p for p in result["problems"]), result["problems"][:3]


def test_n09_an_npm_with_no_package_metadata_is_refused(probe):
    """The exact shape of the original exploit: a bare script named npm in an approved root."""
    root = probe["attacker_named"]
    _write(root / "bin/npm", "#!/bin/sh\necho attacker\nexit 0\n")
    result = _verify_with(root / "bin")
    assert not result["clean"]
    assert any("no npm package root" in p for p in result["problems"]), result["problems"][:3]


def test_n10_a_genuine_version_string_does_not_buy_trust(probe):
    """Self-reported version is the attacker's own claim; nothing here consults it."""
    root = probe["attacker_named"]
    _write(root / "bin/npm",
           "#!/bin/sh\n[ \"$1\" = --version ] && { echo 10.8.2; exit 0; }\nexit 0\n")
    assert not _verify_with(root / "bin")["clean"]


# ------------------------------------------------------------------ 3. NVM provenance
@requires_nvm
def test_n11_a_non_semver_version_directory_is_refused(probe):
    root = probe["attacker_named"]
    _full_attacker_installation(root, header=None, copy_node=True)
    result = _verify_with(root / "bin")
    assert not result["clean"]
    assert any("not a version directory" in p for p in result["problems"]), result["problems"][:3]


@requires_nvm
def test_n12_a_directory_without_a_node_distribution_header_is_refused(probe):
    """Location is not provenance. This is the sentence the whole finding turns on."""
    root = probe["semver_named"]
    _full_attacker_installation(root, header=None, copy_node=True)
    result = _verify_with(root / "bin")
    assert not result["clean"]
    assert any("no include/node/node_version.h" in p for p in result["problems"]), \
        result["problems"][:3]


@requires_nvm
def test_n13_a_header_disagreeing_with_the_directory_name_is_refused(probe):
    root = probe["semver_named"]
    _full_attacker_installation(
        root, copy_node=True,
        header="#define NODE_MAJOR_VERSION 20\n#define NODE_MINOR_VERSION 20\n"
               "#define NODE_PATCH_VERSION 2\n")
    result = _verify_with(root / "bin")
    assert not result["clean"]
    assert any("declares v20.20.2" in p and "named v99.99.99" in p for p in result["problems"]), \
        result["problems"][:3]


@requires_nvm
def test_n14_a_fully_self_consistent_attacker_installation_is_still_refused(probe):
    """THE ESCAPE THIS GATE'S OWN ATTACK MATRIX FOUND, and the most important control here.

    Correct layout, correct symlink, plausible package.json, a copied GENUINE node binary, a semver
    directory name, and a node_version.h that agrees with that name. Every internal check passes,
    because an attacker only has to be consistent with themselves. It is refused because the
    version manager's own metadata references no such installation — the first evidence that is not
    about the directory.
    """
    root = probe["semver_named"]
    _full_attacker_installation(
        root, copy_node=True,
        header="#define NODE_MAJOR_VERSION 99\n#define NODE_MINOR_VERSION 99\n"
               "#define NODE_PATCH_VERSION 99\n")
    result = _verify_with(root / "bin")
    assert not result["clean"], (
        "a self-consistent attacker installation was accepted. Internal consistency is forgeable; "
        "that is precisely why manager metadata is bound.")
    assert any("metadata references none of it" in p for p in result["problems"]), \
        result["problems"][:3]


@requires_nvm
def test_n15_the_manager_metadata_selects_the_genuine_installation():
    selected = na.manager_selected_versions(VERSIONS)
    assert "v20.20.2" in selected, (
        f"the version manager's metadata does not reference the installed Node ({selected}); the "
        "control would then refuse an honest session")


@requires_nvm
def test_n16_an_interpreter_from_another_installation_is_refused(probe, tmp_path):
    """The RELATIONSHIP. A genuine npm beside someone else's node is not a genuine toolchain."""
    elsewhere = tmp_path / "node-only"
    elsewhere.mkdir()
    shutil.copy2(GENUINE / "bin/node", elsewhere / "node")
    result = na.verify(path_env=f"{elsewhere}:{GENUINE / 'bin'}:{os.environ['PATH']}")
    assert not result["clean"]
    assert any("come from different places" in p or "not" in p for p in result["problems"])
    assert any("interpreter resolves to" in p for p in result["problems"]), result["problems"][:3]


@requires_nvm
def test_n17_a_shell_script_standing_in_for_node_is_refused(probe):
    root = probe["semver_named"]
    _full_attacker_installation(
        root, copy_node=False,
        header="#define NODE_MAJOR_VERSION 99\n#define NODE_MINOR_VERSION 99\n"
               "#define NODE_PATCH_VERSION 99\n")
    _write(root / "bin/node", "#!/bin/sh\necho v99.99.99\n")
    result = _verify_with(root / "bin")
    assert not result["clean"]
    assert any("rather than a native executable" in p for p in result["problems"]), \
        result["problems"][:3]


@requires_nvm
def test_n18_the_genuine_installation_is_accepted():
    """The negative control. A check that refuses everything distinguishes nothing."""
    result = na.verify()
    assert result["clean"], result["problems"][:5]
    assert result["family"] == "nvm"
    assert result["authority_model"] == "APPROVED_DIRECTORY_TREE_AND_PROVENANCE_BOUND"


# ------------------------------------------------------------------ 4. families
def test_n19_every_declared_family_carries_an_authority_model():
    doc = na.load_policy()
    assert doc["installation_families"], "an empty family set would authorize vacuously"
    for name, entry in doc["installation_families"].items():
        assert entry["authority_model"] in na.AUTHORITY_MODELS, name
        assert entry["why"], f"{name}: an authority model must state why it holds"


def test_n20_a_bare_prefix_is_not_an_available_authority_model():
    assert not any("PREFIX" in m for m in na.AUTHORITY_MODELS), (
        "a prefix model would reintroduce ADV-I28AP-02 under a new name")


def test_n21_an_unknown_installation_layout_fails_closed_with_an_actionable_message(tmp_path):
    fake = tmp_path / "somewhere/bin"
    _write(fake / "npm", "#!/bin/sh\nexit 0\n")
    (tmp_path / "somewhere/package.json").write_text(json.dumps({"name": "npm", "version": "1"}))
    result = na.verify(path_env=f"{fake}:{os.environ['PATH']}")
    assert not result["clean"]
    joined = " ".join(result["problems"])
    assert "no declared installation family" in joined or "no npm package root" in joined
    if "no declared installation family" in joined:
        assert "do not add a path prefix" in joined, (
            "the message must tell the operator what to do AND what not to do; the obvious wrong "
            "fix here is the defect itself")


def test_n22_an_invalid_authority_model_is_refused(tmp_path):
    doc = json.loads(POLICY.read_text(encoding="utf-8"))
    doc["installation_families"]["nvm"]["authority_model"] = "APPROVED_PATH_PREFIX"
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(doc))
    with pytest.raises(na.NpmAuthorityError, match="not one of"):
        na.load_policy(path)


def test_n23_a_missing_policy_is_refused(tmp_path):
    with pytest.raises(na.NpmAuthorityError, match="policy is missing"):
        na.load_policy(tmp_path / "absent.json")


# ------------------------------------------------------------------ 5. configuration
@pytest.mark.parametrize("var,value", [
    ("NPM_CONFIG_PREFIX", "/tmp/evil"),
    ("NPM_CONFIG_SCRIPT_SHELL", "/tmp/evil-shell"),
    ("NPM_CONFIG_USERCONFIG", "/tmp/evil-npmrc"),
    ("NPM_CONFIG_GLOBALCONFIG", "/tmp/evil-npmrc"),
    ("NPM_CONFIG_REGISTRY", "http://attacker.example"),
    ("NODE_OPTIONS", "--require /tmp/evil.js"),
    ("NODE_PATH", "/tmp/evil"),
    ("npm_config_script_shell", "/tmp/evil-shell"),
])
def test_n24_configuration_that_can_substitute_the_tool_is_refused(var, value, monkeypatch):
    monkeypatch.setenv(var, value)
    result = na.verify()
    assert not result["clean"], f"{var} was accepted although it can change what npm executes"
    assert any(var in p for p in result["problems"]), result["problems"][:3]


def test_n25_an_unclassified_npm_configuration_variable_fails_closed(monkeypatch):
    monkeypatch.setenv("npm_config_i28as_unclassified", "1")
    result = na.verify()
    assert not result["clean"]
    assert any("npm_config_i28as_unclassified" in p for p in result["problems"]), \
        result["problems"][:3]


def test_n26_the_configuration_inventory_reads_the_real_state():
    state = na.configuration_state()
    assert "project_npmrc" in state["files"]
    assert "user_npmrc" in state["files"]
    assert "global_npmrc" in state["files"]
    for label, record in state["files"].items():
        assert record.get("absent") or record.get("sha256"), (
            f"{label}: an existing npmrc must be bound by CONTENT, not merely noticed")


# ------------------------------------------------------------------ 6. lifecycle boundary
def test_n27_every_call_site_carries_a_lifecycle_classification():
    doc = na.load_policy()
    assert doc["call_sites"], "no call site declared, so the boundary adjudicates nothing"
    assert na.lifecycle_problems(doc) == []


def test_n28_a_declared_prohibition_must_be_enforced_not_asserted():
    """A prohibition npm's argv does not implement is worse than an honest classification."""
    doc = json.loads(POLICY.read_text(encoding="utf-8"))
    doc["call_sites"][0]["lifecycle"] = "LIFECYCLE_EXECUTION_PROHIBITED"
    problems = na.lifecycle_problems(doc)
    assert any("does not pass --ignore-scripts" in p for p in problems), problems[:3]


def test_n29_the_gate_does_not_claim_package_script_trust():
    """Scope honesty, asserted rather than left to the reader."""
    doc = na.load_policy()
    assert "not package-script trust" in doc["_lifecycle_boundary"]
    source = " ".join((REPO_ROOT / "scripts" / "npm_authority.py")
                      .read_text(encoding="utf-8").split())
    assert "does NOT vouch for the packages npm installs" in source


# ------------------------------------------------------------------ 7. CI assumption
def test_n30_the_ci_assumption_is_stated_and_matches_the_workflow():
    import yaml

    doc = na.load_policy()["ci_assumption"]
    assert doc["statement"] and "ASSUMES" in doc["statement"]
    workflow = yaml.safe_load((REPO_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    actions, versions = set(), set()
    for job in workflow["jobs"].values():
        for step in job.get("steps") or []:
            if str(step.get("uses", "")).startswith("actions/setup-node"):
                actions.add(step["uses"])
                versions.add(str((step.get("with") or {}).get("node-version")))
    assert actions == {doc["action"]}, (actions, doc["action"])
    assert versions == {"${{ env." + doc["node_version_env"] + " }}"}, versions
    assert workflow["env"][doc["node_version_env"]] == doc["node_version"]


def test_n31_corepack_is_not_engaged_and_the_assumption_says_so():
    """A packageManager field would change the execution chain, so the claim is checked."""
    for manifest in [REPO_ROOT / "package.json", *(REPO_ROOT / "apps").glob("*/package.json")]:
        if manifest.is_file():
            assert "packageManager" not in json.loads(manifest.read_text(encoding="utf-8")), (
                f"{manifest} declares packageManager, so Corepack now mediates npm and the "
                "declared CI assumption no longer describes what executes")
    assert "NOT engaged" in na.load_policy()["ci_assumption"]["corepack"]


def test_n32_a_repository_controlled_npm_is_refused_under_the_ci_assumption(monkeypatch, tmp_path):
    """setup-node provisions npm; an npm the repository supplies afterwards is the substitution."""
    doc = json.loads(POLICY.read_text(encoding="utf-8"))
    chain = {"canonical_npm": str(REPO_ROOT / "node_modules/.bin/npm"),
             "npm_package_root": str(REPO_ROOT / "node_modules/npm")}
    outcome = na._verify_ci_assumption(chain, doc)
    assert any("INSIDE the repository" in p for p in outcome["problems"]), outcome["problems"]


# ------------------------------------------------------------------ 8. session finish
def test_n33_the_snapshot_covers_every_load_bearing_component():
    snap = na.snapshot()
    for field in ("path_selected_npm", "canonical_npm", "npm_sha256", "npm_mode", "symlink_chain",
                  "canonical_node", "node_sha256", "npm_package_root",
                  "npm_package_json_sha256", "path_env_sha256", "nvm_dir", "configuration"):
        assert field in snap, f"{field} is not carried into session-finish comparison"


@pytest.mark.parametrize("field,changed", [
    ("npm_sha256", "deadbeef"),
    ("canonical_npm", "/tmp/other/npm"),
    ("symlink_chain", ["/tmp/redirected"]),
    ("canonical_node", "/tmp/other/node"),
    ("node_sha256", "deadbeef"),
    ("npm_package_json_sha256", "deadbeef"),
    ("npm_package_root", "/tmp/other"),
    ("path_env_sha256", "deadbeef"),
    ("nvm_dir", "/tmp/other-nvm"),
    ("npm_mode", "0o777"),
])
def test_n34_every_late_mutation_is_detected_as_drift(field, changed):
    before = na.snapshot()
    after = dict(before)
    after[field] = changed
    drift = na.compare(before, after)
    assert drift, f"a change to {field} after verification produced no drift"
    assert len(drift) == 1, drift


def test_n35_a_configuration_change_after_verification_is_drift():
    before = na.snapshot()
    after = json.loads(json.dumps(before))
    after["configuration"]["environment"]["NPM_CONFIG_REGISTRY"] = "http://attacker.example"
    assert na.compare(before, after) == ["npm configuration changed after verification"]


def test_n36_an_unchanged_chain_produces_no_drift():
    """The negative control for drift, or every session would fail at finish."""
    assert na.compare(na.snapshot(), na.snapshot()) == []


# ------------------------------------------------------------------ 9. wiring
def test_n37_npm_authority_is_a_protected_module():
    entries = {e["module"]: e for e in
               json.loads(PROTECTED_SET.read_text(encoding="utf-8"))["protected_modules"]}
    entry = entries.get("npm_authority")
    assert entry is not None, "the toolchain authority is not protected, so it can be replaced"
    for name in ("verify", "resolve_chain", "classify_installation", "manager_selected_versions"):
        assert name in entry["critical_callables"], name
    assert entry["relative_path"] == "scripts/npm_authority.py"
    assert entry["proving_substitution"].strip()


def test_n38_the_bootstrap_runs_npm_authority_at_both_trust_boundaries():
    source = (REPO_ROOT / "scripts" / "signalnest_bootstrap.py").read_text(encoding="utf-8")
    assert "npm toolchain identity" in source, "the layer is not in the refusal loop"
    tree = ast.parse(source)
    functions = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    for name in ("establish", "reverify"):
        assert "npm_authority" in ast.dump(functions[name]), (
            f"{name}() does not consult the npm toolchain authority")


def test_n39_the_attestation_carries_the_layers_result():
    """Evidence that it EXECUTED, not that it exists."""
    import signalnest_bootstrap as sb

    attestation = sb.establish(strict=False)
    record = attestation.get("npm_authority")
    assert record is not None, "establish() produced no npm-authority record"
    assert record["clean"] is True, record["problems"][:3]
    assert record["policy_sha256"]
    assert attestation.get("npm_snapshot"), "no snapshot was taken for session-finish comparison"


def test_n40_the_running_session_had_the_layer_active():
    import signalnest_bootstrap as sb

    outcome = sb.reverify()
    assert outcome["layers"].get("npm_authority") is True, outcome["problems"][:3]


# ------------------------------------------------------------------ 10. call-site inventory
def test_n41_every_graded_npm_call_site_is_declared():
    """DERIVED from the workflow with the repository's own command-position grammar.

    Gate 4N-I28AS directed that the previously reported figure of ten graded npm steps not be
    assumed. It was not: the derivation finds eight graded ci.yml steps across TWO jobs, plus three
    developer shell scripts.
    """
    import shell_positions as sp
    import yaml

    workflow = yaml.safe_load((REPO_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    derived = set()
    for job_name, job in workflow["jobs"].items():
        for step in job.get("steps") or []:
            run = step.get("run")
            if not run:
                continue
            for command in sp.scan(run).commands:
                if command.word == "npm":
                    derived.add(job_name)
    assert derived == {"frontend-quality", "migration-and-contract"}, derived

    declared = {s["id"] for s in na.load_policy()["call_sites"]}
    for job in derived:
        assert any(job in d for d in declared), f"no declared call site names the {job} job"


def test_n42_a_call_site_with_an_unknown_lifecycle_class_fails_closed():
    doc = json.loads(POLICY.read_text(encoding="utf-8"))
    doc["call_sites"][0]["lifecycle"] = "PROBABLY_FINE"
    problems = na.lifecycle_problems(doc)
    assert any("is not one of" in p for p in problems), problems[:3]


# ------------------------------------------------------------------ 11. independent oracle
def test_n43_an_independent_chain_oracle_agrees():
    """Resolved with plain shell tools, consuming NO production expected value.

    The oracle must not read the policy or npm_authority's own answer, or it would agree by
    construction.
    """
    npm = shutil.which("npm")
    if npm is None:
        pytest.skip("npm is not installed on this host")
    real = subprocess.run(["python3", "-c",
                           "import os,sys;print(os.path.realpath(sys.argv[1]))", npm],
                          capture_output=True, text=True, timeout=120).stdout.strip()
    chain = na.resolve_chain()
    assert chain["canonical_npm"] == real, (chain["canonical_npm"], real)

    # Walk up to the package root the way a reader would, without consulting production.
    here = Path(real).parent
    found = None
    while here != here.parent:
        candidate = here / "package.json"
        if candidate.is_file() and json.loads(candidate.read_text()).get("name") == "npm":
            found = here
            break
        here = here.parent
    assert chain["npm_package_root"] == (str(found) if found else None)
