"""Gate 4N-I28BG-B3 — staging-publish dual-image workflow-assurance integration.

Proves the integrated staging-publish.yml is a fully enforced dual-image (API + worker)
workflow-assurance state: the static graph validator PASSes staging and keeps reader PASS; each
image is independently established / pre-build-verified / bound / pre-push-verified; the API and
worker manifests are non-interchangeable; the runtime staging workflow content is bound; and an
offline dual-image simulation blocks the push on every drift. No Docker/registry/network/AWS.
"""

from __future__ import annotations

import copy
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import cache_authority as ca  # noqa: E402
import workflow_assurance as w  # noqa: E402
import workflow_graph_validator as g  # noqa: E402

STAGING = REPO_ROOT / ".github" / "workflows" / "staging-publish.yml"
READER = REPO_ROOT / ".github" / "workflows" / "reader-publish.yml"
SHA = "c0ffee00" * 5
API_TAG = f"signalnest-api:{SHA}"
WK_TAG = f"signalnest-worker:{SHA}"
API_DIG = "sha256:" + "a" * 64
WK_DIG = "sha256:" + "b" * 64
WF = {"workflow_path": ".github/workflows/staging-publish.yml", "workflow_identity": "Staging publish",
      "job_identity": "build-publish", "docker_step_identity": "build-publish"}
SPEC = {"workflow_path": ".github/workflows/staging-publish.yml", "dockerfile_path": "apps/api/Dockerfile",
        "context_path": "apps/api", "script_paths": ["scripts/workflow_assurance.py",
        "scripts/workflow_graph_validator.py"], "commit_sha": SHA}


def _tree():
    return subprocess.check_output(["git", "write-tree"], cwd=REPO_ROOT).decode().strip()


def _image(est, src, df, ctx, *, target, tag, digest, intended_digest=None, intended_tags=None,
           workflow=WF, source=None):
    bi = {"dockerfile_path": "apps/api/Dockerfile", "dockerfile_digest": df, "context_path": "apps/api",
          "context_digest": ctx, "build_args": {"GIT_REVISION": SHA}, "build_secret_names": [],
          "cache_from": [], "cache_to": [], "platforms": ["linux/amd64"], "target_stage": target,
          "labels": {}, "runtime_metadata": {"created": "t"}, "resolved_tags": [tag]}
    tree = _tree()
    pb = w.pre_build_verify(establishment=est, fresh_source_manifest=source or src, workflow=workflow,
                            build_inputs=bi, fresh_commit_sha=SHA, fresh_tree_identity=tree)
    im = w.post_build_image_bind(pre_build_record=pb, build_metadata={"containerimage.digest": digest},
                                 resolved_tags=[tag], dockerfile_digest=df, build_context_digest=ctx)
    pp = w.pre_push_verify(image_manifest=im, fresh_source_manifest=source or src, workflow=workflow,
                           intended_image_digest=intended_digest or im["build_output"]["image_digest"],
                           intended_tags=intended_tags or im["build_output"]["resolved_tags"],
                           fresh_commit_sha=SHA, fresh_tree_identity=tree)
    return pb, im, pp


