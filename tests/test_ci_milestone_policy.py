"""Milestone-based post-merge CI: event selection, concurrency isolation, the short check and the evidence verifier.

The policy (docs/operations/milestone-ci-policy.md): pull requests to main run the six required jobs of ci.yml
unchanged; a push to main runs only the Main short check (.github/workflows/main-push.yml); a completed milestone
runs the full suite on its exact final main commit through .github/workflows/milestone-validation.yml, which binds
the request and then calls ci.yml (workflow_call), so those six jobs appear as "Milestone full suite / <name>".

Everything here is derived from the workflow files themselves: GitHub's trigger filters and the workflows' own
`${{ }}` expressions are evaluated by a small model below over an event matrix, never restated by hand. What the
model cannot show — GitHub's actual behaviour on the hosted runner (the called workflow's concurrency evaluation,
check-run naming of called jobs) — is transition validation, observed after merge, and is not claimed here. The
verifier fixtures are synthetic: captured API responses carry 12-digit job ids, which the live-identifier leak scan
rightly refuses in a tracked file, so the real-run negative controls are kept as lane evidence instead.
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
CI = WORKFLOWS / "ci.yml"
MAIN_PUSH = WORKFLOWS / "main-push.yml"
MILESTONE = WORKFLOWS / "milestone-validation.yml"
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import verify_milestone_evidence as vme  # noqa: E402

REPO = "bolade04/signal_nest"
SHA = "0123456789abcdef0123456789abcdef01234567"
NEWER = "fedcba9876543210fedcba9876543210fedcba98"
RUN = 50_000_000_001
# The six required contexts of the main ruleset, in ci.yml job order.
SIX = ("Frontend quality", "Backend quality", "Migrations and API contract", "Container build and security",
       "Integration smoke", "Revision reader (unit, IaC contract, in-image)")


def _load(path: Path) -> dict:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    if True in doc and "on" not in doc:          # PyYAML reads the bare `on:` key as boolean True
        doc["on"] = doc.pop(True)
    return doc


def _security_module():
    spec = importlib.util.spec_from_file_location("scanner_configuration",
                                                  REPO_ROOT / "security" / "test_scanner_configuration.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ============================================================================ a GitHub expression model
_TOKEN = re.compile(r"\s*(?:(\|\||&&|==|!=|!|\(|\)|,|\.)|'((?:[^']|'')*)'|(-?\d+(?:\.\d+)?)|([A-Za-z_][A-Za-z0-9_-]*))")


def _tokens(text: str) -> list:
    out, pos = [], 0
    while pos < len(text):
        if text[pos:].strip() == "":
            break
        m = _TOKEN.match(text, pos)
        assert m and m.end() > pos, f"the expression model cannot read {text[pos:]!r}"
        op, string, number, ident = m.groups()
        out.append(("op", op) if op else ("str", string.replace("''", "'")) if string is not None
                   else ("num", float(number)) if number else ("id", ident))
        pos = m.end()
    return out


def _truthy(v) -> bool:
    return v not in (None, False, 0, "", 0.0)


def _fmt(v) -> str:
    return "" if v is None else ("true" if v is True else "false" if v is False else str(v))


class _Expr:
    """Recursive descent over the subset ci.yml and the new workflows use: || && == != ! ( ) format() paths."""

    def __init__(self, text: str, ctx: dict):
        self.t, self.i, self.ctx = _tokens(text), 0, ctx

    def peek(self, kind=None, value=None):
        if self.i >= len(self.t):
            return None
        tok = self.t[self.i]
        if (kind and tok[0] != kind) or (value is not None and tok[1] != value):
            return None
        return tok

    def take(self, kind=None, value=None):
        tok = self.peek(kind, value)
        assert tok, f"expected {value or kind} at token {self.i} of {self.t}"
        self.i += 1
        return tok

    def run(self):
        v = self.or_()
        assert self.i == len(self.t), f"unparsed tail {self.t[self.i:]}"
        return v

    def or_(self):
        v = self.and_()
        while self.peek("op", "||"):
            self.take()
            r = self.and_()
            v = v if _truthy(v) else r
        return v

    def and_(self):
        v = self.cmp()
        while self.peek("op", "&&"):
            self.take()
            r = self.cmp()
            v = r if _truthy(v) else v
        return v

    def cmp(self):
        v = self.unary()
        for op in ("==", "!="):
            if self.peek("op", op):
                self.take()
                r = self.unary()
                same = (v.lower() == r.lower()) if isinstance(v, str) and isinstance(r, str) else v == r
                return same if op == "==" else not same
        return v

    def unary(self):
        if self.peek("op", "!"):
            self.take()
            return not _truthy(self.unary())
        return self.primary()

    def primary(self):
        if self.peek("op", "("):
            self.take()
            v = self.or_()
            self.take("op", ")")
            return v
        tok = self.take()
        if tok[0] in ("str", "num"):
            return tok[1]
        assert tok[0] == "id", tok
        if tok[1] in ("true", "false", "null"):
            return {"true": True, "false": False, "null": None}[tok[1]]
        if self.peek("op", "("):
            self.take()
            args = [self.or_()]
            while self.peek("op", ","):
                self.take()
                args.append(self.or_())
            self.take("op", ")")
            assert tok[1] == "format", f"function {tok[1]} is not modelled"
            return re.sub(r"\{(\d+)\}", lambda m: _fmt(args[1 + int(m.group(1))]), args[0])
        value = self.ctx.get(tok[1])
        while self.peek("op", "."):
            self.take()
            key = self.take("id")[1]
            value = value.get(key) if isinstance(value, dict) else None
        return value


_TEMPLATE = re.compile(r"\$\{\{(.*?)\}\}", re.S)


def evaluate(template, ctx: dict):
    """A workflow value with its expressions evaluated; a value that is ONE expression keeps its type."""
    if not isinstance(template, str):
        return template
    whole = re.fullmatch(r"\s*\$\{\{(.*?)\}\}\s*", template, re.S)
    if whole:
        return _Expr(whole.group(1), ctx).run()
    return _TEMPLATE.sub(lambda m: _fmt(_Expr(m.group(1), ctx).run()), template)


def test_the_expression_model_follows_github_semantics():
    ctx = {"github": {"event_name": "push", "run_id": 7, "event": {}}}
    assert evaluate("${{ github.event_name == 'PUSH' }}", ctx) is True          # string == ignores case
    assert evaluate("${{ github.event.pull_request.number || 'x' }}", ctx) == "x"
    assert evaluate("${{ false && 'a' || format('r{0}', github.run_id) }}", ctx) == "r7"
    assert evaluate("g-${{ github.missing }}-${{ !github.missing }}", ctx) == "g--true"


# ============================================================================ a trigger and job-selection model
def _glob_re(pattern: str) -> re.Pattern:
    """GitHub filter globs: `**` crosses `/`, `*` does not."""
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**", i):
            out, i = out + ".*", i + 2
        elif pattern[i] == "*":
            out, i = out + "[^/]*", i + 1
        else:
            out, i = out + re.escape(pattern[i]), i + 1
    return re.compile(out + r"\Z")


def _matches(value: str, patterns) -> bool:
    return any(_glob_re(p).match(value) for p in (patterns or []))


def triggered(path: Path, event: dict) -> bool:
    on = _load(path)["on"]
    if isinstance(on, str):
        on = {on: None}
    kind = event["name"]
    if kind not in on:
        return False
    cfg = on[kind] or {}
    if kind == "pull_request":
        if "branches" in cfg and not _matches(event["base"], cfg["branches"]):
            return False
        return "paths" not in cfg or any(_matches(f, cfg["paths"]) for f in event["changed"])
    if kind == "push":
        return "branches" in cfg and _matches(event["branch"], cfg["branches"])
    if kind == "workflow_dispatch":
        return event["workflow"] == path.name
    return False


def check_names(path: Path, prefix: str = "") -> list[str]:
    """The check-run names a run of this workflow produces (a called workflow's jobs are `caller / job`)."""
    names = []
    for job_id, job in _load(path)["jobs"].items():
        if "uses" in job:
            assert job["uses"].startswith("./.github/workflows/"), job["uses"]
            names += check_names(REPO_ROOT / job["uses"][2:], prefix + job["name"] + " / ")
        else:
            assert "if" not in job, f"{path.name}#{job_id}: a job-level if: is outside this model"
            names.append(prefix + job["name"])
    return names


def selected(event: dict) -> dict[str, list[str]]:
    return {p.name: check_names(p) for p in sorted(WORKFLOWS.glob("*.yml")) if triggered(p, event)}


def pr(changed=("apps/api/app/main.py",)):
    return {"name": "pull_request", "base": "main", "changed": list(changed)}


PUSH_MAIN = {"name": "push", "branch": "main"}


def test_a_pull_request_to_main_runs_the_six_required_jobs_unchanged():
    assert selected(pr()) == {"ci.yml": list(SIX)}
    ruleset_contexts = set(SIX)
    assert set(check_names(CI)) == ruleset_contexts


def test_a_pull_request_that_edits_the_short_check_also_measures_it_without_touching_the_six():
    got = selected(pr(changed=[".github/workflows/main-push.yml"]))
    assert got == {"ci.yml": list(SIX), "main-push.yml": ["Main short check"]}
    assert selected(pr(changed=["scripts/verify_milestone_evidence.py"]))["main-push.yml"] == ["Main short check"]


def test_a_push_to_main_selects_the_short_path_only():
    got = selected(PUSH_MAIN)
    assert got == {"main-push.yml": ["Main short check"]}
    assert not set(SIX) & {n for names in got.values() for n in names}, "no required name may run on a main push"


def test_feature_branch_pushes_keep_the_full_suite():
    assert selected({"name": "push", "branch": "feature/x/y"}) == {"ci.yml": list(SIX)}
    assert selected({"name": "push", "branch": "ci/p6-anything"}) == {}


def test_a_milestone_dispatch_selects_the_binding_and_the_full_suite_under_prefixed_names():
    got = selected({"name": "workflow_dispatch", "workflow": "milestone-validation.yml"})
    assert got == {"milestone-validation.yml": list(vme.REQUIRED_JOB_NAMES)}
    assert vme.REQUIRED_JOB_NAMES == (vme.BINDING_JOB_NAME,) + tuple(f"{vme.SUITE_CALLER_JOB_NAME} / {n}" for n in SIX)
    assert not set(SIX) & set(got["milestone-validation.yml"]), "milestone checks can neither satisfy nor block a PR"


def test_a_plain_ci_dispatch_is_unchanged_and_carries_no_milestone_names():
    assert selected({"name": "workflow_dispatch", "workflow": "ci.yml"}) == {"ci.yml": list(SIX)}


def test_the_ci_triggers_are_exactly_the_policy():
    assert _load(CI)["on"] == {"pull_request": {"branches": ["main"]}, "push": {"branches": ["feature/**"]},
                               "workflow_dispatch": None, "workflow_call": None}


def test_the_milestone_suite_needs_the_binding_and_calls_ci_unmodified():
    jobs = _load(MILESTONE)["jobs"]
    assert list(jobs) == ["milestone-binding", "milestone-suite"]
    suite = jobs["milestone-suite"]
    assert suite == {"name": vme.SUITE_CALLER_JOB_NAME, "needs": "milestone-binding",
                     "uses": "./.github/workflows/ci.yml"}, "no if:, with: or secrets: may alter the called suite"
    assert jobs["milestone-binding"]["name"] == vme.BINDING_JOB_NAME
    assert tuple(job["name"] for job in _load(CI)["jobs"].values()) == SIX
    assert vme.REQUIRED_JOB_NAMES == tuple(check_names(MILESTONE)), "the verifier requires exactly what the run carries"


# ============================================================================ event-gated steps (equivalence)
EVENT_CONTEXT = re.compile(r"github\.(event|event_name|ref|ref_name|head_ref|base_ref|workflow|run_id|run_attempt)\b|inputs\.")
ALLOWED_EVENT_EXPRESSIONS = {
    "github.event_name == 'pull_request' && format('pr-{0}', github.event.pull_request.number) || "
    "format('{0}-{1}', github.ref, github.run_id)": 1,
    "github.event_name == 'pull_request'": 1,
    "github.workflow": 1,
    "github.event.pull_request.head.sha || github.sha": 5,
}


def _expressions(text: str) -> list[str]:
    return [" ".join(m.group(1).split()) for m in _TEMPLATE.finditer(text)]


def test_no_ci_step_or_job_is_gated_on_the_event_so_the_milestone_suite_runs_everything_a_main_push_ran():
    """Coordinator review point 1: the called suite must execute at least what a push-to-main run executed."""
    text = CI.read_text(encoding="utf-8")
    doc = _load(CI)
    found = {}
    for expr in _expressions(text):
        if EVENT_CONTEXT.search(expr):
            found[expr] = found.get(expr, 0) + 1
    assert found == ALLOWED_EVENT_EXPRESSIONS, f"an event-dependent expression changed: {found}"
    for job_id, job in doc["jobs"].items():
        assert "if" not in job, f"{job_id}: a job-level condition"
        for step in job.get("steps") or []:
            cond = str(step.get("if", ""))
            assert "github." not in cond and "inputs." not in cond, f"{job_id}: {step.get('name')}: {cond}"
    for tree in ("scripts", "security", "apps/api/tools"):
        for path in (REPO_ROOT / tree).rglob("*.py"):
            if path.name == "verify_milestone_evidence.py":
                continue
            src = path.read_text(encoding="utf-8")
            assert not re.search(r"GITHUB_(EVENT_NAME|EVENT_PATH|REF|BASE_REF|HEAD_REF)\b", src), path
    push_main = {"github": {"event_name": "push", "sha": SHA, "ref": "refs/heads/main", "event": {"after": SHA}}}
    for event_name in ("workflow_dispatch", "workflow_call"):
        milestone = {"github": {"event_name": event_name, "sha": SHA, "ref": "refs/heads/main", "event": {}}}
        for ctx in (push_main, milestone):
            assert evaluate("${{ github.event.pull_request.head.sha || github.sha }}", ctx) == SHA


# ============================================================================ concurrency isolation
def _concurrency(path: Path, ctx: dict):
    c = _load(path).get("concurrency")
    return None if c is None else (evaluate(c["group"], ctx), evaluate(c["cancel-in-progress"], ctx))


def _ctx(event_name, workflow, ref, run_id, pr_number=None):
    event = {"pull_request": {"number": pr_number, "head": {"sha": SHA}}} if pr_number else {}
    return {"github": {"event_name": event_name, "workflow": workflow, "ref": ref, "run_id": run_id, "sha": SHA,
                       "event": event}}


def test_no_pull_request_or_main_push_run_can_cancel_or_queue_a_milestone_run():
    pr_groups = {_concurrency(CI, _ctx("pull_request", "CI", f"refs/pull/{n}/merge", 900 + n, n))
                 for n in (1, 2, 275)}
    assert all(cancel is True for _, cancel in pr_groups)
    short_pr = _concurrency(MAIN_PUSH, _ctx("pull_request", "Main push check", "refs/pull/5/merge", 3, 5))
    short_main = {_concurrency(MAIN_PUSH, _ctx("push", "Main push check", "refs/heads/main", r)) for r in (11, 12)}
    assert short_pr[1] is True and all(c is False for _, c in short_main) and len(short_main) == 2
    assert _load(MILESTONE).get("concurrency") is None, "the caller sets no group, so it cannot deadlock the callee"
    # The called ci.yml's group is evaluated in the CALLER's context; every candidate evaluation is modelled.
    for run_id in (RUN, RUN + 1):
        called = {_concurrency(CI, _ctx(e, w, "refs/heads/main", run_id))
                  for e in ("workflow_dispatch", "workflow_call") for w in ("Milestone validation", "CI")}
        for group, cancel in called:
            assert cancel is False, "a milestone run never cancels anything in progress"
            assert str(run_id) in group and "-pr-" not in group
            assert group not in {g for g, _ in pr_groups} | {short_pr[0]} | {g for g, _ in short_main}
    a = _concurrency(CI, _ctx("workflow_dispatch", "Milestone validation", "refs/heads/main", RUN))
    b = _concurrency(CI, _ctx("workflow_dispatch", "Milestone validation", "refs/heads/main", RUN + 1))
    assert a[0] != b[0], "two milestone runs on one SHA do not share a group (neither queues nor cancels)"


def test_the_concurrency_check_can_fail():
    doc = CI.read_text(encoding="utf-8").replace("format('{0}-{1}', github.ref, github.run_id)",
                                                 "github.ref", 1)
    group = evaluate(re.search(r"group: (.*)", doc).group(1),
                     _ctx("workflow_dispatch", "CI", "refs/heads/main", RUN))
    assert str(RUN) not in group, "without the run id every main dispatch would share one group"


# ============================================================================ the short main check
def test_the_short_check_is_the_integration_smoke_job_verbatim_plus_its_binding():
    ci, mp = _load(CI), _load(MAIN_PUSH)
    assert mp["on"] == {"push": {"branches": ["main"]},
                        "pull_request": {"branches": ["main"], "paths": [".github/workflows/main-push.yml",
                                                                         "scripts/verify_milestone_evidence.py"]}}
    for key in ("env", "defaults", "permissions"):
        assert mp[key] == ci[key], key
    assert mp["permissions"] == {"contents": "read"}
    assert list(mp["jobs"]) == ["main-short-check"]
    job = mp["jobs"]["main-short-check"]
    assert set(job) == {"name", "runs-on", "steps"} and job["name"] == "Main short check"
    ism = ci["jobs"]["integration-smoke"]["steps"]
    steps = job["steps"]
    assert steps[0] == ism[0]
    assert steps[1]["name"].startswith("Bind this run") and set(steps[1]) == {"name", "run"}
    assert "scripts/verify_milestone_evidence.py short-record" in steps[1]["run"]
    assert steps[2:-1] == ism[1:], "every install, scan, gate and control step is the reviewed twin's"
    assert steps[-1]["uses"] == "actions/upload-artifact@v7" and steps[-1]["if"] == "always()"
    assert steps[-1]["with"]["path"] == "short-check-evidence/"


def test_the_short_check_passes_the_merge_path_scanner_checks():
    sc = _security_module()
    text = MAIN_PUSH.read_text(encoding="utf-8")
    count, problems = sc.trivy_step_problems(text)
    assert (count, problems) == (sc.TRIVY_STEPS["integration-smoke"], [])
    body = sc.job_text(text, "main-short-check")
    uses = {m.group(1) for m in re.finditer(r"(?:^|[\s{,])['\"]?uses['\"]?\s*:\s*['\"]?([^\s,}'\"]+)", body, re.M)}
    assert uses == sc.REVIEWED_USES["integration-smoke"]
    for line in body.splitlines():
        if not line.strip().startswith(("#", "- name:")):
            assert not sc.TRIVY_WORD.search(line), line
    assert "working-directory" not in text and not re.search(r"uses['\"]?\s*:\s*['\"]?\.{1,2}/", text)
    assert "--job integration-smoke " in body and "--binary-job integration-smoke " in body


def test_no_workflow_interpolates_inputs_or_event_text_into_a_script():
    for path in (MAIN_PUSH, MILESTONE):
        for job in _load(path)["jobs"].values():
            for step in job.get("steps") or []:
                assert "${{" not in str(step.get("run", "")), f"{path.name}: {step.get('name')}"


def test_the_milestone_workflow_is_dispatch_only_and_passes_inputs_through_the_environment():
    doc = _load(MILESTONE)
    inputs = doc["on"]["workflow_dispatch"]["inputs"]
    assert list(doc["on"]) == ["workflow_dispatch"]
    assert list(inputs) == ["milestone_id", "milestone_scope", "milestone_sha"]
    assert all(i["required"] is True and i["type"] == "string" for i in inputs.values())
    assert doc["permissions"] == {"contents": "read"} and "secrets" not in MILESTONE.read_text(encoding="utf-8")
    assert doc["run-name"] == vme.RUN_NAME.format(milestone_id="${{ inputs.milestone_id }}",
                                                   sha="${{ inputs.milestone_sha }}")
    steps = doc["jobs"]["milestone-binding"]["steps"]
    assert [s.get("uses") for s in steps] == ["actions/checkout@v7", None, "actions/upload-artifact@v7"]
    assert steps[1]["env"] == {"MILESTONE_ID": "${{ inputs.milestone_id }}",
                               "MILESTONE_SCOPE": "${{ inputs.milestone_scope }}",
                               "MILESTONE_SHA": "${{ inputs.milestone_sha }}"}
    assert "verify_milestone_evidence.py bind" in steps[1]["run"]
    assert steps[2]["if"] == "always()"
    assert "trivy" not in MILESTONE.read_text(encoding="utf-8").lower()


# ============================================================================ bind (milestone binding)
def _bind_env(**over):
    env = {"GITHUB_REPOSITORY": REPO, "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REF": "refs/heads/main",
           "GITHUB_SHA": SHA, "GITHUB_WORKFLOW_REF": f"{REPO}/.github/workflows/milestone-validation.yml@refs/heads/main",
           "GITHUB_RUN_ID": str(RUN), "GITHUB_RUN_ATTEMPT": "1", "MILESTONE_ID": "M-2026-10-P6-CI",
           "MILESTONE_SCOPE": "PRs #270-#275 (CI policy, coverage, proofs)", "MILESTONE_SHA": SHA}
    env.update(over)
    return env


TIP = f"{SHA}\trefs/heads/main\n"


def _codes(record) -> set:
    return {r["code"] for r in record["reasons"]}


def test_a_valid_milestone_request_binds():
    rec = vme.bind(_bind_env(), SHA, TIP)
    assert rec["verdict"] == "BOUND" and rec["reasons"] == []
    assert (rec["requested_sha"], rec["github_sha"], rec["checked_out_sha"], rec["main_tip_sha"]) == (SHA,) * 4
    assert rec["milestone_id"] == "M-2026-10-P6-CI" and rec["run_id"] == str(RUN) and rec["run_attempt"] == "1"


@pytest.mark.parametrize("env, checked_out, tip, code", [
    (_bind_env(GITHUB_REF="refs/heads/ci/p6-milestone-ci-policy-01"), SHA, TIP, "ref"),
    (_bind_env(GITHUB_REF="refs/pull/276/merge"), SHA, TIP, "ref"),
    (_bind_env(GITHUB_EVENT_NAME="push"), SHA, TIP, "event"),
    (_bind_env(GITHUB_WORKFLOW_REF=f"{REPO}/.github/workflows/ci.yml@refs/heads/main"), SHA, TIP, "workflow-ref"),
    (_bind_env(GITHUB_WORKFLOW_REF=f"{REPO}/.github/workflows/milestone-validation.yml@refs/heads/x"), SHA, TIP,
     "workflow-ref"),
    (_bind_env(MILESTONE_SHA=NEWER), SHA, TIP, "dispatched-commit"),
    (_bind_env(MILESTONE_SHA=SHA.upper()), SHA, TIP, "requested-sha"),
    (_bind_env(MILESTONE_SHA=SHA[:12]), SHA, TIP, "requested-sha"),
    (_bind_env(), NEWER, TIP, "checked-out"),
    (_bind_env(), "", TIP, "checked-out"),
    (_bind_env(), SHA, f"{NEWER}\trefs/heads/main\n", "main-tip"),
    (_bind_env(), SHA, "", "main-tip"),
    (_bind_env(), SHA, TIP + f"{NEWER}\trefs/heads/main\n", "main-tip"),
    (_bind_env(), SHA, f"{SHA}\trefs/heads/mainline\n", "main-tip"),
    (_bind_env(MILESTONE_ID=""), SHA, TIP, "milestone-id"),
    (_bind_env(MILESTONE_ID="m 1"), SHA, TIP, "milestone-id"),
    (_bind_env(MILESTONE_ID="x" * 65), SHA, TIP, "milestone-id"),
    (_bind_env(MILESTONE_SCOPE=""), SHA, TIP, "milestone-scope"),
    (_bind_env(MILESTONE_SCOPE="a\nb"), SHA, TIP, "milestone-scope"),
    (_bind_env(MILESTONE_SCOPE="x" * 301), SHA, TIP, "milestone-scope"),
])
def test_an_invalid_milestone_request_is_refused_before_any_suite_job(env, checked_out, tip, code):
    rec = vme.bind(env, checked_out, tip)
    assert rec["verdict"] == "REJECTED" and code in _codes(rec), rec["reasons"]


def test_bind_cli_exits_nonzero_and_still_writes_the_record(tmp_path, monkeypatch):
    for key, value in _bind_env(MILESTONE_SHA=NEWER).items():
        monkeypatch.setenv(key, value)
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    out = tmp_path / "e" / "milestone-binding.json"
    assert vme.main(["bind", "--checked-out", SHA, "--main-tip", TIP, "--out", str(out)]) == vme.EXIT_REJECT
    assert json.loads(out.read_text())["verdict"] == "REJECTED"
    assert "Milestone binding: REJECTED" in summary.read_text()


def test_the_summary_renders_a_hostile_scope_inert():
    rec = vme.bind(_bind_env(MILESTONE_SCOPE="<img src=x> | `code` [l](u)"), SHA, TIP)
    md = vme.summary_markdown(rec, "Milestone binding")
    assert "<img" not in md and "\\|" in md and "\\`" in md


# ============================================================================ short-record (main short check)
def _short_env(**over):
    env = {"GITHUB_REPOSITORY": REPO, "GITHUB_EVENT_NAME": "push", "GITHUB_REF": "refs/heads/main",
           "GITHUB_SHA": SHA, "GITHUB_WORKFLOW_REF": f"{REPO}/.github/workflows/main-push.yml@refs/heads/main",
           "GITHUB_RUN_ID": "77", "GITHUB_RUN_ATTEMPT": "1"}
    env.update(over)
    return env


def test_a_main_push_records_main_short_check_evidence_that_never_satisfies_a_milestone():
    rec = vme.short_record(_short_env(), SHA, {"after": SHA, "before": NEWER})
    assert rec["verdict"] == "RECORDED" and rec["is_main_evidence"] is True and rec["satisfies_milestone"] is False
    assert rec["push_before_sha"] == NEWER


def test_a_pull_request_short_check_run_is_a_measurement_only():
    rec = vme.short_record(_short_env(GITHUB_EVENT_NAME="pull_request", GITHUB_REF="refs/pull/276/merge",
                                      GITHUB_WORKFLOW_REF=f"{REPO}/.github/workflows/main-push.yml@refs/pull/276/merge"),
                           SHA, None)
    assert rec["verdict"] == "RECORDED" and rec["evidence_class"] == "pr-measurement"
    assert rec["is_main_evidence"] is False


@pytest.mark.parametrize("env, checked_out, payload, code", [
    (_short_env(GITHUB_REF="refs/heads/feature/x"), SHA, None, "ref"),
    (_short_env(GITHUB_EVENT_NAME="workflow_dispatch"), SHA, None, "event"),
    (_short_env(), NEWER, None, "checked-out"),
    (_short_env(), "", None, "checked-out"),
    (_short_env(), SHA, {"after": NEWER}, "push-payload"),
    (_short_env(GITHUB_WORKFLOW_REF=f"{REPO}/.github/workflows/ci.yml@refs/heads/main"), SHA, None, "workflow-ref"),
])
def test_a_short_check_binding_that_does_not_hold_is_refused(env, checked_out, payload, code):
    rec = vme.short_record(env, checked_out, payload)
    assert rec["verdict"] == "REJECTED" and code in _codes(rec) and rec["is_main_evidence"] is False


# ============================================================================ the evidence verifier
def _job(name, jid, attempt=1, conclusion="success", status="completed", sha=SHA, run_id=RUN):
    return {"id": jid, "run_id": run_id, "run_attempt": attempt, "name": name, "head_sha": sha, "status": status,
            "conclusion": conclusion, "started_at": "2026-10-29T05:00:00Z", "completed_at": "2026-10-29T06:00:00Z",
            "check_run_url": f"https://api.github.com/repos/{REPO}/check-runs/{jid}",
            "html_url": f"https://github.com/{REPO}/actions/runs/{run_id}/job/{jid}"}


def _check(job):
    return {"id": job["id"], "name": job["name"], "head_sha": job["head_sha"], "status": job["status"],
            "conclusion": job["conclusion"], "html_url": job["html_url"], "details_url": job["html_url"]}


def _paged(base: str, key: str, items: list, per_page: int) -> dict:
    """Responses for a listing split into pages linked by `next`, as the Link header would."""
    out, pages = {}, [items[i:i + per_page] for i in range(0, len(items), per_page)] or [[]]
    for n, chunk in enumerate(pages, start=1):
        path = base if n == 1 else f"{base}&page={n}"
        nxt = f"{base}&page={n + 1}" if n < len(pages) else None
        out[path] = {"body": {"total_count": len(items), key: chunk}, "next": nxt}
    return out


def scenario(sha=SHA, run_id=RUN, attempt=1, path=vme.MILESTONE_WORKFLOW_PATH, event="workflow_dispatch",
             branch="main", title=None, jobs=None, extra_checks=(), conclusion="success", per_page=4,
             base_id=10_000) -> dict:
    jobs = jobs if jobs is not None else [_job(n, base_id + i, attempt, sha=sha, run_id=run_id)
                                          for i, n in enumerate(vme.REQUIRED_JOB_NAMES)]
    run = {"id": run_id, "path": path, "event": event, "head_branch": branch, "head_sha": sha,
           "display_title": title if title is not None else vme.RUN_NAME.format(milestone_id="M-1", sha=sha),
           "status": "completed", "conclusion": conclusion, "run_attempt": attempt,
           "repository": {"full_name": REPO}, "html_url": f"https://github.com/{REPO}/actions/runs/{run_id}"}
    responses = {f"/repos/{REPO}/actions/runs/{run_id}": {"body": run, "next": None}}
    responses.update(_paged(f"/repos/{REPO}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100", "jobs",
                            jobs, per_page))
    checks = [_check(j) for j in jobs] + list(extra_checks)
    responses.update(_paged(f"/repos/{REPO}/commits/{sha}/check-runs?filter=all&per_page=100", "check_runs",
                            checks, per_page))
    return {"responses": responses}


def run_verify(doc, sha=SHA, run_id=RUN, **kw):
    return vme.verify(vme.FixtureApi(doc), REPO, sha, run_id, **kw)


def test_a_complete_milestone_run_passes_across_pages():
    doc = scenario()
    v = run_verify(doc, milestone_id="M-1")
    assert v["verdict"] == "PASS", v["reasons"]
    assert [j["name"] for j in v["jobs"]] == list(vme.REQUIRED_JOB_NAMES)
    assert sum(1 for k in doc["responses"] if "/jobs?" in k) == 2 and sum(1 for k in doc["responses"]
                                                                          if "/check-runs?" in k) == 2


def test_a_full_rerun_after_a_partial_failure_passes_on_its_own_check_runs():
    """Coordinator point 2: attempt 1's check runs stay on the SHA; each job binds to ITS OWN check run."""
    failed_first = [_job(n, 20_000 + i, 1, conclusion="failure" if "Backend" in n else "success")
                    for i, n in enumerate(vme.REQUIRED_JOB_NAMES)]
    doc = scenario(attempt=2, base_id=30_000, extra_checks=[_check(j) for j in failed_first])
    assert run_verify(doc)["verdict"] == "PASS"
    assert run_verify(doc, attempt=2)["verdict"] == "PASS"


def test_two_milestone_runs_on_one_sha_each_pass():
    first = scenario(run_id=RUN, base_id=40_000)
    second = scenario(run_id=RUN + 1, base_id=50_000,
                      extra_checks=[_check(_job(n, 40_000 + i)) for i, n in enumerate(vme.REQUIRED_JOB_NAMES)])
    first["responses"].update({k: v for k, v in second["responses"].items() if "/actions/runs/" in k})
    first["responses"].update({k: v for k, v in second["responses"].items() if "/check-runs?" in k})
    assert run_verify(first, run_id=RUN)["verdict"] == "PASS"
    assert run_verify(first, run_id=RUN + 1)["verdict"] == "PASS"


def test_another_runs_failed_check_run_on_the_same_sha_is_irrelevant():
    """A failed check run of the same name from another run (listed first) must not decide this run's verdict."""
    other = [_check(_job(n, 60_000 + i, run_id=RUN + 7, conclusion="failure")) for i, n in
             enumerate(vme.REQUIRED_JOB_NAMES)]
    doc = scenario()
    checks = other + [_check(_job(n, 10_000 + i)) for i, n in enumerate(vme.REQUIRED_JOB_NAMES)]
    doc["responses"] = {k: v for k, v in doc["responses"].items() if "/check-runs?" not in k}
    doc["responses"].update(_paged(f"/repos/{REPO}/commits/{SHA}/check-runs?filter=all&per_page=100", "check_runs",
                                   checks, 4))
    assert run_verify(doc)["verdict"] == "PASS"


def _codes_of(v) -> set:
    return {r["code"] for r in v["reasons"]}


def _mutated_jobs(change):
    jobs = [_job(n, 10_000 + i) for i, n in enumerate(vme.REQUIRED_JOB_NAMES)]
    change(jobs)
    return jobs


REJECTIONS = {
    "another SHA": (lambda: {"responses": {**scenario(sha=NEWER)["responses"], **_paged(
        f"/repos/{REPO}/commits/{SHA}/check-runs?filter=all&per_page=100", "check_runs", [], 4)}}, {"sha": SHA}, "sha"),
    "an earlier milestone presented for a newer commit": (
        lambda: {"responses": {**scenario()["responses"],
                               **{k.replace(SHA, NEWER): v for k, v in scenario()["responses"].items()
                                  if "/check-runs?" in k}}}, {"sha": NEWER}, "sha"),
    "the short-check run": (lambda: scenario(path=vme.SHORT_CHECK_WORKFLOW_PATH, event="push",
                                             title="feat: x (#280)",
                                             jobs=[_job("Main short check", 10_001)]), {}, "workflow-path"),
    "a plain ci.yml dispatch": (lambda: scenario(path=".github/workflows/ci.yml", title="CI",
                                                 jobs=[_job(n, 10_000 + i) for i, n in enumerate(SIX)]), {},
                                "workflow-path"),
    "a pull_request run": (lambda: scenario(path=".github/workflows/ci.yml", event="pull_request", branch="x",
                                            title="feat: x"), {}, "event"),
    "a dispatch on another branch": (lambda: scenario(branch="ci/p6-milestone-ci-policy-01"), {}, "head-branch"),
    "a run that is still running": (lambda: {"responses": {**scenario()["responses"], **{
        f"/repos/{REPO}/actions/runs/{RUN}": {"body": {**scenario()["responses"][f"/repos/{REPO}/actions/runs/{RUN}"]
                                                       ["body"], "status": "in_progress", "conclusion": None}}}}},
                                    {}, "run-incomplete"),
    "a failed run": (lambda: scenario(conclusion="failure"), {}, "run-conclusion"),
    "a stale earlier attempt": (lambda: scenario(attempt=2), {"attempt": 1}, "stale-attempt"),
    "a failed required job": (lambda: scenario(jobs=_mutated_jobs(lambda j: j[2].update(conclusion="failure"))),
                              {}, "job-conclusion"),
    "a cancelled required job": (lambda: scenario(jobs=_mutated_jobs(lambda j: j[3].update(conclusion="cancelled"))),
                                 {}, "job-conclusion"),
    "a skipped required job": (lambda: scenario(jobs=_mutated_jobs(lambda j: j[4].update(conclusion="skipped"))),
                               {}, "job-conclusion"),
    "an incomplete required job": (lambda: scenario(jobs=_mutated_jobs(
        lambda j: j[5].update(status="in_progress", conclusion=None))), {}, "job-incomplete"),
    "a missing required job": (lambda: scenario(jobs=_mutated_jobs(lambda j: j.pop(6))), {}, "job-missing"),
    "a duplicated (ambiguous) required job": (lambda: scenario(jobs=_mutated_jobs(
        lambda j: j.append(_job(j[1]["name"], 19_999)))), {}, "job-ambiguous"),
    "a job carried over from an earlier attempt": (lambda: scenario(attempt=2, jobs=[
        _job(n, 10_000 + i, attempt=1 if i == 2 else 2) for i, n in enumerate(vme.REQUIRED_JOB_NAMES)]), {},
        "job-carried-over"),
    "a job of another commit": (lambda: scenario(jobs=_mutated_jobs(lambda j: j[1].update(head_sha=NEWER))), {},
                                "job-sha"),
    "a run name for another SHA": (lambda: scenario(title=vme.RUN_NAME.format(milestone_id="M-1", sha=NEWER)), {},
                                   "run-name"),
    "another milestone id": (lambda: scenario(), {"milestone_id": "M-2"}, "milestone-id"),
}


@pytest.mark.parametrize("label", sorted(REJECTIONS))
def test_the_verifier_rejects(label):
    build, kwargs, code = REJECTIONS[label]
    doc = build()
    v = run_verify(doc, **kwargs)
    assert v["verdict"] == "REJECT" and code in _codes_of(v), (label, v["reasons"])


def _drop_check(doc, job_id):
    for key, entry in doc["responses"].items():
        if "/check-runs?" in key:
            entry["body"]["check_runs"] = [c for c in entry["body"]["check_runs"] if c["id"] != job_id]
    for key, entry in doc["responses"].items():
        if "/check-runs?" in key:
            entry["body"]["total_count"] = sum(len(e["body"]["check_runs"]) for k, e in doc["responses"].items()
                                               if "/check-runs?" in k)
    return doc


def _edit_check(doc, job_id, **fields):
    for key, entry in doc["responses"].items():
        if "/check-runs?" in key:
            for c in entry["body"]["check_runs"]:
                if c["id"] == job_id:
                    c.update(fields)
    return doc


@pytest.mark.parametrize("label, edit, code", [
    ("its check run is missing", lambda d: _drop_check(d, 10_003), "check-run-missing"),
    ("its check run failed", lambda d: _edit_check(d, 10_003, conclusion="failure"), "check-run-conclusion"),
    ("its check run is on another commit", lambda d: _edit_check(d, 10_003, head_sha=NEWER), "check-run-sha"),
    ("its check run carries another name", lambda d: _edit_check(d, 10_003, name="Backend quality"),
     "check-run-name"),
    ("its check run belongs to another run", lambda d: _edit_check(
        d, 10_003, html_url=f"https://github.com/{REPO}/actions/runs/1/job/10003"), "check-run-run"),
])
def test_each_required_job_is_bound_to_its_own_check_run(label, edit, code):
    v = run_verify(edit(scenario()))
    assert v["verdict"] == "REJECT" and code in _codes_of(v), (label, v["reasons"])


def test_incomplete_or_shifting_pagination_is_refused():
    doc = scenario()
    first = f"/repos/{REPO}/actions/runs/{RUN}/attempts/1/jobs?per_page=100"
    for page in (first, first + "&page=2"):                       # every page claims an item none delivered
        doc["responses"][page]["body"]["total_count"] += 1
    assert "pagination" in _codes_of(run_verify(doc))
    doc = scenario()
    doc["responses"][first]["body"]["total_count"] += 1          # the pages disagree about the total
    assert "pagination" in _codes_of(run_verify(doc))
    doc = scenario()
    doc["responses"][first + "&page=2"]["body"]["total_count"] = 99
    assert "pagination" in _codes_of(run_verify(doc))
    doc = scenario()
    doc["responses"][first + "&page=2"]["body"]["jobs"].append(doc["responses"][first]["body"]["jobs"][0])
    doc["responses"][first + "&page=2"]["body"]["total_count"] = 8
    doc["responses"][first]["body"]["total_count"] = 8
    assert "pagination" in _codes_of(run_verify(doc))


def test_the_pagination_link_is_followed_on_the_api_host_only():
    base = "https://api.github.com"
    assert vme.next_link(f'<{base}/x?page=2>; rel="next", <{base}/x?page=9>; rel="last"', base) == "/x?page=2"
    assert vme.next_link(f'<{base}/x?page=1>; rel="prev"', base) is None
    with pytest.raises(vme.EvidenceError):
        vme.next_link('<https://evil.example/x?page=2>; rel="next"', base)


def _binding(**over):
    b = vme.bind(_bind_env(MILESTONE_ID="M-1", GITHUB_RUN_ID=str(RUN)), SHA, TIP)
    b.update(over)
    return b


def test_the_binding_record_must_agree_with_the_run():
    assert run_verify(scenario(), binding_record=_binding())["verdict"] == "PASS"
    for over in ({"requested_sha": NEWER}, {"main_tip_sha": NEWER}, {"verdict": "REJECTED"}, {"run_id": "1"},
                 {"milestone_id": "M-9"}, {"run_attempt": "2"}, {"milestone_scope": ""}):
        v = run_verify(scenario(), binding_record=_binding(**over))
        assert "binding-record" in _codes_of(v), over


def test_the_evidence_record_carries_the_milestone_fields_and_no_inheritance(tmp_path):
    doc = scenario()
    fx, rec_in, rec_out = tmp_path / "f.json", tmp_path / "b.json", tmp_path / "r.json"
    fx.write_text(json.dumps(doc))
    rec_in.write_text(json.dumps(_binding()))
    rc = vme.main(["verify", "--repo", REPO, "--sha", SHA, "--run", str(RUN), "--fixtures", str(fx),
                   "--binding-record", str(rec_in), "--record-out", str(rec_out)])
    assert rc == vme.EXIT_PASS
    rec = json.loads(rec_out.read_text())
    assert {"milestone_id", "milestone_scope", "tested_sha", "run_id", "run_attempt", "jobs", "verified_at"} <= set(rec)
    assert rec["tested_sha"] == SHA and rec["milestone_scope"] and len(rec["jobs"]) == 7
    assert "do not inherit" in rec["inheritance"]
    doc["responses"].update(_paged(f"/repos/{REPO}/commits/{NEWER}/check-runs?filter=all&per_page=100",
                                   "check_runs", [], 4))
    fx.write_text(json.dumps(doc))
    assert vme.main(["verify", "--repo", REPO, "--sha", NEWER, "--run", str(RUN), "--fixtures", str(fx)]) \
        == vme.EXIT_REJECT
    assert vme.main(["verify", "--repo", REPO, "--sha", SHA, "--run", "1", "--fixtures", str(fx)]) == vme.EXIT_ERROR
