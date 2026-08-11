"""Gate 4N-I28BG-B1 — the static publication-workflow graph validator.

Coverage for the derivation itself (jobs, steps, needs, matrices, conditions, roles, mutable action
references) and for the load-bearing B1 posture: the two REAL publication workflows must be
NOT_YET_INTEGRATED, never falsely PASS and never FAIL.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import workflow_graph_validator as g  # noqa: E402

READER = REPO_ROOT / ".github" / "workflows" / "reader-publish.yml"
STAGING = REPO_ROOT / ".github" / "workflows" / "staging-publish.yml"


# ===================================================================== the B1 posture
def test_reader_publish_is_integrated_after_b2():
    # Gate 4N-I28BG-B2 integrated reader-publish; it is now PASS, not NOT_YET_INTEGRATED.
    res = g.validate_workflow(READER)
    assert res["status"] == g.STATUS_PASS
    assert res["assurance_present"] is True


def test_staging_publish_is_integrated_after_b3():
    # Gate 4N-I28BG-B3 integrated staging-publish; it is now PASS.
    res = g.validate_workflow(STAGING)
    assert res["status"] == g.STATUS_PASS
    assert res["assurance_present"] is True


def test_integration_status_both_integrated():
    status = g.integration_status()
    assert status[g.READER_PUBLISH] == g.STATUS_PASS
    assert status[g.STAGING_PUBLISH] == g.STATUS_PASS


def test_an_unintegrated_workflow_is_reported_not_yet_integrated(tmp_path):
    # Both real workflows are now integrated; the NOT_YET_INTEGRATED posture is proven on a synthetic
    # workflow that builds/pushes with no assurance step.
    yaml_text = ("name: bare\non:\n  workflow_dispatch:\n"
                 "jobs:\n  j:\n    runs-on: ubuntu-latest\n    steps:\n"
                 "      - uses: actions/checkout@v4\n"
                 "      - name: build\n        uses: docker/build-push-action@v6\n"
                 "      - name: push\n        run: docker push x\n")
    p = tmp_path / "bare.yml"
    p.write_text(yaml_text)
    assert g.validate_workflow(p)["status"] == g.STATUS_NOT_YET_INTEGRATED


# ===================================================================== the derivation
def test_both_publish_workflows_are_single_job():
    for p in (READER, STAGING):
        res = g.validate_workflow(p)
        assert res["single_job"] is True
        assert res["job_count"] == 1


def test_reader_has_one_build_and_one_push_site():
    res = g.validate_workflow(READER)
    assert res["docker_build_sites"] == 1
    assert res["docker_push_sites"] == 1


def test_staging_has_two_builds():
    res = g.validate_workflow(STAGING)
    assert res["docker_build_sites"] == 2


def test_mutable_action_references_are_reported():
    graph = g.analyse_workflow(READER)
    uses = {m["uses"] for m in graph["mutable_actions"]}
    # Every real action in this repo is tag-pinned (mutable), including the build action.
    assert any(u.startswith("docker/build-push-action") for u in uses)
    assert any(u.startswith("actions/checkout") for u in uses)


def test_roles_are_derived_structurally_not_from_names(tmp_path):
    # A synthetic workflow with build/push commands but no verifier invocation has build/push roles
    # and NO assurance roles — roles come from `uses:`/`run:`, never from a display name.
    yaml_text = ("name: decoy\non:\n  push:\n"
                 "jobs:\n  j:\n    runs-on: ubuntu-latest\n    steps:\n"
                 "      - uses: actions/checkout@v4\n"
                 "      - name: assurance-establish (fake, no invocation)\n        run: echo nothing\n"
                 "      - name: build\n        run: docker build .\n"
                 "      - name: push\n        run: docker push x\n")
    p = tmp_path / "decoy.yml"
    p.write_text(yaml_text)
    graph = g.analyse_workflow(p)
    steps = graph["jobs"]["j"]["steps"]
    assert [s for s in steps if g.ROLE_BUILD in s["roles"]]
    assert [s for s in steps if g.ROLE_PUSH in s["roles"]]
    assert not any(any(r in s["roles"] for r in g._ASSURANCE_ROLES) for s in steps)


def test_a_display_name_alone_does_not_make_an_assurance_step(tmp_path):
    # A step merely NAMED like an assurance step, with no actual verifier invocation, is not one.
    yaml_text = (
        "name: decoy\non:\n  workflow_dispatch:\n"
        "jobs:\n  j:\n    runs-on: ubuntu-latest\n    steps:\n"
        "      - name: assurance-establish (fake)\n        run: echo doing nothing\n"
        "      - name: build\n        run: docker build .\n"
        "      - name: push\n        run: docker push x\n")
    p = tmp_path / "decoy.yml"
    p.write_text(yaml_text)
    res = g.validate_workflow(p)
    # No real verifier invocation → still NOT_YET_INTEGRATED, not falsely integrated/PASS.
    assert res["status"] == g.STATUS_NOT_YET_INTEGRATED


def test_a_workflow_with_no_docker_sites_is_no_docker_sites(tmp_path):
    yaml_text = ("name: ci\non:\n  push:\n"
                 "jobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n"
                 "      - uses: actions/checkout@v4\n"
                 "      - run: pytest\n")
    p = tmp_path / "ci.yml"
    p.write_text(yaml_text)
    assert g.validate_workflow(p)["status"] == g.STATUS_NO_DOCKER_SITES


def test_needs_graph_and_matrix_are_derived(tmp_path):
    yaml_text = (
        "name: multi\non:\n  workflow_dispatch:\n"
        "jobs:\n"
        "  a:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo a\n"
        "  b:\n    needs: [a]\n    runs-on: ubuntu-latest\n"
        "    strategy:\n      matrix:\n        x: [1, 2]\n    steps:\n      - run: echo b\n")
    p = tmp_path / "m.yml"
    p.write_text(yaml_text)
    graph = g.analyse_workflow(p)
    assert graph["jobs"]["b"]["needs"] == ["a"]
    assert graph["jobs"]["b"]["matrix"] is True
    assert graph["jobs"]["a"]["needs"] == []


def test_the_cli_is_nonzero_only_on_a_genuine_fail(tmp_path, capsys):
    # The two real workflows are NOT_YET_INTEGRATED → exit 0 (a valid B1 posture, not a failure).
    assert g.main([]) == 0
    # A genuinely broken integration → exit 1.
    broken = (
        "name: broken\non:\n  workflow_dispatch:\n"
        "jobs:\n  j:\n    runs-on: ubuntu-latest\n    steps:\n"
        "      - name: build\n        uses: docker/build-push-action@v6\n"
        "      - name: establish\n        run: python scripts/workflow_assurance.py establish\n"
        "      - name: push\n        run: docker push x\n")
    p = tmp_path / "broken.yml"
    p.write_text(broken)
    assert g.main([str(p)]) == 1