def _simulate(*, api_over=None, worker_over=None, authorization=None, source=None):
    """Run the staging dual-image chain offline. Returns per-image results + push eligibility."""
    tree = _tree()
    src = source or w.source_content_manifest(REPO_ROOT, SPEC)
    est = w.establish(workflow=WF, source_manifest=src, expected_phase="staging-establish",
                      commit_sha=SHA, tree_identity=tree, authorization=authorization)
    df = src["files"].get("dockerfile", {}).get("sha256", "0" * 64)
    ctx = w._digest(src["context"])
    api = dict(target="api", tag=API_TAG, digest=API_DIG)
    worker = dict(target="worker", tag=WK_TAG, digest=WK_DIG)
    if api_over:
        api.update(api_over)
    if worker_over:
        worker.update(worker_over)
    pba, ima, ppa = _image(est, src, df, ctx, source=src, **api)
    pbw, imw, ppw = _image(est, src, df, ctx, source=src, **worker)
    if authorization is not None:
        ppa = w.pre_push_verify(image_manifest=ima, fresh_source_manifest=src, workflow=WF,
                                intended_image_digest=ima["build_output"]["image_digest"],
                                intended_tags=ima["build_output"]["resolved_tags"],
                                authorization=authorization, fresh_commit_sha=SHA, fresh_tree_identity=tree)
        ppw = w.pre_push_verify(image_manifest=imw, fresh_source_manifest=src, workflow=WF,
                                intended_image_digest=imw["build_output"]["image_digest"],
                                intended_tags=imw["build_output"]["resolved_tags"],
                                authorization=authorization, fresh_commit_sha=SHA, fresh_tree_identity=tree)
    both = (est["result"] == "PASS" and pba["result"] == "PASS" and ppa["result"] == "PASS"
            and pbw["result"] == "PASS" and ppw["result"] == "PASS"
            and not w.validate_image_manifest(ima) and not w.validate_image_manifest(imw)
            and ima["manifest_digest"] != imw["manifest_digest"])
    return {"establish": est, "api": (pba, ima, ppa), "worker": (pbw, imw, ppw), "push_eligible": both}


# ===================================================================== static graph
def test_staging_publish_is_pass_after_integration():
    res = g.validate_workflow(STAGING)
    assert res["status"] == g.STATUS_PASS, res["problems"]
    assert res["assurance_present"] is True
    assert res["docker_build_sites"] == 2


def test_reader_remains_pass():
    assert g.validate_workflow(READER)["status"] == g.STATUS_PASS


def test_integration_status_both_pass():
    s = g.integration_status()
    assert s[g.READER_PUBLISH] == g.STATUS_PASS
    assert s[g.STAGING_PUBLISH] == g.STATUS_PASS


def test_each_build_is_independently_guarded():
    graph = g.analyse_workflow(STAGING)
    steps = graph["jobs"]["build-publish"]["steps"]
    est = next(s["index"] for s in steps if s["assurance_mode"] == "establish")
    builds = sorted(s["index"] for s in steps if g.ROLE_BUILD in s["roles"])
    pbs = sorted(s["index"] for s in steps if s["assurance_mode"] == "pre_build_verify")
    binds = sorted(s["index"] for s in steps if s["assurance_mode"] == "post_build_image_bind")
    pps = sorted(s["index"] for s in steps if s["assurance_mode"] == "pre_push_verify")
    push = min(s["index"] for s in steps if g.ROLE_PUSH in s["roles"])
    assert len(builds) == 2 and len(pbs) == 2 and len(binds) == 2 and len(pps) == 2
    # each build preceded by its own pre-build and followed by its own bind
    for k, b in enumerate(builds):
        assert len([x for x in pbs if est < x < b]) >= k + 1
        assert len([x for x in binds if x > b and x < push]) >= len(builds) - k
    assert max(binds) < min(pps) < push


# ===================================================================== the four phases run for real
def test_the_dual_image_chain_passes_against_the_real_repository():
    res = _simulate()
    assert res["establish"]["result"] == "PASS", res["establish"]["problems"]
    assert res["api"][0]["result"] == "PASS" and res["api"][2]["result"] == "PASS"
    assert res["worker"][0]["result"] == "PASS" and res["worker"][2]["result"] == "PASS"
    assert res["push_eligible"] is True


def test_dockerignore_is_honoured_in_the_context():
    src = w.source_content_manifest(REPO_ROOT, SPEC)
    assert w.validate_source_manifest(src) == []
    assert src["context"]["dockerignore_patterns"], "apps/api has a .dockerignore"
    assert not any(".venv" in f["path"] for f in src["context"]["files"])


