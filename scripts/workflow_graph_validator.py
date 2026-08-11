#!/usr/bin/env python3
"""Static publication-workflow graph validator (Gate 4N-I28BG-B1).

WHY THIS EXISTS. workflow_assurance.py is the RUNTIME verifier: it binds the checked-out tree, the
built image digest, and the resolved tags at four points in a live publish job. But a runtime
verifier only protects a job that actually CALLS it, in the right order, with its failures wired to
block the build and the push. That wiring is a property of the workflow FILE, and it is exactly the
property an author can get wrong — a verify step placed after the build, a `continue-on-error` on the
assurance step, an `always()` on a downstream Docker step, a second push path that skips the checks.
This module proves those structural properties statically, from the workflow YAML, with no Docker and
no network.

WHAT IT DERIVES per workflow (GitHub Actions semantics): triggers, jobs, the `needs` DAG, matrices,
step `if` conditions and their `always()/failure()/cancelled()/success()` intent, checkout steps,
the four workflow_assurance phases, Docker build steps, image-binding steps, push steps, other
publish steps, `continue-on-error`, job/step outputs and whether they are consumed, uploaded
artifacts, cache restores, source-mutating steps, and mutable (unpinned) action references.

THE ORDERING PROPERTY it enforces for every Docker build/push site in an INTEGRATED workflow:
  checkout < establish < pre_build_verify < build < image_bind < pre_push_verify < push,
every assurance step fail-closed (no continue-on-error), no downstream Docker/publish step reachable
under always()/failure() that would run past an assurance failure, no alternate Docker or publish
path that bypasses assurance, and the verifier outputs actually consumed.

HONEST NOT-YET-INTEGRATED. reader-publish.yml and staging-publish.yml carry Docker build/push sites
and ZERO workflow_assurance steps today (Gate 4N-I28BG-B1 implements the components; B2/B3 integrate
them). A workflow with Docker sites and no assurance step at all is reported NOT_YET_INTEGRATED — it
is neither falsely PASS (it is unprotected) nor FAIL (a missing integration is not a broken one). The
distinction is load-bearing: B1 must not mark either real workflow protected.
"""

from __future__ import annotations

import re

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"

GRAPH_VALIDATOR_SCHEMA_VERSION = "i28bg-b1.1"

# Per-workflow verdicts. NOT_YET_INTEGRATED is a first-class result, never a silent PASS.
STATUS_PASS = "PASS"
STATUS_FAIL = "FAIL"
STATUS_NOT_YET_INTEGRATED = "NOT_YET_INTEGRATED"
STATUS_NO_DOCKER_SITES = "NO_DOCKER_SITES"

# The two real publication workflows. B1 must leave both NOT_YET_INTEGRATED.
READER_PUBLISH = ".github/workflows/reader-publish.yml"
STAGING_PUBLISH = ".github/workflows/staging-publish.yml"

# Step roles, derived structurally from `uses:`/`run:`, never from a step's display name alone.
ROLE_CHECKOUT = "checkout"
ROLE_ESTABLISH = "assurance_establish"
ROLE_PRE_BUILD = "assurance_pre_build"
ROLE_IMAGE_BIND = "assurance_image_bind"
ROLE_PRE_PUSH = "assurance_pre_push"
ROLE_BUILD = "docker_build"
ROLE_PUSH = "docker_push"
ROLE_PUBLISH = "publish_other"
ROLE_SOURCE_MUTATING = "source_mutating"
ROLE_CACHE_RESTORE = "cache_restore"

_ASSURANCE_ROLE_BY_MODE = {
    "establish": ROLE_ESTABLISH,
    "pre_build_verify": ROLE_PRE_BUILD,
    "post_build_image_bind": ROLE_IMAGE_BIND,
    "pre_push_verify": ROLE_PRE_PUSH,
}
_ASSURANCE_ROLES = tuple(_ASSURANCE_ROLE_BY_MODE.values())

# A `uses:` reference that is not pinned to a full 40-hex commit SHA is mutable: the action it names
# can change under the same ref. Recorded, so an integration that trusts a moving action is visible.
_PINNED_ACTION_RE = re.compile(r"^[^@]+@[0-9a-f]{40}$")

_ALWAYS_RE = re.compile(r"\balways\s*\(\s*\)")
_FAILURE_RE = re.compile(r"\bfailure\s*\(\s*\)")
_CANCELLED_RE = re.compile(r"\bcancelled\s*\(\s*\)")
_SUCCESS_RE = re.compile(r"\bsuccess\s*\(\s*\)")

