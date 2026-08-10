"""Gate 4N-I28BG-B1 — the reusable publish-workflow assurance verifier.

Unit and integration coverage for workflow_assurance: the four verifier modes, the source-content
manifest, the executed-image manifest, canonicalization/digest determinism, and the fail-closed
refusals (source drift, image/tag substitution, authorization change, manifest replay). Every test
runs offline — no Docker, no registry, no network.
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import workflow_assurance as w  # noqa: E402

DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
SHA = "c0ffee00" * 5           # a 40-hex commit sha
TREE = "tree-identity-0001"
TAG = f"signalnest-revision-reader:{SHA}"

WF = {
    "workflow_path": ".github/workflows/reader-publish.yml",
    "workflow_identity": "Revision reader publish",
    "job_identity": "build-publish-reader",
    "docker_step_identity": "build-reader",
}


def _source_spec():
    return {
        "workflow_path": ".github/workflows/reader-publish.yml",
        "dockerfile_path": "apps/revision-reader/Dockerfile",
        "context_path": None,
        "script_paths": ["scripts/workflow_assurance.py"],
        "commit_sha": SHA,
    }


def _source():
    return w.source_content_manifest(REPO_ROOT, _source_spec())


def _build_inputs():
    return {
        "dockerfile_path": "apps/revision-reader/Dockerfile", "dockerfile_digest": "d" * 64,
        "context_path": "apps/revision-reader", "context_digest": "e" * 64,
        "build_args": {"GIT_REVISION": SHA}, "build_secret_names": [], "cache_from": [],
        "cache_to": [], "platforms": ["linux/amd64"], "target_stage": "reader", "labels": {},
        "runtime_metadata": {"created": "2026-08-06T00:00:00Z"}, "resolved_tags": [TAG],
    }


def _metadata(digest=DIGEST_A):
    return {"containerimage.digest": digest, "buildx.build.ref": "builder/xyz",
            "containerimage.config.digest": "sha256:" + "f" * 64}


def _pipeline():
    """A valid establish -> pre_build -> image_bind -> pre_push chain over the real tree."""
    src = _source()
    est = w.establish(workflow=WF, source_manifest=src, expected_phase="pre_build",
                      commit_sha=SHA, tree_identity=TREE)
    pb = w.pre_build_verify(establishment=est, fresh_source_manifest=src, workflow=WF,
                            build_inputs=_build_inputs(), fresh_commit_sha=SHA,
                            fresh_tree_identity=TREE)
    im = w.post_build_image_bind(pre_build_record=pb, build_metadata=_metadata(),
                                 resolved_tags=[TAG])
    pp = w.pre_push_verify(image_manifest=im, fresh_source_manifest=src, workflow=WF,
                           intended_image_digest=DIGEST_A, intended_tags=[TAG],
                           fresh_commit_sha=SHA, fresh_tree_identity=TREE)
    return src, est, pb, im, pp


# ===================================================================== the happy path
def test_the_full_four_phase_pipeline_passes():
    src, est, pb, im, pp = _pipeline()
    assert est["result"] == "PASS"
    assert pb["result"] == "PASS"
    assert im["_problems"] == []
    assert pp["result"] == "PASS"
    assert w.record_passes(pp) is True


def test_each_record_validates_against_its_own_schema():
    src, est, pb, im, pp = _pipeline()
    assert w.validate_establishment(est) == []
    assert w.validate_pre_build(pb) == []
    assert w.validate_image_manifest(im) == []
    assert w.validate_pre_push(pp) == []


# ===================================================================== source-content manifest
def test_source_manifest_binds_content_not_commit_sha():
    src = _source()
    assert w.validate_source_manifest(src) == []
    # The digest is over CONTENT; changing only the recorded commit sha does not move it.
    same = copy.deepcopy(src)
    same["commit_sha"] = "different"
    assert w.source_content_digest(same) == w.source_content_digest(src)


def test_a_changed_source_file_moves_the_digest():
    src = _source()
    mutated = copy.deepcopy(src)
    # Simulate a post-checkout content mutation of a bound file.
    mutated["files"]["workflow"]["sha256"] = "0" * 64
    mutated["source_content_digest"] = w._digest(
        {k: mutated[k] for k in ("schema_version", "files", "context", "actions_metadata")})
    assert w.source_content_digest(mutated) != w.source_content_digest(src)


def test_a_missing_declared_source_file_is_refused():
    spec = _source_spec()
    spec["dockerfile_path"] = "apps/revision-reader/DOES-NOT-EXIST"
    src = w.source_content_manifest(REPO_ROOT, spec)
    assert any("missing or not a regular file" in p for p in w.validate_source_manifest(src))


def test_a_symlink_escaping_the_root_is_refused(tmp_path):
    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    (root / "wf.yml").write_text("name: x")
    escape = root / "sub" / "link.txt"
    escape.symlink_to(outside)
    spec = {"workflow_path": "wf.yml", "context_path": "sub", "commit_sha": SHA}
    src = w.source_content_manifest(root, spec)
    assert any("symlink escaping the root" in p for p in src["problems"])


# ===================================================================== canonicalization / digest
def test_semantically_equal_records_digest_identically():
    src = _source()
    est1 = w.establish(workflow=WF, source_manifest=src, expected_phase="pre_build",
                       commit_sha=SHA, tree_identity=TREE)
    est2 = w.establish(workflow=dict(reversed(list(WF.items()))), source_manifest=src,
                       expected_phase="pre_build", commit_sha=SHA, tree_identity=TREE)
    assert est1["establishment_digest"] == est2["establishment_digest"]


def test_a_semantic_change_moves_the_establishment_digest():
    src = _source()
    est = w.establish(workflow=WF, source_manifest=src, expected_phase="pre_build",
                      commit_sha=SHA, tree_identity=TREE)
    other = w.establish(workflow={**WF, "job_identity": "other-job"}, source_manifest=src,
                        expected_phase="pre_build", commit_sha=SHA, tree_identity=TREE)
    assert est["establishment_digest"] != other["establishment_digest"]


# ===================================================================== docker-state binding
def test_establish_binds_a_freshly_validated_docker_state_digest():
    src, est, pb, im, pp = _pipeline()
    ds = est["docker_state"]
    assert ds is not None and ds["load_bearing_count"] > 0
    import docker_assurance_state as das
    assert ds["state_digest"] == das.state_digest(das.fresh_state())


def test_a_static_state_digest_is_not_the_image_digest():
    src, est, pb, im, pp = _pipeline()
    # The Part A state digest and the built-image digest are distinct bindings; one never stands
    # in for the other.
    assert est["docker_state"]["state_digest"] != im["build_output"]["image_digest"]


# ===================================================================== fail-closed refusals
def test_source_drift_after_establishment_is_refused_pre_build():
    src = _source()
    est = w.establish(workflow=WF, source_manifest=src, expected_phase="pre_build",
                      commit_sha=SHA, tree_identity=TREE)
    drifted = copy.deepcopy(src)
    drifted["source_content_digest"] = "deadbeef"
    pb = w.pre_build_verify(establishment=est, fresh_source_manifest=drifted, workflow=WF,
                            build_inputs=_build_inputs(), fresh_commit_sha=SHA,
                            fresh_tree_identity=TREE)
    assert pb["result"] == "FAIL"
    assert any("source content changed" in p for p in pb["problems"])


def test_image_substitution_is_refused_pre_push():
    src, est, pb, im, pp = _pipeline()
    bad = w.pre_push_verify(image_manifest=im, fresh_source_manifest=src, workflow=WF,
                            intended_image_digest=DIGEST_B, intended_tags=[TAG],
                            fresh_commit_sha=SHA, fresh_tree_identity=TREE)
    assert bad["result"] == "FAIL"
    assert any("image substitution" in p for p in bad["problems"])


def test_tag_substitution_is_refused_pre_push():
    src, est, pb, im, pp = _pipeline()
    bad = w.pre_push_verify(image_manifest=im, fresh_source_manifest=src, workflow=WF,
                            intended_image_digest=DIGEST_A, intended_tags=["reader:sneaky"],
                            fresh_commit_sha=SHA, fresh_tree_identity=TREE)
    assert bad["result"] == "FAIL"
    assert any("tag substitution" in p for p in bad["problems"])


def test_a_mutable_only_tag_is_refused_at_image_bind():
    src = _source()
    est = w.establish(workflow=WF, source_manifest=src, expected_phase="pre_build",
                      commit_sha=SHA, tree_identity=TREE)
    pb = w.pre_build_verify(establishment=est, fresh_source_manifest=src, workflow=WF,
                            build_inputs={**_build_inputs(), "resolved_tags": [TAG]},
                            fresh_commit_sha=SHA, fresh_tree_identity=TREE)
    im = w.post_build_image_bind(pre_build_record=pb, build_metadata=_metadata(),
                                 resolved_tags=["signalnest-revision-reader:latest"])
    assert any("mutable-only tag" in p for p in w.validate_image_manifest(im))


def test_a_missing_image_digest_is_refused():
    src = _source()
    est = w.establish(workflow=WF, source_manifest=src, expected_phase="pre_build",
                      commit_sha=SHA, tree_identity=TREE)
    pb = w.pre_build_verify(establishment=est, fresh_source_manifest=src, workflow=WF,
                            build_inputs=_build_inputs(), fresh_commit_sha=SHA,
                            fresh_tree_identity=TREE)
    im = w.post_build_image_bind(pre_build_record=pb, build_metadata={}, resolved_tags=[TAG])
    assert any("no image digest" in p for p in w.validate_image_manifest(im))


def test_a_malformed_image_digest_is_refused():
    src = _source()
    est = w.establish(workflow=WF, source_manifest=src, expected_phase="pre_build",
                      commit_sha=SHA, tree_identity=TREE)
    pb = w.pre_build_verify(establishment=est, fresh_source_manifest=src, workflow=WF,
                            build_inputs=_build_inputs(), fresh_commit_sha=SHA,
                            fresh_tree_identity=TREE)
    im = w.post_build_image_bind(pre_build_record=pb, build_metadata={"containerimage.digest": "nope"},
                                 resolved_tags=[TAG])
    assert any("malformed" in p for p in w.validate_image_manifest(im))


def test_multiple_digests_without_permission_are_refused():
    src = _source()
    est = w.establish(workflow=WF, source_manifest=src, expected_phase="pre_build",
                      commit_sha=SHA, tree_identity=TREE)
    pb = w.pre_build_verify(establishment=est, fresh_source_manifest=src, workflow=WF,
                            build_inputs=_build_inputs(), fresh_commit_sha=SHA,
                            fresh_tree_identity=TREE)
    meta = {"image_digests": [DIGEST_A, DIGEST_B]}
    im = w.post_build_image_bind(pre_build_record=pb, build_metadata=meta, resolved_tags=[TAG])
    assert any("multiple digests" in p for p in w.validate_image_manifest(im))


def test_manifest_replay_from_another_workflow_is_refused():
    src, est, pb, im, pp = _pipeline()
    other = {**WF, "workflow_identity": "Staging publish", "job_identity": "build-publish"}
    bad = w.pre_push_verify(image_manifest=im, fresh_source_manifest=src, workflow=other,
                            intended_image_digest=DIGEST_A, intended_tags=[TAG],
                            fresh_commit_sha=SHA, fresh_tree_identity=TREE)
    assert bad["result"] == "FAIL"
    assert any("replay" in p for p in bad["problems"])


def test_manifest_replay_across_authorization_windows_is_refused():
    src, est, pb, im, pp = _pipeline()
    stale_auth = {"issuance": "2026-08-06T15:30:42Z", "expiry": "2026-08-07T13:30:42Z",
                  "duration_seconds": 79200}
    import cache_authority as ca
    stale_auth["pair_digest"] = ca.digest({k: stale_auth[k] for k in
                                           ("issuance", "expiry", "duration_seconds")})
    bad = w.pre_push_verify(image_manifest=im, fresh_source_manifest=src, workflow=WF,
                            intended_image_digest=DIGEST_A, intended_tags=[TAG],
                            authorization=stale_auth, fresh_commit_sha=SHA, fresh_tree_identity=TREE)
    assert bad["result"] == "FAIL"
    assert any("authorization" in p for p in bad["problems"])


def test_a_forged_pass_result_does_not_validate():
    src, est, pb, im, pp = _pipeline()
    forged = copy.deepcopy(pp)
    forged["problems"] = ["image substitution: ..."]
    forged["result"] = "PASS"                    # claim PASS while carrying a problem
    assert w.record_passes(forged) is False
    # And a record whose token no longer matches its content is rejected.
    tampered = copy.deepcopy(pp)
    tampered["intended_image_digest"] = DIGEST_B
    assert w.validate_pre_push(tampered) != []


# ===================================================================== CLI exit codes
def test_cli_returns_nonzero_on_a_failing_verification(tmp_path):
    import json
    src_spec = _source_spec()
    est = w.establish(workflow=WF, source_manifest=_source(), expected_phase="pre_build",
                      commit_sha=SHA, tree_identity=TREE)
    im = _pipeline()[3]
    params = {"image_manifest": w._thaw(im), "workflow": WF, "source_spec": src_spec,
              "intended_image_digest": DIGEST_B, "intended_tags": [TAG],
              "commit_sha": SHA, "tree_identity": TREE}
    p = tmp_path / "params.json"
    p.write_text(json.dumps(params))
    rc = w.main(["pre_push_verify", "--params", str(p)])
    assert rc == 1


def test_cli_returns_zero_on_a_passing_verification(tmp_path):
    import json
    params = {"source_spec": _source_spec(), "workflow": WF, "expected_phase": "pre_build",
              "commit_sha": SHA, "tree_identity": TREE}
    p = tmp_path / "params.json"
    p.write_text(json.dumps(params))
    rc = w.main(["establish", "--params", str(p)])
    assert rc == 0
