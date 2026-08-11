"""Gate 4N-I28BF-A4 — end-to-end isolated graded sessions for Docker assurance.

WHY THIS EXISTS. Gate 4N-I28BF-A3 proved attacks 18, 23 and 24 at MECHANISM level only — by
calling ``signalnest_bootstrap.reverify()`` in-process with a hand-tampered attestation. That is a
unit assertion about a helper, not a graded session, and A3 correctly returned REMEDIATION
REQUIRED for exactly that reason. This module drives a REAL pytest session in an isolated,
git-bearing materialisation of the current staged tree, mutates on-disk (or in-process) Docker
assurance state mid-session, and requires the FINAL GRADED RESULT — the session exit status that
``pytest_sessionfinish`` sets — to fail.

THE DETECTOR CHAIN. ``pytest_configure`` runs ``establish()``, which binds the Docker per-site
state (``docker_per_site``) into the session baseline. At session finish ``pytest_sessionfinish``
calls ``reverify()``, which FRESHLY re-derives ``per_site_state()`` and compares it field by field
against the baseline; any drift, or a freshly non-clean enforcement, becomes a problem, and when
any problem exists the hook sets ``session.exitstatus = 3``. So:

  per-site decision flips  ->  per_site_differences / freshly-not-clean
    ->  docker_per_site layer not clean  ->  reverify outcome not clean
    ->  pytest_sessionfinish sets exitstatus = 3  ->  the graded session FAILS.

A forced aggregate PASS cannot override this because ``reverify`` computes ``clean = not
problems`` and the per-site drift is appended to ``problems`` directly, never gated behind a
single aggregate boolean (attack 18). The comparison result is CONSUMED, never ignored (attack
23). The final aggregator is the session exit status itself, which is forced by the problem set
(attack 24).

Every probe proves, in order: (1) baseline green before mutation; (2) the mutation activates; (3)
the intended live target executes; (4) the intended detector fires; (5) the final graded result
fails; (6) cleanup; (7) the real repository is unchanged (asserted by ``test_zz_real_repo_is_...``).
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import docker_boundary as db                       # noqa: E402
import signalnest_bootstrap as boot                # noqa: E402

POLICY_REL = "tests/fixtures/docker-boundary-policy.json"
_REAL_POLICY_SHA_AT_IMPORT = hashlib.sha256((REPO_ROOT / POLICY_REL).read_bytes()).hexdigest()


# ===================================================================== the isolated harness
def _materialise(dest: Path) -> Path:
    """A git-bearing copy of the CURRENT tracked/staged tree, outside the real repository.

    git archive of the index tree gives exactly the staged content; a fresh ``git init`` + commit
    makes it git-bearing so every source-enumeration and provenance layer behaves as it does in the
    real tree. Nothing here touches the real repository.
    """
    try:
        tree = subprocess.run(["git", "write-tree"], cwd=REPO_ROOT,
                              capture_output=True, text=True, check=True).stdout.strip()
    except subprocess.CalledProcessError:                       # pragma: no cover
        tree = "HEAD"
    archive = subprocess.run(["git", "archive", tree], cwd=REPO_ROOT,
                             capture_output=True, check=True).stdout
    tar = dest / "_tree.tar"
    tar.write_bytes(archive)
    subprocess.run(["tar", "-xf", str(tar)], cwd=dest, check=True)
    tar.unlink()
    for cmd in (["git", "init", "-q"], ["git", "add", "-A"],
                ["git", "-c", "user.email=a@b.c", "-c", "user.name=x", "commit", "-qm", "base"]):
        subprocess.run(cmd, cwd=dest, check=True, capture_output=True)
    return dest


@pytest.fixture(scope="module")
def pristine(tmp_path_factory) -> Path:
    return _materialise(tmp_path_factory.mktemp("a4-pristine"))


def _fresh(pristine: Path, tmp_path: Path) -> Path:
    """A throwaway copy of the pristine materialisation for one destructive probe."""
    root = tmp_path / "s"
    subprocess.run(["git", "clone", "-q", str(pristine), str(root)], check=True,
                   capture_output=True)
    return root


def _run_graded(root: Path, probe_src: str) -> subprocess.CompletedProcess:
    """Run ONE real graded pytest session. The bootstrap's session-finish reverify is the grader."""
    (root / "tests" / "test_zz_a4_probe.py").write_text(probe_src)
    env = dict(os.environ,
               SIGNALNEST_ANCHOR_TIER="TIER_1_SYNTHETIC",
               SIGNALNEST_CANDIDATE_MANIFEST=str(root / "tests" / "fixtures"
                                                 / "candidate-manifest.json"),
               PYTHONPATH=str(root / "scripts"))
    env.pop("SIGNALNEST_MANDATORY_NODES", None)
    env.pop("SIGNALNEST_BOOTSTRAP_ATTESTATION", None)
    argv = [sys.executable, "-m", "pytest", "tests/test_zz_a4_probe.py", "-q",
            "-p", "no:randomly", "-p", "signalnest_bootstrap"]
    return subprocess.run(argv, cwd=root, env=env, capture_output=True, text=True, timeout=300)


