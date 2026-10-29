#!/usr/bin/env python3
"""Milestone CI evidence: bind a milestone run, record the main short check, verify a milestone run.

The milestone-based post-merge CI policy (docs/operations/milestone-ci-policy.md) splits post-merge CI in two:
every push to main runs the short "Main short check" (.github/workflows/main-push.yml), and a completed milestone
runs the full six-job suite on its exact final main commit through .github/workflows/milestone-validation.yml,
which calls .github/workflows/ci.yml after a binding job. This module is the code behind both bindings and the
evidence verifier:

  bind          the Milestone binding job. Refuses (exit 1) unless the run is a workflow_dispatch of the milestone
                workflow on refs/heads/main AND the requested milestone SHA equals the dispatched commit
                (GITHUB_SHA), the checked-out commit and the refs/heads/main tip read at bind time, with a valid
                milestone id and a one-line scope. The suite job `needs:` this job, so a refused binding starts
                no suite job at all. Writes the binding record and the job summary either way.
  short-record  the Main short check's first step after checkout: binds that run to the commit it checked out.
                A push to main is main short-check evidence; a pull_request run (the path-filtered measurement
                trigger) is recorded as "pr-measurement" and is never main evidence.
  verify        the milestone evidence verifier, for the operator or coordinator before a release, launch or
                full-validation closure claim. Reads the run, the jobs of its latest attempt and the check runs of
                the SHA from the GitHub REST API (GET only; pagination followed and its completeness checked), or
                from captured JSON (--fixtures). Exit 0 PASS, 1 REJECT, 2 cannot establish (usage or API error).

Stdlib only; no subprocess (the workflow passes `git rev-parse HEAD` and `git ls-remote` output as arguments, and
an argument a failed command substitution left empty is refused here). The milestone inputs reach `bind` through
environment variables, never through workflow-expression interpolation into script text.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

MILESTONE_WORKFLOW_PATH = ".github/workflows/milestone-validation.yml"
SHORT_CHECK_WORKFLOW_PATH = ".github/workflows/main-push.yml"
MAIN_REF = "refs/heads/main"
BINDING_JOB_NAME = "Milestone binding"
SUITE_CALLER_JOB_NAME = "Milestone full suite"
# The jobs a milestone run must carry, each exactly once and successful in the latest attempt: the binding job and
# the six jobs of .github/workflows/ci.yml (the six required contexts of the main ruleset), which inside the
# milestone run are named "<caller job name> / <job name>". SECURITY_CRITICAL_LIST, review-pinned
# (tests/fixtures/review-pin-registry.json); tests/test_ci_milestone_policy.py derives it from the workflows.
REQUIRED_JOB_NAMES = (
    "Milestone binding",
    "Milestone full suite / Frontend quality",
    "Milestone full suite / Backend quality",
    "Milestone full suite / Migrations and API contract",
    "Milestone full suite / Container build and security",
    "Milestone full suite / Integration smoke",
    "Milestone full suite / Revision reader (unit, IaC contract, in-image)",
)
RUN_NAME = "Milestone {milestone_id} at {sha}"   # milestone-validation.yml `run-name:`, the runs API `display_title`

BINDING_SCHEMA = "signalnest-milestone-binding/1"
SHORT_SCHEMA = "signalnest-main-short-check/1"
EVIDENCE_SCHEMA = "signalnest-milestone-evidence/1"

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
MILESTONE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
SCOPE_MAX = 300
MAX_PAGES = 50
PER_PAGE = 100

EXIT_PASS, EXIT_REJECT, EXIT_ERROR = 0, 1, 2


class EvidenceError(Exception):
    """The evidence cannot be established (API or usage failure). Never a PASS."""


def _is_sha(value) -> bool:
    return isinstance(value, str) and bool(SHA_RE.match(value))


def _scope_problem(scope: str) -> str | None:
    if not scope or not scope.strip():
        return "the milestone scope is empty"
    if len(scope) > SCOPE_MAX:
        return f"the milestone scope is longer than {SCOPE_MAX} characters"
    if any(ord(c) < 32 or ord(c) == 127 for c in scope):
        return "the milestone scope contains a control character or a line break (one line only)"
    return None


def parse_ls_remote(output: str) -> tuple[str | None, str | None]:
    """(sha, problem) from `git ls-remote --exit-code origin refs/heads/main` output: exactly one main line."""
    lines = [line for line in (output or "").splitlines() if line.strip()]
    if len(lines) != 1:
        return None, f"expected exactly one refs/heads/main line from git ls-remote, got {len(lines)}"
    parts = lines[0].split("\t")
    if len(parts) != 2 or parts[1] != MAIN_REF or not _is_sha(parts[0]):
        return None, f"unrecognised git ls-remote line {lines[0][:120]!r}"
    return parts[0], None


# ------------------------------------------------------------------------------------------------ bind
def bind(env: dict, checked_out: str, main_tip_output: str) -> dict:
    """The milestone binding decision and record. Pure: every input is an argument."""
    reasons: list[dict] = []

    def reject(code: str, detail: str) -> None:
        reasons.append({"code": code, "detail": detail})

    repo = env.get("GITHUB_REPOSITORY", "")
    event = env.get("GITHUB_EVENT_NAME", "")
    ref = env.get("GITHUB_REF", "")
    github_sha = env.get("GITHUB_SHA", "")
    workflow_ref = env.get("GITHUB_WORKFLOW_REF", "")
    milestone_id = env.get("MILESTONE_ID", "")
    scope = env.get("MILESTONE_SCOPE", "")
    requested = env.get("MILESTONE_SHA", "")
    main_tip, tip_problem = parse_ls_remote(main_tip_output)

    if event != "workflow_dispatch":
        reject("event", f"event {event!r}: a milestone is bound only by a workflow_dispatch of the milestone workflow")
    if ref != MAIN_REF:
        reject("ref", f"ref {ref!r}: a milestone validates main only (refs/heads/main); a dispatch on any other ref "
                      "runs that ref's workflow file and is refused")
    expected_wf = f"{repo}/{MILESTONE_WORKFLOW_PATH}@{MAIN_REF}"
    if workflow_ref != expected_wf:
        reject("workflow-ref", f"workflow ref {workflow_ref!r} is not {expected_wf!r}")
    if not MILESTONE_ID_RE.match(milestone_id or ""):
        reject("milestone-id", f"milestone id {milestone_id!r} must match {MILESTONE_ID_RE.pattern}")
    problem = _scope_problem(scope)
    if problem:
        reject("milestone-scope", problem)
    if not _is_sha(requested):
        reject("requested-sha", f"requested milestone SHA {requested!r} is not 40 lowercase hex characters")
    if not _is_sha(github_sha):
        reject("github-sha", f"GITHUB_SHA {github_sha!r} is not a commit SHA")
    elif _is_sha(requested) and github_sha != requested:
        reject("dispatched-commit", f"the dispatched commit {github_sha} is not the requested {requested}")
    if not _is_sha(checked_out):
        reject("checked-out", f"checked-out commit {checked_out!r} is not a commit SHA (git rev-parse failed?)")
    elif _is_sha(requested) and checked_out != requested:
        reject("checked-out", f"the checked-out commit {checked_out} is not the requested {requested}")
    if tip_problem:
        reject("main-tip", tip_problem)
    elif _is_sha(requested) and main_tip != requested:
        reject("main-tip", f"refs/heads/main is now at {main_tip}, not the requested {requested}: a milestone "
                           "validates the CURRENT final main commit; re-dispatch for the new tip")

    return {
        "schema": BINDING_SCHEMA,
        "verdict": "BOUND" if not reasons else "REJECTED",
        "milestone_id": milestone_id,
        "milestone_scope": scope,
        "requested_sha": requested,
        "github_sha": github_sha,
        "checked_out_sha": checked_out,
        "main_tip_sha": main_tip,
        "ref": ref,
        "event": event,
        "repository": repo,
        "workflow_ref": workflow_ref,
        "run_id": env.get("GITHUB_RUN_ID", ""),
        "run_attempt": env.get("GITHUB_RUN_ATTEMPT", ""),
        "bound_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "reasons": reasons,
    }


# ------------------------------------------------------------------------------------------------ short-record
def short_record(env: dict, checked_out: str, event_payload: dict | None) -> dict:
    """The Main short check's binding to the commit it checked out. Pure."""
    reasons: list[dict] = []

    def reject(code: str, detail: str) -> None:
        reasons.append({"code": code, "detail": detail})

    repo = env.get("GITHUB_REPOSITORY", "")
    event = env.get("GITHUB_EVENT_NAME", "")
    ref = env.get("GITHUB_REF", "")
    github_sha = env.get("GITHUB_SHA", "")
    workflow_ref = env.get("GITHUB_WORKFLOW_REF", "")
    before = None
    if event == "push":
        evidence_class = "main-short-check"
        if ref != MAIN_REF:
            reject("ref", f"push to {ref!r}: the short check is main-push evidence only")
        if event_payload is not None:
            if event_payload.get("after") != github_sha:
                reject("push-payload", f"the push payload's after {event_payload.get('after')!r} is not GITHUB_SHA")
            before = event_payload.get("before")
    elif event == "pull_request":
        evidence_class = "pr-measurement"     # duration and behaviour only; never main evidence
    else:
        evidence_class = "none"
        reject("event", f"event {event!r} does not produce short-check evidence")
    if not workflow_ref.startswith(f"{repo}/{SHORT_CHECK_WORKFLOW_PATH}@"):
        reject("workflow-ref", f"workflow ref {workflow_ref!r} is not the main push check workflow")
    if not _is_sha(github_sha):
        reject("github-sha", f"GITHUB_SHA {github_sha!r} is not a commit SHA")
    if not _is_sha(checked_out):
        reject("checked-out", f"checked-out commit {checked_out!r} is not a commit SHA (git rev-parse failed?)")
    elif checked_out != github_sha:
        reject("checked-out", f"the checked-out commit {checked_out} is not the run's commit {github_sha}")
    return {
        "schema": SHORT_SCHEMA,
        "verdict": "RECORDED" if not reasons else "REJECTED",
        "evidence_class": evidence_class,
        "is_main_evidence": evidence_class == "main-short-check" and not reasons,
        "satisfies_milestone": False,     # the short check never stands in for milestone validation
        "tested_sha": github_sha,
        "checked_out_sha": checked_out,
        "push_before_sha": before,
        "ref": ref,
        "event": event,
        "repository": repo,
        "workflow_ref": workflow_ref,
        "run_id": env.get("GITHUB_RUN_ID", ""),
        "run_attempt": env.get("GITHUB_RUN_ATTEMPT", ""),
        "recorded_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "reasons": reasons,
    }


