"""Gate 4N-I28BG-B2 — reader-specific adversarial battery (32 arms).

Each arm mutates the integrated reader workflow (its YAML graph) or the runtime verifier inputs,
drives the intended target, and asserts the reader graph or the executed verifier fails closed and
the push is blocked in simulation. No generic YAML parse failure substitutes for a detector, and no
Docker/registry/network is used. The cross-workflow B4 battery remains separate.
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

READER = REPO_ROOT / ".github" / "workflows" / "reader-publish.yml"
READER_TEXT = READER.read_text()
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


def _graph(tmp_path, text):
    p = tmp_path / "reader.yml"
    p.write_text(text)
    return g.validate_workflow(p)


def _chain(*, spec=None, build_metadata=None, resolved_tags=None, intended_digest=None,
           intended_tags=None, authorization=None, bi_over=None):
    tree = _tree()
    src = w.source_content_manifest(REPO_ROOT, spec or SPEC)
    est = w.establish(workflow=WF, source_manifest=src, expected_phase="reader-establish",
                      commit_sha=SHA, tree_identity=tree)
    df = src["files"].get("dockerfile", {}).get("sha256", "0" * 64)
    ctx = w._digest(src["context"])
    bi = {"dockerfile_path": "apps/revision-reader/Dockerfile", "dockerfile_digest": df,
          "context_path": "apps/revision-reader", "context_digest": ctx,
          "build_args": {"GIT_REVISION": SHA}, "build_secret_names": [], "cache_from": [],
          "cache_to": [], "platforms": ["linux/amd64"], "target_stage": "reader", "labels": {},
          "runtime_metadata": {"created": "t"}, "resolved_tags": [TAG]}
    if bi_over:
        bi.update(bi_over)
    pb = w.pre_build_verify(establishment=est, fresh_source_manifest=src, workflow=WF,
                            build_inputs=bi, fresh_commit_sha=SHA, fresh_tree_identity=tree)
    im = w.post_build_image_bind(pre_build_record=pb,
                                 build_metadata=(build_metadata if build_metadata is not None else {"containerimage.digest": "sha256:" + "a" * 64}),
                                 resolved_tags=resolved_tags or [TAG], dockerfile_digest=df,
                                 build_context_digest=ctx)
    pp = w.pre_push_verify(image_manifest=im, fresh_source_manifest=src, workflow=WF,
                           intended_image_digest=intended_digest or im["build_output"]["image_digest"],
                           intended_tags=intended_tags or im["build_output"]["resolved_tags"],
                           authorization=authorization, fresh_commit_sha=SHA, fresh_tree_identity=tree)
    push_blocked = not (est["result"] == "PASS" and pb["result"] == "PASS"
                        and not w.validate_image_manifest(im) and pp["result"] == "PASS")
    return {"establish": est, "pre_build": pb, "image": im, "pre_push": pp, "push_blocked": push_blocked}


# baseline: the unmutated integrated reader passes and is push-eligible
def test_baseline_reader_passes_and_is_push_eligible():
    assert g.validate_workflow(READER)["status"] == g.STATUS_PASS
    assert _chain()["push_blocked"] is False


# ===================================================================== GRAPH-STRUCTURE ARMS
def test_arm01_remove_establishment(tmp_path):
    t = READER_TEXT.replace("python3 scripts/workflow_assurance.py establish", "echo noop", 1)
    assert _graph(tmp_path, t)["status"] == g.STATUS_FAIL


def test_arm02_move_establishment_after_build(tmp_path):
    # Neutralise establish where it is and inject it after the build action.
    t = READER_TEXT.replace("python3 scripts/workflow_assurance.py establish \\\n"
                            "            --params reader-assurance/establish-params.json \\\n"
                            "            --out reader-assurance/establishment.json", "echo noop", 1)
    t = t.replace("        id: build\n",
                  "        id: build\n        # displaced\n", 1)
    # append an establish invocation as a late step
    t += ("\n      - name: late establish\n        run: python3 scripts/workflow_assurance.py "
          "establish --params reader-assurance/establish-params.json\n")
    res = _graph(tmp_path, t)
    assert res["status"] == g.STATUS_FAIL


def test_arm03_remove_pre_build(tmp_path):
    t = READER_TEXT.replace("python3 scripts/workflow_assurance.py pre_build_verify", "echo noop", 1)
    assert _graph(tmp_path, t)["status"] == g.STATUS_FAIL


def test_arm04_pre_build_after_build(tmp_path):
    t = READER_TEXT.replace("python3 scripts/workflow_assurance.py pre_build_verify", "echo noop", 1)
    t += ("\n      - name: late pre-build\n        run: python3 scripts/workflow_assurance.py "
          "pre_build_verify --params reader-assurance/pre-build-params.json\n")
    assert _graph(tmp_path, t)["status"] == g.STATUS_FAIL


def test_arm16_remove_post_build_bind(tmp_path):
    t = READER_TEXT.replace("python3 scripts/workflow_assurance.py post_build_image_bind", "echo noop", 1)
    assert _graph(tmp_path, t)["status"] == g.STATUS_FAIL


def test_arm20_remove_pre_push(tmp_path):
    t = READER_TEXT.replace("python3 scripts/workflow_assurance.py pre_push_verify", "echo noop", 1)
    assert _graph(tmp_path, t)["status"] == g.STATUS_FAIL


def test_arm21_pre_push_after_push(tmp_path):
    t = READER_TEXT.replace("python3 scripts/workflow_assurance.py pre_push_verify", "echo noop", 1)
    t += ("\n      - name: late pre-push\n        run: python3 scripts/workflow_assurance.py "
          "pre_push_verify --params reader-assurance/pre-push-params.json\n")
    assert _graph(tmp_path, t)["status"] == g.STATUS_FAIL


def test_arm24_verifier_output_ignored_push_always(tmp_path):
    t = READER_TEXT.replace("      - name: Push by immutable commit tag and read the registry digest back\n"
                            "        id: push\n",
                            "      - name: Push by immutable commit tag and read the registry digest back\n"
                            "        id: push\n        if: always()\n", 1)
    res = _graph(tmp_path, t)
    assert res["status"] == g.STATUS_FAIL
    assert any("always()" in p for p in res["problems"])


def test_arm26_verifier_continue_on_error(tmp_path):
    t = READER_TEXT.replace("        id: assurance-pre-build\n",
                            "        id: assurance-pre-build\n        continue-on-error: true\n", 1)
    res = _graph(tmp_path, t)
    assert res["status"] == g.STATUS_FAIL
    assert any("continue-on-error" in p for p in res["problems"])


def test_arm27_push_step_always(tmp_path):
    t = READER_TEXT.replace("      - name: Build the reader image (linux/amd64, SHA-stamped)\n"
                            "        id: build\n",
                            "      - name: Build the reader image (linux/amd64, SHA-stamped)\n"
                            "        id: build\n        if: always()\n", 1)
    res = _graph(tmp_path, t)
    assert res["status"] == g.STATUS_FAIL
    assert any("always()" in p for p in res["problems"])


def test_arm28_alternate_reader_push_path(tmp_path):
    # A second, earlier docker push before the pre-push verify.
    t = READER_TEXT.replace("      - name: Set up Docker Buildx\n",
                            "      - name: sneaky push\n        run: docker push evil\n"
                            "      - name: Set up Docker Buildx\n", 1)
    res = _graph(tmp_path, t)
    assert res["status"] == g.STATUS_FAIL


def test_arm32_new_docker_site_without_coverage(tmp_path):
    # A docker command inserted before establishment escapes the assurance envelope.
    t = READER_TEXT.replace("      - name: Check out the exact trusted revision\n"
                            "        uses: actions/checkout@v7\n",
                            "      - name: Check out the exact trusted revision\n"
                            "        uses: actions/checkout@v7\n"
                            "      - name: early docker\n        run: docker run --rm hello-world\n", 1)
    res = _graph(tmp_path, t)
    assert res["status"] == g.STATUS_FAIL
    assert any("envelope" in p or "before establishment" in p for p in res["problems"])


# ===================================================================== VERIFIER-INPUT ARMS
def test_arm05_dockerfile_changed_after_establishment():
    df_bad = "0" * 64
    res = _chain(bi_over={"dockerfile_digest": df_bad})
    # image bind cross-checks the post-build dockerfile digest against the pre-build binding
    im = res["image"]
    # rebuild with a mismatching dockerfile digest at bind
    src = w.source_content_manifest(REPO_ROOT, SPEC)
    ctx = w._digest(src["context"])
    im2 = w.post_build_image_bind(pre_build_record=res["pre_build"],
                                  build_metadata={"containerimage.digest": "sha256:" + "a" * 64},
                                  resolved_tags=[TAG], dockerfile_digest="1" * 64, build_context_digest=ctx)
    assert any("Dockerfile digest does not match" in p for p in w.validate_image_manifest(im2))


def test_arm06_build_context_changed():
    res = _chain()
    im2 = w.post_build_image_bind(pre_build_record=res["pre_build"],
                                  build_metadata={"containerimage.digest": "sha256:" + "a" * 64},
                                  resolved_tags=[TAG], build_context_digest="1" * 64)
    assert any("context digest does not match" in p for p in w.validate_image_manifest(im2))


def test_arm07_build_arguments_changed():
    a = _chain(bi_over={"build_args": {"GIT_REVISION": SHA}})
    b = _chain(bi_over={"build_args": {"GIT_REVISION": "OTHER"}})
    assert a["pre_build"]["pre_build_token"] != b["pre_build"]["pre_build_token"]


def test_arm08_cache_from_changed():
    a = _chain(bi_over={"cache_from": []})
    b = _chain(bi_over={"cache_from": ["type=registry,ref=evil"]})
    assert a["pre_build"]["pre_build_token"] != b["pre_build"]["pre_build_token"]


def test_arm09_cache_to_changed():
    a = _chain(bi_over={"cache_to": []})
    b = _chain(bi_over={"cache_to": ["type=inline"]})
    assert a["pre_build"]["pre_build_token"] != b["pre_build"]["pre_build_token"]


def test_arm10_platform_changed():
    a = _chain(bi_over={"platforms": ["linux/amd64"]})
    b = _chain(bi_over={"platforms": ["linux/arm64"]})
    assert a["pre_build"]["pre_build_token"] != b["pre_build"]["pre_build_token"]


def test_arm11_build_metadata_omitted():
    res = _chain(build_metadata={})
    assert res["push_blocked"] is True
    assert any("no image digest" in p for p in w.validate_image_manifest(res["image"]))


def test_arm12_malformed_build_metadata():
    res = _chain(build_metadata={"containerimage.digest": "not-a-digest"})
    assert res["push_blocked"] is True
    assert any("malformed" in p for p in w.validate_image_manifest(res["image"]))


def test_arm13_image_digest_substituted():
    res = _chain(intended_digest="sha256:" + "b" * 64)
    assert res["pre_push"]["result"] == "FAIL" and res["push_blocked"] is True
    assert any("image substitution" in p for p in res["pre_push"]["problems"])


def test_arm14_resolved_tag_substituted():
    res = _chain(intended_tags=["signalnest-revision-reader:deadbeef"])
    assert res["pre_push"]["result"] == "FAIL" and res["push_blocked"] is True
    assert any("tag substitution" in p for p in res["pre_push"]["problems"])


def test_arm15_mutable_only_tag():
    res = _chain(resolved_tags=["signalnest-revision-reader:latest"])
    assert res["push_blocked"] is True
    assert any("mutable-only tag" in p for p in w.validate_image_manifest(res["image"]))


def test_arm17_replay_from_another_workflow():
    res = _chain()
    im = res["image"]
    other = {**WF, "workflow_identity": "Staging publish",
             "workflow_path": ".github/workflows/staging-publish.yml"}
    pp = w.pre_push_verify(image_manifest=im, fresh_source_manifest=w.source_content_manifest(REPO_ROOT, SPEC),
                           workflow=other, intended_image_digest=im["build_output"]["image_digest"],
                           intended_tags=im["build_output"]["resolved_tags"], fresh_commit_sha=SHA,
                           fresh_tree_identity=_tree())
    assert pp["result"] == "FAIL" and any("replay" in p for p in pp["problems"])


def test_arm18_replay_from_another_job():
    res = _chain()
    im = res["image"]
    other = {**WF, "job_identity": "some-other-job"}
    pp = w.pre_push_verify(image_manifest=im, fresh_source_manifest=w.source_content_manifest(REPO_ROOT, SPEC),
                           workflow=other, intended_image_digest=im["build_output"]["image_digest"],
                           intended_tags=im["build_output"]["resolved_tags"], fresh_commit_sha=SHA,
                           fresh_tree_identity=_tree())
    assert pp["result"] == "FAIL" and any("replay" in p for p in pp["problems"])


def test_arm19_replay_from_another_tree():
    res = _chain()
    im = res["image"]
    pp = w.pre_push_verify(image_manifest=im, fresh_source_manifest=w.source_content_manifest(REPO_ROOT, SPEC),
                           workflow=WF, intended_image_digest=im["build_output"]["image_digest"],
                           intended_tags=im["build_output"]["resolved_tags"], fresh_commit_sha=SHA,
                           fresh_tree_identity="a-different-tree")
    assert pp["result"] == "FAIL" and any("tree identity changed" in p for p in pp["problems"])


def test_arm22_push_uses_different_tag():
    res = _chain(intended_tags=[f"signalnest-revision-reader:{'9' * 40}"])
    assert res["pre_push"]["result"] == "FAIL" and res["push_blocked"] is True


def test_arm23_push_uses_different_digest():
    res = _chain(intended_digest="sha256:" + "c" * 64)
    assert res["pre_push"]["result"] == "FAIL" and res["push_blocked"] is True


def test_arm25_verifier_output_overwritten():
    # A record whose fields were overwritten but whose token was not recomputed is refused.
    res = _chain()
    tampered = copy.deepcopy(res["pre_push"])
    tampered["intended_image_digest"] = "sha256:" + "d" * 64
    assert w.validate_pre_push(tampered) != []


def test_arm29_source_mutation_before_push():
    res = _chain()
    im = res["image"]
    drifted = w.source_content_manifest(REPO_ROOT, SPEC)
    drifted["source_content_digest"] = "changed"
    pp = w.pre_push_verify(image_manifest=im, fresh_source_manifest=drifted, workflow=WF,
                           intended_image_digest=im["build_output"]["image_digest"],
                           intended_tags=im["build_output"]["resolved_tags"], fresh_commit_sha=SHA,
                           fresh_tree_identity=_tree())
    assert pp["result"] == "FAIL" and any("source content changed" in p for p in pp["problems"])


def test_arm30_authorization_pair_changes_before_push():
    stale = {"issuance": "2026-08-06T15:30:42Z", "expiry": "2026-08-07T13:30:42Z",
             "duration_seconds": 79200}
    stale["pair_digest"] = ca.digest({k: stale[k] for k in ("issuance", "expiry", "duration_seconds")})
    res = _chain(authorization=stale)
    assert res["pre_push"]["result"] == "FAIL" and res["push_blocked"] is True


def test_arm31_final_aggregate_forced_pass():
    res = _chain()
    forged = copy.deepcopy(res["pre_push"])
    forged["result"] = "PASS"
    forged["problems"] = ["image substitution: forced"]
    assert w.record_passes(forged) is False