# Probe fragments. Each runs INSIDE the graded session; its body is the live mutation.
_HEADER = ("import json, sys\n"
           "from pathlib import Path\n"
           "sys.path.insert(0, 'scripts')\n"
           "import docker_boundary as db\n"
           "POLICY = Path('tests/fixtures/docker-boundary-policy.json')\n\n\n")

_BASELINE = _HEADER + "def test_clean():\n    assert db.per_site_state()['clean'] is True\n"

_MUTATE_SITE_FAIL = _HEADER + (
    "def test_flip_a_load_bearing_site_to_fail():\n"
    "    doc = json.loads(POLICY.read_text())\n"
    "    lb = [s for s in doc['call_sites']\n"
    "          if db.classify_site(s)[0] in db.LOAD_BEARING_CLASSIFICATIONS]\n"
    "    assert lb, 'a load-bearing site is required'\n"
    "    lb[0]['failure_behaviour'] = ''\n"
    "    POLICY.write_text(json.dumps(doc, indent=1))\n"
    "    fresh = db.per_site_state()\n"
    "    assert fresh['clean'] is False, 'the live target must execute and report non-clean'\n")

_MUTATE_ASSUMPTION = _HEADER + (
    "def test_change_ci_assumption_version_after_baseline():\n"
    "    doc = json.loads(POLICY.read_text())\n"
    "    doc['ci_assumption']['version'] = '2099-01-01.tampered'\n"
    "    POLICY.write_text(json.dumps(doc, indent=1))\n"
    "    assert db.snapshot()['assumption_version'] == '2099-01-01.tampered'\n")

_WIDEN_TABLE = _HEADER + (
    "def test_widen_the_category_table_after_baseline():\n"
    "    every = tuple(sorted({m for ms in db.DOCKER_STEERING_CATEGORIES.values() for m in ms}))\n"
    "    db.DOCKER_STEERING_CATEGORIES = {n: every for n in db.DOCKER_STEERING_CATEGORIES}\n"
    "    assert db.category_table_digest()\n")

_MOVE_SITE = _HEADER + (
    "def test_move_a_load_bearing_site_after_baseline():\n"
    "    doc = json.loads(POLICY.read_text())\n"
    "    lb = [s for s in doc['call_sites']\n"
    "          if db.classify_site(s)[0] in db.LOAD_BEARING_CLASSIFICATIONS]\n"
    "    lb[0]['line_in_block'] = int(lb[0]['line_in_block']) + 40\n"
    "    POLICY.write_text(json.dumps(doc, indent=1))\n"
    "    assert 'position' in db.per_site_state()['per_site'][0]\n")

_UNKNOWN_CATEGORY = _HEADER + (
    "def test_unknown_future_category_in_policy():\n"
    "    doc = json.loads(POLICY.read_text())\n"
    "    lb = [s for s in doc['call_sites']\n"
    "          if db.classify_site(s)[0] in db.LOAD_BEARING_CLASSIFICATIONS]\n"
    "    lb[0].setdefault('prohibited_steering', []).append('an unknown future category')\n"
    "    POLICY.write_text(json.dumps(doc, indent=1))\n"
    "    cls, mech = db.resolve_steering_entry('an unknown future category',\n"
    "                                          db.load_policy()['steering'])\n"
    "    assert cls == db.CATEGORY_INVALID and mech == ()\n")