def _md(value) -> str:
    """A value made inert for a Markdown table cell (the step summary renders Markdown)."""
    text = "" if value is None else str(value)
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    for ch in "\\`*_[]|#!~":
        text = text.replace(ch, "\\" + ch)
    return text


def summary_markdown(record: dict, title: str) -> str:
    lines = [f"### {title}: {record['verdict']}", "", "| Field | Value |", "| --- | --- |"]
    for key, value in record.items():
        if key != "reasons":
            lines.append(f"| {key} | {_md(value)} |")
    for r in record["reasons"]:
        lines.append(f"- **{_md(r['code'])}**: {_md(r['detail'])}")
    return "\n".join(lines) + "\n"


def _write_record(record: dict, out: pathlib.Path, title: str) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(summary_markdown(record, title))


# ------------------------------------------------------------------------------------------------ API layer
class FixtureApi:
    """Captured or synthetic responses: {"responses": {"<path?query>": {"body": ..., "next": "<path>"|null}}}."""

    def __init__(self, doc: dict):
        self.responses = doc.get("responses") or {}
        self.requests: list[str] = []

    def get(self, path: str):
        self.requests.append(path)
        if path not in self.responses:
            raise EvidenceError(f"no captured response for {path}")
        entry = self.responses[path]
        return entry.get("body"), entry.get("next")


