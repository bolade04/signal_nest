"""Gate 4N-I28BF-B1 — real session-baseline / session-finish integration and focused A–H probes.

Proves the authoritative state and governed cache are wired into the REAL bootstrap
(`establish()` binds them, `reverify()` freshly re-derives them), and drives focused isolated
graded sessions showing cache/state defects propagate to the final graded result while the cache
can never substitute for fresh finish derivation. This is the focused B1 propagation set, not the
complete B3 environment battery.
"""

from __future__ import annotations

import copy
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import docker_assurance_state as das               # noqa: E402
import signalnest_bootstrap as boot                # noqa: E402


# ===================================================================== real in-process integration
def test_establish_binds_the_authoritative_state_and_populates_the_cache():
    das.reset_caches()
    att = boot.establish(strict=True)
    assert "docker_assurance" in att, "establish() must bind the authoritative Docker state"
    bound = att["docker_assurance"]
    assert bound["state_digest"] and bound["cache_key_digest"]
    assert bound["provenance"]["origin"] == "cold"
    assert das._STATE_CACHE, "establish() must populate the governed cache cold"


def test_reverify_adds_a_docker_assurance_layer_and_derives_fresh():
    att = boot.establish(strict=True)
    cfg = types.SimpleNamespace()
    setattr(cfg, boot.BOOTSTRAP_ATTESTATION, att)
    out = boot.reverify(cfg)
    assert out["layers"].get("docker_assurance") is True
    assert out["clean"]


def test_reverify_does_not_substitute_the_cache_for_fresh_derivation():
    """Even with a poisoned cache, reverify_state derives fresh and stays correct."""
    att = boot.establish(strict=True)
    das._STATE_CACHE["bogus"] = das._ca.deep_freeze(
        {"state": {}, "state_digest": "x", "cache_key_digest": "x", "provenance": {},
         "validation_status": "VALIDATED", "cache_schema_version": das.CACHE_SCHEMA_VERSION})
    out = das.reverify_state(att["docker_assurance"])
    assert out["clean"], "the fresh finish derivation must ignore the poisoned cache"


# ===================================================================== focused graded sessions
def _materialise(dest: Path) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    tree = subprocess.run(["git", "write-tree"], cwd=REPO_ROOT,
                          capture_output=True, text=True, check=True).stdout.strip()
    archive = subprocess.run(["git", "archive", tree], cwd=REPO_ROOT,
                             capture_output=True, check=True).stdout
    tar = dest / "_t.tar"
    tar.write_bytes(archive)
    subprocess.run(["tar", "-xf", str(tar)], cwd=dest, check=True)
    tar.unlink()
    for cmd in (["git", "init", "-q"], ["git", "add", "-A"],
                ["git", "-c", "user.email=a@b.c", "-c", "user.name=x", "commit", "-qm", "b"]):
        subprocess.run(cmd, cwd=dest, check=True, capture_output=True)
    return dest


def _run(root: Path, probe: str) -> subprocess.CompletedProcess:
    (root / "tests" / "test_zz_b1_probe.py").write_text(probe)
    env = dict(os.environ, SIGNALNEST_ANCHOR_TIER="TIER_1_SYNTHETIC",
               SIGNALNEST_CANDIDATE_MANIFEST=str(root / "tests" / "fixtures" / "candidate-manifest.json"),
               PYTHONPATH=str(root / "scripts"))
    env.pop("SIGNALNEST_MANDATORY_NODES", None)
    env.pop("SIGNALNEST_BOOTSTRAP_ATTESTATION", None)
    return subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_zz_b1_probe.py", "-q", "-p", "no:randomly",
         "-p", "signalnest_bootstrap"], cwd=root, env=env, capture_output=True, text=True, timeout=300)


@pytest.fixture(scope="module")
def clone(tmp_path_factory):
    return _materialise(tmp_path_factory.mktemp("b1-int"))


def _fresh(clone, tmp_path):
    root = tmp_path / "s"
    subprocess.run(["git", "clone", "-q", str(clone), str(root)], check=True, capture_output=True)
    return root


_BASELINE = "def test_ok():\n    assert True\n"

_MUTATE_TREE = (
    "import json,sys\nfrom pathlib import Path\n"
    "def test_mutate_tree():\n"
    "    p=Path('tests/fixtures/docker-boundary-policy.json'); d=json.loads(p.read_text())\n"
    "    d['ci_assumption']['version']='b1-stale-tree'; p.write_text(json.dumps(d,indent=1))\n"
    "    sys.path.insert(0,'scripts'); import docker_assurance_state as das\n"
    "    assert das.fresh_state()['policy']['policy_digest']\n")