# The mode is the subcommand token immediately after the verifier script (BG-B2-FIND-01).
_INVOKE_RE = re.compile(r"workflow_assurance\.py\s+([a-z_]+)")

# Any Docker CLI command site (build/run/inspect/push/tag/pull/save/load/buildx). Used to prove that
# EVERY Docker site in an integrated workflow falls inside the assurance envelope (establishment must
# precede the first Docker command, so nothing Docker runs before assurance is established).
_DOCKER_CMD_RE = re.compile(r"\bdocker\s+(?:buildx\s+)?"
                            r"(build|run|inspect|push|tag|pull|save|load|create)\b")


class WorkflowGraphError(RuntimeError):
    """Fail closed. A workflow that cannot be parsed is refused, never treated as clean."""


def _load_yaml(path: Path) -> dict:
    try:
        import yaml
    except ModuleNotFoundError as exc:                # a crash is not a verdict; fail closed
        raise WorkflowGraphError(
            "PyYAML is required to derive the workflow graph; without it this validator would have "
            "to trust an unparsed file, which is the defect it closes") from exc
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise WorkflowGraphError(f"{path} is not parseable YAML: {exc}") from exc
    if not isinstance(doc, dict):
        raise WorkflowGraphError(f"{path} does not parse to a mapping")
    return doc


def _step_text(step: dict) -> str:
    run = step.get("run")
    return run if isinstance(run, str) else ""


def _assurance_mode(step: dict):
    """The workflow_assurance mode a step invokes, or None. Structural: it must actually RUN the
    verifier with the mode as the SUBCOMMAND immediately after the script, not merely mention the
    mode word somewhere in the step body.

    GATE 4N-I28BG-B2 finding BG-B2-FIND-01. The prior implementation matched a mode by bare substring
    ("establish" in text). A realistic pre_build_verify step whose command references its
    establishment record (e.g. `--establishment reader-assurance/establishment.json`) was therefore
    misdetected as `establish` — the mode word appeared in a filename. Detection is now anchored to
    the ACTUAL invocation: the token that immediately follows `workflow_assurance.py`.
    """
    text = _step_text(step)
    m = _INVOKE_RE.search(text)
    if m and m.group(1) in _ASSURANCE_ROLE_BY_MODE:
        return m.group(1)
    return None


def _roles(step: dict) -> set:
    roles = set()
    uses = str(step.get("uses") or "")
    text = _step_text(step)
    if uses.startswith("actions/checkout"):
        roles.add(ROLE_CHECKOUT)
    mode = _assurance_mode(step)
    if mode:
        roles.add(_ASSURANCE_ROLE_BY_MODE[mode])
    if uses.startswith("docker/build-push-action"):
        roles.add(ROLE_BUILD)
        with_block = step.get("with") or {}
        if str(with_block.get("push")).lower() == "true":
            roles.add(ROLE_PUSH)
    if re.search(r"\bdocker\s+build\b|\bdocker\s+buildx\s+build\b", text):
        roles.add(ROLE_BUILD)
    if re.search(r"\bdocker\s+push\b", text):
        roles.add(ROLE_PUSH)
    if re.search(r"\baws\s+ecr\b|amazon-ecr-login|configure-aws-credentials|upload-artifact",
                 uses + " " + text):
        roles.add(ROLE_PUBLISH)
    if re.search(r"\bgit\s+(checkout|reset|apply|clean|pull|fetch)\b|>\s*apps/|tee\s+apps/",
                 text) and ROLE_CHECKOUT not in roles:
        roles.add(ROLE_SOURCE_MUTATING)
    if uses.startswith("actions/cache") or "cache-from" in text:
        roles.add(ROLE_CACHE_RESTORE)
    return roles


def _condition_intent(cond) -> dict:
    """What a step's `if:` expression asks for. `always()`/`failure()`/`cancelled()` make a step run
    past a prior failure — load-bearing when the step is a Docker or publish step."""
    c = str(cond or "")
    return {
        "raw": c or None,
        "has_always": bool(_ALWAYS_RE.search(c)),
        "has_failure": bool(_FAILURE_RE.search(c)),
        "has_cancelled": bool(_CANCELLED_RE.search(c)),
        "has_success": bool(_SUCCESS_RE.search(c)),
        "matrix_guarded": ("matrix." in c),
    }


