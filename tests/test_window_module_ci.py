"""Staging-window module suites in CI — hermetic regression coverage (docs/operations/staging-window.md §11).

WHAT THIS PROVES, WITHOUT RUNNING OPENTOFU. The three window suites (alb 3 runs, data_cache 2,
network 2) are wired into the `revision-reader` job as graded steps whose failure fails the job,
each module carries the root's byte-identical provider constraint (the modules/iam Gate 4N-I8
rule — a standalone init without it resolved a newer provider than the 6.55.0 toolchain
contract and was refused by the cache classification), the three module caches are members of
the review-pinned EXPECTED_CACHE_ROOTS collection under an ACTIVE ledger record, and a negative
control step requires a doctored copy of the network suite to FAIL `tofu test`.

Layering (Gate 4N-I28BH-E6 lesson): everything here reads repository text — ci.yml, the
invocation contract, the pin registry, the ledger, versions.tf and the .tftest.hcl files. The
tofu-EXECUTING half (the suites themselves and the negative control) runs in the graded CI
steps; `scripts/failure_propagation.py` and `scripts/ci_invocation_model.py` grade the step
shells structurally, and this file pins the facts those guards rely on so a silent unwiring
is caught by pytest as well as by the guards.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import ci_invocation_model as cim  # noqa: E402
import failure_propagation as fp  # noqa: E402

WORKFLOW = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
CONTRACT = json.loads((REPO_ROOT / "tests" / "fixtures" / "ci-invocation-contract.json").read_text(encoding="utf-8"))
PINS = json.loads((REPO_ROOT / "tests" / "fixtures" / "review-pin-registry.json").read_text(encoding="utf-8"))
LEDGER = json.loads((REPO_ROOT / "tests" / "fixtures" / "review-record-ledger.json").read_text(encoding="utf-8"))
ROOT_VERSIONS = (REPO_ROOT / "infra" / "aws" / "versions.tf").read_text(encoding="utf-8")

MODULES = {"alb": 3, "data_cache": 2, "network": 2}
# P6-WINDOW-DNS-BUDGET: the alb step also runs api_alias.tftest.hcl (the P6-INF-3 windowed API
# alias record — `tofu test` runs every *.tftest.hcl in the module directory), and a ROOT suite
# proves what only the root can (budget bound, api_fqdn/web_fqdn collision, alias wiring).
EXTRA_SUITES = {"alb": {"api_alias.tftest.hcl": 6}}
ROOT_SUITE = ("input_boundary.tftest.hcl", 7)
ROOT_STEP = "root_boundary_tests"
SUITE_STEPS = {m: f"window_tests_{m}" for m in MODULES}
NEGATIVE_STEP = "window_negative_control"
COLLECTION = "check_toolchain_integrity.py::EXPECTED_CACHE_ROOTS"
RECORD = "REV-2026-10-01-P6-STAGING-WINDOW-CI-CACHE-ROOTS"


def _steps() -> dict:
    return {s["id"]: s for s in cim.parse_steps(WORKFLOW)}


def _constraint(text: str) -> str:
    m = re.search(r'aws\s*=\s*\{[^}]*?version\s*=\s*"([^"]+)"', text, re.S)
    assert m, "no aws provider version constraint"
    return m.group(1)


# --------------------------------------------------------------------------- suites exist
def test_each_window_suite_declares_the_expected_run_count():
    for module, runs in MODULES.items():
        text = (REPO_ROOT / "infra" / "aws" / "modules" / module / "window.tftest.hcl").read_text(encoding="utf-8")
        assert len(re.findall(r'^run "', text, re.M)) == runs, module
        assert 'mock_provider "aws"' in text, f"{module}: the suite must be fully mocked (no AWS call)"


def test_the_extra_module_suites_and_the_root_suite_declare_their_run_counts():
    for module, suites in EXTRA_SUITES.items():
        for name, runs in suites.items():
            text = (REPO_ROOT / "infra" / "aws" / "modules" / module / name).read_text(encoding="utf-8")
            assert len(re.findall(r'^run "', text, re.M)) == runs, (module, name)
            assert 'mock_provider "aws"' in text, f"{module}/{name}: the suite must be fully mocked (no AWS call)"
    name, runs = ROOT_SUITE
    text = (REPO_ROOT / "infra" / "aws" / name).read_text(encoding="utf-8")
    assert len(re.findall(r'^run "', text, re.M)) == runs, name
    assert 'mock_provider "aws"' in text and 'alias = "revision_reader"' in text, \
        "the root suite must mock BOTH provider configurations (default + aws.revision_reader)"
    assert "expect_failures = [var.monthly_budget_limit]" in text and "expect_failures = [var.api_fqdn]" in text, \
        "the root suite must exercise the input-boundary rejections, not only the accepted path"


def test_the_ci_comment_states_the_full_alb_run_count():
    total = MODULES["alb"] + sum(EXTRA_SUITES["alb"].values())
    assert f"Expected runs: alb {total} (window.tftest.hcl" in WORKFLOW, \
        "ci.yml must state the alb step's complete run count (window + api_alias suites) for the log reader"


# --------------------------------------------------------------------------- CI wiring
def test_the_root_suite_step_is_a_graded_block_step_in_the_root_directory():
    step = _steps().get(ROOT_STEP)
    assert step is not None, f"{ROOT_STEP} is absent from ci.yml"
    assert step["form"] == "block" and step["run"], ROOT_STEP
    assert not step.get("continue_on_error"), f"{ROOT_STEP}: continue-on-error would mask failure"
    block = re.search(r"id: %s\n(.*?)\n\n" % re.escape(ROOT_STEP), WORKFLOW, re.S).group(1)
    assert "if: always()" in block and "working-directory: infra/aws" in block, ROOT_STEP
    lines = [ln.strip() for ln in step["run"].splitlines() if ln.strip()]
    assert lines == ["tofu fmt -check -diff .", "tofu init -backend=false -input=false", "tofu test"], ROOT_STEP
    # -backend=false is load-bearing: the root declares the committed S3 backend, which CI must never initialise.
    assert "-backend=false" in lines[1]


def test_the_three_suite_steps_are_graded_block_steps_in_their_module_directories():
    steps = _steps()
    for module, sid in SUITE_STEPS.items():
        step = steps.get(sid)
        assert step is not None, f"{sid} is absent from ci.yml"
        assert step["form"] == "block" and step["run"], sid
        assert not step.get("continue_on_error"), f"{sid}: continue-on-error would mask failure"
        block = re.search(r"id: %s\n(.*?)\n\n" % re.escape(sid), WORKFLOW, re.S).group(1)
        assert "if: always()" in block, f"{sid}: must run (and report) regardless of earlier step outcomes, like iam_module_tests"
        assert f"working-directory: infra/aws/modules/{module}" in block, sid
        lines = [ln.strip() for ln in step["run"].splitlines() if ln.strip()]
        assert lines == ["tofu fmt -check -diff .", "tofu init -backend=false -input=false", "tofu test"], sid


def test_the_suite_steps_run_before_the_post_init_cache_classification():
    order = [s["id"] for s in cim.parse_steps(WORKFLOW)]
    post = order.index("toolchain_post")
    pre = order.index("toolchain_pre")
    for sid in list(SUITE_STEPS.values()) + [NEGATIVE_STEP, ROOT_STEP]:
        assert pre < order.index(sid) < post, f"{sid} must sit between the pre-init sanitisation and the post-init classification"


def test_every_new_step_is_in_the_invocation_contract_with_tofu_required():
    for sid in list(SUITE_STEPS.values()) + [NEGATIVE_STEP, ROOT_STEP]:
        entry = CONTRACT["graded_steps"].get(sid)
        assert entry == {"must_invoke": ["TOFU"], "run_form": "block"}, sid


def test_the_invocation_model_and_failure_propagation_accept_the_new_steps():
    result = cim.check(WORKFLOW)
    rows = {r["id"]: r for r in result["rows"]}
    for sid in list(SUITE_STEPS.values()) + [NEGATIVE_STEP, ROOT_STEP]:
        assert rows[sid]["present"], sid
        assert not [p for p in result["problems"] if p.startswith(f"{sid}:")], result["problems"]
    graded = {s["id"] for s in fp.analyse()["steps"] if s.get("graded")} if isinstance(fp.analyse().get("steps"), list) else None
    if graded is not None:
        for sid in list(SUITE_STEPS.values()) + [NEGATIVE_STEP, ROOT_STEP]:
            assert sid in graded, sid


def test_the_negative_control_requires_a_doctored_suite_to_fail():
    step = _steps()[NEGATIVE_STEP]
    run = step["run"]
    assert "if: always()" in re.search(r"id: %s\n(.*?)run: \|" % re.escape(NEGATIVE_STEP), WORKFLOW, re.S).group(1)
    assert "set -euo pipefail" in run.splitlines()[0]
    assert 'shutil.copytree("infra/aws/modules/network", work / "network"' in run, "the doctored copy is made from the real module, outside the tree"
    assert "condition     = var.name_prefix != var.name_prefix" in run, \
        "the injected assertion must be always-false yet reference a module input (OpenTofu rejects a bare `false`)"
    assert '-plugin-dir="$GITHUB_WORKSPACE/infra/aws/modules/network/.terraform/providers"' in run, \
        "the doctored copy must initialise OFFLINE from the already-verified network cache, never a fresh download"
    assert "python3 - \"$work/negative.log\" <<'PY'" in run, "the inverted check uses the repository's python-heredoc step pattern"
    assert "cp " not in run and "rm " not in run, "no executable outside the executable-trust policy (copy/doctoring happen inside the python heredoc)"
    assert "proc.returncode != 0" in run and "sys.exit(0 if ok else 1)" in run
    assert "'run \"negative_control_must_fail\"... fail' in text" in run
    assert '"1 failed" in text and "2 passed" in text' in run, "the two real network runs must still pass inside the doctored copy"
    assert not step.get("continue_on_error")


def test_the_job_result_list_reads_every_new_step_outcome():
    for sid in list(SUITE_STEPS.values()) + [NEGATIVE_STEP, ROOT_STEP]:
        assert f"{sid}=${{{{ steps.{sid}.outcome }}}}" in WORKFLOW, f"{sid}: outcome not read by the guard result list"


# --------------------------------------------------------------------------- provider constraint
def test_each_module_carries_the_root_provider_constraint_byte_identical():
    root = _constraint(ROOT_VERSIONS)
    for module in list(MODULES) + ["iam", "revision_reader"]:
        text = (REPO_ROOT / "infra" / "aws" / "modules" / module / "versions.tf").read_text(encoding="utf-8")
        assert _constraint(text) == root, module
        code = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
        assert not re.search(r'^\s*provider\s+"aws"', code, re.M), f"{module}: a child module must not declare a provider block"


def test_child_module_lockfiles_stay_prohibited():
    ignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "infra/aws/modules/*/.terraform.lock.hcl" in ignore.splitlines()
    # a locally generated, ignored lockfile may exist on a developer machine; what matters is that
    # none is ever TRACKED — proven by test_no_child_module_lockfile_is_tracked below


def test_no_child_module_lockfile_is_tracked():
    import subprocess
    tracked = subprocess.run(["git", "ls-files", "--", "infra/aws/modules"], cwd=REPO_ROOT,
                             capture_output=True, text=True).stdout.splitlines()
    assert not [p for p in tracked if p.endswith(".terraform.lock.hcl")]


# --------------------------------------------------------------------------- cache roots + review binding
def test_the_three_module_roots_are_expected_cache_roots_under_an_active_review():
    import check_toolchain_integrity as cti
    roots = [str(p) for p in cti.EXPECTED_CACHE_ROOTS]
    for module in MODULES:
        assert f"infra/aws/modules/{module}" in roots, module
    for keep in ("infra/aws", "infra/aws/modules/revision_reader", "infra/aws/modules/iam"):
        assert keep in roots, f"prior member {keep} must be preserved"
    pin = PINS["pins"][COLLECTION]
    assert pin["review_record_id"] == RECORD
    import review_pin_control as rpc
    assert pin["reviewed_digest"] == rpc.canonical_digest(COLLECTION, cti.EXPECTED_CACHE_ROOTS, pin["ordered"]), \
        "the pin must bind the CURRENT reviewed membership (a stale pin, or an unreviewed member, fails here as well as in the assurance guard)"
    record = LEDGER["review_records"][RECORD]
    assert record["status"] == "ACTIVE"
    for module in MODULES:
        assert f"infra/aws/modules/{module}" in record["scope"], module


def test_the_root_wiring_mirror_source_set_is_not_widened():
    import root_wiring_check as rwc
    mirror = {str(p) for p in rwc.CACHE_ROOTS}
    assert mirror == {"infra/aws", "infra/aws/modules/revision_reader", "infra/aws/modules/iam"}
