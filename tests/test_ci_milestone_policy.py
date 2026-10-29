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
# Every expression in ci.yml that touches the `github` context, normalised, with its count, as on the base of this
# change. Each is event-independent for what it decides, or decides only what the model above checks:
#   the concurrency group and cancel-in-progress (modelled in the concurrency tests below);
#   github.workflow (inside that group only);
#   github.event.pull_request.head.sha || github.sha (the SBOM identity `--commit`: the tested SHA for push and
#     dispatch alike, asserted below); github.sha (`--checkout`); github.workspace (a path).
ALLOWED_GITHUB_EXPRESSIONS = {
    "github.event_name == 'pull_request' && format('pr-{0}', github.event.pull_request.number) || "
    "format('{0}-{1}', github.ref, github.run_id)": 1,
    "github.event_name == 'pull_request'": 1,
    "github.workflow": 1,
    "github.event.pull_request.head.sha || github.sha": 5,
    "github.sha": 5,
    "github.workspace": 2,
}
# The runner variables that reveal the event, the ref or the workflow (a milestone run differs from a push run only in
# these), in any form: $NAME, ${NAME:-}, os.environ["NAME"], process.env.NAME, env.NAME.
EVENT_VARIABLES = re.compile(
    r"GITHUB_(EVENT_NAME|EVENT_PATH|REF|REF_NAME|REF_TYPE|HEAD_REF|BASE_REF|WORKFLOW|WORKFLOW_REF|WORKFLOW_SHA)\b")
# Code the CI run executes lives under these roots (or at the repository root); a path ci.yml runs outside them fails
# the test, so the scanned universe cannot silently miss a new location.
CODE_ROOTS = ("scripts", "security", "apps", "infra", "tests")
CODE_SUFFIXES = {".py", ".sh", ".bash", ".ts", ".tsx", ".js", ".mjs", ".cjs"}
SKIP_DIRS = {".git", "node_modules", ".venv", "__pycache__", "site-packages", "dist", "build", ".pytest_cache"}
# The only matches allowed, per file, each justified. Exemptions are per MATCH, not per file (MINOR-10): every
# matching line is pinned exactly (as a multiset), so a new read inside an allow-listed file fails the guard too.
EVENT_VARIABLE_READERS = {
    # the bindings themselves: run by milestone-validation.yml and main-push.yml, never by ci.yml
    "scripts/verify_milestone_evidence.py": (
        'event = env.get("GITHUB_EVENT_NAME", "")',
        'ref = env.get("GITHUB_REF", "")',
        'workflow_ref = env.get("GITHUB_WORKFLOW_REF", "")',
        'event = env.get("GITHUB_EVENT_NAME", "")',
        'ref = env.get("GITHUB_REF", "")',
        'workflow_ref = env.get("GITHUB_WORKFLOW_REF", "")',
        'path = os.environ.get("GITHUB_EVENT_PATH")',
    ),
    # models scripts/docker_boundary.py's CI-marker check, which tests GITHUB_WORKFLOW for PRESENCE only (the policy's
    # expected_marker_values constrains GITHUB_ACTIONS alone; pinned below), so it cannot tell a milestone run apart;
    # it sets and pops the markers, it never reads a real value
    "tests/test_i28at_docker_boundary.py": (
        '_GH_MARKERS = ("GITHUB_ACTIONS", "GITHUB_RUN_ID", "GITHUB_WORKFLOW")',
        'os.environ.update({"GITHUB_ACTIONS": "true", "GITHUB_RUN_ID": "1", "GITHUB_WORKFLOW": "ci"})',
        'os.environ.update({"GITHUB_ACTIONS": "yes", "GITHUB_RUN_ID": "1", "GITHUB_WORKFLOW": "ci"})',
        'for name in ("GITHUB_ACTIONS", "GITHUB_RUN_ID", "GITHUB_WORKFLOW"):',
    ),
}
# This module names the variables in its own patterns, pinned lines and test environments; its lines cannot pin
# themselves, so its match COUNT is pinned instead (a new read here changes it).
SELF_READER = "tests/test_ci_milestone_policy.py"
SELF_READER_MATCHES = 39
_GITHUB_WORD = re.compile(r"(?i)(?<![\w.$-])github(?![\w-])")


