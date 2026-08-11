"""Gate 4N-I28BG-B2 — reader-publish workflow-assurance integration.

Proves the integrated reader-publish.yml is a fully enforced workflow-assurance state: the static
graph validator PASSes reader (and leaves staging NOT_YET_INTEGRATED), the four assurance phases run
in order over the real repository, the runtime reader workflow CONTENT is bound by the enforcement
path, and an offline end-to-end simulation blocks the push on every drift. No Docker/registry/network.
"""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import workflow_assurance as w  # noqa: E402
import workflow_graph_validator as g  # noqa: E402

READER = REPO_ROOT / ".github" / "workflows" / "reader-publish.yml"
STAGING = REPO_ROOT / ".github" / "workflows" / "staging-publish.yml"
SHA = "c0ffee00" * 5
TAG = f"signalnest-revision-reader:{SHA}"
WF = {"workflow_path": ".github/workflows/reader-publish.yml",
      "workflow_identity": "Revision reader publish", "job_identity": "build-publish-reader",
      "docker_step_identity": "build-reader"}
SPEC = {"workflow_path": ".github/workflows/reader-publish.yml",
        "dockerfile_path": "apps/revision-reader/Dockerfile", "context_path": "apps/revision-reader",
        "script_paths": ["scripts/workflow_assurance.py", "scripts/workflow_graph_validator.py"],
        "commit_sha": SHA}


def _tree():
    return subprocess.check_output(["git", "write-tree"], cwd=REPO_ROOT).decode().strip()


def _simulate_reader(*, source_spec=None, build_metadata=None, resolved_tags=None,
                     intended_digest=None, intended_tags=None, authorization=None,
                     drift_commit=None, drift_tree=None):
    """Run the reader four-phase chain offline, exactly as the integrated workflow does. Returns a
    dict of per-phase results and the resulting push eligibility."""
    tree = _tree()
    spec = source_spec or SPEC
    src = w.source_content_manifest(REPO_ROOT, spec)
    est = w.establish(workflow=WF, source_manifest=src, expected_phase="reader-establish",
                      commit_sha=SHA, tree_identity=tree)
    df = src["files"]["dockerfile"]["sha256"] if "dockerfile" in src["files"] else "0" * 64
    ctx = w._digest(src["context"])
    bi = {"dockerfile_path": "apps/revision-reader/Dockerfile", "dockerfile_digest": df,
          "context_path": "apps/revision-reader", "context_digest": ctx,
          "build_args": {"GIT_REVISION": SHA}, "build_secret_names": [], "cache_from": [],
          "cache_to": [], "platforms": ["linux/amd64"], "target_stage": "reader", "labels": {},
          "runtime_metadata": {"created": "t"}, "resolved_tags": [TAG]}
    pb = w.pre_build_verify(establishment=est, fresh_source_manifest=src, workflow=WF,
                            build_inputs=bi, fresh_commit_sha=SHA, fresh_tree_identity=tree)
    im = w.post_build_image_bind(pre_build_record=pb,
                                 build_metadata=build_metadata or {"containerimage.digest": "sha256:" + "a" * 64},
                                 resolved_tags=resolved_tags or [TAG], dockerfile_digest=df,
                                 build_context_digest=ctx)
    im_ok = not w.validate_image_manifest(im)
    pp = w.pre_push_verify(
        image_manifest=im, fresh_source_manifest=src, workflow=WF,
        intended_image_digest=intended_digest or im["build_output"]["image_digest"],
        intended_tags=intended_tags or im["build_output"]["resolved_tags"],
        authorization=authorization,
        fresh_commit_sha=drift_commit or SHA, fresh_tree_identity=drift_tree or tree)
    push_eligible = (est["result"] == "PASS" and pb["result"] == "PASS" and im_ok
                     and pp["result"] == "PASS")
    return {"establish": est, "pre_build": pb, "image_manifest": im, "image_ok": im_ok,
            "pre_push": pp, "push_eligible": push_eligible}


# ===================================================================== static graph PASS
def test_reader_publish_is_pass_after_integration():
    res = g.validate_workflow(READER)
    assert res["status"] == g.STATUS_PASS, res["problems"]
    assert res["assurance_present"] is True
    assert res["single_job"] is True