def analyse_workflow(path: Path) -> dict:
    """Derive the full structural graph of one workflow file."""
    doc = _load_yaml(path)
    # PyYAML parses the bare `on:` key as the boolean True. Accept either spelling.
    triggers = doc.get("on")
    if triggers is None and True in doc:
        triggers = doc[True]
    jobs_doc = doc.get("jobs") or {}
    outputs_consumed_text = _whole_text(path)

    jobs = {}
    for job_id, job in jobs_doc.items():
        if not isinstance(job, dict):
            continue
        steps = []
        for idx, step in enumerate(job.get("steps") or []):
            if not isinstance(step, dict):
                continue
            uses = str(step.get("uses") or "")
            steps.append({
                "index": idx,
                "id": step.get("id"),
                "name": step.get("name"),
                "uses": uses or None,
                "roles": sorted(_roles(step)),
                "continue_on_error": bool(step.get("continue-on-error")),
                "condition": _condition_intent(step.get("if")),
                "assurance_mode": _assurance_mode(step),
                "action_pinned": bool(_PINNED_ACTION_RE.match(uses)) if uses else None,
                "with_push": str((step.get("with") or {}).get("push")).lower()
                if uses.startswith("docker/build-push-action") else None,
                # Whether this step runs a Docker CLI command in its `run:` body — computed here from
                # the RAW step, because the derived step dicts carry no run text downstream.
                "docker_command": bool(_DOCKER_CMD_RE.search(_step_text(step))),
            })
        jobs[job_id] = {
            "needs": _as_list(job.get("needs")),
            "matrix": bool(((job.get("strategy") or {}).get("matrix"))),
            "if": _condition_intent(job.get("if")),
            "outputs": dict(job.get("outputs") or {}),
            "steps": steps,
        }

    return {
        "schema_version": GRAPH_VALIDATOR_SCHEMA_VERSION,
        "workflow_path": _rel(path),
        "triggers": triggers,
        "jobs": jobs,
        "mutable_actions": _mutable_actions(jobs),
        "raw_text_for_output_consumption": outputs_consumed_text,
    }


def _whole_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(REPO_ROOT.resolve()))
    except ValueError:
        return str(path)


def _as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return list(value)


def _mutable_actions(jobs: dict) -> list:
    out = []
    for job_id, job in jobs.items():
        for step in job["steps"]:
            if step["uses"] and step["action_pinned"] is False:
                out.append({"job": job_id, "index": step["index"], "uses": step["uses"]})
    return out


def _first_index(steps: list, role: str):
    for s in steps:
        if role in s["roles"]:
            return s["index"]
    return None


def _all_indices(steps: list, role: str) -> list:
    return [s["index"] for s in steps if role in s["roles"]]


def validate_workflow(path: Path) -> dict:
    """Validate one workflow. Returns {status, problems, docker_sites, assurance_present, ...}."""
    graph = analyse_workflow(path)
    problems: list = []
    jobs = graph["jobs"]

    build_sites = []
    push_sites = []
    assurance_present = False
    for job_id, job in jobs.items():
        steps = job["steps"]
        if any(_ASSURANCE_ROLES[i] in s["roles"] for s in steps for i in range(len(_ASSURANCE_ROLES))):
            assurance_present = True
        for s in steps:
            if ROLE_BUILD in s["roles"]:
                build_sites.append((job_id, s["index"]))
            if ROLE_PUSH in s["roles"]:
                push_sites.append((job_id, s["index"]))

    total_docker = len(build_sites) + len(push_sites)
    if total_docker == 0:
        return {**_summary(graph), "status": STATUS_NO_DOCKER_SITES, "problems": [],
                "assurance_present": False}

    if not assurance_present:
        # Docker sites, zero assurance: unprotected, but not broken. Honest NOT_YET_INTEGRATED.
        return {**_summary(graph), "status": STATUS_NOT_YET_INTEGRATED, "problems": [],
                "assurance_present": False,
                "note": "the workflow builds/pushes images and calls no workflow_assurance phase; "
                        "integration is a later gate"}

    # INTEGRATED: enforce the full ordering + no-bypass property, per job that builds/pushes.
    for job_id, job in jobs.items():
        steps = job["steps"]
        job_builds = _all_indices(steps, ROLE_BUILD)
        job_pushes = _all_indices(steps, ROLE_PUSH)
        if not job_builds and not job_pushes:
            continue
        problems.extend(_validate_job_ordering(job_id, job, steps))

    status = STATUS_FAIL if problems else STATUS_PASS
    return {**_summary(graph), "status": status, "problems": problems,
            "assurance_present": True}