def _strings(node):
    """Every string in a parsed YAML document, keys included."""
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _strings(k)
            yield from _strings(v)
    elif isinstance(node, list):
        for v in node:
            yield from _strings(v)
    elif isinstance(node, str):
        yield node


def _conditions(doc):
    """Every job-level and step-level `if:` value (an implicit expression even without ${{ }})."""
    for job in (doc.get("jobs") or {}).values():
        if "if" in job:
            yield str(job["if"])
        for step in job.get("steps") or []:
            if "if" in step:
                yield str(step["if"])


def _package_scripts() -> dict:
    """The `scripts` of every repository package.json (ci.yml runs them through npm), keyed `<path>#scripts`."""
    found = {}
    candidates = [REPO_ROOT / "package.json"] + [p for p in (REPO_ROOT / "apps").glob("*/package.json")]
    for path in candidates:
        if path.is_file():
            scripts = json.loads(path.read_text(encoding="utf-8")).get("scripts") or {}
            found[f"{path.relative_to(REPO_ROOT).as_posix()}#scripts"] = "\n".join(
                f"{name}: {command}" for name, command in sorted(scripts.items()))
    return found


def test_the_package_scripts_ci_runs_are_scanned():
    """R2-2: ci.yml runs npm scripts (lint, type-check, test, build, gen:types, e2e:install); they are in the scan."""
    scripts = _package_scripts()
    assert {"package.json#scripts", "apps/web/package.json#scripts"} <= set(scripts)
    assert "test: vitest run" in scripts["apps/web/package.json#scripts"]
    ci_text = CI.read_text(encoding="utf-8")
    for invoked in re.findall(r"npm run ([\w:-]+)", ci_text):
        assert any(f"\n{invoked}: " in "\n" + text for text in scripts.values()), f"npm run {invoked} is not scanned"


def _code_files():
    roots = [REPO_ROOT / r for r in CODE_ROOTS]
    files = [p for p in REPO_ROOT.iterdir() if p.is_file() and p.suffix in CODE_SUFFIXES]
    for root in roots:
        stack = [root]
        while stack:
            d = stack.pop()
            if d.name in SKIP_DIRS or (d / "pyvenv.cfg").exists():
                continue
            for entry in d.iterdir():
                if entry.is_dir():
                    stack.append(entry)
                elif entry.suffix in CODE_SUFFIXES:
                    files.append(entry)
    return files


def event_gating_problems(ci_text: str, code_overrides: dict | None = None) -> list[str]:
    """Every way ci.yml (or code it runs) could behave differently in a milestone run than in a push run."""
    problems = []
    doc = yaml.safe_load(ci_text)
    found = {}
    for string in _strings(doc):
        for m in _TEMPLATE.finditer(string):
            expr = " ".join(m.group(1).split())
            if _GITHUB_WORD.search(expr):
                found[expr] = found.get(expr, 0) + 1
    if found != ALLOWED_GITHUB_EXPRESSIONS:
        problems.append(f"a `github` context expression changed: {found}")
    for cond in _conditions(doc):
        if _GITHUB_WORD.search(cond) or "inputs" in cond or "toJSON" in cond:
            problems.append(f"a condition reads the run's context: {cond!r}")
    for job_id, job in (doc.get("jobs") or {}).items():
        if "if" in job:
            problems.append(f"{job_id}: a job-level condition")
    if EVENT_VARIABLES.search(ci_text):
        problems.append("ci.yml names an event/ref/workflow runner variable")
    texts = {p.relative_to(REPO_ROOT).as_posix(): p.read_text(encoding="utf-8", errors="replace")
             for p in _code_files()}
    texts.update(_package_scripts())
    texts.update(code_overrides or {})
    readers = {rel for rel, text in texts.items() if EVENT_VARIABLES.search(text)}
    if readers != set(EVENT_VARIABLE_READERS) | {SELF_READER}:
        problems.append(f"event/ref/workflow variable readers changed: {sorted(readers)}")
    for rel, pinned in EVENT_VARIABLE_READERS.items():
        lines = sorted(line.strip() for line in texts.get(rel, "").splitlines() if EVENT_VARIABLES.search(line))
        if lines != sorted(pinned) or len(EVENT_VARIABLES.findall(texts.get(rel, ""))) != len(pinned):
            problems.append(f"{rel}: its event/ref/workflow variable matches changed: {lines}")
    own = len(EVENT_VARIABLES.findall(texts.get(SELF_READER, "")))
    if own != SELF_READER_MATCHES:
        problems.append(f"{SELF_READER}: {own} event/ref/workflow variable matches, pinned {SELF_READER_MATCHES}")
    return problems