def next_link(link_header: str | None, api_base: str) -> str | None:
    """The rel="next" target of a Link header as a path, refusing a link to another host."""
    if not link_header:
        return None
    for part in link_header.split(","):
        m = re.match(r'\s*<([^>]+)>\s*;\s*rel="next"\s*$', part)
        if m:
            url = m.group(1)
            if not url.startswith(api_base.rstrip("/") + "/"):
                raise EvidenceError(f"a pagination link points outside {api_base}: {url[:120]}")
            return url[len(api_base.rstrip("/")):]
    return None


class LiveApi:
    """GET-only GitHub REST reader. The token (GH_TOKEN or GITHUB_TOKEN) is optional for a public repository."""

    def __init__(self, api_base: str = "https://api.github.com", token: str | None = None, timeout: int = 30):
        if not api_base.startswith("https://"):
            raise EvidenceError("the API base must be https")
        self.api_base = api_base.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.requests: list[str] = []

    def get(self, path: str):
        self.requests.append(path)
        req = urllib.request.Request(self.api_base + path, method="GET", headers={
            "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "signalnest-verify-milestone-evidence"})
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:   # noqa: S310 - https only, GET only
                body = json.loads(resp.read().decode("utf-8"))
                return body, next_link(resp.headers.get("Link"), self.api_base)
        except (urllib.error.URLError, ValueError) as exc:
            raise EvidenceError(f"GET {path} failed: {exc}") from exc


def collect(api, path: str, key: str, reasons: list) -> list | None:
    """Every item of a paginated listing, or None (with a reason) when completeness cannot be shown."""
    items, total, pages, seen = [], None, 0, set()
    while path:
        pages += 1
        if pages > MAX_PAGES:
            reasons.append({"code": "pagination", "detail": f"{key}: more than {MAX_PAGES} pages"})
            return None
        body, path = api.get(path)
        if not isinstance(body, dict) or not isinstance(body.get(key), list):
            reasons.append({"code": "pagination", "detail": f"{key}: a page without a {key} list"})
            return None
        page_total = body.get("total_count")
        if total is None:
            total = page_total
        elif page_total != total:
            reasons.append({"code": "pagination", "detail": f"{key}: total_count changed between pages "
                                                            f"({total} -> {page_total}); re-run the verifier"})
            return None
        for item in body[key]:
            ident = item.get("id")
            if ident in seen:
                reasons.append({"code": "pagination", "detail": f"{key}: item {ident} appeared on two pages"})
                return None
            seen.add(ident)
            items.append(item)
    if not isinstance(total, int) or len(items) != total:
        reasons.append({"code": "pagination", "detail": f"{key}: collected {len(items)} of total_count {total}"})
        return None
    return items


# ------------------------------------------------------------------------------------------------ verify
def _check_run_id(job: dict) -> int | None:
    m = re.search(r"/check-runs/(\d+)$", str(job.get("check_run_url") or ""))
    return int(m.group(1)) if m else None


def verify(api, repo: str, sha: str, run_id: int, attempt: int | None = None, milestone_id: str | None = None,
           binding_record: dict | None = None) -> dict:
    """The verdict on one milestone run for one SHA. Every rule appends a coded reason; PASS needs none."""
    reasons: list[dict] = []

    def reject(code: str, detail: str) -> None:
        reasons.append({"code": code, "detail": detail})

    if not REPO_RE.match(repo or ""):
        raise EvidenceError(f"repository {repo!r} is not owner/name")
    if not _is_sha(sha):
        raise EvidenceError(f"SHA {sha!r} is not 40 lowercase hex characters")
    if milestone_id is not None and not MILESTONE_ID_RE.match(milestone_id):
        raise EvidenceError(f"milestone id {milestone_id!r} must match {MILESTONE_ID_RE.pattern}")

    run, _ = api.get(f"/repos/{repo}/actions/runs/{run_id}")
    if not isinstance(run, dict):
        raise EvidenceError("the run response is not an object")
    if run.get("id") != run_id:
        reject("run-id", f"the API returned run {run.get('id')!r}, not {run_id}")
    if run.get("path") != MILESTONE_WORKFLOW_PATH:
        reject("workflow-path", f"run {run_id} is a run of {run.get('path')!r}, not {MILESTONE_WORKFLOW_PATH} "
                                "(a PR, push, short-check or plain ci.yml dispatch run is not milestone evidence)")
    if run.get("event") != "workflow_dispatch":
        reject("event", f"run event {run.get('event')!r}, not workflow_dispatch")
    if run.get("head_branch") != "main":
        reject("head-branch", f"run head branch {run.get('head_branch')!r}, not main")
    if run.get("head_sha") != sha:
        reject("sha", f"run {run_id} tested {run.get('head_sha')!r}, not {sha} (later commits do not inherit an "
                      "earlier milestone's validation)")
    full_name = (run.get("repository") or {}).get("full_name")
    if full_name is not None and full_name != repo:
        reject("repository", f"run belongs to {full_name!r}, not {repo}")
    title = run.get("display_title")
    m = re.fullmatch(r"Milestone (\S+) at ([0-9a-f]{40})", title or "")
    title_id = m.group(1) if m else None
    if not m:
        reject("run-name", f"display title {title!r} is not the milestone run name")
    else:
        if m.group(2) != sha:
            reject("run-name", f"the run was dispatched for {m.group(2)}, not {sha}")
        if milestone_id is not None and title_id != milestone_id:
            reject("milestone-id", f"the run is milestone {title_id!r}, not {milestone_id!r}")
    if run.get("status") != "completed":
        reject("run-incomplete", f"run status {run.get('status')!r}")
    elif run.get("conclusion") != "success":
        reject("run-conclusion", f"run conclusion {run.get('conclusion')!r}")
    latest = run.get("run_attempt")
    if not isinstance(latest, int) or latest < 1:
        raise EvidenceError(f"run_attempt {latest!r} is not a positive integer")
    if attempt is not None and attempt != latest:
        reject("stale-attempt", f"attempt {attempt} was presented but the run's latest attempt is {latest}; only "
                                "the latest attempt's results count")

    jobs = collect(api, f"/repos/{repo}/actions/runs/{run_id}/attempts/{latest}/jobs?per_page={PER_PAGE}",
                   "jobs", reasons)
    bound: dict[str, dict] = {}
    if jobs is not None:
        for name in REQUIRED_JOB_NAMES:
            matches = [j for j in jobs if j.get("name") == name]
            if not matches:
                reject("job-missing", f"required job {name!r} is absent from attempt {latest}")
                continue
            if len(matches) > 1:
                reject("job-ambiguous", f"required job {name!r} appears {len(matches)} times in attempt {latest}")
                continue
            job = matches[0]
            if job.get("status") != "completed":
                reject("job-incomplete", f"{name}: status {job.get('status')!r}")
            elif job.get("conclusion") != "success":
                reject("job-conclusion", f"{name}: conclusion {job.get('conclusion')!r}")
            if job.get("run_attempt") != latest:
                reject("job-carried-over", f"{name}: result from attempt {job.get('run_attempt')!r}, not the latest "
                                           f"attempt {latest} (re-run ALL jobs; a carried-over result is refused)")
            if job.get("run_id") != run_id:
                reject("job-run", f"{name}: belongs to run {job.get('run_id')!r}")
            if job.get("head_sha") != sha:
                reject("job-sha", f"{name}: tested {job.get('head_sha')!r}, not {sha}")
            if _check_run_id(job) is None:
                reject("job-check-run", f"{name}: no check run URL")
            bound[name] = job

    check_runs = collect(api, f"/repos/{repo}/commits/{sha}/check-runs?filter=all&per_page={PER_PAGE}",
                         "check_runs", reasons)
    if check_runs is not None:
        by_id = {c.get("id"): c for c in check_runs}
        for name, job in bound.items():
            cid = _check_run_id(job)
            if cid is None:
                continue
            cr = by_id.get(cid)
            if cr is None:
                reject("check-run-missing", f"{name}: its check run {cid} is not among the check runs on {sha}")
                continue
            if cr.get("name") != name:
                reject("check-run-name", f"{name}: its check run {cid} is named {cr.get('name')!r}")
            if cr.get("head_sha") != sha:
                reject("check-run-sha", f"{name}: its check run {cid} is on {cr.get('head_sha')!r}")
            if cr.get("status") != "completed" or cr.get("conclusion") != "success":
                reject("check-run-conclusion", f"{name}: its check run {cid} is "
                                               f"{cr.get('status')!r}/{cr.get('conclusion')!r}")
            if f"/actions/runs/{run_id}/" not in str(cr.get("html_url") or ""):
                reject("check-run-run", f"{name}: its check run {cid} does not link to run {run_id}")

    if binding_record is not None:
        b = binding_record
        expected = {"schema": BINDING_SCHEMA, "verdict": "BOUND", "requested_sha": sha, "github_sha": sha,
                    "checked_out_sha": sha, "main_tip_sha": sha, "ref": MAIN_REF, "event": "workflow_dispatch",
                    "repository": repo, "run_id": str(run_id), "run_attempt": str(latest)}
        if title_id is not None:
            expected["milestone_id"] = title_id
        for key, value in expected.items():
            if b.get(key) != value:
                reject("binding-record", f"binding record {key} is {b.get(key)!r}, expected {value!r}")
        if _scope_problem(str(b.get("milestone_scope") or "")):
            reject("binding-record", "binding record has no valid milestone scope")

    return {
        "verdict": "PASS" if not reasons else "REJECT",
        "repository": repo,
        "tested_sha": sha,
        "run_id": run_id,
        "run_attempt": latest,
        "milestone_id": title_id,
        "reasons": reasons,
        "jobs": [{"name": n, "id": j.get("id"), "status": j.get("status"), "conclusion": j.get("conclusion"),
                  "run_attempt": j.get("run_attempt"), "started_at": j.get("started_at"),
                  "completed_at": j.get("completed_at")} for n, j in bound.items()],
        "run_url": run.get("html_url"),
    }


def evidence_record(verdict: dict, binding_record: dict) -> dict:
    """The milestone evidence record (PASS only): id, scope, tested SHA, run id, attempt, actual results."""
    if verdict["verdict"] != "PASS":
        raise EvidenceError("an evidence record is written only for a PASS verdict")
    return {
        "schema": EVIDENCE_SCHEMA,
        "milestone_id": verdict["milestone_id"],
        "milestone_scope": binding_record.get("milestone_scope"),
        "tested_sha": verdict["tested_sha"],
        "repository": verdict["repository"],
        "workflow_path": MILESTONE_WORKFLOW_PATH,
        "run_id": verdict["run_id"],
        "run_attempt": verdict["run_attempt"],
        "run_url": verdict["run_url"],
        "jobs": verdict["jobs"],
        "verified_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "verifier": "scripts/verify_milestone_evidence.py verify",
        "verdict": "PASS",
        "inheritance": "valid for tested_sha only; later main commits do not inherit this validation",
    }


# ------------------------------------------------------------------------------------------------ CLI
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("bind", help="milestone binding (Milestone binding job)")
    b.add_argument("--checked-out", required=True)
    b.add_argument("--main-tip", required=True, help="git ls-remote --exit-code origin refs/heads/main output")
    b.add_argument("--out", required=True, type=pathlib.Path)
    s = sub.add_parser("short-record", help="main short check binding (Main short check job)")
    s.add_argument("--checked-out", required=True)
    s.add_argument("--out", required=True, type=pathlib.Path)
    v = sub.add_parser("verify", help="verify a milestone run for one SHA")
    v.add_argument("--repo", required=True)
    v.add_argument("--sha", required=True)
    v.add_argument("--run", required=True, type=int)
    v.add_argument("--attempt", type=int)
    v.add_argument("--milestone-id")
    v.add_argument("--binding-record", type=pathlib.Path, help="milestone-binding.json from the run's artifact")
    v.add_argument("--record-out", type=pathlib.Path, help="write the evidence record here on PASS")
    v.add_argument("--fixtures", type=pathlib.Path, help="captured responses instead of the live API")
    v.add_argument("--api", default="https://api.github.com")
    args = parser.parse_args(argv)

    if args.command == "bind":
        record = bind(dict(os.environ), args.checked_out, args.main_tip)
        _write_record(record, args.out, "Milestone binding")
        print(json.dumps({"verdict": record["verdict"], "reasons": record["reasons"]}, indent=2))
        return EXIT_PASS if record["verdict"] == "BOUND" else EXIT_REJECT
    if args.command == "short-record":
        payload = None
        path = os.environ.get("GITHUB_EVENT_PATH")
        if path and os.path.isfile(path):
            payload = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
        record = short_record(dict(os.environ), args.checked_out, payload)
        _write_record(record, args.out, "Main short check binding")
        print(json.dumps({"verdict": record["verdict"], "evidence_class": record["evidence_class"],
                          "reasons": record["reasons"]}, indent=2))
        return EXIT_PASS if record["verdict"] == "RECORDED" else EXIT_REJECT

    try:
        if args.record_out and not args.binding_record:
            raise EvidenceError("--record-out needs --binding-record (the record must carry the milestone scope)")
        api = (FixtureApi(json.loads(args.fixtures.read_text(encoding="utf-8"))) if args.fixtures
               else LiveApi(args.api, os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")))
        binding = json.loads(args.binding_record.read_text(encoding="utf-8")) if args.binding_record else None
        verdict = verify(api, args.repo, args.sha, args.run, args.attempt, args.milestone_id, binding)
        if args.record_out and verdict["verdict"] == "PASS":
            args.record_out.write_text(json.dumps(evidence_record(verdict, binding), indent=2, sort_keys=True)
                                       + "\n", encoding="utf-8")
    except (EvidenceError, OSError, ValueError) as exc:
        print(json.dumps({"verdict": "ERROR", "detail": str(exc)}, indent=2))
        return EXIT_ERROR
    print(json.dumps(verdict, indent=2))
    return EXIT_PASS if verdict["verdict"] == "PASS" else EXIT_REJECT


if __name__ == "__main__":
    sys.exit(main())
