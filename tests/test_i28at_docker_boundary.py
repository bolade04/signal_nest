"""Gate 4N-I28AT — Docker steering state and execution boundary, closing ADV-I28AP-01.

THE DEFECT. `external_executable_trust` bound the docker CLI BINARY and nothing else. Which daemon
that binary talked to was chosen by state nothing looked at. Reproduced on this tree before the fix:
DOCKER_HOST, DOCKER_CONTEXT, DOCKER_CONFIG, DOCKER_TLS_VERIFY, DOCKER_CERT_PATH, DOCKER_API_VERSION,
DOCKER_CONTENT_TRUST, BUILDKIT_HOST, DOCKER_BUILDKIT and DOCKER_DEFAULT_PLATFORM were EACH accepted
silently with all eight layers plus session-finish reverification clean; a hostile config.json and a
context store pointing at `tcp://attacker.example:2375` were accepted too; and a repository-wide
search for any of those names returned NOTHING.

THE MODEL IS B — EXTERNAL CI INFRASTRUCTURE ASSUMPTION, forced by measurement. This host has no
docker CLI, no ~/.docker, no socket, no context store and no Docker Desktop, and the gate forbids
installing Docker. Model A would require binding a daemon-reported identity that cannot be obtained
here; claiming it would be inventing evidence.

WHAT THESE CONTROLS PROVE, and what they deliberately do not. They prove that no
repository-controlled mechanism can redirect Docker away from the assumed daemon, that the
assumption is machine-enforced rather than prose, and that the whole state is re-compared at session
finish. They do NOT prove anything about the daemon itself — that is the assumption, stated as one.

Every control that writes attacker state confines it to a temporary directory and restores the
environment in a `finally`. Nothing here touches a real Docker installation, because there is none.
"""

from __future__ import annotations

import ast
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import docker_boundary as db                                     # noqa: E402

POLICY = REPO_ROOT / "tests" / "fixtures" / "docker-boundary-policy.json"
PROTECTED_SET = REPO_ROOT / "tests" / "fixtures" / "protected-module-set.json"


@pytest.fixture
def clean_env():
    """Restore the environment however a control ends."""
    saved = dict(os.environ)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


@pytest.fixture
def config_store(tmp_path):
    """A synthetic Docker config directory. There is no real one on this host."""
    root = tmp_path / "docker"
    (root / "contexts" / "meta" / "abc").mkdir(parents=True)
    return root


def _state_for(config_dir: Path) -> dict:
    """Steering state pointed at a synthetic store, WITHOUT setting DOCKER_CONFIG.

    Setting the variable would make DOCKER_CONFIG's own FATAL_IF_PRESENT rule fire first and mask
    the config-FIELD adjudication — a probe that refuses for a different reason than the one it
    names measures nothing. This exercises the field rules directly.
    """
    state = db.steering_state()
    state["config_dir"] = str(config_dir)
    state["config_file_exists"] = (config_dir / "config.json").is_file()
    state["config_is_symlink"] = (config_dir / "config.json").is_symlink()
    state["contexts"] = []
    return state


# ------------------------------------------------------------------ 1. model and assumption
def test_d01_the_model_is_declared_and_coherent():
    doc = db.load_policy()
    assert doc["model"] == "MODEL_B_EXTERNAL_CI_ASSUMPTION"
    assert doc["_model_choice"], "the model choice must state why it was chosen"
    assert "no docker CLI" in doc["_model_choice"] or "MEASUREMENT" in doc["_model_choice"]


def test_d02_the_assumption_is_machine_enforced_not_prose():
    """A prose-only assumption is explicitly insufficient, so its fields are structural."""
    assumption = db.load_policy()["ci_assumption"]
    for field in ("version", "statement", "runner", "marker_env", "workflows",
                  "daemon_provisioned_by", "not_claimed", "failure_behaviour"):
        assert assumption.get(field), field
    assert isinstance(assumption["marker_env"], list) and assumption["marker_env"]


@pytest.mark.parametrize("missing", ["version", "statement", "runner", "marker_env",
                                     "workflows", "daemon_provisioned_by", "not_claimed"])
def test_d03_an_incomplete_assumption_is_refused(missing, tmp_path):
    doc = json.loads(POLICY.read_text(encoding="utf-8"))
    doc["ci_assumption"].pop(missing)
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(doc))
    with pytest.raises(db.DockerBoundaryError, match="machine-enforced|missing"):
        db.load_policy(path)