def test_no_ci_step_or_job_is_gated_on_the_event_so_the_milestone_suite_runs_everything_a_main_push_ran():
    """Coordinator review point 1 and ADV-3: the called suite must execute at least what a push-to-main run did."""
    assert event_gating_problems(CI.read_text(encoding="utf-8")) == []
    marker = json.loads((REPO_ROOT / "tests/fixtures/docker-boundary-policy.json").read_text(encoding="utf-8"))
    assert marker["ci_assumption"]["expected_marker_values"] == {"GITHUB_ACTIONS": "true"}, \
        "a value constraint on GITHUB_WORKFLOW would make the Docker boundary differ between a push and a milestone run"
    push_main = {"github": {"event_name": "push", "sha": SHA, "ref": "refs/heads/main", "event": {"after": SHA}}}
    for event_name in ("workflow_dispatch", "workflow_call"):
        milestone = {"github": {"event_name": event_name, "sha": SHA, "ref": "refs/heads/main", "event": {}}}
        for ctx in (push_main, milestone):
            assert evaluate("${{ github.event.pull_request.head.sha || github.sha }}", ctx) == SHA


PG_STEP = "      - name: PostgreSQL proofs (P6-CI-4 migration chain, P6-CI-5 job-store concurrency)\n"


@pytest.mark.parametrize("label, line", [
    ("index syntax", "        if: github['event_name'] != 'workflow_dispatch'\n"),
    ("toJSON of the whole context", "        if: ${{ !contains(toJSON(github), 'workflow_dispatch') }}\n"),
    ("another spelling of the context", "        if: GitHub.event_name != 'workflow_dispatch'\n"),
    ("an inputs condition", "        if: inputs.milestone_id == ''\n"),
])
def test_the_event_gating_guard_catches_a_conditioned_step(label, line):
    text = CI.read_text(encoding="utf-8")
    assert text.count(PG_STEP) == 1
    assert event_gating_problems(text.replace(PG_STEP, PG_STEP + line)), label


def test_the_event_gating_guard_catches_run_text_and_invoked_scripts():
    text = CI.read_text(encoding="utf-8")
    early_exit = text.replace("            app.tests.test_postgres_job_store_concurrency \\\n",
                              "            app.tests.test_postgres_job_store_concurrency \\\n"
                              '          if [ "$GITHUB_EVENT_NAME" = workflow_dispatch ]; then exit 0; fi\n', 1)
    assert early_exit != text and event_gating_problems(early_exit)
    smoke = (REPO_ROOT / "scripts/ci-smoke.sh").read_text(encoding="utf-8")
    for injected in ('[ "${GITHUB_EVENT_NAME:-}" = workflow_dispatch ] && exit 0\n',
                     '[ "$GITHUB_WORKFLOW" != CI ] && exit 0\n'):
        assert event_gating_problems(text, {"scripts/ci-smoke.sh": smoke + injected})
    assert event_gating_problems(text, {"apps/web/vite.config.ts": "if (process.env.GITHUB_REF_NAME) {}"})
    web = _package_scripts()["apps/web/package.json#scripts"]
    assert event_gating_problems(text, {"apps/web/package.json#scripts": web.replace(
        "test: vitest run", 'test: [ "$GITHUB_EVENT_NAME" = workflow_dispatch ] || vitest run')})


def test_a_new_read_inside_an_allow_listed_file_is_caught():
    """MINOR-10: the exemptions are per match; a new read inside the verifier or test_i28at fails the guard."""
    text = CI.read_text(encoding="utf-8")
    for rel, injected in (("scripts/verify_milestone_evidence.py", '_PROBE = os.environ.get("GITHUB_EVENT_NAME")\n'),
                          ("tests/test_i28at_docker_boundary.py", '_PROBE = os.environ.get("GITHUB_REF_NAME")\n')):
        original = (REPO_ROOT / rel).read_text(encoding="utf-8")
        assert event_gating_problems(text, {rel: original + injected}), rel
        moved = original.replace('"GITHUB_WORKFLOW"', '"GITHUB_REF"', 1)
        assert moved == original or event_gating_problems(text, {rel: moved}), rel