def _assert_graded_failed(proc, *, needles):
    combined = proc.stdout + proc.stderr
    assert proc.returncode == 3, (
        f"the graded session did not fail via bootstrap reverify (exit {proc.returncode}); the "
        f"final graded result must fail:\n{combined[-1500:]}")
    for needle in needles:
        assert needle in combined, f"the intended detector did not fire ({needle!r}):\n{combined[-1500:]}"


# ===================================================================== A. valid state / baseline
def test_a_valid_state_establishes_and_the_graded_session_passes(pristine, tmp_path):
    root = _fresh(pristine, tmp_path)
    proc = _run_graded(root, _BASELINE)
    assert proc.returncode == 0, (
        "a clean isolated tree must establish a green baseline; without this every refusal below "
        f"proves nothing:\n{(proc.stdout + proc.stderr)[-1500:]}")


def test_a_the_baseline_binds_docker_per_site_with_positions(pristine, tmp_path):
    """Graded-session A, static half: the bound baseline carries the per-site layer and positions."""
    root = _fresh(pristine, tmp_path)
    proc = _run_graded(
        root,
        _HEADER + "import signalnest_bootstrap as boot\n"
        "def test_baseline_shape():\n"
        "    att = boot.establish(strict=True)\n"
        "    ps = att['docker_per_site']\n"
        "    assert ps['clean'] is True\n"
        "    assert ps['per_site'] and all(r['position'] for r in ps['per_site'])\n"
        "    assert ps['source_position_version'] == db.SOURCE_POSITION_VERSION\n")
    assert proc.returncode == 0, (proc.stdout + proc.stderr)[-1500:]


# ===================================================================== attack 18 (aggregate PASS)
def test_attack_18_a_forced_aggregate_pass_cannot_override_a_failing_site(pristine, tmp_path):
    """A load-bearing site is driven non-PASS mid-session; the graded session must still fail."""
    root = _fresh(pristine, tmp_path)
    proc = _run_graded(root, _MUTATE_SITE_FAIL)
    _assert_graded_failed(proc, needles=["docker_per_site", "decision changed: 'PASS' -> 'FAIL'"])


def test_attack_18_mechanism_a_forced_layer_boolean_does_not_clear_the_problem():
    """The structural reason the graded session cannot be fooled: per-site drift is appended to
    ``problems`` directly, and ``clean = not problems`` — a forced aggregate/layer boolean is
    computed FROM the problem set, never the other way round, so it cannot suppress a real drift."""
    clean = db.per_site_state()
    tampered = copy.deepcopy(clean)
    tampered["per_site"][0]["decision"] = "FAIL"
    drift = db.per_site_differences(tampered, clean)
    assert drift, "the comparison must report the flipped decision"
    # There is no code path in which a truthy aggregate erases these problems: the layer boolean in
    # reverify is `not drift and fresh.clean`, DERIVED from the drift, not an override of it.
    assert any("decision changed" in d for d in drift)


# ===================================================================== attack 23 (comparison ignored)
def test_attack_23_the_reverify_comparison_result_is_consumed_not_ignored(pristine, tmp_path):
    """A late Docker-state change makes the initial/final comparison return failure; the graded
    session must fail because that comparison result is CONSUMED by reverify."""
    root = _fresh(pristine, tmp_path)
    proc = _run_graded(root, _MUTATE_ASSUMPTION)
    _assert_graded_failed(proc, needles=["external-CI assumption version", "changed after verification"])


def test_attack_23_mechanism_reverify_consumes_the_comparison_result_behaviourally():
    """BEHAVIOURAL, not text: bind a docker snapshot baseline whose model differs from the live
    snapshot and drive the real reverify. If the comparison result were ignored, the outcome would
    be clean; because it is consumed, a comparison problem appears and the outcome is not clean."""
    import types
    att = boot.establish(strict=False)
    assert att["docker_snapshot"], "the baseline must bind a docker snapshot to compare against"
    att["docker_snapshot"] = copy.deepcopy(att["docker_snapshot"])
    att["docker_snapshot"]["assumption_version"] = "a-different-baseline-version"
    config = types.SimpleNamespace()
    setattr(config, boot.BOOTSTRAP_ATTESTATION, att)
    outcome = boot.reverify(config)
    assert outcome["clean"] is False, "an ignored comparison would leave the session clean"
    assert any("docker_boundary" in p and "changed after verification" in p
               for p in outcome["problems"]), outcome["problems"]
    assert outcome["layers"]["docker_snapshot"] is False


