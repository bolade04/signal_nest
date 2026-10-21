"""Offline tests for the delivery-workflow decision logic (P6-INF-2, P6-INF-7).

`scripts/release_plan.py` decides everything a release rests on from recorded AWS JSON; the
workflows only fetch and apply. These tests never call AWS and never open a socket: the
fixtures follow the documented response shapes of `ecs describe-task-definition --include
TAGS`, `ecs describe-services`, `ecs describe-tasks`, `ecr describe-images`, `s3api
head-object` and `ssm get-parameter-history`, and `smoke` is driven through an injected
fetcher.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import release_plan as rp  # noqa: E402

C1 = "1" * 40
C2 = "2" * 40
C3 = "3" * 40
D_OLD = "sha256:" + "a" * 64
D_API = "sha256:" + "b" * 64
D_WRK = "sha256:" + "c" * 64
D_API2 = "sha256:" + "d" * 64
D_WRK2 = "sha256:" + "e" * 64
REPO = "123456789012.dkr.ecr.us-east-1.amazonaws.com/signalnest-staging/api"  # synthetic id
FAMILY = "signalnest-staging-api"


# ------------------------------------------------------------------------------ fixtures


def described(revision: int, managed_by: str = "iac", release_commit: str | None = None,
              status: str = "ACTIVE", image_digest: str = D_OLD) -> dict:
    tags = [{"key": "ManagedBy", "value": managed_by}, {"key": "Environment", "value": "staging"},
            {"key": "Name", "value": FAMILY}]
    if release_commit:
        tags.append({"key": rp.RELEASE_TAG_COMMIT, "value": release_commit})
    return {
        "taskDefinition": {
            "taskDefinitionArn": f"arn:aws:ecs:us-east-1:123456789012:task-definition/{FAMILY}:{revision}",
            "family": FAMILY, "revision": revision, "status": status,
            "taskRoleArn": "arn:aws:iam::123456789012:role/signalnest-staging-api-task",
            "executionRoleArn": "arn:aws:iam::123456789012:role/signalnest-staging-execution",
            "networkMode": "awsvpc", "requiresCompatibilities": ["FARGATE"],
            "cpu": "256", "memory": "512",
            "runtimePlatform": {"operatingSystemFamily": "LINUX", "cpuArchitecture": "X86_64"},
            "volumes": [{"name": "tmp", "host": {}}],
            "placementConstraints": [],
            "containerDefinitions": [{
                "name": "api", "image": f"{REPO}@{image_digest}", "essential": True,
                "user": "10001:10001", "readonlyRootFilesystem": True,
                "portMappings": [{"containerPort": 8000, "hostPort": 8000, "protocol": "tcp"}],
                "environment": [{"name": "ENVIRONMENT", "value": "staging"}],
                "secrets": [{"name": "SECRET_KEY", "valueFrom": "arn:aws:secretsmanager:x"}],
                "mountPoints": [{"sourceVolume": "tmp", "containerPath": "/tmp", "readOnly": False}],
                "volumesFrom": [],
                "logConfiguration": {"logDriver": "awslogs", "options": {"awslogs-group": "/ecs/x"}},
            }],
            "requiresAttributes": [{"name": "com.amazonaws.ecs.capability.ecr-auth"}],
            "compatibilities": ["EC2", "FARGATE"],
            "registeredAt": "2026-10-20T00:00:00Z", "registeredBy": "arn:aws:sts::x",
        },
        "tags": tags,
    }


def service(workload: str, arn_rev: int, desired: int = 1, deployments: int = 1,
            rollout: str = "COMPLETED", rollback: bool = True) -> dict:
    return {
        "serviceName": f"signalnest-staging-{workload}", "status": "ACTIVE",
        "desiredCount": desired, "runningCount": desired, "pendingCount": 0,
        "taskDefinition": f"arn:aws:ecs:us-east-1:123456789012:task-definition/signalnest-staging-{workload}:{arn_rev}",
        "deploymentConfiguration": {"deploymentCircuitBreaker": {"enable": True, "rollback": rollback},
                                    "maximumPercent": 200, "minimumHealthyPercent": 100},
        "deployments": [{"status": "PRIMARY", "rolloutState": rollout}] * deployments,
    }


def tasks(workload: str, arn_rev: int, digest: str, count: int = 1) -> dict:
    arn = f"arn:aws:ecs:us-east-1:123456789012:task-definition/signalnest-staging-{workload}:{arn_rev}"
    return {"tasks": [{"lastStatus": "RUNNING", "taskDefinitionArn": arn,
                       "containers": [{"name": workload, "imageDigest": digest}]}] * count}


def services(api_rev=5, worker_rev=7, **kw) -> dict:
    return {"services": [service("api", api_rev, **kw), service("worker", worker_rev, **kw)],
            "failures": []}


def running_tasks(api_rev=5, worker_rev=7, api=D_API, worker=D_WRK) -> dict:
    return {"api": tasks("api", api_rev, api), "worker": tasks("worker", worker_rev, worker)}


def ledger(*records) -> dict:
    return {"Parameters": [{"Name": "/signalnest-staging/release/ledger", "Version": i + 1,
                            "Value": json.dumps(r)} for i, r in enumerate(records)]}


def record(kind, commit, api, worker, previous=None) -> dict:
    return {"schema": rp.LEDGER_SCHEMA, "kind": kind, "source_commit": commit,
            "commits": {"api": commit, "worker": commit},
            "images": {"api": api, "worker": worker}, "task_definitions": {},
            "previous": previous}


def summary(commit, api, worker) -> dict:
    return {"source_commit": commit, "commits": {"api": commit, "worker": commit},
            "images": {"api": api, "worker": worker}}


# ----------------------------------------------------------------------------------- SPA


def build(tmp_path: Path, extra: dict | None = None) -> Path:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>x</title>", encoding="utf-8")
    (dist / "favicon.svg").write_text("<svg/>", encoding="utf-8")
    (dist / "assets" / "index-AbC123_x.js").write_text("console.log(1)", encoding="utf-8")
    (dist / "assets" / "index-Zz9Yy8Xx.css").write_text("body{}", encoding="utf-8")
    for rel, text in (extra or {}).items():
        (dist / rel).parent.mkdir(parents=True, exist_ok=True)
        (dist / rel).write_text(text, encoding="utf-8")
    return dist


def test_spa_plan_classifies_and_uploads_index_last(tmp_path):
    plan = rp.spa_plan(build(tmp_path))
    kinds = {f["key"]: (f["kind"], f["cache_control"]) for f in plan["files"]}
    assert kinds["index.html"] == ("index", rp.INDEX_CACHE)
    assert kinds["assets/index-AbC123_x.js"] == ("immutable", rp.IMMUTABLE_CACHE)
    assert kinds["favicon.svg"] == ("short", rp.SHORT_CACHE)
    assert plan["upload_order"][-1] == "index.html"
    assert "index.html" not in "".join(rp.spa_upload_lines(plan))


def test_spa_plan_index_hash_is_the_bytes_hash(tmp_path):
    import hashlib
    dist = build(tmp_path)
    plan = rp.spa_plan(dist)
    assert plan["index_sha256"] == hashlib.sha256((dist / "index.html").read_bytes()).hexdigest()


@pytest.mark.parametrize("rel, reason", [
    ("assets/index-AbC123_x.js.map", "source map"),
    ("assets/logo.svg", "without a content hash"),
    (".env", "dotfile"),
    ("robots.unknownext", "no content type"),
    ("nested/thing.js", "unexpected nested path"),
])
def test_spa_plan_refuses_unsafe_files(tmp_path, rel, reason):
    with pytest.raises(rp.ReleaseError, match=reason):
        rp.spa_plan(build(tmp_path, {rel: "x"}))


def test_spa_plan_refuses_a_symlink(tmp_path):
    dist = build(tmp_path)
    (dist / "linked.svg").symlink_to(dist / "favicon.svg")
    with pytest.raises(rp.ReleaseError, match="symlink"):
        rp.spa_plan(dist)


def test_index_metadata_chains_to_the_live_index(tmp_path):
    plan = rp.spa_plan(build(tmp_path))
    live = {"Metadata": {"index-sha256": "f" * 64}}
    meta = rp.spa_index_metadata(plan, live, C1, "99")
    assert meta == {"source-commit": C1, "index-sha256": plan["index_sha256"],
                    "previous-index-sha256": "f" * 64, "release-run": "99"}
    assert rp.spa_index_metadata(plan, None, C1, "99")["previous-index-sha256"] == "none"


def spa_versions(*metas) -> list[dict]:
    return [{"VersionId": f"v{i}", "IsLatest": i == 0, "Metadata": m} for i, m in enumerate(metas)]


H1, H2, H3 = "1" * 64, "2" * 64, "3" * 64


def test_spa_rollback_walks_the_recorded_chain_not_the_version_order():
    # Publish H1, publish H2, publish H3: live H3 names H2; an older H1 copy sits in between.
    versions = spa_versions(
        {"index-sha256": H3, "previous-index-sha256": H2, "source-commit": C3},
        {"index-sha256": H1, "previous-index-sha256": "none", "source-commit": C1},
        {"index-sha256": H2, "previous-index-sha256": H1, "source-commit": C2},
    )
    target = rp.spa_rollback_target(versions, "7")
    assert target["target_version_id"] == "v2"
    assert target["metadata"]["index-sha256"] == H2
    assert target["metadata"]["previous-index-sha256"] == H1  # a second rollback continues to H1
    assert target["metadata"]["rolled-back-from"] == H3


def test_spa_second_rollback_does_not_toggle_back():
    # After rolling H3 -> H2 the live copy restates H2's lineage (previous H1), so the next
    # rollback goes to H1, never back to H3.
    versions = spa_versions(
        {"index-sha256": H2, "previous-index-sha256": H1, "source-commit": C2,
         "rolled-back-from": H3},
        {"index-sha256": H3, "previous-index-sha256": H2, "source-commit": C3},
        {"index-sha256": H2, "previous-index-sha256": H1, "source-commit": C2},
        {"index-sha256": H1, "previous-index-sha256": "none", "source-commit": C1},
    )
    assert rp.spa_rollback_target(versions, "8")["target_index_sha256"] == H1


@pytest.mark.parametrize("versions, reason", [
    ([], "no retained versions"),
    (spa_versions({"previous-index-sha256": H1}), "not written by the publish workflow"),
    (spa_versions({"index-sha256": H1, "previous-index-sha256": "none"}), "no predecessor"),
    (spa_versions({"index-sha256": H2, "previous-index-sha256": H1}), "no longer retained"),
])
def test_spa_rollback_refusals(versions, reason):
    with pytest.raises(rp.ReleaseError, match=reason):
        rp.spa_rollback_target(versions, "1")


# ----------------------------------------------------------------------------------- ECS


def test_template_is_the_newest_iac_revision_never_a_release():
    candidates = [described(9, managed_by="deploy-workflow", release_commit=C2),
                  described(8, status="INACTIVE"), described(7), described(6)]
    assert rp.select_template(candidates, FAMILY)["taskDefinition"]["revision"] == 7


def test_template_refuses_when_only_releases_exist():
    with pytest.raises(rp.ReleaseError, match="no ACTIVE OpenTofu-registered template"):
        rp.select_template([described(9, managed_by="deploy-workflow", release_commit=C2)], FAMILY)


def test_template_refuses_a_foreign_family():
    other = described(3)
    other["taskDefinition"]["family"] = "signalnest-staging-worker"
    with pytest.raises(rp.ReleaseError, match="not in family"):
        rp.select_template([other], FAMILY)


def test_render_changes_only_the_image_digest_and_tags():
    template = described(7)
    rendered = rp.render_task_definition(template, D_API, C1, "42")
    original = template["taskDefinition"]
    assert rendered["containerDefinitions"][0]["image"] == f"{REPO}@{D_API}"
    for key in ("taskRoleArn", "executionRoleArn", "cpu", "memory", "volumes", "family"):
        assert rendered[key] == original[key]
    container = dict(rendered["containerDefinitions"][0], image=None)
    assert container == dict(original["containerDefinitions"][0], image=None)
    for dropped in rp.SERVICE_ASSIGNED_MEMBERS:
        assert dropped not in rendered
    tags = {t["key"]: t["value"] for t in rendered["tags"]}
    assert tags["ManagedBy"] == "deploy-workflow"
    assert tags[rp.RELEASE_TAG_COMMIT] == C1
    assert tags[rp.RELEASE_TAG_TEMPLATE] == f"{FAMILY}:7"
    assert tags["Environment"] == "staging" and tags["Name"] == FAMILY
    # The template itself is untouched (no aliasing).
    assert original["containerDefinitions"][0]["image"] == f"{REPO}@{D_OLD}"


def test_render_refuses_an_unknown_member():
    template = described(7)
    template["taskDefinition"]["somethingNew"] = {"x": 1}
    with pytest.raises(rp.ReleaseError, match="does not know"):
        rp.render_task_definition(template, D_API, C1, "1")


@pytest.mark.parametrize("mutate, reason", [
    (lambda t: t["taskDefinition"]["containerDefinitions"].append({"image": f"{REPO}@{D_OLD}"}),
     "exactly one container"),
    (lambda t: t["taskDefinition"]["containerDefinitions"][0].update(image=f"{REPO}:latest"),
     "repository@sha256"),
])
def test_render_refuses_malformed_templates(mutate, reason):
    template = described(7)
    mutate(template)
    with pytest.raises(rp.ReleaseError, match=reason):
        rp.render_task_definition(template, D_API, C1, "1")


@pytest.mark.parametrize("digest, commit", [("latest", C1), (D_API, "main"), (D_API, C1[:12])])
def test_render_refuses_mutable_references(digest, commit):
    with pytest.raises(rp.ReleaseError):
        rp.render_task_definition(described(7), digest, commit, "1")


def test_running_release_reads_digests_and_revisions():
    running = rp.running_release(services(), running_tasks())
    assert running["images"] == {"api": D_API, "worker": D_WRK}
    assert running["task_definitions"] == {"api": "signalnest-staging-api:5",
                                           "worker": "signalnest-staging-worker:7"}


@pytest.mark.parametrize("svc, tsk, reason", [
    (services(deployments=2), running_tasks(), "deployment in progress"),
    (services(rollout="IN_PROGRESS"), running_tasks(), "did not complete"),
    (services(rollback=False), running_tasks(), "P6-AUTH-4 cutover"),
    (services(desired=0), {"api": {"tasks": []}, "worker": {"tasks": []}}, "desired count"),
    ({"services": [service("api", 5)], "failures": []}, running_tasks(), "not both present"),
    (services(), running_tasks(api_rev=4), "revision other than"),
])
def test_running_release_refuses_ambiguous_states(svc, tsk, reason):
    with pytest.raises(rp.ReleaseError, match=reason):
        rp.running_release(svc, tsk)


def test_running_release_refuses_mixed_digests():
    tsk = running_tasks()
    svc = services()
    svc["services"][0]["desiredCount"] = svc["services"][0]["runningCount"] = 2
    tsk["api"]["tasks"] = tasks("api", 5, D_API)["tasks"] + tasks("api", 5, D_API2)["tasks"]
    with pytest.raises(rp.ReleaseError, match="more than one image digest"):
        rp.running_release(svc, tsk)


def test_verify_rollout_requires_the_registered_revision_and_digest():
    expected = {"images": {"api": D_API, "worker": D_WRK},
                "task_definitions": {"api": "signalnest-staging-api:5",
                                     "worker": "signalnest-staging-worker:7"}}
    rp.verify_rollout(services(), running_tasks(), expected)
    with pytest.raises(rp.ReleaseError, match="target digest"):
        rp.verify_rollout(services(), running_tasks(api=D_API2), expected)
    with pytest.raises(rp.ReleaseError, match="registered revision"):
        rp.verify_rollout(services(api_rev=6), running_tasks(api_rev=6), expected)


def ecr_image(tags, digest=D_API, scan="COMPLETE", counts=None, repository="signalnest-staging/api"):
    return {"imageDetails": [{"repositoryName": repository, "imageDigest": digest,
                              "imageTags": tags, "imageScanStatus": {"status": scan},
                              "imageScanFindingsSummary": {"findingSeverityCounts": counts or {}}}]}


def test_resolve_target_by_immutable_commit_tag():
    out = rp.resolve_target(ecr_image([C1], counts={"HIGH": 2}), "signalnest-staging/api", C1)
    assert out == {"digest": D_API, "scan_counts": {"HIGH": 2}}


@pytest.mark.parametrize("image, reason", [
    ({"imageDetails": []}, "run the staging publish workflow"),
    (ecr_image([C2]), "not tagged with the release commit"),
    (ecr_image([C1], scan="IN_PROGRESS"), "not COMPLETE"),
    (ecr_image([C1], counts={"CRITICAL": 1}), "CRITICAL"),
])
def test_resolve_target_refusals(image, reason):
    with pytest.raises(rp.ReleaseError, match=reason):
        rp.resolve_target(image, "signalnest-staging/api", C1)


def test_commit_for_digest_needs_exactly_one_commit_tag():
    assert rp.commit_for_digest(ecr_image([C1, "other"]), "signalnest-staging/api") == C1
    with pytest.raises(rp.ReleaseError, match="exactly one commit tag"):
        rp.commit_for_digest(ecr_image([C1, C2]), "signalnest-staging/api")


# -------------------------------------------------------------------------------- ledger


TARGET = {"source_commit": C2, "images": {"api": D_API2, "worker": D_WRK2},
          "task_definitions": {"api": "signalnest-staging-api:6", "worker": "signalnest-staging-worker:8"}}
RUNNING = {"images": {"api": D_API, "worker": D_WRK}, "task_definitions": {}}


def test_deploy_records_what_was_actually_running():
    records = rp.ledger_records(ledger(record("genesis", C1, "", "")))
    out = rp.ledger_deploy(records, RUNNING, {"api": C1, "worker": C1}, TARGET, "11",
                           "2026-10-20T00:00:00Z")
    assert out["previous"] == summary(C1, D_API, D_WRK)
    assert out["commits"] == {"api": C2, "worker": C2}
    assert out["images"] == TARGET["images"] and out["ledger_drift_before"] is False


def test_deploy_notes_ledger_drift_after_a_window_reopen():
    records = rp.ledger_records(ledger(record("deploy", C3, D_API2, D_WRK2)))
    out = rp.ledger_deploy(records, RUNNING, {"api": C1, "worker": C1}, TARGET, "11", "t")
    assert out["ledger_drift_before"] is True
    assert out["previous"]["images"] == RUNNING["images"]  # truth, not stale history


def test_a_half_rolled_back_deploy_is_recorded_per_workload_and_stays_restorable():
    # The breaker rolled the api back to C1 while the worker reached C2: no single commit
    # runs. The next deploy must record that state, and a rollback must restore it exactly.
    mixed_running = {"images": {"api": D_API, "worker": D_WRK2}, "task_definitions": {}}
    records = rp.ledger_records(ledger(record("deploy", C1, D_API, D_WRK)))
    out = rp.ledger_deploy(records, mixed_running, {"api": C1, "worker": C2},
                           {**TARGET, "source_commit": C3}, "12", "t")
    assert out["previous"]["source_commit"] is None
    assert out["previous"]["commits"] == {"api": C1, "worker": C2}
    after = rp.ledger_records(ledger(record("deploy", C1, D_API, D_WRK), out))
    plan = rp.rollback_target(after, {"images": TARGET["images"]})
    assert plan["target"]["commits"] == {"api": C1, "worker": C2}
    assert plan["target"]["images"] == {"api": D_API, "worker": D_WRK2}
    rolled = rp.ledger_rollback(plan, {"api": "a:1", "worker": "w:1"}, "13", "t")
    assert rolled["commits"] == {"api": C1, "worker": C2} and rolled["source_commit"] is None


def test_deploy_refuses_an_unresolved_running_commit():
    with pytest.raises(rp.ReleaseError, match="one 40-hex commit per workload"):
        rp.ledger_deploy([], RUNNING, {"api": C1, "worker": "unknown"}, TARGET, "1", "t")


def test_rollback_targets_the_recorded_predecessor_and_keeps_lineage():
    first = record("deploy", C1, D_API, D_WRK, previous=summary(C3, D_OLD, D_OLD))
    second = record("deploy", C2, D_API2, D_WRK2, previous=summary(C1, D_API, D_WRK))
    records = rp.ledger_records(ledger(first, second))
    plan = rp.rollback_target(records, {"images": {"api": D_API2, "worker": D_WRK2}})
    assert plan["target"] == summary(C1, D_API, D_WRK)
    assert plan["target_previous"]["source_commit"] == C3
    rolled = rp.ledger_rollback(plan, {"api": "a:9", "worker": "w:9"}, "12", "t")
    # A second rollback from here goes to C3, not back to C2.
    after = rp.ledger_records(ledger(first, second, rolled))
    again = rp.rollback_target(after, {"images": {"api": D_API, "worker": D_WRK}})
    assert again["target"]["source_commit"] == C3


@pytest.mark.parametrize("records, running, reason", [
    ([], RUNNING, "no release to roll back"),
    ([record("genesis", C1, "", "")], RUNNING, "no release to roll back"),
    ([record("deploy", C2, D_API2, D_WRK2)], RUNNING, "ambiguous"),
    ([record("deploy", C2, D_API, D_WRK)], RUNNING, "no predecessor"),
])
def test_rollback_refusals(records, running, reason):
    with pytest.raises(rp.ReleaseError, match=reason):
        rp.rollback_target(rp.ledger_records(ledger(*records)), running)


def test_ledger_refuses_unknown_schema():
    bad = {"Parameters": [{"Version": 1, "Value": json.dumps({"schema": "other"})}]}
    with pytest.raises(rp.ReleaseError, match="unknown schema"):
        rp.ledger_records(bad)


# --------------------------------------------------------------------------------- smoke


GOOD_HEADERS = {**rp.API_SECURITY_HEADERS,
                "content-security-policy": "default-src 'none'; frame-ancestors 'none'"}


def fake_fetch(overrides=None):
    answers = {
        "/health": {"status": 200, "body": '{"status":"ok","mode":"full"}', "headers": GOOD_HEADERS},
        "/api/v1/system/readiness": {"status": 200, "headers": GOOD_HEADERS,
                                     "body": '{"ready":true,"schema_migrated":true}'},
        "/api/v1/release-smoke-no-such-route": {"status": 404, "body": "{}", "headers": GOOD_HEADERS},
    }
    answers.update(overrides or {})

    def fetch(url):
        return copy.deepcopy(answers[url.split("example.test", 1)[1]])
    return fetch


def test_smoke_passes_on_a_healthy_api():
    assert rp.smoke("https://api.example.test", fetch=fake_fetch())["passed"]


@pytest.mark.parametrize("override, reason", [
    ({"/health": {"status": 503, "body": "", "headers": {}}}, "/health answered 503"),
    ({"/api/v1/system/readiness": {"status": 200, "headers": {},
                                   "body": '{"ready":true,"schema_migrated":false}'}},
     "schema_migrated"),
    ({"/health": {"status": 200, "body": '{"status":"ok"}',
                  "headers": {**GOOD_HEADERS, "x-frame-options": "SAMEORIGIN"}}}, "x-frame-options"),
])
def test_smoke_failures(override, reason):
    result = rp.smoke("https://api.example.test", fetch=fake_fetch(override))
    assert not result["passed"]
    assert any(reason in p for p in result["problems"])


@pytest.mark.parametrize("origin", ["http://api.example.test", "https://api.example.test/x",
                                    "https://API.example.test"])
def test_smoke_refuses_a_malformed_origin(origin):
    with pytest.raises(rp.ReleaseError):
        rp.smoke(origin, fetch=fake_fetch())


def test_module_imports_no_process_or_aws_client():
    """The helper decides; it never launches a process or holds an AWS client."""
    source = (REPO_ROOT / "scripts" / "release_plan.py").read_text(encoding="utf-8")
    for forbidden in ("import subprocess", "import boto3", "from subprocess", "os.system"):
        assert forbidden not in source