def test_every_path_ci_runs_is_inside_the_scanned_roots():
    """The event-variable scan covers the code ci.yml runs: every working directory and script path is in a root."""
    doc = _load(CI)
    for job in doc["jobs"].values():
        for step in job.get("steps") or []:
            wd = step.get("working-directory")
            if wd:
                assert wd.split("/")[0] in CODE_ROOTS, wd
            for token in re.findall(r"[\w./-]+\.(?:sh|py|ts|js|mjs|cjs)\b", str(step.get("run", ""))):
                resolved = (REPO_ROOT / (wd or "") / token).resolve()
                if not resolved.is_file():
                    continue
                rel = resolved.relative_to(REPO_ROOT)
                assert len(rel.parts) == 1 or rel.parts[0] in CODE_ROOTS, f"ci.yml runs {rel} outside the roots"


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
SHORT_RECORD_RUN = ('python3 scripts/verify_milestone_evidence.py short-record --checked-out "$(git rev-parse HEAD)" '
                    "--out short-check-evidence/short-check-binding.json")
BIND_RUN = ('python3 scripts/verify_milestone_evidence.py bind --checked-out "$(git rev-parse HEAD)" '
            '--main-tip "$(git ls-remote --exit-code origin refs/heads/main)" '
            "--out milestone-evidence/milestone-binding.json")


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
    assert steps[1] == {"name": "Bind this run to the commit it checked out (main short check evidence)",
                        "run": SHORT_RECORD_RUN}, "the binding step is pinned exactly: no install, no masking"
    assert steps[2:-1] == ism[1:], "every install, scan, gate and control step is the reviewed twin's"
    assert steps[-1] == {"name": "Upload the main short check binding record", "if": "always()",
                         "uses": "actions/upload-artifact@v7",
                         "with": {"name": "short-check-evidence-attempt-${{ github.run_attempt }}",
                                  "path": "short-check-evidence/", "retention-days": 90,
                                  "if-no-files-found": "error"}}
    assert "continue-on-error" not in MAIN_PUSH.read_text(encoding="utf-8")


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
    job = doc["jobs"]["milestone-binding"]
    assert set(job) == {"name", "runs-on", "timeout-minutes", "steps"}, "no continue-on-error, if:, needs: or env:"
    steps = job["steps"]
    assert steps == [
        {"name": "Check out repository", "uses": "actions/checkout@v7"},
        {"name": "Bind the request to the dispatched commit, the checked-out commit and the main tip",
         "env": {"MILESTONE_ID": "${{ inputs.milestone_id }}", "MILESTONE_SCOPE": "${{ inputs.milestone_scope }}",
                 "MILESTONE_SHA": "${{ inputs.milestone_sha }}"},
         "run": BIND_RUN},
        {"name": "Upload the milestone binding record", "if": "always()", "uses": "actions/upload-artifact@v7",
         "with": {"name": "milestone-evidence-attempt-${{ github.run_attempt }}", "path": "milestone-evidence/",
                  "retention-days": 90, "if-no-files-found": "error"}},
    ], "the binding job is pinned exactly: no install, no masking, no continue-on-error"
    text = MILESTONE.read_text(encoding="utf-8")
    assert "continue-on-error" not in text and "trivy" not in text.lower()


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


@pytest.mark.parametrize("over, code", [
    ({"MILESTONE_ID": "M-1\n"}, "milestone-id"),
    ({"MILESTONE_ID": "M-1\u202e"}, "milestone-id"),
    ({"MILESTONE_SCOPE": "line one\u2028line two"}, "milestone-scope"),
    ({"MILESTONE_SCOPE": "line one\u0085line two"}, "milestone-scope"),
    ({"MILESTONE_SCOPE": "scope \u202edesrever"}, "milestone-scope"),
    ({"MILESTONE_SCOPE": "zero\u200bwidth"}, "milestone-scope"),
    ({"MILESTONE_SHA": SHA + "\n"}, "requested-sha"),
    ({"MILESTONE_SCOPE": "para one\u2029para two"}, "milestone-scope"),
])
def test_input_validation_matches_whole_values_and_one_printable_line(over, code):
    rec = vme.bind(_bind_env(**over), SHA, TIP)
    assert rec["verdict"] == "REJECTED" and code in _codes(rec), rec["reasons"]