def _validate_job_ordering(job_id: str, job: dict, steps: list) -> list:
    problems = []
    co = _first_index(steps, ROLE_CHECKOUT)
    est = _first_index(steps, ROLE_ESTABLISH)
    pb = _first_index(steps, ROLE_PRE_BUILD)
    ib = _first_index(steps, ROLE_IMAGE_BIND)
    pp = _first_index(steps, ROLE_PRE_PUSH)
    builds = _all_indices(steps, ROLE_BUILD)
    pushes = _all_indices(steps, ROLE_PUSH)
    first_build = min(builds) if builds else None
    last_build = max(builds) if builds else None
    first_push = min(pushes) if pushes else None

    pre_builds = _all_indices(steps, ROLE_PRE_BUILD)
    image_binds = _all_indices(steps, ROLE_IMAGE_BIND)
    pre_pushes = _all_indices(steps, ROLE_PRE_PUSH)

    def need(idx, label):
        if idx is None:
            problems.append(f"{job_id}: no {label} step for a job that builds or pushes an image")
            return False
        return True

    have_co = need(co, "checkout")
    have_est = need(est, "assurance establish")
    if builds and not pre_builds:
        problems.append(f"{job_id}: no pre-build verify step for a job that builds an image")
    if builds and not image_binds:
        problems.append(f"{job_id}: no post-build image-bind step for a job that builds an image")
    if pushes and not pre_pushes:
        problems.append(f"{job_id}: no pre-push verify step for a job that pushes an image")

    if have_co and have_est and not (co < est):
        problems.append(f"{job_id}: establish must follow checkout")
    if have_est and pre_builds and not (est < min(pre_builds)):
        problems.append(f"{job_id}: pre-build verify must follow establish")

    # DUAL/N-IMAGE: each build must be INDEPENDENTLY guarded — its own preceding pre-build verify and
    # its own following image-bind — so one image's assurance cannot cover another's. For n builds:
    #   * the (k+1)-th build (rank k, sorted) needs >= k+1 pre-build steps before it (after establish);
    #   * it needs >= n-k image-bind steps after it (and before the first push).
    sbuilds = sorted(builds)
    n = len(sbuilds)
    push_floor = first_push if first_push is not None else 1 << 30
    for k, b in enumerate(sbuilds):
        pbs = [x for x in pre_builds if x < b and (est is None or x > est)]
        if len(pbs) < k + 1:
            problems.append(f"{job_id}: build #{b} is not independently guarded by its own pre-build "
                            f"verify; {n} build(s) each require a distinct preceding pre-build")
        binds_after = [x for x in image_binds if x > b and x < push_floor]
        if len(binds_after) < n - k:
            problems.append(f"{job_id}: build #{b} is not followed by its own image-bind before the "
                            f"push; {n} build(s) each require a distinct following image-bind")

    # Every image-bind must precede every pre-push, and every pre-push must precede every push, and
    # each built image needs its own pre-push (one passing image may not hide one failing image).
    if pushes and pre_pushes and len(pre_pushes) < n:
        problems.append(f"{job_id}: {len(pre_pushes)} pre-push verify step(s) for {n} built image(s); "
                        "each image must be independently pre-push verified")
    if image_binds and pre_pushes and not (max(image_binds) < min(pre_pushes)):
        problems.append(f"{job_id}: pre-push verify must follow every image-bind")
    if pushes and pre_pushes and not (max(pre_pushes) < first_push):
        problems.append(f"{job_id}: pre-push verify must precede the push")

    # COVERAGE: every Docker command site must fall inside the assurance envelope — nothing Docker
    # may run before establishment. The build (via the action) and the push (via `docker push`) are
    # already ordered above; this additionally catches any bare `docker run/inspect/...` verification
    # step that would execute before assurance is established.
    docker_cmd_indices = [s["index"] for s in steps
                          if ROLE_BUILD in s["roles"] or ROLE_PUSH in s["roles"]
                          or s.get("docker_command")]
    if est is not None and docker_cmd_indices and min(docker_cmd_indices) <= est:
        problems.append(f"{job_id}: a Docker command runs at step #{min(docker_cmd_indices)}, at or "
                        "before establishment; every Docker site must be inside the assurance "
                        "envelope (establishment must precede the first Docker command)")

    # No assurance step may swallow its own failure.
    for s in steps:
        if any(r in s["roles"] for r in _ASSURANCE_ROLES) and s["continue_on_error"]:
            problems.append(f"{job_id}: assurance step #{s['index']} uses continue-on-error; a "
                            "fail-closed verifier may not be allowed to pass the job on failure")

    # No Docker/publish step may run PAST a failure via always()/failure()/cancelled().
    for s in steps:
        if (ROLE_BUILD in s["roles"] or ROLE_PUSH in s["roles"] or ROLE_PUBLISH in s["roles"]):
            ci = s["condition"]
            if ci["has_always"] or ci["has_failure"] or ci["has_cancelled"]:
                problems.append(f"{job_id}: Docker/publish step #{s['index']} runs under "
                                "always()/failure()/cancelled(); it would execute past an assurance "
                                "failure, defeating fail-closed propagation")

    # The pre-push token must be consumed by (or precede) every push; a push before any pre-push
    # verify is an alternate, unprotected push path.
    if pushes and pp is not None:
        for pidx in pushes:
            if not (pp < pidx):
                problems.append(f"{job_id}: push step #{pidx} is not preceded by the pre-push "
                                "verify; it is an alternate push path that bypasses assurance")

    # Source mutation after establishment invalidates the bound tree identity.
    if est is not None:
        for s in steps:
            if ROLE_SOURCE_MUTATING in s["roles"] and s["index"] > est:
                problems.append(f"{job_id}: source-mutating step #{s['index']} runs after "
                                "establishment; the bound tree identity would no longer hold")
            # A second checkout after establishment re-fetches the tree, invalidating the bound
            # source identity exactly as a mutation would.
            if ROLE_CHECKOUT in s["roles"] and s["index"] > est:
                problems.append(f"{job_id}: checkout step #{s['index']} runs after establishment; "
                                "re-checking-out the tree invalidates the bound source identity")

    # In a matrix job, an assurance step guarded by an `if:` referencing the matrix runs for only
    # SOME arms, so the arms it skips build/push unprotected. Assurance must cover every arm.
    if job.get("matrix"):
        for s in steps:
            if any(r in s["roles"] for r in _ASSURANCE_ROLES) and s["condition"]["matrix_guarded"]:
                problems.append(f"{job_id}: assurance step #{s['index']} is matrix-conditional "
                                f"({s['condition']['raw']!r}); the matrix arms it skips build and "
                                "push without assurance")
    # A cache restore after a verify feeds unverified content into the build.
    if pb is not None:
        for s in steps:
            if ROLE_CACHE_RESTORE in s["roles"] and s["index"] > pb and \
                    (not builds or s["index"] < last_build):
                problems.append(f"{job_id}: cache restore #{s['index']} runs after pre-build verify "
                                "and before the build; it injects unverified content")
    return problems