def test_d04_the_gate_does_not_claim_daemon_identity():
    """The one thing this model must never assert."""
    doc = db.load_policy()
    not_claimed = " ".join(doc["_what_is_not_claimed"] + doc["ci_assumption"]["not_claimed"])
    for thing in ("daemon", "registry", "TLS", "BuildKit", "SSH", "credential-helper"):
        assert thing.lower() in not_claimed.lower(), thing
    source = " ".join((REPO_ROOT / "scripts" / "docker_boundary.py")
                      .read_text(encoding="utf-8").split())
    assert "does not pretend to identify the daemon" in source


# ------------------------------------------------------------------ 2. call-site inventory
def test_d05_every_derived_call_site_is_classified():
    result = db.verify()
    assert result["clean"], result["problems"][:5]
    assert result["call_site_count"] == 50, (
        f"the Docker call-site inventory is {result['call_site_count']}, not the derived 50")


def test_d06_no_call_site_inherits_another_classification():
    doc = db.load_policy()
    for site in doc["call_sites"]:
        assert site["trust_boundary"] in db.TRUST_BOUNDARIES
        assert site["why"], f"{site['id']} carries no rationale"
        assert site["failure_behaviour"] and site["session_finish_obligation"]


def test_d07_an_unclassified_call_site_fails_closed():
    doc = json.loads(POLICY.read_text(encoding="utf-8"))
    dropped = doc["call_sites"].pop()
    result = db.verify(policy=doc)
    assert not result["clean"]
    assert any(dropped["id"] in p and "NO trust-boundary classification" in p
               for p in result["problems"]), result["problems"][:3]


def test_d08_a_classification_for_a_vanished_call_site_fails_closed():
    doc = json.loads(POLICY.read_text(encoding="utf-8"))
    doc["call_sites"].append(dict(doc["call_sites"][0], id="ghost.yml#gone#0#1"))
    result = db.verify(policy=doc)
    assert not result["clean"]
    assert any("does not exist" in p for p in result["problems"]), result["problems"][:3]


def test_d09_an_invalid_trust_boundary_is_refused(tmp_path):
    doc = json.loads(POLICY.read_text(encoding="utf-8"))
    doc["call_sites"][0]["trust_boundary"] = "PROBABLY_FINE"
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(doc))
    with pytest.raises(db.DockerBoundaryError, match="not one of"):
        db.load_policy(path)


def test_d10_the_derivation_is_a_superset_of_the_shared_deriver():
    """ADV-I28AT-01: the shared deriver silently drops a graded Docker call site.

    This gate does not fix `shell_positions` — that is outside its authorized scope — so it derives
    the Docker inventory itself and REQUIRES its own set to be a superset. A silent disagreement
    between two components that both claim to know where Docker runs is worse than either being
    wrong alone.
    """
    reconciliation = db.reconcile_with_shared_deriver()
    assert reconciliation["problems"] == [], reconciliation["problems"]

    # The SUPERSET requirement is the control and it is unchanged: this module must never find
    # fewer Docker call sites than the shared deriver.
    assert reconciliation["docker_boundary_sites"] >= reconciliation["shell_positions_sites"]

    # UPDATED at Gate 4N-I28AV. This previously asserted `difference == 1`, pinning the SIZE of
    # ADV-I28AT-01 — the shared deriver missed exactly one graded Docker call site because `esac`
    # never cleared the case-pattern skip. That defect is now fixed, so the difference is 0 and the
    # two derivations agree at 50.
    #
    # Pinning the defect's magnitude was the wrong shape for a control: it turned "the bug still
    # exists" into a passing condition, so REPAIRING the parser broke the test. What matters is the
    # superset property above, which is asserted unconditionally, plus the agreement below. The
    # independent Docker derivation is NOT weakened just because the shared parser now agrees with
    # it — §19 is explicit on that, and the superset check remains the enforcement.
    assert reconciliation["difference"] == 0, (
        "the shared deriver and the Docker derivation are expected to AGREE now that "
        f"ADV-I28AT-01 is fixed; they differ by {reconciliation['difference']}")


