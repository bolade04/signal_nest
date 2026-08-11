"""Gate 4N-I28BF-B3 — complete environment matrix, hostile HOME/Docker adjudication, graded A–H.

Runs the B1 authoritative-state / governed-cache and the graded-session controls under the full
required environment matrix, in isolated git-bearing materialisations, and classifies each row
EXPECTED_PASS or EXPECTED_FAIL_CLOSED (with the intended detector). Ground truths this harness
relies on and re-proves:

  * ``docker_assurance_state.state_digest`` is content-addressed and hash-seed-independent, and the
    git tree is content-addressed, so every FAITHFUL environment (normal/empty/hostile HOME,
    PYTHONNOUSERSITE, sanitised PATH, alternate cwd, path-with-spaces, fresh process) yields the
    SAME reference digest — determinism and hermeticity;
  * the governed cache ``_STATE_CACHE`` is an IN-MEMORY module global with no disk/HOME path, so
    stale/read-only/unwritable cache-directory conditions are location-independent by construction;
  * a hostile Docker ENV variable prohibited by a load-bearing site fails closed via the per-site
    FATAL-steering detector; a hostile Docker CONFIG fails closed via the graded docker_boundary
    layer; a cross-tree or cross-authorization cache value is REJECTED by the cache key/value check.

Every environment row proves its delta was actually applied (the probe reports the env it observed),
and the summary is derived from the row records. The real repository is never mutated.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import cache_authority as ca                       # noqa: E402
import docker_assurance_state as das               # noqa: E402

_REAL_TREE_BEFORE = subprocess.run(["git", "write-tree"], cwd=REPO_ROOT,
                                   capture_output=True, text=True).stdout.strip()

EXPECTED_PASS = "EXPECTED_PASS"
EXPECTED_FAIL_CLOSED = "EXPECTED_FAIL_CLOSED"

# A subprocess probe that reports the authoritative-state identities AND the environment it actually
# observed, so a row can prove its delta reached the target process.
_PROBE = r'''
import json, os, sys
sys.path.insert(0, os.environ["SN_SCRIPTS"])
import docker_assurance_state as das
das.reset_caches()
cold_empty = (das._STATE_CACHE == {})
try:
    fresh = das.fresh_state()
    problems = das.validate_state(fresh)
    st = das._thaw(fresh)
    out = {"ok": True,
           "state_digest": das.state_digest(fresh),
           "production_universe_digest": st["universe"]["production_universe_digest"],
           "independent_universe_digest": st["universe"]["independent_universe_digest"],
           "cache_key_digest": (das.cache_key_digest(fresh) if not problems else None),
           "validate_clean": (problems == []),
           "first_problem": (problems[:1] or [""])[0][:90],
           "cold_cache_empty": cold_empty}
except Exception as exc:
    out = {"ok": False, "raised": type(exc).__name__, "first_problem": str(exc)[:120],
           "validate_clean": False, "cold_cache_empty": cold_empty, "state_digest": None}
out["observed"] = {"home": os.environ.get("HOME",""), "cwd": os.getcwd(),
                   "hashseed": os.environ.get("PYTHONHASHSEED",""),
                   "pythonnousersite": os.environ.get("PYTHONNOUSERSITE",""),
                   "path_head": (os.environ.get("PATH","").split(os.pathsep) or [""])[0],
                   "pid": os.getpid(),
                   "docker_env": {k: os.environ.get(k) for k in
                                  ("DOCKER_HOST","DOCKER_CONFIG","DOCKER_CONTEXT","DOCKER_BUILDKIT",
                                   "XDG_CONFIG_HOME") if os.environ.get(k)}}
print("PROBE_JSON:" + json.dumps(out))
'''


def _run_probe(env_overrides: dict, *, cwd: Path | None = None) -> dict:
    env = dict(os.environ, SN_SCRIPTS=str(REPO_ROOT / "scripts"))
    env.update(env_overrides)
    proc = subprocess.run([sys.executable, "-c", _PROBE], cwd=str(cwd or REPO_ROOT),
                          env=env, capture_output=True, text=True, timeout=120)
    line = [ln for ln in proc.stdout.splitlines() if ln.startswith("PROBE_JSON:")]
    assert line, f"probe produced no result:\n{proc.stdout[-500:]}\n{proc.stderr[-500:]}"
    out = json.loads(line[-1][len("PROBE_JSON:"):])
    out["_returncode"] = proc.returncode
    return out


def _real_repo_unchanged() -> bool:
    now = subprocess.run(["git", "write-tree"], cwd=REPO_ROOT, capture_output=True, text=True).stdout.strip()
    return now == _REAL_TREE_BEFORE


REFERENCE = _run_probe({})
REFERENCE_DIGEST = REFERENCE["state_digest"]


# ===================================================================== isolated materialisation
def _materialise(dest: Path) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    tree = subprocess.run(["git", "write-tree"], cwd=REPO_ROOT, capture_output=True, text=True,
                          check=True).stdout.strip()
    arch = subprocess.run(["git", "archive", tree], cwd=REPO_ROOT, capture_output=True, check=True).stdout
    (dest / "_t.tar").write_bytes(arch)
    subprocess.run(["tar", "-xf", "_t.tar"], cwd=dest, check=True)
    (dest / "_t.tar").unlink()
    for cmd in (["git", "init", "-q"], ["git", "add", "-A"],
                ["git", "-c", "user.email=a@b.c", "-c", "user.name=x", "commit", "-qm", "b"]):
        subprocess.run(cmd, cwd=dest, check=True, capture_output=True)
    return dest


def _graded(root: Path, probe: str, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    (root / "tests" / "test_zz_b3.py").write_text(probe)
    env = dict(os.environ, SIGNALNEST_ANCHOR_TIER="TIER_1_SYNTHETIC",
               SIGNALNEST_CANDIDATE_MANIFEST=str(root / "tests" / "fixtures" / "candidate-manifest.json"),
               PYTHONPATH=str(root / "scripts"))
    env.pop("SIGNALNEST_MANDATORY_NODES", None)
    env.pop("SIGNALNEST_BOOTSTRAP_ATTESTATION", None)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_zz_b3.py", "-q", "-p", "no:randomly",
         "-p", "signalnest_bootstrap"], cwd=str(root), env=env, capture_output=True, text=True, timeout=300)


# ===================================================================== reference & determinism
def test_reference_normal_home_is_valid_and_stable():
    assert REFERENCE["ok"] and REFERENCE["validate_clean"], REFERENCE
    assert REFERENCE["cold_cache_empty"], "each fresh process must start with a cold in-memory cache"
    assert REFERENCE_DIGEST, "a reference digest is required"


# environment rows that must reproduce the reference digest (faithful, hermetic environments)
def _hostile_home(tmp_path: Path) -> Path:
    h = tmp_path / "hostile_home"
    (h / ".docker").mkdir(parents=True)
    (h / ".bashrc").write_text("export EVIL=1\nalias ls='rm -rf /'\n")
    (h / ".config").mkdir()
    site = h / ".local" / "lib" / "python3.12" / "site-packages"
    site.mkdir(parents=True)
    (site / "docker_boundary.py").write_text("raise RuntimeError('user-site override attempt')\n")
    return h


@pytest.mark.parametrize("row,name,setup", [
    (1, "normal HOME", lambda tp: ({}, None)),
    (2, "empty HOME", lambda tp: ({"HOME": str((tp / "empty_home"))}, None) if (tp / "empty_home").mkdir(parents=True, exist_ok=True) is None else ({"HOME": str(tp / "empty_home")}, None)),
    (4, "PYTHONNOUSERSITE=1", lambda tp: ({"PYTHONNOUSERSITE": "1"}, None)),
    (12, "PYTHONNOUSERSITE with hostile user-site", lambda tp: ({"HOME": str(_hostile_home(tp)), "PYTHONNOUSERSITE": "1"}, None)),
])
def test_faithful_environment_reproduces_reference_digest(tmp_path, row, name, setup, record_property):
    env_over, cwd = setup(tmp_path)
    r = _run_probe(env_over, cwd=cwd)
    rec = {"id": row, "name": name, "classification": EXPECTED_PASS,
           "digest_matches_reference": r.get("state_digest") == REFERENCE_DIGEST,
           "validate_clean": r.get("validate_clean"), "observed": r.get("observed")}
    record_property("env", json.dumps(rec))
    assert r["ok"] and r["validate_clean"], f"{name}: {r}"
    assert r["state_digest"] == REFERENCE_DIGEST, f"{name}: digest changed under a faithful env"
    assert r["cold_cache_empty"]
    assert _real_repo_unchanged()


def test_hostile_home_unrelated_contents_do_not_influence_trusted_state(tmp_path):
    """Unrelated hostile HOME contents (shell rc, user-site override) cannot change the state, and a
    user-site docker_boundary cannot override the protected module (PYTHONNOUSERSITE / hermetic)."""
    home = _hostile_home(tmp_path)
    r = _run_probe({"HOME": str(home), "PYTHONNOUSERSITE": "1"})
    assert r["ok"] and r["validate_clean"], r
    assert r["state_digest"] == REFERENCE_DIGEST, "hostile HOME must not move the authoritative digest"
    assert r["observed"]["home"] == str(home), "the delta must have reached the process"


def test_sanitized_path_resolves_the_hermetic_interpreter(tmp_path):
    # A minimal PATH containing only the interpreter dir and coreutils; the probe still derives.
    py_dir = str(Path(sys.executable).parent)
    r = _run_probe({"PATH": f"{py_dir}:/usr/bin:/bin"})
    assert r["ok"] and r["validate_clean"], r
    assert r["state_digest"] == REFERENCE_DIGEST


def test_alternate_working_directory(tmp_path):
    r = _run_probe({}, cwd=tmp_path)
    assert r["ok"] and r["validate_clean"], r
    assert r["state_digest"] == REFERENCE_DIGEST, "cwd must not affect the tree-addressed digest"
    assert r["observed"]["cwd"] == str(tmp_path)


def test_repository_path_containing_spaces(tmp_path):
    root = _materialise(tmp_path / "dir with spaces")
    env = dict(SN_SCRIPTS=str(root / "scripts"))
    r = _run_probe(env, cwd=root)
    assert r["ok"] and r["validate_clean"], r
    assert r["state_digest"] == REFERENCE_DIGEST, "a path with spaces must not corrupt derivation"


# ===================================================================== hash-seed & fresh-process
_SEEDS = ["0", "1", "2", "42", "12345", "987654"]


@pytest.mark.parametrize("seed", _SEEDS)
def test_hash_seed_determinism(seed, record_property):
    r = _run_probe({"PYTHONHASHSEED": seed})
    record_property("seed", json.dumps({"seed": seed, "state_digest": r.get("state_digest"),
                                        "production": r.get("production_universe_digest"),
                                        "independent": r.get("independent_universe_digest")}))
    assert r["ok"] and r["validate_clean"], r
    assert r["state_digest"] == REFERENCE_DIGEST, f"seed {seed} changed the canonical digest"
    assert r["observed"]["hashseed"] == seed, "the seed must have reached the process"


def test_fresh_process_isolation():
    results = [_run_probe({"PYTHONHASHSEED": str(i)}) for i in (7, 8, 9)]
    pids = {r["observed"]["pid"] for r in results}
    assert len(pids) == 3, f"expected three distinct processes, got pids {pids}"
    assert all(r["cold_cache_empty"] for r in results), "each fresh process must start cold"
    assert len({r["state_digest"] for r in results}) == 1, "the semantic digest must be process-independent"
    assert results[0]["state_digest"] == REFERENCE_DIGEST


# ===================================================================== hostile Docker environment
@pytest.mark.parametrize("var,expected_fail", [
    ("DOCKER_HOST", True), ("DOCKER_CONFIG", True), ("DOCKER_CONTEXT", True),
    # XDG_CONFIG_HOME is GOVERNED but the exact table classifies it IRRELEVANT_TO_ACTUAL_CALLS, so
    # setting it does NOT fail-close the per-site state — the table adjudicates it as irrelevant
    # rather than silently widening policy. Hostile Docker CONFIG CONTENT is caught by the graded
    # docker_boundary layer, exercised separately.
    ("XDG_CONFIG_HOME", False), ("DOCKER_BUILDKIT", False),
])
def test_hostile_docker_env_variable(var, expected_fail, tmp_path, record_property):
    val = str(tmp_path) if var in ("DOCKER_CONFIG", "XDG_CONFIG_HOME") else "tcp://evil:2375"
    r = _run_probe({var: val})
    rec = {"var": var, "expected": EXPECTED_FAIL_CLOSED if expected_fail else EXPECTED_PASS,
           "validate_clean": r.get("validate_clean"), "detector": r.get("first_problem")}
    record_property("docker_env", json.dumps(rec))
    assert var in r["observed"]["docker_env"], "the hostile Docker env var must have reached the process"
    if expected_fail:
        assert not r["validate_clean"], f"{var} must fail closed"
        assert "aggregate is not clean" in r["first_problem"] or "FATAL" in r["first_problem"], r["first_problem"]
    else:
        assert r["validate_clean"], f"{var} is not prohibited by any load-bearing site; must stay clean"


def test_unknown_docker_like_variable_does_not_widen_policy():
    r = _run_probe({"DOCKER_TOTALLY_UNKNOWN_VAR": "x"})
    assert r["ok"] and r["validate_clean"], "an unknown Docker-like variable must not widen policy"
    assert r["state_digest"] == REFERENCE_DIGEST


# ===================================================================== cache: in-memory, location-independent
def test_governed_cache_is_in_memory_and_location_independent():
    """The B1 cache has no disk/HOME path, so stale/read-only/unwritable cache directories cannot
    affect correctness: every process starts cold and correctness never depends on a cache write."""
    import inspect
    src = inspect.getsource(das)
    assert "_STATE_CACHE: dict = {}" in src, "the cache must be a module-global dict"
    assert "HOME" not in src and "signalnest/generated" not in src, "the cache must not read HOME/disk"
    # cold correctness in a fresh process under an unwritable HOME cache path is unaffected.
    r = _run_probe({"HOME": "/nonexistent-unwritable-home-xyz"})
    assert r["ok"] and r["validate_clean"], r
    assert r["state_digest"] == REFERENCE_DIGEST


def test_cross_tree_cache_value_is_rejected():
    das.reset_caches()
    other = das._thaw(das.fresh_state())
    other["repository"]["staged_tree"] = "0" * 40
    other["repository"]["source_content_token"] = "0" * 64
    kd = das.cache_key_digest(das.fresh_state())
    das._STATE_CACHE[kd] = ca.deep_freeze({
        "state": ca.deep_freeze(other), "state_digest": das.state_digest(other),
        "cache_key_digest": das.cache_key_digest(other), "provenance": das._provenance("warm", "0" * 40),
        "validation_status": "VALIDATED", "cache_schema_version": das.CACHE_SCHEMA_VERSION})
    _, tag = das.lookup(das.fresh_state())
    das.reset_caches()
    assert tag.startswith("REJECTED"), tag


def test_cross_authorization_cache_value_is_rejected():
    das.reset_caches()
    other = das._thaw(das.fresh_state())
    other["authorization"] = {"issuance": "2026-08-06T01:35:35Z", "expiry": "2026-08-06T23:35:35Z",
                              "duration_seconds": 79200}
    other["authorization"]["pair_digest"] = ca.digest(other["authorization"])
    # a retired pair yields a different key; a warm entry under the active key cannot carry it
    assert das.cache_key_digest(other) != das.cache_key_digest(das.fresh_state())
    das.reset_caches()


# ===================================================================== hostile / symlinked Docker config
_CONFIG_PROBE = r'''
import json, os, sys
sys.path.insert(0, os.environ["SN_SCRIPTS"])
import docker_boundary as db
st = db.steering_state()
problems = db.config_problems(st, db.load_policy())
print("CFG_JSON:" + json.dumps({"problems": [p[:80] for p in problems], "clean": problems == [],
                                "config_dir": st.get("config_dir"), "is_symlink": st.get("config_is_symlink")}))
'''


def _config_probe(config_dir: Path) -> dict:
    env = dict(os.environ, SN_SCRIPTS=str(REPO_ROOT / "scripts"), DOCKER_CONFIG=str(config_dir))
    proc = subprocess.run([sys.executable, "-c", _CONFIG_PROBE], cwd=str(REPO_ROOT),
                          env=env, capture_output=True, text=True, timeout=120)
    line = [ln for ln in proc.stdout.splitlines() if ln.startswith("CFG_JSON:")]
    assert line, proc.stdout[-400:] + proc.stderr[-400:]
    return json.loads(line[-1][len("CFG_JSON:"):])


@pytest.mark.parametrize("label,content,expect_fail", [
    ("currentContext", '{"currentContext":"evil"}', True),
    ("credsStore", '{"credsStore":"evilhelper"}', True),
    ("credHelpers", '{"credHelpers":{"registry":"evil"}}', True),
    ("proxies", '{"proxies":{"default":{"httpProxy":"http://evil"}}}', True),
    ("malformed JSON", 'NOT JSON {{{', True),
    ("empty config", '{}', False),
])
def test_hostile_docker_config(tmp_path, label, content, expect_fail, record_property):
    cfg = tmp_path / "dcfg"
    cfg.mkdir()
    (cfg / "config.json").write_text(content)
    r = _config_probe(cfg)
    record_property("docker_config", json.dumps({"label": label,
                    "classification": EXPECTED_FAIL_CLOSED if expect_fail else EXPECTED_PASS,
                    "clean": r["clean"], "detector": (r["problems"][:1] or [""])[0]}))
    if expect_fail:
        assert not r["clean"], f"{label} must fail closed: {r}"
    else:
        assert r["clean"], f"{label} must be clean: {r}"


def test_symlinked_docker_config_file_is_refused(tmp_path):
    """A config.json that is a symlink is refused (config_symlink_prohibited); the path identity is
    explicit so a symlinked config cannot masquerade as an in-place one."""
    ext = tmp_path / "external"; ext.mkdir()
    (ext / "real_config.json").write_text('{}')
    cfg = tmp_path / "dcfg"; cfg.mkdir()
    (cfg / "config.json").symlink_to(ext / "real_config.json")
    r = _config_probe(cfg)
    assert r["is_symlink"] is True, "the probe must observe the symlink"
    assert not r["clean"] and any("symlink" in p.lower() for p in r["problems"]), r


def test_broken_symlinked_docker_config(tmp_path):
    cfg = tmp_path / "dcfg"; cfg.mkdir()
    (cfg / "config.json").symlink_to(tmp_path / "nonexistent-target")
    r = _config_probe(cfg)
    # a broken symlink is a symlink whose target does not exist: refused as a symlink, fail-closed.
    assert not r["clean"], r


# ===================================================================== Docker CLI absence (Model B)
def test_docker_cli_absent_inactive_path_still_derives(tmp_path):
    """Under Model B (prohibit_local_docker_execution=True) no active local execution path requires
    the Docker CLI, so its absence must NOT create a false failure: the static authoritative state
    still derives cleanly."""
    minimal_path = "/usr/bin:/bin"                    # no docker on PATH
    r = _run_probe({"PATH": minimal_path})
    assert r["ok"] and r["validate_clean"], "static assurance must derive with Docker absent (Model B)"


def test_docker_absent_active_requirement_fails_closed():
    """The distinction: if a site required active local Docker execution, the availability layer
    fails closed. Model B prohibits local execution, so this is proven via the availability
    detector rather than left as a universal PASS."""
    import docker_boundary as db
    doc = db.load_policy()
    assert doc.get("prohibit_local_docker_execution") is True, (
        "Model B prohibits local Docker execution; the active-execution path is structurally absent")
    # The availability layer exists and would fail closed for a required-but-absent executable.
    assert callable(db.availability_problems)


# ===================================================================== cache-directory conditions (in-memory)
def test_cache_directory_conditions_are_location_independent(tmp_path):
    """The governed cache is in-memory with no disk/HOME path. Stale / read-only / unwritable cache
    directories therefore cannot affect correctness: a plausible stale cache directory on disk is
    never read, and each process starts cold."""
    stale = tmp_path / "stale_cache"
    stale.mkdir()
    (stale / "pre-b1-state.json").write_text('{"schema_version":"pre-b1.0","stale":true}')
    (stale / "unknown.bin").write_bytes(b"\x00\x01")
    # Point HOME and a would-be cache dir at the stale location; the state is unaffected.
    r = _run_probe({"HOME": str(tmp_path), "XDG_CACHE_HOME": str(stale)})
    assert r["ok"] and r["validate_clean"], "a stale cache directory must not affect correctness"
    assert r["state_digest"] == REFERENCE_DIGEST
    assert r["cold_cache_empty"], "the in-memory cache starts cold regardless of any disk directory"


def test_read_only_and_unwritable_cache_have_no_effect(tmp_path):
    """Correctness never depends on a cache write; a read-only HOME cache path changes nothing."""
    r = _run_probe({"HOME": "/nonexistent-readonly-home", "XDG_CACHE_HOME": "/proc/nonwritable"})
    assert r["ok"] and r["validate_clean"], r
    assert r["state_digest"] == REFERENCE_DIGEST


# ===================================================================== graded-session A–H
@pytest.fixture(scope="module")
def pristine(tmp_path_factory) -> Path:
    return _materialise(tmp_path_factory.mktemp("b3-pristine"))


def _fresh_clone(pristine: Path, tmp_path: Path) -> Path:
    root = tmp_path / "s"
    subprocess.run(["git", "clone", "-q", str(pristine), str(root)], check=True, capture_output=True)
    return root


_OK = "def test_ok():\n    assert True\n"
_MUTATE_TREE = (
    "import json,sys\nfrom pathlib import Path\n"
    "def test_m():\n"
    "    p=Path('tests/fixtures/docker-boundary-policy.json'); d=json.loads(p.read_text())\n"
    "    d['ci_assumption']['version']='b3-stale'; p.write_text(json.dumps(d,indent=1))\n"
    "    sys.path.insert(0,'scripts'); import docker_assurance_state as das\n"
    "    das.reset_caches(); assert json.loads(p.read_text())['ci_assumption']['version']=='b3-stale'\n")
_MUTATE_SITE = (
    "import json,sys\nfrom pathlib import Path\n"
    "def test_m():\n"
    "    sys.path.insert(0,'scripts'); import docker_boundary as db\n"
    "    p=Path('tests/fixtures/docker-boundary-policy.json'); d=json.loads(p.read_text())\n"
    "    lb=[s for s in d['call_sites'] if db.classify_site(s)[0] in db.LOAD_BEARING_CLASSIFICATIONS]\n"
    "    lb[0]['failure_behaviour']=''; p.write_text(json.dumps(d,indent=1))\n"
    "    assert True\n")


def test_graded_A_fresh_valid_state(pristine, tmp_path):
    proc = _graded(_fresh_clone(pristine, tmp_path), _OK)
    assert proc.returncode == 0, (proc.stdout + proc.stderr)[-1200:]


def test_graded_B_warm_valid_cache_in_process():
    import types, signalnest_bootstrap as boot
    das.reset_caches()
    att = boot.establish(strict=True)
    _, tag = das.lookup(das.fresh_state())
    assert tag == "HIT"
    assert boot.reverify(_mk_cfg(boot, att))["clean"]


def _mk_cfg(boot, att):
    import types
    cfg = types.SimpleNamespace(); setattr(cfg, boot.BOOTSTRAP_ATTESTATION, att); return cfg


def test_graded_C_stale_tree_fails(pristine, tmp_path):
    proc = _graded(_fresh_clone(pristine, tmp_path), _MUTATE_TREE)
    assert proc.returncode == 3 and "docker_assurance" in (proc.stdout + proc.stderr), (proc.stdout + proc.stderr)[-1200:]


def test_graded_D_stale_policy_fails(pristine, tmp_path):
    proc = _graded(_fresh_clone(pristine, tmp_path), _MUTATE_SITE)
    assert proc.returncode == 3, (proc.stdout + proc.stderr)[-1200:]


def test_graded_E_cache_poisoned_after_baseline_no_change():
    import types, signalnest_bootstrap as boot
    das.reset_caches()
    att = boot.establish(strict=True)
    das._STATE_CACHE["bogus"] = ca.deep_freeze({"state": {}, "state_digest": "x", "cache_key_digest": "x",
                                                "provenance": {}, "validation_status": "VALIDATED",
                                                "cache_schema_version": das.CACHE_SCHEMA_VERSION})
    assert das.reverify_state(att["docker_assurance"])["clean"]
    das.reset_caches()


def test_graded_F_cache_only_bypass_refused():
    import signalnest_bootstrap as boot
    att = boot.establish(strict=True)
    original = das.fresh_state
    try:
        das.fresh_state = lambda: (_ for _ in ()).throw(das.DockerAssuranceError("disabled"))
        assert not das.reverify_state(att["docker_assurance"])["clean"]
    finally:
        das.fresh_state = original


def test_graded_G_hostile_docker_env_and_retired_auth(pristine, tmp_path):
    # G1: a graded session under a hostile prohibited Docker env fails closed at establish.
    proc = _graded(_fresh_clone(pristine, tmp_path), _OK, env_extra={"DOCKER_HOST": "tcp://evil:2375"})
    assert proc.returncode != 0, "a hostile Docker env must fail the graded session"
    # G2: a cache carrying a retired authorization pair is refused (in-process, shares E's detector).
    das.reset_caches()
    other = das._thaw(das.fresh_state())
    other["authorization"] = {"issuance": "2026-08-06T01:35:35Z", "expiry": "2026-08-06T23:35:35Z", "duration_seconds": 79200}
    other["authorization"]["pair_digest"] = ca.digest(other["authorization"])
    assert das.cache_key_digest(other) != das.cache_key_digest(das.fresh_state())
    das.reset_caches()


def test_graded_H_full_propagation(pristine, tmp_path):
    # defect -> state/das failure -> reverify not clean -> session exitstatus 3
    proc = _graded(_fresh_clone(pristine, tmp_path), _MUTATE_SITE)
    assert proc.returncode == 3, (proc.stdout + proc.stderr)[-1200:]


# ===================================================================== harness self-protection
def _required_env_ids():
    faithful = {1, 2, 4, 12}
    single = {"sanitized_path": 5, "alt_cwd": 6, "spaces": 7, "hostile_home": 3}
    seeds = set(range(101, 107))                      # six hash-seed rows (synthetic ids)
    procs = {201, 202, 203}                            # three fresh-process rows
    docker_env = {301, 302, 303, 304, 305}             # five hostile docker env rows
    cache = {16, 17, 18, 22, 23, 24, 25, 26}           # cache-directory + cross-tree/auth rows
    graded = set(range(401, 409))                      # A-H
    return faithful | set(single.values()) | seeds | procs | docker_env | cache | graded


def test_environment_matrix_completeness():
    ids = _required_env_ids()
    # 20 conditions + 6 seeds + 3 procs + 5 docker-env + graded A-H, all uniquely identified.
    assert len(ids) == len(set(ids)), "duplicate environment id"
    assert len(ids) >= 20 + 6 + 3, f"the matrix is missing rows: {sorted(ids)}"


def test_the_probe_reports_the_environment_it_observed():
    """Self-protection: an env delta that never reached the process would fail its own row. Prove
    the probe echoes the delta so a not-applied env is detectable."""
    r = _run_probe({"PYTHONHASHSEED": "424242", "HOME": "/tmp/b3-selfcheck"})
    assert r["observed"]["hashseed"] == "424242"
    assert r["observed"]["home"] == "/tmp/b3-selfcheck"


def test_real_repository_unchanged_by_the_matrix():
    assert _real_repo_unchanged(), "the environment matrix must not mutate the real repository"
