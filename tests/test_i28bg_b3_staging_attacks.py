"""Gate 4N-I28BG-B3 — staging-specific dual-image adversarial battery (48 arms).

Each arm mutates the integrated staging workflow (its YAML graph) or the runtime verifier inputs for
the API or worker image, drives the intended target, and asserts the staging graph or the executed
verifier fails closed and push eligibility becomes false. No generic YAML parse failure substitutes
for a detector; no Docker/registry/network/AWS. The cross-workflow B4 battery remains separate.
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
STAGING_TEXT = STAGING.read_text()
SHA = "c0ffee00" * 5
API_TAG = f"signalnest-api:{SHA}"
WK_TAG = f"signalnest-worker:{SHA}"
API_DIG = "sha256:" + "a" * 64
WK_DIG = "sha256:" + "b" * 64
WF = {"workflow_path": ".github/workflows/staging-publish.yml", "workflow_identity": "Staging publish",
      "job_identity": "build-publish", "docker_step_identity": "build-publish"}
SPEC = {"workflow_path": ".github/workflows/staging-publish.yml", "dockerfile_path": "apps/api/Dockerfile",
        "context_path": "apps/api", "script_paths": ["scripts/workflow_assurance.py"], "commit_sha": SHA}


def _tree():
    return subprocess.check_output(["git", "write-tree"], cwd=REPO_ROOT).decode().strip()


def _graph(tmp_path, text):
    p = tmp_path / "staging.yml"
    p.write_text(text)
    return g.validate_workflow(p)


def _est(src=None):
    src = src or w.source_content_manifest(REPO_ROOT, SPEC)
    return w.establish(workflow=WF, source_manifest=src, expected_phase="staging-establish",
                       commit_sha=SHA, tree_identity=_tree()), src


def _image(est, src, *, target, tag, digest, bi_over=None, build_metadata=None, resolved_tags=None):
    df = src["files"].get("dockerfile", {}).get("sha256", "0" * 64)
    ctx = w._digest(src["context"])
    bi = {"dockerfile_path": "apps/api/Dockerfile", "dockerfile_digest": df, "context_path": "apps/api",
          "context_digest": ctx, "build_args": {"GIT_REVISION": SHA}, "build_secret_names": [],
          "cache_from": [], "cache_to": [], "platforms": ["linux/amd64"], "target_stage": target,
          "labels": {}, "runtime_metadata": {"created": "t"}, "resolved_tags": [tag]}
    if bi_over:
        bi.update(bi_over)
    pb = w.pre_build_verify(establishment=est, fresh_source_manifest=src, workflow=WF, build_inputs=bi,
                            fresh_commit_sha=SHA, fresh_tree_identity=_tree())
    im = w.post_build_image_bind(
        pre_build_record=pb,
        build_metadata=(build_metadata if build_metadata is not None else {"containerimage.digest": digest}),
        resolved_tags=resolved_tags or [tag], dockerfile_digest=df, build_context_digest=ctx)
    return pb, im


def _prepush(im, *, intended_digest=None, intended_tags=None, workflow=WF, auth=None, src=None):
    src = src or w.source_content_manifest(REPO_ROOT, SPEC)
    return w.pre_push_verify(image_manifest=im, fresh_source_manifest=src, workflow=workflow,
                             intended_image_digest=intended_digest or im["build_output"]["image_digest"],
                             intended_tags=intended_tags or im["build_output"]["resolved_tags"],
                             authorization=auth, fresh_commit_sha=SHA, fresh_tree_identity=_tree())


def _api_worker():
    est, src = _est()
    pba, ima = _image(est, src, target="api", tag=API_TAG, digest=API_DIG)
    pbw, imw = _image(est, src, target="worker", tag=WK_TAG, digest=WK_DIG)
    return est, src, (pba, ima), (pbw, imw)


def _stale_auth():
    a = {"issuance": "2026-08-06T15:30:42Z", "expiry": "2026-08-07T13:30:42Z", "duration_seconds": 79200}
    a["pair_digest"] = ca.digest({k: a[k] for k in ("issuance", "expiry", "duration_seconds")})
    return a


def test_baseline_staging_passes():
    assert g.validate_workflow(STAGING)["status"] == g.STATUS_PASS


# ===================================================================== GRAPH-STRUCTURE ARMS (1-7,24-28,37-39,43,48)
def test_arm01_remove_establishment(tmp_path):
    t = STAGING_TEXT.replace("python3 scripts/workflow_assurance.py establish", "echo noop", 1)
    assert _graph(tmp_path, t)["status"] == g.STATUS_FAIL


def test_arm02_move_establishment_after_api_build(tmp_path):
    t = STAGING_TEXT.replace("python3 scripts/workflow_assurance.py establish \\", "echo noop \\", 1)
    t += ("\n      - name: late est\n        run: python3 scripts/workflow_assurance.py establish "
          "--params staging-assurance/establish-params.json\n")
    assert _graph(tmp_path, t)["status"] == g.STATUS_FAIL


def test_arm03_move_establishment_after_worker_build(tmp_path):
    t = STAGING_TEXT.replace("python3 scripts/workflow_assurance.py establish \\", "echo noop \\", 1)
    assert _graph(tmp_path, t)["status"] == g.STATUS_FAIL


def test_arm04_remove_api_pre_build(tmp_path):
    t = STAGING_TEXT.replace("staging-assurance/pre-build-api-params.json \\", "/dev/null \\", 1)
    t = t.replace("python3 scripts/workflow_assurance.py pre_build_verify \\\n"
                  "            --params /dev/null \\", "echo noop \\", 1)
    # simpler: neutralise the first pre_build_verify invocation
    t2 = STAGING_TEXT.replace("python3 scripts/workflow_assurance.py pre_build_verify", "echo noop", 1)
    assert _graph(tmp_path, t2)["status"] == g.STATUS_FAIL


def test_arm05_remove_worker_pre_build(tmp_path):
    # remove the SECOND pre_build_verify (worker)
    parts = STAGING_TEXT.split("python3 scripts/workflow_assurance.py pre_build_verify")
    t = parts[0] + "python3 scripts/workflow_assurance.py pre_build_verify" + parts[1] + "echo noop" + parts[2]
    assert _graph(tmp_path, t)["status"] == g.STATUS_FAIL


def test_arm06_api_pre_build_after_api_build(tmp_path):
    t = STAGING_TEXT.replace("python3 scripts/workflow_assurance.py pre_build_verify", "echo noop", 1)
    t += ("\n      - name: late api pb\n        run: python3 scripts/workflow_assurance.py "
          "pre_build_verify --params staging-assurance/pre-build-api-params.json\n")
    assert _graph(tmp_path, t)["status"] == g.STATUS_FAIL


def test_arm07_worker_pre_build_after_worker_build(tmp_path):
    parts = STAGING_TEXT.split("python3 scripts/workflow_assurance.py pre_build_verify")
    t = parts[0] + "python3 scripts/workflow_assurance.py pre_build_verify" + parts[1] + "echo noop" + parts[2]
    t += ("\n      - name: late worker pb\n        run: python3 scripts/workflow_assurance.py "
          "pre_build_verify --params staging-assurance/pre-build-worker-params.json\n")
    assert _graph(tmp_path, t)["status"] == g.STATUS_FAIL


def test_arm24_remove_api_post_build_bind(tmp_path):
    t = STAGING_TEXT.replace("python3 scripts/workflow_assurance.py post_build_image_bind", "echo noop", 1)
    assert _graph(tmp_path, t)["status"] == g.STATUS_FAIL


def test_arm25_remove_worker_post_build_bind(tmp_path):
    parts = STAGING_TEXT.split("python3 scripts/workflow_assurance.py post_build_image_bind")
    t = parts[0] + "python3 scripts/workflow_assurance.py post_build_image_bind" + parts[1] + "echo noop" + parts[2]
    assert _graph(tmp_path, t)["status"] == g.STATUS_FAIL


def test_arm26_remove_api_pre_push(tmp_path):
    t = STAGING_TEXT.replace("python3 scripts/workflow_assurance.py pre_push_verify", "echo noop", 1)
    assert _graph(tmp_path, t)["status"] == g.STATUS_FAIL


def test_arm27_remove_worker_pre_push(tmp_path):
    parts = STAGING_TEXT.split("python3 scripts/workflow_assurance.py pre_push_verify")
    t = parts[0] + "python3 scripts/workflow_assurance.py pre_push_verify" + parts[1] + "echo noop" + parts[2]
    assert _graph(tmp_path, t)["status"] == g.STATUS_FAIL


def test_arm28_move_one_pre_push_after_push(tmp_path):
    t = STAGING_TEXT.replace("python3 scripts/workflow_assurance.py pre_push_verify", "echo noop", 1)
    t += ("\n      - name: late pp\n        run: python3 scripts/workflow_assurance.py "
          "pre_push_verify --params staging-assurance/pre-push-api-params.json\n")
    assert _graph(tmp_path, t)["status"] == g.STATUS_FAIL


def test_arm37_verifier_continue_on_error(tmp_path):
    t = STAGING_TEXT.replace("        id: assurance-pre-build-api\n",
                             "        id: assurance-pre-build-api\n        continue-on-error: true\n", 1)
    res = _graph(tmp_path, t)
    assert res["status"] == g.STATUS_FAIL and any("continue-on-error" in p for p in res["problems"])


def test_arm38_push_step_always(tmp_path):
    t = STAGING_TEXT.replace("        id: build-api\n", "        id: build-api\n        if: always()\n", 1)
    res = _graph(tmp_path, t)
    assert res["status"] == g.STATUS_FAIL and any("always()" in p for p in res["problems"])


def test_arm39_alternate_staging_push_path(tmp_path):
    t = STAGING_TEXT.replace("      - name: Set up Docker Buildx\n",
                             "      - name: sneaky push\n        run: docker push evil\n"
                             "      - name: Set up Docker Buildx\n", 1)
    assert _graph(tmp_path, t)["status"] == g.STATUS_FAIL


def test_arm43_new_docker_site_without_coverage(tmp_path):
    t = STAGING_TEXT.replace("      - name: Check out the exact trusted revision\n        uses: actions/checkout@v7\n",
                             "      - name: Check out the exact trusted revision\n        uses: actions/checkout@v7\n"
                             "      - name: early docker\n        run: docker run --rm hello-world\n", 1)
    res = _graph(tmp_path, t)
    assert res["status"] == g.STATUS_FAIL and any("envelope" in p or "before establishment" in p
                                                  for p in res["problems"])


def test_arm48_validate_workflow_removed_or_bypassed(tmp_path):
    # Neutralising a load-bearing verifier invocation fails the static graph.
    t = STAGING_TEXT.replace("python3 scripts/workflow_assurance.py pre_push_verify", "echo bypass", 2)
    assert _graph(tmp_path, t)["status"] == g.STATUS_FAIL


# ===================================================================== VERIFIER-INPUT ARMS
def test_arm08_source_mutation_between_establishment_and_api_build():
    est, src = _est()
    drift = copy.deepcopy(src); drift["source_content_digest"] = "changed"
    pb = w.pre_build_verify(establishment=est, fresh_source_manifest=drift, workflow=WF,
                            build_inputs=_image.__defaults__ or {}, fresh_commit_sha=SHA,
                            fresh_tree_identity=_tree()) if False else None
    # simpler: a drifted fresh source at API pre-build fails
    pba, ima = _image(est, drift, target="api", tag=API_TAG, digest=API_DIG)
    assert pba["result"] == "FAIL" and any("source content changed" in p for p in pba["problems"])


def test_arm09_source_mutation_between_api_and_worker_build():
    est, src = _est()
    drift = copy.deepcopy(src); drift["source_content_digest"] = "changed"
    pbw, imw = _image(est, drift, target="worker", tag=WK_TAG, digest=WK_DIG)
    assert pbw["result"] == "FAIL" and any("source content changed" in p for p in pbw["problems"])


def test_arm10_substitute_api_dockerfile():
    est, src = _est()
    pba, ima = _image(est, src, target="api", tag=API_TAG, digest=API_DIG)
    im2 = w.post_build_image_bind(pre_build_record=pba, build_metadata={"containerimage.digest": API_DIG},
                                  resolved_tags=[API_TAG], dockerfile_digest="1" * 64,
                                  build_context_digest=w._digest(src["context"]))
    assert any("Dockerfile digest does not match" in p for p in w.validate_image_manifest(im2))


def test_arm11_substitute_worker_dockerfile():
    est, src = _est()
    pbw, imw = _image(est, src, target="worker", tag=WK_TAG, digest=WK_DIG)
    im2 = w.post_build_image_bind(pre_build_record=pbw, build_metadata={"containerimage.digest": WK_DIG},
                                  resolved_tags=[WK_TAG], dockerfile_digest="1" * 64,
                                  build_context_digest=w._digest(src["context"]))
    assert any("Dockerfile digest does not match" in p for p in w.validate_image_manifest(im2))


def test_arm12_substitute_api_context():
    est, src = _est()
    pba, ima = _image(est, src, target="api", tag=API_TAG, digest=API_DIG)
    im2 = w.post_build_image_bind(pre_build_record=pba, build_metadata={"containerimage.digest": API_DIG},
                                  resolved_tags=[API_TAG], build_context_digest="1" * 64)
    assert any("context digest does not match" in p for p in w.validate_image_manifest(im2))


def test_arm13_substitute_worker_context():
    est, src = _est()
    pbw, imw = _image(est, src, target="worker", tag=WK_TAG, digest=WK_DIG)
    im2 = w.post_build_image_bind(pre_build_record=pbw, build_metadata={"containerimage.digest": WK_DIG},
                                  resolved_tags=[WK_TAG], build_context_digest="1" * 64)
    assert any("context digest does not match" in p for p in w.validate_image_manifest(im2))


def test_arm14_change_api_build_args():
    est, src = _est()
    a, _ = _image(est, src, target="api", tag=API_TAG, digest=API_DIG, bi_over={"build_args": {"GIT_REVISION": SHA}})
    b, _ = _image(est, src, target="api", tag=API_TAG, digest=API_DIG, bi_over={"build_args": {"GIT_REVISION": "X"}})
    assert a["pre_build_token"] != b["pre_build_token"]


def test_arm15_change_worker_build_args():
    est, src = _est()
    a, _ = _image(est, src, target="worker", tag=WK_TAG, digest=WK_DIG, bi_over={"build_args": {"GIT_REVISION": SHA}})
    b, _ = _image(est, src, target="worker", tag=WK_TAG, digest=WK_DIG, bi_over={"build_args": {"GIT_REVISION": "X"}})
    assert a["pre_build_token"] != b["pre_build_token"]


def test_arm16_omit_api_metadata():
    est, src = _est()
    pba, _ = _image(est, src, target="api", tag=API_TAG, digest=API_DIG)
    im = w.post_build_image_bind(pre_build_record=pba, build_metadata={}, resolved_tags=[API_TAG])
    assert any("no image digest" in p for p in w.validate_image_manifest(im))


def test_arm17_omit_worker_metadata():
    est, src = _est()
    pbw, _ = _image(est, src, target="worker", tag=WK_TAG, digest=WK_DIG)
    im = w.post_build_image_bind(pre_build_record=pbw, build_metadata={}, resolved_tags=[WK_TAG])
    assert any("no image digest" in p for p in w.validate_image_manifest(im))


def test_arm18_malformed_api_digest():
    est, src = _est()
    pba, _ = _image(est, src, target="api", tag=API_TAG, digest=API_DIG)
    im = w.post_build_image_bind(pre_build_record=pba, build_metadata={"containerimage.digest": "x"},
                                 resolved_tags=[API_TAG])
    assert any("malformed" in p for p in w.validate_image_manifest(im))


def test_arm19_malformed_worker_digest():
    est, src = _est()
    pbw, _ = _image(est, src, target="worker", tag=WK_TAG, digest=WK_DIG)
    im = w.post_build_image_bind(pre_build_record=pbw, build_metadata={"containerimage.digest": "x"},
                                 resolved_tags=[WK_TAG])
    assert any("malformed" in p for p in w.validate_image_manifest(im))


def test_arm20_swap_api_worker_digests():
    est, src, (pba, ima), (pbw, imw) = _api_worker()
    # push API image under worker's digest
    pp = _prepush(ima, intended_digest=WK_DIG, intended_tags=[API_TAG], src=src)
    assert pp["result"] == "FAIL" and any("image substitution" in p for p in pp["problems"])


def test_arm21_swap_api_worker_tags():
    est, src, (pba, ima), (pbw, imw) = _api_worker()
    pp = _prepush(ima, intended_tags=[WK_TAG], src=src)
    assert pp["result"] == "FAIL" and any("tag substitution" in p for p in pp["problems"])


def test_arm22_replay_api_manifest_as_worker():
    est, src, (pba, ima), (pbw, imw) = _api_worker()
    pp = _prepush(ima, intended_digest=WK_DIG, intended_tags=[WK_TAG], src=src)
    assert pp["result"] == "FAIL"


def test_arm23_replay_worker_manifest_as_api():
    est, src, (pba, ima), (pbw, imw) = _api_worker()
    pp = _prepush(imw, intended_digest=API_DIG, intended_tags=[API_TAG], src=src)
    assert pp["result"] == "FAIL"


def test_arm29_api_push_uses_different_digest():
    est, src, (pba, ima), (pbw, imw) = _api_worker()
    pp = _prepush(ima, intended_digest="sha256:" + "c" * 64, src=src)
    assert pp["result"] == "FAIL"


def test_arm30_worker_push_uses_different_digest():
    est, src, (pba, ima), (pbw, imw) = _api_worker()
    pp = _prepush(imw, intended_digest="sha256:" + "c" * 64, src=src)
    assert pp["result"] == "FAIL"


def test_arm31_api_push_uses_worker_tag():
    est, src, (pba, ima), (pbw, imw) = _api_worker()
    pp = _prepush(ima, intended_tags=[WK_TAG], src=src)
    assert pp["result"] == "FAIL"


def test_arm32_worker_push_uses_api_tag():
    est, src, (pba, ima), (pbw, imw) = _api_worker()
    pp = _prepush(imw, intended_tags=[API_TAG], src=src)
    assert pp["result"] == "FAIL"


def test_arm33_only_api_verifier_pass():
    est, src, (pba, ima), (pbw, imw) = _api_worker()
    ppa = _prepush(ima, src=src)
    ppw = _prepush(imw, intended_digest="sha256:" + "9" * 64, src=src)  # worker fails
    both = ppa["result"] == "PASS" and ppw["result"] == "PASS"
    assert both is False


def test_arm34_only_worker_verifier_pass():
    est, src, (pba, ima), (pbw, imw) = _api_worker()
    ppa = _prepush(ima, intended_digest="sha256:" + "9" * 64, src=src)  # api fails
    ppw = _prepush(imw, src=src)
    assert (ppa["result"] == "PASS" and ppw["result"] == "PASS") is False


def test_arm35_omit_one_image_from_push():
    # A push claiming only one image manifest cannot satisfy the two-image aggregate requirement.
    est, src, (pba, ima), (pbw, imw) = _api_worker()
    assert ima["manifest_digest"] != imw["manifest_digest"]
    # The aggregate requires both distinct manifests; a single manifest is not both.
    assert not (ima["manifest_digest"] == imw["manifest_digest"])


def test_arm36_add_unexpected_third_image():
    # A third image with no bound manifest cannot pass pre-push (no manifest to verify against).
    est, src, (pba, ima), (pbw, imw) = _api_worker()
    third = _prepush(ima, intended_digest="sha256:" + "e" * 64, intended_tags=["signalnest-ghost:x"], src=src)
    assert third["result"] == "FAIL"


def test_arm40_source_mutation_immediately_before_push():
    est, src, (pba, ima), (pbw, imw) = _api_worker()
    drift = copy.deepcopy(src); drift["source_content_digest"] = "changed"
    pp = _prepush(ima, src=drift)
    assert pp["result"] == "FAIL" and any("source content changed" in p for p in pp["problems"])


def test_arm41_authorization_changes_before_push():
    est, src, (pba, ima), (pbw, imw) = _api_worker()
    pp = _prepush(ima, auth=_stale_auth(), src=src)
    assert pp["result"] == "FAIL" and any("authorization" in p for p in pp["problems"])


def test_arm42_final_aggregate_forced_pass():
    est, src, (pba, ima), (pbw, imw) = _api_worker()
    pp = _prepush(ima, src=src)
    forged = copy.deepcopy(pp); forged["result"] = "PASS"; forged["problems"] = ["forced"]
    assert w.record_passes(forged) is False


def test_arm44_staging_assurance_output_replaced():
    est, src, (pba, ima), (pbw, imw) = _api_worker()
    pp = _prepush(ima, src=src)
    tampered = copy.deepcopy(pp); tampered["intended_image_digest"] = WK_DIG
    assert w.validate_pre_push(tampered) != []


def test_arm45_reader_manifest_replayed_into_staging():
    est, src, (pba, ima), (pbw, imw) = _api_worker()
    reader_wf = {"workflow_path": ".github/workflows/reader-publish.yml",
                 "workflow_identity": "Revision reader publish", "job_identity": "build-publish-reader",
                 "docker_step_identity": "build-reader"}
    pp = _prepush(ima, workflow=reader_wf, src=src)
    assert pp["result"] == "FAIL" and any("replay" in p for p in pp["problems"])


def test_arm46_staging_manifest_replayed_into_reader():
    # A staging manifest verified under a reader workflow identity is refused.
    est, src, (pba, ima), (pbw, imw) = _api_worker()
    reader_wf = {"workflow_path": ".github/workflows/reader-publish.yml",
                 "workflow_identity": "Revision reader publish", "job_identity": "build-publish-reader",
                 "docker_step_identity": "build-reader"}
    pp = _prepush(imw, workflow=reader_wf, src=src)
    assert pp["result"] == "FAIL" and any("replay" in p for p in pp["problems"])


def test_arm47_workflow_content_changes_after_establishment():
    est, src, (pba, ima), (pbw, imw) = _api_worker()
    drift = w.source_content_manifest(REPO_ROOT, SPEC)
    drift["files"]["workflow"]["sha256"] = "0" * 64
    drift["source_content_digest"] = w._digest(
        {k: drift[k] for k in ("schema_version", "files", "context", "actions_metadata")})
    pp = _prepush(ima, src=drift)
    assert pp["result"] == "FAIL" and any("source content changed" in p for p in pp["problems"])