# ------------------------------------------------------------------ 3. steering environment
@pytest.mark.parametrize("var,value", [
    ("DOCKER_HOST", "tcp://attacker.example:2375"),
    ("DOCKER_CONTEXT", "attacker"),
    ("DOCKER_CONFIG", "/tmp/evil"),
    ("DOCKER_TLS", "1"),
    ("DOCKER_TLS_VERIFY", "0"),
    ("DOCKER_CERT_PATH", "/tmp/certs"),
    ("DOCKER_API_VERSION", "1.20"),
    ("DOCKER_CONTENT_TRUST", "0"),
    ("DOCKER_CONTENT_TRUST_SERVER", "https://attacker.example"),
    ("BUILDKIT_HOST", "tcp://attacker.example:1234"),
    ("DOCKER_BUILDKIT", "0"),
    ("COMPOSE_DOCKER_CLI_BUILD", "0"),
    ("DOCKER_DEFAULT_PLATFORM", "linux/386"),
])
def test_d11_every_redirecting_variable_is_refused(var, value, clean_env):
    os.environ[var] = value
    result = db.verify()
    assert not result["clean"], f"{var} was accepted although it can redirect Docker"
    assert any(var in p for p in result["problems"]), result["problems"][:3]


@pytest.mark.parametrize("var", ["DOCKER_MYSTERY_KNOB", "BUILDKIT_FUTURE_OPTION",
                                 "COMPOSE_SOMETHING_NEW"])
def test_d12_an_unclassified_steering_variable_fails_closed(var, clean_env):
    """The first implementation collected only variables the policy already named, so this branch
    was unreachable code and `DOCKER_MYSTERY_KNOB` sailed through. A control that can only see what
    it already knows about cannot fail closed on the unknown."""
    os.environ[var] = "1"
    result = db.verify()
    assert not result["clean"]
    assert any(var in p and "no disposition classifies it" in p for p in result["problems"]), \
        result["problems"][:3]


def test_d13_a_clean_environment_is_accepted():
    """The negative control. A boundary that refuses everything distinguishes nothing."""
    result = db.verify()
    assert result["clean"], result["problems"][:5]


def test_d14_every_declared_steering_mechanism_carries_a_disposition():
    doc = db.load_policy()
    assert doc["steering"], "an empty steering inventory would adjudicate nothing"
    for name, entry in doc["steering"].items():
        assert entry["disposition"] in db.DISPOSITIONS, name
        assert entry["why"], f"{name}: a disposition must state why it holds"


# ------------------------------------------------------------------ 4. configuration
@pytest.mark.parametrize("field,value", [
    ("currentContext", "attacker"),
    ("credsStore", "evil-helper"),
    ("credHelpers", {"registry.example": "evil-helper"}),
    ("auths", {"registry.example": {"auth": "eA=="}}),
    ("cliPluginsExtraDirs", ["/tmp/evil-plugins"]),
    ("plugins", {"buildx": {"path": "/tmp/evil"}}),
    ("proxies", {"default": {"httpProxy": "http://attacker.example:8080"}}),
    ("experimental", "enabled"),
])
def test_d15_a_redirecting_config_field_is_refused(field, value, config_store):
    (config_store / "config.json").write_text(json.dumps({field: value}))
    problems = db.config_problems(_state_for(config_store), db.load_policy())
    assert problems, f"config.json {field} was accepted although it can redirect or inject"
    assert any(field in p for p in problems), problems[:3]


def test_d16_an_unclassified_config_field_fails_closed(config_store):
    """Same fail-open as the steering collector had, in the other direction: extracting only known
    keys made the refusal branch unreachable. Docker adds keys over time; the one this gate has
    never heard of is exactly the one worth refusing."""
    (config_store / "config.json").write_text(json.dumps({"someFutureField": "x"}))
    problems = db.config_problems(_state_for(config_store), db.load_policy())
    assert any("someFutureField" in p and "no rule classifies it" in p for p in problems), problems


@pytest.mark.parametrize("field", ["detachKeys", "psFormat", "imagesFormat", "HttpHeaders"])
def test_d17_a_benign_config_field_is_accepted(field, config_store):
    """A control that refuses an honest CI config is a control someone switches off."""
    (config_store / "config.json").write_text(json.dumps({field: "value"}))
    assert db.config_problems(_state_for(config_store), db.load_policy()) == []


def test_d18_an_unparseable_config_fails_closed(config_store):
    (config_store / "config.json").write_text("{not json")
    problems = db.config_problems(_state_for(config_store), db.load_policy())
    assert any("cannot be parsed" in p for p in problems), problems


def test_d19_a_symlinked_config_is_refused(config_store, tmp_path):
    real = tmp_path / "real-config.json"
    real.write_text("{}")
    (config_store / "config.json").symlink_to(real)
    state = _state_for(config_store)
    problems = db.config_problems(state, db.load_policy())
    assert any("SYMLINK" in p for p in problems), problems


