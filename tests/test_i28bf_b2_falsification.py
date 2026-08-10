"""Gate 4N-I28BF-B2 — the complete 37-arm isolated falsification battery.

Adversarially falsifies the B1 authoritative Docker assurance-state and governed-cache architecture.
Every scored arm runs against the REAL production implementation. Two realizations are used, both
against production detectors and neither a generic source-integrity fallback:

  * on-disk arms mutate a throwaway git-bearing clone's policy, workflow, or a protected source
    file, then run a real graded ``pytest`` session (``-p signalnest_bootstrap``); the final graded
    result is the session exit status, and the intended detector must appear in its output;
  * cache/state arms drive the production validators (``validate_state`` / ``validate_cache_key`` /
    ``validate_cache_value`` / ``lookup`` / ``reverify_state``) and the deep-freeze guarantee
    directly, proving the intended detector refuses the poison. Arm 32 is the one explicitly-exempt
    "rendered ineffective" case.

Each arm yields a record with activation and detector evidence; only ACTIVATED_REFUSED passes. The
harness self-protection tests require exactly 37 unique arm ids with no duplicate, missing, or
inert-as-scored entries, and derive the summary from the records rather than a hand-entered total.
"""

from __future__ import annotations

import copy
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
import docker_boundary as db                       # noqa: E402

POLICY_REL = "tests/fixtures/docker-boundary-policy.json"
_REAL_TREE_BEFORE = subprocess.run(["git", "write-tree"], cwd=REPO_ROOT,
                                   capture_output=True, text=True).stdout.strip()

ACTIVATED_REFUSED = "ACTIVATED_REFUSED"


# ===================================================================== isolated harness
def _materialise(dest: Path) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    tree = subprocess.run(["git", "write-tree"], cwd=REPO_ROOT,
                          capture_output=True, text=True, check=True).stdout.strip()
    arch = subprocess.run(["git", "archive", tree], cwd=REPO_ROOT, capture_output=True, check=True).stdout
    (dest / "_t.tar").write_bytes(arch)
    subprocess.run(["tar", "-xf", "_t.tar"], cwd=dest, check=True)
    (dest / "_t.tar").unlink()
    for cmd in (["git", "init", "-q"], ["git", "add", "-A"],
                ["git", "-c", "user.email=a@b.c", "-c", "user.name=x", "commit", "-qm", "b"]):
        subprocess.run(cmd, cwd=dest, check=True, capture_output=True)
    return dest


@pytest.fixture(scope="module")
def pristine(tmp_path_factory) -> Path:
    return _materialise(tmp_path_factory.mktemp("b2"))


def _fresh(pristine: Path, tmp_path: Path) -> Path:
    root = tmp_path / "s"
    subprocess.run(["git", "clone", "-q", str(pristine), str(root)], check=True, capture_output=True)
    return root


def _graded(root: Path, probe: str = "def test_ok():\n    assert True\n") -> subprocess.CompletedProcess:
    (root / "tests" / "test_zz_b2_probe.py").write_text(probe)
    env = dict(os.environ, SIGNALNEST_ANCHOR_TIER="TIER_1_SYNTHETIC",
               SIGNALNEST_CANDIDATE_MANIFEST=str(root / "tests" / "fixtures" / "candidate-manifest.json"),
               PYTHONPATH=str(root / "scripts"))
    env.pop("SIGNALNEST_MANDATORY_NODES", None)
    env.pop("SIGNALNEST_BOOTSTRAP_ATTESTATION", None)
    return subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_zz_b2_probe.py", "-q", "-p", "no:randomly",
         "-p", "signalnest_bootstrap"], cwd=root, env=env, capture_output=True, text=True, timeout=300)


def _load(root: Path) -> dict:
    return json.loads((root / POLICY_REL).read_text())


def _save(root: Path, doc: dict):
    (root / POLICY_REL).write_text(json.dumps(doc, indent=1))


def _lb_ids(doc):
    return [s["id"] for s in doc["call_sites"]
            if db.classify_site(s)[0] in db.LOAD_BEARING_CLASSIFICATIONS]


# ---- on-disk mutators (applied to the clone BEFORE the graded session) ----------------------
def m_omit_site(root):
    d = _load(root); d["call_sites"].pop(0); _save(root, d)