def test_staging_publish_integrated_after_b3():
    # Reader integration (B2) left staging NOT_YET_INTEGRATED; Gate 4N-I28BG-B3 has since integrated
    # it. Reader remains PASS regardless.
    assert g.validate_workflow(STAGING)["status"] == g.STATUS_PASS
    assert g.validate_workflow(READER)["status"] == g.STATUS_PASS


def test_integration_status_both_pass_after_b3():
    s = g.integration_status()
    assert s[g.READER_PUBLISH] == g.STATUS_PASS
    assert s[g.STAGING_PUBLISH] == g.STATUS_PASS


def test_all_four_assurance_phases_present_in_order():
    graph = g.analyse_workflow(READER)
    steps = graph["jobs"]["build-publish-reader"]["steps"]
    idx = {s["assurance_mode"]: s["index"] for s in steps if s["assurance_mode"]}
    co = next(s["index"] for s in steps if g.ROLE_CHECKOUT in s["roles"])
    build = min(s["index"] for s in steps if g.ROLE_BUILD in s["roles"])
    push = min(s["index"] for s in steps if g.ROLE_PUSH in s["roles"])
    assert co < idx["establish"] < idx["pre_build_verify"] < build
    assert build < idx["post_build_image_bind"] < idx["pre_push_verify"] < push


def test_no_assurance_step_uses_continue_on_error():
    graph = g.analyse_workflow(READER)
    for s in graph["jobs"]["build-publish-reader"]["steps"]:
        if s["assurance_mode"]:
            assert s["continue_on_error"] is False


# ===================================================================== the four phases run for real
def test_the_four_phases_pass_against_the_real_repository():
    res = _simulate_reader()
    assert res["establish"]["result"] == "PASS", res["establish"]["problems"]
    assert res["pre_build"]["result"] == "PASS", res["pre_build"]["problems"]
    assert res["image_ok"] is True
    assert res["pre_push"]["result"] == "PASS", res["pre_push"]["problems"]
    assert res["push_eligible"] is True


def test_establish_binds_the_authoritative_docker_state():
    res = _simulate_reader()
    ds = res["establish"]["docker_state"]
    import docker_assurance_state as das
    assert ds["state_digest"] == das.state_digest(das.fresh_state())
    assert ds["site_count"] == 50 and ds["load_bearing_count"] > 0


# ===================================================================== workflow-content binding (§18)
def test_the_reader_workflow_content_is_bound_by_the_source_manifest():
    src = w.source_content_manifest(REPO_ROOT, SPEC)
    assert "workflow" in src["files"]
    assert src["files"]["workflow"]["path"] == ".github/workflows/reader-publish.yml"
    # The workflow digest participates in source_content_digest, which establish/pre-build/pre-push bind.
    mutated = copy.deepcopy(src)
    mutated["files"]["workflow"]["sha256"] = "0" * 64
    mutated["source_content_digest"] = w._digest(
        {k: mutated[k] for k in ("schema_version", "files", "context", "actions_metadata")})
    assert w.source_content_digest(mutated) != w.source_content_digest(src)


def test_a_workflow_file_mutation_fails_pre_push():
    # A source manifest whose workflow content changed after the image was bound is refused at push.
    res = _simulate_reader()
    im = res["image_manifest"]
    drifted = w.source_content_manifest(REPO_ROOT, SPEC)
    drifted["files"]["workflow"]["sha256"] = "0" * 64
    drifted["source_content_digest"] = w._digest(
        {k: drifted[k] for k in ("schema_version", "files", "context", "actions_metadata")})
    pp = w.pre_push_verify(image_manifest=im, fresh_source_manifest=drifted, workflow=WF,
                           intended_image_digest=im["build_output"]["image_digest"],
                           intended_tags=im["build_output"]["resolved_tags"],
                           fresh_commit_sha=SHA, fresh_tree_identity=_tree())
    assert pp["result"] == "FAIL"
    assert any("source content changed" in p for p in pp["problems"])