def test_d20_no_config_at_all_is_accepted(config_store):
    """Docker absent and unconfigured is the honest state of this host."""
    assert db.config_problems(_state_for(config_store), db.load_policy()) == []


# ------------------------------------------------------------------ 5. contexts
def test_d21_a_defined_context_is_refused(config_store, clean_env):
    (config_store / "config.json").write_text("{}")
    (config_store / "contexts/meta/abc/meta.json").write_text(json.dumps(
        {"Name": "attacker",
         "Endpoints": {"docker": {"Host": "tcp://attacker.example:2375", "SkipTLSVerify": True}}}))
    os.environ["DOCKER_CONFIG"] = str(config_store)
    state = db.steering_state()
    problems = db.config_problems(state, db.load_policy())
    assert any("CONTEXT is defined" in p for p in problems), problems
    assert any("tcp://attacker.example:2375" in p for p in problems), problems


def test_d22_an_unparseable_context_is_refused(config_store, clean_env):
    (config_store / "config.json").write_text("{}")
    (config_store / "contexts/meta/abc/meta.json").write_text("{broken")
    os.environ["DOCKER_CONFIG"] = str(config_store)
    problems = db.config_problems(db.steering_state(), db.load_policy())
    assert any("cannot be parsed" in p for p in problems), problems


def test_d23_no_context_store_is_the_expected_state():
    state = db.steering_state()
    assert state["contexts"] == [], (
        "a Docker context is defined in this environment; under the external-CI assumption the "
        "repository environment must define none")


# ------------------------------------------------------------------ 6. invocation flags
@pytest.mark.parametrize("flag", list(db.STEERING_FLAGS))
def test_d24_a_steering_flag_on_a_graded_argv_is_refused(flag):
    doc = db.load_policy()
    sites = [{"id": "probe#1", "argv": f"docker {flag} tcp://attacker.example:2375 run --rm x"}]
    problems = db.flag_problems(sites, doc)
    assert problems, f"{flag} was accepted on a graded argv"
    assert flag in problems[0]


def test_d25_no_real_call_site_carries_a_steering_flag():
    sites = db.derive_call_sites()["sites"]
    assert db.flag_problems(sites, db.load_policy()) == []


def test_d26_a_flag_expanding_variable_is_a_blocker():
    """A dynamic steering flag cannot be adjudicated statically, so it fails closed."""
    doc = db.load_policy()
    assert doc["flag_expanding_variables"], "no variable is declared capable of carrying flags"
    name = doc["flag_expanding_variables"][0]
    problems = db.flag_problems([{"id": "probe#2", "argv": f"docker run ${name} image"}], doc)
    assert any("dynamic steering flag is a blocker" in p for p in problems), problems


# ------------------------------------------------------------------ 7. availability and CI marker
def test_d27_docker_is_genuinely_absent_on_this_host():
    """Recorded as a FACT, because the model choice rests on it."""
    assert shutil.which("docker") is None
    assert not (Path.home() / ".docker").exists()
    assert not Path("/var/run/docker.sock").exists()


def test_d28_absence_is_permitted_only_outside_the_graded_path(clean_env):
    assert db.verify()["clean"], "Docker absence outside CI must not fail an honest session"
    os.environ.update({"GITHUB_ACTIONS": "true", "GITHUB_RUN_ID": "1", "GITHUB_WORKFLOW": "ci"})
    result = db.verify()
    assert not result["clean"], (
        "with the CI marker set the graded Docker path is ACTIVE, so an absent client must fail "
        "BEFORE the first Docker call rather than being discovered by it")
    assert any("no docker executable is resolvable" in p for p in result["problems"])


def test_d29_a_partially_forged_ci_marker_is_refused(clean_env):
    os.environ["GITHUB_RUN_ID"] = "1"
    problems = db.ci_assumption_problems(db.steering_state(), db.load_policy())
    assert any("partially forged" in p for p in problems), problems


def test_d30_a_forged_marker_value_is_refused(clean_env):
    os.environ.update({"GITHUB_ACTIONS": "yes", "GITHUB_RUN_ID": "1", "GITHUB_WORKFLOW": "ci"})
    problems = db.ci_assumption_problems(db.steering_state(), db.load_policy())
    assert any("requires 'true'" in p for p in problems), problems


def test_d31_outside_ci_the_assumption_is_not_in_force(clean_env):
    for name in ("GITHUB_ACTIONS", "GITHUB_RUN_ID", "GITHUB_WORKFLOW"):
        os.environ.pop(name, None)
    assert db.ci_assumption_problems(db.steering_state(), db.load_policy()) == []