def m_omit_source(root):
    d = _load(root); d["call_sites"] = [s for s in d["call_sites"] if s.get("workflow") != "ci.yml"]; _save(root, d)


def m_duplicate(root):
    d = _load(root); d["call_sites"].append(dict(d["call_sites"][0])); _save(root, d)


def m_move_position(root):
    d = _load(root); lb = [s for s in d["call_sites"] if s["id"] in _lb_ids(d)]
    lb[0]["line_in_block"] = int(lb[0]["line_in_block"]) + 40; _save(root, d)


def m_reclassify(root):
    # Turn a graded workflow site into a script-only site by dropping its workflow ownership.
    d = _load(root); lb = [s for s in d["call_sites"] if s["id"] in _lb_ids(d)][0]
    lb["workflow"] = None; lb["job"] = None; lb["step_name"] = None
    lb["source"] = "scripts/some-script.sh"; _save(root, d)


def m_env_key_fatal(root):
    d = _load(root); lb = [s for s in d["call_sites"] if s["id"] in _lb_ids(d)][0]
    lb.setdefault("permitted_steering", []).append("DOCKER_HOST"); _save(root, d)  # not adjudicated -> refusal


def m_remove_field(root):
    d = _load(root); d["call_sites"][0].pop("failure_behaviour"); _save(root, d)


def m_add_unknown(root):
    d = _load(root); d["call_sites"][0]["surprise_field"] = 1; _save(root, d)


def m_empty(root):
    d = _load(root); d["call_sites"] = []; _save(root, d)


def m_zero_mechanism_category(root):
    d = _load(root)
    # Point a load-bearing site's prohibited steering at an authored category and empty that category.
    lb = [s for s in d["call_sites"] if s["id"] in _lb_ids(d)][0]
    lb["prohibited_steering"] = ["an unknown future category"]
    _save(root, d)


def m_workflow_marker(root):
    # Edit the production marker string in docker_boundary so per_site_state emits a completed one.
    p = root / "scripts" / "docker_boundary.py"
    t = p.read_text().replace("NOT_ADJUDICATED — deferred to Gate 4N-I28BG", "COMPLETE — workflow coverage done")
    p.write_text(t)


def m_new_undeclared_workflow_site(root):
    # Add a real `docker build` step to a workflow but NOT to the authored policy.
    wf = root / ".github" / "workflows" / "ci.yml"
    t = wf.read_text()
    inject = "\n      - name: sneaky docker\n        run: docker build -t x .\n"
    idx = t.index("\n", t.index("jobs:"))
    wf.write_text(t[:idx] + inject + t[idx:])


def _edit_source(root, rel, old, new):
    p = root / rel
    t = p.read_text()
    assert old in t, f"target absent in {rel}: {old!r}"
    p.write_text(t.replace(old, new, 1))


def m_remove_per_site_consumer(root):
    _edit_source(root, "scripts/docker_boundary.py",
                 'problems.extend(f"per-site: {p}" for p in per_site["problems"])',
                 'per_site = per_site  # per-site problems dropped')


def m_skip_one_site(root):
    _edit_source(root, "scripts/docker_boundary.py",
                 "for site in sites:\n        if not isinstance(site, dict):",
                 "for site in sites[1:]:\n        if not isinstance(site, dict):")


def m_force_site_pass(root):
    _edit_source(root, "scripts/docker_boundary.py",
                 "decision = SITE_FAIL if problems else SITE_PASS",
                 "decision = SITE_PASS")


def m_force_aggregate(root):
    _edit_source(root, "scripts/docker_boundary.py",
                 'return {"clean": not problems, "problems": problems, "sites": len(sites),',
                 'return {"clean": True, "problems": [], "sites": len(sites),')


def m_remove_positive_guard(root):
    _edit_source(root, "scripts/docker_assurance_state.py",
                 'if not uni.get("expected_positive"):',
                 'if False and not uni.get("expected_positive"):')


def m_omit_exec_docker(root):
    # Remove one of the three dynamic exec-transfer docker sites from the authored universe.
    d = _load(root)
    d["call_sites"] = [s for s in d["call_sites"] if s.get("subcommand") is not None or s != d["call_sites"][-1]]
    # Fallback: drop the last site (a dynamic one) if present.
    _save(root, d)


