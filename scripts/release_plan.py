#!/usr/bin/env python3
"""Offline decision logic for the staging delivery workflows (P6-INF-2, P6-INF-7).

The four delivery workflows — `spa-publish.yml`, `spa-rollback.yml`, `staging-deploy.yml`
and `staging-rollback.yml` — make every AWS call themselves, through the AWS CLI, and hand
the JSON they read to this module. This module never calls AWS, never opens a socket
except in `smoke` (which reads the environment's own public health endpoints), and never
launches a process. Every decision a release rests on is therefore a pure function of
recorded JSON, testable offline against fixtures (tests/test_p6_release_plan.py) — the
same split as `cloudfront_precheck.evaluate()`.

WHAT A RELEASE IS. Two independently reversible halves:

  SPA   the built `apps/web/dist` tree in the private SPA origin bucket. Content-hashed
        assets are immutable and never deleted; `index.html` is the single pointer that
        selects a build. Its object metadata carries the build's own sha256 and the sha256
        of the index it replaced, so a rollback walks a recorded chain instead of guessing
        "the previous version" (an S3 version list alone cannot tell a rollback from a
        publish). Noncurrent versions expire after 30 days (modules/edge lifecycle), which
        bounds the SPA rollback horizon.

  API   the api and worker ECS services. OpenTofu registers a TEMPLATE task-definition
        revision per family (tag ManagedBy=iac, from default_tags); a release registers a
        new revision that is the template with exactly one field changed — the container
        image digest — and moves the service to it. Nothing else in the task definition
        can differ, so the IaC review of environment, secrets, roles and sizing remains
        the review of what runs. The ledger of releases is one SSM parameter whose version
        history survives staging windows (task definitions do not: they are destroyed at
        every window close).

FAIL CLOSED. Every function raises `ReleaseError` with a fixed, value-free message on any
shape it does not recognise. A refusal is always the safe direction: a refused release
leaves the previous one running.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

LEDGER_SCHEMA = "signalnest-release-ledger/1"

SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
# Vite names every emitted asset `<name>-<hash>.<ext>`; the hash is 8 url-safe characters.
HASHED_ASSET_RE = re.compile(r"^assets/[A-Za-z0-9_.-]+-[A-Za-z0-9_-]{8,}\.[a-z0-9]+$")

IMMUTABLE_CACHE = "public, max-age=31536000, immutable"
SHORT_CACHE = "public, max-age=3600"
INDEX_CACHE = "no-cache"

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".webp": "image/webp",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    ".json": "application/json",
    ".txt": "text/plain; charset=utf-8",
    ".webmanifest": "application/manifest+json",
}

# RegisterTaskDefinition accepts exactly these members (AWS ECS API Reference). Everything
# DescribeTaskDefinition returns beyond them is service-assigned and is dropped; a member in
# NEITHER set is unknown, and an unknown member is refused rather than silently lost.
REGISTER_MEMBERS = frozenset({
    "family", "taskRoleArn", "executionRoleArn", "networkMode", "containerDefinitions",
    "volumes", "placementConstraints", "requiresCompatibilities", "cpu", "memory", "pidMode",
    "ipcMode", "proxyConfiguration", "inferenceAccelerators", "ephemeralStorage",
    "runtimePlatform", "enableFaultInjection",
})
SERVICE_ASSIGNED_MEMBERS = frozenset({
    "taskDefinitionArn", "revision", "status", "requiresAttributes", "compatibilities",
    "registeredAt", "registeredBy", "deregisteredAt", "deleteRequestedAt",
})

RELEASE_TAG_COMMIT = "signalnest:release-commit"
RELEASE_TAG_RUN = "signalnest:release-run"
RELEASE_TAG_TEMPLATE = "signalnest:template-revision"
MANAGED_BY = "ManagedBy"
WORKLOADS = ("api", "worker")

# The six headers P6-AUTH-5 sets on every API response (docs/operations/deployment.md).
API_SECURITY_HEADERS = {
    "strict-transport-security": "max-age=31536000",
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "no-referrer",
    "cache-control": "no-store",
}


class ReleaseError(Exception):
    """A refusal. The message is fixed text plus non-secret identifiers only."""


# ---------------------------------------------------------------------------------- SPA


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def spa_plan(dist: Path) -> dict:
    """Classify every file of a built SPA for upload. Refuses anything it cannot cache safely.

    `index.html` is uploaded LAST by the workflow and is never cached by CloudFront beyond a
    revalidation; hashed assets are immutable; the few un-hashed root files (the favicon) get
    a short cache. A source map, a dotfile, a symlink, an un-hashed file under assets/ or an
    unknown extension is refused: each would either publish something not meant to be public
    or pin a stale object in browsers for a year.
    """
    dist = dist.resolve()
    if not dist.is_dir():
        raise ReleaseError("the SPA build directory does not exist")
    index = dist / "index.html"
    if not index.is_file() or index.is_symlink():
        raise ReleaseError("the SPA build has no regular index.html at its root")

    files = []
    for path in sorted(p for p in dist.rglob("*")):
        rel = path.relative_to(dist).as_posix()
        if path.is_symlink():
            raise ReleaseError(f"refusing a symlink in the SPA build: {rel}")
        if path.is_dir():
            continue
        if any(part.startswith(".") for part in rel.split("/")):
            raise ReleaseError(f"refusing a dotfile in the SPA build: {rel}")
        if rel.endswith(".map"):
            raise ReleaseError(f"refusing a source map in the SPA build: {rel}")
        suffix = path.suffix.lower()
        if suffix not in CONTENT_TYPES:
            raise ReleaseError(f"no content type is declared for {rel}")
        if rel == "index.html":
            kind, cache = "index", INDEX_CACHE
        elif rel.startswith("assets/"):
            if not HASHED_ASSET_RE.match(rel):
                raise ReleaseError(f"an asset without a content hash cannot be immutable: {rel}")
            kind, cache = "immutable", IMMUTABLE_CACHE
        elif "/" in rel:
            raise ReleaseError(f"unexpected nested path outside assets/: {rel}")
        else:
            kind, cache = "short", SHORT_CACHE
        files.append({
            "key": rel, "kind": kind, "content_type": CONTENT_TYPES[suffix],
            "cache_control": cache, "bytes": path.stat().st_size,
            "sha256": _sha256_file(path),
        })

    index_entry = next(f for f in files if f["key"] == "index.html")
    return {
        "index_sha256": index_entry["sha256"],
        "files": files,
        "upload_order": [f["key"] for f in files if f["kind"] != "index"] + ["index.html"],
        "count": len(files),
        "total_bytes": sum(f["bytes"] for f in files),
    }


def spa_upload_lines(plan: dict) -> list[str]:
    """Tab-separated `key, content type, cache control` for every non-index file, in order."""
    by_key = {f["key"]: f for f in plan["files"]}
    lines = []
    for key in plan["upload_order"]:
        entry = by_key[key]
        if entry["kind"] == "index":
            continue
        for value in (key, entry["content_type"], entry["cache_control"]):
            if "\t" in value or "\n" in value:
                raise ReleaseError("a planned upload field contains a tab or newline")
        lines.append(f"{key}\t{entry['content_type']}\t{entry['cache_control']}")
    return lines


def _index_metadata(head: dict) -> dict:
    meta = head.get("Metadata") or {}
    return {k.lower(): v for k, v in meta.items()}


def spa_index_metadata(plan: dict, current_head: dict | None, commit: str, run_id: str) -> dict:
    """The object metadata the new index.html carries: its own hash and the one it replaces."""
    if not SHA1_RE.match(commit):
        raise ReleaseError("the source commit is not a 40-character hex SHA")
    previous = ""
    if current_head is not None:
        previous = _index_metadata(current_head).get("index-sha256", "")
        if previous and not HEX64_RE.match(previous):
            raise ReleaseError("the live index.html carries a malformed index-sha256")
    return {
        "source-commit": commit,
        "index-sha256": plan["index_sha256"],
        # Empty when the live index was not written by this workflow (first publish): a
        # rollback from that release has nowhere recorded to go, and says so.
        "previous-index-sha256": previous or "none",
        "release-run": str(run_id),
    }


def spa_rollback_target(versions: list[dict], run_id: str) -> dict:
    """Pick the build the live index.html replaced, from recorded metadata only.

    `versions` is newest-first: [{"VersionId", "IsLatest", "Metadata"}] — one entry per
    retained version of the single key `index.html`, each from `s3api head-object
    --version-id`. The current version must name its predecessor (`previous-index-sha256`);
    the target is the newest NONCURRENT version whose own `index-sha256` equals it. The new
    object written by the rollback re-states the target's lineage, so a second rollback keeps
    walking back instead of toggling between two builds.
    """
    if not versions:
        raise ReleaseError("index.html has no retained versions")
    latest = [v for v in versions if v.get("IsLatest")]
    if len(latest) != 1 or versions[0] is not latest[0]:
        raise ReleaseError("expected exactly one current index.html version, listed first")
    current = _index_metadata(latest[0])
    wanted = current.get("previous-index-sha256", "")
    if not HEX64_RE.match(current.get("index-sha256", "")):
        raise ReleaseError("the live index.html was not written by the publish workflow")
    if not HEX64_RE.match(wanted):
        raise ReleaseError("the live index.html records no predecessor build to roll back to")
    for version in versions[1:]:
        meta = _index_metadata(version)
        if meta.get("index-sha256") == wanted:
            if not SHA1_RE.match(meta.get("source-commit", "")):
                raise ReleaseError("the predecessor version carries no source commit")
            return {
                "target_version_id": version["VersionId"],
                "target_index_sha256": wanted,
                "target_source_commit": meta["source-commit"],
                "rolled_back_from_index_sha256": current["index-sha256"],
                "metadata": {
                    "source-commit": meta["source-commit"],
                    "index-sha256": wanted,
                    "previous-index-sha256": meta.get("previous-index-sha256", "none"),
                    "rolled-back-from": current["index-sha256"],
                    "release-run": str(run_id),
                },
            }
    raise ReleaseError("the predecessor build is no longer retained (noncurrent versions "
                       "expire after 30 days); republish it from its commit instead")


# ---------------------------------------------------------------------------------- ECS


def _tags(described: dict) -> dict:
    return {t["key"]: t["value"] for t in described.get("tags") or []}


def select_template(candidates: list[dict], family: str) -> dict:
    """The newest ACTIVE revision OpenTofu registered for `family` (tag ManagedBy=iac).

    `candidates` is newest-first, each a `describe-task-definition --include TAGS` output. A
    revision this workflow registered carries ManagedBy=deploy-workflow and a release-commit
    tag and is never a template: basing a release on a previous release would let one bad
    release's task definition outlive the IaC that should have replaced it.
    """
    for described in candidates:
        td = described.get("taskDefinition") or {}
        if td.get("family") != family:
            raise ReleaseError(f"a candidate revision is not in family {family}")
        if td.get("status") != "ACTIVE":
            continue
        tags = _tags(described)
        if tags.get(MANAGED_BY) == "iac" and RELEASE_TAG_COMMIT not in tags:
            return described
    raise ReleaseError(f"no ACTIVE OpenTofu-registered template revision for {family} "
                       "(is the workload stage applied in an open window?)")


def _split_image(image: str) -> tuple[str, str]:
    repo, sep, digest = image.partition("@")
    if not sep or not repo or not DIGEST_RE.match(digest):
        raise ReleaseError("an image reference is not repository@sha256:<digest>")
    return repo, digest


def render_task_definition(template: dict, digest: str, commit: str, run_id: str) -> dict:
    """RegisterTaskDefinition input: the template with only the image digest replaced."""
    if not DIGEST_RE.match(digest):
        raise ReleaseError("the target digest is not sha256:<64 hex>")
    if not SHA1_RE.match(commit):
        raise ReleaseError("the release commit is not a 40-character hex SHA")
    td = template.get("taskDefinition") or {}
    unknown = set(td) - REGISTER_MEMBERS - SERVICE_ASSIGNED_MEMBERS
    if unknown:
        raise ReleaseError(f"the template carries members this renderer does not know: "
                           f"{sorted(unknown)}")
    containers = td.get("containerDefinitions") or []
    if len(containers) != 1:
        raise ReleaseError("a template must define exactly one container")
    repo, _old = _split_image(containers[0].get("image", ""))

    rendered = {k: json.loads(json.dumps(v)) for k, v in td.items() if k in REGISTER_MEMBERS}
    rendered["containerDefinitions"][0]["image"] = f"{repo}@{digest}"

    tags = {k: v for k, v in _tags(template).items() if k != MANAGED_BY}
    tags.update({
        MANAGED_BY: "deploy-workflow",
        RELEASE_TAG_COMMIT: commit,
        RELEASE_TAG_RUN: str(run_id),
        RELEASE_TAG_TEMPLATE: f"{td['family']}:{td['revision']}",
    })
    rendered["tags"] = [{"key": k, "value": tags[k]} for k in sorted(tags)]

    # The guarantee, asserted rather than assumed: image aside, nothing differs.
    before = {k: v for k, v in td.items() if k in REGISTER_MEMBERS}
    after = json.loads(json.dumps({k: v for k, v in rendered.items() if k != "tags"}))
    after["containerDefinitions"][0]["image"] = containers[0]["image"]
    if _canonical(before) != _canonical(after):
        raise ReleaseError("the rendered task definition differs from its template beyond the image")
    return rendered


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def running_release(services: dict, tasks: dict) -> dict:
    """The digests and task definitions actually serving, or a refusal if that is ambiguous.

    `services` is `describe-services` for exactly the api and worker services (in either
    order, each named `<prefix>-<workload>`); `tasks` maps workload -> `describe-tasks` for
    that service's running tasks. A service mid-deployment, scaled to zero, or with tasks on
    mixed digests has no single "running release", and no release may start from it.
    """
    out = {"images": {}, "task_definitions": {}, "desired": {}}
    by_workload = {}
    for svc in services.get("services") or []:
        name = svc.get("serviceName", "")
        workload = name.rsplit("-", 1)[-1]
        if workload in WORKLOADS:
            by_workload[workload] = svc
    if set(by_workload) != set(WORKLOADS) or services.get("failures"):
        raise ReleaseError("the api and worker services are not both present "
                           "(window closed or workload stage not applied)")
    for workload in WORKLOADS:
        svc = by_workload[workload]
        deployments = svc.get("deployments") or []
        if svc.get("status") != "ACTIVE" or len(deployments) != 1:
            raise ReleaseError(f"the {workload} service has a deployment in progress")
        if deployments[0].get("rolloutState") != "COMPLETED":
            raise ReleaseError(f"the {workload} service's last deployment did not complete")
        breaker = ((svc.get("deploymentConfiguration") or {})
                   .get("deploymentCircuitBreaker") or {})
        if not (breaker.get("enable") and breaker.get("rollback")):
            # P6-AUTH-4 cutover Apply 1/2 run with rollback off; Apply 3 restores it. A release
            # in between would race the cutover's own sequence.
            raise ReleaseError(f"the {workload} service has circuit-breaker rollback off "
                               "(the P6-AUTH-4 cutover is not finished)")
        desired = svc.get("desiredCount", 0)
        if desired < 1 or svc.get("runningCount") != desired:
            raise ReleaseError(f"the {workload} service is not at its desired count (or is at 0)")
        arn = svc.get("taskDefinition", "")
        digests = set()
        running = [t for t in (tasks.get(workload, {}).get("tasks") or [])
                   if t.get("lastStatus") == "RUNNING"]
        if len(running) != desired:
            raise ReleaseError(f"the {workload} service's running tasks do not match its count")
        for task in running:
            if task.get("taskDefinitionArn") != arn:
                raise ReleaseError(f"a {workload} task runs a revision other than the service's")
            containers = task.get("containers") or []
            if len(containers) != 1 or not DIGEST_RE.match(containers[0].get("imageDigest", "")):
                raise ReleaseError(f"a {workload} task reports no single image digest")
            digests.add(containers[0]["imageDigest"])
        if len(digests) != 1:
            raise ReleaseError(f"the {workload} service runs more than one image digest")
        out["images"][workload] = digests.pop()
        out["task_definitions"][workload] = arn.rsplit("/", 1)[-1]
        out["desired"][workload] = desired
    return out


def verify_rollout(services: dict, tasks: dict, expected: dict) -> dict:
    """After the update: the services run exactly the registered revisions and digests."""
    running = running_release(services, tasks)
    for workload in WORKLOADS:
        if running["images"][workload] != expected["images"][workload]:
            raise ReleaseError(f"the {workload} service does not run the target digest")
        if running["task_definitions"][workload] != expected["task_definitions"][workload]:
            raise ReleaseError(f"the {workload} service does not run the registered revision")
    return running


def commit_for_digest(described_images: dict, repository: str) -> str:
    """The commit an image was published for: its one 40-hex tag (staging-publish tags by SHA).

    ECR repositories are IMMUTABLE-tag (modules/registry), so a commit tag, once pushed,
    names exactly one digest forever.
    """
    details = described_images.get("imageDetails") or []
    if len(details) != 1 or details[0].get("repositoryName") != repository:
        raise ReleaseError(f"expected exactly one image record in {repository}")
    commits = [t for t in details[0].get("imageTags") or [] if SHA1_RE.match(t)]
    if len(commits) != 1:
        raise ReleaseError(f"an image in {repository} does not carry exactly one commit tag")
    return commits[0]


def resolve_target(described: dict, repository: str, commit: str) -> dict:
    """The digest published for `commit`, refused if its push-time scan reports CRITICAL."""
    details = described.get("imageDetails") or []
    if len(details) != 1 or details[0].get("repositoryName") != repository:
        raise ReleaseError(f"no image tagged with the release commit in {repository} "
                           "(run the staging publish workflow for this commit first)")
    image = details[0]
    if commit not in (image.get("imageTags") or []):
        raise ReleaseError(f"the {repository} image is not tagged with the release commit")
    digest = image.get("imageDigest", "")
    if not DIGEST_RE.match(digest):
        raise ReleaseError(f"the {repository} image has no sha256 digest")
    summary = image.get("imageScanFindingsSummary") or {}
    status = (image.get("imageScanStatus") or {}).get("status")
    counts = summary.get("findingSeverityCounts") or {}
    if status != "COMPLETE":
        raise ReleaseError(f"the {repository} push-time scan is not COMPLETE ({status})")
    if counts.get("CRITICAL", 0):
        raise ReleaseError(f"the {repository} push-time scan reports CRITICAL findings")
    return {"digest": digest, "scan_counts": dict(sorted(counts.items()))}


# ------------------------------------------------------------------------------- ledger


def ledger_records(history: dict) -> list[dict]:
    """Every ledger record, oldest first, from `ssm get-parameter-history` (all pages merged)."""
    records = []
    for param in history.get("Parameters") or []:
        try:
            value = json.loads(param.get("Value", ""))
        except json.JSONDecodeError as exc:
            raise ReleaseError("a ledger version is not JSON") from exc
        if value.get("schema") != LEDGER_SCHEMA:
            raise ReleaseError("a ledger version carries an unknown schema")
        value["_version"] = param.get("Version")
        records.append(value)
    records.sort(key=lambda r: r["_version"])
    return records


def _commits(commits: dict) -> dict:
    if set(commits) != set(WORKLOADS) or not all(SHA1_RE.match(commits[w] or "") for w in WORKLOADS):
        raise ReleaseError("a release must name one 40-hex commit per workload (api, worker)")
    return {w: commits[w] for w in WORKLOADS}


def _summary(record: dict) -> dict:
    """What a rollback needs to restore one release: a commit and a digest per workload.

    Per-workload, because a deployment can end with the two services on different commits
    (the circuit breaker rolls one back and not the other). Recording that state truthfully
    keeps it recoverable; refusing to record it would deadlock both workflows.
    """
    commits = _commits(record["commits"])
    single = commits["api"] if commits["api"] == commits["worker"] else None
    return {"source_commit": single, "commits": commits, "images": dict(record["images"])}


def ledger_deploy(records: list[dict], running: dict, running_commits: dict, target: dict,
                  run_id: str, recorded_utc: str) -> dict:
    """The ledger record a deploy writes. `previous` is what was ACTUALLY running.

    The running release is read from ECS, never from the ledger: after a window close and
    reopen, OpenTofu re-creates the services from the tfvars digests, which may predate the
    last recorded release. The record notes that drift instead of trusting stale history.
    """
    previous = _summary({"commits": running_commits, "images": running["images"]})
    latest = records[-1] if records else None
    drift = bool(latest and latest.get("kind") != "genesis"
                 and latest["images"] != running["images"])
    if not SHA1_RE.match(target.get("source_commit") or ""):
        raise ReleaseError("a deploy targets exactly one 40-hex commit")
    commit = target["source_commit"]
    return {
        "schema": LEDGER_SCHEMA, "kind": "deploy",
        "source_commit": commit, "commits": {w: commit for w in WORKLOADS},
        "images": dict(target["images"]), "task_definitions": dict(target["task_definitions"]),
        "previous": previous,
        "ledger_drift_before": drift, "run_id": str(run_id), "recorded_utc": recorded_utc,
    }


def rollback_target(records: list[dict], running: dict) -> dict:
    """The release to restore: the latest record's `previous`, provided it is what runs now.

    Refuses when the services no longer run what the ledger last recorded (a window reopen,
    a manual apply): "the prior digest" is then ambiguous, and the safe recovery is the IaC
    path with explicit digests rather than a guess.
    """
    if not records or records[-1].get("kind") == "genesis":
        raise ReleaseError("the ledger records no release to roll back from")
    latest = records[-1]
    if latest["images"] != running["images"]:
        raise ReleaseError("the running digests differ from the latest ledger record; "
                           "the prior release is ambiguous")
    previous = latest.get("previous")
    if not previous:
        raise ReleaseError("the latest release records no predecessor")
    if previous["images"] == running["images"]:
        raise ReleaseError("the predecessor is the release already running")
    # The predecessor's own predecessor, so that a second rollback walks further back.
    lineage = None
    for record in reversed(records[:-1]):
        if record.get("kind") in ("deploy", "rollback") and record["images"] == previous["images"]:
            lineage = record.get("previous")
            break
    return {"target": _summary(previous), "rolled_back_from": _summary(latest),
            "target_previous": lineage}


def ledger_rollback(plan: dict, task_definitions: dict, run_id: str, recorded_utc: str) -> dict:
    target = plan["target"]
    return {
        "schema": LEDGER_SCHEMA, "kind": "rollback",
        "source_commit": target["source_commit"], "commits": _commits(target["commits"]),
        "images": dict(target["images"]),
        "task_definitions": dict(task_definitions), "previous": plan["target_previous"],
        "rolled_back_from": plan["rolled_back_from"], "run_id": str(run_id),
        "recorded_utc": recorded_utc,
    }


# -------------------------------------------------------------------------------- smoke


def evaluate_smoke(responses: dict) -> dict:
    """Judge recorded responses: {name: {"status", "headers" (lower-cased), "body"}}."""
    problems = []
    health = responses.get("health") or {}
    if health.get("status") != 200:
        problems.append(f"/health answered {health.get('status')}")
    else:
        try:
            if json.loads(health.get("body") or "{}").get("status") != "ok":
                problems.append("/health body is not status ok")
        except json.JSONDecodeError:
            problems.append("/health body is not JSON")
    headers = health.get("headers") or {}
    for name, value in API_SECURITY_HEADERS.items():
        if headers.get(name) != value:
            problems.append(f"/health header {name} is not the P6-AUTH-5 value")
    if "frame-ancestors 'none'" not in headers.get("content-security-policy", ""):
        problems.append("/health carries no frame-ancestors 'none' policy")
    ready = responses.get("readiness") or {}
    if ready.get("status") != 200:
        problems.append(f"/api/v1/system/readiness answered {ready.get('status')}")
    else:
        try:
            body = json.loads(ready.get("body") or "{}")
        except json.JSONDecodeError:
            body = {}
        if body.get("ready") is not True or body.get("schema_migrated") is not True:
            problems.append("readiness does not report ready and schema_migrated")
    missing = responses.get("not_found") or {}
    if missing.get("status") != 404:
        problems.append(f"an unknown route answered {missing.get('status')}, expected 404")
    elif (missing.get("headers") or {}).get("x-frame-options") != "DENY":
        problems.append("the 404 answer lacks the P6-AUTH-5 headers")
    return {"passed": not problems, "problems": problems,
            "observed": {k: v.get("status") for k, v in responses.items()}}


def _fetch(url: str, timeout: float = 10.0) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": "signalnest-release-smoke"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return {"status": response.status, "body": response.read(65536).decode("utf-8", "replace"),
                    "headers": {k.lower(): v for k, v in response.headers.items()}}
    except urllib.error.HTTPError as exc:
        return {"status": exc.code, "body": exc.read(65536).decode("utf-8", "replace"),
                "headers": {k.lower(): v for k, v in exc.headers.items()}}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {"status": None, "body": "", "headers": {}, "error": type(exc).__name__}


def smoke(api_origin: str, fetch=_fetch) -> dict:
    if not re.fullmatch(r"https://[a-z0-9.-]+", api_origin):
        raise ReleaseError("the API origin must be https://<lowercase host> with no path")
    return evaluate_smoke({
        "health": fetch(f"{api_origin}/health"),
        "readiness": fetch(f"{api_origin}/api/v1/system/readiness"),
        "not_found": fetch(f"{api_origin}/api/v1/release-smoke-no-such-route"),
    })


# ---------------------------------------------------------------------------------- CLI


def _load(path: str):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _dump(value, path: str | None) -> None:
    text = json.dumps(value, indent=2, sort_keys=True) + "\n"
    if path:
        Path(path).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("spa-plan")
    p.add_argument("--dist", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--upload-list", required=True)

    p = sub.add_parser("spa-index-metadata")
    p.add_argument("--plan", required=True)
    p.add_argument("--current-head")
    p.add_argument("--commit", required=True)
    p.add_argument("--run-id", required=True)
    p.add_argument("--out", required=True)

    p = sub.add_parser("spa-rollback-target")
    p.add_argument("--versions", required=True)
    p.add_argument("--run-id", required=True)
    p.add_argument("--out", required=True)

    p = sub.add_parser("ecs-template")
    p.add_argument("--candidates", required=True)
    p.add_argument("--family", required=True)
    p.add_argument("--out", required=True)

    p = sub.add_parser("ecs-render")
    p.add_argument("--template", required=True)
    p.add_argument("--digest", required=True)
    p.add_argument("--commit", required=True)
    p.add_argument("--run-id", required=True)
    p.add_argument("--out", required=True)

    p = sub.add_parser("ecs-running")
    p.add_argument("--services", required=True)
    p.add_argument("--api-tasks", required=True)
    p.add_argument("--worker-tasks", required=True)
    p.add_argument("--out", required=True)

    p = sub.add_parser("ecs-verify")
    p.add_argument("--services", required=True)
    p.add_argument("--api-tasks", required=True)
    p.add_argument("--worker-tasks", required=True)
    p.add_argument("--expected", required=True)

    p = sub.add_parser("image-commit")
    p.add_argument("--described", required=True)
    p.add_argument("--repository", required=True)

    p = sub.add_parser("image-target")
    p.add_argument("--described", required=True)
    p.add_argument("--repository", required=True)
    p.add_argument("--commit", required=True)
    p.add_argument("--out", required=True)

    p = sub.add_parser("ledger-deploy")
    p.add_argument("--history", required=True)
    p.add_argument("--running", required=True)
    p.add_argument("--running-commits", required=True)
    p.add_argument("--target", required=True)
    p.add_argument("--run-id", required=True)
    p.add_argument("--recorded-utc", required=True)
    p.add_argument("--out", required=True)

    p = sub.add_parser("rollback-target")
    p.add_argument("--history", required=True)
    p.add_argument("--running", required=True)
    p.add_argument("--out", required=True)

    p = sub.add_parser("ledger-rollback")
    p.add_argument("--plan", required=True)
    p.add_argument("--task-definitions", required=True)
    p.add_argument("--run-id", required=True)
    p.add_argument("--recorded-utc", required=True)
    p.add_argument("--out", required=True)

    p = sub.add_parser("smoke")
    p.add_argument("--api-origin", required=True)
    p.add_argument("--out", required=True)

    args = parser.parse_args(argv)
    try:
        if args.command == "spa-plan":
            plan = spa_plan(Path(args.dist))
            _dump(plan, args.out)
            Path(args.upload_list).write_text("".join(f"{line}\n" for line in spa_upload_lines(plan)),
                                              encoding="utf-8")
        elif args.command == "spa-index-metadata":
            head = _load(args.current_head) if args.current_head else None
            _dump(spa_index_metadata(_load(args.plan), head, args.commit, args.run_id), args.out)
        elif args.command == "spa-rollback-target":
            _dump(spa_rollback_target(_load(args.versions), args.run_id), args.out)
        elif args.command == "ecs-template":
            _dump(select_template(_load(args.candidates), args.family), args.out)
        elif args.command == "ecs-render":
            _dump(render_task_definition(_load(args.template), args.digest, args.commit,
                                         args.run_id), args.out)
        elif args.command == "ecs-running":
            tasks = {"api": _load(args.api_tasks), "worker": _load(args.worker_tasks)}
            _dump(running_release(_load(args.services), tasks), args.out)
        elif args.command == "ecs-verify":
            tasks = {"api": _load(args.api_tasks), "worker": _load(args.worker_tasks)}
            verify_rollout(_load(args.services), tasks, _load(args.expected))
            print("ok: both services run the registered revisions and target digests")
        elif args.command == "image-commit":
            print(commit_for_digest(_load(args.described), args.repository))
        elif args.command == "image-target":
            _dump(resolve_target(_load(args.described), args.repository, args.commit), args.out)
        elif args.command == "ledger-deploy":
            record = ledger_deploy(ledger_records(_load(args.history)), _load(args.running),
                                   _load(args.running_commits), _load(args.target), args.run_id,
                                   args.recorded_utc)
            _dump(record, args.out)
        elif args.command == "rollback-target":
            _dump(rollback_target(ledger_records(_load(args.history)), _load(args.running)),
                  args.out)
        elif args.command == "ledger-rollback":
            _dump(ledger_rollback(_load(args.plan), _load(args.task_definitions), args.run_id,
                                  args.recorded_utc), args.out)
        elif args.command == "smoke":
            result = smoke(args.api_origin)
            _dump(result, args.out)
            if not result["passed"]:
                for problem in result["problems"]:
                    print(f"::error::post-deploy smoke: {problem}", file=sys.stderr)
                return 1
    except ReleaseError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