# ------------------------------------------------------------------ 8. session finish
def test_d32_the_snapshot_covers_every_load_bearing_component():
    snap = db.snapshot()
    for field in ("model", "assumption_version", "call_site_count", "docker_on_path",
                  "docker_sha256", "path_env_sha256", "steering_environment", "config_dir",
                  "config_sha256", "contexts", "ci_marker", "policy_sha256"):
        assert field in snap, field


@pytest.mark.parametrize("field,changed", [
    ("model", "MODEL_A_REPOSITORY_VERIFIED"),
    ("assumption_version", "9999.9"),
    ("call_site_count", 3),
    ("docker_on_path", "/tmp/docker"),
    ("docker_sha256", "deadbeef"),
    ("path_env_sha256", "deadbeef"),
    ("steering_environment", {"DOCKER_HOST": "tcp://x"}),
    ("config_dir", "/tmp/other"),
    ("config_sha256", "deadbeef"),
    ("contexts", ["deadbeef"]),
    ("ci_marker", {"GITHUB_ACTIONS": "true"}),
    ("policy_sha256", "deadbeef"),
])
def test_d33_every_late_mutation_is_detected_as_drift(field, changed):
    before = db.snapshot()
    after = dict(before)
    after[field] = changed
    drift = db.compare(before, after)
    assert drift, f"a change to {field} after verification produced no drift"
    assert len(drift) == 1, drift


def test_d34_an_unchanged_boundary_produces_no_drift():
    assert db.compare(db.snapshot(), db.snapshot()) == []


# ------------------------------------------------------------------ 9. wiring
def test_d35_docker_boundary_is_a_protected_module():
    entries = {e["module"]: e for e in
               json.loads(PROTECTED_SET.read_text(encoding="utf-8"))["protected_modules"]}
    entry = entries.get("docker_boundary")
    assert entry is not None
    for name in ("verify", "derive_call_sites", "steering_state", "config_problems",
                 "ci_assumption_problems"):
        assert name in entry["critical_callables"], name
    assert entry["proving_substitution"].strip()


def test_d36_the_bootstrap_runs_the_boundary_at_both_trust_boundaries():
    source = (REPO_ROOT / "scripts" / "signalnest_bootstrap.py").read_text(encoding="utf-8")
    assert "docker execution boundary" in source, "the layer is not in the refusal loop"
    tree = ast.parse(source)
    functions = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    for name in ("establish", "reverify"):
        assert "docker_boundary" in ast.dump(functions[name]), name


def test_d37_the_attestation_carries_the_layers_result():
    import signalnest_bootstrap as sb

    attestation = sb.establish(strict=False)
    record = attestation.get("docker_boundary")
    assert record is not None
    assert record["clean"] is True, record["problems"][:3]
    assert record["model"] == "MODEL_B_EXTERNAL_CI_ASSUMPTION"
    assert record["call_site_count"] == 50
    assert attestation.get("docker_snapshot")


def test_d38_the_running_session_had_the_layer_active():
    import signalnest_bootstrap as sb

    outcome = sb.reverify()
    assert outcome["layers"].get("docker_boundary") is True, outcome["problems"][:3]


# ------------------------------------------------------------------ 10. independent oracle
def test_d39_an_independent_call_site_oracle_agrees():
    """Textual, deliberately naive, and consuming no production expected value."""
    import re

    oracle = 0
    for path in (sorted((REPO_ROOT / ".github/workflows").glob("*.yml"))
                 + sorted((REPO_ROOT / "scripts").rglob("*.sh"))):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("#"):
                continue
            oracle += len([m for m in re.finditer(r"\bdocker\s+([a-z][a-z-]*)", line)
                           if m.group(1) in ("run", "inspect", "push", "tag", "create", "rm",
                                             "cp", "build", "login", "exec")])
    assert oracle == db.derive_call_sites()["count"], (
        f"the independent textual oracle counts {oracle} Docker invocations, the derivation counts "
        f"{db.derive_call_sites()['count']}; one of the two is wrong and neither may be tuned")


def test_d40_an_independent_steering_oracle_agrees(clean_env):
    """Enumerate the environment directly, without consulting the collector."""
    os.environ["DOCKER_HOST"] = "tcp://x:1"
    os.environ["DOCKER_MYSTERY"] = "1"
    oracle = {k for k in os.environ if k.startswith(("DOCKER_", "BUILDKIT_", "COMPOSE_"))}
    collected = set(db.steering_state()["environment"])
    assert oracle <= collected, sorted(oracle - collected)