def test_whole_value_matching_in_verify():
    assert not vme._is_sha(SHA + "\n") and vme._is_sha(SHA)
    for kwargs in ({"milestone_id": "M-1\n"}, {"repo": REPO + "\n"}):
        with pytest.raises(vme.EvidenceError):
            vme.verify(vme.FixtureApi(scenario()), kwargs.get("repo", REPO), SHA, RUN,
                       milestone_id=kwargs.get("milestone_id"))


def test_only_the_exact_main_ref_counts_in_ls_remote_output():
    """ls-remote matches the pattern against ref-name tails: a branch `x/refs/heads/main` must not block binding."""
    decoy = f"{NEWER}\trefs/heads/x/refs/heads/main\n"
    rec = vme.bind(_bind_env(), SHA, TIP + decoy)
    assert rec["verdict"] == "BOUND" and rec["main_tip_sha"] == SHA, rec["reasons"]
    rec = vme.bind(_bind_env(), SHA, decoy + TIP)
    assert rec["verdict"] == "BOUND" and rec["main_tip_sha"] == SHA, rec["reasons"]
    assert vme.parse_ls_remote(decoy) == (None, "expected exactly one refs/heads/main line from git ls-remote, got 0")


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
T0 = "2026-10-29T04:59:00Z"          # the default attempt's run_started_at
T_JOB = "2026-10-29T05:00:00Z"       # the default job start (after T0)


def _job(name, jid, attempt=1, conclusion="success", status="completed", sha=SHA, run_id=RUN, started_at=T_JOB):
    return {"id": jid, "run_id": run_id, "run_attempt": attempt, "name": name, "head_sha": sha, "status": status,
            "conclusion": conclusion, "started_at": started_at, "completed_at": "2026-10-29T08:00:00Z",
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
             base_id=10_000, run_started_at=T0, job_started_at=T_JOB, compare="identical") -> dict:
    jobs = jobs if jobs is not None else [_job(n, base_id + i, attempt, sha=sha, run_id=run_id,
                                               started_at=job_started_at)
                                          for i, n in enumerate(vme.REQUIRED_JOB_NAMES)]
    run = {"id": run_id, "path": path, "event": event, "head_branch": branch, "head_sha": sha,
           "display_title": title if title is not None else vme.RUN_NAME.format(milestone_id="M-1", sha=sha),
           "status": "completed", "conclusion": conclusion, "run_attempt": attempt, "run_started_at": run_started_at,
           "repository": {"full_name": REPO}, "html_url": f"https://github.com/{REPO}/actions/runs/{run_id}"}
    responses = {f"/repos/{REPO}/actions/runs/{run_id}": {"body": run, "next": None}}
    if compare is not None:
        responses[f"/repos/{REPO}/compare/{sha}...refs/heads/main"] = {"body": {"status": compare}, "next": None}
    responses.update(_paged(f"/repos/{REPO}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100", "jobs",
                            jobs, per_page))
    checks = [_check(j) for j in jobs] + list(extra_checks)
    responses.update(_paged(f"/repos/{REPO}/commits/{sha}/check-runs?filter=all&per_page=100", "check_runs",
                            checks, per_page))
    return {"responses": responses}


def _record_for(doc, run_id):
    """The run's own binding record (what milestone-evidence-attempt-<latest> carries) for a fixture run."""
    run = doc["responses"][f"/repos/{REPO}/actions/runs/{run_id}"]["body"]
    m = re.fullmatch(r"Milestone (\S+) at [0-9a-f]{40}", run.get("display_title") or "")
    head = run["head_sha"]
    return vme.bind(_bind_env(GITHUB_SHA=head, MILESTONE_SHA=head, GITHUB_RUN_ID=str(run_id),
                              GITHUB_RUN_ATTEMPT=str(run["run_attempt"]), MILESTONE_ID=m.group(1) if m else "M-1"),
                    head, f"{head}\trefs/heads/main\n")