def test_validate_workflow_is_load_bearing_not_integration_status():
    # The load-bearing static enforcement is validate_workflow; integration_status is a thin wrapper.
    # A broken reader integration is caught by validate_workflow regardless of integration_status.
    text = READER.read_text()
    broken = text.replace(
        "python3 scripts/workflow_assurance.py pre_push_verify", "echo skip-pre-push", 1)
    import tempfile
    p = Path(tempfile.mkdtemp()) / "reader.yml"
    p.write_text(broken)
    assert g.validate_workflow(p)["status"] == g.STATUS_FAIL


def test_a_display_name_string_cannot_satisfy_workflow_binding(tmp_path):
    # A step merely NAMED to look like an assurance step, with no real invocation, does not count.
    text = READER.read_text()
    # Neutralise the real establish invocation but keep a decoy name mentioning "establish".
    broken = text.replace("python3 scripts/workflow_assurance.py establish",
                          "echo assurance-establish-decoy", 1)
    p = tmp_path / "reader.yml"
    p.write_text(broken)
    assert g.validate_workflow(p)["status"] == g.STATUS_FAIL


# ===================================================================== isolated simulation A–H
def test_sim_A_valid_reader_workflow():
    assert _simulate_reader()["push_eligible"] is True


def test_sim_B_source_drift_before_build():
    # A Dockerfile that vanished from the declared source set fails pre-build (missing file).
    spec = {**SPEC, "dockerfile_path": "apps/revision-reader/DOES-NOT-EXIST"}
    res = _simulate_reader(source_spec=spec)
    assert res["pre_build"]["result"] == "FAIL"
    assert res["push_eligible"] is False


def test_sim_C_image_substitution_after_build():
    res = _simulate_reader(intended_digest="sha256:" + "b" * 64)
    assert res["pre_push"]["result"] == "FAIL"
    assert res["push_eligible"] is False


def test_sim_D_tag_substitution():
    res = _simulate_reader(intended_tags=["signalnest-revision-reader:deadbeef"])
    assert res["pre_push"]["result"] == "FAIL"
    assert res["push_eligible"] is False


def test_sim_E_authorization_restamp_during_workflow():
    import cache_authority as ca
    stale = {"issuance": "2026-08-06T15:30:42Z", "expiry": "2026-08-07T13:30:42Z",
             "duration_seconds": 79200}
    stale["pair_digest"] = ca.digest({k: stale[k] for k in ("issuance", "expiry", "duration_seconds")})
    res = _simulate_reader(authorization=stale)
    assert res["pre_push"]["result"] == "FAIL"
    assert res["push_eligible"] is False


def test_sim_F_verifier_step_failure_ignored_is_refused_by_graph(tmp_path):
    text = READER.read_text()
    broken = text.replace("        id: assurance-pre-push\n",
                          "        id: assurance-pre-push\n        continue-on-error: true\n", 1)
    p = tmp_path / "reader.yml"
    p.write_text(broken)
    assert g.validate_workflow(p)["status"] == g.STATUS_FAIL


def test_sim_G_existing_reader_security_check_fails_blocks_push():
    # The existing in-image checks are ordinary steps with no continue-on-error, so a failure fails
    # the job before push. Structurally: none of the existing verification steps swallow failure.
    graph = g.analyse_workflow(READER)
    steps = graph["jobs"]["build-publish-reader"]["steps"]
    push = min(s["index"] for s in steps if g.ROLE_PUSH in s["roles"])
    verification = [s for s in steps if s["index"] < push and not s["assurance_mode"]
                    and g.ROLE_BUILD not in s["roles"] and g.ROLE_CHECKOUT not in s["roles"]]
    assert verification, "expected existing verification steps before push"
    assert all(not s["continue_on_error"] for s in verification)


def test_sim_H_aggregate_forced_clean_is_refused():
    # The final aggregate re-validates the emitted records; a forged PASS record with a problem, or a
    # tampered record whose token no longer matches, does not pass the aggregate's re-validation.
    res = _simulate_reader()
    tampered = copy.deepcopy(res["pre_push"])
    tampered["result"] = "PASS"
    tampered["problems"] = ["image substitution: forced"]
    assert w.record_passes(tampered) is False
    tampered2 = copy.deepcopy(res["pre_push"])
    tampered2["intended_image_digest"] = "sha256:" + "b" * 64
    assert w.validate_pre_push(tampered2) != []