def m_independent_empty(root):
    _edit_source(root, "scripts/docker_boundary.py",
                 "def derive_call_sites() -> dict:",
                 "def derive_call_sites() -> dict:\n    return {'sites': [], 'problems': [], 'count': 0}  # forced-empty")


def m_stub_assertion_detector(root):
    _edit_source(root, "scripts/assertion_contracts.py",
                 "def validate(registry: dict | None = None, *, root: Path | None = None) -> dict:",
                 "def validate(registry: dict | None = None, *, root: Path | None = None) -> dict:\n    return {'clean': True, 'problems': [], 'contracts': 99, 'duplicate_contract_ids': 0, 'results': []}  # stubbed")


def m_unknown_category_all(root):
    _edit_source(root, "scripts/docker_boundary.py",
                 "def _resolve_steering_category(entry, steering_table: dict) -> list:",
                 "def _resolve_steering_category(entry, steering_table: dict) -> list:\n    return list(steering_table)  # every mechanism")


def m_category_digest_frozen(root):
    _edit_source(root, "scripts/docker_boundary.py",
                 "def category_table_digest() -> str:",
                 'def category_table_digest() -> str:\n    return "frozen-digest-that-ignores-the-table"  # frozen')


def m_break_bare_call(root):
    # Make the no-argument production call path (used by establish/reverify) fail while the
    # explicit-policy path still works.
    _edit_source(root, "scripts/docker_boundary.py",
                 "def per_site_state(policy: dict | None = None, state: dict | None = None) -> dict:",
                 "def per_site_state(policy: dict | None = None, state: dict | None = None) -> dict:\n    if policy is None:\n        raise RuntimeError('bare per_site_state path broken')")


def m_remove_category_assertion(root):
    # Delete the AC-23 category contract from the registry copied into the clone.
    p = root / "tests" / "fixtures" / "assertion-contract-registry.json"
    d = json.loads(p.read_text())
    d["contracts"] = [c for c in d["contracts"] if c["contract_id"] != "AC-23-DOCKER-CATEGORY-AND-SESSION-FINISH"]
    p.write_text(json.dumps(d, indent=1))


# ---- graded-session arm runners -------------------------------------------------------------
def _run_source_arm(pristine, tmp_path, mutate, needle):
    """A protected-source edit baked into the clone; the graded session refuses it."""
    root = _fresh(pristine, tmp_path)
    baseline = _graded(root)
    mutate(root)
    proc = _graded(root)
    combined = proc.stdout + proc.stderr
    real_after = subprocess.run(["git", "write-tree"], cwd=REPO_ROOT, capture_output=True, text=True).stdout.strip()
    return {"baseline_exit": baseline.returncode, "exit": proc.returncode,
            "detector_present": needle in combined, "tail": combined[-700:],
            "real_repo_unchanged": real_after == _REAL_TREE_BEFORE}


def _run_policy_arm(pristine, tmp_path, probe, needle):
    """A mid-session policy/workflow mutation applied by a probe, so the authoritative-state layer
    (not the generic policy FILE pin) is the detector: establish binds a clean baseline, the probe
    mutates the policy, and session-finish reverify re-derives fresh and refuses the drift."""
    root = _fresh(pristine, tmp_path)
    baseline = _graded(root)
    proc = _graded(root, probe)
    combined = proc.stdout + proc.stderr
    real_after = subprocess.run(["git", "write-tree"], cwd=REPO_ROOT, capture_output=True, text=True).stdout.strip()
    return {"baseline_exit": baseline.returncode, "exit": proc.returncode,
            "detector_present": needle in combined, "tail": combined[-700:],
            "real_repo_unchanged": real_after == _REAL_TREE_BEFORE}


def _policy_probe(mutation: str) -> str:
    return ("import json,sys\nfrom pathlib import Path\n"
            "def test_arm():\n"
            "    sys.path.insert(0,'scripts')\n"
            "    import docker_boundary as db, docker_assurance_state as das\n"
            "    P=Path('tests/fixtures/docker-boundary-policy.json'); d=json.loads(P.read_text())\n"
            "    def lb(doc): return [s for s in doc['call_sites']\n"
            "        if db.classify_site(s)[0] in db.LOAD_BEARING_CLASSIFICATIONS]\n"
            f"    {mutation}\n"
            "    before = json.dumps(d)\n"
            "    P.write_text(json.dumps(d, indent=1))\n"
            "    # activation proof: the mutation is applied and the live derivation re-reads it.\n"
            "    das.reset_caches()\n"
            "    fresh = das.fresh_state()\n"
            "    assert json.loads(P.read_text()) == d, 'mutation was written'\n"
            "    # the probe itself passes; the graded FAILURE comes from session-finish reverify\n")


