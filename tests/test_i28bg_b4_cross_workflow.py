"""Gate 4N-I28BG-B4 — cross-workflow 32-arm falsification harness (closes ADV-I28AX-ARCH-01-PART-B).

One isolated, self-protecting harness that mutates BOTH integrated publication workflows and their
runtime verifier inputs and proves, for exactly 32 scored arms, that the intended detector fires and
push eligibility becomes false — with reader and staging behaving correctly and the real repository
left unchanged. No Docker/registry/network/AWS. Generic YAML/parser failure never substitutes for the
intended detector: every graph arm asserts a specific problem needle.
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
STAGING = REPO_ROOT / ".github" / "workflows" / "staging-publish.yml"
READER_TEXT = READER.read_text()
STAGING_TEXT = STAGING.read_text()
SHA = "c0ffee00" * 5

R_WF = {"workflow_path": ".github/workflows/reader-publish.yml",
        "workflow_identity": "Revision reader publish", "job_identity": "build-publish-reader",
        "docker_step_identity": "build-reader"}
R_SPEC = {"workflow_path": ".github/workflows/reader-publish.yml",
          "dockerfile_path": "apps/revision-reader/Dockerfile", "context_path": "apps/revision-reader",
          "script_paths": ["scripts/workflow_assurance.py"], "commit_sha": SHA}
R_TAG = f"signalnest-revision-reader:{SHA}"
S_WF = {"workflow_path": ".github/workflows/staging-publish.yml", "workflow_identity": "Staging publish",
        "job_identity": "build-publish", "docker_step_identity": "build-publish"}
S_SPEC = {"workflow_path": ".github/workflows/staging-publish.yml", "dockerfile_path": "apps/api/Dockerfile",
          "context_path": "apps/api", "script_paths": ["scripts/workflow_assurance.py"], "commit_sha": SHA}
API_TAG = f"signalnest-api:{SHA}"
WK_TAG = f"signalnest-worker:{SHA}"
API_DIG = "sha256:" + "a" * 64
WK_DIG = "sha256:" + "b" * 64


def _tree():
    return subprocess.check_output(["git", "write-tree"], cwd=REPO_ROOT).decode().strip()


def _graph_fail(text, tmp_path, name):
    p = tmp_path / name
    p.write_text(text)
    return g.validate_workflow(p)


# ---- reader chain ----
def _reader_chain(*, src=None, intended_digest=None, intended_tags=None, authorization=None,
                  bi_over=None):
    tree = _tree()
    src = src or w.source_content_manifest(REPO_ROOT, R_SPEC)
    est = w.establish(workflow=R_WF, source_manifest=src, expected_phase="reader-establish",
                      commit_sha=SHA, tree_identity=tree)
    df = src["files"].get("dockerfile", {}).get("sha256", "0" * 64)
    ctx = w._digest(src["context"])
    bi = {"dockerfile_path": "apps/revision-reader/Dockerfile", "dockerfile_digest": df,
          "context_path": "apps/revision-reader", "context_digest": ctx, "build_args": {"GIT_REVISION": SHA},
          "build_secret_names": [], "cache_from": [], "cache_to": [], "platforms": ["linux/amd64"],
          "target_stage": "reader", "labels": {}, "runtime_metadata": {"created": "t"}, "resolved_tags": [R_TAG]}
    if bi_over:
        bi.update(bi_over)
    pb = w.pre_build_verify(establishment=est, fresh_source_manifest=src, workflow=R_WF, build_inputs=bi,
                            fresh_commit_sha=SHA, fresh_tree_identity=tree)
    im = w.post_build_image_bind(pre_build_record=pb, build_metadata={"containerimage.digest": API_DIG},
                                 resolved_tags=[R_TAG], dockerfile_digest=df, build_context_digest=ctx)
    pp = w.pre_push_verify(image_manifest=im, fresh_source_manifest=src, workflow=R_WF,
                           intended_image_digest=intended_digest or im["build_output"]["image_digest"],
                           intended_tags=intended_tags or im["build_output"]["resolved_tags"],
                           authorization=authorization, fresh_commit_sha=SHA, fresh_tree_identity=tree)
    blocked = not (est["result"] == "PASS" and pb["result"] == "PASS"
                   and not w.validate_image_manifest(im) and pp["result"] == "PASS")
    return {"est": est, "pb": pb, "im": im, "pp": pp, "blocked": blocked}


# ---- staging chain (dual image) ----
def _staging_image(est, src, df, ctx, target, tag, digest, *, intended_digest=None, intended_tags=None,
                   workflow=None, authorization=None):
    bi = {"dockerfile_path": "apps/api/Dockerfile", "dockerfile_digest": df, "context_path": "apps/api",
          "context_digest": ctx, "build_args": {"GIT_REVISION": SHA}, "build_secret_names": [],
          "cache_from": [], "cache_to": [], "platforms": ["linux/amd64"], "target_stage": target,
          "labels": {}, "runtime_metadata": {"created": "t"}, "resolved_tags": [tag]}
    pb = w.pre_build_verify(establishment=est, fresh_source_manifest=src, workflow=workflow or S_WF,
                            build_inputs=bi, fresh_commit_sha=SHA, fresh_tree_identity=_tree())
    im = w.post_build_image_bind(pre_build_record=pb, build_metadata={"containerimage.digest": digest},
                                 resolved_tags=[tag], dockerfile_digest=df, build_context_digest=ctx)
    pp = w.pre_push_verify(image_manifest=im, fresh_source_manifest=src, workflow=workflow or S_WF,
                           intended_image_digest=intended_digest or im["build_output"]["image_digest"],
                           intended_tags=intended_tags or im["build_output"]["resolved_tags"],
                           authorization=authorization, fresh_commit_sha=SHA, fresh_tree_identity=_tree())
    return pb, im, pp


def _staging_chain(*, src=None, authorization=None, api=None, worker=None):
    tree = _tree()
    src = src or w.source_content_manifest(REPO_ROOT, S_SPEC)
    est = w.establish(workflow=S_WF, source_manifest=src, expected_phase="staging-establish",
                      commit_sha=SHA, tree_identity=tree, authorization=authorization)
    df = src["files"].get("dockerfile", {}).get("sha256", "0" * 64)
    ctx = w._digest(src["context"])
    a = dict(target="api", tag=API_TAG, digest=API_DIG)
    b = dict(target="worker", tag=WK_TAG, digest=WK_DIG)
    if api:
        a.update(api)
    if worker:
        b.update(worker)
    pba, ima, ppa = _staging_image(est, src, df, ctx, authorization=authorization, **a)
    pbw, imw, ppw = _staging_image(est, src, df, ctx, authorization=authorization, **b)
    blocked = not (est["result"] == "PASS" and pba["result"] == "PASS" and ppa["result"] == "PASS"
                   and pbw["result"] == "PASS" and ppw["result"] == "PASS"
                   and not w.validate_image_manifest(ima) and not w.validate_image_manifest(imw)
                   and ima["manifest_digest"] != imw["manifest_digest"])
    return {"est": est, "api": (pba, ima, ppa), "worker": (pbw, imw, ppw), "blocked": blocked}


def _drift(spec):
    m = w.source_content_manifest(REPO_ROOT, spec)
    m2 = copy.deepcopy(m)
    m2["source_content_digest"] = "changed-" + m["source_content_digest"]
    return m2


def _stale_auth():
    a = {"issuance": "2026-08-06T15:30:42Z", "expiry": "2026-08-07T13:30:42Z", "duration_seconds": 79200}
    a["pair_digest"] = ca.digest({k: a[k] for k in ("issuance", "expiry", "duration_seconds")})
    return a


# ===================================================================== the 32 scored arms
# Each arm returns (detector_fired: bool, push_blocked: bool). kind is 'graph' or 'verifier'.
def _second(text, needle, repl):
    parts = text.split(needle)
    if len(parts) < 3:
        return text.replace(needle, repl, 1)  # fewer than 2 occurrences
    return parts[0] + needle + parts[1] + repl + needle.join(parts[2:])


def _arm(arm_id, tmp_path):
    RT, ST = READER_TEXT, STAGING_TEXT

    def gfail(text, needle):
        res = _graph_fail(text, tmp_path, f"arm{arm_id}.yml")
        fired = res["status"] == g.STATUS_FAIL and any(needle in p for p in res["problems"])
        return fired, res["status"] == g.STATUS_FAIL

    def gpass(text):  # the *other* workflow unaffected
        return _graph_fail(text, tmp_path, f"arm{arm_id}u.yml")["status"] == g.STATUS_PASS

    if arm_id == 1:
        f, b = gfail(RT.replace("python3 scripts/workflow_assurance.py establish", "echo x", 1), "establish")
        return f and gpass(ST), b
    if arm_id == 2:
        f, b = gfail(ST.replace("python3 scripts/workflow_assurance.py establish", "echo x", 1), "establish")
        return f and gpass(RT), b
    if arm_id == 3:
        t = RT.replace("python3 scripts/workflow_assurance.py establish \\", "echo x \\", 1)
        t += "\n      - name: late\n        run: python3 scripts/workflow_assurance.py establish\n"
        return gfail(t, "establish")
    if arm_id == 4:
        t = ST.replace("python3 scripts/workflow_assurance.py establish \\", "echo x \\", 1)
        t += "\n      - name: late\n        run: python3 scripts/workflow_assurance.py establish\n"
        return gfail(t, "establish")
    if arm_id == 5:
        return gfail(RT.replace("python3 scripts/workflow_assurance.py pre_build_verify", "echo x", 1),
                     "pre-build")
    if arm_id == 6:
        return gfail(ST.replace("python3 scripts/workflow_assurance.py pre_build_verify", "echo x", 1),
                     "pre-build")
    if arm_id == 7:
        return gfail(_second(ST, "python3 scripts/workflow_assurance.py pre_build_verify", "echo x"),
                     "pre-build")
    if arm_id == 8:
        return gfail(RT.replace("python3 scripts/workflow_assurance.py post_build_image_bind", "echo x", 1),
                     "image-bind")
    if arm_id == 9:
        return gfail(ST.replace("python3 scripts/workflow_assurance.py post_build_image_bind", "echo x", 1),
                     "image-bind")
    if arm_id == 10:
        return gfail(_second(ST, "python3 scripts/workflow_assurance.py post_build_image_bind", "echo x"),
                     "image-bind")
    if arm_id == 11:
        return gfail(RT.replace("python3 scripts/workflow_assurance.py pre_push_verify", "echo x", 1),
                     "pre-push")
    if arm_id == 12:
        return gfail(ST.replace("python3 scripts/workflow_assurance.py pre_push_verify", "echo x", 1),
                     "pre-push")
    if arm_id == 13:
        return gfail(_second(ST, "python3 scripts/workflow_assurance.py pre_push_verify", "echo x"),
                     "pre-push")
    # ---- verifier arms 14-26 ----
    if arm_id == 14:  # replay reader manifest into staging
        r = _reader_chain()
        pp = w.pre_push_verify(image_manifest=r["im"], fresh_source_manifest=w.source_content_manifest(REPO_ROOT, R_SPEC),
                               workflow=S_WF, intended_image_digest=r["im"]["build_output"]["image_digest"],
                               intended_tags=r["im"]["build_output"]["resolved_tags"], fresh_commit_sha=SHA,
                               fresh_tree_identity=_tree())
        return pp["result"] == "FAIL" and any("replay" in p for p in pp["problems"]), pp["result"] == "FAIL"
    if arm_id == 15:  # replay staging API manifest into reader
        s = _staging_chain()
        ima = s["api"][1]
        pp = w.pre_push_verify(image_manifest=ima, fresh_source_manifest=w.source_content_manifest(REPO_ROOT, S_SPEC),
                               workflow=R_WF, intended_image_digest=ima["build_output"]["image_digest"],
                               intended_tags=ima["build_output"]["resolved_tags"], fresh_commit_sha=SHA,
                               fresh_tree_identity=_tree())
        return pp["result"] == "FAIL" and any("replay" in p for p in pp["problems"]), pp["result"] == "FAIL"
    if arm_id == 16:  # replay staging worker manifest as staging API (digest/tag mismatch)
        s = _staging_chain()
        imw = s["worker"][1]
        pp = w.pre_push_verify(image_manifest=imw, fresh_source_manifest=w.source_content_manifest(REPO_ROOT, S_SPEC),
                               workflow=S_WF, intended_image_digest=API_DIG, intended_tags=[API_TAG],
                               fresh_commit_sha=SHA, fresh_tree_identity=_tree())
        return pp["result"] == "FAIL" and any("substitution" in p for p in pp["problems"]), pp["result"] == "FAIL"
    if arm_id == 17:  # reader source changes after pre-build -> pre-push fresh
        r = _reader_chain()
        pp = w.pre_push_verify(image_manifest=r["im"], fresh_source_manifest=_drift(R_SPEC), workflow=R_WF,
                               intended_image_digest=r["im"]["build_output"]["image_digest"],
                               intended_tags=r["im"]["build_output"]["resolved_tags"], fresh_commit_sha=SHA,
                               fresh_tree_identity=_tree())
        return pp["result"] == "FAIL" and any("source content changed" in p for p in pp["problems"]), True
    if arm_id == 18:  # staging source changes between API and worker builds
        est, src = _staging_chain()["est"], _drift(S_SPEC)
        est = w.establish(workflow=S_WF, source_manifest=w.source_content_manifest(REPO_ROOT, S_SPEC),
                          expected_phase="staging-establish", commit_sha=SHA, tree_identity=_tree())
        df = "0" * 64
        pbw, imw, ppw = _staging_image(est, src, df, "0" * 64, "worker", WK_TAG, WK_DIG)
        return pbw["result"] == "FAIL" and any("source content changed" in p for p in pbw["problems"]), True
    if arm_id == 19:  # staging source changes after both binds, before push
        s = _staging_chain()
        ima = s["api"][1]
        pp = w.pre_push_verify(image_manifest=ima, fresh_source_manifest=_drift(S_SPEC), workflow=S_WF,
                               intended_image_digest=ima["build_output"]["image_digest"],
                               intended_tags=ima["build_output"]["resolved_tags"], fresh_commit_sha=SHA,
                               fresh_tree_identity=_tree())
        return pp["result"] == "FAIL" and any("source content changed" in p for p in pp["problems"]), True
    if arm_id == 20:  # reader Dockerfile changes after establishment (source manifest refusal)
        r = _reader_chain()
        drift = w.source_content_manifest(REPO_ROOT, R_SPEC)
        drift["files"]["dockerfile"]["sha256"] = "0" * 64
        drift["source_content_digest"] = w._digest({k: drift[k] for k in ("schema_version", "files", "context", "actions_metadata")})
        pp = w.pre_push_verify(image_manifest=r["im"], fresh_source_manifest=drift, workflow=R_WF,
                               intended_image_digest=r["im"]["build_output"]["image_digest"],
                               intended_tags=r["im"]["build_output"]["resolved_tags"], fresh_commit_sha=SHA,
                               fresh_tree_identity=_tree())
        return pp["result"] == "FAIL" and any("source content changed" in p for p in pp["problems"]), True
    if arm_id == 21:  # API Dockerfile changes after establishment
        est = w.establish(workflow=S_WF, source_manifest=w.source_content_manifest(REPO_ROOT, S_SPEC),
                          expected_phase="staging-establish", commit_sha=SHA, tree_identity=_tree())
        drift = w.source_content_manifest(REPO_ROOT, S_SPEC)
        drift["files"]["dockerfile"]["sha256"] = "0" * 64
        drift["source_content_digest"] = w._digest({k: drift[k] for k in ("schema_version", "files", "context", "actions_metadata")})
        pba, ima, ppa = _staging_image(est, drift, "0" * 64, w._digest(drift["context"]), "api", API_TAG, API_DIG)
        return pba["result"] == "FAIL" and any("source content changed" in p for p in pba["problems"]), True
    if arm_id == 22:  # worker Dockerfile changes after API build
        est = w.establish(workflow=S_WF, source_manifest=w.source_content_manifest(REPO_ROOT, S_SPEC),
                          expected_phase="staging-establish", commit_sha=SHA, tree_identity=_tree())
        drift = w.source_content_manifest(REPO_ROOT, S_SPEC)
        drift["files"]["dockerfile"]["sha256"] = "0" * 64
        drift["source_content_digest"] = w._digest({k: drift[k] for k in ("schema_version", "files", "context", "actions_metadata")})
        pbw, imw, ppw = _staging_image(est, drift, "0" * 64, w._digest(drift["context"]), "worker", WK_TAG, WK_DIG)
        return pbw["result"] == "FAIL" and any("source content changed" in p for p in pbw["problems"]), True
    if arm_id == 23:  # reader image digest substituted after bind
        r = _reader_chain(intended_digest="sha256:" + "9" * 64)
        return r["pp"]["result"] == "FAIL" and any("image substitution" in p for p in r["pp"]["problems"]), r["blocked"]
    if arm_id == 24:  # swap staging API/worker digests
        s = _staging_chain()
        ima = s["api"][1]
        pp = w.pre_push_verify(image_manifest=ima, fresh_source_manifest=w.source_content_manifest(REPO_ROOT, S_SPEC),
                               workflow=S_WF, intended_image_digest=WK_DIG, intended_tags=[API_TAG],
                               fresh_commit_sha=SHA, fresh_tree_identity=_tree())
        return pp["result"] == "FAIL" and any("substitution" in p for p in pp["problems"]), True
    if arm_id == 25:  # reader authorization changes before push
        r = _reader_chain(authorization=_stale_auth())
        return r["pp"]["result"] == "FAIL" and any("authorization" in p for p in r["pp"]["problems"]), r["blocked"]
    if arm_id == 26:  # staging authorization changes before push (both)
        s = _staging_chain(authorization=_stale_auth())
        return s["blocked"], s["blocked"]
    # ---- graph bypass arms 27-32 ----
    if arm_id == 27:  # reader push always()
        t = RT.replace("      - name: Push by immutable commit tag and read the registry digest back\n        id: push\n",
                       "      - name: Push by immutable commit tag and read the registry digest back\n        id: push\n        if: always()\n", 1)
        return gfail(t, "always()")
    if arm_id == 28:  # staging push always()
        t = ST.replace("        id: build-api\n", "        id: build-api\n        if: always()\n", 1)
        return gfail(t, "always()")
    if arm_id == 29:  # reader verifier continue-on-error
        t = RT.replace("        id: assurance-pre-build\n", "        id: assurance-pre-build\n        continue-on-error: true\n", 1)
        return gfail(t, "continue-on-error")
    if arm_id == 30:  # staging verifier continue-on-error
        t = ST.replace("        id: assurance-pre-build-api\n", "        id: assurance-pre-build-api\n        continue-on-error: true\n", 1)
        return gfail(t, "continue-on-error")
    if arm_id == 31:  # new reader docker site without coverage
        t = RT.replace("        uses: actions/checkout@v7\n",
                       "        uses: actions/checkout@v7\n      - name: early\n        run: docker run --rm x\n", 1)
        return gfail(t, "envelope")
    if arm_id == 32:  # new staging docker site without coverage
        t = ST.replace("        uses: actions/checkout@v7\n",
                       "        uses: actions/checkout@v7\n      - name: early\n        run: docker run --rm x\n", 1)
        return gfail(t, "envelope")
    raise AssertionError(f"unknown arm {arm_id}")


ARM_IDS = list(range(1, 33))


def test_the_battery_baseline_is_clean():
    assert g.validate_workflow(READER)["status"] == g.STATUS_PASS
    assert g.validate_workflow(STAGING)["status"] == g.STATUS_PASS


@pytest.mark.parametrize("arm_id", ARM_IDS)
def test_cross_workflow_arm(arm_id, tmp_path):
    detector_fired, push_blocked = _arm(arm_id, tmp_path)
    assert detector_fired, f"arm {arm_id}: intended detector did not fire"
    assert push_blocked, f"arm {arm_id}: push eligibility was not blocked"


# ===================================================================== harness self-protection
def test_exactly_32_unique_scored_arms():
    assert len(ARM_IDS) == 32
    assert sorted(set(ARM_IDS)) == list(range(1, 33))


def test_every_arm_activates_and_hits_a_detector(tmp_path):
    results = {}
    for a in ARM_IDS:
        results[a] = _arm(a, tmp_path)
    escaped = [a for a, (fired, blocked) in results.items() if not blocked]
    void = [a for a, (fired, blocked) in results.items() if not fired]
    assert escaped == [], f"escaped arms: {escaped}"
    assert void == [], f"void (no-detector) arms: {void}"
    assert len(results) == 32


def test_the_real_repository_is_unchanged():
    # The harness mutates only temp copies; the real workflows are byte-identical to what was read.
    assert READER.read_text() == READER_TEXT
    assert STAGING.read_text() == STAGING_TEXT