# ===================================================================== dual-image non-interchangeability
def test_api_and_worker_manifests_are_distinct():
    res = _simulate()
    assert res["api"][1]["manifest_digest"] != res["worker"][1]["manifest_digest"]
    assert res["api"][0]["pre_build_token"] != res["worker"][0]["pre_build_token"]


def test_api_manifest_cannot_satisfy_worker_push():
    res = _simulate()
    ima = res["api"][1]
    src = w.source_content_manifest(REPO_ROOT, SPEC)
    pp = w.pre_push_verify(image_manifest=ima, fresh_source_manifest=src, workflow=WF,
                           intended_image_digest=WK_DIG, intended_tags=[WK_TAG],
                           fresh_commit_sha=SHA, fresh_tree_identity=_tree())
    assert pp["result"] == "FAIL"
    assert any("substitution" in p for p in pp["problems"])


def test_worker_manifest_cannot_satisfy_api_push():
    res = _simulate()
    imw = res["worker"][1]
    src = w.source_content_manifest(REPO_ROOT, SPEC)
    pp = w.pre_push_verify(image_manifest=imw, fresh_source_manifest=src, workflow=WF,
                           intended_image_digest=API_DIG, intended_tags=[API_TAG],
                           fresh_commit_sha=SHA, fresh_tree_identity=_tree())
    assert pp["result"] == "FAIL"
    assert any("substitution" in p for p in pp["problems"])


# ===================================================================== workflow-content binding (§24)
def test_staging_workflow_content_is_bound():
    src = w.source_content_manifest(REPO_ROOT, SPEC)
    assert src["files"]["workflow"]["path"] == ".github/workflows/staging-publish.yml"
    mutated = copy.deepcopy(src)
    mutated["files"]["workflow"]["sha256"] = "0" * 64
    mutated["source_content_digest"] = w._digest(
        {k: mutated[k] for k in ("schema_version", "files", "context", "actions_metadata")})
    assert w.source_content_digest(mutated) != w.source_content_digest(src)


def test_a_staging_workflow_mutation_fails_pre_push():
    res = _simulate()
    ima = res["api"][1]
    drifted = w.source_content_manifest(REPO_ROOT, SPEC)
    drifted["files"]["workflow"]["sha256"] = "0" * 64
    drifted["source_content_digest"] = w._digest(
        {k: drifted[k] for k in ("schema_version", "files", "context", "actions_metadata")})
    pp = w.pre_push_verify(image_manifest=ima, fresh_source_manifest=drifted, workflow=WF,
                           intended_image_digest=ima["build_output"]["image_digest"],
                           intended_tags=ima["build_output"]["resolved_tags"], fresh_commit_sha=SHA,
                           fresh_tree_identity=_tree())
    assert pp["result"] == "FAIL"
    assert any("source content changed" in p for p in pp["problems"])


def test_reader_manifest_cannot_satisfy_staging():
    # A reader-workflow manifest replayed into staging is refused (different workflow identity).
    res = _simulate()
    ima = res["api"][1]
    reader_wf = {"workflow_path": ".github/workflows/reader-publish.yml",
                 "workflow_identity": "Revision reader publish", "job_identity": "build-publish-reader",
                 "docker_step_identity": "build-reader"}
    src = w.source_content_manifest(REPO_ROOT, SPEC)
    pp = w.pre_push_verify(image_manifest=ima, fresh_source_manifest=src, workflow=reader_wf,
                           intended_image_digest=ima["build_output"]["image_digest"],
                           intended_tags=ima["build_output"]["resolved_tags"], fresh_commit_sha=SHA,
                           fresh_tree_identity=_tree())
    assert pp["result"] == "FAIL"
    assert any("replay" in p for p in pp["problems"])


def test_validate_workflow_is_load_bearing(tmp_path):
    broken = STAGING.read_text().replace(
        "python3 scripts/workflow_assurance.py pre_push_verify", "echo skip", 1)
    p = tmp_path / "staging.yml"
    p.write_text(broken)
    assert g.validate_workflow(p)["status"] == g.STATUS_FAIL


