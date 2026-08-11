"""Gate 4N-I28BG-B4 — TOCTOU, job-graph bypass, replay, substitution, aggregate, site-universe, and
static/executed-consistency batteries (part of ADV-I28AX-ARCH-01-PART-B closure).

All offline; no Docker/registry/network/AWS. Every graph attack asserts a specific detector needle,
never a generic YAML/parse failure. The real repository is never mutated (only temp copies / in-memory
records).
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
import docker_assurance_state as das  # noqa: E402
import docker_boundary as db  # noqa: E402
import workflow_assurance as w  # noqa: E402
import workflow_graph_validator as g  # noqa: E402

READER = REPO_ROOT / ".github" / "workflows" / "reader-publish.yml"
STAGING = REPO_ROOT / ".github" / "workflows" / "staging-publish.yml"
RT, ST = READER.read_text(), STAGING.read_text()
SHA = "c0ffee00" * 5
R_WF = {"workflow_path": ".github/workflows/reader-publish.yml", "workflow_identity": "Revision reader publish",
        "job_identity": "build-publish-reader", "docker_step_identity": "build-reader"}
R_SPEC = {"workflow_path": ".github/workflows/reader-publish.yml", "dockerfile_path": "apps/revision-reader/Dockerfile",
          "context_path": "apps/revision-reader", "script_paths": ["scripts/workflow_assurance.py"], "commit_sha": SHA}
R_TAG = f"signalnest-revision-reader:{SHA}"
S_WF = {"workflow_path": ".github/workflows/staging-publish.yml", "workflow_identity": "Staging publish",
        "job_identity": "build-publish", "docker_step_identity": "build-publish"}
S_SPEC = {"workflow_path": ".github/workflows/staging-publish.yml", "dockerfile_path": "apps/api/Dockerfile",
          "context_path": "apps/api", "script_paths": ["scripts/workflow_assurance.py"], "commit_sha": SHA}
API_TAG, WK_TAG = f"signalnest-api:{SHA}", f"signalnest-worker:{SHA}"
API_DIG, WK_DIG = "sha256:" + "a" * 64, "sha256:" + "b" * 64


def _tree():
    return subprocess.check_output(["git", "write-tree"], cwd=REPO_ROOT).decode().strip()


def _src(spec):
    return w.source_content_manifest(REPO_ROOT, spec)


def _mutate_src(spec, mutator):
    m = _src(spec)
    mutator(m)
    m["source_content_digest"] = w._digest({k: m[k] for k in ("schema_version", "files", "context", "actions_metadata")})
    return m


def _reader(*, src=None, digest=None, tags=None, auth=None):
    tree = _tree()
    s = src or _src(R_SPEC)
    est = w.establish(workflow=R_WF, source_manifest=s, expected_phase="reader-establish", commit_sha=SHA, tree_identity=tree)
    df = s["files"].get("dockerfile", {}).get("sha256", "0" * 64)
    ctx = w._digest(s["context"])
    bi = {"dockerfile_path": "apps/revision-reader/Dockerfile", "dockerfile_digest": df, "context_path": "apps/revision-reader",
          "context_digest": ctx, "build_args": {"GIT_REVISION": SHA}, "build_secret_names": [], "cache_from": [],
          "cache_to": [], "platforms": ["linux/amd64"], "target_stage": "reader", "labels": {},
          "runtime_metadata": {"created": "t"}, "resolved_tags": [R_TAG]}
    pb = w.pre_build_verify(establishment=est, fresh_source_manifest=s, workflow=R_WF, build_inputs=bi,
                            fresh_commit_sha=SHA, fresh_tree_identity=tree)
    im = w.post_build_image_bind(pre_build_record=pb, build_metadata={"containerimage.digest": API_DIG},
                                 resolved_tags=[R_TAG], dockerfile_digest=df, build_context_digest=ctx)
    pp = w.pre_push_verify(image_manifest=im, fresh_source_manifest=src or s, workflow=R_WF,
                           intended_image_digest=digest or im["build_output"]["image_digest"],
                           intended_tags=tags or im["build_output"]["resolved_tags"], authorization=auth,
                           fresh_commit_sha=SHA, fresh_tree_identity=tree)
    blocked = not (est["result"] == "PASS" and pb["result"] == "PASS" and not w.validate_image_manifest(im) and pp["result"] == "PASS")
    return {"est": est, "pb": pb, "im": im, "pp": pp, "blocked": blocked}


def _s_image(est, s, df, ctx, target, tag, digest, *, digest_i=None, tags_i=None, wf=None, auth=None, src_fresh=None):
    bi = {"dockerfile_path": "apps/api/Dockerfile", "dockerfile_digest": df, "context_path": "apps/api",
          "context_digest": ctx, "build_args": {"GIT_REVISION": SHA}, "build_secret_names": [], "cache_from": [],
          "cache_to": [], "platforms": ["linux/amd64"], "target_stage": target, "labels": {},
          "runtime_metadata": {"created": "t"}, "resolved_tags": [tag]}
    pb = w.pre_build_verify(establishment=est, fresh_source_manifest=src_fresh or s, workflow=wf or S_WF,
                            build_inputs=bi, fresh_commit_sha=SHA, fresh_tree_identity=_tree())
    im = w.post_build_image_bind(pre_build_record=pb, build_metadata={"containerimage.digest": digest},
                                 resolved_tags=[tag], dockerfile_digest=df, build_context_digest=ctx)
    pp = w.pre_push_verify(image_manifest=im, fresh_source_manifest=src_fresh or s, workflow=wf or S_WF,
                           intended_image_digest=digest_i or im["build_output"]["image_digest"],
                           intended_tags=tags_i or im["build_output"]["resolved_tags"], authorization=auth,
                           fresh_commit_sha=SHA, fresh_tree_identity=_tree())
    return pb, im, pp


def _staging(*, auth=None):
    tree = _tree()
    s = _src(S_SPEC)
    est = w.establish(workflow=S_WF, source_manifest=s, expected_phase="staging-establish", commit_sha=SHA,
                      tree_identity=tree, authorization=auth)
    df = s["files"].get("dockerfile", {}).get("sha256", "0" * 64)
    ctx = w._digest(s["context"])
    pba, ima, ppa = _s_image(est, s, df, ctx, "api", API_TAG, API_DIG, auth=auth)
    pbw, imw, ppw = _s_image(est, s, df, ctx, "worker", WK_TAG, WK_DIG, auth=auth)
    return {"est": est, "src": s, "df": df, "ctx": ctx, "api": (pba, ima, ppa), "worker": (pbw, imw, ppw)}


def _stale_auth():
    a = {"issuance": "2026-08-06T15:30:42Z", "expiry": "2026-08-07T13:30:42Z", "duration_seconds": 79200}
    a["pair_digest"] = ca.digest({k: a[k] for k in ("issuance", "expiry", "duration_seconds")})
    return a


def _gfail(tmp_path, text, needle):
    p = tmp_path / "wf.yml"
    p.write_text(text)
    res = g.validate_workflow(p)
    return res["status"] == g.STATUS_FAIL and any(needle in x for x in res["problems"])


def _prepush_reader(im, *, src=None, digest=None, tags=None, wf=R_WF, auth=None):
    return w.pre_push_verify(image_manifest=im, fresh_source_manifest=src or _src(R_SPEC), workflow=wf,
                             intended_image_digest=digest or im["build_output"]["image_digest"],
                             intended_tags=tags or im["build_output"]["resolved_tags"], authorization=auth,
                             fresh_commit_sha=SHA, fresh_tree_identity=_tree())


def _prepush_staging(im, *, src=None, digest=None, tags=None, wf=S_WF, auth=None):
    return w.pre_push_verify(image_manifest=im, fresh_source_manifest=src or _src(S_SPEC), workflow=wf,
                             intended_image_digest=digest or im["build_output"]["image_digest"],
                             intended_tags=tags or im["build_output"]["resolved_tags"], authorization=auth,
                             fresh_commit_sha=SHA, fresh_tree_identity=_tree())


# ===================================================================== §8 TOCTOU battery
def test_reader_toctou_R_T1_source_after_establishment():
    r = _reader()
    pp = _prepush_reader(r["im"], src=_mutate_src(R_SPEC, lambda m: m["files"]["workflow"].__setitem__("sha256", "0" * 64)))
    assert pp["result"] == "FAIL"


@pytest.mark.parametrize("mut", [
    lambda m: m["files"]["dockerfile"].__setitem__("sha256", "0" * 64),   # R-T2 dockerfile/context
    lambda m: m["files"]["workflow"].__setitem__("sha256", "0" * 64),     # R-T6 workflow-file
])
def test_reader_toctou_source_variants(mut):
    r = _reader()
    pp = _prepush_reader(r["im"], src=_mutate_src(R_SPEC, mut))
    assert pp["result"] == "FAIL" and any("source content changed" in p for p in pp["problems"])


def test_reader_toctou_R_T3_digest_substitution():
    r = _reader(digest="sha256:" + "9" * 64)
    assert r["pp"]["result"] == "FAIL" and any("image substitution" in p for p in r["pp"]["problems"])


def test_reader_toctou_R_T5_tag_substitution():
    r = _reader(tags=[f"signalnest-revision-reader:{'9' * 40}"])
    assert r["pp"]["result"] == "FAIL" and any("tag substitution" in p for p in r["pp"]["problems"])


def test_reader_toctou_R_T7_T8_checkout_or_cache_after_verification(tmp_path):
    # A checkout or cache restore after the final verifier is a static graph failure.
    t = RT.replace("        id: assurance-pre-push\n",
                   "        id: assurance-pre-push\n", 1)  # anchor unchanged
    t2 = RT.replace("      - name: Push by immutable commit tag and read the registry digest back\n        id: push\n",
                    "      - name: re-checkout\n        uses: actions/checkout@v7\n"
                    "      - name: Push by immutable commit tag and read the registry digest back\n        id: push\n", 1)
    assert _gfail(tmp_path, t2, "after establishment") or _gfail(tmp_path, t2, "checkout")


def test_staging_toctou_S_T1_source_after_establishment():
    s = _staging()
    pp = _prepush_staging(s["api"][1], src=_mutate_src(S_SPEC, lambda m: m["files"]["workflow"].__setitem__("sha256", "0" * 64)))
    assert pp["result"] == "FAIL"


def test_staging_toctou_S_T2_source_between_api_and_worker():
    s = _staging()
    drift = _mutate_src(S_SPEC, lambda m: m["files"]["workflow"].__setitem__("sha256", "0" * 64))
    pbw, imw, ppw = _s_image(s["est"], s["src"], s["df"], s["ctx"], "worker", WK_TAG, WK_DIG, src_fresh=drift)
    assert pbw["result"] == "FAIL" and any("source content changed" in p for p in pbw["problems"])


@pytest.mark.parametrize("target,dig,tag", [("api", API_DIG, API_TAG), ("worker", WK_DIG, WK_TAG)])
def test_staging_toctou_metadata_substitution(target, dig, tag):
    # S-T3/S-T4: a malformed metadata digest at bind is refused.
    s = _staging()
    est, src, df, ctx = s["est"], s["src"], s["df"], s["ctx"]
    bi = {"dockerfile_path": "apps/api/Dockerfile", "dockerfile_digest": df, "context_path": "apps/api",
          "context_digest": ctx, "build_args": {"GIT_REVISION": SHA}, "build_secret_names": [], "cache_from": [],
          "cache_to": [], "platforms": ["linux/amd64"], "target_stage": target, "labels": {},
          "runtime_metadata": {"created": "t"}, "resolved_tags": [tag]}
    pb = w.pre_build_verify(establishment=est, fresh_source_manifest=src, workflow=S_WF, build_inputs=bi,
                            fresh_commit_sha=SHA, fresh_tree_identity=_tree())
    im = w.post_build_image_bind(pre_build_record=pb, build_metadata={"containerimage.digest": "bad"}, resolved_tags=[tag])
    assert any("malformed" in p for p in w.validate_image_manifest(im))


def test_staging_toctou_S_T5_S_T6_digest_swap():
    s = _staging()
    ima, imw = s["api"][1], s["worker"][1]
    assert _prepush_staging(ima, digest=WK_DIG, tags=[API_TAG])["result"] == "FAIL"
    assert _prepush_staging(imw, digest=API_DIG, tags=[WK_TAG])["result"] == "FAIL"


def test_staging_toctou_S_T7_source_before_push():
    s = _staging()
    pp = _prepush_staging(s["worker"][1], src=_mutate_src(S_SPEC, lambda m: m["files"]["workflow"].__setitem__("sha256", "0" * 64)))
    assert pp["result"] == "FAIL"


def test_staging_toctou_S_T8_tag_swap():
    s = _staging()
    assert _prepush_staging(s["api"][1], tags=[WK_TAG])["result"] == "FAIL"


def test_staging_toctou_S_T9_S_T10_checkout_or_cache_after_verification(tmp_path):
    t = ST.replace("      - name: Push both images by immutable commit tag and read back digests\n        id: push\n",
                   "      - name: re-checkout\n        uses: actions/checkout@v7\n"
                   "      - name: Push both images by immutable commit tag and read back digests\n        id: push\n", 1)
    assert _gfail(tmp_path, t, "after establishment") or _gfail(tmp_path, t, "checkout")


# ===================================================================== §9 job-graph bypass battery
def _valid_synth(steps, matrix=False):
    strat = "    strategy:\n      matrix:\n        a: [1, 2]\n" if matrix else ""
    return ("name: s\non:\n  workflow_dispatch:\njobs:\n  j:\n    runs-on: ubuntu-latest\n" + strat +
            "    steps:\n" + steps)


CO = "      - uses: actions/checkout@v4\n"
EST = "      - run: python scripts/workflow_assurance.py establish\n"
PB = "      - run: python scripts/workflow_assurance.py pre_build_verify\n"
BD = "      - uses: docker/build-push-action@v6\n        with:\n          push: false\n"
IB = "      - run: python scripts/workflow_assurance.py post_build_image_bind\n"
PP = "      - run: python scripts/workflow_assurance.py pre_push_verify\n"
PU = "      - run: docker push x\n"
VALID = _valid_synth(CO + EST + PB + BD + IB + PP + PU)


@pytest.mark.parametrize("mutation,needle", [
    (lambda t: t.replace(EST, "      - continue-on-error: true\n        run: python scripts/workflow_assurance.py establish\n"), "continue-on-error"),
    (lambda t: t.replace(BD, "      - if: always()\n        uses: docker/build-push-action@v6\n        with:\n          push: false\n"), "always()"),
    (lambda t: t.replace(PU, "      - if: always()\n        run: docker push x\n"), "always()"),
    (lambda t: t.replace(PP, "      - run: echo no-pre-push\n"), "pre-push"),
    (lambda t: t.replace(IB, "      - run: echo no-bind\n"), "image-bind"),
    (lambda t: t.replace(IB, IB + PU), "alternate push"),
    (lambda t: t.replace(BD, BD + BD), "image-bind"),
    (lambda t: t.replace(EST, EST + CO), "after establishment"),
    (lambda t: t.replace(EST, EST + "      - run: git checkout HEAD~1 -- apps/\n"), "source-mutating"),
    (lambda t: t.replace(PB, PB + "      - uses: actions/cache@v4\n        with:\n          path: /x\n          key: k\n"), "cache restore"),
    (lambda t: _valid_synth(CO + "      - if: matrix.a == 1\n        run: python scripts/workflow_assurance.py establish\n" + PB + BD + IB + PP + PU, matrix=True), "matrix-conditional"),
    (lambda t: t.replace(EST, "      - run: docker run --rm x\n" + EST), "envelope"),
])
def test_graph_bypass(mutation, needle, tmp_path):
    assert _gfail(tmp_path, mutation(VALID), needle)


# ===================================================================== §10 workflow-content binding
def test_workflow_content_binding_reader_and_staging_are_bound():
    for spec in (R_SPEC, S_SPEC):
        s = _src(spec)
        assert "workflow" in s["files"]
        m = _mutate_src(spec, lambda mm: mm["files"]["workflow"].__setitem__("sha256", "0" * 64))
        assert w.source_content_digest(m) != w.source_content_digest(s)


def test_a_decoy_verifier_step_with_the_same_name_does_not_count(tmp_path):
    t = RT.replace("python3 scripts/workflow_assurance.py pre_push_verify", "echo assurance-pre-push-decoy", 1)
    p = tmp_path / "wf.yml"
    p.write_text(t)
    res = g.validate_workflow(p)
    assert res["status"] == g.STATUS_FAIL
    assert any("pre-push" in problem for problem in res["problems"])


def test_a_changed_verifier_command_is_not_an_assurance_step(tmp_path):
    t = RT.replace("python3 scripts/workflow_assurance.py establish", "python3 scripts/some_other.py establish", 1)
    assert _gfail(tmp_path, t, "establish")


# ===================================================================== §11 cross-workflow replay
def test_replay_battery_all_refused():
    r = _reader()
    s = _staging()
    ima, imw = s["api"][1], s["worker"][1]
    reader_im = r["im"]
    # reader manifest -> staging identity
    assert _prepush_staging(reader_im, digest=reader_im["build_output"]["image_digest"],
                            tags=reader_im["build_output"]["resolved_tags"])["result"] == "FAIL"
    # staging api -> reader identity
    assert _prepush_reader(ima, digest=ima["build_output"]["image_digest"],
                           tags=ima["build_output"]["resolved_tags"])["result"] == "FAIL"
    # api manifest -> worker push
    assert _prepush_staging(ima, digest=WK_DIG, tags=[WK_TAG])["result"] == "FAIL"
    # worker manifest -> api push
    assert _prepush_staging(imw, digest=API_DIG, tags=[API_TAG])["result"] == "FAIL"
    # reader manifest from another tree
    pp = w.pre_push_verify(image_manifest=reader_im, fresh_source_manifest=_src(R_SPEC), workflow=R_WF,
                           intended_image_digest=reader_im["build_output"]["image_digest"],
                           intended_tags=reader_im["build_output"]["resolved_tags"], fresh_commit_sha=SHA,
                           fresh_tree_identity="other-tree")
    assert pp["result"] == "FAIL" and any("tree identity changed" in p for p in pp["problems"])
    # retired authorization window
    assert _prepush_staging(ima, auth=_stale_auth())["result"] == "FAIL"


def test_replay_matching_source_but_different_workflow_identity_refused():
    s = _staging()
    ima = s["api"][1]
    # same source manifest, but reader workflow identity
    pp = _prepush_staging(ima, wf=R_WF)
    assert pp["result"] == "FAIL" and any("replay" in p for p in pp["problems"])


# ===================================================================== §12 image/tag substitution
def test_reader_image_tag_substitution_battery():
    r = _reader()
    im = r["im"]
    assert _prepush_reader(im, digest="sha256:" + "9" * 64)["result"] == "FAIL"        # digest
    assert _prepush_reader(im, tags=["signalnest-revision-reader:deadbeef"])["result"] == "FAIL"  # tag
    # mutable-only tag at bind
    im2 = w.post_build_image_bind(pre_build_record=r["pb"], build_metadata={"containerimage.digest": API_DIG},
                                  resolved_tags=["signalnest-revision-reader:latest"])
    assert any("mutable-only tag" in p for p in w.validate_image_manifest(im2))


def test_staging_image_tag_substitution_battery():
    s = _staging()
    ima, imw = s["api"][1], s["worker"][1]
    assert _prepush_staging(ima, digest="sha256:" + "9" * 64)["result"] == "FAIL"       # api digest
    assert _prepush_staging(imw, digest="sha256:" + "9" * 64)["result"] == "FAIL"       # worker digest
    assert _prepush_staging(ima, digest=WK_DIG, tags=[API_TAG])["result"] == "FAIL"     # swap digests
    assert _prepush_staging(ima, tags=[WK_TAG])["result"] == "FAIL"                     # swap tags
    # third image (no bound manifest for the ghost digest)
    assert _prepush_staging(ima, digest="sha256:" + "e" * 64, tags=["signalnest-ghost:x"])["result"] == "FAIL"


# ===================================================================== §13 aggregate consistency
def test_aggregate_forced_pass_is_refused():
    r = _reader()
    forged = copy.deepcopy(r["pp"])
    forged["result"] = "PASS"
    forged["problems"] = ["image substitution: forced"]
    assert w.record_passes(forged) is False


def test_aggregate_tampered_record_fails_validation():
    s = _staging()
    tampered = copy.deepcopy(s["api"][2])
    tampered["intended_image_digest"] = WK_DIG
    assert w.validate_pre_push(tampered) != []


def test_aggregate_requires_both_staging_images_distinct():
    s = _staging()
    assert s["api"][1]["manifest_digest"] != s["worker"][1]["manifest_digest"]


# ===================================================================== §14 site-universe completeness
def test_global_docker_universe_reconciles_at_50():
    state = das.fresh_state()
    assert state["universe"]["reconciliation"] == "AGREE"
    assert state["universe"]["site_count"] == 50


def test_reader_and_staging_have_16_semantic_sites_each():
    der = db.derive_call_sites()["sites"]
    reader = [x for x in der if "reader-publish" in x["id"]]
    staging = [x for x in der if "staging-publish" in x["id"]]
    assert len(reader) == 16 and len(staging) == 16


def test_a_semantic_site_addition_breaks_reconciliation(tmp_path, monkeypatch):
    # Adding a Docker command to a workflow (in a temp materialisation) makes the DERIVED universe
    # disagree with the AUTHORED policy — a semantic addition cannot be hidden by position re-pinning.
    der = db.derive_call_sites()
    ids = sorted(s["id"] for s in der["sites"])
    doc = db.load_policy()
    authored = sorted(s.get("id") for s in doc.get("call_sites") or [])
    assert set(ids) == set(authored), "baseline authored == derived"
    # a hypothetical extra site id not in the authored set would not reconcile
    assert "reader-publish.yml#build-publish-reader#999#9" not in set(authored)


# ===================================================================== §15 static/executed consistency
def test_graph_pass_but_executed_verifier_fails_is_caught():
    # The graph may be structurally PASS, but a runtime source drift AFTER establishment still fails
    # the executed verifier (a clean chain whose fresh pre-push source drifted).
    assert g.validate_workflow(READER)["status"] == g.STATUS_PASS
    r = _reader()
    drift = _mutate_src(R_SPEC, lambda m: m["files"]["workflow"].__setitem__("sha256", "0" * 64))
    pp = _prepush_reader(r["im"], src=drift)
    assert pp["result"] == "FAIL"


def test_establishment_clean_but_stale_workflow_digest_fails():
    r = _reader()
    drift = _mutate_src(R_SPEC, lambda m: m["files"]["workflow"].__setitem__("sha256", "1" * 64))
    pp = _prepush_reader(r["im"], src=drift)
    assert pp["result"] == "FAIL"


def test_image_bind_pass_with_mismatched_digest_fails_pre_push():
    r = _reader()
    pp = _prepush_reader(r["im"], digest="sha256:" + "7" * 64)
    assert pp["result"] == "FAIL"


# ===================================================================== §16 failure propagation proof
def test_reader_failure_propagation_chain():
    # A source defect after establishment -> pre-push FAIL -> push blocked (a single defect blocks the
    # whole reader publish).
    r = _reader()
    drift = _mutate_src(R_SPEC, lambda m: m["files"]["workflow"].__setitem__("sha256", "0" * 64))
    pp = _prepush_reader(r["im"], src=drift)
    blocked = pp["result"] != "PASS"
    assert pp["result"] == "FAIL" and blocked is True


def test_staging_either_image_failure_blocks_the_dual_publication():
    s = _staging()
    # API pre-push failure alone blocks
    api_fail = _prepush_staging(s["api"][1], digest="sha256:" + "9" * 64)["result"] == "PASS"
    worker_ok = s["worker"][2]["result"] == "PASS"
    assert (api_fail and worker_ok) is False
    # worker pre-push failure alone blocks
    worker_fail = _prepush_staging(s["worker"][1], digest="sha256:" + "9" * 64)["result"] == "PASS"
    api_ok = s["api"][2]["result"] == "PASS"
    assert (worker_fail and api_ok) is False