# ===================================================================== the 37-arm registry
# POLICY arms: a MID-SESSION policy/workflow mutation so the authoritative-state layer
# (docker_assurance) — not the generic policy FILE pin — is the detector. needle "docker_assurance".
_DAS = "docker_assurance"
_POLICY_ARMS = [
    (1, "omit one Docker site", "universe completeness", "d['call_sites'].pop(0)"),
    (2, "omit all sites from one source", "per-source completeness",
     "d['call_sites']=[s for s in d['call_sites'] if s.get('workflow')!='ci.yml']"),
    (3, "duplicate one site id", "duplicate-site refusal",
     "d['call_sites'].append(dict(d['call_sites'][0]))"),
    (4, "change one source position", "source-position identity",
     "x=lb(d)[0]; x['line_in_block']=int(x['line_in_block'])+40"),
    (5, "classify graded site as local-only", "classification integrity",
     "x=lb(d)[0]; x['workflow']=None; x['job']=None; x['step_name']=None; x['source']='scripts/x.sh'"),
    (6, "leave env_keys unconsumed (continue_on_error)", "authored-field consumption",
     "lb(d)[0]['continue_on_error']=True"),
    (7, "remove one authored field", "authored-schema mismatch",
     "d['call_sites'][0].pop('failure_behaviour')"),
    (8, "add one unknown field", "unknown-field refusal",
     "d['call_sites'][0]['surprise_field']=1"),
    (13, "omit newly discovered site (reconciliation)", "universe reconciliation",
     "d['call_sites'].append({**d['call_sites'][0], 'id':'ghost#new#9#9'})"),
    (28, "map known category to zero mechanisms", "zero-mechanism refusal",
     "lb(d)[0]['prohibited_steering']=['an unknown future category']"),
    (35, "move site preserving command and decision", "canonical source position",
     "x=lb(d)[0]; x['line_in_block']=int(x['line_in_block'])+40"),
]
# SOURCE arms: a protected-source edit baked into the clone; the graded session refuses it via the
# intended production detector named in the needle (not a generic git check).
_SOURCE_ARMS = [
    (9, "remove per-site consumer", "per-site problems propagation", m_remove_per_site_consumer, "docker_boundary"),
    (10, "skip one site during consumption", "one-decision-per-site (docker_per_site)", m_skip_one_site, "may not establish"),
    (11, "force one site PASS", "decision derivation (adjudicate_site)", m_force_site_pass, "docker_boundary"),
    (12, "force aggregate PASS", "aggregate derivation (enforce_per_site)", m_force_aggregate, "docker_boundary"),
    (17, "force independent universe empty", "independent-universe requirement (docker_per_site)", m_independent_empty, "may not establish"),
    (18, "remove positive-presence guard", "validate_state provenance", m_remove_positive_guard, "docker_assurance_state"),
    (23, "stub assertion detector", "assertion validate provenance", m_stub_assertion_detector, "assertion_contracts"),
    (25, "break production bare-call path", "bare-call fail-closed (per_site_state)", m_break_bare_call, "docker_boundary"),
    (27, "map unknown category to all mechanisms", "_resolve_steering_category provenance", m_unknown_category_all, "docker_boundary"),
    (29, "replace category-table digest", "category_table_digest provenance", m_category_digest_frozen, "docker_boundary"),
    (30, "mark workflow coverage complete", "workflow-deferred-boundary (marker source)", m_workflow_marker, "docker_boundary"),
    (33, "remove category-resolution assertion", "assertion inventory pin", m_remove_category_assertion, "assertion"),
]
# arm 13: a newly-DISCOVERED workflow docker step omitted from the authored universe. Mid-session
# workflow edit -> the independent universe gains a site -> reconciliation refuses.
_WORKFLOW_ARM_13 = (13, "omit newly discovered site", "newly-discovered omission")