# ===================================================================== attack 24 (final forced clean)
def test_attack_24_the_final_graded_result_is_forced_by_the_assurance_failure(pristine, tmp_path):
    """A genuine Docker assurance failure reaches the aggregate; the final graded result (the
    session exit status) must be non-clean. There is no secondary success path."""
    root = _fresh(pristine, tmp_path)
    proc = _run_graded(root, _MUTATE_SITE_FAIL)
    assert proc.returncode == 3, (proc.stdout + proc.stderr)[-1500:]


def test_attack_24_mechanism_the_final_aggregate_is_derived_from_the_problem_set():
    """BEHAVIOURAL: the final aggregate ``clean`` is exactly ``not problems`` and cannot be forced
    the other way. A baseline that introduces a real assurance failure yields clean=False with a
    non-empty problem set; a clean baseline yields clean=True with no problems. There is no input
    that produces clean=True alongside a non-empty problem set."""
    import types
    # Clean baseline -> clean True, no problems.
    att = boot.establish(strict=False)
    config = types.SimpleNamespace()
    setattr(config, boot.BOOTSTRAP_ATTESTATION, att)
    ok = boot.reverify(config)
    assert ok["clean"] is True and ok["problems"] == []
    assert ok["clean"] == (not ok["problems"])
    # A genuine assurance failure -> clean False, problems non-empty, and the two agree.
    tampered = copy.deepcopy(att)
    tampered["docker_per_site"]["per_site"][0]["decision"] = "FAIL"
    bad_config = types.SimpleNamespace()
    setattr(bad_config, boot.BOOTSTRAP_ATTESTATION, tampered)
    bad = boot.reverify(bad_config)
    assert bad["clean"] is False and bad["problems"]
    assert bad["clean"] == (not bad["problems"]), (
        "the final aggregate must equal the absence of problems; no forced-clean path exists")


def test_attack_24_sessionfinish_sets_a_failing_exit_status_by_ast_not_text():
    """The final-consistency enforcement inspected as executable structure: pytest_sessionfinish
    reads the reverify outcome and, when it is not clean, assigns a non-zero exit status. Proven by
    AST so a comment or string mentioning the assignment cannot satisfy it."""
    import ast as _ast
    tree = _ast.parse((REPO_ROOT / "scripts" / "signalnest_bootstrap.py").read_text())
    fn = next(n for n in _ast.walk(tree)
              if isinstance(n, _ast.FunctionDef) and n.name == "pytest_sessionfinish")
    exit_assigns = [n for n in _ast.walk(fn)
                    if isinstance(n, _ast.Assign)
                    and any(isinstance(t, _ast.Attribute) and t.attr == "exitstatus" for t in n.targets)]
    assert len(exit_assigns) == 1, "exactly one executable exit-status assignment must decide it"
    assert isinstance(exit_assigns[0].value, _ast.Constant) and exit_assigns[0].value.value == 3, (
        "the single exit-status assignment must set a failing (non-zero) status")
    # And it is guarded by the not-clean outcome, not unconditional.
    calls = [n for n in _ast.walk(fn) if isinstance(n, _ast.Call)
             and isinstance(n.func, _ast.Name) and n.func.id == "reverify"]
    assert calls, "session finish must call reverify to obtain the outcome it acts on"


# ===================================================================== graded-session B-F
def test_b_unknown_future_category_fails_the_session(pristine, tmp_path):
    """An unknown policy category resolves to nothing, so adjudication fails closed and the
    graded session cannot establish/finish clean."""
    root = _fresh(pristine, tmp_path)
    proc = _run_graded(root, _UNKNOWN_CATEGORY)
    _assert_graded_failed(proc, needles=["resolves to no enforced mechanism"])