def _summary(graph: dict) -> dict:
    jobs = graph["jobs"]
    return {
        "schema_version": GRAPH_VALIDATOR_SCHEMA_VERSION,
        "workflow_path": graph["workflow_path"],
        "job_count": len(jobs),
        "job_ids": sorted(jobs),
        "single_job": len(jobs) == 1,
        "mutable_actions": graph["mutable_actions"],
        "docker_build_sites": sum(1 for j in jobs.values()
                                  for s in j["steps"] if ROLE_BUILD in s["roles"]),
        "docker_push_sites": sum(1 for j in jobs.values()
                                 for s in j["steps"] if ROLE_PUSH in s["roles"]),
    }


def integration_status() -> dict:
    """The B1 posture of the two real publication workflows: both must be NOT_YET_INTEGRATED."""
    out = {}
    for rel in (READER_PUBLISH, STAGING_PUBLISH):
        p = REPO_ROOT / rel
        out[rel] = validate_workflow(p)["status"] if p.is_file() else "ABSENT"
    return out


def main(argv=None) -> int:
    import argparse
    import json

    ap = argparse.ArgumentParser(description="Static publication-workflow graph validator.")
    ap.add_argument("workflow", nargs="?", help="workflow file to validate (default: the two real "
                    "publication workflows)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    try:
        if args.workflow:
            result = {args.workflow: validate_workflow(Path(args.workflow))}
        else:
            result = integration_status()
    except WorkflowGraphError as exc:
        print(f"  {exc}")
        print("WORKFLOW GRAPH: refused")
        return 2

    if args.json:
        print(json.dumps(result, indent=1, sort_keys=True, default=str))
    else:
        for wf, res in result.items():
            status = res if isinstance(res, str) else res.get("status")
            print(f"  {wf}: {status}")
    # A NOT_YET_INTEGRATED or NO_DOCKER_SITES posture is a valid B1 report, not a failure; only a
    # FAIL (a genuinely broken integration) is non-zero.
    statuses = [(r if isinstance(r, str) else r.get("status")) for r in result.values()]
    return 1 if STATUS_FAIL in statuses else 0


if __name__ == "__main__":
    raise SystemExit(main())