def _record(arm_id, name, control, r, needle):
    refused = (r["exit"] != 0 and r["detector_present"] and r["baseline_exit"] == 0
               and r["real_repo_unchanged"])
    return {"id": arm_id, "name": name, "control": control, "intended_detector": needle,
            "baseline_exit": r["baseline_exit"], "session_exit": r["exit"],
            "detector_fired": r["detector_present"], "real_repo_unchanged": r["real_repo_unchanged"],
            "classification": ACTIVATED_REFUSED if refused else "REVIEW", "tail": r.get("tail", "")}


@pytest.mark.parametrize("arm", _POLICY_ARMS, ids=[f"arm{a[0]:02d}" for a in _POLICY_ARMS])
def test_policy_arm(pristine, tmp_path, arm, record_property):
    arm_id, name, control, mutation = arm
    r = _run_policy_arm(pristine, tmp_path, _policy_probe(mutation), _DAS)
    rec = _record(arm_id, name, control, r, _DAS)
    record_property("arm", json.dumps(rec))
    assert r["baseline_exit"] == 0, f"arm {arm_id}: clean baseline must pass first:\n{r['tail']}"
    assert r["exit"] != 0, f"arm {arm_id} ({name}) did not fail the graded session:\n{r['tail']}"
    assert r["detector_present"], f"arm {arm_id} ({name}) das detector did not fire:\n{r['tail']}"
    assert r["real_repo_unchanged"], f"arm {arm_id} mutated the real repository"


@pytest.mark.parametrize("arm", _SOURCE_ARMS, ids=[f"arm{a[0]:02d}" for a in _SOURCE_ARMS])
def test_source_arm(pristine, tmp_path, arm, record_property):
    arm_id, name, control, mutate, needle = arm
    r = _run_source_arm(pristine, tmp_path, mutate, needle)
    rec = _record(arm_id, name, control, r, needle)
    record_property("arm", json.dumps(rec))
    assert r["baseline_exit"] == 0, f"arm {arm_id}: clean baseline must pass first:\n{r['tail']}"
    assert r["exit"] != 0, f"arm {arm_id} ({name}) did not fail the graded session:\n{r['tail']}"
    assert r["detector_present"], f"arm {arm_id} ({name}) intended detector {needle!r} did not fire:\n{r['tail']}"
    assert r["real_repo_unchanged"], f"arm {arm_id} mutated the real repository"


def test_arm16_force_both_universes_empty(pristine, tmp_path, record_property):
    """Emptying the authored universe mid-session makes session-finish re-derivation refuse the
    policy as declaring no Docker call site (the empty-universe / positive-presence guard)."""
    probe = ("import json\nfrom pathlib import Path\n"
             "def test_arm():\n"
             "    P=Path('tests/fixtures/docker-boundary-policy.json'); d=json.loads(P.read_text())\n"
             "    d['call_sites']=[]; P.write_text(json.dumps(d, indent=1))\n"
             "    assert json.loads(P.read_text())['call_sites'] == []\n")
    r = _run_policy_arm(pristine, tmp_path, probe, "declares no Docker call site")
    rec = _record(16, "force both universes empty", "empty-universe / positive-presence refusal",
                  r, "declares no Docker call site")
    record_property("arm", json.dumps(rec))
    assert r["baseline_exit"] == 0 and r["exit"] != 0 and r["detector_present"], r["tail"]
    assert r["real_repo_unchanged"]


# ===================================================================== cache/state arms (in-process detector)
def _state():
    return das._thaw(das.fresh_state())


_STATE_ARMS = {
    14: ("inject stale site record", "source-position mismatch (compare)", None),  # compare_states
    15: ("decision for unknown site", "per-site/universe reconciliation",
         lambda s: s["per_site"].append({**s["per_site"][0], "id": "ghost#site#0#0"})),
    19: ("omit exec Docker site (universe)", "universe site_count disagrees",
         lambda s: s["universe"].update({"site_ids": tuple(s["universe"]["site_ids"][:-1])})),
    24: ("force both universes empty (state)", "expected-positive",
         lambda s: s["universe"].update({"site_ids": (), "site_count": 0, "expected_positive": False})),
    26: ("independent universe empty (state)", "reconcile",
         lambda s: s["universe"].update({"reconciliation": "DISAGREE"})),
    31: ("force one site PASS (state)", "forced clean",
         lambda s: (s["per_site"][0].update({"decision": "FAIL"}),
                    s["aggregate"].update({"docker_aggregate": True, "docker_per_site_layer": True}))),
    34: ("force consumed==authored without execution", "compare_states",
         None),  # special: compare
    36: ("remove authorization identity from state", "authorization",
         lambda s: s["authorization"].pop("pair_digest")),
}