def run_verify(doc, sha=SHA, run_id=RUN, **kw):
    """verify() with the run's own binding record unless the test passes one (or None) explicitly."""
    kw.setdefault("binding_record", _record_for(doc, run_id))
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
    # Re-run ALL jobs: attempt 2 starts at 07:00 and every job starts at or after it, with new ids.
    doc = scenario(attempt=2, base_id=30_000, extra_checks=[_check(j) for j in failed_first],
                   run_started_at="2026-10-29T07:00:00Z", job_started_at="2026-10-29T07:00:00Z")
    assert run_verify(doc)["verdict"] == "PASS"
    assert run_verify(doc, attempt=2)["verdict"] == "PASS"


def _real_partial_rerun_jobs():
    """The shape GitHub returns after "Re-run failed jobs" (measured, runs 40156345312/37984410563/36409983538):
    EVERY job carries the latest run_attempt and a new id; the five not re-run keep their attempt-1 started_at."""
    rerun = {"Milestone full suite / Backend quality", "Milestone full suite / Integration smoke"}
    return [_job(n, 70_000 + i, 2, started_at="2026-10-29T07:01:00Z" if n in rerun else T_JOB)
            for i, n in enumerate(vme.REQUIRED_JOB_NAMES)]


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
    "a job carried over from an earlier attempt (real shape: latest stamp, new id, earlier start)": (
        lambda: scenario(attempt=2, jobs=_real_partial_rerun_jobs(), run_started_at="2026-10-29T07:00:00Z"), {},
        "job-carried-over"),
    "a job stamped with an earlier attempt": (lambda: scenario(attempt=2, jobs=[
        _job(n, 10_000 + i, attempt=1 if i == 2 else 2) for i, n in enumerate(vme.REQUIRED_JOB_NAMES)]), {},
        "job-carried-over"),
    "a job without a start time": (lambda: scenario(jobs=_mutated_jobs(lambda j: j[3].pop("started_at"))), {},
                                   "job-carried-over"),
    "a run without run_started_at": (lambda: scenario(run_started_at=None), {}, "run-started-at"),
    "no binding record": (lambda: scenario(), {"binding_record": None}, "binding-record"),
    "a SHA behind main's history (compare: behind)": (lambda: scenario(compare="behind"), {}, "not-on-main"),
    "a SHA off main (compare: diverged)": (lambda: scenario(compare="diverged"), {}, "not-on-main"),
    "a SHA the compare call cannot place": (lambda: scenario(compare=None), {}, "not-on-main"),
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


def test_the_compare_names_the_branch_ref_unambiguously():
    """R2-1: the on-main check compares against refs/heads/main, never a bare `main` (a tag can be called main)."""
    api = vme.FixtureApi(scenario())
    assert vme.verify(api, REPO, SHA, RUN, binding_record=_record_for(scenario(), RUN))["verdict"] == "PASS"
    compares = [r for r in api.requests if "/compare/" in r]
    assert compares == [f"/repos/{REPO}/compare/{SHA}...refs/heads/main"]
    assert not any(r.endswith("...main") for r in api.requests)


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


def test_a_malformed_response_cannot_establish_and_exits_2(tmp_path):
    doc = scenario()
    first = f"/repos/{REPO}/actions/runs/{RUN}/attempts/1/jobs?per_page=100"
    doc["responses"][first]["body"]["jobs"][0] = "not a job object"
    fx, rec = tmp_path / "f.json", tmp_path / "b.json"
    fx.write_text(json.dumps(doc))
    rec.write_text(json.dumps(_record_for(doc, RUN)))
    assert vme.main(["verify", "--repo", REPO, "--sha", SHA, "--run", str(RUN), "--fixtures", str(fx),
                     "--binding-record", str(rec)]) == vme.EXIT_ERROR


def test_a_pass_needs_the_binding_record():
    """ADV-1: without the run's own binding record the verifier never passes, whatever the API shows."""
    v = run_verify(scenario(), binding_record=None)
    assert v["verdict"] == "REJECT" and _codes_of(v) == {"binding-record"}