_MUTATE_POLICY_SITE = (
    "import json,sys\nfrom pathlib import Path\n"
    "def test_mutate_policy():\n"
    "    p=Path('tests/fixtures/docker-boundary-policy.json'); d=json.loads(p.read_text())\n"
    "    import sys as _s; _s.path.insert(0,'scripts'); import docker_boundary as db\n"
    "    lb=[s for s in d['call_sites'] if db.classify_site(s)[0] in db.LOAD_BEARING_CLASSIFICATIONS]\n"
    "    lb[0]['failure_behaviour']=''; p.write_text(json.dumps(d,indent=1))\n"
    "    import docker_assurance_state as das\n"
    "    assert das.validate_state(das.fresh_state())\n")

_POISON_CACHE_NO_CHANGE = (
    "import sys\n"
    "def test_poison_cache_only():\n"
    "    sys.path.insert(0,'scripts'); import docker_assurance_state as das, cache_authority as ca\n"
    "    das._STATE_CACHE['bogus']=ca.deep_freeze({'state':{},'state_digest':'x','cache_key_digest':'x',\n"
    "        'provenance':{},'validation_status':'VALIDATED','cache_schema_version':das.CACHE_SCHEMA_VERSION})\n"
    "    assert 'bogus' in das._STATE_CACHE\n")


def test_focused_A_cold_valid_state_passes(clone, tmp_path):
    proc = _run(_fresh(clone, tmp_path), _BASELINE)
    assert proc.returncode == 0, (proc.stdout + proc.stderr)[-1500:]


def test_focused_B_warm_valid_state_passes_in_process():
    das.reset_caches()
    att = boot.establish(strict=True)                    # populates the cache
    state, tag = das.lookup(das.fresh_state())
    assert tag == "HIT", "the warm cache must validate and serve"
    out = das.reverify_state(att["docker_assurance"])
    assert out["clean"], "warm-cache establishment still derives finish fresh and passes"


def test_focused_C_stale_tree_fails_the_session(clone, tmp_path):
    proc = _run(_fresh(clone, tmp_path), _MUTATE_TREE)
    assert proc.returncode == 3, (proc.stdout + proc.stderr)[-1500:]
    assert "docker_assurance" in (proc.stdout + proc.stderr)


def test_focused_D_stale_policy_site_fails_the_session(clone, tmp_path):
    proc = _run(_fresh(clone, tmp_path), _MUTATE_POLICY_SITE)
    assert proc.returncode == 3, (proc.stdout + proc.stderr)[-1500:]


def test_focused_E_retired_authorization_is_detected_in_process():
    att = boot.establish(strict=True)
    tampered = copy.deepcopy(das._thaw(att["docker_assurance"]["state"]))
    tampered["authorization"] = {"issuance": "2026-08-06T01:35:35Z",
                                 "expiry": "2026-08-06T23:35:35Z", "duration_seconds": 79200}
    tampered["authorization"]["pair_digest"] = das._ca.digest(tampered["authorization"])
    diffs = das.compare_states({"schema_version": das.STATE_SCHEMA_VERSION, **tampered}
                               if False else tampered, das.fresh_state())
    assert any("authorization" in d for d in diffs), "a retired authorization pair must be detected"


def test_focused_F_cache_poisoned_after_baseline_does_not_break_the_session(clone, tmp_path):
    proc = _run(_fresh(clone, tmp_path), _POISON_CACHE_NO_CHANGE)
    assert proc.returncode == 0, (
        "poisoning the cache with no real change must not fail the fresh finish derivation:\n"
        + (proc.stdout + proc.stderr)[-1200:])


def test_focused_G_cache_only_bypass_is_refused_in_process():
    """Disable fresh production derivation; a valid-looking cache cannot stand in for it."""
    att = boot.establish(strict=True)
    original = das.fresh_state
    try:
        das.fresh_state = lambda: (_ for _ in ()).throw(
            das.DockerAssuranceError("fresh derivation disabled"))
        out = das.reverify_state(att["docker_assurance"])
        assert not out["clean"], "with fresh derivation disabled, finish must fail, not serve cache"
    finally:
        das.fresh_state = original


def test_focused_H_propagation_from_defect_to_failed_graded_result(clone, tmp_path):
    """defect -> das validate/compare failure -> reverify not clean -> session exitstatus 3."""
    # in-process stage trace
    a = das.fresh_state()
    bad = copy.deepcopy(das._thaw(a))
    bad["per_site"][0]["decision"] = "FAIL"
    bad["aggregate"]["docker_aggregate"] = True
    assert das.validate_state(bad), "stage 1: state-integrity failure"
    # end to end
    proc = _run(_fresh(clone, tmp_path), _MUTATE_POLICY_SITE)
    assert proc.returncode == 3, "stage N: final graded result fails"