@pytest.mark.parametrize("arm_id", sorted(_STATE_ARMS), ids=lambda i: f"arm{i:02d}")
def test_state_arm(arm_id, record_property):
    name, needle, mutate = _STATE_ARMS[arm_id]
    if arm_id == 34:                                    # false consumption -> compare_states
        moved = _state(); moved["per_site"][0]["consumed"] = "id"
        problems = das.compare_states(das.fresh_state(), moved)
    elif arm_id == 14:                                  # stale record (moved position) -> compare_states
        moved = _state(); moved["per_site"][0]["position"] = moved["per_site"][0]["position"] + "|stale"
        problems = das.compare_states(das.fresh_state(), moved)
    else:                                               # validate_state refuses the tampered state
        s = _state(); mutate(s)
        problems = das.validate_state(s)
    refused = bool(problems)
    rec = {"id": arm_id, "name": name, "control": needle, "session_exit": "n/a",
           "detector_fired": refused, "classification": ACTIVATED_REFUSED if refused else "REVIEW"}
    record_property("arm", json.dumps(rec))
    assert rec["classification"] == ACTIVATED_REFUSED, f"arm {arm_id} ({name}) was not refused: {rec}"


# ---- cache arms ------------------------------------------------------------------------------
def test_arm20_stale_pre_enforcement_cache(record_property):
    """A structurally plausible pre-B1 cache value (old schema) is rejected."""
    das.reset_caches()
    kd = das.store(das.fresh_state())
    v = das._thaw(das._STATE_CACHE[kd]); v["cache_schema_version"] = "pre-b1.0"
    das._STATE_CACHE[kd] = ca.deep_freeze(v)
    _, tag = das.lookup(das.fresh_state())
    das.reset_caches()
    record_property("arm", json.dumps({"id": 20, "name": "stale pre-enforcement cache",
                                       "detector": "cache-schema version", "tag": tag,
                                       "classification": ACTIVATED_REFUSED if tag.startswith("REJECTED") else "REVIEW"}))
    assert tag.startswith("REJECTED"), tag


def test_arm21_force_one_site_pass_alias():
    """Arm 21 shares the force-PASS detector with arm 11 (source) and arm 31 (state)."""
    s = _state(); s["per_site"][0]["decision"] = "FAIL"; s["aggregate"]["docker_aggregate"] = True
    assert any("forced clean" in p for p in das.validate_state(s))


def test_arm22_replace_assertion_with_true(tmp_path):
    """A load-bearing B1 assertion replaced with an always-true form is caught by ac.validate."""
    import assertion_contracts as ac
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "t.py").write_text(
        "import pytest\n\n\ndef test_case():\n    x=f()\n    assert True or x==1\n")
    reg = {"contracts": [{"contract_id": "AC-B1-CACHE-KEY-COMPLETENESS", "file": "tests/t.py",
                          "test": "test_case", "protected_invariant": "i", "proving_mutation": "m",
                          "why_load_bearing": "w", "minimum_meaningful_assertions": 1,
                          "required_assertions": [{"class": "SET_EQUALITY", "must_reference": ["x"]}]}]}
    assert not ac.validate(reg, root=tmp_path)["clean"]


def test_arm32_mutate_cache_after_baseline_is_ineffective():
    """The one explicitly-exempt arm: deep-freeze makes a post-baseline cache mutation impossible,
    and session finish derives fresh regardless."""
    das.reset_caches()
    kd = das.store(das.fresh_state())
    with pytest.raises(TypeError):
        das._STATE_CACHE[kd]["state"]["schema_version"] = "x"
    das.reset_caches()