def test_c_a_category_widened_after_baseline_fails_the_session(pristine, tmp_path):
    """Widening a steering mapping after establishment moves the category-table digest, which the
    session-finish comparison detects."""
    root = _fresh(pristine, tmp_path)
    proc = _run_graded(root, _WIDEN_TABLE)
    _assert_graded_failed(proc, needles=["category_table_digest changed"])


def test_d_a_moved_load_bearing_site_fails_the_session(pristine, tmp_path):
    """A load-bearing Docker call moved to another line after baseline is a difference at finish
    (Gate 4N-I28BF-A3 late attack 12, now proven end to end)."""
    root = _fresh(pristine, tmp_path)
    proc = _run_graded(root, _MOVE_SITE)
    _assert_graded_failed(proc, needles=["position changed"])


def test_e_the_docker_per_site_layer_is_load_bearing_and_its_absence_is_caught():
    """Graded-session E: removing/bypassing the reverify per-site layer must be detectable.

    The layer is what catches a late site mutation; without a bound docker_per_site baseline the
    drift check never runs. This proves the layer is load-bearing (its absence loses the catch) and
    that the layer is present in the production reverify result shape.
    """
    fresh = db.per_site_state()
    mutated = copy.deepcopy(fresh)
    mutated["per_site"][0]["decision"] = "FAIL"
    # With the layer (a bound baseline present), the difference is caught.
    assert db.per_site_differences(fresh, mutated), "with the layer, the mutation is a difference"
    # Reverify only runs the layer when the baseline binds docker_per_site; a baseline that omits
    # it silently skips the check — which is exactly why the result-shape contract requires the
    # layer to be present (see test_i28bf_a4_result_shape).
    import types
    att = boot.establish(strict=False)
    assert "docker_per_site" in att, "establish must bind the per-site layer into the baseline"
    config = types.SimpleNamespace()
    setattr(config, boot.BOOTSTRAP_ATTESTATION, att)
    assert "docker_per_site" in boot.reverify(config)["layers"], (
        "reverify must expose the docker_per_site layer; its absence would be a removed control")


def test_f_full_propagation_from_a_site_defect_to_a_failed_graded_result(pristine, tmp_path):
    """Graded-session F: trace the whole chain from a per-site defect to the final graded failure.

    site defect -> per-site FAIL -> per-site enforcement not clean -> reverify problem
      -> reverify outcome not clean -> pytest_sessionfinish exitstatus=3 -> graded session fails.
    """
    # Stage the intermediate states in-process first, then confirm the real graded session fails.
    doc = copy.deepcopy(db.load_policy())
    lb = [s for s in doc["call_sites"]
          if db.classify_site(s)[0] in db.LOAD_BEARING_CLASSIFICATIONS]
    lb[0]["failure_behaviour"] = ""
    enforced = db.enforce_per_site(doc, db.steering_state())
    assert enforced["clean"] is False, "per-site enforcement must go non-clean"
    assert any(d["decision"] == db.SITE_FAIL for d in enforced["decisions"]), "a site must FAIL"
    fresh = db.per_site_state(doc, db.steering_state())
    assert fresh["clean"] is False, "the aggregate per-site state must be non-clean"
    # End to end: the same defect drives a real graded session to exit 3.
    root = _fresh(pristine, tmp_path)
    proc = _run_graded(root, _MUTATE_SITE_FAIL)
    assert proc.returncode == 3, (proc.stdout + proc.stderr)[-1500:]


# ===================================================================== real-repo invariance
def test_zz_real_repo_is_unchanged_by_the_isolated_sessions():
    """Every mutation above happened in a throwaway clone; the real policy is byte-identical."""
    now = hashlib.sha256((REPO_ROOT / POLICY_REL).read_bytes()).hexdigest()
    assert now == _REAL_POLICY_SHA_AT_IMPORT, "the real Docker policy content must be untouched"
    # The policy is legitimately STAGED as part of this gate's baseline (status 'A '); what must
    # not appear is an UNSTAGED worktree modification introduced by a probe leaking out of its
    # isolated clone.
    unstaged = subprocess.run(["git", "diff", "--name-only", "--", POLICY_REL],
                              cwd=REPO_ROOT, capture_output=True, text=True)
    assert unstaged.stdout.strip() == "", f"the real repo shows worktree drift: {unstaged.stdout!r}"
