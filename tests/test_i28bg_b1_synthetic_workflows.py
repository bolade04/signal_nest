"""Gate 4N-I28BG-B1 — isolated synthetic publication-workflow fixtures.

Minimal but structurally realistic single-job publish workflows, one valid and one per structural
defect, asserting the static graph validator PASSes the valid one and FAILs (or otherwise refuses)
each defective one. The two REAL publication workflows are never modified here.
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import workflow_graph_validator as g  # noqa: E402

# A step library. Each entry is a YAML step block (list item under `steps:`).
CHECKOUT = "      - name: checkout\n        uses: actions/checkout@v4\n"
ESTABLISH = ("      - name: establish\n"
             "        run: python scripts/workflow_assurance.py establish --params est.json\n")
PREBUILD = ("      - name: prebuild\n"
            "        run: python scripts/workflow_assurance.py pre_build_verify --params pb.json\n")
BUILD = ("      - name: build\n        uses: docker/build-push-action@v6\n"
         "        with:\n          push: false\n")
IMAGE_BIND = ("      - name: bind\n"
              "        run: python scripts/workflow_assurance.py post_build_image_bind --params ib.json\n")
PREPUSH = ("      - name: prepush\n"
           "        run: python scripts/workflow_assurance.py pre_push_verify --params pp.json\n")
PUSH = "      - name: push\n        run: docker push \"$URI\"\n"


def _wf(steps: str, *, job_extra: str = "", matrix: bool = False) -> str:
    strat = ("    strategy:\n      matrix:\n        arch: [amd64, arm64]\n" if matrix else "")
    return (
        "name: synth publish\n"
        "on:\n  workflow_dispatch:\n"
        "jobs:\n"
        "  build-publish:\n"
        "    runs-on: ubuntu-latest\n"
        f"{strat}{job_extra}"
        "    steps:\n"
        f"{steps}"
    )


VALID = _wf(CHECKOUT + ESTABLISH + PREBUILD + BUILD + IMAGE_BIND + PREPUSH + PUSH)


def _validate(tmp_path, yaml_text):
    p = tmp_path / "wf.yml"
    p.write_text(yaml_text)
    return g.validate_workflow(p)


# ===================================================================== the valid fixture
def test_a_valid_integrated_workflow_passes(tmp_path):
    res = _validate(tmp_path, VALID)
    assert res["status"] == g.STATUS_PASS, res["problems"]
    assert res["single_job"] is True


# ===================================================================== the defect fixtures
def test_missing_checkout_fails(tmp_path):
    res = _validate(tmp_path, _wf(ESTABLISH + PREBUILD + BUILD + IMAGE_BIND + PREPUSH + PUSH))
    assert res["status"] == g.STATUS_FAIL
    assert any("checkout" in p for p in res["problems"])


def test_assurance_after_docker_fails(tmp_path):
    # establish placed AFTER the build.
    res = _validate(tmp_path, _wf(CHECKOUT + BUILD + ESTABLISH + PREBUILD + IMAGE_BIND + PREPUSH + PUSH))
    assert res["status"] == g.STATUS_FAIL
    assert any("not independently guarded by its own pre-build" in p or "envelope" in p or
               "must follow checkout" in p for p in res["problems"])


def test_missing_image_bind_fails(tmp_path):
    res = _validate(tmp_path, _wf(CHECKOUT + ESTABLISH + PREBUILD + BUILD + PREPUSH + PUSH))
    assert res["status"] == g.STATUS_FAIL
    assert any("image-bind" in p for p in res["problems"])


def test_missing_pre_push_fails(tmp_path):
    res = _validate(tmp_path, _wf(CHECKOUT + ESTABLISH + PREBUILD + BUILD + IMAGE_BIND + PUSH))
    assert res["status"] == g.STATUS_FAIL
    assert any("pre-push" in p for p in res["problems"])


def test_verifier_continue_on_error_fails(tmp_path):
    est_coe = ("      - name: establish\n        continue-on-error: true\n"
               "        run: python scripts/workflow_assurance.py establish --params est.json\n")
    res = _validate(tmp_path, _wf(CHECKOUT + est_coe + PREBUILD + BUILD + IMAGE_BIND + PREPUSH + PUSH))
    assert res["status"] == g.STATUS_FAIL
    assert any("continue-on-error" in p for p in res["problems"])


def test_docker_always_fails(tmp_path):
    build_always = ("      - name: build\n        if: always()\n"
                    "        uses: docker/build-push-action@v6\n        with:\n          push: false\n")
    res = _validate(tmp_path, _wf(CHECKOUT + ESTABLISH + PREBUILD + build_always + IMAGE_BIND + PREPUSH + PUSH))
    assert res["status"] == g.STATUS_FAIL
    assert any("always()" in p for p in res["problems"])


def test_alternate_push_path_fails(tmp_path):
    # A push BEFORE the pre-push verify — an unprotected path.
    res = _validate(tmp_path, _wf(CHECKOUT + ESTABLISH + PREBUILD + BUILD + IMAGE_BIND + PUSH + PREPUSH + PUSH))
    assert res["status"] == g.STATUS_FAIL
    assert any("alternate push path" in p for p in res["problems"])


def test_two_builds_only_one_protected_fails(tmp_path):
    build2 = ("      - name: build2\n        uses: docker/build-push-action@v6\n"
              "        with:\n          push: false\n")
    # Second build AFTER the image-bind → the bind does not follow the last build.
    res = _validate(tmp_path, _wf(CHECKOUT + ESTABLISH + PREBUILD + BUILD + IMAGE_BIND + build2 + PREPUSH + PUSH))
    assert res["status"] == g.STATUS_FAIL
    assert any("not followed by its own image-bind" in p for p in res["problems"])


def test_matrix_arm_unprotected_fails(tmp_path):
    est_guarded = ("      - name: establish\n        if: matrix.arch == 'amd64'\n"
                   "        run: python scripts/workflow_assurance.py establish --params est.json\n")
    res = _validate(tmp_path, _wf(CHECKOUT + est_guarded + PREBUILD + BUILD + IMAGE_BIND + PREPUSH + PUSH,
                                  matrix=True))
    assert res["status"] == g.STATUS_FAIL
    assert any("matrix-conditional" in p for p in res["problems"])


def test_source_mutation_after_establishment_fails(tmp_path):
    mutate = "      - name: patch\n        run: git checkout HEAD~1 -- apps/\n"
    res = _validate(tmp_path, _wf(CHECKOUT + ESTABLISH + mutate + PREBUILD + BUILD + IMAGE_BIND + PREPUSH + PUSH))
    assert res["status"] == g.STATUS_FAIL
    assert any("source-mutating" in p for p in res["problems"])


def test_cache_restore_after_verification_fails(tmp_path):
    cache = "      - name: cache\n        uses: actions/cache@v4\n        with:\n          path: /x\n          key: k\n"
    res = _validate(tmp_path, _wf(CHECKOUT + ESTABLISH + PREBUILD + cache + BUILD + IMAGE_BIND + PREPUSH + PUSH))
    assert res["status"] == g.STATUS_FAIL
    assert any("cache restore" in p for p in res["problems"])


def test_checkout_after_verification_fails(tmp_path):
    res = _validate(tmp_path, _wf(CHECKOUT + ESTABLISH + PREBUILD + CHECKOUT + BUILD + IMAGE_BIND + PREPUSH + PUSH))
    assert res["status"] == g.STATUS_FAIL
    assert any("checkout step" in p and "after establishment" in p for p in res["problems"])


def test_mutable_action_reference_is_detected(tmp_path):
    # The valid fixture already uses tag-pinned (mutable) actions; the validator RECORDS them.
    res = _validate(tmp_path, VALID)
    assert res["mutable_actions"], "tag-pinned actions must be reported as mutable"
    assert any(m["uses"].startswith("docker/build-push-action") for m in res["mutable_actions"])


def test_ignored_verifier_output_via_push_always_fails(tmp_path):
    # The pre-push verify runs, but the push runs under always(), ignoring the verify's failure.
    push_always = "      - name: push\n        if: always()\n        run: docker push \"$URI\"\n"
    res = _validate(tmp_path, _wf(CHECKOUT + ESTABLISH + PREBUILD + BUILD + IMAGE_BIND + PREPUSH + push_always))
    assert res["status"] == g.STATUS_FAIL
    assert any("always()" in p for p in res["problems"])


# ===================================================================== a malformed workflow refuses
def test_a_non_mapping_workflow_is_refused(tmp_path):
    p = tmp_path / "bad.yml"
    p.write_text("- just\n- a\n- list\n")
    with pytest.raises(g.WorkflowGraphError):
        g.validate_workflow(p)