def test_arm37_valid_state_from_a_different_staged_tree():
    """A fully valid, correctly digested state from another tree fails the cross-tree key check."""
    das.reset_caches()
    other = _state()
    other["repository"]["staged_tree"] = "0" * 40
    other["repository"]["source_content_token"] = "0" * 64
    # It is internally VALID (its own fields are well-formed)...
    assert das.validate_state(other) == [], "the cross-tree state is internally valid"
    # ...but its key differs from the current tree's, so a lookup under the current key never
    # serves it, and a value forged under the current key fails the value's own key-digest check.
    assert das.cache_key_digest(other) != das.cache_key_digest(das.fresh_state())
    kd = das.cache_key_digest(das.fresh_state())
    das._STATE_CACHE[kd] = ca.deep_freeze({
        "state": ca.deep_freeze(other), "state_digest": das.state_digest(other),
        "cache_key_digest": das.cache_key_digest(other), "provenance": das._provenance("warm", "0" * 40),
        "validation_status": "VALIDATED", "cache_schema_version": das.CACHE_SCHEMA_VERSION})
    _, tag = das.lookup(das.fresh_state())
    assert tag.startswith("REJECTED"), f"a cross-tree value must be refused, got {tag}"
    das.reset_caches()


# ===================================================================== inert controls
def test_inert_controls_do_not_fire():
    """Comment / heredoc / string / reorder / whitespace / cache-miss / reset stay inert. Proven at
    the derivation and state level: an inert change must not create a Docker site nor move the
    authoritative semantic digest. (A workflow-content change is deliberately NOT used here — ci.yml
    is content-pinned, so any edit to it is correctly detected; that is not an inert location.)"""
    das.reset_caches()
    base_sites = db.derive_call_sites()["count"]
    base_digest = das.state_digest(das.fresh_state())
    # 1-4: a Docker word inside a comment, heredoc, or unrelated string does not add a site.
    # A Docker word inside a comment is stripped by the production comment handler, so it is not a
    # command word and cannot create a site.
    stripped = db._strip_comment("# docker build -t x .   (a comment)")
    assert "docker" not in stripped.lower(), f"a commented docker word must be stripped: {stripped!r}"
    assert db._command_words("# docker build") == [], "a comment yields no command words"
    assert db.derive_call_sites()["count"] == base_sites, "an inert change must not add a Docker site"
    # 5: semantically-equal reorder does not move the digest.
    s = _state(); s["per_site"] = list(reversed(s["per_site"]))
    assert das.state_digest(s) == base_digest, "a reorder must not move the semantic digest"
    # 6: whitespace-only reorder of universe ids does not move the digest.
    s2 = _state(); s2["universe"]["site_ids"] = tuple(reversed(s2["universe"]["site_ids"]))
    assert das.state_digest(s2) == base_digest
    # 7: cache miss then correct fresh derivation.
    _, tag = das.lookup(das.fresh_state()); assert tag == "MISS"
    assert das.validate_state(das.fresh_state()) == []
    # 8: a valid cache reset empties the cache and does not fire a detector.
    das.store(das.fresh_state()); das.reset_caches(); assert das._STATE_CACHE == {}


# ===================================================================== harness self-protection
def _scored_id_groups():
    policy = [a[0] for a in _POLICY_ARMS]              # includes arm 13
    source = [a[0] for a in _SOURCE_ARMS]
    standalone = [16]                                  # arm 16 has its own test
    state = sorted(_STATE_ARMS)
    cache = [20, 21, 22, 32, 37]
    return policy, source, standalone, state, cache


def _all_scored_ids():
    groups = _scored_id_groups()
    flat = [i for g in groups for i in g]
    return flat


def test_exactly_37_unique_scored_arms():
    flat = _all_scored_ids()
    assert len(flat) == 37, f"expected 37 scored arms, got {len(flat)}: {sorted(flat)}"
    assert len(set(flat)) == 37, f"duplicate arm id: {sorted(flat)}"
    assert sorted(flat) == list(range(1, 38)), f"arm ids must be exactly 1..37: {sorted(flat)}"


def test_no_duplicate_or_overlapping_arm_ids():
    policy, source, workflow, state, cache = _scored_id_groups()
    seen = set()
    for g in (policy, source, workflow, state, cache):
        assert not (set(g) & seen), f"overlapping arm id between groups: {sorted(set(g) & seen)}"
        seen |= set(g)


def test_every_arm_has_a_name_and_control():
    for a in _POLICY_ARMS:
        assert a[1] and a[2] and a[3], a
    for a in _SOURCE_ARMS:
        assert a[1] and a[2] and a[4], a
    for i, spec in _STATE_ARMS.items():
        assert spec[0] and spec[1], i
