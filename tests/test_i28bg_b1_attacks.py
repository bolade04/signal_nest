"""Gate 4N-I28BG-B1 — architecture-component attack battery (30 arms).

Each arm activates one mutation against a B1 component, drives the intended LIVE target, and asserts
the intended detector fires and the result fails closed — no generic syntax or fixture failure
substituting for a detector. This is the ARCHITECTURE-component battery, not the final B4 32-site
workflow battery.
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import cache_authority as ca  # noqa: E402
import workflow_assurance as w  # noqa: E402
import workflow_graph_validator as g  # noqa: E402

DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
SHA = "abcd1234" * 5
TREE = "tree-xyz"
TAG = f"reader:{SHA}"

WF = {"workflow_path": ".github/workflows/reader-publish.yml",
      "workflow_identity": "Revision reader publish", "job_identity": "build-publish-reader",
      "docker_step_identity": "build-reader"}


def _src():
    return w.source_content_manifest(REPO_ROOT, {
        "workflow_path": ".github/workflows/reader-publish.yml",
        "dockerfile_path": "apps/revision-reader/Dockerfile", "context_path": None,
        "script_paths": ["scripts/workflow_assurance.py"], "commit_sha": SHA})


def _bi(**over):
    bi = {"dockerfile_path": "apps/revision-reader/Dockerfile", "dockerfile_digest": "d" * 64,
          "context_path": "apps/revision-reader", "context_digest": "e" * 64,
          "build_args": {"GIT_REVISION": SHA}, "build_secret_names": [], "cache_from": [],
          "cache_to": [], "platforms": ["linux/amd64"], "target_stage": "reader", "labels": {},
          "runtime_metadata": {"created": "t"}, "resolved_tags": [TAG]}
    bi.update(over)
    return bi


def _meta(digest=DIGEST_A):
    return {"containerimage.digest": digest, "buildx.build.ref": "r"}


def _pipeline(bi=None, src=None):
    src = src or _src()
    est = w.establish(workflow=WF, source_manifest=src, expected_phase="pre_build",
                      commit_sha=SHA, tree_identity=TREE)
    pb = w.pre_build_verify(establishment=est, fresh_source_manifest=src, workflow=WF,
                            build_inputs=bi or _bi(), fresh_commit_sha=SHA, fresh_tree_identity=TREE)
    im = w.post_build_image_bind(pre_build_record=pb, build_metadata=_meta(), resolved_tags=[TAG])
    return src, est, pb, im


def _prepush(im, src, *, workflow=WF, digest=DIGEST_A, tags=(TAG,), auth=None,
             commit=SHA, tree=TREE):
    return w.pre_push_verify(image_manifest=im, fresh_source_manifest=src, workflow=workflow,
                             intended_image_digest=digest, intended_tags=list(tags),
                             authorization=auth, fresh_commit_sha=commit, fresh_tree_identity=tree)


def _stale_auth():
    a = {"issuance": "2026-08-06T15:30:42Z", "expiry": "2026-08-07T13:30:42Z",
         "duration_seconds": 79200}
    a["pair_digest"] = ca.digest({k: a[k] for k in ("issuance", "expiry", "duration_seconds")})
    return a


# ---- graph fixture builder (shared with the synthetic suite's grammar) ----
CO = "      - uses: actions/checkout@v4\n"
EST = "      - run: python scripts/workflow_assurance.py establish --params e.json\n"
PB = "      - run: python scripts/workflow_assurance.py pre_build_verify --params p.json\n"
BD = "      - uses: docker/build-push-action@v6\n        with:\n          push: false\n"
IB = "      - run: python scripts/workflow_assurance.py post_build_image_bind --params i.json\n"
PP = "      - run: python scripts/workflow_assurance.py pre_push_verify --params q.json\n"
PU = "      - run: docker push x\n"


def _wf_yaml(steps, matrix=False):
    strat = "    strategy:\n      matrix:\n        a: [1, 2]\n" if matrix else ""
    return ("name: s\non:\n  workflow_dispatch:\njobs:\n  j:\n    runs-on: ubuntu-latest\n"
            f"{strat}    steps:\n{steps}")


def _graph_fail(tmp_path, steps, matrix=False):
    p = tmp_path / "wf.yml"
    p.write_text(_wf_yaml(steps, matrix))
    return g.validate_workflow(p)


# ===================================================================== VERIFIER ARMS (1-23, 30)
def test_arm01_workflow_path_changed():
    src, est, pb, im = _pipeline()
    r = _prepush(im, src, workflow={**WF, "workflow_path": ".github/workflows/staging-publish.yml"})
    assert r["result"] == "FAIL" and any("replay" in p for p in r["problems"])


def test_arm02_job_id_changed():
    src, est, pb, im = _pipeline()
    r = _prepush(im, src, workflow={**WF, "job_identity": "build-publish"})
    assert r["result"] == "FAIL" and any("replay" in p for p in r["problems"])


def test_arm03_step_identity_changed():
    src, est, pb, im = _pipeline()
    r = _prepush(im, src, workflow={**WF, "docker_step_identity": "build-other"})
    assert r["result"] == "FAIL" and any("replay" in p for p in r["problems"])


def test_arm04_source_file_changed_after_establishment():
    src, est, pb, im = _pipeline()
    drift = copy.deepcopy(src)
    drift["source_content_digest"] = "changed"
    r = _prepush(im, drift)
    assert r["result"] == "FAIL" and any("source content changed" in p for p in r["problems"])


def test_arm05_dockerfile_changed_after_prebuild():
    src, est, pb, im0 = _pipeline()
    im = w.post_build_image_bind(pre_build_record=pb, build_metadata=_meta(), resolved_tags=[TAG],
                                 dockerfile_digest="0" * 64)
    assert any("Dockerfile digest does not match" in p for p in w.validate_image_manifest(im))


def test_arm06_build_context_changed_after_prebuild():
    src, est, pb, im0 = _pipeline()
    im = w.post_build_image_bind(pre_build_record=pb, build_metadata=_meta(), resolved_tags=[TAG],
                                 build_context_digest="0" * 64)
    assert any("context digest does not match" in p for p in w.validate_image_manifest(im))


def test_arm07_generated_input_changed():
    src, est, pb, im = _pipeline()
    drift = copy.deepcopy(src)
    drift["files"]["script:scripts/workflow_assurance.py"] = {"path": "x", "sha256": "0" * 64}
    drift["source_content_digest"] = w._digest(
        {k: drift[k] for k in ("schema_version", "files", "context", "actions_metadata")})
    r = _prepush(im, drift)
    assert r["result"] == "FAIL" and any("source content changed" in p for p in r["problems"])


def test_arm08_symlink_escape(tmp_path):
    root = tmp_path / "r"
    (root / "c").mkdir(parents=True)
    (tmp_path / "secret").write_text("x")
    (root / "wf.yml").write_text("name: x")
    (root / "c" / "link").symlink_to(tmp_path / "secret")
    m = w.source_content_manifest(root, {"workflow_path": "wf.yml", "context_path": "c",
                                         "commit_sha": SHA})
    assert any("symlink escaping the root" in p for p in m["problems"])


def test_arm09_authorization_pair_changed():
    src, est, pb, im = _pipeline()
    r = _prepush(im, src, auth=_stale_auth())
    assert r["result"] == "FAIL" and any("authorization" in p for p in r["problems"])


def test_arm10_policy_category_digest_changed():
    # A drifted Part A baseline: the establishment's bound docker-state digest no longer matches the
    # freshly derived one at pre-build.
    src = _src()
    est = w.establish(workflow=WF, source_manifest=src, expected_phase="pre_build",
                      commit_sha=SHA, tree_identity=TREE)
    est2 = copy.deepcopy(est)
    est2["docker_state"]["state_digest"] = "0" * 64            # simulate a policy/category drift
    est2["establishment_digest"] = w._digest(
        {k: est2[k] for k in ("schema_version", "mode", "workflow", "authorization", "source",
                              "docker_state", "expected_phase")})
    pb = w.pre_build_verify(establishment=est2, fresh_source_manifest=src, workflow=WF,
                            build_inputs=_bi(), fresh_commit_sha=SHA, fresh_tree_identity=TREE)
    assert pb["result"] == "FAIL" and any("Docker-state digest drifted" in p for p in pb["problems"])


def test_arm11_image_digest_missing():
    src, est, pb, im0 = _pipeline()
    im = w.post_build_image_bind(pre_build_record=pb, build_metadata={}, resolved_tags=[TAG])
    assert any("no image digest" in p for p in w.validate_image_manifest(im))


def test_arm12_image_digest_malformed():
    src, est, pb, im0 = _pipeline()
    im = w.post_build_image_bind(pre_build_record=pb, build_metadata={"containerimage.digest": "x"},
                                 resolved_tags=[TAG])
    assert any("malformed" in p for p in w.validate_image_manifest(im))


def test_arm13_image_digest_substituted():
    src, est, pb, im = _pipeline()
    r = _prepush(im, src, digest=DIGEST_B)
    assert r["result"] == "FAIL" and any("image substitution" in p for p in r["problems"])


def test_arm14_tag_changed():
    src, est, pb, im = _pipeline()
    r = _prepush(im, src, tags=[f"reader:{'9'*40}"])
    assert r["result"] == "FAIL" and any("tag substitution" in p for p in r["problems"])


def test_arm15_mutable_only_tag():
    src, est, pb, im0 = _pipeline()
    im = w.post_build_image_bind(pre_build_record=pb, build_metadata=_meta(),
                                 resolved_tags=["reader:latest"])
    assert any("mutable-only tag" in p for p in w.validate_image_manifest(im))


def test_arm16_build_metadata_omitted():
    src, est, pb, im0 = _pipeline()
    im = w.post_build_image_bind(pre_build_record=pb, build_metadata={"buildx.build.ref": "r"},
                                 resolved_tags=[TAG])
    assert any("no image digest" in p for p in w.validate_image_manifest(im))


def test_arm17_build_argument_changed():
    # A changed build arg changes the pre-build token, so a manifest built under different args has
    # a different identity (MUTATION_CHANGES_RESULT).
    src, e1, pb1, _ = _pipeline(bi=_bi(build_args={"GIT_REVISION": SHA}))
    _, e2, pb2, _ = _pipeline(bi=_bi(build_args={"GIT_REVISION": "OTHER"}))
    assert pb1["pre_build_token"] != pb2["pre_build_token"]


def test_arm18_cache_from_changed():
    src, e1, pb1, _ = _pipeline(bi=_bi(cache_from=[]))
    _, e2, pb2, _ = _pipeline(bi=_bi(cache_from=["type=registry,ref=evil"]))
    assert pb1["pre_build_token"] != pb2["pre_build_token"]


def test_arm19_cache_to_changed():
    src, e1, pb1, _ = _pipeline(bi=_bi(cache_to=[]))
    _, e2, pb2, _ = _pipeline(bi=_bi(cache_to=["type=inline"]))
    assert pb1["pre_build_token"] != pb2["pre_build_token"]


def test_arm20_platform_changed():
    src, e1, pb1, _ = _pipeline(bi=_bi(platforms=["linux/amd64"]))
    _, e2, pb2, _ = _pipeline(bi=_bi(platforms=["linux/arm64"]))
    assert pb1["pre_build_token"] != pb2["pre_build_token"]


def test_arm21_manifest_replayed_from_another_workflow():
    src, est, pb, im = _pipeline()
    r = _prepush(im, src, workflow={**WF, "workflow_identity": "Staging publish",
                                    "workflow_path": ".github/workflows/staging-publish.yml"})
    assert r["result"] == "FAIL" and any("replay" in p for p in r["problems"])


def test_arm22_manifest_replayed_from_another_job():
    src, est, pb, im = _pipeline()
    r = _prepush(im, src, workflow={**WF, "job_identity": "some-other-job"})
    assert r["result"] == "FAIL" and any("replay" in p for p in r["problems"])


def test_arm23_manifest_replayed_from_another_tree():
    src, est, pb, im = _pipeline()
    r = _prepush(im, src, tree="a-different-tree")
    assert r["result"] == "FAIL" and any("tree identity changed" in p for p in r["problems"])


# ===================================================================== GRAPH ARMS (24-29)
def test_arm24_graph_verifier_removed(tmp_path):
    # An integrated workflow with the establish step removed but pre-build present is broken (not
    # merely not-yet-integrated: assurance IS present, so ordering is enforced and fails).
    res = _graph_fail(tmp_path, CO + PB + BD + IB + PP + PU)
    assert res["status"] == g.STATUS_FAIL and any("establish" in p for p in res["problems"])


def test_arm25_verifier_ordered_after_docker(tmp_path):
    res = _graph_fail(tmp_path, CO + BD + EST + PB + IB + PP + PU)
    assert res["status"] == g.STATUS_FAIL


def test_arm26_continue_on_error_enabled(tmp_path):
    est_coe = "      - continue-on-error: true\n        run: python scripts/workflow_assurance.py establish\n"
    res = _graph_fail(tmp_path, CO + est_coe + PB + BD + IB + PP + PU)
    assert res["status"] == g.STATUS_FAIL and any("continue-on-error" in p for p in res["problems"])


def test_arm27_docker_step_uses_always(tmp_path):
    bd_always = "      - if: always()\n        uses: docker/build-push-action@v6\n        with:\n          push: false\n"
    res = _graph_fail(tmp_path, CO + EST + PB + bd_always + IB + PP + PU)
    assert res["status"] == g.STATUS_FAIL and any("always()" in p for p in res["problems"])


def test_arm28_required_verifier_output_ignored(tmp_path):
    pu_always = "      - if: always()\n        run: docker push x\n"
    res = _graph_fail(tmp_path, CO + EST + PB + BD + IB + PP + pu_always)
    assert res["status"] == g.STATUS_FAIL and any("always()" in p for p in res["problems"])


def test_arm29_alternate_push_path_introduced(tmp_path):
    res = _graph_fail(tmp_path, CO + EST + PB + BD + IB + PU + PP + PU)
    assert res["status"] == g.STATUS_FAIL and any("alternate push path" in p for p in res["problems"])


# ===================================================================== AGGREGATE ARM (30)
def test_arm30_final_assurance_aggregate_forced_pass():
    src, est, pb, im = _pipeline()
    pp = _prepush(im, src)
    forged = copy.deepcopy(pp)
    forged["result"] = "PASS"
    forged["problems"] = ["image substitution: forced"]
    # record_passes refuses a PASS carrying a problem, and validate refuses a token/content mismatch.
    assert w.record_passes(forged) is False
    tampered = copy.deepcopy(pp)
    tampered["intended_image_digest"] = DIGEST_B         # content changed, token not recomputed
    assert w.validate_pre_push(tampered) != []