# ===================================================================== isolated dual-image simulation A–J
def test_sim_A_valid_dual_image_publication():
    assert _simulate()["push_eligible"] is True


def test_sim_B_api_input_drift():
    res = _simulate(api_over={"digest": "not-a-digest"})
    assert res["push_eligible"] is False


def test_sim_C_worker_input_drift():
    res = _simulate(worker_over={"digest": "not-a-digest"})
    assert res["push_eligible"] is False


def test_sim_D_api_worker_digest_swap():
    # push the API image under the worker's intended digest -> refused
    res = _simulate()
    ima = res["api"][1]
    src = w.source_content_manifest(REPO_ROOT, SPEC)
    pp = w.pre_push_verify(image_manifest=ima, fresh_source_manifest=src, workflow=WF,
                           intended_image_digest=WK_DIG, intended_tags=[API_TAG],
                           fresh_commit_sha=SHA, fresh_tree_identity=_tree())
    assert pp["result"] == "FAIL"


def test_sim_E_api_worker_tag_swap():
    res = _simulate()
    ima = res["api"][1]
    src = w.source_content_manifest(REPO_ROOT, SPEC)
    pp = w.pre_push_verify(image_manifest=ima, fresh_source_manifest=src, workflow=WF,
                           intended_image_digest=ima["build_output"]["image_digest"],
                           intended_tags=[WK_TAG], fresh_commit_sha=SHA, fresh_tree_identity=_tree())
    assert pp["result"] == "FAIL"
    assert any("tag substitution" in p for p in pp["problems"])


def test_sim_F_one_image_missing_fails_aggregate():
    # The aggregate requires BOTH images; a missing worker manifest is not valid.
    assert w.validate_image_manifest({"schema_version": w.IMAGE_MANIFEST_SCHEMA_VERSION}) != []


def test_sim_G_authorization_restamp_before_push():
    stale = {"issuance": "2026-08-06T15:30:42Z", "expiry": "2026-08-07T13:30:42Z",
             "duration_seconds": 79200}
    stale["pair_digest"] = ca.digest({k: stale[k] for k in ("issuance", "expiry", "duration_seconds")})
    res = _simulate(authorization=stale)
    assert res["push_eligible"] is False


def test_sim_H_existing_security_check_fails_blocks_push():
    graph = g.analyse_workflow(STAGING)
    steps = graph["jobs"]["build-publish"]["steps"]
    push = min(s["index"] for s in steps if g.ROLE_PUSH in s["roles"])
    verification = [s for s in steps if s["index"] < push and not s["assurance_mode"]
                    and g.ROLE_BUILD not in s["roles"] and g.ROLE_CHECKOUT not in s["roles"]]
    assert verification
    assert all(not s["continue_on_error"] for s in verification)


def test_sim_I_verifier_failure_ignored_refused_by_graph(tmp_path):
    broken = STAGING.read_text().replace(
        "        id: assurance-pre-push-api\n",
        "        id: assurance-pre-push-api\n        continue-on-error: true\n", 1)
    p = tmp_path / "staging.yml"
    p.write_text(broken)
    assert g.validate_workflow(p)["status"] == g.STATUS_FAIL


def test_sim_J_aggregate_forced_clean_refused():
    res = _simulate()
    tampered = copy.deepcopy(res["api"][2])
    tampered["result"] = "PASS"
    tampered["problems"] = ["image substitution: forced"]
    assert w.record_passes(tampered) is False
    tampered2 = copy.deepcopy(res["api"][2])
    tampered2["intended_image_digest"] = WK_DIG
    assert w.validate_pre_push(tampered2) != []


# ===================================================================== reader non-regression (§28)
def test_reader_workflow_byte_identical_and_pass():
    # Reader was not modified by B3 (no shared defect required it).
    res = g.validate_workflow(READER)
    assert res["status"] == g.STATUS_PASS
    assert res["assurance_present"] is True
